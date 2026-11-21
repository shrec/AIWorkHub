"""NF-2026-02014: public ``latest_events`` exposes the ``keep_fields`` fold.

Terminal-log retention reads a handful of fields per request, yet paid a cold
fold of every full row. ``keep_fields`` folds only the named keys; each kept
field must end with exactly the value the unfiltered fold holds.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import process_event_ledger as pel

KEEP = ("request_id", "state", "task_id", "finished_at")


@pytest.fixture(autouse=True)
def _cold_caches():
    pel.reset_latest_event_cache()
    yield
    pel.reset_latest_event_cache()


def _write_rows(target: Path, rows: list[dict[str, Any]]) -> None:
    with target.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _archive_name(index: int) -> str:
    return f"process_events.20260101T0000{index:02d}.000000Z.1.{index:08x}.jsonl"


def _build(root: Path) -> Path:
    directory = root / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "process_events.jsonl"
    for index in range(3):
        rows: list[dict[str, Any]] = []
        for number in range(5):
            request_id = f"req-{index}-{number}"
            rows.append({
                "request_id": request_id,
                "state": "starting",
                "task_id": f"task-{number}",
                "packet": "p" * 64,
            })
            # Later event omits task_id / state: merge semantics must keep them.
            rows.append({"request_id": request_id, "pid": 100 + number, "packet": "q"})
            if number % 2:
                rows.append({
                    "request_id": request_id,
                    "state": "finished",
                    "finished_at": f"2026-01-0{index + 1}T00:00:00+00:00",
                })
        rows.append({"request_id": "only-unkept", "pid": 1, "packet": "z"})
        rows.append({"no_request": True, "state": "x"})
        _write_rows(path, rows)
        os.replace(path, directory / _archive_name(index))
    _write_rows(path, [
        {"request_id": "req-0-0", "state": "finished", "extra": 1},
        {"request_id": "req-active", "state": "running", "task_id": "t-active"},
        {"request_id": "req-active", "pid": 7},
    ])
    return path


def _restrict(
    full: dict[str, dict[str, Any]], keep: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, row in full.items():
        kept = {k: v for k, v in row.items() if k in keep}
        if any(k != "request_id" for k in kept):
            out[key] = kept
    return out


def test_keep_fields_equals_full_projection_restricted(tmp_path: Path) -> None:
    path = _build(tmp_path)
    full = pel.latest_events(path)
    assert any("packet" in row for row in full.values())
    pel.reset_latest_event_cache()

    kept = pel.latest_events(path, keep_fields=KEEP)

    assert kept == _restrict(full, KEEP)
    assert "only-unkept" not in kept
    assert all("packet" not in row and "pid" not in row for row in kept.values())
    # Merge semantics: a later event omitting task_id leaves the earlier value.
    assert kept["req-0-0"]["task_id"] == "task-0"
    assert kept["req-0-0"]["state"] == "finished"
    assert kept["req-active"] == {
        "request_id": "req-active", "state": "running", "task_id": "t-active",
    }


def test_keep_fields_second_call_is_identical(tmp_path: Path) -> None:
    path = _build(tmp_path)
    first = pel.latest_events(path, keep_fields=KEEP)
    second = pel.latest_events(path, keep_fields=KEEP)
    assert second == first
    assert second is not first
    pel.reset_latest_event_cache()
    assert pel.latest_events(path, keep_fields=KEEP) == first


def test_keep_fields_follows_appended_rows(tmp_path: Path) -> None:
    path = _build(tmp_path)
    before = pel.latest_events(path, keep_fields=KEEP)
    _write_rows(path, [{"request_id": "req-active", "state": "finished"}])
    after = pel.latest_events(path, keep_fields=KEEP)
    assert before["req-active"]["state"] == "running"
    assert after["req-active"]["state"] == "finished"
    assert after == _restrict(pel.latest_events(path), KEEP)


def test_empty_keep_fields_equals_default(tmp_path: Path) -> None:
    path = _build(tmp_path)
    default = pel.latest_events(path)
    pel.reset_latest_event_cache()
    assert pel.latest_events(path, keep_fields=()) == default
    assert pel.latest_events(path, keep_fields=[]) == default


def test_keep_fields_rejects_bare_string(tmp_path: Path) -> None:
    path = _build(tmp_path)
    with pytest.raises(ValueError, match="keep_fields"):
        pel.latest_events(path, keep_fields="state")  # type: ignore[arg-type]
