"""Select the tests a change can affect, from a recorded per-test map (NF-2026-02054, card A).

``python -m aiworkhub.test_impact_select --base <rev> [--head <rev>] [--paths ...] [--json]``

The map is written by the opt-in pytest plugin :mod:`aiworkhub.test_impact_recorder`
under ``.aiworkhub/runtime/test_impact/map.json``. ``git diff -U0`` gives the changed
lines of every changed file, against the working tree (plus untracked files) or
against ``--head``; each changed Python line is mapped to the innermost function
whose *body* contains it, and the tests are selected by these rules:

* a change only inside function bodies selects the tests that executed those functions
  (``executes:<path>::<qualname>``);
* any other change to a Python file -- a ``def`` line, a decorator, a class body, an
  import, a module constant, an added or deleted file -- or a body edit of a function
  that ran at import time is module-level and selects every test that imported the
  module (``imports:<path>``): the test file or a file whose code the test executed
  reaches it through the static import graph;
* a changed non-Python file selects the tests that opened it (``reads:<path>``) and the
  importers of the modules that opened it at import time (``import_reads:<path>``);
* a changed or new test file is selected whole (``changed_test`` / ``new_test``), a
  test file the map does not know is selected whole (``unmapped``), a test whose
  recorded files differ from ``--base`` is selected (``stale:<path>``), as are the
  importers of a stale import-time file, a test whose recording was incomplete is
  selected (``incomplete_record``), and a test that launched a child interpreter
  running repository code is selected when the import closure of the child's entry
  modules changed (``subprocess:<path>``);
* a changed ``conftest.py``, ``pyproject.toml``, ``pytest.ini``, ``setup.cfg``,
  ``tox.ini``, ``test_impact_recorder.py`` or ``test_impact_select.py``, or a
  missing/unreadable/foreign map, sets ``full_suite_required`` with its reason instead
  of selecting anything. An unknown never selects less.

Comment-only and blank-line changes select nothing. This module owns the map's
vocabulary (location, schema, digest, module resolution); the recorder imports it.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import re
import subprocess
import sys
import tokenize
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_ID = "aiworkhub.test_impact.map.v2"
SELECTION_SCHEMA_ID = "aiworkhub.test_impact.selection.v1"
MAP_DIRECTORY_PARTS = (".aiworkhub", "runtime", "test_impact")
MAP_FILE_NAME = "map.json"
SHARD_DIRECTORY_NAME = "shards"
# A child entry that could not be narrowed: any Python change re-runs the test.
WHOLE_REPOSITORY = "*"
# Files that change how pytest collects or configures every test. The recorder and
# this selector are here too: they do not record themselves, and they define the map.
GLOBAL_INVALIDATORS = frozenset(
    {
        "conftest.py",
        "pyproject.toml",
        "pytest.ini",
        "setup.cfg",
        "tox.ini",
        "test_impact_recorder.py",
        "test_impact_select.py",
    }
)
_IGNORED_TOP_LEVEL = frozenset({".aiworkhub", ".git"})
# pytest's default python_files (test_*.py, *_test.py) under the tests/ testpath.
_TEST_MODULE = re.compile(r"tests/(?:[^/]+/)*(?:test_[^/]*|[^/]*_test)\.py")
_DIFF_HEADER = re.compile(r"diff --git a/(.+) b/(.+)")
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_INERT_TOKENS = frozenset(
    {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
    }
)
_GIT_DETAIL_CHARS = 400


class SelectionError(Exception):
    """A named reason the selector cannot answer at all; the CLI exits 2 with it."""


def map_directory(root: Path) -> Path:
    """The only directory the recorder writes: ``<root>/.aiworkhub/runtime/test_impact``."""
    return Path(root).joinpath(*MAP_DIRECTORY_PARTS)


def content_digest(data: bytes | None) -> str:
    """Line-ending independent digest of file content; ``missing`` for an absent file."""
    if data is None:
        return "missing"
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def is_test_module(path: str) -> bool:
    """True for a repository path pytest collects as a test module."""
    return _TEST_MODULE.fullmatch(path) is not None


def resolve_module(
    name: str,
    exists: Callable[[str], bool],
    *,
    origin: str | None = None,
    level: int = 0,
) -> tuple[str, ...]:
    """Repository files executed by importing ``name``: package ``__init__``s, then the module.

    Absolute imports are searched under ``src/`` and the repository root, and -- for a
    file under ``tests/``, whose directory pytest puts on ``sys.path`` -- the importing
    file's directory first. ``exists`` answers for repository-relative paths.
    """
    segments = [part for part in name.split(".") if part]
    if level:
        if origin is None:
            return ()
        package = origin.split("/")[:-1]
        if level - 1 > len(package):
            return ()
        bases = ["/".join(package[: len(package) - (level - 1)])]
    else:
        bases = []
        if origin is not None and origin.startswith("tests/"):
            bases.append(origin.rsplit("/", 1)[0])
        bases += ["src", ""]
    for base in bases:
        found = _resolve_under(base, segments, exists)
        if found:
            return found
    return ()


def _resolve_under(base: str, segments: Sequence[str], exists: Callable[[str], bool]) -> tuple[str, ...]:
    if not segments:
        init = f"{base}/__init__.py" if base else "__init__.py"
        return (init,) if exists(init) else ()
    found: list[str] = []
    current = base
    for segment in segments:
        current = f"{current}/{segment}" if current else segment
        if exists(f"{current}/__init__.py"):
            found.append(f"{current}/__init__.py")
        elif exists(f"{current}.py"):
            found.append(f"{current}.py")
            break
    return tuple(found)


def imported_files(
    source: bytes | str, exists: Callable[[str], bool], *, origin: str | None = None
) -> set[str] | None:
    """Repository files ``source`` imports anywhere in its body; None when it does not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.update(resolve_module(alias.name, exists, origin=origin))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            found.update(resolve_module(module, exists, origin=origin, level=node.level))
            for alias in node.names:
                if alias.name != "*":
                    dotted = f"{module}.{alias.name}" if module else alias.name
                    found.update(resolve_module(dotted, exists, origin=origin, level=node.level))
    if origin is not None:
        found.discard(origin)
    return found


