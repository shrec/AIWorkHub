"""Bounded executor for the authenticated review lifecycle outbox."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from . import (
    needfix_store,
    review_lifecycle,
    task_engine,
    task_store,
    workforce_catalog,
    workforce_router,
)


LENSES = ("correctness", "security", "code_quality")
RECEIPT_SCHEMA = "aiworkhub.review_orchestrator_receipt.v1"

# The hard per-pass bound ``drain`` has always enforced, now also its default.
# One action per reconcile pass could not work off an outbox holding 627
# chains: measured, 30 launches completed automatically in six days while 570
# were typed by hand.
DEFAULT_DRAIN_MAX_ACTIONS = 12

# ACCEPTANCE IS NOT AUTOMATED, AND THIS IS THE SWITCH THAT SAYS SO.
#
# ``review_lifecycle.PLAN`` contains a ``target_accept`` action whose effect is
# ``manager.accept_review(target_request, target_task, ...)``. It was
# unreachable only by accident: every chain failed at its first launch action,
# so nothing ever walked far enough to reach index 9. Repairing the launch
# identity read removes that accident, and an accident is not a control.
#
# Launching a reviewer is not acceptance. Acceptance is a verified manager's
# decision, recorded against that manager's identity, and no orchestrator pass
# may make it. With this False, ``target_accept`` fails with an explicit
# reason; the chain parks there and its two remaining actions are retired by
# the ordinary dead-chain reconciliation. Turning it on would be a deliberate,
# separately-argued change to who accepts -- never a side effect of fixing a
# launch check.
AUTOMATIC_TARGET_ACCEPT_ENABLED = False

# The actions whose whole purpose is to drive ONE candidate through review.
# A target that has left `review` cannot be driven through it, so attempting
# them can only fail -- and a failed action parks every later action in its
# chain, which is how 129 chains and 1,389 actions became permanently
# unreservable here. ``needfix_close`` is deliberately absent: it is
# bookkeeping that stays meaningful after the review ended.
REVIEW_DRIVING_ACTIONS = frozenset({
    "launch", "accept", "archive", "target_accept", "target_archive",
})
# Statuses a target can still be driven through review from. Anything else is
# a decided outcome, and fail-closed means an UNREADABLE card counts as still
# reviewable -- never retire an action on a card we could not read.
REVIEWABLE_TARGET_STATUSES = frozenset({"review", "processing", "pending"})
MAX_RECEIPT_BYTES = 16 * 1024


class Manager(Protocol):
    repo: Path

    def _append_event(self, event: Mapping[str, Any]) -> None: ...
    def launch_quality_reviewer(self, **kwargs: Any) -> dict[str, Any]: ...
    def accept_review(self, request_id: str, task_id: str, **kwargs: Any) -> dict[str, Any]: ...
    def reject_review(self, task_id: str, reason: str, *, to: str = ...) -> dict[str, Any]: ...
    def status(self, request_id: str) -> dict[str, Any]: ...


RouteSelector = Callable[[Path, str, str], Mapping[str, Any]]


def _side_table_connection(db_path: str | Path) -> sqlite3.Connection:
    """The one way this module opens its own side tables.

    ``review_lifecycle`` owns the authenticated chain and outbox rows and opens
    them itself. Everything the ORCHESTRATOR retains beside them -- the expected
    workspace binding, the tier's lens plan, the replay plan -- lives in the
    same file and was being opened at eight separate call sites that had drifted
    apart in nothing but spelling. One opener means one place to change the
    timeout, the row factory or the journal mode, and one place a reader has to
    look to know how this module talks to that database.
    """
    return sqlite3.connect(db_path)


def canonical_review_db(manager: Manager) -> Path | None:
    """Return the sole task-store DB, or no authority for an unready fake repo."""
    readiness = task_store.storage_readiness(manager.repo)
    return Path(readiness.canonical_db) if readiness.ready else None


# --- routing catalog -----------------------------------------------------
#
# ``workforce_catalog.rank_task`` defaults ``catalog`` to a bare
# ``build_catalog(repo)``, whose ``process_rows`` and ``usage_rows`` default to
# empty (workforce_catalog.py:735-750). Ranked on no evidence at all, every
# eligible candidate ties on all nine substantive keys and selection falls
# through to the final lexical ``(provider, model, worker_id)`` tie-break: the
# reviewer for ``risk="critical"`` work chosen by string ordering, a decision
# that looks successful from the outside. Measured here before this call was
# wired: 2 eligible candidates, both accepted_rate=0.5, sample_count=0,
# p50=3600.0, and NO key differing between them.
#
# ``build_routing_catalog`` is the one place that joins the process ledger and
# the cost ledger onto the catalog, so the ranking sees the evidence this
# repository already holds.
#
# It COSTS time, it does not save it: measured bare rank_task 2.20s vs 3.69s
# through the parameterised path, +1.49s per reviewer launch. The justification
# is an evidenced decision, never speed. Because one ``drain`` pass runs up to
# 12 actions, the catalog is memoised so that cost is paid once per pass rather
# than once per launch.
_ROUTING_CATALOG_TTL_SECONDS = 60.0
_ROUTING_CATALOG_CACHE: dict[str, tuple[float, Mapping[str, Any]]] = {}


def reset_routing_catalog_cache() -> None:
    """Drop the memoised routing catalog. Called at the top of every pass."""
    _ROUTING_CATALOG_CACHE.clear()


def _usable_catalog(built: Any) -> Mapping[str, Any] | None:
    """Return a catalog only if it can actually rank; otherwise None.

    ``rank_task`` does ``dict(catalog or build_catalog(repo))``, so a falsy
    catalog already degrades to the conservative prior on its own. A catalog
    that is truthy but carries no workers does NOT: it ranks zero candidates,
    yields ``launch_contract=None`` and takes the reviewer launch down with it.
    That shape is rejected here so it can never reach ``rank_task``.
    """
    if not isinstance(built, Mapping):
        return None
    workers = built.get("workers")
    if not isinstance(workers, (list, tuple)) or not workers:
        return None
    return built


def routing_catalog(repo: Path) -> Mapping[str, Any] | None:
    """Return the evidenced routing catalog, or None to rank on the prior.

    Fail closed means the REVIEW still happens. A reviewer chosen on the
    conservative prior is a worse decision than one chosen on evidence; a
    reviewer that never launches at all, because reading a ledger raised, is
    worse than both. So every failure degrades to None, and the caller ranks
    exactly as it did before this helper existed.
    """
    key = str(repo)
    cached = _ROUTING_CATALOG_CACHE.get(key)
    now = time.monotonic()
    if cached is not None and (now - cached[0]) < _ROUTING_CATALOG_TTL_SECONDS:
        return cached[1]
    try:
        built = workforce_catalog.build_routing_catalog(repo)
    except Exception:
        # Deliberately broad. This reads the cost ledger and, through a lazy
        # ``from . import dashboard``, the process ledger -- a chain that can
        # raise OSError, sqlite3.Error, ValueError on a malformed row, or
        # ImportError/SyntaxError from a module mid-edit (observed live during
        # this change). Any of them must cost evidence, never the review.
        return None
    catalog = _usable_catalog(built)
    if catalog is None:
        return None
    _ROUTING_CATALOG_CACHE[key] = (now, catalog)
    return catalog


def select_reviewer_route(repo: Path, reviewer_task_id: str, lens: str) -> Mapping[str, Any]:
    """Select one currently available review worker through canonical policy."""
    readiness = task_store.storage_readiness(repo)
    if not readiness.ready:
        raise RuntimeError("review_route_storage_not_ready:" + readiness.reason)
    task = workforce_router.TaskRequirements.build(
        task_id=reviewer_task_id,
        repo_id=readiness.repo_id,
        kinds=("review",),
        risk="critical",
        tool_needs=("source-graph", "session-manager", "ai-memory", "kb"),
    )
    # A None catalog makes rank_task rebuild the bare one itself, which is
    # precisely the conservative-prior ranking this did unconditionally before.
    ranked = workforce_catalog.rank_task(repo, task, catalog=routing_catalog(repo))
    contract = ranked.get("launch_contract")
    if not isinstance(contract, Mapping):
        raise RuntimeError("review_route_unavailable:" + lens)
    route = {
        "runner": str(contract.get("runner") or ""),
        "adapter_id": str(contract.get("adapter_id") or ""),
        "model": str(contract.get("model") or ""),
    }
    if not all(route.values()) or route["runner"] == "codex":
        raise RuntimeError("review_route_identity_invalid")
    return route


# --- mechanical short-circuit -------------------------------------------
#
# Measured on this repository's canonical ledger: of 2,470 reject_review
# events, 92.3% were mechanically decidable (69.3% red tests/regression,
# 8.4% unwired/required output unchanged, 7.9% receipt conformance, 5.1%
# workspace/infra, 1.5% lint ratchet, 0.1% scope) and only 3.0% needed a
# model to judge semantic correctness. Every one of those still consumed a
# quality-reviewer launch to discover, at an average 453,650 input tokens
# per reviewer usage record.
#
# The verdict that decides this is ALREADY computed and already on the card:
# task_store._mark_terminal_review_transaction writes
# card["deterministic_verification"] (task_store.py:2155) and the same object
# into card["terminal_review"]["deterministic_verification"] (task_store.py:2120)
# from task_fsm.deterministic_verification (task_fsm.py:394). Nothing in the
# review queue read it. This is the read.
#
# Every rule below is one-directional. A positive, explicit, in-epoch
# mechanical failure short-circuits; ABSOLUTELY EVERYTHING ELSE -- absent,
# malformed, stale, nothing_measured, unreadable, or merely asserted without
# a measured count -- falls through to the normal reviewer launch. Skipping a
# review because evidence was missing would convert "we did not measure" into
# "it passed", the exact inversion this project's fail-closed doctrine
# forbids, and a defect let through unreviewed costs far more than the tokens
# this saves.
MECHANICAL_FAILURE_COUNT_FIELDS = (
    "failed_validation_count",
    "missing_required_output_count",
)


def _positive_count(verdict: Mapping[str, Any], key: str) -> int:
    """Return a trustworthy non-negative count, or 0 when it is not one.

    A bool is not a count (``True`` is not "one failure"), and neither is a
    string, a float or a negative. Anything unreadable contributes nothing,
    so it can only ever make the short-circuit LESS likely to fire.
    """
    value = verdict.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def mechanical_failure_reason(card: Any, claim_epoch: str) -> str:
    """Name the positive mechanical failure on this card, or "" to review it.

    Pure and total: never raises, never mutates, and returns "" for every
    input that is not an explicit, measured, in-epoch mechanical failure.
    ``claim_epoch`` is the chain's bound epoch; a verdict recorded against a
    different claim is STALE evidence about a previous attempt and must never
    decide this one.
    """
    if not isinstance(card, Mapping):
        return ""
    # Same precedence core.py:3939-3941 uses when it gates mark-done on this
    # verdict: the terminal_review copy is the one bound to the outcome, and
    # the hoisted top-level copy is the fallback.
    terminal = card.get("terminal_review")
    verdict = terminal.get("deterministic_verification") if isinstance(terminal, Mapping) else None
    if not isinstance(verdict, Mapping):
        verdict = card.get("deterministic_verification")
    if not isinstance(verdict, Mapping):
        return ""
    # `is True` / `is False`, never truthiness: a missing key, None, 0 or ""
    # must read as "no verdict", not as a verdict that failed.
    if verdict.get("applicable") is not True or verdict.get("pass") is not False:
        return ""
    if str(verdict.get("claim_epoch") or "") != str(claim_epoch or ""):
        return ""
    evidence = verdict.get("evidence_verdict")
    if not isinstance(evidence, Mapping):
        return ""
    # "Nothing measured" is not "nothing wrong" -- task_fsm.evidence_verdict
    # exists precisely to keep those apart. An unmeasured candidate is the
    # one that most needs a reviewer, so it always gets one.
    if evidence.get("nothing_measured") is not False:
        return ""
    # The decisive requirement: a POSITIVE measured count. `pass: False` alone
    # is an assertion; a non-zero failed/missing count is a measurement. Only
    # a measurement may spend a card's review.
    counts = {key: _positive_count(evidence, key) for key in MECHANICAL_FAILURE_COUNT_FIELDS}
    if not any(counts.values()):
        return ""
    detail = ",".join(f"{key}={counts[key]}" for key in MECHANICAL_FAILURE_COUNT_FIELDS)
    return "mechanically_failing_candidate:" + detail

def register_finalized_candidate(
    manager: Manager,
    *,
    db_path: str | Path,
    metadata: Mapping[str, Any],
    artifact_receipt: Mapping[str, Any],
    changed_path_hashes: Mapping[str, Any],
    quality_gate: Mapping[str, Any] | None = None,
) -> review_lifecycle.ReviewChain:
    """Bind the automatic chain to the exact sealed candidate transition.

    ``quality_gate`` is forwarded so a chain seeded through this entry point
    plans the tier's lens set rather than silently falling back to every lens.
    """
    registration = candidate_registration(
        metadata=metadata,
        artifact_receipt=artifact_receipt,
        changed_path_hashes=changed_path_hashes,
        quality_gate=quality_gate,
    )
    return register_candidate(manager, db_path=db_path, registration=registration)


def candidate_digest(changed_path_hashes: Mapping[str, Any]) -> str:
    """Return the exact candidate digest the chain identity is bound to."""
    candidate_json = json.dumps(
        dict(changed_path_hashes), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(candidate_json.encode("utf-8")).hexdigest()


def workspace_identity(workspace_metadata: Any) -> str:
    """Return a stable identity for the retained candidate workspace.

    ``WorkerWorkspace.as_metadata`` is the only durable description of the
    workspace a candidate was sealed in, and it is the same object at
    registration and at launch, so a digest over its identifying triple is
    reproducible without storing the whole metadata blob. An unreadable or
    incomplete metadata mapping returns ``""`` -- an absent identity, never a
    fabricated one.
    """
    if not isinstance(workspace_metadata, Mapping):
        return ""
    request_id = str(workspace_metadata.get("request_id") or "")
    path = str(workspace_metadata.get("path") or "")
    if not request_id or not path:
        return ""
    preimage = json.dumps(
        {
            "schema_id": "aiworkhub.review_candidate_workspace_identity.v1",
            "request_id": request_id,
            "path": path,
            "base_oid": str(workspace_metadata.get("base_oid") or ""),
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def candidate_registration(
    *,
    metadata: Mapping[str, Any],
    artifact_receipt: Mapping[str, Any],
    changed_path_hashes: Mapping[str, Any],
    quality_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the bounded durable preimage needed to retry chain creation.

    ``quality_gate`` carries the finalizer's ``review_risk_profile``
    observation, so the lens plan is decided from the tier the candidate
    actually earned rather than from a static three-lens list. It is optional
    and additive: a registration without it plans every lens, exactly as
    before.
    """
    registration: dict[str, Any] = {
        "target_task_id": str(metadata["task_id"]),
        "target_request_id": str(metadata["request_id"]),
        "claim_epoch": str(metadata["claim_epoch"]),
        "packet_sha256": str(artifact_receipt.get("manifest_sha256") or ""),
        "candidate_sha256": candidate_digest(changed_path_hashes),
    }
    profile = (
        quality_gate.get("review_risk_profile")
        if isinstance(quality_gate, Mapping)
        else None
    )
    if isinstance(profile, Mapping) and not str(profile.get("error") or ""):
        lenses = [
            str(lens)
            for lens in (profile.get("required_reviewer_lenses") or ())
            if str(lens) in LENSES
        ]
        registration["effective_tier"] = str(profile.get("effective_tier") or "")
        registration["required_reviewer_lenses"] = lenses
    return registration


