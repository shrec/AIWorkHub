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


def _force_due(root: Path, task_id: str) -> None:
    _update_card(root, task_id, lambda card: card.__setitem__("retry_not_before", "2000-01-01T00:00:00+00:00"))


def _read_updated_at(root: Path, task_id: str) -> str:
    conn = sqlite3.connect(_db(root))
    try:
        row = conn.execute("SELECT updated_at FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    return str(row[0] or "")


def _set_updated_at(root: Path, task_id: str, value: str) -> None:
    conn = sqlite3.connect(_db(root))
    try:
        conn.execute("UPDATE tasks SET updated_at=? WHERE task_id=?", (value, task_id))
        conn.commit()
    finally:
        conn.close()


def _read_hold_card_updated_at(root: Path, task_id: str) -> str | None:
    conn = sqlite3.connect(_db(root))
    try:
        row = conn.execute(
            "SELECT card_updated_at FROM dependency_autolaunch_holds WHERE task_id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else str(row[0])


def _claim_launch(root: Path, calls: list[str]):
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


def _denying_launch_with_blocker_bump(root: Path, calls: list[str], *, bump_to: str):
    def launch(task_id: str, runner: str, topic: str, request_id: str) -> dict:
        calls.append(task_id)
        conn = sqlite3.connect(_db(root))
        try:
            conn.execute("UPDATE tasks SET updated_at=? WHERE task_id=?", (bump_to, task_id))
            conn.commit()
        finally:
            conn.close()
        return {"ok": False, "stderr": "quality_review_binding_required"}

    return launch


def test_quality_review_card_with_due_retry_is_skipped_without_launch(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    _insert_processing_card(root, "QR1", runner="runner_qr", topic="quality_review")
    ok, state = task_store.mark_transient_retry(root, "QR1", runner="runner_qr", reason="provider_429")
    assert (ok, state) == (True, "pending")
    _force_due(root, "QR1")

    calls: list[str] = []
    outcome = autolaunch.reconcile_transient_due(root, launch=_claim_launch(root, calls))

    assert calls == []
    assert outcome["launched"] == []
    skipped = [row for row in outcome["skipped"] if row["task_id"] == "QR1"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "quality_review_owned_by_review_orchestrator"


def test_deterministic_denial_hold_survives_launcher_blocker_bump(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    _insert_processing_card(root, "T9", runner="runner_t9", topic="task_mcp")
    ok, state = task_store.mark_transient_retry(root, "T9", runner="runner_t9", reason="provider_429")
    assert (ok, state) == (True, "pending")
    _force_due(root, "T9")

    bumped = "2999-01-01T00:00:00+00:00"
    assert _read_updated_at(root, "T9") < bumped

    calls: list[str] = []
    first = autolaunch.reconcile_transient_due(
        root, launch=_denying_launch_with_blocker_bump(root, calls, bump_to=bumped)
    )
    assert calls == ["T9"]
    assert first["launched"] == []
    assert _read_hold_card_updated_at(root, "T9") == bumped

    second = autolaunch.reconcile_transient_due(
        root, launch=_denying_launch_with_blocker_bump(root, calls, bump_to=bumped)
    )
    assert calls == ["T9"]
    assert second["launched"] == []
    held = [row for row in second["skipped"] if row["task_id"] == "T9"]
    assert len(held) == 1
    assert held[0]["reason"].startswith("deterministic_denial_hold:")


def test_real_card_edit_clears_hold_and_relaunches(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    _insert_processing_card(root, "T10", runner="runner_t10", topic="task_mcp")
    ok, state = task_store.mark_transient_retry(root, "T10", runner="runner_t10", reason="provider_429")
    assert (ok, state) == (True, "pending")
    _force_due(root, "T10")

    bumped = "2999-01-01T00:00:00+00:00"
    calls: list[str] = []
    first = autolaunch.reconcile_transient_due(
        root, launch=_denying_launch_with_blocker_bump(root, calls, bump_to=bumped)
    )
    assert calls == ["T10"]
    assert first["launched"] == []
    assert _read_hold_card_updated_at(root, "T10") == bumped

    _set_updated_at(root, "T10", "3000-01-01T00:00:00+00:00")

    relaunched: list[str] = []
    third = autolaunch.reconcile_transient_due(root, launch=_claim_launch(root, relaunched))
    assert relaunched == ["T10"]
    assert [row["task_id"] for row in third["launched"]] == ["T10"]
