from __future__ import annotations

import json
import sqlite3

from aiworkhub import core, stale_recovery, task_store


def _store(tmp_path, monkeypatch, *, alive=False, request_id="req-1", epoch=3):
    db = tmp_path / "tasks.db"
    conn = sqlite3.connect(db)
    conn.executescript(task_store.SCHEMA)
    card = {
        "task_id": "T1", "topic": "dead-worker", "status": "processing",
        "worker_status": "claimed", "claimed_by": "codex",
        "launch_request_id": request_id, "claim_epoch": epoch,
    }
    conn.execute(
        "INSERT INTO tasks(task_id,status,worker_status,card_json,created_at,updated_at,"
        "claimed_by,claimed_at,started_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("T1", "processing", "claimed", json.dumps(card), "now", "now", "codex", "now", "now"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(task_store, "_require_ready", lambda root: ({}, db))
    evidence = {
        "ok": True, "request_id": request_id, "task_id": "T1",
        "state": "blocked", "process_alive": alive, "exit_code": 1,
        "error": "worker died conclusively",
        "latest_event": {
            "request_id": request_id, "task_id": "T1",
            "state": "blocked", "error": "worker died",
        },
    }
    return db, evidence


def test_reconcile_split_brain_then_archive_is_idempotent(tmp_path, monkeypatch):
    db, evidence = _store(tmp_path, monkeypatch)
    ok, state = stale_recovery.reconcile_dead_processing_claim(
        root=str(tmp_path), task_id="T1", request_id="req-1", claim_epoch=3,
        process_status=evidence, actor="manager",
    )
    assert (ok, state) == (True, "reconciled")
    reconciled = task_store.get_task(tmp_path, "T1")["dead_process_reconciliation"]
    assert reconciled["reason"] == "worker died conclusively"
    assert reconciled["evidence"]["error"] == "worker died conclusively"
    assert task_store.archive_task(tmp_path, "T1", actor="manager") == (True, "archived")
    ok, state = stale_recovery.reconcile_dead_processing_claim(
        root=str(tmp_path), task_id="T1", request_id="req-1", claim_epoch=3,
        process_status=evidence, actor="manager",
    )
    assert (ok, state) == (True, "already_reconciled")
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM task_events WHERE event='dead_process_reconciled'").fetchone()[0] == 1


def test_reconcile_fails_closed_and_live_archive_stays_forbidden(tmp_path, monkeypatch):
    _db, evidence = _store(tmp_path, monkeypatch, alive=True)
    before = task_store.get_task(tmp_path, "T1")
    ok, state = stale_recovery.reconcile_dead_processing_claim(
        root=str(tmp_path), task_id="T1", request_id="req-1", claim_epoch=3,
        process_status=evidence, actor="manager",
    )
    assert (ok, state) == (False, "process_not_proven_dead")
    assert task_store.get_task(tmp_path, "T1") == before
    assert task_store.archive_task(tmp_path, "T1", actor="manager") == (
        False, "archive_processing_forbidden"
    )


def test_core_reconcile_binds_task_id_to_card_scoped_write_gate(monkeypatch):
    observed = {}
    blocked = {"ok": False, "returncode": 126, "stderr": "sentinel"}
    monkeypatch.setattr(core, "_live_card", lambda task_id: ({"topic": "quality_review"}, None))

    def fake_gate(action, **kwargs):
        observed.update(action=action, **kwargs)
        return blocked

    monkeypatch.setattr(core, "_canonical_write_gate", fake_gate)
    assert core.reconcile_dead_processing_task("T1", "req-1", 1) is blocked
    assert observed["action"] == "recover-stale"
    assert observed["task_id"] == "T1"


def test_reconcile_rejects_identity_mismatches_without_mutation(tmp_path, monkeypatch):
    _db, evidence = _store(tmp_path, monkeypatch)
    before = task_store.get_task(tmp_path, "T1")
    for request_id, epoch, status in [
        ("wrong", 3, evidence),
        ("req-1", 4, evidence),
        ("req-1", 3, {"ok": False}),
    ]:
        ok, _state = stale_recovery.reconcile_dead_processing_claim(
            root=str(tmp_path), task_id="T1", request_id=request_id,
            claim_epoch=epoch, process_status=status, actor="manager",
        )
        assert not ok
        assert task_store.get_task(tmp_path, "T1") == before


def test_reconcile_requires_exact_terminal_latest_event(tmp_path, monkeypatch):
    _db, evidence = _store(tmp_path, monkeypatch)
    before = task_store.get_task(tmp_path, "T1")
    invalid_latest_events = [
        None,
        {"request_id": "wrong", "task_id": "T1", "state": "blocked"},
        {"request_id": "req-1", "task_id": "wrong", "state": "blocked"},
        {"request_id": "req-1", "task_id": "T1", "state": "running"},
    ]
    for latest_event in invalid_latest_events:
        status = {**evidence, "latest_event": latest_event}
        ok, _state = stale_recovery.reconcile_dead_processing_claim(
            root=str(tmp_path), task_id="T1", request_id="req-1", claim_epoch=3,
            process_status=status, actor="manager",
        )
        assert not ok
        assert task_store.get_task(tmp_path, "T1") == before


def test_reconcile_card_json_preimage_swap_fails_without_mutation(tmp_path, monkeypatch):
    db, evidence = _store(tmp_path, monkeypatch)
    real_connect = task_store._connect
    conn = real_connect(db)

    class ConcurrentSwapConnection:
        def __init__(self):
            self.swapped = False

        def execute(self, sql, parameters=()):
            if sql.startswith("UPDATE tasks SET status='blocked'") and not self.swapped:
                self.swapped = True
                other = sqlite3.connect(db)
                raw = other.execute(
                    "SELECT card_json FROM tasks WHERE task_id='T1'"
                ).fetchone()[0]
                card = json.loads(raw)
                card["concurrent_manager_note"] = "preserve me"
                other.execute(
                    "UPDATE tasks SET card_json=? WHERE task_id='T1'",
                    (json.dumps(card),),
                )
                other.commit()
                other.close()
            return conn.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(conn, name)

    def connect(_db, *, readonly=False):
        if readonly:
            return real_connect(_db, readonly=True)
        return ConcurrentSwapConnection()

    monkeypatch.setattr(task_store, "_connect", connect)
    ok, state = stale_recovery.reconcile_dead_processing_claim(
        root=str(tmp_path), task_id="T1", request_id="req-1", claim_epoch=3,
        process_status=evidence, actor="manager",
    )
    assert (ok, state) == (False, "reconcile_write_conflict")
    row = task_store.get_task(tmp_path, "T1")
    assert row["status"] == "processing"
    assert row["concurrent_manager_note"] == "preserve me"
    check = sqlite3.connect(db)
    assert check.execute(
        "SELECT count(*) FROM task_events WHERE event='dead_process_reconciled'"
    ).fetchone()[0] == 0
