"""CEX connector abstraction — the extensibility seam.

Every exchange implements `BaseCexConnector`. Downstream code (ingestion, pipelines) depends only
on the base interface and the normalized dataclasses, never on a specific exchange SDK.
Add a new exchange by creating one subpackage (e.g. `bybit/`) implementing the same methods.
"""
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
from app.connectors.factory import make_connector

__all__ = [
    "BaseCexConnector",
    "Credentials",
    "BalanceRow",
    "PositionRow",
    "MarginInfo",
    "OptionSummaryRow",
    "FillRow",
    "ClosedPositionRow",
    "BillRow",
    "make_connector",
]
