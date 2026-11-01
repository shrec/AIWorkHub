"""NF-2026-01914: restoring a Manager Chat conversation for display is read-only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiworkhub import manager_loop as ml
from aiworkhub.callback_store import manager_chat_record_holds_seat
from test_manager_loop import REPO_ID, Harness, make  # noqa: F401

_STAMP = "2026-09-21T00:00:00+00:00"


def _record(session_id: str, *, status: str) -> ml.ManagerSession:
    return ml.ManagerSession(
        session_id=session_id,
        repo_id=REPO_ID,
        backend_id="fake",
        model="model-a",
        status=status,
        created_at=_STAMP,
        updated_at=_STAMP,
        closed_at=_STAMP if status == "closed" else None,
    )


def _file(tmp_path: Path, session_id: str) -> Path:
    return tmp_path / "manager_loop" / "sessions" / f"{session_id}.json"


def _close_event(harness: Harness, session_id: str, reason: str) -> None:
    harness.store.append_event(
        session_id, {"at": _STAMP, "turn": 0, "type": "session_close", "payload": {"reason": reason}}
    )


def _holds_seat(tmp_path: Path, session_id: str) -> bool:
    record: dict[str, Any] = json.loads(_file(tmp_path, session_id).read_text(encoding="utf-8"))
    return manager_chat_record_holds_seat(record, state_dir=tmp_path / "manager_loop")


def test_restore_leaves_a_stale_retired_selection_closed_and_off_the_seat(make, tmp_path: Path) -> None:
    harness = make()
    retired = _record("stale-retired-0001", status="closed")
    harness.store.save(retired)
    _close_event(harness, retired.session_id, "stale_active_session")
    harness.store.write_selection(retired.session_id)
    before = _file(tmp_path, retired.session_id).read_bytes()

    restored = harness.orch.restore_latest()

    assert restored is not None and restored.session_id == retired.session_id
    assert (restored.status, restored.closed_at, restored.updated_at) == ("closed", _STAMP, _STAMP)
    assert _file(tmp_path, retired.session_id).read_bytes() == before
    assert not _holds_seat(tmp_path, retired.session_id)
    assert harness.store.read_selection() == retired.session_id
    assert harness.backends == []


def test_restore_of_an_active_selection_renews_no_lease_and_retires_no_other_record(
    make, tmp_path: Path
) -> None:
    harness = make()
    other = _record("other-active-0001", status="active")
    selected = _record("selected-active-0002", status="active")
    harness.store.save(other)
    harness.store.save(selected)
    harness.store.write_selection(selected.session_id)
    before = {item.session_id: _file(tmp_path, item.session_id).read_bytes() for item in (other, selected)}

    restored = harness.orch.restore_latest()

    assert restored is not None and restored.session_id == selected.session_id
    assert restored.updated_at == _STAMP
    for session_id, content in before.items():
        assert _file(tmp_path, session_id).read_bytes() == content
    assert {item.session_id: item.status for item in harness.store.sessions()} == {
        other.session_id: "active",
        selected.session_id: "active",
    }


def test_an_explicit_continue_still_reopens_a_stale_retired_record(make, tmp_path: Path) -> None:
    harness = make()
    retired = _record("stale-retired-0003", status="closed")
    harness.store.save(retired)
    _close_event(harness, retired.session_id, "stale_active_session")
    assert harness.orch.restore_latest().status == "closed"
    again = make(prefix="b")

    reopened = again.orch.attach(retired.session_id)

    assert (reopened.status, reopened.closed_at) == ("active", None)
    assert reopened.updated_at != _STAMP
    assert again.store.read_selection() == retired.session_id


def test_binding_the_same_route_reactivates_a_restored_retired_record(make, tmp_path: Path) -> None:
    harness = make()
    retired = _record("stale-retired-0004", status="closed")
    other = _record("other-active-0004", status="active")
    harness.store.save(retired)
    harness.store.save(other)
    _close_event(harness, retired.session_id, "stale_active_session")
    harness.store.write_selection(retired.session_id)

    restored = harness.orch.restore_latest()
    assert restored is not None and restored.session_id == retired.session_id
    assert restored.status == "closed"
    assert harness.store.read_selection() == retired.session_id

    bound = harness.orch.continue_on_route("fake", "model-a")

    assert bound.session_id == retired.session_id
    assert (bound.status, bound.closed_at) == ("active", None)
    statuses = {item.session_id: item.status for item in harness.store.sessions()}
    assert statuses == {retired.session_id: "active", other.session_id: "closed"}
    closes = [
        event for event in harness.store.events(other.session_id) if event.get("type") == "session_close"
    ]
    assert closes[-1]["payload"]["reason"] == "session_switch"
    assert harness.store.read_selection() == retired.session_id
