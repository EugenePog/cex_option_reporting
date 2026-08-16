"""Scheduled runners for the collectors — the long-lived pm2 processes.

Two independent schedules:
  * snapshot — point-in-time data, fires at each time in SNAPSHOT_TIMES_UTC (several times/day).
  * history  — fills/closed/bills over a limited window, once/day at INGEST_TIME_UTC.

pm2 keeps each process alive and restarts it on failure.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.ingestion.collector import make_okx_k_collector
from config.settings import get_settings

logger = logging.getLogger(__name__)


def _run_snapshot() -> None:
    try:
        make_okx_k_collector().collect_snapshot()
    except Exception:  # noqa: BLE001 - already logged; keep the scheduler alive
        logger.exception("scheduled snapshot collect raised; scheduler continues")


def _run_history() -> None:
    settings = get_settings()
    try:
        make_okx_k_collector().collect_history(lookback_days=settings.ingest_daily_lookback_days)
    except Exception:  # noqa: BLE001
        logger.exception("scheduled history collect raised; scheduler continues")


def run_snapshot_scheduler() -> None:
    settings = get_settings()
    scheduler = BlockingScheduler(timezone="UTC")
    times = settings.snapshot_time_tuples()
    for hour, minute in times:
        scheduler.add_job(
            _run_snapshot,
            trigger=CronTrigger(hour=hour, minute=minute, timezone="UTC"),
            id=f"snapshot_{hour:02d}{minute:02d}",
            max_instances=1,
            coalesce=True,
        )
    pretty = ", ".join(f"{h:02d}:{m:02d}" for h, m in times)
    logger.info("snapshot scheduler started — runs at %s UTC", pretty or "(no times configured)")
    scheduler.start()


def run_history_scheduler() -> None:
    settings = get_settings()
    hour, minute = settings.ingest_time_tuple()
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        _run_history,
        trigger=CronTrigger(hour=hour, minute=minute, timezone="UTC"),
        id="history_daily",
        max_instances=1,
        coalesce=True,
    )
    logger.info("history scheduler started — daily at %02d:%02d UTC", hour, minute)
    scheduler.start()
