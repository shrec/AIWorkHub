"""NF-2026-02065: the dashboard snapshot paid two scans on the request path.

Measured 2026-11-05 (cProfile, fresh process, ``snapshot_view(full=False)``,
~640 MB canonical store): 17.59 s wall, of which

* 5.25 s was one synchronous ``PRAGMA quick_check`` -- every 300 s the first
  readiness call after the TTL paid the full integrity scan;
* 7.89 s was ``get_work_card_decision_counts``, two statements whose plan was
  ``SCAN task_events`` once ``sqlite_stat1`` existed.

These tests pin both fixes: a stale ok verdict is served while exactly ONE
background re-scan runs, and the decision counts are byte-identical to the
previous SQL while the plan no longer scans the event table.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import dashboard, task_store  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache():
    task_store.reset_storage_readiness_cache()
    yield
    _join_refreshes()
    task_store.reset_storage_readiness_cache()


def _join_refreshes(timeout: float = 30.0) -> None:
    """Wait, under one bounded deadline, until no background quick_check is
    registered. A one-shot snapshot of the registry can miss a refresh that
    registers after it was taken, so the registry is re-read until empty."""
    deadline = time.monotonic() + timeout
    while True:
        with task_store._QUICK_CHECK_LOCK:
            threads = list(task_store._QUICK_CHECK_REFRESHING.values())
        if not threads:
            return
        assert time.monotonic() < deadline, "background quick_check did not finish"
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
            assert not thread.is_alive(), "background quick_check did not finish"


def _ready_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    readiness = task_store.storage_readiness(repo)
    assert readiness.ready, readiness.reason
    task_store.reset_storage_readiness_cache()
    return repo, Path(readiness.canonical_db)


def _age_cache(seconds: float) -> None:
    """Make every cached ok verdict look ``seconds`` past the TTL."""
    with task_store._QUICK_CHECK_LOCK:
        for key, (stored_at, verdict) in list(task_store._QUICK_CHECK_CACHE.items()):
            task_store._QUICK_CHECK_CACHE[key] = (
                stored_at - task_store._QUICK_CHECK_TTL_SECONDS - seconds,
                verdict,
            )
    with task_store._READINESS_MEMO_LOCK:
        task_store._READINESS_MEMO.clear()


class _GatedScan:
    """A ``quick_check`` stand-in that records which thread ran it and can
    hold a background scan open so concurrent callers overlap it."""

    def __init__(self, answers=None) -> None:
        self.calls: list[threading.Thread] = []
        self.release = threading.Event()
        self.started = threading.Event()
        self.gate = False
        self._answers = list(answers or [])
        self._real = task_store.quick_check
        self._lock = threading.Lock()

    def __call__(self, path: Path) -> str:
        with self._lock:
            self.calls.append(threading.current_thread())
            answer = self._answers.pop(0) if self._answers else None
        if self.gate and threading.current_thread() is not threading.main_thread():
            self.started.set()
            assert self.release.wait(30), "test never released the background scan"
        return self._real(path) if answer is None else answer


def test_stale_verdict_is_served_and_exactly_one_background_scan_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, db = _ready_repo(tmp_path)
    scan = _GatedScan()
    monkeypatch.setattr(task_store, "quick_check", scan)

    # First check for this identity in the process: synchronous.
    assert task_store.storage_readiness(repo).ready
    assert scan.calls == [threading.main_thread()]

    _age_cache(1.0)
    scan.gate = True
    results: list[bool] = []

    def poll() -> None:
        results.append(task_store.storage_readiness(repo).ready)

    pollers = [threading.Thread(target=poll) for _ in range(8)]
    for thread in pollers:
        thread.start()
    for _ in range(8):
        poll()
    for thread in pollers:
        thread.join(30)
    assert results == [True] * 16, "a stale ok verdict is served, never blocked on"
    assert scan.started.wait(30), "a stale verdict must schedule a background re-scan"
    # While the re-scan is in flight nobody else scanned: single-flight.
    assert len(scan.calls) == 2, [t.name for t in scan.calls]
    background = scan.calls[1]
    assert background.name == "aiworkhub-quick-check-refresh"
    assert background not in pollers and background is not threading.main_thread()
    with task_store._QUICK_CHECK_LOCK:
        assert list(task_store._QUICK_CHECK_REFRESHING.values()) == [background]

    scan.release.set()
    _join_refreshes()
    with task_store._QUICK_CHECK_LOCK:
        assert not task_store._QUICK_CHECK_REFRESHING
        ((stored_at, verdict),) = task_store._QUICK_CHECK_CACHE.values()
    assert verdict == "ok"
    assert task_store.time.monotonic() - stored_at <= task_store._QUICK_CHECK_TTL_SECONDS

    # The refreshed verdict is fresh again: no further scan.
    assert task_store.storage_readiness(repo).ready
    assert len(scan.calls) == 2


def test_identity_change_scans_synchronously(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, db = _ready_repo(tmp_path)
    assert task_store.storage_readiness(repo).ready
    scan = _GatedScan()
    monkeypatch.setattr(task_store, "quick_check", scan)
    _age_cache(1.0)

    copy = db.with_name(db.name + ".nf02065-copy")
    shutil.copyfile(db, copy)
    os.replace(copy, db)

    assert task_store.storage_readiness(repo).ready
    assert scan.calls == [threading.main_thread()], "a new identity has no ok verdict yet"
    with task_store._QUICK_CHECK_LOCK:
        assert not task_store._QUICK_CHECK_REFRESHING


def test_failed_background_scan_is_not_cached_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, db = _ready_repo(tmp_path)
    assert task_store.storage_readiness(repo).ready
    assert task_store.storage_readiness(repo).ready
    scan = _GatedScan(answers=["*** in database main ***", "*** in database main ***"])
    monkeypatch.setattr(task_store, "quick_check", scan)
    _age_cache(1.0)

    assert task_store.storage_readiness(repo).ready, "the stale ok verdict is served once"
    _join_refreshes()
    assert len(scan.calls) == 1 and scan.calls[0] is not threading.main_thread()
    with task_store._QUICK_CHECK_LOCK:
        assert not task_store._QUICK_CHECK_CACHE, "a non-ok verdict is never cached"
    assert not task_store._READINESS_MEMO

    readiness = task_store.storage_readiness(repo)
    assert not readiness.ready
    assert readiness.reason.startswith("quick_check_failed:")
    assert scan.calls[1] is threading.main_thread()
    # Repaired: seen at once, synchronously.
    assert task_store.storage_readiness(repo).ready
    assert scan.calls[2] is threading.main_thread()


def test_reset_clears_in_flight_rescan_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, db = _ready_repo(tmp_path)
    assert task_store.storage_readiness(repo).ready
    scan = _GatedScan()
    scan.gate = True
    monkeypatch.setattr(task_store, "quick_check", scan)
    _age_cache(1.0)

    assert task_store.storage_readiness(repo).ready
    assert scan.started.wait(30)
    with task_store._QUICK_CHECK_LOCK:
        (background,) = task_store._QUICK_CHECK_REFRESHING.values()

    task_store.reset_storage_readiness_cache()
    assert not task_store._QUICK_CHECK_CACHE
    assert not task_store._QUICK_CHECK_REFRESHING

    scan.release.set()
    background.join(30)
    assert not background.is_alive()
    assert not task_store._QUICK_CHECK_CACHE, "a scan in flight across reset is discarded"

    scan.gate = False
    assert task_store.storage_readiness(repo).ready
    assert scan.calls[-1] is threading.main_thread(), "after reset the next check is synchronous"


# ---------------------------------------------------------------------------
# get_work_card_decision_counts: same counts, no full event-table scan.
# ---------------------------------------------------------------------------

# The statements exactly as they were before NF-2026-02065.
_OLD_POPULATION_SQL = """
    CASE
        WHEN t.task_id IS NULL THEN 'unknown_topic'
        WHEN COALESCE(NULLIF(t.topic, ''), json_extract(t.card_json, '$.topic'), '') = ? THEN 'reviewer_child'
        ELSE 'work_card'
    END
