"""Seed CSV value coercion — pure, no DB."""
from __future__ import annotations

import csv
import io
from datetime import date

from sqlalchemy import Boolean, Date, Integer, Numeric, String

from app.db.models_core import StrategyRule
from app.db.seed_loader import _coerce_row, coerce_value


def test_scalar_coercions():
    assert coerce_value(Integer(), "5") == 5
    assert coerce_value(Integer(), "") is None
    assert coerce_value(Boolean(), "true") is True
    assert coerce_value(Boolean(), "0") is False
    assert coerce_value(Numeric(), "1.5") == 1.5
    assert coerce_value(Date(), "2026-08-15") == date(2026, 8, 15)
    # String keeps empty string (must NOT become NULL — matches bronze subacct_name="")
    assert coerce_value(String(), "") == ""


def test_jsonb_column_from_csv_cell():
    # A JSON object embedded in a CSV cell (quotes doubled) must round-trip to a dict.
    line = '1,1,"{""inst_pattern"": ""BTC-USD-*""}",1,100\n'
    header = "id,subaccount_id,match_json,strategy_id,priority\n"
    row = next(csv.DictReader(io.StringIO(header + line)))
    coerced = _coerce_row(StrategyRule, row)
    assert coerced["match_json"] == {"inst_pattern": "BTC-USD-*"}
    assert coerced["id"] == 1 and coerced["priority"] == 100
