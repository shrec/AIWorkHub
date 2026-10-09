"""NF-2026-01334: card creation refuses the same missing validation operands
launch refuses, for suffix-less directories and path-shaped executables, by
reusing ``worker_workspace``'s own command parsing and trusted-head predicate
instead of a second, hand-rolled notion of "looks like a path".
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from aiworkhub import worker_workspace
from aiworkhub.toolchain_authority import (
    CARD_CONTRACT_INPUT_MISSING,
    WORKER_WORKTREE_ABSENT,
    card_contract_missing_inputs,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def _track(repo: Path, *relative_paths: str) -> None:
    _git(repo, "add", *relative_paths)
    _git(repo, "commit", "-q", "-m", "seed")


def _write(repo: Path, relative: str, content: str = "") -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _card(**overrides: object) -> dict[str, object]:
    card: dict[str, object] = {
        "validation": [],
        "allowed_writes": [],
        "required_outputs": [],
    }
    card.update(overrides)
    return card


def test_missing_suffixless_directory_operand_is_reported(tmp_path):
    repo = _git_repo(tmp_path)
    card = _card(validation=["python3 -m pytest -q somepkg/fixtures"])
    assert card_contract_missing_inputs(repo, card) == (
        ("somepkg/fixtures", CARD_CONTRACT_INPUT_MISSING),
    )


def test_missing_path_shaped_executable_operand_is_reported(tmp_path):
    repo = _git_repo(tmp_path)
    _write(repo, "tests/test_x.py", "def test_x(): pass\n")
    _track(repo, "tests/test_x.py")
    card = _card(validation=["scripts/run_checks.sh -m pytest -q tests/test_x.py"])
    assert card_contract_missing_inputs(repo, card) == (
        ("scripts/run_checks.sh", CARD_CONTRACT_INPUT_MISSING),
    )


def test_untracked_path_shaped_executable_gives_worktree_absent(tmp_path):
    repo = _git_repo(tmp_path)
    _write(repo, "scripts/run_checks.sh", "#!/bin/sh\nexit 0\n")
    _write(repo, "tests/test_x.py", "def test_x(): pass\n")
    _track(repo, "tests/test_x.py")
    card = _card(validation=["scripts/run_checks.sh -m pytest -q tests/test_x.py"])
    assert card_contract_missing_inputs(repo, card) == (
        ("scripts/run_checks.sh", WORKER_WORKTREE_ABSENT),
    )


def test_existing_directory_operand_is_not_reported(tmp_path):
    repo = _git_repo(tmp_path)
    _write(repo, "fixtures/data/.keep", "")
    _track(repo, "fixtures/data/.keep")
    card = _card(validation=["python3 -m pytest -q fixtures/data"])
    assert card_contract_missing_inputs(repo, card) == ()


def test_declared_output_is_not_a_missing_input(tmp_path):
    repo = _git_repo(tmp_path)
    card = _card(
        validation=["python3 -m pytest -q generated/report.json"],
        required_outputs=["generated/report.json"],
    )
    assert card_contract_missing_inputs(repo, card) == ()


def test_allowed_write_directory_is_not_a_missing_input(tmp_path):
    repo = _git_repo(tmp_path)
    card = _card(
        validation=["python3 -m pytest -q build/artifacts"],
        allowed_writes=["build/artifacts"],
    )
    assert card_contract_missing_inputs(repo, card) == ()


def test_compiler_declared_output_is_not_a_missing_input(tmp_path):
    repo = _git_repo(tmp_path)
    _write(repo, "src/main.c", "int main(void) { return 0; }\n")
    _track(repo, "src/main.c")
    card = _card(validation=["cc -o build/out.bin src/main.c"])
    assert card_contract_missing_inputs(repo, card) == ()


def test_option_value_token_is_not_treated_as_a_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        worker_workspace, "_VALIDATION_OPTIONS_WITH_VALUE", frozenset({"--config"})
    )
    repo = _git_repo(tmp_path)
    _write(repo, "tests/test_x.py", "def test_x(): pass\n")
    _track(repo, "tests/test_x.py")
    card = _card(
        validation=["python3 -m pytest --config does/not/exist.cfg -q tests/test_x.py"]
    )
    assert card_contract_missing_inputs(repo, card) == ()


def test_pytest_node_id_tail_and_glob_tokens_are_not_treated_as_paths(tmp_path):
    repo = _git_repo(tmp_path)
    _write(repo, "tests/test_x.py", "class TestX:\n    def test_a(self): pass\n")
    _track(repo, "tests/test_x.py")
    card = _card(
        validation=[
            "python3 -m pytest -q tests/test_x.py::TestX::test_a tests/*.py"
        ]
    )
    assert card_contract_missing_inputs(repo, card) == ()


def test_node_id_file_component_still_checked(tmp_path):
    repo = _git_repo(tmp_path)
    card = _card(
        validation=["python3 -m pytest -q tests/test_missing.py::TestX::test_a"]
    )
    assert card_contract_missing_inputs(repo, card) == (
        ("tests/test_missing.py", CARD_CONTRACT_INPUT_MISSING),
    )


def test_cd_relative_operand_resolved_against_cd_target(tmp_path):
    repo = _git_repo(tmp_path)
    _write(repo, "svc/tests/test_x.py", "def test_x(): pass\n")
    _track(repo, "svc/tests/test_x.py")
    card = _card(validation=["cd svc && python3 -m pytest -q tests/test_x.py"])
    assert card_contract_missing_inputs(repo, card) == ()


def test_fully_valid_card_reports_no_missing_inputs(tmp_path):
    repo = _git_repo(tmp_path)
    _write(repo, "tests/test_x.py", "def test_x(): pass\n")
    _track(repo, "tests/test_x.py")
    card = _card(validation=["python3 -m pytest -q tests/test_x.py"])
    assert card_contract_missing_inputs(repo, card) == ()


def test_bare_path_shaped_trusted_executable_is_never_a_path(tmp_path):
    repo = _git_repo(tmp_path)
    _write(repo, "tests/test_x.py", "def test_x(): pass\n")
    _track(repo, "tests/test_x.py")
    card = _card(validation=[".venv/bin/pytest -q tests/test_x.py"])
    findings = card_contract_missing_inputs(repo, card)
    assert all(token != ".venv/bin/pytest" for token, _reason in findings)


def test_untracked_venv_module_head_returns_no_findings(tmp_path):
    repo = _git_repo(tmp_path)
    _write(repo, "tests/test_x.py", "def test_x(): pass\n")
    _track(repo, "tests/test_x.py")
    card = _card(
        validation=[".venv/Scripts/python.exe -m pytest -q tests/test_x.py"]
    )
    assert card_contract_missing_inputs(repo, card) == ()