def _reach(starts: Iterable[str], edges: Mapping[str, Iterable[str]]) -> set[str]:
    seen = set(starts)
    pending = list(seen)
    while pending:
        for target in edges.get(pending.pop(), ()):
            if target not in seen:
                seen.add(target)
                pending.append(target)
    return seen


class ImportGraph:
    """Static import edges between the repository's Python files."""

    def __init__(self, sources: Mapping[str, bytes], also_exists: Iterable[str] = ()) -> None:
        # also_exists: files gone from ``sources`` whose importers must still be found.
        exists = {*sources, *also_exists}.__contains__
        self._edges: dict[str, set[str]] = {
            path: imported_files(data, exists, origin=path) or set()
            for path, data in sources.items()
        }
        self._reverse: dict[str, set[str]] = {}
        for path, targets in self._edges.items():
            for target in targets:
                self._reverse.setdefault(target, set()).add(path)

    def importers(self, targets: Iterable[str]) -> set[str]:
        """``targets`` and every file that imports one of them, transitively."""
        return _reach(targets, self._reverse)

    def closure(self, starts: Iterable[str]) -> set[str]:
        """``starts`` and every file they import, transitively."""
        return _reach(starts, self._edges)


@dataclass
class ChangedFile:
    """One changed path with its ``-U0`` hunk ranges as (start, count) per side."""

    path: str
    status: str  # "added" | "deleted" | "modified"
    old_ranges: list[tuple[int, int]] = field(default_factory=list)
    new_ranges: list[tuple[int, int]] = field(default_factory=list)
    binary: bool = False


def _diff_path(value: str) -> str | None:
    value = value.rstrip("\t")
    if value == "/dev/null":
        return None
    return value[2:] if value[:2] in ("a/", "b/") else value


