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
            "SELECT runner, topic, card_json FROM tasks WHERE task_id=?", (task_id,)
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
        try:
            claim_epoch = int(stored_card.get("claim_epoch") or 0) + 1
        except (TypeError, ValueError):
            claim_epoch = 1
        prior_episode = task_store.begin_claim_episode(stored_card)
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
            (json.dumps(stored_card, ensure_ascii=False, sort_keys=True), runner, now, now, now, task_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return {
                "ok": False, "returncode": 1, "command": command, "stdout": "",
                "stderr": f"claim_conflict:task_id={task_id}",
            }
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) VALUES (?,?,?,?,?)",
            (
                task_id, "claim_start", runner,
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


__all__ = [
    "show_task", "claim_start_exact", "mark_terminal_review", "accept_review",
    "archive_task",
]
