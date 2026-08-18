"""Pipeline orchestration entrypoints (bronze -> silver -> gold), runnable per stage."""
from __future__ import annotations

import logging

from app.pipelines import bronze_to_silver, silver_to_gold

logger = logging.getLogger(__name__)


def run_silver() -> dict:
    return {"silver": bronze_to_silver.run()}


def run_gold() -> dict:
    return {"gold": silver_to_gold.run()}


def run_all() -> dict:
    return {**run_silver(), **run_gold()}


def run_stage(stage: str) -> dict:
    """stage ∈ {'silver', 'gold', 'all'}."""
    if stage == "silver":
        return run_silver()
    if stage == "gold":
        return run_gold()
    if stage == "all":
        return run_all()
    raise ValueError(f"unknown stage {stage!r} (expected silver|gold|all)")


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
