from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path

import pytest

from aiworkhub import core, needfix_ingest, roadmap_store, task_store

_TASK_COUNT = 25
_STATUSES = ("pending", "finished", "processing", "review", "finished")


def _task_id(index: int) -> str:
    return f"task-{index:02d}"


def _seed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    db_path = task_store.canonical_db_path(repo)
    conn = sqlite3.connect(db_path)
    try:
        for index in range(_TASK_COUNT):
            # 7 is coprime with 25: rowid order and updated_at order disagree.
            rank = (index * 7) % _TASK_COUNT
            stamp = f"2026-02-{rank + 1:02d}T00:00:00+00:00"
            conn.execute(
                "INSERT INTO tasks (task_id, status, worker_status, card_json, "
                "created_at, updated_at) VALUES (?, ?, 'unclaimed', '{}', ?, ?)",
                (_task_id(index), _STATUSES[index % len(_STATUSES)], stamp, stamp),
            )
        conn.commit()
    finally:
        conn.close()
    return repo


def _count_calls(monkeypatch: pytest.MonkeyPatch, owner, name: str) -> list[tuple]:
    calls: list[tuple] = []
    original = getattr(owner, name)

    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, counted)
    return calls


def _calls_for_repo(calls: list[tuple], repo: Path) -> list[tuple]:
    """The recorded calls whose first argument (the repository root) is ``repo``."""
    target = Path(repo).resolve()
    own: list[tuple] = []
    for args in calls:
        try:
            if args and Path(args[0]).resolve() == target:
                own.append(args)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
    return own


def test_roadmap_snapshot_incomplete_snapshot_reads_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _seed_repo(tmp_path)
    ghosts = ["ghost-one", "ghost-two"]
    linked = [_task_id(i) for i in range(_TASK_COUNT)] + ghosts
    for position in range(5):
        item = roadmap_store.add_item(
            repo,
            title=f"Item {position}",
            outcome=f"Deliver {position}",
            acceptance=["verified"],
            evidence_refs=["docs/PRODUCT_ROADMAP.md"],
        )
        for task_id in linked[position::5]:
            roadmap_store.link_task(repo, item["id"], task_id)

    expected: dict[str, str] = {}
    for task_id in linked:
        card = task_store.get_task(repo, task_id)
        expected[task_id] = (
            "missing" if card is None else task_store.canonical_status(card)
        )

    root_calls: list[int] = []

    def counted_root() -> Path:
        root_calls.append(1)
        return repo

    monkeypatch.setattr(core, "repo_root", counted_root)
    readiness_calls = _count_calls(monkeypatch, task_store, "storage_readiness")
    get_calls = _count_calls(monkeypatch, task_store, "get_task")

    snapshot = core.roadmap_snapshot(
        task_cards_snapshot=(), task_cards_snapshot_complete=False
    )

    statuses = {
        task["task_id"]: task["status"]
        for row in snapshot["items"]
        for task in row["tasks"]
    }
    assert statuses == expected
    assert statuses["ghost-one"] == "missing"
    assert len(root_calls) == 1
    assert len(readiness_calls) <= 1
    assert get_calls == []


def test_resolve_active_state_hooks_incomplete_snapshot_reads_readiness_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _seed_repo(tmp_path)
    readiness_calls = _count_calls(monkeypatch, task_store, "storage_readiness")
    get_calls: list[tuple] = []
    original_get = task_store.get_task

    def counted_get(*args, **kwargs):
        get_calls.append(args)
        return original_get(*args, **kwargs)

    monkeypatch.setattr(task_store, "get_task", counted_get)

    get_task_fn, _status_fn, _list_fn, reason = (
        needfix_ingest._resolve_active_state_hooks(
            repo, task_cards_snapshot=(), task_cards_snapshot_complete=False
        )
    )

    assert reason is None
    for index in range(_TASK_COUNT):
        card = get_task_fn(_task_id(index))
        assert card is not None and card["task_id"] == _task_id(index)

    # NF-2026-02337: the counters wrap process-wide attributes, so a thread
    # another test leaked into this worker (one reading the real repository)
    # lands in them too. Only this test's own repository is counted.
    assert len(_calls_for_repo(get_calls, repo)) == _TASK_COUNT
    assert len(_calls_for_repo(readiness_calls, repo)) == 1


