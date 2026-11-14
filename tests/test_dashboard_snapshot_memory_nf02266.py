"""NF-2026-02266: the cost ledger's process-event index stays bounded.

The dashboard snapshot builds the cost ledger, which indexes the newest
``_PROCESS_EVENT_LIMIT`` lines of ``process_events.jsonl``. That index used to
hold every parsed event whole (packets, prompts, stdout chunks) inside each
memoized ledger, and the raw window of lines was materialized at once. These
tests pin that the index keeps only the fields ledger readers touch, with the
same values the full-event merge produced, and that the ledger rows built from
it are identical.
"""

from __future__ import annotations

import json
import tracemalloc
import types
from collections import deque
from pathlib import Path
from typing import Any

from aiworkhub import cost_ledger


_LARGE = "x" * 4096


def _legacy_index(
    repo_root: Path, *, resolved_root: str | None = None
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """The pre-NF-02266 index: full parsed events, merged unprojected."""

    path = repo_root / cost_ledger._PROCESS_EVENTS_PATH
    events: dict[tuple[str, str, str], dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in deque(handle, maxlen=cost_ledger._PROCESS_EVENT_LIMIT):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            task_id = str(event.get("task_id") or "")
            request_id = cost_ledger._request_identity(event)
            repository = cost_ledger._repository_identity(
                event, repo_root, resolved_root=resolved_root
            )
            if task_id and request_id and repository:
                identity = (repository, task_id, request_id)
                previous = events.get(identity)
                events[identity] = (
                    cost_ledger._merge_process_events(previous, event)
                    if previous is not None
                    else event
                )
    return events


def _write_ledger(repo_root: Path, lines: list[str], *, newline: str = "\n") -> Path:
    path = repo_root / cost_ledger._PROCESS_EVENTS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(newline.join(lines).encode("utf-8"))
    return path


def _fixture_lines() -> list[str]:
    def dump(event: dict[str, Any] | list[Any]) -> str:
        return json.dumps(event)

    return [
        dump({
            "task_id": "T1", "request_id": "R1", "repository_id": "repo-1",
            "state": "running", "role": "worker", "topic": "code",
            "packet": _LARGE, "prompt": _LARGE,
        }),
        dump({
            "task_id": "T1", "request_id": "R1", "repository_id": "repo-1",
            "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
            "usage_observed": True, "cost_usd": 0.5, "cost_observed": True,
            "stdout": _LARGE,
        }),
        dump({
            "task_id": "T1", "request_id": "R1", "repository_id": "repo-1",
            "status": "completed", "state": "failed",
            "terminal_substatus": "validation_failed",
            "finished_at": "2026-09-01T01:02:03+00:00", "transcript": _LARGE,
        }),
        # Nested usage that is spread over the event when matched.
        dump({
            "task_id": "T2", "attempt_id": "A2", "repository_id": "repo-1",
            "usage": {
                "input_tokens": 7, "output_tokens": 3, "total_tokens": 10,
                "cached_input_tokens": 4, "role": "reviewer", "raw": _LARGE,
            },
            "cache_metrics_observed": True, "events": [_LARGE, _LARGE],
        }),
        # Note-only request identity, with an unrelated note on a later line.
        dump({
            "task_id": "T3", "note": "task_mcp_request:R3", "repo_root": "/r",
            "total_tokens": 5, "usage_observed": True, "topic": "docs",
        }),
        dump({
            "task_id": "T3", "note": "task_mcp_request:R3", "repo_root": "/r",
            "usage": "not-a-dict", "state": "done",
        }),
        # A later non-dict usage replaces an earlier dict usage on merge.
        dump({
            "task_id": "T4", "request_id": "R4", "repository": "repo-4",
            "usage": {"total_tokens": 9, "usage_observed": True},
        }),
        dump({
            "task_id": "T4", "request_id": "R4", "repository": "repo-4",
            "usage": None, "note": "plain operator note " + _LARGE,
        }),
        # Lines that can never enter the index.
        dump({"task_id": "T5", "line": _LARGE}),
        dump({"request_id": "R6", "line": _LARGE}),
        dump(["task_id", "request_id"]),
        '{"task_id": "T7", "request_id": "R7", broken',
        "",
        # Repository identity falls back to the resolved root.
        dump({
            "task_id": "T8", "request_id": "R8", "total_tokens": 0,
            "usage_observed": False, "stderr": _LARGE,
        }),
    ]


def _read_fields_equal(projected: dict[str, Any], legacy: dict[str, Any]) -> None:
    for field in cost_ledger._PROCESS_EVENT_READ_FIELDS:
        assert (field in projected) == (field in legacy), field
        assert projected.get(field) == legacy.get(field), field
    assert cost_ledger._request_identity(projected) == cost_ledger._request_identity(
        legacy
    )
    legacy_usage = legacy.get("usage")
    projected_usage = projected.get("usage")
    assert isinstance(projected_usage, dict) == isinstance(legacy_usage, dict)
    if isinstance(legacy_usage, dict):
        spread_legacy = {**legacy, **legacy_usage}
        spread_projected = {**projected, **projected_usage}
        for field in cost_ledger._PROCESS_EVENT_READ_FIELDS:
            assert spread_projected.get(field) == spread_legacy.get(field), field


def _contains_large(value: Any) -> bool:
    if isinstance(value, str):
        return _LARGE in value
    if isinstance(value, dict):
        return any(_contains_large(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_large(item) for item in value)
    return False


def test_projected_index_matches_legacy_merge_for_read_fields(tmp_path) -> None:
    for newline in ("\n", "\r\n"):
        _write_ledger(tmp_path, _fixture_lines(), newline=newline)
        resolved = str(tmp_path.resolve())

        legacy = _legacy_index(tmp_path, resolved_root=resolved)
        projected = cost_ledger._process_event_index(tmp_path, resolved_root=resolved)

        assert set(projected) == set(legacy)
        assert len(projected) == 5
        for identity, event in projected.items():
            _read_fields_equal(event, legacy[identity])
        # The legacy index held the large payloads; the projection holds none.
        assert any(_contains_large(event) for event in legacy.values())
        assert not any(_contains_large(event) for event in projected.values())
        for event in projected.values():
            assert set(event) <= {
                *cost_ledger._PROCESS_EVENT_READ_FIELDS, "note", "usage"
            }


def test_canonical_rows_identical_with_projected_index(monkeypatch, tmp_path) -> None:
    _write_ledger(tmp_path, _fixture_lines())
    resolved = str(tmp_path.resolve())
    entries = [
        {"task_id": "T1", "request_id": "R1", "repository_id": "repo-1",
         "total_tokens": 0, "usage_observed": False, "created_at": "2026-09-01T00:00:00"},
        {"task_id": "T2", "attempt_id": "A2", "repository_id": "repo-1",
         "usage_observed": False},
        {"task_id": "T3", "note": "task_mcp_request:R3", "repo_root": "/r"},
        {"task_id": "T4", "request_id": "R4", "repository": "repo-4", "topic": "x"},
        {"task_id": "T8", "request_id": "R8"},
        {"task_id": "T9", "request_id": "R9", "topic": "unindexed"},
    ]
    monkeypatch.setattr(cost_ledger, "_task_topic_index", lambda _root: {})

    def context(index: dict[tuple[str, str, str], dict[str, Any]]) -> dict[str, Any]:
        return {
            "resolved_root": resolved,
            "process_events": index,
            "indexed_requests": {(key[1], key[2]) for key in index} or None,
            "topic_index": None,
        }

    legacy_rows = cost_ledger._canonical_rows_from_entries(
        tmp_path,
        [dict(entry) for entry in entries],
        context(_legacy_index(tmp_path, resolved_root=resolved)),
    )
    projected_rows = cost_ledger._canonical_rows_from_entries(
        tmp_path,
        [dict(entry) for entry in entries],
        context(cost_ledger._process_event_index(tmp_path, resolved_root=resolved)),
    )

    assert json.dumps(projected_rows, sort_keys=True) == json.dumps(
        legacy_rows, sort_keys=True
    )
    by_task = {row["task_id"]: row for row in projected_rows}
    assert by_task["T1"]["total_tokens"] == 120
    assert by_task["T1"]["terminal_cause"] == "validation_failed"
    assert by_task["T2"]["cached_input_tokens"] == 4
    assert by_task["T3"]["topic"] == "docs"


def test_lines_without_identity_marks_are_never_decoded(monkeypatch, tmp_path) -> None:
    _write_ledger(tmp_path, _fixture_lines())
    decoded: list[str] = []

    def counting_loads(text: str) -> Any:
        decoded.append(text)
        return json.loads(text)

    monkeypatch.setattr(
        cost_ledger,
        "json",
        types.SimpleNamespace(loads=counting_loads, JSONDecodeError=json.JSONDecodeError),
    )
    cost_ledger._process_event_index(tmp_path)

    assert not any('"line"' in text for text in decoded)
    # Nine identity-bearing events, the identity-only JSON list and the
    # broken identity line; the empty line and the two half-identity lines
    # are skipped before json.loads.
    assert len(decoded) == 11


def test_window_is_newest_lines_and_bounded_to_first_pass(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cost_ledger, "_PROCESS_EVENT_LIMIT", 3)
    lines = [
        json.dumps({"task_id": f"T{n}", "request_id": f"R{n}", "repository_id": "r"})
        for n in range(6)
    ]
    path = _write_ledger(tmp_path, lines)

    index = cost_ledger._process_event_index(tmp_path)
    assert sorted(key[1] for key in index) == ["T3", "T4", "T5"]
    assert set(index) == set(_legacy_index(tmp_path))

    with path.open("rb") as handle:
        tail = cost_ledger._process_event_tail(handle)
        first = next(tail)
        # A concurrent append after the first pass never widens the window.
        with path.open("ab") as writer:
            writer.write(b"\n" + json.dumps({"task_id": "T9", "request_id": "R9"}).encode())
        rest = list(tail)
    assert [first, *rest] == [
        (line + "\n").encode() if n < 2 else line.encode()
        for n, line in enumerate(lines[3:])
    ]


def test_index_peak_and_retained_memory_stay_bounded(tmp_path) -> None:
    payload = "p" * 65_536
    lines = [
        json.dumps({
            "task_id": f"T{n % 50}", "request_id": f"R{n % 50}",
            "repository_id": "repo", "total_tokens": n, "usage_observed": True,
            "packet": payload,
        })
        for n in range(300)
    ]
    path = _write_ledger(tmp_path, lines)
    assert path.stat().st_size > 19_000_000

    tracemalloc.start()
    try:
        baseline, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        index = cost_ledger._process_event_index(tmp_path)
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(index) == 50
    # The old index materialized the ~19 MB window and kept a 64 KB packet
    # per identity (~3 MB); one raw line plus its parse is the new peak.
    assert peak - baseline < 2_000_000
    assert current - baseline < 500_000
    assert not any(_contains_large(event) or "packet" in event for event in index.values())
