"""NF-2026-01922: return a red declared validation to the same worker, once per signature.

About a third of sampled manager rejects said the code was right and only the
card's declared validation failed; each one cost a manager decision that was
purely mechanical: ``reject_review(to=pending)`` with the failing command and
tail, then a relaunch on the same adapter and model. This module owns that step.

Two halves, so the finalizer never acts while it still holds its request lock:

* :func:`plan` runs inside the finalizer, before the ``validation_failed``
  review transition is written. It returns the keys merged into the terminal
  evidence: the decision record, and -- only for an auto-return -- the
  ``manager_callback_deferred`` marker ``task_engine.mark_terminal_review``
  already honours, so no manager callback that needs a decision is enqueued.
* :func:`after_finalize` runs after the finalizing request released its lock.
  It re-reads the card, rejects it back to pending through
  ``core.reject_review`` pinned to the planned request
  (``predecessor_request_id``; a terminal review that moved to another
  request is refused as ``predecessor_request_id_stale`` and the plan is
  dropped as ``stale_plan``), records a mechanical-rework event, and
  relaunches through ``launch``. Any other failure wakes the manager
  immediately; a crash between the halves still surfaces through the callback
  store's deferred-review grace wake.

Auto-return needs all of: a failing declared command, no environmental or
operational classification (the terminal-evidence key
``validation_environment_disposition`` the launcher writes, or
``is_operational_validation_failure``), a failure signature never
auto-returned before, fewer than :data:`MAX_AUTO_RETURNS` prior auto-returns,
and a reject that leaves the rework depth under
``launch_replay_guard.REJECT_DEPTH_CAP``. Everything else goes to the manager
exactly as before, with the prior auto-return count on the evidence (and
therefore on the validation_failed packet).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Protocol

from . import core, launch_replay_guard, sqlite_readonly, task_store, validation_failed_packet
from .process_launcher_validation import is_operational_validation_failure

SCHEMA_ID = "aiworkhub.validation_auto_rework.v1"
REASON_CODE = "validation_auto_rework"
ACTOR = "aiworkhub_validation_auto_rework"
EVENT = "validation_auto_rework"
DECISION_AUTO_RETURN = "auto_return"
DECISION_MANAGER = "manager"
DECISION_STALE_PLAN = "stale_plan"
# core._resolve_reject_review_predecessor's refusal for a moved terminal review.
STALE_PREDECESSOR_ERROR = "predecessor_request_id_stale"
# Independent of the reject depth cap: distinct signatures alone could never
# loop past the cap, this bounds the mechanical laps even if the cap's
# counting rules change.
MAX_AUTO_RETURNS = 3
# Well under task_store.MAX_REWORK_FEEDBACK_BYTES: core refuses an over-cap
# reason instead of cutting it.
REASON_MAX_BYTES = 4000
_TAIL_MAX_BYTES = 2048
_HEADER = "validation_auto_rework signature={signature} auto_return={count}"
_HEADER_RE = re.compile(r"^validation_auto_rework signature=([0-9a-f]{64}) auto_return=(\d+)")

# Volatile tokens stripped before hashing a failure tail. Order matters:
# timestamps before durations (both contain colon-separated digits).
_VOLATILE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (
        re.compile(
            r"(?i)[^\s'\"()\[\]<>,;]*(?:[\\/](?:te?mp|tmp[\w.-]*)[\\/]|pytest-of-|[\\/]pytest-\d+[\\/])"
            r"[^\s'\"()\[\]<>,;]*"
        ),
        "<tmp>",
    ),
    (re.compile(r"(?i)\bpid\s*[=:]?\s*\d+"), "pid=<pid>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<addr>"),
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "<hex>"),
    (re.compile(r"\b\d+:\d{2}:\d{2}(?:\.\d+)?\b"), "<dur>"),
    (re.compile(r"(?i)\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds)\b"), "<dur>"),
    (re.compile(r"[ \t]+"), " "),
)


class Manager(Protocol):
    repo: Path

    # The reject goes to ``core.reject_review`` pinned (see _reject_pinned);
    # the manager supplies the repository binding, the relaunch and, when it
    # has one, ``cancel_disposed_reviewer_processes``.
    def launch(self, **kwargs: Any) -> dict[str, Any]: ...


def _complete_lines(tail: str) -> str:
    """Drop the leading line of a tail that may have been cut by bytes.

    A tail is the last N bytes of a log, so volatile tokens of different length
    later in the log ("in 1.23s" vs "in 12.34s") move the cut point and the
    leading partial line differs between two runs of the same failure. Every
    cut in the pipeline keeps at least ``_TAIL_MIN_BYTES`` (the packet halves a
    tail no further), so a shorter tail is complete and kept whole; a longer
    one is hashed from its first complete line on.
    """
    if len(tail.encode("utf-8")) < validation_failed_packet._TAIL_MIN_BYTES or "\n" not in tail:
        return tail
    return tail.split("\n", 1)[1]


_SIGNATURE_LINES = 12


def normalize_tail(tail: str) -> str:
    text = _complete_lines(str(tail or ""))
    for pattern, replacement in _VOLATILE_PATTERNS:
        text = pattern.sub(replacement, text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # A byte cut can land on either side of a newline, so two runs of the same
    # failure may start one complete line apart: hash a fixed suffix of lines.
    return "\n".join(lines[-_SIGNATURE_LINES:])


def failure_signature(packet: Mapping[str, Any]) -> str:
    """sha256 over each failing declared command and its normalized tail hash."""
    parts = sorted(
        (
            str(entry.get("command") or ""),
            hashlib.sha256(normalize_tail(str(entry.get("tail") or "")).encode("utf-8")).hexdigest(),
        )
        for entry in packet.get("failing") or []
        if isinstance(entry, Mapping)
    )
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def auto_return_signatures(reject_events: list[Mapping[str, Any]]) -> list[str]:
    """Signatures of earlier auto-returns, oldest first, read off the durable rows."""
    found: list[str] = []
    for event in reject_events:
        if not isinstance(event, Mapping):
            continue
        match = _HEADER_RE.match(str(event.get("reason") or "").strip())
        if match:
            found.append(match.group(1))
    return found


def build_reason(packet: Mapping[str, Any], signature: str, auto_return: int) -> str:
    """The rework instruction: failing command, exit code and failure tail."""
    failing = [entry for entry in packet.get("failing") or [] if isinstance(entry, Mapping)]
    lines = [
        _HEADER.format(signature=signature, count=auto_return),
        f"reason_code={REASON_CODE} actor={ACTOR}",
        "The card's declared validation failed on this candidate. It was returned "
        "automatically for rework on the same adapter and model; no manager decision "
        "was made. Fix the cause below; do not weaken or change the declared validation.",
    ]
    tail_budget = max(256, _TAIL_MAX_BYTES // max(1, len(failing)))
    for entry in failing:
        nodeids = ", ".join(str(n) for n in entry.get("failing_nodeids") or [])
        lines.append(f"- failing command: {entry.get('command')}")
        lines.append(f"  exit_code: {entry.get('exit_code')} timed_out: {bool(entry.get('timed_out'))}")
        if nodeids:
            lines.append(f"  failing tests: {nodeids}")
        tail = validation_failed_packet._last_bytes(str(entry.get("tail") or ""), tail_budget)
        if tail:
            lines.append("  failure tail:")
            lines.append(tail)
    log_ref = packet.get("log_ref")
    if isinstance(log_ref, Mapping) and log_ref.get("path"):
        lines.append(f"full log: {log_ref.get('path')}")
    return validation_failed_packet._first_bytes("\n".join(lines), REASON_MAX_BYTES)


def _manager_record(cause: str, prior: int | None, signature: str = "") -> dict[str, Any]:
    # ``prior`` is None when the reject history could not be read: unknown is
    # recorded as unknown, never as zero auto-returns.
    return {
        "schema_id": SCHEMA_ID,
        "decision": DECISION_MANAGER,
        "cause": cause,
        "prior_auto_returns": prior,
        "signature": signature or None,
    }


def _decide(
    repo: Path, metadata: Mapping[str, Any], terminal_state: str, error: str,
    evidence: Mapping[str, Any], reject_events: list[dict[str, Any]],
) -> dict[str, Any]:
    task_id = str(metadata.get("task_id") or "")
    prior_signatures = auto_return_signatures(reject_events)
    prior = len(prior_signatures)
    if evidence.get("validation_environment_disposition") or is_operational_validation_failure(
        terminal_state, error
    ):
        return _manager_record("environmental", prior)
    card = task_store.get_task(repo, task_id)
    if not isinstance(card, dict):
        return _manager_record("card_unreadable", prior)
    packet = validation_failed_packet.packet_from_evidence(
        task_id=task_id,
        request_id=str(evidence.get("request_id") or ""),
        evidence=evidence,
        roles=card.get("validation_roles"),
        repo_root=repo,
    )
    failing = [entry for entry in packet["failing"] if entry.get("command")]
    if not failing:
        return _manager_record("no_failing_declared_command", prior)
    signature = failure_signature(packet)
    if signature in prior_signatures:
        return _manager_record("repeat_signature", prior, signature)
    if prior >= MAX_AUTO_RETURNS:
        return _manager_record("auto_return_cap", prior, signature)
    counted = launch_replay_guard.counted_candidate_code_rejects(
        reject_events, launch_replay_guard.task_contract_identity(card)
    )
    if len(counted) + 1 >= launch_replay_guard.REJECT_DEPTH_CAP:
        return _manager_record("rework_depth_cap", prior, signature)
    adapter_id = str(metadata.get("adapter_id") or "").strip()
    model = str(metadata.get("model") or "").strip()
    if not adapter_id or not model:
        return _manager_record("launch_identity_missing", prior, signature)
    return {
        "schema_id": SCHEMA_ID,
        "decision": DECISION_AUTO_RETURN,
        "cause": REASON_CODE,
        "prior_auto_returns": prior,
        "signature": signature,
        "adapter_id": adapter_id,
        "model": model,
        "reason": build_reason(packet, signature, prior + 1),
    }


def _reject_history(repo: Path, task_id: str) -> list[dict[str, Any]]:
    """The durable reject rows, decoded by the launch guard's own reader.

    Unlike ``launch_replay_guard.recorded_reject_events`` (which maps an
    unreadable store to "no history"), this raises: an unknown history must
    never look like zero prior auto-returns.
    """
    conn = sqlite_readonly.connect_readonly(task_store.canonical_db_path(repo))
    try:
        return launch_replay_guard.reject_event_payloads(conn, task_id)
    finally:
        conn.close()


def plan(
    repo: Path, metadata: Mapping[str, Any], terminal_state: str, error: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Terminal-evidence keys for a ``validation_failed`` review; ``{}`` otherwise.

    Never raises: an exception is recorded as a manager decision, so the card
    reaches the manager exactly as it did before this module existed.
    """
    if terminal_state != "validation_failed":
        return {}
    repo = Path(repo)
    task_id = str(metadata.get("task_id") or "")
    try:
        reject_events = _reject_history(repo, task_id)
        prior = len(auto_return_signatures(reject_events))
    except Exception as exc:  # noqa: BLE001 -- unknown history: count unknown
        record = _manager_record(f"plan_failed:{type(exc).__name__}", None)
    else:
        try:
            record = _decide(repo, metadata, terminal_state, error, evidence, reject_events)
        except Exception as exc:  # noqa: BLE001 -- unknown is never an auto-return
            record = _manager_record(f"plan_failed:{type(exc).__name__}", prior)
    keys: dict[str, Any] = {"validation_auto_rework": record}
    if record["decision"] == DECISION_AUTO_RETURN:
        keys["manager_callback_deferred"] = {"reason": REASON_CODE}
    return keys


