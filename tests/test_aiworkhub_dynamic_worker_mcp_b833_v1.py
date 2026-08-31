"""B833: dynamic, task-scoped worker MCP tool loop.

Covers: MCP tool registration/bounds, repository/task identity rejection,
the three adapter config injection shapes (Claude --mcp-config, Codex
isolated CODEX_HOME config.toml, Copilot --additional-mcp-config), sandbox
visibility (runtime files scoped under the isolated home), no credential
leakage, the source-call completion gate (and its data-task exemption),
telemetry accounting, and a fake-worker end-to-end dynamic tool call.
"""

from __future__ import annotations

import base64
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

    ``os.fchmod`` is POSIX-only -- guard the patch so the same helper runs
    unchanged on Windows where the attribute is absent.
    """
    monkeypatch.setattr(os, "chmod", lambda *args, **kwargs: None)
    if hasattr(os, "fchmod"):
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
    monkeypatch.setattr(
        source_graph_mod,
        "slice_",
        lambda repo_root, query, budget=64, target=None: _payload(
            "slice", target or query, budget
        ),
    )
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
    # Source Graph reads are query-only and fail closed unless the canonical
    # database has a verified schema and generation identity.  Build the real
    # minimal fixture instead of the old non-SQLite placeholder.
    source_graph_mod.build_index(repo)
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
    provider_call_id: str | None = None,
    provenance: str | None = None,
) -> w.WorkerToolContext:
    authority_repo = authority_repo if authority_repo is not None else repo
    runtime = w.generate_worker_mcp_runtime(
        home=home, request_id=request_id, task_id="TASK_B833", runner="claude_b833",
        topic="task_mcp", repo=repo, authority_repo=authority_repo,
        source_graph_targets=list(targets), session_topic="AIWorkHub dynamic worker MCP B833",
        package_import_root=w.resolve_host_package_import_root(),
        provider_call_id=provider_call_id,
        provenance=provenance,
    )
    return w.WorkerToolContext(
        task_id="TASK_B833", runner="claude_b833", topic="task_mcp", request_id=request_id,
        repo=repo, authority_repo=authority_repo, source_graph_targets=targets,
        session_topic="AIWorkHub dynamic worker MCP B833",
        audit_ledger_path=runtime.audit_ledger_path, audit_hmac_key_path=runtime.audit_hmac_key_path,
        provider_call_id=provider_call_id or "",
        provenance=provenance or "",
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


def test_load_context_from_env_binds_bounded_provider_identity(tmp_path: Path) -> None:
    base = {
        w.ENV_TASK_ID: "TASK_B833", w.ENV_RUNNER: "claude_b833", w.ENV_TOPIC: "task_mcp",
        w.ENV_REPO: str(tmp_path), w.ENV_AUTHORITY_REPO: str(tmp_path),
        w.ENV_SESSION_TOPIC: "custom-topic",
        w.ENV_PROVIDER_CALL_ID: "pci_deepseek_42",
        w.ENV_PROVENANCE: "live",
    }
    ctx = w.load_context_from_env(base)
    assert ctx.provider_call_id == "pci_deepseek_42"
    assert ctx.provenance == "live"

    # Absent optional identity binds to empty strings (never spoofed).
    minimal = {k: v for k, v in base.items() if k not in (w.ENV_PROVIDER_CALL_ID, w.ENV_PROVENANCE)}
    ctx_min = w.load_context_from_env(minimal)
    assert ctx_min.provider_call_id == ""
    assert ctx_min.provenance == ""


def test_load_context_from_env_rejects_spoofed_provider_identity(tmp_path: Path) -> None:
    base = {
        w.ENV_TASK_ID: "TASK_B833", w.ENV_RUNNER: "claude_b833", w.ENV_TOPIC: "task_mcp",
        w.ENV_REPO: str(tmp_path), w.ENV_AUTHORITY_REPO: str(tmp_path),
        w.ENV_SESSION_TOPIC: "custom-topic",
    }
    for bad_call_id in ("has space", "x" * 33):
        with pytest.raises(w.WorkerToolError) as exc:
            w.load_context_from_env({**base, w.ENV_PROVIDER_CALL_ID: bad_call_id})
        assert "worker_mcp_provider_call_id" in str(exc.value)
    with pytest.raises(w.WorkerToolError) as exc:
        w.load_context_from_env({**base, w.ENV_PROVENANCE: "spoofed"})
    assert "worker_mcp_provenance" in str(exc.value)


def test_load_context_from_env_rejects_non_string_scalar_identity(tmp_path: Path) -> None:
    base = {
        w.ENV_TASK_ID: "TASK_B833", w.ENV_RUNNER: "claude_b833", w.ENV_TOPIC: "task_mcp",
        w.ENV_REPO: str(tmp_path), w.ENV_AUTHORITY_REPO: str(tmp_path),
        w.ENV_SESSION_TOPIC: "custom-topic",
    }
    # Raw non-string scalars and objects must fail closed with the named
    # malformed/invalid error -- never be str()-coerced into the ledger.
    call_id_negatives = (
        True, False, 0, 12345,
        {"pci": "spoofed"}, ["pci_deepseek_42"], object(),
    )
    for bad_call_id in call_id_negatives:
        with pytest.raises(w.WorkerToolError) as exc:
            w.load_context_from_env({**base, w.ENV_PROVIDER_CALL_ID: bad_call_id})
        assert "worker_mcp_provider_call_id_malformed" in str(exc.value)
    provenance_negatives = (
        True, False, 0, 12345,
        {"p": "live"}, ["live"], object(),
    )
    for bad_provenance in provenance_negatives:
        with pytest.raises(w.WorkerToolError) as exc:
            w.load_context_from_env({**base, w.ENV_PROVENANCE: bad_provenance})
        assert "worker_mcp_provenance_invalid" in str(exc.value)


def test_load_context_from_env_preserves_exact_valid_string_identity(tmp_path: Path) -> None:
    base = {
        w.ENV_TASK_ID: "TASK_B833", w.ENV_RUNNER: "claude_b833", w.ENV_TOPIC: "task_mcp",
        w.ENV_REPO: str(tmp_path), w.ENV_AUTHORITY_REPO: str(tmp_path),
        w.ENV_SESSION_TOPIC: "custom-topic",
    }
    for prov in ("prefetch", "live", "cache"):
        ctx = w.load_context_from_env({**base, w.ENV_PROVENANCE: prov})
        assert ctx.provenance == prov
    for call_id in ("pci_deepseek_42", "codex_abc", "claude_xyz"):
        ctx = w.load_context_from_env({**base, w.ENV_PROVIDER_CALL_ID: call_id})
        assert ctx.provider_call_id == call_id
    # NF389/r6: an ABSENT key binds the empty sentinel (never spoofed); a
    # PRESENT-but-empty key is explicit empty identity and must fail closed
    # with the named error.
    ctx_absent = w.load_context_from_env(base)
    assert ctx_absent.provider_call_id == ""
    assert ctx_absent.provenance == ""
    with pytest.raises(w.WorkerToolError) as exc:
        w.load_context_from_env({**base, w.ENV_PROVIDER_CALL_ID: ""})
    assert "worker_mcp_provider_call_id_empty" in str(exc.value)
    with pytest.raises(w.WorkerToolError) as exc:
        w.load_context_from_env({**base, w.ENV_PROVENANCE: ""})
    assert "worker_mcp_provenance_invalid" in str(exc.value)


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
    invalid_mode = w.source_graph_query(ctx, mode="grep", query="x")
    assert invalid_mode["reason"].startswith("invalid_mode")
    assert invalid_mode["allowed_modes"] == [
        "focus", "slice", "context", "file", "function", "class", "body", "bodygrep",
        "impact", "trace", "deps", "bundle",
        "tags", "hotspots", "coverage", "churn", "reviewqueue", "ownership",
        "testmap", "calls", "symbols", "bottlenecks", "auditmap", "complexity",
        "stats", "summarize", "pipeline",
        "todo", "leaks", "nullrisks", "rawptrs", "casts", "crashes",
        "looprisks", "deadmethods", "duplicates", "gaps",
    ]
    assert invalid_mode["example"]["mode"] == "focus"
    invalid_bundle = w.source_graph_query(ctx, mode="focus", query="x", bundle_type="hack")
    assert invalid_bundle["reason"].startswith("invalid_bundle_type")
    assert invalid_bundle["allowed_bundle_types"] == [
        "bugfix", "feature", "refactor", "audit", "optimize", "explore",
    ]
    invalid_stage = w.source_graph_query(
        ctx, mode="focus", query="x", workflow_stage="guessing"
    )
    assert invalid_stage["reason"].startswith("invalid_workflow_stage")
    assert invalid_stage["allowed_workflow_stages"] == [
        "orientation", "implementation", "validation", "review", "rework", "unspecified",
    ]


# ---------------------------------------------------------------------------
# Bounded tool calls + caching + audit accounting
# ---------------------------------------------------------------------------

def test_source_graph_query_runs_bounded_and_second_call_is_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(
        monkeypatch,
        relevant_files=tuple(
            f"src/large_module_{index:03d}.py" for index in range(80)
        ),
    )
    ctx = _ctx(repo, home=tmp_path / "home", targets=("AITools/source_graph.py",))

    first = w.source_graph_query(
        ctx, mode="focus", query="ignored", budget=32, workflow_stage="orientation"
    )
    assert first["ok"] is True
    assert first["hit_count"] > 0
    assert first["cache_hit"] is False
    assert first["index_revision"] == source_graph_mod.BUILD_REVISION
    assert first["evidence_counts"] == {
        "entity_rows": 0, "edge_rows": 0, "file_rows": 0,
    }

    second = w.source_graph_query(
        ctx, mode="focus", query="ignored", budget=32, workflow_stage="validation"
    )
    assert second["cache_hit"] is True
    assert second["cache_receipt"] is True
    assert second["content_sha256"] == first["content_sha256"]
    assert second["content"] != first["content"]
    receipt = json.loads(second["content"])
    assert receipt["reuse_previous_result"] is True
    assert receipt["content_sha256"] == first["content_sha256"]
    assert second["bytes"] < first["bytes"]
    assert second["replay_bytes_avoided"] == first["bytes"] - second["bytes"]
    assert second["provider_tokens_saved"] is None
    assert second["provider_token_savings_measured"] is False

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic,
    )
    assert verification["call_count_by_tool"]["source_graph"] == 2
    assert verification["cache_hits"] == 1
    assert verification["compact_replay"] == {
        "receipt_count": 1,
        "original_bytes": first["bytes"],
        "returned_bytes": second["bytes"],
        "bytes_avoided": first["bytes"] - second["bytes"],
        "provider_tokens_saved": None,
        "provider_token_savings_measured": False,
    }
    # B834: a cache hit must NOT count toward the live-call gate -- only the
    # first, genuinely fresh call does.
    assert verification["live_source_graph_calls"] == 1
    assert verification["source_graph_hit_count"] == first["hit_count"] * 2
    assert verification["source_graph_zero_hit_calls"] == 0
    assert verification["source_graph_failed_calls"] == 0
    # NF389/r6: the total counters aggregate BOTH the fresh live row and the
    # cache-replay row (backward-compatible), but the live-scoped counters see
    # only the single provenance=="live" row -- the cache replay is provenance
    # "cache" and never enters them.
    assert verification["provenance_counts"] == {"live": 1, "cache": 1}
    assert verification["live_source_graph_call_count"] == 1
    assert verification["live_source_graph_success_count"] == 1
    assert verification["live_source_graph_hit_count"] == first["hit_count"]
    assert verification["live_source_graph_zero_hit_calls"] == 0
    assert verification["live_source_graph_failed_calls"] == 0
    assert verification["live_source_graph_repeated_query_calls"] == 0
    assert verification["live_source_graph_mode_counts"] == {"focus": 1}
    assert verification["live_source_graph_mode_sequence"] == ["focus"]
    assert len(verification["live_source_graph_query_sequence"]) == 1
    assert verification["live_source_graph_stage_counts"] == {"orientation": 1}
    assert verification["bounded_bytes_returned"] > 0
    assert verification["source_graph_stage_counts"] == {
        "orientation": 1, "validation": 1,
    }
    assert verification["source_graph_stage_attributed_calls"] == 2
    assert verification["source_graph_mode_stage_counts"] == {
        "orientation": {"focus": 1}, "validation": {"focus": 1},
    }
    assert verification["source_graph_latency"]["count"] == 2
    assert verification["source_graph_latency"]["p50_ms"] is not None
    assert verification["source_graph_call_gaps"]["count"] == 1
    assert verification["source_graph_index_revision_counts"] == {
        source_graph_mod.BUILD_REVISION: 2,
    }
    assert len(verification["source_graph_index_sequence"]) == 2
    assert len(verification["source_graph_query_sequence"]) == 2
    assert verification["source_graph_query_sequence"][0] == verification["source_graph_query_sequence"][1]
    assert verification["receipt_conformance"]["status"] == "pass"
    # NF389/r6: tool-discipline is scored over LIVE rows only. The fresh call
    # is the sole live row; the cache replay ("cache" provenance) never enters,
    # so source_graph_calls==1 and repeated_query_calls==0 (no live repeat) and
    # the score carries no repeated-query penalty.
    assert verification["tool_discipline"] == {
        "schema_id": "aiworkhub.tool_discipline.v1",
        "status": "observed",
        "observation_only": True,
        "score": 100.0,
        "source_graph_calls": 1,
        "failed_calls": 0,
        "zero_hit_calls": 0,
        "repeated_query_calls": 0,
        "deps_after_trace": False,
        "query_identity_coverage": {"observed": 1, "expected": 1},
    }


def _live_gate_metadata(ctx: w.WorkerToolContext) -> dict:
    """Production-shaped completion-gate metadata for a ``code`` task whose only
    required tool is Source Graph (no injected bundle, no extra sections)."""
    return {
        "task_id": ctx.task_id,
        "runner": ctx.runner,
        "topic": ctx.topic,
        "worker_mcp": {
            "audit_ledger_path": str(ctx.audit_ledger_path),
            "audit_hmac_key_path": str(ctx.audit_hmac_key_path),
        },
        "project_context": {
            "task_context_policy": {"task_type": "code"},
            "sections": [],
        },
    }


def test_prefetch_provenance_is_auditable_but_never_satisfies_live_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """NF389/r6: a launch-time Source Graph prefetch is auditable but must never
    be credited as a live worker call, and a genuine worker call counts once.

    Exercises the real ``source_graph_query`` -> authenticated ledger ->
    ``verify_audit_ledger`` -> ``_worker_mcp_live_call_gate`` production path
    (not a mocked ledger), on one shared request-scoped runtime.
    """
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    home = tmp_path / "home"

    # Coordinator launch-time prefetch: provenance bound exactly to "prefetch".
    prefetch_ctx = _ctx(
        repo, home=home, targets=("AITools/source_graph.py",),
        request_id="req-nf389", provenance="prefetch",
    )
    prefetch = w.source_graph_query(
        prefetch_ctx, mode="focus", query="prefetch-orientation", budget=32,
        workflow_stage="orientation",
    )
    assert prefetch["ok"] is True
    assert prefetch["cache_hit"] is False

    verification = w.verify_audit_ledger(
        prefetch_ctx.audit_ledger_path, prefetch_ctx.audit_hmac_key_path,
        task_id=prefetch_ctx.task_id, runner=prefetch_ctx.runner,
        topic=prefetch_ctx.topic, request_id=prefetch_ctx.request_id,
    )
    # The prefetch is auditable and authoritative...
    assert verification["provenance_counts"] == {"prefetch": 1}
    assert verification["successful_call_count_by_tool"]["source_graph"] == 1
    assert verification["call_count_by_tool"]["source_graph"] == 1
    # ...but it contributes zero live/fresh Source Graph calls.
    assert verification["live_source_graph_calls"] == 0
    assert verification["fresh_source_graph_calls"] == 0

    # Prefetch alone fails the production live-call gate closed.
    metadata = _live_gate_metadata(prefetch_ctx)
    gate = process_launcher._worker_mcp_live_call_gate(metadata, prefetch_ctx.request_id)
    assert gate["gated"] is True
    assert gate["satisfied"] is False
    assert gate["satisfaction_by_tool"]["source_graph"] == "stale_or_cached"
    assert "source_graph" in gate["stale_tools"]
    assert "source_graph_live_call" not in gate.get("missing_tools", [])

    # A genuine provider-originated worker call on the same identity counts
    # exactly once (the prefetch row is not double-counted as live).
    live_ctx = _ctx(
        repo, home=home, targets=("AITools/source_graph.py",),
        request_id="req-nf389",
    )
    live = w.source_graph_query(
        live_ctx, mode="focus", query="genuine-live-call", budget=32,
        workflow_stage="validation",
    )
    assert live["ok"] is True
    assert live["cache_hit"] is False

    verification2 = w.verify_audit_ledger(
        live_ctx.audit_ledger_path, live_ctx.audit_hmac_key_path,
        task_id=live_ctx.task_id, runner=live_ctx.runner,
        topic=live_ctx.topic, request_id=live_ctx.request_id,
    )
    assert verification2["provenance_counts"] == {"prefetch": 1, "live": 1}
    assert verification2["live_source_graph_calls"] == 1
    assert verification2["fresh_source_graph_calls"] == 1
    assert verification2["successful_call_count_by_tool"]["source_graph"] == 2
    # Core NF-2026-00023 fix: one prefetch + one live call keeps the TOTAL
    # authenticated count at 2 (backward-compatible) and both provenance rows
    # in provenance_counts, but every live-scoped counter and the discipline
    # score report exactly 1 -- the prefetch never inflates them.
    assert verification2["call_count_by_tool"]["source_graph"] == 2
    assert verification2["live_source_graph_call_count"] == 1
    assert verification2["live_source_graph_success_count"] == 1
    assert verification2["tool_discipline"]["source_graph_calls"] == 1
    assert verification2["tool_discipline"]["query_identity_coverage"] == {
        "observed": 1, "expected": 1,
    }

    gate2 = process_launcher._worker_mcp_live_call_gate(metadata, live_ctx.request_id)
    assert gate2["satisfied"] is True
    assert gate2["satisfaction_by_tool"]["source_graph"] == "live_worker_call"
    assert gate2["missing_tools"] == []
    assert gate2["stale_tools"] == []


# ---------------------------------------------------------------------------
# NF171: engine-authoritative analytics scope/cursor -- no wrapper-level
# second filter or hidden cap on top of what analytics_query already decided
# ---------------------------------------------------------------------------

def _real_analytics_repo(tmp_path: Path, *, name: str = "analytics_repo") -> Path:
    """A real, indexed repository (not the stubbed-engine ``_fake_repo``).

    These tests assert that the MCP wrapper forwards ``target``/``cursor``
    into the real ``analytics_query`` engine and does not re-derive scope on
    top of its authoritative result, so they need genuine indexed rows.
    """
    repo = tmp_path / name
    repo.mkdir(parents=True)
    repository_state.bootstrap_repository(repo)
    (repo / "pkg" / "in_scope").mkdir(parents=True)
    (repo / "pkg" / "in_scope" / "mod.py").write_text(
        "def alpha_symbol():\n    return 1\n\n\ndef beta_symbol():\n    return 2\n",
        encoding="utf-8",
    )
    (repo / "pkg" / "other").mkdir(parents=True)
    (repo / "pkg" / "other" / "mod.py").write_text(
        "def gamma_symbol():\n    return 3\n", encoding="utf-8",
    )
    (repo / "pkg" / "many").mkdir(parents=True)
    (repo / "pkg" / "many" / "mod.py").write_text(
        "".join(f"def page_{i:02d}():\n    return {i}\n\n\n" for i in range(9)),
        encoding="utf-8",
    )
    source_graph_mod.build_index(repo, incremental=True)
    return repo


def _analytics_ctx(repo: Path, *, targets: tuple[str, ...] = ()) -> w.WorkerToolContext:
    return w.WorkerToolContext(
        task_id="T-NF171", runner="r", topic="topic", request_id="req-nf171",
        repo=repo, authority_repo=repo, source_graph_targets=targets,
        session_topic="topic", audit_ledger_path=None, audit_hmac_key_path=None,
    )


def test_worker_source_graph_query_analytics_target_is_engine_authoritative(
    tmp_path: Path,
) -> None:
    repo = _real_analytics_repo(tmp_path)
    ctx = _analytics_ctx(repo)

    result = w.source_graph_query(
        ctx, mode="symbols", query="zzz_does_not_match_anything_zzz",
        budget=10, target="pkg/in_scope",
    )
    assert result["ok"] is True
    payload = json.loads(result["content"])
    names = {row["name"] for row in payload["symbols"]}
    assert names == {"alpha_symbol", "beta_symbol"}
    assert "gamma_symbol" not in names
    # The wrapper must not re-run its own generic path-prefix filter on top
    # of the engine's already-scoped payload -- the engine's own coverage
    # accounting is untouched evidence that no second filter pass ran.
    assert payload["coverage"] == {
        "scanned": 2, "eligible": 2, "eligible_capped": False,
        "returned": 2, "requested_budget": 10, "effective_budget": 2,
    }
    assert payload["target"] == "pkg/in_scope"


def test_worker_source_graph_query_analytics_scope_empty_stays_empty(
    tmp_path: Path,
) -> None:
    repo = _real_analytics_repo(tmp_path)
    ctx = _analytics_ctx(repo)

    result = w.source_graph_query(
        ctx, mode="symbols", query="zzz_does_not_match_anything_zzz",
        budget=10, target="pkg/does_not_exist",
    )
    assert result["ok"] is True
    payload = json.loads(result["content"])
    assert payload.get("symbols") is None
    assert payload["scope"] == "target_scope_empty"


def test_worker_source_graph_query_cursor_rejected_for_non_analytic_mode(
    tmp_path: Path,
) -> None:
    repo = _real_analytics_repo(tmp_path)
    ctx = _analytics_ctx(repo)

    result = w.source_graph_query(
        ctx, mode="focus", query="alpha_symbol", cursor="0:deadbeefdeadbeef",
    )
    assert result["ok"] is False
    assert result["reason"] == "cursor_not_supported_for_mode"


def test_worker_source_graph_query_analytics_cursor_roundtrip(tmp_path: Path) -> None:
    # MIN_BUDGET floors every worker-requested budget at 8, so a corpus of
    # exactly 2 rows (the ``pkg/in_scope`` fixture) can never force a second
    # page through this wrapper -- 9 rows under ``pkg/many`` can.
    repo = _real_analytics_repo(tmp_path)
    ctx = _analytics_ctx(repo)

    first = w.source_graph_query(
        ctx, mode="symbols", query="zzz_does_not_match_anything_zzz",
        budget=8, target="pkg/many",
    )
    assert first["ok"] is True
    first_payload = json.loads(first["content"])
    assert len(first_payload["symbols"]) == 8
    next_cursor = first_payload["next_cursor"]
    assert next_cursor is not None

    second = w.source_graph_query(
        ctx, mode="symbols", query="zzz_does_not_match_anything_zzz",
        budget=8, target="pkg/many", cursor=next_cursor,
    )
    assert second["ok"] is True
    second_payload = json.loads(second["content"])
    assert len(second_payload["symbols"]) == 1
    first_names = {row["name"] for row in first_payload["symbols"]}
    assert second_payload["symbols"][0]["name"] not in first_names
    assert second_payload["next_cursor"] is None


def test_source_graph_cache_can_return_full_content_for_internal_evaluation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(
        monkeypatch,
        relevant_files=("src/right.py", "src/other.py"),
    )
    ctx = _ctx(repo, home=tmp_path / "home")

    first = w.source_graph_query(ctx, mode="focus", query="symbol", budget=8)
    replay = w.source_graph_query(
        ctx, mode="focus", query="symbol", budget=8, compact_replay=False,
    )

    assert replay["cache_hit"] is True
    assert replay["cache_receipt"] is False
    assert replay["content"] == first["content"]
    assert replay["bytes"] == first["bytes"]
    assert replay["replay_bytes_avoided"] == 0


def test_source_graph_orientation_truncation_preserves_full_evidence_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    large_payload = {
        "mode": "focus",
        "query": "large orientation payload",
        "matches": [
            {"path": f"src/module_{index}.py", "body": "x" * 1200}
            for index in range(24)
        ],
        "ranked_symbols": [
            {"name": f"symbol_{index}", "score": 100 - index}
            for index in range(12)
        ],
    }
    monkeypatch.setattr(
        source_graph_mod,
        "focus",
        lambda repo_root, query, budget=64: large_payload,
    )
    ctx = _ctx(repo, home=tmp_path / "home")

    page = w.source_graph_query(ctx, mode="focus", query="large", budget=32)
    assert page["ok"] is True
    assert page["internal_truncated"] is False
    assert page["outer_truncated"] is True
    assert page["truncated"] is True
    assert page["output_cap_bytes"] == 8 * 1024
    assert page["hit_count"] == w._json_hit_count(large_payload)
    assert page["evidence_counts"] == w._source_graph_evidence_counts(large_payload)

    cursor_payload = json.loads(
        base64.urlsafe_b64decode(page["continuation_cursor"]).decode("utf-8")
    )
    cursor_payload["page_index"] += 1
    tampered_cursor = base64.urlsafe_b64encode(
        json.dumps(
            cursor_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")
    tampered = w.source_graph_query(
        ctx,
        mode="focus",
        query="large",
        budget=32,
        continuation_cursor=tampered_cursor,
    )
    assert tampered["ok"] is False
    assert tampered["reason"] == "invalid_continuation_cursor"

    pages = [page]
    while page["continuation_cursor"] is not None:
        page = w.source_graph_query(
            ctx,
            mode="focus",
            query="large",
            budget=32,
            continuation_cursor=page["continuation_cursor"],
        )
        assert page["ok"] is True
        pages.append(page)

    assert all(page["bytes"] <= page["output_cap_bytes"] for page in pages)
    assert pages[-1]["continuation_cursor"] is None
    assert all(page["content_encoding"] == "base64" for page in pages)
    content = b"".join(base64.b64decode(page["content"]) for page in pages)
    assert json.loads(content.decode("utf-8")) == large_payload


def test_source_graph_cache_is_invalidated_by_index_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    calls = _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo, home=tmp_path / "home")
    generation = {
        "build_revision": source_graph_mod.BUILD_REVISION,
        "finished_at": "2026-08-01T00:00:00+00:00",
    }
    monkeypatch.setattr(
        w, "_source_graph_index_identity",
        lambda db_path, default_revision: dict(generation),
    )

    first = w.source_graph_query(ctx, mode="focus", query="generation")
    second = w.source_graph_query(ctx, mode="focus", query="generation")
    generation["finished_at"] = "2026-08-01T00:05:00+00:00"
    third = w.source_graph_query(ctx, mode="focus", query="generation")

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert third["cache_hit"] is False
    assert third["index_finished_at"] == "2026-08-01T00:05:00+00:00"
    assert len(calls) == 2


def test_source_graph_identity_prefers_newest_single_file_mutation(tmp_path: Path) -> None:
    db_path = tmp_path / "source_graph.sqlite"
    conn = source_graph_mod.connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('last_build', ?)",
                (json.dumps({
                    "build_revision": "full.v1",
                    "finished_at": "2026-08-01T00:00:00+00:00",
                }),),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('single_file_last_mutation', ?)",
                (json.dumps({
                    "build_revision": "single.v2",
                    "finished_at": "2026-08-01T00:01:00+00:00",
                    "file_path": "src/new.py",
                    "operation": "index",
                }),),
            )
    finally:
        conn.close()

    assert w._source_graph_index_identity(
        db_path, default_revision="default",
    ) == {
        "build_revision": "single.v2",
        "finished_at": "2026-08-01T00:01:00+00:00",
    }


def test_source_graph_identity_reads_legacy_single_file_index_marker(tmp_path: Path) -> None:
    db_path = tmp_path / "source_graph.sqlite"
    conn = source_graph_mod.connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('single_file_last_index', ?)",
                (json.dumps({
                    "build_revision": "legacy.v1",
                    "finished_at": "2026-07-31T23:59:00+00:00",
                    "file_path": "src/legacy.py",
                }),),
            )
    finally:
        conn.close()

    assert w._source_graph_index_identity(
        db_path, default_revision="default",
    ) == {
        "build_revision": "legacy.v1",
        "finished_at": "2026-07-31T23:59:00+00:00",
    }


def test_source_graph_evidence_counts_are_unique_and_structural() -> None:
    payload = {
        "matches": [
            {
                "file_path": "src/a.py", "kind": "function", "qualname": "a.run",
                "line_start": 3,
            },
            {
                "file_path": "src/a.py", "kind": "function", "qualname": "a.run",
                "line_start": 3,
            },
        ],
        "neighbors": [
            {
                "file_path": "src/a.py", "kind": "calls", "src": "a.run",
                "dst_name": "b.stop", "line": 5,
            },
        ],
    }

    assert w._source_graph_evidence_counts(payload) == {
        "entity_rows": 1, "edge_rows": 1, "file_rows": 1,
    }


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


def test_validate_provider_call_id_and_provenance_fail_closed() -> None:
    # Bounded provider_call_id: exactly one bounded, authenticated value.
    assert w.validate_provider_call_id("pci_abcd1234") == "pci_abcd1234"
    assert w.validate_provider_call_id("provider_0123456789abcdef0123456") == \
        "provider_0123456789abcdef0123456"  # exactly MAX_PROVIDER_CALL_ID_LEN (32) chars
    for bad in (
        None, "", "x" * 33, "has space", "-leading", "bad$char", "pci\x00null",
        "pci_abcd1234\n", "pci_abcd1234\r\n", "pci_abcd1234 ",
        123, True, 3.14, ["pci"], {"id": "pci"},
    ):
        with pytest.raises(w.WorkerToolError) as exc:
            w.validate_provider_call_id(bad)
        assert "worker_mcp_provider_call_id" in str(exc.value)

    assert w.validate_provenance("prefetch") == "prefetch"
    assert w.validate_provenance("live") == "live"
    assert w.validate_provenance("cache") == "cache"
    for bad in (None, "", "spoofed", "LIVE", "prefetch;DROP", 123, True, 3.14, ["live"], {"p": "live"}):
        with pytest.raises(w.WorkerToolError) as exc:
            w.validate_provenance(bad)
        assert "worker_mcp_provenance" in str(exc.value)

    # A custom __str__ must never be coerced into a valid provenance label.
    class _StrLive:
        def __str__(self) -> str:
            return "live"

    with pytest.raises(w.WorkerToolError) as exc:
        w.validate_provenance(_StrLive())
    assert "worker_mcp_provenance" in str(exc.value)


def test_audit_rows_carry_bounded_provider_call_id_and_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(
        repo, home=tmp_path / "home", targets=("AITools/source_graph.py",),
        provider_call_id="pci_deepseek_42", provenance="live",
    )
    w.source_graph_query(ctx, mode="focus", query="ignored")

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic,
    )
    assert verification["provider_call_id_by_tool"]["source_graph"] == ["pci_deepseek_42"]
    assert verification["provenance_counts"]["live"] == 1
    assert verification["live_source_graph_calls"] == 1
    # Exact valid rows: the ledger verifies clean, with no identity violations.
    assert verification["ok"] is True
    assert verification["entries_tampered"] == 0
    assert verification["entries_invalid_identity"] == 0


def test_hmac_negative_fixture_distinguishes_invalid_provenance_from_tampering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """NF389/r6: authenticated-invalid identity is NOT an HMAC tamper.

    An entry that carries the correct HMAC but an invalid provenance must fail
    closed as an authenticated identity violation (never counted, never copied
    raw into ``provenance_counts``, never credited as live), while a tampered
    entry (wrong HMAC) is dropped as tampered. The two failures are distinct
    and both stay out of the live Source Graph gate.
    """
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo, home=tmp_path / "home", targets=("AITools/source_graph.py",))
    w.source_graph_query(ctx, mode="focus", query="ignored")

    key_bytes = ctx.audit_hmac_key_path.read_bytes()
    authenticated_invalid = {
        "schema_id": w.AUDIT_ENTRY_SCHEMA_ID, "timestamp": "2026-01-01T00:00:00+00:00",
        "task_id": ctx.task_id, "runner": ctx.runner, "topic": ctx.topic, "request_id": ctx.request_id,
        "tool": "source_graph", "ok": True, "cache_hit": False, "hit_count": 1,
        "bytes_returned": 1, "violation": "",
        "authority_source": "canonical", "authority_state": "sole_authority",
        "authority_repo": str(ctx.authority_repo),
        "provider_call_id": "pci_bad_provenance", "provenance": "spoofed",
    }
    authenticated_invalid["hmac_sha256"] = w._hmac_entry(authenticated_invalid, key_bytes)

    tampered = dict(authenticated_invalid)
    tampered["hmac_sha256"] = "0" * 64

    with ctx.audit_ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(authenticated_invalid) + "\n")
        handle.write(json.dumps(tampered) + "\n")

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic,
    )
    assert verification["ok"] is False
    assert verification["entries_total"] == 3
    assert verification["entries_tampered"] == 1
    assert verification["entries_invalid_identity"] == 1
    assert verification["entries_verified"] == 1  # only the genuine live row
    # The authenticated-invalid row must never appear raw in the provenance map.
    assert "spoofed" not in verification["provenance_counts"]
    assert verification["live_source_graph_calls"] == 1  # only the genuine live row
    assert "authenticated_ledger_identity_violation" in verification["reason"]
    assert "tampered_receipts_observed" not in verification["reason"]


def test_prefetch_and_cache_provenance_never_satisfy_live_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo, home=tmp_path / "home", targets=("AITools/source_graph.py",))
    key_bytes = ctx.audit_hmac_key_path.read_bytes()

    def _entry(provenance: str, cache_hit: bool, pci: str) -> dict:
        entry = {
            "schema_id": w.AUDIT_ENTRY_SCHEMA_ID, "timestamp": "2026-01-01T00:00:00+00:00",
            "task_id": ctx.task_id, "runner": ctx.runner, "topic": ctx.topic, "request_id": ctx.request_id,
            "tool": "source_graph", "ok": True, "cache_hit": cache_hit, "hit_count": 1,
            "bytes_returned": 1, "violation": "",
            "authority_source": "canonical", "authority_state": "sole_authority",
            "authority_repo": str(ctx.authority_repo),
            "provider_call_id": pci, "provenance": provenance,
        }
        entry["hmac_sha256"] = w._hmac_entry(entry, key_bytes)
        return entry

    with ctx.audit_ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_entry("prefetch", False, "pci_prefetch_1")) + "\n")
        handle.write(json.dumps(_entry("cache", True, "pci_cache_1")) + "\n")
        handle.write(json.dumps(_entry("live", False, "pci_live_1")) + "\n")

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic,
    )
    assert verification["entries_verified"] == 3
    assert verification["provenance_counts"] == {"prefetch": 1, "cache": 1, "live": 1}
    # Only the genuine "live" row satisfies the gate; prefetch and cache stay
    # auditable but are never counted as a fresh/live provider call.
    assert verification["fresh_source_graph_calls"] == 1
    assert verification["live_source_graph_calls"] == 1


def _authenticated_source_graph_entry(
    ctx: w.WorkerToolContext,
    key_bytes: bytes,
    *,
    provenance: str | None,
    cache_hit: bool = False,
    ok: bool = True,
    hit_count: int = 1,
    query_sha256: str | None = None,
    mode: str = "focus",
    workflow_stage: str = "orientation",
    pci: str = "pci_valid_1",
) -> dict:
    """Build one HMAC-authenticated source_graph ledger row with a bound
    payload, so a test can exercise the live-scoped counters directly without
    depending on real engine timing."""
    entry = {
        "schema_id": w.AUDIT_ENTRY_SCHEMA_ID, "timestamp": "2026-01-01T00:00:00+00:00",
        "task_id": ctx.task_id, "runner": ctx.runner, "topic": ctx.topic,
        "request_id": ctx.request_id,
        "tool": "source_graph", "ok": ok, "cache_hit": cache_hit,
        "hit_count": hit_count, "bytes_returned": 1, "violation": "",
        "authority_source": "canonical", "authority_state": "sole_authority",
        "authority_repo": str(ctx.authority_repo),
        "provider_call_id": pci,
        "payload": {
            "mode": mode,
            "query_sha256": query_sha256 or ("a" * 64),
            "workflow_stage": workflow_stage,
            # Faithful to the live tool surface: every real ``source_graph_query``
            # ledger row carries the authoritative index revision in its payload
            # (worker_ai_tools_mcp writes ``index_revision`` here). ``receipt_
            # conformance_report`` refuses a fresh source_graph call that lacks a
            # revision, so a synthetic authenticated row must model it too --
            # otherwise a genuine zero-hit live call is wrongly gated as if the
            # index was never touched.
            "index_revision": source_graph_mod.BUILD_REVISION,
        },
    }
    if provenance is not None:
        entry["provenance"] = provenance
    entry["hmac_sha256"] = w._hmac_entry(entry, key_bytes)
    return entry


def test_live_scoped_zero_hit_call_counts_as_one_and_satisfies_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An authenticated provenance=="live" call that returns zero rows is still
    exactly one live call: it counts once in every live-scoped counter and
    satisfies the required-tool gate. Query usefulness stays visible through the
    separate zero-hit counter."""
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo, home=tmp_path / "home", targets=("AITools/source_graph.py",))
    key_bytes = ctx.audit_hmac_key_path.read_bytes()

    with ctx.audit_ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_authenticated_source_graph_entry(
            ctx, key_bytes, provenance="live", hit_count=0,
        )) + "\n")

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic,
    )
    assert verification["provenance_counts"] == {"live": 1}
    assert verification["live_source_graph_call_count"] == 1
    assert verification["live_source_graph_success_count"] == 1
    assert verification["live_source_graph_hit_count"] == 0
    assert verification["live_source_graph_zero_hit_calls"] == 1
    assert verification["live_source_graph_failed_calls"] == 0
    assert verification["live_source_graph_calls"] == 1
    # A zero-hit live call is real tool use, not a failure: the discipline
    # score docks for the empty result but the call is still observed.
    discipline = verification["tool_discipline"]
    assert discipline["source_graph_calls"] == 1
    assert discipline["zero_hit_calls"] == 1
    assert discipline["failed_calls"] == 0

    metadata = _live_gate_metadata(ctx)
    gate = process_launcher._worker_mcp_live_call_gate(metadata, ctx.request_id)
    assert gate["gated"] is True
    assert gate["satisfied"] is True
    assert gate["satisfaction_by_tool"]["source_graph"] == "live_worker_call"


