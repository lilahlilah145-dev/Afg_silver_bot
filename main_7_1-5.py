import os
import sqlite3
import logging
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters,
)

# ============================================================
# AFG SILVER EXCHANGE
# Internal trading ledger. Binance transfers are verified manually by the admin.
# Requires: python-telegram-bot >= 20
# Environment: BOT_TOKEN, ADMIN_ID, DB_PATH (optional)
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
GROUP_ID = int(os.getenv("GROUP_ID", "0"))  # Optional Telegram group for anonymous bot announcements
DB_PATH = os.path.abspath(os.getenv("DB_PATH", "afg_silver.sqlite3"))

# Trading rules (admin can change selected values from Settings)
DEFAULTS = {
    "silver_price": "0",                 # USD per troy ounce
    "silver_price_updated": "",
    "margin_per_oz": "5.00",             # initial margin per ounce
    "warning_margin_per_oz": "2.50",     # first warning threshold
    "call_margin_per_oz": "1.00",        # margin call threshold
    "commission_per_oz": "0.05",         # charged immediately on opening and closing, per oz
    "admin_commission_balance": "0.00",  # lifetime commission credited to admin
    "leverage": "10",
    "auction_enabled": "0",              # admin-controlled; no auto-liquidation
    "last_auction": "",
    "deposit_instructions": "د Binance انتقال له اډمین سره همغږی کړئ؛ تر تایید مخکې بیلانس نه زیاتېږي.",
    "price_band": "0.50",
}

REGISTRATION = 1
TRADE_QTY = 2
ADMIN_TEXT = 3
REQUEST_AMOUNT = 4
ALIAS_STATE = 5

RULES = """
🥈 AFG SILVER EXCHANGE — د ګډون او معاملاتو اصول

۱. دا سیستم د سپینو زرو فزیکي پېر او پلور نه دی؛ یوازې مجازي/داخلي معاملې ثبتوي او ګټه، تاوان او بیلانس محاسبه کوي. هېڅ فزیکي سپین زر نه سپارل کېږي او نه لېږدول کېږي.
۲. یوازې د اډمین له تایید وروسته غړی معامله کولای شي.
۳. د سپینو زرو نرخ د اډمین له خوا په USD د هر Troy Ounce ثبتېږي.
۴. د هر اونس لومړنی مارجین $5 دی. د مارجین Warning حد $2.50/oz او Margin Call حد $1.00/oz دی.
۵. کمېشن د هر اونس لپاره $0.05 دی: د معاملې د پرانیستلو پر وخت هم سمدستي کمېږي او د تړلو پر وخت بیا هم سمدستي کمېږي. دواړه کمېشنونه د اډمین حساب ته جمع کېږي.
۶. په ګروپ کې د معاملې غوښتنه د «3خ40» یا «3ف40» په بڼه خپرېږي. بل تایید شوی غړی باید د هماغه پیغام Reply وکړي او «OK»، «اوکي»، «ب» یا یوازې مقدار ولیکي. د غوښتنې د منلو موده ۶۰ ثانیې ده.
۷. د ګروپ هر عادي پیغام/اعلان تر ۶۰ ثانیو وروسته حذف کېږي. د مستعار نوم لپاره یوازې د پښتو/عربي لیکدود توري منل کېږي؛ لاتیني/انګلیسي توري، شمېرې، تشې او نښې نه منل کېږي.
۸. هر ګډونوال خپله معامله په خپل انتخاب کوي او د نرخ، مارجین، ګټې او تاوان څارنه یې خپله مسؤلیت دی. اډمین د مشتریانو د معاملاتو مقابل لوری یا تضمین کوونکی نه دی او د هغوی په پېر/پلور کې برخه نه اخلي.
۹. د Margin Call، حراج یا تصفیې پر مهال د سیستم د ثبت شوو اصولو او نرخ له مخې معامله تړل کېدای شي. ګډونوال باید له پیل څخه خپلې معاملې وڅاري او د تصفیې له پایلې سره د معاملې د منل شوو اصولو له مخې حساب وکړي؛ د تاوان د بېرته ورکولو تضمین نشته.
۱۰. د Binance یا نورو پیسو حقیقي انتقال د ربات له لارې نه اجرا کېږي؛ که د چارج/ایستلو بهیر موجود وي، اډمین یې جلا تاییدوي.

د «اصول منم» تڼۍ په کېکاږلو سره تاییدوئ چې پورته اصول مو ولوستل او د مجازي معاملاتو څرنګوالی مو درک کړ.
"""

# Nicknames must contain only Arabic-script letters (Pashto/Dari/Arabic), no spaces or symbols.
def valid_nickname(value: str) -> bool:
    value = (value or "").strip()
    if not 3 <= len(value) <= 24:
        return False
    for ch in value:
        if unicodedata.category(ch)[0] != "L":
            return False
        if "ARABIC" not in unicodedata.name(ch, ""):
            return False
    return True

def credit_admin_commission(con, amount: float, note: str = "commission"):
    """Credit collected commission to admin account and keep a cumulative audit total."""
    amount = float(amount or 0)
    if amount <= 0:
        return
    row = None
    if ADMIN_ID:
        # Ensure commissions always land in the admin's account, even if /start
        # has not yet been pressed on this deployment.
        con.execute(
            "INSERT OR IGNORE INTO users(telegram_id,full_name,status,created_at) VALUES(?,?,?,?)",
            (ADMIN_ID, "Admin", "approved", now())
        )
        con.execute("INSERT OR IGNORE INTO accounts(telegram_id) VALUES(?)", (ADMIN_ID,))
        row = con.execute("SELECT telegram_id FROM accounts WHERE telegram_id=?", (ADMIN_ID,)).fetchone()
    if row:
        con.execute("UPDATE accounts SET balance=balance+? WHERE telegram_id=?", (amount, ADMIN_ID))
        con.execute(
            "INSERT INTO ledger(telegram_id,kind,amount,note,created_at) VALUES(?,?,?,?,?)",
            (ADMIN_ID, "commission_income", amount, note, now())
        )
    old = con.execute("SELECT value FROM settings WHERE key='admin_commission_balance'").fetchone()
    total = float(old["value"]) if old else 0.0
    con.execute(
        "INSERT INTO settings(key,value) VALUES('admin_commission_balance',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"{total + amount:.8f}",)
    )

# --------------------------- DATABASE ---------------------------

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            nickname TEXT,
            full_name TEXT NOT NULL,
            phone TEXT,
            country TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            accepted_at TEXT,
            created_at TEXT NOT NULL
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            telegram_id INTEGER PRIMARY KEY,
            balance REAL NOT NULL DEFAULT 0,
            reserved_margin REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('long','short')),
            ounces REAL NOT NULL,
            entry_price REAL NOT NULL,
            margin REAL NOT NULL,
            open_commission REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            exit_price REAL,
            realized_pnl REAL,
            close_commission REAL,
            FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            amount REAL NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS money_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('deposit','withdraw')),
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS group_permanent_messages (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            trade_request_id INTEGER,
            created_at TEXT NOT NULL,
            PRIMARY KEY(chat_id, message_id)
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS group_trade_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            requester_id INTEGER NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('long','short')),
            ounces REAL NOT NULL,
            entry_price REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            confirmer_id INTEGER,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(chat_id, message_id)
        )""")
        # Safe schema migrations: CREATE TABLE IF NOT EXISTS does not add columns
        request_columns = {row["name"] for row in con.execute("PRAGMA table_info(group_trade_requests)")}
        if "original_message_id" not in request_columns:
            con.execute("ALTER TABLE group_trade_requests ADD COLUMN original_message_id INTEGER")
        user_columns = {row["name"] for row in con.execute("PRAGMA table_info(users)")}
        if "nickname" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN nickname TEXT")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_nickname_nocase "
                    "ON users(nickname COLLATE NOCASE) WHERE nickname IS NOT NULL")
        # Remove legacy nicknames that do not satisfy the Arabic-script-only rule.
        for old_user in con.execute("SELECT telegram_id,nickname FROM users WHERE nickname IS NOT NULL").fetchall():
            if old_user["nickname"] != "ADMIN" and not valid_nickname(old_user["nickname"]):
                con.execute("UPDATE users SET nickname=NULL WHERE telegram_id=?", (old_user["telegram_id"],))
        # Safe schema migration: CREATE TABLE IF NOT EXISTS does not add columns
        # to an older accounts table. Never delete or recreate the user's database.
        account_columns = {row["name"] for row in con.execute("PRAGMA table_info(accounts)")}
        if "balance" not in account_columns:
            con.execute("ALTER TABLE accounts ADD COLUMN balance REAL NOT NULL DEFAULT 0")
        if "reserved_margin" not in account_columns:
            con.execute("ALTER TABLE accounts ADD COLUMN reserved_margin REAL NOT NULL DEFAULT 0")

        # Ensure every previously registered user has an account row.
        con.execute("INSERT OR IGNORE INTO accounts(telegram_id) SELECT telegram_id FROM users")

        for key, value in DEFAULTS.items():
            con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value))

def setting(key):
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULTS.get(key, "")

def set_setting(key, value):
    with db() as con:
        con.execute("""
        INSERT INTO settings(key,value) VALUES(?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, str(value)))

def get_user(uid):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()

def is_admin(uid):
    return ADMIN_ID != 0 and uid == ADMIN_ID

def approved(uid):
    row = get_user(uid)
    return bool(row and row["status"] == "approved")

def fmt(value, digits=2):
    return f"{float(value):,.{digits}f}"

def positive_number(raw):
    try:
        value = Decimal(str(raw).strip().replace(",", "").replace("$", ""))
        if not value.is_finite() or value <= 0:
            return None
        return float(value)
    except (InvalidOperation, ValueError, TypeError):
        return None

def current_price():
    try:
        return float(setting("silver_price") or 0)
    except ValueError:
        return 0.0

