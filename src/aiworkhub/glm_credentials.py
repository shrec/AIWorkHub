"""Coordinator-only GLM/Copilot BYOK credential handling.

The local ``glm_copilot_cli`` adapter drives the installed official GitHub
Copilot CLI in BYOK mode against GLM's OpenAI-compatible API.  The API key is
loaded only by the coordinator at launch time and enters only the child process
environment as ``COPILOT_PROVIDER_API_KEY``; it is never placed in argv,
metadata, logs, dashboards, cards, exception text, or worker shell/MCP envs.
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
try:
    from .platform_io import chmod_fd
except ImportError:  # pragma: no cover - standalone compatibility
    def chmod_fd(fd: int, mode: int) -> None:
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            fchmod(fd, mode)


ADAPTER_ID = "glm_copilot_cli"

PROVIDER_API_KEY_ENV = "COPILOT_PROVIDER_API_KEY"
PROVIDER_BASE_URL_ENV = "COPILOT_PROVIDER_BASE_URL"
PROVIDER_TYPE_ENV = "COPILOT_PROVIDER_TYPE"
PROVIDER_MODEL_ENV = "COPILOT_MODEL"

GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
GLM_PROVIDER_TYPE = "openai"
GLM_ALLOWED_ENDPOINT_HOSTS = frozenset({"open.bigmodel.cn"})

CREDENTIAL_PATH_ENV = "AIWORKHUB_GLM_CREDENTIAL"
_DEFAULT_CREDENTIAL_REL = Path(".config") / "aiworkhub" / "glm_copilot_credential.json"
_ALLOWED_CREDENTIAL_FIELDS = frozenset({"provider", "provider_type", "base_url", "api_key"})
MAX_CREDENTIAL_FILE_BYTES = 64 * 1024


class CredentialError(RuntimeError):
    """Fail-closed credential validation error with a secret-free reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class GLMCredential:
    """A validated GLM BYOK credential."""

    base_url: str
    provider_type: str
    supported_models: tuple[str, ...]
    default_model: str
    api_key: str = field(repr=False)

    def redacted(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "provider_type": self.provider_type,
            "supported_models": list(self.supported_models),
            "default_model": self.default_model,
            "api_key_present": bool(self.api_key),
        }

    def provider_env(self, model: str) -> dict[str, str]:
        return {
            PROVIDER_TYPE_ENV: self.provider_type,
            PROVIDER_BASE_URL_ENV: self.base_url,
            PROVIDER_MODEL_ENV: model,
            PROVIDER_API_KEY_ENV: self.api_key,
        }


