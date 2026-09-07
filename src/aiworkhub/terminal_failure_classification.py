from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_ID = "aiworkhub.task_mcp.terminal_failure_classification.v1"

MAX_DIAGNOSTIC_CHARS = 400
MAX_TAIL_READ_BYTES = 4096

_TIMEOUT_STALL_STATES = {"timed_out", "stalled", "liveness_lost"}
_CANCELLED_STATES = {"cancelled", "canceled"}
_LAUNCH_FAILED_STATES = {"launch_failed"}

# ``reconcile_pending`` is the isolated finalizer's own not-yet-settled retry
# state (a dead-supervisor reconciliation attempt that may still succeed on
# the next pass) -- it must carry no failure verdict of its own, the same as
# an in-flight cancellation, or a genuinely still-pending card would flash a
# spurious ``nonzero_exit``/etc verdict off its carried-over exit_code before
# its own retry ever gets a chance to settle (NF-2026-00622 V7 rework).
_NO_VERDICT_STATES = _CANCELLED_STATES | {"reconcile_pending"}

# Closed-vocabulary launcher terminal states whose failure_kind equals the
# state name itself. A missing/zero-ish exit_code is normal for these (the
# supervisor never ran a provider process, or ran one to a clean exit that
# still never reached review), so they cannot rely on the exit_code fallback
# below -- each must be named here explicitly, once, rather than rediscovered
# one exit_code-shaped gap at a time (NF-2026-00622 V7 rework).
#
# ``validation_failed``/``finalize_failed``/``scope_rejected``/
# ``promotion_conflict`` are the finalizer's own post-``exited`` outcomes
# (``_terminal_state_for_workspace_error``'s closed return set): a clean
# ``exit_code == 0`` provider exit followed by a failed validation/
# finalization/scope/promotion step. Same exit_code=0 fallback gap as the
# others -- these must be named here too, not rediscovered per state
# (NF-2026-00622 V7 rework-of-rework-of-rework).
_NAMED_FAILURE_KINDS: dict[str, str] = {
    "output_budget_exceeded": "output_budget_exceeded",
    "exited_without_review": "exited_without_review",
    "validation_failed": "validation_failed",
    "finalize_failed": "finalize_failed",
    "scope_rejected": "scope_rejected",
    "promotion_conflict": "promotion_conflict",
    # The isolated finalizer's own dead-end outcome when retry exhausts and
    # the target card can no longer be moved (archived/deleted/reclaimed) --
    # a genuine, named terminal failure, not a retryable ``reconcile_pending``
    # (NF-2026-00622 V7 rework).
    "finalize_abandoned": "finalize_abandoned",
}

_UNCLASSIFIED = "unclassified"

# NF-2026-00622 V7 rework: a regex that tries to spot-and-redact secrets
# inside free provider text is provably bypassable -- quoted JSON
# (`{"password": "x"}`), a Python repr (`{'authorization': 'Bearer x'}`), or
# any other shape the pattern author did not anticipate all sail through as
# plain unmatched text. The fix is not a wider regex: it is to never let any
# byte of caller-supplied text (``error``/stdout/stderr) become durable
# diagnostic content at all. Text is inspected transiently, here, only to
# pick one fixed code from this closed vocabulary; the text itself is
# discarded. A diagnostic can therefore never contain a secret in any shape,
# because it is built exclusively from these code words plus small
# system-owned numeric metadata (exit_code, an http status pulled from a
# launcher-owned ``http_status=NNN`` token) -- never a copied substring.
_SIGNATURES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"invalid credential|re-?authenticat", re.I), "auth_invalid_credential"),
    (re.compile(r"cause_not_distinguished", re.I), "auth_cause_not_distinguished"),
    (re.compile(r"unauthoriz", re.I), "auth_unauthorized"),
    (re.compile(r"forbidden|permission denied", re.I), "auth_forbidden"),
    (re.compile(r"rate.?limit|too many requests|\bquota\b", re.I), "rate_limited"),
    (re.compile(r"out of memory|\boom\b", re.I), "resource_exhausted"),
    (re.compile(r"missing required output artifact", re.I), "missing_output_artifact"),
    (
        re.compile(r"heartbeat_lease_and_recovery_grace_exceeded", re.I),
        "liveness_lost",
    ),
    (re.compile(r"connection refused|econnrefused", re.I), "connection_refused"),
    (re.compile(r"timed? ?out", re.I), "provider_timeout"),
    (re.compile(r"provider[ _]refused", re.I), "provider_refused"),
    (re.compile(r"traceback|exception|error|fatal", re.I), "runtime_error"),
)

# The closed vocabulary of DETERMINISTIC CONTROL-PLANE REASONS: reason tokens
# minted by AIWorkHub's own code to state why the control plane refused or
# could not complete a state transition. They are not provider output and not
# exception text -- each is a program constant this repository writes, drawn
# from process_launcher's ``_FINALIZER_CARD_NOT_PROCESSING_REASONS`` plus the
# transition refusals ``mark_terminal_failure``/the finalizer report.
#
# WHY THIS EXISTS. ``_SIGNATURES`` above is a heuristic scan of UNTRUSTED
# provider text; its last entry matches the bare word ``error``. Every
# control-plane reason reaches ``terminal_event_authority`` embedded in a
# wrapper the finalizer builds around exception text -- e.g.
# ``finalizer_retries_exhausted:attempt=1:RuntimeError:...:not_processing`` --
# so the heuristic fired on ``RuntimeError`` and every such terminal event was
# recorded as ``runtime_error``: the module written to classify failures
# destroyed the one precise diagnosis it had. Naming these reasons in their
# own closed vocabulary, scanned first, is what keeps the diagnosis.
#
# WHY THIS IS STILL SECRET-SAFE. The no-copy invariant documented above is
# unchanged: ``_control_plane_code`` returns the module constant from this
# tuple, never a slice of the scanned text. Matching is a whole-token match
# against a fixed literal, so a token can only be recognised, never
# synthesised, from caller text -- an unrecognised string matches nothing here
# and falls through to sanitation exactly as before (fail-closed). This is an
# allowlist, deliberately, and not a "does this look safe" heuristic: a
# heuristic admitting unknown shapes is a secret-leak surface, an allowlist of
# constants cannot be one.
#
# ORDER IS PRIORITY, AND THE LAST ENTRY IS THE OUTERMOST WRAPPER. The finalizer
# nests reasons: ``finalizer_retries_exhausted:<exception text>:finalize_abandoned
# :<cause>``. A wrapper token names only "this machinery gave up"; the cause it
# wraps names *why*. Listing every wrapper strictly after every cause is what
# makes ``_control_plane_code`` return the innermost known reason, and
# ``test_every_control_plane_reason_survives_the_wrapper`` -- parametrized off
# this tuple, against a wrapper that always contains
# ``finalizer_retries_exhausted`` -- is the standing proof that no wrapper ever
# outranks a cause.
_CONTROL_PLANE_REASONS: tuple[str, ...] = (
    # process_launcher._FINALIZER_CARD_NOT_PROCESSING_REASONS -- the target
    # card can no longer be moved by this finalizer (archived/deleted/
    # reclaimed), which is exactly the dead end ``finalize_abandoned`` names.
    "not_processing",
    "not_claimed",
    "task_not_found",
    "claim_owner_mismatch",
    "runner_mismatch",
    "launch_request_mismatch",
    # The finalizer had no task/runner identity to transition at all.
    "request_identity_missing",
    # A losing race against another terminal writer: retryable, and the
    # reason the retry is kept rather than abandoned.
    "terminal_failure_transition_conflict",
    # ------------------------------------------------------------------ #
    # Mandatory-output validation refusals, minted per failing path by
    # ``validate_required_outputs`` (worker_workspace.py:5541-5552, and the
    # identical fallback at process_launcher.py:298-309) as the
    # ``legacy_error_codes`` entries inside one ``required_output_mismatch``
    # diagnostic. Same species as the transition refusals above: each is a
    # literal this repository writes to state why IT refused to promote the
    # attempt -- not provider output, not exception text.
    #
    # They matter because the mismatch diagnostic embeds a JSON object whose
    # key ``legacy_error_codes`` contains the substring ``error``, so
    # ``_SIGNATURES``' catch-all fired on the classifier's OWN structured
    # evidence and every mandatory-output failure was durably recorded as
    # ``runtime_error`` -- the single most reworkable failure class in the
    # system, filed as "something threw".
    #
    # Specific-before-general: the per-path reason outranks the
    # ``required_output_mismatch`` envelope that carries it. (The
    # whole-token matcher already stops ``required_output_unchanged`` from
    # matching inside ``required_output_unchanged_parent_mismatch`` -- ``_``
    # is a word character -- so these two cannot cross-match either way.)
    "required_output_unchanged_parent_mismatch",
    "required_output_unchanged",
    "required_output_zero_bytes",
    "required_output_symlink",
    "required_output_no_matches",
    "required_output_missing",
    "required_output_invalid",
    "required_output_mismatch",
    # ------------------------------------------------------------------ #
    # Supervisor-status and reconciliation terminal-transition refusals.
    # Same species as the groups above: each is a literal this repository
    # writes to state why IT could not complete a terminal transition. Each
    # reaches ``terminal_event_authority`` inside a string whose tail trips
    # ``_SIGNATURES``' catch-all (a ``RuntimeError`` name, or the bare word
    # ``error``), so until they are named here every one of them decayed to
    # ``runtime_error``/``unclassified``.
    #
    # ``token_budget_exceeded`` -- the FSM's own terminal substatus constant
    #   (task_fsm.py:118,133,155,178,210; task_store.py:3188,3724;
    #   callback_bridge.py:86; callback_store.py:561). The supervisor no
    #   longer authorizes a token budget, so a legacy packet still carrying
    #   it is infrastructure failure (process_launcher.py:11175-11178) -- but
    #   the reason must survive to name WHICH stale packet shape was seen.
    # ``supervisor_incomplete`` -- process_launcher.py:11215-11217,
    #   ``f"supervisor_incomplete:state={detail}:rc={supervisor_returncode}"``:
    #   the launcher's verdict when a supervisor status is missing, stale, or
    #   names a state it does not recognise.
    # ``claim_ownership_lost`` -- process_launcher.py:9883, 9886, 9893.
    #   Only the constant prefix is admitted; the ``:claimed_by=``/``:state=``
    #   tails are card data and are never returned.
    # ``write_gate_closed_during_reconciliation`` -- process_launcher.py:11456,
    #   a bare literal assignment with no interpolation at all.
    #
    # ORDER, AND ONE HONEST CAVEAT. Cause-before-envelope is kept, so a legacy
    # packet is diagnosed by the stale state it carries rather than by the
    # generic ``supervisor_incomplete`` envelope that wraps it. Note though
    # that this envelope's ``state={detail}`` slot is filled from
    # ``supervisor_status["state"]`` (process_launcher.py:11214) -- JSON
    # written by an external supervisor process, an OPEN vocabulary. That is
    # still not a leak (``_control_plane_code`` returns this tuple's own
    # constant and copies no byte), but a corrupt supervisor status CAN steer
    # WHICH constant is reported by writing an allowlisted token into its
    # ``state`` field -- ``supervisor_incomplete:state=not_processing``
    # already resolves to ``not_processing`` off group one, whatever this
    # group's internal order. The sink cannot tell a reason AIWorkHub minted
    # from one a supervisor echoed back at it; closing that is a mint-site
    # concern, not a sink-side one.
    "token_budget_exceeded",
    "supervisor_incomplete",
    "claim_ownership_lost",
    "write_gate_closed_during_reconciliation",
    # ------------------------------------------------------------------ #
    # The isolated finalizer's own two early-return refusals. Each reaches
    # this sink as ``<constant>:{exc}`` -- a literal this repository writes,
    # wrapped around an exception tail it does not -- so the catch-all
    # ``traceback|exception|error|fatal`` signature claimed both and every
    # occurrence was durably filed as ``finalize_failed:runtime_error``:
    # "something threw", for two refusals that state precisely what happened.
    # The tail stays caller text and is still never copied; only the prefix
    # is recognised.
    #
    # ``review_workspace_quarantine_failed`` -- process_launcher.py:10684, the
    #   review workspace could not be quarantined after an integrity refusal.
    # ``metadata_invalid`` -- process_launcher.py:10826, the request metadata
    #   file could not be read or parsed, so there is no identity to finalize.
    "review_workspace_quarantine_failed",
    "metadata_invalid",
    # ------------------------------------------------------------------ #
    # WRAPPERS ONLY BELOW THIS LINE -- see the ordering note above.
    #
    # ``process_launcher.py:10345``: ``"finalizer_retries_exhausted:" +
    # "|".join(errors)``, each entry ``f"attempt={n}:{type(exc).__name__}:{exc}"``.
    # The literal prefix is minted by this repository; the ``{exc}`` tails are
    # not, and are exactly why the generic ``exception|error`` heuristic used
    # to win here. Recognising the prefix keeps the one fact the wrapper
    # actually carries -- the isolated finalizer exhausted its retry budget --
    # for the case where nothing more specific is present. Last, always.
    "finalizer_retries_exhausted",
)

