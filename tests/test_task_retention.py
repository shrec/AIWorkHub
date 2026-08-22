from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

import pytest

from aiworkhub import process_event_ledger, process_launcher, task_retention, task_store


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    return repo


def _write_request_process_authority(
    repo: Path,
    request_id: str,
    *,
    pid: int | None = None,
    ticks: int | None = None,
    metadata: Mapping[str, Any] | None = None,
    supervisor: Mapping[str, Any] | str | None = None,
) -> None:
    log_path = repo / process_launcher.PROCESS_LOG_DEFAULT_REL
    log_path.parent.mkdir(parents=True, exist_ok=True)
    event: dict[str, Any] = {
        "request_id": request_id,
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    if pid is not None:
        event["pid"] = pid
    if ticks is not None:
        event["pid_start_ticks"] = ticks
    if metadata is not None:
        meta_path = log_path.parent / f"{request_id}.metadata.json"
        meta_path.write_text(json.dumps(dict(metadata)), encoding="utf-8")
        event["metadata_path"] = str(meta_path)
    if supervisor is not None:
        status_path = log_path.parent / f"{request_id}.supervisor.json"
        if isinstance(supervisor, str):
            status_path.write_text(supervisor, encoding="utf-8")
        else:
            status_path.write_text(json.dumps(dict(supervisor)), encoding="utf-8")
            status_path.chmod(0o600)
        event["supervisor_status_path"] = str(status_path)
    process_event_ledger.append_event(log_path, event)


def _archived(repo: Path, task_id: str, *, callback_state: str = "delivered") -> None:
    db = task_store.canonical_db_path(repo)
    timestamp = "2025-01-01T00:00:00+00:00"
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,created_at,updated_at,completed_at,origin_thread_id,archived_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                "runner",
                "topic",
                "finished",
                "done",
                json.dumps({"task_id": task_id, "archived_at": timestamp}),
                timestamp,
                timestamp,
                timestamp,
                "thread",
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) VALUES(?,?,?,?,?)",
            (task_id, "archived", "manager", "{}", timestamp),
        )
        connection.execute(
            "INSERT INTO callback_outbox(task_id,origin_thread_id,state,created_at,updated_at) VALUES(?,?,?,?,?)",
            (task_id, "thread", callback_state, timestamp, timestamp),
        )
        connection.commit()
    finally:
        connection.close()


