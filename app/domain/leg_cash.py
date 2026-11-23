"""Option cash in USD at operation-time prices (build 21) — pure, no I/O.

What graph ①'s future-PnL switches add, per position leg (one OKX position, posId + cTime):

  A  open_usd      premium received (+) / paid (−) and fees of the quantity STILL OPEN
  B  realized_usd  realized PnL of the quantity already CLOSED (traded or settled at expiry)

Every amount is converted to USD at the index price of the moment it happened — a trade at its
own index price (OKX fills `fillIdxPx`), otherwise the middle ((high + low) / 2) of the 1-minute
index candle of that moment — and then stays fixed, so the profile only changes when positions do.
Average cost (as OKX): a closing trade realizes its share of the open quantity's USD cost (premium
+ fees at the OPENING prices) plus its own cash and fee at the CLOSING price; A is what is left.

Events come from the best source available for the leg:

  fills      every trade of the leg (silver.trade_fill) — exact; used when they account for the
             whole position (first trade at the position's opening, net quantity = the size now,
             0 after a traded close, what expired after an expiry). An expiry adds one settlement
             event: realizedPnl (positions-history) − the trades' cash and fees, at the settlement
             price.
  snapshots  open legs whose trades are not all there (opened before fills were collected, or the
             newest trade not collected yet): the leg's position snapshots replayed — a size
             increase = an opening trade (cost = avgPx·size change), a decrease = a closing trade
             (closing cash from the change in OKX `pnl`), fee changes with them; each at the
             snapshot's index price (snapshots every minute ≈ the trade's minute). What the first
             snapshot already shows is taken as opened at the position's opening time.
  summary    closed legs without all their trades: positions-history only — opening premium
             (openAvgPx × size) at the opening time, closing cash (pnl − opening cash) and the
             WHOLE fee at the closing time (expiry: the settlement price).

Coin amounts follow OKX's sign: a cost is negative (fees, premium paid).

build 23 / 24 — the leg's P&L split for graph ①'s hovers, in coin and in USD at the same
operation-time prices:
  premium − fees  the OPENING side: premium received (+) / paid (−) of every trade that opened or
                  added to the position, with those trades' fees, USD at the opening prices;
  closing         everything else: buy-backs / sales that reduced or closed it, the expiry
                  settlement (payout), their fees, USD at the closing prices (expiry: settlement);
  P&L             = premium − fees + closing (for a closed leg = OKX realizedPnl in coin).
Without trade data (summary source) a traded close keeps the WHOLE fee at the closing price (build
21); an expiry has no closing trade, so its whole fee goes with the opening (build 24).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

STABLE = frozenset({"USDT", "USDC", "USD"})   # fees charged in these are already USD
OPEN_MATCH = timedelta(seconds=120)           # first trade vs the position's opening time
TOL = 1e-9


@dataclass(frozen=True)
class CashEvent:
    at: datetime
    qty: float                  # coin: + adds to the position, − closes part of it, 0 = lump
    cash: float                 # coin: premium received (+) / paid (−), no fee; lump: the amount
    fee: float = 0.0            # coin (OKX sign: a cost is negative)
    px: float | None = None     # USD per coin at the event
    fee_usd: float | None = None   # a fee already in USD (charged in a stablecoin)
    settle: bool = False        # build 23: `cash` is the expiry settlement, not a premium (closing)


@dataclass
class LegCash:
    open_usd: float
    realized_usd: float
    source: str                 # fills | snapshots | summary
    missing_px: int = 0
    premium_coin: float | None = None   # build 23/24: premium − fees of the opening trades, coin
    premium_usd: float | None = None    # ... USD at the opening prices
    close_coin: float | None = None     # build 24: closing trades + settlement + their fees, coin
    close_usd: float | None = None      # ... USD at the closing prices (expiry: settlement price)
    fee_usd: float | None = None        # build 25: all fees of the leg, USD at their operations' prices


@dataclass(frozen=True)
class Fill:
    at: datetime
    side: str                   # buy | sell
    size: float                 # contracts
    price: float                # premium, coin per coin of underlying
    fee: float = 0.0
    fee_ccy: str | None = None
    idx_px: float | None = None


@dataclass(frozen=True)
class Snap:
    at: datetime
    size: float                 # contracts, magnitude
    avg_px: float | None
    fee: float | None
    pnl: float | None           # OKX `pnl`: closing trades' PnL, coin, no fees
    realized_pnl: float | None = None
    idx_px: float | None = None


@dataclass
class LegInfo:
    status: str                 # open | closed | stale
    side: str | None            # long | short
    opened_at: datetime
    closed_at: datetime | None = None
    close_type: str | None = None   # close | expiry
    size: float | None = None   # contracts (open: now; closed: the max held)
    entry_px: float | None = None
    fee: float | None = None    # closed: the whole fee of the position
    realized_pnl: float | None = None   # closed: OKX realizedPnl (pnl + fee)
    fills: Sequence[Fill] = field(default_factory=tuple)
    snaps: Sequence[Snap] = field(default_factory=tuple)


def _sign(side: str | None) -> float:
    """Opening cash per unit of premium: a long buys (pays, −), a short sells (receives, +)."""
    return -1.0 if (side or "").lower() == "long" else 1.0


def apply(events: Iterable[CashEvent]) -> tuple[float, float, int]:
    """(open_usd, realized_usd, events without a price) by average cost, in time order."""
    qty = basis = realized = 0.0
    missing = 0
    for e in sorted(events, key=lambda ev: ev.at):
        if e.px is None and (e.cash or (e.fee and e.fee_usd is None)):
            missing += 1
        px = e.px or 0.0
        usd = e.cash * px + (e.fee_usd if e.fee_usd is not None else e.fee * px)
        if e.qty > TOL:
            qty += e.qty
            basis += usd
        elif e.qty < -TOL:
            q = -e.qty
            frac = min(q / qty, 1.0) if qty > TOL else 1.0
            realized += basis * frac + usd
            basis -= basis * frac
            qty = max(qty - q, 0.0)
        else:
            realized += usd
    return basis, realized, missing


def premium_net(events: Iterable[CashEvent]) -> tuple[float, float, float, float, float]:
    """build 24 — (premium coin, premium USD, closing coin, closing USD, build 25: fees USD). An event that opens or
    adds to the position (qty > 0) is premium − fees; every other one (reductions, closes, the
    expiry settlement, lumps) is closing. Cash + fee of each, USD at the event's own price (an event
    without one adds 0 USD, it is counted in `missing_px`); a fee charged in a stablecoin is
    converted to coin at the event's price."""
    p_coin = p_usd = c_coin = c_usd = fees_usd = 0.0
    for e in events:
        if e.fee_usd is not None:
            fee_coin, fee_usd = (e.fee_usd / e.px if e.px else 0.0), e.fee_usd
        else:
            fee_coin, fee_usd = e.fee, e.fee * (e.px or 0.0)
        fees_usd += fee_usd
        coin, usd = e.cash + fee_coin, e.cash * (e.px or 0.0) + fee_usd
        if e.qty > TOL and not e.settle:
            p_coin, p_usd = p_coin + coin, p_usd + usd
        else:
            c_coin, c_usd = c_coin + coin, c_usd + usd
    return p_coin, p_usd, c_coin, c_usd, fees_usd


