"""OKX implementation of BaseCexConnector.

Composes OkxClient (raw API) + mappers (normalization). Registered with the factory so
`make_connector("OKX", creds)` returns an instance.

NOTE on sub-accounts: this stub uses one credential set per connector. To address named
sub-accounts via a master key, thread `subacct` into the OKX `subAcct` parameter on the
relevant endpoints (left as a TODO where OKX supports it).
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.connectors.base import (
    BalanceRow,
    BaseCexConnector,
    BillRow,
    ClosedPositionRow,
    Credentials,
    FillRow,
    MarginInfo,
    OptionSummaryRow,
    PositionRow,
)
from app.connectors.factory import register
from app.connectors.okx import mappers
from app.connectors.okx.client import OkxClient


@register
class OkxConnector(BaseCexConnector):
    cex_code = "OKX"

    def __init__(self, credentials: Credentials) -> None:
        super().__init__(credentials)
        self._client = OkxClient(credentials)

    def fetch_balances(self, subacct: str) -> list[BalanceRow]:
        now = datetime.now(timezone.utc)
        return mappers.map_balances(self._client.get_account_balance(), now)

    def fetch_positions(self, subacct: str) -> list[PositionRow]:
        now = datetime.now(timezone.utc)
        return mappers.map_positions(self._client.get_option_positions(), now)

    def fetch_margin(self, subacct: str) -> list[MarginInfo]:
        now = datetime.now(timezone.utc)
        return mappers.map_margin(self._client.get_account_balance(), now)

    def fetch_option_summary(self, inst_ids: list[str]) -> list[OptionSummaryRow]:
        now = datetime.now(timezone.utc)
        rows: list[OptionSummaryRow] = []
        for inst_id in inst_ids:
            uly = mappers.uly_from_inst_id(inst_id)
            row = mappers.map_opt_summary(self._client.get_opt_summary(uly, inst_id), inst_id, now)
            if row is not None:
                rows.append(row)
        return rows

    def fetch_fills(self, subacct: str, since: datetime) -> list[FillRow]:
        """Option fills at or after `since`. Pass a very old `since` to backfill full depth."""
        since_ms = int(since.timestamp() * 1000)
        raw = self._client.get_fills_paginated(inst_type="OPTION", since_ms=since_ms)
        rows = mappers.map_fills({"data": raw})
        # Trim anything older than the requested window (last page may overshoot).
        return [r for r in rows if r.filled_at >= since]

    def fetch_closed_positions(self, subacct: str, since: datetime) -> list[ClosedPositionRow]:
        """Closed positions (incl. expiry/delivery) with realized PnL, at or after `since`."""
        since_ms = int(since.timestamp() * 1000)
        raw = self._client.get_positions_history_paginated(inst_type="OPTION", since_ms=since_ms)
        rows = mappers.map_closed_positions({"data": raw})
        return [r for r in rows if r.closed_at >= since]

    def fetch_bills(self, subacct: str, since: datetime) -> list[BillRow]:
        """Account ledger entries (OPTION) at or after `since`. ~1yr depth via bills-archive."""
        since_ms = int(since.timestamp() * 1000)
        raw = self._client.get_bills_paginated(inst_type="OPTION", since_ms=since_ms)
        rows = mappers.map_bills({"data": raw})
        return [r for r in rows if r.billed_at >= since]
