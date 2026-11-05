"""NF-2026-01928 part C: ``.scratch/<session_id>/`` is the one scratch location.

Scratch never leaves the repository (gitignored), Source Graph indexes it (an
include root), no seat touches it with built-in Read/Write/Edit (tool guard
R8), every seat is told so (one managed policy line), the janitor prunes aged
session dirs, and a worker's own scratch never enters its changed-file set.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    agent_tool_instructions as instr,
    source_graph,
    storage_retention,
    tool_guard_hook,
    worker_workspace,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRATCH_PROBE = ".scratch/s1/probe.py"
SCRATCH_RULE = (
    "Every seat keeps scratch in .scratch/<session_id>/ of the verified repo, never the"
    " host/harness scratchpad or system temp, and creates/edits it only with the AIWorkHub"
    " create/semantic-edit tools."
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60
    )


def _repo_with_canonical_scratch_config(tmp_path: Path) -> Path:
    """A git repo carrying this repository's own .gitignore and Source Graph policy."""
    root = tmp_path / "repo"
    (root / ".aiworkhub" / "config").mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / ".gitignore", root / ".gitignore")
    shutil.copyfile(
        REPO_ROOT / ".aiworkhub" / "config" / "source_graph.json",
        root / ".aiworkhub" / "config" / "source_graph.json",
    )
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Task MCP Tests").returncode == 0
    assert _git(root, "add", ".gitignore", ".aiworkhub/config/source_graph.json", "src/app.py").returncode == 0
    assert _git(root, "commit", "-qm", "fixture").returncode == 0
    return root.resolve()


