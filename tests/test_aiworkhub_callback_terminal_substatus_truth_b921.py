"""B921: the compact Task MCP callback must report the AUTHORITATIVE
terminal substatus, never a hardcoded ``"review_ready"`` literal.

B917 reproduction: a worker recorded ``terminal_review.substatus`` /
``card["terminal_substatus"]`` = ``"validation_failed"`` (lifecycle bucket
``status="review"``), yet the delivered compact callback read
``review_ready`` -- because ``task_engine.mark_terminal_review`` enqueued
the outbox row with a hardcoded ``"review_ready"`` transition regardless of
the actual ``substatus`` argument it was called with.

These tests exercise the REAL chain end-to-end against a real
``.aiworkhub/tasking/task_queue.sqlite``: ``task_engine.mark_terminal_review``
-> ``task_store`` row + card -> ``callback_store.callback_outbox.transition``
-> ``callback_store.claim_pending_callback_batch`` ->
``callback_bridge.CallbackBatch.as_prompt_members`` ->
``callback_bridge.build_batch_callback_prompt`` -- never a mocked shortcut,
so a regression that only fixes one layer (e.g. the outbox row) but leaves
the rendered prompt wrong would still be caught.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import callback_bridge, callback_store, task_engine, task_store  # noqa: E402


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    return repo


def _seed_processing_task(
    repo: Path, task_id: str, *, origin_thread_id: str, runner: str, topic: str = "task_mcp",
) -> None:
    """Insert one canonical task row directly in ``processing`` -- the sole
    legal source status for a terminal-review transition -- with
    ``callback_required`` true and a bound ``origin_thread_id``, exactly the
    shape ``ProcessManager``/the reconciler leave behind before recording a
    terminal outcome."""
    _readiness, db_path = task_store._require_ready(repo)
    conn = task_store._connect(db_path)
    try:
        now = datetime.now(timezone.utc).isoformat()
        card_json = json.dumps({
            "task_id": task_id,
            "callback_required": True,
            "coordinator_provider": "claude",
            "claim_epoch": 1,
            "launch_request_id": f"request-{task_id.lower()}",
        })
        conn.execute(
            "INSERT INTO tasks("
            "  task_id, runner, topic, status, worker_status, card_json,"
            "  created_at, updated_at, claimed_by, origin_thread_id"
            ") VALUES (?, ?, ?, 'processing', 'processing', ?, ?, ?, ?, ?)",
            (task_id, runner, topic, card_json, now, now, runner, origin_thread_id),
        )
        conn.commit()
    finally:
        conn.close()


def _read_outbox_transition(repo: Path, task_id: str) -> str:
    _readiness, db_path = task_store._require_ready(repo)
    conn = task_store._connect(db_path, readonly=True)
    try:
        row = conn.execute(
            "SELECT transition FROM callback_outbox WHERE task_id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"no callback_outbox row enqueued for {task_id}"
    return str(row["transition"])


def _claim_and_render_prompt(repo: Path) -> str:
    """Drive the REAL delivery-formation path (claim batch -> CallbackBatch
    -> as_prompt_members -> build_batch_callback_prompt) and return exactly
    the compact prompt text that would be sent to the coordinator."""
    _readiness, db_path = task_store._require_ready(repo)
    conn = task_store._connect(db_path)
    try:
        claimed = callback_store.claim_pending_callback_batch(conn, lease_seconds=120, max_members=25)
    finally:
        conn.close()
    assert claimed is not None, "expected one claimable callback batch"
    batch = callback_bridge._batch_from_claim_result(claimed)
    return callback_bridge.build_batch_callback_prompt(batch.as_prompt_members())


def test_validation_failed_callback_never_says_review_ready(tmp_path):
    """The exact B917 repro: terminal_substatus=validation_failed must
    render as validation_failed in the delivered compact callback, never
    review_ready merely because the task sits in the generic review queue."""
    repo = _init_repo(tmp_path)
    task_id = "TASK_B921_VALIDATION_FAILED"
    _seed_processing_task(repo, task_id, origin_thread_id="thread-b921-a", runner="claude_worker_b921")

    result = task_engine.mark_terminal_review(
        repo, task_id, "claude_worker_b921", "validation_failed", evidence={},
    )
    assert result["ok"] is True
    assert result["callback_enqueued"] is True

    card = task_store.get_task(repo, task_id)
    assert card["status"] == "review"  # generic lifecycle bucket (the trap)
    assert card["terminal_substatus"] == "validation_failed"  # authoritative truth
    assert card["terminal_review"]["substatus"] == "validation_failed"

    # The outbox row itself must carry the real transition, not the trap.
    transition = _read_outbox_transition(repo, task_id)
    assert transition == "validation_failed"
    assert transition != "review_ready"

    prompt = _claim_and_render_prompt(repo)
    assert prompt == f"Task MCP: {task_id} → validation_failed"
    assert "review_ready" not in prompt


@pytest.mark.parametrize(
    ("substatus", "expected_transition"),
    [
        ("review_ready", "review_ready"),
        ("blocked", "blocked"),
        ("launch_failed", "launch_failed"),
        ("validation_failed", "validation_failed"),
        ("scope_rejected", "scope_rejected"),
        ("cancelled", "cancelled"),
        ("process_lost", "blocked"),
    ],
)
def test_each_terminal_substatus_survives_distinctly_to_the_delivered_callback(
    tmp_path, substatus, expected_transition,
):
    """review_ready, blocked, launch_failed, scope_rejected, cancelled,
    process_lost and validation_failed must remain distinct terminal
    substatuses end-to-end: each records its OWN substatus on the card, and
    each renders its own correctly-classified callback state -- never all
    collapsing to review_ready because the lifecycle bucket is "review"."""
    repo = _init_repo(tmp_path)
    task_id = f"TASK_B921_{substatus.upper()}"
    _seed_processing_task(repo, task_id, origin_thread_id=f"thread-b921-{substatus}", runner="claude_worker_b921")

    result = task_engine.mark_terminal_review(
        repo, task_id, "claude_worker_b921", substatus, evidence={},
    )
    assert result["ok"] is True

    card = task_store.get_task(repo, task_id)
    assert card["status"] == "review"
    # The card's own terminal_substatus is NEVER normalized/lossy -- it is
    # exactly the substatus the worker/reconciler recorded.
    assert card["terminal_substatus"] == substatus

    transition = _read_outbox_transition(repo, task_id)
    assert transition == expected_transition

    prompt = _claim_and_render_prompt(repo)
    assert prompt == f"Task MCP: {task_id} → {expected_transition}"
    if expected_transition != "review_ready":
        assert "review_ready" not in prompt


def test_mark_terminal_review_hardcoded_review_ready_regression_guard(tmp_path):
    """Direct regression guard on the exact historical defect: the enqueue
    call inside task_engine.mark_terminal_review must derive its transition
    from the substatus argument, never pass the literal "review_ready"
    string regardless of substatus. Fails if that hardcoding regresses."""
    repo = _init_repo(tmp_path)
    task_id = "TASK_B921_REGRESSION_GUARD"
    _seed_processing_task(repo, task_id, origin_thread_id="thread-b921-guard", runner="claude_worker_b921")

    task_engine.mark_terminal_review(
        repo, task_id, "claude_worker_b921", "validation_failed", evidence={},
    )

    assert _read_outbox_transition(repo, task_id) == "validation_failed"


@pytest.mark.parametrize(
    ("substatus", "expected_transition"),
    [
        ("timed_out", "timed_out"),
        ("token_budget_exceeded", "token_budget_exceeded"),
        ("output_budget_exceeded", "output_budget_exceeded"),
        ("worker_failed", "worker_failed"),
        ("finalize_failed", "blocked"),
        ("cancelled", "cancelled"),
        ("liveness_lost", "blocked"),
    ],
)
def test_blocked_terminal_failure_remains_callback_eligible(
    tmp_path, substatus, expected_transition,
):
    repo = _init_repo(tmp_path)
    task_id = f"TASK_B921_BLOCKED_{substatus.upper()}"
    runner = "claude_worker_b921"
    _seed_processing_task(
        repo,
        task_id,
        origin_thread_id=f"thread-b921-blocked-{substatus}",
        runner=runner,
    )
    request_id = f"request-{task_id.lower()}"

    result = task_engine.mark_terminal_failure(
        repo,
        task_id,
        runner,
        substatus,
        evidence={"error": f"observed:{substatus}"},
        request_id=request_id,
    )

    assert result["ok"] is True
    assert result["callback_enqueued"] is True
    card = task_store.get_task(repo, task_id)
    assert card["status"] == "blocked"
    assert card["terminal_substatus"] == substatus
    assert _read_outbox_transition(repo, task_id) == expected_transition
    assert _claim_and_render_prompt(repo) == (
        f"Task MCP: {task_id} → {expected_transition}"
    )


def test_normalize_callback_transition_public_wrapper_matches_private_map():
    assert callback_store.normalize_callback_transition("validation_failed") == "validation_failed"
    assert callback_store.normalize_callback_transition("review") == "review_ready"
    assert callback_store.normalize_callback_transition("process_lost") == "blocked"
    assert callback_store.normalize_callback_transition("token_budget_exceeded") == (
        "token_budget_exceeded"
    )
    assert callback_store.normalize_callback_transition("output_budget_exceeded") == (
        "output_budget_exceeded"
    )
    assert callback_store.normalize_callback_transition("blocked_on_dependency") == "blocked"
    assert callback_store.normalize_callback_transition("") is None
    assert callback_store.normalize_callback_transition(None) is None
