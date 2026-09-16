from __future__ import annotations

import json
import os
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


@pytest.mark.parametrize(
    "prefix",
    [
        "validation_unsupported_in_sandbox:",
        "unsupported_sandbox_backend:",
        "validation_exec_scratch_unavailable:",
        "validation_executable_unavailable:",
        "validation_pytest_runtime_unavailable:",
        "validation_pytest_runtime_missing_pytest:",
    ],
)
def test_named_environment_prefix_classifies_environment_unsupported(
    tmp_path: Path, prefix: str
) -> None:
    path = tmp_path / "process_events.jsonl"
    process_event_ledger.append_event(
        path,
        {
            "request_id": "env",
            "state": "validation_failed",
            "error": prefix + "landlock denies os.chmod on the ledger dir",
        },
    )

    persisted = list(process_event_ledger.iter_events(path))[0]
    reason = persisted["terminal_reason"]
    assert reason["code"] == "validation_unsupported_in_sandbox"
    assert reason["taxonomy"] == "validation_environment_unsupported"
    assert reason["source"] == "error"
    assert reason["message"].startswith(prefix)
    assert reason["missing_cause"] is False
    assert reason["alertable"] is True


def test_generic_permission_prose_is_not_promoted_to_environment_unsupported(
    tmp_path: Path,
) -> None:
    # Tests may assert chmod/landlock authority on purpose: substring prose
    # without an approved named prefix stays an ordinary candidate failure.
    path = tmp_path / "process_events.jsonl"
    for request_id, error in (
        ("perm", "PermissionError: [Errno 1] Operation not permitted: fchmod"),
        ("chmod", "test asserts chmod authority: landlock permission denied"),
        ("midfix", "wrapped validation_unsupported_in_sandbox: not a prefix"),
    ):
        process_event_ledger.append_event(
            path,
            {"request_id": request_id, "state": "validation_failed", "error": error},
        )

    for persisted in process_event_ledger.iter_events(path):
        reason = persisted["terminal_reason"]
        assert reason["code"] == "validation_failed"
        assert reason["taxonomy"] == "lifecycle_terminal_failure"


def test_environment_prefix_respects_supplied_alertable_and_missing_cause_parity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "process_events.jsonl"
    process_event_ledger.append_event(
        path,
        {
            "request_id": "env-alert",
            "state": "blocked",
            "terminal_reason": {
                "message": "unsupported_sandbox_backend: seatbelt missing",
                "alertable": False,
            },
        },
    )
    process_event_ledger.append_event(
        path,
        {"request_id": "causeless", "state": "worker_failed"},
    )

    rows = {row["request_id"]: row for row in process_event_ledger.iter_events(path)}
    env = rows["env-alert"]["terminal_reason"]
    assert env["code"] == "validation_unsupported_in_sandbox"
    assert env["taxonomy"] == "validation_environment_unsupported"
    assert env["source"] == "terminal_reason"
    assert env["alertable"] is False
    causeless = rows["causeless"]["terminal_reason"]
    assert causeless["code"] == "terminal_reason_missing"
    assert causeless["taxonomy"] == "observability_missing_cause"
    assert causeless["missing_cause"] is True


def test_huge_integer_terminal_reason_is_replaced_not_serialized(
    tmp_path: Path,
) -> None:
    # json.dumps raises ValueError above the int-to-str digit limit, so a huge
    # caller integer must never reach serialization or the failure event is
    # lost entirely.
    path = tmp_path / "process_events.jsonl"
    process_event_ledger.append_event(
        path,
        {
            "request_id": "huge-nondict",
            "state": "worker_failed",
            "error": "real cause",
            "terminal_reason": 10**5000,
        },
    )
    process_event_ledger.append_event(
        path,
        {
            "request_id": "huge-dictvalue",
            "state": "validation_failed",
            "error": "real cause",
            "terminal_reason": {"code": "x", "big": 10**5000, "small": 7},
        },
    )

    rows = {row["request_id"]: row for row in process_event_ledger.iter_events(path)}
    assert rows["huge-nondict"]["terminal_reason_raw"] == "int_out_of_bounds"
    raw = rows["huge-dictvalue"]["terminal_reason_raw"]
    assert raw["big"] == "int_out_of_bounds"
    assert raw["small"] == 7
    assert raw["code"] == "x"