def test_prefetch_and_cache_rows_never_enter_live_scoped_discipline_counters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A prefetch row and a cache-replay row of the SAME query are auditable in
    the total counters but must contribute nothing to any live-scoped counter or
    the discipline score -- only the single live row does."""
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo, home=tmp_path / "home", targets=("AITools/source_graph.py",))
    key_bytes = ctx.audit_hmac_key_path.read_bytes()
    shared_sha = "b" * 64

    with ctx.audit_ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_authenticated_source_graph_entry(
            ctx, key_bytes, provenance="prefetch", query_sha256=shared_sha,
            pci="pci_prefetch_1",
        )) + "\n")
        handle.write(json.dumps(_authenticated_source_graph_entry(
            ctx, key_bytes, provenance="cache", cache_hit=True,
            query_sha256=shared_sha, pci="pci_cache_1",
        )) + "\n")
        handle.write(json.dumps(_authenticated_source_graph_entry(
            ctx, key_bytes, provenance="live", query_sha256=shared_sha,
            pci="pci_live_1",
        )) + "\n")

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic,
    )
    # Totals see all three authenticated rows (backward-compatible).
    assert verification["entries_verified"] == 3
    assert verification["call_count_by_tool"]["source_graph"] == 3
    assert verification["provenance_counts"] == {"prefetch": 1, "cache": 1, "live": 1}
    assert len(verification["source_graph_query_sequence"]) == 3
    # Live-scoped counters see only the one live row -- and because it is the
    # sole live query there is no repeated-query penalty even though the
    # prefetch/cache rows carried the identical query hash.
    assert verification["live_source_graph_call_count"] == 1
    assert verification["live_source_graph_query_sequence"] == [shared_sha]
    assert verification["live_source_graph_repeated_query_calls"] == 0
    assert verification["live_source_graph_calls"] == 1
    assert verification["tool_discipline"]["source_graph_calls"] == 1
    assert verification["tool_discipline"]["repeated_query_calls"] == 0


def test_repeated_live_query_penalizes_only_live_scoped_discipline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Two genuine live rows carrying the same query hash ARE a repeated live
    query and dock the live-scoped discipline score, distinguishing a real live
    repeat from a cache replay of a live query."""
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo, home=tmp_path / "home", targets=("AITools/source_graph.py",))
    key_bytes = ctx.audit_hmac_key_path.read_bytes()
    shared_sha = "c" * 64

    with ctx.audit_ledger_path.open("a", encoding="utf-8") as handle:
        for pci in ("pci_live_a", "pci_live_b"):
            handle.write(json.dumps(_authenticated_source_graph_entry(
                ctx, key_bytes, provenance="live", query_sha256=shared_sha, pci=pci,
            )) + "\n")

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic,
    )
    assert verification["live_source_graph_call_count"] == 2
    assert verification["live_source_graph_repeated_query_calls"] == 1
    discipline = verification["tool_discipline"]
    assert discipline["source_graph_calls"] == 2
    assert discipline["repeated_query_calls"] == 1
    # penalty = min(1, repeated/calls) * 25 = min(1, 1/2) * 25 = 12.5
    assert discipline["score"] == 87.5