"""
_OLD_DECISION_ROW_SQL = """
    event IN ('accept_review', 'reject_review')
    OR (
        event IN ('archived', 'superseded')
        AND json_extract(payload_json, '$.reason') LIKE 'reject_review:%'
    )
"""
_OLD_CARD_SQL = f"""
WITH decision_events AS (
    SELECT task_id, event_id, event
    FROM task_events
    WHERE {_OLD_DECISION_ROW_SQL}
    ORDER BY event_id DESC
    LIMIT ?
),
latest_decision AS (
    SELECT d.task_id, d.event
    FROM decision_events d
    JOIN (
        SELECT task_id, MAX(event_id) AS event_id
        FROM decision_events GROUP BY task_id
    ) newest ON newest.task_id = d.task_id AND newest.event_id = d.event_id
)
SELECT
    {_OLD_POPULATION_SQL} AS population,
    SUM(CASE WHEN l.event = 'accept_review' THEN 1 ELSE 0 END) AS accepted,
    SUM(CASE WHEN l.event <> 'accept_review' THEN 1 ELSE 0 END) AS rejected
FROM latest_decision l
LEFT JOIN tasks t ON t.task_id = l.task_id
GROUP BY population
"""
_OLD_EVENT_SQL = f"""
WITH decision_events AS (
    SELECT task_id, event_id, event
    FROM task_events
    WHERE {_OLD_DECISION_ROW_SQL}
    ORDER BY event_id DESC
    LIMIT ?
)
SELECT
    {_OLD_POPULATION_SQL} AS population,
    SUM(CASE WHEN e.event = 'accept_review' THEN 1 ELSE 0 END) AS accepted,
    SUM(CASE WHEN e.event <> 'accept_review' THEN 1 ELSE 0 END) AS rejected
