"""Admin JSON API (cross-tenant). All endpoints are admin-gated.

Stub signatures showing the intended contract; queries hit the `gold.client_*` /
`gold.strategy_performance` tables WITHOUT a per-user filter (admin sees all clients).
"""
from __future__ import annotations

# from fastapi import APIRouter, Depends
# from app.web.deps import CurrentUser, require_admin
# router = APIRouter(prefix="/admin/api", tags=["admin"])


def client_leaderboard(period: str = "mtd") -> list[dict]:
    """All clients ranked by performance for `period` ('mtd'|'ytd'|'all').

    SELECT user_id, net_pnl, return_pct, max_drawdown_pct, sharpe, win_rate, n_deals
    FROM gold.client_performance WHERE period = :period ORDER BY net_pnl DESC;
    """
    raise NotImplementedError


def compare_clients(user_ids: list[int]) -> dict:
    """Overlaid equity/PnL curves for the given clients (from gold.client_pnl_daily)."""
    raise NotImplementedError


def compare_strategies(strategy_name: str) -> list[dict]:
    """How one strategy performs across all clients (from gold.strategy_performance)."""
    raise NotImplementedError
