import os
import sqlite3
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "afg_silver.sqlite3")

# All trades in this version are SIMULATIONS. No real money or real silver is transferred.
NAME, PHONE, COUNTRY, TRADE_QTY, ADMIN_PRICE, ADMIN_BALANCE_ID, ADMIN_BALANCE_AMOUNT = range(7)

RULES = """
🥈 AFG SILVER EXCHANGE — د ګډون اصول

۱. د حساب جوړولو لپاره خپل سم نوم، د اړیکې شمېره او د اوسېدو هېواد ورکړئ.
۲. تر هغه چې اډمین ثبت‌نام تایید نه کړي، د معاملې اجازه نشته.
۳. د سپینو زرو نرخ د اډمین له خوا په لاسي ډول ثبتېږي؛ د نرخ وخت هم ښکاره کېږي.
۴. دا نسخه یوازې ازمایښتي ده. بیلانس، پېر او پلور او معاملې ټول DEMO دي.
۵. هېڅ حقیقي پیسې مه لېږئ او هېڅ حقیقي سپین زر د دې ربات له لارې مه اخلئ/مه پلورئ.
۶. د حقیقي معاملاتو د فعالولو مخکې باید د نرخ سرچینه، کمېشن، مارجین، حراج، تصفیه، شکایت او امنیتي تګلارې جلا تصویب او وازمویل شي.

د «اصول منم» تڼۍ په کېکاږلو سره تاییدوئ چې پورته اصول مو لوستي او درک کړي دي.
"""

def now():
    return datetime.now(timezone.utc).isoformat()

def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            phone TEXT,
            country TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            rules_version TEXT NOT NULL DEFAULT '1',
            accepted_at TEXT,
            created_at TEXT NOT NULL
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            telegram_id INTEGER PRIMARY KEY,
            demo_balance REAL NOT NULL DEFAULT 0,
            silver_grams REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            side TEXT NOT NULL,
            grams REAL NOT NULL,
            price_per_gram REAL NOT NULL,
            total REAL NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """)
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('silver_price','0')")
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('silver_price_updated','')")
        # Ensure existing registered users have demo accounts.
        con.execute("""
            INSERT OR IGNORE INTO accounts(telegram_id)
            SELECT telegram_id FROM users
        """)

def get_setting(key, default=""):
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_setting(key, value):
    with db() as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

def get_user(uid):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()

def is_admin(uid):
    return ADMIN_ID != 0 and uid == ADMIN_ID

def money(value):
    return f"{float(value):,.2f}"

def parse_positive(text):
    try:
        value = Decimal(text.strip().replace(",", ""))
        if not value.is_finite() or value <= 0:
            return None
        return float(value)
    except (InvalidOperation, AttributeError):
        return None

def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 د ثبت‌نام غوښتنې", callback_data="admin:pending"),
         InlineKeyboardButton("👥 ټول غړي", callback_data="admin:members")],
        [InlineKeyboardButton("🥈 د سپینو زرو نرخ ټاکل", callback_data="admin:setprice")],
        [InlineKeyboardButton("💳 د Demo بیلانس تنظیمول", callback_data="admin:setbalance")],
        [InlineKeyboardButton("📊 د معاملاتو راپور", callback_data="admin:report")],
        [InlineKeyboardButton("🔄 تازه کول", callback_data="admin:home")],
    ])

def user_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🥈 د سپینو زرو نرخ", callback_data="user:price"),
         InlineKeyboardButton("💰 زما حساب", callback_data="user:account")],
        [InlineKeyboardButton("🟢 Demo پېر", callback_data="trade:buy"),
         InlineKeyboardButton("🔴 Demo پلور", callback_data="trade:sell")],
        [InlineKeyboardButton("📋 زما معاملې", callback_data="user:history")],
        [InlineKeyboardButton("👤 زما معلومات", callback_data="user:profile")],
        [InlineKeyboardButton("📜 اصول", callback_data="user:rules"),
         InlineKeyboardButton("📞 اړیکه", callback_data="user:contact")],
    ])

def main_menu():
    # Persistent Telegram reply keyboard shown below the chat input.
    return ReplyKeyboardMarkup(
        [
            ["📜 د معاملاتو اصول", "📝 ثبت‌نام"],
            ["🌐 ژبه: پښتو"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )

async def main_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the three buttons on the public start menu."""
    choice = (update.message.text or "").strip()

    if choice == "📜 د معاملاتو اصول":
        await update.message.reply_text(RULES, reply_markup=rules_keyboard())
    elif choice == "📝 ثبت‌نام":
        row = get_user(update.effective_user.id)
        if row and row["status"] == "approved":
            await update.message.reply_text(
                "ستاسو ثبت‌نام مخکې تایید شوی دی.", reply_markup=user_menu()
            )
        else:
            await update.message.reply_text(
                "د ثبت‌نام لپاره لومړی اصول ولولئ او ومنئ.",
                reply_markup=rules_keyboard(),
            )
    elif choice == "🌐 ژبه: پښتو":
        await update.message.reply_text("پښتو ژبه فعاله ده.")

