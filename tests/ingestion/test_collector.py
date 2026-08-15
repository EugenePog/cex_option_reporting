"""Collector orchestration tests — no DB, no network. Uses fakes for connector + writer."""
from __future__ import annotations

from datetime import datetime, timezone

from app.connectors.base import (
    BalanceRow,
    ClosedPositionRow,
    FillRow,
    MarginInfo,
    OptionSummaryRow,
    PositionRow,
)
from app.ingestion.collector import Collector


class FakeConnector:
    cex_code = "OKX"

    def __init__(self) -> None:
        self.fills_since: datetime | None = None
        self.closed_since: datetime | None = None
        now = datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)
        self._pos = [PositionRow("BTC-USD-260319-70500-C", "short", -2, 0.01, 12.5, -0.3, 0.008, now,
                                 raw={"instId": "BTC-USD-260319-70500-C"})]
        self._bal = [BalanceRow("BTC", 1.5, 1.0, 90000, now, raw={"ccy": "BTC"})]
        self._mgn = [MarginInfo("ACCOUNT", 90000, 100, 50, 0.5, now, raw={})]
        self._iv = [OptionSummaryRow("BTC-USD-260319-70500-C", 0.6, 0.008, 0.007, 0.009,
                                     -0.4, 0.01, -0.02, 0.03, now, raw={})]

    def fetch_positions(self, subacct): return self._pos
    def fetch_balances(self, subacct): return self._bal
    def fetch_margin(self, subacct): return self._mgn
    def fetch_option_summary(self, inst_ids): return self._iv

    def fetch_fills(self, subacct, since):
        self.fills_since = since
        return [FillRow("t1", "BTC-USD-260319-70500-C", "sell", 2, 0.01, -0.3, "USDT",
                        datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc), raw={"tradeId": "t1"})]

    def fetch_closed_positions(self, subacct, since):
        self.closed_since = since
        closed = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
        return [ClosedPositionRow("p1", "BTC-USD-260319-70500-C", 5.0, 5.0, "3",
                                  0.01, 0.0, None, closed, closed, raw={"posId": "p1"})]


class FakeWriter:
    def __init__(self) -> None:
        self.runs: list[tuple[str, str]] = []   # (ingest_id, mode)
        self.calls: list[str] = []
        self.finished: list[tuple[str, str, int]] = []

    def start_run(self, mode):
        ingest_id = f"run-{len(self.runs)}"
        self.runs.append((ingest_id, mode))
        return ingest_id

    def write_positions(self, i, rows, sub=""): self.calls.append("positions"); return len(rows)
    def write_balances(self, i, rows, sub=""): self.calls.append("balances"); return len(rows)
    def write_margin(self, i, rows, sub=""): self.calls.append("margin"); return len(rows)
    def write_opt_summary(self, i, rows, sub=""): self.calls.append("opt_summary"); return len(rows)
    def write_fills(self, i, rows, sub=""): self.calls.append("fills"); return len(rows)
    def write_closed_positions(self, i, rows, sub=""): self.calls.append("closed"); return len(rows)

    def finish_run(self, ingest_id, status, row_count, error_text=None):
        self.finished.append((ingest_id, status, row_count))


def _fixed_now():
    return datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)


def test_daily_writes_all_layers_and_windows_fills():
    conn, writer = FakeConnector(), FakeWriter()
    c = Collector(conn, writer, now_fn=_fixed_now)

    ingest_id = c.collect_daily(lookback_days=1)

    assert writer.runs == [(ingest_id, "daily")]
    # snapshot + fills + closed positions all written
    assert writer.calls == ["positions", "balances", "margin", "opt_summary", "fills", "closed"]
    # fills + closed-position windows start at midnight of (today - 1 day) = 2026-08-11 00:00 UTC
    assert conn.fills_since == datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
    assert conn.closed_since == datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
    # run finished OK with total rows (1 each: positions, balances, margin, opt_summary, fills, closed)
    assert writer.finished == [(ingest_id, "OK", 6)]


def test_daily_lookback_two_days():
    conn, writer = FakeConnector(), FakeWriter()
    Collector(conn, writer, now_fn=_fixed_now).collect_daily(lookback_days=2)
    assert conn.fills_since == datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)


def test_backfill_uses_epoch_and_marks_mode():
    conn, writer = FakeConnector(), FakeWriter()
    c = Collector(conn, writer, now_fn=_fixed_now)

    c.backfill()

    assert writer.runs[0][1] == "backfill"
    # backfill requests fills AND closed positions from far in the past (full depth)
    assert conn.fills_since is not None and conn.fills_since.year <= 2017
    assert conn.closed_since is not None and conn.closed_since.year <= 2017
    assert "closed" in writer.calls
    assert writer.finished[0][1] == "OK"


def test_daily_failure_marks_run_error():
    class Boom(FakeConnector):
        def fetch_positions(self, subacct):
            raise RuntimeError("api down")

    writer = FakeWriter()
    c = Collector(Boom(), writer, now_fn=_fixed_now)
    try:
        c.collect_daily()
    except RuntimeError:
        pass
    assert writer.finished and writer.finished[0][1] == "ERROR"
