#!/usr/bin/env python3
"""Cut one AIWorkHub release as a single deterministic, ordered run.

    python scripts/release_cut.py VERSION [--dry-run] [--push] [--skip-wsl] [--full-suite]
        [--wsl-baseline PATH] [--wsl-python PATH] [--allow-dirty PATH ...]

Write the release documents first: CHANGELOG.md, vscode-extension/CHANGELOG.md
and both READMEs name VERSION, and that prose is the one manual part. This
script does the rest of the release procedure in order, stops at the first
failing step with a one-line reason, and ends with one JSON summary line on
stdout. A failure after the bump prints one recovery line before it, naming
what the cut already changed, which a rerun must restore first. Each line is
flushed as it is printed: with stdout redirected to a file, the file names the
step that runs.

Preflight, read-only (``--dry-run`` prints the plan and runs only these):

  version         VERSION is above the canonical version and follows the
                  rollover x.y.99 -> x.(y+1).0, never x.y.100
  changelog       CHANGELOG.md has a non-empty [VERSION] section and the
                  extension CHANGELOG a "## VERSION —" one
  readme          README.md and vscode-extension/README.md each carry exactly
                  one "## What's new in" section, and it names VERSION
  worktree        only the release documents, the release ledger and
                  --allow-dirty paths are dirty; generated files are clean;
                  an --allow-dirty prefix over what the VSIX is built from
                  (vscode-extension/, src/aiworkhub/) is refused before any
                  step runs
  branch          HEAD is main
  tag_free        tag vVERSION does not exist yet
  wsl             a Windows host whose WSL interpreter (python3, or the absolute
                  POSIX path --wsl-python names) imports pytest, xdist and
                  pytest_timeout, whose wslpath -a names .scratch/release/VERSION
                  under /mnt/, and a readable --wsl-baseline

The cut:

  bump            release_metadata.bump: the canonical version and projections
  metadata_check  release_metadata.check --tag vVERSION
  host_tests      the suite on this host, xdist workers derived from
                  os.cpu_count() with headroom; every run here and in
                  wsl_tests passes --timeout=PYTEST_TIMEOUT_SECONDS
                  (pytest-timeout), so a stuck test fails with its stack
                  instead of hanging the cut; a run with no new output and,
                  on this host, no basetemp change for STALL_BUDGET_SECONDS
                  has its process tree ended and fails the step, naming the
                  basetemp entries touched last; up to LOAD_RERUN_MAX_NODES
                  failed node ids get one serial rerun, and only a node that
                  fails again stops the cut. It runs the test-impact selection
                  (aiworkhub.test_impact_select) from the last v* tag on HEAD's
                  first parent to the working tree, plus the invariant tests
                  and every changed test file, when the recorded map is fresh
                  for every changed path; otherwise, or with --full-suite, a
                  full_suite_required token in the CHANGELOG [VERSION] notes,
                  no previous tag, or a selector that raises or answers
                  full_suite_required, the whole tests directory. wsl_tests
                  runs the same targets; the JSON summary's test_selection
                  names the lane, the base tag, the count and the fallback
                  reason. CI on push still runs the full suite.
  package         npm --prefix vscode-extension run package
  commit          git commit "release: VERSION" of exactly the release files
  wsl_tests       a git archive of HEAD extracted under .scratch/release/VERSION
                  and run in WSL with MSYS_NO_PATHCONV=1, only after the host
                  suite has finished; WSL names that directory by its
                  wslpath -a path, which must lie under /mnt/, and every WSL
                  command takes absolute paths behind a cd guard; a failing
                  node id absent from --wsl-baseline stops the cut, unless it
                  passes the same one serial rerun as host_tests; before the
                  suite, git init, add -A and commit (identity given with -c)
                  make the extracted tree a one-commit repository, which the
                  tests that run git against the checkout need, and a failing
                  git command stops the cut with its stderr
  receipt         scripts/release_receipt.py record
  tag             git tag vVERSION
  push            only with --push: main and the tag in one atomic push,
                  never --force

Every subprocess goes through one injectable runner (``Host.runner``), so the
tests never run npm, WSL or git push. The version, changelog and README rules
belong to scripts/release_metadata.py and the receipt to
scripts/release_receipt.py; this script orders them and adds no second copy.
It writes nothing outside the repository: the archive, the WSL tree, the host
suite's basetemp and the step logs live under .scratch/release/VERSION. Inside
WSL, pytest's basetemp is /tmp/aiworkhub-release-VERSION on the Linux
filesystem, which the POSIX suite needs for sockets and file modes; the cut
removes it after the run, pass or fail. Two steps stay outside the cut:
installing the VSIX (``code --install-extension``) writes to the editor's own
extension directory, and ``release_receipt.py confirm`` needs the version the
installed extension's server reports after a reload.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import importlib.util
import io
import itertools
import json
import math
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import ModuleType
from typing import IO, Any, Callable, Mapping, Protocol, Sequence


def _load_sibling(name: str, directory: Path | None = None) -> ModuleType:
    """Load a module by path, as scripts/ is not a package: from ``directory``, else scripts/."""

    folder = Path(__file__).resolve().parent if directory is None else directory
    path = folder / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"aiworkhub_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_metadata = _load_sibling("release_metadata")


def _impact_modules() -> tuple[ModuleType, ModuleType]:
    """``aiworkhub.accept_neighbour_tests`` and ``aiworkhub.test_impact_select``, in-process.

    Imported only when a cut decides its test lane, from this repository's src/ when
    the interpreter has no aiworkhub of its own; an import failure is the caller's
    full-suite fallback.
    """

    try:
        import aiworkhub  # noqa: F401
    except ModuleNotFoundError:
        sys.path.insert(0, str(REPO_ROOT / "src"))
    import aiworkhub.accept_neighbour_tests as neighbours
    import aiworkhub.test_impact_select as impact

    return neighbours, impact


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ID = "aiworkhub.release_cut.v1"
MAIN_BRANCH = "main"
REMOTE = "origin"
EXTENSION_DIR = "vscode-extension"
# What `npm run package` builds the VSIX from: the extension's own files and the
# Python runtime it bundles (vscode-extension/test/package-vsix.js, PY_RUNTIME_SRC;
# tests/test_release_cut.py asserts the identity).
PACKAGED_ROOTS: tuple[str, ...] = (EXTENSION_DIR, "src/aiworkhub")
RECEIPT_SCRIPT = "scripts/release_receipt.py"
# The tracked ledger scripts/release_receipt.py appends to. Its owner is
# aiworkhub.sdlc_deploy_proof.LEDGER_REL (tests/test_release_cut.py asserts the
# identity). A cut leaves its own "built" line uncommitted: the next cut's
# release commit carries it.
RELEASE_LEDGER = ".aiworkhub/releases.jsonl"
# The WSL interpreter unless --wsl-python names another: python3 or an absolute POSIX path.
WSL_PYTHON = "python3"
# What the wsl preflight makes that interpreter import before the cut changes anything:
# its suite runs with -n (xdist) and --timeout (pytest_timeout).
WSL_SUITE_MODULES: tuple[str, ...] = ("pytest", "xdist", "pytest_timeout")
WSL_IMPORT_PROBE = f"import {', '.join(WSL_SUITE_MODULES)}"
# wslpath -a must place the scratch directory on a mounted Windows drive.
WSL_MOUNT_ROOT = "/mnt/"
# What a guarded WSL command exits with when WSL cannot enter its directory.
WSL_CD_FAILED = 97
# pytest's basetemp inside WSL, on the Linux filesystem; removed after the run.
WSL_BASETEMP_PREFIX = "/tmp/aiworkhub-release-"
# Run in order in the extracted WSL tree before its suite: ``git archive`` carries no
# .git, and the suite's tests that run git against the checkout need a repository. The
# identity rides on -c, so WSL needs no git config of its own.
WSL_TREE_GIT_IDENTITY = ("-c", "user.name=release", "-c", "user.email=release@localhost")
WSL_TREE_GIT_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("git", "init", "-q"),
    ("git", *WSL_TREE_GIT_IDENTITY, "add", "-A"),
    ("git", *WSL_TREE_GIT_IDENTITY, "commit", "-q", "-m", "release-tree"),
)
# parallelism_fallback in .aiworkhub/config/development_rules.json: at least a
# quarter of the cores stays free for the MCP server and editor on this host.
HOST_TEST_HEADROOM_FRACTION = 0.25
GIT_TIMEOUT_SECONDS = 120.0
LONG_GIT_TIMEOUT_SECONDS = 900.0
# pytest-timeout's per-test limit, which every host and WSL suite run passes, the
# serial reruns included. The 0.13.3 cut sat ~55 min in host_tests on a test that
# never ended; with the limit such a test fails instead of hanging the cut. Under
# xdist on Windows pytest-timeout ends the worker, so the log names the node as
# crashed and the thread dump stays in that worker's stderr, not in the cut log.
# The same suite run with --timeout=420
# finished in 26.5 min; 600 s keeps headroom for a slow test under full xdist load.
PYTEST_TIMEOUT_SECONDS = 600
# NF-2026-02312: on 2026-11-17 host_tests stopped advancing at 06:29 (no new basetemp
# entry across 12 xdist workers) and the cut waited until it was cancelled at 08:08;
# --timeout=600 did not end it. A suite run without progress, no new output and no
# basetemp change, for this long is ended with its process tree (_run_watched). Twice
# the per-test limit: a legitimately slow test holds its worker at most
# PYTEST_TIMEOUT_SECONDS, and its result is printed when it ends.
STALL_BUDGET_SECONDS = 2.0 * PYTEST_TIMEOUT_SECONDS
# Seconds between two looks of the watchdog, so watching costs no CPU worth measuring.
STALL_POLL_SECONDS = 10.0
# The most entries one look lists under a basetemp, which bounds a look on any suite.
STALL_SCAN_MAX_ENTRIES = 20_000
# How long a finished or ended suite run's output pipes may take to close.
PIPE_DRAIN_SECONDS = 10.0
PIPE_READ_BYTES = 65_536
# The pytest option every suite run passes, by which run_subprocess tells one apart.
BASETEMP_OPTION = "--basetemp="
MAX_BASELINE_BYTES = 1024 * 1024
MAX_BASELINE_IDS = 10_000
MAX_LINE_CHARS = 600
MAX_LISTED = 5
# More failures than this is a real break, not load, and is not rerun.
LOAD_RERUN_MAX_NODES = 25
SHORT_SUMMARY_HEADER = "short test summary info"
FAILURE_PREFIXES = ("FAILED ", "ERROR ")
# NF-2026-02220 part C: both suites run the test-impact selection since the last
# release tag (lane impact_map) or, on any fallback, the whole tests directory.
FULL_SUITE_TARGET = "tests"
LANE_IMPACT_MAP = "impact_map"
LANE_FULL_SUITE = "full_suite"
# The token in the CHANGELOG [VERSION] notes that makes a release run everything.
FULL_SUITE_MARKER = "full_suite_required"

PREFLIGHT_STEPS: tuple[str, ...] = (
    "version",
    "changelog",
    "readme",
    "worktree",
    "branch",
    "tag_free",
    "wsl",
)
CUT_STEPS: tuple[str, ...] = (
    "bump",
    "metadata_check",
    "host_tests",
    "package",
    "commit",
    "wsl_tests",
    "receipt",
    "tag",
    "push",
)
STEPS: tuple[str, ...] = PREFLIGHT_STEPS + CUT_STEPS


@dataclass(frozen=True)
class Completed:
    """One finished command: its exit code and decoded output."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def output(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part)