FROM decision_events e
LEFT JOIN tasks t ON t.task_id = e.task_id
GROUP BY population
"""

_EVENT_KINDS = (
    ("accept_review", None),
    ("reject_review", None),
    ("archived", "reject_review:quality"),
    ("archived", "retention"),
    ("archived", None),
    ("superseded", "reject_review:displaced"),
    ("superseded", "superseded_by:X"),
    ("claim", None),
    ("progress", None),
    ("worker_done", None),
    ("archived", "Reject_review:case"),
)


def _seed(db: Path) -> None:
    import random

    rnd = random.Random(2065)
    now = "2026-11-05T00:00:00+00:00"
    conn = sqlite3.connect(db)
    try:
        for index in range(120):
            column_topic = rnd.choice(["quality_review", "task_mcp", "docs", ""])
            card_topic = rnd.choice(["quality_review", "task_mcp", None])
            card = {"task_id": f"T{index}"}
            if card_topic is not None:
                card["topic"] = card_topic
            conn.execute(
                "INSERT INTO tasks(task_id, topic, card_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"T{index}", column_topic, json.dumps(card), now, now),
            )
        for _ in range(3000):
            # Some events belong to cards whose ``tasks`` row is gone.
            task_id = f"T{rnd.randrange(140)}"
            event, reason = rnd.choice(_EVENT_KINDS)
            payload = {} if reason is None else {"reason": reason}
            conn.execute(
                "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
                "VALUES (?, ?, 'codex', ?, ?)",
                (task_id, event, json.dumps(payload), now),
            )
        conn.commit()
    finally:
        conn.close()


def _rows(conn: sqlite3.Connection, sql: str, limit: int) -> list[tuple]:
    return sorted(conn.execute(sql, (limit, dashboard.WORK_CARD_EXCLUDED_TOPICS[0])).fetchall())


def _install_large_store_statistics(db: Path) -> None:
    """Statistics shaped like the ~640 MB canonical store, where the old
    predicate's plan became ``SCAN task_events``."""
    conn = sqlite3.connect(db)
    try:
        conn.execute("ANALYZE")
        conn.execute(
            "UPDATE sqlite_stat1 SET stat='600000 75000 1' "
            "WHERE idx='idx_task_store_events_event_id'"
        )
        conn.execute(
            "UPDATE sqlite_stat1 SET stat='600000 29 8 1' "
            "WHERE idx='idx_task_store_events_task_event_id'"
        )
        conn.commit()
    finally:
        conn.close()


