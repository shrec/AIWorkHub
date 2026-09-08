"""NF632: core.review_queue must filter review status in SQLite and project
only the fields needed to build returned cards, instead of decoding every
row's ``card_json`` and filtering ``status == "review"`` in Python.

Covers:
  * The SQL predicate excludes non-review rows before any ``card_json`` is
    read, so an unrelated huge/malformed ``card_json`` corpus is never
    JSON-decoded when the review queue is empty.
  * Ordering (most recently updated first), the existing 500-row cap, and the
    ``finalize_failed`` substatus exclusion all keep working.
  * A malformed *selected* review row still degrades gracefully (fail closed)
    instead of crashing the whole queue.
  * The rendered card shape (``=== Codex Review Queue (N) ===`` header, two
    space indented ``[topic] [runner] task_id`` rows) is unchanged, since
    ``dashboard.py``/``completion_inbox.py`` regex-parse it.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import core, task_store  # noqa: E402


NOW = "2026-07-20T00:00:00+00:00"


def _init_repo(tmp_path: Path, name: str = "repo_a") -> Path:
    root = tmp_path / name
    root.mkdir()
    result = task_store.initialize_repository(root)
    assert result["ok"], result
    return root


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = _init_repo(tmp_path)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.delenv("AIWORKHUB_ALLOW_WRITES", raising=False)
    yield root


def _insert_task(
    root: Path,
    task_id: str,
    runner: str,
    topic: str,
    *,
    worker_status: str = "unclaimed",
    status: str = "pending",
    updated_at: str = NOW,
    card_extra: dict | None = None,
) -> None:
    """Insert one row with a well-formed ``card_json`` (like a real card)."""
    _insert_task_raw(
        root,
        task_id,
        runner,
        topic,
        worker_status=worker_status,
        status=status,
        updated_at=updated_at,
        raw_card_json=json.dumps(card_extra or {}, ensure_ascii=False),
    )


def _insert_task_raw(
    root: Path,
    task_id: str,
    runner: str,
    topic: str,
    *,
    worker_status: str,
    status: str,
    updated_at: str,
    raw_card_json: str,
) -> None:
    """Insert one row with an exact, possibly-malformed ``card_json`` string."""
    readiness = task_store.storage_readiness(root)
    assert readiness.ready, readiness.reason
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, mode, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, runner, topic, "solo", status, worker_status, "normal",
                "objective", raw_card_json, NOW, updated_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_review_queue_projects_only_review_rows_in_order(repo):
    _insert_task(repo, "TASK_PENDING", "r", "coding", status="pending")
    _insert_task(repo, "TASK_DONE", "r", "coding", status="finished", worker_status="done")
    _insert_task(repo, "TASK_OLD_REVIEW", "r", "coding", status="review",
                 worker_status="review", updated_at="2026-07-19T00:00:00+00:00")
    _insert_task(repo, "TASK_NEW_REVIEW", "r", "coding", status="review",
                 worker_status="review", updated_at="2026-07-21T00:00:00+00:00")

    result = core.review_queue()
    assert result["ok"] is True
    assert "=== Codex Review Queue (2) ===" in result["stdout"]
    lines = [ln for ln in result["stdout"].splitlines() if ln.startswith("  [")]
    assert lines == [
        "  [coding] [r] TASK_NEW_REVIEW",
        "  [coding] [r] TASK_OLD_REVIEW",
    ]


def test_review_queue_excludes_finalize_failed_substatus(repo):
    _insert_task(
        repo, "TASK_FINALIZE_FAILED", "r", "coding",
        status="review", worker_status="review",
        card_extra={"terminal_substatus": "finalize_failed"},
    )
    _insert_task(
        repo, "TASK_REVIEW_OK", "r", "coding",
        status="review", worker_status="review",
    )

    result = core.review_queue()
    assert result["ok"] is True
    assert "=== Codex Review Queue (1) ===" in result["stdout"]
    assert "TASK_FINALIZE_FAILED" not in result["stdout"]
    assert "TASK_REVIEW_OK" in result["stdout"]


def test_review_queue_caps_at_500_rows(repo):
    for i in range(510):
        _insert_task(
            repo, f"TASK_REVIEW_{i:04d}", "r", "coding",
            status="review", worker_status="review",
            updated_at=f"2026-07-20T00:{i % 60:02d}:{i % 60:02d}+00:00",
        )

    result = core.review_queue()
    assert result["ok"] is True
    assert "=== Codex Review Queue (500) ===" in result["stdout"]


def test_review_queue_selected_malformed_row_fails_closed(repo):
    """A review-status row with unparsable ``card_json`` must not crash the
    whole queue -- it degrades to SQL-column data instead of exploding."""
    _insert_task_raw(
        repo, "TASK_MALFORMED_REVIEW", "r", "coding",
        status="review", worker_status="review",
        updated_at=NOW, raw_card_json="{not valid json",
    )

    result = core.review_queue()
    assert result["ok"] is True
    assert "=== Codex Review Queue (1) ===" in result["stdout"]
    assert "TASK_MALFORMED_REVIEW" in result["stdout"]


def test_review_queue_empty_result_never_decodes_unrelated_card_json(repo, monkeypatch):
    """Pin NF632: with zero review-status rows, an unrelated corpus of
    huge/malformed non-review ``card_json`` must never reach ``json.loads``.
    """
    poison_marker = "__UNRELATED_CARD_JSON_MUST_NOT_BE_DECODED__"
    huge_payload = json.dumps({"marker": poison_marker, "padding": "x" * 4096})

    statuses = [
        ("finished", "done"),
        ("blocked", "blocked_workspace"),
        ("pending", "unclaimed"),
        ("processing", "claimed"),
        ("superseded", "superseded"),
    ]
    for i in range(300):
        status, worker_status = statuses[i % len(statuses)]
        _insert_task_raw(
            repo, f"TASK_UNRELATED_{i:04d}", "r", "coding",
            status=status, worker_status=worker_status,
            updated_at=NOW, raw_card_json=huge_payload,
        )
    for i in range(50):
        _insert_task_raw(
            repo, f"TASK_UNRELATED_MALFORMED_{i:04d}", "r", "coding",
            status="finished", worker_status="done",
            updated_at=NOW, raw_card_json=f"{{not valid json {poison_marker}",
        )

    original_loads = task_store.json.loads

    def _guarded_loads(s, *args, **kwargs):
        if isinstance(s, str) and poison_marker in s:
            raise AssertionError("unrelated non-review card_json was decoded")
        return original_loads(s, *args, **kwargs)

    monkeypatch.setattr(task_store.json, "loads", _guarded_loads)

    result = core.review_queue()
    assert result["ok"] is True
    assert "=== Codex Review Queue (0) ===" in result["stdout"]


def test_list_review_queue_cards_bounded_indexed_projection(repo):
    """The task-store primitive itself: SQL-filtered, decoded review cards."""
    _insert_task(repo, "TASK_A", "r", "coding", status="review", worker_status="review")
    _insert_task(repo, "TASK_B", "r", "coding", status="finished", worker_status="done")

    cards = task_store.list_review_queue_cards(repo, limit=500)
    assert [c["task_id"] for c in cards] == ["TASK_A"]
    assert cards[0]["status"] == "review"


def test_list_review_queue_cards_production_query_uses_indexed_plan_and_review_set(repo):
    """NF632 rework: exercise the *exact* production SQL/predicate from
    ``task_store._REVIEW_QUEUE_SQL`` -- including its ``lower()`` wrapping,
    exclusion clauses and ``COALESCE(archived_at, '')`` archived filter --
    rather than an ad hoc raw-column query. Proves both that SQLite plans a
    MULTI-INDEX OR over the expression indexes instead of a table scan, and
    that the same query text still returns exactly the canonical review set.
    """
    _insert_task(repo, "TASK_PLAN_REVIEW", "r", "coding", status="review", worker_status="review")
    _insert_task(repo, "TASK_PLAN_DONE", "r", "coding", status="finished", worker_status="done")
    _insert_task(
        repo, "TASK_PLAN_MIXED_CASE", "r", "coding",
        status="Review", worker_status="REVIEW",
    )

    readiness = task_store.storage_readiness(repo)
    assert readiness.ready, readiness.reason
    placeholders = ",".join("?" for _ in task_store._REVIEW_STATUS_VALUES)
    query = task_store._REVIEW_QUEUE_SQL.format(placeholders=placeholders)
    bind = (*task_store._REVIEW_STATUS_VALUES, *task_store._REVIEW_STATUS_VALUES, 500)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        plan = conn.execute(f"EXPLAIN QUERY PLAN {query}", bind).fetchall()
        rows = conn.execute(query, bind).fetchall()
    finally:
        conn.close()
    plan_text = " ".join(str(row[-1]) for row in plan)
    assert "SCAN tasks" not in plan_text
    assert "idx_task_store_tasks_status_lower" in plan_text
    assert "idx_task_store_tasks_worker_status_lower" in plan_text

    row_task_ids = {row[0] for row in rows}
    assert row_task_ids == {"TASK_PLAN_REVIEW", "TASK_PLAN_MIXED_CASE"}

    cards = task_store.list_review_queue_cards(repo, limit=500)
    assert {c["task_id"] for c in cards} == row_task_ids


def test_list_review_queue_cards_active_row_with_null_archived_at(repo):
    """NF632 rework: a legacy row whose ``archived_at`` predates this store's
    ``NOT NULL DEFAULT ''`` column constraint -- and is therefore a genuine
    SQL ``NULL`` rather than ``''`` -- must still surface as an active review
    row. A bare ``archived_at = ''`` predicate is NULL-unsafe (``NULL = ''``
    is NULL, not true in SQL) and would silently drop it; the production
    query uses ``COALESCE(archived_at, '') = ''`` to match
    ``canonical_status``'s ``str(row.get("archived_at") or "").strip()``
    Python-side treatment of ``None`` as not archived."""
    readiness = task_store.storage_readiness(repo)
    assert readiness.ready, readiness.reason
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.executescript(
            "ALTER TABLE tasks RENAME TO tasks_legacy_test_only;"
            "CREATE TABLE tasks ("
            "  task_id TEXT PRIMARY KEY,"
            "  runner TEXT NOT NULL DEFAULT '',"
            "  topic TEXT NOT NULL DEFAULT '',"
            "  mode TEXT NOT NULL DEFAULT '',"
            "  status TEXT NOT NULL DEFAULT 'pending',"
            "  worker_status TEXT NOT NULL DEFAULT 'unclaimed',"
            "  priority TEXT NOT NULL DEFAULT '',"
            "  objective TEXT NOT NULL DEFAULT '',"
            "  card_json TEXT NOT NULL DEFAULT '{}',"
            "  created_at TEXT NOT NULL,"
            "  updated_at TEXT NOT NULL,"
            "  claimed_by TEXT,"
            "  claimed_at TEXT,"
            "  started_at TEXT,"
            "  completed_at TEXT,"
            "  origin_thread_id TEXT,"
            "  archived_at TEXT"
            ");"
            "INSERT INTO tasks SELECT * FROM tasks_legacy_test_only;"
            "DROP TABLE tasks_legacy_test_only;"
        )
        task_store.ensure_task_status_index(conn)
        task_store.ensure_task_worker_status_index(conn)
        conn.commit()
    finally:
        conn.close()

    # ``archived_at`` is omitted from the insert column list, so this legacy,
    # default-less ``tasks`` table leaves it as a genuine SQL NULL.
    _insert_task(repo, "TASK_NULL_ARCHIVED_REVIEW", "r", "coding",
                 status="review", worker_status="review")

    cards = task_store.list_review_queue_cards(repo, limit=500)
    assert [c["task_id"] for c in cards] == ["TASK_NULL_ARCHIVED_REVIEW"]
    assert cards[0]["status"] == "review"
    assert cards[0]["archived_at"] is None

    result = core.review_queue()
    assert result["ok"] is True
    assert "TASK_NULL_ARCHIVED_REVIEW" in result["stdout"]


def test_list_review_queue_cards_active_row_with_whitespace_only_archived_at(repo):
    """NF632 rework: ``canonical_status`` treats a whitespace-only
    ``archived_at`` (e.g. ``' '``) as not archived, because it computes
    ``str(row.get("archived_at") or "").strip()`` -- a whitespace-only string
    is truthy so ``or ""`` never substitutes, but ``.strip()`` still empties
    it. A bare ``COALESCE(archived_at, '') = ''`` predicate is not
    whitespace-equivalent to that: ``' ' != ''`` so it would silently exclude
    this row before the Python fail-closed cross-check ever sees it. The
    production query's ``trim(...)`` guard must keep this row in the queue."""
    _insert_task(repo, "TASK_WHITESPACE_ARCHIVED_REVIEW", "r", "coding",
                 status="review", worker_status="review")
    readiness = task_store.storage_readiness(repo)
    assert readiness.ready, readiness.reason
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "UPDATE tasks SET archived_at = ' ' WHERE task_id = ?",
            ("TASK_WHITESPACE_ARCHIVED_REVIEW",),
        )
        conn.commit()
    finally:
        conn.close()

    cards = task_store.list_review_queue_cards(repo, limit=500)
    assert [c["task_id"] for c in cards] == ["TASK_WHITESPACE_ARCHIVED_REVIEW"]
    assert cards[0]["status"] == "review"

    result = core.review_queue()
    assert result["ok"] is True
    assert "TASK_WHITESPACE_ARCHIVED_REVIEW" in result["stdout"]


