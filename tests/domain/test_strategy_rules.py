"""Strategy-rule matching + priority resolution."""
from __future__ import annotations

from datetime import datetime, timezone

from app.domain.strategy_rules import Rule, TagRecord, assign_strategy, match_rule


def _rec(**kw):
    base = dict(inst_id="BTC-USD-260618-65500-C", underlying="BTC-USD", opt_type="C", side="short")
    base.update(kw)
    return TagRecord(**base)


def test_inst_pattern_glob():
    assert match_rule({"inst_pattern": "BTC-USD-*"}, _rec())
    assert not match_rule({"inst_pattern": "ETH-USD-*"}, _rec())


def test_anded_conditions():
    m = {"opt_type": "C", "side": "short", "underlying": "BTC-USD"}
    assert match_rule(m, _rec())
    assert not match_rule(m, _rec(opt_type="P"))       # one condition fails → no match


def test_opened_window():
    rec = _rec(opened_at=datetime(2026, 6, 15, tzinfo=timezone.utc))
    assert match_rule({"opened_after": "2026-06-01", "opened_before": "2026-07-01"}, rec)
    assert not match_rule({"opened_after": "2026-07-01"}, rec)
    # missing opened_at can't satisfy a window rule
    assert not match_rule({"opened_after": "2026-06-01"}, _rec(opened_at=None))


def test_unknown_key_is_conservative_nonmatch():
    assert not match_rule({"nonsense": 1}, _rec())


def test_priority_and_unassigned_fallback():
    rules = [
        Rule(strategy_id=10, priority=50, match_json={"inst_pattern": "BTC-USD-*"}),
        Rule(strategy_id=20, priority=100, match_json={"opt_type": "C"}),  # higher priority wins
    ]
    assert assign_strategy(rules, _rec(), unassigned_id=99) == 20
    # nothing matches → unassigned
    assert assign_strategy(rules, _rec(inst_id="ETH-USD-1-1-P", underlying="ETH-USD", opt_type="P"),
                           unassigned_id=99) == 99
