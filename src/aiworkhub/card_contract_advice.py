"""Advisory Source Graph and size-ratchet hints for one task-card plan.

``contract_advice`` reports what a manager cannot see from a card alone and
never rewrites ``allowed_writes`` or ``validation``: the tests Source Graph
``testmap`` relates to the card's production paths that no validation command
names (and which of those the card cannot write), and the ``src/aiworkhub``
files close to the module-size ratchet ceiling.

Every part fails closed. When its evidence is missing, stale or raises, the
part reports ``status: "unavailable"`` plus a short named ``reason`` and no
result lists, so an unavailable check never reads as an all-clear. Nothing
here raises. Keys that only matter when non-empty (``unavailable_paths``,
``not_expanded``, ``truncated_paths``, ``unreadable_paths``, ``omitted_*``)
are left out when empty to keep the preview receipt small.
"""

from __future__ import annotations

import ast
import fnmatch
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

from . import source_graph
from .sdlc_attribution import is_test_path
from .task_templates import split_command_argv

# testmap scores 4 per resolved call edge from a test into the file and 2 for a
# path-stem match alone, so 4 keeps call-edge evidence and drops stem-only noise.
RELATED_TEST_SCORE_FLOOR = 4
RATCHET_HEADROOM_WARN_LINES = 30
RATCHET_TEST_PATH = "tests/test_module_size_ratchet.py"
RATCHET_CEILING_NAME = "MAX_AIWORKHUB_MODULE_LINES"
AIWORKHUB_PACKAGE_PREFIX = "src/aiworkhub/"
TESTMAP_BUDGET = 40
MAX_PRODUCTION_PATHS = 16
MAX_SUGGESTED_TESTS = 10

_FRESHNESS_REASONS = {
    "stale": "index_stale",
    "missing": "file_not_on_disk",
    "unknown": "not_indexed",
}


def contract_advice(repo_root: Path | str, task_plan: Mapping[str, Any]) -> dict[str, Any]:
    """Advisory report for one plan; each part is ok, partial, unavailable or not_applicable."""
    if not isinstance(task_plan, Mapping):
        return {
            "advisory": True,
            "testmap": _unavailable("task_plan_not_a_mapping"),
            "ratchet": _unavailable("task_plan_not_a_mapping"),
        }
    allowed = [
        path for path in map(_normalize_path, _items(task_plan.get("allowed_writes"))) if path
    ]
    commands = [item for item in _items(task_plan.get("validation")) if isinstance(item, str)]
    return {
        "advisory": True,
        "testmap": _guarded("testmap", _testmap_part, repo_root, allowed, commands),
        "ratchet": _guarded("ratchet", _ratchet_part, repo_root, allowed),
    }


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


def _not_applicable(reason: str, unexpanded: Sequence[str]) -> dict[str, Any]:
    part: dict[str, Any] = {"status": "not_applicable", "reason": reason}
    if unexpanded:
        part["not_expanded"] = list(unexpanded)
    return part