class Runner(Protocol):
    """Runs one argv list, never a shell string, to completion."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Completed: ...


def _is_wsl(program: str) -> bool:
    """``wsl``, ``wsl.exe`` or a path to it; PureWindowsPath splits on both separators."""

    return PureWindowsPath(program).stem.casefold() == "wsl"


def _decode(raw: bytes | None, *, wsl: bool) -> str:
    """Captured output as UTF-8 with every byte kept, NULs included.

    ``git status -z`` separates its records with NULs, so they must survive.
    Only wsl.exe output drops them: wsl.exe writes its own errors in UTF-16LE,
    and without the NULs those stay readable. No WSL command of the cut
    prints a NUL-separated record.
    """

    if not raw:
        return ""
    return (raw.replace(b"\x00", b"") if wsl else raw).decode("utf-8", "replace")


def run_subprocess(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> Completed:
    """The real runner: an argv list, no shell, no stdin, output captured.

    A suite run, the one command that hands pytest a basetemp (_pytest_basetemp), is
    watched for progress and ended with its whole process tree once it stalls
    (_run_watched); every other command runs as it always did.
    """

    command = list(argv)
    wsl = _is_wsl(command[0])
    basetemp = _pytest_basetemp(command)
    # PATHEXT lookup: on Windows ``npm`` is npm.cmd, which a bare name misses.
    command[0] = shutil.which(command[0]) or command[0]
    try:
        if basetemp is not None:
            return _run_watched(
                command, cwd=cwd, env=env, timeout=timeout, wsl=wsl, basetemp=basetemp
            )
        done = subprocess.run(
            command,
            cwd=cwd,
            env=None if env is None else dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = f"{_decode(exc.stderr, wsl=wsl)}\ntimed out after {timeout} s"
        return Completed(-1, _decode(exc.stdout, wsl=wsl), stderr)
    except OSError as exc:
        return Completed(-1, "", f"cannot run {argv[0]}: {exc}")
    stdout, stderr = _decode(done.stdout, wsl=wsl), _decode(done.stderr, wsl=wsl)
    return Completed(done.returncode, stdout, stderr)


def _pytest_basetemp(argv: Sequence[str]) -> str | None:
    """The basetemp a suite run hands pytest, which makes run_subprocess watch the run.

    A host suite run passes it as one argv token (_host_test_argv), a WSL one as a token
    of the ``sh -c`` script _wsl_in wraps _wsl_test_command in. No other command of the
    cut passes it, and for those this answers None.
    """

    tokens = list(argv)
    if tokens and _is_wsl(tokens[0]) and "-c" in tokens[:-1]:
        try:
            tokens = shlex.split(tokens[-1])
        except ValueError:  # not a script _wsl_guarded_script built
            return None
    found = [
        token[len(BASETEMP_OPTION) :] for token in tokens if token.startswith(BASETEMP_OPTION)
    ]
    return found[-1] if found else None


def _run_watched(
    command: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
    timeout: float | None,
    wsl: bool,
    basetemp: str,
) -> Completed:
    """One suite run to its end, unless it stops making progress (NF-2026-02312).

    Progress is captured output that grew or, on this host, a change of the newest mtime
    at most two levels under the run's basetemp (_basetemp_scan); a WSL run's basetemp
    lies on the Linux filesystem, so there the output alone tells. A run with neither
    for STALL_BUDGET_SECONDS has its whole process tree ended (_ProcessTree) and answers
    exit -1 with a last stderr line naming how long it stalled and the basetemp entries
    touched last, which name the tests still running. Past ``timeout`` it ends the same
    way and answers as the plain runner does. A run that ends by itself answers exactly
    what it exited with and printed, and what it left running is ended.
    """

    watched = None if wsl else cwd / basetemp
    drains: list[_Drain] = []
    verdict: tuple[str, float] | None = None
    with subprocess.Popen(
        command,
        cwd=cwd,
        env=None if env is None else dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,  # POSIX: the child leads a process group; unused on Windows
    ) as proc:
        tree = _ProcessTree(proc)
        try:
            drains.append(_Drain(proc.stdout))
            drains.append(_Drain(proc.stderr))
            verdict = _watch(proc, drains, watched, timeout)
        finally:
            tree.end()
            for name, drain in zip(("stdout", "stderr"), drains):
                if not drain.finish(PIPE_DRAIN_SECONDS):
                    # Something the tree did not reach still holds this pipe, and closing
                    # it under the blocked read could hang, so the read keeps the stream.
                    setattr(proc, name, None)
        returncode = proc.wait()
    out, err = drains[0].data, drains[1].data
    if verdict is None:
        return Completed(returncode, _decode(out, wsl=wsl), _decode(err, wsl=wsl))
    kind, seconds = verdict
    if kind == "timeout":
        raise subprocess.TimeoutExpired(command, seconds, output=out, stderr=err)
    line = _stall_line(seconds, watched, basetemp, tree.reach)
    return Completed(-1, _decode(out, wsl=wsl), f"{_decode(err, wsl=wsl)}\n{line}")


def _watch(
    proc: subprocess.Popen[bytes],
    drains: Sequence[_Drain],
    watched: Path | None,
    timeout: float | None,
) -> tuple[str, float] | None:
    """Wait for ``proc``: None once it exits, else why the runner has to end it.

    ``("timeout", timeout)`` once ``timeout`` passed, ``("stall", seconds)`` once neither
    the output nor the newest basetemp mtime changed for STALL_BUDGET_SECONDS. One look
    comes every STALL_POLL_SECONDS, and between looks the wait blocks.
    """

    started = last = time.monotonic()
    seen = _progress(drains, watched)
    while True:
        wait = STALL_POLL_SECONDS
        if timeout is not None:
            wait = max(0.0, min(wait, started + timeout - time.monotonic()))
        try:
            proc.wait(timeout=wait)
            return None
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        if timeout is not None and now - started >= timeout:
            return "timeout", timeout
        mark = _progress(drains, watched)
        if mark != seen:
            seen, last = mark, now
        elif now - last >= STALL_BUDGET_SECONDS:
            return "stall", now - last


def _progress(drains: Sequence[_Drain], watched: Path | None) -> tuple[int, int | None]:
    """What one look compares: the output bytes so far and the newest basetemp mtime."""

    newest = None if watched is None else _basetemp_scan(watched)[0]
    return sum(drain.size for drain in drains), newest


def _stall_line(seconds: float, watched: Path | None, basetemp: str, reach: str) -> str:
    """A stalled suite run's last stderr line: how long, what was ended, what ran last."""

    if watched is None:
        return (
            f"stalled {seconds:.0f} s (no new output; the WSL basetemp {basetemp} is not "
            f"visible from this host), {reach}"
        )
    touched = _basetemp_scan(watched)[1]
    return (
        f"stalled {seconds:.0f} s (no new output, no basetemp change), {reach}; "
        f"last touched: {_listing(touched) if touched else 'nothing under the basetemp'}"
    )


