"""APScheduler setup for the daily orchestrator job."""
import os

from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from orchestrator import run_daily_orchestration

# Daily run time (24h, local server time), configurable via .env.
ORCHESTRATOR_HOUR = int(os.getenv("ORCHESTRATOR_HOUR", "21"))
ORCHESTRATOR_MINUTE = int(os.getenv("ORCHESTRATOR_MINUTE", "30"))

# Scout Ideas digest schedule: twice a day by default. SCOUT_DIGEST_HOURS
# accepts a cron field (e.g. "8,20" or "*/6"); SCOUT_DIGEST_ENABLED=0
# disables the job entirely.
SCOUT_DIGEST_ENABLED = os.getenv("SCOUT_DIGEST_ENABLED", "1") != "0"
SCOUT_DIGEST_HOURS = os.getenv("SCOUT_DIGEST_HOURS", "8,20")
SCOUT_DIGEST_MINUTE = os.getenv("SCOUT_DIGEST_MINUTE", "15")

TIMEZONE_NAME = os.getenv("APP_TIMEZONE", "Asia/Jerusalem")
LOCAL_TIMEZONE = ZoneInfo(TIMEZONE_NAME)

scheduler = AsyncIOScheduler(timezone=LOCAL_TIMEZONE)


async def _scheduled_job():
    logger.info("Triggering scheduled daily orchestration job")
    await run_daily_orchestration()


async def _heartbeat_job():
    """Write a periodic heartbeat log line for external watchdogs."""
    logger.info("[HEARTBEAT] Koko scheduler is alive")


async def _scout_digest_job():
    """Collect from configured social sources and send the investment digest."""
    try:
        # Imported lazily so scheduler has no import-time dependency on the
        # agent module (and tests can stub the scheduler freely).
        from agents.scout_ideas_agent import run_scout_digest
        await run_scout_digest()
    except Exception:
        logger.exception("Scout digest job failed")


def start_scheduler():
    """Register and start the daily orchestration job. Safe to call once at
    FastAPI startup.
    """
    if scheduler.running:
        logger.warning("Orchestrator scheduler is already running; skipping duplicate start")
        return
    scheduler.add_job(
        _scheduled_job,
        trigger=CronTrigger(hour=ORCHESTRATOR_HOUR, minute=ORCHESTRATOR_MINUTE, timezone=LOCAL_TIMEZONE),
        id="daily_orchestration",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        _heartbeat_job,
        trigger=CronTrigger(minute="0", timezone=LOCAL_TIMEZONE),
        id="hourly_heartbeat",
        replace_existing=True,
        max_instances=1,
    )
    if SCOUT_DIGEST_ENABLED:
        scheduler.add_job(
            _scout_digest_job,
            trigger=CronTrigger(
                hour=SCOUT_DIGEST_HOURS,
                minute=SCOUT_DIGEST_MINUTE,
                timezone=LOCAL_TIMEZONE,
            ),
            id="scout_ideas_digest",
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=300,
        )
    scheduler.start()
    logger.info(
        "Orchestrator scheduler started",
        hour=ORCHESTRATOR_HOUR,
        minute=ORCHESTRATOR_MINUTE,
        timezone=str(LOCAL_TIMEZONE),
    )


def stop_scheduler():
    """Shut down the scheduler cleanly on FastAPI shutdown."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Orchestrator scheduler stopped")
