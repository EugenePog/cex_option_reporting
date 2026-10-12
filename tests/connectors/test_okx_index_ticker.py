"""OKX current index price (market/index-tickers) for the alerts worker — no network."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.connectors.okx.client import OkxError
from app.connectors.okx.market import OkxMarketClient


def client(resp):
    calls = []

    def http_get(url, params):
        calls.append((url, dict(params)))
        return resp

    return OkxMarketClient(http_get=http_get, min_interval=0, sleep=lambda _s: None), calls


def test_index_ticker_returns_price_and_time():
    c, calls = client({"code": "0", "msg": "", "data": [
        {"instId": "BTC-USD", "idxPx": "62415.3", "high24h": "63000", "ts": "1791620000000"}]})
    assert c.index_ticker("BTC-USD") == (62415.3, datetime.fromtimestamp(1791620000, tz=UTC))
    assert calls == [("https://www.okx.com/api/v5/market/index-tickers", {"instId": "BTC-USD"})]


def test_index_ticker_without_price_is_none():
    c, _ = client({"code": "0", "msg": "", "data": []})
    assert c.index_ticker("BTC-USD") is None


def test_index_ticker_error_raises():
    c, _ = client({"code": "51001", "msg": "Instrument ID does not exist", "data": []})
    with pytest.raises(OkxError):
        c.index_ticker("NOPE-USD")
