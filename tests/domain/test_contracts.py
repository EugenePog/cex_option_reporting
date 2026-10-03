"""Contract sizes per exchange (core.contract_size lookup) + instrument type + expiry time."""
from __future__ import annotations

from datetime import date, datetime, timezone

from app.domain.contracts import ContractSizes, ContractSpec, coin_of, inst_type_of
from app.domain.instruments import expires_at

SIZES = ContractSizes([
    ContractSpec("OKX", "OPTION", "BTC-USD", 0.01, "BTC"),
    ContractSpec("OKX", "OPTION", "ETH-USD", 0.1, "ETH"),
    ContractSpec("DERIBIT", "OPTION", "BTC-USD", 1.0, "BTC"),   # another exchange, other size
])


def test_lookup_is_per_exchange():
    assert SIZES.ct_val("OKX", "BTC-USD") == 0.01
    assert SIZES.ct_val("okx", "eth-usd") == 0.1                 # case-insensitive
    assert SIZES.ct_val("DERIBIT", "BTC-USD") == 1.0             # same underlying, other exchange
    assert SIZES.ct_val("OKX", "BTC-USD", "FUTURES") is None     # type is part of the key


def test_missing_row():
    assert SIZES.ct_val("OKX", "DOGE-USD") is None
    assert SIZES.ct_val(None, "BTC-USD") is None
    assert SIZES.ct_val_or_default("OKX", "DOGE-USD") == 1.0     # payoff fallback: 1 ct = 1 coin
    assert SIZES.size_in_coin("OKX", "DOGE-USD", 3) is None       # gold stays NULL instead
    assert SIZES.coin("OKX", "DOGE-USD") == "DOGE"               # coin still known from underlying


def test_size_in_coin():
    assert SIZES.size_in_coin("OKX", "BTC-USD", 2) == 0.02
    assert SIZES.size_in_coin("OKX", "ETH-USD", 5) == 0.5
    assert SIZES.size_in_coin("OKX", "BTC-USD", None) is None
    assert SIZES.coin("OKX", "BTC-USD") == "BTC"


def test_inst_type_of():
    assert inst_type_of("BTC-USD-260925-60000-C") == "OPTION"
    assert inst_type_of("BTC-USD-260925-60000-p") == "OPTION"
    assert inst_type_of("BTC-USD-260925") == "FUTURES"
    assert inst_type_of("BTC-USD-SWAP") == "SWAP"
    assert inst_type_of("BTC-USDT") == "SPOT"
    assert inst_type_of("") is None and inst_type_of(None) is None
    assert coin_of("BTC-USD") == "BTC" and coin_of(None) is None


def test_expires_at_is_0800_utc():
    assert expires_at(date(2026, 9, 25)) == datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)
    assert expires_at(None) is None
