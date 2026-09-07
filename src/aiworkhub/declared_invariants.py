"""Invariants this repository declares about itself, checked mechanically.

RM-2026-00048. ``development_rules.json`` says what must never be true in prose;
a rule nobody executes is a comment. This module states the same things as
predicates over the tree and returns violations, so a claim like "one concept has
one definition" is a gate rather than an aspiration.

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
import re
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

INVARIANT_NAMES: tuple[str, ...] = tuple(
    sorted(
        name
        for name, _ in (*_TREE_INVARIANTS, *_RUNTIME_INVARIANTS, *_REPOSITORY_INVARIANTS)
    )
)


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

    def evaluated(name: str, found: list[Violation]) -> None:
        violations.extend(found)
        results.append(
            {"invariant": name, "violations": len(found), "evaluated": True}
        )

    for name, tree_check in _TREE_INVARIANTS:
        try:
            found = tree_check(root)
        except Exception as exc:  # noqa: BLE001 - unevaluable is a violation
            found = [_unevaluable(name, root, exc)]
        evaluated(name, found)
    for name, runtime_check in _RUNTIME_INVARIANTS:
        try:
            found = runtime_check()
        except Exception as exc:  # noqa: BLE001 - unevaluable is a violation
            found = [_unevaluable(name, root, exc)]
        evaluated(name, found)
    for name, repository_check in _REPOSITORY_INVARIANTS:
        try:
            reason = (
                "no_repository_root_supplied"
                if repo is None
                else _canonical_store_reason(repo)
            )
            if reason:
                results.append(
                    {
                        "invariant": name,
                        "violations": 0,
                        "evaluated": False,
                        "reason": reason,
                    }
                )
                unevaluated.append({"invariant": name, "reason": reason})
                continue
            found = repository_check(repo)
        except Exception as exc:  # noqa: BLE001 - unevaluable is a violation
            found = [_unevaluable(name, repo if repo is not None else root, exc)]
        evaluated(name, found)
    return {
        "schema_id": SCHEMA_ID,
        "src_root": str(root),
        "repo_root": str(repo) if repo is not None else "",
        "invariants": results,
        "unevaluated": unevaluated,
        "violation_count": len(violations),
        "violations": [v.to_dict() for v in violations[:MAX_VIOLATIONS_PER_INVARIANT]],
        "passed": not violations,
    }


def main(argv: Iterable[str] | None = None) -> int:
    import argparse
    import json
    import sys

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