def _listed(directory: str | Path, limit: int) -> list[os.DirEntry[str]]:
    """At most ``limit`` entries of ``directory``; none when it is absent or unreadable."""

    try:
        with os.scandir(directory) as entries:
            return list(itertools.islice(entries, max(0, limit)))
    except OSError:
        return []


def _mtime_ns(entry: os.DirEntry[str]) -> int | None:
    try:
        return entry.stat(follow_symlinks=False).st_mtime_ns
    except OSError:
        return None


def _basetemp_scan(basetemp: Path) -> tuple[int | None, list[str]]:
    """The newest mtime (ns) at most two levels under ``basetemp``, and what ran last.

    pytest names a test's directory after the test: ``test_x0`` in a serial run,
    ``popen-gwN/test_x0`` under xdist. So each top-level entry stands for its newest
    child, or for itself when it has none, and the second answer lists those newest
    first: under xdist, every worker's latest test. Two levels and
    STALL_SCAN_MAX_ENTRIES bound one look, and an absent or unreadable entry adds
    nothing, so it never counts as progress.
    """

    try:
        newest = basetemp.stat().st_mtime_ns
    except OSError:
        return None, []
    left = STALL_SCAN_MAX_ENTRIES
    picks: list[tuple[int, str]] = []
    tops = _listed(basetemp, left)
    left -= len(tops)
    for top in tops:
        stamp = _mtime_ns(top)
        if stamp is None:
            continue
        try:
            # is_junction: a Windows junction is no symlink to is_dir, and leads elsewhere.
            nested = left > 0 and top.is_dir(follow_symlinks=False) and not top.is_junction()
        except OSError:
            nested = False
        pick: tuple[int, str] | None = None
        if nested:
            children = _listed(top.path, left)
            left -= len(children)
            for child in children:
                child_stamp = _mtime_ns(child)
                if child_stamp is not None:
                    candidate = (child_stamp, f"{top.name}/{child.name}")
                    pick = candidate if pick is None else max(pick, candidate)
        if pick is None:
            pick = (stamp, top.name)
        newest = max(newest, stamp, pick[0])
        picks.append(pick)
    picks.sort(key=lambda item: (-item[0], item[1]))
    return newest, [name for _stamp, name in picks]


class _Drain:
    """One output pipe of a suite run, read to its end on a daemon thread.

    ``size`` counts the bytes read so far, the output half of the watchdog's progress.
    The thread reads the descriptor itself, never through the stream's buffer, whose
    lock a read that a surviving writer blocks would hold against any close.
    """

    def __init__(self, stream: IO[bytes] | None) -> None:
        self._chunks: list[bytes] = []
        self.size = 0
        self._thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self._thread.start()

    def _read(self, stream: IO[bytes] | None) -> None:
        if stream is None:
            return
        with contextlib.suppress(OSError, ValueError):
            descriptor = stream.fileno()
            while chunk := os.read(descriptor, PIPE_READ_BYTES):
                self._chunks.append(chunk)
                self.size += len(chunk)

    def finish(self, grace: float) -> bool:
        """Wait up to ``grace`` s for the pipe to close; False while something holds it."""

        self._thread.join(grace)
        return not self._thread.is_alive()

    @property
    def data(self) -> bytes:
        return b"".join(self._chunks)


def _print_flushed(line: str) -> None:
    """Print one line on stdout and flush it at once; every line the cut prints comes here.

    Redirected to a file, stdout is block-buffered: the 0.13.3 cut's log stayed
    empty for the ~55 min it hung in host_tests, so nobody could tell which step
    it was in before it was killed. Flushed, the log names the step that runs.
    """

    print(line, flush=True)


def _kill_on_close_job(process: Callable[[Any], Any]) -> tuple[Any, str]:
    """A fresh kill-on-close Job Object holding the process ``process(kernel32)`` answers.

    Answers ``(job, "")``, else ``(None, "STEP failed (GetLastError N)")`` with the job
    closed again; nothing here raises. Every child and grandchild a member starts later
    joins the job as well, and closing its last handle makes the kernel end them all.
    ``process`` is asked only once the job is ready. Windows only: callers check
    ``os.name`` first. The cut's one job mechanism (NF-2026-02208): the cut binds
    itself with it, and the stall watchdog binds each suite run (_ProcessTree).
    """

    kernel32 = ctypes.windll.kernel32
    step, job, detail = "load", None, ""
    try:
        import ctypes.wintypes as wintypes

        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        layouts = _load_sibling("windows_job_structures", REPO_ROOT / "src" / "aiworkhub")
        info = layouts.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        step = "create"
        job = kernel32.CreateJobObjectW(None, None)
        if job:
            step = "set information"
            # Class 9 is JobObjectExtendedLimitInformation.
            if kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                step = "assign"
                if kernel32.AssignProcessToJobObject(job, process(kernel32)):
                    return job, ""
    except Exception as exc:  # whatever the load or a kernel call throws, the caller goes on
        detail = f", {type(exc).__name__}"
    code = kernel32.GetLastError()  # read before CloseHandle overwrites it
    if job:
        kernel32.CloseHandle(job)
    return None, f"{step} failed (GetLastError {code}{detail})"


def _bind_children_to_this_process() -> bool:
    """Enter a fresh kill-on-close Job Object, so the kernel ends the cut's tree with it.

    Every later child and grandchild inherits the job, whatever ends this process.
    The cut binds itself, not each child: the interpreter can already sit inside a
    launcher job that lets its children break away silently, and only an innermost
    job without breakaway keeps them. On success the job handle stays open until the
    process ends, because closing it would end the cut. A failed step prints one
    line and the cut goes on unbound; nothing here raises.

    The WSL side was measured to need no code: killing wsl.exe ended its whole Linux
    session within one second, so the job that ends wsl.exe ends the Linux side.
    A POSIX host is not covered: there this returns False and binds nothing.
    """

    if os.name != "nt":
        return False
    job, failure = _kill_on_close_job(lambda kernel32: kernel32.GetCurrentProcess())
    if job:
        return True
    _print_flushed(
        f"warning child_lifetime: {failure}, "
        "so a stopped cut can leave its child processes behind"
    )
    return False


