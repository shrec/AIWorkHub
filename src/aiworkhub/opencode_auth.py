"""Fail-closed OpenCode credential projection for isolated worker homes.

Only OpenCode's exact ``auth.json`` is copied.  The source is read through one
verified descriptor and the destination is materialized atomically below the
request-private HOME; provider databases, configuration, logs, caches, and
session state are never traversed.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Union

from .platform_io import (
    chmod_fd,
    chmod_path,
    posix_path_modes_supported,
    stat_owned_by_current_user,
)

__all__ = [
    "MAX_SOURCE_BYTES",
    "OPENCODE_AUTH_RELATIVE_PATH",
    "OpenCodeAuthDestinationError",
    "OpenCodeAuthError",
    "OpenCodeAuthProjection",
    "OpenCodeAuthSourceError",
    "project_opencode_auth",
]

OPENCODE_AUTH_RELATIVE_PATH = (
    Path(".local") / "share" / "opencode" / "auth.json"
)
MAX_SOURCE_BYTES = 1 << 20
_READ_CHUNK_BYTES = 64 * 1024

StrPath = Union[str, PathLike[str]]


class OpenCodeAuthError(Exception):
    """A stable, secret-free credential projection failure."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class OpenCodeAuthSourceError(OpenCodeAuthError):
    """The host credential source failed validation."""


class OpenCodeAuthDestinationError(OpenCodeAuthError):
    """The isolated destination could not be secured or written."""


@dataclass(frozen=True)
class OpenCodeAuthProjection:
    """Secret-free receipt for a successful projection."""

    source: str
    destination: str
    byte_count: int
    status: str = "projected"


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_size),
        int(getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1e9))),
        int(getattr(metadata, "st_ctime_ns", int(metadata.st_ctime * 1e9))),
    )


def _read_verified_source(source: Path) -> bytes:
    if not source.is_absolute() or ".." in source.parts:
        raise OpenCodeAuthSourceError("opencode_auth_source_path_invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(source, flags)
    except OSError:
        raise OpenCodeAuthSourceError("opencode_auth_source_unavailable") from None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise OpenCodeAuthSourceError("opencode_auth_source_not_regular")
        if not stat_owned_by_current_user(before):
            raise OpenCodeAuthSourceError("opencode_auth_source_wrong_owner")
        if posix_path_modes_supported() and stat.S_IMODE(before.st_mode) & 0o022:
            raise OpenCodeAuthSourceError("opencode_auth_source_writable_by_others")
        if before.st_size <= 0 or before.st_size > MAX_SOURCE_BYTES:
            raise OpenCodeAuthSourceError("opencode_auth_source_size_invalid")

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(_READ_CHUNK_BYTES, MAX_SOURCE_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SOURCE_BYTES:
                raise OpenCodeAuthSourceError("opencode_auth_source_size_invalid")
            chunks.append(chunk)

        after = os.fstat(fd)
        try:
            current = os.stat(source, follow_symlinks=False)
        except OSError:
            raise OpenCodeAuthSourceError("opencode_auth_source_identity_changed") from None
        if (
            _identity(before) != _identity(after)
            or before.st_dev != current.st_dev
            or before.st_ino != current.st_ino
            or not stat.S_ISREG(current.st_mode)
            or total != before.st_size
        ):
            raise OpenCodeAuthSourceError("opencode_auth_source_identity_changed")
        raw = b"".join(chunks)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise OpenCodeAuthSourceError("opencode_auth_source_json_invalid") from None
        if not isinstance(payload, dict) or not payload:
            raise OpenCodeAuthSourceError("opencode_auth_source_json_invalid")
        return raw
    finally:
        os.close(fd)


def _private_directory(path: Path, *, create_parents: bool = False) -> None:
    try:
        if create_parents:
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
        else:
            path.mkdir(mode=0o700, exist_ok=True)
        metadata = path.lstat()
    except OSError:
        raise OpenCodeAuthDestinationError(
            "opencode_auth_destination_directory_unavailable"
        ) from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OpenCodeAuthDestinationError(
            "opencode_auth_destination_directory_unsafe"
        )
    try:
        chmod_path(path, 0o700)
    except OSError:
        raise OpenCodeAuthDestinationError(
            "opencode_auth_destination_permissions_failed"
        ) from None
    if posix_path_modes_supported() and stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise OpenCodeAuthDestinationError(
            "opencode_auth_destination_permissions_failed"
        )


def _destination_directory(home: Path) -> Path:
    if not home.is_absolute() or ".." in home.parts:
        raise OpenCodeAuthDestinationError("opencode_auth_destination_path_invalid")
    _private_directory(home, create_parents=True)
    current = home
    for component in OPENCODE_AUTH_RELATIVE_PATH.parts[:-1]:
        current = current / component
        _private_directory(current)
    return current


def _write_atomic(directory: Path, destination: Path, raw: bytes) -> None:
    try:
        existing = destination.lstat()
    except FileNotFoundError:
        existing = None
    except OSError:
        raise OpenCodeAuthDestinationError(
            "opencode_auth_destination_unavailable"
        ) from None
    if existing is not None and (
        stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
    ):
        raise OpenCodeAuthDestinationError("opencode_auth_destination_unsafe")

    try:
        fd, temporary_name = tempfile.mkstemp(
            dir=directory, prefix=".opencode-auth.", suffix=".tmp"
        )
    except OSError:
        raise OpenCodeAuthDestinationError(
            "opencode_auth_destination_create_failed"
        ) from None
    temporary = Path(temporary_name)
    try:
        try:
            chmod_fd(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            chmod_path(destination, 0o600)
        except OSError:
            raise OpenCodeAuthDestinationError(
                "opencode_auth_destination_write_failed"
            ) from None
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass

    try:
        published = destination.lstat()
    except OSError:
        raise OpenCodeAuthDestinationError(
            "opencode_auth_destination_verify_failed"
        ) from None
    if stat.S_ISLNK(published.st_mode) or not stat.S_ISREG(published.st_mode):
        raise OpenCodeAuthDestinationError("opencode_auth_destination_unsafe")
    if posix_path_modes_supported() and stat.S_IMODE(published.st_mode) != 0o600:
        raise OpenCodeAuthDestinationError(
            "opencode_auth_destination_permissions_failed"
        )


def project_opencode_auth(
    source: StrPath, isolated_home: StrPath
) -> OpenCodeAuthProjection:
    """Copy only one verified OpenCode auth file into an isolated HOME."""

    source_path = Path(os.fspath(source))
    home = Path(os.fspath(isolated_home))
    raw = _read_verified_source(source_path)
    directory = _destination_directory(home)
    destination = home / OPENCODE_AUTH_RELATIVE_PATH
    _write_atomic(directory, destination, raw)
    return OpenCodeAuthProjection(
        source=str(source_path),
        destination=str(destination),
        byte_count=len(raw),
    )