def fill_events(fills: Iterable[Fill], side: str | None, ct_val: float,
                px_at: Callable[[datetime], float | None]) -> list[CashEvent]:
    out = []
    long_ = (side or "").lower() == "long"
    for f in fills:
        q = abs(f.size) * ct_val
        buy = (f.side or "").lower() == "buy"
        stable = (f.fee_ccy or "").upper() in STABLE
        out.append(CashEvent(
            at=f.at, qty=q if buy == long_ else -q, cash=-q * f.price if buy else q * f.price,
            fee=0.0 if stable else (f.fee or 0.0), fee_usd=(f.fee or 0.0) if stable else None,
            px=f.idx_px or px_at(f.at)))
    return out


def fills_cover(events: Sequence[CashEvent], leg: LegInfo, size_coin: float | None) -> bool:
    """Do the trades account for the whole position (see the module docstring)?"""
    if not events or min(e.at for e in events) > leg.opened_at + OPEN_MATCH:
        return False
    net = sum(e.qty for e in events)
    tol = TOL + 1e-6 * max(abs(net), abs(size_coin or 0.0))
    if leg.status == "open":
        return size_coin is not None and abs(net - size_coin) <= tol
    if leg.close_type == "expiry":
        return net >= -tol and leg.realized_pnl is not None
    return abs(net) <= tol


def snapshot_events(snaps: Sequence[Snap], side: str | None, ct_val: float, opened_at: datetime,
                    px_at: Callable[[datetime], float | None]) -> list[CashEvent]:
    out, prev = [], None
    sg = _sign(side)
    for s in sorted(snaps, key=lambda x: x.at):
        q, avg, fee = abs(s.size) * ct_val, s.avg_px or 0.0, s.fee or 0.0
        pnl = s.pnl if s.pnl is not None else (
            (s.realized_pnl - fee) if s.realized_pnl is not None else 0.0)
        px = s.idx_px or px_at(s.at)
        if prev is None:
            out.append(CashEvent(at=opened_at, qty=q, cash=sg * avg * q, fee=fee,
                                 px=px_at(opened_at) or px))
            if abs(pnl) > TOL:                         # closed before we first saw the leg
                out.append(CashEvent(at=s.at, qty=0.0, cash=pnl, px=px))
        else:
            pq, pavg, pfee, ppnl = prev
            dq, dfee, dpnl = q - pq, fee - pfee, pnl - ppnl
            if dq > TOL:
                out.append(CashEvent(at=s.at, qty=dq, cash=sg * (avg * q - pavg * pq), fee=dfee,
                                     px=px))
                if abs(dpnl) > TOL:
                    out.append(CashEvent(at=s.at, qty=0.0, cash=dpnl, px=px))
            elif dq < -TOL:
                closed = -dq
                out.append(CashEvent(at=s.at, qty=dq, cash=dpnl - sg * pavg * closed, fee=dfee,
                                     px=px))
            elif abs(dfee) > TOL or abs(dpnl) > TOL:
                out.append(CashEvent(at=s.at, qty=0.0, cash=dpnl + dfee, px=px))
        prev = (q, avg, fee, pnl)
    return out


