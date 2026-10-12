"""Alerts worker — Telegram notifications for the boxes of graph ① "Price with strategy boxes".

pm2 process `worker` (`python -m app.cli worker --loop`). One check (`run_once`):

  1. read core.notification_setting — what to watch per asset (nothing on → nothing to do);
  2. read the boxes of those assets from gold.box_shape (not `unassigned`, strategy not deleted;
     still open, or past their last expiry by less than ALERTS_EXPIRY_GRACE_HOURS) and their legs
     from gold.position_leg;
  3. get the current index price per exchange × asset: the OKX public index ticker; if that fails,
     the newest stored 1-minute candle (silver.index_candle) when it is at most
     ALERTS_MAX_PRICE_AGE_MINUTES old; otherwise the price alerts wait for the next check;
  4. decide (app.domain.alerts), send (app.worker.notifier) and store core.notification_state for
     the messages Telegram accepted, plus the re-arm / skip updates. A message that failed is not
     stored, so it is tried again on the next check.

`dry_run` prints the messages instead and changes nothing. The loop runs every
ALERTS_INTERVAL_SECONDS (default 60 s); an advisory try-lock keeps two workers from double-sending.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.base import session_scope
from app.db.models_core import NotificationSetting, NotificationState, Strategy, Subaccount
from app.db.models_gold import BoxShape
from app.db.models_gold import PositionLeg as GoldPositionLeg
from app.db.models_silver import IndexCandle as SilverIndexCandle
from app.domain.alerts import (
    AlertBox,
    Evaluation,
    Price,
    Setting,
    State,
    StateKey,
    evaluate,
    is_alive,
    setup_message,
)
from app.domain.boxes import Leg
from app.web.accounts import account_labels
from app.worker import notifier
from config.settings import get_settings

logger = logging.getLogger(__name__)

ALERTS_LOCK_KEY = 0x0CE0_A1E7          # pg advisory lock: one alerts check at a time

PriceFetcher = Callable[[str, str], tuple[float, datetime] | None]   # (cex_code, asset)


@dataclass
class RunResult:
    boxes: int = 0
    alive: int = 0
    sent: list[str] = field(default_factory=list)       # messages accepted by Telegram
    failed: list[str] = field(default_factory=list)     # messages Telegram refused / unreachable
    dry_run: list[str] = field(default_factory=list)    # messages that would be sent
    skipped: str | None = None                          # why nothing was checked


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def _num(v) -> float | None:
    return float(v) if v is not None else None


def load_settings(s) -> dict[tuple[str, str], Setting]:
    return {(r.asset, r.code): Setting(asset=r.asset, code=r.code,
                                       threshold=float(r.threshold or 0),
                                       enabled=bool(r.is_enabled), critical=bool(r.is_critical))
            for r in s.execute(select(NotificationSetting)).scalars()}


def load_boxes(s, assets: set[str], now: datetime, grace: timedelta) -> list[AlertBox]:
    rows = s.execute(
        select(BoxShape, Strategy.name, Subaccount.cex_code)
        .join(Strategy, Strategy.id == BoxShape.strategy_id)
        .join(Subaccount, Subaccount.id == BoxShape.subaccount_id)
        .where(BoxShape.underlying.in_(assets), Strategy.deleted_at.is_(None),
               Strategy.name != "unassigned",
               or_(BoxShape.status == "open", BoxShape.ends_at >= now - grace))
        .order_by(BoxShape.subaccount_id, BoxShape.strategy_id)
    ).all()
    if not rows:
        return []
    names = account_labels(s, {b.subaccount_id for b, _n, _c in rows})
    legs: dict[tuple[int, int, str], list[Leg]] = defaultdict(list)
    for lg in s.execute(select(GoldPositionLeg).where(
            GoldPositionLeg.strategy_id.in_({b.strategy_id for b, _n, _c in rows}),
            GoldPositionLeg.underlying.in_(assets), GoldPositionLeg.strike.isnot(None))
    ).scalars():
        legs[(lg.subaccount_id, lg.strategy_id, lg.underlying)].append(Leg(
            leg_id=lg.position_leg_id, subaccount_id=lg.subaccount_id,
            strategy_id=lg.strategy_id, underlying=lg.underlying, strike=_num(lg.strike),
            opened_at=lg.pos_opened_at, expires_at=lg.expires_at, status=lg.status,
            closed_at=lg.closed_at, last_seen_at=lg.last_seen_at, size=_num(lg.size),
            size_coin=_num(lg.size_coin), coin=lg.coin, close_type=lg.close_type))
    return [AlertBox(subaccount_id=b.subaccount_id, strategy_id=b.strategy_id,
                     underlying=b.underlying, cex_code=cex, name=name,
                     account=names.get(b.subaccount_id, str(b.subaccount_id)),
                     strike_lo=float(b.strike_lo), strike_hi=float(b.strike_hi),
                     ends_at=b.ends_at, legs=legs[(b.subaccount_id, b.strategy_id, b.underlying)])
            for b, name, cex in rows]


def load_states(s, boxes: list[AlertBox]) -> dict[StateKey, State]:
    if not boxes:
        return {}
    keys = {b.key for b in boxes}
    out: dict[StateKey, State] = {}
    for r in s.execute(select(NotificationState).where(
            NotificationState.strategy_id.in_({k[1] for k in keys}))).scalars():
        if (r.subaccount_id, r.strategy_id, r.underlying) in keys:
            out[(r.subaccount_id, r.strategy_id, r.underlying, r.code)] = State(
                armed=bool(r.armed), fired_for_ends_at=r.fired_for_ends_at,
                last_sent_at=r.last_sent_at, last_value=_num(r.last_value),
                last_message=r.last_message)
    return out


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #
def okx_index_price(cex_code: str, asset: str) -> tuple[float, datetime] | None:
    """Live index price from the exchange's public API (OKX only for now)."""
    if cex_code != "OKX":
        return None
    from app.connectors.okx.market import OkxMarketClient

    return OkxMarketClient().index_ticker(asset)


