import asyncio
import io
import logging
import os
import random
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from PIL import Image, ImageDraw, ImageFont
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)

BOT_TOKEN    = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

# ── PostgreSQL ─────────────────────────────────────────────────────────────────
import psycopg2
from psycopg2.extras import RealDictCursor

def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id   BIGINT PRIMARY KEY,
                    coins     INTEGER DEFAULT 0,
                    last_spin TIMESTAMP
                )
            """)
            # Добавляем новые колонки если их нет (миграция)
            for col, definition in [
                ("username",      "TEXT"),
                ("joined_at",     "TIMESTAMP DEFAULT NOW()"),
                ("blocked",       "BOOLEAN DEFAULT FALSE"),
                ("played_webapp", "BOOLEAN DEFAULT FALSE"),
            ]:
                cur.execute("""
                    ALTER TABLE users ADD COLUMN IF NOT EXISTS %s %s
                """ % (col, definition))
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id  BIGINT PRIMARY KEY,
                    title    TEXT,
                    username TEXT,
                    added_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS admin_actions (
                    id         SERIAL PRIMARY KEY,
                    admin_id   BIGINT,
                    target_id  BIGINT,
                    action     TEXT,
                    detail     TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pm_notified (
                    user_id BIGINT PRIMARY KEY
                )
            """)
        conn.commit()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Глобальные настройки ───────────────────────────────────────────────────────
global_country:   str            = "RU"
COOLDOWN_HOURS:   float          = 3.0
MAINTENANCE_MODE: bool           = False
X2_ACTIVE:        bool           = False
X2_UNTIL:         Optional[datetime] = None

ADMIN_USERNAME = "tntks"
SUSPICIOUS_COINS_THRESHOLD = 25

# ── Состояния ConversationHandler ──────────────────────────────────────────────
(
    ADMIN_BC_TEXT, ADMIN_BC_MEDIA, ADMIN_BC_BTN_CHOICE,
    ADMIN_BC_BTN_TEXT, ADMIN_BC_BTN_URL,
    ADMIN_USER_ACTION_ID, ADMIN_USER_GIVE_COINS, ADMIN_USER_TAKE_COINS,
    ADMIN_X2_HOURS,
) = range(9)

# ── БД helpers ─────────────────────────────────────────────────────────────────

def get_user(user_id: int) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
    if row is None:
        return {"coins": 0, "last_spin": None, "blocked": False,
                "played_webapp": False, "username": None}
    return dict(row)

def ensure_user(user_id: int, username: str = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, username) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET username = COALESCE(EXCLUDED.username, users.username)
            """, (user_id, username))
        conn.commit()

def save_user(user_id: int, coins: int, last_spin, username: str = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, coins, last_spin, username)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET coins = EXCLUDED.coins, last_spin = EXCLUDED.last_spin,
                    username = COALESCE(EXCLUDED.username, users.username)
            """, (user_id, coins, last_spin, username))
        conn.commit()

def try_spin(user_id: int) -> bool:
    now          = datetime.now(timezone.utc)
    cooldown_ago = now - timedelta(hours=COOLDOWN_HOURS)
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Обновляем last_spin только если КД прошёл
            cur.execute("""
                UPDATE users
                SET last_spin = %s
                WHERE user_id = %s
                  AND (last_spin IS NULL OR last_spin <= %s)
                RETURNING user_id
            """, (now, user_id, cooldown_ago))
            result = cur.fetchone()
            if result is None:
                # Новый пользователь — создаём запись
                cur.execute("""
                    INSERT INTO users (user_id, coins, last_spin)
                    VALUES (%s, 0, %s)
                    ON CONFLICT (user_id) DO NOTHING
                    RETURNING user_id
                """, (user_id, now))
                result = cur.fetchone()
        conn.commit()
    return result is not None

def reset_spin(user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET last_spin = NULL WHERE user_id = %s", (user_id,))
        conn.commit()

def get_cooldown_remaining(user_id: int):
    user = get_user(user_id)
    if user["last_spin"] is None:
        return None
    last_spin = user["last_spin"]
    if last_spin.tzinfo is None:
        last_spin = last_spin.replace(tzinfo=timezone.utc)
    now      = datetime.now(timezone.utc)
    elapsed  = now - last_spin
    cooldown = timedelta(hours=COOLDOWN_HOURS)
    if elapsed >= cooldown:
        return None
    return cooldown - elapsed

def format_cooldown(td: timedelta) -> str:
    total = int(td.total_seconds())
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:   return f"{h}ч {m}мин"
    if m > 0:   return f"{m}мин {s}сек"
    return f"{s}сек"

def log_admin_action(admin_id: int, target_id: int, action: str, detail: str = ""):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO admin_actions (admin_id, target_id, action, detail)
                VALUES (%s, %s, %s, %s)
            """, (admin_id, target_id, action, detail))
        conn.commit()

def register_chat(chat_id: int, title: str, username: str = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO chats (chat_id, title, username)
                VALUES (%s, %s, %s)
                ON CONFLICT (chat_id) DO UPDATE
                SET title = EXCLUDED.title,
                    username = COALESCE(EXCLUDED.username, chats.username)
            """, (chat_id, title, username))
        conn.commit()

def get_all_users_for_broadcast() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE blocked = FALSE")
            rows = cur.fetchall()
    return [r["user_id"] for r in rows]

# ── Вспомогательные ────────────────────────────────────────────────────────────

def is_admin(user) -> bool:
    if user is None:
        return False
    return (user.username or "").lstrip("@").lower() == ADMIN_USERNAME.lower()

def check_x2():
    global X2_ACTIVE, X2_UNTIL
    if X2_ACTIVE and X2_UNTIL and datetime.now(timezone.utc) >= X2_UNTIL:
        X2_ACTIVE = False
        X2_UNTIL  = None