def user_account(uid):
    with db() as con:
        return con.execute("SELECT * FROM accounts WHERE telegram_id=?", (uid,)).fetchone()

def ensure_account(uid):
    with db() as con:
        con.execute("INSERT OR IGNORE INTO accounts(telegram_id) VALUES(?)", (uid,))

def open_positions(uid=None):
    with db() as con:
        if uid is None:
            return con.execute("""
                SELECT p.*,u.full_name FROM positions p
                JOIN users u ON u.telegram_id=p.telegram_id
                WHERE p.status='open' ORDER BY p.id
            """).fetchall()
        return con.execute("""
            SELECT * FROM positions WHERE telegram_id=? AND status='open'
            ORDER BY id DESC
        """, (uid,)).fetchall()

def unrealized(position, price):
    diff = price - float(position["entry_price"])
    if position["side"] == "short":
        diff = -diff
    return diff * float(position["ounces"])

def margin_per_oz(uid, price=None):
    price = current_price() if price is None else price
    positions = open_positions(uid)
    total_oz = sum(float(p["ounces"]) for p in positions)
    if total_oz <= 0:
        return None
    equity_in_positions = sum(
        float(p["margin"]) + unrealized(p, price) - float(p["open_commission"] or 0)
        for p in positions
    )
    return equity_in_positions / total_oz

async def send_margin_alerts(context: ContextTypes.DEFAULT_TYPE):
    price = current_price()
    if price <= 0:
        return
    for p in open_positions():
        uid = int(p["telegram_id"])
        per_oz = margin_per_oz(uid, price)
        if per_oz is None:
            continue
        # Avoid flooding: one alert of each level per position until it recovers.
        if per_oz <= float(setting("call_margin_per_oz")):
            level = "call"
            message = (
                "🚨 MARGIN CALL\n"
                f"ستاسو د مارجین پاتې ارزښت: ${fmt(per_oz)}/oz\n"
                "خپل خطر وارزوئ؛ اډمین کولای شي د اصولو له مخې معامله تصفیه کړي."
            )
        elif per_oz <= float(setting("warning_margin_per_oz")):
            level = "warning"
            message = (
                "⚠️ MARGIN WARNING\n"
                f"ستاسو د مارجین پاتې ارزښت: ${fmt(per_oz)}/oz\n"
                "د تاوان خطر لوړ شوی دی."
            )
        else:
            continue
        key = f"alert_{level}_{p['id']}"
        if setting(key) != "sent":
            try:
                await context.bot.send_message(uid, message)
                set_setting(key, "sent")
            except Exception:
                logging.exception("Could not send margin alert to %s", uid)

# --------------------------- MENUS ---------------------------

def user_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Long / پېر", callback_data="trade:long"),
         InlineKeyboardButton("🔴 Short / پلور", callback_data="trade:short")],
        [InlineKeyboardButton("🥈 د سپینو زرو نرخ", callback_data="u:price"),
         InlineKeyboardButton("💵 زما حساب", callback_data="u:account")],
        [InlineKeyboardButton("📈 خلاصې معاملې", callback_data="u:positions"),
         InlineKeyboardButton("📊 ګټه او تاوان", callback_data="u:pnl")],
        [InlineKeyboardButton("📜 د معاملاتو تاریخچه", callback_data="u:history")],
        [InlineKeyboardButton("📥 حساب چارج", callback_data="money:deposit"),
         InlineKeyboardButton("📤 پیسې ایستل", callback_data="money:withdraw")],
        [InlineKeyboardButton("👤 زما معلومات", callback_data="u:profile"),
         InlineKeyboardButton("📞 اړیکه", callback_data="u:contact")],
        [InlineKeyboardButton("🎭 مستعار نوم ټاکل/بدلول", callback_data="u:alias")],
        [InlineKeyboardButton("📜 اصول", callback_data="u:rules")],
    ])

def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 د ثبت‌نام غوښتنې", callback_data="a:pending"),
         InlineKeyboardButton("👥 غړي", callback_data="a:members")],
        [InlineKeyboardButton("💲 د XAG/USD نرخ", callback_data="a:price"),
         InlineKeyboardButton("💳 بیلانس تنظیمول", callback_data="a:balance")],
        [InlineKeyboardButton("🔍 د مستعار نوم له لارې غړی پیدا کول", callback_data="a:lookup")],
        [InlineKeyboardButton("🎁 خپلوانو ته وړیا کریډیټ", callback_data="a:freecredit")],
        [InlineKeyboardButton("📊 ټول راپور", callback_data="a:report"),
         InlineKeyboardButton("📈 خلاصې معاملې", callback_data="a:positions")],
        [InlineKeyboardButton("📥 د چارج غوښتنې", callback_data="a:deposits"),
         InlineKeyboardButton("📤 د ایستلو غوښتنې", callback_data="a:withdrawals")],
        [InlineKeyboardButton("⚙️ د مارجین/کمېشن تنظیمات", callback_data="a:settings")],
        [InlineKeyboardButton("🔨 د حراج/تصفیې اجرا", callback_data="a:auction")],
        [InlineKeyboardButton("🟢 زما Long", callback_data="trade:long"),
         InlineKeyboardButton("🔴 زما Short", callback_data="trade:short")],
        [InlineKeyboardButton("🎭 زما مستعار نوم", callback_data="u:alias")],
        [InlineKeyboardButton("🔄 تازه کول", callback_data="a:home")],
    ])

def home_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🏠 مینو"), KeyboardButton("📜 اصول")]],
        resize_keyboard=True, is_persistent=True
    )

def rules_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ اصول منم", callback_data="accept")],
        [InlineKeyboardButton("❌ نه منم", callback_data="decline")],
    ])

# --------------------------- START / REGISTRATION ---------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    async def start_reply(text, **kwargs):
        response = await update.effective_message.reply_text(text, **kwargs)
        if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
            context.application.create_task(
                _delete_group_echo_after_60(context, update.effective_chat.id, response.message_id)
            )
        return response
    if is_admin(uid):
        # Give the administrator a private ledger account so the admin can trade too.
        tg_user = update.effective_user
        with db() as con:
            con.execute("""INSERT OR IGNORE INTO users
                (telegram_id,username,full_name,nickname,status,created_at)
                VALUES(?,?,?,?, 'approved', ?)""",
                (uid, tg_user.username, tg_user.full_name or "Admin", "ADMIN", now()))
            con.execute("UPDATE users SET status='approved' WHERE telegram_id=?", (uid,))
            con.execute("INSERT OR IGNORE INTO accounts(telegram_id) VALUES(?)", (uid,))
        await start_reply(
            "👑 AFG SILVER EXCHANGE — اډمین پینل\n"
            "یادونه: ربات داخلي حسابداري او معاملې ثبتوي؛ د پیسو انتقال اډمین جلا تاییدوي.",
            reply_markup=admin_menu()
        )
        return
    row = get_user(uid)
    if row and row["status"] == "approved":
        await start_reply(
            "ښه راغلاست! ستاسو حساب فعال دی.",
            reply_markup=user_menu()
        )
    elif row and row["status"] == "pending":
        await start_reply(
            "ستاسو ثبت‌نام د اډمین تایید ته منتظر دی.", reply_markup=home_keyboard()
        )
    else:
        await start_reply(
            "🥈 AFG SILVER EXCHANGE ته ښه راغلاست!\n"
            "د ګډون اصول ولولئ او ومنئ.",
            reply_markup=home_keyboard()
        )
        await start_reply(RULES, reply_markup=rules_keyboard())

async def accept_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["accepted_at"] = now()
    await q.message.reply_text("خپل بشپړ نوم ولیکئ:", reply_markup=ReplyKeyboardRemove())
    return REGISTRATION

async def decline_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("د اصولو له منلو پرته ثبت‌نام ممکن نه دی.", reply_markup=home_keyboard())
    return ConversationHandler.END

async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    if len(name) < 3 or len(name) > 100:
        await update.message.reply_text("سم نوم ولیکئ (۳ تر ۱۰۰ تورو).")
        return REGISTRATION
    context.user_data["reg_name"] = name
    await update.message.reply_text(
        "اوس خپل مستعار نوم (Nickname) وټاکئ. همدا نوم به په ګروپ کې ښکاري؛ اصلي نوم او هویت به یوازې اډمین ویني.\n"
        "۳ تر ۲۴ پورې یوازې د پښتو/عربي توري ولیکئ؛ انګلیسي توري، شمېرې، تشې او نښې مه کاروئ:")
    context.user_data["waiting_nickname"] = True
    return REGISTRATION

async def register_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("waiting_country"):
        return await registration_text(update, context)
    if not context.user_data.get("reg_name"):
        return await register_name(update, context)
    nickname = (update.message.text or "").strip()
    if not valid_nickname(nickname):
        await update.message.reply_text("مستعار نوم باید ۳ تر ۲۴ پورې یوازې د پښتو/عربي توري ولري؛ انګلیسي توري، شمېرې، تشې او نښې نه منل کېږي.")
        return REGISTRATION
    with db() as con:
        exists = con.execute("SELECT 1 FROM users WHERE nickname=? COLLATE NOCASE", (nickname,)).fetchone()
    if exists:
        await update.message.reply_text("دا مستعار نوم مخکې نیول شوی؛ بل نوم وټاکئ.")
        return REGISTRATION
    context.user_data["reg_nickname"] = nickname
    context.user_data.pop("waiting_nickname", None)
    button = KeyboardButton("📱 شمېره شریکول", request_contact=True)
    await update.message.reply_text(
        "د لاندې تڼۍ له لارې خپله شمېره شریکه کړئ:",
        reply_markup=ReplyKeyboardMarkup([[button]], resize_keyboard=True, one_time_keyboard=True)
    )
    return REGISTRATION

async def register_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact
    if not contact or (contact.user_id and contact.user_id != update.effective_user.id):
        await update.message.reply_text("مهرباني وکړئ خپل شخصي تماس شریک کړئ.")
        return REGISTRATION
    context.user_data["reg_phone"] = contact.phone_number
    await update.message.reply_text("د اوسېدو هېواد ولیکئ:", reply_markup=ReplyKeyboardRemove())
    context.user_data["waiting_country"] = True
    return REGISTRATION

