"""Coordinator-only DeepSeek/Copilot BYOK credential handling.

The local ``deepseek_copilot_cli`` adapter drives the installed official GitHub
Copilot CLI in "bring your own key" (BYOK) mode against DeepSeek's
OpenAI-compatible API. Copilot activates BYOK when ``COPILOT_PROVIDER_BASE_URL``
is set and reads the key from ``COPILOT_PROVIDER_API_KEY`` (see
``copilot help providers``).

Security contract (enforced by tests):
  * The API key lives in exactly ONE place at rest: a mode-0600 JSON file
    OUTSIDE the repository, owned by the current user, never a symlink, never
    group/world accessible, and never inside the Git tree.
  * The key is loaded only on the coordinator/host side, only at launch time,
    and enters ONLY the launched child's environment as
    ``COPILOT_PROVIDER_API_KEY``. It never appears in argv, task cards, logs,
    audit events, dashboard payloads, test fixtures, Git, or the worker
    shell/MCP environment.
  * Read-only readiness views expose booleans and category blocker reasons
    only -- never the key contents or any hash of them.

This module never launches a process and never writes to the task queue.
"""

from __future__ import annotations

import getpass
import json
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import runtime_adapters


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADAPTER_ID = "deepseek_copilot_cli"

# Copilot BYOK environment variable names (from ``copilot help providers``).
PROVIDER_API_KEY_ENV = "COPILOT_PROVIDER_API_KEY"
PROVIDER_BASE_URL_ENV = "COPILOT_PROVIDER_BASE_URL"
PROVIDER_TYPE_ENV = "COPILOT_PROVIDER_TYPE"
PROVIDER_MODEL_ENV = "COPILOT_MODEL"

# The only endpoint a DeepSeek-labeled credential may target. A non-DeepSeek
# host is rejected so a DeepSeek task can never be silently pointed at another
# provider.
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_PROVIDER_TYPE = "openai"
DEEPSEEK_ALLOWED_ENDPOINT_HOSTS = frozenset({"api.deepseek.com"})

CREDENTIAL_PATH_ENV = "AIWORKHUB_DEEPSEEK_CREDENTIAL"
_DEFAULT_CREDENTIAL_REL = Path(".config") / "aiworkhub" / "deepseek_copilot_credential.json"

MAX_CREDENTIAL_FILE_BYTES = 64 * 1024