def _cd_label() -> str:
    h = COOLDOWN_HOURS
    if h < 1:
        return f"{int(h*60)}мин"
    if h == int(h):
        return f"{int(h)}ч"
    full_h = int(h)
    return f"{full_h}ч {int((h - full_h)*60)}мин"

def _back_admin_btn():
    return [[InlineKeyboardButton("◀️ Назад", callback_data="adm_back")]]

def _admin_keyboard():
    x2_lbl = "✨ Х2 монеты: ВКЛ 🟢" if X2_ACTIVE else "✨ Х2 монеты: ВЫКЛ 🔴"
    mt_lbl = "🔧 Тех перерыв: ВКЛ 🟢" if MAINTENANCE_MODE else "🔧 Тех перерыв: ВЫКЛ 🔴"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📨 Рассылка",                  callback_data="adm_broadcast")],
        [InlineKeyboardButton("📊 Полная статистика",         callback_data="adm_stats_full")],
        [InlineKeyboardButton("📅 Статистика по дням",        callback_data="adm_stats_days")],
        [InlineKeyboardButton("🏆 Топ по монетам",            callback_data="adm_top_coins")],
        [InlineKeyboardButton("💬 Статистика групп",          callback_data="adm_group_stats")],
        [InlineKeyboardButton("👤 Действия с юзером",         callback_data="adm_user_actions")],
        [InlineKeyboardButton(mt_lbl,                         callback_data="adm_maintenance")],
        [InlineKeyboardButton(f"⏱ КД ({_cd_label()})",       callback_data="adm_change_cd")],
        [InlineKeyboardButton(x2_lbl,                         callback_data="adm_x2")],
    ])

# ── Монеты ─────────────────────────────────────────────────────────────────────

def calc_coins(chars: str, country: str) -> tuple[int, str]:
    digits = [c for c in chars if c.isdigit()]
    if not digits:
        return 5, "Обычный"
    if len(digits) >= 3 and len(set(digits)) == 1:
        return 100, "🏆 Легендарный"
    digit_str = "".join(digits)
    has_quad = (len(digit_str) >= 4 and
                any(digit_str[i]==digit_str[i+1]==digit_str[i+2]==digit_str[i+3]
                    for i in range(len(digit_str)-3)))
    if has_quad:
        return 40, "🔥 Четвёрка"
    has_triple = (len(digit_str) >= 3 and
                  any(digit_str[i]==digit_str[i+1]==digit_str[i+2]
                      for i in range(len(digit_str)-2)))
    if has_triple:
        return 25, "✨ Тройник"
    if len(digits) >= 3 and digits == digits[::-1]:
        return 15, "🪞 Зеркальный"
    asc  = all(int(digits[i+1])==int(digits[i])+1 for i in range(len(digits)-1))
    desc = all(int(digits[i+1])==int(digits[i])-1 for i in range(len(digits)-1))
    if (asc or desc) and len(digits) >= 3:
        return 10, "⭐ Красивый"
    return 5, "Обычный"

# ── Тексты ─────────────────────────────────────────────────────────────────────

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

MAINTENANCE_PM_TEXT = (
    "🔧 Сейчас проходит технический перерыв.\n"
    "Бот напишет тебе, когда снова будет доступен."
)

# ── Шрифты ─────────────────────────────────────────────────────────────────────

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
        return bold, reg
    font_dir = "/tmp/bot_fonts"
    os.makedirs(font_dir, exist_ok=True)
    dl_bold = os.path.join(font_dir, "Roboto-Bold.ttf")
    dl_reg  = os.path.join(font_dir, "Roboto-Regular.ttf")
    for dest, urls in [
        (dl_bold, [
            "https://github.com/google/fonts/raw/main/apache/roboto/static/Roboto-Bold.ttf",
            "https://github.com/googlefonts/roboto/raw/v2.138/src/hinted/Roboto-Bold.ttf",
        ]),
        (dl_reg, [
            "https://github.com/google/fonts/raw/main/apache/roboto/static/Roboto-Regular.ttf",
            "https://github.com/googlefonts/roboto/raw/v2.138/src/hinted/Roboto-Regular.ttf",
        ]),
    ]:
        if not (os.path.exists(dest) and os.path.getsize(dest) > 10000):
            for url in urls:
                try:
                    urllib.request.urlretrieve(url, dest)
                    if os.path.getsize(dest) > 10000:
                        break
                except Exception:
                    pass
    bold_ok = os.path.exists(dl_bold) and os.path.getsize(dl_bold) > 10000
    reg_ok  = os.path.exists(dl_reg)  and os.path.getsize(dl_reg)  > 10000
    return (dl_bold if bold_ok else None), (dl_reg if reg_ok else None)

FONT_BOLD, FONT_REG = _find_or_download_fonts()

