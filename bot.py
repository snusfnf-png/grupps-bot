import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ.get("BOT_TOKEN")

WELCOME_TEXT = """Scrolling plates - генератор номерных знаков

• Получай крутые ежедневные награды в течение недели
• Крути н/з своей страны со всеми регионами
• Доступны страны: Россия, Украина, Беларусь, Казахстан
• Украшай номерные знаки разными модификаторами и рамками
• Создавай комнату и играй с друзьями в разные режимы
• Меняй настройки игры под себя, выбери свою удобную тему
• Продавай свои номера игрокам на маркетплейсе

Присоединяйся, вводи свой регион и крути номера👇"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Убираем Reply-клавиатуру (кнопки внизу вместо клавиатуры)
    await update.message.reply_text(
        "Загрузка...",
        reply_markup=ReplyKeyboardRemove()
    )

    keyboard = [
        [InlineKeyboardButton("Играть", web_app={"url": "https://snusfnf-png.github.io/cardrop/"})],
        [
            InlineKeyboardButton("Наш чат", url="https://t.me/chatcarzdrop"),
            InlineKeyboardButton("Наш канал", url="https://t.me/carzdrop"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        WELCOME_TEXT,
        reply_markup=reply_markup
    )

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    print("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
  
