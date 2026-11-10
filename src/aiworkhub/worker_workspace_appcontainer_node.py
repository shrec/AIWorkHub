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
import time
from pathlib import Path
from typing import Any, Callable, Iterable


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
_APPCONTAINER_NODE_TEST_FILE_SUFFIXES = (
    ".js",
    ".cjs",
    ".mjs",
    ".ts",
    ".cts",
    ".mts",
)
_APPCONTAINER_NODE_GLOB_METACHARACTERS = frozenset("*?[]{}()!")
_APPCONTAINER_NODE_VALUE_FLAG = re.compile(r"--[A-Za-z0-9_][A-Za-z0-9_-]*=.+")


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


def _appcontainer_node_declares_isolation(parts: Iterable[str]) -> bool:
    """Whether ``parts`` already pick a test isolation, in either spelling."""
    return any(
        part == flag or part.startswith(f"{flag}=")
        for part in parts
        for flag in _APPCONTAINER_NODE_ISOLATION_FLAGS
    )


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

    This is the node counterpart of the Python adaptation in
    ``_run_appcontainer_validation``.  It rewrites argv rather than env, because
    what a node validation needs inside the container is different flags, not a
    shim on the import path.  The version probe is the only host call this
    adds, and every other command still reaches ``launch_appcontainer`` with the
    argv the card declared, byte for byte.

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
    if _appcontainer_node_declares_isolation(rewritten[1:]):
        return rewritten
    isolation = _appcontainer_node_isolation_flag(node_version)
    if isolation:
        # Directly after ``--test``, the form measured working on v22.16.0 --
        # and never appended, where node would read it as one more test file
        # pattern rather than an option.
        rewritten.insert(rewritten.index("--test", 1) + 1, isolation)
    return rewritten


def _is_appcontainer_node_test_file(part: str) -> bool:
    """A literal test-file positional: known suffix, no glob, not an option."""
    return (
        not part.startswith("-")
        and part.endswith(_APPCONTAINER_NODE_TEST_FILE_SUFFIXES)
        and _APPCONTAINER_NODE_GLOB_METACHARACTERS.isdisjoint(part)
    )


def _appcontainer_node_per_file_argvs(
    argv: Iterable[str], node_version: str
) -> list[list[str]] | None:
    """One argv per test file when the row must fan out, else ``None``.

    Only a row the lane would run in-process can need this: with an explicit
    isolation flag, or a node that gets none, today's argv keeps its meaning.

    Fail closed on shape.  An argument that is not exactly ``--test``, a
    ``--name=value`` flag or a literal test file might consume the next one
    (``--require x.js``) or expand to any number of files (``dir/*.test.js``),
    so the row is left alone rather than guessed at.  ``--test`` after the
    first file is a script argument to node, not the test runner's flag.

    Node stops reading its own options at the first positional, so each file
    takes the first file's slot and every flag stays on its declared side.
    """
    parts = [str(part) for part in argv]
    if (
        not parts
        or not _is_appcontainer_node_executable(parts[0])
        or not _appcontainer_node_isolation_flag(node_version)
        or _appcontainer_node_declares_isolation(parts[1:])
    ):
        return None
    files: list[str] = []
    for part in parts[1:]:
        if _is_appcontainer_node_test_file(part):
            files.append(part)
        elif part != "--test" and not _APPCONTAINER_NODE_VALUE_FLAG.fullmatch(part):
            return None
    if len(files) < 2:
        return None
    first_file = parts.index(files[0], 1)
    if "--test" not in parts[1:first_file]:
        return None
    trailing = [
        part
        for part in parts[first_file + 1 :]
        if not _is_appcontainer_node_test_file(part)
    ]
    return [[*parts[:first_file], file, *trailing] for file in files]


def _run_appcontainer_node_row_per_file(
    run_one: Callable[..., subprocess.CompletedProcess[str]],
    argv: Iterable[str],
    node_version: str,
    *,
    timeout_seconds: float,
    **run_kwargs: Any,
) -> subprocess.CompletedProcess[str] | None:
    """Run a multi-file ``node --test`` row one launch per file, else ``None``.

    The lane's ``--test-isolation=none`` puts every file of a row in one
    process, so a file that stubs ``vscode`` through ``Module._load`` collides
    with a cached ``extension.js`` and one ``process.exit(1)`` silently ends
    the rest (measured on v24.21.0: host rc 0 with 103 tests; lane rc 1 with no
    stdout; each of the 8 files alone rc 0).  One launch per file is the host's
    one process per file.

    Every file runs even after one fails, because stopping early would change
    what the row means on the host.  The fold answers for the declared row:
    first non-zero returncode, streams concatenated in declared order, and one
    deadline shared by every launch.  ``OSError`` propagates untouched.
    """
    per_file = _appcontainer_node_per_file_argvs(argv, node_version)
    if per_file is None:
        return None
    declared = [str(part) for part in argv]
    stdout: list[str] = []
    stderr: list[str] = []

    def _row_timeout() -> subprocess.TimeoutExpired:
        return subprocess.TimeoutExpired(
            declared,
            timeout_seconds,
            output="".join(stdout),
            stderr="".join(stderr),
        )

    returncode = 0
    deadline = time.monotonic() + timeout_seconds
    for file_argv in per_file:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _row_timeout()
        try:
            completed = run_one(file_argv, timeout_seconds=remaining, **run_kwargs)
        except subprocess.TimeoutExpired as exc:
            stdout.append(exc.stdout or "")
            stderr.append(exc.stderr or "")
            raise _row_timeout() from exc
        stdout.append(completed.stdout or "")
        stderr.append(completed.stderr or "")
        returncode = returncode or completed.returncode
    return subprocess.CompletedProcess(
        declared, returncode, "".join(stdout), "".join(stderr)
    )
