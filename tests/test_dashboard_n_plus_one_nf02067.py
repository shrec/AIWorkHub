"""NF-2026-02067: the full dashboard snapshot does no N+1 task reads.

Two branches dominated a warm ``snapshot_view(full=True)``:

* the NeedFix join point-looked-up one linked card per row (plus an
  O(cards) successor walk per miss); it now bulk-loads every candidate id
  through one ``task_store.get_tasks`` read, and
* the cost ledger was rebuilt from every usage event on every poll; it is now
  memoized per process, keyed by the usage high-water mark plus the task-store
  fingerprint, and folds only newly appended usage events.

Each optimisation is pinned against the unoptimised path on the same fixture.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import cost_ledger  # noqa: E402
from aiworkhub import needfix_ingest  # noqa: E402
from aiworkhub import needfix_store  # noqa: E402
from aiworkhub import task_store  # noqa: E402

_FROZEN_NOW = "2026-10-01T00:00:00+00:00"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _store_conn(root: Path) -> sqlite3.Connection:
    _readiness, db_path = task_store._require_ready(root)
    return sqlite3.connect(db_path)


def _insert_task(root: Path, task_id: str, status: str, topic: str = "") -> None:
    conn = _store_conn(root)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, topic, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, topic, status, _FROZEN_NOW, _FROZEN_NOW),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_events(root: Path, events: list[tuple[str, str, dict[str, Any]]]) -> None:
    conn = _store_conn(root)
    try:
        for index, (task_id, event, payload) in enumerate(events):
            conn.execute(
                "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
                "VALUES (?, ?, 'codex_worker', ?, ?)",
                (task_id, event, json.dumps(payload), f"2026-10-01T00:{index % 60:02d}:00+00:00"),
            )
        conn.commit()
    finally:
        conn.close()


def _usage(task_id: str, request: str, *, topic: str = "task_mcp", tokens: int = 120) -> tuple[str, str, dict[str, Any]]:
    payload: dict[str, Any] = {
        "runner": "codex_worker",
        "model": "model-a",
        "provider": "codex",
        "role": "worker",
        "input_tokens": tokens - 20,
        "output_tokens": 20,
        "total_tokens": tokens,
        "cost_usd": 0.01 * tokens,
        "cost_observed": True,
        "usage_observed": True,
        "source": "task_mcp_launcher",
        "note": f"task_mcp_request:{request}",
    }
    if topic:
        payload["topic"] = topic
    return task_id, "usage_record", payload


# --------------------------------------------------------------------------
# NeedFix join: batched read == per-row read
# --------------------------------------------------------------------------


def _link(root: Path, title: str, task_id: str) -> str:
    rec = needfix_store.capture_proposal(root, title=title, description=title)
    conn = sqlite3.connect(str(needfix_store._db_path(root)))
    try:
        conn.execute(
            "UPDATE needfix SET converted_task_id = ?, status = 'task_created' WHERE id = ?",
            (task_id, rec["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return str(rec["id"])


def _accepted(root: Path, title: str) -> dict[str, Any]:
    rec = needfix_store.capture_proposal(root, title=title, description=title)
    needfix_store.triage_needfix(root, rec["id"])
    return needfix_store.accept_needfix(root, rec["id"])


def _seed_needfix_repo(root: Path, *, linked: int = 12) -> None:
    task_store.initialize_repository(root)
    statuses = ("finished", "pending", "claimed", "accepted", "rework")
    for index in range(linked):
        task_id = f"T-{index:03d}"
        if index % 7 != 6:  # every seventh linked card is gone from the store
            _insert_task(root, task_id, statuses[index % len(statuses)])
        _link(root, f"linked finding {index:03d}", task_id)
    # Unlinked accepted records: half bind through their deterministic id.
    for index in range(4):
        rec = _accepted(root, f"unlinked finding {index}")
        if index % 2 == 0:
            deterministic = needfix_store._deterministic_task_id_for(rec)
            assert deterministic
            _insert_task(root, deterministic, "finished")


@pytest.fixture
def frozen_needfix_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(needfix_store, "_utcnow_iso", lambda: _FROZEN_NOW)


def _count_store_reads(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"get_task": 0, "get_tasks": 0}
    real_get, real_get_many = task_store.get_task, task_store.get_tasks

    def counted_get(*args: Any, **kwargs: Any) -> Any:
        calls["get_task"] += 1
        return real_get(*args, **kwargs)

    def counted_get_many(*args: Any, **kwargs: Any) -> Any:
        calls["get_tasks"] += 1
        return real_get_many(*args, **kwargs)

    monkeypatch.setattr(task_store, "get_task", counted_get)
    monkeypatch.setattr(task_store, "get_tasks", counted_get_many)
    return calls


def _partial_reports(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    listed = needfix_ingest.list_active(
        root,
        task_cards_snapshot=(),
        task_cards_snapshot_complete=False,
        include_archived=True,
        limit=needfix_store.MAX_LIST_LIMIT,
    )
    counted = needfix_ingest.count_active(
        root,
        task_cards_snapshot=(),
        task_cards_snapshot_complete=False,
        include_archived=True,
    )
    return listed, counted


def test_batched_needfix_join_matches_the_per_row_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frozen_needfix_clock: None
) -> None:
    batched_root = tmp_path / "batched"
    per_row_root = tmp_path / "per_row"
    batched_root.mkdir()
    per_row_root.mkdir()
    _seed_needfix_repo(batched_root)
    _seed_needfix_repo(per_row_root)

    with monkeypatch.context() as patch:
        per_row_calls = _count_store_reads(patch)
        # No candidate ids -> nothing is bulk-loaded: the pre-NF-02067 join.
        patch.setattr(needfix_store, "task_link_candidate_ids", lambda _root: [])
        per_row = _partial_reports(per_row_root)

    with monkeypatch.context() as patch:
        batched_calls = _count_store_reads(patch)
        batched = _partial_reports(batched_root)

    assert _canonical_json(batched) == _canonical_json(per_row)
    listed, counted = batched
    assert listed["derived"] is True and counted["derived"] is True
    assert listed["count"] == counted["count"] == len(listed["items"])
    states = {item["derived_state"] for item in listed["items"]}
    assert "active" in states  # the missing cards and the unbound record
    # The per-row join point-looks-up every linked card on every read ...
    assert per_row_calls["get_tasks"] == 0
    assert per_row_calls["get_task"] >= 12
    # ... the batched join pays one point lookup (the first miss) and one bulk
    # read per successor round, for each of the two surfaces.
    assert batched_calls["get_task"] <= 2
    assert 1 <= batched_calls["get_tasks"] <= 4


def test_candidate_ids_cover_linked_and_deterministic_ids(
    tmp_path: Path, frozen_needfix_clock: None
) -> None:
    _seed_needfix_repo(tmp_path, linked=3)

    ids = needfix_store.task_link_candidate_ids(tmp_path)

    assert ids[:3] == ["T-000", "T-001", "T-002"]
    assert len(ids) == len(set(ids)) == 3 + 4
    assert all(task_id.startswith("needfix-") for task_id in ids[3:])


def test_a_failing_bulk_read_keeps_the_point_lookup_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frozen_needfix_clock: None
) -> None:
    reference_root = tmp_path / "reference"
    failing_root = tmp_path / "failing"
    reference_root.mkdir()
    failing_root.mkdir()
    _seed_needfix_repo(reference_root)
    _seed_needfix_repo(failing_root)
    reference = _partial_reports(reference_root)

    def broken_bulk(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(task_store, "get_tasks", broken_bulk)
    assert _canonical_json(_partial_reports(failing_root)) == _canonical_json(reference)


# --------------------------------------------------------------------------
# Cost ledger: memoized, incrementally folded, equal to a full rebuild
# --------------------------------------------------------------------------


@pytest.fixture
def ledger_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_home_missing"))
    cost_ledger.reset_cost_ledger_cache()
    root = tmp_path / "repo"
    root.mkdir()
    task_store.initialize_repository(root)
    _insert_task(root, "T-A", "finished", topic="alpha")
    _insert_task(root, "T-B", "pending", topic="beta")
    _insert_task(root, "T-NEW", "pending", topic="gamma")
    _insert_events(
        root,
        [
            _usage("T-A", "req-a1"),
            ("T-A", "terminal_failure", {"request_id": "req-a1", "substatus": "worker_failed"}),
            _usage("T-A", "req-a2", tokens=300),
            ("T-A", "accept_review", {"request_id": "req-a2"}),
            _usage("T-B", "req-b1", topic=""),
            # Evidence for a usage event that is appended only later: the
            # fold must attribute its cause exactly like a full rebuild.
            ("T-NEW", "claim_start", {"request_id": "req-new-1"}),
            ("T-NEW", "terminal_failure", {"request_id": "req-new-1", "substatus": "worker_failed"}),
        ],
    )
    yield root
    cost_ledger.reset_cost_ledger_cache()


def _ledger(root: Path) -> dict[str, Any]:
    return cost_ledger.build_cost_ledger(repo_root=root, include_tasks=True)


def test_incremental_ledger_equals_a_full_rebuild(ledger_repo: Path) -> None:
    before = _ledger(ledger_repo)
    assert cost_ledger.LEDGER_CACHE_STATS["full_builds"] == 1

    _insert_events(
        ledger_repo,
        [
            _usage("T-NEW", "req-new-1", topic=""),
            _usage("T-NEW", "req-new-2", tokens=500),
            _usage("T-B", "req-b2"),
        ],
    )
    folded = _ledger(ledger_repo)
    assert cost_ledger.LEDGER_CACHE_STATS["incremental_folds"] == 1
    assert cost_ledger.LEDGER_CACHE_STATS["full_builds"] == 1

    cost_ledger.reset_cost_ledger_cache()
    rebuilt = _ledger(ledger_repo)
    assert cost_ledger.LEDGER_CACHE_STATS["full_builds"] == 1

    assert _canonical_json(folded) == _canonical_json(rebuilt)
    assert folded["tasks"] == rebuilt["tasks"]
    assert folded["counts"]["usage_rows"] == before["counts"]["usage_rows"] + 3
    totals = folded["aggregates"]["by_runner"]["codex_worker"]
    assert totals["total_tokens"] == sum(row["total_tokens"] for row in rebuilt["tasks"])
    new_row = next(row for row in folded["tasks"] if row["attempt_id"] == "req-new-1")
    assert new_row["topic"] == "gamma" and new_row["topic_source"] == "task_join"
    assert new_row["terminal_cause_source"] == "task_store"


def test_unchanged_store_is_answered_from_the_memo(ledger_repo: Path) -> None:
    first = _ledger(ledger_repo)
    first["tasks"].clear()
    first["aggregates"]["by_runner"].clear()

    second = _ledger(ledger_repo)

    assert cost_ledger.LEDGER_CACHE_STATS["hits"] == 1
    assert cost_ledger.LEDGER_CACHE_STATS["full_builds"] == 1
    # A caller mutating its copy never corrupts the memo.
    assert second["tasks"] and second["aggregates"]["by_runner"]
    cost_ledger.reset_cost_ledger_cache()
    assert _canonical_json(second) == _canonical_json(_ledger(ledger_repo))


def test_ledger_is_invalidated_by_store_fingerprint_and_usage_appends(
    ledger_repo: Path,
) -> None:
    baseline = _ledger(ledger_repo)
    assert baseline["model_outcomes"] == _ledger(ledger_repo)["model_outcomes"]
    assert cost_ledger.LEDGER_CACHE_STATS["hits"] == 1

    # A non-usage event (a manager decision) changes the store fingerprint:
    # the memo must rebuild, never fold, and must reflect the decision.
    _insert_events(ledger_repo, [("T-B", "reject_review", {"request_id": "req-b1"})])
    decided = _ledger(ledger_repo)
    assert cost_ledger.LEDGER_CACHE_STATS["full_builds"] == 2
    assert cost_ledger.LEDGER_CACHE_STATS["incremental_folds"] == 0
    assert decided["model_outcomes"] != baseline["model_outcomes"]

    # A task-table change alone also changes the fingerprint.
    conn = _store_conn(ledger_repo)
    try:
        conn.execute(
            "UPDATE tasks SET topic='delta', updated_at='2026-10-02T00:00:00+00:00' "
            "WHERE task_id='T-B'"
        )
        conn.commit()
    finally:
        conn.close()
    retopiced = _ledger(ledger_repo)
    assert cost_ledger.LEDGER_CACHE_STATS["full_builds"] == 3
    b_row = next(row for row in retopiced["tasks"] if row["attempt_id"] == "req-b1")
    assert b_row["topic"] == "delta"

    # Appending usage events invalidates the cached ledger by folding them in.
    _insert_events(ledger_repo, [_usage("T-A", "req-a3", tokens=40)])
    appended = _ledger(ledger_repo)
    assert cost_ledger.LEDGER_CACHE_STATS["incremental_folds"] == 1
    assert appended["counts"]["usage_rows"] == retopiced["counts"]["usage_rows"] + 1

    # The process ledger is part of the fingerprint as well.
    ledger_path = ledger_repo / cost_ledger._PROCESS_EVENTS_PATH
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps({"task_id": "T-A", "request_id": "req-a3", "topic": "alpha"}) + "\n",
        encoding="utf-8",
    )
    _ledger(ledger_repo)
    assert cost_ledger.LEDGER_CACHE_STATS["full_builds"] == 4


def test_an_unready_store_is_built_uncached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_home_missing"))
    monkeypatch.setattr(task_store, "list_usage_events", lambda _root, limit=10_000: [])
    cost_ledger.reset_cost_ledger_cache()

    result = cost_ledger.build_cost_ledger(repo_root=tmp_path)

    assert result["counts"]["usage_rows"] == 0
    assert cost_ledger.LEDGER_CACHE_STATS["uncached_builds"] == 1
    assert cost_ledger.LEDGER_CACHE_STATS["hits"] == 0
    cost_ledger.reset_cost_ledger_cache()


# --------------------------------------------------------------------------
# Warm snapshot budget for the two branches
# --------------------------------------------------------------------------


def test_warm_cost_ledger_and_needfix_join_stay_under_one_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frozen_needfix_clock: None
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_home_missing"))
    cost_ledger.reset_cost_ledger_cache()
    root = tmp_path / "repo"
    root.mkdir()
    _seed_needfix_repo(root, linked=120)
    _insert_events(
        root,
        [_usage(f"T-{index % 120:03d}", f"req-{index}") for index in range(3000)],
    )

    def snapshot_branches() -> float:
        started = time.perf_counter()
        cost_ledger.build_cost_ledger(repo_root=root, include_tasks=True)
        _partial_reports(root)
        return time.perf_counter() - started

    snapshot_branches()  # cold: fills the ledger memo, runs the reconciles
    warm = snapshot_branches()

    assert cost_ledger.LEDGER_CACHE_STATS["hits"] == 1
    assert warm < 1.0, f"warm cost-ledger + NeedFix-join branches took {warm:.3f}s"
    cost_ledger.reset_cost_ledger_cache()
