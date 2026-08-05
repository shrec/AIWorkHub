"""Canonical AIWorkHub Source Graph: sole implementation and storage authority.

Repository identity, not a fixed path or ambient ``cwd``, decides which
repository this module builds/queries. Durable graph state lives ONLY under
``<repo>/.aiworkhub/source_graph`` (resolved through
:mod:`aiworkhub.storage_registry`), so two repositories attached to this
tool never share or cross-contaminate a database.

Design constraints (see task card B849):

  * No model/API/network call, no Graphify dependency, no ``graph.json``
    authority, no second external graph product -- SQLite only.
  * Extraction is AST-first for Python (:mod:`aiworkhub.source_graph_ast`).
    PHP and C/C++/CUDA receive conservative semantic lexical extraction.
    Registered file families without a semantic extractor get truthful
    file-level evidence (no fabricated functions/calls/edges); truly
    unregistered extensions fail closed rather than being mislabeled as
    extracted evidence.
  * Incremental indexing removes every entity/edge a changed OR deleted
    file owned before re-indexing it, so renames/deletes never leave a
    stale edge behind.
  * Compact discovery modes stay backward compatible, while repository-neutral
    analytics (hotspots, coverage maps, ownership, review queue, risk
    candidates and pipeline views) share the same JSON and byte/row budgets.
  * ``neighbors``/``shortest_path``/``component_summary`` are deterministic
    and enforce explicit depth/result caps -- no unbounded traversal.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sqlite3
import stat
import sys
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import source_graph_ast as sgast
from . import source_graph_analytics as sganalytics
from . import source_graph_insights as sginsights
from . import source_graph_languages as sglanguages
from .repository_state import HUB_DIRNAME, RepositoryStateError, inspect_repository
from .storage_registry import (
    StorageRegistryError,
    load_storage_registry,
    resolve_database_path,
)

SCHEMA_ID = "aiworkhub.source_graph.v1"
BUILD_REVISION = "aiworkhub.source_graph.semantic.v5"
IGNORE_SCHEMA_ID = "aiworkhub.source_graph.ignore.v1"
POLICY_SCHEMA_ID = "aiworkhub.source_graph.policy.v2"
IGNORE_CONFIG_RELATIVE_PATH = Path(HUB_DIRNAME) / "config" / "source_graph.json"
MAX_POLICY_BYTES = 64 * 1024

SOURCE_GRAPH_MODES: tuple[str, ...] = (
    "focus", "slice", "context", "file", "function", "class", "body", "bodygrep",
    "impact", "trace", "deps", "bundle",
    *sganalytics.ANALYTIC_MODES,
)
SOURCE_GRAPH_BUNDLE_TYPES: tuple[str, ...] = (
    "bugfix", "feature", "refactor", "audit", "optimize", "explore",
)

MAX_BUDGET_ROWS = 200
MAX_DEPTH = 6
MAX_NEIGHBOR_RESULTS = 200
MAX_COMPONENT_NODES = 500
MAX_PATH_VISITS = 5000
SOURCE_GRAPH_COMPACT_MIN_BYTES = 64 * 1024 * 1024
SOURCE_GRAPH_COMPACT_MIN_FREELIST_RATIO = 0.20
_INDEXED_EXTENSION_SET = frozenset(
    suffix.casefold() for suffix in sglanguages.INDEXED_EXTENSIONS
)

DEFAULT_EXCLUDE_DIR_NAMES = frozenset({
    ".git", "__pycache__", ".venv", "venv", "env", "node_modules",
    HUB_DIRNAME, ".mypy_cache", ".pytest_cache", ".tox", ".ruff_cache",
    "dist", "build", "archive", "logs", ".tmp",
    # CMake writes non-source ``.ts`` timestamp/dependency-tracking files
    # here (e.g. ``compiler_depend.ts``) -- indexing them as file-level
    # "typescript" evidence would be a false language label, not truthful
    # evidence of real JS/TS source.
    "CMakeFiles",
    # Ephemeral nested worktrees and editor/agent state, not canonical
    # source: ``.claude/worktrees/<task>`` holds full nested checkouts of
    # this same repository (their own ``tools/``, ``scripts/`` trees would
    # otherwise be indexed a second time per active worktree).
    ".claude", ".hg", ".svn", ".cache",
})

# New repository policy files start with only high-confidence generated
# measurement artifacts excluded.  JSON/XML remain enabled languages and
# ordinary configuration/data files remain indexable; owners can remove any
# of these editable globs from ``.aiworkhub/config/source_graph.json``.
DEFAULT_CONFIG_EXCLUDE_GLOBS: tuple[str, ...] = (
    "eval/*.json",
    "eval/**/*.json",
    "eval/*.jsonl",
    "eval/**/*.jsonl",
)

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
    file_size INTEGER NOT NULL DEFAULT -1,
    mtime_ns INTEGER NOT NULL DEFAULT -1,
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

CREATE TABLE IF NOT EXISTS file_history (
    file_path TEXT PRIMARY KEY,
    commit_touches_90d INTEGER NOT NULL DEFAULT 0,
    lines_added_90d INTEGER NOT NULL DEFAULT 0,
    lines_deleted_90d INTEGER NOT NULL DEFAULT 0,
    authors_90d INTEGER NOT NULL DEFAULT 0,
    primary_author_90d TEXT,
    evidence TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
    name, qualname, signature, file_path, entity_id UNINDEXED
);
"""


class SourceGraphError(RuntimeError):
    """Base class for canonical Source Graph failures."""


class RepositoryUnresolvedError(SourceGraphError):
    """The repository identity could not be resolved (no manifest/registry)."""


class SourceGraphBuildInProgressError(SourceGraphError):
    """Another process currently owns this repository's index writer lease."""


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


