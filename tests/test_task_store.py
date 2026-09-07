from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import task_store  # noqa: E402
from aiworkhub import review_lifecycle  # noqa: E402


def _insert_task(repo: Path, task_id: str, *, status: str) -> None:
    task_store.initialize_repository(repo)
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-07-22T00:00:00+00:00"
    card = {
        "task_id": task_id,
        "runner": "codex_worker_b891",
        "topic": "task_mcp",
        "allowed_writes": ["out.txt"],
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, started_at) "
            "VALUES (?, ?, 'task_mcp', ?, ?, '', '', ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                "codex_worker_b891",
                status,
                "claimed" if status == "processing" else "unclaimed",
                json.dumps(card),
                now,
                now,
                "codex_worker_b891" if status == "processing" else "",
                now if status == "processing" else "",
                now if status == "processing" else "",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_write_connections_use_bounded_wal_concurrency_pragmas(tmp_path: Path) -> None:
    database = tmp_path / "task.sqlite"
    connection = task_store._connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        connection.close()


def test_initialize_repository_adds_review_lifecycle_tables_to_canonical_task_db(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    result = task_store.initialize_repository(repo)
    assert result["ok"] is True
    _readiness, db_path = task_store._require_ready(repo)
    connection = sqlite3.connect(db_path)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(review_action_outbox)")
        }
    finally:
        connection.close()

    assert {"review_chains", "review_action_outbox"} <= tables
    assert {
        "descriptor_json",
        "descriptor_sha256",
        "state",
        "lease_token",
        "receipt_commitment_sha256",
        "completed_at",
        "failure_reason",
    } <= columns

    chain = review_lifecycle.create_or_replay_chain(
        db_path,
        target_task_id="TASK_STORE_REVIEW",
        target_request_id="req-store-review",
        claim_epoch="1",
        packet_sha256="a" * 64,
        candidate_sha256="b" * 64,
    )
    assert len(chain.actions) == 12


