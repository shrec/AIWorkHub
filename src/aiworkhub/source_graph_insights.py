"""Bounded, repository-neutral reasoning signals for Source Graph queries.

This module distils the useful analytical layer from the proven
UltrafastSecp256k1 Source Graph without importing any project-specific packet,
curve, build-layout, or domain rules.  Every result is derived from canonical
AIWorkHub graph rows, bounded source snippets, or bounded Git history reads.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


_TEST_PATH_RE = re.compile(r"(^|/)(tests?|specs?)(/|$)|(^|/)(test_|spec_)", re.IGNORECASE)
_TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b[:\s-]*(.{0,180})", re.IGNORECASE)
_COMMENT_PREFIX_RE = re.compile(
    r"(?:^|\s)(?:#|//|/\*|\*|<!--|--|;|REM\s+)", re.IGNORECASE,
)
_BRANCH_RE = re.compile(r"\b(if|elif|else|switch|case|match|catch|except)\b|\?")
_LOOP_RE = re.compile(r"\b(for|while|loop)\b")
_SECURITY_RE = re.compile(
    r"\b(auth|credential|password|secret|token|permission|sandbox|signature|crypto)\b",
    re.IGNORECASE,
)


def candidate_files(matches: list[dict[str, Any]], *, limit: int) -> list[str]:
    files: list[str] = []
    for row in matches:
        path = str(row.get("file_path") or "")
        if path and path not in files:
            files.append(path)
        if len(files) >= max(1, limit):
            break
    return files


def call_edges(
    conn: sqlite3.Connection,
    files: list[str],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not files:
        return [], []
    placeholders = ",".join("?" for _ in files)
    select = (
        "SELECT DISTINCT e.src_qualname AS caller_symbol, e.file_path AS caller_file, "
        "e.dst_name AS callee_symbol, e.dst_qualname, t.file_path AS callee_file, "
        "e.line, e.evidence_label, e.confidence FROM edges e "
        "LEFT JOIN entities t ON t.qualname=e.dst_qualname "
    )
    cap = max(1, limit)
    outgoing = [dict(row) for row in conn.execute(
        select + f"WHERE e.kind='calls' AND e.file_path IN ({placeholders}) "
        "ORDER BY e.confidence DESC, e.file_path, e.line LIMIT ?",
        (*files, cap),
    )]
    incoming = [dict(row) for row in conn.execute(
        select + f"WHERE e.kind='calls' AND t.file_path IN ({placeholders}) "
        "ORDER BY e.confidence DESC, t.file_path, e.line LIMIT ?",
        (*files, cap),
    )]
    return outgoing, incoming


def call_edges_for_symbols(
    conn: sqlite3.Connection,
    qualnames: list[str],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return only call edges that touch the selected symbol authorities.

    A file can contain hundreds of unrelated functions.  ``slice`` is a
    symbol-level operation, so widening an exact target to every call in the
    containing file wastes response bytes and can send an agent toward the
    wrong implementation boundary.  Keep file-level expansion available to
    ``impact``/``trace`` while making the minimal slice exact.
    """

    selected = list(dict.fromkeys(item for item in qualnames if item))
    if not selected:
        return [], []
    placeholders = ",".join("?" for _ in selected)
    select = (
        "SELECT DISTINCT e.src_qualname AS caller_symbol, e.file_path AS caller_file, "
        "e.dst_name AS callee_symbol, e.dst_qualname, t.file_path AS callee_file, "
        "e.line, e.evidence_label, e.confidence FROM edges e "
        "LEFT JOIN entities t ON t.qualname=e.dst_qualname "
    )
    cap = max(1, limit)
    outgoing = [dict(row) for row in conn.execute(
        select + f"WHERE e.kind='calls' AND e.src_qualname IN ({placeholders}) "
        "ORDER BY e.confidence DESC, e.file_path, e.line LIMIT ?",
        (*selected, cap),
    )]
    incoming = [dict(row) for row in conn.execute(
        select + f"WHERE e.kind='calls' AND e.dst_qualname IN ({placeholders}) "
        "ORDER BY e.confidence DESC, e.file_path, e.line LIMIT ?",
        (*selected, cap),
    )]
    return outgoing, incoming


