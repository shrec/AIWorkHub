from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from types import SimpleNamespace

import pytest

from aiworkhub import needfix_store, sdlc_outcome_metrics, task_engine, task_store


def _receipt(task_id: str, request_id: str, digest: str):
    hashes = {"fixed.txt": digest}
    unsigned = {
        "schema_id": task_engine.ACCEPTED_OUTCOME_RECEIPT_SCHEMA,
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": 1,
        "base_oid": "base",
        "promoted_paths": ["fixed.txt"],
        "changed_path_hashes": hashes,
        "attempt_artifact_manifest_id": digest,
        "repository_revision": "sha256:" + task_engine._canonical_json_hash({
            "base_oid": "base", "changed_path_hashes": hashes
        }),
    }
    return {
        **unsigned,
        "receipt_id": "sha256:" + task_engine._canonical_json_hash(unsigned),
    }


def _accepted(event_id: int, task_id: str, request_id: str, digest: str):
    return {
        "event_id": event_id,
        "task_id": task_id,
        "event": "accept_review",
        "payload": {
            "request_id": request_id,
            "accepted_outcome_receipt": _receipt(task_id, request_id, digest),
        },
    }


def _cause(task_id: str, request_id: str, digest: str):
    return {
        "schema_id": needfix_store.CAUSED_BY_SCHEMA_ID,
        "repository_id": "repo-one",
        "task_id": task_id,
        "request_id": request_id,
        "accepted_outcome_receipt": _receipt(task_id, request_id, digest),
    }