def _font(path, size: int):
    if path and os.path.exists(path):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    global FONT_BOLD, FONT_REG
    FONT_BOLD, FONT_REG = _find_or_download_fonts()
    target = FONT_BOLD if path == FONT_BOLD else FONT_REG
    if target and os.path.exists(target):
        try:
            return ImageFont.truetype(target, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()

# ── Флаг Казахстана ────────────────────────────────────────────────────────────

def _find_or_download_kz_flag():
    font_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
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

# ── Данные регионов ─────────────────────────────────────────────────────────────

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
    region  = random.choice(UA_REGIONS)
    digits  = "".join(random.choices("0123456789", k=4))
    letters = "".join(random.choices(UA_LETTERS, k=2))
    return f"{digits}{letters}", region

def _random_by_plate():
    region  = random.choice(BY_REGIONS)
    digits  = "".join(random.choices("0123456789", k=4))
    letters = "".join(random.choices(BY_LETTERS, k=2))
    return f"{digits}{letters}", region

def _random_kz_plate():
    region  = random.choice(KZ_REGIONS)
    digits  = "".join(random.choices("0123456789", k=3))
    letters = "".join(random.choices(KZ_LAT, k=3))
    return f"{digits}{letters}", region

# ── Генерация изображений ──────────────────────────────────────────────────────

def _dot_grid(draw, w, h):
    for x in range(0, w+1, 20):
        for y in range(0, h+1, 20):
            draw.ellipse([x-1, y-1, x+1, y+1], fill="#d0d0d0")

def _ru_flag(draw, fx, fy, fw=32, fh=22):
    th = fh // 3
    draw.rectangle([fx, fy,      fx+fw, fy+th],   fill="white", outline="#cccccc", width=1)
    draw.rectangle([fx, fy+th,   fx+fw, fy+th*2], fill="#003DA5")
    draw.rectangle([fx, fy+th*2, fx+fw, fy+fh],   fill="#CC0000")

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
        draw.rounded_rectangle([px, py, px+pw, py+ph], radius=10, fill="white", outline="#111111", width=4)
        right_w = 110; rdx = px+pw-right_w
        draw.line([(rdx, py+6),(rdx, py+ph-6)], fill="#111111", width=3)
        fnt_pl = _font(FONT_BOLD, 72)
        draw.text((px+(pw-right_w)//2, cy), chars, fill="#111111", font=fnt_pl, anchor="mm")
        rcx = rdx+right_w//2; rpt = py+6; rpb = py+ph-6; rph = rpb-rpt
        fnt_r = _font(FONT_BOLD, 48)
        draw.text((rcx, rpt+int(rph*0.42)), region, fill="#111111", font=fnt_r, anchor="mm")
        fw, fh = 22, 15; fnt_rus = _font(FONT_BOLD, 13)
        rus_w = int(fnt_rus.getlength("RUS")); gap = 3; total_w = rus_w+gap+fw
        rus_cy = rpt+int(rph*0.82); tx = rcx-total_w//2; fx = tx+rus_w+gap; fy = rus_cy-fh//2
        draw.text((tx, rus_cy), "RUS", fill="#111111", font=fnt_rus, anchor="lm")
        _ru_flag(draw, fx, fy, fw=fw, fh=fh)

    elif country == "UA":
        draw.rounded_rectangle([px+4, py+4, px+pw+4, py+ph+4], radius=8, fill="#b0b0b0")
        draw.rounded_rectangle([px, py, px+pw, py+ph], radius=8, fill="white", outline="#111111", width=4)
        strip_w = 62
        draw.rounded_rectangle([px+2, py+2, px+strip_w, py+ph-2], radius=7, fill="#003DA5")
        draw.line([(px+strip_w, py+4),(px+strip_w, py+ph-4)], fill="#111111", width=3)
        ffw, ffh = 38, 26; ffx = px+(strip_w-ffw)//2; ffy = py+16
        draw.rectangle([ffx, ffy, ffx+ffw, ffy+ffh//2], fill="#005BBB")
        draw.rectangle([ffx, ffy+ffh//2, ffx+ffw, ffy+ffh], fill="#FFD500")
        fnt_ua = _font(FONT_BOLD, 17)
        draw.text((px+strip_w//2, py+ph-14), "UA", fill="white", font=fnt_ua, anchor="mm")
        c = chars.strip().upper().replace(" ", "")
        body = f"{region} {c[:4]} {c[4:]}" if len(c) >= 6 else f"{region} {c}"
        fnt_pl = _font(FONT_BOLD, 66)
        draw.text((px+strip_w+(pw-strip_w)//2, cy), body, fill="#111111", font=fnt_pl, anchor="mm")

    elif country == "BY":
        draw.rounded_rectangle([px+4, py+4, px+pw+4, py+ph+4], radius=8, fill="#b0b0b0")
        draw.rounded_rectangle([px, py, px+pw, py+ph], radius=8, fill="white", outline="#111111", width=5)
        zone_w = 90; fl_w = 72; fl_h = 48
        fnt_by2 = _font(FONT_BOLD, 16); by_bbox = fnt_by2.getbbox("BY")
        by_h = by_bbox[3]-by_bbox[1]; total_h = fl_h+4+by_h
        fl_x = px+(zone_w-fl_w)//2; fl_y = py+(ph-total_h)//2; red_h = round(fl_h*2/3)
        draw.rectangle([fl_x, fl_y, fl_x+fl_w, fl_y+red_h], fill="#CF101A")
        draw.rectangle([fl_x, fl_y+red_h, fl_x+fl_w, fl_y+fl_h], fill="#007828")
        orn_w = max(7, fl_w//9); step = max(5, orn_w+1); ocx = fl_x+orn_w//2
        draw.rectangle([fl_x, fl_y, fl_x+orn_w, fl_y+fl_h], fill="white")
        for yi in range(fl_y, fl_y+fl_h, step):
            y_top=yi; y_mid=yi+step//2; y_bot=min(yi+step, fl_y+fl_h)
            col = "#CF101A" if y_mid < (fl_y+red_h) else "#007828"
            draw.polygon([(ocx,y_top),(fl_x+orn_w-1,y_mid),(ocx,y_bot),(fl_x+1,y_mid)], fill=col)
            if y_mid < fl_y+fl_h-2:
                mini=step//4
                draw.polygon([(ocx,y_mid-mini),(ocx+mini,y_mid),(ocx,y_mid+mini),(ocx-mini,y_mid)], fill="white")
        draw.text((fl_x+fl_w//2, fl_y+fl_h+4), "BY", fill="#111111", font=fnt_by2, anchor="mt")
        c = chars.replace(" ", "").upper()
        body = f"{c[:4]} {c[4:6]}-{region}" if len(c) >= 6 else f"{chars}-{region}"
        fnt_pl = _font(FONT_BOLD, 68)
        draw.text((px+zone_w+6+(px+pw-8-(px+zone_w+6))//2, cy), body, fill="#111111", font=fnt_pl, anchor="mm")

    elif country == "KZ":
        draw.rounded_rectangle([px+4, py+6, px+pw+4, py+ph+6], radius=10, fill="#aaaaaa")
        draw.rounded_rectangle([px, py, px+pw, py+ph], radius=10, fill="white", outline="#1a1a1a", width=5)
        SW = 90; RW = 64; col_cx = px+SW//2
        fnt_kz_label = _font(FONT_BOLD, 17); kz_bbox = fnt_kz_label.getbbox("KZ")
        kz_h = kz_bbox[3]-kz_bbox[1]; flag_drawn = False; flag_bottom = cy
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

# ══════════════════════════════════════════════════════════════════════════════
#  ХЕНДЛЕРЫ — обычные команды
# ══════════════════════════════════════════════════════════════════════════════

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling update:", exc_info=context.error)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        ensure_user(user.id, user.username)
    keyboard = [
        [InlineKeyboardButton("🎮 Играть", web_app=WebAppInfo(url="https://snusfnf-png.github.io/cardrop/"))],
        [InlineKeyboardButton("Наш чат", url="https://t.me/chatcarzdrop"),
         InlineKeyboardButton("Наш канал", url="https://t.me/carzdrop")],
        [InlineKeyboardButton("➕ Добавить бота в чат", callback_data="add_to_chat")],
    ]
    await update.message.reply_text(WELCOME_TEXT, reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if MAINTENANCE_MODE and not is_admin(update.effective_user):
        return
    await update.message.reply_text(INFO_TEXT, parse_mode="HTML")

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if MAINTENANCE_MODE and not is_admin(update.effective_user):
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("⚙️ Настройки доступны только в личных сообщениях с ботом.")
        return
    current = global_country
    flags   = {"RU": "🇷🇺", "UA": "🇺🇦", "BY": "🇧🇾", "KZ": "🇰🇿"}
    names   = {"RU": "Россия", "UA": "Украина", "BY": "Беларусь", "KZ": "Казахстан"}
    keyboard = []; row = []
    for code in ["RU", "UA", "BY", "KZ"]:
        mark = " ✅" if code == current else ""
        row.append(InlineKeyboardButton(f"{flags[code]} {names[code]}{mark}", callback_data=f"set_country_{code}"))
        if len(row) == 2:
            keyboard.append(row); row = []
    if row:
        keyboard.append(row)
    await update.message.reply_text(
        "🌍 Выбери страну номерного знака (применяется во всех группах):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        return
    await update.message.reply_text(
        "🛠 <b>Админ-панель</b>",
        parse_mode="HTML",
        reply_markup=_admin_keyboard()
    )

# ── Кнопки страны ──────────────────────────────────────────────────────────────

async def add_to_chat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    bot_username = (await context.bot.get_me()).username
    keyboard = [[InlineKeyboardButton("➕ Добавить бота", url=f"https://t.me/{bot_username}?startgroup=start")]]
    await query.message.reply_text(ADD_TO_CHAT_TEXT, reply_markup=InlineKeyboardMarkup(keyboard))

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global global_country
    query = update.callback_query; await query.answer()
    code = query.data.replace("set_country_", "")
    global_country = code
    flags = {"RU": "🇷🇺", "UA": "🇺🇦", "BY": "🇧🇾", "KZ": "🇰🇿"}
    names = {"RU": "Россия", "UA": "Украина", "BY": "Беларусь", "KZ": "Казахстан"}
    keyboard = []; row = []
    for c in ["RU", "UA", "BY", "KZ"]:
        mark = " ✅" if c == code else ""
        row.append(InlineKeyboardButton(f"{flags[c]} {names[c]}{mark}", callback_data=f"set_country_{c}"))
        if len(row) == 2:
            keyboard.append(row); row = []
    if row:
        keyboard.append(row)
    await query.edit_message_text(
        f"✅ Страна изменена на {flags[code]} {names[code]}!\n\n🌍 Выбери страну номерного знака:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN CALLBACKS — простые
# ══════════════════════════════════════════════════════════════════════════════

async def adm_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user): return
    await query.edit_message_text("🛠 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=_admin_keyboard())

async def adm_stats_full(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user): return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM users")
            total = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE played_webapp = TRUE")
            played = cur.fetchone()["cnt"]
            cur.execute("SELECT user_id, username, coins FROM users ORDER BY coins DESC LIMIT 100")
            rows = cur.fetchall()
    lines = [
        f"👥 <b>Всего пользователей:</b> {total}",
        f"🎮 <b>Нажали «Играть»:</b> {played}", "",
        "<b>Пользователи (топ-100 по монетам):</b>"
    ]
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else "—"
        lines.append(f"<code>{r['user_id']}</code>  {uname}  ⚡{r['coins']}")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…"
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(_back_admin_btn()))

async def adm_stats_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user): return
    lines = ["📅 <b>Статистика по дням (последние 14)</b>", ""]
    with get_conn() as conn:
        with conn.cursor() as cur:
            for i in range(13, -1, -1):
                d_start = datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0) - timedelta(days=i)
                d_end   = d_start + timedelta(days=1)
                cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE joined_at >= %s AND joined_at < %s", (d_start, d_end))
                new_u = cur.fetchone()["cnt"]
                cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE played_webapp=TRUE AND joined_at >= %s AND joined_at < %s", (d_start, d_end))
                played = cur.fetchone()["cnt"]
                lines.append(f"<b>{d_start.strftime('%d.%m')}</b>  👤 +{new_u}  🎮 {played}")
    await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(_back_admin_btn()))

async def adm_top_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user): return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, username, coins FROM users ORDER BY coins DESC LIMIT 50")
            rows = cur.fetchall()
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines  = ["🏆 <b>Топ-50 по монетам</b>", ""]
    for i, r in enumerate(rows, 1):
        uname = f"@{r['username']}" if r["username"] else "—"
        lines.append(f"{medals.get(i, f'{i}.')} <code>{r['user_id']}</code> {uname} — ⚡{r['coins']}")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…"
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(_back_admin_btn()))

async def adm_group_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user): return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT chat_id, title, username, added_at FROM chats ORDER BY added_at DESC")
            rows = cur.fetchall()
    lines = [f"💬 <b>Бот в {len(rows)} чатах:</b>", ""]
    for r in rows:
        added = r["added_at"].strftime("%d.%m.%Y %H:%M") if r["added_at"] else "—"
        link  = f'<a href="https://t.me/{r["username"]}">{r["title"]}</a>' if r["username"] else f"{r['title']} (<code>{r['chat_id']}</code>)"
        lines.append(f"• {link}\n  📅 {added}")
    text = "\n".join(lines) or "Нет данных"
    if len(text) > 4000:
        text = text[:4000] + "\n…"
    await query.edit_message_text(text, parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup(_back_admin_btn()),
                                  disable_web_page_preview=True)

async def adm_user_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user): return
    await query.edit_message_text(
        "👤 <b>Действия с пользователем</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Выдать монеты",    callback_data="usr_give")],
            [InlineKeyboardButton("➖ Снять монеты",     callback_data="usr_take")],
            [InlineKeyboardButton("🔒 Заблокировать",    callback_data="usr_ban")],
            [InlineKeyboardButton("🔓 Разблокировать",   callback_data="usr_unban")],
            [InlineKeyboardButton("🔄 Сброс КД",         callback_data="usr_reset_cd")],
            [InlineKeyboardButton("📋 История действий", callback_data="usr_history")],
            [InlineKeyboardButton("◀️ Назад",            callback_data="adm_back")],
        ])
    )

async def adm_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE_MODE
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user): return
    MAINTENANCE_MODE = not MAINTENANCE_MODE

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT chat_id FROM chats")
            chats = [r["chat_id"] for r in cur.fetchall()]

    grp_msg = "🔧 Начался технический перерыв. Бот временно недоступен." if MAINTENANCE_MODE \
              else "✅ Технический перерыв завершён. Бот снова работает!"
    for cid in chats:
        try:
            await context.bot.send_message(cid, grp_msg)
        except Exception:
            pass

    if not MAINTENANCE_MODE:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM pm_notified")
                notified = [r["user_id"] for r in cur.fetchall()]
                cur.execute("DELETE FROM pm_notified")
            conn.commit()
        for uid in notified:
            try:
                await context.bot.send_message(uid, "✅ Технический перерыв завершён! Бот снова работает.")
            except Exception:
                pass

    status = "ВКЛ 🟢" if MAINTENANCE_MODE else "ВЫКЛ 🔴"
    await query.edit_message_text(
        f"🔧 Тех перерыв <b>{status}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(_back_admin_btn())
    )

async def adm_change_cd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user): return
    options = [
        ("10мин","0.1667"),("30мин","0.5"),
        ("1ч","1"),("1ч 30мин","1.5"),
        ("2ч","2"),("2ч 30мин","2.5"),
        ("3ч","3"),("4ч","4"),("5ч","5"),
    ]
    keyboard = []; row = []
    for label, val in options:
        mark = " ✅" if abs(COOLDOWN_HOURS - float(val)) < 0.01 else ""
        row.append(InlineKeyboardButton(f"{label}{mark}", callback_data=f"set_cd_{val}"))
        if len(row) == 3:
            keyboard.append(row); row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_back")])
    await query.edit_message_text(
        f"⏱ <b>Текущий КД: {_cd_label()}</b>\nВыбери новый:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def adm_set_cd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global COOLDOWN_HOURS
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user): return
    COOLDOWN_HOURS = float(query.data.replace("set_cd_", ""))
    await query.edit_message_text(
        f"✅ КД изменён на <b>{_cd_label()}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(_back_admin_btn())
    )

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN ConversationHandler: РАССЫЛКА
# ══════════════════════════════════════════════════════════════════════════════

async def adm_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user): return ConversationHandler.END
    context.user_data.clear()
    await query.edit_message_text(
        "📨 <b>Шаг 1: Отправьте текст сообщения для рассылки:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(_back_admin_btn())
    )
    return ADMIN_BC_TEXT

async def adm_bc_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return ConversationHandler.END
    context.user_data["bc_text"] = update.message.text or update.message.caption or ""
    await update.message.reply_text(
        "📨 <b>Шаг 2: Отправьте фото/видео/GIF или нажмите «Пропустить»</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Пропустить ➡️", callback_data="bc_skip_media")],
            [InlineKeyboardButton("◀️ Отмена",     callback_data="adm_back")],
        ])
    )
    return ADMIN_BC_MEDIA

