"""Smoke test: prove the connector layer imports and normalizes payloads — no DB, no network, no SDK.

Run:  python scripts/smoke_connector.py
Expect: prints normalized rows and "SMOKE OK".
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as `python scripts/smoke_connector.py` before `make install`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.connectors import make_connector
from app.connectors.base import BaseCexConnector, Credentials
from app.connectors.factory import supported_cexes
from app.connectors.okx import mappers


def main() -> None:
    now = datetime.now(timezone.utc)
    print("supported CEXes:", supported_cexes())

    # Fake OKX-shaped payloads (same shape the SDK returns).
    balance = {"data": [{"details": [
        {"ccy": "BTC", "eq": "1.5", "availEq": "1.0", "eqUsd": "90000"}
    ]}]}
    positions = {"data": [
        {"instId": "BTC-USD-260319-70500-C", "posSide": "short", "pos": "-2",
         "avgPx": "0.01", "upl": "12.5", "fee": "-0.3", "markPx": "0.008"}
    ]}

    bals = mappers.map_balances(balance, now)
    poss = mappers.map_positions(positions, now)
    print("balance row:", bals[0])
    print("position row:", poss[0])
    print("underlying parse:", mappers.uly_from_inst_id("BTC-USD-260319-70500-C"))

    conn = make_connector("OKX", Credentials("key", "secret", "pass"))
    assert isinstance(conn, BaseCexConnector) and conn.cex_code == "OKX"

    try:
        make_connector("BYBIT", Credentials("k", "s"))
    except ValueError as e:
        print("unknown CEX handled:", str(e)[:50], "...")

    print("SMOKE OK")


if __name__ == "__main__":
    main()
