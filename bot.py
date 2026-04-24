import io
import logging
import os
import json
import random
import urllib.request
from datetime import datetime, timedelta
from typing import Dict

from PIL import Image, ImageDraw, ImageFont
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
    ConversationHandler
)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")  # postgresql://... из Render

# ── PostgreSQL ─────────────────────────────────────────────────────────────────
import psycopg2
from psycopg2.extras import RealDictCursor

def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    """Создаёт таблицу если её нет."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    coins INTEGER DEFAULT 0,
                    last_spin TIMESTAMP
                )
            """)
        conn.commit()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling update:", exc_info=context.error)

# Глобальная настройка страны (одна для всех групп)
global_country: str = "RU"

# ── Данные игроков (user_id -> данные) ────────────────────────────────────────
# { user_id: { "coins": int, "last_spin": datetime | None } }

COOLDOWN_HOURS = 3  # КД между крутками
ADMIN_USERNAME = "tntks"  # Единственный администратор

def get_user(user_id: int) -> dict:
    """Получает данные пользователя из PostgreSQL."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT coins, last_spin FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
    if row is None:
        return {"coins": 0, "last_spin": None}
    return {"coins": row["coins"], "last_spin": row["last_spin"]}

def save_user(user_id: int, coins: int, last_spin):
    """Сохраняет данные пользователя в PostgreSQL."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, coins, last_spin)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET coins = EXCLUDED.coins, last_spin = EXCLUDED.last_spin
            """, (user_id, coins, last_spin))
        conn.commit()

def get_all_user_ids() -> list[int]:
    """Возвращает список всех user_id из БД."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users")
            rows = cur.fetchall()
    return [row["user_id"] for row in rows]

def try_spin(user_id: int) -> bool:
    """
    Атомарно проверяет КД и выставляет last_spin.
    Возвращает True если прокрутка разрешена, False если КД ещё идёт.
    """
    from datetime import timezone
    now = datetime.now(timezone.utc)
    cooldown_ago = now - timedelta(hours=COOLDOWN_HOURS)
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Вставляем нового пользователя или обновляем если КД прошёл
            cur.execute("""
                INSERT INTO users (user_id, coins, last_spin)
                VALUES (%s, 0, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET last_spin = %s
                WHERE users.last_spin IS NULL OR users.last_spin <= %s
                RETURNING user_id
            """, (user_id, now, now, cooldown_ago))
            result = cur.fetchone()
        conn.commit()
    return result is not None

def get_cooldown_remaining(user_id: int):
    """Возвращает timedelta до конца КД или None если КД прошёл."""
    from datetime import timezone
    user = get_user(user_id)
    if user["last_spin"] is None:
        return None
    last_spin = user["last_spin"]
    # Приводим к UTC если нет timezone
    if last_spin.tzinfo is None:
        last_spin = last_spin.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    elapsed = now - last_spin
    cooldown = timedelta(hours=COOLDOWN_HOURS)
    if elapsed >= cooldown:
        return None
    return cooldown - elapsed

def format_cooldown(td: timedelta) -> str:
    total = int(td.total_seconds())
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h}ч {m}мин"
    if m > 0:
        return f"{m}мин {s}сек"
    return f"{s}сек"

def calc_coins(chars: str, country: str) -> tuple[int, str]:
    """
    Считает монеты и возвращает (кол-во монет, название редкости).
    Работает по цифрам из номера.
    """
    # Извлекаем только цифры
    digits = [c for c in chars if c.isdigit()]

    if not digits:
        return 5, "Обычный"

    # Легендарный: все цифры одинаковые И их 3+
    if len(digits) >= 3 and len(set(digits)) == 1:
        return 100, "🏆 Легендарный"

    # Четвёрка: четыре одинаковых цифры подряд среди digits
    digit_str = "".join(digits)
    has_quad = any(digit_str[i] == digit_str[i+1] == digit_str[i+2] == digit_str[i+3]
                   for i in range(len(digit_str) - 3)) if len(digit_str) >= 4 else False
    if has_quad:
        return 40, "🔥 Четвёрка"

    # Тройник: три одинаковых цифры подряд
    has_triple = any(digit_str[i] == digit_str[i+1] == digit_str[i+2]
                     for i in range(len(digit_str) - 2)) if len(digit_str) >= 3 else False
    if has_triple:
        return 25, "✨ Тройник"

    # Зеркальный: цифры симметричны (121, 1221 и т.д.)
    if len(digits) >= 3 and digits == digits[::-1]:
        return 15, "🪞 Зеркальный"

    # Красивый: цифры идут подряд по возрастанию или убыванию (123, 321, 456...)
    asc  = all(int(digits[i+1]) == int(digits[i]) + 1 for i in range(len(digits)-1))
    desc = all(int(digits[i+1]) == int(digits[i]) - 1 for i in range(len(digits)-1))
    if (asc or desc) and len(digits) >= 3:
        return 10, "⭐ Красивый"

    return 5, "Обычный"

