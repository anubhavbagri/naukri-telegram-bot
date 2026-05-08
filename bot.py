#!/usr/bin/env python3
"""
Naukri Auto-Update + Jobs Bot
─────────────────────────────────────────────────────────────────────────────
AUTONOMOUS:
  - 8:30–11:30 AM IST daily: rotate headline/summary/resume updates
  - 10:00 PM IST daily: fetch recommended jobs and send to Telegram

MANUAL COMMANDS:
  /ping      – open manual profile-update menu
  /testrun   – force-run the morning profile update right now (no delay)
  /jobnow    – force-fetch recommended jobs right now
  /search    – search jobs with optional filters
               usage: /search <keyword> [exp=N] [loc=City] [company=Name] [page=N]
               example: /search Java backend exp=2 loc=Hyderabad
  /cancel    – cancel any active flow

Required .env:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, USERNAME, PASSWORD
Optional .env:
  DEFAULT_RESUME_PATH  (default: assets/resume.pdf)
  TIMEZONE             (default: Asia/Kolkata)
  NAUKRI_DEFAULT_FAREA (farea filter for /search; default: empty = all)

State: scheduler_state.json  |  Content: src/config/content.py
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

from src.client.job_client import NaukriJobClient
from src.client.jop_classifier import JobFilterPipeline2
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
DEFAULT_FAREA = _env("NAUKRI_DEFAULT_FAREA", "")

# Morning update window: 08:30 IST + up to +3 h random jitter
_WINDOW_HOUR = 8
_WINDOW_MINUTE = 30
_WINDOW_JITTER_SECS = 180 * 60

# Evening recommended-jobs feed
_JOBS_HOUR = 22
_JOBS_MINUTE = 0
_MAX_RECOMMENDED = 25

STATE_FILE = Path("scheduler_state.json")

# ── Conversation states ───────────────────────────────────────────────────────
(
    CHOOSING,
    HEADLINE_INPUT,
    SUMMARY_INPUT,
    RESUME_AWAITING_FILE,
    QUESTIONNAIRE_Q,
) = range(5)

# ── Jobs cache ────────────────────────────────────────────────────────────────
def _cache_jobs(app, jobs: list) -> None:
    cache = app.bot_data.setdefault("jobs_cache", {})
    for j in jobs:
        cache[j.job_id] = j


def _get_cached_job(app, job_id: str):
    return app.bot_data.get("jobs_cache", {}).get(job_id)


def _mark_applied(app, job_id: str) -> None:
    app.bot_data.setdefault("applied_jobs", set()).add(job_id)


def _is_applied(app, job_id: str) -> bool:
    return job_id in app.bot_data.get("applied_jobs", set())


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


# Alias used in new job/apply code
_get_login_client = _get_client


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


# ─────────────────────────────────────────────────────────────────────────────
# JOB DISPLAY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _job_keyboard(job_id: str, source: str, apply_link: str, applied: bool = False) -> InlineKeyboardMarkup:
    if applied:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Applied", callback_data=f"noop:{job_id}"),
            InlineKeyboardButton("🔗 View Job", url=apply_link),
        ]])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Apply",    callback_data=f"apply:{job_id}:{source}"),
        InlineKeyboardButton("🔗 View Job", url=apply_link),
    ]])


def _format_job_text(job, idx: int = 0, total: int = 0) -> str:
    tags = ", ".join(job.tags[:8]) if job.tags else "—"
    counter = f"  [{idx}/{total}]" if total else ""
    return (
        f"<b>{job.title}</b>{counter}\n"
        f"🏢 {job.company}\n"
        f"📍 {job.location}   💼 {job.experience}\n"
        f"💰 {job.salary}\n"
        f"🕒 {job.posted_date}\n"
        f"🔧 {tags}"
    )


async def _send_job(bot, chat_id: int, job, idx: int = 0, total: int = 0, source: str = "recommended") -> None:
    text = _format_job_text(job, idx, total)
    keyboard = _job_keyboard(job.job_id, source, job.apply_link)
    if job.logo_url:
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=job.logo_url,
                caption=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return
        except Exception:
            pass
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# AUTONOMOUS: evening recommended jobs
# ─────────────────────────────────────────────────────────────────────────────

async def run_recommended_jobs(bot, app, *, force: bool = False) -> None:
    try:
        client = _get_login_client()
        client.login()
        jc = NaukriJobClient(client)
        jobs = jc.get_recommended_jobs()
        if not jobs:
            await bot.send_message(chat_id=ALLOWED_CHAT_ID, text="🔍 No recommended jobs found tonight.")
            return

        before = len(jobs)
        jobs = JobFilterPipeline2.filter_experience_only(jobs)
        logger.info("Recommended jobs: %d raw → %d after experience filter", before, len(jobs))

        jobs = [j for j in jobs if not _is_applied(app, j.job_id)]

        if not jobs:
            await bot.send_message(chat_id=ALLOWED_CHAT_ID, text="🔍 No new jobs found tonight.")
            return

        jobs = jobs[:_MAX_RECOMMENDED]
        _cache_jobs(app, jobs)

        await bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=f"🌙 <b>Recommended jobs tonight — {len(jobs)} openings</b>",
            parse_mode="HTML",
        )
        for i, job in enumerate(jobs, 1):
            await _send_job(bot, ALLOWED_CHAT_ID, job, idx=i, total=len(jobs), source="recommended")
            await asyncio.sleep(0.4)

        logger.info("Sent %d recommended jobs", len(jobs))

    except Exception as exc:
        logger.exception("Recommended jobs fetch failed")
        try:
            await bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=f"❌ <b>Recommended jobs failed:</b>\n<code>{exc}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# APPLY FLOW  (entry point: inline Apply button)
# ─────────────────────────────────────────────────────────────────────────────

async def on_apply_tap(update: Update, context) -> int:
    query = update.callback_query
    if not _is_authorised(update):
        await query.answer()
        return ConversationHandler.END

    await query.answer("⏳ Processing…")
    _, job_id, source = query.data.split(":", 2)
    job = _get_cached_job(context.application, job_id)

    if not job:
        await query.message.reply_text("⚠️ Job not found in cache. Try /jobnow or /search again.")
        return ConversationHandler.END

    msg = await query.message.reply_text("⏳ Authenticating and applying…")

    try:
        client = _get_login_client()
        client.login()
        jc = NaukriJobClient(client)
        data = jc.apply_job(job, source=source)
    except Exception as exc:
        logger.exception("Apply failed for job_id=%s", job_id)
        await msg.edit_text(f"❌ Apply failed: {exc}")
        return ConversationHandler.END

    job_result = (data.get("jobs") or [{}])[0]

    # External apply
    redirect = (
        job_result.get("applyRedirectUrl")
        or job_result.get("redirectUrl")
        or job_result.get("externalApplyURL")
    )
    if redirect:
        await msg.edit_text(
            f"🔗 <b>External apply required:</b>\n<a href=\"{redirect}\">Open application page</a>",
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
        return ConversationHandler.END

    # Questionnaire
    questionnaire = job_result.get("questionnaire")
    if questionnaire:
        from datetime import datetime as _dt
        sid = _dt.utcnow().strftime("%Y%m%d%H%M%S") + "0000000"
        context.user_data["q_state"] = {
            "job": job,
            "jc": jc,
            "questions": questionnaire,
            "q_idx": 0,
            "answers": {},
            "sid": sid,
            "source": source,
            "awaiting_text": False,
            "origin_message": query.message,
        }
        await msg.edit_text(
            f"❓ <b>{len(questionnaire)} question(s) required to complete this application.</b>",
            parse_mode="HTML",
        )
        return await _ask_next_question(update, context, use_new_message=True)

    # Direct success
    _mark_applied(context.application, job_id)
    try:
        await query.message.edit_reply_markup(
            reply_markup=_job_keyboard(job_id, source, job.apply_link, applied=True)
        )
    except Exception:
        pass
    await msg.edit_text(
        f"✅ <b>Applied!</b>\n🏢 {job.company}\n📌 {job.title}",
        parse_mode="HTML",
    )
    return ConversationHandler.END


def _questionnaire_radio_keyboard(options: dict) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(label, callback_data=f"qans:{key}")] for key, label in options.items()]
    buttons.append([InlineKeyboardButton("❌ Cancel apply", callback_data="qcancel")])
    return InlineKeyboardMarkup(buttons)


async def _ask_next_question(update: Update, context, use_new_message: bool = False) -> int:
    q_state = context.user_data.get("q_state", {})
    questions = q_state.get("questions", [])
    q_idx = q_state.get("q_idx", 0)

    if q_idx >= len(questions):
        return await _submit_questionnaire(update, context)

    q = questions[q_idx]
    q_text = q.get("questionName") or q.get("questionText") or f"Question {q_idx + 1}"
    q_type = q.get("questionType", "")
    options: dict = q.get("answerOption") or {}

    header = f"<b>Q{q_idx + 1}/{len(questions)}:</b> {q_text}"
    chat = update.callback_query.message if update.callback_query else update.message

    if q_type == "Radio Button" and options:
        keyboard = _questionnaire_radio_keyboard(options)
        if use_new_message:
            await chat.reply_text(header, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.callback_query.edit_message_text(header, parse_mode="HTML", reply_markup=keyboard)
    else:
        text = header + "\n\n<i>Type your answer:</i>"
        q_state["awaiting_text"] = True
        if use_new_message:
            await chat.reply_text(text, parse_mode="HTML")
        else:
            await update.callback_query.edit_message_text(text, parse_mode="HTML")

    return QUESTIONNAIRE_Q


async def on_questionnaire_radio(update: Update, context) -> int:
    query = update.callback_query
    if not _is_authorised(update):
        await query.answer()
        return ConversationHandler.END

    if query.data == "qcancel":
        await query.answer()
        await query.edit_message_text("❌ Application cancelled.")
        context.user_data.pop("q_state", None)
        return ConversationHandler.END

    await query.answer()
    answer_key = query.data[len("qans:"):]

    q_state = context.user_data.get("q_state", {})
    questions = q_state.get("questions", [])
    q_idx = q_state.get("q_idx", 0)

    if q_idx < len(questions):
        q_id = questions[q_idx].get("questionId")
        q_state["answers"][q_id] = answer_key
        q_state["q_idx"] = q_idx + 1

    await query.edit_message_text(f"✅ Recorded Q{q_idx + 1} answer.", parse_mode="HTML")
    return await _ask_next_question(update, context, use_new_message=True)


async def on_questionnaire_text(update: Update, context) -> int:
    if not _is_authorised(update):
        return ConversationHandler.END

    q_state = context.user_data.get("q_state", {})
    if not q_state.get("awaiting_text"):
        return QUESTIONNAIRE_Q

    questions = q_state.get("questions", [])
    q_idx = q_state.get("q_idx", 0)
    if q_idx < len(questions):
        q_id = questions[q_idx].get("questionId")
        q_state["answers"][q_id] = update.message.text.strip()
        q_state["q_idx"] = q_idx + 1
        q_state["awaiting_text"] = False

    return await _ask_next_question(update, context, use_new_message=True)


async def _submit_questionnaire(update: Update, context) -> int:
    q_state = context.user_data.pop("q_state", {})
    job = q_state.get("job")
    jc: NaukriJobClient = q_state.get("jc")
    answers = q_state.get("answers", {})
    sid = q_state.get("sid", "")
    source = q_state.get("source", "recommended")

    chat = update.callback_query.message if update.callback_query else update.message
    status_msg = await chat.reply_text("⏳ Submitting answers…")

    try:
        jc.apply_with_answers(job, answers, sid=sid, source=source)
        _mark_applied(context.application, job.job_id)
        origin_msg = q_state.get("origin_message")
        if origin_msg:
            try:
                await origin_msg.edit_reply_markup(
                    reply_markup=_job_keyboard(job.job_id, source, job.apply_link, applied=True)
                )
            except Exception:
                pass
        await status_msg.edit_text(
            f"✅ <b>Applied!</b>\n🏢 {job.company}\n📌 {job.title}",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("Questionnaire submit failed")
        await status_msg.edit_text(f"❌ Submit failed: {exc}")

    return ConversationHandler.END


async def on_apply_cancel(update: Update, context) -> int:
    if not _is_authorised(update):
        return ConversationHandler.END
    context.user_data.pop("q_state", None)
    await update.message.reply_text("Application cancelled.")
    return ConversationHandler.END


async def on_noop(update: Update, context) -> None:
    """Ack taps on the 'Applied' button without doing anything visible."""
    await update.callback_query.answer("Already applied ✅")


# ─────────────────────────────────────────────────────────────────────────────
# /jobnow  – force recommended jobs fetch
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_jobnow(update: Update, context) -> None:
    if not _is_authorised(update):
        return
    await update.message.reply_text("⏳ Fetching recommended jobs…")
    await run_recommended_jobs(context.bot, context.application, force=True)


# ─────────────────────────────────────────────────────────────────────────────
# /search  – job search with optional filters
# ─────────────────────────────────────────────────────────────────────────────

def _parse_search_args(args: list) -> dict:
    params = {"keyword": [], "exp": 0, "loc": "", "days": 0, "page": 1, "results": 50}
    for arg in args:
        if "=" in arg:
            k, v = arg.split("=", 1)
            k = k.lower()
            if k in ("exp", "experience"):
                try:
                    params["exp"] = int(v)
                except ValueError:
                    pass
            elif k in ("loc", "location"):
                params["loc"] = v
            elif k in ("days", "age"):
                try:
                    params["days"] = int(v)
                except ValueError:
                    pass
            elif k == "page":
                try:
                    params["page"] = int(v)
                except ValueError:
                    pass
            elif k in ("results", "n"):
                try:
                    params["results"] = int(v)
                except ValueError:
                    pass
        else:
            params["keyword"].append(arg)
    params["keyword"] = " ".join(params["keyword"])
    return params


async def cmd_search(update: Update, context) -> None:
    if not _is_authorised(update):
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: <code>/search keyword [exp=N] [loc=City] [days=N] [page=N] [results=N]</code>\n"
            "Examples:\n"
            "  <code>/search Java backend exp=2 loc=Hyderabad</code>\n"
            "  <code>/search Tekion days=7</code>",
            parse_mode="HTML",
        )
        return

    p = _parse_search_args(args)
    if not p["keyword"]:
        await update.message.reply_text(
            "⚠️ Provide a keyword, e.g. <code>/search Java backend</code>",
            parse_mode="HTML",
        )
        return

    status_parts = [f"<b>{p['keyword']}</b>"]
    if p["loc"]:
        status_parts.append(f"in <b>{p['loc']}</b>")
    if p["exp"]:
        status_parts.append(f"exp: <b>{p['exp']}y</b>")
    if p["days"]:
        status_parts.append(f"last <b>{p['days']} days</b>")
    status = await update.message.reply_text(
        f"🔍 Searching: {' | '.join(status_parts)}",
        parse_mode="HTML",
    )

    try:
        client = _get_login_client()
        client.login()
        jc = NaukriJobClient(client)
        jobs = jc.search_jobs(
            keyword=p["keyword"],
            location=p["loc"],
            experience=p["exp"],
            job_age=p["days"],
            page=p["page"],
            results_per_page=p["results"],
        )
    except Exception as exc:
        logger.exception("Search failed")
        await status.edit_text(f"❌ Search failed: {exc}")
        return

    if not jobs:
        await status.edit_text("🔍 No jobs found for that query.")
        return

    jobs = [j for j in jobs if not _is_applied(context.application, j.job_id)]
    if not jobs:
        await status.edit_text("🔍 No new jobs (all already applied).")
        return

    _cache_jobs(context.application, jobs)
    await status.edit_text(f"✅ Found <b>{len(jobs)}</b> jobs:", parse_mode="HTML")

    for i, job in enumerate(jobs, 1):
        await _send_job(context.bot, update.effective_chat.id, job, idx=i, total=len(jobs), source="search")
        await asyncio.sleep(0.3)


# ── Application lifecycle ─────────────────────────────────────────────────────

async def _on_startup(app: Application) -> None:
    tz = pytz.timezone(TIMEZONE)
    scheduler = AsyncIOScheduler(timezone=tz)

    # Morning: profile update at 08:30 IST + 0–180 min random jitter
    scheduler.add_job(
        run_scheduled_update,
        trigger="cron",
        hour=_WINDOW_HOUR,
        minute=_WINDOW_MINUTE,
        args=[app.bot],
        kwargs={"force": False},
    )

    # Evening: recommended jobs at 22:00 IST
    scheduler.add_job(
        run_recommended_jobs,
        trigger="cron",
        hour=_JOBS_HOUR,
        minute=_JOBS_MINUTE,
        args=[app.bot, app],
        kwargs={"force": False},
    )

    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    logger.info(
        "Scheduler running — update %02d:%02d %s (+0–180 min), jobs %02d:%02d %s",
        _WINDOW_HOUR, _WINDOW_MINUTE, TIMEZONE,
        _JOBS_HOUR, _JOBS_MINUTE, TIMEZONE,
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

    # ── ConversationHandler 1: manual profile update (/ping) ─────────────────
    manual_conv = ConversationHandler(
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
                    lambda u, c: u.message.reply_text("⚠️ Send a PDF or /cancel."),
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", on_cancel)],
        per_chat=True,
        allow_reentry=True,
    )

    # ── ConversationHandler 2: apply + questionnaire (triggered by Apply button) ─
    apply_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_apply_tap, pattern=r"^apply:")],
        states={
            QUESTIONNAIRE_Q: [
                CallbackQueryHandler(on_questionnaire_radio, pattern=r"^(qans:|qcancel)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_questionnaire_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", on_apply_cancel)],
        per_chat=True,
        allow_reentry=True,
    )

    app.add_handler(manual_conv)
    app.add_handler(apply_conv)

    # Standalone commands (work outside any active conversation)
    app.add_handler(CommandHandler("testrun", cmd_testrun))
    app.add_handler(CommandHandler("jobnow",  cmd_jobnow))
    app.add_handler(CommandHandler("search",  cmd_search))
    app.add_handler(CallbackQueryHandler(on_noop, pattern=r"^noop:"))

    logger.info("Bot starting — polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