async def adm_bc_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return ConversationHandler.END
    msg = update.message
    if msg.photo:
        context.user_data.update(bc_file_id=msg.photo[-1].file_id, bc_file_type="photo")
    elif msg.video:
        context.user_data.update(bc_file_id=msg.video.file_id, bc_file_type="video")
    elif msg.animation:
        context.user_data.update(bc_file_id=msg.animation.file_id, bc_file_type="animation")
    await _bc_ask_button(msg.chat.id, context)
    return ADMIN_BC_BTN_CHOICE

async def adm_bc_skip_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user): return ConversationHandler.END
    context.user_data.update(bc_file_id=None, bc_file_type=None)
    await _bc_ask_button(query.message.chat.id, context)
    return ADMIN_BC_BTN_CHOICE

async def _bc_ask_button(chat_id, context):
    await context.bot.send_message(
        chat_id,
        "📨 <b>Шаг 3: Добавить кнопку со ссылкой?</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Добавить кнопку", callback_data="bc_add_button")],
            [InlineKeyboardButton("Без кнопки ➡️",   callback_data="bc_skip_button")],
            [InlineKeyboardButton("◀️ Отмена",        callback_data="adm_back")],
        ])
    )

async def adm_bc_add_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user): return ConversationHandler.END
    await query.message.reply_text(
        "📨 <b>Введите текст кнопки:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(_back_admin_btn())
    )
    return ADMIN_BC_BTN_TEXT

