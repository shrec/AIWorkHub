"""Read/execute grants for an SDK a container command launches (NF-2026-02249).

A Flutter validation (``flutter analyze``) runs ``<sdk>\\bin\\flutter.bat``,
which ``cmd.exe`` interprets and which then starts
``<sdk>\\bin\\cache\\dart-sdk\\bin\\dart.exe``.  The SDK usually lives outside
the request (``C:\\tools\\flutter``), so the AppContainer SID could not even
open the batch file and the launch came back as a bare ``[WinError 5]`` from
``_winapi.CreateProcess``.  This module finds, for a launch argv, the SDK root
that owns its command head and plans ONE persistent read/execute grant on it --
the same shape :func:`windows_appcontainer.python_read_grants` gives an
interpreter's install root.  The grant is inheritable, so ``bin\\cache\\dart-sdk``
(and every cache directory Flutter recreates there later) is covered by it.

The command head is ``argv[0]`` plus the command a launcher in front of it
runs: ``cmd.exe /c <head>`` and the validation lane's ``python -c <trampoline>
<head> ...`` (worker_workspace ``_APPCONTAINER_TEMP_TRAMPOLINE``).  Only a head
named ``flutter`` or ``dart`` is ever resolved, so every other launch costs no
filesystem access at all.  An unrecognised layout plans nothing and the launch
behaves exactly as before; a recognised root that cannot be granted is refused
by :func:`windows_appcontainer.launch_appcontainer` with this module's
:func:`grant_failure_detail`, naming the root and the cause.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

try:
    from .windows_appcontainer import (
        ContainerGrant,
        _within,
        is_python_executable,
        outside_system_trees,
    )
except ImportError:  # direct-script entrypoint, like windows_appcontainer's own
    from windows_appcontainer import (  # type: ignore[no-redef]
        ContainerGrant,
        _within,
        is_python_executable,
        outside_system_trees,
    )

TOOLCHAIN_HEAD_STEMS = frozenset({"flutter", "dart"})
FLUTTER_LAUNCHER_NAMES = ("flutter.bat", "flutter")
DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"
MAX_SHELL_SWITCH_SCAN = 3
_SHELL_RUN_SWITCHES = frozenset({"/c", "/k"})


@dataclass(frozen=True)
class ToolchainRoot:
    """One SDK install root a command head resolved into."""

    kind: str  # "flutter_sdk" | "dart_sdk"
    root: str
    launcher: str

    def describe(self) -> str:
        return f"{self.kind} toolchain root {self.root!r} (launcher {self.launcher!r})"


@dataclass(frozen=True)
class ToolchainGrant:
    toolchain: ToolchainRoot
    grant: ContainerGrant


def _first_token(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith('""'):
        # cmd /s /c outer quotes: ""C:\x\flutter.bat" analyze" -> drop the outer one.
        stripped = stripped[1:]
    if stripped.startswith('"'):
        end = stripped.find('"', 1)
        return stripped[1:end] if end > 0 else stripped[1:]
    return stripped.split()[0] if stripped else ""


def command_heads(argv: Sequence[str]) -> list[str]:
    """``argv[0]`` and the command a ``cmd.exe /c`` or ``python -c`` launcher runs."""
    if not argv:
        return []
    first = str(argv[0])
    heads = [first]
    if PureWindowsPath(first).stem.casefold() == "cmd":
        for index in range(1, min(len(argv) - 1, 1 + MAX_SHELL_SWITCH_SCAN)):
            if str(argv[index]).casefold() in _SHELL_RUN_SWITCHES:
                heads.append(_first_token(str(argv[index + 1])))
                break
    elif is_python_executable(first) and len(argv) > 3 and argv[1] == "-c":
        # A trampoline may itself run cmd.exe /c <tool>: resolve that layer too.
        heads.extend(command_heads([str(arg) for arg in argv[3:]]))
    return [head for head in heads if head]


def _is_flutter_bin(directory: Path) -> bool:
    return directory.name.casefold() == "bin" and any(
        (directory / name).is_file() for name in FLUTTER_LAUNCHER_NAMES
    )


def toolchain_root_for(launcher: Path) -> ToolchainRoot | None:
    """The SDK root owning a resolved ``flutter``/``dart`` launcher, else ``None``.

    Flutter: the parent of the ``bin`` holding ``flutter``/``flutter.bat``,
    reached from ``bin\\flutter.bat``, ``bin\\dart.bat`` or the cached
    ``bin\\cache\\dart-sdk\\bin\\dart.exe``.  A standalone Dart SDK: the parent of
    ``bin\\dart.exe`` that carries the SDK's ``version`` file.
    """
    stem = PureWindowsPath(launcher.name).stem.casefold()
    directory = launcher.parent
    if stem not in TOOLCHAIN_HEAD_STEMS or directory.name.casefold() != "bin":
        return None
    if _is_flutter_bin(directory):
        return ToolchainRoot("flutter_sdk", str(directory.parent), str(launcher))
    if stem != "dart":
        return None
    sdk = directory.parent
    cache = sdk.parent
    if (
        sdk.name.casefold() == "dart-sdk"
        and cache.name.casefold() == "cache"
        and _is_flutter_bin(cache.parent)
    ):
        return ToolchainRoot("flutter_sdk", str(cache.parent.parent), str(launcher))
    if (sdk / "version").is_file():
        return ToolchainRoot("dart_sdk", str(sdk), str(launcher))
    return None


def _env_value(environment: Mapping[str, str], name: str) -> str:
    key = next((key for key in environment if key.upper() == name), None)
    return str(environment.get(key) or "") if key is not None else ""


def launch_toolchain_roots(
    argv: Sequence[str], environment: Mapping[str, str] | None
) -> list[ToolchainRoot]:
    """Distinct SDK roots behind ``argv``'s command heads, resolved on its PATH."""
    heads = [
        head
        for head in command_heads(argv)
        if PureWindowsPath(head).stem.casefold() in TOOLCHAIN_HEAD_STEMS
    ]
    if not heads:
        return []
    # The resolver the spawn diagnosis uses (NF-2026-01936), one definition;
    # imported only once a flutter/dart head is seen.
    try:
        from .validation_spawn_diagnosis import _resolve
    except ImportError:  # direct-script entrypoint
        from validation_spawn_diagnosis import _resolve  # type: ignore[no-redef]

    env = os.environ if environment is None else environment
    path = _env_value(env, "PATH")
    pathext = _env_value(env, "PATHEXT") or DEFAULT_PATHEXT
    roots: list[ToolchainRoot] = []
    seen: set[str] = set()
    for head in heads:
        found = _resolve(head, path, pathext)
        if found is None:
            continue
        toolchain = toolchain_root_for(Path(os.path.realpath(found)))
        if toolchain is None:
            continue
        key = os.path.normcase(os.path.normpath(toolchain.root))
        if key not in seen:
            seen.add(key)
            roots.append(toolchain)
    return roots


def plan_toolchain_grants(
    argv: Sequence[str],
    environment: Mapping[str, str] | None,
    *,
    covered: Sequence[str] = (),
) -> list[ToolchainGrant]:
    """Persistent read/execute on each SDK root ``argv`` launches.

    A root inside ``covered`` (what the request already grants) is already
    reachable and is skipped; one in a system tree is omitted, as for
    :func:`windows_appcontainer.python_read_grants`.
    """
    writable = [os.path.normcase(os.path.normpath(path)) for path in covered]
    planned: list[ToolchainGrant] = []
    for toolchain in launch_toolchain_roots(argv, environment):
        key = os.path.normcase(os.path.normpath(toolchain.root))
        if any(_within(key, other) for other in writable):
            continue
        for grant in outside_system_trees(
            [ContainerGrant(toolchain.root, "read_execute", persistent=True)]
        ):
            planned.append(ToolchainGrant(toolchain, grant))
    return planned


def grant_failure_detail(toolchain: ToolchainRoot, cause: object) -> str:
    """The refusal text for a toolchain root the container could not be granted."""
    return (
        f"toolchain_grant_failed: {toolchain.describe()} could not be granted "
        f"read_execute for the AppContainer: {cause}"
    )
