from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .platform_io import (
    chmod_fd,
    is_windows,
    stat_owned_by_current_user,
    windows_descriptor_secret_trust,
    windows_harden_owner_only_key_dacl,
)
from .worker_workspace import write_json_0600


SCHEMA_ID = "aiworkhub.task_mcp.terminal_authority.v1"
KEY_FILENAME = ".terminal_authority_hmac.key"


def signing_material(
    *, repo: Path, task_id: str, runner: str, topic: str, request_id: str,
) -> bytes:
    return "|".join(
        [SCHEMA_ID, str(repo), task_id, runner, topic, request_id]
    ).encode("utf-8")


def _trusted_key_file(
    st: os.stat_result, *, platform_name: str, fd: int | None = None
) -> bool:
    """Return whether an opened key file satisfies the host trust model.

    POSIX asks this with ``st_uid`` plus an exact ``0o600`` mode. Windows can
    answer neither: ``os.chmod(..., 0o600)`` reads back as ``0o666`` and
    ``st_uid`` is always 0, so requiring the POSIX answer there rejected
    AIWorkHub's own key and drove the create-once path into an endless retry.

    NF-2026-00011: answering "trusted" unconditionally instead removed the owner
    check altogether, so a key planted by any other principal was accepted.
    Windows now asks the EQUIVALENT question -- owner plus DACL -- against the
    security descriptor of this exact open descriptor.
    """

    if not stat.S_ISREG(st.st_mode):
        return False
    if is_windows(platform_name):
        if not is_windows() or fd is None:
            # Cross-platform tests drive the Windows BRANCH from a POSIX host,
            # where there is no Windows security descriptor to read.
            return True
        trusted, _reason = windows_descriptor_secret_trust(fd)
        return trusted
    return stat_owned_by_current_user(
        st, platform_name=platform_name
    ) and stat.S_IMODE(st.st_mode) == 0o600


def _windows_link_identity(key_path: Path) -> os.stat_result | None:
    """Refuse a reparse point before opening, and pin the identity observed.

    Windows has no ``O_NOFOLLOW``, so ``os.open`` follows a symlink silently and
    the key file could be redirected anywhere. The link is refused here, and the
    caller re-checks ``(st_dev, st_ino)`` on the OPEN descriptor so a swap
    performed between these two steps is refused as well.
    """

    try:
        link_st = os.lstat(key_path)
    except OSError:
        return None
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(link_st.st_mode) or (
        reparse and getattr(link_st, "st_file_attributes", 0) & reparse
    ):
        return None
    return link_st


def _read_existing_key(key_path: Path, *, platform_name: str) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    link_identity: os.stat_result | None = None
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    else:
        # No O_NOFOLLOW on this runtime (Windows): pin the pre-open identity.
        link_identity = _windows_link_identity(key_path)
        if link_identity is None:
            return None
    try:
        fd = os.open(key_path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if link_identity is not None and (
            not st.st_ino
            or (st.st_dev, st.st_ino) != (link_identity.st_dev, link_identity.st_ino)
        ):
            return None
        if not _trusted_key_file(st, platform_name=platform_name, fd=fd):
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(33)
        return data if len(data) == 32 else None
    finally:
        os.close(fd)


def _windows_untrusted_reason(key_path: Path) -> str:
    """Name WHY an existing key on Windows fails the trust check, for the
    diagnostic only -- this never changes whether the key is accepted.

    Deliberately does not attempt any repair. A key someone else broadened
    the ACL on after creation, and a key this process itself created before
    the write-side DACL hardening existed, produce the IDENTICAL evidence from
    the file alone (an over-broad DACL); silently accepting one to fix the
    other would also silently accept the first. Naming the reason keeps the
    refusal fail-closed while turning "terminal_authority_key_invalid" from an
    unexplained dead end into an actionable diagnostic -- delete the file and
    let the (now-hardened) create path mint a new one.
    """

    try:
        fd = os.open(key_path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return "unreadable"
    try:
        _trusted, reason = windows_descriptor_secret_trust(fd)
        return reason or "untrusted"
    except OSError:
        return "unreadable"
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
            reason = (
                _windows_untrusted_reason(key_path)
                if is_windows(platform_name)
                else "owner_or_mode_mismatch"
            )
            raise RuntimeError(f"terminal_authority_key_invalid:{reason}")

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
        if is_windows(platform_name):
            # Harden the DACL before this key is ever readable as trusted, so
            # the create path can never again produce one the read side must
            # refuse. Fail closed: an unhardened key is deleted, never kept.
            hardened, reason = windows_harden_owner_only_key_dacl(key_path)
            if not hardened:
                os.close(new_fd)
                try:
                    key_path.unlink()
                except OSError:
                    pass
                raise RuntimeError(
                    f"terminal_authority_key_dacl_hardening_failed:{reason}"
                )
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
    # POSIX enforces owner equivalence via uid + mode bits; Windows relies on
    # the ACL-protected per-user profile.
    if not stat_owned_by_current_user(st) or (
        os.name != "nt" and stat.S_IMODE(st.st_mode) & 0o077
    ):
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
