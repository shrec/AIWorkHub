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
from .learning_commit import (
    ALLOWED_EVIDENCE_ID_SCHEMES,
    FailureCategory,
    LearningCommit,
    Outcome,
    commit_owed,
    learning_commit_from_dict,
    validate_repo_match,
)


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


# Written by core.reject_review only, from structured card evidence, in the
# same transaction that clears terminal_review.
_REJECTION_DISPOSITION_SCHEMA_ID = "aiworkhub.rejection_disposition.v1"


def _pinned_rejection_disposition(card: dict[str, Any], request_id: str) -> str | None:
    """Return the failure category ``core.reject_review`` pinned for this request.

    A pin belonging to a *different* episode is refused rather than borrowed:
    a card rejected twice carries only the latest pin, and attributing the
    newer episode's cause to an older commit would be worse than the absent
    answer the caller already tolerates.
    """
    pin = card.get("rejection_disposition")
    if not isinstance(pin, dict):
        return None
    if pin.get("schema_id") != _REJECTION_DISPOSITION_SCHEMA_ID:
        return None
    if str(pin.get("request_id") or "") != request_id:
        return None
    try:
        return str(FailureCategory(str(pin.get("failure_category") or "")).value)
    except ValueError:
        return None


def _rejection_failure_category(card: dict[str, Any], request_id: str) -> str:
    """Failure category for a non-accepted outcome, pin first, card second."""
    pinned = _pinned_rejection_disposition(card, request_id)
    if pinned is not None:
        return pinned
    return str(core.classify_terminal_disposition(card).value)


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

# How many of the newest decisions the head-run scan reads. The run this bounds
# is only ever compared against a small threshold, so the bound cannot change a
# verdict; it exists so a repository with a long uncommitted history reads a
# bounded number of rows rather than the whole window.
RECENT_DECISION_SCAN_LIMIT = 200


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

    Two numbers, because they answer different questions.
    ``coverage_percent`` says what the practice has been; it moves by about a
    point per lesson and no single decision can be held to it.
    ``consecutive_recent_without_lesson`` says whether the practice is running
    right now, counts only the newest decisions, and is reset to zero by filing
    one lesson for the decision in hand. It is the one an invariant can hold a
    manager to. ``None`` for ``coverage_percent`` means no decided cards in the
    window -- an absent denominator, never zero coverage.
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
        # The run of most-recently-decided cards that recorded no lesson.
        #
        # An aggregate percentage cannot be moved by the decision in hand: one
        # lesson shifts it by a point, so it can never say "this decision". The
        # head run can. Measured on this repository, newest first, the runs of
        # decided-without-lesson are 6, 15, 1, 2, 14, 6, 38 -- lessons arrive in
        # bursts and then stop, which is what a number nobody is answerable for
        # looks like.
        if has_store:
            head = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM learning_commits l "
                "WHERE l.task_id = t.task_id) AS has_lesson FROM tasks t "
                "WHERE t.updated_at >= ? "
                "AND t.topic <> 'quality_review' "
                "AND (t.status='finished' OR t.status LIKE 'blocked%') "
                "ORDER BY t.updated_at DESC LIMIT ?",
                (cutoff, RECENT_DECISION_SCAN_LIMIT),
            ).fetchall()
            streak = 0
            for row in head:
                if row["has_lesson"]:
                    break
                streak += 1
        else:
            streak = min(int(decided), RECENT_DECISION_SCAN_LIMIT)
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
        "consecutive_recent_without_lesson": streak,
        # A streak reported at the scan limit is a floor, not a total. Saying so
        # keeps a bound from reading as a measurement.
        "consecutive_recent_without_lesson_capped": (
            streak >= RECENT_DECISION_SCAN_LIMIT
        ),
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
    #
    # For a rejection that evidence is already gone by the time we run:
    # core.reject_review's rework transition clears terminal_review from the
    # card, so it classifies first and pins the answer. Prefer that pin when it
    # is bound to this exact adjudicated request; otherwise fall back to
    # re-deriving from the live card, which is still correct for an accepted or
    # otherwise-terminal card and returns the previous (inconclusive) answer
    # for a card rejected before the pin existed.
    normalized["failure_category"] = (
        None if outcome == Outcome.ACCEPTED
        else _rejection_failure_category(card, request_id)
    )
    try:
        commit = learning_commit_from_dict(normalized)
        validate_repo_match(commit, readiness.repo_id)
    except (TypeError, ValueError, evidence_levels.EvidenceValidationError) as exc:
        # ``EvidenceValidationError`` is NOT a ValueError, so a bad evidence id
        # -- the commonest measured shape failure, a ``sha256:`` receipt id --
        # escaped this handler and reached the MCP surface as an uncaught
        # exception instead of a named refusal the caller could act on.
        raise LearningCommitStoreError(
            f"invalid_learning_commit:{exc}; allowed evidence id schemes: "
            + ", ".join(ALLOWED_EVIDENCE_ID_SCHEMES)
        ) from exc
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


