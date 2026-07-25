from __future__ import annotations

import json
import sys
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import core, manager_ai_tools, server, shared_router, task_store  # noqa: E402


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


def test_callback_origin_requires_real_uuid_not_window_alias():
    assert core._valid_origin_thread_id("019f5097-6dbe-7172-870a-945afc5f3bfa")
    assert not core._valid_origin_thread_id("codex:window_33c3be4debf9f7ca38063548")
    assert not core._valid_origin_thread_id("claude:window_33c3be4debf9f7ca38063548")


def test_codex_vscode_env_identity_survives_route_pending(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    route_dir = root / ".aiworkhub" / "config" / "routing"
    route_dir.mkdir(parents=True, exist_ok=True)
    route = {
        "schema_id": "aiworkhub.coordinator_targets.v1",
        "repo_id": task_store.storage_readiness(root).repo_id,
        "selected_provider": "codex",
        "extension_host_pid": 12345,
        "window_id": "window_route_pending",
        "targets": {
            "codex": {
                "provider": "codex",
                "capability_state": "route_pending",
                "route": {
                    "window_id": "window_route_pending",
                    "thread_id": "",
                    "session_id": "episode_pending",
                },
            }
        },
    }
    (route_dir / "coordinator-targets.json").write_text(
        json.dumps(route, ensure_ascii=False), encoding="utf-8",
    )
    thread_id = "019f5097-6dbe-7172-870a-945afc5f3bfa"
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "codex_vscode")
    monkeypatch.setenv("CODEX_THREAD_ID", thread_id)
    monkeypatch.setenv("VSCODE_AGENT_FOLDER", "/tmp/vscode-agent")
    monkeypatch.setattr(core, "_pid_in_same_uid_ancestor_chain", lambda pid, *, max_depth: pid == 12345)

    identity = core._codex_vscode_env_manager_identity()

    assert identity == {
        "provider": "codex",
        "session_id": thread_id,
        "thread_id": thread_id,
        "window_id": "window_route_pending",
    }


def test_codex_extension_owned_mcp_identity_uses_persisted_route(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    route_dir = root / ".aiworkhub" / "config" / "routing"
    route_dir.mkdir(parents=True, exist_ok=True)
    thread_id = "019f5097-6dbe-7172-870a-945afc5f3bfa"
    repo_id = task_store.storage_readiness(root).repo_id
    route = {
        "schema_id": "aiworkhub.coordinator_targets.v1",
        "repo_id": repo_id,
        "selected_provider": "codex",
        "extension_host_pid": 12345,
        "window_id": "window_extension_owned",
        "targets": {
            "codex": {
                "provider": "codex",
                "capability_state": "available",
                "route": {
                    "repo_id": repo_id,
                    "window_id": "window_extension_owned",
                    "thread_id": thread_id,
                    "session_id": thread_id,
                },
            }
        },
    }
    (route_dir / "coordinator-targets.json").write_text(
        json.dumps(route, ensure_ascii=False), encoding="utf-8",
    )
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_WINDOW_ID", "window_extension_owned")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", raising=False)
    monkeypatch.setattr(core, "_pid_in_same_uid_ancestor_chain", lambda pid, *, max_depth: pid == 12345)

    identity = core._codex_extension_route_manager_identity()

    assert identity == {
        "provider": "codex",
        "session_id": thread_id,
        "thread_id": thread_id,
        "window_id": "window_extension_owned",
        "callback_supported": "true",
        "route_state": "available",
    }


def test_codex_extension_owned_route_pending_is_repo_local_manager_without_callback(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    route_dir = root / ".aiworkhub" / "config" / "routing"
    route_dir.mkdir(parents=True, exist_ok=True)
    repo_id = task_store.storage_readiness(root).repo_id
    route = {
        "schema_id": "aiworkhub.coordinator_targets.v1",
        "repo_id": repo_id,
        "selected_provider": "codex",
        "extension_host_pid": 12345,
        "window_id": "window_extension_owned",
        "claim_episode": "episode_pending",
        "targets": {
            "codex": {
                "provider": "codex",
                "capability_state": "route_pending",
                "route": {
                    "repo_id": repo_id,
                    "window_id": "window_extension_owned",
                    "thread_id": "",
                    "session_id": "episode_pending",
                },
            }
        },
    }
    (route_dir / "coordinator-targets.json").write_text(
        json.dumps(route, ensure_ascii=False), encoding="utf-8",
    )
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_WINDOW_ID", "window_extension_owned")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", raising=False)
    monkeypatch.setattr(core, "_pid_in_same_uid_ancestor_chain", lambda pid, *, max_depth: pid == 12345)

    identity = core._codex_extension_route_manager_identity()

    assert identity == {
        "provider": "codex",
        "session_id": "episode_pending",
        "thread_id": "",
        "window_id": "window_extension_owned",
        "callback_supported": "false",
        "route_state": "route_pending",
    }