def _write_connection(repo: Path) -> Any:
    """A writer on the same canonical store ``task_store.get_task(repo, ...)`` reads."""
    return task_store._connect(task_store.canonical_db_path(repo))


def _record_event(repo: Path, task_id: str, payload: dict[str, Any]) -> None:
    from . import callback_store

    conn = _write_connection(repo)
    try:
        callback_store.append_event(conn, task_id, EVENT, ACTOR, payload)
    finally:
        conn.close()


def _enqueue_wake(repo: Path, task_id: str, card: Mapping[str, Any], substatus: str, request_id: str) -> bool:
    from . import callback_store

    conn = _write_connection(repo)
    try:
        callback_store.init_db(conn)
        thread = str(
            callback_store.read_origin_thread(conn, task_id) or card.get("origin_thread_id") or ""
        ).strip()
        return callback_store.enqueue_callback(
            conn,
            task_id,
            thread,
            callback_store.resolve_callback_transition(substatus),
            provider=str(card.get("coordinator_provider") or "").strip().lower(),
            request_id=request_id,
        )
    finally:
        conn.close()


def _wake_manager(repo: Path, task_id: str, request_id: str) -> bool:
    """Enqueue the validation_failed callback the plan deferred, if still current.

    Bound to ``request_id``: a card whose terminal review belongs to another
    request (a newer attempt) is never woken by a stale caller.
    """
    card = task_store.get_task(repo, task_id)
    if not isinstance(card, Mapping):
        return False
    if (
        str(card.get("worker_status") or "") != "review"
        or str(card.get("terminal_substatus") or "") != "validation_failed"
    ):
        return False
    review = card.get("terminal_review")
    evidence = review.get("evidence") if isinstance(review, Mapping) else None
    if not isinstance(evidence, Mapping) or str(evidence.get("request_id") or "") != request_id:
        return False
    return _enqueue_wake(repo, task_id, card, "validation_failed", request_id)


