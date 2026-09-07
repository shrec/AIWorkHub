"""Invariants this repository declares about itself, checked mechanically.

RM-2026-00048. ``development_rules.json`` says what must never be true in prose;
a rule nobody executes is a comment. This module states the same things as
predicates over the tree and returns violations, so a claim like "one concept has
one definition" is a gate rather than an aspiration.

``check`` reads that manifest and reports, for every rule it declares, whether an
executable predicate covers it. It did not always: it hardcoded its own list,
named the manifest only in a comment, and returned ``unevaluated: []`` with
``passed: true`` while 15 of the 20 declared rules had no detector at all. A gate
that silently covers a quarter of what it is trusted for is worse than no gate,
so the honest split is now in the report itself -- ``RULE_DETECTORS`` maps each
rule's forbidden tokens to the detectors that execute them, every token that does
not execute is listed by name in ``unevaluated`` with the reason, and
``all_declared_obligations_checked`` says plainly that the answer today is 5 of
63. A rule added to the manifest with no detector and no written reason fails
this module immediately, the way
``dependency_autolaunch.unclassified_denial_reasons`` fails on a new reason.

Every invariant here is the shape of a defect this repository actually had, with
the measurement that found it. None is a general style preference:

``terminal_vocabulary_has_one_owner``
    The terminal-outcome vocabulary existed in six places and three had drifted,
    so an outcome ``process_launcher`` produced was illegal to ``task_fsm`` and
    the card could never be recorded. Fixed by making the other sites reference
    ``task_fsm``'s objects; this asserts they still do, by identity rather than
    by equality, because two equal-but-separate sets are exactly what drifted.

``module_level_caches_are_bounded``
    Two probe caches keyed by git HEAD grew one entry per commit forever inside a
    long-lived server. ``development_rules`` already forbids ``cache_without_bound``;
    this finds the ones that are.

``sqlite_context_managers_close``
    ``sqlite3.Connection.__exit__`` commits the transaction and does NOT close the
    connection. Measured: 50 sequential ``with sqlite3.connect(p) as c`` blocks
    left 50 extra open file descriptors. Nine call sites relied on that block to
    scope a connection; exactly one in the repository did it correctly.

``one_policy_one_predicate``
    ``chmod_fd`` and ``chmod_path`` decided "do POSIX mode bits apply here" by two
    different rules, and only one of them was testable.

``copied_helpers_have_one_definition`` and ``parallel_implementations_have_one_owner``
    The other two things ``single_definition`` forbids, and the only two that
    cannot be checked by listing sites: they are claims about every definition
    against every other. Measured over all 4,427 definitions in the package: 37
    are byte-identical copies of another body, and 82 more share a body's exact
    shape with a different implementation of it -- ``_connect`` written out three
    times, ``credential_path`` and ``bootstrap_credential`` in both credential
    modules, four separate pairs across ``server`` and ``stdio_fastmcp``. Both
    are ratchets against a declared baseline rather than hard failures, because
    the first exhaustive scan of a 161,710-line package finds the accumulated
    history of the package and failing on all of it would block every card.
    The count may not grow, and shrinks.

``recent_decisions_record_a_lesson``
    The learning duty was named at the decision, measured in health, and enforced
    nowhere: ``accept_review`` and ``reject_review`` returned the exact arguments
    for the lesson they owed, and skipping it cost nothing. Measured: 21.9 percent
    coverage, 48 lessons arriving in bursts separated by runs of 6, 14, 15 and 38
    decisions with none. This is the one invariant about what the repository DID
    rather than what its source says, so it needs a repository root; without one
    it reports itself unevaluated rather than clean.

The tree and runtime invariants read source text and the canonical modules; the
repository invariants additionally read the canonical stores, read-only. The
checker performs no writes, imports nothing from the repository beyond what it
inspects, and reports a violation as a named, bounded record rather than raising.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA_ID = "aiworkhub.declared_invariants.v1"

MAX_VIOLATIONS_PER_INVARIANT = 50


@dataclass(frozen=True)
class Violation:
    """One named breach of a declared invariant, with where and why."""

    invariant: str
    path: str
    detail: str
    line: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "invariant": self.invariant,
            "path": self.path,
            "detail": self.detail,
            "line": self.line,
        }


def _python_sources(src_root: Path) -> list[Path]:
    if not src_root.is_dir():
        raise NotADirectoryError(f"source root is not a directory: {src_root}")
    return sorted(p for p in src_root.rglob("*.py") if p.is_file())


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        error = OSError(f"source read failed ({type(exc).__name__})")
        error.filename = str(path)
        raise error from exc


# --------------------------------------------------------------------------- #
# one concept, one definition
# --------------------------------------------------------------------------- #

_VOCABULARY_IDENTITIES: tuple[tuple[str, str, str, str], ...] = (
    # (module, attribute, owner module, owner attribute)
    ("process_launcher", "TERMINAL_PROCESS_STATES", "task_fsm", "LAUNCHER_TERMINAL_SUBSTATUSES"),
    ("callback_store", "CALLBACK_ELIGIBLE_TRANSITIONS", "task_fsm", "TERMINAL_CALLBACK_CLASSES"),
    ("task_store", "_ATOMIC_CALLBACK_TRANSITIONS", "task_fsm", "TERMINAL_CALLBACK_CLASSES"),
)


def terminal_vocabulary_has_one_owner() -> list[Violation]:
    """Each restated terminal vocabulary must BE the owner's object, not equal it.

    Equality is not enough: two equal frozensets are what six hand-copied
    vocabularies looked like the day before three of them drifted.
    """

    from . import task_fsm  # local import: this module must stay importable alone

    violations: list[Violation] = []
    for module_name, attribute, owner_name, owner_attribute in _VOCABULARY_IDENTITIES:
        try:
            module = __import__(f"aiworkhub.{module_name}", fromlist=[module_name])
        except Exception as exc:  # noqa: BLE001 - a missing module is the violation
            violations.append(Violation(
                "terminal_vocabulary_has_one_owner", f"src/aiworkhub/{module_name}.py",
                f"module could not be imported: {type(exc).__name__}",
            ))
            continue
        owner_value = getattr(task_fsm, owner_attribute, None)
        value = getattr(module, attribute, None)
        if value is None or owner_value is None:
            violations.append(Violation(
                "terminal_vocabulary_has_one_owner", f"src/aiworkhub/{module_name}.py",
                f"{attribute} or {owner_name}.{owner_attribute} is missing",
            ))
        elif value is not owner_value:
            same = "equal but separate" if value == owner_value else "DIFFERENT"
            violations.append(Violation(
                "terminal_vocabulary_has_one_owner", f"src/aiworkhub/{module_name}.py",
                f"{attribute} is {same} from {owner_name}.{owner_attribute}; "
                "it must be the same object, not a copy",
            ))
    return violations


def one_policy_one_predicate() -> list[Violation]:
    """chmod_fd and chmod_path must decide applicability the same way."""

    from . import platform_io

    violations: list[Violation] = []
    source = _read(Path(platform_io.__file__))
    for name in ("chmod_fd", "chmod_path"):
        match = re.search(rf"def {name}\(.*?(?=\ndef )", source, re.S)
        body = match.group(0) if match else ""
        if "posix_path_modes_supported" not in body:
            violations.append(Violation(
                "one_policy_one_predicate", "src/aiworkhub/platform_io.py",
                f"{name} does not decide POSIX-mode applicability through "
                "posix_path_modes_supported; one policy answered by two "
                "predicates is how they diverge",
            ))
    return violations


# --------------------------------------------------------------------------- #
# one concept, one definition: duplication across the whole package
# --------------------------------------------------------------------------- #
#
# ``single_definition`` forbids four things. ``restated_vocabulary`` and
# ``two_predicates_one_policy`` are checked above by asserting identity between
# named objects: both are about three or four sites someone can list. The other
# two are not -- ``copied_helper`` and ``parallel_implementation`` are claims
# about every definition in the package against every other, so the only honest
# detector is an exhaustive pass.
#
# Source Graph has a ``duplicates`` lens, and it is not usable as a gate: it caps
# at 200 eligible symbols per query and samples rather than enumerates. A gate
# that reads a sample reports "clean" for everything the sample missed. This
# reads all 4,427 function and method definitions in the package, every time.
#
# The comparison is exact structural equality of the AST, in two strengths:
#
#   copied_helper           the ``ast.dump`` of the body, docstring removed and
#                           line numbers excluded, is byte-identical. Every name,
#                           attribute, call and literal must match. This is a
#                           literal copy, renamed at most in the def line.
#
#   parallel_implementation the same dump with every identifier and every
#                           literal erased is identical, while the strict dump
#                           is not. Same algorithm, independently written or
#                           since drifted -- the shape ``deepseek_credentials``
#                           and ``glm_credentials`` are in, and ``server`` and
#                           ``stdio_fastmcp``.
#
# What this provably catches: any duplicate whose statement structure is
# identical, including one renamed throughout, across the whole package rather
# than a sample.
#
# What it provably does not catch, measured on this tree:
#   * anything below the node thresholds below -- including 14 separate copies
#     of a ``datetime.now(timezone.utc)`` helper, which is real duplication this
#     deliberately does not report;
#   * a copy with one statement added, removed or reordered. This is exact
#     structural equality, not similarity: there is no edit distance, so a
#     near-copy is invisible rather than partially reported;
#   * two implementations of one concept written with different structure -- a
#     loop against a comprehension computes the same thing and shares no shape;
#   * duplicated data. A restated constant table is not a function body;
#     ``restated_vocabulary`` covers three declared identities and no more;
#   * JavaScript and TypeScript. The manifest rule applies to all three
#     languages and this reads Python only, which is why ``single_definition``
#     reports as partially covered rather than covered.


@dataclass(frozen=True)
class _Definition:
    """One function or method definition, reduced to what duplication compares."""

    path: str
    qualname: str
    line: int
    exact: str
    shape: str
    nodes: int


# Which field of which node holds a bare name or literal rather than a child
# node. Keyed by node type and not by field name alone: ``value`` is the literal
# on ``Constant`` but the whole right-hand side on ``Assign`` and ``Return``, and
# erasing it by name collapsed every return statement in the package to one
# shape. Measured on this tree: that mistake reported 324 duplicate definitions
# where reading the fields exactly finds 82.
_ERASED_FIELDS: dict[type, frozenset[str]] = {
    ast.Name: frozenset({"id"}),
    ast.Attribute: frozenset({"attr"}),
    ast.arg: frozenset({"arg"}),
    ast.keyword: frozenset({"arg"}),
    ast.alias: frozenset({"name", "asname"}),
    ast.ImportFrom: frozenset({"module"}),
    ast.Constant: frozenset({"value", "kind"}),
    ast.FunctionDef: frozenset({"name"}),
    ast.AsyncFunctionDef: frozenset({"name"}),
    ast.ClassDef: frozenset({"name"}),
    ast.ExceptHandler: frozenset({"name"}),
    ast.Global: frozenset({"names"}),
    ast.Nonlocal: frozenset({"names"}),
}


def _shape_dump(node: Any) -> str:
    """Serialise an AST by structure alone, with every name and literal erased.

    Built as its own walk rather than by mutating and re-``ast.dump``-ing:
    erasing an outer function in place would destroy the nested definitions
    whose own digests have not been taken yet.
    """

    if isinstance(node, ast.AST):
        erased = _ERASED_FIELDS.get(type(node), frozenset())
        fields = []
        for name, value in ast.iter_fields(node):
            fields.append(f"{name}=_" if name in erased else f"{name}={_shape_dump(value)}")
        return f"{type(node).__name__}({','.join(fields)})"
    if isinstance(node, list):
        return "[" + ",".join(_shape_dump(item) for item in node) + "]"
    return repr(node)


def _body_without_docstring(node: ast.AST) -> list[ast.stmt]:
    body = list(getattr(node, "body", []))
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def scan_definitions(path: str, relative: str) -> list[_Definition]:
    """Return every definition in one module. Module level so a pool can call it."""

    source = _read(Path(path))
    tree = ast.parse(source, filename=path)
    found: list[_Definition] = []
    scope: list[str] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                scope.append(child.name)
                walk(child)
                scope.pop()
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = _body_without_docstring(child)
                if body:
                    module = ast.Module(body=body, type_ignores=[])
                    exact = hashlib.sha256(
                        ast.dump(module, annotate_fields=True, include_attributes=False).encode("utf-8")
                    ).hexdigest()
                    shape = hashlib.sha256(_shape_dump(module).encode("utf-8")).hexdigest()
                    nodes = sum(1 for _ in ast.walk(module))
                    found.append(_Definition(
                        relative, ".".join((*scope, child.name)), child.lineno, exact, shape, nodes,
                    ))
                scope.append(child.name)
                walk(child)
                scope.pop()
            else:
                walk(child)

    walk(tree)
    return found


# Measured on this repository, 2026-09-07, 16 cores, 152 modules / 161,710 lines:
#
#   sequential      2949 ms
#   threads(8)      3328 ms   -- slower: ast.parse holds the GIL, so threads only
#   threads(12)     3440 ms      add contention to work that never releases it
#   processes(8)     827 ms
#   processes(12)    667 ms   -- 4.4x
#
# Processes, then, because the work is CPU-bound parsing. Twelve of sixteen
# cores leaves four, so a scan cannot starve the interactive MCP server sharing
# this machine; the count is derived from the observed cores, never a constant.
#
# Below the crossover the pool costs more than it saves: measured pool start plus
# shutdown is 12-29 ms on fork, and far more on the spawn platforms macOS and
# Windows use, where each worker re-imports the package. At the measured 19 ms
# per module, 24 modules is ~450 ms of work, which dominates even a pessimistic
# spawn start. Under it, sequential.
_PARALLEL_SCAN_MIN_MODULES = 24
_SCAN_CORE_HEADROOM = 4


def _scan_workers(module_count: int) -> int:
    """Return the worker count for a scan of ``module_count`` modules."""

    if module_count < _PARALLEL_SCAN_MIN_MODULES:
        return 1
    cores = os.cpu_count() or 1
    return max(1, min(module_count, cores - _SCAN_CORE_HEADROOM))


def collect_definitions(src_root: Path) -> list[_Definition]:
    """Return every definition under ``src_root``, exhaustively.

    Parallel is a speed decision only: the pool maps the same module-level
    function over the same sorted file list, and the result is re-sorted, so it
    is identical to the sequential list. A pool that cannot start falls back
    rather than returning a shorter answer, because a duplication scan that
    quietly reads fewer files reports a cleaner tree than there is.
    """

    root = src_root.parent.parent
    jobs = [(str(path), path.relative_to(root).as_posix()) for path in _python_sources(src_root)]
    workers = _scan_workers(len(jobs))
    collected: list[_Definition] = []
    if workers > 1:
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                for batch in pool.map(scan_definitions, *zip(*jobs), chunksize=8):
                    collected.extend(batch)
        except Exception:  # noqa: BLE001 - a pool failure must not shrink the scan
            collected = []
            workers = 1
    if workers == 1:
        for path, relative in jobs:
            collected.extend(scan_definitions(path, relative))
    return sorted(collected, key=lambda d: (d.path, d.line, d.qualname))


def duplicate_definition_counts(
    src_root: Path, thresholds: dict[str, int]
) -> dict[str, dict[str, int]]:
    """Return, per pattern, how many definitions each module has in a duplicate group."""

    definitions = collect_definitions(src_root)
    by_exact: dict[str, list[_Definition]] = {}
    by_shape: dict[str, list[_Definition]] = {}
    for definition in definitions:
        by_exact.setdefault(definition.exact, []).append(definition)
        by_shape.setdefault(definition.shape, []).append(definition)

    counts: dict[str, dict[str, int]] = {"copied_helper": {}, "parallel_implementation": {}}
    copied_minimum = thresholds["copied_helper"]
    for group in by_exact.values():
        if len(group) < 2 or group[0].nodes < copied_minimum:
            continue
        for definition in group:
            counts["copied_helper"][definition.path] = counts["copied_helper"].get(definition.path, 0) + 1
    parallel_minimum = thresholds["parallel_implementation"]
    for group in by_shape.values():
        # A group with one exact digest is a copy, already counted above; two or
        # more distinct bodies sharing one shape is the parallel implementation.
        if len(group) < 2 or group[0].nodes < parallel_minimum:
            continue
        if len({definition.exact for definition in group}) < 2:
            continue
        for definition in group:
            key = "parallel_implementation"
            counts[key][definition.path] = counts[key].get(definition.path, 0) + 1
    return counts


def _ratchet_violations(
    invariant: str, pattern: str, current: dict[str, int], baseline: dict[str, int]
) -> list[Violation]:
    """Report only growth against the declared baseline: a descending ratchet.

    The first exhaustive scan of a package this size finds a great deal, and
    failing on all of it would block every card rather than improve anything.
    So the declared baseline is what exists, the count may never rise, and a
    module that was clean and stops being clean is a new identity, not a
    tolerated delta -- the same shape ``os_dependency_boundary`` uses.
    """

    violations: list[Violation] = []
    for path in sorted(current):
        count = current[path]
        allowed = baseline.get(path)
        if allowed is None:
            violations.append(Violation(
                invariant, path,
                f"{count} definition(s) newly enter a {pattern} group in a module "
                "the manifest baseline records as having none",
            ))
        elif count > allowed:
            violations.append(Violation(
                invariant, path,
                f"{pattern} definitions grew from {allowed} to {count}; this "
                "ratchet may only descend",
            ))
        if len(violations) >= MAX_VIOLATIONS_PER_INVARIANT:
            return violations
    return violations


def copied_helpers_have_one_definition(
    counts: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]
) -> list[Violation]:
    """No module may gain a byte-identical copy of another definition's body."""

    return _ratchet_violations(
        "copied_helpers_have_one_definition", "copied_helper",
        counts["copied_helper"], baseline.get("copied_helper", {}),
    )


