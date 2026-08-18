"""Provider refusal vs worker crash classification (NF-2026-00275).

A launched worker that exits non-zero was always recorded as ``worker_failed``
exit_code 1, so a dead balance, an expired credential and a genuine crash were
indistinguishable. These tests cover at least two refusal shapes -- including a
429 session limit -- plus the crash case and clean exit, and assert that an
exhausted-quota refusal is operationally recoverable while respecting the reset
window it reports.
"""

from __future__ import annotations

from aiworkhub import runtime_adapters


def test_429_session_limit_is_a_recoverable_provider_refusal() -> None:
    """The measured 429 session limit that killed four cards mid-flight."""
    outcome = runtime_adapters.classify_provider_outcome(
        exit_code=1,
        message=(
            "429 You've hit your session limit. "
            "Your limit resets at 2026-08-18T18:00:00Z"
        ),
    )
    assert outcome["outcome"] == runtime_adapters.OUTCOME_PROVIDER_REFUSED
    assert outcome["refusal"] is True
    assert outcome["refusal_kind"] == runtime_adapters.REFUSAL_SESSION_LIMIT
    # Exhausted-quota refusal is operationally recoverable...
    assert outcome["recoverable"] is True
    # ...and respects the reset window the provider actually reported.
    assert outcome["reset_reported"] is True
    assert outcome["reset_at"] == "2026-08-18T18:00:00Z"
    # The provider's own message is carried, not discarded.
    assert "session limit" in outcome["provider_message"]


def test_expired_credential_is_a_refusal_not_recoverable_by_waiting() -> None:
    """A second refusal shape: an authentication/credential rejection."""
    outcome = runtime_adapters.classify_provider_outcome(
        exit_code=1, message="401 Unauthorized: invalid API key"
    )
    assert outcome["outcome"] == runtime_adapters.OUTCOME_PROVIDER_REFUSED
    assert outcome["refusal"] is True
    assert outcome["refusal_kind"] == runtime_adapters.REFUSAL_CREDENTIAL_REJECTED
    # A dead credential is a refusal, but time alone never recovers it.
    assert outcome["recoverable"] is False
    assert outcome["reset_reported"] is False


def test_rate_limit_with_retry_after_seconds_is_recoverable() -> None:
    outcome = runtime_adapters.classify_provider_outcome(
        exit_code=1, message="429 Too Many Requests. retry-after: 30"
    )
    assert outcome["outcome"] == runtime_adapters.OUTCOME_PROVIDER_REFUSED
    assert outcome["refusal_kind"] == runtime_adapters.REFUSAL_RATE_LIMITED
    assert outcome["recoverable"] is True
    assert outcome["retry_after_seconds"] == 30
    assert outcome["reset_reported"] is True


def test_genuine_worker_crash_is_distinct_from_a_provider_refusal() -> None:
    outcome = runtime_adapters.classify_provider_outcome(
        exit_code=1,
        message="Traceback (most recent call last):\nZeroDivisionError: division by zero",
    )
    assert outcome["outcome"] == runtime_adapters.OUTCOME_WORKER_FAILED
    assert outcome["refusal"] is False
    assert outcome["refusal_kind"] == ""
    assert outcome["recoverable"] is False


def test_quota_exhausted_without_a_reported_window_is_bounded_not_free_retry() -> None:
    outcome = runtime_adapters.classify_provider_outcome(
        exit_code=1, message="Your account balance is exhausted"
    )
    assert outcome["refusal_kind"] == runtime_adapters.REFUSAL_QUOTA_EXHAUSTED
    assert outcome["recoverable"] is True
    # No window was reported, so a caller must not retry immediately: the
    # verdict says so rather than implying a free retry.
    assert outcome["reset_reported"] is False
    assert outcome["retry_after_seconds"] is None
    assert "reset_window_unreported" in outcome["reason"]


def test_clean_exit_is_ok_and_not_a_refusal() -> None:
    outcome = runtime_adapters.classify_provider_outcome(exit_code=0)
    assert outcome["outcome"] == runtime_adapters.OUTCOME_OK
    assert outcome["refusal"] is False
    assert outcome["recoverable"] is False
