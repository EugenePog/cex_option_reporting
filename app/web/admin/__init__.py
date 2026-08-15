"""Admin mode: role-gated, cross-tenant views (all clients, all strategies, comparison).

Every route here depends on `app.web.deps.require_admin`. Reads the cross-client gold tables
(`gold.client_performance`, `gold.client_pnl_daily`, `gold.strategy_performance`) — see ARCHITECTURE.md §5.5.
"""
