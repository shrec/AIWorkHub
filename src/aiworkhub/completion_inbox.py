"""READ-ONLY MCP completion-inbox view (B275 contract).

Combines four read-only facets Codex needs to orchestrate the parent task
queue into a single MCP tool:

  * review_queue              -- tasks awaiting Codex review.
  * stale_processing           -- tasks claimed (processing) with no recent
                                   artifact/validation activity.
  * runner_mismatch_warnings   -- batch-token mismatches between a runner
                                   name and the task_id it claimed.
  * latest_validation_facts    -- the most recently recorded test/verify
                                   status per task, as reported on the card.
  * read_errors (B280)         -- bounded, scoped taskctl list/show read
                                   failures. ``error_kind`` is one of
                                   ``nonzero_returncode``, ``exception``,
                                   ``not_found``, ``json_parse_error``,
                                   ``invalid_card``, or
                                   ``task_identity_mismatch``. A read-source
                                   outage never silently reads as an empty
                                   queue and never crosses this module's
                                   boundary as an uncaught exception.

Read path: every facet is derived by shelling out to the existing read-only
``taskctl.py list``/``show`` subcommands via ``core.run_taskctl`` -- the
IDENTICAL path ``taskctl.py`` itself uses internally. ``cmd_list``
(AITools/taskctl.py:544-578) and ``cmd_show`` (AITools/taskctl.py:579-589)
both read from ``_load_cards()`` -> ``taskdb.load_cards()``
(AITools/taskdb.py), backed by the live SQLite queue
``bitnnv2/data/tasking/task_queue_v1.sqlite``. This module never imports
``taskdb``/``AITools`` directly -- it stays import-light exactly like
``core.py``'s ``_lifecycle_state`` (a documented "faithful local replica" of
``taskdb.canonical_status``) and ``review_summarizer.py``'s
``_collect_review_cards``, both of which shell out to ``taskctl.py`` instead
of touching the DB module directly.

Hard invariants (asserted by tests):
  * READ-ONLY: every taskctl invocation goes through ``core.run_taskctl``/
    ``core.list_tasks``/``core.show_task`` with no ``allow_write=True`` --
    write commands stay refused by the existing write gate regardless of
    what this module passes.
  * NO LAUNCH: no subprocess/exec/fork/spawn/shell code beyond the
    pre-existing ``core.run_taskctl`` (itself only ever invokes
    ``python3 taskctl.py <args>``, never an agent/model process).
  * NO MUTATION: never calls taskctl done/review/start/auto-pickup/add-card/
    export-jsonl/usage. Only ``list`` and ``show`` (both read-only taskctl
    subcommands) are ever issued.
  * Batch-mismatch detection (``_runner_task_batch_mismatch`` below) is a
    local PURE replica of ``AITools/taskctl.py::_runner_task_batch_mismatch``
    (lines 264-295) -- no I/O, no mutation, kept in sync by comment
    reference, mirroring the ``core.py::_lifecycle_state`` precedent.
  * BOUNDED READ-ERROR HANDLING (B280): every ``list``/``show`` call is
    wrapped in ``try/except`` (see ``_fetch_full_cards``) so a subprocess-
    level failure degrades to a ``read_errors`` entry instead of an uncaught
    exception, and a non-zero LIST-call returncode is recorded instead of
    silently parsing as zero rows. Identity-mismatched, unparsable, or
    invalid live target cards fail closed (they do not authorize terminal
    exclusion). This adds no new authority: it is purely
    evidence/degrade-bounding on the existing read-only path -- no write, no
    launch, ``_authority_flags`` unchanged (all False).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from aiworkhub import core, task_plan

READONLY: bool = True
LAUNCH_IMPLEMENTED: bool = False
SUBPROCESS_LAUNCH_TRIPWIRE: int = 0

# Matches AITools/task_activity_facts.py's STALE_WORKER_HOURS (24h) precedent:
# a "processing" card whose most recent queue-DB timestamp (updated_at,
# falling back to started_at then claimed_at) is older than this is flagged
# stale -- claimed but with no recent artifact/validation activity bumping
# the row.
DEFAULT_STALE_PROCESSING_HOURS = 24.0

MAX_LIMIT = 500

# Parses `taskctl.py list` line format (AITools/taskctl.py:544-578,
# cmd_list): "[status] [topic] [runner] task_id" -- one line per card, no
# banner (unlike `review-queue`'s "=== ... ===" header + " -- objective" tail).
_LIST_LINE_RE = re.compile(
    r"^\s*\[(?P<cstatus>[^\]]+)\]\s*\[(?P<topic>[^\]]+)\]\s*\[(?P<runner>[^\]]+)\]\s*(?P<task_id>\S+)"
    r"(?:\s+model=(?P<model>\S+))?"
    r"(?:\s+outcome=(?P<outcome>\S+))?\s*$"
)

# Pure regexes mirroring AITools/taskctl.py:264-275 (_runner_batch_token /
# _task_batch_token) byte-for-byte.
_RUNNER_BATCH_RE = re.compile(r"(?:^|_)b(\d+)(?=_|$)", re.IGNORECASE)
_TASK_BATCH_RE = re.compile(r"(?:^|_)B(\d+)(?=_|$)", re.IGNORECASE)


def _authority_flags() -> dict[str, bool]:
    """Same shape/contract as review_summarizer._authority_flags and
    core._launch_queue_authority_flags -- every flag stays False; only
    write_gate_enabled mirrors the parent write-gate env state for
    information only (this module writes nothing regardless of its value).
    """
    return {
        "write_gate_enabled": core.writes_allowed(),
        "readonly": READONLY,
        "process_launch": False,
        "agent_launch": False,
        "shell_invocation": False,
        "queue_write": False,
        "audit_write": False,
        "subprocess_launch_tripwire_zero": SUBPROCESS_LAUNCH_TRIPWIRE == 0,
    }


def _runner_batch_token(runner: str | None) -> str | None:
    """Pure local replica of AITools/taskctl.py:264-269 (`_runner_batch_token`)."""
    matches = _RUNNER_BATCH_RE.findall(str(runner or ""))
    return matches[-1] if matches else None


def _task_batch_token(task_id: str | None) -> str | None:
    """Pure local replica of AITools/taskctl.py:270-275 (`_task_batch_token`)."""
    matches = _TASK_BATCH_RE.findall(str(task_id or ""))
    return matches[-1] if matches else None


def _runner_task_batch_mismatch(card: dict[str, Any], runner: str | None = None) -> str:
    """Pure local replica of AITools/taskctl.py:276-295
    (`_runner_task_batch_mismatch`). Detects worker/card affinity errors like
    runner ``*_b250`` claiming task ``*_B192_*``. Only fires when BOTH sides
    declare a batch token -- not every legacy runner/task has one. No I/O, no
    mutation, never raises.
    """
    actual_runner = runner if runner is not None else card.get("runner")
    runner_batch = _runner_batch_token(actual_runner)
    task_batch = _task_batch_token(card.get("task_id"))
    if runner_batch and task_batch and runner_batch != task_batch:
        return (
            "RUNNER_TASK_BATCH_MISMATCH "
            f"runner={actual_runner} runner_batch=b{runner_batch} "
            f"task={card.get('task_id')} task_batch=B{task_batch}"
        )
    return ""


def _iso_parse(s: str | None) -> datetime | None:
    """Parse an ISO-8601 string safely, returning None on failure. Mirrors
    AITools/task_activity_facts.py::_iso_parse."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_list_task_ids(stdout: str) -> list[dict[str, str]]:
    """Parse ``taskctl.py list``'s plain-text line format into rows.
    Tolerant: lines that don't match are silently skipped (never raises)."""
    rows: list[dict[str, str]] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LIST_LINE_RE.match(line)
        if m:
            rows.append(m.groupdict())
    return rows


