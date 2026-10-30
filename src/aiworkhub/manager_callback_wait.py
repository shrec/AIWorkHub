from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


# The long-poll hold.  Measured 2026-09-08 over 206 manager waits: the model
# asked for 900 s in 112 of them, the server silently clamped to 300 s, and 83
# waits (40%) came back empty, after which the manager polled by hand (240
# Bash sleeps, 83 tail -F monitors).  The cap is now a configured value that is
# negotiated against the host's own tool timeout and ALWAYS reported back, so
# the model can stop asking for what it cannot get.
CALLBACK_WAIT_MAX_SECONDS_ENV = "AIWORKHUB_CALLBACK_WAIT_MAX_SECONDS"
CALLBACK_WAIT_MAX_SECONDS_DEFAULT = 300
_CALLBACK_WAIT_MAX_SECONDS_RANGE = (1, 3600)
# Claude Code exports its per-call MCP tool timeout to the server process in
# milliseconds; a hold longer than that is a response the host will never
# read (the lease reclaim would still recover it -- safe, but a wasted turn).
CALLBACK_WAIT_HOST_TIMEOUT_MS_ENV = "MCP_TOOL_TIMEOUT"
CALLBACK_WAIT_HOST_SAFETY_SECONDS = 15
CALLBACK_WAIT_WATCH_POLL_SECONDS = 2.0
CALLBACK_WAIT_WATCH_LIMIT = 64
CALLBACK_WAIT_SNAPSHOT_LIMIT = 64
CALLBACK_WAIT_REVIEW_WAITING_LIMIT = 20
CALLBACK_WAIT_SKIPPED_STALE_LIMIT = 50


def callback_wait_limits() -> dict[str, Any]:
    """The effective ceiling on one ``claude_callback_wait`` hold, with its
    provenance: the configured value (env, default 300 s) negotiated down to
    stay under the host's MCP tool timeout when the host declares one."""
    configured = CALLBACK_WAIT_MAX_SECONDS_DEFAULT
    source = "default"
    raw = os.environ.get(CALLBACK_WAIT_MAX_SECONDS_ENV, "").strip()
    if raw:
        try:
            low, high = _CALLBACK_WAIT_MAX_SECONDS_RANGE
            configured = max(low, min(int(raw), high))
            source = f"env:{CALLBACK_WAIT_MAX_SECONDS_ENV}"
        except ValueError:
            source = f"default:invalid_{CALLBACK_WAIT_MAX_SECONDS_ENV}"
    host_seconds: float | None = None
    raw_host = os.environ.get(CALLBACK_WAIT_HOST_TIMEOUT_MS_ENV, "").strip()
    if raw_host:
        try:
            host_ms = int(raw_host)
        except ValueError:
            host_ms = 0
        if host_ms > 0:
            host_seconds = host_ms / 1000.0
    max_timeout = configured
    if host_seconds is not None:
        negotiated = max(1, int(host_seconds) - CALLBACK_WAIT_HOST_SAFETY_SECONDS)
        if negotiated < max_timeout:
            max_timeout = negotiated
            source = f"host:{CALLBACK_WAIT_HOST_TIMEOUT_MS_ENV}"
    return {
        "max_timeout_seconds": max_timeout,
        "configured_max_timeout_seconds": configured,
        "host_tool_timeout_seconds": host_seconds,
        "timeout_clamp_source": source,
    }


