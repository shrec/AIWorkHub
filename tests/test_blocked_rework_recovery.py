from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from aiworkhub import task_store


_TERMINAL_REVIEW_EVIDENCE = {
    "validation": [
        {"command": "pytest -q", "returncode": 1, "stdout": "1 failed", "stderr": ""}
    ],
    "required_outputs": [],
    "changed_path_hashes": {
        "src/aiworkhub/task_store.py": "abc123hash",
    },
}


def test_recover_blocked_terminal_failure_retained_delta_without_required_outputs(
    tmp_path: Path,
) -> None:
    repo = _setup_repo(tmp_path)
    request_id = "a" * 32
    workspace = repo / ".aiworkhub" / "runtime" / "worktrees" / request_id / "worktree"
    baseline = repo / "src" / "aiworkhub" / "task_store.py"
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes(b"baseline\n")
    baseline_digest = hashlib.sha256(baseline.read_bytes()).hexdigest()
    baseline_token = f"file:664:{baseline_digest}"
    candidate = workspace / "src" / "aiworkhub" / "task_store.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"retained candidate\n")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    authority = {
        "schema_id": "aiworkhub.python_candidate_authority.v1",
        "sources": [
            {
                "path": "src/aiworkhub/task_store.py",
                "state": "modified",
                "bytes_sha256": digest,
            }
        ],
    }
    workspace_metadata = {
        "request_id": request_id,
        "repo": str(repo),
        "path": str(workspace),
        "allowed_writes": ["src/aiworkhub/task_store.py"],
        "parent_baseline": {"src/aiworkhub/task_store.py": baseline_token},
        "base_oid": "b" * 40,
        "python_candidate_authority": authority,
    }
    evidence = {
        "required_outputs": [],
        "changed_paths": ["src/aiworkhub/task_store.py"],
        "changed_path_hashes": {"src/aiworkhub/task_store.py": digest},
        "request_identity": {
            "request_id": request_id,
            "task_id": "BLOCKED_RETAINED_EMPTY_OUTPUTS",
            "runner": "codex_worker_test",
            "topic": "aiworkhub_blocked_rework_recovery",
            "repo": str(repo),
            "claim_epoch": 1,
            "allowed_writes": ["src/aiworkhub/task_store.py"],
            "parent_baseline": {"src/aiworkhub/task_store.py": baseline_token},
            "base_oid": "b" * 40,
        },
        "workspace": workspace_metadata,
        "python_candidate_authority": authority,
    }
    _insert_processing_task(
        repo, "BLOCKED_RETAINED_EMPTY_OUTPUTS", request_id=request_id,
    )
    assert task_store.mark_terminal_failure(
        repo,
        "BLOCKED_RETAINED_EMPTY_OUTPUTS",
        runner="codex_worker_test",
        substatus="finalize_failed",
        evidence=evidence,
        request_id=request_id,
        claim_epoch=1,
    ) == (True, "blocked")

    candidate.write_bytes(b"tampered retained candidate\n")
    assert task_store.recover_blocked_rework(
        repo,
        "BLOCKED_RETAINED_EMPTY_OUTPUTS",
        actor="coordinator",
        feedback_reason="NeedFix: replay retained candidate",
        validation_only_replay=True,
    ) == (False, "retained_terminal_candidate_hash_mismatch")
    blocked_card = _get_card(repo, "BLOCKED_RETAINED_EMPTY_OUTPUTS")
    assert blocked_card["status"] == "blocked"
    assert "rework_predecessor" not in blocked_card

    candidate.write_bytes(b"retained candidate\n")
    assert task_store.recover_blocked_rework(
        repo,
        "BLOCKED_RETAINED_EMPTY_OUTPUTS",
        actor="coordinator",
        feedback_reason="NeedFix: replay retained candidate",
        validation_only_replay=True,
    ) == (True, "recovered")
    card = _get_card(repo, "BLOCKED_RETAINED_EMPTY_OUTPUTS")
    assert card["rework_predecessor"]["request_id"] == request_id
    assert card["rework_predecessor"]["changed_path_hashes"] == {
        "src/aiworkhub/task_store.py": digest
    }
    assert card["validation_only_replay_authorization"]["next_claim_epoch"] == 2


def _setup_repo(tmp_path: Path) -> Path:
    """Create and initialize a minimal repo for task_store tests."""
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    return repo


