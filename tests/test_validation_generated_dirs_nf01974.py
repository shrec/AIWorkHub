"""NF-2026-01974: a path under a directory an EARLIER validation step creates
(``cmake -B``, ``mkdir``, ``meson setup``, ``--out-dir``) is generated, not a
missing input; a path nothing earlier creates is still reported by name.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aiworkhub.toolchain_authority import (
    CARD_CONTRACT_INPUT_MISSING,
    card_contract_missing_inputs,
    declared_compiler_outputs,
    worker_workspace_unresolvable_paths,
)


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "CMakeLists.txt").write_text("project(x)\n", encoding="utf-8")
    subprocess.run(["git", "add", "CMakeLists.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True, capture_output=True)
    return repo


def _card(*commands: str) -> dict[str, object]:
    return {"validation": list(commands), "allowed_writes": [], "required_outputs": []}


def test_three_step_cmake_card_has_no_missing_inputs(tmp_path):
    repo = _git_repo(tmp_path)
    card = _card(
        "cmake -S . -B build",
        "cmake --build build",
        "ctest --test-dir build",
        "build/tests/run_tests",
    )
    assert card_contract_missing_inputs(repo, card) == ()


def test_mkdir_then_read_under_it_is_clean(tmp_path):
    repo = _git_repo(tmp_path)
    card = _card("mkdir -p out", "python3 -m pytest -q out/x.txt")
    assert card_contract_missing_inputs(repo, card) == ()


def test_path_nothing_creates_is_still_reported(tmp_path):
    repo = _git_repo(tmp_path)
    card = _card("mkdir -p out", "python3 -m pytest -q never_created/x.txt")
    assert card_contract_missing_inputs(repo, card) == (
        ("never_created/x.txt", CARD_CONTRACT_INPUT_MISSING),
    )


def test_path_created_only_by_a_later_command_is_still_reported(tmp_path):
    repo = _git_repo(tmp_path)
    card = _card("python3 -m pytest -q out/x.txt", "mkdir -p out")
    assert card_contract_missing_inputs(repo, card) == (
        ("out/x.txt", CARD_CONTRACT_INPUT_MISSING),
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cmake -S . -B build", {"build"}),
        ("cmake -Bbuild/debug -S .", {"build/debug"}),
        ("mkdir -p out gen/sub", {"out", "gen/sub"}),
        ("meson setup builddir src", {"builddir"}),
        ("tool --out-dir dist", {"dist"}),
        ("tool --outdir dist", {"dist"}),
        ("tool --out-dir=dist", {"dist"}),
        ("cmake -B /abs/build", set()),
        ("mkdir -p ../outside", set()),
        ("mkdir -p c:/x", set()),
        ("mkdir -m 0755 out", {"out"}),
        ("meson setup --backend ninja builddir src", {"builddir"}),
        ("meson setup --wipe -D opt=1 builddir", {"builddir"}),
        ("cmake -B '\\abs\\build'", set()),
    ],
)
def test_declared_outputs_recognize_directory_creating_steps(command, expected):
    assert declared_compiler_outputs(command) == frozenset(expected)


def test_launch_resolver_treats_paths_under_generated_dir_as_generated(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "build" / "tests").mkdir(parents=True)
    (repo / "build" / "tests" / "run_tests").write_text("stale", encoding="utf-8")
    tracked = frozenset({"CMakeLists.txt"})
    command = "build/tests/run_tests"
    assert worker_workspace_unresolvable_paths(repo, command, tracked=tracked)
    assert (
        worker_workspace_unresolvable_paths(
            repo, command, tracked=tracked, generated_paths=frozenset({"build"})
        )
        == ()
    )
