from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
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
    assert list(process_event_ledger.iter_events(path)) == [
        earlier,
        {
            **recovery,
            "terminal_reason": {
                "code": "terminal_reason_missing",
                "taxonomy": "observability_missing_cause",
                "source": "append_event",
                "message": "terminal failure has no supported scalar cause",
                "missing_cause": True,
                "alertable": True,
            },
        },
    ]


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


_FAILURE_STATES = (
    "validation_failed",
    "worker_failed",
    "launch_failed",
    "finalize_failed",
    "blocked",
    "cancelled",
    "timed_out",
    "process_lost",
    "liveness_lost",
    "scope_rejected",
    "output_budget_exceeded",
)


@pytest.mark.parametrize("state", _FAILURE_STATES)
def test_failure_states_persist_fixed_canonical_terminal_reason(
    tmp_path: Path, state: str
) -> None:
    path = tmp_path / f"{state}.jsonl"
    event = {
        "request_id": state,
        "state": state.upper(),
        "terminal_reason": {
            "code": "caller_safe_code",
            "taxonomy": "caller_safe_taxonomy",
            "source": "caller_safe_source",
            "message": "  explicit cause  ",
            "alertable": False,
            "custom": {"secret": "must not survive"},
        },
    }
    original = deepcopy(event)

    process_event_ledger.append_event(path, event)
    persisted = list(process_event_ledger.iter_events(path))[0]

    assert event == original
    assert persisted["state"] == state
    assert persisted["terminal_reason"] == {
        "code": state,
        "taxonomy": "lifecycle_terminal_failure",
        "source": "terminal_reason",
        "message": "explicit cause",
        "missing_cause": False,
        "alertable": False,
    }


@pytest.mark.parametrize(
    ("event_fields", "source", "message"),
    [
        ({"terminal_reason": {"reason": "reason cause"}}, "terminal_reason", "reason cause"),
        ({"error": "error cause", "message": "later"}, "error", "error cause"),
        ({"blocked_reason": "blocked cause"}, "blocked_reason", "blocked cause"),
        ({"blocker_reason": "blocker cause"}, "blocker_reason", "blocker cause"),
        ({"evidence": {"message": "evidence cause"}}, "evidence", "evidence cause"),
        ({"evidence": {"summary": "summary cause"}}, "evidence", "summary cause"),
        ({"evidence": {"reason": "evidence reason"}}, "evidence", "evidence reason"),
        ({"message": "top-level cause"}, "message", "top-level cause"),
    ],
)
def test_failure_cause_priority_and_source_are_deterministic(
    tmp_path: Path,
    event_fields: dict[str, object],
    source: str,
    message: str,
) -> None:
    path = tmp_path / f"{source}-{message}.jsonl"
    event = {"request_id": message, "state": "worker_failed", **event_fields}

    process_event_ledger.append_event(path, event)
    reason = list(process_event_ledger.iter_events(path))[0]["terminal_reason"]

    assert reason["source"] == source
    assert reason["message"] == message
    assert reason["code"] == "worker_failed"
    assert reason["taxonomy"] == "lifecycle_terminal_failure"


@pytest.mark.parametrize(
    "terminal_reason",
    [
        None,
        "caller text",
        {"code": "safe_but_ignored", "taxonomy": "safe", "source": "safe"},
        {"message": {"nested": "not scalar"}, "reason": ["also", "nested"]},
        {"message": ""},
    ],
)
def test_causeless_failure_forces_observability_alert(
    tmp_path: Path, terminal_reason: object
) -> None:
    path = tmp_path / "missing.jsonl"
    event = {
        "request_id": "missing",
        "state": "finalize_failed",
        "terminal_reason": terminal_reason,
        "error": {"nested": "ignored"},
        "evidence": [{"message": "not recursively inspected"}],
        "message": False,
    }

    process_event_ledger.append_event(path, event)
    reason = list(process_event_ledger.iter_events(path))[0]["terminal_reason"]

    assert reason == {
        "code": "terminal_reason_missing",
        "taxonomy": "observability_missing_cause",
        "source": "append_event",
        "message": "terminal failure has no supported scalar cause",
        "missing_cause": True,
        "alertable": True,
    }


def test_failure_reason_bounds_message_and_alertable_type(tmp_path: Path) -> None:
    path = tmp_path / "bounded.jsonl"
    process_event_ledger.append_event(
        path,
        {
            "request_id": "bounded",
            "state": "scope_rejected",
            "terminal_reason": {
                "message": "x" * 20_000,
                "alertable": 1,
                "code": "a" * 20_000,
                "taxonomy": "b" * 20_000,
                "source": "c" * 20_000,
                "nested": {"raw": "never copied"},
            },
        },
    )
    reason = list(process_event_ledger.iter_events(path))[0]["terminal_reason"]

    assert set(reason) == {
        "code",
        "taxonomy",
        "source",
        "message",
        "missing_cause",
        "alertable",
    }
    assert reason["message"] == "x" * 512
    assert reason["alertable"] is True