RARITY_MESSAGES = {
    "Обычный":        "Обычный номер. +5 ⚡",
    "⭐ Красивый":    "Цифры идут по порядку — красиво. +10 ⚡",
    "🪞 Зеркальный":  "Симметричный номер, одинаково с обоих сторон. +15 ⚡",
    "✨ Тройник":     "Три одинаковых подряд — редкость. +25 ⚡",
    "🔥 Четвёрка":    "Четыре одинаковых цифры. Серьёзный номер. +40 ⚡",
    "🏆 Легендарный": "Все цифры одинаковые. Такое почти не выпадает. +100 ⚡",
}

WELCOME_TEXT = """Scrolling plates - генератор номерных знаков

• Получай крутые ежедневные награды в течение недели
• Крути н/з своей страны со всеми регионами
• Доступны страны: Россия, Украина, Беларусь, Казахстан
• Украшай номерные знаки разными модификаторами и рамками
• Создавай комнату и играй с друзьями в разные режимы
• Меняй настройки игры под себя, выбери свою удобную тему
• Продавай свои номера игрокам на маркетплейсе

Присоединяйся, вводи свой регион и крути номера👇"""

ADD_TO_CHAT_TEXT = """Это генератор номерных знаков

Напишите нз — бот пришлёт случайный номер выбранной страны. Прокрутка доступна каждые 3ч

/info - подробная информация
/settings - настройки номера"""

INFO_TEXT = """<blockquote>⚠️ Чтобы бот присылал номер нужно дать доступ к сообщениям!</blockquote>
Напишите нз — бот пришлёт случайный номер выбранной страны. Прокрутка доступна каждые 3ч

Доступные страны:
🇷🇺 Россия — формат: А 000 АА [000]
🇺🇦 Украина — формат: [АА] 0000 АА
🇧🇾 Беларусь — формат: 0000 АА-[0]
🇰🇿 Казахстан — формат: 000 AAA [00]

🔗 Полезные ссылки:
• <a href="https://t.me/chatcarzdrop">наш чат</a>
• <a href="https://t.me/carzdrop">наш канал</a>
• <a href="https://t.me/tntks">поддержка</a>"""

# ── Шрифты ────────────────────────────────────────────────────────────────────

def _find_or_download_fonts():
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
        print(f"Системные шрифты: {bold}, {reg}")
        return bold, reg

    # Сохраняем в /tmp — всегда доступна на любом хостинге
    font_dir = "/tmp/bot_fonts"
    os.makedirs(font_dir, exist_ok=True)
    dl_bold = os.path.join(font_dir, "Roboto-Bold.ttf")
    dl_reg  = os.path.join(font_dir, "Roboto-Regular.ttf")

    bold_urls = [
        "https://github.com/google/fonts/raw/main/apache/roboto/static/Roboto-Bold.ttf",
        "https://github.com/googlefonts/roboto/raw/v2.138/src/hinted/Roboto-Bold.ttf",
    ]
    reg_urls = [
        "https://github.com/google/fonts/raw/main/apache/roboto/static/Roboto-Regular.ttf",
        "https://github.com/googlefonts/roboto/raw/v2.138/src/hinted/Roboto-Regular.ttf",
    ]

    for dest, urls in [(dl_bold, bold_urls), (dl_reg, reg_urls)]:
        if not (os.path.exists(dest) and os.path.getsize(dest) > 10000):
            for url in urls:
                try:
                    urllib.request.urlretrieve(url, dest)
                    if os.path.getsize(dest) > 10000:
                        print(f"Шрифт скачан: {dest}")
                        break
                except Exception as e:
                    print(f"Не удалось скачать шрифт {url}: {e}")

    bold_ok = os.path.exists(dl_bold) and os.path.getsize(dl_bold) > 10000
    reg_ok  = os.path.exists(dl_reg)  and os.path.getsize(dl_reg)  > 10000
    print(f"Шрифты готовы: bold={bold_ok}, reg={reg_ok}")
    return (dl_bold if bold_ok else None), (dl_reg if reg_ok else None)


FONT_BOLD, FONT_REG = _find_or_download_fonts()

def _font(path, size: int):
    """Безопасная загрузка шрифта. Если TrueType недоступен — пробуем скачать заново."""
    if path and os.path.exists(path):
        try:
            return ImageFont.truetype(path, size)
        except Exception as e:
            print(f"[FONT] Ошибка загрузки {path}: {e}")
    # Попытка скачать шрифты прямо сейчас
    global FONT_BOLD, FONT_REG
    FONT_BOLD, FONT_REG = _find_or_download_fonts()
    target = FONT_BOLD if path == FONT_BOLD else FONT_REG
    if target and os.path.exists(target):
        try:
            return ImageFont.truetype(target, size)
        except Exception:
            pass
    # Последний fallback — load_default с size (Pillow >= 10.1)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()