class _ProcessTree:
    """A suite run's child and every process it starts, ended together by ``end``.

    On Windows the child joins a kill-on-close Job Object of its own, the NF-2026-02208
    mechanism (_kill_on_close_job), as soon as it starts, long before pytest starts a
    worker. That job nests inside the cut's, so closing it ends the child, its xdist
    workers and, through wsl.exe, the Linux side while the cut goes on. On POSIX, where
    NF-2026-02208 binds nothing, start_new_session made the child lead a process group,
    which one SIGKILL ends. ``reach`` names what ``end`` ends, a failed bind included;
    ``end`` never raises.
    """

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        self._job: Any = None
        self.reach = "process tree ended"
        if os.name == "nt":
            # Popen's own handle from CreateProcess: full access, and it pins the pid.
            handle = getattr(proc, "_handle", None)
            self._job, failure = _kill_on_close_job(lambda _kernel32: handle)
            if not self._job:
                self.reach = f"process ended, not its unbound tree ({failure})"

    def end(self) -> None:
        """End what still runs: a stalled tree, or whatever a finished run left behind."""

        if self._job:
            job, self._job = self._job, None
            # The job's only handle: closing it makes the kernel end every process in it.
            with contextlib.suppress(Exception):
                ctypes.windll.kernel32.CloseHandle(job)
        elif sys.platform != "win32" and self._proc.returncode is None:
            # Only an unreaped leader pins the group id; after a reap it could be reused.
            with contextlib.suppress(OSError):
                os.killpg(self._proc.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            self._proc.kill()


@dataclass(frozen=True)
class Host:
    """What a cut touches beyond its own logic; the tests inject every part."""

    root: Path
    runner: Runner
    python: str
    cpu_count: int | None
    env: Mapping[str, str]
    is_windows: bool
    out: Callable[[str], None]
    # aiworkhub.test_impact_select.select_tests(root, base) unless a test injects another.
    impact_selector: Callable[[Path, str], Any] | None = None

    @classmethod
    def current(cls) -> Host:
        return cls(
            root=REPO_ROOT,
            runner=run_subprocess,
            python=sys.executable,
            cpu_count=os.cpu_count(),
            env=dict(os.environ),
            is_windows=os.name == "nt",
            out=_print_flushed,
        )


@dataclass(frozen=True)
class Options:
    version: str
    dry_run: bool = False
    push: bool = False
    skip_wsl: bool = False
    wsl_baseline: Path | None = None
    allow_dirty: tuple[str, ...] = ()
    wsl_python: str = WSL_PYTHON
    full_suite: bool = False


@dataclass(frozen=True)
class StepResult:
    name: str
    status: str  # ok | failed | skipped | planned | not_run
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


class StepFailed(Exception):
    """Stops the cut; the message is the step's one-line reason."""


class StepSkipped(Exception):
    """The step does not apply to this cut; the message says why."""


def host_test_workers(cpu_count: int | None) -> int:
    """xdist workers for the host suite: os.cpu_count() less at least a quarter of it."""

    cores = max(1, int(cpu_count or 1))
    return max(1, cores - max(1, math.ceil(cores * HOST_TEST_HEADROOM_FRACTION)))


def _xdist_flag(workers: int) -> str:
    # A single xdist worker only puts a controller in front of a serial run.
    return str(workers) if workers > 1 else "0"


def _wsl_guarded_script(directory: PurePosixPath, inner: Sequence[str]) -> str:
    """``sh -c`` text that enters ``directory`` or exits 97, then execs ``inner``.

    Unguarded, a directory WSL cannot enter leaves the command in ~; every
    token is shell-quoted, so the script is built from data, never from a shell
    string a caller wrote.
    """

    guard = f"cd -- {shlex.quote(str(directory))} || exit {WSL_CD_FAILED}"
    return f"{guard}; exec {shlex.join(inner)}"


def _summary_node_id(line: str) -> str | None:
    """The node id on one ``FAILED``/``ERROR`` short-summary line, else None."""

    prefix = next((p for p in FAILURE_PREFIXES if line.startswith(p)), None)
    if prefix is None:
        return None
    rest = line[len(prefix) :]
    depth = 0
    for index, char in enumerate(rest):
        if char == "[":
            depth += 1
        elif char == "]" and depth:
            depth -= 1
        elif not depth and rest.startswith(" - ", index):
            rest = rest[:index]
            break
    return rest.strip() or None


def failing_node_ids(output: str) -> list[str]:
    """Every distinct node id of pytest's short test summary, in order.

    Only lines after the summary header count, so a test that prints "FAILED"
    into its captured output is not taken for a failure. Unlike
    aiworkhub.recipes.posix_replay.parse_failing, a report capped at 50 ids of
    300 characters, nothing is capped: a baseline comparison needs every id
    intact.
    """

    lines = output.splitlines()
    headers = [index for index, line in enumerate(lines) if SHORT_SUMMARY_HEADER in line]
    if not headers:
        return []
    found: dict[str, None] = {}
    for line in lines[headers[-1] + 1 :]:
        node = _summary_node_id(line)
        if node is not None:
            found.setdefault(node, None)
    return list(found)


def _rerunnable(nodes: Sequence[str]) -> bool:
    """Whether one serial rerun of these suite-reported ids may go to pytest.

    The ids are parsed from suite output and become pytest arguments, so each
    must name a test under tests/. Only the path before "::" is checked: a
    parametrize id after it may hold anything.
    """

    def under_tests(path: str) -> bool:
        parts = path.split("/")
        return parts[0] == "tests" and ".." not in parts and "\\" not in path

    return bool(nodes) and len(nodes) <= LOAD_RERUN_MAX_NODES and all(
        under_tests(node.partition("::")[0]) for node in nodes
    )


def load_wsl_baseline(path: Path) -> frozenset[str]:
    """Known WSL failures: one node id, or one pasted FAILED/ERROR line, per line.

    Blank lines and ``#`` comments are skipped. An unreadable, oversized or
    non-UTF-8 file refuses the cut instead of silently emptying the baseline.
    """

    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_BASELINE_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"WSL baseline {path} is unreadable: {exc.strerror or exc}") from exc
    if len(raw) > MAX_BASELINE_BYTES:
        raise ValueError(f"WSL baseline {path} is larger than {MAX_BASELINE_BYTES} bytes")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"WSL baseline {path} is not UTF-8") from exc
    nodes: set[str] = set()
    for line in text.splitlines():
        entry = line.strip()
        if entry and not entry.startswith("#"):
            nodes.add(_summary_node_id(entry) or entry)
    if len(nodes) > MAX_BASELINE_IDS:
        raise ValueError(f"WSL baseline {path} lists more than {MAX_BASELINE_IDS} node ids")
    return frozenset(nodes)


def dirty_paths(porcelain: str) -> list[str]:
    """Every path ``git status --porcelain=v1 -z`` names; a rename or copy names both."""

    paths: list[str] = []
    fields = porcelain.split("\0")
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            raise ValueError(f"unparseable git status entry {entry!r}")
        paths.append(entry[3:])
        if "R" in entry[:2] or "C" in entry[:2]:
            if index < len(fields) and fields[index]:
                paths.append(fields[index])
            index += 1
    return paths


def _repo_relative(raw: str) -> str:
    text = raw.strip().replace("\\", "/")
    path = PurePosixPath(text)
    if (
        not text
        or path.is_absolute()
        or ".." in path.parts
        or (bool(path.parts) and ":" in path.parts[0])
        or path.as_posix() == "."
    ):
        raise StepFailed(f"--allow-dirty {raw!r} is not a path inside the repository")
    return path.as_posix()


def _covered(path: str, prefixes: Sequence[str]) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


def allowed_dirty_prefixes(raw: Sequence[str]) -> tuple[str, ...]:
    """The --allow-dirty prefixes, repo-relative; refused outside the repo or over packaged input.

    The package step builds the VSIX from the working tree, so a dirty file it
    reads would ship in the VSIX while the release commit, the tag and the WSL
    suite all lack it. No prefix may equal, lie inside or contain a packaged
    root; the comparison ignores case, as Windows and macOS file systems do.
    """

    prefixes: list[str] = []
    for item in raw:
        prefix = _repo_relative(item)
        folded = prefix.casefold()
        for packaged in PACKAGED_ROOTS:
            if _covered(folded, [packaged]) or _covered(packaged, [folded]):
                raise StepFailed(
                    f"--allow-dirty {item!r} overlaps {packaged}/, which the package step "
                    "builds into the VSIX; a dirty packaged file would ship without being in "
                    "the release commit, so commit it or leave it out"
                )
        prefixes.append(prefix)
    return tuple(prefixes)


def _one_line(text: str, limit: int = MAX_LINE_CHARS) -> str:
    flat = " ".join(text.splitlines()).strip()
    return flat if len(flat) <= limit else f"{flat[: limit - 3]}..."


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return _one_line(lines[-1], 300) if lines else "no output"


def _listing(items: Sequence[str]) -> str:
    shown = ", ".join(items[:MAX_LISTED])
    more = len(items) - MAX_LISTED
    return f"{shown} (+{more} more)" if more > 0 else shown


