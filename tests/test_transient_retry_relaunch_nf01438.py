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
task_reconciler = importlib.import_module("aiworkhub.task_reconciler")
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


def _force_due(root: Path, task_id: str) -> None:
    _update_card(root, task_id, lambda card: card.__setitem__("retry_not_before", "2000-01-01T00:00:00+00:00"))


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


def test_card_is_not_relaunched_before_retry_not_before(tmp_path):
    root = _init_repo(tmp_path)
    _insert_processing_card(root, "T1", runner="runner_t1")
    ok, state = task_store.mark_transient_retry(
        root, "T1", runner="runner_t1", reason="provider_429_rate_limited"
    )
    assert (ok, state) == (True, "pending")
    assert _card(root, "T1")["retry_not_before"]

    calls: list[str] = []
    outcome = autolaunch.reconcile_transient_due(root, launch=_fake_launch(root, calls))

    assert calls == []
    assert outcome["launched"] == []
    assert _card(root, "T1")["_status"] == "pending"


def test_card_is_relaunched_exactly_once_after_retry_not_before_passes(tmp_path):
    root = _init_repo(tmp_path)
    _insert_processing_card(root, "T2", runner="runner_t2", topic="task_mcp")
    task_store.mark_transient_retry(root, "T2", runner="runner_t2", reason="provider_429")
    _force_due(root, "T2")

    calls: list[str] = []
    outcome = autolaunch.reconcile_transient_due(root, launch=_fake_launch(root, calls))

    assert calls == ["T2"]
    assert [row["task_id"] for row in outcome["launched"]] == ["T2"]
    launched = outcome["launched"][0]
    assert (launched["runner"], launched["topic"]) == ("runner_t2", "task_mcp")
    card = _card(root, "T2")
    assert (card["_status"], card["_worker_status"]) == ("processing", "claimed")

    # A second pass must not relaunch the same attempt again: the card is no
    # longer pending/unclaimed, so it is no longer a candidate at all.
    second = autolaunch.reconcile_transient_due(root, launch=_fake_launch(root, calls))
    assert calls == ["T2"]
    assert second["launched"] == []


def test_card_beyond_its_retry_budget_is_not_selected(tmp_path):
    root = _init_repo(tmp_path)
    _insert_processing_card(root, "T3", runner="runner_t3")

    def _exhaust(card: dict) -> None:
        card["status"] = "pending"
        card["worker_status"] = "unclaimed"
        card["transient_retry"] = {
            "schema_id": "aiworkhub.transient_retry.v1",
            "attempts": 4,
            "budget": 3,
            "reason": "provider_429",
            "request_id": "",
            "recorded_at": "2026-10-01T00:00:00+00:00",
        }
        card["retry_not_before"] = "2000-01-01T00:00:00+00:00"

    _update_card(root, "T3", _exhaust)

    calls: list[str] = []
    outcome = autolaunch.reconcile_transient_due(root, launch=_fake_launch(root, calls))

    assert calls == []
    assert outcome["launched"] == []
    assert _card(root, "T3")["_status"] == "pending"


def test_card_with_unmet_depends_on_is_left_to_reconcile(tmp_path):
    root = _init_repo(tmp_path)
    _insert_processing_card(root, "T4", runner="runner_t4")
    task_store.mark_transient_retry(root, "T4", runner="runner_t4", reason="provider_429")
    _force_due(root, "T4")
    # DEP_NOT_FINISHED names no row at all: an unmet dependency by construction.
    _update_card(root, "T4", lambda card: card.__setitem__("depends_on", ["DEP_NOT_FINISHED"]))

    calls: list[str] = []
    outcome = autolaunch.reconcile_transient_due(root, launch=_fake_launch(root, calls))

    assert calls == []
    assert outcome["launched"] == []
    [skipped] = [row for row in outcome["skipped"] if row["task_id"] == "T4"]
    assert skipped["reason"] == "unmet_depends_on_owned_by_reconcile"


def test_production_scan_pass_wires_the_relaunch_through_claim_start_exact():
    reconciler_source = (SRC / "aiworkhub" / "task_reconciler.py").read_text(encoding="utf-8")
    assert "dependency_autolaunch.reconcile_transient_due" in reconciler_source
    assert "core.claim_start_exact" in reconciler_source
    assert "_scan_transient_retry_relaunch" in reconciler_source
    assert "result[\"transient_retry_relaunch\"]" not in reconciler_source  # sanity: no stray literal
    assert "\"transient_retry_relaunch\": transient_retry_relaunch," in reconciler_source


def test_run_scan_includes_the_transient_retry_relaunch_receipt(monkeypatch, tmp_path):
    class _Mgr:
        def __init__(self, repo: Path) -> None:
            self.repo = repo

        def reconcile(self, *, include_gc: bool = True) -> dict:
            return {"finalized": 0, "watched": 0, "gc_included": include_gc}

    monkeypatch.setattr(
        task_reconciler.review_orchestrator, "canonical_review_db", lambda _mgr: None
    )
    monkeypatch.setattr(
        task_reconciler.dependency_autolaunch,
        "reconcile_transient_due",
        lambda repo, *, launch, capacity=None: {"ok": True, "launched": [], "capacity": capacity},
    )

    result = task_reconciler.run_scan(_Mgr(tmp_path), include_gc=False)

    assert result["transient_retry_relaunch"]["ok"] is True
    assert result["transient_retry_relaunch"]["capacity"] == task_reconciler.TRANSIENT_RETRY_RELAUNCH_CAPACITY


def test_run_scan_survives_a_transient_retry_relaunch_exception(monkeypatch, tmp_path):
    class _Mgr:
        def __init__(self, repo: Path) -> None:
            self.repo = repo

        def reconcile(self, *, include_gc: bool = True) -> dict:
            return {"finalized": 0, "watched": 0, "gc_included": include_gc}

    monkeypatch.setattr(
        task_reconciler.review_orchestrator, "canonical_review_db", lambda _mgr: None
    )

    def _boom(repo, *, launch, capacity=None):
        raise RuntimeError("provider_unreachable")

    monkeypatch.setattr(task_reconciler.dependency_autolaunch, "reconcile_transient_due", _boom)

    result = task_reconciler.run_scan(_Mgr(tmp_path), include_gc=False)

    assert result["ok"] is True
    assert result["transient_retry_relaunch"] == {
        "ok": False, "state": "skipped", "reason": "RuntimeError",
    }
