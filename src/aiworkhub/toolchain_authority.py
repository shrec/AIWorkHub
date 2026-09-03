"""Coordinator-owned authority for ordinary validation toolchains.

The authority intentionally delegates parsing and executable normalization to
``worker_workspace``.  It records what that trusted boundary resolved; it does
not introduce a second PATH resolver or a task-card capability vocabulary.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

SCHEMA_ID = "aiworkhub.toolchain_authority.v1"
_FINGERPRINT_LIMIT = 1024 * 1024
_REPOSITORY_METADATA = (
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
class AuthoritySnapshot:
    schema_id: str
    repository: str
    path: str
    executables: tuple[ExecutableFact, ...]
    modules: tuple[str, ...]
    repository_fingerprint: str
    missing: tuple[MissingRequirement, ...]
    digest: str

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
        }


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


def _executable_fact(requested: str, resolved: str) -> ExecutableFact | None:
    path = Path(resolved)
    try:
        canonical = path.resolve(strict=True)
        status = canonical.stat()
    except OSError:
        return None
    if not stat.S_ISREG(status.st_mode) or not os.access(canonical, os.X_OK):
        return None
    fingerprint = _hash_file(canonical)
    # A fingerprint-derived version fact is bounded, deterministic and never
    # executes an untrusted binary merely to ask it for a version string.
    version_fact = f"sha256:{fingerprint[:16]}:size:{status.st_size}"
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

    def _derive(self, card: Mapping[str, Any]) -> tuple[list[ExecutableFact], set[str], set[MissingRequirement]]:
        # Lazy import avoids making the bare-script worker wrapper package-bound.
        from . import validation_runner
        from . import worker_workspace

        facts: dict[str, ExecutableFact] = {}
        modules: set[str] = set()
        missing: set[MissingRequirement] = set()
        commands = tuple(
            value for value in (card.get("validation") or ())
            if isinstance(value, str) and value.strip()
        )
        for command in commands:
            try:
                argv, _parts, _tmpdir, _cwd = worker_workspace._parse_validation_command_detailed(command)
                normalized, _roots = worker_workspace._normalize_trusted_validation_executable_argv_with_roots(argv, self.repo)
            except (ValueError, worker_workspace.WorkspaceError):
                # The canonical capability probe below owns exact failure
                # classification and precedence.  Fact collection is best
                # effort so there is only one source of missing-tool truth.
                continue
            fact = _executable_fact(argv[0], normalized[0]) if normalized else None
            if fact is None:
                continue
            facts[fact.canonical_path] = fact
            for module in validation_runner.dash_m_validator_modules(normalized):
                modules.add(module)
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
            facts, modules, missing = self._derive(card)
            metadata = _metadata_fingerprint(self.repo)
            payload: dict[str, object] = {
                "schema_id": SCHEMA_ID,
                "repository": str(self.repo),
                "path": "sha256:"
                + hashlib.sha256(
                    os.environ.get("PATH", "").encode("utf-8", errors="surrogateescape")
                ).hexdigest(),
                "executables": [fact.as_dict() for fact in facts],
                "modules": sorted(modules),
                "repository_fingerprint": metadata,
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
            )
            self._cache_key, self._cached = digest, snapshot
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