def test_list_review_queue_cards_excludes_precedence_override_without_crowding_limit(repo):
    """A row whose raw ``status`` matches a review alias must still be
    excluded when a higher-precedence signal (``worker_status='done'``) makes
    its canonical status something else -- the SQL predicate now excludes
    that higher-precedence override itself, so ``ORDER BY``/``LIMIT`` run
    in SQL and a false-positive row can never crowd a genuine review row out
    of a small ``limit`` even though it sorts first by ``updated_at``."""
    _insert_task(
        repo, "TASK_FALSE_POSITIVE", "r", "coding",
        status="review", worker_status="done",
        updated_at="2026-07-22T00:00:00+00:00",
    )
    _insert_task(
        repo, "TASK_REAL_REVIEW", "r", "coding",
        status="processing", worker_status="review",
        updated_at="2026-07-20T00:00:00+00:00",
    )

    cards = task_store.list_review_queue_cards(repo, limit=1)
    assert [c["task_id"] for c in cards] == ["TASK_REAL_REVIEW"]


def test_list_review_queue_cards_large_false_positive_corpus_stays_within_bound(repo):
    """NF632 rework: a large corpus of precedence-override false positives
    (raw ``status='review'`` but canonically ``finished``/``blocked``) that
    outnumbers and outsorts the genuine review rows must never reach or
    crowd the bounded response -- proving the exact precedence exclusion and
    the ``LIMIT`` both run inside SQL rather than a Python post-filter over
    an unbounded fetch."""
    false_positive_specs = [
        ("review", "done"),
        ("finished", "review"),
        ("blocked", "review"),
        ("review", "blocked_workspace"),
        ("review", "deferred_retry"),
    ]
    for i in range(600):
        status, worker_status = false_positive_specs[i % len(false_positive_specs)]
        _insert_task(
            repo, f"TASK_FALSE_POSITIVE_{i:04d}", "r", "coding",
            status=status, worker_status=worker_status,
            updated_at=f"2026-08-01T00:{i % 60:02d}:{i % 60:02d}+00:00",
        )

    genuine_ids = [f"TASK_REAL_REVIEW_{i}" for i in range(5)]
    for i, task_id in enumerate(genuine_ids):
        _insert_task(
            repo, task_id, "r", "coding",
            status="review", worker_status="review",
            updated_at=f"2026-07-01T00:00:{i:02d}+00:00",
        )

    cards = task_store.list_review_queue_cards(repo, limit=5)
    assert len(cards) == 5
    assert {c["task_id"] for c in cards} == set(genuine_ids)
    assert all(c["status"] == "review" for c in cards)


