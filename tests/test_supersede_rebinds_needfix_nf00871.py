"""NF-2026-00871: supersede --by rebinds the linked NeedFix onto the successor.

Every case runs against a real temporary task store and NeedFix store: the
coordinator's ``core.supersede_task`` is the only declared-successor surface,
and ``needfix_store.close_for_accepted_task`` must resolve the NeedFix once the
successor is accepted instead of returning ``not_linked``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, needfix_store, task_store  # noqa: E402


def _insert_card(repo: Path, task_id: str, *, status: str = "pending",
                 worker_status: str = "unclaimed", **extra) -> None:
    _readiness, db_path = task_store._require_ready(repo)
    card = {
        "task_id": task_id,
        "runner": "codex_a",
        "topic": "coding",
        "allowed_writes": [],
        "depends_on": [],
        "priority": "",
        "status": status,
        "worker_status": worker_status,
        **extra,
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, archived_at) "
            "VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, '')",
            (
                task_id, "codex_a", "coding", status, worker_status, json.dumps(card),
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    task_store.initialize_repository(root)
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *args, **kwargs: None)
    return core.repo_root()


@pytest.fixture
def store(repo: Path) -> Path:
    needfix_store.initialize_repository(repo)
    assert needfix_store._db_path(repo).is_file()
    return repo


def _linked_needfix(repo: Path, title: str, task_id: str) -> str:
    rec = needfix_store.capture_proposal(repo, title=title, description=title)
    conn = sqlite3.connect(str(needfix_store._db_path(repo)))
    try:
        conn.execute(
            "UPDATE needfix SET converted_task_id = ?, status = 'task_created' WHERE id = ?",
            (task_id, rec["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return rec["id"]


def _row(repo: Path, needfix_id: str) -> dict:
    conn = sqlite3.connect(str(needfix_store._db_path(repo)))
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM needfix WHERE id = ?", (needfix_id,)).fetchone())
    finally:
        conn.close()


def _events(repo: Path, needfix_id: str, event: str) -> list[dict]:
    conn = sqlite3.connect(str(needfix_store._db_path(repo)))
    try:
        rows = conn.execute(
            "SELECT detail_json FROM needfix_events WHERE needfix_id = ? AND event = ? "
            "ORDER BY seq",
            (needfix_id, event),
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(detail) for (detail,) in rows]


def test_supersede_rebinds_needfix_and_successor_acceptance_resolves_it(store):
    _insert_card(store, "old")
    _insert_card(store, "new")
    nf = _linked_needfix(store, "superseded card defect", "old")

    result = core.supersede_task("old", reason="replaced", by="new")

    assert result["ok"] is True, result
    rebind = result["needfix_rebind"]
    assert rebind["ok"] is True
    assert rebind["state"] == "rebound"
    assert rebind["needfix_id"] == nf
    assert "closure" not in rebind
    row = _row(store, nf)
    assert row["converted_task_id"] == "new"
    assert row["status"] == "task_created"
    events = _events(store, nf, needfix_store.SUPERSEDED_REBIND_EVENT)
    assert needfix_store.SUPERSEDED_REBIND_EVENT == "superseded_task_rebound"
    assert len(events) == 1
    assert events[0]["from_task_id"] == "old"
    assert events[0]["to_task_id"] == "new"
    assert events[0]["actor"] == core._verified_manager_actor()
    assert "replaced" in events[0]["reason"]

    closure = needfix_store.close_for_accepted_task(
        store, "new", accepted_request_id="req-new"
    )

    assert closure["state"] == "closed"
    assert closure["needfix_id"] == nf
    assert _row(store, nf)["status"] == "resolved"


def test_supersede_without_by_changes_no_needfix(store):
    _insert_card(store, "old")
    nf = _linked_needfix(store, "no successor declared", "old")
    before = _row(store, nf)

    result = core.supersede_task("old", reason="stale")

    assert result["ok"] is True, result
    assert result["returncode"] == 0
    assert json.loads(result["stdout"])["archive_operation"] == "superseded"
    assert result["needfix_rebind"] == {
        "ok": True, "state": "skipped", "reason": "no_successor_declared",
    }
    assert _row(store, nf) == before
    assert _events(store, nf, needfix_store.SUPERSEDED_REBIND_EVENT) == []


def test_supersede_with_no_linked_needfix_reports_not_linked(store):
    _insert_card(store, "old")
    _insert_card(store, "new")
    other = _linked_needfix(store, "unrelated defect", "unrelated-task")
    before = _row(store, other)

    result = core.supersede_task("old", reason="replaced", by="new")

    assert result["ok"] is True, result
    assert result["needfix_rebind"]["ok"] is True
    assert result["needfix_rebind"]["state"] == "not_linked"
    assert result["needfix_rebind"]["needfix_id"] is None
    assert _row(store, other) == before


def test_rebind_without_needfix_store_never_creates_one(tmp_path):
    root = tmp_path / "no-store"
    root.mkdir()

    result = needfix_store.rebind_superseded_task_link(
        root, "old", "new", actor="codex", reason="replaced"
    )

    assert result == {
        "ok": True, "state": "not_linked", "from_task_id": "old",
        "to_task_id": "new", "needfix_id": None,
    }
    assert not needfix_store._db_path(root).exists()


def test_successor_linked_to_other_needfix_refuses_but_supersede_succeeds(store):
    _insert_card(store, "old")
    _insert_card(store, "new")
    nf_old = _linked_needfix(store, "old lineage", "old")
    nf_new = _linked_needfix(store, "new lineage", "new")
    before_old, before_new = _row(store, nf_old), _row(store, nf_new)

    result = core.supersede_task("old", reason="replaced", by="new")

    assert result["ok"] is True, result
    assert task_store.get_task(store, "old")["superseded_by"] == "new"
    rebind = result["needfix_rebind"]
    assert rebind["ok"] is False
    assert rebind["state"] == "refused"
    assert rebind["reason"] == f"successor_already_linked_to_needfix:{nf_new}"
    assert _row(store, nf_old) == before_old
    assert _row(store, nf_new) == before_new
    assert _events(store, nf_old, needfix_store.SUPERSEDED_REBIND_EVENT) == []
    assert _events(store, nf_new, needfix_store.SUPERSEDED_REBIND_EVENT) == []


def test_rebind_rerun_for_same_pair_is_idempotent(store):
    _insert_card(store, "old")
    _insert_card(store, "new")
    nf = _linked_needfix(store, "idempotent rebind", "old")
    assert core.supersede_task("old", reason="replaced", by="new")["ok"] is True
    after_first = _row(store, nf)

    again = needfix_store.rebind_superseded_task_link(
        store, "old", "new", actor="codex", reason="retry",
        get_task_fn=lambda task_id: task_store.get_task(store, task_id),
        canonical_status_fn=task_store.canonical_status,
    )

    assert again["ok"] is True
    assert again["state"] == "idempotent"
    assert again["needfix_id"] == nf
    assert _row(store, nf) == after_first
    assert len(_events(store, nf, needfix_store.SUPERSEDED_REBIND_EVENT)) == 1


def test_already_accepted_successor_resolves_needfix_in_same_call(store):
    _insert_card(store, "old")
    _insert_card(
        store, "new", status="finished", worker_status="done",
        accepted_request_id="req-accepted",
    )
    nf = _linked_needfix(store, "successor already accepted", "old")

    result = core.supersede_task("old", reason="replaced", by="new")

    assert result["ok"] is True, result
    rebind = result["needfix_rebind"]
    assert rebind["state"] == "rebound"
    assert rebind["closure"]["state"] == "closed"
    assert rebind["closure"]["needfix_id"] == nf
    row = _row(store, nf)
    assert row["status"] == "resolved"
    assert row["converted_task_id"] == "new"
    closed = _events(store, nf, "accepted_task_closed")
    assert len(closed) == 1
    assert closed[0]["accepted_request_id"] == "req-accepted"


def test_rebind_failure_never_undoes_supersede(store, monkeypatch):
    _insert_card(store, "old")
    _insert_card(store, "new")
    nf = _linked_needfix(store, "rebind failure", "old")

    def _boom(*args, **kwargs):
        raise needfix_store.NeedFixConflictError("injected rebind failure")

    monkeypatch.setattr(needfix_store, "rebind_superseded_task_link", _boom)

    result = core.supersede_task("old", reason="replaced", by="new")

    assert result["ok"] is True, result
    assert result["needfix_rebind"]["ok"] is False
    assert result["needfix_rebind"]["state"] == "error"
    assert "injected rebind failure" in result["needfix_rebind"]["error"]
    assert task_store.get_task(store, "old")["archive_operation"] == "superseded"
    assert _row(store, nf)["converted_task_id"] == "old"
