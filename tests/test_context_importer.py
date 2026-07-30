from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import context_importer, core, manager_ai_tools, server, storage_registry, task_store  # noqa: E402


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    return root


def _legacy(root: Path, component: str) -> Path:
    path = root / "legacy" / f"{component}.sqlite"
    path.parent.mkdir()
    con = sqlite3.connect(path)
    try:
        if component == "session":
            con.execute(
                "CREATE TABLE documents(doc_id INTEGER PRIMARY KEY,source_id TEXT,timestamp TEXT,kind TEXT,content TEXT)"
            )
            con.executemany(
                "INSERT INTO documents(source_id,timestamp,kind,content) VALUES(?,?,?,?)",
                [
                    ("old:one", "2026-01-01T00:00:00Z", "checkpoint", "first"),
                    ("old:two", "2026-01-02T00:00:00Z", "event", "second"),
                ],
            )
        elif component == "memory":
            con.execute(
                "CREATE TABLE memories(id INTEGER PRIMARY KEY,key TEXT,value TEXT,tags TEXT,scope TEXT)"
            )
            con.executemany(
                "INSERT INTO memories(key,value,tags,scope) VALUES(?,?,?,?)",
                [("one", "value-1", "tag", "project"), ("two", "value-2", "tag", "project")],
            )
        else:
            con.execute(
                "CREATE TABLE entries(id INTEGER PRIMARY KEY,key TEXT UNIQUE,title TEXT,body TEXT,category TEXT,tags TEXT,source_refs TEXT)"
            )
            con.executemany(
                "INSERT INTO entries(key,title,body,category,tags,source_refs) VALUES(?,?,?,?,?,?)",
                [
                    ("one", "One", "body-1", "architecture", "tag", "legacy"),
                    ("two", "Two", "body-2", "architecture", "tag", "legacy"),
                ],
            )
        con.commit()
    finally:
        con.close()

    return path


def _canonical(repo: Path, component: str) -> Path:
    registry = storage_registry.load_storage_registry(repo)
    db_id = {"session": "transcript", "memory": "memory", "kb": "kb"}[component]
    return storage_registry.resolve_database_path(registry, db_id)


@pytest.mark.parametrize("component", ["session", "memory", "kb"])
def test_explicit_context_import_dry_apply_idempotent_and_rollback(tmp_path, component):
    repo = _repo(tmp_path)
    source = _legacy(repo, component)
    relative = str(source.relative_to(repo))

    dry = context_importer.import_context(
        repo, component=component, operation="dry_run", source_path=relative,
    )
    assert dry["source"] == 2
    assert dry["new"] == 2
    assert dry["duplicate"] == 0
    con = sqlite3.connect(_canonical(repo, component))
    try:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    assert "context_import_runs" not in tables, "dry-run must not mutate canonical storage"

    kwargs = {
        "component": component,
        "operation": "apply",
        "source_path": relative,
        "idempotency_key": f"context-import:{component}:0001",
        "actor_id": "manager-thread",
        "provider": "codex",
        "provenance": "test:legacy-fixture",
    }
    applied = context_importer.import_context(repo, **kwargs)
    repeated = context_importer.import_context(repo, **kwargs)

    assert applied["inserted"] == 2
    assert applied["idempotent"] is False
    assert repeated["idempotent"] is True
    assert repeated["import_id"] == applied["import_id"]

    second_dry = context_importer.import_context(
        repo, component=component, operation="dry_run", source_path=relative,
    )
    assert second_dry["new"] == 0
    assert second_dry["duplicate"] == 2

    rolled_back = context_importer.import_context(
        repo,
        component=component,
        operation="rollback",
        import_id=applied["import_id"],
        actor_id="manager-thread",
        provider="codex",
        provenance="test:rollback",
    )
    rolled_back_again = context_importer.import_context(
        repo,
        component=component,
        operation="rollback",
        import_id=applied["import_id"],
        actor_id="manager-thread",
        provider="codex",
        provenance="test:rollback-retry",
    )
    assert rolled_back["removed"] == 2
    assert rolled_back_again["idempotent"] is True
    final_dry = context_importer.import_context(
        repo, component=component, operation="dry_run", source_path=relative,
    )
    assert final_dry["new"] == 2


