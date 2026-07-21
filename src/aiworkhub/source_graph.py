"""Canonical AIWorkHub Source Graph: sole implementation and storage authority.

Repository identity, not a fixed path or ambient ``cwd``, decides which
repository this module builds/queries. Durable graph state lives ONLY under
``<repo>/.aiworkhub/source_graph`` (resolved through
:mod:`aiworkhub.storage_registry`), so two repositories attached to this
tool never share or cross-contaminate a database.

Design constraints (see task card B849):

  * No model/API/network call, no Graphify dependency, no ``graph.json``
    authority, no second external graph product -- SQLite only.
  * Extraction is AST-first for Python (:mod:`aiworkhub.source_graph_ast`);
    unsupported languages fail closed rather than being approximated with
    regex heuristics mislabeled as extracted evidence.
  * Incremental indexing removes every entity/edge a changed OR deleted
    file owned before re-indexing it, so renames/deletes never leave a
    stale edge behind.
  * ``focus``/``slice``/``bundle`` stay backward compatible with the
    existing AIWorkHub project-context/worker-MCP callers: same command
    surface, JSON output, explicit byte/row budgets.
  * ``neighbors``/``shortest_path``/``component_summary`` are deterministic
    and enforce explicit depth/result caps -- no unbounded traversal.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import source_graph_ast as sgast
from .repository_state import HUB_DIRNAME, RepositoryStateError, inspect_repository
from .storage_registry import (
    StorageRegistryError,
    load_storage_registry,
    resolve_database_path,
)

SCHEMA_ID = "aiworkhub.source_graph.v1"
BUILD_REVISION = "aiworkhub.source_graph.python_ast.v1"

SOURCE_GRAPH_MODES: tuple[str, ...] = ("focus", "slice", "bundle")
SOURCE_GRAPH_BUNDLE_TYPES: tuple[str, ...] = (
    "bugfix", "feature", "refactor", "audit", "optimize", "explore",
)

MAX_BUDGET_ROWS = 200
MAX_DEPTH = 6
MAX_NEIGHBOR_RESULTS = 200
MAX_COMPONENT_NODES = 500
MAX_PATH_VISITS = 5000

DEFAULT_EXCLUDE_DIR_NAMES = frozenset({
    ".git", "__pycache__", ".venv", "venv", "env", "node_modules",
    HUB_DIRNAME, ".mypy_cache", ".pytest_cache", ".tox", ".ruff_cache",
    "dist", "build",
    # Ephemeral nested worktrees and editor/agent state, not canonical
    # source: ``.claude/worktrees/<task>`` holds full nested checkouts of
    # this same repository (their own ``tools/``, ``scripts/`` trees would
    # otherwise be indexed a second time per active worktree).
    ".claude", ".hg", ".svn", ".cache",
})

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    file_path TEXT PRIMARY KEY,
    language TEXT NOT NULL,
    status TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    build_revision TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualname TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    signature TEXT NOT NULL DEFAULT '',
    evidence_label TEXT NOT NULL,
    extractor TEXT NOT NULL,
    confidence REAL NOT NULL,
    source_hash TEXT NOT NULL,
    build_revision TEXT NOT NULL,
    UNIQUE(file_path, qualname)
);
CREATE INDEX IF NOT EXISTS idx_entities_file ON entities(file_path);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    kind TEXT NOT NULL,
    src_qualname TEXT NOT NULL,
    dst_name TEXT NOT NULL,
    dst_qualname TEXT,
    line INTEGER NOT NULL,
    evidence_label TEXT NOT NULL,
    extractor TEXT NOT NULL,
    confidence REAL NOT NULL,
    source_hash TEXT NOT NULL,
    build_revision TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_file ON edges(file_path);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_qualname);
CREATE INDEX IF NOT EXISTS idx_edges_dst_name ON edges(dst_name);
CREATE INDEX IF NOT EXISTS idx_edges_dst_qualname ON edges(dst_qualname);

CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
    name, qualname, signature, file_path, entity_id UNINDEXED
);
"""


class SourceGraphError(RuntimeError):
    """Base class for canonical Source Graph failures."""


class RepositoryUnresolvedError(SourceGraphError):
    """The repository identity could not be resolved (no manifest/registry)."""