def test_read_repository_metrics_reads_real_hash_paths_readonly(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    repo_root = tmp_path / "repo#frag"
    repo_root.mkdir()
    task_db = repo_root / "tasks#main.db"
    conn = sqlite3.connect(str(task_db))
    try:
        conn.execute(
            "CREATE TABLE task_events("
            "event_id INTEGER PRIMARY KEY, task_id TEXT, event TEXT, "
            "payload_json TEXT, created_at TEXT)"
        )
        event = _accepted(1, "T1", "R1", "a" * 64)
        conn.execute(
            "INSERT INTO task_events"
            "(event_id, task_id, event, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                1,
                "T1",
                "accept_review",
                json.dumps(event["payload"]),
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    needfix_db = repo_root.joinpath(*needfix_store.NEEDFIX_DB_REL)
    needfix_db.parent.mkdir(parents=True, exist_ok=True)
    nf_conn = sqlite3.connect(str(needfix_db))
    try:
        nf_conn.execute(
            "CREATE TABLE needfix("
            "id TEXT, caused_by_json TEXT, created_at TEXT)"
        )
        nf_conn.execute(
            "INSERT INTO needfix(id, caused_by_json, created_at) "
            "VALUES (?, ?, ?)",
            (
                "NF-1",
                json.dumps(_cause("T1", "R1", "a" * 64)),
                "2026-01-01T00:00:01Z",
            ),
        )
        nf_conn.commit()
    finally:
        nf_conn.close()
    monkeypatch.setattr(
        task_store,
        "storage_readiness",
        lambda root: SimpleNamespace(
            ready=True, reason="", canonical_db=task_db
        ),
    )
    result = sdlc_outcome_metrics.read_repository_metrics(
        repo_root, repository_id="repo-one"
    )
    assert result["first_pass_acceptance"]["numerator"] == 1
    assert result["first_pass_acceptance"]["denominator"] == 1
    assert result["escaped_defect_attribution"]["numerator"] == 1
    assert result["escaped_defect_attribution"]["unknown_unattributed"] == 0
    assert (
        result["population_bounds"]["canonical_events_after_deduplication"] == 1
    )
    # Filesystem truth: both '#' databases still exist and no fragment-stripped
    # sibling file a raw f-string URI open would create was materialised.
    assert task_db.exists()
    assert needfix_db.exists()
    assert not (tmp_path / "repo").exists()

def test_mixed_population_reports_coverage_and_unknown_without_guessing():
    events = [
        _accepted(1, "T1", "R1", "a" * 64),
        {"event_id": 2, "task_id": "T2", "event": "reject_review", "payload": {}},
        _accepted(3, "T2", "R2", "c" * 64),
        _accepted(3, "T2", "R2", "c" * 64),
    ]
    rows = [
        {"id": "NF-1", "caused_by": _cause("T1", "R1", "a" * 64)},
        {"id": "NF-2", "caused_by": None},
        {"id": "NF-3", "caused_by": _cause("T1", "stale", "a" * 64)},
    ]
    result = sdlc_outcome_metrics.aggregate(
        events, rows, repository_id="repo-one", limit=100
    )
    assert result["first_pass_acceptance"] == {
        "numerator": 1, "denominator": 2, "evidence_covered": 2, "evidence_total": 2
    }
    assert result["review_rounds_per_accepted_task"]["numerator"] == 3
    assert result["escaped_defect_attribution"] == {
        "numerator": 1,
        "denominator": 3,
        "evidence_covered": 1,
        "evidence_total": 3,
        "unknown_unattributed": 2,
        "outside_event_bound_unknown": 0,
        "task_event_population_complete": True,
    }
    assert result["population_bounds"]["canonical_events_after_deduplication"] == 3


def test_cross_repository_identity_is_unknown():
    events = [_accepted(1, "T1", "R1", "a" * 64)]
    cause = _cause("T1", "R1", "a" * 64)
    cause["repository_id"] = "other-repo"
    result = sdlc_outcome_metrics.aggregate(
        events, [{"caused_by": cause}], repository_id="repo-one"
    )
    assert result["escaped_defect_attribution"]["numerator"] == 0
    assert result["escaped_defect_attribution"]["unknown_unattributed"] == 1


def test_malformed_stored_identity_with_extra_fields_is_unknown():
    events = [_accepted(1, "T1", "R1", "a" * 64)]
    cause = {**_cause("T1", "R1", "a" * 64), "unverified": True}
    result = sdlc_outcome_metrics.aggregate(
        events, [{"caused_by": cause}], repository_id="repo-one"
    )
    assert result["escaped_defect_attribution"]["numerator"] == 0
    assert result["escaped_defect_attribution"]["unknown_unattributed"] == 1


def test_population_is_bounded_and_never_emits_a_percentage():
    result = sdlc_outcome_metrics.aggregate(
        [_accepted(i, f"T{i}", f"R{i}", "a" * 64) for i in range(5)],
        [],
        repository_id="repo-one",
        limit=2,
    )
    assert result["population_bounds"]["task_events_scanned"] == 2
    assert result["population_bounds"]["task_events_truncated"] is True
    assert "percentage" not in str(result).lower()


def test_truncated_event_population_does_not_claim_unattributed_certainty():
    cross_repository = _cause("old", "old-request", "b" * 64)
    cross_repository["repository_id"] = "other-repo"
    result = sdlc_outcome_metrics.aggregate(
        [
            _accepted(3, "new", "new-request", "a" * 64),
            _accepted(2, "newer", "newer-request", "c" * 64),
            _accepted(1, "old", "old-request", "b" * 64),
        ],
        [
            {"caused_by": _cause("old", "old-request", "b" * 64)},
            {"caused_by": cross_repository},
        ],
        repository_id="repo-one",
        limit=2,
    )
    attribution = result["escaped_defect_attribution"]
    assert result["first_pass_acceptance"]["evidence_covered"] == 0
    assert result["review_rounds_per_accepted_task"]["evidence_covered"] == 0
    assert attribution["numerator"] == 0
    assert attribution["evidence_covered"] == 0
    assert attribution["unknown_unattributed"] == 2
    assert attribution["outside_event_bound_unknown"] == 1
    assert attribution["task_event_population_complete"] is False


def test_identity_parses_real_task_engine_acceptance_event(monkeypatch, tmp_path):
    db_path = tmp_path / "task.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(task_store.SCHEMA)
    changed = tmp_path / "fixed.txt"
    changed.write_text("fixed", encoding="utf-8")
    changed_digest = hashlib.sha256(changed.read_bytes()).hexdigest()
    manifest = {"request_id": "request-real"}
    card = {
        "task_id": "TASK-REAL",
        "runner": "codex",
        "topic": "metrics",
        "claim_epoch": 1,
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "request_identity": {"request_id": "request-real"},
                "changed_paths": ["fixed.txt"],
                "changed_path_hashes": {"fixed.txt": changed_digest},
                "attempt_artifact_manifest": manifest,
                "workspace": {"base_oid": "base"},
            },
        },
    }
    conn.execute(
        "INSERT INTO tasks (task_id, runner, topic, status, worker_status, card_json, "
        "created_at, updated_at, claimed_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "TASK-REAL", "codex", "metrics", "review", "review",
            json.dumps(card), "now", "now", "codex",
        ),
    )
    conn.commit()
    conn.close()

    readiness = task_store.StorageReadiness(True, "ready", "repo-one", str(db_path))
    monkeypatch.setattr(task_store, "_require_ready", lambda repo: (readiness, db_path))
    monkeypatch.setattr(task_store, "storage_readiness", lambda repo: readiness)
    unsigned = {
        "schema_id": task_engine.ACCEPTED_OUTCOME_RECEIPT_SCHEMA,
        "task_id": "TASK-REAL",
        "request_id": "request-real",
        "claim_epoch": 1,
        "base_oid": "base",
        "promoted_paths": ["fixed.txt"],
        "changed_path_hashes": {"fixed.txt": changed_digest},
        "attempt_artifact_manifest_id": task_engine._canonical_json_hash(manifest),
        "repository_revision": "sha256:" + task_engine._canonical_json_hash({
            "base_oid": "base", "changed_path_hashes": {"fixed.txt": changed_digest}
        }),
    }
    receipt = {
        **unsigned,
        "receipt_id": "sha256:" + task_engine._canonical_json_hash(unsigned),
    }
    accepted = task_engine.accept_review(
        tmp_path,
        "TASK-REAL",
        runner="codex",
        topic="metrics",
        request_id="request-real",
        evidence={},
        accepted_outcome_receipt=receipt,
    )
    assert accepted["ok"] is True
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    event = dict(conn.execute(
        "SELECT event_id, task_id, event, payload_json, created_at FROM task_events "
        "WHERE event='accept_review'"
    ).fetchone())
    conn.close()
    assert sdlc_outcome_metrics.accepted_outcome_identity(event, "repo-one") == {
        "schema_id": needfix_store.CAUSED_BY_SCHEMA_ID,
        "repository_id": "repo-one",
        "task_id": "TASK-REAL",
        "request_id": "request-real",
        "accepted_outcome_receipt": receipt,
    }


