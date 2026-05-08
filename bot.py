#!/usr/bin/env python3
"""
Naukri Auto-Update Bot
─────────────────────────────────────────────────────────────────────────────
Autonomous daily update, random time 8:30–11:30 AM IST, rotating cycle:
  H → H → S → H → H → S → ...   (H = headline, S = summary)
  + resume refresh every 4 updates.

A Telegram notification is sent automatically after each update completes.

Manual commands:
  /ping     – show the manual update menu (interactive, any time)
  /testrun  – immediately force-run the scheduled update (skip delay + date guard)
  /cancel   – cancel any in-progress manual flow

Required .env keys:
  TELEGRAM_BOT_TOKEN   – token from @BotFather
  TELEGRAM_CHAT_ID     – your personal chat id (integer)
  USERNAME             – Naukri login email
  PASSWORD             – Naukri password
  DEFAULT_RESUME_PATH  – path to the default resume PDF
                         (optional, default: assets/resume.pdf)
  TIMEZONE             – tz database name (optional, default: Asia/Kolkata)

State file: scheduler_state.json (auto-created, gitignored)
Content:    src/config/content.py
"""

import asyncio
import json
import logging
import os
import random
import tempfile
from datetime import date
from pathlib import Path

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
from src.config.content import HEADLINES, SUMMARIES

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
def _env(key: str, default: str = "") -> str:
    """Strip inline comments from .env values (e.g. 'val  # note' → 'val')."""
    return os.getenv(key, default).split("#")[0].strip()


BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
_raw_chat_id = _env("TELEGRAM_CHAT_ID")
ALLOWED_CHAT_ID: int = int(_raw_chat_id) if _raw_chat_id.lstrip("-").isdigit() else 0
DEFAULT_RESUME_PATH = _env("DEFAULT_RESUME_PATH", "assets/resume.pdf")
TIMEZONE = _env("TIMEZONE", "Asia/Kolkata")

# Scheduler fires at 08:30 IST; the job then sleeps a random 0–180 min
_WINDOW_HOUR = 8
_WINDOW_MINUTE = 30
_WINDOW_JITTER_SECS = 180 * 60  # 3-hour window

STATE_FILE = Path("scheduler_state.json")

# ── Conversation states (manual flow) ────────────────────────────────────────
CHOOSING, HEADLINE_INPUT, SUMMARY_INPUT, RESUME_AWAITING_FILE = range(4)


# ── State management ──────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "update_count": 0,
        "headline_index": 0,
        "summary_index": 0,
        "last_run_date": None,
    }


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _peek_next_action(state: dict) -> tuple:
    """
    Return (update_type, also_resume) without mutating state.

    Cycle:  update_count % 3 → 0 or 1 = headline, 2 = summary
    Resume: every 4th update  → (update_count + 1) % 4 == 0
    """
    count = state["update_count"]
    update_type = "headline" if count % 3 in (0, 1) else "summary"
    also_resume = (count + 1) % 4 == 0
    return update_type, also_resume


def _consume_next_content(state: dict, update_type: str) -> str:
    """Advance the appropriate index and return the content string."""
    if update_type == "headline":
        idx = state["headline_index"] % len(HEADLINES)
        state["headline_index"] = idx + 1
        return HEADLINES[idx]
    idx = state["summary_index"] % len(SUMMARIES)
    state["summary_index"] = idx + 1
    return SUMMARIES[idx]


# ── Naukri client helper ──────────────────────────────────────────────────────

def _get_client() -> NaukriLoginClient:
    username = _env("USERNAME")
    password = _env("PASSWORD")
    if not username or not password:
        raise RuntimeError("USERNAME / PASSWORD not set in .env")
    return NaukriLoginClient(username, password)


# ── Auth check ────────────────────────────────────────────────────────────────

def _is_authorised(update: Update) -> bool:
    chat_id = update.effective_chat.id if update.effective_chat else None
    return chat_id == ALLOWED_CHAT_ID


# ── Keyboards ─────────────────────────────────────────────────────────────────

def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄  Update Resume",   callback_data="opt_resume")],
        [InlineKeyboardButton("✏️  Update Headline", callback_data="opt_headline")],
        [InlineKeyboardButton("📝  Update Summary",  callback_data="opt_summary")],
    ])


def _resume_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬆️  Upload a PDF now",        callback_data="resume_upload")],
        [InlineKeyboardButton("📁  Use default (repo file)",  callback_data="resume_default")],
        [InlineKeyboardButton("« Back",                       callback_data="back")],
    ])


# ── Autonomous scheduled update ───────────────────────────────────────────────