def test_route_pending_is_enriched_from_live_mux_active_thread(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    route_dir = root / ".aiworkhub" / "config" / "routing"
    route_dir.mkdir(parents=True, exist_ok=True)
    repo_id = task_store.storage_readiness(root).repo_id
    route = {
        "schema_id": "aiworkhub.coordinator_targets.v1",
        "repo_id": repo_id,
        "selected_provider": "codex",
        "extension_host_pid": 12345,
        "window_id": "window_mux",
        "claim_episode": "episode_pending",
        "targets": {
            "codex": {
                "provider": "codex",
                "capability_state": "route_pending",
                "route": {
                    "repo_id": repo_id,
                    "window_id": "window_mux",
                    "thread_id": "",
                    "session_id": "episode_pending",
                },
            }
        },
    }
    (route_dir / "coordinator-targets.json").write_text(
        json.dumps(route, ensure_ascii=False), encoding="utf-8",
    )
    thread_id = "019f5097-6dbe-7172-870a-945afc5f3bfa"
    monkeypatch.setattr(core, "_live_mux_active_thread", lambda _root, _target: thread_id)

    enriched = core.read_selected_coordinator_target(root)
    codex = enriched["targets"]["codex"]

    assert codex["capability_state"] == "available"
    assert codex["route"]["thread_id"] == thread_id
    assert codex["route"]["session_id"] == thread_id
    assert codex["wake"] == {"mode": "app_server_sideband", "supported": True}


def test_task_create_callback_required_waits_for_real_origin_thread(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: {
        "provider": "codex",
        "session_id": "episode_pending",
        "thread_id": "",
        "window_id": "window_extension_owned",
        "callback_supported": "false",
        "route_state": "route_pending",
    })
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda runner: (True, "ok"))

    result = core.create_task(
        task_id="TASK_CALLBACK_PENDING",
        title="Callback route pending",
        runner="claude_callback_pending",
        topic="task_mcp",
        objective="Should not create callback-required cards without a real thread.",
        acceptance=["Fails closed."],
        allowed_writes=[],
        callback_required=True,
    )

    assert result["ok"] is False
    assert result["stderr"] == "callback_route_pending:codex_thread_id_not_observed"


def test_task_create_can_create_polling_only_card_from_route_pending_manager(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: {
        "provider": "codex",
        "session_id": "episode_pending",
        "thread_id": "",
        "window_id": "window_extension_owned",
        "callback_supported": "false",
        "route_state": "route_pending",
    })
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda runner: (True, "ok"))

    result = core.create_task(
        task_id="TASK_POLLING_ONLY",
        title="Polling only",
        runner="claude_polling_only",
        topic="task_mcp",
        objective="Create a route-pending manager task without callback delivery.",
        acceptance=["Card is persisted."],
        allowed_writes=[],
        callback_required=False,
    )

    assert result["ok"] is True, result
    card = json.loads(result["stdout"])
    assert card["origin_thread_id"] == ""
    assert card["callback_required"] is False
    assert card["callback_supported"] is False
    assert card["manager_route_state"] == "route_pending"


def test_codex_shared_repo_route_manager_identity_without_window_env(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    repo_id = task_store.storage_readiness(root).repo_id
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.delenv("AIWORKHUB_WINDOW_ID", raising=False)
    monkeypatch.setattr(
        shared_router,
        "list_known_repositories",
        lambda *, current_root, limit=32, include_inactive=False: {
            "ok": True,
            "repositories": [
                {
                    "repo_id": repo_id,
                    "current_repo": True,
                    "extension_host_alive": True,
                    "stale": False,
                    "selected_provider": "codex",
                    "window_id": "window_live",
                    "targets": {
                        "codex": {
                            "capability_state": "route_pending",
                            "route": {
                                "repo_id": repo_id,
                                "window_id": "window_live",
                                "thread_id": "",
                                "session_id": "episode_live",
                            },
                        }
                    },
                }
            ],
        },
    )

    identity = core._codex_shared_repo_route_manager_identity()

    assert identity == {
        "provider": "codex",
        "session_id": "episode_live",
        "thread_id": "",
        "window_id": "window_live",
        "callback_supported": "false",
        "route_state": "route_pending",
    }


def test_stale_synthetic_codex_route_is_downgraded_on_read(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    route_dir = root / ".aiworkhub" / "config" / "routing"
    route_dir.mkdir(parents=True, exist_ok=True)
    repo_id = task_store.storage_readiness(root).repo_id
    route = {
        "schema_id": "aiworkhub.coordinator_targets.v1",
        "repo_id": repo_id,
        "selected_provider": "codex",
        "window_id": "window_stale",
        "claim_episode": "episode_stale",
        "targets": {
            "codex": {
                "provider": "codex",
                "capability_state": "available",
                "route": {
                    "repo_id": repo_id,
                    "window_id": "window_stale",
                    "thread_id": "codex:window_stale",
                    "session_id": "episode_stale",
                },
                "wake": {"mode": "direct_api_or_callback_inbox", "supported": True},
            }
        },
    }
    (route_dir / "coordinator-targets.json").write_text(
        json.dumps(route, ensure_ascii=False), encoding="utf-8",
    )
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))

    selected = core.read_selected_coordinator_target(root)
    codex = selected["targets"]["codex"]

    assert codex["capability_state"] == "route_pending"
    assert codex["route"]["thread_id"] == ""
    assert codex["wake"]["supported"] is False
    assert codex["wake"]["reason"] == "codex_thread_id_not_observed"
