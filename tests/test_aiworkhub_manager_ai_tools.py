from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import (  # noqa: E402
    context_writes,
    callback_store,
    core,
    feature_settings,
    manager_ai_tools,
    server,
    shared_router,
    source_graph,
    storage_registry,
    task_store,
    workforce_catalog,
    worker_ai_tools_mcp as worker_tools,
)
from aiworkhub.worker_workspace import materialize_rework_overlay  # noqa: E402


def test_windows_ancestor_chain_accepts_only_same_owner_bounded_path(monkeypatch):
    parents = {300: 200, 200: 100, 100: 10}
    monkeypatch.setattr(core, "_windows_process_parent_map", lambda: parents)
    monkeypatch.setattr(core.os, "getpid", lambda: 400)
    monkeypatch.setattr(
        core,
        "_windows_process_owner_sid",
        lambda pid: "S-1-test" if pid in {400, 300, 200, 100} else "S-1-other",
    )

    assert core._pid_in_same_windows_user_ancestor_chain(
        100, max_depth=3, start_pid=300,
    )
    assert not core._pid_in_same_windows_user_ancestor_chain(
        100, max_depth=2, start_pid=300,
    )
    assert not core._pid_in_same_windows_user_ancestor_chain(
        10, max_depth=4, start_pid=300,
    )


def test_windows_ancestor_chain_fails_closed_without_process_snapshot(monkeypatch):
    monkeypatch.setattr(core, "_windows_process_parent_map", lambda: None)
    monkeypatch.setattr(core, "_windows_process_owner_sid", lambda pid: "S-1-test")

    assert not core._pid_in_same_windows_user_ancestor_chain(
        123, max_depth=4, start_pid=456,
    )


def test_codex_manager_identity_dispatches_to_native_windows_verifier(monkeypatch):
    expected = {
        "provider": "codex",
        "session_id": "episode_windows",
        "thread_id": "",
        "window_id": "window_windows",
    }
    monkeypatch.setattr(core, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(core, "_codex_vscode_env_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_extension_route_manager_identity", lambda: expected)
    monkeypatch.setattr(
        core,
        "_codex_shared_repo_route_manager_identity",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected fallback")),
    )

    assert core._codex_manager_identity() == expected


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


def _manager_rework_context(tmp_path: Path) -> tuple[worker_tools.WorkerToolContext, dict, dict]:
    authority = tmp_path / "manager_rework_authority"
    workspace = tmp_path / "manager_rework_workspace"
    authority.mkdir()
    workspace.mkdir()
    assert task_store.initialize_repository(authority)["ok"]
    assert task_store.initialize_repository(workspace)["ok"]
    (authority / "src").mkdir()
    (authority / "src" / "mod.py").write_text(
        "def canonical_only():\n    return 1\n", encoding="utf-8",
    )
    source_graph.build_index(authority, incremental=False)
    body = (
        "def manager_overlay_target():\n"
        + "".join(f"    # manager padding {i}\n" for i in range(90))
        + "    return 1\n"
    ).encode("utf-8")
    target = workspace / "src" / "mod.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    packet = json.loads(
        materialize_rework_overlay(
            "manager-successor",
            "manager-task",
            "manager-predecessor",
            "manager-task",
            authority,
            [("src/mod.py", hashlib.sha256(body).hexdigest(), body)],
        )
    )
    runtime = tmp_path / "manager_runtime"
    runtime.mkdir()
    packet_path = runtime / "rework_overlay.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = worker_tools.WorkerToolContext(
        task_id="manager-task",
        runner="codex_manager",
        topic="management",
        request_id="manager-successor",
        repo=workspace,
        authority_repo=authority,
        source_graph_targets=("src/mod.py",),
        allowed_writes=("src/mod.py",),
        session_topic="management",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        rework_overlay_packet=packet,
        rework_overlay_packet_path=packet_path,
    )
    manager = {
        "provider": "codex",
        "session_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
        "repo": str(authority),
    }
    return ctx, packet, manager


def test_manager_uses_same_canonical_ai_tools_as_workers(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    source_graph.build_index(root)
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


def test_manager_source_graph_continuation_rejects_stale_rework_overlay_authority(
    tmp_path, monkeypatch,
):
    ctx, packet, manager = _manager_rework_context(tmp_path)
    monkeypatch.setattr(manager_ai_tools, "_manager_context", lambda **_kwargs: (ctx, manager))
    monkeypatch.setattr(worker_tools, "_source_graph_output_cap", lambda mode: 2048)

    first = manager_ai_tools.source_graph_query(
        mode="body",
        query="manager_overlay_target",
        target="src/mod.py",
        budget=8,
        workflow_stage="rework",
    )
    assert first["ok"] is True
    assert first["authority_source"] == "rework_overlay"
    assert first["continuation_cursor"]

    unchanged = manager_ai_tools.source_graph_query(
        mode="body",
        query="manager_overlay_target",
        target="src/mod.py",
        budget=8,
        workflow_stage="rework",
        continuation_cursor=first["continuation_cursor"],
    )
    assert unchanged["ok"] is True
    assert unchanged["page_index"] == 1

    second = manager_ai_tools.source_graph_query(
        mode="body",
        query="manager_overlay_target",
        target="src/mod.py",
        budget=8,
        workflow_stage="rework",
    )
    packet["predecessor_request_id"] = "manager-predecessor-stale"
    stale = manager_ai_tools.source_graph_query(
        mode="body",
        query="manager_overlay_target",
        target="src/mod.py",
        budget=8,
        workflow_stage="rework",
        continuation_cursor=second["continuation_cursor"],
    )
    assert stale["ok"] is False
    assert stale["reason"] == "continuation_authority_mismatch"


def test_unverified_client_cannot_use_manager_ai_tools(monkeypatch):
    monkeypatch.setattr(
        core,
        "manager_bootstrap",
        lambda: {"ok": True, "role": "worker_or_unverified_client", "manager_route": {}},
    )
    result = manager_ai_tools.ai_memory_search(query="anything")
    assert result == {"ok": False, "error": "verified_manager_identity_required"}


def test_manager_context_writes_require_write_gate_and_are_repo_bound(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(root))
    monkeypatch.setattr(core, "writes_allowed", lambda: False)
    denied = manager_ai_tools.session_write(
        action="checkpoint", topic="release", content="not written",
        idempotency_key="session:manager:0001", provenance="test",
    )
    assert denied["error"] == "write_gate_closed"

    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    written = manager_ai_tools.session_write(
        action="checkpoint", topic="release", content="written",
        idempotency_key="session:manager:0002", provenance="test",
    )
    assert written["ok"] is True
    assert written["manager"]["repo"] == str(root)
    assert written["surface"] == "manager_mcp"


def test_manager_ai_memory_read_surface_closes_get_search_related_cycle(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(root))
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    for suffix, key in (("a", "routing.contract"), ("b", "callback.contract")):
        result = manager_ai_tools.ai_memory_write(
            action="remember", key=key, value=f"value-{suffix}",
            tags="routing,callback", scope="project",
            idempotency_key=f"memory:manager:{suffix}:0001", provenance="test",
        )
        assert result["ok"] is True

    exact = manager_ai_tools.ai_memory_get(key="routing.contract")
    related = manager_ai_tools.ai_memory_related(key="routing.contract")

    assert json.loads(exact["content"])["memory"]["value"] == "value-a"
    assert json.loads(related["content"])["related"][0]["key"] == "callback.contract"


def _seed_searchable_memory(root: Path, *, strand: bool) -> Path:
    """Write one memory canonically; ``strand`` then drops memories_fts, as a
    legacy or half-migrated store lacks it.  Returns the memory database path."""
    context_writes.memory_write(
        root,
        actor={
            "role": "manager", "actor_id": "thread-1", "task_id": "",
            "provider": "codex", "session_id": "thread-1",
        },
        action="remember", key="routing.contract", value="callback gate passed",
        idempotency_key="memory:manager:fts:0001", provenance="test",
    )
    db = storage_registry.resolve_database_path(
        storage_registry.load_storage_registry(root), "memory"
    )
    if strand:
        con = sqlite3.connect(str(db))
        try:
            con.execute("DROP TABLE memories_fts")
            con.commit()
        finally:
            con.close()
    return db


def _memory_schema(db: Path) -> list[tuple]:
    con = sqlite3.connect(str(db))
    try:
        return [
            tuple(row) for row in con.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall()
        ]
    finally:
        con.close()


def _has_memories_fts(db: Path) -> bool:
    return any(name == "memories_fts" for _type, name, _sql in _memory_schema(db))


def test_manager_ai_memory_search_repairs_missing_fts_and_returns_hits(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(root))
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    db = _seed_searchable_memory(root, strand=True)
    assert not _has_memories_fts(db)

    # The worker-facing tool only reads: it reports the missing index, never writes.
    ctx, _manager = manager_ai_tools._manager_context()
    unrepaired = worker_tools.ai_memory_search(ctx, query="callback", limit=3)
    assert unrepaired["ok"] is False
    assert unrepaired["reason"] == "fts_unavailable:memories_fts_absent"
    assert not _has_memories_fts(db)

    result = manager_ai_tools.ai_memory_search(query="callback", limit=3)

    assert result["ok"] is True
    assert result["surface"] == "manager_mcp"
    assert "fts_repair" not in result
    assert [hit["key"] for hit in json.loads(result["content"])["results"]] == ["routing.contract"]
    assert _has_memories_fts(db)


def test_manager_ai_memory_search_leaves_a_stranded_store_untouched_while_the_write_gate_is_closed(
    tmp_path, monkeypatch
):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(root))
    db = _seed_searchable_memory(root, strand=True)
    schema_before = _memory_schema(db)
    repaired_repos = []
    real_ensure = context_writes.ensure_memories_fts

    def spy(repo):
        repaired_repos.append(repo)
        return real_ensure(repo)

    monkeypatch.setattr(context_writes, "ensure_memories_fts", spy)
    monkeypatch.setattr(core, "writes_allowed", lambda: False)

    closed = manager_ai_tools.ai_memory_search(query="callback", limit=3)

    # A read surface behind a closed write gate: it reports the missing index and
    # repairs nothing -- the store is not even opened for the repair.
    assert closed["ok"] is False
    assert closed["reason"] == "fts_unavailable:memories_fts_absent"
    assert "fts_repair" not in closed
    assert manager_ai_tools._repair_memory_fts(root) == {
        "ok": True, "created": False, "reason": "write_gate_closed",
    }
    assert repaired_repos == []
    assert _memory_schema(db) == schema_before

    # The very same search repairs the store once the manager's write gate opens.
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    opened = manager_ai_tools.ai_memory_search(query="callback", limit=3)

    assert opened["ok"] is True
    assert repaired_repos == [root.resolve()]
    assert [hit["key"] for hit in json.loads(opened["content"])["results"]] == ["routing.contract"]
    assert _has_memories_fts(db)


