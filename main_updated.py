import os
import sqlite3
import logging
from datetime import datetime, timezone
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
# Demo trading platform. No real money or silver is transferred.
# Requires: python-telegram-bot >= 20
# Environment: BOT_TOKEN, ADMIN_ID, DB_PATH (optional)
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "afg_silver.sqlite3")

# Trading rules (admin can change selected values from Settings)
DEFAULTS = {
    "silver_price": "0",                 # USD per troy ounce
    "silver_price_updated": "",
    "margin_per_oz": "5.00",             # initial margin per ounce
    "warning_margin_per_oz": "2.50",     # first warning threshold
    "call_margin_per_oz": "1.00",        # margin call threshold
    "commission_per_oz": "0.05",         # charged on opening and closing
    "leverage": "10",
    "auction_enabled": "0",              # admin-controlled; no auto-liquidation
    "last_auction": "",
    "deposit_instructions": "د Demo چارج لپاره له اډمین سره اړیکه ونیسئ.",
}

REGISTRATION = 1
TRADE_QTY = 2
ADMIN_TEXT = 3
REQUEST_AMOUNT = 4

RULES = """
🥈 AFG SILVER EXCHANGE — د ګډون او معاملاتو اصول

۱. د حساب جوړولو لپاره سم نوم، د اړیکې شمېره او د اوسېدو هېواد ورکړئ.
۲. تر هغه چې اډمین ثبت‌نام تایید نه کړي، معامله نه شئ کولای.
۳. د سپینو زرو نرخ د اډمین له خوا په USD د هر Troy Ounce ثبتېږي.
۴. دا نسخه DEMO/SIMULATION ده؛ حقیقي پیسې یا سپین زر نه لېږدول کېږي.
۵. Demo معاملې د Long او Short په بڼه دي؛ ګټه او تاوان د ثبت شوي نرخ له مخې محاسبه کېږي.
۶. د مارجین Warning حد $2.50/oz او Margin Call حد $1.00/oz دی.
۷. د اجباري حراج/تصفیې اجرا یوازې د اډمین له خوا ده؛ اتومات Liquidation نه شته.
۸. د حقیقي پیسو فعالولو مخکې باید قانوني، بانکي، امنیتي او د خطر مدیریت چارې جلا بشپړې او وازمویل شي.

د «اصول منم» تڼۍ په کېکاږلو سره تاییدوئ چې اصول مو ولوستل.
"""

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
        float(p["margin"]) + unrealized(p, price) for p in positions
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
        [InlineKeyboardButton("📥 Demo چارج", callback_data="money:deposit"),
         InlineKeyboardButton("📤 Demo ایستل", callback_data="money:withdraw")],
        [InlineKeyboardButton("👤 زما معلومات", callback_data="u:profile"),
         InlineKeyboardButton("📞 اړیکه", callback_data="u:contact")],
        [InlineKeyboardButton("📜 اصول", callback_data="u:rules")],
    ])

