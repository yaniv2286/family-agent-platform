"""Cross-channel Messenger agent.

Currently implements Telegram. Future channels (email, WhatsApp, SMS) can be
added behind the same mesh interface without changing callers.

Mesh intents:
  - messenger.send_message  -> deliver a message to a configured channel
  - messenger.process_feedback -> process inbound parent feedback (Telegram)
"""
import os
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select

from agent_mesh import agent_mesh
from database import SessionLocal, User, add_parent_feedback

router = APIRouter(prefix="/api/messenger", tags=["Messenger"])

SUBJECT_LABELS = {"math": "חשבון", "english": "אנגלית"}

# In-memory offset for Telegram long-polling. Starting at 0 means the first
# poll only probes the latest update id to avoid reprocessing old messages.
_LAST_TELEGRAM_UPDATE_ID = 0


class SendMessageRequest(BaseModel):
    channel: str = "telegram"
    text: str
    recipient: Optional[str] = None  # future use for email/phone


async def _get_children():
    """Return all child User rows from the main database."""
    async with SessionLocal() as db:
        result = await db.execute(select(User).where(User.role == "child"))
        return result.scalars().all()


def _detect_feedback_subject(text: str) -> Optional[str]:
    """Guess the subject a parent feedback refers to (math, english, or None)."""
    lowered = text.lower()
    if any(k in lowered for k in ("math", "חשבון", "מתמטיקה", "כפל", "חילוק", "חיבור", "חיסור", "לוח הכפל", "אריתמטיקה")):
        return "math"
    if any(k in lowered for k in ("english", "אנגלית", "anglit", "אנגלי", "אנגליה", "grammar", "vocabulary")):
        return "english"
    return None


async def send_telegram_message(text: str) -> bool:
    """Send a message via Telegram Bot API.

    Returns True on success, False otherwise (never raises, so a Telegram
    outage doesn't crash the caller).
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or bot_token == "your_telegram_bot_token_here" or not chat_id:
        logger.warning(
            "TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID not configured; "
            "skipping Telegram send."
        )
        return False

    deployment_label = os.getenv("DEPLOYMENT_LABEL", "LOCAL").strip().upper() or "LOCAL"
    labeled_text = f"[{deployment_label}]\n{text}"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json={"chat_id": chat_id, "text": labeled_text})
            response.raise_for_status()
        logger.info("Message sent to Telegram successfully")
        return True
    except Exception:
        logger.exception("Failed to send message to Telegram")
        return False


async def handle_send_message(event, payload):
    """Mesh ask handler: deliver a message."""
    channel = payload.get("channel", "telegram")
    text = payload.get("text", "")
    if channel == "telegram":
        success = await send_telegram_message(text)
        return {"channel": channel, "sent": success}
    return {"channel": channel, "sent": False, "error": "channel not configured"}


SUBJECT_LABELS = {"math": "חשבון", "english": "אנגלית"}


async def check_telegram_feedback(updates):
    """Process Telegram parent feedback updates delivered by webhook."""
    global _LAST_TELEGRAM_UPDATE_ID

    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not chat_id or not updates:
        return

    if isinstance(updates, dict):
        updates = [updates]

    try:
        children = await _get_children()
        child_names = [c.name for c in children]

        new_offset = _LAST_TELEGRAM_UPDATE_ID
        for update in updates:
            msg = update.get("message") or update.get("edited_message")
            if not msg:
                continue
            if str(msg.get("chat", {}).get("id")) != str(chat_id):
                continue
            if msg.get("from", {}).get("is_bot"):
                continue
            text = (msg.get("text") or "").strip()
            if not text:
                continue

            stored_targets = []
            instruction_lines = [line.strip(" -•\t") for line in text.splitlines() if line.strip(" -•\t")]
            for instruction in instruction_lines:
                subject = _detect_feedback_subject(instruction)
                matched_children = [name for name in child_names if name and name in instruction]
                if matched_children:
                    for child_name in matched_children:
                        await add_parent_feedback(
                            child_name=child_name,
                            subject=subject,
                            message=instruction,
                        )
                        subject_label = SUBJECT_LABELS.get(subject, subject) if subject else None
                        stored_targets.append(
                            f"{child_name} / {subject_label or 'כל המקצועות'}"
                        )
                else:
                    await add_parent_feedback(
                        child_name=None,
                        subject=subject,
                        message=instruction,
                    )
                    subject_label = SUBJECT_LABELS.get(subject, subject) if subject else None
                    stored_targets.append(
                        f"כל הילדים / {subject_label or 'כל המקצועות'}"
                    )

            logger.info(
                "Stored parent directives from Telegram",
                targets=stored_targets,
                text=text[:120],
            )
            targets_text = "\n".join(f"• {target}" for target in stored_targets)
            await send_telegram_message(
                f"✅ ההנחיות התקבלו ונשמרו בזיכרון המתמשך:\n{targets_text}"
            )

            new_offset = max(new_offset, update.get("update_id", new_offset))

        _LAST_TELEGRAM_UPDATE_ID = new_offset
    except Exception:
        logger.exception("Telegram feedback processing failed")


async def handle_process_feedback(event, payload):
    """Mesh handler for inbound feedback from any channel."""
    channel = payload.get("channel", "telegram")
    if channel == "telegram":
        await check_telegram_feedback(payload.get("updates", []))
        return {"processed": True, "channel": channel}
    return {"processed": False, "error": "channel not supported"}


agent_mesh.on_ask("messenger", "send_message", handle_send_message)
agent_mesh.on_ask("messenger", "process_feedback", handle_process_feedback)


@router.post("/send")
async def send_message(data: SendMessageRequest):
    """Direct API to send a message through the messenger agent."""
    result = await handle_send_message(None, data.model_dump())
    return result


@router.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """Receive Telegram webhook updates and process them as parent feedback."""
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    expected = os.getenv("TELEGRAM_SECRET_TOKEN")
    if not expected or secret != expected:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    await check_telegram_feedback(data)
    return {"status": "ok"}