def test_manager_ai_memory_search_repair_is_a_noop_on_a_healthy_store(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(root))
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    db = _seed_searchable_memory(root, strand=False)
    schema_before = _memory_schema(db)
    outcomes = []
    real_ensure = context_writes.ensure_memories_fts

    def spy(repo):
        outcome = real_ensure(repo)
        outcomes.append((repo, outcome))
        return outcome

    monkeypatch.setattr(context_writes, "ensure_memories_fts", spy)

    result = manager_ai_tools.ai_memory_search(query="callback", limit=3)

    assert result["ok"] is True
    assert result["hit_count"] == 1
    assert "fts_repair" not in result
    # Once, on the manager's authority repo, and nothing to repair.
    assert outcomes == [
        (root.resolve(), {"ok": True, "created": False, "reason": "fts_already_exists"})
    ]
    assert _memory_schema(db) == schema_before


def test_manager_ai_memory_search_survives_a_failed_fts_repair(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(root))
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    db = _seed_searchable_memory(root, strand=True)

    def raising(repo):
        raise sqlite3.OperationalError("disk I/O error")

    def reporting(repo):
        return {"ok": False, "error": "fts_lock_blocked", "detail": "database is locked"}

    for broken, expected_error in ((raising, "fts_repair_raised"), (reporting, "fts_lock_blocked")):
        monkeypatch.setattr(context_writes, "ensure_memories_fts", broken)

        result = manager_ai_tools.ai_memory_search(query="callback", limit=3)

        # Still the search's own fts_unavailable result, now carrying the repair error.
        assert result["ok"] is False
        assert result["reason"] == "fts_unavailable:memories_fts_absent"
        assert result["surface"] == "manager_mcp"
        assert result["fts_repair"]["ok"] is False
        assert result["fts_repair"]["error"] == expected_error
        assert not _has_memories_fts(db)


def test_manager_ai_memory_search_leaves_a_disabled_store_untouched(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(root))
    # An open gate, so it is the disabled feature alone that keeps the store untouched.
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    db = _seed_searchable_memory(root, strand=True)
    monkeypatch.setattr(feature_settings, "enabled", lambda repo_path, name: name != "ai_memory")
    monkeypatch.setattr(
        context_writes,
        "ensure_memories_fts",
        lambda repo: (_ for _ in ()).throw(AssertionError("a disabled feature must not be repaired")),
    )

    result = manager_ai_tools.ai_memory_search(query="callback", limit=3)

    assert result["ok"] is False
    assert result["error"] == "feature_disabled:ai_memory"
    assert "fts_repair" not in result
    assert not _has_memories_fts(db)


def test_main_mcp_exposes_complete_manager_ai_tool_surface():
    for name in (
        "aiworkhub_manager_source_graph_query",
        "aiworkhub_manager_session_current_state",
        "aiworkhub_manager_ai_memory_search",
        "aiworkhub_manager_ai_memory_get",
        "aiworkhub_manager_ai_memory_related",
        "aiworkhub_manager_kb_search",
        "aiworkhub_manager_kb_get",
        "aiworkhub_manager_kb_related",
        "aiworkhub_manager_session_write",
        "aiworkhub_manager_ai_memory_write",
        "aiworkhub_manager_kb_write",
        "aiworkhub_manager_learning_commit",
        "aiworkhub_manager_needfix_markdown_preview",
        "aiworkhub_manager_needfix_markdown_commit",
        "aiworkhub_manager_context_write_intents",
        "aiworkhub_manager_context_write_intent_dispose",
        "aiworkhub_manager_context_import",
        "aiworkhub_manager_context_graph_search",
        "aiworkhub_manager_context_graph_range",
        "aiworkhub_manager_context_graph_related",
        "aiworkhub_manager_context_graph_event_write",
        "aiworkhub_manager_context_graph_rebuild",
        "aiworkhub_manager_workforce_catalog",
        "aiworkhub_manager_workforce_rank",
        "aiworkhub_manager_workforce_upsert",
        "aiworkhub_repo_list",
        "aiworkhub_repo_current",
        "aiworkhub_repo_switch",
    ):
        assert callable(getattr(server, name))


def test_manager_context_graph_is_repo_bound_and_write_gated(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    feature_settings.update(
        root,
        changes={"context_graph": True},
        expected_revision=0,
    )
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(root))
    monkeypatch.setattr(core, "writes_allowed", lambda: False)
    denied = manager_ai_tools.context_graph_event_write(
        role="manager",
        event_type="checkpoint",
        content="not written",
        source_ref="test:denied",
        idempotency_key="context-manager-denied-0001",
    )
    assert denied["error"] == "write_gate_closed"

    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    written = manager_ai_tools.context_graph_event_write(
        role="manager",
        event_type="checkpoint",
        content="manager context graph evidence",
        source_ref="test:allowed",
        idempotency_key="context-manager-allowed-0001",
        task_id="TASK-1",
    )
    found = manager_ai_tools.context_graph_search(query="graph evidence")

    assert written["ok"] is True
    assert written["manager"]["repo"] == str(root)
    assert found["count"] == 1
    assert found["results"][0]["task_id"] == "TASK-1"


def test_manager_workforce_reads_only_authority_repo_process_log(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(root))
    observed: list[Path] = []
    # The reader moved into workforce_catalog.default_process_rows when the
    # hand-assembled catalog collapsed onto build_routing_catalog. The contract
    # under test is unchanged: the process ledger is read from the exact
    # authority repository, never an ambient ProcessManager's default.
    monkeypatch.setattr(
        workforce_catalog,
        "default_process_rows",
        lambda repo, **kwargs: observed.append(Path(repo)) or [],
    )
    result = manager_ai_tools.workforce_catalog_read()

    assert result["ok"] is True
    assert observed == [root.resolve()]
    assert result["manager"]["repo"] == str(root)