# ── Флаг Казахстана ───────────────────────────────────────────────────────────

def _find_or_download_kz_flag():
    font_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
    os.makedirs(font_dir, exist_ok=True)
    flag_path = os.path.join(font_dir, "kz_flag.png")
    if os.path.exists(flag_path) and os.path.getsize(flag_path) > 1000:
        return flag_path
    for url in [
        "https://flagcdn.com/w160/kz.png",
        "https://raw.githubusercontent.com/hampusborgos/country-flags/main/png250px/kz.png",
    ]:
        try:
            urllib.request.urlretrieve(url, flag_path)
            return flag_path
        except Exception:
            pass
    return None

KZ_FLAG_PATH = _find_or_download_kz_flag()

# ── Данные регионов ───────────────────────────────────────────────────────────

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

UA_REGIONS = ["AA","AI","BC","AC","AO","AT","AM","BK","BO","BX",
              "CE","BA","BI","CA","CB","BM","AB","AX","AE","AH",
              "BB","BH","BT","ВА"]
UA_LETTERS = "АВЕIКМНОРСТХ"

BY_REGIONS = ["1","2","3","4","5","6","7"]
BY_LETTERS = "АВЕIКМНОРСТХ"

KZ_REGIONS = ["01","02","03","04","05","06","07","08","09","10",
              "11","12","13","14","15","16","17","18","19","20"]
KZ_LAT = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

def _random_ru_plate():
    L = RU_LETTERS
    chars = f"{random.choice(L)} {''.join(random.choices('0123456789', k=3))} {''.join(random.choices(L, k=2))}"
    return chars, random.choice(RU_REGIONS)

def _random_ua_plate():
    region = random.choice(UA_REGIONS)
    digits = "".join(random.choices("0123456789", k=4))
    letters = "".join(random.choices(UA_LETTERS, k=2))
    chars = f"{digits}{letters}"
    return chars, region

def _random_by_plate():
    region = random.choice(BY_REGIONS)
    digits = "".join(random.choices("0123456789", k=4))
    letters = "".join(random.choices(BY_LETTERS, k=2))
    chars = f"{digits}{letters}"
    return chars, region

def _random_kz_plate():
    region = random.choice(KZ_REGIONS)
    digits = "".join(random.choices("0123456789", k=3))
    letters = "".join(random.choices(KZ_LAT, k=3))
    chars = f"{digits}{letters}"
    return chars, region

# ── Генерация изображений ─────────────────────────────────────────────────────

def _dot_grid(draw, w, h):
    for x in range(0, w + 1, 20):
        for y in range(0, h + 1, 20):
            draw.ellipse([x-1, y-1, x+1, y+1], fill="#d0d0d0")

def _ru_flag(draw, fx, fy, fw=32, fh=22):
    th = fh // 3
    draw.rectangle([fx, fy,       fx+fw, fy+th],   fill="white", outline="#cccccc", width=1)
    draw.rectangle([fx, fy+th,    fx+fw, fy+th*2], fill="#003DA5")
    draw.rectangle([fx, fy+th*2,  fx+fw, fy+fh],   fill="#CC0000")

def _base_image():
    W, H = 580, 290
    img  = Image.new("RGB", (W, H), "#efefef")
    draw = ImageDraw.Draw(img)
    _dot_grid(draw, W, H)

    return img, draw, W, H

