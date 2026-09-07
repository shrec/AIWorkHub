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

import sqlite3
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


def _task_actor(root: Path, task_id: str) -> tuple[str, str, str]:
    """Derive ``(role, actor_id, source_anchor)`` from one finished task card.

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
    if not str(card.get("completed_at") or "").strip():
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


def propose(
    *,
    identity: str,
    version: str,
    scope: str,
    task_family: str,
    path_or_symbol: str,
    risk: str,
    stage: str,
    triggers: list[str],
    confidence: float,
    applicability: list[str] | None = None,
    procedure_steps: list[str] | None = None,
    avoid_rules: list[str] | None = None,
    preferred_tools: list[str] | None = None,
) -> dict[str, Any]:
    """MANAGER WRITE: register and persist one caller-defined proposed skill.

    Every field is taken verbatim from the caller; lifecycle state, evidence and
    counters are never accepted here, so the proposal is always evidence-free.
    A duplicate ``(identity, version)`` is refused by the loaded registry before
    any write, so no stored record is overwritten.
    """
    def operation(root: Path, token: str) -> sr.SkillRecord:
        record = sr.SkillRecord.from_mapping({
            "identity": identity,
            "version": version,
            "scope": scope,
            "task_family": task_family,
            "path_or_symbol": path_or_symbol,
            "risk": risk,
            "stage": stage,
            "triggers": list(triggers),
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
    """
    def operation(root: Path, token: str) -> sr.SkillRecord:
        authority = sr.Authority(sr.AuthorityRole.MANAGER, actor_id=actor_id, token=token)
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
