"""Tests for recipe_miner (NF-2026-01425): mining recurring accepted-card
validation commands into proposed tool recipes, mirroring the skill-mining
coverage added for NF-2026-01412.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from _taskdb_compat import upsert_card
from aiworkhub import manager_recipe_tools, recipe_miner, task_store, tool_recipes, tool_recipes_store


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    return root


def _card(root: Path, *, task_id: str, status: str, worker_status: str, runner: str,
          validation, accepted_at: str = "") -> None:
    card = {
        "task_id": task_id,
        "runner": runner,
        "topic": "code",
        "mode": "edit",
        "status": status,
        "worker_status": worker_status,
        "validation": validation,
    }
    if accepted_at:
        card["accepted_at"] = accepted_at
    con = sqlite3.connect(str(task_store.canonical_db_path(root)))
    con.row_factory = sqlite3.Row
    try:
        upsert_card(con, card)
    finally:
        con.close()


def _accepted_card(root: Path, *, task_id: str, runner: str, validation,
                    accepted_at: str = "2026-01-01T00:00:00+00:00") -> None:
    _card(
        root, task_id=task_id, status="finished", worker_status="done",
        runner=runner, validation=validation, accepted_at=accepted_at,
    )


def _seed_node_check_cluster(root: Path, *, count: int = 3) -> None:
    for index in range(count):
        letter = "abcdefgh"[index]
        _accepted_card(
            root, task_id=f"card-{letter}", runner=f"runner{index % 2}",
            validation=[f"node --check vscode-extension/{letter}.js"],
        )


def test_three_accepted_cards_two_runners_yield_one_node_check_candidate(tmp_path):
    root = _repo(tmp_path)
    _seed_node_check_cluster(root)

    report = recipe_miner.mine(root)

    assert report["corpus"]["cards"] == 3
    assert report["corpus"]["commands"] == 3
    assert len(report["candidates"]) == 1
    candidate = report["candidates"][0]
    assert candidate["head"] == ["node", "--check"]
    assert candidate["provenance"]["distinct_cards"] == 3
    assert candidate["provenance"]["distinct_actors"] == 2

    draft = candidate["draft"]
    recipe = tool_recipes.recipe_from_mapping(draft)
    literal_head: list[str] = []
    for token in recipe.argv:
        if isinstance(token, tool_recipes.ArgvLiteral):
            literal_head.append(token.text)
        else:
            break
    assert literal_head == ["node", "--check"]
    path_slots = [p for p in recipe.parameters if p.type is tool_recipes.ParamType.LIST]
    assert len(path_slots) == 1
    assert path_slots[0].item_type is tool_recipes.ParamType.PATH

    put_result = tool_recipes_store.put_proposal(root, draft, candidate["provenance"])
    assert put_result["status"] == "proposed"
    stored = tool_recipes_store.list_proposals(root)
    assert len(stored) == 1
    assert stored[0]["recipe_id"] == candidate["recipe_id"]
    assert stored[0]["status"] == "proposed"


@pytest.mark.parametrize("command,expected_reason", [
    ("python -m pytest; rm -rf /", recipe_miner.REASON_SHELL_SYNTAX),
    ("echo $(whoami) && python -m pytest", recipe_miner.REASON_SHELL_SYNTAX),
    ("FOO=bar python -m pytest -q", recipe_miner.REASON_ENV_ASSIGNMENT),
    ('python -m pytest "unterminated', recipe_miner.REASON_UNPARSEABLE),
    ("run a.py --only b.py", recipe_miner.REASON_INTERLEAVED_PATHS),
])
def test_unsafe_commands_are_refused_with_distinct_reason_codes(tmp_path, command, expected_reason):
    root = _repo(tmp_path)
    for index in range(3):
        letter = "abc"[index]
        _accepted_card(
            root, task_id=f"card-{letter}", runner=f"runner{index % 2}",
            validation=[command],
        )

    report = recipe_miner.mine(root)

    assert report["candidates"] == []
    assert report["refused"][expected_reason] == 3


def test_two_distinct_cards_is_below_recurrence(tmp_path):
    root = _repo(tmp_path)
    _seed_node_check_cluster(root, count=2)

    report = recipe_miner.mine(root)

    assert report["candidates"] == []
    assert report["refused"][recipe_miner.REASON_BELOW_RECURRENCE] == 1


def test_one_actor_across_three_cards_is_below_recurrence(tmp_path):
    root = _repo(tmp_path)
    for letter in "abc":
        _accepted_card(
            root, task_id=f"card-{letter}", runner="solo-runner",
            validation=["node --check vscode-extension/x.js"],
        )

    report = recipe_miner.mine(root)

    assert report["candidates"] == []
    assert report["refused"][recipe_miner.REASON_BELOW_RECURRENCE] == 1


def test_python_pytest_with_paths_is_covered_by_a_registered_recipe(tmp_path):
    root = _repo(tmp_path)
    for index, letter in enumerate("abc"):
        _accepted_card(
            root, task_id=f"card-{letter}", runner=f"runner{index % 2}",
            validation=[f"python -m pytest -q tests/test_{letter}.py"],
        )
    pytest_recipe = next(
        r for r in manager_recipe_tools.CANONICAL_RECIPES
        if r.id == "aiworkhub.validation.pytest"
    )
    tool_recipes_store.put_recipe(root, pytest_recipe)

    report = recipe_miner.mine(root)

    assert report["candidates"] == []
    assert report["refused"][recipe_miner.REASON_COVERED_BY_REGISTERED] == 1


def test_non_accepted_cards_contribute_nothing(tmp_path):
    root = _repo(tmp_path)
    _card(root, task_id="pending-1", status="pending", worker_status="unclaimed",
          runner="runner0", validation=["node --check vscode-extension/a.js"])
    _card(root, task_id="review-1", status="review", worker_status="review",
          runner="runner1", validation=["node --check vscode-extension/a.js"])
    _card(root, task_id="finished-no-accept", status="finished", worker_status="done",
          runner="runner0", validation=["node --check vscode-extension/a.js"])

    report = recipe_miner.mine(root)

    assert report["corpus"]["cards"] == 0
    assert report["candidates"] == []
    assert report["refused"] == {}


def test_registry_is_unchanged_by_mining_and_proposing(tmp_path):
    root = _repo(tmp_path)
    for index, letter in enumerate("abc"):
        _accepted_card(
            root, task_id=f"card-{letter}", runner=f"runner{index % 2}",
            validation=[f"python -m pytest tests/test_{letter}.py"],
        )
    pytest_recipe = next(
        r for r in manager_recipe_tools.CANONICAL_RECIPES
        if r.id == "aiworkhub.validation.pytest"
    )
    tool_recipes_store.put_recipe(root, pytest_recipe)
    before = tool_recipes_store.list_recipes(root)

    _seed_node_check_cluster(root, count=3)
    report = recipe_miner.mine(root)
    for candidate in report["candidates"]:
        tool_recipes_store.put_proposal(root, candidate["draft"], candidate["provenance"])

    after = tool_recipes_store.list_recipes(root)
    assert after == before
    assert [r.id for r in after] == ["aiworkhub.validation.pytest"]


def test_remining_the_same_template_reports_already_proposed_with_same_recipe_id(tmp_path):
    root = _repo(tmp_path)
    _seed_node_check_cluster(root, count=3)

    first_report = recipe_miner.mine(root)
    first_candidate = first_report["candidates"][0]
    first_put = tool_recipes_store.put_proposal(
        root, first_candidate["draft"], first_candidate["provenance"]
    )
    assert first_put["status"] == "proposed"

    second_report = recipe_miner.mine(root)
    second_candidate = second_report["candidates"][0]
    assert second_candidate["recipe_id"] == first_candidate["recipe_id"]
    second_put = tool_recipes_store.put_proposal(
        root, second_candidate["draft"], second_candidate["provenance"]
    )
    assert second_put["status"] == "already_proposed"
    assert second_put["recipe_id"] == first_candidate["recipe_id"]

    stored = tool_recipes_store.list_proposals(root)
    assert len(stored) == 1


def test_mined_head_shorter_than_registered_head_is_not_covered(tmp_path):
    root = _repo(tmp_path)
    for index, letter in enumerate("abc"):
        _accepted_card(
            root, task_id=f"card-{letter}", runner=f"runner{index % 2}",
            validation=["python scripts/render.py"],
        )
    pytest_recipe = next(
        r for r in manager_recipe_tools.CANONICAL_RECIPES
        if r.id == "aiworkhub.validation.pytest"
    )
    tool_recipes_store.put_recipe(root, pytest_recipe)

    report = recipe_miner.mine(root)

    assert len(report["candidates"]) == 1
    assert report["refused"].get(recipe_miner.REASON_COVERED_BY_REGISTERED, 0) == 0


def test_multiple_contiguous_paths_collapse_into_one_slot(tmp_path):
    root = _repo(tmp_path)
    for index, letter in enumerate("abc"):
        _accepted_card(
            root, task_id=f"card-{letter}", runner=f"runner{index % 2}",
            validation=["python -m pytest tests/a.py tests/b.py"],
        )

    report = recipe_miner.mine(root)

    assert len(report["candidates"]) == 1
    candidate = report["candidates"][0]
    recipe = tool_recipes.recipe_from_mapping(candidate["draft"])
    slot_tokens = [token for token in recipe.argv if isinstance(token, tool_recipes.ArgvSlot)]
    assert len(slot_tokens) == 1
    assert len(recipe.parameters) == 1
