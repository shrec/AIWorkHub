"""Shared AIWorkHub repository route registry.

Durable task state remains repository-local under ``.aiworkhub``.  The shared
registry under ``~/.aiworkhub/router`` contains only bounded discovery leases
and a manager-route ownership fence; it never moves task or context data.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import repository_state
from .platform_io import atomic_replace, chmod_path, lock_fd, process_is_alive, unlock_fd


SCHEMA_ID = "aiworkhub.shared_repo_route.v1"
DEFAULT_TTL_SECONDS = 15 * 60
MAX_RECORD_BYTES = 256 * 1024
_REPO_ID_RE = re.compile(r"^repo_[a-f0-9]{32}$")
_THREAD_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
OWNERSHIP_SCHEMA_ID = "aiworkhub.shared_manager_route_ownership.v1"
_OWNERSHIP_LOCK = threading.RLock()


def registry_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".aiworkhub" / "router" / "repos"


def _ownership_path(home: Path | None = None) -> Path:
    return registry_dir(home).parent / "manager-route-ownership.json"


def _read_ownership(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_id": OWNERSHIP_SCHEMA_ID, "revision": 0, "routes": {}}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"route_ownership_read_failed:{type(exc).__name__}") from exc
    if not isinstance(payload, dict) or payload.get("schema_id") != OWNERSHIP_SCHEMA_ID:
        raise RuntimeError("route_ownership_schema_mismatch")
    if not isinstance(payload.get("routes"), dict):
        raise RuntimeError("route_ownership_routes_invalid")
    return payload


def _write_ownership(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        chmod_path(temporary, 0o600)
        atomic_replace(temporary, path)
        chmod_path(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _route_key(provider: str, thread_id: str) -> str:
    return f"{provider}:{thread_id}"


def _apply_manager_route_ownership(
    records: list[dict[str, Any]], *, home: Path | None = None
) -> tuple[list[dict[str, Any]], str]:
    """Project atomic ownership rows onto renewable extension records."""

    try:
        ownership = _read_ownership(_ownership_path(home))
    except RuntimeError as exc:
        return records, str(exc)
    projected = [dict(record) for record in records]
    for raw in ownership.get("routes", {}).values():
        if not isinstance(raw, dict):
            continue
        provider = str(raw.get("provider") or "").strip().lower()
        thread_id = str(raw.get("thread_id") or "").strip().lower()
        target_repo_id = str(raw.get("repo_id") or "").strip()
        window_id = str(raw.get("window_id") or "").strip()
        epoch = raw.get("epoch")
        if (
            provider != "codex"
            or not _THREAD_ID_RE.fullmatch(thread_id)
            or not _REPO_ID_RE.fullmatch(target_repo_id)
            or not window_id
            or type(epoch) is not int
            or epoch < 1
        ):
            continue
        for record in projected:
            targets = record.get("targets")
            provider_target = targets.get(provider) if isinstance(targets, dict) else None
            route = provider_target.get("route") if isinstance(provider_target, dict) else None
            if not isinstance(route, dict):
                route = {}
            route_thread = str(route.get("thread_id") or "").strip().lower()
            is_target = str(record.get("repo_id") or "") == target_repo_id
            if not is_target and route_thread != thread_id:
                continue
            next_route = dict(route)
            if is_target:
                next_route.update(
                    {
                        "thread_id": thread_id,
                        "session_id": thread_id,
                        "repo_id": target_repo_id,
                        "owner_window_id": window_id,
                        "ownership_epoch": epoch,
                    }
                )
            else:
                next_route.update(
                    {
                        "thread_id": "",
                        "session_id": "",
                        "ownership_epoch": epoch,
                        "fenced_by_repo_id": target_repo_id,
                    }
                )
            next_target = {
                **(provider_target if isinstance(provider_target, dict) else {}),
                "route": next_route,
            }
            record["targets"] = {
                **(targets if isinstance(targets, dict) else {}),
                provider: next_target,
            }
            record["manager_route_ownership"] = dict(raw)
    return projected, ""


def _acquire_ownership_file_lock(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        lock_fd(fd, blocking=False)
    except OSError as exc:
        os.close(fd)
        raise RuntimeError("route_ownership_conflict") from exc
    return fd


def transfer_manager_route(
    *,
    provider: str,
    thread_id: str,
    window_id: str,
    source_repo_id: str,
    target_repo_id: str,
    repositories: list[dict[str, Any]],
    home: Path | None = None,
) -> dict[str, Any]:
    """CAS one verified manager route from a live source to a live target.

    The shared ownership ledger is intentionally separate from repository-local
    task stores and extension lease records.  One atomic row is therefore the
    fence: readers never need a two-file source/target update to agree on who
    owns the chat.  The extension may continue renewing either repository
    record without overwriting this manager-owned transfer epoch.
    """

    normalized_provider = str(provider or "").strip().lower()
    normalized_thread = str(thread_id or "").strip().lower()
    normalized_window = str(window_id or "").strip()
    source = str(source_repo_id or "").strip()
    target = str(target_repo_id or "").strip()
    if normalized_provider != "codex":
        return {"ok": False, "error": "route_transfer_provider_invalid"}
    if not _THREAD_ID_RE.fullmatch(normalized_thread) or not normalized_window:
        return {"ok": False, "error": "route_transfer_identity_invalid"}
    if not _REPO_ID_RE.fullmatch(source) or not _REPO_ID_RE.fullmatch(target) or source == target:
        return {"ok": False, "error": "route_transfer_repository_invalid"}

    live = {
        str(record.get("repo_id") or ""): record
        for record in repositories
        if isinstance(record, dict)
        and bool(record.get("extension_host_alive"))
        and not bool(record.get("stale"))
    }
    if target not in live:
        return {"ok": False, "error": "route_transfer_target_not_live"}
    target_record = live[target]
    target_targets = target_record.get("targets")
    target_provider = target_targets.get(normalized_provider) if isinstance(target_targets, dict) else None
    target_route = target_provider.get("route") if isinstance(target_provider, dict) else None
    target_thread = (
        str(target_route.get("thread_id") or "").strip().lower()
        if isinstance(target_route, dict)
        else ""
    )
    if _THREAD_ID_RE.fullmatch(target_thread) and target_thread != normalized_thread:
        return {"ok": False, "error": "route_transfer_target_owned_by_foreign_manager"}

    ownership_path = _ownership_path(home)
    lock_path = ownership_path.with_suffix(".lock")
    with _OWNERSHIP_LOCK:
        try:
            fd = _acquire_ownership_file_lock(lock_path)
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            payload = _read_ownership(ownership_path)
            routes = dict(payload["routes"])
            key = _route_key(normalized_provider, normalized_thread)
            current = routes.get(key)
            if isinstance(current, dict):
                if (
                    str(current.get("repo_id") or "") != source
                    or str(current.get("window_id") or "") != normalized_window
                ):
                    return {
                        "ok": False,
                        "error": "route_ownership_epoch_conflict",
                        "current_repo_id": str(current.get("repo_id") or ""),
                        "current_epoch": int(current.get("epoch") or 0),
                    }
                epoch = int(current.get("epoch") or 0) + 1
            else:
                source_record = live.get(source)
                source_targets = source_record.get("targets") if isinstance(source_record, dict) else None
                source_target = source_targets.get(normalized_provider) if isinstance(source_targets, dict) else None
                source_route = source_target.get("route") if isinstance(source_target, dict) else None
                if (
                    not isinstance(source_record, dict)
                    or str(source_record.get("window_id") or "") != normalized_window
                    or not isinstance(source_route, dict)
                    or str(source_route.get("thread_id") or "").strip().lower() != normalized_thread
                ):
                    return {"ok": False, "error": "route_transfer_source_not_owned"}
                epoch = 1
            route = {
                "provider": normalized_provider,
                "thread_id": normalized_thread,
                "window_id": normalized_window,
                "repo_id": target,
                "previous_repo_id": source,
                "epoch": epoch,
                "updated_at_epoch": time.time(),
            }
            routes[key] = route
            next_payload = {
                "schema_id": OWNERSHIP_SCHEMA_ID,
                "revision": int(payload.get("revision") or 0) + 1,
                "routes": routes,
            }
            _write_ownership(ownership_path, next_payload)
            return {
                "ok": True,
                "schema_id": "aiworkhub.manager_route_transfer.v1",
                **route,
                "ledger_revision": next_payload["revision"],
            }
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)[:200]}
        finally:
            unlock_fd(fd)
            os.close(fd)


def rollback_manager_route(
    *,
    provider: str,
    thread_id: str,
    window_id: str,
    failed_repo_id: str,
    restore_repo_id: str,
    expected_epoch: int,
    home: Path | None = None,
) -> dict[str, Any]:
    """Restore a failed transfer only when its exact epoch still owns it."""

    ownership_path = _ownership_path(home)
    lock_path = ownership_path.with_suffix(".lock")
    key = _route_key(str(provider).strip().lower(), str(thread_id).strip().lower())
    with _OWNERSHIP_LOCK:
        try:
            fd = _acquire_ownership_file_lock(lock_path)
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            payload = _read_ownership(ownership_path)
            routes = dict(payload["routes"])
            current = routes.get(key)
            if not isinstance(current, dict) or (
                str(current.get("repo_id") or "") != str(failed_repo_id)
                or str(current.get("window_id") or "") != str(window_id)
                or type(current.get("epoch")) is not int
                or current["epoch"] != expected_epoch
            ):
                return {"ok": False, "error": "route_rollback_epoch_conflict"}
            route = {
                **current,
                "repo_id": str(restore_repo_id),
                "previous_repo_id": str(failed_repo_id),
                "epoch": expected_epoch + 1,
                "updated_at_epoch": time.time(),
            }
            routes[key] = route
            next_payload = {
                "schema_id": OWNERSHIP_SCHEMA_ID,
                "revision": int(payload.get("revision") or 0) + 1,
                "routes": routes,
            }
            _write_ownership(ownership_path, next_payload)
            return {"ok": True, "schema_id": "aiworkhub.manager_route_rollback.v1", **route}
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)[:200]}
        finally:
            unlock_fd(fd)
            os.close(fd)


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
    try:
        pid = int(payload.get("extension_host_pid") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "extension_host_pid_invalid", "path": path.name, "repo_id": repo_id}
    return {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "repo_id": repo_id,
        "repo_name": str(payload.get("repo_name") or root.name),
        "repo_root": str(root),
        "window_id": str(payload.get("window_id") or ""),
        "extension_host_pid": pid,
        "extension_host_alive": process_is_alive(pid),
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
    records, ownership_error = _apply_manager_route_ownership(records)
    return {
        "ok": True,
        "schema_id": SCHEMA_ID,
        "registry_dir": str(root_dir),
        "repositories": records,
        "inactive": inactive[:16],
        "rejects": rejects[:16],
        "ownership_error": ownership_error,
    }


def resolve_repository_route(
    *,
    provider: str,
    thread_id: str,
    extension_host_pid: int = 0,
) -> dict[str, Any]:
    """Resolve one live repository for an already-observed chat route.

    This is deliberately an identity lookup, not a path selector.  The caller
    supplies a provider/thread identity learned from its own transport; roots
    are accepted only from valid, live shared-registry records whose manifest
    still contains the advertised ``repo_id``.  Zero hits and ambiguity both
    fail closed.
    """

    normalized_provider = str(provider or "").strip().lower()
    normalized_thread = str(thread_id or "").strip()
    if normalized_provider not in {"codex", "claude", "copilot"}:
        return {"ok": False, "error": "provider_invalid", "matches": 0}
    if not normalized_thread:
        return {"ok": False, "error": "thread_id_missing", "matches": 0}
    registry = list_known_repositories(limit=256)
    if not registry.get("ok"):
        return {"ok": False, "error": str(registry.get("error") or "registry_unavailable"), "matches": 0}
    matches: list[dict[str, Any]] = []
    for record in registry.get("repositories", []):
        if not isinstance(record, dict):
            continue
        if not bool(record.get("extension_host_alive")) or bool(record.get("stale")):
            continue
        if extension_host_pid > 1 and int(record.get("extension_host_pid") or 0) != extension_host_pid:
            continue
        targets = record.get("targets")
        target = targets.get(normalized_provider) if isinstance(targets, dict) else None
        route = target.get("route") if isinstance(target, dict) else None
        if not isinstance(route, dict):
            continue
        if str(route.get("thread_id") or "").strip() != normalized_thread:
            continue
        if str(route.get("repo_id") or record.get("repo_id") or "") != str(record.get("repo_id") or ""):
            continue
        matches.append(record)
    if len(matches) != 1:
        return {
            "ok": False,
            "error": "route_not_observed" if not matches else "route_ambiguous",
            "matches": len(matches),
        }
    match = matches[0]
    return {
        "ok": True,
        "schema_id": "aiworkhub.shared_repo_resolution.v1",
        "repo_id": str(match["repo_id"]),
        "repo_name": str(match["repo_name"]),
        "repo_root": str(match["repo_root"]),
        "window_id": str(match.get("window_id") or ""),
        "extension_host_pid": int(match.get("extension_host_pid") or 0),
        "provider": normalized_provider,
        "thread_id": normalized_thread,
    }
