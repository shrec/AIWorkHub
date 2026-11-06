"""NF-2026-02112: needfix_list derives the active set from narrow reads.

``needfix_list(limit=100)`` used to hold every task card (``SELECT t.*`` +
``_decode_task_card`` for up to 5000 rows) and every NeedFix row (``SELECT *``,
twice: once for the page, once for the count) to show one page -- a 976 MB
peak on a mature store. The derivation needs a handful of scalar fields, so:

* the task store answers it with ``ACTIVE_DERIVATION_PROJECTION`` cards that
  carry only the fields the derivation, the terminal-artifact projection and
  the link reconciliation inspect;
* the NeedFix store derives the active set ONCE per call from the narrow
  derivation columns and reads full rows only for the returned page.

These tests prove the answers are byte-identical to the full-card derivation
and to the pre-change store algorithm, that the derivation path never decodes
a full card, and that the traced peak on a large seeded store is bounded by
the page rather than by the store.
"""

from __future__ import annotations

import itertools
import json
import os
import sqlite3
import tracemalloc
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from aiworkhub import core, needfix_ingest, needfix_store, task_plan, task_store

_PROJECTION = task_store.ACTIVE_DERIVATION_PROJECTION


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


class _TickingClock:
    """A deterministic ``_utcnow_iso``: two identically driven repos match."""

    def __init__(self) -> None:
        self.reset(0)

    def reset(self, base: int) -> None:
        self._ticks = itertools.count(base)

    def __call__(self) -> str:
        tick = next(self._ticks)
        return f"2026-10-{1 + tick // 86_400 % 28:02d}T{tick // 3600 % 24:02d}:{tick // 60 % 60:02d}:{tick % 60:02d}+00:00"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _TickingClock:
    ticking = _TickingClock()
    monkeypatch.setattr(needfix_store, "_utcnow_iso", ticking)
    return ticking


