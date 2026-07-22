"""B833: dynamic, task-scoped worker MCP tool loop.

Covers: MCP tool registration/bounds, repository/task identity rejection,
the three adapter config injection shapes (Claude --mcp-config, Codex
isolated CODEX_HOME config.toml, Copilot --additional-mcp-config), sandbox
visibility (runtime files scoped under the isolated home), no credential
leakage, the source-call completion gate (and its data-task exemption),
telemetry accounting, and a fake-worker end-to-end dynamic tool call.
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import agent_tool_instructions as instr  # noqa: E402
from aiworkhub import process_launcher  # noqa: E402
from aiworkhub import repository_state  # noqa: E402
from aiworkhub import runtime_adapters  # noqa: E402
from aiworkhub import source_graph as source_graph_mod  # noqa: E402
from aiworkhub import worker_ai_tools_mcp as w  # noqa: E402
from aiworkhub import worker_workspace  # noqa: E402


def _mute_chmod(monkeypatch: pytest.MonkeyPatch) -> None:
    """This exact sandboxed worker session denies chmod/fchmod (seccomp) --
    the SAME condition ``worker_workspace._probe_exec_capable_dir`` already
    documents as an anticipated, best-effort-only failure mode. The files
    under test are created with the right mode bits atomically via
    ``os.open(path, flags, mode)``/``mkdir(mode=...)``; the follow-up
    ``chmod``/``fchmod`` calls are defense-in-depth only, so muting them here
    (matching the existing ``test_worker_ai_infra_context_b437_v1.py``
    idiom) does not weaken what this test actually verifies.
    """
    monkeypatch.setattr(os, "chmod", lambda *args, **kwargs: None)
    monkeypatch.setattr(os, "fchmod", lambda *args, **kwargs: None)


# ---------------------------------------------------------------------------
# Real, minimal canonical SQLite fixtures (B878): worker_ai_tools_mcp now
# queries the KB / AI Memory / Session-Manager canonical databases directly
# with in-process sqlite3 -- it no longer shells out to AITools/*.py, so
# these tests must build real schema-matching databases at the registry's
# canonical paths instead of fake non-sqlite placeholder bytes.
# ---------------------------------------------------------------------------

def _seed_kb_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'note', tags TEXT NOT NULL DEFAULT '',
            source_refs TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE entries_fts USING fts5(
            key, title, body, tags, content=entries, content_rowid=id,
            tokenize='unicode61 remove_diacritics 0'
        );
        CREATE TRIGGER entries_ai AFTER INSERT ON entries BEGIN
            INSERT INTO entries_fts(rowid, key, title, body, tags)
            VALUES (new.id, new.key, new.title, new.body, new.tags);
        END;
        CREATE TABLE links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_key TEXT NOT NULL, to_key TEXT NOT NULL, relation TEXT NOT NULL DEFAULT 'related'
        );
        """
    )
    now = "2026-01-01T00:00:00Z"
    con.executemany(
        "INSERT INTO entries(key,title,body,category,tags,source_refs,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                "pipeline.stage_order", "Task MCP worker tools",
                "bounded Source Graph context for Task MCP worker tools",
                "module", "task_mcp,worker", "", now, now,
            ),
            (
                "arch.v_grow.multi_layer", "v_grow multi layer",
                "spawn conditions, dilution risk", "arch", "task_mcp", "", now, now,
            ),
        ],
    )
    con.execute(
        "INSERT INTO links(from_key,to_key,relation) VALUES (?,?,?)",
        ("arch.v_grow.multi_layer", "pipeline.stage_order", "related"),
    )
    con.commit()
    con.close()