def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 د ثبت‌نام غوښتنې", callback_data="a:pending"),
         InlineKeyboardButton("👥 غړي", callback_data="a:members")],
        [InlineKeyboardButton("💲 د XAG/USD نرخ", callback_data="a:price"),
         InlineKeyboardButton("💳 بیلانس تنظیمول", callback_data="a:balance")],
        [InlineKeyboardButton("📊 ټول راپور", callback_data="a:report"),
         InlineKeyboardButton("📈 خلاصې معاملې", callback_data="a:positions")],
        [InlineKeyboardButton("📥 د چارج غوښتنې", callback_data="a:deposits"),
         InlineKeyboardButton("📤 د ایستلو غوښتنې", callback_data="a:withdrawals")],
        [InlineKeyboardButton("⚙️ د مارجین/کمېشن تنظیمات", callback_data="a:settings")],
        [InlineKeyboardButton("🔨 د حراج/تصفیې اجرا", callback_data="a:auction")],
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
    if is_admin(uid):
        await update.effective_message.reply_text(
            "👑 AFG SILVER EXCHANGE — اډمین پینل\n"
            "یادونه: ټولې معاملې Demo دي.",
            reply_markup=admin_menu()
        )
        return
    row = get_user(uid)
    if row and row["status"] == "approved":
        await update.effective_message.reply_text(
            "ښه راغلاست! ستاسو حساب فعال دی. ټولې معاملې Demo دي.",
            reply_markup=user_menu()
        )
    elif row and row["status"] == "pending":
        await update.effective_message.reply_text(
            "ستاسو ثبت‌نام د اډمین تایید ته منتظر دی.", reply_markup=home_keyboard()
        )
    else:
        await update.effective_message.reply_text(
            "🥈 AFG SILVER EXCHANGE ته ښه راغلاست!\n"
            "د ګډون اصول ولولئ او ومنئ.",
            reply_markup=home_keyboard()
        )
        await update.effective_message.reply_text(RULES, reply_markup=rules_keyboard())

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
            INSERT INTO users(telegram_id,username,full_name,phone,country,status,accepted_at,created_at)
            VALUES(?,?,?,?,?,'pending',?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET
              username=excluded.username,full_name=excluded.full_name,
              phone=excluded.phone,country=excluded.country,status='pending',
              accepted_at=excluded.accepted_at
            """, (user.id, user.username, d["reg_name"], d["reg_phone"],
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
                    f"📝 نوې غوښتنه\nنوم: {d['reg_name']}\nشمېره: {d['reg_phone']}\n"
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

# --------------------------- USER VIEWS ---------------------------

async def show_account(chat, uid):
    a = user_account(uid)
    positions = open_positions(uid)
    price = current_price()
    floating = sum(unrealized(p, price) for p in positions) if price else 0
    total_margin = sum(float(p["margin"]) for p in positions)
    await chat.reply_text(
        "💵 ستاسو Demo حساب\n\n"
        f"ازاد بیلانس: ${fmt(a['balance'])}\n"
        f"مصروف مارجین: ${fmt(a['reserved_margin'])}\n"
        f"د خلاصو معاملاتو مارجین: ${fmt(total_margin)}\n"
        f"ناخالصه ګټه/تاوان: ${fmt(floating)}\n"
        f"اټکلی Equity: ${fmt(float(a['balance']) + float(a['reserved_margin']) + floating)}\n"
        "دا حقیقي پیسې نه دي."
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
    if not approved(uid):
        await q.message.reply_text("معامله یوازې د تایید شویو غړو لپاره ده.")
        return ConversationHandler.END
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
    margin = float(setting("margin_per_oz")) * qty
    commission = float(setting("commission_per_oz")) * qty
    required = margin + commission
    with db() as con:
        a = con.execute("SELECT * FROM accounts WHERE telegram_id=?", (uid,)).fetchone()
        if not a or float(a["balance"]) < required:
            available = float(a["balance"]) if a else 0
            await update.message.reply_text(
                f"Demo بیلانس کافي نه دی.\nاړین: ${fmt(required)}\nشته: ${fmt(available)}",
                reply_markup=user_menu()
            )
            return ConversationHandler.END
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
        """, (uid, "open_trade", -required, f"{side} {qty} oz", now()))
    context.user_data.pop("trade_side", None)
    context.user_data.pop("trade_price", None)
    await update.message.reply_text(
        f"✅ Demo {side.upper()} پرانیستل شو.\n"
        f"مقدار: {fmt(qty,4)} oz\nEntry: ${fmt(price,3)}\n"
        f"مارجین: ${fmt(margin)}\nکمېشن: ${fmt(commission)}\n"
        "یادونه: دا Simulation دی، حقیقي معامله نه ده.",
        reply_markup=user_menu()
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
    await q.message.reply_text(
        f"🔒 معامله #{pid} بنده شوه.\nExit: ${fmt(price,3)}\n"
        f"Realized P/L: ${fmt(pnl)}\nClose commission: ${fmt(close_fee)}\n"
        f"حساب ته بېرته ورګرځېد: ${fmt(credit)}"
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
        f"د Demo {'چارج' if kind == 'deposit' else 'ایستلو'} اندازه په USD ولیکئ:"
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
        f"✅ د Demo غوښتنه ثبت شوه. شمېره: #{rid}\n"
        "د اډمین تر پرېکړې پورې انتظار وباسئ.",
        reply_markup=user_menu()
    )
    if ADMIN_ID:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ تایید", callback_data=f"moneyapprove:{rid}"),
            InlineKeyboardButton("❌ رد", callback_data=f"moneyreject:{rid}")
        ]])
        await context.bot.send_message(
            ADMIN_ID, f"د Demo غوښتنه #{rid}\nغړی: {uid}\nډول: {kind}\nمبلغ: ${fmt(amount)}",
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
        await context.bot.send_message(uid, f"ستاسو د Demo غوښتنه #{rid}: {status}")
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
    if action == "home":
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
            f"د غړو ازاد Demo بیلانس: ${fmt(bal)}\n"
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

# --------------------------- ADMIN TEXT COMMANDS ---------------------------

async def admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    action = context.user_data.get("admin_action")
    text = (update.message.text or "").strip()
    if not action:
        return
    if action == "price":
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
        await update.message.reply_text("نوی Demo USD بیلانس ولیکئ:")
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
        await update.message.reply_text(f"✅ د {target} نوی Demo بیلانس: ${fmt(value)}", reply_markup=admin_menu())

# --------------------------- SETTINGS COMMANDS ---------------------------

async def set_parameter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
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
            await update.message.reply_text("کارونه: /setauction on یا /setauction off")
            return
        set_setting("auction_enabled", "1" if context.args[0] == "on" else "0")
        await update.message.reply_text("د حراج حالت بدل شو.")
        return
    key, label = mapping[command]
    if len(context.args) != 1:
        await update.message.reply_text(f"کارونه: {command} مقدار")
        return
    value = positive_number(context.args[0])
    if value is None:
        await update.message.reply_text("سم مثبت عدد ولیکئ.")
        return
    set_setting(key, value)
    await update.message.reply_text(f"✅ {label}: {value}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("عملیه لغوه شوه.", reply_markup=home_keyboard())
    return ConversationHandler.END

async def text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == "🏠 مینو":
        await start(update, context)
    elif text == "📜 اصول":
        await update.message.reply_text(RULES)

# --------------------------- APP ---------------------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing")
    if not ADMIN_ID:
        logging.warning("ADMIN_ID is missing; admin features will not work.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    registration = ConversationHandler(
        entry_points=[CallbackQueryHandler(accept_rules, pattern=r"^accept$")],
        states={
            REGISTRATION: [
                MessageHandler(filters.CONTACT, register_contact),
                MessageHandler(filters.TEXT & ~filters.COMMAND, registration_text),
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
    app.add_handler(MessageHandler(filters.Regex(r"^/(setmargin|setwarning|setcall|setfee|setleverage|setauction)(?:@\\w+)?(?:\\s|$)"), set_parameter))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text))

    logging.info("AFG SILVER EXCHANGE demo bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    main()
