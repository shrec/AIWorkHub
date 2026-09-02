"""Durable manager-gated Learning Commit ledger and resumable projections.

The task database is the canonical receipt/outbox.  Session Manager, Context
Graph, AI Memory and KB remain separate authorities and are updated through
their existing idempotent write paths.  A crash can therefore leave a truthful
``partial`` record; retrying the same idempotency key resumes only projections
that have not already completed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, cast

from . import (
    context_graph,
    context_writes,
    core,
    evidence_levels,
    feature_settings,
    sqlite_readonly,
    task_store,
)
from .learning_commit import LearningCommit, Outcome, learning_commit_from_dict, validate_repo_match


SCHEMA_ID = "aiworkhub.learning_commit.v1"
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,191}$")
_MAX_PROVENANCE_BYTES = 2048
_MAX_PAYLOAD_BYTES = 64 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_commits(
    commit_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    task_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    repo_area TEXT NOT NULL,
    outcome TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    projections_json TEXT NOT NULL,
    state TEXT NOT NULL,
    manager_id TEXT NOT NULL,
    manager_provider TEXT NOT NULL,
    provenance TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_learning_commits_state
ON learning_commits(state, updated_at);
"""


class LearningCommitStoreError(RuntimeError):
    """Invalid authority, evidence, identity or durable projection state."""


