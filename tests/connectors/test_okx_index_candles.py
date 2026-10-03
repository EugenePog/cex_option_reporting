"""OKX index candles: mapping and backwards pagination against a simulated API (no network)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.connectors.okx import mappers
from app.connectors.okx.client import OkxTransientError
from app.connectors.okx.market import OkxMarketClient

UTC = timezone.utc
M = 60_000


class FakeOkx:
    """Serves 1m BTC-USD index candles for [data_start, now); the current minute is unconfirmed.
    Mimics OKX: newest first, `after` = strictly older than, at most `limit` (100) rows."""

    def __init__(self, data_start: datetime, now: datetime, fail_on_call: int | None = None):
        self.first = int(data_start.timestamp() * 1000)
        self.now = int(now.timestamp() * 1000) // M * M          # current (forming) candle
        self.calls: list[dict] = []
        self.fail_on_call = fail_on_call

    def __call__(self, url, params):
        self.calls.append(dict(params))
        if self.fail_on_call and len(self.calls) == self.fail_on_call:
            raise OkxTransientError("OKX history-index-candles failed: rate limit (code 50011)")
        assert url.endswith("/api/v5/market/history-index-candles")
        assert params["instId"] == "BTC-USD" and params["bar"] == "1m"
        limit = int(params["limit"])
        top = int(params["after"]) - M if "after" in params else self.now
        top = min(top, self.now)
        rows = []
        ts = top
        while ts >= self.first and len(rows) < limit:
            px = 60000 + (ts // M) % 1000
            rows.append([str(ts), str(px), str(px + 5), str(px - 5), str(px + 1),
                         "0" if ts == self.now else "1"])
            ts -= M
        return {"code": "0", "msg": "", "data": rows}


def _client(fake):
    return OkxMarketClient(http_get=fake, min_interval=0, sleep=lambda s: None)


def test_map_index_candles_fields_and_confirm():
    resp = {"data": [["1790000060000", "1", "2", "0.5", "1.5", "0"],
                     ["1790000000000", "1", "2", "0.5", "1.5", "1"]]}
    rows = mappers.map_index_candles(resp, "BTC-USD", "1m")
    assert [r.confirmed for r in rows] == [False, True]
    assert rows[1].ts == datetime.fromtimestamp(1790000000, tz=UTC)
    assert (rows[1].open, rows[1].high, rows[1].low, rows[1].close) == (1.0, 2.0, 0.5, 1.5)
    assert rows[1].raw == ["1790000000000", "1", "2", "0.5", "1.5", "1"]


def test_range_is_read_backwards_exactly_and_in_pages_of_100():
    now = datetime(2026, 10, 2, 21, 30, 20, tzinfo=UTC)
    fake = FakeOkx(datetime(2026, 9, 1, tzinfo=UTC), now)
    start, end = datetime(2026, 10, 2, 12, 0, tzinfo=UTC), datetime(2026, 10, 2, 17, 0, tzinfo=UTC)
    pages = list(_client(fake).iter_index_candles("BTC-USD", "1m", start, end))
    ts = sorted(r.ts for p in pages for r in p)
    assert len(ts) == 300 and ts[0] == start and ts[-1] == end - timedelta(minutes=1)
    assert len(fake.calls) == 3                       # 300 minutes / 100 per page
    assert fake.calls[0]["after"] == str(int(end.timestamp() * 1000))


def test_current_unconfirmed_candle_is_never_returned():
    now = datetime(2026, 10, 2, 21, 30, 20, tzinfo=UTC)
    fake = FakeOkx(datetime(2026, 9, 1, tzinfo=UTC), now)
    start = datetime(2026, 10, 2, 21, 0, tzinfo=UTC)
    rows = [r for p in _client(fake).iter_index_candles(
        "BTC-USD", "1m", start, now + timedelta(hours=1)) for r in p]
    assert rows and all(r.confirmed for r in rows)
    assert max(r.ts for r in rows) == datetime(2026, 10, 2, 21, 29, tzinfo=UTC)


def test_stops_when_okx_has_nothing_older():
    now = datetime(2026, 10, 2, 0, 0, tzinfo=UTC)
    fake = FakeOkx(datetime(2026, 10, 1, 22, 30, tzinfo=UTC), now)   # only 90 minutes exist
    rows = [r for p in _client(fake).iter_index_candles(
        "BTC-USD", "1m", datetime(2026, 9, 1, tzinfo=UTC), now) for r in p]
    assert len(rows) == 90 and len(fake.calls) <= 2


def test_transient_error_keeps_pages_already_read():
    now = datetime(2026, 10, 2, 21, 0, tzinfo=UTC)
    fake = FakeOkx(datetime(2026, 9, 1, tzinfo=UTC), now, fail_on_call=3)
    client = _client(fake)
    # bypass the retry decorator so the transient error ends the range immediately
    unwrapped = client.history_index_candles_page.__wrapped__
    client.history_index_candles_page = unwrapped.__get__(client)
    pages = list(client.iter_index_candles("BTC-USD", "1m", now - timedelta(minutes=500), now))
    assert sum(len(p) for p in pages) == 200          # 2 good pages, then stop (no exception)
