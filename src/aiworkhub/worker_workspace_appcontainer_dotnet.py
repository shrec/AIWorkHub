"""Request-scoped dotnet adaptation for the AppContainer validation lane.

Split out of ``worker_workspace`` for the module size ratchet.  NF-2026-01366
measured the failures inside the container, not on the host, with no
capability SIDs: ``restore`` died MSB0001 acquiring an MSBuild node and
``test`` crashed OutOfProcNode (node reuse and the VBCSCompiler need global
named pipes an AppContainer cannot open), ``build`` died MSB3191 writing
``obj`` into the read-only worktree, and NuGet failed with a null Path1
because ``APPDATA`` is unset for the container user.  The flags below keep
MSBuild in one process and move obj/bin to a request-scoped directory,
``_appcontainer_dotnet_env`` points the CLI at the request home, and
``_appcontainer_dotnet_prerestore`` seeds that home from the host once per
request.  The recipe measured 76/76 build and 9/9 test green.

The follow-up measured the seeding itself: ``.slnx`` was not a project suffix,
a named target the canonical tree lacked fell back to whatever solution sat in
the working directory, and a card that adds a project or a ``PackageReference``
could not be seeded at all.  The named target is now authoritative -- restored
from the canonical tree, from the synthetic project
``worker_workspace_appcontainer_dotnet_restore`` authors out of candidate XML
data, or from both when the candidate adds to a target the canonical tree
already carries -- and anything else is a typed environment reason.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from .worker_workspace_appcontainer_dotnet_restore import (
    _is_within,
    synthetic_restore_project,
)

_DOTNET_EXECUTABLE_BASENAMES = frozenset({"dotnet", "dotnet.exe"})
_DOTNET_ADAPTED_VERBS = frozenset({"build", "test", "restore", "publish"})
_DOTNET_APP_CONTAINER_SWITCHES = {
    "DOTNET_NOLOGO": "1",
    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "MSBUILDDISABLENODEREUSE": "1",
    "DOTNET_CLI_DO_NOT_USE_MSBUILD_SERVER": "1",
}
_DOTNET_HOST_SWITCHES = ("PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")
_DOTNET_APP_CONTAINER_FLAGS = (
    (("-m:1",), frozenset({"m", "maxcpucount"})),
    (("-nodeReuse:false",), frozenset({"nodereuse", "nr"})),
    (("-p:UseSharedCompilation=false",), frozenset({"usesharedcompilation"})),
    (("-p:NuGetAudit=false",), frozenset({"nugetaudit"})),
)
_DOTNET_ARTIFACTS_SWITCHES = frozenset({"artifacts-path", "artifactspath"})
_DOTNET_PROPERTY_PREFIXES = ("p:", "property:", "p=", "property=")
_DOTNET_PROJECT_SUFFIXES = (
    ".csproj",
    ".fsproj",
    ".vbproj",
    ".sln",
    # The XML solution format. NF-2026-01366 measured EntryLink.Edge.slnx being
    # skipped as "not a project", which sent the pre-restore to the cwd
    # fallback and restored a different solution entirely.
    ".slnx",
    ".slnf",
    ".proj",
)
# ``/p:Name=value``, ``/t:Build``, ``/m:1``: an MSBuild switch in the Windows
# style. A POSIX absolute path deliberately does not match, so a positional
# project argument is recognised identically on every OS.
_DOTNET_SWITCH_SHAPE = re.compile(r"^/[A-Za-z][A-Za-z0-9_-]*[:=]")
_DOTNET_PRERESTORE_PREFIX = "validation_executable_unavailable:dotnet_prerestore:"
_DOTNET_PRERESTORE_TIMEOUT_SECONDS = 600
_DOTNET_PRERESTORE_MARKER = ".aiworkhub-dotnet-prerestore"
_APPCONTAINER_DOTNET_ARTIFACTS_DIRNAME = "dotnet-artifacts"
_DOTNET_PRERESTORE_ARTIFACTS_DIRNAME = "dotnet-prerestore"


def _is_appcontainer_dotnet_executable(executable: str) -> bool:
    """True when ``executable`` is the dotnet host, on either path style."""
    name = str(executable).replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name in _DOTNET_EXECUTABLE_BASENAMES


def _dotnet_declared_switches(argv: Sequence[str]) -> frozenset[str]:
    """Every switch ``argv`` declares, lowercased and value-stripped.

    ``-x``, ``/x``, ``--x``, ``-p:Name=...`` and ``-property:Name=...`` all
    count, so a flag the caller declared always wins over a lane default.
    """
    declared: set[str] = set()
    for token in argv[1:]:
        stripped = token.lstrip("-/")
        name = stripped.split("=", 1)[0].split(":", 1)[0]
        if name:
            declared.add(name.lower())
        lowered = stripped.lower()
        for prefix in _DOTNET_PROPERTY_PREFIXES:
            if lowered.startswith(prefix):
                prop = stripped[len(prefix):].split("=", 1)[0]
                prop = prop.split(":", 1)[0]
                if prop:
                    declared.add(prop.lower())
    return frozenset(declared)


def _appcontainer_dotnet_validation_argv(
    argv: Iterable[str], artifacts_path: str | Path
) -> list[str]:
    """The argv a dotnet validation can run inside the AppContainer.

    Pure and idempotent: a second pass finds every switch already declared and
    inserts nothing.  Only build/test/restore/publish are adapted; every
    insertion lands before a bare ``--`` so the caller's own pass-through
    arguments keep their position, and a flag the caller declared wins.
    """
    rewritten = [str(part) for part in argv]
    if len(rewritten) < 2 or not _is_appcontainer_dotnet_executable(rewritten[0]):
        return rewritten
    if rewritten[1].lower() not in _DOTNET_ADAPTED_VERBS:
        return rewritten
    declared = _dotnet_declared_switches(rewritten)
    insertions: list[tuple[str, ...]] = []
    if not declared & _DOTNET_ARTIFACTS_SWITCHES:
        insertions.append(("--artifacts-path", str(artifacts_path)))
    for tokens, names in _DOTNET_APP_CONTAINER_FLAGS:
        if not declared & names:
            insertions.append(tokens)
    if not insertions:
        return rewritten
    insert_at = rewritten.index("--", 2) if "--" in rewritten[2:] else 2
    for tokens in insertions:
        rewritten[insert_at:insert_at] = list(tokens)
        insert_at += len(tokens)
    return rewritten


def _appcontainer_dotnet_env(
    env: Mapping[str, str], home: str | Path
) -> dict[str, str]:
    """The env one dotnet validation needs, scoped to the request home.

    The input mapping is never mutated.  ``DOTNET_CLI_HOME``, ``APPDATA``,
    ``LOCALAPPDATA`` and ``NUGET_PACKAGES`` all land under ``home``, which the
    lane already modify-grants, so this adds no grant and no capability SID.
    The five switches cut the host-wide rendezvous points (MSBuild node reuse,
    the MSBuild server, VBCSCompiler); the platform variables are copied from
    the host only when the caller omitted them.
    """
    root = Path(home)
    scoped = {
        "DOTNET_CLI_HOME": root,
        "APPDATA": root / "AppData" / "Roaming",
        "LOCALAPPDATA": root / "AppData" / "Local",
        "NUGET_PACKAGES": root / ".nuget" / "packages",
    }
    rewritten = dict(env)
    for name, path in scoped.items():
        path.mkdir(parents=True, exist_ok=True)
        rewritten[name] = str(path)
    rewritten.update(_DOTNET_APP_CONTAINER_SWITCHES)
    for name in _DOTNET_HOST_SWITCHES:
        if name not in rewritten and name in os.environ:
            rewritten[name] = os.environ[name]
    return rewritten


def _appcontainer_dotnet_needs_packages(argv: Sequence[str]) -> bool:
    """True when the verb resolves packages, so the pre-restore matters."""
    if (
        len(argv) < 2
        or not _is_appcontainer_dotnet_executable(argv[0])
        or argv[1].lower() not in _DOTNET_ADAPTED_VERBS
    ):
        return False
    return not any(flag in argv for flag in ("--no-restore", "--no-build"))


def _resolved_dotnet_executable(executable: str) -> str:
    """The absolute dotnet host, or the declared name when unresolvable."""
    return shutil.which(str(executable)) or str(executable)


def _host_nuget_packages() -> str:
    """The host's global packages folder, used only as a local source."""
    configured = os.environ.get("NUGET_PACKAGES", "").strip()
    return configured or str(Path.home() / ".nuget" / "packages")


