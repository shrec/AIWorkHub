"""Fail-closed projection of the xAI provider record into an isolated Kilo HOME.

Standalone credential boundary for the future ``grok_kilo_cli`` adapter
(production model ``xai/grok-4.6``).  This module reads one explicitly
supplied Kilo ``auth.json``, validates it as a regular non-symlink bounded
JSON file, extracts only the exact ``xai`` provider record, and writes a
request-local ``auth.json`` below an explicitly supplied isolated HOME using
atomic replacement and restrictive permissions.  It never starts a process,
never consults an ambient HOME, and never touches network APIs.

Invariants:

* Only the exact ``xai`` provider record is projected; ``kilo`` and every
  unrelated provider are stripped before anything is written.
* The source auth file is never modified and the ambient user HOME is never
  read or written; only the two explicitly supplied paths are touched.
* Success returns a receipt limited to paths, the provider identity ``xai``,
  byte/hash metadata, and status; failure raises a typed ``KiloAuthError``
  whose message never contains file contents or token material.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Union

__all__ = [
    "AUTH_FILENAME",
    "KILO_AUTH_RELATIVE_PATH",
    "KiloAuthDestinationError",
    "KiloAuthError",
    "KiloAuthProjection",
    "KiloAuthProviderMissing",
    "KiloAuthSourceError",
    "MAX_SOURCE_BYTES",
    "PROVIDER_ID",
    "project_xai_auth",
    "resolve_kilo_auth_source",
]

# The only provider identity ever read, projected, or reported.
PROVIDER_ID: str = "xai"

# Fixed final component of the Kilo data file.  Being a literal (never
# caller-supplied) keeps the destination confined strictly inside that HOME.
AUTH_FILENAME: str = "auth.json"

# Exact request-local destination layout below the isolated HOME.  The whole
# path is a literal, so the projection can never escape the explicit HOME.
KILO_AUTH_RELATIVE_PATH: Path = Path(".local") / "share" / "kilo" / AUTH_FILENAME

# Hard upper bound on the source auth file.  Larger inputs fail closed before
# any credential material is parsed.
MAX_SOURCE_BYTES: int = 1 << 20

StrPath = Union[str, PathLike]

_READ_CHUNK_BYTES = 65536


class KiloAuthError(Exception):
    """Fail-closed credential projection failure (never carries token material)."""

    def __init__(self, reason: str, path: StrPath | None = None) -> None:
        self.reason = reason
        self.path = None if path is None else os.fspath(path)
        detail = f": {self.path}" if self.path is not None else ""
        super().__init__(f"{reason}{detail}")


class KiloAuthSourceError(KiloAuthError):
    """The explicitly supplied source auth file failed validation."""


class KiloAuthProviderMissing(KiloAuthSourceError):
    """The source auth file contains no ``xai`` provider record."""


class KiloAuthDestinationError(KiloAuthError):
    """The isolated HOME destination failed validation or could not be written."""


@dataclass(frozen=True)
class KiloAuthProjection:
    """Receipt for one successful projection.

    Fields are limited to paths, the provider identity ``xai``, byte/hash
    metadata, and status; no token string ever appears here.
    """

    provider: str
    source: str
    destination: str
    source_bytes: int
    source_sha256: str
    destination_bytes: int
    destination_sha256: str
    status: str = "projected"


def _read_source_bytes(source: Path) -> bytes:
    """Read the bounded source file once, without following symlinks."""
    if not source.is_absolute():
        raise KiloAuthSourceError("source must be an absolute path", source)
    if source.is_symlink():
        raise KiloAuthSourceError("source must not be a symlink", source)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(source, flags)
    except OSError:
        raise KiloAuthSourceError(
            "source is missing or cannot be opened", source
        ) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise KiloAuthSourceError("source must be a regular file", source)
        if info.st_size > MAX_SOURCE_BYTES:
            raise KiloAuthSourceError("source exceeds the size bound", source)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, _READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SOURCE_BYTES:
                raise KiloAuthSourceError("source exceeds the size bound", source)
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _extract_xai_record(source: Path, raw: bytes) -> dict:
    """Parse the source JSON object and return only the ``xai`` record."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise KiloAuthSourceError("source is not valid JSON", source) from None
    if not isinstance(payload, dict):
        raise KiloAuthSourceError("source JSON root must be an object", source)
    if PROVIDER_ID not in payload:
        raise KiloAuthProviderMissing("source has no xai provider record", source)
    record = payload[PROVIDER_ID]
    if not isinstance(record, dict):
        raise KiloAuthSourceError("xai provider record must be an object", source)
    return record


def _prepare_isolated_home(home: Path) -> Path:
    """Validate the explicit isolated HOME and make its permissions restrictive."""
    if not home.is_absolute():
        raise KiloAuthDestinationError(
            "isolated HOME must be an absolute path", home
        )
    if ".." in home.parts:
        raise KiloAuthDestinationError(
            "isolated HOME must not contain '..' components", home
        )
    try:
        os.makedirs(home, mode=0o700, exist_ok=True)
        info = os.lstat(home)
    except OSError:
        raise KiloAuthDestinationError(
            "isolated HOME cannot be created", home
        ) from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise KiloAuthDestinationError(
            "isolated HOME must be a non-symlink directory", home
        )
    if os.name == "posix":
        try:
            os.chmod(home, 0o700)
        except OSError:
            raise KiloAuthDestinationError(
                "isolated HOME permissions cannot be restricted", home
            ) from None
    return home


