"""Command-line entrypoints (Typer). pm2 and the Makefile call these.

    python -m app.cli init-db            # create tables (dev; prefer `alembic upgrade head`)
    python -m app.cli collect            # run ONE daily collection now
    python -m app.cli collect --loop     # run the scheduler (daily at INGEST_HOUR_UTC) — pm2 module
    python -m app.cli backfill           # collect full available history once (manual)
    python -m app.cli pipeline [--loop]  # bronze -> silver -> gold  (stub)
    python -m app.cli worker  [--loop]   # alerts / reports          (stub)
"""
from __future__ import annotations

import logging

import typer

from config.logging import setup_logging

app = typer.Typer(help="CEX option reporting — operational commands.")
logger = logging.getLogger(__name__)


@app.command("init-db")
def init_db() -> None:
    """Create schemas + tables from the ORM metadata (dev convenience)."""
    setup_logging()
    from app.db.base import create_all

    create_all()
    typer.echo("Database schemas and tables created.")


@app.command()
def collect(loop: bool = typer.Option(False, help="Run the daily scheduler instead of one pass.")) -> None:
    """Collect a daily snapshot + recent fills into bronze (once, or on a schedule with --loop)."""
    setup_logging()
    if loop:
        from app.ingestion.scheduler import run_scheduler

        run_scheduler()
    else:
        from app.ingestion.collector import make_okx_k_collector

        ingest_id = make_okx_k_collector().collect_daily()
        typer.echo(f"Daily collect complete. ingest_id={ingest_id}")


@app.command()
def backfill() -> None:
    """Collect the full available history depth from the exchange (manual, one-off)."""
    setup_logging()
    from app.ingestion.collector import make_okx_k_collector

    ingest_id = make_okx_k_collector().backfill()
    typer.echo(f"Backfill complete. ingest_id={ingest_id}")


@app.command()
def pipeline(loop: bool = typer.Option(False, help="Run continuously.")) -> None:
    """Run bronze -> silver -> gold transforms (stub)."""
    setup_logging()
    typer.echo(f"[pipeline] loop={loop} — TODO: wire app.pipelines.runner")


@app.command()
def worker(loop: bool = typer.Option(False, help="Run continuously.")) -> None:
    """Run alerts / scheduled reports (stub)."""
    setup_logging()
    typer.echo(f"[worker] loop={loop} — TODO: wire app.worker")


if __name__ == "__main__":
    app()
