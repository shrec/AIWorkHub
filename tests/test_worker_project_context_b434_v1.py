from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _ensure_deepseek_credentials_stub() -> None:
    import importlib
    import types

    try:
        importlib.import_module("aiworkhub.deepseek_credentials")
        return
    except ImportError:
        pass

    stub = types.ModuleType("aiworkhub.deepseek_credentials")

    class CredentialError(Exception):
        def __init__(self, reason: str = "deepseek_credential_stub_environment") -> None:
            super().__init__(reason)
            self.reason = reason

    def load_credential(repo=None):  # noqa: ANN001, ARG001
        raise CredentialError("deepseek_credential_stub_environment")

    stub.CredentialError = CredentialError
    stub.load_credential = load_credential
    sys.modules["aiworkhub.deepseek_credentials"] = stub


_ensure_deepseek_credentials_stub()

from aiworkhub import process_launcher, project_context, runtime_adapters, worker_workspace  # noqa: E402


def _write_tool(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _stub_source_graph_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic in-process Source Graph stub (B849 canonical authority).

    Since B849, ``collect_project_context`` calls
    ``project_context._source_graph_direct`` in-process -- no subprocess,
    no ``AITools/source_graph.py`` dependency. B866/B871 proved the
    accepted fixture pattern: monkeypatch ``project_context._source_graph_direct``
    directly rather than shelling out to a fake script, echoing back the
    contract's mode/query/budget so mode-dependent assertions still hold.
    """

    def fake_direct(repo: Path, contract: dict) -> tuple[str, bool]:
        source = contract["source_graph"]
        mode = source["bundle_type"] if source["mode"] == "bundle" else source["mode"]
        payload = {
            "tool": "source_graph",
            "mode": mode,
            "query": source["query"],
            "budget": source["budget"],
        }
        return json.dumps(payload, sort_keys=True), False

    monkeypatch.setattr(project_context, "_source_graph_direct", fake_direct)


def _stub_worker_tools_direct(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_hit: bool = True,
    memory_hit: bool = False,
    kb_hit: bool = False,
) -> None:
    """Deterministic in-process Session Manager / AI Memory / KB stub
    (B879 canonical authority). Since B878/B879, ``collect_project_context``
    calls ``worker_ai_tools_mcp.session_current_state`` / ``ai_memory_search``
    / ``kb_search`` in-process against the repository's canonical
    ``.aiworkhub`` SQLite authority -- no subprocess, no ``AITools/*.py``
    dependency. Monkeypatching these call sites directly mirrors the
    accepted ``_stub_source_graph_direct`` pattern for Source Graph.
    """

    def fake_session(ctx, *, limit: int = 12):
        evidence = (
            [{"source_id": "evt-1", "timestamp": "2026-01-01T00:00:00Z", "kind": "progress", "snippet": "bounded"}]
            if session_hit else []
        )
        content = json.dumps(
            {"topic": ctx.session_topic, "state": "current" if session_hit else "unknown",
             "evidence_count": len(evidence), "evidence": evidence},
            sort_keys=True,
        )
        return {"ok": True, "content": content, "truncated": False, "hit_count": len(evidence)}

    def fake_memory(ctx, *, query: str, limit: int = 8):
        results = [{"key": "ctx", "value": "bounded", "tags": "task_mcp"}] if memory_hit else []
        content = json.dumps({"results": results, "count": len(results)}, sort_keys=True)
        return {"ok": True, "content": content, "truncated": False, "hit_count": len(results)}

    def fake_kb(ctx, *, query: str, limit: int = 8):
        results = [{"key": "pipeline.stage_order", "title": "x", "category": "module", "tags": "task_mcp", "body": "y"}] if kb_hit else []
        content = json.dumps({"results": results, "count": len(results)}, sort_keys=True)
        return {"ok": True, "content": content, "truncated": False, "hit_count": len(results)}

    monkeypatch.setattr(project_context._worker_tools, "session_current_state", fake_session)
    monkeypatch.setattr(project_context._worker_tools, "ai_memory_search", fake_memory)
    monkeypatch.setattr(project_context._worker_tools, "kb_search", fake_kb)


def _context_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "AITools").mkdir(parents=True)
    _write_tool(
        repo / "AITools/transcript_graph.py",
        """
import json, sys
print(json.dumps({"tool":"transcript_graph","cmd":sys.argv[1],"topic":sys.argv[2],"limit":sys.argv[sys.argv.index("--limit") + 1]}, sort_keys=True))
""",
    )
    _write_tool(
        repo / "AITools/kb.py",
        """
import sys
print("[kb] no results for '%s'" % sys.argv[2])
""",
    )
    (repo / "AITools/source_graph.db").write_bytes(b"x" * 1024)
    (repo / "AITools/session.db").write_bytes(b"y" * 1024)
    return repo


def _project_context_card(mode: str = "focus", *, required: bool = True) -> dict:
    return {
        "task_id": "TASK_CTX",
        "runner": "claude_worker_ctx",
        "topic": "task_mcp",
        "status": "pending",
        "worker_status": "unclaimed",
        "claimed_by": "",
        "allowed_writes": ["out/result.json"],
        "project_context": {
            "required": required,
            "source_graph": {
                "mode": mode,
                "query": "ProcessManager",
                "budget": 32,
                "bundle_type": "explore",
            },
            "session": {"topic": "task-mcp worker isolation", "limit": 3},
            "kb": {"query": "missing B434 context", "limit": 2},
        },
    }


def test_project_context_collects_source_modes_bounded_session_and_optional_kb(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _context_repo(tmp_path)
    _stub_source_graph_direct(monkeypatch)
    _stub_worker_tools_direct(monkeypatch)
    seen = {}
    for mode in ("focus", "slice", "bundle"):
        result = project_context.collect_project_context(repo, _project_context_card(mode))
        assert result is not None
        seen[mode] = result.prompt_bundle
        assert "PROJECT_CONTEXT_BUNDLE" in result.prompt_bundle
        payload = json.loads(result.prompt_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1])
        assert payload["schema_id"] == project_context.SCHEMA_ID
        source_evidence = payload["evidence"]["source_graph"]
        assert source_evidence["mode"] == (
            mode if mode != "bundle" else "explore"
        )
        assert payload["evidence"]["session_current_state"]["state"] == "current"
        assert "--no-refresh" not in result.prompt_bundle
        assert result.metadata["section_count"] == 3
        assert "missing B434 context" not in json.dumps(result.metadata)
        assert "source_graph.db" not in result.prompt_bundle

    assert '"mode":"focus"' in seen["focus"]
    assert '"mode":"slice"' in seen["slice"]
    assert '"mode":"explore"' in seen["bundle"]


def test_project_context_preserves_declared_mode_query_and_slice_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analytics_calls: list[tuple[str, str, int]] = []
    slice_calls: list[tuple[str, int, str | None]] = []

    from aiworkhub import source_graph

    monkeypatch.setattr(
        source_graph,
        "analytics_query",
        lambda _repo, mode, query, budget: (
            analytics_calls.append((mode, query, budget))
            or {"mode": mode, "query": query}
        ),
    )
    monkeypatch.setattr(
        source_graph,
        "slice_",
        lambda _repo, query, budget, target=None: (
            slice_calls.append((query, budget, target))
            or {"mode": "slice", "query": query, "target": target}
        ),
    )

    analytics_card = _project_context_card("complexity")
    analytics_card["project_context"]["source_graph"]["targets"] = ["pkg/wrong.py"]
    analytics_contract = project_context._validate_contract(analytics_card)
    analytics_raw, _ = project_context._source_graph_direct(Path("/repo"), analytics_contract)
    assert json.loads(analytics_raw)["mode"] == "complexity"
    assert analytics_calls == [("complexity", "ProcessManager", 32)]

    slice_card = _project_context_card("slice")
    slice_card["project_context"]["source_graph"]["targets"] = ["pkg.Service.run"]
    slice_contract = project_context._validate_contract(slice_card)
    slice_raw, _ = project_context._source_graph_direct(Path("/repo"), slice_contract)
    assert json.loads(slice_raw)["query"] == "ProcessManager"
    assert slice_calls == [("ProcessManager", 32, "pkg.Service.run")]


def test_project_context_validates_types_and_rejects_shellish_or_overbudget_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _context_repo(tmp_path)
    card = _project_context_card()
    card["project_context"]["source_graph"]["mode"] = "find"
    with pytest.raises(project_context.ProjectContextError, match="mode_invalid"):
        project_context.collect_project_context(repo, card)

    card = _project_context_card()
    card["project_context"]["source_graph"]["budget"] = 10_000
    with pytest.raises(project_context.ProjectContextError, match="budget_out_of_range"):
        project_context.collect_project_context(repo, card)

    _stub_source_graph_direct(monkeypatch)
    _stub_worker_tools_direct(monkeypatch)
    card = _project_context_card()
    card["project_context"]["source_graph"]["query"] = "ProcessManager; rm -rf /"
    result = project_context.collect_project_context(repo, card)
    assert result is not None
    assert "rm -rf" in result.prompt_bundle


def test_large_valid_json_is_canonicalized_into_bounded_valid_preview(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _context_repo(tmp_path)

    def fake_direct(repo_: Path, contract: dict) -> tuple[str, bool]:
        return json.dumps({"rows": ["x" * 1024] * 80}), False

    monkeypatch.setattr(project_context, "_source_graph_direct", fake_direct)
    _stub_worker_tools_direct(monkeypatch)
    result = project_context.collect_project_context(repo, _project_context_card())
    assert result is not None
    payload = json.loads(result.prompt_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1])
    bounded = payload["evidence"]["source_graph"]["data"]
    assert bounded["truncated"] is True
    assert bounded["original_bytes"] > project_context.MAX_TOOL_OUTPUT_BYTES
    assert bounded["original_hit_count"] > 0
    source_metadata = next(
        section
        for section in result.metadata["sections"]
        if section["name"] == "source_graph"
    )
    assert source_metadata["hit_count"] == bounded["original_hit_count"]
    assert result.metadata["bundle_bytes"] <= project_context.MAX_BUNDLE_BYTES


def test_required_context_rejects_before_claim_and_optional_degrades(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    repo = _context_repo(tmp_path)
    (repo / "AITools/transcript_graph.py").unlink()
    claims = []
    monkeypatch.setattr(process_launcher.core, "claim_start_exact", lambda *args: claims.append(args) or {"ok": True})
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=lambda _task_id: {"returncode": 0, "stdout": json.dumps(_project_context_card()), "stderr": ""},
        collision_guard=lambda **_kwargs: {"returncode": 0, "stdout": "{}", "stderr": ""},
        adapter_builder=lambda **_kwargs: SimpleNamespace(argv=[sys.executable, "-c", "pass"], cwd=str(repo), launchable=True, reason=""),
        isolation_enabled=False,
    )
    blocked = manager.launch(
        task_id="TASK_CTX",
        runner="claude_worker_ctx",
        topic="task_mcp",
        adapter_id="claude_cli",
        timeout_seconds=30,
    )
    assert blocked["ok"] is False
    assert "source_graph_query_failed" in blocked["blocked_reason"]
    assert claims == []

    def failing_direct(repo_: Path, contract: dict) -> tuple[str, bool]:
        raise project_context.ProjectContextError("source_graph_query_failed:test_stub")

    monkeypatch.setattr(project_context, "_source_graph_direct", failing_direct)
    optional = _project_context_card(required=False)
    degraded = project_context.collect_project_context(repo, optional)
    assert degraded is not None
    assert degraded.metadata["sections"][0]["degraded_reason"]


def test_identical_bundle_reaches_all_adapter_prompts_and_metadata_is_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _context_repo(tmp_path)
    _stub_source_graph_direct(monkeypatch)
    _stub_worker_tools_direct(monkeypatch)
    result = project_context.collect_project_context(repo, _project_context_card())
    assert result is not None
    prompts = [
        process_launcher.build_worker_prompt(
            task_id="TASK_CTX",
            runner=f"{adapter}_runner",
            topic="task_mcp",
            card=_project_context_card(),
            project_context_bundle=result.prompt_bundle,
        )
        for adapter in ("claude", "codex", "deepseek")
    ]
    bundles = [prompt.split("PROJECT_CONTEXT_BUNDLE:", 1)[1] for prompt in prompts]
    assert bundles[0] == bundles[1] == bundles[2]
    assert result.metadata["bundle_sha256"]
    assert "ProcessManager" not in json.dumps(result.metadata)
    assert "SECRET" not in json.dumps(result.metadata)


def test_worker_prompt_bounds_contract_and_excludes_recursive_persistence_envelopes() -> None:
    recursive_artifact = {
        "card_json": json.dumps(
            {
                "card_json": "MEGABYTE_REWORK_ARTIFACT" * 20_000,
                "provider_input_tokens": 9_999_999,
                "review_packet": "REPEATED_REVIEW_PACKET",
            },
            sort_keys=True,
        ),
        "persistence": {"card_json": "REPEATED_PERSISTENCE_ENVELOPE" * 10_000},
    }
    prompt = process_launcher.build_worker_prompt(
        task_id="TASK_BOUNDED_CARD",
        runner="codex_worker_b434",
        topic="task_mcp",
        card={
            "review_feedback": {
                "instruction": "repair only row 7",
                "card_json": json.dumps(recursive_artifact, sort_keys=True),
            },
            "persistence": recursive_artifact,
            "quality_review": recursive_artifact,
        },
    )
    contract_json = prompt.split("TASK_CONTRACT_JSON:\n", 1)[1].split(
        "\nEND_TASK_CONTRACT_JSON", 1
    )[0]
    contract = json.loads(contract_json)

    assert contract["review_feedback"] == {"instruction": "repair only row 7"}
    assert "card_json" not in prompt
    assert "MEGABYTE_REWORK_ARTIFACT" not in prompt
    assert "REPEATED_REVIEW_PACKET" not in prompt
    assert "provider_input_tokens" not in prompt
    assert len(contract_json.encode("utf-8")) < 1024
    assert len(prompt.encode("utf-8")) < 16_000

    with pytest.raises(ValueError, match="task_contract_too_large"):
        process_launcher.build_worker_prompt(
            task_id="TASK_OVERSIZED_CARD",
            runner="codex_worker_b434",
            topic="task_mcp",
            card={"review_feedback": {"instruction": "x" * (129 * 1024)}},
        )


def test_workspace_creation_does_not_copy_live_context_databases(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = _context_repo(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Task MCP Tests"], cwd=repo, check=True)
    (repo / "out").mkdir()
    (repo / "out/result.json").write_text("baseline", encoding="utf-8")
    subprocess.run(["git", "add", "AITools/transcript_graph.py", "AITools/kb.py", "out/result.json"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    workspace = worker_workspace.create_workspace(
        repo,
        "no-db-copy",
        {"allowed_writes": ["out/result.json"], "read_first": ["AITools/transcript_graph.py"]},
        "claude_cli",
    )
    try:
        assert not (workspace.path / "AITools/source_graph.db").exists()
        assert not (workspace.path / "AITools/session.db").exists()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_codex_inner_sandbox_switches_only_under_outer_confinement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: False)
    repo = tmp_path / "repo"
    repo.mkdir()
    exe = tmp_path / "codex"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _binary: str(exe))

    standalone = runtime_adapters.build_runtime_command("codex_cli", "Prompt", repo)
    assert standalone.argv[standalone.argv.index("-s") + 1] == "workspace-write"

    confined = runtime_adapters.build_runtime_command(
        "codex_cli", "Prompt", repo, outer_sandbox_backend="landlock"
    )
    assert confined.argv[confined.argv.index("-s") + 1] == "danger-full-access"

    monkeypatch.setenv(runtime_adapters.CODEX_INNER_SANDBOX_MODE_ENV, "danger-full-access")
    explicit_override = runtime_adapters.build_runtime_command("codex_cli", "Prompt", repo)
    assert explicit_override.launchable is True
    assert explicit_override.argv[explicit_override.argv.index("-s") + 1] == "danger-full-access"


def test_required_outputs_reject_zero_or_missing_and_accept_valid(tmp_path: Path) -> None:
    workspace = worker_workspace.WorkerWorkspace(
        request_id="required",
        repo=tmp_path,
        path=tmp_path / "worktree",
        home=tmp_path / "home",
        allowed_writes=("out/*.json",),
        parent_baseline={},
        workspace_baseline={},
    )
    (workspace.path / "out").mkdir(parents=True)
    with pytest.raises(worker_workspace.WorkspaceError, match="no_matches"):
        worker_workspace.validate_required_outputs(workspace, ["out/*.json"])
    (workspace.path / "out/empty.json").write_bytes(b"")
    with pytest.raises(worker_workspace.WorkspaceError, match="zero_bytes"):
        worker_workspace.validate_required_outputs(workspace, ["out/*.json"])
    (workspace.path / "out/valid.json").write_text("{}", encoding="utf-8")
    (workspace.path / "out/empty.json").unlink()
    records = worker_workspace.validate_required_outputs(workspace, ["out/*.json"])
    assert records[0]["path"] == "out/valid.json"

    unchanged = worker_workspace.WorkerWorkspace(
        request_id="unchanged",
        repo=tmp_path,
        path=workspace.path,
        home=workspace.home,
        allowed_writes=("out/*.json",),
        parent_baseline={},
        workspace_baseline={"out/valid.json": records[0]["sha256"]},
    )
    with pytest.raises(worker_workspace.WorkspaceError, match="unchanged"):
        worker_workspace.validate_required_outputs(unchanged, ["out/*.json"])


# --- worker_prompt-1 / worker_prompt-2 -------------------------------------


def _zero_hit_focus_payload(query: str, *, budget: int = 48) -> dict:
    """The exact envelope ``source_graph.focus`` returns on a miss.

    The engine restores ``query_tokens`` provenance even when ``matches`` is
    empty; before the fix the section counted those tokens as hits
    (``hit_count == len(query_tokens)`` in 670/680 production bundles).
    """

    from aiworkhub import source_graph

    return {
        "mode": "focus",
        "query": query,
        "budget": budget,
        "matches": [],
        "query_tokens": source_graph._query_tokens(query),
        "candidate_files": [],
        "truncated": False,
    }


def test_json_hit_count_ignores_echoed_query_tokens_and_counts_result_rows() -> None:
    zero_hit = _zero_hit_focus_payload(
        "src/aiworkhub/project_context.py collect_project_context hit_count"
    )
    assert len(zero_hit["query_tokens"]) >= 6
    assert project_context._json_hit_count(zero_hit) == 0

    hit = dict(
        zero_hit,
        matches=[{"qualname": f"pkg.f{index}", "kind": "function"} for index in range(3)],
        candidate_files=["pkg.py"],
    )
    assert project_context._json_hit_count(hit) == 3

    # Other engine shapes: empty containers are zero, rows are counted once.
    assert project_context._json_hit_count(
        {"mode": "file", "query": "x.py", "matches": [], "contexts": [], "candidate_files": []}
    ) == 0
    assert project_context._json_hit_count(
        {"mode": "hotspots", "ranked_symbols": [], "coverage": {"scanned": 9, "eligible": 9}}
    ) == 0
    assert project_context._json_hit_count({"sections": [{"items": [{"file": "a.py"}]}]}) == 2
    assert project_context._json_hit_count({"rows": ["x"] * 80}) == 80
    assert project_context._json_hit_count([{"a": 1}, {"b": 2}]) == 2

    # Opaque shapes stay lenient (one hit when non-empty); empty is empty.
    assert project_context._json_hit_count({"hit": True, "relevant_files": ["a.py"]}) == 1
    assert project_context._json_hit_count({}) == 0

    # A bounded preview reports the count of the payload it previews, and a
    # preview of a zero-hit envelope stays zero.
    preview = {
        "schema_id": "aiworkhub.task_mcp.bounded_json_preview.v1",
        "truncated": True,
        "original_hit_count": 17,
        "preview": {"query_tokens": ["a", "b"], "matches": []},
    }
    assert project_context._json_hit_count(preview) == 17
    padded = dict(zero_hit, padding="x" * (project_context.TOOL_CAPS["source_graph"]["bytes"] + 1))
    bounded, truncated = project_context._canonical_json_output(
        "source_graph", json.dumps(padded), max_bytes=project_context.TOOL_CAPS["source_graph"]["bytes"]
    )
    assert truncated is True
    assert json.loads(bounded)["original_hit_count"] == 0
    assert project_context._json_hit_count(json.loads(bounded)) == 0


def test_zero_hit_focus_envelope_with_echoed_tokens_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _context_repo(tmp_path)
    payload = _zero_hit_focus_payload("ProcessManager launch claim")
    monkeypatch.setattr(
        project_context,
        "_source_graph_direct",
        lambda repo_, contract: (json.dumps(payload, sort_keys=True), False),
    )
    _stub_worker_tools_direct(monkeypatch)
    # No orientation block in this payload, so no exemption applies: the exact
    # historical message is raised and the launcher blocks before any claim.
    with pytest.raises(
        project_context.ProjectContextError, match="^source_graph_required_empty_result$"
    ):
        project_context.collect_project_context(repo, _project_context_card())

    optional = project_context.collect_project_context(repo, _project_context_card(required=False))
    assert optional is not None
    source_meta = optional.metadata["sections"][0]
    assert source_meta["name"] == "source_graph"
    assert source_meta["hit_count"] == 0
    assert source_meta["executed"] is True
    assert source_meta["degraded_reason"] == ""


_SERVICE_ENTITIES = [
    {"kind": "module", "name": "src/pkg/service.py", "qualname": "src/pkg/service.py", "line_start": 1, "line_end": 1, "signature": ""},
    {"kind": "import", "name": "json", "qualname": "json", "line_start": 1, "line_end": 1, "signature": "json"},
    {"kind": "class", "name": "Service", "qualname": "src/pkg/service.py.Service", "line_start": 3, "line_end": 8, "signature": "class Service"},
    {"kind": "method", "name": "run", "qualname": "src/pkg/service.py.Service.run", "line_start": 4, "line_end": 8, "signature": "def run(self)"},
    {"kind": "function", "name": "helper", "qualname": "src/pkg/service.py.helper", "line_start": 10, "line_end": 12, "signature": "def helper()"},
]


def _fake_file_query(indexed: dict[str, dict], calls: list | None = None):
    def fake_file_query(repo_: Path, path: str, budget: int) -> dict:
        if calls is not None:
            calls.append((path, budget))
        row = indexed.get(path)
        if row is None:
            return {
                "mode": "file", "query": path, "budget": budget, "matches": [],
                "contexts": [], "candidate_files": [], "truncated": False,
            }
        return {
            "mode": "file", "query": path, "budget": budget,
            "matches": [{"kind": "file", "name": Path(path).name, "qualname": path, "line_start": 1, "line_end": 1}],
            "contexts": [{
                "file_path": path,
                "file": {"file_path": path, "language": row["language"], "status": "ok", "source_hash": "h" * 64, "indexed_at": "2026-09-08T00:00:00+00:00"},
                "entities": [dict(entity, file_path=path) for entity in row["entities"]],
                "edges": [{"kind": "imports", "src_qualname": path, "dst_name": "json", "line": 1}],
                "found": True,
                "source_preview": "RAW_SOURCE_PREVIEW_MUST_NOT_BE_INJECTED",
                "source_preview_bytes": 40,
                "source_preview_truncated": False,
            }],
            "candidate_files": [path],
            "truncated": False,
        }

    return fake_file_query


def _fake_focus(matches: list[dict], calls: list | None = None):
    """Mirror the engine's CURRENT focus payload, metrics folded into matches.

    The engine used to return a separate ``ranked_symbols`` list that restated
    the match rows (measured at 25-45% of a focus payload). It was folded into
    the rows themselves. A stub that still emitted the old list kept this test
    green while ``_source_graph_direct`` read a key the engine no longer sets,
    so every worker's injected orientation silently lost its priority hints.
    The stub therefore carries the metrics where the engine now carries them.
    """

    def fake_focus(repo_: Path, query: str, budget: int) -> dict:
        if calls is not None:
            calls.append((query, budget))
        payload = {
            "mode": "focus", "query": query, "budget": budget, "matches": list(matches),
            "query_tokens": ["service", "run"], "candidate_files": ["src/pkg/service.py"] if matches else [],
            "truncated": False,
        }
        if matches:
            payload.update({
                "risks": [], "todos": [],
                "related_tests": [{"file_path": "tests/test_service.py", "score": 9, "reasons": ["path_stem_match"]}],
                "recommended_next_steps": ["slice:src/pkg/service.py.Service.run", "context:src/pkg/service.py"],
            })
        return payload

    return fake_focus


_FOCUS_MATCH = {
    "file_path": "src/pkg/service.py", "kind": "method", "name": "run",
    "qualname": "src/pkg/service.py.Service.run", "line_start": 4, "line_end": 8,
    "signature": "def run(self)", "evidence_label": "EXTRACTED", "confidence": 1.0,
    "priority_score": 12, "risk_reasons": ["branch_heavy"],
}


def _orientation_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src/pkg/service.py").write_text("class Service:\n    pass\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs/guide.md").write_text("# guide\n", encoding="utf-8")
    return repo


def test_focus_orientation_previews_derived_targets_and_keeps_focus_only_when_it_hits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aiworkhub import source_graph

    repo = _orientation_repo(tmp_path)
    file_calls: list[tuple[str, int]] = []
    focus_calls: list[tuple[str, int]] = []
    indexed = {
        "src/pkg/service.py": {"language": "python", "entities": _SERVICE_ENTITIES},
        "docs/guide.md": {"language": "documentation", "entities": [
            {"kind": "file", "name": "guide.md", "qualname": "docs/guide.md", "line_start": 1, "line_end": 40, "signature": "bytes=512"},
        ]},
    }
    monkeypatch.setattr(source_graph, "file_query", _fake_file_query(indexed, file_calls))
    monkeypatch.setattr(source_graph, "focus", _fake_focus([_FOCUS_MATCH], focus_calls))
    monkeypatch.setattr(
        source_graph, "analytics_query",
        lambda *args, **kwargs: pytest.fail("no directory target in this card"),
    )

    card = _project_context_card("focus")
    card["read_first"] = ["src/pkg/service.py", "docs/guide.md"]
    card["allowed_writes"] = ["src/pkg/service.py", "src/pkg/new_module.py"]
    contract = project_context._validate_contract(card)
    assert contract["source_graph"]["targets_origin"] == "derived"

    text, truncated = project_context._source_graph_direct(repo, contract)
    payload = json.loads(text)

    # One bounded ``file`` preview per derived target, in derivation order, and
    # the manager's free text runs exactly once as the second slice.
    assert [path for path, _budget in file_calls] == [
        "src/pkg/service.py", "docs/guide.md", "src/pkg/new_module.py"
    ]
    assert all(budget >= 2 * project_context.ORIENTATION_ENTITIES_PER_TARGET_MAX for _p, budget in file_calls)
    assert focus_calls == [("ProcessManager", 32)]

    assert payload["mode"] == "focus" and payload["query"] == "ProcessManager"
    assert payload["orientation"] == {
        "schema_id": project_context.SOURCE_GRAPH_ORIENTATION_SCHEMA_ID,
        "targets_origin": "derived",
        "targets": 3,
        "indexed": 2,
        "unindexed": 1,
        "unindexed_on_disk": 0,
        "focus_hit_count": 1,
    }
    service, guide = payload["files"]
    assert service["target"] == "src/pkg/service.py" and service["language"] == "python"
    # Module and import rows are not definitions; the rest keep their signature.
    assert [entity["kind"] for entity in service["entities"]] == ["class", "method", "function"]
    assert service["entities"][0] == {
        "kind": "class", "name": "Service", "line_start": 3, "line_end": 8, "signature": "class Service",
    }
    assert guide["target"] == "docs/guide.md" and guide["entities"][0]["kind"] == "file"
    assert "RAW_SOURCE_PREVIEW" not in text and "edges" not in service
    assert payload["unindexed_targets"] == ["src/pkg/new_module.py"]
    assert payload["matches"] == [
        {"qualname": "src/pkg/service.py.Service.run", "kind": "method", "line_start": 4, "line_end": 8}
    ]
    assert payload["ranked_symbols"] == [
        {"qualname": "src/pkg/service.py.Service.run", "priority_score": 12, "risk_reasons": ["branch_heavy"]}
    ]
    assert payload["related_tests"] == ["tests/test_service.py"]
    assert payload["recommended_next_steps"] == [
        "slice:src/pkg/service.py.Service.run", "context:src/pkg/service.py"
    ]
    assert payload["query_tokens"] == ["service", "run"]
    assert payload["truncated"] is False and truncated is False
    # files 2 + entities 3 + 1 + focus match 1 + ranked symbol 1
    assert project_context._json_hit_count(payload) == 8
    assert len(text.encode("utf-8")) <= project_context.SOURCE_GRAPH_ORIENTATION_BYTES

    # A free-text miss keeps the deterministic target previews and carries no
    # empty focus slice, only the measured miss.
    monkeypatch.setattr(source_graph, "focus", _fake_focus([]))
    text, _ = project_context._source_graph_direct(repo, contract)
    payload = json.loads(text)
    assert payload["matches"] == [] and payload["candidate_files"] == []
    assert not any(key in payload for key in ("ranked_symbols", "related_tests", "recommended_next_steps"))
    assert payload["orientation"]["focus_hit_count"] == 0
    assert "empty_reason" not in payload["orientation"]
    assert [row["target"] for row in payload["files"]] == ["src/pkg/service.py", "docs/guide.md"]
    assert project_context._json_hit_count(payload) == 6


def test_focus_orientation_is_fitted_under_the_worker_focus_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aiworkhub import source_graph

    repo = _orientation_repo(tmp_path)
    targets = [f"src/pkg/module_{index:02d}.py" for index in range(project_context.MAX_TARGETS + 3)]
    wide_entities = [
        {
            "kind": "function", "name": f"function_{index:02d}",
            "qualname": f"pkg.function_{index:02d}", "line_start": index * 10, "line_end": index * 10 + 9,
            "signature": f"def function_{index:02d}(" + ", ".join(f"argument_{n}" for n in range(9)) + ")",
        }
        for index in range(16)
    ]
    indexed = {target: {"language": "python", "entities": wide_entities} for target in targets}
    monkeypatch.setattr(source_graph, "file_query", _fake_file_query(indexed))
    wide_matches = [dict(_FOCUS_MATCH, qualname=f"src/pkg/service.py.Service.run_{index}") for index in range(24)]
    monkeypatch.setattr(source_graph, "focus", _fake_focus(wide_matches))

    card = _project_context_card("focus")
    card["read_first"] = targets
    card["allowed_writes"] = []
    contract = project_context._validate_contract(card)
    assert len(contract["source_graph"]["targets"]) == project_context.MAX_TARGETS

    text, _ = project_context._source_graph_direct(repo, contract)
    payload = json.loads(text)
    assert len(text.encode("utf-8")) <= project_context.SOURCE_GRAPH_ORIENTATION_BYTES
    assert payload["truncated"] is True
    assert payload["orientation"]["targets"] == project_context.MAX_TARGETS
    assert payload["orientation"]["indexed"] == project_context.MAX_TARGETS
    assert len(payload["files"]) + int(payload["orientation"].get("omitted_targets") or 0) == project_context.MAX_TARGETS
    for row in payload["files"]:
        assert len(row["entities"]) >= min(
            project_context.ORIENTATION_ENTITIES_PER_TARGET_MIN, 16
        ) or row["entities_omitted"] == 16
        assert len(row["entities"]) + int(row.get("entities_omitted") or 0) == 16
        # Definitions survive; the trailing rows are what was cut.
        assert [entity["name"] for entity in row["entities"]] == [
            f"function_{index:02d}" for index in range(len(row["entities"]))
        ]
    assert len(payload["matches"]) <= project_context.ORIENTATION_FOCUS_MATCHES
    # Deterministic: the same inputs fit to the identical payload.
    again, _ = project_context._source_graph_direct(repo, contract)
    assert again == text


def test_focus_orientation_directory_target_uses_scoped_symbols(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aiworkhub import source_graph

    repo = _orientation_repo(tmp_path)
    analytics_calls: list[tuple] = []

    def fake_analytics(repo_: Path, mode: str, query: str, budget: int, *, target=None, cursor=None) -> dict:
        analytics_calls.append((mode, query, budget, target))
        return {
            "mode": mode, "query": query, "budget": budget, "target": target, "scope": "target",
            "symbols": [
                {"file_path": "src/pkg/service.py", "kind": "method", "name": "run",
                 "qualname": "src/pkg/service.py.Service.run", "line_start": 4, "line_end": 8,
                 "signature": "def run(self)", "priority_score": 12},
                {"file_path": "src/pkg/other.py", "kind": "function", "name": "other",
                 "qualname": "src/pkg/other.py.other", "line_start": 1, "line_end": 3,
                 "signature": "def other()", "priority_score": 3},
            ],
            "coverage": {"scanned": 2, "eligible": 57, "returned": 2, "requested_budget": budget, "effective_budget": 2},
            "truncated": False,
        }

    monkeypatch.setattr(source_graph, "file_query", _fake_file_query({}))
    monkeypatch.setattr(source_graph, "focus", _fake_focus([]))
    monkeypatch.setattr(source_graph, "analytics_query", fake_analytics)

    card = _project_context_card("focus")
    card["read_first"] = ["src/pkg", "docs/missing_dir"]
    card["allowed_writes"] = []
    contract = project_context._validate_contract(card)
    text, _ = project_context._source_graph_direct(repo, contract)
    payload = json.loads(text)

    # Only the directory that exists on disk is previewed through ``symbols``,
    # scoped to that directory and bounded; a missing path is just unindexed.
    assert analytics_calls == [("symbols", "ProcessManager", project_context.ORIENTATION_DIRECTORY_SYMBOLS, "src/pkg")]
    assert payload["files"] == [{
        "target": "src/pkg",
        "kind": "directory",
        "symbols": [
            {"qualname": "src/pkg/service.py.Service.run", "kind": "method", "line_start": 4, "line_end": 8},
            {"qualname": "src/pkg/other.py.other", "kind": "function", "line_start": 1, "line_end": 3},
        ],
        "symbol_count": 57,
    }]
    assert payload["unindexed_targets"] == ["docs/missing_dir"]
    assert payload["orientation"]["indexed"] == 1 and payload["orientation"]["unindexed_on_disk"] == 0
    assert project_context._json_hit_count(payload) == 3


def test_empty_orientation_fails_closed_unless_the_card_names_nothing_indexable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aiworkhub import source_graph

    repo = _orientation_repo(tmp_path)
    # The index knows none of these paths and the free text misses everywhere.
    monkeypatch.setattr(source_graph, "file_query", _fake_file_query({}))
    monkeypatch.setattr(source_graph, "focus", _fake_focus([]))
    monkeypatch.setattr(
        source_graph, "analytics_query",
        lambda *args, **kwargs: {"mode": "symbols", "symbols": [], "coverage": {"eligible": 0}},
    )
    _stub_worker_tools_direct(monkeypatch)

    def card_with(*, read_first: list[str], allowed_writes: list[str], source_required: bool | None = None) -> dict:
        card = _project_context_card("focus")
        card["read_first"] = read_first
        card["allowed_writes"] = allowed_writes
        if source_required is not None:
            card["project_context"]["source_graph"]["required"] = source_required
        return card

    # (a) Only a path that does not exist yet: nothing Source Graph could hold,
    # so the launch proceeds with a zero-hit section that says exactly that.
    new_only = project_context.collect_project_context(
        repo, card_with(read_first=[], allowed_writes=["src/pkg/new_module.py"])
    )
    assert new_only is not None
    section = new_only.metadata["sections"][0]
    assert section["name"] == "source_graph" and section["hit_count"] == 0
    assert section["executed"] is True and section["degraded_reason"] == ""
    assert new_only.metadata["source_graph_orientation"] == {
        "schema_id": project_context.SOURCE_GRAPH_ORIENTATION_SCHEMA_ID,
        "targets_origin": "derived",
        "targets": 1, "indexed": 0, "unindexed": 1, "unindexed_on_disk": 0,
        "focus_hit_count": 0, "empty_reason": "targets_not_on_disk",
    }
    prompt = json.loads(new_only.prompt_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1])
    assert prompt["evidence"]["source_graph"]["orientation"]["empty_reason"] == "targets_not_on_disk"
    assert prompt["evidence"]["source_graph"]["unindexed_targets"] == ["src/pkg/new_module.py"]
    assert "src/pkg/new_module.py" not in json.dumps(new_only.metadata)

    # (c) No derivable target at all (globs only) is the same truthful zero.
    globs_only = project_context.collect_project_context(
        repo, card_with(read_first=[], allowed_writes=["out/*.json"])
    )
    assert globs_only is not None
    assert globs_only.metadata["source_graph_orientation"]["empty_reason"] == "no_targets"
    assert globs_only.metadata["sections"][0]["hit_count"] == 0

    # (b) A file that exists on disk but is unknown to the index is a stale or
    # incomplete graph for the card's own file: the launch stays blocked.
    with pytest.raises(
        project_context.ProjectContextError,
        match="^source_graph_required_empty_result:targets_unindexed$",
    ):
        project_context.collect_project_context(
            repo, card_with(read_first=["src/pkg/service.py"], allowed_writes=["src/pkg/new_module.py"])
        )

    # (d) The contract can still declare Source Graph non-gating for the card.
    optional = project_context.collect_project_context(
        repo,
        card_with(read_first=["src/pkg/service.py"], allowed_writes=[], source_required=False),
    )
    assert optional is not None
    assert optional.metadata["sections"][0]["hit_count"] == 0
    assert optional.metadata["source_graph_orientation"]["empty_reason"] == "targets_unindexed"
    assert optional.metadata["source_graph_orientation"]["unindexed_on_disk"] == 1


def test_zero_hit_session_state_is_suppressed_from_prompt_but_credited_in_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _context_repo(tmp_path)
    _stub_source_graph_direct(monkeypatch)
    _stub_worker_tools_direct(monkeypatch, session_hit=False)
    result = project_context.collect_project_context(repo, _project_context_card())
    assert result is not None
    payload = json.loads(result.prompt_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1])
    assert set(payload["evidence"]) == {"source_graph"}
    session = next(s for s in result.metadata["sections"] if s["name"] == "session_current_state")
    assert session["requested"] is True
    assert session["executed"] is True
    assert session["hit_count"] == 0
    assert session["degraded_reason"] == ""
    assert session["bytes"] == 0
    assert session["sha256"] == project_context._sha256_text("")
    # session_current_state and the zero-hit optional kb section.
    assert result.metadata["optimization"]["zero_hit_suppression_count"] == 2
    assert result.metadata["optimization"]["suppressed_bytes"] > 0

    # A degraded Session Manager is not a clean zero: the reason survives
    # suppression so the gate still demands a live recovery call.
    monkeypatch.setattr(
        project_context._worker_tools,
        "session_current_state",
        lambda ctx, *, limit=12: {"ok": False, "reason": "session_store_unavailable"},
    )
    degraded = project_context.collect_project_context(repo, _project_context_card(required=False))
    assert degraded is not None
    section = next(s for s in degraded.metadata["sections"] if s["name"] == "session_current_state")
    assert section["hit_count"] == 0
    assert section["degraded_reason"] == (
        "context_tool_failed:session_current_state:session_store_unavailable"
    )
