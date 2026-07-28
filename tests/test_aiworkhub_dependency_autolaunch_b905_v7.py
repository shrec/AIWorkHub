from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

autolaunch = importlib.import_module("aiworkhub.dependency_autolaunch")


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    db_dir = repo / ".aiworkhub" / "tasking"
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(db_dir / "task_queue.sqlite")
    conn.execute(
        "CREATE TABLE tasks("
        "task_id TEXT PRIMARY KEY, runner TEXT, topic TEXT, mode TEXT DEFAULT '', "
        "status TEXT, worker_status TEXT, priority TEXT DEFAULT 'normal', objective TEXT DEFAULT '', "
        "card_json TEXT, created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '', "
        "completed_at TEXT DEFAULT '', claimed_by TEXT DEFAULT '', claimed_at TEXT DEFAULT '', "
        "started_at TEXT DEFAULT '', origin_thread_id TEXT DEFAULT '', archived_at TEXT DEFAULT '')"
    )
    conn.execute(
        "CREATE TABLE task_events("
        "task_id TEXT, event TEXT, runner TEXT, payload_json TEXT, created_at TEXT)"
    )
    conn.commit()
    conn.close()
    return repo


def _add(repo: Path, task_id: str, *, deps=None, status="pending", worker="unclaimed", runner=None, topic="task_mcp"):
    card = {
        "task_id": task_id,
        "runner": runner or f"runner_{task_id.lower()}",
        "topic": topic,
        "status": status,
        "worker_status": worker,
        "depends_on": list(deps or []),
        "origin_thread_id": f"thread_{task_id.lower()}",
        "coordinator_provider": "codex",
    }
    conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    conn.execute(
        "INSERT INTO tasks(task_id, runner, topic, status, worker_status, card_json, origin_thread_id) VALUES(?,?,?,?,?,?,?)",
        (task_id, card["runner"], topic, status, worker, json.dumps(card), card["origin_thread_id"]),
    )
    conn.commit()
    conn.close()


def _canonical_claim_start(repo: Path, calls: list[str], lock: threading.Lock | None = None):
    def launch(task_id: str, runner: str, topic: str, request_id: str):
        if lock is None:
            return _claim(repo, calls, task_id, runner, topic, request_id)
        with lock:
            return _claim(repo, calls, task_id, runner, topic, request_id)

    return launch


