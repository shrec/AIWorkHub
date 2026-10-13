"""NF-2026-01514: reject_review(to="pending") must clear stale recovery_feedback."""

import json
import sqlite3
from pathlib import Path

from aiworkhub import core, task_store

_NOW = "2026-08-06T00:00:00+00:00"


def _setup_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    return repo


def _insert(repo: Path, task_id: str, *, card: dict) -> None:
    readiness = task_store.storage_readiness(repo)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id,runner,topic,mode,status,worker_status,priority,"
            "objective,card_json,created_at,updated_at,claimed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                "claude_coding",
                "coding",
                "solo",
                "review",
                "review",
                "normal",
                "obj",
                json.dumps(card),
                _NOW,
                _NOW,
                "claude_coding",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _row(repo: Path, task_id: str) -> dict:
    readiness = task_store.storage_readiness(repo)
    conn = sqlite3.connect(readiness.canonical_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    return dict(row)


def _sealed_review_card() -> dict:
    return {
        "claim_epoch": 1,
        "terminal_review": {"substatus": "review_ready"},
        "deterministic_verification": {"pass": True, "claim_epoch": 1},
        "recovery_feedback": "RECOVERY-OLD",
    }


def _authorize(repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda runner: (True, "ok"))


def test_reject_to_pending_clears_stale_recovery_feedback(tmp_path, monkeypatch):
    repo = _setup_repo(tmp_path)
    _authorize(repo, monkeypatch)
    _insert(repo, "T_NF01514_PENDING", card=_sealed_review_card())

    res = core.reject_review("T_NF01514_PENDING", "NEW-REVIEW", to="pending")
    assert res["ok"] is True, res

    card = json.loads(_row(repo, "T_NF01514_PENDING")["card_json"])
    assert "recovery_feedback" not in card
    assert card["review_feedback"]["instruction"] == "NEW-REVIEW"


def test_reject_to_blocked_keeps_recovery_feedback(tmp_path, monkeypatch):
    repo = _setup_repo(tmp_path)
    _authorize(repo, monkeypatch)
    _insert(repo, "T_NF01514_BLOCKED", card=_sealed_review_card())

    res = core.reject_review("T_NF01514_BLOCKED", "needs external input", to="blocked")
    assert res["ok"] is True, res

    card = json.loads(_row(repo, "T_NF01514_BLOCKED")["card_json"])
    assert card["recovery_feedback"] == "RECOVERY-OLD"
