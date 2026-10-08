"""Tests for NF-2026-01425: ``recipe_miner.auto_mine_and_propose`` re-mining
and idempotency, mirroring test_skill_auto_mining.py's auto-mining coverage
for NF-2026-01412.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from _taskdb_compat import upsert_card
from aiworkhub import recipe_miner, task_store, tool_recipes_store


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    return root


def _accepted_card(root: Path, *, task_id: str, runner: str, validation) -> None:
    card = {
        "task_id": task_id,
        "runner": runner,
        "topic": "code",
        "mode": "edit",
        "status": "finished",
        "worker_status": "done",
        "accepted_at": "2026-01-01T00:00:00+00:00",
        "validation": validation,
    }
    con = sqlite3.connect(str(task_store.canonical_db_path(root)))
    con.row_factory = sqlite3.Row
    try:
        upsert_card(con, card)
    finally:
        con.close()


def _seed_cluster(root: Path, *, count: int = 3) -> None:
    for index in range(count):
        letter = "abcdefgh"[index]
        _accepted_card(
            root, task_id=f"card-{letter}", runner=f"runner{index % 2}",
            validation=[f"node --check vscode-extension/{letter}.js"],
        )


def test_remining_after_a_fourth_card_updates_provenance_not_a_second_proposal(tmp_path):
    root = _repo(tmp_path)
    _seed_cluster(root, count=3)

    first = recipe_miner.auto_mine_and_propose(root)
    assert first["candidates"] == 1
    assert len(first["proposed"]) == 1
    assert first["already_proposed"] == []
    recipe_id = first["proposed"][0]

    _accepted_card(
        root, task_id="card-d", runner="runner0",
        validation=["node --check vscode-extension/d.js"],
    )

    second = recipe_miner.auto_mine_and_propose(root)
    assert second["candidates"] == 1
    assert second["proposed"] == []
    assert second["already_proposed"] == [recipe_id]

    stored = tool_recipes_store.list_proposals(root)
    assert len(stored) == 1
    assert stored[0]["recipe_id"] == recipe_id
    assert stored[0]["provenance"]["distinct_cards"] == 4


def test_running_the_hook_twice_proposes_nothing_new(tmp_path):
    root = _repo(tmp_path)
    _seed_cluster(root, count=3)

    first = recipe_miner.auto_mine_and_propose(root)
    second = recipe_miner.auto_mine_and_propose(root)

    assert first["proposed"] != []
    assert second["proposed"] == []
    assert second["already_proposed"] == first["proposed"]
    assert len(tool_recipes_store.list_proposals(root)) == 1


def test_auto_mine_and_propose_never_raises_on_a_forced_mining_exception(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _seed_cluster(root, count=3)

    def _raise(*args, **kwargs):
        raise RuntimeError("forced_failure")

    monkeypatch.setattr(recipe_miner, "mine", _raise)
    result = recipe_miner.auto_mine_and_propose(root)

    assert result["candidates"] == 0
    assert result["proposed"] == []
    assert result["already_proposed"] == []
    assert result["refused_by_reason"]["mining"].startswith("recipe_mining_failed:")
    assert isinstance(result["elapsed_ms"], (int, float))
    assert tool_recipes_store.list_proposals(root) == []