def _epoch_to_iso(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _seconds_since_iso(value: Any, now_epoch: float) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return round(max(0.0, now_epoch - parsed.timestamp()), 1)


def _claude_session_process_rows(
    session_id: str, watch_request_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Latest ledger row per request that this session launched (its task's
    ``origin_thread_id`` is the session) and is still active, plus every
    watched request regardless of state.  A pure read of the process ledger
    and the tasks table; never a reconcile."""
    from . import core  # lazy: core re-exports this module
    from . import process_event_ledger  # local import: cycle-safe
    from . import process_launcher  # local import: cycle-safe

    root = core.repo_root()
    log_path = Path(
        os.environ.get(
            process_launcher.PROCESS_LOG_ENV,
            str(root / process_launcher.PROCESS_LOG_DEFAULT_REL),
        )
    )
    if not log_path.is_file():
        return []
    latest = process_event_ledger.latest_events(
        log_path,
        key_field="request_id",
        skip_event_kinds=(process_launcher.RUNTIME_NOTICE_EVENT_KIND,),
        replace=True,
    )
    active_states = (
        process_launcher.ACTIVE_PROCESS_STATES
        | process_launcher.FINALIZATION_PENDING_STATES
        | {process_launcher.REVIEWER_CLAIM_BOUND_STATE}
    )
    watched = set(watch_request_ids)
    candidates = [
        dict(row) for request_id, row in latest.items()
        if isinstance(row, dict)
        and (str(row.get("state") or "") in active_states or str(request_id) in watched)
    ]
    if not candidates:
        return []
    task_ids = sorted({str(row.get("task_id") or "") for row in candidates if row.get("task_id")})
    origins: dict[str, str] = {}
    if task_ids:
        conn = core._canonical_connect(readonly=True)
        try:
            for start in range(0, len(task_ids), 200):
                chunk = task_ids[start:start + 200]
                marks = ",".join("?" for _ in chunk)
                for row in conn.execute(
                    f"SELECT task_id, origin_thread_id FROM tasks WHERE task_id IN ({marks})",
                    tuple(chunk),
                ).fetchall():
                    origins[str(row["task_id"])] = str(row["origin_thread_id"] or "")
        finally:
            conn.close()
    return [
        row for row in candidates
        if str(row.get("request_id") or "") in watched
        or origins.get(str(row.get("task_id") or "")) == session_id
    ]


def _claude_session_liveness_snapshot(
    identity: Mapping[str, str], watch_request_ids: Sequence[str],
) -> dict[str, Any]:
    """Compact, read-only evidence returned with an empty wait: what this
    session's launched requests are doing right now, and when the reconciler
    last closed a scan.  It states observations only -- ``state`` and
    ``liveness_state`` are the ledger's and the supervisor artifact's own
    words -- and never infers a terminal outcome (``status()`` already
    refuses to guess on ``pid_identity_unknown``; so does this)."""
    from . import core  # lazy: core re-exports this module
    from . import process_launcher  # local import: cycle-safe
    from . import task_reconciler  # local import: cycle-safe

    now_epoch = time.time()
    snapshot: dict[str, Any] = {
        "schema_id": "aiworkhub.callback_wait_liveness.v1",
        "inference": "none",
        "requests": [],
        "reconciler": {"status_present": False, "last_scan_age_s": None, "stale": True},
    }
    try:
        rows = core._claude_session_process_rows(str(identity.get("session_id") or ""), watch_request_ids)
    except Exception as exc:  # noqa: BLE001 -- evidence must never break the wait
        snapshot["requests_error"] = f"{type(exc).__name__}:{exc}"[:200]
        rows = []
    rows.sort(key=lambda row: str(row.get("timestamp") or ""))
    truncated = max(0, len(rows) - CALLBACK_WAIT_SNAPSHOT_LIMIT)
    for row in rows[:CALLBACK_WAIT_SNAPSHOT_LIMIT]:
        try:
            liveness = process_launcher.ProcessManager._liveness_snapshot(row)
        except Exception:  # noqa: BLE001 -- a bad artifact is reported as absent
            liveness = {}
        runtime = liveness.get("runtime_seconds")
        elapsed = (
            round(float(runtime), 1)
            if isinstance(runtime, (int, float)) and not isinstance(runtime, bool)
            else _seconds_since_iso(row.get("timestamp"), now_epoch)
        )
        last_artifact = _epoch_to_iso(liveness.get("last_meaningful_progress_epoch"))
        if last_artifact is None:
            activity_age = liveness.get("activity_age_seconds")
            if isinstance(activity_age, (int, float)) and not isinstance(activity_age, bool):
                last_artifact = _epoch_to_iso(now_epoch - float(activity_age))
        entry = {
            "request_id": str(row.get("request_id") or ""),
            "task_id": str(row.get("task_id") or ""),
            "state": str(row.get("state") or ""),
            "preparation_phase": row.get("preparation_phase"),
            "elapsed_s": elapsed,
            "last_event_ts": row.get("timestamp"),
            "last_artifact_ts": last_artifact,
            "liveness_state": liveness.get("liveness_state"),
            "reconciliation_deferred": row.get("reconciliation_deferred"),
        }
        progress = process_launcher.format_finalization_progress(liveness.get("finalization_progress"))
        if progress:
            entry["validation_progress"] = progress
        snapshot["requests"].append(entry)
    if truncated:
        snapshot["requests_truncated"] = truncated
    try:
        record = task_reconciler.read_status(core.repo_root())
        stale, age = task_reconciler._staleness(record)
        phase_epoch = record.get("scan_phase_started_epoch")
        phase_known = (
            bool(record.get("scan_in_progress"))
            and isinstance(phase_epoch, (int, float))
            and not isinstance(phase_epoch, bool)
        )
        snapshot["reconciler"] = {
            "status_present": bool(record),
            "last_scan_age_s": round(age, 1) if age is not None else None,
            "stale": bool(stale),
            "scan_in_progress": bool(record.get("scan_in_progress")),
            "last_error": str(record.get("last_error") or ""),
            "scan_phase": str(record.get("scan_phase") or ""),
            "scan_phase_age_s": round(now_epoch - float(phase_epoch), 1) if phase_known else None,
        }
    except Exception as exc:  # noqa: BLE001 -- evidence must never break the wait
        snapshot["reconciler"] = {
            "status_present": False, "last_scan_age_s": None, "stale": True,
            "error": f"{type(exc).__name__}"[:80],
        }
    return snapshot


def _watch_signature(rows: Sequence[Mapping[str, Any]], watch: Sequence[str]) -> dict[str, tuple]:
    by_request = {str(row.get("request_id") or ""): row for row in rows}
    signature: dict[str, tuple] = {}
    for request_id in watch:
        row = by_request.get(request_id)
        if row is None:
            signature[request_id] = ("absent", None, None, None)
            continue
        signature[request_id] = (
            str(row.get("state") or ""),
            row.get("preparation_phase"),
            row.get("exit_code"),
            str(row.get("timestamp") or ""),
        )
    return signature


def _claude_lane_refusal() -> dict[str, Any]:
    """NF-2026-00972: why the Claude callback lane refuses a non-Claude caller.

    Mirrors manager_bootstrap: the open Manager Chat seat, else the verified
    Codex route, owns this caller's wakes, and the reply names it. Only a caller
    with no verified manager route at all gets ``verified_claude_manager_required``.
    """
    from . import core  # lazy: core re-exports this module
    chat = core._active_manager_chat_record()
    if chat:
        return {
            "reason": "callback_lane_owned_by_manager_chat",
            "owning_route": {
                "provider": "manager_chat",
                "session_id": chat["session_id"],
                "delivery": "the Manager Chat loop delivers and acknowledges these wakes",
            },
        }
    codex = core._codex_manager_identity()
    if codex:
        return {
            "reason": "callback_lane_owned_by_codex",
            "owning_route": {
                "provider": "codex",
                "thread_id": str(codex.get("thread_id") or ""),
                "route_state": str(codex.get("route_state") or ""),
                "callback_supported": str(codex.get("callback_supported") or "false"),
                "delivery": "the Codex extension delivers to the verified Codex thread",
            },
        }
    return {"reason": "verified_claude_manager_required"}


def _callback_member_stale_reason(conn: sqlite3.Connection, member: Mapping[str, Any]) -> str:
    """NF-2026-01859: ``task_<state>`` when a claimed callback member no longer
    describes its task (closed, or moved off the transition it announces), else
    ''.  A member with no task row is left to the claim's own terminal-state
    recheck, which already supersedes it."""
    from . import core  # lazy: core re-exports this module
    task_id = str(member.get("task_id") or "")
    row = conn.execute(
        "SELECT status, worker_status, archived_at, card_json FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        return ""
    try:
        card = json.loads(row["card_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        card = {}
    if not isinstance(card, dict):
        card = {}
    status = str(row["status"] or "").strip().lower()
    worker_status = str(row["worker_status"] or "").strip().lower()
    lifecycle = core._lifecycle_state({"status": status, "worker_status": worker_status})
    if str(row["archived_at"] or "").strip() or lifecycle == "archived":
        return "task_archived"
    substatus = str(card.get("terminal_substatus") or "").strip().lower()
    cancelled = {"cancelled", "canceled"}
    if status in cancelled or worker_status in cancelled or (
        lifecycle == "blocked" and substatus in cancelled
    ):
        return "task_cancelled"
    if lifecycle in {"superseded", "finished"}:
        return f"task_{lifecycle}"
    if core.callback_store._task_still_in_matching_terminal_state(
        conn,
        task_id,
        str(member.get("transition") or ""),
        str(member.get("episode_id") or "0"),
    ):
        return ""
    return "task_transition_changed" if lifecycle in {"review", "blocked"} else f"task_{lifecycle}"


def _callback_review_waiting(provider: str, session_id: str) -> dict[str, Any]:
    """NF-2026-01859: review-bucket cards of ``provider`` this wait cannot
    deliver, bounded to ``CALLBACK_WAIT_REVIEW_WAITING_LIMIT`` rows.  Visibility
    only: nothing here claims, seeds or acknowledges, and the claim's route
    scoping is untouched.  ``reason`` is other_origin (a callback row for the
    current episode belongs to another origin), deferred_review_chain (the wake
    is held for the quality-review chain) or no_callback_row."""
    from . import core  # lazy: core re-exports this module
    waiting: list[dict[str, str]] = []
    try:
        conn = core._canonical_connect(readonly=True)
        try:
            tasks = conn.execute(
                "SELECT task_id, status, card_json FROM tasks "
                "WHERE status IN ('review', 'blocked') AND (archived_at IS NULL OR archived_at='') "
                "ORDER BY updated_at DESC, task_id ASC"
            )
            for task in tasks:
                if len(waiting) >= CALLBACK_WAIT_REVIEW_WAITING_LIMIT:
                    break
                try:
                    card = json.loads(task["card_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    card = {}
                if not isinstance(card, dict):
                    card = {}
                if str(card.get("coordinator_provider") or provider).strip().lower() != provider:
                    continue
                substatus = str(card.get("terminal_substatus") or "").strip()
                if str(task["status"] or "") == "blocked" and (
                    core.callback_store.normalize_callback_transition(substatus)
                    not in core.callback_store.CALLBACK_ELIGIBLE_TRANSITIONS
                ):
                    continue
                task_id = str(task["task_id"] or "")
                outbox = conn.execute(
                    "SELECT origin_thread_id, state FROM callback_outbox "
                    "WHERE task_id=? AND provider IN (?, ?) AND episode_id=?",
                    (
                        task_id,
                        provider,
                        core.callback_store.MANAGER_CHAT_PROVIDER,
                        str(card.get("claim_epoch") or 0),
                    ),
                ).fetchall()
                if any(
                    row["origin_thread_id"] == session_id
                    and row["state"] in {"pending", "inflight", "delivered"}
                    for row in outbox
                ):
                    continue
                review = card.get("terminal_review")
                evidence = review.get("evidence") if isinstance(review, dict) else None
                if any(row["origin_thread_id"] != session_id for row in outbox):
                    reason = "other_origin"
                elif (
                    isinstance(evidence, dict)
                    and isinstance(evidence.get("manager_callback_deferred"), dict)
                    and not card.get("manager_ready_receipt")
                ):
                    reason = "deferred_review_chain"
                else:
                    reason = "no_callback_row"
                waiting.append(
                    {
                        "task_id": task_id,
                        "status": str(task["status"] or ""),
                        "terminal_substatus": substatus,
                        "reason": reason,
                    }
                )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 -- evidence must never break the wait
        return {"review_waiting": [], "review_waiting_error": type(exc).__name__}
    return {"review_waiting": waiting}


def claude_callback_wait(
    timeout_seconds: int = 240,
    ack_batch_id: str = "",
    ack_lease_id: str = "",
    watch_request_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Wait for one callback batch belonging to this exact Claude manager.

    Returning the batch wakes the already-open Claude turn through its own
    MCP request.  Delivery remains inflight until it is acknowledged; if the
    tool response is lost, the normal lease reclaim path retries it.

    Acknowledgement rides on the next wait: ``ack_batch_id``/``ack_lease_id``
    are the ids the previous ``callback_ready`` reply carried, and they are
    acknowledged inside the same identity-checked transaction that claims the
    next batch (``callback_store.acknowledge_then_claim``).  The explicit
    ``claude_callback_ack`` remains available.  Nothing is acknowledged at
    delivery time.

    The hold is capped by :func:`callback_wait_limits`; the reply always says
    what the effective cap was.  An empty wait returns a read-only liveness
    snapshot of this session's launched requests instead of nothing, and a
    ``watch_request_ids`` list also wakes the wait when a watched request's
    ledger row changes (a non-terminal milestone), reported as
    ``watch_changed``.  When another route owns the lane, a non-empty
    ``watch_request_ids`` runs ``callback_watch.read_only_watch`` instead of
    refusing: it never claims, leases or acknowledges (NF-2026-01769).
    """
    from . import core  # lazy: core re-exports this module
    from . import callback_watch  # local import: cycle-safe

    identity = core._claude_manager_identity()
    if identity is None:
        # seams resolve through core so monkeypatch.setattr(core, ...) keeps working
        refusal = core._claude_lane_refusal()
        if watch_request_ids and refusal["reason"] in callback_watch.WATCHABLE_LANE_OWNERS:
            return callback_watch.read_only_watch(refusal, watch_request_ids, timeout_seconds)
        return {"ok": False, **refusal}
    timeout, timing = callback_watch.callback_wait_timing(timeout_seconds)
    watch = callback_watch.normalize_watch_ids(watch_request_ids)
    ack_pending = bool(str(ack_batch_id or "").strip() or str(ack_lease_id or "").strip())
    ack_result: dict[str, Any] | None = None
    session_id = identity["session_id"]

    def _watch_rows() -> list[dict[str, Any]]:
        try:
            return core._claude_session_process_rows(session_id, watch)
        except Exception:  # noqa: BLE001 -- a watch is evidence, never a failure
            return []

    watch_baseline = _watch_signature(_watch_rows(), watch) if watch else {}
    next_watch_poll = time.monotonic() + core.CALLBACK_WAIT_WATCH_POLL_SECONDS
    deadline = time.monotonic() + timeout
    skipped_stale: list[dict[str, str]] = []
    while True:
        conn = core._canonical_connect()
        try:
            core.callback_store.seed_missing_review_callbacks(
                conn,
                provider="claude",
                origin_thread_id=session_id,
            )
            # Claim at route level, not just provider level: pass this verified
            # manager's own session identity through so a second manager on the
            # same repository can never lease (and then park) a batch belonging
            # to another route. The store's ``origin_thread_id`` scope is exactly
            # this guarantee; passing only ``provider`` left it unused.
            if ack_pending:
                ack_result, batch = core.callback_store.acknowledge_then_claim(
                    conn,
                    ack_batch_id=str(ack_batch_id or ""),
                    ack_lease_id=ack_lease_id,
                    provider="claude",
                    origin_thread_id=session_id,
                    lease_seconds=max(120, timeout + 30),
                )
                ack_pending = False
            else:
                batch = core.callback_store.claim_pending_callback_batch(
                    conn,
                    lease_seconds=max(120, timeout + 30),
                    provider="claude",
                    origin_thread_id=session_id,
                )
        finally:
            conn.close()
        if batch is not None:
            if batch.get("origin_thread_id") != session_id:
                conn = core._canonical_connect()
                try:
                    core.callback_store.defer_batch_busy(
                        conn,
                        str(batch["batch_id"]),
                        "callback_belongs_to_different_claude_session",
                        str(batch["lease_id"]),
                        delay_seconds=30.0,
                    )
                finally:
                    conn.close()
            else:
                members: list[dict[str, Any]] = []
                conn = core._canonical_connect(readonly=True)
                try:
                    for member in batch.get("members", []):
                        entry: dict[str, Any] = {
                            "task_id": str(member.get("task_id") or ""),
                            "state": str(member.get("transition") or ""),
                            "event_id": str(member.get("event_id") or ""),
                            "request_id": str(member.get("request_id") or ""),
                        }
                        stale_reason = core._callback_member_stale_reason(conn, member)
                        if stale_reason:
                            entry["actionable"] = False
                            entry["stale_reason"] = stale_reason
                        members.append(entry)
                finally:
                    conn.close()
                stale_acked = False
                if members and all(entry.get("actionable") is False for entry in members):
                    conn = core._canonical_connect()
                    try:
                        stale_acked = core.callback_store.acknowledge_callback_batch(
                            conn,
                            str(batch["batch_id"]),
                            str(batch["lease_id"]),
                            provider="claude",
                            origin_thread_id=session_id,
                        )
                    finally:
                        conn.close()
                # An unacknowledged all-stale batch is delivered like any other, so
                # skipped_stale only ever names batches that were really consumed.
                if not stale_acked:
                    return {
                        "ok": True,
                        "status": "callback_ready",
                        "provider": "claude",
                        "batch_id": batch["batch_id"],
                        "lease_id": batch["lease_id"],
                        "origin_thread_id": batch["origin_thread_id"],
                        "members": members,
                        "ack": ack_result,
                        "ack_hint": (
                            "pass batch_id/lease_id as ack_batch_id/ack_lease_id on the next "
                            "aiworkhub_claude_callback_wait, or call aiworkhub_claude_callback_ack"
                        ),
                        **({"skipped_stale": list(skipped_stale)} if skipped_stale else {}),
                        **timing,
                    }
                for entry in members:
                    if len(skipped_stale) < CALLBACK_WAIT_SKIPPED_STALE_LIMIT:
                        skipped_stale.append(
                            {
                                "task_id": entry["task_id"],
                                "batch_id": str(batch["batch_id"]),
                                "stale_reason": entry["stale_reason"],
                            }
                        )
                # Claim the next batch at once, but never past the wait's deadline;
                # once it has passed, fall through to the timeout reply below.
                if deadline - time.monotonic() > 0:
                    continue
        now = time.monotonic()
        if watch and now >= next_watch_poll:
            next_watch_poll = now + core.CALLBACK_WAIT_WATCH_POLL_SECONDS
            current = _watch_signature(_watch_rows(), watch)
            changes = [
                {
                    "request_id": request_id,
                    "from": {"state": before[0], "preparation_phase": before[1], "exit_code": before[2]},
                    "to": {"state": after[0], "preparation_phase": after[1], "exit_code": after[2]},
                }
                for request_id, before in watch_baseline.items()
                for after in (current.get(request_id),)
                if after is not None and after != before
            ]
            if changes:
                return {
                    "ok": True,
                    "status": "watch_changed",
                    "provider": "claude",
                    "changes": changes,
                    "ack": ack_result,
                    "liveness": core._claude_session_liveness_snapshot(identity, watch),
                    **core._callback_review_waiting("claude", session_id),
                    **({"skipped_stale": list(skipped_stale)} if skipped_stale else {}),
                    **timing,
                }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "ok": True,
                "status": "timeout_no_callback",
                "provider": "claude",
                "waited_seconds": timeout,
                "ack": ack_result,
                "liveness": core._claude_session_liveness_snapshot(identity, watch),
                **core._callback_review_waiting("claude", session_id),
                **({"skipped_stale": list(skipped_stale)} if skipped_stale else {}),
                **timing,
            }
        time.sleep(min(0.5, remaining))


def claude_callback_ack(batch_id: str, lease_id: str) -> dict[str, Any]:
    """Durably acknowledge a callback returned by ``claude_callback_wait``.

    The explicit fallback; the same acknowledgement rides on the next wait
    (``ack_batch_id``/``ack_lease_id``) and on a same-session action naming a
    member (``claude_callback_ack_by_reference``)."""
    from . import core  # lazy: core re-exports this module
    identity = core._claude_manager_identity()
    if identity is None:
        return {"ok": False, **core._claude_lane_refusal()}
    conn = core._canonical_connect()
    try:
        acknowledged = core.callback_store.acknowledge_callback_batch(
            conn,
            batch_id,
            lease_id,
            provider="claude",
            origin_thread_id=identity["session_id"],
        )
    finally:
        conn.close()
    return {
        "ok": acknowledged,
        "status": "delivered" if acknowledged else "ack_rejected",
        "provider": "claude",
        "batch_id": batch_id,
    }
