"""NF-2026-01018 / audit B1: the measured failure shapes get their own names.

``worker_failed:runtime_error:exit_code=1`` was the dominant blocked reason on
this repository, and it was not because the causes were unknown. Measured over
all 141 non-zero worker exits recorded on 2026-09-26, three shapes account for
99 of them and every one states precisely what happened:

  * 64 VS Code LM turn failures, carried as
    ``vscode_lm_request_failed:<code>:diagnostics={...}``. The catch-all
    signature ``traceback|exception|error|fatal`` fired on the ``RuntimeError``
    NAME and filed all 64 as "something threw".
  * 36 OS-level filesystem denials -- 22 Bun ``EPERM ... realpath`` refusals
    inside an AppContainer, 14 Windows ``Access is denied. (os error 5)`` from a
    codex ``CODEX_HOME`` the container could not reach.
  * 13 launcher aborts, ``AppContainerError:filesystem_grant_failed``, 11 of
    them with ``exit_code=126``. An OS-level denial that reached
    ``auth_forbidden`` pointed every reader at credentials instead of at a DACL.

These tests feed the classifier each of those shapes -- synthetic strings built
to the measured form, never a copied process log -- and assert the specific
code. They also pin the three things that must NOT change: provider/HTTP auth
text still classifies as auth, the no-copy invariant still holds for the new
codes, and genuinely unknown text still falls through to ``runtime_error``.

NAMING A CAUSE AND CHOOSING A RESPONSE ARE SEPARATE POWERS, and the split runs
straight through the sandbox pair. ``sandbox_filesystem_grant_failed`` is a
whole-token match on a literal the launcher minted about its own abort, so it
places the card as infrastructure. ``sandbox_filesystem_denied`` comes from a
prose regex over worker-controlled stderr -- a candidate's own
``PermissionError``/``EACCES`` mints it just as readily -- so it is recognised
and left unplaced, and a test below proves it earns no transient requeue.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from aiworkhub import terminal_failure_classification as tfc
from aiworkhub.terminal_failure_classification import (
    ACTION_CANDIDATE_REWORK,
    ACTION_DEPENDENCY_HOLD,
    ACTION_MANAGER_JUDGMENT_UNKNOWN,
    CAUSE_CANDIDATE_CODE,
    CAUSE_SANDBOX_UNSUPPORTED,
    FAILURE_CLASS_DEFECT,
    FAILURE_CLASS_TRANSIENT,
    FAILURE_CLASS_UNKNOWN,
    MAX_DIAGNOSTIC_CHARS,
    RETRY_SCOPE_NONE,
    _SANDBOX_CODES,
    _SANDBOX_FILESYSTEM_DENIED,
    _SANDBOX_FILESYSTEM_GRANT_FAILED,
    _VSCODE_LM_BEHAVIOUR_CODES,
    _VSCODE_LM_CODES,
    _VSCODE_LM_REQUEST_FAILED,
    _VSCODE_LM_TRANSPORT_CODES,
    classify_terminal_failure,
    classify_terminal_failure_from_paths,
    constant,
    disposition_for_reason,
    failure_disposition_from_substatus,
    safe_error_text,
    unclassified_reason_constants,
)

# The same strict shape ``tests/test_terminal_failure_classification.py`` pins:
# ``<failure_kind>:<code>`` plus optional system-owned numeric metadata, and
# nothing else. Braces, quotes, slashes and whitespace are excluded by
# construction, so a diagnostic that matches cannot be carrying a path or a JSON
# blob out of the scanned text.
#
# DIGITS ARE PART OF THE CODE ALPHABET. The vocabulary is versioned --
# ``vscode_lm_edit_response_v2_shape_invalid`` and
# ``vscode_lm_edit_response_v2_path_count_exceeded`` both carry a ``2`` -- so a
# ``[a-z_]+`` code class rejects real codes and says nothing about leakage. What
# this shape exists to exclude is the punctuation a path or a JSON blob needs,
# and that is unchanged.
_ALLOWLISTED_DIAGNOSTIC = re.compile(
    r"^[a-z0-9_]+:[a-z0-9_]+(?::http_status=\d{3})?(?::exit_code=-?\d+)?$"
)

_WORKER_FAILED_WRAPPER = "worker_failed:supervisor_state=exited:exit_code=1"

# --------------------------------------------------------------------------- #
# The measured shapes, rebuilt synthetically. Paths and identifiers are
# invented; only the FORM is taken from the measurement.
# --------------------------------------------------------------------------- #

# Bucket 2a: Bun (grok/kilo) in an AppContainer, refused a realpath on the
# request home it was never granted.
_BUN_DENIED_PATH = r"C:\synthetic\req\home\.local\state"
_BUN_EPERM_REALPATH = (
    f"EPERM: operation not permitted, realpath '{_BUN_DENIED_PATH}'\n"
    '  syscall: "realpath", errno: -4048, code: "EPERM"\n'
)

# Bucket 2b: codex canonicalising a CODEX_HOME whose directory is unreachable.
_CODEX_HOME_PATH = r"C:\synthetic\req\home\.codex"
_CODEX_ACCESS_DENIED = (
    f"ERROR: failed to canonicalize CODEX_HOME `{_CODEX_HOME_PATH}`: "
    "Access is denied. (os error 5)\n"
)

# Bucket 3: the launcher's own abort, in the supervisor status ``error`` field.
_APPCONTAINER_GRANT_FAILED = (
    r"AppContainerError:filesystem_grant_failed: write DACL C:\Users"
)

_OS_DENIAL_SHAPES = (
    ("bun_eperm_realpath", _BUN_EPERM_REALPATH),
    ("codex_access_denied_os_error_5", _CODEX_ACCESS_DENIED),
    # The POSIX and Python spellings of the same refusal, which the same
    # container shape produces on Linux hosts.
    ("python_permissionerror", "PermissionError: [Errno 13] cannot open state dir\n"),
    ("node_eacces", "Error: EACCES: permission denied, mkdir '/synthetic/state'\n"),
)


def _vscode_lm_worker_stderr(code: str) -> str:
    """The exact form ``vscode_lm_worker.py`` raises when the bridge response
    carries an ``error``: a short Python traceback whose message is the
    launcher-minted wrapper, the code, and a diagnostics blob."""
    diagnostics = json.dumps(
        {"turns": 11, "trace": ["tool_request", "reply"], "preview": "..."},
        separators=(",", ":"),
    )
    return (
        "Traceback (most recent call last):\n"
        '  File "vscode_lm_worker.py", line 1412, in _await_bridge_response\n'
        f"RuntimeError: vscode_lm_request_failed:{code}:diagnostics={diagnostics}\n"
    )


_MEASURED_VSCODE_LM_CODES = _VSCODE_LM_BEHAVIOUR_CODES + _VSCODE_LM_TRANSPORT_CODES

# The eight codes the 2026-09-26 sample actually produced, kept as their own
# list so the counted buckets stay readable next to the whole vocabulary.
_SAMPLED_VSCODE_LM_CODES = (
    "vscode_lm_semantic_edit_stage_required",
    "vscode_lm_agent_turn_limit",
    "vscode_lm_quality_review_submit_required",
    "vscode_lm_finalization_limit",
    "vscode_lm_tool_not_allowed",
    "vscode_lm_text_response_too_large",
    "vscode_lm_request_cancelled",
    "vscode_lm_text_protocol_invalid_json",
)

_NEVER = ("runtime_error", "unclassified", "auth_forbidden")


def _assert_names_a_cause(diagnostic: str) -> None:
    assert _ALLOWLISTED_DIAGNOSTIC.match(diagnostic), diagnostic
    assert len(diagnostic) <= MAX_DIAGNOSTIC_CHARS
    for flattened in _NEVER:
        assert flattened not in diagnostic, diagnostic


# --------------------------------------------------------------------------- #
# Bucket 1 -- the VS Code LM turn failures.
# --------------------------------------------------------------------------- #

def test_every_sampled_vscode_lm_code_is_in_the_vocabulary() -> None:
    """The counted buckets are a subset of the vocabulary, not the whole of it:
    the list is taken from the literals the extension and worker mint, so a code
    that simply has not failed yet is named the first time it does."""
    for code in _SAMPLED_VSCODE_LM_CODES:
        assert code in _MEASURED_VSCODE_LM_CODES, code


@pytest.mark.parametrize("code", _MEASURED_VSCODE_LM_CODES)
def test_every_vscode_lm_code_survives_the_worker_traceback_from_stderr(code: str) -> None:
    """The measured channel: the ``RuntimeError`` lands in the worker's stderr
    and the supervisor wrapper says only ``supervisor_state=exited``."""
    result = classify_terminal_failure(
        state="worker_failed",
        exit_code=1,
        error=_WORKER_FAILED_WRAPPER,
        stderr_tail=_vscode_lm_worker_stderr(code),
    )
    assert result["failure_kind"] == "worker_failed"
    assert result["diagnostic"] == f"worker_failed:{code}:exit_code=1"
    _assert_names_a_cause(result["diagnostic"])


@pytest.mark.parametrize("code", _MEASURED_VSCODE_LM_CODES)
def test_every_vscode_lm_code_survives_on_the_error_channel_too(code: str) -> None:
    """The same wrapper reaching the classifier as the terminal ``error``
    instead of a log tail must reduce to the same code -- the finalizer's two
    channels cannot disagree about one failure."""
    error = f"vscode_lm_request_failed:{code}:diagnostics={{}}"
    result = classify_terminal_failure(state="worker_failed", exit_code=1, error=error)
    assert result["diagnostic"] == f"worker_failed:{code}:exit_code=1"
    assert safe_error_text(state="worker_failed", exit_code=1, error=error) == (
        result["diagnostic"]
    )


@pytest.mark.parametrize("code", _SAMPLED_VSCODE_LM_CODES)
def test_a_sampled_vscode_lm_code_is_read_from_the_real_stderr_log(
    tmp_path: Path, code: str,
) -> None:
    stderr_path = tmp_path / f"{code}.stderr.log"
    stderr_path.write_text(_vscode_lm_worker_stderr(code), encoding="utf-8")

    result = classify_terminal_failure_from_paths(
        state="worker_failed",
        exit_code=1,
        error=_WORKER_FAILED_WRAPPER,
        stdout_path=tmp_path / "missing.stdout.log",
        stderr_path=stderr_path,
    )
    assert result["diagnostic"] == f"worker_failed:{code}:exit_code=1"


def test_the_wrapper_names_the_bridge_turn_when_the_code_is_not_in_the_vocabulary() -> None:
    """The measured ``network`` bucket. A bare ``network`` token is far too
    generic to allowlist against arbitrary provider text -- a stderr line reading
    "network unreachable" would claim it -- so the honest answer is the wrapper
    the launcher itself minted: the bridge turn failed. That is still strictly
    better than ``runtime_error``, which named nothing at all."""
    result = classify_terminal_failure(
        state="worker_failed",
        exit_code=1,
        error=_WORKER_FAILED_WRAPPER,
        stderr_tail=_vscode_lm_worker_stderr("network"),
    )
    assert result["diagnostic"] == f"worker_failed:{_VSCODE_LM_REQUEST_FAILED}:exit_code=1"
    _assert_names_a_cause(result["diagnostic"])


def test_a_specific_code_always_outranks_the_wrapper_that_carries_it() -> None:
    """Order is priority and the wrapper is last, exactly as
    ``finalizer_retries_exhausted`` is last in the control-plane tuple."""
    assert _VSCODE_LM_CODES[-1] == _VSCODE_LM_REQUEST_FAILED
    assert _VSCODE_LM_CODES.count(_VSCODE_LM_REQUEST_FAILED) == 1
    assert len(set(_VSCODE_LM_CODES)) == len(_VSCODE_LM_CODES)
    result = classify_terminal_failure(
        state="worker_failed",
        exit_code=1,
        error=_WORKER_FAILED_WRAPPER,
        stderr_tail=_vscode_lm_worker_stderr("vscode_lm_agent_turn_limit"),
    )
    assert _VSCODE_LM_REQUEST_FAILED not in result["diagnostic"]


def test_the_vscode_lm_block_is_the_leading_run_of_the_signature_table() -> None:
    """``_signature_code`` skips the VS Code LM block by SLICING the first
    ``_VSCODE_LM_SIGNATURE_COUNT`` entries when the ``vscode_lm_`` prefix is
    absent. That is only equivalent to scanning them while the block really is
    the leading run, so the structure is asserted here rather than trusted: an
    entry inserted at the front would otherwise silently make the fast path skip
    a prose signature instead."""
    codes = [code for _pattern, code in tfc._SIGNATURES]
    assert codes[:tfc._VSCODE_LM_SIGNATURE_COUNT] == list(_VSCODE_LM_CODES)
    assert all(
        code.startswith(tfc._VSCODE_LM_CODE_PREFIX)
        for code in codes[:tfc._VSCODE_LM_SIGNATURE_COUNT]
    )
    assert not any(
        code.startswith(tfc._VSCODE_LM_CODE_PREFIX)
        for code in codes[tfc._VSCODE_LM_SIGNATURE_COUNT:]
    )
    # The sandbox codes follow immediately, and both sit ahead of every auth code
    # and of the ``runtime_error`` catch-all.
    tail = codes[tfc._VSCODE_LM_SIGNATURE_COUNT:]
    assert tail[:2] == list(_SANDBOX_CODES)
    assert tail.index("auth_forbidden") > tail.index(_SANDBOX_FILESYSTEM_DENIED)
    assert codes[-1] == "runtime_error"


# --------------------------------------------------------------------------- #
# Buckets 2 and 3 -- the sandbox filesystem denials.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "stderr_tail", [shape for _name, shape in _OS_DENIAL_SHAPES],
    ids=[name for name, _shape in _OS_DENIAL_SHAPES],
)
def test_every_os_filesystem_denial_shape_names_the_sandbox_not_auth(
    stderr_tail: str,
) -> None:
    result = classify_terminal_failure(
        state="worker_failed",
        exit_code=1,
        error=_WORKER_FAILED_WRAPPER,
        stderr_tail=stderr_tail,
    )
    assert result["failure_kind"] == "worker_failed"
    assert result["diagnostic"] == (
        f"worker_failed:{_SANDBOX_FILESYSTEM_DENIED}:exit_code=1"
    )
    _assert_names_a_cause(result["diagnostic"])


def test_the_bun_eperm_realpath_block_is_read_from_the_real_stderr_log(
    tmp_path: Path,
) -> None:
    stderr_path = tmp_path / "bun.stderr.log"
    stderr_path.write_text(_BUN_EPERM_REALPATH, encoding="utf-8")

    result = classify_terminal_failure_from_paths(
        state="worker_failed",
        exit_code=1,
        error=_WORKER_FAILED_WRAPPER,
        stdout_path=None,
        stderr_path=stderr_path,
    )
    assert result["diagnostic"] == (
        f"worker_failed:{_SANDBOX_FILESYSTEM_DENIED}:exit_code=1"
    )
    assert ".local" not in result["diagnostic"]


@pytest.mark.parametrize("state", ["worker_failed", "launch_failed"])
def test_the_launcher_grant_failure_names_itself_with_its_measured_exit_code(
    state: str,
) -> None:
    """11 of the 13 measured occurrences carried ``exit_code=126`` from the
    ``child_spawn`` phase. The exit code is system-owned numeric metadata and
    travels; the DACL path in the same string does not."""
    result = classify_terminal_failure(
        state=state, exit_code=126, error=_APPCONTAINER_GRANT_FAILED,
    )
    assert result["failure_kind"] == state
    assert result["diagnostic"] == (
        f"{state}:{_SANDBOX_FILESYSTEM_GRANT_FAILED}:exit_code=126"
    )
    _assert_names_a_cause(result["diagnostic"])


def test_the_launcher_grant_failure_also_survives_from_a_provider_stream() -> None:
    """The supervisor keeps a bounded copy of the same string in the child's
    stderr, so both channels must reduce to the same code."""
    result = classify_terminal_failure(
        state="worker_failed",
        exit_code=126,
        error=_WORKER_FAILED_WRAPPER,
        stderr_tail=_APPCONTAINER_GRANT_FAILED + "\n",
    )
    assert result["diagnostic"] == (
        f"worker_failed:{_SANDBOX_FILESYSTEM_GRANT_FAILED}:exit_code=126"
    )


# --------------------------------------------------------------------------- #
# What must NOT change: provider/HTTP auth text.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("HTTP/1.1 403 Forbidden", "auth_forbidden"),
        ("403 Forbidden: the request was refused", "auth_forbidden"),
        ("Permission denied: /etc/shadow", "auth_forbidden"),
        ("HTTP 401 Unauthorized", "auth_unauthorized"),
        ("Unauthorized", "auth_unauthorized"),
        ("fatal: invalid credential, please re-authenticate", "auth_invalid_credential"),
        ("invalid credential", "auth_invalid_credential"),
    ],
)
def test_provider_and_http_auth_text_still_classifies_as_the_existing_auth_codes(
    text: str, expected: str,
) -> None:
    """The sandbox codes sit AHEAD of ``auth_forbidden`` in the signature table,
    which is only safe because every phrase they admit names an errno, a Python
    exception type or a Windows error NUMBER. Bare ``permission denied`` is
    deliberately still auth's -- an HTTP refusal prints it too."""
    for stream in ("stdout_tail", "stderr_tail"):
        result = classify_terminal_failure(
            state="worker_failed",
            exit_code=1,
            error=_WORKER_FAILED_WRAPPER,
            **{stream: text},
        )
        assert result["diagnostic"] == f"worker_failed:{expected}:exit_code=1"