def parse_diff(text: str) -> list[ChangedFile]:
    """Parse ``git diff -U0 --no-renames`` output into changed files and hunk ranges."""
    changes: list[ChangedFile] = []
    current: ChangedFile | None = None
    in_header = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            match = _DIFF_HEADER.fullmatch(line)
            current = ChangedFile(match.group(2) if match else line[11:], "modified")
            changes.append(current)
            in_header = True
            continue
        if current is None:
            continue
        if line.startswith("@@"):
            in_header = False
            hunk = _HUNK.match(line)
            if hunk is not None:
                old_count = 1 if hunk.group(2) is None else int(hunk.group(2))
                new_count = 1 if hunk.group(4) is None else int(hunk.group(4))
                current.old_ranges.append((int(hunk.group(1)), old_count))
                current.new_ranges.append((int(hunk.group(3)), new_count))
        elif in_header:
            if line.startswith("new file mode"):
                current.status = "added"
            elif line.startswith("deleted file mode"):
                current.status = "deleted"
            elif line.startswith("Binary files "):
                current.binary = True
            elif line.startswith("+++ ") and _diff_path(line[4:]):
                current.path = _diff_path(line[4:]) or current.path
            elif line.startswith("--- ") and current.status == "deleted":
                current.path = _diff_path(line[4:]) or current.path
    return changes


