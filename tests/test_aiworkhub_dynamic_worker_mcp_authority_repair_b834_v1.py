"""B834: authority repair for the B833 dynamic worker MCP candidate.

Covers exactly the independently identified production defects: the dual
repo/authority-repo binding, storage.json-driven legacy-read-only authority
resolution with fail-closed absent/empty-database handling, the
query-vs-target argv bug, the zero-hit/cache-hit live-call-gate bug,
cross-context cache isolation, Landlock/bubblewrap authority path mapping,
mandatory (fail-closed) worker MCP config injection, and the no-mutation /
data-task-exemption / all-three-adapter-injection invariants under the new
dual binding. Companion to test_aiworkhub_dynamic_worker_mcp_b833_v1.py,
which covers the rest of the recovered B833 surface.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher  # noqa: E402
from aiworkhub import repository_state  # noqa: E402
from aiworkhub import runtime_adapters  # noqa: E402
from aiworkhub import source_graph as source_graph_mod  # noqa: E402
from aiworkhub import worker_ai_tools_mcp as w  # noqa: E402
from aiworkhub import worker_workspace  # noqa: E402


def _write_tool(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _write_storage_registry(repo: Path, *, canonical_active: bool = False) -> None:
    registry = {
        "durable_root": ".aiworkhub",
        "databases": [
            {"component": "source_graph", "id": "source_graph", "legacy_source": "AITools/source_graph.db",
             "canonical_durable_path": "source_graph/source_graph.sqlite",
             "authority": {"canonical_active": canonical_active, "state": "shadow"}},
            {"component": "sessions", "id": "session", "legacy_source": "AITools/session.db",
             "canonical_durable_path": "sessions/sessions.sqlite",
             "authority": {"canonical_active": False, "state": "shadow"}},
            {"component": "sessions", "id": "transcript", "legacy_source": "AITools/transcript_graph.db",
             "canonical_durable_path": "sessions/transcript_graph.sqlite",
             "authority": {"canonical_active": False, "state": "shadow"}},
            {"component": "memory", "id": "memory", "legacy_source": "AITools/ai_memory/ai_memory.db",
             "canonical_durable_path": "memory/memory.sqlite",
             "authority": {"canonical_active": False, "state": "shadow"}},
            {"component": "kb", "id": "kb", "legacy_source": "AITools/kb.db",
             "canonical_durable_path": "kb/knowledge.sqlite",
             "authority": {"canonical_active": False, "state": "shadow"}},
        ],
    }
    path = repo / ".aiworkhub" / "config" / "storage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry), encoding="utf-8")


def _stub_source_graph_engine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    relevant_files: tuple[str, ...] = ("AITools/source_graph.py", "tools/geoai-task-mcp/src/aiworkhub/x.py"),
) -> list[tuple[str, str, int]]:
    """Stand in for aiworkhub.source_graph's real FTS query engine (its own
    correctness is covered by that module's own test suite) so these
    fixtures can assert query-vs-target/caching/audit semantics against a
    deterministic payload without building a real on-disk index. Returns
    the list of (mode, query, budget) calls actually made -- the direct,
    in-process replacement for the old subprocess argv-echo trick."""
    calls: list[tuple[str, str, int]] = []

    def _payload(mode: str, query: str, budget: int) -> dict:
        calls.append((mode, query, budget))
        return {"target": query, "relevant_files": list(relevant_files)}

    monkeypatch.setattr(source_graph_mod, "focus", lambda repo_root, query, budget=64: _payload("focus", query, budget))
    monkeypatch.setattr(source_graph_mod, "slice_", lambda repo_root, query, budget=64: _payload("slice", query, budget))
    monkeypatch.setattr(
        source_graph_mod, "bundle",
        lambda repo_root, bundle_type, query, max_lines=64: _payload("bundle", query, max_lines),
    )
    return calls


def _authority_repo(tmp_path: Path, *, name: str = "authority_repo") -> Path:
    repo = tmp_path / name
    (repo / "AITools" / "ai_memory").mkdir(parents=True)
    for relative in (
        "AITools/session.db", "AITools/transcript_graph.db",
        "AITools/ai_memory/ai_memory.db", "AITools/kb.db",
    ):
        (repo / relative).write_bytes(b"SQLite format 3\x00fake-non-empty-authority-db")
    repository_state.bootstrap_repository(repo)
    source_graph_db = repo / ".aiworkhub" / "source_graph" / "source_graph.sqlite"
    source_graph_db.parent.mkdir(parents=True, exist_ok=True)
    source_graph_db.write_bytes(b"SQLite format 3\x00fake-non-empty-source-graph-db")
    _write_tool(
        repo / "AITools/transcript_graph.py",
        "import json, sys\n"
        "print(json.dumps({'state': 'partial_state', 'evidence': [{'id': 1}], 'topic': sys.argv[2]}))\n",
    )
    _write_tool(
        repo / "AITools/ai_memory/ai_memory.py",
        "import json\nprint(json.dumps({'count': 1, 'results': [{'key': 'ctx', 'value': 'bounded'}]}))\n",
    )
    _write_tool(
        repo / "AITools/kb.py",
        "import sys\nprint('[module] bounded KB context')\n",
    )
    return repo


def _worktree_repo(tmp_path: Path, *, name: str = "worktree") -> Path:
    """A worktree-shaped repo: current AIWorkHub 0.6.2 manifest+registry
    present (so authority resolution succeeds) but no Source Graph database
    ever built -- the exact shape of a fresh ``git worktree add --detach``
    checkout, which is what makes it unsafe as an authority source."""
    repo = tmp_path / name
    (repo / "AITools" / "ai_memory").mkdir(parents=True)
    repository_state.bootstrap_repository(repo)
    return repo


def _mute_chmod(monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


def _ctx(
    *, repo: Path, authority_repo: Path, home: Path,
    targets: tuple[str, ...] = (), request_id: str = "req1", task_id: str = "TASK_B834",
) -> w.WorkerToolContext:
    runtime = w.generate_worker_mcp_runtime(
        home=home, request_id=request_id, task_id=task_id, runner="claude_b834",
        topic="task_mcp", repo=repo, authority_repo=authority_repo,
        source_graph_targets=list(targets), session_topic="AIWorkHub dynamic worker MCP B834 authority repair",
        package_import_root=w.resolve_host_package_import_root(),
    )
    return w.WorkerToolContext(
        task_id=task_id, runner="claude_b834", topic="task_mcp", request_id=request_id,
        repo=repo, authority_repo=authority_repo, source_graph_targets=targets,
        session_topic="AIWorkHub dynamic worker MCP B834 authority repair",
        audit_ledger_path=runtime.audit_ledger_path, audit_hmac_key_path=runtime.audit_hmac_key_path,
    )


# ---------------------------------------------------------------------------
# Dual binding: repo != authority_repo, both immutable, neither overridable
# ---------------------------------------------------------------------------

def test_load_context_from_env_requires_authority_repo_binding(tmp_path: Path) -> None:
    with pytest.raises(w.WorkerToolError):
        w.load_context_from_env({
            w.ENV_TASK_ID: "T", w.ENV_RUNNER: "r", w.ENV_TOPIC: "t",
            w.ENV_REPO: str(tmp_path),
            # ENV_AUTHORITY_REPO deliberately omitted.
        })


def test_load_context_from_env_rejects_nonexistent_authority_repo(tmp_path: Path) -> None:
    with pytest.raises(w.WorkerToolError):
        w.load_context_from_env({
            w.ENV_TASK_ID: "T", w.ENV_RUNNER: "r", w.ENV_TOPIC: "t",
            w.ENV_REPO: str(tmp_path), w.ENV_AUTHORITY_REPO: str(tmp_path / "does_not_exist"),
        })


def test_load_context_from_env_binds_two_independent_repo_paths(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    authority = tmp_path / "authority"
    authority.mkdir()
    ctx = w.load_context_from_env({
        w.ENV_TASK_ID: "T", w.ENV_RUNNER: "r", w.ENV_TOPIC: "t",
        w.ENV_REPO: str(worktree), w.ENV_AUTHORITY_REPO: str(authority),
    })
    assert ctx.repo == worktree.resolve()
    assert ctx.authority_repo == authority.resolve()
    assert ctx.repo != ctx.authority_repo


def test_registered_tools_never_accept_an_authority_repo_override_param(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No tool signature exposes a way for the caller to pick/override the
    authority repository, database path, or task identity."""
    import inspect

    _mute_chmod(monkeypatch)
    authority = _authority_repo(tmp_path)
    ctx = _ctx(repo=authority, authority_repo=authority, home=tmp_path / "home")

    class _FakeMcp:
        def __init__(self) -> None:
            self.registered: dict[str, object] = {}

        def tool(self, *, name: str):
            def decorator(fn):
                self.registered[name] = fn
                return fn
            return decorator

    fake_mcp = _FakeMcp()
    w.register_tools(fake_mcp, ctx)
    forbidden = {"repo", "authority_repo", "repo_root", "task_id", "runner", "topic", "database", "db_path"}
    for name, fn in fake_mcp.registered.items():
        params = set(inspect.signature(fn).parameters)
        assert not (params & forbidden), f"{name} exposes {params & forbidden}"