def rules_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 اصول منم", callback_data="accept_rules")],
        [InlineKeyboardButton("🔴 نه منم", callback_data="decline_rules")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_admin(user.id):
        await update.effective_message.reply_text(
            "👑 د AFG SILVER EXCHANGE اډمین پینل\n\n"
            "یادونه: ټولې معاملې په دې نسخه کې یوازې Demo دي.",
            reply_markup=admin_menu(),
        )
        return
    row = get_user(user.id)
    if row and row["status"] == "approved":
        await update.effective_message.reply_text(
            "ښه راغلاست! ستاسو ثبت‌نام تایید شوی دی.\n"
            "دا ازمایښتي سیستم دی؛ حقیقي پیسې او حقیقي معاملات نشته.",
            reply_markup=user_menu(),
        )
    elif row and row["status"] == "pending":
        await update.effective_message.reply_text(
            "ستاسو ثبت‌نام ثبت شوی او د اډمین تایید ته منتظر دی.",
            reply_markup=main_menu(),
        )
    elif row and row["status"] == "rejected":
        await update.effective_message.reply_text(
            "ستاسو پخوانۍ غوښتنه نه ده منل شوې. که بیا غوښتنه کول غواړئ، له اډمین سره اړیکه ونیسئ.",
            reply_markup=main_menu(),
        )
    else:
        await update.effective_message.reply_text(
            "🥈 AFG SILVER EXCHANGE ته ښه راغلاست!\n"
            "د ګډون لپاره لومړی اصول ولولئ، بیا ثبت‌نام وکړئ.",
            reply_markup=main_menu(),
        )

async def show_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(RULES, reply_markup=rules_keyboard())

async def accept_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["rules_accepted"] = True
    context.user_data["accepted_at"] = now()
    await q.message.reply_text(
        "مننه. اوس خپل بشپړ نوم ولیکئ:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return NAME

async def decline_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(
        "د اصولو له منلو پرته ثبت‌نام ممکن نه دی.",
        reply_markup=main_menu(),
    )
    return ConversationHandler.END

async def register_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    row = get_user(q.from_user.id)
    if row and row["status"] == "approved":
        await q.message.reply_text("ستاسو ثبت‌نام مخکې تایید شوی دی.", reply_markup=user_menu())
        return
    await q.message.reply_text(
        "لومړی اصول ولولئ او د «اصول منم» تڼۍ کېکاږئ.",
    )
    await q.message.reply_text(RULES, reply_markup=rules_keyboard())

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) < 3 or len(name) > 100:
        await update.message.reply_text("سم بشپړ نوم ولیکئ (۳ تر ۱۰۰ تورو).")
        return NAME
    context.user_data["full_name"] = name
    contact_button = KeyboardButton("📱 زما د اړیکې شمېره شریکول", request_contact=True)
    await update.message.reply_text(
        "اوس د لاندې تڼۍ له لارې خپله د موبایل شمېره شریکه کړئ:",
        reply_markup=ReplyKeyboardMarkup(
            [[contact_button]], resize_keyboard=True, one_time_keyboard=True
        ),
    )
    return PHONE

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.contact:
        await update.message.reply_text("مهرباني وکړئ د Telegram د تماس تڼۍ وکاروئ.")
        return PHONE
    if update.message.contact.user_id and update.message.contact.user_id != update.effective_user.id:
        await update.message.reply_text("مهرباني وکړئ خپل شخصي تماس شریک کړئ.")
        return PHONE
    context.user_data["phone"] = update.message.contact.phone_number
    await update.message.reply_text("د اوسېدو هېواد ولیکئ:", reply_markup=ReplyKeyboardRemove())
    return COUNTRY

async def get_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    country = update.message.text.strip()
    if len(country) < 2 or len(country) > 80:
        await update.message.reply_text("د هېواد نوم سم ولیکئ.")
        return COUNTRY
    user = update.effective_user
    data = context.user_data
    timestamp = now()
    with db() as con:
        con.execute("""
        INSERT INTO users (telegram_id, username, full_name, phone, country, status,
                           rules_version, accepted_at, created_at)
        VALUES (?, ?, ?, ?, ?, 'pending', '1', ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
          username=excluded.username, full_name=excluded.full_name,
          phone=excluded.phone, country=excluded.country,
          status='pending', rules_version='1',
          accepted_at=excluded.accepted_at
        """, (
            user.id, user.username, data["full_name"], data["phone"], country,
            data.get("accepted_at", timestamp), timestamp,
        ))
        con.execute("INSERT OR IGNORE INTO accounts(telegram_id) VALUES(?)", (user.id,))

    await update.message.reply_text(
        "✅ ستاسو غوښتنه ثبت شوه. د اډمین تر تایید پورې معاملې نه شئ کولای.",
        reply_markup=ReplyKeyboardRemove(),
    )
    if ADMIN_ID:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ تایید", callback_data=f"approve:{user.id}"),
            InlineKeyboardButton("❌ رد", callback_data=f"reject:{user.id}"),
        ]])
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "📝 د ثبت‌نام نوې غوښتنه\n\n"
                    f"نوم: {data['full_name']}\n"
                    f"Telegram: @{user.username or 'ندارد'}\n"
                    f"Telegram ID: {user.id}\n"
                    f"شمېره: {data['phone']}\n"
                    f"هېواد: {country}"
                ),
                reply_markup=keyboard,
            )
        except Exception:
            logging.exception("Admin notification failed")
            await update.message.reply_text(
                "خبرتیا اډمین ته ونه لېږل شوه. اډمین باید لومړی ربات Start کړي."
            )
    else:
        await update.message.reply_text("د اډمین ID لا نه دی تنظیم شوی.")
    context.user_data.clear()
    return ConversationHandler.END

