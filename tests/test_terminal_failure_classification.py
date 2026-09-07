from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from aiworkhub.terminal_failure_classification import (
    MAX_DIAGNOSTIC_CHARS,
    MAX_TAIL_READ_BYTES,
    _CONTROL_PLANE_REASONS,
    _PROVIDER_REFUSAL_REASONS,
    _REASON_CONSTANTS,
    TerminalReason,
    _control_plane_code,
    classify_terminal_failure,
    classify_terminal_failure_from_paths,
    constant,
    normalize_exit_code,
    recognised_reason,
    safe_error_text,
    supervisor_failure_reason,
    supervisor_incomplete_reason,
    terminal_event_authority,
    workspace_error_reason,
)

# Any diagnostic must be exactly `<failure_kind>:<code>` optionally followed
# by `:http_status=NNN` and/or `:exit_code=N` -- never anything else. This
# shape excludes braces, quotes, and whitespace by construction, so no secret
# representation (labelled, quoted JSON, Python repr, or an unknown shape
# entirely) can pass it regardless of what the caller-supplied text contained.
_ALLOWLISTED_DIAGNOSTIC = re.compile(
    r"^[a-z_]+:[a-z_]+(?::http_status=\d{3})?(?::exit_code=-?\d+)?$"
)

_SECRET_PAYLOADS = [
    "api_key=sk-ABCDEFGHIJKLMNOP123456",
    "Authorization: Bearer abcd1234efgh5678ijkl",
    "Authorization: Basic dXNlcjpwYXNzd29yZA==",
    "private_key=abcdEFGH12345678",
    '{"password": "hunter2plain"}',
    "{'authorization': 'Bearer abcXYZ789secret'}",
    "aws_secret_access_key=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
    "X-Totally-Unknown-Secret-Shape ~~~hunter2plain~~~",
    "-----BEGIN RSA PRIVATE KEY-----\nSECRETKEYMATERIAL\n-----END RSA PRIVATE KEY-----",
]


def test_worker_failed_persists_stable_failure_kind_and_bounded_diagnostic() -> None:
    result = classify_terminal_failure(
        state="worker_failed",
        exit_code=1,
        error="vscode_lm_edit_response_stale_hash:src/app.py",
    )
    assert result["failure_kind"] == "worker_failed"
    assert _ALLOWLISTED_DIAGNOSTIC.match(result["diagnostic"])
    assert len(result["diagnostic"]) <= MAX_DIAGNOSTIC_CHARS


def test_timed_out_persists_timeout_stall() -> None:
    result = classify_terminal_failure(state="timed_out", exit_code=None, error=None)
    assert result["failure_kind"] == "timeout_stall"


def test_liveness_stall_persists_timeout_stall() -> None:
    result = classify_terminal_failure(state="liveness_lost", exit_code=None, error="stall")
    assert result["failure_kind"] == "timeout_stall"


def test_liveness_stall_diagnostic_names_liveness_lost_from_launcher_owned_code() -> None:
    result = classify_terminal_failure(
        state="liveness_lost",
        exit_code=None,
        error="liveness_lost:heartbeat_lease_and_recovery_grace_exceeded:rc=None",
    )
    assert result["diagnostic"] == "timeout_stall:liveness_lost"


def test_cancellation_has_no_failure_verdict() -> None:
    result = classify_terminal_failure(state="cancelled", exit_code=137, error="sigterm")
    assert result["failure_kind"] is None
    assert result["diagnostic"] == ""


def test_cancelled_flag_overrides_state() -> None:
    result = classify_terminal_failure(
        state="worker_failed", exit_code=1, error="x", cancelled=True
    )
    assert result["failure_kind"] is None


def test_nonzero_exit_without_named_state_is_classified() -> None:
    result = classify_terminal_failure(state="exited", exit_code=2, error=None)
    assert result["failure_kind"] == "nonzero_exit"
    assert result["diagnostic"] == "nonzero_exit:unclassified:exit_code=2"


def test_diagnostic_is_bounded() -> None:
    long_error = "x" * 5000
    result = classify_terminal_failure(state="worker_failed", exit_code=1, error=long_error)
    assert len(result["diagnostic"]) <= MAX_DIAGNOSTIC_CHARS


def test_success_state_yields_no_failure_kind() -> None:
    result = classify_terminal_failure(state="done", exit_code=0, error=None)
    assert result["failure_kind"] is None
    assert result["diagnostic"] == ""


def test_launch_failed_with_none_exit_code_is_classified_not_success() -> None:
    """NF-2026-00622 V7 rework: a missing/malformed/stale supervisor status
    resolves exit_code to None even when the finalizer already found a stable
    provider auth-refusal reason -- that must never fall through to the
    no-failure default, which would be indistinguishable from success."""
    result = classify_terminal_failure(
        state="launch_failed",
        exit_code=None,
        error="provider_refused:http_status=401:cause_not_distinguished_by_response",
    )
    assert result["failure_kind"] == "launch_failed"
    assert result["diagnostic"] == "launch_failed:auth_cause_not_distinguished:http_status=401"


def test_launch_failed_diagnostic_falls_back_to_unclassified_when_error_missing() -> None:
    result = classify_terminal_failure(state="launch_failed", exit_code=None, error=None)
    assert result["failure_kind"] == "launch_failed"
    assert result["diagnostic"] == "launch_failed:unclassified"


def test_launch_failed_prefers_provider_tail_signal_over_generic_error() -> None:
    result = classify_terminal_failure(
        state="launch_failed",
        exit_code=None,
        error="provider_refused:http_status=401:cause_not_distinguished_by_response",
        stderr_tail="fatal: invalid credential, please re-authenticate\n",
    )
    assert result["failure_kind"] == "launch_failed"
    assert result["diagnostic"] == (
        "launch_failed:auth_invalid_credential:http_status=401"
    )


