from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from aiworkhub import core, roadmap_store, task_retention, task_store

_STAMP = "2025-01-01T00:00:00+00:00"


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    return repo


def _insert_task(repo: Path, task_id: str, *, archived: bool) -> None:
    connection = sqlite3.connect(task_store.canonical_db_path(repo))
    try:
        connection.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,"
            "created_at,updated_at,completed_at,origin_thread_id,archived_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, "runner", "topic", "finished", "done",
                json.dumps({"task_id": task_id}), _STAMP, _STAMP, _STAMP, "thread",
                _STAMP if archived else "",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _item(repo: Path, title: str, *task_ids: str) -> dict:
    item = roadmap_store.add_item(
        repo,
        title=title,
        outcome=f"Deliver {title}",
        acceptance=[f"{title} is verified"],
        evidence_refs=["docs/PRODUCT_ROADMAP.md"],
    )
    for task_id in task_ids:
        item = roadmap_store.link_task(repo, item["id"], task_id)
    return item


def _quarantine(repo: Path) -> dict:
    preview = task_retention.preview(repo, older_than_days=30)
    return task_retention.quarantine(
        repo,
        preview_digest=preview["preview_digest"],
        older_than_days=30,
        confirm=True,
    )


def _row(item_id: str) -> dict:
    snapshot = core.roadmap_snapshot()
    return next(row for row in snapshot["items"] if row["id"] == item_id)