def _write(path: Path, text: str = "print('probe')\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_scratch_is_gitignored_and_a_source_graph_include_root() -> None:
    ignore_lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/.scratch/" in ignore_lines
    policy = json.loads(
        (REPO_ROOT / ".aiworkhub" / "config" / "source_graph.json").read_text(encoding="utf-8")
    )
    assert tool_guard_hook.SCRATCH_DIR in policy["include_roots"]
    assert policy["revision"] >= 3
    # Source Graph's non-bypassable excludes drop a dir before include_roots
    # is consulted, so the scratch root must not be one of them.
    assert tool_guard_hook.SCRATCH_DIR not in tool_guard_hook.EXCLUDED_DIRS
    assert tool_guard_hook.SCRATCH_DIR not in source_graph.DEFAULT_EXCLUDE_DIR_NAMES


def test_scratch_files_are_indexed_although_git_ignores_them(tmp_path: Path) -> None:
    root = _repo_with_canonical_scratch_config(tmp_path)
    _write(root / SCRATCH_PROBE)
    assert _git(root, "check-ignore", "-q", SCRATCH_PROBE).returncode == 0

    indexed = {path.relative_to(root).as_posix() for path in source_graph.iter_source_files(root)}
    assert SCRATCH_PROBE in indexed
    assert "src/app.py" in indexed

    # The shared part-A predicate, with the guard's own reading of the policy.
    policy = tool_guard_hook.load_index_policy(root)
    assert tool_guard_hook.index_admits(policy, ".scratch", is_dir=True, ignored=True)
    assert tool_guard_hook.index_admits(policy, ".scratch/s1", is_dir=True, ignored=True)
    assert tool_guard_hook.index_admits(policy, SCRATCH_PROBE, is_dir=False, ignored=True)


@pytest.mark.parametrize(
    ("tool", "extra"),
    [
        ("Read", {}),
        ("Read", {"offset": 1, "limit": 20}),
        ("Write", {"content": "x = 2\n"}),
        ("Edit", {"old_string": "probe", "new_string": "other"}),
        ("MultiEdit", {"edits": []}),
    ],
)
def test_builtin_read_edit_and_overwrite_in_scratch_are_denied_with_replacements(
    tmp_path: Path, tool: str, extra: dict[str, object]
) -> None:
    root = _repo_with_canonical_scratch_config(tmp_path)
    _write(root / SCRATCH_PROBE)
    verdict = tool_guard_hook.decide(tool, {"file_path": str(root / SCRATCH_PROBE), **extra}, root)

    assert verdict is not None, tool
    rule, reason = verdict
    assert rule == "R8", reason
    assert reason.startswith("AIWorkHub tool guard R8:")
    assert len(reason) <= tool_guard_hook.REASON_MAX_CHARS
    assert SCRATCH_PROBE in reason
    for replacement in (
        "aiworkhub_manager_source_graph_query",
        "aiworkhub_worker_source_graph_query",
        '"body"',
        '"file"',
        '"bodygrep"',
        "aiworkhub_worker_semantic_edit_prepare",
        "aiworkhub_manager_semantic_edit_prepare/_apply",
    ):
        assert replacement in reason, (replacement, reason)
    # NF-2026-02088: a managed repository carries no scripts/manager_semantic_edit.py.
    assert "scripts/manager_semantic_edit.py" not in reason
    assert "unindexed" not in reason


def test_write_that_creates_a_new_scratch_file_is_allowed(tmp_path: Path) -> None:
    root = _repo_with_canonical_scratch_config(tmp_path)
    tool_input = {"file_path": str(root / SCRATCH_PROBE), "content": "x = 1\n"}
    assert tool_guard_hook.decide("Write", tool_input, root) is None


def test_scratch_denial_is_independent_of_the_file_suffix(tmp_path: Path) -> None:
    root = _repo_with_canonical_scratch_config(tmp_path)
    notes = _write(root / ".scratch" / "s1" / "notes.txt", "free text\n")
    for tool in ("Read", "Edit", "Write"):
        verdict = tool_guard_hook.decide(tool, {"file_path": str(notes)}, root)
        assert verdict is not None and verdict[0] == "R8", tool


def test_new_file_write_outside_scratch_is_still_allowed(tmp_path: Path) -> None:
    root = _repo_with_canonical_scratch_config(tmp_path)
    for rel in ("src/new_module.py", ".scratchpad/new.py", "notes/.scratch.py"):
        tool_input = {"file_path": str(root / rel), "content": "x = 1\n"}
        assert tool_guard_hook.decide("Write", tool_input, root) is None, rel


def test_one_managed_rule_line_names_the_scratch_location() -> None:
    canonical = instr.render_canonical()
    assert canonical.splitlines().count(f"- {SCRATCH_RULE}") == 1
    assert sum(".scratch/<session_id>/" in line for line in canonical.splitlines()) == 1
    for provider in instr.PROVIDERS:
        assert f"- {SCRATCH_RULE}" in instr.render_projection(provider).splitlines(), provider


@pytest.mark.parametrize("provider", ["CLAUDE.md", "AGENTS.md"])
def test_checked_in_documents_are_the_generator_output(provider: str) -> None:
    current = (REPO_ROOT / provider).read_text(encoding="utf-8")
    plan = instr.build_apply_plan(provider, current)  # type: ignore[arg-type]
    assert plan["ok"], plan["reason"]
    assert plan["planned_text"] == current
    assert f"- {SCRATCH_RULE}" in current.splitlines()


def test_retention_prunes_an_aged_scratch_session_and_keeps_a_fresh_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (tmp_path / "repo").resolve()
    (root / ".aiworkhub").mkdir(parents=True)
    old = _write(root / ".scratch" / "old-session" / "probe.py").parent
    fresh = _write(root / ".scratch" / "fresh-session" / "probe.py").parent
    loose = _write(root / ".scratch" / "loose.txt", "kept\n")
    now = time.time()
    window = storage_retention._SCRATCH_MAX_AGE_SECONDS
    ages = {old: now - window - 3600, fresh: now - 60}
    # utime is denied in the worker sandbox, so the age is injected, not stamped.
    monkeypatch.setattr(storage_retention, "_janitor_mtime", lambda path: ages.get(path, now))

    result = storage_retention._janitor_scratch(root, now)

    assert result["ok"] is True, result
    assert result["removed"] == 1
    assert not old.exists()
    assert fresh.is_dir()
    assert loose.is_file()


def test_retention_keeps_a_scratch_root_that_resolves_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (tmp_path / "repo").resolve()
    (root / ".aiworkhub").mkdir(parents=True)
    elsewhere = _write(tmp_path / "elsewhere" / "old-session" / "probe.py").parent
    try:
        (root / ".scratch").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    monkeypatch.setattr(storage_retention, "_janitor_mtime", lambda path: 0.0)

    result = storage_retention._janitor_scratch(root, time.time())

    assert result["ok"] is True, result
    assert elsewhere.is_dir()


def test_a_workers_own_scratch_never_enters_its_changed_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_canonical_scratch_config(tmp_path)
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    workspace = worker_workspace.create_workspace(
        repo,
        "nf01928-scratch",
        {"allowed_writes": ["src/app.py"], "read_first": ["src/app.py"]},
        "validation",
    )
    try:
        _write(workspace.path / SCRATCH_PROBE)
        _write(workspace.path / ".scratch" / "s1" / "notes.txt", "free text\n")
        (workspace.path / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")

        changed = worker_workspace.changed_paths(workspace)

        assert changed == ["src/app.py"]
        assert not any(path.startswith(".scratch") for path in changed)
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)