def test_invalid_and_tampered_provenance_never_enter_live_scoped_counters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Malformed/spoofed (authenticated-invalid) and tampered (bad HMAC)
    provenance rows are dropped before any aggregation, so neither can seed a
    live-scoped counter. Only the genuine live row survives."""
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo, home=tmp_path / "home", targets=("AITools/source_graph.py",))
    key_bytes = ctx.audit_hmac_key_path.read_bytes()

    spoofed = _authenticated_source_graph_entry(
        ctx, key_bytes, provenance="spoofed", pci="pci_spoofed",
    )
    tampered = _authenticated_source_graph_entry(
        ctx, key_bytes, provenance="live", pci="pci_tampered",
    )
    tampered["hmac_sha256"] = "0" * 64
    unbound = _authenticated_source_graph_entry(
        ctx, key_bytes, provenance=None, pci="pci_unbound",
    )
    genuine = _authenticated_source_graph_entry(
        ctx, key_bytes, provenance="live", pci="pci_genuine",
    )

    with ctx.audit_ledger_path.open("a", encoding="utf-8") as handle:
        for entry in (spoofed, tampered, unbound, genuine):
            handle.write(json.dumps(entry) + "\n")

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic,
    )
    assert verification["ok"] is False  # spoofed row is an identity violation
    assert verification["entries_tampered"] == 1
    assert verification["entries_invalid_identity"] == 1
    # Only the unbound (verified, no provenance) and genuine live rows verify.
    assert verification["entries_verified"] == 2
    assert "spoofed" not in verification["provenance_counts"]
    assert verification["provenance_counts"] == {"live": 1}
    # The unbound row has no "live" provenance, so live-scoped counters see
    # exactly the one genuine live row -- never the spoofed/tampered/unbound.
    assert verification["live_source_graph_call_count"] == 1
    assert verification["live_source_graph_calls"] == 1
    assert verification["tool_discipline"]["source_graph_calls"] == 1


def test_hmac_valid_empty_and_missing_provenance_never_satisfy_live_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """NF389/r6: absent vs empty provenance is the fail-closed boundary.

    A MISSING provenance key is the backward-compatible empty sentinel (the row
    is verified but never satisfies the live gate). A PRESENT-but-empty
    provenance is an authenticated identity violation that fails the whole
    ledger closed; it is never copied into ``provenance_counts`` and never
    credited as live. Only the exact ``"live"`` row satisfies the gate.
    """
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo, home=tmp_path / "home", targets=("AITools/source_graph.py",))
    key_bytes = ctx.audit_hmac_key_path.read_bytes()

    def _entry(provenance_value: object, pci: str) -> dict:
        entry = {
            "schema_id": w.AUDIT_ENTRY_SCHEMA_ID, "timestamp": "2026-01-01T00:00:00+00:00",
            "task_id": ctx.task_id, "runner": ctx.runner, "topic": ctx.topic, "request_id": ctx.request_id,
            "tool": "source_graph", "ok": True, "cache_hit": False, "hit_count": 1,
            "bytes_returned": 1, "violation": "",
            "authority_source": "canonical", "authority_state": "sole_authority",
            "authority_repo": str(ctx.authority_repo),
            "provider_call_id": pci,
        }
        if provenance_value is not None:
            entry["provenance"] = provenance_value
        entry["hmac_sha256"] = w._hmac_entry(entry, key_bytes)
        return entry

    with ctx.audit_ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_entry("", "pci_empty_provenance")) + "\n")
        handle.write(json.dumps(_entry(None, "pci_missing_provenance")) + "\n")
        handle.write(json.dumps(_entry("live", "pci_live_ok")) + "\n")

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic,
    )
    assert verification["ok"] is False
    assert verification["entries_total"] == 3
    assert verification["entries_tampered"] == 0
    assert verification["entries_invalid_identity"] == 1
    assert verification["entries_verified"] == 2  # missing-key + live rows
    assert verification["provenance_counts"] == {"live": 1}
    assert verification["fresh_source_graph_calls"] == 1
    assert verification["live_source_graph_calls"] == 1
    assert "authenticated_ledger_identity_violation" in verification["reason"]


@pytest.mark.parametrize(
    "bad_provider_call_id",
    [
        "",  # present-but-empty
        "x" * (w.MAX_PROVIDER_CALL_ID_LEN + 1),  # oversized
        "pci\x00control",  # control character
        "bad$char",  # malformed punctuation
        "has space",  # malformed whitespace
    ],
)
def test_hmac_valid_invalid_provider_call_id_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_provider_call_id: str,
) -> None:
    """NF389/r6: an HMAC-valid but invalid provider_call_id is an authenticated
    identity violation. The ledger fails closed and the raw value never reaches
    provider_call_ids/provider_call_id_by_tool, provenance_counts, or the live
    Source Graph gate."""
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo, home=tmp_path / "home", targets=("AITools/source_graph.py",))
    key_bytes = ctx.audit_hmac_key_path.read_bytes()

    entry = {
        "schema_id": w.AUDIT_ENTRY_SCHEMA_ID, "timestamp": "2026-01-01T00:00:00+00:00",
        "task_id": ctx.task_id, "runner": ctx.runner, "topic": ctx.topic, "request_id": ctx.request_id,
        "tool": "source_graph", "ok": True, "cache_hit": False, "hit_count": 1,
        "bytes_returned": 1, "violation": "",
        "authority_source": "canonical", "authority_state": "sole_authority",
        "authority_repo": str(ctx.authority_repo),
        "provider_call_id": bad_provider_call_id, "provenance": "live",
    }
    entry["hmac_sha256"] = w._hmac_entry(entry, key_bytes)
    with ctx.audit_ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic,
    )
    assert verification["ok"] is False
    assert verification["entries_total"] == 1
    assert verification["entries_tampered"] == 0
    assert verification["entries_invalid_identity"] == 1
    assert verification["entries_verified"] == 0
    assert verification["provider_call_ids"] == []
    assert verification["provider_call_id_by_tool"] == {}
    assert verification["provenance_counts"] == {}
    assert verification["fresh_source_graph_calls"] == 0
    assert verification["live_source_graph_calls"] == 0
    assert "authenticated_ledger_identity_violation" in verification["reason"]
    if bad_provider_call_id:
        assert bad_provider_call_id not in verification["reason"]


@pytest.mark.parametrize(
    "bad_provenance",
    ["", "spoofed", "LIVE", "prefetch;DROP", "cache\x00"],
)
def test_hmac_valid_invalid_provenance_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_provenance: str,
) -> None:
    """NF389/r6: an HMAC-valid but invalid provenance (empty, spoofed, wrong
    case, or control-bearing) is an authenticated identity violation. The
    ledger fails closed and the raw label never reaches provenance_counts or
    the live Source Graph gate."""
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    _stub_source_graph_engine(monkeypatch)
    ctx = _ctx(repo, home=tmp_path / "home", targets=("AITools/source_graph.py",))
    key_bytes = ctx.audit_hmac_key_path.read_bytes()

    entry = {
        "schema_id": w.AUDIT_ENTRY_SCHEMA_ID, "timestamp": "2026-01-01T00:00:00+00:00",
        "task_id": ctx.task_id, "runner": ctx.runner, "topic": ctx.topic, "request_id": ctx.request_id,
        "tool": "source_graph", "ok": True, "cache_hit": False, "hit_count": 1,
        "bytes_returned": 1, "violation": "",
        "authority_source": "canonical", "authority_state": "sole_authority",
        "authority_repo": str(ctx.authority_repo),
        "provider_call_id": "pci_valid_1", "provenance": bad_provenance,
    }
    entry["hmac_sha256"] = w._hmac_entry(entry, key_bytes)
    with ctx.audit_ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")

    verification = w.verify_audit_ledger(
        ctx.audit_ledger_path, ctx.audit_hmac_key_path,
        task_id=ctx.task_id, runner=ctx.runner, topic=ctx.topic,
    )
    assert verification["ok"] is False
    assert verification["entries_total"] == 1
    assert verification["entries_tampered"] == 0
    assert verification["entries_invalid_identity"] == 1
    assert verification["entries_verified"] == 0
    assert verification["provider_call_ids"] == []
    assert verification["provider_call_id_by_tool"] == {}
    assert verification["provenance_counts"] == {}
    assert verification["fresh_source_graph_calls"] == 0
    assert verification["live_source_graph_calls"] == 0
    assert "authenticated_ledger_identity_violation" in verification["reason"]
    if bad_provenance:
        assert bad_provenance not in verification["reason"]


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

    def tool(self, *, name: str, description: str | None = None):
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


def test_registered_quality_review_packet_read_accepts_no_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mute_chmod(monkeypatch)
    repo = _fake_repo(tmp_path)
    fake_mcp = _FakeMcp()
    w.register_tools(fake_mcp, _ctx(repo, home=tmp_path / "home"))
    tool = fake_mcp.registered["aiworkhub_worker_quality_review_packet_read"]
    assert not inspect.signature(tool).parameters


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
    # TOML escapes backslashes in Windows paths (e.g. ``D:\Dev`` -> ``D:\\Dev``),
    # so the raw text never contains the bare ``str(path)``. Parse the TOML and
    # assert on the deserialised value instead, which is platform-independent.
    import tomllib

    parsed_toml = tomllib.loads(codex_toml)
    assert parsed_toml["mcp_servers"][w.SERVER_NAME]["env"][w.ENV_PYTHONPATH] == str(
        module_file.parents[1]
    )
    assert runtime.codex_config_toml_path == home / ".codex" / "config.toml"

    # No credential leakage: the runtime env never carries a provider secret.
    assert "ANTHROPIC_API_KEY" not in runtime.env
    assert "COPILOT_PROVIDER_API_KEY" not in runtime.env
    # Optional per-call identity (provider_call_id/provenance) is only bound
    # when a non-empty value is supplied; assert presence only for the
    # bootstrap-required bound variables.
    required_bound_vars = tuple(
        name for name in w.BOUND_ENV_VARS
        if name not in (w.ENV_PROVIDER_CALL_ID, w.ENV_PROVENANCE)
    )
    assert all(name in runtime.env for name in required_bound_vars)


def test_generate_worker_mcp_runtime_absent_vs_empty_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """NF389/r6: runtime env generation must distinguish an absent identity
    (backward-compatible unbound sentinel) from an explicit empty value, which
    fails closed with the named error."""
    _mute_chmod(monkeypatch)
    home = tmp_path / "home"
    repo = _fake_repo(tmp_path)
    kwargs = dict(
        home=home, request_id="req42", task_id="TASK_B833", runner="claude_b833",
        topic="task_mcp", repo=repo, authority_repo=repo,
        source_graph_targets=["AITools/source_graph.py"],
        session_topic="AIWorkHub dynamic worker MCP B833",
        package_import_root=w.resolve_host_package_import_root(),
    )
    # Absent identity: the runtime env never binds provider_call_id/provenance.
    runtime_absent = w.generate_worker_mcp_runtime(**kwargs)
    assert w.ENV_PROVIDER_CALL_ID not in runtime_absent.env
    assert w.ENV_PROVENANCE not in runtime_absent.env
    # Present valid identity: bound exactly once into the runtime env.
    runtime_present = w.generate_worker_mcp_runtime(
        **kwargs, provider_call_id="pci_deepseek_42", provenance="prefetch",
    )
    assert runtime_present.env[w.ENV_PROVIDER_CALL_ID] == "pci_deepseek_42"
    assert runtime_present.env[w.ENV_PROVENANCE] == "prefetch"
    # Explicit empty identity/provenance fails closed with the named error.
    with pytest.raises(w.WorkerToolError) as exc:
        w.generate_worker_mcp_runtime(**kwargs, provider_call_id="")
    assert "worker_mcp_provider_call_id_empty" in str(exc.value)
    with pytest.raises(w.WorkerToolError) as exc:
        w.generate_worker_mcp_runtime(**kwargs, provenance="")
    assert "worker_mcp_provenance_invalid" in str(exc.value)


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
