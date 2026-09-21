from __future__ import annotations

import hashlib
import io
import json
import random
import sqlite3
from types import SimpleNamespace

import pytest

from aiworkhub import needfix_store, sdlc_outcome_metrics, task_engine, task_store


def _receipt(task_id: str, request_id: str, digest: str, claim_epoch: int = 1):
    hashes = {"fixed.txt": digest}
    unsigned = {
        "schema_id": task_engine.ACCEPTED_OUTCOME_RECEIPT_SCHEMA,
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": claim_epoch,
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


def _accepted(event_id: int, task_id: str, request_id: str, digest: str, claim_epoch: int = 1):
    return {
        "event_id": event_id,
        "task_id": task_id,
        "event": "accept_review",
        "payload": {
            "request_id": request_id,
            "accepted_outcome_receipt": _receipt(task_id, request_id, digest, claim_epoch),
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


# --- matched reasoning/context outcome comparison (plan Task 3) -------------------------------

_REPO = "repo-one"
_LEDGER_DIR = (".aiworkhub", "runtime", "process_logs")
_APPLIED_HIGH = {
    "profile": "canonical_high", "status": "applied", "key": "reasoningEffort", "value": "high",
}
_UNSUPPORTED_HIGH = {"profile": "canonical_high", "status": "unsupported"}
_APPLIED_ARM = "canonical_high|applied|reasoningEffort|high"
_UNSUPPORTED_ARM = "canonical_high|unsupported|-|-"
_OWN_CARDS = object()
_FAILED_WORKER = {
    "state": "worker_failed",
    "failure_kind": "worker_failed",
    "diagnostic": "worker_failed:runtime_error:exit_code=1",
    "terminal_reason": {"code": "worker_failed"},
}
_LAUNCH_REFUSED = {
    "state": "launch_failed",
    "failure_kind": "launch_failed",
    "terminal_reason": {"code": "launch_failed"},
}


def _host_receipt(
    request_id, *, model="glm-5.3", profile="canonical_high", status="unsupported",
    key=None, value=None, turns=1, capacity=1_000_000, repo=_REPO, unknown_reason=None,
):
    return {
        "schema_id": "aiworkhub.reasoning_context_attempt.v1",
        "request_id": request_id,
        "repo_id": repo,
        "requested_model": model,
        "host_model": {
            "id": model, "family": model, "name": model.upper(),
            "vendor": "customendpoint", "version": "1.0.0",
        },
        "requested_profile": profile,
        "send_state": "sent",
        "send_turn_count": turns,
        "provider_request_acknowledged": True,
        "option_status": status,
        "option_key": key,
        "option_value": value,
        "context_capacity_tokens": capacity,
        "context_capacity_source": "model.maxInputTokens" if capacity else "unknown",
        "provider_internal_state": "unknown",
        "unknown_reason": unknown_reason,
    }


def _attempt(
    task_id, request_id, *, adapter="glm_vscode_lm", model="glm-5.3", epoch=1, repo=_REPO,
    receipt=None, **effort,
):
    return {
        "schema_id": "aiworkhub.reasoning_context_attempt_event.v1",
        "identity": {
            "repo_id": repo, "task_id": task_id, "request_id": request_id,
            "adapter_id": adapter, "model": model, "claim_epoch": epoch,
        },
        "receipt": receipt or _host_receipt(request_id, model=model, repo=repo, **effort),
    }


def _unknown_attempt(task_id, request_id, *, reason="receipt_absent", **identity):
    from aiworkhub import vscode_lm_worker

    spec = {
        "request_id": request_id, "repo_id": _REPO, "model": identity.get("model", "glm-5.3"),
    }
    return _attempt(
        task_id, request_id, receipt=vscode_lm_worker._attempt_unknown(spec, reason), **identity
    )


def _ledger_row(
    task_id, request_id, attempt=None, *, cost=None, tokens=None,
    timestamp="2026-09-21T10:00:00+00:00", **row,
):
    attempt = attempt or _attempt(task_id, request_id, **_UNSUPPORTED_HIGH)
    return {
        "schema_id": "aiworkhub.task_mcp.process_event.v1",
        "timestamp": timestamp,
        "request_id": request_id,
        "task_id": task_id,
        "adapter_id": attempt["identity"]["adapter_id"],
        "model": attempt["identity"]["model"],
        "state": "review_ready",
        "failure_kind": None,
        "usage": {
            "role": "worker",
            "cost_observed": cost is not None,
            "cost_usd": cost,
            "total_tokens_observed": tokens is not None,
            "total_tokens": tokens or 0,
        },
        "reasoning_context_attempt": attempt,
        **row,
    }


def _card(family="code", risk="high"):
    card = {"topic": "task_mcp", "project_context": {"task_type": family} if family else {}}
    if risk:
        card["risk_tier"] = risk
    return card


def _decided(events, task_id, *, rejections=0, claim_epoch=1, start=None, end=None, verified=True):
    request_id = f"R-{task_id}"

    def add(event, payload=None, at=None):
        row = {
            "event_id": len(events) + 1, "task_id": task_id, "event": event,
            "payload": payload or {},
        }
        if at:
            row["created_at"] = at
        events.append(row)

    add("claim_start", at=start)
    for _ in range(rejections):
        add("reject_review")
    accepted = _accepted(0, task_id, request_id, "a" * 64, claim_epoch)["payload"]
    add("accept_review", accepted if verified else {"request_id": request_id}, at=end)
    return request_id


class _Population:
    def __init__(self):
        self.events, self.cards, self.rows, self.order = [], {}, [], []

    def task(
        self, task_id, *, family="code", risk="high", ledger=True, attempt=None,
        cost=None, tokens=None, row=None, **history,
    ):
        request_id = _decided(self.events, task_id, **history)
        self.order.append(task_id)
        self.cards[task_id] = _card(family, risk)
        if ledger:
            self.rows.append(
                _ledger_row(task_id, request_id, attempt, cost=cost, tokens=tokens, **(row or {}))
            )
        return request_id

    def evidence(self, **fields):
        return sdlc_outcome_metrics.AttemptEvidence(
            rows=tuple(self.rows), available=True, **fields
        )

    def aggregate(
        self, *, attempts="rows", complete=None, needfix=(), limit=500, cards=_OWN_CARDS,
    ):
        if isinstance(attempts, str):
            attempts = self.evidence()
        return sdlc_outcome_metrics.aggregate(
            self.events,
            list(needfix),
            repository_id=_REPO,
            limit=limit,
            cohort=sdlc_outcome_metrics.DecidedTaskCohort(
                tuple(self.order), frozenset(self.order if complete is None else complete)
            ),
            attempts=attempts,
            task_cards=self.cards if cards is _OWN_CARDS else cards,
        )

    def section(self, **kwargs):
        return self.aggregate(**kwargs)["reasoning_context_outcome_comparison"]


def _metric(numerator, denominator, covered, total, *, truncated=False, **extra):
    return {
        "numerator": numerator, "denominator": denominator, "sample_size": covered,
        "evidence_covered": covered, "evidence_total": total, "unknown": total - covered,
        "truncated": truncated, **extra,
    }


def _matched_scenario():
    pop = _Population()
    pop.task(
        "A1", attempt=_attempt("A1", "R-A1", **_APPLIED_HIGH), cost=0.4, tokens=1000,
        start="2026-09-21T10:00:00Z", end="2026-09-21T10:10:00Z",
    )
    pop.task("A2", rejections=1, attempt=_attempt("A2", "R-A2", **_APPLIED_HIGH))
    pop.task("B1", cost=0.3, tokens=800)
    pop.task("B2", rejections=1, cost=0.5, tokens=1200)
    pop.task("C1", family="research", risk="low", cost=0.1, tokens=300)
    pop.task(
        "D1",
        attempt=_attempt(
            "D1", "R-D1", adapter="vscode_lm", model="deepseek-v4-pro", **_UNSUPPORTED_HIGH
        ),
        cost=0.2, tokens=500,
    )
    pop.task("E1", ledger=False)
    pop.task("F1", ledger=False)
    pop.task(
        "G1", attempt=_unknown_attempt("G1", "R-G1"),
        row={
            **_LAUNCH_REFUSED,
            "diagnostic": "launch_failed:provider_refused:http_status=402",
        },
    )
    pop.task("H1", attempt=_attempt("H1", "R-H1", epoch=2, **_UNSUPPORTED_HIGH))
    pop.task("I1", attempt=_attempt("I1", "R-I1", repo="other-repo", **_UNSUPPORTED_HIGH))
    pop.task("J1", attempt=_unknown_attempt("J1", "R-J1"), row=_FAILED_WORKER)
    pop.task("K1", risk=None)
    pop.task("L1", attempt=_unknown_attempt("L1", "R-L1"))
    pop.task("M1", ledger=False, verified=False)
    pop.rows.append(_ledger_row("STRAY", "R-STRAY"))
    return pop


def test_matched_cohorts_join_exact_receipts_and_type_every_exclusion():
    pop = _matched_scenario()
    result = pop.aggregate(complete=set(pop.order) - {"F1"})
    section = result["reasoning_context_outcome_comparison"]

    assert result["decided_task_cohort"]["selected"] == 15
    assert section["schema_id"] == sdlc_outcome_metrics.COMPARISON_SCHEMA_ID
    assert section["state"] == "MATCHED_OBSERVATIONAL"
    population = section["population"]
    assert population == {
        "selected": 15,
        "joined": 6,
        "comparable": 4,
        "unmatched": 2,
        "unmatched_reasons": {"single_effort_arm": 2},
        "option_status_counts": {"applied": 2, "unsupported": 4},
        "excluded": {
            "accepted_outcome_unverified": 1,
            "attempt_failed_non_provider": 1,
            "attempt_identity_foreign": 1,
            "attempt_identity_stale": 1,
            "attempt_receipt_missing": 1,
            "attempt_receipt_unknown": 1,
            "history_incomplete": 1,
            "provider_quota_refusal": 1,
            "risk_tier_unknown": 1,
        },
        "excluded_listed": [
            {"task_id": "E1", "reason": "attempt_receipt_missing"},
            {"task_id": "F1", "reason": "history_incomplete"},
            {"task_id": "G1", "reason": "provider_quota_refusal", "detail": "http_402"},
            {"task_id": "H1", "reason": "attempt_identity_stale"},
            {"task_id": "I1", "reason": "attempt_identity_foreign"},
            {"task_id": "J1", "reason": "attempt_failed_non_provider", "detail": "worker_failed"},
            {"task_id": "K1", "reason": "risk_tier_unknown"},
            {"task_id": "L1", "reason": "attempt_receipt_unknown", "detail": "receipt_absent"},
            {"task_id": "M1", "reason": "accepted_outcome_unverified"},
        ],
        "excluded_truncated": False,
        "truncated": False,
    }
    assert population["selected"] == population["joined"] + sum(population["excluded"].values())
    assert population["joined"] == population["comparable"] + population["unmatched"]
    ledger = section["attempt_ledger"]
    assert (ledger["available"], ledger["truncated"]) == (True, False)
    assert (
        ledger["receipt_rows"], ledger["receipt_rows_unmatched"], ledger["receipt_rows_superseded"]
    ) == (13, 1, 0)

    cohorts = section["cohorts"]
    assert [
        (c["route"], c["model"], c["task_family"], c["risk_tier"], c["arm_id"]) for c in cohorts
    ] == [
        ("glm_vscode_lm", "glm-5.3", "code", "high", _APPLIED_ARM),
        ("glm_vscode_lm", "glm-5.3", "code", "high", _UNSUPPORTED_ARM),
        ("glm_vscode_lm", "glm-5.3", "research", "low", _UNSUPPORTED_ARM),
        ("vscode_lm", "deepseek-v4-pro", "code", "high", _UNSUPPORTED_ARM),
    ]
    assert [c["comparable"] for c in cohorts] == [True, True, False, False]
    assert [c["cell_effort_settings"] for c in cohorts] == [2, 2, 1, 1]
    assert [c["sample_size"] for c in cohorts] == [2, 2, 1, 1]
    assert section["cells"] == {"total": 3, "comparable": 1}
    assert section["cohorts_truncated"] is False

    applied, unsupported = cohorts[0], cohorts[1]
    assert applied["effort_setting"] == {
        "requested_profile": "canonical_high", "option_status": "applied",
        "option_key": "reasoningEffort", "option_value": "high",
    }
    assert unsupported["effort_setting"] == {
        "requested_profile": "canonical_high", "option_status": "unsupported",
        "option_key": None, "option_value": None,
    }
    assert applied["metrics"] == {
        "first_pass_acceptance": _metric(1, 2, 2, 2),
        "review_rounds_per_accepted_task": _metric(3, 2, 2, 2),
        "severe_findings": _metric(0, 2, 2, 2, unknown_severity=0),
        "elapsed_seconds": _metric(600.0, 1, 1, 2),
        "accepted_attempt_total_tokens": _metric(1000, 1, 1, 2),
        "accepted_attempt_cost_usd": _metric(0.4, 1, 1, 2),
        "context_capacity": {
            **_metric(None, 2, 2, 2),
            "sources": {"model.maxInputTokens": 2},
            "tokens_min": 1_000_000,
            "tokens_max": 1_000_000,
            "basis": "model_capacity_not_consumption",
        },
    }
    assert unsupported["metrics"]["first_pass_acceptance"] == _metric(1, 2, 2, 2)
    assert unsupported["metrics"]["review_rounds_per_accepted_task"] == _metric(3, 2, 2, 2)
    assert unsupported["metrics"]["elapsed_seconds"] == _metric(None, 0, 0, 2)
    assert unsupported["metrics"]["accepted_attempt_total_tokens"] == _metric(2000, 2, 2, 2)
    assert unsupported["metrics"]["accepted_attempt_cost_usd"] == _metric(0.8, 2, 2, 2)
    # The unknown price stays UNKNOWN: the observed 0.4 is never scaled up to two tasks.
    assert applied["metrics"]["accepted_attempt_cost_usd"]["numerator"] == 0.4


def test_small_complete_fixture_retains_first_pass_and_review_round_values():
    pop = _Population()
    pop.task("T1")
    pop.task("T2", rejections=2)
    result = pop.aggregate()
    [cohort] = result["reasoning_context_outcome_comparison"]["cohorts"]

    assert result["first_pass_acceptance"] == {
        "numerator": 1, "denominator": 2, "evidence_covered": 2, "evidence_total": 2
    }
    assert result["review_rounds_per_accepted_task"]["numerator"] == 4
    assert cohort["comparable"] is False
    assert cohort["metrics"]["first_pass_acceptance"] == _metric(1, 2, 2, 2)
    assert cohort["metrics"]["review_rounds_per_accepted_task"] == _metric(4, 2, 2, 2)


def test_requested_high_effort_the_host_could_not_apply_is_never_an_applied_arm():
    pop = _Population()
    pop.task("T1", attempt=_attempt("T1", "R-T1", turns=18, **_UNSUPPORTED_HIGH))
    section = pop.section()
    [cohort] = section["cohorts"]

    assert cohort["arm_id"] == _UNSUPPORTED_ARM
    assert cohort["effort_setting"] == {
        "requested_profile": "canonical_high", "option_status": "unsupported",
        "option_key": None, "option_value": None,
    }
    assert cohort["metrics"]["context_capacity"] == {
        **_metric(None, 1, 1, 1),
        "sources": {"model.maxInputTokens": 1},
        "tokens_min": 1_000_000,
        "tokens_max": 1_000_000,
        "basis": "model_capacity_not_consumption",
    }
    assert cohort["comparable"] is False
    assert section["state"] == "NO_MATCHED_COHORT"
    assert all(c["effort_setting"]["option_status"] != "applied" for c in section["cohorts"])


def test_a_failed_bridge_attempt_is_neither_applied_nor_a_quality_failure():
    pop = _Population()
    # The durable row of a bridge error carries the launcher's typed UNKNOWN (receipt_absent);
    # a failed row that still holds a verified 18-turn host receipt must not become an arm either.
    pop.task("T1", attempt=_unknown_attempt("T1", "R-T1"), row=_FAILED_WORKER)
    pop.task(
        "T2", attempt=_attempt("T2", "R-T2", turns=18, **_UNSUPPORTED_HIGH), row=_FAILED_WORKER
    )
    result = pop.aggregate()
    section = result["reasoning_context_outcome_comparison"]

    assert section["state"] == "UNKNOWN"
    assert section["cohorts"] == []
    assert section["population"]["option_status_counts"] == {}
    assert section["population"]["excluded"] == {"attempt_failed_non_provider": 2}
    assert section["population"]["excluded_listed"] == [
        {"task_id": "T1", "reason": "attempt_failed_non_provider", "detail": "worker_failed"},
        {"task_id": "T2", "reason": "attempt_failed_non_provider", "detail": "worker_failed"},
    ]
    # The decided tasks' review outcomes are unchanged; an attempt failure adds no rejection.
    assert result["first_pass_acceptance"]["numerator"] == 2
    assert result["first_pass_acceptance"]["denominator"] == 2


def test_provider_refusals_are_typed_only_from_typed_evidence_never_from_prose():
    pop = _Population()
    quota_body = {"owner": "provider", "sealed": True, "http_status": 402}
    cases = {
        "P1": {**_LAUNCH_REFUSED, "provider_error": quota_body,
               "diagnostic": "launch_failed:runtime_error"},
        "P2": {**_LAUNCH_REFUSED, "diagnostic": "launch_failed:auth_unauthorized:http_status=401"},
        "P3": {**_LAUNCH_REFUSED, "error": "http 402 insufficient_balance",
               "diagnostic": "launch_failed:runtime_error:exit_code=1"},
        "P4": {**_LAUNCH_REFUSED, "provider_error": {**quota_body, "sealed": False},
               "diagnostic": "launch_failed:runtime_error"},
        "P5": {"state": "cancelled", "failure_kind": None,
               "terminal_reason": {"code": "cancelled"}},
        "P6": {**_LAUNCH_REFUSED, "diagnostic": "launch_failed:rate_limited:http_status=429"},
    }
    for task_id, row in cases.items():
        pop.task(task_id, attempt=_unknown_attempt(task_id, f"R-{task_id}"), row=row)

    listed = pop.section()["population"]["excluded_listed"]
    assert {entry["task_id"]: entry["reason"] for entry in listed} == {
        "P1": "provider_quota_refusal",
        "P2": "provider_auth_refusal",
        "P3": "attempt_failed_non_provider",
        "P4": "attempt_failed_non_provider",
        "P5": "attempt_failed_non_provider",
        "P6": "provider_quota_refusal",
    }


def test_missing_unavailable_and_out_of_bound_evidence_are_distinct_unknowns():
    pop = _Population()
    pop.task("T1", ledger=False)

    def excluded(attempts):
        return pop.section(attempts=attempts)["population"]["excluded"]

    assert excluded(None) == {"attempt_ledger_unavailable": 1}
    assert excluded(
        sdlc_outcome_metrics.AttemptEvidence(available=True, truncated=True)
    ) == {"attempt_receipt_outside_scan_bound": 1}
    assert excluded(
        sdlc_outcome_metrics.AttemptEvidence(available=True)
    ) == {"attempt_receipt_missing": 1}
    pop.rows.append({"request_id": "R-T1", "task_id": "T1"})
    assert excluded("rows") == {"attempt_receipt_missing": 1}


def test_receipts_the_worker_validator_refuses_are_typed_and_never_joined():
    pop = _Population()
    pop.task("T1", attempt=_attempt("T1", "R-T1", receipt=_host_receipt("R-T1", turns=-1)))
    pop.task("T2", attempt=_attempt("T2", "R-T2", receipt=_host_receipt("R-OTHER")))
    pop.task(
        "T3",
        attempt=_attempt("T3", "R-T3", receipt={**_host_receipt("R-T3"), "schema_id": "other"}),
    )
    pop.task("T4", attempt=_attempt("T4", "R-T4", receipt=_host_receipt("R-T4", status="applied")))
    pop.task("T5", attempt={**_attempt("T5", "R-T5"), "schema_id": "other"})
    section = pop.section()

    assert section["population"]["joined"] == 0
    assert section["population"]["excluded_listed"] == [
        {"task_id": "T1", "reason": "attempt_receipt_malformed", "detail": "receipt_bounds_invalid"},
        {"task_id": "T2", "reason": "attempt_identity_foreign"},
        {"task_id": "T3", "reason": "attempt_receipt_malformed",
         "detail": "receipt_schema_mismatch"},
        {"task_id": "T4", "reason": "attempt_receipt_malformed", "detail": "receipt_inconsistent"},
        {"task_id": "T5", "reason": "attempt_receipt_malformed"},
    ]


def test_cohorts_match_only_on_known_task_family_and_risk_tier():
    pop = _Population()
    pop.task("F1", family=None)
    pop.task("R1", risk=None)
    pop.task("BOTH", family=None, risk=None)
    pop.task("OK")

    section = pop.section()
    assert section["population"]["excluded"] == {"risk_tier_unknown": 1, "task_family_unknown": 2}
    assert section["population"]["joined"] == 1
    # Unread cards are typed apart from a read card that genuinely lacks family or risk.
    unread = pop.section(cards=None)["population"]
    assert unread["excluded"] == {"task_card_unavailable": 4}
    assert {entry.get("detail") for entry in unread["excluded_listed"]} == {"not_supplied"}
    failed = sdlc_outcome_metrics.TaskCardEvidence(failure="store_error")
    assert pop.section(cards=failed)["population"]["excluded"] == {"task_card_unavailable": 4}
    partial = pop.section(cards={"OK": _card()})["population"]
    assert partial["excluded"] == {"task_card_missing": 3}


@pytest.mark.parametrize(
    "card",
    [
        {"topic": "quality_review", "risk_tier": "high", "project_context": {"task_type": "research"}},
        {"topic": "task_mcp", "risk_tier": " HIGH ", "project_context": {"task_type": "Code"}},
        {"topic": "x", "risk_tier": "urgent", "project_context": {"task_type": "data_classification"}},
        {"topic": "x", "project_context": {"task_type": "translate"}},
        {"topic": "x", "risk_tier": None, "project_context": "not-a-mapping"},
        {},
    ],
)
def test_family_and_risk_classification_matches_the_cost_ledger(card):
    from aiworkhub import cost_ledger

    assert sdlc_outcome_metrics._task_family(card) == cost_ledger._task_family(card)
    assert sdlc_outcome_metrics._task_risk(card) == cost_ledger._task_risk(card)


def test_vocabulary_restated_here_stays_inside_its_owners():
    from aiworkhub import process_launcher, runtime_adapters

    assert (
        sdlc_outcome_metrics.ATTEMPT_EVENT_SCHEMA_ID
        == process_launcher.REASONING_CONTEXT_ATTEMPT_EVENT_SCHEMA_ID
    )
    assert sdlc_outcome_metrics.SEVERE_SEVERITIES <= set(needfix_store.SEVERITIES)
    assert sdlc_outcome_metrics._AUTH_STATUSES <= runtime_adapters.PROVIDER_REFUSAL_STATUSES


def _truncation_flags(cohort):
    return {metric["truncated"] for metric in cohort["metrics"].values()}


def test_truncation_marks_a_metric_only_when_the_cut_could_have_hidden_members():
    pop = _Population()
    pop.task("T1", cost=0.5, tokens=100)
    # A byte-bound cut that still found every accepted request hides no decided task.
    older_cut = {"truncated": True, "truncation_causes": ("byte_bound", "row_bound")}
    found = pop.section(attempts=pop.evidence(**older_cut))
    assert found["attempt_ledger"]["truncated"] is True
    assert found["attempt_ledger"]["truncation_reasons"] == ["byte_bound", "row_bound"]
    assert _truncation_flags(found["cohorts"][0]) == {False}

    # Any other or untyped cause may hide a newer row of a joined request: never clean.
    for causes in (
        (), ("segment_unreadable",), ("oversized_row",), ("byte_bound", "segment_incomplete"),
    ):
        unsafe = pop.section(attempts=pop.evidence(truncated=True, truncation_causes=causes))
        assert unsafe["population"]["joined"] == 1
        assert _truncation_flags(unsafe["cohorts"][0]) == {True}, causes

    # Once the cut leaves a decided task unjoined, every metric of the arms is marked.
    pop.task("T2", ledger=False)
    hidden = pop.section(attempts=pop.evidence(**older_cut))
    assert hidden["population"]["excluded"] == {"attempt_receipt_outside_scan_bound": 1}
    assert _truncation_flags(hidden["cohorts"][0]) == {True}

    # Events past the event bound cut the decided population itself.
    fresh = _Population()
    fresh.task("T1")
    fresh.events.extend(
        {"event_id": 100 + i, "task_id": "OTHER", "event": "claim_start", "payload": {}}
        for i in range(5)
    )
    population_cut = fresh.section(limit=3)
    assert population_cut["population"]["truncated"] is True
    assert _truncation_flags(population_cut["cohorts"][0]) == {True}


def test_severe_findings_count_only_exactly_attributed_known_severe_rows():
    pop = _Population()
    pop.task("A1", attempt=_attempt("A1", "R-A1", **_APPLIED_HIGH))
    pop.task("B1")
    exact = _cause("A1", "R-A1", "a" * 64)
    needfix = [
        {"id": "NF-1", "severity": "high", "caused_by": exact},
        {"id": "NF-2", "severity": "critical", "caused_by": exact},
        {"id": "NF-3", "severity": "low", "caused_by": exact},
        {"id": "NF-4", "severity": "high", "caused_by": _cause("A1", "stale-request", "a" * 64)},
        {"id": "NF-5", "caused_by": exact},
        {"id": "NF-6", "severity": "bogus", "caused_by": exact},
        {"id": "NF-7", "severity": "high", "caused_by": None},
    ]
    applied, unsupported = pop.section(needfix=needfix)["cohorts"]

    assert applied["metrics"]["severe_findings"] == _metric(2, 1, 1, 1, unknown_severity=2)
    assert unsupported["metrics"]["severe_findings"] == _metric(0, 1, 1, 1, unknown_severity=0)

    bounded = pop.section(
        needfix=needfix + [{"id": f"X{i}", "caused_by": None} for i in range(200)], limit=100
    )["cohorts"][0]["metrics"]["severe_findings"]
    assert bounded == _metric(2, 1, 0, 1, truncated=True, unknown_severity=2)


def test_comparison_listing_is_bounded_and_reports_totals():
    listed = sdlc_outcome_metrics.MAX_COHORTS_LISTED
    pop = _Population()
    for index in range(listed + 5):
        task_id = f"T{index:03d}"
        pop.task(
            task_id,
            attempt=_attempt(task_id, f"R-{task_id}", adapter=f"route-{index:03d}", **_UNSUPPORTED_HIGH),
        )
    section = pop.section()
    assert section["cells"] == {"total": listed + 5, "comparable": 0}
    assert section["population"]["joined"] == listed + 5
    assert section["cohorts_truncated"] is True
    assert [c["route"] for c in section["cohorts"]] == [f"route-{i:03d}" for i in range(listed)]

    unmatched = _Population()
    excluded_listed = sdlc_outcome_metrics.MAX_EXCLUDED_LISTED
    for index in range(excluded_listed + 5):
        unmatched.task(f"U{index:03d}", ledger=False)
    population = unmatched.section(attempts=unmatched.evidence())["population"]
    assert population["excluded"] == {"attempt_receipt_missing": excluded_listed + 5}
    assert len(population["excluded_listed"]) == excluded_listed
    assert population["excluded_truncated"] is True


def test_comparison_is_independent_of_input_and_read_order():
    pop = _matched_scenario()
    complete = set(pop.order) - {"F1"}
    expected = pop.section(complete=complete)
    for seed in range(4):
        shuffled = _Population()
        shuffled.events, shuffled.rows = list(pop.events), list(pop.rows)
        shuffled.cards, shuffled.order = pop.cards, list(pop.order)
        for offset, items in enumerate((shuffled.events, shuffled.rows, shuffled.order)):
            random.Random(seed + 100 * offset).shuffle(items)
        assert shuffled.section(complete=complete) == expected


def _all_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _all_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _all_keys(item)


def test_comparison_makes_no_causal_effect_or_extrapolated_cost_claim():
    section = _matched_scenario().section()
    assert section["claim_boundary"] == sdlc_outcome_metrics.CLAIM_BOUNDARY
    boundary = section["claim_boundary"].lower()
    for phrase in ("observational", "provider-internal", "not consumption", "never extrapolated"):
        assert phrase in boundary
    assert not [
        key for key in _all_keys(section)
        if any(word in key for word in ("caus", "saving", "improv", "effect", "percent", "mean"))
    ]


def _write_jsonl(path, rows, tail=""):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows) + tail, encoding="utf-8")


def test_read_attempt_evidence_is_bounded_read_only_and_projects_rows(tmp_path, monkeypatch):
    directory = tmp_path / "logs"
    directory.mkdir()
    archive = directory / "process_events.20260101T000000.000000Z.1.aaaaaaaa.jsonl"
    ledger = directory / "process_events.jsonl"
    _write_jsonl(archive, [_ledger_row("T-old", "R-old", timestamp="2026-01-01T00:00:00+00:00")])
    heavy = _ledger_row(
        "T-new", "R-new", project_context={"blob": "x" * 20_000},
        validation=[{"stdout": "y" * 5_000}],
    )
    hostile = '{"reasoning_context_attempt": ' + "[" * 200_000 + "\n"
    ledger.write_text(
        json.dumps({"request_id": "R-noise", "state": "running", "blob": "z" * 30_000}) + "\n"
        + json.dumps(heavy) + "\n"
        + json.dumps({"request_id": "R-nested", "detail": {"reasoning_context_attempt": {"a": 1}}})
        + "\n"
        + json.dumps({"request_id": "R-null", "reasoning_context_attempt": None}) + "\n"
        + '{"request_id": "R-corrupt", "reasoning_context_attempt": {"identity"\n'
        + hostile
        + '{"request_id": "R-partial", "reasoning_context_attempt": {"identity"',
        encoding="utf-8",
    )
    before = (ledger.read_bytes(), archive.read_bytes(), sorted(p.name for p in directory.iterdir()))

    evidence = sdlc_outcome_metrics.read_attempt_evidence(ledger)

    assert (ledger.read_bytes(), archive.read_bytes(), sorted(p.name for p in directory.iterdir())) == before
    # The unterminated R-partial tail is excluded, so the evidence is honestly incomplete.
    assert (evidence.available, evidence.truncated, evidence.oversized_rows) == (True, True, 0)
    assert evidence.truncation_causes == ("segment_incomplete",)
    assert (evidence.segments_total, evidence.segments_scanned) == (2, 2)
    assert evidence.bytes_scanned == ledger.stat().st_size + archive.stat().st_size
    assert sorted(row["request_id"] for row in evidence.rows) == ["R-new", "R-old"]
    projected = next(row for row in evidence.rows if row["request_id"] == "R-new")
    assert "project_context" not in projected and "validation" not in projected
    assert projected["reasoning_context_attempt"] == heavy["reasoning_context_attempt"]
    assert set(projected["usage"]) <= {
        "role", "cost_observed", "cost_usd", "total_tokens_observed", "total_tokens"
    }

    newest_only = sdlc_outcome_metrics.read_attempt_evidence(ledger, byte_bound=ledger.stat().st_size)
    assert (newest_only.segments_scanned, newest_only.truncated) == (1, True)
    assert newest_only.truncation_causes == ("byte_bound", "segment_incomplete")
    assert [row["request_id"] for row in newest_only.rows] == ["R-new"]
    nothing = sdlc_outcome_metrics.read_attempt_evidence(ledger, byte_bound=0)
    assert (nothing.available, nothing.segments_scanned, nothing.truncated, nothing.rows) == (
        True, 0, True, ()
    )
    assert nothing.truncation_causes == ("byte_bound",)

    monkeypatch.setattr(sdlc_outcome_metrics, "MAX_LEDGER_ROWS", 0)
    row_capped = sdlc_outcome_metrics.read_attempt_evidence(ledger)
    assert (row_capped.segments_scanned, row_capped.truncated) == (1, True)
    assert row_capped.truncation_causes == ("row_bound", "segment_incomplete")
    assert [row["request_id"] for row in row_capped.rows] == ["R-new"]
    monkeypatch.setattr(sdlc_outcome_metrics, "MAX_LEDGER_ROWS", 20_000)

    monkeypatch.setattr(sdlc_outcome_metrics, "MAX_LEDGER_ROW_BYTES", 4096)
    capped = sdlc_outcome_metrics.read_attempt_evidence(ledger)
    assert [row["request_id"] for row in capped.rows] == ["R-old"]
    assert (capped.oversized_rows, capped.truncated) == (3, True)
    assert capped.truncation_causes == ("oversized_row", "segment_incomplete")

    absent = sdlc_outcome_metrics.read_attempt_evidence(directory / "elsewhere" / "log.jsonl")
    assert (absent.available, absent.rows, absent.truncated) == (False, (), False)
    assert absent.truncation_causes == ()
    assert not (directory / "elsewhere").exists()


@pytest.mark.parametrize("oversized_tail", [False, True])
def test_read_attempt_evidence_never_reads_past_a_growing_snapshot(
    tmp_path, monkeypatch, oversized_tail
):
    # A writer appends between lstat and read: the snapshot ends mid-record (a JSON-complete
    # receipt still missing its newline, or an oversized unterminated record) and the file
    # then grows by a newline, a newer receipt and an unterminated MiB-scale record.
    monkeypatch.setattr(sdlc_outcome_metrics, "MAX_LEDGER_ROW_BYTES", 4096)
    directory = tmp_path / "logs"
    directory.mkdir()
    ledger = directory / "process_events.jsonl"
    cut = "y" * 5_000 if oversized_tail else json.dumps(_ledger_row("T-cut", "R-cut"))
    _write_jsonl(ledger, [_ledger_row("T-kept", "R-kept")], tail=cut)
    snapshot = ledger.stat().st_size
    growth = (
        ("y" * 5_000 if oversized_tail else "") + "\n"
        + json.dumps(_ledger_row("T-late", "R-late")) + "\n" + "x" * (1 << 21)
    ).encode()
    real_open = sdlc_outcome_metrics.os.open

    def open_after_append(path, flags, *args):
        if str(path) == str(ledger):
            with ledger.open("ab") as handle:
                handle.write(growth)
        return real_open(path, flags, *args)

    monkeypatch.setattr(sdlc_outcome_metrics.os, "open", open_after_append)
    evidence = sdlc_outcome_metrics.read_attempt_evidence(ledger, byte_bound=snapshot)

    assert ledger.stat().st_size == snapshot + len(growth)
    assert evidence.bytes_scanned == snapshot <= evidence.byte_bound
    assert [row["request_id"] for row in evidence.rows] == ["R-kept"]
    assert (evidence.segments_scanned, evidence.truncated) == (1, True)
    assert evidence.oversized_rows == (1 if oversized_tail else 0)


@pytest.mark.parametrize("tail_kind", ["valid_json_without_newline", "partial_json"])
def test_read_attempt_evidence_reports_a_stable_unterminated_tail_as_incomplete(
    tmp_path, tail_kind
):
    # The file never grows after lstat, yet its last record lacks a newline: the record is
    # excluded, so the evidence must say it is incomplete rather than claim a full read.
    directory = tmp_path / "logs"
    directory.mkdir()
    ledger = directory / "process_events.jsonl"
    receipt = json.dumps(_ledger_row("T-tail", "R-tail"))
    tail = receipt if tail_kind == "valid_json_without_newline" else receipt[: len(receipt) // 2]
    _write_jsonl(ledger, [_ledger_row("T-kept", "R-kept")], tail=tail)
    size = ledger.stat().st_size

    evidence = sdlc_outcome_metrics.read_attempt_evidence(ledger)

    assert ledger.stat().st_size == size
    assert [row["request_id"] for row in evidence.rows] == ["R-kept"]
    assert (evidence.bytes_scanned, evidence.oversized_rows) == (size, 0)
    assert (evidence.segments_scanned, evidence.truncated) == (1, True)

    _write_jsonl(ledger, [_ledger_row("T-kept", "R-kept")])
    complete = sdlc_outcome_metrics.read_attempt_evidence(ledger)
    assert [row["request_id"] for row in complete.rows] == ["R-kept"]
    assert complete.truncated is False


def test_scan_segment_flags_a_discarded_tail_without_growth(tmp_path, monkeypatch):
    ledger = tmp_path / "process_events.jsonl"
    ledger.write_bytes(b"unfinished")
    monkeypatch.setattr(sdlc_outcome_metrics.os, "open", lambda *_args, **_kwargs: 99)
    monkeypatch.setattr(
        sdlc_outcome_metrics.os, "fdopen", lambda *_args, **_kwargs: io.BytesIO(b"unfinished")
    )
    rows: list = []
    assert sdlc_outcome_metrics._scan_segment(ledger, 10, rows) == (10, 0, True)
    assert rows == []


def test_read_attempt_evidence_keeps_the_newest_row_for_one_request_id():
    older = _ledger_row("T", "R-T", timestamp="2026-09-21T09:00:00+00:00")
    newer = _ledger_row("T", "R-T", cost=1.0, tokens=10, timestamp="2026-09-21T11:00:00+00:00")
    pop = _Population()
    pop.task("T", ledger=False)
    pop.rows.extend([newer, older])
    section = pop.section()
    [cohort] = section["cohorts"]
    assert section["attempt_ledger"]["receipt_rows"] == 1
    assert section["attempt_ledger"]["receipt_rows_superseded"] == 1
    assert cohort["metrics"]["accepted_attempt_cost_usd"]["numerator"] == 1.0


def _seed_cards(path, cards):
    conn = sqlite3.connect(str(path))
    try:
        conn.executemany(
            "INSERT INTO tasks (task_id, runner, topic, status, worker_status, card_json, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (task_id, "glm", card.get("topic", ""), "accepted", "accepted",
                 json.dumps(card), "now", "now")
                for task_id, card in cards.items()
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _seed_needfix_with_severity(repo_root, entries):
    needfix_db = repo_root.joinpath(*needfix_store.NEEDFIX_DB_REL)
    needfix_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(needfix_db))
    try:
        conn.execute(
            "CREATE TABLE needfix(id TEXT, caused_by_json TEXT, severity TEXT, created_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO needfix(id, caused_by_json, severity, created_at) VALUES (?, ?, ?, ?)",
            [
                (f"NF-{index}", json.dumps(cause), severity, f"2026-09-20T00:00:{index:02d}Z")
                for index, (cause, severity) in enumerate(entries)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_read_repository_metrics_joins_the_process_ledger_and_task_cards(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task_db = repo_root / "tasks.db"
    pop = _Population()
    pop.task("A1", attempt=_attempt("A1", "R-A1", **_APPLIED_HIGH), cost=0.4, tokens=1000)
    pop.task("B1", rejections=1, cost=0.3, tokens=800)
    _seed_task_store(
        task_db, [(e["event_id"], e["task_id"], e["event"], e["payload"]) for e in pop.events]
    )
    _seed_cards(task_db, pop.cards)
    _seed_needfix_with_severity(repo_root, [
        (_cause("A1", "R-A1", "a" * 64), "high"), (_cause("B1", "R-B1", "a" * 64), "low"),
    ])
    ledger_dir = repo_root.joinpath(*_LEDGER_DIR)
    ledger_dir.mkdir(parents=True)
    ledger = ledger_dir / "process_events.jsonl"
    _write_jsonl(ledger, pop.rows)
    _use_store(monkeypatch, task_db)
    statements = []
    real_connect = sdlc_outcome_metrics.connect_readonly

    def traced(path, **kwargs):
        conn = real_connect(path, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sdlc_outcome_metrics, "connect_readonly", traced)
    before = (ledger.read_bytes(), sorted(p.name for p in ledger_dir.iterdir()))

    result = sdlc_outcome_metrics.read_repository_metrics(
        repo_root, repository_id=_REPO, limit=100
    )

    assert (ledger.read_bytes(), sorted(p.name for p in ledger_dir.iterdir())) == before
    assert {sql.split()[0].upper() for sql in statements} <= {"SELECT"}
    assert len([sql for sql in statements if "task_events" in sql]) == 1
    section = result["reasoning_context_outcome_comparison"]
    assert section["state"] == "MATCHED_OBSERVATIONAL"
    assert section["attempt_ledger"]["available"] is True
    assert section["population"]["joined"] == 2
    applied, unsupported = section["cohorts"]
    assert (applied["arm_id"], unsupported["arm_id"]) == (_APPLIED_ARM, _UNSUPPORTED_ARM)
    assert applied["metrics"]["severe_findings"]["numerator"] == 1
    assert unsupported["metrics"]["severe_findings"]["numerator"] == 0
    assert applied["metrics"]["accepted_attempt_cost_usd"]["numerator"] == 0.4
    assert unsupported["metrics"]["review_rounds_per_accepted_task"]["numerator"] == 2
    assert result == sdlc_outcome_metrics.read_repository_metrics(
        repo_root, repository_id=_REPO, limit=100
    )


def test_absent_process_ledger_reports_unavailable_not_missing(tmp_path, monkeypatch):
    rows = _numbered([("T1", "accept_review", _acceptance("T1", "R1", "a" * 64))])
    result = _read_store(tmp_path, monkeypatch, rows, limit=50)
    section = result["reasoning_context_outcome_comparison"]

    assert section["state"] == "UNKNOWN"
    assert section["attempt_ledger"]["available"] is False
    assert section["population"]["excluded"] == {"attempt_ledger_unavailable": 1}
    assert not (tmp_path / "repo" / ".aiworkhub").exists()


def test_a_host_receipt_that_could_not_name_the_option_is_unknown_not_an_effort_arm():
    pop = _Population()
    unnamed = _host_receipt("R-T1", status="unknown", unknown_reason="option_shape_unrecognized")
    pop.task("T1", attempt=_attempt("T1", "R-T1", receipt=unnamed))
    section = pop.section()

    assert section["cohorts"] == []
    assert section["state"] == "UNKNOWN"
    assert section["population"]["excluded_listed"] == [
        {"task_id": "T1", "reason": "attempt_receipt_unknown", "detail": "option_shape_unrecognized"}
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: row["reasoning_context_attempt"]["identity"].update(repo_id="other-repo"),
        lambda row: row["reasoning_context_attempt"]["identity"].update(task_id="OTHER"),
        lambda row: row["reasoning_context_attempt"]["identity"].update(request_id="R-OTHER"),
        lambda row: row.update(task_id="OTHER"),
        lambda row: row.update(adapter_id="claude_cli"),
    ],
)
def test_any_identity_that_is_not_the_accepted_attempts_own_is_foreign(mutate):
    pop = _Population()
    pop.task("T1")
    mutate(pop.rows[0])

    assert pop.section()["population"]["excluded"] == {"attempt_identity_foreign": 1}


@pytest.mark.parametrize("epoch", [2, 0, None, True, "1"])
def test_a_claim_epoch_that_is_not_the_accepted_one_is_stale(epoch):
    pop = _Population()
    pop.task("T1", attempt=_attempt("T1", "R-T1", epoch=epoch, **_UNSUPPORTED_HIGH))

    assert pop.section()["population"]["excluded"] == {"attempt_identity_stale": 1}


def test_unobserved_or_unusable_usage_values_are_unknown_never_zero():
    usage = {"role": "worker", "cost_observed": True, "total_tokens_observed": True}
    observed = {"cost_usd": 0.25, "total_tokens": 10}
    cases = {
        "U1": {**usage, **observed},
        "U2": {**usage, "cost_observed": False, "total_tokens_observed": False,
               "cost_usd": 0.0, "total_tokens": 0},
        "U3": {**usage, "cost_usd": 10**400, "total_tokens": True},
        "U4": {**usage, "cost_usd": float("nan"), "total_tokens": -5},
        "U5": {**usage, "cost_usd": True, "total_tokens": "7"},
        "U6": {**usage, "cost_usd": 0.0, "total_tokens": 0},
        "U7": {"role": "worker"},
    }
    pop = _Population()
    for task_id, values in cases.items():
        pop.task(task_id, row={"usage": values})
    [cohort] = pop.section()["cohorts"]

    assert cohort["metrics"]["accepted_attempt_cost_usd"] == _metric(0.25, 2, 2, 7)
    assert cohort["metrics"]["accepted_attempt_total_tokens"] == _metric(10, 2, 2, 7)


def test_read_task_cards_is_chunked_read_only_and_fails_closed(tmp_path):
    path = tmp_path / "tasks.db"
    _seed_task_store(path, [])
    cards = {f"T{index:04d}": _card(risk="low" if index % 2 else None) for index in range(1201)}
    _seed_cards(path, cards)
    statements = []
    conn = sdlc_outcome_metrics.connect_readonly(path)
    try:
        conn.set_trace_callback(statements.append)
        found = sdlc_outcome_metrics.read_task_cards(conn, [*cards, "T0000", "ABSENT"])
    finally:
        conn.close()
    assert (found.available, found.failure) == (True, None)
    assert len(found.cards) == 1201 and "ABSENT" not in found.cards
    assert found.cards["T0001"] == {
        "topic": "task_mcp", "risk_tier": "low", "project_context": {"task_type": "code"},
    }
    assert found.cards["T0002"]["risk_tier"] is None
    assert len(statements) == 3
    assert all(sql.lstrip().startswith("SELECT") for sql in statements)

    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, card_json, created_at, updated_at) "
            "VALUES ('BAD', '{not json', 'now', 'now')"
        )
        conn.commit()
    finally:
        conn.close()
    bare = tmp_path / "bare.db"
    sqlite3.connect(str(bare)).close()
    for source, ids, failure in (
        (path, ["T0001", "BAD"], "card_malformed"), (bare, ["T0001"], "store_error"),
    ):
        conn = sdlc_outcome_metrics.connect_readonly(source)
        try:
            # An unreadable card fails the whole read closed rather than biasing the subset.
            assert sdlc_outcome_metrics.read_task_cards(conn, ids) == (
                sdlc_outcome_metrics.TaskCardEvidence(failure=failure)
            )
        finally:
            conn.close()


def _repository_with_cards(root, monkeypatch, pop, card_rows):
    root.mkdir()
    task_db = root / "tasks.db"
    _seed_task_store(
        task_db, [(e["event_id"], e["task_id"], e["event"], e["payload"]) for e in pop.events]
    )
    _seed_cards(task_db, card_rows)
    ledger_dir = root.joinpath(*_LEDGER_DIR)
    ledger_dir.mkdir(parents=True)
    _write_jsonl(ledger_dir / "process_events.jsonl", pop.rows)
    _use_store(monkeypatch, task_db)
    return sdlc_outcome_metrics.read_repository_metrics(root, repository_id=_REPO, limit=100)[
        "reasoning_context_outcome_comparison"
    ]["population"]


def test_repository_metrics_type_an_unread_card_store_apart_from_an_absent_family(
    tmp_path, monkeypatch
):
    pop = _Population()
    pop.task("A1", attempt=_attempt("A1", "R-A1", **_APPLIED_HIGH))
    pop.task("B1")

    malformed = tmp_path / "malformed"
    population = _repository_with_cards(malformed, monkeypatch, pop, {"A1": pop.cards["A1"]})
    conn = sqlite3.connect(str(malformed / "tasks.db"))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, card_json, created_at, updated_at) "
            "VALUES ('B1', '{not json', 'now', 'now')"
        )
        conn.commit()
    finally:
        conn.close()
    population = sdlc_outcome_metrics.read_repository_metrics(
        malformed, repository_id=_REPO, limit=100
    )["reasoning_context_outcome_comparison"]["population"]
    assert population["joined"] == 0
    assert population["excluded"] == {"task_card_unavailable": 2}
    assert {entry["detail"] for entry in population["excluded_listed"]} == {"card_malformed"}

    # A readable store that lacks one card, or a card lacking its family, is a different fact.
    absent = _repository_with_cards(
        tmp_path / "absent", monkeypatch, pop, {"A1": _card(family=None)}
    )
    assert absent["excluded"] == {"task_card_missing": 1, "task_family_unknown": 1}


