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


# Cap on how many of today's actual user messages we pull per child/subject
# for the LLM to inspect when judging engagement quality vs. spam.
MAX_SAMPLE_MESSAGES = 20


async def gather_daily_report_data() -> list:
    """Collect per-child, per-subject activity and profile summaries for today.

    Includes a sample of today's actual user messages (not just a count) so
    the summarizer can judge whether the child engaged in genuine learning
    or was just spamming short/meaningless messages.

    Returns a list of dicts:
        {
            "child_name": str,
            "grade_level": str | None,
            "subjects": {
                "math": {
                    "messages_today": int,
                    "profile_summary": str | None,
                    "sample_messages": list[str],
                },
                "english": {...},
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

                messages_result = await history_db.execute(
                    select(ChatHistory.content)
                    .where(
                        ChatHistory.child_name == child_name,
                        ChatHistory.subject == subject,
                        ChatHistory.role == "user",
                        ChatHistory.timestamp >= since,
                    )
                    .order_by(ChatHistory.timestamp.asc())
                    .limit(MAX_SAMPLE_MESSAGES)
                )
                sample_messages = [row[0] for row in messages_result.all()]

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
                    "sample_messages": sample_messages,
                }

            report.append({
                "child_name": child_name,
                "grade_level": child.grade_level,
                "subjects": subjects_data,
            })

    return report


async def _generate_summary_text(report_data: list) -> str:
    """Ask the LLM to act as a strict executive analyst and turn the raw
    activity data (including sampled message content) into a terse,
    bullet-point Hebrew report for Yaniv: who genuinely studied today, who
    didn't, and whether any child is gaming the system with low-quality
    spam messages instead of real engagement.

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
            samples = data["sample_messages"]
            samples_block = (
                " | ".join(f'"{s}"' for s in samples) if samples else "(אין הודעות היום)"
            )
            lines.append(
                f"- {name} ({label}): למד/ה היום? {studied} "
                f"(הודעות היום: {data['messages_today']}). "
                f"סיכום פרופיל: {summary} "
                f"הודעות התלמיד/ה היום (לפי סדר כרונולוגי, לבדיקת איכות): {samples_block}"
            )
    raw_data_block = "\n".join(lines) if lines else "אין נתונים על ילדים רשומים במערכת."

    system_prompt = """אתה אנליסט ביצועים בכיר ומחמיר, שמכין ליניב (אבא) דוח מנהלים יומי קצרצר על פעילות הלמידה של ילדיו במערכת מנטור הלמידה הדיגיטלי. אתה לא מנטור, לא מעודד ילדים - אתה מנתח נתונים בצורה עניינית וישירה.

חובה מוחלטת - בדיקת איכות (Quality over Quantity):
- עבור כל תלמיד/ה שיש לו הודעות היום, קרא/י את דוגמאות ההודעות שסופקו.
- אם ההודעות קצרות, חזרתיות, חסרות משמעות, או נראות כמו ניסיון "לספאם" את המערכת כדי לצבור נקודות/הודעות בלי למידה אמיתית (למשל: "כן", "אוקיי", "1", "..." שוב ושוב, או אותה הודעה בדיוק חזור ושוב) - חובה לדגל את זה במפורש כ"פעילות חשודה / ספאם" ולא לרשום אותו כ"למד/ה" באמת.
- אם ההודעות מראות מעורבות אמיתית (שאלות, תשובות מהותיות, תרגול בעיות) - זו למידה אמיתית.

חובה מוחלטת - פורמט פלט:
- כתוב/י בעברית בלבד, בפורמט בולטים (•) בלבד. אין להשתמש בפסקאות טקסט רגיל, אין הקדמה, אין ניסוח מקדים, אין סיכום מסכם בסוף.
- שורה ראשונה בלבד: תאריך היום בפורמט "📅 דוח יומי - DD/MM/YYYY" (בלי טקסט נוסף בשורה הזו).
- לאחר מכן, בולט אחד לכל תלמיד/ה עם פעילות היום (מקסימום 3 בולטים לכל תלמיד/ה פעיל/ה), ובולט קצר אחד לכל תלמיד/ה שלא למד/ה כלל.
- כל בולט חייב להיות משפט אחד קצר וממוקד - עובדה, לא ניסוח מנופח.
- ציין/י תלמיד/ה שספאם/ה בבולט נפרד ומפורש שמתחיל ב"⚠️".
- אל תמציא/י מידע שלא קיים בנתונים. אל תשתמש/י בפורמט Markdown מסובך (בלי **, בלי כותרות #)."""

    try:
        from tutors import call_llm  # local import avoids a circular import at module load time
        conversation = [{"role": "user", "content": f"נתוני היום:\n{raw_data_block}"}]
        # Multi-child bullet reports need a larger completion budget than the
        # short kid-chat replies - the default was too small for reasoning
        # models and could silently return an empty response.
        summary = await call_llm(system_prompt, conversation, max_tokens=1000)
        if summary and not summary.strip().startswith("מפתח ה-API"):
            return summary.strip()
        logger.warning("LLM returned an empty/invalid daily summary; falling back to raw report", summary=summary)
    except Exception:
        logger.exception("Failed to generate LLM daily summary; falling back to raw report")

    # Deterministic fallback so a report is always sent even if the LLM fails.
    date_str = datetime.now().strftime("%d/%m/%Y")
    return f"📅 דוח יומי - {date_str}\n\n{raw_data_block}"


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
