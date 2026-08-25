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


# Subject-aware spam thresholds, computed entirely in Python from message
# metadata - the LLM never sees raw chat content, only these derived flags.
SPAM_THRESHOLDS = {
    # English short answers are usually unproductive; single words are not
    # valid English practice unless they are clear, full answers.
    "english": {"min_messages": 3, "max_avg_words": 2.5},
    # Math answers are naturally short (e.g. "4" or "12"), so only flag
    # heavy streams of essentially empty/garbage inputs.
    "math": {"min_messages": 5, "max_avg_words": 1.0},
}


def _compute_engagement_stats(message_contents: list, subject: str) -> dict:
    """Compute message_count, average_words_per_message, and the subject-aware
    is_lazy_spam heuristic flag from a list of raw message strings. This is
    the only place raw chat content is touched - the content itself is
    discarded immediately after these numbers are derived, and never sent
    to the LLM.
    """
    message_count = len(message_contents)
    if message_count == 0:
        return {
            "message_count": 0,
            "average_words_per_message": 0.0,
            "is_lazy_spam": False,
        }

    word_counts = [len((content or "").split()) for content in message_contents]
    average_words_per_message = sum(word_counts) / message_count

    thresholds = SPAM_THRESHOLDS.get(subject, SPAM_THRESHOLDS["math"])
    is_lazy_spam = (
        message_count > thresholds["min_messages"]
        and average_words_per_message < thresholds["max_avg_words"]
    )

    return {
        "message_count": message_count,
        "average_words_per_message": round(average_words_per_message, 2),
        "is_lazy_spam": is_lazy_spam,
    }


async def gather_daily_report_data() -> list:
    """Collect per-child, per-subject activity and profile summaries for today.

    All spam/engagement analysis happens here in Python from message
    metadata (count, average word length) - raw message content is fetched
    only to compute these numbers and is never propagated further (not
    returned, not sent to the LLM). This avoids wasting tokens on raw chat
    logs and keeps the spam heuristic deterministic and auditable.

    Returns a list of dicts:
        {
            "child_name": str,
            "grade_level": str | None,
            "subjects": {
                "math": {
                    "message_count": int,
                    "average_words_per_message": float,
                    "is_lazy_spam": bool,
                    "profile_summary": str | None,
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
                messages_result = await history_db.execute(
                    select(ChatHistory.content).where(
                        ChatHistory.child_name == child_name,
                        ChatHistory.subject == subject,
                        ChatHistory.role == "user",
                        ChatHistory.timestamp >= since,
                    )
                )
                # Raw content is used only inside _compute_engagement_stats
                # and is not kept or returned beyond this point.
                message_contents = [row[0] for row in messages_result.all()]
                engagement_stats = _compute_engagement_stats(message_contents, subject)

                profile_result = await history_db.execute(
                    select(StudentProfile).where(
                        StudentProfile.child_name == child_name,
                        StudentProfile.subject == subject,
                    )
                )
                profile = profile_result.scalar_one_or_none()

                subjects_data[subject] = {
                    **engagement_stats,
                    "profile_summary": profile.profile_summary if profile else None,
                }

            report.append({
                "child_name": child_name,
                "grade_level": child.grade_level,
                "subjects": subjects_data,
            })

    return report


def _build_internal_data_block(report_data: list) -> str:
    """Serialize only the Python-computed metadata (message count, average
    words per message, the is_lazy_spam flag, and the profile summary) as
    compact [DATA] tags for the LLM.

    ZERO raw chat content is included here by design - the spam
    determination is already made deterministically in Python
    (_compute_engagement_stats); the LLM's only job is to phrase that
    conclusion and the profile summary into the final report.
    """
    lines = []
    for child in report_data:
        name = child["child_name"]
        for subject in SUBJECTS:
            data = child["subjects"][subject]
            lines.append(
                f"[DATA] child_name={name}; subject={subject}; "
                f"message_count={data['message_count']}; "
                f"average_words_per_message={data['average_words_per_message']}; "
                f"is_lazy_spam={data['is_lazy_spam']}; "
                f"profile_summary={data['profile_summary'] or 'NONE'}"
            )
    return "\n".join(lines) if lines else "[DATA] no_children_registered=true"


async def _generate_summary_text(report_data: list) -> str:
    """Ask the LLM to act as a strict executive analyst and turn Python-
    computed engagement metadata (never raw chat content) into a terse,
    human-readable Hebrew executive report for Yaniv: who genuinely studied
    today, who didn't, and which children were already flagged by the
    Python spam heuristic as low-quality/lazy engagement.

    Falls back to a plain, deterministic summary if the LLM call fails or no
    API key is configured, so the daily report is never silently skipped.
    """
    internal_data_block = _build_internal_data_block(report_data)

    system_prompt = """אתה אנליסט ביצועים בכיר ומחמיר בסטייל ישראלי-עסקי, שמכין ליניב (אבא) דוח מנהלים יומי קצרצר על פעילות הלמידה של ילדיו במערכת מנטור הלמידה הדיגיטלי. אתה לא מנטור ולא כותב לילדים - אתה כותב תמצית מנהלים לאדם בוגר.

