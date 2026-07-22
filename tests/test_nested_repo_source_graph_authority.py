from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import project_context, worker_ai_tools_mcp  # noqa: E402


def _card() -> dict:
    return {
        "task_id": "B816",
        "runner": "codex_worker",
        "topic": "task_mcp",
        "allowed_writes": [
            "tools/geoai-task-mcp/src/aiworkhub/project_context.py",
            "tools/geoai-task-mcp/tests/test_nested_repo_source_graph_authority.py",
        ],
        "project_context": {
            "required": True,
            "task_type": "code",
            "source_graph": {"mode": "focus", "query": "task", "budget": 16},
            "session": {"topic": "Route worker Source Graph to the task-owned nested repository", "limit": 2},
            "ai_memory": {"query": "Route worker Source Graph to the task-owned nested repository task_mcp", "limit": 2},
            "kb": {"query": "Route worker Source Graph to the task-owned nested repository task_mcp", "limit": 2},
        },
    }


def _repo_pair(tmp_path: Path) -> tuple[Path, Path]:
    outer = tmp_path / "outer"
    nested = outer / "tools" / "geoai-task-mcp"
    (outer / ".git").mkdir(parents=True)
    (nested / ".git").mkdir(parents=True)
    (nested / "src" / "aiworkhub").mkdir(parents=True)
    return outer, nested


def test_project_context_uses_nested_repo_authority_and_rebased_targets(tmp_path, monkeypatch) -> None:
    outer, nested = _repo_pair(tmp_path)

    def source_graph_direct(repo: Path, contract: dict) -> tuple[str, bool]:
        assert repo == nested.resolve()
        assert contract["source_graph"]["targets"] == [
            "src/aiworkhub/project_context.py",
            "tests/test_nested_repo_source_graph_authority.py",
        ]
        return json.dumps({"matches": [{"file_path": "src/aiworkhub/project_context.py", "name": "task"}]}), False

    monkeypatch.setattr(project_context, "_source_graph_direct", source_graph_direct)
    monkeypatch.setattr(
        project_context._worker_tools,
        "session_current_state",
        lambda ctx, limit: {
            "ok": True,
            "content": json.dumps({"evidence": [{"source_id": "1"}]}),
            "truncated": False,
            "hit_count": 1,
        },
    )
    monkeypatch.setattr(
        project_context._worker_tools,
        "ai_memory_search",
        lambda ctx, query, limit: {
            "ok": True,
            "content": json.dumps({"results": []}),
            "truncated": False,
            "hit_count": 0,
        },
    )
    monkeypatch.setattr(
        project_context._worker_tools,
        "kb_search",
        lambda ctx, query, limit: {
            "ok": True,
            "content": json.dumps({"results": []}),
            "truncated": False,
            "hit_count": 0,
        },
    )

    result = project_context.collect_project_context(outer, _card())
    assert result is not None
    assert result.metadata["repo_identity"] == {
        "repo_root": str(nested.resolve()),
        "scope_root": "tools/geoai-task-mcp",
    }
    payload = json.loads(result.prompt_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1])
    assert payload["repo_identity"]["scope_root"] == "tools/geoai-task-mcp"
    assert payload["source_graph"]["targets"][0] == "src/aiworkhub/project_context.py"


def test_root_repo_task_stays_on_outer_root(tmp_path) -> None:
    outer = tmp_path / "outer"
    (outer / ".git").mkdir(parents=True)
    card = {"allowed_writes": ["src/root_task.py"]}

    assert project_context.resolve_task_repository_root(outer, card) == outer.resolve()


def test_mixed_root_and_nested_repo_scope_fails_closed(tmp_path) -> None:
    outer, _nested = _repo_pair(tmp_path)
    card = _card()
    card["allowed_writes"] = [
        "README.md",
        "tools/geoai-task-mcp/src/aiworkhub/project_context.py",
    ]

    try:
        project_context.resolve_task_repository_root(outer, card)
    except project_context.ProjectContextError as exc:
        assert str(exc).startswith("task_repo_scope_ambiguous:")
    else:
        raise AssertionError("mixed repository scope should fail closed")


def test_nested_source_graph_call_records_authority_repo_in_audit(tmp_path, monkeypatch) -> None:
    outer, nested = _repo_pair(tmp_path)
    ledger = tmp_path / "audit.jsonl"
    key = tmp_path / "audit.key"
    key.write_bytes(b"k" * 32)
    ctx = worker_ai_tools_mcp.WorkerToolContext(
        task_id="B816",
        runner="codex_worker",
        topic="task_mcp",
        request_id="request-1",
        repo=outer.resolve(),
        authority_repo=nested.resolve(),
        source_graph_targets=("src/aiworkhub/project_context.py",),
        session_topic="task_mcp",
        audit_ledger_path=ledger,
        audit_hmac_key_path=key,
    )
    monkeypatch.setattr(
        worker_ai_tools_mcp,
        "_resolve_source_graph_db",
        lambda _ctx: worker_ai_tools_mcp.AuthorityBinding(
            db_path=nested / ".aiworkhub" / "source_graph" / "source_graph.sqlite",
            authority_source="canonical",
            authority_state="sole_authority",
        ),
    )
    from aiworkhub import source_graph

    monkeypatch.setattr(
        source_graph,
        "focus",
        lambda repo, query, budget: {
            "matches": [{"file_path": "src/aiworkhub/project_context.py", "name": query}],
            "query": query,
        },
    )
    worker_ai_tools_mcp._CACHE.clear()

    result = worker_ai_tools_mcp.source_graph_query(ctx, mode="focus", query="task", budget=8)
    assert result["ok"] is True
    assert result["hit_count"] > 0
    assert result["authority_repo"] == str(nested.resolve())

    verification = worker_ai_tools_mcp.verify_audit_ledger(
        ledger,
        key,
        task_id="B816",
        runner="codex_worker",
        topic="task_mcp",
        request_id="request-1",
    )
    assert verification["live_source_graph_calls"] == 1
    assert verification["authority_index_identity"] == [
        f"source_graph:canonical:sole_authority:{nested.resolve()}"
    ]
