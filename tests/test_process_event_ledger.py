from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from aiworkhub import process_event_ledger


def test_rotates_before_active_bound_and_streams_all_rows(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    rows = [
        {"request_id": f"request-{index}", "state": "running", "payload": "x" * 90}
        for index in range(12)
    ]
    for row in rows:
        process_event_ledger.append_event(path, row, max_active_bytes=1024)

    ledgers = process_event_ledger.ledger_paths(path)
    assert len(ledgers) > 1
    assert ledgers[-1] == path
    assert path.stat().st_size <= 1024
    assert list(process_event_ledger.iter_events(path)) == rows


def test_stream_reader_skips_malformed_rows(tmp_path: Path) -> None:
    path = tmp_path / "process_events.jsonl"
    path.write_text('{"request_id":"good"}\nnot-json\n', encoding="utf-8")
    assert list(process_event_ledger.iter_events(path)) == [{"request_id": "good"}]


def test_append_lock_timeout_publishes_ordered_immutable_spill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "process_events.jsonl"
    earlier = {
        "request_id": "request-old",
        "state": "release_pending",
        "timestamp": "2026-08-09T10:00:00+00:00",
    }
    recovery = {
        "request_id": "request-old",
        "state": "finalize_failed",
        "timestamp": "2026-08-09T10:01:00+00:00",
    }
    process_event_ledger.append_event(path, earlier)

    @contextmanager
    def timed_out_lock(_path: Path):
        raise TimeoutError("windows_advisory_lock_timeout after 20s")
        yield

    monkeypatch.setattr(process_event_ledger, "_append_lock", timed_out_lock)
    process_event_ledger.append_event(path, recovery)

    spills = [
        candidate
        for candidate in process_event_ledger.ledger_paths(path)
        if ".spill." in candidate.name
    ]
    assert len(spills) == 1
    assert list(process_event_ledger.iter_events(path)) == [earlier, recovery]


def test_multiple_spills_merge_with_active_events_by_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "process_events.jsonl"
    first = {
        "request_id": "request-a",
        "state": "starting",
        "timestamp": "2026-08-09T10:00:00+00:00",
    }
    process_event_ledger.append_event(path, first)

    @contextmanager
    def timed_out_lock(_path: Path):
        raise TimeoutError("windows_advisory_lock_timeout after 20s")
        yield

    monkeypatch.setattr(process_event_ledger, "_append_lock", timed_out_lock)
    second = {
        "request_id": "request-b",
        "state": "starting",
        "timestamp": "2026-08-09T10:00:01+00:00",
    }
    third = {
        "request_id": "request-a",
        "state": "running",
        "timestamp": "2026-08-09T10:00:02+00:00",
    }
    process_event_ledger.append_event(path, third)
    process_event_ledger.append_event(path, second)

    assert list(process_event_ledger.iter_events(path)) == [first, second, third]


def test_non_timeout_append_lock_failure_remains_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "process_events.jsonl"

    @contextmanager
    def denied_lock(_path: Path):
        raise PermissionError("denied")
        yield

    monkeypatch.setattr(process_event_ledger, "_append_lock", denied_lock)
    with pytest.raises(PermissionError, match="denied"):
        process_event_ledger.append_event(path, {"request_id": "request-a"})
    assert process_event_ledger.ledger_paths(path) == []


def test_latest_events_reuses_projection_and_reads_only_complete_appends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "process_events.jsonl"
    process_event_ledger.append_event(
        path, {"request_id": "request-a", "state": "starting"}
    )
    reads = 0
    original = process_event_ledger._iter_ledger_file

    def counted(ledger: Path):
        nonlocal reads
        reads += 1
        yield from original(ledger)

    monkeypatch.setattr(process_event_ledger, "_iter_ledger_file", counted)
    assert process_event_ledger.latest_events(path)["request-a"]["state"] == "starting"
    cold_reads = reads
    assert process_event_ledger.latest_events(path)["request-a"]["state"] == "starting"
    assert reads == cold_reads

    partial = b'{"request_id":"request-a","state":"running"}'
    with path.open("ab") as handle:
        handle.write(partial)
    assert process_event_ledger.latest_events(path)["request-a"]["state"] == "starting"
    with path.open("ab") as handle:
        handle.write(b"\n")
    assert process_event_ledger.latest_events(path)["request-a"]["state"] == "running"
    assert reads == cold_reads


def test_latest_events_invalidates_deleted_segment_and_truncated_active(
    tmp_path: Path,
) -> None:
    path = tmp_path / "process_events.jsonl"
    archive = tmp_path / "process_events.20260821T000000.000000Z.1.a.jsonl"
    archive.write_text(
        '{"request_id":"request-archive","state":"finished"}\n',
        encoding="utf-8",
    )
    path.write_text(
        '{"request_id":"request-a","state":"starting"}\n'
        '{"request_id":"request-b","state":"starting"}\n',
        encoding="utf-8",
    )
    assert set(process_event_ledger.latest_events(path)) == {
        "request-archive",
        "request-a",
        "request-b",
    }

    archive.unlink()
    path.write_text(
        '{"request_id":"request-b","state":"finished"}\n', encoding="utf-8"
    )
    assert process_event_ledger.latest_events(path) == {
        "request-b": {"request_id": "request-b", "state": "finished"}
    }


def test_latest_events_preserves_spill_timestamp_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "process_events.jsonl"
    process_event_ledger.append_event(
        path,
        {
            "request_id": "request-a",
            "state": "starting",
            "timestamp": "2026-08-21T00:00:00+00:00",
        },
    )

    @contextmanager
    def timed_out_lock(_path: Path):
        raise TimeoutError("locked")
        yield

    monkeypatch.setattr(process_event_ledger, "_append_lock", timed_out_lock)
    process_event_ledger.append_event(
        path,
        {
            "request_id": "request-a",
            "state": "finished",
            "timestamp": "2026-08-21T00:00:01+00:00",
        },
    )
    assert process_event_ledger.latest_events(path)["request-a"]["state"] == "finished"