def connect(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")
        # Isolated workers receive read-only access to the canonical graph
        # directory. WAL readers may still need to create/update ``-shm``
        # even when the database URI itself uses ``mode=ro``; after a writer
        # closes and removes the sidecars, a later worker query therefore
        # fails with ``attempt to write a readonly database``. The rollback
        # journal keeps SQLite's normal reader/writer locking while requiring
        # no directory mutation from readers. Existing WAL databases migrate
        # on the next manager-owned build connection.
        journal_mode = str(conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
        if journal_mode != "delete":
            conn.close()
            raise SourceGraphBuildInProgressError(
                f"source_graph_journal_migration_busy:{journal_mode}"
            )
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        file_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(files)")
        }
        if "file_size" not in file_columns:
            conn.execute(
                "ALTER TABLE files ADD COLUMN file_size INTEGER NOT NULL DEFAULT -1"
            )
        if "mtime_ns" not in file_columns:
            conn.execute(
                "ALTER TABLE files ADD COLUMN mtime_ns INTEGER NOT NULL DEFAULT -1"
            )
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def index_write_lease(repo_root: Path):
    """Try to own the single cross-process writer lease for ``repo_root``.

    OS advisory locks are released automatically when a process exits, so a
    crashed/reloaded VS Code child cannot leave stale ownership. The lock is
    repository-local and therefore preserves multi-repository isolation.
    """

    lock_path = resolve_db_path(repo_root).with_name("index.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (BlockingIOError, OSError):
            acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# File discovery -- generic, repo-agnostic (no project-specific hardcoding)
# ---------------------------------------------------------------------------

INDEXED_EXTENSIONS: tuple[str, ...] = sglanguages.INDEXED_EXTENSIONS
LANGUAGE_CAPABILITIES: dict[str, str] = dict(sglanguages.LANGUAGE_CAPABILITIES)


@dataclass(frozen=True, slots=True)
class SourceGraphIgnorePolicy:
    """Repository-local additions to the non-bypassable safe defaults.

    ``exclude_dirs`` matches directory basenames at any depth.  Use
    ``exclude_globs`` for repository-relative path rules such as
    ``generated/**`` or ``**/*.min.js``.  Default excludes are always active:
    a repository config can extend them, but cannot accidentally make build,
    archive, VCS, cache, or AIWorkHub runtime trees indexable.
    """

    exclude_dirs: frozenset[str]
    exclude_globs: tuple[str, ...]
    disabled_languages: frozenset[str] = frozenset()
    revision: int = 0
    configured: bool = False

    @property
    def enabled_languages(self) -> frozenset[str]:
        return frozenset(sglanguages.LANGUAGE_BY_ID) - self.disabled_languages

    @property
    def indexed_extensions(self) -> frozenset[str]:
        return frozenset(
            extension
            for language in self.enabled_languages
            for extension in sglanguages.LANGUAGE_BY_ID[language].extensions
        )


def ignore_config_path(repo_root: Path) -> Path:
    return repo_root.resolve() / IGNORE_CONFIG_RELATIVE_PATH


def ensure_ignore_config(repo_root: Path) -> Path:
    """Create the editable repository-local ignore policy once.

    The exclusive create is intentionally non-destructive: repeated InitRepo
    calls never overwrite owner additions.  A concurrent initializer either
    wins the create or observes the winner's complete file.
    """

    path = ignore_config_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_id": POLICY_SCHEMA_ID,
        "revision": 1,
        "exclude_dirs": [],
        "exclude_globs": list(DEFAULT_CONFIG_EXCLUDE_GLOBS),
        "disabled_languages": [],
    }
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        pass
    return path


def _string_list(value: Any, *, field: str, config_path: Path) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise SourceGraphError(f"source_graph_ignore_invalid:{config_path}:{field}_must_be_string_list")
    return [item.strip().replace("\\", "/") for item in value]


def load_ignore_policy(repo_root: Path) -> SourceGraphIgnorePolicy:
    """Load repository additions; fail closed on malformed policy data."""

    path = ignore_config_path(repo_root)
    if not path.exists():
        return SourceGraphIgnorePolicy(frozenset(DEFAULT_EXCLUDE_DIR_NAMES), ())
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise SourceGraphError(f"source_graph_ignore_invalid:{path}:regular_file_required")
        if info.st_size > MAX_POLICY_BYTES:
            raise SourceGraphError(f"source_graph_ignore_invalid:{path}:too_large")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceGraphError(f"source_graph_ignore_invalid:{path}:{exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_id") not in {
        IGNORE_SCHEMA_ID, POLICY_SCHEMA_ID,
    }:
        raise SourceGraphError(f"source_graph_ignore_invalid:{path}:schema_id")
    is_legacy = payload.get("schema_id") == IGNORE_SCHEMA_ID
    revision = 0 if is_legacy else payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        if not is_legacy:
            raise SourceGraphError(f"source_graph_ignore_invalid:{path}:revision")
        revision = 0
    extra_dirs = _string_list(payload.get("exclude_dirs", []), field="exclude_dirs", config_path=path)
    extra_globs = _string_list(payload.get("exclude_globs", []), field="exclude_globs", config_path=path)
    disabled_languages = _string_list(
        payload.get("disabled_languages", []),
        field="disabled_languages",
        config_path=path,
    )
    if any("/" in item or item in {".", ".."} for item in extra_dirs):
        raise SourceGraphError(f"source_graph_ignore_invalid:{path}:exclude_dirs_must_be_basenames")
    if any(item.startswith("/") or item == ".." or item.startswith("../") for item in extra_globs):
        raise SourceGraphError(f"source_graph_ignore_invalid:{path}:exclude_globs_must_be_relative")
    unknown_languages = set(disabled_languages) - set(sglanguages.LANGUAGE_BY_ID)
    if unknown_languages:
        raise SourceGraphError(
            f"source_graph_ignore_invalid:{path}:unknown_languages:"
            f"{','.join(sorted(unknown_languages))}"
        )
    return SourceGraphIgnorePolicy(
        frozenset((*DEFAULT_EXCLUDE_DIR_NAMES, *extra_dirs)),
        tuple(dict.fromkeys(extra_globs)),
        frozenset(disabled_languages),
        revision,
        True,
    )


def source_graph_policy_view(repo_root: Path) -> dict[str, Any]:
    """Return the bounded repository language policy used by discovery."""

    policy = load_ignore_policy(repo_root)
    languages = sglanguages.public_registry(disabled_languages=policy.disabled_languages)
    return {
        "ok": True,
        "schema_id": POLICY_SCHEMA_ID,
        "revision": policy.revision,
        "configured": policy.configured,
        "language_count": len(languages),
        "enabled_count": sum(1 for row in languages if row["enabled"]),
        "languages": languages,
        "exclude_dirs": sorted(policy.exclude_dirs - DEFAULT_EXCLUDE_DIR_NAMES),
        "exclude_globs": list(policy.exclude_globs),
    }


def update_language_policy(
    repo_root: Path,
    *,
    language_changes: dict[str, bool],
    expected_revision: int,
) -> dict[str, Any]:
    """Atomically apply bounded per-language switches with optimistic locking."""

    root = repo_root.resolve()
    if not isinstance(language_changes, dict) or not language_changes:
        raise SourceGraphError("source_graph_policy_language_changes_required")
    unknown = set(language_changes) - set(sglanguages.LANGUAGE_BY_ID)
    if unknown:
        raise SourceGraphError(f"source_graph_policy_unknown_language:{','.join(sorted(unknown))}")
    if any(not isinstance(value, bool) for value in language_changes.values()):
        raise SourceGraphError("source_graph_policy_language_values_must_be_boolean")
    current = load_ignore_policy(root)
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision != current.revision
    ):
        raise SourceGraphError("source_graph_policy_revision_conflict")

    disabled = set(current.disabled_languages)
    for language, enabled in language_changes.items():
        if enabled:
            disabled.discard(language)
        else:
            disabled.add(language)
    revision = current.revision + 1
    payload = {
        "schema_id": POLICY_SCHEMA_ID,
        "revision": revision,
        "exclude_dirs": sorted(current.exclude_dirs - DEFAULT_EXCLUDE_DIR_NAMES),
        "exclude_globs": list(current.exclude_globs),
        "disabled_languages": sorted(disabled),
    }
    path = ignore_config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise SourceGraphError("source_graph_policy_regular_file_required")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return source_graph_policy_view(root)


def _glob_ignored(relative_path: str, patterns: tuple[str, ...], *, is_dir: bool = False) -> bool:
    relative_path = relative_path.strip("/")
    for pattern in patterns:
        normalized = pattern.strip().strip("/")
        if not normalized:
            continue
        if fnmatch.fnmatchcase(relative_path, normalized):
            return True
        # ``foo/**`` must prune ``foo`` before os.walk descends into it.
        if is_dir and normalized.endswith("/**"):
            base = normalized[:-3].rstrip("/")
            if relative_path == base or relative_path.startswith(f"{base}/"):
                return True
    return False


