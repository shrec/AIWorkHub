"""Retry economics by cause: why each retry's previous attempt ended."""

import json
import sqlite3

from aiworkhub import cost_ledger, task_store


def _attempt(
    task_id: str,
    when: str,
    *,
    tokens: int = 100,
    cost_usd: float = 0.5,
    terminal_cause: str | None = None,
    legacy: bool = False,
) -> dict[str, object]:
    """One hand-built usage row; no ledger store or subprocess is touched."""
    row: dict[str, object] = {
        "task_id": task_id,
        "attempt_recorded_at": when,
        "created_at": when,
        "total_tokens": tokens,
        "cost_usd": cost_usd,
        "cost_known": True,
    }
    if not legacy:
        # A canonical row carries the joined terminal cause; a legacy row
        # (usage stdout, history_series callers) predates the field entirely.
        row["terminal_cause"] = terminal_cause or ""
    return row


def test_quota_exhausted_previous_attempt_counts_under_quota_exhausted():
    rows = [
        _attempt("t1", "2026-01-01T00:00:00Z", terminal_cause="quota_exhausted"),
        _attempt("t1", "2026-01-02T00:00:00Z", tokens=200, cost_usd=0.25),
    ]
    by_cause = cost_ledger._retry_economics(rows, {})["by_cause"]
    assert by_cause["quota_exhausted"]["retry_records"] == 1
    assert by_cause["quota_exhausted"]["retry_tokens"] == 200
    assert by_cause["quota_exhausted"]["retry_cost_usd"] == 0.25


def test_failed_state_without_terminal_substatus_counts_under_failed():
    rows = [
        _attempt("t1", "2026-01-01T00:00:00Z", terminal_cause="failed"),
        _attempt("t1", "2026-01-02T00:00:00Z", tokens=150, cost_usd=0.1),
    ]
    by_cause = cost_ledger._retry_economics(rows, {})["by_cause"]
    assert by_cause["failed"]["retry_records"] == 1
    assert by_cause["failed"]["retry_tokens"] == 150


def test_missing_previous_event_counts_under_unknown():
    rows = [
        _attempt("t1", "2026-01-01T00:00:00Z"),
        _attempt("t1", "2026-01-02T00:00:00Z", tokens=30, cost_usd=0.05),
    ]
    by_cause = cost_ledger._retry_economics(rows, {})["by_cause"]
    assert by_cause["unknown"]["retry_records"] == 1
    assert by_cause["unknown"]["retry_tokens"] == 30


def test_legacy_row_without_the_field_yields_unknown_not_keyerror():
    rows = [
        _attempt("t1", "2026-01-01T00:00:00Z", legacy=True),
        _attempt("t1", "2026-01-02T00:00:00Z", tokens=30, cost_usd=0.05, legacy=True),
    ]
    by_cause = cost_ledger._retry_economics(rows, {})["by_cause"]
    assert by_cause["unknown"]["retry_records"] == 1


def test_by_cause_sums_equal_retry_totals_exactly():
    rows = [
        # t1 retried once because attempt 1 exhausted its quota.
        _attempt("t1", "2026-01-01T00:00:00Z", terminal_cause="quota_exhausted"),
        _attempt("t1", "2026-01-02T00:00:00Z", tokens=200, cost_usd=0.25),
        # t2 retried twice, each time after a failed previous attempt.
        _attempt(
            "t2", "2026-01-01T00:00:00Z", tokens=50, cost_usd=0.05,
            terminal_cause="failed",
        ),
        _attempt(
            "t2", "2026-01-02T00:00:00Z", tokens=150, cost_usd=0.1,
            terminal_cause="failed",
        ),
        _attempt("t2", "2026-01-03T00:00:00Z", tokens=10, cost_usd=0.0),
        # t3 retried once with no matching process event at all.
        _attempt("t3", "2026-01-01T00:00:00Z", tokens=70, cost_usd=0.2),
        _attempt("t3", "2026-01-02T00:00:00Z", tokens=30, cost_usd=0.05),
    ]
    result = cost_ledger._retry_economics(rows, {})
    by_cause = result["by_cause"]
    assert list(by_cause) == sorted(by_cause)
    assert (
        sum(bucket["retry_records"] for bucket in by_cause.values())
        == result["retry_records"]
    )
    assert (
        sum(bucket["retry_tokens"] for bucket in by_cause.values())
        == result["retry_tokens"]
    )
    assert result["retry_records"] == 4
    assert result["retry_tokens"] == 390


def test_process_event_in_newest_ledger_lines_still_joins(monkeypatch, tmp_path):
    monkeypatch.setattr(cost_ledger, "_PROCESS_EVENT_LIMIT", 2)
    ledger = tmp_path / ".aiworkhub" / "runtime" / "process_logs" / "process_events.jsonl"
    ledger.parent.mkdir(parents=True)
    older = [
        {"task_id": "t_old1", "request_id": "r_old1", "state": "done"},
        {"task_id": "t_old2", "request_id": "r_old2", "state": "done"},
    ]
    newest = {
        "task_id": "t1",
        "request_id": "r1",
        "state": "failed",
        "terminal_substatus": "quota_exhausted",
    }
    ledger.write_text(
        "".join(json.dumps(event) + "\n" for event in [*older, newest]),
        encoding="utf-8",
    )
    usage = {
        "task_id": "t1",
        "request_id": "r1",
        "topic": "x",
        "created_at": "2026-01-02T00:00:00Z",
        "attempt_recorded_at": "2026-01-02T00:00:00Z",
        "total_tokens": 10,
        "cost_usd": 0.0,
    }
    monkeypatch.setattr(
        cost_ledger.task_store,
        "list_usage_events",
        lambda root, *, limit=10_000: [usage],
    )
    [row] = cost_ledger._canonical_usage_rows(tmp_path)
    assert row["terminal_cause"] == "quota_exhausted"

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