class ReleaseCut:
    """One ordered release cut; ``run`` returns the process exit code."""

    def __init__(self, options: Options, host: Host) -> None:
        self.options = options
        self.host = host
        self.root = host.root
        self.version = options.version
        self.tag = f"v{options.version}"
        self.vsix = f"{EXTENSION_DIR}/dist/aiworkhub-{options.version}.vsix"
        self.workers = host_test_workers(host.cpu_count)
        self.current: str | None = None
        self.baseline: frozenset[str] = frozenset()
        # How the recovery line names the release commit once the commit step made it.
        self.release_commit: str | None = None
        # The ledger's size just before the receipt step appended its built line, which the
        # recovery line truncates back to; None until that step starts.
        self.ledger_bytes_before_receipt: int | None = None
        self.results: list[StepResult] = []
        # What the host and the WSL suite both run, and the lane that decided it; the
        # host_tests step decides once (_decide_test_lane), and until then it is everything.
        self.test_targets: tuple[str, ...] = (FULL_SUITE_TARGET,)
        self.test_selection: dict[str, object] = {
            "lane": None,
            "base_tag": None,
            "selected_count": None,
            "fallback_reason": "host_tests_not_run",
        }
        self._steps: dict[str, Callable[[], str]] = {
            "version": self._step_version,
            "changelog": self._step_changelog,
            "readme": self._step_readme,
            "worktree": self._step_worktree,
            "branch": self._step_branch,
            "tag_free": self._step_tag_free,
            "wsl": self._step_wsl,
            "bump": self._step_bump,
            "metadata_check": self._step_metadata_check,
            "host_tests": self._step_host_tests,
            "package": self._step_package,
            "commit": self._step_commit,
            "wsl_tests": self._step_wsl_tests,
            "receipt": self._step_receipt,
            "tag": self._step_tag,
            "push": self._step_push,
        }

    @property
    def scratch(self) -> Path:
        """.scratch/release/VERSION, used only once the version step accepted VERSION."""

        return self.root.joinpath(".scratch", "release", self.version)

    @property
    def wsl_basetemp(self) -> PurePosixPath:
        return PurePosixPath(f"{WSL_BASETEMP_PREFIX}{self.version}")

    # -- the run -----------------------------------------------------------

    def run(self) -> int:
        failed = self._refused_allow_dirty()
        if failed is None:
            failed = self._run_steps()
        else:
            # Refused before any step ran; the worktree step owns the --allow-dirty rule.
            not_run = f"{failed.name} failed"
            self.results = [
                failed if name == failed.name else StepResult(name, "not_run", not_run)
                for name in STEPS
            ]
        recovery = self._recovery(failed)
        if recovery is not None:
            self.host.out(f"recovery: {recovery}")
        self.host.out(json.dumps(self._summary(failed, recovery), sort_keys=True))
        return 0 if failed is None else 1

    def _refused_allow_dirty(self) -> StepResult | None:
        """The worktree step's refusal of an --allow-dirty prefix, made before any step runs."""

        try:
            allowed_dirty_prefixes(self.options.allow_dirty)
        except StepFailed as refusal:
            result = StepResult("worktree", "failed", _one_line(str(refusal)))
            self._emit("FAILED", result.name, result.detail)
            return result
        return None

    def _run_steps(self) -> StepResult | None:
        """Every step in order up to the first failure, which it returns."""

        dry_run = self.options.dry_run
        failed: StepResult | None = None
        for name in STEPS:
            if failed is not None:
                self.results.append(StepResult(name, "not_run", f"{failed.name} failed"))
            elif dry_run and name in CUT_STEPS:
                self.results.append(StepResult(name, "planned", self._plan(name)))
            else:
                if name in CUT_STEPS:
                    self._emit("run", name, self._plan(name))
                result = self._run_step(name)
                if result.status == "failed":
                    failed = result
                elif dry_run and name == "version":
                    # Every plan line is built from VERSION, the scratch path among
                    # them, so the plan waits for the version step to accept it.
                    for planned in STEPS:
                        self._emit("plan", planned, self._plan(planned))
        return failed

    def _run_step(self, name: str) -> StepResult:
        try:
            status, detail = "ok", self._steps[name]()
        except StepSkipped as skipped:
            status, detail = "skipped", str(skipped)
        except StepFailed as failure:
            status, detail = "failed", str(failure)
        except (OSError, ValueError) as exc:
            status, detail = "failed", str(exc) or type(exc).__name__
        except Exception as exc:  # a step bug still ends as a named failure in the summary
            status, detail = "failed", f"{type(exc).__name__}: {exc}"
        # An ok detail is the step's own text and the cut report has to name every
        # node that passed alone, so it is not held to the bounded line.
        limit = len(detail) if status == "ok" else MAX_LINE_CHARS
        result = StepResult(name, status, _one_line(detail, limit))
        self.results.append(result)
        self._emit("FAILED" if status == "failed" else status, name, result.detail)
        return result

    def _emit(self, status: str, name: str, detail: str) -> None:
        self.host.out(f"{status:<8}{name}: {detail}")

    def _summary(self, failed: StepResult | None, recovery: str | None) -> dict[str, object]:
        return {
            "schema_id": SCHEMA_ID,
            "ok": failed is None,
            "version": self.version,
            "tag": self.tag,
            "current_version": self.current,
            "dry_run": self.options.dry_run,
            "push": self.options.push,
            "skip_wsl": self.options.skip_wsl,
            "host_test_workers": self.workers,
            "test_selection": dict(self.test_selection),
            "failed_step": failed.name if failed is not None else None,
            "reason": failed.detail if failed is not None else None,
            "completed_steps": [result.name for result in self.results if result.status == "ok"],
            "recovery": recovery,
            "steps": [result.to_dict() for result in self.results],
        }

    def _recovery(self, failed: StepResult | None) -> str | None:
        """One line naming what a failed cut already changed; None when it changed nothing.

        A hint, not a resume: a rerun starts again at the version step, which
        refuses a VERSION that is already canonical, so it must restore first.
        """

        if failed is None or failed.name not in CUT_STEPS:
            return None
        done = {result.name for result in self.results if result.status == "ok"}
        files = " ".join(self._generated_files())
        changed = [f"the bump {'rewrote' if 'bump' in done else 'may have rewritten'} {files}"]
        restore = [f"git checkout -- {files}"]
        if self.release_commit is not None:
            changed.append(f"release commit {self.release_commit} carries them")
            restore.insert(0, "git reset HEAD~1")
        if "receipt" in done and self.ledger_bytes_before_receipt is not None:
            # The receipt step appended its "built" line to the tracked ledger after the
            # release commit, and that commit carries the earlier cuts' built lines, which
            # exist nowhere else: a checkout of the ledger would drop them too. Cut the
            # file back to its size before the append instead, which drops only this cut's.
            size = self.ledger_bytes_before_receipt
            truncate = f"import os; os.truncate('{RELEASE_LEDGER}', {size})"
            changed.append(
                f"the receipt appended a built line to {RELEASE_LEDGER} "
                "(the truncate below drops only this cut's built line)"
            )
            restore.append(f'python -c "{truncate}"')
        if "tag" in done:
            changed.append(f"tag {self.tag} exists")
            restore.insert(0, f"git tag -d {self.tag}")
        return f"{'; '.join(changed)}; a rerun must restore them first: {' && '.join(restore)}"

    # -- commands ----------------------------------------------------------

    def _run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Completed:
        return self.host.runner(
            list(argv),
            cwd=self.root,
            env=self.host.env if env is None else env,
            timeout=timeout,
        )

    def _git(self, *args: str, timeout: float = GIT_TIMEOUT_SECONDS) -> Completed:
        return self._run(["git", *args], timeout=timeout)

    def _wsl(self, *args: str) -> Completed:
        # MSYS_NO_PATHCONV=1: a Git-for-Windows layer between here and wsl.exe
        # must not rewrite the Linux-side arguments into Windows paths.
        env = {**self.host.env, "MSYS_NO_PATHCONV": "1"}
        return self._run(["wsl", *args], env=env)

    def _wsl_in(self, directory: PurePosixPath, inner: Sequence[str]) -> Completed:
        """``inner`` run by WSL inside ``directory``; exit 97 when WSL cannot enter it."""

        return self._wsl("-e", "sh", "-c", _wsl_guarded_script(directory, inner))

    @staticmethod
    def _require(done: Completed, what: str) -> Completed:
        if done.returncode != 0:
            raise StepFailed(f"{what} failed (exit {done.returncode}): {_last_line(done.output)}")
        return done

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _write_log(self, name: str, done: Completed) -> str:
        self.scratch.mkdir(parents=True, exist_ok=True)
        path = self.scratch / f"{name}.log"
        path.write_text(done.output, encoding="utf-8", newline="")
        return self._relative(path)

    @staticmethod
    def _generated_files() -> tuple[str, ...]:
        return (release_metadata.CANONICAL_VERSION_FILE, *release_metadata.PROJECTION_FILES)

    def _release_files(self) -> tuple[str, ...]:
        # The release ledger is host-local (.gitignore, 77a9c5ac): it is appended, never committed.
        return (*self._generated_files(), *release_metadata.RELEASE_DOCUMENTS)

    def _host_test_argv(self, nodes: Sequence[str] = ()) -> list[str]:
        # With nodes: the serial rerun of exactly those ids, in a basetemp of its own
        # because deleting the full run's basetemp measured 203 s. Both pass the
        # per-test --timeout, so a stuck test fails with its stack instead of hanging.
        return [
            self.host.python,
            "-m",
            "pytest",
            "-q",
            "-rfE",
            f"--timeout={PYTEST_TIMEOUT_SECONDS}",
            "-n",
            "0" if nodes else _xdist_flag(self.workers),
            f"{BASETEMP_OPTION}{self.scratch / ('br' if nodes else 'b')}",
            *(nodes or self.test_targets),
        ]

    def _wsl_test_command(self, nodes: Sequence[str] = ()) -> list[str]:
        # PYTHONPATH=src with the ini pythonpath cleared, the way
        # aiworkhub.recipes.posix_replay runs the suite: children the tests
        # start import the archived tree as well. The basetemp sits on the Linux
        # filesystem, which the POSIX suite needs for sockets and file modes;
        # _step_wsl_tests removes it after the run. With nodes: the serial rerun
        # of exactly those ids, in the same basetemp. Both pass the per-test
        # --timeout the host suite does, which the wsl preflight checks WSL can load.
        return [
            "env",
            "PYTHONPATH=src",
            self.options.wsl_python,
            "-m",
            "pytest",
            "-q",
            "-rfE",
            f"--timeout={PYTEST_TIMEOUT_SECONDS}",
            "-p",
            "no:cacheprovider",
            "-o",
            "pythonpath=",
            "-n",
            "0" if nodes else _xdist_flag(self.workers),
            f"{BASETEMP_OPTION}{self.wsl_basetemp}",
            *(nodes or self.test_targets),
        ]

    @staticmethod
    def _package_argv() -> list[str]:
        return ["npm", "--prefix", EXTENSION_DIR, "run", "package"]

    def _commit_args(self) -> list[str]:
        return ["commit", "-m", f"release: {self.version}", "--", *self._release_files()]

    def _receipt_argv(self) -> list[str]:
        return [
            self.host.python,
            RECEIPT_SCRIPT,
            "record",
            "--version",
            self.version,
            "--vsix",
            self.vsix,
            "--repo-root",
            str(self.root),
        ]

    def _push_args(self) -> list[str]:
        return ["push", "--atomic", REMOTE, f"refs/heads/{MAIN_BRANCH}", f"refs/tags/{self.tag}"]

    def _plan(self, name: str) -> str:
        if self.options.skip_wsl and name in ("wsl", "wsl_tests"):
            return "skipped: --skip-wsl"
        if name == "push" and not self.options.push:
            return "skipped: --push not given"
        scratch = self._relative(self.scratch)
        baseline = str(self.options.wsl_baseline) if self.options.wsl_baseline else "none"
        plans = {
            "version": f"{self.version} succeeds {release_metadata.CANONICAL_VERSION_FILE}"
            " (x.y.99 -> x.(y+1).0)",
            "changelog": f"CHANGELOG.md [{self.version}] and the extension CHANGELOG name it",
            "readme": f"each README has one '## What's new in {self.version}' section",
            "worktree": "git status --porcelain=v1 -z --untracked-files=all",
            "branch": f"git symbolic-ref --quiet --short HEAD is {MAIN_BRANCH}",
            "tag_free": f"git rev-parse --quiet --verify refs/tags/{self.tag} finds nothing",
            "wsl": f"wsl -e {self.options.wsl_python} -c {shlex.quote(WSL_IMPORT_PROBE)}; "
            f"wslpath -a {scratch} must lie under {WSL_MOUNT_ROOT}; baseline {baseline}",
            "bump": f"release_metadata.bump {self.version}",
            "metadata_check": f"release_metadata.check --tag {self.tag}",
            "host_tests": f"{shlex.join(self._host_test_argv())}; {FULL_SUITE_TARGET} narrows to "
            "the test-impact selection since the last v* tag, the same for wsl_tests, when the "
            "map is fresh for every changed path and neither --full-suite nor the CHANGELOG "
            f"notes' {FULL_SUITE_MARKER} asks for everything",
            "package": shlex.join(self._package_argv()),
            "commit": shlex.join(["git", *self._commit_args()]),
            "wsl_tests": f"git archive HEAD to {scratch}/head.tar, extracted into {scratch}/tree "
            f"at its wslpath -a path (refused unless under {WSL_MOUNT_ROOT}) and committed "
            f"there by {', then '.join(shlex.join(argv) for argv in WSL_TREE_GIT_COMMANDS)}, "
            f"each WSL command behind 'cd -- DIR || exit {WSL_CD_FAILED}'; MSYS_NO_PATHCONV=1 "
            f"{shlex.join(self._wsl_test_command())}; {self.wsl_basetemp} removed after; "
            f"baseline {baseline}",
            "receipt": shlex.join(self._receipt_argv()),
            "tag": f"git tag {self.tag}",
            "push": shlex.join(["git", *self._push_args()]),
        }
        return plans[name]

    # -- preflight ---------------------------------------------------------

    def _step_version(self) -> str:
        current = release_metadata.canonical_version(self.root)
        self.current = current
        release_metadata.validate_successor(current, self.version)
        return f"{current} -> {self.version}"

    def _narrative_gaps(self, *, readmes: bool) -> str:
        gaps = release_metadata.narrative_mismatches(self.root, self.version)
        return "; ".join(
            f"{relative}: {why}"
            for relative, why in sorted(gaps.items())
            if (relative in release_metadata.SINGLE_WHATS_NEW_READMES) == readmes
        )

    def _step_changelog(self) -> str:
        notes = release_metadata.changelog_section(self.root, self.tag)
        gaps = self._narrative_gaps(readmes=False)
        if gaps:
            raise StepFailed(gaps)
        return (
            f"CHANGELOG.md [{self.version}] has {len(notes.splitlines())} line(s) of "
            "notes and the extension CHANGELOG names it"
        )

    def _step_readme(self) -> str:
        gaps = self._narrative_gaps(readmes=True)
        if gaps:
            raise StepFailed(gaps)
        return f"each README has exactly one '## What's new in {self.version}' section"

    def _step_worktree(self) -> str:
        allowed = allowed_dirty_prefixes(self.options.allow_dirty)
        status = self._require(
            self._git("status", "--porcelain=v1", "-z", "--untracked-files=all"), "git status"
        )
        dirty = sorted(set(dirty_paths(status.stdout)))
        generated = set(self._generated_files())
        stale = [path for path in dirty if path in generated]
        if stale:
            raise StepFailed(
                f"generated by the bump, so they must be clean before a cut: {_listing(stale)}"
            )
        documents = {*release_metadata.RELEASE_DOCUMENTS, RELEASE_LEDGER}
        stray = [path for path in dirty if path not in documents and not _covered(path, allowed)]
        if stray:
            raise StepFailed(
                f"dirty outside the release documents and --allow-dirty: {_listing(stray)}"
            )
        return f"{len(dirty)} dirty path(s), each a release document or under --allow-dirty"

    def _step_branch(self) -> str:
        done = self._git("symbolic-ref", "--quiet", "--short", "HEAD")
        if done.returncode == 1:
            raise StepFailed(f"HEAD is detached; a release is cut from {MAIN_BRANCH}")
        branch = self._require(done, "git symbolic-ref HEAD").stdout.strip()
        if branch != MAIN_BRANCH:
            raise StepFailed(f"HEAD is on {branch!r}; a release is cut from {MAIN_BRANCH}")
        return f"HEAD is {MAIN_BRANCH}"

    def _step_tag_free(self) -> str:
        done = self._git("rev-parse", "--quiet", "--verify", f"refs/tags/{self.tag}")
        if done.returncode == 0:
            raise StepFailed(f"tag {self.tag} already exists")
        if done.returncode != 1:
            self._require(done, f"git rev-parse --verify refs/tags/{self.tag}")
        return f"tag {self.tag} is free"

    def _step_wsl(self) -> str:
        if self.options.skip_wsl:
            raise StepSkipped("--skip-wsl")
        if not self.host.is_windows:
            raise StepFailed("the WSL suite runs from a Windows host; elsewhere pass --skip-wsl")
        if self.options.wsl_baseline is not None:
            self.baseline = load_wsl_baseline(self.options.wsl_baseline)
        python = self.options.wsl_python
        modules = ", ".join(WSL_SUITE_MODULES)
        self._require(
            self._wsl("--cd", str(self.root), "-e", python, "-c", WSL_IMPORT_PROBE),
            f"WSL {python} importing {modules}",
        )
        # The /mnt/ rule _step_wsl_tests applies, checked here as well: this step
        # is read-only and --dry-run runs it, so a scratch path outside /mnt/ stops
        # the cut before the bump and the release commit, not after them.
        scratch = self._wsl_scratch()
        return (
            f"WSL {python} imports {modules}; WSL names the scratch directory "
            f"{scratch}; the baseline lists {len(self.baseline)} node id(s)"
        )

    # -- the cut -----------------------------------------------------------

    def _step_bump(self) -> str:
        release_metadata.bump(self.root, self.version)
        return (
            f"{release_metadata.CANONICAL_VERSION_FILE} and "
            f"{len(release_metadata.PROJECTION_FILES)} projections carry {self.version}"
        )

    def _step_metadata_check(self) -> str:
        result = release_metadata.check(self.root, tag=self.tag)
        if not result.get("ok"):
            mismatches = result.get("mismatches")
            pairs = sorted(mismatches.items()) if isinstance(mismatches, dict) else []
            raise StepFailed(
                "release metadata mismatches: "
                + _listing([f"{path}: {why}" for path, why in pairs])
            )
        return f"projections, tag and release documents all name {self.version}"

    def _decide_test_lane(self) -> None:
        """Set the one target list the host and the WSL suite both run, and record why.

        The test-impact selection since the last release tag (``impact_map``) when the
        map is fresh for every path changed since that tag; otherwise the whole
        ``tests`` directory (``full_suite``) with the named reason. An unknown never
        selects less: every miss, raise or full-suite answer falls back.
        """

        targets: tuple[str, ...] = ()
        base: str | None = None
        if self.options.full_suite:
            reason = "full_suite_flag"
        else:
            reason = self._metadata_full_suite_reason()
        if not reason:
            base = self._last_release_tag()
            if base is None:
                reason = "no_previous_tag"
            else:
                try:
                    targets, reason = self._impact_targets(base)
                except Exception as exc:  # a failed selector never narrows the run
                    targets, reason = (), f"selector_failed:{type(exc).__name__}"
                if not targets and not reason:
                    reason = "impact_map_selected_nothing"
        if targets:
            self.test_targets = targets
            self.test_selection = {
                "lane": LANE_IMPACT_MAP,
                "base_tag": base,
                "selected_count": len(targets),
                "fallback_reason": None,
            }
            return
        self.test_targets = (FULL_SUITE_TARGET,)
        self.test_selection = {
            "lane": LANE_FULL_SUITE,
            "base_tag": base,
            "selected_count": None,
            "fallback_reason": _one_line(reason),
        }

    def _metadata_full_suite_reason(self) -> str:
        """Why the release metadata requires the whole suite, else ``""``.

        The CHANGELOG [VERSION] notes naming ``full_suite_required`` mark the release;
        notes that cannot be read require it too.
        """

        try:
            notes = release_metadata.changelog_section(self.root, self.tag)
        except Exception as exc:  # unreadable metadata is never a narrowed run
            return f"release_metadata_unreadable:{type(exc).__name__}"
        return "release_metadata_full_suite_required" if FULL_SUITE_MARKER in notes else ""

    def _last_release_tag(self) -> str | None:
        """The nearest ``v*`` tag on HEAD's first-parent history, else None."""

        done = self._git(
            "describe", "--tags", "--abbrev=0", "--match", "v*", "--first-parent", "HEAD"
        )
        text = done.stdout.strip()
        if (
            done.returncode != 0
            or not text.startswith("v")
            or any(char.isspace() for char in text)
            or text == self.tag
        ):
            return None
        return text

    def _impact_targets(self, base: str) -> tuple[tuple[str, ...], str]:
        """The selector's tests for ``base`` to the working tree plus the invariants.

        Returns ``(tests, "")``, or ``((), reason)`` when the map is missing, does not
        record a changed path, records a changed path stale (a file the version bump
        rewrites is accepted when HEAD's bytes match the map; no other path is), or
        records any other non-test path stale. The selection always adds every
        ``accept_neighbour_tests.INVARIANT_TESTS`` file (the module size ratchet among
        them) and every changed test file; argv folding and the per-entry checks are
        that module's ``_impact_tests``.
        """

        neighbours, impact = _impact_modules()
        selector = self.host.impact_selector or impact.select_tests
        result = selector(self.root, base)
        if result.full_suite_required:
            return (), "selector_full_suite:" + ";".join(result.full_suite_reasons)
        payload, problem = impact.load_map(impact.map_directory(self.root) / impact.MAP_FILE_NAME)
        digests = payload.get("digests") if payload is not None else None
        if not isinstance(digests, Mapping):
            return (), f"impact_map_unavailable:{problem or 'map_schema_mismatch'}"
        changed = sorted(set(result.changed_paths))
        # A changed test file runs whole whatever the map records of it, so only the
        # other changed paths need the map's edges.
        states = {
            path: neighbours._map_state(self.root, digests, path)
            for path in changed
            if not impact.is_test_module(path)
        }
        unmapped = [path for path, state in states.items() if state == "unmapped"]
        if unmapped:
            return (), f"impact_map_unmapped:{unmapped[0]}"
        stale = [path for path, state in states.items() if state]
        # Only the files the version bump rewrites may be accepted on a HEAD digest
        # match; any other stale changed path means the map predates its bytes.
        generated = set(self._generated_files())
        relaxable = [path for path in stale if path in generated]
        if relaxable:
            try:
                at_head = impact.read_blobs(self.root, "HEAD", relaxable)
            except Exception:  # HEAD unreadable: the working tree's answer stands
                at_head = {}
            stale = [
                path
                for path in stale
                if path not in generated
                or at_head.get(path) is None
                or impact.content_digest(at_head[path]) != digests[path]
            ]
        if stale:
            return (), f"impact_map_stale:{stale[0]}"
        for path in sorted(set(digests) - set(changed)):
            if not impact.is_test_module(path) and neighbours._map_state(
                self.root, digests, path
            ):
                return (), f"impact_map_stale_dependency:{path}"
        entries: dict[str, tuple[str, ...]] = dict(result.selected)
        for test in neighbours.INVARIANT_TESTS:
            entries.setdefault(test, ("invariant",))
        for path in changed:
            if impact.is_test_module(path) and (self.root / path).is_file():
                entries.setdefault(path, ("changed_test",))
        return neighbours._impact_tests(self.root, entries, ())

    def _step_host_tests(self) -> str:
        self.scratch.mkdir(parents=True, exist_ok=True)
        self._decide_test_lane()
        done = self._run(self._host_test_argv())
        log = self._write_log("host_tests", done)
        cores = max(1, int(self.host.cpu_count or 1))
        detail = f"{_last_line(done.output)} with {self.workers} worker(s) on {cores} core(s)"
        if done.returncode == 0:
            return detail
        # Exit 1 names failed tests; any other exit is no verdict about a node. One
        # serial rerun of those nodes tells full-load time bounds from a real failure.
        nodes = failing_node_ids(done.output) if done.returncode == 1 else []
        if not _rerunnable(nodes):
            raise StepFailed(
                f"pytest exited {done.returncode}: {_last_line(done.output)} (log {log})"
            )
        redone = self._run(self._host_test_argv(nodes))
        rerun_log = self._write_log("host_tests_rerun", redone)
        if redone.returncode != 0:
            raise StepFailed(
                f"the serial rerun of {len(nodes)} failed node(s) exited {redone.returncode}: "
                f"{_listing(nodes)} (logs {log}, {rerun_log})"
            )
        return f"{detail}; {len(nodes)} failed node(s) passed alone: {', '.join(nodes)}"

    def _step_package(self) -> str:
        done = self._run(self._package_argv())
        log = self._write_log("package", done)
        if done.returncode != 0:
            raise StepFailed(
                f"npm run package exited {done.returncode}: {_last_line(done.output)} (log {log})"
            )
        vsix = self.root / self.vsix
        if not vsix.is_file() or vsix.stat().st_size <= 0:
            raise StepFailed(f"npm run package did not produce {self.vsix}")
        return f"{self.vsix}, {vsix.stat().st_size} bytes"

    def _step_commit(self) -> str:
        self._require(self._git(*self._commit_args()), "git commit")
        # Named before its sha is read, so a failure from here on still reports it.
        self.release_commit = f"'release: {self.version}'"
        head = self._require(self._git("rev-parse", "--verify", "HEAD"), "git rev-parse HEAD")
        self.release_commit = head.stdout.strip()
        return (
            f"{self.release_commit} 'release: {self.version}' of "
            f"{len(self._release_files())} release path(s)"
        )

    def _wsl_scratch(self) -> PurePosixPath:
        """The scratch directory as WSL names it (``wslpath -a``), refused unless under /mnt/."""

        relative = self._relative(self.scratch)
        done = self._require(
            self._wsl("-e", "wslpath", "-a", str(self.scratch)), f"wslpath -a {relative}"
        )
        text = done.stdout.strip()
        linux = PurePosixPath(text)
        suffix = PurePosixPath(relative).parts
        if (
            text.splitlines() != [text]
            or not text.startswith(WSL_MOUNT_ROOT)
            or ".." in linux.parts
            or linux.parts[-len(suffix) :] != suffix
        ):
            raise StepFailed(
                f"WSL names {relative} {text!r}, not a path under {WSL_MOUNT_ROOT} ending in "
                f"{relative}; nothing is cleared or run there"
            )
        return linux

    def _step_wsl_tests(self) -> str:
        if self.options.skip_wsl:
            raise StepSkipped("--skip-wsl")
        self.scratch.mkdir(parents=True, exist_ok=True)
        # Translated once here, with the same /mnt/ rule the wsl preflight already
        # applied before any commit. Every WSL command below names only absolute
        # paths and runs behind a cd guard, so a directory WSL cannot enter stops
        # the command instead of running it in ~.
        scratch = self._wsl_scratch()
        tree, archive = scratch / "tree", scratch / "head.tar"
        clear = ("rm", "-rf", "--", str(tree), str(archive), str(self.wsl_basetemp))
        self._require(self._wsl_in(scratch, clear), "clearing the previous WSL tree")
        try:
            output = self.scratch / "head.tar"
            archived = self._git(
                "archive",
                "--format=tar",
                f"--output={output}",
                "HEAD",
                timeout=LONG_GIT_TIMEOUT_SECONDS,
            )
            if archived.returncode != 0 or not output.is_file():
                raise StepFailed(
                    f"git archive HEAD failed (exit {archived.returncode}): "
                    f"{_last_line(archived.output)}"
                )
            self._require(
                self._wsl_in(scratch, ("mkdir", "--", str(tree))), "creating the WSL tree"
            )
            self._require(
                self._wsl_in(scratch, ("tar", "-xf", str(archive), "-C", str(tree))),
                "extracting HEAD in WSL",
            )
            self._commit_wsl_tree(tree)
            done = self._wsl_in(tree, self._wsl_test_command())
            return self._wsl_verdict(done, self._write_log("wsl_tests", done), tree)
        finally:
            # WSL wrote the tree and pytest's basetemp, so WSL removes both, pass
            # or fail. A failed removal leaves only git-ignored scratch and a /tmp
            # directory behind and never changes the verdict.
            self._wsl_in(scratch, clear)

    def _commit_wsl_tree(self, tree: PurePosixPath) -> None:
        """Make the extracted tree a repository whose one commit holds every archived file.

        ``git archive`` carries no .git, so without this the suite's tests that run git
        against the checkout (tests/test_retry_finalization_host_lane_nf01030.py) fail
        as noise on every cut. Each of WSL_TREE_GIT_COMMANDS runs behind the tree's cd
        guard, and the first that fails stops the step with its whole stderr: git often
        prints a hint after the line that names the cause.
        """

        for argv in WSL_TREE_GIT_COMMANDS:
            done = self._wsl_in(tree, argv)
            if done.returncode != 0:
                raise StepFailed(
                    f"{shlex.join(argv)} in the WSL tree failed (exit {done.returncode}): "
                    f"{_one_line(done.stderr) or _last_line(done.output)}"
                )

    def _wsl_verdict(self, done: Completed, log: str, tree: PurePosixPath) -> str:
        summary = _last_line(done.output)
        if done.returncode == 0:
            return f"WSL suite passed: {summary}"
        if done.returncode != 1:
            raise StepFailed(f"WSL pytest exited {done.returncode}: {summary} (log {log})")
        failing = failing_node_ids(done.output)
        if not failing:
            raise StepFailed(f"WSL pytest exited 1 without naming a failing node id (log {log})")
        new = [node for node in failing if node not in self.baseline]
        if new:
            stopped = f"{len(new)} WSL failure(s) not in the baseline: {_listing(new)}"
            if not _rerunnable(new):
                raise StepFailed(f"{stopped} (log {log})")
            redone = self._wsl_in(tree, self._wsl_test_command(new))
            rerun_log = self._write_log("wsl_tests_rerun", redone)
            if redone.returncode != 0:
                raise StepFailed(f"{stopped} (logs {log}, {rerun_log})")
            return (
                f"{len(new)} WSL failure(s) passed alone: {', '.join(new)}; "
                f"{len(failing) - len(new)} failure(s) in the baseline: {summary}"
            )
        return f"{len(failing)} WSL failure(s), each in the baseline: {summary}"

    def _step_receipt(self) -> str:
        # The receipt only appends, so the recovery line can cut the ledger back to this size.
        ledger = self.root / RELEASE_LEDGER
        self.ledger_bytes_before_receipt = ledger.stat().st_size if ledger.is_file() else 0
        self._require(self._run(self._receipt_argv()), "release_receipt.py record")
        return f"built receipt for {self.version} appended to {RELEASE_LEDGER}"

    def _step_tag(self) -> str:
        self._require(self._git("tag", self.tag), f"git tag {self.tag}")
        return f"tagged {self.tag}"

    def _step_push(self) -> str:
        if not self.options.push:
            raise StepSkipped(
                f"--push not given; publish with: {shlex.join(['git', *self._push_args()])}"
            )
        self._require(self._git(*self._push_args(), timeout=LONG_GIT_TIMEOUT_SECONDS), "git push")
        return f"{MAIN_BRANCH} and {self.tag} pushed to {REMOTE} together"


