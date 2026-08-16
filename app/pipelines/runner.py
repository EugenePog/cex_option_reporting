"""Pipeline orchestration entrypoints (bronze -> silver -> gold)."""
from __future__ import annotations

import logging

from app.pipelines import bronze_to_silver

logger = logging.getLogger(__name__)


def run_once() -> dict:
    """Run one full pipeline pass. Currently: bronze -> silver (gold to follow)."""
    silver = bronze_to_silver.run()
    return {"silver": silver}


def run_loop() -> None:
    """Run the pipeline on a schedule (interval from settings), keeping the process alive."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    from config.settings import get_settings

    interval = get_settings().pipeline_interval_seconds
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run_once, "interval", seconds=interval, id="pipeline",
                      max_instances=1, coalesce=True)
    logger.info("pipeline scheduler started — every %d seconds", interval)
    run_once()  # run immediately, then on the interval
    scheduler.start()
