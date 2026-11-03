"""NF-2026-02009: sealed_request_field folds a tiny cached seal projection.

It used to call ``latest_events(path, key_field="request_id")`` -- a full merge
fold of the whole process ledger that was too large to cache -- and then copy
every row to keep one field. It must now return exactly the previous result
from a projection that retains only ``request_id`` plus the field, is cached,
and replays only appended bytes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import process_event_ledger as ledger

_FIELD = "quality_review_attempt"


@pytest.fixture(autouse=True)
def _clean_cache():
    ledger.reset_latest_event_cache()
    yield
    ledger.reset_latest_event_cache()


def _append(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("ab") as handle:
        for row in rows:
            handle.write((json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))


def _old_sealed(path: Path, field: str) -> dict[str, Any]:
    """The pre-fix implementation: full merge fold, then keep one field."""

    return {
        key: row[field]
        for key, row in ledger.latest_events(path, key_field="request_id").items()
        if isinstance(row.get(field), Mapping)
    }


def _ledger(tmp_path: Path) -> Path:
    path = tmp_path / "process_events.jsonl"
    rotated = tmp_path / "process_events.20260101T000000Z.jsonl"
    _append(
        rotated,
        [
            {"request_id": "r1", "state": "reserved", _FIELD: {"attempt": 1}},
            {"request_id": "r2", "state": "reserved", _FIELD: {"attempt": 1}},
            {"request_id": "r3", "state": "reserved", "packet": "x" * 64},
            {"request_id": "r1", "state": "running"},
        ],
    )
    _append(
        path,
        [
            # Later row overwrites the seal.
            {"request_id": "r2", "state": "running", _FIELD: {"attempt": 2}},
            # Row without the field keeps the earlier seal.
            {"request_id": "r1", "state": "succeeded", "packet": "y"},
            # A runtime_notice row carrying the field merges like any other.
            {
                "request_id": "r4",
                "event_kind": "runtime_notice",
                _FIELD: {"attempt": 9},
            },
            # A non-mapping value replaces the seal and yields no entry.
            {"request_id": "r5", _FIELD: {"attempt": 1}},
            {"request_id": "r5", _FIELD: "revoked"},
            {"state": "orphan", _FIELD: {"attempt": 7}},
            "not-a-row",
        ],
    )
    with path.open("ab") as handle:
        handle.write(b"{broken json\n")
    return path


def test_parity_with_full_fold_then_filter(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    expected = _old_sealed(path, _FIELD)
    ledger.reset_latest_event_cache()
    actual = ledger.sealed_request_field(path, _FIELD)
    assert actual == expected
    assert actual == {
        "r1": {"attempt": 1},
        "r2": {"attempt": 2},
        "r4": {"attempt": 9},
    }
    # Warm call: identical, and the copy-on-return guarantee holds.
    actual["r1"]["attempt"] = 99
    again = ledger.sealed_request_field(path, _FIELD)
    assert again == expected
    assert ledger.sealed_request_field(path, "never_written") == {}


def test_seal_projection_is_cached_and_replays_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _ledger(tmp_path)
    assert ledger.sealed_request_field(path, _FIELD)["r2"] == {"attempt": 2}
    stats = ledger.latest_event_cache_stats()
    assert stats["entries"] == 1
    # Only request_id plus the field is retained, for keys that carried it.
    (cached,) = ledger._LATEST_EVENT_CACHE.values()
    assert set(cached.latest) == {"r1", "r2", "r4", "r5"}
    assert all(set(row) <= {"request_id", _FIELD} for row in cached.latest.values())

    rebuilds: list[Any] = []
    original = ledger._rebuild_latest_events

    def counting(*args: Any, **kwargs: Any) -> Any:
        rebuilds.append(kwargs.get("cache_key"))
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger, "_rebuild_latest_events", counting)
    _append(
        path,
        [
            {"request_id": "r6", "state": "reserved", _FIELD: {"attempt": 1}},
            {"request_id": "r2", "state": "finalizing"},
        ],
    )
    sealed = ledger.sealed_request_field(path, _FIELD)
    assert rebuilds == []
    assert sealed["r6"] == {"attempt": 1}
    assert sealed["r2"] == {"attempt": 2}
    monkeypatch.setattr(ledger, "_rebuild_latest_events", original)
    assert sealed == _old_sealed(path, _FIELD)


def test_field_name_inside_a_string_value_creates_no_seal(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    _append(
        path,
        [
            {"request_id": "r1", "packet": f'review "{_FIELD}" carefully'},
            {"request_id": "r2", "packet": json.dumps({_FIELD: {"attempt": 1}})},
            {"request_id": "r3", "note": _FIELD},
        ],
    )
    assert ledger.sealed_request_field(path, _FIELD) == {}
    assert _old_sealed(path, _FIELD) == {}
