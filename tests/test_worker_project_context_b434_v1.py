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
        assert payload["source_graph"]["mode"] == mode
        source_section = next(
            section for section in payload["sections"]
            if section["name"] == "source_graph"
        )
        assert json.loads(source_section["content"])["mode"] == (
            mode if mode != "bundle" else "explore"
        )
        session_section = next(
            section for section in payload["sections"]
            if section["name"] == "session_current_state"
        )
        assert json.loads(session_section["content"])["state"] == "current"
        assert "--no-refresh" not in result.prompt_bundle
        assert result.metadata["section_count"] == 3
        assert "missing B434 context" not in json.dumps(result.metadata)
        assert "source_graph.db" not in result.prompt_bundle

    assert '"mode": "focus"' in seen["focus"]
    assert '"mode": "slice"' in seen["slice"]
    assert '"mode": "bundle"' in seen["bundle"]


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
    source = next(s for s in payload["sections"] if s["name"] == "source_graph")
    bounded = json.loads(source["content"])
    assert bounded["truncated"] is True
    assert bounded["original_bytes"] > project_context.MAX_TOOL_OUTPUT_BYTES
    assert bounded["original_hit_count"] > 0
    assert source["hit_count"] == bounded["original_hit_count"]
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