def _bounded(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise LearningCommitStoreError(f"invalid_{field}")
    text = value.strip()
    if not text or "\x00" in text or len(text.encode("utf-8")) > maximum:
        raise LearningCommitStoreError(f"invalid_{field}")
    return text


def _json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise LearningCommitStoreError("learning_commit_not_json_serializable") from exc
    if len(encoded.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise LearningCommitStoreError("learning_commit_payload_too_large")
    return encoded


def _edge_dicts(commit: LearningCommit) -> list[dict[str, str]]:
    return [
        {"source": edge.source, "target": edge.target, "relation": edge.relation}
        for edge in commit.edge_candidates
    ]


def _commit_payload(commit: LearningCommit) -> dict[str, Any]:
    return {
        "schema_id": SCHEMA_ID,
        "task_id": commit.task_id,
        "repository_id": commit.repository_id,
        "repo_area": commit.repo_area,
        "outcome": commit.outcome.value,
        "failure_category": commit.failure_category.value if commit.failure_category else None,
        "evidence_ids": list(commit.evidence_ids),
        "root_cause_candidate": commit.root_cause_candidate,
        "invariant_candidate": commit.invariant_candidate,
        "lesson_candidate": commit.lesson_candidate,
        "edge_candidates": _edge_dicts(commit),
        "promotion_eligible_ai_memory": commit.promotion_eligible_ai_memory,
        "promotion_eligible_context_graph": commit.promotion_eligible_context_graph,
        "promotion_eligible_kb": commit.promotion_eligible_kb,
    }


def _canonical_acceptance_reference(card: dict[str, Any], request_id: str) -> str:
    if task_store.canonical_status(card) != "finished":
        raise LearningCommitStoreError("learning_commit_task_not_manager_accepted")
    if str(card.get("accepted_request_id") or "") != request_id:
        raise LearningCommitStoreError("learning_commit_request_identity_mismatch")
    accept_evidence = card.get("accept_evidence")
    if not isinstance(accept_evidence, dict):
        raise LearningCommitStoreError("learning_commit_acceptance_evidence_missing")
    try:
        record = evidence_levels.validate_evidence_record(
            accept_evidence.get("acceptance_evidence_record")
        )
    except (evidence_levels.EvidenceValidationError, TypeError) as exc:
        raise LearningCommitStoreError("learning_commit_acceptance_evidence_invalid") from exc
    if record.evidence_level != evidence_levels.EvidenceLevel.FIXED_AND_VERIFIED:
        raise LearningCommitStoreError("learning_commit_acceptance_not_fixed_and_verified")
    if not record.reference:
        raise LearningCommitStoreError("learning_commit_acceptance_reference_missing")
    return cast(str, record.reference)


def _request_matches_candidate(card: dict[str, Any], request_id: str) -> bool:
    if str(card.get("accepted_request_id") or "") == request_id:
        return True
    identity = (
        ((card.get("terminal_review") or {}).get("evidence") or {}).get("request_identity")
        or {}
    )
    if str(identity.get("request_id") or "") == request_id:
        return True
    # A rejection that sends the card back for rework is still an adjudicated
    # outcome, and it is the COMMON one -- but it never stamps terminal_review,
    # so until now only a rejection that TERMINATED a card could be learned
    # from. Measured 2026-09-02 on AIWORKHUB_01082: after reject_review the
    # card carried the adjudicated request id twice, in review_feedback and in
    # rework_predecessor, and this predicate looked at neither, so the commit
    # failed learning_commit_request_identity_mismatch.
    #
    # Both are written by reject_review itself, not supplied by a model:
    # rework_predecessor pins the predecessor's changed-path hashes and
    # review_feedback carries the reason's sha256. Accepting them binds the
    # lesson to the exact request that was judged, which is what this predicate
    # exists to guarantee.
    for section in ("rework_predecessor", "review_feedback"):
        block = card.get(section)
        if not isinstance(block, dict):
            continue
        for key in ("request_id", "predecessor_request_id"):
            if str(block.get(key) or "") == request_id:
                return True
    return False


def _open(repo: Path) -> sqlite3.Connection:
    _readiness, db_path = task_store._require_ready(repo)
    con = cast(sqlite3.Connection, task_store._connect(db_path))
    con.executescript(_SCHEMA)
    return con


def _projection_plan(commit: LearningCommit, repo: Path) -> dict[str, dict[str, Any]]:
    def state(requested: bool, feature: str) -> dict[str, Any]:
        if not requested:
            return {"state": "not_requested"}
        if not feature_settings.enabled(repo, feature):
            return {"state": "skipped_disabled", "feature": feature}
        return {"state": "pending"}

    return {
        "session": state(True, "session_manager"),
        "context_graph": state(
            commit.promotion_eligible_context_graph, "context_graph"
        ),
        "ai_memory": state(commit.promotion_eligible_ai_memory, "ai_memory"),
        "kb": state(commit.promotion_eligible_kb, "knowledge_base"),
    }


def _load_or_create(
    repo: Path,
    *,
    commit: LearningCommit,
    request_id: str,
    idempotency_key: str,
    actor: dict[str, str],
    provenance: str,
) -> tuple[str, dict[str, Any], bool]:
    payload = _commit_payload(commit)
    payload_json = _json(payload)
    payload_sha = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    repository_id = str(commit.repository_id or "")
    commit_id = hashlib.sha256(
        f"{repository_id}\0{commit.task_id}\0{request_id}".encode("utf-8")
    ).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    con = _open(repo)
    try:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT * FROM learning_commits WHERE idempotency_key=? OR "
            "(task_id=? AND request_id=?)",
            (idempotency_key, commit.task_id, request_id),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["idempotency_key"]) != idempotency_key
                or str(existing["payload_sha256"]) != payload_sha
            ):
                con.rollback()
                raise LearningCommitStoreError("learning_commit_identity_conflict")
            projections = json.loads(str(existing["projections_json"]))
            con.rollback()
            return str(existing["commit_id"]), projections, True
        projections = _projection_plan(commit, repo)
        con.execute(
            "INSERT INTO learning_commits(commit_id,idempotency_key,task_id,request_id,"
            "repository_id,repo_area,outcome,payload_json,payload_sha256,projections_json,"
            "state,manager_id,manager_provider,provenance,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                commit_id,
                idempotency_key,
                commit.task_id,
                request_id,
                repository_id,
                commit.repo_area,
                commit.outcome.value,
                payload_json,
                payload_sha,
                _json(projections),
                "pending",
                actor["actor_id"],
                actor["provider"],
                provenance,
                now,
                now,
            ),
        )
        con.commit()
        return commit_id, projections, False
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _save_projection(
    repo: Path, commit_id: str, projections: dict[str, Any]
) -> str:
    states = {str(row.get("state") or "") for row in projections.values()}
    if "failed" in states or "pending" in states:
        state = "partial"
    else:
        state = "completed"
    con = _open(repo)
    try:
        con.execute(
            "UPDATE learning_commits SET projections_json=?,state=?,updated_at=? "
            "WHERE commit_id=?",
            (
                _json(projections),
                state,
                datetime.now(timezone.utc).isoformat(),
                commit_id,
            ),
        )
        con.commit()
    finally:
        con.close()
    return state


def _summary(commit_id: str, request_id: str, commit: LearningCommit) -> dict[str, Any]:
    evidence_json = _json(list(commit.evidence_ids))
    return {
        "schema_id": SCHEMA_ID,
        "commit_id": commit_id,
        "task_id": commit.task_id,
        "request_id": request_id,
        "repository_id": commit.repository_id,
        "repo_area": commit.repo_area,
        "outcome": commit.outcome.value,
        "failure_category": commit.failure_category.value if commit.failure_category else None,
        "root_cause": commit.root_cause_candidate,
        "invariant": commit.invariant_candidate,
        "lesson": commit.lesson_candidate,
        "edges": _edge_dicts(commit),
        "evidence_count": len(commit.evidence_ids),
        "evidence_sha256": hashlib.sha256(evidence_json.encode("utf-8")).hexdigest(),
        "primary_evidence": commit.evidence_ids[0] if commit.evidence_ids else "",
    }


