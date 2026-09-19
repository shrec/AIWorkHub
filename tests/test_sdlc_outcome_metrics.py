from __future__ import annotations

import hashlib
import json
import sqlite3

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
