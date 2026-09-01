"""APScheduler setup for the daily orchestrator job."""
import os

import tzlocal
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from orchestrator import run_daily_orchestration

# Daily run time (24h, local server time), configurable via .env.
ORCHESTRATOR_HOUR = int(os.getenv("ORCHESTRATOR_HOUR", "21"))
ORCHESTRATOR_MINUTE = int(os.getenv("ORCHESTRATOR_MINUTE", "30"))

# Explicitly pin the trigger to the server's local timezone so the schedule
# doesn't silently shift if the scheduler's default ever resolves to UTC.
LOCAL_TIMEZONE = tzlocal.get_localzone()

scheduler = AsyncIOScheduler(timezone=LOCAL_TIMEZONE)


async def _scheduled_job():
    logger.info("Triggering scheduled daily orchestration job")
    await run_daily_orchestration()


def start_scheduler():
    """Register and start the daily orchestration job. Safe to call once at
    FastAPI startup.
    """
    scheduler.add_job(
        _scheduled_job,
        trigger=CronTrigger(hour=ORCHESTRATOR_HOUR, minute=ORCHESTRATOR_MINUTE, timezone=LOCAL_TIMEZONE),
        id="daily_orchestration",
        replace_existing=True,
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