# --------------------------------------------------------------------------- #
# What must NOT change: the no-copy invariant, for the new codes.
# --------------------------------------------------------------------------- #

_SECRET_BEARING_MEASURED_SHAPES = (
    # A realpath whose denied path is itself credential-shaped: a request home
    # is machine-generated, but nothing stops a path segment looking like a key.
    "EPERM: operation not permitted, realpath "
    r"'C:\synthetic\AKIAIOSFODNN7EXAMPLE\home\.local\state'",
    # The diagnostics blob the VS Code LM wrapper carries is free-form JSON the
    # extension builds from the failing turn, including a response preview.
    "RuntimeError: vscode_lm_request_failed:vscode_lm_agent_turn_limit"
    ':diagnostics={"preview":"Authorization: Bearer abcd1234efgh5678ijkl",'
    '"password":"hunter2plain"}',
    r"AppContainerError:filesystem_grant_failed: write DACL C:\Users\sk-ABCDEF123456",
)

_FORBIDDEN_SUBSTRINGS = (
    "AKIAIOSFODNN7EXAMPLE",
    "Bearer",
    "hunter2plain",
    "sk-ABCDEF123456",
    "realpath",
    ".local",
    "DACL",
    "diagnostics",
    "preview",
)


@pytest.mark.parametrize("shape", _SECRET_BEARING_MEASURED_SHAPES)
def test_a_newly_named_code_never_carries_a_byte_of_the_input_out_with_it(
    shape: str,
) -> None:
    """Naming the cause must not become a smuggling route. The diagnostic is
    still built from a constant plus system-owned numbers only, so neither the
    realpath path nor the diagnostics JSON can reach durable state."""
    for state, exit_code in (("worker_failed", 1), ("launch_failed", None)):
        for channel in ("error", "stderr_tail", "stdout_tail"):
            kwargs = {"error": _WORKER_FAILED_WRAPPER}
            kwargs[channel] = shape
            result = classify_terminal_failure(
                state=state, exit_code=exit_code, **kwargs,
            )
            diagnostic = result["diagnostic"]
            assert shape not in diagnostic
            for fragment in _FORBIDDEN_SUBSTRINGS:
                assert fragment not in diagnostic, diagnostic
            assert "{" not in diagnostic and '"' not in diagnostic
            assert "\\" not in diagnostic and "/" not in diagnostic
            _assert_names_a_cause(diagnostic)
        # The sanitised public channel is built the same way, so it must be
        # exactly as safe for the same input.
        sanitised = safe_error_text(state=state, exit_code=exit_code, error=shape)
        assert shape not in sanitised
        for fragment in _FORBIDDEN_SUBSTRINGS:
            assert fragment not in sanitised, sanitised
        assert _ALLOWLISTED_DIAGNOSTIC.match(sanitised), sanitised


