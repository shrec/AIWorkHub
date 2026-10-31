"""Optional accept gate: a recipe run bound to the exact candidate (NF-2026-01835).

A long acceptance suite is run by hand outside accept, so accept could promote
a candidate whose suite never ran, ran on other bytes, or ran red. With the
policy ``.aiworkhub/config/accept_gate.json`` = ``{recipe_id, version, params}``
accept is refused unless a COMPLETED, returncode-0 detached recipe run bound to
the current ``candidate_sha256``, recipe digest and params exists.

Nothing is invented here: the run is a NF-2026-01849 detached recipe run
(``recipe_runner.start_detached_run`` / ``_detached_main`` /
``read_detached_status`` and its host run lock). This module adds the policy,
the receipt that names which run belongs to which accept request, the child's
combined candidate tree, and the one predicate both ``accept_preview`` and
``accept_review`` ask. No policy file means no gate and accept is unchanged.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import recipe_runner
from . import tool_recipes as tr
from . import tool_recipes_store as store

POLICY_REL = (".aiworkhub", "config", "accept_gate.json")
RECEIPT_REL = (".aiworkhub", "runtime", "accept_gate")
RECEIPT_SCHEMA_ID = "aiworkhub.accept_gate_receipt.v1"

GATE_POLICY_INVALID = "gate_policy_invalid"
GATE_RECEIPT_MISSING = "gate_receipt_missing"
GATE_RECEIPT_PENDING = "gate_receipt_pending"
GATE_RECEIPT_STALE = "gate_receipt_stale"
GATE_RECEIPT_RED = "gate_receipt_red"
GATE_BLOCKER_KINDS = (
    GATE_POLICY_INVALID,
    GATE_RECEIPT_MISSING,
    GATE_RECEIPT_PENDING,
    GATE_RECEIPT_STALE,
    GATE_RECEIPT_RED,
)

REASON_POLICY_MISMATCH = "gate_policy_mismatch"
REASON_RUN_IN_PROGRESS = "accept_gate_run_in_progress"

# A queued run that never recorded a pid is only believed for this long.
_SPAWN_GRACE_SECONDS = 600.0
_PENDING_STATES = frozenset(
    {
        recipe_runner.DETACHED_STATE_QUEUED,
        recipe_runner.DETACHED_STATE_WAITING_MEMORY,
        recipe_runner.DETACHED_STATE_RUNNING,
    }
)
_BINDING_KEYS = ("task_id", "request_id", "candidate_sha256")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class AcceptGateError(ValueError):
    """The gate cannot be evaluated or run; the message names the cause."""


def _refusal(kind: str, detail: str = "") -> dict[str, str]:
    detail = str(detail)[:300]
    return {"kind": kind, "error": f"{kind}:{detail}" if detail else kind}


def params_sha256(params: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(params), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_policy(repo_root: str | Path) -> dict[str, Any] | None:
    """The gate policy with its resolved recipe, ``None`` when none is declared.

    Raises :class:`AcceptGateError` for a malformed policy or a recipe the
    store does not hold -- never a silent "no gate".
    """
    root = Path(repo_root)
    try:
        raw = root.joinpath(*POLICY_REL).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise AcceptGateError(f"policy_unreadable:{type(exc).__name__}") from exc
    try:
        loaded = json.loads(raw)
    except ValueError as exc:
        raise AcceptGateError("policy_malformed_json") from exc
    if not isinstance(loaded, dict):
        raise AcceptGateError("policy_not_an_object")
    recipe_id = loaded.get("recipe_id")
    if not isinstance(recipe_id, str) or not recipe_id.strip():
        raise AcceptGateError("policy_recipe_id_missing")
    version = loaded.get("version")
    if version is not None and (not isinstance(version, str) or not version.strip()):
        raise AcceptGateError("policy_version_invalid")
    params = loaded.get("params")
    params = {} if params is None else params
    if not isinstance(params, dict):
        raise AcceptGateError("policy_params_invalid")
    try:
        recipe = store.load_registry(root).get(recipe_id.strip(), version)
    except Exception as exc:  # noqa: BLE001 - any store failure is an invalid policy
        raise AcceptGateError(
            f"policy_recipe_unavailable:{recipe_id.strip()}:{type(exc).__name__}"
        ) from exc
    return {
        "recipe_id": recipe.id,
        "version": recipe.version,
        "params": dict(params),
        "recipe": recipe,
    }


def receipt_path(repo_root: str | Path, request_id: str) -> Path:
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id) or ".." in request_id:
        raise AcceptGateError("request_id_invalid")
    return Path(repo_root).joinpath(*RECEIPT_REL, f"{request_id}.json")


def _read_receipt(repo_root: Path, request_id: str) -> dict[str, Any] | None:
    try:
        loaded = recipe_runner._load_status(receipt_path(repo_root, request_id))
    except FileNotFoundError:
        return None
    return loaded


def _utc(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _run_state(record: Mapping[str, Any]) -> str:
    """The run's state, a pending one with no live owner reading as ``lost``."""
    state = str(record.get("state") or "")
    if state not in _PENDING_STATES:
        return state
    pid = record.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool):
        return state
    queued = _utc(record.get("queued_at"))
    if queued is not None and (
        datetime.now(timezone.utc) - queued
    ).total_seconds() < _SPAWN_GRACE_SECONDS:
        return state
    return recipe_runner.DETACHED_STATE_LOST


