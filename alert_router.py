"""Central alert router for the Family Agent Platform.

Routes operational alerts to configured channels (Telegram, email). Alerts are
best-effort: a failure to send must never crash the critical path.
"""

import os
import smtplib
import traceback
from email.mime.text import MIMEText
from typing import Optional

import httpx
from loguru import logger

from exceptions import AppError


def _telegram_creds() -> tuple[Optional[str], Optional[str]]:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or token.strip().lower().startswith("your") or not chat_id:
        return None, None
    return token, chat_id


def _email_creds() -> tuple[Optional[str], Optional[str], Optional[str]]:
    sender = os.getenv("NOTIFIER_EMAIL")
    password = os.getenv("NOTIFIER_PASSWORD")
    recipient = os.getenv("EMAIL_RECIPIENT")
    if not sender or not password or not recipient:
        return None, None, None
    if "your" in (sender + password + recipient).lower():
        return None, None, None
    return sender, password, recipient


def _format_alert(
    level: str,
    component: str,
    message: str,
    error: Optional[AppError] = None,
    request_id: Optional[str] = None,
) -> str:
    lines = [
        f"[Koko Alert] {level}",
        f"Component: {component}",
        f"Request ID: {request_id or 'unknown'}",
        f"Message: {message}",
    ]
    if error and error.context:
        for key, value in error.context.items():
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


async def send_telegram(text: str) -> bool:
    """Best-effort Telegram alert."""
    token, chat_id = _telegram_creds()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json={"chat_id": chat_id, "text": text[:4000]})
            response.raise_for_status()
        return True
    except Exception:
        logger.exception("Telegram alert failed")
        return False


def send_email(subject: str, body: str) -> bool:
    """Best-effort email alert via Gmail SMTP."""
    sender, password, recipient = _email_creds()
    if not sender or not password or not recipient:
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recipient
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, [recipient], msg.as_string())
        return True
    except Exception:
        logger.exception("Email alert failed")
        return False


async def alert(
    level: str,
    component: str,
    message: str,
    error: Optional[AppError] = None,
    request_id: Optional[str] = None,
    exc: Optional[BaseException] = None,
) -> None:
    """Route an operational alert to every configured channel.

    level: warning | error | critical
    component: module or subsystem name
    message: human-readable summary
    error: optional AppError with structured context
    request_id: optional correlation id
    exc: optional raw exception for the email body
    """
    text = _format_alert(level, component, message, error=error, request_id=request_id)
    logger.bind(level=level, component=component, request_id=request_id).error(text)

    telegram_ok = await send_telegram(text)

    email_body = text
    if exc:
        email_body += "\n\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    email_ok = send_email(f"[Koko {level.upper()}] {component}: {message}", email_body)

    logger.info(
        "Alert routing complete",
        level=level,
        component=component,
        telegram_sent=telegram_ok,
        email_sent=email_ok,
    )