def _finish_image(img, draw, W, H):
    fnt_ftr = _font(FONT_REG, 12)
    draw.text((W//2, H-18), "@carzplate_bot", fill="#aaaaaa", font=fnt_ftr, anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()

def generate_plate_image(country: str, chars: str, region: str) -> bytes:
    img, draw, W, H = _base_image()
    cx, cy = W//2, H//2
    pw, ph = 490, 118
    px, py = cx - pw//2, cy - ph//2

    if country == "RU":
        draw.rounded_rectangle([px+5, py+5, px+pw+5, py+ph+5], radius=10, fill="#b8b8b8")
        draw.rounded_rectangle([px, py, px+pw, py+ph], radius=10, fill="white",
                                outline="#111111", width=4)
        right_w = 110
        rdx = px + pw - right_w
        draw.line([(rdx, py+6), (rdx, py+ph-6)], fill="#111111", width=3)
        fnt_pl = _font(FONT_BOLD, 72)
        draw.text((px + (pw - right_w)//2, cy), chars, fill="#111111", font=fnt_pl, anchor="mm")
        rcx = rdx + right_w//2
        rpt = py + 6; rpb = py + ph - 6; rph = rpb - rpt
        fnt_r = _font(FONT_BOLD, 48)
        draw.text((rcx, rpt + int(rph*0.42)), region, fill="#111111", font=fnt_r, anchor="mm")
        fw, fh = 22, 15
        fnt_rus = _font(FONT_BOLD, 13)
        rus_w = int(fnt_rus.getlength("RUS"))
        gap = 3; total_w = rus_w + gap + fw
        rus_cy = rpt + int(rph*0.82)
        tx = rcx - total_w//2; fx = tx + rus_w + gap; fy = rus_cy - fh//2
        draw.text((tx, rus_cy), "RUS", fill="#111111", font=fnt_rus, anchor="lm")
        _ru_flag(draw, fx, fy, fw=fw, fh=fh)

    elif country == "UA":
        draw.rounded_rectangle([px+4, py+4, px+pw+4, py+ph+4], radius=8, fill="#b0b0b0")
        draw.rounded_rectangle([px, py, px+pw, py+ph], radius=8, fill="white",
                                outline="#111111", width=4)
        strip_w = 62
        draw.rounded_rectangle([px+2, py+2, px+strip_w, py+ph-2], radius=7, fill="#003DA5")
        draw.line([(px+strip_w, py+4), (px+strip_w, py+ph-4)], fill="#111111", width=3)
        ffw, ffh = 38, 26
        ffx = px + (strip_w - ffw)//2; ffy = py + 16
        draw.rectangle([ffx, ffy,        ffx+ffw, ffy+ffh//2], fill="#005BBB")
        draw.rectangle([ffx, ffy+ffh//2, ffx+ffw, ffy+ffh],   fill="#FFD500")
        fnt_ua = _font(FONT_BOLD, 17)
        draw.text((px + strip_w//2, py + ph - 14), "UA", fill="white", font=fnt_ua, anchor="mm")
        c = chars.strip().upper().replace(" ", "")
        body = f"{region} {c[:4]} {c[4:]}" if len(c) >= 6 else f"{region} {c}"
        fnt_pl = _font(FONT_BOLD, 66)
        content_cx = px + strip_w + (pw - strip_w)//2
        draw.text((content_cx, cy), body, fill="#111111", font=fnt_pl, anchor="mm")

    elif country == "BY":
        draw.rounded_rectangle([px+4, py+4, px+pw+4, py+ph+4], radius=8, fill="#b0b0b0")
        draw.rounded_rectangle([px, py, px+pw, py+ph], radius=8, fill="white",
                                outline="#111111", width=5)
        zone_w = 90; fl_w = 72; fl_h = 48
        fnt_by2 = _font(FONT_BOLD, 16)
        by_bbox = fnt_by2.getbbox("BY")
        by_h = by_bbox[3] - by_bbox[1]
        total_h = fl_h + 4 + by_h
        fl_x = px + (zone_w - fl_w)//2
        fl_y = py + (ph - total_h)//2
        red_h = round(fl_h * 2/3)
        draw.rectangle([fl_x, fl_y,        fl_x+fl_w, fl_y+red_h], fill="#CF101A")
        draw.rectangle([fl_x, fl_y+red_h,  fl_x+fl_w, fl_y+fl_h],  fill="#007828")
        orn_w = max(7, fl_w//9)
        draw.rectangle([fl_x, fl_y, fl_x+orn_w, fl_y+fl_h], fill="white")
        step = max(5, orn_w+1); ocx = fl_x + orn_w//2
        for yi in range(fl_y, fl_y+fl_h, step):
            y_top=yi; y_mid=yi+step//2; y_bot=min(yi+step, fl_y+fl_h)
            col = "#CF101A" if y_mid < (fl_y+red_h) else "#007828"
            draw.polygon([(ocx,y_top),(fl_x+orn_w-1,y_mid),(ocx,y_bot),(fl_x+1,y_mid)], fill=col)
            if y_mid < fl_y+fl_h-2:
                mini=step//4
                draw.polygon([(ocx,y_mid-mini),(ocx+mini,y_mid),(ocx,y_mid+mini),(ocx-mini,y_mid)], fill="white")
        draw.text((fl_x+fl_w//2, fl_y+fl_h+4), "BY", fill="#111111", font=fnt_by2, anchor="mt")
        text_x = px + zone_w + 6
        c = chars.replace(" ", "").upper()
        body = f"{c[:4]} {c[4:6]}-{region}" if len(c) >= 6 else f"{chars}-{region}"
        fnt_pl = _font(FONT_BOLD, 68)
        content_cx = text_x + (px+pw-8-text_x)//2
        draw.text((content_cx, cy), body, fill="#111111", font=fnt_pl, anchor="mm")

    elif country == "KZ":
        draw.rounded_rectangle([px+4, py+6, px+pw+4, py+ph+6], radius=10, fill="#aaaaaa")
        draw.rounded_rectangle([px, py, px+pw, py+ph], radius=10, fill="white",
                                outline="#1a1a1a", width=5)
        SW = 90; RW = 64; col_cx = px + SW//2
        fnt_kz_label = _font(FONT_BOLD, 17)
        kz_bbox = fnt_kz_label.getbbox("KZ"); kz_h = kz_bbox[3]-kz_bbox[1]
        flag_drawn = False
        flag_bottom = cy
        if KZ_FLAG_PATH and os.path.exists(KZ_FLAG_PATH):
            try:
                flag_img = Image.open(KZ_FLAG_PATH).convert("RGBA")
                target_flag_h = 52; max_flag_w = SW-16
                ratio_h = target_flag_h/flag_img.height
                new_w = int(flag_img.width*ratio_h)
                if new_w > max_flag_w: ratio_h = max_flag_w/flag_img.width
                new_w = max(1, int(flag_img.width*ratio_h)); new_h = max(1, int(flag_img.height*ratio_h))
                resized = flag_img.resize((new_w, new_h), Image.LANCZOS)
                total_h = resized.height+3+kz_h
                flag_y = py+(ph-total_h)//2; flag_x = col_cx-resized.width//2
                img.paste(resized, (flag_x, flag_y), resized)
                flag_bottom = flag_y+resized.height; flag_drawn = True
            except Exception:
                pass
        if not flag_drawn:
            fl_w, fl_h = 56, 38; total_h = fl_h+3+kz_h
            flag_y = py+(ph-total_h)//2; fl_x = col_cx-fl_w//2
            draw.rectangle([fl_x, flag_y, fl_x+fl_w, flag_y+fl_h], fill="#00AFCA")
            flag_bottom = flag_y+fl_h
        draw.text((col_cx, flag_bottom+3), "KZ", fill="#111111", font=fnt_kz_label, anchor="mt")
        rdx = px+pw-RW
        draw.line([(rdx, py+10),(rdx, py+ph-10)], fill="#bbbbbb", width=2)
        fnt_reg = _font(FONT_BOLD, 40)
        draw.text((rdx+RW//2, cy), region, fill="#111111", font=fnt_reg, anchor="mm")
        c = chars.replace(" ", "")
        body = f"{c[:3]} {c[3:]}" if len(c) == 6 else chars
        num_cx = px+SW+(rdx-px-SW)//2
        fnt_pl = _font(FONT_BOLD, 72)
        draw.text((num_cx, cy+2), body, fill="#111111", font=fnt_pl, anchor="mm")

    return _finish_image(img, draw, W, H)

def make_random_plate(country: str) -> tuple[bytes, str]:
    """Возвращает (изображение, chars) чтобы не генерировать номер дважды."""
    if country == "RU":
        chars, region = _random_ru_plate()
    elif country == "UA":
        chars, region = _random_ua_plate()
    elif country == "BY":
        chars, region = _random_by_plate()
    elif country == "KZ":
        chars, region = _random_kz_plate()
    else:
        chars, region = _random_ru_plate()
    return generate_plate_image(country, chars, region), chars

# ── Хендлеры ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Играть", web_app={"url": "https://snusfnf-png.github.io/cardrop/"})],
        [
            InlineKeyboardButton("Наш чат",   url="https://t.me/chatcarzdrop"),
            InlineKeyboardButton("Наш канал", url="https://t.me/carzdrop"),
        ],
        [InlineKeyboardButton("➕ Добавить бота в чат", callback_data="add_to_chat")],
    ]
    await update.message.reply_text(
        WELCOME_TEXT,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        INFO_TEXT,
        parse_mode="HTML",
    )

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Только в личных сообщениях
    if update.effective_chat.type != "private":
        await update.message.reply_text("⚙️ Настройки доступны только в личных сообщениях с ботом.")
        return

    current = global_country
    flags = {"RU": "🇷🇺", "UA": "🇺🇦", "BY": "🇧🇾", "KZ": "🇰🇿"}
    names = {"RU": "Россия", "UA": "Украина", "BY": "Беларусь", "KZ": "Казахстан"}

    keyboard = []
    row = []
    for code in ["RU", "UA", "BY", "KZ"]:
        mark = " ✅" if code == current else ""
        row.append(InlineKeyboardButton(
            f"{flags[code]} {names[code]}{mark}",
            callback_data=f"set_country_{code}"
        ))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await update.message.reply_text(
        "🌍 Выбери страну номерного знака (применяется во всех группах):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def add_to_chat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot_username = (await context.bot.get_me()).username
    keyboard = [
        [InlineKeyboardButton("➕ Добавить бота",
                              url=f"https://t.me/{bot_username}?startgroup=start")],
    ]
    await query.message.reply_text(
        ADD_TO_CHAT_TEXT,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global global_country
    query = update.callback_query
    await query.answer()
    code = query.data.replace("set_country_", "")
    global_country = code

    flags = {"RU": "🇷🇺", "UA": "🇺🇦", "BY": "🇧🇾", "KZ": "🇰🇿"}
    names = {"RU": "Россия", "UA": "Украина", "BY": "Беларусь", "KZ": "Казахстан"}

    keyboard = []
    row = []
    for c in ["RU", "UA", "BY", "KZ"]:
        mark = " ✅" if c == code else ""
        row.append(InlineKeyboardButton(
            f"{flags[c]} {names[c]}{mark}",
            callback_data=f"set_country_{c}"
        ))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await query.edit_message_text(
        f"✅ Страна изменена на {flags[code]} {names[code]}!\n\n🌍 Выбери страну номерного знака (применяется во всех группах):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_nz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    if msg.text.strip().lower() != "нз":
        return

    user = msg.from_user
    if not user:
        return

    user_id = user.id

    # Проверяем КД (если КД идёт — сразу отвечаем)
    remaining = get_cooldown_remaining(user_id)
    if remaining is not None:
        await msg.reply_text(f'🙁 Следующая прокрутка будет доступна через {format_cooldown(remaining)}')
        return

    # Атомарно занимаем слот — защита от двух чатов одновременно
    allowed = try_spin(user_id)
    if not allowed:
        remaining = get_cooldown_remaining(user_id)
        cd_text = format_cooldown(remaining) if remaining else "скоро"
        await msg.reply_text(f'🙁 Следующая прокрутка будет доступна через {cd_text}')
        return

    try:
        # Генерируем номер
        country = global_country
        img_bytes, chars = make_random_plate(country)

        # Считаем монеты
        coins_earned, rarity = calc_coins(chars, country)

        # Обновляем монеты игрока
        data = get_user(user_id)
        new_coins = data["coins"] + coins_earned
        from datetime import timezone
        save_user(user_id, new_coins, datetime.now(timezone.utc))
        total_coins = new_coins

        buf = io.BytesIO(img_bytes)
        buf.name = "plate.png"

        caption = f"<blockquote>+{coins_earned} ⚡  |  Всего: {total_coins} ⚡</blockquote>"
        await msg.reply_photo(photo=buf, caption=caption, parse_mode="HTML")

    except Exception as e:
        print(f"[ERROR] handle_nz: {e}", flush=True)

# ── Админ-панель ──────────────────────────────────────────────────────────────

# Состояния ConversationHandler для рассылки
BC_TEXT, BC_MEDIA, BC_BTN_TEXT, BC_BTN_URL, BC_PREVIEW = range(5)

def is_admin(user) -> bool:
    return user and user.username and user.username.lower() == ADMIN_USERNAME.lower()

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton("❌ Закрыть", callback_data="admin_close")],
    ])

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user):
        return
    await update.message.reply_text(
        "🛠 <b>Админ-панель</b>\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=admin_keyboard()
    )

async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает кнопки главного меню админки (не рассылка)."""
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user):
        return
    if query.data == "admin_close":
        await query.message.delete()
    elif query.data == "admin_back":
        await query.edit_message_text(
            "🛠 <b>Админ-панель</b>\n\nВыберите действие:",
            parse_mode="HTML",
            reply_markup=admin_keyboard()
        )

# ── Рассылка: шаг 1 — запуск через кнопку в меню ─────────────────────────────

async def bc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback 'admin_broadcast' — начало рассылки."""
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user):
        return ConversationHandler.END
    context.user_data.clear()
    await query.edit_message_text(
        "📨 <b>Шаг 1: Отправьте текст сообщения для рассылки:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Отмена", callback_data="bc_cancel")]
        ])
    )
    return BC_TEXT

# ── Шаг 1 — получаем текст ───────────────────────────────────────────────────

async def bc_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not is_admin(msg.from_user):
        return ConversationHandler.END
    context.user_data["bc_text"] = msg.text or msg.caption or ""
    context.user_data["bc_entities"] = msg.entities or msg.caption_entities
    try:
        await msg.delete()
    except Exception:
        pass
    sent = await msg.chat.send_message(
        "📨 <b>Шаг 2: Отправьте медиа (фото, видео, GIF) или нажмите «Пропустить»:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➡️ Пропустить", callback_data="bc_skip_media")],
            [InlineKeyboardButton("◀️ Отмена",     callback_data="bc_cancel")],
        ])
    )
    context.user_data["step_msg_id"] = sent.message_id
    return BC_MEDIA

# ── Шаг 2 — медиа ────────────────────────────────────────────────────────────

async def bc_receive_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not is_admin(msg.from_user):
        return ConversationHandler.END
    # Удаляем сервисное сообщение шага
    try:
        await msg.chat.delete_message(context.user_data.get("step_msg_id"))
    except Exception:
        pass
    try:
        await msg.delete()
    except Exception:
        pass
    if msg.photo:
        context.user_data.update(bc_photo=msg.photo[-1].file_id, bc_video=None, bc_animation=None)
    elif msg.video:
        context.user_data.update(bc_photo=None, bc_video=msg.video.file_id, bc_animation=None)
    elif msg.animation:
        context.user_data.update(bc_photo=None, bc_video=None, bc_animation=msg.animation.file_id)
    return await _bc_ask_button(msg.chat.id, context)

async def bc_skip_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user):
        return ConversationHandler.END
    context.user_data.update(bc_photo=None, bc_video=None, bc_animation=None)
    try:
        await query.message.delete()
    except Exception:
        pass
    return await _bc_ask_button(query.message.chat.id, context)

# ── Шаг 3 — кнопка ───────────────────────────────────────────────────────────

async def _bc_ask_button(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    sent = await context.bot.send_message(
        chat_id,
        "📨 <b>Шаг 3: Добавить кнопку со ссылкой?</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Добавить кнопку", callback_data="bc_add_button")],
            [InlineKeyboardButton("➡️ Без кнопки",      callback_data="bc_skip_button")],
            [InlineKeyboardButton("◀️ Отмена",           callback_data="bc_cancel")],
        ])
    )
    context.user_data["step_msg_id"] = sent.message_id
    return BC_BTN_TEXT

