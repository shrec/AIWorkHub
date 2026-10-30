"""Operator-registered compilers usable inside the validation sandbox (NF-2026-01843).

``validation_toolchains`` decides which bare names may head a validation
command; this module adds the names an operator registered in the CANONICAL
``<repo_root>/.aiworkhub/config/toolchains.json`` -- never the workspace or
candidate, so a candidate cannot add or change a toolchain.  A registered name
is a trusted system executable that must resolve beneath its entry's roots, and
the Windows AppContainer launch gives that command (only) read/execute on those
roots plus the entry's environment.  A missing registry file is an empty
registry: every existing command behaves exactly as before.

The Linux (Landlock/bubblewrap) bind of these roots is a later card; non-Windows
backends report ``backend_not_probed`` and never refuse.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import platform_io
from .card_read_roots import _contains, _real_key as _norm
from .validation_toolchains import (
    PYTHON_VALIDATION_EXECUTABLES,
    SYSTEM_VALIDATION_EXECUTABLES,
)

if TYPE_CHECKING:
    from .windows_appcontainer import ContainerGrant

REGISTRY_SCHEMA_ID = "aiworkhub.toolchains.v1"
REGISTRY_RELATIVE_PATH = Path(".aiworkhub") / "config" / "toolchains.json"
REQUEST_TEMP_TOKEN = "{request_temp}"
MAX_REGISTRY_BYTES = 64 * 1024
MAX_TOOLCHAINS = 64
MAX_LIST_ITEMS = 64
MAX_ENV_VALUE_CHARS = 4096
MAX_MEMOIZED_PROBES = 64
PROBE_TIMEOUT_SECONDS = 30

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_EXECUTABLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_RESERVED_ENV_NAMES = frozenset(
    {
        "PATH", "PYTHONPATH", "PYTHONHOME", "HOME", "USERPROFILE", "TEMP", "TMP",
        "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "COMSPEC", "PATHEXT",
    }
)
_ENTRY_KEYS = frozenset({"executables", "roots", "path_prepend", "env"})
_TOP_LEVEL_KEYS = frozenset({"schema_id", "toolchains"})


def _refusal(message: str) -> Exception:
    # Lazy: worker_workspace imports this module, so a module-level import cycles.
    from .worker_workspace import WorkspaceError

    return WorkspaceError(message)


def _invalid(detail: str) -> Exception:
    return _refusal(f"toolchain_registry_invalid:{detail}")


def _is_windows() -> bool:
    return platform_io.is_windows()


def _under(path: Path, directories: Sequence[str]) -> bool:
    key = _norm(path.resolve(strict=False))
    return any(_contains(_norm(Path(d).resolve(strict=False)), key) for d in directories)


@dataclass(frozen=True)
class Toolchain:
    id: str
    executables: tuple[str, ...]
    roots: tuple[str, ...]
    path_prepend: tuple[str, ...]
    env: tuple[tuple[str, str], ...]

    def directories(self) -> tuple[str, ...]:
        return (*self.roots, *self.path_prepend)

    def launch_env(self, env: Mapping[str, str]) -> dict[str, str]:
        """``env`` with this toolchain's variables and PATH prepend, for its command only."""
        out = dict(env)
        temp = env.get("TMPDIR") or env.get("TEMP") or env.get("TMP") or ""
        for name, value in self.env:
            if REQUEST_TEMP_TOKEN in value and not temp:
                raise _refusal(f"toolchain_request_temp_unavailable:{self.id}")
            out[name] = value.replace(REQUEST_TEMP_TOKEN, temp)
        if self.path_prepend:
            path_key = next((key for key in out if key.upper() == "PATH"), "PATH")
            existing = out.get(path_key)
            out[path_key] = os.pathsep.join(
                [*self.path_prepend, *([existing] if existing else [])]
            )
        return out

    def read_grants(self, *, covered: Sequence[str] = ()) -> list[ContainerGrant]:
        """Persistent read/execute on the roots and PATH prepend, like ``python_read_grants``.

        ``covered`` is what the request already grants (worktree, HOME, temp, all
        container-writable).  A toolchain directory inside it -- or one that
        contains it -- is refused: a worker could plant the former and the
        latter would hand the container a wider read than the request has.
        """
        from .windows_appcontainer import (
            AppContainerError,
            AppContainerReason,
            ContainerGrant,
            outside_system_trees,
        )

        writable = [_norm(path) for path in covered]
        keyed = [(directory, _norm(directory)) for directory in self.directories()]
        for directory, key in keyed:
            if any(_contains(w, key) or _contains(key, w) for w in writable):
                raise AppContainerError(
                    AppContainerReason.INVALID_REQUEST,
                    detail=f"a toolchain root the container can write is never granted: {directory!r}.",
                )
        skip: list[str] = []
        grants: list[ContainerGrant] = []
        for directory, key in keyed:
            if os.path.exists(directory) and not any(_contains(other, key) for other in skip):
                skip.append(key)
                grants.append(ContainerGrant(directory, "read_execute", persistent=True))
        return outside_system_trees(grants)