def _guarded(part: str, build: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
    try:
        return build(*args)
    except Exception as exc:  # advisory: a failing part reports itself and never raises
        return _unavailable(f"{part}_failed:{type(exc).__name__}")


def _failure_reason(exc: Exception) -> str:
    if isinstance(exc, source_graph.SourceGraphError):
        code = str(exc).split(":", 1)[0].strip()
        return (code or type(exc).__name__)[:64]
    return f"source_graph_unavailable:{type(exc).__name__}"


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _normalize_path(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return "" if ".." in text.split("/") else text


def _has_glob(text: str) -> bool:
    return any(char in text for char in "*?[")


def _covers(entries: Iterable[str], path: str) -> bool:
    """True when ``path`` equals, sits under, or glob-matches one of ``entries``."""
    for entry in entries:
        if path == entry or path.startswith(entry.rstrip("/") + "/"):
            return True
        if _has_glob(entry) and fnmatch.fnmatchcase(path, entry):
            return True
    return False


def _scoped_paths(allowed: Iterable[str], prefix: str) -> tuple[list[str], list[str]]:
    """Plain ``.py`` files under ``prefix``, plus the glob/dir entries that cannot be expanded."""
    files: set[str] = set()
    unexpanded: set[str] = set()
    for entry in allowed:
        if not entry.startswith(prefix):
            continue
        if _has_glob(entry) or entry.endswith("/"):
            unexpanded.add(entry)
        elif entry.endswith(".py"):
            files.add(entry)
    return sorted(files), sorted(unexpanded)


def _command_names(commands: Iterable[str]) -> list[str]:
    """Paths the validation commands name, tokenised exactly as the runner splits them."""
    names: list[str] = []
    for command in commands:
        for token in split_command_argv(command):
            name = _normalize_path(token.strip("\"'").split("::", 1)[0])
            if name:
                names.append(name)
    return names


def _freshness_states(root: Path, paths: Sequence[str]) -> dict[str, str]:
    # Source Graph exposes no public per-file freshness API; this is its own check.
    with closing(source_graph.connect(source_graph.resolve_db_path(root), read_only=True)) as conn:
        return {
            path: str(source_graph._file_freshness_state(conn, root, path)["state"])
            for path in paths
        }


def _related_rows(payload: Any) -> tuple[list[tuple[str, Any]] | None, str]:
    """Related-test rows of a recognised testmap payload, else ``(None, reason)``."""
    if not isinstance(payload, Mapping):
        return None, "testmap_payload_unrecognized"
    if payload.get("scope") == "target_scope_empty":
        return None, "target_scope_empty"
    rows = payload.get("related_tests")
    if not isinstance(rows, list):
        return None, "testmap_payload_unrecognized"
    parsed: list[tuple[str, Any]] = []
    for row in rows:
        path = _normalize_path(row.get("file_path")) if isinstance(row, Mapping) else ""
        score = row.get("score") if isinstance(row, Mapping) else None
        if not path or isinstance(score, bool) or not isinstance(score, (int, float)):
            return None, "testmap_payload_unrecognized"
        parsed.append((path, score))
    return parsed, ""


def _testmap_part(
    repo_root: Path | str, allowed: Sequence[str], commands: Sequence[str]
) -> dict[str, Any]:
    root = Path(repo_root)
    files, unexpanded = _scoped_paths(allowed, "src/")
    production = [path for path in files if not is_test_path(path)]
    if not production:
        return _not_applicable("no_production_python_in_allowed_writes", unexpanded)
    consulted = production[:MAX_PRODUCTION_PATHS]
    try:
        freshness = _freshness_states(root, consulted)
    except Exception as exc:  # unresolved repo, missing or unreadable index, ...
        return _unavailable(_failure_reason(exc))
    unavailable = [
        {"path": path, "reason": _FRESHNESS_REASONS.get(freshness.get(path, ""), "not_indexed")}
        for path in consulted
        if freshness.get(path) != "fresh"
    ]
    queryable = [path for path in consulted if freshness.get(path) == "fresh"]
    named = _command_names(commands)
    best: dict[str, tuple[Any, str]] = {}
    truncated_paths: list[str] = []
    answered = 0
    # Sequential: a thread pool is not adopted until measured faster; <=16 short read-only queries.
    for position, path in enumerate(queryable):
        try:
            payload = source_graph.analytics_query(
                root, "testmap", path, TESTMAP_BUDGET, target=path
            )
        except Exception as exc:
            # The first engine failure ends the pass: a locked index can stall 30 s per query.
            reason = _failure_reason(exc)
            unavailable.extend({"path": rest, "reason": reason} for rest in queryable[position:])
            break
        rows, reason = _related_rows(payload)
        if rows is None:
            unavailable.append({"path": path, "reason": reason})
            continue
        answered += 1
        if payload.get("truncated"):
            truncated_paths.append(path)
        for test, score in rows:
            if score < RELATED_TEST_SCORE_FLOOR or _covers(named, test):
                continue
            if test not in best or score > best[test][0]:
                best[test] = (score, path)
    if not answered:
        return {**_unavailable(unavailable[0]["reason"]), "unavailable_paths": unavailable}
    # Tests the card cannot write come first so the cap never drops the actionable ones.
    ranked = sorted(
        best.items(), key=lambda item: (_covers(allowed, item[0]), -item[1][0], item[0])
    )
    kept = sorted(ranked[:MAX_SUGGESTED_TESTS], key=lambda item: item[0])
    suggested = [
        {"test": test, "score": score, "production_path": path} for test, (score, path) in kept
    ]
    part: dict[str, Any] = {
        "status": "partial" if unavailable else "ok",
        "score_floor": RELATED_TEST_SCORE_FLOOR,
        "production_paths": consulted,
        "suggested_validation_tests": suggested,
        "allowed_writes_missing_tests": [
            entry["test"] for entry in suggested if not _covers(allowed, entry["test"])
        ],
    }
    extras = {
        "reason": "some_paths_unavailable" if unavailable else "",
        "unavailable_paths": unavailable,
        "not_expanded": unexpanded,
        "truncated_paths": truncated_paths,
        "omitted_paths": len(production) - len(consulted),
        "omitted_suggestions": len(best) - len(kept),
    }
    part.update({key: value for key, value in extras.items() if value})
    return part


def _ratchet_ceiling(root: Path) -> tuple[int | None, str]:
    """The ceiling literal from the ratchet test (its single source), else ``(None, reason)``."""
    try:
        tree = ast.parse((root / RATCHET_TEST_PATH).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "ratchet_test_missing"
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"ratchet_test_unreadable:{type(exc).__name__}"
    except (SyntaxError, ValueError):
        return None, "ratchet_test_unparseable"
    ceiling: int | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if (
            any(isinstance(t, ast.Name) and t.id == RATCHET_CEILING_NAME for t in targets)
            and isinstance(value, ast.Constant)
            and type(value.value) is int
        ):
            ceiling = value.value
    if ceiling is None or ceiling <= 0:
        return None, "ratchet_ceiling_not_found"
    return ceiling, ""


def _ratchet_part(repo_root: Path | str, allowed: Sequence[str]) -> dict[str, Any]:
    root = Path(repo_root)
    files, unexpanded = _scoped_paths(allowed, AIWORKHUB_PACKAGE_PREFIX)
    if not files:
        return _not_applicable("no_python_file_under_src_aiworkhub", unexpanded)
    ceiling, reason = _ratchet_ceiling(root)
    if ceiling is None:
        return _unavailable(reason)
    warnings: list[dict[str, Any]] = []
    unreadable: list[str] = []
    for path in files:
        try:
            # The same line count tests/test_module_size_ratchet.py applies.
            lines = len((root / path).read_text(encoding="utf-8").splitlines())
        except FileNotFoundError:
            continue  # a file this card will create has no lines to count yet
        except (OSError, UnicodeDecodeError):
            unreadable.append(path)
            continue
        headroom = ceiling - lines
        if headroom <= RATCHET_HEADROOM_WARN_LINES:
            warnings.append(
                {"path": path, "lines": lines, "ceiling": ceiling, "headroom": headroom}
            )
    part: dict[str, Any] = {
        "status": "partial" if unreadable else "ok",
        "ceiling": ceiling,
        "warn_within_lines": RATCHET_HEADROOM_WARN_LINES,
        "ratchet_headroom_warnings": warnings,
    }
    extras = {
        "reason": "some_files_unreadable" if unreadable else "",
        "unreadable_paths": unreadable,
        "not_expanded": unexpanded,
    }
    part.update({key: value for key, value in extras.items() if value})
    return part