def _seed_memory_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, value TEXT NOT NULL,
            tags TEXT DEFAULT '', scope TEXT DEFAULT 'persistent', project TEXT DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            access_count INTEGER DEFAULT 0, last_accessed TEXT DEFAULT ''
        );
        CREATE VIRTUAL TABLE memories_fts USING fts5(key, value, tags, content=memories, content_rowid=id);
        CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, key, value, tags)
            VALUES (new.id, new.key, new.value, new.tags);
        END;
        """
    )
    now = "2026-01-01T00:00:00+00:00"
    con.execute(
        "INSERT INTO memories(key,value,tags,scope,project,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        ("ctx", "bounded worker dynamic MCP context", "task_mcp", "persistent", "", now, now),
    )
    con.commit()
    con.close()


def _seed_transcript_db(path: Path, *, topic: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE documents (
            doc_id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, source_id TEXT NOT NULL,
            session_id INTEGER, timestamp TEXT, kind TEXT, speaker TEXT, content TEXT NOT NULL, tags TEXT
        );
        CREATE VIRTUAL TABLE documents_fts USING fts5(
            content, kind, tags, content='documents', content_rowid='doc_id'
        );
        CREATE TRIGGER documents_ai AFTER INSERT ON documents BEGIN
            INSERT INTO documents_fts(rowid, content, kind, tags)
            VALUES (new.doc_id, new.content, new.kind, new.tags);
        END;
        """
    )
    con.execute(
        "INSERT INTO documents(source,source_id,session_id,timestamp,kind,speaker,content,tags) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("session", "evt-1", 1, "2026-01-01T00:00:00Z", "progress", "worker", f"{topic} bounded discovery", ""),
    )
    con.commit()
    con.close()


def _bootstrap_manifest_and_registry(repo: Path) -> None:
    """Canonical AIWorkHub 0.6.2 repo-local authority: a real
    ``.aiworkhub/project.json`` manifest plus the matching
    ``.aiworkhub/config/storage.json`` registry -- the current explicit
    binding every authority lookup (generic and Source Graph) resolves
    against. Every declared component defaults to its ``legacy_source``
    (matches the real repo's current, not-yet-cut-over state)."""
    repository_state.bootstrap_repository(repo)


def _stub_source_graph_engine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    relevant_files: tuple[str, ...] = ("AITools/x.py", "tools/geoai-task-mcp/src/aiworkhub/x.py"),
) -> list[tuple[str, str, int]]:
    """Stand in for aiworkhub.source_graph's real FTS query engine (its own
    correctness is covered by that module's own test suite) so these
    fixtures can assert query/caching/audit semantics against a
    deterministic payload without building a real on-disk index. Returns
    the list of (mode, query, budget) calls actually made."""
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


def _fake_repo(tmp_path: Path, *, name: str = "repo") -> Path:
    """A standalone authority repository with canonical SQLite stores only.

    Deliberately does not create an ``AITools`` directory: every worker tool
    must operate from the selected repository's ``.aiworkhub`` authority.
    """
    repo = tmp_path / name
    repo.mkdir(parents=True)
    _bootstrap_manifest_and_registry(repo)
    assert not (repo / "AITools").exists()
    source_graph_db = repo / ".aiworkhub" / "source_graph" / "source_graph.sqlite"
    source_graph_db.parent.mkdir(parents=True, exist_ok=True)
    source_graph_db.write_bytes(b"SQLite format 3\x00fake-non-empty-source-graph-db")
    # session_current_state resolves this component's existence/size only --
    # it is never opened -- so a non-empty placeholder is sufficient.
    (repo / ".aiworkhub" / "sessions" / "sessions.sqlite").parent.mkdir(parents=True, exist_ok=True)
    (repo / ".aiworkhub" / "sessions" / "sessions.sqlite").write_bytes(b"SQLite format 3\x00fake-non-empty-session-db")
    _seed_transcript_db(
        repo / ".aiworkhub" / "sessions" / "transcript_graph.sqlite",
        topic="AIWorkHub dynamic worker MCP B833",
    )
    _seed_memory_db(repo / ".aiworkhub" / "memory" / "memory.sqlite")
    _seed_kb_db(repo / ".aiworkhub" / "kb" / "knowledge.sqlite")
    return repo