# WHAT IS DELIBERATELY *NOT* IN THIS VOCABULARY: the structured evidence that
# travels with a mandatory-output failure. ``validate_required_outputs`` raises
# ``"required_output_mismatch:" + json.dumps({...,"unchanged_mandatory_outputs":
# ["out/result.json"],...})``, and that path list is the actionable half of the
# diagnosis -- it names which declared outputs the attempt failed to change.
#
# It is not admitted here, and ``error`` keeps its constant
# ``<failure_kind>:<code>[:http_status=NNN][:exit_code=N]`` shape, because a
# path is DATA and this tuple is a VOCABULARY. The distinction is not
# stylistic. ``required_outputs`` may be a glob, so the filename the validator
# reports back can be chosen by the worker -- ``out/AKIAIOSFODNN7EXAMPLE.json``
# is a legal filename -- and no character grammar separates that from a benign
# path: every credential shape that matters fits inside ``[A-Za-z0-9._/-]``.
# "Validate the shape, then copy it" is the same bypassable heuristic the
# module docstring above rejects, wearing a schema. An allowlist of constants
# is provably leak-free; an allowlist of *shapes* over caller-influenced bytes
# is not, and the difference is the whole invariant.
#
# The provable version, if the evidence is wanted durably, is a re-mint rather
# than a copy: emit only entries that exactly match the card's own declared
# ``required_outputs`` (card content, manager-authored, already durable), and
# reduce anything a glob discovered to the declared pattern plus a count -- in
# a separate field with its own validated schema, never inside ``error``.
# ``terminal_event_authority`` is not given the declared set, so that belongs
# to the caller in process_launcher.py, not to this module. Until it exists the
# reason travels and the paths do not: a missing detail is a bug, a copied byte
# is a breach.

# Whole-token match only: a reason must be delimited by the separators these
# control-plane strings are actually built from (``:`` ``|`` ``=`` whitespace)
# or by a string boundary, so ``not_claimed`` can never be matched inside a
# longer unrelated word. Compiled once, in priority order.
_CONTROL_PLANE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(r"(?:\A|[^0-9A-Za-z_])" + re.escape(t) + r"(?:\Z|[^0-9A-Za-z_])"), t)
    for t in _CONTROL_PLANE_REASONS
)


def _control_plane_code(text: str) -> str | None:
    """Return the module's own constant for the first control-plane reason
    present in ``text``, else ``None``.

    Never returns a substring of ``text``: the value is always the element of
    ``_CONTROL_PLANE_REASONS`` that matched, so no caller-supplied byte can
    reach a durable diagnostic through this path.
    """
    for pattern, reason in _CONTROL_PLANE_PATTERNS:
        if pattern.search(text):
            return reason
    return None


# Digits only -- the surrounding ``http_status=`` token is a launcher-owned
# format (see process_launcher._provider_auth_failure_from_output), and a
# bare 3-digit capture can never itself carry secret material.
_HTTP_STATUS = re.compile(r"\bhttp_status=(\d{3})\b")

# A real process exit code never exceeds this magnitude; anything larger is
# not process-exit metadata, so it is treated the same as any other invalid
# shape (NF-2026-00622 V7 rework-of-rework: boundary-hardening finding).
_EXIT_CODE_BOUND = 2**31 - 1