def _safe_lines(repo_root: Path, relative_path: str) -> list[str]:
    try:
        root = repo_root.resolve()
        target = (root / relative_path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return []
        return target.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return []


def _test_candidates(
    conn: sqlite3.Connection,
    files: list[str],
    matches: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if not files:
        return []
    target_qualnames = {
        str(row.get("qualname") or "") for row in matches if row.get("qualname")
    }
    scored: dict[str, dict[str, Any]] = {}

    if target_qualnames:
        placeholders = ",".join("?" for _ in target_qualnames)
        rows = conn.execute(
            f"SELECT file_path, dst_qualname, COUNT(*) AS edge_count FROM edges "
            f"WHERE kind='calls' AND dst_qualname IN ({placeholders}) "
            "GROUP BY file_path, dst_qualname ORDER BY edge_count DESC LIMIT ?",
            (*sorted(target_qualnames), max(1, limit * 4)),
        ).fetchall()
        for row in rows:
            path = str(row["file_path"])
            if not _TEST_PATH_RE.search(path):
                continue
            entry = scored.setdefault(path, {"file_path": path, "score": 0, "reasons": []})
            entry["score"] += int(row["edge_count"]) * 4
            entry["reasons"].append(f"calls:{row['dst_qualname']}")

    stems = {Path(path).stem.lower().removeprefix("test_").removesuffix("_test") for path in files}
    rows = conn.execute(
        "SELECT file_path FROM files WHERE lower(file_path) LIKE '%test%' "
        "OR lower(file_path) LIKE '%spec%' ORDER BY file_path LIMIT 1000"
    ).fetchall()
    for row in rows:
        path = str(row["file_path"])
        lowered = path.lower()
        overlap = [stem for stem in stems if len(stem) >= 3 and stem in lowered]
        if not overlap:
            continue
        entry = scored.setdefault(path, {"file_path": path, "score": 0, "reasons": []})
        entry["score"] += len(overlap) * 2
        entry["reasons"].append("path_stem_match")

    results = sorted(scored.values(), key=lambda row: (-int(row["score"]), row["file_path"]))
    return results[: max(1, limit)]


def test_candidates(
    conn: sqlite3.Connection,
    files: list[str],
    matches: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Public bounded structural test mapping used by analytics modes."""

    return _test_candidates(conn, files, matches, limit=limit)


def compute_git_metrics(
    conn: sqlite3.Connection,
    repo_root: Path,
    files: list[str],
    *,
    limit: int = 10000,
) -> dict[str, Any]:
    """Run the bounded 90-day git walk OUTSIDE any held write transaction.

    The git subprocess -- ``rev-parse`` and, only on a changed HEAD, the
    ``git log --numstat`` walk -- happens here and nowhere else, so it never
    executes while the index write transaction holds the connection. The
    recorded HEAD oid is the generation key: an unchanged HEAD returns a
    ``cached`` plan that does no walk and touches no history. The returned plan
    is pure data; :func:`persist_git_metrics` applies it with SQLite-only work.
    Only reads run against ``conn`` here, never a write, so no writer lock is
    taken across the subprocess.
    """

    selected = files[: max(1, min(limit, 10000))]
    selected_set = set(selected)
    if not selected:
        return {
            "action": "noop", "delete_history": False, "rows": [], "head": "",
            "result": {"available": True, "window": "90d", "files": []},
        }
    if not (repo_root / ".git").exists():
        return {
            "action": "noop", "delete_history": False, "rows": [], "head": "",
            "result": {
                "available": False, "window": "90d",
                "reason": "not_git_repo", "files": [],
            },
        }
    try:
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=2, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "action": "noop", "delete_history": False, "rows": [], "head": "",
            "result": {
                "available": False, "window": "90d",
                "reason": type(exc).__name__, "files": [],
            },
        }
    head = head_result.stdout.strip() if head_result.returncode == 0 else ""
    prior = conn.execute("SELECT value FROM meta WHERE key='git_history_head'").fetchone()
    history_count = int(conn.execute("SELECT COUNT(*) FROM file_history").fetchone()[0])
    if head and prior is not None and prior["value"] == head and history_count:
        return {
            "action": "cached", "delete_history": False, "rows": [], "head": head,
            "result": {"available": True, "window": "90d", "cached": True, "files": []},
        }
    try:
        result = subprocess.run(
            [
                "git", "log", "--since=90 days", "--format=@@%an", "--numstat",
                "--", ".",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # A cache miss already committed us to a rewrite; a failed walk must
        # empty the stale history so metrics degrade to absent, matching the
        # pre-split behaviour where DELETE ran before the walk.
        return {
            "action": "clear", "delete_history": True, "rows": [], "head": "",
            "result": {
                "available": False, "window": "90d",
                "reason": type(exc).__name__, "files": [],
            },
        }
    if result.returncode != 0:
        return {
            "action": "clear", "delete_history": True, "rows": [], "head": "",
            "result": {
                "available": False,
                "window": "90d",
                "reason": f"git_exit_{result.returncode}",
                "files": [],
            },
        }

    commits: Counter[str] = Counter()
    additions: Counter[str] = Counter()
    deletions: Counter[str] = Counter()
    authors: defaultdict[str, Counter[str]] = defaultdict(Counter)
    current_author = "unknown"
    for raw in result.stdout.splitlines():
        if raw.startswith("@@"):
            current_author = raw[2:].strip() or "unknown"
            continue
        parts = raw.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if path not in selected_set:
            continue
        commits[path] += 1
        authors[path][current_author] += 1
        if added.isdigit():
            additions[path] += int(added)
        if deleted.isdigit():
            deletions[path] += int(deleted)

    rows: list[dict[str, Any]] = []
    for path in selected:
        primary = authors[path].most_common(1)
        rows.append({
            "file_path": path,
            "commit_file_touches_90d": commits[path],
            "lines_added_90d": additions[path],
            "lines_deleted_90d": deletions[path],
            "churn_90d": additions[path] + deletions[path],
            "authors_90d": len(authors[path]),
            "primary_author_90d": primary[0][0] if primary else None,
            "ownership_evidence": "git_commit_file_touches",
        })
    return {
        "action": "write", "delete_history": True, "rows": rows, "head": head,
        "result": {"available": True, "window": "90d", "files": rows},
    }


def persist_git_metrics(
    conn: sqlite3.Connection, plan: dict[str, Any]
) -> dict[str, Any]:
    """Apply a :func:`compute_git_metrics` plan inside the held write txn.

    Pure SQLite work -- no subprocess. Rewriting ``file_history`` and the HEAD
    generation marker is fast, so this is the only git-metrics step that runs
    while the index write transaction owns the connection. A ``cached``/``noop``
    plan leaves history untouched; a ``clear`` plan empties it (a git failure
    degrades to absent metrics, never a failed build); a ``write`` plan
    rematerialises byte-identical rows and records the new HEAD generation key.
    """

    if plan.get("delete_history"):
        conn.execute("DELETE FROM file_history")
    for row in plan.get("rows", ()):  # type: ignore[assignment]
        conn.execute(
            "INSERT INTO file_history "
            "(file_path, commit_touches_90d, lines_added_90d, lines_deleted_90d, "
            "authors_90d, primary_author_90d, evidence) VALUES (?,?,?,?,?,?,?)",
            (
                row["file_path"], row["commit_file_touches_90d"],
                row["lines_added_90d"], row["lines_deleted_90d"],
                row["authors_90d"], row["primary_author_90d"],
                row["ownership_evidence"],
            ),
        )
    if plan.get("action") == "write" and plan.get("head"):
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('git_history_head', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (plan["head"],),
        )
    return plan["result"]


def materialize_git_metrics(
    conn: sqlite3.Connection,
    repo_root: Path,
    files: list[str],
    *,
    limit: int = 10000,
) -> dict[str, Any]:
    """Refresh bounded 90-day history during indexing, never during a query.

    Retained as the single-call form for callers outside the index build. The
    build path splits this into :func:`compute_git_metrics` (the git walk, run
    with no write transaction held) and :func:`persist_git_metrics` (the
    SQLite-only apply). Calling both here in one connection is byte-identical to
    the prior monolithic behaviour.
    """

    return persist_git_metrics(
        conn, compute_git_metrics(conn, repo_root, files, limit=limit)
    )


def _git_metrics(conn: sqlite3.Connection, files: list[str], *, limit: int) -> dict[str, Any]:
    selected = files[: max(1, min(limit, 16))]
    if not selected:
        return {"available": True, "window": "90d", "files": []}
    placeholders = ",".join("?" for _ in selected)
    try:
        rows = [dict(row) for row in conn.execute(
            "SELECT file_path, commit_touches_90d AS commit_file_touches_90d, "
            "lines_added_90d, lines_deleted_90d, "
            "(lines_added_90d + lines_deleted_90d) AS churn_90d, authors_90d, "
            "primary_author_90d, evidence AS ownership_evidence FROM file_history "
            f"WHERE file_path IN ({placeholders}) ORDER BY churn_90d DESC, file_path",
            selected,
        )]
    except sqlite3.OperationalError:
        return {"available": False, "window": "90d", "reason": "not_materialized", "files": []}
    return {"available": True, "window": "90d", "files": rows}


def _symbol_metrics(
    conn: sqlite3.Connection,
    repo_root: Path,
    matches: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in matches:
        if str(match.get("kind") or "") not in {
            "function", "method", "class", "struct", "union", "enum", "namespace",
        }:
            continue
        qualname = str(match.get("qualname") or "")
        if not qualname or qualname in seen:
            continue
        seen.add(qualname)
        path = str(match.get("file_path") or "")
        start = max(1, int(match.get("line_start") or 1))
        end = max(start, int(match.get("line_end") or start))
        lines = _safe_lines(repo_root, path)
        source = "\n".join(lines[start - 1:end])[:12000]
        branches = len(_BRANCH_RE.findall(source))
        loops = len(_LOOP_RE.findall(source))
        incoming = int(conn.execute(
            "SELECT COUNT(*) FROM edges WHERE kind='calls' AND dst_qualname=?", (qualname,)
        ).fetchone()[0])
        outgoing = int(conn.execute(
            "SELECT COUNT(*) FROM edges WHERE kind='calls' AND src_qualname=?", (qualname,)
        ).fetchone()[0])
        span = end - start + 1
        risk_reasons: list[str] = []
        if _SECURITY_RE.search(f"{qualname} {path}"):
            risk_reasons.append("security_sensitive_name_or_path")
        if branches >= 8:
            risk_reasons.append("branch_heavy")
        if span >= 120:
            risk_reasons.append("large_symbol")
        score = incoming * 4 + outgoing * 2 + loops * 3 + branches + min(span // 20, 10)
        rows.append({
            **match,
            "line_span": span,
            "incoming_calls": incoming,
            "outgoing_calls": outgoing,
            "loop_count": loops,
            "branch_count": branches,
            "priority_score": score,
            "risk_reasons": risk_reasons,
            "metrics_evidence": "deterministic_lexical_and_graph",
        })
        if len(rows) >= max(1, limit * 2):
            break
    rows.sort(key=lambda row: (-int(row["priority_score"]), str(row["qualname"])))
    return rows[: max(1, limit)]


def symbol_metrics(
    conn: sqlite3.Connection,
    repo_root: Path,
    matches: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Public deterministic symbol metrics over canonical graph evidence."""

    return _symbol_metrics(conn, repo_root, matches, limit=limit)


def _todos(repo_root: Path, files: list[str], *, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in files:
        for line_number, line in enumerate(_safe_lines(repo_root, path), start=1):
            match = _TODO_RE.search(line)
            # A TODO-like identifier or string literal is not repository work
            # evidence.  Require an observed comment marker before the tag;
            # this stays language-neutral and deliberately conservative.
            if match and _COMMENT_PREFIX_RE.search(line[:match.start()]):
                rows.append({
                    "file_path": path,
                    "line": line_number,
                    "marker": match.group(1).upper(),
                    "text": match.group(2).strip(),
                })
                if len(rows) >= max(1, limit):
                    return rows
    return rows


def todos(repo_root: Path, files: list[str], *, limit: int) -> list[dict[str, Any]]:
    """Public bounded TODO/FIXME evidence for repository summaries."""

    return _todos(repo_root, files, limit=limit)


def focus_insights(
    conn: sqlite3.Connection,
    repo_root: Path,
    matches: list[dict[str, Any]],
    *,
    budget: int,
) -> dict[str, Any]:
    files = candidate_files(matches, limit=min(12, budget))
    ranked = _symbol_metrics(conn, repo_root, matches, limit=min(12, budget))
    git = _git_metrics(conn, files, limit=min(12, budget))
    tests = _test_candidates(conn, files, matches, limit=min(8, budget))
    risks = [
        {"qualname": row["qualname"], "file_path": row["file_path"],
         "priority_score": row["priority_score"], "reasons": row["risk_reasons"]}
        for row in ranked if row["risk_reasons"]
    ]
    return {
        "ranked_symbols": ranked,
        # ``ranked_symbols`` already owns the full symbol rows.  Keep the hot
        # projection as stable references plus the one differentiating score
        # instead of repeating signatures, ranges and evidence byte-for-byte.
        "hot_symbols": [
            {
                "qualname": row["qualname"],
                "file_path": row["file_path"],
                "priority_score": row["priority_score"],
            }
            for row in ranked if int(row["priority_score"]) >= 8
        ][:8],
        "risks": risks[:8],
        "related_tests": tests,
        "git_signals": git,
        "todos": _todos(repo_root, files, limit=min(8, budget)),
        "recommended_next_steps": (
            [f"slice:{ranked[0]['qualname']}", f"context:{ranked[0]['file_path']}"]
            if ranked else []
        ),
    }


def slice_insights(
    conn: sqlite3.Connection,
    repo_root: Path,
    matches: list[dict[str, Any]],
    *,
    budget: int,
) -> dict[str, Any]:
    files = candidate_files(matches, limit=min(12, budget))
    qualnames = [
        str(row.get("qualname") or "")
        for row in matches
        if row.get("qualname")
    ]
    outgoing, incoming = call_edges_for_symbols(
        conn, qualnames, limit=min(50, budget),
    )
    return {
        "entry_symbols": _symbol_metrics(conn, repo_root, matches, limit=min(16, budget)),
        "outgoing_calls": outgoing,
        "incoming_calls": incoming,
        "related_tests": _test_candidates(conn, files, matches, limit=min(12, budget)),
    }


def trace_insights(
    conn: sqlite3.Connection,
    matches: list[dict[str, Any]],
    *,
    budget: int,
) -> dict[str, Any]:
    files = candidate_files(matches, limit=min(16, budget))
    outgoing, incoming = call_edges(conn, files, limit=min(64, budget))
    return {
        "outgoing_calls": outgoing,
        "incoming_calls": incoming,
        "related_tests": _test_candidates(conn, files, matches, limit=min(12, budget)),
        "trace_evidence": "resolved_call_edges_and_test_path_heuristics",
    }


def impact_insights(
    conn: sqlite3.Connection,
    repo_root: Path,
    matches: list[dict[str, Any]],
    *,
    budget: int,
) -> dict[str, Any]:
    files = candidate_files(matches, limit=min(24, budget))
    outgoing, incoming = call_edges(conn, files, limit=min(100, budget * 2))
    affected: dict[str, dict[str, Any]] = {
        path: {"file_path": path, "inbound": 0, "outbound": 0, "score": 0}
        for path in files
    }
    for edge in incoming:
        caller = str(edge.get("caller_file") or "")
        callee = str(edge.get("callee_file") or "")
        if callee in affected:
            affected[callee]["inbound"] += 1
            affected[callee]["score"] += 4
        if caller and caller not in affected:
            affected[caller] = {"file_path": caller, "inbound": 0, "outbound": 1, "score": 2}
    for edge in outgoing:
        caller = str(edge.get("caller_file") or "")
        callee = str(edge.get("callee_file") or "")
        if caller in affected:
            affected[caller]["outbound"] += 1
            affected[caller]["score"] += 2
        if callee and callee not in affected:
            affected[callee] = {"file_path": callee, "inbound": 1, "outbound": 0, "score": 4}
    ranked = sorted(affected.values(), key=lambda row: (-int(row["score"]), row["file_path"]))
    return {
        "affected_files": ranked[: max(1, budget)],
        "related_tests": _test_candidates(conn, files, matches, limit=min(16, budget)),
        "git_signals": _git_metrics(conn, files, limit=min(16, budget)),
        "impact_evidence": "bidirectional_resolved_calls_plus_bounded_git_history",
    }


__all__ = [
    "call_edges",
    "call_edges_for_symbols",
    "candidate_files",
    "focus_insights",
    "impact_insights",
    "materialize_git_metrics",
    "symbol_metrics",
    "slice_insights",
    "test_candidates",
    "todos",
    "trace_insights",
]