@dataclass(frozen=True)
class ToolchainRegistry:
    sha256: str
    toolchains: tuple[Toolchain, ...] = ()

    def get(self, toolchain_id: str) -> Toolchain | None:
        return next((tc for tc in self.toolchains if tc.id == toolchain_id), None)

    def for_executable(self, name: str) -> Toolchain | None:
        return next((tc for tc in self.toolchains if name in tc.executables), None)


_EMPTY_REGISTRY = ToolchainRegistry(sha256="")


def canonical_repo_root() -> Path | None:
    """The canonical repository root, without importing ``core`` into a sparse closure."""
    core = sys.modules.get(f"{__package__}.core")
    try:
        if core is not None:
            return Path(core.repo_root()).resolve()
        raw = os.environ.get("AIWORKHUB_REPO_ROOT", "").strip()
        return Path(raw).resolve() if raw else None
    except Exception:  # an unbound or unresolvable canonical root means no registry
        return None


def _directories(value: Any, field: str, repo: Path) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_LIST_ITEMS:
        raise _invalid(f"{field}_not_a_bounded_list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or "\x00" in item:
            raise _invalid(f"{field}_entry_not_a_path")
        if not os.path.isabs(item):
            raise _invalid(f"{field}_not_absolute:{item}")
        if _contains(_norm(repo), _norm(Path(item).resolve(strict=False))):
            raise _invalid(f"{field}_inside_repository:{item}")
        out.append(os.path.normpath(item))
    return tuple(out)


def _environment(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or len(value) > MAX_LIST_ITEMS:
        raise _invalid("env_not_a_bounded_object")
    out: list[tuple[str, str]] = []
    for name, text in value.items():
        if not isinstance(name, str) or not _ENV_NAME_RE.match(name):
            raise _invalid(f"env_name_invalid:{name!r}")
        if name.upper() in _RESERVED_ENV_NAMES:
            raise _invalid(f"env_name_reserved:{name}")
        if not isinstance(text, str) or "\x00" in text or len(text) > MAX_ENV_VALUE_CHARS:
            raise _invalid(f"env_value_invalid:{name}")
        out.append((name, text))
    return tuple(sorted(out))


def _toolchain(toolchain_id: Any, entry: Any, repo: Path) -> Toolchain:
    if not isinstance(toolchain_id, str) or not _ID_RE.match(toolchain_id):
        raise _invalid(f"id_invalid:{toolchain_id!r}")
    if not isinstance(entry, dict):
        raise _invalid(f"entry_not_an_object:{toolchain_id}")
    unknown = sorted(set(entry) - _ENTRY_KEYS)
    if unknown:
        raise _invalid(f"unknown_keys:{toolchain_id}:{','.join(map(str, unknown))}")
    executables = entry.get("executables")
    if not isinstance(executables, list) or not executables or len(executables) > MAX_LIST_ITEMS:
        raise _invalid(f"executables_not_a_bounded_list:{toolchain_id}")
    for name in executables:
        if not isinstance(name, str) or not _EXECUTABLE_RE.match(name):
            raise _invalid(f"executable_invalid:{toolchain_id}:{name!r}")
        if name in PYTHON_VALIDATION_EXECUTABLES:
            raise _invalid(f"executable_reserved:{toolchain_id}:{name}")
    roots = _directories(entry.get("roots"), "roots", repo)
    if not roots:
        raise _invalid(f"roots_empty:{toolchain_id}")
    return Toolchain(
        id=toolchain_id,
        executables=tuple(executables),
        roots=roots,
        path_prepend=_directories(entry.get("path_prepend", []), "path_prepend", repo),
        env=_environment(entry.get("env", {})),
    )


def parse_registry(raw: bytes, repo: Path) -> ToolchainRegistry:
    try:
        document = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise _invalid("not_json") from exc
    if not isinstance(document, dict):
        raise _invalid("not_an_object")
    unknown = sorted(set(document) - _TOP_LEVEL_KEYS)
    if unknown:
        raise _invalid(f"unknown_keys:{','.join(map(str, unknown))}")
    if document.get("schema_id") != REGISTRY_SCHEMA_ID:
        raise _invalid("schema_id_mismatch")
    entries = document.get("toolchains")
    if not isinstance(entries, dict) or len(entries) > MAX_TOOLCHAINS:
        raise _invalid("toolchains_not_a_bounded_object")
    toolchains = tuple(_toolchain(key, value, repo) for key, value in sorted(entries.items()))
    seen: dict[str, str] = {}
    for toolchain in toolchains:
        for name in toolchain.executables:
            if seen.setdefault(name, toolchain.id) != toolchain.id:
                raise _invalid(f"executable_in_two_toolchains:{name}")
    return ToolchainRegistry(hashlib.sha256(raw).hexdigest(), toolchains)


def load_registry() -> ToolchainRegistry:
    """Read ONLY the canonical registry; absent means empty, malformed refuses."""
    root = canonical_repo_root()
    if root is None:
        return _EMPTY_REGISTRY
    try:
        with (root / REGISTRY_RELATIVE_PATH).open("rb") as handle:
            raw = handle.read(MAX_REGISTRY_BYTES + 1)
    except (FileNotFoundError, NotADirectoryError):
        return _EMPTY_REGISTRY
    except OSError as exc:
        raise _refusal(f"toolchain_registry_unreadable:{type(exc).__name__}") from exc
    if len(raw) > MAX_REGISTRY_BYTES:
        raise _invalid("too_large")
    return parse_registry(raw, root)


def registered_toolchain(name: str) -> Toolchain | None:
    if not name or name in PYTHON_VALIDATION_EXECUTABLES:
        return None
    return load_registry().for_executable(name)


def toolchain_for_launch(executable: str) -> Toolchain | None:
    """The entry an absolute launch ``executable`` resolves through, else ``None``."""
    path = Path(executable)
    if not path.is_absolute() or path.name.lower().startswith("python"):
        return None
    names = {path.name.casefold(), path.stem.casefold()}
    resolved = path.resolve(strict=False)
    for toolchain in load_registry().toolchains:
        if not _under(path, toolchain.directories()):
            continue
        if any(name.casefold() in names for name in toolchain.executables):
            return toolchain
        # ``executable`` arrives symlink-resolved, so a registered name that links
        # to a differently named binary matches only by what it resolves to.
        search = os.pathsep.join([*toolchain.path_prepend, os.environ.get("PATH", "")])
        for name in toolchain.executables:
            found = shutil.which(name, path=search)
            if found and Path(found).resolve(strict=False) == resolved:
                return toolchain
    return None


def _resolve_trusted_system_validation_executable(
    name: str, repo: Path | None = None
) -> Path:
    """Resolve one explicitly approved host tool to an immutable absolute path."""
    toolchain = registered_toolchain(name)
    if toolchain is None and name not in SYSTEM_VALIDATION_EXECUTABLES:
        raise _refusal(f"validation_executable_not_approved:{name}")
    if toolchain is None:
        discovered = shutil.which(name)
    else:
        search = os.pathsep.join([*toolchain.path_prepend, os.environ.get("PATH", "")])
        discovered = shutil.which(name, path=search)
    if not discovered:
        raise _refusal(f"validation_executable_unavailable:{name}")
    try:
        resolved = Path(discovered).resolve(strict=True)
    except OSError as exc:
        raise _refusal(f"validation_executable_unavailable:{name}") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise _refusal(f"validation_executable_not_executable:{resolved}")
    if repo is not None:
        try:
            resolved.relative_to(repo.resolve(strict=False))
        except ValueError:
            pass
        else:
            raise _refusal(f"validation_executable_repository_owned:{resolved}")
    if toolchain is not None and not _under(resolved, toolchain.directories()):
        raise _refusal(f"validation_executable_outside_toolchain_roots:{resolved}")
    return resolved


@dataclass(frozen=True)
class ToolchainProbe:
    status: str
    reason: str = ""


_PROBES: dict[tuple[str, str], ToolchainProbe] = {}


def _launch_in_appcontainer(argv: list[str], repo: Path) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` through the same AppContainer validation launch a command uses."""
    from . import worker_workspace as ww

    with tempfile.TemporaryDirectory(prefix="toolchain-probe-", ignore_cleanup_errors=True) as raw:
        request_id = "toolchain-probe"
        root = Path(raw) / request_id
        worktree, home = root / "worktree", root / "home"
        worktree.mkdir(parents=True)
        home.mkdir()
        workspace = ww.WorkerWorkspace(
            request_id=request_id,
            repo=repo,
            path=worktree,
            home=home,
            allowed_writes=(),
            parent_baseline={},
            workspace_baseline={},
        )
        env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "COMSPEC", "PATHEXT") if key in os.environ}
        env.update(HOME=str(home), USERPROFILE=str(home), TMPDIR=str(home), TEMP=str(home), TMP=str(home))
        return ww._run_appcontainer_validation(
            argv,
            workspace=workspace,
            adapter_id="toolchain_probe",
            cwd=worktree,
            env=env,
            timeout_seconds=PROBE_TIMEOUT_SECONDS,
        )


def _probe(
    repo: Path,
    toolchain: Toolchain,
    launch: Callable[[list[str], Path], subprocess.CompletedProcess[str]],
) -> ToolchainProbe:
    try:
        executable = _resolve_trusted_system_validation_executable(toolchain.executables[0], repo)
        result = launch([str(executable), "--version"], repo)
    except subprocess.TimeoutExpired:
        return ToolchainProbe("unreachable", "timeout")
    except Exception as exc:  # a probe that cannot launch is unreachable, never a preflight crash
        return ToolchainProbe("unreachable", " ".join(str(exc).split())[:160] or type(exc).__name__)
    if result.returncode != 0:
        return ToolchainProbe("unreachable", f"exit_{result.returncode}")
    return ToolchainProbe("reachable")


def probe_toolchain(
    repo: Path,
    toolchain_id: str,
    *,
    launch: Callable[[list[str], Path], subprocess.CompletedProcess[str]] | None = None,
) -> ToolchainProbe:
    """Run ``<first executable> --version`` through the launch path; memoized per (id, registry sha256)."""
    registry = load_registry()
    toolchain = registry.get(toolchain_id)
    if toolchain is None:
        return ToolchainProbe("unreachable", "not_registered")
    if launch is None:
        if not _is_windows():
            return ToolchainProbe("backend_not_probed")
        launch = _launch_in_appcontainer
    key = (toolchain_id, registry.sha256)
    cached = _PROBES.get(key)
    if cached is not None:
        return cached
    probe = _probe(repo, toolchain, launch)
    if len(_PROBES) >= MAX_MEMOIZED_PROBES:
        _PROBES.pop(next(iter(_PROBES)))
    _PROBES[key] = probe
    return probe


def toolchain_unreachable_reason(repo: Path, head: str) -> str | None:
    """``toolchain_unreachable:<id>:<reason>`` when ``head`` is a registered but unreachable tool."""
    toolchain = registered_toolchain(head)
    if toolchain is None:
        return None
    probe = probe_toolchain(repo, toolchain.id)
    if probe.status != "unreachable":
        return None
    return f"toolchain_unreachable:{toolchain.id}:{probe.reason}"