@pytest.mark.parametrize(
    "shape",
    [
        _vscode_lm_worker_stderr("vscode_lm_agent_turn_limit"),
        _BUN_EPERM_REALPATH,
        _CODEX_ACCESS_DENIED,
        _APPCONTAINER_GRANT_FAILED,
    ],
)
def test_the_code_returned_is_the_modules_own_object_never_a_slice_of_the_input(
    shape: str,
) -> None:
    """The no-copy invariant asserted by identity, the same way
    ``test_control_plane_code_is_the_module_constant_never_a_slice_of_the_input``
    asserts it for the control-plane vocabulary."""
    code = tfc._signature_code(shape)
    assert code is not None
    owned = _VSCODE_LM_CODES + _SANDBOX_CODES
    assert any(code is constant_object for constant_object in owned), code


# --------------------------------------------------------------------------- #
# What must NOT change: the catch-all is still the last resort.
# --------------------------------------------------------------------------- #

def test_genuinely_unknown_error_text_still_falls_through_to_runtime_error() -> None:
    result = classify_terminal_failure(
        state="worker_failed",
        exit_code=1,
        error=_WORKER_FAILED_WRAPPER,
        stderr_tail=(
            "Traceback (most recent call last):\n"
            "ValueError: nothing in any vocabulary describes this\n"
        ),
    )
    assert result["diagnostic"] == "worker_failed:runtime_error:exit_code=1"


