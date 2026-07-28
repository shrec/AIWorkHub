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
