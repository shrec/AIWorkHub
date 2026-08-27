"""Repository-local model inventory backed by observed task outcomes.

Configuration declares capability only. Availability, quota and performance
are never invented: runtime readiness and canonical task/process evidence are
joined at read time, with missing observations labeled explicitly.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping

from . import (
    core,
    cost_ledger,
    learning_commit,
    model_settings,
    repo_policy,
    runtime_adapters,
    task_store,
    workforce_router,
)
from .platform_io import posix_path_modes_supported


# Editor (vscode_lm) routes seed a single capability row per provider; when the
# live editor host reports a catalog, that seed expands to one row per
# discovered model.  Only these adapters read their model set from VS Code.
_DISCOVERY_ADAPTERS: frozenset[str] = frozenset({"glm_vscode_lm"})

# These adapters all execute through the VS Code Language Model API.  The
# model vendor (OpenAI, Anthropic, Zhipu, ...) and the subscription/transport
# owner are different identities: repository policy must be able to disable
# Copilot without also disabling a native Codex/Claude/provider route.
SCHEMA_ID = "aiworkhub.workforce_catalog.v1"
CATALOG_RELATIVE_PATH = Path(".aiworkhub/config/workforce.json")
AUDIT_RELATIVE_PATH = Path(".aiworkhub/config/workforce.audit.jsonl")
MAX_CATALOG_BYTES = 256 * 1024
MAX_WORKERS = 64
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MODEL_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,127}$")
ROUTE_CIRCUIT_COOLDOWN_SECONDS = 600.0
ROUTE_CIRCUIT_LOOKBACK_SECONDS = 86_400.0
ROUTE_CIRCUIT_TRANSIENT_THRESHOLD = 2
# validation_failed, finalize_failed and review_ready are downstream gate
# outcomes: the provider returned a usable result, so they close (never open)
# a route circuit and are never counted as provider-route failures.
_ROUTE_SUCCESS_STATES = frozenset(
    {"accepted", "review_ready", "validation_failed", "finalize_failed"}
)
# Route-terminal kinds that fail closed after a single authenticated,
# provider-owned terminal error rather than the transient retry threshold.
_ROUTE_SINGLE_FAILURE_KINDS = frozenset({"auth", "quota"})
# Sealed, provider-owned structured terminal error codes.  These are honoured
# only from an error object the transport sealed itself; the free-form ``error``
# string and any assistant/model prose are never scanned for them, so a model
# cannot spoof a balance/quota or refresh-token failure into a route circuit.
_ROUTE_QUOTA_ERROR_CODES = frozenset({
    "insufficient_balance", "insufficient_quota",
    "balance_exhausted", "quota_exhausted",
})
_ROUTE_AUTH_ERROR_CODES = frozenset({
    "invalid_grant", "unknown_refresh_token",
    "invalid_api_key", "unauthorized",
    "authentication_failed", "authorization_failed",
})
_ROUTE_TRANSIENT_MARKERS = (
    "provider_timeout", "mcp_request_timeout", "no_terminal_event",
    "empty_provider_response", "direct app-server input", "turn/start failed",
    "access is denied", "mcp_unavailable", "malformed_stream",
)


def execution_runner(worker_id: str, adapter_id: str) -> str:
    """Return the stable worker identity used by create+launch receipts.

    Workforce selection used to return adapter/model without the third exact
    identity required by task creation and launch.  Managers consequently
    reused their own ``codex`` identity, which preflight accepted but the
    card-scoped claim gate correctly rejected.  Keep the identity derived and
    deterministic so the rank receipt is directly executable.
    """

    family = {
        "claude_cli": "claude",
        "codex_cli": "codex",
        "deepseek_vscode_lm": "deepseek",
        "deepseek_copilot_cli": "deepseek",
        "deepseek_manual": "deepseek",
        "glm_vscode_lm": "glm",
        "glm_copilot_cli": "glm",
        "grok_kilo_cli": "grok",
        "vscode_lm": "copilot",
    }.get(str(adapter_id).strip(), "worker")
    slug = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(worker_id).strip()).strip("_.:-")
    remainder = slug
    for separator in ("_", "-", ".", ":"):
        prefix = family + separator
        if slug.casefold().startswith(prefix.casefold()):
            remainder = slug[len(prefix) :]
            break
    candidate = f"{family}_{remainder or slug}"
    if len(candidate) <= 128 and _TOKEN_RE.fullmatch(candidate):
        return candidate
    digest = hashlib.sha256(
        f"{worker_id}\0{adapter_id}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{candidate[:115].rstrip('_.:-')}_{digest}"

# Keep the declared worker/provider/model identity stable while allowing an
# equivalent launch transport when the preferred VS Code LM surface is not
# visible in the current host.  The effective adapter remains explicit in the
# catalog response; this is routing evidence, not fabricated credential/quota
# evidence.
_WORKER_ADAPTER_FALLBACKS: dict[str, tuple[str, ...]] = {
    # First-party Claude subscription workers never fall back to the editor
    # broker.  A VS Code/Copilot Claude model is a distinct authorization and
    # billing surface, and a Claude Code-contributed LM may be visible while
    # yielding no background response parts.  Copilot-owned Claude workers
    # must be declared explicitly with their own ``copilot_*`` identity.
    "codex_cli": ("vscode_lm",),
    "deepseek_vscode_lm": ("vscode_lm", "deepseek_copilot_cli"),
    "glm_vscode_lm": ("vscode_lm", "glm_copilot_cli"),
}

_EDITOR_MODEL_ALIASES: dict[str, tuple[str, ...]] = {
    "haiku": ("claude-haiku-4.5",),
    "sonnet": ("claude-sonnet-5", "claude-sonnet-4.6", "claude-sonnet-4.5"),
    "opus": ("claude-opus-5", "claude-opus-4.8-fast", "claude-opus-4.8"),
}


DEFAULT_WORKERS: tuple[dict[str, Any], ...] = (
    {"worker_id": "claude-haiku", "adapter_id": "claude_cli", "model": "haiku", "provider": "anthropic", "supports": ["mechanical", "code", "review"], "tools": ["filesystem", "source-graph"], "max_context_tokens": 200_000, "max_risk": "medium", "quality_ceiling": 0.85},
    {"worker_id": "claude-sonnet-5", "adapter_id": "claude_cli", "model": "sonnet", "provider": "anthropic", "supports": ["mechanical", "code", "research", "linguistic", "review"], "tools": ["filesystem", "source-graph"], "max_context_tokens": 1_000_000, "max_risk": "high", "quality_ceiling": 0.97},
    {"worker_id": "claude-opus-5", "adapter_id": "claude_cli", "model": "opus", "provider": "anthropic", "supports": ["code", "research", "linguistic", "review"], "tools": ["filesystem", "source-graph"], "max_context_tokens": 1_000_000, "max_risk": "critical", "quality_ceiling": 1.0},
    {"worker_id": "gpt-5.5", "adapter_id": "codex_cli", "model": "gpt-5.5", "provider": "openai", "supports": ["code", "research", "linguistic", "review"], "tools": ["filesystem", "source-graph"], "max_context_tokens": 921_000, "max_risk": "critical", "quality_ceiling": 1.0},
    {"worker_id": "gpt-5.3-codex", "adapter_id": "codex_cli", "model": "gpt-5.3-codex", "provider": "openai", "supports": ["mechanical", "code", "review"], "tools": ["filesystem", "source-graph"], "max_context_tokens": 272_000, "max_risk": "high", "quality_ceiling": 0.96},
    {"worker_id": "gpt-5.3-codex-spark", "adapter_id": "codex_cli", "model": "gpt-5.3-codex-spark", "provider": "openai", "supports": ["mechanical", "code"], "tools": ["filesystem", "source-graph"], "max_context_tokens": 272_000, "max_risk": "medium", "quality_ceiling": 0.88},
    {"worker_id": "deepseek-v4-pro", "adapter_id": "deepseek_vscode_lm", "model": "deepseek-v4-pro", "provider": "deepseek", "supports": ["mechanical", "code", "research", "review"], "tools": ["filesystem", "source-graph", "session-manager", "ai-memory", "kb", "semantic-edit"], "max_context_tokens": 1_000_000, "max_risk": "high", "quality_ceiling": 0.96},
    {"worker_id": "deepseek-v4-flash", "adapter_id": "deepseek_vscode_lm", "model": "deepseek-v4-flash", "provider": "deepseek", "supports": ["mechanical", "code"], "tools": ["filesystem", "source-graph", "session-manager", "ai-memory", "kb", "semantic-edit"], "max_context_tokens": 1_000_000, "max_risk": "medium", "quality_ceiling": 0.86},
    {"worker_id": "glm-5.2", "adapter_id": "glm_vscode_lm", "model": "glm-5.2", "provider": "zhipu", "supports": ["mechanical", "code", "research", "review"], "tools": ["filesystem", "source-graph", "session-manager", "ai-memory", "kb", "semantic-edit"], "max_context_tokens": 1_000_000, "max_risk": "high", "quality_ceiling": 0.95},
    {"worker_id": "grok-4.6", "adapter_id": "grok_kilo_cli", "model": "xai/grok-4.6", "provider": "xai", "supports": ["mechanical", "code", "research", "review"], "tools": ["filesystem", "source-graph", "session-manager", "ai-memory", "kb", "semantic-edit"], "max_context_tokens": 256_000, "max_risk": "high", "quality_ceiling": 0.97},
)


class WorkforceCatalogError(RuntimeError):
    pass


def policy_route_identity(provider: str, adapter_id: str) -> tuple[str, str]:
    """Return the repository-policy owner for one effective launch route.

    Editor-hosted models are paid/authorized through the Copilot/VS Code LM
    surface even when their model vendor is GLM, DeepSeek or OpenAI.  Collapse
    the implementation-specific editor adapters into one stable policy key so
    a single Copilot switch, and its exact per-model children, gate every such
    route consistently.
    """

    return model_settings.policy_route_identity(provider, adapter_id)


def model_identity_valid(value: str) -> bool:
    """Return whether a discovered model id is safe for policy persistence."""

    return bool(_TOKEN_RE.fullmatch(str(value)))


def catalog_path(repo_root: Path | str) -> Path:
    return Path(repo_root).resolve() / CATALOG_RELATIVE_PATH


def _tokens(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 32:
        raise WorkforceCatalogError(f"{field}_must_be_bounded_list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _TOKEN_RE.fullmatch(item):
            raise WorkforceCatalogError(f"{field}_invalid_token")
        normalized = item.strip().lower()
        if normalized not in out:
            out.append(normalized)
    return out


def _worker(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkforceCatalogError("worker_must_be_object")
    worker_id = str(value.get("worker_id") or "").strip()
    adapter_id = str(value.get("adapter_id") or "").strip()
    model = str(value.get("model") or "").strip()
    provider = str(value.get("provider") or "").strip().lower()
    if (
        not all(_TOKEN_RE.fullmatch(item) for item in (worker_id, adapter_id, provider))
        or not _MODEL_TOKEN_RE.fullmatch(model)
    ):
        raise WorkforceCatalogError("worker_identity_invalid")
    if adapter_id not in repo_policy.DEFAULT_POLICY["providers"]["allowed_adapters"]:
        raise WorkforceCatalogError("worker_adapter_unsupported")
    max_context = value.get("max_context_tokens", 0)
    if isinstance(max_context, bool) or not isinstance(max_context, int) or not 0 <= max_context <= 10_000_000:
        raise WorkforceCatalogError("max_context_tokens_out_of_range")
    risk = str(value.get("max_risk") or "medium").strip().lower()
    if risk not in workforce_router.RISK_ORDER:
        raise WorkforceCatalogError("max_risk_invalid")
    try:
        ceiling = float(value.get("quality_ceiling", 1.0))
        adjustment = float(value.get("manager_score_adjustment", 0.0))
    except (TypeError, ValueError, OverflowError) as exc:
        raise WorkforceCatalogError("worker_score_invalid") from exc
    if not math.isfinite(ceiling) or not 0.0 <= ceiling <= 1.0:
        raise WorkforceCatalogError("quality_ceiling_out_of_range")
    if not math.isfinite(adjustment) or not -20.0 <= adjustment <= 20.0:
        raise WorkforceCatalogError("manager_score_adjustment_out_of_range")
    supports = _tokens(value.get("supports"), "supports")
    if not supports or set(supports) - set(workforce_router.TASK_KINDS):
        raise WorkforceCatalogError("worker_supports_invalid")
    return {
        "worker_id": worker_id,
        "adapter_id": adapter_id,
        "model": model,
        "provider": provider,
        "enabled": bool(value.get("enabled", True)),
        "supports": supports,
        "tools": _tokens(value.get("tools", []), "tools"),
        "max_context_tokens": max_context,
        "max_risk": risk,
        "quality_ceiling": ceiling,
        "manager_score_adjustment": adjustment,
    }


def validate_catalog(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema_id") != SCHEMA_ID:
        raise WorkforceCatalogError("catalog_schema_invalid")
    raw_workers = value.get("workers")
    if not isinstance(raw_workers, list) or len(raw_workers) > MAX_WORKERS:
        raise WorkforceCatalogError("workers_must_be_bounded_list")
    workers = [_worker(item) for item in raw_workers]
    ids = [item["worker_id"] for item in workers]
    if len(ids) != len(set(ids)):
        raise WorkforceCatalogError("duplicate_worker_id")
    revision = value.get("revision", 1)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise WorkforceCatalogError("catalog_revision_invalid")
    return {"schema_id": SCHEMA_ID, "revision": revision, "workers": workers}


def _default_catalog() -> dict[str, Any]:
    return validate_catalog({"schema_id": SCHEMA_ID, "revision": 1, "workers": [dict(item, enabled=True, manager_score_adjustment=0.0) for item in DEFAULT_WORKERS]})


def _merge_new_builtin_workers(catalog: dict[str, Any]) -> dict[str, Any]:
    """Expose newly shipped built-ins without overwriting repository choices."""

    existing = {str(row["worker_id"]) for row in catalog["workers"]}
    additions = [
        dict(item, enabled=True, manager_score_adjustment=0.0)
        for item in DEFAULT_WORKERS
        if str(item["worker_id"]) not in existing
    ]
    if not additions:
        return catalog
    return validate_catalog(
        {
            "schema_id": catalog["schema_id"],
            "revision": catalog["revision"],
            "workers": [*catalog["workers"], *additions],
        }
    )


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > MAX_CATALOG_BYTES:
        raise WorkforceCatalogError("catalog_too_large")
    fd, name = tempfile.mkstemp(prefix=".workforce-", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        if (
            posix_path_modes_supported()
            and os.fstat(fd).st_mode & 0o777 != 0o600
        ):
            os.chmod(tmp, 0o600)
        handle = os.fdopen(fd, "wb")
        fd = -1  # ownership transferred to ``handle``
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        if posix_path_modes_supported() and path.stat().st_mode & 0o777 != 0o600:
            os.chmod(path, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)


def load_catalog(repo_root: Path | str) -> dict[str, Any]:
    path = catalog_path(repo_root)
    if not path.exists():
        return {**_default_catalog(), "configured": False}
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CATALOG_BYTES:
        raise WorkforceCatalogError("catalog_file_invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkforceCatalogError(f"catalog_invalid_json:{type(exc).__name__}") from exc
    return {**_merge_new_builtin_workers(validate_catalog(value)), "configured": True}


def ensure_catalog(repo_root: Path | str) -> tuple[Path, bool]:
    root = Path(repo_root).resolve()
    if not (root / ".aiworkhub/project.json").is_file():
        raise WorkforceCatalogError("repository_not_initialized")
    path = catalog_path(root)
    if path.exists():
        load_catalog(root)
        return path, False
    _atomic_write(path, _default_catalog())
    return path, True


def _append_audit(root: Path, actor: Mapping[str, str], action: str, worker_id: str, revision: int) -> None:
    path = root / AUDIT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "schema_id": "aiworkhub.workforce_audit.v1",
        "timestamp": datetime.now().astimezone().isoformat(),
        "action": action,
        "worker_id": worker_id,
        "revision": revision,
        "role": str(actor.get("role") or "manager")[:40],
        "provider": str(actor.get("provider") or "manager")[:80],
        "actor_id_suffix": str(actor.get("actor_id") or "")[-12:],
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def upsert_worker(repo_root: Path | str, worker: Mapping[str, Any], *, actor: Mapping[str, str]) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    ensure_catalog(root)
    catalog = load_catalog(root)
    normalized = _worker(worker)
    workers = [dict(item) for item in catalog["workers"]]
    index = next((idx for idx, item in enumerate(workers) if item["worker_id"] == normalized["worker_id"]), None)
    action = "updated" if index is not None else "created"
    if index is None:
        if len(workers) >= MAX_WORKERS:
            raise WorkforceCatalogError("worker_limit_reached")
        workers.append(normalized)
    else:
        workers[index] = normalized
    revision = int(catalog["revision"]) + 1
    payload = validate_catalog({"schema_id": SCHEMA_ID, "revision": revision, "workers": workers})
    _atomic_write(catalog_path(root), payload)
    _append_audit(root, actor, action, normalized["worker_id"], revision)
    return {"ok": True, "action": action, "worker_id": normalized["worker_id"], "revision": revision}


def _iso_seconds(start: Any, finish: Any) -> float | None:
    try:
        first = datetime.fromisoformat(str(start))
        last = datetime.fromisoformat(str(finish))
    except (TypeError, ValueError):
        return None
    return max(0.0, (last - first).total_seconds())


def _process_event_epoch(process: Mapping[str, Any]) -> float | None:
    for field in ("finished_at", "timestamp", "started_at"):
        raw = process.get(field)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _sealed_provider_error(process: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the sealed, provider-owned structured terminal error, if present.

    Only an error object the provider transport sealed itself (``owner`` is the
    provider and ``sealed`` is exactly ``True``) is trusted.  Arbitrary
    assistant/model prose -- including free-form ``error`` text that merely
    contains ``402`` or ``insufficient_balance`` -- is never a classification
    signal, so substring spoofing cannot forge a balance/quota or refresh-token
    failure into a route circuit.
    """

    sealed = process.get("provider_error")
    if not isinstance(sealed, Mapping):
        return None
    if str(sealed.get("owner") or "").strip().casefold() != "provider":
        return None
    if sealed.get("sealed") is not True:
        return None
    return sealed