# --- Bounded multi-request event projection (NF-853) -------------------------
#
# These build ledger bytes directly rather than through ``append_event``: the
# projection under test is a reader, and writing the JSONL keeps each test to
# exactly the append/rotate/truncate/replace shape it is asserting about.


def _write_rows(path: Path, rows: list[dict[str, object]], *, mode: str = "a") -> None:
    with path.open(mode, encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _count_full_passes(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count canonical whole-ledger replays, not wall-clock time."""

    calls = {"count": 0}
    original = process_event_ledger.iter_events

    def counting(path: Path):
        calls["count"] += 1
        return original(path)

    monkeypatch.setattr(process_event_ledger, "iter_events", counting)
    return calls


def _event(request_id: str, index: int, **extra: object) -> dict[str, object]:
    row = {"request_id": request_id, "state": "running", "seq": index}
    row.update(extra)
    return row


def test_events_for_requests_projects_every_key_from_one_ledger_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_event_ledger.reset_request_event_cache()
    path = tmp_path / "process_events.jsonl"
    _write_rows(
        path,
        [
            _event("a", 0),
            _event("b", 0),
            _event("a", 1),
            _event("c", 0),
            _event("a", 2),
        ],
        mode="w",
    )

    passes = _count_full_passes(monkeypatch)
    projection = process_event_ledger.events_for_requests(path, ["a", "b", "absent"])

    assert passes["count"] == 1
    assert [row["seq"] for row in projection["a"]] == [0, 1, 2]
    assert [row["seq"] for row in projection["b"]] == [0]
    # Present-but-empty, never missing: a caller must not have to guess whether
    # a key was unasked or simply has no events.
    assert projection["absent"] == []


def test_appending_one_request_does_not_rebuild_unrelated_projections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_event_ledger.reset_request_event_cache()
    path = tmp_path / "process_events.jsonl"
    _write_rows(path, [_event("a", 0), _event("b", 0)], mode="w")

    passes = _count_full_passes(monkeypatch)
    first = process_event_ledger.events_for_requests(path, ["a", "b"])
    assert passes["count"] == 1
    assert [row["seq"] for row in first["a"]] == [0]

    # The NF-853 shape: a reviewer launch appends for ONE request, and the old
    # whole-ledger fingerprint cache then threw away every other request too.
    _write_rows(path, [_event("a", 1)])
    second = process_event_ledger.events_for_requests(path, ["a", "b"])

    assert passes["count"] == 1
    assert [row["seq"] for row in second["a"]] == [0, 1]
    assert [row["seq"] for row in second["b"]] == [0]


def test_synthetic_review_backlog_drains_without_per_action_full_ledger_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured 22-action case: one pass total, not one pass per action."""

    process_event_ledger.reset_request_event_cache()
    path = tmp_path / "process_events.jsonl"
    targets = [f"request-{index:02d}" for index in range(22)]
    _write_rows(path, [_event(target, 0) for target in targets], mode="w")

    passes = _count_full_passes(monkeypatch)
    primed = process_event_ledger.events_for_requests(path, targets)
    assert passes["count"] == 1
    assert len(primed) == 22

    for index, target in enumerate(targets):
        # Each action appends its own launch evidence and then reads back the
        # request it just acted on, exactly as the drain does.
        _write_rows(path, [_event(target, 1, action_index=index)])
        seen = process_event_ledger.events_for_requests(path, [target])
        assert [row["seq"] for row in seen[target]] == [0, 1]

    assert passes["count"] == 1
    final = process_event_ledger.events_for_requests(path, targets)
    assert passes["count"] == 1
    assert all(len(rows) == 2 for rows in final.values())


def test_request_projection_never_borrows_another_requests_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_event_ledger.reset_request_event_cache()
    path = tmp_path / "process_events.jsonl"
    _write_rows(
        path,
        [
            _event("req", 0),
            _event("req-successor", 0),
            {"request_id": 123, "state": "running", "seq": 0},
            {"state": "running", "seq": 0},
        ],
        mode="w",
    )

    passes = _count_full_passes(monkeypatch)
    projection = process_event_ledger.events_for_requests(
        path, ["req", "req-successor", "123"]
    )

    assert passes["count"] == 1
    assert [row["seq"] for row in projection["req"]] == [0]
    assert [row["seq"] for row in projection["req-successor"]] == [0]
    # A non-string ledger id is not coerced: 123 must never answer for "123".
    assert projection["123"] == []


def test_projection_identity_includes_the_key_field(
    tmp_path: Path
) -> None:
    process_event_ledger.reset_request_event_cache()
    path = tmp_path / "process_events.jsonl"
    _write_rows(
        path,
        [{"request_id": "shared", "task_id": "shared", "seq": 0, "who": "request"}],
        mode="w",
    )

    by_request = process_event_ledger.events_for_requests(path, ["shared"])
    by_task = process_event_ledger.events_for_requests(
        path, ["shared"], key_field="task_id"
    )
    assert by_request["shared"] == by_task["shared"]

    # A row that belongs to only one of the two projections must not be served
    # from the other's retained answer.
    _write_rows(path, [{"task_id": "shared", "seq": 1, "who": "task"}])
    assert len(process_event_ledger.events_for_requests(path, ["shared"])["shared"]) == 1
    assert (
        len(
            process_event_ledger.events_for_requests(
                path, ["shared"], key_field="task_id"
            )["shared"]
        )
        == 2
    )


def test_incomplete_trailing_row_is_projected_only_once_complete(
    tmp_path: Path
) -> None:
    process_event_ledger.reset_request_event_cache()
    path = tmp_path / "process_events.jsonl"
    _write_rows(path, [_event("a", 0)], mode="w")
    process_event_ledger.events_for_requests(path, ["a"])

    partial = json.dumps(_event("a", 1), sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(partial[:-3])
    assert [
        row["seq"] for row in process_event_ledger.events_for_requests(path, ["a"])["a"]
    ] == [0]

    with path.open("a", encoding="utf-8") as handle:
        handle.write(partial[-3:] + "\n")
    assert [
        row["seq"] for row in process_event_ledger.events_for_requests(path, ["a"])["a"]
    ] == [0, 1]


def test_truncation_replacement_and_rotation_replay_the_canonical_ordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_event_ledger.reset_request_event_cache()
    path = tmp_path / "process_events.jsonl"
    _write_rows(path, [_event("a", 0), _event("a", 1)], mode="w")

    passes = _count_full_passes(monkeypatch)
    assert len(process_event_ledger.events_for_requests(path, ["a"])["a"]) == 2
    assert passes["count"] == 1

    # Truncation: the active file shrank, so folded rows are not extendable.
    os.truncate(path, path.stat().st_size - len(json.dumps(_event("a", 1))))
    assert len(process_event_ledger.events_for_requests(path, ["a"])["a"]) == 1
    assert passes["count"] == 2

    # Replacement: same path, different inode.
    replacement = tmp_path / "replacement.jsonl"
    _write_rows(replacement, [_event("a", 7), _event("a", 8), _event("a", 9)], mode="w")
    os.replace(replacement, path)
    assert [
        row["seq"] for row in process_event_ledger.events_for_requests(path, ["a"])["a"]
    ] == [7, 8, 9]
    assert passes["count"] == 3

    # Rotation: the active file became an immutable archive behind a new active.
    archive = path.with_name("process_events.20260915T000000.000000Z.1.abcdef01.jsonl")
    os.replace(path, archive)
    _write_rows(path, [_event("a", 10)], mode="w")
    assert [
        row["seq"] for row in process_event_ledger.events_for_requests(path, ["a"])["a"]
    ] == [7, 8, 9, 10]
    assert passes["count"] == 4


def test_untracked_key_is_rebuilt_rather_than_reported_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_event_ledger.reset_request_event_cache()
    path = tmp_path / "process_events.jsonl"
    _write_rows(path, [_event("a", 0), _event("b", 0)], mode="w")

    passes = _count_full_passes(monkeypatch)
    assert process_event_ledger.events_for_requests(path, ["a"])["a"]
    assert passes["count"] == 1

    # "b" was never tracked. Serving it from the retained projection would
    # report an empty history for a request that has one.
    widened = process_event_ledger.events_for_requests(path, ["b"])
    assert passes["count"] == 2
    assert [row["seq"] for row in widened["b"]] == [0]
    # The earlier key is retained alongside it, so neither costs a third pass.
    both = process_event_ledger.events_for_requests(path, ["a", "b"])
    assert passes["count"] == 2
    assert both["a"] and both["b"]


def test_over_bound_batch_answers_in_one_pass_without_evicting_the_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_event_ledger.reset_request_event_cache()
    path = tmp_path / "process_events.jsonl"
    oversized = [
        f"request-{index:03d}"
        for index in range(process_event_ledger._REQUEST_EVENT_PROJECTION_MAX_KEYS + 5)
    ]
    _write_rows(path, [_event(target, 0) for target in oversized], mode="w")

    passes = _count_full_passes(monkeypatch)
    projection = process_event_ledger.events_for_requests(path, oversized)
    # One pass, every key answered -- never one pass per key, and never a
    # silently truncated request.
    assert passes["count"] == 1
    assert sorted(projection) == sorted(oversized)
    assert all(len(rows) == 1 for rows in projection.values())


def test_returned_rows_cannot_mutate_the_retained_projection(
    tmp_path: Path
) -> None:
    process_event_ledger.reset_request_event_cache()
    path = tmp_path / "process_events.jsonl"
    _write_rows(path, [_event("a", 0)], mode="w")

    first = process_event_ledger.events_for_requests(path, ["a"])
    first["a"][0]["state"] = "tampered"
    first["a"].append(_event("a", 99))

    second = process_event_ledger.events_for_requests(path, ["a"])
    assert [row["seq"] for row in second["a"]] == [0]
    assert second["a"][0]["state"] == "running"


def test_returned_nested_values_cannot_mutate_the_retained_projection(
    tmp_path: Path
) -> None:
    """Isolation has to go all the way down, not one level.

    ``dict(row)`` copies the row but not what the row points at, so a caller
    editing ``row["detail"]`` reached straight into the retained projection and
    every later reader was served that edit back as ledger truth.  Events carry
    nested payloads routinely, so this is the ordinary case, not an exotic one.
    """

    process_event_ledger.reset_request_event_cache()
    path = tmp_path / "process_events.jsonl"
    _write_rows(
        path,
        [_event("a", 0, detail={"phase": "security", "lenses": ["x", "y"]})],
        mode="w",
    )

    first = process_event_ledger.events_for_requests(path, ["a"])
    first["a"][0]["detail"]["phase"] = "tampered"
    first["a"][0]["detail"]["lenses"].append("injected")

    second = process_event_ledger.events_for_requests(path, ["a"])
    assert second["a"][0]["detail"] == {"phase": "security", "lenses": ["x", "y"]}
    # Each read owns its own nested containers, so one caller's edit is not a
    # later caller's input.
    assert first["a"][0]["detail"] is not second["a"][0]["detail"]