# ---------------------------------------------------------------------------
# Authority resolution: legacy read-only, fail-closed absent/empty database
# ---------------------------------------------------------------------------

def test_resolve_authority_db_uses_canonical_path_and_reports_state(tmp_path: Path) -> None:
    """B878: canonical-only resolution -- a fresh registry's ``kb`` entry is
    already ``canonical_active`` from birth, so the resolver reads the
    canonical ``.aiworkhub/kb/knowledge.sqlite`` path, never a legacy path."""
    authority = _authority_repo(tmp_path)
    kb_db = authority / ".aiworkhub" / "kb" / "knowledge.sqlite"
    kb_db.parent.mkdir(parents=True, exist_ok=True)
    kb_db.write_bytes(b"SQLite format 3\x00fake-non-empty-kb-db")
    ctx = w.WorkerToolContext(
        task_id="T", runner="r", topic="t", request_id="req1",
        repo=authority, authority_repo=authority, source_graph_targets=(),
        session_topic="t", audit_ledger_path=None, audit_hmac_key_path=None,
    )
    binding = w._resolve_authority_db(ctx, component="kb", db_id="kb")
    assert binding.authority_source == "canonical"
    assert binding.authority_state == "canonical_active"
    assert binding.db_path == kb_db.resolve()


def test_resolve_authority_db_fails_closed_when_component_not_canonical_active(tmp_path: Path) -> None:
    """B878: there is no ``legacy_source`` fallback -- an entry that is not
    yet ``authority.canonical_active`` fails closed instead of silently
    reading a legacy path."""
    worktree = _worktree_repo(tmp_path)
    _write_storage_registry(worktree, canonical_active=False)
    ctx = w.WorkerToolContext(
        task_id="T", runner="r", topic="t", request_id="req1",
        repo=worktree, authority_repo=worktree, source_graph_targets=(),
        session_topic="t", audit_ledger_path=None, audit_hmac_key_path=None,
    )
    with pytest.raises(w.WorkerToolError, match="authority_component_not_canonical_active"):
        w._resolve_authority_db(ctx, component="kb", db_id="kb")


