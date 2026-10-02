"""Admin mode: role-gated, cross-tenant views (all clients, all strategies, comparison).

`box_builder` — the admin "Box builder" tab API: move position legs between strategies via
manual pins in core.strategy_link (precedence over strategy_rule).

Every route here depends on `app.web.deps.require_admin`. Reads the cross-client gold tables
(`gold.client_performance`, `gold.client_pnl_daily`, `gold.strategy_performance`) — see ARCHITECTURE.md §5.5.
"""