def _prepare_kilo_data_dir(home: Path) -> Path:
    """Create Kilo's request-local data directory without following symlinks."""
    current = home
    for component in KILO_AUTH_RELATIVE_PATH.parts[:-1]:
        current = current / component
        try:
            os.mkdir(current, mode=0o700)
        except FileExistsError:
            pass
        except OSError:
            raise KiloAuthDestinationError(
                "Kilo data directory cannot be created", current
            ) from None
        try:
            info = os.lstat(current)
        except OSError:
            raise KiloAuthDestinationError(
                "Kilo data directory cannot be verified", current
            ) from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise KiloAuthDestinationError(
                "Kilo data path must contain only non-symlink directories", current
            )
        if os.name == "posix":
            try:
                os.chmod(current, 0o700)
            except OSError:
                raise KiloAuthDestinationError(
                    "Kilo data directory permissions cannot be restricted", current
                ) from None
    return current


def _write_destination(
    destination_dir: Path, dest: Path, document: bytes
) -> None:
    """Atomically materialize ``document`` at ``dest`` with restrictive mode."""
    if os.path.islink(dest):
        raise KiloAuthDestinationError(
            "destination must not be a symlink", destination_dir
        )
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(destination_dir), prefix=".kilo-auth.", suffix=".tmp"
        )
    except OSError:
        raise KiloAuthDestinationError(
            "cannot create a temporary Kilo auth file", destination_dir
        ) from None
    try:
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(document)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name == "posix":
                os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, dest)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
    except OSError:
        raise KiloAuthDestinationError(
            "cannot atomically write the destination auth file", destination_dir
        ) from None
    try:
        info = os.lstat(dest)
    except OSError:
        raise KiloAuthDestinationError(
            "cannot verify the destination auth file", destination_dir
        ) from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise KiloAuthDestinationError(
            "destination is not a regular file", destination_dir
        )
    if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
        raise KiloAuthDestinationError(
            "destination permissions are too permissive", destination_dir
        )


def project_xai_auth(source: StrPath, isolated_home: StrPath) -> KiloAuthProjection:
    """Project only the ``xai`` provider record into an isolated Kilo HOME.

    Reads the absolute ``source`` Kilo auth.json (a regular, non-symlink,
    bounded JSON object keyed by provider id), extracts exactly the ``xai``
    record, and atomically replaces Kilo's
    ``isolated_home/.local/share/kilo/auth.json`` (mode 0600 on POSIX inside
    restrictive request-local directories) without touching the source file or any
    ambient HOME.  Every failure raises a ``KiloAuthError`` subclass instead
    of, or before, writing anything; token strings never appear in errors or
    in the returned receipt.
    """
    source_path = Path(os.fspath(source))
    home_path = Path(os.fspath(isolated_home))

    raw = _read_source_bytes(source_path)
    source_sha256 = hashlib.sha256(raw).hexdigest()
    record = _extract_xai_record(source_path, raw)
    projected = json.dumps({PROVIDER_ID: record}, indent=2, sort_keys=True) + "\n"
    document = projected.encode("utf-8")

    home = _prepare_isolated_home(home_path)
    destination_dir = _prepare_kilo_data_dir(home)
    dest = home / KILO_AUTH_RELATIVE_PATH
    _write_destination(destination_dir, dest, document)

    return KiloAuthProjection(
        provider=PROVIDER_ID,
        source=str(source_path),
        destination=str(dest),
        source_bytes=len(raw),
        source_sha256=source_sha256,
        destination_bytes=len(document),
        destination_sha256=hashlib.sha256(document).hexdigest(),
        status="projected",
    )


def resolve_kilo_auth_source(
    *,
    home: StrPath,
    xdg_data_home: StrPath | None = None,
    platform_name: str = "posix",
) -> Path:
    """Return Kilo's canonical auth path from explicit path inputs only.

    The function performs no filesystem access and never consults ``HOME`` or
    the process environment.  Callers may supply an explicit XDG data root;
    otherwise Kilo's portable default below the supplied home is used on both
    POSIX and Windows hosts.
    """

    if platform_name not in {"posix", "nt"}:
        raise KiloAuthSourceError("unsupported platform name")
    home_path = Path(os.fspath(home))
    if not home_path.is_absolute() or ".." in home_path.parts:
        raise KiloAuthSourceError("home must be an absolute normalized path")
    if xdg_data_home is None:
        data_root = home_path / ".local" / "share"
    else:
        data_root = Path(os.fspath(xdg_data_home))
        if not data_root.is_absolute() or ".." in data_root.parts:
            raise KiloAuthSourceError(
                "XDG data home must be an absolute normalized path"
            )
    return data_root / "kilo" / AUTH_FILENAME