def normalize_exit_code(value: Any) -> int | None:
    """Coerce untrusted supervisor/status ``exit_code`` metadata to a safe,
    bounded ``int`` or ``None``.

    ``supervisor_status`` is a JSON file the finalizer reads back from an
    external supervisor process, so its ``exit_code`` field carries no type
    guarantee at all -- a secret-bearing string, a bool, a float, a
    dict/list, or an out-of-range magnitude must never reach diagnostic/error
    formatting or a durable ``exit_code`` field as attacker-controlled
    content. Only a genuine ``int`` (``bool`` is deliberately excluded even
    though it is an ``int`` subclass) within a sane bounded magnitude is
    trusted; every other shape normalizes to ``None``, which classifies as an
    invalid/closed state rather than silently formatting the raw value.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if abs(value) > _EXIT_CODE_BOUND:
        return None
    return value


def _signature_code(text: str) -> str | None:
    for pattern, code in _SIGNATURES:
        if pattern.search(text):
            return code
    return None


def _classify_evidence(*sources: str, control_plane: str = "") -> str:
    """Pick the first matching closed-vocabulary code across ``sources``, in order.

    Sources are scanned but never copied -- callers pass stderr before stdout
    before ``error`` so a specific provider signal always outranks the
    generic supervisor-wrapper string.

    ``control_plane`` is the AIWorkHub-minted ``error`` string (and only that
    string -- never a provider log tail). It is scanned first, against the
    closed ``_CONTROL_PLANE_REASONS`` vocabulary, so a deterministic reason
    this system minted about its own state transition is reported as itself
    instead of decaying into whichever provider heuristic its embedded
    exception text happens to trip (almost always ``runtime_error``). A
    control-plane refusal is why the terminal event exists at all, so it
    outranks provider text, which describes something that happened earlier
    and did not cause this outcome. Unrecognised text falls straight through
    to the provider signatures and, failing those, ``_UNCLASSIFIED``.
    """
    if control_plane:
        code = _control_plane_code(control_plane)
        if code is not None:
            return code
    for source in sources:
        if not source:
            continue
        code = _signature_code(source)
        if code is not None:
            return code
    return _UNCLASSIFIED


def _http_status(text: str) -> str | None:
    match = _HTTP_STATUS.search(text)
    return match.group(1) if match else None


def _assemble_diagnostic(
    failure_kind: str, *, code: str, exit_code: int | None, http_status: str | None,
) -> str:
    parts = [failure_kind, code]
    if http_status is not None:
        parts.append(f"http_status={http_status}")
    if exit_code is not None:
        parts.append(f"exit_code={exit_code}")
    return ":".join(parts)[:MAX_DIAGNOSTIC_CHARS]


def _read_log_tail(path: str | Path | None, *, max_bytes: int = MAX_TAIL_READ_BYTES) -> str:
    """Read up to ``max_bytes`` from the end of ``path``, exactly once, transiently.

    The result is fed only to ``_classify_evidence`` for code selection and is
    never itself persisted, so a tail read landing mid-secret or mid-PEM-body
    carries no durability risk. Never raises on a missing, unreadable, or
    foreign-owned file -- absent log evidence must fall back to the generic
    supervisor error, not crash terminal finalization. Symlink-safe: a
    pre-open ``is_symlink`` check plus an O_NOFOLLOW-guarded open, since a log
    path can be replaced by a symlink to an arbitrary host file between
    process exit and terminal finalization reading it.
    """
    if not path:
        return ""
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        return ""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(target, flags)
    except OSError:
        return ""
    try:
        with os.fdopen(fd, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - max_bytes)
            handle.seek(start)
            data = handle.read(max_bytes)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def classify_terminal_failure(
    *,
    state: str | None,
    exit_code: int | None,
    error: str | None,
    stdout_tail: str | None = None,
    stderr_tail: str | None = None,
    cancelled: bool = False,
) -> dict[str, Any]:
    """Classify one terminal event into a stable failure_kind + closed-vocabulary diagnostic.

    Computed exactly once at terminal finalization and persisted; later
    GC/retention overlay events must never recompute it. ``diagnostic`` is
    built only from a fixed code vocabulary plus ``exit_code``/``http_status``
    -- ``error``/``stdout_tail``/``stderr_tail`` are inspected transiently to
    pick a code and are never themselves written into it, so no shape of
    embedded secret (labelled, quoted JSON, Python repr, or anything else)
    can ever reach durable diagnostic state.

    ``launch_failed`` is classified on ``state`` alone, independent of
    ``exit_code``: a missing/malformed/stale supervisor status resolves
    ``exit_code`` to ``None`` even when the caller already found a stable
    provider auth-refusal reason in ``error``, and that verdict must never
    fall through to the no-failure default.
    """

    state_norm = str(state or "").strip().lower()
    exit_code = normalize_exit_code(exit_code)

    if cancelled or state_norm in _NO_VERDICT_STATES:
        return {
            "failure_kind": None,
            "diagnostic": "",
        }

    error_text = str(error or "")
    code = _classify_evidence(
        str(stderr_tail or ""), str(stdout_tail or ""), error_text,
        control_plane=error_text,
    )
    http_status = _http_status(error_text)

    if state_norm in _TIMEOUT_STALL_STATES:
        return {
            "failure_kind": "timeout_stall",
            "diagnostic": _assemble_diagnostic(
                "timeout_stall", code=code, exit_code=exit_code, http_status=http_status,
            ),
        }

    if state_norm in _LAUNCH_FAILED_STATES:
        return {
            "failure_kind": "launch_failed",
            "diagnostic": _assemble_diagnostic(
                "launch_failed", code=code, exit_code=exit_code, http_status=http_status,
            ),
        }

    if state_norm == "worker_failed":
        return {
            "failure_kind": "worker_failed",
            "diagnostic": _assemble_diagnostic(
                "worker_failed", code=code, exit_code=exit_code, http_status=http_status,
            ),
        }

    if state_norm in _NAMED_FAILURE_KINDS:
        failure_kind = _NAMED_FAILURE_KINDS[state_norm]
        return {
            "failure_kind": failure_kind,
            "diagnostic": _assemble_diagnostic(
                failure_kind, code=code, exit_code=exit_code, http_status=http_status,
            ),
        }

    if exit_code not in (None, 0):
        return {
            "failure_kind": "nonzero_exit",
            "diagnostic": _assemble_diagnostic(
                "nonzero_exit", code=code, exit_code=exit_code, http_status=http_status,
            ),
        }

    return {
        "failure_kind": None,
        "diagnostic": "",
    }


def classify_terminal_failure_from_paths(
    *,
    state: str | None,
    exit_code: int | None,
    error: str | None,
    stdout_path: str | Path | None = None,
    stderr_path: str | Path | None = None,
    cancelled: bool = False,
) -> dict[str, Any]:
    """Read each log's bounded tail exactly once, then classify with it.

    Terminal finalization is the only caller: the read happens here, once,
    at terminal time. Collect/status rehydration must reuse the persisted
    ``failure_kind``/``diagnostic`` instead of calling this again against
    logs that may since have rotated, been GC'd, or moved.
    """
    state_norm = str(state or "").strip().lower()
    exit_code = normalize_exit_code(exit_code)
    needs_provider_evidence = not cancelled and (
        state_norm == "worker_failed"
        or state_norm in _LAUNCH_FAILED_STATES
        or state_norm in _NAMED_FAILURE_KINDS
        or (state_norm not in _NO_VERDICT_STATES and exit_code not in (None, 0))
    )
    stdout_tail = _read_log_tail(stdout_path) if needs_provider_evidence else None
    stderr_tail = _read_log_tail(stderr_path) if needs_provider_evidence else None
    return classify_terminal_failure(
        state=state,
        exit_code=exit_code,
        error=error,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        cancelled=cancelled,
    )


def safe_error_text(*, state: str | None, exit_code: int | None, error: str | None) -> str:
    """Bounded, closed-vocabulary-derived text safe for any durable/public
    error/reason/evidence surface, independent of the failure verdict.

    Content sanitation is orthogonal to ``classify_terminal_failure``'s
    verdict: a cancelled or otherwise successful terminal outcome carries no
    ``failure_kind``/``diagnostic`` of its own, but a pre-existing ``error``
    on that same outcome (e.g. a stale secret-shaped supervisor-status field)
    must still never reach a durable/public surface unsanitized. Built the
    same way ``diagnostic`` is -- a fixed code plus ``exit_code``/
    ``http_status``, never a copied substring -- so every terminal outcome
    gets one safe public error representation regardless of what produced
    the raw text. Returns ``""`` when ``error`` is empty: there is nothing to
    sanitize.
    """
    if not error:
        return ""
    error_text = str(error)
    code = _classify_evidence(error_text, control_plane=error_text)
    http_status = _http_status(error_text)
    label = str(state or "").strip().lower() or "unknown"
    return _assemble_diagnostic(
        label, code=code, exit_code=normalize_exit_code(exit_code), http_status=http_status,
    )


# --------------------------------------------------------------------------- #
# FINALIZER RETRY CLASSIFICATION.
#
# Moved here from process_launcher (whose descending size ratchet forced the
# subject out, and this is its natural home: the module that decides what a
# terminal attempt MEANS). Nothing else referenced these four names, so the
# move is mechanical; process_launcher re-exports them under their original
# names so every existing reader resolves the same objects.
#
# Finalizer retry exhaustion is not one outcome, and terminalising every shape
# of it as ``finalize_failed`` blocks cards a later pass would have finalized
# cleanly. Exactly two shapes exist, and only one of them may be re-armed.
#
# TRANSIENT -- an attempt that ended on one of these exception types. They name
# a contended or temporarily-unavailable resource, never a decided outcome:
# ``OSError`` (and its subclasses ``PermissionError``/``BlockingIOError``/
# ``TimeoutError`` -- the Windows file and antivirus races the finalizer's own
# docstring names) and ``sqlite3.OperationalError`` (``database is locked``,
# whose default busy window is measured in seconds, i.e. more than an order of
# magnitude wider than that loop's whole 250ms budget). A later reconcile pass
# against an uncontended boundary can still succeed.
#
# PERMANENT -- everything else, including an attempt that returned no terminal
# event at all. ``_finalize_isolated_request`` has six ``None`` returns; five
# of them (an absent status hint, unreadable metadata, an absent status
# artifact, and a live-and-progressing liveness verdict) are each guarded by
# ``PidIdentityVerdict.MATCH`` and are already resolved by the caller's own
# MATCH branch before any error is recorded, while ``UNKNOWN`` already defers.
# The only ``None`` that can survive to exhaustion under a MISMATCH verdict is
# the missing or unusable request metadata file -- and ``retry_finalization``
# refuses precisely that condition, forever, as
# ``finalization_retry_metadata_invalid``. Re-arming it could only loop with no
# possible progress, so it stays terminal. An empty request history is likewise
# already handled: it carries no task identity, so no terminal transition is
# even attempted and the existing ``reconcile_pending`` fallback keeps it.
FINALIZER_TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError,
    sqlite3.OperationalError,
)

# A transient cause must not defer without bound. A deterministic bug that
# raises a transient-shaped error on every single pass has to settle rather
# than re-arm forever, so each deferral is recorded on the request and counted;
# once the budget is spent the original terminal path runs. Five deferrals is
# roughly two and a half minutes of extra grace at the reconciler's thirty
# second cadence -- ample for any lock window, and strictly finite.
FINALIZER_TRANSIENT_DEFERRAL_BUDGET = 5
FINALIZER_TRANSIENT_DEFERRAL_REASON = "finalizer_transient_error"


def finalizer_attempt_is_transient(cause: BaseException | None) -> bool:
    """True only when an exhausted finalization attempt ended on a contended or
    temporarily-unavailable boundary.

    ``None`` -- the attempt produced no terminal event -- is a decided outcome,
    never a retry; see ``FINALIZER_TRANSIENT_EXCEPTIONS`` for the evidence.
    """
    return cause is not None and isinstance(cause, FINALIZER_TRANSIENT_EXCEPTIONS)


# --------------------------------------------------------------------------- #
# TYPED REASONS -- the other half of the split, applied at the MINT site.
#
# The allowlist above is a SINK-side defence over an untrusted string channel
# (provider logs, exception tails, rows another writer already persisted, a
# foreign supervisor). It stays exactly as it is; it is the fail-closed floor.
#
# What it cannot do is carry STRUCTURE. ``_assemble_diagnostic`` emits one
# fixed ``<failure_kind>:<code>[:http_status=NNN][:exit_code=N]`` shape, so a
# launcher reason such as ``supervisor_incomplete:state=token_budget_exceeded``
# has nowhere to put its ``state=`` field and decays to the bare code: the
# system knew precisely which stale packet shape it saw and recorded only that
# something was incomplete. Three of this repository's own terminal reasons
# are structured that way.
#
# ``TerminalReason`` is that missing channel, minted where the launcher ALREADY
# HOLDS the constant, so nothing has to be recovered from prose downstream.
# Typed-at-mint and allowlist-at-sink are complements: a call site that knows
# its reason states it; every other string still goes through sanitation.
#
# IT IS DELIBERATELY NOT A ``str`` SUBCLASS. A str subclass would leave every
# existing ``f"{reason}:{tail}"`` compiling, silently reproducing an untyped
# string, and the marking would erode invisibly one interpolation at a time.
# A frozen, non-str dataclass makes that a type error at the point of misuse;
# ``render()`` is the single, explicit exit back to text.
#
# NO FREE TEXT CAN ENTER IT. ``code`` and every string-valued detail is passed
# through ``constant()``, which returns THIS MODULE'S OWN element rather than
# the caller's argument -- so the no-copy invariant stated in the module
# docstring holds for typed reasons exactly as it does for allowlisted
# diagnostics. Numeric detail is bounded by ``_EXIT_CODE_BOUND``. A caller that
# tries to smuggle a path, an exception tail or a provider message through gets
# ``unrecognized`` back, never its own bytes.
# --------------------------------------------------------------------------- #

_UNRECOGNIZED = "unrecognized"

# ``runtime_adapters.classify_provider_outcome``'s closed reason vocabulary,
# named here rather than imported because this module deliberately depends on
# nothing outside the standard library (every terminal writer imports it).
# ``test_provider_refusal_vocabulary_matches_runtime_adapters`` is the standing
# proof that the two cannot drift apart.
_PROVIDER_REFUSAL_REASONS: tuple[str, ...] = (
    "provider_refused",
    "cause_not_distinguished_by_response",
    "provider_refused_credential_rejected_needs_new_credential",
    # ``claude_auth.RUNTIME_AUTH_FAILURE_REASON`` -- the subscription-session
    # circuit's own verdict. Minted by this repository, not by a provider.
    "claude_subscription_session_refresh_required",
) + tuple(
    f"provider_refused_{kind}{tail}"
    for kind in (
        "credential_rejected", "quota_exhausted", "rate_limited",
        "balance_exhausted", "session_limit", "provider_unavailable",
        "cause_not_distinguished",
    )
    for tail in (
        "", "_recoverable_after_reported_window",
        "_recoverable_but_reset_window_unreported",
    )
)

# Supervisor/launcher terminal-state names. A supervisor status ``state`` is
# EXTERNAL JSON, so a state read from one is RECOGNISED against this tuple and
# never copied out of the packet -- selection is possible, synthesis is not
# (the same honest caveat the ``_CONTROL_PLANE_REASONS`` note records).
_TERMINAL_STATE_NAMES: tuple[str, ...] = (
    "starting", "running", "exited", "spawn_failed", "supervisor_error",
    "timed_out", "stalled", "liveness_lost", "cancelled", "canceled",
    "launch_failed", "worker_failed", "missing", "reconcile_pending",
    "review_pending", "release_pending", "blocked",
    # The supervisor's own output-budget refusal token, travelling in its
    # status ``error`` field. Recognising it is what keeps a byte-cap refusal
    # from being filed as ``unclassified``.
    "captured_output_bytes",
) + tuple(_NAMED_FAILURE_KINDS)

# Launcher-minted reasons that are not transition refusals and so have no place
# in ``_CONTROL_PLANE_REASONS``, but which a typed reason may still state: the
# two cancellation/timeout verdicts ``_finalize_isolated_request`` writes.
_LAUNCHER_MINTED_REASONS: tuple[str, ...] = (
    "worker_cancelled",
    "worker_timed_out",
)

# The one registry every typed reason draws from. Values are the module's own
# constants; ``constant()`` hands back the KEY object, never the argument.
_REASON_CONSTANTS: dict[str, str] = {
    token: token
    for token in (
        _CONTROL_PLANE_REASONS
        + _PROVIDER_REFUSAL_REASONS
        + _TERMINAL_STATE_NAMES
        + _LAUNCHER_MINTED_REASONS
        + tuple(code for _pattern, code in _SIGNATURES)
        + (_UNCLASSIFIED,)
    )
}

# Detail field names a typed reason may use -- closed, like everything else.
_REASON_FIELDS: frozenset[str] = frozenset({
    "state", "supervisor_state", "exit_code", "rc", "http_status",
    "cap_bytes", "observed_bytes", "timeout_seconds", "attempt",
})

# The only re-mintable structured evidence: mandatory-output paths, and only
# those that are exact members of the CARD's declared ``required_outputs``.
_REMINT_FIELDS: tuple[str, ...] = (
    "missing_required_artifacts",
    "unchanged_mandatory_outputs",
)

# A bounded ASCII-digits guard before ``int()``. ``str.isdigit()`` alone is not
# one: it is True for Unicode forms ``int()`` then refuses (``"²"``), and
# an unbounded digit run is neither an exit code nor an HTTP status.
_MAX_DETAIL_DIGITS = 12


def _is_bounded_digits(value: str) -> bool:
    digits = value.lstrip("-")
    return bool(digits) and digits.isascii() and digits.isdigit() and len(digits) <= _MAX_DETAIL_DIGITS


def constant(value: object) -> str:
    """Return THIS module's own element equal to ``value``, else ``unrecognized``.

    Never returns the argument. The object handed back is the registry key, so
    a caller's bytes can no more travel through a typed reason than they can
    through ``_control_plane_code``.
    """
    return _REASON_CONSTANTS.get(str(value or ""), _UNRECOGNIZED)


def _detail_value(value: object) -> int | str:
    """Ints stay bounded ints; every other shape must be a module constant."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value if abs(value) <= _EXIT_CODE_BOUND else _UNRECOGNIZED
    return constant(value)