async def registration_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("waiting_country"):
        country = (update.message.text or "").strip()
        if len(country) < 2 or len(country) > 80:
            await update.message.reply_text("د هېواد نوم سم ولیکئ.")
            return REGISTRATION
        user = update.effective_user
        d = context.user_data
        with db() as con:
            con.execute("""
            INSERT INTO users(telegram_id,username,nickname,full_name,phone,country,status,accepted_at,created_at)
            VALUES(?,?,?,?,?,?,'pending',?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET
              username=excluded.username,nickname=excluded.nickname,full_name=excluded.full_name,
              phone=excluded.phone,country=excluded.country,status='pending',
              accepted_at=excluded.accepted_at
            """, (user.id, user.username, d["reg_nickname"], d["reg_name"], d["reg_phone"],
                  country, d.get("accepted_at", now()), now()))
            con.execute("INSERT OR IGNORE INTO accounts(telegram_id) VALUES(?)", (user.id,))
        await update.message.reply_text(
            "✅ ثبت‌نام مو ثبت شو. د اډمین تایید ته انتظار وباسئ.",
            reply_markup=home_keyboard()
        )
        if ADMIN_ID:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ تایید", callback_data=f"approve:{user.id}"),
                InlineKeyboardButton("❌ رد", callback_data=f"reject:{user.id}")
            ]])
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"📝 نوې غوښتنه\nاصلي نوم: {d['reg_name']}\nمستعار نوم: {d['reg_nickname']}\nشمېره: {d['reg_phone']}\n"
                    f"هېواد: {country}\nTelegram ID: {user.id}",
                    reply_markup=kb
                )
            except Exception:
                logging.exception("Admin notification failed")
        context.user_data.clear()
        return ConversationHandler.END
    return await register_name(update, context)

# --------------------------- ADMIN APPROVAL ---------------------------

async def admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("دا واک یوازې د اډمین دی.")
        return
    action, raw_id = q.data.split(":")
    uid = int(raw_id)
    status = "approved" if action == "approve" else "rejected"
    with db() as con:
        cur = con.execute("UPDATE users SET status=? WHERE telegram_id=?", (status, uid))
        if status == "approved":
            con.execute("INSERT OR IGNORE INTO accounts(telegram_id) VALUES(?)", (uid,))
    if cur.rowcount:
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(f"د غړي حالت بدل شو: {status}")
        try:
            await context.bot.send_message(
                uid,
                "✅ ستاسو ثبت‌نام تایید شو." if status == "approved"
                else "❌ ستاسو غوښتنه ونه منل شوه."
            )
        except Exception:
            logging.exception("User approval notification failed")
    else:
        await q.message.reply_text("غړی ونه موندل شو.")

# --------------------------- PRIVATE ALIAS SETUP ---------------------------