def test_atomic_json_closes_descriptor_when_fdopen_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "registry.json"
    descriptor = os.open(tmp_path / "owned.tmp", os.O_RDWR | os.O_CREAT, 0o600)
    temp_path = tmp_path / "atomic.tmp"
    monkeypatch.setattr(
        task_store.tempfile,
        "mkstemp",
        lambda **_kwargs: (descriptor, str(temp_path)),
    )
    monkeypatch.setattr(
        task_store.os,
        "fdopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("fdopen failed")),
    )

    with pytest.raises(OSError, match="fdopen failed"):
        task_store._atomic_write_json(target, {"ok": True})
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_archive_removes_pending_card_from_active_lists_and_preserves_events(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_ARCHIVE_B891", status="pending")

    ok, state = task_store.archive_task(
        repo,
        "TASK_ARCHIVE_B891",
        actor="codex",
        reason="reviewed cleanup",
    )
    assert (ok, state) == (True, "archived")
    assert task_store.list_tasks(repo, status="pending") == []
    assert task_store.list_tasks(repo, status="archived")[0]["task_id"] == "TASK_ARCHIVE_B891"
    assert task_store.get_task_events(repo, "TASK_ARCHIVE_B891")[0]["event"] == "archived"


def test_manager_decision_counts_include_rework_and_review_archival(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_DECISIONS", status="pending")
    _readiness, db_path = task_store._require_ready(repo)
    connection = sqlite3.connect(db_path)
    try:
        rows = [
            ("accept_review", "{}"),
            ("reject_review", '{"to":"pending"}'),
            ("archived", '{"reason":"reject_review:not useful"}'),
            ("archived", '{"reason":"dashboard cleanup"}'),
        ]
        connection.executemany(
            "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) "
            "VALUES('TASK_DECISIONS',?,'codex',?,'2026-08-02T00:00:00Z')",
            rows,
        )
        connection.commit()
    finally:
        connection.close()

    result = task_store.manager_decision_counts(repo)
    assert {key: result[key] for key in ("accepted", "rejected", "total")} == {
        "accepted": 1, "rejected": 2, "total": 3,
    }
    assert result["rejected_latency"]["count"] == 0
    connection = sqlite3.connect(db_path)
    try:
        indexes = {
            str(row[1]) for row in connection.execute("PRAGMA index_list('task_events')")
        }
    finally:
        connection.close()
    assert {
        "idx_task_store_events_event_id",
        "idx_task_store_events_task_event_id",
    } <= indexes


def test_manager_decision_counts_uses_nearest_prior_review_per_decision(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_DECISION_LATENCY", status="pending")
    _readiness, db_path = task_store._require_ready(repo)
    connection = sqlite3.connect(db_path)
    try:
        connection.executemany(
            "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) "
            "VALUES('TASK_DECISION_LATENCY',?,'codex','{}',?)",
            [
                ("terminal_review", "2026-08-02T00:00:00Z"),
                ("accept_review", "2026-08-02T00:00:09Z"),
                ("terminal_review", "2026-08-02T00:01:00Z"),
                ("reject_review", "2026-08-02T00:01:21Z"),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    result = task_store.manager_decision_counts(repo)

    assert result["accepted_latency"] == {
        "count": 1,
        "p50_seconds": 9.0,
        "p95_seconds": 9.0,
    }
    assert result["rejected_latency"] == {
        "count": 1,
        "p50_seconds": 21.0,
        "p95_seconds": 21.0,
    }


def test_list_task_cards_matches_canonical_detail_in_one_bounded_batch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_BATCH_A", status="pending")
    _insert_task(repo, "TASK_BATCH_B", status="processing")

    cards = task_store.list_task_cards(repo, limit=10)

    assert {card["task_id"] for card in cards} == {"TASK_BATCH_A", "TASK_BATCH_B"}
    assert all(card["allowed_writes"] == ["out.txt"] for card in cards)
    by_id = {card["task_id"]: card for card in cards}
    assert by_id["TASK_BATCH_A"] == task_store.get_task(repo, "TASK_BATCH_A")
    assert by_id["TASK_BATCH_B"] == task_store.get_task(repo, "TASK_BATCH_B")


def test_decoded_card_omits_recursive_storage_envelope_and_upgrade_compacts_it(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_RECURSIVE_CARD", status="pending")
    _readiness, db_path = task_store._require_ready(repo)
    recursive = {
        "task_id": "TASK_RECURSIVE_CARD",
        "runner": "codex_worker_b891",
        "topic": "task_mcp",
        "allowed_writes": ["out.txt"],
        "card_json": json.dumps({"card_json": "x" * 200_000}),
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?",
            (json.dumps(recursive), "TASK_RECURSIVE_CARD"),
        )
        conn.commit()
    finally:
        conn.close()

    decoded = task_store.get_task(repo, "TASK_RECURSIVE_CARD")
    assert decoded is not None
    assert "card_json" not in decoded
    assert "card_json" not in task_store.persistable_card_payload(decoded)

    assert task_store._upgrade_compatible_schema(db_path) is True
    conn = sqlite3.connect(db_path)
    try:
        compacted = json.loads(
            conn.execute(
                "SELECT card_json FROM tasks WHERE task_id=?",
                ("TASK_RECURSIVE_CARD",),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert "card_json" not in compacted
    assert len(json.dumps(compacted)) < 1_000


def test_initialize_repository_survives_malformed_legacy_card_json(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_MALFORMED_CARD", status="pending")
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET topic='', card_json='{not-json' "
            "WHERE task_id='TASK_MALFORMED_CARD'",
        )
        conn.commit()
    finally:
        conn.close()

    result = task_store.initialize_repository(repo)
    assert result["ok"] is True

    conn = sqlite3.connect(db_path)
    try:
        topic = conn.execute(
            "SELECT topic FROM tasks WHERE task_id='TASK_MALFORMED_CARD'",
        ).fetchone()[0]
    finally:
        conn.close()
    assert topic == ""


def test_initialize_repository_backfills_valid_card_json_topic_guarded_by_json_valid(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_VALID_CARD", status="pending")
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET topic='', card_json=? WHERE task_id='TASK_VALID_CARD'",
            (json.dumps({"topic": "recovered_topic"}),),
        )
        conn.commit()
    finally:
        conn.close()

    task_store.initialize_repository(repo)

    conn = sqlite3.connect(db_path)
    try:
        topic = conn.execute(
            "SELECT topic FROM tasks WHERE task_id='TASK_VALID_CARD'",
        ).fetchone()[0]
    finally:
        conn.close()
    assert topic == "recovered_topic"


def test_supersede_removes_processing_orphan_without_deleting_audit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_SUPERSEDE_B891", status="processing")

    ok, state = task_store.archive_task(
        repo,
        "TASK_SUPERSEDE_B891",
        actor="codex",
        reason="orphaned canary",
        allow_processing=True,
        operation="superseded",
        superseded_by="TASK_REPLACEMENT_B891",
    )
    assert (ok, state) == (True, "superseded")
    assert task_store.list_tasks(repo, status="processing") == []
    archived = task_store.list_tasks(repo, status="archived")
    assert archived[0]["task_id"] == "TASK_SUPERSEDE_B891"
    detail = task_store.get_task(repo, "TASK_SUPERSEDE_B891")
    assert detail is not None
    assert detail["superseded_by"] == "TASK_REPLACEMENT_B891"
    event = task_store.get_task_events(repo, "TASK_SUPERSEDE_B891")[0]
    assert event["event"] == "superseded"
    assert json.loads(event["payload"])["superseded_by"] == "TASK_REPLACEMENT_B891"


@pytest.mark.parametrize(
    "substatus",
    [
        "exited",
        "validation_failed",
        "worker_failed",
        "launch_failed",
        "scope_rejected",
        "cancelled",
        "timed_out",
    ],
)
def test_mark_terminal_review_routes_processing_task_to_review_for_every_terminal_class(
    tmp_path: Path, substatus: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = f"TASK_TERMINAL_{substatus.upper()}"
    _insert_task(repo, task_id, status="processing")

    ok, state = task_store.mark_terminal_review(
        repo,
        task_id,
        runner="codex_worker_b891",
        substatus=substatus,
        evidence={"exit_code": 1},
    )

    assert (ok, state) == (True, "review")
    assert task_store.list_tasks(repo, status="processing") == []
    reviewed = task_store.list_tasks(repo, status="review")
    assert reviewed[0]["task_id"] == task_id
    card = task_store.get_task(repo, task_id)
    assert card["terminal_substatus"] == substatus
    events = [e["event"] for e in task_store.get_task_events(repo, task_id)]
    assert "terminal_review" in events


def test_missing_retained_review_workspace_blocks_exact_episode_and_releases_claim(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_REVIEW_WORKSPACE_MISSING"
    request_id = "req-review-workspace-missing"
    _insert_task(repo, task_id, status="processing")
    ok, state = task_store.mark_terminal_review(
        repo,
        task_id,
        runner="codex_worker_b891",
        substatus="review_ready",
        evidence={
            "request_identity": {
                "request_id": request_id,
                "task_id": task_id,
                "runner": "codex_worker_b891",
            },
            "workspace": {},
            "changed_path_hashes": {},
        },
    )
    assert (ok, state) == (True, "review")

    ok, state = task_store.mark_review_workspace_missing(
        repo,
        task_id,
        runner="codex_worker_b891",
        request_id=request_id,
        reason="review_workspace_missing",
    )

    assert (ok, state) == (True, "blocked")
    card = task_store.get_task(repo, task_id)
    assert card is not None
    assert card["status"] == "blocked"
    assert card["worker_status"] == "finalize_failed"
    assert card["claimed_by"] is None
    assert card["workspace_retention_failure"]["request_id"] == request_id
    assert card["terminal_review"]["evidence"]["request_identity"]["request_id"] == request_id
    events = [row["event"] for row in task_store.get_task_events(repo, task_id)]
    assert "review_workspace_missing" in events


def test_missing_retained_review_workspace_rejects_stale_request_identity(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_REVIEW_WORKSPACE_STALE"
    _insert_task(repo, task_id, status="processing")
    task_store.mark_terminal_review(
        repo,
        task_id,
        runner="codex_worker_b891",
        substatus="review_ready",
        evidence={"request_identity": {"request_id": "current-request"}},
    )

    ok, state = task_store.mark_review_workspace_missing(
        repo,
        task_id,
        runner="codex_worker_b891",
        request_id="stale-request",
        reason="review_workspace_missing",
    )

    assert (ok, state) == (False, "review_request_identity_mismatch")
    assert task_store.get_task(repo, task_id)["status"] == "review"


def test_mark_terminal_review_allows_launch_failed_from_pending(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_LAUNCH_FAILED_B894"
    _insert_task(repo, task_id, status="pending")

    ok, state = task_store.mark_terminal_review(
        repo, task_id, runner="codex_worker_b891", substatus="launch_failed"
    )
    assert (ok, state) == (True, "review")
    card = task_store.get_task(repo, task_id)
    assert card["status"] == "review"
    assert card["terminal_substatus"] == "launch_failed"


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"status": "superseded", "worker_status": "unclaimed"}, "superseded"),
        ({"status": "pending", "worker_status": "superseded"}, "superseded"),
        (
            {
                "archived_at": "2026-08-08T00:00:00Z",
                "status": "superseded",
                "worker_status": "superseded",
            },
            "archived",
        ),
        ({"status": "finished", "worker_status": "superseded"}, "finished"),
        ({"status": "superseded", "worker_status": "deferred"}, "blocked"),
        ({"status": "superseded", "worker_status": "ready_for_review"}, "review"),
        ({"status": "superseded", "worker_status": "claimed"}, "processing"),
    ],
)
def test_canonical_status_preserves_superseded_and_existing_precedence(
    row: dict[str, str], expected: str
) -> None:
    assert task_store.canonical_status(row) == expected


def test_get_task_and_list_task_cards_preserve_superseded_status(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_SUPERSEDED_REVIEWER"
    _insert_task(repo, task_id, status="pending")
    _readiness, db_path = task_store._require_ready(repo)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "UPDATE tasks SET status = ?, worker_status = ? WHERE task_id = ?",
            ("superseded", "superseded", task_id),
        )
        connection.commit()
    finally:
        connection.close()

    detail = task_store.get_task(repo, task_id)
    listed = {
        card["task_id"]: card for card in task_store.list_task_cards(repo, limit=10)
    }[task_id]

    assert detail is not None
    assert detail["status"] == "superseded"
    assert listed["status"] == "superseded"


def test_mark_terminal_review_rejects_illegal_regression_to_pending_from_review(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_ILLEGAL_REGRESSION_B892"
    _insert_task(repo, task_id, status="processing")

    ok, state = task_store.mark_terminal_review(
        repo, task_id, runner="codex_worker_b891", substatus="exited"
    )
    assert (ok, state) == (True, "review")

    # A second terminal-review attempt against an already-reviewed task must
    # fail closed instead of silently re-recording (or regressing) the task.
    ok2, state2 = task_store.mark_terminal_review(
        repo, task_id, runner="codex_worker_b891", substatus="worker_failed"
    )
    assert ok2 is False
    assert state2.startswith("illegal_transition:from=review")

    # The task must remain exactly in review -- never pending, never finished.
    card = task_store.get_task(repo, task_id)
    assert card["status"] == "review"
    events = [e["event"] for e in task_store.get_task_events(repo, task_id)]
    assert "illegal_transition_rejected" in events


def test_mark_terminal_review_rejects_unknown_substatus(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_UNKNOWN_SUBSTATUS_B895"
    _insert_task(repo, task_id, status="processing")

    ok, state = task_store.mark_terminal_review(
        repo, task_id, runner="codex_worker_b891", substatus="totally_made_up_outcome"
    )
    assert ok is False
    assert state == "illegal_transition:unknown_substatus=totally_made_up_outcome"
    card = task_store.get_task(repo, task_id)
    assert card["status"] == "processing"


def test_mark_terminal_review_known_failure_substatus_never_deterministically_passes(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_DETERMINISTIC_FAILURE_B896"
    _insert_task(repo, task_id, status="processing")

    ok, state = task_store.mark_terminal_review(
        repo,
        task_id,
        runner="codex_worker_b891",
        substatus="worker_failed",
        evidence={
            "validation": [{"command": "pytest", "returncode": 0}],
            "required_outputs": [{"path": "out.txt", "sha256": "a" * 64, "bytes": 5}],
        },
    )
    assert (ok, state) == (True, "review")
    card = task_store.get_task(repo, task_id)
    verification = card["deterministic_verification"]
    assert verification["applicable"] is True
    assert verification["pass"] is False
    assert verification["reason"] == "known_failure_substatus"


def test_mark_terminal_review_persists_validation_evidence_support(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_CONTRADICTED_VALIDATION_FAILURE_NF621"
    _insert_task(repo, task_id, status="processing")

    ok, state = task_store.mark_terminal_review(
        repo,
        task_id,
        runner="codex_worker_b891",
        substatus="validation_failed",
        evidence={"validation": [{"command": "pytest", "returncode": 0}]},
    )

    assert (ok, state) == (True, "review")
    card = task_store.get_task(repo, task_id)
    verification = card["deterministic_verification"]
    assert verification["reason"] == "substatus_contradicted_by_evidence"
    assert verification["evidence_support"] == "contradicted"
    assert card["terminal_review"]["evidence_support"] == "contradicted"


def test_mark_terminal_review_review_ready_with_no_gates_is_not_applicable(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_NO_GATES_B897"
    _insert_task(repo, task_id, status="processing")

    ok, state = task_store.mark_terminal_review(
        repo, task_id, runner="codex_worker_b891", substatus="review_ready"
    )
    assert (ok, state) == (True, "review")
    card = task_store.get_task(repo, task_id)
    verification = card["deterministic_verification"]
    assert verification["applicable"] is False
    assert verification["pass"] is False
    assert verification["reason"] == "no_gates_recorded"


def test_mark_terminal_review_review_ready_passes_only_with_clean_evidence(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_CLEAN_EVIDENCE_B898"
    _insert_task(repo, task_id, status="processing")

    ok, state = task_store.mark_terminal_review(
        repo,
        task_id,
        runner="codex_worker_b891",
        substatus="review_ready",
        evidence={
            "validation": [{"command": "pytest", "returncode": 0}],
            "required_outputs": [{"path": "out.txt", "sha256": "a" * 64, "bytes": 5}],
        },
    )
    assert (ok, state) == (True, "review")
    card = task_store.get_task(repo, task_id)
    verification = card["deterministic_verification"]
    assert verification["applicable"] is True
    assert verification["pass"] is True
    assert verification["claim_epoch"] == 0
    assert card["terminal_review"]["claim_epoch"] == 0


def test_mark_terminal_review_review_ready_fails_on_none_returncode(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_NONE_RETURNCODE_B899"
    _insert_task(repo, task_id, status="processing")

    ok, state = task_store.mark_terminal_review(
        repo,
        task_id,
        runner="codex_worker_b891",
        substatus="exited",
        evidence={
            "validation": [{"command": "pytest", "returncode": None}],
            "required_outputs": [{"path": "out.txt", "sha256": "a" * 64, "bytes": 5}],
        },
    )
    assert (ok, state) == (True, "review")
    card = task_store.get_task(repo, task_id)
    verification = card["deterministic_verification"]
    assert verification["applicable"] is True
    assert verification["pass"] is False
    assert verification["reason"] == "evidence_verdict_failed"


def _insert_review_ready_task(
    repo: Path,
    task_id: str,
    *,
    request_id: str = "req-coord-b1",
    deterministic_verification: dict | None = None,
) -> None:
    _insert_task(repo, task_id, status="review")
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET claimed_by='codex_worker_b891', worker_status='review' WHERE task_id=?",
            (task_id,),
        )
        card = json.loads(
            conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]
        )
        terminal_review = {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {
                    "request_id": request_id,
                    "task_id": task_id,
                    "runner": "codex_worker_b891",
                },
            },
        }
        if deterministic_verification is not None:
            terminal_review["deterministic_verification"] = deterministic_verification
        card["terminal_review"] = terminal_review
        conn.execute("UPDATE tasks SET card_json=? WHERE task_id=?", (json.dumps(card), task_id))
        conn.commit()
    finally:
        conn.close()


def test_mark_terminal_review_rejects_regression_from_archived(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = "TASK_ILLEGAL_FROM_ARCHIVED_B893"
    _insert_task(repo, task_id, status="pending")
    ok, state = task_store.archive_task(repo, task_id, actor="codex", reason="done")
    assert (ok, state) == (True, "archived")

    ok2, state2 = task_store.mark_terminal_review(
        repo, task_id, runner="codex_worker_b891", substatus="worker_failed"
    )
    assert ok2 is False
    assert state2.startswith("illegal_transition:from=archived")
    card = task_store.get_task(repo, task_id)
    assert card["status"] == "archived"


def test_exact_status_counts_includes_superseded_without_keyerror(tmp_path: Path) -> None:
    """A persisted superseded row must be counted in its own exact bucket
    without raising KeyError and without folding into pending or active."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "TASK_SUPERSEDED_A", status="pending")
    _insert_task(repo, "TASK_PROCESSING_A", status="processing")
    _insert_task(repo, "TASK_FINISHED_A", status="finished")
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET status='superseded', worker_status='superseded' "
            "WHERE task_id='TASK_SUPERSEDED_A'"
        )
        conn.execute(
            "UPDATE tasks SET worker_status='done' WHERE task_id='TASK_FINISHED_A'"
        )
        conn.commit()
    finally:
        conn.close()

    counts = task_store.exact_status_counts(repo)
    assert counts["superseded"] == 1
    assert counts["pending"] == 0
    assert counts["processing"] == 1
    assert counts["finished"] == 1
    assert "superseded" in counts
    # Superseded must not be folded into pending or any active lifecycle bucket.
    assert counts["pending"] == 0
    assert counts["review"] == 0


def test_mark_terminal_failure_stamps_relaunch_guard_identity(tmp_path: Path) -> None:
    # NF-2026-00548 reachability: the guard in process_launcher can only fire
    # against records this recorder actually writes, so the two are proven
    # together, end to end, with no hand-built failure record.
    from aiworkhub import process_launcher

    repo = tmp_path
    task_store.initialize_repository(repo)
    _readiness, db_path = task_store._require_ready(repo)
    card = {
        "task_id": "GUARD_TASK",
        "runner": "codex_worker_b891",
        "topic": "task_mcp",
        "objective": "guard reachability",
        "allowed_writes": ["out.txt"],
        "acceptance": ["out.txt changes"],
        "launch_request_id": "guard-req-1",
        "claim_epoch": 1,
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by) "
            "VALUES (?, ?, 'task_mcp', 'processing', 'claimed', '', 'guard reachability', ?, ?, ?, ?)",
            (
                "GUARD_TASK",
                "codex_worker_b891",
                json.dumps(card),
                "2026-09-01T00:00:00+00:00",
                "2026-09-01T00:00:00+00:00",
                "codex_worker_b891",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    ok, state = task_store.mark_terminal_review(
        repo,
        "GUARD_TASK",
        runner="codex_worker_b891",
        substatus="validation_failed",
        evidence={
            "request_id": "guard-req-1",
            "adapter_id": "codex_cli",
            "error": "validation_failed: exact repeated red",
        },
    )
    assert ok, state

    recorded = task_store.get_task(repo, "GUARD_TASK")
    failure = recorded["terminal_review"]
    assert failure["request_id"] == "guard-req-1"
    assert failure["adapter_id"] == "codex_cli"
    assert failure["error_hash"] == task_store.bounded_error_hash(
        "validation_failed: exact repeated red"
    )
    assert failure["card_content_sha256"] == task_store.card_content_identity(recorded)
    assert failure["review_feedback_identity"] == task_store.review_feedback_identity(
        recorded
    )

    refusal = process_launcher.identical_relaunch_refusal(
        recorded, runner="codex_worker_b891", adapter_id="codex_cli"
    )
    assert refusal.startswith("identical_relaunch_blocked:guard-req-1:")

    reworked = dict(recorded)
    reworked["review_feedback"] = {"reason": "new manager instruction"}
    assert (
        process_launcher.identical_relaunch_refusal(
            reworked, runner="codex_worker_b891", adapter_id="codex_cli"
        )
        == ""
    )


# --- the review-feedback authentication axis --------------------------------
#
# ``CARD_CONTENT_IDENTITY_KEYS`` excludes ``review_feedback`` on the written
# claim that it "is authenticated on its own axis (review_feedback_identity), so
# the two relaunch-guard inputs stay independent instead of collapsing into one
# hash".  These tests execute that claim against the shape the writer actually
# emits, which is where it was previously false.


def _rework_feedback(instruction: str) -> dict:
    """The object ``core.reject_review`` really stores on a reworked card."""
    return {
        "schema_id": "aiworkhub.rework_feedback_delta.v1",
        "instruction": instruction,
        "reason_identity": {
            "bytes": len(instruction.encode("utf-8")),
            "sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
            "truncated": False,
        },
        "predecessor_request_id": "req-prev",
        "predecessor_changed_paths": [],
        "residual_identities": [],
    }


def test_review_feedback_identity_distinguishes_two_manager_instructions() -> None:
    """Two different rework instructions must not share one feedback identity.

    Previously ``_review_feedback_reasons`` looked only for ``reason``/``code``,
    keys the writer never emits, so every card the system produced digested the
    same empty list and the axis was a constant.
    """
    first = {"review_feedback": _rework_feedback("fix the guarded transaction")}
    second = {"review_feedback": _rework_feedback("revert and re-scope the card")}

    assert task_store.review_feedback_identity(
        first
    ) != task_store.review_feedback_identity(second)

    # And the axis is stable for the same instruction.
    same = {"review_feedback": _rework_feedback("fix the guarded transaction")}
    assert task_store.review_feedback_identity(
        first
    ) == task_store.review_feedback_identity(same)


def test_review_feedback_identity_is_not_constant_across_written_cards() -> None:
    """The regression itself, stated precisely.

    The old reader was not constant over *everything* -- it still separated "no
    feedback" (``[]``) from "some feedback" (``[""]``).  What it could not do is
    separate one written instruction from another: every card the writer
    produced digested to that same ``[""]``.  So the property under test is
    cardinality across DISTINCT instructions, not merely difference from empty.
    """
    instructions = [
        "fix the guarded transaction",
        "revert and re-scope the card",
        "add the inverse scan",
    ]
    identities = {
        task_store.review_feedback_identity({"review_feedback": _rework_feedback(text)})
        for text in instructions
    }
    assert len(identities) == len(instructions)
    assert task_store.review_feedback_identity({}) not in identities


def test_review_feedback_identity_separates_instructions_sharing_a_prefix() -> None:
    """``reason_identity`` outranks ``instruction`` because it is unbounded.

    ``instruction`` is the truncated reason; two instructions that agree up to
    the byte cap would share it.  The digest of the full pre-truncation bytes
    keeps them apart.
    """
    shared_prefix = "x" * 64
    first = {
        "review_feedback": {
            "schema_id": "aiworkhub.rework_feedback_delta.v1",
            "instruction": shared_prefix,
            "reason_identity": {"sha256": "a" * 64, "bytes": 200, "truncated": True},
        }
    }
    second = {
        "review_feedback": {
            "schema_id": "aiworkhub.rework_feedback_delta.v1",
            "instruction": shared_prefix,
            "reason_identity": {"sha256": "b" * 64, "bytes": 200, "truncated": True},
        }
    }
    assert task_store.review_feedback_identity(
        first
    ) != task_store.review_feedback_identity(second)


def test_review_feedback_identity_still_reads_legacy_rows() -> None:
    """Rows written before the delta schema authenticate on what they carry."""
    assert task_store.review_feedback_identity(
        {"review_feedback": [{"reason": "validation_failed", "detail": "one"}]}
    ) == task_store.review_feedback_identity(
        {"review_feedback": [{"reason": "validation_failed", "detail": "two"}]}
    )
    assert task_store.review_feedback_identity(
        {"review_feedback": [{"reason": "validation_failed"}]}
    ) != task_store.review_feedback_identity(
        {"review_feedback": [{"reason": "scope_violation"}]}
    )
    # ``code`` remains the fallback when ``reason`` is absent or null.
    assert task_store.review_feedback_identity(
        {"review_feedback": [{"reason": None, "code": "E_SCOPE"}]}
    ) == task_store.review_feedback_identity({"review_feedback": [{"code": "E_SCOPE"}]})
    # Absent and empty stay indistinguishable, as before.
    assert task_store.review_feedback_identity({}) == task_store.review_feedback_identity(
        {"review_feedback": None}
    )


# ---------------------------------------------------------------------------
# Canonical write-path serialization (the DB-lock class).
#
# Measured cause: 452 of 549 recorded runtime lock events are
# "review_transition_failed:database is locked" raised out of
# mark_terminal_review_with_callback. The failure is a waiter at the back of the
# single-writer queue burning its whole 5000ms busy_timeout -- reproduced at
# 12 processes x ~120ms hold as 5/72 failures at exactly 5008ms, and reduced to
# 0/72 by the cross-process write lease.
# ---------------------------------------------------------------------------


def test_connect_keeps_implicit_transactions_unless_explicit_txn_requested(
    tmp_path: Path,
) -> None:
    """The explicit-transaction mode must stay opt-in.

    task_engine (7 sites) and learning_commit_store (1 site) open canonical
    connections through task_store._connect and rely on sqlite3's implicit
    transaction. Making isolation_level=None the module default would silently
    convert their multi-statement writes to autocommit and turn their
    rollback() calls into no-ops, so the default must not change.
    """
    database = tmp_path / "task.sqlite"

    legacy = task_store._connect(database)
    try:
        assert legacy.isolation_level == ""
    finally:
        legacy.close()

    explicit = task_store._connect(database, explicit_txn=True)
    try:
        assert explicit.isolation_level is None
        # commit/rollback must still work once a transaction is actually open.
        explicit.execute("CREATE TABLE probe(a)")
        task_store._begin_immediate(explicit)
        explicit.execute("INSERT INTO probe VALUES (1)")
        assert explicit.in_transaction is True
        explicit.commit()
        assert explicit.in_transaction is False
        assert explicit.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 1
        task_store._begin_immediate(explicit)
        explicit.execute("INSERT INTO probe VALUES (2)")
        explicit.rollback()
        assert explicit.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 1
    finally:
        explicit.close()


def test_begin_immediate_is_idempotent_inside_an_open_transaction(
    tmp_path: Path,
) -> None:
    """A second _begin_immediate must not raise 'transaction within a transaction'."""
    connection = task_store._connect(tmp_path / "task.sqlite", explicit_txn=True)
    try:
        connection.execute("CREATE TABLE probe(a)")
        task_store._begin_immediate(connection)
        assert connection.in_transaction is True
        task_store._begin_immediate(connection)  # must be a no-op, not an error
        assert connection.in_transaction is True
    finally:
        connection.close()


class _FlakyWalConnection(sqlite3.Connection):
    """sqlite3.Connection is an immutable C type, so the WAL pragma failure is
    injected through a connection factory rather than by patching a method."""

    wal_attempts = 0
    wal_failures_to_inject = 0
    wal_error = "database is locked"

    def execute(self, sql, *args):  # type: ignore[override,no-untyped-def]
        if str(sql).strip().upper() == "PRAGMA JOURNAL_MODE=WAL":
            type(self).wal_attempts += 1
            if type(self).wal_attempts <= type(self).wal_failures_to_inject:
                raise sqlite3.OperationalError(type(self).wal_error)
        return super().execute(sql, *args)


def _install_flaky_wal(monkeypatch, *, failures: int, error: str) -> type:
    factory = type("_Flaky", (_FlakyWalConnection,), {})
    factory.wal_attempts = 0
    factory.wal_failures_to_inject = failures
    factory.wal_error = error
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["factory"] = factory
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(task_store.sqlite3, "connect", connect)
    return factory


def test_connect_retries_wal_pragma_while_the_database_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRAGMA journal_mode=WAL is itself a write on a not-yet-WAL database.

    Measured: 301ms to 'database is locked' against a held BEGIN EXCLUSIVE on a
    DELETE-mode file. callback_store.open_db already retries this exact
    statement on this exact file; _connect must too.
    """
    factory = _install_flaky_wal(monkeypatch, failures=2, error="database is locked")
    connection = task_store._connect(tmp_path / "task.sqlite")
    try:
        assert factory.wal_attempts == 3  # two failures, then success
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        connection.close()


def test_connect_gives_up_after_the_bounded_wal_retry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounded, not infinite: a permanently locked file must fail closed."""
    factory = _install_flaky_wal(monkeypatch, failures=99, error="database is locked")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        task_store._connect(tmp_path / "task.sqlite")
    assert factory.wal_attempts == task_store._WAL_RETRY_COUNT


def test_connect_does_not_retry_a_non_lock_pragma_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry loop must not swallow an unrelated failure."""
    factory = _install_flaky_wal(monkeypatch, failures=99, error="disk I/O error")
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        task_store._connect(tmp_path / "task.sqlite")
    assert factory.wal_attempts == 1  # failed closed on the first attempt


def test_write_connection_takes_the_lease_before_it_opens_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering is the whole point: _connect issues the WAL pragma, which is
    itself a write that can fail on a lock, so connecting outside the lease
    would leave the first statement of the write path unserialized. Taking the
    lease outside also keeps it from ever being acquired inside an open
    transaction, which is the ordering that could deadlock."""
    order: list[str] = []
    real_lease = task_store.db_writer.write_lease
    real_connect = task_store._connect

    import contextlib as _contextlib

    @_contextlib.contextmanager
    def traced_lease(db_path, **kwargs):  # type: ignore[no-untyped-def]
        order.append("lease_acquired")
        with real_lease(db_path, **kwargs) as receipt:
            yield receipt
        order.append("lease_released")

    def traced_connect(path, **kwargs):  # type: ignore[no-untyped-def]
        order.append("connect")
        return real_connect(path, **kwargs)

    monkeypatch.setattr(task_store.db_writer, "write_lease", traced_lease)
    monkeypatch.setattr(task_store, "_connect", traced_connect)

    with task_store._write_connection(tmp_path / "task.sqlite") as conn:
        order.append("body")
        assert conn is not None

    assert order == ["lease_acquired", "connect", "body", "lease_released"]


def test_write_connection_holds_the_lease_for_the_whole_block(tmp_path: Path) -> None:
    """No second holder may enter while the block runs.

    The probe runs on another THREAD: db_writer's lease is deliberately
    re-entrant within one (process, thread, path) so nested use cannot
    self-deadlock, so a same-thread probe would be let straight through and
    would prove nothing.
    """
    import threading

    database = tmp_path / "task.sqlite"
    outcome: list[str] = []

    def probe() -> None:
        try:
            with task_store.db_writer.write_lease(database, timeout_s=0.05):
                outcome.append("acquired")
        except task_store.db_writer.WriteLeaseTimeout:
            outcome.append("timed_out")

    with task_store._write_connection(database):
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(timeout=30)

    assert outcome == ["timed_out"]


def _child_probe_lease(database: str, started, done) -> None:  # pragma: no cover
    """Run in a separate PROCESS: an in-process lock could not exclude this."""
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from aiworkhub import db_writer as _db_writer

    started.set()
    try:
        with _db_writer.write_lease(database, timeout_s=0.5):
            done.put("acquired")
    except _db_writer.WriteLeaseTimeout:
        done.put("timed_out")


def test_write_connection_excludes_another_process(tmp_path: Path) -> None:
    """The measured contention is cross-process (6,247 distinct supervisor pids,
    up to 12 alive at once), so the exclusion must hold across processes."""
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    database = tmp_path / "task.sqlite"
    started = ctx.Event()
    done: "mp.Queue[str]" = ctx.Queue()

    with task_store._write_connection(database):
        child = ctx.Process(target=_child_probe_lease, args=(str(database), started, done))
        child.start()
        assert started.wait(timeout=30)
        outcome = done.get(timeout=30)
        child.join(timeout=30)

    assert outcome == "timed_out"


# --------------------------------------------------------------------------
# Every canonical write path takes the lease
# --------------------------------------------------------------------------
# The five terminal-transition helpers were leased first because they carried
# all 549 measured lock events.  These ten are the remaining writers on the
# canonical database.  They were left unleased on purpose while the evidence
# named only the terminal transitions; they are leased now because leaving a
# writer outside the queue reintroduces exactly the aggregate ``busy_timeout``
# exhaustion the lease removes -- no single long holder is needed, twelve short
# ones are enough.  ``append_live_usage_event`` is the one that would have gone
# next on its own: it is called from ``process_launcher`` once per provider
# turn, which is the highest write rate in the system and the same supervisor
# process population that produced the measured events.

_LEASED_CANONICAL_WRITE_PATHS = (
    "archive_task",
    "restore_task",
    "force_terminalize",
    "retry_finalize_failed",
    "recover_blocked_rework",
    "reconcile_dead_processing_claim",
    "repair_archive_inconsistencies",
    "enqueue_terminal_callback",
    "append_usage_capture_event",
    "append_live_usage_event",
)


def _canonical_write_call(name: str, repo: Path):
    """One invocation per write path that reaches its write connection.

    Each reaches the connection and stops at its own guard, so the probe
    observes the lease without depending on a fixture for ten different
    lifecycle states -- the guards themselves are pinned by their own tests.
    """
    usage_note = "task_mcp_request:" + "0" * 32
    calls = {
        "archive_task": lambda: task_store.archive_task(repo, "T_LEASE_PROBE"),
        "restore_task": lambda: task_store.restore_task(repo, "T_LEASE_PROBE"),
        "force_terminalize": lambda: task_store.force_terminalize(
            repo, "T_LEASE_PROBE", reason="lease probe"
        ),
        "retry_finalize_failed": lambda: task_store.retry_finalize_failed(
            repo, "T_LEASE_PROBE", runner="codex_worker_b891", request_id="req-lease"
        ),
        "recover_blocked_rework": lambda: task_store.recover_blocked_rework(
            repo, "T_LEASE_PROBE"
        ),
        "reconcile_dead_processing_claim": (
            lambda: task_store.reconcile_dead_processing_claim(
                repo,
                "T_LEASE_PROBE",
                request_id="req-lease",
                claim_epoch=1,
                terminal_evidence={"state": "terminal_blocked"},
                actor="reconciler",
            )
        ),
        "repair_archive_inconsistencies": (
            lambda: task_store.repair_archive_inconsistencies(repo)
        ),
        "enqueue_terminal_callback": lambda: task_store.enqueue_terminal_callback(
            repo, "T_LEASE_PROBE", substatus="finalize_failed"
        ),
        "append_usage_capture_event": lambda: task_store.append_usage_capture_event(
            repo,
            "T_LEASE_PROBE",
            "codex_worker_b891",
            {"note": usage_note, "source": "task_mcp_launcher"},
        ),
        "append_live_usage_event": lambda: task_store.append_live_usage_event(
            repo,
            "T_LEASE_PROBE",
            "codex_worker_b891",
            request_id="req-lease",
            claimed_by="codex_worker_b891",
            claim_epoch=1,
            payload={},
        ),
    }
    return calls[name]


@pytest.mark.parametrize("path_name", _LEASED_CANONICAL_WRITE_PATHS)
def test_canonical_write_path_takes_the_lease_before_it_connects(
    path_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two separate claims, both of which a regression can break on its own:

    * the lease is taken at all, on the canonical database; and
    * the write connection is opened INSIDE it -- ``_connect`` issues
      ``PRAGMA journal_mode=WAL``, itself a write that can fail on a lock, so a
      connection opened before the lease leaves the first statement of the
      write path unserialized.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "T_LEASE_PROBE", status="pending")
    _readiness, db_path = task_store._require_ready(repo)

    order: list[tuple[str, str]] = []
    real_lease = task_store.db_writer.write_lease
    real_connect = task_store._connect

    import contextlib as _contextlib

    @_contextlib.contextmanager
    def traced_lease(path, **kwargs):  # type: ignore[no-untyped-def]
        order.append(("lease", str(path)))
        with real_lease(path, **kwargs) as receipt:
            yield receipt

    def traced_connect(path, **kwargs):  # type: ignore[no-untyped-def]
        if not kwargs.get("readonly"):
            order.append(("connect", str(path)))
        return real_connect(path, **kwargs)

    monkeypatch.setattr(task_store.db_writer, "write_lease", traced_lease)
    monkeypatch.setattr(task_store, "_connect", traced_connect)

    _canonical_write_call(path_name, repo)()

    assert ("lease", str(db_path)) in order, (
        f"{path_name} wrote the canonical store without taking the write lease: {order}"
    )
    assert any(
        order[index] == ("lease", str(db_path))
        and index + 1 < len(order)
        and order[index + 1] == ("connect", str(db_path))
        for index in range(len(order))
    ), f"{path_name} opened its write connection outside the lease: {order}"


def test_the_write_lease_is_re_entrant_on_one_thread_and_path(tmp_path: Path) -> None:
    """``db_writer`` documents the lease as re-entrant per (thread, path).

    Verified here rather than trusted: if it were not, any future nesting of
    two leased canonical helpers would self-deadlock for the whole bounded
    timeout instead of proceeding.  The receipt must also SAY it was
    re-entrant, because that is what distinguishes a nested acquisition from a
    second lease taken after a lost release.
    """
    database = tmp_path / "task.sqlite"

    with task_store.db_writer.write_lease(database) as outer:
        assert outer["reentrant"] is False
        with task_store.db_writer.write_lease(database, timeout_s=0.05) as inner:
            assert inner["reentrant"] is True
            assert inner["waited_s"] == 0.0

    # The nested exit released only its own depth: a fresh acquisition after
    # the block is a first acquisition again, not a still-held one.
    with task_store.db_writer.write_lease(database, timeout_s=0.05) as after:
        assert after["reentrant"] is False


def test_nested_write_connections_on_one_path_do_not_deadlock(tmp_path: Path) -> None:
    """No leased canonical helper calls another today, and this is what keeps
    that from becoming a latent deadlock: nesting ``_write_connection`` on one
    path from one thread proceeds, and both connections work."""
    database = tmp_path / "task.sqlite"

    with task_store._write_connection(database) as outer:
        outer.execute("CREATE TABLE IF NOT EXISTS probe(id INTEGER PRIMARY KEY)")
        with task_store._write_connection(database, timeout_s=1.0) as inner:
            assert inner is not outer
            inner.execute("INSERT INTO probe(id) VALUES (1)")
            inner.commit()

    check = sqlite3.connect(database)
    try:
        assert check.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 1
    finally:
        check.close()


def test_a_nested_write_transaction_fails_loudly_instead_of_hanging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honest limit of the re-entrant lease, pinned so it cannot be
    mistaken for full re-entrancy.

    The LEASE is re-entrant; SQLite's writer lock is not.  A nested
    ``_write_connection`` whose outer connection already holds an open write
    transaction is a second writer on the same database, and it surfaces as a
    bounded ``database is locked`` -- never as a silent hang, and never as an
    unserialized write.  This is why every leased helper opens exactly one
    write connection, and why a future one that nests must join the outer
    transaction rather than open its own.
    """
    real_connect = task_store._connect

    def impatient_connect(path, **kwargs):  # type: ignore[no-untyped-def]
        # Bounded so the proof costs milliseconds; the store's own 5000ms
        # default would make this test a five-second sleep.
        kwargs.setdefault("busy_timeout_ms", 50)
        return real_connect(path, **kwargs)

    monkeypatch.setattr(task_store, "_connect", impatient_connect)
    database = tmp_path / "task.sqlite"

    with task_store._write_connection(database) as outer:
        task_store._begin_immediate(outer)
        outer.execute("CREATE TABLE IF NOT EXISTS probe(id INTEGER PRIMARY KEY)")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with task_store._write_connection(database, timeout_s=1.0) as inner:
                task_store._begin_immediate(inner)


def test_mark_terminal_review_runs_inside_an_explicit_write_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card UPDATE, the task_events INSERT and the callback outbox row are
    one transaction whose boundary is stated, not inferred from driver
    behaviour."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _insert_task(repo, "T-EXPLICIT-TXN", status="processing")

    observed: dict[str, object] = {}
    real_txn = task_store._mark_terminal_review_transaction

    def probing_txn(conn, task_id, **kwargs):  # type: ignore[no-untyped-def]
        result = real_txn(conn, task_id, **kwargs)
        observed["isolation_level"] = conn.isolation_level
        return result

    monkeypatch.setattr(task_store, "_mark_terminal_review_transaction", probing_txn)
    ok, state, _enqueued = task_store.mark_terminal_review_with_callback(
        repo,
        "T-EXPLICIT-TXN",
        runner="codex_worker_b891",
        substatus="review_ready",
        evidence={"request_id": "req-1"},
        callback_transition="review_ready",
        callback_provider="codex",
    )

    assert (ok, state) == (True, "review")
    # An explicit-transaction connection: sqlite3 issued no implicit BEGIN.
    assert observed["isolation_level"] is None
    # And the guarded write really landed.
    card = task_store.get_task(repo, "T-EXPLICIT-TXN") or {}
    assert card.get("status") == "review"
    assert card.get("terminal_substatus") == "review_ready"


def _child_mark_review(repo: str, task_id: str, out) -> None:  # pragma: no cover
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from pathlib import Path as _Path

    from aiworkhub import task_store as _task_store

    try:
        ok, state, _enq = _task_store.mark_terminal_review_with_callback(
            _Path(repo),
            task_id,
            runner="codex_worker_b891",
            substatus="review_ready",
            evidence={"request_id": "req-concurrent"},
            callback_transition="review_ready",
            callback_provider="codex",
        )
        out.put(("ok", f"{ok}:{state}"))
    except Exception as exc:  # noqa: BLE001 - the whole point is to catch a lock
        out.put((type(exc).__name__, str(exc)))


def test_concurrent_processes_do_not_surface_database_is_locked(tmp_path: Path) -> None:
    """The regression this whole change exists for.

    Every one of the 452 measured failures reached process_launcher as
    'review_transition_failed:database is locked'. Concurrent supervisor
    PROCESSES must now queue on the lease instead.
    """
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    repo = tmp_path / "repo"
    repo.mkdir()
    task_ids = [f"T-CONC-{i}" for i in range(6)]
    for task_id in task_ids:
        _insert_task(repo, task_id, status="processing")

    out: "mp.Queue[tuple[str, str]]" = ctx.Queue()
    children = [
        ctx.Process(target=_child_mark_review, args=(str(repo), task_id, out))
        for task_id in task_ids
    ]
    for child in children:
        child.start()
    results = [out.get(timeout=120) for _ in task_ids]
    for child in children:
        child.join(timeout=120)

    locked = [r for r in results if "locked" in r[1].lower()]
    assert not locked, f"database is locked resurfaced: {locked}"
    assert all(kind == "ok" and value == "True:review" for kind, value in results), results
    for task_id in task_ids:
        assert (task_store.get_task(repo, task_id) or {}).get("status") == "review"


def test_tasks_status_index_is_created_and_used_by_the_review_seed_query(
    tmp_path: Path,
) -> None:
    """tasks shipped with only sqlite_autoindex_tasks_1; the dispatcher poll
    filters it on status IN ('review','blocked') on every pass.

    Measured on the real 341MB canonical store (4,628 rows):
      before  SCAN tasks / USE TEMP B-TREE FOR ORDER BY  -- median 8.9ms
      after   SEARCH tasks USING INDEX ... (status=?)    -- median 3.5ms
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    _readiness, db_path = task_store._require_ready(repo)

    connection = sqlite3.connect(db_path)
    try:
        names = {str(row[1]) for row in connection.execute("PRAGMA index_list(tasks)")}
        assert "idx_task_store_tasks_status" in names

        plan = " ".join(
            str(row[-1])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT task_id, status, card_json, origin_thread_id FROM tasks "
                "WHERE status IN ('review','blocked') "
                "AND (archived_at IS NULL OR archived_at='') "
                "ORDER BY updated_at ASC, task_id ASC"
            )
        )
        assert "idx_task_store_tasks_status" in plan
        assert "SCAN tasks" not in plan
    finally:
        connection.close()


def test_ensure_task_status_index_migrates_an_existing_store_and_is_idempotent(
    tmp_path: Path,
) -> None:
    """Existing canonical stores must gain the index through the migration
    path, never by writing to a live database directly."""
    database = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending');"
        )
        connection.commit()
        names = {str(row[1]) for row in connection.execute("PRAGMA index_list(tasks)")}
        assert "idx_task_store_tasks_status" not in names

        assert task_store.ensure_task_status_index(connection) is True
        names = {str(row[1]) for row in connection.execute("PRAGMA index_list(tasks)")}
        assert "idx_task_store_tasks_status" in names

        # Idempotent: a second migration pass reports no change.
        assert task_store.ensure_task_status_index(connection) is False
    finally:
        connection.close()


def test_ensure_task_status_index_declines_a_table_without_status(tmp_path: Path) -> None:
    """Fail closed rather than raise against an unexpected/older shape."""
    connection = sqlite3.connect(tmp_path / "odd.sqlite")
    try:
        connection.executescript("CREATE TABLE tasks (task_id TEXT PRIMARY KEY);")
        connection.commit()
        assert task_store.ensure_task_status_index(connection) is False
    finally:
        connection.close()


def test_write_lease_converts_a_busy_timeout_failure_into_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact measured failure, made deterministic.

    In production a waiter at the back of the single-writer queue burnt its
    whole 5000ms busy_timeout and surfaced 'database is locked' (5/72 at 12
    processes x ~120ms hold). Here the timeout is shortened to 200ms and the
    holder holds for 1s, so without the lease the second writer MUST fail and
    with the lease it MUST simply wait its turn. Two threads, not one: the
    lease is re-entrant within a single (process, thread, path).
    """
    import threading
    import time as _time

    database = tmp_path / "task.sqlite"
    seed = task_store._connect(database)
    try:
        seed.execute("CREATE TABLE probe(a)")
        seed.commit()
    finally:
        seed.close()

    real_connect = task_store._connect

    def short_busy_timeout_connect(path, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("busy_timeout_ms", 200)
        return real_connect(path, **kwargs)

    monkeypatch.setattr(task_store, "_connect", short_busy_timeout_connect)

    holder_inside = threading.Event()
    outcome: dict[str, str | None] = {}

    def holder() -> None:
        with task_store._write_connection(database) as conn:
            task_store._begin_immediate(conn)
            conn.execute("INSERT INTO probe VALUES (1)")
            holder_inside.set()
            _time.sleep(1.0)  # 5x the 200ms busy_timeout
            conn.commit()

    def waiter() -> None:
        assert holder_inside.wait(timeout=30)
        try:
            with task_store._write_connection(database) as conn:
                task_store._begin_immediate(conn)
                conn.execute("INSERT INTO probe VALUES (2)")
                conn.commit()
            outcome["waiter"] = None
        except Exception as exc:  # noqa: BLE001 - catching the lock is the point
            outcome["waiter"] = f"{type(exc).__name__}: {exc}"

    threads = [threading.Thread(target=holder), threading.Thread(target=waiter)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert outcome["waiter"] is None, (
        f"the second writer failed instead of waiting: {outcome['waiter']}"
    )
    check = sqlite3.connect(database)
    try:
        assert check.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 2
    finally:
        check.close()
