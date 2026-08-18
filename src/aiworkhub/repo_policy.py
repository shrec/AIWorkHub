"""Repository-local launch policy and unified environment preflight.

The policy is data, never executable configuration: command rules are fixed
tokens, validations are named checks, and retention values are bounded
integers.  No shell fragment, Python expression, host path, or credential is
accepted or returned.
"""

from __future__ import annotations

import json
import os
import re
import stat
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from . import (
    claude_auth,
    quality_evidence,
    runtime_adapters,
    source_graph_daemon,
    task_store,
    worker_workspace,
    workspace_hygiene,
)

try:
    from . import deepseek_credentials, glm_credentials, vscode_lm_bridge
except ImportError:  # packaged/minimal runtime: report unavailable, never guess
    deepseek_credentials = None  # type: ignore[assignment]
    glm_credentials = None  # type: ignore[assignment]
    vscode_lm_bridge = None  # type: ignore[assignment]


SCHEMA_ID = "aiworkhub.repo_policy.v1"
PREFLIGHT_SCHEMA_ID = "aiworkhub.environment_preflight.v1"
POLICY_RELATIVE_PATH = Path(".aiworkhub/config/policy.json")
MAX_POLICY_BYTES = 64 * 1024
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
MANDATORY_RAW_DISCOVERY_DENIES = ("grep", "rg", "find", "tree")

# Workforce readiness vocabulary. A route must never be labeled "ready" when
# the provider quota that gates it has not actually been observed; the
# distinct value below lets a caller reading readiness alone route honestly.
READINESS_READY = "ready"
READINESS_READY_UNVERIFIED = "ready_unverified"
QUOTA_STATE_UNAVAILABLE = "unavailable_from_provider_api"

DEFAULT_POLICY: dict[str, Any] = {
    "schema_id": SCHEMA_ID,
    "providers": {"allowed_adapters": list(runtime_adapters.LOCAL_ADAPTERS)},
    "tools": {
        "source_graph_required_for_code": True,
        "session_memory_kb_required_for_nontrivial": True,
        "raw_discovery_forbidden": list(MANDATORY_RAW_DISCOVERY_DENIES),
    },
    "validation": {"required_check_ids": []},
    "retention": {
        "logs_days": 7,
        "terminal_runs_days": 30,
        "archived_tasks_days": 90,
        "source_graph_generations": 3,
        "worktree_max_bytes": 5 * 1024 * 1024 * 1024,
    },
}


class RepoPolicyError(RuntimeError):
    """A malformed or unsafe repository policy."""


def policy_path(repo_root: Path | str) -> Path:
    return Path(repo_root).resolve() / POLICY_RELATIVE_PATH