def parallel_implementations_have_one_owner(
    counts: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]
) -> list[Violation]:
    """No module may gain a second independent implementation of one shape."""

    return _ratchet_violations(
        "parallel_implementations_have_one_owner", "parallel_implementation",
        counts["parallel_implementation"], baseline.get("parallel_implementation", {}),
    )


# --------------------------------------------------------------------------- #
# bounded caches

# --------------------------------------------------------------------------- #
# bounded caches
# --------------------------------------------------------------------------- #

_CACHE_NAME_RE = re.compile(r"^_[A-Z0-9_]*(?:CACHE|REGISTRY)[A-Z0-9_]*$")
_BOUND_HINT_RE = re.compile(r"MAX_[A-Z0-9_]*(?:ENTRIES|ENTRY|SIZE|ROWS)")
_EVICTION_RE = re.compile(r"\.\s*(?:popitem|pop|clear)\s*\(")

# Caches with no size bound because their key space is closed. Declared here
# with the reason rather than inferred, so a genuinely unbounded cache cannot
# join the list by looking similar. Each entry is a measured claim a reviewer
# can check, not an exemption granted for convenience.
BOUNDED_BY_CONSTRUCTION: dict[tuple[str, str], str] = {
    ("source_graph_daemon.py", "_REGISTRY"):
        "one daemon per repository root; the key space is the set of registered "
        "repositories and entries are popped on stop",
    ("worker_ai_tools_mcp.py", "_STORAGE_REGISTRY_CACHE"):
        "keyed by str(authority_repo); one entry per repository this process "
        "has served, not per request",
    ("review_orchestrator.py", "_ROUTING_CATALOG_CACHE"):
        "keyed by repository root, one entry per repository this process has "
        "served -- and unlike a read-only TTL, reset_routing_catalog_cache() "
        "clears the whole dict at the top of every drain() pass, so the key "
        "space cannot outlive a single pass",
}


