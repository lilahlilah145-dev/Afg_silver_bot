import os
import sqlite3
import logging
from datetime import datetime, timezone

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "afg_silver.sqlite3")

NAME, PHONE, COUNTRY = range(3)

RULES = """
🥈 AFG SILVER EXCHANGE — د ګډون اصول

۱. د حساب جوړولو لپاره باید خپل سم نوم، د اړیکې شمېره او د اوسېدو هېواد ورکړئ.
۲. تر هغه چې اډمین ستاسو ثبت‌نام تایید نه کړي، د معاملې اجازه نشته.
۳. د سپینو زرو د نرخ سرچینه، د نرخ د تازه کېدو وخت او د معاملې د منلو حدود باید د هرې معاملې پر مهال ښکاره وي.
۴. د مارجین، خبرداري، کال‌مارجین، حراج، کمېشن او جبري تصفیې دقیق فورمولونه به د معاملاتو له فعالېدو مخکې په جلا سند کې ښکاره او منل کېږي.
۵. د مارجین او حراج په اړه د مخکې اعلان شوو اصولو مطابق اجرا شوې معامله د مشتری د خوښې له مخې نه لغوه کېږي.
۶. د حساب د محاسبې تېروتنه، غیرمجاز لاسرسی، د نرخ د معلوماتو خرابوالی یا تخنیکي ستونزه د څېړنې او اعتراض وړ ده.
۷. په بازار کې د نرخ چټک بدلون، د نرخ د سرچینې ځنډ او د حراج پر مهال د مقابل لوري نه موندل د مالي خطر سبب کېدای شي.
۸. تر هغه چې د سیستم ازمایښتي پړاو، د قانوني شرایطو ارزونه او د مالي خوندیتوب ازموینې بشپړې شوې نه وي، دا ربات یوازې د ثبت‌نام او ازمایښتي حساب لپاره دی؛ حقیقي پیسې او حقیقي معاملات مه لېږئ.

د «منم» تڼۍ په کېکاږلو سره تاییدوئ چې پورته اصول مو لوستي او درک کړي دي.
"""

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

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

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 د معاملاتو اصول", callback_data="show_rules")],
        [InlineKeyboardButton("📝 ثبت‌نام", callback_data="register")],
        [InlineKeyboardButton("🌐 ژبه: پښتو", callback_data="language_pashto")]
    ])

def rules_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 اصول منم", callback_data="accept_rules")],
        [InlineKeyboardButton("🔴 نه منم", callback_data="decline_rules")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with db() as con:
        row = con.execute("SELECT status FROM users WHERE telegram_id=?", (user.id,)).fetchone()
    if row and row["status"] == "approved":
        await update.message.reply_text(
            "ښه راغلاست! ستاسو ثبت‌نام تایید شوی دی.\n"
            "اوس مهال یوازې د حساب او ثبت‌نام ازمایښتي برخه فعاله ده؛ حقیقي معامله نشته."
        )
    elif row:
        await update.message.reply_text(
            "ستاسو ثبت‌نام ثبت شوی او د اډمین تایید ته منتظر دی.",
            reply_markup=main_menu()
        )
    else:
        await update.message.reply_text(
            "🥈 AFG SILVER EXCHANGE ته ښه راغلاست!\n\n"
            "د ګډون لپاره لومړی اصول ولولئ، بیا ثبت‌نام وکړئ.",
            reply_markup=main_menu()
        )

async def show_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(RULES, reply_markup=rules_keyboard())

async def accept_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["rules_accepted"] = True
    context.user_data["accepted_at"] = datetime.now(timezone.utc).isoformat()
    await q.message.reply_text(
        "مننه. اوس د ثبت‌نام لپاره خپل بشپړ نوم ولیکئ:",
        reply_markup=ReplyKeyboardRemove()
    )
    return NAME

async def decline_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("د اصولو له منلو پرته ثبت‌نام او ګډون ممکن نه دی.", reply_markup=main_menu())
    return ConversationHandler.END

async def register_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("لومړی د معاملاتو اصول ولولئ او د «اصول منم» تڼۍ کېکاږئ.")
    await q.message.reply_text(RULES, reply_markup=rules_keyboard())

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) < 3 or len(name) > 100:
        await update.message.reply_text("مهرباني وکړئ سم بشپړ نوم ولیکئ (۳ تر ۱۰۰ تورو).")
        return NAME
    context.user_data["full_name"] = name
    contact_button = KeyboardButton("📱 زما د اړیکې شمېره شریکول", request_contact=True)
    await update.message.reply_text(
        "اوس د موبایل شمېره د لاندې تڼۍ له لارې شریکه کړئ:",
        reply_markup=ReplyKeyboardMarkup([[contact_button]], resize_keyboard=True, one_time_keyboard=True)
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
        await update.message.reply_text("مهرباني وکړئ د هېواد نوم سم ولیکئ.")
        return COUNTRY

    user = update.effective_user
    data = context.user_data
    now = datetime.now(timezone.utc).isoformat()
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
        """, (user.id, user.username, data["full_name"], data["phone"], country,
              data.get("accepted_at", now), now))

    await update.message.reply_text(
        "✅ ستاسو غوښتنه ثبت شوه.\n"
        "د اډمین تر تایید پورې معامله نه شئ کولای.",
        reply_markup=ReplyKeyboardRemove()
    )

    if ADMIN_ID:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ تایید", callback_data=f"approve:{user.id}"),
            InlineKeyboardButton("❌ رد", callback_data=f"reject:{user.id}")
        ]])
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
            reply_markup=keyboard
        )
    else:
        await update.message.reply_text(
            "یادونه: د اډمین ID لا نه دی تنظیم شوی؛ د تایید خبرتیا نه شي لېږل کېدای."
        )
    context.user_data.clear()
    return ConversationHandler.END

async def admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        await q.message.reply_text("دا واک یوازې د ربات اډمین لري.")
        return
    action, target = q.data.split(":")
    target_id = int(target)
    status = "approved" if action == "approve" else "rejected"
    with db() as con:
        con.execute("UPDATE users SET status=? WHERE telegram_id=?", (status, target_id))
    await q.edit_message_reply_markup(reply_markup=None)
    await q.message.reply_text(f"د کارن {target_id} غوښتنه: {status}")
    try:
        msg = "✅ ستاسو ثبت‌نام د اډمین له خوا تایید شو." if status == "approved" else "❌ ستاسو د ثبت‌نام غوښتنه ونه منل شوه."
        await context.bot.send_message(chat_id=target_id, text=msg)
    except Exception:
        logging.exception("Could not notify user")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("ثبت‌نام لغوه شو.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(accept_rules, pattern="^accept_rules$")],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            PHONE: [MessageHandler(filters.CONTACT, get_phone), MessageHandler(filters.ALL, get_phone)],
            COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_country)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(show_rules, pattern="^show_rules$"))
    app.add_handler(CallbackQueryHandler(register_button, pattern="^register$"))
    app.add_handler(CallbackQueryHandler(decline_rules, pattern="^decline_rules$"))
    app.add_handler(CallbackQueryHandler(admin_decision, pattern="^(approve|reject):"))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer("پښتو ژبه فعاله ده."), pattern="^language_pashto$"))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
