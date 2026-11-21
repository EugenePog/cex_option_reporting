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

Build 17 — the worker can't check (per exchange × asset of the alive boxes, not per box):

    g  no_data_warning   no fresh price   price age ≥ X min            X = threshold (default 10)
    h  no_data_error     no fresh price   price age ≥ X min, critical  X = threshold (default 30)

Price age = now − the time of the newest price the worker has for the asset: the live index ticker
(≈ 0), else the newest stored 1-minute candle (any age), else unknown (= no price at all, due at
once). Sent once per outage; when a fresh price is back (age below the threshold) the code re-arms
and, if a g / h message went out, one "✅ alerts can check ASSET again" message follows. h covers g:
when both are due, only h is sent. These codes are never suppressed by a box being switched off or
not alive — only an asset without alive boxes is not checked (nothing to miss there).

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
               "near_bottom_pct": "d", "last_expiry": "e", "last_expiry_days": "f",
               "no_data_warning": "g", "no_data_error": "h"}
PRICE_CODES = ("top_strike", "bottom_strike", "near_top_pct", "near_bottom_pct")
TIME_CODES = ("last_expiry", "last_expiry_days")
TOP_CODES = ("top_strike", "near_top_pct")          # level = top strike − X %, hit when price ≥
BOTTOM_CODES = ("bottom_strike", "near_bottom_pct")  # level = bottom strike + Y %, hit when price ≤
NO_DATA_CODES = ("no_data_warning", "no_data_error")    # g, h — per exchange × asset (build 17)
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
    # build 17: minutes without a fresh price (1 … 1440; 0 would fire at once and never re-arm)
    "no_data_warning": {"letter": "g", "label": "no fresh price for X min — alerts can't check",
                        "unit": "min", "critical": False, "default": 10, "min": 1, "max": 1440},
    "no_data_error": {"letter": "h", "label": "no fresh price for X min — alerts can't check",
                      "unit": "min", "critical": True, "default": 30, "min": 1, "max": 1440},
}
THRESHOLD_MIN, THRESHOLD_MAX = 0, 100


def threshold_range(code: str) -> tuple[float, float]:
    """Allowed threshold of a code: 0 … 100 (% or days), 1 … 1440 for the minute codes g / h."""
    info = CODE_INFO.get(code, {})
    return info.get("min", THRESHOLD_MIN), info.get("max", THRESHOLD_MAX)


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


# --------------------------------------------------------------------------- #
# No-data notifications (build 17): the worker can't check an asset
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Feed:
    """The price the worker has for one exchange × asset of alive boxes."""
    cex_code: str
    asset: str
    last_at: datetime | None        # time of the newest price (live ticker / stored candle end)
    source: str | None              # 'index' | 'candle' | None (no price at all)
    usable: bool                    # price alerts run on it (live, or candle ≤ max age)
    n_boxes: int = 0                # alive boxes of this exchange × asset

    def age(self, now: datetime) -> timedelta | None:
        return None if self.last_at is None else max(timedelta(0), now - self.last_at)


@dataclass(frozen=True)
class FeedState:
    armed: bool = True
    sent: bool = False              # a g / h message went out for the current outage
    last_sent_at: datetime | None = None
    last_value: float | None = None  # minutes without a price when sent
    last_message: str | None = None


FeedKey = tuple[str, str, str]      # cex_code, asset, code


@dataclass
class FeedMessage:
    feed: Feed
    code: str                       # g / h code, or the code that recovered
    message: str
    state_key: FeedKey | None       # state stored once Telegram accepted it (None: recovery)
    new_state: FeedState | None


@dataclass
class FeedEvaluation:
    messages: list[FeedMessage] = field(default_factory=list)
    updates: dict[FeedKey, FeedState] = field(default_factory=dict)   # stored without a message


