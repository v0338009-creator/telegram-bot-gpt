import os
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from openai import OpenAI

# 1. Получаем переменные окружения (секреты из GitHub)
YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# 2. Инициализация клиента YandexGPT через OpenAI-совместимый интерфейс
client = OpenAI(
    base_url="https://llm.api.cloud.yandex.net/foundation_models/v1",
    api_key=YANDEX_API_KEY
)

# Формируем правильный URI модели с использованием Folder ID
MODEL_URI = f"gpt://{YANDEX_FOLDER_ID}/yandexgpt-lite"

# 3. Функция запроса к ИИ
async def get_ai_response(text: str) -> str:
    try:
        response = client.chat.completions.create(
            model=MODEL_URI, # Используем полный URI
            messages=[{"role": "user", "content": text}],
            temperature=0.6
        )
        return response.choices[0].message.content
    except Exception as e:
        # Если произошла ошибка, возвращаем её текст, чтобы увидеть в Telegram
        return f"❌ Ошибка ИИ:\n{str(e)}"

# 4. Обработчик сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    
    # Показываем статус "печатает..."
    await update.message.chat.send_action(action="typing")
    
    # Получаем ответ и отправляем
    ai_answer = await get_ai_response(user_text)
    await update.message.reply_text(ai_answer)

# 5. Запуск бота
def main():
    if not TELEGRAM_BOT_TOKEN:
        print("Ошибка: не найден TELEGRAM_BOT_TOKEN")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🤖 Бот запущен и слушает сообщения...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