def _bound_run(repo_root: Path, receipt: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        return recipe_runner.read_detached_status(
            repo_root, str(receipt.get("run_id") or ""), wait_seconds=0
        )
    except recipe_runner.RecipeRunError:
        return None


def _receipt_blocker(
    root: Path,
    policy: Mapping[str, Any],
    *,
    task_id: str,
    request_id: str,
    candidate_sha256: str,
) -> dict[str, str] | None:
    recipe = policy["recipe"]
    wanted_params = params_sha256(policy["params"])
    receipt = _read_receipt(root, request_id)
    if receipt is None:
        return _refusal(GATE_RECEIPT_MISSING, request_id)
    if receipt.get("task_id") != task_id or receipt.get("request_id") != request_id:
        return _refusal(GATE_RECEIPT_MISSING, "receipt_binding_mismatch")
    if not candidate_sha256 or receipt.get("candidate_sha256") != candidate_sha256:
        return _refusal(GATE_RECEIPT_STALE, "candidate_changed")
    if (
        receipt.get("recipe_id") != recipe.id
        or receipt.get("recipe_version") != recipe.version
        or receipt.get("recipe_digest") != recipe.digest
    ):
        return _refusal(GATE_RECEIPT_STALE, "recipe_changed")
    if receipt.get("params_sha256") != wanted_params:
        return _refusal(GATE_RECEIPT_STALE, "params_changed")
    record = _bound_run(root, receipt)
    if record is None:
        return _refusal(GATE_RECEIPT_MISSING, "run_unknown")
    expected_binding = {
        "task_id": task_id,
        "request_id": request_id,
        "candidate_sha256": candidate_sha256,
    }
    if record.get("accept_binding") != expected_binding:
        return _refusal(GATE_RECEIPT_STALE, "run_binding_mismatch")
    if (
        record.get("recipe_digest") != recipe.digest
        or params_sha256(record.get("params") or {}) != wanted_params
    ):
        return _refusal(GATE_RECEIPT_STALE, "run_recipe_mismatch")
    state = _run_state(record)
    if state in _PENDING_STATES:
        return _refusal(GATE_RECEIPT_PENDING, state)
    if state == recipe_runner.DETACHED_STATE_LOST:
        return _refusal(GATE_RECEIPT_STALE, "run_lost")
    if record.get("reason") == recipe_runner.REASON_RECIPE_DIGEST_CHANGED:
        return _refusal(GATE_RECEIPT_STALE, recipe_runner.REASON_RECIPE_DIGEST_CHANGED)
    if state == recipe_runner.DETACHED_STATE_COMPLETED:
        digest = record.get("digest")
        digest = digest if isinstance(digest, Mapping) else {}
        returncode = digest.get("returncode")
        if (
            isinstance(returncode, int)
            and not isinstance(returncode, bool)
            and returncode == 0
            and digest.get("exit_status") == tr.EXIT_STATUS_COMPLETED
        ):
            return None
        return _refusal(
            GATE_RECEIPT_RED,
            f"returncode={returncode} exit_status={digest.get('exit_status')}",
        )
    if state in (
        recipe_runner.DETACHED_STATE_FAILED,
        recipe_runner.DETACHED_STATE_REFUSED,
    ):
        return _refusal(GATE_RECEIPT_RED, f"{state}:{record.get('reason') or ''}")
    return _refusal(GATE_RECEIPT_STALE, f"run_state_{state or 'unknown'}")


def gate_blocker(
    repo_root: str | Path,
    *,
    task_id: str,
    request_id: str,
    candidate_sha256: str,
) -> dict[str, str] | None:
    """``None`` when no gate is declared or the bound run is green; else the blocker."""
    root = Path(repo_root)
    try:
        policy = load_policy(root)
    except AcceptGateError as exc:
        return _refusal(GATE_POLICY_INVALID, str(exc))
    if policy is None:
        return None
    try:
        return _receipt_blocker(
            root,
            policy,
            task_id=task_id,
            request_id=request_id,
            candidate_sha256=candidate_sha256,
        )
    except AcceptGateError as exc:
        return _refusal(GATE_RECEIPT_MISSING, str(exc))
    except Exception as exc:  # noqa: BLE001 - an unreadable receipt blocks, never passes
        return _refusal(GATE_RECEIPT_MISSING, f"receipt_unreadable:{type(exc).__name__}")


def _load_candidate(repo_root: Path, task_id: str) -> tuple[Any, Any, list[str]]:
    """The target card, its verified workspace and its candidate paths."""
    from . import process_launcher as _pl
    from .worker_workspace import WorkerWorkspace

    manager = _pl.default_manager()
    if Path(manager.repo).resolve() != repo_root.resolve():
        raise AcceptGateError("repository_mismatch")
    card = _pl._parse_card(manager._show_task(task_id), task_id)
    terminal = card.get("terminal_review")
    evidence = terminal.get("evidence") if isinstance(terminal, Mapping) else None
    if not isinstance(evidence, Mapping):
        raise AcceptGateError("terminal_evidence_missing")
    try:
        workspace = WorkerWorkspace.from_metadata(evidence.get("workspace"))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise AcceptGateError("workspace_metadata_invalid") from exc
    hashes = evidence.get("changed_path_hashes")
    changed = sorted(
        {str(v) for v in (evidence.get("changed_paths") or ())}
        | {str(v) for v in (hashes if isinstance(hashes, Mapping) else ())}
    )
    return card, workspace, changed


def _candidate_sha256(card: Any) -> str:
    from .process_launcher_accept_review import _target_candidate_sha256

    return _target_candidate_sha256(card)


def run_on_candidate_tree(
    repo_root: str | Path,
    binding: Mapping[str, Any],
    run: Callable[[Path], Any],
) -> Any:
    """Run ``run(tree)`` on the combined candidate tree and always remove the tree."""
    from . import worker_workspace

    root = Path(repo_root)
    if not isinstance(binding, Mapping) or any(
        not isinstance(binding.get(key), str) or not binding.get(key)
        for key in _BINDING_KEYS
    ):
        raise AcceptGateError(recipe_runner.REASON_ACCEPT_BINDING_INCOMPLETE)
    card, workspace, changed = _load_candidate(root, binding["task_id"])
    if _candidate_sha256(card) != binding["candidate_sha256"]:
        raise AcceptGateError("candidate_changed_since_queued")
    union, _tree = worker_workspace.create_combined_validation_workspace(
        workspace, card, changed
    )
    try:
        return run(Path(union.path))
    finally:
        # A leftover tree is the storage janitor's; it must not mask the run's result.
        with contextlib.suppress(Exception):
            worker_workspace.cleanup_workspace(union.repo, union.path, union.home)


def start_accept_run(
    repo_root: str | Path,
    recipe: tr.Recipe,
    *,
    task_id: str,
    request_id: str,
    params: Mapping[str, Any],
    grant_capabilities: tuple[str, ...],
    actor: tr.ActorIdentity | None,
    manager: Mapping[str, Any],
) -> dict[str, Any]:
    """Queue the policy's recipe against the exact candidate and write its receipt.

    Returns ``{"ok": True, **started}`` or ``{"ok": False, "error", "reason_code"}``.
    """
    root = Path(repo_root).resolve()

    def refuse(reason: str, detail: str = "") -> dict[str, Any]:
        return {
            "ok": False,
            "error": f"{reason}:{detail}"[:300] if detail else reason,
            "reason_code": reason,
        }

    try:
        policy = load_policy(root)
    except AcceptGateError as exc:
        return refuse(GATE_POLICY_INVALID, str(exc))
    if policy is None:
        return refuse(GATE_POLICY_INVALID, "policy_absent")
    if (
        recipe.id != policy["recipe_id"]
        or recipe.version != policy["version"]
        or recipe.digest != policy["recipe"].digest
        or dict(params) != policy["params"]
    ):
        return refuse(REASON_POLICY_MISMATCH, f"{policy['recipe_id']}@{policy['version']}")

    from . import process_launcher as _pl
    from . import review_orchestrator

    try:
        card, workspace, _changed = _load_candidate(root, task_id)
    except AcceptGateError as exc:
        return refuse("accept_gate_target_unverified", str(exc))
    except Exception as exc:  # noqa: BLE001 - an unreadable target is not verified
        return refuse("accept_gate_target_unverified", f"task_lookup_failed:{type(exc).__name__}")
    status = _pl._canonical_task_status(card)
    if status != "review_ready":
        return refuse("accept_gate_target_not_review_ready", status)
    identity = review_orchestrator.target_identity_from_card(card)
    if identity.get("target_request_id") != request_id or identity.get("target_task_id") != task_id:
        return refuse("accept_gate_request_mismatch", request_id)
    if not Path(workspace.path).is_dir():
        return refuse("accept_gate_target_unverified", "workspace_missing")
    candidate = _candidate_sha256(card)
    if not candidate:
        return refuse("accept_gate_target_unverified", "candidate_sha256_missing")

    try:
        path = receipt_path(root, request_id)
        prior = _read_receipt(root, request_id)
    except AcceptGateError as exc:
        return refuse("accept_gate_request_mismatch", str(exc))
    if prior is not None:
        record = _bound_run(root, prior)
        if record is not None and _run_state(record) in _PENDING_STATES:
            return refuse(REASON_RUN_IN_PROGRESS, str(prior.get("run_id") or ""))

    started = recipe_runner.start_detached_run(
        root,
        recipe,
        policy["params"],
        grant_capabilities=grant_capabilities,
        actor=actor,
        accept_binding={
            "task_id": task_id,
            "request_id": request_id,
            "candidate_sha256": candidate,
        },
    )
    receipt = {
        "schema_id": RECEIPT_SCHEMA_ID,
        "task_id": task_id,
        "request_id": request_id,
        "run_id": started["run_id"],
        "candidate_sha256": candidate,
        "recipe_id": recipe.id,
        "recipe_version": recipe.version,
        "recipe_digest": recipe.digest,
        "params_sha256": params_sha256(policy["params"]),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "actor": {
            key: manager.get(key) for key in ("provider", "session_id") if manager.get(key)
        },
    }
    try:
        recipe_runner._write_status(path, receipt)
    except OSError as exc:
        return refuse("accept_gate_receipt_unwritable", type(exc).__name__)
    return {"ok": True, **started, "accept_gate_receipt": str(path)}


__all__ = [
    "GATE_BLOCKER_KINDS",
    "AcceptGateError",
    "gate_blocker",
    "load_policy",
    "params_sha256",
    "receipt_path",
    "run_on_candidate_tree",
    "start_accept_run",
]