def _acceptance(task_id: str, request_id: str, digest: str):
    return _accepted(0, task_id, request_id, digest)["payload"]


def _numbered(events):
    return [
        (event_id, task_id, event, payload)
        for event_id, (task_id, event, payload) in enumerate(events, start=1)
    ]


def _seed_task_store(path, rows):
    """Real task_events schema and production event indexes; rows are (id, task, event, payload)."""

    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(task_store.SCHEMA)
        task_store.ensure_event_indexes(conn)
        conn.executemany(
            "INSERT INTO task_events(event_id, task_id, event, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (event_id, task_id, event, json.dumps(payload), "2026-09-20T00:00:00Z")
                for event_id, task_id, event, payload in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _seed_needfix(repo_root, causes):
    needfix_db = repo_root.joinpath(*needfix_store.NEEDFIX_DB_REL)
    needfix_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(needfix_db))
    try:
        conn.execute("CREATE TABLE needfix(id TEXT, caused_by_json TEXT, created_at TEXT)")
        conn.executemany(
            "INSERT INTO needfix(id, caused_by_json, created_at) VALUES (?, ?, ?)",
            [
                (
                    f"NF-{index}",
                    json.dumps(cause) if cause else None,
                    f"2026-09-20T00:00:{index:02d}Z",
                )
                for index, cause in enumerate(causes)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _use_store(monkeypatch, task_db):
    monkeypatch.setattr(
        task_store,
        "storage_readiness",
        lambda root: SimpleNamespace(ready=True, reason="", canonical_db=task_db),
    )


def _read_store(tmp_path, monkeypatch, rows, *, limit, causes=(), name="repo"):
    repo_root = tmp_path / name
    repo_root.mkdir()
    task_db = repo_root / "tasks.db"
    _seed_task_store(task_db, rows)
    if causes:
        _seed_needfix(repo_root, causes)
    _use_store(monkeypatch, task_db)
    return sdlc_outcome_metrics.read_repository_metrics(
        repo_root, repository_id="repo-one", limit=limit
    )


def _large_store_events():
    events = []

    def add(task_id, event, payload=None):
        events.append((task_id, event, payload or {}))

    def noise(count, offset):
        for index in range(count):
            add(f"undecided-{(offset + index) % 50}", "claim")

    add("T-old", "claim")
    add("T-old", "reject_review")
    for _ in range(2100):
        add("T-old", "progress")
    add("T-old", "accept_review", _acceptance("T-old", "R-old", "a" * 64))
    noise(600, 0)
    add("T-legacy", "claim")
    add("T-legacy", "accept_review", {"request_id": "R-legacy"})
    add("T-mid", "claim")
    noise(150, 7)
    add("T-mid", "accept_review", _acceptance("T-mid", "R-mid", "b" * 64))
    add("T-new", "claim")
    noise(20, 3)
    add("T-new", "reject_review")
    add("T-new", "claim")
    noise(20, 11)
    add("T-new", "accept_review", _acceptance("T-new", "R-new", "c" * 64))
    noise(50, 13)
    return events


def test_recent_complete_cohort_is_covered_despite_more_events_than_the_cap(
    tmp_path, monkeypatch
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task_db = repo_root / "tasks.db"
    _seed_task_store(task_db, _numbered(_large_store_events()))
    _seed_needfix(repo_root, [
        _cause("T-new", "R-new", "c" * 64),
        _cause("T-old", "R-old", "a" * 64),
        None,
    ])
    _use_store(monkeypatch, task_db)
    cap = sdlc_outcome_metrics.MAX_LIMIT
    conn = sqlite3.connect(str(task_db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] > cap
        newest_window = [
            dict(zip(("event_id", "task_id", "event", "payload_json", "created_at"), row))
            for row in conn.execute(
                "SELECT event_id, task_id, event, payload_json, created_at FROM task_events "
                "ORDER BY event_id DESC LIMIT ?", (cap + 1,)
            )
        ]
    finally:
        conn.close()
    before = (task_db.read_bytes(), sorted(path.name for path in repo_root.iterdir()))

    result = sdlc_outcome_metrics.read_repository_metrics(
        repo_root, repository_id="repo-one", limit=cap
    )

    assert (task_db.read_bytes(), sorted(path.name for path in repo_root.iterdir())) == before
    assert result == sdlc_outcome_metrics.read_repository_metrics(
        repo_root, repository_id="repo-one", limit=cap
    )
    assert result["population_bounds"] == {
        "limit": cap,
        "task_events_scanned": cap,
        "canonical_events_after_deduplication": cap,
        "needfix_rows_scanned": 3,
        "task_events_truncated": True,
        "needfix_rows_truncated": False,
    }
    assert result["decided_task_cohort"] == {
        "selected": 4,
        "complete": 2,
        "incomplete": 1,
        "unknown": 1,
        "truncated": True,
        "excluded": [
            {"task_id": "T-legacy", "reason": "accepted_outcome_unverified"},
            {"task_id": "T-old", "reason": "history_incomplete"},
        ],
        "excluded_truncated": False,
    }
    assert result["first_pass_acceptance"] == {
        "numerator": 1, "denominator": 2, "evidence_covered": 2, "evidence_total": 4
    }
    assert result["review_rounds_per_accepted_task"] == {
        "numerator": 3, "denominator": 2, "evidence_covered": 2, "evidence_total": 4
    }
    assert result["escaped_defect_attribution"] == {
        "numerator": 1,
        "denominator": 3,
        "evidence_covered": 0,
        "evidence_total": 3,
        "unknown_unattributed": 2,
        "outside_event_bound_unknown": 1,
        "task_event_population_complete": False,
    }

    # The raw newest-events window cannot vouch for any history, so it still claims none.
    legacy = sdlc_outcome_metrics.aggregate(
        newest_window, [], repository_id="repo-one", limit=cap
    )
    assert legacy["first_pass_acceptance"]["evidence_covered"] == 0
    assert legacy["decided_task_cohort"]["complete"] == 0
    assert legacy["decided_task_cohort"]["incomplete"] == 4


def test_cohort_statement_uses_the_existing_event_indexes_without_a_table_scan(tmp_path):
    db_path = tmp_path / "tasks.db"
    _seed_task_store(db_path, _numbered([
        (f"T{index % 40}", "accept_review" if index % 7 == 0 else "claim", {})
        for index in range(400)
    ]))
    conn = sqlite3.connect(str(db_path))
    try:
        plan = [
            row[3] for row in conn.execute(
                "EXPLAIN QUERY PLAN " + sdlc_outcome_metrics._DECIDED_COHORT_SQL, (50,)
            )
        ]
    finally:
        conn.close()
    assert not [step for step in plan if step.startswith("SCAN")], plan
    assert any("idx_task_store_events_event_id" in step for step in plan), plan
    assert any("idx_task_store_events_task_event_id" in step for step in plan), plan


def test_cohort_read_is_one_task_event_statement_however_many_tasks(tmp_path, monkeypatch):
    events = []
    for index in range(300):
        events.append((f"T{index}", "claim", {}))
        events.append(
            (f"T{index}", "accept_review", _acceptance(f"T{index}", f"R{index}", "a" * 64))
        )
    statements = []
    real_connect = sdlc_outcome_metrics.connect_readonly

    def traced(path, **kwargs):
        conn = real_connect(path, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sdlc_outcome_metrics, "connect_readonly", traced)
    result = _read_store(
        tmp_path, monkeypatch, _numbered(events), limit=sdlc_outcome_metrics.MAX_LIMIT
    )
    assert result["decided_task_cohort"]["complete"] == 300
    assert len([sql for sql in statements if "task_events" in sql]) == 1
    assert {sql.split()[0].upper() for sql in statements} <= {"SELECT"}


@pytest.mark.parametrize(
    ("limit", "selected", "complete", "incomplete", "truncated", "first_pass"),
    [
        (5, 2, 2, 0, False, (1, 2)),
        (4, 2, 1, 1, True, (0, 1)),
        (3, 1, 1, 0, True, (0, 1)),
        (2, 1, 0, 1, True, (0, 0)),
        (1, 1, 0, 1, True, (0, 0)),
    ],
)
def test_event_bound_cuts_only_the_last_history_and_reports_it(
    tmp_path, monkeypatch, limit, selected, complete, incomplete, truncated, first_pass
):
    rows = _numbered([
        ("older", "claim", {}),
        ("older", "accept_review", _acceptance("older", "R-older", "a" * 64)),
        ("newer", "claim", {}),
        ("newer", "reject_review", {}),
        ("newer", "accept_review", _acceptance("newer", "R-newer", "b" * 64)),
    ])
    result = _read_store(tmp_path, monkeypatch, rows, limit=limit)
    cohort = result["decided_task_cohort"]
    assert (cohort["selected"], cohort["complete"], cohort["incomplete"]) == (
        selected, complete, incomplete
    )
    assert cohort["unknown"] == 0
    assert cohort["truncated"] is truncated
    metric = result["first_pass_acceptance"]
    assert (metric["numerator"], metric["denominator"]) == first_pass
    assert metric["evidence_covered"] == complete


def test_a_task_accepted_twice_is_one_history_not_two(tmp_path, monkeypatch):
    rows = _numbered([
        ("T", "claim", {}),
        ("T", "accept_review", _acceptance("T", "R1", "a" * 64)),
        ("T", "reject_review", {}),
        ("T", "accept_review", _acceptance("T", "R2", "b" * 64)),
    ])
    result = _read_store(tmp_path, monkeypatch, rows, limit=50)
    assert result["decided_task_cohort"]["selected"] == 1
    assert result["population_bounds"]["task_events_scanned"] == 4
    assert result["population_bounds"]["canonical_events_after_deduplication"] == 4
    assert result["first_pass_acceptance"]["numerator"] == 1
    assert result["review_rounds_per_accepted_task"]["numerator"] == 1


def test_small_store_reads_the_same_metrics_as_the_pure_aggregate(tmp_path, monkeypatch):
    events = [
        ("T1", "accept_review", _acceptance("T1", "R1", "a" * 64)),
        ("T2", "reject_review", {}),
        ("T2", "accept_review", _acceptance("T2", "R2", "c" * 64)),
    ]
    causes = [_cause("T1", "R1", "a" * 64), None, _cause("T1", "stale", "a" * 64)]
    stored = _read_store(tmp_path, monkeypatch, _numbered(events), limit=100, causes=causes)
    pure = sdlc_outcome_metrics.aggregate(
        [
            {"event_id": event_id, "task_id": task_id, "event": event, "payload": payload}
            for event_id, task_id, event, payload in _numbered(events)
        ],
        [{"id": f"NF-{index}", "caused_by": cause} for index, cause in enumerate(causes)],
        repository_id="repo-one",
        limit=100,
    )
    assert stored == pure
    assert stored["first_pass_acceptance"] == {
        "numerator": 1, "denominator": 2, "evidence_covered": 2, "evidence_total": 2
    }
    assert stored["decided_task_cohort"]["truncated"] is False


def test_store_result_does_not_depend_on_insertion_order(tmp_path, monkeypatch):
    events = []
    for index in range(6):
        events.append((f"T{index}", "claim", {}))
        events.append((f"noise{index}", "claim", {}))
        if index % 2:
            events.append((f"T{index}", "reject_review", {}))
        events.append(
            (f"T{index}", "accept_review", _acceptance(f"T{index}", f"R{index}", "a" * 64))
        )
    numbered = _numbered(events)
    results = []
    for seed in range(3):
        shuffled = list(numbered)
        random.Random(seed).shuffle(shuffled)
        results.append(
            _read_store(tmp_path, monkeypatch, shuffled, limit=12, name=f"repo{seed}")
        )
    assert results[0] == results[1] == results[2]
    cohort = results[0]["decided_task_cohort"]
    assert (cohort["selected"], cohort["complete"], cohort["incomplete"]) == (5, 4, 1)
    assert cohort["truncated"] is True


def test_aggregate_ignores_event_order():
    events = [
        _accepted(1, "T1", "R1", "a" * 64),
        {"event_id": 2, "task_id": "T2", "event": "reject_review", "payload": {}},
        _accepted(3, "T2", "R2", "c" * 64),
        {"event_id": 4, "task_id": "T3", "event": "claim", "payload": {}},
        {"event_id": 5, "task_id": "T4", "event": "accept_review", "payload": {}},
    ]
    rows = [{"id": "NF-1", "caused_by": _cause("T1", "R1", "a" * 64)}, {"caused_by": None}]
    expected = sdlc_outcome_metrics.aggregate(
        events, rows, repository_id="repo-one", limit=100
    )
    assert expected["decided_task_cohort"]["unknown"] == 1
    for seed in range(8):
        shuffled = list(events)
        random.Random(seed).shuffle(shuffled)
        assert sdlc_outcome_metrics.aggregate(
            shuffled, rows, repository_id="repo-one", limit=100
        ) == expected


def test_aggregate_honours_an_incomplete_mark_on_an_untruncated_list():
    events = [_accepted(1, "T1", "R1", "a" * 64), _accepted(2, "T2", "R2", "b" * 64)]
    cohort = sdlc_outcome_metrics.DecidedTaskCohort(("T2", "T1"), frozenset({"T1"}))
    result = sdlc_outcome_metrics.aggregate(
        events, [], repository_id="repo-one", cohort=cohort
    )
    assert result["decided_task_cohort"] == {
        "selected": 2,
        "complete": 1,
        "incomplete": 1,
        "unknown": 0,
        "truncated": False,
        "excluded": [{"task_id": "T2", "reason": "history_incomplete"}],
        "excluded_truncated": False,
    }
    assert result["first_pass_acceptance"] == {
        "numerator": 1, "denominator": 1, "evidence_covered": 1, "evidence_total": 2
    }


def test_excluded_histories_are_listed_within_a_fixed_bound():
    limit = sdlc_outcome_metrics.MAX_EXCLUDED_LISTED
    events = [
        {"event_id": index + 1, "task_id": f"U{index:02d}", "event": "accept_review", "payload": {}}
        for index in range(limit + 5)
    ]
    cohort = sdlc_outcome_metrics.aggregate(
        events, [], repository_id="repo-one", limit=100
    )["decided_task_cohort"]
    assert cohort["unknown"] == limit + 5
    assert [entry["task_id"] for entry in cohort["excluded"]] == [
        f"U{index:02d}" for index in range(limit)
    ]
    assert cohort["excluded_truncated"] is True


def test_aggregate_never_drops_a_selected_task_that_has_no_events():
    events = [_accepted(1, "T1", "R1", "a" * 64)]
    cohort = sdlc_outcome_metrics.DecidedTaskCohort(("T2", "T1"), frozenset({"T1", "T2"}))
    result = sdlc_outcome_metrics.aggregate(
        events, [], repository_id="repo-one", cohort=cohort
    )
    assert result["decided_task_cohort"]["selected"] == 2
    assert result["decided_task_cohort"]["incomplete"] == 1
    assert result["decided_task_cohort"]["excluded"] == [
        {"task_id": "T2", "reason": "history_incomplete"}
    ]
    assert result["first_pass_acceptance"]["denominator"] == 1


def test_cohort_read_loads_payloads_only_for_acceptance_events(tmp_path):
    db_path = tmp_path / "tasks.db"
    _seed_task_store(db_path, _numbered([
        ("T", "claim", {"blob": "x" * 50_000}),
        ("T", "review_ready", {"blob": "y" * 50_000}),
        ("T", "accept_review", _acceptance("T", "R", "a" * 64)),
    ]))
    conn = sdlc_outcome_metrics.connect_readonly(db_path)
    try:
        rows, cohort = sdlc_outcome_metrics.read_decided_task_cohort(conn, 50)
    finally:
        conn.close()
    assert [(row["event"], row["payload_json"] is None) for row in rows] == [
        ("claim", True), ("review_ready", True), ("accept_review", False)
    ]
    assert sum(len(row["payload_json"] or "") for row in rows) < 5_000
    assert cohort.complete == frozenset({"T"})