@dataclass(frozen=True, slots=True)
class TerminalReason:
    """One deterministic control-plane reason, as a VALUE rather than a string.

    ``code`` names why the control plane produced this terminal event.
    ``detail`` carries the structured fields the allowlisted diagnostic shape
    has no room for: ``(field, value)`` renders ``field=value``, and
    ``("", value)`` renders a bare trailing token. ``outputs`` carries the one
    re-minted list this repository allows (see ``required_output_reason``).

    Both scalar slots are normalised at construction, so an invalid reason
    cannot exist: there is no state of this object in which a caller-supplied
    byte is held.
    """

    code: str
    detail: tuple[tuple[str, int | str], ...] = ()
    outputs: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", constant(self.code))
        object.__setattr__(self, "detail", tuple(
            (field if field in _REASON_FIELDS else "", _detail_value(value))
            for field, value in self.detail
        ))
        # ``outputs`` is the single slot whose safety rests on its mint
        # function rather than on a module-owned registry, because the registry
        # it is checked against -- the card's declared ``required_outputs`` --
        # is per-card and cannot live here. ``required_output_reason`` is the
        # only sanctioned mint, and it emits the CARD's own entries. The field
        # names are still closed.
        object.__setattr__(self, "outputs", tuple(
            (field, tuple(str(entry) for entry in entries))
            for field, entries in self.outputs
            if field in _REMINT_FIELDS and entries
        ))

    def render(self) -> str:
        """The bounded public string form -- the single exit from the type."""
        parts = [self.code]
        for field, value in self.detail:
            parts.append(f"{field}={value}" if field else str(value))
        for field, entries in self.outputs:
            parts.append(json.dumps({field: list(entries)}, separators=(",", ":")))
        return ":".join(parts)[:MAX_DIAGNOSTIC_CHARS]


