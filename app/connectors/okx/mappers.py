"""Map raw OKX API dicts -> normalized connector rows.

Pure functions (no I/O) so they're trivially unit-testable against recorded OKX JSON fixtures.
Field extraction mirrors the reference sample code (check_balance / check_positions / opt-summary).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.connectors.base import (
    BalanceRow,
    BillRow,
    ClosedPositionRow,
    FillRow,
    MarginInfo,
    OptionSummaryRow,
    PositionRow,
)


def _ts_to_dt(ms: Any) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def _f(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def uly_from_inst_id(inst_id: str) -> str:
    """'BTC-USD-260319-70500-C' -> 'BTC-USD'."""
    parts = inst_id.split("-")
    return f"{parts[0]}-{parts[1]}"


def map_balances(resp: dict[str, Any], captured_at: datetime) -> list[BalanceRow]:
    rows: list[BalanceRow] = []
    for asset in resp["data"][0].get("details", []):
        total = _f(asset.get("eq"))
        if total <= 0:
            continue
        rows.append(
            BalanceRow(
                ccy=asset["ccy"],
                total=round(total, 6),
                available=round(_f(asset.get("availEq") or asset.get("availBal")), 6),
                usd_value=round(_f(asset.get("eqUsd")), 2),
                captured_at=captured_at,
                raw=asset,
            )
        )
    return rows


def map_positions(resp: dict[str, Any], captured_at: datetime) -> list[PositionRow]:
    rows: list[PositionRow] = []
    for pos in resp.get("data", []):
        rows.append(
            PositionRow(
                inst_id=pos.get("instId", ""),
                side=pos.get("posSide", ""),
                size=_f(pos.get("pos")),
                avg_px=_f(pos.get("avgPx")),
                upl=_f(pos.get("upl")),
                fee=_f(pos.get("fee")) if pos.get("fee") else None,
                mark_px=_f(pos.get("markPx")) if pos.get("markPx") else None,
                captured_at=captured_at,
                raw=pos,
            )
        )
    return rows


def map_opt_summary(resp: dict[str, Any], inst_id: str, captured_at: datetime) -> OptionSummaryRow | None:
    data = next((d for d in resp.get("data", []) if d.get("instId") == inst_id), None)
    if not data:
        return None
    return OptionSummaryRow(
        inst_id=inst_id,
        iv=_f(data.get("markVol")),
        mark_px=_f(data.get("markPx")),
        bid_px=_f(data.get("bidPx")),
        ask_px=_f(data.get("askPx")),
        delta=_f(data.get("delta")),
        gamma=_f(data.get("gamma")),
        theta=_f(data.get("theta")),
        vega=_f(data.get("vega")),
        captured_at=captured_at,
        raw=data,
    )


def map_margin(resp: dict[str, Any], captured_at: datetime) -> list[MarginInfo]:
    """Account-level margin when available, else per-currency (mirrors reference logic)."""
    data = resp["data"][0]
    details = data.get("details", [])
    total_eq_usd = sum(_f(d.get("eqUsd")) for d in details)
    rows: list[MarginInfo] = []

    if data.get("imr") and data.get("mmr") and data.get("mgnRatio"):
        rows.append(
            MarginInfo(
                scope="ACCOUNT",
                eq_usd=round(total_eq_usd, 2),
                imr_usd=round(_f(data.get("imr")), 2),
                mmr_usd=round(_f(data.get("mmr")), 2),
                margin_ratio=round(_f(data.get("mgnRatio")), 4),
                captured_at=captured_at,
                raw=data,
            )
        )
        return rows

    for d in details:
        imr = _f(d.get("imr"))
        mmr = _f(d.get("mmr"))
        if imr == 0 and mmr == 0:
            continue
        eq = _f(d.get("eq"))
        eq_usd = _f(d.get("eqUsd"))
        price = (eq_usd / eq) if eq > 0 else 0.0
        mmr_usd = mmr * price
        ratio = _f(d.get("mgnRatio")) if d.get("mgnRatio") else (
            (eq_usd / mmr_usd) if mmr_usd > 0 else float("inf")
        )
        rows.append(
            MarginInfo(
                scope=d.get("ccy", ""),
                eq_usd=round(eq_usd, 2),
                imr_usd=round(imr * price, 2),
                mmr_usd=round(mmr_usd, 2),
                margin_ratio=round(ratio, 4),
                captured_at=captured_at,
                raw=d,
            )
        )
    return rows


def map_bills(resp: dict[str, Any]) -> list[BillRow]:
    """Map OKX bills-archive rows. `pnl` on settlement/delivery bills carries realized PnL."""
    rows: list[BillRow] = []
    now = datetime.now(timezone.utc)
    for d in resp.get("data", []):
        rows.append(
            BillRow(
                bill_id=str(d.get("billId") or ""),
                inst_id=d.get("instId", ""),
                bill_type=d.get("type", ""),
                sub_type=d.get("subType", ""),
                pnl=_f(d.get("pnl")),
                ccy=d.get("ccy", ""),
                billed_at=_ts_to_dt(d.get("ts")) or now,
                captured_at=_ts_to_dt(d.get("ts")) or now,
                raw=d,
            )
        )
    return rows


def map_closed_positions(resp: dict[str, Any]) -> list[ClosedPositionRow]:
    """Map OKX positions-history rows. `cTime` = opened, `uTime` = closed; `realizedPnl` = PnL.

    Includes options closed by expiry/delivery (OKX `type` = '3' delivery / '6' etc.).
    """
    rows: list[ClosedPositionRow] = []
    now = datetime.now(timezone.utc)
    for d in resp.get("data", []):
        closed_at = _ts_to_dt(d.get("uTime")) or now
        rows.append(
            ClosedPositionRow(
                ext_id=str(d.get("posId") or ""),
                inst_id=d.get("instId", ""),
                realized_pnl=_f(d.get("realizedPnl")),
                pnl=_f(d.get("pnl")),
                close_type=d.get("type", ""),
                open_avg_px=_f(d.get("openAvgPx")),
                close_avg_px=_f(d.get("closeAvgPx")),
                opened_at=_ts_to_dt(d.get("cTime")),
                closed_at=closed_at,
                captured_at=closed_at,
                raw=d,
            )
        )
    return rows


def map_fills(resp: dict[str, Any], captured_at: datetime | None = None) -> list[FillRow]:
    rows: list[FillRow] = []
    for f in resp.get("data", []):
        ts = f.get("ts") or f.get("fillTime")
        filled_at = (
            datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
            if ts
            else (captured_at or datetime.now(timezone.utc))
        )
        rows.append(
            FillRow(
                trade_id=str(f.get("tradeId") or f.get("billId") or ""),
                inst_id=f.get("instId", ""),
                side=f.get("side", ""),
                size=_f(f.get("fillSz")),
                price=_f(f.get("fillPx")),
                fee=_f(f.get("fee")),
                fee_ccy=f.get("feeCcy", ""),
                filled_at=filled_at,
                raw=f,
            )
        )
    return rows