def module_level_caches_are_bounded(src_root: Path) -> list[Violation]:
    """A module-level mutable cache must show an explicit eviction bound.

    Evidence of a bound is a max-entries constant plus an eviction call in the
    module, or the cache being an OrderedDict that is popped. A TTL consulted
    only on read is not a bound: it decides whether a HIT is fresh and never
    removes a key, which is exactly how two probe caches grew one entry per
    commit for the lifetime of a long-running server.
    """

    violations: list[Violation] = []
    for path in _python_sources(src_root):
        source = _read(path)
        if not source:
            continue
        tree = ast.parse(source, filename=str(path))
        has_bound_constant = bool(_BOUND_HINT_RE.search(source))
        for node in tree.body:
            targets: list[str] = []
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]
            elif isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            for name in targets:
                if not _CACHE_NAME_RE.match(name):
                    continue
                value = node.value
                is_container = isinstance(value, (ast.Dict, ast.DictComp)) or (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id in {"dict", "OrderedDict"}
                )
                if not is_container:
                    continue
                declared = BOUNDED_BY_CONSTRUCTION.get((path.name, name))
                if declared:
                    continue
                # Eviction may go through a shared helper that takes the cache as
                # a parameter -- which is the shape this repository moved TO, so
                # looking only for the global name would flag the fixed code.
                # Any eviction call in a module that also declares a max-entry
                # constant is the honest signal.
                evicts = bool(
                    re.search(rf"{re.escape(name)}\s*\.\s*(?:pop|popitem|clear)\s*\(", source)
                    or re.search(rf"len\(\s*{re.escape(name)}\s*\)", source)
                    or (has_bound_constant and _EVICTION_RE.search(source))
                )
                if not (evicts and has_bound_constant):
                    violations.append(Violation(
                        "module_level_caches_are_bounded",
                        str(path.relative_to(src_root.parent.parent)),
                        f"{name} is a module-level cache with no explicit "
                        "max-entry bound and eviction, and is not declared "
                        "bounded-by-construction in BOUNDED_BY_CONSTRUCTION",
                        getattr(node, "lineno", 0),
                    ))
                if len(violations) >= MAX_VIOLATIONS_PER_INVARIANT:
                    return violations
    return violations


