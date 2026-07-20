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
        importlib.import_module("geoai_task_mcp.deepseek_credentials")
        return
    except ImportError:
        pass

    stub = types.ModuleType("geoai_task_mcp.deepseek_credentials")

    class CredentialError(Exception):
        def __init__(self, reason: str = "deepseek_credential_stub_environment") -> None:
            super().__init__(reason)
            self.reason = reason

    def load_credential(repo=None):  # noqa: ANN001, ARG001
        raise CredentialError("deepseek_credential_stub_environment")

    stub.CredentialError = CredentialError
    stub.load_credential = load_credential
    sys.modules["geoai_task_mcp.deepseek_credentials"] = stub


_ensure_deepseek_credentials_stub()

from geoai_task_mcp import process_launcher, project_context, runtime_adapters, worker_workspace  # noqa: E402


def _write_tool(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _context_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "AITools").mkdir(parents=True)
    _write_tool(
        repo / "AITools/source_graph.py",
        """
import json, sys
cmd = sys.argv[1]
if cmd == "bundle":
    mode, query, budget = sys.argv[2], sys.argv[3], sys.argv[sys.argv.index("--max-lines") + 1]
else:
    mode, query, budget = cmd, sys.argv[2], sys.argv[3]
print("[*] Language: python (python)")
print(json.dumps({"tool":"source_graph","mode":mode,"query":query,"budget":budget}, sort_keys=True))
""",
    )
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


def test_project_context_collects_source_modes_bounded_session_and_optional_kb(tmp_path: Path) -> None:
    repo = _context_repo(tmp_path)
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
        assert json.loads(session_section["content"])["cmd"] == "current-state"
        assert "--no-refresh" not in result.prompt_bundle
        assert result.metadata["section_count"] == 3
        assert "missing B434 context" not in json.dumps(result.metadata)
        assert "source_graph.db" not in result.prompt_bundle

    assert '"mode": "focus"' in seen["focus"]
    assert '"mode": "slice"' in seen["slice"]
    assert '"mode": "bundle"' in seen["bundle"]


def test_project_context_validates_types_and_rejects_shellish_or_overbudget_values(tmp_path: Path) -> None:
    repo = _context_repo(tmp_path)
    card = _project_context_card()
    card["project_context"]["source_graph"]["mode"] = "find"
    with pytest.raises(project_context.ProjectContextError, match="mode_invalid"):
        project_context.collect_project_context(repo, card)

    card = _project_context_card()
    card["project_context"]["source_graph"]["budget"] = 10_000
    with pytest.raises(project_context.ProjectContextError, match="budget_out_of_range"):
        project_context.collect_project_context(repo, card)

    card = _project_context_card()
    card["project_context"]["source_graph"]["query"] = "ProcessManager; rm -rf /"
    result = project_context.collect_project_context(repo, card)
    assert result is not None
    assert "rm -rf" in result.prompt_bundle


def test_large_valid_json_is_canonicalized_into_bounded_valid_preview(tmp_path: Path) -> None:
    repo = _context_repo(tmp_path)
    _write_tool(
        repo / "AITools/source_graph.py",
        "import json\nprint('[*] Language: python (python)')\n"
        "print(json.dumps({'rows':['x' * 1024] * 80}))\n",
    )
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


def test_required_context_rejects_before_claim_and_optional_degrades(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    repo = _context_repo(tmp_path)
    (repo / "AITools/source_graph.py").unlink()
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
    assert "context_tool_missing" in blocked["blocked_reason"]
    assert claims == []

    optional = _project_context_card(required=False)
    degraded = project_context.collect_project_context(repo, optional)
    assert degraded is not None
    assert degraded.metadata["sections"][0]["degraded_reason"]


def test_identical_bundle_reaches_all_adapter_prompts_and_metadata_is_redacted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = _context_repo(tmp_path)
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
    subprocess.run(["git", "add", "AITools/source_graph.py", "AITools/transcript_graph.py", "AITools/kb.py", "out/result.json"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    workspace = worker_workspace.create_workspace(
        repo,
        "no-db-copy",
        {"allowed_writes": ["out/result.json"], "read_first": ["AITools/source_graph.py"]},
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
