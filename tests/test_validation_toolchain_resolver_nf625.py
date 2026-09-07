"""NF-2026-00625: a declared validation command must be resolved against the
environment the WORKER receives, not the coordinator's own repository.

The coordinator resolves a declared validation command against the canonical
repository, where an untracked path such as a repo-local virtualenv is present.
The worker executes it inside an isolated git worktree, which materializes
tracked content only -- so the command preflights clean and then cannot run.

Measured on this repository's canonical ledger before the fix: of 1,053
``validation_failed`` records carrying a declared command, 196 attempts across
138 distinct tasks named a repository-local virtualenv binary and 48 attempts
across 40 tasks required node/npm/npx, while a filesystem census of the 18 live
worker worktrees found 0 containing a virtualenv directory and 0 containing an
installed ``vscode-extension/node_modules`` tree.

Scope boundary asserted here as well as in the module: this resolver adjudicates
only repository-relative paths, where "present for the coordinator, untracked,
therefore absent for the worker" is provable from the git index. It deliberately
returns nothing for bare PATH executables, because whether a given adapter's
sandbox grants ``node`` is adapter policy this module cannot observe.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import toolchain_authority, worker_workspace  # noqa: E402


TRACKED = frozenset(
    {
        "src/aiworkhub/core.py",
        "tests/test_task_fsm.py",
        "scripts/check_release_assurance.py",
        "vscode-extension/package.json",
        "vscode-extension/test/bridge.test.js",
    }
)


def _repo(tmp_path: Path) -> Path:
    """A repository where the tracked files AND an untracked venv both exist."""
    for relative in TRACKED:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    venv_binary = tmp_path / ".venv" / "bin" / "mypy"
    venv_binary.parent.mkdir(parents=True, exist_ok=True)
    venv_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    node_modules = tmp_path / "vscode-extension" / "node_modules" / "mocha" / "index.js"
    node_modules.parent.mkdir(parents=True, exist_ok=True)
    node_modules.write_text("//", encoding="utf-8")
    return tmp_path


def test_repo_local_virtualenv_binary_is_unresolvable_for_a_worker(tmp_path):
    # The 196-attempt class. The binary exists for the coordinator and is
    # untracked, so a git worktree cannot supply it.
    findings = toolchain_authority.worker_workspace_unresolvable_paths(
        _repo(tmp_path), ".venv/bin/mypy src/aiworkhub/core.py", tracked=TRACKED
    )
    assert findings == ((".venv/bin/mypy", toolchain_authority.WORKER_WORKTREE_ABSENT),)


def test_untracked_node_modules_input_is_unresolvable_for_a_worker(tmp_path):
    findings = toolchain_authority.worker_workspace_unresolvable_paths(
        _repo(tmp_path),
        "node vscode-extension/node_modules/mocha/index.js",
        tracked=TRACKED,
    )
    assert findings == (
        (
            "vscode-extension/node_modules/mocha/index.js",
            toolchain_authority.WORKER_WORKTREE_ABSENT,
        ),
    )


def test_compile_then_run_generated_executable_is_not_unresolvable(tmp_path):
    repo = _repo(tmp_path)
    generated = repo / "experiments" / "probe"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text("stale coordinator copy", encoding="utf-8")
    commands = (
        "g++ -std=c++17 src/aiworkhub/core.py -o experiments/probe",
        "experiments/probe",
    )
    outputs = frozenset(
        output
        for command in commands
        for output in toolchain_authority.declared_compiler_outputs(command)
    )
    assert outputs == frozenset({"experiments/probe"})
    for command in commands:
        assert (
            toolchain_authority.worker_workspace_unresolvable_paths(
                repo, command, tracked=TRACKED, generated_paths=outputs
            )
            == ()
        )
@pytest.mark.parametrize(
    "command",
    [
        "python3 -m pytest -q tests/test_task_fsm.py",
        "python3 scripts/check_release_assurance.py",
        "python3 -m mypy src/aiworkhub/core.py",
        "npm --prefix vscode-extension test",
        "node vscode-extension/test/bridge.test.js",
    ],
)
def test_commands_a_worker_can_actually_run_are_left_alone(tmp_path, command):
    assert (
        toolchain_authority.worker_workspace_unresolvable_paths(
            _repo(tmp_path), command, tracked=TRACKED
        )
        == ()
    )


def test_a_tracked_directory_prefix_counts_as_present(tmp_path):
    # "vscode-extension" is not itself a tracked entry -- its children are.
    assert (
        toolchain_authority.worker_workspace_unresolvable_paths(
            _repo(tmp_path), "npm --prefix vscode-extension/test run x", tracked=TRACKED
        )
        == ()
    )


def test_bare_path_executables_are_never_adjudicated(tmp_path):
    # node/npm/npx carry no "/" and are therefore out of scope by construction:
    # this module cannot observe an adapter's sandbox PATH and must not invent
    # a verdict for it.
    for command in ("node --test x.test.js", "npm test", "npx mocha"):
        assert (
            toolchain_authority.worker_workspace_unresolvable_paths(
                _repo(tmp_path), command, tracked=TRACKED
            )
            == ()
        )


def test_a_path_missing_for_the_coordinator_too_is_not_double_reported(tmp_path):
    # Already covered by the existing executable/repository_input refusals.
    assert (
        toolchain_authority.worker_workspace_unresolvable_paths(
            _repo(tmp_path), "python3 tools/not_here_at_all.py", tracked=TRACKED
        )
        == ()
    )


def test_resolver_reports_nothing_when_the_git_index_is_unavailable(tmp_path):
    # Fail-open on purpose: this check may only ever ADD a refusal it can
    # prove. A missing git index must never manufacture one.
    assert (
        toolchain_authority.worker_workspace_unresolvable_paths(
            _repo(tmp_path), ".venv/bin/mypy src/aiworkhub/core.py", tracked=frozenset()
        )
        == ()
    )


def test_resolver_is_total_on_unparseable_and_hostile_commands(tmp_path):
    repo = _repo(tmp_path)
    for command in ("'unterminated", "", "   ", "../../etc/passwd", "/abs/path/x"):
        assert (
            toolchain_authority.worker_workspace_unresolvable_paths(
                repo, command, tracked=TRACKED
            )
            == ()
        )


def _system_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    return path


# NF-2026-00625 M2: the measured residual blocker was bare ``npm`` -- canonical
# preflight trusted only git/node, so the exact ``npm --prefix
# vscode-extension test`` card shape fell through as an unresolved bare Path
# and NF633 stayed blocked even though node itself resolved cleanly. node,
# npm and npx are now one trusted system-tool family in
# ``worker_workspace._TRUSTED_VALIDATION_SYSTEM_EXECUTABLES``.
@pytest.mark.parametrize("tool", ["node", "npm", "npx"])
def test_node_family_resolves_to_an_immutable_absolute_path(
    tool: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_tool = _system_executable(tmp_path / "system" / tool)
    monkeypatch.setattr(
        worker_workspace.shutil,
        "which",
        lambda name: str(system_tool) if name == tool else None,
    )

    resolved = worker_workspace._resolve_trusted_system_validation_executable(tool)

    assert resolved == system_tool.resolve()
    assert resolved.is_absolute()


def test_npm_prefix_card_becomes_launch_capable_when_npm_is_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Outside ``tmp_path`` (the repo passed below): a system tool resolved
    # from inside the repository is repository-owned and must stay rejected,
    # so a real trusted host tool has to live elsewhere.
    system_npm = _system_executable(tmp_path.parent / "system" / "npm")
    monkeypatch.setattr(
        worker_workspace.shutil,
        "which",
        lambda name: str(system_npm) if name == "npm" else None,
    )
    monkeypatch.setattr(
        worker_workspace,
        "_declared_workspace_seed_closure",
        lambda *args: ((), (), ()),
    )

    missing = worker_workspace.preflight_validation_capabilities(
        tmp_path,
        {
            "allowed_writes": ["vscode-extension/test/bridge.test.js"],
            "validation": ["npm --prefix vscode-extension test"],
        },
    )

    assert missing == ()


def test_npm_prefix_card_stays_blocked_when_npm_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_workspace.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        worker_workspace,
        "_declared_workspace_seed_closure",
        lambda *args: ((), (), ()),
    )

    missing = worker_workspace.preflight_validation_capabilities(
        tmp_path,
        {
            "allowed_writes": ["vscode-extension/test/bridge.test.js"],
            "validation": ["npm --prefix vscode-extension test"],
        },
    )

    assert missing == ("executable:validation_executable_unavailable:npm",)


def test_repository_tracked_paths_reads_this_repository(tmp_path):
    # Against the real repository the helper must return a non-empty index
    # containing a file we know is tracked, and must return empty for a
    # directory that is not a git work tree.
    here = Path(__file__).resolve().parents[1]
    tracked = toolchain_authority.repository_tracked_paths(here)
    assert "src/aiworkhub/toolchain_authority.py" in tracked
    assert ".venv/bin/mypy" not in tracked
    assert toolchain_authority.repository_tracked_paths(tmp_path) == frozenset()