def recognised_reason(text: str | None) -> TerminalReason | None:
    """Re-mint ``text`` as a typed reason IFF every one of its ``:``-separated
    tokens is already a module constant or a bounded ``field=<int|constant>``.

    This is the allowlist applied to a WHOLE string instead of to one code. A
    string that parses completely contains, by construction, no byte this
    module did not already own, so re-emitting it is a re-mint and not a copy:
    every part returned is a registry element or a bounded int. A single
    unrecognised token -- an exception tail, a filesystem path, a provider
    message, a card's ``claimed_by`` value -- fails the whole parse and returns
    ``None``, and the caller falls straight back to sanitation. Fail-closed.

    It exists for the strings AIWorkHub minted in ANOTHER process, chiefly the
    supervisor status ``error`` field, which Step 1 deliberately does not
    re-schema. Recognising such a string cannot leak, but it does let external
    JSON SELECT which known reason is reported; the spoofing surface is named
    at the read site in process_launcher.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    tokens = raw.split(":")
    if tokens[0] not in _REASON_CONSTANTS:
        return None
    detail: list[tuple[str, int | str]] = []
    for token in tokens[1:]:
        field, separator, value = token.partition("=")
        if not separator:
            if token not in _REASON_CONSTANTS:
                return None
            detail.append(("", token))
        elif field not in _REASON_FIELDS:
            return None
        elif _is_bounded_digits(value):
            detail.append((field, int(value)))
        elif value in _REASON_CONSTANTS:
            detail.append((field, value))
        else:
            return None
    return TerminalReason(tokens[0], tuple(detail))


def supervisor_failure_reason(state: object, exit_code: int | None) -> TerminalReason:
    """``worker_failed:supervisor_state=<state>[:exit_code=<n>]``, typed.

    The launcher reaches this only inside a closed ``supervisor_state`` branch,
    and the state is still recognised rather than copied so the value written
    is this module's constant even there.
    """
    detail: tuple[tuple[str, int | str], ...] = (("supervisor_state", constant(state)),)
    if exit_code is not None:
        detail += (("exit_code", exit_code),)
    return TerminalReason("worker_failed", detail)


def supervisor_incomplete_reason(state: object, returncode: object) -> TerminalReason:
    """``supervisor_incomplete:state=<state>[:rc=<n>]``, typed.

    ``state`` is the launcher's verdict when a supervisor status is missing,
    stale, or names something it does not recognise -- so this is exactly the
    reason whose ``state=`` slot the allowlisted diagnostic shape could not
    carry, and the reason a stale packet used to be filed as merely
    ``supervisor_incomplete`` with the packet shape it named thrown away.
    """
    detail: tuple[tuple[str, int | str], ...] = (("state", constant(state or "missing")),)
    rc = normalize_exit_code(returncode)
    if rc is not None:
        detail += (("rc", rc),)
    return TerminalReason("supervisor_incomplete", detail)


def workspace_error_reason(
    text: str | None, declared_outputs: Sequence[str] | None = None,
) -> TerminalReason | None:
    """Type the launcher-minted PREFIX of a ``WorkspaceError`` message.

    A workspace error reads ``<constant>:<caller text>`` -- the prefix is a
    reason this repository mints, the tail is a path, a card field
    (``claimed_by=``) or an exception. Only the prefix is typed, and only when
    it is a deterministic control-plane reason; anything else returns ``None``
    and keeps the existing sanitation verbatim.

    The single exception is the mandatory-output mismatch, whose actionable
    half is the list of declared outputs the attempt failed to change. Those
    entries are RE-MINTED from ``declared_outputs`` -- the card's own
    manager-authored ``required_outputs``, already durable -- by membership, so
    the strings that travel are the CARD's and never the validator's. This is
    the re-mint the ``WHAT IS DELIBERATELY *NOT* IN THIS VOCABULARY`` note
    above prescribes, now that a caller can supply the declared set.
    """
    raw = str(text or "")
    code = raw.partition(":")[0]
    if code not in _WORKSPACE_ERROR_REASONS:
        return None
    if not code.startswith("required_output"):
        return TerminalReason(code)
    return required_output_reason(code, raw.partition(":")[2], declared_outputs)


def required_output_reason(
    code: str, payload_text: str, declared_outputs: Sequence[str] | None,
) -> TerminalReason:
    """Re-mint a mandatory-output refusal from the card's DECLARED outputs.

    ``payload_text`` is read only to decide WHICH declared entries to name; not
    one of its bytes is emitted. An entry appears in the result exactly when it
    is an element of ``declared_outputs`` that the payload also names, and the
    object emitted is the declared element -- so a glob-discovered filename a
    worker chose can never reach durable state, while the manager-authored path
    it corresponds to can.
    """
    declared = tuple(str(entry) for entry in (declared_outputs or ()))
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError):
        return TerminalReason(code)
    if not isinstance(payload, dict) or not declared:
        return TerminalReason(code)
    outputs: list[tuple[str, tuple[str, ...]]] = []
    for field in _REMINT_FIELDS:
        observed = payload.get(field)
        if not isinstance(observed, list):
            continue
        kept = tuple(entry for entry in declared if entry in observed)
        if kept:
            outputs.append((field, kept))
    return TerminalReason(code, (), tuple(outputs))


# Only a DETERMINISTIC CONTROL-PLANE REASON may be typed out of a workspace
# error at all -- a terminal-state name or a provider signature code appearing
# at the head of an exception message is not evidence that the message IS that
# reason -- and, within those, only the two the ALLOWLISTED DIAGNOSTIC SHAPE
# PROVABLY CANNOT CARRY. That restriction is deliberate and is the whole of
# Step 1's blast radius:
#
# ``claim_ownership_lost`` -- the terminal state the launcher pairs it with is
#   the generic ``finalize_failed``, so the diagnostic ``finalize_failed:
#   claim_ownership_lost:exit_code=0`` MISNAMES the event: nothing failed to
#   finalize, a claim moved. The launcher's own control flow keys on the
#   reason's prefix (``ownership_lost = error.startswith(...)``), which is the
#   evidence that the reason, not the wrapper, is what this event is.
# ``required_output_mismatch`` -- the only reason carrying structured evidence
#   (which declared outputs went unchanged) that the fixed shape has no slot
#   for. See ``required_output_reason``.
#
# Every other control-plane reason is already named correctly by the
# diagnostic, whose ``<failure_kind>:<code>[:exit_code=N]`` form states both
# halves; typing those too would only change the string that has been the
# settled authority triple's ``error`` since NF-2026-00622 (``error`` equals
# ``diagnostic`` on that path, and tests pin it). Widening this set is Step 2
# work and must revisit that contract deliberately, not as a side effect.
_WORKSPACE_ERROR_REASONS: frozenset[str] = frozenset({
    "claim_ownership_lost",
    "required_output_mismatch",
})


def terminal_event_authority(
    *,
    state: str | None,
    exit_code: int | None,
    error: str | None,
    reason: TerminalReason | None = None,
    stdout_path: str | Path | None = None,
    stderr_path: str | Path | None = None,
    cancelled: bool = False,
) -> dict[str, Any]:
    """The one durable ``failure_kind``/``diagnostic``/``error`` triple for a
    terminal event, and the only call every terminal-state append site may
    use to build them.

    NF-2026-00622 V7 rework: a metadata-parse early return once hand-built a
    ``finalize_failed`` event with a raw ``error`` string, skipping
    classification and sanitation entirely because it returned before the
    isolated finalizer's own local authority closure was ever defined. That
    bypass class -- any call site constructing ``failure_kind``/
    ``diagnostic``/``error`` by hand instead of through one shared function --
    is what this closes: every terminal-state append (an early return, the
    direct/non-isolated monitor, the isolated finalizer and its retry-
    exhaustion fallback) must route through this function, never assemble
    those fields itself.

    TWO CHANNELS, ONE SINK. ``error`` is the UNTRUSTED string channel: provider
    output, an exception tail, a row another writer persisted, a foreign
    supervisor's status field. It is never copied -- it selects a code from the
    closed vocabularies and is then discarded, exactly as before.

    ``reason`` is the TYPED channel, supplied by a call site that already holds
    the constant it is reporting. A ``TerminalReason`` cannot contain a byte
    the caller supplied (see its class docstring), so it is emitted as itself
    and can carry the structured detail -- ``state=``, ``http_status=``,
    ``exit_code=``, a re-minted declared-output list -- that the fixed
    ``<failure_kind>:<code>`` diagnostic shape has no room for. The two are
    complements, not alternatives: a reason is stated where it is known, and
    every other string still goes through sanitation.

    ``diagnostic`` is ALWAYS the classifier's own shape-guarded verdict, on
    both channels, so the field that downstream shape checks pin never widens.
    Only ``error`` -- the human/operator-facing reason -- takes the typed value
    when one is supplied, else the diagnostic when a failure verdict was found,
    else the orthogonal ``safe_error_text`` sanitation, so even a verdict-free
    terminal outcome never carries a raw/secret-shaped string.
    """
    verdict = classify_terminal_failure_from_paths(
        state=state,
        exit_code=exit_code,
        error=error,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        cancelled=cancelled,
    )
    if isinstance(reason, TerminalReason):
        safe_error = reason.render()
    elif verdict.get("failure_kind") is not None:
        safe_error = verdict["diagnostic"]
    else:
        safe_error = safe_error_text(state=state, exit_code=exit_code, error=error)
    return {**verdict, "error": safe_error}


# --------------------------------------------------------------------------- #
# FAILURE DISPOSITION -- transient / credential / defect (R4, NF-2026-00646).
#
# THE PROBLEM. Every terminal reason above answers "what happened". None answers
# "may this be tried again", so a provider at capacity, an expired credential
# and a genuinely broken card all end identically at ``blocked`` with a bare
# ``exit_code=1``. Measured on this repository: 40 of 147 blocked cards over 30
# days died on exactly that string, and one of them burned 190.8M tokens and
# $46.07 before its credential expired at the END of the run -- with the
# finished work then thrown away by the ``launch_failed`` cleanup path.
#
# THE RULE. A disposition is only ever read off something the provider or this
# repository ASSERTED: a typed field in the provider's own terminal envelope, an
# HTTP status it returned, a refusal kind ``runtime_adapters`` already named, or
# a control-plane constant AIWorkHub minted about its own refusal. Nothing here
# matches prose. ``_SIGNATURES`` above is deliberately NOT an input: it is a
# heuristic scan whose last entry matches the bare word ``error``, and a
# heuristic that can call a defect "transient" retries a broken card until its
# budget is spent. Where the evidence names no class the answer is ``unknown``,
# which behaves exactly as today.
#
# FAIL-CLOSED, PER CLASS -- the direction is NOT the same for all three, because
# the cost of being wrong is not the same:
#
#   transient  PROVEN ONLY, strictest. A wrong ``transient`` re-runs a real
#              defect for its whole retry budget and hides the diagnosis, so
#              only a provider-asserted retryable status or an already-named
#              recoverable refusal kind may set it. Unproven is never transient.
#              (Note this is the OPPOSITE direction from
#              ``dependency_autolaunch.TRANSIENT_DENIAL_REASONS`` and
#              ``task_reconciler.classify_lock_failure``, where unproven means
#              transient: a denied launch has not spent anything yet, so
#              retrying it is nearly free, while a terminal failure has already
#              consumed a whole card cycle.)
#   credential PROVEN ONLY, but a WIDER evidence set is admitted, because being
#              wrong here is cheap and reversible: the lane pauses and the owner
#              is told, the card is left holding its work, and nothing is
#              retried or destroyed. Missing a credential failure is what costs
#              $46.07; calling one falsely costs one operator message.
#   defect     PROVEN ONLY. Only AIWorkHub's OWN validator/scope refusals -- the
#              constants this repository mints to say the declared work did not
#              happen -- earn it. A defect is never inferred from a provider
#              message, and never from an exit code alone.
#   unknown    everything else, including a bare 401/403 whose body names no
#              cause (NF-2026-00326: a dead key, an expired token and a rate
#              condition are indistinguishable from that status). Behaves as
#              today, and records that the cause was not established rather than
#              pretending one was.
#
# NO BYTE OF PROVIDER TEXT SURVIVES. ``provider_terminal_signal`` parses typed
# JSON and returns THIS module's own constants plus a bounded int status; an
# unrecognised machine code becomes ``unrecognized``. The module docstring's
# no-copy invariant therefore holds here exactly as it does for diagnostics.
# --------------------------------------------------------------------------- #

FAILURE_CLASS_TRANSIENT = "transient"
FAILURE_CLASS_CREDENTIAL = "credential"
FAILURE_CLASS_DEFECT = "defect"
FAILURE_CLASS_UNKNOWN = "unknown"

FAILURE_CLASSES: frozenset[str] = frozenset({
    FAILURE_CLASS_TRANSIENT,
    FAILURE_CLASS_CREDENTIAL,
    FAILURE_CLASS_DEFECT,
    FAILURE_CLASS_UNKNOWN,
})

# ``runtime_adapters``' refusal-kind vocabulary, plus the one kind
# ``process_launcher._provider_model_rejection_from_output`` mints. Named here
# rather than imported for the same reason ``_PROVIDER_REFUSAL_REASONS`` is;
# ``test_refusal_kind_vocabulary_matches_runtime_adapters`` is the drift proof.
PROVIDER_REFUSAL_KINDS: tuple[str, ...] = (
    "session_limit", "quota_exhausted", "balance_exhausted", "rate_limited",
    "credential_rejected", "provider_unavailable", "cause_not_distinguished",
    "model_not_found",
)

# Every refusal kind, classified by reading what raises it, never its name.
#
# ``session_limit``/``quota_exhausted``/``rate_limited`` are exactly
# ``runtime_adapters._RECOVERABLE_REFUSALS`` -- the provider itself reports a
# reset window for them. ``provider_unavailable`` is a 5xx upstream outage.
# ``balance_exhausted`` is an HTTP 402 dead account: only added credit clears
# it, never elapsed time, so it is emphatically NOT transient -- it needs the
# owner, which is what ``credential`` means here. ``credential_rejected`` needs
# a new credential. ``cause_not_distinguished`` is the honest verdict for a
# bare 401/403 and must stay unplaced. ``model_not_found`` is the route being
# unusable for this account rather than the card being wrong: the work is
# retried, and R1's ``workforce_catalog`` circuit is what makes the retry land
# on a different route instead of the same wall.
REFUSAL_KIND_DISPOSITION: dict[str, str] = {
    "session_limit": FAILURE_CLASS_TRANSIENT,
    "quota_exhausted": FAILURE_CLASS_TRANSIENT,
    "rate_limited": FAILURE_CLASS_TRANSIENT,
    "provider_unavailable": FAILURE_CLASS_TRANSIENT,
    "model_not_found": FAILURE_CLASS_TRANSIENT,
    "balance_exhausted": FAILURE_CLASS_CREDENTIAL,
    "credential_rejected": FAILURE_CLASS_CREDENTIAL,
    "cause_not_distinguished": FAILURE_CLASS_UNKNOWN,
}

# HTTP statuses the PROVIDER returned in its own terminal envelope.
# 408/425/429 and the 5xx family are retryable by the specification that
# defines them. 402 PAYMENT REQUIRED is an account condition. 401/403 name no
# cause on their own and stay unplaced (NF-2026-00326). 400/404/409 are listed
# so that a status which reaches this table can never fall through unnoticed --
# they are unplaced on purpose, and only a machine CODE can place them.
PROVIDER_STATUS_DISPOSITION: dict[int, str] = {
    408: FAILURE_CLASS_TRANSIENT,
    425: FAILURE_CLASS_TRANSIENT,
    429: FAILURE_CLASS_TRANSIENT,
    500: FAILURE_CLASS_TRANSIENT,
    502: FAILURE_CLASS_TRANSIENT,
    503: FAILURE_CLASS_TRANSIENT,
    504: FAILURE_CLASS_TRANSIENT,
    529: FAILURE_CLASS_TRANSIENT,
    402: FAILURE_CLASS_CREDENTIAL,
    400: FAILURE_CLASS_UNKNOWN,
    401: FAILURE_CLASS_UNKNOWN,
    403: FAILURE_CLASS_UNKNOWN,
    404: FAILURE_CLASS_UNKNOWN,
    409: FAILURE_CLASS_UNKNOWN,
}

# Machine error codes carried in a typed field of a provider terminal envelope
# -- never prose. The OAuth block is RFC 6749 section 5.2: a token endpoint
# answers with an ``error`` member drawn from a fixed enumeration, and the four
# listed there each mean the credential this lane holds will not be accepted
# again without operator action. That is a machine field of a standard
# response, which is why it is admitted where a message never would be.
PROVIDER_CODE_DISPOSITION: dict[str, str] = {
    # model/route
    "model_not_found": FAILURE_CLASS_TRANSIENT,
    "model_not_supported": FAILURE_CLASS_TRANSIENT,
    "model_not_available": FAILURE_CLASS_TRANSIENT,
    "unknown_model": FAILURE_CLASS_TRANSIENT,
    # provider-side load
    "overloaded_error": FAILURE_CLASS_TRANSIENT,
    "rate_limit_error": FAILURE_CLASS_TRANSIENT,
    # credential / account
    "authentication_failed": FAILURE_CLASS_CREDENTIAL,
    "invalid_api_key": FAILURE_CLASS_CREDENTIAL,
    "insufficient_balance": FAILURE_CLASS_CREDENTIAL,
    # RFC 6749 5.2 token-endpoint error codes
    "invalid_grant": FAILURE_CLASS_CREDENTIAL,
    "invalid_client": FAILURE_CLASS_CREDENTIAL,
    "unauthorized_client": FAILURE_CLASS_CREDENTIAL,
    "invalid_token": FAILURE_CLASS_CREDENTIAL,
    "expired_token": FAILURE_CLASS_CREDENTIAL,
    # named, and deliberately unplaced: each is an envelope label rather than a
    # cause, so it must never on its own decide that a card may be retried.
    "api_error": FAILURE_CLASS_UNKNOWN,
    "invalid_request_error": FAILURE_CLASS_UNKNOWN,
    "not_found_error": FAILURE_CLASS_UNKNOWN,
    "permission_error": FAILURE_CLASS_UNKNOWN,
    "unauthorized": FAILURE_CLASS_UNKNOWN,
}

# The typed envelope labels that prove the PROVIDER (not the worker, not this
# repository) terminated the attempt. One of these must be present before any
# status or code is allowed to place a card.
_PROVIDER_TERMINAL_ENVELOPES: tuple[str, ...] = (
    "result_api_error", "api_retry", "provider_error", "turn_failed",
)

# The two stream events a CLI writes ABOUT A FAILED REQUEST rather than about
# model output. Only these may have a nested provider body read out of their
# ``message``: an ``assistant`` line's message is the MODEL's own content, and
# unwrapping one would let any worker mint any provider body simply by printing
# it -- the forgery ``test_worker_prose_cannot_forge_a_route_failure`` pins.
PROVIDER_OWNED_MESSAGE_TYPES: frozenset[str] = frozenset({"error", "turn.failed"})
_PROVIDER_OWNED_MESSAGE_TYPES = PROVIDER_OWNED_MESSAGE_TYPES

_NO_PROVIDER_ENVELOPE = "no_provider_terminal_envelope"
_ENVELOPE_NAMES_NO_CLASS = "envelope_names_no_class"

# Bounds on the transient parse. A log tail is untrusted input, so the scan is
# capped in both dimensions rather than trusting the caller to have bounded it.
_MAX_SIGNAL_LINES = 400
_MAX_EMBEDDED_BYTES = 8192
_MAX_EMBEDDED_DEPTH = 2

# Reasons THIS repository mints, placed by reading the code that raises each.
#
# DEFECT is the narrow one on purpose. Only a refusal AIWorkHub itself issued
# after inspecting the attempt earns it: the mandatory-output validator saying
# the declared outputs did not change, the scope enforcer rejecting the diff,
# the declared validation failing. Those are this repository's own findings
# about the work, which is the only evidence that can honestly say "this work
# is wrong" rather than "something went wrong near it".
_DEFECT_REASONS: frozenset[str] = frozenset({
    "required_output_unchanged_parent_mismatch",
    "required_output_unchanged",
    "required_output_zero_bytes",
    "required_output_symlink",
    "required_output_no_matches",
    "required_output_missing",
    "required_output_invalid",
    "required_output_mismatch",
    "scope_rejected",
    "validation_failed",
})

# The one control-plane reason whose own definition states it is retryable:
# ``terminal_failure_transition_conflict`` is a losing race against another
# terminal writer, and ``TERMINAL_FAILURE_RECLAIM_REFUSAL_STATES`` in
# ``task_store`` exists because a later pass settles it.
_TRANSIENT_REASONS: frozenset[str] = frozenset({
    "terminal_failure_transition_conflict",
})

# ``claude_auth.RUNTIME_AUTH_FAILURE_REASON``: the subscription-session circuit
# has decided this credential must be renewed before the route can run again.
# This is the reason on the card that spent 190.8M tokens and $46.07.
_CREDENTIAL_REASONS: frozenset[str] = frozenset({
    "claude_subscription_session_refresh_required",
})


def _provider_refused_disposition() -> dict[str, str]:
    """Derive every ``provider_refused_<kind>[_suffix]`` reason from the KIND table.

    Deriving rather than restating is what keeps the compound reason vocabulary
    and ``REFUSAL_KIND_DISPOSITION`` from ever disagreeing about the same kind.
    The recoverability suffixes describe whether the provider reported a reset
    window; they never change WHICH cause was named, so they never change the
    class.
    """
    placed: dict[str, str] = {}
    for reason in _PROVIDER_REFUSAL_REASONS:
        if not reason.startswith("provider_refused_"):
            continue
        tail = reason[len("provider_refused_") :]
        for kind, kind_class in REFUSAL_KIND_DISPOSITION.items():
            if tail == kind or tail.startswith(kind + "_"):
                if kind_class != FAILURE_CLASS_UNKNOWN:
                    placed[reason] = kind_class
                break
    return placed


REASON_DISPOSITION: dict[str, str] = {
    **{reason: FAILURE_CLASS_DEFECT for reason in _DEFECT_REASONS},
    **{reason: FAILURE_CLASS_TRANSIENT for reason in _TRANSIENT_REASONS},
    **{reason: FAILURE_CLASS_CREDENTIAL for reason in _CREDENTIAL_REASONS},
    **_provider_refused_disposition(),
}

# Reasons deliberately left unplaced, and why they cannot be placed:
#
#   * every remaining control-plane reason states that the CARD ROW moved or
#     that this finalizer could not act (``not_processing``, ``not_claimed``,
#     ``claim_ownership_lost``, ``finalizer_retries_exhausted``, ...). None of
#     them says anything about the work or about the provider.
#   * every terminal STATE name is an envelope label. ``worker_failed`` is the
#     exact string this whole card exists because it means nothing.
#   * every ``_SIGNATURES`` code is a heuristic over untrusted prose -- see the
#     section header for why none of them may place a card.
#   * ``provider_refused`` and ``cause_not_distinguished_by_response`` are the
#     classifier's own admissions that the cause was NOT distinguished.
_DISCLAIMED_REASONS: frozenset[str] = (
    frozenset(_CONTROL_PLANE_REASONS)
    | frozenset(_TERMINAL_STATE_NAMES)
    | frozenset(_LAUNCHER_MINTED_REASONS)
    | frozenset(code for _pattern, code in _SIGNATURES)
    | frozenset(_PROVIDER_REFUSAL_REASONS)
    | {_UNCLASSIFIED, _UNRECOGNIZED}
) - frozenset(REASON_DISPOSITION)


def unclassified_reason_constants() -> tuple[str, ...]:
    """Reason constants this taxonomy neither places nor disclaims.

    Empty by construction today. A constant added to any of the vocabularies
    above lands in neither set, so
    ``test_every_reason_constant_is_placed_or_disclaimed`` fails on it -- the
    same gate ``dependency_autolaunch.unclassified_denial_reasons`` provides
    for launch denials, for the same reason: a new reason must not be able to
    arrive silently unclassified.
    """
    return tuple(sorted(
        token
        for token in _REASON_CONSTANTS
        if token not in REASON_DISPOSITION and token not in _DISCLAIMED_REASONS
    ))


def _embedded_objects(text: object, depth: int) -> list[dict[str, Any]]:
    """Return JSON objects a provider nested INSIDE a message string.

    The Codex CLI forwards the upstream provider's own error body as a quoted
    JSON string in ``message`` rather than as an object -- measured on the 13
    byte-identical ``gpt-5.4`` failures, whose outer line carries no status at
    all while the nested body carries ``status: 400`` and
    ``error.type: invalid_request_error``. Reading only the outer line loses
    every typed field the provider actually sent.

    Bounded on all three axes (depth, byte length, one object per string) and
    parsed, never pattern-matched: a string that is not JSON yields nothing.
    """
    if depth > _MAX_EMBEDDED_DEPTH or not isinstance(text, str):
        return []
    if not text or len(text) > _MAX_EMBEDDED_BYTES:
        return []
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return []
    try:
        obj = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return []
    return [obj] if isinstance(obj, dict) else []


def embedded_provider_objects(text: object) -> list[dict[str, Any]]:
    """Public entry to :func:`_embedded_objects` for one un-nested message.

    ``process_launcher``'s provider-boundary detectors read the same streams
    this module reads, so the rule for "what did the provider actually send"
    lives in ONE place. Two implementations of it would drift, and the drift
    would show up as a route seal that matches nothing on a real log.
    """
    return _embedded_objects(text, 0)
def _harvest_provider_event(event: object, found: dict[str, Any], depth: int = 0) -> None:
    """Fold one typed provider event into the accumulating signal.

    Only named fields are read. The first status and the first recognised code
    win, so a nested upstream body cannot be overwritten by a later generic
    wrapper, and no branch ever stores a slice of the event.
    """
    if depth > _MAX_EMBEDDED_DEPTH or not isinstance(event, dict):
        return
    kind = str(event.get("type") or "").strip().lower()
    subtype = str(event.get("subtype") or "").strip().lower()
    status = None
    for field in ("api_error_status", "error_status", "status"):
        candidate = event.get(field)
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            status = candidate
            break
    if status is not None and found["status"] is None and 100 <= status <= 599:
        found["status"] = status

    body = event.get("error")
    message = event.get("message") if isinstance(event.get("message"), str) else ""
    code = ""
    if isinstance(body, str):
        code = body.strip().lower()
    elif isinstance(body, dict):
        code = str(body.get("code") or body.get("type") or "").strip().lower()
        if not message and isinstance(body.get("message"), str):
            message = body["message"]
        data = body.get("data")
        if not message and isinstance(data, dict) and isinstance(data.get("message"), str):
            message = data["message"]
    if code and not found["code"] and code in PROVIDER_CODE_DISPOSITION:
        # The KEY object is stored, never the parsed argument, so no provider
        # byte can reach a durable field through this path.
        found["code"] = next(key for key in PROVIDER_CODE_DISPOSITION if key == code)

    if not found["envelope"]:
        if (
            kind == "result"
            and event.get("is_error") is True
            and str(event.get("terminal_reason") or "").strip().lower() == "api_error"
        ):
            found["envelope"] = _PROVIDER_TERMINAL_ENVELOPES[0]
        elif kind == "system" and subtype == "api_retry":
            found["envelope"] = _PROVIDER_TERMINAL_ENVELOPES[1]
        elif kind == "error":
            found["envelope"] = _PROVIDER_TERMINAL_ENVELOPES[2]
        elif kind == "turn.failed":
            found["envelope"] = _PROVIDER_TERMINAL_ENVELOPES[3]

    # UNWRAP ONLY A PROVIDER-OWNED ENVELOPE'S MESSAGE.  An ``assistant`` line's
    # ``message`` is the MODEL's own content, so unwrapping one would let a
    # worker mint any provider body it liked simply by printing it -- exactly
    # what ``test_worker_prose_cannot_forge_a_route_failure`` forbids, and what
    # an unrestricted unwrap regressed. Only ``error``/``turn.failed``, the two
    # envelopes a CLI writes about a failed request rather than about model
    # output, carry a nested body worth reading.
    if kind in _PROVIDER_OWNED_MESSAGE_TYPES:
        for nested in _embedded_objects(message, depth):
            _harvest_provider_event(nested, found, depth + 1)


def provider_terminal_signal(text: str | None) -> dict[str, Any]:
    """Read the PROVIDER's own typed terminal envelope out of a bounded tail.

    Returns ``{"status", "code", "envelope"}`` where ``status`` is a bounded
    int or ``None``, ``code`` is an element of ``PROVIDER_CODE_DISPOSITION`` or
    ``""``, and ``envelope`` is an element of ``_PROVIDER_TERMINAL_ENVELOPES``
    or ``""``. Every returned string is this module's own constant.

    WHY A TAIL IS SOUND EVIDENCE. The tail is the LAST bytes of the stream, and
    a provider CLI writes its terminal envelope last, by construction. Model
    prose that happened to look like one of these envelopes would have to be
    the final line of the stream to be read at all, which is the same anchor
    ``_provider_auth_failure_from_output`` has always relied on. It is not
    proof against a determined forger, and it is not asked to be: a forged
    ``transient`` costs the card's bounded retry budget and nothing else, and a
    forged ``credential`` pauses a lane and destroys no work.
    """
    found: dict[str, Any] = {"status": None, "code": "", "envelope": ""}
    for index, raw_line in enumerate(str(text or "").splitlines()):
        if index >= _MAX_SIGNAL_LINES:
            break
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        _harvest_provider_event(event, found)
    return found


def disposition_for_reason(reason: str | None) -> str:
    """Class named by a reason constant THIS repository minted, else ``unknown``.

    Only a reason that parses completely as a module constant is read; a string
    carrying anything else falls through, exactly as ``recognised_reason`` does.
    """
    token = str(reason or "").strip().split(":")[0]
    return REASON_DISPOSITION.get(token, FAILURE_CLASS_UNKNOWN)


def failure_disposition(
    *,
    refusal_kind: str | None = None,
    reason: str | None = None,
    stdout_tail: str | None = None,
    stderr_tail: str | None = None,
) -> dict[str, Any]:
    """Name the class of one terminal failure, and the evidence that named it.

    PRECEDENCE, and why. A refusal kind already established at the provider
    boundary is read first: ``process_launcher`` held the provider's response
    body when it minted that kind, which is strictly more evidence than a log
    tail retains. A control-plane reason this repository minted is read next --
    it states why the control plane refused, which is the reason the terminal
    event exists at all. The provider tail is read last, and only to place what
    the first two left unplaced.

    ``evidence`` is assembled from module constants and a bounded int only.
    """
    kind = str(refusal_kind or "").strip().lower()
    if kind in REFUSAL_KIND_DISPOSITION:
        placed = REFUSAL_KIND_DISPOSITION[kind]
        if placed != FAILURE_CLASS_UNKNOWN:
            named = next(key for key in REFUSAL_KIND_DISPOSITION if key == kind)
            return {
                "failure_class": placed,
                "evidence": f"refusal_kind={named}",
                "provider_status": None,
                "provider_code": "",
                "envelope": "",
            }

    reason_class = disposition_for_reason(reason)
    if reason_class != FAILURE_CLASS_UNKNOWN:
        token = str(reason or "").strip().split(":")[0]
        named = next(key for key in REASON_DISPOSITION if key == token)
        return {
            "failure_class": reason_class,
            "evidence": f"control_plane_reason={named}",
            "provider_status": None,
            "provider_code": "",
            "envelope": "",
        }

    for tail in (stderr_tail, stdout_tail):
        signal = provider_terminal_signal(tail)
        if signal["envelope"] not in _PROVIDER_TERMINAL_ENVELOPES:
            continue
        code = str(signal["code"])
        status = signal["status"]
        if PROVIDER_CODE_DISPOSITION.get(code, FAILURE_CLASS_UNKNOWN) != FAILURE_CLASS_UNKNOWN:
            return {
                "failure_class": PROVIDER_CODE_DISPOSITION[code],
                "evidence": f"provider_code={code}",
                "provider_status": status,
                "provider_code": code,
                "envelope": signal["envelope"],
            }
        placed_status = PROVIDER_STATUS_DISPOSITION.get(status, FAILURE_CLASS_UNKNOWN)
        if placed_status != FAILURE_CLASS_UNKNOWN:
            return {
                "failure_class": placed_status,
                "evidence": f"provider_status={status}",
                "provider_status": status,
                "provider_code": code,
                "envelope": signal["envelope"],
            }
        return {
            "failure_class": FAILURE_CLASS_UNKNOWN,
            "evidence": f"{_ENVELOPE_NAMES_NO_CLASS}={signal['envelope']}",
            "provider_status": status,
            "provider_code": code,
            "envelope": signal["envelope"],
        }

    return {
        "failure_class": FAILURE_CLASS_UNKNOWN,
        "evidence": _NO_PROVIDER_ENVELOPE,
        "provider_status": None,
        "provider_code": "",
        "envelope": "",
    }


def failure_disposition_from_paths(
    *,
    state: str | None,
    error: str | None = None,
    refusal_kind: str | None = None,
    stdout_path: str | Path | None = None,
    stderr_path: str | Path | None = None,
    cancelled: bool = False,
) -> dict[str, Any]:
    """``failure_disposition`` over the same bounded log tails the classifier reads.

    A cancelled or verdict-free outcome is never dispositioned: there is no
    failure to retry, pause a lane for, or blame on the card.
    """
    state_norm = str(state or "").strip().lower()
    if cancelled or state_norm in _NO_VERDICT_STATES:
        return {
            "failure_class": FAILURE_CLASS_UNKNOWN,
            "evidence": "no_failure_verdict",
            "provider_status": None,
            "provider_code": "",
            "envelope": "",
        }
    return failure_disposition(
        refusal_kind=refusal_kind,
        reason=error,
        stdout_tail=_read_log_tail(stdout_path),
        stderr_tail=_read_log_tail(stderr_path),
    )


def terminal_workspace_cleanup_allowed(
    *, terminal_state: str | None, failure_class: str | None,
) -> bool:
    """May the finalizer delete this attempt's worktree?

    Only ``launch_failed`` ever swept a workspace, on the reasoning that a
    launch failure means nothing ran. That reasoning holds for a launch that
    genuinely never started and fails for the case R4 exists to fix: a
    CREDENTIAL that expires at the END of a run flips a completed attempt onto
    ``launch_failed``, and the sweep then deletes finished work. One measured
    card lost 190.8M tokens and $46.07 that way.

    So a credential-class outcome never sweeps, whatever state it landed on.
    The owner has to renew a credential either way; the work must still be on
    disk when they do. Every other combination is unchanged.

    A predicate rather than an inline expression because this is exactly the
    kind of rule that must be provable in a test without standing up a
    supervisor, a workspace and a provider.
    """
    if str(terminal_state or "").strip().lower() != "launch_failed":
        return False
    return str(failure_class or "") != FAILURE_CLASS_CREDENTIAL
