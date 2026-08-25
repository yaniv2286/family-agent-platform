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


def _build_internal_data_block(report_data: list) -> str:
    """Serialize the raw report data as compact, clearly-labeled internal
    data tags (not natural Hebrew sentences), so the LLM has everything it
    needs for analysis but has no ready-made prose to copy-paste verbatim
    into its output.
    """
    lines = []
    for child in report_data:
        name = child["child_name"]
        for subject in SUBJECTS:
            data = child["subjects"][subject]
            samples = data["sample_messages"]
            lines.append(
                f"[DATA] child_name={name}; subject={subject}; "
                f"messages_today_count={data['messages_today']}; "
                f"profile_summary={data['profile_summary'] or 'NONE'}; "
                f"raw_user_messages_today={samples if samples else 'NONE'}"
            )
    return "\n".join(lines) if lines else "[DATA] no_children_registered=true"


async def _generate_summary_text(report_data: list) -> str:
    """Ask the LLM to act as a strict executive analyst and turn the raw
    activity data (including sampled message content) into a terse,
    human-readable Hebrew executive report for Yaniv: who genuinely studied
    today, who didn't, and whether any child is gaming the system with
    low-quality spam messages instead of real engagement.

    Falls back to a plain, deterministic summary if the LLM call fails or no
    API key is configured, so the daily report is never silently skipped.
    """
    internal_data_block = _build_internal_data_block(report_data)

    system_prompt = """אתה אנליסט ביצועים בכיר ומחמיר בסטייל ישראלי-עסקי, שמכין ליניב (אבא) דוח מנהלים יומי קצרצר על פעילות הלמידה של ילדיו במערכת מנטור הלמידה הדיגיטלי. אתה לא מנטור ולא כותב לילדים - אתה כותב תמצית מנהלים לאדם בוגר.

אתה מקבל בהודעת המשתמש נתונים גולמיים בתגית [DATA] (שם, מקצוע, מספר הודעות היום, סיכום פרופיל, והודעות הגלם של התלמיד/ה היום). זה מידע פנימי לניתוח בלבד.

חובה מוחלטת #1 - אין דאמפ נתונים גולמי:
- אסור בהחלט להעביר לפלט הסופי מונחים גולמיים כמו "messages_today_count", "profile_summary", "raw_user_messages_today", "[DATA]", "למד/ה היום?" או כל תבנית מפתח=ערך.
- אסור בהחלט להדביק את תוכן שדה profile_summary כמו שהוא. יש לתמצת אותו במשפט תיאורי קצר משלך, בשפה טבעית וזורמת, ולא להעביר אותו verbatim.
- כל שורה בפלט חייבת להיות משפט אנושי רגיל, לא רשומת מסד נתונים.

חובה מוחלטת #2 - איסור ציטוט הודעות:
- אסור בהחלט לצטט מילה במילה הודעות מתוך raw_user_messages_today (בלי מירכאות עם הטקסט המקורי של התלמיד/ה).
- אם ההודעות קצרות/חזרתיות/חסרות תוכן (למשל תשובות של מילה אחת שחוזרות על עצמן) - זה סימן לספאם. תאר/י את התבנית במילים שלך (למשל: "עונה בתשובות קצרות וללא מעורבות אמיתית"), בלי להעביר את הציטוטים בפועל.
- אם ההודעות מראות שאלות/תשובות מהותיות - זו למידה אמיתית, תאר/י זאת בקצרה.

חובה מוחלטת #3 - קיבוץ לא-פעילים:
- כל תלמיד/ה עם messages_today_count=0 בשני המקצועות אסור שיקבל סעיף נפרד משלו.
- באיזה סוף הדוח, שורה אחת בלבד לכל הלא-פעילים ביחד: "💤 לא פעלו היום: שם1, שם2".
- אם כולם היו פעילים, אל תכלול שורה זו כלל.

חובה מוחלטת #4 - פורמט קשיח:
- שורה ראשונה בלבד: "📅 דוח יומי - DD/MM/YYYY".
- לכל תלמיד/ה פעיל/ה (עם הודעה אחת לפחות באחד המקצועות): כתוב/י את השם מודגש בכתיב **שם** ומתחתיו עד 2 בולטים קצרים בלבד (•) - לא יותר.
- כל בולט שמתייחס לחשבון יתחיל באימוג'י 📐, כל בולט שמתייחס לאנגלית יתחיל באימוג'י 🔤.
- אם זוהה ספאם/מעורבות מזויפת במקצוע מסוים, הבולט של אותו מקצוע חייב להתחיל ב-⚠️ במקום באימוג'י המקצוע, ולתאר את התבנית החשודה בקצרה.
- שורת הלא-פעילים (אם יש) מגיעה בסוף, אחרי כל התלמידים הפעילים.
- אין הקדמה, אין משפט סיכום מסכם, אין כותרות Markdown (#), אין הדגשת ** מלבד שמות התלמידים.

חובה מוחלטת #5 - טון:
- תמציתי באכזריות, ישיר, סטייל דוח מנהלים ישראלי. אפס פלואף, אפס משפטי נימוס.

אל תמציא/י מידע שלא קיים בנתונים."""

    try:
        from tutors import call_llm  # local import avoids a circular import at module load time
        conversation = [{"role": "user", "content": internal_data_block}]
        # Multi-child bullet reports need a larger completion budget than the
        # short kid-chat replies - the default was too small for reasoning
        # models and could silently return an empty response.
        summary = await call_llm(system_prompt, conversation, max_tokens=1000)
        if summary and not summary.strip().startswith("מפתח ה-API"):
            return summary.strip()
        logger.warning("LLM returned an empty/invalid daily summary; falling back to raw report", summary=summary)
    except Exception:
        logger.exception("Failed to generate LLM daily summary; falling back to raw report")

    return _build_fallback_summary(report_data)


def _build_fallback_summary(report_data: list) -> str:
    """Deterministic, human-readable fallback used only if the LLM call
    fails outright - keeps the same grouping rules (inactive kids on one
    line) so the report is never a raw data dump even in the failure path.
    """
    date_str = datetime.now().strftime("%d/%m/%Y")
    active_lines = []
    inactive_names = []

    for child in report_data:
        name = child["child_name"]
        total_messages = sum(child["subjects"][s]["messages_today"] for s in SUBJECTS)
        if total_messages == 0:
            inactive_names.append(name)
            continue

        active_lines.append(f"**{name}**")
        for subject in SUBJECTS:
            data = child["subjects"][subject]
            emoji = "📐" if subject == "math" else "🔤"
            if data["messages_today"] > 0:
                active_lines.append(f"{emoji} התקבלו {data['messages_today']} הודעות היום.")

    parts = [f"📅 דוח יומי - {date_str}"]
    parts.extend(active_lines)
    if inactive_names:
        parts.append(f"💤 לא פעלו היום: {', '.join(inactive_names)}")

    return "\n".join(parts)


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