def adjudicated_decision(card: dict[str, Any], request_id: str) -> str:
    """Return ``"accepted"``, ``"rejected"`` or ``""`` for ONE request on a card.

    The card is the authority on its own outcome, and this is the single place
    that reads it. ``""`` means the card names no decision bound to this exact
    request -- not that none was taken -- so a caller must treat it as unknown
    rather than as an absence of judgement.
    """
    if not isinstance(card, dict):
        return ""
    request = str(request_id or "").strip()
    if not request:
        return ""
    if (
        task_store.canonical_status(card) == "finished"
        and str(card.get("accepted_request_id") or "") == request
    ):
        return "accepted"
    if _request_matches_candidate(card, request):
        return "rejected"
    return ""


def _decision_changed_paths(
    card: dict[str, Any], decision: str
) -> tuple[list[str], str]:
    """The paths the decision was taken over, and WHERE they were read from.

    Accepted: exactly what was promoted into the canonical tree. Rejected: the
    predecessor's changed paths, which ``core.reject_review`` pinned in the same
    transition that produced the feedback. Both are written by the decision path
    itself, never by a model.

    When the decision recorded no paths at all -- a card accepted with an empty
    promotion, or one rejected before the pin existed -- the card's own declared
    write scope is used instead. That is still read off the card, and the source
    is returned so a reader can tell the two apart rather than being handed a
    repo area whose provenance is invisible.
    """
    if decision == "accepted":
        evidence = card.get("accept_evidence")
        promoted = evidence.get("promoted_paths") if isinstance(evidence, dict) else None
        paths = [str(item) for item in promoted or [] if isinstance(item, str)][:256]
        if paths:
            return paths, "accept_evidence.promoted_paths"
    else:
        feedback = card.get("review_feedback")
        if isinstance(feedback, dict):
            raw = feedback.get("predecessor_changed_paths")
            if isinstance(raw, (list, tuple)) and raw:
                return (
                    [str(item) for item in raw if isinstance(item, str)][:256],
                    "review_feedback.predecessor_changed_paths",
                )
        predecessor = card.get("rework_predecessor")
        if isinstance(predecessor, dict):
            hashes = predecessor.get("changed_path_hashes")
            if isinstance(hashes, dict) and hashes:
                return (
                    sorted(str(key) for key in hashes)[:256],
                    "rework_predecessor.changed_path_hashes",
                )
    declared = card.get("allowed_writes") or card.get("read_first") or []
    if isinstance(declared, (list, tuple)) and declared:
        return (
            [str(item) for item in declared if isinstance(item, str)][:256],
            "card.allowed_writes",
        )
    return [], ""


def resolve_short_form(
    repo: str | Path, *, task_id: str, request_id: str
) -> dict[str, Any]:
    """Fill every MECHANICAL learning-commit field from the card's own decision.

    ``aiworkhub_manager_learning_commit`` demands seven fields of which six are
    already computed by :func:`learning_commit.commit_owed` and echoed in the
    accept/reject reply the manager just read. Retyping them cost 16 shape
    failures in 40 measured calls (an invalid outcome, a refused ``sha256:``
    evidence id, an identity mismatch) and coverage sat at 53 commits against
    3,383 decisions -- 1.6%.

    Nothing here is a judgement. The outcome is READ from the card's own
    decision event, the repo area is derived from the paths that decision was
    taken over, the acceptance evidence id is the canonical
    FIXED_AND_VERIFIED reference the accept path already sealed, and the
    idempotency key and provenance are the same deterministic strings
    ``commit_owed`` returns. The lesson text stays caller-authored; this writes
    nothing.
    """
    repo_path = Path(repo)
    task = str(task_id or "").strip()
    request = str(request_id or "").strip()
    if not task or not request:
        raise LearningCommitStoreError("learning_commit_task_and_request_required")
    readiness = task_store.storage_readiness(repo_path)
    if not readiness.ready:
        raise LearningCommitStoreError(
            f"canonical_task_store_unavailable:{readiness.reason}"
        )
    card = task_store.get_task(repo_path, task)
    if card is None:
        raise LearningCommitStoreError("learning_commit_task_not_found")
    decision = adjudicated_decision(card, request)
    if not decision:
        raise LearningCommitStoreError("learning_commit_request_identity_mismatch")
    evidence_reference = ""
    if decision == "accepted":
        # Raises with the exact reason when the acceptance seal is missing or
        # not FIXED_AND_VERIFIED, which is the same refusal commit_learning
        # would produce -- surfaced before the manager writes a lesson.
        evidence_reference = _canonical_acceptance_reference(card, request)
    changed_paths, paths_source = _decision_changed_paths(card, decision)
    owed = commit_owed(
        task_id=task,
        request_id=request,
        outcome=decision,
        changed_paths=changed_paths,
        evidence_reference=evidence_reference,
    )
    if not str(owed["repo_area"] or "").strip():
        raise LearningCommitStoreError(
            "learning_commit_repo_area_not_derivable:"
            f"the {decision} card records no changed paths and declares no write "
            "scope; supply repo_area explicitly"
        )
    return {
        **owed,
        "resolved_from": "task_card_decision_event",
        "repo_area_source": paths_source,
        "repository_id": readiness.repo_id,
        "allowed_evidence_id_schemes": list(ALLOWED_EVIDENCE_ID_SCHEMES),
    }