def test_context_import_reports_conflict_without_overwrite(tmp_path):
    repo = _repo(tmp_path)
    source = _legacy(repo, "memory")
    canonical = _canonical(repo, "memory")
    con = sqlite3.connect(canonical)
    try:
        con.execute(
            "INSERT INTO memories(key,value,tags,scope) VALUES('one','canonical','tag','project')"
        )
        con.commit()
    finally:
        con.close()

    report = context_importer.import_context(
        repo,
        component="memory",
        operation="dry_run",
        source_path=str(source.relative_to(repo)),
    )

    assert report["conflict"] == 1
    assert report["new"] == 1
    con = sqlite3.connect(canonical)
    try:
        assert con.execute("SELECT value FROM memories WHERE key='one'").fetchone()[0] == "canonical"
    finally:
        con.close()


def test_context_import_rollback_refuses_to_delete_post_import_changes(tmp_path):
    repo = _repo(tmp_path)
    source = _legacy(repo, "memory")
    applied = context_importer.import_context(
        repo,
        component="memory",
        operation="apply",
        source_path=str(source.relative_to(repo)),
        idempotency_key="context-import:memory:changed-row",
        actor_id="manager-thread",
        provider="codex",
        provenance="test:legacy-fixture",
    )
    canonical = _canonical(repo, "memory")
    con = sqlite3.connect(canonical)
    try:
        con.execute("UPDATE memories SET value='new canonical work' WHERE key='one'")
        con.commit()
    finally:
        con.close()

    with pytest.raises(context_importer.ContextImportError, match="rollback_entity_changed"):
        context_importer.import_context(
            repo,
            component="memory",
            operation="rollback",
            import_id=applied["import_id"],
            actor_id="manager-thread",
            provider="codex",
            provenance="test:rollback",
        )

    con = sqlite3.connect(canonical)
    try:
        assert con.execute("SELECT value FROM memories WHERE key='one'").fetchone()[0] == "new canonical work"
        assert con.execute(
            "SELECT status FROM context_import_runs WHERE import_id=?", (applied["import_id"],)
        ).fetchone()[0] == "applied"
    finally:
        con.close()

def test_context_import_rejects_absolute_traversal_and_canonical_sources(tmp_path):
    repo = _repo(tmp_path)
    source = _legacy(repo, "memory")
    for bad in (str(source), "../outside.sqlite", ".aiworkhub/memory/memory.sqlite"):
        with pytest.raises(context_importer.ContextImportError):
            context_importer.import_context(
                repo, component="memory", operation="dry_run", source_path=bad,
            )


def test_manager_import_surface_requires_write_gate_only_for_mutation(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    source = _legacy(repo, "memory")
    thread = "019f5097-6dbe-7172-870a-945afc5f3bfa"
    monkeypatch.setattr(core, "manager_bootstrap", lambda: {
        "ok": True,
        "role": "manager",
        "provider": "codex",
        "repo": str(repo),
        "manager_route": {"provider": "codex", "thread_id": thread, "session_id": thread},
    })
    monkeypatch.setattr(core, "writes_allowed", lambda: False)

    dry = manager_ai_tools.context_import(
        component="memory", operation="dry_run", source_path=str(source.relative_to(repo)),
    )
    denied = manager_ai_tools.context_import(
        component="memory", operation="apply", source_path=str(source.relative_to(repo)),
        idempotency_key="context-import:memory:manager", provenance="test",
    )

    assert dry["ok"] is True and dry["surface"] == "manager_mcp"
    assert denied["error"] == "write_gate_closed"
    assert callable(server.aiworkhub_manager_context_import)