def stored_candle_price(s, cex_code: str, asset: str,
                        now: datetime) -> tuple[float, datetime] | None:
    """Close of the newest 1-minute candle that ended by `now` → (price, end of that minute)."""
    row = s.execute(select(SilverIndexCandle.close, SilverIndexCandle.ts).where(
        SilverIndexCandle.cex_code == cex_code, SilverIndexCandle.inst_id == asset,
        SilverIndexCandle.bar == "1m", SilverIndexCandle.ts <= now - timedelta(minutes=1))
        .order_by(SilverIndexCandle.ts.desc()).limit(1)).first()
    return (float(row[0]), row[1] + timedelta(minutes=1)) if row else None


def current_prices(s, pairs: set[tuple[str, str]], now: datetime, max_age: timedelta,
                   fetch: PriceFetcher | None) -> dict[tuple[str, str], Price | None]:
    out: dict[tuple[str, str], Price | None] = {}
    for cex, asset in sorted(pairs):
        live = None
        if fetch is not None:
            try:
                live = fetch(cex, asset)
            except Exception as e:  # noqa: BLE001 - fall back to the stored candles
                logger.warning("alerts: live %s %s index price failed: %s", cex, asset, e)
        if live is not None:
            out[(cex, asset)] = Price(live[0], live[1], "index")
            continue
        stored = stored_candle_price(s, cex, asset, now)
        if stored is not None and now - stored[1] <= max_age:
            out[(cex, asset)] = Price(stored[0], stored[1], "candle")
        else:
            out[(cex, asset)] = None
            logger.warning("alerts: no current %s %s price (live failed, newest stored candle %s) "
                           "— price alerts wait", cex, asset,
                           stored[1].astimezone(UTC).isoformat() if stored else "none")
    return out


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def save_states(s, states: dict[StateKey, State], now: datetime) -> None:
    for (sub, strat, uly, code), st in states.items():
        values = dict(subaccount_id=sub, strategy_id=strat, underlying=uly, code=code,
                      armed=st.armed, fired_for_ends_at=st.fired_for_ends_at,
                      last_sent_at=st.last_sent_at, last_value=st.last_value,
                      last_message=st.last_message, updated_at=now)
        stmt = pg_insert(NotificationState).values(**values)
        s.execute(stmt.on_conflict_do_update(
            index_elements=["subaccount_id", "strategy_id", "underlying", "code"],
            set_={k: stmt.excluded[k] for k in values
                  if k not in ("subaccount_id", "strategy_id", "underlying", "code")}))


