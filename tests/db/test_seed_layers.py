"""Seed folders and file format (build 11) — pure, no DB."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.db.seed_loader import COMMON, NATURAL_KEYS, PARENTS, SeedError, _read_rows, seed_folders

REPO_SEED = Path(__file__).resolve().parents[2] / "seed"


def test_layers_common_then_env(tmp_path):
    (tmp_path / "common").mkdir()
    (tmp_path / "dev").mkdir()
    assert seed_folders(tmp_path, "dev") == [tmp_path / "common", tmp_path / "dev"]


def test_layers_without_common(tmp_path):
    (tmp_path / "test").mkdir()
    assert seed_folders(tmp_path, "test") == [tmp_path / "test"]


def test_unknown_env_names_the_available_ones(tmp_path):
    for d in ("common", "dev", "test", "examples"):
        (tmp_path / d).mkdir()
    with pytest.raises(FileNotFoundError) as e:
        seed_folders(tmp_path, "staging")
    assert "staging" in str(e.value) and "dev, test" in str(e.value)


def test_old_format_with_parent_ids_is_refused(tmp_path):
    p = tmp_path / "strategy.csv"
    p.write_text("id,subaccount_id,name\n1,1,x\n")
    with pytest.raises(SeedError, match="old seed format"):
        _read_rows(p, "strategy")


def test_id_column_is_ignored_and_blank_lines_skipped(tmp_path, caplog):
    p = tmp_path / "user.csv"
    p.write_text("id,email,role\n7,a@b.c,admin\n,,\n8,d@e.f,client\n")
    rows = _read_rows(p, "user")
    assert [r.raw["email"] for r in rows] == ["a@b.c", "d@e.f"]
    assert [r.line for r in rows] == [2, 4]
    assert "ignored" in caplog.text


def test_every_seeded_table_has_a_key_and_known_parents():
    assert set(NATURAL_KEYS) == {"user", "cex_account", "subaccount", "strategy", "strategy_rule",
                                 "contract_size", "notification_setting"}
    assert PARENTS["strategy_rule"] == ("subaccount", "strategy")


def test_repo_seed_files_have_no_ids():
    files = [p for d in (COMMON, "dev", "test", "prod", "examples")
             for p in sorted((REPO_SEED / d).glob("*.csv"))]
    assert files, "no seed files found"
    for p in files:
        header = p.read_text(encoding="utf-8").splitlines()[0].split(",")
        assert "id" not in header and not any(h.endswith("_id") for h in header), p
    assert not list(REPO_SEED.glob("*.csv")), "seed files left in the old place"
    assert not list((REPO_SEED / "prod").glob("*.csv")), "prod seed should start empty"