def test_repository_switch_is_repo_id_only_and_preserves_current_manager(tmp_path, monkeypatch):
    old = tmp_path / "old"
    target = tmp_path / "target"
    old.mkdir()
    target.mkdir()
    assert task_store.initialize_repository(old)["ok"]
    assert task_store.initialize_repository(target)["ok"]
    old_id = task_store.storage_readiness(old).repo_id
    target_id = task_store.storage_readiness(target).repo_id
    thread_id = "019f5097-6dbe-7172-870a-945afc5f3bfa"
    identity = {
        "provider": "codex", "session_id": thread_id, "thread_id": thread_id,
        "window_id": "window_switch",
    }
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT", raising=False)
    monkeypatch.setenv("AIWORKHUB_REPO", str(old))
    monkeypatch.setattr(core, "_PROCESS_REPO_ROOT_OVERRIDE", None)
    monkeypatch.setattr(core, "_implicit_codex_repository_root", lambda: None)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: identity)
    monkeypatch.setattr(
        shared_router, "registry_dir", lambda home=None: tmp_path / "router" / "repos"
    )
    monkeypatch.setattr(
        shared_router,
        "list_known_repositories",
        lambda **kwargs: {
            "ok": True,
            "repositories": [
                {
                    "repo_id": old_id, "repo_root": str(old), "repo_name": "old",
                    "window_id": "window_switch", "extension_host_alive": True, "stale": False,
                    "targets": {"codex": {"route": {"repo_id": old_id, "thread_id": thread_id}}},
                },
                {
                    "repo_id": target_id, "repo_root": str(target), "repo_name": "target",
                    "window_id": "window_target", "extension_host_alive": True, "stale": False,
                    "targets": {"codex": {"route": {"repo_id": target_id, "thread_id": ""}}},
                },
            ],
        },
    )
    lifecycle = {"stopped_dispatcher": [], "stopped_daemon": [], "started_daemon": []}
    monkeypatch.setattr(
        core,
        "_callback_bridge_module",
        lambda: type("Bridge", (), {"stop_dispatcher": lambda _self, root: lifecycle["stopped_dispatcher"].append(root)})(),
    )
    monkeypatch.setattr(
        core,
        "_source_graph_daemon_module",
        lambda: type("Daemon", (), {
            "stop_daemon": lambda _self, root: lifecycle["stopped_daemon"].append(root),
            "ensure_started": lambda _self, root: lifecycle["started_daemon"].append(root) or {"ok": True},
        })(),
    )
    monkeypatch.setattr(core, "dispatcher_ensure_started", lambda: {"ok": True, "status": "manager_inbox"})

    result = core.repository_switch(target_id)

    assert result["ok"] is True
    assert result["switched"] is True
    assert result["route_transfer"]["epoch"] == 1
    assert result["binding_source"] == "manager_switch"
    assert core.repo_root() == target.resolve()
    assert lifecycle["stopped_dispatcher"] == [old.resolve()]
    assert lifecycle["stopped_daemon"] == [old.resolve()]
    assert lifecycle["started_daemon"] == [target.resolve()]


def test_repository_switch_roundtrip_is_serialized_and_repo_local(tmp_path, monkeypatch):
    root_a = tmp_path / "repo_a"
    root_b = tmp_path / "repo_b"
    root_a.mkdir()
    root_b.mkdir()
    assert task_store.initialize_repository(root_a)["ok"]
    assert task_store.initialize_repository(root_b)["ok"]
    repo_a = task_store.storage_readiness(root_a).repo_id
    repo_b = task_store.storage_readiness(root_b).repo_id
    thread_id = "019f5097-6dbe-7172-870a-945afc5f3bfa"
    identity = {
        "provider": "codex", "session_id": thread_id, "thread_id": thread_id,
        "window_id": "window_roundtrip",
    }
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT", raising=False)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root_a))
    monkeypatch.setattr(core, "_PROCESS_REPO_ROOT_OVERRIDE", None)
    monkeypatch.setattr(core, "_implicit_codex_repository_root", lambda: None)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: identity)
    monkeypatch.setattr(
        shared_router, "registry_dir", lambda home=None: tmp_path / "router" / "repos"
    )

    def record(root: Path, repo_id: str) -> dict:
        return {
            "repo_id": repo_id,
            "repo_root": str(root),
            "window_id": "window_roundtrip",
            "extension_host_alive": True,
            "stale": False,
            "targets": {"codex": {"route": {"repo_id": repo_id, "thread_id": thread_id}}},
        }

    monkeypatch.setattr(
        shared_router,
        "list_known_repositories",
        lambda **kwargs: {"ok": True, "repositories": [record(root_a, repo_a), record(root_b, repo_b)]},
    )
    lifecycle = {"dispatcher_stop": [], "daemon_stop": [], "daemon_start": []}
    monkeypatch.setattr(
        core,
        "_callback_bridge_module",
        lambda: type("Bridge", (), {
            "stop_dispatcher": lambda _self, root: lifecycle["dispatcher_stop"].append(root),
        })(),
    )
    monkeypatch.setattr(
        core,
        "_source_graph_daemon_module",
        lambda: type("Daemon", (), {
            "stop_daemon": lambda _self, root: lifecycle["daemon_stop"].append(root),
            "ensure_started": lambda _self, root: lifecycle["daemon_start"].append(root) or {"ok": True},
        })(),
    )
    monkeypatch.setattr(core, "dispatcher_ensure_started", lambda: {"ok": True, "status": "manager_inbox"})

    to_b = core.repository_switch(repo_b)
    back_to_a = core.repository_switch(repo_a)

    assert to_b["ok"] is True and to_b["repo_id"] == repo_b
    assert back_to_a["ok"] is True and back_to_a["repo_id"] == repo_a
    assert core.repo_root() == root_a.resolve()
    assert lifecycle["dispatcher_stop"] == [root_a.resolve(), root_b.resolve()]
    assert lifecycle["daemon_stop"] == [root_a.resolve(), root_b.resolve()]
    assert lifecycle["daemon_start"] == [root_b.resolve(), root_a.resolve()]


@pytest.fixture
def distinct_window_manager_routes(tmp_path, monkeypatch):
    roots = [tmp_path / name for name in ("repo_a", "repo_b", "repo_c")]
    repo_ids = []
    registry = tmp_path / "router" / "repos"
    registry.mkdir(parents=True)
    thread_id = "019f5097-6dbe-7172-870a-945afc5f3bfa"
    for root, window in zip(roots, ("window_owner", "window_b", "window_c")):
        root.mkdir()
        assert task_store.initialize_repository(root)["ok"]
        repo_id = task_store.storage_readiness(root).repo_id
        repo_ids.append(repo_id)
        (registry / f"{repo_id}.json").write_text(json.dumps({
            "schema_id": shared_router.SCHEMA_ID,
            "repo_id": repo_id,
            "repo_root": str(root),
            "window_id": window,
            "extension_host_pid": core.os.getpid(),
            "selected_provider": "codex",
            "targets": {"codex": {
                "capability_state": "ready",
                "route": {
                    "repo_id": repo_id,
                    "window_id": window,
                    "thread_id": thread_id if root == roots[0] else "",
                },
            }},
        }), encoding="utf-8")
    monkeypatch.setattr(shared_router, "registry_dir", lambda home=None: registry)
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT", raising=False)
    monkeypatch.delenv("AIWORKHUB_WINDOW_ID", raising=False)
    monkeypatch.setenv("AIWORKHUB_REPO", str(roots[0]))
    monkeypatch.setattr(core, "_PROCESS_REPO_ROOT_OVERRIDE", None)
    monkeypatch.setattr(core, "_implicit_codex_repository_root", lambda: None)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    # Keep identity recovery real; bypass only platform-specific identity inputs.
    monkeypatch.setattr(
        core, "_codex_manager_identity", core._codex_shared_repo_route_manager_identity,
    )
    lifecycle = {"dispatcher_stop": [], "daemon_stop": [], "daemon_start": [],
                 "dispatcher_start": []}
    monkeypatch.setattr(core, "_callback_bridge_module", lambda: SimpleNamespace(
        stop_dispatcher=lambda root: lifecycle["dispatcher_stop"].append(root),
    ))
    monkeypatch.setattr(core, "_source_graph_daemon_module", lambda: SimpleNamespace(
        stop_daemon=lambda root: lifecycle["daemon_stop"].append(root),
        ensure_started=lambda root: lifecycle["daemon_start"].append(root) or {"ok": True},
    ))
    monkeypatch.setattr(core, "dispatcher_ensure_started", lambda:
        lifecycle["dispatcher_start"].append(core.repo_root())
        or {"ok": True, "status": "manager_inbox"})
    return roots, repo_ids, thread_id, lifecycle


@pytest.mark.parametrize("destinations, epochs", [((1, 0), (1, 2)), ((1, 2, 0), (1, 2, 3))])
def test_repository_switch_distinct_windows_preserves_original_owner(
    distinct_window_manager_routes, destinations, epochs,
):
    # Returning the destination window instead of the ledger owner breaks the
    # second real transfer's CAS, even when its repository and epoch are right.
    roots, repo_ids, thread_id, lifecycle = distinct_window_manager_routes
    previous = 0
    for destination, epoch in zip(destinations, epochs):
        result = core.repository_switch(repo_ids[destination])
        assert result["ok"] is True, result
        assert result["repo_id"] == repo_ids[destination]
        assert result["binding_source"] == "manager_switch"
        assert core.repo_root() == roots[destination].resolve()
        assert result["route_transfer"]["epoch"] == epoch
        assert result["route_transfer"]["previous_repo_id"] == repo_ids[previous]
        assert result["route_transfer"]["window_id"] == "window_owner"
        assert result["route_transfer"]["thread_id"] == thread_id
        identity = core._codex_shared_repo_route_manager_identity()
        assert identity["window_id"] == "window_owner"
        assert identity["thread_id"] == identity["session_id"] == thread_id
        previous = destination
    ownership = shared_router._read_ownership(shared_router._ownership_path())
    owner = ownership["routes"][f"codex:{thread_id}"]
    assert owner["window_id"] == "window_owner"
    assert owner["thread_id"] == thread_id
    assert owner["repo_id"] == repo_ids[0]
    assert owner["epoch"] == epochs[-1]
    assert ownership["revision"] == epochs[-1]
    stopped = [roots[index].resolve() for index in (0, *destinations[:-1])]
    started = [roots[index].resolve() for index in destinations]
    assert lifecycle["dispatcher_stop"] == stopped
    assert lifecycle["daemon_stop"] == stopped
    assert lifecycle["daemon_start"] == started
    assert lifecycle["dispatcher_start"] == started