_CLAIM_REQUEST_KEYS = ("request_id", "claimed_request_id", "active_request_id")


def _claimed_by_newer_request(card: Mapping[str, Any], request_id: str) -> bool:
    """True when a request other than the rejected one owns the card now."""
    for key in _CLAIM_REQUEST_KEYS:
        claimed = str(card.get(key) or "").strip()
        if claimed and claimed != request_id:
            return True
    review = card.get("terminal_review")
    evidence = review.get("evidence") if isinstance(review, Mapping) else None
    terminal = str(evidence.get("request_id") or "").strip() if isinstance(evidence, Mapping) else ""
    return bool(terminal and terminal != request_id)


def _wake_relaunch_failed(repo: Path, task_id: str, request_id: str) -> bool:
    """The reject committed but the relaunch did not: the card is pending now.

    The review-only wake cannot fire for a pending card, and neither can the
    deferred-review grace wake, so the manager is told through the
    ``launch_failed`` transition, bound to the auto-returned ``request_id``.
    A launch that claimed the card before failing (it moved on from pending,
    or a newer request owns it) is not a stranded card: nothing is enqueued.
    """
    card = task_store.get_task(repo, task_id)
    if not isinstance(card, Mapping):
        return False
    if str(card.get("worker_status") or "") != "pending" or _claimed_by_newer_request(card, request_id):
        return False
    return _enqueue_wake(repo, task_id, card, "launch_failed", request_id)


