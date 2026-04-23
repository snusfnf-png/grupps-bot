import io
import os
import random
import string

from PIL import Image, ImageDraw, ImageFont
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

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

# ── Шрифты ────────────────────────────────────────────────────────────────────

def _find_or_download_fonts():
    import urllib.request
    candidates_bold = [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
    ]
    candidates_reg = [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
    ]
    bold = next((p for p in candidates_bold if os.path.exists(p)), None)
    reg  = next((p for p in candidates_reg  if os.path.exists(p)), None)
    if bold and reg:
        return bold, reg

    font_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
    os.makedirs(font_dir, exist_ok=True)
    dl_bold = os.path.join(font_dir, "Roboto-Bold.ttf")
    dl_reg  = os.path.join(font_dir, "Roboto-Regular.ttf")
    for path, url in [
        (dl_bold, "https://github.com/googlefonts/roboto/raw/main/src/hinted/Roboto-Bold.ttf"),
        (dl_reg,  "https://github.com/googlefonts/roboto/raw/main/src/hinted/Roboto-Regular.ttf"),
    ]:
        if not os.path.exists(path):
            try:
                urllib.request.urlretrieve(url, path)
            except Exception as e:
                print(f"Не удалось скачать шрифт: {e}")
    return (bold or dl_bold), (reg or dl_reg)

FONT_BOLD, FONT_REG = _find_or_download_fonts()

# ── Регионы России ────────────────────────────────────────────────────────────

RU_REGIONS = [
    "01","02","03","04","05","06","07","08","09","10",
    "11","12","13","14","15","16","17","18","19","21",
    "22","23","24","25","26","27","28","29","30","31",
    "32","33","34","35","36","37","38","39","40","41",
    "42","43","44","45","46","47","48","49","50","51",
    "52","53","54","55","56","57","58","59","60","61",
    "62","63","64","65","66","67","68","69","70","71",
    "72","73","74","75","76","77","78","79","82","83",
    "86","89","90","93","95","96","97","98","99",
]

RU_LETTERS = "АВЕКМНОРСТУХ"

def _random_ru_plate():
    """Генерирует случайный номер РФ: Л ЦЦЦ ЛЛ"""
    L = RU_LETTERS
    letter1  = random.choice(L)
    digits   = "".join(random.choices("0123456789", k=3))
    letter23 = "".join(random.choices(L, k=2))
    chars    = f"{letter1} {digits} {letter23}"
    region   = random.choice(RU_REGIONS)
    return chars, region

# ── Генерация изображения (Россия) ────────────────────────────────────────────

def _dot_grid(draw, w, h):
    for x in range(0, w + 1, 20):
        for y in range(0, h + 1, 20):
            draw.ellipse([x-1, y-1, x+1, y+1], fill="#d0d0d0")

def _ru_flag(draw, fx, fy, fw=32, fh=22):
    th = fh // 3
    draw.rectangle([fx, fy,           fx+fw, fy+th],     fill="white", outline="#cccccc", width=1)
    draw.rectangle([fx, fy+th,        fx+fw, fy+th*2],   fill="#003DA5")
    draw.rectangle([fx, fy+th*2,      fx+fw, fy+fh],     fill="#CC0000")

def generate_ru_plate_image(chars: str, region: str) -> bytes:
    W, H = 580, 290
    img  = Image.new("RGB", (W, H), "#efefef")
    draw = ImageDraw.Draw(img)
    _dot_grid(draw, W, H)

    fnt_hdr = ImageFont.truetype(FONT_REG, 13)
    fnt_sub = ImageFont.truetype(FONT_REG, 11)
    fnt_ftr = ImageFont.truetype(FONT_REG, 12)
    draw.text((W//2, 15), "НОМЕРА  —  CARDROP",  fill="#aaaaaa", font=fnt_hdr, anchor="mm")
    draw.text((W//2, 29), "@cardrop_game_bot",   fill="#aaaaaa", font=fnt_sub, anchor="mm")

    cx, cy = W//2, H//2
    pw, ph = 490, 118
    px, py = cx - pw//2, cy - ph//2

    draw.rounded_rectangle([px+5, py+5, px+pw+5, py+ph+5], radius=10, fill="#b8b8b8")
    draw.rounded_rectangle([px, py, px+pw, py+ph], radius=10, fill="white",
                            outline="#111111", width=4)

    right_w = 110
    rdx = px + pw - right_w
    draw.line([(rdx, py+6), (rdx, py+ph-6)], fill="#111111", width=3)

    fnt_pl = ImageFont.truetype(FONT_BOLD, 72)
    draw.text((px + (pw - right_w)//2, py + ph//2), chars,
              fill="#111111", font=fnt_pl, anchor="mm")

    rcx = rdx + right_w//2
    rpt = py + 6
    rpb = py + ph - 6
    rph = rpb - rpt

    region_cy = rpt + int(rph * 0.42)
    fnt_r = ImageFont.truetype(FONT_BOLD, 48)
    draw.text((rcx, region_cy), region, fill="#111111", font=fnt_r, anchor="mm")

    fw, fh = 22, 15
    fnt_rus = ImageFont.truetype(FONT_BOLD, 13)
    rus_w = int(fnt_rus.getlength("RUS"))
    gap = 3
    total_w = rus_w + gap + fw
    rus_cy = rpt + int(rph * 0.82)
    tx = rcx - total_w//2
    fx = tx + rus_w + gap
    fy = rus_cy - fh//2
    draw.text((tx, rus_cy), "RUS", fill="#111111", font=fnt_rus, anchor="lm")
    _ru_flag(draw, fx, fy, fw=fw, fh=fh)

    draw.text((W//2, H-18), "@cardrop_game_bot", fill="#aaaaaa", font=fnt_ftr, anchor="mm")

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()

# ── Хендлеры ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Играть", web_app={"url": "https://snusfnf-png.github.io/cardrop/"})],
        [
            InlineKeyboardButton("Наш чат",   url="https://t.me/chatcarzdrop"),
            InlineKeyboardButton("Наш канал", url="https://t.me/carzdrop"),
        ],
    ]
    await update.message.reply_text(
        WELCOME_TEXT,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_nz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """В любом чате при сообщении 'нз' отправляет случайный номер России."""
    msg = update.message
    if not msg or not msg.text:
        return
    if msg.text.strip().lower() != "нз":
        return

    chars, region = _random_ru_plate()
    img_bytes = generate_ru_plate_image(chars, region)
    buf = io.BytesIO(img_bytes)
    buf.name = "plate.png"
    await msg.reply_photo(photo=buf)

async def post_init(application: Application):
    await application.bot.delete_my_commands()

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_nz))
    print("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
    
