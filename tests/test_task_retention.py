from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
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


def test_task_family_parser_is_explicit_and_deterministic() -> None:
    assert task_retention.task_family("AIWORKHUB_FEATURE_V4_CODEX56") == (
        "AIWORKHUB_FEATURE",
        (4, 0),
        False,
    )
    assert task_retention.task_family("needfix-NF-2026-00401-r4") == (
        "needfix-NF-2026-00401",
        (0, 4),
        False,
    )
    assert task_retention.task_family("AIWORKHUB_FEATURE_E12_CORRECTNESS_V9") == (
        "AIWORKHUB_FEATURE",
        (9, 12),
        True,
    )
    assert task_retention.task_family("AIWORKHUB_FEATURE_COMMON_PREFIX") is None
    assert task_retention.task_family("AIWORKHUB_FEATURE_V0") is None


def test_hygiene_config_fails_closed_and_is_same_day(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        task_retention.HYGIENE_TTL_ENV,
        task_retention.HYGIENE_INTERVAL_ENV,
        task_retention.HYGIENE_BATCH_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    config = task_retention.hygiene_config()
    assert 0 < config["interval_seconds"] <= 86_400
    assert 0 < config["ttl_seconds"] <= 86_400
    assert 0 < config["batch_size"] <= 100

    monkeypatch.setenv(task_retention.HYGIENE_TTL_ENV, "six-hours")
    with pytest.raises(task_retention.TaskRetentionError, match="invalid_config"):
        task_retention.hygiene_config()


def test_automatic_hygiene_keeps_head_and_uses_mocked_archive_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    old = "2026-08-31T00:00:00+00:00"
    rows = [
        {
            "task_id": "FAMILY_V1",
            "topic": "coding",
            "status": "blocked",
            "worker_status": "blocked",
            "updated_at": old,
            "completed_at": old,
            "card": {"task_id": "FAMILY_V1", "launch_request_id": "req-v1"},
        },
        {
            "task_id": "FAMILY_V2",
            "topic": "coding",
            "status": "blocked",
            "worker_status": "blocked",
            "updated_at": old,
            "completed_at": old,
            "card": {"task_id": "FAMILY_V2", "launch_request_id": "req-v2"},
        },
        {
            "task_id": "FAMILY_E1_SECURITY_V1",
            "topic": "quality_review",
            "status": "finished",
            "worker_status": "done",
            "updated_at": old,
            "completed_at": old,
            "card": {
                "task_id": "FAMILY_E1_SECURITY_V1",
                "topic": "quality_review",
                "launch_request_id": "req-review",
            },
        },
        {
            "task_id": "FAMILY_WITH_SHARED_PREFIX",
            "topic": "coding",
            "status": "blocked",
            "worker_status": "blocked",
            "updated_at": old,
            "completed_at": old,
            "card": {"task_id": "FAMILY_WITH_SHARED_PREFIX"},
        },
    ]
    archived: list[str] = []
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(task_retention, "_hygiene_rows", lambda _root: rows)
    monkeypatch.setattr(
        task_retention,
        "_final_archive_fence",
        lambda _root, task_id, _request_id: ({"task_id": task_id}, ""),
    )
    monkeypatch.setattr(
        task_store,
        "archive_task",
        lambda _root, task_id, **_kwargs: (archived.append(task_id) is None, "archived"),
    )

    result = task_retention.run_automatic_hygiene(
        repo, now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    )

    assert result["archived"] == 2
    assert archived == ["FAMILY_E1_SECURITY_V1", "FAMILY_V1"]
    assert "FAMILY_V2" not in archived
    assert "FAMILY_WITH_SHARED_PREFIX" not in archived


@pytest.mark.parametrize(
    ("card_delta", "ledger_delta", "expected_reason"),
    [
        ({"status": "processing", "worker_status": "running"}, {}, "task_live"),
        ({}, {"state": "starting"}, "ledger_live"),
        ({}, {"state": "running"}, "ledger_live"),
        ({"reservation_id": "held-1"}, {}, "task_reserved"),
        ({"retained_workspace": {"path": "/"}}, {}, "retained_evidence"),
        ({}, None, "ledger_missing"),
        ({}, {"task_id": "OTHER"}, "ledger_task_mismatch"),
        ({}, {"request_id": "other-request"}, "ledger_request_mismatch"),
        ({}, {"timestamp": "2025-12-31T23:59:59+00:00"}, "ledger_stale"),
    ],
)
def test_final_archive_fence_rejects_live_reserved_retained_and_bad_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    card_delta: dict[str, Any],
    ledger_delta: dict[str, Any] | None,
    expected_reason: str,
) -> None:
    repo = _repo(tmp_path)
    card: dict[str, Any] = {
        "task_id": "FAMILY_V1",
        "launch_request_id": "req-v1",
        "status": "blocked",
        "worker_status": "blocked",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "claimed_by": "stale-worker-attribution",
    }
    card.update(card_delta)
    ledger: dict[str, Any] | None = {
        "task_id": "FAMILY_V1",
        "request_id": "req-v1",
        "state": "worker_failed",
        "timestamp": "2026-01-01T00:00:01+00:00",
    }
    if ledger_delta is None:
        ledger = None
    elif ledger is not None:
        ledger.update(ledger_delta)
    monkeypatch.setattr(task_store, "get_task", lambda _root, _task_id: card)
    monkeypatch.setattr(
        task_retention, "_latest_process_row", lambda _root, _request: ledger
    )

    fenced, reason = task_retention._final_archive_fence(repo, "FAMILY_V1", "req-v1")

    assert fenced is None
    assert reason == expected_reason