def test_rejected_previous_attempt_without_process_event_buckets_by_failure_category(
    monkeypatch, tmp_path
):
    _seed_store(tmp_path, [
        ("t1", "reject_review", {"request_id": "r1", "failure_category": "validation_environment"}),
    ])
    rows = _rows(monkeypatch, tmp_path, [_usage("t1", "r1", 1), _usage("t1", "r2", 2)])
    previous = next(row for row in rows if row["attempt_id"] == "r1")
    assert previous["terminal_cause"] == "reject:validation_environment"
    assert previous["terminal_cause_source"] == "task_store"
    assert previous["process_event_miss_reason"] == "process_ledger_empty"

    economics = cost_ledger._retry_economics(rows, {})
    assert economics["by_cause"]["reject:validation_environment"]["retry_records"] == 1
    assert economics["by_cause_source"] == {"task_store": 1}
    assert economics["unknown_share_percent"] == 0.0


def test_terminal_timeout_substatus_maps_through_the_failure_classifier(
    monkeypatch, tmp_path
):
    _seed_store(tmp_path, [
        ("t1", "terminal_failure", {"request_id": "r1", "substatus": "timed_out"}),
    ])
    rows = _rows(monkeypatch, tmp_path, [_usage("t1", "r1", 1), _usage("t1", "r2", 2)])
    previous = next(row for row in rows if row["attempt_id"] == "r1")
    assert previous["terminal_cause"] == "cancellation_or_timeout"
    assert previous["terminal_cause_source"] == "task_store"


def test_task_store_is_read_once_per_build_not_per_row(monkeypatch, tmp_path):
    _seed_store(tmp_path, [
        ("t1", "reject_review", {"request_id": "r1", "failure_category": "candidate_code"}),
    ])
    connects = []
    bulk_calls = []
    real_connect = cost_ledger.task_store._connect
    real_bulk = cost_ledger._task_store_terminal_causes

    def counting_connect(*args, **kwargs):
        if kwargs.get("readonly"):
            connects.append(args)
        return real_connect(*args, **kwargs)

    def counting_bulk(*args, **kwargs):
        bulk_calls.append(args)
        return real_bulk(*args, **kwargs)

    monkeypatch.setattr(cost_ledger.task_store, "_connect", counting_connect)
    monkeypatch.setattr(cost_ledger, "_task_store_terminal_causes", counting_bulk)

    _rows(monkeypatch, tmp_path, [_usage("t1", "r1", 1)])
    single_row_connects = len(connects)
    assert len(bulk_calls) == 1

    connects.clear()
    bulk_calls.clear()
    usage = [_usage(f"t{n}", f"r{n}", 1) for n in range(6)] + [_usage("t1", "r1", 2)]
    _rows(monkeypatch, tmp_path, usage)
    assert len(bulk_calls) == 1
    assert len(connects) == single_row_connects


def test_process_event_cause_wins_and_skips_the_store_read(monkeypatch, tmp_path):
    ledger = tmp_path / ".aiworkhub" / "runtime" / "process_logs" / "process_events.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({
            "task_id": "t1", "request_id": "r1", "terminal_substatus": "quota_exhausted",
        }) + "\n",
        encoding="utf-8",
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no store read when every row has a process event")

    monkeypatch.setattr(cost_ledger.task_store, "_require_ready", forbidden)
    [row] = _rows(monkeypatch, tmp_path, [_usage("t1", "r1", 1)])
    assert row["terminal_cause"] == "quota_exhausted"
    assert row["terminal_cause_source"] == "process_event"


def test_store_read_failure_leaves_unknown_with_a_named_reason(monkeypatch, tmp_path):
    def broken(*_args, **_kwargs):
        raise cost_ledger.task_store.TaskStoreError("boom")

    monkeypatch.setattr(cost_ledger.task_store, "_require_ready", broken)
    rows = _rows(monkeypatch, tmp_path, [_usage("t1", "r1", 1), _usage("t1", "r2", 2)])
    previous = next(row for row in rows if row["attempt_id"] == "r1")
    assert previous["terminal_cause"] == ""
    assert previous["terminal_cause_source"] == "unknown"
    assert previous["terminal_cause_unresolved"] == "task_store_unreadable:TaskStoreError"
    economics = cost_ledger._retry_economics(rows, {})
    assert economics["by_cause"]["unknown"]["retry_records"] == 1
    assert economics["unknown_share_percent"] == 100.0
    assert economics["unknown_reasons"] == {"task_store_unreadable:TaskStoreError": 1}


def test_rows_with_no_evidence_stay_unknown(monkeypatch, tmp_path):
    _seed_store(tmp_path, [
        ("t1", "reject_review", {"request_id": "other", "failure_category": "candidate_code"}),
    ])
    no_request = {**_usage("t2", "r1", 1), "request_id": ""}
    rows = _rows(monkeypatch, tmp_path, [_usage("t1", "r1", 1), no_request])
    assert {row["terminal_cause"] for row in rows} == {""}
    assert {row["terminal_cause_unresolved"] for row in rows} == {
        "no_store_evidence", "no_request_id",
    }