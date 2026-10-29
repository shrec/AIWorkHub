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
keeps no testmap client of its own. It never raises: an unavailable index or no
related test yields an empty selection with a named ``reason``, which the lane
records and which does not block acceptance. A selected run that fails, or that
returns no evidence, refuses the accept with ``neighbour_tests_failed:``.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import card_contract_advice as _advice
from .sdlc_attribution import is_test_path
from .toolchain_authority import RECEIPT_CARD_KEY
from .worker_workspace import WorkspaceError

# No commit history was readable when this was written, so recall at a lower cap
# is unmeasured; the default is the hard ceiling, which keeps every
# highest-scoring neighbour a smaller cap would also keep.
MAX_NEIGHBOUR_TESTS = 12
DEFAULT_NEIGHBOUR_LIMIT = MAX_NEIGHBOUR_TESTS
# Repository-wide invariants no testmap links to one module; they run whenever
# a src/ module changed and do not count against the neighbour cap.
INVARIANT_TESTS = ("tests/test_module_size_ratchet.py",)
NEIGHBOUR_FAILURE_PREFIX = "neighbour_tests_failed:"
NEIGHBOUR_FAILURE_DETAIL_CHARS = 4000
MAX_REPORTED_FAILURES = 20
_FIRST_FAILURE_CHARS = 500
# A selected path becomes one argv token of ``python -m pytest``; anything
# needing quoting is not a test path this lane runs.
_TEST_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.py")
_PYTEST_FAILURE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)
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

    def evidence(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "selected": list(self.tests),
            "limit": self.limit,
            "production_paths": list(self.production_paths),
            "related_count": self.related_count,
            "omitted": max(0, self.related_count - (len(self.tests) - len(self.invariant))),
        }
        if self.invariant:
            record["invariant"] = list(self.invariant)
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
    kept. Never raises.
    """
    bounded = _bounded_limit(limit)
    try:
        selection = _select(Path(repo_root), changed_paths, declared_commands, bounded)
    except Exception as exc:  # the lane is evidence-gathering: it records, never raises
        selection = _empty(f"neighbour_selection_failed:{type(exc).__name__}", bounded)
    try:
        return _with_invariants(Path(repo_root), selection, changed_paths, declared_commands)
    except Exception:  # the invariant addition must never turn a selection into a raise
        return selection


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
        if _TEST_PATH.fullmatch(test) and (root / test).is_file()
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


def neighbour_command(tests: Sequence[str]) -> str:
    return "python -m pytest " + " ".join(tests) + " -q"


def neighbour_authority(card: Mapping[str, Any], command: str) -> dict[str, Any]:
    """The card with ``command`` as its only validation and no card-bound keys."""
    authority = {key: value for key, value in card.items() if key not in _CARD_ONLY_KEYS}
    authority["validation"] = [command]
    return authority


def _failed_tests(rows: Iterable[Mapping[str, Any]]) -> tuple[list[str], str]:
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
    return failed[:MAX_REPORTED_FAILURES], first_line[:_FIRST_FAILURE_CHARS]


def run_neighbour_tests(
    selection: NeighbourSelection,
    card: Mapping[str, Any],
    *,
    run: Callable[[Any, Mapping[str, Any], Mapping[str, Any]], list[dict[str, Any]]],
    workspace: Any,
    route_metadata: Mapping[str, Any],
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run the selection once through ``run`` and return its evidence.

    ``run`` is the accept's own declared-validation runner, called as
    ``run(workspace, authority, route_metadata)``. A failing, erroring or
    evidence-free run raises ``WorkspaceError("neighbour_tests_failed:" + json)``
    naming the selected and failed tests, so the card stays in review.
    """
    evidence = selection.evidence()
    if not selection.tests:
        return evidence
    command = neighbour_command(selection.tests)
    evidence["command"] = command
    started = clock()
    error = ""
    try:
        authority = neighbour_authority(card, command)
        rows = [dict(row) for row in run(workspace, authority, route_metadata)]
    except WorkspaceError as exc:
        rows = [dict(row) for row in getattr(exc, "results", None) or []]
        error = str(exc) or type(exc).__name__
    evidence["duration_seconds"] = round(max(0.0, clock() - started), 3)
    red = [row for row in rows if row.get("returncode") != 0 or row.get("timed_out")]
    if error or red or not rows:
        failed, first_line = _failed_tests(rows)
        fallback = (
            "neighbour_validation_failed" if rows else "neighbour_validation_returned_no_rows"
        )
        detail = {
            "tests": list(selection.tests),
            "failed": failed,
            "first_failure": (first_line or error or fallback)[:_FIRST_FAILURE_CHARS],
        }
        raise WorkspaceError(
            NEIGHBOUR_FAILURE_PREFIX + json.dumps(detail)[:NEIGHBOUR_FAILURE_DETAIL_CHARS]
        )
    evidence["validation"] = rows
    return evidence