def test_readiness_counter_ignores_reads_of_other_repositories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forced ordering for NF-2026-02337: a foreign thread reads another
    repository's readiness while this repository's hooks are being resolved.
    The unfiltered counter sees two reads (the old assertion's failure); the
    per-repository count stays exactly one, and a second read of this
    repository would still be counted."""
    repo = _seed_repo(tmp_path)
    other = tmp_path / "other_repo"
    other.mkdir()
    readiness_calls = _count_calls(monkeypatch, task_store, "storage_readiness")

    def foreign_reader() -> None:
        try:
            task_store.storage_readiness(other)
        except Exception:  # an unbootstrapped repository may refuse; the call is what counts
            pass

    reader = threading.Thread(target=foreign_reader)
    reader.start()
    reader.join(timeout=30)
    assert not reader.is_alive()

    _get, _status, _list, reason = needfix_ingest._resolve_active_state_hooks(
        repo, task_cards_snapshot=(), task_cards_snapshot_complete=False
    )

    assert reason is None
    assert len(readiness_calls) >= 2, "the foreign read reached the process-wide counter"
    assert len(_calls_for_repo(readiness_calls, repo)) == 1
    assert len(_calls_for_repo(readiness_calls, other)) == 1


def test_list_task_cards_matches_old_query_and_plans_a_rowid_lookup(
    tmp_path: Path,
) -> None:
    repo = _seed_repo(tmp_path)
    db_path = task_store.canonical_db_path(repo)
    limit = 10

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        old_rows = conn.execute(
            "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        plan = conn.execute(
            "EXPLAIN QUERY PLAN " + task_store._LIST_TASK_CARDS_SQL, (limit,)
        ).fetchall()
    finally:
        conn.close()

    new_cards = task_store.list_task_cards(repo, limit=limit)
    assert new_cards == [task_store._decode_task_card(row) for row in old_rows]
    assert len(new_cards) == limit

    details = [(row[0], row[1], row[3]) for row in plan]
    assert any(
        detail.startswith("SEARCH") and "rowid=?" in detail
        for _id, _parent, detail in details
    )
    scans = [
        (node, parent)
        for node, parent, detail in details
        if re.match(r"SCAN (TABLE )?tasks\b", detail)
    ]
    assert len(scans) == 1
    assert scans[0][1] != 0


def test_not_ready_readiness_raises_for_point_and_batch_reads(
    tmp_path: Path,
) -> None:
    repo = _seed_repo(tmp_path)
    not_ready = task_store.StorageReadiness(
        False, "canonical_db_missing", "repo-id", str(tmp_path / "missing.sqlite")
    )

    with pytest.raises(task_store.StorageNotReadyError, match="canonical_db_missing"):
        task_store.get_task(repo, _task_id(0), readiness=not_ready)
    with pytest.raises(task_store.StorageNotReadyError, match="canonical_db_missing"):
        task_store.get_tasks(repo, [_task_id(0)], readiness=not_ready)


def test_get_task_with_ready_readiness_equals_default_without_readiness_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _seed_repo(tmp_path)
    ready = task_store.storage_readiness(repo)
    assert ready.ready
    default = task_store.get_task(repo, _task_id(3))

    readiness_calls = _count_calls(monkeypatch, task_store, "storage_readiness")
    supplied = task_store.get_task(repo, _task_id(3), readiness=ready)

    assert supplied == default
    assert supplied is not None
    assert readiness_calls == []
    assert task_store.get_task(repo, "ghost", readiness=ready) is None


def test_get_tasks_chunks_dedupes_and_checks_readiness_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _seed_repo(tmp_path)
    real_ids = [_task_id(i) for i in range(_TASK_COUNT)]
    expected = {task_id: task_store.get_task(repo, task_id) for task_id in real_ids}

    monkeypatch.setattr(task_store, "_GET_TASKS_CHUNK", 7)
    readiness_calls = _count_calls(monkeypatch, task_store, "storage_readiness")

    requested = [*real_ids, real_ids[3], "ghost-a", real_ids[0], "ghost-b"]
    found = task_store.get_tasks(repo, requested)

    assert found == expected
    assert "ghost-a" not in found and "ghost-b" not in found
    assert len(readiness_calls) == 1
    assert task_store.get_tasks(repo, []) == {}
    assert len(readiness_calls) == 1
