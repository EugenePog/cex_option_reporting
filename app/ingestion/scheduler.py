"""Scheduled runner for the daily collector — the long-lived pm2 process.

Uses APScheduler's cron trigger to fire once a day at INGEST_HOUR_UTC (UTC). pm2 keeps the
process alive and restarts it on failure.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.ingestion.collector import make_okx_k_collector
from config.settings import get_settings

logger = logging.getLogger(__name__)


def _run_daily() -> None:
    settings = get_settings()
    collector = make_okx_k_collector()
    try:
        collector.collect_daily(lookback_days=settings.ingest_daily_lookback_days)
    except Exception:  # noqa: BLE001 - already logged; keep the scheduler alive
        logger.exception("scheduled daily collect raised; scheduler continues")


def run_scheduler() -> None:
    settings = get_settings()
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        _run_daily,
        trigger=CronTrigger(hour=settings.ingest_hour_utc, minute=0, timezone="UTC"),
        id="daily_collect",
        max_instances=1,
        coalesce=True,      # if runs pile up (e.g. after downtime), collapse to one
    )
    logger.info("scheduler started — daily collect at %02d:00 UTC", settings.ingest_hour_utc)
    scheduler.start()
