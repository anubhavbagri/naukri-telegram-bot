"""
Gmail SMTP email sender (App Password auth — no OAuth flow required).

Usage
─────
Call send_email() with the fields populated.  The function will:
  1. Render the subject & body templates (position_phrase / jobid_line auto-built)
  2. Attach the resume PDF
  3. Send via Gmail SMTP SSL on port 465

Required .env keys
──────────────────
  GMAIL_FROM          – your Gmail address  (e.g. you@gmail.com)
  GMAIL_APP_PASSWORD  – 16-char App Password from https://myaccount.google.com/apppasswords
  GMAIL_SENDER_NAME   – display name shown in From: header (e.g. "Anubhav Bagri")
"""

import logging
import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger(__name__)

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 465  # SSL


def _cfg(key: str) -> str:
    """Read env var at call time (after load_dotenv has run)."""
    return os.getenv(key, "").split("#")[0].strip()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _render(
    template: str,
    name: str,
    company: str,
    position: str,
    jobid: str,
) -> str:
    """Substitute all template placeholders."""
    position_phrase = (
        f"for the **{position}** role"
        if position
        else "for a suitable opportunity"
    )
    jobid_line = f"(Job ID: {jobid})\n" if jobid else ""
    position_or_role = position if position else "Software Engineer"

    return template.format(
        name=name,
        company=company,
        position_phrase=position_phrase,
        jobid_line=jobid_line,
        position_or_role=position_or_role,
    )


# ── Public API ────────────────────────────────────────────────────────────────

def send_email(
    *,
    to: str,
    subject_template: str,
    body_template: str,
    name: str,
    company: str,
    position: str = "",
    jobid: str = "",
    resume_path: str,
) -> None:
    """
    Render templates and send email with resume attached.

    Raises
    ------
    RuntimeError  – if GMAIL_FROM or GMAIL_APP_PASSWORD are not set in .env
    smtplib.SMTPException / OSError  – propagated on send/file failure
    """
    gmail_from = _cfg("GMAIL_FROM")
    gmail_app_password = _cfg("GMAIL_APP_PASSWORD")
    gmail_sender_name = _cfg("GMAIL_SENDER_NAME")

    if not gmail_from or not gmail_app_password:
        raise RuntimeError(
            "GMAIL_FROM and GMAIL_APP_PASSWORD must be set in .env  "
            "(see setup instructions in README)"
        )

    subject = _render(subject_template, name, company, position, jobid)
    body = _render(body_template, name, company, position, jobid)

    from_header = (
        f"{gmail_sender_name} <{gmail_from}>"
        if gmail_sender_name
        else gmail_from
    )

    msg = MIMEMultipart()
    msg["From"] = from_header
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    # Attach resume
    resume = Path(resume_path)
    if not resume.is_file():
        raise FileNotFoundError(f"Resume not found: {resume_path}")
    with open(resume, "rb") as fh:
        attachment = MIMEApplication(fh.read(), Name=resume.name)
    attachment["Content-Disposition"] = f'attachment; filename="{resume.name}"'
    msg.attach(attachment)

    logger.info("Sending email to %s | subject: %s", to, subject)
    with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT) as server:
        server.login(gmail_from, gmail_app_password)
        server.sendmail(gmail_from, to, msg.as_string())
    logger.info("Email sent to %s", to)
