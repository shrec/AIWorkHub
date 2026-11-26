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

NF-2026-02220: when :mod:`aiworkhub.test_impact_select`'s recorded map is fresh for
every changed path, its selection replaces the testmap ranking (``lane: impact_map``);
otherwise the ranking above decides unchanged (``lane: structural``), with the reason.
A collected test the map lacks or records stale always runs whole; any other recorded
path the map records stale sends the decision to the structural lane.
"""

from __future__ import annotations

import ast
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

from . import accept_neighbour_direct as _direct
from . import accept_neighbour_nodes as _nodes
from . import card_contract_advice as _advice
from . import test_impact_select as _impact
from . import worker_workspace as _workspace
from .sdlc_attribution import is_test_path
from .task_templates import split_command_argv
from .toolchain_authority import RECEIPT_CARD_KEY
from .validation_skip_audit import VALIDATION_ENV_SKIPPED
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
# NF-2026-02245: the 0.12.98 wave accepted many cards clean and the release-cut full
# suite then found 51 failures, because the selection above only follows the changed
# module's importers and repository-wide contract tests never ran at accept. This
# static table is the always-run contract tier: when a rule's trigger matches, its
# tests run outside the neighbour cap, each with a ``contract_tier:<rule>:<path>``
# reason. A rule test missing on disk is skipped and named, never a selection failure.
CONTRACT_TIER_PREFIX = "contract_tier"
TIER_TRIGGER_CHANGED = "changed"  # one of ``paths`` is a changed path
TIER_TRIGGER_NEW_RELATIVE_IMPORT = "new_relative_import"  # ``paths`` gained a relative import
TIER_TRIGGER_ADDED_TEST = "added_test"  # a changed tests/**.py file the repo root lacks


@dataclass(frozen=True)
class ContractTierRule:
    """One always-run contract rule: a trigger over ``paths`` selects ``tests``."""

    rule: str
    trigger: str
    paths: tuple[str, ...]
    tests: tuple[str, ...]


CONTRACT_TIER_RULES: tuple[ContractTierRule, ...] = (
    ContractTierRule(
        "tool_surface",
        TIER_TRIGGER_CHANGED,
        CONTRACT_GATE_MODULES,
        ("tests/test_mcp_tool_metadata_tdqs.py", "tests/test_stdio_tools_list_parity_nf01642.py"),
    ),
    ContractTierRule(
        "readme",
        TIER_TRIGGER_CHANGED,
        ("README.md", "vscode-extension/README.md"),
        ("tests/test_public_docs.py", "tests/test_release_metadata.py"),
    ),
    ContractTierRule(
        "worker_workspace_import",
        TIER_TRIGGER_NEW_RELATIVE_IMPORT,
        ("src/aiworkhub/worker_workspace.py",),
        ("tests/test_worker_workspace.py",),
    ),
    ContractTierRule(
        "host_path_guard",
        TIER_TRIGGER_ADDED_TEST,
        (),
        ("tests/test_no_host_paths_in_tracked_files_nf01061.py",),
    ),
)
# A module larger than this is not parsed for its relative imports; the rule then
# treats the import set as unknown and selects its tests (fail closed).
TIER_IMPORT_SCAN_MAX_BYTES = 4 * 1024 * 1024
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
# NF-2026-02408: a summary line, optionally behind an xdist ``[gw3]`` worker prefix
# (and its ``[ 50%]`` progress marker); matched over ANSI-stripped text.
_PYTEST_FAILURE = re.compile(
    r"^(?:\[gw\d+\][ \t]+(?:\[[ \t]*\d+%\][ \t]+)?)?(?:FAILED|ERROR) (\S+)", re.MULTILINE
)
# CSI sequences (colours, cursor moves) and two-byte ESC sequences.
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])")
_OUTPUT_TAIL_CHARS = 3000
# A failed node id re-run as one argv token: a selected path, optionally ``::name``,
# with nothing a command line would need to quote.
_NODE_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.py(?:::[A-Za-z0-9_.:\[\]/,+=-]+)?")
# A neighbour run is its own one-command contract: the card's evidence roles and
# its toolchain receipt are bound to the card's own validation list.
_CARD_ONLY_KEYS = ("validation_roles", "work_kind", RECEIPT_CARD_KEY)
# NF-2026-02220: the lane that decided the ranked part of a selection -- the recorded
# test-impact map when it is fresh for every changed path, else the structural lane.
LANE_IMPACT_MAP = "impact_map"
LANE_STRUCTURAL = "structural"
# The impact lane is uncapped (never narrower than the selector), so its one argv must
# stay under the 32767-character Windows command line; node ids fold to files first.
MAX_IMPACT_COMMAND_CHARS = 24_000
# A declared pytest run with any of these may collect less than the files it names, or
# (--co, --collect-only, --collectonly, --setup-only, --setup-plan, --fixtures,
# --fixtures-per-test, --markers, -h, --help, --version) executes no test at all, so its
# names never excuse an impacted test from the neighbour run.
_NARROWING_OPTIONS = frozenset(
    {"-k", "-m", "--deselect", "--ignore", "--ignore-glob", "--lf", "--last-failed",
     "--sw", "--stepwise", "--co", "--collect-only", "--setup-only", "--setup-plan",
     "--fixtures", "--fixtures-per-test", "--markers", "-h", "--help", "--version",
     "--collectonly", "--sw-skip", "--stepwise-skip"}
)


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
    # NF-2026-02220: LANE_IMPACT_MAP or LANE_STRUCTURAL, and why that lane decided.
    lane: str = ""
    lane_reason: str = ""
    # NF-2026-02245: the contract-tier tests added, each row {"test", "reason"}; the
    # rule tests missing on disk, each row {"test", "reason"}; or why the tier failed.
    contract_tier: tuple[str, ...] = ()
    contract_tier_reasons: tuple[Mapping[str, str], ...] = field(default=())
    contract_tier_skipped: tuple[Mapping[str, str], ...] = field(default=())
    contract_tier_reason: str = ""
    # NF-2026-02390: the tests that directly import a changed module, outside ``limit``;
    # how many direct importers ``DIRECT_IMPORTER_CAP`` left out, and why. The tier is
    # additive: ``direct_added`` counts the ones appended because no other lane held them.
    direct: tuple[str, ...] = ()
    direct_omitted: int = 0
    direct_reason: str = ""
    direct_added: int = 0

    def evidence(self) -> dict[str, Any]:
        ranked = (
            len(self.tests)
            - self.direct_added
            - len(self.invariant)
            - len(self.contract_gate)
            - len(self.contract_tier)
            - len(self.referencing)
        )
        record: dict[str, Any] = {
            "selected": list(self.tests),
            "limit": self.limit,
            "production_paths": list(self.production_paths),
            "related_count": self.related_count,
            "omitted": max(0, self.related_count - ranked),
        }
        if self.direct or self.direct_omitted:
            record["direct"] = list(self.direct)
            record["direct_omitted"] = self.direct_omitted
        if self.direct_reason:
            record["direct_reason"] = self.direct_reason
        if self.invariant:
            record["invariant"] = list(self.invariant)
        if self.contract_gate:
            record["contract_gate"] = list(self.contract_gate)
        if self.contract_gate_reason:
            record["contract_gate_reason"] = self.contract_gate_reason
        if self.contract_tier:
            record["contract_tier"] = list(self.contract_tier)
            record["contract_tier_reasons"] = [dict(row) for row in self.contract_tier_reasons]
        if self.contract_tier_skipped:
            record["contract_tier_skipped"] = [dict(row) for row in self.contract_tier_skipped]
        if self.contract_tier_reason:
            record["contract_tier_reason"] = self.contract_tier_reason
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
        if self.lane:
            record["lane"] = self.lane
            record["lane_reason"] = self.lane_reason
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


# NF-2026-02298: a candidate that changed only vscode-extension/extension.js ran no
# neighbour test at accept, though a Python test reads that file (84831f41). Every changed
# non-test path outside src/**.py is now a selector path. Documentation (.md, .rst) keeps
# today's behaviour: CONTRACT_TIER_RULES owns it. conftest.py is a test-tree file.
_DOC_SUFFIXES = (".md", ".rst")
_NON_SOURCE_PREFIXES = (".git/", ".aiworkhub/runtime/")
# A basename that appears in too many unrelated tests to name its file; only the
# repo-relative path matches for these.
_GENERIC_BASENAMES = frozenset(
    {"__init__.py", "__main__.py", "conftest.py", "main.py", "index.js", "setup.py",
     "package.json", "package-lock.json", "tsconfig.json", "config.json", "settings.json"}
)
_MIN_BASENAME_CHARS = 8


def _non_python_paths(changed_paths: Iterable[Any]) -> list[str]:
    """Changed non-test paths that are not ``src/**.py`` modules or documentation."""
    paths = {_advice._normalize_path(path) for path in changed_paths}
    return sorted(
        path
        for path in paths
        if path
        and not (path.startswith("src/") and path.endswith(".py"))
        and not path.endswith(_DOC_SUFFIXES)
        and not path.startswith(_NON_SOURCE_PREFIXES)
        and path.rsplit("/", 1)[-1] != "conftest.py"
        and not is_test_path(path)
    )


def _distinctive_basename(path: str) -> str:
    """The basename of ``path`` when a bare mention of it names that file, else ``""``.

    A test that spells ``extension.js`` without the directory still loads that file
    in practice, so the basename matches too -- unless it is short or one of the
    ``_GENERIC_BASENAMES`` every package has, where a bare mention proves nothing.
    """
    name = path.rsplit("/", 1)[-1]
    if len(name) < _MIN_BASENAME_CHARS or name in _GENERIC_BASENAMES:
        return ""
    return name


def select_neighbour_tests(
    repo_root: Path | str,
    changed_paths: Iterable[Any],
    declared_commands: Sequence[str],
    *,
    limit: int = DEFAULT_NEIGHBOUR_LIMIT,
    candidate_root: Path | str | None = None,
) -> NeighbourSelection:
    """Rank the related tests of ``changed_paths`` that ``declared_commands`` miss.

    NF-2026-02220: when the recorded test-impact map is fresh for every changed
    path, the selector's tests decide (``lane="impact_map"``), uncapped, so the
    run is never narrower than that result together with the declared runs.
    Otherwise -- or when the impact lane raises -- the structural lane below
    decides unchanged (``lane="structural"``) and ``lane_reason`` names why.

    Structural: highest testmap score first, ties by path, capped at ``limit`` (at most
    ``MAX_NEIGHBOUR_TESTS``). Only test files present under ``repo_root`` are
    kept. With ``candidate_root`` the impacted test nodes replace those files
    when every changed path maps to nodes (NF-2026-02102). ``INVARIANT_TESTS``
    and the tests that load a changed module by path or ratchet its size
    follow, outside that cap. Never raises.

    NF-2026-02390: in either lane the tests that directly import a changed
    module (``direct``) are added last, outside ``limit`` under ``DIRECT_IMPORTER_CAP``
    with any overflow in ``direct_omitted``/``direct_reason``. The tier is strictly
    additive: every entry above stays as it was, node ids included, and only the
    importers no entry already covers are appended.
    """
    bounded = _bounded_limit(limit)
    try:
        changed_paths = tuple(changed_paths)
    except Exception:  # an unreadable path list selects through no lane, never raises
        changed_paths = ()
    try:
        impact, why = _impact_lane(
            Path(repo_root), changed_paths, declared_commands, bounded, candidate_root
        )
    except Exception as exc:  # an unknown map never narrows: the structural lane decides
        impact, why = None, f"impact_map_failed:{type(exc).__name__}"
    if impact is not None:
        selection = impact
    else:
        try:
            selection = _select(
                Path(repo_root), changed_paths, declared_commands, bounded, candidate_root
            )
        except Exception as exc:  # the lane is evidence-gathering: it records, never raises
            selection = _empty(f"neighbour_selection_failed:{type(exc).__name__}", bounded)
        selection = replace(selection, lane=LANE_STRUCTURAL, lane_reason=why)
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
        selection = _with_contract_tier(
            Path(repo_root), selection, changed_paths, declared_commands, candidate_root
        )
    except Exception as exc:  # NF-2026-02245: a failed tier is named, never silently absent
        failed = f"{CONTRACT_TIER_PREFIX}_failed:{type(exc).__name__}"
        selection = replace(selection, contract_tier_reason=failed)
    try:
        selection = _with_referencing(
            Path(repo_root), selection, changed_paths, declared_commands
        )
    except Exception as exc:  # nor the referencing scan: it names its failure and adds nothing
        reason = f"referencing_scan_failed:{type(exc).__name__}"
        selection = replace(selection, referencing_reason=reason)
    try:
        direct, direct_omitted, direct_reason = _direct_importers(
            Path(repo_root), changed_paths, declared_commands, bounded
        )
        return _with_direct(selection, direct, direct_omitted, direct_reason)
    except Exception as exc:  # a failed scan keeps the selection and is named, never silent
        return replace(selection, direct_reason=f"direct_scan_failed:{type(exc).__name__}")


def _impact_lane(
    root: Path,
    changed_paths: Sequence[Any],
    declared_commands: Sequence[str],
    limit: int,
    candidate_root: Path | str | None,
) -> tuple[NeighbourSelection | None, str]:
    """NF-2026-02220: the test-impact selector's tests, or ``(None, reason)``.

    The map under ``root`` must record every changed path with the content
    ``root`` holds now; a test module ``root`` lacks counts as recorded, since the
    selector always selects a new test whole. ``candidate_root`` must hold every
    file the map records and every collected test of ``root``, and the selector's
    diff of ``candidate_root`` against its ``HEAD`` must show every changed path.
    Any miss, a full-suite answer or an unrunnable selected test is the named
    reason, and the caller decides through the structural lane instead.
    """
    if limit == 0:
        return None, "impact_map_skipped:neighbour_limit_zero"
    production = _production_paths(changed_paths)
    non_python = _non_python_paths(changed_paths)  # NF-2026-02298: selector paths too
    if not production and not non_python:
        return None, "impact_map_skipped:no_changed_production_python"
    map_path = _impact.map_directory(root) / _impact.MAP_FILE_NAME
    payload, problem = _impact.load_map(map_path)
    digests = payload.get("digests") if payload is not None else None
    if not isinstance(digests, Mapping):
        return None, f"impact_map_unavailable:{problem or 'map_schema_mismatch'}"
    changed = sorted({path for path in map(_advice._normalize_path, changed_paths) if path})
    for path in changed:
        state = _map_state(root, digests, path)
        if state:
            return None, f"impact_map_{state}:{path}"
    if candidate_root is None:
        return None, "impact_map_skipped:no_candidate_tree"
    candidate = Path(candidate_root)
    collected = [test for test, _path in _collected_tests(root, bounded=False)]
    present = sorted(set(digests) | set(collected))
    for path in present:
        if path not in changed and (root / path).is_file() and not (candidate / path).is_file():
            return None, f"impact_map_candidate_tree_incomplete:{path}"
    # Fail open: a collected test the map lacks, or records with other content than
    # ``root`` holds now, is not fresh, so it runs whole whatever the selector says.
    kept = {
        test: ("impact_map_not_fresh_for_test",)
        for test in collected
        if test not in changed and _map_state(root, digests, test)
    }
    # Any other recorded path (conftest.py, a test helper, an unchanged production
    # module) whose content moved since recording leaves the selector's edges unknown,
    # so the structural lane decides.
    for path in sorted(set(digests) - set(changed) - set(collected)):
        if _map_state(root, digests, path):
            return None, f"impact_map_stale_dependency:{path}"
    result = _impact.select_tests(candidate, "HEAD", paths=changed, map_path=map_path)
    if result.full_suite_required:
        return None, "impact_map_full_suite:" + ";".join(result.full_suite_reasons)
    unseen = [path for path in changed if path not in result.changed_paths]
    if unseen:
        return None, f"impact_map_diff_missing:{unseen[0]}"
    tests, problem = _impact_tests(candidate, {**result.selected, **kept}, declared_commands)
    if problem:
        return None, problem
    lane_reason = f"impact_map_fresh:{len(changed)}"
    if kept:
        lane_reason += f";map_stale_tests_kept:{len(kept)}"
    return NeighbourSelection(
        tests=tests, reason="" if tests else "no_impacted_tests", limit=limit,
        production_paths=(*production, *non_python), related_count=len(tests),
        lane=LANE_IMPACT_MAP, lane_reason=lane_reason,
    ), ""


def _map_state(root: Path, digests: Mapping[str, Any], path: str) -> str:
    """``""`` when the map records ``path`` as ``root`` holds it now, else why not."""
    recorded = digests.get(path)
    target = root / path
    if recorded is None:
        absent = not target.exists() and not target.is_symlink()
        return "" if absent and _impact.is_test_module(path) else "unmapped"
    data = target.read_bytes() if target.is_file() else None
    return "" if _impact.content_digest(data) == recorded else "stale"


def _impact_tests(
    candidate: Path, selected: Mapping[str, Any], declared_commands: Sequence[str]
) -> tuple[tuple[str, ...], str]:
    """The selector's tests as argv tokens, minus the files a declared run collects whole.

    A node id needing quotes folds into its file, an invariant test's node ids into
    the whole invariant file, and every node id into its file when the argv would
    pass ``MAX_IMPACT_COMMAND_CHARS``: each fold runs more, never less. A selected
    test that is not a present, collected file is a named reason, never dropped.
    """
    whole = _pytest_whole_files(declared_commands)
    entries: list[str] = []
    for entry in sorted(selected):
        file = entry.split("::", 1)[0]
        if not (
            _TEST_PATH.fullmatch(file)
            and _COLLECTED_TEST_PATH.fullmatch(file)
            and (candidate / file).is_file()
        ):
            return (), f"impact_map_unrunnable:{entry}"
        if _advice._covers(whole, file):
            continue
        folded = file in INVARIANT_TESTS or not _NODE_ID.fullmatch(entry)
        entries.append(file if folded else entry)
    tests = _absorbed(entries)
    if len(" ".join(tests)) > MAX_IMPACT_COMMAND_CHARS:
        tests = _absorbed([test.split("::", 1)[0] for test in tests])
    if len(" ".join(tests)) > MAX_IMPACT_COMMAND_CHARS:
        return (), f"impact_map_selection_too_wide:{len(tests)}"
    return tests, ""


def _absorbed(entries: Sequence[str]) -> tuple[str, ...]:
    """``entries`` once each, in order, with a selected whole file absorbing its node ids."""
    files = {entry for entry in entries if "::" not in entry}
    return tuple(
        dict.fromkeys(
            entry for entry in entries
            if "::" not in entry or entry.split("::", 1)[0] not in files
        )
    )


def _short_cluster_narrows(arg: str) -> bool:
    """A short-option cluster such as ``-qk expr`` or ``-vm slow`` that holds -k, -m or -h.

    Conservative: a stray k/m/h inside an attached short value also counts, which only
    runs more neighbour tests, never fewer.
    """
    return arg.startswith("-") and not arg.startswith("--") and any(c in "kmh" for c in arg[1:])


def _pytest_whole_files(declared_commands: Sequence[str]) -> list[str]:
    """The paths a declared ``pytest`` run collects whole.

    Stricter than ``card_contract_advice._command_names``: a non-pytest command, a
    ``::`` node id or a run with a ``_NARROWING_OPTIONS`` option names nothing, so
    the declared runs never excuse an impacted test they would not run.
    """
    names: list[str] = []
    for command in declared_commands:
        if not isinstance(command, str):
            continue
        tokens = [token.strip("\"'") for token in split_command_argv(command)]
        runner = next(
            (
                index for index, token in enumerate(tokens)
                if token.replace("\\", "/").rsplit("/", 1)[-1] == "pytest"
            ),
            None,
        )
        if runner is None:
            continue
        args = tokens[runner + 1 :]
        if any(
            arg.split("=", 1)[0] in _NARROWING_OPTIONS or _short_cluster_narrows(arg)
            for arg in args
        ):
            continue
        for arg in args:
            name = "" if arg.startswith("-") or "::" in arg else _advice._normalize_path(arg)
            if name:
                names.append(name)
    return names


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


def _with_contract_tier(
    root: Path,
    selection: NeighbourSelection,
    changed_paths: Iterable[Any],
    declared_commands: Sequence[str],
    candidate_root: Path | str | None,
) -> NeighbourSelection:
    """NF-2026-02245: append the contract-tier tests a changed path triggers.

    ``CONTRACT_TIER_RULES`` is read in table order; each rule test is added at most
    once, outside the neighbour cap, with the reason of the first rule that selected
    it. A test the declared validation names or the selection already holds as a
    whole file is not run twice, and ranked node ids of an added test fold into the
    whole file. A rule test missing from ``root`` is named in
    ``contract_tier_skipped`` and adds nothing.
    """
    if selection.limit == 0:
        return selection
    changed = sorted({_advice._normalize_path(path) for path in changed_paths} - {""})
    if not changed:
        return selection
    named = _advice._command_names(
        command for command in declared_commands if isinstance(command, str)
    )
    present = set(selection.tests)
    added: dict[str, str] = {}
    skipped: dict[str, str] = {}
    for rule in CONTRACT_TIER_RULES:
        trigger = _tier_trigger(rule, root, changed, candidate_root)
        if not trigger:
            continue
        for test in rule.tests:
            if test in added or test in skipped or test in present:
                continue
            if _advice._covers(named, test):
                continue
            if not (root / test).is_file():
                skipped[test] = f"{CONTRACT_TIER_PREFIX}_missing:{rule.rule}"
                continue
            added[test] = f"{CONTRACT_TIER_PREFIX}:{rule.rule}:{trigger}"
    if not added and not skipped:
        return selection
    kept = tuple(test for test in selection.tests if test.split("::", 1)[0] not in added)
    folded = len(selection.tests) - len(kept)
    node_count = selection.node_count
    if selection.selection_mode == "node":
        node_count = max(0, node_count - folded)
    return replace(
        selection,
        tests=kept + tuple(added),
        related_count=max(0, selection.related_count - folded),
        node_count=node_count,
        contract_tier=tuple(added),
        contract_tier_reasons=tuple(
            {"test": test, "reason": reason} for test, reason in added.items()
        ),
        contract_tier_skipped=tuple(
            {"test": test, "reason": reason} for test, reason in skipped.items()
        ),
    )


def _tier_trigger(
    rule: ContractTierRule,
    root: Path,
    changed: Sequence[str],
    candidate_root: Path | str | None,
) -> str:
    """The changed path(s) that trigger ``rule``, as one bounded reason token, or ``""``."""
    if rule.trigger == TIER_TRIGGER_CHANGED:
        hits = [path for path in changed if path in rule.paths]
    elif rule.trigger == TIER_TRIGGER_NEW_RELATIVE_IMPORT:
        hits = []
        for path in changed:
            if path not in rule.paths:
                continue
            state = _relative_import_change(root, path, candidate_root)
            if state == TIER_TRIGGER_NEW_RELATIVE_IMPORT:
                hits.append(path)
            elif state:
                hits.append(f"{path}#{state}")
    elif rule.trigger == TIER_TRIGGER_ADDED_TEST:
        hits = [path for path in changed if _is_added_test(root, path, candidate_root)]
    else:  # an unknown trigger is a table defect: name it rather than skip the rule
        raise ValueError(f"unknown contract tier trigger: {rule.trigger}")
    if not hits:
        return ""
    return hits[0] if len(hits) == 1 else f"{hits[0]}+{len(hits) - 1}"


def _relative_import_change(root: Path, path: str, candidate_root: Path | str | None) -> str:
    """``new_relative_import`` when the candidate adds a relative import to ``path``.

    ``""`` when it adds none; ``unverified`` when either copy cannot be read or no
    candidate copy was given -- an unknown import set selects the rule's tests.
    """
    if candidate_root is None:
        return "unverified"
    before = _relative_imports(root / path)
    after = _relative_imports(Path(candidate_root) / path)
    if before is None or after is None:
        return "unverified"
    return TIER_TRIGGER_NEW_RELATIVE_IMPORT if after - before else ""


def _relative_imports(path: Path) -> frozenset[tuple[int, str, str]] | None:
    """Every ``from .x import y`` binding of ``path``; empty when absent, None when unreadable."""
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return frozenset()
    except OSError:
        return None
    if (
        _is_link(status)
        or not stat.S_ISREG(status.st_mode)
        or status.st_size > TIER_IMPORT_SCAN_MAX_BYTES
    ):
        return None
    try:
        tree = ast.parse(path.read_bytes())
    except (OSError, SyntaxError, ValueError):
        return None
    return frozenset(
        (node.level, node.module or "", alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level
        for alias in node.names
    )


def _is_added_test(root: Path, path: str, candidate_root: Path | str | None) -> bool:
    """A changed ``tests/**.py`` file ``root`` lacks and the candidate (when given) holds."""
    if not (path.startswith(f"{_TESTS_DIR}/") and path.endswith(".py")):
        return False
    if os.path.lexists(root / path):
        return False
    return candidate_root is None or os.path.lexists(Path(candidate_root) / path)


def _with_referencing(
    root: Path,
    selection: NeighbourSelection,
    changed_paths: Iterable[Any],
    declared_commands: Sequence[str],
) -> NeighbourSelection:
    """Add the tests that load a changed module by path or ratchet its size.

    Ratchet tests first, then path order, at most ``MAX_REFERENCING_TESTS``;
    tests the declared validation names, the selection holds or
    ``INVARIANT_TESTS`` lists are left out. NF-2026-02298: when the impact lane did
    not decide, a changed non-Python path also selects the tests whose text names its
    repo-relative path (rank 2) or a distinctive basename (rank 3), after the stem ranks.
    """
    if selection.limit == 0:
        return selection
    stems = sorted(
        {path.rsplit("/", 1)[-1][: -len(".py")] for path in _production_paths(changed_paths)}
        - {"__init__"}  # a package's __init__.py is named by fixtures that build packages
    )
    extra = [] if selection.lane == LANE_IMPACT_MAP else _non_python_paths(changed_paths)
    if extra:  # the receipt shows why these tests ran
        named_paths = tuple(dict.fromkeys((*selection.production_paths, *extra)))
        selection = replace(selection, production_paths=named_paths)
    basenames = sorted({name for name in map(_distinctive_basename, extra) if name})
    if not stems and not extra:
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
    literal, ratchet = _stem_patterns(stems) if stems else (None, None)
    needles = (
        tuple(f"{stem}.py" for stem in stems)
        + tuple(f"{stem.upper()}_MAX_LINES" for stem in stems)
        + tuple(extra)
        + tuple(basenames)
    )

    def rank(item: tuple[str, Path]) -> int | None:
        text = _read_test(item[1])
        # Either pattern needs one of these substrings; ``in`` is far cheaper than a regex.
        if not any(needle in text for needle in needles):
            return None
        found = _referencing_rank(text, literal, ratchet) if literal and ratchet else None
        if found is not None:
            return found
        if any(path in text for path in extra):
            return 2
        return 3 if any(name in text for name in basenames) else None

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


def _collected_tests(root: Path, *, bounded: bool = True) -> list[tuple[str, Path]]:
    """Collected test files under ``root/tests``, sorted, never through a link.

    When ``bounded``, files over ``REFERENCING_SCAN_MAX_BYTES`` are skipped; an
    unreadable directory raises, and the caller records that the scan failed.
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
                    and (not bounded or status.st_size <= REFERENCING_SCAN_MAX_BYTES)
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


def _direct_importers(
    root: Path,
    changed_paths: Iterable[Any],
    declared_commands: Sequence[str],
    limit: int,
) -> tuple[tuple[str, ...], int, str]:
    """NF-2026-02390: ``(direct, omitted, reason)`` for the changed ``src/`` modules.

    Every collected test that imports a changed module (see
    :mod:`aiworkhub.accept_neighbour_direct`), minus the tests the declared
    validation names and ``INVARIANT_TESTS``, capped at ``DIRECT_IMPORTER_CAP``
    with the overflow counted and named. Within the cap the files keep path
    order. On overflow they are first ordered by the ranked lane's testmap score
    (highest first, ties by path), so the cut drops the least-related importers
    and never the alphabetically last ones. The extra testmap lookup runs only
    on overflow. When the scores cannot be read, path order decides and the
    reason says ``unranked``.
    """
    if limit == 0:
        return (), 0, ""
    production = _production_paths(changed_paths)
    modules = _direct.changed_modules(production)
    if not modules:
        return (), 0, ""
    named = _advice._command_names(
        command for command in declared_commands if isinstance(command, str)
    )
    candidates = [
        (test, path)
        for test, path in _collected_tests(root, bounded=False)
        if test not in INVARIANT_TESTS and not _advice._covers(named, test)
    ]
    found = _direct.scan_direct_importers(
        candidates, modules, workers=_scan_workers(len(candidates))
    )
    if len(found) <= _direct.DIRECT_IMPORTER_CAP:
        return _direct.cap_direct(found)
    unranked = ""
    try:
        consulted = tuple(production[: _advice.MAX_PRODUCTION_PATHS])
        _freshness, best, _unavailable, answered = _testmap_related(root, consulted, named)
    except Exception as exc:  # unknown ranking: scan order, and the reason says so
        answered, unranked = False, f":unranked:{type(exc).__name__}"
    else:
        if not answered:  # an unanswered testmap is no ranking: never present it as one
            unranked = ":unranked:testmap_not_answered"
    if answered:
        scores = {test: float(score) for test, (score, _path) in best.items()}
        found = tuple(sorted(found, key=lambda test: (-scores.get(test, 0.0), test)))
    kept, omitted, reason = _direct.cap_direct(found)
    return kept, omitted, reason + unranked


def _testmap_related(
    root: Path, consulted: Sequence[str], named: Any, freshness: Any = None
) -> tuple[Any, dict[str, Any], list[Any], bool]:
    """The ranked lane's testmap lookup: ``(freshness, best, unavailable, answered)``.

    ``best`` maps a test path to ``(score, path)``. Both the ranked lane and the
    direct tier's overflow order read scores here. ``freshness`` is read when not
    given, and the call raises when it cannot be read.
    """
    if freshness is None:
        freshness = _advice._freshness_states(root, tuple(consulted))
    best, unavailable, _truncated, answered = _advice._related_tests(
        root, tuple(consulted), freshness, named
    )
    return freshness, best, list(unavailable), bool(answered)


def _with_direct(
    selection: NeighbourSelection, direct: tuple[str, ...], omitted: int, reason: str
) -> NeighbourSelection:
    """Append the direct importers no entry covers; every existing entry stays as it is.

    A file is covered by an equal entry or by a node id of it (``file::...``), so a
    node id is never replaced by its file and the order above never changes.
    """
    if not direct and not omitted and not reason:
        return selection
    covered = {test.split("::", 1)[0] for test in selection.tests}
    added = tuple(test for test in direct if test not in covered)
    return replace(
        selection,
        tests=selection.tests + added,
        direct=direct,
        direct_omitted=omitted,
        direct_reason=reason,
        direct_added=len(added),
    )


def _select(
    root: Path,
    changed_paths: Iterable[Any],
    declared_commands: Sequence[str],
    limit: int,
    candidate_root: Path | str | None = None,
) -> NeighbourSelection:
    """The testmap-ranked lane; it never depends on the NF-2026-02390 direct tier."""
    if limit == 0:
        return _empty("neighbour_limit_zero", limit)
    production = _production_paths(changed_paths)
    if not production:
        return _empty("no_changed_production_python", limit)
    consulted = tuple(production[: _advice.MAX_PRODUCTION_PATHS])
    named = _advice._command_names(
        command for command in declared_commands if isinstance(command, str)
    )
    try:
        freshness = _advice._freshness_states(root, consulted)
    except Exception as exc:  # unresolved repo, missing or unreadable index, ...
        return _empty(_advice._failure_reason(exc), limit, production_paths=consulted)
    freshness, best, unavailable, answered = _testmap_related(root, consulted, named, freshness)
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
    """xdist workers for one neighbour run (NF-2026-02069, NF-2026-02434).

    ``max(1, min(distinct test files, cores - headroom))`` with
    ``headroom = max(2, cores // 4)`` and ``cores`` sampled now, so the run never
    takes every core and never starts more workers than it has files. The result
    never exceeds ``validation_runner.bounded_xdist_workers()`` for the same host
    facts, so the memory bound of the worker lane applies here too.
    """
    from .validation_runner import bounded_xdist_workers

    files = {str(test).split("::", 1)[0] for test in tests}
    cores = os.cpu_count() or 1
    headroom = max(2, cores // 4)
    return max(1, min(len(files), cores - headroom, bounded_xdist_workers()))


def neighbour_command(tests: Sequence[str], *, serial: bool = False) -> str:
    """One ``python -m pytest`` over ``tests`` with an explicit ``-n`` width.

    The width is always stated, so the repository's ``-n auto`` addopts never
    decides it; ``serial`` is ``-n 0``. ``-rfE`` keeps the FAILED/ERROR summary
    lines even when a consumer ``addopts`` sets a narrower ``-r`` (NF-2026-02435).
    """
    width = 0 if serial else neighbour_width(tests)
    return f"python -m pytest -n {width} -rfE " + " ".join(tests) + " -q"


def neighbour_authority(card: Mapping[str, Any], command: str) -> dict[str, Any]:
    """The card with ``command`` as its only validation and no card-bound keys."""
    authority = {key: value for key, value in card.items() if key not in _CARD_ONLY_KEYS}
    authority["validation"] = [command]
    return authority


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


def _digest_entries(row: Mapping[str, Any]) -> tuple[list[tuple[str, str]], int]:
    """``(test, short-summary line)`` pairs and the omitted count of a row's digest.

    The line mirrors the pytest short-summary format the tail regex captures:
    ``FAILED|ERROR <node>[ - <message>]``, from the digest item's own kind/message.
    """
    digest = row.get("pytest_failure_digest")
    if not isinstance(digest, Mapping):
        return [], 0
    failures = digest.get("failures")
    if not isinstance(failures, (list, tuple)):
        return [], 0
    omitted = digest.get("omitted")
    omitted_count = omitted if isinstance(omitted, int) and omitted > 0 else 0
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in failures:
        if not isinstance(item, Mapping):
            continue
        name = item.get("test")
        if not isinstance(name, str) or not name.strip() or name in seen:
            continue
        seen.add(name)
        kind = "ERROR" if str(item.get("kind") or "").lower() == "error" else "FAILED"
        message = item.get("message")
        line = f"{kind} {name}"
        if isinstance(message, str) and message.strip():
            line = f"{line} - {message.strip()}"
        entries.append((name, line))
    return entries, omitted_count


def _failed_tests(
    rows: Iterable[Mapping[str, Any]], limit: int = MAX_REPORTED_FAILURES
) -> tuple[list[str], str]:
    """Failed test names: the digest first, the tail regex as fallback and top-up.

    The 4 KB output tails can miss the summary lines of a large run, while every
    row carries a digest parsed from the full stdout. A row whose digest names no
    test, or whose digest dropped some (``omitted > 0``), is also scanned with
    ``_PYTEST_FAILURE``; new names are merged deduped. At most ``limit`` names
    are returned.
    """
    failed: list[str] = []
    first_line = ""
    for row in rows:
        entries, omitted = _digest_entries(row)
        for node, line in entries:
            if not first_line:
                first_line = line
            if node not in failed and len(failed) < limit:
                failed.append(node)
        if entries and not omitted:
            continue
        for key in ("stdout_tail", "stderr_tail"):
            raw = row.get(key)
            if not isinstance(raw, str):
                continue
            text = _strip_ansi(raw)
            for match in _PYTEST_FAILURE.finditer(text):
                node = match.group(1)
                if not first_line:
                    first_line = text[match.start() :].split("\n", 1)[0]
                if node not in failed and len(failed) < limit:
                    failed.append(node)
    return failed[:limit], first_line[:_FIRST_FAILURE_CHARS]


def _red(rows: Sequence[Mapping[str, Any]]) -> bool:
    return any(row.get("returncode") != 0 or row.get("timed_out") for row in rows)


def _env_skipped_only(rows: Sequence[Mapping[str, Any]]) -> bool:
    """True when every red row is a sandbox env-skip row whose pytest exit was 0.

    NF-2026-02319: an AppContainer-skipped neighbour test rewrites its green row to
    ``returncode`` None (NF-2026-02307), but pytest itself exited 0, so every
    collected non-skipped test passed. A timed-out row, a real non-zero returncode,
    or an env-skip row whose ``pytest_returncode`` is not 0 is never excused.
    """
    red = [row for row in rows if row.get("returncode") != 0 or row.get("timed_out")]
    return bool(red) and all(
        row.get(VALIDATION_ENV_SKIPPED)
        and row.get("pytest_returncode") == 0
        and not row.get("timed_out")
        for row in red
    )


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


def _output_tail(rows: Sequence[Mapping[str, Any]]) -> str:
    """The last ``_OUTPUT_TAIL_CHARS`` of the first red row that names no node id.

    NF-2026-02408: stdout_tail then stderr_tail, ANSI stripped, so a refusal whose
    ``failed`` list is empty still shows what pytest printed.
    """
    for row in rows:
        if row.get("returncode") == 0 and not row.get("timed_out"):
            continue
        parts = [
            _strip_ansi(text)
            for text in (row.get("stdout_tail"), row.get("stderr_tail"))
            if isinstance(text, str) and text
        ]
        if parts and not any(_PYTEST_FAILURE.search(part) for part in parts):
            return "\n".join(parts)[-_OUTPUT_TAIL_CHARS:]
    return ""


def _fit_detail(detail: dict[str, Any]) -> str:
    """``detail`` as JSON within ``NEIGHBOUR_FAILURE_DETAIL_CHARS``, trimming the
    output tail first and then the test list, never the JSON structure."""
    text = json.dumps(detail)
    tail = detail.get("output_tail")
    if len(text) > NEIGHBOUR_FAILURE_DETAIL_CHARS and tail:
        # JSON escaping makes the cost per char vary: keep the longest suffix that fits.
        low, high = 0, len(tail)
        while low < high:
            mid = (low + high + 1) // 2
            detail["output_tail"] = tail[-mid:]
            if len(json.dumps(detail)) <= NEIGHBOUR_FAILURE_DETAIL_CHARS:
                low = mid
            else:
                high = mid - 1
        if low:
            detail["output_tail"] = tail[-low:]
        else:
            del detail["output_tail"]
        text = json.dumps(detail)
    while len(text) > NEIGHBOUR_FAILURE_DETAIL_CHARS and detail["tests"]:
        detail["tests"] = detail["tests"][:-1]
        text = json.dumps(detail)
    # Long node ids in ``failed`` or a JSON-escaped (non-ASCII) base_red_refused /
    # first_failure can still overflow: keep the first failure name, halve the texts.
    while len(text) > NEIGHBOUR_FAILURE_DETAIL_CHARS and len(detail["failed"]) > 1:
        detail["failed"] = detail["failed"][:-1]
        text = json.dumps(detail)
    for key in ("base_red_refused", "first_failure"):
        value = detail.get(key)
        while len(text) > NEIGHBOUR_FAILURE_DETAIL_CHARS and value:
            value = value[: len(value) // 2]
            detail[key] = value
            text = json.dumps(detail)
    # Backstop for one node id longer than the bound on its own.
    return text[:NEIGHBOUR_FAILURE_DETAIL_CHARS]


def _refusal(
    tests: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    error: str,
    default_failed: Sequence[str] = (),
    base_red_refused: str = "",
) -> WorkspaceError:
    failed, first_line = _failed_tests(rows)
    fallback = "neighbour_validation_failed" if rows else "neighbour_validation_returned_no_rows"
    detail: dict[str, Any] = {
        "tests": list(tests),
        "failed": failed or list(default_failed),
        "first_failure": (first_line or error or fallback)[:_FIRST_FAILURE_CHARS],
    }
    if not failed:
        tail = _output_tail(rows)
        if tail:
            detail["output_tail"] = tail
    if base_red_refused:
        # NF-2026-02101: why the still-red nodes were not excused as base_red.
        detail["base_red_refused"] = base_red_refused[:_FIRST_FAILURE_CHARS]
    return WorkspaceError(NEIGHBOUR_FAILURE_PREFIX + _fit_detail(detail))


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
    if rows and rerunnable and _env_skipped_only(rows):
        # NF-2026-02319: an env-skipped green row is evidence, not a refusal.
        evidence["validation"] = rows
        evidence["env_skipped"] = [
            dict(item) for row in rows for item in row.get("env_skipped_tests") or ()
        ]
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
    base_card = {key: value for key, value in card.items() if key not in ("rework_predecessor", "required_outputs")}
    base_card.setdefault("allowed_writes", list(getattr(workspace, "allowed_writes", ())))
    base = replace(
        _workspace.create_workspace(
            Path(repo), f"nbase_{uuid.uuid4().hex[:16]}", base_card, "validation",
            pinned_base_oid=head_oid,
        ),
        allowed_writes=(),
    )
    try:
        # create_workspace seeds a sparse closure; a neighbour test needs the full
        # tree, minus forbidden paths under the card's materialize/checkout rule.
        from . import worker_materialize

        worker_materialize.expand_full_tree(
            base.path, base_card, Path(repo), run=_workspace._run,
            timeout=_workspace.WORKTREE_CREATE_TIMEOUT_SECONDS,
            failure=lambda _detail: WorkspaceError("neighbour_base_full_tree_failed"),
        )
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
# NF-2026-02436: a red finalization record stops the card before review_ready,
# on the route a failing declared validation of the same kind takes. The kind
# is measured from the candidate snapshot's own last attempt, never guessed.
FINALIZATION_FAILURE_PREFIX = "finalization_" + NEIGHBOUR_FAILURE_PREFIX
FINALIZATION_UNRUNNABLE_PREFIX = "finalization_neighbour_tests_unrunnable:"
FAILURE_KIND_TEST = "test_failure"  # a test verdict -> ValidationRunError (validation_failed)
FAILURE_KIND_ENVIRONMENT = "environment_blocked"  # -> ValidationEnvironmentBlocked
FAILURE_KIND_UNRUNNABLE = "unrunnable"  # no verdict at all -> WorkspaceError (finalize_failed)


def _refusal_detail(error: str) -> dict[str, Any]:
    """The JSON detail of a ``neighbour_tests_failed:`` text, ``{}`` when unparseable."""
    if not error.startswith(NEIGHBOUR_FAILURE_PREFIX):
        return {}
    try:
        detail = json.loads(error[len(NEIGHBOUR_FAILURE_PREFIX):])
    except ValueError:
        return {}
    return detail if isinstance(detail, dict) else {}


def _failure_kind(cause: BaseException | None, failed_names: Sequence[str]) -> str:
    """Name what a red neighbour run proved (NF-2026-02436).

    Only a run whose refusal (or candidate rows) names failed tests is a test
    verdict; a refusal with no named failure is ``unrunnable``, never a test
    failure, so it is not routed to ``validation_failed`` without evidence.
    """
    if isinstance(cause, _workspace.ValidationEnvironmentBlocked):
        return FAILURE_KIND_ENVIRONMENT
    if failed_names:
        return FAILURE_KIND_TEST
    return FAILURE_KIND_UNRUNNABLE


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
    evidence-free run is recorded with ``passed: False``, its refusal text under
    ``error``, the failed node ids under ``failed``, the last candidate attempt's
    rows under ``validation`` and its ``failure_kind``; the caller routes it with
    :func:`finalization_neighbour_refusal` (NF-2026-02436).
    """
    selection = select_neighbour_tests(
        repo_root, candidate_changed_paths, declared_commands, candidate_root=candidate_root
    )
    if not selection.tests:
        return None
    last: dict[str, Any] = {}

    def candidate_run(ws: Any, authority: Mapping[str, Any], meta: Mapping[str, Any]) -> Any:
        # Only the candidate snapshot's attempts decide the failure kind; a
        # base_red run on canonical HEAD is judged inside run_neighbour_tests.
        if ws is not workspace:
            return run(ws, authority, meta)
        last.clear()
        try:
            rows = run(ws, authority, meta)
        except BaseException as exc:
            last.update(rows=[dict(row) for row in getattr(exc, "results", None) or []], exc=exc)
            raise
        last.update(rows=[dict(row) for row in rows], exc=None)
        return rows

    started = clock()
    try:
        evidence = run_neighbour_tests(
            selection, card, run=candidate_run, workspace=workspace,
            route_metadata=route_metadata, clock=clock,
        )
    except Exception as exc:  # recorded, never raised here: the caller routes it
        failed = selection.evidence()
        failed["command"] = neighbour_command(selection.tests)
        failed["duration_seconds"] = round(max(0.0, clock() - started), 3)
        failed["passed"] = False
        failed["error"] = (str(exc) or type(exc).__name__)[:NEIGHBOUR_FAILURE_DETAIL_CHARS]
        rows = list(last.get("rows") or [])
        cause = last["exc"] if last else exc
        detail = _refusal_detail(str(exc))
        names = detail.get("failed")
        failed["failed"] = (
            [str(name) for name in names][:MAX_REPORTED_FAILURES]
            if isinstance(names, list) else _failed_tests(rows)[0]
        )
        failed["first_failure"] = str(
            detail.get("first_failure") or failed["error"]
        )[:_FIRST_FAILURE_CHARS]
        failed["failure_kind"] = _failure_kind(cause, failed["failed"])
        if failed["failure_kind"] == FAILURE_KIND_ENVIRONMENT:
            failed["restriction"] = str(getattr(cause, "restriction", "") or "")
        if rows:
            failed["validation"] = rows
        return failed
    evidence["passed"] = True
    return evidence


def red_neighbour_view(recorded: Any) -> dict[str, Any] | None:
    """NF-2026-02436: the named view of a red neighbour record, else ``None``.

    Only ``passed is False`` is red; a green, absent or malformed record is not.
    A record written before ``failed`` was stored falls back to its refusal text.
    """
    if not isinstance(recorded, Mapping) or recorded.get("passed") is not False:
        return None
    error = str(recorded.get("error") or "")
    detail = _refusal_detail(error)
    names = recorded.get("failed")
    if not isinstance(names, list):
        names = detail.get("failed")
    selected = recorded.get("selected")
    return {
        "selected": [str(test) for test in selected] if isinstance(selected, list) else [],
        "failed": (
            [str(name) for name in names][:MAX_REPORTED_FAILURES]
            if isinstance(names, list) else []
        ),
        "failure_kind": str(recorded.get("failure_kind") or ""),
        "first_failure": str(
            recorded.get("first_failure") or detail.get("first_failure") or error
        )[:_FIRST_FAILURE_CHARS],
    }


def _view_detail(view: Mapping[str, Any], **extra: Any) -> str:
    return _fit_detail({
        "tests": list(view["selected"]), "failed": list(view["failed"]),
        "first_failure": str(view["first_failure"]), **extra,
    })


def finalization_neighbour_refusal(
    recorded: Any, validations: Sequence[Mapping[str, Any]]
) -> WorkspaceError | None:
    """The error a red finalization record finalizes with, ``None`` when it is not red.

    A test verdict is a ``ValidationRunError`` (``validation_failed``, the
    declared-validation failure route), an environment block a
    ``ValidationEnvironmentBlocked`` (as a declared one), and a run with no
    verdict a plain ``WorkspaceError`` (``finalize_failed``). The rows are the
    declared ``validations`` then the neighbour run's own, stamped
    ``finalization_neighbour``, whose digests name the failed tests.
    """
    view = red_neighbour_view(recorded)
    if view is None:
        return None
    rows = [dict(row) for row in validations] + [
        {**dict(row), "finalization_neighbour": True}
        for row in recorded.get("validation") or ()
        if isinstance(row, Mapping)
    ]
    text = _view_detail(view, failure_kind=view["failure_kind"])
    if view["failure_kind"] == FAILURE_KIND_TEST:
        return ValidationRunError("validation_failed:" + FINALIZATION_FAILURE_PREFIX + text, rows)
    if view["failure_kind"] == FAILURE_KIND_ENVIRONMENT:
        return _workspace.ValidationEnvironmentBlocked(
            "validation_environment_blocked:" + FINALIZATION_FAILURE_PREFIX + text, rows,
            restriction=str(recorded.get("restriction") or "") or "finalization_neighbour_tests",
        )
    return WorkspaceError(FINALIZATION_UNRUNNABLE_PREFIX + text)


def _matched_finalization_record(
    finalizer_reuse: Mapping[str, Any] | None,
    union_reuse: Mapping[str, Any] | None,
    selection: NeighbourSelection,
) -> Mapping[str, Any] | None:
    """The finalizer's neighbour record for this exact tree and selection, else ``None``.

    Needs all of: a non-empty accept-time selection; the accept's union
    validation having reused this exact finalizer record (same snapshot, no
    accept-union run of its own -- which a manager correction already rules
    out, since it yields no finalizer record); and a recorded selection equal
    to ``selection.tests``. Green or red is the caller's question.
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
    return recorded


def reused_finalization_neighbours(
    finalizer_reuse: Mapping[str, Any] | None,
    union_reuse: Mapping[str, Any] | None,
    selection: NeighbourSelection,
) -> dict[str, Any] | None:
    """The finalizer's green neighbour evidence when accept may reuse it, else ``None``.

    Reuse needs :func:`_matched_finalization_record` and recorded ``passed is True``.
    """
    recorded = _matched_finalization_record(finalizer_reuse, union_reuse, selection)
    if recorded is None or recorded.get("passed") is not True:
        return None
    return {**dict(recorded), "reused_from": FINALIZATION_REUSE_SOURCE}


def refuse_recorded_red_neighbours(
    finalizer_reuse: Mapping[str, Any] | None,
    union_reuse: Mapping[str, Any] | None,
    selection: NeighbourSelection,
) -> None:
    """NF-2026-02436: refuse from a red finalization record on this exact tree.

    The same gate as reuse (:func:`_matched_finalization_record`), so the known
    failure is not re-run in full; a different tree identity, selection or a
    moved HEAD the closure does not cover runs the neighbours again. Only a
    recorded test verdict refuses: an unrunnable or environment-blocked record
    (or a legacy record naming no failed test) proved nothing about the tree,
    so accept re-runs the neighbours as before.
    """
    view = red_neighbour_view(
        _matched_finalization_record(finalizer_reuse, union_reuse, selection)
    )
    if view is None:
        return
    kind = view["failure_kind"]
    if kind != FAILURE_KIND_TEST and (kind or not view["failed"]):
        return
    raise WorkspaceError(
        NEIGHBOUR_FAILURE_PREFIX + _view_detail(view, reused_from=FINALIZATION_REUSE_SOURCE)
    )
