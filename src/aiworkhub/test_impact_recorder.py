"""Per-test execution recorder for changed-lines test selection (NF-2026-02054, card A).

Opt in with ``python -m pytest -p aiworkhub.test_impact_recorder``. For every test id
the recorder keeps:

* ``code`` -- the repository code objects the test executed. A ``sys.monitoring``
  ``PY_START`` callback returns ``DISABLE`` after the first hit and every recording
  window re-arms it with ``restart_events``, so each code object costs one callback
  per test;
* ``reads`` -- the repository non-Python files the test opened for reading (an
  ``open`` audit hook, ``sys.addaudithook``);
* ``children`` -- for a test that launched a child Python interpreter running
  repository code (``subprocess.Popen`` audit event), the child's entry modules.
  What the child ran is opaque to this process, so the selector re-runs the test
  whenever the static import closure of those entries changed. A child that cannot
  be narrowed is ``*``, re-run for any change: a shell (``shell=True``), a console
  script or other launcher, ``os.spawn*``/``os.system``/``os.fork`` (their audit
  events), and a multiprocessing process of any start method (``BaseProcess.start``
  is watched: spawn and forkserver raise no audit event).

Module-level code is deliberately not recorded per test: it runs once per process,
inside whichever test or collection step imported the module first, so recording it
would make the map depend on test order and on how xdist split the run. The selector
derives "imported the module" from the static import closure of the test file and of
the files whose code the test executed, which is the same for every split. What runs
while a repository module body is on the stack is instead recorded process-wide: the
functions it called (``import_time``) and the data files it opened (``import_reads``),
so an edit of either selects the importers. The setup of a fixture wider than function
scope is recorded per fixture definition; a new parameter's setup replaces the previous
one and each test is credited with the setup live when it ran. Teardown is recorded for
function-scope fixtures only: a wider
fixture's finalizers run after whichever test a worker happened to run last, so they
are attributed to no test.

xdist workers each write a shard; the controller -- the only process under ``-n 0``
-- merges the shards into one map with sorted keys and no worker or time fields, so
``-n 0`` and ``-n 4`` produce byte-identical maps. The map is written only from a
session that ran the whole configured suite: a deselected, path-narrowed, stopped,
interrupted or collection-erroring session leaves the previous map untouched and the
terminal summary says why. Everything is written under
``.aiworkhub/runtime/test_impact/`` of the pytest rootdir and nowhere else.
"""

from __future__ import annotations

import glob
import json
import os
import shlex
import shutil
import sys
import sysconfig
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import CodeType
from typing import Any, NamedTuple

import pytest

from . import test_impact_select as _select
from .platform_io import is_windows

