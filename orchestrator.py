"""Meta-Agent (Orchestrator) for the Family Agent Platform.

Runs as a scheduled background job (see scheduler.py) and coordinates other
agents to produce family reports and escalations. Intents are small,
testable plugins so the orchestrator stays easy to extend.
"""
import os
import re
from typing import Awaitable, Callable, Dict
from datetime import datetime, time as dt_time, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from loguru import logger
from sqlalchemy import select

from agent_mesh import agent_mesh
from database import (
    SessionLocal,
    User,
    TutorHistorySessionLocal,
    StudentProfile,
    ChatHistory,
    WorkoutPerformance,
    MemoryFact,
)

router = APIRouter(prefix="/api/orchestrator", tags=["Orchestrator"])

SUBJECTS = ("math", "english")
SUBJECT_LABELS = {"math": "חשבון", "english": "אנגלית"}
APP_TIMEZONE = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Jerusalem"))


def _local_now() -> datetime:
    return datetime.now(APP_TIMEZONE)


def _today_utc_bounds() -> tuple[datetime, datetime]:
    local_date = _local_now().date()
    local_start = datetime.combine(local_date, dt_time.min, tzinfo=APP_TIMEZONE)
    local_end = datetime.combine(local_date, dt_time.max, tzinfo=APP_TIMEZONE)
    return (
        local_start.astimezone(timezone.utc).replace(tzinfo=None),
        local_end.astimezone(timezone.utc).replace(tzinfo=None),
    )


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
    since, until = _today_utc_bounds()
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
                        ChatHistory.timestamp <= until,
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

            async with SessionLocal() as main_db:
                fitness_rows = (await main_db.execute(
                    select(WorkoutPerformance).where(
                        WorkoutPerformance.user_id == child.id,
                        WorkoutPerformance.completed_at >= since,
                        WorkoutPerformance.completed_at <= until,
                    )
                )).scalars().all()
                recommendations = (await main_db.execute(
                    select(MemoryFact).where(
                        MemoryFact.child_id == child.id,
                        MemoryFact.domain.in_(("fitness", "nutrition")),
                        MemoryFact.category == "recommendation",
                        MemoryFact.active == 1,
                    ).order_by(MemoryFact.updated_at.desc()).limit(4)
                )).scalars().all()
            report.append({
                "child_name": child_name,
                "grade_level": child.grade_level,
                "subjects": subjects_data,
                "wellness": {
                    "workouts_completed": len(fitness_rows),
                    "activity_minutes": sum(row.duration_minutes for row in fitness_rows),
                    "recommendations": [
                        {"domain": fact.domain, "summary": fact.value[:240]}
                        for fact in recommendations
                    ],
                },
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
        wellness = child.get("wellness", {})
        lines.append(
            f"[WELLNESS] child_name={name}; workouts_completed={wellness.get('workouts_completed', 0)}; "
            f"activity_minutes={wellness.get('activity_minutes', 0)}; "
            f"recommendations={wellness.get('recommendations', [])}"
        )
    return "\n".join(lines) if lines else "[DATA] no_children_registered=true"


def _filter_active_inactive(report_data: list) -> tuple:
    """Split report data into children who had any activity today and those
    with zero messages across all subjects. The LLM is only given the active
    children; inactive names are appended deterministically afterwards.
    """
    active = []
    inactive = []
    for child in report_data:
        total_messages = sum(child["subjects"][s]["message_count"] for s in SUBJECTS)
        if total_messages == 0 and child.get("wellness", {}).get("workouts_completed", 0) == 0:
            inactive.append(child["child_name"])
        else:
            active.append(child)
    return active, inactive


async def _generate_summary_text(report_data: list) -> str:
    """Ask the LLM to act as a strict executive analyst and turn Python-
    computed engagement metadata (never raw chat content) into a terse,
    human-readable Hebrew executive report for Yaniv: who genuinely studied
    today, who didn't, and which children were already flagged by the
    Python spam heuristic as low-quality/lazy engagement.

    Falls back to a plain, deterministic summary if the LLM call fails or no
    API key is configured, so the daily report is never silently skipped.
    """
    date_str = _local_now().strftime("%d/%m/%Y")
    active_children, inactive_names = _filter_active_inactive(report_data)

    # Logic gate: children with zero messages today are never fed to the LLM.
    # They are reported only on the deterministic "did not operate" line.
    if not active_children:
        return _build_fallback_summary(report_data)

    internal_data_block = _build_internal_data_block(active_children)

    system_prompt = f"""אתה אנליסט ביצועים בכיר ומחמיר בסטייל ישראלי-עסקי, שמכין ליניב (אבא) דוח מנהלים יומי קצרצר על פעילות הלמידה של ילדיו במערכת מנטור הלמידה הדיגיטלי. אתה לא מנטור ולא כותב לילדים - אתה כותב תמצית מנהלים לאדם בוגר.

תאריך הדוח המבוקש: {date_str}. השתמש/י בו בשורה הראשונה בלבד.

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
- שורה ראשונה בלבד: "📅 דוח יומי - {date_str}".
- לכל תלמיד/ה פעיל/ה (message_count>0 באחד המקצועות לפחות): כתוב/י את השם מודגש בכתיב **שם** ומתחתיו עד 2 בולטים קצרים בלבד (•) - לא יותר.
- כל בולט שמתייחס לחשבון יתחיל באימוג'י 📐, כל בולט שמתייחס לאנגלית יתחיל באימוג'י 🔤.
- אם is_lazy_spam=True במקצוע מסוים, הבולט של אותו מקצוע חייב להתחיל ב-⚠️ במקום באימוג'י המקצוע.
- שורת הלא-פעילים (אם יש) מגיעה בסוף, אחרי כל התלמידים הפעילים.
- אין הקדמה, אין משפט סיכום מסכם, אין כותרות Markdown (#), אין הדגשת ** מלבד שמות התלמידים.

חובה מוחלטת #5 - טון:
- תמציתי באכזריות, ישיר, סטייל דוח מנהלים ישראלי. אפס פלואף, אפס משפטי נימוס.

נתוני [WELLNESS] כוללים רק ספירת אימונים, דקות פעילות והמלצות בטיחות/תזונה מובנות. אם הייתה פעילות, הוסף/י בולט 🏃 תמציתי; אפשר לציין המלצה רלוונטית בלי לחשוף מגבלות רפואיות או פרטים פרטיים. אל תציג/י את תגיות הנתונים או מפתחותיהן.

אל תמציא/י מידע שלא קיים בנתונים."""

    try:
        from tutors import call_llm  # local import avoids a circular import at module load time
        conversation = [{"role": "user", "content": internal_data_block}]
        # Multi-child bullet reports need a larger completion budget than the
        # short kid-chat replies - the default was too small for reasoning
        # models and could silently return an empty response.
        summary = await call_llm(system_prompt, conversation, max_tokens=1000)
        if summary and not summary.strip().startswith("מפתח ה-API"):
            # Ensure the LLM never fabricates the inactive list: we append it.
            if inactive_names:
                if "לא פעלו היום" not in summary:
                    summary = summary.strip() + f"\n💤 לא פעלו היום: {', '.join(inactive_names)}"
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
    date_str = _local_now().strftime("%d/%m/%Y")
    active_lines = []
    inactive_names = []

    for child in report_data:
        name = child["child_name"]
        total_messages = sum(child["subjects"][s]["message_count"] for s in SUBJECTS)
        wellness = child.get("wellness", {})
        if total_messages == 0 and wellness.get("workouts_completed", 0) == 0:
            inactive_names.append(name)
            continue

        active_lines.append(f"**{name}**")
        if wellness.get("workouts_completed", 0):
            active_lines.append(f"🏃 {wellness['workouts_completed']} אימונים, {wellness.get('activity_minutes', 0)} דקות פעילות.")
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


async def _send_via_messenger(text: str, channel: str = "telegram") -> bool:
    """Ask the messenger agent to deliver text to a channel.

    Returns True when the messenger reports success, False otherwise. This
    keeps orchestrator channel-agnostic: it does not know about Telegram
    tokens.
    """
    try:
        response = await agent_mesh.ask(
            to_agent="messenger",
            intent="send_message",
            user_id=0,
            child_id=0,
            source_agent="orchestrator",
            payload={"channel": channel, "text": text},
            timeout=20.0,
        )
        return response.get("ok") and response.get("result", {}).get("sent") is True
    except Exception:
        logger.exception("Failed to ask messenger to send message")
        return False


async def run_daily_orchestration():
    """Entry point for the scheduled job: gather data, summarize, and send."""
    logger.info("Starting daily orchestration run")
    try:
        report_data = await gather_daily_report_data()
        summary_text = await _generate_summary_text(report_data)
        sent = await _send_via_messenger(summary_text, channel="telegram")
        logger.info("Daily orchestration run finished", telegram_sent=sent)
        return {"report_data": report_data, "summary_text": summary_text, "telegram_sent": sent}
    except Exception:
        logger.exception("Daily orchestration run failed")
        raise


# ---------------------------------------------------------------------------
# Generic intent registry
# ---------------------------------------------------------------------------
IntentHandler = Callable[[dict], Awaitable[dict]]
INTENTS: Dict[str, IntentHandler] = {
    "daily_summary": run_daily_orchestration,
}


async def handle_run_intent(event, payload):
    """Mesh handler: execute a registered orchestrator intent."""
    intent = payload.get("intent")
    if not intent or intent not in INTENTS:
        return {"ok": False, "error": f"unknown intent: {intent}"}
    try:
        result = await INTENTS[intent](**payload.get("kwargs", {}))
        return {"ok": True, "result": result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


agent_mesh.on_ask("orchestrator", "run_intent", handle_run_intent)


@router.post("/run-now")
async def orchestrator_run_now():
    """Manually trigger the daily orchestration intent."""
    result = await run_daily_orchestration()
    return {
        "status": "ok",
        "telegram_sent": result["telegram_sent"],
        "summary_text": result["summary_text"],
    }


@router.post("/intents/{intent}")
async def run_intent_endpoint(intent: str):
    """Run any registered orchestrator intent by name."""
    if intent not in INTENTS:
        return {"status": "error", "message": f"unknown intent: {intent}"}
    result = await INTENTS[intent]()
    return {"status": "ok", "intent": intent, "result": result}