@pytest.mark.parametrize("incoherent", [
    "missing_projection", "foreign_provider", "foreign_thread", "foreign_repo",
    "foreign_window", "missing_owner", "stale_epoch", "boolean_epoch", "zero_epoch",
    "route_repo_mismatch",
])
def test_shared_route_identity_requires_coherent_owner_projection(
    distinct_window_manager_routes, monkeypatch, incoherent,
):
    # A bare owner hint, or a projection for another identity/epoch, must never
    # replace the live repository record's window authority.
    roots, repo_ids, thread_id, _ = distinct_window_manager_routes
    transfer = shared_router.transfer_manager_route(
        provider="codex", thread_id=thread_id, window_id="window_owner",
        source_repo_id=repo_ids[0], target_repo_id=repo_ids[1],
        repositories=shared_router.list_known_repositories()["repositories"],
    )
    assert transfer["ok"] is True
    monkeypatch.setattr(core, "_PROCESS_REPO_ROOT_OVERRIDE", roots[1].resolve())
    real_list = shared_router.list_known_repositories

    def incoherent_registry(**kwargs):
        result = real_list(**kwargs)
        record = next(row for row in result["repositories"] if row["repo_id"] == repo_ids[1])
        projection = record["manager_route_ownership"]
        route = record["targets"]["codex"]["route"]
        if incoherent == "missing_projection":
            del record["manager_route_ownership"]
        elif incoherent == "foreign_provider":
            projection["provider"] = "claude"
        elif incoherent == "foreign_thread":
            projection["thread_id"] = "019f5097-6dbe-7172-870a-945afc5f3bfb"
        elif incoherent == "foreign_repo":
            projection["repo_id"] = repo_ids[2]
        elif incoherent == "foreign_window":
            projection["window_id"] = "window_foreign"
        elif incoherent == "missing_owner":
            del route["owner_window_id"]
        elif incoherent == "stale_epoch":
            route["ownership_epoch"] = 2
        elif incoherent == "boolean_epoch":
            projection["epoch"] = True
        elif incoherent == "zero_epoch":
            projection["epoch"] = route["ownership_epoch"] = 0
        elif incoherent == "route_repo_mismatch":
            route["repo_id"] = repo_ids[2]
        return result

    monkeypatch.setattr(shared_router, "list_known_repositories", incoherent_registry)
    identity = core._codex_shared_repo_route_manager_identity()
    assert identity["window_id"] == "window_b"
    assert identity["thread_id"] == thread_id


def test_repository_switch_failed_target_stops_target_and_restores_old_services(tmp_path, monkeypatch):
    old = tmp_path / "old"
    target = tmp_path / "target"
    old.mkdir()
    target.mkdir()
    assert task_store.initialize_repository(old)["ok"]
    assert task_store.initialize_repository(target)["ok"]
    old_id = task_store.storage_readiness(old).repo_id
    target_id = task_store.storage_readiness(target).repo_id
    thread_id = "019f5097-6dbe-7172-870a-945afc5f3bfa"
    identity = {
        "provider": "codex", "session_id": thread_id, "thread_id": thread_id,
        "window_id": "window_rollback",
    }
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT", raising=False)
    monkeypatch.setenv("AIWORKHUB_REPO", str(old))
    monkeypatch.setattr(core, "_PROCESS_REPO_ROOT_OVERRIDE", None)
    monkeypatch.setattr(core, "_implicit_codex_repository_root", lambda: None)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: identity)
    monkeypatch.setattr(
        shared_router, "registry_dir", lambda home=None: tmp_path / "router" / "repos"
    )
    monkeypatch.setattr(shared_router, "list_known_repositories", lambda **kwargs: {
        "ok": True,
        "repositories": [
            {
                "repo_id": old_id,
                "repo_root": str(old),
                "window_id": "window_rollback",
                "extension_host_alive": True,
                "stale": False,
                "targets": {"codex": {"route": {"repo_id": old_id, "thread_id": thread_id}}},
            },
            {
                "repo_id": target_id,
                "repo_root": str(target),
                "window_id": "window_target",
                "extension_host_alive": True,
                "stale": False,
                "targets": {"codex": {"route": {"repo_id": target_id, "thread_id": ""}}},
            },
        ],
    })
    lifecycle = {"dispatcher_stop": [], "daemon_stop": [], "daemon_start": []}
    monkeypatch.setattr(
        core,
        "_callback_bridge_module",
        lambda: type("Bridge", (), {
            "stop_dispatcher": lambda _self, root: lifecycle["dispatcher_stop"].append(root),
        })(),
    )

    def ensure_daemon(_self, root: Path) -> dict:
        lifecycle["daemon_start"].append(root)
        return {"ok": root == old.resolve(), "error": "target_index_failed"}

    monkeypatch.setattr(
        core,
        "_source_graph_daemon_module",
        lambda: type("Daemon", (), {
            "stop_daemon": lambda _self, root: lifecycle["daemon_stop"].append(root),
            "ensure_started": ensure_daemon,
        })(),
    )
    monkeypatch.setattr(core, "dispatcher_ensure_started", lambda: {"ok": True, "status": "manager_inbox"})

    result = core.repository_switch(target_id)

    assert result["ok"] is False
    assert "target_index_failed" in result["error"]
    assert result["route_rollback"]["repo_id"] == old_id
    assert result["route_rollback"]["epoch"] == 2
    assert core.repo_root() == old.resolve()
    assert lifecycle["dispatcher_stop"] == [old.resolve(), target.resolve()]
    assert lifecycle["daemon_stop"] == [old.resolve(), target.resolve()]
    assert lifecycle["daemon_start"] == [target.resolve(), old.resolve()]


def test_repository_switch_rejects_foreign_thread_without_mutating_binding(tmp_path, monkeypatch):
    old = tmp_path / "old"
    target = tmp_path / "target"
    old.mkdir()
    target.mkdir()
    assert task_store.initialize_repository(old)["ok"]
    assert task_store.initialize_repository(target)["ok"]
    target_id = task_store.storage_readiness(target).repo_id
    thread_id = "019f5097-6dbe-7172-870a-945afc5f3bfa"
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT", raising=False)
    monkeypatch.setenv("AIWORKHUB_REPO", str(old))
    monkeypatch.setattr(core, "_PROCESS_REPO_ROOT_OVERRIDE", None)
    monkeypatch.setattr(core, "_implicit_codex_repository_root", lambda: None)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: {
        "provider": "codex", "session_id": thread_id, "thread_id": thread_id,
        "window_id": "window_owner",
    })
    monkeypatch.setattr(
        shared_router, "registry_dir", lambda home=None: tmp_path / "router" / "repos"
    )
    monkeypatch.setattr(shared_router, "list_known_repositories", lambda **kwargs: {
        "ok": True, "repositories": [{
            "repo_id": target_id, "repo_root": str(target), "window_id": "window_foreign",
            "extension_host_alive": True, "stale": False,
            "targets": {"codex": {"route": {"repo_id": target_id, "thread_id": thread_id}}},
        }],
    })

    result = core.repository_switch(target_id)

    assert result["ok"] is False
    assert result["error"] == "route_transfer_source_not_owned"
    assert core.repo_root() == old.resolve()


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


def test_manager_bootstrap_describes_current_repo_manager_callback_ownership(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        core,
        "_codex_manager_identity",
        lambda: {
            "provider": "codex",
            "session_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
            "thread_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
        },
    )
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    # Schema v2 delivers the contract prose to a verified session once, so this
    # test states the empty-delivery-map precondition it has always relied on
    # rather than inheriting whatever ran before it in the same process.
    monkeypatch.setattr(core, "_CONTRACT_DELIVERIES", {})

    contract = core.manager_bootstrap()

    assert contract["contract_delivered"] is True
    assert "current verified Codex manager" in contract["callback"]["codex"]
    assert "audit provenance" in contract["callback"]["codex"]
    assert "optional explicit claim" in contract["operating_contract"]["task_state_machine"]["claim"]
    assert "always required" in contract["operating_contract"]["task_state_machine"]["launch"]
    authority = " ".join(contract["operating_contract"]["authority"])
    assert "override host cwd, workspace_roots, environment_context" in authority
    assert "never inspect the hinted repository as fallback" in authority
    assert not any("auto_pickup or" in step for step in contract["workflow"])


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
        required_outputs=["research/context_default.json"],
        validation=["python3 -m json.tool research/context_default.json"],
        risk_tier="high",
        custom_template_escape="audited_custom_unclassified",
    )
    assert result["ok"] is True, result
    card = json.loads(result["stdout"])
    context = card["project_context"]
    assert context["required"] is True
    assert context["task_type"] == "code"
    assert context["source_graph"]["required"] is True
    assert context["source_graph"]["query"].startswith(
        "research/context_default.json"
    )
    assert context["source_graph"]["query"] != "task"
    assert context["session"]["topic"] == "Strict task context"
    assert context["ai_memory"]["query"]
    assert context["kb"]["query"]
    assert card["risk_tier"] == "high"
    stored = task_store.get_task(root, "TASK_CONTEXT_DEFAULT")
    assert stored is not None
    assert stored["project_context"] == context
    assert stored["risk_tier"] == "high"


