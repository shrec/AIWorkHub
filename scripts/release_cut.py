#!/usr/bin/env python3
"""Cut one AIWorkHub release as a single deterministic, ordered run.

    python scripts/release_cut.py VERSION [--dry-run] [--push] [--skip-wsl]
        [--wsl-baseline PATH] [--wsl-python PATH] [--allow-dirty PATH ...]

Write the release documents first: CHANGELOG.md, vscode-extension/CHANGELOG.md
and both READMEs name VERSION, and that prose is the one manual part. This
script does the rest of the release procedure in order, stops at the first
failing step with a one-line reason, and ends with one JSON summary line on
stdout. A failure after the bump prints one recovery line before it, naming
what the cut already changed, which a rerun must restore first.

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
                  POSIX path --wsl-python names) imports pytest and xdist, whose
                  wslpath -a names .scratch/release/VERSION under /mnt/, and a
                  readable --wsl-baseline

The cut:

  bump            release_metadata.bump: the canonical version and projections
  metadata_check  release_metadata.check --tag vVERSION
  host_tests      the whole suite on this host, xdist workers derived from
                  os.cpu_count() with headroom; up to LOAD_RERUN_MAX_NODES
                  failed node ids get one serial rerun, and only a node that
                  fails again stops the cut
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
import ctypes
import importlib.util
import io
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import ModuleType
from typing import Callable, Mapping, Protocol, Sequence


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
MAX_BASELINE_BYTES = 1024 * 1024
MAX_BASELINE_IDS = 10_000
MAX_LINE_CHARS = 600
MAX_LISTED = 5
# More failures than this is a real break, not load, and is not rerun.
LOAD_RERUN_MAX_NODES = 25
SHORT_SUMMARY_HEADER = "short test summary info"
FAILURE_PREFIXES = ("FAILED ", "ERROR ")

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
    """The real runner: an argv list, no shell, no stdin, output captured."""

    command = list(argv)
    wsl = _is_wsl(command[0])
    # PATHEXT lookup: on Windows ``npm`` is npm.cmd, which a bare name misses.
    command[0] = shutil.which(command[0]) or command[0]
    try:
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
                if kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
                    return True
    except Exception as exc:  # whatever the load or a kernel call throws, the cut goes on
        detail = f", {type(exc).__name__}"
    code = kernel32.GetLastError()  # read before CloseHandle overwrites it
    if job:
        kernel32.CloseHandle(job)
    print(
        f"warning child_lifetime: {step} failed (GetLastError {code}{detail}), "
        "so a stopped cut can leave its child processes behind"
    )
    return False


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

    @classmethod
    def current(cls) -> Host:
        return cls(
            root=REPO_ROOT,
            runner=run_subprocess,
            python=sys.executable,
            cpu_count=os.cpu_count(),
            env=dict(os.environ),
            is_windows=os.name == "nt",
            out=print,
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
        # because deleting the full run's basetemp measured 203 s.
        return [
            self.host.python,
            "-m",
            "pytest",
            "-q",
            "-rfE",
            "-n",
            "0" if nodes else _xdist_flag(self.workers),
            f"--basetemp={self.scratch / ('br' if nodes else 'b')}",
            *(nodes or ("tests",)),
        ]

    def _wsl_test_command(self, nodes: Sequence[str] = ()) -> list[str]:
        # PYTHONPATH=src with the ini pythonpath cleared, the way
        # aiworkhub.recipes.posix_replay runs the suite: children the tests
        # start import the archived tree as well. The basetemp sits on the Linux
        # filesystem, which the POSIX suite needs for sockets and file modes;
        # _step_wsl_tests removes it after the run. With nodes: the serial rerun
        # of exactly those ids, in the same basetemp.
        return [
            "env",
            "PYTHONPATH=src",
            self.options.wsl_python,
            "-m",
            "pytest",
            "-q",
            "-rfE",
            "-p",
            "no:cacheprovider",
            "-o",
            "pythonpath=",
            "-n",
            "0" if nodes else _xdist_flag(self.workers),
            f"--basetemp={self.wsl_basetemp}",
            *(nodes or ("tests",)),
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
            "wsl": f"wsl -e {self.options.wsl_python} -c 'import pytest, xdist'; "
            f"wslpath -a {scratch} must lie under {WSL_MOUNT_ROOT}; baseline {baseline}",
            "bump": f"release_metadata.bump {self.version}",
            "metadata_check": f"release_metadata.check --tag {self.tag}",
            "host_tests": shlex.join(self._host_test_argv()),
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
        self._require(
            self._wsl("--cd", str(self.root), "-e", python, "-c", "import pytest, xdist"),
            f"WSL {python} importing pytest and xdist",
        )
        # The /mnt/ rule _step_wsl_tests applies, checked here as well: this step
        # is read-only and --dry-run runs it, so a scratch path outside /mnt/ stops
        # the cut before the bump and the release commit, not after them.
        scratch = self._wsl_scratch()
        return (
            f"WSL {python} imports pytest and xdist; WSL names the scratch directory "
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

    def _step_host_tests(self) -> str:
        self.scratch.mkdir(parents=True, exist_ok=True)
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
            f"the WSL interpreter that has pytest and xdist: {WSL_PYTHON} (default) or an "
            "absolute POSIX path such as /root/venv/bin/python; Git Bash rewrites a leading "
            "'/' unless MSYS_NO_PATHCONV=1 is set"
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
    )
    if host is None:
        _bind_children_to_this_process()
        host = Host.current()
    return ReleaseCut(options, host).run()


if __name__ == "__main__":
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
