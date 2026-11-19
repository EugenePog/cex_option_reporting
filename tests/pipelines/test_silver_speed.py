"""Build 15 — bronze → silver speed-ups, without a database: duplicate / NULL keys, the bulk
upsert SQL (unchanged rows not rewritten) and the pipeline loop skipping a silver run whose
sources did not change.
"""
from __future__ import annotations

import pytest

from app.db.models_silver import BalanceSnapshot, ClosedPosition, PositionLeg
from app.pipelines import bronze_to_silver as b2s
from app.pipelines import runner

BAL_KEY = ["subaccount_id", "ccy", "captured_at"]
BAL_COLS = ["cex_code", "subaccount_id", "ccy", "total", "available", "usd_value", "captured_at",
            "ingest_id"]


def _bal(ccy, total, ts="t1"):
    return {"cex_code": "OKX", "subaccount_id": 1, "ccy": ccy, "total": total, "available": None,
            "usd_value": None, "captured_at": ts, "ingest_id": "r"}


def test_last_duplicate_wins():
    rows = [_bal("BTC", 1.0), _bal("USDT", 2.0), _bal("BTC", 9.0), _bal("BTC", 3.0, ts="t2")]
    out = b2s._dedupe(rows, BAL_KEY)
    assert len(out) == 3                                     # (BTC, t1) collapsed
    assert [r for r in out if r["ccy"] == "BTC" and r["captured_at"] == "t1"] == [_bal("BTC", 9.0)]


def test_null_keys_kept_unless_nulls_not_distinct():
    rows = [{"cex_code": "OKX", "ext_id": "7", "opened_at": None, "pnl": p} for p in (1.0, 2.0)]
    key = ["cex_code", "ext_id", "opened_at"]
    assert len(b2s._dedupe(rows, key)) == 2                  # NULLs never conflict
    out = b2s._dedupe(rows, key, nulls_conflict=True)        # NULLS NOT DISTINCT constraint
    assert len(out) == 1 and out[0]["pnl"] == 2.0


def test_merge_sql_skips_unchanged_rows():
    sql = b2s._merge_sql(BalanceSnapshot.__table__, BAL_COLS, BAL_KEY)
    assert sql.merge.startswith('INSERT INTO silver.balance_snapshot AS t ("cex_code"')
    conflict = 'ON CONFLICT ("subaccount_id", "ccy", "captured_at")'
    assert f"FROM {sql.stage} {conflict}" in sql.merge
    set_part = sql.merge.split("DO UPDATE SET", 1)[1].split(" WHERE ", 1)[0]
    assert '"total" = EXCLUDED."total"' in set_part
    assert '"ccy" =' not in set_part                         # key columns are not updated
    where = sql.merge.split(" WHERE ", 1)[1]
    assert where.startswith('(t."cex_code", t."total"') and "IS DISTINCT FROM (EXCLUDED." in where
    assert sql.copy == f"COPY {sql.stage} ({', '.join(chr(34) + c + chr(34) for c in BAL_COLS)})" \
                       " FROM STDIN"


def test_merge_sql_stages_numeric_as_float8_and_uses_constraint():
    sql = b2s._merge_sql(ClosedPosition.__table__, ["cex_code", "ext_id", "opened_at", "pnl"],
                         ["cex_code", "ext_id", "opened_at"],
                         constraint="uq_silver_closed_position")
    assert '"pnl"::double precision AS "pnl"' in sql.create   # same float8 → numeric as before
    assert '"ext_id"::double' not in sql.create
    assert "ON COMMIT DROP" in sql.create and sql.create.endswith("WITH NO DATA")
    assert 'ON CONFLICT ON CONSTRAINT "uq_silver_closed_position"' in sql.merge


def test_position_leg_always_refreshed():
    sql = b2s._merge_sql(PositionLeg.__table__, ["cex_code", "subaccount_id", "pos_id",
                                                 "pos_opened_at", "status", "updated_at"],
                         ["cex_code", "subaccount_id", "pos_id", "pos_opened_at"], compare=False)
    assert "IS DISTINCT FROM" not in sql.merge
    assert '"updated_at" = EXCLUDED."updated_at"' in sql.merge


def test_no_rows_no_statement():
    assert b2s._upsert_many(object(), BalanceSnapshot, [], BAL_KEY) == 0   # session never used


def test_loop_skips_silver_when_sources_unchanged(monkeypatch):
    runs = []
    source = {"v": ("bronze-1",)}
    monkeypatch.setattr(runner.bronze_to_silver, "source_fingerprint", lambda: source["v"])
    monkeypatch.setattr(runner.bronze_to_silver, "run", lambda: runs.append(1) or {"x": (1, 0)})
    monkeypatch.setattr(runner, "_last_silver_source", None)

    assert runner.run_silver(skip_unchanged=True) == {"silver": {"x": (1, 0)}}   # first: runs
    assert runner.run_silver(skip_unchanged=True) == {"silver": {}}              # same: skipped
    assert runner.run_silver() == {"silver": {"x": (1, 0)}}     # manual / Box builder: always runs
    source["v"] = ("bronze-2",)
    assert runner.run_silver(skip_unchanged=True) == {"silver": {"x": (1, 0)}}   # changed: runs
    assert len(runs) == 3


def test_failed_silver_run_is_not_remembered(monkeypatch):
    monkeypatch.setattr(runner.bronze_to_silver, "source_fingerprint", lambda: ("b",))
    monkeypatch.setattr(runner, "_last_silver_source", None)

    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(runner.bronze_to_silver, "run", boom)
    with pytest.raises(RuntimeError):
        runner.run_silver(skip_unchanged=True)
    assert runner._last_silver_source is None       # the next loop tick tries again