def _canonical_dotnet_cwd(workspace, cwd: str | Path) -> Path:
    """Map a candidate working directory onto the canonical tree."""
    absolute = os.path.abspath(str(cwd))
    if _is_within(absolute, workspace.path):
        return Path(workspace.repo) / os.path.relpath(absolute, str(workspace.path))
    return Path(workspace.repo)


def _is_dotnet_switch(token: str) -> bool:
    """True for a flag rather than a positional target, on either path style.

    ``-x``/``--x`` and the MSBuild ``/p:Name=value`` shape are switches; a
    POSIX absolute path such as ``/repo/app.csproj`` is not, which is what lets
    these decisions be proven on any OS.
    """
    return token.startswith("-") or bool(_DOTNET_SWITCH_SHAPE.match(token))


def _dotnet_target_token(argv: Sequence[str]) -> str | None:
    """The project or solution ``argv`` names, before any tree is consulted.

    Answered from argv alone: the pre-restore has to know that a target was
    named even when neither tree carries it, because that is exactly the case
    in which falling back to the working directory restores the wrong thing.
    """
    for token in argv[2:]:
        if _is_dotnet_switch(token):
            continue
        if token.lower().endswith(_DOTNET_PROJECT_SUFFIXES):
            return token
    return None


def _canonical_dotnet_project(
    token: str, workspace, canonical_cwd: Path
) -> str | None:
    """The canonical file a named target maps to, or ``None``.

    SECURITY: ``dotnet restore`` runs project targets, so the answer is always
    a file inside ``workspace.repo``.  A candidate path is mapped onto the
    canonical tree rather than returned, and a target the canonical tree does
    not carry is ``None`` -- never a different file that happens to be nearby.
    """
    candidate = Path(token)
    if candidate.is_absolute():
        if _is_within(candidate, workspace.repo):
            mapped = candidate
        elif _is_within(candidate, workspace.path):
            mapped = Path(workspace.repo) / os.path.relpath(
                str(candidate), str(workspace.path)
            )
        else:
            return None
    else:
        mapped = canonical_cwd / candidate
    if _is_within(mapped, workspace.repo) and mapped.is_file():
        return str(mapped)
    return None