def _insert_processing_task(repo: Path, task_id: str, *, request_id: str) -> None:
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-08-06T00:00:00+00:00"
    card = {
        "task_id": task_id,
        "runner": "codex_worker_test",
        "topic": "aiworkhub_blocked_rework_recovery",
        "allowed_writes": ["src/aiworkhub/task_store.py"],
        "claim_epoch": 1,
        "launch_request_id": request_id,
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, "
            "started_at) VALUES (?, ?, ?, 'processing', 'in_progress', '', '', ?, ?, ?, ?, ?, ?)",
            (task_id, "codex_worker_test", "aiworkhub_blocked_rework_recovery", json.dumps(card),
             now, now, "codex_worker_test", now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_blocked_task(
    repo: Path,
    task_id: str,
    *,
    runner: str = "codex_worker_test",
    terminal_substatus: str = "validation_failed",
    terminal_evidence: dict | None = None,
    reject_review_reason: str = "",
    extra_card: dict | None = None,
    worker_status: str = "validation_failed",
    claimed_by: str = "",
    topic: str = "aiworkhub_blocked_rework_recovery",
) -> None:
    """Insert a blocked task with terminal-review predecessor evidence."""
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-08-06T00:00:00+00:00"

    evidence = dict(terminal_evidence or _TERMINAL_REVIEW_EVIDENCE)
    card = {
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "mode": "",
        "allowed_writes": ["src/aiworkhub/task_store.py"],
        "objective": "Implement blocked rework recovery",
        "terminal_review": {
            "substatus": terminal_substatus,
            "evidence": evidence,
            "deterministic_verification": {
                "applicable": True,
                "pass": False,
                "substatus": terminal_substatus,
                "reason": "evidence_verdict_failed",
                "claim_epoch": 0,
                "evidence_verdict": {
                    "passed": False,
                    "validation_count": 1,
                    "failed_validation_count": 1,
                    "required_output_count": 0,
                    "missing_required_output_count": 0,
                },
            },
            "recorded_at": now,
            "runner": runner,
            "claim_epoch": 0,
        },
        "terminal_substatus": terminal_substatus,
        "deterministic_verification": {
            "applicable": True,
            "pass": False,
            "substatus": terminal_substatus,
            "reason": "evidence_verdict_failed",
            "claim_epoch": 0,
            "evidence_verdict": {"passed": False, "validation_count": 1, "failed_validation_count": 1},
        },
        "blocker_reason": f"{terminal_substatus}: tests failed",
        "blocked_at": now,
        "blocked_by": runner,
        "claim_epoch": 0,
    }
    if reject_review_reason:
        card["reject_review"] = {
            "to": "pending",
            "reason": reject_review_reason,
            "recorded_at": now,
        }
    if extra_card:
        card.update(extra_card)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, started_at, "
            "completed_at) "
            "VALUES (?, ?, ?, 'blocked', ?, '', '', "
            "?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                runner,
                topic,
                worker_status,
                json.dumps(card),
                now,
                now,
                claimed_by,
                now if claimed_by else "",
                now if claimed_by else "",
                now,
            ),
        )
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, 'terminal_review', ?, ?, ?)",
            (
                task_id,
                runner,
                json.dumps({"substatus": terminal_substatus, "evidence": evidence}),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _get_card(repo: Path, task_id: str) -> dict:
    task = task_store.get_task(repo, task_id)
    assert task is not None, f"task {task_id} not found"
    return task


# ---------------------------------------------------------------------------
# Successful recovery
# ---------------------------------------------------------------------------


def test_recover_blocked_validation_failed_with_feedback_requeues_to_pending(
    tmp_path: Path,
) -> None:
    """A blocked task with retained predecessor evidence and residual feedback
    is safely requeued to pending, preserving history."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "BLOCKED_NEEDFIX_01",
        terminal_substatus="validation_failed",
        reject_review_reason="NeedFix: handle edge case when input is empty",
    )

    ok, state = task_store.recover_blocked_rework(
        repo,
        "BLOCKED_NEEDFIX_01",
        actor="coordinator",
        feedback_reason="NeedFix: handle edge case when input is empty",
    )
    assert (ok, state) == (True, "recovered")

    task = _get_card(repo, "BLOCKED_NEEDFIX_01")
    assert task["status"] == "pending"
    assert task["worker_status"] == "unclaimed"
    assert task.get("claimed_by") in (None, "")
    assert task.get("claimed_at") in (None, "")
    assert task.get("claim_epoch") == 1
    assert task.get("recovery_epoch") == 1
    assert task.get("recovered_by") == "coordinator"
    assert task.get("recovery_feedback") == "NeedFix: handle edge case when input is empty"

    # Predecessor evidence is preserved.
    pred = task.get("recovery_predecessor")
    assert isinstance(pred, dict)
    assert pred["terminal_substatus"] == "validation_failed"
    assert pred["changed_path_hashes"] == {
        "src/aiworkhub/task_store.py": "abc123hash",
    }

    # Audit event is recorded.
    events = task_store.get_task_events(repo, "BLOCKED_NEEDFIX_01")
    event_names = [e["event"] for e in events]
    assert "blocked_rework_recovery" in event_names
    # Terminal review event is preserved.
    assert "terminal_review" in event_names


def test_recover_blocked_worker_failed_with_feedback_requeues_to_pending(
    tmp_path: Path,
) -> None:
    """A worker_failed blocked task with feedback is recoverable."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "BLOCKED_WORKER_FAIL",
        terminal_substatus="worker_failed",
        reject_review_reason="NeedFix: worker crashed due to OOM",
    )

    ok, state = task_store.recover_blocked_rework(
        repo,
        "BLOCKED_WORKER_FAIL",
        actor="coordinator",
        feedback_reason="NeedFix: worker crashed due to OOM",
    )
    assert (ok, state) == (True, "recovered")
    assert _get_card(repo, "BLOCKED_WORKER_FAIL")["status"] == "pending"