אתה מקבל בהודעת המשתמש נתונים בתגית [DATA] לכל תלמיד/ה ומקצוע: message_count (מספר הודעות היום), average_words_per_message (אורך הודעה ממוצע במילים), is_lazy_spam (True/False - דגל שכבר חושב ונקבע באופן דטרמיניסטי בקוד Python לפי מקצוע: באנגלית - יותר מ-3 הודעות היום וממוצע מילים נמוך מ-2.5; בחשבון - יותר מ-5 הודעות היום וממוצע מילים נמוך מ-1.0, כי תשובות קצרות כמו "4" הן לגיטימיות), ו-profile_summary (סיכום פדגוגי). אין לך גישה לתוכן ההודעות בפועל - רק למספרים האלה. is_lazy_spam הוא המסקנה הסופית, לא רמז - סמוך/י עליו לחלוטין ואל תנסה/י "לנתח" אותו מחדש.

חובה מוחלטת #1 - אין דאמפ נתונים גולמי:
- אסור בהחלט להעביר לפלט הסופי מונחים גולמיים כמו "message_count", "average_words_per_message", "is_lazy_spam", "profile_summary", "[DATA]" או כל תבנית מפתח=ערך.
- אסור בהחלט להדביק את תוכן שדה profile_summary כמו שהוא. יש לתמצת אותו במשפט תיאורי קצר משלך, בשפה טבעית וזורמת, ולא להעביר אותו verbatim.
- כל שורה בפלט חייבת להיות משפט אנושי רגיל, לא רשומת מסד נתונים.

חובה מוחלטת #2 - ספאם מגיע מוכן מקוד, לא מניתוח טקסט:
- אם is_lazy_spam=True במקצוע מסוים, חובה לכתוב עבורו בולט אזהרה שמתחיל ב-⚠️ שמתאר במילים שלך שהתלמיד/ה שלח/ה הרבה הודעות קצרות וחסרות תוכן (ציין/י את message_count אם רלוונטי, בלי לצטט טקסט - אין לך טקסט לצטט בכלל).
- אם is_lazy_spam=False, אל תרמז/י על ספאם - תאר/י את הפעילות כלמידה רגילה על בסיס profile_summary ו-message_count.

חובה מוחלטת #3 - קיבוץ לא-פעילים:
- כל תלמיד/ה עם message_count=0 בשני המקצועות אסור שיקבל סעיף נפרד משלו.
- בסוף הדוח, שורה אחת בלבד לכל הלא-פעילים ביחד: "💤 לא פעלו היום: שם1, שם2".
- אם כולם היו פעילים, אל תכלול שורה זו כלל.

חובה מוחלטת #4 - פורמט קשיח:
- שורה ראשונה בלבד: "📅 דוח יומי - DD/MM/YYYY".
- לכל תלמיד/ה פעיל/ה (message_count>0 באחד המקצועות לפחות): כתוב/י את השם מודגש בכתיב **שם** ומתחתיו עד 2 בולטים קצרים בלבד (•) - לא יותר.
- כל בולט שמתייחס לחשבון יתחיל באימוג'י 📐, כל בולט שמתייחס לאנגלית יתחיל באימוג'י 🔤.
- אם is_lazy_spam=True במקצוע מסוים, הבולט של אותו מקצוע חייב להתחיל ב-⚠️ במקום באימוג'י המקצוע.
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
    line) and surfaces the Python-computed is_lazy_spam flag directly, so
    the report is never a raw data dump even in the failure path.
    """
    date_str = datetime.now().strftime("%d/%m/%Y")
    active_lines = []
    inactive_names = []

    for child in report_data:
        name = child["child_name"]
        total_messages = sum(child["subjects"][s]["message_count"] for s in SUBJECTS)
        if total_messages == 0:
            inactive_names.append(name)
            continue

        active_lines.append(f"**{name}**")
        for subject in SUBJECTS:
            data = child["subjects"][subject]
            if data["message_count"] == 0:
                continue
            if data["is_lazy_spam"]:
                active_lines.append(f"⚠️ הודעות רבות ({data['message_count']}) אך קצרות וחסרות תוכן.")
            else:
                emoji = "📐" if subject == "math" else "🔤"
                active_lines.append(f"{emoji} התקבלו {data['message_count']} הודעות היום.")

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
