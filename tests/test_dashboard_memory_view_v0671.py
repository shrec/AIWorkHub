from __future__ import annotations

import sqlite3
from pathlib import Path

from aiworkhub import dashboard_mcp_app


def test_memory_view_reads_repo_canonical_db_without_mutation(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "memory.sqlite"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY, key TEXT, value TEXT, tags TEXT, "
        "scope TEXT, project TEXT, created_at TEXT, updated_at TEXT, access_count INTEGER)"
    )
    connection.execute(
        "INSERT INTO memories VALUES (1, 'older', 'old value', 'tag-a', 'persistent', '', "
        "'2026-01-01', '2026-01-01', 7)"
    )
    connection.execute(
        "INSERT INTO memories VALUES (2, 'newer', 'new value', 'tag-b', 'project', 'demo', "
        "'2026-01-02', '2026-01-03', 11)"
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(dashboard_mcp_app.core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(dashboard_mcp_app.storage_registry, "load_storage_registry", lambda _root: object())
    monkeypatch.setattr(dashboard_mcp_app.storage_registry, "resolve_database_path", lambda _registry, db_id: db)

    result = dashboard_mcp_app.memory_view(limit=1)
    assert result["ok"] is True
    assert result["total"] == 2
    assert result["count"] == 1
    assert result["truncated"] is True
    assert result["entries"][0]["key"] == "newer"

    readonly_check = sqlite3.connect(db).execute(
        "SELECT access_count FROM memories WHERE id = 2"
    ).fetchone()[0]
    assert readonly_check == 11


def test_memory_view_is_registered_as_bounded_readonly_tool() -> None:
    assert dashboard_mcp_app.MEMORY_TOOLS[dashboard_mcp_app.MEMORY_TOOL_NAME] is dashboard_mcp_app.memory_view
    assert dashboard_mcp_app.MAX_MEMORY_ROWS == 200


def test_memory_view_supports_fresh_minimal_schema(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "memory-minimal.sqlite"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY, key TEXT, value TEXT, tags TEXT, scope TEXT)"
    )
    connection.execute("INSERT INTO memories VALUES (1, 'decision', 'keep it', 'architecture', 'project')")
    connection.commit()
    connection.close()

    monkeypatch.setattr(dashboard_mcp_app.core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(dashboard_mcp_app.storage_registry, "load_storage_registry", lambda _root: object())
    monkeypatch.setattr(dashboard_mcp_app.storage_registry, "resolve_database_path", lambda _registry, _db_id: db)

    result = dashboard_mcp_app.memory_view(limit=10)
    assert result["ok"] is True
    assert result["entries"][0]["key"] == "decision"
    assert result["entries"][0]["project"] == ""
    assert result["entries"][0]["updated_at"] == ""


def test_session_and_kb_views_are_bounded_repo_local_reads(monkeypatch, tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.sqlite"
    connection = sqlite3.connect(transcript)
    connection.execute(
        "CREATE TABLE documents(doc_id INTEGER PRIMARY KEY,source_id TEXT,timestamp TEXT,kind TEXT,content TEXT)"
    )
    connection.execute("INSERT INTO documents VALUES (1, 'session:one', '2026-07-30T09:00:00Z', 'checkpoint', 'ready')")
    connection.commit()
    connection.close()

    kb = tmp_path / "knowledge.sqlite"
    connection = sqlite3.connect(kb)
    connection.execute(
        "CREATE TABLE entries(id INTEGER PRIMARY KEY,key TEXT,title TEXT,body TEXT,category TEXT,tags TEXT,source_refs TEXT)"
    )
    connection.execute("INSERT INTO entries VALUES (1, 'routing', 'Routing contract', 'repo isolated', 'architecture', 'mux', 'ADR-1')")
    connection.commit()
    connection.close()

    monkeypatch.setattr(dashboard_mcp_app.core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(dashboard_mcp_app.storage_registry, "load_storage_registry", lambda _root: object())
    monkeypatch.setattr(
        dashboard_mcp_app.storage_registry,
        "resolve_database_path",
        lambda _registry, db_id: transcript if db_id == "transcript" else kb,
    )

    sessions = dashboard_mcp_app.session_view(limit=10)
    knowledge = dashboard_mcp_app.kb_view(limit=10)
    assert sessions["ok"] is True
    assert sessions["entries"][0]["source_id"] == "session:one"
    assert knowledge["ok"] is True
    assert knowledge["entries"][0]["key"] == "routing"
    assert dashboard_mcp_app.SESSION_TOOLS[dashboard_mcp_app.SESSION_TOOL_NAME] is dashboard_mcp_app.session_view
    assert dashboard_mcp_app.KB_TOOLS[dashboard_mcp_app.KB_TOOL_NAME] is dashboard_mcp_app.kb_view
