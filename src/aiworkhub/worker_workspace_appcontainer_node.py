"""AppContainer node validation argv rewrite (NF-2026-01009).

Split out of ``worker_workspace.py`` to keep that module under the module-size
ratchet; behaviour is unchanged.  ``worker_workspace`` re-imports the names its
AppContainer validation runner uses.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Iterable


# NF-2026-01009: node grew ``--experimental-test-isolation`` in v22.8.0 and
# renamed it ``--test-isolation`` in v23.6.0.  Below 22.8 neither spelling
# exists, and offering the wrong one makes node exit on a bad option -- a worse
# failure than the hang this rewrite removes -- so those builds get no flag.
_APPCONTAINER_NODE_EXPERIMENTAL_ISOLATION_RELEASE = (22, 8)
_APPCONTAINER_NODE_STABLE_ISOLATION_RELEASE = (23, 6)
_APPCONTAINER_NODE_PRESERVE_SYMLINK_FLAGS = (
    "--preserve-symlinks",
    "--preserve-symlinks-main",
)
_APPCONTAINER_NODE_ISOLATION_FLAGS = (
    "--test-isolation",
    "--experimental-test-isolation",
)
_APPCONTAINER_NODE_VERSION_PROBE_SECONDS = 10
_APPCONTAINER_NODE_VERSION_CACHE_MAX_ENTRIES = 32
_APPCONTAINER_NODE_VERSION_CACHE: dict[str, str] = {}
_APPCONTAINER_NODE_VERSION_LOCK = threading.Lock()


def _is_appcontainer_node_executable(executable: str) -> bool:
    """A bare ``node``/``node.exe`` -- the argv[0] the node rewrite serves."""
    return Path(executable).name.lower() in {"node", "node.exe"}


def _appcontainer_node_isolation_flag(node_version: str) -> str:
    """The ``=none`` isolation flag ``node_version`` accepts, else ``""``.

    ``node_version`` is whatever the probe read (``v22.16.0``); anything that
    does not parse as ``<major>.<minor>`` is an unknown version, which is the
    same answer as too old: no flag.
    """
    release = re.match(r"v?(\d+)\.(\d+)", node_version.strip())
    if release is None:
        return ""
    measured = (int(release.group(1)), int(release.group(2)))
    if measured >= _APPCONTAINER_NODE_STABLE_ISOLATION_RELEASE:
        return "--test-isolation=none"
    if measured >= _APPCONTAINER_NODE_EXPERIMENTAL_ISOLATION_RELEASE:
        return "--experimental-test-isolation=none"
    return ""


def _appcontainer_node_version(executable: str) -> str:
    """``<node> --version``, once per resolved executable, failure-tolerant.

    Which isolation flag exists is a property of the installed node build, so
    the rewrite has to ask.  ``--version`` on a host-installed binary runs no
    candidate code, needs no shell and no repository cwd, so one bounded call
    is the honest reading -- and the answer is only ever an improvement, so
    every failure returns the empty fact, which
    :func:`_appcontainer_node_validation_argv` reads as "leave isolation
    alone".

    A failed probe is cached exactly like a successful one: a card may declare
    several node commands, and re-running a probe that already timed out would
    spend ``_APPCONTAINER_NODE_VERSION_PROBE_SECONDS`` again per command.
    """
    try:
        resolved = str(Path(executable).resolve(strict=True))
    except OSError:
        return ""
    key = os.path.normcase(resolved)
    with _APPCONTAINER_NODE_VERSION_LOCK:
        cached = _APPCONTAINER_NODE_VERSION_CACHE.get(key)
    if cached is not None:
        return cached
    version = ""
    try:
        probe = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=_APPCONTAINER_NODE_VERSION_PROBE_SECONDS,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        version = ""
    else:
        reported = (probe.stdout or "").strip().splitlines()
        if probe.returncode == 0 and reported:
            version = reported[0].strip()[:64]
    with _APPCONTAINER_NODE_VERSION_LOCK:
        while (
            len(_APPCONTAINER_NODE_VERSION_CACHE)
            >= _APPCONTAINER_NODE_VERSION_CACHE_MAX_ENTRIES
        ):
            _APPCONTAINER_NODE_VERSION_CACHE.pop(
                next(iter(_APPCONTAINER_NODE_VERSION_CACHE))
            )
        _APPCONTAINER_NODE_VERSION_CACHE[key] = version
    return version


def _appcontainer_node_validation_argv(
    argv: Iterable[str], node_version: str
) -> list[str]:
    r"""The argv a node validation can actually pass inside the AppContainer.

    NF-2026-01009, both halves measured through ``launch_appcontainer`` with no
    capability SIDs:

    * node's ESM/CJS resolver calls ``realpathSync`` on its entry point, which
      ``lstat``s every ancestor and fails ``EPERM lstat 'C:\Users'`` because the
      Windows worktree root lives under ``%TEMP%``.  ``--preserve-symlinks``
      and ``--preserve-symlinks-main`` skip that walk, so both go in for every
      node command, not just ``--test``.
    * ``node --test`` defaults to one child process per file with piped stdio,
      and libuv's ``uv__pipe_server`` reads ``ERROR_ACCESS_DENIED`` from
      ``CreateNamedPipe(\\.\pipe\uv\...)`` as a name collision it retries
      forever: 100% CPU until the validation timeout.  In-process isolation
      spawns no child, and measured 50/50 in 0.2 s on v22.16.0.

    Pure, so the whole rewrite is provable off Windows.  Every insertion is
    idempotent, an explicit isolation flag on the declared command wins (a card
    that asked for child isolation keeps it, hang included), and an argv that
    is not a node argv comes back unchanged.
    """
    rewritten = [str(part) for part in argv]
    if not rewritten or not _is_appcontainer_node_executable(rewritten[0]):
        return rewritten
    for flag in reversed(_APPCONTAINER_NODE_PRESERVE_SYMLINK_FLAGS):
        if flag not in rewritten[1:]:
            rewritten.insert(1, flag)
    if "--test" not in rewritten[1:]:
        return rewritten
    if any(
        part == flag or part.startswith(f"{flag}=")
        for part in rewritten[1:]
        for flag in _APPCONTAINER_NODE_ISOLATION_FLAGS
    ):
        return rewritten
    isolation = _appcontainer_node_isolation_flag(node_version)
    if isolation:
        # Directly after ``--test``, the form measured working on v22.16.0 --
        # and never appended, where node would read it as one more test file
        # pattern rather than an option.
        rewritten.insert(rewritten.index("--test", 1) + 1, isolation)
    return rewritten