def test_classify_from_paths_reads_tail_for_launch_failed_state(tmp_path: Path) -> None:
    stdout_path = tmp_path / "out.log"
    stderr_path = tmp_path / "err.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("fatal: invalid credential\n", encoding="utf-8")

    result = classify_terminal_failure_from_paths(
        state="launch_failed",
        exit_code=None,
        error="provider_refused:http_status=401:cause_not_distinguished_by_response",
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    assert result["failure_kind"] == "launch_failed"
    assert result["diagnostic"] == "launch_failed:auth_invalid_credential:http_status=401"


def test_worker_failed_prefers_stderr_signal_over_generic_wrapper_error() -> None:
    result = classify_terminal_failure(
        state="worker_failed",
        exit_code=1,
        error="worker_failed:supervisor_state=exited:exit_code=1",
        stderr_tail="RuntimeError: missing required output artifact\n",
    )
    assert result["failure_kind"] == "worker_failed"
    assert result["diagnostic"] == "worker_failed:missing_output_artifact:exit_code=1"


def test_worker_failed_falls_back_to_stdout_tail_when_stderr_empty() -> None:
    result = classify_terminal_failure(
        state="worker_failed",
        exit_code=1,
        error="worker_failed:supervisor_state=exited:exit_code=1",
        stdout_tail="fatal: provider refused the request\n",
        stderr_tail="   ",
    )
    assert result["diagnostic"] == "worker_failed:provider_refused:exit_code=1"


def test_worker_failed_diagnostic_falls_back_to_unclassified_when_no_signal_anywhere() -> None:
    result = classify_terminal_failure(
        state="worker_failed",
        exit_code=1,
        error="worker_failed:supervisor_state=exited:exit_code=1",
        stdout_tail="",
        stderr_tail=None,
    )
    assert result["diagnostic"] == "worker_failed:unclassified:exit_code=1"


def test_classify_from_paths_reads_stderr_tail_once(tmp_path: Path) -> None:
    stdout_path = tmp_path / "out.log"
    stderr_path = tmp_path / "err.log"
    stdout_path.write_text("Starting worker...\n", encoding="utf-8")
    stderr_path.write_text("ValueError: actionable cause\n", encoding="utf-8")

    result = classify_terminal_failure_from_paths(
        state="worker_failed",
        exit_code=1,
        error="worker_failed:supervisor_state=exited:exit_code=1",
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    assert result["failure_kind"] == "worker_failed"
    assert result["diagnostic"] == "worker_failed:runtime_error:exit_code=1"


def test_classify_from_paths_skips_reads_for_a_clean_exit(tmp_path: Path) -> None:
    stdout_path = tmp_path / "out.log"
    stderr_path = tmp_path / "err.log"
    stdout_path.write_text("all good\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")

    result = classify_terminal_failure_from_paths(
        state="exited", exit_code=0, error=None,
        stdout_path=stdout_path, stderr_path=stderr_path,
    )
    assert result["failure_kind"] is None
    assert result["diagnostic"] == ""


def test_classify_from_paths_never_follows_a_symlinked_log_path(tmp_path: Path) -> None:
    """Sentinel symlink-swap regression: if a declared log path is replaced by
    a symlink to an arbitrary host file, that file's content must never be
    read into or persisted as terminal diagnostic evidence."""
    host_secret = tmp_path / "outside_host_secret.txt"
    host_secret.write_text("ARBITRARY_HOST_FILE_CONTENT\n", encoding="utf-8")
    stderr_path = tmp_path / "err.log"
    stderr_path.symlink_to(host_secret)
    stdout_path = tmp_path / "out.log"
    stdout_path.symlink_to(host_secret)

    result = classify_terminal_failure_from_paths(
        state="worker_failed",
        exit_code=1,
        error="worker_failed:supervisor_state=exited:exit_code=1",
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    assert "ARBITRARY_HOST_FILE_CONTENT" not in result["diagnostic"]
    assert result["diagnostic"] == "worker_failed:unclassified:exit_code=1"


def test_classify_from_paths_never_leaks_pem_body_straddling_the_tail_read_boundary(
    tmp_path: Path,
) -> None:
    """NF-2026-00622 V7 rework BLOCKING fix: a private key whose BEGIN marker
    falls before the classifier's bounded tail-read window (so only the body
    + END marker are inside it) must never leak raw body bytes into durable
    diagnostic evidence -- because the tail is only ever scanned for a closed
    set of signature codes and never copied, there is no window position at
    which any of its bytes can reach the result."""
    stderr_path = tmp_path / "err.log"
    body_line = "SECRETKEYMATERIAL_LINE_0123456789ABCDEF\n"
    pem_block = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        + body_line * 200
        + "-----END RSA PRIVATE KEY-----\n"
    )
    content = pem_block + "SAFE_TRAILING_TEXT\n"
    assert len(content) - MAX_TAIL_READ_BYTES > len("-----BEGIN RSA PRIVATE KEY-----\n")
    stderr_path.write_bytes(content.encode("utf-8"))

    result = classify_terminal_failure_from_paths(
        state="worker_failed",
        exit_code=1,
        error="worker_failed:supervisor_state=exited:exit_code=1",
        stdout_path=None,
        stderr_path=stderr_path,
    )
    assert "SECRETKEYMATERIAL" not in result["diagnostic"]
    assert "-----BEGIN" not in result["diagnostic"]
    assert _ALLOWLISTED_DIAGNOSTIC.match(result["diagnostic"])


def test_classify_from_paths_tolerates_missing_log_files(tmp_path: Path) -> None:
    result = classify_terminal_failure_from_paths(
        state="worker_failed",
        exit_code=1,
        error="worker_failed:supervisor_state=exited:exit_code=1",
        stdout_path=tmp_path / "missing.stdout.log",
        stderr_path=tmp_path / "missing.stderr.log",
    )
    assert result["failure_kind"] == "worker_failed"
    assert result["diagnostic"] == "worker_failed:unclassified:exit_code=1"


def test_classification_does_not_match_arbitrary_numeric_substrings() -> None:
    """Arbitrary PID/build numbers embedded in caller text must never surface
    in the diagnostic, and must never be mistaken for an unrelated numeric
    signal (like an HTTP status): classification is driven only by fixed
    signature words and the typed ``exit_code``/``http_status`` fields, so two
    inputs differing only by an arbitrary embedded number classify identically."""
    result_a = classify_terminal_failure(state="worker_failed", exit_code=1, error="pid=12345")
    result_b = classify_terminal_failure(state="worker_failed", exit_code=1, error="pid=99999")
    assert result_a["failure_kind"] == result_b["failure_kind"] == "worker_failed"
    assert result_a["diagnostic"] == result_b["diagnostic"]
    assert "12345" not in result_a["diagnostic"]
    assert "99999" not in result_b["diagnostic"]


def test_diagnostic_never_contains_any_shape_of_secret_payload() -> None:
    """Closed-allowlist regression for the blocking finding: quoted JSON,
    Python repr, bearer/basic, aws-style, PEM, and a wholly unknown secret
    shape must never appear in the diagnostic -- proven for every shape at
    once because diagnostic content can only ever be a code from the closed
    vocabulary plus numeric metadata, never a copied substring."""
    for payload in _SECRET_PAYLOADS:
        for state in ("worker_failed", "launch_failed", "nonzero_exit"):
            exit_code = 1 if state != "launch_failed" else None
            result = classify_terminal_failure(
                state=state if state != "nonzero_exit" else "exited",
                exit_code=exit_code if state != "nonzero_exit" else 2,
                error=payload,
                stdout_tail=payload,
                stderr_tail=payload,
            )
            assert payload not in result["diagnostic"]
            assert "hunter2plain" not in result["diagnostic"]
            assert "SECRETKEYMATERIAL" not in result["diagnostic"]
            assert "{" not in result["diagnostic"]
            assert "'" not in result["diagnostic"]
            assert '"' not in result["diagnostic"]
            assert _ALLOWLISTED_DIAGNOSTIC.match(result["diagnostic"]), result["diagnostic"]


def test_diagnostic_from_paths_never_contains_any_shape_of_secret_payload(
    tmp_path: Path,
) -> None:
    for index, payload in enumerate(_SECRET_PAYLOADS):
        stdout_path = tmp_path / f"out_{index}.log"
        stderr_path = tmp_path / f"err_{index}.log"
        stdout_path.write_text(payload, encoding="utf-8")
        stderr_path.write_text(payload, encoding="utf-8")

        result = classify_terminal_failure_from_paths(
            state="worker_failed",
            exit_code=1,
            error="worker_failed:supervisor_state=exited:exit_code=1",
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        assert payload not in result["diagnostic"]
        assert "hunter2plain" not in result["diagnostic"]
        assert _ALLOWLISTED_DIAGNOSTIC.match(result["diagnostic"]), result["diagnostic"]


def test_output_budget_exceeded_with_none_exit_code_is_classified_not_success() -> None:
    """NF-2026-00622 V7 rework BLOCKING fix: the supervisor never resolves an
    ``exit_code`` for an output-budget cutoff, so ``exit_code=None`` must
    never fall through to the no-failure default -- that default is
    indistinguishable from success and is exactly the retained-candidate
    correctness hole this closes."""
    result = classify_terminal_failure(
        state="output_budget_exceeded",
        exit_code=None,
        error="output_budget_exceeded:cap_bytes=1000000:observed_bytes=1200000",
    )
    assert result["failure_kind"] == "output_budget_exceeded"
    assert result["diagnostic"] == "output_budget_exceeded:unclassified"
    assert _ALLOWLISTED_DIAGNOSTIC.match(result["diagnostic"])


def test_classify_from_paths_reads_tail_for_output_budget_exceeded_state(
    tmp_path: Path,
) -> None:
    stdout_path = tmp_path / "out.log"
    stderr_path = tmp_path / "err.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("RuntimeError: missing required output artifact\n", encoding="utf-8")

    result = classify_terminal_failure_from_paths(
        state="output_budget_exceeded",
        exit_code=None,
        error="output_budget_exceeded:cap_bytes=1000000:observed_bytes=1200000",
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    assert result["failure_kind"] == "output_budget_exceeded"
    assert result["diagnostic"] == "output_budget_exceeded:missing_output_artifact"


def test_exited_without_review_with_zero_exit_code_is_classified_not_success() -> None:
    """Companion gap of the identical shape: the direct/non-isolated monitor
    only ever reaches this state with ``exit_code=0`` (a clean process exit
    that never reached review), so it too must never rely on the exit_code
    fallback to be recognized as a failure."""
    result = classify_terminal_failure(
        state="exited_without_review", exit_code=0, error=None,
    )
    assert result["failure_kind"] == "exited_without_review"
    assert result["diagnostic"] == "exited_without_review:unclassified:exit_code=0"


# NF-2026-00622 V7 rework: bounded exhaustive completeness audit. Every
# terminal ``state`` value process_launcher.py actually passes into
# ``classify_terminal_failure``/``classify_terminal_failure_from_paths`` --
# from its direct/non-isolated monitor call site and its isolated-finalizer
# call site -- is enumerated here exactly once, with a representative
# worst-case ``exit_code`` (``None`` wherever the launcher can produce that
# state without ever resolving one). This proves completeness across the
# whole vocabulary at once instead of one state at a time, which is how the
# ``output_budget_exceeded`` gap this rework fixes went unnoticed.
#
# ``validation_failed``/``finalize_failed``/``scope_rejected``/
# ``promotion_conflict`` are the isolated finalizer's own post-``exited``
# outcomes (a settled ``exit_code == 0`` clean exit followed by a failed
# review-pipeline step) -- the same exit_code=0 fallback gap, closed by the
# rework-of-rework-of-rework that also fixed the stale pre-pipeline authority.
_LAUNCHER_NON_CANCEL_NON_SUCCESS_FAILURE_STATES = (
    ("timed_out", None),
    ("liveness_lost", None),
    ("worker_failed", 1),
    ("launch_failed", None),
    ("output_budget_exceeded", None),
    ("exited_without_review", 0),
    ("exited", 2),
    ("validation_failed", 0),
    ("finalize_failed", 0),
    ("scope_rejected", 0),
    ("promotion_conflict", 0),
    # The isolated finalizer's own retry-exhaustion wrapper
    # (``_finalize_after_process_exit``) names this when the target card can
    # no longer be moved at all -- a dead end distinct from the retryable
    # ``reconcile_pending`` below (NF-2026-00622 V7 rework).
    ("finalize_abandoned", None),
)


@pytest.mark.parametrize(
    "state,exit_code", _LAUNCHER_NON_CANCEL_NON_SUCCESS_FAILURE_STATES,
)
def test_every_launcher_failure_state_yields_nonempty_safe_failure_kind_and_diagnostic(
    state: str, exit_code: int | None,
) -> None:
    result = classify_terminal_failure(state=state, exit_code=exit_code, error=None)
    assert result["failure_kind"], f"{state}: expected a nonempty failure_kind"
    assert result["diagnostic"], f"{state}: expected a nonempty diagnostic"
    assert _ALLOWLISTED_DIAGNOSTIC.match(result["diagnostic"]), result["diagnostic"]


@pytest.mark.parametrize(
    "state,exit_code",
    [("review_ready", 0), ("cancelled", 0), ("cancelled", 137)],
)
def test_review_success_and_cancellation_stay_verdict_free(
    state: str, exit_code: int,
) -> None:
    result = classify_terminal_failure(state=state, exit_code=exit_code, error=None)
    assert result["failure_kind"] is None
    assert result["diagnostic"] == ""


@pytest.mark.parametrize("exit_code", [None, 1, 137])
def test_reconcile_pending_stays_verdict_free_regardless_of_exit_code(
    exit_code: int | None,
) -> None:
    """NF-2026-00622 V7 rework: ``reconcile_pending`` is the isolated
    finalizer's own not-yet-settled retry state -- a genuinely nonzero
    ``supervisor_returncode`` carried over from the dead supervisor it is
    still trying to reconcile must never be mistaken for a settled
    ``nonzero_exit`` failure verdict while the retry itself may still
    succeed."""
    result = classify_terminal_failure(
        state="reconcile_pending", exit_code=exit_code, error="finalizer_retries_exhausted:x",
    )
    assert result["failure_kind"] is None
    assert result["diagnostic"] == ""


# NF-2026-00622 V7 rework-of-rework: security-review finding. Content
# sanitation for any durable/public error/reason/evidence surface must be
# unconditional on the failure verdict -- a cancelled or successful/review
# outcome carrying a pre-existing secret-shaped error must be exactly as
# safe as a genuine failure. This matrix exercises `safe_error_text`, the
# function process_launcher.py's real finalizer now calls for every
# terminal outcome that carries no failure_kind, across representative
# failure/cancellation/success/review state labels x every secret shape.
_TERMINAL_OUTCOME_LABEL_MATRIX = (
    ("worker_failed", 1),
    ("launch_failed", None),
    ("cancelled", 137),
    ("cancelled", 0),
    ("cancel_requested", None),
    ("exited", 0),
    ("review_ready", 0),
    ("review_pending", 0),
)


@pytest.mark.parametrize("payload", _SECRET_PAYLOADS)
@pytest.mark.parametrize("state,exit_code", _TERMINAL_OUTCOME_LABEL_MATRIX)
def test_safe_error_text_never_leaks_any_shape_of_secret_across_every_terminal_outcome(
    state: str, exit_code: int | None, payload: str,
) -> None:
    result = safe_error_text(state=state, exit_code=exit_code, error=payload)
    assert payload not in result
    assert "hunter2plain" not in result
    assert "SECRETKEYMATERIAL" not in result
    assert "{" not in result
    assert "'" not in result
    assert '"' not in result
    if result:
        assert _ALLOWLISTED_DIAGNOSTIC.match(result), result


@pytest.mark.parametrize("state,exit_code", _TERMINAL_OUTCOME_LABEL_MATRIX)
def test_safe_error_text_is_empty_when_there_is_nothing_to_sanitize(
    state: str, exit_code: int | None,
) -> None:
    assert safe_error_text(state=state, exit_code=exit_code, error=None) == ""
    assert safe_error_text(state=state, exit_code=exit_code, error="") == ""


# NF-2026-00622 V7 rework-of-rework: boundary-hardening finding. A retained
# candidate trusted supervisor_status["exit_code"] as if it were always a
# real int -- a secret-bearing string (or a bool/float/container/huge int
# adjacent to a secret shape) reached diagnostic/error formatting unchanged.
# ``exit_code`` is untrusted JSON read back from an external supervisor
# process, so every non-int, bool, and out-of-range shape must normalize to
# ``None`` rather than ever being formatted as-is.
_INVALID_EXIT_CODE_SHAPES = (
    "Authorization: Bearer abcd1234efgh5678ijkl",
    '{"password": "hunter2plain"}',
    True,
    False,
    1.5,
    -1.0,
    {"authorization": "Bearer abcXYZ789secret"},
    ["Authorization: Bearer abcd1234efgh5678ijkl"],
    10**20,
    -(10**20),
)

_VALID_EXIT_CODES = (0, 1, -1, 137, 255, -255, 2**31 - 1, -(2**31 - 1))


@pytest.mark.parametrize("value", _INVALID_EXIT_CODE_SHAPES)
def test_normalize_exit_code_rejects_every_non_bounded_int_shape(value: object) -> None:
    assert normalize_exit_code(value) is None


@pytest.mark.parametrize("value", _VALID_EXIT_CODES)
def test_normalize_exit_code_accepts_every_valid_bounded_int(value: int) -> None:
    assert normalize_exit_code(value) == value


def test_normalize_exit_code_treats_none_as_none() -> None:
    assert normalize_exit_code(None) is None


@pytest.mark.parametrize("bad_exit_code", _INVALID_EXIT_CODE_SHAPES)
def test_classify_terminal_failure_never_formats_an_invalid_exit_code_shape(
    bad_exit_code: object,
) -> None:
    """Every invalid ``exit_code`` shape -- string, bool, float, dict/list, or
    a huge int -- must normalize identically to ``None`` and therefore never
    appear (in any form) in the diagnostic, regardless of what the shape
    itself carries."""
    result = classify_terminal_failure(state="worker_failed", exit_code=bad_exit_code, error=None)
    assert result["failure_kind"] == "worker_failed"
    assert result["diagnostic"] == "worker_failed:unclassified"
    assert _ALLOWLISTED_DIAGNOSTIC.match(result["diagnostic"])
    assert "exit_code=" not in result["diagnostic"]


@pytest.mark.parametrize("valid_exit_code", _VALID_EXIT_CODES)
def test_classify_terminal_failure_keeps_correct_semantics_for_valid_exit_codes(
    valid_exit_code: int,
) -> None:
    result = classify_terminal_failure(state="worker_failed", exit_code=valid_exit_code, error=None)
    assert result["failure_kind"] == "worker_failed"
    assert result["diagnostic"] == f"worker_failed:unclassified:exit_code={valid_exit_code}"


@pytest.mark.parametrize("bad_exit_code", _INVALID_EXIT_CODE_SHAPES)
def test_classify_from_paths_never_formats_an_invalid_exit_code_shape(
    bad_exit_code: object, tmp_path: Path,
) -> None:
    result = classify_terminal_failure_from_paths(
        state="worker_failed",
        exit_code=bad_exit_code,
        error=None,
        stdout_path=tmp_path / "missing.stdout.log",
        stderr_path=tmp_path / "missing.stderr.log",
    )
    assert result["failure_kind"] == "worker_failed"
    assert result["diagnostic"] == "worker_failed:unclassified"


@pytest.mark.parametrize("bad_exit_code", _INVALID_EXIT_CODE_SHAPES)
def test_safe_error_text_never_formats_an_invalid_exit_code_shape(bad_exit_code: object) -> None:
    result = safe_error_text(state="review_ready", exit_code=bad_exit_code, error="benign")
    assert "exit_code=" not in result
    assert _ALLOWLISTED_DIAGNOSTIC.match(result)


def test_safe_error_text_does_not_change_the_classify_terminal_failure_verdict() -> None:
    """Sanitation is an orthogonal, additional surface -- it must never be
    mistaken for, or substitute, the failure_kind/diagnostic verdict."""
    secret = "Authorization: Bearer abcd1234efgh5678ijkl"
    for state, exit_code in (("cancelled", 137), ("exited", 0), ("review_ready", 0)):
        verdict = classify_terminal_failure(state=state, exit_code=exit_code, error=secret)
        assert verdict["failure_kind"] is None
        assert verdict["diagnostic"] == ""
        assert secret not in safe_error_text(state=state, exit_code=exit_code, error=secret)
    for state, exit_code in (("worker_failed", 1), ("launch_failed", None)):
        verdict = classify_terminal_failure(state=state, exit_code=exit_code, error=secret)
        assert verdict["failure_kind"] is not None


# NF-2026-00622 V7 rework: ``terminal_event_authority`` is the one function
# every terminal-state append site in process_launcher.py must route through
# instead of hand-building failure_kind/diagnostic/error -- these tests pin
# its exact contract independent of any particular call site.
def test_terminal_event_authority_combines_verdict_and_safe_error_for_a_failure() -> None:
    result = terminal_event_authority(
        state="finalize_failed", exit_code=None, error="metadata_invalid:something_broke",
    )
    assert result["failure_kind"] == "finalize_failed"
    # STRENGTHENED, not relaxed. ``metadata_invalid`` is a constant this
    # repository mints -- process_launcher's metadata-parse early return -- and
    # it is now a named control-plane reason, so this no longer records
    # ``unclassified``/``runtime_error`` ("no idea") for a refusal that states
    # exactly what happened. The contract this test exists for is unchanged and
    # is the line below: ``error`` is the diagnostic on the untyped channel.
    assert result["diagnostic"] == "finalize_failed:metadata_invalid"
    assert result["error"] == result["diagnostic"]


def test_terminal_event_authority_sanitizes_error_when_no_failure_verdict() -> None:
    secret = "Authorization: Bearer abcd1234efgh5678ijkl"
    result = terminal_event_authority(state="cancelled", exit_code=137, error=secret)
    assert result["failure_kind"] is None
    assert result["diagnostic"] == ""
    assert secret not in result["error"]


def test_terminal_event_authority_never_persists_a_secret_shaped_metadata_error() -> None:
    """The exact bypass shape the flagged finding closed: a metadata-parse
    early return's raw ``metadata_invalid:{exc}`` string must never itself
    become durable -- only a code from the closed vocabulary can."""
    secret_exc = "Authorization: Bearer abcd1234efgh5678ijkl {'password': 'hunter2plain'}"
    result = terminal_event_authority(
        state="finalize_failed", exit_code=None, error=f"metadata_invalid:{secret_exc}",
    )
    assert result["failure_kind"] == "finalize_failed"
    assert "hunter2plain" not in result["error"]
    assert "Bearer" not in result["error"]
    assert _ALLOWLISTED_DIAGNOSTIC.match(result["error"])


def test_terminal_event_authority_reads_log_tails_from_paths(tmp_path: Path) -> None:
    stderr_path = tmp_path / "err.log"
    stderr_path.write_text("RuntimeError: missing required output artifact\n", encoding="utf-8")
    result = terminal_event_authority(
        state="worker_failed",
        exit_code=1,
        error="worker_failed:supervisor_state=exited:exit_code=1",
        stdout_path=None,
        stderr_path=stderr_path,
    )
    assert result["diagnostic"] == "worker_failed:missing_output_artifact:exit_code=1"
    assert result["error"] == result["diagnostic"]


def test_terminal_event_authority_names_finalize_abandoned() -> None:
    result = terminal_event_authority(
        state="finalize_abandoned",
        exit_code=None,
        error="finalizer_retries_exhausted:x:finalize_abandoned:task_archived:y",
    )
    assert result["failure_kind"] == "finalize_abandoned"
    # ``task_archived`` is NOT a real control-plane reason token, so it is
    # correctly not recognised and the verdict falls back to the outermost
    # token that IS one -- the ``finalizer_retries_exhausted`` wrapper
    # process_launcher.py:10345 mints. Naming the wrapper is the weakest true
    # statement available here, which is exactly what should be recorded when
    # the cause it wraps is unknown to the vocabulary.
    assert result["diagnostic"] == "finalize_abandoned:finalizer_retries_exhausted"
    assert "task_archived" not in result["diagnostic"]
    assert "task_archived" not in result["error"]


# --------------------------------------------------------------------------- #
# The control-plane reason split.
#
# Sanitation defends one thing: no byte of caller-supplied text -- provider
# stdout/stderr, ``str(exc)``, a supervisor-status field -- may become durable
# diagnostic content, because a secret can hide in any shape a redaction regex
# did not anticipate. That defence is total and is unchanged by these tests.
#
# It was, however, being applied to two different classes of string at once.
# A DETERMINISTIC CONTROL-PLANE REASON (``not_processing``,
# ``request_identity_missing``, ...) is not caller text: it is a constant this
# repository mints to say why its own state transition was refused, and it IS
# the diagnosis. Because the finalizer wraps it around exception text before
# calling here, the generic ``traceback|exception|error|fatal`` heuristic fired
# on the wrapper and every one of these events was durably recorded as
# ``runtime_error`` -- the classifier destroying the only precise reason it
# had, which is the "terminal transition with no recorded reason" class three
# audits of this repository named as the top observability defect.
#
# The split is a CLOSED ALLOWLIST, never a "looks safe" heuristic: a reason can
# only be recognised, never synthesised, and anything unrecognised is sanitised
# exactly as before.
# --------------------------------------------------------------------------- #

# The production shape from process_launcher._finalize_after_process_exit:
# ``error + ":finalize_abandoned:" + abandon_cause + ":" + transition_reason``,
# where ``error`` is ``"finalizer_retries_exhausted:" + "|".join(errors)`` and
# each entry is ``f"attempt={n}:{type(exc).__name__}:{exc}"`` -- i.e. a real
# control-plane reason carried inside untrusted exception text.
def _finalizer_wrapper(cause: str, *, exception_text: str = "no_terminal_event") -> str:
    attempts = "|".join(f"attempt={n}:RuntimeError:{exception_text}" for n in (1, 2, 3))
    return (
        f"finalizer_retries_exhausted:{attempts}"
        f":finalize_abandoned:{cause}:{cause}:current=archived"
    )


def test_control_plane_reason_survives_the_real_finalizer_wrapper() -> None:
    """The measured regression: ``not_processing`` came back as
    ``runtime_error`` because the wrapper's ``RuntimeError`` text tripped the
    generic provider heuristic first."""
    result = terminal_event_authority(
        state="finalize_abandoned",
        exit_code=None,
        error=_finalizer_wrapper("not_processing"),
    )
    assert result["failure_kind"] == "finalize_abandoned"
    assert result["diagnostic"] == "finalize_abandoned:not_processing"
    assert result["error"] == "finalize_abandoned:not_processing"
    assert "runtime_error" not in result["error"]


@pytest.mark.parametrize("reason", _CONTROL_PLANE_REASONS)
def test_every_control_plane_reason_survives_the_wrapper(reason: str) -> None:
    """Parametrized off the vocabulary itself, so a reason added later cannot
    silently regress to ``runtime_error``."""
    result = terminal_event_authority(
        state="finalize_abandoned", exit_code=None, error=_finalizer_wrapper(reason),
    )
    assert result["diagnostic"] == f"finalize_abandoned:{reason}"
    assert _ALLOWLISTED_DIAGNOSTIC.match(result["diagnostic"]), result["diagnostic"]


def test_control_plane_reason_survives_on_the_verdict_free_branch_too() -> None:
    """``reconcile_pending`` carries no failure verdict, so its error goes
    through ``safe_error_text`` instead -- the reason must survive there as
    well, or the retryable half of the finalizer stays undiagnosable."""
    result = terminal_event_authority(
        state="reconcile_pending",
        exit_code=None,
        error=(
            "finalizer_retries_exhausted:attempt=1:RuntimeError:no_terminal_event"
            ":terminal_transition_failed:request_identity_missing"
        ),
    )
    assert result["failure_kind"] is None
    assert result["diagnostic"] == ""
    assert result["error"] == "reconcile_pending:request_identity_missing"


def test_control_plane_code_is_the_module_constant_never_a_slice_of_the_input() -> None:
    """The no-copy invariant, asserted by object identity: recognising a reason
    returns the vocabulary's own constant, so not one caller-supplied byte can
    reach a durable diagnostic through this path."""
    code = _control_plane_code("noise:not_processing:current=archived")
    assert code == "not_processing"
    assert any(code is reason for reason in _CONTROL_PLANE_REASONS)


# --------------------------------------------------------------------------- #
# Fail-closed: everything that is NOT a recognised control-plane reason is
# sanitised exactly as it was before.
# --------------------------------------------------------------------------- #

def test_provider_log_tails_are_never_trusted_for_control_plane_vocabulary() -> None:
    """Only the AIWorkHub-minted ``error`` is control-plane-bearing. A worker
    that prints a reason token to its own stderr must not be able to mint a
    control-plane diagnosis for its terminal event."""
    result = classify_terminal_failure(
        state="worker_failed",
        exit_code=1,
        error="worker_failed:supervisor_state=exited:exit_code=1",
        stderr_tail="not_processing\n",
        stdout_tail="request_identity_missing\n",
    )
    assert result["diagnostic"] == "worker_failed:unclassified:exit_code=1"
    assert "not_processing" not in result["diagnostic"]
    assert "request_identity_missing" not in result["diagnostic"]


@pytest.mark.parametrize(
    "unknown",
    [
        "card_vanished_mysteriously",
        "not_processing_soon",           # near-miss: longer word, must not match
        "xnot_processing",               # near-miss: no left token boundary
        "NOT_PROCESSING",                # the vocabulary is case-exact
        "some_reason_nobody_declared",
    ],
)
def test_an_unrecognised_reason_shaped_string_is_still_sanitised(unknown: str) -> None:
    """An allowlist admits only what it names. Anything else -- including a
    string that merely looks like a control-plane reason -- is discarded, never
    echoed.

    The wrapper this is fed is the real production one, so once
    ``finalizer_retries_exhausted`` joined the vocabulary the outer wrapper --
    a genuine AIWorkHub constant -- is legitimately recognised and the verdict
    falls back to it rather than to ``runtime_error``. That is a strictly
    better diagnosis and it changes nothing this test defends: the unknown
    cause is still refused by the allowlist, still absent from the output, and
    the output is still nothing but vocabulary constants in the strict shape.
    ``test_a_wrapperless_unrecognised_string_still_falls_through_to_the_heuristics``
    below keeps the fall-through-to-``runtime_error`` path itself covered.
    """
    assert _control_plane_code(unknown) is None
    result = terminal_event_authority(
        state="finalize_abandoned", exit_code=None, error=_finalizer_wrapper(unknown),
    )
    assert unknown not in result["error"]
    assert unknown.lower() not in result["error"]
    assert result["diagnostic"] == "finalize_abandoned:finalizer_retries_exhausted"
    assert _ALLOWLISTED_DIAGNOSTIC.match(result["diagnostic"]), result["diagnostic"]


def test_a_wrapperless_unrecognised_string_still_falls_through_to_the_heuristics() -> None:
    """The fall-through the parametrized test above no longer exercises: with
    no recognised control-plane token anywhere in it, an AIWorkHub-shaped but
    unknown error is still reduced by the provider heuristics alone."""
    result = terminal_event_authority(
        state="finalize_abandoned",
        exit_code=None,
        error="attempt=1:RuntimeError:card_vanished_mysteriously",
    )
    assert result["diagnostic"] == "finalize_abandoned:runtime_error"
    assert "card_vanished_mysteriously" not in result["error"]


def test_provider_and_exception_text_is_still_reduced_to_a_signature_code() -> None:
    """Untrusted text keeps its old treatment exactly: inspected transiently,
    reduced to one code word, never copied."""
    result = terminal_event_authority(
        state="worker_failed",
        exit_code=1,
        error="worker_failed:supervisor_state=exited:exit_code=1",
        stdout_path=None,
        stderr_path=None,
    )
    assert result["diagnostic"] == "worker_failed:unclassified:exit_code=1"
    exploded = classify_terminal_failure(
        state="worker_failed",
        exit_code=1,
        error="attempt=1:ValueError:the provider refused the request outright",
    )
    assert exploded["diagnostic"] == "worker_failed:provider_refused:exit_code=1"
    assert "outright" not in exploded["diagnostic"]


@pytest.mark.parametrize("payload", _SECRET_PAYLOADS)
@pytest.mark.parametrize("reason", _CONTROL_PLANE_REASONS)
def test_a_control_plane_reason_never_carries_a_secret_out_with_it(
    reason: str, payload: str,
) -> None:
    """The split must not become a smuggling route: a recognised reason wrapped
    around every secret shape still emits only vocabulary constants."""
    error = _finalizer_wrapper(reason, exception_text=payload)
    for result in (
        terminal_event_authority(state="finalize_abandoned", exit_code=None, error=error),
        terminal_event_authority(state="cancelled", exit_code=137, error=error),
        {"error": safe_error_text(state="review_ready", exit_code=0, error=error)},
    ):
        text = result["error"]
        assert payload not in text
        assert "hunter2plain" not in text
        assert "SECRETKEYMATERIAL" not in text
        assert "Bearer" not in text
        assert "{" not in text and "'" not in text and '"' not in text
        assert _ALLOWLISTED_DIAGNOSTIC.match(text), text


# --------------------------------------------------------------------------- #
# Mandatory-output validation evidence.
#
# ``validate_required_outputs`` (worker_workspace.py:5541-5552, and the
# identical fallback at process_launcher.py:298-309) raises
# ``"required_output_mismatch:" + json.dumps(diagnostics)``. That JSON is the
# most reworkable diagnosis the system produces -- it names which declared
# outputs a worker failed to change -- and it was being recorded as
# ``runtime_error``, because its own key ``legacy_error_codes`` contains the
# substring ``error`` and tripped ``_SIGNATURES``' catch-all.
#
# THE SPLIT, AND WHY IT IS WHERE IT IS. The diagnosis has two halves:
#
#   * the REASON (``required_output_unchanged``, ``..._zero_bytes``, ...) is a
#     constant this repository mints. It is admitted to the closed vocabulary
#     and now survives, in ``error``, in the strict constant shape.
#
#   * the PATHS are data, not vocabulary. They are not admitted, and ``error``
#     keeps its constant shape. A repo-relative path is machine-generated, but
#     it is generated by enumerating a worktree with a card-declared glob, so
#     when the card declares a pattern rather than a literal the filename is
#     WORKER-CHOSEN -- ``out/AKIAIOSFODNN7EXAMPLE.json`` is a legal filename.
#     A character grammar cannot separate the two: every secret in
#     ``_SECRET_PAYLOADS`` that matters here fits inside ``[A-Za-z0-9._/-]``,
#     so "validate the shape and copy it" is the same rejected "does this look
#     safe" heuristic wearing a schema.
#
#     What WOULD be provable is a re-mint by exact match against the card's
#     own declared ``required_outputs`` -- card content, manager-authored,
#     already durable -- emitting the declared pattern plus a count for
#     anything a glob discovered. That needs the declared set, which
#     ``terminal_event_authority`` is not given; supplying it is a
#     process_launcher.py change, not a change here. Until then the honest
#     answer is: the reason travels, the paths do not.
# --------------------------------------------------------------------------- #

def _required_output_mismatch(**buckets: object) -> str:
    """The exact production string shape, minted the same way the validator does."""
    diagnostics: dict[str, object] = {
        "missing_required_artifacts": [],
        "unchanged_mandatory_outputs": [],
        "scope_violations": [],
        "primary_validation_result": [],
        "legacy_error_codes": [],
    }
    diagnostics.update(buckets)
    return "required_output_mismatch:" + json.dumps(
        diagnostics, sort_keys=True, separators=(",", ":")
    )


def test_the_real_required_output_mismatch_names_the_validation_reason() -> None:
    """The measured regression, at the exact string process_launcher passes:
    ``validation_failed:runtime_error`` -- "something threw" -- for the one
    failure class that is fully reworkable."""
    error = _required_output_mismatch(
        unchanged_mandatory_outputs=["out/result.json"],
        legacy_error_codes=["required_output_unchanged:out/result.json"],
    )
    result = terminal_event_authority(state="validation_failed", exit_code=0, error=error)
    assert result["failure_kind"] == "validation_failed"
    assert result["diagnostic"] == "validation_failed:required_output_unchanged:exit_code=0"
    assert result["error"] == result["diagnostic"]
    assert "runtime_error" not in result["error"]


@pytest.mark.parametrize(
    ("legacy_code", "expected"),
    [
        ("required_output_unchanged:out/result.json", "required_output_unchanged"),
        (
            "required_output_unchanged_parent_mismatch:out/result.json",
            "required_output_unchanged_parent_mismatch",
        ),
        ("required_output_zero_bytes:out/result.json", "required_output_zero_bytes"),
        ("required_output_symlink:out/result.json", "required_output_symlink"),
        ("required_output_no_matches:out/*.json", "required_output_no_matches"),
        ("required_output_missing:out/result.json", "required_output_missing"),
    ],
)
def test_every_validation_reason_the_validator_mints_survives(
    legacy_code: str, expected: str,
) -> None:
    """Each bucket the validator can fill, not just the one the first failing
    card happened to hit."""
    result = terminal_event_authority(
        state="validation_failed",
        exit_code=0,
        error=_required_output_mismatch(legacy_error_codes=[legacy_code]),
    )
    assert result["diagnostic"] == f"validation_failed:{expected}:exit_code=0"
    assert _ALLOWLISTED_DIAGNOSTIC.match(result["diagnostic"]), result["diagnostic"]


def test_the_specific_reason_outranks_the_mismatch_envelope_that_carries_it() -> None:
    """``required_output_mismatch`` is the envelope; the per-path code is the
    diagnosis. The envelope is the answer only when no per-path code is
    present."""
    specific = terminal_event_authority(
        state="validation_failed",
        exit_code=0,
        error=_required_output_mismatch(
            legacy_error_codes=["required_output_zero_bytes:out/result.json"],
        ),
    )
    assert specific["diagnostic"] == "validation_failed:required_output_zero_bytes:exit_code=0"
    envelope = terminal_event_authority(
        state="validation_failed",
        exit_code=0,
        error=_required_output_mismatch(scope_violations=[{"path": "out/a", "reason": "x"}]),
    )
    assert envelope["diagnostic"] == "validation_failed:required_output_mismatch:exit_code=0"


def test_no_validation_path_or_json_byte_reaches_the_error_field() -> None:
    """The design decision, pinned. ``error`` carries the reason and nothing
    else -- not the paths, not the JSON punctuation -- so its constant shape
    stays provable by construction rather than by trusting a path filter."""
    error = _required_output_mismatch(
        unchanged_mandatory_outputs=["out/result.json", "docs/report.md"],
        missing_required_artifacts=["out/missing.json"],
        scope_violations=[{"path": "out/link.json", "reason": "symlink"}],
        legacy_error_codes=["required_output_unchanged:out/result.json"],
    )
    for text in (
        terminal_event_authority(state="validation_failed", exit_code=0, error=error)["error"],
        safe_error_text(state="review_ready", exit_code=0, error=error),
    ):
        for path in ("out/result.json", "docs/report.md", "out/missing.json", "out/link.json"):
            assert path not in text
        assert "/" not in text and "{" not in text and "[" not in text and '"' not in text
        assert _ALLOWLISTED_DIAGNOSTIC.match(text), text


def test_a_worker_chosen_output_filename_can_never_reach_a_durable_diagnostic() -> None:
    """Why the paths are not admitted, concretely: a card that declares a glob
    lets the WORKER choose the filename the validator then reports. The reason
    survives; the worker's bytes do not."""
    error = _required_output_mismatch(
        unchanged_mandatory_outputs=["out/AKIAIOSFODNN7EXAMPLE.json"],
        legacy_error_codes=[
            "required_output_unchanged:out/sk-ABCDEFGHIJKLMNOP123456.json",
        ],
    )
    result = terminal_event_authority(state="validation_failed", exit_code=0, error=error)
    assert result["diagnostic"] == "validation_failed:required_output_unchanged:exit_code=0"
    assert "AKIAIOSFODNN7EXAMPLE" not in result["error"]
    assert "sk-ABCDEFGHIJKLMNOP123456" not in result["error"]


# --------------------------------------------------------------------------- #
# Wrapper ordering.
# --------------------------------------------------------------------------- #

def test_wrapper_reasons_are_ordered_strictly_last() -> None:
    """Priority is tuple order, so an insertion in the wrong place silently
    demotes every cause the wrapper encloses. ``finalizer_retries_exhausted``
    wraps all of them and must therefore stay final -- this is the structural
    half of the guarantee ``test_every_control_plane_reason_survives_the_wrapper``
    proves behaviourally."""
    assert _CONTROL_PLANE_REASONS[-1] == "finalizer_retries_exhausted"
    assert _CONTROL_PLANE_REASONS.count("finalizer_retries_exhausted") == 1
    assert len(set(_CONTROL_PLANE_REASONS)) == len(_CONTROL_PLANE_REASONS)


def test_the_finalizer_wrapper_never_outranks_a_validation_reason() -> None:
    """The two vocabularies nest in production: the isolated finalizer wraps a
    validation refusal when its own retry then fails."""
    result = terminal_event_authority(
        state="finalize_abandoned",
        exit_code=None,
        error=(
            "finalizer_retries_exhausted:attempt=1:RuntimeError:"
            + _required_output_mismatch(
                legacy_error_codes=["required_output_unchanged:out/result.json"],
            )
        ),
    )
    assert result["diagnostic"] == "finalize_abandoned:required_output_unchanged"
    assert "finalizer_retries_exhausted" not in result["diagnostic"]


# --------------------------------------------------------------------------- #
# TYPED REASONS.
#
# The allowlist above defends the UNTRUSTED string channel and stays exactly as
# it is. These tests pin the second channel: a reason a call site already holds
# as a constant, stated as a value instead of recovered from prose downstream.
# --------------------------------------------------------------------------- #


def test_terminal_reason_is_not_a_str_subclass() -> None:
    """The whole point of the type. If ``TerminalReason`` subclassed ``str``,
    every existing ``f"{reason}:{tail}"`` would keep compiling and silently
    reproduce an untyped string, and the marking would erode one interpolation
    at a time. ``render()`` must be the single, explicit exit back to text."""
    reason = TerminalReason("not_processing")
    assert not isinstance(reason, str)
    assert reason.render() == "not_processing"


@pytest.mark.parametrize("payload", _SECRET_PAYLOADS)
def test_a_typed_reason_cannot_be_minted_from_caller_text(payload: str) -> None:
    """No byte of a caller-supplied string can enter a typed reason, in either
    slot. An unrecognised value collapses to ``unrecognized`` -- the same
    fail-closed answer an unrecognised allowlist token gets."""
    reason = TerminalReason(payload, (("state", payload), ("", payload)))
    rendered = reason.render()
    assert payload not in rendered
    for fragment in payload.split():
        assert fragment not in rendered


def test_a_typed_reason_returns_the_modules_own_constant_never_the_argument() -> None:
    """No-copy, demonstrated by identity: the object handed back is the
    registry's element, not the caller's equal-but-distinct string."""
    supplied = "".join(["not", "_", "processing"])
    assert supplied == "not_processing" and supplied is not "not_processing"  # noqa: F632
    assert constant(supplied) is _REASON_CONSTANTS["not_processing"]


def test_recognised_reason_re_mints_a_fully_owned_string_exactly() -> None:
    for text in (
        "output_budget_exceeded:captured_output_bytes",
        "provider_refused:http_status=401:cause_not_distinguished_by_response",
        "worker_failed:supervisor_state=timed_out:exit_code=124",
        "claude_subscription_session_refresh_required",
    ):
        reason = recognised_reason(text)
        assert reason is not None, text
        assert reason.render() == text


@pytest.mark.parametrize("text", [
    "claim_ownership_lost:claimed_by=a_different_runner",
    "finalizer_retries_exhausted:attempt=1:RuntimeError:boom",
    "worker_failed:supervisor_state=timed_out:exit_code=124:Bearer abcd1234",
    "not_a_reason_at_all",
    "",
])
def test_recognised_reason_fails_closed_on_one_unowned_token(text: str) -> None:
    """One token this module does not already own fails the WHOLE parse: the
    caller falls back to sanitation rather than getting a partial re-mint that
    could carry the tail."""
    assert recognised_reason(text) is None


def test_recognised_reason_refuses_an_unbounded_digit_run() -> None:
    assert recognised_reason("worker_failed:exit_code=" + "9" * 40) is None


def test_workspace_error_reason_types_only_the_minted_prefix() -> None:
    reason = workspace_error_reason("claim_ownership_lost:claimed_by=worker_x")
    assert reason is not None
    assert reason.render() == "claim_ownership_lost"
    assert "worker_x" not in reason.render()


@pytest.mark.parametrize("text", [
    "required_output_missing:required.txt",
    "quality_review_workspace_mutated:src/secret.py",
    "ValidationRunError: mypy failed",
])
def test_workspace_error_reason_is_deliberately_narrow(text: str) -> None:
    """Everything the diagnostic already names correctly keeps the settled
    ``error == diagnostic`` authority triple. Widening this is Step 2."""
    assert workspace_error_reason(text) is None


def test_required_output_reason_re_mints_from_the_cards_declared_outputs() -> None:
    """The actionable half of a mandatory-output refusal travels -- but as a
    re-mint of the CARD's declared paths, never a copy of what the validator
    observed (a glob lets a worker choose the filename it reports back)."""
    payload = json.dumps(
        {
            "legacy_error_codes": ["required_output_unchanged:out/result.json"],
            "missing_required_artifacts": [],
            "scope_violations": [],
            "unchanged_mandatory_outputs": ["out/result.json"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    reason = workspace_error_reason(
        "required_output_mismatch:" + payload, ["out/result.json"],
    )
    assert reason is not None
    assert reason.render() == (
        'required_output_mismatch:{"unchanged_mandatory_outputs":["out/result.json"]}'
    )


def test_required_output_reason_drops_a_path_the_card_never_declared() -> None:
    payload = json.dumps(
        {"unchanged_mandatory_outputs": ["out/AKIAIOSFODNN7EXAMPLE.json"]},
        separators=(",", ":"),
    )
    reason = workspace_error_reason(
        "required_output_mismatch:" + payload, ["out/result.json"],
    )
    assert reason is not None
    assert reason.render() == "required_output_mismatch"
    assert "AKIA" not in reason.render()


def test_a_typed_reason_wins_error_but_never_widens_the_diagnostic() -> None:
    """``diagnostic`` stays the classifier's shape-guarded verdict on BOTH
    channels, so every downstream shape check keeps holding; only the
    operator-facing ``error`` takes the typed value."""
    result = terminal_event_authority(
        state="worker_failed",
        exit_code=124,
        error="worker_failed:supervisor_state=timed_out:exit_code=124",
        reason=supervisor_failure_reason("timed_out", 124),
    )
    assert result["error"] == "worker_failed:supervisor_state=timed_out:exit_code=124"
    assert result["diagnostic"] == "worker_failed:unclassified:exit_code=124"
    assert _ALLOWLISTED_DIAGNOSTIC.match(result["diagnostic"])


def test_terminal_event_authority_without_a_reason_is_unchanged() -> None:
    typed = terminal_event_authority(
        state="finalize_failed", exit_code=None, error="metadata_invalid:boom",
        reason=TerminalReason("metadata_invalid"),
    )
    untyped = terminal_event_authority(
        state="finalize_failed", exit_code=None, error="metadata_invalid:boom",
    )
    assert untyped["error"] == untyped["diagnostic"] == "finalize_failed:metadata_invalid"
    assert typed["error"] == "metadata_invalid"
    assert typed["diagnostic"] == untyped["diagnostic"]


def test_supervisor_incomplete_reason_keeps_the_stale_packet_shape() -> None:
    """The exact information the fixed ``<failure_kind>:<code>`` diagnostic
    shape has no slot for: WHICH stale supervisor state was seen."""
    reason = supervisor_incomplete_reason("token_budget_exceeded", 0)
    assert reason.render() == "supervisor_incomplete:state=token_budget_exceeded:rc=0"
    assert supervisor_incomplete_reason("", None).render() == (
        "supervisor_incomplete:state=missing"
    )


def test_supervisor_incomplete_reason_refuses_an_unknown_state() -> None:
    """A supervisor status ``state`` is external JSON. It can SELECT a known
    constant; it can never synthesise one."""
    assert supervisor_incomplete_reason("../../etc/passwd", 0).render() == (
        "supervisor_incomplete:state=unrecognized:rc=0"
    )


def test_provider_refusal_vocabulary_matches_runtime_adapters() -> None:
    """``_PROVIDER_REFUSAL_REASONS`` is named here rather than imported so this
    module keeps its standard-library-only dependency. This is the standing
    proof that the copy cannot drift from the owner."""
    from aiworkhub import claude_auth, runtime_adapters

    kinds = {
        value
        for name, value in vars(runtime_adapters).items()
        if name.startswith("REFUSAL_") and isinstance(value, str)
    }
    assert kinds, "runtime_adapters must expose its refusal-kind vocabulary"
    for kind in kinds:
        assert f"provider_refused_{kind}" in _PROVIDER_REFUSAL_REASONS
    assert runtime_adapters.OUTCOME_PROVIDER_REFUSED in _PROVIDER_REFUSAL_REASONS
    assert claude_auth.RUNTIME_AUTH_FAILURE_REASON in _PROVIDER_REFUSAL_REASONS
    for status in runtime_adapters.PROVIDER_REFUSAL_STATUSES:
        outcome = runtime_adapters.classify_provider_outcome(
            exit_code=1, message=f"http_status={status}",
        )
        if outcome["outcome"] != runtime_adapters.OUTCOME_PROVIDER_REFUSED:
            continue
        assert recognised_reason(outcome["reason"]) is not None, outcome["reason"]