def test_review_queue_topic_falls_back_to_card_json_topic(repo):
    """Parity regression: the legacy ``core.review_queue``/``_decode_task_card``
    path read a task's display topic from ``card_json`` whenever the SQL
    ``topic`` column was empty -- the SQL projection must preserve that exact
    fallback instead of regressing the display label to ``?``."""
    _insert_task(
        repo, "TASK_TOPIC_FALLBACK", "r", "",
        status="review", worker_status="review",
        card_extra={"topic": "fallback-topic"},
    )

    result = core.review_queue()
    assert result["ok"] is True
    assert "  [fallback-topic] [r] TASK_TOPIC_FALLBACK" in result["stdout"].splitlines()

    cards = task_store.list_review_queue_cards(repo, limit=500)
    assert cards[0]["topic"] == "fallback-topic"


def test_list_review_queue_cards_matches_mixed_case_persisted_status(repo):
    """Parity regression: ``canonical_status`` lowercases ``status``/
    ``worker_status`` before comparing, so a mixed-case persisted alias must
    still be treated as a review row by the SQL prefilter, not silently
    excluded before the Python cross-check ever sees it."""
    _insert_task(
        repo, "TASK_MIXED_CASE_REVIEW", "r", "coding",
        status="Review", worker_status="REVIEW",
    )

    cards = task_store.list_review_queue_cards(repo, limit=500)
    assert [c["task_id"] for c in cards] == ["TASK_MIXED_CASE_REVIEW"]
    assert cards[0]["status"] == "review"

    result = core.review_queue()
    assert result["ok"] is True
    assert "TASK_MIXED_CASE_REVIEW" in result["stdout"]