def test_resolve_authority_db_fails_closed_on_missing_registry_entry(tmp_path: Path) -> None:
    authority = _authority_repo(tmp_path)
    ctx = w.WorkerToolContext(
        task_id="T", runner="r", topic="t", request_id="req1",
        repo=authority, authority_repo=authority, source_graph_targets=(),
        session_topic="t", audit_ledger_path=None, audit_hmac_key_path=None,
    )
    with pytest.raises(w.WorkerToolError, match="authority_registry_entry_missing"):
        w._resolve_authority_db(ctx, component="does_not", db_id="exist")


def test_resolve_authority_db_fails_closed_on_absent_database(tmp_path: Path) -> None:
    """The isolated-worktree shape: registry entry is canonical_active, but
    the canonical database file was never built. Never falls back to a
    legacy path and never lets a script silently create an empty one."""
    worktree = _worktree_repo(tmp_path)
    _write_storage_registry(worktree, canonical_active=True)
    ctx = w.WorkerToolContext(
        task_id="T", runner="r", topic="t", request_id="req1",
        repo=worktree, authority_repo=worktree, source_graph_targets=(),
        session_topic="t", audit_ledger_path=None, audit_hmac_key_path=None,
    )
    with pytest.raises(w.WorkerToolError, match="authority_db_absent_or_empty"):
        w._resolve_authority_db(ctx, component="source_graph", db_id="source_graph")
    # And no database file was created as a side effect of the failed lookup.
    assert not (worktree / "AITools/source_graph.db").exists()
    assert not (worktree / ".aiworkhub/source_graph/source_graph.sqlite").exists()


