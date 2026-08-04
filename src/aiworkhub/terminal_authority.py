from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .platform_io import chmod_fd
from .worker_workspace import write_json_0600


SCHEMA_ID = "aiworkhub.task_mcp.terminal_authority.v1"
KEY_FILENAME = ".terminal_authority_hmac.key"


def signing_material(
    *, repo: Path, task_id: str, runner: str, topic: str, request_id: str,
) -> bytes:
    return "|".join(
        [SCHEMA_ID, str(repo), task_id, runner, topic, request_id]
    ).encode("utf-8")


def _trusted_key_file(st: os.stat_result, *, platform_name: str) -> bool:
    """Return whether an opened key file satisfies the host trust model.

    Windows does not represent profile ACLs through POSIX permission bits:
    ``os.chmod(..., 0o600)`` commonly reads back as ``0o666`` or ``0o444``.
    Requiring an exact ``0o600`` there rejected AIWorkHub's own existing key,
    then the create-once race path recursively retried forever.  POSIX keeps
    the original owner + exact-mode requirement.
    """

    if not stat.S_ISREG(st.st_mode):
        return False
    if platform_name == "nt":
        return True
    return st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o600


def _read_existing_key(key_path: Path, *, platform_name: str) -> bytes | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(key_path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not _trusted_key_file(st, platform_name=platform_name):
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(33)
        return data if len(data) == 32 else None
    finally:
        os.close(fd)


def load_or_create_key(
    key_path: Path,
    *,
    _platform_name: str | None = None,
) -> bytes:
    """Load or atomically mint one owner-only process-directory HMAC key.

    Create races are reconciled with a small bounded loop.  A pre-existing
    untrusted or malformed key fails closed instead of recursively calling
    this function until Python's recursion limit terminates worker launch.
    ``_platform_name`` exists only for deterministic cross-platform tests.
    """

    platform_name = _platform_name or os.name
    key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(key_path.parent, 0o700)
    for _attempt in range(3):
        existing = _read_existing_key(key_path, platform_name=platform_name)
        if existing is not None:
            return existing
        if key_path.exists():
            existing = _read_existing_key(key_path, platform_name=platform_name)
            if existing is not None:
                return existing
            raise RuntimeError("terminal_authority_key_invalid")

        key = os.urandom(32)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            new_fd = os.open(key_path, flags, 0o600)
        except FileExistsError:
            # Another process won the create race. Re-open and validate the
            # exact file on the next bounded iteration; never recurse.
            continue
        chmod_fd(new_fd, 0o600)
        with os.fdopen(new_fd, "wb") as handle:
            handle.write(key)
        return key
    raise RuntimeError("terminal_authority_key_race_unresolved")


def write_grant(
    path: Path,
    key: bytes,
    *,
    repo: Path,
    task_id: str,
    runner: str,
    topic: str,
    request_id: str,
) -> None:
    material = signing_material(
        repo=repo,
        task_id=task_id,
        runner=runner,
        topic=topic,
        request_id=request_id,
    )
    write_json_0600(
        path,
        {
            "schema_id": SCHEMA_ID,
            "repo": str(repo),
            "task_id": task_id,
            "runner": runner,
            "topic": topic,
            "request_id": request_id,
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "signature": hmac.new(key, material, hashlib.sha256).hexdigest(),
        },
    )


def read_grant(path: Path) -> dict[str, Any]:
    """Read one owner-only, non-symlink grant; malformed input is empty."""

    try:
        st = path.lstat()
    except OSError:
        return {}
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return {}
    # POSIX enforces owner equivalence via mode bits; Windows relies on the
    # ACL-protected per-user profile, so os.getuid() is skipped there.
    if (os.name != "nt" and (st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077)):
        return {}
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return {}
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            payload = json.loads(handle.read())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = [
    "KEY_FILENAME",
    "SCHEMA_ID",
    "load_or_create_key",
    "read_grant",
    "signing_material",
    "write_grant",
]