async def alias_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start alias setup from /alias or the menu; no account re-registration needed."""
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        uid = q.from_user.id
        message = q.message
    else:
        uid = update.effective_user.id
        message = update.effective_message

    if message.chat.type != "private":
        await message.reply_text("د محرمیت لپاره مستعار نوم یوازې په شخصي چټ کې وټاکئ: /alias")
        return ConversationHandler.END

    if is_admin(uid) and not get_user(uid):
        tg_user = update.effective_user
        with db() as con:
            con.execute(
                "INSERT OR IGNORE INTO users(telegram_id,username,full_name,status,created_at) VALUES(?,?,?,'approved',?)",
                (uid, tg_user.username, tg_user.full_name or "Admin", now()),
            )
            con.execute("INSERT OR IGNORE INTO accounts(telegram_id) VALUES(?)", (uid,))

    user = get_user(uid)
    if not user:
        await message.reply_text("ستاسو حساب لا نه دی ثبت شوی. لومړی /start وکړئ؛ بیا /alias وکاروئ.")
        return ConversationHandler.END

    current = user["nickname"] or "تر اوسه نه دی ټاکل شوی"
    await message.reply_text(
        f"اوسنی مستعار نوم: {current}\n\n"
        "نوی مستعار نوم ولیکئ (۳ تر ۲۴ تورو). دا نوم به د ربات په ګروپي اعلانونو کې ښکاري.\n"
        "دا کار ثبت‌نام، بیلانس او معاملې نه بدلوي."
    )
    return ALIAS_STATE

async def alias_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    nickname = (update.message.text or "").strip()
    if not valid_nickname(nickname):
        await update.message.reply_text("مستعار نوم باید ۳ تر ۲۴ پورې یوازې د پښتو/عربي توري ولري؛ انګلیسي توري، شمېرې، تشې او نښې نه منل کېږي. بیا یې ولیکئ.")
        return ALIAS_STATE
    try:
        with db() as con:
            con.execute("UPDATE users SET nickname=? WHERE telegram_id=?", (nickname, uid))
    except sqlite3.IntegrityError:
        await update.message.reply_text("دا نوم بل غړي نیولی دی؛ بل ځانګړی نوم وټاکئ.")
        return ALIAS_STATE

    await update.message.reply_text(
        f"✅ مستعار نوم مو ثبت شو: {nickname}\n"
        "له دې وروسته به د ربات په ګروپي معاملاتي اعلانونو کې همدا نوم ښکاري."
    )
    return ConversationHandler.END

# --------------------------- USER VIEWS ---------------------------

async def show_account(chat, uid):
    a = user_account(uid)
    positions = open_positions(uid)
    price = current_price()
    floating = sum(unrealized(p, price) for p in positions) if price else 0
    total_margin = sum(float(p["margin"]) for p in positions)
    await chat.reply_text(
        "💵 ستاسو حساب\n\n"
        f"ازاد بیلانس (له مارجین او فیس وروسته): ${fmt(a['balance'])}\n"
        f"مصروف مارجین: ${fmt(a['reserved_margin'])}\n"
        f"د خلاصو معاملاتو مارجین: ${fmt(total_margin)}\n"
        f"ناخالصه ګټه/تاوان: ${fmt(floating)}\n"
        f"د حساب خالص ارزښت (Equity): ${fmt(float(a['balance']) + float(a['reserved_margin']) + floating)}\n"
        "بیلانس یوازې د اډمین له تایید وروسته د Binance انتقال استازیتوب کوي."
    )

async def user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not approved(uid):
        await q.message.reply_text("دا برخه یوازې د تایید شویو غړو لپاره ده.")
        return
    action = q.data.split(":")[1]
    if action == "price":
        price = current_price()
        await q.message.reply_text(
            f"🥈 XAG/USD: ${fmt(price, 3)} / Troy Ounce\n"
            f"د تازه کېدو وخت: {setting('silver_price_updated') or 'لا نه دی ثبت شوی'}\n"
            "نرخ د اډمین له خوا لاسي ثبتېږي؛ Live market feed نه دی."
            if price > 0 else "اډمین لا نرخ نه دی ثبت کړی."
        )
    elif action == "account":
        await show_account(q.message, uid)
    elif action == "positions":
        rows = open_positions(uid)
        if not rows:
            await q.message.reply_text("تاسو خلاصه معامله نه لرئ.")
        else:
            price = current_price()
            for p in rows[:20]:
                pnl = unrealized(p, price) if price else 0
                side = "LONG" if p["side"] == "long" else "SHORT"
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(f"🔒 معامله #{p['id']} بنده کړه", callback_data=f"close:{p['id']}")
                ]])
                await q.message.reply_text(
                    f"#{p['id']} {side}\nاندازه: {fmt(p['ounces'],4)} oz\n"
                    f"Entry: ${fmt(p['entry_price'],3)}\n"
                    f"اوسنی P/L: ${fmt(pnl)}\nمارجین: ${fmt(p['margin'])}",
                    reply_markup=kb
                )
    elif action == "pnl":
        price = current_price()
        rows = open_positions(uid)
        floating = sum(unrealized(p, price) for p in rows) if price else 0
        with db() as con:
            realized = con.execute("""
                SELECT COALESCE(SUM(realized_pnl-close_commission-open_commission),0) n
                FROM positions WHERE telegram_id=? AND status='closed'
            """, (uid,)).fetchone()["n"]
        await q.message.reply_text(
            f"📊 د ګټې او تاوان راپور\n\n"
            f"Realized P/L: ${fmt(realized)}\n"
            f"Unrealized P/L: ${fmt(floating)}\n"
            f"ټول اټکلی P/L: ${fmt(float(realized)+floating)}"
        )
    elif action == "history":
        with db() as con:
            rows = con.execute("""
                SELECT * FROM positions WHERE telegram_id=? ORDER BY id DESC LIMIT 15
            """, (uid,)).fetchall()
        if not rows:
            await q.message.reply_text("تراوسه معامله نه لرئ.")
        else:
            lines = ["📜 وروستۍ معاملې:"]
            for p in rows:
                side = "LONG" if p["side"] == "long" else "SHORT"
                result = "خلاصه" if p["status"] == "open" else f"P/L ${fmt(p['realized_pnl'] or 0)}"
                lines.append(f"#{p['id']} {side} {fmt(p['ounces'],4)} oz | {result}")
            await q.message.reply_text("\n".join(lines))
    elif action == "profile":
        u = get_user(uid)
        await q.message.reply_text(
            f"👤 نوم: {u['full_name']}\nشمېره: {u['phone']}\n"
            f"هېواد: {u['country']}\nحالت: {u['status']}"
        )
    elif action == "contact":
        await q.message.reply_text(f"د اډمین Telegram ID: {ADMIN_ID or 'نه دی ټاکل شوی'}")
    elif action == "rules":
        await q.message.reply_text(RULES)

# --------------------------- OPEN / CLOSE TRADES ---------------------------

async def begin_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not (approved(uid) or is_admin(uid)):
        await q.message.reply_text("معامله یوازې د تایید شویو غړو لپاره ده.")
        return ConversationHandler.END
    if is_admin(uid):
        ensure_account(uid)
    price = current_price()
    if price <= 0:
        await q.message.reply_text("اډمین لا د سپینو زرو نرخ نه دی ثبت کړی.")
        return ConversationHandler.END
    side = q.data.split(":")[1]
    context.user_data["trade_side"] = side
    context.user_data["trade_price"] = price
    margin = float(setting("margin_per_oz"))
    commission = float(setting("commission_per_oz"))
    await q.message.reply_text(
        f"د {'LONG' if side == 'long' else 'SHORT'} معامله\n"
        f"اوسنی ثبت شوی نرخ: ${fmt(price,3)}/oz\n"
        f"د معاملې د نرخ حدود: ${fmt(price-0.50,3)} – ${fmt(price+0.50,3)}\n"
        f"مارجین: ${fmt(margin)}/oz | کمېشن: ${fmt(commission)}/oz\n"
        "د سپینو زرو اندازه په Troy Ounce ولیکئ.\n"
        "د لغوه کولو لپاره /cancel ولیکئ."
    )
    return TRADE_QTY

async def execute_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    qty = positive_number(update.message.text)
    if qty is None or qty > 100000:
        await update.message.reply_text("سم مثبت مقدار په اونس ولیکئ.")
        return TRADE_QTY
    uid = update.effective_user.id
    side = context.user_data.get("trade_side")
    price = float(context.user_data.get("trade_price", 0))
    latest = current_price()
    if latest <= 0 or abs(latest - price) > 0.000001:
        context.user_data.pop("trade_side", None)
        context.user_data.pop("trade_price", None)
        await update.message.reply_text("مظنه بدله شوې ده. مهرباني وکړئ د نوې مظنې له مخې معامله له سره پیل کړئ.", reply_markup=admin_menu() if is_admin(uid) else user_menu())
        return ConversationHandler.END
    margin = float(setting("margin_per_oz")) * qty
    commission = float(setting("commission_per_oz")) * qty
    required = margin + commission
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        a = con.execute("SELECT * FROM accounts WHERE telegram_id=?", (uid,)).fetchone()
        # The $5/oz margin alone determines trade capacity; the $0.05/oz fee
        # is charged immediately in addition and is NOT included in ounce capacity.
        if not a or float(a["balance"]) < margin:
            available = float(a["balance"]) if a else 0
            con.rollback()
            await update.message.reply_text(
                f"بیلانس د مارجین لپاره کافي نه دی.\nاړین مارجین: ${fmt(margin)}\nشته بیلانس: ${fmt(available)}\nکمېشن جلا ${fmt(commission)} دی.",
                reply_markup=admin_menu() if is_admin(uid) else user_menu()
            )
            return ConversationHandler.END
        equity_after_open = float(a["balance"]) - commission
        con.execute("""
            UPDATE accounts SET balance=balance-?,reserved_margin=reserved_margin+?
            WHERE telegram_id=?
        """, (required, margin, uid))
        con.execute("""
            INSERT INTO positions(telegram_id,side,ounces,entry_price,margin,open_commission,opened_at)
            VALUES(?,?,?,?,?,?,?)
        """, (uid, side, qty, price, margin, commission, now()))
        con.execute("""
            INSERT INTO ledger(telegram_id,kind,amount,note,created_at)
            VALUES(?,?,?,?,?)
        """, (uid, "open_trade", -required, f"{side} {qty} oz; opening fee={commission}", now()))
        credit_admin_commission(con, commission, f"opening commission from {uid}: {qty} oz")
        con.commit()
    context.user_data.pop("trade_side", None)
    context.user_data.pop("trade_price", None)
    # Public group announcement contains ONLY the member's chosen nickname.
    if GROUP_ID:
        try:
            with db() as con:
                public_user = con.execute(
                    "SELECT nickname FROM users WHERE telegram_id=?", (uid,)
                ).fetchone()
            public_name = (public_user["nickname"] if public_user and public_user["nickname"]
                           else ("ADMIN" if is_admin(uid) else "غړی"))
            group_post = await context.bot.send_message(
                GROUP_ID,
                f"📢 نوې معامله\n"
                f"👤 مستعار نوم: {public_name}\n"
                f"{'🟢 LONG' if side == 'long' else '🔴 SHORT'}\n"
                f"مقدار: {fmt(qty,4)} oz\n"
                f"د پرانیستلو نرخ: ${fmt(price,3)} / oz"
            )
            context.application.create_task(
                _delete_group_echo_after_60(context, GROUP_ID, group_post.message_id)
            )
        except Exception:
            logging.exception("Anonymous group trade announcement failed")
    await update.message.reply_text(
        f"✅ {side.upper()} معامله پرانیستل شوه.\n"
        f"مقدار: {fmt(qty,4)} oz\nEntry: ${fmt(price,3)}\n"
        f"مارجین: ${fmt(margin)}\nد پرانیستلو کمېشن (سمدستي): ${fmt(commission)}\n"
        f"د کمېشن وروسته د حساب خالص ارزښت: ${fmt(equity_after_open)}\n"
        "کمېشن د اډمین حساب ته جمع شو. معامله یوازې مجازي/داخلي ثبت ده؛ فزیکي سپین زر نه لېږدول کېږي.",
        reply_markup=admin_menu() if is_admin(uid) else user_menu()
    )
    return ConversationHandler.END

async def close_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not approved(uid):
        await q.message.reply_text("حساب فعال نه دی.")
        return
    try:
        pid = int(q.data.split(":")[1])
    except ValueError:
        return
    price = current_price()
    if price <= 0:
        await q.message.reply_text("د تړلو لپاره نرخ ثبت شوی نه دی.")
        return
    with db() as con:
        p = con.execute("""
            SELECT * FROM positions WHERE id=? AND telegram_id=? AND status='open'
        """, (pid, uid)).fetchone()
        if not p:
            await q.message.reply_text("دا خلاصه معامله ونه موندل شوه.")
            return
        pnl = unrealized(p, price)
        close_fee = float(setting("commission_per_oz")) * float(p["ounces"])
        release = float(p["margin"]) + pnl - close_fee
        # Negative settlement is capped at zero in this demo; no extra debt is created.
        credit = max(0.0, release)
        con.execute("""
            UPDATE accounts SET balance=balance+?,reserved_margin=MAX(0,reserved_margin-?)
            WHERE telegram_id=?
        """, (credit, float(p["margin"]), uid))
        con.execute("""
            UPDATE positions SET status='closed',closed_at=?,exit_price=?,realized_pnl=?,
                close_commission=? WHERE id=?
        """, (now(), price, pnl, close_fee, pid))
        con.execute("""
            INSERT INTO ledger(telegram_id,kind,amount,note,created_at)
            VALUES(?,?,?,?,?)
        """, (uid, "close_trade", credit, f"position #{pid}; pnl={pnl}", now()))
        credit_admin_commission(con, close_fee, f"closing commission from {uid}, position #{pid}")
    updated_account = user_account(uid)
    await q.message.reply_text(
        f"🔒 معامله #{pid} بنده شوه.\nExit: ${fmt(price,3)}\n"
        f"Realized P/L: ${fmt(pnl)}\nد تړلو کمېشن (سمدستي): ${fmt(close_fee)}\n"
        f"کمېشن د اډمین حساب ته جمع شو. حساب ته بېرته ورګرځېد: ${fmt(credit)}\n"
        f"نوی ازاد بیلانس: ${fmt(updated_account['balance'])}"
    )
    await send_margin_alerts(context)

# --------------------------- DEPOSIT / WITHDRAW REQUESTS ---------------------------

async def money_request_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not approved(q.from_user.id):
        await q.message.reply_text("حساب مو لا تایید شوی نه دی.")
        return ConversationHandler.END
    kind = q.data.split(":")[1]
    context.user_data["money_request_kind"] = kind
    await q.message.reply_text(
        f"د {'چارج' if kind == 'deposit' else 'ایستلو'} اندازه په USD ولیکئ:"
    )
    return REQUEST_AMOUNT

async def money_request_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = positive_number(update.message.text)
    if amount is None or amount > 1000000:
        await update.message.reply_text("سم مثبت مبلغ ولیکئ.")
        return REQUEST_AMOUNT
    uid = update.effective_user.id
    kind = context.user_data.get("money_request_kind")
    with db() as con:
        if kind == "withdraw":
            a = con.execute("SELECT balance FROM accounts WHERE telegram_id=?", (uid,)).fetchone()
            if not a or float(a["balance"]) < amount:
                await update.message.reply_text("ستاسو ازاد Demo بیلانس د دې غوښتنې لپاره کافي نه دی.")
                return ConversationHandler.END
        cur = con.execute("""
            INSERT INTO money_requests(telegram_id,kind,amount,created_at)
            VALUES(?,?,?,?)
        """, (uid, kind, amount, now()))
        rid = cur.lastrowid
    await update.message.reply_text(
        f"✅ د پیسو غوښتنه ثبت شوه. شمېره: #{rid}\n"
        "د اډمین تر پرېکړې پورې انتظار وباسئ.",
        reply_markup=user_menu()
    )
    if ADMIN_ID:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ تایید", callback_data=f"moneyapprove:{rid}"),
            InlineKeyboardButton("❌ رد", callback_data=f"moneyreject:{rid}")
        ]])
        await context.bot.send_message(
            ADMIN_ID, f"د پیسو غوښتنه #{rid}\nغړی: {uid}\nډول: {kind}\nمبلغ: ${fmt(amount)}",
            reply_markup=kb
        )
    context.user_data.pop("money_request_kind", None)
    return ConversationHandler.END

async def review_money_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("یوازې اډمین.")
        return
    action, raw = q.data.split(":")
    rid = int(raw)
    with db() as con:
        req = con.execute("SELECT * FROM money_requests WHERE id=?", (rid,)).fetchone()
        if not req or req["status"] != "pending":
            await q.message.reply_text("غوښتنه نه شته یا مخکې ارزول شوې ده.")
            return
        approved_req = action == "moneyapprove"
        uid, amount, kind = int(req["telegram_id"]), float(req["amount"]), req["kind"]
        if approved_req:
            if kind == "deposit":
                con.execute("UPDATE accounts SET balance=balance+? WHERE telegram_id=?", (amount, uid))
                ledger_amount = amount
            else:
                a = con.execute("SELECT balance FROM accounts WHERE telegram_id=?", (uid,)).fetchone()
                if not a or float(a["balance"]) < amount:
                    await q.message.reply_text("د غړي اوسنی بیلانس د ایستلو لپاره کافي نه دی؛ غوښتنه رد کړئ.")
                    return
                con.execute("UPDATE accounts SET balance=balance-? WHERE telegram_id=?", (amount, uid))
                ledger_amount = -amount
            con.execute("""
                INSERT INTO ledger(telegram_id,kind,amount,note,created_at)
                VALUES(?,?,?,?,?)
            """, (uid, kind, ledger_amount, f"request #{rid}", now()))
            status = "approved"
        else:
            status = "rejected"
        con.execute("UPDATE money_requests SET status=?,reviewed_at=? WHERE id=?", (status, now(), rid))
    await q.edit_message_reply_markup(reply_markup=None)
    await q.message.reply_text(f"غوښتنه #{rid}: {status}")
    try:
        await context.bot.send_message(uid, f"ستاسو د پیسو غوښتنه #{rid}: {status}")
    except Exception:
        logging.exception("Could not notify money request user")

# --------------------------- ADMIN PANEL ---------------------------

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("دا برخه یوازې د اډمین لپاره ده.")
        return
    action = q.data.split(":")[1]
    if action == "memberbalance":
        parts = q.data.split(":")
        if len(parts) != 3 or not parts[2].isdigit():
            await q.message.reply_text("د غړي پېژندنه ناسمه ده.")
            return
        target = int(parts[2])
        if not get_user(target):
            await q.message.reply_text("دا غړی نور په ډیټابېس کې نه موندل کېږي.")
            return
        context.user_data["balance_target"] = target
        context.user_data["admin_action"] = "balance_amount"
        await q.message.reply_text(
            "د دې غړي نوی ازاد بیلانس په USD ولیکئ. مصروف مارجین به نه بدلېږي؛ یوازې ازاد بیلانس ټاکل کېږي."
        )
    elif action == "lookup":
        context.user_data["admin_action"] = "member_lookup"
        await q.message.reply_text("د غړي ځانګړی مستعار نوم ولیکئ:")
    elif action == "home":
        await q.message.reply_text("👑 اډمین پینل", reply_markup=admin_menu())
    elif action == "pending":
        with db() as con:
            rows = con.execute("SELECT * FROM users WHERE status='pending' ORDER BY created_at LIMIT 30").fetchall()
        if not rows:
            await q.message.reply_text("د تایید غوښتنه نشته.")
        for u in rows:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ تایید", callback_data=f"approve:{u['telegram_id']}"),
                InlineKeyboardButton("❌ رد", callback_data=f"reject:{u['telegram_id']}")
            ]])
            await q.message.reply_text(
                f"{u['full_name']}\nشمېره: {u['phone']}\nهېواد: {u['country']}\nID: {u['telegram_id']}",
                reply_markup=kb
            )
    elif action == "members":
        with db() as con:
            rows = con.execute("""
                SELECT u.telegram_id,u.full_name,u.status,a.balance,a.reserved_margin
                FROM users u LEFT JOIN accounts a USING(telegram_id)
                ORDER BY u.created_at DESC LIMIT 50
            """).fetchall()
        lines = ["👥 غړي:"]
        for u in rows:
            lines.append(f"{u['full_name']} | {u['telegram_id']} | {u['status']} | ${fmt(u['balance'] or 0)}")
        await q.message.reply_text("\n".join(lines)[:4000] if rows else "غړي نشته.")
    elif action == "price":
        context.user_data["admin_action"] = "price"
        await q.message.reply_text("نوی XAG/USD نرخ په USD د هر Troy Ounce ولیکئ:")
    elif action == "balance":
        context.user_data["admin_action"] = "balance_id"
        await q.message.reply_text("د غړي Telegram ID ولیکئ:")
    elif action == "freecredit":
        context.user_data["admin_action"] = "freecredit_id"
        await q.message.reply_text("د خپلوان Telegram ID ولیکئ؛ باید مخکې ثبت شوی وي:")
    elif action == "report":
        with db() as con:
            users = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
            approved_n = con.execute("SELECT COUNT(*) n FROM users WHERE status='approved'").fetchone()["n"]
            pending = con.execute("SELECT COUNT(*) n FROM users WHERE status='pending'").fetchone()["n"]
            positions = con.execute("SELECT COUNT(*) n FROM positions WHERE status='open'").fetchone()["n"]
            volume = con.execute("SELECT COALESCE(SUM(ounces),0) n FROM positions").fetchone()["n"]
            reqs = con.execute("SELECT COUNT(*) n FROM money_requests WHERE status='pending'").fetchone()["n"]
            bal = con.execute("SELECT COALESCE(SUM(balance),0) n FROM accounts").fetchone()["n"]
        await q.message.reply_text(
            f"📊 د سیستم راپور\nټول غړي: {users}\nتایید شوي: {approved_n}\n"
            f"انتظار: {pending}\nخلاصې معاملې: {positions}\n"
            f"د ټولو ثبت شویو معاملاتو حجم: {fmt(volume,4)} oz\n"
            f"د غړو ازاد بیلانس: ${fmt(bal)}\n"
            f"د پیسو انتظار غوښتنې: {reqs}"
        )
    elif action == "positions":
        rows = open_positions()
        if not rows:
            await q.message.reply_text("خلاصې معاملې نشته.")
        else:
            price = current_price()
            for p in rows[:25]:
                pnl = unrealized(p, price) if price else 0
                await q.message.reply_text(
                    f"#{p['id']} | {p['full_name']} | {p['side'].upper()}\n"
                    f"{fmt(p['ounces'],4)} oz | Entry ${fmt(p['entry_price'],3)} | P/L ${fmt(pnl)}"
                )
    elif action in ("deposits", "withdrawals"):
        kind = "deposit" if action == "deposits" else "withdraw"
        with db() as con:
            rows = con.execute("""
                SELECT r.*,u.full_name FROM money_requests r JOIN users u USING(telegram_id)
                WHERE r.kind=? AND r.status='pending' ORDER BY r.id LIMIT 20
            """, (kind,)).fetchall()
        if not rows:
            await q.message.reply_text("انتظار غوښتنې نشته.")
        for r in rows:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ تایید", callback_data=f"moneyapprove:{r['id']}"),
                InlineKeyboardButton("❌ رد", callback_data=f"moneyreject:{r['id']}")
            ]])
            await q.message.reply_text(
                f"#{r['id']} | {r['full_name']} | ${fmt(r['amount'])}",
                reply_markup=kb
            )
    elif action == "settings":
        await q.message.reply_text(
            "⚙️ اوسني تنظیمات\n"
            f"Margin/oz: ${setting('margin_per_oz')}\n"
            f"Warning/oz: ${setting('warning_margin_per_oz')}\n"
            f"Margin Call/oz: ${setting('call_margin_per_oz')}\n"
            f"Commission/oz: ${setting('commission_per_oz')} هر اړخ\n"
            f"د اډمین ټول ثبت شوی کمېشن: ${setting('admin_commission_balance') or '0.00'}\n"
            f"Leverage ښودنیز: {setting('leverage')}x\n"
            f"د حراج حالت: {'فعال' if setting('auction_enabled') == '1' else 'غیرفعال'}\n\n"
            "د بدلون لپاره /setmargin، /setwarning، /setcall، /setfee، /setleverage، /setauction وکاروئ."
        )
    elif action == "auction":
        context.user_data["admin_action"] = "auction_confirm"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚠️ هو، ټولې خلاصې معاملې وتړه", callback_data="auction:confirm"),
            InlineKeyboardButton("لغوه", callback_data="auction:cancel")
        ]])
        await q.message.reply_text(
            "دا عمل به ټولې خلاصې معاملې د اوسني ثبت شوي نرخ له مخې وتړي. دوام ورکوئ؟",
            reply_markup=kb
        )

async def auction_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("یوازې اډمین.")
        return
    if q.data == "auction:cancel":
        await q.message.reply_text("حراج لغوه شو.")
        return
    price = current_price()
    if price <= 0:
        await q.message.reply_text("لومړی نرخ ثبت کړئ.")
        return
    rows = open_positions()
    closed = 0
    for p in rows:
        uid, pid = int(p["telegram_id"]), int(p["id"])
        with db() as con:
            fresh = con.execute("SELECT * FROM positions WHERE id=? AND status='open'", (pid,)).fetchone()
            if not fresh:
                continue
            pnl = unrealized(fresh, price)
            fee = float(setting("commission_per_oz")) * float(fresh["ounces"])
            credit = max(0.0, float(fresh["margin"]) + pnl - fee)
            con.execute("""
                UPDATE accounts SET balance=balance+?,reserved_margin=MAX(0,reserved_margin-?)
                WHERE telegram_id=?
            """, (credit, float(fresh["margin"]), uid))
            con.execute("""
                UPDATE positions SET status='closed',closed_at=?,exit_price=?,realized_pnl=?,
                close_commission=? WHERE id=?
            """, (now(), price, pnl, fee, pid))
            con.execute("""
                INSERT INTO ledger(telegram_id,kind,amount,note,created_at)
                VALUES(?,?,?,?,?)
            """, (uid, "auction_close", credit, f"position #{pid}", now()))
            credit_admin_commission(con, fee, f"auction closing commission from {uid}, position #{pid}")
        closed += 1
        try:
            await context.bot.send_message(
                uid, f"🔨 د اډمین له خوا معامله #{pid} تصفیه شوه.\n"
                f"Exit: ${fmt(price,3)} | P/L: ${fmt(pnl)}"
            )
        except Exception:
            logging.exception("Auction notification failed")
    set_setting("last_auction", now())
    await q.message.reply_text(f"حراج بشپړ شو. تړل شوې معاملې: {closed}")


def _normalize_digits(value: str) -> str:
    """Normalize Arabic/Persian digits and common Arabic decimal separators."""
    value = (value or "").translate(str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"
    ))
    return value.replace("٫", ".").replace("٬", ",").replace("﹐", ",")

def _parse_group_trade_request(raw: str):
    """Expected user format: ounce quantity, then خ/ف, then USD price (e.g. 1خ40)."""
    raw = _normalize_digits((raw or "")).strip().replace(" ", "")
    # Accept both decimal comma and decimal point, but keep the required order: qty + خ/ف + price.
    match = re.fullmatch(r"(\d+(?:[.,]\d+)?)ـ?([خف])ـ?(\d+(?:[.,]\d+)?)", raw)
    if not match:
        return None
    try:
        qty = Decimal(match.group(1).replace(",", "."))
        price = Decimal(match.group(3).replace(",", "."))
        if not qty.is_finite() or not price.is_finite() or qty <= 0 or price <= 0:
            return None
        if qty > Decimal("100000") or price > Decimal("1000000"):
            return None
        return float(qty), ("long" if match.group(2) == "خ" else "short"), float(price)
    except (InvalidOperation, ValueError):
        return None

async def _announce_pair_trade(context, uid, side, qty, price, public_name=None):
    if not GROUP_ID:
        return
    try:
        with db() as con:
            row = con.execute("SELECT nickname FROM users WHERE telegram_id=?", (uid,)).fetchone()
        name = (row["nickname"] if row and row["nickname"] else ("ADMIN" if is_admin(uid) else "غړی"))
        group_post = await context.bot.send_message(
            GROUP_ID,
            f"📢 نوې معامله ثبت شوه\n👤 مستعار نوم: {name}\n"
            f"{'🟢 LONG' if side == 'long' else '🔴 SHORT'}\n"
            f"مقدار: {fmt(qty,4)} oz\nد پرانیستلو نرخ: ${fmt(price,3)} / oz"
        )
        context.application.create_task(
            _delete_group_echo_after_60(context, GROUP_ID, group_post.message_id)
        )
    except Exception:
        logging.exception("Group paired-trade announcement failed")

async def _announce_completed_pair_trade(context, chat_id, requester_id, confirmer_id, requester_side, qty, price, request_id=None):
    """Post one permanent public receipt for a successfully matched group trade."""
    requester_name = _group_member_alias(requester_id)
    confirmer_name = _group_member_alias(confirmer_id)
    buyer = requester_name if requester_side == "long" else confirmer_name
    seller = confirmer_name if requester_side == "long" else requester_name
    # Display Afghanistan local time (UTC+04:30), while DB timestamps remain UTC.
    local_time = datetime.now(timezone(timedelta(hours=4, minutes=30)))
    stamp = local_time.strftime("%Y-%m-%d | %H:%M:%S")
    record = (
        "✅ معامله په بریالیتوب ثبت شوه\n"
        f"🟢 خریدار: {buyer}\n"
        f"🔴 فروشنده: {seller}\n"
        f"💵 قیمت: ${fmt(price,3)} د هر اونس\n"
        f"⚖️ مقدار: {fmt(qty,4)} اونس\n"
        f"📅 نېټه: {stamp.split(' | ')[0]}\n"
        f"🕒 وخت: {stamp.split(' | ')[1]} (د افغانستان وخت)"
    )
    posted = await context.bot.send_message(chat_id=chat_id, text=record)
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO group_permanent_messages(chat_id,message_id,trade_request_id,created_at) VALUES(?,?,?,?)",
            (chat_id, posted.message_id, request_id, now())
        )
    return posted


async def _delete_group_echo_after_60(context, chat_id, message_id, request_id=None, original_message_id=None):
    """Expire a group post/request after exactly 60 seconds (best-effort Telegram deletion)."""
    import asyncio
    await asyncio.sleep(60)
    if request_id is not None:
        try:
            with db() as con:
                con.execute(
                    "UPDATE group_trade_requests SET status='expired' WHERE id=? AND status='pending'",
                    (request_id,)
                )
        except Exception:
            logging.exception("Could not expire group trade request")
    for target_id in (message_id, original_message_id):
        if target_id is None:
            continue
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=target_id)
        except Exception:
            logging.info("60-second group message deletion failed; check bot delete permissions")


def _group_member_alias(uid):
    row = get_user(uid)
    if row and row["nickname"]:
        return row["nickname"]
    if is_admin(uid):
        return "ADMIN"
    if row and row["full_name"]:
        return row["full_name"]
    return f"غړی-{uid}"


async def _group_trade_notice(context, chat_id, text):
    """Send a short diagnostic in the group; it is removed after 60 seconds."""
    try:
        notice = await context.bot.send_message(chat_id=chat_id, text=text)
        context.application.create_task(_delete_group_echo_after_60(context, chat_id, notice.message_id))
    except Exception:
        logging.exception("Could not send group trade diagnostic")

async def group_trade_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Short alias-only group posts; requests expire after 60 seconds."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not msg.text:
        return
    raw = msg.text.strip()

    # Keep the admin's existing /price workflow.
    if is_admin(user.id):
        price_text = raw.split(maxsplit=1)[1] if raw.lower().startswith("/price ") else raw
        if positive_number(price_text) is not None:
            await group_price(update, context)
            return

    # A reply to a pending request can be: OK, اوکي, ب, 2, or ب 2 / OK 2.
    replied = msg.reply_to_message
    reply_qty = None
    confirmation = False
    if replied:
        normalized = _normalize_digits(raw).strip().casefold()
        m = re.fullmatch(r"(?:(?:ok|b|ب|اوکي|اوکی)\s*)?(\d+(?:[.,]\d+)?)", normalized)
        if normalized in {"ok", "b", "ب", "اوکي", "اوکی"}:
            confirmation = True
        elif m:
            confirmation = True
            reply_qty = float(m.group(1).replace(",", "."))

    if confirmation and replied:
        # Delete the confirmer's short reply; keep the bot's aliased quote.
        try:
            await msg.delete()
        except Exception:
            pass
        with db() as con:
            req = con.execute(
                "SELECT * FROM group_trade_requests WHERE chat_id=? AND (message_id=? OR original_message_id=?) ORDER BY id DESC LIMIT 1",
                (msg.chat_id, replied.message_id, replied.message_id)
            ).fetchone()
        if not req or req["status"] != "pending":
            await _group_trade_notice(context, msg.chat_id, "⚠️ د دې پیغام لپاره فعاله معامله ونه موندل شوه. د ربات د مستعار نوم لرونکي معاملې پیغام ته Reply وکړئ.")
            return
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(req["created_at"])).total_seconds()
        except Exception:
            age = 61
        if age > 60:
            with db() as con:
                con.execute("UPDATE group_trade_requests SET status='expired' WHERE id=? AND status='pending'", (req["id"],))
            await _group_trade_notice(context, msg.chat_id, "⌛ د معاملې د تایید ۶۰ ثانیې تېرې شوې؛ غوښتنه ختمه شوې ده.")
            return

        confirmer_id = user.id
        requester_id = int(req["requester_id"])
        if confirmer_id == requester_id:
            await _group_trade_notice(context, msg.chat_id, "⚠️ یو غړی خپله معامله نه شي تاییدولای؛ بل ثبت شوی غړی باید Reply وکړي.")
            return
        if not approved(confirmer_id) or not approved(requester_id):
            await _group_trade_notice(context, msg.chat_id, "⚠️ دواړه ګډونوال باید په ربات کې ثبت او د اډمین له خوا تایید شوي وي.")
            return

        price = float(req["entry_price"])
        requester_side = req["side"]
        confirmer_side = "short" if requester_side == "long" else "long"
        latest = current_price()
        band = float(setting("price_band") or 0.50)
        if latest <= 0 or abs(price - latest) > band:
            with db() as con:
                con.execute("UPDATE group_trade_requests SET status='expired' WHERE id=? AND status='pending'", (req["id"],))
            await _group_trade_notice(context, msg.chat_id, f"⚠️ معامله ونه منل شوه: د غوښتل شوي نرخ ${fmt(price,3)} او اوسني نرخ ${fmt(latest,3)} ترمنځ توپیر له مجاز حد ${fmt(band,3)} څخه زیات دی.")
            return

        # $5/oz margin determines ounce capacity. The $0.05/oz opening
        # commission is charged immediately in addition and credited to admin.
        margin_per_oz = float(setting("margin_per_oz") or 5.0)
        commission_per_oz = float(setting("commission_per_oz") or 0.05)
        cost_per_oz = margin_per_oz
        try:
            with db() as con:
                con.execute("BEGIN IMMEDIATE")
                fresh = con.execute("SELECT * FROM group_trade_requests WHERE id=?", (req["id"],)).fetchone()
                if not fresh or fresh["status"] != "pending":
                    con.rollback()
                    return
                requester_acc = con.execute("SELECT balance FROM accounts WHERE telegram_id=?", (requester_id,)).fetchone()
                confirmer_acc = con.execute("SELECT balance FROM accounts WHERE telegram_id=?", (confirmer_id,)).fetchone()
                requester_balance = float(requester_acc["balance"]) if requester_acc else 0.0
                confirmer_balance = float(confirmer_acc["balance"]) if confirmer_acc else 0.0
                requested_qty = float(req["ounces"])
                requester_capacity = requester_balance / cost_per_oz if cost_per_oz > 0 else 0
                confirmer_capacity = confirmer_balance / cost_per_oz if cost_per_oz > 0 else 0
                qty = min(requested_qty, requester_capacity, confirmer_capacity)
                if reply_qty is not None:
                    if reply_qty <= 0:
                        con.rollback()
                        return
                    qty = min(qty, reply_qty)
                # Round down to 4 decimals so the execution never exceeds either balance.
                from decimal import ROUND_DOWN
                qty = float(Decimal(str(qty)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN))
                if qty <= 0:
                    con.rollback()
                    await _group_trade_notice(context, msg.chat_id, "⚠️ معامله ونه اجرا شوه: د یوه یا دواړو غړو ازاد بیلانس د مارجین لپاره کافي نه دی.")
                    return
                margin_each = margin_per_oz * qty
                fee_each = commission_per_oz * qty
                required_each = margin_each + fee_each
                for member_id, side in ((requester_id, requester_side), (confirmer_id, confirmer_side)):
                    con.execute(
                        "UPDATE accounts SET balance=balance-?, reserved_margin=reserved_margin+? WHERE telegram_id=?",
                        (required_each, margin_each, member_id)
                    )
                    con.execute(
                        "INSERT INTO positions(telegram_id,side,ounces,entry_price,margin,open_commission,opened_at) VALUES(?,?,?,?,?,?,?)",
                        (member_id, side, qty, price, margin_each, fee_each, now())
                    )
                    con.execute(
                        "INSERT INTO ledger(telegram_id,kind,amount,note,created_at) VALUES(?,?,?,?,?)",
                        (member_id, "open_trade", -required_each, f"group paired {side} {qty} oz @ {price}; opening fee={fee_each}", now())
                    )
                    credit_admin_commission(con, fee_each, f"group opening commission from {member_id}: {qty} oz")
                updated = con.execute(
                    "UPDATE group_trade_requests SET status='completed', confirmer_id=?, completed_at=? WHERE id=? AND status='pending'",
                    (confirmer_id, now(), req["id"])
                )
                if updated.rowcount != 1:
                    raise RuntimeError("Trade request was already processed")
                con.commit()
        except Exception:
            logging.exception("Atomic group trade execution failed")
            return

        # Keep group output minimal: only the alias and executed quantity/quote.
        try:
            execution_echo = await context.bot.send_message(
                chat_id=msg.chat_id,
                text=f"{_group_member_alias(requester_id)} {fmt(qty,4)}{'خ' if requester_side == 'long' else 'ف'}{fmt(price,3)}"
            )
            context.application.create_task(
                _delete_group_echo_after_60(context, msg.chat_id, execution_echo.message_id)
            )
        except Exception:
            logging.exception("Could not post short execution confirmation")
        # Notify both traders privately so each sees the immediately charged fee.
        for member_id, member_side in ((requester_id, requester_side), (confirmer_id, confirmer_side)):
            try:
                await context.bot.send_message(
                    member_id,
                    "✅ ستاسو ګروپي معامله اجرا شوه.\n"
                    f"لوری: {'LONG' if member_side == 'long' else 'SHORT'}\n"
                    f"مقدار: {fmt(qty,4)} oz | نرخ: ${fmt(price,3)}\n"
                    f"مارجین: ${fmt(margin_each)}\n"
                    f"د پرانیستلو کمېشن: ${fmt(fee_each)} (سمدستي له حسابه کم شو او د اډمین حساب ته جمع شو).\n"
                    f"د کمېشن وروسته اټکلی Equity: ${fmt((requester_balance if member_id == requester_id else confirmer_balance) - fee_each)}"
                )
            except Exception:
                logging.exception("Could not send group trade fee receipt to %s", member_id)
        try:
            await _announce_completed_pair_trade(
                context, msg.chat_id, requester_id, confirmer_id, requester_side, qty, price, req["id"]
            )
        except Exception:
            logging.exception("Could not publish permanent completed-trade receipt")
        return

    parsed = _parse_group_trade_request(raw)
    if not parsed:
        # If the post looks like a trade attempt, explain the exact accepted order.
        if "خ" in raw or "ف" in raw:
            try:
                await msg.delete()
            except Exception:
                pass
            await _group_trade_notice(context, msg.chat_id, "⚠️ د معاملې بڼه ناسمه ده. سمه بڼه: تعداد + خ/ف + قیمت په ډالرو؛ بېلګه: 1خ40 یا 1ف40")
            return
        # Ordinary group text: publish alias-only copy, then delete the member's original.
        alias = _group_member_alias(user.id)
        try:
            echo = await context.bot.send_message(chat_id=msg.chat_id, text=f"{alias} {raw}")
            try:
                await msg.delete()
            except Exception:
                logging.info("Could not delete original group message; check bot admin permissions")
            context.application.create_task(
                _delete_group_echo_after_60(context, msg.chat_id, echo.message_id)
            )
        except Exception:
            logging.exception("Could not echo group member message")
        return

    qty, side, price = parsed
    if not approved(user.id):
        try:
            await msg.delete()
        except Exception:
            pass
        await _group_trade_notice(context, msg.chat_id, "⚠️ د معاملې لپاره باید لومړی په ربات کې ثبت او د اډمین له خوا تایید شوی اوسې.")
        return
    live = current_price()
    band = float(setting("price_band") or 0.50)
    if live <= 0 or abs(price - live) > band:
        try:
            await msg.delete()
        except Exception:
            pass
        await _group_trade_notice(context, msg.chat_id, f"⚠️ غوښتنه ونه منل شوه: اوسنی نرخ ${fmt(live,3)} دی؛ ستا نرخ ${fmt(price,3)} دی. مجاز توپیر ${fmt(band,3)} دی.")
        return

    # Reject an oversized offer BEFORE publishing it. Ounce capacity is based
    # on the $5/oz margin only; the opening fee is charged separately at execution.
    margin_per_oz = float(setting("margin_per_oz") or 5.0)
    with db() as con:
        account = con.execute("SELECT balance FROM accounts WHERE telegram_id=?", (user.id,)).fetchone()
    available_balance = float(account["balance"]) if account else 0.0
    max_ounces = available_balance / margin_per_oz if margin_per_oz > 0 else 0.0
    if qty > max_ounces + 1e-9:
        try:
            await msg.delete()
        except Exception:
            logging.info("Could not delete over-balance group offer; check bot delete permissions")
        return

    alias = _group_member_alias(user.id)
    try:
        # Exactly one compact bot message; no instructions/explanations.
        echo = await context.bot.send_message(chat_id=msg.chat_id, text=f"{alias} {raw}")
        try:
            await msg.delete()
        except Exception:
            logging.info("Could not delete original trade message; check bot admin permissions")
        with db() as con:
            cur = con.execute(
                "INSERT INTO group_trade_requests(chat_id,message_id,requester_id,side,ounces,entry_price,created_at,original_message_id) VALUES(?,?,?,?,?,?,?,?)",
                (msg.chat_id, echo.message_id, user.id, side, qty, price, now(), msg.message_id)
            )
            request_id = cur.lastrowid
        context.application.create_task(
            _delete_group_echo_after_60(context, msg.chat_id, echo.message_id, request_id)
        )
    except Exception:
        logging.exception("Could not publish aliased trade request")

async def group_message_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete ordinary group messages after 60s, but never delete completed-trade receipts."""
    msg = update.effective_message
    if not msg or not update.effective_chat:
        return
    chat_id, message_id = update.effective_chat.id, msg.message_id
    with db() as con:
        permanent = con.execute(
            "SELECT 1 FROM group_permanent_messages WHERE chat_id=? AND message_id=?",
            (chat_id, message_id)
        ).fetchone()
    if permanent:
        return
    import asyncio
    await asyncio.sleep(60)
    # Check again in case this message was marked as a permanent trade receipt meanwhile.
    with db() as con:
        permanent = con.execute(
            "SELECT 1 FROM group_permanent_messages WHERE chat_id=? AND message_id=?",
            (chat_id, message_id)
        ).fetchone()
    if permanent:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        logging.info("60-second group message cleanup failed; verify Delete Messages permission")

