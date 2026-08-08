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

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import callback_store
from . import core
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
                "WHERE task_id=? AND worker_status='unclaimed' AND status='pending'",
                (
                    json.dumps(stored_card, ensure_ascii=False, sort_keys=True),
                    runner,
                    now,
                    now,
                    now,
                    task_id,
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
        conn.close()
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
) -> dict[str, Any]:
    """Record a pre-claim launch blocker without fabricating a claim.

    The card remains pending/unclaimed and can be retried explicitly after the
    environment is repaired, but Plan-DAG/auto-pickup can now see the exact
    operational blocker instead of looping or reporting an empty blocker map.
    """
    command = ["launch-blocked", task_id, "--runner", runner]
    blocked = core._canonical_write_gate(
        "claim-start", runner=runner, topic=topic, task_id=task_id
    )
    if blocked is not None:
        # The gate checks the same narrow claim-start authority, but callers
        # are performing a distinct launch-blocker write. Preserve the exact
        # denial while reporting the operation they actually requested.
        return {**blocked, "command": command}
    now = datetime.now(timezone.utc).isoformat()
    try:
        _readiness, db_path = task_store._require_ready(repo)
        conn = task_store._connect(db_path)
    except task_store.TaskStoreError as exc:
        return {"ok": False, "returncode": 1, "command": command, "stdout": "", "stderr": str(exc)}
    try:
        row = conn.execute(
            "SELECT runner, topic, status, worker_status, card_json FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return {"ok": False, "returncode": 1, "command": command, "stdout": "", "stderr": f"task_not_found:{task_id}"}
        if row["runner"] != runner or _effective_topic(row) != topic:
            conn.rollback()
            return {"ok": False, "returncode": 1, "command": command, "stdout": "", "stderr": "identity_mismatch"}
        if str(row["status"] or "").lower() != "pending" or str(row["worker_status"] or "").lower() != "unclaimed":
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
) -> dict[str, Any]:
    command = ["terminal-review", task_id, "--runner", runner, "--substatus", substatus]
    try:
        ok, state = task_store.mark_terminal_review(
            repo,
            task_id,
            runner=runner,
            substatus=substatus,
            evidence=evidence or {},
        )
    except task_store.TaskStoreError as exc:
        return {"ok": False, "returncode": 1, "command": command, "stdout": "", "stderr": str(exc)}
    callback_enqueued = False
    if ok:
        card = task_store.get_task(repo, task_id) or {}
        # Callback delivery is a lifecycle invariant, not a per-card option:
        # every task that entered review gets an outbox row.  The terminal
        # substatus only determines the compact payload state.
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
                transition = callback_store.resolve_callback_transition(substatus)
                callback_enqueued = callback_store.enqueue_callback(
                    conn,
                    task_id,
                    origin_thread_id,
                    transition,
                    provider=provider,
                    episode_id=str(card.get("claim_epoch") or 0),
                    request_id=str((evidence or {}).get("request_id") or ""),
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


def accept_review(
    repo: Path,
    task_id: str,
    *,
    runner: str,
    topic: str,
    request_id: str,
    evidence: dict[str, Any] | None = None,
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
            conn.rollback()
            return {
                "ok": already,
                "returncode": 0 if already else 1,
                "command": command,
                "stdout": json.dumps(
                    {"task_id": task_id, "already_accepted": already}, ensure_ascii=False
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
        card["accepted_request_id"] = request_id
        card["accepted_by"] = runner
        card["accepted_at"] = now
        card["accept_evidence"] = dict(evidence or {})
        conn.execute(
            "UPDATE tasks SET status='finished', worker_status='done', "
            "completed_at=COALESCE(NULLIF(completed_at, ''), ?), updated_at=?, card_json=? "
            "WHERE task_id=?",
            (now, now, json.dumps(card, ensure_ascii=False, sort_keys=True), task_id),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (
                task_id, "accept_review", runner,
                json.dumps(
                    {"request_id": request_id, **(evidence or {})},
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
    finalized or superseded.  Fail-closed: any child whose target binding does
    not match this parent is left untouched and reported.
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
    finalized: list[str] = []
    superseded: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for row in rows:
        child_task_id = str(row["task_id"] or "")
        try:
            card = json.loads(row["card_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            card = {}
        if not isinstance(card, dict):
            card = {}
        binding = card.get("quality_review") or {}
        target = str(binding.get("target_task_id") or "")
        target_request = str(binding.get("target_request_id") or "")
        if target != parent_task_id or target_request != parent_request_id:
            continue
        child_status = str(row["status"] or "")
        if child_status in ("finished", "done", "archived", "superseded"):
            skipped.append(child_task_id)
            continue
        child_runner = str(row["runner"] or "")
        if child_task_id in verified_set:
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
            card["reviewer_disposition"] = {
                "parent_task_id": parent_task_id,
                "parent_request_id": parent_request_id,
                "disposition": "superseded",
                "disposed_at": now,
            }
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
                        "disposition": "superseded",
                    }, ensure_ascii=False, default=str, sort_keys=True),
                    now,
                ),
            )
            superseded.append(child_task_id)
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "returncode": 0,
        "command": command,
        "stdout": json.dumps({
            "parent_task_id": parent_task_id,
            "finalized": finalized,
            "superseded": superseded,
            "skipped": skipped,
            "errors": errors,
        }, ensure_ascii=False),
        "stderr": "",
    }


__all__ = [
    "show_task", "claim_start_exact", "mark_terminal_review", "mark_launch_failed", "accept_review",
    "archive_task", "disposition_reviewer_children",
]
