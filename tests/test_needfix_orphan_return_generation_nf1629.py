"""Orphan-return of a NeedFix advances reopen generation so it can reconvert.

An archived unaccepted card must not be reused. The orphan-return writer
counts as one durable reopen, and rows returned before that writer existed
derive the same generation from the event log without a manual generation edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import needfix_store


@pytest.fixture
def init_store(tmp_path: Path) -> Path:
    result = needfix_store.initialize_repository(tmp_path)
    assert result["initialized"] is True
    return tmp_path


def _accepted(repo: Path, title: str = "T") -> dict:
    record = needfix_store.capture_proposal(repo, title=title, description="D")
    needfix_store.triage_needfix(repo, record["id"])
    return needfix_store.accept_needfix(repo, record["id"])


def _create_task(card: dict) -> dict:
    return {"ok": True, "task_id": card["task_id"]}


def _insert_event(repo: Path, needfix_id: str, event: str) -> None:
    conn = needfix_store._connect(repo)
    try:
        conn.execute(
            "INSERT INTO needfix_events (needfix_id, event, detail_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (needfix_id, event, "{}", "2026-01-01T00:00:00+00:00"),
        )
    finally:
        conn.close()


def _set_generation(repo: Path, needfix_id: str, value: int) -> None:
    conn = needfix_store._connect(repo)
    try:
        conn.execute(
            "UPDATE needfix SET reopen_generation = ? WHERE id = ?",
            (value, needfix_id),
        )
    finally:
        conn.close()


def _raw_row(repo: Path, needfix_id: str) -> dict:
    conn = needfix_store._connect(repo)
    try:
        row = conn.execute(
            "SELECT * FROM needfix WHERE id = ?", (needfix_id,)
        ).fetchone()
        return {key: row[key] for key in row.keys()}
    finally:
        conn.close()


def _archived_card(task_id: str) -> dict:
    return {"id": task_id, "status": "archived", "archive_operation": "archived"}


def _reconcile(repo: Path) -> dict:
    return needfix_store.reconcile_orphaned_conversions(
        repo,
        get_task_fn=_archived_card,
        canonical_status_fn=lambda task: task["status"],
    )


def _convert_refusing_archived(repo: Path, needfix_id: str, archived_id: str) -> dict:
    """Stand in for needfix_convert's task_terminal:archived refusal."""

    def create_task(card: dict) -> dict:
        task_id = card["task_id"]
        if task_id == archived_id:
            raise RuntimeError(f"task_terminal:archived:{task_id}")
        return {"ok": True, "task_id": task_id}

    return needfix_store.convert_needfix(repo, needfix_id, create_task)


def test_orphan_return_raises_generation_and_reconverts(init_store: Path) -> None:
    record = _accepted(init_store)
    archived = needfix_store.convert_needfix(init_store, record["id"], _create_task)
    archived_id = archived["converted_task_id"]
    assert archived_id == f"needfix-{record['id']}"
    before = needfix_store.get_needfix(init_store, record["id"])["reopen_generation"]

    result = _reconcile(init_store)
    assert result["returned_to_accepted_count"] == 1

    returned = needfix_store.get_needfix(init_store, record["id"])
    assert returned["status"] == "accepted"
    assert returned["converted_task_id"] is None
    assert returned["reopen_generation"] == before + 1
    assert _raw_row(init_store, record["id"])["reopen_generation"] == before + 1

    reconverted = _convert_refusing_archived(init_store, record["id"], archived_id)
    assert reconverted["converted_task_id"] == f"needfix-{record['id']}-r1"
    assert reconverted["converted_task_id"] != archived_id


def test_orphan_return_of_reopened_card_does_not_reuse_archived_successor(
    init_store: Path,
) -> None:
    record = _accepted(init_store)
    needfix_store.convert_needfix(init_store, record["id"], _create_task)
    needfix_store.reopen_superseded_task_link(
        init_store,
        record["id"],
        get_task_fn=lambda task_id: {
            "id": task_id,
            "status": "archived",
            "archive_operation": "superseded",
        },
        canonical_status_fn=lambda task: task["status"],
        reason="Replacement task planned from the rebased residual.",
    )
    successor = needfix_store.convert_needfix(init_store, record["id"], _create_task)
    archived_id = successor["converted_task_id"]
    assert archived_id == f"needfix-{record['id']}-r1"

    result = _reconcile(init_store)
    assert result["returned_to_accepted_count"] == 1
    returned = needfix_store.get_needfix(init_store, record["id"])
    assert returned["status"] == "accepted"
    assert returned["reopen_generation"] == 2

    reconverted = _convert_refusing_archived(init_store, record["id"], archived_id)
    assert reconverted["converted_task_id"] == f"needfix-{record['id']}-r2"
    assert reconverted["converted_task_id"] != archived_id