def credential_path(path: os.PathLike[str] | str | None = None) -> Path:
    """Resolve the GLM credential path.

    Priority: explicit ``path`` argument > ``AIWORKHUB_GLM_CREDENTIAL`` >
    ``~/.config/aiworkhub/glm_copilot_credential.json``.
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


def _validate_endpoint(base_url: Any) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise CredentialError("credential_missing_base_url")
    value = base_url.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "https":
        raise CredentialError("credential_non_https_endpoint")
    host = (parsed.hostname or "").lower()
    if host not in GLM_ALLOWED_ENDPOINT_HOSTS:
        raise CredentialError(f"credential_non_glm_endpoint:{host or 'none'}")
    if parsed.path not in {"", "/api/paas/v4"}:
        raise CredentialError("credential_unsupported_base_url_path")
    return value


def load_credential(
    *,
    path: os.PathLike[str] | str | None = None,
    repo: os.PathLike[str] | str | None = None,
) -> GLMCredential:
    """Load and fully validate the GLM BYOK credential."""
    resolved = credential_path(path)
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
    unknown = sorted(str(key) for key in payload if key not in _ALLOWED_CREDENTIAL_FIELDS)
    if unknown:
        raise CredentialError("credential_unsupported_fields:" + "|".join(unknown[:8]))

    api_key = payload.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise CredentialError("credential_empty_api_key")

    provider = str(payload.get("provider") or "glm").strip().lower()
    if provider != "glm":
        raise CredentialError(f"credential_unsupported_provider:{provider or 'none'}")

    base_url = _validate_endpoint(payload.get("base_url") or GLM_BASE_URL)
    provider_type = str(payload.get("provider_type") or GLM_PROVIDER_TYPE).strip().lower()
    if provider_type != GLM_PROVIDER_TYPE:
        raise CredentialError(f"credential_unsupported_provider_type:{provider_type}")

    return GLMCredential(
        base_url=base_url,
        provider_type=provider_type,
        supported_models=runtime_adapters.GLM_SUPPORTED_MODELS,
        default_model=runtime_adapters.GLM_DEFAULT_MODEL,
        api_key=api_key.strip(),
    )


def bootstrap_credential(
    *,
    path: os.PathLike[str] | str | None = None,
    base_url: str = GLM_BASE_URL,
    api_key: str | None = None,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    repo: os.PathLike[str] | str | None = None,
) -> Path:
    """Write a mode-0600 GLM credential file outside the repository."""
    endpoint = _validate_endpoint(base_url)
    if api_key is None:
        api_key = getpass_fn("GLM API key (input hidden): ")
    if not isinstance(api_key, str) or not api_key.strip():
        raise CredentialError("credential_empty_api_key")

    target = credential_path(path)
    if repo is not None:
        repo_root = Path(repo).expanduser().resolve()
        if _within(repo_root, (target.parent.resolve() / target.name)):
            raise CredentialError("credential_inside_repository")

    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    document = {
        "provider": "glm",
        "provider_type": GLM_PROVIDER_TYPE,
        "base_url": endpoint,
        "api_key": api_key.strip(),
    }
    data = (json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")

    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(tmp, flags, 0o600)
    try:
        chmod_fd(fd, 0o600)
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


def credential_status(
    *,
    path: os.PathLike[str] | str | None = None,
    repo: os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    """Secret-free readiness view for the ``glm_copilot_cli`` adapter."""
    resolution = runtime_adapters.resolve_executable(ADAPTER_ID)
    installed = bool(resolution.ok)

    credential_present = False
    blocker_reason = ""
    try:
        load_credential(path=path, repo=repo)
        credential_present = True
    except CredentialError as exc:
        blocker_reason = exc.reason
    except Exception:  # noqa: BLE001
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
        "kind": "local_copilot_byok_glm",
        "installed": installed,
        "credential_present": credential_present,
        "endpoint": GLM_BASE_URL,
        "provider_type": GLM_PROVIDER_TYPE,
        "supported_models": list(runtime_adapters.GLM_SUPPORTED_MODELS),
        "default_model": runtime_adapters.GLM_DEFAULT_MODEL,
        "credential_path_configured": bool(
            os.environ.get(CREDENTIAL_PATH_ENV, "").strip() or path is not None
        ),
        "credential_path_default": "~/.config/aiworkhub/glm_copilot_credential.json",
        "setup_command": "python3 -m aiworkhub.glm_credentials setup",
        "status_command": "python3 -m aiworkhub.glm_credentials status",
        "launchable": launchable,
        "blocker_reason": "" if launchable else (blocker_reason or "not_launchable"),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="aiworkhub-task-glm-credential",
        description="One-time GLM/Copilot BYOK credential bootstrap and status.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    setup_parser = sub.add_parser("setup", help="prompt for and store the GLM API key (0600, outside repo)")
    setup_parser.add_argument("--path", default=None, help="override credential file path")
    setup_parser.add_argument("--base-url", default=GLM_BASE_URL, help="GLM OpenAI-compatible endpoint")
    status_parser = sub.add_parser("status", help="print secret-free readiness for the GLM adapter")
    status_parser.add_argument("--path", default=None, help="override credential file path")

    args = parser.parse_args(argv)
    if args.command == "setup":
        try:
            written = bootstrap_credential(path=args.path, base_url=args.base_url)
        except CredentialError as exc:
            print(f"credential bootstrap failed: {exc.reason}", file=sys.stderr)
            return 2
        print(f"stored GLM credential (0600) at: {written}")
        print("The API key is never printed; it enters only the launched child process.")
        return 0
    status = credential_status(path=args.path)
    print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if status["launchable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
