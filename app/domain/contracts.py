"""Contract sizes per exchange — a pure lookup over the rows of core.contract_size. No I/O.

Exchanges quote option positions in *contracts*; one contract stands for `ct_val` units of the
underlying coin (OKX: BTC-USD options 0.01 BTC, ETH-USD 0.1 ETH). The value depends on the exchange,
the instrument type and the underlying, so the key is (cex_code, inst_type, underlying).

The rows are loaded from the database by `app.db.contract_sizes.load_contract_sizes()`; this module
only answers questions about them, so it is easy to test.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Used only where a value is unavoidable (payoff scaling) and core.contract_size has no row:
# 1 contract = 1 coin, the previous default. Gold columns stay NULL instead (see size_in_coin).
DEFAULT_CT_VAL = 1.0


@dataclass(frozen=True)
class ContractSpec:
    cex_code: str
    inst_type: str        # 'OPTION' | 'FUTURES' | 'SWAP' | 'SPOT'
    underlying: str       # 'BTC-USD' (OKX instFamily)
    ct_val: float         # coin units per contract
    ct_val_ccy: str       # 'BTC'


def inst_type_of(inst_id: str | None) -> str | None:
    """OKX instrument type from an instId.

    'BTC-USD-260925-60000-C' → OPTION, 'BTC-USD-260925' → FUTURES, 'BTC-USD-SWAP' → SWAP,
    'BTC-USDT' → SPOT. Anything else → None.
    """
    if not inst_id:
        return None
    parts = inst_id.upper().split("-")
    if len(parts) == 5 and parts[4] in ("C", "P"):
        return "OPTION"
    if len(parts) == 3 and parts[2] == "SWAP":
        return "SWAP"
    if len(parts) == 3 and parts[2].isdigit():
        return "FUTURES"
    if len(parts) == 2:
        return "SPOT"
    return None


def coin_of(underlying: str | None) -> str | None:
    """Base coin of an underlying: 'BTC-USD' → 'BTC'."""
    return underlying.upper().split("-")[0] if underlying else None


class ContractSizes:
    """(cex_code, inst_type, underlying) → ContractSpec. Keys are case-insensitive."""

    def __init__(self, specs: Iterable[ContractSpec] = ()) -> None:
        self._by_key: dict[tuple[str, str, str], ContractSpec] = {}
        for s in specs:
            self._by_key[(s.cex_code.upper(), s.inst_type.upper(), s.underlying.upper())] = s
        self._warned: set[tuple[str, str, str]] = set()

    def __len__(self) -> int:
        return len(self._by_key)

    def get(self, cex_code: str | None, underlying: str | None,
            inst_type: str | None = "OPTION") -> ContractSpec | None:
        if not cex_code or not underlying or not inst_type:
            return None
        key = (cex_code.upper(), inst_type.upper(), underlying.upper())
        spec = self._by_key.get(key)
        if spec is None and key not in self._warned:
            self._warned.add(key)
            logger.warning("no contract size for %s %s %s in core.contract_size — add a row "
                           "(seed/contract_size.csv)", *key)
        return spec

    def ct_val(self, cex_code: str | None, underlying: str | None,
               inst_type: str | None = "OPTION") -> float | None:
        spec = self.get(cex_code, underlying, inst_type)
        return spec.ct_val if spec else None

    def ct_val_or_default(self, cex_code: str | None, underlying: str | None,
                          inst_type: str | None = "OPTION") -> float:
        """ct_val, or DEFAULT_CT_VAL (1.0) when the row is missing (warned once per key)."""
        v = self.ct_val(cex_code, underlying, inst_type)
        return v if v is not None else DEFAULT_CT_VAL

    def size_in_coin(self, cex_code: str | None, underlying: str | None,
                     contracts: float | None, inst_type: str | None = "OPTION") -> float | None:
        """contracts × ct_val, or None when the size or the contract size is unknown."""
        v = self.ct_val(cex_code, underlying, inst_type)
        if v is None or contracts is None:
            return None
        return float(contracts) * v

    def coin(self, cex_code: str | None, underlying: str | None,
             inst_type: str | None = "OPTION") -> str | None:
        """Coin the size is counted in: the row's ct_val_ccy, else the underlying's base coin."""
        spec = self._by_key.get(((cex_code or "").upper(), (inst_type or "").upper(),
                                 (underlying or "").upper()))
        return spec.ct_val_ccy if spec else coin_of(underlying)