def _result_fields(result: Any) -> tuple[Any, str, str]:
    """Pull (returncode, stdout, stderr) out of either a dict (real
    ``TaskCtlResult.as_dict()`` / test-fixture dict) or an attribute-bearing
    stub object. Never raises -- missing fields default to 0/""/"unknown"."""
    if isinstance(result, dict):
        return (
            result.get("returncode", 0),
            result.get("stdout", ""),
            result.get("stderr", "unknown"),
        )
    return (
        getattr(result, "returncode", 0),
        getattr(result, "stdout", ""),
        getattr(result, "stderr", "unknown"),
    )


def _show_one_card(show_fn, task_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        show_result = show_fn(task_id)
    except Exception as exc:  # noqa: BLE001 -- bounded degrade, never re-raise
        return None, {
            "scope": "show",
            "task_id": task_id,
            "error_kind": "exception",
            "error_message": str(exc)[:200],
        }
    returncode, stdout, stderr = _result_fields(show_result)
    text = (stdout or "").strip()
    full_stderr = str(stderr)
    if returncode not in (0, None):
        return None, {
            "scope": "show",
            "task_id": task_id,
            "error_kind": "nonzero_returncode",
            "error_message": full_stderr[:200],
            "stderr": full_stderr,
        }
    if "Task not found:" in text:
        return None, {
            "scope": "show",
            "task_id": task_id,
            "error_kind": "not_found",
            "error_message": "not_found_on_show",
        }
    if not text:
        return None, {
            "scope": "show",
            "task_id": task_id,
            "error_kind": "not_found",
            "error_message": "not_found_on_show",
        }
    try:
        card = json.loads(text)
    except json.JSONDecodeError:
        return None, {
            "scope": "show",
            "task_id": task_id,
            "error_kind": "json_parse_error",
            "error_message": "json_parse_error",
        }
    if isinstance(card, dict):
        returned_id = str(card.get("task_id") or "").strip()
        requested_id = str(task_id or "").strip()
        if returned_id != requested_id:
            return None, {
                "scope": "show",
                "task_id": requested_id,
                "error_kind": "task_identity_mismatch",
                "error_message": "task_identity_mismatch",
            }
        return card, None
    return None, {
        "scope": "show",
        "task_id": task_id,
        "error_kind": "invalid_card",
        "error_message": "invalid_card",
    }


def _enrich_artifact_target_cards(
    scanned_cards: list[dict[str, Any]],
    cards_by_id: dict[str, Any],
    show_fn,
) -> tuple[list[dict[str, Any]], bool]:
    read_errors: list[dict[str, Any]] = []

    def load_one(target_id: str):
        card, error = _show_one_card(show_fn, target_id)
        if error is not None:
            read_errors.append(error)
        return card if isinstance(card, dict) else None

    complete = task_plan.prefetch_unknown_terminal_cards(
        scanned_cards,
        cards_by_id,
        load_one,
    )
    return read_errors, not complete


def _fetch_full_cards(
    status: str,
    topic: str | None,
    limit: int,
    *,
    _list_tasks=None,
    _show_task=None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """READ-ONLY: ``taskctl list --status <status>`` -> parse task_ids ->
    ``taskctl show <task_id>`` for each. Mirrors
    ``review_summarizer._collect_review_cards``, generalized to any canonical
    status accepted by ``cmd_list``'s alias table (pending/processing/review/
    finished/blocked). Never issues a write command -- only ``list``/``show``.

    The ``_list_tasks``/``_show_task`` kwargs exist ONLY for test stubbing;
    they are never used in production and do not weaken the read-only
    invariant (same pattern as ``review_summarizer.summarize_review_queue``'s
    ``_run_taskctl``/``_show_task`` injection points).

    Bounded read-error handling (B280): both the LIST call and each SHOW call
    are guarded so a read-source failure degrades to a recorded error entry
    instead of (a) silently parsing as zero rows (a LIST-call non-zero
    returncode used to be indistinguishable from a genuinely empty queue) or
    (b) an uncaught exception crossing the tool boundary (``core.run_taskctl``
    has no try/except around ``subprocess.run``, so a subprocess-level
    failure -- e.g. missing interpreter, timeout -- previously propagated
    raw). Every failure mode is caught here, in this read-only layer, and
    turned into a bounded ``read_errors`` entry; no new write/launch surface
    is introduced (``_authority_flags`` stays all-False regardless).

    Returns ``(cards, fetch_errors, read_errors)``. ``fetch_errors`` keeps its
    original shape/contents (task_id + error, SHOW-call scoped only) for
    backward compatibility with existing consumers. ``read_errors`` is the new
    additive, scoped (list vs show) facet -- see ``build_completion_inbox``.
    """
    list_fn = _list_tasks if _list_tasks is not None else core.list_tasks
    show_fn = _show_task if _show_task is not None else core.show_task

    read_errors: list[dict[str, Any]] = []

    try:
        list_result = list_fn(status=status, topic=topic, limit=limit)
    except Exception as exc:  # noqa: BLE001 -- bounded degrade, never re-raise
        read_errors.append({
            "scope": "list",
            "status": status,
            "topic": topic,
            "error_kind": "exception",
            "error_message": str(exc)[:200],
        })
        list_result = None

    if list_result is None:
        rows: list[dict[str, str]] = []
    else:
        list_returncode, list_stdout, list_stderr = _result_fields(list_result)
        if list_returncode not in (0, None):
            read_errors.append({
                "scope": "list",
                "status": status,
                "topic": topic,
                "error_kind": "nonzero_returncode",
                "error_message": str(list_stderr)[:200],
            })
            rows = []
        else:
            rows = _parse_list_task_ids(list_stdout)

    cards: list[dict[str, Any]] = []
    fetch_errors: list[dict[str, Any]] = []
    for row in rows:
        tid = row["task_id"]
        card, error = _show_one_card(show_fn, tid)
        if error is not None:
            fetch_errors.append({
                "task_id": tid,
                "error": error.get("stderr")
                or error.get("error_message")
                or error.get("error_kind"),
            })
            read_errors.append(error)
            continue
        if not isinstance(card, dict):
            continue
        returned_id = str(card.get("task_id") or "").strip()
        if returned_id != str(tid or "").strip():
            mismatch = {
                "scope": "show",
                "task_id": tid,
                "error_kind": "task_identity_mismatch",
                "error_message": "task_identity_mismatch",
            }
            fetch_errors.append({
                "task_id": tid,
                "error": "task_identity_mismatch",
            })
            read_errors.append(mismatch)
            continue
        cards.append(card)
    return cards, fetch_errors, read_errors


def _compact_review_entry(card: dict[str, Any], mismatch: str) -> dict[str, Any]:
    terminal_review = card.get("terminal_review")
    terminal_substatus = str(
        card.get("terminal_substatus")
        or (
            terminal_review.get("substatus")
            if isinstance(terminal_review, dict)
            else ""
        )
        or ""
    )
    terminal_evidence: dict[str, Any] = {}
    if isinstance(terminal_review, dict):
        review_evidence = terminal_review.get("evidence")
        if isinstance(review_evidence, dict):
            terminal_evidence = review_evidence
    operational_error = str(
        terminal_evidence.get("error") or card.get("validation_error") or ""
    )
    return {
        "task_id": card.get("task_id"),
        "runner": card.get("runner"),
        "claimed_by": card.get("claimed_by"),
        "topic": card.get("topic"),
        "priority": card.get("priority"),
        "objective": str(card.get("objective", ""))[:160],
        "allowed_writes": list(card.get("allowed_writes") or [])[:10],
        "updated_at": card.get("updated_at", ""),
        "review_at": card.get("review_at", ""),
        "validation_status": card.get("validation_status", "unreported"),
        "terminal_substatus": terminal_substatus,
        "operational_error": operational_error[:300] or None,
        "quality_reviewer_eligible": terminal_substatus == "review_ready",
        "runner_task_batch_mismatch": mismatch or None,
    }


def _compact_blocked_entry(card: dict[str, Any], mismatch: str) -> dict[str, Any]:
    terminal_review = card.get("terminal_review")
    terminal_failure = card.get("terminal_failure")
    terminal_substatus = str(
        card.get("terminal_substatus")
        or (
            terminal_review.get("substatus")
            if isinstance(terminal_review, dict)
            else ""
        )
        or (
            terminal_failure.get("substatus")
            if isinstance(terminal_failure, dict)
            else ""
        )
        or ""
    )
    terminal_review_evidence: dict[str, Any] = {}
    if isinstance(terminal_review, dict):
        review_evidence = terminal_review.get("evidence")
        if isinstance(review_evidence, dict):
            terminal_review_evidence = review_evidence
    terminal_failure_evidence: dict[str, Any] = {}
    if isinstance(terminal_failure, dict):
        failure_evidence = terminal_failure.get("evidence")
        if isinstance(failure_evidence, dict):
            terminal_failure_evidence = failure_evidence
    request_id = (
        terminal_review_evidence.get("request_id")
        or terminal_failure_evidence.get("request_id")
        or card.get("launch_request_id")
    )
    operational_error = str(
        terminal_review_evidence.get("error")
        or terminal_failure_evidence.get("error")
        or card.get("blocker_reason")
        or card.get("validation_error")
        or ""
    )
    entry: dict[str, Any] = {
        "task_id": card.get("task_id"),
        "runner": card.get("runner"),
        "claimed_by": card.get("claimed_by"),
        "topic": card.get("topic"),
        "priority": card.get("priority"),
        "objective": str(card.get("objective", ""))[:160],
        "allowed_writes": list(card.get("allowed_writes") or [])[:10],
        "updated_at": card.get("updated_at", ""),
        "review_at": card.get("review_at", ""),
        "validation_status": card.get("validation_status", "unreported"),
        "terminal_substatus": terminal_substatus,
        "operational_error": operational_error[:300] or None,
        "quality_reviewer_eligible": False,
        "runner_task_batch_mismatch": mismatch or None,
    }
    if request_id is not None:
        entry["request_id"] = request_id
    launch_request_id = card.get("launch_request_id")
    if launch_request_id is not None:
        entry["launch_request_id"] = launch_request_id
    if "workspace_retained" in card:
        entry["workspace_retained"] = card["workspace_retained"]
    return entry


def _is_operational_finalization_failure(entry: dict[str, Any]) -> bool:
    substatus = str(entry.get("terminal_substatus") or "")
    if substatus == "finalize_failed":
        return True
    error = str(entry.get("operational_error") or "")
    return substatus == "validation_failed" and (
        error.startswith("validation_exec_scratch_unavailable:")
        or error.startswith(
            "validation_failed:validation_exec_scratch_unavailable:"
        )
    )


def _is_operational_worker_failure(entry: dict[str, Any]) -> bool:
    """Precise operational predicate for blocked ``worker_failed`` cards.

    Returns True only when the entry is ``worker_failed`` AND the recorded
    error carries a supervisor/process infrastructure signature (e.g.
    ``supervisor_incomplete:state=running:rc=None``). Ordinary provider/user
    errors -- monthly credit limits, quota exhaustion, model refusals -- are
    NOT AIWorkHub operational failures and are excluded from
    ``operational_failures``.
    """
    if str(entry.get("terminal_substatus") or "") != "worker_failed":
        return False
    error = str(entry.get("operational_error") or "")
    _supervisor_process_markers = (
        "supervisor_incomplete:",
        "supervisor_incomplete_",
        "supervisor_crashed:",
        "supervisor_died:",
        "supervisor_missing:",
        "supervisor_aborted:",
        "supervisor_unreachable:",
        "process_missing:",
        "process_crashed:",
        "process_unreachable:",
    )
    return any(marker in error for marker in _supervisor_process_markers)


def _stale_processing_entry(
    card: dict[str, Any],
    mismatch: str,
    now_dt: datetime,
    threshold_hours: float,
) -> dict[str, Any] | None:
    last_ts = card.get("updated_at") or card.get("started_at") or card.get("claimed_at")
    parsed = _iso_parse(last_ts)
    if parsed is None:
        return None
    stale_hours = (now_dt - parsed).total_seconds() / 3600.0
    if stale_hours < threshold_hours:
        return None
    return {
        "task_id": card.get("task_id"),
        "runner": card.get("runner"),
        "claimed_by": card.get("claimed_by"),
        "topic": card.get("topic"),
        "last_activity_at": last_ts,
        "last_activity_field": (
            "updated_at" if card.get("updated_at")
            else "started_at" if card.get("started_at")
            else "claimed_at"
        ),
        "stale_hours": round(stale_hours, 1),
        "runner_task_batch_mismatch": mismatch or None,
    }


def _validation_fact_entry(card: dict[str, Any], lifecycle_state: str) -> dict[str, Any]:
    return {
        "task_id": card.get("task_id"),
        "runner": card.get("runner"),
        "topic": card.get("topic"),
        "lifecycle_state": lifecycle_state,
        "validation_status": card.get("validation_status", "unreported"),
        "validation_error": (str(card.get("validation_error", ""))[:200] or None),
        "blocker_reason": (str(card.get("blocker_reason", ""))[:200] or None),
        "last_activity_at": card.get("updated_at", ""),
    }


def build_completion_inbox(
    topic: str | None = None,
    limit: int = 200,
    stale_processing_hours: float = DEFAULT_STALE_PROCESSING_HOURS,
    *,
    _list_tasks=None,
    _show_task=None,
) -> dict[str, Any]:
    """READ-ONLY: combined completion-inbox facts for Codex orchestration.

    Facets:
      * ``review_queue``: cards in canonical state "review" (awaiting Codex),
        excluding non-reviewable finalizer failures, newest ``updated_at``
        first.
      * ``operational_failures``: legacy ``review/finalize_failed`` cards and
        narrowly retryable validation-scratch failures that require retained
        finalization retry rather than quality review.
      * ``stale_processing``: cards in canonical state "processing" whose
        last recorded activity timestamp (``updated_at``, falling back to
        ``started_at`` then ``claimed_at``) is older than
        ``stale_processing_hours``, oldest-first.
      * ``runner_mismatch_warnings``: batch-token mismatches (runner
        ``*_bNN`` vs task_id ``*_BNN``) across every fetched pending/
        processing/review card -- a local pure replica of
        ``taskctl.py::_runner_task_batch_mismatch``.
      * ``latest_validation_facts``: ``validation_status``/
        ``validation_error``/``blocker_reason`` as currently recorded on
        every fetched card -- the only per-task validation signal available
        without executing anything.
      * ``read_errors`` (B280, additive): bounded, scoped read-tool-failure
        records -- ``scope: "list"`` for a failed/raising ``taskctl list``
        call (per canonical status queried) and ``scope: "show"`` for a
        failed/raising ``taskctl show <task_id>`` call. Each entry carries
        ``error_kind`` (``"nonzero_returncode"``, ``"exception"``,
        ``"not_found"``, ``"json_parse_error"``, ``"invalid_card"``, or
        ``"task_identity_mismatch"``) and a truncated ``error_message``.
        Missing or unusable live target evidence fails closed and never
        authorizes terminal exclusion. This is what lets a caller distinguish
        "the queue is genuinely empty" from "the read tool failed" -- neither
        case is ever an uncaught exception out of this function.

    Never mutates the parent queue: only ``list``/``show`` (read-only)
    taskctl subcommands are issued, via the existing ``core.run_taskctl``
    write gate (which stays closed by default and is never opened here).
    Every LIST/SHOW call is exception-guarded (B280) -- a read-source failure
    always degrades to a bounded ``read_errors`` entry, never an uncaught
    exception out of ``build_completion_inbox`` itself.
    """
    now_dt = _now()
    safe_limit = max(1, min(int(limit), MAX_LIMIT))
    fetch_errors: list[dict[str, Any]] = []
    read_errors: list[dict[str, Any]] = []

    buckets: list[tuple[str, list[dict[str, Any]]]] = []
    for cstatus in ("pending", "processing", "review", "blocked"):
        cards, errs, r_errs = _fetch_full_cards(
            cstatus, topic, safe_limit, _list_tasks=_list_tasks, _show_task=_show_task
        )
        buckets.append((cstatus, cards))
        fetch_errors.extend(errs)
        read_errors.extend(r_errs)

    cards_by_status = dict(buckets)
    scanned_cards = [card for _status, cards in buckets for card in cards]
    cards_by_id = {
        str(card.get("task_id") or ""): card
        for card in scanned_cards
        if card.get("task_id")
    }
    show_fn = _show_task if _show_task is not None else core.show_task
    enrich_errors, projection_incomplete = _enrich_artifact_target_cards(
        scanned_cards, cards_by_id, show_fn
    )
    read_errors.extend(enrich_errors)
    terminal_artifacts_excluded, excluded_ids = task_plan.terminal_artifact_projection(
        scanned_cards, cards_by_id
    )
    if excluded_ids:
        buckets = [
            (
                cstatus,
                [
                    card
                    for card in cards
                    if str(card.get("task_id") or "") not in excluded_ids
                ],
            )
            for cstatus, cards in buckets
        ]
        cards_by_status = dict(buckets)

    review_entries = [
        _compact_review_entry(card, _runner_task_batch_mismatch(card))
        for card in cards_by_status["review"]
    ]
    operational_failures = [
        entry
        for entry in review_entries
        if _is_operational_finalization_failure(entry)
    ]
    review_queue = [
        entry
        for entry in review_entries
        if not _is_operational_finalization_failure(entry)
    ]
    review_queue.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    operational_failures.sort(
        key=lambda x: x.get("updated_at", ""), reverse=True
    )

    _blocked_failure_keys = {
        (e.get("task_id"), e.get("request_id")) for e in operational_failures
    }
    for card in cards_by_status.get("blocked", []):
        entry = _compact_blocked_entry(card, _runner_task_batch_mismatch(card))
        terminal_sub = entry.get("terminal_substatus")
        if (
            terminal_sub != "finalize_failed"
            and not _is_operational_worker_failure(entry)
        ):
            continue
        key = (entry.get("task_id"), entry.get("request_id"))
        if key in _blocked_failure_keys:
            continue
        _blocked_failure_keys.add(key)
        operational_failures.append(entry)

    stale_processing = []
    for card in cards_by_status["processing"]:
        stale_entry = _stale_processing_entry(
            card, _runner_task_batch_mismatch(card), now_dt, stale_processing_hours
        )
        if stale_entry:
            stale_processing.append(stale_entry)
    stale_processing.sort(key=lambda x: x["stale_hours"], reverse=True)

    runner_mismatch_warnings = []
    for cstatus, cards in buckets:
        for card in cards:
            mismatch = _runner_task_batch_mismatch(card)
            if mismatch:
                runner_mismatch_warnings.append({
                    "task_id": card.get("task_id"),
                    "runner": card.get("runner"),
                    "claimed_by": card.get("claimed_by"),
                    "topic": card.get("topic"),
                    "lifecycle_state": cstatus,
                    "warning": mismatch,
                })

    latest_validation_facts = [
        _validation_fact_entry(card, cstatus)
        for cstatus, cards in buckets
        for card in cards
    ]

    return {
        "tool": "aiworkhub_completion_inbox",
        "contract": "B275_v1_readonly_completion_inbox",
        "generated_at": now_dt.isoformat(),
        "readonly": True,
        "authority_flags": _authority_flags(),
        "filters": {
            "topic": topic,
            "limit": safe_limit,
            "stale_processing_hours": stale_processing_hours,
        },
        "review_queue": review_queue,
        "operational_failures": operational_failures,
        "stale_processing": stale_processing,
        "runner_mismatch_warnings": runner_mismatch_warnings,
        "latest_validation_facts": latest_validation_facts,
        "terminal_artifacts_excluded": terminal_artifacts_excluded,
        "terminal_projection_incomplete": projection_incomplete,
        "counts": {
            "pending_scanned": len(cards_by_status["pending"]),
            "processing_scanned": len(cards_by_status["processing"]),
            "review_scanned": len(cards_by_status["review"]),
            "blocked_scanned": len(cards_by_status.get("blocked", [])),
            "review_queue": len(review_queue),
            "operational_failures": len(operational_failures),
            "stale_processing": len(stale_processing),
            "runner_mismatch_warnings": len(runner_mismatch_warnings),
            "latest_validation_facts": len(latest_validation_facts),
            "terminal_artifacts_excluded": len(excluded_ids),
            "fetch_errors": len(fetch_errors),
            "read_errors": len(read_errors),
        },
        "fetch_errors": fetch_errors,
        "read_errors": read_errors,
        "mutation": {
            "queue_mutated": False,
            "write_gate_bypassed": False,
            "write_command_invoked": False,
            "agent_or_process_launched": False,
            "writes_allowed_env": core.writes_allowed(),
        },
    }


# ---------------------------------------------------------------------------
# ONE bounded review packet (audit 2026-09-08, problem 5).
#
# The evidence a manager needs to decide a ``review_ready`` candidate is
# already persisted -- validation rows with their declared commands and output
# tails, the quality gate's checks and blockers, the worker MCP gate and its
# receipt-conformance verdict, required outputs, destructive-diff checks, the
# effective tier and its per-lens status, the reviewer findings, the
# reachability observation -- and it is spread across six nested objects on one
# card. Reading it by hand was the manager's own discovery cost: measured over
# the 2026-09-08 worker audit, discovery was 39% of every byte a worker read
# and reviewer lenses spent 85% of theirs re-acquiring what the server already
# held.
#
# This assembles that into ONE object, bounded, from persisted data only. It
# reads nothing live, launches nothing, and decides nothing: every verdict here
# was already recorded by the gate that produced it. What it adds is that they
# arrive together and under a size bound.
# ---------------------------------------------------------------------------

REVIEW_PACKET_SCHEMA_ID = "aiworkhub.review_packet.v1"

# The size bound, and it is enforced by MEASUREMENT, not by hope: the packet is
# encoded, and while it exceeds this the sections below are dropped in the
# declared order and named in ``truncation``. 90 KB is the target the audit
# set; the encoder never returns more.
REVIEW_PACKET_MAX_BYTES = 90_000

# Per-field caps applied before the whole-packet bound, so a single pathological
# field cannot consume the budget and force every other section out.
_PACKET_TAIL_CHARS = 2_000
_PACKET_TEXT_CHARS = 600
_PACKET_MAX_ROWS = 60

# Dropped in this order while the encoded packet exceeds the bound. Least
# decisive first: an output tail can be re-read from the artifact bundle, a
# blocker cannot be re-derived from anything the manager has open.
_PACKET_DROP_ORDER = (
    "validation_output_tails",
    "gate_checks_passed",
    "reviewer_observations",
    "changed_paths",
    "validation",
    "gate_checks",
    "reviewer_findings",
)


def _packet_text(value: Any, limit: int = _PACKET_TEXT_CHARS) -> str:
    return str(value or "")[:limit]


def _packet_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value[:_PACKET_MAX_ROWS] if isinstance(row, dict)]


def _packet_validation_rows(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Validation rows as declared/executed pairs with bounded tails."""
    rows = []
    for row in _packet_rows(evidence.get("validation")):
        returncode = row.get("returncode")
        rows.append(
            {
                "declared_command": _packet_text(
                    row.get("declared_command") or row.get("command")
                ),
                "executed_command": _packet_text(
                    row.get("executed_command")
                    or " ".join(str(v) for v in (row.get("executed_argv") or []))
                ),
                "returncode": returncode if isinstance(returncode, int) else None,
                "behavioral_role": _packet_text(row.get("behavioral_role"), 80),
                "duration_seconds": row.get("duration_seconds"),
                "stdout_tail": _packet_text(row.get("stdout_tail"), _PACKET_TAIL_CHARS),
                "stderr_tail": _packet_text(row.get("stderr_tail"), _PACKET_TAIL_CHARS),
                "truncated": bool(
                    row.get("stdout_truncated") or row.get("stderr_truncated")
                ),
            }
        )
    return rows


def _packet_gate_checks(gate: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "check_id": _packet_text(row.get("check_id"), 200),
            "kind": _packet_text(row.get("kind"), 60),
            "status": _packet_text(row.get("status"), 40),
            "command": _packet_text(row.get("command"), 300),
            "summary": _packet_text(row.get("summary")),
            "error": _packet_text(row.get("error")),
        }
        for row in _packet_rows(gate.get("checks"))
    ]


def _packet_worker_mcp_gate(evidence: dict[str, Any]) -> dict[str, Any]:
    """The MCP gate verdict plus the receipt-conformance blockers under it.

    ``receipt_conformance`` is not a top-level evidence key -- it is nested at
    ``worker_mcp_gate.verification.receipt_conformance``, which is why a
    manager reading the card by hand keeps missing it. It is a green card's
    most common late refusal.
    """
    gate = evidence.get("worker_mcp_gate")
    if not isinstance(gate, dict):
        return {"present": False, "gated": None, "satisfied": None, "blockers": []}
    verification = gate.get("verification")
    conformance = (
        verification.get("receipt_conformance") if isinstance(verification, dict) else None
    )
    conformance = conformance if isinstance(conformance, dict) else {}
    return {
        "present": True,
        "gated": bool(gate.get("gated")),
        "satisfied": gate.get("satisfied"),
        "reason": _packet_text(gate.get("reason")),
        "missing_tools": [
            _packet_text(v, 120) for v in (gate.get("missing_tools") or [])[:20]
        ],
        "stale_tools": [
            _packet_text(v, 120) for v in (gate.get("stale_tools") or [])[:20]
        ],
        "receipt_conformance_status": _packet_text(conformance.get("status"), 60),
        "receipt_conformance_blocking": bool(conformance.get("blocking")),
        "blockers": [
            _packet_text(v, 200) for v in (conformance.get("blockers") or [])[:20]
        ],
    }


def _packet_finding_key(lens: str, finding: dict[str, Any]) -> tuple[str, str, str]:
    """Dedupe key: (path, line, check_id), as the audit specified.

    Reviewer findings carry their location inside ``evidence`` prose rather
    than in structured fields, so the first ``path:line`` in that text is the
    location and the finding id is the check identity. Findings that name no
    location fall back to their own id, which cannot collide across lenses
    because the lens is part of the key.
    """
    text = str(finding.get("evidence") or "")
    match = re.search(r"([\w./\\-]+\.[A-Za-z0-9_]+):(\d+)", text)
    path = match.group(1) if match else ""
    line = match.group(2) if match else ""
    return (path, line, f"{lens}:{str(finding.get('id') or '')}" if not path else str(finding.get("id") or ""))


def _packet_reviewer_sections(
    reviewer_reports: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Deduped actionable findings, deduped observations, and the duplicate count."""
    findings: dict[tuple[str, str, str], dict[str, Any]] = {}
    observations: dict[tuple[str, str, str], dict[str, Any]] = {}
    duplicates = 0
    for report in reviewer_reports if isinstance(reviewer_reports, list) else []:
        if not isinstance(report, dict):
            continue
        lens = _packet_text(report.get("lens"), 40)
        for finding in report.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            key = _packet_finding_key(lens, finding)
            bucket = (
                findings
                if finding.get("disposition") == "defect"
                else observations
            )
            if key in bucket:
                duplicates += 1
                if lens not in bucket[key]["lenses"]:
                    bucket[key]["lenses"].append(lens)
                continue
            bucket[key] = {
                "id": _packet_text(finding.get("id"), 200),
                "lenses": [lens],
                "severity": _packet_text(finding.get("severity"), 30),
                "disposition": _packet_text(finding.get("disposition"), 40),
                "category": _packet_text(finding.get("category"), 60),
                "path": key[0],
                "line": key[1],
                "summary": _packet_text(finding.get("summary")),
                "evidence": _packet_text(finding.get("evidence")),
            }
    return (
        list(findings.values())[:_PACKET_MAX_ROWS],
        list(observations.values())[:_PACKET_MAX_ROWS],
        duplicates,
    )


def _packet_encoded_bytes(packet: dict[str, Any]) -> int:
    return len(
        json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _packet_drop(packet: dict[str, Any], section: str) -> bool:
    """Apply one declared drop. Returns whether anything was actually removed."""
    if section == "validation_output_tails":
        rows = packet.get("gates", {}).get("validation") or []
        dropped = False
        for row in rows:
            if row.get("stdout_tail") or row.get("stderr_tail"):
                dropped = True
            row["stdout_tail"] = ""
            row["stderr_tail"] = ""
            row["tails_dropped"] = True
        return dropped
    if section == "gate_checks_passed":
        gates = packet.get("gates", {})
        rows = gates.get("quality_gate_checks") or []
        kept = [row for row in rows if row.get("status") != "passed"]
        gates["quality_gate_checks"] = kept
        return len(kept) != len(rows)
    if section == "reviewer_observations":
        review = packet.get("review", {})
        had = bool(review.get("observations"))
        review["observations"] = []
        return had
    if section == "changed_paths":
        diff = packet.get("diff", {})
        had = bool(diff.get("changed_paths"))
        diff["changed_paths"] = []
        return had
    if section == "validation":
        gates = packet.get("gates", {})
        had = bool(gates.get("validation"))
        gates["validation"] = []
        return had
    if section == "gate_checks":
        gates = packet.get("gates", {})
        had = bool(gates.get("quality_gate_checks"))
        gates["quality_gate_checks"] = []
        return had
    if section == "reviewer_findings":
        review = packet.get("review", {})
        had = bool(review.get("findings"))
        review["findings"] = []
        return had
    return False


def review_packet(
    request_id: str,
    *,
    card: dict[str, Any] | None = None,
    task_id: str = "",
    show_fn: Any = None,
    accept_preview: dict[str, Any] | None = None,
    reviewer_reports: Any = None,
    max_bytes: int = REVIEW_PACKET_MAX_BYTES,
) -> dict[str, Any]:
    """One bounded review packet for a ``review_ready`` request, from the card.

    Read-only and total. ``card`` may be supplied directly; otherwise it is
    fetched with ``show_fn`` (default :func:`core.show_task`), the same
    read-only path every other facet in this module uses. ``accept_preview`` is
    the fold from
    :func:`process_launcher_accept_review.accept_preview` -- passed in rather
    than called, because that fold needs a live ProcessManager and this module
    holds no launch authority.

    Nothing here is a verdict. Every status in the packet was decided by the
    gate that recorded it; assembling them cannot change one, and a manager
    still accepts through ``accept_review``, which re-runs the whole fold.
    """
    packet: dict[str, Any] = {
        "schema_id": REVIEW_PACKET_SCHEMA_ID,
        "request_id": str(request_id),
        "task_id": str(task_id),
        "ok": True,
        "readonly": READONLY,
    }
    if card is None:
        if not task_id:
            return {**packet, "ok": False, "error": "task_id_required_without_card"}
        fetched, read_error = _show_one_card(show_fn or core.show_task, task_id)
        if fetched is None:
            return {**packet, "ok": False, "error": "card_unreadable",
                    "read_error": read_error}
        card = fetched
    terminal = card.get("terminal_review")
    terminal = terminal if isinstance(terminal, dict) else {}
    evidence = terminal.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    gate = evidence.get("quality_gate")
    gate = gate if isinstance(gate, dict) else {}
    verdict = gate.get("quality_verdict")
    verdict = verdict if isinstance(verdict, dict) else {}
    # The finalizer's own observation first, the accept-time profile second:
    # they are the same computation at two moments, and where both exist the
    # review-ready one is what this candidate was actually planned against.
    profile = gate.get("review_risk_profile")
    if not isinstance(profile, dict) or profile.get("error"):
        profile = gate.get("risk_profile")
    profile = profile if isinstance(profile, dict) else {}
    identity = evidence.get("request_identity")
    identity = identity if isinstance(identity, dict) else {}
    changed_paths = [
        _packet_text(value, 300) for value in (evidence.get("changed_paths") or [])
    ][:200]
    hashes = evidence.get("changed_path_hashes")
    hashes = hashes if isinstance(hashes, dict) else {}
    if reviewer_reports is None:
        reviewer_reports = verdict.get("reviewer_reports")
    findings, observations, duplicate_count = _packet_reviewer_sections(reviewer_reports)
    preview = accept_preview if isinstance(accept_preview, dict) else {}
    packet.update(
        {
            "task_id": str(task_id or identity.get("task_id") or card.get("task_id") or ""),
            "identity": {
                "request_id": _packet_text(identity.get("request_id") or request_id, 100),
                "task_id": _packet_text(identity.get("task_id") or card.get("task_id"), 300),
                "runner": _packet_text(identity.get("runner") or card.get("runner"), 120),
                "topic": _packet_text(identity.get("topic") or card.get("topic"), 120),
                "claim_epoch": _packet_text(card.get("claim_epoch"), 40),
                "adapter_id": _packet_text(evidence.get("adapter_id"), 120),
                "model": _packet_text(evidence.get("model"), 120),
            },
            "status": {
                "canonical_status": _packet_text(card.get("status"), 40),
                "terminal_substatus": _packet_text(terminal.get("substatus"), 60),
                "error": _packet_text(evidence.get("error")),
            },
            "risk": {
                "effective_tier": _packet_text(profile.get("effective_tier"), 20),
                "requested_tier": _packet_text(profile.get("requested_tier"), 20),
                "signals": [_packet_text(v, 60) for v in (profile.get("signals") or [])][:30],
                "required_reviewer_lenses": [
                    _packet_text(v, 40)
                    for v in (profile.get("required_reviewer_lenses") or [])
                ][:10],
                "card_declared_risk_tier": _packet_text(card.get("risk_tier"), 20),
                "source": (
                    "review_ready_observation"
                    if isinstance(gate.get("review_risk_profile"), dict)
                    else "accept_time_profile" if gate.get("risk_profile") else "absent"
                ),
            },
            "gates": {
                "quality_gate_passed": gate.get("passed"),
                "quality_gate_blockers": [
                    _packet_text(v, 200) for v in (gate.get("blocking_checks") or [])
                ][:40],
                "quality_gate_config_error": _packet_text(gate.get("config_error")),
                "quality_gate_checks": _packet_gate_checks(gate),
                "validation": _packet_validation_rows(evidence),
                "worker_mcp_gate": _packet_worker_mcp_gate(evidence),
                "required_outputs": [
                    {
                        "path": _packet_text(row.get("path"), 300),
                        "pattern": _packet_text(row.get("pattern"), 300),
                        "bytes": row.get("bytes"),
                        "unchanged_allowed": bool(row.get("unchanged_allowed")),
                    }
                    for row in _packet_rows(evidence.get("required_outputs"))
                ],
                "destructive_diff_checks": [
                    {
                        "check_id": _packet_text(row.get("check_id"), 200),
                        "status": _packet_text(row.get("status"), 40),
                        "summary": _packet_text(row.get("summary")),
                    }
                    for row in _packet_rows(
                        gate.get("review_ready_destructive_diff_checks")
                        or evidence.get("destructive_diff_checks")
                    )
                ],
                "behavioral_gate": gate.get("behavioral_gate"),
            },
            "review": {
                "lenses": [
                    {
                        "lens": _packet_text(row.get("lens"), 40),
                        "status": _packet_text(row.get("status"), 60),
                        "finding_count": len(row.get("finding_ids") or []),
                        "observation_count": len(row.get("observation_ids") or []),
                        "independence_rung": _packet_text(row.get("independence_rung"), 60),
                    }
                    for row in _packet_rows(verdict.get("lenses"))
                ],
                "findings": findings,
                "observations": observations,
                "duplicate_findings_collapsed": duplicate_count,
                "refine_required": verdict.get("refine_required"),
            },
            "diff": {
                "changed_path_count": len(evidence.get("changed_paths") or []),
                "hashed_path_count": len(hashes),
                "required_output_count": len(evidence.get("required_outputs") or []),
                "changed_paths": changed_paths,
            },
            "reachability": gate.get("reachability")
            if isinstance(gate.get("reachability"), dict)
            else {"evaluated": False, "reason": "not_recorded_on_this_card"},
            "accept_preview": {
                "evaluated": bool(preview.get("evaluated")),
                "blocked": preview.get("blocked"),
                "blockers": [
                    {
                        "kind": _packet_text(row.get("kind"), 80),
                        "error": _packet_text(row.get("error"), 400),
                    }
                    for row in _packet_rows(preview.get("blockers"))
                ],
                "reviewer_request_ids": [
                    _packet_text(v, 100)
                    for v in (preview.get("reviewer_request_ids") or [])
                ][:20],
                "reviewer_request_id_source": _packet_text(
                    preview.get("reviewer_request_id_source"), 60
                ),
            },
            "truncation": {"dropped_sections": [], "max_bytes": int(max_bytes)},
            "mutation": {
                "queue_mutated": False,
                "write_gate_bypassed": False,
                "write_command_invoked": False,
                "agent_or_process_launched": False,
            },
        }
    )
    for section in _PACKET_DROP_ORDER:
        if _packet_encoded_bytes(packet) <= int(max_bytes):
            break
        if _packet_drop(packet, section):
            packet["truncation"]["dropped_sections"].append(section)
    packet["encoded_bytes"] = _packet_encoded_bytes(packet)
    packet["truncation"]["within_bound"] = packet["encoded_bytes"] <= int(max_bytes)
    return packet
