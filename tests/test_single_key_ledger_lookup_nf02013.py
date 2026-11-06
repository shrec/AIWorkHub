"""NF-2026-02013: single-key and small-key-set ledger lookups scan one key.

The merge (~347 MB) and packet-dropped (~203 MB) projections of the live
ledger exceed the latest-event cache budget, so every ``latest_event`` caller
and every ``events_for_requests`` key miss cold-folded the whole ledger. These
tests pin exact parity with the full projections, that a cold single-key read
never builds or touches the full-projection cache, that a key miss scans only
the missing keys, and that the two routed callers answer as before.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_event_ledger as ledger  # noqa: E402

KEY = "a" * 32
OTHER = "b" * 32
NOTE_ONLY = "c" * 32
ABSENT = "f" * 32
QUOTED = 'q"uote'
NUMERIC = "123"

_OPTIONS: list[dict[str, Any]] = [
    {},
    {"replace": True},
    {"skip_event_kinds": ("observation",)},
    {"drop_fields": ("packet",)},
    {"replace": True, "skip_event_kinds": ("observation",), "drop_fields": ("packet",)},
]


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch: pytest.MonkeyPatch):
    # These tests assert on the process-wide latest-event cache. A storage
    # retention sweep that an earlier test on this worker started keeps
    # reading its own ledger through that cache (seen under full-suite load),
    # so wait it out and let no new sweep start until the test ends.
    from aiworkhub import storage_retention

    monkeypatch.setattr(
        storage_retention, "schedule_repository_cleanup", lambda *_a, **_k: False
    )
    for thread in threading.enumerate():
        if thread.name == "aiworkhub-storage-retention":
            thread.join(timeout=60)
    ledger.reset_latest_event_cache()
    ledger.reset_request_event_cache()
    yield
    ledger.reset_latest_event_cache()
    ledger.reset_request_event_cache()


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _ledger(tmp_path: Path) -> Path:
    """Two rotated segments plus the active file, with later rows overwriting
    fields, an observation row, keys inside other rows' values and names,
    a numeric key and a key JSON must escape."""

    path = tmp_path / "events.jsonl"
    _write(
        tmp_path / "events.20260101T000001Z.jsonl",
        [
            {"request_id": KEY, "state": "starting", "packet": "P" * 64, "task_id": "T1"},
            {"request_id": OTHER, "state": "running", "note": f"see {KEY} {NOTE_ONLY}"},
            {"request_id": KEY, "event_kind": "observation", "state": "observed"},
            {"request_id": 123, "state": "numeric"},
            {"request_id": QUOTED, "state": "quoted"},
        ],
    )
    _write(
        tmp_path / "events.20260101T000002Z.jsonl",
        [
            {"request_id": KEY, "state": "running", "pid": 7},
            {"request_id": OTHER, KEY: "as-a-field-name", "state": "done"},
            {"request_id": NUMERIC, "extra": "string-keyed"},
        ],
    )
    _write(
        path,
        [
            {"request_id": KEY, "state": "launch_failed", "error": "boom", "changed_paths": []},
            {"request_id": OTHER, "nested": {"request_id": KEY}},
            {"request_id": QUOTED, "event_kind": "observation", "state": "seen"},
        ],
    )
    return path


def _cache_snapshot() -> list[Any]:
    with ledger._LATEST_EVENT_CACHE_LOCK:
        return [(key, id(value)) for key, value in ledger._LATEST_EVENT_CACHE.items()]


@pytest.mark.parametrize("options", _OPTIONS)
@pytest.mark.parametrize("key", [KEY, OTHER, NOTE_ONLY, ABSENT, QUOTED, NUMERIC])
def test_latest_event_matches_full_projection(tmp_path: Path, options, key) -> None:
    path = _ledger(tmp_path)
    cold = ledger.latest_event(path, key, **options)
    ledger.reset_latest_event_cache()
    expected = ledger.latest_events(path, **options).get(key)
    assert cold == expected
    # Served from the now-cached projection, the answer is the same.
    assert ledger.latest_event(path, key, **options) == expected
    if key in (NOTE_ONLY, ABSENT):
        assert cold is None


def test_later_rows_overwrite_and_keys_inside_values_never_match(tmp_path: Path) -> None:
    path = _ledger(tmp_path)
    assert ledger.latest_event(path, KEY) == {
        "request_id": KEY,
        "state": "launch_failed",
        "packet": "P" * 64,
        "task_id": "T1",
        "event_kind": "observation",
        "pid": 7,
        "error": "boom",
        "changed_paths": [],
    }
    assert ledger.latest_event(path, KEY, replace=True) == {
        "request_id": KEY, "state": "launch_failed", "error": "boom", "changed_paths": []
    }
    assert ledger.latest_event(path, NOTE_ONLY) is None


def test_cold_single_key_never_builds_or_touches_full_projection_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _ledger(tmp_path)
    # An unrelated cached projection must be neither evicted nor reordered.
    ledger.latest_events(path, drop_fields=("packet",))
    ledger.latest_events(path, replace=True)
    before = _cache_snapshot()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("cold single-key lookup rebuilt the full projection")

    monkeypatch.setattr(ledger, "_rebuild_latest_events", forbidden)
    monkeypatch.setattr(ledger, "_cache_projection", forbidden)
    for options in ({}, {"skip_event_kinds": ("observation",)}):
        row = ledger.latest_event(path, KEY, **options)
        assert row is not None and row["state"] == "launch_failed"
        assert ledger.latest_event(path, ABSENT, **options) is None
    assert _cache_snapshot() == before


def test_cached_projection_is_served_with_incremental_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _ledger(tmp_path)
    ledger.latest_events(path)
    _write(path, [{"request_id": KEY, "state": "succeeded"}])
    monkeypatch.setattr(
        ledger, "_iter_events_containing", lambda *_a, **_k: pytest.fail("rescanned")
    )
    monkeypatch.setattr(
        ledger, "_rebuild_latest_events", lambda *_a, **_k: pytest.fail("rebuilt")
    )
    row = ledger.latest_event(path, KEY)
    assert row is not None and row["state"] == "succeeded"


def _expected_events(path: Path, keys: list[str]) -> dict[str, list[dict[str, Any]]]:
    rows = list(ledger.iter_events(path))
    return {key: [row for row in rows if row.get("request_id") == key] for key in keys}


def test_events_for_requests_miss_scans_only_missing_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _ledger(tmp_path)
    assert ledger.events_for_requests(path, [KEY]) == _expected_events(path, [KEY])
    _write(path, [{"request_id": KEY, "state": "succeeded"}])

    scanned: list[list[str]] = []
    original_scan = ledger._scan_request_events

    def spy_scan(scan_path, keys, **kwargs):
        scanned.append(list(keys))
        return original_scan(scan_path, keys, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("key miss replayed the full canonical ledger")

    monkeypatch.setattr(ledger, "_scan_request_events", spy_scan)
    monkeypatch.setattr(ledger, "iter_events", forbidden)
    warm = ledger.events_for_requests(path, [KEY, OTHER, NOTE_ONLY])
    assert scanned == [[OTHER, NOTE_ONLY]]
    monkeypatch.undo()

    ledger.reset_request_event_cache()
    cold = ledger.events_for_requests(path, [KEY, OTHER, NOTE_ONLY])
    assert warm == cold == _expected_events(path, [KEY, OTHER, NOTE_ONLY])
    assert warm[KEY][-1]["state"] == "succeeded"
    assert warm[NOTE_ONLY] == []


def test_events_for_requests_seeds_cache_while_active_file_grows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _ledger(tmp_path)
    original_scan = ledger._scan_request_events

    def scan_then_append(scan_path, keys, **kwargs):
        result = original_scan(scan_path, keys, **kwargs)
        _write(path, [{"request_id": KEY, "state": "appended-during-scan"}])
        return result

    monkeypatch.setattr(ledger, "_scan_request_events", scan_then_append)
    first = ledger.events_for_requests(path, [KEY, OTHER])
    monkeypatch.undo()
    with ledger._REQUEST_EVENT_CACHE_LOCK:
        assert ledger._REQUEST_EVENT_CACHE, "the snapshot scan must seed the cache"
    assert first[KEY][-1]["state"] == "launch_failed"

    monkeypatch.setattr(
        ledger, "_rebuild_request_events", lambda *_a, **_k: pytest.fail("rebuilt")
    )
    second = ledger.events_for_requests(path, [KEY, OTHER])
    assert second == _expected_events(path, [KEY, OTHER])
    monkeypatch.undo()

    # A key JSON must escape has no exact raw token: the canonical pass answers
    # it with the same rows.
    ledger.reset_request_event_cache()
    assert ledger.events_for_requests(path, [QUOTED]) == _expected_events(path, [QUOTED])
    assert second[KEY][-1]["state"] == "appended-during-scan"


def test_routed_callers_return_what_they_returned_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiworkhub import core, process_launcher, task_retention

    path = _ledger(tmp_path)
    _write(
        path,
        [
            {
                "request_id": OTHER,
                "task_id": "T2",
                "runner": "claude",
                "adapter_id": "claude_cli",
                "model": "m",
                "state": "launch_failed",
                "failure_kind": "launch_failed",
                "error": "boom",
                "exit_code": 1,
                "changed_paths": [],
                "packet": "P" * 64,
            }
        ],
    )
    monkeypatch.setenv(process_launcher.PROCESS_LOG_ENV, str(path))

    full = ledger.latest_events(path)[OTHER]
    dropped = ledger.latest_events(path, drop_fields=("packet",))
    ledger.reset_latest_event_cache()

    verified = core._verified_zero_delta_launch_failure_event(
        {}, task_id="T2", request_id=OTHER, runner="claude", reason="boom"
    )
    assert verified == {
        key: full.get(key)
        for key in (
            "request_id", "task_id", "runner", "adapter_id", "model", "state",
            "failure_kind", "error", "exit_code", "changed_paths",
        )
    }
    assert core._verified_zero_delta_launch_failure_event(
        {}, task_id="T2", request_id=ABSENT, runner="claude", reason="boom"
    ) is None

    assert task_retention._latest_process_row(tmp_path, OTHER) == dropped[OTHER]
    assert task_retention._latest_process_row(tmp_path, KEY) == dropped[KEY]
    assert task_retention._latest_process_row(tmp_path, ABSENT) is None
    assert ledger.latest_event_cache_stats()["entries"] == 0
