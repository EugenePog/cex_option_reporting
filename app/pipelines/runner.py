"""Pipeline orchestration entrypoints (bronze -> silver -> gold), runnable per stage.

Every stage run takes a Postgres advisory lock, so the pm2 pipeline loop and an on-demand
recompute (e.g. after a Box builder Apply in the web app) never run concurrently — the second
caller simply waits for the first to finish.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text

from app.pipelines import bronze_to_silver, silver_to_gold

logger = logging.getLogger(__name__)

# Arbitrary app-wide key for pg_advisory_lock (int64); one pipeline run at a time.
PIPELINE_LOCK_KEY = 0x0CE0_B0C5


@contextmanager
def pipeline_lock() -> Iterator[None]:
    """Session-level Postgres advisory lock held for the duration of a pipeline run."""
    from app.db.base import engine

    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": PIPELINE_LOCK_KEY})
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": PIPELINE_LOCK_KEY})
            conn.commit()


# Fingerprint of the bronze + core rows the last silver run of THIS process was built from
# (bronze_to_silver.source_fingerprint). Only the pm2 loop uses it to skip unchanged runs; a manual
# `pipeline` run and the Box builder's recompute always run in full.
_last_silver_source: tuple | None = None


def run_silver(skip_unchanged: bool = False) -> dict:
    """bronze -> silver. With `skip_unchanged`, nothing is done when no bronze row was added and no
    core row silver depends on changed since this process's last silver run (build 15)."""
    global _last_silver_source
    source = bronze_to_silver.source_fingerprint()
    if skip_unchanged and source == _last_silver_source:
        logger.info("silver: skipped — no new bronze rows and no core changes since the last run")
        return {"silver": {}}
    result = {"silver": bronze_to_silver.run()}
    _last_silver_source = source
    return result


def run_gold() -> dict:
    return {"gold": silver_to_gold.run()}


def run_all(skip_unchanged: bool = False) -> dict:
    return {**run_silver(skip_unchanged), **run_gold()}


def run_stage(stage: str, skip_unchanged: bool = False) -> dict:
    """stage ∈ {'silver', 'gold', 'all'}. Serialized across processes via pipeline_lock().
    `skip_unchanged` (the pm2 loop): skip the silver step when its sources did not change."""
    runners = {"silver": lambda: run_silver(skip_unchanged), "gold": run_gold,
               "all": lambda: run_all(skip_unchanged)}
    if stage not in runners:
        raise ValueError(f"unknown stage {stage!r} (expected silver|gold|all)")
    with pipeline_lock():
        return runners[stage]()


def run_loop(stage: str = "all") -> None:
    """Run the given stage on a schedule (interval from settings), keeping the process alive."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    from config.settings import get_settings

    interval = get_settings().pipeline_interval_seconds
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(lambda: run_stage(stage, skip_unchanged=True), "interval", seconds=interval,
                      id="pipeline", max_instances=1, coalesce=True)
    logger.info("pipeline scheduler started — stage=%s every %d seconds", stage, interval)
    run_stage(stage, skip_unchanged=True)  # run immediately (always: nothing remembered yet)
    scheduler.start()