# --------------------------------------------------------------------------- #
# sqlite connections close
# --------------------------------------------------------------------------- #

_SQLITE_WITH_RE = re.compile(
    r"^[ \t]*(?:async[ \t]+)?with[ \t]+([^\n:]*)", re.MULTILINE
)
_SQLITE_ACQUIRE_RE = re.compile(r"\bsqlite3\s*\.\s*connect\s*\(|\b_connect\s*\(")
_CLOSING_RE = re.compile(r"\bclosing\s*\(")


def sqlite_context_managers_close(src_root: Path) -> list[Violation]:
    """`with <connect>(...)` must be wrapped in contextlib.closing.

    ``Connection.__exit__`` commits or rolls back the transaction; it never
    closes. A block that reads as scoping a connection does not.
    """

    violations: list[Violation] = []
    for path in _python_sources(src_root):
        source = _read(path)
        if not source:
            continue
        for index, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if not (stripped.startswith("with ") or stripped.startswith("async with ")):
                continue
            if not _SQLITE_ACQUIRE_RE.search(stripped):
                continue
            if _CLOSING_RE.search(stripped):
                continue
            violations.append(Violation(
                "sqlite_context_managers_close",
                str(path.relative_to(src_root.parent.parent)),
                "sqlite3.Connection.__exit__ commits but does not close; wrap "
                "the acquisition in contextlib.closing",
                index,
            ))
            if len(violations) >= MAX_VIOLATIONS_PER_INVARIANT:
                return violations
    return violations


# --------------------------------------------------------------------------- #
# the learning duty is discharged
# --------------------------------------------------------------------------- #

# How many decisions in a row may record no lesson before the omission is a
# violation rather than a number.
#
# Derived from this repository's own record, not chosen. Reading the decided
# cards newest first on 2026-09-07, the runs that recorded no lesson are
# 6, 15, 1, 2, 14, 6 and 38. The two short runs are what a deliberate skip looks
# like -- a card that taught nothing -- and every other run is six or longer,
# which is what walking away looks like. Two is the largest limit that accuses
# none of the observed deliberate skips, so the third consecutive omission is
# the first one this can call a defect.
#
# Reachable by construction: only the head run counts, so filing one lesson for
# one of the newest decisions resets it to zero. There is no state in which a
# manager doing the right thing now cannot clear it -- which is the difference
# between a duty that costs something and an acceptance gate that wedges. If a
# repository ever reaches a state where none of the head-run cards can still be
# committed against, raising this constant is one reviewable edit; silently
# skipping the duty is not.
MAX_DECISIONS_WITHOUT_A_LESSON = 2


def recent_decisions_record_a_lesson(repo_root: Path) -> list[Violation]:
    """A run of decisions that recorded no lesson is a defect, not a statistic.

    ``accept_review`` and ``reject_review`` already hand the manager
    ``learning_commit_owed`` -- the repo area, the evidence id in the form the
    store accepts, the exact tool to call -- at the one moment the lesson is
    cheap to write. Nothing followed when it was skipped, and the record shows
    what "nothing follows" produces: 48 lessons arriving in bursts separated by
    runs of 6, 14, 15 and 38 decisions with none, and 21.9 percent coverage that
    moved 0.2 points across a full day of work. The duty was named, measured,
    and enforced nowhere.

    Measures the head run rather than the percentage on purpose. A percentage
    cannot be moved by the decision in hand, so it can never say "this one";
    the head run can, and one lesson clears it.

    Reads only. It never writes and never composes a lesson: authorship is the
    manager's judgement, and a lesson invented to clear a gate is worth less
    than an honest gap. This makes the omission cost what every other defect in
    this repository costs -- a red gate -- and nothing more.
    """

    # Local import: this module must stay importable on its own, and this is
    # the one invariant that reaches a canonical store rather than source text.
    from . import learning_commit_store

    root = Path(repo_root)
    measured = learning_commit_store.coverage(root)
    if not int(measured["decided_cards"]):
        # An absent denominator is not zero coverage. A repository that has
        # decided nothing owes nothing, and a gate that fires on day one of
        # every repository is a gate people learn to route around.
        return []
    streak = int(measured["consecutive_recent_without_lesson"])
    if streak <= MAX_DECISIONS_WITHOUT_A_LESSON:
        return []
    clearing = measured["recent_without_lesson"][: MAX_DECISIONS_WITHOUT_A_LESSON + 1]
    return [Violation(
        "recent_decisions_record_a_lesson",
        str(root),
        f"the {streak} most recently decided cards recorded no lesson "
        f"(limit {MAX_DECISIONS_WITHOUT_A_LESSON}; "
        f"{measured['window_days']}-day coverage "
        f"{measured['coverage_percent']}%). Clear it by committing the lesson "
        f"one of these already owes, through aiworkhub_manager_learning_commit "
        f"with the learning_commit_owed payload its decision returned: "
        + ", ".join(clearing),
    )]


def _canonical_store_reason(repo_root: Path) -> str:
    """Empty when this root owns a canonical task store, else why it does not.

    A worker's validation worktree holds ``src/``, ``tests/`` and no
    ``.aiworkhub`` at all, so "there is no repository here" is genuinely not
    applicable and must not fail a card for a duty that root never owed. Any
    other failure -- a root that IS an AIWorkHub repository whose store cannot
    be read -- is deliberately not caught here, so it reaches ``check`` as an
    unevaluable violation: "could not check" and "checked and clean" must never
    look the same.
    """

    from . import task_store

    try:
        task_store.inspect_repository(repo_root)
    except task_store.RepositoryStateError as exc:
        return f"not_an_aiworkhub_repository:{type(exc).__name__}"
    return ""


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

_TREE_INVARIANTS: tuple[tuple[str, Callable[[Path], list[Violation]]], ...] = (
    ("module_level_caches_are_bounded", module_level_caches_are_bounded),
    ("sqlite_context_managers_close", sqlite_context_managers_close),
)

_RUNTIME_INVARIANTS: tuple[tuple[str, Callable[[], list[Violation]]], ...] = (
    ("terminal_vocabulary_has_one_owner", terminal_vocabulary_has_one_owner),
    ("one_policy_one_predicate", one_policy_one_predicate),
)

# Invariants about what the repository DID, not about what its source says.
# Separate from the tree family because they need the canonical stores, which
# the sparse worktree a worker validates in does not contain.
_REPOSITORY_INVARIANTS: tuple[tuple[str, Callable[[Path], list[Violation]]], ...] = (
    ("recent_decisions_record_a_lesson", recent_decisions_record_a_lesson),
)