async def group_nontext_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove non-text group posts; the universal cleanup also retries at 60 seconds."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    try:
        await msg.delete()
    except Exception:
        logging.exception("Could not delete non-text group message")

async def group_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Only the configured admin may publish the reference price in a group."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not is_admin(user.id):
        return
    raw = (msg.text or "").strip()
    if raw.startswith("/price"):
        parts = raw.split()
        raw = parts[1] if len(parts) == 2 else ""
    value = positive_number(raw)
    if value is None:
        return
    set_setting("silver_price", value)
    set_setting("silver_price_updated", now())
    response = await msg.reply_text(
        f"🥈 نوې مظنه ثبت شوه: ${fmt(value,3)} / oz\n"
        f"د نویو معاملاتو د منلو حد: ${fmt(value-0.50,3)} – ${fmt(value+0.50,3)}\n"
        "دا مظنه یوازې پر نویو معاملاتو تطبیقېږي؛ پخوانۍ معاملې نه بدلېږي."
    )
    import asyncio
    async def expire_price_messages():
        await asyncio.sleep(60)
        for message_id in (msg.message_id, response.message_id):
            try:
                await context.bot.delete_message(chat_id=msg.chat_id, message_id=message_id)
            except Exception:
                logging.info("Could not delete 60-second price message")
    context.application.create_task(expire_price_messages())
    await send_margin_alerts(context)