# How far back a coverage measurement looks. Older cards predate the learning
# path being wired at all, so counting them would report a permanent failure
# rather than current practice.
COVERAGE_WINDOW_DAYS = 14


def coverage(root: str | Path, *, window_days: int = COVERAGE_WINDOW_DAYS) -> dict[str, Any]:
    """Measure how much of what was decided recently produced a lesson.

    Committing a lesson after an accept or a reject is a manager duty with no
    gate: nothing failed when it was skipped, and nothing said so. Measured on
    this repository the day the loop first closed: 3 lessons against 758
    finished cards. A duty nobody measures is a duty that quietly stops.

    Bounded and read-only. Cards older than the window are excluded because
    they were decided before a lesson could be recorded at all -- counting them
    would report history as a failure of present practice.

    Quality-review children are excluded too: a reviewer run is the review
    mechanism, not a decision about code, and a lesson drawn from one would be
    a lesson about reviewing. Left in, they dominated the denominator -- every
    one of the five most recent uncommitted cards was a reviewer child.
    """

    _readiness, db_path = task_store._require_ready(root)
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max(1, int(window_days)))
    ).isoformat()
    with closing(sqlite_readonly.connect_readonly(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        # The table is created lazily by the first commit, so a repository that
        # has never recorded a lesson has none. That is a true measurement --
        # zero coverage -- and exactly the state where this number matters, so
        # it must not raise.
        has_store = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='learning_commits'"
        ).fetchone() is not None
        decided = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE updated_at >= ? "
            "AND topic <> 'quality_review' AND (status='finished' OR status LIKE 'blocked%')",
            (cutoff,),
        ).fetchone()[0]
        with_lesson = conn.execute(
            "SELECT COUNT(DISTINCT t.task_id) FROM tasks t "
            "JOIN learning_commits l ON l.task_id = t.task_id "
            "WHERE t.updated_at >= ? "
            "AND t.topic <> 'quality_review' AND (t.status='finished' OR t.status LIKE 'blocked%')",
            (cutoff,),
        ).fetchone()[0] if has_store else 0
        missing = conn.execute(
            "SELECT t.task_id FROM tasks t "
            + ("LEFT JOIN learning_commits l ON l.task_id = t.task_id "
               if has_store else "")
            + "WHERE t.updated_at >= ? "
            + ("AND l.task_id IS NULL " if has_store else "")
            + "AND t.topic <> 'quality_review' AND (t.status='finished' OR t.status LIKE 'blocked%') "
            "ORDER BY t.updated_at DESC LIMIT 5",
            (cutoff,),
        ).fetchall()
    decided = int(decided)
    with_lesson = int(with_lesson)
    return {
        "schema_id": "aiworkhub.learning_coverage.v1",
        "window_days": int(window_days),
        "decided_cards": decided,
        "cards_with_lesson": with_lesson,
        "cards_without_lesson": max(0, decided - with_lesson),
        "coverage_percent": (
            round(with_lesson / decided * 100.0, 1) if decided else None
        ),
        "recent_without_lesson": [str(row["task_id"]) for row in missing],
    }


