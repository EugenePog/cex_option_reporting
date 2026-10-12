"""Telegram alerts for the boxes of graph ① "Price with strategy boxes". Pure, no I/O.

Notification codes — core.notification_setting, one row per asset × code (the same settings for
every account and box of that asset). Every code has a threshold (build 7), so the critical tier
(a, b, e) and the early tier (c, d, f) can each be made as strict as wanted:

    a  top_strike        top reached     price ≥ top strike − X %          X = threshold (default 0)
    b  bottom_strike     bottom reached  price ≤ bottom strike + Y %       Y = threshold (default 0)
    c  near_top_pct      top near        price ≥ top strike − X %          X = threshold (default 2)
    d  near_bottom_pct   bottom near     price ≤ bottom strike + Y %       Y = threshold (default 2)
    e  last_expiry       expiry reached  time ≥ last expiry − N days       N = threshold (default 0)
    f  last_expiry_days  expiry near     time ≥ last expiry − N days       N = threshold (default 1)

With threshold 0, a / b fire at the strike itself and e at the expiry moment.

The box is the one graph ① draws (gold.box_shape: all legs of one strategy on one underlying): top /
bottom = its highest / lowest strike, last expiry = its right edge. Only ALIVE boxes are watched —
at least one leg still open: status open and not past its expiry (an option can't be open after it
expired, even before the close is collected). The `unassigned` legs are not a box and are skipped.

Each notification goes out once per crossing:
  * price codes (a–d) are DISARMED when sent and RE-ARMED once the price is back on the other side;
    the same notification then waits at least `rearm` after the previous one (no ping-pong around
    a strike). When a box stops being alive, its price codes are re-armed.
  * time codes (e, f) are sent once per last-expiry value — a box that rolls on to a later expiry
    gets them again. A time code is due from `last expiry − N days` while the box is alive; a check
    that comes late (worker paused) still sends it until max(last expiry, due time + `grace`),
    provided the box was alive right before its last expiry (not when every leg was closed early).
    With N = 0 that is: from the expiry moment, for `grace`.
  * The critical tier covers the early tier of the same side: when a and c are both true, only a is
    sent and c is marked done; the same for b over d, and for e over f once e is due — unless the
    critical code is switched off.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from app.domain.boxes import Leg

CODE_LETTER = {"top_strike": "a", "bottom_strike": "b", "near_top_pct": "c",
               "near_bottom_pct": "d", "last_expiry": "e", "last_expiry_days": "f"}
PRICE_CODES = ("top_strike", "bottom_strike", "near_top_pct", "near_bottom_pct")
TIME_CODES = ("last_expiry", "last_expiry_days")
TOP_CODES = ("top_strike", "near_top_pct")          # level = top strike − X %, hit when price ≥
BOTTOM_CODES = ("bottom_strike", "near_bottom_pct")  # level = bottom strike + Y %, hit when price ≤
SUPPRESSED_BY = {"near_top_pct": "top_strike", "near_bottom_pct": "bottom_strike",
                 "last_expiry_days": "last_expiry"}

CRITICAL_MARK, NORMAL_MARK = "🔴", "🟡"

# Settings → Telegram notifications (build 6; build 7: every code has a threshold): how each code is
# offered, the threshold's unit, and the defaults for a new row (critical flag, threshold).
CODE_INFO: dict[str, dict] = {
    "top_strike": {"letter": "a", "label": "price ≥ top strike − X %",
                   "unit": "%", "critical": True, "default": 0},
    "bottom_strike": {"letter": "b", "label": "price ≤ bottom strike + Y %",
                      "unit": "%", "critical": True, "default": 0},
    "near_top_pct": {"letter": "c", "label": "price ≥ top strike − X %",
                     "unit": "%", "critical": False, "default": 2},
    "near_bottom_pct": {"letter": "d", "label": "price ≤ bottom strike + Y %",
                        "unit": "%", "critical": False, "default": 2},
    "last_expiry": {"letter": "e", "label": "time ≥ last expiry − N days",
                    "unit": "days", "critical": True, "default": 0},
    "last_expiry_days": {"letter": "f", "label": "time ≥ last expiry − N days",
                         "unit": "days", "critical": False, "default": 1},
}
THRESHOLD_MIN, THRESHOLD_MAX = 0, 100


@dataclass(frozen=True)
class Setting:
    asset: str
    code: str
    threshold: float = 0.0
    enabled: bool = True
    critical: bool = False


@dataclass(frozen=True)
class Price:
    value: float
    at: datetime
    source: str = "index"           # 'index' = live index ticker; 'candle' = last stored minute


@dataclass
class AlertBox:
    subaccount_id: int
    strategy_id: int
    underlying: str
    cex_code: str
    name: str                       # box (strategy) name
    account: str                    # account name, as on the Dashboard
    strike_lo: float
    strike_hi: float
    ends_at: datetime               # last expiry = right edge of the box
    legs: list[Leg] = field(default_factory=list)

    @property
    def key(self) -> tuple[int, int, str]:
        return (self.subaccount_id, self.strategy_id, self.underlying)


@dataclass(frozen=True)
class State:
    armed: bool = True
    fired_for_ends_at: datetime | None = None
    last_sent_at: datetime | None = None
    last_value: float | None = None
    last_message: str | None = None


StateKey = tuple[int, int, str, str]        # subaccount_id, strategy_id, underlying, code


@dataclass
class Alert:
    box: AlertBox
    setting: Setting
    message: str
    new_state: State                # stored once Telegram accepted the message

    @property
    def state_key(self) -> StateKey:
        return (*self.box.key, self.setting.code)


@dataclass
class Evaluation:
    alerts: list[Alert] = field(default_factory=list)
    updates: dict[StateKey, State] = field(default_factory=dict)   # no message: re-arm / skip


# --------------------------------------------------------------------------- #
# Legs and boxes
# --------------------------------------------------------------------------- #
def leg_open_at(leg: Leg, t: datetime) -> bool:
    """Was the leg open at time t (t ≤ now)? Opened by then, not expired, and not yet closed."""
    if leg.opened_at > t or (leg.expires_at is not None and t >= leg.expires_at):
        return False
    if leg.status == "open":
        return True
    if leg.status == "closed":
        return leg.closed_at is not None and t < leg.closed_at
    return leg.last_seen_at is not None and t < leg.last_seen_at        # stale: last seen


def open_legs(box: AlertBox, t: datetime) -> int:
    return sum(leg_open_at(lg, t) for lg in box.legs)


def is_alive(box: AlertBox, now: datetime) -> bool:
    """At least one leg open now (not closed, not expired)."""
    return open_legs(box, now) > 0


def price_level(code: str, threshold: float, box: AlertBox) -> float:
    """The price the code compares with: top strike − X % (a, c) or bottom strike + Y % (b, d)."""
    if code in TOP_CODES:
        return box.strike_hi * (1 - threshold / 100)
    if code in BOTTOM_CODES:
        return box.strike_lo * (1 + threshold / 100)
    raise ValueError(f"not a price code: {code}")


def price_hit(code: str, price: float, level: float) -> bool:
    return price >= level if code in TOP_CODES else price <= level


def time_due(box: AlertBox, threshold: float) -> datetime:
    """When a time code (e, f) is due: last expiry − N days."""
    return box.ends_at - timedelta(days=threshold)


def time_hit(box: AlertBox, threshold: float, now: datetime, grace: timedelta) -> bool:
    """Due and not too late — until max(last expiry, due + grace) — and the box alive now, or (after
    the expiry) right before its last expiry."""
    due = time_due(box, threshold)
    if not due <= now < max(box.ends_at, due + grace):
        return False
    return open_legs(box, min(now, box.ends_at - timedelta(seconds=1))) > 0


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
def evaluate(boxes: list[AlertBox], settings: dict[tuple[str, str], Setting],
             prices: dict[tuple[str, str], Price | None], states: dict[StateKey, State],
             now: datetime, rearm: timedelta, grace: timedelta) -> Evaluation:
    """Decide which notifications to send now and which states change without a message.

    settings: (asset, code) → Setting; prices: (cex_code, asset) → current price (None = unknown,
    price codes wait); states: stored core.notification_state rows.
    """
    ev = Evaluation()
    for box in boxes:
        own = {code: settings.get((box.underlying, code)) for code in (*PRICE_CODES, *TIME_CODES)}
        if not any(s is not None for s in own.values()):
            continue
        alive = is_alive(box, now)
        price = prices.get((box.cex_code, box.underlying))
        hits: dict[str, bool] = {}

        for code in PRICE_CODES:
            st = own[code]
            if st is None:
                continue
            key: StateKey = (*box.key, code)
            state = states.get(key, State())
            if not alive:                                   # box gone quiet → re-arm
                if not state.armed:
                    ev.updates[key] = replace(state, armed=True)
                continue
            if not st.enabled or price is None:
                continue
            level = price_level(code, st.threshold, box)
            hit = hits[code] = price_hit(code, price.value, level)
            if not hit:                                     # back on the other side → re-arm
                if not state.armed:
                    ev.updates[key] = replace(state, armed=True)
                continue
            if not state.armed:
                continue
            sup = SUPPRESSED_BY.get(code)
            if sup and hits.get(sup) and own[sup] is not None and own[sup].enabled:
                ev.updates[key] = replace(state, armed=False)   # a / b covers it
                continue
            if state.last_sent_at is not None and now - state.last_sent_at < rearm:
                continue                                    # re-armed too recently: wait
            msg = format_message(box, st, now, price=price, level=level)
            ev.alerts.append(Alert(box, st, msg, replace(
                state, armed=False, last_sent_at=now, last_value=price.value, last_message=msg)))

        for code in TIME_CODES:
            st = own[code]
            if st is None or not st.enabled:
                continue
            key = (*box.key, code)
            state = states.get(key, State())
            if state.fired_for_ends_at == box.ends_at:
                continue
            if not time_hit(box, st.threshold, now, grace):
                continue
            sup = own.get(SUPPRESSED_BY.get(code, ""))
            if sup is not None and sup.enabled and now >= time_due(box, sup.threshold):
                ev.updates[key] = replace(state, fired_for_ends_at=box.ends_at)   # e covers it
                continue
            msg = format_message(box, st, now)
            ev.alerts.append(Alert(box, st, msg, replace(
                state, fired_for_ends_at=box.ends_at, last_sent_at=now, last_value=None,
                last_message=msg)))
    return ev


# --------------------------------------------------------------------------- #
# Messages (Telegram HTML)
# --------------------------------------------------------------------------- #
def fmt_price(v: float) -> str:
    return f"{v:,.1f}"


def fmt_strike(v: float) -> str:
    return f"{int(v):,}" if float(v).is_integer() else f"{v:,.2f}".rstrip("0").rstrip(".")


def fmt_num(v: float) -> str:
    return f"{v:g}"


def fmt_utc(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def fmt_span(d: timedelta) -> str:
    """'2 d 3 h', '5 h 20 min', '12 min'."""
    mins = max(0, int(d.total_seconds() // 60))
    days, rem = divmod(mins, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days} d {hours} h" if hours else f"{days} d"
    if hours:
        return f"{hours} h {mins} min" if mins else f"{hours} h"
    return f"{mins} min"


def title(setting: Setting, expired: bool = False) -> str:
    """Header text; the threshold decides the wording (0 = "reached")."""
    a, x, t = setting.asset, fmt_num(setting.threshold), setting.threshold
    if setting.code in TOP_CODES:
        return f"{a} reached the top strike" if t == 0 else f"{a} within {x}% of the top strike"
    if setting.code in BOTTOM_CODES:
        return (f"{a} reached the bottom strike" if t == 0
                else f"{a} within {x}% of the bottom strike")
    if t == 0 or expired:
        return "Last expiry reached"
    return f"Last expiry within {x} day{'' if t == 1 else 's'}"


def esc(v: str) -> str:
    """Telegram HTML: only <, > and & need escaping in text."""
    return html.escape(v, quote=False)


def format_message(box: AlertBox, setting: Setting, now: datetime, price: Price | None = None,
                   level: float | None = None) -> str:
    """Critical: 🔴 + bold "CRITICAL · …" header; otherwise 🟡 + bold header (Telegram can't color
    text). Then the box, the detail line, the box summary, the time and a #code hashtag."""
    expired = now >= box.ends_at
    head = title(setting, expired)
    head = f"CRITICAL · {head}" if setting.critical else head
    mark = CRITICAL_MARK if setting.critical else NORMAL_MARK
    code, x, t = setting.code, fmt_num(setting.threshold), setting.threshold
    if price is not None and level is not None:
        px = fmt_price(price.value)
        src = "" if price.source == "index" else f" (last candle {fmt_utc(price.at)[11:]})"
        top = code in TOP_CODES
        side, op, k = ("top", "≥", box.strike_hi) if top else ("bottom", "≤", box.strike_lo)
        if t == 0:
            detail = f"Price {px} {op} {side} strike {fmt_strike(k)}"
        else:
            detail = (f"Price {px} {op} {fmt_strike(round(level, 2))} = {side} strike "
                      f"{fmt_strike(k)} {'−' if top else '+'} {x}%")
        detail += src
    elif expired:
        detail = f"Last expiry {fmt_utc(box.ends_at)} — the box's last options expired"
    else:
        detail = f"Last expiry {fmt_utc(box.ends_at)} — in {fmt_span(box.ends_at - now)}"
    n_open = open_legs(box, now)
    summary = (f"Box {fmt_strike(box.strike_lo)} – {fmt_strike(box.strike_hi)} · "
               f"{len(box.legs)} leg{'' if len(box.legs) == 1 else 's'}, {n_open} open · "
               f"last expiry {fmt_utc(box.ends_at)}")
    return "\n".join([
        f"{mark} <b>{esc(head)}</b>",
        f"<b>{esc(box.name)}</b> · {esc(box.account)} · {esc(box.underlying)}",
        esc(detail),
        esc(summary),
        f"{fmt_utc(now)} · #{code}",
    ])


def setup_message(now: datetime, n_settings: int) -> str:
    return (f"✅ <b>CEX Option Reporting — Telegram alerts are set up</b>\n"
            f"{n_settings} notification setting{'' if n_settings == 1 else 's'} on · "
            f"{fmt_utc(now)}\n"
            f"Critical alerts start with {CRITICAL_MARK} <b>CRITICAL</b>, the others with "
            f"{NORMAL_MARK}.")