# --------------------------- ADMIN TEXT COMMANDS ---------------------------

async def admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    action = context.user_data.get("admin_action")
    text = (update.message.text or "").strip()
    if not action:
        return
    if action == "member_lookup":
        with db() as con:
            member = con.execute("""
                SELECT u.telegram_id,u.username,u.full_name,u.nickname,u.phone,u.country,u.status,
                       u.created_at,a.balance,a.reserved_margin
                FROM users u LEFT JOIN accounts a USING(telegram_id)
                WHERE u.nickname=? COLLATE NOCASE
            """, (text,)).fetchone()
            if not member:
                await update.message.reply_text("دا مستعار نوم ونه موندل شو. نوم کټ مټ ولیکئ.")
                return
            positions = con.execute("""
                SELECT id,side,ounces,entry_price,margin,open_commission,opened_at
                FROM positions WHERE telegram_id=? AND status='open' ORDER BY id DESC
            """, (member["telegram_id"],)).fetchall()
        bal = float(member["balance"] or 0)
        reserved = float(member["reserved_margin"] or 0)
        price = current_price()
        pnl_total = sum(unrealized(pos, price) for pos in positions) if price else 0.0
        equity = bal + reserved + pnl_total
        lines = [
            "👤 د غړي معلومات",
            f"مستعار نوم: {member['nickname']}",
            f"اصلي نوم: {member['full_name'] or '—'}",
            f"Telegram ID: {member['telegram_id']}",
            f"Telegram username: @{member['username']}" if member['username'] else "Telegram username: نه لري",
            f"د ټیلیفون شمېره: {member['phone'] or 'نه ده شریکه شوې'}",
            f"هېواد: {member['country'] or '—'} | حالت: {member['status']}",
            f"ازاد بیلانس: ${fmt(bal)}",
            f"مصروف مارجین: ${fmt(reserved)}",
            f"د خلاصو معاملاتو شمېر: {len(positions)}",
            f"ناواقعي ګټه/تاوان: ${fmt(pnl_total)}",
            f"خالص Equity: ${fmt(equity)}",
        ]
        if positions:
            lines.append("\n📈 خلاصې معاملې:")
            for pos in positions[:10]:
                lines.append(
                    f"#{pos['id']} | {pos['side'].upper()} | {fmt(pos['ounces'],4)} oz | "
                    f"Entry ${fmt(pos['entry_price'],3)} | Margin ${fmt(pos['margin'])}"
                )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("💳 د دې غړي ازاد بیلانس بدلول", callback_data=f"a:memberbalance:{member['telegram_id']}")
        ]])
        context.user_data.pop("admin_action", None)
        await update.message.reply_text("\n".join(lines), reply_markup=kb)
    elif action == "price":
        value = positive_number(text)
        if value is None:
            await update.message.reply_text("سم مثبت نرخ ولیکئ.")
            return
        set_setting("silver_price", value)
        set_setting("silver_price_updated", now())
        context.user_data.pop("admin_action", None)
        await update.message.reply_text(f"✅ XAG/USD نرخ ثبت شو: ${fmt(value,3)}/oz", reply_markup=admin_menu())
        await send_margin_alerts(context)
    elif action == "balance_id":
        if not text.isdigit() or not get_user(int(text)):
            await update.message.reply_text("سم ثبت شوی Telegram ID ولیکئ.")
            return
        context.user_data["balance_target"] = int(text)
        context.user_data["admin_action"] = "balance_amount"
        await update.message.reply_text("نوی USD بیلانس ولیکئ:")
    elif action == "freecredit_id":
        if not text.isdigit() or not get_user(int(text)):
            await update.message.reply_text("سم ثبت شوی Telegram ID ولیکئ.")
            return
        context.user_data["balance_target"] = int(text)
        context.user_data["admin_action"] = "freecredit_amount"
        await update.message.reply_text("د وړیا کریډیټ اندازه په USD ولیکئ (مثلاً 100):")
    elif action == "freecredit_amount":
        value = positive_number(text)
        if value is None or value > 100000:
            await update.message.reply_text("سم مثبت مقدار ولیکئ (تر 100000 USD پورې).")
            return
        target = context.user_data["balance_target"]
        with db() as con:
            con.execute("UPDATE accounts SET balance=balance+? WHERE telegram_id=?", (value, target))
            con.execute("INSERT INTO ledger(telegram_id,kind,amount,note,created_at) VALUES(?,?,?,?,?)",
                        (target, "free_credit", value, "Admin family/friends credit", now()))
        context.user_data.clear()
        await update.message.reply_text(f"🎁 د {target} حساب ته ${fmt(value)} وړیا کریډیټ ورزیات شو.", reply_markup=admin_menu())
        try:
            await context.bot.send_message(target, f"🎁 اډمین ستاسو حساب ته ${fmt(value)} وړیا کریډیټ اضافه کړ.")
        except Exception:
            logging.exception("Free credit notification failed")
    elif action == "balance_amount":
        value = positive_number(text)
        if value is None:
            await update.message.reply_text("سم مثبت مقدار ولیکئ.")
            return
        target = context.user_data["balance_target"]
        with db() as con:
            con.execute("UPDATE accounts SET balance=? WHERE telegram_id=?", (value, target))
            con.execute("""
                INSERT INTO ledger(telegram_id,kind,amount,note,created_at)
                VALUES(?,?,?,?,?)
            """, (target, "admin_balance_set", value, "Admin set balance", now()))
        context.user_data.clear()
        await update.message.reply_text(f"✅ د {target} نوی بیلانس: ${fmt(value)}", reply_markup=admin_menu())