def _current_plan(card: Mapping[str, Any] | None, request_id: str) -> dict[str, Any] | None:
    if not isinstance(card, Mapping):
        return None
    review = card.get("terminal_review")
    evidence = review.get("evidence") if isinstance(review, Mapping) else None
    if not isinstance(evidence, Mapping) or str(evidence.get("request_id") or "") != request_id:
        return None
    record = evidence.get("validation_auto_rework")
    if not isinstance(record, dict) or record.get("decision") != DECISION_AUTO_RETURN:
        return None
    return record


def _reject_pinned(manager: Manager, task_id: str, reason: str, request_id: str) -> dict[str, Any]:
    """``core.reject_review`` back to pending, pinned to the planned request.

    ``ProcessManager.reject_review`` forwards no ``predecessor_request_id``, so
    the pin goes to the core transition it wraps (the same code, not a copy);
    core refuses with ``predecessor_request_id_stale`` when the terminal review
    moved to another request. The manager's repository-binding check and its
    post-commit reviewer cleanup are kept around the call.
    """
    refusal: dict[str, Any] = {"ok": False, "task_id": task_id, "to": "pending"}
    try:
        authority = core.repo_root().resolve()
    except Exception as exc:  # noqa: BLE001 -- an unresolvable root refuses
        return {**refusal, "error": f"repo_authority_unavailable:{type(exc).__name__}"}
    if authority != Path(manager.repo).resolve():
        return {**refusal, "error": "repo_authority_mismatch"}
    result = core.reject_review(task_id, reason, to="pending", predecessor_request_id=request_id)
    if not isinstance(result, dict):
        return {**refusal, "error": "reject_review_result_invalid"}
    if result.get("ok") is not True:
        result["error"] = str(result.get("stderr") or result.get("error") or "reject_review_failed")
        return result
    cancel = getattr(manager, "cancel_disposed_reviewer_processes", None)
    if callable(cancel) and result.get("reviewer_finalization"):
        try:
            result["reviewer_process_cancellation"] = cancel(
                result["reviewer_finalization"], rejected_request_id=request_id
            )
        except Exception as exc:  # noqa: BLE001 -- the reject committed; cleanup is deferred
            result["cleanup_deferred"] = f"reviewer_process_cancellation:{type(exc).__name__}"
    return result