def test_final_archive_fence_decision_is_unchanged_by_a_dropped_packet_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NF-2026-00637: ``_latest_process_row`` requests ``drop_fields=("packet",)``.

    A large ``packet`` field on the real ledger row must be stripped before it
    is retained in the cached projection, and ``_final_archive_fence``'s
    decision (fenced or not, and why) must be identical to what it would be
    without that field -- this exercises the real ledger file, not a mock of
    ``_latest_process_row``.
    """

    repo = _repo(tmp_path)
    card: dict[str, Any] = {
        "task_id": "FAMILY_V1",
        "launch_request_id": "req-v1",
        "status": "blocked",
        "worker_status": "blocked",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "claimed_by": "stale-worker-attribution",
    }
    monkeypatch.setattr(task_store, "get_task", lambda _root, _task_id: card)
    log_path = repo / process_launcher.PROCESS_LOG_DEFAULT_REL
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "task_id": "FAMILY_V1",
                    "request_id": "req-v1",
                    "state": "worker_failed",
                    "timestamp": "2026-01-01T00:00:01+00:00",
                    "packet": "x" * 8192,
                }
            )
            + "\n"
        )

    row = task_retention._latest_process_row(repo, "req-v1")
    assert row is not None and "packet" not in row

    fenced, reason = task_retention._final_archive_fence(repo, "FAMILY_V1", "req-v1")

    assert reason == ""
    assert fenced == card


@pytest.mark.parametrize("callback_state", ["pending", "inflight"])
def test_final_archive_fence_rejects_live_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, callback_state: str
) -> None:
    repo = _repo(tmp_path)
    card = {
        "task_id": "FAMILY_V1",
        "launch_request_id": "req-v1",
        "status": "blocked",
        "worker_status": "blocked",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    connection = sqlite3.connect(task_store.canonical_db_path(repo))
    try:
        connection.execute(
            "INSERT INTO callback_outbox(task_id,origin_thread_id,state,created_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            (
                "FAMILY_V1",
                "thread",
                callback_state,
                card["updated_at"],
                card["updated_at"],
            ),
        )
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setattr(task_store, "get_task", lambda _root, _task_id: card)

    fenced, reason = task_retention._final_archive_fence(repo, "FAMILY_V1", "req-v1")

    assert fenced is None
    assert reason == "callback_live"


def test_terminal_blocked_stale_claimed_by_reaches_mocked_archive_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    old = "2026-08-31T00:00:00+00:00"
    rows = [
        {
            "task_id": task_id,
            "topic": "coding",
            "status": "blocked",
            "worker_status": "blocked",
            "claimed_by": "old-worker",
            "updated_at": old,
            "completed_at": old,
            "card": {"task_id": task_id, "launch_request_id": request_id},
        }
        for task_id, request_id in (("FAMILY_V1", "req-v1"), ("FAMILY_V2", "req-v2"))
    ]
    canonical = {
        **rows[0]["card"],
        "status": "blocked",
        "worker_status": "blocked",
        "claimed_by": "old-worker",
        "updated_at": old,
    }
    archived: list[str] = []
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(task_retention, "_hygiene_rows", lambda _root: rows)
    monkeypatch.setattr(task_store, "get_task", lambda _root, _task_id: canonical)
    monkeypatch.setattr(
        task_retention,
        "_latest_process_row",
        lambda _root, _request: {
            "task_id": "FAMILY_V1",
            "request_id": "req-v1",
            "state": "worker_failed",
            "timestamp": "2026-08-31T00:00:01+00:00",
        },
    )
    monkeypatch.setattr(
        task_store,
        "archive_task",
        lambda _root, task_id, **_kwargs: (archived.append(task_id) is None, "archived"),
    )

    result = task_retention.run_automatic_hygiene(
        repo, now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    )

    assert result["archived"] == 1
    assert archived == ["FAMILY_V1"]


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


def test_hygiene_batch_budget_bounds_archives_not_examined_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bottleneck audit B4: 25 permanently fenced cards sorted first used to
    # occupy the whole batch on every run, so the remaining eligible cards were
    # never examined (measured eligible 263, archived 0, batch_limited 238).
    repo = _repo(tmp_path)
    old = "2026-01-01T00:00:00+00:00"
    rows = [
        {
            "task_id": f"GRAVE_V{index}",
            "status": "blocked",
            "worker_status": "blocked",
            "updated_at": old,
            "completed_at": old,
            "card": {"task_id": f"GRAVE_V{index}", "launch_request_id": f"req-{index}"},
        }
        for index in range(1, 32)
    ]
    fenced_ids = set(sorted(f"GRAVE_V{index}" for index in range(1, 31))[:25])
    archived: list[str] = []
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(task_retention, "_hygiene_rows", lambda _root: rows)
    monkeypatch.setattr(
        task_retention,
        "hygiene_config",
        lambda: {"ttl_seconds": 3600, "batch_size": 25, "reviewer_stale_grace_seconds": 60},
    )
    monkeypatch.setattr(
        task_retention,
        "_final_archive_fence",
        lambda _root, task_id, _request_id: (
            (None, "retained_evidence") if task_id in fenced_ids else ({"task_id": task_id}, "")
        ),
    )
    monkeypatch.setattr(
        task_store,
        "archive_task",
        lambda _root, task_id, **_kwargs: (archived.append(task_id) is None, "archived"),
    )

    result = task_retention.run_automatic_hygiene(
        repo, now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    )

    # GRAVE_V31 is the retained family head; the other 30 are eligible, the 25
    # fenced ones are skipped without consuming the budget, and the remaining
    # 5 are archived instead of being starved behind the fenced prefix.
    assert result["eligible"] == 30
    assert result["archived"] == 5
    assert result["reasons"]["retained_evidence"] == 25
    assert "batch_limited" not in result["reasons"]
    assert "GRAVE_V31" not in archived


def test_final_archive_fence_releases_retained_evidence_once_workspace_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bottleneck audit B4: evidence lives on disk, not on the card.  Once
    # storage retention has quarantined or purged the recorded workspace the
    # retained-evidence keys no longer protect anything and the terminal card
    # becomes archivable; a card with no recorded path stays fenced.
    repo = _repo(tmp_path)
    gone = tmp_path / "worktrees" / "req-v1" / "worktree"
    card: dict[str, Any] = {
        "task_id": "FAMILY_V1",
        "launch_request_id": "req-v1",
        "status": "blocked",
        "worker_status": "blocked",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "rework_predecessor": {"workspace": {"path": str(gone)}},
    }
    ledger = {
        "task_id": "FAMILY_V1",
        "request_id": "req-v1",
        "state": "worker_failed",
        "timestamp": "2026-01-01T00:00:01+00:00",
    }
    monkeypatch.setattr(task_store, "get_task", lambda _root, _task_id: card)
    monkeypatch.setattr(
        task_retention, "_latest_process_row", lambda _root, _request: ledger
    )

    fenced, reason = task_retention._final_archive_fence(repo, "FAMILY_V1", "req-v1")
    assert fenced is not None and reason == ""

    gone.mkdir(parents=True)
    fenced, reason = task_retention._final_archive_fence(repo, "FAMILY_V1", "req-v1")
    assert fenced is None and reason == "retained_evidence"

    pathless = dict(card)
    pathless["rework_predecessor"] = {"workspace": {"allowed_writes": ["a.py"]}}
    monkeypatch.setattr(task_store, "get_task", lambda _root, _task_id: pathless)
    fenced, reason = task_retention._final_archive_fence(repo, "FAMILY_V1", "req-v1")
    assert fenced is None and reason == "retained_evidence"


# --- records janitor: decided tasks ------------------------------------------


_OLD = "2026-08-31T00:00:00+00:00"
_JANITOR_NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def _task_row(
    repo: Path,
    task_id: str,
    status: str,
    *,
    worker_status: str = "done",
    updated_at: str = _OLD,
    card: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    body = {
        "task_id": task_id,
        "launch_request_id": f"req-{task_id}",
        "status": status,
        "worker_status": worker_status,
        "updated_at": updated_at,
        **dict(card or {}),
    }
    connection = sqlite3.connect(task_store.canonical_db_path(repo))
    try:
        connection.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,"
            "created_at,updated_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (task_id, "runner", "coding", status, worker_status, json.dumps(body),
             updated_at, updated_at, updated_at),
        )
        connection.commit()
    finally:
        connection.close()
    return body


def _terminal_ledger(timestamp: str = "2026-08-31T00:00:01+00:00"):
    def latest(_root: Path, request_id: str) -> dict[str, Any]:
        return {
            "task_id": request_id.removeprefix("req-"),
            "request_id": request_id,
            "state": "accepted",
            "timestamp": timestamp,
        }

    return latest


def test_automatic_hygiene_archives_standalone_decided_rows_past_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    cards = {
        "STANDALONE_DONE": _task_row(repo, "STANDALONE_DONE", "finished"),
        # Touched after its worker's last ledger row: ledger_stale must not hold it.
        "STANDALONE_SUPERSEDED": _task_row(
            repo,
            "STANDALONE_SUPERSEDED",
            "superseded",
            worker_status="superseded",
            updated_at="2026-08-31T06:00:00+00:00",
        ),
        "STANDALONE_FRESH": _task_row(
            repo, "STANDALONE_FRESH", "finished", updated_at="2026-09-01T11:00:00+00:00"
        ),
        "STANDALONE_BLOCKED": _task_row(
            repo, "STANDALONE_BLOCKED", "blocked", worker_status="blocked"
        ),
    }
    archived: list[str] = []
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(task_store, "get_task", lambda _root, task_id: cards.get(task_id))
    monkeypatch.setattr(task_retention, "_latest_process_row", _terminal_ledger())
    monkeypatch.setattr(
        task_store,
        "archive_task",
        lambda _root, task_id, **_kwargs: (archived.append(task_id) is None, "archived"),
    )

    result = task_retention.run_automatic_hygiene(repo, now=_JANITOR_NOW)

    assert archived == ["STANDALONE_DONE", "STANDALONE_SUPERSEDED"]
    assert result["eligible"] == 2
    assert result["archived"] == 2


def test_final_archive_fence_returns_dependency_live_for_undecided_dependent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    dependency = _task_row(repo, "DEP_DONE", "finished")
    _task_row(
        repo,
        "DEPENDENT_WAITING",
        "pending",
        worker_status="unclaimed",
        card={"depends_on": ["DEP_DONE"]},
    )
    monkeypatch.setattr(task_store, "get_task", lambda _root, _task_id: dependency)
    monkeypatch.setattr(task_retention, "_latest_process_row", _terminal_ledger())

    fenced, reason = task_retention._final_archive_fence(repo, "DEP_DONE", "req-DEP_DONE")
    assert fenced is None and reason == "dependency_live"

    connection = sqlite3.connect(task_store.canonical_db_path(repo))
    try:
        connection.execute(
            "UPDATE tasks SET status='finished', worker_status='done' "
            "WHERE task_id='DEPENDENT_WAITING'"
        )
        connection.commit()
    finally:
        connection.close()
    fenced, reason = task_retention._final_archive_fence(repo, "DEP_DONE", "req-DEP_DONE")
    assert reason == "" and fenced == dependency


def test_final_archive_fence_matches_padded_dependency_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    dependency = _task_row(repo, "DEP_DONE", "finished")
    _task_row(
        repo,
        "DEPENDENT_WAITING",
        "pending",
        worker_status="unclaimed",
        card={"depends_on": [" DEP_DONE "]},
    )
    monkeypatch.setattr(task_store, "get_task", lambda _root, _task_id: dependency)
    monkeypatch.setattr(task_retention, "_latest_process_row", _terminal_ledger())

    fenced, reason = task_retention._final_archive_fence(repo, "DEP_DONE", "req-DEP_DONE")
    assert fenced is None and reason == "dependency_live"


def test_records_janitor_supersedes_callback_before_archiving_in_one_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiworkhub import needfix_store

    repo = _repo(tmp_path)
    card = _task_row(repo, "STANDALONE_DONE", "finished")
    connection = sqlite3.connect(task_store.canonical_db_path(repo))
    try:
        connection.execute(
            "INSERT INTO callback_outbox(task_id,origin_thread_id,state,created_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            ("STANDALONE_DONE", "thread", "pending", _OLD, _OLD),
        )
        connection.commit()
    finally:
        connection.close()
    archived: list[str] = []
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(task_store, "get_task", lambda _root, _task_id: card)
    monkeypatch.setattr(task_retention, "_latest_process_row", _terminal_ledger())
    monkeypatch.setattr(
        task_store,
        "archive_task",
        lambda _root, task_id, **_kwargs: (archived.append(task_id) is None, "archived"),
    )
    monkeypatch.setattr(
        needfix_store,
        "janitor_bookkeeping",
        lambda _root, **_kwargs: {"scanned": 0, "archived": 0, "reopened": 0, "skipped": 0},
    )

    result = task_retention.run_records_janitor(repo, now=_JANITOR_NOW)

    assert result["callbacks"] == {
        "ok": True, "scanned": 1, "superseded": 1, "batches_superseded": 0,
    }
    assert result["tasks"]["archived"] == 1
    assert archived == ["STANDALONE_DONE"]
    assert result["needfix"]["ok"] is True


def test_records_janitor_isolates_a_raising_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiworkhub import callback_store, needfix_store

    repo = _repo(tmp_path)
    ran: list[str] = []

    def boom(_conn: Any) -> dict[str, int]:
        raise RuntimeError("boom")

    monkeypatch.setattr(callback_store, "supersede_decided_task_callbacks", boom)
    monkeypatch.setattr(
        task_retention,
        "run_automatic_hygiene",
        lambda _root, **_kwargs: ran.append("tasks") or {"ok": True, "archived": 0},
    )
    monkeypatch.setattr(
        needfix_store,
        "janitor_bookkeeping",
        lambda _root, **_kwargs: ran.append("needfix") or {"scanned": 0},
    )

    result = task_retention.run_records_janitor(repo, now=_JANITOR_NOW)

    assert result["callbacks"] == {"ok": False, "error": "RuntimeError: boom"}
    assert ran == ["needfix", "tasks"]
    assert result["tasks"] == {"ok": True, "archived": 0}
    assert result["needfix"] == {"ok": True, "scanned": 0}


def _accepted_task_needfix(repo: Path) -> tuple[str, str]:
    from aiworkhub import needfix_store

    needfix_store.initialize_repository(repo)
    record = needfix_store.add_needfix(
        repo, title="accepted fix", description="d", status="accepted"
    )
    converted = needfix_store.convert_needfix(
        repo, record["id"], lambda card: {"ok": True, "task_id": card["task_id"]}
    )
    return record["id"], str(converted["converted_task_id"])


def _quarantine_on_archive(
    repo: Path, monkeypatch: pytest.MonkeyPatch, card: Mapping[str, Any]
) -> None:
    """Stand in for quarantine: hygiene moves the row into a retention batch."""

    def live_card(_root: Path, task_id: str) -> dict[str, Any] | None:
        connection = sqlite3.connect(task_store.canonical_db_path(repo))
        try:
            row = connection.execute(
                "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        finally:
            connection.close()
        return dict(card) if row else None

    def quarantine(_root: Path, task_id: str, **_kwargs: Any) -> tuple[bool, str]:
        connection = sqlite3.connect(task_store.canonical_db_path(repo))
        connection.row_factory = sqlite3.Row
        try:
            task_retention._ensure_schema(connection)
            tasks = [
                dict(row)
                for row in connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,))
            ]
            payload = {
                "schema_id": task_retention.SCHEMA_ID,
                "batch_id": f"tasks-test-{task_id}",
                "tasks": tasks,
                "task_events": [],
                "callback_outbox": [],
                "callback_batches": [],
            }
            connection.execute(
                "INSERT INTO task_retention_batches("
                "batch_id,task_count,payload_json,quarantined_at,restore_deadline"
                ") VALUES (?,?,?,?,?)",
                (payload["batch_id"], len(tasks), json.dumps(payload), _OLD, _OLD),
            )
            connection.execute("DELETE FROM task_events WHERE task_id=?", (task_id,))
            connection.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
            connection.commit()
        finally:
            connection.close()
        return True, "archived"

    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(task_store, "get_task", live_card)
    monkeypatch.setattr(task_retention, "_latest_process_row", _terminal_ledger())
    monkeypatch.setattr(task_store, "archive_task", quarantine)


def _assert_needfix_retired_and_task_quarantined(
    repo: Path, needfix_id: str, task_id: str
) -> None:
    from aiworkhub import needfix_store

    assert needfix_store.get_needfix(repo, needfix_id)["status"] == "archived"
    events = [event["event"] for event in needfix_store.list_events(repo, needfix_id, limit=20)]
    assert "missing_task_link_reopened" not in events
    assert task_store.get_task(repo, task_id) is None
    assert task_retention._retained_task_row(repo, task_id)["status"] == "finished"


def test_records_janitor_retires_needfix_of_accepted_task_it_quarantines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    needfix_id, task_id = _accepted_task_needfix(repo)
    card = _task_row(repo, task_id, "finished", card={"accepted_at": _OLD})
    _quarantine_on_archive(repo, monkeypatch, card)

    result = task_retention.run_records_janitor(repo, now=_JANITOR_NOW)

    assert result["needfix"]["archived"] == 1
    assert result["needfix"]["reopened"] == 0
    assert result["tasks"]["archived"] == 1
    _assert_needfix_retired_and_task_quarantined(repo, needfix_id, task_id)


def test_records_janitor_retires_needfix_of_already_quarantined_accepted_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    needfix_id, task_id = _accepted_task_needfix(repo)
    card = _task_row(repo, task_id, "finished", card={"accepted_at": _OLD})
    _quarantine_on_archive(repo, monkeypatch, card)
    assert task_retention.run_automatic_hygiene(repo, now=_JANITOR_NOW)["archived"] == 1

    result = task_retention.run_records_janitor(repo, now=_JANITOR_NOW)

    assert result["needfix"]["archived"] == 1
    assert result["needfix"]["reopened"] == 0
    _assert_needfix_retired_and_task_quarantined(repo, needfix_id, task_id)


def test_records_janitor_retires_needfix_of_quarantined_archived_accepted_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiworkhub import needfix_store

    repo = _repo(tmp_path)
    needfix_id, task_id = _accepted_task_needfix(repo)
    card = _task_row(
        repo,
        task_id,
        "archived",
        card={"accepted_at": _OLD, "archive_operation": "archived"},
    )
    _quarantine_on_archive(repo, monkeypatch, card)
    # accepted_at lives only inside the retained row's card_json.
    assert task_store.archive_task(repo, task_id) == (True, "archived")
    retained = task_retention._retained_task_row(repo, task_id)
    assert retained["status"] == "archived" and retained["accepted_at"] == _OLD
    assert "card_json" not in retained

    result = task_retention.run_records_janitor(repo, now=_JANITOR_NOW)

    assert result["needfix"]["archived"] == 1
    assert result["needfix"]["reopened"] == 0
    assert needfix_store.get_needfix(repo, needfix_id)["status"] == "archived"
    events = [event["event"] for event in needfix_store.list_events(repo, needfix_id, limit=20)]
    assert "missing_task_link_reopened" not in events
