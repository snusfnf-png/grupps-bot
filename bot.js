юconst { Bot, InlineKeyboard } = require("grammy");

// Токен берем из переменных окружения Railway
const bot = new Bot(process.env.BOT_TOKEN);

bot.command("start", async (ctx) => {
    const text = `Scrolling plates - генератор номерных знаков

• Получай крутые ежедневные награды в течение недели
• Крути н/з своей страны со всеми регионами
• Доступны страны: Россия, Украина, Беларусь, Казахстан
• Украшай номерные знаки разными модификаторами и рамками
• Создавай комнату и играй с друзьями в разные режимы
• Меняй настройки игры под себя, выбери свою удобную тему
• Продавай свои номера игрокам на маркетплейсе

Присоединяйся, вводи свой регион и крути номера👇`;
}

    // Создаем кнопку с ссылкой на Mini App
    const keyboard = new InlineKeyboard().url("Играть 🎮", "https://snusfnf-png.github.io/cardrop/");

    await ctx.reply(text, {
        reply_markup: keyboard,
        // Опционально: можно отключить превью ссылки, чтобы было чище
        // link_preview_options: { is_disabled: true } 
    });
});

// Запуск бота (Railway сам даст порт для вебхука, но для простоты запустим long polling)
bot.start({
    onStart: () => console.log("Бот запущен!"),
});