def test_preview_only_selects_old_archived_tasks_with_terminal_callbacks(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _archived(repo, "TASK_READY")
    _archived(repo, "TASK_CALLBACK_PENDING", callback_state="pending")

    result = task_retention.preview(repo, older_than_days=30)

    assert result["candidate_count"] == 1
    assert result["candidates"][0]["task_id"] == "TASK_READY"
    assert result["protected_callback_count"] == 1


def test_quarantine_restore_roundtrip_preserves_task_events_and_callback(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _archived(repo, "TASK_ROUNDTRIP")
    preview = task_retention.preview(repo, older_than_days=30)

    moved = task_retention.quarantine(
        repo,
        preview_digest=preview["preview_digest"],
        older_than_days=30,
        confirm=True,
    )

    assert moved["quarantined"] == 1
    assert task_store.get_task(repo, "TASK_ROUNDTRIP") is None
    batch = task_retention.list_batches(repo)["batches"][0]
    assert batch["task_count"] == 1
    assert batch["purge_eligible"] is False

    restored = task_retention.restore(repo, batch_id=moved["batch_id"], confirm=True)
    assert restored["restored"] == 1
    assert task_store.get_task(repo, "TASK_ROUNDTRIP")["task_id"] == "TASK_ROUNDTRIP"
    assert task_store.get_task_events(repo, "TASK_ROUNDTRIP")[0]["event"] == "archived"
    assert task_retention.list_batches(repo)["batches"][0]["purge_eligible"] is True
    connection = sqlite3.connect(task_store.canonical_db_path(repo))
    try:
        events = [
            row[0]
            for row in connection.execute(
                "SELECT event FROM task_retention_audit WHERE batch_id=? ORDER BY audit_id",
                (moved["batch_id"],),
            )
        ]
    finally:
        connection.close()
    assert events == ["quarantined", "restored"]


def test_quarantine_is_preview_bound_and_purge_respects_undo_window(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _archived(repo, "TASK_SAFE")
    preview = task_retention.preview(repo, older_than_days=30)

    with pytest.raises(task_retention.TaskRetentionError, match="preview_changed"):
        task_retention.quarantine(
            repo,
            preview_digest="0" * 64,
            older_than_days=30,
            confirm=True,
        )

    moved = task_retention.quarantine(
        repo,
        preview_digest=preview["preview_digest"],
        older_than_days=30,
        confirm=True,
    )
    with pytest.raises(task_retention.TaskRetentionError, match="undo_window_active"):
        task_retention.purge(repo, batch_id=moved["batch_id"], confirm=True)


def test_confirmation_and_age_bounds_fail_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _archived(repo, "TASK_BOUNDS")
    with pytest.raises(task_retention.TaskRetentionError, match="days_out_of_range"):
        task_retention.preview(repo, older_than_days=1)
    preview = task_retention.preview(repo, older_than_days=30)
    with pytest.raises(task_retention.TaskRetentionError, match="confirmation_required"):
        task_retention.quarantine(
            repo,
            preview_digest=preview["preview_digest"],
            older_than_days=30,
        )


def test_validate_accepted_cleanup_evidence_fail_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _archived(repo, "TASK_ARCHIVED")
    db = task_store.canonical_db_path(repo)
    connection = sqlite3.connect(db)
    try:
        now = "2026-01-01T00:00:00+00:00"
        connection.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                "TASK_FINISHED",
                "runner",
                "topic",
                "finished",
                "done",
                json.dumps({"accepted_request_id": "req-finished"}),
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                "TASK_LIVE",
                "runner",
                "topic",
                "processing",
                "claimed",
                json.dumps({"launch_request_id": "req-live"}),
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    digest = task_retention.canonical_acceptance_digest(
        accept_evidence={},
        accepted_request_id="req-finished",
        predecessor_request_id="",
        request_id="req-finished",
        status="finished",
        task_id="TASK_FINISHED",
    )
    good = {
        "schema_id": task_retention.ACCEPTED_CLEANUP_EVIDENCE_SCHEMA,
        "task_id": "TASK_FINISHED",
        "request_id": "req-finished",
        "predecessor_request_id": "",
        "canonical_digest": digest,
    }
    absent_ledger = task_retention.validate_accepted_cleanup_evidence(repo, good)
    assert absent_ledger["ok"] is False
    assert absent_ledger["reason"] == "ambiguous_ownership"
    assert absent_ledger["deleted"] is False
    _write_request_process_authority(repo, "req-finished", pid=1_000_000_001, ticks=1)
    assert task_retention.validate_accepted_cleanup_evidence(repo, good)["ok"] is True
    assert task_retention.validate_accepted_cleanup_evidence(repo, None)["reason"] == "unknown_identity"
    assert (
        task_retention.validate_accepted_cleanup_evidence(
            repo,
            {**good, "task_id": "TASK_MISSING"},
        )["reason"]
        == "unresolved_task"
    )
    live = task_retention.validate_accepted_cleanup_evidence(
        repo,
        {
            "schema_id": task_retention.ACCEPTED_CLEANUP_EVIDENCE_SCHEMA,
            "task_id": "TASK_LIVE",
            "request_id": "req-live",
            "canonical_digest": "a" * 64,
        },
    )
    assert live["reason"] == "live_process"
    assert live["deleted"] is False


def test_live_process_holders_fail_closed_unreadable_supervisor(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    db = task_store.canonical_db_path(repo)
    now = "2026-01-01T00:00:00+00:00"
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                "TASK_CLAIMED",
                "runner",
                "topic",
                "pending",
                "claimed",
                "{not-json",
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    holders, verified = task_retention.live_process_holders(repo, "req-any")
    assert verified is False
    assert holders == []
    claimed = task_retention.validate_accepted_cleanup_evidence(
        repo,
        {
            "schema_id": task_retention.ACCEPTED_CLEANUP_EVIDENCE_SCHEMA,
            "task_id": "TASK_CLAIMED",
            "request_id": "req-any",
            "canonical_digest": "a" * 64,
        },
    )
    assert claimed["ok"] is False
    assert claimed["reason"] == "live_process"
    assert claimed["deleted"] is False


def test_live_process_holders_fail_closed_absent_unreadable_and_recycled(tmp_path: Path) -> None:
    import os

    repo = _repo(tmp_path)
    db = task_store.canonical_db_path(repo)
    now = "2026-01-01T00:00:00+00:00"
    live_pid = os.getpid()
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                "TASK_ABSENT",
                "runner",
                "topic",
                "finished",
                "unclaimed",
                json.dumps({"accepted_request_id": "req-absent"}),
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                "TASK_UNREADABLE",
                "runner",
                "topic",
                "finished",
                "unclaimed",
                json.dumps({"accepted_request_id": "req-unreadable"}),
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                "TASK_RECYCLED",
                "runner",
                "topic",
                "finished",
                "unclaimed",
                json.dumps({"accepted_request_id": "req-recycled"}),
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    _write_request_process_authority(repo, "req-absent", pid=live_pid)
    _write_request_process_authority(
        repo,
        "req-unreadable",
        supervisor="not-a-mapping",
    )
    _write_request_process_authority(
        repo,
        "req-recycled",
        supervisor={"child_pid": live_pid, "child_pid_start_ticks": 1},
    )

    absent_holders, absent_ok = task_retention.live_process_holders(repo, "req-absent")
    unreadable_holders, unreadable_ok = task_retention.live_process_holders(repo, "req-unreadable")
    recycled_holders, recycled_ok = task_retention.live_process_holders(repo, "req-recycled")
    none_holders, none_ok = task_retention.live_process_holders(repo, "req-no-ledger")
    assert absent_ok is False and absent_holders == []
    assert unreadable_ok is False and unreadable_holders == []
    assert recycled_ok is False and recycled_holders == []
    assert none_ok is False and none_holders == []


def _insert_cleanup_card(
    repo: Path,
    task_id: str,
    *,
    status: str,
    card_json: str,
) -> None:
    now = "2026-01-01T00:00:00+00:00"
    connection = sqlite3.connect(task_store.canonical_db_path(repo))
    try:
        connection.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (task_id, "runner", "topic", status, "unclaimed", card_json, now, now),
        )
        connection.commit()
    finally:
        connection.close()


def test_live_rework_references_fail_closed_unreadable_non_dict_live_cards(
    tmp_path: Path,
) -> None:
    (tmp_path / "unreadable").mkdir()
    unreadable = _repo(tmp_path / "unreadable")
    _insert_cleanup_card(
        unreadable,
        "TASK_PENDING_BAD",
        status="pending",
        card_json="{not-json",
    )
    pins, verified = task_retention.live_rework_references(unreadable, "pred-any")
    assert verified is False
    assert pins == []

    (tmp_path / "nondict").mkdir()
    non_dict = _repo(tmp_path / "nondict")
    _insert_cleanup_card(non_dict, "TASK_REVIEW_LIST", status="review", card_json="[]")
    pins, verified = task_retention.live_rework_references(non_dict, "pred-any")
    assert verified is False
    assert pins == []

    (tmp_path / "finished").mkdir()
    finished = _repo(tmp_path / "finished")
    _insert_cleanup_card(
        finished,
        "TASK_FINISHED_BAD",
        status="finished",
        card_json="{not-json",
    )
    pins, verified = task_retention.live_rework_references(finished, "pred-any")
    assert verified is True
    assert pins == []


def test_validate_accepted_cleanup_evidence_accepts_canonical_task_ids(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    colon_id = "needfix:NF-2026-00385"
    max_id = "T" + ("a" * 255)
    now = "2026-01-01T00:00:00+00:00"
    connection = sqlite3.connect(task_store.canonical_db_path(repo))
    try:
        for task_id, request_id in ((colon_id, "req-colon"), (max_id, "req-maxlen")):
            connection.execute(
                "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    "runner",
                    "topic",
                    "finished",
                    "done",
                    json.dumps({"accepted_request_id": request_id}),
                    now,
                    now,
                ),
            )
        connection.commit()
    finally:
        connection.close()
    _write_request_process_authority(repo, "req-colon", pid=1_000_000_001, ticks=1)
    _write_request_process_authority(repo, "req-maxlen", pid=1_000_000_002, ticks=1)

    for task_id, request_id in ((colon_id, "req-colon"), (max_id, "req-maxlen")):
        digest = task_retention.canonical_acceptance_digest(
            accept_evidence={},
            accepted_request_id=request_id,
            predecessor_request_id="",
            request_id=request_id,
            status="finished",
            task_id=task_id,
        )
        verdict = task_retention.validate_accepted_cleanup_evidence(
            repo,
            {
                "schema_id": task_retention.ACCEPTED_CLEANUP_EVIDENCE_SCHEMA,
                "task_id": task_id,
                "request_id": request_id,
                "predecessor_request_id": "",
                "canonical_digest": digest,
            },
        )
        assert verdict["ok"] is True
        assert verdict["task_id"] == task_id

    malformed = task_retention.validate_accepted_cleanup_evidence(
        repo,
        {
            "schema_id": task_retention.ACCEPTED_CLEANUP_EVIDENCE_SCHEMA,
            "task_id": "T" + ("a" * 256),
            "request_id": "req-colon",
            "predecessor_request_id": "",
            "canonical_digest": "a" * 64,
        },
    )
    assert malformed["ok"] is False
    assert malformed["reason"] == "unknown_identity"
    assert malformed["deleted"] is False
