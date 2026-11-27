"""NF-2026-01013: host-local AIWorkHub files stay out of a hosted repo's git."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from aiworkhub import core, repo_git_exclude, task_store
from aiworkhub.repo_git_exclude import (
    BLOCK_BEGIN,
    BLOCK_END,
    HOST_LOCAL_PATTERNS,
    ensure_host_local_excludes,
)

HOST_LOCAL_FILES = (
    ".aiworkhub/state.db",
    ".scratch/note.txt",
    ".mcp.json",
    ".vscode/mcp.json",
    "AGENTS.md",
    "CLAUDE.md",
    ".github/copilot-instructions.md",
)

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git executable not available")


def _git(root: Path, *args: str) -> str:
    assert GIT is not None
    result = subprocess.run(
        [GIT, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout


def _create_host_local_files(root: Path) -> None:
    for rel in HOST_LOCAL_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("host-local\n", encoding="utf-8")


def test_block_lists_every_host_local_path():
    assert HOST_LOCAL_PATTERNS == (
        "/.aiworkhub/",
        "/.scratch/",
        "/.mcp.json",
        "/.vscode/mcp.json",
        "/AGENTS.md",
        "/CLAUDE.md",
        "/.github/copilot-instructions.md",
    )


@needs_git
def test_fresh_repo_hides_host_local_files_and_is_idempotent(tmp_path):
    root = tmp_path / "hosted"
    root.mkdir()
    _git(root, "init", "-q")
    _create_host_local_files(root)
    (root / "README.md").write_text("project\n", encoding="utf-8")

    first = ensure_host_local_excludes(root)
    assert first["state"] == "written", first
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    listed = {line[3:].strip() for line in status.splitlines() if line.strip()}
    assert listed == {"README.md"}, status
    assert not (root / ".gitignore").exists()

    exclude = Path(first["exclude_path"])
    before = exclude.read_bytes()
    assert before.count(BLOCK_BEGIN.encode()) == 1
    second = ensure_host_local_excludes(root)
    assert second["state"] == "unchanged", second
    assert exclude.read_bytes() == before


@needs_git
def test_already_tracked_claude_md_stays_tracked(tmp_path):
    root = tmp_path / "selfhost"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "CLAUDE.md").write_text("v1\n", encoding="utf-8")
    _git(root, "add", "CLAUDE.md")
    _git(root, "commit", "-q", "-m", "track CLAUDE.md")

    assert ensure_host_local_excludes(root)["state"] == "written"
    (root / "CLAUDE.md").write_text("v2\n", encoding="utf-8")
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    assert " M CLAUDE.md" in status.splitlines(), status
    assert "CLAUDE.md" in _git(root, "ls-files").split()


def test_existing_exclude_content_is_preserved_and_stale_block_replaced(tmp_path):
    root = tmp_path / "r"
    info = root / ".git" / "info"
    info.mkdir(parents=True)
    exclude = info / "exclude"
    exclude.write_text(f"*.log\n{BLOCK_BEGIN}\n/old-entry\n{BLOCK_END}\nbuild/", encoding="utf-8")

    receipt = ensure_host_local_excludes(root)
    assert receipt["state"] == "updated", receipt
    text = exclude.read_text(encoding="utf-8")
    assert text.startswith("*.log\n")
    assert "/old-entry" not in text
    assert text.count(BLOCK_BEGIN) == 1 and text.count(BLOCK_END) == 1
    assert text.endswith("build/")
    assert ensure_host_local_excludes(root)["state"] == "unchanged"


def test_unterminated_block_keeps_user_rules_after_the_marker(tmp_path):
    root = tmp_path / "r"
    info = root / ".git" / "info"
    info.mkdir(parents=True)
    exclude = info / "exclude"
    exclude.write_text(f"*.log\n{BLOCK_BEGIN}\nuser-rule/\n", encoding="utf-8")

    assert ensure_host_local_excludes(root)["state"] == "updated"
    text = exclude.read_text(encoding="utf-8")
    assert "*.log\n" in text and "user-rule/\n" in text
    assert text.count(BLOCK_BEGIN) == 1 and text.count(BLOCK_END) == 1
    assert ensure_host_local_excludes(root)["state"] == "unchanged"


def test_linked_worktree_writes_the_common_info_exclude(tmp_path):
    main_git = tmp_path / "main" / ".git"
    private = main_git / "worktrees" / "wt"
    private.mkdir(parents=True)
    (private / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n", encoding="utf-8")

    receipt = ensure_host_local_excludes(wt)
    assert receipt["state"] == "written", receipt
    common_exclude = main_git / "info" / "exclude"
    assert Path(receipt["exclude_path"]).resolve() == common_exclude.resolve()
    assert BLOCK_BEGIN in common_exclude.read_text(encoding="utf-8")
    assert not (private / "info").exists()


@needs_git
def test_real_git_worktree_resolves_common_exclude(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "README.md").write_text("x\n", encoding="utf-8")
    _git(main, "add", "README.md")
    _git(main, "commit", "-q", "-m", "init")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(wt))
    assert (wt / ".git").is_file()

    receipt = ensure_host_local_excludes(wt)
    assert receipt["state"] == "written", receipt
    _create_host_local_files(wt)
    assert _git(wt, "status", "--porcelain", "--untracked-files=all").strip() == ""


def test_non_git_directory_is_a_noop_receipt(tmp_path):
    receipt = ensure_host_local_excludes(tmp_path)
    assert receipt["state"] == "no_git_dir"
    assert receipt["reason"] == "no_git_dir"
    assert list(tmp_path.iterdir()) == []


def test_broken_gitdir_pointer_returns_reason_without_raising(tmp_path):
    (tmp_path / ".git").write_text("gitdir: does/not/exist\n", encoding="utf-8")
    receipt = ensure_host_local_excludes(tmp_path)
    assert receipt["state"] == "error"
    assert receipt["reason"] == "gitdir_target_missing"


def test_manager_bootstrap_calls_it_only_for_the_bootstrap_tool(tmp_path, monkeypatch):
    root = tmp_path / "repo_a"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(
        core,
        "_codex_manager_identity",
        lambda: {
            "provider": "codex",
            "session_id": "nf01013-0000-4000-8000-00000000c0de",
            "thread_id": "nf01013-0000-4000-8000-00000000c0de",
        },
    )
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_CONTRACT_DELIVERIES", {})
    monkeypatch.setattr(core, "_run_off_request_path", lambda target, *, name: None)

    calls: list[Path] = []

    def _record(repo_root):
        calls.append(Path(repo_root))
        return {"state": "unchanged", "reason": "", "exclude_path": "x"}

    monkeypatch.setattr(repo_git_exclude, "ensure_host_local_excludes", _record)

    gate = core.manager_bootstrap()
    assert calls == []
    assert "host_local_git_exclude" not in gate

    reply = core.manager_bootstrap(bootstrap_call=True)
    assert len(calls) == 1
    assert calls[0].resolve() == root.resolve()
    assert reply["host_local_git_exclude"] == {"state": "unchanged", "reason": ""}

    def _boom(repo_root):
        raise RuntimeError("boom")

    monkeypatch.setattr(repo_git_exclude, "ensure_host_local_excludes", _boom)
    failed = core.manager_bootstrap(bootstrap_call=True)
    assert failed["ok"] is True
    assert failed["host_local_git_exclude"] == {"state": "error", "reason": "RuntimeError"}
