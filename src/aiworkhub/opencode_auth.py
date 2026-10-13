"""Fail-closed OpenCode credential projection for isolated worker homes.

Only OpenCode's exact ``auth.json`` is copied.  The source is read through one
verified descriptor and the destination is materialized atomically below the
request-private HOME; provider databases, configuration, logs, caches, and
session state are never traversed.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Union
from urllib.parse import urlsplit

from .platform_io import (
    chmod_fd,
    chmod_path,
    posix_path_modes_supported,
    stat_owned_by_current_user,
)

__all__ = [
    "MAX_SOURCE_BYTES",
    "OPENCODE_API_KEY_ENV",
    "OPENCODE_AUTH_RELATIVE_PATH",
    "OpenCodeAuthDestinationError",
    "OpenCodeAuthError",
    "OpenCodeAuthProjection",
    "OpenCodeAuthSourceError",
    "opencode_go_api_key_env",
    "project_opencode_auth",
    "project_opencode_custom_provider",
]

OPENCODE_AUTH_RELATIVE_PATH = (
    Path(".local") / "share" / "opencode" / "auth.json"
)
# The env var name carrying the ``opencode-go`` provider's API key to a
# headless OpenCode child (see ``opencode_go_api_key_env``).
OPENCODE_API_KEY_ENV = "OPENCODE_API_KEY"
MAX_SOURCE_BYTES = 1 << 20
_READ_CHUNK_BYTES = 64 * 1024
_OPENCODE_BUILTIN_PROVIDERS = ("opencode", "opencode-go")
# The only owner model-entry keys a projected custom provider keeps; the worker
# config validator in ``runtime_adapters`` imports these same names.
_OPENCODE_MODEL_FLAGS = ("tool_call", "reasoning", "attachment", "temperature")
_OPENCODE_MODEL_LIMIT_KEYS = ("context", "output")
# OpenCode's ``{env:NAME}`` config substitution, and any other ``{...}`` token.
_OPENCODE_ENV_TEMPLATE = re.compile(r"\{env:([^{}]*)\}")
_OPENCODE_ANY_TEMPLATE = re.compile(r"\{[^{}]*\}")
# Owner-config read failures that leave the file unread, whatever the cause.
_OPENCODE_CONFIG_UNREADABLE_REASONS = (
    "opencode_auth_source_path_invalid",
    "opencode_auth_source_unavailable",
    "opencode_auth_source_not_regular",
    "opencode_auth_source_size_invalid",
    "opencode_auth_source_identity_changed",
)

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


def _read_verified_source(
    source: Path, *, encoding: str = "utf-8", allow_empty: bool = False
) -> bytes:
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
            payload = json.loads(raw.decode(encoding))
        except (UnicodeDecodeError, ValueError):
            raise OpenCodeAuthSourceError("opencode_auth_source_json_invalid") from None
        if not isinstance(payload, dict) or not (payload or allow_empty):
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


def opencode_go_api_key_env(isolated_home: StrPath) -> dict[str, str]:
    """The ``opencode-go`` API key from one projected auth file, as env.

    Returns ``{OPENCODE_API_KEY_ENV: key}`` when the isolated HOME's projected
    ``auth.json`` carries a usable ``opencode-go`` block -- an object whose
    ``type`` is ``"api"`` with a non-empty string ``key`` (stripped, so the
    projection's byte-identical copy still tolerates padded input) -- and
    ``{}`` for every other fact: no projected file, unreadable bytes, any
    other document shape, or any other provider's credentials.  Fail-closed
    and bounded like the projection itself: only the exact env mapping is
    returned, never the document.
    """
    home = Path(os.fspath(isolated_home))
    try:
        raw = (home / OPENCODE_AUTH_RELATIVE_PATH).read_bytes()
    except OSError:
        return {}
    if not raw or len(raw) > MAX_SOURCE_BYTES:
        return {}
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(document, dict):
        return {}
    block = document.get("opencode-go")
    if not isinstance(block, dict) or block.get("type") != "api":
        return {}
    key = block.get("key")
    if not isinstance(key, str):
        return {}
    key = key.strip()
    if not key:
        return {}
    return {OPENCODE_API_KEY_ENV: key}


def _owner_opencode_global_config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "opencode" / "opencode.json"
    return Path.home() / ".config" / "opencode" / "opencode.json"


def _read_bounded_json(path: Path) -> object | None:
    # The owner config decides which host secret is released and where it is
    # sent, so it gets auth.json's bounded, owner- and mode-checked read.  Only
    # an absent file or an empty {} is "not configured"; an unparseable one (strict
    # JSON, so a JSONC comment too) is opencode_provider_config_json_invalid; a
    # symlinked, non-regular, oversized or otherwise unreadable one is
    # opencode_provider_config_unreadable and an unsafe one keeps its own
    # opencode_provider_config_* reason.
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            raise OpenCodeAuthError("opencode_provider_config_unreadable")
        raw = _read_verified_source(path, encoding="utf-8-sig", allow_empty=True)
        return json.loads(raw.decode("utf-8-sig"))
    except FileNotFoundError:
        return None
    except OpenCodeAuthSourceError as exc:
        if exc.reason in _OPENCODE_CONFIG_UNREADABLE_REASONS:
            raise OpenCodeAuthError("opencode_provider_config_unreadable") from None
        raise OpenCodeAuthError(
            exc.reason.replace("opencode_auth_source_", "opencode_provider_config_", 1)
        ) from None
    except OSError:
        raise OpenCodeAuthError("opencode_provider_config_unreadable") from None
    except RecursionError:
        raise OpenCodeAuthError("opencode_provider_config_json_invalid") from None


def _resolve_opencode_env_templates(
    text: object, host_env: Mapping[str, str], *, credential: bool = False
) -> str:
    """One owner-config string with every ``{env:NAME}`` resolved from ``host_env``.

    A whole or partial (``sk-{env:NAME}``) reference resolves; an unset or empty
    ``NAME`` is opencode_provider_env_unresolved.  A non-string value or any
    ``{file:`` reference is opencode_provider_credential_unsupported, and in a
    credential (apiKey) any other {...} token is
    opencode_provider_credential_unsupported.  Other literal braces (a
    JSON-valued header, a display name) pass through verbatim.
    """
    if not isinstance(text, str):
        raise OpenCodeAuthError("opencode_provider_credential_unsupported")
    if "{file:" in text or (
        credential and _OPENCODE_ANY_TEMPLATE.search(_OPENCODE_ENV_TEMPLATE.sub("", text))
    ):
        raise OpenCodeAuthError("opencode_provider_credential_unsupported")

    def resolve(match: re.Match[str]) -> str:
        value = host_env.get(match.group(1))
        if not value:
            raise OpenCodeAuthError("opencode_provider_env_unresolved")
        return value

    return _OPENCODE_ENV_TEMPLATE.sub(resolve, text)


def _project_opencode_model_entry(
    entry: Mapping[str, object], env: Mapping[str, str]
) -> dict[str, object]:
    """Only the owner model entry's ``id`` (``{env:NAME}`` resolved), ``limit``
    and boolean capability flags."""
    projected: dict[str, object] = {}
    if isinstance(entry.get("id"), str):
        projected["id"] = _resolve_opencode_env_templates(entry["id"], env)
    limit = entry.get("limit")
    if isinstance(limit, dict):
        bounded = {key: limit.get(key) for key in _OPENCODE_MODEL_LIMIT_KEYS}
        # Projected only whole: both limits, each a positive integer.
        if all(type(value) is int and value > 0 for value in bounded.values()):
            projected["limit"] = bounded
    for flag in _OPENCODE_MODEL_FLAGS:
        if isinstance(entry.get(flag), bool):
            projected[flag] = entry[flag]
    return projected


def _opencode_provider_base_url_is_loopback(base_url: str) -> bool:
    """Whether a resolved ``options.baseURL`` host is loopback-only or unspecified.

    ``localhost`` and ``*.localhost`` (case and a trailing dot ignored), every
    IPv4 spelling ``inet_aton`` accepts (``127.1``, ``2130706433``,
    ``0x7f000001``), IPv4-mapped IPv6 (``::ffff:127.0.0.1``) and the unspecified
    ``0.0.0.0``/``::`` all count.
    """
    try:
        hostname = urlsplit(base_url).hostname
    except ValueError:
        return False
    if not hostname:
        return False
    hostname = hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            address = ipaddress.IPv4Address(socket.inet_aton(hostname))
        except (OSError, ValueError):
            return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


def project_opencode_custom_provider(
    model: str,
    *,
    host_env: dict[str, str] | None = None,
    appcontainer: bool = False,
) -> dict[str, object] | None:
    """Project the owner's exact custom-provider block for one model, or None.

    Returns ``None`` for a built-in provider (``opencode``/``opencode-go``) or
    a model with no provider prefix: nothing to project, not a refusal.  Every
    other fact -- a missing owner config, provider block or model, an
    unreadable, unparseable or unsafe owner config file (symlinked, not
    regular, foreign-owned, group/other-writable or over ``MAX_SOURCE_BYTES``),
    an unsupported credential substitution, an unset ``{env:NAME}``, or (with
    ``appcontainer=True``) a resolved loopback-only ``baseURL``
    an AppContainer child cannot reach -- raises :class:`OpenCodeAuthError`
    with a named reason before any claim.  The returned mapping holds exactly
    one provider with exactly one model and only the bounded keys ``npm``,
    ``name``, ``options`` (``baseURL``, ``apiKey``, ``headers``), ``models``;
    every ``{env:NAME}`` there is resolved from ``host_env`` (default
    ``os.environ``) and the model entry keeps only ``id``, ``limit`` and the
    boolean capability flags.
    """
    if "/" not in model:
        return None
    provider, _, name = model.partition("/")
    if not provider or not name or provider in _OPENCODE_BUILTIN_PROVIDERS:
        return None
    # os.environ itself, not a dict copy: on Windows only it keeps the
    # case-insensitive lookup OpenCode's process.env uses for {env:VAR}.
    env = host_env if host_env is not None else os.environ
    config_path = _owner_opencode_global_config_path()
    document = _read_bounded_json(config_path)
    if not isinstance(document, dict):
        raise OpenCodeAuthError("opencode_provider_not_configured")
    providers = document.get("provider")
    block = providers.get(provider) if isinstance(providers, dict) else None
    if not isinstance(block, dict):
        raise OpenCodeAuthError("opencode_provider_not_configured")
    models = block.get("models")
    model_entry = models.get(name) if isinstance(models, dict) else None
    if not isinstance(model_entry, dict):
        raise OpenCodeAuthError("opencode_provider_not_configured")
    options = block.get("options") if isinstance(block.get("options"), dict) else {}

    projected_options: dict[str, object] = {}
    for key_name in ("baseURL", "apiKey"):
        if key_name in options:
            projected_options[key_name] = _resolve_opencode_env_templates(
                options[key_name], env, credential=key_name == "apiKey"
            )
    if "headers" in options:
        headers = options["headers"]
        if not isinstance(headers, dict):
            raise OpenCodeAuthError("opencode_provider_credential_unsupported")
        projected_options["headers"] = {
            header: _resolve_opencode_env_templates(value, env)
            for header, value in headers.items()
        }

    if appcontainer:
        base_url = projected_options.get("baseURL")
        if isinstance(base_url, str) and _opencode_provider_base_url_is_loopback(base_url):
            raise OpenCodeAuthError("opencode_provider_loopback_unreachable_in_appcontainer")

    projected_block: dict[str, object] = {}
    for key_name in ("npm", "name"):
        if key_name in block:
            projected_block[key_name] = _resolve_opencode_env_templates(block[key_name], env)
    if projected_options:
        projected_block["options"] = projected_options
    projected_block["models"] = {name: _project_opencode_model_entry(model_entry, env)}
    return {provider: projected_block}
