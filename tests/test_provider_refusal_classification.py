"""Provider refusal vs worker crash classification (NF-2026-00275).

A launched worker that exits non-zero was always recorded as ``worker_failed``
exit_code 1, so a dead balance, an expired credential and a genuine crash were
indistinguishable. These tests cover at least two refusal shapes -- including a
429 session limit -- plus the crash case and clean exit, and assert that an
exhausted-quota refusal is operationally recoverable while respecting the reset
window it reports.
"""

from __future__ import annotations

import json
from pathlib import Path

from aiworkhub import process_launcher, runtime_adapters


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
    # The stimulus names USAGE QUOTA specifically ("quota exhausted"), not a
    # balance condition: a bare "...balance is exhausted" is a dead account and
    # now classifies as balance, so it can no longer stand in for a quota body
    # here (NF-2026-00275 rework round four).  The assertions -- quota, recoverable,
    # no reported window -- are unchanged.
    outcome = runtime_adapters.classify_provider_outcome(
        exit_code=1, message="Your usage quota exhausted for this period"
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


def test_quota_refusal_is_not_reported_as_worker_failed() -> None:
    """NF-2026-00275: a quota refusal must not collapse into a worker crash.

    Request dfaa1f4bcfcd46ec8035d47925d93411 discarded 2.6M tokens of completed
    work on a provider quota refusal while the recorded reason pointed at the
    worker. The reason must be derived from the provider's OWN body, not the
    exit code, and must name quota rather than worker_failed.
    """
    outcome = runtime_adapters.classify_provider_outcome(
        exit_code=1,
        message="429 quota exceeded for this billing period. try again in 600 seconds",
    )
    assert outcome["outcome"] == runtime_adapters.OUTCOME_PROVIDER_REFUSED
    assert outcome["outcome"] != runtime_adapters.OUTCOME_WORKER_FAILED
    assert outcome["refusal_kind"] == runtime_adapters.REFUSAL_QUOTA_EXHAUSTED
    # The named reason comes from the body and points at quota, not the worker.
    assert "quota_exhausted" in outcome["reason"]
    assert "worker_failed" not in outcome["reason"]
    # The provider's own words are carried for the operator to read.
    assert "quota exceeded" in outcome["provider_message"]


def test_bare_401_records_cause_not_distinguished_not_a_credential_guess() -> None:
    """NF-2026-00326: a 401 whose body names no cause is not a credential verdict.

    The same claude_cli credential launched nine workers/reviewers successfully,
    then three reviewers hit an HTTP 401 with an empty body, then it cleared on
    its own within ten minutes. A credential that worked nine times and then
    recovered is a quota or rate condition, not a bad key. Naming it
    ``credential_rejected`` (or ``provider_authentication_failed``) from the
    status code alone is a guess wearing a fact's clothing, so the recorded
    reason must say the cause is not distinguished by the response.
    """
    outcome = runtime_adapters.classify_provider_outcome(
        exit_code=1, message="401 Unauthorized"
    )
    assert outcome["outcome"] == runtime_adapters.OUTCOME_PROVIDER_REFUSED
    assert outcome["refusal"] is True
    assert (
        outcome["refusal_kind"]
        == runtime_adapters.REFUSAL_CAUSE_NOT_DISTINGUISHED
    )
    # Explicitly NOT a credential verdict and NOT a worker crash.
    assert outcome["refusal_kind"] != runtime_adapters.REFUSAL_CREDENTIAL_REJECTED
    assert outcome["outcome"] != runtime_adapters.OUTCOME_WORKER_FAILED
    assert outcome["reason"] == (
        "provider_refused:http_status=401:cause_not_distinguished_by_response"
    )
    assert outcome["status_code"] == 401
    # No cause is invented, so no free retry is implied.
    assert outcome["recoverable"] is False
    assert outcome["reset_reported"] is False


def test_distinguishable_and_indistinguishable_401_produce_different_reasons() -> None:
    """A distinguishable 401 body and an indistinguishable one must not collapse.

    The distinction between authentication, quota and rate lives in the body,
    not the status code. A 401 that literally names an invalid key is a
    credential rejection; a 401 with no such token cannot be named, and the two
    must produce DIFFERENT reasons -- a test asserting only that *some* reason
    exists would prove nothing (NF-2026-00326).
    """
    distinguishable = runtime_adapters.classify_provider_outcome(
        exit_code=1, message="401 Unauthorized: invalid API key"
    )
    indistinguishable = runtime_adapters.classify_provider_outcome(
        exit_code=1, message="401 Unauthorized"
    )
    assert (
        distinguishable["refusal_kind"]
        == runtime_adapters.REFUSAL_CREDENTIAL_REJECTED
    )
    assert (
        indistinguishable["refusal_kind"]
        == runtime_adapters.REFUSAL_CAUSE_NOT_DISTINGUISHED
    )
    # Same status code, different named reasons -- the body, not 401, decided.
    assert distinguishable["status_code"] == indistinguishable["status_code"] == 401
    assert distinguishable["reason"] != indistinguishable["reason"]
    assert "credential_rejected" in distinguishable["reason"]
    assert "cause_not_distinguished_by_response" in indistinguishable["reason"]


# ---------------------------------------------------------------------------
# Production-path regressions (NF-2026-00304/00330 rework).
#
# The classifier above is only correct if something on the terminal-reason
# path calls it. ``process_launcher._provider_auth_failure_from_output`` is that
# call site: the finalizer reads it from the worker's stdout and its ``reason``
# becomes the RECORDED blocker reason (``error`` ->
# ``task_engine.mark_launch_failed(reason=error)``). These tests drive that
# production function against constructed provider output rather than calling the
# classifier directly, so they fail if the wiring is removed and the twenty-six
# ``worker_failed:exit_code=1`` rows return.
# ---------------------------------------------------------------------------


def _write_events(base: Path, *events: dict[str, object]) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    path = base / "worker.stdout.log"
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    return path


def test_production_quota_refusal_recorded_reason_names_quota_not_worker_failed(
    tmp_path: Path,
) -> None:
    """NF-2026-00275: a quota refusal on the production path must name quota.

    A provider-owned ``api_error`` result carrying a 429 quota code is what the
    finalizer sees; the reason it records must be derived from that body and
    name quota, never the worker or a bare authentication guess.
    """
    path = _write_events(
        tmp_path,
        {
            "type": "result",
            "is_error": True,
            "api_error_status": 429,
            "terminal_reason": "api_error",
            "error": "insufficient_quota",
            # Model-authored prose that must never reach the classifier.
            "result": "secret-bearing provider text must not persist",
        },
    )

    launch_failure = process_launcher._provider_auth_failure_from_output(path)

    assert launch_failure is not None
    recorded_reason = launch_failure["reason"]
    # The recorded blocker reason names quota, derived from the body...
    assert "quota_exhausted" in recorded_reason
    assert launch_failure["refusal_kind"] == runtime_adapters.REFUSAL_QUOTA_EXHAUSTED
    assert launch_failure["recoverable"] is True
    # ...and is emphatically not the pre-wiring worker_failed / auth guess.
    assert "worker_failed" not in recorded_reason
    assert recorded_reason != "provider_authentication_failed:http_status=429"
    # The provider's prose was not carried into the durable reason.
    assert "secret-bearing" not in recorded_reason


def test_production_distinguishable_and_indistinguishable_401_record_different_reasons(
    tmp_path: Path,
) -> None:
    """NF-2026-00326 on the production path: the body, not 401, decides.

    A 401 whose body names an invalid key records a credential rejection; a 401
    whose body names no cause records ``cause_not_distinguished`` -- and the two
    RECORDED reasons must differ, proving the status code alone did not decide.
    """
    named = process_launcher._provider_auth_failure_from_output(
        _write_events(
            tmp_path / "named",
            {
                "type": "result",
                "is_error": True,
                "api_error_status": 401,
                "terminal_reason": "api_error",
                "error": "invalid_api_key",
            },
        )
    )
    bare = process_launcher._provider_auth_failure_from_output(
        _write_events(
            tmp_path / "bare",
            {
                "type": "result",
                "is_error": True,
                "api_error_status": 401,
                "terminal_reason": "api_error",
            },
        )
    )

    assert named is not None and bare is not None
    assert named["http_status"] == bare["http_status"] == 401
    assert named["refusal_kind"] == runtime_adapters.REFUSAL_CREDENTIAL_REJECTED
    assert bare["refusal_kind"] == runtime_adapters.REFUSAL_CAUSE_NOT_DISTINGUISHED
    # Same status, different RECORDED reasons -- the wiring carried the body.
    assert named["reason"] != bare["reason"]
    assert "credential_rejected" in named["reason"]
    assert bare["reason"] == (
        "provider_refused:http_status=401:cause_not_distinguished_by_response"
    )


def test_production_genuine_crash_is_not_reclassified_as_a_provider_refusal(
    tmp_path: Path,
) -> None:
    """A real worker crash carries no provider-owned refusal event, so the
    finalizer returns None and the terminal reason stays worker_failed -- the
    quota wiring is a targeted classification, not a blanket relabel."""
    path = _write_events(
        tmp_path,
        {"type": "assistant", "message": {"text": "working"}},
        {"type": "result", "is_error": True, "subtype": "error",
         "result": "Traceback ... ZeroDivisionError: division by zero"},
    )

    assert process_launcher._provider_auth_failure_from_output(path) is None


# ---------------------------------------------------------------------------
# Balance / HTTP 402 regressions (NF-2026-00275 rework).
#
# The detector forwards machine error codes (``insufficient_balance``) and the
# status 402, so the classifier is exercised on those exact forms -- not prose.
# A dead balance is PAYMENT REQUIRED: it recovers by adding credit, never by
# waiting, so it must classify as balance (not quota, not rate) and must NOT be
# recoverable.  Each line below was measured failing on the predecessor
# candidate: a bare 402 returned ``worker_failed``, and ``429 insufficient_balance``
# returned a recoverable ``rate_limited`` -- telling the operator to wait for a
# condition that never clears.
# ---------------------------------------------------------------------------


def test_bare_402_is_a_refusal_not_a_worker_failure() -> None:
    """PAYMENT REQUIRED with no body detail is still a provider refusal.

    The detector forwards ``http_status=402`` with an empty machine code; the
    classifier must name it a refusal, never collapse it into ``worker_failed``
    (the predecessor returned ``worker_failed`` here because 402 had no branch).
    """
    outcome = runtime_adapters.classify_provider_outcome(
        exit_code=1, message="http_status=402 "
    )
    assert outcome["outcome"] == runtime_adapters.OUTCOME_PROVIDER_REFUSED
    assert outcome["outcome"] != runtime_adapters.OUTCOME_WORKER_FAILED
    assert outcome["refusal_kind"] == runtime_adapters.REFUSAL_BALANCE_EXHAUSTED
    assert outcome["status_code"] == 402
    # A dead balance never recovers by waiting.
    assert outcome["recoverable"] is False
    assert "worker_failed" not in outcome["reason"]


def test_402_insufficient_balance_machine_code_names_balance() -> None:
    """The ``insufficient_balance`` machine code (underscored) names balance.

    It must match the balance vocabulary despite being a machine code rather
    than the space-form prose ``insufficient balance``.
    """
    outcome = runtime_adapters.classify_provider_outcome(
        exit_code=1, message="http_status=402 insufficient_balance"
    )
    assert outcome["refusal_kind"] == runtime_adapters.REFUSAL_BALANCE_EXHAUSTED
    # Not quota-exhausted-by-usage and not rate limiting -- distinct remedies.
    assert outcome["refusal_kind"] != runtime_adapters.REFUSAL_QUOTA_EXHAUSTED
    assert outcome["refusal_kind"] != runtime_adapters.REFUSAL_RATE_LIMITED
    assert outcome["recoverable"] is False
    assert "balance_exhausted" in outcome["reason"]


def test_body_naming_balance_beats_a_429_status_and_is_not_recoverable() -> None:
    """A balance body at status 429 classifies as balance, NOT recoverable rate.

    The worst measured outcome: ``429 insufficient_balance`` returned
    ``rate_limited`` / recoverable, telling any retry logic reading
    ``recoverable`` that waiting would help.  It will not -- the account needs
    credit.  The body is more specific than the status, so balance wins.
    """
    outcome = runtime_adapters.classify_provider_outcome(
        exit_code=1, message="http_status=429 insufficient_balance"
    )
    assert outcome["refusal_kind"] == runtime_adapters.REFUSAL_BALANCE_EXHAUSTED
    assert outcome["refusal_kind"] != runtime_adapters.REFUSAL_RATE_LIMITED
    # A dead balance reported as recoverable is a more expensive wrong answer
    # than saying nothing: waiting never clears it.
    assert outcome["recoverable"] is False


def test_a_genuine_429_without_a_balance_body_stays_recoverable_rate_limited() -> None:
    """A bare 429 with no balance token is still a recoverable rate limit.

    Balance and rate must not collapse into each other in either direction: the
    body decides, and a 429 that names no balance condition remains the
    recoverable rate-limit case it always was.
    """
    outcome = runtime_adapters.classify_provider_outcome(
        exit_code=1, message="http_status=429 rate_limit_exceeded"
    )
    assert outcome["refusal_kind"] == runtime_adapters.REFUSAL_RATE_LIMITED
    assert outcome["refusal_kind"] != runtime_adapters.REFUSAL_BALANCE_EXHAUSTED
    assert outcome["recoverable"] is True


def test_production_balance_refusal_recorded_reason_names_balance_not_worker_failed(
    tmp_path: Path,
) -> None:
    """NF-2026-00275: a 402/insufficient_balance refusal on the production path.

    The finalizer sees a provider-owned ``api_error`` carrying 402 and
    ``insufficient_balance``; the RECORDED blocker reason must name balance,
    must not be recoverable, and must never be ``worker_failed``.
    """
    path = _write_events(
        tmp_path,
        {
            "type": "result",
            "is_error": True,
            "api_error_status": 402,
            "terminal_reason": "api_error",
            "error": "insufficient_balance",
        },
    )

    launch_failure = process_launcher._provider_auth_failure_from_output(path)

    assert launch_failure is not None
    assert launch_failure["http_status"] == 402
    assert launch_failure["refusal_kind"] == runtime_adapters.REFUSAL_BALANCE_EXHAUSTED
    assert launch_failure["recoverable"] is False
    assert "balance_exhausted" in launch_failure["reason"]
    assert "worker_failed" not in launch_failure["reason"]


# ---------------------------------------------------------------------------
# The "...exhausted" balance phrasings (NF-2026-00275 rework round four).
#
# A bare ``exhausted`` token once lived in the quota vocabulary, so a body that
# literally said ``balance_exhausted`` / ``credits_exhausted`` fell past the
# balance branch and was caught as a RECOVERABLE quota wait -- the same "wait,
# it will clear" wrong answer, reached through a different door.  Quota, balance,
# credits and sessions can all be "exhausted", so the bare token named nothing;
# it is gone, and the balance vocabulary now carries the ``exhausted`` forms
# that DO name a dead account.  These lines were measured wrong on the prior
# candidate.
# ---------------------------------------------------------------------------


def test_402_balance_exhausted_body_is_balance_not_a_recoverable_quota_wait() -> None:
    """``http_status=402 balance_exhausted`` is a dead account, never a wait."""
    outcome = runtime_adapters.classify_provider_outcome(
        exit_code=1, message="http_status=402 balance_exhausted"
    )
    assert outcome["refusal_kind"] == runtime_adapters.REFUSAL_BALANCE_EXHAUSTED
    # NOT quota-exhausted (which would read as recoverable) and NOT rate limited.
    assert outcome["refusal_kind"] != runtime_adapters.REFUSAL_QUOTA_EXHAUSTED
    assert outcome["refusal_kind"] != runtime_adapters.REFUSAL_RATE_LIMITED
    assert outcome["recoverable"] is False
    assert "balance_exhausted" in outcome["reason"]


def test_credits_exhausted_body_is_balance_at_both_402_and_429() -> None:
    """``credits_exhausted`` names a dead account whatever the status.

    At 429 the token-move alone must carry it to balance (the 402 invariant does
    not apply); at 402 both agree.  Neither is a recoverable quota wait.
    """
    for body in (
        "http_status=429 credits_exhausted",
        "http_status=402 credits_exhausted",
    ):
        outcome = runtime_adapters.classify_provider_outcome(exit_code=1, message=body)
        assert outcome["refusal_kind"] == runtime_adapters.REFUSAL_BALANCE_EXHAUSTED, body
        assert outcome["refusal_kind"] != runtime_adapters.REFUSAL_QUOTA_EXHAUSTED, body
        assert outcome["recoverable"] is False, body


def test_a_402_is_never_recoverable_whatever_the_body_says() -> None:
    """Invariant: HTTP 402 PAYMENT REQUIRED can never produce a recoverable wait.

    402 clears only when someone adds credit, never when time passes.  The
    guarantee holds across EVERY body -- including an empty one and one whose
    token would otherwise name a recoverable kind (quota, rate, a retry window) --
    so a future token can never again pull a 402 into a recoverable verdict.  The
    invariant is enforced at the status level and does not depend on token
    membership, which has been wrong about this twice (NF-2026-00275 rework
    round four).
    """
    bodies = [
        "http_status=402 ",  # bare PAYMENT REQUIRED, empty of cause detail
        "http_status=402 balance_exhausted",
        "http_status=402 credits_exhausted",
        "http_status=402 insufficient_balance",
        "http_status=402 quota exceeded",  # a token that IS recoverable at 429...
        "http_status=402 quota_exhausted",
        "http_status=402 rate limit",  # ...and another
        "http_status=402 too many requests; retry-after: 30",
    ]
    for body in bodies:
        outcome = runtime_adapters.classify_provider_outcome(exit_code=1, message=body)
        assert outcome["status_code"] == 402, body
        assert outcome["recoverable"] is False, body


# ---------------------------------------------------------------------------
# The detector's status-less auth match (NF-2026-00275 rework: invariant 1 on
# the production path).
#
# The detector's ``structured_auth_error`` set accepts {authentication_failed,
# unauthorized, invalid_api_key}, so an ``api_error`` event carrying one of those
# codes with NO ``api_error_status`` still matches as a provider refusal -- the
# detector established a refusal BECAUSE the event named an auth cause.  But the
# classifier deliberately excludes bare ``unauthorized``/``authentication_failed``
# (they accompany every 401 and name nothing), and with http_status=0 there is no
# status fallback either, so the classifier returns ``worker_failed``.  The bug
# this rework closes: the detector then RETURNED that ``worker_failed`` verdict,
# recording a provider auth refusal -- a dead credential, where the operator must
# re-authenticate -- as a worker crash whose own reason says no refusal signal was
# present.  A matched refusal the classifier cannot name is ``cause_not_distinguished``,
# never ``worker_failed``.
# ---------------------------------------------------------------------------


def test_production_statusless_auth_refusal_is_cause_not_distinguished_not_worker_failed(
    tmp_path: Path,
) -> None:
    """A status-less ``unauthorized`` the detector matched must not be worker_failed.

    Measured hole: the event names an auth cause (so the detector forwards it)
    but carries no HTTP status, so the classifier cannot name WHICH cause. The
    recorded reason must say the cause was not distinguished -- not
    ``worker_process_failure_no_provider_refusal_signal``, whose own words deny
    the refusal the detector just established.
    """
    for code in ("unauthorized", "authentication_failed"):
        path = _write_events(
            tmp_path / code,
            {
                "type": "result",
                "is_error": True,
                "terminal_reason": "api_error",
                "error": code,
            },
        )

        launch_failure = process_launcher._provider_auth_failure_from_output(path)

        assert launch_failure is not None, code
        recorded_reason = launch_failure["reason"]
        # The detector established a refusal; it must never be recorded as a crash.
        assert "worker_failed" not in recorded_reason, code
        assert "no_provider_refusal_signal" not in recorded_reason, code
        assert (
            launch_failure["refusal_kind"]
            == runtime_adapters.REFUSAL_CAUSE_NOT_DISTINGUISHED
        ), code
        assert recorded_reason == (
            "provider_refused:http_status=0:cause_not_distinguished_by_response"
        ), code
        # No cause was named, so no free retry is implied.
        assert launch_failure["recoverable"] is False, code


def test_production_statusless_invalid_key_is_distinguished_from_bare_auth(
    tmp_path: Path,
) -> None:
    """A distinguishable status-less body and an indistinguishable one must differ.

    ``invalid_api_key`` names a concrete credential defect the classifier CAN
    name, so even without a status it records ``credential_rejected``; bare
    ``unauthorized`` names nothing and records ``cause_not_distinguished``. If the
    two collapsed onto one reason the detector's auth match would be proving
    nothing -- so the recorded reasons must differ (NF-2026-00326 parallel on the
    status-less production path).
    """
    named = process_launcher._provider_auth_failure_from_output(
        _write_events(
            tmp_path / "named",
            {
                "type": "result",
                "is_error": True,
                "terminal_reason": "api_error",
                "error": "invalid_api_key",
            },
        )
    )
    bare = process_launcher._provider_auth_failure_from_output(
        _write_events(
            tmp_path / "bare",
            {
                "type": "result",
                "is_error": True,
                "terminal_reason": "api_error",
                "error": "unauthorized",
            },
        )
    )

    assert named is not None and bare is not None
    # Both carry no HTTP status -- the body alone decided, not a status code.
    assert named["http_status"] == bare["http_status"] == 0
    assert named["refusal_kind"] == runtime_adapters.REFUSAL_CREDENTIAL_REJECTED
    assert bare["refusal_kind"] == runtime_adapters.REFUSAL_CAUSE_NOT_DISTINGUISHED
    assert named["reason"] != bare["reason"]
    assert "credential_rejected" in named["reason"]
    assert "cause_not_distinguished_by_response" in bare["reason"]
    # Neither is the pre-fix worker crash verdict.
    assert "worker_failed" not in named["reason"]
    assert "worker_failed" not in bare["reason"]