def _sealed_error_kind(sealed: Mapping[str, Any]) -> str:
    code = str(sealed.get("code") or "").strip().casefold()
    status = sealed.get("http_status")
    status_code = (
        status if isinstance(status, int) and not isinstance(status, bool) else 0
    )
    if status_code == 402 or code in _ROUTE_QUOTA_ERROR_CODES:
        return "quota"
    if status_code in {401, 403} or code in _ROUTE_AUTH_ERROR_CODES:
        return "auth"
    return ""


def _route_failure_kind(process: Mapping[str, Any]) -> str:
    sealed = _sealed_provider_error(process)
    if sealed is not None:
        sealed_kind = _sealed_error_kind(sealed)
        if sealed_kind:
            return sealed_kind
    # Auth/quota hard-failure kinds are classified only from the sealed,
    # provider-owned structured error handled above.  The free-form ``error``
    # string is never scanned for auth/quota markers, so assistant/model prose
    # or substring spoofing cannot forge a single-failure circuit trip; only
    # unauthenticated transport signals fall through to the multi-failure
    # transient threshold below.
    state = str(process.get("state") or "").strip().casefold()
    error = str(process.get("error") or "").strip().casefold()
    if state in {"launch_failed", "timed_out"}:
        return "transient"
    if state == "worker_failed" and any(
        marker in error for marker in _ROUTE_TRANSIENT_MARKERS
    ):
        return "transient"
    return ""