@pytest.mark.parametrize(
    ("terminal_review_value", "expected"),
    [
        (1, 1),
        (1.5, 1.5),
        (True, True),
        (False, False),
        ("hello", "hello"),
        ({"substatus": "finalize_ok"}, {"substatus": "finalize_ok"}),
        ([1, 2], [1, 2]),
        (None, None),
    ],
)
def test_list_review_queue_cards_terminal_review_scalar_and_container_shapes(
    repo, terminal_review_value, expected
):
    """NF632 rework: SQLite's ``json_extract`` returns JSON scalars (number,
    boolean, string) as native INTEGER/REAL/TEXT rather than JSON-encoded
    text, so passing that raw value straight to ``json.loads`` used to raise
    ``TypeError`` for numbers/booleans and kill the whole query. The
    production SQL must re-encode every shape -- scalar or container -- into
    valid JSON text first, so a single malformed-shaped selected row can
    never suppress the rest of the (otherwise empty) review queue."""
    _insert_task(
        repo, "TASK_TERMINAL_REVIEW_SHAPE", "r", "coding",
        status="review", worker_status="review",
        card_extra={"terminal_review": terminal_review_value},
    )

    cards = task_store.list_review_queue_cards(repo, limit=500)
    assert [c["task_id"] for c in cards] == ["TASK_TERMINAL_REVIEW_SHAPE"]
    assert cards[0]["terminal_review"] == expected

    result = core.review_queue()
    assert result["ok"] is True
    assert "=== Codex Review Queue (1) ===" in result["stdout"]
    assert "TASK_TERMINAL_REVIEW_SHAPE" in result["stdout"]


