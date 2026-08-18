"""Gold performance-metric helpers."""
from __future__ import annotations

from app.domain.metrics import deal_metrics, equity_metrics, max_drawdown_pct


def test_deal_metrics_basic():
    m = deal_metrics([10.0, -4.0, 6.0, -2.0])
    assert m.n_deals == 4
    assert m.net_pnl == 10.0
    assert m.win_rate == 0.5
    assert m.avg_win == 8.0            # (10+6)/2
    assert m.avg_loss == -3.0          # (-4-2)/2
    assert m.avg_pnl_per_deal == 2.5
    assert m.profit_factor == 16.0 / 6.0


def test_deal_metrics_empty_and_all_wins():
    e = deal_metrics([])
    assert e.n_deals == 0 and e.net_pnl == 0.0 and e.win_rate is None
    w = deal_metrics([1.0, 2.0])
    assert w.win_rate == 1.0 and w.profit_factor is None  # no losses → undefined


def test_max_drawdown():
    # peak 100 -> trough 80 = 20% dd
    assert max_drawdown_pct([100, 120, 96, 110]) == 20.0
    assert max_drawdown_pct([100]) is None


def test_equity_metrics_return_and_dd():
    em = equity_metrics([100.0, 110.0, 88.0, 99.0])
    assert em.return_pct == -1.0                 # 99/100 - 1 = -1%
    assert em.max_drawdown_pct == 20.0           # 110 -> 88
    assert em.sharpe is not None
