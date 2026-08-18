"""Command-line entrypoints (Typer). pm2 and the Makefile call these.

    python -m app.cli init-db            # create tables (dev; prefer `alembic upgrade head`)
    python -m app.cli snapshot [--loop]  # point-in-time data; --loop fires at SNAPSHOT_TIMES_UTC — pm2 module
    python -m app.cli history  [--loop]  # fills/closed/bills; --loop once/day at INGEST_TIME_UTC — pm2 module
    python -m app.cli backfill           # collect full available history once (manual)
    python -m app.cli pipeline [--stage silver|gold|all] [--loop]  # transforms (default: all)
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
def seed(
    folder: str = typer.Option("seed", help="Folder holding <table>.csv files."),
    table: str = typer.Option(None, help="Load only this table."),
    replace: bool = typer.Option(False, help="Truncate target tables before loading."),
) -> None:
    """Load core/settings CSVs (user, cex_account, subaccount, strategy, strategy_rule) into the DB."""
    setup_logging()
    from app.db.seed_loader import load_seed

    counts = load_seed(folder=folder, only=table, replace=replace)
    if not counts:
        typer.echo("No seed CSVs found.")
    for t, c in counts.items():
        typer.echo(f"core.{t}: {c} rows")


@app.command()
def snapshot(loop: bool = typer.Option(False, help="Run the snapshot scheduler (SNAPSHOT_TIMES_UTC).")) -> None:
    """Collect point-in-time data (balance/positions/margin/greeks) into bronze.

    --loop runs the scheduler that fires at each SNAPSHOT_TIMES_UTC entry (several times/day).
    """
    setup_logging()
    if loop:
        from app.ingestion.scheduler import run_snapshot_scheduler

        run_snapshot_scheduler()
    else:
        from app.ingestion.collector import make_okx_k_collector

        ingest_id = make_okx_k_collector().collect_snapshot()
        typer.echo(f"Snapshot collect complete. ingest_id={ingest_id}")


@app.command()
def history(loop: bool = typer.Option(False, help="Run the daily history scheduler (INGEST_TIME_UTC).")) -> None:
    """Collect history (fills/closed-positions/bills) over a limited window into bronze.

    --loop runs the scheduler that fires once/day at INGEST_TIME_UTC.
    """
    setup_logging()
    if loop:
        from app.ingestion.scheduler import run_history_scheduler

        run_history_scheduler()
    else:
        from app.ingestion.collector import make_okx_k_collector

        ingest_id = make_okx_k_collector().collect_history()
        typer.echo(f"History collect complete. ingest_id={ingest_id}")


@app.command()
def backfill() -> None:
    """Collect the full available history depth from the exchange (manual, one-off)."""
    setup_logging()
    from app.ingestion.collector import make_okx_k_collector

    ingest_id = make_okx_k_collector().backfill()
    typer.echo(f"Backfill complete. ingest_id={ingest_id}")


@app.command()
def pipeline(
    stage: str = typer.Option("all", help="Which stage to run: silver | gold | all."),
    loop: bool = typer.Option(False, help="Run continuously on a schedule."),
) -> None:
    """Run transforms. bronze->silver and silver->gold can run separately via --stage.

        python -m app.cli pipeline --stage silver
        python -m app.cli pipeline --stage gold
        python -m app.cli pipeline                 # both (silver then gold)
    """
    setup_logging()
    from app.pipelines import runner

    if stage not in ("silver", "gold", "all"):
        raise typer.BadParameter("stage must be silver | gold | all")

    if loop:
        runner.run_loop(stage=stage)
    else:
        results = runner.run_stage(stage)
        for st, tables in results.items():
            for table, val in tables.items():
                # silver returns (written, skipped); gold returns an int count
                if isinstance(val, tuple):
                    typer.echo(f"{st}.{table}: {val[0]} written, {val[1]} skipped")
                else:
                    typer.echo(f"{st}.{table}: {val} rows")


@app.command()
def worker(loop: bool = typer.Option(False, help="Run continuously.")) -> None:
    """Run alerts / scheduled reports (stub)."""
    setup_logging()
    typer.echo(f"[worker] loop={loop} — TODO: wire app.worker")


if __name__ == "__main__":
    app()