def test_resolve_authority_db_fails_closed_on_empty_database(tmp_path: Path) -> None:
    authority = _authority_repo(tmp_path)
    (authority / "AITools/kb.db").write_bytes(b"")  # truncate to empty
    ctx = w.WorkerToolContext(
        task_id="T", runner="r", topic="t", request_id="req1",
        repo=authority, authority_repo=authority, source_graph_targets=(),
        session_topic="t", audit_ledger_path=None, audit_hmac_key_path=None,
    )
    with pytest.raises(w.WorkerToolError, match="authority_db_absent_or_empty"):
        w._resolve_authority_db(ctx, component="kb", db_id="kb")


def test_source_graph_query_against_isolated_worktree_authority_is_a_violation_not_empty_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A B833-style single-binding call against a database-less worktree
    used to report ok=true with hit_count trending to zero. It must now be a
    recorded, fail-closed violation instead."""
    _mute_chmod(monkeypatch)
    worktree = _worktree_repo(tmp_path)
    ctx = _ctx(repo=worktree, authority_repo=worktree, home=tmp_path / "home")
    result = w.source_graph_query(ctx, mode="focus", query="anything")
    assert result["ok"] is False
    assert "authority_db_absent_or_empty" in result["reason"]


# ---------------------------------------------------------------------------
# Query-vs-target argv bug: query is always the CLI term, target only scopes
# ---------------------------------------------------------------------------

def test_omitting_target_never_replaces_query_with_declared_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _mute_chmod(monkeypatch)
    authority = _authority_repo(tmp_path)
    calls = _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(
        repo=authority, authority_repo=authority, home=tmp_path / "home",
        targets=("AITools/source_graph.py",),
    )
    result = w.source_graph_query(ctx, mode="focus", query="a very specific semantic query")
    assert result["ok"] is True
    assert result["target"] is None
    assert calls == [("focus", "a very specific semantic query", 64)]
    payload = json.loads(result["content"])
    assert payload["target"] == "a very specific semantic query"


def test_declared_target_scopes_returned_files_without_touching_the_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _mute_chmod(monkeypatch)
    authority = _authority_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(
        repo=authority, authority_repo=authority, home=tmp_path / "home",
        targets=("AITools",),
    )
    result = w.source_graph_query(ctx, mode="focus", query="real query text", target="AITools")
    assert result["ok"] is True
    assert result["target"] == "AITools"
    payload = json.loads(result["content"])
    assert payload["target"] == "real query text"
    assert payload["relevant_files"] == ["AITools/source_graph.py"]


def test_target_outside_declared_allowlist_is_rejected_before_any_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _mute_chmod(monkeypatch)
    authority = _authority_repo(tmp_path)
    ctx = _ctx(
        repo=authority, authority_repo=authority, home=tmp_path / "home",
        targets=("AITools",),
    )
    result = w.source_graph_query(ctx, mode="focus", query="q", target="tools/unrelated")
    assert result["ok"] is False
    assert result["reason"] == "target_not_allowed"


# ---------------------------------------------------------------------------
# Live-call gate: zero-hit and cache-hit calls must NOT satisfy it
# ---------------------------------------------------------------------------

def test_zero_hit_call_does_not_satisfy_the_live_call_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _mute_chmod(monkeypatch)
    authority = _authority_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo=authority, authority_repo=authority, home=tmp_path / "home", request_id="req1")
    result = w.source_graph_query(ctx, mode="focus", query="q", target=None)
    assert result["hit_count"] > 0  # sanity: the fixture itself is non-empty

    # A target scope that matches nothing produces hit_count == 0 for an
    # otherwise ok=true, non-cached call.
    monkeypatch.setattr(w, "_filter_by_scope", lambda payload, scope: {})
    zero_hit = w.source_graph_query(ctx, mode="focus", query="q", target="AITools")
    assert zero_hit["ok"] is True
    assert zero_hit["hit_count"] == 0

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic, request_id="req1",
    )
    # Exactly one live call recorded (the first, non-empty one); the zero-hit
    # call must not count even though it was ok=true and not cached.
    assert verification["live_source_graph_calls"] == 1


def test_cache_hit_call_does_not_satisfy_the_live_call_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _mute_chmod(monkeypatch)
    authority = _authority_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo=authority, authority_repo=authority, home=tmp_path / "home", request_id="req1")
    w.source_graph_query(ctx, mode="focus", query="repeat me")
    w.source_graph_query(ctx, mode="focus", query="repeat me")  # second call is a cache hit

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic, request_id="req1",
    )
    assert verification["call_count_by_tool"]["source_graph"] == 2
    assert verification["cache_hits"] == 1
    # Only the first (live, non-cached) call counts toward the gate.
    assert verification["live_source_graph_calls"] == 1
    # Source Graph is the sole authority since B849 -- never legacy/shadow.
    assert verification["authority_index_identity"] == ["source_graph:canonical:sole_authority"]


def test_process_launcher_live_call_gate_rejects_a_cache_only_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    entry = {
        "schema_id": w.AUDIT_ENTRY_SCHEMA_ID, "timestamp": "2026-01-01T00:00:00+00:00",
        "task_id": "T", "runner": "r", "topic": "t", "request_id": "req1",
        "tool": "source_graph", "ok": True, "cache_hit": True, "hit_count": 5,
        "bytes_returned": 5, "violation": "", "authority_source": "legacy", "authority_state": "shadow",
    }
    digest = w._hmac_entry(entry, key.read_bytes())
    ledger.write_text(json.dumps({**entry, "hmac_sha256": digest}) + "\n", encoding="utf-8")
    metadata = {
        "task_id": "T", "runner": "r", "topic": "t",
        "project_context": {"task_context_policy": {"task_type": "code"}},
        "worker_mcp": {"audit_ledger_path": str(ledger), "audit_hmac_key_path": str(key)},
    }
    gate = process_launcher._worker_mcp_live_call_gate(metadata, "req1")
    assert gate["gated"] is True
    assert gate["satisfied"] is False


# ---------------------------------------------------------------------------
# Cross-context cache isolation
# ---------------------------------------------------------------------------

def test_cache_does_not_leak_across_task_or_authority_repo_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _mute_chmod(monkeypatch)
    authority_a = _authority_repo(tmp_path, name="authority_a")
    authority_b = _authority_repo(tmp_path, name="authority_b")
    _stub_source_graph_engine(monkeypatch)

    ctx_a = _ctx(repo=authority_a, authority_repo=authority_a, home=tmp_path / "home_a",
                 request_id="req_a", task_id="TASK_A")
    first = w.source_graph_query(ctx_a, mode="focus", query="shared query text")
    assert first["cache_hit"] is False

    # Different task_id, same authority_repo, same query/mode/budget.
    ctx_b = _ctx(repo=authority_a, authority_repo=authority_a, home=tmp_path / "home_b",
                 request_id="req_b", task_id="TASK_B")
    second = w.source_graph_query(ctx_b, mode="focus", query="shared query text")
    assert second["cache_hit"] is False

    # Different authority_repo entirely, same task/request id reused.
    ctx_c = _ctx(repo=authority_b, authority_repo=authority_b, home=tmp_path / "home_c",
                 request_id="req_a", task_id="TASK_A")
    third = w.source_graph_query(ctx_c, mode="focus", query="shared query text")
    assert third["cache_hit"] is False


# ---------------------------------------------------------------------------
# Landlock / bubblewrap authority path mapping
# ---------------------------------------------------------------------------

def test_bubblewrap_sandbox_argv_ro_binds_the_authority_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mute_chmod(monkeypatch)
    repo = tmp_path / "authority"
    repo.mkdir()
    path = tmp_path / "req" / "worktree"
    home = tmp_path / "req" / "home"
    path.mkdir(parents=True)
    home.mkdir(parents=True)
    (path / ".git").mkdir()
    workspace = worker_workspace.WorkerWorkspace(
        request_id="req1", repo=repo, path=path, home=home,
        allowed_writes=("out.json",), parent_baseline={}, workspace_baseline={},
    )
    argv = worker_workspace.sandbox_argv(workspace, "claude_cli", ["/usr/bin/claude"], backend="bubblewrap")
    assert "--ro-bind" in argv
    idx = argv.index(str(repo))
    assert argv[idx - 1] == "--ro-bind"
    assert argv[idx + 1] == worker_workspace.SANDBOX_AUTHORITY_REPO


def test_provision_worker_mcp_runtime_requires_a_real_authority_repo_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _mute_chmod(monkeypatch)
    missing_repo = tmp_path / "does_not_exist"
    workspace = worker_workspace.WorkerWorkspace(
        request_id="req1", repo=missing_repo, path=tmp_path / "wt", home=tmp_path / "home",
        allowed_writes=("out.json",), parent_baseline={}, workspace_baseline={},
    )
    with pytest.raises(worker_workspace.WorkspaceError):
        worker_workspace.provision_worker_mcp_runtime(
            workspace, request_id="req1", task_id="T", runner="r", topic="t",
            backend="landlock", source_graph_targets=[], session_topic="t",
        )


# ---------------------------------------------------------------------------
# Mandatory config injection: failure rejects launch, never silently degrades
# ---------------------------------------------------------------------------

def test_all_three_adapters_receive_the_authority_bound_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mute_chmod(monkeypatch)
    authority = _authority_repo(tmp_path)
    home = tmp_path / "home"
    runtime = w.generate_worker_mcp_runtime(
        home=home, request_id="req1", task_id="T", runner="r", topic="t",
        repo=authority, authority_repo=authority, source_graph_targets=[], session_topic="t",
        package_import_root=w.resolve_host_package_import_root(),
    )
    claude_cfg = json.loads(runtime.claude_mcp_config_path.read_text(encoding="utf-8"))
    copilot_cfg = json.loads(runtime.copilot_mcp_config_path.read_text(encoding="utf-8"))
    codex_toml = runtime.codex_config_toml_path.read_text(encoding="utf-8")
    for cfg in (claude_cfg, copilot_cfg):
        assert cfg["mcpServers"][w.SERVER_NAME]["env"][w.ENV_AUTHORITY_REPO] == str(authority)
    assert w.ENV_AUTHORITY_REPO in codex_toml and str(authority) in codex_toml


def test_inject_worker_mcp_config_rejects_launch_for_all_launchable_adapters_on_missing_config(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for adapter_id in ("claude_cli", runtime_adapters.DEEPSEEK_COPILOT_ADAPTER, runtime_adapters.GLM_COPILOT_ADAPTER):
        plan = runtime_adapters.RuntimeAdapterPlan(
            adapter_id=adapter_id, argv=[f"/usr/bin/{adapter_id}"], cwd=str(repo),
            executable=f"/usr/bin/{adapter_id}", launchable=True, manual_only=False,
            validation_ok=True, validation_reason="",
        )
        with pytest.raises(ValueError):
            runtime_adapters.inject_worker_mcp_config(plan, tmp_path / "missing.json")


# ---------------------------------------------------------------------------
# No mutation/coordinator tools; data-task exemption preserved under repair
# ---------------------------------------------------------------------------

def test_worker_ai_tools_module_still_imports_no_mutation_or_coordinator_surface() -> None:
    source = Path(w.__file__).read_text(encoding="utf-8")
    for forbidden in ("import core", "from . import core", "import process_launcher", "from . import process_launcher"):
        assert forbidden not in source
    assert "shell=True" not in source


def test_live_call_gate_still_exempts_data_classification_tasks_after_repair(tmp_path: Path) -> None:
    gate = process_launcher._worker_mcp_live_call_gate(
        {
            "task_id": "T", "runner": "r", "topic": "t",
            "project_context": {"task_context_policy": {"task_type": "data_classification"}},
            "worker_mcp": {},
        },
        "req1",
    )
    assert gate["gated"] is False
    assert gate["satisfied"] is True


# ---------------------------------------------------------------------------
# Cross-module consistency + eval artifact
# ---------------------------------------------------------------------------

def test_eval_artifact_b834_matches_live_defect_repair() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "eval"
        / "aiworkhub_dynamic_worker_mcp_authority_repair_b834_v1.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_id"] == "aiworkhub.task_mcp.aiworkhub_dynamic_worker_mcp_authority_repair_b834_v1.eval.v1"
    assert payload["recovered_from_commit"] == "742099f332d57cd84109b0f964c1487cac54477d"
    defect_ids = {d["id"] for d in payload["defects_repaired"]}
    assert "query_argument_discarded_when_target_declared" in defect_ids
    assert "zero_hit_and_cache_hit_calls_satisfied_the_live_call_gate" in defect_ids
    assert "single_repo_binding_hid_missing_authority_db" in defect_ids
    b829_paths = {
        "tools/geoai-task-mcp/eval/aiworkhub_context_optimization_b829_v1.json",
        "tools/geoai-task-mcp/eval/aiworkhub_context_optimization_rows_b829_v1.jsonl",
    }
    assert b829_paths.isdisjoint(set(payload["changed_paths"]))
