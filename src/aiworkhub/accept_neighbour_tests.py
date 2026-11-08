"""Neighbour tests of a candidate's changed modules, run at accept (NF-2026-01814).

Card validation runs only the tests the card names, so an accepted card could
break a neighbouring test that only the full suite ran, and each such break
cost a later repair commit. ``accept_review`` therefore asks
:func:`select_neighbour_tests` for the tests Source Graph ``testmap`` relates to
the candidate's changed ``src/`` modules that the declared validation does not
already name, and runs them once on the combined tree with
:func:`run_neighbour_tests`.

The selection reuses :mod:`aiworkhub.card_contract_advice`'s testmap pass --
the same freshness check, query budget, payload parsing and score floor -- and
keeps no testmap client of its own. It selects only the files pytest collects by default
(test_*.py and *_test.py), and it never raises: an unavailable index or no
related test yields an empty selection with a named ``reason``, which the lane
records and which does not block acceptance. A selected run that fails, or that
returns no evidence, refuses the accept with ``neighbour_tests_failed:``, unless
every node still red after the serial rerun also fails on a full checkout of
canonical HEAD: that is recorded as ``base_red`` and does not refuse (NF-2026-02101).
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import accept_neighbour_nodes as _nodes
from . import card_contract_advice as _advice
from . import worker_workspace as _workspace
from .sdlc_attribution import is_test_path
from .toolchain_authority import RECEIPT_CARD_KEY
from .worker_workspace import ValidationRunError, WorkspaceError

# No commit history was readable when this was written, so recall at a lower cap
# is unmeasured; the default is the hard ceiling, which keeps every
# highest-scoring neighbour a smaller cap would also keep.
MAX_NEIGHBOUR_TESTS = 12
DEFAULT_NEIGHBOUR_LIMIT = MAX_NEIGHBOUR_TESTS
# Repository-wide invariants no testmap links to one module; they run whenever
# a src/ module changed and do not count against the neighbour cap.
INVARIANT_TESTS = ("tests/test_module_size_ratchet.py", "tests/test_os_dependency_boundary.py")
# NF-2026-02162: the MCP schema-freeze gates (tests/mcp_*.py) run as subprocesses of
# tests/test_mcp_contract_gates.py, so no call-graph lane links a tool-surface change to
# them. Measured: dfa2ea6d changed two tool input schemas in server.py and was accepted
# green while that gate went red (schema_fingerprints_match_frozen). The modules below are
# the ones the freeze scripts (mcp_client_smoke_contract_freeze.py, mcp_stdio_client_smoke.py,
# mcp_stdio_concurrent_client_smoke.py) fingerprint tools/list through: server.py registers
# the tools (``prepare_registration``, ``_stdio_schema_for``) and mcp_tool_metadata.py
# supplies the descriptions, parameter descriptions, aliases and annotations they carry.
# A change to either pins the gate file outside the neighbour cap.
CONTRACT_GATE_TEST = "tests/test_mcp_contract_gates.py"
CONTRACT_GATE_MODULES = ("src/aiworkhub/mcp_tool_metadata.py", "src/aiworkhub/server.py")
# NF-2026-01947: a test that loads a changed module by path ("<stem>.py") has no
# testmap import edge, and a NAME_MAX_LINES ratchet on a module with many related
# tests can rank below the neighbour cap. Both run whenever their module changed,
# outside the neighbour cap and under their own ceiling.
MAX_REFERENCING_TESTS = 12
REFERENCING_SCAN_MAX_BYTES = 1024 * 1024
_TESTS_DIR = "tests"
# The tail of a module-level ``<STEM>_MAX_LINES = <int>`` binding (optionally
# annotated); ``_stem_patterns`` puts the upper-cased changed stems in front of it.
_RATCHET_BINDING = r"_MAX_LINES[ \t]*(?::[^=\n]*)?=[ \t]*\d[\d_]*[ \t]*(?:#.*)?$"
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: a Windows symlink or junction
NEIGHBOUR_FAILURE_PREFIX = "neighbour_tests_failed:"
NEIGHBOUR_FAILURE_DETAIL_CHARS = 4000
MAX_REPORTED_FAILURES = 20
_FIRST_FAILURE_CHARS = 500
# A selected path becomes one argv token of ``python -m pytest``; anything
# needing quoting is not a test path this lane runs.
_TEST_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.py")
# pytest's default python_files (test_*.py, *_test.py): the only test files CI collects.
_COLLECTED_TEST_PATH = re.compile(r"(?:.*/)?(?:test_[^/]*|[^/]*_test)\.py")
_PYTEST_FAILURE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)
# A failed node id re-run as one argv token: a selected path, optionally ``::name``,
# with nothing a command line would need to quote.
_NODE_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.py(?:::[A-Za-z0-9_.:\[\]/,+=-]+)?")
# A neighbour run is its own one-command contract: the card's evidence roles and
# its toolchain receipt are bound to the card's own validation list.
_CARD_ONLY_KEYS = ("validation_roles", "work_kind", RECEIPT_CARD_KEY)


@dataclass(frozen=True)
class NeighbourSelection:
    """The ranked neighbour tests for one candidate, or why there are none."""

    tests: tuple[str, ...]
    reason: str
    limit: int
    production_paths: tuple[str, ...] = ()
    related_count: int = 0
    unavailable_paths: tuple[Mapping[str, str], ...] = field(default=())
    invariant: tuple[str, ...] = ()
    referencing: tuple[str, ...] = ()
    referencing_omitted: int = 0
    referencing_reason: str = ""
    # NF-2026-02102: "node" or "file_fallback"; empty when no candidate_root was given.
    selection_mode: str = ""
    node_count: int = 0
    fallback_reasons: tuple[Mapping[str, str], ...] = field(default=())
    # NF-2026-02162: the pinned MCP contract gate and why it was added.
    contract_gate: tuple[str, ...] = ()
    contract_gate_reason: str = ""

    def evidence(self) -> dict[str, Any]:
        ranked = (
            len(self.tests) - len(self.invariant) - len(self.contract_gate) - len(self.referencing)
        )
        record: dict[str, Any] = {
            "selected": list(self.tests),
            "limit": self.limit,
            "production_paths": list(self.production_paths),
            "related_count": self.related_count,
            "omitted": max(0, self.related_count - ranked),
        }
        if self.invariant:
            record["invariant"] = list(self.invariant)
        if self.contract_gate:
            record["contract_gate"] = list(self.contract_gate)
        if self.contract_gate_reason:
            record["contract_gate_reason"] = self.contract_gate_reason
        if self.referencing or self.referencing_omitted:
            record["referencing"] = list(self.referencing)
            record["referencing_omitted"] = self.referencing_omitted
        if self.referencing_reason:
            record["referencing_reason"] = self.referencing_reason
        if self.reason:
            record["reason"] = self.reason
        if self.unavailable_paths:
            record["unavailable_paths"] = [dict(row) for row in self.unavailable_paths]
        if self.selection_mode:
            record["selection_mode"] = self.selection_mode
            record["node_level"] = self.selection_mode == "node"
            record["node_count"] = self.node_count
            record["fallback_reasons"] = [dict(row) for row in self.fallback_reasons]
        return record


def _empty(reason: str, limit: int, **extra: Any) -> NeighbourSelection:
    return NeighbourSelection(tests=(), reason=reason, limit=limit, **extra)


def _bounded_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        return DEFAULT_NEIGHBOUR_LIMIT
    return max(0, min(limit, MAX_NEIGHBOUR_TESTS))


def _production_paths(changed_paths: Iterable[Any]) -> list[str]:
    paths = {_advice._normalize_path(path) for path in changed_paths}
    return sorted(
        path
        for path in paths
        if path.startswith("src/") and path.endswith(".py") and not is_test_path(path)
    )


def select_neighbour_tests(
    repo_root: Path | str,
    changed_paths: Iterable[Any],
    declared_commands: Sequence[str],
    *,
    limit: int = DEFAULT_NEIGHBOUR_LIMIT,
    candidate_root: Path | str | None = None,
) -> NeighbourSelection:
    """Rank the related tests of ``changed_paths`` that ``declared_commands`` miss.

    Highest testmap score first, ties by path, capped at ``limit`` (at most
    ``MAX_NEIGHBOUR_TESTS``). Only test files present under ``repo_root`` are
    kept. With ``candidate_root`` the impacted test nodes replace those files
    when every changed path maps to nodes (NF-2026-02102). ``INVARIANT_TESTS``
    and the tests that load a changed module by path or ratchet its size
    follow, outside that cap. Never raises.
    """
    bounded = _bounded_limit(limit)
    try:
        selection = _select(
            Path(repo_root), changed_paths, declared_commands, bounded, candidate_root
        )
    except Exception as exc:  # the lane is evidence-gathering: it records, never raises
        selection = _empty(f"neighbour_selection_failed:{type(exc).__name__}", bounded)
    try:
        selection = _with_invariants(Path(repo_root), selection, changed_paths, declared_commands)
    except Exception:  # the invariant addition must never turn a selection into a raise
        pass
    try:
        selection = _with_contract_gates(
            Path(repo_root), selection, changed_paths, declared_commands
        )
    except Exception as exc:  # a failed pin is named in the evidence, never silently absent
        failed = f"contract_gate_failed:{type(exc).__name__}"
        selection = replace(selection, contract_gate_reason=failed)
    try:
        return _with_referencing(Path(repo_root), selection, changed_paths, declared_commands)
    except Exception as exc:  # nor the referencing scan: it names its failure and adds nothing
        reason = f"referencing_scan_failed:{type(exc).__name__}"
        return replace(selection, referencing_reason=reason)


def _with_invariants(
    root: Path,
    selection: NeighbourSelection,
    changed_paths: Iterable[Any],
    declared_commands: Sequence[str],
) -> NeighbourSelection:
    if selection.limit == 0 or not _production_paths(changed_paths):
        return selection
    named = set(
        _advice._command_names(
            command for command in declared_commands if isinstance(command, str)
        )
    )
    present = set(selection.tests)
    invariant = tuple(
        test
        for test in INVARIANT_TESTS
        if test not in named and test not in present and (root / test).is_file()
    )
    if not invariant:
        return selection
    return replace(selection, tests=selection.tests + invariant, invariant=invariant)


def _with_contract_gates(
    root: Path,
    selection: NeighbourSelection,
    changed_paths: Iterable[Any],
    declared_commands: Sequence[str],
) -> NeighbourSelection:
    """NF-2026-02162: pin the MCP schema-freeze gate when a tool-surface module changed.

    The whole gate file is appended outside the neighbour cap, so the cap never drops
    it and a successful node lane does not hide it. Ranked entries of the gate file
    (file or node ids) fold into the pinned whole file. A gate the declared validation
    already names is not run twice; a gate file missing from ``root`` is named.
    """
    if selection.limit == 0:
        return selection
    surface = tuple(
        path for path in _production_paths(changed_paths) if path in CONTRACT_GATE_MODULES
    )
    if not surface:
        return selection
    named = _advice._command_names(
        command for command in declared_commands if isinstance(command, str)
    )
    if _advice._covers(named, CONTRACT_GATE_TEST):
        return selection
    if not (root / CONTRACT_GATE_TEST).is_file():
        return replace(
            selection, contract_gate_reason=f"contract_gate_missing:{CONTRACT_GATE_TEST}"
        )
    reason = "contract_gate:" + ",".join(path.rsplit("/", 1)[-1] for path in surface)
    kept = tuple(test for test in selection.tests if test.split("::", 1)[0] != CONTRACT_GATE_TEST)
    folded = len(selection.tests) - len(kept)
    node_count = selection.node_count - folded if selection.selection_mode == "node" else 0
    return replace(
        selection,
        tests=kept + (CONTRACT_GATE_TEST,),
        contract_gate=(CONTRACT_GATE_TEST,),
        contract_gate_reason=reason,
        related_count=max(0, selection.related_count - folded),
        node_count=max(0, node_count),
    )


def _with_referencing(
    root: Path,
    selection: NeighbourSelection,
    changed_paths: Iterable[Any],
    declared_commands: Sequence[str],
) -> NeighbourSelection:
    """Add the tests that load a changed module by path or ratchet its size.

    Ratchet tests first, then path order, at most ``MAX_REFERENCING_TESTS``;
    tests the declared validation names, the selection holds or
    ``INVARIANT_TESTS`` lists are left out.
    """
    if selection.limit == 0:
        return selection
    stems = sorted(
        {path.rsplit("/", 1)[-1][: -len(".py")] for path in _production_paths(changed_paths)}
        - {"__init__"}  # a package's __init__.py is named by fixtures that build packages
    )
    if not stems:
        return selection
    named = _advice._command_names(
        command for command in declared_commands if isinstance(command, str)
    )
    present = set(selection.tests)
    candidates = [
        (test, path)
        for test, path in _collected_tests(root)
        if test not in present
        and test not in INVARIANT_TESTS
        and not _advice._covers(named, test)
    ]
    literal, ratchet = _stem_patterns(stems)
    needles = tuple(f"{stem}.py" for stem in stems) + tuple(
        f"{stem.upper()}_MAX_LINES" for stem in stems
    )

    def rank(item: tuple[str, Path]) -> int | None:
        text = _read_test(item[1])
        # Either pattern needs one of these substrings; ``in`` is far cheaper than a regex.
        if not any(needle in text for needle in needles):
            return None
        return _referencing_rank(text, literal, ratchet)

    # Measured 2026-11-02 over the canonical tests/ tree (844 files, 17 MB), three stems:
    # sequential 1.4-2.6 s, 12 threads 0.75-1.1 s, identical ranks. Without the substring
    # filter the GIL-bound regexes dominated (3.4-3.8 s) and threads gained little.
    # pool.map keeps candidate order, so the result equals a sequential scan.
    with ThreadPoolExecutor(max_workers=_scan_workers(len(candidates))) as pool:
        ranks = list(pool.map(rank, candidates))
    ranked = sorted(
        (rank, test) for rank, (test, _path) in zip(ranks, candidates) if rank is not None
    )
    ordered = tuple(test for _rank, test in ranked)
    if not ordered:
        return selection
    kept = ordered[:MAX_REFERENCING_TESTS]
    return replace(
        selection,
        tests=selection.tests + kept,
        referencing=kept,
        referencing_omitted=len(ordered) - len(kept),
    )


def _stem_patterns(stems: Sequence[str]) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """The ``<stem>.py`` file-name literal as a word, and the ``<STEM>_MAX_LINES`` ratchet."""
    literal = re.compile(
        "|".join(rf"(?<![A-Za-z0-9_]){re.escape(stem)}\.py(?![A-Za-z0-9_])" for stem in stems)
    )
    ratchet = re.compile(
        "^(?:" + "|".join(re.escape(stem.upper()) for stem in stems) + ")" + _RATCHET_BINDING,
        re.MULTILINE,
    )
    return literal, ratchet


def _referencing_rank(
    text: str, literal: re.Pattern[str], ratchet: re.Pattern[str]
) -> int | None:
    """0 for a ratchet named for a changed module, 1 for a path-loading test, else None."""
    if ratchet.search(text):
        return 0
    if literal.search(text):
        return 1
    return None


def _is_link(status: os.stat_result) -> bool:
    """A symlink, or on Windows any reparse point such as a junction."""
    attributes = getattr(status, "st_file_attributes", 0)
    return stat.S_ISLNK(status.st_mode) or bool(attributes & _REPARSE_POINT)


def _collected_tests(root: Path) -> list[tuple[str, Path]]:
    """Collected test files under ``root/tests``, sorted, never through a link.

    Files over ``REFERENCING_SCAN_MAX_BYTES`` are skipped; an unreadable
    directory raises, and the caller records that the scan failed.
    """
    top = root / _TESTS_DIR
    try:
        status = os.lstat(top)
    except FileNotFoundError:
        return []
    if _is_link(status) or not stat.S_ISDIR(status.st_mode):
        return []
    found: list[tuple[str, Path]] = []
    pending = [(top, _TESTS_DIR)]
    while pending:
        directory, relative = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                child = f"{relative}/{entry.name}"
                status = entry.stat(follow_symlinks=False)
                if _is_link(status):
                    continue
                if stat.S_ISDIR(status.st_mode):
                    pending.append((Path(entry.path), child))
                elif (
                    stat.S_ISREG(status.st_mode)
                    and status.st_size <= REFERENCING_SCAN_MAX_BYTES
                    and _TEST_PATH.fullmatch(child)
                    and _COLLECTED_TEST_PATH.fullmatch(child)
                ):
                    found.append((child, Path(entry.path)))
    return sorted(found)


def _read_test(path: Path) -> str:
    """The head of one test file; an unreadable one reads as empty, so only it ranks None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(REFERENCING_SCAN_MAX_BYTES)
    except OSError:
        return ""