def _ctx(
    repo: Path, *, home: Path, targets: tuple[str, ...] = (), request_id: str = "req1",
    authority_repo: Path | None = None,
) -> w.WorkerToolContext:
    authority_repo = authority_repo if authority_repo is not None else repo
    runtime = w.generate_worker_mcp_runtime(
        home=home, request_id=request_id, task_id="TASK_B833", runner="claude_b833",
        topic="task_mcp", repo=repo, authority_repo=authority_repo,
        source_graph_targets=list(targets), session_topic="AIWorkHub dynamic worker MCP B833",
        package_import_root=w.resolve_host_package_import_root(),
    )
    return w.WorkerToolContext(
        task_id="TASK_B833", runner="claude_b833", topic="task_mcp", request_id=request_id,
        repo=repo, authority_repo=authority_repo, source_graph_targets=targets,
        session_topic="AIWorkHub dynamic worker MCP B833",
        audit_ledger_path=runtime.audit_ledger_path, audit_hmac_key_path=runtime.audit_hmac_key_path,
    )


# ---------------------------------------------------------------------------
# Identity binding / fail-closed rejection
# ---------------------------------------------------------------------------

def test_load_context_from_env_fails_closed_on_missing_identity() -> None:
    with pytest.raises(w.WorkerToolError):
        w.load_context_from_env({})


def test_load_context_from_env_rejects_nonexistent_repo(tmp_path: Path) -> None:
    with pytest.raises(w.WorkerToolError):
        w.load_context_from_env({
            w.ENV_TASK_ID: "T", w.ENV_RUNNER: "r", w.ENV_TOPIC: "t",
            w.ENV_REPO: str(tmp_path / "does_not_exist"),
        })


def test_load_context_from_env_binds_exact_identity(tmp_path: Path) -> None:
    ctx = w.load_context_from_env({
        w.ENV_TASK_ID: "TASK_B833", w.ENV_RUNNER: "claude_b833", w.ENV_TOPIC: "task_mcp",
        w.ENV_REPO: str(tmp_path), w.ENV_AUTHORITY_REPO: str(tmp_path),
        w.ENV_SOURCE_GRAPH_TARGETS: json.dumps(["a.py", "b.py"]),
        w.ENV_SESSION_TOPIC: "custom-topic",
    })
    assert ctx.task_id == "TASK_B833"
    assert ctx.authority_repo == tmp_path.resolve()
    assert ctx.source_graph_targets == ("a.py", "b.py")
    assert ctx.session_topic == "custom-topic"


def test_source_graph_query_rejects_target_outside_declared_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    ctx = _ctx(repo, home=tmp_path / "home", targets=("AITools/source_graph.py",))
    result = w.source_graph_query(ctx, mode="focus", query="anything", target="tools/unrelated/other_repo.py")
    assert result["ok"] is False
    assert result["reason"] == "target_not_allowed"
    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic,
    )
    assert verification["policy_violations"] == 1
    assert verification["live_source_graph_calls"] == 0


def test_source_graph_query_rejects_invalid_mode_and_bundle_type(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    ctx = _ctx(repo, home=tmp_path / "home")
    assert w.source_graph_query(ctx, mode="grep", query="x")["reason"].startswith("invalid_mode")
    assert w.source_graph_query(ctx, mode="focus", query="x", bundle_type="hack")["reason"].startswith("invalid_bundle_type")


# ---------------------------------------------------------------------------
# Bounded tool calls + caching + audit accounting
# ---------------------------------------------------------------------------

def test_source_graph_query_runs_bounded_and_second_call_is_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo, home=tmp_path / "home", targets=("AITools/source_graph.py",))

    first = w.source_graph_query(ctx, mode="focus", query="ignored", budget=32)
    assert first["ok"] is True
    assert first["hit_count"] > 0
    assert first["cache_hit"] is False

    second = w.source_graph_query(ctx, mode="focus", query="ignored", budget=32)
    assert second["cache_hit"] is True
    assert second["content"] == first["content"]

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic,
    )
    assert verification["call_count_by_tool"]["source_graph"] == 2
    assert verification["cache_hits"] == 1
    # B834: a cache hit must NOT count toward the live-call gate -- only the
    # first, genuinely fresh call does.
    assert verification["live_source_graph_calls"] == 1
    assert verification["bounded_bytes_returned"] > 0


