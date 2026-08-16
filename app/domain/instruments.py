"""Parse exchange instrument ids into structured fields. Pure, no I/O."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class ParsedInstrument:
    underlying: str | None
    opt_type: str | None      # 'C' | 'P'
    strike: float | None
    expiry: date | None


def parse_inst_id(inst_id: str) -> ParsedInstrument:
    """Parse an OKX option instId, e.g. 'BTC-USD-260618-65500-C'.

    Layout: <base>-<quote>-<YYMMDD>-<strike>-<C|P>. Returns Nones for anything that doesn't fit
    (e.g. a spot/futures id), so callers can store partial data without crashing.
    """
    if not inst_id:
        return ParsedInstrument(None, None, None, None)

    parts = inst_id.split("-")
    if len(parts) != 5:
        return ParsedInstrument(None, None, None, None)

    base, quote, yymmdd, strike_s, opt = parts
    underlying = f"{base}-{quote}"

    try:
        expiry: date | None = datetime.strptime(yymmdd, "%y%m%d").date()
    except ValueError:
        expiry = None
    try:
        strike: float | None = float(strike_s)
    except ValueError:
        strike = None

    opt_type = opt.upper() if opt.upper() in ("C", "P") else None
    return ParsedInstrument(underlying=underlying, opt_type=opt_type, strike=strike, expiry=expiry)
