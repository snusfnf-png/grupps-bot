const { Bot, InlineKeyboard } = require("grammy");
const http = require("http");

const bot = new Bot(process.env.BOT_TOKEN);

bot.command("start", async (ctx) => {
    const text = "Scrolling plates - генератор номерных знаков\n\n• Получай крутые ежедневные награды в течение недели\n• Крути н/з своей страны со всеми регионами\n• Доступны страны: Россия, Украина, Беларусь, Казахстан\n• Украшай номерные знаки разными модификаторами и рамками\n• Создавай комнату и играй с друзьями в разные режимы\n• Меняй настройки игры под себя, выбери свою удобную тему\n• Продавай свои номера игрокам на маркетплейсе\n\nПрисоединяйся, вводи свой регион и крути номера👇";

    const keyboard = new InlineKeyboard()
        .webApp("Играть", "https://snusfnf-png.github.io/cardrop/")
        .row()
        .url("Наш чат", "https://t.me/chatcarzdrop")
        .url("Наш канал", "https://t.me/carzdrop");

    await ctx.reply(text, {
        reply_markup: keyboard,
    });
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