# --------------------------- SETTINGS COMMANDS ---------------------------

async def set_parameter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    async def reply_setting(text):
        response = await update.message.reply_text(text)
        if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
            context.application.create_task(
                _delete_group_echo_after_60(context, update.effective_chat.id, response.message_id,
                                            original_message_id=update.message.message_id)
            )
        return response
    command = update.message.text.split()[0].split("@")[0]
    mapping = {
        "/setmargin": ("margin_per_oz", "Initial margin per ounce"),
        "/setwarning": ("warning_margin_per_oz", "Warning threshold per ounce"),
        "/setcall": ("call_margin_per_oz", "Margin Call threshold per ounce"),
        "/setfee": ("commission_per_oz", "Commission per ounce"),
        "/setleverage": ("leverage", "Display leverage"),
    }
    if command == "/setauction":
        if len(context.args) != 1 or context.args[0] not in ("on", "off"):
            await reply_setting("کارونه: /setauction on یا /setauction off")
            return
        set_setting("auction_enabled", "1" if context.args[0] == "on" else "0")
        await reply_setting("د حراج حالت بدل شو.")
        return
    key, label = mapping[command]
    if len(context.args) != 1:
        await reply_setting(f"کارونه: {command} مقدار")
        return
    value = positive_number(context.args[0])
    if value is None:
        await reply_setting("سم مثبت عدد ولیکئ.")
        return
    set_setting(key, value)
    await reply_setting(f"✅ {label}: {value}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("عملیه لغوه شوه.", reply_markup=home_keyboard())
    return ConversationHandler.END

async def text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == "🏠 مینو":
        await start(update, context)
    elif text == "📜 اصول":
        response = await update.message.reply_text(RULES)
        if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
            context.application.create_task(
                _delete_group_echo_after_60(context, update.effective_chat.id, response.message_id)
            )

# --------------------------- APP ---------------------------

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log unexpected handler errors without exposing secrets or crashing polling."""
    logging.error("Unhandled bot exception", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ تخنیکي ستونزه رامنځته شوه. اډمین خبر شوی؛ مهرباني وکړئ بیا هڅه وکړئ."
            )
        except Exception:
            logging.exception("Could not send error notice to user")

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing")
    if not ADMIN_ID:
        logging.warning("ADMIN_ID is missing; admin features will not work.")
    init_db()
    logging.info("Using database: %s", DB_PATH)
    if GROUP_ID:
        logging.info("Anonymous trade announcements enabled for configured group.")
    app = Application.builder().token(BOT_TOKEN).build()

    alias_flow = ConversationHandler(
        entry_points=[
            CommandHandler("alias", alias_prompt),
            CallbackQueryHandler(alias_prompt, pattern=r"^u:alias$"),
        ],
        states={ALIAS_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, alias_save)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    registration = ConversationHandler(
        entry_points=[CallbackQueryHandler(accept_rules, pattern=r"^accept$")],
        states={
            REGISTRATION: [
                MessageHandler(filters.CONTACT, register_contact),
                MessageHandler(filters.TEXT & ~filters.COMMAND, register_alias),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    trade_flow = ConversationHandler(
        entry_points=[CallbackQueryHandler(begin_trade, pattern=r"^trade:(long|short)$")],
        states={TRADE_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, execute_trade)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    money_flow = ConversationHandler(
        entry_points=[CallbackQueryHandler(money_request_start, pattern=r"^money:(deposit|withdraw)$")],
        states={REQUEST_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, money_request_amount)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("price", group_price))
    app.add_handler(alias_flow)
    app.add_handler(registration)
    app.add_handler(trade_flow)
    app.add_handler(money_flow)
    app.add_handler(CallbackQueryHandler(accept_rules, pattern=r"^accept$"))
    app.add_handler(CallbackQueryHandler(decline_rules, pattern=r"^decline$"))
    app.add_handler(CallbackQueryHandler(admin_decision, pattern=r"^(approve|reject):"))
    app.add_handler(CallbackQueryHandler(review_money_request, pattern=r"^money(approve|reject):"))
    app.add_handler(CallbackQueryHandler(auction_callback, pattern=r"^auction:"))
    app.add_handler(CallbackQueryHandler(close_position, pattern=r"^close:"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^a:"))
    app.add_handler(CallbackQueryHandler(user_callback, pattern=r"^u:"))
    app.add_handler(MessageHandler(filters.Regex(r"^(🏠 مینو|📜 اصول)$"), text_buttons))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, group_trade_message))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.TEXT, group_nontext_cleanup))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.ALL, group_message_expiry), group=1)
    app.add_handler(MessageHandler(filters.Regex(r"^/(setmargin|setwarning|setcall|setfee|setleverage|setauction)(?:@\w+)?(?:\s|$)"), set_parameter))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text))

    app.add_error_handler(global_error_handler)
    logging.info("AFG SILVER EXCHANGE bot starting; database=%s", DB_PATH)
    with db() as con:
        cols = [row["name"] for row in con.execute("PRAGMA table_info(accounts)")]
    logging.info("accounts table columns: %s", cols)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    main()
