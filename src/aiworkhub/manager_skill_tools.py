"""Manager-bound driver for the persisted skill registry lifecycle.

``skill_registry`` owns a complete, tested lifecycle -- propose, add_evidence,
activate -- and ``skill_registry_store`` owns durable persistence for it, but no
production caller ever drove them together, so the lifecycle was reachable from
no tool and the skills panel stayed structurally empty. This module is that one
driver: three manager operations that each load the registry through the store,
perform the exact ``skill_registry`` call under an authenticated manager
authority, and persist the result back through the store's public API only.

It never infers or generates a skill field -- every content field of a proposal
is supplied by the caller, and evidence provenance is supplied by the caller and
bound by the registry. It never weakens ``min_accepted_evidence`` (activation
stays gated at the store's default of two independent accepted actors), never
exposes a worker-facing surface, and never touches a private attribute of the
store or the registry: persistence goes through :func:`skill_registry_store.put_record`
for a new proposal and :func:`skill_registry_store.advance_record` for an
in-place runtime advance of an existing ``(identity, version)``.

An evidence or activation advance is a load-modify-write, and these tools are
not the only writer of a repository's skills store, so each reads the loaded
record's compare-and-swap token via :func:`skill_registry_store.state_digest`
and passes it back to ``advance_record``. A stale advance is then refused rather
than allowed to silently overwrite a newer one, so two independent accepted
evidence entries can never collapse into one lost update.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from . import core
from . import skill_registry as sr
from . import skill_miner
from . import skill_registry_store as store
from . import task_store

# The non-secret provenance identity of the manager's own lifecycle authority
# (propose/activate). Must match the registry's actor-id shape
# (``[a-z][a-z0-9_.-]*``).
#
# :func:`add_evidence` takes its provenance from the CALLER, which does not by
# itself make two entries independent: every such entry ultimately names the one
# verified manager route, and once identities are compared canonically that is a
# single actor however it is spelled. Genuinely distinct provenance comes from
# :func:`add_task_evidence`, which reads the actor off a finished task card.
_MANAGER_ACTOR = "manager"


def _manager_context() -> tuple[Path | None, str, dict[str, Any]]:
    """Resolve the verified manager identity and repository, or an error payload.

    Returns ``(root, token, manager)``. On failure ``root`` is ``None`` and
    ``manager`` is the fail-closed error result to return verbatim. The manager
    session id doubles as the in-process manager gate token; it is used only for
    the registry's manager check and is never persisted.
    """
    route = core.manager_bootstrap()
    identity = route.get("manager_route") if isinstance(route, dict) else None
    if not isinstance(route, dict) or route.get("role") != "manager" or not isinstance(identity, dict):
        return None, "", {
            "ok": False,
            "error": "verified_manager_identity_required",
            "surface": "manager_mcp",
        }
    session_id = str(identity.get("thread_id") or identity.get("session_id") or "").strip()
    if not session_id:
        return None, "", {
            "ok": False,
            "error": "manager_session_identity_missing",
            "surface": "manager_mcp",
        }
    provider = str(identity.get("provider") or route.get("provider") or "manager").strip()
    root = Path(str(route.get("repo") or core.repo_root())).resolve()
    return root, session_id, {
        "provider": provider,
        "session_id": session_id,
        "repo": str(root),
    }

# Role prefixes for a card-derived provenance identity. They are emitted first
# and are the reason such an identity can never collide with the manager's own:
# ``skill_registry.canonical_actor_id`` orders the role token ahead of
# everything else, so ``worker.claude.sonnet.5`` and ``manager.claude.7e6e8a47``
# stay two actors under every spelling of either.
_TASK_EVIDENCE_ROLES = ("worker", "reviewer")
_REVIEW_TOPICS = frozenset({"quality_review", "review", "security_review"})

# Role tokens that may appear ONLY in a DERIVED provenance identity. A
# card-derived actor is read off the canonical task store's own ``runner``; a
# caller-typed one is a string. Before this, nothing stopped a manager typing
# ``worker.claude.sonnet.5`` into :func:`add_evidence` and manufacturing the
# second "independent" actor an activation needs -- the exact self-certification
# the two-actor floor exists to prevent, and the shape the one stored ACTIVE
# record already took (``manager.claude.7e6e8a47`` and ``claude_manager_7e6e8a47``
# were one manager under two spellings).
#
# Canonicalization fixed the accidental half of that. This fixes the deliberate
# half: the derived namespace is reserved, so the free-text surface can name only
# a manager, and manager entries all canonicalize to one actor.
_DERIVED_ONLY_ROLE_TOKENS = frozenset(sr.ACTOR_ROLE_TOKENS) - {"manager"}
_ACTOR_TOKEN_RE = re.compile(r"[._-]+")

# The card decision vocabulary this maps into ``EvidenceOutcome``. A skill that
# was injected into a card the manager ACCEPTED contributed to an accepted
# outcome; one injected into a card that was REJECTED is negative evidence about
# that skill on that card. Nothing else is a decision.
DECISION_EVIDENCE_OUTCOMES: dict[str, str] = {
    "accepted": sr.EvidenceOutcome.ACCEPTED.value,
    "rejected": sr.EvidenceOutcome.NEGATIVE.value,
}


def _caller_actor(actor_id: Any) -> str:
    """Validate a CALLER-TYPED provenance identity, refusing an impersonation.

    The registry's own charset check runs first (through the public
    :func:`skill_registry.canonical_actor_id`), then the reserved-role check.
    A refusal names the token, because the fix is always the same one: file the
    entry through the derived path that can actually prove that actor acted.
    """
    text = str(actor_id or "")
    sr.canonical_actor_id(text)  # raises SkillRegistryError on an invalid shape
    claimed = sorted(
        {token for token in _ACTOR_TOKEN_RE.split(text.lower()) if token}
        & _DERIVED_ONLY_ROLE_TOKENS
    )
    if claimed:
        raise sr.SkillRegistryError(
            "skill_registry.invalid_evidence",
            f"actor_id may not claim the reserved role token {claimed[0]!r}: a "
            f"{claimed[0]} identity is DERIVED from a task card's own runner, "
            "never typed; file it through add_task_evidence or the decision "
            "evidence recorded at accept/reject",
        )
    return text


def _actor_subject(raw: str) -> str:
    """Reduce a runner identity to the actor-id charset, dropping role tokens.

    Role words are stripped from the subject so exactly one role token survives
    in the finished identity. Without that, a runner literally named
    ``claude_manager_x`` would produce a two-role id that
    :func:`skill_registry.canonical_actor_id` refuses to parse.
    """
    lowered = "".join(
        char if char.isascii() and (char.isalnum()) else "." for char in str(raw).lower()
    )
    tokens = [
        token
        for token in lowered.split(".")
        if token and token not in sr.ACTOR_ROLE_TOKENS
    ]
    return ".".join(tokens)


def _task_actor(
    root: Path, task_id: str, *, require_finished: bool = True
) -> tuple[str, str, str]:
    """Derive ``(role, actor_id, source_anchor)`` from one finished task card.

    ``require_finished=False`` is for the decision path only. A rejection sends
    the card straight back into a new claim episode, which clears
    ``completed_at``; the runner that produced the judged candidate is still on
    the card and is still the actor whose work was adjudicated, so demanding a
    finish there would silently drop every rejection's evidence.

    Fails closed through :class:`skill_registry.SkillRegistryError` -- the same
    channel :func:`_invoke_write` already surfaces with a stable ``code`` -- when
    the card is unknown, unfinished, or carries no runner. Each refusal means the
    same thing: there is no actor here whose contribution can be recorded, and
    the manager may not substitute one.
    """
    bounded = str(task_id or "").strip()
    if not bounded or len(bounded) > 200:
        raise sr.SkillRegistryError(
            "skill_registry.invalid_evidence", "task_id must be a bounded non-empty string"
        )
    try:
        card = task_store.get_task(root, bounded)
    except Exception as exc:  # noqa: BLE001 -- any store failure is "no verified actor"
        raise sr.SkillRegistryError(
            "skill_registry.invalid_evidence",
            f"task card {bounded!r} could not be read: {type(exc).__name__}",
        ) from exc
    if not isinstance(card, dict):
        raise sr.SkillRegistryError(
            "skill_registry.invalid_evidence",
            f"task card {bounded!r} is not in the canonical task store",
        )
    if require_finished and not str(card.get("completed_at") or "").strip():
        raise sr.SkillRegistryError(
            "skill_registry.invalid_evidence",
            f"task {bounded!r} never finished; an unfinished card produces no evidence",
        )
    runner = str(card.get("runner") or card.get("claimed_by") or "").strip()
    subject = _actor_subject(runner)
    if not subject:
        raise sr.SkillRegistryError(
            "skill_registry.invalid_evidence",
            f"task {bounded!r} carries no runner identity to attribute evidence to",
        )
    topic = str(card.get("topic") or "").strip().lower()
    role = "reviewer" if topic in _REVIEW_TOPICS else "worker"
    actor_id = f"{role}.{subject}"[:128].rstrip(".")
    return role, actor_id, bounded[:128]


def _invoke_write(
    operation: Callable[[Path, str], sr.SkillRecord],
) -> dict[str, Any]:
    """Run one lifecycle operation behind the manager identity and write gate.

    ``operation`` performs the exact registry call and persists it, returning the
    resulting record. Registry and store rejections both fail closed, surfacing
    the registry's own stable ``code`` when it raised, and leave persistence
    untouched.
    """
    root, token, manager = _manager_context()
    if root is None:
        return manager
    if not core.writes_allowed():
        return {"ok": False, "error": "write_gate_closed", "surface": "manager_mcp", "manager": manager}
    try:
        record = operation(root, token)
    except sr.SkillRegistryError as exc:
        return {
            "ok": False,
            "error": str(exc)[:240],
            "reason_code": exc.code,
            "manager": manager,
            "surface": "manager_mcp",
        }
    except store.SkillStoreError as exc:
        return {"ok": False, "error": str(exc)[:240], "manager": manager, "surface": "manager_mcp"}
    except (OSError, sqlite3.Error) as exc:
        return {
            "ok": False,
            "error": f"skill_store_failed:{type(exc).__name__}",
            "manager": manager,
            "surface": "manager_mcp",
        }
    return {
        "ok": True,
        "identity": record.identity,
        "version": record.version,
        "digest": sr.skill_digest(record),
        "lifecycle_state": record.lifecycle_state.value,
        "manager": manager,
        "surface": "manager_mcp",
    }


# The dimensions ``skill_miner._proposal_draft`` derives from measured card
# evidence. When a candidate_id is given these are copied server-side and a
# conflicting caller value is REFUSED rather than silently preferred -- a
# mistyped identity would otherwise propose a different skill than the one the
# evidence supports, under a name that looks mined.
_CANDIDATE_DERIVED_FIELDS = ("identity", "version", "scope", "path_or_symbol", "risk", "stage")


def propose(
    *,
    identity: str = "",
    version: str = "",
    scope: str = "",
    task_family: str = "",
    path_or_symbol: str = "",
    risk: str = "",
    stage: str = "",
    triggers: list[str] | None = None,
    confidence: float = 0.0,
    applicability: list[str] | None = None,
    procedure_steps: list[str] | None = None,
    avoid_rules: list[str] | None = None,
    preferred_tools: list[str] | None = None,
    candidate_id: str = "",
) -> dict[str, Any]:
    """MANAGER WRITE: register and persist one proposed skill.

    Two ways in, and both leave every JUDGEMENT field with the caller:

    * Full form -- every field is taken verbatim from the caller.
    * ``candidate_id`` from ``aiworkhub_manager_skill_mine`` -- the mechanical
      dimensions (identity, version, scope, path_or_symbol, risk, stage) are
      copied server-side from that candidate's own measured draft, and the
      caller supplies only what no measurement can: ``task_family``,
      ``triggers``, ``applicability``, ``confidence``, ``procedure_steps`` and
      ``avoid_rules``. Passing a conflicting mechanical value is refused, not
      silently overridden.

    Lifecycle state, evidence and counters are never accepted here, so the
    proposal is always evidence-free. A duplicate ``(identity, version)`` is
    refused by the loaded registry before any write, so no stored record is
    overwritten.
    """
    def operation(root: Path, token: str) -> sr.SkillRecord:
        fields = {
            "identity": identity,
            "version": version,
            "scope": scope,
            "path_or_symbol": path_or_symbol,
            "risk": risk,
            "stage": stage,
        }
        wanted = str(candidate_id or "").strip()
        if wanted:
            try:
                draft = skill_miner.candidate_draft(root, wanted)["proposal_draft"]
            except skill_miner.SkillMinerError as exc:
                raise sr.SkillRegistryError(
                    "skill_registry.invalid_value", str(exc)[:400]
                ) from exc
            for field in _CANDIDATE_DERIVED_FIELDS:
                supplied = str(fields.get(field) or "").strip()
                derived = str(draft.get(field) or "")
                if supplied and supplied != derived:
                    raise sr.SkillRegistryError(
                        "skill_registry.invalid_value",
                        f"{field} is derived from candidate {wanted!r} "
                        f"({derived!r}); it may not be supplied as {supplied!r}",
                    )
                fields[field] = derived
        record = sr.SkillRecord.from_mapping({
            **fields,
            "task_family": task_family,
            "triggers": list(triggers or ()),
            "confidence": confidence,
            "applicability": list(applicability or ()),
            "procedure_steps": list(procedure_steps or ()),
            "avoid_rules": list(avoid_rules or ()),
            "preferred_tools": list(preferred_tools or ()),
        })
        authority = sr.Authority(sr.AuthorityRole.MANAGER, actor_id=_MANAGER_ACTOR, token=token)
        registry = store.load_registry(root)
        proposed = registry.propose(record, authority)
        store.put_record(root, proposed)
        return proposed

    return _invoke_write(operation)


def add_evidence(
    *,
    identity: str,
    version: str,
    source: str,
    outcome: str,
    actor_id: str,
    note: str = "",
) -> dict[str, Any]:
    """MANAGER WRITE: append one caller-supplied evidence entry to a version.

    Provenance is the caller-supplied ``actor_id`` bound by the registry, so two
    entries from distinct actors count as two independent contributions while two
    from one actor count as one. The advanced runtime state is persisted in place
    on the same immutable ``(identity, version)`` row.

    The caller may NOT type an identity in the derived namespace. ``worker``,
    ``reviewer``, ``coordinator``, ``agent`` and ``owner`` are reserved for an
    actor read off a task card's own runner, so this surface can name only a
    manager -- and manager entries canonicalize to one actor however they are
    spelled. Without that reservation, two typed strings were two "independent"
    actors and the two-actor activation floor certified itself.
    """
    def operation(root: Path, token: str) -> sr.SkillRecord:
        authority = sr.Authority(
            sr.AuthorityRole.MANAGER, actor_id=_caller_actor(actor_id), token=token
        )
        registry = store.load_registry(root)
        expected = store.stored_state_digest(root, identity, version)
        updated = registry.add_evidence(
            identity, version, {"source": source, "outcome": outcome, "note": note}, authority
        )
        store.advance_record(root, updated, expected_state_digest=expected)
        return updated

    return _invoke_write(operation)


def add_task_evidence(
    *,
    identity: str,
    version: str,
    task_id: str,
    outcome: str,
    note: str = "",
) -> dict[str, Any]:
    """MANAGER WRITE: append evidence whose actor is DERIVED from a finished card.

    This is the second evidence source, and the reason the registry's activation
    gate is reachable at all. :func:`add_evidence` takes ``actor_id`` as free
    text from the manager, so every entry it can produce ultimately names the one
    verified manager route; once identities are compared canonically, that is a
    single actor no matter how many entries are filed, and a floor of two
    independent actors would be unsatisfiable from that surface alone.

    Here the provenance identity is not typed, it is READ. The caller names a
    ``task_id``; the canonical task store is consulted for that exact card, and
    the actor identity is built from the card's own ``runner`` -- the process
    that actually did the work and whose output the manager reviewed. A card
    that does not exist, never finished, or carries no runner yields no evidence,
    so the manager cannot mint a contributor by choosing a string. The actor is
    ``worker.<runner>`` (or ``reviewer.<runner>`` for a quality-review card), and
    because :func:`skill_registry.canonical_actor_id` emits the role first, such
    an identity can never canonicalize onto a ``manager.*`` one.

    Two cards run by the SAME runner are one actor, not two: the runner is the
    actor, the card is only the provenance anchor recorded in ``source``.

    This raises the bar against format drift, accident, and evidence invented at
    the keyboard. It is not a defence against a manager who can already write
    ``skills.sqlite`` directly -- the store's digests are detection, not
    authentication, and this changes nothing about that.
    """
    def operation(root: Path, _token: str) -> sr.SkillRecord:
        role, actor_id, anchor = _task_actor(root, task_id)
        authority = sr.Authority(sr.AuthorityRole.WORKER, actor_id=actor_id, token="")
        registry = store.load_registry(root)
        expected = store.stored_state_digest(root, identity, version)
        updated = registry.add_evidence(
            identity,
            version,
            {"source": anchor, "outcome": outcome, "note": note},
            authority,
        )
        store.advance_record(root, updated, expected_state_digest=expected)
        return updated

    return _invoke_write(operation)


def record_decision_evidence(
    repo_root: str | Path,
    *,
    task_id: str,
    request_id: str,
    outcome: str,
    note: str = "",
) -> dict[str, Any]:
    """Append one evidence row per INJECTED skill when a decision is recorded.

    This is the automatic half of the loop, and the one that was missing. Across
    3,383 recorded accept/reject decisions the registry held ZERO evidence rows,
    because the only producer was a manager hand-typing
    ``skill_add_evidence`` with a free-text actor (8 such calls in 27 sessions,
    3 of them refused on a malformed actor or an invalid outcome). A floor of two
    independent actors is unreachable from a surface nothing drives.

    Called at the decision site with the card and the request that was judged, it:

    * reads the SELECTION RECEIPT the launcher persisted for that card+request,
      so the skills credited are exactly the ones the worker received -- never a
      replay of selection against today's registry;
    * derives the actor from the card's own ``runner`` (see :func:`_task_actor`),
      so provenance is READ, not typed. There is deliberately no ``actor_id``
      parameter: the security property is that this surface cannot be told who
      acted;
    * records the card's OWN adjudicated decision as the evidence outcome, and
      refuses when the caller's outcome contradicts what the card says.

    Deliberately NOT manager-gated and deliberately fail-soft: it is invoked from
    the accept/reject path, which must not fail because a skill store is missing
    or a record was retired. Every refusal is reported, never raised, and nothing
    here can activate a skill -- activation stays a manager decision behind the
    unchanged two-actor gate.

    Idempotent: an entry with the same ``(source, actor_id, outcome)`` already on
    the record is not appended twice, so a retried finalization cannot inflate
    ``accepted_count``.
    """
    root = Path(repo_root)
    decision = str(outcome or "").strip().lower()
    if decision not in DECISION_EVIDENCE_OUTCOMES:
        return {
            "ok": False,
            "reason": "decision_outcome_not_adjudicated",
            "allowed_outcomes": sorted(DECISION_EVIDENCE_OUTCOMES),
            "recorded": [],
        }
    evidence_outcome = DECISION_EVIDENCE_OUTCOMES[decision]
    try:
        receipt = store.get_selection(root, str(task_id), str(request_id or ""))
    except (store.SkillStoreError, OSError, sqlite3.Error) as exc:
        return {
            "ok": False,
            "reason": f"selection_receipt_unreadable:{type(exc).__name__}",
            "recorded": [],
        }
    if receipt is None:
        return {
            "ok": True,
            "reason": "no_selection_receipt_for_this_card",
            "task_id": str(task_id),
            "request_id": str(request_id or ""),
            "recorded": [],
        }
    if not receipt["skills"]:
        return {
            "ok": True,
            "reason": "selection_receipt_is_empty",
            "task_id": str(task_id),
            "request_id": str(request_id or ""),
            "packet_sha256": receipt["packet_sha256"],
            "recorded": [],
        }
    try:
        _role, actor_id, _anchor = _task_actor(
            root, str(task_id), require_finished=False
        )
    except sr.SkillRegistryError as exc:
        return {"ok": False, "reason": str(exc)[:240], "recorded": []}
    try:
        contradiction = _decision_contradiction(root, str(task_id), str(request_id or ""), decision)
    except (OSError, sqlite3.Error, ValueError):  # noqa: BLE001 - unreadable card is not a veto
        contradiction = ""
    if contradiction:
        return {"ok": False, "reason": contradiction, "recorded": []}

    anchor = str(request_id or task_id)[:128]
    recorded: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for row in receipt["skills"]:
        identity = str(row.get("identity") or "")
        version = str(row.get("version") or "")
        try:
            registry = store.load_registry(root)
            record = registry.get(identity, version)
            if record is None:
                refused.append({"identity": identity, "version": version,
                                "reason": "skill_version_not_stored"})
                continue
            if any(
                item.source == anchor
                and item.actor_id == actor_id
                and item.outcome.value == evidence_outcome
                for item in record.evidence
            ):
                recorded.append({"identity": identity, "version": version,
                                 "outcome": evidence_outcome, "actor_id": actor_id,
                                 "idempotent": True})
                continue
            expected = store.stored_state_digest(root, identity, version)
            authority = sr.Authority(sr.AuthorityRole.WORKER, actor_id=actor_id, token="")
            updated = registry.add_evidence(
                identity,
                version,
                {
                    "source": anchor,
                    "outcome": evidence_outcome,
                    "note": (note or f"card {task_id} was {decision} by the manager")[:2000],
                },
                authority,
            )
            store.advance_record(root, updated, expected_state_digest=expected)
        except (sr.SkillRegistryError, store.SkillStoreError, OSError, sqlite3.Error) as exc:
            refused.append({"identity": identity, "version": version,
                            "reason": str(exc)[:200]})
            continue
        recorded.append({"identity": identity, "version": version,
                         "outcome": evidence_outcome, "actor_id": actor_id,
                         "idempotent": False})
    return {
        "ok": True,
        "schema_id": "aiworkhub.skill_decision_evidence.v1",
        "task_id": str(task_id),
        "request_id": str(request_id or ""),
        "packet_sha256": receipt["packet_sha256"],
        "decision": decision,
        "evidence_outcome": evidence_outcome,
        "actor_id": actor_id,
        "actor_source": "task_card_runner",
        "recorded": recorded,
        "refused": refused,
    }


def _decision_contradiction(
    root: Path, task_id: str, request_id: str, decision: str
) -> str:
    """Return a refusal reason when the card contradicts the claimed decision.

    The card is the authority on what was adjudicated. When it names an outcome
    for this exact request and that outcome is not the one being recorded, the
    evidence would attribute the wrong sign to every injected skill, so it is
    refused. When the card names nothing resolvable -- a rejection has already
    cleared the terminal evidence by the time the reply is built -- the decision
    site's own outcome stands, because the decision site IS the authority there.
    """
    from . import learning_commit_store  # local import: keeps the import graph acyclic

    card = task_store.get_task(root, task_id)
    if not isinstance(card, dict):
        return "task_card_not_in_canonical_store"
    adjudicated = learning_commit_store.adjudicated_decision(card, request_id)
    if adjudicated and adjudicated != decision:
        return f"card_adjudicated_{adjudicated}_not_{decision}"
    return ""


def usage(*, min_accepted_evidence: int = 2) -> dict[str, Any]:
    """MANAGER READ: the per-skill usage statistics, measured, never inferred.

    The dashboard could say only "4 skills / 4 proposed / 0 active / 0 retired",
    which answers none of the questions an owner actually asks: is anything
    being used, is anything reachable, and if not, why not. Each of those is now
    answered from stored evidence:

    * ``proposals`` -- how many versions of this identity are stored.
    * ``evidence_by_outcome`` -- accepted/negative entry counts as recorded.
    * ``distinct_actors`` and ``actor_ids`` -- the CANONICAL independent
      identities the two-actor gate actually counts, not the raw strings, so two
      spellings of one manager read as the one actor they are.
    * ``injectable`` plus the exact ``injectable_reason`` when it is false.
    * ``injected_cards`` -- from the persisted selection receipts only.

    Read-only, and it never activates or retires anything.
    """
    root, _token, manager = _manager_context()
    if root is None:
        return manager
    try:
        stored = store.list_records(root)
        injection = store.injection_counts(root)
    except (store.SkillStoreError, OSError, sqlite3.Error) as exc:
        return {
            "ok": False,
            "error": f"skill_store_failed:{type(exc).__name__}",
            "manager": manager,
            "surface": "manager_mcp",
        }
    versions_per_identity = Counter(record.identity for record in stored)
    skills: list[dict[str, Any]] = []
    for record in stored:
        injectable, reason = skill_miner.injectability(record)
        outcomes = Counter(item.outcome.value for item in record.evidence)
        actors = sr.independent_accepted_actor_ids(record)
        counts = injection.get(f"{record.identity}@{record.version}", {})
        skills.append(
            {
                "identity": record.identity,
                "version": record.version,
                "stored_lifecycle_state": record.lifecycle_state.value,
                "proposals": int(versions_per_identity[record.identity]),
                "evidence_rows": len(record.evidence),
                "evidence_by_outcome": {
                    outcome.value: int(outcomes.get(outcome.value, 0))
                    for outcome in sr.EvidenceOutcome
                },
                "distinct_actors": len(actors),
                "actor_ids": list(actors),
                "raw_actor_ids": sorted({item.actor_id for item in record.evidence}),
                "unresolved_negative_evidence": len(
                    sr.unresolved_negative_evidence(record)
                ),
                "injectable": bool(injectable),
                "injectable_reason": reason,
                "injected_cards": int(counts.get("injected_cards", 0)),
                "injected_requests": int(counts.get("injected_requests", 0)),
                "evidence_anchors": sorted(
                    {item.source for item in record.evidence if item.source}
                ),
            }
        )
    skills.sort(key=lambda item: (item["identity"], item["version"]))
    lifecycles = Counter(item["stored_lifecycle_state"] for item in skills)
    return {
        "ok": True,
        "schema_id": "aiworkhub.skill_usage_report.v1",
        "min_accepted_evidence": int(min_accepted_evidence),
        "totals": {
            "skills": len(skills),
            "proposed": int(lifecycles.get("proposed", 0)),
            "active": int(lifecycles.get("active", 0)),
            "retired": int(lifecycles.get("retired", 0)),
            "injectable": sum(1 for item in skills if item["injectable"]),
            "evidence_rows": sum(item["evidence_rows"] for item in skills),
            "selection_receipts": len(store.list_selections(root)),
        },
        "skills": skills,
        "authority": {
            "produces": "measurements_only",
            "writes": "none",
            "activation": "manager_gated_two_distinct_actor_identities",
        },
        "manager": manager,
        "surface": "manager_mcp",
    }


def audit(*, min_accepted_evidence: int = 2) -> dict[str, Any]:
    """MANAGER READ: report every persisted ACTIVE record against the gate.

    ``accepted_count`` counts evidence ENTRIES and so reads as strong for a
    record whose entries all came from one actor under two spellings. This
    reports the canonical independent actor identities an activation actually
    rests on, and whether the record still satisfies the rule in force. It never
    writes.
    """
    root, _token, manager = _manager_context()
    if root is None:
        return manager
    try:
        records = store.audit_active_records(
            root, min_accepted_evidence=min_accepted_evidence
        )
    except (store.SkillStoreError, OSError, sqlite3.Error) as exc:
        return {
            "ok": False,
            "error": f"skill_store_failed:{type(exc).__name__}",
            "manager": manager,
            "surface": "manager_mcp",
        }
    return {
        "ok": True,
        "active_records": records,
        "unverified_active": [item for item in records if not item["verified"]],
        "manager": manager,
        "surface": "manager_mcp",
    }


def activate(*, identity: str, version: str) -> dict[str, Any]:
    """MANAGER WRITE: activate a proposed skill, evidence-gated and fail-closed.

    The registry's own activation gate (at least ``min_accepted_evidence``
    independent accepted actors and no unresolved negative evidence) is enforced
    unchanged. When the threshold is unmet the registry raises and this returns
    its stable reason without persisting, so the stored record is left as it was.
    """
    def operation(root: Path, token: str) -> sr.SkillRecord:
        authority = sr.Authority(sr.AuthorityRole.MANAGER, actor_id=_MANAGER_ACTOR, token=token)
        registry = store.load_registry(root)
        expected = store.stored_state_digest(root, identity, version)
        updated = registry.activate(identity, version, authority)
        store.advance_record(root, updated, expected_state_digest=expected)
        return updated

    return _invoke_write(operation)


def mine(
    *,
    threshold: float = skill_miner.DEFAULT_THRESHOLD,
    min_cards: int = skill_miner.MIN_DISTINCT_CARDS,
    min_files: int = skill_miner.MIN_DISTINCT_FILES,
    include_sensitivity: bool = True,
) -> dict[str, Any]:
    """MANAGER READ: mine the correction record into gated skill PROPOSALS.

    Read-only in the strongest sense available here: it never opens a store for
    writing and it never returns a lifecycle transition. What it returns is a
    set of rule families that recurred across distinct cards and distinct files,
    each with the exact card ids and request ids where the rule was violated,
    and each with a proposal draft whose closed-vocabulary selection dimensions
    are deliberately left blank for the manager to supply.

    The hand-off is a separate, explicit call: a candidate becomes a stored
    record only when a manager passes a completed draft to :func:`propose`, and
    becomes ACTIVE only through the registry's unchanged evidence gate. There is
    no path from this function to an activation.
    """
    root, _token, manager = _manager_context()
    if root is None:
        return manager
    try:
        report = skill_miner.mine(
            root,
            threshold=threshold,
            min_cards=min_cards,
            min_files=min_files,
            include_sensitivity=include_sensitivity,
        )
    except skill_miner.SkillMinerError as exc:
        return {
            "ok": False,
            "error": str(exc)[:240],
            "manager": manager,
            "surface": "manager_mcp",
        }
    except (OSError, sqlite3.Error) as exc:
        return {
            "ok": False,
            "error": f"correction_record_unreadable:{type(exc).__name__}",
            "manager": manager,
            "surface": "manager_mcp",
        }
    return {"ok": True, **report, "manager": manager, "surface": "manager_mcp"}


def retirement_report(
    *, min_anchors: int = skill_miner.MIN_RETIREMENT_ANCHORS
) -> dict[str, Any]:
    """MANAGER READ: measure each stored skill against its own failure class.

    Reports what can be measured and states plainly what cannot. The injection
    denominator NF-2026-00312 layer six asks for is not derivable from this
    repository's stored cards, and the report says so with both counts rather
    than substituting a number that would read as a measurement. Every verdict
    is a recommendation; retirement stays manager-gated, and this never writes.
    """
    root, _token, manager = _manager_context()
    if root is None:
        return manager
    try:
        report = skill_miner.measure_retirement(root, min_anchors=min_anchors)
    except skill_miner.SkillMinerError as exc:
        return {
            "ok": False,
            "error": str(exc)[:240],
            "manager": manager,
            "surface": "manager_mcp",
        }
    except (store.SkillStoreError, OSError, sqlite3.Error) as exc:
        return {
            "ok": False,
            "error": f"skill_store_failed:{type(exc).__name__}",
            "manager": manager,
            "surface": "manager_mcp",
        }
    return {"ok": True, **report, "manager": manager, "surface": "manager_mcp"}