# Invariants that need the tree AND the declared ratchet baseline from the
# manifest. Separate again because a missing manifest must make them report
# themselves unevaluated rather than clean.
_RATCHET_INVARIANTS: tuple[
    tuple[str, Callable[[dict[str, dict[str, int]], dict[str, dict[str, int]]], list[Violation]]], ...
] = (
    ("copied_helpers_have_one_definition", copied_helpers_have_one_definition),
    ("parallel_implementations_have_one_owner", parallel_implementations_have_one_owner),
)

INVARIANT_NAMES: tuple[str, ...] = tuple(
    sorted(
        name
        for name, _ in (
            *_TREE_INVARIANTS,
            *_RUNTIME_INVARIANTS,
            *_RATCHET_INVARIANTS,
            *_REPOSITORY_INVARIANTS,
        )
    )
)


# --------------------------------------------------------------------------- #
# what the manifest declares, and what actually executes
# --------------------------------------------------------------------------- #
#
# ``development_rules.json`` declares 20 rules. This module executes seven
# detectors. Before RM-2026-00048 the two facts never met: ``check`` hardcoded
# its own list, named the manifest only in a comment, and returned
# ``unevaluated: []`` with ``passed: true`` -- so a report that covered a
# fraction of the declared rules was indistinguishable from one that covered
# them all. A gate trusted for more than it checks is worse than no gate.
#
# The unit of coverage is not the rule but the forbidden token, because one rule
# forbids several distinct things: ``single_definition`` alone forbids four, and
# a detector for one of them says nothing about the other three.
#
# This map is the declaration. It is explicit and checked, never inferred from
# name similarity: ``rule_detector_coverage`` verifies every rule id here exists
# in the manifest, every token is one that rule actually forbids, and every
# detector named is one this module actually runs.
RULE_DETECTORS: dict[str, dict[str, str]] = {
    "single_definition": {
        "restated_vocabulary": "terminal_vocabulary_has_one_owner",
        "two_predicates_one_policy": "one_policy_one_predicate",
        "copied_helper": "copied_helpers_have_one_definition",
        "parallel_implementation": "parallel_implementations_have_one_owner",
    },
    "cache_discipline": {
        "cache_without_bound": "module_level_caches_are_bounded",
    },
}

# Forbidden tokens this repository has decided, in writing, that it does not yet
# detect -- each with the reason, so an unchecked rule is an acknowledged debt
# rather than an oversight. The shape is deliberate: a token that is neither
# detected nor listed here is a hard failure, so adding a rule to the manifest
# breaks this gate until someone either writes the detector or writes the reason.
# This is the same construction as ``dependency_autolaunch.unclassified_denial_reasons``.
# Why a declared token does not execute. Five reasons, none of them "we forgot":
_NOT_A_SOURCE_PROPERTY = (
    "names a property of a run -- a process that outlived its parent, a release "
    "cut on red CI, two cards writing one file -- and not a pattern in source "
    "text; reading the tree cannot decide it"
)
_NEEDS_MEASUREMENT = (
    "is a claim about measured behaviour, not about what the source says; "
    "deciding it needs a profile or a hit-rate, and a static reading that "
    "guessed would be a detector this repository could not trust"
)
_NOT_THIS_LANGUAGE = (
    "the rule declares languages c, cpp, cuda and rust; this package is Python, "
    "so there is no code here for the rule to govern"
)
_PROCESS_DISCIPLINE = (
    "governs how a manager or worker conducts a task, not what the resulting "
    "code says; the evidence lives in receipts and the audit ledger"
)
_ANOTHER_GATE = (
    "partly enforced outside this module by the OS-dependency ratchet in "
    "scripts/check_os_dependency_boundary.py, which counts eight exact "
    "constructs -- os.name ==, os.name !=, sys.platform, creationflags, "
    "os.killpg, def chmod_fd, def _atomic*, sqlite3.connect -- outside the "
    "platform_io facade and refuses growth above a baseline. It does NOT see a "
    "POSIX assumption written any other way, and on 2026-09-07 three Windows "
    "blockers passed it: hasattr(os, \"O_DIRECTORY\") degrading a mask instead "
    "of refusing, a literal os.O_CLOEXEC, and stat.S_IMODE(...) & 0o077 as a "
    "privacy test on a host with no POSIX mode bits. Treat this as partial "
    "coverage of a narrow vocabulary, not as the obligation being discharged"
)
_NO_DETECTOR_YET = (
    "is a static pattern this repository could read from the tree and has not "
    "written a detector for; this is real debt, not an exemption"
)