def test_legacy_zero_generation_derives_from_orphan_return_event(init_store: Path) -> None:
    record = _accepted(init_store)
    _insert_event(init_store, record["id"], needfix_store.REOPEN_EVENT_ORPHAN_RETURN)
    assert _raw_row(init_store, record["id"])["reopen_generation"] == 0

    assert needfix_store.get_needfix(init_store, record["id"])["reopen_generation"] == 1

    needfix_store.initialize_repository(init_store)
    assert _raw_row(init_store, record["id"])["reopen_generation"] == 1
    reconverted = needfix_store.convert_needfix(init_store, record["id"], _create_task)
    assert reconverted["converted_task_id"] == f"needfix-{record['id']}-r1"
    assert reconverted["converted_task_id"] != f"needfix-{record['id']}"


def test_legacy_nonzero_generation_adds_only_orphan_return_events(init_store: Path) -> None:
    """Stored generation 1 plus one uncounted orphan event derives 2.

    The stored 1 already matches one previously counted reopen, so the
    additive backfill's only delta is the orphan-return event. A sibling
    without that event, and every other column, stay unchanged. A row whose
    stored generation already equals the durable count is not incremented
    again -- that is the post-writer state.
    """
    subject = _accepted(init_store)
    unchanged = _accepted(init_store, title="unchanged")
    already_counted = _accepted(init_store, title="already-counted")
    assert subject["id"] != unchanged["id"]
    assert already_counted["id"] not in {subject["id"], unchanged["id"]}
    _insert_event(init_store, subject["id"], needfix_store.REOPEN_EVENT)
    _set_generation(init_store, subject["id"], 1)
    _insert_event(init_store, subject["id"], needfix_store.REOPEN_EVENT_ORPHAN_RETURN)
    _insert_event(init_store, unchanged["id"], needfix_store.REOPEN_EVENT)
    _set_generation(init_store, unchanged["id"], 1)
    _insert_event(
        init_store, already_counted["id"], needfix_store.REOPEN_EVENT_ORPHAN_RETURN
    )
    _set_generation(init_store, already_counted["id"], 1)

    before = _raw_row(init_store, subject["id"])
    assert before["reopen_generation"] == 1
    assert needfix_store.get_needfix(init_store, subject["id"])["reopen_generation"] == 2

    needfix_store.initialize_repository(init_store)
    after = _raw_row(init_store, subject["id"])
    assert after["reopen_generation"] == 2
    assert {
        key: value for key, value in after.items() if key != "reopen_generation"
    } == {
        key: value for key, value in before.items() if key != "reopen_generation"
    }
    assert _raw_row(init_store, unchanged["id"])["reopen_generation"] == 1
    assert _raw_row(init_store, already_counted["id"])["reopen_generation"] == 1

    needfix_store.initialize_repository(init_store)
    assert _raw_row(init_store, subject["id"])["reopen_generation"] == 2
    assert _raw_row(init_store, unchanged["id"])["reopen_generation"] == 1


def test_existing_reopen_events_still_count_once(init_store: Path) -> None:
    record = _accepted(init_store)
    for event in (
        needfix_store.REOPEN_EVENT,
        needfix_store.REOPEN_EVENT_MISSING_TASK,
        needfix_store.REOPEN_EVENT_RECURRED,
    ):
        _insert_event(init_store, record["id"], event)

    assert needfix_store.get_needfix(init_store, record["id"])["reopen_generation"] == 3
    needfix_store.initialize_repository(init_store)
    assert _raw_row(init_store, record["id"])["reopen_generation"] == 3
    needfix_store.initialize_repository(init_store)
    assert _raw_row(init_store, record["id"])["reopen_generation"] == 3

    alias = _accepted(init_store, title="alias")
    _insert_event(init_store, alias["id"], needfix_store.REOPEN_EVENT_LEGACY_ALIAS)
    assert needfix_store.get_needfix(init_store, alias["id"])["reopen_generation"] == 1

    mixed = _accepted(init_store, title="mixed")
    for event in (
        needfix_store.REOPEN_EVENT,
        needfix_store.REOPEN_EVENT_MISSING_TASK,
        needfix_store.REOPEN_EVENT_RECURRED,
    ):
        _insert_event(init_store, mixed["id"], event)
    _set_generation(init_store, mixed["id"], 3)
    _insert_event(init_store, mixed["id"], needfix_store.REOPEN_EVENT_ORPHAN_RETURN)
    assert needfix_store.get_needfix(init_store, mixed["id"])["reopen_generation"] == 4
    needfix_store.initialize_repository(init_store)
    assert _raw_row(init_store, mixed["id"])["reopen_generation"] == 4