async def bc_add_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user):
        return ConversationHandler.END
    await query.edit_message_text(
        "📨 <b>Шаг 4: Введите текст кнопки:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Отмена", callback_data="bc_cancel")]
        ])
    )
    return BC_BTN_TEXT

async def bc_receive_btn_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not is_admin(msg.from_user):
        return ConversationHandler.END
    text = (msg.text or "").strip()
    if not text:
        await msg.reply_text("✖️ Текст кнопки не может быть пустым.")
        return BC_BTN_TEXT
    context.user_data["bc_btn_text"] = text
    try:
        await msg.delete()
    except Exception:
        pass
    sent = await msg.chat.send_message(
        "📨 <b>Шаг 5: Введите URL для кнопки:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Отмена", callback_data="bc_cancel")]
        ])
    )
    context.user_data["step_msg_id"] = sent.message_id
    return BC_BTN_URL

async def bc_skip_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user):
        return ConversationHandler.END
    context.user_data.update(bc_btn_text=None, bc_btn_url=None)
    try:
        await query.message.delete()
    except Exception:
        pass
    return await _bc_show_preview(query.message.chat.id, context)

async def bc_receive_btn_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not is_admin(msg.from_user):
        return ConversationHandler.END
    url = (msg.text or "").strip()
    context.user_data["bc_btn_url"] = url
    try:
        await msg.delete()
    except Exception:
        pass
    try:
        await msg.chat.delete_message(context.user_data.get("step_msg_id"))
    except Exception:
        pass
    return await _bc_show_preview(msg.chat.id, context)