ACCEPTED_UNDETECTED: dict[str, str] = {
    "cache_discipline:authority_cached_with_result": _NO_DETECTOR_YET,
    "cache_discipline:stale_generation_retention": _NO_DETECTOR_YET,
    "cache_discipline:unmeasured_cache": _NEEDS_MEASUREMENT,
    "coding_baseline:ambient_authority": _NO_DETECTOR_YET,
    "coding_baseline:silent_fallback": _NO_DETECTOR_YET,
    "coding_baseline:unbounded_work": _NEEDS_MEASUREMENT,
    "coding_baseline:unnamed_refusal": _NO_DETECTOR_YET,
    "cross_platform_contract:platform_only_side_effect": _ANOTHER_GATE,
    "cross_platform_contract:posix_only_assumption": _ANOTHER_GATE,
    "cross_platform_contract:shell_string_command": _NO_DETECTOR_YET,
    "cross_platform_contract:untestable_platform_branch": _NO_DETECTOR_YET,
    "cross_platform_contract:windows_path_guess": _NO_DETECTOR_YET,
    "detector_evidence:fixture_only_validation": _PROCESS_DISCIPLINE,
    "fail_closed_default:unknown_maps_to_success": _NO_DETECTOR_YET,
    "fail_closed_default:unreadable_input_disables_check": _NO_DETECTOR_YET,
    "fail_closed_default:vacuous_pass_on_empty_evidence": _NO_DETECTOR_YET,
    "graph_cpu_parallelism:hardcoded_worker_count": _NO_DETECTOR_YET,
    "graph_cpu_parallelism:shared_mutable_partition_state": _NO_DETECTOR_YET,
    "graph_cpu_parallelism:single_core_full_rebuild": _NEEDS_MEASUREMENT,
    "hot_path_allocation:allocation_in_hot_loop": _NEEDS_MEASUREMENT,
    "hot_path_allocation:full_copy_in_hot_loop": _NEEDS_MEASUREMENT,
    "hot_path_allocation:materialize_unbounded_collection": _NEEDS_MEASUREMENT,
    "hot_path_allocation:repeated_decode_in_hot_loop": _NEEDS_MEASUREMENT,
    "incremental_state:copy_full_canonical_graph": _NO_DETECTOR_YET,
    "incremental_state:full_rescan_without_change": _NEEDS_MEASUREMENT,
    "incremental_state:recompute_unchanged_storage": _NEEDS_MEASUREMENT,
    "incremental_state:write_in_place_generation": _NO_DETECTOR_YET,
    "mechanical_work_off_model:model_recomputes_recorded_evidence": _PROCESS_DISCIPLINE,
    "mechanical_work_off_model:model_retypes_relocated_text": _PROCESS_DISCIPLINE,
    "model_execution_policy:elapsed_time_model_kill": _NO_DETECTOR_YET,
    "model_execution_policy:implicit_output_budget": _NO_DETECTOR_YET,
    "model_execution_policy:implicit_token_budget": _NO_DETECTOR_YET,
    "model_execution_policy:silent_provider_fallback": _NO_DETECTOR_YET,
    "native_ownership:manual_lifetime_pair": _NOT_THIS_LANGUAGE,
    "native_ownership:owning_raw_pointer": _NOT_THIS_LANGUAGE,
    "native_ownership:unbounded_stack_buffer": _NOT_THIS_LANGUAGE,
    "parallelism_fallback:hardcoded_worker_count": _NO_DETECTOR_YET,
    "parallelism_fallback:unmeasured_sequential_hot_path": _NEEDS_MEASUREMENT,
    "performance_evidence:performance_claim_without_measurement": _PROCESS_DISCIPLINE,
    "release_evidence:release_with_red_ci": _NOT_A_SOURCE_PROPERTY,
    "release_evidence:release_without_artifact_verification": _NOT_A_SOURCE_PROPERTY,
    "runtime_io_parallelism:global_lock_during_io": _NO_DETECTOR_YET,
    "runtime_io_parallelism:serial_independent_io": _NEEDS_MEASUREMENT,
    "runtime_io_parallelism:unbounded_thread_creation": _NO_DETECTOR_YET,
    "self_hosting_break_glass:continued_use_of_known_broken_plugin": _PROCESS_DISCIPLINE,
    "self_hosting_break_glass:scope_expansion_during_break_glass": _PROCESS_DISCIPLINE,
    "self_hosting_break_glass:silent_task_system_bypass": _PROCESS_DISCIPLINE,
    "self_hosting_break_glass:unmeasured_break_glass": _PROCESS_DISCIPLINE,
    "sqlite_concurrency:database_lock_without_owner": _NO_DETECTOR_YET,
    "sqlite_concurrency:long_write_transaction": _NEEDS_MEASUREMENT,
    "sqlite_concurrency:network_db_dependency": _NO_DETECTOR_YET,
    "subprocess_lifecycle:elapsed_time_worker_kill": _NO_DETECTOR_YET,
    "subprocess_lifecycle:orphan_process": _NOT_A_SOURCE_PROPERTY,
    "subprocess_lifecycle:system_temp_leak": _NO_DETECTOR_YET,
    "subprocess_lifecycle:unreleased_lock": _NOT_A_SOURCE_PROPERTY,
    "task_atomicity:overlapping_parallel_writes": _NOT_A_SOURCE_PROPERTY,
    "task_atomicity:review_queue_accumulation": _NOT_A_SOURCE_PROPERTY,
    "task_atomicity:whole_file_regeneration": _NOT_A_SOURCE_PROPERTY,
}

# The invariant that has no manifest rule to answer to. Declared here rather than
# silently ignored, so the mapping is total in both directions.
_DETECTORS_WITHOUT_A_DECLARED_RULE: dict[str, str] = {
    "sqlite_context_managers_close":
        "no manifest rule forbids relying on Connection.__exit__ to close; the "
        "invariant predates the rule set and measured nine real call sites",
    "recent_decisions_record_a_lesson":
        "measures what the repository DID, not what its source says; the "
        "manifest declares rules about code, and this one has no code to name",
}


def _obligations(manifest: Any) -> list[tuple[str, str]]:
    """Return every (rule id, forbidden token) the manifest declares."""

    return sorted(
        (rule.id, token) for rule in manifest.rules for token in rule.forbid
    )


def rule_detector_coverage(manifest: Any) -> dict[str, Any]:
    """Return, for every declared rule, which forbidden tokens execute.

    Raises ``ValueError`` if ``RULE_DETECTORS`` and the manifest disagree, because
    a coverage claim resting on a stale map is exactly the false comfort this
    exists to remove.
    """

    declared = {rule.id: set(rule.forbid) for rule in manifest.rules}
    for rule_id, tokens in RULE_DETECTORS.items():
        if rule_id not in declared:
            raise ValueError(f"RULE_DETECTORS names rule {rule_id!r}, which the manifest does not declare")
        unknown = sorted(set(tokens) - declared[rule_id])
        if unknown:
            raise ValueError(f"RULE_DETECTORS claims tokens {rule_id} does not forbid: {unknown}")
        missing = sorted(set(tokens.values()) - set(INVARIANT_NAMES))
        if missing:
            raise ValueError(f"RULE_DETECTORS names detectors that do not run: {missing}")

    detected: list[dict[str, str]] = []
    accepted: list[dict[str, str]] = []
    undetected: list[dict[str, str]] = []
    for rule_id, token in _obligations(manifest):
        detector = RULE_DETECTORS.get(rule_id, {}).get(token)
        if detector is not None:
            detected.append({"rule": rule_id, "forbids": token, "detector": detector})
        elif f"{rule_id}:{token}" in ACCEPTED_UNDETECTED:
            accepted.append({
                "rule": rule_id, "forbids": token,
                "reason": ACCEPTED_UNDETECTED[f"{rule_id}:{token}"],
            })
        else:
            undetected.append({
                "rule": rule_id, "forbids": token,
                "reason": "no executable detector and no declared reason for its absence",
            })

    covered = {row["rule"] for row in detected}
    uncovered = {row["rule"] for row in (*accepted, *undetected)}
    return {
        "declared_rules": len(declared),
        "obligations": len(detected) + len(accepted) + len(undetected),
        "detected": detected,
        "accepted_undetected": accepted,
        "undetected": undetected,
        "rules_fully_covered": sorted(covered - uncovered),
        "rules_partly_covered": sorted(covered & uncovered),
        "rules_not_covered": sorted(uncovered - covered),
        "detectors_without_a_declared_rule": sorted(_DETECTORS_WITHOUT_A_DECLARED_RULE),
    }


def undetected_obligations(manifest: Any) -> list[str]:
    """Return declared obligations that neither execute nor carry a written reason.

    Empty by construction today. A rule added to the manifest lands here the
    moment it is added, which is the point: silence about a new rule is a
    failure, not a pass.
    """

    return [f"{row['rule']}:{row['forbids']}" for row in rule_detector_coverage(manifest)["undetected"]]


def _manifest_path(src_root: Path, repo_root: Path | None) -> Path:
    root = repo_root if repo_root is not None else src_root.parent.parent
    return root / ".aiworkhub" / "config" / "development_rules.json"