def iter_source_files(repo_root: Path) -> list[Path]:
    repo_root = repo_root.resolve()
    policy = load_ignore_policy(repo_root)
    out: list[Path] = []
    indexed_extensions = policy.indexed_extensions
    for current, dirnames, filenames in os.walk(repo_root, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for dirname in dirnames:
            candidate = current_path / dirname
            rel = candidate.relative_to(repo_root).as_posix()
            if dirname in policy.exclude_dirs or dirname.endswith(".egg-info"):
                continue
            if _glob_ignored(rel, policy.exclude_globs, is_dir=True):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = sorted(kept_dirs)
        for filename in sorted(filenames):
            path = current_path / filename
            if path.suffix.lower() not in indexed_extensions:
                continue
            rel = path.relative_to(repo_root).as_posix()
            if _glob_ignored(rel, policy.exclude_globs):
                continue
            out.append(path)
    return sorted(set(out))


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
    compaction_performed: bool = False
    database_bytes_before_compaction: int = 0
    database_bytes_after_compaction: int = 0
    freelist_ratio_before_compaction: float = 0.0
    compaction_error: str = ""
    compaction_recommended: bool = False
    compaction_deferred_reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "repo_root": self.repo_root, "db_path": self.db_path,
            "incremental": self.incremental, "files_seen": self.files_seen,
            "files_changed": self.files_changed, "files_unchanged": self.files_unchanged,
            "files_removed": self.files_removed, "entities_written": self.entities_written,
            "edges_written": self.edges_written, "errors": self.errors,
            "build_revision": self.build_revision, "finished_at": self.finished_at,
            "compaction_performed": self.compaction_performed,
            "database_bytes_before_compaction": self.database_bytes_before_compaction,
            "database_bytes_after_compaction": self.database_bytes_after_compaction,
            "freelist_ratio_before_compaction": self.freelist_ratio_before_compaction,
            "compaction_error": self.compaction_error,
            "compaction_recommended": self.compaction_recommended,
            "compaction_deferred_reason": self.compaction_deferred_reason,
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


def _write_extraction(
    conn: sqlite3.Connection,
    extraction: sgast.FileExtraction,
    *,
    file_size: int = -1,
    mtime_ns: int = -1,
) -> tuple[int, int]:
    """Persist one extraction and return the rows actually inserted.

    Extractors may conservatively emit the same edge more than once.  The
    database writer deliberately deduplicates those identities, so callers
    must report the inserted population rather than the pre-dedup candidate
    population.
    """
    conn.execute(
        "INSERT INTO files(file_path, language, status, source_hash, file_size, mtime_ns, "
        "indexed_at, build_revision) VALUES (?,?,?,?,?,?,?,?)",
        (extraction.file_path, extraction.language, extraction.status,
         extraction.source_hash, file_size, mtime_ns, _now_iso(), BUILD_REVISION),
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
    seen_edges: set[tuple[Any, ...]] = set()
    for edge in extraction.edges:
        identity = (
            edge.file_path, edge.kind, edge.src_qualname, edge.dst_name,
            edge.dst_qualname, edge.line, edge.evidence_label, edge.extractor,
            edge.confidence, edge.source_hash, edge.build_revision,
        )
        if identity in seen_edges:
            continue
        seen_edges.add(identity)
        conn.execute(
            "INSERT INTO edges(file_path, kind, src_qualname, dst_name, dst_qualname, line, "
            "evidence_label, extractor, confidence, source_hash, build_revision) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (edge.file_path, edge.kind, edge.src_qualname, edge.dst_name, edge.dst_qualname,
             edge.line, edge.evidence_label, edge.extractor, edge.confidence,
             edge.source_hash, edge.build_revision),
        )
    return len(extraction.entities), len(seen_edges)


def _resolve_cpp_cross_file_edges(conn: sqlite3.Connection) -> int:
    """Resolve lexical-language targets only with unique bounded evidence.

    Lexical extraction proves that call syntax exists but not which overload or
    translation unit owns the callee.  A unique canonical entity name is safe
    to bind as inferred evidence. When a name is globally ambiguous, an exact
    import/include-to-file match may disambiguate one candidate. Zero or
    multiple candidates remain visibly unresolved. Recomputing after every
    build also clears targets made stale by a rename/delete during an
    incremental refresh.
    """

    resolvable_extractors = (
        sgast.CPP_LEXICAL_EXTRACTOR_ID,
        sgast.POLYGLOT_LEXICAL_EXTRACTOR_ID,
        sgast.TREE_SITTER_JS_TS_EXTRACTOR_ID,
    )
    placeholders = ",".join("?" for _ in resolvable_extractors)
    conn.execute(
        f"UPDATE edges SET dst_qualname=NULL WHERE extractor IN ({placeholders}) "
        "AND kind IN ('calls','inherits')",
        resolvable_extractors,
    )
    resolved = 0
    unresolved_with_language = conn.execute(
        f"SELECT e.id, e.file_path, e.dst_name, f.language FROM edges e "
        "JOIN files f ON f.file_path=e.file_path "
        f"WHERE e.extractor IN ({placeholders}) "
        "AND e.kind IN ('calls','inherits') AND e.dst_qualname IS NULL "
        "ORDER BY e.id",
        resolvable_extractors,
    ).fetchall()
    candidates_by_language_and_name: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for edge in unresolved_with_language:
        source_language = str(edge["language"])
        name = str(edge["dst_name"])
        key = (source_language, name)
        if key not in candidates_by_language_and_name:
            candidates = conn.execute(
                "SELECT e.file_path, e.qualname, f.language FROM entities e "
                "JOIN files f ON f.file_path=e.file_path WHERE e.name=? AND "
                "e.kind IN ('function','method','class','struct','union','enum') "
                "ORDER BY e.file_path, e.qualname",
                (name,),
            ).fetchall()
            candidates_by_language_and_name[key] = [
                candidate for candidate in candidates
                if _resolution_languages_compatible(
                    source_language, str(candidate["language"])
                )
            ]
        candidates = candidates_by_language_and_name[key]
        if len(candidates) != 1:
            continue
        cur = conn.execute(
            "UPDATE edges SET dst_qualname=? WHERE id=? AND dst_qualname IS NULL",
            (candidates[0]["qualname"], edge["id"]),
        )
        resolved += int(cur.rowcount or 0)

    # Second pass: imported-file evidence can safely narrow a globally
    # ambiguous short name. This remains INFERRED authority; the resolver only
    # fills the canonical target identity already carried by the edge.
    unresolved = conn.execute(
        f"SELECT e.id, e.file_path, e.dst_name, f.language FROM edges e "
        "JOIN files f ON f.file_path=e.file_path "
        f"WHERE e.extractor IN ({placeholders}) "
        "AND e.kind IN ('calls','inherits') AND e.dst_qualname IS NULL "
        "ORDER BY id",
        resolvable_extractors,
    ).fetchall()
    imports_by_file: dict[str, list[str]] = {}
    candidates_by_name: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for edge in unresolved:
        file_path = str(edge["file_path"])
        if file_path not in imports_by_file:
            imports_by_file[file_path] = [
                str(row["dst_name"])
                for row in conn.execute(
                    "SELECT dst_name FROM edges WHERE file_path=? AND kind='imports' "
                    "ORDER BY id",
                    (file_path,),
                )
            ]
        imports = imports_by_file[file_path]
        if not imports:
            continue
        name = str(edge["dst_name"])
        source_language = str(edge["language"])
        candidate_key = (source_language, name)
        if candidate_key not in candidates_by_name:
            rows = conn.execute(
                "SELECT e.file_path, e.qualname, f.language FROM entities e "
                "JOIN files f ON f.file_path=e.file_path WHERE e.name=? AND "
                "e.kind IN ('function','method','class','struct','union','enum') "
                "ORDER BY e.file_path, e.qualname",
                (name,),
            ).fetchall()
            candidates_by_name[candidate_key] = [
                row for row in rows
                if _resolution_languages_compatible(
                    source_language, str(row["language"])
                )
            ]
        candidates = [
            row for row in candidates_by_name[candidate_key]
            if any(
                _import_target_matches_file(target, str(row["file_path"]))
                for target in imports
            )
        ]
        if len(candidates) != 1:
            continue
        cur = conn.execute(
            "UPDATE edges SET dst_qualname=? WHERE id=? AND dst_qualname IS NULL",
            (candidates[0]["qualname"], edge["id"]),
        )
        resolved += int(cur.rowcount or 0)
    return resolved


def _resolution_languages_compatible(source: str, target: str) -> bool:
    """Conservatively bound lexical resolution to interoperable families."""

    if source == target:
        return True
    return {source, target} <= {"javascript", "typescript"}


def _import_target_matches_file(target: str, file_path: str) -> bool:
    """Match an observed import/include target to one candidate source file."""

    normalized_target = str(target).strip().replace("\\", "/")
    normalized_target = normalized_target.removeprefix("./")
    while normalized_target.startswith("../"):
        normalized_target = normalized_target[3:]
    target_path = Path(normalized_target)
    if target_path.suffix.casefold() in _INDEXED_EXTENSION_SET:
        normalized_target = target_path.with_suffix("").as_posix()
    normalized_target = normalized_target.replace("::", "/").replace(".", "/")
    normalized_file = str(file_path).strip().replace("\\", "/")
    target_path = Path(normalized_target)
    file_path_obj = Path(normalized_file)
    target_stem = target_path.stem.casefold()
    file_stem = file_path_obj.stem.casefold()
    # A bare include/module name may match by stem. Once the import carries a
    # directory component, discarding that path would turn ``../b/math`` into
    # a match for both ``a/math.ts`` and ``b/math.ts`` and destroy the exact
    # disambiguating evidence.
    if "/" not in normalized_target and target_stem and target_stem == file_stem:
        return True
    target_no_suffix = target_path.with_suffix("").as_posix().casefold().strip("/")
    file_no_suffix = file_path_obj.with_suffix("").as_posix().casefold().strip("/")
    return bool(
        target_no_suffix
        and (
            file_no_suffix.endswith(target_no_suffix)
            or file_no_suffix.endswith(f"{target_no_suffix}/index")
            or file_no_suffix.endswith(f"{target_no_suffix}/mod")
        )
    )


def _build_index_locked(repo_root: Path, *, db_path: Path | None = None, incremental: bool = True) -> BuildReport:
    repo_root = repo_root.resolve()
    resolved_db_path = db_path or resolve_db_path(repo_root)
    conn = connect(resolved_db_path)
    files_on_disk = iter_source_files(repo_root)
    seen_rel: set[str] = set()
    changed = unchanged = removed = entities_written = edges_written = 0
    errors: list[dict[str, str]] = []
    compaction_performed = False
    compaction_error = ""
    bytes_before_compaction = 0
    bytes_after_compaction = 0
    freelist_ratio = 0.0
    compaction_recommended = False
    compaction_deferred_reason = ""
    try:
        # Read the prior generation before extraction, but do not begin the
        # write transaction until every source file has been parsed.  Large
        # repositories can spend minutes in AST/lexical extraction; keeping a
        # rollback journal alive for that whole interval unnecessarily widens
        # the writer lock and makes concurrent worker context queries fragile.
        existing = {
            row["file_path"]: (
                row["source_hash"], row["build_revision"],
                int(row["file_size"]), int(row["mtime_ns"]),
            )
            for row in conn.execute(
                "SELECT file_path, source_hash, build_revision, file_size, mtime_ns FROM files"
            )
        }
        existing_extractors: dict[str, set[str]] = {}
        for row in conn.execute(
            "SELECT file_path, extractor FROM entities "
            "UNION SELECT file_path, extractor FROM edges"
        ):
            existing_extractors.setdefault(str(row["file_path"]), set()).add(
                str(row["extractor"])
            )
        pending_extractions: list[tuple[sgast.FileExtraction, int, int]] = []
        pending_stat_updates: list[tuple[int, int, str]] = []
        expected_extractors_by_suffix: dict[str, frozenset[str]] = {}
        for path in files_on_disk:
            rel = path.relative_to(repo_root).as_posix()
            seen_rel.add(rel)
            try:
                path_stat = path.stat()
                file_size = int(path_stat.st_size)
                mtime_ns = int(path_stat.st_mtime_ns)
            except OSError:
                file_size = -1
                mtime_ns = -1
            prior = existing.get(rel)
            capability_key = path.suffix.casefold()
            expected_extractors = expected_extractors_by_suffix.get(capability_key)
            if expected_extractors is None:
                expected_extractors = sgast.expected_extractor_ids(path)
                expected_extractors_by_suffix[capability_key] = expected_extractors
            if (
                incremental and prior is not None
                and prior[1] == BUILD_REVISION
                and file_size >= 0 and mtime_ns >= 0
                and prior[2] == file_size and prior[3] == mtime_ns
                and existing_extractors.get(rel, set()) == expected_extractors
            ):
                unchanged += 1
                continue
            extraction = sgast.extract_file(repo_root, path, build_revision=BUILD_REVISION)
            seen_rel.discard(rel)
            seen_rel.add(extraction.file_path)
            prior = existing.get(extraction.file_path)
            expected_extractors = {
                item.extractor for item in (*extraction.entities, *extraction.edges)
            }
            if (
                incremental and prior is not None
                and prior[0] == extraction.source_hash
                and prior[1] == BUILD_REVISION
                and existing_extractors.get(extraction.file_path, set())
                == expected_extractors
            ):
                unchanged += 1
                if prior[2] != file_size or prior[3] != mtime_ns:
                    pending_stat_updates.append((file_size, mtime_ns, extraction.file_path))
                continue
            pending_extractions.append((extraction, file_size, mtime_ns))

        with conn:
            if pending_stat_updates:
                conn.executemany(
                    "UPDATE files SET file_size=?, mtime_ns=? WHERE file_path=?",
                    pending_stat_updates,
                )
            for extraction, file_size, mtime_ns in pending_extractions:
                _invalidate_file(conn, extraction.file_path)
                inserted_entities, inserted_edges = _write_extraction(
                    conn, extraction, file_size=file_size, mtime_ns=mtime_ns,
                )
                changed += 1
                entities_written += inserted_entities
                edges_written += inserted_edges
                if extraction.status != "ok" and extraction.error:
                    errors.append({
                        "file": extraction.file_path, "status": extraction.status,
                        "error": extraction.error,
                    })
            for rel in list(existing):
                if rel not in seen_rel:
                    _invalidate_file(conn, rel)
                    removed += 1
            if changed or removed:
                _resolve_cpp_cross_file_edges(conn)
            sginsights.materialize_git_metrics(
                conn, repo_root, sorted(seen_rel), limit=10000,
            )
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
        bytes_before_compaction = resolved_db_path.stat().st_size
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        freelist_ratio = (freelist_count / page_count) if page_count else 0.0
        compaction_recommended = bool(
            bytes_before_compaction >= SOURCE_GRAPH_COMPACT_MIN_BYTES
            and freelist_ratio >= SOURCE_GRAPH_COMPACT_MIN_FREELIST_RATIO
        )
        if compaction_recommended:
            # VACUUM takes an exclusive SQLite lock and rewrites the complete
            # database. Running it synchronously after every qualifying live
            # refresh blocked all manager/worker readers and made a killed
            # refresh capable of stranding a hot rollback journal. Report the
            # maintenance need truthfully; compaction belongs to an explicit
            # quiescent maintenance operation, never the query-serving build.
            compaction_deferred_reason = "live_generation_in_use"
        bytes_after_compaction = resolved_db_path.stat().st_size
    finally:
        conn.close()
    return BuildReport(
        repo_root=str(repo_root), db_path=str(resolved_db_path), incremental=incremental,
        files_seen=len(files_on_disk), files_changed=changed, files_unchanged=unchanged,
        files_removed=removed, entities_written=entities_written, edges_written=edges_written,
        errors=errors, build_revision=BUILD_REVISION, finished_at=_now_iso(),
        compaction_performed=compaction_performed,
        database_bytes_before_compaction=bytes_before_compaction,
        database_bytes_after_compaction=bytes_after_compaction,
        freelist_ratio_before_compaction=freelist_ratio,
        compaction_error=compaction_error,
        compaction_recommended=compaction_recommended,
        compaction_deferred_reason=compaction_deferred_reason,
    )


def build_index(repo_root: Path, *, db_path: Path | None = None, incremental: bool = True) -> BuildReport:
    """Build with a repository-local, cross-process single-writer lease."""

    repo_root = repo_root.resolve()
    with index_write_lease(repo_root) as acquired:
        if not acquired:
            raise SourceGraphBuildInProgressError(
                f"source_graph_build_in_progress:{repo_root}"
            )
        return _build_index_locked(repo_root, db_path=db_path, incremental=incremental)


# ---------------------------------------------------------------------------
# Query surface: find / func / body / struct / context / summary
# ---------------------------------------------------------------------------

def _fts_phrase(term: str) -> str:
    cleaned = term.replace('"', '""').strip()
    return f'"{cleaned}"*' if cleaned else '""'


def _query_tokens(term: str) -> list[str]:
    """Normalize code identifiers and qualified names into FTS tokens.

    The transformation is deterministic and deliberately syntax-light: it
    splits camel/Pascal case and common namespace/path separators without
    guessing synonyms. Exact phrase lookup still runs first, so normalization
    only broadens a query after the highest-precision pass misses.
    """

    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", term or "")
    expanded = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", expanded)
    # Preserve Unicode words (including Georgian); only punctuation and
    # namespace/path separators become boundaries.
    expanded = "".join(char if char.isalnum() else " " for char in expanded)
    tokens: list[str] = []
    seen: set[str] = set()
    for token in expanded.casefold().split():
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


def _fts_terms(term: str, *, operator: str) -> str:
    """Build a safe token query without treating whitespace as one phrase.

    FTS5 phrase-prefix lookup is still the first and highest-precision pass.
    The token passes repair the common natural-language query case where a
    manager names several related symbols that do not occur contiguously in a
    single indexed field.
    """

    tokens = _query_tokens(term)
    escaped = [f'"{token.replace(chr(34), chr(34) * 2)}"*' for token in tokens]
    return f" {operator} ".join(escaped)


def _looks_like_identifier_query(term: str) -> bool:
    return bool(
        any(separator in term for separator in ("_", "::", ".", "/", "\\"))
        or re.search(r"[a-z0-9][A-Z]", term)
    )


def find(conn: sqlite3.Connection, term: str, *, limit: int = 24) -> list[dict[str, Any]]:
    term = (term or "").strip()
    if not term:
        return []
    limit = max(1, min(int(limit), MAX_BUDGET_ROWS))
    rows = []
    expressions = [_fts_phrase(term)]
    if len(_query_tokens(term)) > 1:
        expressions.append(_fts_terms(term, operator="AND"))
        if not _looks_like_identifier_query(term):
            expressions.append(_fts_terms(term, operator="OR"))
    for expression in expressions:
        try:
            rows = conn.execute(
            "SELECT e.file_path, e.kind, e.name, e.qualname, e.line_start, e.line_end, "
            "e.signature, e.evidence_label, e.confidence FROM entities_fts f "
            "JOIN entities e ON e.id = f.entity_id WHERE entities_fts MATCH ? "
            "ORDER BY CASE WHEN lower(e.qualname)=lower(?) THEN 0 "
            "WHEN lower(e.name)=lower(?) THEN 1 "
            "WHEN lower(e.name) LIKE lower(?) THEN 2 ELSE 3 END, "
            "CASE WHEN e.kind IN ('function','method','class','struct','union','enum') "
            "THEN 0 WHEN e.kind='file' THEN 1 ELSE 2 END, "
            # Symbol identity is stronger evidence than incidental signature
            # or path text.  Keep the decomposition explicit so generated/data
            # paths cannot outrank an exact code authority merely by repeating
            # the query term in their filename.
            "bm25(entities_fts, 10.0, 6.0, 2.0, 0.5), "
            "e.file_path, e.line_start LIMIT ?",
                (expression, term, term, f"{term}%", limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if rows:
            break
    if not rows:
        like = f"%{term}%"
        rows = conn.execute(
            "SELECT file_path, kind, name, qualname, line_start, line_end, signature, "
            "evidence_label, confidence FROM entities WHERE name LIKE ? OR qualname LIKE ? "
            "ORDER BY CASE WHEN lower(qualname)=lower(?) THEN 0 "
            "WHEN lower(name)=lower(?) THEN 1 ELSE 2 END, "
            "confidence DESC, file_path, line_start LIMIT ?",
            (like, like, term, term, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def bodygrep_query(
    repo_root: Path,
    term: str,
    budget: int = 64,
    *,
    target: str | None = None,
) -> dict[str, Any]:
    """Search literal/body text only inside canonical indexed source files.

    The graph stores symbols and edges, not whole file bodies.  This bounded
    mode closes that deliberate storage gap without shelling out to grep or
    silently scanning ignored/unindexed paths.  It reports scan limits so a
    zero hit remains truthful rather than looking like full-repository proof.
    """

    term = (term or "").strip()
    if not term:
        return {
            "mode": "bodygrep", "query": term, "budget": 0, "matches": [],
            "candidate_files": [], "files_scanned": 0, "bytes_scanned": 0,
            "scan_truncated": False, "truncated": False,
        }
    budget = max(1, min(int(budget), MAX_BUDGET_ROWS))
    byte_cap = max(512, budget * 512)
    scan_file_cap = max(64, min(4000, budget * 32))
    scan_byte_cap = max(1_048_576, min(32 * 1_048_576, budget * 262_144))
    normalized_target = ""
    if target is not None:
        normalized_target = str(target).strip().replace("\\", "/").strip("/")
        target_parts = Path(normalized_target).parts
        if (
            not normalized_target
            or "\x00" in normalized_target
            or Path(str(target)).is_absolute()
            or ".." in target_parts
        ):
            raise SourceGraphError("bodygrep_target_invalid")

    conn = connect(resolve_db_path(repo_root), read_only=True)
    try:
        if normalized_target:
            exact = conn.execute(
                "SELECT 1 FROM files WHERE file_path=? LIMIT 1",
                (normalized_target,),
            ).fetchone()
            if exact:
                query = "SELECT file_path FROM files WHERE file_path=? ORDER BY file_path LIMIT ?"
                params: tuple[Any, ...] = (normalized_target, scan_file_cap + 1)
            else:
                escaped = (
                    normalized_target.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                query = (
                    "SELECT file_path FROM files WHERE file_path LIKE ? ESCAPE '\\' "
                    "ORDER BY file_path LIMIT ?"
                )
                params = (f"{escaped}/%", scan_file_cap + 1)
        else:
            query = "SELECT file_path FROM files ORDER BY file_path LIMIT ?"
            params = (scan_file_cap + 1,)
        paths = [
            str(row["file_path"])
            for row in conn.execute(query, params)
        ]
    finally:
        conn.close()

    scan_truncated = len(paths) > scan_file_cap
    paths = paths[:scan_file_cap]
    repo_root = repo_root.resolve()
    needle = term.casefold()
    matches: list[dict[str, Any]] = []
    files_scanned = 0
    bytes_scanned = 0
    for file_path in paths:
        target = (repo_root / file_path).resolve()
        if not target.is_relative_to(repo_root):
            continue
        try:
            raw = target.read_bytes()
        except OSError:
            continue
        if bytes_scanned + len(raw) > scan_byte_cap:
            scan_truncated = True
            break
        bytes_scanned += len(raw)
        files_scanned += 1
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if needle not in line.casefold():
                continue
            matches.append({
                "file_path": file_path,
                "kind": "body_match",
                "name": term,
                "qualname": f"{file_path}:{line_number}",
                "line_start": line_number,
                "line_end": line_number,
                "signature": line.strip()[:320],
                "evidence_label": "EXTRACTED",
                "confidence": 1.0,
            })
            if len(matches) >= budget:
                scan_truncated = True
                break
        if len(matches) >= budget:
            break
    rows, output_truncated = _bounded_rows(matches, budget, byte_cap)
    payload = {
        "mode": "bodygrep", "query": term, "budget": budget,
        "matches": rows,
        "candidate_files": _candidate_files(rows, limit=min(16, budget)),
        "files_scanned": files_scanned,
        "bytes_scanned": bytes_scanned,
        "scan_file_cap": scan_file_cap,
        "scan_byte_cap": scan_byte_cap,
        "scan_truncated": scan_truncated,
        "target": normalized_target or None,
        "truncated": bool(output_truncated or scan_truncated),
    }
    return _fit_payload_bytes(payload, byte_cap)


def body_query(repo_root: Path, name: str, budget: int = 64) -> dict[str, Any]:
    """Return the bounded body of one exact indexed symbol."""

    budget = max(1, min(int(budget), MAX_BUDGET_ROWS))
    conn = connect(resolve_db_path(repo_root), read_only=True)
    try:
        match = body(conn, repo_root, name)
    finally:
        conn.close()
    matches = [match] if match else []
    return _fit_payload_bytes({
        "mode": "body", "query": name, "budget": budget,
        "matches": matches,
        "candidate_files": _candidate_files(matches, limit=1),
        "truncated": False,
    }, max(512, budget * 512))


def _bounded_file_preview(
    repo_root: Path, file_path: str, *, max_bytes: int,
) -> dict[str, Any]:
    """Read one exact repository file without exposing an unbounded body."""

    try:
        root = repo_root.resolve()
        target = (root / file_path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return {}
        with target.open("rb") as stream:
            data = stream.read(max_bytes + 1)
    except OSError:
        return {}
    preview = data[:max_bytes]
    return {
        "source_preview": preview.decode("utf-8", errors="replace"),
        "source_preview_bytes": len(preview),
        "source_preview_truncated": len(data) > max_bytes,
    }


def file_query(repo_root: Path, file_path: str, budget: int = 64) -> dict[str, Any]:
    """Return exact metadata and a bounded preview for one indexed path."""

    budget = max(1, min(int(budget), MAX_BUDGET_ROWS))
    conn = connect(resolve_db_path(repo_root), read_only=True)
    try:
        payload = context(conn, file_path)
    finally:
        conn.close()
    if payload.get("found"):
        entity_limit = max(1, min(16, budget // 2))
        edge_limit = max(1, min(16, budget // 2))
        payload["entities"] = payload["entities"][:entity_limit]
        payload["edges"] = payload["edges"][:edge_limit]
        payload.update(_bounded_file_preview(
            repo_root, file_path,
            max_bytes=min(4096, max(1024, budget * 128)),
        ))
        file_match = {
            **dict(payload.get("file") or {}),
            "kind": "file",
            "name": Path(file_path).name,
            "qualname": file_path,
            "line_start": 1,
            "line_end": 1,
        }
    else:
        file_match = None
    return _fit_payload_bytes({
        "mode": "file", "query": file_path, "budget": budget,
        "matches": [file_match] if file_match else [],
        "contexts": [payload] if payload.get("found") else [],
        "candidate_files": [file_path] if payload.get("found") else [],
        "truncated": False,
    }, max(4096, budget * 768))


def function_query(repo_root: Path, name: str, budget: int = 64) -> dict[str, Any]:
    """Return exact function/method authorities, including bounded bodies."""

    budget = max(1, min(int(budget), MAX_BUDGET_ROWS))
    conn = connect(resolve_db_path(repo_root), read_only=True)
    try:
        matches = func(conn, name, limit=budget)
        for match in matches:
            match["source"] = _source_snippet(repo_root, match)
    finally:
        conn.close()
    return _fit_payload_bytes({
        "mode": "function", "query": name, "budget": budget,
        "matches": matches,
        "candidate_files": _candidate_files(matches, limit=min(16, budget)),
        "truncated": len(matches) >= budget,
    }, max(512, budget * 512))


def class_query(repo_root: Path, name: str, budget: int = 64) -> dict[str, Any]:
    """Return exact class/struct/enum authorities, including bounded bodies."""

    budget = max(1, min(int(budget), MAX_BUDGET_ROWS))
    conn = connect(resolve_db_path(repo_root), read_only=True)
    try:
        matches = struct(conn, name, limit=budget)
        for match in matches:
            match["source"] = _source_snippet(repo_root, match)
    finally:
        conn.close()
    return _fit_payload_bytes({
        "mode": "class", "query": name, "budget": budget,
        "matches": matches,
        "candidate_files": _candidate_files(matches, limit=min(16, budget)),
        "truncated": len(matches) >= budget,
    }, max(512, budget * 512))


def deps_query(repo_root: Path, query: str, budget: int = 64) -> dict[str, Any]:
    """Expose symbol dependencies without duplicating the ``trace`` payload.

    ``trace`` is an execution-call view.  ``deps`` instead partitions calls,
    imports and inheritance edges around the selected authorities so an agent
    can choose the next boundary without paying for an identical response.
    """

    budget = max(1, min(int(budget), MAX_BUDGET_ROWS))
    conn = connect(resolve_db_path(repo_root), read_only=True)
    try:
        matches = find(conn, query, limit=budget)
        qualnames = list(dict.fromkeys(
            str(row.get("qualname") or "") for row in matches
            if row.get("qualname")
        ))
        if not qualnames:
            return {
                "mode": "deps", "query": query, "budget": budget,
                "direct_matches": [], "dependency_edges": [],
                "dependent_edges": [], "candidate_files": [],
                "dependency_kinds": ["calls", "imports", "inherits"],
                "truncated": False,
            }
        placeholders = ",".join("?" for _ in qualnames)
        edge_select = (
            "SELECT file_path, kind, src_qualname, dst_name, dst_qualname, line, "
            "evidence_label, confidence FROM edges "
        )
        outgoing = [dict(row) for row in conn.execute(
            edge_select
            + f"WHERE kind IN ('calls','imports','inherits') "
            f"AND src_qualname IN ({placeholders}) "
            "ORDER BY confidence DESC, kind, file_path, line LIMIT ?",
            (*qualnames, budget + 1),
        )]
        incoming = [dict(row) for row in conn.execute(
            edge_select
            + f"WHERE kind IN ('calls','inherits') "
            f"AND dst_qualname IN ({placeholders}) "
            "ORDER BY confidence DESC, kind, file_path, line LIMIT ?",
            (*qualnames, budget + 1),
        )]
        truncated = len(outgoing) > budget or len(incoming) > budget
        return _fit_payload_bytes({
            "mode": "deps", "query": query, "budget": budget,
            "direct_matches": matches[:budget],
            "dependency_edges": outgoing[:budget],
            "dependent_edges": incoming[:budget],
            "candidate_files": _candidate_files(matches, limit=min(16, budget)),
            "dependency_kinds": ["calls", "imports", "inherits"],
            "truncated": truncated,
        }, max(512, budget * 768))
    finally:
        conn.close()


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
        "evidence_label, confidence FROM entities WHERE kind IN "
        "('class','struct','union','enum','namespace') AND name = ? LIMIT ?",
        (name, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def body(conn: sqlite3.Connection, repo_root: Path, name: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT file_path, kind, name, qualname, line_start, line_end, signature "
        "FROM entities WHERE kind IN "
        "('function','method','class','struct','union','enum','namespace') "
        "AND (name = ? OR qualname = ?) "
        "ORDER BY CASE WHEN qualname=? THEN 0 ELSE 1 END, "
        "confidence DESC, file_path, line_start LIMIT 1",
        (name, name, name),
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
        {**dict(row), "file_path": file_path} for row in conn.execute(
            "SELECT kind, name, qualname, line_start, line_end, signature, evidence_label, "
            "confidence FROM entities WHERE file_path = ? ORDER BY line_start LIMIT ?",
            (file_path, MAX_BUDGET_ROWS),
        )
    ]
    edges = [
        {**dict(row), "file_path": file_path} for row in conn.execute(
            "SELECT kind, src_qualname, dst_name, dst_qualname, line, evidence_label, "
            "confidence FROM edges WHERE file_path = ? ORDER BY line LIMIT ?",
            (file_path, MAX_BUDGET_ROWS),
        )
    ]
    return {
        "file_path": file_path,
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
    by_language = {
        row["language"]: row["c"]
        for row in conn.execute("SELECT language, COUNT(*) c FROM files GROUP BY language")
    }
    last_build_row = conn.execute("SELECT value FROM meta WHERE key='last_build'").fetchone()
    return {
        "files": file_count, "entities": entity_count, "edges": edge_count,
        "entities_by_kind": by_kind, "edges_by_evidence_label": by_evidence,
        "files_by_status": by_status, "files_by_language": by_language,
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


def _fit_payload_bytes(payload: dict[str, Any], byte_cap: int) -> dict[str, Any]:
    """Deterministically trim nested optional evidence to the public byte cap."""

    protected_keys = {
        "mode",
        "query",
        "budget",
        "target",
        "query_tokens",
        "query_tokens_source",
        "candidate_files",
        "truncated",
    }

    def encoded_size() -> int:
        return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    while encoded_size() > byte_cap:
        lists: list[tuple[int, list[Any]]] = []
        strings: list[tuple[int, dict[str, Any], str]] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in protected_keys:
                        continue
                    if isinstance(item, str) and len(item) > 256:
                        strings.append((len(item), value, key))
                    else:
                        visit(item)
            elif isinstance(value, list):
                if value:
                    lists.append((len(json.dumps(value, ensure_ascii=False)), value))
                for item in value:
                    visit(item)

        visit(payload)
        if strings:
            _, owner, key = max(strings, key=lambda item: item[0])
            text = str(owner[key])
            owner[key] = text[: max(256, len(text) // 2)]
            payload["truncated"] = True
            continue
        if lists:
            _, target = max(lists, key=lambda item: item[0])
            del target[len(target) // 2:]
            payload["truncated"] = True
            continue
        break
    return payload


def _candidate_files(matches: list[dict[str, Any]], *, limit: int) -> list[str]:
    return sginsights.candidate_files(matches, limit=limit)


def _call_edges_for_files(
    conn: sqlite3.Connection, files: list[str], *, limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return sginsights.call_edges(conn, files, limit=limit)


def _source_snippet(repo_root: Path, row: dict[str, Any], *, max_chars: int = 4000) -> str:
    try:
        target = (repo_root / str(row["file_path"])).resolve()
        if not target.is_relative_to(repo_root.resolve()):
            return ""
        lines = target.read_text(encoding="utf-8").splitlines()
        start = max(0, int(row.get("line_start") or 1) - 1)
        end = max(start + 1, int(row.get("line_end") or start + 1))
        return "\n".join(lines[start:end])[:max_chars]
    except (KeyError, OSError, UnicodeDecodeError, ValueError, TypeError):
        return ""


def _query_payload(
    repo_root: Path,
    mode: str,
    query: str,
    budget: int,
    *,
    target: str | None = None,
) -> dict[str, Any]:
    budget = max(1, min(int(budget), MAX_BUDGET_ROWS))
    byte_cap = max(512, budget * 512)
    db_path = resolve_db_path(repo_root)
    conn = connect(db_path, read_only=True)
    try:
        lookup = str(target or query).strip()
        matches = find(conn, lookup, limit=budget)
        matches, truncated = _bounded_rows(matches, budget, byte_cap)
        files = _candidate_files(matches, limit=min(budget, 16))
        payload: dict[str, Any] = {
            "mode": mode, "query": query, "budget": budget, "matches": matches,
            "query_tokens": _query_tokens(lookup), "candidate_files": files,
            "truncated": truncated,
        }
        if target:
            payload["target"] = target
            payload["query_tokens_source"] = "target"
        if mode == "focus" and matches:
            payload.update(sginsights.focus_insights(
                conn, repo_root, matches, budget=budget,
            ))
        elif mode == "slice" and matches:
            payload.update(sginsights.slice_insights(
                conn, repo_root, matches, budget=budget,
            ))
            top = matches[0]
            payload["neighbors"] = neighbors(
                conn, top["qualname"], depth=1, limit=min(budget, 50)
            )["neighbors"]
        return _fit_payload_bytes(payload, byte_cap)
    finally:
        conn.close()


def focus(repo_root: Path, query: str, budget: int = 64) -> dict[str, Any]:
    return _query_payload(repo_root, "focus", query, budget)


def slice_(
    repo_root: Path,
    query: str,
    budget: int = 64,
    *,
    target: str | None = None,
) -> dict[str, Any]:
    return _query_payload(repo_root, "slice", query, budget, target=target)


def context_query(repo_root: Path, query: str, budget: int = 64) -> dict[str, Any]:
    """Return exact file context, resolving a semantic term to its top file."""

    budget = max(1, min(int(budget), MAX_BUDGET_ROWS))
    db_path = resolve_db_path(repo_root)
    conn = connect(db_path, read_only=True)
    try:
        exact = conn.execute("SELECT 1 FROM files WHERE file_path=?", (query,)).fetchone()
        matches = find(conn, query, limit=budget)
        files = [query] if exact else _candidate_files(matches, limit=min(8, budget))
        contexts: list[dict[str, Any]] = []
        remaining = budget
        for path in files[:4]:
            item = context(conn, path)
            per_file = max(1, min(8, remaining))
            item["entities"] = item["entities"][:per_file]
            item["edges"] = item["edges"][:per_file]
            for entity in item["entities"][: min(4, per_file)]:
                if entity.get("kind") in {"function", "method", "class", "struct"}:
                    entity["source"] = _source_snippet(
                        repo_root, {**entity, "file_path": path}, max_chars=800
                    )
            contexts.append(item)
            remaining -= len(item["entities"]) + len(item["edges"])
            if remaining <= 0:
                break
        rows, truncated = _bounded_rows(contexts, budget, max(4096, budget * 768))
        insights = sginsights.slice_insights(
            conn, repo_root, matches, budget=min(budget, 32),
        ) if matches else {}
        return _fit_payload_bytes({
            "mode": "context", "query": query, "budget": budget,
            "matches": matches[: min(budget, 8)], "contexts": rows,
            "candidate_files": files[:4],
            "insights": insights,
            "truncated": truncated,
        }, max(4096, budget * 768))
    finally:
        conn.close()


def trace(repo_root: Path, query: str, budget: int = 64) -> dict[str, Any]:
    """Build a compact bidirectional symbol/file call trace."""

    budget = max(1, min(int(budget), MAX_BUDGET_ROWS))
    conn = connect(resolve_db_path(repo_root), read_only=True)
    try:
        matches = find(conn, query, limit=budget)
        files = _candidate_files(matches, limit=min(16, budget))
        insights = sginsights.trace_insights(conn, matches, budget=budget)
        payload = {
            "mode": "trace", "query": query, "budget": budget,
            "direct_matches": matches, "candidate_files": files,
            **insights,
        }
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        payload["truncated"] = len(encoded) > max(512, budget * 768)
        if payload["truncated"]:
            payload["direct_matches"] = matches[: max(1, budget // 3)]
            payload["outgoing_calls"] = payload["outgoing_calls"][: max(1, budget // 3)]
            payload["incoming_calls"] = payload["incoming_calls"][: max(1, budget // 3)]
        return _fit_payload_bytes(payload, max(512, budget * 768))
    finally:
        conn.close()


def impact(repo_root: Path, query: str, budget: int = 64) -> dict[str, Any]:
    """Rank likely affected files from symbols and bidirectional call edges."""

    budget = max(1, min(int(budget), MAX_BUDGET_ROWS))
    conn = connect(resolve_db_path(repo_root), read_only=True)
    try:
        matches = find(conn, query, limit=budget)
        files = _candidate_files(matches, limit=min(24, budget))
        outgoing, incoming = _call_edges_for_files(conn, files, limit=budget * 2)
        rows: list[dict[str, Any]] = []
        for path in files:
            entity_count = int(conn.execute(
                "SELECT COUNT(*) FROM entities WHERE file_path=?", (path,)
            ).fetchone()[0])
            callers = sum(1 for edge in incoming if edge.get("callee_file") == path)
            callees = sum(1 for edge in outgoing if edge.get("caller_file") == path)
            stem = Path(path).stem
            test_rows = conn.execute(
                "SELECT file_path FROM files WHERE "
                "(file_path LIKE '%test%' OR file_path LIKE '%spec%') AND file_path LIKE ? "
                "ORDER BY file_path LIMIT 8",
                (f"%{stem}%",),
            ).fetchall()
            rows.append({
                "file_path": path, "entities": entity_count,
                "inbound_call_edges": callers, "outbound_call_edges": callees,
                "related_tests": [row["file_path"] for row in test_rows],
                "impact_score": callers * 3 + callees * 2 + min(entity_count, 20),
            })
        rows.sort(key=lambda row: (-row["impact_score"], row["file_path"]))
        insights = sginsights.impact_insights(
            conn, repo_root, matches, budget=budget,
        )
        return _fit_payload_bytes({
            "mode": "impact", "query": query, "budget": budget,
            "impacted_files": rows[:budget],
            "incoming_calls": incoming[:budget],
            **insights,
            "truncated": len(rows) > budget or len(incoming) > budget,
        }, max(512, budget * 768))
    finally:
        conn.close()


def analytics_query(
    repo_root: Path,
    mode: str,
    query: str,
    budget: int = 64,
) -> dict[str, Any]:
    """Run one repository-neutral analytic mode over canonical graph rows."""

    if mode not in sganalytics.ANALYTIC_MODES:
        raise SourceGraphError(f"invalid_analytic_mode:{mode}")
    budget = max(1, min(int(budget), MAX_BUDGET_ROWS))
    byte_cap = max(512, budget * 768)
    conn = connect(resolve_db_path(repo_root), read_only=True)
    try:
        matches = find(conn, query, limit=budget)
        payload = sganalytics.query(
            conn,
            repo_root,
            mode=mode,
            query_text=query,
            matches=matches,
            budget=budget,
        )
        payload["truncated"] = False
        return _fit_payload_bytes(payload, byte_cap)
    finally:
        conn.close()


def bundle(repo_root: Path, bundle_type: str, query: str, max_lines: int = 64) -> dict[str, Any]:
    if bundle_type not in SOURCE_GRAPH_BUNDLE_TYPES:
        raise SourceGraphError(f"invalid_bundle_type:{bundle_type}")
    budget = max(1, min(int(max_lines), MAX_BUDGET_ROWS))
    byte_cap = max(512, budget * 512)
    db_path = resolve_db_path(repo_root)
    conn = connect(db_path, read_only=True)
    try:
        matches = find(conn, query, limit=budget)
        sections: list[dict[str, Any]] = []
        remaining = budget
        seen_files: set[str] = set()
        for match in matches:
            if remaining <= 0:
                break
            if match["file_path"] in seen_files:
                continue
            seen_files.add(match["file_path"])
            ctx = context(conn, match["file_path"])
            ctx["entities"] = ctx["entities"][: max(1, remaining)]
            for entity in ctx["entities"][: min(4, remaining)]:
                if entity.get("kind") in {"function", "method", "class", "struct"}:
                    entity["source"] = _source_snippet(
                        repo_root, {**entity, "file_path": match["file_path"]}, max_chars=2400,
                    )
            sections.append(ctx)
            remaining -= len(ctx["entities"])
        files = list(seen_files)
        outgoing, incoming = _call_edges_for_files(conn, files, limit=min(budget, 40))
        sections, truncated = _bounded_rows(sections, budget, byte_cap)
        insights = sginsights.focus_insights(
            conn, repo_root, matches, budget=min(budget, 32),
        ) if matches else {}
        task_evidence: dict[str, Any] = {}
        if matches and bundle_type in {"bugfix", "feature", "refactor"}:
            task_evidence = sginsights.slice_insights(
                conn, repo_root, matches, budget=min(budget, 32),
            )
        if matches and bundle_type in {"feature", "refactor", "audit", "optimize"}:
            task_evidence["impact"] = sginsights.impact_insights(
                conn, repo_root, matches, budget=min(budget, 24),
            )
        return _fit_payload_bytes({
            "mode": "bundle", "bundle_type": bundle_type, "query": query,
            "budget": budget, "sections": sections,
            "outgoing_calls": outgoing, "incoming_calls": incoming,
            "insights": insights,
            "task_evidence": task_evidence,
            "truncated": truncated,
        }, byte_cap)
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

    for name in (
        "file", "function", "class", "body-query", "focus", "slice",
        "bodygrep", "trace", "impact", "deps", *sganalytics.ANALYTIC_MODES,
    ):
        p = sub.add_parser(name)
        p.add_argument("term")
        p.add_argument("budget", type=int, nargs="?", default=64)
        p.add_argument("--json", action="store_true", default=True)

    context_query_p = sub.add_parser("context-query")
    context_query_p.add_argument("term")
    context_query_p.add_argument("budget", type=int, nargs="?", default=64)
    context_query_p.add_argument("--json", action="store_true", default=True)

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
        elif args.command == "file":
            _print_json(file_query(repo_root, args.term, args.budget))
        elif args.command == "function":
            _print_json(function_query(repo_root, args.term, args.budget))
        elif args.command == "class":
            _print_json(class_query(repo_root, args.term, args.budget))
        elif args.command == "body-query":
            _print_json(body_query(repo_root, args.term, args.budget))
        elif args.command == "focus":
            _print_json(focus(repo_root, args.term, args.budget))
        elif args.command == "slice":
            _print_json(slice_(repo_root, args.term, args.budget))
        elif args.command == "bodygrep":
            _print_json(bodygrep_query(repo_root, args.term, args.budget))
        elif args.command == "context-query":
            _print_json(context_query(repo_root, args.term, args.budget))
        elif args.command == "trace":
            _print_json(trace(repo_root, args.term, args.budget))
        elif args.command == "impact":
            _print_json(impact(repo_root, args.term, args.budget))
        elif args.command == "deps":
            _print_json(deps_query(repo_root, args.term, args.budget))
        elif args.command in sganalytics.ANALYTIC_MODES:
            _print_json(analytics_query(repo_root, args.command, args.term, args.budget))
        elif args.command == "bundle":
            _print_json(bundle(repo_root, args.bundle_type, args.term, args.max_lines))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "BUILD_REVISION",
    "INDEXED_EXTENSIONS",
    "LANGUAGE_CAPABILITIES",
    "POLICY_SCHEMA_ID",
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
    "body_query",
    "bodygrep_query",
    "class_query",
    "build_index",
    "bundle",
    "analytics_query",
    "component_summary",
    "ensure_ignore_config",
    "ignore_config_path",
    "iter_source_files",
    "load_ignore_policy",
    "source_graph_policy_view",
    "update_language_policy",
    "connect",
    "context",
    "deps_query",
    "context_query",
    "find",
    "file_query",
    "focus",
    "function_query",
    "func",
    "impact",
    "slice_",
    "struct",
    "summary",
    "trace",
    "neighbors",
    "resolve_db_path",
    "shortest_path",
]