def _deterministic_uuids(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = itertools.count(1)
    monkeypatch.setattr(needfix_store.uuid, "uuid4", lambda: uuid.UUID(int=next(counter)))


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------


def _task_conn(root: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(task_store.canonical_db_path(root)))


def _insert_tasks(root: Path, rows: list[tuple[str, str, str, str, str, str]]) -> None:
    """Rows are ``(task_id, status, worker_status, archived_at, updated_at, card_json)``."""
    conn = _task_conn(root)
    try:
        conn.executemany(
            "INSERT INTO tasks (task_id, status, worker_status, archived_at, created_at, "
            "updated_at, card_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(tid, st, ws, arch, upd, upd, card) for tid, st, ws, arch, upd, card in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _task_row(
    task_id: str,
    status: str,
    *,
    worker_status: str = "unclaimed",
    archived_at: str = "",
    index: int = 0,
    raw_card_json: str | None = None,
    **card_fields: Any,
) -> tuple[str, str, str, str, str, str]:
    card_json = (
        raw_card_json
        if raw_card_json is not None
        else json.dumps({"task_id": task_id, "status": status, **card_fields})
    )
    updated_at = f"2026-09-01T00:{index // 60 % 60:02d}:{index % 60:02d}+00:00"
    return task_id, status, worker_status, archived_at, updated_at, card_json


def _needfix_sql(root: Path, sql: str, params: tuple[Any, ...]) -> None:
    conn = sqlite3.connect(str(needfix_store._db_path(root)))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _link(root: Path, title: str, task_id: str, *, tags: list[str] | None = None) -> str:
    rec = needfix_store.capture_proposal(root, title=title, description=title, tags=tags)
    _needfix_sql(
        root,
        "UPDATE needfix SET converted_task_id = ?, status = 'task_created' WHERE id = ?",
        (task_id, rec["id"]),
    )
    return str(rec["id"])


def _accepted(root: Path, title: str) -> dict[str, Any]:
    rec = needfix_store.capture_proposal(root, title=title, description=title)
    needfix_store.triage_needfix(root, rec["id"])
    return needfix_store.accept_needfix(root, rec["id"])


_SHAPED_TASKS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("T-fin", "finished", {"worker_status": "done"}),
    ("T-pend", "pending", {}),
    ("T-claimed", "pending", {"worker_status": "claimed"}),
    ("T-review", "review", {}),
    ("T-blocked", "blocked", {}),
    ("T-rework", "rework", {}),
    (
        "T-acc",
        "pending",
        {
            "accepted_request_id": "req-1",
            "accepted_by": "owner",
            "accepted_at": "2026-09-01T00:00:00+00:00",
            "accept_evidence": {"receipt": {"log": "x" * 2000}},
        },
    ),
    (
        "T-acc-unproven",
        "pending",
        {
            "accepted_request_id": "req-2",
            "accepted_by": "owner",
            "accepted_at": "2026-09-01T00:00:00+00:00",
            "accept_evidence": "not-a-mapping",
        },
    ),
    ("T-cancel", "pending", {"archive_operation": "cancelled"}),
    (
        "T-sup-landed",
        "superseded",
        {
            "archived_at": "2026-09-02T00:00:00+00:00",
            "archive_operation": "superseded",
            "superseded_by": "T-fin",
        },
    ),
    (
        "T-sup-open",
        "superseded",
        {
            "archived_at": "2026-09-02T00:00:00+00:00",
            "archive_operation": "superseded",
            "archive_reason": "superseded_by:T-pend; reviewer displaced it",
        },
    ),
    (
        "T-sup-hop",
        "pending",
        {"archive_operation": "superseded", "archive_reason": "superseded_by:T-sup-landed"},
    ),
    (
        "QR-done",
        "review",
        {
            "quality_review": {
                "target_task_id": "T-fin",
                "target_status": "finished",
                "findings": ["f" * 500],
            }
        },
    ),
    (
        "QR-nested",
        "review",
        {"terminal_review": {"evidence": {"quality_review": {"target_task_id": "T-acc"}}}},
    ),
    (
        "IMPL-hop",
        "pending",
        {"implementation": {"target_task_id": "T-sup-hop", "target_status": "superseded"}},
    ),
    ("RT-done", "pending", {"retry_target": {"task_id": "T-fin", "kind": "odd"}}),
    ("QR-open", "review", {"quality_review": {"target_task_id": "T-pend"}}),
    ("QR-not-mapping", "review", {"quality_review": "T-fin"}),
)

_RAW_CARD_TASKS: tuple[tuple[str, str], ...] = (
    ("T-bad-json", "not json{"),
    ("T-array-json", "[1, 2]"),
    ("T-nan", '{"archive_operation": "cancelled", "x": NaN}'),
)


def _seed_golden(root: Path) -> None:
    """Every card shape and NeedFix state the derivation distinguishes."""
    task_store.initialize_repository(root)
    rows = []
    for index, (task_id, status, fields) in enumerate(_SHAPED_TASKS):
        fields = dict(fields)
        worker_status = fields.pop("worker_status", "unclaimed")
        archived_at = fields.pop("archived_at", "")
        rows.append(
            _task_row(
                task_id,
                status,
                worker_status=worker_status,
                archived_at=archived_at,
                index=index,
                **fields,
            )
        )
    for offset, (task_id, raw) in enumerate(_RAW_CARD_TASKS, start=len(rows)):
        rows.append(_task_row(task_id, "pending", index=offset, raw_card_json=raw))
    for task_id, _status, _fields in _SHAPED_TASKS:
        _link(root, f"linked to {task_id}", task_id)
    for task_id, _raw in _RAW_CARD_TASKS:
        _link(root, f"linked to {task_id}", task_id)
    _link(root, "foreign card gone", "T-gone", tags=[needfix_store.FOREIGN_ORIGIN_CARD_TAG])
    _link(root, "card gone", "T-gone-2")
    needfix_store.capture_proposal(root, title="captured one", description="open")
    needfix_store.capture_proposal(root, title="captured two", description="open")
    explicit = _accepted(root, "merged into a multi-finding card")
    deterministic = _accepted(root, "bound by its deterministic id")
    _accepted(root, "accepted without any card")
    rejected = needfix_store.capture_proposal(root, title="rejected", description="no")
    _needfix_sql(
        root,
        "UPDATE needfix SET status = 'rejected', converted_task_id = 'T-cancel' WHERE id = ?",
        (rejected["id"],),
    )
    needfix_store.add_needfix(
        root, title="archived noise", description="archived", status="archived"
    )
    deterministic_id = needfix_store._deterministic_task_id_for(deterministic)
    assert deterministic_id
    index = len(rows)
    rows.append(
        _task_row(
            "T-merge",
            "finished",
            worker_status="done",
            index=index,
            needfix_ids=[explicit["id"]],
        )
    )
    rows.append(_task_row(deterministic_id, "pending", index=index + 1))
    _insert_tasks(root, rows)