@pytest.mark.parametrize(
    "near_miss",
    [
        "xvscode_lm_agent_turn_limit",
        "vscode_lm_agent_turn_limits",
        "VSCODE_LM_AGENT_TURN_LIMIT",
        "vscode_lm_agent_turn_limit_exceeded",
        "sandbox_filesystem_grant_failedx",
        "xfilesystem_grant_failed",
        "os error 50",
        "epermission",
    ],
)
def test_a_near_miss_is_not_admitted_by_the_whole_token_matcher(near_miss: str) -> None:
    """An allowlist admits only what it names. A longer word, a missing left
    boundary, or a different case matches nothing and falls through to the
    catch-all exactly as before."""
    result = classify_terminal_failure(
        state="worker_failed",
        exit_code=1,
        error=_WORKER_FAILED_WRAPPER,
        stderr_tail=f"Traceback (most recent call last):\nRuntimeError: {near_miss}\n",
    )
    assert result["diagnostic"] == "worker_failed:runtime_error:exit_code=1"
    assert near_miss not in result["diagnostic"]


def test_the_bridge_response_timeout_still_outranks_the_new_vocabulary() -> None:
    """``vscode_lm_response_timeout`` is deliberately absent from the vocabulary:
    it is the bridge's own machine-generated timeout reason and
    ``provider_timeout`` is the stronger statement about it."""
    assert "vscode_lm_response_timeout" not in _VSCODE_LM_CODES
    result = classify_terminal_failure(
        state="worker_failed",
        exit_code=1,
        error=_WORKER_FAILED_WRAPPER,
        stderr_tail="RuntimeError: vscode_lm_response_timeout\n",
    )
    assert result["diagnostic"] == "worker_failed:provider_timeout:exit_code=1"


