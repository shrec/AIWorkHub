"""Coordinator-owned authority for ordinary validation toolchains.

The authority intentionally delegates parsing and executable normalization to
``worker_workspace``.  It records what that trusted boundary resolved; it does
not introduce a second PATH resolver or a task-card capability vocabulary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import re
import shlex
import stat
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from . import terminal_authority

SCHEMA_ID = "aiworkhub.toolchain_authority.v1"
RECEIPT_SCHEMA_ID = "aiworkhub.toolchain_authority.receipt.v1"
RECEIPT_CARD_KEY = "toolchain_authority_receipt"
REGISTRY_SCHEMA_ID = "aiworkhub.toolchain_registry.v1"
_FINGERPRINT_LIMIT = 1024 * 1024
_REGISTRY_RELATIVE = "aiworkhub.toolchain.json"
_SUPPORTED_SANDBOX_CAPABILITIES = frozenset({"validation_subprocess"})
_REPOSITORY_METADATA = (
    _REGISTRY_RELATIVE,
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
)
_VERSION_RE = re.compile(r"\d+(?:\.\d+)*")

_SECRET_ENV_VARS = (
    "AIWORKHUB_TOOLCHAIN_AUTHORITY_HMAC_KEY",
    "AIWORKHUB_TOOLCHAIN_AUTHORITY_SECRET",
)
_SECRET_RELATIVE = ".aiworkhub/toolchain-authority/receipt-hmac.key"
_RECEIPT_MAC_KEY = "receipt_mac"


class ProvisioningDomain(str, Enum):
    """Typed boundary for capabilities deliberately outside ordinary tools."""

    REPOSITORY_OVERLAY = "repository_overlay"
    KERNEL_BACKEND = "kernel_backend"


class ProvisioningExtension(Protocol):
    """Future coordinator provisioners must remain explicit and separately typed."""

    domain: ProvisioningDomain

    def provision(self, repo: Path, request: Mapping[str, object]) -> object: ...


@dataclass(frozen=True, slots=True)
class MissingRequirement:
    kind: str
    value: str
    command: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value, "command": self.command}


@dataclass(frozen=True, slots=True)
class ExecutableFact:
    requested: str
    canonical_path: str
    device: int
    inode: int
    size: int
    mode: int
    mtime_ns: int
    fingerprint: str
    version_fact: str

    def as_dict(self) -> dict[str, object]:
        return {
            "requested": self.requested,
            "canonical_path": self.canonical_path,
            "device": self.device,
            "inode": self.inode,
            "size": self.size,
            "mode": self.mode,
            "mtime_ns": self.mtime_ns,
            "fingerprint": self.fingerprint,
            "version_fact": self.version_fact,
        }


@dataclass(frozen=True, slots=True)
class ProjectToolRequirement:
    name: str
    commands: tuple[str, ...]
    minimum_version: str = ""


@dataclass(frozen=True, slots=True)
class ProjectToolchainRegistry:
    fingerprint: str
    requirements: tuple[ProjectToolRequirement, ...]
    sandbox_capabilities: tuple[str, ...]
    missing: tuple[MissingRequirement, ...] = ()


@dataclass(frozen=True, slots=True)
class AuthoritySnapshot:
    schema_id: str
    repository: str
    path: str
    executables: tuple[ExecutableFact, ...]
    modules: tuple[str, ...]
    repository_fingerprint: str
    missing: tuple[MissingRequirement, ...]
    digest: str
    registry_fingerprint: str = ""
    cache_identity: str = ""

    @property
    def available(self) -> bool:
        return not self.missing

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "repository": self.repository,
            "path": self.path,
            "executables": [fact.as_dict() for fact in self.executables],
            "modules": list(self.modules),
            "repository_fingerprint": self.repository_fingerprint,
            "missing": [fact.as_dict() for fact in self.missing],
            "digest": self.digest,
            "registry_fingerprint": self.registry_fingerprint,
            "cache_identity": self.cache_identity,
        }


def _decode_secret(value: str) -> bytes:
    raw = value.strip()
    if not raw:
        return b""
    if raw.startswith("hex:"):
        try:
            return bytes.fromhex(raw[4:])
        except ValueError:
            return b""
    if re.fullmatch(r"[0-9a-fA-F]{64,}", raw) and len(raw) % 2 == 0:
        try:
            return bytes.fromhex(raw)
        except ValueError:
            return b""
    return raw.encode("utf-8", errors="surrogateescape")


def _authority_secret(repo: Path, *, create: bool) -> bytes:
    for name in _SECRET_ENV_VARS:
        secret = _decode_secret(os.environ.get(name, ""))
        if len(secret) >= 16:
            return secret
    key_path = repo / _SECRET_RELATIVE
    if not create and not key_path.exists():
        return b""
    try:
        return terminal_authority.load_or_create_key(key_path)
    except (OSError, RuntimeError):
        return b""


def _receipt_card_identity(card: Mapping[str, Any]) -> str:
    payload = {
        "task_id": str(card.get("task_id") or ""),
        "request_id": str(
            card.get("request_id")
            or card.get("claimed_request_id")
            or card.get("accepted_request_id")
            or ""
        ),
        "validation": [
            command.strip()
            for command in card.get("validation") or ()
            if isinstance(command, str) and command.strip()
        ],
        "allowed_writes": [str(item) for item in card.get("allowed_writes") or ()],
        "required_outputs": [str(item) for item in card.get("required_outputs") or ()],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _receipt_mac_payload(receipt: Mapping[str, object], card_identity: str) -> bytes:
    payload = {
        "schema_id": RECEIPT_SCHEMA_ID,
        "card_identity": card_identity,
        "receipt": {
            str(key): value
            for key, value in receipt.items()
            if key != _RECEIPT_MAC_KEY
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _receipt_mac(secret: bytes, receipt: Mapping[str, object], card_identity: str) -> str:
    return hmac.new(secret, _receipt_mac_payload(receipt, card_identity), hashlib.sha256).hexdigest()


def _hash_file(path: Path, *, limit: int = _FINGERPRINT_LIMIT) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        remaining = limit
        while remaining:
            block = stream.read(min(65536, remaining))
            if not block:
                break
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def _metadata_fingerprint(repo: Path) -> str:
    rows: list[tuple[str, str, int]] = []
    for relative in _REPOSITORY_METADATA:
        path = repo / relative
        if path.is_file() and not path.is_symlink():
            rows.append((relative, _hash_file(path), path.stat().st_size))
    encoded = json.dumps(rows, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _path_fingerprint() -> str:
    return "sha256:" + hashlib.sha256(
        os.environ.get("PATH", "").encode("utf-8", errors="surrogateescape")
    ).hexdigest()


def _platform_fingerprint() -> str:
    payload = {
        "machine": platform.machine(),
        "platform": sys_platform(),
        "python": platform.python_implementation(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def sys_platform() -> str:
    return platform.system().lower() or os.name


def _logical_name(value: object) -> str:
    text = str(value or "").strip()
    if not text or text.startswith(("/", "~")) or "\\" in text:
        return ""
    if ".." in Path(text).parts:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", text):
        return ""
    return text


def _version_tuple(value: str) -> tuple[int, ...] | None:
    match = _VERSION_RE.search(value)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def _version_meets(actual: str, minimum: str) -> bool:
    if not minimum:
        return True
    found = _version_tuple(actual)
    needed = _version_tuple(minimum)
    if found is None or needed is None:
        return False
    width = max(len(found), len(needed))
    return found + (0,) * (width - len(found)) >= needed + (0,) * (width - len(needed))


def _registry_fingerprint(registry_path: Path) -> str:
    if not registry_path.exists():
        return ""
    if not registry_path.is_file() or registry_path.is_symlink():
        return "malformed"
    return _hash_file(registry_path)


def _candidate_command(candidate: Mapping[str, object], name: str) -> str:
    command = candidate.get("command")
    if isinstance(command, str) and command.strip():
        try:
            parts = shlex.split(command)
        except ValueError:
            return ""
        if not parts or not _logical_name(parts[0]):
            return ""
        if any(part in {";", "&&", "||", "|", "`"} for part in parts):
            return ""
        if any(part.startswith(("/", "~")) for part in parts[1:]):
            return ""
        return shlex.join(parts)
    executable = _logical_name(candidate.get("executable") or name)
    if not executable:
        return ""
    args = candidate.get("args")
    if args is None:
        return executable
    if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        return ""
    if any(item in {";", "&&", "||", "|", "`"} for item in args):
        return ""
    if any(item.startswith(("/", "~")) for item in args):
        return ""
    return shlex.join([executable, *args])


def _platform_selected(selector: object) -> bool:
    if selector is None:
        return True
    if isinstance(selector, str):
        values = [selector]
    elif isinstance(selector, list) and all(isinstance(item, str) for item in selector):
        values = selector
    else:
        return False
    normalized = {item.strip().lower() for item in values if item.strip()}
    return bool(normalized) and sys_platform() in normalized


def _load_project_registry(repo: Path) -> ProjectToolchainRegistry:
    registry_path = repo / _REGISTRY_RELATIVE
    fingerprint = _registry_fingerprint(registry_path)
    if not fingerprint:
        return ProjectToolchainRegistry("", (), ())
    if fingerprint == "malformed":
        return ProjectToolchainRegistry(
            fingerprint,
            (),
            (),
            (MissingRequirement("registry", "not_regular", _REGISTRY_RELATIVE),),
        )
    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ProjectToolchainRegistry(
            fingerprint,
            (),
            (),
            (MissingRequirement("registry", "malformed_json", _REGISTRY_RELATIVE),),
        )
    missing: list[MissingRequirement] = []
    if not isinstance(raw, dict) or raw.get("schema_id") != REGISTRY_SCHEMA_ID:
        return ProjectToolchainRegistry(
            fingerprint,
            (),
            (),
            (MissingRequirement("registry", "schema_id", _REGISTRY_RELATIVE),),
        )
    version = raw.get("version")
    tools = raw.get("tools")
    if version != 1 or not isinstance(tools, dict):
        return ProjectToolchainRegistry(
            fingerprint,
            (),
            (),
            (MissingRequirement("registry", "underspecified", _REGISTRY_RELATIVE),),
        )
    baseline = raw.get("baseline", [])
    if not isinstance(baseline, list) or any(not isinstance(item, str) for item in baseline):
        missing.append(MissingRequirement("registry", "baseline", _REGISTRY_RELATIVE))
        baseline = []
    requirements: list[ProjectToolRequirement] = []
    for raw_name in baseline:
        name = _logical_name(raw_name)
        spec = tools.get(raw_name)
        if not name or not isinstance(spec, dict):
            missing.append(MissingRequirement("registry", f"tool:{raw_name}", _REGISTRY_RELATIVE))
            continue
        candidates = spec.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            missing.append(MissingRequirement("registry", f"candidates:{name}", _REGISTRY_RELATIVE))
            continue
        selected: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                missing.append(MissingRequirement("registry", f"candidate:{name}", _REGISTRY_RELATIVE))
                continue
            if not _platform_selected(candidate.get("platforms")):
                continue
            command = _candidate_command(candidate, name)
            if command:
                selected.append(command)
        if not selected:
            missing.append(MissingRequirement("registry", f"candidate_unavailable:{name}", _REGISTRY_RELATIVE))
            continue
        minimum = spec.get("minimum_version", "")
        if not isinstance(minimum, str):
            missing.append(MissingRequirement("registry", f"minimum_version:{name}", _REGISTRY_RELATIVE))
            continue
        requirements.append(ProjectToolRequirement(name, tuple(selected), minimum.strip()))
    sandbox = raw.get("sandbox_capabilities", [])
    if not isinstance(sandbox, list) or any(not isinstance(item, str) for item in sandbox):
        missing.append(MissingRequirement("registry", "sandbox_capabilities", _REGISTRY_RELATIVE))
        sandbox = []
    capabilities = tuple(sorted({item.strip() for item in sandbox if item.strip()}))
    for capability in capabilities:
        if capability not in _SUPPORTED_SANDBOX_CAPABILITIES:
            missing.append(MissingRequirement("sandbox_capability", capability, _REGISTRY_RELATIVE))
    return ProjectToolchainRegistry(
        fingerprint,
        tuple(sorted(requirements, key=lambda item: item.name)),
        capabilities,
        tuple(missing),
    )


def _read_executable_version(resolved: str) -> str:
    from . import worker_workspace

    return worker_workspace.trusted_validation_executable_version(resolved)


def _executable_fact(
    requested: str, resolved: str, *, version_fact: str | None = None
) -> ExecutableFact | None:
    path = Path(resolved)
    try:
        canonical = path.resolve(strict=True)
        status = canonical.stat()
    except OSError:
        return None
    if not stat.S_ISREG(status.st_mode) or not os.access(canonical, os.X_OK):
        return None
    fingerprint = _hash_file(canonical)
    if version_fact is None:
        version_fact = _read_executable_version(str(canonical))
    return ExecutableFact(
        requested=requested[:512],
        canonical_path=str(canonical),
        device=status.st_dev,
        inode=status.st_ino,
        size=status.st_size,
        mode=stat.S_IMODE(status.st_mode),
        mtime_ns=status.st_mtime_ns,
        fingerprint=fingerprint,
        version_fact=version_fact,
    )


class ToolchainAuthority:
    """Build and cache immutable snapshots for one canonical repository."""

    def __init__(
        self,
        repo: Path,
        *,
        manifest_path: Path | None = None,
        capability_probe: Callable[[Path, Mapping[str, Any]], Sequence[str]] | None = None,
    ) -> None:
        self.repo = repo.resolve()
        self.manifest_path = manifest_path or (
            self.repo / ".aiworkhub" / "toolchain-authority" / "snapshot-v1.json"
        )
        self._capability_probe = capability_probe
        self._lock = threading.RLock()
        self._cache_key = ""
        self._cached: AuthoritySnapshot | None = None

    @staticmethod
    def _dynamic_requirements_digest(card: Mapping[str, Any]) -> str:
        commands = tuple(
            command.strip()
            for command in card.get("validation") or ()
            if isinstance(command, str) and command.strip()
        )
        encoded = json.dumps(commands, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _cache_identity(
        self, registry: ProjectToolchainRegistry, card: Mapping[str, Any]
    ) -> tuple[str, str, str]:
        metadata = _metadata_fingerprint(self.repo)
        identity_payload = {
            "schema_id": SCHEMA_ID,
            "dynamic_requirements": self._dynamic_requirements_digest(card),
            "metadata": metadata,
            "path": _path_fingerprint(),
            "platform": _platform_fingerprint(),
            "registry": registry.fingerprint,
        }
        encoded = json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest(), metadata, str(identity_payload["path"])

    @staticmethod
    def _snapshot_from_dict(raw: object) -> AuthoritySnapshot | None:
        if not isinstance(raw, dict) or raw.get("schema_id") != SCHEMA_ID:
            return None
        try:
            facts = tuple(
                ExecutableFact(
                    requested=str(item["requested"]),
                    canonical_path=str(item["canonical_path"]),
                    device=int(item["device"]),
                    inode=int(item["inode"]),
                    size=int(item["size"]),
                    mode=int(item["mode"]),
                    mtime_ns=int(item["mtime_ns"]),
                    fingerprint=str(item["fingerprint"]),
                    version_fact=str(item["version_fact"]),
                )
                for item in raw.get("executables", [])
                if isinstance(item, dict)
            )
            missing = tuple(
                MissingRequirement(
                    kind=str(item["kind"]),
                    value=str(item["value"]),
                    command=str(item.get("command") or ""),
                )
                for item in raw.get("missing", [])
                if isinstance(item, dict)
            )
            modules = tuple(str(item) for item in raw.get("modules", []))
            digest = str(raw["digest"])
        except (KeyError, TypeError, ValueError):
            return None
        return AuthoritySnapshot(
            schema_id=SCHEMA_ID,
            repository=str(raw.get("repository") or ""),
            path=str(raw.get("path") or ""),
            executables=facts,
            modules=modules,
            repository_fingerprint=str(raw.get("repository_fingerprint") or ""),
            missing=missing,
            digest=digest,
            registry_fingerprint=str(raw.get("registry_fingerprint") or ""),
            cache_identity=str(raw.get("cache_identity") or ""),
        )

    @staticmethod
    def _payload_for_digest(snapshot: AuthoritySnapshot) -> dict[str, object]:
        return {
            "schema_id": snapshot.schema_id,
            "repository": snapshot.repository,
            "path": snapshot.path,
            "executables": [fact.as_dict() for fact in snapshot.executables],
            "modules": list(snapshot.modules),
            "repository_fingerprint": snapshot.repository_fingerprint,
            "registry_fingerprint": snapshot.registry_fingerprint,
            "cache_identity": snapshot.cache_identity,
            "missing": [fact.as_dict() for fact in snapshot.missing],
        }

    @classmethod
    def _payload_digest(cls, snapshot: AuthoritySnapshot) -> str:
        encoded = json.dumps(
            cls._payload_for_digest(snapshot),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _load_snapshot(self, cache_identity: str) -> AuthoritySnapshot | None:
        try:
            snapshot = self._snapshot_from_dict(
                json.loads(self.manifest_path.read_text(encoding="utf-8"))
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if snapshot is None or snapshot.cache_identity != cache_identity:
            return None
        if snapshot.digest != self._payload_digest(snapshot):
            return None
        if not self._executable_identities_match(snapshot):
            return None
        return snapshot

    @staticmethod
    def _executable_identities_match(snapshot: AuthoritySnapshot) -> bool:
        for fact in snapshot.executables:
            current = _executable_fact(
                fact.requested, fact.canonical_path, version_fact=fact.version_fact
            )
            if current is None:
                return False
            if (
                current.device,
                current.inode,
                current.size,
                current.mode,
                current.mtime_ns,
                current.fingerprint,
            ) != (
                fact.device,
                fact.inode,
                fact.size,
                fact.mode,
                fact.mtime_ns,
                fact.fingerprint,
            ):
                return False
        return True

    def _commands(
        self, card: Mapping[str, Any], registry: ProjectToolchainRegistry
    ) -> tuple[tuple[str, tuple[str, ...], str, str], ...]:
        commands: list[tuple[str, tuple[str, ...], str, str]] = [
            (requirement.name, requirement.commands, requirement.minimum_version, "registry")
            for requirement in registry.requirements
        ]
        for command in card.get("validation") or ():
            if isinstance(command, str) and command.strip():
                commands.append(("", (command.strip(),), "", "card"))
        return tuple(commands)

    def _derive(
        self, card: Mapping[str, Any], registry: ProjectToolchainRegistry
    ) -> tuple[list[ExecutableFact], set[str], set[MissingRequirement]]:
        # Lazy import avoids making the bare-script worker wrapper package-bound.
        from . import validation_runner
        from . import worker_workspace

        facts: dict[str, ExecutableFact] = {}
        modules: set[str] = set()
        missing: set[MissingRequirement] = set()
        for name, commands, minimum_version, source in self._commands(card, registry):
            fact: ExecutableFact | None = None
            normalized: list[str] = []
            argv: list[str] = []
            command = commands[0]
            version_mismatch = False
            for candidate in commands:
                command = candidate
                try:
                    argv, _parts, _tmpdir, _cwd = worker_workspace._parse_validation_command_detailed(candidate)
                    normalized, _roots = worker_workspace._normalize_trusted_validation_executable_argv_with_roots(argv, self.repo)
                except (ValueError, worker_workspace.WorkspaceError):
                    continue
                fact = _executable_fact(argv[0], normalized[0]) if normalized else None
                if fact is None:
                    continue
                if minimum_version and not _version_meets(fact.version_fact, minimum_version):
                    version_mismatch = True
                    fact = None
                    continue
                break
            if fact is None:
                if source == "registry":
                    if version_mismatch:
                        missing.add(
                            MissingRequirement(
                                "version",
                                f"{name}>={minimum_version}",
                                command,
                            )
                        )
                    else:
                        missing.add(MissingRequirement("executable", name, command))
                continue
            facts[fact.canonical_path] = fact
            for module in validation_runner.dash_m_validator_modules(normalized):
                modules.add(module)
        missing.update(registry.missing)
        probe = self._capability_probe or worker_workspace.preflight_validation_capabilities
        for value in probe(self.repo, card):
            kind, separator, detail = str(value).partition(":")
            missing.add(
                MissingRequirement(
                    kind if separator else "capability",
                    (detail if separator else kind)[:1024],
                )
            )
        return sorted(facts.values(), key=lambda fact: fact.canonical_path), modules, missing

    def evaluate(self, card: Mapping[str, Any]) -> AuthoritySnapshot:
        with self._lock:
            registry = _load_project_registry(self.repo)
            cache_identity, metadata, path_fingerprint = self._cache_identity(registry, card)
            if registry.fingerprint:
                if cache_identity == self._cache_key and self._cached is not None:
                    if self._executable_identities_match(self._cached):
                        return self._cached
                loaded = self._load_snapshot(cache_identity)
                if loaded is not None:
                    self._cache_key, self._cached = cache_identity, loaded
                    return loaded
            facts, modules, missing = self._derive(card, registry)
            payload: dict[str, object] = {
                "schema_id": SCHEMA_ID,
                "repository": str(self.repo),
                "path": path_fingerprint,
                "executables": [fact.as_dict() for fact in facts],
                "modules": sorted(modules),
                "repository_fingerprint": metadata,
                "registry_fingerprint": registry.fingerprint,
                "cache_identity": cache_identity,
                "missing": [fact.as_dict() for fact in sorted(missing, key=lambda item: (item.kind, item.value, item.command))],
            }
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            if digest == self._cache_key and self._cached is not None:
                return self._cached
            snapshot = AuthoritySnapshot(
                schema_id=SCHEMA_ID,
                repository=str(self.repo),
                path=str(payload["path"]),
                executables=tuple(facts),
                modules=tuple(sorted(modules)),
                repository_fingerprint=metadata,
                missing=tuple(sorted(missing, key=lambda item: (item.kind, item.value, item.command))),
                digest=digest,
                registry_fingerprint=registry.fingerprint,
                cache_identity=cache_identity,
            )
            self._cache_key = cache_identity if registry.fingerprint else digest
            self._cached = snapshot
            return snapshot

    def repair(self, snapshot: AuthoritySnapshot) -> bool:
        """Atomically persist only AIWorkHub-owned authority metadata.

        This is the concrete bounded repair operation.  It is idempotent and
        refuses paths outside the repository's ``.aiworkhub`` directory.  It
        cannot invoke a package manager, alter dependency locks, or grant a
        permission; unresolved external requirements remain unresolved.
        """
        owned = (self.repo / ".aiworkhub").resolve()
        target = self.manifest_path.resolve()
        try:
            target.relative_to(owned)
        except ValueError:
            return False
        data = json.dumps(snapshot.as_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        try:
            if target.is_file() and target.read_text(encoding="utf-8") == data:
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=target.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        except OSError:
            return False
        return True


def build_authority_snapshot(repo: Path, card: Mapping[str, Any]) -> AuthoritySnapshot:
    return ToolchainAuthority(repo).evaluate(card)


def authority_receipt(
    snapshot: AuthoritySnapshot, card: Mapping[str, Any] | None = None
) -> dict[str, object]:
    card_mapping = card or {}
    receipt: dict[str, object] = {
        "schema_id": RECEIPT_SCHEMA_ID,
        "snapshot_digest": snapshot.digest,
        "repository": snapshot.repository,
        "cache_identity": snapshot.cache_identity,
        "path": snapshot.path,
        "registry_fingerprint": snapshot.registry_fingerprint,
        "repository_fingerprint": snapshot.repository_fingerprint,
        "request_id": str(
            card_mapping.get("request_id")
            or card_mapping.get("claimed_request_id")
            or card_mapping.get("accepted_request_id")
            or ""
        ),
        "card_identity": _receipt_card_identity(card_mapping),
        "modules": list(snapshot.modules),
        "missing": [fact.as_dict() for fact in snapshot.missing],
        "executables": [fact.as_dict() for fact in snapshot.executables],
    }
    secret = _authority_secret(Path(snapshot.repository), create=True)
    if not secret:
        raise ValueError("validation_toolchain_authority_secret_unavailable")
    receipt[_RECEIPT_MAC_KEY] = _receipt_mac(
        secret, receipt, str(receipt["card_identity"])
    )
    return receipt


def verify_authority_receipt(
    receipt: Mapping[str, Any] | None, repo: Path, card: Mapping[str, Any]
) -> dict[str, object] | None:
    """Verify a launch receipt against current repository/cache identity."""
    if receipt is None:
        return None
    if receipt.get("schema_id") != RECEIPT_SCHEMA_ID:
        raise ValueError("validation_toolchain_authority_receipt_schema")
    authority = ToolchainAuthority(repo)
    registry = _load_project_registry(authority.repo)
    cache_identity, metadata, path_fingerprint = authority._cache_identity(registry, card)
    expected_scalars = {
        "repository": str(authority.repo),
        "cache_identity": cache_identity,
        "path": path_fingerprint,
        "registry_fingerprint": registry.fingerprint,
        "repository_fingerprint": metadata,
        "request_id": str(
            card.get("request_id")
            or card.get("claimed_request_id")
            or card.get("accepted_request_id")
            or ""
        ),
        "card_identity": _receipt_card_identity(card),
    }
    for key, expected in expected_scalars.items():
        if str(receipt.get(key) or "") != expected:
            raise ValueError(f"validation_toolchain_authority_receipt_{key}_mismatch")
    secret = _authority_secret(authority.repo, create=False)
    if not secret:
        raise ValueError("validation_toolchain_authority_secret_unavailable")
    expected_mac = _receipt_mac(secret, receipt, str(expected_scalars["card_identity"]))
    if not hmac.compare_digest(str(receipt.get(_RECEIPT_MAC_KEY) or ""), expected_mac):
        raise ValueError("validation_toolchain_authority_receipt_mac_mismatch")
    try:
        snapshot = AuthoritySnapshot(
            schema_id=SCHEMA_ID,
            repository=expected_scalars["repository"],
            path=expected_scalars["path"],
            executables=tuple(
                ExecutableFact(
                    requested=str(item["requested"]),
                    canonical_path=str(item["canonical_path"]),
                    device=int(item["device"]),
                    inode=int(item["inode"]),
                    size=int(item["size"]),
                    mode=int(item["mode"]),
                    mtime_ns=int(item["mtime_ns"]),
                    fingerprint=str(item["fingerprint"]),
                    version_fact=str(item["version_fact"]),
                )
                for item in receipt.get("executables", [])
                if isinstance(item, Mapping)
            ),
            modules=tuple(str(item) for item in receipt.get("modules", [])),
            repository_fingerprint=metadata,
            missing=tuple(
                MissingRequirement(
                    kind=str(item["kind"]),
                    value=str(item["value"]),
                    command=str(item.get("command") or ""),
                )
                for item in receipt.get("missing", [])
                if isinstance(item, Mapping)
            ),
            digest=str(receipt["snapshot_digest"]),
            registry_fingerprint=registry.fingerprint,
            cache_identity=cache_identity,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("validation_toolchain_authority_receipt_malformed") from exc
    if not hmac.compare_digest(snapshot.digest, authority._payload_digest(snapshot)):
        raise ValueError("validation_toolchain_authority_receipt_digest_mismatch")
    if not authority._executable_identities_match(snapshot):
        raise ValueError("validation_toolchain_authority_executable_identity_drift")
    return dict(receipt)
