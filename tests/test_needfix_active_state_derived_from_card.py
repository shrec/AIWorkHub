"""Acceptance tests: NeedFix active state derived at read time from the card.

Covers the eight task-contract acceptance criteria:
1. The active listing excludes owned (pending/processing/review/blocked) and
   closed (finished/archived/accepted) records.
2. cancelled/superseded WITHOUT a landed successor -> ACTIVE again, with reason.
3. converted_task_id naming a missing card -> ACTIVE (not silently owned).
4. Active state is derived at read time (self-heals) and the report states no
   background synchronisation/daemon/repair was added.
5. count_needfix and list_needfix agree on "active" and both state the
   definition; before/after numbers demonstrated.
6. reconcile_unlinked_needfix PERFORMS links from the system, but only on
   verifiable evidence (the deterministic needfix-{NF-ID} id or an explicit
   card reference) -- never on a title/objective/id coincidence.
7. Archived and closed records remain queryable with their reasons intact.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Mapping

import pytest

from aiworkhub import needfix_store


@pytest.fixture
def repo_root() -> Path:
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def init_store(repo_root: Path) -> Path:
    result = needfix_store.initialize_repository(repo_root)
    assert result["initialized"] is True
    return repo_root


def _card(task_id: str, status: str, *, superseded_by: str | None = None) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "objective": f"Land the fix behind {task_id}",
        "status": status,
        "superseded_by": superseded_by,
    }


def _get_task_fn(cards: Mapping[str, Mapping[str, Any]]):
    def get_task(task_id: str):
        return cards.get(task_id)

    return get_task


def _canonical_status_fn():
    def canonical_status(card: Mapping[str, Any]) -> str:
        return card["status"]

    return canonical_status


def _set_converted(repo_root: Path, needfix_id: str, task_id: str | None) -> None:
    path = needfix_store._db_path(repo_root)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "UPDATE needfix SET converted_task_id = ?, status = ? WHERE id = ?",
            (task_id, "task_created", needfix_id),
        )
        conn.commit()
    finally:
        conn.close()


def _linked(repo_root: Path, title: str, task_id: str | None) -> dict[str, Any]:
    rec = needfix_store.capture_proposal(repo_root, title=title, description=title)
    _set_converted(repo_root, rec["id"], task_id)
    return needfix_store.get_needfix(repo_root, rec["id"])


def _active_ids(init_store: Path, cards: Mapping[str, Mapping[str, Any]]) -> set[str]:
    report = needfix_store.list_active_needfix(
        init_store, get_task_fn=_get_task_fn(cards), canonical_status_fn=_canonical_status_fn()
    )
    return {item["id"] for item in report["items"]}


# --- 1. Active listing excludes owned and closed records ---------------------

def test_active_listing_excludes_owned_and_closed(init_store: Path):
    owned = ("pending", "processing", "review", "blocked")
    closed = ("finished", "archived", "accepted")
    cards: dict[str, Mapping[str, Any]] = {}
    excluded_ids: set[str] = set()
    for status in (*owned, *closed):
        task_id = f"T-{status}"
        cards[task_id] = _card(task_id, status)
        excluded_ids.add(_linked(init_store, f"linked-{status}", task_id)["id"])

    empty = needfix_store.capture_proposal(init_store, title="unlinked", description="no card")
    dangling = _linked(init_store, "dangling", "T-missing")

    active_ids = _active_ids(init_store, cards)

    assert empty["id"] in active_ids
    assert dangling["id"] in active_ids
    assert active_ids & excluded_ids == set()
    assert active_ids == {empty["id"], dangling["id"]}


# --- 2. cancelled/superseded without a landed successor -> ACTIVE AGAIN ------

def test_superseded_without_successor_is_active_again_with_reason(init_store: Path):
    cards = {"T-old": _card("T-old", "superseded")}
    rec = _linked(init_store, "superseded-no-successor", "T-old")

    items = needfix_store.list_active_needfix(
        init_store, get_task_fn=_get_task_fn(cards), canonical_status_fn=_canonical_status_fn()
    )["items"]

    assert [i["id"] for i in items] == [rec["id"]]
    assert items[0]["derived_state"] == "reopened"
    assert items[0]["active"] is True
    assert "superseded" in items[0]["active_reason"]
    assert "live again" in items[0]["active_reason"]


def test_cancelled_without_successor_is_active_again_with_reason(init_store: Path):
    cards = {"T-canc": _card("T-canc", "cancelled")}
    rec = _linked(init_store, "cancelled-no-successor", "T-canc")

    items = needfix_store.list_active_needfix(
        init_store, get_task_fn=_get_task_fn(cards), canonical_status_fn=_canonical_status_fn()
    )["items"]

    assert [i["id"] for i in items] == [rec["id"]]
    assert items[0]["derived_state"] == "reopened"
    assert "cancelled" in items[0]["active_reason"]


def test_superseded_with_landed_successor_is_closed(init_store: Path):
    cards = {
        "T-old": _card("T-old", "superseded", superseded_by="T-new"),
        "T-new": _card("T-new", "finished"),
    }
    rec = _linked(init_store, "superseded-with-successor", "T-old")

    active = _active_ids(init_store, cards)
    assert rec["id"] not in active

    listing = needfix_store.list_needfix(
        init_store,
        get_task_fn=_get_task_fn(cards),
        canonical_status_fn=_canonical_status_fn(),
    )
    found = [r for r in listing if r["id"] == rec["id"]]
    assert found[0]["derived_state"] == "closed"
    assert "successor" in found[0]["active_reason"]


# --- 3. dangling converted_task_id -> ACTIVE, not silently owned -------------

def test_missing_card_is_active_not_owned(init_store: Path):
    rec = _linked(init_store, "missing-card", "T-ghost")
    cards: dict[str, Mapping[str, Any]] = {}

    active = _active_ids(init_store, cards)
    assert rec["id"] in active

    listing = needfix_store.list_needfix(
        init_store, get_task_fn=_get_task_fn(cards), canonical_status_fn=_canonical_status_fn()
    )
    found = [r for r in listing if r["id"] == rec["id"]]
    assert found[0]["derived_state"] == "active"
    assert found[0]["active"] is True
    assert "no longer exists" in found[0]["active_reason"]


# --- 4. Derived at read time; report states no synchronisation ---------------

def test_active_state_is_derived_at_read_time_and_self_heals(init_store: Path):
    cards = {"T-live": _card("T-live", "pending")}
    rec = _linked(init_store, "self-heal", "T-live")

    get_task = _get_task_fn(cards)
    canonical = _canonical_status_fn()

    assert rec["id"] not in _active_ids(init_store, cards)

    cards["T-live"]["status"] = "finished"
    assert rec["id"] not in _active_ids(init_store, cards)
    listing = needfix_store.list_needfix(
        init_store, get_task_fn=get_task, canonical_status_fn=canonical
    )
    assert [r for r in listing if r["id"] == rec["id"]][0]["derived_state"] == "closed"

    cards["T-live"]["status"] = "cancelled"
    assert rec["id"] in _active_ids(init_store, cards)


def test_report_states_read_time_derivation_and_no_sync(init_store: Path):
    cards: dict[str, Mapping[str, Any]] = {}
    report = needfix_store.list_active_needfix(
        init_store, get_task_fn=_get_task_fn(cards), canonical_status_fn=_canonical_status_fn()
    )
    assert report["derivation"] == "read_time_from_linked_card"
    assert report["no_synchronisation"] is True
    assert report["definition"] == needfix_store.ACTIVE_STATE_DEFINITION
    assert "ACTIVE" in report["definition"]


# --- 5. count_needfix and list_needfix agree; both state the definition ------

def test_count_and_list_agree_and_state_definition(init_store: Path):
    cards = {
        "T-pending": _card("T-pending", "pending"),
        "T-finished": _card("T-finished", "finished"),
    }
    _linked(init_store, "owned", "T-pending")
    _linked(init_store, "closed", "T-finished")
    needfix_store.capture_proposal(init_store, title="active-a", description="a")
    needfix_store.capture_proposal(init_store, title="active-b", description="b")

    get_task = _get_task_fn(cards)
    canonical = _canonical_status_fn()

    listed = needfix_store.list_needfix(
        init_store, get_task_fn=get_task, canonical_status_fn=canonical, active_only=True
    )
    counted = needfix_store.count_needfix(
        init_store, get_task_fn=get_task, canonical_status_fn=canonical, active_only=True
    )
    count_report = needfix_store.count_active_needfix(
        init_store, get_task_fn=get_task, canonical_status_fn=canonical
    )
    list_report = needfix_store.list_active_needfix(
        init_store, get_task_fn=get_task, canonical_status_fn=canonical
    )

    assert counted == len(listed) == count_report["count"] == list_report["count"]
    assert count_report["definition"] == list_report["definition"] == needfix_store.ACTIVE_STATE_DEFINITION
    assert count_report["before"] == list_report["before"]
    assert count_report["after"] == list_report["after"] == counted
    assert count_report["raw_total"] == list_report["raw_total"]


def test_before_and_after_counts_are_stated(init_store: Path):
    for i in range(9):
        needfix_store.add_needfix(
            init_store, title=f"archived-{i}", description="archived", status="archived"
        )
    cards = {
        "T-1": _card("T-1", "pending"),
        "T-2": _card("T-2", "finished"),
    }
    _linked(init_store, "owned-noise", "T-1")
    _linked(init_store, "closed-noise", "T-2")
    needfix_store.capture_proposal(init_store, title="signal", description="live")

    raw_count = needfix_store.count_needfix(init_store)
    active = needfix_store.count_active_needfix(
        init_store, get_task_fn=_get_task_fn(cards), canonical_status_fn=_canonical_status_fn()
    )

    assert raw_count == 3
    assert active["count"] == 1
    # The report states the before/after numbers for this repository.
    assert active["raw_total"] == 12  # 9 archived + 2 noise + 1 signal
    assert active["before"] == raw_count == 3
    assert active["after"] == 1


def test_count_and_list_agree_beyond_default_list_limit(init_store: Path):
    """The active count is computed independently of pagination.

    ``list_active_needfix`` returns a page-bounded ``items`` list, but its
    ``count`` must equal ``count_active_needfix`` at every store size --
    including past ``DEFAULT_LIST_LIMIT`` where a page-bounded count would
    silently diverge.
    """
    total = needfix_store.DEFAULT_LIST_LIMIT + 29
    for i in range(total):
        needfix_store.capture_proposal(
            init_store, title=f"active-{i}", description="unlinked active record"
        )

    get_task = _get_task_fn({})
    canonical = _canonical_status_fn()

    count_report = needfix_store.count_active_needfix(
        init_store, get_task_fn=get_task, canonical_status_fn=canonical
    )
    list_report = needfix_store.list_active_needfix(
        init_store, get_task_fn=get_task, canonical_status_fn=canonical
    )

    assert count_report["count"] == total
    # The list view is bounded to one page, but its stated count is the full
    # active total -- the two surfaces agree at a size above the page limit.
    assert len(list_report["items"]) == needfix_store.DEFAULT_LIST_LIMIT
    assert list_report["count"] == count_report["count"] == total
    assert list_report["after"] == count_report["after"] == total


def test_list_active_only_bounds_lookups_to_page(init_store: Path):
    """Page size bounds the derivation work, not just the output.

    Every record links to a missing card, so deriving active state requires a
    ``get_task_fn`` lookup per record. Asking for a small page must not perform
    the lookups for the whole table: the scan stops once the page is full.
    """
    total = needfix_store.DEFAULT_LIST_LIMIT + 40
    for i in range(total):
        rec = needfix_store.capture_proposal(
            init_store, title=f"dangling-{i}", description="dangling card link"
        )
        _set_converted(init_store, rec["id"], f"T-missing-{i}")

    calls = {"n": 0}

    def counting_get_task(task_id: str):
        calls["n"] += 1
        return None  # every linked card is missing -> ACTIVE, one lookup each

    page = 10
    items = needfix_store.list_needfix(
        init_store,
        get_task_fn=counting_get_task,
        canonical_status_fn=_canonical_status_fn(),
        active_only=True,
        limit=page,
    )

    assert len(items) == page
    assert all(item["active"] is True for item in items)
    # Work is bounded by the page: we stop decorating once the page is full
    # instead of looking up every dangling card in the store.
    assert calls["n"] <= page
    assert calls["n"] < total


# --- 6. Reconciliation performs verifiable links and refuses to guess --------

def test_reconcile_unlinked_needfix_links_only_on_verifiable_evidence(init_store: Path):
    """The old contract PROPOSED links from id/title/objective substrings and
    performed none. The new contract PERFORMS the link itself, but only on
    evidence the store can verify -- so a lookalike id, a title substring and an
    objective mention (all of which the old report surfaced as candidates) now
    establish NO link at all."""
    # Accepted record whose deterministic needfix-{NF-ID} card exists -> linked.
    bindable = needfix_store.add_needfix(
        init_store, title="bindable", description="d", status="accepted"
    )["id"]
    det = f"needfix-{bindable}"

    # Accepted records that only RESEMBLE a card must never be linked: a title
    # substring, an objective mention and a lookalike card id are not evidence.
    title_only = needfix_store.add_needfix(
        init_store, title="title-match", description="d", status="accepted"
    )["id"]
    obj_only = needfix_store.add_needfix(
        init_store, title="objective-match", description="d", status="accepted"
    )["id"]

    # An already-linked record is left untouched (idempotent).
    already = needfix_store.add_needfix(
        init_store, title="already-linked", description="d", status="accepted"
    )["id"]
    _set_converted(init_store, already, "T-already")

    cards = {
        det: _card(det, "processing"),
        "T-title": {"id": "T-title", "title": f"Please fix {title_only} now",
                    "objective": "x", "status": "processing"},
        "T-objective": {"id": "T-objective", "title": "x",
                        "objective": f"Objective mentions {obj_only}",
                        "status": "processing"},
        f"card-{title_only}": _card(f"card-{title_only}", "processing"),
        "T-already": _card("T-already", "finished"),
    }

    report = needfix_store.reconcile_unlinked_needfix(
        init_store,
        get_task_fn=_get_task_fn(cards),
        canonical_status_fn=_canonical_status_fn(),
        list_task_cards_fn=lambda: list(cards.values()),
    )

    # Only the deterministic-id record is linked; the coincidences are not.
    assert needfix_store.get_needfix(init_store, bindable)["converted_task_id"] == det
    assert {e["needfix_id"] for e in report["newly_linked"]} == {bindable}
    assert report["before_linked"] == 1  # the already-linked record
    for nfid in (title_only, obj_only):
        assert needfix_store.get_needfix(init_store, nfid)["converted_task_id"] is None
    # The already-linked record is not repointed.
    assert (
        needfix_store.get_needfix(init_store, already)["converted_task_id"]
        == "T-already"
    )


# --- 7. Archived and closed records stay queryable with reasons --------------

def test_archived_and_closed_records_remain_queryable(init_store: Path):
    cards = {"T-done": _card("T-done", "finished")}
    closed = _linked(init_store, "closed-queryable", "T-done")
    archived = needfix_store.add_needfix(
        init_store, title="archived-queryable", description="archived", status="archived"
    )

    listing = needfix_store.list_needfix(
        init_store, get_task_fn=_get_task_fn(cards), canonical_status_fn=_canonical_status_fn()
    )
    closed_row = [r for r in listing if r["id"] == closed["id"]][0]
    assert closed_row["derived_state"] == "closed"
    assert "finished" in closed_row["active_reason"]

    full = needfix_store.list_needfix(init_store, include_archived=True)
    archived_row = [r for r in full if r["id"] == archived["id"]][0]
    assert archived_row["status"] == "archived"
    assert needfix_store.get_needfix(init_store, archived["id"])["status"] == "archived"


# --- 8. The NeedFix's own terminal status is decisive (outer layer) ----------

def _seed_status(
    repo_root: Path, status: str, *, converted_task_id: str | None = None, title: str | None = None
) -> str:
    """Seed one NeedFix and force its own status (and optional link) directly."""
    rec = needfix_store.capture_proposal(
        repo_root, title=title or f"seed-{status}", description=f"seed for {status}"
    )
    conn = sqlite3.connect(str(needfix_store._db_path(repo_root)))
    try:
        conn.execute(
            "UPDATE needfix SET status = ?, converted_task_id = ? WHERE id = ?",
            (status, converted_task_id, rec["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return rec["id"]


# Every status the table can hold, plus an unrecognised one nobody planned for.
ALL_SEEDED_STATUSES = tuple(needfix_store.STATUSES) + ("converting", "mystery_status")


def test_one_record_per_status_shows_exactly_the_non_terminal_set(init_store: Path):
    ids = {status: _seed_status(init_store, status) for status in ALL_SEEDED_STATUSES}

    report = needfix_store.list_active_needfix(
        init_store,
        get_task_fn=_get_task_fn({}),
        canonical_status_fn=_canonical_status_fn(),
        limit=needfix_store.MAX_LIST_LIMIT,
    )
    active_ids = {item["id"] for item in report["items"]}

    # Only records whose OWN status is non-terminal (card-derived) appear.
    expected = {
        ids[status]
        for status in ALL_SEEDED_STATUSES
        if status in needfix_store.NEEDFIX_CARD_DERIVED_STATUSES
    }
    assert active_ids == expected
    # Terminal records (rejected/duplicate/resolved/archived) and an
    # unrecognised status are all absent -- none default to ACTIVE.
    for gone in ("rejected", "duplicate", "resolved", "archived", "mystery_status"):
        assert ids[gone] not in active_ids
    # count agrees with the listed set.
    assert report["count"] == len(expected) == len(active_ids)


def test_terminal_record_stays_off_active_list_without_card_lookup(init_store: Path):
    # A rejected record linked (however it got there) to a card that would
    # otherwise derive ACTIVE must stay off the list, and the card lookup must
    # never run: ``boom`` raises if it is consulted.
    rejected = _seed_status(init_store, "rejected", converted_task_id="T-cancelled")

    def boom(task_id: str):
        raise AssertionError(f"card lookup must not run for a terminal record: {task_id!r}")

    report = needfix_store.list_active_needfix(
        init_store, get_task_fn=boom, canonical_status_fn=_canonical_status_fn()
    )
    assert rejected not in {item["id"] for item in report["items"]}
    assert report["count"] == 0


def test_count_and_list_agree_under_include_archived(init_store: Path):
    cards = {
        "T-own": _card("T-own", "pending"),
        "T-done": _card("T-done", "finished"),
    }
    needfix_store.capture_proposal(init_store, title="live-1", description="live-1")
    needfix_store.capture_proposal(init_store, title="live-2", description="live-2")
    _linked(init_store, "owned-noise", "T-own")
    _linked(init_store, "closed-noise", "T-done")
    needfix_store.add_needfix(
        init_store, title="archived-noise", description="archived", status="archived"
    )

    get_task = _get_task_fn(cards)
    canonical = _canonical_status_fn()

    for include_archived in (False, True):
        report = needfix_store.list_active_needfix(
            init_store,
            get_task_fn=get_task,
            canonical_status_fn=canonical,
            include_archived=include_archived,
            limit=needfix_store.MAX_LIST_LIMIT,
        )
        # count and the full items page describe the same set under every
        # filter the listing honours, include_archived included.
        assert report["count"] == len(report["items"]), include_archived
        # Only the two live records are ever active; archived is terminal.
        assert report["count"] == 2, include_archived
