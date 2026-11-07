"""Node-level neighbour test selection (NF-2026-02102).

The neighbour lane (:mod:`aiworkhub.accept_neighbour_tests`) ranks whole related
test files, so a one-line helper change could pull a 20-minute test file. Here
each changed production path is diffed (canonical repo copy against the
candidate copy), every changed OLD-side line range is mapped to the innermost
indexed function or method containing it, and that symbol's incoming call
edges are walked to the test functions and methods calling it, at most
``MAX_CALL_DEPTH`` hops. The result is pytest node ids
(``tests/x.py::test_fn`` or ``tests/x.py::TestC::test_m``).

Everything unknown fails closed: a module-level change (an old range outside
one function/method, or new lines indented no deeper than its def line), an
unattributed same-name caller, a non-test caller still pending at the depth
limit, a hop-1 caller (fixture or production) with no indexed callers, too many
callers or nodes, a stale index, an index error, an unreadable file, or a node
whose test file the candidate changed each name a fallback reason, and the
caller then keeps today's file-level selection.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import source_graph

MAX_NEIGHBOUR_NODES = 200
# A changed symbol's callers are hop 1; a non-test hop-1 caller's callers hop 2.
MAX_CALL_DEPTH = 2
_CALLABLE_KINDS = ("function", "method")
_ENTITY_SELECT = "SELECT name, qualname, kind, line_start, line_end FROM entities "
_QUALNAME_PARTS = re.compile(r"::|\.")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class NodeSelectionResult:
    """Node ids for one changed production path, or the reason it falls back."""

    nodes: tuple[str, ...] = ()
    fallback: bool = False
    reason: str = ""


def _fallback(reason: str) -> NodeSelectionResult:
    return NodeSelectionResult(fallback=True, reason=reason)


def _read_lines(path: Path) -> list[str] | None:
    # split("\n"), not splitlines(): \x0c, \x85, U+2028 etc. are not line breaks to
    # the tokenizer, so splitting on them would drift from the index's AST lines.
    # read_text already normalises \r\n and \r to \n.
    try:
        return path.read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeDecodeError):
        return None


def unchanged_in_candidate(root: Path, candidate_root: Path, path: str) -> bool:
    """True when ``candidate_root/path`` is a regular file with the canonical bytes."""
    candidate = candidate_root / path
    try:
        return (
            candidate.is_file()
            and not candidate.is_symlink()
            and candidate.read_bytes() == (root / path).read_bytes()
        )
    except OSError:
        return False


def changed_hunks(
    old_lines: Sequence[str], new_lines: Sequence[str]
) -> list[tuple[tuple[int, int], tuple[str, ...], tuple[int, int] | None]]:
    """Every differing hunk: its 1-based inclusive OLD-side range, its NEW-side lines
    and the 1-based inclusive OLD range it removes (``None`` for a pure insertion).

    A pure insertion between old lines i and i+1 maps to ``(i, i + 1)``: the old
    lines on both sides, so an insertion at a function's edge (or at the start
    or end of the file) is not inside one function and falls back.
    """
    matcher = difflib.SequenceMatcher(a=list(old_lines), b=list(new_lines), autojunk=False)
    hunks: list[tuple[tuple[int, int], tuple[str, ...], tuple[int, int] | None]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed = (i1 + 1, i2) if i1 < i2 else None
        span = removed or (i1, i1 + 1)
        hunks.append((span, tuple(new_lines[j1:j2]), removed))
    return hunks


def changed_old_ranges(old_lines: Sequence[str], new_lines: Sequence[str]) -> list[tuple[int, int]]:
    """1-based inclusive OLD-side line ranges that differ (see :func:`changed_hunks`)."""
    return [hunk[0] for hunk in changed_hunks(old_lines, new_lines)]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _leaves_body(
    old_lines: Sequence[str], symbol: Mapping[str, Any], added: Sequence[str]
) -> bool:
    """True when a new-side code line is indented no deeper than ``symbol``'s def line.

    Such a line (a new top-level statement, def, class or decorator, or a new
    method at class level) is outside the function the OLD range was charged to.
    Blank lines and pure comments carry no code and are ignored.
    """
    start = int(symbol.get("line_start") or 0)
    if not 0 < start <= len(old_lines):
        return True
    limit = _indent(old_lines[start - 1])
    return any(
        _indent(line) <= limit
        for line in added
        if line.strip() and not line.lstrip().startswith("#")
    )


def _rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, tuple(params))
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _file_symbols(conn: sqlite3.Connection, path: str) -> list[dict[str, Any]]:
    return _rows(conn, _ENTITY_SELECT + "WHERE file_path=? ORDER BY line_start, qualname", (path,))


def _entity(conn: sqlite3.Connection, path: str, qualname: str) -> dict[str, Any] | None:
    rows = _rows(conn, _ENTITY_SELECT + "WHERE file_path=? AND qualname=?", (path, qualname))
    return rows[0] if rows else None


def innermost_callable(
    symbols: Sequence[Mapping[str, Any]], start: int, end: int
) -> Mapping[str, Any] | None:
    """The smallest function/method whose ``[line_start, line_end]`` holds ``[start, end]``."""
    best: Mapping[str, Any] | None = None
    for symbol in symbols:
        if symbol.get("kind") not in _CALLABLE_KINDS:
            continue
        low, high = int(symbol.get("line_start") or 0), int(symbol.get("line_end") or 0)
        if not (0 < low <= start and end <= high):
            continue
        if best is None or high - low < int(best["line_end"]) - int(best["line_start"]):
            best = symbol
    return best


def _local_parts(path: str, qualname: str) -> list[str]:
    """``qualname`` without its file prefix, split into its nested names."""
    for prefix in (f"{path}::", f"{path}."):
        if qualname.startswith(prefix):
            qualname = qualname[len(prefix):]
            break
    return _QUALNAME_PARTS.split(qualname)


def node_id_for(path: str, symbol: Mapping[str, Any]) -> str | None:
    """The pytest node id of a test function or Test* class method, else ``None``."""
    if not source_graph._is_test_source_path(path):
        return None
    parts = _local_parts(path, str(symbol.get("qualname") or ""))
    if not all(_IDENTIFIER.fullmatch(part) for part in parts):
        return None
    kind, name = symbol.get("kind"), str(symbol.get("name") or "")
    if not name.startswith("test") or parts[-1] != name:
        return None
    if kind == "function" and len(parts) == 1:
        return f"{path}::{name}"
    if kind == "method" and len(parts) == 2 and parts[0].startswith("Test"):
        return f"{path}::{parts[0]}::{name}"
    return None


def _symbol_nodes(
    conn: sqlite3.Connection, path: str, symbol: Mapping[str, Any]
) -> tuple[set[str], str]:
    """Test nodes calling ``symbol`` within ``MAX_CALL_DEPTH`` hops, or a fallback reason."""
    nodes: set[str] = set()
    seen = {(path, str(symbol.get("qualname") or ""))}
    pending: list[tuple[str, Mapping[str, Any], int]] = [(path, symbol, 0)]
    while pending:
        current_path, current, depth = pending.pop(0)
        _outgoing, incoming, unattributed = source_graph._calls_edges_and_unattributed_count(
            conn, dict(current), limit=MAX_NEIGHBOUR_NODES + 1
        )
        if unattributed > 0:
            return set(), "unattributed"
        if len(incoming) > MAX_NEIGHBOUR_NODES:
            return set(), "too_many_callers"
        if not incoming and depth > 0:
            # A fixture (pytest injects it) or a production caller reached only
            # dynamically (registered handler, dispatch): its tests are unknown.
            if source_graph._is_test_source_path(current_path):
                return set(), "test_helper_without_callers"
            return set(), "caller_without_callers"
        for edge in incoming:
            caller_path = str(edge.get("caller_file") or "")
            caller = _entity(conn, caller_path, str(edge.get("caller_symbol") or ""))
            if caller is None or caller.get("kind") not in _CALLABLE_KINDS:
                return set(), "caller_not_a_function"
            node = node_id_for(caller_path, caller)
            if node is not None:
                nodes.add(node)
                continue
            key = (caller_path, str(caller["qualname"]))
            if key in seen:
                continue
            seen.add(key)
            if depth + 1 >= MAX_CALL_DEPTH:
                return set(), "depth_exceeded"
            pending.append((caller_path, caller, depth + 1))
        if len(nodes) > MAX_NEIGHBOUR_NODES:
            return set(), "too_many_nodes"
    return nodes, "" if nodes else "no_test_callers"


def select_nodes_for_path(
    conn: sqlite3.Connection, production_path: str, repo_root: Path, candidate_root: Path
) -> NodeSelectionResult:
    """Node ids for one changed production path; never raises, falls back instead."""
    old_lines = _read_lines(repo_root / production_path)
    if old_lines is None:
        return _fallback("canonical_unreadable")
    new_lines = _read_lines(candidate_root / production_path)
    if new_lines is None:
        return _fallback("candidate_unreadable")
    hunks = changed_hunks(old_lines, new_lines)
    if not hunks:
        return _fallback("unchanged_content")
    try:
        symbols = _file_symbols(conn, production_path)
        collected: set[str] = set()
        for (start, end), added, removed in hunks:
            symbol = innermost_callable(symbols, start, end)
            if symbol is None:
                return _fallback("module_level")
            # A removed or rewritten def line: the indexed symbol no longer exists as such.
            if removed is not None and removed[0] <= int(symbol.get("line_start") or 0) <= removed[1]:
                return _fallback("def_line_changed")
            if _leaves_body(old_lines, symbol, added):
                return _fallback("module_level")
            nodes, reason = _symbol_nodes(conn, production_path, symbol)
            if reason:
                return _fallback(reason)
            collected |= nodes
            if len(collected) > MAX_NEIGHBOUR_NODES:
                return _fallback("too_many_nodes")
    except Exception as exc:  # a locked, missing or malformed index: unknown is never fewer
        return _fallback(f"index_error:{type(exc).__name__}")
    return NodeSelectionResult(nodes=tuple(sorted(collected)))


def _connect(root: Path) -> sqlite3.Connection:
    return source_graph.connect(source_graph.resolve_db_path(root), read_only=True)


def select_nodes(
    repo_root: Path,
    candidate_root: Path,
    production_paths: Sequence[str],
    freshness: Mapping[str, str],
    *,
    connect: Callable[[Path], sqlite3.Connection] | None = None,
) -> tuple[tuple[str, ...], tuple[dict[str, str], ...]]:
    """Sorted node ids for ``production_paths`` and the per-path fallback reasons.

    Any fallback reason means the node ids must not replace the file-level
    selection. Never raises.
    """
    reasons: list[dict[str, str]] = []
    fresh = [path for path in production_paths if freshness.get(path) == "fresh"]
    reasons.extend(
        {"path": path, "reason": "stale"} for path in production_paths if path not in fresh
    )
    nodes: set[str] = set()
    try:
        with closing((connect or _connect)(repo_root)) as conn:
            # Sequential: per path this is a diff plus a few indexed SQLite lookups on
            # one read-only connection; a pool would only add connection setup.
            for path in fresh:
                result = select_nodes_for_path(conn, path, repo_root, candidate_root)
                if result.fallback:
                    reasons.append({"path": path, "reason": result.reason})
                nodes.update(result.nodes)
    except Exception as exc:  # connect/resolve failure: every remaining path falls back
        done = {row["path"] for row in reasons}
        reason = f"index_error:{type(exc).__name__}"
        reasons.extend({"path": path, "reason": reason} for path in fresh if path not in done)
    if len(nodes) > MAX_NEIGHBOUR_NODES:
        reasons.append({"path": "", "reason": "too_many_nodes"})
    return tuple(sorted(nodes)), tuple(sorted(reasons, key=lambda row: (row["path"], row["reason"])))
