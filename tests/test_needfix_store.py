"""Unit tests for read-time NeedFix active-state derivation and link relaxation."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

import pytest

from aiworkhub import core, needfix_store
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


# --- foreign_origin_card: positive manager evidence on an absent card -------

def test_absent_card_without_tag_stays_active():
    row = _row(converted_task_id="T-ghost", tags=[])
    st = derive_active_state(row, _get({}), _status())
    assert st["state"] == "active" and st["active"] is True
    assert "no longer exists" in st["reason"]


def test_absent_card_with_unrelated_tag_stays_active():
    row = _row(converted_task_id="T-ghost", tags=["markdown_intake"])
    st = derive_active_state(row, _get({}), _status())
    assert st["state"] == "active" and st["active"] is True


def test_absent_card_with_foreign_origin_tag_is_hidden():
    row = _row(converted_task_id="T-ghost", tags=[needfix_store.FOREIGN_ORIGIN_CARD_TAG])
    st = derive_active_state(row, _get({}), _status())
    assert st["state"] == "foreign_card_unverified"
    assert st["active"] is False
    assert "T-ghost" in st["reason"]
    assert needfix_store.FOREIGN_ORIGIN_CARD_TAG in st["reason"]


def test_foreign_origin_tag_with_existing_card_derives_from_card():
    row = _row(converted_task_id="T-1", tags=[needfix_store.FOREIGN_ORIGIN_CARD_TAG])
    st = derive_active_state(row, _get({"T-1": _card("pending")}), _status())
    assert st == {
        "state": "owned",
        "active": False,
        "reason": "owned by pending task 'T-1'",
    }


def test_foreign_origin_tag_with_raising_lookup_stays_active():
    row = _row(converted_task_id="T-ghost", tags=[needfix_store.FOREIGN_ORIGIN_CARD_TAG])

    def raising_get_task(task_id: str):
        raise RuntimeError("transient lookup failure")

    st = derive_active_state(row, raising_get_task, _status())
    assert st["state"] == "active" and st["active"] is True
    assert "no longer exists" in st["reason"]


@pytest.mark.parametrize(
    "malformed_tags", ["not-json{{{", '"a-json-string"', 42, {"a": 1}]
)
def test_malformed_tags_value_is_treated_as_untagged(malformed_tags):
    row = _row(converted_task_id="T-ghost", tags=malformed_tags)
    st = derive_active_state(row, _get({}), _status())
    assert st["state"] == "active" and st["active"] is True


def test_active_state_definition_names_foreign_card_state_and_tag():
    assert "foreign_card_unverified" in ACTIVE_STATE_DEFINITION
    assert needfix_store.FOREIGN_ORIGIN_CARD_TAG in ACTIVE_STATE_DEFINITION


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


# --- authoritative reopen generation: durable event vocabulary --------------


def _reopen_create_task_fn(card: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "task_id": card["task_id"]}


def _archived_get_task(task_id: str) -> dict[str, Any]:
    return {"id": task_id, "status": "archived", "archive_operation": "archived"}


def _superseded_get_task(task_id: str) -> dict[str, Any]:
    return {"id": task_id, "status": "archived", "archive_operation": "superseded"}


def _linkable_get_task(task_id: str) -> dict[str, Any]:
    return {"id": task_id, "status": "accepted"}


def _reopen_canonical_status(task: Mapping[str, Any]) -> str:
    return task["status"]


def test_legacy_archived_alias_event_counts_toward_authoritative_generation(init: Path):
    # ``archived_task_link_reopened`` is the legacy alias durable rows may
    # carry; the authoritative reader must count it exactly like the
    # canonical ``superseded_task_link_reopened`` event.
    rec = needfix_store.add_needfix(init, title="t", description="d", status="accepted")
    needfix_store.convert_needfix(init, rec["id"], _reopen_create_task_fn)

    reopened = needfix_store.reopen_superseded_task_link(
        init,
        rec["id"],
        get_task_fn=_archived_get_task,
        canonical_status_fn=_reopen_canonical_status,
        reason="legacy archived link reconciliation",
    )
    assert reopened["reopen_generation"] == 1

    events = needfix_store.list_events(init, rec["id"], limit=10)
    assert events[0]["event"] == "archived_task_link_reopened"
    assert needfix_store.get_needfix(init, rec["id"])["reopen_generation"] == 1


def test_reopen_show_link_existing_round_trip_needs_no_second_reopen(init: Path):
    rec = needfix_store.add_needfix(init, title="t", description="d", status="accepted")
    first = needfix_store.convert_needfix(init, rec["id"], _reopen_create_task_fn)

    needfix_store.reopen_superseded_task_link(
        init,
        rec["id"],
        get_task_fn=_superseded_get_task,
        canonical_status_fn=_reopen_canonical_status,
        reason="stale superseded link reconciliation",
    )

    shown = needfix_store.get_needfix(init, rec["id"])
    assert shown["status"] == "accepted"
    assert shown["converted_task_id"] is None
    assert shown["reopen_generation"] == 1

    linked = needfix_store.link_existing_task(
        init, rec["id"], "T-existing-accepted", _linkable_get_task, _reopen_canonical_status
    )
    assert linked["converted_task_id"] == "T-existing-accepted"
    assert linked["already_converted"] is False

    final = needfix_store.get_needfix(init, rec["id"])
    assert final["converted_task_id"] == "T-existing-accepted"
    assert final["converted_task_id"] != first["converted_task_id"]
    assert final["reopen_generation"] == 1


def test_unrelated_and_forged_events_do_not_count_toward_generation(init: Path):
    rec = needfix_store.add_needfix(init, title="t", description="d", status="accepted")
    conn = needfix_store._connect(init)
    try:
        needfix_store._record_event(
            conn, rec["id"], "existing_task_link_claimed", {"note": "unrelated authenticated event"}
        )
        needfix_store._record_event(
            conn, rec["id"], "forged_reopen_event", {"note": "not a recognised reopen alias"}
        )
        conn.commit()
    finally:
        conn.close()
    assert needfix_store.get_needfix(init, rec["id"])["reopen_generation"] == 0

    needfix_store.convert_needfix(init, rec["id"], _reopen_create_task_fn)
    reopened = needfix_store.reopen_superseded_task_link(
        init,
        rec["id"],
        get_task_fn=_superseded_get_task,
        canonical_status_fn=_reopen_canonical_status,
        reason="genuine reopen after unrelated and forged events",
    )
    assert reopened["reopen_generation"] == 1
    assert needfix_store.get_needfix(init, rec["id"])["reopen_generation"] == 1


# --- records janitor bookkeeping --------------------------------------------


def _converted(init: Path, title: str) -> tuple[str, str]:
    rec = needfix_store.add_needfix(init, title=title, description="d", status="accepted")
    converted = needfix_store.convert_needfix(init, rec["id"], _reopen_create_task_fn)
    return rec["id"], converted["converted_task_id"]


def test_reopen_missing_task_without_history_fn_still_raises(init: Path):
    rec_id, _task_id = _converted(init, "missing")
    with pytest.raises(needfix_store.NeedFixValidationError, match="not found"):
        needfix_store.reopen_superseded_task_link(
            init,
            rec_id,
            get_task_fn=lambda _task_id: None,
            canonical_status_fn=_reopen_canonical_status,
            reason="missing task, no local history authority",
        )
    assert needfix_store.get_needfix(init, rec_id)["status"] == "task_created"


def test_reopen_accepts_canonical_superseded_without_accepted_at(init: Path):
    rec_id, _task_id = _converted(init, "superseded")
    reopened = needfix_store.reopen_superseded_task_link(
        init,
        rec_id,
        get_task_fn=lambda task_id: {"id": task_id, "status": "superseded"},
        canonical_status_fn=_reopen_canonical_status,
        reason="canonical superseded task",
    )
    assert reopened["status"] == "accepted"
    events = needfix_store.list_events(init, rec_id, limit=10)
    assert events[0]["event"] == "superseded_task_link_reopened"


def test_janitor_bookkeeping_archives_accepted_reopens_dead_and_skips_foreign(init: Path):
    finished = _converted(init, "finished")
    archived_accepted = _converted(init, "archived accepted")
    archived_superseded = _converted(init, "archived superseded")
    superseded = _converted(init, "superseded")
    missing_local = _converted(init, "missing local")
    missing_foreign = _converted(init, "missing foreign")
    live = _converted(init, "live")
    cards = {
        finished[1]: {"id": finished[1], "status": "finished"},
        archived_accepted[1]: {
            "id": archived_accepted[1],
            "status": "archived",
            "archive_operation": "archived",
            "accepted_at": "2026-09-01T00:00:00+00:00",
        },
        archived_superseded[1]: {
            "id": archived_superseded[1],
            "status": "archived",
            "archive_operation": "superseded",
        },
        superseded[1]: {"id": superseded[1], "status": "superseded"},
        live[1]: {"id": live[1], "status": "review"},
    }

    summary = needfix_store.janitor_bookkeeping(
        init,
        get_task_fn=cards.get,
        canonical_status_fn=_reopen_canonical_status,
        task_has_local_history_fn=lambda task_id: task_id == missing_local[1],
    )

    assert summary == {"scanned": 7, "archived": 2, "reopened": 3, "skipped": 1}
    for rec_id, _task_id in (finished, archived_accepted):
        row = needfix_store.get_needfix(init, rec_id)
        assert row["status"] == "archived"
        event = needfix_store.list_events(init, rec_id, limit=10)[0]
        assert event["event"] == "archived"
    for rec_id, _task_id in (archived_superseded, superseded, missing_local):
        row = needfix_store.get_needfix(init, rec_id)
        assert row["status"] == "accepted"
        assert row["converted_task_id"] is None
        assert row["reopen_generation"] == 1
    missing_event = needfix_store.list_events(init, missing_local[0], limit=10)[0]
    assert missing_event["event"] == "missing_task_link_reopened"
    for rec_id, task_id in (missing_foreign, live):
        row = needfix_store.get_needfix(init, rec_id)
        assert row["status"] == "task_created"
        assert row["converted_task_id"] == task_id


def _receipt(task_id: str = "TASK-1", request_id: str = "request-1") -> dict[str, Any]:
    hashes = {"src/fix.py": "b" * 64}
    unsigned = {
        "schema_id": needfix_store.ACCEPTED_OUTCOME_RECEIPT_SCHEMA_ID,
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": 1,
        "base_oid": "base",
        "promoted_paths": ["src/fix.py"],
        "changed_path_hashes": hashes,
        "attempt_artifact_manifest_id": "c" * 64,
        "repository_revision": "sha256:" + hashlib.sha256(
            json.dumps(
                {"base_oid": "base", "changed_path_hashes": hashes},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }
    return {
        **unsigned,
        "receipt_id": "sha256:" + hashlib.sha256(
            json.dumps(
                unsigned, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
    }


def _cause(**overrides: Any) -> dict[str, Any]:
    result = {
        "schema_id": needfix_store.CAUSED_BY_SCHEMA_ID,
        "repository_id": "repo-one",
        "task_id": "TASK-1",
        "request_id": "request-1",
        "accepted_outcome_receipt": _receipt(),
    }
    result.update(overrides)
    return result


def _accepted(identity: Mapping[str, Any]) -> dict[str, Any]:
    return {**identity, "outcome": "accepted"}


def test_capture_persists_verified_caused_by_and_is_idempotent(init: Path):
    kwargs = {
        "title": "escaped defect",
        "description": "regression after accepted outcome",
        "caused_by": _cause(),
        "repository_id": "repo-one",
        "verify_accepted_outcome": _accepted,
    }
    first = needfix_store.capture_proposal(init, **kwargs)
    second = needfix_store.capture_proposal(init, **kwargs)
    assert second["id"] == first["id"]
    assert needfix_store.get_needfix(init, first["id"])["caused_by"] == _cause()


def test_dedupe_without_incoming_cause_preserves_stored_attribution(init: Path):
    first = needfix_store.capture_proposal(
        init,
        title="escaped defect",
        description="regression after accepted outcome",
        caused_by=_cause(),
        repository_id="repo-one",
        verify_accepted_outcome=_accepted,
    )
    second = needfix_store.capture_proposal(
        init,
        title="escaped defect",
        description="regression after accepted outcome",
    )
    assert second["id"] == first["id"]
    assert second["caused_by"] == _cause()


@pytest.mark.parametrize(
    "cause,repo,verifier",
    [
        (_cause(accepted_outcome_receipt={}), "repo-one", _accepted),
        (_cause(repository_id="repo-two"), "repo-one", _accepted),
        (_cause(), "repo-one", lambda identity: {**identity, "outcome": "rejected"}),
        (_cause(), "repo-one", lambda identity: None),
        (
            _cause(accepted_outcome_receipt=_receipt(request_id="stale")),
            "repo-one",
            _accepted,
        ),
        (
            _cause(
                accepted_outcome_receipt={
                    **_receipt(),
                    "receipt_id": "sha256:" + "e" * 64,
                }
            ),
            "repo-one",
            lambda identity: _accepted(_cause()),
        ),
    ],
)
def test_caused_by_forged_stale_cross_repo_or_unverifiable_fails_closed(
    init: Path, cause: dict[str, Any], repo: str, verifier
):
    with pytest.raises(needfix_store.NeedFixValidationError):
        needfix_store.capture_proposal(
            init,
            title="bad cause",
            description="must not persist",
            caused_by=cause,
            repository_id=repo,
            verify_accepted_outcome=verifier,
        )


def test_legacy_row_has_explicitly_absent_caused_by(init: Path):
    row = needfix_store.capture_proposal(init, title="legacy", description="no cause")
    assert row["caused_by"] is None
    assert needfix_store.get_needfix(init, row["id"])["caused_by"] is None


# --- update_needfix caused_by: write-once causality -------------------------


def _uncaused(init: Path, title: str = "escaped defect") -> str:
    row = needfix_store.capture_proposal(
        init, title=title, description="regression after accepted outcome",
    )
    assert row["caused_by"] is None
    return row["id"]


def test_update_sets_caused_by_on_a_row_that_has_none(init: Path):
    needfix_id = _uncaused(init)

    updated = needfix_store.update_needfix(
        init,
        needfix_id,
        caused_by=_cause(),
        repository_id="repo-one",
        verify_accepted_outcome=_accepted,
    )

    assert "caused_by" in updated["update_receipt"]["fields_changed"]
    assert updated["caused_by"] == _cause()
    assert needfix_store.get_needfix(init, needfix_id)["caused_by"] == _cause()


def test_update_with_the_same_caused_by_is_a_no_op(init: Path):
    """Re-sending an identical identity is not a change and must not report one."""
    needfix_id = _uncaused(init)
    kwargs = {
        "caused_by": _cause(),
        "repository_id": "repo-one",
        "verify_accepted_outcome": _accepted,
    }
    needfix_store.update_needfix(init, needfix_id, **kwargs)

    again = needfix_store.update_needfix(init, needfix_id, **kwargs)

    assert again["update_receipt"]["fields_changed"] == []
    assert again["caused_by"] == _cause()


def test_update_with_a_different_caused_by_is_refused(init: Path):
    """Who caused a defect is evidence; a later caller may not overwrite it."""
    needfix_id = _uncaused(init)
    needfix_store.update_needfix(
        init,
        needfix_id,
        caused_by=_cause(),
        repository_id="repo-one",
        verify_accepted_outcome=_accepted,
    )
    other = _cause(
        task_id="TASK-2",
        request_id="request-2",
        accepted_outcome_receipt=_receipt(task_id="TASK-2", request_id="request-2"),
    )

    with pytest.raises(needfix_store.NeedFixConflictError):
        needfix_store.update_needfix(
            init,
            needfix_id,
            caused_by=other,
            repository_id="repo-one",
            verify_accepted_outcome=_accepted,
        )

    assert needfix_store.get_needfix(init, needfix_id)["caused_by"] == _cause()


def test_update_refuses_a_cause_with_no_canonical_verifier(init: Path):
    needfix_id = _uncaused(init)

    with pytest.raises(needfix_store.NeedFixValidationError):
        needfix_store.update_needfix(
            init, needfix_id, caused_by=_cause(), repository_id="repo-one",
        )

    assert needfix_store.get_needfix(init, needfix_id)["caused_by"] is None


def test_an_ordinary_update_never_touches_a_recorded_cause(init: Path):
    needfix_id = _uncaused(init)
    needfix_store.update_needfix(
        init,
        needfix_id,
        caused_by=_cause(),
        repository_id="repo-one",
        verify_accepted_outcome=_accepted,
    )

    updated = needfix_store.update_needfix(init, needfix_id, title="retitled")

    assert updated["update_receipt"]["fields_changed"] == ["title"]
    assert updated["caused_by"] == _cause()


# --- core._verify_integrated_commit against a real git repo -----------------


def _git(cwd: Path, *args: str) -> str:
    # cwd= as well as -C: git resolves getcwd() on the inherited directory
    # before applying -C, and a sandboxed pytest cwd may not be resolvable.
    result = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd), *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def git_repo(repo_root: Path) -> Path:
    try:
        _git(repo_root, "init", "-q")
    except subprocess.CalledProcessError as exc:
        pytest.skip(
            f"git init unavailable (rc={exc.returncode}): "
            f"{(exc.stderr or '')[:300]}"
        )
    (repo_root / "a.txt").write_text("a", encoding="utf-8")
    _git(repo_root, "add", "a.txt")
    _git(repo_root, "commit", "-q", "-m", "c0")
    return repo_root


def test_verify_integrated_commit_head_ancestor_returns_full_oid(git_repo: Path):
    head = _git(git_repo, "rev-parse", "HEAD")
    assert core._verify_integrated_commit(git_repo, head[:12]) == head


def test_verify_integrated_commit_unknown_sha_returns_none(git_repo: Path):
    assert core._verify_integrated_commit(git_repo, "abc1234") is None


def test_verify_integrated_commit_unreachable_from_head_returns_none(git_repo: Path):
    head = _git(git_repo, "rev-parse", "HEAD")
    _git(git_repo, "checkout", "-q", "-b", "side")
    (git_repo / "b.txt").write_text("b", encoding="utf-8")
    _git(git_repo, "add", "b.txt")
    _git(git_repo, "commit", "-q", "-m", "c1")
    side_oid = _git(git_repo, "rev-parse", "HEAD")
    _git(git_repo, "checkout", "-q", head)

    assert core._verify_integrated_commit(git_repo, side_oid) is None


def test_verify_integrated_commit_rejects_non_hex_without_running_git(
    monkeypatch, repo_root: Path
):
    def _boom(*_a, **_kw):
        raise AssertionError("git must not be invoked for a non-hex candidate")

    monkeypatch.setattr(core.subprocess, "run", _boom)

    assert core._verify_integrated_commit(repo_root, "--output=x") is None
    assert core._verify_integrated_commit(repo_root, "HEAD~1") is None