async def adm_bc_btn_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return ConversationHandler.END
    context.user_data["bc_btn_text"] = update.message.text
    await update.message.reply_text(
        "📨 <b>Введите URL для кнопки:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(_back_admin_btn())
    )
    return ADMIN_BC_BTN_URL

async def adm_bc_btn_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return ConversationHandler.END
    context.user_data["bc_btn_url"] = update.message.text
    await _bc_show_preview(update.message.chat.id, context)
    return ADMIN_BC_BTN_CHOICE

async def adm_bc_skip_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user): return ConversationHandler.END
    context.user_data.update(bc_btn_text=None, bc_btn_url=None)
    await _bc_show_preview(query.message.chat.id, context)
    return ADMIN_BC_BTN_CHOICE

async def _bc_show_preview(chat_id, context):
    d        = context.user_data
    text     = d.get("bc_text", "")
    file_id  = d.get("bc_file_id")
    ftype    = d.get("bc_file_type")
    btn_text = d.get("bc_btn_text")
    btn_url  = d.get("bc_btn_url")
    markup   = InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, url=btn_url)]]) if btn_text and btn_url else None
    await context.bot.send_message(chat_id, "👁 <b>Предпросмотр:</b>", parse_mode="HTML")
    if ftype == "photo":
        await context.bot.send_photo(chat_id, file_id, caption=text, reply_markup=markup)
    elif ftype == "video":
        await context.bot.send_video(chat_id, file_id, caption=text, reply_markup=markup)
    elif ftype == "animation":
        await context.bot.send_animation(chat_id, file_id, caption=text, reply_markup=markup)
    else:
        await context.bot.send_message(chat_id, text, reply_markup=markup)
    await context.bot.send_message(
        chat_id, "❓ <b>Отправить рассылку?</b>", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Отправить", callback_data="bc_confirm"),
             InlineKeyboardButton("❌ Отмена",    callback_data="bc_cancel_preview")],
        ])
    )

