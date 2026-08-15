"""OKX pagination resilience — a persistent transient error keeps already-collected pages."""
from __future__ import annotations

from app.connectors.base import Credentials
from app.connectors.okx.client import OkxClient, OkxTransientError


def _make_client() -> OkxClient:
    return OkxClient(Credentials("k", "s", "p", "0"))


def test_positions_history_returns_partial_on_transient_error():
    client = _make_client()
    page1 = [{"posId": str(i), "uTime": "1780000000000", "realizedPnl": "1"} for i in range(100)]
    calls = {"n": 0}

    def fake_page(inst_type, after, limit):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"data": page1}
        raise OkxTransientError("OKX get_positions_history failed: System error (code 50026)")

    client._positions_history_page = fake_page  # bypasses the retry decorator on purpose

    rows = client.get_positions_history_paginated(inst_type="OPTION", since_ms=0)
    assert len(rows) == 100          # page 1 preserved, no exception raised
    assert calls["n"] == 2           # attempted page 2, then stopped


def test_fills_returns_partial_on_transient_error():
    client = _make_client()
    page1 = [{"billId": str(i), "tradeId": str(i), "ts": "1780000000000"} for i in range(100)]
    calls = {"n": 0}

    def fake_page(inst_type, after, limit):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"data": page1}
        raise OkxTransientError("OKX get_fills failed: System busy (code 50013)")

    client._fills_page = fake_page

    rows = client.get_fills_paginated(inst_type="OPTION", since_ms=0)
    assert len(rows) == 100
    assert calls["n"] == 2