def summary_events(leg: LegInfo, ct_val: float, px_open: float | None,
                   px_close: float | None) -> list[CashEvent]:
    q = abs(leg.size or 0.0) * ct_val
    opening = _sign(leg.side) * (leg.entry_px or 0.0) * q
    expiry = leg.status == "closed" and leg.close_type == "expiry"
    fee = (leg.fee or 0.0) if leg.status == "closed" else 0.0
    # build 24: an expiry has no closing trade — its whole fee goes with the opening trade
    out = [CashEvent(at=leg.opened_at, qty=q, cash=opening, fee=fee if expiry else 0.0,
                     px=px_open)]
    if leg.status == "closed":
        pnl = (leg.realized_pnl or 0.0) - fee            # realizedPnl = pnl + fee
        out.append(CashEvent(at=leg.closed_at or leg.opened_at, qty=-q, cash=pnl - opening,
                             fee=0.0 if expiry else fee,  # traded close: whole fee at closing
                             px=px_close, settle=expiry))   # expired: cash = settlement
    return out


@dataclass(frozen=True)
class PremiumSpan:
    """build 23 — one leg for the premium-per-day read-outs of graph ①."""
    start: datetime | None          # the position's opening
    end: datetime | None            # open: its expiry; closed: when it closed (expiry included)
    coin: float | None              # premium − fees (None = not computed for this leg)
    usd: float | None


def leg_end(status: str | None, expires_at: datetime | None, closed_at: datetime | None,
            last_seen_at: datetime | None = None) -> datetime | None:
    """End of a leg for its duration: an open leg runs to its expiry, a closed one to its close
    (an expiry close = the expiry); a stale leg (gone from the snapshots) to when it was last seen."""
    if status == "open":
        return expires_at
    if status == "closed":
        return closed_at or expires_at
    return closed_at or last_seen_at or expires_at


def premium_per_day(legs: Iterable[PremiumSpan]) -> dict:
    """build 23 — premium − fees of one leg or a box of legs, its duration in days (earliest
    opening → latest end) and premium − fees per day, in coin and USD. Legs without a premium are
    counted in `n_missing` (their dates still count)."""
    legs = list(legs)
    have = [x for x in legs if x.coin is not None]
    starts = [x.start for x in legs if x.start is not None]
    ends = [x.end for x in legs if x.end is not None]
    start, end = (min(starts) if starts else None), (max(ends) if ends else None)
    days = (end - start).total_seconds() / 86400 if start and end and end > start else None
    coin = sum(x.coin for x in have) if have else None
    usd = sum(x.usd or 0.0 for x in have) if have else None
    return {"coin": coin, "usd": usd, "start": start, "end": end, "days": days,
            "coin_per_day": coin / days if coin is not None and days else None,
            "usd_per_day": usd / days if usd is not None and days else None,
            "n_missing": len(legs) - len(have)}


def leg_cash(leg: LegInfo, ct_val: float | None, size_coin: float | None,
             px_at: Callable[[datetime], float | None],
             settle_px: float | None = None) -> LegCash | None:
    """A and B of one leg (None for a stale leg or one without a contract size)."""
    if ct_val is None or leg.status not in ("open", "closed"):
        return None
    expiry = leg.status == "closed" and leg.close_type == "expiry"
    px_close = (settle_px if expiry else None) or (px_at(leg.closed_at) if leg.closed_at else None)
    fev = fill_events(leg.fills, leg.side, ct_val, px_at)
    if fills_cover(fev, leg, size_coin):
        events, source = list(fev), "fills"
        if expiry:                                       # the rest settled at expiry
            net = sum(e.qty for e in fev)
            traded = sum(e.cash + (e.fee if e.fee_usd is None else 0.0) for e in fev)
            events.append(CashEvent(at=leg.closed_at or leg.opened_at, qty=-net,
                                    cash=(leg.realized_pnl or 0.0) - traded, px=px_close,
                                    settle=True))
    elif leg.status == "open" and leg.snaps:
        events, source = snapshot_events(leg.snaps, leg.side, ct_val, leg.opened_at, px_at), \
            "snapshots"
    else:
        events, source = summary_events(leg, ct_val, px_at(leg.opened_at), px_close), "summary"
    open_usd, realized_usd, missing = apply(events)
    if leg.status == "closed":                           # nothing is open any more
        realized_usd, open_usd = realized_usd + open_usd, 0.0
    p_coin, p_usd, c_coin, c_usd, f_usd = premium_net(events)
    return LegCash(open_usd=open_usd, realized_usd=realized_usd, source=source,
                   missing_px=missing, premium_coin=p_coin, premium_usd=p_usd,
                   close_coin=c_coin, close_usd=c_usd, fee_usd=f_usd)
