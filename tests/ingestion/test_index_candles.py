"""Index-candle collector logic: gap detection / merging and the start of each run (no DB)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.connectors.base import IndexCandleRow
from app.ingestion.index_candles import (
    MODE_BACKFILL,
    MODE_SYNC,
    IndexCandleCollector,
    compute_gaps,
    merge_gaps,
)

UTC = timezone.utc
MIN = timedelta(minutes=1)
T = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def test_compute_gaps_empty_head_interior_tail():
    end = T + 60 * MIN
    assert compute_gaps(T, end, MIN, None, None, []) == [(T, end)]
    # stored 12:10..12:40 with a hole between 12:20 and 12:25
    gaps = compute_gaps(T, end, MIN, T + 10 * MIN, T + 40 * MIN, [(T + 20 * MIN, T + 25 * MIN)])
    assert gaps == [(T, T + 10 * MIN), (T + 21 * MIN, T + 25 * MIN), (T + 41 * MIN, end)]
    # nothing missing
    assert compute_gaps(T, end, MIN, T, end - MIN, []) == []


def test_merge_gaps_within_one_page():
    g = [(T, T + 5 * MIN), (T + 50 * MIN, T + 51 * MIN), (T + 300 * MIN, T + 301 * MIN)]
    assert merge_gaps(g, 100 * MIN) == [(T, T + 51 * MIN), (T + 300 * MIN, T + 301 * MIN)]


class FakeStore:
    def __init__(self, latest=None, earliest=None):
        self._latest, self._earliest, self.gap_calls = latest, earliest, []

    def latest(self, inst_id, bar):
        return self._latest

    def gaps(self, inst_id, bar, start, end):
        self.gap_calls.append((start, end))
        return [(start, end)]

    def earliest_position_open(self):
        return self._earliest


class FakeClient:
    def __init__(self):
        self.ranges = []

    def iter_index_candles(self, inst_id, bar, start, end):
        self.ranges.append((start, end))
        yield [IndexCandleRow(inst_id, bar, start, 1, 1, 1, 1, True, raw=[])]


class FakeWriter:
    def __init__(self):
        self.runs, self.finished, self.rows = [], [], 0

    def start_run(self, mode):
        self.runs.append(mode)
        return f"run-{len(self.runs)}"

    def write_index_candles(self, ingest_id, rows):
        self.rows += len(rows)
        return len(rows)

    def finish_run(self, ingest_id, status, n, error_text=None):
        self.finished.append((status, n))


NOW = datetime(2026, 10, 2, 21, 30, 40, tzinfo=UTC)


def _collector(store):
    return IndexCandleCollector(FakeClient(), FakeWriter(), store, ["BTC-USD"], now_fn=lambda: NOW)


def test_sync_recent_rechecks_lookback_window_and_excludes_current_minute():
    c = _collector(FakeStore(latest=NOW - timedelta(minutes=5)))
    c.sync_recent(lookback_days=1)
    start, end = c.store.gap_calls[0]
    assert start == datetime(2026, 10, 1, tzinfo=UTC)           # today + 1 previous day
    assert end == datetime(2026, 10, 2, 21, 30, tzinfo=UTC)     # current minute excluded
    assert c.writer.runs == [MODE_SYNC] and c.writer.finished == [("OK", 1)]


def test_sync_recent_after_downtime_starts_at_latest_candle():
    latest = datetime(2026, 9, 20, 3, 7, tzinfo=UTC)
    c = _collector(FakeStore(latest=latest))
    c.sync_recent(lookback_days=1)
    assert c.store.gap_calls[0][0] == latest


def test_backfill_starts_at_day_of_earliest_position():
    c = _collector(FakeStore(earliest=datetime(2026, 7, 25, 8, 3, 11, tzinfo=UTC)))
    c.backfill()
    assert c.store.gap_calls[0][0] == datetime(2026, 7, 25, tzinfo=UTC)
    assert c.writer.runs == [MODE_BACKFILL]


def test_backfill_without_positions_does_nothing():
    c = _collector(FakeStore(earliest=None))
    assert c.backfill() == [] and c.writer.runs == []
