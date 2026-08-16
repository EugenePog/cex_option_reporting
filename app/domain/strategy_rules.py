"""Strategy tagging: match a position/fill/closed-position against core.strategy_rule rows.

match_json vocabulary (keys AND-ed together):
    inst_pattern   glob on inst_id           e.g. "BTC-USD-*"
    opt_type       "C" | "P"
    side           "long" | "short"
    underlying     exact, e.g. "BTC-USD"
    opened_after   UTC date "YYYY-MM-DD"  (inclusive)
    opened_before  UTC date "YYYY-MM-DD"  (exclusive)

Rules for a subaccount are evaluated highest-`priority` first; the first match wins. If nothing
matches, the caller falls back to the subaccount's "unassigned" strategy.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from fnmatch import fnmatchcase
from typing import Any


@dataclass
class Rule:
    strategy_id: int
    priority: int
    match_json: dict[str, Any]


@dataclass
class TagRecord:
    inst_id: str
    underlying: str | None = None
    opt_type: str | None = None
    side: str | None = None                 # normalized to "long"/"short" by the caller
    opened_at: datetime | None = None


def _as_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def match_rule(match_json: dict[str, Any], rec: TagRecord) -> bool:
    """True iff every condition in match_json holds for the record."""
    if not match_json:
        return False
    for key, val in match_json.items():
        if key == "inst_pattern":
            if not fnmatchcase(rec.inst_id or "", str(val)):
                return False
        elif key == "opt_type":
            if (rec.opt_type or "").upper() != str(val).upper():
                return False
        elif key == "side":
            if (rec.side or "").lower() != str(val).lower():
                return False
        elif key == "underlying":
            if rec.underlying != val:
                return False
        elif key == "opened_after":
            d = _as_date(val)
            if rec.opened_at is None or d is None or rec.opened_at.date() < d:
                return False
        elif key == "opened_before":
            d = _as_date(val)
            if rec.opened_at is None or d is None or rec.opened_at.date() >= d:
                return False
        else:
            return False  # unknown key → conservative non-match
    return True


def assign_strategy(rules: list[Rule], rec: TagRecord, unassigned_id: int | None) -> int | None:
    """Return the strategy_id of the first matching rule (highest priority), else unassigned."""
    for rule in sorted(rules, key=lambda r: (-r.priority, r.strategy_id)):
        if match_rule(rule.match_json, rec):
            return rule.strategy_id
    return unassigned_id
