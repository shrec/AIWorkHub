from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from aiworkhub import core, roadmap_store, task_store

_STAMP = "2025-01-01T00:00:00+00:00"


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    return repo


def _task(repo: Path, task_id: str, status: str, superseded_by: str = "") -> None:
    card = {"task_id": task_id}
    if superseded_by:
        card["superseded_by"] = superseded_by
    finished = status == "finished"
    connection = sqlite3.connect(task_store.canonical_db_path(repo))
    try:
        connection.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,"
            "created_at,updated_at,completed_at,origin_thread_id,archived_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, "runner", "topic", status, "done" if finished else "",
                json.dumps(card), _STAMP, _STAMP, _STAMP if finished else "",
                "thread", "",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _item(repo: Path, *task_ids: str) -> dict:
    item = roadmap_store.add_item(
        repo,
        title="Outcome",
        outcome="Deliver Outcome",
        acceptance=["Outcome is verified"],
        evidence_refs=["docs/PRODUCT_ROADMAP.md"],
    )
    for task_id in task_ids:
        item = roadmap_store.link_task(repo, item["id"], task_id)
    return item


def _row(item_id: str) -> dict:
    return next(r for r in core.roadmap_snapshot()["items"] if r["id"] == item_id)


def _complete(item_id: str) -> dict:
    return core.roadmap_transition(item_id, "completed", reason="verified")


def _approve(item_id: str) -> None:
    for status in ("approved", "in_progress"):
        core.roadmap_transition(item_id, status, reason="start")


def test_completion_accepts_superseded_task_with_finished_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _task(repo, "r1", "pending", superseded_by="r2")
    _task(repo, "r2", "finished")
    item = _item(repo, "r1", "r2")
    _approve(item["id"])

    assert _complete(item["id"])["status"] == "completed"


@pytest.mark.parametrize("successor", ["pending", None])
def test_completion_refuses_unfinished_or_missing_successor_and_names_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, successor: str | None
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _task(repo, "r1", "pending", superseded_by="r2")
    if successor:
        _task(repo, "r2", successor)
    item = _item(repo, "r1")
    _approve(item["id"])

    with pytest.raises(roadmap_store.RoadmapConflictError) as raised:
        _complete(item["id"])

    message = str(raised.value)
    assert message.startswith("roadmap completion blocked by task authority:")
    assert "r1" in message and "superseded->r2" in message


def test_completion_refuses_cyclic_chain_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _task(repo, "r1", "pending", superseded_by="r2")
    _task(repo, "r2", "pending", superseded_by="r1")
    item = _item(repo, "r1")
    _approve(item["id"])

    with pytest.raises(roadmap_store.RoadmapConflictError) as raised:
        _complete(item["id"])

    assert "superseded->r2->r1:cycle" in str(raised.value)


def test_chain_follows_multiple_hops_and_bounds_long_chains() -> None:
    cards = {
        f"t{i}": {"task_id": f"t{i}", "status": "pending", "superseded_by": f"t{i + 1}"}
        for i in range(40)
    }
    cards["t40"] = {"task_id": "t40", "status": "finished"}

    def status(card):
        return str(card["status"])

    short = roadmap_store.linked_task_outcomes(
        ".", ["t35"], {"t35": cards["t35"]}, status, get_card=cards.get
    )
    assert short == [("t35", "superseded->t36->t37->t38->t39->t40:finished", "finished")]

    long = roadmap_store.linked_task_outcomes(
        ".", ["t0"], {"t0": cards["t0"]}, status, get_card=cards.get
    )
    assert long[0][2] == "pending"
    assert long[0][1].endswith(":too_long")


def test_omitting_get_card_keeps_original_behaviour() -> None:
    card = {"task_id": "r1", "status": "pending", "superseded_by": "r2"}
    resolved = roadmap_store.linked_task_outcomes(
        ".", ["r1"], {"r1": card}, lambda c: str(c["status"])
    )
    assert resolved == [("r1", "pending", "pending")]


def test_snapshot_separates_missing_from_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _task(repo, "T_DONE", "finished")
    _task(repo, "T_OPEN", "pending")
    item = _item(repo, "T_DONE", "T_GHOST", "T_OPEN")

    row = _row(item["id"])

    assert (row["linked_total"], row["accepted"], row["missing"], row["unresolved"]) == (
        3, 1, 1, 1
    )
    assert row["progress_unmeasurable"] is False


def test_snapshot_is_unmeasurable_only_when_every_link_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    item = _item(repo, "GHOST_ONE", "GHOST_TWO")
    empty = _item(repo)

    row = _row(item["id"])

    assert (row["accepted"], row["missing"], row["unresolved"]) == (0, 2, 0)
    assert row["progress_unmeasurable"] is True
    assert _row(empty["id"])["progress_unmeasurable"] is False


def test_snapshot_counts_superseded_link_with_finished_successor_as_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _task(repo, "r1", "pending", superseded_by="r2")
    _task(repo, "r2", "finished")
    item = _item(repo, "r1")

    row = _row(item["id"])

    assert row["tasks"] == [{"task_id": "r1", "status": "superseded->r2:finished"}]
    assert (row["accepted"], row["unresolved"], row["missing"]) == (1, 0, 0)
    assert row["progress_unmeasurable"] is False


def test_snapshot_is_unmeasurable_when_every_link_is_archived_without_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _task(repo, "T_DROPPED", "pending")
    connection = sqlite3.connect(task_store.canonical_db_path(repo))
    try:
        connection.execute("UPDATE tasks SET archived_at=? WHERE task_id='T_DROPPED'", (_STAMP,))
        connection.commit()
    finally:
        connection.close()
    item = _item(repo, "T_DROPPED")

    row = _row(item["id"])

    assert row["accepted"] == 0 and row["missing"] == 0
    assert row["progress_unmeasurable"] is True
