"""SNAPSHOT_TIMES_UTC / INGEST_TIME_UTC parsing."""
from __future__ import annotations

from config.settings import Settings


def _snap(value: str) -> Settings:
    s = Settings()
    s.snapshot_times_utc = value
    return s


def _hist(value: str) -> Settings:
    s = Settings()
    s.ingest_time_utc = value
    return s


def test_snapshot_basic_times():
    assert _snap("09:30,15:00,23:45").snapshot_time_tuples() == [(9, 30), (15, 0), (23, 45)]


def test_snapshot_tolerates_spaces_braces_quotes():
    assert _snap("{'00:00', '06:00'}").snapshot_time_tuples() == [(0, 0), (6, 0)]
    assert _snap(" 12:00 ,  18:00 ").snapshot_time_tuples() == [(12, 0), (18, 0)]


def test_snapshot_empty_tokens_skipped():
    assert _snap("00:00,,06:00,").snapshot_time_tuples() == [(0, 0), (6, 0)]


def test_ingest_time_with_minutes():
    assert _hist("10:00").ingest_time_tuple() == (10, 0)
    assert _hist("08:45").ingest_time_tuple() == (8, 45)


def test_ingest_time_bare_hour():
    assert _hist("10").ingest_time_tuple() == (10, 0)


def test_hourly_wildcard():
    assert _snap("*:00").snapshot_time_tuples() == [("*", 0)]
    assert _hist("*:15").ingest_time_tuples() == [("*", 15)]
    assert _snap("*:00, 06:30").snapshot_time_tuples() == [("*", 0), (6, 30)]


def test_defaults_are_hourly(monkeypatch):
    monkeypatch.delenv("SNAPSHOT_TIMES_UTC", raising=False)
    monkeypatch.delenv("INGEST_TIME_UTC", raising=False)
    s = Settings(_env_file=None)
    assert s.snapshot_time_tuples() == [("*", 0)]
    assert s.ingest_time_tuples() == [("*", 0)]


def test_ingest_several_times_and_duplicates():
    assert _hist("10:00,22:00,10:00").ingest_time_tuples() == [(10, 0), (22, 0)]
    assert _hist("10:00,22:00").ingest_time_tuple() == (10, 0)
