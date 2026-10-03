"""Command-line entrypoints (Typer). pm2 and the Makefile call these.

    python -m app.cli init-db            # create tables (dev; prefer `alembic upgrade head`)
    python -m app.cli snapshot [--loop]  # point-in-time data; --loop fires at SNAPSHOT_TIMES_UTC (default hourly)
    python -m app.cli history  [--loop]  # fills/closed/bills; --loop fires at INGEST_TIME_UTC (default hourly)
    python -m app.cli backfill           # collect full available history once (manual)
    python -m app.cli index-candles [--since YYYY-MM-DD]  # BTC-USD 1m index candles (gaps only)
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
    wipe_links: bool = typer.Option(
        False, help="With --replace: allow deleting the Box builder's manual strategy links."),
) -> None:
    """Load core/settings CSVs (user, cex_account, subaccount, strategy, strategy_rule,
    contract_size) into the DB."""
    setup_logging()
    from app.db.seed_loader import SeedReplaceWouldWipeLinks, load_seed

    try:
        counts = load_seed(folder=folder, only=table, replace=replace, wipe_links=wipe_links)
    except SeedReplaceWouldWipeLinks as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    if not counts:
        typer.echo("No seed CSVs found.")
    for t, c in counts.items():
        typer.echo(f"core.{t}: {c} rows")


@app.command()
def snapshot(
    loop: bool = typer.Option(False, help="Run the snapshot scheduler (SNAPSHOT_TIMES_UTC)."),
    label: str = typer.Option(None, help="Only this account label (default: all accounts)."),
) -> None:
    """Collect point-in-time data (balance/positions/margin/greeks) into bronze.

    Runs once for EVERY account in core.cex_account (or just --label). --loop runs the
    scheduler that fires at each SNAPSHOT_TIMES_UTC time (default every hour), over all accounts.
    """
    setup_logging()
    if loop:
        from app.ingestion.scheduler import run_snapshot_scheduler

        run_snapshot_scheduler()
    else:
        from app.ingestion.collector import iter_account_collectors

        collectors = iter_account_collectors(only_label=label)
        if not collectors:
            typer.echo(
                "No CEX accounts to collect. "
                "Seed core.cex_account and set per-label creds in .env."
            )
            raise typer.Exit(1)
        for c in collectors:
            ingest_id = c.collect_snapshot()
            typer.echo(f"[{c.writer.account_label}] snapshot complete. ingest_id={ingest_id}")
        _sync_candles()


@app.command()
def history(
    loop: bool = typer.Option(False, help="Run the history scheduler (INGEST_TIME_UTC, default hourly)."),
    label: str = typer.Option(None, help="Only this account label (default: all accounts)."),
) -> None:
    """Collect history (fills/closed-positions/bills) over a limited window into bronze.

    Runs once for EVERY account in core.cex_account (or just --label). --loop runs the
    scheduler that fires at each INGEST_TIME_UTC time (default every hour), also over all
    accounts.
    """
    setup_logging()
    if loop:
        from app.ingestion.scheduler import run_history_scheduler

        run_history_scheduler()
    else:
        from app.ingestion.collector import iter_account_collectors

        collectors = iter_account_collectors(only_label=label)
        if not collectors:
            typer.echo(
                "No CEX accounts to collect. "
                "Seed core.cex_account and set per-label creds in .env."
            )
            raise typer.Exit(1)
        for c in collectors:
            ingest_id = c.collect_history()
            typer.echo(f"[{c.writer.account_label}] history complete. ingest_id={ingest_id}")
        _sync_candles()


def _sync_candles() -> None:
    """Top up the index candles after a one-shot snapshot / history run (like the loops)."""
    from app.ingestion.index_candles import sync_index_candles
    from config.settings import get_settings

    n = sync_index_candles(get_settings().ingest_daily_lookback_days)
    typer.echo(f"[market] index candles: {n} new")


@app.command("set-password")
def set_password(email: str = typer.Argument(...),
                 password: str = typer.Option(..., prompt=True, hide_input=True,
                                              confirmation_prompt=True)) -> None:
    """Set (or reset) a portal user's login password."""
    setup_logging()
    from sqlalchemy import select

    from app.db.base import session_scope
    from app.db.models_core import CoreUser
    from app.web.security import hash_password

    with session_scope() as s:
        user = s.execute(select(CoreUser).where(CoreUser.email == email)).scalar_one_or_none()
        if user is None:
            raise typer.BadParameter(f"No user with email {email!r} (seed core first).")
        user.password_hash = hash_password(password)
    typer.echo(f"Password set for {email}.")


@app.command()
def backfill(
    label: str = typer.Option(None, help="Only this account label (default: all accounts)."),
    candles: bool = typer.Option(True, help="Also backfill the index candles (after positions)."),
) -> None:
    """Collect the full available history depth from the exchange (manual, one-off).

    Runs for EVERY account in core.cex_account unless --label narrows it to one. Then, with all
    positions loaded, fills BTC-USD 1-minute index candles from the day of the earliest open or
    closed position in bronze (any account) up to now — missing minutes only.
    """
    setup_logging()
    from app.ingestion.collector import iter_account_collectors

    collectors = iter_account_collectors(only_label=label)
    if not collectors:
        typer.echo(
            "No CEX accounts to backfill. "
            "Seed core.cex_account and set per-label creds in .env."
        )
        raise typer.Exit(1)
    for c in collectors:
        ingest_id = c.backfill()
        typer.echo(f"[{c.writer.account_label}] backfill complete. ingest_id={ingest_id}")
    if candles:
        _backfill_candles(None)


def _backfill_candles(since) -> None:
    from app.ingestion.index_candles import make_index_candle_collector

    collector = make_index_candle_collector()
    if collector is None:
        typer.echo("[market] index candles disabled (INDEX_CANDLE_INST_IDS is empty).")
        return
    try:
        results = collector.backfill(since)
    except Exception as e:  # noqa: BLE001 - account data is already committed
        typer.echo(f"[market] index candle backfill FAILED: {e}", err=True)
        raise typer.Exit(1) from e
    if not results:
        typer.echo("[market] index candles: no positions in bronze yet — nothing to backfill.")
    for r in results:
        typer.echo(f"[market] index candles {r.inst_id} 1m: {r.written} new "
                   f"({r.start:%Y-%m-%d %H:%M} → {r.end:%Y-%m-%d %H:%M} UTC, "
                   f"{r.gaps} gap(s) filled)")


@app.command("index-candles")
def index_candles(
    since: str = typer.Option(None, help="Start date YYYY-MM-DD (UTC). Default: the day of the "
                                         "earliest open or closed position in bronze."),
) -> None:
    """Backfill index candles (INDEX_CANDLE_INST_IDS, default BTC-USD, 1m) — missing minutes only.

    The same step `backfill` runs after loading positions; use it to re-run or extend candles
    without re-collecting account history.
    """
    setup_logging()
    from datetime import datetime, timezone

    start = None
    if since:
        d = datetime.strptime(since, "%Y-%m-%d")
        start = d.replace(tzinfo=timezone.utc)
    _backfill_candles(start)


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
