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
                                   failures (nonzero returncode or a raised
                                   exception), so a read-source outage never
                                   silently reads as an empty queue and never
                                   crosses this module's boundary as an
                                   uncaught exception.

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
    silently parsing as zero rows. This adds no new authority: it is purely
    evidence/degrade-bounding on the existing read-only path -- no write, no
    launch, ``_authority_flags`` unchanged (all False).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from aiworkhub import core

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
        try:
            show_result = show_fn(tid)
        except Exception as exc:  # noqa: BLE001 -- bounded degrade, never re-raise
            fetch_errors.append({"task_id": tid, "error": str(exc)[:200]})
            read_errors.append({
                "scope": "show",
                "task_id": tid,
                "error_kind": "exception",
                "error_message": str(exc)[:200],
            })
            continue

        returncode, stdout, stderr = _result_fields(show_result)

        if returncode not in (0, None):
            fetch_errors.append({"task_id": tid, "error": stderr})
            read_errors.append({
                "scope": "show",
                "task_id": tid,
                "error_kind": "nonzero_returncode",
                "error_message": str(stderr)[:200],
            })
            continue

        text = (stdout or "").strip()
        if not text or "Task not found:" in text:
            fetch_errors.append({"task_id": tid, "error": "not_found_on_show"})
            continue
        try:
            cards.append(json.loads(text))
        except json.JSONDecodeError:
            fetch_errors.append({"task_id": tid, "error": "json_parse_error"})
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
        ``error_kind`` (``"nonzero_returncode"`` or ``"exception"``) and a
        truncated ``error_message``. This is what lets a caller distinguish
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
        if entry.get("terminal_substatus") != "finalize_failed":
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
