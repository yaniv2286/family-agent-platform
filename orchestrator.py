"""Meta-Agent (Orchestrator) for the Family Agent Platform.

Runs as a scheduled background job (see scheduler.py) that reviews all
children's math/English activity for the day and sends Yaniv a single daily
summary on Telegram, so he can forward the highlights to the family group
manually.
"""
import os
from datetime import datetime, time as dt_time

import httpx
from loguru import logger
from sqlalchemy import select, func

from database import SessionLocal, User, TutorHistorySessionLocal, StudentProfile, ChatHistory

SUBJECTS = ("math", "english")
SUBJECT_LABELS = {"math": "חשבון", "english": "אנגלית"}


def _today_start() -> datetime:
    return datetime.combine(datetime.now().date(), dt_time.min)


async def _get_children():
    """Return all child User rows from the main database."""
    async with SessionLocal() as db:
        result = await db.execute(select(User).where(User.role == "child"))
        return result.scalars().all()


async def gather_daily_report_data() -> list:
    """Collect per-child, per-subject activity and profile summaries for today.

    Returns a list of dicts:
        {
            "child_name": str,
            "grade_level": str | None,
            "subjects": {
                "math": {"messages_today": int, "profile_summary": str | None},
                "english": {"messages_today": int, "profile_summary": str | None},
            },
        }
    """
    children = await _get_children()
    since = _today_start()
    report = []

    async with TutorHistorySessionLocal() as history_db:
        for child in children:
            child_name = child.name
            subjects_data = {}

            for subject in SUBJECTS:
                count_result = await history_db.execute(
                    select(func.count()).where(
                        ChatHistory.child_name == child_name,
                        ChatHistory.subject == subject,
                        ChatHistory.role == "user",
                        ChatHistory.timestamp >= since,
                    )
                )
                messages_today = count_result.scalar() or 0

                profile_result = await history_db.execute(
                    select(StudentProfile).where(
                        StudentProfile.child_name == child_name,
                        StudentProfile.subject == subject,
                    )
                )
                profile = profile_result.scalar_one_or_none()

                subjects_data[subject] = {
                    "messages_today": messages_today,
                    "profile_summary": profile.profile_summary if profile else None,
                }

            report.append({
                "child_name": child_name,
                "grade_level": child.grade_level,
                "subjects": subjects_data,
            })

    return report


async def _generate_summary_text(report_data: list) -> str:
    """Ask the LLM to turn the raw activity data into a short, friendly Hebrew
    summary for Yaniv: who studied today, who didn't, and pedagogical
    highlights worth sharing with the family.

    Falls back to a plain, deterministic summary if the LLM call fails or no
    API key is configured, so the daily report is never silently skipped.
    """
    lines = []
    for child in report_data:
        name = child["child_name"]
        for subject in SUBJECTS:
            data = child["subjects"][subject]
            label = SUBJECT_LABELS[subject]
            studied = "כן" if data["messages_today"] > 0 else "לא"
            summary = data["profile_summary"] or "אין עדיין סיכום למידה."
            lines.append(
                f"- {name} ({label}): למד/ה היום? {studied} "
                f"(הודעות היום: {data['messages_today']}). סיכום פרופיל: {summary}"
            )
    raw_data_block = "\n".join(lines) if lines else "אין נתונים על ילדים רשומים במערכת."

    system_prompt = """אתה עוזר שמסכם ליניב, אבא במשפחה, את פעילות הלמידה היומית של ילדיו במערכת מנטור הלמידה הדיגיטלי.
כתוב הודעה קצרה וברורה בעברית לטלגרם (עד 8-10 שורות), במבנה הבא:
1. כותרת קצרה עם תאריך היום.
2. מי למד היום ומי לא (רשימה לפי שם).
3. 1-2 נקודות מעניינות מהפרופיל הפדגוגי (חוזקות, קשיים, התקדמות) - רק אם יש מידע.
4. סיום קצר ומעודד.
אל תשתמש בפורמט Markdown מסובך, רק טקסט רגיל עם אימוג'ים קלים. אל תמציא מידע שלא קיים בנתונים."""

    try:
        from tutors import call_llm  # local import avoids a circular import at module load time
        conversation = [{"role": "user", "content": f"נתוני היום:\n{raw_data_block}"}]
        summary = await call_llm(system_prompt, conversation)
        if summary and not summary.strip().startswith("מפתח ה-API"):
            return summary.strip()
    except Exception:
        logger.exception("Failed to generate LLM daily summary; falling back to raw report")

    # Deterministic fallback so a report is always sent even if the LLM fails.
    date_str = datetime.now().strftime("%d/%m/%Y")
    return f"📅 סיכום למידה יומי - {date_str}\n\n{raw_data_block}"


async def send_telegram_message(text: str) -> bool:
    """Send a message to Yaniv via the Telegram Bot API.

    Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to be set in the
    environment. Returns True on success, False otherwise (never raises, so
    a Telegram outage doesn't crash the scheduled job).
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or bot_token == "your_telegram_bot_token_here" or not chat_id:
        logger.warning(
            "TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID not configured; "
            "skipping daily summary Telegram send."
        )
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json={"chat_id": chat_id, "text": text})
            response.raise_for_status()
        logger.info("Daily summary sent to Telegram successfully")
        return True
    except Exception:
        logger.exception("Failed to send daily summary to Telegram")
        return False


async def run_daily_orchestration():
    """Entry point for the scheduled job: gather data, summarize, and send."""
    logger.info("Starting daily orchestration run")
    try:
        report_data = await gather_daily_report_data()
        summary_text = await _generate_summary_text(report_data)
        sent = await send_telegram_message(summary_text)
        logger.info("Daily orchestration run finished", telegram_sent=sent)
        return {"report_data": report_data, "summary_text": summary_text, "telegram_sent": sent}
    except Exception:
        logger.exception("Daily orchestration run failed")
        raise
