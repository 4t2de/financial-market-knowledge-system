"""
Scheduler
---------
Runs the Module 2 pipeline once per day at the configured time (UTC).
Uses APScheduler with a blocking scheduler — intended to run as a long-lived
process (e.g. a systemd service or Docker container on Azure).

Usage:
    python -m scheduler.daily_scheduler
"""
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from config.settings import SCHEDULER_HOUR, SCHEDULER_MINUTE
from pipeline.orchestrator import run_pipeline


def _job():
    logger.info("Scheduled job triggered.")
    try:
        run_pipeline()
    except Exception as e:
        logger.exception(f"Unhandled error in scheduled pipeline run: {e}")


def start():
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        _job,
        trigger=CronTrigger(hour=SCHEDULER_HOUR, minute=SCHEDULER_MINUTE),
        id="module2_daily_pipeline",
        name="Module 2 Daily Knowledge Pre-Processing",
        misfire_grace_time=3600,   # if process was down, run within 1hr window
        coalesce=True,             # don't stack up if behind
    )
    logger.info(
        f"Scheduler started. Pipeline will run daily at "
        f"{SCHEDULER_HOUR:02d}:{SCHEDULER_MINUTE:02d} UTC."
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")
