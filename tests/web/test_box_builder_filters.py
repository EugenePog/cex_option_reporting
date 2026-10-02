"""Box builder leg filters: they narrow the Legs list only (the board API still returns every leg
of the account for the boxes, plus the ids that pass the filters)."""
from __future__ import annotations

from app.web.admin.box_builder import _filter_legs


def _leg(i, uly="BTC-USD", day="2026-09-01", status="closed", source="rule", inst=None):
    return {"id": i, "underlying": uly, "pos_opened_at": f"{day}T08:00:00+00:00",
            "status": status, "strategy_source": source,
            "inst_id": inst or f"{uly}-260902-78000-C", "pos_id": f"p{i}"}


LEGS = [
    _leg(1),
    _leg(2, uly="ETH-USD", day="2026-09-05", status="open", source="manual"),
    _leg(3, day="2026-09-10", status="stale", source="default", inst="BTC-USD-260911-80000-P"),
]


def _ids(**kw):
    keys = ("underlying", "opened_from", "opened_to", "status", "source", "q")
    args = {k: kw.get(k) for k in keys}
    return [lg["id"] for lg in _filter_legs(LEGS, **args)]


def test_no_filter_keeps_every_leg():
    assert _ids() == [1, 2, 3]


def test_each_filter_narrows_the_legs():
    assert _ids(underlying="ETH-USD") == [2]
    assert _ids(opened_from="2026-09-05") == [2, 3]
    assert _ids(opened_to="2026-09-05") == [1, 2]
    assert _ids(status="open") == [2]
    assert _ids(source="default") == [3]
    assert _ids(q="80000-p") == [3]          # inst_id, case-insensitive
    assert _ids(q="p2") == [2]               # posId


def test_filters_combine():
    assert _ids(underlying="BTC-USD", opened_from="2026-09-02") == [3]