def _full_cards(root: Path) -> list[dict[str, Any]]:
    return task_store.list_task_cards(root, limit=needfix_ingest._RECONCILE_CARD_SCAN_LIMIT)


def _legacy_store_report(
    root: Path,
    derived: Mapping[str, Any],
    *,
    include_archived: bool,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """The pre-NF-02112 ``list_active_needfix``: every whole row, derived twice."""
    get_task_fn = derived["get_task_fn"]
    canonical_status_fn = derived["canonical_status_fn"]
    where = "" if include_archived else "WHERE status != 'archived'"
    conn = sqlite3.connect(str(needfix_store._db_path(root)))
    conn.row_factory = sqlite3.Row
    try:
        needed = offset + limit
        page: list[dict[str, Any]] = []
        for row in conn.execute(f"SELECT * FROM needfix {where} ORDER BY created_at DESC"):
            if len(page) >= needed:
                break
            decorated = needfix_store._decorate_derived(
                needfix_store._row_to_dict(row), get_task_fn, canonical_status_fn
            )
            if decorated["active"]:
                page.append(decorated)
        every_row = conn.execute(f"SELECT * FROM needfix {where}").fetchall()
        count = sum(
            1
            for row in every_row
            if needfix_store.derive_active_state(
                needfix_store._row_to_dict(row), get_task_fn, canonical_status_fn
            )["active"]
        )
    finally:
        conn.close()
    return {
        "count": count,
        "items": page[offset:needed],
        "terminal_artifacts_excluded": derived["excluded"],
    }


# --------------------------------------------------------------------------
# Golden: the narrow derivation answers byte-identically
# --------------------------------------------------------------------------


def test_projection_answers_every_derivation_reader_like_the_full_card(
    tmp_path: Path, clock: _TickingClock
) -> None:
    _seed_golden(tmp_path)
    full = _full_cards(tmp_path)
    narrow = task_store.list_task_cards(
        tmp_path, limit=needfix_ingest._RECONCILE_CARD_SCAN_LIMIT, projection=_PROJECTION
    )
    assert [card["task_id"] for card in narrow] == [card["task_id"] for card in full]
    by_id = {card["task_id"]: card for card in full}
    bulk = task_store.get_tasks(tmp_path, list(by_id), projection=_PROJECTION)
    readers: tuple[Callable[[Mapping[str, Any]], Any], ...] = (
        task_store.canonical_status,
        task_plan.card_status_evidence,
        task_plan.successor_task_id,
        task_plan.exact_artifact_target,
        needfix_store._superseded_by,
        needfix_store._card_explicit_needfix_refs,
        lambda card: isinstance(card.get("accept_evidence"), Mapping),
        lambda card: (card.get("archive_operation"), card.get("accepted_at")),
    )
    for card in narrow:
        reference = by_id[card["task_id"]]
        assert task_store.get_task(tmp_path, card["task_id"], projection=_PROJECTION) == card
        assert bulk[card["task_id"]] == card
        for reader in readers:
            assert reader(card) == reader(reference), (card["task_id"], reader)
        # Bodies the derivation never reads are not carried.
        assert "objective" not in card and "history" not in card
        assert "findings" not in (card.get("quality_review") or {})
    cards_by_id = {card["task_id"]: card for card in narrow}
    assert task_plan.terminal_artifact_projection(
        narrow, cards_by_id
    ) == task_plan.terminal_artifact_projection(full, by_id)
    assert task_plan.terminal_artifact_projection(full, by_id)[1], "seed must exclude artifacts"
    with pytest.raises(ValueError, match="unknown task card projection"):
        task_store.get_task(tmp_path, "T-fin", projection="everything")


def test_list_and_count_are_byte_identical_to_the_full_card_derivation(
    tmp_path: Path, clock: _TickingClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrow_root = tmp_path / "narrow"
    full_root = tmp_path / "full"
    for root in (narrow_root, full_root):
        root.mkdir()
        clock.reset(0)
        _seed_golden(root)

    def narrow_reports(**window: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        include_archived = window.get("include_archived", False)
        return (
            needfix_ingest.list_active(narrow_root, **window),
            needfix_ingest.count_active(narrow_root, include_archived=include_archived),
        )

    def full_reports(**window: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        include_archived = window.get("include_archived", False)
        snapshot = {"task_cards_snapshot": _full_cards(full_root), "task_cards_snapshot_complete": True}
        return (
            needfix_ingest.list_active(full_root, **snapshot, **window),
            needfix_ingest.count_active(full_root, include_archived=include_archived, **snapshot),
        )

    # The first read reconciles (links, orphan returns): both repos must make
    # the very same writes, so drive each from the same clock and uuid stream.
    for root, reports in ((narrow_root, narrow_reports), (full_root, full_reports)):
        clock.reset(10_000)
        _deterministic_uuids(monkeypatch)
        first = reports()
        assert first[0]["derived"] is True, (root, first[0]["underived_reason"])
    stored = [
        needfix_store.list_needfix(root, include_archived=True, limit=needfix_store.MAX_LIST_LIMIT)
        for root in (narrow_root, full_root)
    ]
    assert _canonical_json(stored[0]) == _canonical_json(stored[1])
    linked = {row["title"]: row["converted_task_id"] for row in stored[0]}
    assert linked["merged into a multi-finding card"] == "T-merge"  # explicit-reference route

    windows: list[dict[str, Any]] = [
        {"include_archived": include_archived, "limit": limit, "offset": offset, "order_dir": order_dir}
        for include_archived in (False, True)
        for limit, offset in ((needfix_store.MAX_LIST_LIMIT, 0), (3, 2))
        for order_dir in ("DESC", "ASC")
    ]
    for window in windows:
        narrow = narrow_reports(**window)
        assert _canonical_json(narrow) == _canonical_json(full_reports(**window)), window
    listed, counted = narrow_reports(limit=needfix_store.MAX_LIST_LIMIT)
    states = {item["derived_state"] for item in listed["items"]}
    assert {"active", "reopened"} <= states
    assert listed["count"] == counted["count"] == len(listed["items"]) >= 4
    assert listed["terminal_artifacts_excluded_count"] >= 1

    # And identical to the pre-change store algorithm (whole rows, derived twice).
    for include_archived in (False, True):
        for limit, offset in ((needfix_store.MAX_LIST_LIMIT, 0), (3, 2)):
            derived, reason = needfix_ingest._derive_active_surface(
                narrow_root,
                task_cards_snapshot=_full_cards(narrow_root),
                task_cards_snapshot_complete=True,
                include_archived=include_archived,
            )
            assert reason is None and derived is not None
            legacy = _legacy_store_report(
                narrow_root,
                derived,
                include_archived=include_archived,
                limit=limit,
                offset=offset,
            )
            report = needfix_ingest.list_active(
                narrow_root, include_archived=include_archived, limit=limit, offset=offset
            )
            current = {
                "count": report["count"],
                "items": report["items"],
                "terminal_artifacts_excluded": report["terminal_artifacts_excluded"],
            }
            assert _canonical_json(current) == _canonical_json(legacy)


# --------------------------------------------------------------------------
# The derivation path never decodes a full card
# --------------------------------------------------------------------------


def test_needfix_list_derivation_reads_no_full_card_and_page_only_rows(
    tmp_path: Path, clock: _TickingClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    _seed_golden(tmp_path)
    needfix_ingest.list_active(tmp_path)  # settle the read-time reconcile writes

    calls: dict[str, list[Any]] = {
        "decode": [], "list_task_cards": [], "get_task": [], "get_tasks": [], "row_to_dict": [],
    }

    def spy(module: Any, name: str, key: str, record: Callable[..., Any]) -> None:
        real = getattr(module, name)

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            calls[key].append(record(*args, **kwargs))
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapped)

    spy(task_store, "_decode_task_card", "decode", lambda row, **_kw: row["task_id"])
    for name in ("list_task_cards", "get_task", "get_tasks"):
        spy(task_store, name, name, lambda *_a, **kw: kw.get("projection"))
    spy(needfix_store, "_row_to_dict", "row_to_dict", lambda row: row["id"])

    listing = core.needfix_list(limit=3)

    assert listing.derived is True
    assert len(listing) == 3
    assert calls["decode"] == []
    assert calls["list_task_cards"] == [_PROJECTION]
    assert set(calls["get_task"]) <= {_PROJECTION}
    assert set(calls["get_tasks"]) <= {_PROJECTION}
    # Full NeedFix rows are read for the returned page only.
    assert calls["row_to_dict"] == [row["id"] for row in listing]

    for values in calls.values():
        values.clear()
    counted = needfix_ingest.count_active(tmp_path)
    assert counted["derived"] is True and counted["count"] >= 4
    assert calls["decode"] == [] and calls["row_to_dict"] == []
    assert calls["list_task_cards"] == [_PROJECTION]


# --------------------------------------------------------------------------
# Measurement: traced peak bounded by the page, not by the store
# --------------------------------------------------------------------------

_MEASURE_TASKS = 2000
_MEASURE_HISTORY_ENTRIES = 100  # ~50 KB of card_json per task
_MEASURE_NEEDFIX = 500
_MEASURE_BODY_BYTES = 20_000
_MEASURE_PAGE = 100
# Today's derivation exceeds this on the seeded store by several times; the
# narrow derivation stays well inside it.
_PEAK_BOUND_BYTES = 32 * 1024 * 1024


def seed_measurement_store(root: Path) -> None:
    """>= 2000 tasks of ~50 KB card_json and >= 500 NeedFix rows with large bodies."""
    task_store.initialize_repository(root)
    history = [
        {"event": f"event-{entry:03d}", "note": "n" * 480}
        for entry in range(_MEASURE_HISTORY_ENTRIES)
    ]
    rows = []
    for index in range(_MEASURE_TASKS):
        task_id = f"T-{index:05d}"
        shape = index % 4
        fields: dict[str, Any] = {"objective": f"objective {index}", "history": history}
        status, worker_status = "pending", "unclaimed"
        if shape == 0:
            status, worker_status = "finished", "done"
        elif shape == 2:
            fields["archive_operation"] = "cancelled"
        elif shape == 3:
            status = "review"
            fields["quality_review"] = {"target_task_id": f"T-{index - 3:05d}"}
        rows.append(
            _task_row(task_id, status, worker_status=worker_status, index=index, **fields)
        )
    _insert_tasks(root, rows)
    assert len(rows[0][5]) >= 50_000

    template = needfix_store.capture_proposal(root, title="template", description="t")
    conn = sqlite3.connect(str(needfix_store._db_path(root)))
    conn.row_factory = sqlite3.Row
    try:
        columns = [info["name"] for info in conn.execute("PRAGMA table_info(needfix)")]
        source = dict(conn.execute("SELECT * FROM needfix WHERE id = ?", (template["id"],)).fetchone())
        prefix = str(template["id"])[: -5]
        clones = []
        for index in range(_MEASURE_NEEDFIX):
            row = dict(source)
            linked = index % 2 == 1
            row.update(
                id=f"{prefix}{90_000 + index:05d}",
                dedupe_key=f"measure-{index}",
                title=f"measured finding {index}",
                description="d" * _MEASURE_BODY_BYTES,
                evidence_json=json.dumps({"log": "e" * _MEASURE_BODY_BYTES}),
                status="task_created" if linked else "captured",
                converted_task_id=f"T-{index % _MEASURE_TASKS:05d}" if linked else None,
                created_at=f"2026-10-01T{index // 3600:02d}:{index // 60 % 60:02d}:{index % 60:02d}+00:00",
            )
            clones.append(tuple(row[name] for name in columns))
        conn.execute("BEGIN")
        conn.executemany(
            f"INSERT INTO needfix ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            clones,
        )
        conn.commit()
    finally:
        conn.close()


def _traced_peak(fn: Callable[[], Any]) -> tuple[Any, int]:
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        baseline, _peak = tracemalloc.get_traced_memory()
        result = fn()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        if not was_tracing:
            tracemalloc.stop()
    return result, peak - baseline


def _legacy_needfix_list(root: Path, *, limit: int) -> dict[str, Any]:
    """Today's (pre-NF-02112) algorithm: full cards + every whole NeedFix row."""
    cards = task_store.list_task_cards(root, limit=needfix_ingest._RECONCILE_CARD_SCAN_LIMIT)
    derived, reason = needfix_ingest._derive_active_surface(
        root,
        task_cards_snapshot=cards,
        task_cards_snapshot_complete=len(cards) < needfix_ingest._RECONCILE_CARD_SCAN_LIMIT,
    )
    assert reason is None and derived is not None
    return _legacy_store_report(root, derived, include_archived=False, limit=limit, offset=0)


def measure_needfix_list_peaks(root: Path) -> dict[str, Any]:
    """Traced peak bytes of ``core.needfix_list`` vs today's algorithm on ``root``."""
    os.environ["AIWORKHUB_REPO_ROOT"] = str(root)
    os.environ.pop("AIWORKHUB_REPO", None)
    needfix_ingest.list_active(root)  # settle the read-time reconcile writes
    listing, narrow_peak = _traced_peak(lambda: core.needfix_list(limit=_MEASURE_PAGE))
    legacy, legacy_peak = _traced_peak(lambda: _legacy_needfix_list(root, limit=_MEASURE_PAGE))
    return {
        "narrow_peak_bytes": narrow_peak,
        "legacy_peak_bytes": legacy_peak,
        "listing": listing,
        "legacy": legacy,
    }


def test_needfix_list_traced_peak_is_bounded_by_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AIWORKHUB_REPO", raising=False)
    seed_measurement_store(tmp_path)

    measured = measure_needfix_list_peaks(tmp_path)

    listing = measured["listing"]
    assert listing.derived is True
    assert len(listing) == _MEASURE_PAGE
    # Same page as today's algorithm, row for row.
    assert _canonical_json(list(listing)) == _canonical_json(measured["legacy"]["items"])
    assert measured["legacy_peak_bytes"] > _PEAK_BOUND_BYTES, measured["legacy_peak_bytes"]
    assert measured["narrow_peak_bytes"] < _PEAK_BOUND_BYTES, measured["narrow_peak_bytes"]