def test_decision_counts_match_the_previous_sql_and_avoid_a_full_scan(tmp_path: Path) -> None:
    repo, db = _ready_repo(tmp_path)
    _seed(db)
    _install_large_store_statistics(db)

    conn = sqlite3.connect(db)
    try:
        for limit in (dashboard.WORK_CARD_DECISION_EVENT_LIMIT, 1, 7, 250):
            assert _rows(conn, dashboard._WORK_CARD_DECISION_CARD_SQL, limit) == _rows(
                conn, _OLD_CARD_SQL, limit
            ), limit
            assert _rows(conn, dashboard._WORK_CARD_DECISION_EVENT_SQL, limit) == _rows(
                conn, _OLD_EVENT_SQL, limit
            ), limit
        old_card = _rows(conn, _OLD_CARD_SQL, dashboard.WORK_CARD_DECISION_EVENT_LIMIT)
        old_event = _rows(conn, _OLD_EVENT_SQL, dashboard.WORK_CARD_DECISION_EVENT_LIMIT)
        assert {row[0] for row in old_card} == {"work_card", "reviewer_child", "unknown_topic"}

        for sql in (dashboard._WORK_CARD_DECISION_CARD_SQL, dashboard._WORK_CARD_DECISION_EVENT_SQL):
            plan = [
                str(row[3])
                for row in conn.execute(
                    "EXPLAIN QUERY PLAN " + sql,
                    (dashboard.WORK_CARD_DECISION_EVENT_LIMIT, dashboard.WORK_CARD_EXCLUDED_TOPICS[0]),
                )
            ]
            scanned = {step.split()[1] for step in plan if step.startswith("SCAN ")}
            assert not scanned & {"task_events", "tasks", "t"}, plan
            assert any(
                "SEARCH task_events USING" in step and "idx_task_store_events_event_id" in step
                for step in plan
            ), plan
            # NF-2026-02140: the population is materialized once, and the GROUP BY
            # scans that CTE rather than re-evaluating card_json per decision row.
            assert any(step.startswith("MATERIALIZE classified") for step in plan), plan
            assert "classified" in scanned, plan
    finally:
        conn.close()

    counts = dashboard.DashboardProvider(repo_root=repo).get_work_card_decision_counts()
    names = {
        "work_card": "work_cards",
        "reviewer_child": "reviewer_children",
        "unknown_topic": "unknown_topic_cards",
    }
    for population, accepted, rejected in old_card:
        assert counts[names[population]] == {"accepted": accepted, "rejected": rejected}
    (work_events,) = [row for row in old_event if row[0] == "work_card"]
    assert counts["work_card_events"] == {"accepted": work_events[1], "rejected": work_events[2]}


def test_decision_counts_identical_with_large_card_json(tmp_path: Path) -> None:
    """NF-2026-02140: the materialized population must not change a count, even
    when every referenced card_json is large (the cause of the slow GROUP BY)."""
    _repo, db = _ready_repo(tmp_path)
    _seed(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE tasks SET card_json = json_set(card_json, '$.padding', ?)",
            ("x" * 200_000,),
        )
        conn.commit()
        assert conn.execute("SELECT MIN(length(card_json)) FROM tasks").fetchone()[0] > 200_000
        for limit in (dashboard.WORK_CARD_DECISION_EVENT_LIMIT, 1, 7, 250):
            assert _rows(conn, dashboard._WORK_CARD_DECISION_CARD_SQL, limit) == _rows(
                conn, _OLD_CARD_SQL, limit
            ), limit
            assert _rows(conn, dashboard._WORK_CARD_DECISION_EVENT_SQL, limit) == _rows(
                conn, _OLD_EVENT_SQL, limit
            ), limit
    finally:
        conn.close()