@pytest.mark.parametrize("state", ["starting", "running", "finished"])
def test_non_failure_events_remain_value_equivalent(tmp_path: Path, state: str) -> None:
    path = tmp_path / f"{state}.jsonl"
    event = {
        "request_id": state,
        "state": state,
        "terminal_reason": {"arbitrary": {"value": "unchanged"}},
    }
    process_event_ledger.append_event(path, event)
    assert list(process_event_ledger.iter_events(path)) == [event]


def test_canonical_reason_survives_rotation_spill_and_latest_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "process_events.jsonl"
    first = {
        "request_id": "rotated",
        "state": "validation_failed",
        "error": "rotation cause",
        "payload": "x" * 600,
        "timestamp": "2026-08-21T00:00:00+00:00",
    }
    process_event_ledger.append_event(path, first, max_active_bytes=1024)
    process_event_ledger.append_event(
        path,
        {
            "request_id": "active",
            "state": "worker_failed",
            "blocked_reason": "active cause",
            "payload": "y" * 600,
            "timestamp": "2026-08-21T00:00:01+00:00",
        },
        max_active_bytes=1024,
    )

    @contextmanager
    def timed_out_lock(_path: Path):
        raise TimeoutError("locked")
        yield

    monkeypatch.setattr(process_event_ledger, "_append_lock", timed_out_lock)
    process_event_ledger.append_event(
        path,
        {
            "request_id": "spill",
            "state": "process_lost",
            "message": "spill cause",
            "timestamp": "2026-08-21T00:00:02+00:00",
        },
        max_active_bytes=1024,
    )

    rows = list(process_event_ledger.iter_events(path))
    assert len(process_event_ledger.ledger_paths(path)) == 3
    assert [row["terminal_reason"]["source"] for row in rows] == [
        "error",
        "blocked_reason",
        "message",
    ]
    latest = process_event_ledger.latest_events(path)
    assert latest["rotated"]["terminal_reason"] == rows[0]["terminal_reason"]
    assert latest["active"]["terminal_reason"] == rows[1]["terminal_reason"]
    assert latest["spill"]["terminal_reason"] == rows[2]["terminal_reason"]


def test_caller_dict_terminal_reason_preserved_bounded_in_raw_side_field(
    tmp_path: Path,
) -> None:
    path = tmp_path / "raw-dict.jsonl"
    huge_key = "k" * 5_000
    event = {
        "request_id": "raw-dict",
        "state": "worker_failed",
        "error": "primary cause",
        "terminal_reason": {
            "code": "caller_code",
            huge_key: "x" * 5_000,
            "nested": {"secret": "must not survive"},
            "listy": ["also", "dropped"],
            "flag": True,
        },
    }
    original = deepcopy(event)

    process_event_ledger.append_event(path, event)
    persisted = list(process_event_ledger.iter_events(path))[0]

    assert event == original
    assert persisted["terminal_reason"]["source"] == "error"
    assert set(persisted["terminal_reason"]) == {
        "code",
        "taxonomy",
        "source",
        "message",
        "missing_cause",
        "alertable",
    }
    raw = persisted["terminal_reason_raw"]
    assert raw["code"] == "caller_code"
    assert raw["flag"] is True
    assert raw[huge_key[:512]] == "x" * 512
    assert "nested" not in raw
    assert "listy" not in raw
    assert all(len(key) <= 512 for key in raw)


def test_raw_side_field_bounds_dict_key_count(tmp_path: Path) -> None:
    path = tmp_path / "raw-keys.jsonl"
    reason = {f"k{index:03d}": index for index in range(100)}
    process_event_ledger.append_event(
        path,
        {
            "request_id": "raw-keys",
            "state": "blocked",
            "error": "cause",
            "terminal_reason": reason,
        },
    )
    raw = list(process_event_ledger.iter_events(path))[0]["terminal_reason_raw"]
    assert len(raw) == 16