def _route_circuit(
    matched: Iterable[Mapping[str, Any]], *, now_epoch: float,
) -> dict[str, Any]:
    """Derive one exact adapter+model circuit from durable process evidence.

    Validation/finalization failures are route successes: the provider
    returned a usable result and must not be penalized for a downstream gate.
    Only authenticated route failures and repeated transport failures count.
    The circuit is advisory/selection-local; it never mutates or disables the
    shared MCP control plane.
    """

    observed = [
        (epoch, process)
        for process in matched
        if (epoch := _process_event_epoch(process)) is not None
        and 0.0 <= now_epoch - epoch <= ROUTE_CIRCUIT_LOOKBACK_SECONDS
    ]
    observed.sort(key=lambda item: item[0], reverse=True)
    consecutive = 0
    latest_kind = ""
    latest_failure_epoch: float | None = None
    for epoch, process in observed:
        kind = _route_failure_kind(process)
        state = str(process.get("state") or "").strip().casefold()
        if kind:
            consecutive += 1
            latest_kind = latest_kind or kind
            latest_failure_epoch = (
                epoch if latest_failure_epoch is None else latest_failure_epoch
            )
            if kind in _ROUTE_SINGLE_FAILURE_KINDS:
                break
            continue
        if state in _ROUTE_SUCCESS_STATES:
            break

    threshold = (
        1 if latest_kind in _ROUTE_SINGLE_FAILURE_KINDS
        else ROUTE_CIRCUIT_TRANSIENT_THRESHOLD
    )
    failure_age = (
        max(0.0, now_epoch - latest_failure_epoch)
        if latest_failure_epoch is not None else None
    )
    tripped = bool(latest_kind and consecutive >= threshold)
    open_now = bool(
        tripped
        and failure_age is not None
        and failure_age < ROUTE_CIRCUIT_COOLDOWN_SECONDS
    )
    return {
        "schema_id": "aiworkhub.route_failure_circuit.v1",
        "scope": "exact_adapter_and_model",
        "state": "open" if open_now else ("half_open" if tripped else "closed"),
        "failure_kind": latest_kind,
        "consecutive_failures": consecutive,
        "threshold": threshold,
        "latest_failure_age_seconds": failure_age,
        "cooldown_seconds": ROUTE_CIRCUIT_COOLDOWN_SECONDS,
        "mcp_control_plane_affected": False,
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return float(ordered[index])


def _canonical_cards(root: Path) -> list[dict[str, Any]]:
    return task_store.list_task_cards(root, limit=5000)


def _resolve_effective_adapter(
    worker: Mapping[str, Any],
    ready_by_adapter: Mapping[str, Mapping[str, Any]],
) -> tuple[str, Mapping[str, Any]]:
    def launchable(adapter_id: str, readiness: Mapping[str, Any]) -> bool:
        if not readiness.get("launchable"):
            return False
        observed = readiness.get("observed_models")
        model = str(worker.get("model") or "")
        if adapter_id == "codex_cli":
            # An installed Codex binary proves neither account access nor
            # model entitlement. Require a current authenticated capability
            # receipt for this exact model; historical outcomes remain useful
            # scoring evidence but cannot override present route authority.
            if readiness.get("access_observed") is not True:
                return False
            if not isinstance(observed, list):
                return False
            return model in {
                str(value) for value in observed if isinstance(value, str)
            }
        if adapter_id not in {
            "vscode_lm",
            "deepseek_vscode_lm",
            "glm_vscode_lm",
        }:
            return True
        # Older/fake preflight receipts have no catalog. Preserve their
        # explicit launchable verdict; current receipts always include a
        # bounded observed_models list and therefore get exact model gating.
        if not isinstance(observed, list):
            return True
        visible = {str(value) for value in observed if isinstance(value, str)}
        accepted = {model, *_EDITOR_MODEL_ALIASES.get(model, ())}
        return bool(visible.intersection(accepted))

    declared = str(worker.get("adapter_id") or "")
    candidate = ready_by_adapter.get(declared, {})
    if launchable(declared, candidate):
        return declared, candidate
    for fallback in _WORKER_ADAPTER_FALLBACKS.get(declared, ()):
        candidate = ready_by_adapter.get(fallback, {})
        if launchable(fallback, candidate):
            return fallback, candidate
    unavailable = dict(ready_by_adapter.get(declared, {}))
    if unavailable.get("launchable"):
        unavailable["transport_launchable"] = True
    unavailable["launchable"] = False
    if declared == "codex_cli":
        unavailable["status"] = "model_access_unverified"
        unavailable["reason"] = "codex_model_capability_not_observed"
    return declared, unavailable


def _discovered_family_models(
    worker: Mapping[str, Any],
    ready_by_adapter: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Return the editor-discovered models that belong to ``worker``'s family.

    The names come from the live editor readiness (``observed_models``), held to
    the shared requested-name regex and to the seed's provider-family stem so a
    GLM seed only ever fans out into GLM models the editor actually reported.
    No model name is enumerated here.
    """

    readiness = ready_by_adapter.get(str(worker.get("adapter_id") or ""), {})
    observed = readiness.get("observed_models")
    if not isinstance(observed, list):
        return []
    stem_match = re.match(r"[a-z]+", str(worker.get("model") or "").lower())
    stem = stem_match.group(0) if stem_match else ""
    discovered: list[str] = []
    for value in observed:
        name = str(value).strip()
        if not name or not runtime_adapters.editor_requested_name_ok(name):
            continue
        normalized = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if stem and not normalized.startswith(stem):
            continue
        if name not in discovered:
            discovered.append(name)
    return discovered


def _expand_discovered_workers(
    workers: Iterable[Mapping[str, Any]],
    ready_by_adapter: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project each editor-discovered seed into one row per discovered model.

    Configuration is populated from what VS Code reported, never by hand: a
    model newly exposed by the endpoint appears as its own row without a code
    change. A seed whose own declared model the editor did not report is
    preserved (readiness marks it unavailable by name) rather than dropped, and
    a seed for an adapter with no live catalog stays a single row (cold start).
    """

    expanded: list[dict[str, Any]] = []
    for worker in workers:
        row_worker = dict(worker)
        if str(row_worker.get("adapter_id") or "") not in _DISCOVERY_ADAPTERS:
            expanded.append(row_worker)
            continue
        discovered = _discovered_family_models(row_worker, ready_by_adapter)
        if not discovered:
            expanded.append(row_worker)
            continue
        seed_model = str(row_worker.get("model") or "")
        seed_worker_id = str(row_worker.get("worker_id") or "")
        for name in discovered:
            projected = dict(row_worker, model=name)
            projected["worker_id"] = seed_worker_id if name == seed_model else name
            projected["discovered_from_editor"] = True
            expanded.append(projected)
        if seed_model and seed_model not in discovered:
            preserved = dict(row_worker)
            preserved["discovered_from_editor"] = False
            expanded.append(preserved)
    return expanded


def build_catalog(
    repo_root: Path | str,
    *,
    cards: Iterable[Mapping[str, Any]] | None = None,
    process_rows: Iterable[Mapping[str, Any]] | None = None,
    usage_rows: Iterable[Mapping[str, Any]] | None = None,
    preflight: Mapping[str, Any] | None = None,
    cost_per_accepted_outcome: Mapping[str, Any] | None = None,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    catalog = load_catalog(root)
    model_policy = model_settings.load(root)
    task_cards = [dict(item) for item in (cards if cards is not None else _canonical_cards(root))]
    processes = [dict(item) for item in (process_rows or [])]
    usage = [dict(item) for item in (usage_rows or [])]
    card_by_task = {str(item.get("task_id") or ""): item for item in task_cards}
    identity_recovered = 0
    for process in processes:
        task_id = str(process.get("task_id") or "")
        card = card_by_task.get(task_id) or {}
        terminal = card.get("terminal_review") if isinstance(card, Mapping) else {}
        evidence = terminal.get("evidence") if isinstance(terminal, Mapping) else {}
        recovered_fields: list[str] = []
        if not str(process.get("model") or "") and isinstance(evidence, Mapping):
            model = str(evidence.get("model") or "")
            if model:
                process["model"] = model
                recovered_fields.append("model")
        if not str(process.get("adapter_id") or "") and isinstance(evidence, Mapping):
            adapter = str(evidence.get("adapter_id") or "")
            if adapter:
                process["adapter_id"] = adapter
                recovered_fields.append("adapter_id")
        if not str(process.get("runner") or ""):
            runner = str(card.get("runner") or "") if isinstance(card, Mapping) else ""
            if runner:
                process["runner"] = runner
                recovered_fields.append("runner")
        if recovered_fields:
            process["identity_evidence_source"] = "canonical_terminal_review"
            process["identity_recovered_fields"] = recovered_fields
            identity_recovered += 1
    readiness = preflight or repo_policy.build_preflight(root)
    ready_by_adapter = {
        str(item.get("adapter_id") or ""): item
        for item in readiness.get("providers") or []
        if isinstance(item, Mapping)
    }
    observed_now_epoch = (
        float(now_epoch)
        if now_epoch is not None
        else datetime.now(timezone.utc).timestamp()
    )
    economic_view = dict(
        cost_per_accepted_outcome
        or cost_ledger.cost_per_accepted_outcome_view(usage, {}, task_cards)
    )
    economics_by_model = economic_view.get("routes")
    if not isinstance(economics_by_model, Mapping):
        economics_by_model = {}
    rows: list[dict[str, Any]] = []
    attributed_process_ids: set[int] = set()
    for worker in _expand_discovered_workers(catalog["workers"], ready_by_adapter):
        effective_adapter, adapter_ready = _resolve_effective_adapter(
            worker, ready_by_adapter
        )
        adapter_ids_to_match = {worker["adapter_id"], effective_adapter}
        matched: list[dict[str, Any]] = []
        for index, process in enumerate(processes):
            if str(process.get("adapter_id") or "") not in adapter_ids_to_match:
                continue
            if str(process.get("model") or "") != worker["model"]:
                continue
            matched.append(process)
            attributed_process_ids.add(index)
        task_ids = {str(item.get("task_id") or "") for item in matched if item.get("task_id")}
        runners = {str(item.get("runner") or "") for item in matched if item.get("runner")}
        matched_usage = [
            item for item in usage
            if str(item.get("task_id") or "") in task_ids
            and (
                not str(item.get("model") or "")
                or str(item.get("model") or "") == worker["model"]
            )
            and (
                not runners
                or not str(item.get("runner") or "")
                or str(item.get("runner") or "") in runners
            )
        ]
        matched_cards = [card_by_task[task_id] for task_id in task_ids if task_id in card_by_task]
        # The canonical taxonomy (aiworkhub.learning_commit.FailureCategory) is
        # the single source of truth for "this finalized task's failure is
        # infrastructure, not a candidate-code failure" -- classification
        # reuses core.classify_terminal_disposition (the exact function the
        # learning-commit path calls) so code-quality rates and
        # learning-commit classification can never disagree, and so a sealed
        # provider diagnostic (e.g. a dependency/route failure hidden behind
        # an otherwise-successful terminal substatus) is excluded here too.
        infrastructure_cards = [
            item for item in matched_cards
            if core.classify_terminal_disposition(item)
            in learning_commit.INFRASTRUCTURE_FAILURE_CATEGORIES
        ]
        quality_cards = [
            item for item in matched_cards if item not in infrastructure_cards
        ]
        sample_count = len(quality_cards)
        accepted = sum(
            1 for item in quality_cards
            if str(item.get("status") or "") == "finished"
        )
        review_ready = sum(
            1 for item in quality_cards
            if str(item.get("status") or "") in {"review", "finished"}
        )
        failed = 0
        for item in quality_cards:
            verification = item.get("deterministic_verification")
            verification_failed = (
                isinstance(verification, Mapping) and verification.get("passed") is False
            )
            # Same single source of truth as infrastructure_cards above: a
            # quality card only counts as a code-quality failure when the
            # canonical taxonomy classifies it CANDIDATE_CODE and it was not
            # itself manager-accepted (an accepted review_ready card is a
            # success, not a failure) -- never a private literal here.
            code_quality_failure = (
                str(item.get("status") or "") != "finished"
                and core.classify_terminal_disposition(item)
                in learning_commit.CODE_QUALITY_FAILURE_CATEGORIES
            )
            if code_quality_failure or verification_failed:
                failed += 1
        latencies = [
            value
            for value in (_iso_seconds(item.get("started_at"), item.get("finished_at")) for item in matched)
            if value is not None
        ]
        attempts = len(matched)
        retries = max(0, attempts - len(matched_cards))
        usage_source = matched_usage or matched
        tokens = sum(int(item.get("total_tokens") or 0) for item in usage_source)
        def cost_known(item: Mapping[str, Any]) -> bool:
            declared = item.get("cost_known")
            if declared is not None:
                return declared is True
            return float(item.get("cost_usd") or 0.0) > 0.0

        known_cost_rows = [item for item in usage_source if cost_known(item)]
        known_cost_tokens = sum(
            int(item.get("total_tokens") or 0) for item in known_cost_rows
        )
        cost = sum(float(item.get("cost_usd") or 0.0) for item in known_cost_rows)
        unknown_cost_tokens = sum(
            int(item.get("total_tokens") or 0)
            for item in usage_source
            if not cost_known(item)
        )
        accepted_rate = accepted / sample_count if sample_count else None
        review_rate = review_ready / sample_count if sample_count else None
        failure_rate = failed / sample_count if sample_count else None
        retry_rate = retries / attempts if attempts else None
        discipline_scores = []
        for process in matched:
            infra = process.get("ai_infra_context")
            tool_use = infra.get("tool_use") if isinstance(infra, Mapping) else None
            discipline = (
                tool_use.get("tool_discipline")
                if isinstance(tool_use, Mapping)
                else None
            )
            score = discipline.get("score") if isinstance(discipline, Mapping) else None
            if isinstance(score, (int, float)):
                discipline_scores.append(max(0.0, min(100.0, float(score))))
        tool_discipline_score = (
            round(sum(discipline_scores) / len(discipline_scores), 2)
            if discipline_scores else None
        )
        effective = (
            max(0.0, min(accepted_rate or 0.0, review_rate or 0.0) - (failure_rate or 0.0))
            if sample_count else None
        )
        observed_score = (
            round(100.0 * (0.8 * effective + 0.2 * (1.0 - (retry_rate or 0.0))), 2)
            if effective is not None else None
        )
        effective_score = max(0.0, min(100.0, (observed_score if observed_score is not None else 50.0) + worker["manager_score_adjustment"]))
        access_observed = bool(adapter_ready.get("access_observed"))
        route_health = _route_circuit(matched, now_epoch=observed_now_epoch)
        route_available = route_health["state"] != "open"
        policy_provider, policy_adapter = policy_route_identity(
            worker["provider"], effective_adapter
        )
        route_policy_enabled = model_settings.evaluate_state(
            model_policy,
            provider=policy_provider,
            adapter=policy_adapter,
            model=worker["model"],
        )
        # Preserve 0.10.24 vendor-level settings while adding the explicit
        # transport-owner gate.  A legacy disabled vendor remains fail-closed;
        # a newly disabled Copilot group now also blocks every editor route.
        vendor_policy_enabled = model_settings.evaluate_state(
            model_policy,
            provider=worker["provider"],
            adapter=effective_adapter,
            model=worker["model"],
        )
        policy_enabled = route_policy_enabled and vendor_policy_enabled
        effective_enabled = bool(worker["enabled"] and policy_enabled)
        model_routes = economics_by_model.get(worker["model"])
        if not isinstance(model_routes, Mapping):
            model_routes = {}
        model_economics = model_routes.get(effective_adapter)
        if not isinstance(model_economics, Mapping):
            model_economics = model_routes.get(worker["adapter_id"])
        if not isinstance(model_economics, Mapping):
            model_economics = {}
        rows.append({
            "execution_runner": execution_runner(
                worker["worker_id"], effective_adapter
            ),
            **worker,
            "enabled": effective_enabled,
            "policy_enabled": policy_enabled,
            "policy_provider": policy_provider,
            "policy_adapter": policy_adapter,
            "vendor_policy_enabled": vendor_policy_enabled,
            "effective_adapter_id": effective_adapter,
            "adapter_fallback_used": effective_adapter != worker["adapter_id"],
            "available": bool(
                effective_enabled
                and adapter_ready.get("launchable")
                and route_available
            ),
            "availability_observed": access_observed or bool(sample_count > 0),
            "readiness_status": (
                "route_circuit_open"
                if not route_available
                else str(adapter_ready.get("status") or "unobserved")
            ),
            "route_health": route_health,
            "quota_observed": False,
            "quota_state": "unavailable_from_provider_api",
            "outcomes": {
                "sample_count": sample_count,
                "attempted_task_count": len(matched_cards),
                "infrastructure_failure_count": len(infrastructure_cards),
                "attempt_count": attempts,
                "retry_count": retries,
                "accepted_rate": accepted_rate,
                "review_ready_rate": review_rate,
                "validation_failure_rate": failure_rate,
                "retry_rate": retry_rate,
                "p50_latency_seconds": median(latencies) if latencies else None,
                "p95_latency_seconds": _percentile(latencies, 0.95),
                "total_tokens": tokens,
                "estimated_tokens_per_attempt": (
                    round(tokens / attempts) if tokens and attempts else None
                ),
                "tool_discipline_score": tool_discipline_score,
                "tool_discipline_samples": len(discipline_scores),
                "tool_discipline_role": "observational_tiebreaker_only",
                "cost_usd": round(cost, 6) if cost else None,
                "cost_known_records": len(known_cost_rows),
                "cost_unknown_records": len(usage_source) - len(known_cost_rows),
                "tokens_with_known_cost": known_cost_tokens,
                "tokens_with_unknown_cost": unknown_cost_tokens,
                "cost_usd_per_1k_tokens": (
                    round(cost * 1000.0 / known_cost_tokens, 6)
                    if cost and known_cost_tokens else None
                ),
                "evidence_source": "observed" if sample_count else "conservative_prior",
            },
            "cost_per_accepted_outcome": dict(model_economics),
            "observed_score": observed_score,
            "effective_score": round(effective_score, 2),
        })
    unattributed = [
        process for index, process in enumerate(processes)
        if index not in attributed_process_ids
    ]
    missing_model = sum(1 for process in unattributed if not str(process.get("model") or ""))
    return {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "revision": catalog["revision"],
        "configured": bool(catalog.get("configured")),
        "workers": rows,
        "summary": {
            "workers": len(rows),
            "enabled": sum(1 for item in rows if item["enabled"]),
            "available": sum(1 for item in rows if item["available"]),
            "observed": sum(1 for item in rows if item["outcomes"]["sample_count"]),
            "unattributed_process_rows": len(unattributed),
            "unattributed_missing_model_rows": missing_model,
            "unattributed_unknown_adapter_or_model_rows": len(unattributed) - missing_model,
            "process_identity_recovered_rows": identity_recovered,
        },
        "truth_contract": {
            "provider_quota_fabricated": False,
            "missing_outcomes_use_labeled_prior": True,
            "manager_adjustment_range": [-20.0, 20.0],
            "economic_routing_is_advisory_only": True,
            "unknown_cost_never_ranks_as_free": True,
            "repository_model_policy_enforced": True,
        },
    }


def rank_task(repo_root: Path | str, task: workforce_router.TaskRequirements, *, catalog: Mapping[str, Any] | None = None) -> dict[str, Any]:
    snapshot = dict(catalog or build_catalog(repo_root))
    workers: list[workforce_router.WorkerCapability] = []
    requested_families = [
        family
        for family in ("code", "research", "linguistic", "review", "mechanical")
        if family in task.kinds
    ]
    requested_family = (
        requested_families[0] if len(requested_families) == 1 else "unknown"
    )
    for item in snapshot.get("workers") or []:
        outcomes = item.get("outcomes") if isinstance(item, Mapping) else {}
        if not isinstance(outcomes, Mapping):
            outcomes = {}
        economic_by_family = item.get("cost_per_accepted_outcome") if isinstance(item, Mapping) else {}
        if not isinstance(economic_by_family, Mapping):
            economic_by_family = {}
        economics = economic_by_family.get(requested_family)
        if not isinstance(economics, Mapping):
            economics = {}
        economics = economics.get(task.risk)
        if not isinstance(economics, Mapping):
            economics = {}
        workers.append(workforce_router.WorkerCapability.build(
            worker_id=item["worker_id"], adapter_id=item.get("effective_adapter_id") or item["adapter_id"], model=item["model"], provider=item["provider"],
            supports=item["supports"], tools=item["tools"], max_context_tokens=item["max_context_tokens"],
            max_risk=item["max_risk"], quality_ceiling=item["quality_ceiling"],
            available=bool(item["available"]), credential_ok=bool(item["available"]), quota_available=None,
            manager_score_adjustment=float(item.get("manager_score_adjustment") or 0.0),
            evidence=workforce_router.OutcomeEvidence(
                accepted_rate=outcomes.get("accepted_rate"), review_ready_rate=outcomes.get("review_ready_rate"),
                validation_failure_rate=outcomes.get("validation_failure_rate"), p50_latency_seconds=outcomes.get("p50_latency_seconds"),
                p95_latency_seconds=outcomes.get("p95_latency_seconds"), cost_usd_per_1k_tokens=outcomes.get("cost_usd_per_1k_tokens"),
                estimated_tokens=outcomes.get("estimated_tokens_per_attempt"),
                tool_discipline_score=outcomes.get("tool_discipline_score"),
                cost_per_accepted_outcome_usd=economics.get("cost_per_accepted_outcome_usd"),
                economic_evidence_state=str(economics.get("state") or "UNMEASURED"),
                economic_matched_tasks=int(economics.get("matched_decided_tasks") or 0),
                economic_accepted_outcomes=int(economics.get("accepted_outcomes") or 0),
                economic_cost_coverage=economics.get("cost_coverage"),
                sample_count=int(outcomes.get("sample_count") or 0),
            ),
        ))
    raw_decision = workforce_router.rank_workforce(task, workers)
    decision = raw_decision.as_dict()
    decision["economic_advisory"] = workforce_router.economic_advisory(raw_decision)
    decision["economic_advisory"]["task_family"] = requested_family
    decision["economic_advisory"]["risk_tier"] = task.risk
    by_worker = {
        str(item.get("worker_id") or ""): item
        for item in snapshot.get("workers") or []
        if isinstance(item, Mapping)
    }
    for candidate in decision.get("candidates") or []:
        item = by_worker.get(str(candidate.get("worker_id") or ""), {})
        candidate["execution_runner"] = execution_runner(
            str(candidate.get("worker_id") or ""),
            str(candidate.get("adapter_id") or item.get("effective_adapter_id") or item.get("adapter_id") or ""),
        )
    selected_worker_id = str(decision.get("selected_worker_id") or "")
    selected_runner = ""
    if selected_worker_id:
        selected_runner = execution_runner(
            selected_worker_id,
            str(decision.get("selected_adapter_id") or ""),
        )
    decision["selected_execution_runner"] = selected_runner or None
    decision["launch_contract"] = (
        {
            "runner": selected_runner,
            "adapter_id": decision.get("selected_adapter_id"),
            "model": decision.get("selected_model"),
            "task_id": task.task_id,
            "identity_rule": "use_same_runner_for_task_create_and_agent_launch_task",
        }
        if selected_runner
        else None
    )
    return decision


__all__ = ["AUDIT_RELATIVE_PATH", "CATALOG_RELATIVE_PATH", "DEFAULT_WORKERS", "SCHEMA_ID", "WorkforceCatalogError", "build_catalog", "catalog_path", "ensure_catalog", "execution_runner", "load_catalog", "rank_task", "upsert_worker", "validate_catalog"]