def test_task_create_rejects_invalid_explicit_risk_tier(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: {
        "provider": "codex",
        "session_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
        "thread_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
    })
    monkeypatch.setattr(
        core, "_verify_coordinator_capability", lambda runner: (True, "ok")
    )

    result = core.create_task(
        task_id="TASK_INVALID_RISK",
        title="Invalid risk",
        runner="claude_invalid_risk",
        topic="task_mcp",
        objective="Reject invented risk categories.",
        acceptance=["Rejected."],
        allowed_writes=[],
        read_only=True,
        risk_tier="probably-safe",
    )

    assert result["ok"] is False
    assert result["stderr"] == "invalid_risk_tier"
    assert result["allowed_risk_tiers"] == ["low", "medium", "high", "critical"]
    assert result["received_risk_tier"] == "probably-safe"


def _difficulty_repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: {
        "provider": "codex",
        "session_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
        "thread_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
    })
    monkeypatch.setattr(
        core, "_verify_coordinator_capability", lambda runner: (True, "ok")
    )
    return root


def _create_difficulty_task(task_id, **overrides):
    kwargs = dict(
        task_id=task_id,
        title="Difficulty card",
        runner="claude_difficulty",
        topic="task_mcp",
        objective="Prove the card records a declared difficulty.",
        acceptance=["Difficulty is persisted."],
        allowed_writes=["research/difficulty.json"],
        required_outputs=["research/difficulty.json"],
        validation=["python3 -m json.tool research/difficulty.json"],
        custom_template_escape="audited_custom_unclassified",
    )
    kwargs.update(overrides)
    return core.create_task(**kwargs)


def test_task_create_persists_declared_difficulty_and_reads_it_back(
    tmp_path, monkeypatch
):
    root = _difficulty_repo(tmp_path, monkeypatch)

    result = _create_difficulty_task("TASK_DIFFICULTY_BOUNDED", difficulty="bounded")

    assert result["ok"] is True, result
    card = json.loads(result["stdout"])
    assert card["difficulty"] == "bounded"
    assert card["difficulty_origin"] == "declared"
    stored = task_store.get_task(root, "TASK_DIFFICULTY_BOUNDED")
    assert stored is not None
    assert stored["difficulty"] == "bounded"
    assert stored["difficulty_origin"] == "declared"

    retry = _create_difficulty_task("TASK_DIFFICULTY_BOUNDED", difficulty="bounded")
    assert retry["ok"] is True, retry
    assert retry["reconciled"] is True
    assert retry["receipt_state"] == "existing_identical"

    for other in ("complex", None):
        conflict = _create_difficulty_task("TASK_DIFFICULTY_BOUNDED", difficulty=other)
        assert conflict["ok"] is False
        assert conflict["stderr"] == "task_already_exists:TASK_DIFFICULTY_BOUNDED"
        assert conflict["conflict_fields"] == ["difficulty"]


def test_task_create_without_difficulty_stores_none_and_undeclared(
    tmp_path, monkeypatch
):
    root = _difficulty_repo(tmp_path, monkeypatch)

    result = _create_difficulty_task("TASK_DIFFICULTY_UNDECLARED")

    assert result["ok"] is True, result
    card = json.loads(result["stdout"])
    assert "difficulty" in card
    assert card["difficulty"] is None
    assert card["difficulty_origin"] == "undeclared"
    stored = task_store.get_task(root, "TASK_DIFFICULTY_UNDECLARED")
    assert stored is not None
    assert stored.get("difficulty") is None
    assert stored["difficulty_origin"] == "undeclared"

    retry = _create_difficulty_task("TASK_DIFFICULTY_UNDECLARED")
    assert retry["ok"] is True, retry
    assert retry["reconciled"] is True
    assert retry["receipt_state"] == "existing_identical"


def test_task_create_normalizes_declared_difficulty_case_and_space(
    tmp_path, monkeypatch
):
    _difficulty_repo(tmp_path, monkeypatch)

    result = _create_difficulty_task(
        "TASK_DIFFICULTY_NORMALIZED", difficulty=" Standard "
    )

    assert result["ok"] is True, result
    card = json.loads(result["stdout"])
    assert card["difficulty"] == "standard"
    assert card["difficulty_origin"] == "declared"


def test_task_create_rejects_invalid_difficulty(tmp_path, monkeypatch):
    root = _difficulty_repo(tmp_path, monkeypatch)

    result = _create_difficulty_task(
        "TASK_DIFFICULTY_INVALID",
        difficulty="easy",
        allowed_writes=[],
        required_outputs=[],
        validation=[],
        read_only=True,
    )

    assert result["ok"] is False
    assert result["stderr"] == "invalid_difficulty"
    assert result["allowed_difficulties"] == ["bounded", "standard", "complex"]
    assert result["received_difficulty"] == "easy"
    assert task_store.get_task(root, "TASK_DIFFICULTY_INVALID") is None


def test_task_create_from_template_forwards_declared_difficulty(monkeypatch):
    captured = {}

    def fake_create_task(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(server.core, "create_task", fake_create_task)
    common = dict(
        title="Fix the leak",
        runner="codex_worker",
        topic="coding",
        objective="Close the leak and add a regression.",
        acceptance=["Leak is gone."],
        template_id="bugfix_with_regression",
        production_paths=["src/aiworkhub/a.py"],
        test_paths=["tests/test_a.py"],
    )

    declared = server.aiworkhub_task_create_from_template(
        task_id="TASK_TEMPLATE_DIFFICULTY", difficulty="bounded", **common
    )
    assert declared["ok"] is True
    assert captured["difficulty"] == "bounded"

    server.aiworkhub_task_create_from_template(
        task_id="TASK_TEMPLATE_NO_DIFFICULTY", **common
    )
    assert captured["difficulty"] is None


def test_task_create_forwards_declared_difficulty(monkeypatch):
    captured = {}

    def fake_create_task(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(server.core, "create_task", fake_create_task)
    common = dict(
        title="Fix the leak",
        runner="codex_worker",
        topic="coding",
        objective="Close the leak and add a regression.",
        acceptance=["Leak is gone."],
        allowed_writes=["src/aiworkhub/a.py", "tests/test_a.py"],
        validation=[".venv/Scripts/python.exe -m pytest -q tests/test_a.py"],
    )

    declared = server.aiworkhub_task_create(
        task_id="TASK_CREATE_DIFFICULTY", difficulty="bounded", **common
    )
    assert declared["ok"] is True
    assert captured["difficulty"] == "bounded"

    server.aiworkhub_task_create(task_id="TASK_CREATE_NO_DIFFICULTY", **common)
    assert captured["difficulty"] is None


def test_task_create_receipt_reads_back_declared_difficulty(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root.resolve()))
    monkeypatch.setenv("AIWORKHUB_REPO", str(root.resolve()))
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        core, "_verify_coordinator_capability", lambda *args, **kwargs: (True, "")
    )
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {
            "provider": "claude",
            "session_id": "01234567-89ab-4def-8123-456789abcdef",
            "route_state": "verified",
        },
    )
    monkeypatch.setattr(core, "_PROCESS_REPO_ROOT_OVERRIDE", root.resolve())
    common = dict(
        title="Difficulty receipt",
        runner="codex_worker_nf390",
        topic="task_mcp",
        objective="Read the declared difficulty back through the receipt.",
        acceptance=["Receipt names difficulty."],
        template_id="bugfix_with_regression",
        production_paths=["src/a.py"],
        test_paths=["tests/test_a.py"],
    )

    declared = server.aiworkhub_task_create_from_template(
        task_id="TASK_RECEIPT_DECLARED", difficulty="bounded", **common
    )
    assert declared["ok"] is True, declared
    assert declared["difficulty"] == "bounded"
    assert declared["difficulty_origin"] == "declared"

    undeclared = server.aiworkhub_task_create_from_template(
        task_id="TASK_RECEIPT_UNDECLARED", **common
    )
    assert undeclared["ok"] is True, undeclared
    assert undeclared["difficulty"] is None
    assert undeclared["difficulty_origin"] == "undeclared"