async def admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("دا واک یوازې د ربات اډمین لري.")
        return
    action, target = q.data.split(":")
    target_id = int(target)
    status = "approved" if action == "approve" else "rejected"
    with db() as con:
        cur = con.execute("UPDATE users SET status=? WHERE telegram_id=?", (status, target_id))
        con.execute("INSERT OR IGNORE INTO accounts(telegram_id) VALUES(?)", (target_id,))
    if cur.rowcount == 0:
        await q.message.reply_text("دا کارن په ډیټابېس کې ونه موندل شو.")
        return
    await q.edit_message_reply_markup(reply_markup=None)
    await q.message.reply_text(f"د کارن {target_id} غوښتنه: {status}")
    try:
        msg = "✅ ستاسو ثبت‌نام تایید شو." if status == "approved" else "❌ ستاسو د ثبت‌نام غوښتنه ونه منل شوه."
        await context.bot.send_message(chat_id=target_id, text=msg)
    except Exception:
        logging.exception("Could not notify user")

async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("دا برخه یوازې د اډمین لپاره ده.")
        return
    action = q.data.split(":")[1]

    if action == "home":
        await q.message.reply_text("👑 د اډمین پینل", reply_markup=admin_menu())

    elif action == "pending":
        with db() as con:
            rows = con.execute(
                "SELECT telegram_id,full_name,phone,country FROM users WHERE status='pending' ORDER BY created_at"
            ).fetchall()
        if not rows:
            await q.message.reply_text("اوس مهال د تایید لپاره نوې غوښتنه نشته.")
            return
        for row in rows[:30]:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ تایید", callback_data=f"approve:{row['telegram_id']}"),
                InlineKeyboardButton("❌ رد", callback_data=f"reject:{row['telegram_id']}"),
            ]])
            await q.message.reply_text(
                f"نوم: {row['full_name']}\nشمېره: {row['phone']}\n"
                f"هېواد: {row['country']}\nID: {row['telegram_id']}",
                reply_markup=kb,
            )

    elif action == "members":
        with db() as con:
            rows = con.execute(
                "SELECT telegram_id,full_name,status FROM users ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
        if not rows:
            await q.message.reply_text("تر اوسه هېڅ غړی ثبت شوی نه دی.")
        else:
            lines = ["👥 وروستي غړي (تر ۵۰ پورې):"]
            for r in rows:
                lines.append(f"• {r['full_name']} | {r['telegram_id']} | {r['status']}")
            await q.message.reply_text("\n".join(lines))

    elif action == "setprice":
        context.user_data["flow"] = "admin_price"
        await q.message.reply_text(
            "د سپینو زرو نوی Demo نرخ په AFN د هر ګرام ولیکئ.\n"
            "یوازې مثبت عدد ولیکئ؛ دا نرخ په اتومات ډول له بازار څخه نه راځي."
        )

    elif action == "setbalance":
        context.user_data["flow"] = "admin_balance_id"
        await q.message.reply_text("د هغه غړي Telegram ID ولیکئ چې Demo بیلانس یې بدلول غواړئ.")

    elif action == "report":
        with db() as con:
            users_count = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
            pending = con.execute("SELECT COUNT(*) n FROM users WHERE status='pending'").fetchone()["n"]
            approved = con.execute("SELECT COUNT(*) n FROM users WHERE status='approved'").fetchone()["n"]
            trades = con.execute("SELECT COUNT(*) n FROM trades").fetchone()["n"]
            volume = con.execute("SELECT COALESCE(SUM(total),0) n FROM trades").fetchone()["n"]
        await q.message.reply_text(
            "📊 د سیستم راپور\n\n"
            f"ټول غړي: {users_count}\nانتظار: {pending}\nتایید شوي: {approved}\n"
            f"Demo معاملات: {trades}\nDemo ټول حجم: {money(volume)} AFN"
        )

async def user_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    row = get_user(uid)
    if not row or row["status"] != "approved":
        await q.message.reply_text("دا برخه یوازې د تایید شویو غړو لپاره ده.")
        return
    action = q.data.split(":")[1]

    if action == "price":
        price = float(get_setting("silver_price", "0") or 0)
        updated = get_setting("silver_price_updated", "")
        if price <= 0:
            await q.message.reply_text("د سپینو زرو Demo نرخ لا اډمین نه دی ټاکلی.")
        else:
            await q.message.reply_text(
                f"🥈 اوسنی ثبت شوی Demo نرخ: {money(price)} AFN / ګرام\n"
                f"د تازه کېدو وخت: {updated}\n"
                "یادونه: دا نرخ د اډمین له خوا لاسي ثبت شوی، ژوندی بازار نرخ نه دی."
            )

    elif action == "account":
        with db() as con:
            a = con.execute("SELECT * FROM accounts WHERE telegram_id=?", (uid,)).fetchone()
        if not a:
            await q.message.reply_text("حساب ونه موندل شو.")
        else:
            await q.message.reply_text(
                "💰 ستاسو Demo حساب\n\n"
                f"Demo بیلانس: {money(a['demo_balance'])} AFN\n"
                f"سپین زر: {money(a['silver_grams'])} ګرام\n\n"
                "دا یوازې ازمایښتي ارزښتونه دي، حقیقي پیسې نه دي."
            )

    elif action == "history":
        with db() as con:
            rows = con.execute(
                "SELECT * FROM trades WHERE telegram_id=? ORDER BY id DESC LIMIT 10", (uid,)
            ).fetchall()
        if not rows:
            await q.message.reply_text("تاسو تر اوسه Demo معامله نه لرئ.")
        else:
            lines = ["📋 ستاسو وروستۍ Demo معاملې:"]
            for r in rows:
                side = "پېر" if r["side"] == "buy" else "پلور"
                lines.append(
                    f"#{r['id']} {side}: {money(r['grams'])}g × "
                    f"{money(r['price_per_gram'])} = {money(r['total'])} AFN"
                )
            await q.message.reply_text("\n".join(lines))

    elif action == "profile":
        await q.message.reply_text(
            f"👤 نوم: {row['full_name']}\nشمېره: {row['phone']}\n"
            f"هېواد: {row['country']}\nحالت: {row['status']}"
        )

    elif action == "rules":
        await q.message.reply_text(RULES)

    elif action == "contact":
        await q.message.reply_text("د اډمین سره د اړیکې لپاره د ربات له اډمین سره مستقیم تماس ونیسئ.")

async def begin_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    row = get_user(q.from_user.id)
    if not row or row["status"] != "approved":
        await q.message.reply_text("معامله یوازې د تایید شویو غړو لپاره ده.")
        return ConversationHandler.END
    side = q.data.split(":")[1]
    price = float(get_setting("silver_price", "0") or 0)
    if price <= 0:
        await q.message.reply_text("اډمین لا د سپینو زرو Demo نرخ نه دی ټاکلی.")
        return ConversationHandler.END
    context.user_data["trade_side"] = side
    context.user_data["trade_price"] = price
    await q.message.reply_text(
        f"د {'پېر' if side == 'buy' else 'پلور'} مقدار په ګرام ولیکئ.\n"
        f"ثبت شوی Demo نرخ: {money(price)} AFN / ګرام\n"
        "د لغوه کولو لپاره /cancel ولیکئ."
    )
    return TRADE_QTY

async def execute_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    qty = parse_positive(update.message.text)
    if qty is None or qty > 1000000:
        await update.message.reply_text("مهرباني وکړئ یو سم مثبت مقدار په ګرام ولیکئ.")
        return TRADE_QTY
    uid = update.effective_user.id
    side = context.user_data.get("trade_side")
    price = float(context.user_data.get("trade_price", 0))
    total = qty * price
    with db() as con:
        account = con.execute("SELECT * FROM accounts WHERE telegram_id=?", (uid,)).fetchone()
        if not account:
            await update.message.reply_text("حساب ونه موندل شو؛ له اډمین سره اړیکه ونیسئ.")
            return ConversationHandler.END
        balance = float(account["demo_balance"])
        silver = float(account["silver_grams"])
        if side == "buy":
            if balance < total:
                await update.message.reply_text(
                    f"Demo بیلانس کافي نه دی.\nاړین: {money(total)} AFN\nشته: {money(balance)} AFN"
                )
                return ConversationHandler.END
            con.execute(
                "UPDATE accounts SET demo_balance=?, silver_grams=? WHERE telegram_id=?",
                (balance - total, silver + qty, uid),
            )
        else:
            if silver < qty:
                await update.message.reply_text(
                    f"د پلور لپاره سپین زر کافي نه دي.\nشته: {money(silver)} ګرام"
                )
                return ConversationHandler.END
            con.execute(
                "UPDATE accounts SET demo_balance=?, silver_grams=? WHERE telegram_id=?",
                (balance + total, silver - qty, uid),
            )
        con.execute(
            "INSERT INTO trades(telegram_id,side,grams,price_per_gram,total,created_at) VALUES(?,?,?,?,?,?)",
            (uid, side, qty, price, total, now()),
        )
    verb = "پېر" if side == "buy" else "پلور"
    await update.message.reply_text(
        f"✅ Demo {verb} ترسره شو.\n\n"
        f"مقدار: {money(qty)} ګرام\nنرخ: {money(price)} AFN / ګرام\n"
        f"ټول: {money(total)} AFN\n\n"
        "یادونه: دا یوازې Simulation دی؛ هېڅ حقیقي پیسې یا سپین زر نه دي لېږدول شوي.",
        reply_markup=user_menu(),
    )
    context.user_data.pop("trade_side", None)
    context.user_data.pop("trade_price", None)
    return ConversationHandler.END

async def admin_text_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    flow = context.user_data.get("flow")
    text = update.message.text.strip()

    if flow == "admin_price":
        value = parse_positive(text)
        if value is None:
            await update.message.reply_text("سم مثبت نرخ ولیکئ.")
            return
        set_setting("silver_price", value)
        set_setting("silver_price_updated", now())
        context.user_data.pop("flow", None)
        await update.message.reply_text(
            f"✅ د Demo نرخ ثبت شو: {money(value)} AFN / ګرام",
            reply_markup=admin_menu(),
        )

    elif flow == "admin_balance_id":
        if not text.isdigit():
            await update.message.reply_text("یوازې عددي Telegram ID ولیکئ.")
            return
        target = int(text)
        if not get_user(target):
            await update.message.reply_text("دا ID په ثبت شویو غړو کې نشته. بل ID ولیکئ.")
            return
        context.user_data["balance_target"] = target
        context.user_data["flow"] = "admin_balance_amount"
        await update.message.reply_text(
            "د نوي Demo بیلانس اندازه په AFN ولیکئ.\n"
            "دا به د پخواني بیلانس پر ځای کېښودل شي، ورزیات به نه شي."
        )

    elif flow == "admin_balance_amount":
        value = parse_positive(text)
        if value is None:
            await update.message.reply_text("سم مثبت مقدار ولیکئ.")
            return
        target = context.user_data["balance_target"]
        with db() as con:
            con.execute(
                "INSERT INTO accounts(telegram_id,demo_balance) VALUES(?,?) "
                "ON CONFLICT(telegram_id) DO UPDATE SET demo_balance=excluded.demo_balance",
                (target, value),
            )
        context.user_data.pop("flow", None)
        context.user_data.pop("balance_target", None)
        await update.message.reply_text(
            f"✅ د کارن {target} Demo بیلانس {money(value)} AFN شو.",
            reply_markup=admin_menu(),
        )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("عملیه لغوه شوه.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def language_pashto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("پښتو ژبه فعاله ده.")

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing")
    if not ADMIN_ID:
        logging.warning("ADMIN_ID is missing or zero; admin panel will not be available.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    registration = ConversationHandler(
        entry_points=[CallbackQueryHandler(accept_rules, pattern=r"^accept_rules$")],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            PHONE: [MessageHandler(filters.CONTACT, get_phone), MessageHandler(filters.ALL, get_phone)],
            COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_country)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    trade_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(begin_trade, pattern=r"^trade:(buy|sell)$")],
        states={
            TRADE_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, execute_trade)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(registration)
    app.add_handler(trade_conversation)
    app.add_handler(CallbackQueryHandler(show_rules, pattern=r"^show_rules$"))
    app.add_handler(CallbackQueryHandler(register_button, pattern=r"^register$"))
    app.add_handler(CallbackQueryHandler(decline_rules, pattern=r"^decline_rules$"))
    app.add_handler(CallbackQueryHandler(admin_decision, pattern=r"^(approve|reject):"))
    app.add_handler(CallbackQueryHandler(admin_callbacks, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(user_callbacks, pattern=r"^user:"))
    app.add_handler(CallbackQueryHandler(language_pashto, pattern=r"^language_pashto$"))
    # Public reply-keyboard buttons must be handled before the admin text flow.
    app.add_handler(MessageHandler(
        filters.Regex(r"^(📜 د معاملاتو اصول|📝 ثبت‌نام|🌐 ژبه: پښتو)$"),
        main_menu_text,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text_flow))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    main()
