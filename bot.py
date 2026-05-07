#!/usr/bin/env python3
"""
Naukri Telegram Bot
─────────────────────────────────────────────────────────────────────────────
Daily ping at 10 AM → inline keyboard → update resume / headline / summary.

Required .env keys:
  TELEGRAM_BOT_TOKEN   – token from @BotFather
  TELEGRAM_CHAT_ID     – your personal chat id (integer)
  USERNAME             – Naukri login email
  PASSWORD             – Naukri password
  DEFAULT_RESUME_PATH  – path to the default resume PDF (optional,
                         defaults to "assets/resume.pdf")
  TIMEZONE             – tz database name (optional, default "Asia/Kolkata")
  PING_HOUR            – 24-h hour for daily ping (optional, default 10)
  PING_MINUTE          – minute for daily ping (optional, default 0)
"""

import logging
import os
import tempfile

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from src.client.naukri_client import NaukriLoginClient

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_raw_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
ALLOWED_CHAT_ID: int = int(_raw_chat_id) if _raw_chat_id.lstrip("-").isdigit() else 0
DEFAULT_RESUME_PATH = os.getenv("DEFAULT_RESUME_PATH", "assets/resume.pdf")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")
PING_HOUR = int(os.getenv("PING_HOUR", "10"))
PING_MINUTE = int(os.getenv("PING_MINUTE", "0"))

# ── Conversation states ───────────────────────────────────────────────────────
CHOOSING, HEADLINE_INPUT, SUMMARY_INPUT, RESUME_AWAITING_FILE = range(4)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_client() -> NaukriLoginClient:
    username = os.getenv("USERNAME")
    password = os.getenv("PASSWORD")
    if not username or not password:
        raise RuntimeError("USERNAME / PASSWORD not set in .env")
    return NaukriLoginClient(username, password)


def _is_authorised(update: Update) -> bool:
    chat_id = update.effective_chat.id if update.effective_chat else None
    return chat_id == ALLOWED_CHAT_ID


def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄  Update Resume",   callback_data="opt_resume")],
        [InlineKeyboardButton("✏️  Update Headline", callback_data="opt_headline")],
        [InlineKeyboardButton("📝  Update Summary",  callback_data="opt_summary")],
    ])


def _resume_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬆️  Upload a PDF now",       callback_data="resume_upload")],
        [InlineKeyboardButton("📁  Use default (repo file)", callback_data="resume_default")],
        [InlineKeyboardButton("« Back",                      callback_data="back")],
    ])


# ── Entry point / /start ──────────────────────────────────────────────────────

async def cmd_start(update: Update, context) -> int:
    if not _is_authorised(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 What would you like to update on Naukri today?",
        reply_markup=_main_keyboard(),
    )
    return CHOOSING


async def cmd_ping(update: Update, context) -> int:
    """Manually trigger the daily ping — useful for local testing."""
    if not _is_authorised(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "🌅 Good morning! What would you like to update on Naukri today?",
        reply_markup=_main_keyboard(),
    )
    return CHOOSING


# ── Inline-button router ──────────────────────────────────────────────────────

async def on_choice(update: Update, context) -> int:
    query = update.callback_query
    if not _is_authorised(update):
        await query.answer()
        return ConversationHandler.END

    await query.answer()
    data = query.data

    if data == "opt_resume":
        await query.edit_message_text(
            "How would you like to update your resume?",
            reply_markup=_resume_keyboard(),
        )
        return CHOOSING

    if data == "back":
        await query.edit_message_text(
            "What would you like to update on Naukri today?",
            reply_markup=_main_keyboard(),
        )
        return CHOOSING

    if data == "resume_default":
        if not os.path.isfile(DEFAULT_RESUME_PATH):
            await query.edit_message_text(
                f"❌ Default resume not found at `{DEFAULT_RESUME_PATH}`.\n"
                "Set DEFAULT_RESUME_PATH in .env or upload a file instead.",
                parse_mode="Markdown",
            )
            return ConversationHandler.END

        await query.edit_message_text("⏳ Authenticating and updating resume…")
        try:
            client = _get_client()
            client.login()
            result = client.update_resume(DEFAULT_RESUME_PATH)
            await query.edit_message_text(f"✅ Resume updated!\n\n`{result}`", parse_mode="Markdown")
        except Exception as exc:
            logger.exception("Default resume update failed")
            await query.edit_message_text(f"❌ Failed: {exc}")
        return ConversationHandler.END

    if data == "resume_upload":
        await query.edit_message_text("📎 Send me your resume now (PDF only).")
        return RESUME_AWAITING_FILE

    if data == "opt_headline":
        await query.edit_message_text("✏️ Send me your new headline:")
        return HEADLINE_INPUT

    if data == "opt_summary":
        await query.edit_message_text("📝 Send me your new summary:")
        return SUMMARY_INPUT

    return CHOOSING


