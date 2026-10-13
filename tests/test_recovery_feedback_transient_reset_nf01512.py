from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

autolaunch = importlib.import_module("aiworkhub.dependency_autolaunch")
task_store = importlib.import_module("aiworkhub.task_store")


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    return repo


def _db(root: Path) -> Path:
    return task_store.storage_readiness(root).canonical_db


def _insert_processing_card(root: Path, task_id: str, *, runner: str, topic: str = "task_mcp") -> None:
    """A card already claimed and running, exactly as a real launch leaves it."""
    now = "2026-10-01T00:00:00+00:00"
    card = {
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "status": "processing",
        "worker_status": "claimed",
        "origin_thread_id": f"thread_{task_id.lower()}",
        "coordinator_provider": "codex",
    }
    conn = sqlite3.connect(_db(root))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, status, worker_status, "
            "claimed_by, card_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, runner, topic, "processing", "claimed", runner, json.dumps(card), now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _card(root: Path, task_id: str) -> dict:
    conn = sqlite3.connect(_db(root))
    try:
        row = conn.execute(
            "SELECT status, worker_status, card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    card = json.loads(row[2])
    card["_status"], card["_worker_status"] = row[0], row[1]
    return card


def _update_card(root: Path, task_id: str, mutate) -> None:
    conn = sqlite3.connect(_db(root))
    try:
        row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        card = json.loads(row[0])
        mutate(card)
        conn.execute(
            "UPDATE tasks SET status=?, worker_status=?, card_json=? WHERE task_id=?",
            (card.get("status", "pending"), card.get("worker_status", "unclaimed"), json.dumps(card), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def _fake_launch(root: Path, calls: list[str]):
    """Mirrors ``core.claim_start_exact``'s own compare-and-update contract,
    without needing a real runner/topic to clear the production write-gate
    allowlist -- the same substitution the existing autolaunch tests make for
    the identical production callable."""

    def launch(task_id: str, runner: str, topic: str, request_id: str) -> dict:
        calls.append(task_id)
        conn = sqlite3.connect(_db(root))
        try:
            row = conn.execute(
                "SELECT runner, topic FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None or row[0] != runner or row[1] != topic:
                return {"ok": False, "stderr": f"identity_mismatch:task_id={task_id}"}
            cur = conn.execute(
                "UPDATE tasks SET status='processing', worker_status='claimed', claimed_by=? "
                "WHERE task_id=? AND status='pending' AND worker_status='unclaimed'",
                (runner, task_id),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return {"ok": False, "stderr": f"claim_conflict:task_id={task_id}"}
            conn.execute(
                "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
                "VALUES (?, 'claim_start', ?, ?, '')",
                (task_id, runner, json.dumps({"request_id": request_id})),
            )
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    return launch


def _get_card(repo: Path, task_id: str) -> dict:
    task = task_store.get_task(repo, task_id)
    assert task is not None, f"task {task_id} not found"
    return task


def test_task_contract_and_identity_keys_membership() -> None:
    assert "recovery_feedback" in task_store.TASK_CONTRACT_KEYS
    assert "review_feedback" not in task_store.CARD_CONTENT_IDENTITY_KEYS
    assert "recovery_feedback" not in task_store.CARD_CONTENT_IDENTITY_KEYS


def test_card_content_identity_stable_with_or_without_recovery_feedback() -> None:
    base = {"objective": "Do the thing", "allowed_writes": ["src/aiworkhub/task_store.py"]}
    with_feedback = dict(base, recovery_feedback="Manager instruction for the recovered worker")
    assert task_store.card_content_identity(base) == task_store.card_content_identity(with_feedback)


def test_begin_claim_episode_clears_transient_retry_but_keeps_attempts() -> None:
    card = {
        "transient_retry": {
            "schema_id": "aiworkhub.transient_retry.v1",
            "attempts": 1,
            "budget": 5,
            "reason": "provider_429",
            "request_id": "",
            "recorded_at": "2026-10-01T00:00:00+00:00",
        },
        "retry_not_before": "2026-10-01T00:05:00+00:00",
        "transient_retry_attempts": 1,
        "terminal_substatus": "validation_failed",
    }
    task_store.begin_claim_episode(card)
    assert "transient_retry" not in card
    assert "retry_not_before" not in card
    assert card["transient_retry_attempts"] == 1


def test_build_worker_prompt_renders_recovery_feedback() -> None:
    process_launcher = importlib.import_module("aiworkhub.process_launcher")
    prompt = process_launcher.build_worker_prompt(
        task_id="NF01512-PROMPT",
        runner="claude_opus-5",
        topic="task_mcp",
        card={"objective": "x", "recovery_feedback": "RECOVERY-MARKER-X"},
    )
    start_marker = "TASK_CONTRACT_JSON:\n"
    end_marker = "\nEND_TASK_CONTRACT_JSON"
    start = prompt.index(start_marker) + len(start_marker)
    end = prompt.index(end_marker)
    contract = json.loads(prompt[start:end])
    assert contract["recovery_feedback"] == "RECOVERY-MARKER-X"


def test_recovery_sheds_stale_transient_retry_so_reconcile_leaves_it_alone(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    task_id = "NF01512-E2E"
    runner = "codex_worker_test"
    blocked_at = "2026-08-06T00:00:00+00:00"

    _insert_processing_card(root, task_id, runner=runner)
    ok, state = task_store.mark_transient_retry(
        root, task_id, runner=runner, reason="provider_429_rate_limited"
    )
    assert (ok, state) == (True, "pending")
    assert _card(root, task_id)["transient_retry"]

    evidence = {
        "validation": [
            {"command": "pytest -q", "returncode": 1, "stdout": "1 failed", "stderr": ""}
        ],
        "required_outputs": [],
        "changed_path_hashes": {"src/aiworkhub/task_store.py": "abc123hash"},
    }

    def _become_blocked(card: dict) -> None:
        card["status"] = "blocked"
        card["worker_status"] = "validation_failed"
        card["mode"] = ""
        card["allowed_writes"] = ["src/aiworkhub/task_store.py"]
        card["objective"] = "Recover from a blocked validation failure"
        card["terminal_substatus"] = "validation_failed"
        card["terminal_review"] = {
            "substatus": "validation_failed",
            "evidence": evidence,
            "deterministic_verification": {
                "applicable": True,
                "pass": False,
                "substatus": "validation_failed",
                "reason": "evidence_verdict_failed",
                "claim_epoch": 0,
                "evidence_verdict": {
                    "passed": False,
                    "validation_count": 1,
                    "failed_validation_count": 1,
                },
            },
            "recorded_at": blocked_at,
            "runner": runner,
            "claim_epoch": 0,
        }
        card["deterministic_verification"] = card["terminal_review"]["deterministic_verification"]
        card["blocker_reason"] = "validation_failed: tests failed"
        card["blocked_at"] = blocked_at
        card["blocked_by"] = runner
        card["claim_epoch"] = 0
        # NF-2026-01513: the stale transient-retry bookkeeping from the earlier
        # provider failure survives, untouched, into this blocked episode --
        # exactly the state a real blocked-then-recovered card is in.  Pinning
        # ``retry_not_before`` to the same instant as ``blocked_at`` makes it
        # already due by the time recovery runs, so unfixed code relaunches
        # immediately instead of merely delaying.
        card["retry_not_before"] = blocked_at

    _update_card(root, task_id, _become_blocked)

    conn = sqlite3.connect(_db(root))
    conn.execute("UPDATE tasks SET claimed_by='' WHERE task_id=?", (task_id,))
    conn.execute(
        "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
        "VALUES (?, 'terminal_review', ?, ?, ?)",
        (
            task_id,
            runner,
            json.dumps({"substatus": "validation_failed", "evidence": evidence}),
            blocked_at,
        ),
    )
    conn.commit()
    conn.close()

    feedback = "Re-run against the canonical root; the flake is gone upstream."
    ok, state = task_store.recover_blocked_rework(
        root, task_id, actor="coordinator", feedback_reason=feedback,
    )
    assert (ok, state) == (True, "recovered")

    card = _get_card(root, task_id)
    assert card["status"] == "pending"
    assert "transient_retry" not in card
    assert "retry_not_before" not in card
    assert card["recovery_feedback"] == feedback

    calls: list[str] = []
    outcome = autolaunch.reconcile_transient_due(root, launch=_fake_launch(root, calls))
    assert calls == []
    assert outcome["launched"] == []
    assert outcome["delayed"] == []