def test_quality_review_recovery_requires_packet_bound_relaunch(
    tmp_path: Path,
) -> None:
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "BLOCKED_REVIEWER",
        topic="quality_review",
        terminal_substatus="worker_failed",
        reject_review_reason="Retry exact reviewer packet",
        extra_card={
            "quality_review": {
                "target_request_id": "a" * 32,
                "target_task_id": "TARGET_TASK",
                "packet_sha256": "b" * 64,
            }
        },
    )

    ok, state = task_store.recover_blocked_rework(
        repo,
        "BLOCKED_REVIEWER",
        actor="coordinator",
        feedback_reason="Retry exact reviewer packet",
    )

    assert (ok, state) == (
        False,
        "quality_review_recovery_requires_bound_relaunch",
    )
    assert _get_card(repo, "BLOCKED_REVIEWER")["status"] == "blocked"
    events = task_store.get_task_events(repo, "BLOCKED_REVIEWER")
    assert "blocked_rework_recovery" not in {event["event"] for event in events}


def _missing_rework_predecessor(repo: Path, tmp_path: Path) -> dict:
    request_id = "a" * 32
    return {
        "request_id": request_id,
        "changed_path_hashes": {"src/aiworkhub/task_store.py": "b" * 64},
        "workspace": {
            "request_id": request_id,
            "repo": str(repo),
            "path": str((tmp_path / "removed-worktree").resolve()),
            "home": str((tmp_path / "removed-home").resolve()),
            "allowed_writes": ["src/aiworkhub/task_store.py"],
        },
    }


def test_recover_missing_predecessor_can_explicitly_use_clean_root(
    tmp_path: Path,
) -> None:
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "CLEAN_ROOT_MISSING_PREDECESSOR",
        terminal_substatus="validation_failed",
        reject_review_reason="Reconstruct the bounded candidate on current HEAD",
        extra_card={
            "rework_predecessor": _missing_rework_predecessor(repo, tmp_path),
        },
    )

    ok, state = task_store.recover_blocked_rework(
        repo,
        "CLEAN_ROOT_MISSING_PREDECESSOR",
        actor="coordinator",
        feedback_reason="Reconstruct the bounded candidate on current HEAD",
        clean_root_if_predecessor_missing=True,
    )

    assert (ok, state) == (True, "recovered")
    task = _get_card(repo, "CLEAN_ROOT_MISSING_PREDECESSOR")
    assert "rework_predecessor" not in task
    assert task["recovery_mode"] == "clean_root_missing_predecessor"
    authorization = task["clean_root_recovery_authorization"]
    assert authorization["predecessor_request_id"] == "a" * 32
    assert authorization["changed_path_hashes"] == {
        "src/aiworkhub/task_store.py": "b" * 64
    }
    events = task_store.get_task_events(repo, "CLEAN_ROOT_MISSING_PREDECESSOR")
    assert "blocked_rework_recovery" in {event["event"] for event in events}