# ---------------------------------------------------------------------------
# Repository / database resolution -- identity-bound, never a fixed path
# ---------------------------------------------------------------------------

def resolve_db_path(repo_root: Path) -> Path:
    """Resolve the canonical Source Graph database for ``repo_root``.

    Always ``<repo_root>/.aiworkhub/source_graph/source_graph.sqlite`` via
    the repository-bound storage registry -- never a fixed project-specific
    path and never influenced by process ``cwd``.
    """

    try:
        registry = load_storage_registry(repo_root)
        db_path = resolve_database_path(registry, "source_graph")
    except (RepositoryStateError, StorageRegistryError) as exc:
        raise RepositoryUnresolvedError(f"source_graph_repo_unresolved:{exc}") from exc
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def migration_dir(repo_root: Path) -> Path:
    return resolve_db_path(repo_root).parent


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# File discovery -- generic, repo-agnostic (no project-specific hardcoding)
# ---------------------------------------------------------------------------

def iter_source_files(repo_root: Path) -> list[Path]:
    repo_root = repo_root.resolve()
    out: list[Path] = []
    for path in sorted(repo_root.rglob("*.py")):
        rel_parts = path.relative_to(repo_root).parts[:-1]
        if any(part in DEFAULT_EXCLUDE_DIR_NAMES or part.endswith(".egg-info") for part in rel_parts):
            continue
        out.append(path)
    return out


# ---------------------------------------------------------------------------
# Build / incremental indexing
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class BuildReport:
    repo_root: str
    db_path: str
    incremental: bool
    files_seen: int
    files_changed: int
    files_unchanged: int
    files_removed: int
    entities_written: int
    edges_written: int
    errors: list[dict[str, str]]
    build_revision: str
    finished_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "repo_root": self.repo_root, "db_path": self.db_path,
            "incremental": self.incremental, "files_seen": self.files_seen,
            "files_changed": self.files_changed, "files_unchanged": self.files_unchanged,
            "files_removed": self.files_removed, "entities_written": self.entities_written,
            "edges_written": self.edges_written, "errors": self.errors,
            "build_revision": self.build_revision, "finished_at": self.finished_at,
        }


def _invalidate_file(conn: sqlite3.Connection, rel: str) -> None:
    """Remove every entity/edge/FTS row a file owns before re-indexing it.

    Called for changed files (before re-extraction) AND for files that
    were indexed before but no longer exist on disk (rename/delete), so a
    stale edge from a moved-away file can never survive a rebuild.
    """

    ids = [row[0] for row in conn.execute("SELECT id FROM entities WHERE file_path=?", (rel,))]
    if ids:
        conn.executemany("DELETE FROM entities_fts WHERE entity_id=?", [(i,) for i in ids])
    conn.execute("DELETE FROM entities WHERE file_path=?", (rel,))
    conn.execute("DELETE FROM edges WHERE file_path=?", (rel,))
    conn.execute("DELETE FROM files WHERE file_path=?", (rel,))


def _write_extraction(conn: sqlite3.Connection, extraction: sgast.FileExtraction) -> None:
    conn.execute(
        "INSERT INTO files(file_path, language, status, source_hash, indexed_at, build_revision) "
        "VALUES (?,?,?,?,?,?)",
        (extraction.file_path, extraction.language, extraction.status,
         extraction.source_hash, _now_iso(), BUILD_REVISION),
    )
    for entity in extraction.entities:
        cur = conn.execute(
            "INSERT INTO entities(file_path, kind, name, qualname, line_start, line_end, "
            "signature, evidence_label, extractor, confidence, source_hash, build_revision) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (entity.file_path, entity.kind, entity.name, entity.qualname, entity.line_start,
             entity.line_end, entity.signature, entity.evidence_label, entity.extractor,
             entity.confidence, entity.source_hash, entity.build_revision),
        )
        conn.execute(
            "INSERT INTO entities_fts(entity_id, name, qualname, signature, file_path) "
            "VALUES (?,?,?,?,?)",
            (cur.lastrowid, entity.name, entity.qualname, entity.signature, entity.file_path),
        )
    for edge in extraction.edges:
        conn.execute(
            "INSERT INTO edges(file_path, kind, src_qualname, dst_name, dst_qualname, line, "
            "evidence_label, extractor, confidence, source_hash, build_revision) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (edge.file_path, edge.kind, edge.src_qualname, edge.dst_name, edge.dst_qualname,
             edge.line, edge.evidence_label, edge.extractor, edge.confidence,
             edge.source_hash, edge.build_revision),
        )


