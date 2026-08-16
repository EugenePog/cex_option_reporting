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