def test_review_queue_terminal_review_scalar_shapes_do_not_suppress_other_rows(repo):
    """A selected review row whose ``terminal_review`` is a bare JSON number
    or boolean must not raise and kill ``core.review_queue`` for every other
    genuine review row alongside it."""
    _insert_task(
        repo, "TASK_TERMINAL_REVIEW_NUMBER", "r", "coding",
        status="review", worker_status="review",
        card_extra={"terminal_review": 1},
    )
    _insert_task(
        repo, "TASK_TERMINAL_REVIEW_BOOL", "r", "coding",
        status="review", worker_status="review",
        card_extra={"terminal_review": True},
    )
    _insert_task(
        repo, "TASK_TERMINAL_REVIEW_STRING", "r", "coding",
        status="review", worker_status="review",
        card_extra={"terminal_review": "not-a-substatus"},
    )
    _insert_task(
        repo, "TASK_REVIEW_OK", "r", "coding",
        status="review", worker_status="review",
    )

    result = core.review_queue()
    assert result["ok"] is True
    assert "=== Codex Review Queue (4) ===" in result["stdout"]
    for task_id in (
        "TASK_TERMINAL_REVIEW_NUMBER",
        "TASK_TERMINAL_REVIEW_BOOL",
        "TASK_TERMINAL_REVIEW_STRING",
        "TASK_REVIEW_OK",
    ):
        assert task_id in result["stdout"]


def test_list_review_queue_cards_excludes_mixed_case_precedence_override(repo):
    """A mixed-case ``Review``/``DONE`` combination is canonically finished
    (the ``worker_status == 'done'`` branch outranks ``review``), so the SQL
    exclusion must be case-insensitive too, not just the candidate
    predicate."""
    _insert_task(
        repo, "TASK_MIXED_CASE_FINISHED", "r", "coding",
        status="Review", worker_status="DONE",
    )

    cards = task_store.list_review_queue_cards(repo, limit=500)
    assert cards == []
