"""Strategy tagging: resolve a position LEG's strategy — manual pin first, then strategy_rule.

Precedence (resolve_strategy):
    1. manual pin   — the leg's current core.strategy_link row (action='pin'), set in Box builder
    2. strategy_rule — highest `priority` match wins (match_json below)
    3. unassigned   — the subaccount's "unassigned" strategy

Tagging happens once per leg (silver.position_leg, key posId + cTime); snapshots and closed
positions inherit the leg's result, fills link to the leg (no strategy of their own).

match_json vocabulary (keys AND-ed together):
    inst_pattern   glob on inst_id           e.g. "BTC-USD-*"
    opt_type       "C" | "P"
    side           "long" | "short"
    underlying     exact, e.g. "BTC-USD"
    opened_after   UTC date "YYYY-MM-DD"  (inclusive)
    opened_before  UTC date "YYYY-MM-DD"  (exclusive)

Rules for a subaccount are evaluated highest-`priority` first; the first match wins. If nothing
matches, the subaccount's "unassigned" strategy is used. `opened_at` is the leg's open time (cTime)
and `side` the leg's direction (long/short), so a rule evaluates the same for every row of a leg.
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


def first_matching_rule(rules: list[Rule], rec: TagRecord) -> Rule | None:
    """The highest-priority rule matching the record (ties broken by strategy_id), or None."""
    for rule in sorted(rules, key=lambda r: (-r.priority, r.strategy_id)):
        if match_rule(rule.match_json, rec):
            return rule
    return None


def assign_strategy(rules: list[Rule], rec: TagRecord, unassigned_id: int | None) -> int | None:
    """Return the strategy_id of the first matching rule (highest priority), else unassigned."""
    rule = first_matching_rule(rules, rec)
    return rule.strategy_id if rule else unassigned_id


# strategy_source values written to silver/gold
SOURCE_MANUAL, SOURCE_RULE, SOURCE_DEFAULT = "manual", "rule", "default"


@dataclass(frozen=True)
class Resolution:
    strategy_id: int | None
    source: str                    # 'manual' | 'rule' | 'default'
    rule_strategy_id: int | None   # what the rules alone would assign (shown as "without the pin")


def resolve_strategy(rules: list[Rule], rec: TagRecord, unassigned_id: int | None,
                     pinned_strategy_id: int | None = None) -> Resolution:
    """Final strategy for a leg: manual pin > highest-priority rule > unassigned."""
    rule = first_matching_rule(rules, rec)
    rule_sid = rule.strategy_id if rule else unassigned_id
    if pinned_strategy_id is not None:
        return Resolution(pinned_strategy_id, SOURCE_MANUAL, rule_sid)
    if rule is not None:
        return Resolution(rule.strategy_id, SOURCE_RULE, rule_sid)
    return Resolution(unassigned_id, SOURCE_DEFAULT, unassigned_id)