# ── Text handlers ─────────────────────────────────────────────────────────────

async def on_headline(update: Update, context) -> int:
    if not _is_authorised(update):
        return ConversationHandler.END
    headline = update.message.text.strip()
    msg = await update.message.reply_text("⏳ Authenticating and updating headline…")
    try:
        client = _get_client()
        client.login()
        client.update_profile(headline=headline)
        await msg.edit_text("✅ Headline updated successfully!")
    except Exception as exc:
        logger.exception("Headline update failed")
        await msg.edit_text(f"❌ Failed: {exc}")
    return ConversationHandler.END


async def on_summary(update: Update, context) -> int:
    if not _is_authorised(update):
        return ConversationHandler.END
    summary = update.message.text.strip()
    msg = await update.message.reply_text("⏳ Authenticating and updating summary…")
    try:
        client = _get_client()
        client.login()
        client.update_profile(summary=summary)
        await msg.edit_text("✅ Summary updated successfully!")
    except Exception as exc:
        logger.exception("Summary update failed")
        await msg.edit_text(f"❌ Failed: {exc}")
    return ConversationHandler.END


# ── Document handler (manual resume upload) ───────────────────────────────────

async def on_resume_file(update: Update, context) -> int:
    if not _is_authorised(update):
        return ConversationHandler.END

    doc = update.message.document
    if not doc or doc.mime_type != "application/pdf":
        await update.message.reply_text("⚠️ Please send a PDF file.")
        return RESUME_AWAITING_FILE

    msg = await update.message.reply_text("⏳ Downloading and uploading resume…")
    tmp_path = None
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)

        client = _get_client()
        client.login()
        result = client.update_resume(tmp_path)
        await msg.edit_text(f"✅ Resume updated!\n\n`{result}`", parse_mode="Markdown")
    except Exception as exc:
        logger.exception("Manual resume upload failed")
        await msg.edit_text(f"❌ Failed: {exc}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
    return ConversationHandler.END


# ── Cancel fallback ───────────────────────────────────────────────────────────

async def on_cancel(update: Update, context) -> int:
    if not _is_authorised(update):
        return ConversationHandler.END
    await update.message.reply_text("Cancelled. Send /start to begin again.")
    return ConversationHandler.END


# ── Daily ping ────────────────────────────────────────────────────────────────

async def send_daily_ping(bot) -> None:
    try:
        await bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text="🌅 Good morning! What would you like to update on Naukri today?",
            reply_markup=_main_keyboard(),
        )
        logger.info("Daily ping sent to chat_id=%s", ALLOWED_CHAT_ID)
    except Exception:
        logger.exception("Failed to send daily ping")


# ── Application lifecycle hooks ───────────────────────────────────────────────

async def _on_startup(app: Application) -> None:
    tz = pytz.timezone(TIMEZONE)
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        send_daily_ping,
        trigger="cron",
        hour=PING_HOUR,
        minute=PING_MINUTE,
        args=[app.bot],
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    logger.info(
        "Scheduler started — daily ping at %02d:%02d %s",
        PING_HOUR, PING_MINUTE, TIMEZONE,
    )


async def _on_shutdown(app: Application) -> None:
    scheduler = app.bot_data.get("scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")
    if not ALLOWED_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID is not set or invalid in .env")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("ping", cmd_ping),
            # daily ping buttons also kick off the conversation
            CallbackQueryHandler(on_choice, pattern="^opt_"),
        ],
        states={
            CHOOSING: [
                CallbackQueryHandler(on_choice),
            ],
            HEADLINE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_headline),
            ],
            SUMMARY_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_summary),
            ],
            RESUME_AWAITING_FILE: [
                MessageHandler(filters.Document.PDF, on_resume_file),
                # allow cancellation via text during upload wait
                MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: (
                    u.message.reply_text("⚠️ I'm waiting for a PDF. Send /cancel to abort.")
                )),
            ],
        },
        fallbacks=[CommandHandler("cancel", on_cancel)],
        per_chat=True,
        allow_reentry=True,
    )

    app.add_handler(conv)

    logger.info("Bot starting — polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