async def run_scheduled_update(bot, *, force: bool = False) -> None:
    """
    Perform the next scheduled Naukri update and send a Telegram notification.

    force=True  – skip the random delay and the "already ran today" guard
                  (used by /testrun command).
    """
    today = date.today().isoformat()
    state = _load_state()

    if not force and state.get("last_run_date") == today:
        logger.info("Already ran today (%s) — skipping.", today)
        return

    if not force:
        delay = random.randint(0, _WINDOW_JITTER_SECS)
        logger.info("Waiting %d min %d s before update…", delay // 60, delay % 60)
        await asyncio.sleep(delay)

    update_type, also_resume = _peek_next_action(state)
    content = _consume_next_content(state, update_type)

    parts: list[str] = []
    try:
        client = _get_client()
        client.login()
        logger.info("Logged in — running %s update", update_type)

        if update_type == "headline":
            client.update_profile(headline=content)
            parts.append(f"✏️ <b>Headline updated:</b>\n<i>{content}</i>")
        else:
            client.update_profile(summary=content)
            parts.append(f"📝 <b>Summary updated:</b>\n<i>{content}</i>")

        if also_resume:
            if os.path.isfile(DEFAULT_RESUME_PATH):
                client.update_resume(DEFAULT_RESUME_PATH)
                parts.append("📄 <b>Resume refreshed</b> (4-update cycle)")
            else:
                parts.append(
                    f"⚠️ Resume refresh skipped — "
                    f"<code>{DEFAULT_RESUME_PATH}</code> not found"
                )

        state["update_count"] += 1
        state["last_run_date"] = today
        _save_state(state)

        text = "✅ <b>Naukri auto-update complete</b>\n\n" + "\n\n".join(parts)
        await bot.send_message(chat_id=ALLOWED_CHAT_ID, text=text, parse_mode="HTML")
        logger.info(
            "Scheduled update done — type=%s, update_count now %d",
            update_type, state["update_count"],
        )

    except Exception as exc:
        logger.exception("Scheduled update failed")
        try:
            await bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=f"❌ <b>Scheduled update failed:</b>\n<code>{exc}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ── Manual command handlers ───────────────────────────────────────────────────

async def cmd_ping(update: Update, context) -> int:
    """Show the manual update menu."""
    if not _is_authorised(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 What would you like to update on Naukri manually?",
        reply_markup=_main_keyboard(),
    )
    return CHOOSING


async def cmd_testrun(update: Update, context) -> int:
    """Force-run the scheduled update immediately (no delay, no date guard)."""
    if not _is_authorised(update):
        return ConversationHandler.END
    await update.message.reply_text("⏳ Running scheduled update now (forced, no delay)…")
    await run_scheduled_update(context.bot, force=True)
    return ConversationHandler.END


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
            "What would you like to update manually?",
            reply_markup=_main_keyboard(),
        )
        return CHOOSING

    if data == "resume_default":
        if not os.path.isfile(DEFAULT_RESUME_PATH):
            await query.edit_message_text(
                f"❌ Default resume not found at <code>{DEFAULT_RESUME_PATH}</code>.",
                parse_mode="HTML",
            )
            return ConversationHandler.END
        await query.edit_message_text("⏳ Authenticating and uploading resume…")
        try:
            client = _get_client()
            client.login()
            result = client.update_resume(DEFAULT_RESUME_PATH)
            await query.edit_message_text(
                f"✅ Resume updated!\n\n<code>{result}</code>", parse_mode="HTML"
            )
        except Exception as exc:
            logger.exception("Manual default resume update failed")
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
        logger.exception("Manual headline update failed")
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
        logger.exception("Manual summary update failed")
        await msg.edit_text(f"❌ Failed: {exc}")
    return ConversationHandler.END


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
        await msg.edit_text(
            f"✅ Resume updated!\n\n<code>{result}</code>", parse_mode="HTML"
        )
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
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ── Application lifecycle ─────────────────────────────────────────────────────

async def _on_startup(app: Application) -> None:
    tz = pytz.timezone(TIMEZONE)
    scheduler = AsyncIOScheduler(timezone=tz)
    # Fires at 08:30 IST; the job itself sleeps a random 0–180 min within the window
    scheduler.add_job(
        run_scheduled_update,
        trigger="cron",
        hour=_WINDOW_HOUR,
        minute=_WINDOW_MINUTE,
        args=[app.bot],
        kwargs={"force": False},
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    logger.info(
        "Scheduler running — auto-update fires at %02d:%02d %s (+0–180 min random delay)",
        _WINDOW_HOUR, _WINDOW_MINUTE, TIMEZONE,
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
            CommandHandler("ping", cmd_ping),
            CommandHandler("testrun", cmd_testrun),
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
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    lambda u, c: u.message.reply_text(
                        "⚠️ Send a PDF or /cancel to abort."
                    ),
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", on_cancel),
        ],
        per_chat=True,
        allow_reentry=True,
    )

    app.add_handler(conv)
    # /testrun also works outside an active conversation
    app.add_handler(CommandHandler("testrun", cmd_testrun))

    logger.info("Bot starting — polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

