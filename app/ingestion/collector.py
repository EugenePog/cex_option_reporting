"""Collector — pulls from a CEX connector and writes the bronze layer, in two modes.

  * daily    — a fresh point-in-time snapshot (positions/balances/margin/greeks) plus fills over a
               short lookback window (today + previous N days) so a missed run self-heals.
  * backfill — the same snapshot plus the *full* available fills history from the exchange.

Note: exchanges only expose *current* positions/balances/margin — there is no historical snapshot
to fetch. So "today + previous day" applies to fills/trade history; live snapshots are always "now".

The Collector takes its connector, writer, and clock by injection, so its logic is unit-testable
without a database or network (see tests/ingestion).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from app.connectors.base import BaseCexConnector
from app.ingestion.bronze_writer import BronzeWriter

logger = logging.getLogger(__name__)

# A "since the beginning of time" marker for full backfills.
_EPOCH = datetime(2017, 1, 1, tzinfo=timezone.utc)  # OKX options predate nothing relevant before this


class Collector:
    def __init__(
        self,
        connector: BaseCexConnector,
        writer: BronzeWriter,
        subacct: str = "",
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.connector = connector
        self.writer = writer
        self.subacct = subacct
        self._now = now_fn

    # -- shared snapshot step ---------------------------------------------- #
    def _write_snapshot(self, ingest_id: str) -> int:
        """Current positions + balances + margin + greeks. Returns rows written."""
        positions = self.connector.fetch_positions(self.subacct)
        n = self.writer.write_positions(ingest_id, positions, self.subacct)

        balances = self.connector.fetch_balances(self.subacct)
        n += self.writer.write_balances(ingest_id, balances, self.subacct)

        margin = self.connector.fetch_margin(self.subacct)
        n += self.writer.write_margin(ingest_id, margin, self.subacct)

        inst_ids = [p.inst_id for p in positions if p.inst_id]
        if inst_ids:
            greeks = self.connector.fetch_option_summary(inst_ids)
            n += self.writer.write_opt_summary(ingest_id, greeks, self.subacct)
        return n

    def _window_start(self, lookback_days: int) -> datetime:
        """Midnight UTC `lookback_days` before today (default 1 → today + yesterday)."""
        today = self._now().astimezone(timezone.utc).date()
        start_date = today - timedelta(days=lookback_days)
        return datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)

    # -- mode A1: point-in-time snapshot (runs several times/day) ----------- #
    def collect_snapshot(self) -> str:
        """Current balance / positions / margin / greeks. No history — safe to run often."""
        ingest_id = self.writer.start_run("snapshot")
        try:
            n = self._write_snapshot(ingest_id)
            self.writer.finish_run(ingest_id, "OK", n)
            logger.info("snapshot collect OK (ingest_id=%s, rows=%d)", ingest_id, n)
            return ingest_id
        except Exception as e:  # noqa: BLE001 - record failure, never crash the loop
            self.writer.finish_run(ingest_id, "ERROR", 0, error_text=str(e))
            logger.exception("snapshot collect FAILED (ingest_id=%s)", ingest_id)
            raise

    # -- mode A2: history over a limited window (once/day) ------------------ #
    def collect_history(self, lookback_days: int = 1) -> str:
        """Fills / closed positions / bills over today + `lookback_days` prior days."""
        ingest_id = self.writer.start_run("history")
        try:
            since = self._window_start(lookback_days)
            n = self.writer.write_fills(
                ingest_id, self.connector.fetch_fills(self.subacct, since), self.subacct)
            n += self.writer.write_closed_positions(
                ingest_id, self.connector.fetch_closed_positions(self.subacct, since), self.subacct)
            n += self.writer.write_bills(
                ingest_id, self.connector.fetch_bills(self.subacct, since), self.subacct)
            self.writer.finish_run(ingest_id, "OK", n)
            logger.info("history collect OK (ingest_id=%s, rows=%d, since=%s)",
                        ingest_id, n, since.date())
            return ingest_id
        except Exception as e:  # noqa: BLE001
            self.writer.finish_run(ingest_id, "ERROR", 0, error_text=str(e))
            logger.exception("history collect FAILED (ingest_id=%s)", ingest_id)
            raise

    # -- mode B: manual full backfill -------------------------------------- #
    def backfill(self) -> str:
        ingest_id = self.writer.start_run("backfill")
        try:
            n = self._write_snapshot(ingest_id)  # snapshot the current state too
            fills = self.connector.fetch_fills(self.subacct, _EPOCH)  # full depth
            n += self.writer.write_fills(ingest_id, fills, self.subacct)
            closed = self.connector.fetch_closed_positions(self.subacct, _EPOCH)  # expiry PnL, full depth
            n += self.writer.write_closed_positions(ingest_id, closed, self.subacct)
            bills = self.connector.fetch_bills(self.subacct, _EPOCH)  # ledger, full depth (~1yr)
            n += self.writer.write_bills(ingest_id, bills, self.subacct)
            self.writer.finish_run(ingest_id, "OK", n)
            logger.info("backfill OK (ingest_id=%s, rows=%d)", ingest_id, n)
            return ingest_id
        except Exception as e:  # noqa: BLE001
            self.writer.finish_run(ingest_id, "ERROR", 0, error_text=str(e))
            logger.exception("backfill FAILED (ingest_id=%s)", ingest_id)
            raise


# --------------------------------------------------------------------------- #
# Wiring for the dev OKX account (OKX_K_* env vars).
# --------------------------------------------------------------------------- #
def make_okx_k_collector() -> Collector:
    """Assemble a Collector for the OKX 'K' dev account from settings."""
    from app.connectors import make_connector
    from config.settings import get_settings

    s = get_settings()
    connector = make_connector("OKX", s.okx_k_credentials())
    writer = BronzeWriter(cex_code="OKX", account_label=s.okx_k_account_label)
    return Collector(connector, writer)