# ── Предпросмотр и подтверждение ─────────────────────────────────────────────

def _build_markup(btn_text, btn_url):
    if btn_text and btn_url:
        return InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, url=btn_url)]])
    return None

async def _bc_show_preview(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    d            = context.user_data
    bc_text      = d.get("bc_text")
    bc_entities  = d.get("bc_entities")
    bc_photo     = d.get("bc_photo")
    bc_video     = d.get("bc_video")
    bc_animation = d.get("bc_animation")
    btn_text     = d.get("bc_btn_text")
    btn_url      = d.get("bc_btn_url")
    reply_markup = _build_markup(btn_text, btn_url)

    await context.bot.send_message(chat_id, "👁 <b>Предпросмотр рассылки:</b>", parse_mode="HTML")

    if bc_photo:
        preview = await context.bot.send_photo(
            chat_id, bc_photo, caption=bc_text,
            caption_entities=bc_entities, reply_markup=reply_markup
        )
    elif bc_video:
        preview = await context.bot.send_video(
            chat_id, bc_video, caption=bc_text,
            caption_entities=bc_entities, reply_markup=reply_markup
        )
    elif bc_animation:
        preview = await context.bot.send_animation(
            chat_id, bc_animation, caption=bc_text,
            caption_entities=bc_entities, reply_markup=reply_markup
        )
    else:
        preview = await context.bot.send_message(
            chat_id, bc_text,
            entities=bc_entities, reply_markup=reply_markup
        )

    confirm = await context.bot.send_message(
        chat_id,
        "❓ <b>Отправить рассылку?</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Отправить", callback_data="bc_confirm"),
            InlineKeyboardButton("✖️ Отмена",   callback_data="bc_cancel"),
        ]])
    )
    context.user_data["preview_msg_id"] = preview.message_id
    context.user_data["confirm_msg_id"] = confirm.message_id
    return BC_PREVIEW

