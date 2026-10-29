"""Retry economics by cause: why each retry's previous attempt ended."""

from aiworkhub import cost_ledger


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