def commit_learning(
    repo: Path,
    *,
    actor: dict[str, str],
    request_id: str,
    data: dict[str, Any],
    idempotency_key: str,
    provenance: str,
) -> dict[str, Any]:
    """Persist and project one explicit manager learning decision."""
    request_id = _bounded(request_id, "request_id", 256)
    provenance = _bounded(provenance, "provenance", _MAX_PROVENANCE_BYTES)
    if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
        raise LearningCommitStoreError("invalid_idempotency_key")
    if actor.get("role") != "manager" or not actor.get("actor_id"):
        raise LearningCommitStoreError("verified_manager_identity_required")

    readiness = task_store.storage_readiness(repo)
    if not readiness.ready:
        raise LearningCommitStoreError(f"canonical_task_store_unavailable:{readiness.reason}")
    normalized = dict(data)
    normalized["repository_id"] = readiness.repo_id
    task_id = _bounded(normalized.get("task_id"), "task_id", 256)
    try:
        outcome = Outcome(str(normalized.get("outcome") or "").lower())
    except ValueError as exc:
        raise LearningCommitStoreError("invalid_learning_commit:invalid outcome") from exc
    card = task_store.get_task(repo, task_id)
    if card is None:
        raise LearningCommitStoreError("learning_commit_task_not_found")
    raw_evidence_ids = normalized.get("evidence_ids")
    if not isinstance(raw_evidence_ids, list):
        raise LearningCommitStoreError("invalid_learning_commit:evidence_ids must be a list")
    evidence_ids = list(raw_evidence_ids)
    if outcome == Outcome.ACCEPTED:
        canonical_reference = _canonical_acceptance_reference(card, request_id)
        evidence_ids = list(dict.fromkeys([canonical_reference, *evidence_ids]))
    elif not _request_matches_candidate(card, request_id):
        raise LearningCommitStoreError("learning_commit_request_identity_mismatch")
    normalized["evidence_ids"] = evidence_ids
    # The failure taxonomy is never caller-suppliable: it is always derived
    # server-side from the canonical card's own structured terminal evidence,
    # never from a manager-authored reason or root-cause candidate string, so
    # a model cannot talk its way into a false category.
    normalized["failure_category"] = (
        None if outcome == Outcome.ACCEPTED
        else core.classify_terminal_disposition(card).value
    )
    try:
        commit = learning_commit_from_dict(normalized)
        validate_repo_match(commit, readiness.repo_id)
    except (TypeError, ValueError) as exc:
        raise LearningCommitStoreError(f"invalid_learning_commit:{exc}") from exc
    if commit.promotion_eligible_ai_memory and not commit.lesson_candidate:
        raise LearningCommitStoreError("learning_commit_memory_promotion_requires_lesson")
    if commit.promotion_eligible_context_graph and not commit.edge_candidates:
        raise LearningCommitStoreError("learning_commit_graph_promotion_requires_edges")
    if commit.promotion_eligible_kb and not commit.invariant_candidate:
        raise LearningCommitStoreError("learning_commit_kb_promotion_requires_invariant")

    commit_id, projections, idempotent = _load_or_create(
        repo,
        commit=commit,
        request_id=request_id,
        idempotency_key=idempotency_key,
        actor=actor,
        provenance=provenance,
    )
    summary = _summary(commit_id, request_id, commit)
    source_ref = str(summary["primary_evidence"] or f"file:learning-commits/{commit_id}")
    projection_calls: dict[str, Callable[[], dict[str, Any]]] = {
        "session": lambda: context_writes.session_write(
            repo,
            actor=actor,
            action="event",
            topic="learning_commit",
            content=_json(summary),
            idempotency_key=f"learning:{commit_id}:session",
            provenance=provenance,
        ),
        "context_graph": lambda: context_graph.append_event(
            repo,
            thread_id=actor["session_id"],
            session_id=actor["session_id"],
            provider=actor["provider"],
            role="manager",
            event_type="learning_commit",
            content=_json(summary),
            source_ref=source_ref,
            idempotency_key=f"learning:{commit_id}:context",
            task_id=commit.task_id,
            metadata={
                "commit_id": commit_id,
                "learning_edges": _edge_dicts(commit),
            },
        ),
        "ai_memory": lambda: context_writes.memory_write(
            repo,
            actor=actor,
            action="remember",
            key=f"learning.{commit.task_id}.{commit_id[:12]}",
            value=_json(summary),
            tags="learning_commit,manager_verified",
            scope="project",
            idempotency_key=f"learning:{commit_id}:memory",
            provenance=provenance,
        ),
        "kb": lambda: context_writes.kb_write(
            repo,
            actor=actor,
            action="upsert",
            key=f"learning-contract.{commit.task_id}.{commit_id[:12]}",
            title=f"Verified invariant from {commit.task_id}",
            body=_json(summary),
            category="verified_project_invariant",
            tags="learning_commit,manager_verified",
            source_refs=source_ref,
            idempotency_key=f"learning:{commit_id}:kb",
            provenance=provenance,
        ),
    }

    for component, call in projection_calls.items():
        row = projections[component]
        if row.get("state") in {"applied", "not_requested", "skipped_disabled"}:
            continue
        try:
            receipt = call()
            if receipt.get("ok") is not True:
                raise LearningCommitStoreError(str(receipt.get("error") or "projection_failed"))
            projections[component] = {
                "state": "applied",
                "idempotent": bool(receipt.get("idempotent")),
                "receipt": {
                    key: receipt.get(key)
                    for key in ("document_id", "event_id", "memory_id", "key", "timestamp")
                    if receipt.get(key) is not None
                },
            }
        except (context_writes.ContextWriteError, context_graph.ContextGraphError,
                LearningCommitStoreError, OSError, sqlite3.Error) as exc:
            projections[component] = {
                "state": "failed",
                "error": f"{type(exc).__name__}:{exc}"[:300],
            }
        _save_projection(repo, commit_id, projections)

    state = _save_projection(repo, commit_id, projections)
    failures = {
        component: row.get("error", "projection_failed")
        for component, row in projections.items()
        if row.get("state") == "failed"
    }
    return {
        "ok": not failures,
        "schema_id": SCHEMA_ID,
        "commit_id": commit_id,
        "task_id": commit.task_id,
        "request_id": request_id,
        "outcome": commit.outcome.value,
        "failure_category": commit.failure_category.value if commit.failure_category else None,
        "state": state,
        "idempotent": idempotent,
        "projections": projections,
        "failures": failures,
    }


__all__ = ["LearningCommitStoreError", "SCHEMA_ID", "commit_learning"]