def build_index(repo_root: Path, *, db_path: Path | None = None, incremental: bool = True) -> BuildReport:
    repo_root = repo_root.resolve()
    resolved_db_path = db_path or resolve_db_path(repo_root)
    conn = connect(resolved_db_path)
    files_on_disk = iter_source_files(repo_root)
    seen_rel: set[str] = set()
    changed = unchanged = removed = entities_written = edges_written = 0
    errors: list[dict[str, str]] = []
    try:
        with conn:
            existing = {
                row["file_path"]: row["source_hash"]
                for row in conn.execute("SELECT file_path, source_hash FROM files")
            }
            for path in files_on_disk:
                extraction = sgast.extract_file(repo_root, path, build_revision=BUILD_REVISION)
                seen_rel.add(extraction.file_path)
                prior_hash = existing.get(extraction.file_path)
                if incremental and prior_hash is not None and prior_hash == extraction.source_hash:
                    unchanged += 1
                    continue
                _invalidate_file(conn, extraction.file_path)
                _write_extraction(conn, extraction)
                changed += 1
                entities_written += len(extraction.entities)
                edges_written += len(extraction.edges)
                if extraction.status != "ok" and extraction.error:
                    errors.append({
                        "file": extraction.file_path, "status": extraction.status,
                        "error": extraction.error,
                    })
            for rel in list(existing):
                if rel not in seen_rel:
                    _invalidate_file(conn, rel)
                    removed += 1
            finished_at = _now_iso()
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('last_build', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps({
                    "finished_at": finished_at, "incremental": incremental,
                    "files_seen": len(files_on_disk), "files_changed": changed,
                    "files_removed": removed, "build_revision": BUILD_REVISION,
                }),),
            )
    finally:
        conn.close()
    return BuildReport(
        repo_root=str(repo_root), db_path=str(resolved_db_path), incremental=incremental,
        files_seen=len(files_on_disk), files_changed=changed, files_unchanged=unchanged,
        files_removed=removed, entities_written=entities_written, edges_written=edges_written,
        errors=errors, build_revision=BUILD_REVISION, finished_at=_now_iso(),
    )


# ---------------------------------------------------------------------------
# Query surface: find / func / body / struct / context / summary
# ---------------------------------------------------------------------------

def _fts_phrase(term: str) -> str:
    cleaned = term.replace('"', '""').strip()
    return f'"{cleaned}"*' if cleaned else '""'