def _string_list(value: Any, field: str, *, maximum: int = 64) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise RepoPolicyError(f"{field}_must_be_bounded_string_list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _TOKEN_RE.fullmatch(item):
            raise RepoPolicyError(f"{field}_contains_invalid_token")
        if item not in result:
            result.append(item)
    return result


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise RepoPolicyError(f"{field}_out_of_range")
    return value


def validate_policy(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RepoPolicyError("policy_must_be_object")
    if value.get("schema_id") != SCHEMA_ID:
        raise RepoPolicyError("policy_schema_id_invalid")
    providers = value.get("providers")
    tools = value.get("tools")
    validation = value.get("validation")
    retention = value.get("retention")
    if not all(isinstance(item, Mapping) for item in (providers, tools, validation, retention)):
        raise RepoPolicyError("policy_sections_invalid")

    allowed = _string_list(providers.get("allowed_adapters"), "allowed_adapters")
    unsupported = sorted(set(allowed) - set(runtime_adapters.LOCAL_ADAPTERS))
    if unsupported or not allowed:
        raise RepoPolicyError("allowed_adapters_unsupported_or_empty")
    raw_denies = _string_list(tools.get("raw_discovery_forbidden"), "raw_discovery_forbidden")
    if not set(MANDATORY_RAW_DISCOVERY_DENIES).issubset(raw_denies):
        raise RepoPolicyError("mandatory_raw_discovery_denies_missing")
    for field in ("source_graph_required_for_code", "session_memory_kb_required_for_nontrivial"):
        if not isinstance(tools.get(field), bool):
            raise RepoPolicyError(f"{field}_must_be_bool")
    required_checks = _string_list(validation.get("required_check_ids"), "required_check_ids")
    return {
        "schema_id": SCHEMA_ID,
        "providers": {"allowed_adapters": allowed},
        "tools": {
            "source_graph_required_for_code": tools["source_graph_required_for_code"],
            "session_memory_kb_required_for_nontrivial": tools[
                "session_memory_kb_required_for_nontrivial"
            ],
            "raw_discovery_forbidden": raw_denies,
        },
        "validation": {"required_check_ids": required_checks},
        "retention": {
            "logs_days": _bounded_int(retention.get("logs_days"), "logs_days", 1, 7),
            "terminal_runs_days": _bounded_int(
                retention.get("terminal_runs_days"), "terminal_runs_days", 1, 365
            ),
            "archived_tasks_days": _bounded_int(
                retention.get(
                    "archived_tasks_days",
                    DEFAULT_POLICY["retention"]["archived_tasks_days"],
                ),
                "archived_tasks_days",
                7,
                3650,
            ),
            "source_graph_generations": _bounded_int(
                retention.get("source_graph_generations"),
                "source_graph_generations",
                1,
                20,
            ),
            "worktree_max_bytes": _bounded_int(
                retention.get(
                    "worktree_max_bytes",
                    DEFAULT_POLICY["retention"]["worktree_max_bytes"],
                ),
                "worktree_max_bytes",
                64 * 1024 * 1024,
                1024 * 1024 * 1024 * 1024,
            ),
        },
    }


def load_policy(repo_root: Path | str) -> dict[str, Any]:
    path = policy_path(repo_root)
    if not path.exists():
        return {**validate_policy(deepcopy(DEFAULT_POLICY)), "configured": False}
    try:
        info = path.lstat()
    except OSError as exc:
        raise RepoPolicyError(f"policy_unreadable:{type(exc).__name__}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RepoPolicyError("policy_must_be_regular_file")
    if info.st_size > MAX_POLICY_BYTES:
        raise RepoPolicyError("policy_too_large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepoPolicyError(f"policy_invalid_json:{type(exc).__name__}") from exc
    return {**validate_policy(value), "configured": True}


def ensure_policy(repo_root: Path | str) -> tuple[Path, bool]:
    """Create the immutable-safe default policy once; never overwrite edits."""
    root = Path(repo_root).resolve()
    if not (root / ".aiworkhub/project.json").is_file():
        raise RepoPolicyError("repository_not_initialized")
    path = policy_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        load_policy(root)
        return path, False
    payload = (json.dumps(DEFAULT_POLICY, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        load_policy(root)
        return path, False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    load_policy(root)
    return path, True


def _declared_check_ids(repo_root: Path) -> tuple[list[str], str]:
    try:
        config = quality_evidence.load_repo_config(repo_root)
    except quality_evidence.MalformedConfigError as exc:
        return [], str(exc)[:300]
    return [str(item.get("id")) for item in config.get("checks") or []], ""


def validate_launch(repo_root: Path | str, card: Mapping[str, Any], adapter_id: str) -> dict[str, Any]:
    """Apply policy before claim/start; returns a bounded, deterministic verdict."""
    root = Path(repo_root).resolve()
    try:
        policy = load_policy(root)
    except RepoPolicyError as exc:
        return {"ok": False, "reason": f"repo_policy_invalid:{exc}"}
    if adapter_id not in policy["providers"]["allowed_adapters"]:
        return {"ok": False, "reason": f"adapter_denied_by_repo_policy:{adapter_id}"}
    if card.get("callback_required") and (
        not card.get("callback_supported") or not str(card.get("origin_thread_id") or "").strip()
    ):
        return {"ok": False, "reason": "repo_policy_callback_route_required"}

    initialized = (root / ".aiworkhub/project.json").is_file()
    context = card.get("project_context")
    task_type = str(context.get("task_type") or "code") if isinstance(context, Mapping) else "code"
    is_code = task_type == "code" and bool(card.get("allowed_writes"))
    policy_applies_to_context = bool(policy["configured"]) or card.get("schema_id") == "aiworkhub.task_card.v1"
    if (
        initialized
        and policy_applies_to_context
        and is_code
        and policy["tools"]["source_graph_required_for_code"]
    ):
        source = context.get("source_graph") if isinstance(context, Mapping) else None
        if not isinstance(source, Mapping) or source.get("required") is not True:
            return {"ok": False, "reason": "repo_policy_source_graph_required_for_code"}

    declared, config_error = _declared_check_ids(root)
    if config_error:
        return {"ok": False, "reason": "repo_policy_quality_config_invalid"}
    missing = sorted(set(policy["validation"]["required_check_ids"]) - set(declared))
    if missing:
        return {"ok": False, "reason": "repo_policy_required_checks_missing:" + ",".join(missing)}
    return {"ok": True, "reason": "ready", "configured": bool(policy["configured"])}


_VSCODE_LM_IN_PROCESS_ADAPTERS = frozenset(
    {
        runtime_adapters.VSCODE_LM_ADAPTER,
        runtime_adapters.DEEPSEEK_VSCODE_LM_ADAPTER,
        runtime_adapters.GLM_VSCODE_LM_ADAPTER,
    }
)


def _is_windows_host() -> bool:
    return os.name == "nt"


def _provider_status(
    repo_root: Path,
    adapter_id: str,
    policy: Mapping[str, Any],
    sandbox_backend: str,
    sandbox_error: str,
) -> dict[str, Any]:
    resolution = runtime_adapters.resolve_executable(adapter_id)
    policy_allowed = adapter_id in policy["providers"]["allowed_adapters"]
    native_windows_cli_without_broker = (
        _is_windows_host()
        and adapter_id not in _VSCODE_LM_IN_PROCESS_ADAPTERS
        and sandbox_backend != "windows_appcontainer"
    )
    result: dict[str, Any] = {
        "adapter_id": adapter_id,
        "policy_allowed": policy_allowed,
        # Coverage describes routes this host and repository can actually
        # support. Windows native CLI routes remain fail-closed until an
        # AppContainer-grade broker exists, but they must not make healthy
        # editor-hosted routes look degraded merely by being in the portable
        # adapter catalog. Explicitly policy-denied routes are likewise not a
        # required coverage target.
        "coverage_required": bool(policy_allowed and not native_windows_cli_without_broker),
        "platform_excluded": bool(native_windows_cli_without_broker),
        "installed": bool(resolution.ok),
        "launchable": bool(resolution.ok),
        "access_observed": False,
        # No credential/auth helper can observe a provider's metered quota
        # today, so the honest default is unobserved until a helper reports it.
        "quota_observed": False,
        "quota_state": QUOTA_STATE_UNAVAILABLE,
        "status": "installed_unverified_access" if resolution.ok else "not_installed",
        "reason": str(resolution.reason or "")[:200],
    }
    readiness: Mapping[str, Any] | None = None
    try:
        if adapter_id == runtime_adapters.DEEPSEEK_COPILOT_ADAPTER and deepseek_credentials:
            readiness = deepseek_credentials.credential_status(repo=repo_root)
        elif adapter_id == runtime_adapters.GLM_COPILOT_ADAPTER and glm_credentials:
            readiness = glm_credentials.credential_status(repo=repo_root)
        elif adapter_id == runtime_adapters.DEEPSEEK_VSCODE_LM_ADAPTER and vscode_lm_bridge:
            readiness = vscode_lm_bridge.bridge_readiness(
                repo_root,
                model=runtime_adapters.DEEPSEEK_DEFAULT_MODEL,
                adapter_id=adapter_id,
            )
        elif adapter_id == runtime_adapters.GLM_VSCODE_LM_ADAPTER and vscode_lm_bridge:
            readiness = vscode_lm_bridge.bridge_readiness(
                repo_root,
                model=runtime_adapters.GLM_DEFAULT_MODEL,
                adapter_id=adapter_id,
            )
        elif adapter_id == runtime_adapters.VSCODE_LM_ADAPTER and vscode_lm_bridge:
            readiness = vscode_lm_bridge.bridge_readiness(
                repo_root,
                model=None,
                adapter_id=adapter_id,
            )
        elif adapter_id == "claude_cli":
            readiness = claude_auth.auth_status(resolution.executable)
    except (OSError, RuntimeError, ValueError):
        readiness = None
    if isinstance(readiness, Mapping):
        if adapter_id in _VSCODE_LM_IN_PROCESS_ADAPTERS:
            # Editor visibility/host presence is not consent or a completed
            # provider turn; the bridge must report its explicit observation.
            result["access_observed"] = bool(readiness.get("access_observed"))
        else:
            # Native credential/auth helpers have already validated their
            # exact local credential/subscription boundary before declaring
            # launchable. They do not use the editor consent state machine.
            result["access_observed"] = bool(
                readiness.get("access_observed")
                or readiness.get("authenticated")
                or readiness.get("credential_present")
                or readiness.get("launchable")
            )
        result["launchable"] = bool(resolution.ok and readiness.get("launchable"))
        # Quota is a separate axis from credential/access observation. A route
        # that is launchable and whose access is observed may still be
        # unverifiable because the provider's quota was never observed. Such a
        # route must not be labeled "ready"; it is "ready_unverified", and the
        # quota reason travels alongside the readiness value.
        quota_observed = bool(readiness.get("quota_observed"))
        quota_state = str(readiness.get("quota_state") or QUOTA_STATE_UNAVAILABLE)[:128]
        result["quota_observed"] = quota_observed
        result["quota_state"] = quota_state
        if result["launchable"] and result["access_observed"]:
            result["status"] = (
                READINESS_READY if quota_observed else READINESS_READY_UNVERIFIED
            )
        elif result["launchable"] and readiness.get("consent_required"):
            result["status"] = "consent_required"
        else:
            result["status"] = "access_unavailable"
        result["reason"] = str(readiness.get("blocker_reason") or readiness.get("reason") or "")[:200]
        if result["status"] == READINESS_READY_UNVERIFIED:
            result["reason"] = f"quota_unobserved:{quota_state}"
        # Preserve only bounded, secret-free broker observability.  The UI can
        # then report real editor model capacity instead of presenting
        # redundant transports as if they were distinct models.
        for key in (
            "window_id",
            "host_count",
            "live_host_count",
            "stale_host_count",
            "freshest_age_seconds",
            "access_state",
            "consent_required",
        ):
            value = readiness.get(key)
            if value is not None:
                result[key] = value
        observed_models = readiness.get("observed_models")
        if isinstance(observed_models, list):
            result["observed_models"] = [
                str(value)[:128]
                for value in observed_models[:128]
                if isinstance(value, str) and value
            ]
    if adapter_id in _VSCODE_LM_IN_PROCESS_ADAPTERS:
        result["sandbox_backend"] = "vscode_lm_in_process"
    else:
        result["sandbox_backend"] = sandbox_backend
        if native_windows_cli_without_broker:
            result["launchable"] = False
            result["status"] = "sandbox_unavailable"
            result["reason"] = runtime_adapters.WINDOWS_NATIVE_CLI_REQUIRES_APPCONTAINER
            result["sandbox_backend"] = ""
        elif not sandbox_backend:
            result["launchable"] = False
            result["status"] = "sandbox_unavailable"
            result["reason"] = sandbox_error or "sandbox_unavailable"
    if not result["policy_allowed"]:
        result["launchable"] = False
        result["status"] = "policy_denied"
        result["reason"] = "adapter_denied_by_repo_policy"
    return result


def build_preflight(repo_root: Path | str, adapter_id: str | None = None) -> dict[str, Any]:
    """Return one portable readiness report across repository, tools and providers."""
    root = Path(repo_root).resolve()
    readiness = task_store.storage_readiness(root)
    errors: list[str] = []
    try:
        policy = load_policy(root)
        policy_error = ""
    except RepoPolicyError as exc:
        policy = {**validate_policy(deepcopy(DEFAULT_POLICY)), "configured": False}
        policy_error = str(exc)[:300]
        errors.append("repo_policy_invalid")
    declared_checks, quality_error = _declared_check_ids(root)
    missing_checks = sorted(
        set(policy["validation"]["required_check_ids"]) - set(declared_checks)
    )
    if not readiness.ready:
        errors.append("repository_not_ready")
    if quality_error:
        errors.append("quality_config_invalid")
    if missing_checks:
        errors.append("required_validation_missing")
    source_health = source_graph_daemon.daemon_health(root)
    source_age = source_health.get("index_age_seconds")
    source_stale_after = source_health.get("stale_after_seconds")
    source_generation_fresh = not (
        source_age is not None
        and source_stale_after is not None
        and float(source_age) > float(source_stale_after)
    )
    source_graph_ready_for_code = (
        bool(source_health.get("readable_generation"))
        and bool(source_health.get("last_success_at"))
        and bool(source_health.get("build_revision"))
        and int(source_health.get("files_seen") or 0) > 0
        and source_generation_fresh
    )
    if policy["tools"]["source_graph_required_for_code"] and not source_graph_ready_for_code:
        errors.append("source_graph_not_ready")
    try:
        sandbox_backend = worker_workspace.select_sandbox_backend()
        sandbox_error = ""
    except worker_workspace.WorkspaceError as exc:
        sandbox_backend = ""
        sandbox_error = str(exc)[:200]
    providers = [
        _provider_status(root, name, policy, sandbox_backend, sandbox_error)
        for name in runtime_adapters.LOCAL_ADAPTERS
    ]
    selected = next((item for item in providers if item["adapter_id"] == adapter_id), None)
    if adapter_id and selected is None:
        errors.append("selected_adapter_unsupported")
    elif selected is not None and not selected["launchable"]:
        errors.append("selected_adapter_not_launchable")
    eligible_routes = [item for item in providers if item.get("coverage_required", True)]
    excluded_routes = [item for item in providers if not item.get("coverage_required", True)]
    launchable_routes = [item for item in eligible_routes if item.get("launchable")]
    unavailable_routes = [item for item in eligible_routes if not item.get("launchable")]
    if not launchable_routes:
        errors.append("no_launchable_provider_routes")
    route_coverage_status = (
        "blocked"
        if not launchable_routes
        else ("degraded" if unavailable_routes else "full")
    )
    selected_route_backend = str((selected or {}).get("sandbox_backend") or "")
    route_enforceable = bool(
        selected_route_backend and (selected or {}).get("launchable")
    ) if selected is not None else bool(
        any(item.get("sandbox_backend") for item in launchable_routes)
    )
    try:
        callback_health = task_store.callback_bridge_health(root) if readiness.ready else {}
    except (OSError, RuntimeError, ValueError, task_store.TaskStoreError):
        callback_health = {"ok": False, "reason": "callback_health_unavailable"}
    try:
        # Preflight is called on every dashboard refresh. Use persisted slot
        # accounting only; explicit hygiene preview performs the potentially
        # expensive recursive byte/rogue-tree scan on demand.
        hygiene = workspace_hygiene.inventory(root, refresh_sizes=False)
        hygiene_status = {
            "ok": True,
            "slot_count": int(hygiene.get("slot_count") or 0),
            "total_bytes": int(hygiene.get("total_bytes") or 0),
            "effective_bytes": int(hygiene.get("effective_bytes") or 0),
            "sizes_refreshed": False,
            "explicit_preview_required": True,
        }
    except (OSError, RuntimeError, ValueError, workspace_hygiene.WorkspaceHygieneError) as exc:
        hygiene_status = {
            "ok": False,
            "reason": f"workspace_hygiene_unavailable:{type(exc).__name__}",
        }
    unique_errors = list(dict.fromkeys(errors))
    warnings = (
        ["provider_route_coverage_degraded"]
        if route_coverage_status == "degraded"
        else []
    )
    return {
        "ok": not unique_errors,
        "schema_id": PREFLIGHT_SCHEMA_ID,
        "status": (
            "blocked"
            if unique_errors
            else ("degraded" if route_coverage_status == "degraded" else "ready")
        ),
        "errors": unique_errors,
        "warnings": warnings,
        "repository": {
            "ready": bool(readiness.ready),
            "reason": str(readiness.reason)[:200],
            "repo_id": str(readiness.repo_id),
        },
        "policy": {
            "valid": not policy_error,
            "configured": bool(policy.get("configured")),
            "error": policy_error,
            "providers": dict(policy["providers"]),
            "tools": dict(policy["tools"]),
            "validation": {
                **dict(policy["validation"]),
                "declared_check_ids": declared_checks[:100],
                "missing_check_ids": missing_checks[:100],
                "config_error": quality_error,
            },
            "retention": dict(policy["retention"]),
        },
        "source_graph": {
            **{
                key: source_health.get(key)
                for key in (
                    "ok", "status", "running", "registered", "last_success_at",
                    "stale_reason", "build_revision", "files_seen",
                    "readable_generation",
                )
            },
            "ready_for_code": source_graph_ready_for_code,
        },
        "sandbox": {
            # Primary fields describe the selected route (or the set of
            # launchable routes when no adapter is selected), not only native
            # CLI sandbox availability. This prevents a ready VS Code LM
            # in-process route from being displayed beside a contradictory
            # global ``sandbox unenforceable`` warning.
            "backend": (
                selected_route_backend
                if selected is not None
                else (sandbox_backend or ("route_specific" if route_enforceable else ""))
            ),
            "enforceable": route_enforceable,
            "reason": (
                str((selected or {}).get("reason") or "")[:200]
                if selected is not None and not route_enforceable
                else ("" if route_enforceable else sandbox_error)
            ),
            "selected_adapter": str((selected or {}).get("adapter_id") or ""),
            "selected_backend": selected_route_backend,
            "native_cli_backend": sandbox_backend,
            "native_cli_enforceable": bool(sandbox_backend),
            "native_cli_reason": sandbox_error,
            "route_aware": True,
        },
        "callback": {
            key: callback_health.get(key)
            for key in (
                "ok",
                "backlog_count",
                "retry_count",
                "last_delivered_at",
                "last_dead_letter_at",
                "last_dead_letter_error",
                "reason",
            )
            if key in callback_health
        },
        "workspace_hygiene": hygiene_status,
        "providers": providers,
        "provider_summary": {
            "route_count": len(providers),
            "eligible_route_count": len(eligible_routes),
            "launchable_route_count": len(launchable_routes),
            "unavailable_route_count": len(unavailable_routes),
            "excluded_route_count": len(excluded_routes),
            "coverage_status": route_coverage_status,
            "coverage_ratio": (
                round(len(launchable_routes) / len(eligible_routes), 6)
                if eligible_routes
                else 0.0
            ),
            "unavailable_routes": [
                {
                    "adapter_id": str(item.get("adapter_id") or "")[:128],
                    "status": str(item.get("status") or "unavailable")[:128],
                    "reason": str(item.get("reason") or "unavailable")[:200],
                    "sandbox_backend": str(item.get("sandbox_backend") or "")[:128],
                }
                for item in unavailable_routes
            ],
            "excluded_routes": [
                {
                    "adapter_id": str(item.get("adapter_id") or "")[:128],
                    "status": str(item.get("status") or "excluded")[:128],
                    "reason": str(item.get("reason") or "excluded")[:200],
                    "exclusion": (
                        "platform"
                        if item.get("platform_excluded")
                        else "policy"
                    ),
                }
                for item in excluded_routes
            ],
        },
        "selected_adapter": selected,
    }


# ---------------------------------------------------------------------------
# Worker concurrency capacity.
#
# ``AIWORKHUB_MAX_PROCESSES`` is the authoritative cap on how many workers may
# run at once.  Capping by runner identity instead means a configured cap is
# not the cap that applies, and two cards that share a runner serialise even
# when the machine has spare capacity.  ``resolve_workforce_cap`` reports the
# effective cap from the configured value; ``workforce_admission`` admits a
# launch by total running count and never by runner serialisation.
# ---------------------------------------------------------------------------
WORKFORCE_SCHEMA_ID = "aiworkhub.workforce_capacity.v1"
MAX_PROCESSES_ENV = "AIWORKHUB_MAX_PROCESSES"
DEFAULT_MAX_PROCESSES = 4
MAX_PROCESSES_CEILING = 256


def resolve_workforce_cap(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Resolve the authoritative worker concurrency cap.

    ``AIWORKHUB_MAX_PROCESSES`` decides the cap; the returned ``effective_cap``
    is what actually applies.  A missing or malformed value falls back to
    ``DEFAULT_MAX_PROCESSES`` with a named reason, a below-minimum value is
    clamped up to 1, and an above-ceiling value is clamped down to
    ``MAX_PROCESSES_CEILING`` -- each stated in ``reason`` rather than silently.
    """

    source = os.environ if env is None else env
    raw = source.get(MAX_PROCESSES_ENV)
    if raw is None or str(raw).strip() == "":
        return {
            "schema_id": WORKFORCE_SCHEMA_ID,
            "effective_cap": DEFAULT_MAX_PROCESSES,
            "configured": False,
            "source": "default",
            "raw_value": None,
            "reason": "max_processes_unset_default_applied",
        }
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return {
            "schema_id": WORKFORCE_SCHEMA_ID,
            "effective_cap": DEFAULT_MAX_PROCESSES,
            "configured": False,
            "source": "default_invalid",
            "raw_value": str(raw)[:64],
            "reason": f"max_processes_invalid_default_applied:{str(raw)[:64]}",
        }
    if value < 1:
        return {
            "schema_id": WORKFORCE_SCHEMA_ID,
            "effective_cap": 1,
            "configured": True,
            "source": "env",
            "raw_value": str(raw)[:64],
            "reason": f"max_processes_below_minimum_clamped:{value}->1",
        }
    clamped = min(value, MAX_PROCESSES_CEILING)
    reason = (
        "max_processes_configured"
        if clamped == value
        else f"max_processes_above_ceiling_clamped:{value}->{clamped}"
    )
    return {
        "schema_id": WORKFORCE_SCHEMA_ID,
        "effective_cap": clamped,
        "configured": True,
        "source": "env",
        "raw_value": str(raw)[:64],
        "reason": reason,
    }


def workforce_admission(
    *,
    running_total: int,
    runner: str = "",
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Admit a worker launch by configured capacity, never by runner identity.

    Two cards that share a runner are both admissible while the total running
    worker count is below the authoritative ``AIWORKHUB_MAX_PROCESSES`` cap: a
    launch is never rejected merely because another worker with the same runner
    is already running.  ``effective_cap`` is reported so a caller sees the real
    cap that applied.
    """

    resolved = resolve_workforce_cap(env)
    effective = int(resolved["effective_cap"])
    running = max(0, int(running_total))
    admit = running < effective
    return {
        "schema_id": WORKFORCE_SCHEMA_ID,
        "admit": bool(admit),
        "effective_cap": effective,
        "cap_source": resolved["source"],
        "cap_reason": resolved["reason"],
        "running_total": running,
        "runner": str(runner or ""),
        "serialised_by_runner": False,
        "reason": (
            "workforce_capacity_available"
            if admit
            else f"workforce_cap_reached:running={running}>=cap={effective}"
        ),
    }


# ---------------------------------------------------------------------------
# Provider observability.
#
# No configured adapter exposes a metered provider-side quota to this control
# plane, so a blanket ``ready_unknown`` told the manager nothing.  This report
# states, per configured adapter, what IS observable here (executable install,
# credential presence, editor host reachability/consent) and NAMES the reason
# quota is not observable for that adapter family rather than returning a single
# opaque unknown.  For ``glm_vscode_lm`` it states whether the adapter is
# reachable in THIS installation, with evidence, before any code claim.
# ---------------------------------------------------------------------------
PROVIDER_OBSERVABILITY_SCHEMA_ID = "aiworkhub.provider_observability.v1"

_ROUTE_FAMILY_EDITOR = "editor_hosted_vscode_lm"
_ROUTE_FAMILY_COPILOT = "copilot_byok_cli"
_ROUTE_FAMILY_CLAUDE = "claude_cli"
_ROUTE_FAMILY_CODEX = "codex_cli"
_ROUTE_FAMILY_UNKNOWN = "unknown"

_QUOTA_UNOBSERVABLE_REASON_BY_FAMILY: Mapping[str, str] = {
    _ROUTE_FAMILY_EDITOR: (
        "editor_hosted_route_exposes_host_reachability_and_consent_not_metered_quota"
    ),
    _ROUTE_FAMILY_COPILOT: (
        "copilot_byok_exposes_credential_presence_not_provider_side_quota"
    ),
    _ROUTE_FAMILY_CLAUDE: (
        "claude_cli_exposes_subscription_auth_not_metered_token_quota"
    ),
    _ROUTE_FAMILY_CODEX: "codex_cli_exposes_no_quota_endpoint_to_this_host",
    _ROUTE_FAMILY_UNKNOWN: "adapter_family_unknown_quota_observability_undetermined",
}


def _adapter_route_family(adapter_id: str) -> str:
    if adapter_id in _VSCODE_LM_IN_PROCESS_ADAPTERS:
        return _ROUTE_FAMILY_EDITOR
    if adapter_id in (
        runtime_adapters.DEEPSEEK_COPILOT_ADAPTER,
        runtime_adapters.GLM_COPILOT_ADAPTER,
    ):
        return _ROUTE_FAMILY_COPILOT
    if adapter_id == "claude_cli":
        return _ROUTE_FAMILY_CLAUDE
    if adapter_id == "codex_cli":
        return _ROUTE_FAMILY_CODEX
    return _ROUTE_FAMILY_UNKNOWN


def describe_provider_observability(
    repo_root: Path | str, adapter_id: str
) -> dict[str, Any]:
    """Report what is observable for one adapter here, and quota's named reason.

    Every probe is bounded and guarded: a route is described whether or not it
    is serviceable in this environment, and ``reachability_evidence`` always
    states the ground truth (module present/absent, executable resolved,
    probe outcome) instead of a bare unknown.
    """

    root = Path(repo_root).resolve()
    resolution = runtime_adapters.resolve_executable(adapter_id)
    family = _adapter_route_family(adapter_id)
    installed = bool(resolution.ok)
    install_evidence = (
        f"executable={resolution.executable}"
        if installed
        else f"not_installed:{resolution.reason}"
    )[:200]
    # Installability is observable for every adapter, installed or not; it is
    # the one signal that always names a real observation here.
    observable_signals: list[str] = ["executable_installability"]
    if installed:
        observable_signals.append("executable_installed")
    access_observed = False
    reachable = installed
    reachability_evidence = install_evidence

    if family == _ROUTE_FAMILY_EDITOR:
        if vscode_lm_bridge is None:
            reachable = False
            reachability_evidence = (
                "vscode_lm_bridge_module_unavailable_in_this_installation"
            )
        else:
            observable_signals.append("editor_host_bridge_module_present")
            if adapter_id == runtime_adapters.GLM_VSCODE_LM_ADAPTER:
                model: str | None = runtime_adapters.GLM_DEFAULT_MODEL
            elif adapter_id == runtime_adapters.DEEPSEEK_VSCODE_LM_ADAPTER:
                model = runtime_adapters.DEEPSEEK_DEFAULT_MODEL
            else:
                model = None
            readiness: Mapping[str, Any] | None = None
            try:
                readiness = vscode_lm_bridge.bridge_readiness(
                    root, model=model, adapter_id=adapter_id
                )
            except Exception as exc:  # bounded probe: never fails the report
                readiness = None
                reachable = True  # module present + callable in this install
                reachability_evidence = (
                    "bridge_module_present_probe_unavailable_here:"
                    f"{type(exc).__name__}:{str(exc)[:140]}"
                )[:200]
                observable_signals.append("editor_host_bridge_callable")
            if isinstance(readiness, Mapping):
                reachable = True
                access_observed = bool(readiness.get("access_observed"))
                host_count = int(
                    readiness.get("host_count")
                    or readiness.get("live_host_count")
                    or 0
                )
                observable_signals.append("editor_host_reachability")
                if readiness.get("consent_required") is not None:
                    observable_signals.append("editor_access_consent_state")
                reachability_evidence = (
                    f"bridge_module_present:host_count={host_count}:"
                    f"launchable={bool(readiness.get('launchable'))}:"
                    f"access_observed={access_observed}"
                )[:200]
    elif family == _ROUTE_FAMILY_COPILOT:
        creds = (
            deepseek_credentials
            if adapter_id == runtime_adapters.DEEPSEEK_COPILOT_ADAPTER
            else glm_credentials
        )
        if creds is None:
            observable_signals.append("credential_module_unavailable")
            reachable = False
            reachability_evidence = (
                f"{install_evidence}:credential_module_unavailable"
            )[:200]
        else:
            observable_signals.append("credential_module_present")
            status: Mapping[str, Any] | None = None
            try:
                status = creds.credential_status(repo=root)
            except Exception as exc:  # bounded probe
                status = None
                reachability_evidence = (
                    f"{install_evidence}:credential_probe_error:{type(exc).__name__}"
                )[:200]
            if isinstance(status, Mapping):
                access_observed = bool(
                    status.get("credential_present")
                    or status.get("authenticated")
                    or status.get("launchable")
                )
                observable_signals.append("credential_presence")
                reachability_evidence = (
                    f"{install_evidence}:credential_present={access_observed}"
                )[:200]
    elif family == _ROUTE_FAMILY_CLAUDE and installed:
        status = None
        try:
            status = claude_auth.auth_status(resolution.executable)
        except Exception:  # bounded probe
            status = None
        if isinstance(status, Mapping):
            access_observed = bool(
                status.get("authenticated")
                or status.get("access_observed")
                or status.get("launchable")
            )
            observable_signals.append("subscription_auth_state")

    return {
        "schema_id": PROVIDER_OBSERVABILITY_SCHEMA_ID,
        "adapter_id": adapter_id,
        "route_family": family,
        "installed": installed,
        "install_evidence": install_evidence,
        "reachable": bool(reachable),
        "reachability_evidence": str(reachability_evidence)[:200],
        "access_observed": bool(access_observed),
        "quota_observable": False,
        "quota_observability_reason": _QUOTA_UNOBSERVABLE_REASON_BY_FAMILY[family],
        "observable_signals": observable_signals,
    }


def provider_observability_report(repo_root: Path | str) -> dict[str, Any]:
    """Per-adapter observability across every configured local adapter.

    Each adapter names what is observable here and why its quota is not, so a
    caller reading this report can distinguish an available provider from an
    exhausted one where that is observable, and otherwise knows -- by name --
    exactly why it cannot.
    """

    root = Path(repo_root).resolve()
    adapters = [
        describe_provider_observability(root, name)
        for name in runtime_adapters.LOCAL_ADAPTERS
    ]
    return {
        "schema_id": PROVIDER_OBSERVABILITY_SCHEMA_ID,
        "adapters": adapters,
        "quota_observable_any": any(item["quota_observable"] for item in adapters),
        "note": (
            "no configured adapter exposes a metered provider-side quota to this "
            "control plane; each adapter names what IS observable and the reason "
            "quota is not, rather than returning a single blanket unknown"
        ),
    }


__all__ = [
    "DEFAULT_POLICY",
    "POLICY_RELATIVE_PATH",
    "PREFLIGHT_SCHEMA_ID",
    "QUOTA_STATE_UNAVAILABLE",
    "READINESS_READY",
    "READINESS_READY_UNVERIFIED",
    "RepoPolicyError",
    "SCHEMA_ID",
    "WORKFORCE_SCHEMA_ID",
    "MAX_PROCESSES_ENV",
    "DEFAULT_MAX_PROCESSES",
    "MAX_PROCESSES_CEILING",
    "PROVIDER_OBSERVABILITY_SCHEMA_ID",
    "build_preflight",
    "describe_provider_observability",
    "ensure_policy",
    "load_policy",
    "policy_path",
    "provider_observability_report",
    "resolve_workforce_cap",
    "validate_launch",
    "validate_policy",
    "workforce_admission",
]