TARGET_IDENTITY_FIELDS = (
    "target_task_id", "target_request_id", "claim_epoch",
    "packet_sha256", "candidate_sha256",
)

# The contract half of a replay key. Identical candidate BYTES reviewed against
# a different CONTRACT is a different review: the same diff can pass an
# objective and fail the one that replaced it, and the acceptance criteria, the
# required outputs, the declared validation and the forbidden set are exactly
# what a lens is asked to judge the bytes against. These are the same fields
# ``quality_reviewer.build_review_packet`` seals into ``packet.contract``.
CONTRACT_IDENTITY_FIELDS = (
    "objective", "acceptance", "required_outputs", "validation", "forbidden",
)
CONTRACT_IDENTITY_SCHEMA = "aiworkhub.review_contract_identity.v1"


def contract_identity_digest(card: Any) -> str:
    """Digest the contract a lens is asked to judge a candidate against.

    Returns ``""`` for an unreadable card. Empty is UNKNOWN, and unknown never
    matches anything, so a card that cannot be read simply launches a fresh
    reviewer -- the same outcome as before any of this existed.
    """
    if not isinstance(card, Mapping):
        return ""
    preimage = {"schema_id": CONTRACT_IDENTITY_SCHEMA}
    for field in CONTRACT_IDENTITY_FIELDS:
        value = card.get(field)
        if isinstance(value, str):
            preimage[field] = value
        elif isinstance(value, (list, tuple)):
            preimage[field] = [str(item) for item in value]
        elif value is None:
            preimage[field] = None
        else:
            # An unexpected shape is not silently normalized into a match.
            return ""
    encoded = json.dumps(
        preimage, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def target_identity_from_card(card: Any) -> dict[str, str]:
    """Read the candidate identity from where the finalizer actually writes it.

    ``_finalize_isolated_request`` seals every one of these on
    ``terminal_review.evidence``: ``request_identity`` (request/task/epoch),
    ``attempt_artifact_manifest.manifest_sha256`` (the packet digest) and
    ``changed_path_hashes`` (whose canonical digest IS ``candidate_sha256``).
    Nothing writes them as top-level card keys, which is why the readiness
    check that read them there refused 522 of 581 failed launch actions with
    ``target_request_identity_invalid`` -- measured over this repository's
    627 chains, 0 target cards carry a top-level ``request_id``.

    Returns ``{}`` unless every field resolves; a partially readable candidate
    is not an identity and must never be treated as one.
    """
    if not isinstance(card, Mapping):
        return {}
    terminal = card.get("terminal_review")
    evidence = terminal.get("evidence") if isinstance(terminal, Mapping) else None
    if not isinstance(evidence, Mapping):
        return {}
    request_identity = evidence.get("request_identity")
    if not isinstance(request_identity, Mapping):
        return {}
    manifest = evidence.get("attempt_artifact_manifest")
    changed_path_hashes = evidence.get("changed_path_hashes")
    epoch = request_identity.get("claim_epoch")
    if epoch is None:
        epoch = card.get("claim_epoch")
    identity = {
        "target_task_id": str(request_identity.get("task_id") or ""),
        "target_request_id": str(request_identity.get("request_id") or ""),
        "claim_epoch": str(epoch if epoch is not None else ""),
        "packet_sha256": str(
            manifest.get("manifest_sha256") if isinstance(manifest, Mapping) else ""
        ),
        "candidate_sha256": (
            candidate_digest(changed_path_hashes)
            if isinstance(changed_path_hashes, Mapping)
            else ""
        ),
    }
    if not all(identity[field] for field in TARGET_IDENTITY_FIELDS):
        return {}
    return identity


def target_identity_from_registration(event: Any) -> dict[str, str]:
    """Read the identity out of the registration payload the finalizer stored.

    ``process_launcher`` records the exact ``candidate_registration`` preimage
    on the terminal event as ``review_automation.registration``, both when the
    chain seeds and when it stays pending for retry. It is the same five
    fields, written by the same transition, so it is used here to CORROBORATE
    the card read -- and, for a card whose terminal evidence has since been
    replaced, as the only remaining statement of what this chain was bound to.
    """
    if not isinstance(event, Mapping):
        return {}
    automation = event.get("review_automation")
    if not isinstance(automation, Mapping):
        return {}
    registration = automation.get("registration")
    if not isinstance(registration, Mapping):
        return {}
    identity = {field: str(registration.get(field) or "") for field in TARGET_IDENTITY_FIELDS}
    if not all(identity[field] for field in TARGET_IDENTITY_FIELDS):
        return {}
    return identity


def _legacy_card_identity(card: Any) -> dict[str, str]:
    """Top-level card keys. Never produced in production; kept for exact fixtures."""
    if not isinstance(card, Mapping):
        return {}
    identity = {
        "target_task_id": str(card.get("task_id") or ""),
        "target_request_id": str(card.get("request_id") or ""),
        "claim_epoch": str(card.get("claim_epoch") or ""),
        "packet_sha256": str(card.get("packet_sha256") or ""),
        "candidate_sha256": str(card.get("candidate_sha256") or ""),
    }
    if not all(identity[field] for field in TARGET_IDENTITY_FIELDS):
        return {}
    return identity


def resolve_target_identity(status: Any) -> dict[str, Any]:
    """Resolve one candidate identity, its provenance, and any disagreement.

    Precedence is authority order, not convenience order: the canonical card's
    sealed terminal evidence first, the durable registration payload second,
    the legacy top-level keys last. When the first two both resolve and
    disagree, that is reported as a conflict and the caller fails closed --
    two durable statements about which bytes are under review must never be
    silently reconciled by picking one.
    """
    card = status.get("task_card") if isinstance(status, Mapping) else None
    latest_event = status.get("latest_event") if isinstance(status, Mapping) else None
    from_card = target_identity_from_card(card)
    from_registration = target_identity_from_registration(latest_event)
    conflict = ""
    if from_card and from_registration and from_card != from_registration:
        differing = sorted(
            field for field in TARGET_IDENTITY_FIELDS
            if from_card[field] != from_registration[field]
        )
        # A newer claim episode legitimately replaces the sealed evidence while
        # the registration still names the older one; that is supersession, not
        # tampering, and the card (the newer statement) wins.
        if differing == ["claim_epoch"] or "target_request_id" in differing:
            conflict = ""
        else:
            conflict = ",".join(differing)
    legacy = {} if (from_card or from_registration) else _legacy_card_identity(card)
    identity = from_card or from_registration or legacy
    source = (
        "terminal_review_evidence" if from_card
        else "review_automation_registration" if from_registration
        else "card_top_level" if legacy
        else "unavailable"
    )
    workspace = ""
    if isinstance(card, Mapping):
        terminal = card.get("terminal_review")
        evidence = terminal.get("evidence") if isinstance(terminal, Mapping) else None
        if isinstance(evidence, Mapping):
            workspace = workspace_identity(evidence.get("workspace"))
        if not workspace:
            workspace = str(card.get("workspace_identity") or "")
    return {
        "identity": identity,
        "identity_source": source,
        "workspace_identity": workspace,
        "conflict": conflict,
    }


def register_candidate(
    manager: Manager,
    *,
    db_path: str | Path,
    registration: Mapping[str, Any],
) -> review_lifecycle.ReviewChain:
    """Create or replay one exact chain from a durable registration preimage."""
    return ReviewOrchestrator(manager, db_path=db_path).ensure_chain(
        target_task_id=str(registration["target_task_id"]),
        target_request_id=str(registration["target_request_id"]),
        claim_epoch=str(registration["claim_epoch"]),
        packet_sha256=str(registration["packet_sha256"]),
        candidate_sha256=str(registration["candidate_sha256"]),
        required_reviewer_lenses=registration.get("required_reviewer_lenses"),
        effective_tier=str(registration.get("effective_tier") or ""),
    )


# --- tier-planned lenses -------------------------------------------------
#
# ``review_lifecycle.PLAN`` is a fixed 12-action shape and every stored row is
# authenticated against it (``_verify_action_row`` refuses a chain whose action
# count or per-index descriptor differs), so the lens set cannot be varied by
# shortening the plan without invalidating every chain already on disk.
#
# The plan therefore stays 12 actions and the TIER decides which of them do
# work. A lens the effective profile does not require is completed as an
# explicit ``obsolete`` receipt -- the mechanism already used for an action
# whose target left review -- so the chain still walks its authenticated shape
# while spending no reviewer. Measured on accepted targets: 194 of 893 launches
# (21.7%) were of a lens the accepted tier never required, at ~889K input
# tokens each.
#
# Fail-open on the plan, fail-closed at accept: an unknown or unreadable tier
# binds the full lens set (what happened before this existed), and a tier
# planned too narrowly is caught by ``required_reviewer_missing`` in the accept
# fold, which refuses to accept. Under-planning costs a relaunch; it can never
# buy an acceptance.
# --- hash-keyed replay ---------------------------------------------------
#
# Side table, deliberately outside the immutable lifecycle rows: the plan is a
# DECISION about what to do, not a receipt of what was done. The receipt of the
# replay lands where every other receipt lands -- in the completed launch
# action, carrying ``replayed_from_chain`` and both packet digests.
REPLAY_PLAN_TABLE = (
    "CREATE TABLE IF NOT EXISTS review_orchestrator_replay_plan ("
    "chain_id INTEGER NOT NULL, lens TEXT NOT NULL, plan_json TEXT NOT NULL, "
    "PRIMARY KEY (chain_id, lens))"
)

LENS_PLAN_TABLE = (
    "CREATE TABLE IF NOT EXISTS review_orchestrator_lens_plan ("
    "chain_id INTEGER PRIMARY KEY, lenses TEXT NOT NULL, "
    "effective_tier TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '')"
)


def _normalize_lenses(lenses: Any) -> tuple[str, ...]:
    if isinstance(lenses, str) or not isinstance(lenses, (list, tuple, set, frozenset)):
        return ()
    return tuple(lens for lens in LENSES if lens in {str(value) for value in lenses})


def bind_lens_plan(
    db_path: str | Path,
    *,
    chain_id: int,
    lenses: Any,
    effective_tier: str = "",
    source: str = "registration",
) -> tuple[str, ...]:
    """Bind the required lens set for one chain, once. Returns what is bound."""
    planned = _normalize_lenses(lenses)
    with closing(_side_table_connection(db_path)) as conn, conn:
        conn.execute(LENS_PLAN_TABLE)
        conn.execute(
            "INSERT OR IGNORE INTO review_orchestrator_lens_plan "
            "(chain_id, lenses, effective_tier, source) VALUES (?,?,?,?)",
            (int(chain_id), ",".join(planned), str(effective_tier), str(source)),
        )
        row = conn.execute(
            "SELECT lenses FROM review_orchestrator_lens_plan WHERE chain_id=?",
            (int(chain_id),),
        ).fetchone()
    return _normalize_lenses(str(row[0]).split(",")) if row is not None else planned


def required_lenses(db_path: str | Path, chain_id: int) -> tuple[str, ...]:
    """Return the lens set this chain must launch, or every lens when unplanned."""
    try:
        with closing(_side_table_connection(db_path)) as conn:
            conn.execute(LENS_PLAN_TABLE)
            row = conn.execute(
                "SELECT lenses FROM review_orchestrator_lens_plan WHERE chain_id=?",
                (int(chain_id),),
            ).fetchone()
    except sqlite3.Error:
        return LENSES
    if row is None:
        return LENSES
    return _normalize_lenses(str(row[0]).split(","))


def lens_plan_record(db_path: str | Path, chain_id: int) -> dict[str, Any]:
    """Read-only view of one chain's lens plan for a manager or a review packet."""
    try:
        with closing(_side_table_connection(db_path)) as conn:
            conn.execute(LENS_PLAN_TABLE)
            row = conn.execute(
                "SELECT lenses, effective_tier, source FROM "
                "review_orchestrator_lens_plan WHERE chain_id=?",
                (int(chain_id),),
            ).fetchone()
    except sqlite3.Error as exc:
        return {"planned": False, "error": f"{type(exc).__name__}"[:80],
                "lenses": list(LENSES), "effective_tier": "", "source": ""}
    if row is None:
        return {"planned": False, "lenses": list(LENSES), "effective_tier": "",
                "source": "unplanned_defaults_to_every_lens"}
    return {
        "planned": True,
        "lenses": list(_normalize_lenses(str(row[0]).split(","))),
        "effective_tier": str(row[1] or ""),
        "source": str(row[2] or ""),
    }


def add_required_lens(db_path: str | Path, *, chain_id: int, lens: str) -> tuple[str, ...]:
    """Manager override: add one lens beyond the tier. Never removes a lens.

    The tier decides the floor; a manager may always ask for more review than
    the floor requires. Nothing here can ask for less, because a lens removed
    after a chain was planned would silently lower an acceptance bar that
    ``accept_review`` is still going to enforce.
    """
    if lens not in LENSES:
        raise ValueError("unknown_review_lens:" + str(lens))
    with closing(_side_table_connection(db_path)) as conn, conn:
        conn.execute(LENS_PLAN_TABLE)
        row = conn.execute(
            "SELECT lenses FROM review_orchestrator_lens_plan WHERE chain_id=?",
            (int(chain_id),),
        ).fetchone()
        current = _normalize_lenses(str(row[0]).split(",")) if row is not None else LENSES
        merged = _normalize_lenses({*current, lens})
        conn.execute(
            "INSERT INTO review_orchestrator_lens_plan "
            "(chain_id, lenses, effective_tier, source) VALUES (?,?,?,'manager_override') "
            "ON CONFLICT(chain_id) DO UPDATE SET lenses=excluded.lenses, "
            "source='manager_override'",
            (int(chain_id), ",".join(merged), ""),
        )
    return merged


def retry_pending_registrations(
    manager: Manager,
    *,
    db_path: str | Path,
    events: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Replay each latest durable pending seed once per reconciliation pass."""
    retried = seeded = failed = 0
    failures: list[dict[str, str]] = []
    for event in events.values():
        automation = event.get("review_automation")
        if not isinstance(automation, dict) or automation.get("state") != "pending":
            continue
        registration = automation.get("registration")
        if not isinstance(registration, dict):
            continue
        retried += 1
        try:
            chain = register_candidate(manager, db_path=db_path, registration=registration)
        except Exception as exc:  # one durable failure receipt per bounded pass
            failed += 1
            error = f"{type(exc).__name__}:{exc}"[:300]
            failures.append({
                "request_id": str(event.get("request_id") or ""),
                "error": error,
            })
            manager._append_event({
                **dict(event),
                "review_automation": {
                    "state": "pending",
                    "registration": dict(registration),
                    "error": error,
                },
            })
        else:
            seeded += 1
            manager._append_event({
                **dict(event),
                "review_automation": {
                    "state": "seeded",
                    "registration": dict(registration),
                    "chain_identity_sha256": str(
                        getattr(chain, "chain_identity_sha256", "")
                    ),
                },
            })
    return {
        "automation_retried": retried,
        "automation_seeded": seeded,
        "automation_failed": failed,
        "automation_failures": failures,
    }


@dataclass(frozen=True, slots=True)
class DrainResult:
    attempted: int
    completed: int
    failed: int
    pending: int
    counts: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "completed": self.completed,
            "failed": self.failed,
            "pending": self.pending,
            "review_actions": dict(self.counts),
        }


class _DeferredLaunch(RuntimeError):
    """A recoverable launch gate result, kept distinct from effect failures."""

    def __init__(self, reason: str, receipt: Mapping[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.receipt = dict(receipt)


class ReviewOrchestrator:
    """Execute at most ``max_actions`` effects, never while SQLite is open."""

    def __init__(
        self,
        manager: Manager,
        *,
        db_path: str | Path,
        route_selector: RouteSelector | None = None,
        owner: str = "process-manager-review-driver",
        lease_seconds: int = 300,
    ) -> None:
        self.manager = manager
        self.db_path = Path(db_path)
        self.route_selector = route_selector or select_reviewer_route
        self.owner = str(owner)
        self.lease_seconds = max(1, int(lease_seconds))

    def ensure_chain(
        self,
        *,
        target_task_id: str,
        target_request_id: str,
        claim_epoch: str | int,
        packet_sha256: str,
        candidate_sha256: str,
        now: datetime | None = None,
        required_reviewer_lenses: Any = None,
        effective_tier: str = "",
    ) -> review_lifecycle.ReviewChain:
        contract_identity = self._contract_identity(target_request_id)
        chain = review_lifecycle.create_or_replay_chain(
            self.db_path,
            target_task_id=target_task_id,
            target_request_id=target_request_id,
            claim_epoch=claim_epoch,
            packet_sha256=packet_sha256,
            candidate_sha256=candidate_sha256,
            now=now,
            contract_identity_sha256=contract_identity,
        )
        self._bind_expected_workspace(chain)
        self._bind_lens_plan(chain, required_reviewer_lenses, effective_tier)
        self._bind_replay_plan(
            chain,
            target_task_id=target_task_id,
            candidate_sha256=candidate_sha256,
            contract_identity_sha256=contract_identity,
        )
        return chain

    def _contract_identity(self, target_request_id: str) -> str:
        """Digest the target's contract, or return ``""`` when it cannot be read."""
        try:
            status = self.manager.status(str(target_request_id))
        except Exception:  # noqa: BLE001 -- unknown contract, never a replay
            return ""
        card = status.get("task_card") if isinstance(status, Mapping) else None
        return contract_identity_digest(card)

    def _bind_replay_plan(
        self,
        chain: review_lifecycle.ReviewChain,
        *,
        target_task_id: str,
        candidate_sha256: str,
        contract_identity_sha256: str,
    ) -> None:
        """Record which lenses this chain may serve from an already-ingested report.

        Decided HERE, at registration, because this is the moment the chain's
        bytes and contract are known and nothing has been spent yet. It is
        APPLIED in ``_execute``, where the launch action completes through the
        ordinary authenticated outbox path -- a replay must not need a second
        way of completing an action, and it does not get one.

        Bound once and never rewritten: ``INSERT OR IGNORE``, so a re-registered
        chain keeps the decision its first registration measured.
        """
        try:
            sources = review_lifecycle.replay_sources(
                self.db_path,
                target_task_id=target_task_id,
                candidate_sha256=candidate_sha256,
                contract_identity_sha256=contract_identity_sha256,
                exclude_chain_id=chain.chain_id,
            )
        except (sqlite3.Error, review_lifecycle.ReviewLifecycleError):
            # A lookup that cannot run means no replay, which means a fresh
            # reviewer: strictly the behaviour that existed before.
            return
        if not sources:
            return
        try:
            with closing(_side_table_connection(self.db_path)) as conn, conn:
                conn.execute(REPLAY_PLAN_TABLE)
                for lens, plan in sorted(sources.items()):
                    conn.execute(
                        "INSERT OR IGNORE INTO review_orchestrator_replay_plan "
                        "(chain_id, lens, plan_json) VALUES (?, ?, ?)",
                        (
                            chain.chain_id,
                            lens,
                            json.dumps(plan, sort_keys=True, separators=(",", ":")),
                        ),
                    )
        except sqlite3.Error:
            return

    def _replay_plan(self, chain_id: int, lens: str) -> dict[str, Any]:
        """Return this chain's bound replay decision for one lens, or ``{}``."""
        if not lens:
            return {}
        try:
            with closing(_side_table_connection(self.db_path)) as conn:
                conn.execute(REPLAY_PLAN_TABLE)
                row = conn.execute(
                    "SELECT plan_json FROM review_orchestrator_replay_plan "
                    "WHERE chain_id=? AND lens=?",
                    (int(chain_id), str(lens)),
                ).fetchone()
        except sqlite3.Error:
            return {}
        if row is None:
            return {}
        try:
            plan = json.loads(str(row[0] or "{}"))
        except (TypeError, ValueError):
            return {}
        return plan if isinstance(plan, dict) else {}

    def _bind_lens_plan(
        self,
        chain: review_lifecycle.ReviewChain,
        required_reviewer_lenses: Any,
        effective_tier: str,
    ) -> None:
        """Bind the tier's lens set, falling back to the candidate's own record."""
        planned = _normalize_lenses(required_reviewer_lenses)
        source = "registration"
        if required_reviewer_lenses is None:
            planned, effective_tier, source = self._lens_plan_from_target(chain)
        try:
            bind_lens_plan(
                self.db_path, chain_id=chain.chain_id, lenses=planned,
                effective_tier=effective_tier, source=source,
            )
        except sqlite3.Error:
            # An unbound plan reads as "every lens": more review than the tier
            # asks for, never less. Losing the plan must not lose the review.
            return

    def _lens_plan_from_target(
        self, chain: review_lifecycle.ReviewChain
    ) -> tuple[tuple[str, ...], str, str]:
        """Recover the tier's lens set from the finalizer's own gate record."""
        try:
            status = self.manager.status(str(chain.chain_identity["target_request_id"]))
        except Exception:  # noqa: BLE001 -- an unreadable target plans every lens
            return LENSES, "", "target_unreadable_defaults_to_every_lens"
        card = status.get("task_card") if isinstance(status, Mapping) else None
        terminal = card.get("terminal_review") if isinstance(card, Mapping) else None
        evidence = terminal.get("evidence") if isinstance(terminal, Mapping) else None
        gate = evidence.get("quality_gate") if isinstance(evidence, Mapping) else None
        profile = gate.get("review_risk_profile") if isinstance(gate, Mapping) else None
        if not isinstance(profile, Mapping) or str(profile.get("error") or ""):
            return LENSES, "", "gate_profile_absent_defaults_to_every_lens"
        return (
            _normalize_lenses(profile.get("required_reviewer_lenses")),
            str(profile.get("effective_tier") or ""),
            "terminal_review_quality_gate",
        )

    def _bind_expected_workspace(self, chain: review_lifecycle.ReviewChain) -> None:
        """Persist the original workspace identity outside immutable lifecycle rows."""
        identity = chain.chain_identity
        expected = ""
        try:
            status = self.manager.status(str(identity["target_request_id"]))
            resolved = resolve_target_identity(status)
            bound = {field: str(identity[field]) for field in TARGET_IDENTITY_FIELDS}
            if not resolved["conflict"] and resolved["identity"] == bound:
                expected = str(resolved["workspace_identity"] or "")
        except Exception:
            pass
        self._repair_expected_workspace(chain.chain_id, expected)

    def _repair_expected_workspace(self, chain_id: int, workspace_identity: str) -> None:
        """Bind a later verified workspace only while the retained binding is empty."""
        with closing(_side_table_connection(self.db_path)) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS review_orchestrator_workspace_bindings "
                "(chain_id INTEGER PRIMARY KEY, workspace_identity TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO review_orchestrator_workspace_bindings "
                "(chain_id, workspace_identity) VALUES (?, ?)",
                (chain_id, workspace_identity),
            )
            if workspace_identity:
                conn.execute(
                    "UPDATE review_orchestrator_workspace_bindings "
                    "SET workspace_identity=? WHERE chain_id=? AND workspace_identity=''",
                    (workspace_identity, chain_id),
                )

    def _expected_workspace_identity(self, chain_id: int) -> str:
        with closing(_side_table_connection(self.db_path)) as conn:
            row = conn.execute(
                "SELECT workspace_identity FROM review_orchestrator_workspace_bindings "
                "WHERE chain_id=?",
                (chain_id,),
            ).fetchone()
        return str(row[0]) if row is not None else ""

    def drain(
        self, *, max_actions: int = DEFAULT_DRAIN_MAX_ACTIONS, now: datetime | None = None
    ) -> DrainResult:
        """Claim and execute a bounded number of lifecycle effects exactly once."""
        # One routing catalog per pass, not per action. This pass runs up to 12
        # actions and each launch would otherwise rebuild it at +1.49s measured;
        # the reset also keeps a pass from ever ranking on a previous pass's
        # ledger, so evidence can go stale within a pass but never across one.
        reset_routing_catalog_cache()
        instant = now or datetime.now(timezone.utc)
        review_lifecycle.reconcile_dead_chains(self.db_path, now=instant)
        attempted = completed = failed = pending = 0
        # One deferred-wait event per pass, not one per deferred action. A pass
        # that now looks at up to 12 actions passes over many chains still
        # waiting on the same thing; recording each of them would grow the
        # event ledger by the size of the backlog on every reconcile.
        deferred_recorded = False
        # A deferred action goes straight back to ``pending``, and the pending
        # cursor wraps to the start of its round once a window is exhausted, so
        # without this the same waiting action is re-reserved -- and re-asks the
        # manager for the same status -- several times in one pass. Seeing it
        # twice means the reservable set is exhausted; stop.
        seen_actions: set[int] = set()
        for _ in range(max(0, min(int(max_actions), DEFAULT_DRAIN_MAX_ACTIONS))):
            token = uuid.uuid4().hex
            action = review_lifecycle.reserve_next_action(
                self.db_path,
                owner=self.owner,
                lease_token=token,
                now=instant,
                lease_seconds=self.lease_seconds,
            )
            if action is None:
                break
            if action.action_id in seen_actions:
                review_lifecycle.defer_action(
                    self.db_path, action_id=action.action_id, owner=self.owner,
                    lease_token=token, now=instant,
                )
                break
            seen_actions.add(action.action_id)
            attempted += 1
            try:
                receipt = self._execute(action)
                if receipt is None:
                    review_lifecycle.defer_action(
                        self.db_path, action_id=action.action_id, owner=self.owner,
                        lease_token=token, now=instant,
                    )
                    pending += 1
                    # Deliberately NOT a break. One chain waiting on a running
                    # reviewer used to end the whole pass, so a single waiting
                    # candidate starved every other chain in the outbox --
                    # which, with one action per pass, meant the outbox never
                    # moved at all. The reservation cursor advances on every
                    # reserve, so continuing cannot re-reserve this same row
                    # within this pass.
                    continue
                self._validate_receipt(action, receipt)
            except _DeferredLaunch as deferred:
                if not deferred_recorded:
                    self._record_deferred_wait(action, deferred, instant)
                    deferred_recorded = True
                review_lifecycle.defer_action(
                    self.db_path, action_id=action.action_id, owner=self.owner,
                    lease_token=token, now=instant,
                )
                pending += 1
                continue
            except Exception as exc:  # fail closed and stop this chain/pass
                review_lifecycle.fail_action(
                    self.db_path,
                    action_id=action.action_id,
                    owner=self.owner,
                    lease_token=token,
                    reason=f"{type(exc).__name__}:{exc}",
                    now=instant,
                )
                failed += 1
                break
            # Deliberately outside the effect-error handler: a crash or store
            # fault here leaves the lease reclaimable and never converts a
            # successful external effect into a terminal action failure.
            review_lifecycle.complete_action(
                self.db_path,
                action_id=action.action_id,
                owner=self.owner,
                lease_token=token,
                receipt=receipt,
                now=instant,
            )
            completed += 1
        return DrainResult(
            attempted, completed, failed, pending,
            review_lifecycle.lifecycle_counts(self.db_path),
        )

    def _execute(self, action: review_lifecycle.ReviewAction) -> dict[str, Any] | None:
        identity = action.descriptor["chain_identity"]
        target_task = str(identity["target_task_id"])
        target_request = str(identity["target_request_id"])
        reviewer_task = self._reviewer_task_id(identity, action.lens)
        prior = self._receipts(action.chain_id)
        if action.action_type in REVIEW_DRIVING_ACTIONS:
            decided = self._target_left_review(target_task)
            if decided:
                # Retiring an obsolete action is a real terminal outcome, not a
                # failure: there was nothing left to do. Failing it instead
                # parked the rest of its chain forever.
                return self._receipt(
                    action,
                    obsolete_reason=f"target_left_review:{decided}",
                    result={"ok": True, "state": "obsolete", "task_id": target_task},
                )
            superseded = self._target_request_superseded(identity)
            if superseded:
                # The task is still IN review, but not for these bytes: a newer
                # request or claim episode replaced the candidate this chain is
                # bound to. Driving it would spend a reviewer on bytes nobody
                # can accept. Measured over the live store's 627 chains, 332 are
                # in exactly this state, and 315 of the 581 failed launch
                # actions belong to them -- every one parked the rest of its
                # chain permanently.
                return self._receipt(
                    action,
                    obsolete_reason=superseded,
                    result={"ok": True, "state": "obsolete", "task_id": target_task},
                )
        if action.lens and action.action_type in {"launch", "accept", "archive"}:
            planned = required_lenses(self.db_path, action.chain_id)
            if action.lens not in planned:
                plan = lens_plan_record(self.db_path, action.chain_id)
                return self._receipt(
                    action,
                    obsolete_reason=(
                        "lens_not_required_by_tier:"
                        + str(plan.get("effective_tier") or "unknown")
                    ),
                    lens_plan=plan,
                    result={"ok": True, "state": "obsolete", "task_id": target_task},
                )
        if action.action_type == "launch":
            readiness = self._launch_readiness(action)
            if readiness["outcome"] == "deferred":
                raise _DeferredLaunch(str(readiness["reason"]), readiness)
            if readiness["outcome"] == "obsolete":
                # A superseded candidate is not a failed one. Failing it here
                # parked every later action in its chain; completing it as
                # obsolete lets the chain walk to its own end.
                return self._receipt(
                    action,
                    obsolete_reason=str(readiness["reason"]),
                    target_readiness_receipt=readiness,
                    result={"ok": True, "state": "obsolete", "task_id": target_task},
                )
            if readiness["outcome"] == "mechanical_rework":
                # The candidate is already measurably failing, so no reviewer
                # is spent on it. Return it to `pending` for rework, then fail
                # this action carrying BOTH the mechanical reason and what the
                # rejection actually did. The rejection is total: a refused one
                # simply leaves the card in `review` for the manager, and the
                # reason is still durable in two independent places -- the
                # target's event stream and the outbox row. Neither outcome
                # leaves a state transition unexplained, and neither can drop
                # a card on a rejection that did not happen.
                rejection = self._return_target_for_rework(action, readiness)
                self._record_mechanical_rework(action, readiness, rejection)
                detail = str(rejection.get("detail") or "")
                raise RuntimeError(
                    str(readiness["reason"])
                    + ":returned_for_rework=" + str(rejection["state"])
                    + ((":" + detail) if detail else "")
                )
            if readiness["outcome"] == "terminal":
                raise RuntimeError(str(readiness["reason"]))
            if readiness["outcome"] != "ready":
                # Fail closed on a vocabulary this branch does not know. An
                # unhandled outcome must never fall through into a launch.
                raise RuntimeError("launch_readiness_outcome_unknown:"
                                   + str(readiness["outcome"]))
            replay = self._replay_plan(action.chain_id, action.lens)
            if replay:
                # HASH-KEYED REPLAY. The bytes and the contract are identical to
                # a candidate this lens already reported on, and that report was
                # already INGESTED (its accept action completed, which only
                # happens after the sealed receipt authenticated). Nothing about
                # the judgment is re-derived and nothing about it is re-signed:
                # the completed action names the source chain and the original
                # reviewer, and the report itself is still the original
                # reviewer's HMAC-authenticated receipt on the original
                # reviewer's own card, resolved at accept time.
                return self._receipt(
                    action,
                    reviewer_task_id=str(replay.get("reviewer_task_id") or ""),
                    reviewer_request_id=str(replay.get("reviewer_request_id") or ""),
                    reviewer_route=replay.get("reviewer_route") or {},
                    replayed_from_chain=int(replay.get("source_chain_id") or 0),
                    replay={
                        "source_chain_id": int(replay.get("source_chain_id") or 0),
                        "source_target_request_id": str(
                            replay.get("source_target_request_id") or ""
                        ),
                        "source_claim_epoch": str(replay.get("source_claim_epoch") or ""),
                        # BOTH packet digests: the manifest digest of the
                        # candidate this chain is bound to, and the manifest
                        # digest of the chain the report came from. They are
                        # equal only when the two chains really are the same
                        # bytes, and both are on the record either way.
                        "packet_sha256": str(identity["packet_sha256"]),
                        "source_packet_sha256": str(
                            replay.get("source_packet_sha256") or ""
                        ),
                        "candidate_sha256": str(replay.get("candidate_sha256") or ""),
                        "contract_identity_sha256": str(
                            replay.get("contract_identity_sha256") or ""
                        ),
                    },
                    target_readiness_receipt=readiness,
                    result={"ok": True, "state": "replayed", "task_id": target_task},
                )
            route = dict(self.route_selector(self.manager.repo, reviewer_task, action.lens))
            runner = str(route.get("runner") or "")
            adapter_id = str(route.get("adapter_id") or "")
            model = str(route.get("model") or "")
            if not runner or not adapter_id or not model or runner == "codex":
                raise RuntimeError("review_route_identity_invalid")
            result = self.manager.launch_quality_reviewer(
                target_request_id=target_request,
                target_task_id=target_task,
                reviewer_task_id=reviewer_task,
                runner=runner,
                adapter_id=adapter_id,
                model=model,
                lens=action.lens,
            )
            self._require_ok(result, "reviewer_launch_failed")
            request_id = str(result.get("request_id") or "")
            if not request_id or str(result.get("task_id") or reviewer_task) != reviewer_task:
                raise RuntimeError("reviewer_launch_identity_invalid")
            return self._receipt(
                action, reviewer_task_id=reviewer_task,
                reviewer_request_id=request_id, reviewer_route=route,
                target_readiness_receipt=readiness, result=result,
            )
        if action.action_type == "accept":
            launch = self._lens_receipt(prior, action.lens, "launch")
            replay = launch.get("replay") if isinstance(launch, Mapping) else None
            replay = replay if isinstance(replay, Mapping) else {}
            reviewer_request = str(launch["reviewer_request_id"])
            status = self.manager.status(reviewer_request)
            if str(status.get("state") or "") in {
                "starting", "running", "processing", "finalizing", "reconcile_pending"
            }:
                return None
            if replay:
                # The replayed report is resolved from the ORIGINAL reviewer's
                # own card, against the SOURCE chain's target identity, through
                # the identical verifier a fresh report goes through. Its
                # provider, its packet digest, its submission counters and its
                # authenticated receipt are the original reviewer's -- nothing
                # here mints a new one, and the independence rung stays
                # resolvable from that reviewer's own provider identity.
                source_identity = {
                    "target_task_id": str(identity["target_task_id"]),
                    "target_request_id": str(
                        replay.get("source_target_request_id") or ""
                    ),
                    "claim_epoch": str(replay.get("source_claim_epoch") or ""),
                }
                receipt = self._review_receipt(
                    action,
                    status,
                    reviewer_request,
                    str(launch.get("reviewer_task_id") or ""),
                    source_identity,
                )
                findings = receipt["report"]["findings"]
                if any(
                    finding.get("actionable") is True
                    or finding.get("disposition") == "defect"
                    for finding in findings
                ):
                    raise RuntimeError("reviewer_actionable_findings")
                # No second acceptance of one report: the source chain already
                # accepted this reviewer task, and accepting it again would put
                # two acceptances behind one piece of work.
                return self._receipt(
                    action,
                    reviewer_task_id=str(launch.get("reviewer_task_id") or ""),
                    reviewer_request_id=reviewer_request,
                    replayed_from_chain=int(replay.get("source_chain_id") or 0),
                    replay=dict(replay),
                    reviewer_provider=str(status.get("adapter_id") or ""),
                    result={"ok": True, "state": "replayed", "task_id": reviewer_task},
                )
            receipt = self._review_receipt(
                action, status, reviewer_request, reviewer_task
            )
            findings = receipt["report"]["findings"]
            if any(
                finding.get("actionable") is True
                or finding.get("disposition") == "defect"
                for finding in findings
            ):
                raise RuntimeError("reviewer_actionable_findings")
            result = self.manager.accept_review(reviewer_request, reviewer_task)
            self._require_ok(result, "reviewer_accept_failed")
            return self._receipt(action, reviewer_task_id=reviewer_task,
                                 reviewer_request_id=reviewer_request, result=result)
        if action.action_type == "archive":
            accepted = self._lens_receipt(prior, action.lens, "accept")
            launch = self._lens_receipt(prior, action.lens, "launch")
            if isinstance(launch, Mapping) and launch.get("replay"):
                # The reviewer task belongs to the source chain, which archived
                # it. Archiving it again from here would be a second terminal
                # disposition of one card.
                return self._receipt(
                    action,
                    reviewer_task_id=str(launch.get("reviewer_task_id") or ""),
                    reviewer_request_id=str(accepted.get("reviewer_request_id") or ""),
                    replayed_from_chain=int(
                        (launch.get("replay") or {}).get("source_chain_id") or 0
                    ),
                    result={"ok": True, "state": "replayed", "task_id": target_task},
                )
            result = task_engine.archive_task(
                self.manager.repo, reviewer_task,
                actor=str((launch.get("reviewer_route") or {}).get("runner") or "system"),
                reason=f"automatic review accepted:{target_request}",
            )
            if result.get("ok") is not True and not self._is_archived(reviewer_task):
                self._require_ok(result, "reviewer_archive_failed")
            return self._receipt(
                action, reviewer_task_id=reviewer_task,
                reviewer_request_id=str(accepted["reviewer_request_id"]), result=result,
            )
        # Computed per branch, not up front: only the two target actions carry
        # reviewer identities into their effect. needfix_close never used them,
        # and hoisting the lookup made it fail with KeyError on a chain whose
        # reviewer actions had been retired as obsolete -- bookkeeping dying of
        # a dependency it did not have.
        def _reviewer_ids() -> list[str]:
            ids: list[str] = []
            for lens in required_lenses(self.db_path, action.chain_id):
                receipt = self._lens_receipt(prior, lens, "accept")
                reviewer_request_id = str(receipt.get("reviewer_request_id") or "")
                if reviewer_request_id:
                    ids.append(reviewer_request_id)
            return ids

        if action.action_type == "target_accept":
            if not AUTOMATIC_TARGET_ACCEPT_ENABLED:
                # See AUTOMATIC_TARGET_ACCEPT_ENABLED. Failing here is the
                # point: the chain parks with an explicit reason instead of
                # accepting a candidate no verified manager decided on.
                raise RuntimeError("target_accept_requires_verified_manager")
            reviewer_ids = _reviewer_ids()
            result = self.manager.accept_review(
                target_request, target_task, reviewer_request_ids=reviewer_ids
            )
            self._require_ok(result, "target_accept_failed")
            return self._receipt(action, reviewer_request_ids=reviewer_ids, result=result)
        if action.action_type == "target_archive":
            reviewer_ids = _reviewer_ids()
            result = task_engine.archive_task(
                self.manager.repo, target_task, actor="system",
                reason=f"automatic review chain complete:{target_request}",
            )
            if result.get("ok") is not True and not self._is_archived(target_task):
                self._require_ok(result, "target_archive_failed")
            return self._receipt(action, reviewer_request_ids=reviewer_ids, result=result)
        if action.action_type == "needfix_close":
            linked = self._linked_needfix_rows(target_task)
            newly_resolved: list[str] = []
            for row in linked:
                if row["status"] != "task_created":
                    continue
                needfix_id = str(row["id"])
                resolved = needfix_store.resolve_needfix(
                    self.manager.repo,
                    needfix_id,
                    resolution_note=(
                        "automatic review lifecycle accepted and archived task "
                        + target_task
                    ),
                )
                if (
                    resolved.get("id") != needfix_id
                    or resolved.get("status") != "resolved"
                    or resolved.get("converted_task_id") != target_task
                ):
                    raise RuntimeError("needfix_close_receipt_invalid")
                newly_resolved.append(needfix_id)
            return self._receipt(
                action,
                needfix_ids=sorted(str(row["id"]) for row in linked),
                needfix_newly_resolved=sorted(newly_resolved),
                needfix_closed_count=len(linked),
                result={"ok": True, "state": "resolved"},
            )
        raise RuntimeError("unknown_review_action")

    def _launch_readiness(self, action: review_lifecycle.ReviewAction) -> dict[str, Any]:
        """Bind one launch evaluation to the retained canonical target envelope."""
        identity = action.descriptor["chain_identity"]
        target_request = str(identity["target_request_id"])
        target_task = str(identity["target_task_id"])
        try:
            status = self.manager.status(target_request)
        except Exception as exc:
            return self._readiness_receipt(
                action, "deferred", "target_status_unavailable:" + type(exc).__name__
            )
        if not isinstance(status, Mapping) or status.get("ok") is not True:
            return self._readiness_receipt(action, "deferred", "target_status_unavailable")
        card = status.get("task_card")
        if not isinstance(card, Mapping):
            return self._readiness_receipt(action, "deferred", "target_card_missing")
        # The identity is read from where ``_finalize_isolated_request`` writes
        # it -- ``terminal_review.evidence`` and the ``review_automation``
        # registration payload -- not from top-level card keys, which nothing
        # has ever written. Measured over this repository's 627 chains: the old
        # read resolved 0 of them and the new read resolves 93 exactly, with
        # 332 correctly classified as superseded and retired rather than failed
        # and 202 unreadable (199 of which the target has already left review,
        # so they retire one check earlier as ``target_left_review``).
        resolved = resolve_target_identity(status)
        if resolved["conflict"]:
            return self._readiness_receipt(
                action, "terminal", "target_identity_conflict:" + str(resolved["conflict"])
            )
        observed = resolved["identity"]
        if not observed:
            return self._readiness_receipt(action, "deferred", "target_identity_unavailable")
        identity_source = str(resolved["identity_source"])
        if observed["target_task_id"] != target_task:
            return self._readiness_receipt(action, "terminal", "target_task_identity_invalid")
        if observed["target_request_id"] != target_request:
            return self._readiness_receipt(
                action, "obsolete",
                "target_request_superseded:" + observed["target_request_id"],
            )
        if observed["claim_epoch"] != str(identity["claim_epoch"]):
            return self._readiness_receipt(
                action, "obsolete",
                "target_claim_epoch_superseded:" + observed["claim_epoch"],
            )
        if observed["packet_sha256"] != str(identity["packet_sha256"]):
            return self._readiness_receipt(action, "terminal", "target_packet_identity_invalid")
        if observed["candidate_sha256"] != str(identity["candidate_sha256"]):
            return self._readiness_receipt(action, "terminal", "target_candidate_identity_invalid")
        process_review_ready = str(status.get("state") or "") == "review_ready"
        card_review_ready = (
            str(card.get("status") or "") == "review"
            and str(card.get("worker_status") or "") == "review"
            and str(card.get("terminal_substatus") or "") == "review_ready"
        )
        if not process_review_ready and not card_review_ready:
            return self._readiness_receipt(action, "deferred", "target_not_review_ready")
        # Every immutable identity above has matched and the card is genuinely
        # at review_ready, so its deterministic verdict is about THIS claim of
        # THIS candidate. Read it before spending a reviewer launch. Placed
        # ahead of the workspace/partition gates deliberately: those exist to
        # get a REVIEWER started, and a mechanically failing candidate is not
        # going to be reviewed, so waiting on a Source Graph partition it will
        # never use would defer it forever instead of returning it for rework.
        mechanical = mechanical_failure_reason(card, str(identity["claim_epoch"]))
        if mechanical:
            return self._readiness_receipt(action, "mechanical_rework", mechanical)
        workspace = str(resolved["workspace_identity"] or "")
        if not workspace:
            return self._readiness_receipt(action, "deferred", "target_workspace_identity_missing")
        expected_workspace = self._expected_workspace_identity(action.chain_id)
        if not expected_workspace:
            # Chain registration may predate canonical target availability.
            # Bind only after all immutable identities above have matched.
            self._repair_expected_workspace(action.chain_id, workspace)
            expected_workspace = self._expected_workspace_identity(action.chain_id)
            if not expected_workspace:
                return self._readiness_receipt(
                    action, "terminal", "target_workspace_identity_unbound"
                )
        if workspace != expected_workspace:
            return self._readiness_receipt(
                action, "terminal", "target_workspace_identity_invalid"
            )
        evidence = card.get("evidence")
        partitions = (
            evidence.get("source_graph_partition_readiness")
            if isinstance(evidence, Mapping)
            else None
        )
        if not isinstance(partitions, Mapping) or not partitions:
            return self._readiness_receipt(action, "deferred", "source_graph_partition_empty")
        if any(value is not True for value in partitions.values()):
            return self._readiness_receipt(action, "deferred", "source_graph_partition_not_ready")
        return self._readiness_receipt(
            action, "ready", "ready", workspace, partitions,
            identity_source=identity_source,
        )

    def _target_request_superseded(self, identity: Mapping[str, Any]) -> str:
        """Name the newer request/claim that replaced these bytes, or "".

        Fail closed on every unreadable answer: an unresolvable target is not a
        superseded one, and retiring a chain on a read failure would silently
        drop a review that is still owed.
        """
        try:
            status = self.manager.status(str(identity["target_request_id"]))
        except Exception:  # noqa: BLE001 -- unreadable is not superseded
            return ""
        resolved = resolve_target_identity(status)
        observed = resolved["identity"]
        if resolved["conflict"] or not observed:
            return ""
        if observed["target_task_id"] != str(identity["target_task_id"]):
            return ""
        if observed["target_request_id"] != str(identity["target_request_id"]):
            return "target_request_superseded:" + observed["target_request_id"]
        if observed["claim_epoch"] != str(identity["claim_epoch"]):
            return "target_claim_epoch_superseded:" + observed["claim_epoch"]
        return ""

    @staticmethod
    def _readiness_receipt(
        action: review_lifecycle.ReviewAction,
        outcome: str,
        reason: str,
        workspace_identity: str = "",
        partitions: Mapping[str, Any] | None = None,
        identity_source: str = "",
    ) -> dict[str, Any]:
        identity = action.descriptor["chain_identity"]
        return {
            "schema_id": "aiworkhub.review_target_readiness_receipt.v1",
            "action_id": action.action_id,
            "target_task_id": identity["target_task_id"],
            "target_request_id": identity["target_request_id"],
            "claim_epoch": identity["claim_epoch"],
            "packet_sha256": identity["packet_sha256"],
            "candidate_sha256": identity["candidate_sha256"],
            "workspace_identity": workspace_identity,
            "identity_source": identity_source,
            "partition_readiness": dict(partitions or {}),
            "outcome": outcome,
            "reason": reason,
        }

    def _return_target_for_rework(
        self, action: review_lifecycle.ReviewAction, readiness: Mapping[str, Any]
    ) -> dict[str, str]:
        """Return a mechanically failing target to ``pending`` for rework.

        Total by construction: a manager with no reject surface, a refused
        rejection and a raising one all resolve to a named outcome rather than
        an exception. The caller fails this action either way, so the only
        result that must never occur is a card that left ``review`` with
        nothing saying why -- and a refusal cannot produce one, because it
        leaves the card exactly where it was, in ``review``, for the manager
        to dispose of. That is the safe side of this decision: a rejection
        that did not happen costs one reviewer launch, while a rejection
        wrongly believed to have happened loses a completed review.
        """
        identity = action.descriptor["chain_identity"]
        target_task = str(identity["target_task_id"])
        reject = getattr(self.manager, "reject_review", None)
        if not callable(reject):
            return {"state": "unavailable", "detail": "manager_has_no_reject_review"}
        try:
            result = reject(target_task, str(readiness["reason"]), to="pending")
        except Exception as exc:  # noqa: BLE001 -- a refusal, never a crash
            return {"state": "error", "detail": f"{type(exc).__name__}:{exc}"[:160]}
        if not isinstance(result, Mapping):
            return {"state": "refused", "detail": "reject_result_not_a_mapping"}
        if result.get("ok") is not True:
            detail = str(result.get("error") or result.get("stderr") or "unknown")
            return {"state": "refused", "detail": detail[:160]}
        return {"state": "returned", "detail": "pending"}

    def _record_mechanical_rework(
        self,
        action: review_lifecycle.ReviewAction,
        readiness: Mapping[str, Any],
        rejection: Mapping[str, str] | None = None,
    ) -> None:
        """Record on the target why no reviewer was spent on this candidate.

        The action itself is failed with the same reason immediately after, so
        the mechanical verdict is durable in two independent places: the
        target's event stream and the review outbox row. Best-effort by
        design -- a manager without an event surface must not turn a correct
        short-circuit into a crash, and the outbox reason still explains it.
        A manager whose event surface *raises* must not either: letting that
        escape would replace the mechanical reason on the outbox row with an
        append error, which is exactly the unexplained transition this method
        exists to prevent.
        """
        append = getattr(self.manager, "_append_event", None)
        if not callable(append):
            return
        identity = action.descriptor["chain_identity"]
        try:
            append({
                "event_type": "review_orchestrator_mechanical_rework",
                "request_id": identity["target_request_id"],
                "task_id": identity["target_task_id"],
                "review_automation": {
                    "state": "mechanical_rework",
                    "reason": str(readiness["reason"]),
                    "action_id": action.action_id,
                    "lens": action.lens,
                    "reviewer_launched": False,
                    "disposition": "return_for_rework",
                    "rework_return": dict(rejection or {}),
                    "readiness_receipt": dict(readiness),
                },
            })
        except Exception:  # noqa: BLE001 -- the outbox row still carries the reason
            return

    def _record_deferred_wait(
        self, action: review_lifecycle.ReviewAction, deferred: _DeferredLaunch, now: datetime
    ) -> None:
        append = getattr(self.manager, "_append_event", None)
        if not callable(append):
            return
        identity = action.descriptor["chain_identity"]
        append({
            "event_type": "review_orchestrator_wait",
            "request_id": identity["target_request_id"],
            "task_id": identity["target_task_id"],
            "review_automation": {
                "state": "deferred",
                "reason": deferred.reason,
                "action_id": action.action_id,
                "retry_after_seconds": 60,
                "recorded_at": now.isoformat(),
                "readiness_receipt": deferred.receipt,
            },
        })

    def _linked_needfix_rows(self, target_task_id: str) -> list[dict[str, Any]]:
        """Return every durable NeedFix row already bound to this exact task."""
        linked: list[dict[str, Any]] = []
        for status in ("task_created", "resolved"):
            offset = 0
            while True:
                page = needfix_store.list_needfix(
                    self.manager.repo,
                    status=status,
                    include_archived=True,
                    limit=500,
                    offset=offset,
                    order_by="created_at",
                    order_dir="ASC",
                )
                linked.extend(
                    row
                    for row in page
                    if str(row.get("converted_task_id") or "") == target_task_id
                )
                if len(page) < 500:
                    break
                offset += len(page)
        return linked

    def _receipt(self, action: review_lifecycle.ReviewAction, **payload: Any) -> dict[str, Any]:
        result = payload.pop("result", {})
        bounded_result = {
            key: result[key] for key in ("ok", "already_accepted", "already_reserved",
                                         "request_id", "task_id", "state") if key in result
        }
        return {
            "schema_id": RECEIPT_SCHEMA,
            "action_id": action.action_id,
            "action_index": action.action_index,
            "action_type": action.action_type,
            "lens": action.lens,
            "descriptor_sha256": action.descriptor_sha256,
            "target_task_id": action.descriptor["target_task_id"],
            "target_request_id": action.descriptor["target_request_id"],
            **payload,
            "result": bounded_result,
        }

    def _validate_receipt(self, action: review_lifecycle.ReviewAction,
                          receipt: Mapping[str, Any]) -> None:
        if (
            receipt.get("schema_id") != RECEIPT_SCHEMA
            or receipt.get("action_id") != action.action_id
            or receipt.get("action_index") != action.action_index
            or receipt.get("action_type") != action.action_type
            or receipt.get("lens") != action.lens
            or receipt.get("descriptor_sha256") != action.descriptor_sha256
            or receipt.get("target_task_id") != action.descriptor["target_task_id"]
            or receipt.get("target_request_id") != action.descriptor["target_request_id"]
        ):
            raise RuntimeError("external_receipt_binding_invalid")
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        if len(encoded.encode("utf-8")) > MAX_RECEIPT_BYTES:
            raise RuntimeError("external_receipt_too_large")

    def _receipts(self, chain_id: int) -> list[dict[str, Any]]:
        return list(review_lifecycle.completed_receipts_for_chain(self.db_path, chain_id))

    @staticmethod
    def _review_receipt(
        action: review_lifecycle.ReviewAction,
        status: Mapping[str, Any],
        reviewer_request: str,
        reviewer_task: str,
        identity: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status.get("ok") is not True or status.get("state") != "review_ready":
            raise RuntimeError("reviewer_terminal_receipt_missing")
        event = status.get("latest_event")
        card = status.get("task_card")
        if not isinstance(event, Mapping) or not isinstance(card, Mapping):
            raise RuntimeError("reviewer_terminal_receipt_missing")
        terminal = card.get("terminal_review")
        evidence = terminal.get("evidence") if isinstance(terminal, Mapping) else None
        event_receipt = event.get("quality_review_receipt")
        card_receipt = evidence.get("quality_review_receipt") if isinstance(evidence, Mapping) else None
        if not isinstance(event_receipt, Mapping) or event_receipt != card_receipt:
            raise RuntimeError("reviewer_terminal_receipt_mismatch")
        receipt = json.loads(json.dumps(event_receipt, ensure_ascii=False))
        target, reviewer, report, authority = (
            receipt.get("target"), receipt.get("reviewer"),
            receipt.get("report"), receipt.get("authority"),
        )
        # A replayed report was written against the SOURCE chain's target
        # request, not this one, so the caller supplies that identity. Every
        # other binding below -- the reviewer's own request/task, its provider,
        # the lens, the read-only authority, the sealed packet digest and the
        # submission counters -- is checked exactly as it is for a fresh run.
        identity = identity if identity is not None else action.descriptor["chain_identity"]
        if not all(isinstance(value, dict) for value in (target, reviewer, report, authority)):
            raise RuntimeError("reviewer_receipt_shape_invalid")
        # TWO DIGESTS OF TWO DIFFERENT OBJECTS.
        #
        # ``identity["packet_sha256"]`` is the ATTEMPT-ARTIFACT MANIFEST digest
        # (``candidate_registration`` sets it from
        # ``attempt_artifact_manifest.manifest_sha256``); ``receipt`` carries the
        # REVIEW-PACKET digest that ``quality_reviewer.verify_reviewer_receipt``
        # recomputes over the packet body. Comparing them could never be equal:
        # measured over the live store, 0 of 627 chain packet digests appear
        # among the 958 distinct receipt digests, and across the 103 receipts
        # whose target request owns a chain, 0 matched and 103 differed. The
        # branch was reachable only in tests, which put ``"a" * 64`` on both
        # sides.
        #
        # The receipt's true counterpart is on the reviewer's own card:
        # ``terminal_review.evidence.quality_review.packet_sha256`` is the digest
        # of the packet THIS repository sealed and handed to THIS reviewer. That
        # comparison is a real one -- it holds in 2,219 of 2,219 stored reviewer
        # cards, with the lens matching in all 2,219 -- and it proves what the
        # broken one meant to: the report was written against the packet the
        # chain's own launch produced, for this lens, bound to this target.
        binding = evidence.get("quality_review") if isinstance(evidence, Mapping) else None
        if not isinstance(binding, Mapping):
            raise RuntimeError("reviewer_packet_binding_missing")
        review_packet_sha256 = str(binding.get("packet_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", review_packet_sha256):
            raise RuntimeError("reviewer_packet_binding_invalid")
        if (
            str(binding.get("target_request_id") or "") != identity["target_request_id"]
            or str(binding.get("target_task_id") or "") != identity["target_task_id"]
            or str(binding.get("target_claim_epoch") or "") != identity["claim_epoch"]
            or str(binding.get("lens") or "") != action.lens
        ):
            raise RuntimeError("reviewer_packet_binding_invalid")
        if (
            receipt.get("packet_sha256") != review_packet_sha256
            or target.get("request_id") != identity["target_request_id"]
            or target.get("task_id") != identity["target_task_id"]
            or str(target.get("claim_epoch")) != identity["claim_epoch"]
            or reviewer.get("request_id") != reviewer_request
            or reviewer.get("task_id") != reviewer_task
            or reviewer.get("provider") != status.get("adapter_id")
            or report.get("provider") != status.get("adapter_id")
            or report.get("lens") != action.lens
            or report.get("read_only") is not True
            or report.get("can_mutate_repo") is not False
            or not isinstance(report.get("findings"), list)
            or authority != {
                "process_identity_verified": True,
                "audit_verified": True,
                "terminal_state": "review_ready",
            }
            or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("submission_id") or ""))
            or receipt.get("physical_submission_count") != 1
            or receipt.get("logical_submission_count") != 1
        ):
            raise RuntimeError("reviewer_receipt_binding_invalid")
        if not all(isinstance(finding, dict) for finding in report["findings"]):
            raise RuntimeError("reviewer_findings_invalid")
        return receipt

    @staticmethod
    def _lens_receipt(receipts: list[dict[str, Any]], lens: str,
                      action_type: str) -> dict[str, Any]:
        matches = [r for r in receipts if r.get("lens") == lens
                   and r.get("action_type") == action_type]
        if len(matches) != 1:
            raise RuntimeError("reviewer_receipt_missing_or_duplicate")
        return matches[0]

    @staticmethod
    def _require_ok(result: Mapping[str, Any], prefix: str) -> None:
        if result.get("ok") is not True:
            detail = result.get("error") or result.get("stderr") or "unknown"
            raise RuntimeError(prefix + ":" + str(detail))

    def _target_left_review(self, task_id: str) -> str:
        """Canonical status once the target is no longer a review surface.

        Returns "" when the target can still be driven through review AND when
        the card cannot be read at all -- an unreadable card must never cause
        an action to be retired, only a card that is readably decided.
        """
        try:
            status = task_engine.show_task(self.manager.repo, task_id)
            if status.get("returncode") != 0:
                return ""
            card = json.loads(str(status.get("stdout") or ""))
        except Exception:
            return ""
        if not isinstance(card, dict):
            return ""
        canonical = task_store.canonical_status(card)
        return "" if canonical in REVIEWABLE_TARGET_STATUSES else str(canonical)

    def _is_archived(self, task_id: str) -> bool:
        try:
            status = task_engine.show_task(self.manager.repo, task_id)
            if status.get("returncode") != 0:
                return False
            card = json.loads(str(status.get("stdout") or ""))
        except Exception:
            return False
        return bool(str(card.get("archived_at") or "").strip())

    @staticmethod
    def _reviewer_task_id(identity: Mapping[str, Any], lens: str) -> str:
        preimage = json.dumps({"identity": dict(identity), "lens": lens},
                              sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return "QUALITY_REVIEW_" + hashlib.sha256(preimage.encode()).hexdigest()[:24].upper()