def _execute(manager: Manager, event: Mapping[str, Any]) -> dict[str, Any] | None:
    repo = Path(manager.repo)
    task_id = str(event.get("task_id") or "")
    request_id = str(event.get("request_id") or "")
    card = task_store.get_task(repo, task_id)
    record = _current_plan(card, request_id)
    if record is None:
        return None
    assert isinstance(card, Mapping)
    receipt: dict[str, Any] = {
        "schema_id": SCHEMA_ID,
        "reason_code": REASON_CODE,
        "actor": ACTOR,
        "rework_kind": "mechanical",
        "request_id": request_id,
        "signature": record.get("signature"),
        "auto_return": int(record.get("prior_auto_returns") or 0) + 1,
    }
    if (
        str(card.get("worker_status") or "") != "review"
        or str(card.get("terminal_substatus") or "") != "validation_failed"
    ):
        receipt["state"] = "skipped_card_moved"
        return receipt
    reject = _reject_pinned(manager, task_id, str(record.get("reason") or ""), request_id)
    if not isinstance(reject, Mapping) or reject.get("ok") is not True:
        error = str((reject or {}).get("error") or "reject_review_failed")[:300]
        receipt["error"] = error
        if error.startswith(STALE_PREDECESSOR_ERROR):
            # The terminal review moved to another request between the plan
            # read and the reject: that request owns the card now, so neither
            # a launch nor a wake on its behalf belongs to this plan.
            receipt["state"] = DECISION_STALE_PLAN
            _record_event(repo, task_id, receipt)
            return receipt
        receipt["state"] = "reject_failed"
        receipt["manager_woken"] = _wake_manager(repo, task_id, request_id)
        _record_event(repo, task_id, receipt)
        return receipt
    receipt["state"] = "rejected_to_pending"
    try:
        launch = manager.launch(
            task_id=task_id,
            runner=str(event.get("runner") or card.get("runner") or ""),
            topic=str(event.get("topic") or card.get("topic") or ""),
            adapter_id=str(record.get("adapter_id") or ""),
            model=str(record.get("model") or ""),
        )
    except Exception as exc:  # noqa: BLE001 -- the reject already committed
        launch = {"ok": False, "error": f"launch_raised:{type(exc).__name__}"}
    launched = isinstance(launch, Mapping)
    receipt["relaunch"] = {
        "ok": bool(launched and launch.get("ok")),
        "request_id": launch.get("request_id") if launched else None,
        "adapter_id": record.get("adapter_id"),
        "model": record.get("model"),
        # ``error`` only: provider/worker stderr never lands in a durable event.
        "error": str(launch.get("error") or "")[:300] if launched else "launch_result_invalid",
    }
    if receipt["relaunch"]["ok"]:
        receipt["state"] = "relaunched"
    else:
        receipt["state"] = "relaunch_failed"
        try:
            receipt["manager_woken"] = _wake_relaunch_failed(repo, task_id, request_id)
        except Exception:  # noqa: BLE001 -- still record the receipt below
            receipt["manager_woken"] = False
    _record_event(repo, task_id, receipt)
    return receipt


def after_finalize(manager: Manager, event: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Carry out a planned auto-return once the finalizer released its lock.

    ``None`` when the terminal event planned none. Never raises: the monitor
    thread that calls this must stay durable, and a failure wakes the manager.
    """
    if not isinstance(event, Mapping) or event.get("state") != "validation_failed":
        return None
    try:
        return _execute(manager, event)
    except Exception as exc:  # noqa: BLE001
        task_id = str(event.get("task_id") or "")
        request_id = str(event.get("request_id") or "")
        receipt = {
            "schema_id": SCHEMA_ID,
            "reason_code": REASON_CODE,
            "state": "failed",
            "error": f"{type(exc).__name__}:{exc}"[:300],
        }
        try:
            receipt["manager_woken"] = _wake_manager(Path(manager.repo), task_id, request_id)
        except Exception:  # noqa: BLE001 -- the deferred-review grace wake remains
            receipt["manager_woken"] = False
        return receipt
