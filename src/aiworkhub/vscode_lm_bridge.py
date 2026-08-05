"""Secure spool contract for VS Code-hosted language-model workers.

The VS Code extension is the only process that can use ``vscode.lm`` and the
user's already-authorized editor model providers.  The Python Task MCP runtime
therefore exchanges bounded request/result documents with that extension via
an owner-only host spool.  Durable task state remains repository-local; this
spool is runtime-only and contains no provider credential.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import repository_state
from .source_graph import SOURCE_GRAPH_MODES
try:
    from .platform_io import chmod_fd
except ImportError:  # pragma: no cover - standalone compatibility
    def chmod_fd(fd: int, mode: int) -> None:
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            fchmod(fd, mode)


REQUEST_SCHEMA_ID = "aiworkhub.vscode_lm.request.v1"
HOST_SCHEMA_ID = "aiworkhub.vscode_lm.host.v1"
RESPONSE_SCHEMA_ID = "aiworkhub.vscode_lm.response.v1"
EDIT_RESPONSE_SCHEMA_ID_V1 = "aiworkhub.vscode_lm.edit_response.v1"
EDIT_RESPONSE_SCHEMA_ID_V2 = "aiworkhub.vscode_lm.edit_response.v2"
EDIT_RESPONSE_SCHEMA_ID = "aiworkhub.vscode_lm.semantic_edit_response.v3"
BRIDGE_ROOT_ENV = "AIWORKHUB_VSCODE_LM_BRIDGE_ROOT"
DEFAULT_ROOT_REL = Path(".aiworkhub") / "vscode_lm_bridge"
HOST_TTL_SECONDS = 45
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_PROMPT_BYTES = 6 * 1024 * 1024
_REQUEST_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_CONTEXT_RECEIPT_PREFIX = "PROJECT_CONTEXT_RECEIPT:"
SOURCE_GRAPH_WORKFLOW_STAGES: tuple[str, ...] = (
    "orientation", "implementation", "validation", "review", "rework", "unspecified",
)
EDITOR_MODEL_ALIASES: dict[str, tuple[str, ...]] = {
    "deepseek-v4-pro": (
        "deepseek-v4-pro",
        "deepseek-v4",
        "deepseek-chat",
        "deepseek/deepseek-v4-pro",
        "deepseek.deepseek-v4-pro",
    ),
    "deepseek-v4-flash": (
        "deepseek-v4-flash",
        "deepseek-reasoner",
        "deepseek/deepseek-v4-flash",
        "deepseek.deepseek-v4-flash",
    ),
    "glm-5.2": (
        "glm-5.2",
        "glm-5_2",
        "z-ai/glm-5.2",
        "zhipu/glm-5.2",
    ),
    "claude-sonnet-current": (
        "claude-sonnet-current",
        "claude-3.5-sonnet",
        "claude-3-5-sonnet",
        "claude-3.7-sonnet",
        "claude-3-7-sonnet",
        "claude-sonnet-4",
        "claude-4-sonnet",
        "anthropic/claude-sonnet-4",
    ),
}


class BridgeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BridgeRequest:
    request_id: str
    repo_id: str
    request_path: Path
    response_path: Path
    worker_spec_path: Path


def bridge_root() -> Path:
    override = os.environ.get(BRIDGE_ROOT_ENV, "").strip()
    root = Path(override).expanduser() if override else Path.home() / DEFAULT_ROOT_REL
    return root.resolve(strict=False)


def _repo_id(repo: Path) -> str:
    state = repository_state.inspect_repository(repo)
    return state.manifest.repo_id


def resolve_editor_model_alias(model: str | None, observed_models: Iterable[str]) -> str | None:
    """Resolve one requested model only to an editor-observed same-provider alias."""
    if model is None:
        return None
    requested = str(model).strip()
    observed = [str(value).strip() for value in observed_models if str(value).strip()]
    if requested in observed:
        return requested
    aliases = EDITOR_MODEL_ALIASES.get(requested, (requested,))
    for alias in aliases:
        if alias in observed:
            return alias
    return None


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise BridgeError("bridge_document_too_large")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        chmod_fd(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows refuses to replace a file while this process still holds
        # the mkstemp handle open (WinError 32).  POSIX permits it, which hid
        # the bug in the original implementation.
        os.close(fd)
        fd = -1
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def bridge_readiness(
    repo: Path,
    *,
    model: str | None = "glm-5.2",
    adapter_id: str = "glm_vscode_lm",
) -> dict[str, Any]:
    """Return a secret-free, fail-closed editor-host bridge status.

    ``model=None`` measures the shared VS Code broker itself.  A model-specific
    query distinguishes three materially different states: no heartbeat ever
    observed, only expired heartbeats, and a live host whose current model
    catalog does not contain the requested identity.  Older code collapsed the
    latter two into ``vscode_lm_model_not_visible``; after a reload that made a
    healthy Windows/Remote-SSH authorization look like a model entitlement
    failure merely because a stale JSON file still existed.
    """
    repo = repo.resolve()
    repo_id = _repo_id(repo)
    hosts_dir = bridge_root() / "hosts" / repo_id
    now = time.time()
    candidates: list[dict[str, Any]] = []
    try:
        paths = list(hosts_dir.glob("*.json"))
    except OSError:
        paths = []
    for path in paths[:32]:
        try:
            stat_result = path.stat()
            getuid = getattr(os, "getuid", None)
            if (
                (getuid is not None and stat_result.st_uid != getuid())
                or stat_result.st_size > 256 * 1024
            ):
                continue
            if os.name != "nt" and stat.S_IMODE(stat_result.st_mode) != 0o600:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("schema_id") != HOST_SCHEMA_ID:
            continue
        if payload.get("repo_id") != repo_id:
            continue
        age = max(0.0, now - stat_result.st_mtime)
        models = payload.get("models") if isinstance(payload.get("models"), list) else []
        candidates.append({**payload, "age_seconds": round(age, 3), "models": models})
    live_hosts = [
        item for item in candidates if item["age_seconds"] <= HOST_TTL_SECONDS
    ]
    matching_hosts = []
    resolved_by_host: dict[int, str] = {}
    for item in live_hosts:
        models = item.get("models", [])
        resolved_model = resolve_editor_model_alias(model, models)
        if model is None or resolved_model is not None:
            matching_hosts.append(item)
            if resolved_model is not None:
                resolved_by_host[id(item)] = resolved_model
    selected = (
        sorted(matching_hosts, key=lambda item: item["age_seconds"])[0]
        if matching_hosts
        else None
    )
    selected_model = resolved_by_host.get(id(selected), "") if selected is not None else ""
    selected_access_state = "unknown"
    if selected is not None:
        metadata = selected.get("model_metadata")
        if isinstance(metadata, list):
            for entry in metadata[:128]:
                if not isinstance(entry, dict):
                    continue
                identities = {
                    str(entry.get(key) or "").strip()
                    for key in ("canonical", "id", "family", "name")
                }
                if model is None or selected_model in identities:
                    selected_access_state = str(entry.get("access_state") or "unknown")[:64]
                    if selected_access_state.startswith("granted") or model is not None:
                        break
        if model is None and selected_access_state == "unknown" and selected.get("permission_granted") is True:
            selected_access_state = "granted_observed"
    access_observed = selected_access_state.startswith("granted")
    observed_models = sorted(
        {
            str(name)
            for item in live_hosts
            for name in item.get("models", [])
            if isinstance(name, str) and name
        }
    )[:128]
    if selected is not None:
        blocker_reason = ""
    elif not candidates:
        blocker_reason = "vscode_lm_host_unavailable"
    elif not live_hosts:
        blocker_reason = "vscode_lm_host_stale"
    elif model is not None:
        blocker_reason = "vscode_lm_model_not_visible"
    else:
        blocker_reason = "vscode_lm_model_catalog_empty"
    return {
        "adapter_id": adapter_id,
        "kind": "vscode_language_model_api",
        "repo_id": repo_id,
        "model": model,
        "resolved_model": selected_model,
        "launchable": selected is not None,
        "blocker_reason": blocker_reason,
        "access_state": selected_access_state,
        "access_observed": access_observed,
        "consent_required": selected is not None and not access_observed,
        "window_id": str((selected or {}).get("window_id") or ""),
        "host_count": len(matching_hosts),
        "live_host_count": len(live_hosts),
        "stale_host_count": max(0, len(candidates) - len(live_hosts)),
        "observed_models": observed_models,
        "freshest_age_seconds": min(
            (float(item["age_seconds"]) for item in candidates),
            default=None,
        ),
        "credential_required": False,
    }


def create_request(
    *,
    repo: Path,
    request_id: str,
    workspace_path: Path,
    workspace_home: Path,
    prompt: str,
    model: str,
    allowed_writes: Iterable[str],
    timeout_seconds: int,
    workspace_parent_baseline: dict[str, str | None] | None = None,
    source_graph_request: dict[str, Any] | None = None,
    source_graph_result: dict[str, Any] | None = None,
) -> BridgeRequest:
    """Publish one repo-scoped request and private worker-side contract."""
    if not _REQUEST_ID_RE.fullmatch(request_id):
        raise BridgeError("bridge_request_id_invalid")
    if not isinstance(prompt, str) or not prompt.strip():
        raise BridgeError("bridge_prompt_missing")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise BridgeError("bridge_prompt_too_large")
    repo = repo.resolve()
    workspace_path = workspace_path.resolve()
    workspace_home = workspace_home.resolve()
    if workspace_path.name != "worktree" or workspace_home.name != "home":
        raise BridgeError("bridge_workspace_shape_invalid")
    if workspace_path.parent != workspace_home.parent or workspace_path.parent.name != request_id:
        raise BridgeError("bridge_workspace_request_mismatch")
    repo_id = _repo_id(repo)
    response_path = workspace_home / ".aiworkhub_vscode_lm_response.json"
    worker_spec_path = workspace_home / ".aiworkhub_vscode_lm_worker.json"
    request_path = bridge_root() / "requests" / repo_id / f"{request_id}.json"
    allowed = [str(value) for value in allowed_writes]
    parent_baseline = dict(workspace_parent_baseline or {})
    path_contracts: dict[str, dict[str, Any]] = {}
    create_paths: list[str] = []
    for relative in allowed:
        # Only exact declared paths receive a contract. Glob patterns remain
        # valid scope declarations but are never expanded by the bridge.
        if any(marker in relative for marker in ("*", "?", "[")):
            continue
        candidate = (workspace_path / relative).resolve(strict=False)
        try:
            candidate.relative_to(workspace_path)
        except ValueError:
            continue
        parent_missing = relative in parent_baseline and parent_baseline[relative] is None
        if parent_missing:
            create_paths.append(relative)
        current_sha256 = ""
        if candidate.is_file() and not candidate.is_symlink():
            current_sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
        path_contracts[relative] = {
            "action": "create" if parent_missing else "edit",
            "current_sha256": current_sha256,
            "parent_existed": not parent_missing,
        }
    deadline = datetime.now(timezone.utc) + timedelta(seconds=int(timeout_seconds))
    initial_source_graph_request: dict[str, Any] | None = None
    if source_graph_request:
        mode = str(source_graph_request.get("mode") or "focus")
        query = str(source_graph_request.get("query") or "").strip()
        if (
            mode not in set(SOURCE_GRAPH_MODES)
            or not query
            or len(query) > 512
        ):
            raise BridgeError("bridge_source_graph_request_invalid")
        workflow_stage = str(
            source_graph_request.get("workflow_stage") or "orientation"
        )
        if workflow_stage not in SOURCE_GRAPH_WORKFLOW_STAGES:
            raise BridgeError("bridge_source_graph_workflow_stage_invalid")
        initial_source_graph_request = {
            "mode": mode,
            "query": query,
            "workflow_stage": workflow_stage,
        }
        for key in ("budget", "target", "bundle_type"):
            value = source_graph_request.get(key)
            if value is not None:
                initial_source_graph_request[key] = value
    initial_source_graph_result: dict[str, Any] | None = None
    if source_graph_result is not None:
        if initial_source_graph_request is None:
            raise BridgeError("bridge_source_graph_result_without_request")
        if not isinstance(source_graph_result, dict) or source_graph_result.get("ok") is not True:
            raise BridgeError("bridge_source_graph_result_invalid")
        encoded_result = json.dumps(
            source_graph_result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded_result) > MAX_REQUEST_BYTES // 2:
            raise BridgeError("bridge_source_graph_result_too_large")
        initial_source_graph_result = dict(source_graph_result)
    shared = {
        "schema_id": REQUEST_SCHEMA_ID,
        "request_id": request_id,
        "repo_id": repo_id,
        "repo_root": str(repo),
        "workspace_path": str(workspace_path),
        "workspace_home": str(workspace_home),
        "response_path": str(response_path),
        "model": model,
        "prompt": prompt,
        "allowed_writes": allowed,
        "path_contracts": path_contracts,
        "initial_source_graph_request": initial_source_graph_request,
        "initial_source_graph_result": initial_source_graph_result,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "deadline": deadline.isoformat(),
        "response_contract": {
            "schema_id": EDIT_RESPONSE_SCHEMA_ID,
            "format": "json_only",
            "edits": [
                {
                    "path": "repo-relative allowed_writes path",
                    "current_sha256": "lowercase sha256 of current workspace bytes",
                    "ranges": [
                        {
                            "start_line": 1,
                            "end_line": 1,
                            "new": "replacement text only; never the complete file",
                            "preserve_trailing_newline": True,
                        }
                    ],
                }
            ],
            "creates": [{"path": "repo-relative allowed_writes path", "content": "complete UTF-8 file"}],
            "legacy_v2_edits": [{
                "path": "repo-relative allowed_writes path",
                "current_sha256": "lowercase sha256",
                "replacements": [{"old": "exact current text", "new": "replacement text", "expected_count": 1}],
            }],
            "legacy_v1_files": [{"path": "repo-relative allowed_writes path", "content": "complete UTF-8 file"}],
        },
    }
    context_receipt = ""
    marker = prompt.rfind(_CONTEXT_RECEIPT_PREFIX)
    if marker >= 0:
        candidate = prompt[marker + len(_CONTEXT_RECEIPT_PREFIX):].lstrip()
        try:
            value, _end = json.JSONDecoder().raw_decode(candidate)
            if isinstance(value, dict):
                context_receipt = _CONTEXT_RECEIPT_PREFIX + " " + json.dumps(
                    value, ensure_ascii=False, sort_keys=True
                )
        except json.JSONDecodeError:
            context_receipt = ""
    worker = {
        "schema_id": "aiworkhub.vscode_lm.worker_spec.v1",
        "request_id": request_id,
        "repo_id": repo_id,
        "workspace_path": str(workspace_path),
        "response_path": str(response_path),
        "allowed_writes": allowed,
        "create_paths": sorted(create_paths),
        "timeout_seconds": int(timeout_seconds),
        "project_context_receipt": context_receipt,
    }
    _atomic_json(worker_spec_path, worker)
    _atomic_json(request_path, shared)
    return BridgeRequest(request_id, repo_id, request_path, response_path, worker_spec_path)


def cancel_request(request: BridgeRequest) -> None:
    try:
        request.request_path.unlink()
    except FileNotFoundError:
        pass
