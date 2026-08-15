"""Base CEX connector interface and normalized data primitives.

Design goals:
  * Downstream (ingestion/pipelines) depends ONLY on this module, never on an exchange SDK.
  * Each connector returns *normalized* rows plus keeps the *raw* payload, so the bronze layer
    can store the exact API response while silver/gold work off typed fields.
  * Adding a new exchange = implement the abstract methods in a new subpackage.

The `raw` field on every row is the untouched dict the exchange returned; the ingestion layer
writes `raw` to bronze `payload JSONB`, and pipelines derive silver rows from the typed fields.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Credentials:
    """Decrypted credentials for one CEX account. Never persisted in plaintext."""

    api_key: str
    api_secret: str
    passphrase: str = ""          # OKX requires this; others may not
    flag: str = "0"               # OKX: "0" live, "1" demo. Free-form per exchange.
    extra: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Normalized rows (typed fields the silver layer consumes; `raw` -> bronze)
# --------------------------------------------------------------------------- #
@dataclass
class BalanceRow:
    ccy: str
    total: float
    available: float
    usd_value: float
    captured_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PositionRow:
    inst_id: str                  # e.g. "BTC-USD-260319-70500-C"
    side: str                     # "long" / "short" (posSide)
    size: float
    avg_px: float
    upl: float                    # unrealized PnL
    fee: float | None
    mark_px: float | None
    captured_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MarginInfo:
    scope: str                    # "ACCOUNT" or a currency code
    eq_usd: float
    imr_usd: float
    mmr_usd: float
    margin_ratio: float
    captured_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class OptionSummaryRow:
    """IV + greeks for a single instrument (mark price, delta/gamma/theta/vega)."""

    inst_id: str
    iv: float
    mark_px: float
    bid_px: float
    ask_px: float
    delta: float
    gamma: float
    theta: float
    vega: float
    captured_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class FillRow:
    """A single trade fill — the basis for realized PnL / the deal ledger."""

    trade_id: str
    inst_id: str
    side: str                     # "buy" / "sell"
    size: float
    price: float
    fee: float
    fee_ccy: str
    filled_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# The interface
# --------------------------------------------------------------------------- #
class BaseCexConnector(ABC):
    """Read-only market/account access for one CEX account.

    Implementations wrap the exchange SDK/REST and map responses to the normalized rows above.
    All methods take a `subacct` name so a single credential set can address multiple sub-accounts
    where the exchange supports it (e.g. OKX master key + subAcct param).
    """

    #: Short uppercase code stored on every row (e.g. "OKX", "BYBIT"). Set by each subclass.
    cex_code: str = "BASE"

    def __init__(self, credentials: Credentials) -> None:
        self.credentials = credentials

    # -- account / positions ------------------------------------------------ #
    @abstractmethod
    def fetch_balances(self, subacct: str) -> list[BalanceRow]:
        """Current balances per currency for the sub-account."""

    @abstractmethod
    def fetch_positions(self, subacct: str) -> list[PositionRow]:
        """Open OPTION positions for the sub-account."""

    @abstractmethod
    def fetch_margin(self, subacct: str) -> list[MarginInfo]:
        """Margin / equity / IMR / MMR (account-level and/or per-currency)."""

    # -- market ------------------------------------------------------------- #
    @abstractmethod
    def fetch_option_summary(self, inst_ids: list[str]) -> list[OptionSummaryRow]:
        """IV and greeks for the given instruments (typically the open positions)."""

    # -- history ------------------------------------------------------------ #
    @abstractmethod
    def fetch_fills(self, subacct: str, since: datetime) -> list[FillRow]:
        """Trade fills since `since` — feeds realized PnL and the deal ledger."""

    # -- convenience -------------------------------------------------------- #
    def health_check(self) -> bool:
        """Cheap credential/connectivity probe. Override with a real lightweight call."""
        try:
            self.fetch_balances(subacct="")
            return True
        except Exception:  # noqa: BLE001 - health check must never raise
            return False