@contextmanager
def alerts_lock() -> Iterator[bool]:
    """Try (don't wait) to take the alerts lock; yields True when this process got it."""
    from app.db.base import engine

    with engine.connect() as conn:
        got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                {"k": ALERTS_LOCK_KEY}).scalar())
        try:
            yield got
        finally:
            if got:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ALERTS_LOCK_KEY})
            conn.commit()


# --------------------------------------------------------------------------- #
# One check
# --------------------------------------------------------------------------- #
_warned_unconfigured = False


def run_once(now: datetime | None = None, dry_run: bool = False,
             fetch: PriceFetcher | None = okx_index_price,
             send_many: Callable[[list[str]], list[bool]] | None = None) -> RunResult:
    """One alerts check. `now`, `fetch` and `send_many` are injectable for tests."""
    global _warned_unconfigured
    cfg = get_settings()
    now = now or datetime.now(UTC)
    send_many = send_many or notifier.send_many
    res = RunResult()

    if not dry_run and send_many is notifier.send_many and not notifier.is_configured():
        if not _warned_unconfigured:
            logger.warning("alerts: Telegram is not configured (TELEGRAM_BOT_TOKEN / "
                           "TELEGRAM_CHAT_ID in .env) — alerts are not checked")
            _warned_unconfigured = True
        res.skipped = "telegram not configured"
        return res

    with alerts_lock() as got:
        if not got:
            res.skipped = "another alerts check is running"
            logger.info("alerts: %s — skipped", res.skipped)
            return res

        grace = timedelta(hours=cfg.alerts_expiry_grace_hours)
        with session_scope() as s:
            settings = load_settings(s)
            assets = {a for (a, _c), st in settings.items() if st.enabled}
            if not assets:
                res.skipped = "no notification is switched on"
                return res
            boxes = load_boxes(s, assets, now, grace)
            states = load_states(s, boxes)
            alive = [b for b in boxes if is_alive(b, now)]
            res.boxes, res.alive = len(boxes), len(alive)
            prices = current_prices(s, {(b.cex_code, b.underlying) for b in alive}, now,
                                    timedelta(minutes=cfg.alerts_max_price_age_minutes), fetch)
            ev: Evaluation = evaluate(boxes, settings, prices, states, now,
                                      rearm=timedelta(minutes=cfg.alerts_rearm_minutes),
                                      grace=grace)

        if dry_run:
            res.dry_run = [a.message for a in ev.alerts]
            return res

        ok = send_many([a.message for a in ev.alerts]) if ev.alerts else []
        to_store = dict(ev.updates)
        for alert, accepted in zip(ev.alerts, ok, strict=True):
            if accepted:
                to_store[alert.state_key] = alert.new_state
                res.sent.append(alert.message)
            else:
                res.failed.append(alert.message)
        if to_store:
            with session_scope() as s:
                save_states(s, to_store, now)
    if res.sent or res.failed:
        logger.info("alerts: %d sent, %d failed (%d alive boxes of %d)", len(res.sent),
                    len(res.failed), res.alive, res.boxes)
    return res


def send_setup_message() -> bool:
    """`worker --test`: one message to the configured chat, to check the bot setup."""
    with session_scope() as s:
        n_on = sum(st.enabled for st in load_settings(s).values())
    return notifier.send(setup_message(datetime.now(UTC), n_on))


def run_loop() -> None:
    """Check every ALERTS_INTERVAL_SECONDS, keeping the process alive (pm2 `worker`)."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    interval = get_settings().alerts_interval_seconds

    def tick() -> None:
        try:
            run_once()
        except Exception:  # noqa: BLE001 - keep the loop alive; the next check retries
            logger.exception("alerts check failed")

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(tick, "interval", seconds=interval, id="alerts", max_instances=1,
                      coalesce=True)
    logger.info("alerts worker started — checking every %d seconds", interval)
    tick()  # run immediately, then on the interval
    scheduler.start()
