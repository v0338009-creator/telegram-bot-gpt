import os
import re
import html
import json
import logging
from collections import defaultdict, deque
from urllib.parse import urlparse, unquote

import aiomysql
from openai import AsyncOpenAI
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
AI_API_KEY = os.environ.get("AI_API_KEY")
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://api.groq.com/openai/v1")
MODEL = os.environ.get("AI_MODEL", "openai/gpt-oss-120b")
DATABASE_URL = os.environ.get("DATABASE_URL")  # mysql://user:pass@host:3306/dbname

HISTORY_LIMIT = 10
CHUNK_SIZE = 3500

SYSTEM_PROMPT = (
    "Ты дружелюбный ИИ-ассистент в Telegram. Отвечай по-русски, понятно и по делу. "
    "Оформляй ответ в Markdown: **жирный** для главного, списки через '-', код в блоках ```. "
    "Не используй таблицы и HTML."
)

client = AsyncOpenAI(base_url=AI_BASE_URL, api_key=AI_API_KEY)
history = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
no_preview = LinkPreviewOptions(is_disabled=True)

# Пул соединений MySQL
db_pool: aiomysql.Pool | None = None

MENU = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("🧹 Новый диалог", callback_data="reset"),
        InlineKeyboardButton("ℹ️ Помощь", callback_data="help"),
    ]
])

WELCOME = (
    "<b>👋 Привет, {name}!</b>\n\n"
    "Я ИИ-ассистент. Задай любой вопрос: объясню тему, помогу с кодом, "
    "напишу текст или подкину идею.\n\n"
    "<i>Я помню последние сообщения, так что можно задавать уточняющие вопросы.</i>"
)

HELP = (
    "<b>ℹ️ Как пользоваться</b>\n\n"
    "• Просто напиши сообщение, и я отвечу\n"
    "• /reset — начать диалог заново\n"
    "• /help — эта справка\n\n"
    f"<b>Модель:</b> <code>{html.escape(MODEL)}</code>"
)

RESET_TEXT = "🧹 <b>Память очищена.</b> Начнём с чистого листа!"
THINKING_TEXT = "💭 <i>Думаю…</i>"


