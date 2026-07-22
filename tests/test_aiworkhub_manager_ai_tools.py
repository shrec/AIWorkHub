from __future__ import annotations

import json
import sys
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import core, manager_ai_tools, server, task_store  # noqa: E402


def _manager_route(root: Path) -> dict:
    session_id = "019f5097-6dbe-7172-870a-945afc5f3bfa"
    return {
        "ok": True,
        "role": "manager",
        "provider": "codex",
        "repo": str(root),
        "manager_route": {
            "provider": "codex",
            "session_id": session_id,
            "thread_id": session_id,
        },
    }


def test_manager_uses_same_canonical_ai_tools_as_workers(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(root))

    results = [
        manager_ai_tools.source_graph_query(mode="focus", query="AIWorkHub", budget=8),
        manager_ai_tools.session_current_state(topic="task_mcp", limit=1),
        manager_ai_tools.ai_memory_search(query="AIWorkHub", limit=1),
        manager_ai_tools.kb_search(query="AIWorkHub", limit=1),
    ]

    assert all(result["ok"] is True for result in results)
    assert all(result["surface"] == "manager_mcp" for result in results)
    assert all(result["authority_source"] == "canonical" for result in results)
    assert all(result["manager"]["repo"] == str(root) for result in results)


def test_unverified_client_cannot_use_manager_ai_tools(monkeypatch):
    monkeypatch.setattr(
        core,
        "manager_bootstrap",
        lambda: {"ok": True, "role": "worker_or_unverified_client", "manager_route": {}},
    )
    result = manager_ai_tools.ai_memory_search(query="anything")
    assert result == {"ok": False, "error": "verified_manager_identity_required"}


def test_main_mcp_exposes_complete_manager_ai_tool_surface():
    for name in (
        "aiworkhub_manager_source_graph_query",
        "aiworkhub_manager_session_current_state",
        "aiworkhub_manager_ai_memory_search",
        "aiworkhub_manager_kb_search",
        "aiworkhub_manager_kb_get",
        "aiworkhub_manager_kb_related",
    ):
        assert callable(getattr(server, name))


def test_task_create_public_schema_requires_automatic_project_context():
    import inspect

    server_signature = inspect.signature(server.aiworkhub_task_create)
    core_signature = inspect.signature(core.create_task)
    assert server_signature.parameters["task_type"].default == "code"
    assert core_signature.parameters["task_type"].default == "code"
    source = inspect.getsource(core.create_task)
    assert '"project_context"' in source
    assert '"required": True' in source
    assert '"source_graph"' in source
    assert '"session"' in source
    assert '"ai_memory"' in source
    assert '"kb"' in source


def test_task_create_persists_required_project_context(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    session_id = "019f5097-6dbe-7172-870a-945afc5f3bfa"
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: {
        "provider": "codex", "session_id": session_id, "thread_id": session_id,
    })
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda runner: (True, "ok"))

    result = core.create_task(
        task_id="TASK_CONTEXT_DEFAULT",
        title="Strict task context",
        runner="claude_context_default",
        topic="task_mcp",
        objective="Prove every manager-created code task receives mandatory AI context.",
        acceptance=["Context is persisted."],
        allowed_writes=["research/context_default.json"],
    )
    assert result["ok"] is True, result
    card = json.loads(result["stdout"])
    context = card["project_context"]
    assert context["required"] is True
    assert context["task_type"] == "code"
    assert context["source_graph"]["required"] is True
    assert context["source_graph"]["query"] == "task"
    assert context["session"]["topic"] == "Strict task context"
    assert context["ai_memory"]["query"]
    assert context["kb"]["query"]
    stored = task_store.get_task(root, "TASK_CONTEXT_DEFAULT")
    assert stored is not None
    assert stored["project_context"] == context
