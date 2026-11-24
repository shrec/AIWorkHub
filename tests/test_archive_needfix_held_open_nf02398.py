"""NF-2026-02398: archive refuses a card that an open NeedFix still holds.

A card whose task_id is the ``converted_task_id`` of a non-terminal NeedFix
(e.g. ``task_created``) must be listed under ``refused`` by the batch selector
and refused by ``core.archive_task``; archiving it would orphan the NeedFix.
Terminal NeedFixes and a repo without a NeedFix store archive as before.
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

from aiworkhub import core, needfix_store, task_engine, task_store  # noqa: E402


def _insert_card(repo: Path, task_id: str, *, status: str = "blocked") -> None:
    _readiness, db_path = task_store._require_ready(repo)
    card = {
        "task_id": task_id,
        "runner": "codex_a",
        "topic": "coding",
        "allowed_writes": [],
        "depends_on": [],
        "priority": "",
        "status": status,
        "worker_status": "unclaimed",
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, archived_at) "
            "VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, '')",
            (
                task_id, "codex_a", "coding", status, "unclaimed", json.dumps(card),
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _archived_at(repo: Path, task_id: str) -> str:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT archived_at FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    return str(row[0] or "")


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


def _linked_needfix(repo: Path, task_id: str, status: str = "task_created") -> str:
    rec = needfix_store.capture_proposal(repo, title=f"defect {task_id}", description="d")
    conn = sqlite3.connect(str(needfix_store._db_path(repo)))
    try:
        conn.execute(
            "UPDATE needfix SET converted_task_id = ?, status = ? WHERE id = ?",
            (task_id, status, rec["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return rec["id"]


def test_selector_refuses_card_held_by_open_needfix(store):
    _insert_card(store, "held")
    _insert_card(store, "free")
    nf = _linked_needfix(store, "held")

    out = task_engine.select_archivable_tasks(store, status="blocked", older_than_hours=168)

    selected_ids = [e["task_id"] for e in out["selected"]]
    assert selected_ids == ["free"]
    assert out["selected_count"] == 1
    held = [r for r in out["refused"] if r["task_id"] == "held"]
    assert held == [
        {**held[0], "reason": "needfix_held_open", "needfix_id": nf}
    ]


def test_explicit_archive_refuses_card_held_by_open_needfix(store):
    _insert_card(store, "held")
    nf = _linked_needfix(store, "held")

    result = core.archive_task("held", reason="stale")

    assert result["ok"] is False, result
    assert f"archive_failed:needfix_held_open:{nf}" in str(result.get("stderr"))
    assert _archived_at(store, "held") == ""


@pytest.mark.parametrize("terminal", sorted(needfix_store.NEEDFIX_TERMINAL_STATUSES))
def test_terminal_needfix_does_not_hold_card(store, terminal):
    _insert_card(store, "done")
    _linked_needfix(store, "done", status=terminal)

    out = task_engine.select_archivable_tasks(store, status="blocked")
    assert [e["task_id"] for e in out["selected"]] == ["done"]
    assert out["refused"] == []

    result = core.archive_task("done", reason="stale")
    assert result["ok"] is True, result
    assert _archived_at(store, "done") != ""


def test_repo_without_needfix_store_archives_as_before(repo):
    db = needfix_store._db_path(repo)
    for suffix in ("", "-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)
    assert not needfix_store._db_path(repo).is_file()
    _insert_card(repo, "plain")

    out = task_engine.select_archivable_tasks(repo, status="blocked")
    assert [e["task_id"] for e in out["selected"]] == ["plain"]
    assert out["refused"] == []

    result = core.archive_task("plain", reason="stale")
    assert result["ok"] is True, result
    assert _archived_at(repo, "plain") != ""
    # The lookup never creates a store as a side effect.
    assert not needfix_store._db_path(repo).is_file()


def test_helper_maps_only_open_links(store):
    open_nf = _linked_needfix(store, "a")
    _linked_needfix(store, "b", status="resolved")
    assert needfix_store.open_needfix_ids_by_task(store, ["a", "b", "c", ""]) == {"a": open_nf}
    assert needfix_store.open_needfix_ids_by_task(store, []) == {}