def test_session_ai_memory_and_kb_tools_are_bounded_and_audited(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    ctx = _ctx(repo, home=tmp_path / "home")

    session = w.session_current_state(ctx, limit=5)
    assert session["ok"] is True and session["topic"] == ctx.session_topic

    memory = w.ai_memory_search(ctx, query="worker dynamic MCP", limit=3)
    assert memory["ok"] is True and memory["hit_count"] > 0

    kb_search = w.kb_search(ctx, query="Task MCP worker tools")
    kb_get = w.kb_get(ctx, key="pipeline.stage_order")
    kb_related = w.kb_related(ctx, key="arch.v_grow.multi_layer")
    for result in (kb_search, kb_get, kb_related):
        assert result["ok"] is True
        assert result["tool"] == "kb"

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic,
    )
    assert verification["call_count_by_tool"] == {
        "session_current_state": 1, "ai_memory": 1, "kb": 3,
    }


def test_ai_memory_search_rejects_overbudget_query(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    ctx = _ctx(repo, home=tmp_path / "home")
    result = w.ai_memory_search(ctx, query="x" * 600)
    assert result["ok"] is False and result["reason"] == "invalid_query"


# ---------------------------------------------------------------------------
# Audit ledger authentication: tamper detection + cross-task isolation
# ---------------------------------------------------------------------------

def test_verify_audit_ledger_drops_tampered_and_forged_entries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo, home=tmp_path / "home", targets=("AITools/source_graph.py",))
    w.source_graph_query(ctx, mode="focus", query="ignored")

    # A worker cannot fabricate a "live call" merely by appending text that
    # looks like an audit entry -- it does not know the per-request HMAC key.
    forged = {
        "schema_id": w.AUDIT_ENTRY_SCHEMA_ID, "timestamp": "2026-01-01T00:00:00+00:00",
        "task_id": ctx.task_id, "runner": ctx.runner, "topic": ctx.topic, "request_id": ctx.request_id,
        "tool": "source_graph", "ok": True, "cache_hit": False, "hit_count": 99,
        "bytes_returned": 99, "violation": "", "hmac_sha256": "0" * 64,
    }
    with ctx.audit_ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(forged) + "\n")

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic,
    )
    assert verification["entries_total"] == 2
    assert verification["entries_tampered"] == 1
    assert verification["entries_verified"] == 1
    assert verification["live_source_graph_calls"] == 1


def test_verify_audit_ledger_ignores_calls_from_a_different_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo, home=tmp_path / "home", targets=("AITools/source_graph.py",))
    w.source_graph_query(ctx, mode="focus", query="ignored")

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id="SOME_OTHER_TASK", runner=ctx.runner, topic=ctx.topic,
    )
    assert verification["live_source_graph_calls"] == 0
    assert verification["entries_verified"] == 0


def test_verify_audit_ledger_fails_closed_on_unreadable_key_or_ledger(tmp_path: Path) -> None:
    result = w.verify_audit_ledger(
        tmp_path / "missing_ledger.jsonl", tmp_path / "missing_key",
        task_id="T", runner="r", topic="t",
    )
    assert result["ok"] is False
    assert result["live_source_graph_calls"] == 0


def test_audit_verification_never_exposes_paths_or_key_material(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo, home=tmp_path / "home", targets=("AITools/source_graph.py",))
    w.source_graph_query(ctx, mode="focus", query="ignored")
    key_bytes = ctx.audit_hmac_key_path.read_bytes()

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic,
    )
    dumped = json.dumps(verification)
    assert str(ctx.audit_ledger_path) not in dumped
    assert str(ctx.audit_hmac_key_path) not in dumped
    assert key_bytes.hex() not in dumped