def _task_count(repo: Path) -> int:
    connection = sqlite3.connect(task_store.canonical_db_path(repo))
    try:
        return int(connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
    finally:
        connection.close()


def test_quarantine_records_status_and_restore_lets_live_status_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _insert_task(repo, "TASK_ONE", archived=True)
    item = _item(repo, "Outcome", "TASK_ONE", "TASK_NEVER_EXISTED")

    batch = _quarantine(repo)

    row = _row(item["id"])
    assert row["tasks"] == [
        {"task_id": "TASK_ONE", "status": "purged:finished"},
        {"task_id": "TASK_NEVER_EXISTED", "status": "missing"},
    ]
    assert (row["linked_total"], row["accepted"], row["unresolved"]) == (2, 1, 0)
    events = roadmap_store.list_events(repo, item["id"])
    retired = [e for e in events if e["event"] == "task_retired"]
    assert [e["detail"] for e in retired] == [
        {"task_id": "TASK_ONE", "status": "finished"}
    ]

    task_retention.restore(repo, batch_id=batch["batch_id"], confirm=True)

    row = _row(item["id"])
    assert row["tasks"][0] == {"task_id": "TASK_ONE", "status": "archived"}
    assert row["tasks"][1] == {"task_id": "TASK_NEVER_EXISTED", "status": "missing"}
    assert (row["linked_total"], row["accepted"], row["unresolved"]) == (2, 1, 0)


def test_unlinked_task_quarantines_without_roadmap_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _insert_task(repo, "TASK_UNLINKED", archived=True)

    assert not roadmap_store._db_path(repo).exists()
    assert _quarantine(repo)["quarantined"] == 1
    assert not roadmap_store._db_path(repo).exists()
    assert _task_count(repo) == 0

    other = _item(repo, "Other", "TASK_ELSEWHERE")
    _insert_task(repo, "TASK_UNLINKED_TWO", archived=True)
    assert _quarantine(repo)["quarantined"] == 1
    assert [e["event"] for e in roadmap_store.list_events(repo, other["id"])] == [
        "task_linked",
        "created",
    ]


def test_record_task_retired_touches_every_linking_item_and_counts_them(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    first = _item(repo, "First", "TASK_SHARED")
    second = _item(repo, "Second", "TASK_SHARED", "TASK_OTHER")
    third = _item(repo, "Third", "TASK_OTHER")

    assert roadmap_store.record_task_retired(repo, "TASK_SHARED", "finished") == 2
    assert roadmap_store.record_task_retired(repo, "TASK_NOBODY", "finished") == 0
    for item, expected in ((first, 1), (second, 1), (third, 0)):
        events = roadmap_store.list_events(repo, item["id"])
        assert sum(e["event"] == "task_retired" for e in events) == expected
    assert roadmap_store.retired_task_statuses(
        repo, ["TASK_SHARED", "TASK_OTHER"]
    ) == {"TASK_SHARED": "finished"}


def test_latest_retired_status_wins(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _item(repo, "Outcome", "TASK_ONE")
    roadmap_store.record_task_retired(repo, "TASK_ONE", "blocked")
    roadmap_store.record_task_retired(repo, "TASK_ONE", "finished")

    assert roadmap_store.retired_task_statuses(repo, ["TASK_ONE"]) == {
        "TASK_ONE": "finished"
    }


def test_snapshot_counts_accepted_and_unresolved_across_live_and_retired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _insert_task(repo, "TASK_LIVE_DONE", archived=False)
    item = _item(
        repo, "Outcome", "TASK_LIVE_DONE", "TASK_GONE_DONE", "TASK_GONE_BLOCKED", "TASK_LOST"
    )
    roadmap_store.record_task_retired(repo, "TASK_GONE_DONE", "finished")
    roadmap_store.record_task_retired(repo, "TASK_GONE_BLOCKED", "blocked")

    row = _row(item["id"])

    assert row["tasks"] == [
        {"task_id": "TASK_LIVE_DONE", "status": "finished"},
        {"task_id": "TASK_GONE_DONE", "status": "purged:finished"},
        {"task_id": "TASK_GONE_BLOCKED", "status": "purged:blocked"},
        {"task_id": "TASK_LOST", "status": "missing"},
    ]
    assert (row["linked_total"], row["accepted"], row["unresolved"]) == (4, 2, 1)
    assert row["status"] == "proposed"


def test_snapshot_reads_retired_statuses_in_one_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _insert_task(repo, "TASK_LIVE", archived=False)
    _item(repo, "First", "TASK_LIVE", "TASK_A", "TASK_B")
    _item(repo, "Second", "TASK_C", "TASK_A")
    calls: list[list[str]] = []
    real = roadmap_store.retired_task_statuses

    def counted(root, task_ids):
        calls.append(list(task_ids))
        return real(root, task_ids)

    monkeypatch.setattr(roadmap_store, "retired_task_statuses", counted)
    core.roadmap_snapshot()

    assert calls == [["TASK_A", "TASK_B", "TASK_C"]]


def test_snapshot_skips_retired_lookup_when_every_task_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _insert_task(repo, "TASK_LIVE", archived=False)
    _item(repo, "First", "TASK_LIVE")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no retired lookup needed when every task is live")

    monkeypatch.setattr(roadmap_store, "retired_task_statuses", forbidden)
    assert core.roadmap_snapshot()["items"][0]["tasks"] == [
        {"task_id": "TASK_LIVE", "status": "finished"}
    ]


def test_quarantine_aborts_and_keeps_tasks_when_roadmap_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _insert_task(repo, "TASK_ONE", archived=True)
    item = _item(repo, "Outcome", "TASK_ONE")

    def fail(*_args, **_kwargs):
        raise RuntimeError("roadmap write failed")

    monkeypatch.setattr(roadmap_store, "record_tasks_retired", fail)
    with pytest.raises(
        task_retention.TaskRetentionError, match="task_retention_roadmap_record_failed"
    ):
        _quarantine(repo)

    assert _task_count(repo) == 1
    assert task_retention.list_batches(repo)["count"] == 0
    assert _row(item["id"])["tasks"] == [{"task_id": "TASK_ONE", "status": "archived"}]


def test_live_archived_finished_task_still_counts_as_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _insert_task(repo, "TASK_ARCHIVED_DONE", archived=True)
    item = _item(repo, "Outcome", "TASK_ARCHIVED_DONE")

    row = _row(item["id"])

    assert row["tasks"] == [{"task_id": "TASK_ARCHIVED_DONE", "status": "archived"}]
    assert (row["linked_total"], row["accepted"], row["unresolved"]) == (1, 1, 0)


def test_unlinked_quarantine_does_not_need_the_roadmap_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _item(repo, "Other", "TASK_ELSEWHERE")
    _insert_task(repo, "TASK_UNLINKED", archived=True)
    holder = sqlite3.connect(roadmap_store._db_path(repo), timeout=0)
    try:
        holder.execute("BEGIN IMMEDIATE")
        assert _quarantine(repo)["quarantined"] == 1
    finally:
        holder.rollback()
        holder.close()

    assert _task_count(repo) == 0


def test_retired_statuses_without_a_roadmap_db_create_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    assert roadmap_store.retired_task_statuses(repo, ["TASK_ONE"]) == {}
    assert not roadmap_store._db_path(repo).exists()