# --------------------------------------------------------------------------- #
# End to end: every new code is registered, and placed or disclaimed.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("code", _VSCODE_LM_CODES + _SANDBOX_CODES)
def test_every_new_code_is_a_registered_reason_constant(code: str) -> None:
    assert code in tfc._REASON_CONSTANTS
    assert constant(code) is tfc._REASON_CONSTANTS[code]
    assert constant(code) == code


def test_no_new_code_arrives_unplaced_and_undisclaimed() -> None:
    """The standing gate: a constant added to any vocabulary must be either
    placed by the disposition taxonomy or explicitly disclaimed by it."""
    assert unclassified_reason_constants() == ()


@pytest.mark.parametrize("code", _VSCODE_LM_BEHAVIOUR_CODES)
def test_a_vscode_lm_behaviour_code_places_the_card_as_candidate_rework(code: str) -> None:
    """The launcher refused THIS WORKER'S OWN TURN after inspecting it, which is
    the same species of evidence as the mandatory-output validator's refusal."""
    assert disposition_for_reason(code) == FAILURE_CLASS_DEFECT
    disposition = failure_disposition_from_substatus(
        terminal_substatus="worker_failed", reason=code,
    )
    assert disposition["cause"] == CAUSE_CANDIDATE_CODE
    assert disposition["failure_class"] == FAILURE_CLASS_DEFECT
    assert disposition["action"] == ACTION_CANDIDATE_REWORK
    assert disposition["evidence"] == f"control_plane_reason={code}"