def no_data_due(feed: Feed, setting: Setting, now: datetime) -> bool:
    """No fresh price for at least `threshold` minutes (no price at all counts as due)."""
    age = feed.age(now)
    return age is None or age >= timedelta(minutes=setting.threshold)


def evaluate_no_data(feeds: list[Feed], settings: dict[tuple[str, str], Setting],
                     states: dict[FeedKey, FeedState], now: datetime) -> FeedEvaluation:
    ev = FeedEvaluation()
    for feed in feeds:
        own = {c: settings.get((feed.asset, c)) for c in NO_DATA_CODES}
        on = {c: st for c, st in own.items() if st is not None and st.enabled}
        if not on:
            continue
        due = {c: no_data_due(feed, st, now) for c, st in on.items()}
        recovered: list[str] = []
        for code, st in on.items():
            key: FeedKey = (feed.cex_code, feed.asset, code)
            state = states.get(key, FeedState())
            if not due[code]:                                   # fresh price back → re-arm
                if not state.armed or state.sent:
                    if state.sent:
                        recovered.append(code)
                    ev.updates[key] = replace(state, armed=True, sent=False)
                continue
            if not state.armed:
                continue
            if code == "no_data_warning" and due.get("no_data_error") and \
                    states.get((feed.cex_code, feed.asset, "no_data_error"), FeedState()).armed:
                ev.updates[key] = replace(state, armed=False)   # h is sent now and covers g
                continue
            msg = format_no_data(feed, st, now)
            age = feed.age(now)
            ev.messages.append(FeedMessage(feed, code, msg, key, replace(
                state, armed=False, sent=True, last_sent_at=now,
                last_value=None if age is None else round(age.total_seconds() / 60, 1),
                last_message=msg)))
        if recovered:
            last = max(recovered, key=lambda c: NO_DATA_CODES.index(c))   # h over g
            ev.messages.append(FeedMessage(feed, last, format_feed_back(feed, now, states.get(
                (feed.cex_code, feed.asset, last))), None, None))
    return ev


def format_no_data(feed: Feed, setting: Setting, now: datetime) -> str:
    """⚠ alerts can't check ASSET: no price for X min — with what the worker has and does."""
    age = feed.age(now)
    span = "no price at all" if age is None else f"no price for {fmt_span(age)}"
    head = f"⚠ alerts can't check {feed.asset}: {span}"
    head = f"CRITICAL · {head}" if setting.critical else head
    mark = CRITICAL_MARK if setting.critical else NORMAL_MARK
    if feed.last_at is not None and feed.source == "index":
        have = f"Last live {feed.cex_code} index price {fmt_utc(feed.last_at)}"
    elif feed.last_at is not None:
        have = (f"Live {feed.cex_code} index price failing · newest stored candle "
                f"{fmt_utc(feed.last_at)}")
    else:
        have = f"Live {feed.cex_code} index price failing · no stored candle"
    boxes = f"{feed.n_boxes} alive box{'' if feed.n_boxes == 1 else 'es'}"
    doing = (f"Price alerts of {boxes} run on that old price" if feed.usable
             else f"Price alerts of {boxes} are NOT checked until a price is back")
    return "\n".join([
        f"{mark} <b>{esc(head)}</b>",
        esc(have),
        esc(doing),
        f"Threshold {fmt_num(setting.threshold)} min · {fmt_utc(now)} · #{setting.code}",
    ])


def format_feed_back(feed: Feed, now: datetime, state: FeedState | None) -> str:
    since = f" (alert sent {fmt_utc(state.last_sent_at)})" if state and state.last_sent_at else ""
    src = "live index" if feed.source == "index" else "stored candle"
    at = f", {fmt_utc(feed.last_at)}" if feed.last_at is not None else ""
    return "\n".join([
        f"✅ <b>{esc(f'alerts can check {feed.asset} again')}</b>",
        esc(f"{feed.cex_code} price back ({src}{at}){since}"),
        f"{fmt_utc(now)} · #no_data_ok",
    ])