def test_already_recovered_missing_predecessor_can_be_clean_root_authorized(
    tmp_path: Path,
) -> None:
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "CLEAN_ROOT_AFTER_RECOVERY",
        terminal_substatus="validation_failed",
        reject_review_reason="Reconstruct on current HEAD",
        extra_card={
            "rework_predecessor": _missing_rework_predecessor(repo, tmp_path),
        },
    )
    assert task_store.recover_blocked_rework(
        repo,
        "CLEAN_ROOT_AFTER_RECOVERY",
        actor="coordinator",
        feedback_reason="Reconstruct on current HEAD",
    ) == (True, "recovered")

    ok, state = task_store.recover_blocked_rework(
        repo,
        "CLEAN_ROOT_AFTER_RECOVERY",
        actor="coordinator",
        feedback_reason="Reconstruct on current HEAD",
        clean_root_if_predecessor_missing=True,
    )

    assert (ok, state) == (True, "recovered_clean_root")
    task = _get_card(repo, "CLEAN_ROOT_AFTER_RECOVERY")
    assert "rework_predecessor" not in task
    assert task["recovery_mode"] == "clean_root_missing_predecessor"
    events = task_store.get_task_events(repo, "CLEAN_ROOT_AFTER_RECOVERY")
    assert "blocked_rework_clean_root_authorized" in {
        event["event"] for event in events
    }


def test_clean_root_recovery_is_incompatible_with_validation_only_replay(
    tmp_path: Path,
) -> None:
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "CLEAN_ROOT_REPLAY_FORBIDDEN",
        terminal_substatus="validation_failed",
        reject_review_reason="Replay exact retained bytes",
        extra_card={
            "rework_predecessor": _missing_rework_predecessor(repo, tmp_path),
        },
    )

    ok, state = task_store.recover_blocked_rework(
        repo,
        "CLEAN_ROOT_REPLAY_FORBIDDEN",
        actor="coordinator",
        feedback_reason="Replay exact retained bytes",
        validation_only_replay=True,
        clean_root_if_predecessor_missing=True,
    )

    assert (ok, state) == (
        False,
        "clean_root_incompatible_with_validation_only_replay",
    )


# ---------------------------------------------------------------------------
# Idempotent retry
# ---------------------------------------------------------------------------


def test_recover_already_recovered_task_returns_idempotent_already_recovered(
    tmp_path: Path,
) -> None:
    """Calling recover on an already-recovered (pending) task returns
    already_recovered without duplicating audit history."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "IDEMPOTENT_01",
        terminal_substatus="validation_failed",
        reject_review_reason="NeedFix: rework needed",
    )

    # First recovery.
    ok, state = task_store.recover_blocked_rework(
        repo, "IDEMPOTENT_01", actor="coordinator",
        feedback_reason="NeedFix: rework needed",
    )
    assert (ok, state) == (True, "recovered")
    events_after_first = len(task_store.get_task_events(repo, "IDEMPOTENT_01"))

    # Second call -- idempotent.
    ok, state = task_store.recover_blocked_rework(
        repo, "IDEMPOTENT_01", actor="coordinator",
        feedback_reason="NeedFix: rework needed",
    )
    assert (ok, state) == (True, "already_recovered")

    # No duplicate audit event.
    events_after_second = len(task_store.get_task_events(repo, "IDEMPOTENT_01"))
    assert events_after_second == events_after_first

    # Task is still pending.
    assert _get_card(repo, "IDEMPOTENT_01")["status"] == "pending"


def test_recover_allows_second_cycle_after_reblock(
    tmp_path: Path,
) -> None:
    """A task that was recovered, re-claimed, and blocked again can be
    recovered a second time (new recovery cycle)."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "CYCLE_02",
        terminal_substatus="validation_failed",
        reject_review_reason="NeedFix: first attempt",
    )

    # First recovery.
    ok, _ = task_store.recover_blocked_rework(
        repo, "CYCLE_02", actor="coordinator",
        feedback_reason="NeedFix: first attempt",
    )
    assert ok

    # Simulate re-block (worker picked up, failed again).
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET status='blocked', worker_status='validation_failed' "
            "WHERE task_id='CYCLE_02'"
        )
        conn.commit()
    finally:
        conn.close()

    # Second recovery should succeed (new cycle).
    ok, state = task_store.recover_blocked_rework(
        repo, "CYCLE_02", actor="coordinator",
        feedback_reason="NeedFix: second attempt",
    )
    assert (ok, state) == (True, "recovered")
    assert _get_card(repo, "CYCLE_02")["status"] == "pending"


