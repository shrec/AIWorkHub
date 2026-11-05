"""NF-2026-02071: a verified manager_add NeedFix converts in one call.

The store used to refuse a captured or triaged item until a manager had
called the transition tool twice. A verified manager_add item with evidence
and scope_files now promotes itself to accepted inside the convert
transaction; every other captured or triaged item keeps the refusal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aiworkhub import needfix_store as store

SCOPE = ["src/aiworkhub/needfix_store.py"]
EVIDENCE = {"measured": {"command": "pytest -q", "passed": 3}}
MANAGER = "manager:test-seat"
REASON = "convert_from_verified_manager_add"
# convert_needfix refuses a preview without an explicit workforce launch contract.
PLAN = {
    "runner": "claude_haiku",
    "topic": "needfix_conversion",
    "allowed_writes": SCOPE,
    "required_outputs": SCOPE,
}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store.initialize_repository(tmp_path)
    # The card-input gate reads the worktree contract files. These tests pin
    # the status machine, not that gate, so it is stubbed to pass.
    monkeypatch.setattr(store, "_card_input_refusal_reason", lambda repo_root, card: None)
    return tmp_path


def _create_task(card: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "task_id": f"task-{card.get('task_id') or 'nf02071'}"}


def _manager_item(repo: Path, **overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "title": "Verified manager item",
        "description": "measured regression in the convert path",
        "evidence": EVIDENCE,
        "scope_files": SCOPE,
    }
    kwargs.update(overrides)
    return store.add_needfix(repo, **kwargs)["id"]


def _event_names(repo: Path, needfix_id: str) -> list[str]:
    # list_events is newest-first; reverse it to read the log in order.
    return [event["event"] for event in reversed(store.list_events(repo, needfix_id))]


def test_verified_manager_add_converts_in_one_call(repo: Path) -> None:
    needfix_id = _manager_item(repo)

    preview = store.preview_convert(repo, needfix_id, task_plan=PLAN)
    assert preview["convertible"] is True, preview
    assert "next_action" not in preview

    result = store.convert_needfix(repo, needfix_id, _create_task)

    assert result["already_converted"] is False
    assert store.get_needfix(repo, needfix_id)["status"] == "task_created"
    assert _event_names(repo, needfix_id) == [
        "created",
        "triaged",
        "accepted",
        "conversion_claimed",
        "conversion_committed",
    ]
    events = store.list_events(repo, needfix_id)
    implied = [e for e in reversed(events) if e["event"] in ("triaged", "accepted")]
    for event in implied:
        assert event["detail"]["reason"] == REASON
        assert event["detail"]["origin"] == "manager_add"
        assert "manager_identity" not in event["detail"]


def test_manager_identity_is_recorded_on_the_implied_events(repo: Path) -> None:
    needfix_id = _manager_item(repo, provenance={"manager_identity": MANAGER})

    store.convert_needfix(repo, needfix_id, _create_task)

    events = store.list_events(repo, needfix_id)
    implied = [e for e in events if e["event"] in ("triaged", "accepted")]
    assert len(implied) == 2
    for event in implied:
        assert event["detail"]["reason"] == REASON
        assert event["detail"]["manager_identity"] == MANAGER


def test_already_triaged_verified_item_only_records_the_accept(repo: Path) -> None:
    needfix_id = _manager_item(repo)
    store.triage_needfix(repo, needfix_id)

    store.convert_needfix(repo, needfix_id, _create_task)

    assert _event_names(repo, needfix_id) == [
        "created",
        "triaged",
        "accepted",
        "conversion_claimed",
        "conversion_committed",
    ]
    accepted = [e for e in store.list_events(repo, needfix_id) if e["event"] == "accepted"]
    assert accepted[0]["detail"]["reason"] == REASON


def test_preview_is_read_only_for_a_verified_item(repo: Path) -> None:
    needfix_id = _manager_item(repo)

    store.preview_convert(repo, needfix_id, task_plan=PLAN)

    assert store.get_needfix(repo, needfix_id)["status"] == "captured"
    assert _event_names(repo, needfix_id) == ["created"]


def test_unverified_worker_proposal_is_still_refused(repo: Path) -> None:
    needfix_id = store.capture_proposal(
        repo,
        title="Worker proposal",
        description="unverified worker finding",
        evidence=EVIDENCE,
        scope_files=SCOPE,
    )["id"]

    preview = store.preview_convert(repo, needfix_id, task_plan=PLAN)
    assert preview["convertible"] is False
    assert "refusal_reason" in preview

    with pytest.raises(store.NeedFixConflictError):
        store.convert_needfix(repo, needfix_id, _create_task)

    assert store.get_needfix(repo, needfix_id)["status"] == "captured"
    assert _event_names(repo, needfix_id) == ["created"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"evidence": None},
        {"evidence": {}},
        {"scope_files": None},
        {"scope_files": []},
        {"provenance": {"origin": "ingest"}},
    ],
    ids=["no_evidence", "empty_evidence", "no_scope_files", "empty_scope_files", "not_manager_origin"],
)
def test_verified_manager_add_missing_its_proof_is_refused(
    repo: Path, overrides: dict[str, Any]
) -> None:
    needfix_id = _manager_item(repo, **overrides)

    with pytest.raises(store.NeedFixConflictError):
        store.convert_needfix(repo, needfix_id, _create_task)

    assert store.get_needfix(repo, needfix_id)["status"] == "captured"
    assert _event_names(repo, needfix_id) == ["created"]


def test_failed_task_creation_keeps_the_verified_promotion(repo: Path) -> None:
    needfix_id = _manager_item(repo)

    def failing_create(card: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "error": "task store unavailable"}

    with pytest.raises(store.NeedFixError):
        store.convert_needfix(repo, needfix_id, failing_create)

    # The claim is compensated back to accepted, the status the promotion
    # reached. The item stays convertible and needs no second manual call.
    assert store.get_needfix(repo, needfix_id)["status"] == "accepted"
    store.convert_needfix(repo, needfix_id, _create_task)
    assert store.get_needfix(repo, needfix_id)["status"] == "task_created"