def _candidate_dotnet_project(token: str, workspace, cwd: str | Path) -> Path | None:
    """The candidate-worktree file a named target resolves to, or ``None``.

    Only this file's XML is ever read:
    ``worker_workspace_appcontainer_dotnet_restore`` turns that data into an
    AIWorkHub-authored project, and the candidate file itself never reaches the
    host CLI.
    """
    candidate = Path(token)
    if candidate.is_absolute():
        mapped = candidate
    else:
        mapped = Path(os.path.abspath(str(cwd))) / candidate
    if not _is_within(mapped, workspace.path):
        return None
    return Path(os.path.abspath(str(mapped)))


def _canonical_project_in(canonical_cwd: Path) -> bool:
    """True when the canonical directory carries a project or solution."""
    try:
        if not canonical_cwd.is_dir():
            return False
        return any(
            entry.is_file() and entry.name.lower().endswith(_DOTNET_PROJECT_SUFFIXES)
            for entry in canonical_cwd.iterdir()
        )
    except OSError:
        return False


def _dotnet_restore_one(
    argv: Sequence[str],
    project: Sequence[str],
    restore_cwd: str | Path,
    packages: Path,
    home: str | Path,
) -> int:
    """Run the one offline host restore shape, and return its exit code.

    Both seeds -- the canonical tree and an AIWorkHub-authored synthetic
    project -- go through exactly this command, so the single local source, the
    package destination and the request-scoped artifacts directory cannot drift
    apart between them.
    """
    command = [
        _resolved_dotnet_executable(str(argv[0])),
        "restore",
        *project,
        "--packages",
        str(packages),
        "--source",
        _host_nuget_packages(),
        "-p:NuGetAudit=false",
        "-nodeReuse:false",
        "--artifacts-path",
        str(Path(home) / _DOTNET_PRERESTORE_ARTIFACTS_DIRNAME),
    ]
    completed = subprocess.run(
        command,
        cwd=str(restore_cwd),
        capture_output=True,
        check=False,
        shell=False,
        text=True,
        timeout=_DOTNET_PRERESTORE_TIMEOUT_SECONDS,
    )
    return completed.returncode


