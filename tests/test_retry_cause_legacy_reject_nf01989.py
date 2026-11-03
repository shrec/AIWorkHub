"""NF-2026-01989: legacy reject_review rows without request_id, by event order."""

import json
import sqlite3

from aiworkhub import cost_ledger, task_store


def _seed_store(root, events):
    task_store.initialize_repository(root)
    _readiness, db_path = task_store._require_ready(root)
    conn = sqlite3.connect(db_path)
    try:
        for index, (task_id, event, payload) in enumerate(events):
            conn.execute(
                "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
                "VALUES (?, ?, 'codex_worker', ?, ?)",
                (task_id, event, json.dumps(payload), f"2026-01-01T00:00:{index:02d}+00:00"),
            )
        conn.commit()
    finally:
        conn.close()


def _usage(task_id: str, request_id: str, day: int) -> dict[str, object]:
    return {
        "task_id": task_id,
        "request_id": request_id,
        "topic": "x",
        "created_at": f"2026-01-{day:02d}T00:00:00Z",
        "attempt_recorded_at": f"2026-01-{day:02d}T00:00:00Z",
        "total_tokens": 10,
        "cost_usd": 0.0,
    }


def _rows(monkeypatch, root, usage):
    monkeypatch.setattr(
        cost_ledger.task_store, "list_usage_events", lambda _root, *, limit=10_000: usage
    )
    return cost_ledger._canonical_usage_rows(root)


def _row(rows, request_id):
    return next(row for row in rows if row["attempt_id"] == request_id)


def _legacy_reject(**extra):
    # The legacy writer's shape: no request_id, no failure_category, and a
    # prior_episode that names no request.
    return {"decision": "rejected", "prior_episode": {"episode": 1}, **extra}


def test_legacy_reject_between_claims_attributes_to_the_earlier_request(
    monkeypatch, tmp_path
):
    _seed_store(tmp_path, [
        ("t1", "claim_start", {"request_id": "rA"}),
        ("t1", "reject_review", _legacy_reject(terminal_disposition="candidate_code")),
        ("t1", "claim_start", {"request_id": "rB"}),
    ])
    rows = _rows(monkeypatch, tmp_path, [_usage("t1", "rA", 1), _usage("t1", "rB", 2)])
    previous = _row(rows, "rA")
    assert previous["terminal_cause"] == "reject:candidate_code"
    assert previous["terminal_cause_source"] == "task_store_event_order"
    assert _row(rows, "rB")["terminal_cause"] == ""

    economics = cost_ledger._retry_economics(rows, {})
    assert economics["by_cause"]["reject:candidate_code"]["retry_records"] == 1
    assert economics["by_cause_source"] == {"task_store_event_order": 1}
    assert "unknown" not in economics["by_cause"]


def test_legacy_reject_without_any_category_is_unclassified_not_unknown(
    monkeypatch, tmp_path
):
    _seed_store(tmp_path, [
        ("t1", "claim_start", {"request_id": "rA"}),
        ("t1", "reject_review", _legacy_reject()),
        ("t1", "claim_start", {"request_id": "rB"}),
    ])
    rows = _rows(monkeypatch, tmp_path, [_usage("t1", "rA", 1), _usage("t1", "rB", 2)])
    assert _row(rows, "rA")["terminal_cause"] == "reject:unclassified"
    economics = cost_ledger._retry_economics(rows, {})
    assert economics["by_cause"]["reject:unclassified"]["retry_records"] == 1
    assert "unknown" not in economics["by_cause"]


def test_legacy_failure_category_outranks_terminal_disposition(monkeypatch, tmp_path):
    _seed_store(tmp_path, [
        ("t1", "claim_start", {"request_id": "rA"}),
        ("t1", "reject_review", _legacy_reject(
            failure_category="validation_environment",
            terminal_disposition="candidate_code",
        )),
    ])
    rows = _rows(monkeypatch, tmp_path, [_usage("t1", "rA", 1)])
    assert _row(rows, "rA")["terminal_cause"] == "reject:validation_environment"


