"""NeedFix work-state projection, count breakdown and id/title query filter.

Owner complaint 2026-11-12: needfix_count said 311 "active" rows that mixed
never-triaged captures, decided-but-unstarted rows and the few with a card.
These tests pin the read-time WORK STATE projection layered on
``derive_active_state`` (whose semantics and count stay unchanged), the
``breakdown=true`` count, the ``work_state`` list filter, and the
NF-2026-02168 ``query`` filter on needfix_list/roadmap_list.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Mapping

import pytest

from aiworkhub import core, needfix_ingest, needfix_store, task_store
from test_needfix_operator_surfaces_derived import _insert_task, _link


@pytest.fixture
def store() -> Path:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        assert needfix_store.initialize_repository(root)["initialized"] is True
        yield root


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> Path:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
        monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
        yield root


def _card(task_id: str, status: str) -> dict[str, Any]:
    return {"id": task_id, "title": task_id, "status": status, "superseded_by": None}


class _CountingCards:
    """A ``get_task_fn`` over fixed cards that counts every lookup."""

    def __init__(self, cards: Mapping[str, Mapping[str, Any]]) -> None:
        self.cards = dict(cards)
        self.calls = 0

    def __call__(self, task_id: str):
        self.calls += 1
        return self.cards.get(task_id)


def _status(card: Mapping[str, Any]) -> str:
    return card["status"]


def _row(store: Path, title: str, status: str, task_id: str | None = None) -> str:
    rec = needfix_store.capture_proposal(store, title=title, description=title)
    conn = sqlite3.connect(str(needfix_store._db_path(store)))
    try:
        conn.execute(
            "UPDATE needfix SET status = ?, converted_task_id = ? WHERE id = ?",
            (status, task_id, rec["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return rec["id"]


def _seed_every_work_state(store: Path) -> tuple[dict[str, str], _CountingCards]:
    """One row per work state (plus a terminal one); returns id -> expected work state."""
    cards = {
        "T-pending": _card("T-pending", "pending"),
        "T-blocked": _card("T-blocked", "blocked"),
        "T-cancelled": _card("T-cancelled", "cancelled"),
        "T-finished": _card("T-finished", "finished"),
    }
    expected = {
        _row(store, "never triaged", "captured"): "untriaged",
        _row(store, "accepted no card", "accepted"): "decided_no_card",
        _row(store, "triaged no card", "triaged"): "decided_no_card",
        _row(store, "owned by pending card", "task_created", "T-pending"): "in_work",
        _row(store, "blocked card", "task_created", "T-blocked"): "stalled",
        _row(store, "cancelled card", "task_created", "T-cancelled"): "reopened",
        _row(store, "parked", "deferred"): "deferred",
        _row(store, "fixed by card", "task_created", "T-finished"): "closed",
        _row(store, "rejected outright", "rejected"): "closed",
        _row(store, "dangling card", "task_created", "T-missing"): "decided_no_card",
    }
    return expected, _CountingCards(cards)


# --- 1. derive_work_state is pure and total ------------------------------------


def test_derive_work_state_covers_each_of_the_seven_work_states():
    cases = [
        ({"status": "captured"}, {"state": "active"}, "untriaged"),
        ({"status": "accepted"}, {"state": "active"}, "decided_no_card"),
        ({"status": "triaged"}, {"state": "active"}, "decided_no_card"),
        ({"status": "task_created"}, {"state": "owned"}, "in_work"),
        ({"status": "task_created"}, {"state": "stalled"}, "stalled"),
        ({"status": "task_created"}, {"state": "reopened"}, "reopened"),
        ({"status": "deferred"}, {"state": "active"}, "deferred"),
        ({"status": "resolved"}, {"state": "closed"}, "closed"),
    ]
    seen = set()
    for row, derived, expected in cases:
        assert needfix_store.derive_work_state(row, derived) == expected
        seen.add(expected)
    assert seen == set(needfix_store.WORK_STATES)
    assert len(needfix_store.WORK_STATES) == 7


def test_every_derived_state_maps_to_exactly_one_work_state_preserving_activeness():
    active_states = {"active", "stalled", "reopened"}
    statuses = sorted(needfix_store.NEEDFIX_CARD_DERIVED_STATUSES | {""})
    for state in needfix_store.ACTIVE_STATES:
        for own_status in statuses:
            work_state = needfix_store.derive_work_state({"status": own_status}, {"state": state})
            assert work_state in needfix_store.WORK_STATES
            # Active work states are exactly the derived-active rows, so they sum to the count.
            assert (work_state in needfix_store.ACTIVE_WORK_STATES) == (state in active_states)
    with pytest.raises(needfix_store.NeedFixValidationError):
        needfix_store.derive_work_state({"status": "captured"}, {"state": "made_up"})


def test_work_state_definition_states_every_work_state_next_to_the_active_one():
    for work_state in needfix_store.WORK_STATES:
        assert work_state in needfix_store.WORK_STATE_DEFINITION
    assert needfix_store.ACTIVE_STATE_DEFINITION  # unchanged and still exported


def test_derive_active_state_output_is_unchanged_by_the_projection():
    cards = _CountingCards({"T-p": _card("T-p", "pending")})
    derived = needfix_store.derive_active_state(
        {"status": "task_created", "converted_task_id": "T-p", "tags": []}, cards, _status
    )
    assert derived == {"state": "owned", "active": False, "reason": "owned by pending task 'T-p'"}


# --- 2. list items carry work_state and the linked card; work_state filter -----


def test_list_items_carry_work_state_and_linked_card(store: Path):
    expected, cards = _seed_every_work_state(store)
    items = needfix_store.list_needfix(
        store, get_task_fn=cards, canonical_status_fn=_status, limit=500
    )
    by_id = {item["id"]: item for item in items}
    assert {i: by_id[i]["work_state"] for i in expected} == expected
    owned = next(i for i, ws in expected.items() if ws == "in_work")
    assert by_id[owned]["linked_card_id"] == "T-pending"
    assert by_id[owned]["linked_card_status"] == "pending"
    untriaged = next(i for i, ws in expected.items() if ws == "untriaged")
    assert by_id[untriaged]["linked_card_id"] is None
    assert by_id[untriaged]["linked_card_status"] is None

    report = needfix_store.list_active_needfix(store, get_task_fn=cards, canonical_status_fn=_status)
    for item in report["items"]:
        assert item["work_state"] in needfix_store.ACTIVE_WORK_STATES
        assert item["work_state"] == expected[item["id"]]


def test_work_state_filter_returns_only_matches_and_pages_stably(store: Path):
    for i in range(7):
        _row(store, f"undecided {i}", "captured")
        _row(store, f"decided {i}", "accepted")
    cards = _CountingCards({})
    full = needfix_store.list_needfix(
        store, get_task_fn=cards, canonical_status_fn=_status, work_state="decided_no_card",
        limit=500,
    )
    assert len(full) == 7
    assert all(item["work_state"] == "decided_no_card" for item in full)
    paged: list[dict[str, Any]] = []
    for offset in range(0, 9, 3):
        paged.extend(
            needfix_store.list_needfix(
                store, get_task_fn=cards, canonical_status_fn=_status,
                work_state="decided_no_card", limit=3, offset=offset,
            )
        )
    assert [item["id"] for item in paged] == [item["id"] for item in full]
    assert needfix_store.count_needfix(
        store, get_task_fn=cards, canonical_status_fn=_status, work_state="decided_no_card"
    ) == len(full)

    report = needfix_store.list_active_needfix(
        store, get_task_fn=cards, canonical_status_fn=_status, work_state="untriaged", limit=2,
        offset=2,
    )
    assert report["count"] == 7
    assert len(report["items"]) == 2
    assert {item["work_state"] for item in report["items"]} == {"untriaged"}

    with pytest.raises(needfix_store.NeedFixValidationError):
        needfix_store.list_needfix(store, work_state="untriaged")  # needs the derivation hooks
    with pytest.raises(needfix_store.NeedFixValidationError):
        needfix_store.list_needfix(
            store, get_task_fn=cards, canonical_status_fn=_status, work_state="finished"
        )


# --- 3. breakdown: same candidate set, active work states sum to count --------


def test_work_state_breakdown_partitions_the_candidates_and_sums_to_the_active_count(store: Path):
    expected, cards = _seed_every_work_state(store)
    report = needfix_store.work_state_breakdown(store, get_task_fn=cards, canonical_status_fn=_status)
    active_count = needfix_store.count_needfix(
        store, get_task_fn=cards, canonical_status_fn=_status, active_only=True
    )
    assert report["active_count"] == active_count
    assert sum(report["by_work_state"][s] for s in needfix_store.ACTIVE_WORK_STATES) == active_count
    assert set(report["by_work_state"]) == set(needfix_store.WORK_STATES)
    assert sum(report["by_work_state"].values()) == report["total"] == len(expected)
    assert sum(report["by_status"].values()) == report["total"]
    assert report["by_status"]["task_created"] == 5
    for work_state in needfix_store.WORK_STATES:
        assert report["by_work_state"][work_state] == sum(
            1 for ws in expected.values() if ws == work_state
        )


# --- 4. no extra card lookup and no per-row SQL ----------------------------------


def test_projection_adds_no_card_lookup_beyond_the_active_count(store: Path):
    _expected, baseline = _seed_every_work_state(store)
    needfix_store.count_active_needfix(store, get_task_fn=baseline, canonical_status_fn=_status)
    assert baseline.calls > 0

    for run in (
        lambda fn: needfix_store.work_state_breakdown(store, get_task_fn=fn, canonical_status_fn=_status),
        lambda fn: needfix_store.list_active_needfix(
            store, get_task_fn=fn, canonical_status_fn=_status, limit=500
        ),
        lambda fn: needfix_store.list_active_needfix(
            store, get_task_fn=fn, canonical_status_fn=_status, work_state="stalled"
        ),
    ):
        counting = _CountingCards(baseline.cards)
        run(counting)
        assert counting.calls == baseline.calls


def test_statement_count_does_not_grow_with_the_row_count(store: Path, monkeypatch):
    statements: list[str] = []
    real_connect = needfix_store._connect

    def tracing_connect(repo_root):
        conn = real_connect(repo_root)
        conn.set_trace_callback(statements.append)
        return conn

    def run_once() -> int:
        statements.clear()
        with monkeypatch.context() as patch:
            patch.setattr(needfix_store, "_connect", tracing_connect)
            cards = _CountingCards({})
            needfix_store.work_state_breakdown(store, get_task_fn=cards, canonical_status_fn=_status)
            needfix_store.list_active_needfix(
                store, get_task_fn=cards, canonical_status_fn=_status, work_state="untriaged"
            )
        return sum(1 for sql in statements if "needfix" in sql.lower())

    for i in range(5):
        _row(store, f"first batch {i}", "captured")
    small = run_once()
    for i in range(20):
        _row(store, f"second batch {i}", "accepted")
    assert run_once() == small


# --- 5. query filter (NF-2026-02168) -------------------------------------------


def test_store_query_is_case_insensitive_over_id_and_title_before_paging(store: Path):
    alpha = _row(store, "Alpha WIDGET leak", "captured")
    beta = _row(store, "beta widget crash", "captured")
    _row(store, "gamma unrelated", "captured")
    hits = needfix_store.list_needfix(store, query="  Widget ", order_dir="ASC")
    assert [row["id"] for row in hits] == [alpha, beta]
    assert [row["id"] for row in needfix_store.list_needfix(
        store, query="widget", order_dir="ASC", limit=1, offset=1
    )] == [beta]
    assert [row["id"] for row in needfix_store.list_needfix(store, query=alpha.lower())] == [alpha]
    assert needfix_store.count_needfix(store, query="WIDGET") == 2
    assert needfix_store.list_needfix(store, query="") == needfix_store.list_needfix(store)
    with pytest.raises(needfix_store.NeedFixValidationError):
        needfix_store.list_needfix(store, query="x" * (needfix_store.MAX_QUERY_CHARS + 1))


def test_query_paged_filters_before_paging_and_passes_through_when_empty():
    rows = [{"id": f"RM-2026-{i:05d}", "title": "Widget" if i % 2 else "other"} for i in range(9)]
    calls: list[tuple[int, int]] = []

    def fetch(limit: int, offset: int):
        calls.append((limit, offset))
        return rows[offset:offset + min(limit, 4)]  # a store that clamps its own page size

    sentinel = fetch(3, 2)
    calls.clear()
    assert needfix_store.query_paged(fetch, query="", limit=3, offset=2) == sentinel
    assert calls == [(3, 2)]
    matched = needfix_store.query_paged(fetch, query="WIDGET", limit=2, offset=1)
    assert [row["id"] for row in matched] == ["RM-2026-00003", "RM-2026-00005"]


# --- 6. the operator doors: core and MCP -------------------------------------------


def _jsonable(rows) -> str:
    return json.dumps(list(rows), sort_keys=True, default=str)


def test_core_needfix_count_default_unchanged_and_breakdown_sums(repo: Path):
    task_store.initialize_repository(repo)
    _insert_task(repo, "T-fin", "finished")
    _insert_task(repo, "T-pend", "pending")
    _link(repo, "already landed", "T-fin")
    _link(repo, "in flight", "T-pend")
    needfix_store.capture_proposal(repo, title="live one", description="open")
    needfix_store.capture_proposal(repo, title="live two", description="open")

    plain = core.needfix_count()
    assert isinstance(plain, core.NeedfixCount)
    assert plain.derived is True
    assert int(plain) == needfix_ingest.count_active(repo)["count"] == 2

    report = core.needfix_count(breakdown=True)
    assert report["count"] == int(plain)
    assert report["derived"] is True
    assert sum(report["by_work_state"][s] for s in needfix_store.ACTIVE_WORK_STATES) == report["count"]
    assert report["by_work_state"]["untriaged"] == 2
    assert report["by_work_state"]["in_work"] == 1
    assert report["by_work_state"]["closed"] == 1
    assert sum(report["by_status"].values()) == sum(report["by_work_state"].values())

    filtered = core.needfix_count(status="captured", breakdown=True)
    assert filtered["count"] == int(core.needfix_count(status="captured")) == 2
    assert filtered["derived"] is False


def test_breakdown_without_task_store_reports_the_plain_filtered_underived_reason(repo: Path):
    needfix_store.capture_proposal(repo, title="live one", description="open")
    plain = core.needfix_count(status="captured")
    report = core.needfix_count(status="captured", breakdown=True)
    assert report["by_work_state"] is None  # no ready task store
    assert report["underived_reason"] == plain.underived_reason


def test_core_needfix_list_query_filters_before_paging_and_empty_is_identical(repo: Path):
    task_store.initialize_repository(repo)
    alpha = needfix_store.capture_proposal(repo, title="Alpha Widget", description="a")["id"]
    beta = needfix_store.capture_proposal(repo, title="beta WIDGET", description="b")["id"]
    needfix_store.capture_proposal(repo, title="gamma", description="c")

    assert _jsonable(core.needfix_list(query="")) == _jsonable(core.needfix_list())
    hits = core.needfix_list(query="widget", order_dir="ASC")
    assert hits.derived is True
    assert [row["id"] for row in hits] == [alpha, beta]
    assert all(row["work_state"] == "untriaged" for row in hits)
    page = core.needfix_list(query="widget", order_dir="ASC", limit=1, offset=1)
    assert [row["id"] for row in page] == [beta]
    raw = core.needfix_list(status="captured", query="ALPHA")
    assert [row["id"] for row in raw] == [alpha]
    assert raw.derived is False


def test_core_roadmap_list_query_filters_and_empty_is_identical(repo: Path):
    first = core.roadmap_add("Ship the widget dashboard", "operators see widgets")["id"]
    core.roadmap_add("Harden storage", "no data loss")
    assert _jsonable(core.roadmap_list(query="")) == _jsonable(core.roadmap_list())
    assert [row["id"] for row in core.roadmap_list(query="WIDGET")] == [first]
    assert [row["id"] for row in core.roadmap_list(query=first.lower())] == [first]
    assert core.roadmap_list(query="widget", offset=1) == []


def test_mcp_tools_accept_the_new_arguments():
    from aiworkhub import server

    assert inspect.signature(server.needfix_list).parameters["query"].default == ""
    assert inspect.signature(server.roadmap_list).parameters["query"].default == ""
    assert inspect.signature(server.needfix_count).parameters["breakdown"].default is False
    for fn, arg in (
        (server.needfix_list, "query"),
        (server.roadmap_list, "query"),
        (server.needfix_count, "breakdown"),
    ):
        assert f"{arg}:" in (inspect.getdoc(fn) or "")
