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


def load_or_create_key(key_path: Path) -> bytes:
    """Load or atomically mint one owner-only process-directory HMAC key."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(key_path, flags)
    except OSError:
        fd = None
    if fd is not None:
        try:
            st = os.fstat(fd)
            # os.getuid() is POSIX-only; on Windows the per-user profile
            # directory is ACL-protected instead of mode-bit protected, so
            # owner equivalence is enforced only where mode bits are meaningful.
            if (
                stat.S_ISREG(st.st_mode)
                and (os.name == "nt" or st.st_uid == os.getuid())
                and stat.S_IMODE(st.st_mode) == 0o600
            ):
                with os.fdopen(fd, "rb") as handle:
                    data = handle.read()
                if len(data) == 32:
                    return data
            else:
                os.close(fd)
        except OSError:
            pass
    key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(key_path.parent, 0o700)
    key = os.urandom(32)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        new_fd = os.open(key_path, flags, 0o600)
    except FileExistsError:
        return load_or_create_key(key_path)
    chmod_fd(new_fd, 0o600)
    with os.fdopen(new_fd, "wb") as handle:
        handle.write(key)
    return key


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