async def bc_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user):
        return ConversationHandler.END

    d            = context.user_data
    bc_text      = d.get("bc_text")
    bc_entities  = d.get("bc_entities")
    bc_photo     = d.get("bc_photo")
    bc_video     = d.get("bc_video")
    bc_animation = d.get("bc_animation")
    btn_text     = d.get("bc_btn_text")
    btn_url      = d.get("bc_btn_url")
    reply_markup = _build_markup(btn_text, btn_url)

    # Удаляем предпросмотр и подтверждение
    for mid in [d.get("confirm_msg_id"), d.get("preview_msg_id")]:
        if mid:
            try:
                await context.bot.delete_message(query.message.chat.id, mid)
            except Exception:
                pass

    context.user_data.clear()

    user_ids = get_all_user_ids()
    status = await query.message.chat.send_message(
        f"⏳ <b>Рассылка начата…</b>\nПолучателей: <b>{len(user_ids)}</b>",
        parse_mode="HTML"
    )

    import asyncio
    success = failed = 0
    for uid in user_ids:
        try:
            if bc_photo:
                await context.bot.send_photo(
                    uid, bc_photo, caption=bc_text,
                    caption_entities=bc_entities, reply_markup=reply_markup
                )
            elif bc_video:
                await context.bot.send_video(
                    uid, bc_video, caption=bc_text,
                    caption_entities=bc_entities, reply_markup=reply_markup
                )
            elif bc_animation:
                await context.bot.send_animation(
                    uid, bc_animation, caption=bc_text,
                    caption_entities=bc_entities, reply_markup=reply_markup
                )
            else:
                await context.bot.send_message(
                    uid, bc_text,
                    entities=bc_entities, reply_markup=reply_markup
                )
            success += 1
        except Exception as e:
            failed += 1
            logger.warning(f"[BROADCAST] uid={uid}: {e}")
        await asyncio.sleep(0.05)

    await status.edit_text(
        f"✅ <b>Рассылка завершена</b>\n\n"
        f"Отправлено: <b>{success}</b>\n"
        f"Ошибок: <b>{failed}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ В меню", callback_data="admin_back")]
        ])
    )
    return ConversationHandler.END