def find(conn: sqlite3.Connection, term: str, *, limit: int = 24) -> list[dict[str, Any]]:
    term = (term or "").strip()
    if not term:
        return []
    limit = max(1, min(int(limit), MAX_BUDGET_ROWS))
    try:
        rows = conn.execute(
            "SELECT e.file_path, e.kind, e.name, e.qualname, e.line_start, e.line_end, "
            "e.signature, e.evidence_label, e.confidence FROM entities_fts f "
            "JOIN entities e ON e.id = f.entity_id WHERE entities_fts MATCH ? LIMIT ?",
            (_fts_phrase(term), limit),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        like = f"%{term}%"
        rows = conn.execute(
            "SELECT file_path, kind, name, qualname, line_start, line_end, signature, "
            "evidence_label, confidence FROM entities WHERE name LIKE ? OR qualname LIKE ? "
            "LIMIT ?",
            (like, like, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def func(conn: sqlite3.Connection, name: str, *, limit: int = 24) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), MAX_BUDGET_ROWS))
    rows = conn.execute(
        "SELECT file_path, kind, name, qualname, line_start, line_end, signature, "
        "evidence_label, confidence FROM entities WHERE kind IN ('function','method') "
        "AND name = ? LIMIT ?",
        (name, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def struct(conn: sqlite3.Connection, name: str, *, limit: int = 24) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), MAX_BUDGET_ROWS))
    rows = conn.execute(
        "SELECT file_path, kind, name, qualname, line_start, line_end, signature, "
        "evidence_label, confidence FROM entities WHERE kind = 'class' AND name = ? LIMIT ?",
        (name, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def body(conn: sqlite3.Connection, repo_root: Path, name: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT file_path, kind, name, qualname, line_start, line_end, signature "
        "FROM entities WHERE kind IN ('function','method','class') AND name = ? "
        "ORDER BY kind LIMIT 1",
        (name,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    try:
        target = (repo_root / result["file_path"]).resolve()
        if not target.is_relative_to(repo_root.resolve()):
            raise ValueError("path_escape")
        lines = target.read_text(encoding="utf-8").splitlines()
        snippet = "\n".join(lines[result["line_start"] - 1: result["line_end"]])
    except (OSError, ValueError, UnicodeDecodeError):
        snippet = ""
    result["source"] = snippet
    return result


def context(conn: sqlite3.Connection, file_path: str) -> dict[str, Any]:
    file_row = conn.execute(
        "SELECT file_path, language, status, source_hash, indexed_at, build_revision "
        "FROM files WHERE file_path = ?",
        (file_path,),
    ).fetchone()
    entities = [
        dict(row) for row in conn.execute(
            "SELECT kind, name, qualname, line_start, line_end, signature, evidence_label, "
            "confidence FROM entities WHERE file_path = ? ORDER BY line_start LIMIT ?",
            (file_path, MAX_BUDGET_ROWS),
        )
    ]
    edges = [
        dict(row) for row in conn.execute(
            "SELECT kind, src_qualname, dst_name, dst_qualname, line, evidence_label, "
            "confidence FROM edges WHERE file_path = ? ORDER BY line LIMIT ?",
            (file_path, MAX_BUDGET_ROWS),
        )
    ]
    return {
        "file": dict(file_row) if file_row else None,
        "entities": entities,
        "edges": edges,
        "found": file_row is not None,
    }


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    by_kind = {
        row["kind"]: row["c"]
        for row in conn.execute("SELECT kind, COUNT(*) c FROM entities GROUP BY kind")
    }
    by_evidence = {
        row["evidence_label"]: row["c"]
        for row in conn.execute("SELECT evidence_label, COUNT(*) c FROM edges GROUP BY evidence_label")
    }
    by_status = {
        row["status"]: row["c"]
        for row in conn.execute("SELECT status, COUNT(*) c FROM files GROUP BY status")
    }
    last_build_row = conn.execute("SELECT value FROM meta WHERE key='last_build'").fetchone()
    return {
        "files": file_count, "entities": entity_count, "edges": edge_count,
        "entities_by_kind": by_kind, "edges_by_evidence_label": by_evidence,
        "files_by_status": by_status,
        "last_build": json.loads(last_build_row["value"]) if last_build_row else None,
    }


# ---------------------------------------------------------------------------
# Bounded graph traversal: neighbors / shortest_path / component_summary
# ---------------------------------------------------------------------------

def neighbors(conn: sqlite3.Connection, qualname: str, *, depth: int = 1, limit: int = 50) -> dict[str, Any]:
    depth = max(1, min(int(depth), MAX_DEPTH))
    limit = max(1, min(int(limit), MAX_NEIGHBOR_RESULTS))
    frontier = {qualname}
    visited = {qualname}
    out: list[dict[str, Any]] = []
    for _ in range(depth):
        next_frontier: set[str] = set()
        for node in frontier:
            rows = conn.execute(
                "SELECT dst_name, dst_qualname, kind, evidence_label, confidence, file_path, "
                "line FROM edges WHERE src_qualname = ? ORDER BY id LIMIT ?",
                (node, limit),
            ).fetchall()
            for row in rows:
                dst = row["dst_qualname"] or row["dst_name"]
                if dst not in visited:
                    visited.add(dst)
                    next_frontier.add(dst)
                    out.append({**dict(row), "src": node})
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break
        frontier = next_frontier
        if len(out) >= limit or not frontier:
            break
    return {"root": qualname, "depth": depth, "limit": limit, "neighbors": out[:limit]}


def shortest_path(
    conn: sqlite3.Connection, src: str, dst: str, *, max_depth: int = 6, max_visits: int = MAX_PATH_VISITS,
) -> dict[str, Any]:
    max_depth = max(1, min(int(max_depth), MAX_DEPTH))
    max_visits = max(1, min(int(max_visits), MAX_PATH_VISITS))
    if src == dst:
        return {"src": src, "dst": dst, "path": [src], "found": True, "truncated": False}
    queue: deque[tuple[str, list[str]]] = deque([(src, [src])])
    seen = {src}
    visits = 0
    while queue:
        node, path = queue.popleft()
        if len(path) - 1 >= max_depth:
            continue
        rows = conn.execute(
            "SELECT dst_name, dst_qualname FROM edges WHERE src_qualname = ? ORDER BY id",
            (node,),
        ).fetchall()
        for row in rows:
            nxt = row["dst_qualname"] or row["dst_name"]
            if nxt in seen:
                continue
            visits += 1
            if visits > max_visits:
                return {"src": src, "dst": dst, "path": [], "found": False, "truncated": True}
            new_path = path + [nxt]
            if nxt == dst:
                return {"src": src, "dst": dst, "path": new_path, "found": True, "truncated": False}
            seen.add(nxt)
            queue.append((nxt, new_path))
    return {"src": src, "dst": dst, "path": [], "found": False, "truncated": False}


def component_summary(
    conn: sqlite3.Connection, qualname: str, *, max_depth: int = 3, max_nodes: int = 200,
) -> dict[str, Any]:
    max_depth = max(1, min(int(max_depth), MAX_DEPTH))
    max_nodes = max(1, min(int(max_nodes), MAX_COMPONENT_NODES))
    seen = {qualname}
    queue: deque[tuple[str, int]] = deque([(qualname, 0)])
    members: list[str] = []
    while queue and len(members) < max_nodes:
        node, depth = queue.popleft()
        members.append(node)
        if depth >= max_depth:
            continue
        rows = conn.execute(
            "SELECT dst_name, dst_qualname FROM edges WHERE src_qualname = ? "
            "UNION SELECT src_qualname AS dst_name, src_qualname AS dst_qualname "
            "FROM edges WHERE dst_qualname = ? ORDER BY 1 LIMIT ?",
            (node, node, max_nodes),
        ).fetchall()
        for row in rows:
            nxt = row["dst_qualname"] or row["dst_name"]
            if nxt not in seen and len(seen) < max_nodes:
                seen.add(nxt)
                queue.append((nxt, depth + 1))
    kind_counts: dict[str, int] = {}
    if members:
        placeholders = ",".join("?" for _ in members)
        for row in conn.execute(
            f"SELECT kind, COUNT(*) c FROM entities WHERE qualname IN ({placeholders}) GROUP BY kind",
            members,
        ):
            kind_counts[row["kind"]] = row["c"]
    return {
        "root": qualname, "max_depth": max_depth, "max_nodes": max_nodes,
        "member_count": len(members), "members": members[:max_nodes], "kind_counts": kind_counts,
    }


# ---------------------------------------------------------------------------
# focus / slice / bundle -- compact, budget-bounded (project-context contract)
# ---------------------------------------------------------------------------

def _bounded_rows(rows: list[dict[str, Any]], row_cap: int, byte_cap: int) -> tuple[list[dict[str, Any]], bool]:
    rows = rows[:row_cap]
    truncated = False
    while rows and len(json.dumps(rows, ensure_ascii=False).encode("utf-8")) > byte_cap:
        rows = rows[:-1]
        truncated = True
    return rows, truncated


def _query_payload(repo_root: Path, mode: str, query: str, budget: int) -> dict[str, Any]:
    budget = max(1, min(int(budget), MAX_BUDGET_ROWS))
    byte_cap = max(512, budget * 512)
    db_path = resolve_db_path(repo_root)
    conn = connect(db_path)
    try:
        matches = find(conn, query, limit=budget)
        matches, truncated = _bounded_rows(matches, budget, byte_cap)
        payload: dict[str, Any] = {
            "mode": mode, "query": query, "budget": budget, "matches": matches,
            "truncated": truncated,
        }
        if mode == "slice" and matches:
            top = matches[0]
            payload["neighbors"] = neighbors(conn, top["qualname"], depth=1, limit=min(budget, 50))["neighbors"]
        return payload
    finally:
        conn.close()


def focus(repo_root: Path, query: str, budget: int = 64) -> dict[str, Any]:
    return _query_payload(repo_root, "focus", query, budget)


def slice_(repo_root: Path, query: str, budget: int = 64) -> dict[str, Any]:
    return _query_payload(repo_root, "slice", query, budget)


def bundle(repo_root: Path, bundle_type: str, query: str, max_lines: int = 64) -> dict[str, Any]:
    if bundle_type not in SOURCE_GRAPH_BUNDLE_TYPES:
        raise SourceGraphError(f"invalid_bundle_type:{bundle_type}")
    budget = max(1, min(int(max_lines), MAX_BUDGET_ROWS))
    byte_cap = max(512, budget * 512)
    db_path = resolve_db_path(repo_root)
    conn = connect(db_path)
    try:
        matches = find(conn, query, limit=budget)
        sections: list[dict[str, Any]] = []
        remaining = budget
        for match in matches:
            if remaining <= 0:
                break
            ctx = context(conn, match["file_path"])
            ctx["entities"] = ctx["entities"][: max(1, remaining)]
            sections.append(ctx)
            remaining -= len(ctx["entities"])
        sections, truncated = _bounded_rows(sections, budget, byte_cap)
        return {
            "mode": "bundle", "bundle_type": bundle_type, "query": query,
            "budget": budget, "sections": sections, "truncated": truncated,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aiworkhub.source_graph")
    parser.add_argument("--repo", default=None, help="repository root (defaults to cwd-resolved manifest)")
    sub = parser.add_subparsers(dest="command", required=True)

    build_p = sub.add_parser("build")
    build_p.add_argument("-i", "--incremental", action="store_true")

    for name in ("find", "func", "struct", "body", "context"):
        p = sub.add_parser(name)
        p.add_argument("term")
        if name != "body" and name != "context":
            p.add_argument("--json", action="store_true", default=True)

    sub.add_parser("summary")

    for name in ("focus", "slice"):
        p = sub.add_parser(name)
        p.add_argument("term")
        p.add_argument("budget", type=int, nargs="?", default=64)
        p.add_argument("--json", action="store_true", default=True)

    bundle_p = sub.add_parser("bundle")
    bundle_p.add_argument("bundle_type")
    bundle_p.add_argument("term")
    bundle_p.add_argument("--max-lines", type=int, default=64)
    bundle_p.add_argument("--json", action="store_true", default=True)

    args = parser.parse_args(argv)
    repo_root = Path(args.repo).resolve() if args.repo else inspect_repository().root

    if args.command == "build":
        report = build_index(repo_root, incremental=args.incremental)
        _print_json(report.to_json())
        return 0

    db_path = resolve_db_path(repo_root)
    conn = connect(db_path)
    try:
        if args.command == "find":
            _print_json({"matches": find(conn, args.term)})
        elif args.command == "func":
            _print_json({"matches": func(conn, args.term)})
        elif args.command == "struct":
            _print_json({"matches": struct(conn, args.term)})
        elif args.command == "body":
            result = body(conn, repo_root, args.term)
            _print_json(result or {})
        elif args.command == "context":
            _print_json(context(conn, args.term))
        elif args.command == "summary":
            _print_json(summary(conn))
        elif args.command == "focus":
            _print_json(focus(repo_root, args.term, args.budget))
        elif args.command == "slice":
            _print_json(slice_(repo_root, args.term, args.budget))
        elif args.command == "bundle":
            _print_json(bundle(repo_root, args.bundle_type, args.term, args.max_lines))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "BUILD_REVISION",
    "BuildReport",
    "MAX_BUDGET_ROWS",
    "MAX_COMPONENT_NODES",
    "MAX_DEPTH",
    "MAX_NEIGHBOR_RESULTS",
    "RepositoryUnresolvedError",
    "SCHEMA",
    "SOURCE_GRAPH_BUNDLE_TYPES",
    "SOURCE_GRAPH_MODES",
    "SourceGraphError",
    "body",
    "build_index",
    "bundle",
    "component_summary",
    "connect",
    "context",
    "find",
    "focus",
    "func",
    "iter_source_files",
    "neighbors",
    "resolve_db_path",
    "shortest_path",
    "slice_",
    "struct",
    "summary",
]