__all__ = [
    "ALLOWED_EVIDENCE_ID_SCHEMES",
    "LearningCommitStoreError",
    "SCHEMA_ID",
    "adjudicated_decision",
    "commit_learning",
    "injection_ledger_state",
    "read_card_outcomes",
    "read_correction_record",
    "resolve_short_form",
]

# ---------------------------------------------------------------------------
# Read-only correction-record readers (RM-2026-00021 layer two).
#
# The learning ledger and the cards it points at are the repository's record of
# what was corrected and why. ``skill_miner`` mines that record, and reads it
# exclusively through the three functions below so it never learns this
# module's schema. All three open the task database READ-ONLY and never write.
# ---------------------------------------------------------------------------

_MAX_CORRECTION_TEXT = 8000


def _readonly(repo: Path) -> sqlite3.Connection:
    """Open the canonical task database read-only for a bounded scan."""
    _readiness, db_path = task_store._require_ready(repo)
    return cast(sqlite3.Connection, task_store._connect(db_path, readonly=True))


def _card_of(row: Any) -> dict[str, Any]:
    try:
        card = json.loads(str(row["card_json"] or "{}"))
    except (TypeError, ValueError):
        return {}
    return card if isinstance(card, dict) else {}


def _card_paths(card: dict[str, Any]) -> list[str]:
    """The card's own write set -- the instance vocabulary a rule must not use."""
    writes = card.get("allowed_writes") or card.get("read_first") or []
    if not isinstance(writes, (list, tuple)):
        return []
    return [str(item) for item in writes if isinstance(item, str)][:64]


def read_correction_record(
    repo: str | Path, *, limit: int = 5000
) -> list[dict[str, Any]]:
    """Return the repository's correction statements with their provenance.

    Two captured sources, each already bound to the exact judgement it came
    from:

    * ``learning_commits`` -- the ``invariant_candidate`` and
      ``lesson_candidate`` a manager wrote when adjudicating one request. These
      are already rule statements.
    * ``tasks.card_json.review_feedback.instruction`` -- the manager's exact
      statement of what was wrong when a card was returned for rework.

    Rows are plain mappings, not typed records, because the consumer's job is
    to reduce them to vocabulary and this module's job is only to read them
    truthfully. ``limit`` bounds the scan; an absent or unreadable store
    yields an empty record rather than an error, so a repository that has
    never been corrected mines to zero instead of failing.
    """
    repo_path = Path(repo)
    try:
        conn = _readonly(repo_path)
    except Exception:  # noqa: BLE001 -- an unreadable store is an empty record
        return []
    rows: list[dict[str, Any]] = []
    try:
        conn.row_factory = sqlite3.Row
        cards: dict[str, dict[str, Any]] = {}
        runners: dict[str, str] = {}
        for row in conn.execute(
            "SELECT task_id, runner, card_json FROM tasks LIMIT ?", (limit,)
        ):
            task_id = str(row["task_id"])
            cards[task_id] = _card_of(row)
            runners[task_id] = str(row["runner"] or "")

        for row in conn.execute(
            "SELECT task_id, request_id, repo_area, payload_json, created_at "
            "FROM learning_commits ORDER BY created_at, task_id LIMIT ?",
            (limit,),
        ):
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            task_id = str(row["task_id"])
            card = cards.get(task_id, {})
            for field_name in ("invariant_candidate", "lesson_candidate"):
                text = str(payload.get(field_name) or "").strip()
                if not text:
                    continue
                rows.append(
                    {
                        "kind": f"learning_commit.{field_name}",
                        "task_id": task_id,
                        "anchor": str(row["request_id"] or ""),
                        "text": text[:_MAX_CORRECTION_TEXT],
                        "area": str(row["repo_area"] or ""),
                        "paths": _card_paths(card),
                        "actor": runners.get(task_id, ""),
                        "failure_category": str(payload.get("failure_category") or ""),
                        "occurred_at": str(row["created_at"] or ""),
                    }
                )

        for task_id, card in cards.items():
            feedback = card.get("review_feedback")
            if not isinstance(feedback, dict):
                continue
            text = str(feedback.get("instruction") or "").strip()
            if not text:
                continue
            rows.append(
                {
                    "kind": "review_feedback.instruction",
                    "task_id": task_id,
                    "anchor": str(feedback.get("predecessor_request_id") or ""),
                    "text": text[:_MAX_CORRECTION_TEXT],
                    "area": "",
                    "paths": _card_paths(card),
                    "actor": runners.get(task_id, ""),
                    "failure_category": "",
                    "occurred_at": "",
                }
            )
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return rows