def load_manifest(src_root: Path, repo_root: Path | None) -> tuple[Any, str, str]:
    """Return ``(manifest, reason, status)`` for the repository's rules manifest.

    ``status`` separates three things that must not be confused. ``absent``
    means this tree is not a repository that declares rules -- the sparse
    worktree a worker validates in, or a fixture -- and the coverage question
    does not apply, exactly as a repository invariant with no canonical store
    does not apply. ``unreadable`` means a repository DOES declare rules and
    they cannot be read, which is a defect and fails closed. ``ok`` means they
    were read.

    ``absent`` is a positive claim about a tree, so it may only be made about a
    tree that was inspected. Two ways it used to be made about one that was not.
    Without an explicit ``repo_root`` the manifest path is DERIVED from
    ``src_root``, so a ``src_root`` that does not exist derived a path that
    means nothing and the missing file there was reported as "this repository
    declares no rules". And ``Path.is_file()`` answers False for a file it was
    not permitted to stat, so a manifest behind a closed directory read as a
    manifest that was never written. Both are now ``unreadable``.
    """

    if repo_root is None and not src_root.is_dir():
        return (
            None,
            f"manifest location is derived from a source root that is not a "
            f"readable directory: {src_root}",
            "unreadable",
        )
    path = _manifest_path(src_root, repo_root)
    try:
        present = path.is_file()
        if not present:
            # ``is_file()`` swallows the OSError and answers False for both
            # "not there" and "not permitted to look", so ask again in a form
            # that raises and let the error say which it was.
            path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None, f"no development rules manifest at {path.as_posix()}", "absent"
    except OSError as exc:
        return (
            None,
            f"manifest at {path.as_posix()} could not be inspected: {type(exc).__name__}",
            "unreadable",
        )
    if not present:
        return None, f"no development rules manifest at {path.as_posix()}", "absent"
    try:
        from .development_rules import parse_manifest_bytes

        return parse_manifest_bytes(path.read_bytes()), "", "ok"
    except Exception as exc:  # noqa: BLE001 - a declared but unreadable manifest blocks
        return None, f"manifest could not be parsed: {type(exc).__name__}", "unreadable"


def _unevaluable(name: str, root: Path, exc: Exception) -> Violation:
    """Describe an inspection failure without allowing unbounded exception text."""

    affected = getattr(exc, "filename", None) or root
    try:
        message = " ".join(str(exc).split())[:200]
    except Exception:  # noqa: BLE001 - diagnostic formatting must also fail closed
        message = ""
    detail = f"invariant could not be evaluated: {type(exc).__name__}"
    if message:
        detail += f": {message}"
    return Violation(name, str(affected), detail)


