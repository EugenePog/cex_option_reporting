"""Scheduled runners for the collectors — the long-lived pm2 processes.

Two independent schedules (UTC; a time "*:MM" = every hour at minute MM — the default for both is
every hour on the hour):
  * snapshot — point-in-time data, fires at each time in SNAPSHOT_TIMES_UTC.
  * history  — fills/closed/bills over a limited window, fires at each time in INGEST_TIME_UTC.
Both also top up the BTC-USD 1-minute index candles (bronze.raw_index_candle) after the accounts —
market data, collected once per run (app.ingestion.index_candles). When both fire at the same time,
an advisory lock lets only one of them fetch the candles.

pm2 keeps each process alive and restarts it on failure.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.ingestion.collector import iter_account_collectors
from app.ingestion.index_candles import sync_index_candles
from config.settings import get_settings

logger = logging.getLogger(__name__)


def _run_snapshot() -> None:
    # Fan out over every configured account; one failure never stops the others.
    for collector in iter_account_collectors():
        label = collector.writer.account_label
        try:
            collector.collect_snapshot()
        except Exception:  # noqa: BLE001 - already logged; keep the scheduler alive
            logger.exception("scheduled snapshot collect raised for %s; scheduler continues", label)
    sync_index_candles(get_settings().ingest_daily_lookback_days)   # never raises


def _run_history() -> None:
    settings = get_settings()
    for collector in iter_account_collectors():
        label = collector.writer.account_label
        try:
            collector.collect_history(lookback_days=settings.ingest_daily_lookback_days)
        except Exception:  # noqa: BLE001
            logger.exception("scheduled history collect raised for %s; scheduler continues", label)
    sync_index_candles(settings.ingest_daily_lookback_days)          # never raises


def _fmt(hour: int | str, minute: int) -> str:
    return f"every hour at :{minute:02d}" if hour == "*" else f"{hour:02d}:{minute:02d}"


def _add_jobs(scheduler, func, name: str, times: list[tuple[int | str, int]]) -> str:
    """One cron job per configured time; '*' as hour = every hour."""
    for hour, minute in times:
        hh = "xx" if hour == "*" else f"{hour:02d}"
        scheduler.add_job(
            func,
            trigger=CronTrigger(hour=hour, minute=minute, timezone="UTC"),
            id=f"{name}_{hh}{minute:02d}",
            max_instances=1,
            coalesce=True,
        )
    return ", ".join(_fmt(h, m) for h, m in times) or "(no times configured)"


def run_snapshot_scheduler() -> None:
    settings = get_settings()
    scheduler = BlockingScheduler(timezone="UTC")
    pretty = _add_jobs(scheduler, _run_snapshot, "snapshot", settings.snapshot_time_tuples())
    logger.info("snapshot scheduler started — runs at %s UTC", pretty)
    scheduler.start()


def run_history_scheduler() -> None:
    settings = get_settings()
    scheduler = BlockingScheduler(timezone="UTC")
    pretty = _add_jobs(scheduler, _run_history, "history", settings.ingest_time_tuples())
    logger.info("history scheduler started — runs at %s UTC", pretty)
    scheduler.start()
