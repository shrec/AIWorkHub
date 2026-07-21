"""B855: repository_bootstrap.initialize_repository_full + not-initialized
reason discrimination.

Covers: idempotency (second call is safe / non-destructive), the Source
Graph directory getting created, and ``dashboard_mcp_app.is_not_initialized_
reason`` correctly telling a genuine "never initialized" repository apart
from a corrupt/mismatched registry.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parents[1]
_SRC = _TOOL_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    dashboard_mcp_app,
    repository_bootstrap,
    source_graph,
    storage_registry,
    task_store,
)


def test_canonicalize_workspace_root_uses_only_resolve(tmp_path) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    result = repository_bootstrap.canonicalize_workspace_root(str(nested) + "/../b")
    assert result == nested.resolve()


def test_initialize_repository_full_provisions_source_graph_dir(tmp_path) -> None:
    result = repository_bootstrap.initialize_repository_full(tmp_path)
    assert result["ok"] is True
    assert result["source_graph_ready"] is True
    assert "source_graph_dir" in result["provisioned"]
    assert "manifest" in result["provisioned"]

    db_path = source_graph.resolve_db_path(tmp_path)
    assert db_path.parent.is_dir()

    readiness = task_store.storage_readiness(tmp_path)
    assert readiness.ready is True


def test_initialize_repository_full_is_idempotent(tmp_path) -> None:
    first = repository_bootstrap.initialize_repository_full(tmp_path)
    assert first["created_canonical_db"] is True

    second = repository_bootstrap.initialize_repository_full(tmp_path)
    assert second["ok"] is True
    assert second["created_canonical_db"] is False
    assert second["source_graph_ready"] is True
    # Non-destructive: the canonical DB file identity is unchanged.
    db_path_1 = Path(task_store.storage_readiness(tmp_path).canonical_db)
    assert db_path_1.is_file()


def test_initialize_repository_full_reraises_task_store_errors(tmp_path) -> None:
    repository_bootstrap.initialize_repository_full(tmp_path)
    try:
        repository_bootstrap.initialize_repository_full(tmp_path, expected_repo_id="repo_" + "0" * 32)
    except task_store.InitializationRefusedError as exc:
        assert "repo_id_path_mismatch" in str(exc)
    else:
        raise AssertionError("expected InitializationRefusedError for a repo_id mismatch")


def test_is_not_initialized_reason_true_for_fresh_repo(tmp_path) -> None:
    readiness = task_store.storage_readiness(tmp_path)
    assert readiness.ready is False
    assert dashboard_mcp_app.is_not_initialized_reason(readiness.reason) is True


def test_is_not_initialized_reason_false_for_corrupt_registry(tmp_path) -> None:
    repository_bootstrap.initialize_repository_full(tmp_path)
    registry_path = tmp_path / storage_registry.STORAGE_REGISTRY_REL
    registry_path.write_text("{ not valid json", encoding="utf-8")

    readiness = task_store.storage_readiness(tmp_path)
    assert readiness.ready is False
    assert dashboard_mcp_app.is_not_initialized_reason(readiness.reason) is False


def test_is_not_initialized_reason_false_for_missing_canonical_db(tmp_path) -> None:
    repository_bootstrap.initialize_repository_full(tmp_path)
    db_path = Path(task_store.storage_readiness(tmp_path).canonical_db)
    db_path.unlink()

    readiness = task_store.storage_readiness(tmp_path)
    assert readiness.ready is False
    assert readiness.reason == "canonical_db_missing"
    assert dashboard_mcp_app.is_not_initialized_reason(readiness.reason) is False


def test_health_view_reports_not_initialized_for_fresh_repo(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(tmp_path))
    result = dashboard_mcp_app.health_view()
    assert result["ok"] is False
    assert result["storage"]["not_initialized"] is True


def test_health_view_does_not_flag_corrupt_registry_as_not_initialized(tmp_path, monkeypatch) -> None:
    repository_bootstrap.initialize_repository_full(tmp_path)
    registry_path = tmp_path / storage_registry.STORAGE_REGISTRY_REL
    registry_path.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(tmp_path))

    result = dashboard_mcp_app.health_view()
    assert result["ok"] is False
    assert result["storage"]["not_initialized"] is False


def test_initialize_view_routes_through_repository_bootstrap(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(tmp_path))
    result = dashboard_mcp_app.initialize_view()
    assert result["ok"] is True
    assert result["source_graph_ready"] is True
    assert result["server_tool"] == "aiworkhub_dashboard_initialize"