def _scan_workers(count: int) -> int:
    """Threads for ``count`` reads: a quarter of the cores stay free for the accept."""
    return max(1, min(count, (os.cpu_count() or 1) * 3 // 4))


def _select(
    root: Path,
    changed_paths: Iterable[Any],
    declared_commands: Sequence[str],
    limit: int,
    candidate_root: Path | str | None = None,
) -> NeighbourSelection:
    if limit == 0:
        return _empty("neighbour_limit_zero", limit)
    production = _production_paths(changed_paths)
    if not production:
        return _empty("no_changed_production_python", limit)
    consulted = tuple(production[: _advice.MAX_PRODUCTION_PATHS])
    try:
        freshness = _advice._freshness_states(root, consulted)
    except Exception as exc:  # unresolved repo, missing or unreadable index, ...
        return _empty(_advice._failure_reason(exc), limit, production_paths=consulted)
    named = _advice._command_names(
        command for command in declared_commands if isinstance(command, str)
    )
    best, unavailable, _truncated, answered = _advice._related_tests(
        root, consulted, freshness, named
    )
    if not answered:
        reason = unavailable[0]["reason"] if unavailable else "testmap_not_answered"
        return _empty(
            reason, limit, production_paths=consulted, unavailable_paths=tuple(unavailable)
        )
    runnable = {
        test: score
        for test, (score, _path) in best.items()
        if _TEST_PATH.fullmatch(test)
        and _COLLECTED_TEST_PATH.fullmatch(test)
        and (root / test).is_file()
    }
    ranked = sorted(runnable.items(), key=lambda item: (-item[1], item[0]))
    tests = tuple(test for test, _score in ranked[:limit])
    selection = NeighbourSelection(
        tests=tests,
        reason="" if tests else "no_related_tests",
        limit=limit,
        production_paths=consulted,
        related_count=len(ranked),
        unavailable_paths=tuple(unavailable),
    )
    if candidate_root is None:
        return selection
    try:
        return _with_nodes(root, Path(candidate_root), selection, freshness, named)
    except Exception as exc:  # unknown never selects fewer than the file rule
        failed = ({"path": "", "reason": f"node_selection_failed:{type(exc).__name__}"},)
        return replace(selection, selection_mode="file_fallback", fallback_reasons=failed)


def _with_nodes(
    root: Path,
    candidate_root: Path,
    selection: NeighbourSelection,
    freshness: Mapping[str, str],
    named: Sequence[str],
) -> NeighbourSelection:
    """NF-2026-02102: the impacted test nodes replace the ranked files only when
    every production path maps to nodes; any fallback keeps the file selection."""
    nodes, reasons = _nodes.select_nodes(
        root, candidate_root, selection.production_paths, freshness
    )
    files = {node: node.split("::", 1)[0] for node in nodes}
    kept = tuple(
        node for node in nodes
        if files[node] not in INVARIANT_TESTS and not _advice._covers(named, files[node])
    )
    reasons += tuple(
        {"path": node, "reason": "node_unrunnable"}
        for node in kept
        if not (
            _NODE_ID.fullmatch(node)
            and _COLLECTED_TEST_PATH.fullmatch(files[node])
            and (root / files[node]).is_file()
        )
    )
    # Node ids come from the canonical index: a candidate-edited test file may lack them.
    same = {
        f: _nodes.unchanged_in_candidate(root, candidate_root, f) for f in {files[n] for n in kept}
    }
    reasons += tuple(
        {"path": node, "reason": "test_file_changed_in_candidate"}
        for node in kept if not same[files[node]]
    )
    if reasons:
        return replace(selection, selection_mode="file_fallback", fallback_reasons=reasons)
    return replace(
        selection, tests=kept, reason="" if kept else "no_related_tests",
        related_count=len(kept), selection_mode="node", node_count=len(kept),
    )


def neighbour_width(tests: Sequence[str]) -> int:
    """xdist workers for one neighbour run (NF-2026-02069).

    ``max(1, min(distinct test files, cores - headroom))`` with
    ``headroom = max(2, cores // 4)`` and ``cores`` sampled now, so the run never
    takes every core and never starts more workers than it has files.
    """
    files = {str(test).split("::", 1)[0] for test in tests}
    cores = os.cpu_count() or 1
    headroom = max(2, cores // 4)
    return max(1, min(len(files), cores - headroom))


def neighbour_command(tests: Sequence[str], *, serial: bool = False) -> str:
    """One ``python -m pytest`` over ``tests`` with an explicit ``-n`` width.

    The width is always stated, so the repository's ``-n auto`` addopts never
    decides it; ``serial`` is ``-n 0``.
    """
    width = 0 if serial else neighbour_width(tests)
    return f"python -m pytest -n {width} " + " ".join(tests) + " -q"


def neighbour_authority(card: Mapping[str, Any], command: str) -> dict[str, Any]:
    """The card with ``command`` as its only validation and no card-bound keys."""
    authority = {key: value for key, value in card.items() if key not in _CARD_ONLY_KEYS}
    authority["validation"] = [command]
    return authority


def _failed_tests(
    rows: Iterable[Mapping[str, Any]], limit: int = MAX_REPORTED_FAILURES
) -> tuple[list[str], str]:
    failed: list[str] = []
    first_line = ""
    for row in rows:
        for key in ("stdout_tail", "stderr_tail"):
            text = row.get(key)
            if not isinstance(text, str):
                continue
            for match in _PYTEST_FAILURE.finditer(text):
                node = match.group(1)
                if not first_line:
                    first_line = text[match.start() :].split("\n", 1)[0]
                if node not in failed:
                    failed.append(node)
    return failed[:limit], first_line[:_FIRST_FAILURE_CHARS]


def _red(rows: Sequence[Mapping[str, Any]]) -> bool:
    return any(row.get("returncode") != 0 or row.get("timed_out") for row in rows)


def _attempt(
    run: Callable[[Any, Mapping[str, Any], Mapping[str, Any]], list[dict[str, Any]]],
    workspace: Any,
    card: Mapping[str, Any],
    command: str,
    route_metadata: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], str, bool]:
    """One run: its rows, its error text, and whether a red result may be re-run.

    Only a plain red run -- returned rows, or a bare ``ValidationRunError``
    carrying them -- is a test verdict; any other refusal is not re-run.
    """
    try:
        authority = neighbour_authority(card, command)
        return [dict(row) for row in run(workspace, authority, route_metadata)], "", True
    except WorkspaceError as exc:
        rows = [dict(row) for row in getattr(exc, "results", None) or []]
        return rows, str(exc) or type(exc).__name__, type(exc) is ValidationRunError


def _refusal(
    tests: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    error: str,
    default_failed: Sequence[str] = (),
    base_red_refused: str = "",
) -> WorkspaceError:
    failed, first_line = _failed_tests(rows)
    fallback = "neighbour_validation_failed" if rows else "neighbour_validation_returned_no_rows"
    detail = {
        "tests": list(tests),
        "failed": failed or list(default_failed),
        "first_failure": (first_line or error or fallback)[:_FIRST_FAILURE_CHARS],
    }
    if base_red_refused:
        # NF-2026-02101: why the still-red nodes were not excused as base_red.
        detail["base_red_refused"] = base_red_refused[:_FIRST_FAILURE_CHARS]
    return WorkspaceError(
        NEIGHBOUR_FAILURE_PREFIX + json.dumps(detail)[:NEIGHBOUR_FAILURE_DETAIL_CHARS]
    )


def _rerun_nodes(
    selection: NeighbourSelection, rows: Sequence[Mapping[str, Any]], rerunnable: bool
) -> list[str]:
    """The failed node ids of a red run worth one serial rerun, or ``[]`` to refuse."""
    if not rerunnable or not rows:
        return []
    nodes, _first = _failed_tests(rows, MAX_REPORTED_FAILURES + 1)
    if len(nodes) > MAX_REPORTED_FAILURES:
        return []
    selected = set(selection.tests)
    if not all(
        _NODE_ID.fullmatch(node) and node.split("::", 1)[0] in selected for node in nodes
    ):
        return []
    return nodes


def run_neighbour_tests(
    selection: NeighbourSelection,
    card: Mapping[str, Any],
    *,
    run: Callable[[Any, Mapping[str, Any], Mapping[str, Any]], list[dict[str, Any]]],
    workspace: Any,
    route_metadata: Mapping[str, Any],
    clock: Callable[[], float] = time.monotonic,
    base_workspace: Callable[[Any, Mapping[str, Any]], AbstractContextManager[Any]] | None = None,
) -> dict[str, Any]:
    """Run the selection through ``run`` and return its evidence.

    ``run`` is the accept's own declared-validation runner, called as
    ``run(workspace, authority, route_metadata)``. A failing, erroring or
    evidence-free run raises ``WorkspaceError("neighbour_tests_failed:" + json)``
    naming the selected and failed tests, so the card stays in review.
    NF-2026-02070: a red run whose failed node ids parse (at most
    ``MAX_REPORTED_FAILURES``) re-runs only those nodes once, serially; an
    all-green rerun is recorded as ``neighbour_flaky_rerun`` and does not refuse.
    NF-2026-02101: nodes still red after that rerun run once more on
    a full checkout of canonical HEAD as it is now (``base_workspace``, default
    :func:`base_commit_workspace`); when every one of them fails there too the
    failure is not the candidate's and is recorded as ``base_red`` instead of
    refusing. A base that cannot be built or judged refuses as before.
    """
    evidence = selection.evidence()
    if not selection.tests:
        return evidence
    command = neighbour_command(selection.tests)
    evidence["command"] = command
    started = clock()
    rows, error, rerunnable = _attempt(run, workspace, card, command, route_metadata)
    evidence["duration_seconds"] = round(max(0.0, clock() - started), 3)
    if rows and not error and not _red(rows):
        evidence["validation"] = rows
        return evidence
    nodes = _rerun_nodes(selection, rows, rerunnable and _red(rows))
    if not nodes:
        raise _refusal(selection.tests, rows, error)
    rerun_command = neighbour_command(nodes, serial=True)
    started = clock()
    rerun_rows, rerun_error, rerun_verdict = _attempt(
        run, workspace, card, rerun_command, route_metadata
    )
    rerun = {
        "nodes": nodes,
        "command": rerun_command,
        "duration_seconds": round(max(0.0, clock() - started), 3),
        "validation": rerun_rows,
    }
    evidence["validation"] = rows
    if not rerun_error and rerun_rows and not _red(rerun_rows):
        evidence["neighbour_flaky_rerun"] = rerun
        return evidence
    still_red = _rerun_nodes(selection, rerun_rows, rerun_verdict and _red(rerun_rows))
    base_red, refused = _base_red(
        still_red, card, run=run, workspace=workspace, route_metadata=route_metadata,
        clock=clock, base_workspace=base_workspace or base_commit_workspace,
    ) if still_red else (None, "rerun_not_a_parseable_test_verdict")
    if not base_red:
        raise _refusal(
            selection.tests, rerun_rows, rerun_error, default_failed=nodes,
            base_red_refused=refused,
        )
    evidence["neighbour_rerun"] = rerun
    evidence["base_red"] = base_red
    return evidence


@contextmanager
def base_commit_workspace(workspace: Any, card: Mapping[str, Any]) -> Iterator[Any]:
    """A full, write-free checkout of the canonical HEAD, removed on exit.

    The combined tree is built from ``workspace.repo``'s canonical HEAD plus the
    candidate, so HEAD is pinned as it is now, at the neighbour run -- never the
    worker's original ``base_oid``, which would excuse a node canonical already
    fixed and the candidate broke again. No repository or HEAD raises.
    """
    repo = getattr(workspace, "repo", None)
    if not isinstance(repo, (str, os.PathLike)) or not str(repo):
        raise WorkspaceError("neighbour_canonical_head_unavailable")
    head_oid = _workspace._repository_head_oid(Path(repo))
    base_card = {key: value for key, value in card.items() if key != "rework_predecessor"}
    base = replace(
        _workspace.create_workspace(
            Path(repo), f"nbase_{uuid.uuid4().hex[:16]}", base_card, "validation",
            pinned_base_oid=head_oid,
        ),
        allowed_writes=(),
    )
    try:
        # create_workspace seeds a sparse closure; a neighbour test needs the full tree.
        expanded = _workspace._run(
            ["git", "sparse-checkout", "disable"], cwd=base.path,
            timeout=_workspace.WORKTREE_CREATE_TIMEOUT_SECONDS, phase="workspace_provision",
        )
        if expanded.returncode != 0:
            raise WorkspaceError("neighbour_base_full_tree_failed")
        yield base
    finally:
        _workspace.cleanup_workspace(base.repo, base.path, base.home)


def _base_red(
    nodes: Sequence[str],
    card: Mapping[str, Any],
    *,
    run: Callable[[Any, Mapping[str, Any], Mapping[str, Any]], list[dict[str, Any]]],
    workspace: Any,
    route_metadata: Mapping[str, Any],
    clock: Callable[[], float],
    base_workspace: Callable[[Any, Mapping[str, Any]], AbstractContextManager[Any]],
) -> tuple[dict[str, Any] | None, str]:
    """The base-commit record when every one of ``nodes`` fails there too.

    Unknown is never base_red: an unbuildable base, a refusal that is not a
    plain test verdict, or a node that passes or goes unreported all yield
    ``(None, reason)``, and the reason names which of those it was so the
    refusal states its cause instead of swallowing it.
    """
    command = neighbour_command(nodes, serial=True)
    try:
        with base_workspace(workspace, card) as base:
            # Timed once the base exists: an unbuildable base reads no clock.
            started = clock()
            rows, error, verdict = _attempt(run, base, card, command, route_metadata)
            seconds = round(max(0.0, clock() - started), 3)
    except Exception as exc:  # an unbuildable base is unknown, and unknown never excuses a failure
        return None, f"base_unavailable:{type(exc).__name__}:{exc}"
    if not verdict:
        return None, f"base_not_a_test_verdict:{error or 'no_rows'}"
    failed, _first = _failed_tests(rows, MAX_REPORTED_FAILURES + 1)
    green = [node for node in nodes if node not in failed] if _red(rows) else list(nodes)
    if green:
        return None, "base_green:" + ",".join(green)
    return {
        "nodes": list(nodes),
        # The commit the base run actually checked out: canonical HEAD at this run.
        "base_oid": getattr(base, "base_oid", None),
        "base_source": "canonical_head",
        "command": command,
        "duration_seconds": seconds,
        "validation": rows,
    }, ""


# NF-2026-02069: the neighbour run moves to finalization, on the same sealed
# combined tree the declared validations ran in; accept reuses a green record
# only when its own validation reuse matched that tree.
FINALIZATION_REUSE_SOURCE = "finalization"


def run_finalization_neighbours(
    repo_root: Path | str,
    candidate_changed_paths: Iterable[Any],
    card: Mapping[str, Any],
    declared_commands: Sequence[str],
    *,
    run: Callable[[Any, Mapping[str, Any], Mapping[str, Any]], list[dict[str, Any]]],
    workspace: Any,
    route_metadata: Mapping[str, Any],
    clock: Callable[[], float] = time.monotonic,
    candidate_root: Path | str | None = None,
) -> dict[str, Any] | None:
    """Select and run the neighbours once in the finalization snapshot.

    ``None`` when nothing is selected. Never raises: a red, erroring or
    evidence-free run is recorded with ``passed: False`` (and its refusal text
    under ``error``), so finalization stands and the accept runs it again.
    """
    selection = select_neighbour_tests(
        repo_root, candidate_changed_paths, declared_commands, candidate_root=candidate_root
    )
    if not selection.tests:
        return None
    started = clock()
    try:
        evidence = run_neighbour_tests(
            selection, card, run=run, workspace=workspace,
            route_metadata=route_metadata, clock=clock,
        )
    except Exception as exc:  # recorded, never raised: finalization must not fail on it
        failed = selection.evidence()
        failed["command"] = neighbour_command(selection.tests)
        failed["duration_seconds"] = round(max(0.0, clock() - started), 3)
        failed["passed"] = False
        failed["error"] = (str(exc) or type(exc).__name__)[:NEIGHBOUR_FAILURE_DETAIL_CHARS]
        return failed
    evidence["passed"] = True
    return evidence


def reused_finalization_neighbours(
    finalizer_reuse: Mapping[str, Any] | None,
    union_reuse: Mapping[str, Any] | None,
    selection: NeighbourSelection,
) -> dict[str, Any] | None:
    """The finalizer's green neighbour evidence when accept may reuse it, else ``None``.

    Reuse needs all of: a non-empty accept-time selection; the accept's union
    validation having reused this exact finalizer record (same snapshot, no
    accept-union run of its own -- which a manager correction already rules
    out, since it yields no finalizer record); recorded ``passed is True``; and
    a recorded selection equal to ``selection.tests``.
    """
    if not selection.tests:
        return None
    if not isinstance(finalizer_reuse, Mapping) or not isinstance(union_reuse, Mapping):
        return None
    snapshot = finalizer_reuse.get("full_validation_snapshot")
    if (
        not isinstance(snapshot, Mapping)
        or union_reuse.get("full_validation_snapshot") != snapshot
        or union_reuse.get("reused_from") != finalizer_reuse.get("reused_from")
    ):
        return None
    recorded = snapshot.get("neighbour_tests")
    if (
        not isinstance(recorded, Mapping)
        or recorded.get("passed") is not True
        or recorded.get("selected") != list(selection.tests)
    ):
        return None
    # NF-2026-02062: a HEAD that moved (or whose movement is unknown) leaves the
    # exact-HEAD guarantee behind; the neighbours' own files must then sit inside
    # the validation closure the matched digest covered.  An exact-HEAD receipt
    # reports ``head_moved`` False, and a receipt-less union record is exact-HEAD.
    receipt = union_reuse.get("tree_identity_receipt")
    if (
        isinstance(receipt, Mapping)
        and receipt.get("head_moved") is not False
        and recorded.get("closure_covers_selected") is not True
    ):
        return None
    return {**dict(recorded), "reused_from": FINALIZATION_REUSE_SOURCE}