def test_only_the_launcher_minted_sandbox_abort_is_placed() -> None:
    """The placed set is pinned EXACTLY here rather than derived from the module,
    because re-adding the prose-matched ``sandbox_filesystem_denied`` to it is the
    defect this assertion exists to catch: it must fail here, not in a requeue
    loop that spends a card's retry budget."""
    assert tfc._SANDBOX_REASONS == frozenset({_SANDBOX_FILESYSTEM_GRANT_FAILED})


def test_the_launcher_minted_sandbox_abort_places_the_card_as_infrastructure() -> None:
    """Infrastructure: nothing the candidate does differently next time changes
    a DACL the launcher could not write, so it holds rather than reworking."""
    code = _SANDBOX_FILESYSTEM_GRANT_FAILED
    assert disposition_for_reason(code) == FAILURE_CLASS_TRANSIENT
    disposition = failure_disposition_from_substatus(
        terminal_substatus="worker_failed", reason=code,
    )
    assert disposition["cause"] == CAUSE_SANDBOX_UNSUPPORTED
    assert disposition["failure_class"] == FAILURE_CLASS_TRANSIENT
    assert disposition["action"] == ACTION_DEPENDENCY_HOLD
    assert disposition["provider_launched"] is False


def test_the_os_denial_code_is_recognised_but_places_no_card() -> None:
    """``sandbox_filesystem_denied`` is minted by a PROSE regex over
    worker-controlled stderr, so it may describe a failure and must not choose the
    response to one. Recognised and registered, placed by neither table."""
    assert _SANDBOX_FILESYSTEM_DENIED in tfc._REASON_CONSTANTS
    assert _SANDBOX_FILESYSTEM_DENIED not in tfc.REASON_DISPOSITION
    assert _SANDBOX_FILESYSTEM_DENIED not in tfc._REASON_CAUSE
    assert _SANDBOX_FILESYSTEM_DENIED in tfc._DISCLAIMED_REASONS
    assert disposition_for_reason(_SANDBOX_FILESYSTEM_DENIED) == FAILURE_CLASS_UNKNOWN