# ---------------------------------------------------------------------------
# MCP tool registration: worker-safe bounds, no repo/task override
# ---------------------------------------------------------------------------

class _FakeMcp:
    def __init__(self) -> None:
        self.registered: dict[str, object] = {}

    def tool(self, *, name: str):
        def decorator(fn):
            self.registered[name] = fn
            return fn
        return decorator


def test_register_tools_exposes_exactly_the_worker_safe_tool_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    ctx = _ctx(repo, home=tmp_path / "home")
    fake_mcp = _FakeMcp()
    names = w.register_tools(fake_mcp, ctx)
    assert set(names) == set(w.MCP_TOOL_NAMES)
    assert set(fake_mcp.registered) == set(w.MCP_TOOL_NAMES)


def test_registered_tool_signatures_never_accept_repo_or_task_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    ctx = _ctx(repo, home=tmp_path / "home")
    fake_mcp = _FakeMcp()
    w.register_tools(fake_mcp, ctx)
    forbidden_params = {"repo", "repo_root", "task_id", "runner", "topic", "path", "cwd", "database", "db_path"}
    for name, fn in fake_mcp.registered.items():
        params = set(inspect.signature(fn).parameters)
        assert not (params & forbidden_params), f"{name} exposes a caller-controlled binding param: {params & forbidden_params}"


def test_worker_ai_tools_module_imports_no_task_mutation_or_shell_surface() -> None:
    """Task mutation / process launch / coordinator tools live only in
    server.py / process_launcher.py -- this module must never import them."""
    source = Path(w.__file__).read_text(encoding="utf-8")
    assert "import core" not in source
    assert "from . import core" not in source
    assert "import process_launcher" not in source
    assert "from . import process_launcher" not in source
    # B878: KB / AI Memory / Session Manager are now queried in-process via
    # sqlite3 -- this module never shells out to AITools/*.py at all.
    assert "import subprocess" not in source
    assert "shell=True" not in source


