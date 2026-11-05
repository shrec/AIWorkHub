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
returns no evidence, refuses the accept with ``neighbour_tests_failed:``.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import card_contract_advice as _advice
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
INVARIANT_TESTS = ("tests/test_module_size_ratchet.py",)
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

    def evidence(self) -> dict[str, Any]:
        ranked = len(self.tests) - len(self.invariant) - len(self.referencing)
        record: dict[str, Any] = {
            "selected": list(self.tests),
            "limit": self.limit,
            "production_paths": list(self.production_paths),
            "related_count": self.related_count,
            "omitted": max(0, self.related_count - ranked),
        }
        if self.invariant:
            record["invariant"] = list(self.invariant)
        if self.referencing or self.referencing_omitted:
            record["referencing"] = list(self.referencing)
            record["referencing_omitted"] = self.referencing_omitted
        if self.referencing_reason:
            record["referencing_reason"] = self.referencing_reason
        if self.reason:
            record["reason"] = self.reason
        if self.unavailable_paths:
            record["unavailable_paths"] = [dict(row) for row in self.unavailable_paths]
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
) -> NeighbourSelection:
    """Rank the related tests of ``changed_paths`` that ``declared_commands`` miss.

    Highest testmap score first, ties by path, capped at ``limit`` (at most
    ``MAX_NEIGHBOUR_TESTS``). Only test files present under ``repo_root`` are
    kept. ``INVARIANT_TESTS`` and the tests that load a changed module by path
    or ratchet its size follow, outside that cap. Never raises.
    """
    bounded = _bounded_limit(limit)
    try:
        selection = _select(Path(repo_root), changed_paths, declared_commands, bounded)
    except Exception as exc:  # the lane is evidence-gathering: it records, never raises
        selection = _empty(f"neighbour_selection_failed:{type(exc).__name__}", bounded)
    try:
        selection = _with_invariants(Path(repo_root), selection, changed_paths, declared_commands)
    except Exception:  # the invariant addition must never turn a selection into a raise
        pass
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
    return NeighbourSelection(
        tests=tests,
        reason="" if tests else "no_related_tests",
        limit=limit,
        production_paths=consulted,
        related_count=len(ranked),
        unavailable_paths=tuple(unavailable),
    )


def neighbour_command(tests: Sequence[str], *, serial: bool = False) -> str:
    return "python -m pytest " + ("-n 0 " if serial else "") + " ".join(tests) + " -q"


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
) -> WorkspaceError:
    failed, first_line = _failed_tests(rows)
    fallback = "neighbour_validation_failed" if rows else "neighbour_validation_returned_no_rows"
    detail = {
        "tests": list(tests),
        "failed": failed or list(default_failed),
        "first_failure": (first_line or error or fallback)[:_FIRST_FAILURE_CHARS],
    }
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
) -> dict[str, Any]:
    """Run the selection through ``run`` and return its evidence.

    ``run`` is the accept's own declared-validation runner, called as
    ``run(workspace, authority, route_metadata)``. A failing, erroring or
    evidence-free run raises ``WorkspaceError("neighbour_tests_failed:" + json)``
    naming the selected and failed tests, so the card stays in review.
    NF-2026-02070: a red run whose failed node ids parse (at most
    ``MAX_REPORTED_FAILURES``) re-runs only those nodes once, serially; an
    all-green rerun is recorded as ``neighbour_flaky_rerun`` and does not refuse.
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
    rerun_rows, rerun_error, _ = _attempt(
        run, workspace, card, rerun_command, route_metadata
    )
    rerun_seconds = round(max(0.0, clock() - started), 3)
    if rerun_error or not rerun_rows or _red(rerun_rows):
        raise _refusal(selection.tests, rerun_rows, rerun_error, default_failed=nodes)
    evidence["validation"] = rows
    evidence["neighbour_flaky_rerun"] = {
        "nodes": nodes,
        "command": rerun_command,
        "duration_seconds": rerun_seconds,
        "validation": rerun_rows,
    }
    return evidence
