"""Canonical, repository-bound task-authority reads (B863).

``core.show_task`` resolves its own repository ambiently, via
``core.repo_root()`` (the ``AIWORKHUB_REPO`` env var, or a ``DEFAULT_REPO``
fallback). A caller that already knows exactly which repository's isolated
workspace it launched a worker against -- e.g. ``ProcessManager.repo``, or an
explicit ``--repo`` passed to the reconciler daemon -- has no way to make
``core.show_task`` honor that binding: it always re-resolves the repo on its
own, independently, at call time. When the ambient resolution and the
caller's already-known repo diverge (multiple repositories handled by one
process, a reconciler invoked with an explicit ``--repo``, or a nested
independent repository misresolved to its outer checkout), the launcher and
the finalizer end up reading two different ``.aiworkhub/tasking/task_queue.sqlite``
files for the same claim/finalization decision -- the exact disagreement that
produces a false ``claim_ownership_lost``.

Every read here takes ``repo`` explicitly and never falls back to ambient
env/cwd state, so a caller bound to one repository can never have its
claim/finalization authority silently answered by a different repository's
queue. This is also, by construction, immune to "legacy JSONL/card_json
claim field" override: the only data source is ``task_store.get_task``,
whose canonical SQLite row always wins over any stale ``card_json`` copy.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import callback_store
from . import core
from . import db_writer
from . import task_store


def _effective_topic(row: Any) -> str:
    """Same fallback ``task_store.get_task``/``list_tasks`` already apply:
    an older writer could persist the canonical topic only in ``card_json``
    while leaving the ``tasks.topic`` SQL column at its schema default of
    ``''`` (see ``task_store.py``'s own migration comment for this exact
    class of task). A raw ``row["topic"]`` compare would then read as a
    mismatch for a task every read path already reports correctly."""
    topic = str(row["topic"] or "")
    if topic:
        return topic
    try:
        card_json = json.loads(row["card_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return topic
    return str(card_json.get("topic") or "") if isinstance(card_json, dict) else topic


def claim_start_exact(
    repo: Path, task_id: str, runner: str, topic: str, request_id: str = ""
) -> dict[str, Any]:
    """Same wire contract and authority as ``core.claim_start_exact`` --
    same write gate, same fail-closed identity/collision behavior -- but
    bound to an explicit ``repo`` (see module docstring) instead of an
    ambiently re-resolved one, and normalized against the same topic
    fallback ``_effective_topic``/``task_store.get_task`` already tolerate
    for reads. This is the one place a caller that already knows its exact
    bound repository (``ProcessManager.repo``) claims a task; it never
    widens the write gate -- ``core._canonical_write_gate`` (the same
    runner/topic allowlist plus card-scoped authority check
    ``core.claim_start_exact`` itself uses) still runs unchanged first.
    """
    command = ["claim-start", task_id, "--runner", runner, "--topic", topic]
    if request_id:
        command.extend(["--request-id", request_id])
    blocked = core._canonical_write_gate(
        "claim-start", runner=runner, topic=topic, task_id=task_id
    )
    if blocked is not None:
        return blocked
    now = datetime.now(timezone.utc).isoformat()
    try:
        _readiness, db_path = task_store._require_ready(repo)
    except task_store.TaskStoreError as exc:
        return {"ok": False, "returncode": 1, "command": command, "stdout": "", "stderr": str(exc)}
    # One serialized writer per database, ACROSS PROCESSES. The claim CAS below
    # is the exact statement the measured launch-path traceback dies on
    # (process_launcher._launch_isolated -> task_engine.claim_start_exact ->
    # conn.execute -> sqlite3.OperationalError: database is locked), and its
    # competing writers are separate supervisor OS processes -- 169 distinct
    # pids, up to 12 alive at once -- so an in-process queue could not have
    # made them take turns. The lease changes the failure mode from
    # "lock -> fail -> retry" to "queue -> transaction -> commit"; it does not
    # change the transaction, the preimage CAS, or the error contract below.
    #
    # The lease is taken BEFORE _connect deliberately: _connect issues
    # "PRAGMA journal_mode=WAL", which is itself a write and does raise
    # "database is locked" under contention (callback_store.open_db already
    # retries that exact statement). Connecting outside the lease would leave
    # the first statement of the claim path unserialized.
    lease_stack = contextlib.ExitStack()
    try:
        lease_stack.enter_context(db_writer.write_lease(db_path))
    except db_writer.WriteLeaseError as exc:
        return {
            "ok": False,
            "returncode": 1,
            "command": command,
            "stdout": "",
            "stderr": f"task_queue_write_lease_unavailable:{exc}",
        }
    try:
        conn = task_store._connect(db_path)
    except task_store.TaskStoreError as exc:
        lease_stack.close()
        return {"ok": False, "returncode": 1, "command": command, "stdout": "", "stderr": str(exc)}
    try:
        row = conn.execute(
            "SELECT runner, topic, status, worker_status, claimed_by, card_json "
            "FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return {
                "ok": False, "returncode": 1, "command": command, "stdout": "",
                "stderr": f"task_not_found:{task_id}",
            }
        if row["runner"] != runner or _effective_topic(row) != topic:
            conn.rollback()
            return {
                "ok": False, "returncode": 1, "command": command, "stdout": "",
                "stderr": f"identity_mismatch:task_id={task_id}",
            }
        try:
            stored_card = json.loads(row["card_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            stored_card = {}
        if not isinstance(stored_card, dict):
            stored_card = {}
        raw_card_json = str(row["card_json"] or "{}")
        status = str(row["status"] or "").strip().lower()
        worker_status = str(row["worker_status"] or "").strip().lower()
        claimed_by = str(row["claimed_by"] or "")
        attached_request_id = str(stored_card.get("launch_request_id") or "")

        if status == "processing" and worker_status == "claimed" and claimed_by == runner:
            if not request_id:
                conn.rollback()
                return {
                    "ok": False, "returncode": 1, "command": command, "stdout": "",
                    "stderr": f"claimed_task_requires_launch_request_id:task_id={task_id}",
                }
            if attached_request_id == request_id:
                conn.rollback()
                card = task_store.get_task(repo, task_id)
                stdout = json.dumps(card, ensure_ascii=False, default=str) if card else ""
                return {
                    "ok": True,
                    "returncode": 0,
                    "command": command,
                    "stdout": stdout,
                    "stderr": "",
                    "claim_reconciled": True,
                }
            if attached_request_id:
                conn.rollback()
                return {
                    "ok": False, "returncode": 1, "command": command, "stdout": "",
                    "stderr": f"launch_request_conflict:task_id={task_id}",
                }
            stored_card["launch_request_id"] = request_id
            cur = conn.execute(
                "UPDATE tasks SET card_json=?, updated_at=? "
                "WHERE task_id=? AND status='processing' AND worker_status='claimed' "
                "AND claimed_by=? AND card_json=?",
                (
                    json.dumps(stored_card, ensure_ascii=False, sort_keys=True),
                    now,
                    task_id,
                    runner,
                    raw_card_json,
                ),
            )
            event_name = "launch_attach"
            try:
                claim_epoch = int(stored_card.get("claim_epoch") or 0)
            except (TypeError, ValueError):
                claim_epoch = 0
            prior_episode: dict[str, Any] = {}
        else:
            try:
                claim_epoch = int(stored_card.get("claim_epoch") or 0) + 1
            except (TypeError, ValueError):
                claim_epoch = 1
            replay = stored_card.get("validation_only_replay_authorization")
            if replay is not None:
                predecessor = stored_card.get("rework_predecessor")
                hashes = replay.get("changed_path_hashes") if isinstance(replay, dict) else None
                if (
                    not isinstance(replay, dict)
                    or not isinstance(predecessor, dict)
                    or replay.get("task_id") != task_id
                    or replay.get("actor") != core.CODEX_RUNNER
                    or replay.get("one_episode_binding") is not True
                    or type(replay.get("next_claim_epoch")) is not int
                    or replay["next_claim_epoch"] != claim_epoch - 1
                    or not request_id
                    or replay.get("request_id")
                    or replay.get("repo") not in (None, "", str(repo.resolve()))
                    or not replay.get("predecessor_request_id")
                    or replay["predecessor_request_id"] != predecessor.get("request_id")
                    or not isinstance(hashes, dict)
                    or not hashes
                    or hashes != predecessor.get("changed_path_hashes")
                    or not all(
                        isinstance(path, str) and path.strip()
                        and isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest)
                        for path, digest in hashes.items()
                    )
                ):
                    conn.rollback()
                    return {
                        "ok": False, "returncode": 1, "command": command, "stdout": "",
                        "stderr": "validation_only_replay_claim_binding_invalid",
                    }
                # Consume the pending grant in the same CAS as the new claim.
                # The launcher must derive its receipt from this committed
                # episode, never rewrite an inherited/signed evidence receipt.
                stored_card["validation_only_replay_authorization"] = {
                    **replay, "next_claim_epoch": claim_epoch,
                    "request_id": request_id, "repo": str(repo.resolve()),
                }
            prior_episode = task_store.begin_claim_episode(stored_card)
            # A prior pre-claim launch failure is operational history, not a
            # permanent task lifecycle state.  This exact successful claim is
            # the authority that clears it.
            stored_card.pop("operational_blocker", None)
            stored_card.update(
                claim_epoch=claim_epoch,
                launch_request_id=request_id,
                status="processing",
                worker_status="claimed",
                claimed_by=runner,
            )
            cur = conn.execute(
                "UPDATE tasks SET card_json=?, worker_status='claimed', status='processing', claimed_by=?, "
                "claimed_at=?, started_at=?, completed_at=NULL, updated_at=? "
                "WHERE task_id=? AND worker_status='unclaimed' AND status='pending' AND card_json=?",
                (
                    json.dumps(stored_card, ensure_ascii=False, sort_keys=True),
                    runner,
                    now,
                    now,
                    now,
                    task_id,
                    raw_card_json,
                ),
            )
            event_name = "claim_start"
        if cur.rowcount != 1:
            conn.rollback()
            return {
                "ok": False, "returncode": 1, "command": command, "stdout": "",
                "stderr": f"claim_conflict:task_id={task_id}",
            }
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (
                task_id, event_name, runner,
                json.dumps(
                    {
                        "runner": runner,
                        "topic": topic,
                        "request_id": request_id,
                        "claim_epoch": claim_epoch,
                        "prior_episode": prior_episode,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
            ),
        )
        conn.commit()
    finally:
        try:
            conn.close()
        finally:
            lease_stack.close()
    card = task_store.get_task(repo, task_id)
    stdout = json.dumps(card, ensure_ascii=False, default=str) if card else ""
    return {"ok": True, "returncode": 0, "command": command, "stdout": stdout, "stderr": ""}


def record_launch_blocker(
    repo: Path,
    task_id: str,
    runner: str,
    topic: str,
    *,
    adapter_id: str,
    reason: str,
    request_id: str = "",
) -> dict[str, Any]:
    """Record a pre-claim launch blocker without fabricating a claim.

    The card remains pending/unclaimed and can be retried explicitly after the
    environment is repaired, but Plan-DAG/auto-pickup can now see the exact
    operational blocker instead of looping or reporting an empty blocker map.

    A launch-preflight rejection observed against a card THIS exact runner
    already owns (status=processing/worker_status=claimed/claimed_by=runner --
    e.g. a retried launch attempt whose earlier claim succeeded before this
    rejection) can never go through the CAS below: the card-scoped
    "launch-blocked" write gate itself only authorizes a pending/unclaimed
    card, so it denies this case before the CAS ever runs. NF-2026-00772: the
    caller (process_launcher's isolated-launch failure path) branches on
    whether ITS OWN attempt performed the claim, so a retry against an
    already-owned claim used to hit that gate denial with no fallback,
    leaving the card stuck ``processing``/``claimed`` forever with no
    recorded reason -- a phantom processing owner. Detecting that exact case
    up front and retiring the claim atomically through the same terminal
    ``mark_launch_failed`` transition a post-claim failure already uses
    closes that gap: the rejection always resolves to exactly one typed,
    persisted blocker.
    """
    command = ["launch-blocked", task_id, "--runner", runner]
    precheck = task_store.get_task(repo, task_id) or {}
    if (
        precheck.get("runner") == runner
        and str(precheck.get("topic") or "") == topic
        and str(precheck.get("status") or "").lower() == "processing"
        and str(precheck.get("worker_status") or "").lower() == "claimed"
        and str(precheck.get("claimed_by") or "") == runner
    ):
        effective_request_id = request_id or str(precheck.get("launch_request_id") or "")
        return mark_launch_failed(
            repo, task_id, runner, reason=reason, request_id=effective_request_id,
        )
    # A launch blocker is a narrower card-scoped write than claim-start. It is
    # authorized only for this exact pending/unclaimed card, including legacy
    # cards incorrectly owned by the coordinator runner; it never grants that
    # runner claim/review/usage authority and has no coordinator token-file
    # dependency.
    blocked = core._canonical_write_gate(
        "launch-blocked",
        runner=runner,
        topic=topic,
        task_id=task_id,
    )
    if blocked is not None:
        # Preserve the card-gate denial while reporting the operation the
        # launcher actually requested.
        return {**blocked, "command": command}
    now = datetime.now(timezone.utc).isoformat()
    try:
        _readiness, db_path = task_store._require_ready(repo)
        conn = task_store._connect(db_path)
    except task_store.TaskStoreError as exc:
        return {"ok": False, "returncode": 1, "command": command, "stdout": "", "stderr": str(exc)}
    try:
        row = conn.execute(
            "SELECT runner, topic, status, worker_status, claimed_by, card_json FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return {"ok": False, "returncode": 1, "command": command, "stdout": "", "stderr": f"task_not_found:{task_id}"}
        if row["runner"] != runner or _effective_topic(row) != topic:
            conn.rollback()
            return {"ok": False, "returncode": 1, "command": command, "stdout": "", "stderr": "identity_mismatch"}
        status = str(row["status"] or "").lower()
        worker_status = str(row["worker_status"] or "").lower()
        if status == "processing" and worker_status == "claimed" and str(row["claimed_by"] or "") == runner:
            try:
                stuck_card = json.loads(str(row["card_json"] or "{}"))
            except (TypeError, json.JSONDecodeError):
                stuck_card = {}
            if not isinstance(stuck_card, dict):
                stuck_card = {}
            effective_request_id = request_id or str(stuck_card.get("launch_request_id") or "")
            conn.rollback()
            return mark_launch_failed(
                repo, task_id, runner, reason=reason, request_id=effective_request_id,
            )
        if status != "pending" or worker_status != "unclaimed":
            conn.rollback()
            return {"ok": False, "returncode": 1, "command": command, "stdout": "", "stderr": "task_not_pending_unclaimed"}
        raw_card = str(row["card_json"] or "{}")
        try:
            card = json.loads(raw_card)
        except (TypeError, json.JSONDecodeError):
            card = {}
        if not isinstance(card, dict):
            card = {}
        card["operational_blocker"] = {
            "kind": "launch_blocked",
            "adapter_id": str(adapter_id)[:128],
            "reason": str(reason)[:500],
            "observed_at": now,
        }
        cur = conn.execute(
            "UPDATE tasks SET card_json=?, updated_at=? "
            "WHERE task_id=? AND status='pending' AND worker_status='unclaimed' AND card_json=?",
            (json.dumps(card, ensure_ascii=False, sort_keys=True), now, task_id, raw_card),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return {"ok": False, "returncode": 1, "command": command, "stdout": "", "stderr": "launch_blocker_conflict"}
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (
                task_id,
                "launch_blocked",
                runner,
                json.dumps(card["operational_blocker"], ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "returncode": 0,
        "command": command,
        "stdout": json.dumps({"task_id": task_id, "status": "pending"}, ensure_ascii=False),
        "stderr": "",
    }


def show_task(repo: Path, task_id: str) -> dict[str, Any]:
    """Same wire contract as ``core.show_task`` (a ``TaskCtlResult.as_dict()``
    envelope whose ``stdout`` is the canonical card JSON), but bound to an
    explicit ``repo`` instead of an ambiently re-resolved one."""
    command = ["show", task_id]
    try:
        card = task_store.get_task(repo, task_id)
    except task_store.TaskStoreError as exc:
        return {"ok": False, "returncode": 1, "command": command, "stdout": "", "stderr": str(exc)}
    if card is None:
        return {
            "ok": True,
            "returncode": 0,
            "command": command,
            "stdout": f"Task not found: {task_id}",
            "stderr": "",
        }
    stdout = json.dumps(card, indent=2, ensure_ascii=False, default=str)
    return {"ok": True, "returncode": 0, "command": command, "stdout": stdout, "stderr": ""}


def mark_terminal_review(
    repo: Path,
    task_id: str,
    runner: str,
    substatus: str,
    *,
    evidence: dict[str, Any] | None = None,
    notify_manager: bool = True,
) -> dict[str, Any]:
    command = ["terminal-review", task_id, "--runner", runner, "--substatus", substatus]
    card = task_store.get_task(repo, task_id) or {}
    provider = str(card.get("coordinator_provider") or "").strip().lower()
    callback_deferred = isinstance(
        (evidence or {}).get("manager_callback_deferred"), dict
    )
    transition = (
        callback_store.resolve_callback_transition(substatus)
        if notify_manager and not callback_deferred
        else ""
    )
    try:
        ok, state, callback_enqueued = task_store.mark_terminal_review_with_callback(
            repo,
            task_id,
            runner=runner,
            substatus=substatus,
            evidence=evidence or {},
            callback_transition=transition,
            callback_provider=provider,
            callback_request_id=str((evidence or {}).get("request_id") or ""),
        )
    except (task_store.TaskStoreError, sqlite3.Error) as exc:
        return {"ok": False, "returncode": 1, "command": command, "stdout": "", "stderr": str(exc)}
    return {
        "ok": ok,
        "returncode": 0 if ok else 1,
        "command": command,
        "stdout": json.dumps({"task_id": task_id, "status": state}, ensure_ascii=False),
        "stderr": "" if ok else state,
        "callback_enqueued": callback_enqueued,
    }


def mark_launch_failed(
    repo: Path,
    task_id: str,
    runner: str,
    *,
    reason: str,
    request_id: str = "",
) -> dict[str, Any]:
    """Truthfully block a task whose worker process never launched."""
    command = ["launch-failed", task_id, "--runner", runner]
    try:
        ok, state = task_store.mark_launch_failed(
            repo,
            task_id,
            runner=runner,
            reason=reason,
            request_id=request_id,
        )
    except task_store.TaskStoreError as exc:
        return {
            "ok": False,
            "returncode": 1,
            "command": command,
            "stdout": "",
            "stderr": str(exc),
            "callback_enqueued": False,
        }
    callback_enqueued = False
    if ok:
        card = task_store.get_task(repo, task_id) or {}
        try:
            _readiness, db_path = task_store._require_ready(repo)
            conn = task_store._connect(db_path)
            try:
                callback_store.init_db(conn)
                origin_thread_id = (
                    callback_store.read_origin_thread(conn, task_id)
                    or str(card.get("origin_thread_id") or "").strip()
                )
                provider = str(card.get("coordinator_provider") or "").strip().lower()
                callback_enqueued = callback_store.enqueue_callback(
                    conn,
                    task_id,
                    origin_thread_id,
                    callback_store.resolve_callback_transition("launch_failed"),
                    provider=provider,
                    episode_id=str(card.get("claim_epoch") or 0),
                    request_id=request_id,
                )
                conn.commit()
            finally:
                conn.close()
        except task_store.TaskStoreError:
            callback_enqueued = False
    return {
        "ok": ok,
        "returncode": 0 if ok else 1,
        "command": command,
        "stdout": json.dumps({"task_id": task_id, "status": state}, ensure_ascii=False),
        "stderr": "" if ok else state,
        "callback_enqueued": callback_enqueued,
    }


def mark_terminal_failure(
    repo: Path,
    task_id: str,
    runner: str,
    substatus: str,
    *,
    evidence: dict[str, Any] | None = None,
    request_id: str = "",
) -> dict[str, Any]:
    """Truthfully block one launched worker failure and notify the manager."""
    command = ["terminal-failure", task_id, "--runner", runner, "--substatus", substatus]
    try:
        ok, state = task_store.mark_terminal_failure(
            repo,
            task_id,
            runner=runner,
            substatus=substatus,
            evidence=evidence or {},
            request_id=request_id,
        )
    except task_store.TaskStoreError as exc:
        return {
            "ok": False,
            "returncode": 1,
            "command": command,
            "stdout": "",
            "stderr": str(exc),
            "callback_enqueued": False,
        }
    callback_enqueued = False
    if ok:
        card = task_store.get_task(repo, task_id) or {}
        try:
            _readiness, db_path = task_store._require_ready(repo)
            conn = task_store._connect(db_path)
            try:
                callback_store.init_db(conn)
                origin_thread_id = (
                    callback_store.read_origin_thread(conn, task_id)
                    or str(card.get("origin_thread_id") or "").strip()
                )
                provider = str(card.get("coordinator_provider") or "").strip().lower()
                callback_enqueued = callback_store.enqueue_callback(
                    conn,
                    task_id,
                    origin_thread_id,
                    callback_store.resolve_callback_transition(substatus),
                    provider=provider,
                    episode_id=str(card.get("claim_epoch") or 0),
                    request_id=request_id,
                )
                conn.commit()
            finally:
                conn.close()
        except task_store.TaskStoreError:
            callback_enqueued = False
    return {
        "ok": ok,
        "returncode": 0 if ok else 1,
        "command": command,
        "stdout": json.dumps({"task_id": task_id, "status": state}, ensure_ascii=False),
        "stderr": "" if ok else state,
        "callback_enqueued": callback_enqueued,
    }


def mark_review_workspace_missing(
    repo: Path,
    task_id: str,
    runner: str,
    request_id: str,
    *,
    reason: str,
) -> dict[str, Any]:
    """Block an exact unusable review and notify its canonical manager."""

    command = ["review-workspace-missing", task_id, "--request-id", request_id]
    try:
        ok, state = task_store.mark_review_workspace_missing(
            repo,
            task_id,
            runner=runner,
            request_id=request_id,
            reason=reason,
        )
    except task_store.TaskStoreError as exc:
        return {
            "ok": False,
            "returncode": 1,
            "command": command,
            "stdout": "",
            "stderr": str(exc),
            "callback_enqueued": False,
        }
    callback_enqueued = False
    if ok:
        card = task_store.get_task(repo, task_id) or {}
        try:
            _readiness, db_path = task_store._require_ready(repo)
            conn = task_store._connect(db_path)
            try:
                callback_store.init_db(conn)
                origin_thread_id = (
                    callback_store.read_origin_thread(conn, task_id)
                    or str(card.get("origin_thread_id") or "").strip()
                )
                provider = str(card.get("coordinator_provider") or "").strip().lower()
                callback_enqueued = callback_store.enqueue_callback(
                    conn,
                    task_id,
                    origin_thread_id,
                    callback_store.resolve_callback_transition("finalize_failed"),
                    provider=provider,
                    episode_id=str(card.get("claim_epoch") or 0),
                    request_id=request_id,
                )
                conn.commit()
            finally:
                conn.close()
        except task_store.TaskStoreError:
            callback_enqueued = False
    return {
        "ok": ok,
        "returncode": 0 if ok else 1,
        "command": command,
        "stdout": json.dumps({"task_id": task_id, "status": state}, ensure_ascii=False),
        "stderr": "" if ok else state,
        "callback_enqueued": callback_enqueued,
    }


def retry_finalize_failed(
    repo: Path,
    task_id: str,
    runner: str,
    request_id: str,
    *,
    actor: str = "codex",
) -> dict[str, Any]:
    """Authorize deterministic finalization retry for one retained request."""
    command = ["retry-finalization", task_id, "--request-id", request_id]
    try:
        ok, state = task_store.retry_finalize_failed(
            repo,
            task_id,
            runner=runner,
            request_id=request_id,
            actor=actor,
        )
    except task_store.TaskStoreError as exc:
        return {
            "ok": False,
            "returncode": 1,
            "command": command,
            "stdout": "",
            "stderr": str(exc),
        }
    return {
        "ok": ok,
        "returncode": 0 if ok else 1,
        "command": command,
        "stdout": json.dumps({"task_id": task_id, "status": state}, ensure_ascii=False),
        "stderr": "" if ok else state,
    }


ACCEPTED_OUTCOME_RECEIPT_SCHEMA = "aiworkhub.accepted_outcome_receipt.v1"


def _canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _validate_accepted_outcome_receipt(
    repo: Path,
    card: dict[str, Any],
    task_id: str,
    request_id: str,
    receipt: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(receipt, dict):
        return None, "accepted_outcome_receipt_missing"
    required = {
        "schema_id", "receipt_id", "task_id", "request_id", "claim_epoch",
        "base_oid", "promoted_paths", "changed_path_hashes",
        "attempt_artifact_manifest_id", "repository_revision",
    }
    if (
        set(receipt) != required
        or receipt.get("schema_id") != ACCEPTED_OUTCOME_RECEIPT_SCHEMA
    ):
        return None, "accepted_outcome_receipt_malformed"
    digest_fields = (
        receipt.get("receipt_id"),
        receipt.get("attempt_artifact_manifest_id"),
        receipt.get("repository_revision"),
    )
    promoted_paths = receipt.get("promoted_paths")
    if (
        not isinstance(receipt.get("task_id"), str)
        or not isinstance(receipt.get("request_id"), str)
        or not isinstance(receipt.get("claim_epoch"), int)
        or isinstance(receipt.get("claim_epoch"), bool)
        or not isinstance(receipt.get("base_oid"), str)
        or not receipt.get("base_oid")
        or not isinstance(promoted_paths, list)
        or any(not isinstance(path, str) for path in promoted_paths)
        or promoted_paths != sorted(set(promoted_paths))
        or not isinstance(receipt.get("changed_path_hashes"), dict)
        or any(
            not isinstance(raw_digest, str)
            or len(raw_digest.removeprefix("sha256:")) != 64
            or any(
                char not in "0123456789abcdef"
                for char in raw_digest.removeprefix("sha256:")
            )
            for raw_digest in digest_fields
        )
    ):
        return None, "accepted_outcome_receipt_malformed"
    terminal = card.get("terminal_review") or {}
    sealed = terminal.get("evidence") or {}
    paths = sorted(str(path) for path in (sealed.get("changed_paths") or []))
    hashes = sealed.get("changed_path_hashes")
    manifest = sealed.get("attempt_artifact_manifest")
    workspace = sealed.get("workspace") or {}
    expected = {
        "schema_id": ACCEPTED_OUTCOME_RECEIPT_SCHEMA,
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": int(card.get("claim_epoch") or 0),
        "base_oid": str(workspace.get("base_oid") or ""),
        "promoted_paths": paths,
        "changed_path_hashes": hashes,
        "attempt_artifact_manifest_id": _canonical_json_hash(manifest),
    }
    if not isinstance(hashes, dict) or not isinstance(manifest, dict):
        return None, "accepted_outcome_receipt_sealed_evidence_missing"
    if any(receipt.get(key) != value for key, value in expected.items()):
        return None, "accepted_outcome_receipt_identity_mismatch"
    canonical_hashes: dict[str, str | None] = {}
    for relative in paths:
        path = repo / relative
        canonical_hashes[relative] = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() and not path.is_symlink() else None
        )
    if canonical_hashes != hashes:
        return None, "accepted_outcome_receipt_canonical_hash_mismatch"
    revision = "sha256:" + _canonical_json_hash({
        "base_oid": expected["base_oid"], "changed_path_hashes": canonical_hashes,
    })
    if receipt.get("repository_revision") != revision:
        return None, "accepted_outcome_receipt_revision_mismatch"
    unsigned = dict(receipt)
    receipt_id = str(unsigned.pop("receipt_id", ""))
    if receipt_id != "sha256:" + _canonical_json_hash(unsigned):
        return None, "accepted_outcome_receipt_id_mismatch"
    return dict(receipt), ""


def accept_review(
    repo: Path,
    task_id: str,
    *,
    runner: str,
    topic: str,
    request_id: str,
    evidence: dict[str, Any] | None = None,
    accepted_outcome_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Coordinator-only promotion finalize: ``review`` -> ``finished``.

    This is the sole authority that may move a review-first request's task
    out of ``review`` after ``ProcessManager.accept_review`` has already
    re-run scope/required-output/validation gates and promoted the exact,
    hash-verified changed paths into this bound ``repo``. Every identity
    check here re-derives from the canonical row read under the same write
    transaction (never a stale caller-supplied copy), so a concurrent
    accept-review call for a different request can never race this one into
    finishing the task twice. Idempotent: retrying the exact same
    already-finished request returns ``ok: True`` with ``already_accepted``;
    a different request retried against an already-finished task fails
    closed instead of silently re-finishing it.
    """
    command = [
        "accept-review", task_id, "--runner", runner, "--topic", topic,
        "--request-id", request_id,
    ]
    now = datetime.now(timezone.utc).isoformat()
    try:
        _readiness, db_path = task_store._require_ready(repo)
        conn = task_store._connect(db_path)
    except task_store.TaskStoreError as exc:
        return {"ok": False, "returncode": 1, "command": command, "stdout": "", "stderr": str(exc)}
    try:
        row = conn.execute(
            "SELECT runner, topic, status, worker_status, claimed_by, card_json "
            "FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return {
                "ok": False, "returncode": 1, "command": command, "stdout": "",
                "stderr": f"task_not_found:{task_id}",
            }
        if row["runner"] != runner or _effective_topic(row) != topic:
            conn.rollback()
            return {
                "ok": False, "returncode": 1, "command": command, "stdout": "",
                "stderr": f"identity_mismatch:task_id={task_id}",
            }
        if row["claimed_by"] != runner:
            conn.rollback()
            return {
                "ok": False, "returncode": 1, "command": command, "stdout": "",
                "stderr": f"claim_mismatch:claimed_by={row['claimed_by']}",
            }
        try:
            card = json.loads(row["card_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            card = {}
        if not isinstance(card, dict):
            card = {}
        status = str(row["status"] or "")
        worker_status = str(row["worker_status"] or "")
        if status == "finished" or worker_status == "done":
            already = str(card.get("accepted_request_id") or "") == request_id
            persisted_evidence = card.get("accept_evidence") or {}
            persisted_receipt = (
                persisted_evidence.get("accepted_outcome_receipt")
                if isinstance(persisted_evidence, dict)
                else None
            )
            conn.rollback()
            return {
                "ok": already,
                "returncode": 0 if already else 1,
                "command": command,
                "stdout": json.dumps(
                    {
                        "task_id": task_id,
                        "already_accepted": already,
                        "accepted_outcome_receipt": (
                            persisted_receipt if already else None
                        ),
                    },
                    ensure_ascii=False,
                ),
                "stderr": "" if already else "task_already_finished_by_other_request",
            }
        terminal_review = card.get("terminal_review") or {}
        if str(terminal_review.get("substatus") or "") != "review_ready":
            conn.rollback()
            return {
                "ok": False, "returncode": 1, "command": command, "stdout": "",
                "stderr": (
                    "terminal_substatus_not_review_ready:"
                    + str(terminal_review.get("substatus") or "")
                ),
            }
        request_identity = (terminal_review.get("evidence") or {}).get("request_identity") or {}
        if str(request_identity.get("request_id") or "") != request_id:
            conn.rollback()
            return {
                "ok": False, "returncode": 1, "command": command, "stdout": "",
                "stderr": "request_identity_mismatch",
            }
        if status != "review" or worker_status != "review":
            conn.rollback()
            return {
                "ok": False, "returncode": 1, "command": command, "stdout": "",
                "stderr": f"task_not_reviewable:status={status}:worker_status={worker_status}",
            }
        receipt, receipt_error = _validate_accepted_outcome_receipt(
            repo, card, task_id, request_id, accepted_outcome_receipt
        )
        if receipt is None:
            conn.rollback()
            return {
                "ok": False,
                "returncode": 1,
                "command": command,
                "stdout": "",
                "stderr": receipt_error,
            }
        if not isinstance(evidence, dict) or "accepted_outcome_receipt" in evidence:
            conn.rollback()
            return {
                "ok": False, "returncode": 1, "command": command, "stdout": "",
                "stderr": "accept_evidence_malformed",
            }
        acceptance_evidence = dict(evidence)
        acceptance_evidence["accepted_outcome_receipt"] = receipt
        card["accepted_request_id"] = request_id
        card["accepted_by"] = runner
        card["accepted_at"] = now
        card["accept_evidence"] = acceptance_evidence
        update = conn.execute(
            "UPDATE tasks SET status='finished', worker_status='done', "
            "completed_at=COALESCE(NULLIF(completed_at, ''), ?), updated_at=?, card_json=? "
            "WHERE task_id=? AND runner IS ? AND topic IS ? AND status IS ? "
            "AND worker_status IS ? AND claimed_by IS ? AND card_json IS ?",
            (
                now,
                now,
                json.dumps(card, ensure_ascii=False, sort_keys=True),
                task_id,
                row["runner"],
                row["topic"],
                row["status"],
                row["worker_status"],
                row["claimed_by"],
                row["card_json"],
            ),
        )
        if update.rowcount != 1:
            conn.rollback()
            return {
                "ok": False,
                "returncode": 1,
                "command": command,
                "stdout": "",
                "stderr": "accept_review_preimage_changed",
            }
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (
                task_id, "accept_review", runner,
                json.dumps(
                    {
                        "request_id": request_id,
                        **acceptance_evidence,
                    },
                    ensure_ascii=False, default=str, sort_keys=True,
                ),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    card2 = task_store.get_task(repo, task_id)
    stdout = json.dumps(card2, ensure_ascii=False, default=str) if card2 else ""
    return {"ok": True, "returncode": 0, "command": command, "stdout": stdout, "stderr": ""}


def archive_task(
    repo: Path,
    task_id: str,
    *,
    actor: str,
    reason: str = "",
    supersede: bool = False,
) -> dict[str, Any]:
    operation = "superseded" if supersede else "archived"
    command = [operation, task_id, "--actor", actor]
    try:
        ok, state = task_store.archive_task(
            repo,
            task_id,
            actor=actor,
            reason=reason,
            allow_processing=supersede,
            operation=operation,
        )
    except task_store.TaskStoreError as exc:
        return {"ok": False, "returncode": 1, "command": command, "stdout": "", "stderr": str(exc)}
    return {
        "ok": ok,
        "returncode": 0 if ok else 1,
        "command": command,
        "stdout": json.dumps({"task_id": task_id, "status": state}, ensure_ascii=False),
        "stderr": "" if ok else state,
    }


# Batch archive surface. Measured: 210 single-id archive calls in 27 manager
# sessions (62% of all assistant turns were pure tool relays), each re-sending
# the whole manager context to the provider for a 195-byte envelope. The loop
# runs here under the same per-item write gate the single form uses; each
# item keeps its own audit event and its own receipt, so one refusal never
# hides behind an all-or-nothing reply.
MAX_ARCHIVE_BATCH = 200
ARCHIVE_SELECTOR_FIELDS = ("status", "topic", "older_than_hours", "task_id_prefix")
_ARCHIVE_SELECTION_SCAN_LIMIT = 5000


def archive_receipt(task_id: str, result: Any) -> dict[str, Any]:
    """Project one archive envelope (``archive_task``/``core.archive_task``)
    to a per-id receipt ``{task_id, ok, status_after, error?}``."""

    if not isinstance(result, dict):
        return {"task_id": task_id, "ok": False, "status_after": None, "error": "archive_result_invalid"}
    ok = bool(result.get("ok"))
    status_after = None
    stdout = result.get("stdout")
    if isinstance(stdout, str) and stdout:
        try:
            payload = json.loads(stdout)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            status_after = payload.get("status")
    receipt: dict[str, Any] = {"task_id": task_id, "ok": ok, "status_after": status_after}
    if not ok:
        receipt["error"] = str(
            result.get("stderr") or result.get("error") or "archive_failed"
        )[:300]
    return receipt


def _parse_iso_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def select_archivable_tasks(
    repo: Path,
    *,
    status: str | None = None,
    topic: str | None = None,
    older_than_hours: float | None = None,
    task_id_prefix: str | None = None,
    limit: int = MAX_ARCHIVE_BATCH,
) -> dict[str, Any]:
    """Read-only selection of cards a batch archive would act on.

    Filters are conjunctive. Already archived/superseded rows are skipped;
    ``processing`` rows are listed under ``refused`` (the archive backend
    refuses them, ``allow_processing=False``) so a preview names them instead
    of silently dropping them. Never writes.
    """

    rows = task_store.list_tasks(repo, status=status or None, limit=_ARCHIVE_SELECTION_SCAN_LIMIT)
    now = datetime.now(timezone.utc)
    bounded_limit = max(1, min(int(limit), MAX_ARCHIVE_BATCH))
    selected: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for row in rows:
        task_id = str(row.get("task_id") or "")
        canonical = str(row.get("status") or "")
        row_topic = str(row.get("topic") or "")
        if topic and row_topic != topic:
            continue
        if task_id_prefix and not task_id.startswith(task_id_prefix):
            continue
        if older_than_hours is not None:
            updated = _parse_iso_timestamp(row.get("updated_at"))
            if updated is None or (now - updated).total_seconds() < float(older_than_hours) * 3600.0:
                continue
        entry = {
            "task_id": task_id,
            "status": canonical,
            "topic": row_topic,
            "updated_at": row.get("updated_at"),
        }
        if canonical in ("archived", "superseded"):
            continue
        if canonical == "processing":
            refused.append({**entry, "reason": "processing_refused"})
            continue
        selected.append(entry)
    return {
        "selected": selected[:bounded_limit],
        "selected_count": min(len(selected), bounded_limit),
        "truncated": len(selected) > bounded_limit,
        "refused": refused,
        "scanned": len(rows),
        "limit": bounded_limit,
    }


_REVIEWER_CHILD_TERMINAL_STATUSES = ("finished", "done", "archived", "superseded")


def _claim_epoch_for_request(conn: Any, task_id: str, request_id: str) -> int | None:
    """Resolve the claim epoch of one exact launch episode of ``task_id``.

    ``claim_start_exact`` writes ``{"request_id": ..., "claim_epoch": ...}`` on
    every ``claim_start``/``launch_attach`` event, and ``claim_epoch`` is only
    ever incremented for a new episode (never reset, never reused), so those
    events are a durable total order over one task's launch episodes.  Returns
    ``None`` when the episode was never recorded -- never a guess.
    """
    if not task_id or not request_id:
        return None
    try:
        rows = conn.execute(
            "SELECT payload_json FROM task_events "
            "WHERE task_id=? AND event IN ('claim_start','launch_attach') "
            "ORDER BY event_id DESC",
            (task_id,),
        ).fetchall()
    except Exception:
        return None
    for row in rows:
        try:
            payload = json.loads(row[0] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("request_id") or "") != request_id:
            continue
        epoch = payload.get("claim_epoch")
        if type(epoch) is int:
            return epoch
    return None


def _parent_claim_epoch(conn: Any, task_id: str, request_id: str) -> int | None:
    """Claim epoch of ``request_id`` on ``task_id``, or ``None`` if unknowable.

    The event log is authority.  The card is consulted only when it names this
    exact request as the live episode: a card epoch paired with a *different*
    ``launch_request_id`` describes a later relaunch and proves nothing about
    the request being disposed.
    """
    epoch = _claim_epoch_for_request(conn, task_id, request_id)
    if epoch is not None:
        return epoch
    try:
        row = conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    try:
        card = json.loads(row[0] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(card, dict):
        return None
    if str(card.get("launch_request_id") or "") != request_id:
        return None
    epoch = card.get("claim_epoch")
    return epoch if type(epoch) is int else None


def _reviewer_child_mismatch_reason(
    target_task_id: str,
    target_request_id: str,
    target_claim_epoch: Any,
    *,
    parent_task_id: str,
    parent_request_id: str,
    parent_claim_epoch: int | None,
) -> str | None:
    """Name why a mismatched reviewer child may not be disposed, or ``None``.

    ``None`` means exactly one thing: the child is bound to *this* parent task
    at a strictly earlier launch episode.  Its review packet was sealed against
    candidate bytes that this request no longer carries, so its verdict can
    never be verified against this or any later request -- superseding it is
    the only honest disposition, and re-binding it to the current request would
    credit an inspection that never happened.

    Every other mismatch is refused by name:

    * ``foreign_target_task`` -- another parent's child; never ours to touch.
    * ``child_claim_epoch_newer_than_parent_request`` -- a *later* episode's
      reviewer.  ``reject_review`` disposes the rejected request while a rework
      relaunch may already be in flight, and that relaunch's reviewers must
      survive (see ``_finalize_bound_reviewers`` in ``core.py``).
    * ``child_claim_epoch_equal_request_conflict`` -- one episode cannot carry
      two request ids; the binding is not trustworthy enough to act on.
    * ``*_unknown``/``*_unresolved`` -- no order, so no authority to dispose.
    """
    if target_task_id != parent_task_id:
        return "foreign_target_task"
    if not target_request_id:
        return "child_request_id_missing"
    if target_request_id == parent_request_id:
        return None
    if type(target_claim_epoch) is not int:
        return "child_claim_epoch_unknown"
    if parent_claim_epoch is None:
        return "parent_claim_epoch_unresolved"
    if target_claim_epoch > parent_claim_epoch:
        return "child_claim_epoch_newer_than_parent_request"
    if target_claim_epoch == parent_claim_epoch:
        return "child_claim_epoch_equal_request_conflict"
    return None


def disposition_reviewer_children(
    repo: Path,
    parent_task_id: str,
    *,
    verified_reviewer_task_ids: list[str],
    parent_request_id: str,
    disposition: str = "accepted",
) -> dict[str, Any]:
    """Atomically disposition exact reviewer child cards for one parent.

    When a parent candidate is accepted or rejected, every bound quality-review
    child card is disposed without deleting its immutable receipt/audit history:
    * verified (successful) reviewer tasks are finalized, preserving their
      receipts and leaving an actionable Review event.
    * redundant/failed sibling reviewer attempts are superseded so they are
      not counted in future dashboard KPIs yet remain in history.

    Idempotent: a repeat call with the same verified set updates nothing already
    finalized or superseded.  Fail-closed on identity: a child bound to another
    task, or to an episode of this task whose order cannot be established, is
    left untouched and reported by name in ``refused`` *and* as a
    ``reviewer_child_disposition_refused`` event.  A child bound to this task at
    a strictly earlier claim epoch is superseded as ``stale_claim_episode``.
    """
    verified_set = frozenset(str(tid) for tid in (verified_reviewer_task_ids or []))
    command = [
        "disposition-reviewer-children", parent_task_id,
        "--request-id", parent_request_id, "--disposition", disposition,
    ]
    now = datetime.now(timezone.utc).isoformat()
    try:
        _readiness, db_path = task_store._require_ready(repo)
        conn = task_store._connect(db_path)
    except task_store.TaskStoreError as exc:
        return {"ok": False, "returncode": 1, "command": command, "stdout": "", "stderr": str(exc)}
    try:
        rows = conn.execute(
            "SELECT task_id, runner, topic, status, worker_status, card_json "
            "FROM tasks WHERE topic='quality_review' AND (status NOT IN ('archived'))"
        ).fetchall()
    except Exception:
        conn.rollback()
        conn.close()
        return {"ok": False, "returncode": 1, "command": command, "stdout": "",
                "stderr": "reviewer_children_query_failed"}
    # One durable episode order for this parent, resolved once.  A reviewer
    # child bound to a strictly earlier episode is provably stale; nothing else
    # is, so nothing else may be disposed on identity grounds.
    parent_claim_epoch = _parent_claim_epoch(conn, parent_task_id, parent_request_id)
    finalized: list[str] = []
    superseded: list[str] = []
    stale_superseded: list[str] = []
    skipped: list[str] = []
    refused: list[str] = []
    errors: list[str] = []
    for row in rows:
        child_task_id = str(row["task_id"] or "")
        try:
            card = json.loads(row["card_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            card = {}
        if not isinstance(card, dict):
            card = {}
        root_binding = card.get("quality_review") or {}
        terminal_review = card.get("terminal_review") or {}
        terminal_evidence = (
            terminal_review.get("evidence") or {}
            if isinstance(terminal_review, dict)
            else {}
        )
        terminal_binding = (
            terminal_evidence.get("quality_review") or {}
            if isinstance(terminal_evidence, dict)
            else {}
        )
        if not isinstance(root_binding, dict):
            root_binding = {}
        if not isinstance(terminal_binding, dict):
            terminal_binding = {}
        if root_binding and terminal_binding and root_binding != terminal_binding:
            errors.append(f"{child_task_id}:reviewer_binding_conflict")
            continue
        # Current terminal-review cards persist the authenticated target binding
        # with the terminal evidence.  Keep the root lookup for older cards, but
        # never guess when both durable representations disagree.
        binding = terminal_binding or root_binding
        target = str(binding.get("target_task_id") or "")
        target_request = str(binding.get("target_request_id") or "")
        target_epoch = binding.get("target_claim_epoch")
        child_status = str(row["status"] or "")
        child_runner = str(row["runner"] or "")
        stale_episode = False
        if target != parent_task_id or target_request != parent_request_id:
            # NF: this branch used to be a bare ``continue``.  A reviewer bound
            # to a *superseded launch episode* of this same parent was skipped
            # with no row change, no list entry and no event: the transition
            # that never happened was also never explained, and the children of
            # every relaunch were stranded in review until a human cleared them
            # by hand.  Classify the mismatch instead, and act only where the
            # durable episode order proves the child is stale.
            reason = _reviewer_child_mismatch_reason(
                target,
                target_request,
                target_epoch,
                parent_task_id=parent_task_id,
                parent_request_id=parent_request_id,
                parent_claim_epoch=parent_claim_epoch,
            )
            if reason is not None:
                if child_status in _REVIEWER_CHILD_TERMINAL_STATUSES:
                    # Already terminal: its own disposition event explains it,
                    # and re-emitting here would grow the log without adding a
                    # fact.
                    skipped.append(child_task_id)
                    continue
                # Refusing to dispose is itself a decision about a live card.
                # Record it by name so the stranded population is measurable.
                conn.execute(
                    "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (
                        child_task_id, "reviewer_child_disposition_refused", child_runner,
                        json.dumps({
                            "parent_task_id": parent_task_id,
                            "parent_request_id": parent_request_id,
                            "parent_claim_epoch": parent_claim_epoch,
                            "child_target_task_id": target,
                            "child_target_request_id": target_request,
                            "child_target_claim_epoch": target_epoch,
                            "child_status": child_status,
                            "disposition": disposition,
                            "reason": reason,
                        }, ensure_ascii=False, default=str, sort_keys=True),
                        now,
                    ),
                )
                refused.append(f"{child_task_id}:{reason}")
                continue
            stale_episode = True
        if child_status in _REVIEWER_CHILD_TERMINAL_STATUSES:
            skipped.append(child_task_id)
            continue
        # A stale-episode child is never finalized even if it appears in the
        # verified set: its verdict was authenticated against candidate bytes
        # this request no longer carries.
        if child_task_id in verified_set and not stale_episode:
            card["reviewer_disposition"] = {
                "parent_task_id": parent_task_id,
                "parent_request_id": parent_request_id,
                "disposition": disposition,
                "disposed_at": now,
            }
            conn.execute(
                "UPDATE tasks SET status='finished', worker_status='done', "
                "completed_at=COALESCE(NULLIF(completed_at, ''), ?), updated_at=?, card_json=? "
                "WHERE task_id=?",
                (now, now, json.dumps(card, ensure_ascii=False, sort_keys=True), child_task_id),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    child_task_id, "reviewer_child_finalized", child_runner,
                    json.dumps({
                        "parent_task_id": parent_task_id,
                        "parent_request_id": parent_request_id,
                        "disposition": disposition,
                    }, ensure_ascii=False, default=str, sort_keys=True),
                    now,
                ),
            )
            finalized.append(child_task_id)
        else:
            supersede_reason = (
                "stale_claim_episode" if stale_episode else "redundant_sibling"
            )
            card["reviewer_disposition"] = {
                "parent_task_id": parent_task_id,
                "parent_request_id": parent_request_id,
                "disposition": "superseded",
                "reason": supersede_reason,
                "disposed_at": now,
            }
            if stale_episode:
                card["reviewer_disposition"].update(
                    parent_claim_epoch=parent_claim_epoch,
                    child_target_request_id=target_request,
                    child_target_claim_epoch=target_epoch,
                )
            # Durable superseded status must remain visible through task_store.canonical_status.
            conn.execute(
                "UPDATE tasks SET status='superseded', worker_status='superseded', "
                "updated_at=?, card_json=? WHERE task_id=?",
                (now, json.dumps(card, ensure_ascii=False, sort_keys=True), child_task_id),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    child_task_id, "reviewer_child_superseded", child_runner,
                    json.dumps({
                        "parent_task_id": parent_task_id,
                        "parent_request_id": parent_request_id,
                        "parent_claim_epoch": parent_claim_epoch,
                        "child_target_request_id": target_request,
                        "child_target_claim_epoch": target_epoch,
                        "disposition": "superseded",
                        "reason": supersede_reason,
                    }, ensure_ascii=False, default=str, sort_keys=True),
                    now,
                ),
            )
            superseded.append(child_task_id)
            if stale_episode:
                stale_superseded.append(child_task_id)
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "returncode": 0,
        "command": command,
        "stdout": json.dumps({
            "parent_task_id": parent_task_id,
            "parent_request_id": parent_request_id,
            "parent_claim_epoch": parent_claim_epoch,
            "finalized": finalized,
            "superseded": superseded,
            "stale_superseded": stale_superseded,
            "skipped": skipped,
            "refused": refused,
            "errors": errors,
        }, ensure_ascii=False),
        "stderr": "",
    }


__all__ = [
    "show_task", "claim_start_exact", "mark_terminal_review", "mark_launch_failed", "accept_review",
    "archive_task", "disposition_reviewer_children",
]