def test_fake_worker_end_to_end_dynamic_tool_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Simulate a live worker calling a dynamically registered MCP tool
    (not the precomputed PROJECT_CONTEXT_BUNDLE) and verify it lands in the
    authenticated audit ledger as a real, independently-verifiable call."""
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo, home=tmp_path / "home", targets=("AITools/source_graph.py",))
    fake_mcp = _FakeMcp()
    w.register_tools(fake_mcp, ctx)

    dynamic_call = fake_mcp.registered["aiworkhub_worker_source_graph_query"]
    response = dynamic_call(mode="slice", query="worker mcp tools", budget=40)
    assert response["ok"] is True

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic,
    )
    assert verification["live_source_graph_calls"] == 1


# ---------------------------------------------------------------------------
# Per-request runtime generation: three adapter config shapes + sandbox scope
# ---------------------------------------------------------------------------

def test_generate_worker_mcp_runtime_writes_three_adapter_config_shapes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mute_chmod(monkeypatch)
    home = tmp_path / "home"
    repo = _fake_repo(tmp_path)
    runtime = w.generate_worker_mcp_runtime(
        home=home, request_id="req42", task_id="TASK_B833", runner="claude_b833",
        topic="task_mcp", repo=repo, authority_repo=repo, source_graph_targets=["AITools/source_graph.py"],
        session_topic="AIWorkHub dynamic worker MCP B833",
        package_import_root=w.resolve_host_package_import_root(),
    )

    # Sandbox visibility: every generated artifact lives under this request's
    # isolated home, not the repo and not a sibling task's home.
    for path in (
        runtime.claude_mcp_config_path, runtime.copilot_mcp_config_path,
        runtime.codex_config_toml_path, runtime.audit_ledger_path, runtime.audit_hmac_key_path,
    ):
        assert str(path).startswith(str(home))

    module_file = Path(w.__file__).resolve()
    expected_package_module = f"{module_file.parent.name}.{module_file.stem}"

    claude_cfg = json.loads(runtime.claude_mcp_config_path.read_text(encoding="utf-8"))
    server = claude_cfg["mcpServers"][w.SERVER_NAME]
    assert server["command"] == sys.executable
    # B869: launched as a package module, never the bare relative-importing
    # file -- `python <file.py>` runs it as `__main__` with no known parent
    # package, so `from .repository_state import ...` raises ImportError.
    assert server["args"] == ["-m", expected_package_module]
    assert server["env"][w.ENV_TASK_ID] == "TASK_B833"
    assert server["env"][w.ENV_PYTHONPATH] == str(module_file.parents[1])

    copilot_cfg = json.loads(runtime.copilot_mcp_config_path.read_text(encoding="utf-8"))
    assert copilot_cfg == claude_cfg

    codex_toml = runtime.codex_config_toml_path.read_text(encoding="utf-8")
    assert f"[mcp_servers.{w.SERVER_NAME}]" in codex_toml
    assert f"[mcp_servers.{w.SERVER_NAME}.env]" in codex_toml
    assert f'args = ["-m", "{expected_package_module}"]' in codex_toml
    assert w.ENV_TASK_ID in codex_toml and "TASK_B833" in codex_toml
    assert w.ENV_PYTHONPATH in codex_toml and str(module_file.parents[1]) in codex_toml
    assert runtime.codex_config_toml_path == home / ".codex" / "config.toml"

    # No credential leakage: the runtime env never carries a provider secret.
    assert "ANTHROPIC_API_KEY" not in runtime.env
    assert "COPILOT_PROVIDER_API_KEY" not in runtime.env
    assert all(name in runtime.env for name in w.BOUND_ENV_VARS)


def test_generate_worker_mcp_runtime_is_idempotent_on_the_audit_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mute_chmod(monkeypatch)
    home = tmp_path / "home"
    repo = _fake_repo(tmp_path)
    kwargs = dict(
        home=home, request_id="req1", task_id="T", runner="r", topic="t",
        repo=repo, authority_repo=repo, source_graph_targets=[], session_topic="t",
        package_import_root=w.resolve_host_package_import_root(),
    )
    first = w.generate_worker_mcp_runtime(**kwargs)
    key_after_first = first.audit_hmac_key_path.read_bytes()
    second = w.generate_worker_mcp_runtime(**kwargs)
    assert second.audit_hmac_key_path.read_bytes() == key_after_first


def test_provision_worker_mcp_runtime_wrapper_delegates_to_worker_ai_tools_mcp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    workspace = worker_workspace.WorkerWorkspace(
        request_id="req1", repo=repo, path=repo, home=tmp_path / "home",
        allowed_writes=("out.json",), parent_baseline={}, workspace_baseline={},
    )
    runtime = worker_workspace.provision_worker_mcp_runtime(
        workspace, request_id="req1", task_id="TASK_B833", runner="claude_b833",
        topic="task_mcp", backend="landlock", source_graph_targets=["AITools/source_graph.py"],
        session_topic="AIWorkHub dynamic worker MCP B833",
    )
    assert runtime.env[w.ENV_REPO] == str(repo)
    assert runtime.env[w.ENV_AUTHORITY_REPO] == str(repo)
    assert str(runtime.audit_ledger_path).startswith(str(workspace.home))


def test_provision_worker_mcp_runtime_rewrites_bubblewrap_paths_to_sandbox_aliases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """B834: under bubblewrap, the real host paths are invisible inside the
    sandbox mount namespace -- only the bound sandbox aliases are -- so the
    injected env must carry the alias strings, not the host paths."""
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    workspace = worker_workspace.WorkerWorkspace(
        request_id="req1", repo=repo, path=repo, home=tmp_path / "home",
        allowed_writes=("out.json",), parent_baseline={}, workspace_baseline={},
    )
    runtime = worker_workspace.provision_worker_mcp_runtime(
        workspace, request_id="req1", task_id="TASK_B833", runner="claude_b833",
        topic="task_mcp", backend="bubblewrap", source_graph_targets=[],
        session_topic="AIWorkHub dynamic worker MCP B833",
    )
    assert runtime.env[w.ENV_REPO] == worker_workspace.SANDBOX_WORKSPACE
    assert runtime.env[w.ENV_AUTHORITY_REPO] == worker_workspace.SANDBOX_AUTHORITY_REPO
    assert str(repo) not in runtime.env[w.ENV_REPO]
    assert str(repo) not in runtime.env[w.ENV_AUTHORITY_REPO]


# ---------------------------------------------------------------------------
# Adapter injection: Claude --mcp-config, Copilot --additional-mcp-config,
# Codex left unchanged (it uses the isolated CODEX_HOME config.toml instead)
# ---------------------------------------------------------------------------

def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "mcp_config.json"
    path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    return path


def _launchable_plan(adapter_id: str, cwd: Path) -> runtime_adapters.RuntimeAdapterPlan:
    # Constructed directly (bypassing executable PATH resolution, which needs
    # a real +x bit this sandboxed worker session cannot chmod-set) so this
    # test exercises exactly what inject_worker_mcp_config itself does with
    # an already-validated, launchable plan.
    return runtime_adapters.RuntimeAdapterPlan(
        adapter_id=adapter_id, argv=[f"/usr/bin/{adapter_id}", "-p", "hello"], cwd=str(cwd),
        executable=f"/usr/bin/{adapter_id}", launchable=True, manual_only=False,
        validation_ok=True, validation_reason="",
    )


def test_inject_worker_mcp_config_claude_gets_strict_mcp_config_flag(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = _launchable_plan("claude_cli", repo)
    config = _config_file(tmp_path)
    injected = runtime_adapters.inject_worker_mcp_config(plan, config)
    assert injected.argv[-3:] == ["--mcp-config", str(config.resolve()), "--strict-mcp-config"]
    assert injected.argv[: len(plan.argv)] == plan.argv


def test_inject_worker_mcp_config_copilot_gets_additional_mcp_config_flag(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = _launchable_plan(runtime_adapters.DEEPSEEK_COPILOT_ADAPTER, repo)
    config = _config_file(tmp_path)
    injected = runtime_adapters.inject_worker_mcp_config(plan, config)
    assert injected.argv[-2:] == ["--additional-mcp-config", f"@{config.resolve()}"]

    glm_plan = _launchable_plan(runtime_adapters.GLM_COPILOT_ADAPTER, repo)
    glm_injected = runtime_adapters.inject_worker_mcp_config(glm_plan, config)
    assert glm_injected.argv[-2:] == ["--additional-mcp-config", f"@{config.resolve()}"]


def test_inject_worker_mcp_config_codex_argv_unchanged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = _launchable_plan("codex_cli", repo)
    config = _config_file(tmp_path)
    injected = runtime_adapters.inject_worker_mcp_config(plan, config)
    assert injected.argv == plan.argv  # Codex uses $HOME/.codex/config.toml instead
    assert injected is plan


def test_inject_worker_mcp_config_is_a_noop_for_non_launchable_or_manual_only_plan(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _config_file(tmp_path)

    manual_plan = runtime_adapters.build_runtime_command("deepseek_manual", "hello", repo)
    assert runtime_adapters.inject_worker_mcp_config(manual_plan, config) is manual_plan

    rejected_plan = runtime_adapters.build_runtime_command("codex_cli", "", repo)
    assert rejected_plan.launchable is False
    assert runtime_adapters.inject_worker_mcp_config(rejected_plan, config) is rejected_plan


def test_inject_worker_mcp_config_rejects_launch_when_config_path_missing(tmp_path: Path) -> None:
    """B834: a launchable adapter that requires the worker MCP surface must
    reject the launch (raise) rather than silently launching without tools
    when its generated config is missing -- the B833 candidate's silent
    no-op here was the "generated config is mandatory" defect."""
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = _launchable_plan("claude_cli", repo)
    with pytest.raises(ValueError):
        runtime_adapters.inject_worker_mcp_config(plan, tmp_path / "does_not_exist.json")


# ---------------------------------------------------------------------------
# Completion gate: non-trivial code tasks require >=1 live source_graph call;
# data-classification tasks are exempt
# ---------------------------------------------------------------------------

def _gate_metadata(*, task_type: str, ledger: Path | None, key: Path | None) -> dict:
    return {
        "task_id": "TASK_B833", "runner": "claude_b833", "topic": "task_mcp",
        "project_context": {"task_context_policy": {"task_type": task_type}},
        "worker_mcp": (
            {"audit_ledger_path": str(ledger), "audit_hmac_key_path": str(key)}
            if ledger is not None else {}
        ),
    }


def test_live_call_gate_blocks_code_task_with_no_recorded_call(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    key = tmp_path / "key"
    ledger.write_text("", encoding="utf-8")
    key.write_bytes(b"k" * 32)
    gate = process_launcher._worker_mcp_live_call_gate(
        _gate_metadata(task_type="code", ledger=ledger, key=key), "req1",
    )
    assert gate["gated"] is True
    assert gate["satisfied"] is False


def test_live_call_gate_blocks_code_task_when_runtime_never_provisioned(tmp_path: Path) -> None:
    gate = process_launcher._worker_mcp_live_call_gate(
        _gate_metadata(task_type="code", ledger=None, key=None), "req1",
    )
    assert gate["gated"] is True
    assert gate["satisfied"] is False
    assert gate["reason"] == "worker_mcp_runtime_not_provisioned"


def test_live_call_gate_passes_code_task_with_one_verified_live_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo, home=tmp_path / "home", targets=("AITools/source_graph.py",), request_id="req1")
    w.source_graph_query(ctx, mode="focus", query="ignored")
    gate = process_launcher._worker_mcp_live_call_gate(
        _gate_metadata(task_type="code", ledger=ctx.audit_ledger_path, key=ctx.audit_hmac_key_path),
        "req1",
    )
    assert gate["satisfied"] is True
    assert gate["verification"]["live_source_graph_calls"] == 1


def test_live_call_gate_exempts_data_classification_tasks(tmp_path: Path) -> None:
    gate = process_launcher._worker_mcp_live_call_gate(
        _gate_metadata(task_type="data_classification", ledger=None, key=None), "req1",
    )
    assert gate["gated"] is False
    assert gate["satisfied"] is True


def test_live_call_gate_is_a_noop_without_a_project_context_contract(tmp_path: Path) -> None:
    gate = process_launcher._worker_mcp_live_call_gate({"task_id": "T", "runner": "r", "topic": "t"}, "req1")
    assert gate["gated"] is False
    assert gate["satisfied"] is True


def test_live_call_gate_telemetry_never_leaks_ledger_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo, home=tmp_path / "home", targets=("AITools/source_graph.py",), request_id="req1")
    w.source_graph_query(ctx, mode="focus", query="ignored")
    gate = process_launcher._worker_mcp_live_call_gate(
        _gate_metadata(task_type="code", ledger=ctx.audit_ledger_path, key=ctx.audit_hmac_key_path),
        "req1",
    )
    dumped = json.dumps(gate)
    assert str(ctx.audit_ledger_path) not in dumped
    assert str(ctx.audit_hmac_key_path) not in dumped


# ---------------------------------------------------------------------------
# Cross-module consistency + eval artifact
# ---------------------------------------------------------------------------

def test_worker_mcp_tool_names_match_agent_tool_instructions_reference() -> None:
    assert instr.WORKER_MCP_TOOL_NAMES == w.MCP_TOOL_NAMES


def test_eval_artifact_b833_matches_live_tool_surface() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "eval"
        / "aiworkhub_dynamic_worker_mcp_b833_v1.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_id"] == "aiworkhub.task_mcp.aiworkhub_dynamic_worker_mcp_b833_v1.eval.v1"
    assert set(payload["mcp_tool_names"]) == set(w.MCP_TOOL_NAMES)
    assert payload["server_name"] == w.SERVER_NAME