async def bc_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.message.chat.send_message(
        "🛠 <b>Админ-панель</b>\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=admin_keyboard()
    )
    return ConversationHandler.END

async def handle_add_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Когда бота добавляют в группу — отправляет приветственное сообщение."""
    msg = update.message
    if not msg:
        return
    bot_id = (await context.bot.get_me()).id
    for member in (msg.new_chat_members or []):
        if member.id == bot_id:
            await msg.reply_text(ADD_TO_CHAT_TEXT)
            break

async def post_init(application: Application):
    init_db()
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.bot.delete_my_commands()

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("info",     cmd_info))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("admin",    cmd_admin))
    app.add_handler(CallbackQueryHandler(add_to_chat_callback, pattern=r"^add_to_chat$"))
    app.add_handler(CallbackQueryHandler(settings_callback,    pattern=r"^set_country_"))
    app.add_handler(CallbackQueryHandler(admin_panel_callback, pattern=r"^admin_(close|back)$"))

    # Рассылка — пошаговый диалог
    broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(bc_start, pattern=r"^admin_broadcast$")],
        states={
            BC_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bc_receive_text),
            ],
            BC_MEDIA: [
                MessageHandler(filters.PHOTO | filters.VIDEO | filters.ANIMATION, bc_receive_media),
                CallbackQueryHandler(bc_skip_media, pattern=r"^bc_skip_media$"),
            ],
            BC_BTN_TEXT: [
                CallbackQueryHandler(bc_add_button,  pattern=r"^bc_add_button$"),
                CallbackQueryHandler(bc_skip_button, pattern=r"^bc_skip_button$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, bc_receive_btn_text),
            ],
            BC_BTN_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bc_receive_btn_url),
            ],
            BC_PREVIEW: [
                CallbackQueryHandler(bc_confirm, pattern=r"^bc_confirm$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(bc_cancel, pattern=r"^bc_cancel$")],
        per_chat=True,
        per_user=True,
    )
    app.add_handler(broadcast_conv)

    # Только группы
    group_only = filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND
    app.add_handler(MessageHandler(group_only, handle_nz))

    # Бота добавили в чат
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_add_to_chat))
    app.add_error_handler(error_handler)

    WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
    PORT = int(os.environ.get("PORT", 8080))

    if WEBHOOK_URL:
        print(f"Бот запущен в webhook режиме на порту {PORT}...")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )
    else:
        print("Бот запущен в polling режиме (локально)...")
        app.run_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
            close_loop=False,
        )

if __name__ == "__main__":
    main()