def read_card_outcomes(repo: str | Path) -> dict[str, dict[str, Any]]:
    """Return the adjudicated outcome of every card the learning ledger judged.

    Keyed by task id AND by ``task_id:request_id``, because a skill's evidence
    entry anchors to whichever of the two the manager recorded, and a lookup
    that only understood one of them would silently report no evidence.
    """
    repo_path = Path(repo)
    try:
        conn = _readonly(repo_path)
    except Exception:  # noqa: BLE001 -- unreadable means "no outcomes known"
        return {}
    outcomes: dict[str, dict[str, Any]] = {}
    try:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT task_id, request_id, outcome, payload_json FROM learning_commits"
        ):
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except (TypeError, ValueError):
                payload = {}
            entry = {
                "outcome": str(row["outcome"] or ""),
                "failure_category": str(
                    (payload or {}).get("failure_category") or ""
                ),
                "request_id": str(row["request_id"] or ""),
            }
            task_id = str(row["task_id"])
            outcomes[f"{task_id}:{entry['request_id']}"] = entry
            # A bare task id maps to its LATEST judgement; rows arrive in
            # insertion order, so a later adjudication of the same card wins.
            outcomes[task_id] = entry
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return outcomes


def injection_ledger_state(repo: str | Path) -> dict[str, Any]:
    """Report whether skill injection into cards is measurable at all.

    NF-2026-00312 layer six needs "the count of cards a skill was injected
    into". This function establishes, by measurement rather than assumption,
    whether that count can be produced -- and on this repository it currently
    cannot, for two independent reasons that are both reported:

    * no stored card carries a persisted skill packet, because the worker
      bundle builds the packet at prompt-build time and does not write it back;
    * no stored card carries the selection vocabulary a packet needs, so
      replaying selection over history would return zero for every skill
      whatever its merit.

    Reporting ``state="unavailable"`` with both counts is the honest answer. A
    rate computed against a zero denominator would read as a measurement and be
    a fabrication, and the retirement verdict that rested on it would retire
    good skills and keep bad ones with equal confidence.
    """
    from . import skill_registry as _skill_registry
    from . import skill_registry_store as _skill_store

    repo_path = Path(repo)
    # The selection receipt store is now the authority for "which skills did
    # this card receive". It is a RECORD of what was injected, written at the
    # selection site, never a replay of selection over history -- so a card with
    # no receipt still contributes nothing rather than a guess.
    try:
        receipts = _skill_store.list_selections(repo_path)
    except (_skill_store.SkillStoreError, OSError, sqlite3.Error):
        receipts = []
    receipt_cards = {str(item["task_id"]) for item in receipts}
    receipt_cards_with_skills = {
        str(item["task_id"]) for item in receipts if item["skills"]
    }
    try:
        conn = _readonly(repo_path)
    except Exception:  # noqa: BLE001
        return {
            "state": "available" if receipt_cards_with_skills else "unavailable",
            "reason": (
                "" if receipt_cards_with_skills else "task_store_unreadable"
            ),
            "cards_scanned": 0,
            "cards_with_persisted_packet": len(receipt_cards),
            "cards_with_selection_context": 0,
            "injected_cards": len(receipt_cards_with_skills),
            "selection_receipts": len(receipts),
        }
    scanned = with_packet = with_context = 0
    try:
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT task_id, card_json FROM tasks"):
            card = _card_of(row)
            scanned += 1
            context = card.get("project_context")
            if str(row["task_id"]) in receipt_cards or (
                isinstance(context, dict)
                and any("skill" in str(key).lower() for key in context)
            ):
                with_packet += 1
            try:
                if _skill_registry.card_selection_context(card) is not None:
                    with_context += 1
            except _skill_registry.SkillRegistryError:
                continue
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    injected = len(receipt_cards_with_skills)
    measurable = injected > 0
    return {
        "state": "available" if measurable else "unavailable",
        "reason": (
            ""
            if measurable
            else "no_card_persists_a_skill_packet_and_no_selection_receipt_is_recorded"
        ),
        "cards_scanned": scanned,
        "cards_with_persisted_packet": max(with_packet, len(receipt_cards)),
        "cards_with_selection_context": with_context,
        "injected_cards": injected,
        "selection_receipts": len(receipts),
    }

