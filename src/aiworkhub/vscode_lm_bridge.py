"""Secure spool contract for VS Code-hosted language-model workers.

The VS Code extension is the only process that can use ``vscode.lm`` and the
user's already-authorized editor model providers.  The Python Task MCP runtime
therefore exchanges bounded request/result documents with that extension via
an owner-only host spool.  Durable task state remains repository-local; this
spool is runtime-only and contains no provider credential.
"""

from __future__ import annotations

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
EDIT_RESPONSE_SCHEMA_ID = "aiworkhub.vscode_lm.edit_response.v1"
BRIDGE_ROOT_ENV = "AIWORKHUB_VSCODE_LM_BRIDGE_ROOT"
DEFAULT_ROOT_REL = Path(".aiworkhub") / "vscode_lm_bridge"
HOST_TTL_SECONDS = 45
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_PROMPT_BYTES = 6 * 1024 * 1024
_REQUEST_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_CONTEXT_RECEIPT_PREFIX = "PROJECT_CONTEXT_RECEIPT:"


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
    matching_hosts = [
        item
        for item in live_hosts
        if model is None or model in item.get("models", [])
    ]
    selected = (
        sorted(matching_hosts, key=lambda item: item["age_seconds"])[0]
        if matching_hosts
        else None
    )
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
        "launchable": selected is not None,
        "blocker_reason": blocker_reason,
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
    source_graph_request: dict[str, Any] | None = None,
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
        initial_source_graph_request = {"mode": mode, "query": query}
        for key in ("budget", "target", "bundle_type"):
            value = source_graph_request.get(key)
            if value is not None:
                initial_source_graph_request[key] = value
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
        "initial_source_graph_request": initial_source_graph_request,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "deadline": deadline.isoformat(),
        "response_contract": {
            "schema_id": EDIT_RESPONSE_SCHEMA_ID,
            "format": "json_only",
            "files": [{"path": "repo-relative allowed_writes path", "content": "complete UTF-8 file"}],
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
