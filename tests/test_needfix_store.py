"""Unit tests for read-time NeedFix active-state derivation and link relaxation."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Mapping

import pytest

from aiworkhub import needfix_store
from aiworkhub.needfix_store import (
    ACTIVE_STATE_DEFINITION,
    CLOSED_CARD_STATUSES,
    LINKABLE_CARD_STATUSES,
    NEEDFIX_CARD_DERIVED_STATUSES,
    NEEDFIX_TERMINAL_STATUSES,
    OWNED_CARD_STATUSES,
    REOPEN_CARD_STATUSES,
    STATUSES,
    derive_active_state,
)


@pytest.fixture
def repo_root() -> Path:
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def init(repo_root: Path) -> Path:
    needfix_store.initialize_repository(repo_root)
    return repo_root


def _row(**kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"id": "NF-1", "converted_task_id": None}
    row.update(kw)
    return row


def _card(status: str, *, superseded_by: str | None = None) -> dict[str, Any]:
    return {"id": "T-1", "status": status, "superseded_by": superseded_by}


def _get(cards: Mapping[str, Mapping[str, Any]]):
    def get_task(task_id: str):
        return cards.get(task_id)

    return get_task


def _status():
    def canonical_status(card: Mapping[str, Any]) -> str:
        return card["status"]

    return canonical_status


# --- derive_active_state ----------------------------------------------------

def test_empty_converted_id_is_active():
    st = derive_active_state(_row(), _get({}), _status())
    assert st == {"state": "active", "active": True, "reason": None}


def test_dangling_card_is_active_with_reason():
    st = derive_active_state(_row(converted_task_id="T-ghost"), _get({}), _status())
    assert st["state"] == "active" and st["active"] is True
    assert "no longer exists" in st["reason"]


@pytest.mark.parametrize("status", sorted(OWNED_CARD_STATUSES))
def test_owned_status_is_hidden(status: str):
    st = derive_active_state(
        _row(converted_task_id="T-1"), _get({"T-1": _card(status)}), _status()
    )
    assert st == {
        "state": "owned",
        "active": False,
        "reason": f"owned by {status} task 'T-1'",
    }


@pytest.mark.parametrize("status", sorted(CLOSED_CARD_STATUSES))
def test_closed_status_is_hidden_as_fixed(status: str):
    st = derive_active_state(
        _row(converted_task_id="T-1"), _get({"T-1": _card(status)}), _status()
    )
    assert st["state"] == "closed" and st["active"] is False
    assert "fixed" in st["reason"]


@pytest.mark.parametrize("status", sorted(REOPEN_CARD_STATUSES))
def test_reopen_status_without_successor_is_active_again(status: str):
    st = derive_active_state(
        _row(converted_task_id="T-1"), _get({"T-1": _card(status)}), _status()
    )
    assert st["state"] == "reopened" and st["active"] is True
    assert "live again" in st["reason"]
    assert status in st["reason"]


def test_reopen_status_with_landed_successor_is_closed():
    cards = {"T-1": _card("superseded", superseded_by="T-2"), "T-2": _card("finished")}
    st = derive_active_state(_row(converted_task_id="T-1"), _get(cards), _status())
    assert st["state"] == "closed" and st["active"] is False
    assert "successor" in st["reason"]


def test_unknown_status_fails_safe_to_active():
    st = derive_active_state(
        _row(converted_task_id="T-1"), _get({"T-1": _card("weird")}), _status()
    )
    assert st["state"] == "active" and st["active"] is True
    assert "unrecognised" in st["reason"]


# --- link_existing_task relaxation ------------------------------------------

def test_link_existing_task_accepts_owned_status(init: Path):
    rec = needfix_store.add_needfix(init, title="t", description="d", status="accepted")
    result = needfix_store.link_existing_task(
        init, rec["id"], "T-pending", _get({"T-pending": _card("pending")}), _status()
    )
    assert result["converted_task_id"] == "T-pending"
    assert result["already_converted"] is False
    updated = needfix_store.get_needfix(init, rec["id"])
    assert updated["converted_task_id"] == "T-pending"
    assert updated["status"] == "task_created"


def test_link_existing_task_accepts_closed_status(init: Path):
    rec = needfix_store.add_needfix(init, title="t", description="d", status="accepted")
    result = needfix_store.link_existing_task(
        init, rec["id"], "T-done", _get({"T-done": _card("finished")}), _status()
    )
    assert result["converted_task_id"] == "T-done"


def test_link_existing_task_rejects_non_linkable_status(init: Path):
    rec = needfix_store.add_needfix(init, title="t", description="d", status="accepted")
    with pytest.raises(needfix_store.NeedFixConflictError):
        needfix_store.link_existing_task(
            init, rec["id"], "T-old", _get({"T-old": _card("superseded")}), _status()
        )
    # claim was compensated back to accepted
    assert needfix_store.get_needfix(init, rec["id"])["status"] == "accepted"


def test_link_existing_task_rejects_captured(init: Path):
    rec = needfix_store.capture_proposal(init, title="t", description="d")
    with pytest.raises(needfix_store.NeedFixConflictError):
        needfix_store.link_existing_task(
            init, rec["id"], "T-x", _get({"T-x": _card("pending")}), _status()
        )


# --- constants sanity -------------------------------------------------------

def test_active_state_definition_names_all_rule_branches():
    assert ACTIVE_STATE_DEFINITION
    for word in ("ACTIVE", "OWNED", "CLOSED"):
        assert word in ACTIVE_STATE_DEFINITION
    # Every card status in the rule is accounted for in a status bucket.
    buckets = OWNED_CARD_STATUSES | CLOSED_CARD_STATUSES | REOPEN_CARD_STATUSES
    assert "pending" in buckets and "finished" in buckets and "superseded" in buckets
    assert "cancelled" in buckets or "canceled" in buckets
    assert LINKABLE_CARD_STATUSES == (
        OWNED_CARD_STATUSES | CLOSED_CARD_STATUSES
    ) - {"archived"}
    # The three buckets are pairwise disjoint: a status landing in two buckets
    # would make derived state depend on evaluation order.
    assert OWNED_CARD_STATUSES.isdisjoint(CLOSED_CARD_STATUSES)
    assert OWNED_CARD_STATUSES.isdisjoint(REOPEN_CARD_STATUSES)
    assert CLOSED_CARD_STATUSES.isdisjoint(REOPEN_CARD_STATUSES)
    # Every card status named in the ACTIVE rule lands in exactly one bucket:
    # pairwise disjointness only proves "at most one", so also prove "at least
    # one" for the exact statuses the rule talks about.
    rule_statuses = (
        "pending", "processing", "review", "blocked",
        "finished", "archived", "accepted",
        "superseded", "cancelled",
    )
    for rule_status in rule_statuses:
        membership = sum(
            rule_status in bucket
            for bucket in (OWNED_CARD_STATUSES, CLOSED_CARD_STATUSES, REOPEN_CARD_STATUSES)
        )
        assert membership == 1, f"{rule_status!r} lands in {membership} buckets"


# --- outer layer: the NeedFix's OWN status is decisive ----------------------

def _boom_get_task(task_id: str):
    raise AssertionError(f"card lookup must not run for terminal record: {task_id!r}")


@pytest.mark.parametrize("status", sorted(NEEDFIX_TERMINAL_STATUSES))
def test_terminal_needfix_status_never_active_and_skips_card_lookup(status: str):
    # The linked card, if consulted, points at a cancelled task with no
    # successor -- which would derive ACTIVE. The outer terminal layer must win
    # AND must not look the card up at all (``_boom_get_task`` raises if it is).
    row = _row(status=status, converted_task_id="T-1")
    st = derive_active_state(row, _boom_get_task, _status())
    assert st["active"] is False
    assert st["state"] == "closed"
    assert status in st["reason"]


def test_unrecognised_needfix_status_is_not_active():
    st = derive_active_state(
        _row(status="a_status_nobody_thought_about", converted_task_id=None),
        _get({}),
        _status(),
    )
    assert st["active"] is False
    assert st["state"] == "unknown"
    assert "a_status_nobody_thought_about" in st["reason"]


@pytest.mark.parametrize("status", sorted(NEEDFIX_CARD_DERIVED_STATUSES))
def test_non_terminal_unlinked_status_is_active(status: str):
    # Every non-terminal status with no linked card is a live, unowned problem.
    st = derive_active_state(_row(status=status, converted_task_id=None), _get({}), _status())
    assert st["active"] is True
    assert st["state"] == "active"


def test_needfix_status_buckets_are_frozensets_disjoint_and_exhaustive():
    assert isinstance(NEEDFIX_TERMINAL_STATUSES, frozenset)
    assert isinstance(NEEDFIX_CARD_DERIVED_STATUSES, frozenset)
    # A status landing in both buckets would make its active state depend on
    # evaluation order.
    assert NEEDFIX_TERMINAL_STATUSES.isdisjoint(NEEDFIX_CARD_DERIVED_STATUSES)
    # Every status the table can hold -- the canonical set plus the transient
    # ``converting`` -- is placed explicitly in exactly one bucket.
    every_status = set(STATUSES) | {"converting"}
    assert NEEDFIX_TERMINAL_STATUSES | NEEDFIX_CARD_DERIVED_STATUSES == every_status


def test_card_status_buckets_are_frozensets_and_pairwise_disjoint():
    assert isinstance(OWNED_CARD_STATUSES, frozenset)
    assert isinstance(CLOSED_CARD_STATUSES, frozenset)
    assert isinstance(REOPEN_CARD_STATUSES, frozenset)
    assert OWNED_CARD_STATUSES.isdisjoint(CLOSED_CARD_STATUSES)
    assert OWNED_CARD_STATUSES.isdisjoint(REOPEN_CARD_STATUSES)
    assert CLOSED_CARD_STATUSES.isdisjoint(REOPEN_CARD_STATUSES)


def test_active_state_definition_is_derived_from_the_buckets():
    # The text is built from the frozensets, so every bucket member must appear
    # in it verbatim -- proving it cannot drift from the behaviour.
    for bucket in (
        OWNED_CARD_STATUSES,
        CLOSED_CARD_STATUSES,
        REOPEN_CARD_STATUSES,
        NEEDFIX_TERMINAL_STATUSES,
    ):
        for member in bucket:
            assert member in ACTIVE_STATE_DEFINITION, member