def _appcontainer_dotnet_prerestore(
    argv: Sequence[str],
    workspace,
    cwd: str | Path,
    home: str | Path,
) -> str | None:
    """Seed the request-scoped package cache from the host, once per request.

    ``None`` means nothing blocks the container command, including for a verb
    that needs no packages and for a candidate target that declares no package
    at all; a string is a short reason the caller surfaces as a typed
    validation-environment failure.  Never raises.

    SECURITY: ``dotnet restore`` runs project targets, so the host CLI is never
    given a candidate file.  When ``argv`` names a target the canonical tree
    carries, that canonical file is restored first, unchanged; a candidate file
    of the same name then adds a SECOND, supplementary restore of the
    AIWorkHub-authored synthetic project, which is how a card that adds a
    project or a ``PackageReference`` to an existing solution gets its packages
    (EntryLink 003c adds ``apps/edge`` to a canonical ``EntryLink.Edge.slnx``).
    When the canonical tree lacks the target entirely, that synthetic project is
    the only thing restored.  Either way the candidate XML is read as DATA and
    the candidate file itself never reaches the CLI.  The cwd fallback is
    reachable only when ``argv`` names no target at all, so a named target can
    never be substituted by a different solution sitting in the same directory
    -- the NF-2026-01366 wrong-solution pre-restore that filed an environment
    block as candidate code.  The only writes go to ``--packages`` under the
    request home and a request-scoped ``--artifacts-path``; no host source other
    than the global packages folder is added, and no classification reads the
    CLI's stdout or stderr.
    """
    try:
        if not _appcontainer_dotnet_needs_packages(argv):
            return None
        packages = Path(home) / ".nuget" / "packages"
        packages.mkdir(parents=True, exist_ok=True)
        marker = packages / _DOTNET_PRERESTORE_MARKER
        if marker.exists():
            return None
        canonical_cwd = _canonical_dotnet_cwd(workspace, cwd)
        token = _dotnet_target_token(argv)
        synthetic = False
        addition: Path | None = None
        if token is None:
            if not _canonical_project_in(canonical_cwd):
                return "no_canonical_project"
            project: list[str] = []
            restore_cwd: Path = canonical_cwd
        else:
            canonical = _canonical_dotnet_project(token, workspace, canonical_cwd)
            candidate = _candidate_dotnet_project(token, workspace, cwd)
            if canonical is not None:
                project = [canonical]
                restore_cwd = canonical_cwd
                if candidate is not None and candidate.is_file():
                    addition = candidate
            else:
                if candidate is None:
                    return "candidate_target_outside_worktree"
                seed, reason = synthetic_restore_project(
                    candidate, workspace.path, home
                )
                if reason is not None:
                    return reason
                if seed is None:
                    return None
                synthetic = True
                project = [str(seed)]
                restore_cwd = seed.parent
        marker.write_text("attempted\n", encoding="utf-8")
        returncode = _dotnet_restore_one(argv, project, restore_cwd, packages, home)
        if returncode != 0:
            if synthetic:
                return f"synthetic_restore_exit_{returncode}"
            return f"prerestore_exit_{returncode}"
        if addition is None:
            return None
        seed, reason = synthetic_restore_project(
            addition, workspace.path, home, supplementary=True
        )
        if reason is not None:
            return reason
        if seed is None:
            return None
        returncode = _dotnet_restore_one(argv, [str(seed)], seed.parent, packages, home)
        if returncode != 0:
            return f"synthetic_restore_exit_{returncode}"
    except subprocess.TimeoutExpired:
        return "prerestore_timeout"
    except Exception as exc:
        return f"prerestore_failed:{type(exc).__name__}"
    return None


def _appcontainer_dotnet_prerestore_failure(reason: str) -> OSError:
    """The typed validation-environment failure for a blocked pre-restore.

    ``process_launcher_validation`` reads the
    ``validation_executable_unavailable:`` prefix off the surfaced failure, so
    a missing host package store is filed as an environment block and never as
    candidate code.
    """
    return OSError(f"{_DOTNET_PRERESTORE_PREFIX}{reason}")


def _appcontainer_dotnet_validation_command(
    argv: Iterable[str],
    *,
    workspace,
    cwd: str | Path,
    env: Mapping[str, str],
) -> tuple[list[str], dict[str, str]]:
    """The effective argv and env for one dotnet validation command.

    ``_run_appcontainer_validation`` calls exactly this, so every dotnet
    decision lives in this module.  Raises the typed validation-environment
    ``OSError`` when the one-shot pre-restore cannot seed the package cache of
    a verb that needs packages.
    """
    home = Path(workspace.home)
    effective_argv = _appcontainer_dotnet_validation_argv(
        argv, home / _APPCONTAINER_DOTNET_ARTIFACTS_DIRNAME
    )
    scoped_env = _appcontainer_dotnet_env(env, home)
    reason = _appcontainer_dotnet_prerestore(effective_argv, workspace, cwd, home)
    if reason:
        raise _appcontainer_dotnet_prerestore_failure(reason)
    return effective_argv, scoped_env
