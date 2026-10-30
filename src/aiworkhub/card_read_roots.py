"""Card ``read_roots``: host directories a provider worker may READ (NF-2026-01857).

A card declares up to ``MAX_READ_ROOTS`` absolute directories outside its
worktree (runtime data a worker must read).  ``validate_read_roots`` is the one
validator, run at create and again at launch; ``read_root_grants`` turns the
validated roots into persistent ``read_execute`` AppContainer grants.

This module has no package-level imports, so the direct-script worker
supervisor can import it.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Imported lazily in read_root_grants: validation must work where the Windows
# AppContainer module cannot be imported, and the direct-script supervisor
# needs the try/except spelling there.

if TYPE_CHECKING:
    from .windows_appcontainer import ContainerGrant

MAX_READ_ROOTS = 16
_PATH_ECHO_CHARS = 240
_FORBIDDEN_COMPONENTS = frozenset({".git", ".aiworkhub"})
PROFILE_SECRET_RELATIVE_PATHS = (
    ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker", ".claude", ".codex",
    os.path.join(".config", "gh"),
    os.path.join("AppData", "Roaming", "gh"),
    os.path.join("AppData", "Roaming", "Microsoft", "Credentials"),
    os.path.join("AppData", "Local", "Microsoft", "Credentials"),
    os.path.join("AppData", "Roaming", "Microsoft", "Protect"),
)


def _invalid(reason: str, detail: object) -> ValueError:
    return ValueError(f"read_root_invalid:{reason}:{str(detail)[:_PATH_ECHO_CHARS]}")


def _key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(path)))


def _real_key(path: str | os.PathLike[str]) -> str:
    # realpath: a junction or symlink must not hide that a root lies in a writable tree.
    return os.path.normcase(os.path.realpath(path))


def _contains(parent: str, child: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("\\/") + os.sep)


def _nearest_git_ancestor(key: str) -> str | None:
    current = key
    while True:
        if os.path.lexists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _check_syntax(item: Any) -> str:
    if not isinstance(item, str):
        raise _invalid("not_a_string", type(item).__name__)
    if not item.strip():
        raise _invalid("empty", "")
    if "\x00" in item:
        raise _invalid("nul_character", item.replace("\x00", "\\x00"))
    if item.startswith(("\\\\", "//")):
        raise _invalid("unc_or_device_path", item)
    drive, rest = os.path.splitdrive(item)
    if not os.path.isabs(item) or (os.name == "nt" and (not drive or rest[:1] not in ("\\", "/"))):
        raise _invalid("not_absolute", item)
    return os.path.normpath(item)


def _check_root(root: str, *, repo_key: str, home_key: str) -> str:
    if not os.path.isdir(root):
        raise _invalid("not_an_existing_directory", root)
    key = _key(root)
    if _real_key(root) != key:
        raise _invalid("reparse_point", root)
    if os.path.dirname(key) == key:
        raise _invalid("volume_root", root)
    if any(part.casefold() in _FORBIDDEN_COMPONENTS for part in Path(root).parts):
        raise _invalid("forbidden_component", root)
    if _contains(key, repo_key):
        raise _invalid("contains_repo_root", root)
    if _contains(key, home_key):
        raise _invalid("contains_home", root)
    for relative in PROFILE_SECRET_RELATIVE_PATHS:
        secret = _key(os.path.join(home_key, relative))
        # A root above a secret directory exposes it exactly as a root inside it.
        if _contains(secret, key) or _contains(key, secret):
            raise _invalid("profile_secret", root)
    git_root = _nearest_git_ancestor(key)
    if git_root is not None and git_root != repo_key:
        raise _invalid("foreign_git_repository", root)
    return root


def validate_read_roots(raw: Any, *, repo_root: str | os.PathLike[str]) -> list[str]:
    """Normalized, deduplicated roots with nested ones collapsed to the outer.

    Raises ``ValueError('read_root_invalid:<reason>:<path>')``.  ``None`` and an
    empty list are the no-roots case and return ``[]``.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _invalid("not_a_list", type(raw).__name__)
    if len(raw) > MAX_READ_ROOTS:
        raise _invalid("too_many", f"{len(raw)}>{MAX_READ_ROOTS}")
    repo_key = _real_key(repo_root)
    home_key = _real_key(Path.home())
    checked: dict[str, str] = {}
    for item in raw:
        root = _check_root(_check_syntax(item), repo_key=repo_key, home_key=home_key)
        checked.setdefault(_key(root), root)
    return [
        root
        for key, root in checked.items()
        if not any(other != key and _contains(other, key) for other in checked)
    ]


def read_root_grants(
    roots: Sequence[str], *, covered: Sequence[str] = ()
) -> list[ContainerGrant]:
    """Persistent read/execute on each root, like ``Toolchain.read_grants``.

    ``covered`` is what the request already lets the container write; a root
    inside it or containing it is refused.  Persistent because a revoke strips
    every ACE of the container SID on that path and would break a concurrent
    sibling worker; read_execute because it is the only access that may persist.
    """
    try:
        from . import windows_appcontainer
    except ImportError:  # direct-script entrypoint (worker_supervisor)
        import windows_appcontainer  # type: ignore[no-redef]

    writable = [_real_key(path) for path in covered]
    for root in roots:
        key = _real_key(root)
        if key != _key(root):
            raise windows_appcontainer.AppContainerError(
                windows_appcontainer.AppContainerReason.INVALID_REQUEST,
                detail=f"a read root became a reparse point before the grant: {root!r}.",
            )
        if any(_contains(w, key) or _contains(key, w) for w in writable):
            raise windows_appcontainer.AppContainerError(
                windows_appcontainer.AppContainerReason.INVALID_REQUEST,
                detail=f"a read root the container can write is never granted: {root!r}.",
            )
    return windows_appcontainer.outside_system_trees([
        windows_appcontainer.ContainerGrant(root, "read_execute", persistent=True)
        for root in roots
    ])