def check(
    src_root: Path | str, *, repo_root: Path | str | None = None
) -> dict[str, Any]:
    """Return every declared invariant's verdict over ``src_root``.

    Never raises for a repository-shaped problem: an invariant that cannot be
    evaluated reports itself as a violation, because "could not check" and
    "checked and clean" must never look the same.

    Four states, deliberately not three. An invariant row carries
    ``evaluated: true`` only when its predicate actually ran; ``unevaluable:
    true`` with the reason when it ran and raised, which is still a violation so
    the verdict fails closed; ``evaluated: false`` with a reason when there was
    nothing to run it against, which is not a violation because nothing is
    broken -- but it is never counted as clean either. ``source_sample`` says in
    one place what the tree detectors had to read: ``readable`` false for a root
    that could not be inspected, and ``modules: 0`` for one that holds nothing to
    inspect. A detector that read zero files has not found a clean tree.

    Three numbers, and they mean different things. ``violation_count`` counts
    breaches and blind spots together, because both must fail; ``unevaluable_count``
    is how many of those were blind spots. ``passed`` says no detector that ran
    found a breach. ``all_declared_obligations_checked`` says whether everything
    ``development_rules.json`` declares was both covered by a detector and
    actually executed -- and on this repository it is False, because 5 of 63
    declared obligations execute. Before RM-2026-00048 only ``passed`` existed
    and it read as the second claim while meaning the first: 15 of 20 declared
    rules were unchecked and the report said ``unevaluated: []``. Every
    obligation that does not execute is now listed in ``unevaluated`` by name,
    with the reason.

    ``repo_root`` is the repository whose canonical stores the repository
    invariants measure, and is separate from ``src_root`` deliberately. The
    tree invariants read source text and run anywhere, including the sparse
    worktree a worker validates in; a repository invariant needs a canonical
    store that such a worktree does not contain. A repository invariant with
    nothing to measure reports ``evaluated: false`` with the reason and is
    listed in ``unevaluated`` -- it is never folded into the clean count.
    """

    root = Path(src_root)
    repo = Path(repo_root) if repo_root is not None else None
    results: list[dict[str, Any]] = []
    violations: list[Violation] = []
    unevaluated: list[dict[str, str]] = []

    def evaluated(name: str, found: list[Violation], **extra: Any) -> None:
        violations.extend(found)
        results.append(
            {"invariant": name, "violations": len(found), "evaluated": True, **extra}
        )

    def unevaluable(name: str, where: Path, exc: Exception) -> None:
        """Record an invariant whose predicate could not run.

        Fails closed -- it is still a violation, so ``passed`` is False and the
        CLI exit code is unchanged -- but it is no longer recorded as
        ``evaluated: true``, which was a plain untruth about an invariant that
        never ran. A reader can tell a breach from a blind spot from the row
        itself, and count the blind spots with ``unevaluable_count``.

        Deliberately NOT added to ``unevaluated``. That list is the accepted
        ones -- an obligation no detector covers, or a detector with nothing to
        measure -- and a detector that raised is not accepted, it is broken. The
        two must not share a channel or a reader will discharge the second by
        reading the first.
        """

        violation = _unevaluable(name, where, exc)
        violations.append(violation)
        results.append({
            "invariant": name,
            "violations": 1,
            "evaluated": False,
            "unevaluable": True,
            "reason": violation.detail,
        })

    def no_sample(name: str, reason: str) -> None:
        """Record an invariant whose predicate had nothing to run against."""

        results.append({
            "invariant": name, "violations": 0, "evaluated": False, "reason": reason,
        })
        unevaluated.append({"invariant": name, "reason": reason})

    # One inspection of the tree, before any detector runs, so that the report
    # can distinguish three states a single "0 violations" used to flatten: the
    # root could not be read at all, the root is readable but holds no module to
    # read, and the detectors read N modules and found nothing. A tree detector
    # that scanned zero files has not found a clean tree; it has not looked.
    source_error: Exception | None = None
    modules = 0
    try:
        modules = len(_python_sources(root))
    except Exception as exc:  # noqa: BLE001 - an unreadable root is not a clean root
        source_error = exc
    no_modules = "" if source_error is not None or modules else (
        f"source root holds no python module to check: {root}"
    )

    for name, tree_check in _TREE_INVARIANTS:
        if source_error is not None:
            unevaluable(name, root, source_error)
            continue
        if no_modules:
            no_sample(name, no_modules)
            continue
        try:
            found = tree_check(root)
        except Exception as exc:  # noqa: BLE001 - unevaluable is a violation
            unevaluable(name, root, exc)
            continue
        evaluated(name, found, measurement={"modules_scanned": modules})
    for name, runtime_check in _RUNTIME_INVARIANTS:
        try:
            found = runtime_check()
        except Exception as exc:  # noqa: BLE001 - unevaluable is a violation
            unevaluable(name, root, exc)
            continue
        evaluated(name, found)
    for name, repository_check in _REPOSITORY_INVARIANTS:
        try:
            reason = (
                "no_repository_root_supplied"
                if repo is None
                else _canonical_store_reason(repo)
            )
            if reason:
                no_sample(name, reason)
                continue
            found = repository_check(repo)
        except Exception as exc:  # noqa: BLE001 - unevaluable is a violation
            unevaluable(name, repo if repo is not None else root, exc)
            continue
        evaluated(name, found)

    # The manifest is what the repository DECLARES; everything above is what it
    # EXECUTES. Reading it here is the whole point of RM-2026-00048: a report
    # that never opens the manifest cannot know what it is failing to check.
    manifest, manifest_reason, manifest_status = load_manifest(root, repo)
    if manifest is None:
        coverage: dict[str, Any] = {
            "status": "not_applicable" if manifest_status == "absent" else "unavailable",
            "reason": manifest_reason,
        }
        for name, _ in _RATCHET_INVARIANTS:
            no_sample(name, manifest_reason)
    else:
        try:
            coverage = {"status": "evaluated", "reason": "", **rule_detector_coverage(manifest)}
        except Exception as exc:  # noqa: BLE001 - a stale map must block, never pass
            coverage = {
                "status": "unavailable",
                "reason": f"detector map disagrees with the manifest: {type(exc).__name__}: {exc}",
            }
        boundary = manifest.single_definition_boundary
        if boundary is None:
            reason = "manifest declares no single_definition_boundary ratchet"
            for name, _ in _RATCHET_INVARIANTS:
                no_sample(name, reason)
        elif source_error is not None or no_modules:
            # The ratchets scan the same tree the tree invariants do. A root that
            # could not be read, or that holds no module, gives them nothing to
            # count -- and a duplicate count of zero over zero definitions is not
            # a tree that matches its baseline.
            for name, _ in _RATCHET_INVARIANTS:
                if source_error is not None:
                    unevaluable(name, root, source_error)
                else:
                    no_sample(name, no_modules)
        else:
            thresholds = {pattern: boundary.threshold(pattern) for pattern in boundary.patterns}
            baseline: dict[str, dict[str, int]] = {}
            for entry in boundary.baseline:
                baseline.setdefault(entry.pattern, {})[entry.path] = entry.count
            try:
                counts = duplicate_definition_counts(root, thresholds)
            except Exception as exc:  # noqa: BLE001 - an unreadable tree is not a clean tree
                counts = None
                for name, _ in _RATCHET_INVARIANTS:
                    unevaluable(name, root, exc)
            if counts is not None:
                for name, ratchet_check in _RATCHET_INVARIANTS:
                    found = ratchet_check(counts, baseline)
                    pattern = (
                        "copied_helper"
                        if name == "copied_helpers_have_one_definition"
                        else "parallel_implementation"
                    )
                    evaluated(name, found, measurement={
                        "pattern": pattern,
                        "modules_scanned": modules,
                        "modules": len(counts[pattern]),
                        "definitions": sum(counts[pattern].values()),
                        "baseline_definitions": sum(baseline.get(pattern, {}).values()),
                    })

    # An obligation that neither executes nor carries a written reason is a hard
    # failure: a rule added to the manifest with no detector must break this, not
    # widen the silence it was added to end.
    unclassified = list(coverage.get("undetected", []))
    for row in unclassified:
        violations.append(Violation(
            "declared_rules_are_all_classified",
            ".aiworkhub/config/development_rules.json",
            f"rule {row['rule']} forbids {row['forbids']}, which no detector "
            "executes and ACCEPTED_UNDETECTED does not explain",
        ))
    # Every obligation this repository does not check is named in the report with
    # its reason, so `passed` can never be read as "all declared rules hold".
    for row in coverage.get("accepted_undetected", []):
        unevaluated.append({
            "invariant": f"{row['rule']}:{row['forbids']}",
            "reason": row["reason"],
        })
    if coverage["status"] == "unavailable":
        # A repository that DECLARES rules and cannot read them is broken; one
        # that declares none is simply not this kind of repository.
        violations.append(Violation(
            "declared_rules_are_all_classified",
            ".aiworkhub/config/development_rules.json",
            f"declared-rule coverage could not be determined: {coverage['reason']}",
        ))

    # The count of invariants that did not run because they could not, as
    # opposed to those that ran and found nothing. Both leave `passed` False --
    # unevaluable fails closed, as RM-2026-00048 requires -- but they are not the
    # same fact and a reader must not have to grep violation text to tell them
    # apart.
    unevaluable_count = sum(1 for row in results if row.get("unevaluable"))
    return {
        "schema_id": SCHEMA_ID,
        "src_root": str(root),
        "repo_root": str(repo) if repo is not None else "",
        # What the tree detectors actually had to read. `readable: false` means
        # the root could not be inspected at all; `modules: 0` on a readable root
        # means there was nothing to inspect. Neither is a clean tree, and before
        # this both looked exactly like one.
        "source_sample": {
            "readable": source_error is None,
            "modules": modules,
            "reason": (
                f"source root could not be read: {type(source_error).__name__}"
                if source_error is not None
                else no_modules
            ),
        },
        "invariants": results,
        "unevaluated": unevaluated,
        "rule_coverage": coverage,
        # False whenever any declared obligation does not execute -- because no
        # detector covers it, because a detector that covers it had nothing to
        # measure, or because a detector that covers it raised.
        "all_declared_obligations_checked": (
            coverage["status"] == "evaluated"
            and not coverage["accepted_undetected"]
            and not coverage["undetected"]
            and not unevaluated
            and not unevaluable_count
        ),
        "unevaluable_count": unevaluable_count,
        "violation_count": len(violations),
        "violations": [v.to_dict() for v in violations[:MAX_VIOLATIONS_PER_INVARIANT]],
        # "no detector that ran found a breach", NOT "every declared rule holds"
        # and NOT "every detector ran". Read it with all_declared_obligations_checked
        # and unevaluable_count, never alone.
        "passed": not violations,
    }


def main(argv: Iterable[str] | None = None) -> int:
    import argparse
    import json
    import sys

    # Half of this module's invariants import the package they inspect
    # (``from .task_fsm import ...``), and ``load_manifest`` imports
    # ``development_rules`` the same way. Run as a plain script the package is
    # not on the import path, every one of those raises ImportError, and the
    # report that comes back is a repository with four breaches and unreadable
    # rules -- an unrunnable checker describing itself as a broken tree. Refuse
    # instead, naming the invocation that works. This is the module's own rule
    # applied to its entry point: could-not-run must not be reported as a result.
    if not __package__:
        sys.stderr.write(
            "aiworkhub.declared_invariants must run as a module, not a script: "
            "the invariants import the package they inspect, which a script run "
            "cannot resolve. Use: python -m aiworkhub.declared_invariants\n"
        )
        return 2

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--src", default=str(Path(__file__).resolve().parent),
        help="package root to check (defaults to this package)",
    )
    parser.add_argument(
        "--repo", default=str(Path.cwd()),
        help=(
            "repository whose canonical stores the repository invariants "
            "measure (defaults to the working directory; a root with no "
            "AIWorkHub manifest reports them not evaluated, with the reason)"
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = check(Path(args.src), repo_root=Path(args.repo))
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