# ---------- Схема MySQL ----------

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id        BIGINT PRIMARY KEY,
        username       VARCHAR(64),
        first_name     VARCHAR(255),
        last_name      VARCHAR(255),
        language_code  VARCHAR(16),
        is_bot         TINYINT(1) NOT NULL DEFAULT 0,
        created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_seen_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        messages_count INT NOT NULL DEFAULT 0
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id         BIGINT AUTO_INCREMENT PRIMARY KEY,
        user_id    BIGINT NOT NULL,
        chat_id    BIGINT NOT NULL,
        role       ENUM('user','assistant','system') NOT NULL,
        content    MEDIUMTEXT NOT NULL,
        model      VARCHAR(128),
        length     INT NOT NULL DEFAULT 0,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_messages_user (user_id),
        INDEX idx_messages_chat (chat_id),
        INDEX idx_messages_time (created_at),
        CONSTRAINT fk_messages_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id         BIGINT AUTO_INCREMENT PRIMARY KEY,
        user_id    BIGINT,
        chat_id    BIGINT,
        kind       VARCHAR(64) NOT NULL,
        payload    JSON,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_events_user (user_id),
        INDEX idx_events_time (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS errors (
        id         BIGINT AUTO_INCREMENT PRIMARY KEY,
        user_id    BIGINT,
        chat_id    BIGINT,
        message    TEXT,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_errors_time (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]


def parse_db_url(url: str) -> dict:
    """Разбирает mysql://user:pass@host:port/dbname в kwargs для aiomysql."""
    p = urlparse(url)
    if p.scheme not in ("mysql", "mysql+aiomysql", "mariadb"):
        raise ValueError(f"Ожидался mysql://..., получено: {p.scheme}")
    return {
        "host": p.hostname,
        "port": p.port or 3306,
        "user": unquote(p.username or ""),
        "password": unquote(p.password or ""),
        "db": (p.path or "/").lstrip("/"),
    }


async def init_db():
    global db_pool
    if not DATABASE_URL:
        logging.warning("DATABASE_URL не задан — БД отключена")
        return
    try:
        cfg = parse_db_url(DATABASE_URL)
        db_pool = await aiomysql.create_pool(
            **cfg,
            minsize=1,
            maxsize=5,
            autocommit=True,
            charset="utf8mb4",
        )
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                for stmt in SCHEMA_STATEMENTS:
                    await cur.execute(stmt)
        logging.info("MySQL подключён, схема готова")
    except Exception:
        logging.exception("Не удалось подключиться к MySQL — работаем без сохранения")
        db_pool = None


async def db_execute(query: str, *args):
    if db_pool is None:
        return None
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, args)
                return cur.lastrowid
    except Exception:
        logging.exception("Ошибка выполнения запроса MySQL")
        return None


async def upsert_user(user) -> None:
    if user is None or db_pool is None:
        return
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO users (user_id, username, first_name, last_name, language_code, is_bot)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        username      = VALUES(username),
                        first_name    = VALUES(first_name),
                        last_name     = VALUES(last_name),
                        language_code = VALUES(language_code),
                        last_seen_at  = CURRENT_TIMESTAMP
                    """,
                    (
                        user.id,
                        user.username,
                        user.first_name,
                        user.last_name,
                        user.language_code,
                        1 if user.is_bot else 0,
                    ),
                )
    except Exception:
        logging.exception("Ошибка upsert_user")


async def save_message(user_id: int, chat_id: int, role: str, content: str, model: str | None = None):
    if db_pool is None:
        return
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO messages (user_id, chat_id, role, content, model, length)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (user_id, chat_id, role, content, model, len(content or "")),
                )
                if role == "user":
                    await cur.execute(
                        "UPDATE users SET messages_count = messages_count + 1 WHERE user_id = %s",
                        (user_id,),
                    )
    except Exception:
        logging.exception("Ошибка save_message")


async def log_event(user_id, chat_id, kind: str, payload: dict | None = None):
    if db_pool is None:
        return
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO events (user_id, chat_id, kind, payload) VALUES (%s, %s, %s, %s)",
                    (user_id, chat_id, kind, json.dumps(payload, ensure_ascii=False) if payload else None),
                )
    except Exception:
        logging.exception("Ошибка log_event")


async def log_error(user_id, chat_id, message: str):
    if db_pool is None:
        return
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO errors (user_id, chat_id, message) VALUES (%s, %s, %s)",
                    (user_id, chat_id, message[:2000]),
                )
    except Exception:
        logging.exception("Ошибка log_error")


# ---------- Форматирование ----------

def format_inline(text):
    parts = re.split(r"`([^`\n]+)`", text)
    result = []
    for i, part in enumerate(parts):
        if i % 2:
            result.append(f"<code>{html.escape(part)}</code>")
            continue
        part = html.escape(part, quote=False)
        part = re.sub(r"^\s*[-*_]{3,}\s*$", "──────────", part, flags=re.M)
        part = re.sub(r"^#{1,6}\s*(.+)$", lambda m: f"<b>{m.group(1).replace('**', '')}</b>", part, flags=re.M)
        part = re.sub(r"^(\s*)[-*]\s+", r"\1• ", part, flags=re.M)
        part = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", part)
        part = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", part)
        part = re.sub(r"\[([^\]]+)\]\((https?://[^)\s\"]+)\)", r'<a href="\2">\1</a>', part)
        result.append(part)
    return "".join(result)


def to_html(text):
    parts = re.split(r"```[\w+-]*\n?(.*?)```", text, flags=re.S)
    result = []
    for i, part in enumerate(parts):
        if i % 2:
            result.append(f"<pre>{html.escape(part.rstrip())}</pre>")
        else:
            result.append(format_inline(part))
    return "".join(result).strip()


def split_text(text):
    chunks, current = [], ""
    for block in text.split("\n\n"):
        if current and len(current) + len(block) + 2 > CHUNK_SIZE:
            chunks.append(current)
            current = ""
        while len(block) > CHUNK_SIZE:
            chunks.append(block[:CHUNK_SIZE])
            block = block[CHUNK_SIZE:]
        current = f"{current}\n\n{block}" if current else block
    if current.strip():
        chunks.append(current)
    return chunks or ["…"]


async def send_formatted(message, text, edit=False):
    send = message.edit_text if edit else message.reply_text
    try:
        await send(to_html(text), parse_mode=ParseMode.HTML, link_preview_options=no_preview)
    except BadRequest:
        await send(text, link_preview_options=no_preview)


# ---------- ИИ ----------

async def get_ai_response(chat_id, user_id, text):
    history[chat_id].append({"role": "user", "content": text})
    await save_message(user_id, chat_id, "user", text)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history[chat_id]]
    try:
        response = await client.chat.completions.create(model=MODEL, messages=messages)
        answer = response.choices[0].message.content or "Ответ получился пустым, попробуй переформулировать."
        history[chat_id].append({"role": "assistant", "content": answer})
        await save_message(user_id, chat_id, "assistant", answer, model=MODEL)
        return answer
    except Exception as e:
        logging.exception("Ошибка ИИ")
        history[chat_id].pop()
        err = str(e)[:300]
        await log_error(user_id, chat_id, err)
        return f"⚠️ **Не получилось получить ответ.** Попробуй ещё раз чуть позже.\n\n{err}"


# ---------- Хендлеры ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await upsert_user(user)
    await log_event(user.id, update.effective_chat.id, "start")
    name = html.escape(user.first_name or "друг")
    await update.message.reply_text(WELCOME.format(name=name), parse_mode=ParseMode.HTML, reply_markup=MENU)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await upsert_user(user)
    await log_event(user.id, update.effective_chat.id, "help")
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML, reply_markup=MENU)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await upsert_user(user)
    history[update.effective_chat.id].clear()
    await log_event(user.id, update.effective_chat.id, "reset")
    await update.message.reply_text(RESET_TEXT, parse_mode=ParseMode.HTML)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    await upsert_user(user)
    await log_event(user.id, query.message.chat.id, f"button:{query.data}")

    if query.data == "reset":
        history[query.message.chat.id].clear()
        await query.message.reply_text(RESET_TEXT, parse_mode=ParseMode.HTML)
    elif query.data == "help":
        await query.message.reply_text(HELP, parse_mode=ParseMode.HTML, reply_markup=MENU)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await upsert_user(user)

    placeholder = await update.message.reply_text(THINKING_TEXT, parse_mode=ParseMode.HTML)
    await update.message.chat.send_action(ChatAction.TYPING)

    answer = await get_ai_response(update.effective_chat.id, user.id, update.message.text)
    chunks = split_text(answer)
    await send_formatted(placeholder, chunks[0], edit=True)
    for chunk in chunks[1:]:
        await send_formatted(update.message, chunk)


async def post_init(app: Application):
    await init_db()
    await app.bot.set_my_commands([
        BotCommand("start", "Начать"),
        BotCommand("reset", "Новый диалог"),
        BotCommand("help", "Помощь"),
    ])


async def post_shutdown(app: Application):
    global db_pool
    if db_pool is not None:
        db_pool.close()
        await db_pool.wait_closed()
        db_pool = None


def main():
    if not TELEGRAM_BOT_TOKEN or not AI_API_KEY:
        raise SystemExit("Не заданы TELEGRAM_BOT_TOKEN и/или AI_API_KEY")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
