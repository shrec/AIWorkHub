"""Repository-neutral Source Graph analytics over the canonical graph database.

The original UltrafastSecp256k1 Source Graph exposed useful operational views
in addition to semantic search.  This module ports those generic views without
copying its project-specific crypto, packet, build-layout, or decision stores.
Every result is computed from the repository's canonical Source Graph tables
and is explicitly bounded.  Runtime coverage is never inferred from filenames
or call edges: when no imported runtime evidence exists it is reported as
``not_available``.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import source_graph_insights as insights


ANALYTIC_MODES: tuple[str, ...] = (
    "tags",
    "hotspots",
    "coverage",
    "churn",
    "reviewqueue",
    "ownership",
    "testmap",
    "calls",
    "symbols",
    "bottlenecks",
    "auditmap",
    "complexity",
    "stats",
    "summarize",
    "pipeline",
    "todo",
    "leaks",
    "nullrisks",
    "rawptrs",
    "casts",
    "crashes",
    "looprisks",
    "deadmethods",
    "duplicates",
    "gaps",
)

_SECURITY_RE = re.compile(
    r"\b(auth|credential|password|secret|token|permission|sandbox|signature|crypto)\b",
    re.IGNORECASE,
)
_BUILD_RE = re.compile(r"(^|/)(build|ci|scripts?|tools?)(/|$)", re.IGNORECASE)
_DATA_RE = re.compile(r"\b(data|schema|model|record|entity|store|database|db)\b", re.IGNORECASE)
_API_RE = re.compile(r"\b(api|server|client|handler|route|controller|endpoint)\b", re.IGNORECASE)
_TEST_RE = re.compile(r"(^|/)(tests?|specs?)(/|$)|(^|/)(test_|spec_)", re.IGNORECASE)
_RAW_PTR_RE = re.compile(
    r"\b(?:char|short|int|long|float|double|void|[A-Z]\w*(?:::\w+)*)\s*\*\s*\w+"
)
_UNSAFE_CAST_RE = re.compile(r"\b(?:reinterpret_cast|const_cast)\s*<|\([A-Za-z_]\w*\s*\*\)\s*\w+")
_NULL_DEREF_RE = re.compile(r"\b([A-Za-z_]\w*)\s*->")
_ALLOC_RE = re.compile(r"\b(?:new|malloc|calloc|realloc)\b")
_FREE_RE = re.compile(r"\b(?:delete|free)\b")
_INFINITE_LOOP_RE = re.compile(r"\bwhile\s*\(\s*(?:true|1)\s*\)|\bfor\s*\(\s*;\s*;\s*\)")
_DIVISION_RE = re.compile(r"(?<!/)\/(?![/=*])\s*([A-Za-z_]\w*)")


def _entity_rows(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        "SELECT file_path, kind, name, qualname, line_start, line_end, signature, "
        "evidence_label, confidence FROM entities WHERE kind <> 'file' "
        "ORDER BY CASE WHEN kind IN ('function','method') THEN 0 ELSE 1 END, "
        "file_path, line_start LIMIT ?",
        (max(1, limit),),
    )]


def _file_rows(conn: sqlite3.Connection, files: list[str], *, limit: int) -> list[dict[str, Any]]:
    cap = max(1, limit)
    if files:
        placeholders = ",".join("?" for _ in files)
        return [dict(row) for row in conn.execute(
            "SELECT file_path, language, status, source_hash, indexed_at, build_revision "
            f"FROM files WHERE file_path IN ({placeholders}) ORDER BY file_path LIMIT ?",
            (*files, cap),
        )]
    return [dict(row) for row in conn.execute(
        "SELECT file_path, language, status, source_hash, indexed_at, build_revision "
        "FROM files ORDER BY file_path LIMIT ?",
        (cap,),
    )]


def _scope_matches(
    conn: sqlite3.Connection,
    matches: list[dict[str, Any]],
    *,
    budget: int,
) -> list[dict[str, Any]]:
    if matches:
        return matches[: max(1, min(budget * 4, 400))]
    return _entity_rows(conn, limit=max(16, min(budget * 4, 400)))


def _scope_files(
    conn: sqlite3.Connection,
    matches: list[dict[str, Any]],
    *,
    budget: int,
) -> list[str]:
    files = insights.candidate_files(matches, limit=min(max(8, budget), 64))
    if files:
        return files
    return [
        str(row["file_path"])
        for row in conn.execute("SELECT file_path FROM files ORDER BY file_path LIMIT ?", (min(64, max(8, budget)),))
    ]


def _history_rows(
    conn: sqlite3.Connection,
    files: list[str],
    *,
    budget: int,
) -> tuple[bool, list[dict[str, Any]], str | None]:
    try:
        if files:
            placeholders = ",".join("?" for _ in files)
            rows = conn.execute(
                "SELECT file_path, commit_touches_90d, lines_added_90d, lines_deleted_90d, "
                "(lines_added_90d + lines_deleted_90d) AS churn_90d, authors_90d, "
                "primary_author_90d, evidence FROM file_history "
                f"WHERE file_path IN ({placeholders}) "
                "ORDER BY churn_90d DESC, commit_touches_90d DESC, file_path LIMIT ?",
                (*files, max(1, budget)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT file_path, commit_touches_90d, lines_added_90d, lines_deleted_90d, "
                "(lines_added_90d + lines_deleted_90d) AS churn_90d, authors_90d, "
                "primary_author_90d, evidence FROM file_history "
                "ORDER BY churn_90d DESC, commit_touches_90d DESC, file_path LIMIT ?",
                (max(1, budget),),
            ).fetchall()
    except sqlite3.OperationalError:
        return False, [], "not_materialized"
    return True, [dict(row) for row in rows], None


def _test_map(
    conn: sqlite3.Connection,
    repo_root: Path,
    files: list[str],
    matches: list[dict[str, Any]],
    *,
    budget: int,
) -> dict[str, Any]:
    candidates = insights.test_candidates(
        conn, files, matches, limit=max(1, min(budget, 40)),
    )
    mapped_files = {str(row["file_path"]) for row in candidates}
    from . import evidence_instruments

    return {
        "source_files": files[: max(1, budget)],
        "related_tests": candidates,
        "structural_mapping": {
            "status": "available",
            "method": "resolved_call_edges_plus_path_stem_heuristics",
            "candidate_test_files": len(mapped_files),
            "claim": "test_relationship_only_not_execution_coverage",
        },
        "runtime_coverage": evidence_instruments.runtime_coverage_for_paths(
            repo_root, files
        ),
    }


def _tags_for(row: dict[str, Any]) -> list[str]:
    path = str(row.get("file_path") or "")
    text = " ".join((path, str(row.get("name") or ""), str(row.get("signature") or "")))
    tags = {f"kind:{row.get('kind') or 'unknown'}"}
    if _TEST_RE.search(path):
        tags.add("role:test")
    if _SECURITY_RE.search(text):
        tags.add("risk:security")
    if _BUILD_RE.search(path):
        tags.add("role:build_or_tooling")
    if _DATA_RE.search(text):
        tags.add("domain:data")
    if _API_RE.search(text):
        tags.add("role:api_boundary")
    return sorted(tags)


def _symbol_source(repo_root: Path, row: dict[str, Any], *, max_chars: int = 24000) -> str:
    try:
        root = repo_root.resolve()
        path = (root / str(row.get("file_path") or "")).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(row.get("line_start") or 1))
        end = max(start, int(row.get("line_end") or start))
        return "\n".join(lines[start - 1:end])[:max_chars]
    except (OSError, TypeError, ValueError):
        return ""


def _risk_views(
    conn: sqlite3.Connection,
    repo_root: Path,
    mode: str,
    rows: list[dict[str, Any]],
    *,
    budget: int,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    duplicate_groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    scanned = 0
    for row in rows:
        if str(row.get("kind") or "") not in {"function", "method", "class", "struct"}:
            continue
        source = _symbol_source(repo_root, row)
        if not source:
            continue
        scanned += 1
        reasons: list[str] = []
        if mode == "leaks" and len(_ALLOC_RE.findall(source)) > len(_FREE_RE.findall(source)):
            reasons.append("allocation_release_imbalance")
        elif mode == "rawptrs" and _RAW_PTR_RE.search(source):
            reasons.append("raw_pointer_declaration")
        elif mode == "casts" and _UNSAFE_CAST_RE.search(source):
            reasons.append("unsafe_cast_candidate")
        elif mode == "looprisks":
            for match in _INFINITE_LOOP_RE.finditer(source):
                tail = source[match.end():]
                close = tail.find("}")
                loop_body = tail[:close] if close >= 0 else tail[:4000]
                if not re.search(r"\b(?:break|return|throw|co_await|await)\b", loop_body):
                    reasons.append("unbounded_loop_without_lexical_exit")
                    break
        elif mode == "nullrisks":
            dereferenced = set(_NULL_DEREF_RE.findall(source))
            for name in dereferenced:
                guarded = re.search(
                    rf"(?:if|assert)\s*\([^\n)]*\b{re.escape(name)}\b[^\n)]*\)", source,
                )
                if not guarded:
                    reasons.append(f"unguarded_pointer_dereference:{name}")
                    break
        elif mode == "crashes":
            divisors = set(_DIVISION_RE.findall(source))
            for name in divisors:
                guarded = re.search(
                    rf"(?:if|assert)\s*\([^\n)]*\b{re.escape(name)}\b[^\n)]*\)", source,
                )
                if not guarded:
                    reasons.append(f"unchecked_divisor:{name}")
                    break
            if re.search(r"\b(?:abort|std::terminate)\s*\(", source):
                reasons.append("explicit_process_termination")
        elif mode == "deadmethods":
            qualname = str(row.get("qualname") or "")
            incoming = int(conn.execute(
                "SELECT COUNT(*) FROM edges WHERE kind='calls' AND dst_qualname=?", (qualname,)
            ).fetchone()[0])
            name = str(row.get("name") or "")
            if incoming == 0 and name not in {"main", "__init__", "activate", "deactivate"}:
                reasons.append("zero_resolved_incoming_calls")
        elif mode == "duplicates":
            normalized = re.sub(r"\s+", " ", re.sub(r"//.*|/\*.*?\*/|#.*", "", source, flags=re.S)).strip()
            if len(normalized) >= 80:
                duplicate_groups[hashlib.sha256(normalized.encode()).hexdigest()].append(row)
        if reasons:
            findings.append({
                "file_path": row.get("file_path"),
                "qualname": row.get("qualname"),
                "line_start": row.get("line_start"),
                "reasons": reasons,
                "evidence_class": "bounded_lexical_candidate_not_proven_defect",
            })
        if len(findings) >= budget:
            break
    if mode == "duplicates":
        findings = [
            {
                "source_hash": digest,
                "symbols": [
                    {"file_path": row.get("file_path"), "qualname": row.get("qualname")}
                    for row in group
                ],
                "evidence_class": "exact_normalized_symbol_body_match",
            }
            for digest, group in duplicate_groups.items()
            if len(group) > 1
        ][:budget]
    return {
        "status": "available" if scanned else "not_available",
        "reason": None if scanned else "semantic_symbol_bodies_unavailable_for_scope",
        "symbols_scanned": scanned,
        "findings": findings[:budget],
        "blocking": False,
    }


def _summary(conn: sqlite3.Connection) -> dict[str, Any]:
    by_language = {
        str(row["language"]): int(row["count"])
        for row in conn.execute(
            "SELECT language, COUNT(*) AS count FROM files GROUP BY language ORDER BY language"
        )
    }
    by_kind = {
        str(row["kind"]): int(row["count"])
        for row in conn.execute(
            "SELECT kind, COUNT(*) AS count FROM entities GROUP BY kind ORDER BY kind"
        )
    }
    return {
        "files": int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]),
        "entities": int(conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]),
        "edges": int(conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]),
        "files_by_language": by_language,
        "entities_by_kind": by_kind,
    }


def query(
    conn: sqlite3.Connection,
    repo_root: Path,
    *,
    mode: str,
    query_text: str,
    matches: list[dict[str, Any]],
    budget: int,
) -> dict[str, Any]:
    """Compute one bounded analytic view from canonical Source Graph rows."""

    if mode not in ANALYTIC_MODES:
        raise ValueError(f"unsupported_source_graph_analytic:{mode}")
    budget = max(1, min(int(budget), 200))
    scoped_matches = _scope_matches(conn, matches, budget=budget)
    files = _scope_files(conn, matches, budget=budget)
    symbol_metrics = insights.symbol_metrics(
        conn, repo_root, scoped_matches, limit=max(1, min(budget, 80)),
    )

    base: dict[str, Any] = {
        "mode": mode,
        "query": query_text,
        "budget": budget,
        "scope": "query_matches" if matches else "repository_fallback",
    }

    if mode == "tags":
        tagged = [
            {**row, "tags": _tags_for(row)}
            for row in scoped_matches[:budget]
        ]
        counts = Counter(tag for row in tagged for tag in row["tags"])
        return {**base, "symbols": tagged, "tag_counts": dict(sorted(counts.items()))}

    if mode in {"hotspots", "complexity", "bottlenecks"}:
        if mode == "complexity":
            ranked = sorted(
                symbol_metrics,
                key=lambda row: (
                    -int(row.get("branch_count") or 0),
                    -int(row.get("loop_count") or 0),
                    -int(row.get("line_span") or 0),
                    str(row.get("qualname") or ""),
                ),
            )
            score_contract = "branches_then_loops_then_line_span"
        elif mode == "bottlenecks":
            ranked = sorted(
                symbol_metrics,
                key=lambda row: (
                    -(int(row.get("incoming_calls") or 0) * 3 + int(row.get("outgoing_calls") or 0)),
                    str(row.get("qualname") or ""),
                ),
            )
            score_contract = "incoming_calls_x3_plus_outgoing_calls"
        else:
            ranked = sorted(
                symbol_metrics,
                key=lambda row: (-int(row.get("priority_score") or 0), str(row.get("qualname") or "")),
            )
            score_contract = "graph_fanout_branches_loops_span_and_security_risk"
        return {**base, "score_contract": score_contract, "ranked_symbols": ranked[:budget]}

    if mode in {"coverage", "testmap", "auditmap"}:
        test_map = _test_map(conn, repo_root, files, matches or scoped_matches, budget=budget)
        payload: dict[str, Any] = {**base, **test_map}
        if mode == "auditmap":
            mapped = bool(test_map["related_tests"])
            payload["audit_queue"] = [
                {
                    "file_path": path,
                    "structural_test_mapping": "present" if mapped else "missing",
                    "runtime_coverage": test_map["runtime_coverage"].get("status"),
                    "review_reason": "runtime_evidence_required",
                }
                for path in files[:budget]
            ]
        return payload

    if mode in {"churn", "ownership"}:
        available, history, reason = _history_rows(conn, files if matches else [], budget=budget)
        payload = {
            **base,
            "history": {"available": available, "window": "90d", "reason": reason},
            "files": history,
        }
        if mode == "ownership":
            payload["ownership_risks"] = [
                {
                    "file_path": row["file_path"],
                    "authors_90d": row["authors_90d"],
                    "primary_author_90d": row["primary_author_90d"],
                    "risk": "single_author" if int(row["authors_90d"]) == 1 else "shared",
                }
                for row in history
                if int(row["commit_touches_90d"]) > 0
            ]
        return payload

    if mode == "calls":
        outgoing, incoming = insights.call_edges(conn, files, limit=max(1, budget))
        return {
            **base,
            "files": files[:budget],
            "outgoing_calls": outgoing,
            "incoming_calls": incoming,
            "evidence": "canonical_resolved_and_inferred_call_edges",
        }

    if mode == "symbols":
        return {**base, "symbols": symbol_metrics[:budget]}

    if mode == "reviewqueue":
        tests = insights.test_candidates(conn, files, matches or scoped_matches, limit=min(budget, 40))
        from . import evidence_instruments

        runtime_coverage = evidence_instruments.runtime_coverage_for_paths(
            repo_root, files
        )
        test_files = {str(row["file_path"]) for row in tests}
        queue: list[dict[str, Any]] = []
        for row in symbol_metrics:
            reasons = list(row.get("risk_reasons") or [])
            path = str(row.get("file_path") or "")
            if not any(Path(test).stem.find(Path(path).stem) >= 0 for test in test_files):
                reasons.append("no_structural_test_mapping")
            if int(row.get("incoming_calls") or 0) >= 5:
                reasons.append("high_fan_in")
            if not reasons and int(row.get("priority_score") or 0) < 8:
                continue
            queue.append({**row, "review_reasons": sorted(set(reasons))})
        queue.sort(key=lambda row: (-int(row.get("priority_score") or 0), str(row.get("qualname") or "")))
        return {
            **base,
            "queue": queue[:budget],
            "runtime_coverage": runtime_coverage,
        }

    if mode == "todo":
        return {**base, "todos": insights.todos(repo_root, files, limit=budget)}

    if mode in {
        "leaks", "nullrisks", "rawptrs", "casts", "crashes", "looprisks",
        "deadmethods", "duplicates",
    }:
        return {
            **base,
            "analysis": _risk_views(
                conn, repo_root, mode, scoped_matches, budget=budget,
            ),
        }

    if mode == "gaps":
        tests = _test_map(conn, repo_root, files, matches or scoped_matches, budget=min(budget, 40))
        low_confidence = [
            dict(row) for row in conn.execute(
                "SELECT file_path, kind, src_qualname, dst_name, dst_qualname, line, "
                "evidence_label, confidence FROM edges WHERE confidence < 1.0 "
                "ORDER BY confidence, file_path, line LIMIT ?",
                (budget,),
            )
        ]
        return {
            **base,
            "todos": insights.todos(repo_root, files, limit=min(budget, 40)),
            "structural_test_mapping": tests,
            "low_confidence_edges": low_confidence,
            "runtime_coverage": tests["runtime_coverage"],
        }

    if mode == "stats":
        history_available, history, reason = _history_rows(conn, [], budget=min(budget, 10))
        from . import evidence_instruments

        return {
            **base,
            **_summary(conn),
            "history": {"available": history_available, "reason": reason, "top_churn": history},
            "runtime_coverage": evidence_instruments.runtime_coverage_for_paths(
                repo_root, []
            ),
            "capabilities": list(ANALYTIC_MODES),
        }

    if mode == "summarize":
        file_rows = _file_rows(conn, files if matches else [], limit=min(budget, 40))
        return {
            **base,
            "repository": _summary(conn),
            "files": file_rows,
            "top_symbols": symbol_metrics[: min(budget, 20)],
            "todos": insights.todos(repo_root, files, limit=min(budget, 20)),
        }

    # pipeline: one compact planning packet, still backed by the same DB.
    outgoing, incoming = insights.call_edges(conn, files, limit=min(budget, 40))
    test_map = _test_map(conn, repo_root, files, matches or scoped_matches, budget=min(budget, 24))
    return {
        **base,
        "focus": {"ranked_symbols": symbol_metrics[: min(budget, 20)]},
        "impact": {"outgoing_calls": outgoing, "incoming_calls": incoming},
        "verification": test_map,
        "recommended_sequence": ["focus", "slice", "impact", "testmap", "reviewqueue"],
        "authority": "canonical_source_graph_only",
    }


__all__ = ["ANALYTIC_MODES", "query"]
