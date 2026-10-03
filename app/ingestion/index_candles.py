"""Index candles (BTC-USD, 1-minute) -> bronze.raw_index_candle.

Market data, shared by every account, so it is collected ONCE per run (not per account) with
OKX's public endpoint (no API key): `app.connectors.okx.market.OkxMarketClient`.

When it runs
  * `backfill` (make backfill): after every account's positions are loaded — from the UTC day of
    the earliest open or closed position in bronze (raw_position / raw_closed_position cTime) up
    to now.  Also on demand: `python -m app.cli index-candles [--since YYYY-MM-DD]`.
  * snapshot and history collectors (collect-snapshot-loop / collect-loop and their one-shot
    commands), at their existing times — from the latest stored candle up to now, re-checking the
    last INGEST_DAILY_LOOKBACK_DAYS + 1 days for holes.

Only MISSING minutes are requested: stored candles are scanned for gaps first (SQL, lag()), gaps
closer than one page apart are merged, and each gap is read backwards page by page. Inserts are
idempotent (unique (cex_code, inst_id, bar, ts)), so overlapping runs never duplicate a candle.
The current, still-forming minute is never stored (end bound = start of the current minute).
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.connectors.okx.market import PAGE_LIMIT, OkxMarketClient, bar_delta
from app.ingestion.bronze_writer import BronzeWriter

logger = logging.getLogger(__name__)

MARKET_LABEL = "market"           # ingest_run.account_label for market-data runs
BAR = "1m"
MODE_SYNC, MODE_BACKFILL = "candles", "candles_backfill"
PROGRESS_EVERY = 20_000           # log progress every N new candles during a long fill


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def floor_to(dt: datetime, step: timedelta) -> datetime:
    secs = int(step.total_seconds())
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp(epoch - epoch % secs, tz=timezone.utc)


def day_start(dt: datetime) -> datetime:
    d = dt.astimezone(timezone.utc).date()
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def compute_gaps(start: datetime, end: datetime, step: timedelta, first: datetime | None,
                 last: datetime | None, holes: Iterable[tuple[datetime, datetime]]
                 ) -> list[tuple[datetime, datetime]]:
    """Missing [from, to) ranges in [start, end) given the stored candles' first / last ts and
    the interior holes as (prev_ts, next_ts) pairs of neighbouring stored candles."""
    if start >= end:
        return []
    if first is None:
        return [(start, end)]
    gaps: list[tuple[datetime, datetime]] = []
    if first > start:
        gaps.append((start, first))
    for prev, nxt in sorted(holes):
        if nxt - prev > step:
            gaps.append((prev + step, nxt))
    if last + step < end:
        gaps.append((last + step, end))
    return gaps


def merge_gaps(gaps: list[tuple[datetime, datetime]], within: timedelta
               ) -> list[tuple[datetime, datetime]]:
    """Merge gaps separated by less than `within` (one OKX page) — one request covers both."""
    out: list[tuple[datetime, datetime]] = []
    for a, b in sorted(gaps):
        if out and a - out[-1][1] < within:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


# --------------------------------------------------------------------------- #
# DB access
# --------------------------------------------------------------------------- #
class CandleStore:
    """Reads what is already in bronze.raw_index_candle (and where the positions start)."""

    def __init__(self, cex_code: str = "OKX") -> None:
        self.cex_code = cex_code

    def _q(self, sql: str, **kw):
        from app.db.base import session_scope

        with session_scope() as s:
            return s.execute(text(sql), kw).all()

    def latest(self, inst_id: str, bar: str) -> datetime | None:
        return self._q("SELECT max(ts) FROM bronze.raw_index_candle WHERE cex_code=:c AND "
                       "inst_id=:i AND bar=:b", c=self.cex_code, i=inst_id, b=bar)[0][0]

    def gaps(self, inst_id: str, bar: str, start: datetime, end: datetime
             ) -> list[tuple[datetime, datetime]]:
        step = bar_delta(bar)
        kw = {"c": self.cex_code, "i": inst_id, "b": bar, "s": start, "e": end}
        where = "cex_code=:c AND inst_id=:i AND bar=:b AND ts >= :s AND ts < :e"
        first, last = self._q(f"SELECT min(ts), max(ts) FROM bronze.raw_index_candle WHERE {where}",
                              **kw)[0]
        holes = self._q(
            f"SELECT prev, ts FROM (SELECT ts, lag(ts) OVER (ORDER BY ts) AS prev "
            f"FROM bronze.raw_index_candle WHERE {where}) x "
            f"WHERE prev IS NOT NULL AND ts - prev > make_interval(secs => :step)",
            step=step.total_seconds(), **kw)
        return compute_gaps(start, end, step, first, last, [(p, t) for p, t in holes])

    def earliest_position_open(self) -> datetime | None:
        """Earliest position open time (OKX cTime) across ALL accounts in bronze: closed
        positions (raw_closed_position.pos_opened_at) and open/current ones (raw_position
        payload cTime)."""
        row = self._q(r"""
            SELECT least(
              (SELECT min(pos_opened_at) FROM bronze.raw_closed_position),
              (SELECT to_timestamp(min((payload->>'cTime')::bigint) / 1000.0)
                 FROM bronze.raw_position WHERE payload->>'cTime' ~ '^[0-9]{10,}$'))""")
        return row[0][0]


# --------------------------------------------------------------------------- #
# Collector
# --------------------------------------------------------------------------- #
@dataclass
class CandleResult:
    inst_id: str
    start: datetime
    end: datetime
    gaps: int
    written: int


class IndexCandleCollector:
    def __init__(self, client: OkxMarketClient, writer: BronzeWriter, store: CandleStore,
                 inst_ids: list[str], bar: str = BAR,
                 now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.client, self.writer, self.store = client, writer, store
        self.inst_ids, self.bar, self._now = inst_ids, bar, now_fn
        self.step = bar_delta(bar)

    def _end(self) -> datetime:
        """Exclusive end: the start of the current (still-forming) candle."""
        return floor_to(self._now(), self.step)

    def _fill(self, ingest_id: str, inst_id: str, start: datetime) -> CandleResult:
        end = self._end()
        start = floor_to(start, self.step)
        gaps = merge_gaps(self.store.gaps(inst_id, self.bar, start, end), self.step * PAGE_LIMIT)
        written, next_log = 0, PROGRESS_EVERY
        for a, b in sorted(gaps, reverse=True):              # newest gap first
            for page in self.client.iter_index_candles(inst_id, self.bar, a, b):
                written += self.writer.write_index_candles(ingest_id, page)
                if written >= next_log:
                    logger.info("index candles %s %s: %d new so far (at %s)", inst_id, self.bar,
                                written, page[-1].ts.isoformat())
                    next_log += PROGRESS_EVERY
        return CandleResult(inst_id, start, end, len(gaps), written)

    def _run(self, mode: str, starts: dict[str, datetime]) -> list[CandleResult]:
        ingest_id = self.writer.start_run(mode)
        results: list[CandleResult] = []
        try:
            for inst_id in self.inst_ids:
                res = self._fill(ingest_id, inst_id, starts[inst_id])
                results.append(res)
                logger.info("index candles %s %s %s: %d gap(s) in %s → %s, %d new candle(s)",
                            mode, inst_id, self.bar, res.gaps, res.start.isoformat(),
                            res.end.isoformat(), res.written)
            self.writer.finish_run(ingest_id, "OK", sum(r.written for r in results))
            return results
        except Exception as e:  # noqa: BLE001 - record failure on the run, then re-raise
            self.writer.finish_run(ingest_id, "ERROR", sum(r.written for r in results),
                                   error_text=str(e))
            logger.exception("index candles %s FAILED (ingest_id=%s)", mode, ingest_id)
            raise

    def sync_recent(self, lookback_days: int = 1) -> list[CandleResult]:
        """Collector loops: from the latest stored candle to now, re-checking the last
        `lookback_days + 1` days for holes (an empty table starts there; backfill fills older)."""
        window = day_start(self._now()) - timedelta(days=lookback_days)
        starts = {}
        for inst_id in self.inst_ids:
            latest = self.store.latest(inst_id, self.bar)
            starts[inst_id] = min(latest, window) if latest else window
        return self._run(MODE_SYNC, starts)

    def backfill(self, since: datetime | None = None) -> list[CandleResult]:
        """From `since` (default: UTC day of the earliest open/closed position) to now."""
        if since is None:
            first = self.store.earliest_position_open()
            if first is None:
                logger.warning("index candles backfill: no positions in bronze yet — skipped")
                return []
            since = day_start(first)
        return self._run(MODE_BACKFILL, {i: since for i in self.inst_ids})


def make_index_candle_collector() -> IndexCandleCollector | None:
    """Collector for INDEX_CANDLE_INST_IDS (default BTC-USD); None if candles are disabled."""
    from config.settings import get_settings

    inst_ids = get_settings().index_candle_inst_id_list()
    if not inst_ids:
        return None
    return IndexCandleCollector(OkxMarketClient(), BronzeWriter("OKX", MARKET_LABEL),
                                CandleStore("OKX"), inst_ids)


def sync_index_candles(lookback_days: int) -> int:
    """Used by the snapshot / history collectors: never raises (market data must not break the
    account collection). Returns the number of new candles."""
    try:
        collector = make_index_candle_collector()
        if collector is None:
            return 0
        return sum(r.written for r in collector.sync_recent(lookback_days))
    except Exception:  # noqa: BLE001
        logger.exception("index candle sync failed; collectors continue")
        return 0
