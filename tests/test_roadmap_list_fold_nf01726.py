"""NF-2026-01726: roadmap_list/roadmap_snapshot fold outcome and acceptance bodies by default."""

from __future__ import annotations

import copy
import inspect

import pytest

from aiworkhub import core, server

BIG_OUTCOME = "outcome text " * 400
BIG_ACCEPTANCE = ["criterion %d " % i + "x" * 200 for i in range(20)]


def _row(**overrides):
    row = {
        "id": "RM-2026-90001",
        "title": "big roadmap item",
        "status": "proposed",
        "priority": "medium",
        "outcome": BIG_OUTCOME,
        "acceptance": list(BIG_ACCEPTANCE),
    }
    row.update(overrides)
    return row


def _snapshot_row():
    return _row(
        tasks=[{"task_id": "t1", "status": "accepted"}],
        linked_total=1,
        accepted=1,
        unresolved=0,
        dependency_blockers=[],
        dependency_ready=True,
    )


def _snapshot(rows):
    return {
        "schema_id": "aiworkhub.roadmap_snapshot.v1",
        "total": len(rows),
        "active": len(rows),
        "status_counts": {"proposed": len(rows)},
        "items": rows,
        "truncated": False,
    }


@pytest.fixture
def core_rows(monkeypatch):
    rows = [_snapshot_row()]
    snapshot = _snapshot(rows)
    monkeypatch.setattr(core, "roadmap_list", lambda **_kw: rows)
    monkeypatch.setattr(core, "roadmap_snapshot", lambda **_kw: snapshot)
    return rows, snapshot


def _identity(value):
    size, digest = core._evidence_identity(value)
    return {"summarized": True, "bytes": size, "length": len(value), "sha256": digest}


def test_roadmap_list_default_folds_outcome_and_acceptance(core_rows):
    rows, _ = core_rows
    original = copy.deepcopy(rows)

    result = server.roadmap_list()

    assert result[0]["outcome"] == _identity(BIG_OUTCOME)
    assert result[0]["acceptance"] == _identity(BIG_ACCEPTANCE)
    assert result[0]["title"] == "big roadmap item"
    assert rows == original


def test_roadmap_snapshot_default_folds_outcome_and_acceptance(core_rows):
    _, snapshot = core_rows
    original = copy.deepcopy(snapshot)

    result = server.roadmap_snapshot()

    item = result["items"][0]
    assert item["outcome"] == _identity(BIG_OUTCOME)
    assert item["acceptance"] == _identity(BIG_ACCEPTANCE)
    assert snapshot == original


def test_full_returns_raw_bodies(core_rows):
    rows, snapshot = core_rows

    assert server.roadmap_list(full=True) == rows
    assert server.roadmap_snapshot(full=True) == snapshot
    assert server.roadmap_list(full=True)[0]["outcome"] == BIG_OUTCOME
    assert server.roadmap_snapshot(full=True)["items"][0]["acceptance"] == BIG_ACCEPTANCE


def test_snapshot_aggregates_identical_between_default_and_full(core_rows):
    folded = server.roadmap_snapshot()
    raw = server.roadmap_snapshot(full=True)

    assert {k: v for k, v in folded.items() if k != "items"} == {
        k: v for k, v in raw.items() if k != "items"
    }
    for key in ("tasks", "linked_total", "accepted", "unresolved", "dependency_blockers", "dependency_ready"):
        assert folded["items"][0][key] == raw["items"][0][key]


def test_missing_and_empty_bodies_stay_as_is(monkeypatch):
    rows = [_row(outcome="", acceptance=None), {"id": "RM-2026-90002"}]
    monkeypatch.setattr(core, "roadmap_list", lambda **_kw: rows)

    result = server.roadmap_list()

    assert result[0]["outcome"] == ""
    assert result[0]["acceptance"] is None
    assert result[1] == {"id": "RM-2026-90002"}


def test_full_parameter_defaults_false():
    for tool in (server.roadmap_list, server.roadmap_snapshot):
        param = inspect.signature(tool).parameters["full"]
        assert param.default is False