def _claim(repo: Path, calls: list[str], task_id: str, runner: str, topic: str, request_id: str):
    conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    try:
        row = conn.execute("SELECT runner, topic FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None or row[0] != runner or row[1] != topic:
            return {"ok": False, "stderr": "identity_mismatch"}
        cur = conn.execute(
            "UPDATE tasks SET status='processing', worker_status='claimed', claimed_by=? "
            "WHERE task_id=? AND status='pending' AND worker_status='unclaimed'",
            (runner, task_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return {"ok": False, "stderr": "claim_conflict"}
        conn.execute(
            "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) VALUES(?,?,?,?, '')",
            (task_id, "claim_start", runner, json.dumps({"request_id": request_id})),
        )
        conn.commit()
        calls.append(task_id)
        return {"ok": True}
    finally:
        conn.close()


def _state(repo: Path, task_id: str):
    conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    row = conn.execute("SELECT status, worker_status, card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    conn.close()
    return row[0], row[1], json.loads(row[2])


def test_mark_done_hook_is_production_connected_to_reconciler_and_claim_start_path():
    core_source = (SRC / "aiworkhub" / "core.py").read_text(encoding="utf-8")
    assert "dependency_autolaunch.reconcile_after_accept" in core_source
    assert "claim_start_exact" in core_source
    assert "result[\"dependency_autolaunch\"]" in core_source


def test_no_dependents_and_finished_dependency_not_returned_ready(tmp_path):
    repo = _init_repo(tmp_path)
    _add(repo, "P", status="finished", worker="done")
    calls: list[str] = []
    outcome = autolaunch.reconcile_after_accept(repo, "P", _canonical_claim_start(repo, calls))
    assert outcome["schema_id"] == "aiworkhub.dependency_autolaunch_outcome.v1"
    assert outcome["launched"] == []
    assert calls == []
    assert _state(repo, "P")[:2] == ("finished", "done")


def test_chain_fan_in_fan_out_and_diamond_graphs(tmp_path):
    repo = _init_repo(tmp_path)
    for tid in ("A", "B"):
        _add(repo, tid, status="finished", worker="done")
    _add(repo, "C", deps=["A"])
    _add(repo, "D", deps=["A", "B"])
    _add(repo, "E", deps=["A"])
    _add(repo, "F", deps=["C", "D"])
    calls: list[str] = []
    first = autolaunch.reconcile_after_accept(repo, "A", _canonical_claim_start(repo, calls))
    assert [r["task_id"] for r in first["launched"]] == ["C", "D", "E"]
    assert "F" not in calls
    for tid in ("C", "D"):
        conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
        conn.execute("UPDATE tasks SET status='finished', worker_status='done' WHERE task_id=?", (tid,))
        conn.commit()
        conn.close()
    second = autolaunch.reconcile_after_accept(repo, "D", _canonical_claim_start(repo, calls))
    assert [r["task_id"] for r in second["launched"]] == ["F"]


def test_duplicate_restart_and_concurrent_exact_once(tmp_path):
    repo = _init_repo(tmp_path)
    _add(repo, "A", status="finished", worker="done")
    _add(repo, "B", deps=["A"])
    calls: list[str] = []
    lock = threading.Lock()
    launch = _canonical_claim_start(repo, calls, lock)
    threads = [threading.Thread(target=autolaunch.reconcile_after_accept, args=(repo, "A", launch)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    again = autolaunch.reconcile_startup(repo, launch)
    assert calls == ["B"]
    assert again["launched"] == []
    assert _state(repo, "B")[:2] == ("processing", "claimed")


def test_capacity_delay_retry_and_manual_launch_preservation(tmp_path):
    repo = _init_repo(tmp_path)
    _add(repo, "A", status="finished", worker="done")
    _add(repo, "B", deps=["A"])
    _add(repo, "C", deps=["A"])
    _add(repo, "MANUAL", deps=["A"], status="processing", worker="claimed")
    calls: list[str] = []
    first = autolaunch.reconcile_startup(repo, _canonical_claim_start(repo, calls), capacity=1)
    assert [r["task_id"] for r in first["launched"]] == ["B"]
    assert any(r["task_id"] == "C" and r["reason"] == "capacity" for r in first["delayed"])
    second = autolaunch.reconcile_startup(repo, _canonical_claim_start(repo, calls))
    assert [r["task_id"] for r in second["launched"]] == ["C"]
    assert _state(repo, "MANUAL")[:2] == ("processing", "claimed")


def test_failed_and_cancelled_dependency_routes_child_to_review_visible_blocked(tmp_path):
    repo = _init_repo(tmp_path)
    _add(repo, "FAILED", status="failed", worker="worker_failed")
    _add(repo, "CANCELLED", status="cancelled", worker="cancelled")
    _add(repo, "CHILD", deps=["FAILED", "CANCELLED"])
    outcome = autolaunch.reconcile_after_accept(repo, "FAILED", _canonical_claim_start(repo, []))
    assert outcome["blocked"] == [{"task_id": "CHILD", "blocked_by": ["CANCELLED", "FAILED"]}]
    status, worker, card = _state(repo, "CHILD")
    assert (status, worker, card["substatus"]) == ("review", "review", "dependency_blocked")
    conn = sqlite3.connect(repo / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    row = conn.execute(
        "SELECT transition,state FROM callback_outbox WHERE task_id='CHILD'"
    ).fetchone()
    conn.close()
    assert row == ("blocked", "pending")


def test_adapter_families_and_two_repository_isolation(tmp_path):
    repo1 = _init_repo(tmp_path / "one")
    repo2 = _init_repo(tmp_path / "two")
    adapters = [
        ("CLAUDE_CHILD", "claude_task_mcp_family_b905"),
        ("DEEPSEEK_CHILD", "deepseek_v4pro_task_mcp_family_b905"),
        ("CODEX_CHILD", "codex_gpt55_task_mcp_family_b905"),
    ]
    for repo in (repo1, repo2):
        _add(repo, "P", status="finished", worker="done")
    for tid, runner in adapters:
        _add(repo1, tid, deps=["P"], runner=runner)
    _add(repo2, "CLAUDE_CHILD", deps=["P"], runner="claude_task_mcp_family_b905")
    calls1: list[str] = []
    calls2: list[str] = []
    out1 = autolaunch.reconcile_after_accept(repo1, "P", _canonical_claim_start(repo1, calls1))
    assert sorted(row["task_id"] for row in out1["launched"]) == sorted(tid for tid, _runner in adapters)
    assert calls2 == []
    assert _state(repo2, "CLAUDE_CHILD")[:2] == ("pending", "unclaimed")
