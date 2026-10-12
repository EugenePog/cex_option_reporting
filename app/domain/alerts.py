"""Telegram alerts for the boxes of graph ① "Price with strategy boxes". Pure, no I/O.

Notification codes — core.notification_setting, one row per asset × code (the same settings for
every account and box of that asset):

    a  top_strike        price ≥ the box's top strike
    b  bottom_strike     price ≤ the box's bottom strike
    c  near_top_pct      price ≥ top strike − X %             X = threshold
    d  near_bottom_pct   price ≤ bottom strike + Y %          Y = threshold
    e  last_expiry       time reaches the box's last expiry
    f  last_expiry_days  time reaches the last expiry − N days     N = threshold

The box is the one graph ① draws (gold.box_shape: all legs of one strategy on one underlying): top /
bottom = its highest / lowest strike, last expiry = its right edge. Only ALIVE boxes are watched —
at least one leg still open: status open and not past its expiry (an option can't be open after it
expired, even before the close is collected). The `unassigned` legs are not a box and are skipped.

Each notification goes out once per crossing:
  * price codes (a–d) are DISARMED when sent and RE-ARMED once the price is back on the other side;
    the same notification then waits at least `rearm` after the previous one (no ping-pong around
    a strike). When a box stops being alive, its price codes are re-armed.
  * time codes (e, f) are sent once per last-expiry value — a box that rolls on to a later expiry
    gets them again. e is sent from the expiry moment for `grace`, only for a box that was still
    alive right before its last expiry (not when every leg was closed early); f while the box is
    alive within the N days before its last expiry.
  * When a and c are both true (price at or above the top), only a is sent and c is marked done;
    the same for b over d — unless a / b is switched off.
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
SUPPRESSED_BY = {"near_top_pct": "top_strike", "near_bottom_pct": "bottom_strike"}

CRITICAL_MARK, NORMAL_MARK = "🔴", "🟡"


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
    """The price the code compares with."""
    if code == "top_strike":
        return box.strike_hi
    if code == "bottom_strike":
        return box.strike_lo
    if code == "near_top_pct":
        return box.strike_hi * (1 - threshold / 100)
    if code == "near_bottom_pct":
        return box.strike_lo * (1 + threshold / 100)
    raise ValueError(f"not a price code: {code}")


def price_hit(code: str, price: float, level: float) -> bool:
    return price >= level if code in ("top_strike", "near_top_pct") else price <= level


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
            if code == "last_expiry":
                hit = (box.ends_at <= now < box.ends_at + grace
                       and open_legs(box, box.ends_at - timedelta(seconds=1)) > 0)
            else:
                hit = alive and box.ends_at - timedelta(days=st.threshold) <= now < box.ends_at
            if not hit:
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


def title(setting: Setting) -> str:
    a, x = setting.asset, fmt_num(setting.threshold)
    return {
        "top_strike": f"{a} reached the top strike",
        "bottom_strike": f"{a} reached the bottom strike",
        "near_top_pct": f"{a} within {x}% of the top strike",
        "near_bottom_pct": f"{a} within {x}% of the bottom strike",
        "last_expiry": "Last expiry reached",
        "last_expiry_days": f"Last expiry within {x} day{'' if setting.threshold == 1 else 's'}",
    }[setting.code]


def esc(v: str) -> str:
    """Telegram HTML: only <, > and & need escaping in text."""
    return html.escape(v, quote=False)


def format_message(box: AlertBox, setting: Setting, now: datetime, price: Price | None = None,
                   level: float | None = None) -> str:
    """Critical: 🔴 + bold "CRITICAL · …" header; otherwise 🟡 + bold header (Telegram can't color
    text). Then the box, the detail line, the box summary, the time and a #code hashtag."""
    head = f"CRITICAL · {title(setting)}" if setting.critical else title(setting)
    mark = CRITICAL_MARK if setting.critical else NORMAL_MARK
    code, x = setting.code, fmt_num(setting.threshold)
    if price is not None and level is not None:
        px = fmt_price(price.value)
        src = "" if price.source == "index" else f" (last candle {fmt_utc(price.at)[11:]})"
        detail = {
            "top_strike": f"Price {px} ≥ top strike {fmt_strike(level)}",
            "bottom_strike": f"Price {px} ≤ bottom strike {fmt_strike(level)}",
            "near_top_pct": f"Price {px} ≥ {fmt_strike(round(level, 2))} = top strike "
                            f"{fmt_strike(box.strike_hi)} − {x}%",
            "near_bottom_pct": f"Price {px} ≤ {fmt_strike(round(level, 2))} = bottom strike "
                               f"{fmt_strike(box.strike_lo)} + {x}%",
        }[code] + src
    elif code == "last_expiry":
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
