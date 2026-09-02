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
from . import skill_registry_store as store

# The non-secret provenance identity of the manager's own lifecycle authority
# (propose/activate). Evidence provenance is caller-supplied instead, so distinct
# contributing actors can be recorded independently. Must match the registry's
# actor-id shape (``[a-z][a-z0-9_.-]*``).
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
        prior = registry.get(identity, version)
        expected = store.state_digest(prior) if prior is not None else None
        updated = registry.add_evidence(
            identity, version, {"source": source, "outcome": outcome, "note": note}, authority
        )
        store.advance_record(root, updated, expected_state_digest=expected)
        return updated

    return _invoke_write(operation)


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
        prior = registry.get(identity, version)
        expected = store.state_digest(prior) if prior is not None else None
        updated = registry.activate(identity, version, authority)
        store.advance_record(root, updated, expected_state_digest=expected)
        return updated

    return _invoke_write(operation)