def test_task_create_rejects_invalid_priority_with_supported_values(
    tmp_path, monkeypatch
):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: {
        "provider": "codex",
        "session_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
        "thread_id": "019f5097-6dbe-7172-870a-945afc5f3bfa",
    })
    monkeypatch.setattr(
        core, "_verify_coordinator_capability", lambda runner: (True, "ok")
    )

    result = core.create_task(
        task_id="TASK_INVALID_PRIORITY",
        title="Invalid priority",
        runner="codex_gpt-5.6-sol",
        topic="task_mcp",
        objective="Reject invented priority categories with a complete hint.",
        acceptance=["Rejected."],
        allowed_writes=[],
        read_only=True,
        priority="p0",
    )

    assert result["ok"] is False
    assert result["stderr"] == "invalid_priority"
    assert result["allowed_priorities"] == ["low", "normal", "high", "critical"]
    assert result["received_priority"] == "p0"


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


def test_repo_root_prefers_exact_live_codex_route_over_stale_cwd(tmp_path, monkeypatch):
    routed = tmp_path / "routed"
    routed.mkdir()
    monkeypatch.setattr(core, "_implicit_windows_codex_repository_root", lambda: None)
    monkeypatch.delenv("AIWORKHUB_REPO_ROOT", raising=False)
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    monkeypatch.setenv("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "codex_vscode")
    monkeypatch.setenv("CODEX_THREAD_ID", "019f5097-6dbe-7172-870a-945afc5f3bfa")
    monkeypatch.setenv("VSCODE_AGENT_FOLDER", "/tmp/vscode-agent")
    monkeypatch.setattr(
        shared_router,
        "resolve_repository_route",
        lambda **kwargs: {"ok": True, "repo_root": str(routed), "repo_id": "repo_" + "a" * 32},
    )

    assert core.repo_root() == routed.resolve()


def test_windows_repo_root_prefers_exact_owning_window_without_thread_env(tmp_path, monkeypatch):
    routed = tmp_path / "routed-windows"
    routed.mkdir()
    repo_id = "repo_" + "a" * 32
    record = {
        "repo_id": repo_id,
        "repo_root": str(routed),
        "window_id": "window_windows",
        "extension_host_pid": 12345,
        "extension_host_alive": True,
        "stale": False,
        "selected_provider": "codex",
        "targets": {
            "codex": {
                "capability_state": "available",
                "route": {
                    "repo_id": repo_id,
                    "window_id": "window_windows",
                    "thread_id": "",
                    "session_id": "episode_windows",
                },
            },
        },
    }
    monkeypatch.setattr(
        shared_router,
        "list_known_repositories",
        lambda *, limit: {"ok": True, "repositories": [record]},
    )
    monkeypatch.setattr(
        core,
        "_pid_in_same_windows_user_ancestor_chain",
        lambda pid, *, max_depth, **_snapshot: pid == 12345 and max_depth == 16,
    )

    assert core._implicit_windows_codex_repository_root() == routed.resolve()


def test_windows_repo_root_route_fails_closed_when_two_owning_windows_match(tmp_path, monkeypatch):
    records = []
    for index in range(2):
        root = tmp_path / f"routed-{index}"
        root.mkdir()
        repo_id = "repo_" + str(index + 1) * 32
        window_id = f"window_{index}"
        records.append({
            "repo_id": repo_id,
            "repo_root": str(root),
            "window_id": window_id,
            "extension_host_pid": 12345 + index,
            "extension_host_alive": True,
            "stale": False,
            "selected_provider": "codex",
            "targets": {
                "codex": {
                    "capability_state": "available",
                    "route": {"repo_id": repo_id, "window_id": window_id},
                },
            },
        })
    monkeypatch.setattr(
        shared_router,
        "list_known_repositories",
        lambda *, limit: {"ok": True, "repositories": records},
    )
    monkeypatch.setattr(
        core,
        "_pid_in_same_windows_user_ancestor_chain",
        lambda pid, *, max_depth, **_snapshot: True,
    )

    assert core._implicit_windows_codex_repository_root() is None


def test_implicit_codex_repo_uses_windows_window_route_before_thread_env(tmp_path, monkeypatch):
    routed = (tmp_path / "routed-windows").resolve()
    monkeypatch.setattr(core, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(core, "_implicit_windows_codex_repository_root", lambda: routed)

    assert core._implicit_codex_repository_root() == routed


def test_repo_root_explicit_binding_wins_over_dynamic_chat_route(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    routed = tmp_path / "routed"
    explicit.mkdir()
    routed.mkdir()
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(explicit))
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    monkeypatch.setattr(
        shared_router,
        "resolve_repository_route",
        lambda **kwargs: {"ok": True, "repo_root": str(routed)},
    )

    assert core.repo_root() == explicit.resolve()


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
        read_only=True,
        callback_required=True,
    )

    assert result["ok"] is False
    assert result["stderr"] == "callback_route_pending:codex_thread_id_not_observed"


_SEAT_SESSION = "mls-" + "ab" * 16


def _hours_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _hours_ahead(hours):
    return _hours_ago(-hours)


def _select_manager_chat_session(root, session_id=_SEAT_SESSION, **fields):
    """Select one Manager Chat session record: active, bound and just created unless ``fields`` differ."""

    state = root / ".aiworkhub" / "runtime" / "manager_loop"
    (state / "sessions").mkdir(parents=True, exist_ok=True)
    (state / "selected.json").write_text(json.dumps({"session_id": session_id}), encoding="utf-8")
    record = {
        "session_id": session_id,
        "status": "active",
        "backend_id": "opencode_cli",
        "model": "xai/grok-4.7",
        "created_at": _hours_ago(0),
        **fields,
    }
    (state / "sessions" / f"{session_id}.json").write_text(json.dumps(record), encoding="utf-8")
    return state


def test_task_create_uses_active_manager_chat_session_as_callback_origin(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    session_id = "mls-" + "ab" * 16
    _select_manager_chat_session(root, session_id)
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
        task_id="TASK_MANAGER_CHAT_ORIGIN",
        title="Manager chat owns the callback",
        runner="manager_chat_canary",
        topic="task_mcp",
        objective="Create a callback-required card against the active manager chat session.",
        acceptance=["Card stores the manager session."],
        allowed_writes=[],
        read_only=True,
        callback_required=True,
    )

    assert result["ok"] is True
    card = json.loads(result["stdout"])
    assert card["origin_thread_id"] == session_id
    assert card["manager_chat_session_id"] == session_id
    assert card["callback_required"] is True
    assert card["callback_supported"] is True
    assert card["callback_supported"] is True


def test_task_create_keeps_codex_thread_and_manager_chat_session(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    session_id = "mls-" + "cd" * 16
    thread_id = "11111111-1111-4111-8111-111111111111"
    _select_manager_chat_session(root, session_id)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: {
        "provider": "codex",
        "session_id": "episode_pending",
        "thread_id": thread_id,
        "window_id": "window_extension_owned",
        "callback_supported": "true",
        "route_state": "ready",
    })
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda runner: (True, "ok"))

    result = core.create_task(
        task_id="TASK_BOTH_CALLBACK_ROUTES",
        title="Both callback seats",
        runner="manager_chat_canary",
        topic="task_mcp",
        objective="Keep the Codex thread and also stamp the manager chat session.",
        acceptance=["Card stores both destinations."],
        allowed_writes=[],
        read_only=True,
        callback_required=True,
    )

    assert result["ok"] is True
    card = json.loads(result["stdout"])
    assert card["origin_thread_id"] == thread_id
    assert card["manager_chat_session_id"] == session_id
    assert card["callback_supported"] is True
    assert card["callback_supported"] is True


def test_manager_bootstrap_reports_the_active_manager_chat_seat(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    session_id = "mls-" + "ab" * 16
    _select_manager_chat_session(root, session_id)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: {
        "provider": "codex",
        "session_id": "episode_pending",
        "thread_id": "",
        "window_id": "window_old",
        "route_state": "route_pending",
        "callback_supported": "false",
    })
    monkeypatch.setattr(core, "_CONTRACT_DELIVERIES", {})

    contract = core.manager_bootstrap()

    assert contract["provider"] == "manager_chat"
    assert contract["manager_verified"] is True
    route = contract["manager_route"]
    assert route["session_id"] == session_id
    assert route["route_state"] == "ready"
    assert route["callback_supported"] == "true"
    assert route["model"] == "xai/grok-4.7"
    assert route["backend_id"] == "opencode_cli"
    assert route["manager_chat"]["session_id"] == session_id
    assert contract["codex_route"]["session_id"] == "episode_pending"
    assert contract["reason"] == ""


_VERIFIED_CODEX_ROUTE = {
    "provider": "codex",
    "session_id": "episode_verified",
    "thread_id": "11111111-1111-4111-8111-111111111111",
    "window_id": "window_owner",
    "route_state": "ready",
    "callback_supported": "true",
}


