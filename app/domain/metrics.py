"""Pure performance-metric helpers for the gold layer. No I/O — unit-tested.

Two families:
  * deal_metrics  — trade-outcome stats from a list of realized PnL per closed deal.
  * equity_metrics — account-level return/drawdown/Sharpe from an equity time-series.

Currency note: deal PnL is in the account settlement currency (coin, e.g. BTC) as OKX returns it;
the equity series is USD (`gold.balance_timeseries`). Keep them conceptually separate — net_pnl is
trading PnL in coin, return_pct/drawdown/sharpe describe the USD equity curve.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class DealMetrics:
    net_pnl: float
    n_deals: int
    win_rate: float | None          # fraction 0..1
    avg_win: float | None
    avg_loss: float | None          # negative
    avg_pnl_per_deal: float | None
    profit_factor: float | None     # sum(wins) / |sum(losses)|; None if no losses


def deal_metrics(realized: list[float]) -> DealMetrics:
    n = len(realized)
    if n == 0:
        return DealMetrics(0.0, 0, None, None, None, None, None)
    wins = [x for x in realized if x > 0]
    losses = [x for x in realized if x < 0]
    net = sum(realized)
    loss_sum = sum(losses)
    return DealMetrics(
        net_pnl=net,
        n_deals=n,
        win_rate=len(wins) / n,
        avg_win=(sum(wins) / len(wins)) if wins else None,
        avg_loss=(loss_sum / len(losses)) if losses else None,
        avg_pnl_per_deal=net / n,
        profit_factor=(sum(wins) / abs(loss_sum)) if loss_sum < 0 else None,
    )


@dataclass
class EquityMetrics:
    return_pct: float | None        # percent over the period, e.g. 12.3
    max_drawdown_pct: float | None  # percent, e.g. 8.5 (positive number)
    sharpe: float | None            # annualized (365d)


def max_drawdown_pct(equity: list[float]) -> float | None:
    """Largest peak-to-trough decline of the equity curve, as a positive percent."""
    if len(equity) < 2:
        return None
    peak = equity[0]
    max_dd = 0.0
    for eq in equity:
        if eq > peak:
            peak = eq
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    return round(max_dd * 100, 4)


def sharpe(equity: list[float], periods_per_year: int = 365) -> float | None:
    """Annualized Sharpe from period-over-period equity returns (risk-free = 0)."""
    if len(equity) < 3:
        return None
    rets = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity)) if equity[i - 1]]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return round((mean / sd) * math.sqrt(periods_per_year), 4)


def equity_metrics(equity: list[float]) -> EquityMetrics:
    """`equity` is an ordered (by time) list of USD equity values over the period."""
    if len(equity) < 2 or not equity[0]:
        return EquityMetrics(None, max_drawdown_pct(equity), sharpe(equity))
    ret = round((equity[-1] / equity[0] - 1) * 100, 4)
    return EquityMetrics(ret, max_drawdown_pct(equity), sharpe(equity))