SHARD_SCHEMA_ID = "aiworkhub.test_impact.shard.v3"
TOOL_NAME = "aiworkhub.test_impact_recorder"
_PLUGIN_NAME = "aiworkhub_test_impact_recorder"
# sys.monitoring reserves ids 0 (debugger), 1 (coverage), 2 (profiler) and
# 5 (optimizer) by convention; 3 and 4 are unassigned, so they are tried first.
_TOOL_IDS = (3, 4, 2, 5)
_NOT_RECORDED_PARTS = frozenset(
    {".git", ".aiworkhub", "__pycache__", "site-packages", ".venv", "venv", ".tox", "node_modules"}
)
# Opening these is importing, not reading data.
_CODE_SUFFIXES = frozenset({".py", ".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib", ".pth"})
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_OPTIONS_WITH_VALUE = frozenset({"-W", "-X", "--check-hash-based-pycs"})
_EXITS_BEFORE_RUNNING = frozenset({"-V", "-VV", "--version", "-h", "-?", "--help"})
_UNSEEN = object()
# Children this process cannot narrow (NF-2026-02219): shells and script hosts run any
# command, and these audit events start a child without a subprocess.Popen event --
# os.spawn* forks on POSIX. _winapi.CreateProcess counts only outside subprocess.
_SHELLS = frozenset(
    {"sh", "bash", "dash", "zsh", "ksh", "csh", "tcsh", "fish", "ash", "busybox", "cmd", "powershell", "pwsh"}
)
_SCRIPT_SUFFIXES = frozenset({".bat", ".cmd", ".ps1", ".sh"})
# Native programs that run the program their argv names; one naming nothing resolvable is unseen.
_WRAPPERS = frozenset(
    {
        "env", "timeout", "nice", "nohup", "ionice", "stdbuf", "time", "taskset", "chrt", "setsid",
        "sudo", "doas", "xargs", "flock", "strace", "ltrace", "valgrind", "uv", "uvx", "pipx", "poetry",
        "pipenv", "pdm", "hatch", "rye", "pixi", "conda", "mamba", "micromamba", "tox", "nox", "start",
    }
)
_UNOBSERVED_CHILD_EVENTS = frozenset(
    {"os.fork", "os.forkpty", "os.spawn", "os.posix_spawn", "os.system", "os.startfile", "_winapi.CreateProcess"}
)
_CHILD_EVENTS = _UNOBSERVED_CHILD_EVENTS | {"subprocess.Popen"}
# Functions that start a child with no audit event at all, by co_qualname -> package
# directory: multiprocessing's spawn and forkserver launch through _posixsubprocess.
_UNAUDITED_LAUNCHERS = {"BaseProcess.start": "multiprocessing"}
# Windows console scripts are .exe launchers; POSIX ones are #! scripts, read instead.
_LAUNCHER_DIRECTORY_CHECK = is_windows()
# A session is complete only if every test ran: failures are recorded, an interrupted,
# erroring or empty session is not.
_COMPLETE_EXIT_STATUSES = frozenset({int(pytest.ExitCode.OK), int(pytest.ExitCode.TESTS_FAILED)})
# Command-line options that skip tests without a pytest_deselected call.
_NARROWING_OPTIONS = frozenset(
    {"--ignore", "--ignore-glob", "--lf", "--last-failed", "--sw", "--stepwise", "--sw-skip", "--stepwise-skip"}
)
# Option destinations under which pytest reports tests without running their bodies.
_NO_RUN_OPTIONS = (("collectonly", "collect_only"), ("setuponly", "setup_only"), ("setupplan", "setup_plan"))


class RecorderError(RuntimeError):
    """A shard the controller cannot merge; the map is left as it was."""


@dataclass
class _Sink:
    code: set[str] = field(default_factory=set)
    reads: set[str] = field(default_factory=set)
    children: set[str] = field(default_factory=set)
    errors: int = 0

    def absorb(self, other: _Sink) -> None:
        self.code |= other.code
        self.reads |= other.reads
        self.children |= other.children
        self.errors += other.errors

    def row(self, test_file: str) -> dict[str, Any]:
        return {
            "file": test_file,
            "code": sorted(self.code),
            "reads": sorted(self.reads),
            "children": sorted(self.children),
            "incomplete": self.errors > 0,
        }


def function_qualname(qualname: str) -> str:
    """The enclosing function of a lambda, comprehension or genexpr code object."""
    parts = qualname.split(".")
    while len(parts) > 1 and parts[-1].startswith("<"):
        parts.pop()
    return ".".join(parts)


def _under(roots: Iterable[str], absolute: str) -> str | None:
    folded = os.path.normcase(absolute)
    for root in roots:
        if folded.startswith(os.path.normcase(root) + os.sep):
            parts = absolute[len(root) + 1 :].split(os.sep)
            return None if not _NOT_RECORDED_PARTS.isdisjoint(parts) else "/".join(parts)
    return None


def repo_relative(roots: Iterable[str], path: str) -> str | None:
    """``path`` relative to the repository with "/" separators, or None outside it."""
    roots = tuple(roots)
    absolute = os.path.abspath(path)
    found = _under(roots, absolute)
    return found if found is not None else _under(roots, os.path.realpath(absolute))


def _writes(mode: object, flags: object) -> bool:
    if isinstance(mode, str):
        return any(letter in mode for letter in "wax")
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