def _bootstrap_as_verified_codex(monkeypatch, root):
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: dict(_VERIFIED_CODEX_ROUTE))
    monkeypatch.setattr(core, "_CONTRACT_DELIVERIES", {})
    return core.manager_bootstrap()


def test_bootstrap_keeps_the_verified_route_when_the_selected_session_is_passive(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _select_manager_chat_session(root, backend_id="", model="")

    contract = _bootstrap_as_verified_codex(monkeypatch, root)

    assert contract["provider"] == "codex"
    assert contract["manager_verified"] is True
    assert contract["manager_route"] == _VERIFIED_CODEX_ROUTE
    assert "codex_route" not in contract


def test_bootstrap_keeps_the_verified_route_when_the_selected_session_is_idle_past_the_lease(
    tmp_path, monkeypatch
):
    root = tmp_path / "repo"
    root.mkdir()
    _select_manager_chat_session(root, created_at=_hours_ago(13))

    contract = _bootstrap_as_verified_codex(monkeypatch, root)

    assert contract["provider"] == "codex"
    assert contract["manager_verified"] is True
    assert contract["manager_route"] == _VERIFIED_CODEX_ROUTE
    assert "codex_route" not in contract


def test_bootstrap_keeps_the_verified_route_when_the_selected_session_is_stamped_from_the_future(
    tmp_path, monkeypatch
):
    root = tmp_path / "repo"
    root.mkdir()
    _select_manager_chat_session(root, created_at=_hours_ahead(1))

    contract = _bootstrap_as_verified_codex(monkeypatch, root)

    assert contract["provider"] == "codex"
    assert contract["manager_verified"] is True
    assert contract["manager_route"] == _VERIFIED_CODEX_ROUTE
    assert "codex_route" not in contract


def _seen_seat(monkeypatch, root):
    """What core and callback_store each report as the seat for the selected session."""

    monkeypatch.setattr(core, "repo_root", lambda: root)
    conn = callback_store.open_db(root / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    try:
        callback_store.init_db(conn)
        return (
            core._active_manager_chat_record(),
            core._manager_chat_session_origin(),
            callback_store._active_manager_chat_session_from_conn(conn),
            callback_store._callback_manager_chat_session(conn, "TASK_WITHOUT_A_CARD"),
        )
    finally:
        conn.close()


def test_a_fresh_bound_session_is_the_manager_chat_seat_in_core_and_callback_store(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _select_manager_chat_session(root)

    record, origin, session, callback_origin = _seen_seat(monkeypatch, root)

    assert record == {
        "session_id": _SEAT_SESSION,
        "backend_id": "opencode_cli",
        "model": "xai/grok-4.7",
        "status": "active",
    }
    assert origin == session == callback_origin == _SEAT_SESSION


def test_an_active_session_idle_past_the_lease_is_not_the_manager_chat_seat(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _select_manager_chat_session(root, created_at=_hours_ago(13))

    assert _seen_seat(monkeypatch, root) == (None, "", "", "")


def test_a_session_stamped_from_the_future_is_not_the_manager_chat_seat(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _select_manager_chat_session(root, created_at=_hours_ahead(1))

    assert _seen_seat(monkeypatch, root) == (None, "", "", "")

    _select_manager_chat_session(root, created_at=_hours_ahead(1 / 60))

    record, origin, session, callback_origin = _seen_seat(monkeypatch, root)

    assert record is not None
    assert origin == session == callback_origin == _SEAT_SESSION


def test_a_passive_session_is_not_the_manager_chat_seat(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _select_manager_chat_session(root, backend_id="", model="")

    assert _seen_seat(monkeypatch, root) == (None, "", "", "")


def test_a_session_created_long_ago_but_still_in_use_keeps_the_manager_chat_seat(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    state = _select_manager_chat_session(root, created_at=_hours_ago(72))
    log = state / "events" / f"{_SEAT_SESSION}.jsonl"
    log.parent.mkdir()
    log.write_text(
        json.dumps({"at": _hours_ago(1), "seq": 1, "turn": 1, "type": "user_message", "payload": {}})
        + "\n",
        encoding="utf-8",
    )

    record, origin, session, callback_origin = _seen_seat(monkeypatch, root)

    assert record is not None
    assert origin == session == callback_origin == _SEAT_SESSION


def test_core_and_callback_store_both_defer_to_the_one_shared_seat_predicate(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _select_manager_chat_session(root)
    monkeypatch.setattr(callback_store, "manager_chat_record_holds_seat", lambda record, **_kw: False)

    assert _seen_seat(monkeypatch, root) == (None, "", "", "")

    _select_manager_chat_session(root, backend_id="", model="", created_at=_hours_ago(99))
    monkeypatch.setattr(callback_store, "manager_chat_record_holds_seat", lambda record, **_kw: True)

    record, origin, session, callback_origin = _seen_seat(monkeypatch, root)

    assert record is not None
    assert origin == session == callback_origin == _SEAT_SESSION


def test_manager_chat_seat_needs_an_active_bound_session_used_within_the_lease():
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    lease = callback_store.MANAGER_CHAT_SEAT_LEASE
    assert lease == timedelta(hours=12)

    def holds(**fields):
        record = {
            "session_id": _SEAT_SESSION,
            "status": "active",
            "backend_id": "opencode_cli",
            "model": "xai/grok-4.7",
            "created_at": (now - timedelta(minutes=5)).isoformat(),
            **fields,
        }
        return callback_store.manager_chat_record_holds_seat(record, now=now)

    assert holds()
    assert holds(created_at="2026-09-28T11:55:00Z")
    assert holds(created_at="2026-09-28T11:55:00")
    assert holds(created_at=(now - lease).isoformat())
    assert not holds(created_at=(now - lease - timedelta(seconds=1)).isoformat())
    assert not holds(status="closed")
    assert not holds(backend_id="")
    assert not holds(model="  ")
    assert not holds(backend_id="", model="")
    assert not holds(created_at="")
    assert not holds(created_at=None)
    assert not holds(created_at="not a timestamp")
    assert not callback_store.manager_chat_record_holds_seat(None, now=now)

    old = (now - timedelta(days=3)).isoformat()
    recent = (now - timedelta(hours=1)).isoformat()
    assert not holds(created_at=old)
    assert holds(created_at=old, updated_at=recent)
    assert holds(created_at=old, last_turn_at=recent)


def test_manager_chat_seat_lease_counts_the_last_logged_event(tmp_path):
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    state = tmp_path / "manager_loop"
    log = state / "events" / f"{_SEAT_SESSION}.jsonl"
    log.parent.mkdir(parents=True)
    record = {
        "session_id": _SEAT_SESSION,
        "status": "active",
        "backend_id": "opencode_cli",
        "model": "xai/grok-4.7",
        "created_at": (now - timedelta(days=3)).isoformat(),
    }

    def holds(session_record=record):
        return callback_store.manager_chat_record_holds_seat(session_record, state_dir=state, now=now)

    def event(seq, when):
        return json.dumps(
            {"at": when.isoformat(), "seq": seq, "turn": seq, "type": "user_message", "payload": {}}
        )

    long_ago = event(1, now - timedelta(days=3))
    an_hour_ago = event(2, now - timedelta(hours=1))
    assert not holds()  # no log yet: only the three-day-old creation counts
    log.write_text(long_ago + "\n", encoding="utf-8")
    assert not holds()
    log.write_text(long_ago + "\n" + an_hour_ago + "\n", encoding="utf-8")
    assert holds()
    # A torn last line is skipped, not trusted: the last whole event still counts.
    log.write_text(long_ago + "\n" + an_hour_ago + '\n{"at": "2026-09-28T11', encoding="utf-8")
    assert holds()
    log.write_text(long_ago + '\n{"at": "not a timestamp"}\n', encoding="utf-8")
    assert not holds()
    # Only a Manager Chat session's own log is read, never a path a record names.
    (state / "escape.jsonl").write_text(event(1, now) + "\n", encoding="utf-8")
    assert not holds({**record, "session_id": "../escape"})


def test_manager_chat_seat_ignores_a_stamp_from_the_future():
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    skew = callback_store.MANAGER_CHAT_SEAT_CLOCK_SKEW
    assert skew == timedelta(minutes=5)

    def at(delta):
        return (now + delta).isoformat()

    def holds(**fields):
        record = {
            "session_id": _SEAT_SESSION,
            "status": "active",
            "backend_id": "opencode_cli",
            "model": "xai/grok-4.7",
            **fields,
        }
        return callback_store.manager_chat_record_holds_seat(record, now=now)

    assert holds(created_at=at(timedelta(minutes=1)))
    assert holds(created_at=at(skew))
    assert not holds(created_at=at(skew + timedelta(seconds=1)))
    assert not holds(created_at=at(timedelta(hours=1)))
    assert not holds(created_at=at(timedelta(days=3650)))
    for field in ("last_turn_at", "updated_at"):
        assert not holds(created_at=at(-timedelta(days=3)), **{field: at(timedelta(hours=1))})
    # A wrong clock on one stamp does not throw away a real recent one.
    assert holds(created_at=at(-timedelta(minutes=5)), updated_at=at(timedelta(hours=1)))


def test_manager_chat_seat_lease_ignores_a_logged_event_from_the_future(tmp_path):
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    skew = callback_store.MANAGER_CHAT_SEAT_CLOCK_SKEW
    state = tmp_path / "manager_loop"
    log = state / "events" / f"{_SEAT_SESSION}.jsonl"
    log.parent.mkdir(parents=True)
    record = {
        "session_id": _SEAT_SESSION,
        "status": "active",
        "backend_id": "opencode_cli",
        "model": "xai/grok-4.7",
        "created_at": (now - timedelta(days=3)).isoformat(),
    }

    def event(seq, when):
        return json.dumps(
            {"at": when.isoformat(), "seq": seq, "turn": seq, "type": "user_message", "payload": {}}
        )

    def holds_after_logging(*offsets):
        lines = [event(seq, now + offset) for seq, offset in enumerate(offsets, 1)]
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return callback_store.manager_chat_record_holds_seat(record, state_dir=state, now=now)

    assert not holds_after_logging(timedelta(hours=1))
    assert not holds_after_logging(skew + timedelta(seconds=1))
    assert holds_after_logging(timedelta(minutes=1))
    assert holds_after_logging(skew)
    # Behind a wrong-clock line the real last event is the one that counts.
    assert holds_after_logging(-timedelta(hours=1), timedelta(hours=1))
    assert not holds_after_logging(-timedelta(days=3), timedelta(hours=1))


def test_manager_chat_event_tail_holds_the_longest_line_the_writer_can_log(tmp_path):
    from aiworkhub import manager_loop

    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    store = manager_loop.SessionStore(tmp_path / "manager_loop", "repo_event_tail")
    log = store.root / "events" / f"{_SEAT_SESSION}.jsonl"
    cap = manager_loop.MAX_EVENT_PAYLOAD_BYTES
    bound = manager_loop.FIELD_BOUNDS["text"]
    assert bound == max(manager_loop.FIELD_BOUNDS.values())
    # The longest lines the writer logs: the largest bounded field filled to its bound with
    # the characters json.dumps escapes to the most bytes per byte of UTF-8 (a control
    # character becomes 6, a 2-byte one 6, a 4-byte one 12), that field past its bound,
    # which is cut and marked with its quotes doubled, and unbounded fields past the
    # payload cap, which collapse to a preview.
    payloads = {
        "control": {"text": "\x01" * bound},
        "two_byte": {"text": chr(0xE9) * (bound // 2)},
        "four_byte": {"text": "\U0001f600" * (bound // 4)},
        "cut": {"text": '"' * (4 * bound)},
        "preview": {"note": '"' * (4 * cap)},
    }

    for name, payload in payloads.items():
        event = store.append_event(
            _SEAT_SESSION,
            {"at": now.isoformat(), "turn": 999, "type": "assistant_text", "payload": payload},
        )
        line = log.read_bytes().splitlines(keepends=True)[-1]

        assert ("truncated" in event["payload"]) == (name in ("cut", "preview")), name
        assert len(line) <= callback_store._MANAGER_CHAT_EVENT_TAIL_BYTES, name
        newest = callback_store._manager_chat_last_event_at(
            store.root, _SEAT_SESSION, not_after=now
        )
        assert newest == now, name


def test_manager_chat_seat_fails_closed_when_the_newest_event_line_outgrows_the_tail(tmp_path):
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    state = tmp_path / "manager_loop"
    log = state / "events" / f"{_SEAT_SESSION}.jsonl"
    log.parent.mkdir(parents=True)
    record = {
        "session_id": _SEAT_SESSION,
        "status": "active",
        "backend_id": "opencode_cli",
        "model": "xai/grok-4.7",
        "created_at": (now - timedelta(days=3)).isoformat(),
    }
    tail = callback_store._MANAGER_CHAT_EVENT_TAIL_BYTES
    stem = f'{{"at": "{(now - timedelta(minutes=1)).isoformat()}", "pad": "'

    def holds_with_a_newest_line_of(size):
        log.write_bytes((stem + "x" * (size - len(stem) - 3) + '"}\n').encode("ascii"))
        assert log.stat().st_size == size
        return callback_store.manager_chat_record_holds_seat(record, state_dir=state, now=now)

    assert holds_with_a_newest_line_of(tail)
    assert not holds_with_a_newest_line_of(tail + 1)


def test_a_stale_manager_chat_session_gets_no_mirrored_callback_copy(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    thread = "11111111-1111-4111-8111-111111111111"
    conn = callback_store.open_db(root / ".aiworkhub" / "tasking" / "task_queue.sqlite")
    try:
        callback_store.init_db(conn)
        now = callback_store.utc_now()
        for task_id in ("TASK_IDLE_SEAT", "TASK_LIVE_SEAT"):
            card = {"task_id": task_id, "origin_thread_id": thread, "coordinator_provider": "codex"}
            conn.execute(
                "INSERT INTO tasks(task_id, runner, topic, status, worker_status, card_json, created_at, updated_at, origin_thread_id) "
                "VALUES (?, 'codex', 'task_mcp', 'review', 'review', ?, ?, ?, ?)",
                (task_id, json.dumps(card), now, now, thread),
            )
        conn.commit()

        _select_manager_chat_session(root, created_at=_hours_ago(13))
        assert callback_store.enqueue_callback(
            conn, "TASK_IDLE_SEAT", thread, "review_ready", provider="codex", episode_id="1"
        ) is True
        _select_manager_chat_session(root)
        assert callback_store.enqueue_callback(
            conn, "TASK_LIVE_SEAT", thread, "review_ready", provider="codex", episode_id="1"
        ) is True

        rows = conn.execute(
            "SELECT task_id, provider, origin_thread_id FROM callback_outbox ORDER BY task_id, provider"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("TASK_IDLE_SEAT", "codex", thread),
            ("TASK_LIVE_SEAT", "codex", thread),
            ("TASK_LIVE_SEAT", "manager_chat", _SEAT_SESSION),
        ]
    finally:
        conn.close()


def test_task_create_polling_only_succeeds_while_route_is_pending(tmp_path, monkeypatch):
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
        read_only=True,
        callback_required=False,
    )

    assert result["ok"] is True
    card = json.loads(result["stdout"])
    assert card["origin_thread_id"] == ""
    assert card["callback_supported"] is False
    assert card["callback_required"] is False
    assert card["manager_route_state"] == "route_pending"
    assert task_store.get_task(root, "TASK_POLLING_ONLY")["origin_thread_id"] == ""


def test_task_create_invalid_type_enumerates_supported_values(tmp_path, monkeypatch):
    root = tmp_path / "repo_invalid_type"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: {
        "provider": "claude", "session_id": "session", "window_id": "window",
    })
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda runner: (True, "ok"))
    result = core.create_task(
        task_id="TASK_INVALID_TYPE",
        title="Invalid type",
        runner="claude_worker",
        topic="task_mcp",
        objective="Reject with an actionable schema hint.",
        acceptance=["Rejected."],
        allowed_writes=[],
        callback_required=False,
        task_type="coding",
    )
    assert result["ok"] is False
    assert result["stderr"] == "invalid_task_type"
    assert result["allowed_task_types"] == ["code", "data_classification", "research"]
    assert result["received_task_type"] == "coding"


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


def test_manager_source_graph_continuation_passthrough(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    (root / "pkg").mkdir()
    (root / "pkg" / "big.py").write_text(
        "def big_manager_target():\n"
        + "".join(f"    # padding line {i} for outer pagination\n" for i in range(50))
        + "    return 1\n",
        encoding="utf-8",
    )
    source_graph.build_index(root)
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(root))
    monkeypatch.setattr(
        manager_ai_tools.worker_tools, "_source_graph_output_cap", lambda mode: 2048,
    )

    first = manager_ai_tools.source_graph_query(
        mode="body", query="big_manager_target", budget=8,
    )
    assert first["ok"] is True
    assert first["surface"] == "manager_mcp"
    assert first["outer_truncated"] is True
    assert first["internal_truncated"] is False
    cursor = first["continuation_cursor"]
    assert cursor

    chunks = [base64.b64decode(first["content"])]
    while cursor:
        page = manager_ai_tools.source_graph_query(
            mode="body", query="big_manager_target", budget=8, continuation_cursor=cursor,
        )
        assert page["ok"] is True
        assert page["surface"] == "manager_mcp"
        assert page["content_encoding"] == "base64"
        chunks.append(base64.b64decode(page["content"]))
        cursor = page["continuation_cursor"]

    reassembled = b"".join(chunks)
    assert len(reassembled) == first["full_bytes"]
    assert hashlib.sha256(reassembled).hexdigest() == first["content_sha256"]
