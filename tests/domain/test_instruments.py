"""inst_id parsing."""
from __future__ import annotations

from datetime import date

from app.domain.instruments import parse_inst_id


def test_parse_option_call():
    p = parse_inst_id("BTC-USD-260618-65500-C")
    assert p.underlying == "BTC-USD"
    assert p.opt_type == "C"
    assert p.strike == 65500.0
    assert p.expiry == date(2026, 6, 18)


def test_parse_option_put():
    p = parse_inst_id("BTC-USD-260226-65000-P")
    assert p.opt_type == "P" and p.strike == 65000.0 and p.expiry == date(2026, 2, 26)


def test_parse_non_option_returns_nones():
    p = parse_inst_id("BTC-USDT")           # spot-like, not 5 parts
    assert p.underlying is None and p.opt_type is None and p.strike is None and p.expiry is None
    empty = parse_inst_id("")
    assert empty.underlying is None