def _wsl_python_option(value: str) -> str:
    """The --wsl-python value: ``python3`` or an absolute POSIX path, else a named refusal.

    The interpreter runs inside WSL, so a relative path, a Windows path and a
    path with whitespace or a backslash cannot name it, and ``env``, which starts
    the suite, would read a path containing ``=`` as a NAME=VALUE assignment.
    """

    if value == WSL_PYTHON:
        return value
    if not value.startswith("/"):
        cause = "it does not start with '/'"
    elif "\\" in value:
        cause = "it contains a backslash"
    elif any(char.isspace() for char in value):
        cause = "it contains whitespace"
    elif "=" in value:
        cause = "it contains '=' (env would read it as an assignment)"
    else:
        return value
    raise argparse.ArgumentTypeError(
        f"{value!r} is not {WSL_PYTHON} or an absolute POSIX path: {cause}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release_cut.py",
        description="Cut one AIWorkHub release in order; stop at the first failing step.",
    )
    parser.add_argument("version", help="the release version, MAJOR.MINOR.PATCH")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the plan and run only the read-only checks"
    )
    parser.add_argument(
        "--push", action="store_true", help="push main and the tag together; never forced"
    )
    parser.add_argument("--skip-wsl", action="store_true", help="do not run the WSL suite")
    parser.add_argument(
        "--full-suite",
        action="store_true",
        help="run the whole suite on the host and in WSL, never the test-impact selection",
    )
    parser.add_argument(
        "--wsl-baseline",
        type=Path,
        metavar="PATH",
        help="known WSL failures, one pytest node id per line",
    )
    parser.add_argument(
        "--wsl-python",
        type=_wsl_python_option,
        default=WSL_PYTHON,
        metavar="PATH",
        help=(
            f"the WSL interpreter that has pytest, xdist and pytest-timeout: {WSL_PYTHON} "
            "(default) or an absolute POSIX path such as /root/venv/bin/python; Git Bash "
            "rewrites a leading '/' unless MSYS_NO_PATHCONV=1 is set"
        ),
    )
    parser.add_argument(
        "--allow-dirty",
        action="extend",
        nargs="+",
        default=[],
        metavar="PATH",
        help="paths that may be dirty; they stay out of the release commit",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, host: Host | None = None) -> int:
    args = _parser().parse_args(None if argv is None else list(argv))
    options = Options(
        version=args.version,
        dry_run=args.dry_run,
        push=args.push,
        skip_wsl=args.skip_wsl,
        wsl_baseline=args.wsl_baseline,
        wsl_python=args.wsl_python,
        allow_dirty=tuple(args.allow_dirty),
        full_suite=args.full_suite,
    )
    if host is None:
        _bind_children_to_this_process()
        host = Host.current()
    return ReleaseCut(options, host).run()


if __name__ == "__main__":
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
