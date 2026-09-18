import os
import re
import html
import logging
from collections import defaultdict, deque

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


async def get_ai_response(chat_id, text):
    history[chat_id].append({"role": "user", "content": text})
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history[chat_id]]
    try:
        response = await client.chat.completions.create(model=MODEL, messages=messages)
        answer = response.choices[0].message.content or "Ответ получился пустым, попробуй переформулировать."
        history[chat_id].append({"role": "assistant", "content": answer})
        return answer
    except Exception as e:
        logging.exception("Ошибка ИИ")
        history[chat_id].pop()
        return f"⚠️ **Не получилось получить ответ.** Попробуй ещё раз чуть позже.\n\n{str(e)[:300]}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = html.escape(update.effective_user.first_name or "друг")
    await update.message.reply_text(WELCOME.format(name=name), parse_mode=ParseMode.HTML, reply_markup=MENU)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML, reply_markup=MENU)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history[update.effective_chat.id].clear()
    await update.message.reply_text(RESET_TEXT, parse_mode=ParseMode.HTML)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "reset":
        history[query.message.chat.id].clear()
        await query.message.reply_text(RESET_TEXT, parse_mode=ParseMode.HTML)
    elif query.data == "help":
        await query.message.reply_text(HELP, parse_mode=ParseMode.HTML, reply_markup=MENU)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    placeholder = await update.message.reply_text(THINKING_TEXT, parse_mode=ParseMode.HTML)
    await update.message.chat.send_action(ChatAction.TYPING)
    answer = await get_ai_response(update.effective_chat.id, update.message.text)
    chunks = split_text(answer)
    await send_formatted(placeholder, chunks[0], edit=True)
    for chunk in chunks[1:]:
        await send_formatted(update.message, chunk)


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Начать"),
        BotCommand("reset", "Новый диалог"),
        BotCommand("help", "Помощь"),
    ])


def main():
    if not TELEGRAM_BOT_TOKEN or not AI_API_KEY:
        raise SystemExit("Не заданы TELEGRAM_BOT_TOKEN и/или AI_API_KEY")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