@pytest.mark.parametrize("hidden", ["oversized_row", "segment_unreadable"])
def test_a_hidden_newer_row_of_a_joined_request_never_yields_a_clean_cohort(
    tmp_path, monkeypatch, hidden
):
    monkeypatch.setattr(sdlc_outcome_metrics, "MAX_LEDGER_ROW_BYTES", 4096)
    directory = tmp_path / "logs"
    directory.mkdir()
    archive = directory / "process_events.20260101T000000.000000Z.1.aaaaaaaa.jsonl"
    ledger = directory / "process_events.jsonl"
    older = _ledger_row("T", "R-T", cost=0.1, timestamp="2026-09-21T09:00:00+00:00")
    newer = _ledger_row(
        "T", "R-T", cost=9.0, timestamp="2026-09-21T11:00:00+00:00",
        **({"project_context": {"blob": "x" * 8_000}} if hidden == "oversized_row" else {}),
    )
    _write_jsonl(archive, [older])
    _write_jsonl(ledger, [newer])
    if hidden == "segment_unreadable":
        real_scan = sdlc_outcome_metrics._scan_segment

        def unreadable(path, size, rows):
            if path == ledger:
                raise PermissionError(path)
            return real_scan(path, size, rows)

        monkeypatch.setattr(sdlc_outcome_metrics, "_scan_segment", unreadable)

    evidence = sdlc_outcome_metrics.read_attempt_evidence(ledger)
    assert evidence.truncation_causes == (hidden,)
    assert [row["timestamp"] for row in evidence.rows] == [older["timestamp"]]

    pop = _Population()
    pop.task("T", ledger=False)
    section = pop.section(attempts=evidence)
    # The older row still joins, but its stale cost can never be reported as complete.
    assert section["population"]["joined"] == 1
    [cohort] = section["cohorts"]
    assert cohort["metrics"]["accepted_attempt_cost_usd"]["numerator"] == 0.1
    assert _truncation_flags(cohort) == {True}
    assert section["attempt_ledger"]["truncation_reasons"] == [hidden]
