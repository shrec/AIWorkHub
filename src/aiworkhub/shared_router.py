"""Shared AIWorkHub repository route registry.

This module is intentionally read-only from Python MCP tools.  Durable task
state remains repository-local under ``.aiworkhub``; the shared registry under
``~/.aiworkhub/router/repos`` is only a bounded discovery/index surface that
lets a manager see which VS Code windows/repositories are alive.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from . import repository_state


SCHEMA_ID = "aiworkhub.shared_repo_route.v1"
DEFAULT_TTL_SECONDS = 15 * 60
MAX_RECORD_BYTES = 256 * 1024
_REPO_ID_RE = re.compile(r"^repo_[a-f0-9]{32}$")


def registry_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".aiworkhub" / "router" / "repos"


def _read_manifest_repo_id(root: Path) -> str:
    try:
        payload = json.loads(
            (root / repository_state.PROJECT_MANIFEST_REL).read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("repo_id") or "")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _bounded_record(path: Path, *, now: float) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError as exc:
        return {"ok": False, "error": f"stat_failed:{type(exc).__name__}", "path": path.name}
    if not path.is_file():
        return {"ok": False, "error": "not_file", "path": path.name}
    if stat.st_size > MAX_RECORD_BYTES:
        return {"ok": False, "error": "record_too_large", "path": path.name}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"read_failed:{type(exc).__name__}", "path": path.name}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "not_object", "path": path.name}
    if payload.get("schema_id") != SCHEMA_ID:
        return {"ok": False, "error": "schema_mismatch", "path": path.name}
    repo_id = str(payload.get("repo_id") or "")
    if not _REPO_ID_RE.fullmatch(repo_id):
        return {"ok": False, "error": "repo_id_invalid", "path": path.name}
    if path.name != f"{repo_id}.json":
        return {"ok": False, "error": "repo_id_filename_mismatch", "path": path.name, "repo_id": repo_id}
    root_raw = str(payload.get("repo_root") or "")
    if not root_raw:
        return {"ok": False, "error": "repo_root_missing", "path": path.name, "repo_id": repo_id}
    root = Path(root_raw).expanduser()
    try:
        root = root.resolve()
    except OSError:
        root = root.absolute()
    if not root.is_dir():
        return {"ok": False, "error": "repo_root_missing_on_disk", "path": path.name, "repo_id": repo_id}
    manifest_repo_id = _read_manifest_repo_id(root)
    if manifest_repo_id != repo_id:
        return {
            "ok": False,
            "error": "manifest_repo_id_mismatch",
            "path": path.name,
            "repo_id": repo_id,
            "manifest_repo_id": manifest_repo_id,
        }
    updated_at = str(payload.get("updated_at") or "")
    expires_at = str(payload.get("lease_expires_at") or "")
    mtime_age_seconds = max(0.0, now - stat.st_mtime)
    stale = mtime_age_seconds > DEFAULT_TTL_SECONDS
    pid = int(payload.get("extension_host_pid") or 0)
    return {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "repo_id": repo_id,
        "repo_name": str(payload.get("repo_name") or root.name),
        "repo_root": str(root),
        "window_id": str(payload.get("window_id") or ""),
        "extension_host_pid": pid,
        "extension_host_alive": _pid_alive(pid),
        "selected_provider": str(payload.get("selected_provider") or ""),
        "targets": payload.get("targets") if isinstance(payload.get("targets"), dict) else {},
        "updated_at": updated_at,
        "lease_expires_at": expires_at,
        "age_seconds": round(mtime_age_seconds, 3),
        "stale": stale,
        "current_repo": False,
    }


def list_known_repositories(
    *,
    current_root: Path | None = None,
    limit: int = 64,
    include_inactive: bool = False,
) -> dict[str, Any]:
    """Return a bounded list of valid shared repo route records.

    Invalid records are summarized as rejects instead of raising.  This is
    dashboard/manager discovery only; callers must still use the repo-local
    MCP child for all task mutations.
    """

    now = time.time()
    root_dir = registry_dir()
    records: list[dict[str, Any]] = []
    inactive: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    try:
        paths = sorted(root_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as exc:
        return {
            "ok": False,
            "error": f"registry_unavailable:{type(exc).__name__}",
            "schema_id": SCHEMA_ID,
            "repositories": [],
            "rejects": [],
        }
    current = ""
    if current_root is not None:
        try:
            current = str(current_root.resolve())
        except OSError:
            current = str(current_root.absolute())
    for path in paths[: max(1, min(int(limit or 64), 256))]:
        record = _bounded_record(path, now=now)
        if record.get("ok"):
            record["current_repo"] = bool(current and record.get("repo_root") == current)
            is_live = bool(record.get("extension_host_alive")) and not bool(record.get("stale"))
            if is_live or record["current_repo"] or include_inactive:
                records.append(record)
            else:
                inactive.append(record)
        else:
            rejects.append(record)
    return {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "registry_dir": str(root_dir),
        "repositories": records,
        "inactive": inactive[:16],
        "rejects": rejects[:16],
    }
