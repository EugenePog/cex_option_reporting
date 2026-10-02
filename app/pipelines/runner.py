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


def run_silver() -> dict:
    return {"silver": bronze_to_silver.run()}


def run_gold() -> dict:
    return {"gold": silver_to_gold.run()}


def run_all() -> dict:
    return {**run_silver(), **run_gold()}


def run_stage(stage: str) -> dict:
    """stage ∈ {'silver', 'gold', 'all'}. Serialized across processes via pipeline_lock()."""
    runners = {"silver": run_silver, "gold": run_gold, "all": run_all}
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
    scheduler.add_job(lambda: run_stage(stage), "interval", seconds=interval, id="pipeline",
                      max_instances=1, coalesce=True)
    logger.info("pipeline scheduler started — stage=%s every %d seconds", stage, interval)
    run_stage(stage)  # run immediately, then on the interval
    scheduler.start()