# ---------------------------------------------------------------------------
# Fail closed: hard blockers
# ---------------------------------------------------------------------------


def test_recover_dependency_blocked_task_fails_closed(
    tmp_path: Path,
) -> None:
    """A dependency-blocked task must not be silently recovered."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "DEP_BLOCKED_01",
        terminal_substatus="dependency_blocked",
        reject_review_reason="waiting for prerequisite",
    )

    ok, state = task_store.recover_blocked_rework(
        repo, "DEP_BLOCKED_01", actor="coordinator",
        feedback_reason="waiting for prerequisite",
    )
    assert (ok, state) == (False, "hard_blocker:dependency_blocked")
    assert _get_card(repo, "DEP_BLOCKED_01")["status"] == "blocked"


def test_recover_scope_rejected_task_fails_closed(
    tmp_path: Path,
) -> None:
    """A scope-rejected task must not be silently recovered."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "SCOPE_REJECTED_01",
        terminal_substatus="scope_rejected",
        reject_review_reason="out of scope",
    )

    ok, state = task_store.recover_blocked_rework(
        repo, "SCOPE_REJECTED_01", actor="coordinator",
        feedback_reason="out of scope",
    )
    assert (ok, state) == (False, "hard_blocker:scope_rejected")
    assert _get_card(repo, "SCOPE_REJECTED_01")["status"] == "blocked"


def test_recover_generic_blocked_substatus_fails_closed(
    tmp_path: Path,
) -> None:
    """A task with the generic 'blocked' terminal substatus fails closed
    since its blocker nature is unknown."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "GENERIC_BLOCKED_01",
        terminal_substatus="blocked",
        reject_review_reason="unspecified blocker",
    )

    ok, state = task_store.recover_blocked_rework(
        repo, "GENERIC_BLOCKED_01", actor="coordinator",
        feedback_reason="unspecified blocker",
    )
    assert (ok, state) == (False, "hard_blocker:blocked")
    assert _get_card(repo, "GENERIC_BLOCKED_01")["status"] == "blocked"


# ---------------------------------------------------------------------------
# Fail closed: missing evidence
# ---------------------------------------------------------------------------


def test_recover_without_terminal_review_evidence_fails_closed(
    tmp_path: Path,
) -> None:
    """A blocked task without retained terminal_review predecessor fails."""
    repo = _setup_repo(tmp_path)
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-08-06T00:00:00+00:00"
    card = {"task_id": "NO_EVIDENCE_01", "runner": "test", "topic": "test"}
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at, completed_at) "
            "VALUES ('NO_EVIDENCE_01', 'test', 'test', 'blocked', 'blocked', "
            "?, ?, ?, ?)",
            (json.dumps(card), now, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    ok, state = task_store.recover_blocked_rework(
        repo, "NO_EVIDENCE_01", actor="coordinator",
        feedback_reason="some feedback",
    )
    assert (ok, state) == (False, "no_retained_predecessor_evidence")
    assert _get_card(repo, "NO_EVIDENCE_01")["status"] == "blocked"


def test_recover_without_feedback_reason_fails_closed(
    tmp_path: Path,
) -> None:
    """A blocked task with predecessor evidence but no residual feedback fails."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "NO_FEEDBACK_01",
        terminal_substatus="validation_failed",
    )

    ok, state = task_store.recover_blocked_rework(
        repo, "NO_FEEDBACK_01", actor="coordinator",
        feedback_reason="",
    )
    assert (ok, state) == (False, "no_residual_feedback")
    assert _get_card(repo, "NO_FEEDBACK_01")["status"] == "blocked"


