import os
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from openai import OpenAI

# 1. Инициализация клиента YandexGPT через совместимый OpenAI интерфейс
client = OpenAI(
    base_url="https://llm.api.cloud.yandex.net/foundation_models/v1",
    api_key=os.environ.get("YANDEX_API_KEY")  # Ключ берется из безопасного хранилища GitHub
)

# 2. Функция, которая отправляет текст в ИИ и получает ответ
async def get_ai_response(text: str) -> str:
    try:
        response = client.chat.completions.create(
            model="yandexgpt-lite", # Легкая и быстрая модель Яндекса
            messages=[{"role": "user", "content": text}]
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Ошибка ИИ: {e}"

# 3. Обработчик любых текстовых сообщений от пользователя
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    
    # Показываем статус "печатает..." в Telegram
    await update.message.chat.send_action(action="typing")
    
    # Запрашиваем ответ у ИИ и отправляем его пользователю
    ai_answer = await get_ai_response(user_text)
    await update.message.reply_text(ai_answer)

# 4. Главная функция запуска
def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN") # Ключ берется из безопасного хранилища GitHub
    
    # Создаем приложение бота
    app = Application.builder().token(token).build()
    
    # Говорим боту: "На любое сообщение без команд, запускай функцию handle_message"
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🤖 Бот запущен и слушает сообщения...")
    
    # Запускаем режим Polling (постоянный опрос серверов Telegram)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
