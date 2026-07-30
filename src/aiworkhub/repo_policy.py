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

from . import quality_evidence, runtime_adapters, source_graph_daemon, task_store

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
        "source_graph_generations": 3,
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
            "source_graph_generations": _bounded_int(
                retention.get("source_graph_generations"),
                "source_graph_generations",
                1,
                20,
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


def _provider_status(repo_root: Path, adapter_id: str, policy: Mapping[str, Any]) -> dict[str, Any]:
    resolution = runtime_adapters.resolve_executable(adapter_id)
    result: dict[str, Any] = {
        "adapter_id": adapter_id,
        "policy_allowed": adapter_id in policy["providers"]["allowed_adapters"],
        "installed": bool(resolution.ok),
        "launchable": bool(resolution.ok),
        "access_observed": False,
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
    except (OSError, RuntimeError, ValueError):
        readiness = None
    if isinstance(readiness, Mapping):
        result["access_observed"] = True
        result["launchable"] = bool(resolution.ok and readiness.get("launchable"))
        result["status"] = "ready" if result["launchable"] else "access_unavailable"
        result["reason"] = str(readiness.get("blocker_reason") or readiness.get("reason") or "")[:200]
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
    if policy["tools"]["source_graph_required_for_code"] and (
        not source_health.get("running") or not source_health.get("ok")
    ):
        errors.append("source_graph_not_ready")
    providers = [
        _provider_status(root, name, policy)
        for name in runtime_adapters.LOCAL_ADAPTERS
    ]
    selected = next((item for item in providers if item["adapter_id"] == adapter_id), None)
    if adapter_id and selected is None:
        errors.append("selected_adapter_unsupported")
    elif selected is not None and not selected["launchable"]:
        errors.append("selected_adapter_not_launchable")
    try:
        callback_health = task_store.callback_bridge_health(root) if readiness.ready else {}
    except (OSError, RuntimeError, ValueError, task_store.TaskStoreError):
        callback_health = {"ok": False, "reason": "callback_health_unavailable"}
    return {
        "ok": not errors,
        "schema_id": PREFLIGHT_SCHEMA_ID,
        "status": "ready" if not errors else "blocked",
        "errors": list(dict.fromkeys(errors)),
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
            key: source_health.get(key)
            for key in ("ok", "status", "running", "registered", "last_success_at", "stale_reason")
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
        "providers": providers,
        "selected_adapter": selected,
    }


__all__ = [
    "DEFAULT_POLICY",
    "POLICY_RELATIVE_PATH",
    "PREFLIGHT_SCHEMA_ID",
    "RepoPolicyError",
    "SCHEMA_ID",
    "build_preflight",
    "ensure_policy",
    "load_policy",
    "policy_path",
    "validate_launch",
    "validate_policy",
]