# The measured shape's evil twin: the same stderr grammar, the opposite owner. A
# Windows sharing violation is the candidate's OWN file handle, not a container
# the launcher misconfigured, and no pattern over this text can tell the two
# apart -- which is exactly why neither may select an automatic action.
_CANDIDATE_OWNED_DENIAL = (
    "Traceback (most recent call last):\n"
    '  File "tests/test_widget.py", line 20, in test_writes_report\n'
    "PermissionError: [WinError 32] The process cannot access the file because "
    "it is being used by another process\n"
)


def test_a_candidate_owned_permission_error_is_never_requeued_as_transient() -> None:
    """The failure this rework exists to prevent. A ``transient`` class has the
    launcher call ``task_store.mark_transient_retry`` and requeue the card, so a
    candidate whose own tests raise ``PermissionError`` would be re-run until its
    budget was gone instead of reworked. The denial is still NAMED; the
    disposition stays unknown, which sends the card to a manager."""
    result = classify_terminal_failure(
        state="worker_failed",
        exit_code=1,
        error=_WORKER_FAILED_WRAPPER,
        stderr_tail=_CANDIDATE_OWNED_DENIAL,
    )
    assert result["diagnostic"] == (
        f"worker_failed:{_SANDBOX_FILESYSTEM_DENIED}:exit_code=1"
    )
    _assert_names_a_cause(result["diagnostic"])

    disposition = failure_disposition_from_substatus(
        terminal_substatus="worker_failed", reason=_SANDBOX_FILESYSTEM_DENIED,
    )
    assert disposition["failure_class"] != FAILURE_CLASS_TRANSIENT
    assert disposition["failure_class"] == FAILURE_CLASS_UNKNOWN
    assert disposition["cause"] != CAUSE_SANDBOX_UNSUPPORTED
    assert disposition["action"] == ACTION_MANAGER_JUDGMENT_UNKNOWN
    assert disposition["retry_scope"] == RETRY_SCOPE_NONE


@pytest.mark.parametrize("code", _VSCODE_LM_TRANSPORT_CODES + (_VSCODE_LM_REQUEST_FAILED,))
def test_a_transport_or_wrapper_code_is_recognised_but_places_no_card(code: str) -> None:
    """A cancellation, an unavailable MCP host, a request/response identity
    mismatch, or the bare wrapper says nothing about whether the work was wrong.
    Each is recognised so the diagnosis survives, and each stays unplaced."""
    assert disposition_for_reason(code) == FAILURE_CLASS_UNKNOWN
    assert code in tfc._DISCLAIMED_REASONS