def test_current_format_reject_is_attributed_exactly_as_before(monkeypatch, tmp_path):
    _seed_store(tmp_path, [
        ("t1", "claim_start", {"request_id": "rB"}),
        # Explicit request_id names rA even though rB is the latest claim.
        ("t1", "reject_review", {"request_id": "rA", "failure_category": "dependency_or_route"}),
    ])
    rows = _rows(monkeypatch, tmp_path, [_usage("t1", "rA", 1), _usage("t1", "rB", 2)])
    assert _row(rows, "rA")["terminal_cause"] == "reject:dependency_or_route"
    assert _row(rows, "rA")["terminal_cause_source"] == "task_store"
    assert _row(rows, "rB")["terminal_cause"] == ""


def test_explicit_request_reject_outranks_event_order(monkeypatch, tmp_path):
    _seed_store(tmp_path, [
        ("t1", "claim_start", {"request_id": "rA"}),
        ("t1", "reject_review", {"request_id": "rA", "failure_category": "provider_runtime"}),
        ("t1", "reject_review", _legacy_reject(terminal_disposition="candidate_code")),
    ])
    rows = _rows(monkeypatch, tmp_path, [_usage("t1", "rA", 1)])
    assert _row(rows, "rA")["terminal_cause"] == "reject:provider_runtime"
    assert _row(rows, "rA")["terminal_cause_source"] == "task_store"


def test_terminal_failure_substatus_still_wins_over_any_reject(monkeypatch, tmp_path):
    _seed_store(tmp_path, [
        ("t1", "claim_start", {"request_id": "rA"}),
        ("t1", "terminal_failure", {"request_id": "rA", "substatus": "quota_exhausted"}),
        ("t1", "reject_review", _legacy_reject(terminal_disposition="candidate_code")),
        ("t1", "reject_review", {"request_id": "rA", "failure_category": "candidate_code"}),
    ])
    rows = _rows(monkeypatch, tmp_path, [_usage("t1", "rA", 1)])
    previous = _row(rows, "rA")
    assert not previous["terminal_cause"].startswith("reject:")
    assert previous["terminal_cause"] == cost_ledger._substatus_cause("quota_exhausted")
    assert previous["terminal_cause_source"] == "task_store"


def test_reject_before_any_claim_start_attributes_nothing(monkeypatch, tmp_path):
    _seed_store(tmp_path, [
        ("t1", "reject_review", _legacy_reject(terminal_disposition="candidate_code")),
        ("t1", "claim_start", {"request_id": "rA"}),
        # Another task's claim never leaks across tasks.
        ("t2", "claim_start", {"request_id": "rX"}),
        ("t1", "claim_start", {"request_id": "rB"}),
    ])
    rows = _rows(monkeypatch, tmp_path, [_usage("t1", "rA", 1), _usage("t1", "rB", 2)])
    assert {row["terminal_cause"] for row in rows} == {""}
    assert {row["terminal_cause_unresolved"] for row in rows} == {"no_store_evidence"}


def test_legacy_attribution_keeps_a_single_store_read(monkeypatch, tmp_path):
    _seed_store(tmp_path, [
        ("t1", "claim_start", {"request_id": "rA"}),
        ("t1", "reject_review", _legacy_reject(terminal_disposition="candidate_code")),
        ("t1", "claim_start", {"request_id": "rB"}),
    ])
    executes = []
    real_connect = cost_ledger.task_store._connect

    class _Spy:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args):
            executes.append(sql)
            return self._conn.execute(sql, *args)

        def close(self):
            self._conn.close()

    def spying_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        return _Spy(conn) if kwargs.get("readonly") else conn

    monkeypatch.setattr(cost_ledger.task_store, "_connect", spying_connect)
    usage = [_usage("t1", "rA", 1), _usage("t1", "rB", 2)] + [
        _usage(f"t{n}", f"r{n}", 1) for n in range(2, 8)
    ]
    _rows(monkeypatch, tmp_path, usage)
    assert sum("FROM task_events" in sql for sql in executes) == 1