def test_recover_with_reject_review_feedback_from_card_succeeds(
    tmp_path: Path,
) -> None:
    """When feedback_reason is empty but card contains reject_review reason,
    recovery succeeds using the card-level feedback."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "CARD_FEEDBACK_01",
        terminal_substatus="validation_failed",
        reject_review_reason="NeedFix: card-level feedback present",
    )

    # feedback_reason param is empty, but card has reject_review.reason.
    ok, state = task_store.recover_blocked_rework(
        repo, "CARD_FEEDBACK_01", actor="coordinator",
        feedback_reason="",
    )
    assert (ok, state) == (True, "recovered")
    assert _get_card(repo, "CARD_FEEDBACK_01")["status"] == "pending"


# ---------------------------------------------------------------------------
# Fail closed: live claim
# ---------------------------------------------------------------------------


def test_recover_live_claimed_task_fails_closed(
    tmp_path: Path,
) -> None:
    """A task with an active claim must not be silently recovered."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "LIVE_CLAIM_01",
        terminal_substatus="validation_failed",
        reject_review_reason="NeedFix: rework",
        worker_status="claimed",
        claimed_by="some_worker",
    )

    ok, state = task_store.recover_blocked_rework(
        repo, "LIVE_CLAIM_01", actor="coordinator",
        feedback_reason="NeedFix: rework",
    )
    assert (ok, state) == (False, "live_claim_detected")


# ---------------------------------------------------------------------------
# Fail closed: non-blocked
# ---------------------------------------------------------------------------


def test_recover_pending_task_fails_closed(tmp_path: Path) -> None:
    """A pending (never-blocked) task must not be recovered."""
    repo = _setup_repo(tmp_path)
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-08-06T00:00:00+00:00"
    card = {"task_id": "FRESH_TASK", "runner": "test", "topic": "test"}
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at) "
            "VALUES ('FRESH_TASK', 'test', 'test', 'pending', 'unclaimed', ?, ?, ?)",
            (json.dumps(card), now, now),
        )
        conn.commit()
    finally:
        conn.close()

    ok, state = task_store.recover_blocked_rework(
        repo, "FRESH_TASK", actor="coordinator",
        feedback_reason="some feedback",
    )
    assert (ok, state) == (False, "not_blocked:current=pending")


