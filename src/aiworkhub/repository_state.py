"""Repository-local AIWorkHub durable state foundation.

This module is intentionally import-safe: it performs no filesystem reads or
writes at import time and does not depend on the legacy AIWorkHub checkout path.
Callers must explicitly ask to resolve, inspect, or bootstrap a repository.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


HUB_DIRNAME = ".aiworkhub"
PROJECT_MANIFEST_REL = Path(HUB_DIRNAME) / "project.json"
MANIFEST_SCHEMA_ID = "aiworkhub.project_manifest.v1"
MANIFEST_VERSION = 1
LAYOUT_VERSION = 1
REPO_ID_RE = re.compile(r"^repo_[a-f0-9]{32}$|^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")

DURABLE_LAYOUT: dict[str, str] = {
    "tasking": "tasking",
    "source_graph": "source_graph",
    "sessions": "sessions",
    "memory": "memory",
    "kb": "kb",
    "config": "config",
}
RUNTIME_DIRNAME = "runtime"


class RepositoryStateError(RuntimeError):
    """Base class for fail-closed repository-state errors."""


class RepositoryNotFoundError(RepositoryStateError):
    """No repository root could be resolved."""


class ManifestMissingError(RepositoryStateError):
    """A repository root exists, but no AIWorkHub manifest is present."""


class ManifestExistsError(RepositoryStateError):
    """Bootstrap refused to overwrite an existing manifest."""


class ManifestInvalidError(RepositoryStateError):
    """Manifest content or identity is invalid."""


class PathEscapeError(RepositoryStateError):
    """A path is absolute, traverses upward, is a symlink, or escapes root."""


@dataclass(frozen=True, slots=True)
class RepositoryManifest:
    repo_id: str
    repo_name: str
    created_at: str
    schema_id: str = MANIFEST_SCHEMA_ID
    manifest_version: int = MANIFEST_VERSION
    layout_version: int = LAYOUT_VERSION

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "RepositoryManifest":
        if payload.get("schema_id") != MANIFEST_SCHEMA_ID:
            raise ManifestInvalidError("manifest_schema_id_invalid")
        if payload.get("manifest_version") != MANIFEST_VERSION:
            raise ManifestInvalidError("manifest_version_invalid")
        if payload.get("layout_version") != LAYOUT_VERSION:
            raise ManifestInvalidError("layout_version_invalid")
        repo_id = str(payload.get("repo_id") or "")
        if not is_valid_repo_id(repo_id):
            raise ManifestInvalidError("repo_id_invalid")
        layout = payload.get("layout")
        if not isinstance(layout, dict):
            raise ManifestInvalidError("layout_missing")
        if layout.get("durable") != DURABLE_LAYOUT:
            raise ManifestInvalidError("durable_layout_invalid")
        runtime = layout.get("runtime")
        if not isinstance(runtime, dict) or runtime.get("path") != RUNTIME_DIRNAME:
            raise ManifestInvalidError("runtime_layout_invalid")
        if runtime.get("durable") is not False or runtime.get("ignored") is not True:
            raise ManifestInvalidError("runtime_must_be_ignored_non_durable")
        return cls(
            repo_id=repo_id,
            repo_name=str(payload.get("repo_name") or ""),
            created_at=str(payload.get("created_at") or ""),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_id": self.schema_id,
            "manifest_version": self.manifest_version,
            "layout_version": self.layout_version,
            "repo_id": self.repo_id,
            "repo_name": self.repo_name,
            "created_at": self.created_at,
            "layout": {
                "durable": dict(DURABLE_LAYOUT),
                "runtime": {
                    "path": RUNTIME_DIRNAME,
                    "durable": False,
                    "ignored": True,
                    "contains": ["process_logs", "sockets", "locks", "worktrees"],
                },
            },
            "security": {
                "credentials_in_repository": False,
                "host_runtime_state_in_durable_payload": False,
                "automatic_legacy_discovery": False,
            },
        }


@dataclass(frozen=True, slots=True)
class RepositoryState:
    root: Path
    hub_dir: Path
    manifest_path: Path
    manifest: RepositoryManifest
    durable_paths: dict[str, Path]
    runtime_path: Path


def is_valid_repo_id(value: str) -> bool:
    return bool(value and "\x00" not in value and REPO_ID_RE.match(value))


def new_repo_id() -> str:
    return f"repo_{uuid.uuid4().hex}"


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts[len(current.parts):]:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _resolve_existing_dir(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RepositoryNotFoundError(f"repository_root_unavailable:{path}") from exc
    if _has_symlink_component(path.expanduser()):
        raise PathEscapeError(f"repository_root_symlink_component:{path}")
    if not resolved.is_dir():
        raise RepositoryNotFoundError(f"repository_root_not_directory:{path}")
    return resolved


def _assert_relative_child(root: Path, relative: str | Path) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or "\x00" in rel.as_posix() or ".." in rel.parts:
        raise PathEscapeError(f"path_escape:{relative}")
    candidate = root / rel
    if _has_symlink_component(candidate):
        raise PathEscapeError(f"path_symlink_component:{relative}")
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathEscapeError(f"path_unresolvable:{relative}") from exc
    if resolved != root and root not in resolved.parents:
        raise PathEscapeError(f"path_outside_repository:{relative}")
    return resolved


def _find_upward(start: Path, marker: Path, *, boundary: Path | None = None) -> Path | None:
    """Search ``start`` and its ancestors for ``marker``.

    When ``boundary`` is given, the search never looks past it: ``boundary``
    itself is still checked, but nothing above it is. This is what keeps a
    nested independent git repository from being resolved as untracked
    content of whatever outer repository happens to sit above it -- without
    a boundary, a manifest search from inside the nested repo would keep
    climbing past its own ``.git`` and silently bind to the outer repo's
    ``.aiworkhub/project.json`` instead.
    """
    current = _resolve_existing_dir(start)
    for directory in (current, *current.parents):
        candidate = directory / marker
        if candidate.exists():
            if _has_symlink_component(candidate):
                raise PathEscapeError(f"marker_symlink_component:{candidate}")
            return directory
        if boundary is not None and directory == boundary:
            break
    return None


def _git_root_from(start: Path) -> Path | None:
    return _find_upward(start, Path(".git"))


def resolve_repository_root(
    explicit_root: str | Path | None = None,
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    require_manifest: bool = True,
) -> Path:
    """Resolve a repository root with explicit precedence.

    Precedence is: explicit_root, AIWORKHUB_REPO_ROOT env, cwd ancestor with
    .aiworkhub/project.json, then cwd git root.  When require_manifest is
    true, the winning root must contain a valid manifest.
    """

    environ = env if env is not None else os.environ
    start = Path(cwd or os.getcwd())
    if explicit_root is not None:
        root = _resolve_existing_dir(Path(explicit_root))
    elif environ.get("AIWORKHUB_REPO_ROOT"):
        root = _resolve_existing_dir(Path(str(environ["AIWORKHUB_REPO_ROOT"])))
    else:
        # Bind to the nearest enclosing git repository, never past it: a
        # nested independent repository (its own ``.git``) must resolve to
        # its own manifest/root, not escape upward into whatever outer
        # repository happens to contain it.
        git_root = _git_root_from(start)
        root = _find_upward(start, PROJECT_MANIFEST_REL, boundary=git_root) or git_root
        if root is None:
            raise RepositoryNotFoundError("repository_root_not_found")

    if require_manifest:
        inspect_repository(root)
    return root


def _read_manifest(path: Path) -> RepositoryManifest:
    if _has_symlink_component(path):
        raise PathEscapeError(f"manifest_symlink_component:{path}")
    if not path.is_file():
        raise ManifestMissingError(f"manifest_missing:{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestInvalidError(f"manifest_unreadable:{path}") from exc
    if not isinstance(payload, dict):
        raise ManifestInvalidError("manifest_must_be_object")
    return RepositoryManifest.from_json(payload)


def inspect_repository(
    root: str | Path | None = None,
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    expected_repo_id: str | None = None,
) -> RepositoryState:
    repo = (
        resolve_repository_root(root, cwd=cwd, env=env, require_manifest=False)
        if root is not None
        else resolve_repository_root(cwd=cwd, env=env, require_manifest=False)
    )
    hub = _assert_relative_child(repo, HUB_DIRNAME)
    manifest_path = _assert_relative_child(repo, PROJECT_MANIFEST_REL)
    manifest = _read_manifest(manifest_path)
    if expected_repo_id is not None and manifest.repo_id != expected_repo_id:
        raise ManifestInvalidError("repo_id_mismatch")
    durable_paths = {
        name: _assert_relative_child(hub, rel)
        for name, rel in DURABLE_LAYOUT.items()
    }
    runtime_path = _assert_relative_child(hub, RUNTIME_DIRNAME)
    for name, path in durable_paths.items():
        if path.exists() and not path.is_dir():
            raise ManifestInvalidError(f"durable_path_not_directory:{name}")
    if runtime_path.exists() and not runtime_path.is_dir():
        raise ManifestInvalidError("runtime_path_not_directory")
    return RepositoryState(
        root=repo,
        hub_dir=hub,
        manifest_path=manifest_path,
        manifest=manifest,
        durable_paths=durable_paths,
        runtime_path=runtime_path,
    )


def _atomic_write_text(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
            text=True,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        tmp_name = ""
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, data)


def bootstrap_repository(
    root: str | Path | None = None,
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    repo_id: str | None = None,
    repo_name: str | None = None,
    created_at: str | None = None,
) -> RepositoryState:
    """Create .aiworkhub/project.json and canonical directories.

    Existing manifests are never overwritten.  Passing repo_id makes identity
    adoption explicit; if a manifest already exists with a different ID this
    call fails instead of accepting it.
    """

    repo = resolve_repository_root(root, cwd=cwd, env=env, require_manifest=False)
    hub = _assert_relative_child(repo, HUB_DIRNAME)
    manifest_path = _assert_relative_child(repo, PROJECT_MANIFEST_REL)
    if hub.exists() and not hub.is_dir():
        raise PathEscapeError("hub_path_not_directory")
    if manifest_path.exists():
        existing = _read_manifest(manifest_path)
        if repo_id is not None and existing.repo_id != repo_id:
            raise ManifestInvalidError("existing_repo_id_mismatch")
        raise ManifestExistsError("manifest_already_exists")
    final_repo_id = repo_id or new_repo_id()
    if not is_valid_repo_id(final_repo_id):
        raise ManifestInvalidError("repo_id_invalid")

    manifest = RepositoryManifest(
        repo_id=final_repo_id,
        repo_name=repo_name or repo.name,
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
    )

    # Phase 1: create idempotent layout (dirs + .gitignore).
    # These are safe to re-run: crash before phase 2 leaves NO manifest,
    # so the next attach retries cleanly.
    hub.mkdir(parents=True, exist_ok=True)
    if _has_symlink_component(hub):
        raise PathEscapeError("hub_symlink_component")
    for rel in (*DURABLE_LAYOUT.values(), RUNTIME_DIRNAME):
        path = _assert_relative_child(hub, rel)
        path.mkdir(parents=True, exist_ok=True)
    runtime_ignore = _assert_relative_child(hub, Path(RUNTIME_DIRNAME) / ".gitignore")
    if not runtime_ignore.exists():
        _atomic_write_text(runtime_ignore, "*\n!.gitignore\n")
    from .storage_registry import STORAGE_REGISTRY_REL, default_registry_payload

    registry_path = _assert_relative_child(repo, STORAGE_REGISTRY_REL)
    if not registry_path.exists():
        _atomic_write_json(registry_path, default_registry_payload(final_repo_id))

    # Phase 2: publish manifest as the ATOMIC SEAL — always the final write.
    # If anything fails before this point, no valid manifest exists and the
    # next attach can retry cleanly.
    _atomic_write_json(manifest_path, manifest.to_json())
    return inspect_repository(repo, expected_repo_id=final_repo_id)


__all__ = [
    "DURABLE_LAYOUT",
    "HUB_DIRNAME",
    "LAYOUT_VERSION",
    "MANIFEST_SCHEMA_ID",
    "MANIFEST_VERSION",
    "PROJECT_MANIFEST_REL",
    "PathEscapeError",
    "ManifestExistsError",
    "ManifestInvalidError",
    "ManifestMissingError",
    "RepositoryManifest",
    "RepositoryNotFoundError",
    "RepositoryState",
    "RepositoryStateError",
    "bootstrap_repository",
    "inspect_repository",
    "is_valid_repo_id",
    "new_repo_id",
    "resolve_repository_root",
]
