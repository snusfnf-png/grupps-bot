const { Bot, InlineKeyboard } = require("grammy");
const http = require("http");

const bot = new Bot(process.env.BOT_TOKEN);

bot.command("start", async (ctx) => {
    const text = "Scrolling plates - генератор номерных знаков\n\n• Получай крутые ежедневные награды в течение недели\n• Крути н/з своей страны со всеми регионами\n• Доступны страны: Россия, Украина, Беларусь, Казахстан\n• Украшай номерные знаки разными модификаторами и рамками\n• Создавай комнату и играй с друзьями в разные режимы\n• Меняй настройки игры под себя, выбери свою удобную тему\n• Продавай свои номера игрокам на маркетплейсе\n\nПрисоединяйся, вводи свой регион и крути номера👇";

    // Inline-кнопка с премиум эмодзи (под сообщением)
    const playButton = new InlineKeyboard().webApp(
        "Играть", 
        "https://snusfnf-png.github.io/cardrop/"
    );

    await ctx.reply(text, {
        reply_markup: playButton,
    });

    // Клавиатура в поле ввода с премиум эмодзи
    await ctx.reply("Выберите действие:", {
        reply_markup: {
            keyboard: [
                [
                    {
                        text: "Играть 🎮"
                    }
                ],
                [
                    {
                        text: "Наш чат 👥"
                    },
                    {
                        text: "Наш канал 📢"
                    }
                ]
            ],
            resize_keyboard: true,
            persistent: true
        }
    });
});

// Обработчик текстовых сообщений для кнопок в поле ввода
bot.on("message:text", async (ctx) => {
    const msg = ctx.message.text;
    
    if (msg.includes("Играть")) {
        await ctx.reply("Запускайте Mini App по кнопке выше 👆");
    } else if (msg.includes("Наш чат")) {
        await ctx.reply("Присоединяйтесь в наш чат: https://t.me/chatcarzdrop");
    } else if (msg.includes("Наш канал")) {
        await ctx.reply("Подписывайтесь на наш канал: https://t.me/carzdrop");
    }
});

const server = http.createServer((req, res) => {
    res.writeHead(200);
    res.end("Bot is running");
});

const PORT = process.env.PORT || 3000;
server.listen(PORT, () => {
    console.log("Server running on port " + PORT);
});

bot.start({
    onStart: () => console.log("Bot started!"),
});