def test_raw_side_field_drops_hostile_key_without_stringifying(tmp_path: Path) -> None:
    path = tmp_path / "raw-hostile-key.jsonl"

    class _HostileKey:
        def __hash__(self) -> int:
            return 0

        def __str__(self) -> str:
            raise RuntimeError("hostile __str__ must never be invoked")

        __repr__ = __str__

    hostile = _HostileKey()
    event = {
        "request_id": "raw-hostile-key",
        "state": "worker_failed",
        "error": "primary cause",
        "terminal_reason": {hostile: "dropped without coercion", "safe": "kept"},
    }

    # A caller key whose __str__/__repr__ raises must never crash or stall
    # append_event: the record still persists, the hostile key is dropped
    # without being stringified, and only the already-``str`` key survives.
    process_event_ledger.append_event(path, event)
    persisted = list(process_event_ledger.iter_events(path))[0]

    assert persisted["terminal_reason"]["source"] == "error"
    assert persisted["terminal_reason_raw"] == {"safe": "kept"}
    assert hostile in event["terminal_reason"]


def test_non_dict_caller_terminal_reason_preserved_bounded(tmp_path: Path) -> None:
    path = tmp_path / "raw-str.jsonl"
    process_event_ledger.append_event(
        path,
        {
            "request_id": "raw-str",
            "state": "timed_out",
            "error": "real cause",
            "terminal_reason": "c" * 20_000,
        },
    )
    persisted = list(process_event_ledger.iter_events(path))[0]
    assert persisted["terminal_reason"]["source"] == "error"
    assert persisted["terminal_reason_raw"] == "c" * 512


def test_causeless_conflicting_reason_forces_canonical_yet_preserves_raw(
    tmp_path: Path,
) -> None:
    path = tmp_path / "raw-conflict.jsonl"
    process_event_ledger.append_event(
        path,
        {
            "request_id": "raw-conflict",
            "state": "finalize_failed",
            "terminal_reason": {
                "code": "caller_override",
                "taxonomy": "caller_taxonomy",
                "note": "no scalar cause present",
            },
        },
    )
    persisted = list(process_event_ledger.iter_events(path))[0]
    assert persisted["terminal_reason"] == {
        "code": "terminal_reason_missing",
        "taxonomy": "observability_missing_cause",
        "source": "append_event",
        "message": "terminal failure has no supported scalar cause",
        "missing_cause": True,
        "alertable": True,
    }
    assert persisted["terminal_reason_raw"] == {
        "code": "caller_override",
        "taxonomy": "caller_taxonomy",
        "note": "no scalar cause present",
    }


def test_non_failure_event_receives_no_raw_side_field(tmp_path: Path) -> None:
    path = tmp_path / "raw-nonfailure.jsonl"
    event = {
        "request_id": "raw-nonfailure",
        "state": "running",
        "terminal_reason": "caller text",
    }
    process_event_ledger.append_event(path, event)
    persisted = list(process_event_ledger.iter_events(path))[0]
    assert persisted == event
    assert "terminal_reason_raw" not in persisted


def test_raw_side_field_survives_rotation_spill_and_latest_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "process_events.jsonl"
    process_event_ledger.append_event(
        path,
        {
            "request_id": "rotated",
            "state": "validation_failed",
            "error": "rotation cause",
            "terminal_reason": "caller-rotated",
            "payload": "x" * 600,
            "timestamp": "2026-08-21T00:00:00+00:00",
        },
        max_active_bytes=1024,
    )
    process_event_ledger.append_event(
        path,
        {
            "request_id": "active",
            "state": "worker_failed",
            "terminal_reason": {"code": "caller", "message": "active cause"},
            "payload": "y" * 600,
            "timestamp": "2026-08-21T00:00:01+00:00",
        },
        max_active_bytes=1024,
    )

    @contextmanager
    def timed_out_lock(_path: Path):
        raise TimeoutError("locked")
        yield

    monkeypatch.setattr(process_event_ledger, "_append_lock", timed_out_lock)
    process_event_ledger.append_event(
        path,
        {
            "request_id": "spill",
            "state": "process_lost",
            "terminal_reason": {"reason": "spill cause", "extra": {"drop": "me"}},
            "timestamp": "2026-08-21T00:00:02+00:00",
        },
        max_active_bytes=1024,
    )

    rows = list(process_event_ledger.iter_events(path))
    assert len(process_event_ledger.ledger_paths(path)) == 3
    assert rows[0]["terminal_reason_raw"] == "caller-rotated"
    assert rows[1]["terminal_reason_raw"] == {
        "code": "caller",
        "message": "active cause",
    }
    assert rows[2]["terminal_reason_raw"] == {"reason": "spill cause"}
    latest = process_event_ledger.latest_events(path)
    assert latest["rotated"]["terminal_reason_raw"] == "caller-rotated"
    assert latest["active"]["terminal_reason_raw"] == rows[1]["terminal_reason_raw"]
    assert latest["spill"]["terminal_reason_raw"] == rows[2]["terminal_reason_raw"]