def _git(root: Path, args: Sequence[str], data: bytes | None = None) -> bytes:
    argv = ["git", "-c", "core.quotePath=false", *args]
    try:
        completed = subprocess.run(argv, cwd=root, input=data, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise SelectionError("git_unavailable") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()[-_GIT_DETAIL_CHARS:]
        raise SelectionError(f"git_failed:{args[0]}:{completed.returncode}:{detail}")
    return completed.stdout


def _git_names(root: Path, args: Sequence[str]) -> list[str]:
    raw = _git(root, args).decode("utf-8", "surrogateescape")
    return [name for name in raw.split("\0") if name]


def _ignored(path: str) -> bool:
    parts = path.split("/")
    return parts[0] in _IGNORED_TOP_LEVEL or "__pycache__" in parts


def collect_changes(
    root: Path, base: str, head: str | None = None, paths: Sequence[str] = ()
) -> list[ChangedFile]:
    """Changed files between ``base`` and ``head`` (default: working tree plus untracked)."""
    # Explicit prefixes: diff.noprefix and diff.mnemonicPrefix would rewrite the headers parse_diff reads.
    argv = ["diff", "-U0", "--no-renames", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/", base]
    if head is not None:
        argv.append(head)
    text = _git(root, [*argv, "--", *paths]).decode("utf-8", "surrogateescape")
    changes = parse_diff(text)
    if head is None:
        untracked = ["ls-files", "-z", "--others", "--exclude-standard", "--", *paths]
        changes += [ChangedFile(name, "added") for name in _git_names(root, untracked)]
    return [change for change in changes if not _ignored(change.path)]


def read_blobs(root: Path, rev: str, paths: Sequence[str]) -> dict[str, bytes | None]:
    """Content of each path at ``rev`` through one ``git cat-file --batch``; None if absent."""
    if not paths:
        return {}
    output = _git(root, ["cat-file", "--batch"], "".join(f"{rev}:{p}\n" for p in paths).encode())
    blobs: dict[str, bytes | None] = {}
    position = 0
    for path in paths:
        end = output.index(b"\n", position)
        header = output[position:end].split()
        position = end + 1
        if header[-1] == b"missing":  # "<rev>:<path> missing", and no payload follows
            blobs[path] = None
            continue
        if len(header) != 3 or header[1] != b"blob":
            blobs[path] = None
            if len(header) == 3:
                position += int(header[2]) + 1
            continue
        size = int(header[2])
        blobs[path] = output[position : position + size]
        position += size + 1
    return blobs


@dataclass(frozen=True)
class PythonChange:
    """What a change to one Python file touched: function bodies, or module-level code."""

    functions: frozenset[str]
    module_level: bool


def _header_end(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    end = node.lineno
    for part in ast.walk(node.args):
        end = max(end, getattr(part, "end_lineno", None) or end)
    if node.returns is not None:
        end = max(end, node.returns.end_lineno or end)
    return end


def function_scopes(tree: ast.AST) -> list[tuple[int, int, str]]:
    """(first body line, last line, co_qualname) of every function with a separate body."""
    scopes: list[tuple[int, int, str]] = []
    pending: list[tuple[ast.AST, str]] = [(tree, "")]
    while pending:
        node, prefix = pending.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = prefix + child.name
                body_start = child.body[0].lineno
                if body_start > _header_end(child):
                    scopes.append((body_start, child.end_lineno or body_start, qualname))
                pending.append((child, f"{qualname}.<locals>."))
            elif isinstance(child, ast.ClassDef):
                pending.append((child, f"{prefix}{child.name}."))
            else:
                pending.append((child, prefix))
    return scopes


def _inert_lines(source: bytes) -> set[int]:
    code_lines: set[int] = set()
    try:
        for token in tokenize.tokenize(io.BytesIO(source).readline):
            if token.type not in _INERT_TOKENS:
                code_lines.update(range(token.start[0], token.end[0] + 1))
    except (tokenize.TokenError, SyntaxError):
        return set()
    return set(range(1, source.count(b"\n") + 2)) - code_lines


def _innermost(scopes: Sequence[tuple[int, int, str]], line: int) -> str | None:
    best: tuple[int, int, str] | None = None
    for scope in scopes:
        if scope[0] <= line <= scope[1] and (best is None or scope[0] > best[0]):
            best = scope
    return None if best is None else best[2]


def classify_python_change(
    old: bytes | None, new: bytes | None, change: ChangedFile
) -> PythonChange:
    """Map the changed lines of both sides to function bodies or to module-level code."""
    if change.binary or old is None or new is None:
        return PythonChange(frozenset(), True)
    functions: set[str] = set()
    module_level = False
    for source, ranges in ((old, change.old_ranges), (new, change.new_ranges)):
        lines = [line for start, count in ranges for line in range(start, start + count)]
        if not lines:
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return PythonChange(frozenset(functions), True)
        scopes = function_scopes(tree)
        inert = _inert_lines(source)
        for line in lines:
            if line in inert:
                continue
            owner = _innermost(scopes, line)
            if owner is None:
                module_level = True
            else:
                functions.add(owner)
    return PythonChange(frozenset(functions), module_level)


@dataclass(frozen=True)
class _Recorded:
    file: str
    code: frozenset[str]
    code_files: frozenset[str]
    reads: frozenset[str]
    children: frozenset[str]
    incomplete: bool


@dataclass(frozen=True)
class Selection:
    """The selected tests with their reasons, or why the full suite is required."""

    full_suite_required: bool
    full_suite_reasons: tuple[str, ...]
    selected: dict[str, tuple[str, ...]]
    changed_paths: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_id": SELECTION_SCHEMA_ID,
            "full_suite_required": self.full_suite_required,
            "full_suite_reasons": list(self.full_suite_reasons),
            "changed_paths": list(self.changed_paths),
            "selected_count": len(self.selected),
            "selected": [
                {"test": test, "reasons": list(reasons)}
                for test, reasons in sorted(self.selected.items())
            ],
        }

    def render(self) -> str:
        if self.full_suite_required:
            return "full_suite_required: " + "; ".join(self.full_suite_reasons)
        lines = [f"{test}\t{'; '.join(reasons)}" for test, reasons in sorted(self.selected.items())]
        lines.append(f"selected {len(self.selected)} test(s) for {len(self.changed_paths)} changed path(s)")
        return "\n".join(lines)


def load_map(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """The recorded map, or None with the named reason it cannot be used."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, "map_missing"
    except OSError as exc:
        return None, f"map_unreadable:{type(exc).__name__}"
    try:
        payload = json.loads(raw)
    except ValueError:
        return None, "map_unreadable:invalid_json"
    if not isinstance(payload, dict) or payload.get("schema_id") != SCHEMA_ID:
        return None, "map_schema_mismatch"
    return payload, None


class _Selector:
    def __init__(self, root: Path, base: str, head: str | None, payload: Mapping[str, Any]):
        self.root, self.base, self.head = root, base, head
        objects = payload["objects"]
        self.tests: dict[str, _Recorded] = {}
        for nodeid, row in payload["tests"].items():
            code = frozenset(objects[index] for index in row["code"])
            self.tests[nodeid] = _Recorded(
                file=str(row["file"]),
                code=code,
                code_files=frozenset(item.split("::", 1)[0] for item in code),
                reads=frozenset(row["reads"]),
                children=frozenset(row["children"]),
                incomplete=bool(row["incomplete"]),
            )
        self.digests: dict[str, str] = dict(payload["digests"])
        self.import_code = frozenset(payload["import_time"])
        self.import_code_files = frozenset(item.split("::", 1)[0] for item in self.import_code)
        self.import_reads: dict[str, frozenset[str]] = {
            path: frozenset(modules) for path, modules in payload["import_reads"].items()
        }
        self.new_sources: Mapping[str, bytes] = {}
        self.picked: dict[str, set[str]] = {}

    def _pick(self, test: str, reason: str) -> None:
        self.picked.setdefault(test, set()).add(reason)

    def _new_python_sources(self) -> dict[str, bytes]:
        if self.head is not None:
            names = _git_names(self.root, ["ls-tree", "-r", "-z", "--name-only", self.head])
            wanted = [name for name in names if name.endswith(".py") and not _ignored(name)]
            return {k: v for k, v in read_blobs(self.root, self.head, wanted).items() if v is not None}
        listing = ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
        sources: dict[str, bytes] = {}
        for name in _git_names(self.root, listing):
            if name.endswith(".py") and not _ignored(name) and (self.root / name).is_file():
                sources[name] = (self.root / name).read_bytes()
        return sources

    def run(self, changes: Sequence[ChangedFile], changed_paths: tuple[str, ...]) -> Selection:
        new_sources = self._new_python_sources()
        self.new_sources = new_sources
        graph = ImportGraph(new_sources, (c.path for c in changes if c.status == "deleted"))
        changed_python = {change.path for change in changes if change.path.endswith(".py")}
        wanted = sorted(set(self.digests) | {c.path for c in changes if c.status != "added"})
        base_blobs = read_blobs(self.root, self.base, wanted)
        for change in changes:
            self._apply_change(change, base_blobs, new_sources, graph)
        self._apply_children(graph, changed_python)
        self._apply_unmapped(new_sources, {c.path for c in changes if c.status == "added"})
        stale = {p for p, digest in self.digests.items() if content_digest(base_blobs.get(p)) != digest}
        for nodeid, test in self.tests.items():
            references = {test.file} | test.code_files | test.reads | test.children
            for path in sorted(references & stale):
                self._pick(test.file if path == test.file else nodeid, f"stale:{path}")
            if test.incomplete:
                self._pick(nodeid, "incomplete_record")
        for path in sorted(stale):
            modules = set(self.import_reads.get(path, ()))
            if path in self.import_code_files:
                modules.add(path)
            if modules:
                self._pick_importers(graph, modules, f"stale:{path}")
        return Selection(False, (), self._collapsed(), changed_paths)

    def _pick_importers(self, graph: ImportGraph, modules: Iterable[str], reason: str) -> None:
        importers = graph.importers(modules)
        for nodeid, test in self.tests.items():
            if test.file in importers or not importers.isdisjoint(test.code_files):
                self._pick(nodeid, reason)

    def _apply_change(
        self,
        change: ChangedFile,
        base_blobs: Mapping[str, bytes | None],
        new_sources: Mapping[str, bytes],
        graph: ImportGraph,
    ) -> None:
        path = change.path
        if is_test_module(path) and change.status != "deleted":
            self._pick(path, "new_test" if change.status == "added" else "changed_test")
        if not path.endswith(".py"):
            for nodeid, test in self.tests.items():
                if path in test.reads:
                    self._pick(nodeid, f"reads:{path}")
            if path in self.import_reads:
                self._pick_importers(graph, self.import_reads[path], f"import_reads:{path}")
            return
        old = None if change.status == "added" else base_blobs.get(path)
        result = classify_python_change(old, new_sources.get(path), change)
        for qualname in sorted(result.functions):
            target = f"{path}::{qualname}"
            for nodeid, test in self.tests.items():
                if target in test.code:
                    self._pick(nodeid, f"executes:{target}")
        if result.module_level or any(f"{path}::{q}" in self.import_code for q in result.functions):
            self._pick_importers(graph, [path], f"imports:{path}")

    def _apply_children(self, graph: ImportGraph, changed_python: set[str]) -> None:
        closures: dict[frozenset[str], set[str]] = {}
        for nodeid, test in self.tests.items():
            if not test.children:
                continue
            if WHOLE_REPOSITORY in test.children:
                hits = changed_python
            else:
                if test.children not in closures:
                    closures[test.children] = graph.closure(test.children)
                hits = closures[test.children] & changed_python
            for path in sorted(hits):
                self._pick(nodeid, f"subprocess:{path}")

    def _apply_unmapped(self, new_sources: Mapping[str, bytes], added: set[str]) -> None:
        mapped = {test.file for test in self.tests.values()}
        for path in sorted(new_sources):
            if is_test_module(path) and path not in mapped and path not in added:
                self._pick(path, "unmapped")

    def _collapsed(self) -> dict[str, tuple[str, ...]]:
        """Drop picks under a deleted file; a selected test file absorbs its test ids."""
        # pytest exits 4 on a path that does not exist, losing the whole selected run.
        picked = {k: v for k, v in self.picked.items() if k.split("::", 1)[0] in self.new_sources}
        merged: dict[str, set[str]] = {k: set(v) for k, v in picked.items() if "::" not in k}
        for test, reasons in picked.items():
            if "::" in test:
                owner = test.split("::", 1)[0]
                merged.setdefault(owner if owner in merged else test, set()).update(reasons)
        return {test: tuple(sorted(reasons)) for test, reasons in sorted(merged.items())}


def select_tests(
    root: Path,
    base: str,
    *,
    head: str | None = None,
    paths: Sequence[str] = (),
    map_path: Path | None = None,
) -> Selection:
    """Apply the selection rules to the diff ``base..head`` (or ``base..working tree``)."""
    root = Path(root)
    _git(root, ["rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"])
    changes = collect_changes(root, base, head, paths)
    changed_paths = tuple(sorted({change.path for change in changes}))
    reasons = [
        f"global_invalidator:{path}"
        for path in changed_paths
        if path.rsplit("/", 1)[-1] in GLOBAL_INVALIDATORS
    ]
    payload, problem = load_map(map_path or map_directory(root) / MAP_FILE_NAME)
    if problem is not None:
        reasons.append(problem)
    if reasons or payload is None:
        return Selection(True, tuple(reasons), {}, changed_paths)
    try:
        selector = _Selector(root, base, head, payload)
    except (KeyError, IndexError, TypeError, AttributeError):
        return Selection(True, ("map_schema_mismatch",), {}, changed_paths)
    return selector.run(changes, changed_paths)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aiworkhub.test_impact_select",
        description="Select the tests a change can affect from the recorded test impact map.",
    )
    parser.add_argument("--base", required=True, help="revision the change is measured from")
    parser.add_argument("--head", default=None, help="compare against this revision, not the working tree")
    parser.add_argument("--paths", nargs="*", default=[], help="limit the diff to these paths")
    parser.add_argument("--json", action="store_true", help="print the selection as JSON")
    parser.add_argument("--root", default=None, help="repository root (default: git toplevel)")
    parser.add_argument("--map", default=None, help="map file (default: the recorder's location)")
    args = parser.parse_args(argv)
    try:
        if args.root is not None:
            root = Path(args.root)
        else:
            toplevel = _git(Path.cwd(), ["rev-parse", "--show-toplevel"])
            root = Path(toplevel.decode("utf-8", "surrogateescape").strip())
        selection = select_tests(
            root,
            args.base,
            head=args.head,
            paths=args.paths,
            map_path=Path(args.map) if args.map else None,
        )
    except SelectionError as exc:
        print(f"test_impact_select: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(selection.to_json(), indent=2, sort_keys=True))
    else:
        print(selection.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
