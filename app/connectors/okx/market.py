"""OKX public market data — index candles (no API key needed).

GET /api/v5/market/history-index-candles?instId=BTC-USD&bar=1m&after=<ms>&limit=100

  * `instId` is the index (same as the option underlying, e.g. "BTC-USD").
  * At most 100 candles per request, NEWEST first. `after=<ms>` returns candles strictly older than
    that open time, so a range is read backwards page by page.
  * Each item: ["ts","o","h","l","c","confirm"] — `ts` = candle open time (ms), confirm "1" = done.
  * Public endpoint, limited per IP (about 10 requests / 2 s): requests are spaced by
    MIN_REQUEST_INTERVAL and transient errors (rate limit 50011, system busy, HTTP 429/5xx) are
    retried with backoff (same policy as OkxClient).

The installed python-okx SDK only wraps `index-candles` (recent data), so this calls REST directly.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from app.connectors.base import IndexCandleRow
from app.connectors.okx import mappers
from app.connectors.okx.client import (
    _BASE_URL,
    OkxTransientError,
    _raise_on_error,
    _retry_transient,
)

logger = logging.getLogger(__name__)

HISTORY_INDEX_CANDLES = "/api/v5/market/history-index-candles"
PAGE_LIMIT = 100                    # OKX maximum for index candles
MIN_REQUEST_INTERVAL = 0.22         # seconds between requests (~9 / 2 s, under the IP limit)
BAR_SECONDS = {"1m": 60}


def bar_delta(bar: str) -> timedelta:
    return timedelta(seconds=BAR_SECONDS[bar])


class OkxMarketClient:
    """Thin public-REST client. `http_get(url, params) -> dict` is injectable for tests."""

    cex_code = "OKX"

    def __init__(self, http_get: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
                 min_interval: float = MIN_REQUEST_INTERVAL,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._http_get = http_get or _httpx_get
        self._min_interval = min_interval
        self._sleep = sleep
        self._last_call = 0.0
        self.requests = 0

    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            self._sleep(wait)
        self._last_call = time.monotonic()

    @_retry_transient
    def history_index_candles_page(self, inst_id: str, bar: str, after_ms: int | None,
                                   limit: int = PAGE_LIMIT) -> dict[str, Any]:
        """One page: up to `limit` candles older than `after_ms` (newest first)."""
        params = {"instId": inst_id, "bar": bar, "limit": str(limit)}
        if after_ms is not None:
            params["after"] = str(after_ms)
        self._throttle()
        self.requests += 1
        resp = self._http_get(_BASE_URL + HISTORY_INDEX_CANDLES, params)
        _raise_on_error(resp, "history-index-candles")
        return resp

    def iter_index_candles(self, inst_id: str, bar: str, start: datetime,
                           end: datetime) -> Iterator[list[IndexCandleRow]]:
        """Yield pages of COMPLETED candles with start <= ts < end, newest page first.

        Pages backwards from `end` (OKX `after` is exclusive) until a page reaches `start` or OKX
        has nothing older. A persistent transient error ends the range early (already-yielded
        pages are kept; the next run fills the rest).
        """
        start_ms, cursor = _ms(start), _ms(end)
        while True:
            try:
                resp = self.history_index_candles_page(inst_id, bar, cursor)
            except OkxTransientError as e:
                logger.warning("index candles %s %s: stopped early before %s after transient "
                               "error: %s", inst_id, bar, _iso(cursor), e)
                return
            rows = mappers.map_index_candles(resp, inst_id, bar)
            if not rows:
                return
            keep = [r for r in rows if r.confirmed and start_ms <= _ms(r.ts) < _ms(end)]
            if keep:
                yield keep
            oldest = min(_ms(r.ts) for r in rows)
            if oldest <= start_ms or oldest >= cursor:   # reached the range start / no progress
                return
            cursor = oldest


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _httpx_get(url: str, params: dict[str, str]) -> dict[str, Any]:
    import httpx

    r = httpx.get(url, params=params, timeout=10.0)
    if r.status_code == 429 or r.status_code >= 500:
        raise OkxTransientError(f"OKX history-index-candles HTTP {r.status_code}")
    return r.json()