def test_recover_processing_task_fails_closed(tmp_path: Path) -> None:
    """A processing task must not be recovered."""
    repo = _setup_repo(tmp_path)
    _readiness, db_path = task_store._require_ready(repo)
    now = "2026-08-06T00:00:00+00:00"
    card = {"task_id": "PROCESSING_TASK", "runner": "test", "topic": "test"}
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "card_json, created_at, updated_at, claimed_by, claimed_at) "
            "VALUES ('PROCESSING_TASK', 'test', 'test', 'processing', 'claimed', "
            "?, ?, ?, 'worker_1', ?)",
            (json.dumps(card), now, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    ok, state = task_store.recover_blocked_rework(
        repo, "PROCESSING_TASK", actor="coordinator",
        feedback_reason="some feedback",
    )
    assert ok is False
    assert "not_blocked" in state


# ---------------------------------------------------------------------------
# Fail closed: task not found
# ---------------------------------------------------------------------------


def test_recover_unknown_task_fails_closed(tmp_path: Path) -> None:
    """Recovering a non-existent task returns task_not_found."""
    repo = _setup_repo(tmp_path)
    ok, state = task_store.recover_blocked_rework(
        repo, "NONEXISTENT_TASK", actor="coordinator",
        feedback_reason="some feedback",
    )
    assert (ok, state) == (False, "task_not_found")


# ---------------------------------------------------------------------------
# Regression: NeedFix foundation scenario
# ---------------------------------------------------------------------------


def test_regression_blocked_needfix_foundation_scenario_preserves_lineage(
    tmp_path: Path,
) -> None:
    """Models the blocked NeedFix foundation scenario: a task undergoes
    terminal_review -> reject_review with NeedFix disposition, then is
    recovered for rework without creating a replacement task."""
    repo = _setup_repo(tmp_path)
    task_id = "NEEDFIX_FOUNDATION"

    # Step 1: Insert a blocked task with full NeedFix evidence.
    _insert_blocked_task(
        repo,
        task_id,
        terminal_substatus="validation_failed",
        reject_review_reason="NeedFix: fix the edge case in validation logic",
        extra_card={
            "predecessor_task_id": task_id,  # same ID, rework cycle
            "predecessor_sha256": "deadbeefcafe",
            "residual_feedback": "The validation should handle empty string input",
            "terminal_outcome": "validation_failed",
        },
    )

    # Step 2: Recover.
    ok, state = task_store.recover_blocked_rework(
        repo, task_id, actor="coordinator",
        feedback_reason="NeedFix: fix the edge case in validation logic",
    )
    assert (ok, state) == (True, "recovered")

    task = _get_card(repo, task_id)

    # Step 3: Task identity is preserved (no replacement).
    assert task["task_id"] == task_id
    assert task["runner"] == "codex_worker_test"
    assert task["topic"] == "aiworkhub_blocked_rework_recovery"

    # Step 4: Task is pending for rework.
    assert task["status"] == "pending"
    assert task["worker_status"] == "unclaimed"

    # Step 5: Lineage is preserved.
    assert task["claim_epoch"] == 1
    assert task["recovery_epoch"] == 1

    # Step 6: Predecessor evidence is pinned.
    pred = task["recovery_predecessor"]
    assert pred["terminal_substatus"] == "validation_failed"
    assert pred["changed_path_hashes"] == {
        "src/aiworkhub/task_store.py": "abc123hash",
    }

    # Step 7: Durable extra fields are preserved while current-episode truth
    # is cleared from card_json.
    assert task.get("predecessor_sha256") == "deadbeefcafe"
    assert task.get("residual_feedback") == "The validation should handle empty string input"
    assert task.get("terminal_outcome") in (None, "")

    # Step 8: Episode fields are cleared.
    assert task.get("terminal_substatus") in (None, "")
    assert task.get("blocker_reason") in (None, "")
    assert task.get("launch_error") in (None, "")
    assert task.get("launch_failed") in (None, "")

    # Step 9: Audit trail is intact.
    events = task_store.get_task_events(repo, task_id)
    event_names = [e["event"] for e in events]
    assert "terminal_review" in event_names
    assert "blocked_rework_recovery" in event_names

    # Step 10: Idempotent retry.
    ok2, state2 = task_store.recover_blocked_rework(
        repo, task_id, actor="coordinator",
        feedback_reason="NeedFix: fix the edge case in validation logic",
    )
    assert (ok2, state2) == (True, "already_recovered")


# ---------------------------------------------------------------------------
# History / evidence preservation
# ---------------------------------------------------------------------------


def test_recovery_preserves_original_terminal_review_in_events(
    tmp_path: Path,
) -> None:
    """Terminal evidence remains durable without contaminating a new episode."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "PRESERVE_EVIDENCE",
        terminal_substatus="timed_out",
        reject_review_reason="NeedFix: increase timeout",
    )

    task_store.recover_blocked_rework(
        repo, "PRESERVE_EVIDENCE", actor="coordinator",
        feedback_reason="NeedFix: increase timeout",
    )

    task = _get_card(repo, "PRESERVE_EVIDENCE")
    assert task.get("terminal_review") is None
    assert task.get("deterministic_verification") is None
    assert task["recovery_predecessor"]["terminal_substatus"] == "timed_out"

    events = task_store.get_task_events(repo, "PRESERVE_EVIDENCE")
    terminal_events = [event for event in events if event["event"] == "terminal_review"]
    assert len(terminal_events) == 1


def test_recovery_bumps_claim_epoch_for_fresh_lineage(
    tmp_path: Path,
) -> None:
    """Each recovery cycle produces a strictly increasing claim_epoch."""
    repo = _setup_repo(tmp_path)
    _insert_blocked_task(
        repo,
        "EPOCH_BUMP",
        terminal_substatus="validation_failed",
        reject_review_reason="NeedFix: fix bug",
        extra_card={"claim_epoch": 2},
    )

    task_store.recover_blocked_rework(
        repo, "EPOCH_BUMP", actor="coordinator",
        feedback_reason="NeedFix: fix bug",
    )

    task = _get_card(repo, "EPOCH_BUMP")
    assert task["claim_epoch"] == 3
    assert task["recovery_epoch"] == 3


def test_recovery_accepts_launch_failed_and_timed_out_substatuses(
    tmp_path: Path,
) -> None:
    """Launch failures and timeouts are rework-eligible."""
    for substatus in ("launch_failed", "timed_out", "token_budget_exceeded"):
        parent = tmp_path / substatus
        parent.mkdir()
        repo = _setup_repo(parent)
        task_id = f"RECOVER_{substatus.upper()}"
        _insert_blocked_task(
            repo,
            task_id,
            terminal_substatus=substatus,
            reject_review_reason=f"NeedFix: {substatus} recovery",
        )
        ok, state = task_store.recover_blocked_rework(
            repo, task_id, actor="coordinator",
            feedback_reason=f"NeedFix: {substatus} recovery",
        )
        assert (ok, state) == (True, "recovered"), f"failed for {substatus}"
        assert _get_card(repo, task_id)["status"] == "pending"