def _is_python(program: str) -> bool:
    name = os.path.basename(program).lower()
    if name.startswith(("python", "pypy")):
        return True
    executable = os.path.normcase(os.path.abspath(sys.executable))
    return bool(program) and os.path.normcase(os.path.abspath(program)) == executable


def split_windows_command_line(text: str) -> list[str]:
    """Split ``text`` by the Microsoft C runtime rules, the inverse of ``list2cmdline``.

    Spaces and tabs outside quotes separate words. 2n backslashes before a quote give
    n backslashes and the quote toggles; 2n+1 give n backslashes and a literal quote;
    backslashes not before a quote are literal.
    """
    words: list[str] = []
    current: list[str] = []
    in_word = in_quotes = False
    index = 0
    while index < len(text):
        char = text[index]
        if char in " \t" and not in_quotes:
            if in_word:
                words.append("".join(current))
                current.clear()
                in_word = False
            index += 1
            continue
        in_word = True
        if char == "\\":
            end = index
            while end < len(text) and text[end] == "\\":
                end += 1
            count = end - index
            if end < len(text) and text[end] == '"':
                current.append("\\" * (count // 2))
                if count % 2:
                    current.append('"')
                    end += 1
            else:
                current.append("\\" * count)
            index = end
            continue
        if char == '"':
            in_quotes = not in_quotes
        else:
            current.append(char)
        index += 1
    if in_word:
        words.append("".join(current))
    return words


def _split_posix_command_line(text: str) -> list[str]:
    return shlex.split(text, posix=True)


_split_command_line: Callable[[str], list[str]] = (
    split_windows_command_line if is_windows() else _split_posix_command_line
)


def _launcher_directories() -> frozenset[str]:
    places = (os.path.dirname(sys.executable), sysconfig.get_path("scripts"))
    return frozenset(os.path.normcase(os.path.abspath(place)) for place in places if place)


def _locate(program: str, cwd: str, env: object) -> str | None:
    if os.path.dirname(program):
        candidate = os.path.join(cwd, program)
        return candidate if os.path.isfile(candidate) else None
    search = env.get("PATH") if isinstance(env, Mapping) else None
    return shutil.which(program, path=search) or shutil.which(program)


def _console_script(found: str | None, named: str | None = None) -> bool:
    """Whether a program launches the interpreter: a ``#!`` script on POSIX, an ``.exe``
    beside the interpreter or in its Scripts directory on Windows.

    ``found`` is the resolved file; ``named``, the path Popen was given, is judged by
    its directory when it does not resolve.
    """
    target = found or named
    if _LAUNCHER_DIRECTORY_CHECK and target:
        place = os.path.dirname(os.path.abspath(target))
        if os.path.normcase(place) in _launcher_directories():
            return True
    if found is None:
        return False
    with open(found, "rb") as handle:
        return handle.read(2) == b"#!"


def runs_unseen_code(program: str, args: list[str], cwd: str, env: object) -> bool:
    """Whether a non-Python program can start Python code this process cannot narrow.

    A shell (``shell=True``) or script host runs whatever its command line says, and a
    console script launches the interpreter. A native program can start either from its
    argv (``env``, ``timeout``, ``nice``, ``uv run``): an argv word that is an
    interpreter or a console script makes the child unseen, and a known wrapper whose
    argv names no program this process can resolve is unseen too, never dropped. A
    program that does not exist starts nothing: Popen raises first.
    """
    stem, suffix = os.path.splitext(os.path.basename(program).lower())
    if stem in _SHELLS or suffix in _SCRIPT_SUFFIXES or any(_is_python(word) for word in args):
        return True
    found = _locate(program, cwd, env)
    if _console_script(found, os.path.join(cwd, program)):
        return True
    if found is None:
        return False
    located = [_locate(word, cwd, env) for word in args if word and not word.startswith("-")]
    if any(_console_script(place) for place in located if place is not None):
        return True
    return stem in _WRAPPERS and all(place is None for place in located)


def _inside_subprocess() -> bool:
    """Whether subprocess.Popen is starting this child: its own audit event recorded it."""
    frame = sys._getframe(1)
    while frame is not None:
        code = frame.f_code
        if code.co_name == "_execute_child" and os.path.basename(code.co_filename) == "subprocess.py":
            return True
        frame = frame.f_back
    return False


def _is_unaudited_launcher(code: CodeType) -> bool:
    if code.co_qualname not in _UNAUDITED_LAUNCHERS:
        return False
    package = os.path.basename(os.path.dirname(code.co_filename))
    return package == _UNAUDITED_LAUNCHERS[code.co_qualname]


def _testpaths(config: pytest.Config) -> list[Path]:
    root = config.rootpath
    found: list[Path] = []
    for entry in config.getini("testpaths"):
        pattern = str(root / entry)
        magic = any(char in entry for char in "*?[")
        matches = glob.glob(pattern, recursive=True) if magic else [pattern]
        found += [Path(match).resolve() for match in sorted(matches) if os.path.exists(match)]
    return found or [root.resolve()]


def narrowed_invocation(config: pytest.Config) -> list[str]:
    """Why the command line selects less than the configured suite, if it does."""
    gaps = sorted(
        {
            f"option:{word.split('=', 1)[0]}"
            for word in config.invocation_params.args
            if word.split("=", 1)[0] in _NARROWING_OPTIONS
        }
    )
    if getattr(getattr(config, "args_source", None), "name", "") == "TESTPATHS":
        return gaps
    here = Path(config.invocation_params.dir)
    if any("::" in arg for arg in config.args):
        gaps.append("explicit_test_ids")
    targets = [(here / arg.split("::", 1)[0]).resolve() for arg in config.args]
    if not all(any(t == p or t in p.parents for t in targets) for p in _testpaths(config)):
        gaps.append("explicit_paths")
    return gaps


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    staged.write_bytes((json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode())
    os.replace(staged, path)


def shard_directory(root: Path) -> Path:
    return _select.map_directory(root) / _select.SHARD_DIRECTORY_NAME


def clear_shards(root: Path) -> None:
    directory = shard_directory(root)
    if directory.is_dir():
        for shard in directory.glob("*.json"):
            shard.unlink()


class MergeResult(NamedTuple):
    """The map written by :func:`merge_shards`, or why the previous map was kept."""

    map_path: Path | None
    reason: str | None


def merge_shards(
    root: Path, gaps: Iterable[str] = (), reported: Iterable[str] | None = None
) -> MergeResult:
    """Merge every shard into the map, but only from a session that ran the whole suite.

    ``gaps`` are the controller's reasons the session was partial; each shard adds its
    worker's, and a ``reported`` test with no shard row (a crashed worker) is one too.
    A partial map would forget every test that did not run -- the selector would then
    see them as unmapped at best -- so the previous map is left untouched and the
    reason returned.
    """
    directory = shard_directory(root)
    shards = sorted(directory.glob("*.json")) if directory.is_dir() else []
    if not shards:
        return MergeResult(None, "no_shards")
    rows: dict[str, _Sink] = {}
    files: dict[str, str] = {}
    import_time: set[str] = set()
    import_reads: dict[str, set[str]] = {}
    partial = set(gaps)
    for shard in shards:
        payload = json.loads(shard.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_id") != SHARD_SCHEMA_ID:
            raise RecorderError(f"shard_schema_mismatch:{shard.name}")
        partial.update(payload["gaps"])
        for nodeid, row in payload["tests"].items():
            files[nodeid] = row["file"]
            sink = rows.setdefault(nodeid, _Sink())
            sink.absorb(
                _Sink(set(row["code"]), set(row["reads"]), set(row["children"]), int(row["incomplete"]))
            )
        import_time.update(payload["import_time"])
        for data_path, modules in payload["import_reads"].items():
            import_reads.setdefault(data_path, set()).update(modules)
    unrecorded = set(reported or ()) - set(rows)
    if unrecorded:
        partial.add(f"unrecorded_tests:{len(unrecorded)}")
    if partial:
        return MergeResult(None, "partial_session:" + ",".join(sorted(partial)))
    objects = sorted({item for sink in rows.values() for item in sink.code})
    index = {item: position for position, item in enumerate(objects)}
    referenced: set[str] = {item.split("::", 1)[0] for item in import_time} | set(import_reads)
    tests: dict[str, Any] = {}
    for nodeid in sorted(rows):
        row = rows[nodeid].row(files[nodeid])
        row["code"] = [index[item] for item in row["code"]]
        tests[nodeid] = row
        referenced.add(files[nodeid])
        referenced.update(item.split("::", 1)[0] for item in rows[nodeid].code)
        referenced.update(rows[nodeid].reads)
        referenced.update(rows[nodeid].children - {_select.WHOLE_REPOSITORY})
    digests = {}
    for path in sorted(referenced):
        target = Path(root) / path
        digests[path] = _select.content_digest(target.read_bytes() if target.is_file() else None)
    map_path = _select.map_directory(root) / _select.MAP_FILE_NAME
    _write_json(
        map_path,
        {
            "schema_id": _select.SCHEMA_ID,
            "objects": objects,
            "tests": tests,
            "digests": digests,
            "import_time": sorted(import_time),
            "import_reads": {path: sorted(import_reads[path]) for path in sorted(import_reads)},
        },
    )
    return MergeResult(map_path, None)


class ImpactRecorder:
    """The plugin object: one per pytest process, registered by :func:`pytest_configure`."""

    def __init__(self, root: Path, worker_id: str | None) -> None:
        self.root = Path(root)
        self.worker_id = worker_id
        self._roots = tuple(dict.fromkeys((os.path.abspath(root), os.path.realpath(root))))
        own = (repo_relative(self._roots, path) for path in (__file__, _select.__file__))
        self._own = frozenset(path for path in own if path is not None)
        # co_filename -> repository path or None: one entry per distinct code file this
        # process executes, so it is bounded by the files the process loaded.
        self._code_paths: dict[str, str | None] = {}
        self._windows: list[_Sink] = []
        self._baseline: frozenset[int] = frozenset()
        self._open_tests: dict[str, _Sink] = {}
        self._fixtures: dict[tuple[str, str], _Sink] = {}
        self._tests: dict[str, dict[str, Any]] = {}
        # Process-wide: what ran while a repository module body was on the stack.
        self._import_code: set[str] = set()
        self._import_reads: dict[str, set[str]] = {}
        self._tool_id: int | None = None
        self._in_hook = False
        # Session completeness: the map is merged only from a run of the whole suite.
        self._deselected = 0
        self._collection_errors = 0
        self._reported: set[str] = set()
        self.merge_result: MergeResult | None = None

    # -- monitoring -------------------------------------------------------------------

    def start(self) -> None:
        monitoring = sys.monitoring
        for tool_id in _TOOL_IDS:
            if monitoring.get_tool(tool_id) is None:
                monitoring.use_tool_id(tool_id, TOOL_NAME)
                break
        else:
            raise pytest.UsageError(
                f"test_impact_recorder: every sys.monitoring tool id in {_TOOL_IDS} is taken"
            )
        self._tool_id = tool_id
        monitoring.register_callback(tool_id, monitoring.events.PY_START, self._on_start)
        monitoring.set_events(tool_id, monitoring.events.PY_START)
        sys.addaudithook(self._audit)

    def stop(self) -> None:
        """Release the monitoring tool id; the audit hook stays but records nothing."""
        self._windows.clear()
        if self._tool_id is None:
            return
        monitoring = sys.monitoring
        monitoring.set_events(self._tool_id, 0)
        monitoring.register_callback(self._tool_id, monitoring.events.PY_START, None)
        monitoring.free_tool_id(self._tool_id)
        self._tool_id = None

    def _code_path(self, filename: str) -> str | None:
        known = self._code_paths.get(filename, _UNSEEN)
        if known is not _UNSEEN:
            return known  # type: ignore[return-value]
        found = None
        if filename and not filename.startswith("<"):
            try:
                found = repo_relative(self._roots, filename)
            except ValueError:
                found = None
            if found in self._own:
                found = None
        self._code_paths[filename] = found
        return found

    def _module_frames(self) -> frozenset[int]:
        frame = sys._getframe(1)
        found = set()
        while frame is not None:
            code = frame.f_code
            if code.co_name == "<module>" and self._code_path(code.co_filename) is not None:
                found.add(id(frame))
            frame = frame.f_back
        return frozenset(found)

    def _importing_module(self) -> str | None:
        """The innermost repository module body running above the window baseline, if any."""
        frame = sys._getframe(1)
        while frame is not None:
            code = frame.f_code
            if code.co_name == "<module>" and id(frame) not in self._baseline:
                path = self._code_path(code.co_filename)
                if path is not None:
                    return path
            frame = frame.f_back
        return None

    def _on_start(self, code: CodeType, offset: int) -> object:
        path = self._code_path(code.co_filename)
        if path is None:
            if self._windows and _is_unaudited_launcher(code):
                self._windows[-1].children.add(_select.WHOLE_REPOSITORY)
            return sys.monitoring.DISABLE
        if code.co_name == "<module>":
            if self._windows:  # a lazy import: its body may call what this window already disabled
                sys.monitoring.restart_events()
            return sys.monitoring.DISABLE
        name = f"{path}::{function_qualname(code.co_qualname)}"
        if self._importing_module() is not None:
            self._import_code.add(name)
            return None if self._windows else sys.monitoring.DISABLE
        if not self._windows:
            return sys.monitoring.DISABLE
        self._windows[-1].code.add(name)
        return sys.monitoring.DISABLE

    def _enter(self, sink: _Sink) -> None:
        self._baseline = self._module_frames()
        self._windows.append(sink)
        sys.monitoring.restart_events()

    def _leave(self) -> None:
        self._windows.pop()
        if not self._windows:
            self._baseline = frozenset()
        sys.monitoring.restart_events()

    # -- audit events -----------------------------------------------------------------

    def _audit(self, event: str, args: tuple[Any, ...]) -> None:
        if event != "open" and event not in _CHILD_EVENTS:
            return
        if self._in_hook or (event != "open" and not self._windows):
            return  # an open outside any window can still be an import-time read
        self._in_hook = True
        sink = self._windows[-1] if self._windows else None
        try:
            if event == "open":
                self._on_open(sink, args)
            elif sink is not None and event == "subprocess.Popen":
                self._on_popen(sink, args)
            elif sink is not None and not _inside_subprocess():
                sink.children.add(_select.WHOLE_REPOSITORY)  # os.spawn*, os.system, fork, ...
        except Exception:  # noqa: BLE001 -- raising here would fail the audited call itself
            if sink is not None:
                sink.errors += 1  # the test is then recorded incomplete and always selected
        finally:
            self._in_hook = False

    def _on_open(self, sink: _Sink | None, args: tuple[Any, ...]) -> None:
        path, mode, flags = args
        if isinstance(path, int) or _writes(mode, flags):
            return
        text = os.fsdecode(path)
        if os.path.splitext(text)[1].lower() in _CODE_SUFFIXES:
            return
        found = repo_relative(self._roots, text)
        if found is None or not os.path.isfile(text):
            return
        module = self._importing_module()
        if module is not None:
            self._import_reads.setdefault(found, set()).add(module)
        elif sink is not None:
            sink.reads.add(found)

    def _on_popen(self, sink: _Sink, args: tuple[Any, ...]) -> None:
        executable, argv, cwd, env = args
        if isinstance(argv, (str, bytes)):
            words = _split_command_line(os.fsdecode(argv))
        else:
            words = [os.fsdecode(word) for word in argv]
        program = os.fsdecode(executable) if executable is not None else (words[0] if words else "")
        directory = os.fsdecode(cwd) if cwd is not None else os.getcwd()
        if _is_python(program):
            sink.children.update(self.child_entries(words[1:], directory))
        elif runs_unseen_code(program, words[1:], directory, env):
            sink.children.add(_select.WHOLE_REPOSITORY)

    def _exists(self, path: str) -> bool:
        return (self.root / path).is_file()

    def child_entries(self, args: list[str], cwd: str) -> set[str]:
        """The repository files a ``python <args>`` child starts from.

        A child that cannot be narrowed to repository files can still import repository
        code, so it is the whole repository. Only a child with no entry point at all
        (``python -V``) is empty.
        """
        index = 0
        while index < len(args):
            word = args[index]
            value = args[index + 1] if index + 1 < len(args) else ""
            if word in ("-m", "-c") or (word[:2] in ("-m", "-c") and not word.startswith("--")):
                payload = value if len(word) == 2 else word[2:]
                if word.startswith("-m"):
                    return set(_select.resolve_module(payload, self._exists)) or {_select.WHOLE_REPOSITORY}
                scan = _select.scan_imports(payload, self._exists)
                if scan is None or scan.dynamic or scan.opaque:  # it imports beyond its statements
                    return {_select.WHOLE_REPOSITORY}
                return set(scan.static) or {_select.WHOLE_REPOSITORY}
            if word in _OPTIONS_WITH_VALUE:
                index += 2
                continue
            if word == "-":
                return {_select.WHOLE_REPOSITORY}
            if word.startswith("-"):
                if word in _EXITS_BEFORE_RUNNING:
                    return set()
                index += 1
                continue
            script = repo_relative(self._roots, os.path.join(cwd, word))
            return {script} if script is not None and script.endswith(".py") else {_select.WHOLE_REPOSITORY}
        return {_select.WHOLE_REPOSITORY}  # no script and no -c/-m: the program comes from stdin

    # -- pytest hooks -----------------------------------------------------------------

    def _test_window(self, item: pytest.Item) -> Generator[None, object, object]:
        self._enter(self._open_tests.setdefault(item.nodeid, _Sink()))
        try:
            return (yield)
        finally:
            self._leave()

    pytest_runtest_setup = pytest.hookimpl(wrapper=True)(_test_window)
    pytest_runtest_call = pytest.hookimpl(wrapper=True)(_test_window)

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(
        self, fixturedef: pytest.FixtureDef[Any], request: pytest.FixtureRequest
    ) -> Generator[None, object, object]:
        if fixturedef.scope == "function":
            return (yield)
        key = (fixturedef.baseid, fixturedef.argname)
        arm_shield = self._shield_finalizers(fixturedef)
        self._fixtures[key] = _Sink()
        self._enter(self._fixtures[key])
        try:
            result = yield
        finally:
            self._leave()
        arm_shield()
        return result

    def _shield_finalizers(self, fixturedef: pytest.FixtureDef[Any]) -> Callable[[], None]:
        """Keep a wider fixture's finalizers out of the test that happens to be running.

        Finalizers run last-in-first-out, so ``close`` is registered before the fixture
        body and the returned callable registers ``open_window`` after it. A wider
        fixture finishes after whichever test a worker ran last, or inside the setup of
        the next parameter, so what it runs is attributed to no test.
        """
        opened: list[bool] = []

        def close() -> None:
            if opened:
                opened.clear()
                self._leave()

        def open_window() -> None:
            opened.append(True)
            self._enter(_Sink())

        fixturedef.addfinalizer(close)
        return lambda: fixturedef.addfinalizer(open_window)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(
        self, item: pytest.Item, call: pytest.CallInfo[None]
    ) -> Generator[None, object, object]:
        if call.excinfo is not None and item.nodeid in self._open_tests:
            self._open_tests[item.nodeid].errors += 1  # it stopped before its tail ran
        return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_teardown(
        self, item: pytest.Item, nextitem: pytest.Item | None
    ) -> Generator[None, object, object]:
        sink = self._open_tests.setdefault(item.nodeid, _Sink())
        self._enter(sink)
        try:
            return (yield)
        except BaseException:
            sink.errors += 1  # makereport for this phase runs after the row is written
            raise
        finally:
            self._leave()
            self._write_row(item, sink)

    def _write_row(self, item: pytest.Item, sink: _Sink) -> None:
        self._open_tests.pop(item.nodeid, None)
        names = set(getattr(item, "fixturenames", ()))
        request = getattr(item, "_request", None)
        if request:
            names.update(request.fixturenames)
        for (baseid, argname), fixture in self._fixtures.items():
            if argname in names and _fixture_applies(baseid, item.nodeid) and _resolves_to(item, argname, baseid):
                sink.absorb(fixture)
        self._tests[item.nodeid] = sink.row(item.nodeid.split("::", 1)[0])

    def pytest_deselected(self, items: Sequence[pytest.Item]) -> None:
        self._deselected += len(items)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self._collection_errors += 1

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self._reported.add(report.nodeid)  # on the controller: every test, from any worker

    def _session_gaps(self, session: pytest.Session, exitstatus: int) -> list[str]:
        """Why this process ran less than the whole suite; empty for a complete run."""
        gaps = []
        if self._deselected:
            gaps.append(f"deselected:{self._deselected}")
        if self._collection_errors:
            gaps.append(f"collection_errors:{self._collection_errors}")
        if exitstatus not in _COMPLETE_EXIT_STATUSES:
            gaps.append(f"exit_status:{exitstatus}")
        if getattr(session, "shouldstop", False) or getattr(session, "shouldfail", False):
            gaps.append("stopped_early")
        option = session.config.option
        gaps.extend(gap for dest, gap in _NO_RUN_OPTIONS if getattr(option, dest, False))
        if self.worker_id is None:
            gaps.extend(narrowed_invocation(session.config))
        return gaps

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self.stop()
        gaps = self._session_gaps(session, int(exitstatus))
        if self._tests or self.worker_id is not None:  # a worker's gaps must reach the merge
            shard = shard_directory(self.root) / f"{self.worker_id or 'controller'}.json"
            payload = {
                "schema_id": SHARD_SCHEMA_ID,
                "tests": self._tests,
                "gaps": gaps,
                "import_time": sorted(self._import_code),
                "import_reads": {k: sorted(v) for k, v in self._import_reads.items()},
            }
            _write_json(shard, payload)
        if self.worker_id is None:
            self.merge_result = merge_shards(self.root, gaps, self._reported)

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        result = self.merge_result
        if result is not None and result.reason is not None:
            terminalreporter.write_line(
                f"test_impact: map not written ({result.reason}); the previous map is unchanged"
            )


def _resolves_to(item: pytest.Item, argname: str, baseid: str) -> bool:
    """Whether this test runs the definition of ``argname`` at ``baseid``, not one overriding it."""
    info = getattr(item, "_fixtureinfo", None)
    definitions = info.name2fixturedefs.get(argname) if info is not None else None
    if not definitions:  # requested only through getfixturevalue: nothing is bound statically
        return True
    for definition in reversed(definitions):  # most specific first
        if definition.baseid == baseid:
            return True
        if argname not in definition.argnames:  # it does not request the definition it overrides
            return False
    return False


def _fixture_applies(baseid: str, nodeid: str) -> bool:
    return not baseid or nodeid == baseid or nodeid.startswith((f"{baseid}/", f"{baseid}::"))


def _started_recorder(config: pytest.Config) -> ImpactRecorder:
    recorder = config.pluginmanager.get_plugin(_PLUGIN_NAME)
    if isinstance(recorder, ImpactRecorder):
        return recorder
    recorder = ImpactRecorder(config.rootpath, None)
    recorder.start()
    config.pluginmanager.register(recorder, _PLUGIN_NAME)
    return recorder


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config) -> None:
    # The initial conftest.py files import repository modules before pytest_configure,
    # so import-time code would go unrecorded if recording started there. xdist sets
    # workerinput only later, so pytest_configure names the worker.
    _started_recorder(early_config)


def pytest_configure(config: pytest.Config) -> None:
    workerinput = getattr(config, "workerinput", None)
    worker_id = str(workerinput["workerid"]) if workerinput else None
    if worker_id is None:
        clear_shards(config.rootpath)
    _started_recorder(config).worker_id = worker_id


def pytest_unconfigure(config: pytest.Config) -> None:
    recorder = config.pluginmanager.get_plugin(_PLUGIN_NAME)
    if isinstance(recorder, ImpactRecorder):
        recorder.stop()