async def adm_bc_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user): return ConversationHandler.END
    d        = context.user_data
    text     = d.get("bc_text", "")
    file_id  = d.get("bc_file_id")
    ftype    = d.get("bc_file_type")
    btn_text = d.get("bc_btn_text")
    btn_url  = d.get("bc_btn_url")
    markup   = InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, url=btn_url)]]) if btn_text and btn_url else None
    users    = get_all_users_for_broadcast()
    status   = await query.message.reply_text("📤 <b>Рассылка начата…</b>", parse_mode="HTML")
    ok = fail = 0
    for uid in users:
        try:
            if ftype == "photo":       await context.bot.send_photo(uid, file_id, caption=text, reply_markup=markup)
            elif ftype == "video":     await context.bot.send_video(uid, file_id, caption=text, reply_markup=markup)
            elif ftype == "animation": await context.bot.send_animation(uid, file_id, caption=text, reply_markup=markup)
            else:                      await context.bot.send_message(uid, text, reply_markup=markup)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await status.edit_text(
        f"✅ <b>Рассылка завершена</b>\n\n<blockquote>Успешно: {ok}\nОшибок: {fail}</blockquote>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(_back_admin_btn())
    )
    context.user_data.clear()
    return ConversationHandler.END

async def adm_bc_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    context.user_data.clear()
    await query.edit_message_text("🛠 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=_admin_keyboard())
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN ConversationHandler: ДЕЙСТВИЯ С ЮЗЕРОМ
# ══════════════════════════════════════════════════════════════════════════════

async def _ask_user_id(query, context, action_key, prompt):
    context.user_data["usr_action"] = action_key
    await query.edit_message_text(
        f"👤 <b>{prompt}</b>\n\n<i>Введите Telegram ID:</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(_back_admin_btn())
    )
    return ADMIN_USER_ACTION_ID

async def usr_give_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    return await _ask_user_id(q, context, "give", "Выдать монеты")

async def usr_take_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    return await _ask_user_id(q, context, "take", "Снять монеты")

async def usr_ban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    return await _ask_user_id(q, context, "ban", "Заблокировать")

async def usr_unban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    return await _ask_user_id(q, context, "unban", "Разблокировать")

async def usr_reset_cd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    return await _ask_user_id(q, context, "reset_cd", "Сброс КД")

async def usr_history_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    return await _ask_user_id(q, context, "history", "История действий")

async def usr_action_id_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return ConversationHandler.END
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Введите числовой ID"); return ADMIN_USER_ACTION_ID
    uid    = int(text)
    action = context.user_data.get("usr_action")
    admin  = update.effective_user

    if action in ("ban", "unban"):
        blocked = (action == "ban")
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET blocked = %s WHERE user_id = %s", (blocked, uid))
            conn.commit()
        log_admin_action(admin.id, uid, action)
        label = "заблокирован 🔒" if blocked else "разблокирован 🔓"
        await update.message.reply_text(
            f"Пользователь <code>{uid}</code> {label}.",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(_back_admin_btn())
        )
        return ConversationHandler.END

    elif action == "reset_cd":
        reset_spin(uid)
        log_admin_action(admin.id, uid, "reset_cd")
        await update.message.reply_text(
            f"🔄 КД пользователя <code>{uid}</code> сброшен.",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(_back_admin_btn())
        )
        return ConversationHandler.END

    elif action == "history":
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT action, detail, created_at FROM admin_actions
                    WHERE target_id = %s ORDER BY created_at DESC LIMIT 20
                """, (uid,))
                rows = cur.fetchall()
        if not rows:
            result = f"📋 История <code>{uid}</code>: пусто"
        else:
            lines = [f"📋 <b>История <code>{uid}</code>:</b>"]
            for r in rows:
                ts = r["created_at"].strftime("%d.%m %H:%M")
                lines.append(f"• {ts} — {r['action']} {r['detail'] or ''}")
            result = "\n".join(lines)
        await update.message.reply_text(result, parse_mode="HTML",
                                        reply_markup=InlineKeyboardMarkup(_back_admin_btn()))
        return ConversationHandler.END

    elif action == "give":
        context.user_data["usr_target_id"] = uid
        await update.message.reply_text(
            f"➕ Выдать монеты <code>{uid}</code>\nСколько?",
            parse_mode="HTML"
        )
        return ADMIN_USER_GIVE_COINS

    elif action == "take":
        context.user_data["usr_target_id"] = uid
        await update.message.reply_text(
            f"➖ Снять монеты у <code>{uid}</code>\nСколько?",
            parse_mode="HTML"
        )
        return ADMIN_USER_GIVE_COINS  # переиспользуем состояние, action хранится в user_data

    return ConversationHandler.END

async def usr_give_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return ConversationHandler.END
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Введите число"); return ADMIN_USER_GIVE_COINS
    amount = int(text)
    uid    = context.user_data.get("usr_target_id")
    action = context.user_data.get("usr_action", "give")
    data   = get_user(uid)
    if action == "take":
        new_coins = max(0, data["coins"] - amount)
        label = f"снято <b>{amount} ⚡</b>"
        log_admin_action(update.effective_user.id, uid, "take_coins", f"-{amount}")
    else:
        new_coins = data["coins"] + amount
        label = f"выдано <b>+{amount} ⚡</b>"
        log_admin_action(update.effective_user.id, uid, "give_coins", f"+{amount}")
    save_user(uid, new_coins, data["last_spin"])
    await update.message.reply_text(
        f"✅ Пользователю <code>{uid}</code> {label}\nБаланс: {new_coins} ⚡",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(_back_admin_btn())
    )
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN ConversationHandler: X2
# ══════════════════════════════════════════════════════════════════════════════

async def adm_x2_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global X2_ACTIVE, X2_UNTIL
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user): return ConversationHandler.END
    check_x2()
    if X2_ACTIVE:
        X2_ACTIVE = False; X2_UNTIL = None
        await query.edit_message_text(
            "✨ Х2 монеты <b>отключён</b>.",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(_back_admin_btn())
        )
        return ConversationHandler.END
    await query.edit_message_text(
        "✨ <b>Х2 монеты</b>\nНа сколько часов включить? (введите число)",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(_back_admin_btn())
    )
    return ADMIN_X2_HOURS

async def adm_x2_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global X2_ACTIVE, X2_UNTIL
    if not is_admin(update.effective_user): return ConversationHandler.END
    text = update.message.text.strip().replace(",", ".")
    try:
        hours = float(text); assert hours > 0
    except Exception:
        await update.message.reply_text("❌ Введите положительное число")
        return ADMIN_X2_HOURS
    X2_ACTIVE = True
    X2_UNTIL  = datetime.now(timezone.utc) + timedelta(hours=hours)
    await update.message.reply_text(
        f"✅ <b>Х2 монеты включён на {hours}ч</b>\nДо: {X2_UNTIL.strftime('%d.%m %H:%M')} UTC",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(_back_admin_btn())
    )
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
#  НЗ
# ══════════════════════════════════════════════════════════════════════════════

async def _alert_admin(context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM users WHERE username = %s", (ADMIN_USERNAME,))
                row = cur.fetchone()
        if row:
            await context.bot.send_message(row["user_id"], text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"[_alert_admin] {e}")

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

    if MAINTENANCE_MODE:
        return

    udata = get_user(user_id)
    if udata.get("blocked"):
        return

    remaining = get_cooldown_remaining(user_id)
    if remaining is not None:
        await msg.reply_text(f'🙁 Следующая прокрутка будет доступна через {format_cooldown(remaining)}')
        return

    allowed = try_spin(user_id)
    if not allowed:
        remaining = get_cooldown_remaining(user_id)
        await msg.reply_text(f'🙁 Следующая прокрутка будет доступна через {format_cooldown(remaining) if remaining else "скоро"}')
        return

    try:
        img_bytes, chars = make_random_plate(global_country)
        coins_earned, rarity = calc_coins(chars, global_country)

        check_x2()
        if X2_ACTIVE:
            coins_earned *= 2

        data      = get_user(user_id)
        new_coins = data["coins"] + coins_earned
        save_user(user_id, new_coins, datetime.now(timezone.utc), user.username)

        buf = io.BytesIO(img_bytes); buf.name = "plate.png"
        caption = f"<blockquote>+{coins_earned} ⚡  |  Всего: {new_coins} ⚡</blockquote>"
        await msg.reply_photo(photo=buf, caption=caption, parse_mode="HTML")

        if coins_earned >= SUSPICIOUS_COINS_THRESHOLD:
            uname = f"@{user.username}" if user.username else str(user_id)
            await _alert_admin(
                context,
                f"⚠️ <b>Подозрительная активность</b>\n"
                f"Пользователь {uname} (<code>{user_id}</code>) получил <b>{coins_earned} ⚡</b>\n"
                f"Номер: <code>{chars}</code>  Редкость: {rarity}"
            )
    except Exception as e:
        logger.error(f"[handle_nz] {e}", exc_info=True)

# ══════════════════════════════════════════════════════════════════════════════
#  ЛС — тех перерыв + webapp tracking
# ══════════════════════════════════════════════════════════════════════════════

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    user = msg.from_user
    if not user or is_admin(user):
        return
    if MAINTENANCE_MODE:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pm_notified WHERE user_id = %s", (user.id,))
                already = cur.fetchone()
        if not already:
            await msg.reply_text(MAINTENANCE_PM_TEXT)
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO pm_notified (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user.id,))
                conn.commit()

async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.web_app_data:
        return
    user = msg.from_user
    if not user:
        return
    # Засчитываем любой sendData из мини-аппа как "играл"
    ensure_user(user.id, user.username)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET played_webapp = TRUE WHERE user_id = %s",
                (user.id,)
            )
        conn.commit()
    logger.info(f"[webapp] user {user.id} opened miniapp, data: {msg.web_app_data.data}")

# ══════════════════════════════════════════════════════════════════════════════
#  Добавление в группу
# ══════════════════════════════════════════════════════════════════════════════

async def handle_add_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    bot_id = (await context.bot.get_me()).id
    for member in (msg.new_chat_members or []):
        if member.id == bot_id:
            chat = update.effective_chat
            register_chat(chat.id, chat.title or str(chat.id), getattr(chat, "username", None))
            await msg.reply_text(ADD_TO_CHAT_TEXT)
            break

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

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

    # ── ConversationHandler: рассылка ─────────────────────────────────────────
    broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_broadcast_start, pattern=r"^adm_broadcast$")],
        states={
            ADMIN_BC_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_bc_text)],
            ADMIN_BC_MEDIA: [
                MessageHandler(filters.PHOTO | filters.VIDEO | filters.ANIMATION, adm_bc_media),
                CallbackQueryHandler(adm_bc_skip_media, pattern=r"^bc_skip_media$"),
            ],
            ADMIN_BC_BTN_CHOICE: [
                CallbackQueryHandler(adm_bc_add_button,  pattern=r"^bc_add_button$"),
                CallbackQueryHandler(adm_bc_skip_button, pattern=r"^bc_skip_button$"),
                CallbackQueryHandler(adm_bc_confirm,     pattern=r"^bc_confirm$"),
                CallbackQueryHandler(adm_bc_cancel,      pattern=r"^bc_cancel_preview$"),
            ],
            ADMIN_BC_BTN_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_bc_btn_text)],
            ADMIN_BC_BTN_URL:  [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_bc_btn_url)],
        },
        fallbacks=[CallbackQueryHandler(adm_bc_cancel, pattern=r"^adm_back$")],
        per_message=False,
    )

    # ── ConversationHandler: действия с юзером ────────────────────────────────
    user_actions_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(usr_give_start,     pattern=r"^usr_give$"),
            CallbackQueryHandler(usr_take_start,     pattern=r"^usr_take$"),
            CallbackQueryHandler(usr_ban_start,      pattern=r"^usr_ban$"),
            CallbackQueryHandler(usr_unban_start,    pattern=r"^usr_unban$"),
            CallbackQueryHandler(usr_reset_cd_start, pattern=r"^usr_reset_cd$"),
            CallbackQueryHandler(usr_history_start,  pattern=r"^usr_history$"),
        ],
        states={
            ADMIN_USER_ACTION_ID:  [MessageHandler(filters.TEXT & ~filters.COMMAND, usr_action_id_received)],
            ADMIN_USER_GIVE_COINS: [MessageHandler(filters.TEXT & ~filters.COMMAND, usr_give_coins)],
        },
        fallbacks=[CallbackQueryHandler(adm_bc_cancel, pattern=r"^adm_back$")],
        per_message=False,
    )

    # ── ConversationHandler: X2 ───────────────────────────────────────────────
    x2_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_x2_start, pattern=r"^adm_x2$")],
        states={
            ADMIN_X2_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_x2_hours)],
        },
        fallbacks=[CallbackQueryHandler(adm_bc_cancel, pattern=r"^adm_back$")],
        per_message=False,
    )

    # ── Регистрация ────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("info",     cmd_info))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("admin",    cmd_admin))

    app.add_handler(broadcast_conv)
    app.add_handler(user_actions_conv)
    app.add_handler(x2_conv)

    app.add_handler(CallbackQueryHandler(adm_back,         pattern=r"^adm_back$"))
    app.add_handler(CallbackQueryHandler(adm_stats_full,   pattern=r"^adm_stats_full$"))
    app.add_handler(CallbackQueryHandler(adm_stats_days,   pattern=r"^adm_stats_days$"))
    app.add_handler(CallbackQueryHandler(adm_top_coins,    pattern=r"^adm_top_coins$"))
    app.add_handler(CallbackQueryHandler(adm_group_stats,  pattern=r"^adm_group_stats$"))
    app.add_handler(CallbackQueryHandler(adm_user_actions, pattern=r"^adm_user_actions$"))
    app.add_handler(CallbackQueryHandler(adm_maintenance,  pattern=r"^adm_maintenance$"))
    app.add_handler(CallbackQueryHandler(adm_change_cd,    pattern=r"^adm_change_cd$"))
    app.add_handler(CallbackQueryHandler(adm_set_cd,       pattern=r"^set_cd_"))
    app.add_handler(CallbackQueryHandler(add_to_chat_callback, pattern=r"^add_to_chat$"))
    app.add_handler(CallbackQueryHandler(settings_callback,    pattern=r"^set_country_"))

    group_only = filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND
    app.add_handler(MessageHandler(group_only, handle_nz))

    pm_only = filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND
    app.add_handler(MessageHandler(pm_only, handle_private_message))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
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
