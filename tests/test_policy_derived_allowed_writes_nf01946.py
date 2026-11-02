"""NF-2026-01946: a card that writes the tool-use policy module also writes
every file rendered from it."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import agent_tool_instructions as instr
from aiworkhub import core, task_store
from aiworkhub.policy_derived_writes import POLICY_SOURCE, expand_policy_derived_writes

REPO = Path(__file__).resolve().parents[1]
DERIVED = list(instr.DERIVED_ARTIFACTS)
OTHER = "src/aiworkhub/example_target.py"


@pytest.fixture
def manager_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    task_store.initialize_repository(root)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {"provider": "claude", "session_id": "SESSION-A", "route_state": "verified"},
    )
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **k: None)
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda _runner: (True, "test_route"))
    return root


def _create_args(task_id: str, allowed_writes: list[str], **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = dict(
        task_id=task_id,
        title="policy derived writes",
        runner="claude-worker",
        topic="task_create",
        objective="exercise create-time policy-derived allowed_writes",
        acceptance=["allowed_writes carries every policy-derived artifact"],
        allowed_writes=allowed_writes,
        required_outputs=[allowed_writes[-1]] if allowed_writes else None,
        validation=["pytest -q tests/test_agent_tool_instructions.py"],
        custom_template_escape="audited_custom_unclassified",
        callback_required=False,
    )
    args.update(overrides)
    return args


def _stored_card(task_id: str) -> dict[str, Any]:
    conn = core._canonical_connect()
    try:
        row = conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None, task_id
    card = json.loads(row[0])
    assert isinstance(card, dict)
    return card


def _tracked(path: str) -> bool:
    git = shutil.which("git")
    if git is None:
        return False
    result = subprocess.run(
        [git, "-C", str(REPO), "ls-files", "--error-unmatch", "--", path],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def test_derived_artifacts_come_from_providers_and_name_every_rendered_file() -> None:
    providers = list(instr.PROVIDERS)
    assert DERIVED[: len(providers)] == providers
    assert DERIVED[len(providers):] == [
        "docs/AIWORKHUB_TOOL_USE_POLICY.md",
        "eval/aiworkhub_agent_tool_instruction_generator_b816_v2.json",
        "eval/aiworkhub_agent_tool_instruction_activation_b821_v1.json",
    ]
    assert len(set(DERIVED)) == len(DERIVED)
    assert POLICY_SOURCE not in DERIVED
    assert Path(instr.__file__).resolve().as_posix().endswith(POLICY_SOURCE)


def test_every_derived_artifact_exists_in_the_repository() -> None:
    # A sparse worker tree holds only the card's declared paths, so a path that
    # is absent on disk must still be a tracked file of this repository.
    missing = [p for p in DERIVED if not (REPO / p).is_file() and not _tracked(p)]
    assert missing == []


def test_helper_appends_missing_paths_after_the_callers_order() -> None:
    writes, appended = expand_policy_derived_writes(
        [OTHER, "CLAUDE.md", POLICY_SOURCE], read_only=False
    )
    expected_appended = [p for p in DERIVED if p != "CLAUDE.md"]
    assert appended == expected_appended
    assert writes == [OTHER, "CLAUDE.md", POLICY_SOURCE, *expected_appended]
    assert len(set(writes)) == len(writes)


def test_helper_leaves_other_and_read_only_cards_unchanged() -> None:
    assert expand_policy_derived_writes([OTHER], read_only=False) == ([OTHER], [])
    assert expand_policy_derived_writes([], read_only=True) == ([], [])
    assert expand_policy_derived_writes([POLICY_SOURCE], read_only=True) == (
        [POLICY_SOURCE],
        [],
    )
    everything = [POLICY_SOURCE, *DERIVED]
    assert expand_policy_derived_writes(everything, read_only=False) == (everything, [])


def test_create_task_stores_every_derived_artifact(manager_repo: Path) -> None:
    created = core.create_task(**_create_args("NF01946-POLICY", [POLICY_SOURCE]))
    assert created.get("created") is True, created

    card = _stored_card("NF01946-POLICY")
    assert card["allowed_writes"] == [POLICY_SOURCE, *DERIVED]
    assert card["allowed_writes_derived"] == DERIVED

    # The expansion is deterministic, so an identical retry reconciles.
    retry = core.create_task(**_create_args("NF01946-POLICY", [POLICY_SOURCE]))
    assert retry.get("reconciled") is True, retry
    assert retry.get("receipt_state") == "existing_identical"


def test_create_task_keeps_a_listed_path_in_place(manager_repo: Path) -> None:
    writes = ["AGENTS.md", POLICY_SOURCE]
    created = core.create_task(
        **_create_args("NF01946-LISTED", writes, required_outputs=[POLICY_SOURCE])
    )
    assert created.get("created") is True, created

    card = _stored_card("NF01946-LISTED")
    appended = [p for p in DERIVED if p != "AGENTS.md"]
    assert card["allowed_writes"] == [*writes, *appended]
    assert card["allowed_writes_derived"] == appended


def test_create_task_without_the_policy_module_is_unchanged(manager_repo: Path) -> None:
    created = core.create_task(**_create_args("NF01946-OTHER", [OTHER]))
    assert created.get("created") is True, created

    card = _stored_card("NF01946-OTHER")
    assert card["allowed_writes"] == [OTHER]
    assert "allowed_writes_derived" not in card


def test_read_only_card_is_unchanged(manager_repo: Path) -> None:
    created = core.create_task(
        **_create_args("NF01946-READONLY", [], read_only=True, required_outputs=None)
    )
    assert created.get("created") is True, created

    card = _stored_card("NF01946-READONLY")
    assert card["allowed_writes"] == []
    assert "allowed_writes_derived" not in card