class CredentialError(RuntimeError):
    """A fail-closed credential validation error carrying a safe blocker reason.

    ``reason`` is always a non-secret category token (optionally an endpoint
    host); it never contains the API key.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class DeepSeekCredential:
    """A validated DeepSeek BYOK credential.

    ``api_key`` is ``repr=False`` so it can never leak through a dataclass
    repr, a traceback frame, or a naive ``str()`` of the object.
    """

    base_url: str
    provider_type: str
    supported_models: tuple[str, ...]
    default_model: str
    api_key: str = field(repr=False)

    def redacted(self) -> dict[str, Any]:
        """A safe, secret-free view for status/dashboard payloads."""
        return {
            "base_url": self.base_url,
            "provider_type": self.provider_type,
            "supported_models": list(self.supported_models),
            "default_model": self.default_model,
            "api_key_present": bool(self.api_key),
        }

    def provider_env(self, model: str) -> dict[str, str]:
        """Minimum BYOK provider environment for one launch.

        Only ``COPILOT_PROVIDER_API_KEY`` is secret; the other three keys are
        the non-secret provider type, endpoint, and selected model. The
        launcher declares the redaction of the key via the CLI's
        ``--secret-env-vars`` flag (see ``runtime_adapters``).
        """
        return {
            PROVIDER_TYPE_ENV: self.provider_type,
            PROVIDER_BASE_URL_ENV: self.base_url,
            PROVIDER_MODEL_ENV: model,
            PROVIDER_API_KEY_ENV: self.api_key,
        }


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def credential_path(path: os.PathLike[str] | str | None = None) -> Path:
    """Resolve the credential file path.

    Priority: explicit ``path`` argument > ``AIWORKHUB_DEEPSEEK_CREDENTIAL``
    env override > ``~/.config/aiworkhub/deepseek_copilot_credential.json``.
    """
    if path is not None:
        return Path(path).expanduser()
    override = os.environ.get(CREDENTIAL_PATH_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / _DEFAULT_CREDENTIAL_REL


def _within(repo: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(repo)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Validation + loading (coordinator-only)
# ---------------------------------------------------------------------------

def _validate_endpoint(base_url: Any) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise CredentialError("credential_missing_base_url")
    parsed = urlsplit(base_url.strip())
    if parsed.scheme != "https":
        raise CredentialError("credential_non_https_endpoint")
    host = (parsed.hostname or "").lower()
    if host not in DEEPSEEK_ALLOWED_ENDPOINT_HOSTS:
        raise CredentialError(f"credential_non_deepseek_endpoint:{host or 'none'}")
    return base_url.strip()


def load_credential(
    *,
    path: os.PathLike[str] | str | None = None,
    repo: os.PathLike[str] | str | None = None,
) -> DeepSeekCredential:
    """Load and fully validate the DeepSeek BYOK credential.

    Raises :class:`CredentialError` with a non-secret ``reason`` on any
    problem: absent file, symlink, wrong owner, group/world-accessible mode,
    a path inside the repository, oversize/invalid JSON, empty API key, or a
    non-DeepSeek endpoint. Never returns a partially-validated credential and
    never logs the key.
    """
    resolved = credential_path(path)

    # lstat (never stat) so a symlinked credential is rejected outright rather
    # than followed to an attacker-chosen target.
    try:
        lst = os.lstat(resolved)
    except FileNotFoundError:
        raise CredentialError("credential_file_absent") from None
    except OSError:
        raise CredentialError("credential_file_unreadable") from None

    if stat.S_ISLNK(lst.st_mode):
        raise CredentialError("credential_symlink_rejected")
    if not stat.S_ISREG(lst.st_mode):
        raise CredentialError("credential_not_regular_file")
    if lst.st_uid != os.getuid():
        raise CredentialError("credential_wrong_owner")
    if stat.S_IMODE(lst.st_mode) & 0o077:
        raise CredentialError("credential_group_or_world_accessible")
    if lst.st_size == 0:
        raise CredentialError("credential_empty_file")
    if lst.st_size > MAX_CREDENTIAL_FILE_BYTES:
        raise CredentialError("credential_file_too_large")

    if repo is not None:
        repo_root = Path(repo).expanduser().resolve()
        if _within(repo_root, resolved.resolve()):
            raise CredentialError("credential_inside_repository")

    # O_NOFOLLOW closes the check->open symlink-swap race atomically.
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(resolved, flags)
    except OSError:
        raise CredentialError("credential_file_unreadable") from None
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if mode & 0o077:
            raise CredentialError("credential_group_or_world_accessible")
        with os.fdopen(fd, "r", closefd=False, encoding="utf-8") as fh:
            raw = fh.read(MAX_CREDENTIAL_FILE_BYTES + 1)
    finally:
        os.close(fd)

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise CredentialError("credential_invalid_json") from None
    if not isinstance(payload, dict):
        raise CredentialError("credential_invalid_object")

    api_key = payload.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise CredentialError("credential_empty_api_key")

    base_url = _validate_endpoint(payload.get("base_url") or DEEPSEEK_BASE_URL)
    provider_type = str(payload.get("provider_type") or DEEPSEEK_PROVIDER_TYPE).strip().lower()
    if provider_type != DEEPSEEK_PROVIDER_TYPE:
        raise CredentialError(f"credential_unsupported_provider_type:{provider_type}")

    return DeepSeekCredential(
        base_url=base_url,
        provider_type=provider_type,
        supported_models=runtime_adapters.DEEPSEEK_SUPPORTED_MODELS,
        default_model=runtime_adapters.DEEPSEEK_DEFAULT_MODEL,
        api_key=api_key.strip(),
    )


# ---------------------------------------------------------------------------
# One-time secure bootstrap
# ---------------------------------------------------------------------------

def bootstrap_credential(
    *,
    path: os.PathLike[str] | str | None = None,
    base_url: str = DEEPSEEK_BASE_URL,
    api_key: str | None = None,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    repo: os.PathLike[str] | str | None = None,
) -> Path:
    """Write a mode-0600 DeepSeek credential file outside the repository.

    The API key is read interactively via ``getpass`` (never echoed, never
    passed on a command line) unless ``api_key`` is supplied directly (used by
    tests). The file is created with ``O_CREAT | O_EXCL`` at 0600 in a 0700
    parent directory. Refuses to write inside the repository. Returns the path
    written; never prints or returns the key.
    """
    endpoint = _validate_endpoint(base_url)
    if api_key is None:
        api_key = getpass_fn("DeepSeek API key (input hidden): ")
    if not isinstance(api_key, str) or not api_key.strip():
        raise CredentialError("credential_empty_api_key")

    target = credential_path(path)
    if repo is not None:
        repo_root = Path(repo).expanduser().resolve()
        # Resolve the parent (the file itself does not exist yet).
        if _within(repo_root, (target.parent.resolve() / target.name)):
            raise CredentialError("credential_inside_repository")

    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)

    document = {
        "provider": "deepseek",
        "provider_type": DEEPSEEK_PROVIDER_TYPE,
        "base_url": endpoint,
        "api_key": api_key.strip(),
    }
    data = (json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")

    # Replace any prior file atomically; O_EXCL on a temp then rename keeps the
    # window at 0600 the whole time and never leaves a world-readable file.
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(tmp, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        os.chmod(target, 0o600)
    finally:
        os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return target


# ---------------------------------------------------------------------------
# Read-only readiness (never exposes the key)
# ---------------------------------------------------------------------------

def credential_status(
    *,
    path: os.PathLike[str] | str | None = None,
    repo: os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    """Secret-free readiness view for the ``deepseek_copilot_cli`` adapter.

    Reports ``installed`` (Copilot CLI discoverable), ``credential_present``
    (a valid credential loads), ``endpoint``, ``supported_models``,
    ``launchable``, and an exact non-secret ``blocker_reason``. Never returns
    the API key or any hash of it.
    """
    resolution = runtime_adapters.resolve_executable(ADAPTER_ID)
    installed = bool(resolution.ok)

    credential_present = False
    blocker_reason = ""
    try:
        load_credential(path=path, repo=repo)
        credential_present = True
    except CredentialError as exc:
        blocker_reason = exc.reason
    except Exception:  # noqa: BLE001 - degrade to a bounded, non-secret status
        blocker_reason = "credential_load_error"

    if not installed:
        install_blocker = resolution.reason or "copilot_cli_not_found"
        blocker_reason = (
            install_blocker
            if not blocker_reason
            else f"{install_blocker};{blocker_reason}"
        )

    launchable = installed and credential_present
    return {
        "adapter_id": ADAPTER_ID,
        "kind": "local_copilot_byok_deepseek",
        "installed": installed,
        "credential_present": credential_present,
        "endpoint": DEEPSEEK_BASE_URL,
        "provider_type": DEEPSEEK_PROVIDER_TYPE,
        "supported_models": list(runtime_adapters.DEEPSEEK_SUPPORTED_MODELS),
        "default_model": runtime_adapters.DEEPSEEK_DEFAULT_MODEL,
        "credential_path_configured": bool(
            os.environ.get(CREDENTIAL_PATH_ENV, "").strip() or path is not None
        ),
        "launchable": launchable,
        "blocker_reason": "" if launchable else (blocker_reason or "not_launchable"),
    }


def adapter_readiness(
    *,
    repo: os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    """Read-only readiness for every supported adapter.

    Local CLI adapters report ``installed``/``launchable`` from executable
    discovery; ``deepseek_copilot_cli`` additionally reports
    ``credential_present``; ``deepseek_manual`` is a non-launchable explicit
    fallback. No secret value is ever included.
    """
    adapters: list[dict[str, Any]] = []
    for adapter_id in runtime_adapters.SUPPORTED_ADAPTERS:
        if adapter_id == ADAPTER_ID:
            adapters.append(credential_status(repo=repo))
            continue
        if adapter_id in runtime_adapters.MANUAL_ONLY_ADAPTERS:
            adapters.append({
                "adapter_id": adapter_id,
                "kind": "manual_fallback",
                "installed": False,
                "credential_present": False,
                "endpoint": None,
                "supported_models": [],
                "launchable": False,
                "blocker_reason": "manual_only_explicit_fallback",
            })
            continue
        resolution = runtime_adapters.resolve_executable(adapter_id)
        adapters.append({
            "adapter_id": adapter_id,
            "kind": "local_cli",
            "installed": bool(resolution.ok),
            "credential_present": bool(resolution.ok),
            "endpoint": None,
            "supported_models": [],
            "launchable": bool(resolution.ok),
            "blocker_reason": "" if resolution.ok else (resolution.reason or "not_installed"),
        })
    return {
        "ok": True,
        "readonly": True,
        "adapters": adapters,
        "authority_flags": {
            "process_launch": False,
            "agent_launch": False,
            "queue_write": False,
            "audit_write": False,
            "secret_export": False,
        },
    }


# ---------------------------------------------------------------------------
# One-time bootstrap / status CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="aiworkhub-deepseek-credential",
        description="One-time DeepSeek/Copilot BYOK credential bootstrap and status.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    set_parser = sub.add_parser("set", help="prompt for and store the DeepSeek API key (0600, outside repo)")
    set_parser.add_argument("--path", default=None, help="override credential file path")
    set_parser.add_argument("--base-url", default=DEEPSEEK_BASE_URL, help="DeepSeek OpenAI-compatible endpoint")
    status_parser = sub.add_parser("status", help="print secret-free readiness for the deepseek adapter")
    status_parser.add_argument("--path", default=None, help="override credential file path")

    args = parser.parse_args(argv)
    if args.command == "set":
        try:
            written = bootstrap_credential(path=args.path, base_url=args.base_url)
        except CredentialError as exc:
            print(f"credential bootstrap failed: {exc.reason}", file=sys.stderr)
            return 2
        print(f"stored DeepSeek credential (0600) at: {written}")
        print("The API key is never printed; it enters only the launched child process.")
        return 0
    status = credential_status(path=args.path)
    print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if status["launchable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
