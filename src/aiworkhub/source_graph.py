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
import concurrent.futures
import fnmatch
import hashlib
import json
import multiprocessing
import os
import re
import sqlite3
import stat
import sys
import time
from collections import deque
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from . import parallelism
from . import source_graph_ast as sgast
from . import source_graph_analytics as sganalytics
from . import source_graph_insights as sginsights
from . import source_graph_languages as sglanguages
from .sqlite_readonly import connect_readonly
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
MAX_ANALYTICS_CORPUS_ROWS = 4000
MAX_DEPTH = 6
MAX_NEIGHBOR_RESULTS = 200
MAX_COMPONENT_NODES = 500
MAX_PATH_VISITS = 5000
SOURCE_GRAPH_COMPACT_MIN_BYTES = 64 * 1024 * 1024
SOURCE_GRAPH_COMPACT_MIN_FREELIST_RATIO = 0.20
SOURCE_GRAPH_EXTRACT_WORKERS_ENV = "AIWORKHUB_SOURCE_GRAPH_EXTRACT_WORKERS"
DEFAULT_SOURCE_GRAPH_EXTRACT_WORKERS = 2
MAX_SOURCE_GRAPH_EXTRACT_WORKERS = 8
MIN_PARALLEL_EXTRACTION_FILES = 8
MIN_PARALLEL_EXTRACTION_BYTES = 256 * 1024
SOURCE_GRAPH_HASH_WORKERS_ENV = "AIWORKHUB_SOURCE_GRAPH_HASH_WORKERS"
MAX_SOURCE_GRAPH_HASH_WORKERS = 16
MIN_PARALLEL_HASH_FILES = 8
MIN_PARALLEL_HASH_BYTES = 256 * 1024
SOURCE_GRAPH_HASH_STABLE_READ_ATTEMPTS = 3
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

CREATE TABLE IF NOT EXISTS index_quality_history (
    finished_at TEXT PRIMARY KEY,
    build_revision TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class SourceGraphError(RuntimeError):
    """Base class for canonical Source Graph failures."""


class RepositoryUnresolvedError(SourceGraphError):
    """The repository identity could not be resolved (no manifest/registry)."""


class SourceGraphBuildInProgressError(SourceGraphError):
    """Another process currently owns this repository's index writer lease."""


class SourceGraphBuildFailedError(SourceGraphError):
    """A build's per-file write containment tripped its floor.

    Per-file skips exist so one bad file cannot take a repository down. They do
    NOT exist to let a wholesale write failure return a clean report over a
    near-empty index. When a generation attempted writes but committed none, or
    skipped more than half of what it attempted, the build failed and says so.
    """


# ---------------------------------------------------------------------------
# Repository / database resolution -- identity-bound, never a fixed path
# ---------------------------------------------------------------------------

_DB_PATH_OVERRIDE: ContextVar[Path | None] = ContextVar(
    "aiworkhub_source_graph_db_path_override", default=None
)


@contextmanager
def database_path_override(db_path: Path):
    """Bind one private graph DB to the current execution context only.

    Reviewer/rework overlays must not replace the module-level resolver:
    another thread may concurrently index a different repository. ContextVar
    scoping keeps nested calls such as ``index_write_lease`` on the same DB
    without leaking that authority into other threads or async tasks.
    """

    resolved = Path(db_path).resolve()
    token = _DB_PATH_OVERRIDE.set(resolved)
    try:
        yield resolved
    finally:
        _DB_PATH_OVERRIDE.reset(token)

def resolve_db_path(repo_root: Path) -> Path:
    """Resolve the canonical Source Graph database for ``repo_root``.

    Always ``<repo_root>/.aiworkhub/source_graph/source_graph.sqlite`` via
    the repository-bound storage registry -- never a fixed project-specific
    path and never influenced by process ``cwd``.
    """

    override = _DB_PATH_OVERRIDE.get()
    if override is not None:
        override.parent.mkdir(parents=True, exist_ok=True)
        return override

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
        conn = connect_readonly(db_path, timeout=30.0)
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

                # Windows may retain a byte-range lock for a very short window
                # while another build handle is closing. Avoid surfacing that
                # release race as a false permanent build-in-progress state,
                # while remaining bounded when another writer is genuinely
                # active.
                deadline = time.monotonic() + 1.0
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.01)
                        handle.seek(0)
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
            except (OSError, ValueError):
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
        return SourceGraphIgnorePolicy(frozenset(DEFAULT_EXCLUDE_DIR_NAMES), DEFAULT_CONFIG_EXCLUDE_GLOBS)
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
    extraction_workers: int = 1
    extraction_seconds: float = 0.0
    extraction_backend: str = "sequential"
    extraction_fallback_reason: str = ""
    extraction_telemetry: dict[str, Any] = field(default_factory=dict)
    index_quality: dict[str, Any] = field(default_factory=dict)
    hash_candidates: int = 0
    hash_reused: int = 0
    hash_mismatched: int = 0
    hash_unstable: int = 0
    hash_workers: int = 1
    hash_seconds: float = 0.0
    hash_backend: str = "sequential"
    hash_telemetry: dict[str, Any] = field(default_factory=dict)
    quality_reused: bool = False
    files_skipped: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "repo_root": self.repo_root, "db_path": self.db_path,
            "incremental": self.incremental, "files_seen": self.files_seen,
            "files_changed": self.files_changed, "files_unchanged": self.files_unchanged,
            "files_removed": self.files_removed, "files_skipped": self.files_skipped,
            "entities_written": self.entities_written,
            "edges_written": self.edges_written, "errors": self.errors,
            "build_revision": self.build_revision, "finished_at": self.finished_at,
            "compaction_performed": self.compaction_performed,
            "database_bytes_before_compaction": self.database_bytes_before_compaction,
            "extraction_workers": self.extraction_workers,
            "extraction_seconds": self.extraction_seconds,
            "extraction_backend": self.extraction_backend,
            "extraction_fallback_reason": self.extraction_fallback_reason,
            "extraction_telemetry": self.extraction_telemetry,
            "database_bytes_after_compaction": self.database_bytes_after_compaction,
            "freelist_ratio_before_compaction": self.freelist_ratio_before_compaction,
            "compaction_error": self.compaction_error,
            "compaction_recommended": self.compaction_recommended,
            "compaction_deferred_reason": self.compaction_deferred_reason,
            "index_quality": self.index_quality,
            "hash_candidates": self.hash_candidates,
            "hash_reused": self.hash_reused,
            "hash_mismatched": self.hash_mismatched,
            "hash_unstable": self.hash_unstable,
            "hash_workers": self.hash_workers,
            "hash_seconds": self.hash_seconds,
            "hash_backend": self.hash_backend,
            "hash_telemetry": self.hash_telemetry,
            "quality_reused": self.quality_reused,
        }


def _source_graph_extract_workers(
    candidate_count: int, candidate_bytes: int = 0,
) -> int:
    """Return a bounded extraction width for one repository build.

    Extraction is the read/parse phase only; SQLite invalidation, merge and
    cross-file resolution remain single-writer and deterministic.  A value of
    ``1`` is used for zero/one candidate so small incremental refreshes do not
    pay executor startup cost.  The environment override is intentionally
    bounded and cannot create an unbounded process fan-out.
    """

    configured = os.environ.get(SOURCE_GRAPH_EXTRACT_WORKERS_ENV, "").strip()
    if configured:
        try:
            requested = int(configured)
        except ValueError:
            requested = 1
        return min(
            max(1, candidate_count),
            max(1, min(requested, MAX_SOURCE_GRAPH_EXTRACT_WORKERS)),
        )
    if (
        candidate_count < MIN_PARALLEL_EXTRACTION_FILES
        or candidate_bytes < MIN_PARALLEL_EXTRACTION_BYTES
    ):
        return 1
    workers, _ = parallelism.compute_worker_count(
        candidate_count=candidate_count,
        reserve=1,
        ceiling=MAX_SOURCE_GRAPH_EXTRACT_WORKERS,
        min_candidates=MIN_PARALLEL_EXTRACTION_FILES,
    )
    return workers


def _source_graph_extraction_telemetry(
    workers: int,
    candidate_count: int,
    candidate_bytes: int = 0,
) -> dict[str, Any]:
    """Describe the exact extraction-width decision without fabricating it."""

    capacity = parallelism.get_cpu_capacity()
    configured = os.environ.get(SOURCE_GRAPH_EXTRACT_WORKERS_ENV, "").strip()
    if configured:
        return parallelism.WorkerSelection(
            available_cpus=capacity,
            selected_workers=workers,
            reserve=0,
            ceiling=MAX_SOURCE_GRAPH_EXTRACT_WORKERS,
            nested=parallelism.pool_is_nested(),
            reason="env_override",
        ).to_dict()
    if (
        candidate_count < MIN_PARALLEL_EXTRACTION_FILES
        or candidate_bytes < MIN_PARALLEL_EXTRACTION_BYTES
    ):
        return parallelism.WorkerSelection(
            available_cpus=capacity,
            selected_workers=workers,
            reserve=1,
            ceiling=MAX_SOURCE_GRAPH_EXTRACT_WORKERS,
            nested=parallelism.pool_is_nested(),
            reason="below_threshold",
        ).to_dict()
    _, selection = parallelism.compute_worker_count(
        candidate_count=candidate_count,
        reserve=1,
        ceiling=MAX_SOURCE_GRAPH_EXTRACT_WORKERS,
        min_candidates=MIN_PARALLEL_EXTRACTION_FILES,
    )
    return selection.to_dict()


def _spawn_bootstrap_in_progress() -> bool:
    """True while the current process is inside multiprocessing spawn bootstrap.

    ``multiprocessing`` sets ``current_process()._inheriting`` only during the
    handshake a freshly spawned child re-executes before importing and running
    its target.  Constructing a nested ``ProcessPoolExecutor`` from inside that
    window would re-enter the spawn bootstrap and deadlock or corrupt the
    pickle protocol, so callers must detect it *before* pool construction and
    fall back to deterministic sequential extraction.
    """

    return bool(getattr(multiprocessing.current_process(), "_inheriting", False))


def _source_graph_hash_workers(
    candidate_count: int, candidate_bytes: int = 0,
) -> int:
    """Return a bounded content-hashing width for one repository build.

    Mirrors ``_source_graph_extract_workers``: hashing only reads bytes and
    computes a digest (no AST/lexical parse), so a thread pool -- not a
    process pool -- is bounded by the same capacity-aware policy.
    """

    configured = os.environ.get(SOURCE_GRAPH_HASH_WORKERS_ENV, "").strip()
    if configured:
        try:
            requested = int(configured)
        except ValueError:
            requested = 1
        return min(
            max(1, candidate_count),
            max(1, min(requested, MAX_SOURCE_GRAPH_HASH_WORKERS)),
        )
    if (
        candidate_count < MIN_PARALLEL_HASH_FILES
        or candidate_bytes < MIN_PARALLEL_HASH_BYTES
    ):
        return 1
    workers, _ = parallelism.compute_worker_count(
        candidate_count=candidate_count,
        reserve=1,
        ceiling=MAX_SOURCE_GRAPH_HASH_WORKERS,
        min_candidates=MIN_PARALLEL_HASH_FILES,
    )
    return workers


def _source_graph_hash_telemetry(
    workers: int,
    candidate_count: int,
    candidate_bytes: int = 0,
) -> dict[str, Any]:
    """Describe the exact hashing-width decision without fabricating it."""

    capacity = parallelism.get_cpu_capacity()
    configured = os.environ.get(SOURCE_GRAPH_HASH_WORKERS_ENV, "").strip()
    if configured:
        return parallelism.WorkerSelection(
            available_cpus=capacity,
            selected_workers=workers,
            reserve=0,
            ceiling=MAX_SOURCE_GRAPH_HASH_WORKERS,
            nested=parallelism.pool_is_nested(),
            reason="env_override",
        ).to_dict()
    if (
        candidate_count < MIN_PARALLEL_HASH_FILES
        or candidate_bytes < MIN_PARALLEL_HASH_BYTES
    ):
        return parallelism.WorkerSelection(
            available_cpus=capacity,
            selected_workers=workers,
            reserve=1,
            ceiling=MAX_SOURCE_GRAPH_HASH_WORKERS,
            nested=parallelism.pool_is_nested(),
            reason="below_threshold",
        ).to_dict()
    _, selection = parallelism.compute_worker_count(
        candidate_count=candidate_count,
        reserve=1,
        ceiling=MAX_SOURCE_GRAPH_HASH_WORKERS,
        min_candidates=MIN_PARALLEL_HASH_FILES,
    )
    return selection.to_dict()


def _stable_content_hash(path: Path) -> tuple[str, int, int] | None:
    """Read one file and hash it with a stable pre/post stat straddle.

    Returns ``(sha256_hex, file_size, mtime_ns)`` only when the stat taken
    immediately before the read matches the stat taken immediately after,
    across up to ``SOURCE_GRAPH_HASH_STABLE_READ_ATTEMPTS`` tries. Returns
    ``None`` (fail closed) when the file kept changing under a concurrent
    writer or could not be read at all -- callers must never trust a torn
    read's hash and must route the file to full extraction instead.
    """

    for _ in range(SOURCE_GRAPH_HASH_STABLE_READ_ATTEMPTS):
        try:
            before = path.stat()
            raw = path.read_bytes()
            after = path.stat()
        except OSError:
            continue
        if before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns:
            return sgast.sha256_bytes(raw), int(after.st_size), int(after.st_mtime_ns)
    return None


def _hash_source_graph_candidate(
    candidate: tuple[str, str],
) -> tuple[str, str | None, int, int]:
    """Bounded thread-pool worker: stable content hash for one file.

    Returns ``(rel, hash_or_None, file_size, mtime_ns)``. ``hash`` is
    ``None`` when the stat-straddled read could not be proven stable, so
    the caller must fail closed and route this file to full extraction.
    """

    path_str, rel = candidate
    result = _stable_content_hash(Path(path_str))
    if result is None:
        return rel, None, -1, -1
    content_hash, file_size, mtime_ns = result
    return rel, content_hash, file_size, mtime_ns


def _extract_source_graph_candidate(
    candidate: tuple[str, str, str, int, int, str],
) -> tuple[sgast.FileExtraction, str, int, int]:
    """Spawn-safe pure extraction worker; it never opens the graph DB."""

    repo_root, path, rel, file_size, mtime_ns, build_revision = candidate
    extraction = sgast.extract_file(
        Path(repo_root), Path(path), build_revision=build_revision
    )
    return extraction, rel, file_size, mtime_ns


def _index_quality_scorecard(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    finished_at: str,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute cheap, generation-bound truth about one committed index.

    These are structural database measurements, not retrieval-quality or
    provider-token claims.  Keeping that boundary explicit lets health flag
    a thin graph without pretending that a high edge ratio proves a model
    will produce a correct change.
    """

    total_edges = int(conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0])
    resolved_edges = int(conn.execute(
        "SELECT COUNT(*) FROM edges e WHERE e.dst_qualname IS NOT NULL "
        "AND e.dst_qualname != '' AND EXISTS ("
        "SELECT 1 FROM entities d WHERE d.qualname=e.dst_qualname)"
    ).fetchone()[0])
    cross_language_edges = int(conn.execute(
        "SELECT COUNT(DISTINCT e.id) FROM edges e "
        "JOIN files sf ON sf.file_path=e.file_path "
        "JOIN entities d ON d.qualname=e.dst_qualname "
        "JOIN files df ON df.file_path=d.file_path "
        "WHERE sf.language != df.language"
    ).fetchone()[0])
    junk_destination_edges = int(conn.execute(
        "SELECT COUNT(*) FROM edges WHERE "
        "dst_name LIKE '../%' OR dst_name LIKE './%' OR "
        "dst_name LIKE '%/../%' OR dst_name LIKE '%\\\\%'"
    ).fetchone()[0])
    entity_count = int(conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0])
    artifact_entities = int(conn.execute(
        "SELECT COUNT(*) FROM entities WHERE "
        "file_path LIKE 'eval/%' OR file_path LIKE 'artifacts/%' OR "
        "file_path LIKE 'coverage/%' OR file_path LIKE 'tmp/%' OR "
        "file_path LIKE '.tmp/%' OR file_path LIKE 'dist/%' OR "
        "file_path LIKE 'build/%'"
    ).fetchone()[0])

    # Each metric is pre-aggregated against ``files`` on its own, single
    # foreign key -- never joined to more than one fan-out table at a time.
    # The prior single query joined files -> entities -> edges -> resolved
    # in one statement, so SQLite's planner multiplied every file's entity
    # rows by its edge rows before the four COUNT(DISTINCT) temp B-trees
    # could collapse the product back down; on a high-fanout file that
    # Cartesian intermediate made an unchanged incremental refresh scan
    # the same cost as a full rebuild (see NF-2026-00204).
    files_by_language: dict[str, int] = {
        str(row["language"]): int(row["files"])
        for row in conn.execute(
            "SELECT language, COUNT(*) AS files FROM files GROUP BY language"
        )
    }
    entities_by_language: dict[str, int] = {
        str(row["language"]): int(row["entities"])
        for row in conn.execute(
            "SELECT f.language AS language, COUNT(*) AS entities "
            "FROM entities en JOIN files f ON f.file_path=en.file_path "
            "GROUP BY f.language"
        )
    }
    edges_by_language: dict[str, dict[str, int]] = {
        str(row["language"]): {
            "edges": int(row["edges"] or 0),
            "resolved_edges": int(row["resolved_edges"] or 0),
        }
        for row in conn.execute(
            "SELECT f.language AS language, COUNT(*) AS edges, "
            "SUM(CASE WHEN e.dst_qualname IS NOT NULL AND e.dst_qualname != '' "
            "AND EXISTS (SELECT 1 FROM entities d WHERE d.qualname=e.dst_qualname) "
            "THEN 1 ELSE 0 END) AS resolved_edges "
            "FROM edges e JOIN files f ON f.file_path=e.file_path "
            "GROUP BY f.language"
        )
    }
    span_rows = {
        str(row["language"]): int(row["indexed_span_lines"] or 0)
        for row in conn.execute(
            "WITH spans AS ("
            " SELECT file_path, MAX(line_end) AS lines FROM entities GROUP BY file_path"
            ") SELECT f.language, COALESCE(SUM(sp.lines), 0) AS indexed_span_lines "
            "FROM files f LEFT JOIN spans sp ON sp.file_path=f.file_path "
            "GROUP BY f.language"
        )
    }
    by_language: dict[str, dict[str, Any]] = {}
    for language in sorted(files_by_language):
        edge_stats = edges_by_language.get(language, {})
        edges = int(edge_stats.get("edges", 0) or 0)
        resolved = int(edge_stats.get("resolved_edges", 0) or 0)
        entities = int(entities_by_language.get(language, 0) or 0)
        span_lines = span_rows.get(language, 0)
        by_language[language] = {
            "files": files_by_language[language],
            "entities": entities,
            "edges": edges,
            "resolved_edges": resolved,
            "resolved_edge_ratio": round(resolved / edges, 6) if edges else None,
            "indexed_span_lines": span_lines,
            "entities_per_kloc": (
                round(entities * 1000.0 / span_lines, 3) if span_lines else None
            ),
        }

    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    resolved_ratio = resolved_edges / total_edges if total_edges else None
    artifact_ratio = artifact_entities / entity_count if entity_count else None
    degraded_reasons: list[str] = []
    if total_edges and resolved_edges == 0:
        degraded_reasons.append("no_resolved_edges")
    if cross_language_edges:
        degraded_reasons.append("cross_language_edges_present")
    if artifact_ratio is not None and artifact_ratio > 0.25:
        degraded_reasons.append("artifact_entity_share_above_25_percent")
    previous_edges = (previous or {}).get("edges") or {}
    previous_ratio = previous_edges.get("resolved_ratio")
    resolved_ratio_delta = None
    if resolved_ratio is not None and isinstance(previous_ratio, (int, float)):
        resolved_ratio_delta = resolved_ratio - float(previous_ratio)
        if resolved_ratio_delta < -0.05:
            degraded_reasons.append("resolved_edge_ratio_dropped_over_5_points")

    density_regressions: list[dict[str, Any]] = []
    previous_languages = (previous or {}).get("by_language") or {}
    for language, row in by_language.items():
        prior = previous_languages.get(language) or {}
        current_density = row.get("entities_per_kloc")
        prior_density = prior.get("entities_per_kloc")
        if not isinstance(current_density, (int, float)) or not isinstance(
            prior_density, (int, float)
        ) or prior_density <= 0:
            continue
        relative_delta = (float(current_density) - float(prior_density)) / float(
            prior_density
        )
        row["entities_per_kloc_delta_ratio"] = round(relative_delta, 6)
        if relative_delta < -0.20:
            density_regressions.append({
                "language": language,
                "previous": prior_density,
                "current": current_density,
                "delta_ratio": round(relative_delta, 6),
            })
    if density_regressions:
        degraded_reasons.append("language_entity_density_dropped_over_20_percent")

    thin_language_guidance: list[dict[str, Any]] = []
    for family, members in {
        "javascript_typescript": ("javascript", "typescript", "jsx", "tsx"),
    }.items():
        present = [by_language[name] for name in members if name in by_language]
        files = sum(int(row.get("files") or 0) for row in present)
        edges = sum(int(row.get("edges") or 0) for row in present)
        resolved = sum(int(row.get("resolved_edges") or 0) for row in present)
        family_ratio = (resolved / edges) if edges else None
        if files and (edges == 0 or (family_ratio is not None and family_ratio < 0.10)):
            thin_language_guidance.append({
                "family": family,
                "files": files,
                "edges": edges,
                "resolved_edges": resolved,
                "resolved_edge_ratio": (
                    round(family_ratio, 6) if family_ratio is not None else None
                ),
                "guidance": "use_exact_symbols_and_bounded_file_context",
            })

    return {
        "schema_id": "aiworkhub.source_graph.index_quality.v1",
        "build_revision": BUILD_REVISION,
        "finished_at": finished_at,
        "edges": {
            "total": total_edges,
            "resolved": resolved_edges,
            "unresolved": max(0, total_edges - resolved_edges),
            "resolved_ratio": round(resolved_ratio, 6) if resolved_ratio is not None else None,
            "cross_language": cross_language_edges,
            "junk_destination": junk_destination_edges,
        },
        "artifacts": {
            "entities": artifact_entities,
            "total_entities": entity_count,
            "entity_share": round(artifact_ratio, 6) if artifact_ratio is not None else None,
            "path_families": ["eval", "artifacts", "coverage", "tmp", "dist", "build"],
        },
        "by_language": by_language,
        "generation_delta": {
            "previous_finished_at": str((previous or {}).get("finished_at") or ""),
            "resolved_edge_ratio_delta": (
                round(resolved_ratio_delta, 6)
                if resolved_ratio_delta is not None else None
            ),
            "density_regressions": density_regressions,
        },
        "thin_language_guidance": thin_language_guidance,
        "storage": {
            "db_bytes": int(db_path.stat().st_size) if db_path.exists() else 0,
            "page_count": page_count,
            "freelist_count": freelist_count,
            "freelist_ratio": round(freelist_count / page_count, 6) if page_count else 0.0,
        },
        "degraded": bool(degraded_reasons),
        "degraded_reasons": degraded_reasons,
        "measurement_boundary": "structural_index_metrics_not_retrieval_or_token_savings",
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
) -> tuple[int, int, list[dict[str, str]]]:
    """Persist one extraction; return ``(entities, edges, dropped_duplicates)``.

    Extractors may conservatively emit the same edge more than once.  The
    database writer deliberately deduplicates those identities, so callers
    must report the inserted population rather than the pre-dedup candidate
    population.

    The dropped-duplicate report is RETURNED rather than accumulated through a
    caller-supplied out-parameter on purpose: the write contract stays
    ``(conn, extraction, *, file_size, mtime_ns)`` so any forwarding wrapper or
    subclass around this path keeps working. (An earlier revision took a mutable
    ``dropped_duplicates`` keyword; a stub that forwarded the old signature then
    raised ``TypeError`` on the new keyword, and the broad per-file containment
    swallowed it for every file -- turning a total write failure into a
    success-costumed empty index.)

    The ``(file_path, qualname)`` entity dedup is a last-resort guard: every
    extractor is expected to disambiguate before this point (Python and JS/TS
    thread ``_dedupe_qualname``/``_unique_qualname`` through every emitted kind;
    PHP, the C family and the polyglot lexical path thread one per-file counter
    likewise). A duplicate reaching here is therefore an extractor defect, not
    normal output, so it must stay VISIBLE rather than vanish: every collapsed
    identity is recorded in the returned ``dropped_duplicates`` list (file,
    qualname, extractor, kind) for the caller to surface in the build report.
    The row is still dropped rather than inserted so one such defect cannot
    abort the whole run.
    """
    conn.execute(
        "INSERT INTO files(file_path, language, status, source_hash, file_size, mtime_ns, "
        "indexed_at, build_revision) VALUES (?,?,?,?,?,?,?,?)",
        (extraction.file_path, extraction.language, extraction.status,
         extraction.source_hash, file_size, mtime_ns, _now_iso(), BUILD_REVISION),
    )
    seen_entities: set[tuple[str, str]] = set()
    inserted_entities = 0
    dropped: list[dict[str, str]] = []
    for entity in extraction.entities:
        natural_key = (entity.file_path, entity.qualname)
        if natural_key in seen_entities:
            # Idempotent on the ``(file_path, qualname)`` natural key: an
            # extractor that emits the very same identity twice must never abort
            # the run. Genuinely distinct declarations already carry distinct
            # qualnames (the extractors disambiguate before this point), so a
            # real declaration is never the row that gets collapsed here -- but
            # record every collapse so a genuine extractor defect stays visible
            # instead of silently vanishing.
            dropped.append({
                "file": entity.file_path, "qualname": entity.qualname,
                "extractor": entity.extractor, "kind": entity.kind,
            })
            continue
        seen_entities.add(natural_key)
        try:
            cur = conn.execute(
                "INSERT INTO entities(file_path, kind, name, qualname, line_start, line_end, "
                "signature, evidence_label, extractor, confidence, source_hash, build_revision) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (entity.file_path, entity.kind, entity.name, entity.qualname, entity.line_start,
                 entity.line_end, entity.signature, entity.evidence_label, entity.extractor,
                 entity.confidence, entity.source_hash, entity.build_revision),
            )
        except sqlite3.IntegrityError as exc:
            # Name the file, qualname and extractor id so an integrity failure is
            # diagnosable instead of an opaque "UNIQUE constraint failed".
            raise sqlite3.IntegrityError(
                f"entities uniqueness violated: file={entity.file_path!r} "
                f"qualname={entity.qualname!r} extractor={entity.extractor!r}"
            ) from exc
        conn.execute(
            "INSERT INTO entities_fts(entity_id, name, qualname, signature, file_path) "
            "VALUES (?,?,?,?,?)",
            (cur.lastrowid, entity.name, entity.qualname, entity.signature, entity.file_path),
        )
        inserted_entities += 1
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
    return inserted_entities, len(seen_edges), dropped


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


def _resolve_python_imported_calls(
    conn: sqlite3.Connection, repo_root: Path,
) -> int:
    """Bind Python calls only when imports and exact call syntax agree.

    Python AST extraction intentionally leaves cross-file targets unresolved.
    This pass resolves imported functions without guessing receiver types: a
    direct call must name the imported symbol, while an attribute call must
    use the exact local import alias on its recorded source line.  The target
    must map to exactly one Python function in the imported module.

    Like the C++ resolver, recomputing after every build must also clear a
    binding whose target has disappeared. A Python call edge only fills when
    its ``dst_qualname`` resolves to a real entity, so any managed edge whose
    ``dst_qualname`` no longer names an existing entity -- an incremental build
    renamed/deleted the callee while the caller file was untouched -- is reset
    to NULL first, then re-resolved below. Without this, a stale binding to a
    vanished symbol would read as EXTRACTED against an entity that is gone.
    """

    conn.execute(
        "UPDATE edges SET dst_qualname=NULL "
        "WHERE extractor=? AND kind='calls' AND dst_qualname IS NOT NULL "
        "AND evidence_label IN (?, ?) "
        "AND NOT EXISTS (SELECT 1 FROM entities en WHERE en.qualname = edges.dst_qualname)",
        (sgast.EXTRACTOR_ID, sgast.EXTRACTED, sgast.INFERRED),
    )

    unresolved = conn.execute(
        "SELECT e.id, e.file_path, e.line, e.dst_name, e.evidence_label "
        "FROM edges e JOIN files f ON f.file_path=e.file_path "
        "WHERE e.extractor=? AND f.language='python' AND e.kind='calls' "
        "AND e.dst_qualname IS NULL AND e.evidence_label IN (?, ?) "
        "ORDER BY e.id",
        (sgast.EXTRACTOR_ID, sgast.EXTRACTED, sgast.INFERRED),
    ).fetchall()
    imports_by_file: dict[str, list[sqlite3.Row]] = {}
    lines_by_file: dict[str, tuple[str, ...]] = {}
    candidates_by_name: dict[str, list[sqlite3.Row]] = {}
    resolved = 0

    for edge in unresolved:
        file_path = str(edge["file_path"])
        imports = imports_by_file.get(file_path)
        if imports is None:
            imports = conn.execute(
                "SELECT name, signature FROM entities WHERE file_path=? "
                "AND kind='import' AND extractor=? ORDER BY id",
                (file_path, sgast.EXTRACTOR_ID),
            ).fetchall()
            imports_by_file[file_path] = imports
        if not imports:
            continue

        name = str(edge["dst_name"])
        candidates = candidates_by_name.get(name)
        if candidates is None:
            candidates = conn.execute(
                "SELECT e.file_path, e.qualname FROM entities e "
                "JOIN files f ON f.file_path=e.file_path "
                "WHERE e.name=? AND e.kind='function' AND f.language='python' "
                "ORDER BY e.file_path, e.qualname",
                (name,),
            ).fetchall()
            candidates_by_name[name] = candidates

        matching_imports: list[str] = []
        if str(edge["evidence_label"]) == sgast.EXTRACTED:
            matching_imports = [
                str(item["signature"])
                for item in imports
                if str(item["name"]) == name
                and str(item["signature"]).lstrip(".").rsplit(".", 1)[-1] == name
            ]
        else:
            lines = lines_by_file.get(file_path)
            if lines is None:
                try:
                    lines = tuple(
                        (repo_root / file_path).read_text(
                            encoding="utf-8", errors="strict",
                        ).splitlines()
                    )
                except (OSError, UnicodeError):
                    lines = ()
                lines_by_file[file_path] = lines
            line_number = int(edge["line"] or 0)
            source_line = lines[line_number - 1] if 0 < line_number <= len(lines) else ""
            for item in imports:
                alias = str(item["name"])
                alias_call = re.compile(
                    rf"\b{re.escape(alias)}(?:\s*\.\s*[A-Za-z_]\w*)*"
                    rf"\s*\.\s*{re.escape(name)}\s*\("
                )
                if alias_call.search(source_line):
                    matching_imports.append(str(item["signature"]))
        if not matching_imports:
            continue

        matched_candidates = []
        for candidate in candidates:
            candidate_file = str(candidate["file_path"])
            for target in matching_imports:
                module_target = target
                if target.lstrip(".").rsplit(".", 1)[-1] == name:
                    module_target = target.rsplit(".", 1)[0]
                if _import_target_matches_file(module_target, candidate_file):
                    matched_candidates.append(candidate)
                    break
        if len(matched_candidates) != 1:
            continue
        cur = conn.execute(
            "UPDATE edges SET dst_qualname=? WHERE id=? AND dst_qualname IS NULL",
            (matched_candidates[0]["qualname"], edge["id"]),
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
    # ``from . import symbol`` has an empty module component after the
    # imported symbol is removed.  Converting that empty component through
    # the generic dotted-target normalizer produces ``/``.  It carries no
    # module identity, so fail closed instead of calling ``with_suffix`` on a
    # filesystem root (which raises ``ValueError`` and aborts the full index).
    if target_path.name in {"", ".", ".."} or file_path_obj.name in {"", ".", ".."}:
        return False
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


def _resolve_javascript_import_bindings(conn: sqlite3.Connection) -> int:
    """Bind each JS/TS ``imports`` edge to the one indexed module it names.

    Both the tree-sitter and lexical-fallback JS/TS extractors record the
    observed import specifier string but never prove which indexed file it
    names -- ``dst_qualname`` starts NULL for every ``imports`` edge
    regardless of extractor. A specifier that matches exactly one indexed
    JS/TS module in this repository is bound to that module's own qualname
    (EXTRACTED: the specifier match is exact and unique). More than one
    candidate module is recorded as AMBIGUOUS -- distinct from an edge that
    was never examined -- so a caller can tell "the specifier does not name
    one file" apart from "resolution was never attempted". Zero candidates
    (an npm package, an unindexed extension, a path outside the repository)
    are left untouched.
    """

    unresolved = conn.execute(
        "SELECT e.id, e.dst_name FROM edges e "
        "JOIN files f ON f.file_path=e.file_path "
        "WHERE e.kind='imports' AND e.dst_qualname IS NULL "
        "AND f.language IN ('javascript', 'typescript') "
        "ORDER BY e.id"
    ).fetchall()
    if not unresolved:
        return 0
    modules = [
        (str(row["file_path"]), str(row["qualname"]))
        for row in conn.execute(
            "SELECT en.file_path, en.qualname FROM entities en "
            "JOIN files f ON f.file_path=en.file_path "
            "WHERE en.kind='module' AND f.language IN ('javascript', 'typescript')"
        )
    ]
    candidates_by_target: dict[str, list[tuple[str, str]]] = {}
    resolved = 0
    for edge in unresolved:
        target = str(edge["dst_name"])
        candidates = candidates_by_target.get(target)
        if candidates is None:
            candidates = [
                (file_path, qualname) for file_path, qualname in modules
                if _import_target_matches_file(target, file_path)
            ]
            candidates_by_target[target] = candidates
        if len(candidates) == 1:
            cur = conn.execute(
                "UPDATE edges SET dst_qualname=?, evidence_label=? "
                "WHERE id=? AND dst_qualname IS NULL",
                (candidates[0][1], sgast.EXTRACTED, edge["id"]),
            )
            resolved += int(cur.rowcount or 0)
        elif len(candidates) > 1:
            conn.execute(
                "UPDATE edges SET evidence_label=? WHERE id=?",
                (sgast.AMBIGUOUS, edge["id"]),
            )
    return resolved


def _build_index_locked(repo_root: Path, *, db_path: Path | None = None, incremental: bool = True) -> BuildReport:
    repo_root = repo_root.resolve()
    resolved_db_path = db_path or resolve_db_path(repo_root)
    conn = connect(resolved_db_path)
    files_on_disk = iter_source_files(repo_root)
    seen_rel: set[str] = set()
    changed = unchanged = removed = entities_written = edges_written = 0
    skipped = 0
    errors: list[dict[str, str]] = []
    extraction_workers = 1
    extraction_seconds = 0.0
    extraction_backend = "sequential"
    extraction_fallback_reason = ""
    extraction_telemetry: dict[str, Any] = {}
    hash_workers = 1
    hash_seconds = 0.0
    hash_backend = "sequential"
    hash_telemetry: dict[str, Any] = {}
    hash_candidates_seen = 0
    hash_reused = 0
    hash_mismatched = 0
    hash_unstable = 0
    quality_reused = False
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
        extraction_candidates: list[tuple[Path, str, int, int]] = []
        hash_candidates: list[tuple[Path, str, int, int, str]] = []
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
                # Size and mtime agree with the prior generation's hint,
                # but a hint is not identity: a same-second content
                # mutation can leave both unchanged. Content hashing --
                # not the stat hint -- is authoritative, so this file still
                # goes through a bounded stable-read hash before it can be
                # trusted as unchanged.
                hash_candidates.append((path, rel, file_size, mtime_ns, prior[0]))
                continue
            extraction_candidates.append((path, rel, file_size, mtime_ns))

        hash_candidate_bytes = sum(max(0, item[2]) for item in hash_candidates)
        hash_workers = _source_graph_hash_workers(
            len(hash_candidates), hash_candidate_bytes,
        )
        hash_telemetry = _source_graph_hash_telemetry(
            hash_workers, len(hash_candidates), hash_candidate_bytes,
        )
        hash_candidates_seen = len(hash_candidates)
        hash_started = time.monotonic()
        if hash_candidates:
            hash_inputs = [(str(path), rel) for path, rel, _, _, _ in hash_candidates]
            if hash_workers > 1:
                with parallelism.worker_pool_scope():
                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=hash_workers,
                    ) as executor:
                        hash_results = list(
                            executor.map(_hash_source_graph_candidate, hash_inputs)
                        )
                hash_backend = "thread_pool"
            else:
                hash_results = [
                    _hash_source_graph_candidate(item) for item in hash_inputs
                ]
            hashed_by_rel = {
                result_rel: content_hash
                for result_rel, content_hash, _, _ in hash_results
            }
            for path, rel, file_size, mtime_ns, prior_hash in hash_candidates:
                content_hash = hashed_by_rel.get(rel)
                if content_hash is not None and content_hash == prior_hash:
                    unchanged += 1
                    hash_reused += 1
                    continue
                if content_hash is None:
                    hash_unstable += 1
                else:
                    hash_mismatched += 1
                # Hash mismatch (content mutated behind an unchanged stat
                # hint) or an unstable straddled read (fail closed): both
                # must go through full extraction, never be silently
                # counted as unchanged.
                extraction_candidates.append((path, rel, file_size, mtime_ns))
        hash_seconds = max(0.0, time.monotonic() - hash_started)

        candidate_count = len(extraction_candidates)
        candidate_bytes = sum(
            max(0, candidate[2]) for candidate in extraction_candidates
        )
        extraction_workers = _source_graph_extract_workers(
            candidate_count, candidate_bytes,
        )
        extraction_telemetry = _source_graph_extraction_telemetry(
            extraction_workers, candidate_count, candidate_bytes,
        )
        extraction_started = time.monotonic()
        process_candidates = [
            (
                str(repo_root), str(path), rel, file_size, mtime_ns,
                BUILD_REVISION,
            )
            for path, rel, file_size, mtime_ns in extraction_candidates
        ]
        if extraction_workers > 1:
            if _spawn_bootstrap_in_progress():
                # Constructing a nested ProcessPoolExecutor while the current
                # process is still inside the multiprocessing spawn bootstrap
                # handshake would re-enter bootstrap and deadlock or corrupt
                # the pickle protocol.  Extraction never mutates canonical
                # state, so an all-candidate sequential replay is deterministic
                # and the fallback stays visible in the build receipt.
                extraction_workers = 1
                extraction_backend = "sequential_fallback"
                extraction_fallback_reason = "spawn_bootstrap_in_progress"
                extraction_telemetry = {
                    **extraction_telemetry,
                    "selected_workers": 1,
                    "reason": "fallback_spawn_bootstrap_in_progress",
                }
                extracted_candidates = [
                    _extract_source_graph_candidate(candidate)
                    for candidate in process_candidates
                ]
            else:
                try:
                    # Spawn is used on every OS.  It avoids forking the live MCP
                    # server (which may already own threads/SQLite handles) and
                    # keeps the worker contract identical on Linux, Windows and
                    # macOS.  Workers only read source and return immutable
                    # extraction records; the parent remains the sole DB writer.
                    with parallelism.worker_pool_scope():
                        with concurrent.futures.ProcessPoolExecutor(
                            max_workers=extraction_workers,
                            mp_context=multiprocessing.get_context("spawn"),
                        ) as executor:
                            extracted_candidates = list(
                                executor.map(
                                    _extract_source_graph_candidate,
                                    process_candidates,
                                    chunksize=1,
                                )
                            )
                    extraction_backend = "process_pool"
                except (OSError, BrokenProcessPool) as exc:
                    # Sandboxes may deny process creation.  No extraction mutates
                    # canonical state, so an all-candidate sequential replay is
                    # safe and the fallback stays visible in the build receipt.
                    extraction_workers = 1
                    extraction_backend = "sequential_fallback"
                    extraction_fallback_reason = type(exc).__name__
                    extraction_telemetry = {
                        **extraction_telemetry,
                        "selected_workers": 1,
                        "reason": f"fallback_{type(exc).__name__}",
                    }
                    extracted_candidates = [
                        _extract_source_graph_candidate(candidate)
                        for candidate in process_candidates
                    ]
        else:
            extracted_candidates = [
                _extract_source_graph_candidate(candidate)
                for candidate in process_candidates
            ]
        extraction_seconds = max(0.0, time.monotonic() - extraction_started)

        # ``Executor.map`` preserves candidate order.  The SQLite merge stays
        # single-threaded so parallel extraction cannot change row authority,
        # invalidate a file twice, or make query results schedule-dependent.
        for extraction, rel, file_size, mtime_ns in extracted_candidates:
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
            for write_index, (extraction, file_size, mtime_ns) in enumerate(
                pending_extractions
            ):
                # Fail closed PER FILE, never per repository. A single file whose
                # rows cannot be persisted is rolled back to its own savepoint,
                # skipped and counted, while every other file in the run still
                # commits. One bad row must never leave the entire repository
                # index unavailable. The savepoint is taken BEFORE invalidation
                # so a skip restores the file's prior generation intact rather
                # than leaving a half-deleted partial.
                savepoint = f"sg_write_{write_index}"
                file_dropped: list[dict[str, str]] = []
                conn.execute(f"SAVEPOINT {savepoint}")
                try:
                    _invalidate_file(conn, extraction.file_path)
                    inserted_entities, inserted_edges, file_dropped = _write_extraction(
                        conn, extraction, file_size=file_size, mtime_ns=mtime_ns,
                    )
                except Exception as exc:  # noqa: BLE001
                    # Containment is deliberately BROAD. The whole point of this
                    # boundary is that ONE file must never take down a repository,
                    # and the failure this exists for -- an IntegrityError -- is
                    # exactly the kind of exception nobody enumerated in advance.
                    # Catching only the types we thought of is how the original
                    # repository-wide abort was written; so any Exception rolls
                    # the file back to its own savepoint, is recorded with its
                    # type, and the run keeps going. Only BaseException
                    # (KeyboardInterrupt/SystemExit) still propagates, because a
                    # cancellation or interpreter shutdown genuinely must abort
                    # the whole build rather than be swallowed per file.
                    conn.execute(f"ROLLBACK TO {savepoint}")
                    conn.execute(f"RELEASE {savepoint}")
                    skipped += 1
                    errors.append({
                        "file": extraction.file_path, "status": "index_write_skipped",
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    continue
                conn.execute(f"RELEASE {savepoint}")
                changed += 1
                entities_written += inserted_entities
                edges_written += inserted_edges
                for dropped in file_dropped:
                    # A duplicate natural key reaching the writer is an extractor
                    # defect, not normal output. Surface it instead of letting it
                    # vanish behind the idempotent dedup.
                    errors.append({
                        "file": dropped["file"], "status": "duplicate_entity_dropped",
                        "error": (
                            f"duplicate (file_path, qualname) collapsed: "
                            f"qualname={dropped['qualname']!r} kind={dropped['kind']!r} "
                            f"extractor={dropped['extractor']!r}"
                        ),
                    })
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
                _resolve_python_imported_calls(conn, repo_root)
                _resolve_javascript_import_bindings(conn)
            sginsights.materialize_git_metrics(
                conn, repo_root, sorted(seen_rel), limit=10000,
            )
            finished_at = _now_iso()
            skipped_files = [
                entry["file"] for entry in errors
                if entry.get("status") == "index_write_skipped"
            ]
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('last_build', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps({
                    "finished_at": finished_at, "incremental": incremental,
                    "files_seen": len(files_on_disk), "files_changed": changed,
                    "files_removed": removed, "build_revision": BUILD_REVISION,
                    # Persist the skip outcome so a partially-failed build cannot
                    # be mistaken for a clean one by any consumer that reads meta
                    # rather than the in-memory BuildReport. ``files_skipped`` > 0
                    # (and the named ``skipped_files``) makes the degradation
                    # visible to an operator after the fact.
                    "files_skipped": skipped,
                    "skipped_files": skipped_files,
                }),),
            )
        # Containment floor. Per-file skips exist so ONE bad file cannot take a
        # repository down -- never so a wholesale write failure can pass as a
        # healthy build. A generation that attempted writes but committed NONE
        # (every file skipped), or whose skips exceed half of what it attempted,
        # is a FAILED build and must fail loudly instead of returning a clean
        # report over a near-empty index. ``last_build`` above already recorded
        # files_skipped/skipped_files, so the failure stays durable and
        # diagnosable after this raise. ``changed``/``skipped`` count only files
        # that reached the writer this run, so a legitimate no-op incremental
        # refresh (attempted == 0) and a clean full build never trip the floor.
        attempted_writes = changed + skipped
        if attempted_writes and (changed == 0 or skipped * 2 > attempted_writes):
            raise SourceGraphBuildFailedError(
                "source_graph_build_failed_write_containment: "
                f"changed={changed} skipped={skipped} "
                f"attempted={attempted_writes} skipped_files={skipped_files}"
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
        previous_quality_row = conn.execute(
            "SELECT value FROM meta WHERE key='index_quality'"
        ).fetchone()
        try:
            previous_quality = (
                json.loads(previous_quality_row["value"])
                if previous_quality_row is not None else None
            )
        except (TypeError, json.JSONDecodeError):
            previous_quality = None
        if (
            changed == 0 and removed == 0
            and isinstance(previous_quality, dict) and previous_quality
        ):
            # A true no-op generation -- nothing changed or removed, even
            # after hash-authoritative reconciliation -- leaves the graph
            # byte-for-byte identical to the prior generation. Re-running
            # every graph-wide aggregation query would cost the same as a
            # full rebuild for zero new truth; reuse the prior metrics
            # verbatim and only align the receipt to this generation.
            index_quality = {**previous_quality, "finished_at": finished_at}
            quality_reused = True
        else:
            index_quality = _index_quality_scorecard(
                conn,
                resolved_db_path,
                finished_at=finished_at,
                previous=previous_quality if isinstance(previous_quality, dict) else None,
            )
        with conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('index_quality', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(index_quality, ensure_ascii=False, sort_keys=True),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO index_quality_history"
                "(finished_at, build_revision, payload) VALUES(?,?,?)",
                (
                    finished_at,
                    BUILD_REVISION,
                    json.dumps(index_quality, ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.execute(
                "DELETE FROM index_quality_history WHERE finished_at NOT IN ("
                "SELECT finished_at FROM index_quality_history "
                "ORDER BY finished_at DESC LIMIT 100)"
            )
    finally:
        conn.close()
    return BuildReport(
        repo_root=str(repo_root), db_path=str(resolved_db_path), incremental=incremental,
        files_seen=len(files_on_disk), files_changed=changed, files_unchanged=unchanged,
        files_removed=removed, entities_written=entities_written, edges_written=edges_written,
        files_skipped=skipped,
        errors=errors, build_revision=BUILD_REVISION, finished_at=finished_at,
        compaction_performed=compaction_performed,
        database_bytes_before_compaction=bytes_before_compaction,
        database_bytes_after_compaction=bytes_after_compaction,
        freelist_ratio_before_compaction=freelist_ratio,
        compaction_error=compaction_error,
        compaction_recommended=compaction_recommended,
        compaction_deferred_reason=compaction_deferred_reason,
        extraction_workers=extraction_workers,
        extraction_seconds=extraction_seconds,
        extraction_backend=extraction_backend,
        extraction_fallback_reason=extraction_fallback_reason,
        extraction_telemetry=extraction_telemetry,
        index_quality=index_quality,
        hash_candidates=hash_candidates_seen,
        hash_reused=hash_reused,
        hash_mismatched=hash_mismatched,
        hash_unstable=hash_unstable,
        hash_workers=hash_workers,
        hash_seconds=hash_seconds,
        hash_backend=hash_backend,
        hash_telemetry=hash_telemetry,
        quality_reused=quality_reused,
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


# A bodygrep cursor resumes at ``file_path >= after_file`` and skips lines
# ``<= after_line`` inside ``after_file``. This sentinel means "skip every line
# of after_file" -- used to resume strictly past a file that was fully scanned
# (e.g. the last file before the scan-file cap) without re-emitting its matches.
_BODYGREP_SKIP_ALL_LINES = 1 << 62


def _bodygrep_cursor_digest(term: str, target: str, budget: int) -> str:
    payload = f"{term}\x1f{target}\x1f{budget}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _encode_bodygrep_cursor(
    after_file: str, after_line: int, *, term: str, target: str, budget: int,
) -> str:
    af_hex = after_file.encode("utf-8").hex()
    return f"{af_hex}-{int(after_line)}-{_bodygrep_cursor_digest(term, target, budget)}"


def _decode_bodygrep_cursor(
    cursor: str | None, *, term: str, target: str, budget: int,
) -> tuple[str, int] | None:
    if cursor is None:
        return None
    raw = str(cursor).strip()
    if not raw:
        return None
    parts = raw.split("-")
    if len(parts) != 3:
        raise SourceGraphError("invalid_cursor")
    af_hex, after_line_text, digest = parts
    if (
        not after_line_text.isdigit()
        or digest != _bodygrep_cursor_digest(term, target, budget)
    ):
        raise SourceGraphError("invalid_cursor")
    try:
        after_file = bytes.fromhex(af_hex).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise SourceGraphError("invalid_cursor") from exc
    return (after_file, int(after_line_text))


def bodygrep_query(
    repo_root: Path,
    term: str,
    budget: int = 64,
    *,
    target: str | None = None,
    cursor: str | None = None,
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
            "scan_truncated": False, "cursor": cursor, "next_cursor": None,
            "truncated": False,
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

    # A cursor resumes the ORDER BY file_path walk at ``file_path >=
    # resume_file`` and skips lines already returned inside that file, so the
    # rest of the repository stays reachable across pages instead of the scan
    # ending forever at the first byte-cap or file-cap overrun. The cursor is
    # bound to this exact (term, target, budget) tuple.
    resume = _decode_bodygrep_cursor(
        cursor, term=term, target=normalized_target, budget=budget,
    )
    resume_file = resume[0] if resume else None
    resume_line = resume[1] if resume else 0
    conn = connect(resolve_db_path(repo_root), read_only=True)
    try:
        resume_clause = " AND file_path >= ?" if resume_file is not None else ""
        resume_param: tuple[Any, ...] = (resume_file,) if resume_file is not None else ()
        if normalized_target:
            exact = conn.execute(
                "SELECT 1 FROM files WHERE file_path=? LIMIT 1",
                (normalized_target,),
            ).fetchone()
            if exact:
                query = (
                    "SELECT file_path FROM files WHERE file_path=?" + resume_clause
                    + " ORDER BY file_path LIMIT ?"
                )
                params: tuple[Any, ...] = (
                    normalized_target, *resume_param, scan_file_cap + 1,
                )
            else:
                escaped = (
                    normalized_target.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                query = (
                    "SELECT file_path FROM files WHERE file_path LIKE ? ESCAPE '\\'"
                    + resume_clause
                    + " ORDER BY file_path LIMIT ?"
                )
                params = (f"{escaped}/%", *resume_param, scan_file_cap + 1)
        else:
            where = (" WHERE file_path >= ?" if resume_file is not None else "")
            query = "SELECT file_path FROM files" + where + " ORDER BY file_path LIMIT ?"
            params = (*resume_param, scan_file_cap + 1)
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
    last_scanned_file: str | None = None
    next_cursor: str | None = None
    for file_path in paths:
        candidate = (repo_root / file_path).resolve()
        if not candidate.is_relative_to(repo_root):
            continue
        try:
            raw = candidate.read_bytes()
        except OSError:
            continue
        # Always scan at least one file per page, even one larger than the byte
        # cap, so a cursor can still advance past it -- otherwise a single
        # oversized file would trap the scan on the same offset forever.
        if files_scanned > 0 and bytes_scanned + len(raw) > scan_byte_cap:
            scan_truncated = True
            next_cursor = _encode_bodygrep_cursor(
                file_path, 0, term=term, target=normalized_target, budget=budget,
            )
            break
        bytes_scanned += len(raw)
        files_scanned += 1
        last_scanned_file = file_path
        # Skip lines already returned on a prior page for the resume file.
        start_after = resume_line if file_path == resume_file else 0
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        hit_budget = False
        for line_number, line in enumerate(text.splitlines(), start=1):
            if line_number <= start_after:
                continue
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
                hit_budget = True
                # Resume inside this same file past the last line returned, so
                # remaining matches here are not skipped and not duplicated.
                next_cursor = _encode_bodygrep_cursor(
                    file_path, line_number,
                    term=term, target=normalized_target, budget=budget,
                )
                break
        if hit_budget:
            break
    if next_cursor is None and scan_truncated and last_scanned_file is not None:
        # The scan stopped at the file-count cap with every scanned file
        # complete. Resume strictly past the last scanned file so the caller
        # reaches the files the cap cut off.
        next_cursor = _encode_bodygrep_cursor(
            last_scanned_file, _BODYGREP_SKIP_ALL_LINES,
            term=term, target=normalized_target, budget=budget,
        )
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
        "cursor": cursor,
        "next_cursor": next_cursor,
        "truncated": bool(output_truncated or scan_truncated or next_cursor is not None),
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
    # Top-level freshness is a scalar state; the indexed/on-disk hashes ride on
    # the match itself (``match["freshness"]``) inside the results container.
    freshness = str(match["freshness"]["state"]) if match else "no_match"
    return _fit_payload_bytes({
        "mode": "body", "query": name, "budget": budget,
        "matches": matches,
        "candidate_files": _candidate_files(matches, limit=1),
        "freshness": freshness,
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
    file_freshness: dict[str, dict[str, Any]] = {}
    try:
        matches = func(conn, name, limit=budget)
        for match in matches:
            file_path = str(match.get("file_path") or "")
            fresh = file_freshness.get(file_path)
            if fresh is None:
                fresh = _file_freshness_state(conn, repo_root, file_path)
                file_freshness[file_path] = fresh
            match["freshness"] = fresh
            match["source"] = _source_snippet(
                repo_root, match, fresh=fresh["state"] == "fresh",
            )
    finally:
        conn.close()
    return _fit_payload_bytes({
        "mode": "function", "query": name, "budget": budget,
        "matches": matches,
        "candidate_files": _candidate_files(matches, limit=min(16, budget)),
        "freshness": _overall_freshness(file_freshness),
        "truncated": len(matches) >= budget,
    }, max(512, budget * 512))


def class_query(repo_root: Path, name: str, budget: int = 64) -> dict[str, Any]:
    """Return exact class/struct/enum authorities, including bounded bodies."""

    budget = max(1, min(int(budget), MAX_BUDGET_ROWS))
    conn = connect(resolve_db_path(repo_root), read_only=True)
    file_freshness: dict[str, dict[str, Any]] = {}
    try:
        matches = struct(conn, name, limit=budget)
        for match in matches:
            file_path = str(match.get("file_path") or "")
            fresh = file_freshness.get(file_path)
            if fresh is None:
                fresh = _file_freshness_state(conn, repo_root, file_path)
                file_freshness[file_path] = fresh
            match["freshness"] = fresh
            match["source"] = _source_snippet(
                repo_root, match, fresh=fresh["state"] == "fresh",
            )
    finally:
        conn.close()
    return _fit_payload_bytes({
        "mode": "class", "query": name, "budget": budget,
        "matches": matches,
        "candidate_files": _candidate_files(matches, limit=min(16, budget)),
        "freshness": _overall_freshness(file_freshness),
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
    freshness = _file_freshness_state(conn, repo_root, str(result["file_path"]))
    result["freshness"] = freshness
    # Only a file whose on-disk sha256 still matches the indexed source_hash may
    # be sliced with its stored line numbers. A stale/missing/unverifiable file
    # returns an explicit freshness state and no source, never lines belonging
    # to a different generation.
    snippet = ""
    if freshness["state"] == "fresh":
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
    quality_row = conn.execute("SELECT value FROM meta WHERE key='index_quality'").fetchone()
    roundtrip_row = conn.execute(
        "SELECT value FROM meta WHERE key='recommendation_roundtrip'"
    ).fetchone()
    return {
        "files": file_count, "entities": entity_count, "edges": edge_count,
        "entities_by_kind": by_kind, "edges_by_evidence_label": by_evidence,
        "files_by_status": by_status, "files_by_language": by_language,
        "last_build": json.loads(last_build_row["value"]) if last_build_row else None,
        "index_quality": json.loads(quality_row["value"]) if quality_row else None,
        "recommendation_resolvability": (
            json.loads(roundtrip_row["value"]) if roundtrip_row else None
        ),
    }


def record_recommendation_roundtrip(
    repo_root: Path,
    payload: dict[str, Any],
) -> None:
    """Persist one manager-owned self-check for the current generation."""

    db_path = resolve_db_path(repo_root.resolve())
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('recommendation_roundtrip', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True),),
            )
    finally:
        conn.close()


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
        "coverage",
        "cursor",
        "next_cursor",
        "freshness",
    }

    def encoded_size() -> int:
        return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    while encoded_size() > byte_cap:
        lists: list[tuple[int, list[Any]]] = []
        strings: list[tuple[int, dict[str, Any], str]] = []
        removable: list[tuple[int, dict[str, Any], str]] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in protected_keys:
                        continue
                    removable.append(
                        (len(json.dumps(item, ensure_ascii=False)), value, key)
                    )
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
        if removable:
            # Nothing left is a compressible string or a shrinkable list --
            # every remaining non-protected value is small fixed scaffolding
            # (short strings, already-empty lists, small nested dicts). The
            # byte cap is still a hard requirement, so drop whole fields,
            # largest encoded size first, until the cap is met or only
            # protected keys remain.
            _, owner, key = max(removable, key=lambda item: item[0])
            del owner[key]
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


def _disk_source_hash(repo_root: Path, file_path: str) -> str | None:
    """sha256 of one repository file on disk, or None if unreadable/out of tree."""

    try:
        root = repo_root.resolve()
        target = (root / str(file_path)).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return None
        return sgast.sha256_bytes(target.read_bytes())
    except (OSError, ValueError):
        return None


def _file_freshness_state(
    conn: sqlite3.Connection, repo_root: Path, file_path: str,
) -> dict[str, Any]:
    """Compare the indexed ``source_hash`` against the file on disk.

    ``state`` is ``fresh`` only when the on-disk sha256 still equals the hash
    recorded when the file was indexed. ``stale`` means the file changed since
    indexing (its stored line numbers no longer describe it), ``missing`` that
    the file is gone/unreadable, and ``unknown`` that the index carries no hash
    to check against. A caller reads ``state`` to decide whether the indexed
    line numbers can still be trusted to slice the live file.
    """

    row = conn.execute(
        "SELECT source_hash FROM files WHERE file_path=?", (file_path,),
    ).fetchone()
    stored = None
    if row is not None and row["source_hash"] is not None:
        stored = str(row["source_hash"])
    disk = _disk_source_hash(repo_root, file_path)
    if disk is None:
        state = "missing"
    elif stored is None:
        state = "unknown"
    elif disk == stored:
        state = "fresh"
    else:
        state = "stale"
    return {
        "state": state,
        "indexed_source_hash": stored,
        "disk_source_hash": disk,
    }


def _overall_freshness(per_file: dict[str, dict[str, Any]]) -> str:
    """Collapse per-file freshness into one overall state string.

    The top-level ``freshness`` field is a bare string (not a dict) on purpose:
    a caller reads it directly, and, unlike a nested object, a scalar is never
    miscounted as graph evidence by a downstream zero-hit/hit-count check -- so
    carrying it can never flip an honest scoped miss into a false hit. The rich
    per-file detail (indexed vs on-disk hash) rides on each match/section
    instead, inside a result container where it is already accounted for.

    ``fresh`` only when every file backing the response is fresh; otherwise the
    worst observed state (``stale`` over ``missing`` over ``unknown``) so a
    caller never reads a body-style answer as current when part of it is not.
    """

    states = [entry["state"] for entry in per_file.values()]
    if not states:
        return "unknown"
    if all(state == "fresh" for state in states):
        return "fresh"
    for degraded in ("stale", "missing", "unknown"):
        if degraded in states:
            return degraded
    return "stale"


def _source_snippet(
    repo_root: Path, row: dict[str, Any], *, max_chars: int = 4000, fresh: bool = True,
) -> str:
    # A caller that cannot prove the indexed ``source_hash`` still matches the
    # file on disk must never receive lines sliced with the prior generation's
    # line numbers: return nothing rather than code from a different generation.
    if not fresh:
        return ""
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
    file_freshness: dict[str, dict[str, Any]] = {}
    try:
        exact = conn.execute("SELECT 1 FROM files WHERE file_path=?", (query,)).fetchone()
        matches = find(conn, query, limit=budget)
        files = [query] if exact else _candidate_files(matches, limit=min(8, budget))
        contexts: list[dict[str, Any]] = []
        remaining = budget
        for path in files[:4]:
            item = context(conn, path)
            fresh = _file_freshness_state(conn, repo_root, path)
            file_freshness[path] = fresh
            item["freshness"] = fresh
            per_file = max(1, min(8, remaining))
            item["entities"] = item["entities"][:per_file]
            item["edges"] = item["edges"][:per_file]
            for entity in item["entities"][: min(4, per_file)]:
                if entity.get("kind") in {"function", "method", "class", "struct"}:
                    entity["source"] = _source_snippet(
                        repo_root, {**entity, "file_path": path}, max_chars=800,
                        fresh=fresh["state"] == "fresh",
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
            "freshness": _overall_freshness(file_freshness),
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


_ANALYTICS_RESULT_KEYS: dict[str, tuple[str, ...]] = {
    "tags": ("symbols",),
    "hotspots": ("ranked_symbols",),
    "complexity": ("ranked_symbols",),
    "bottlenecks": ("ranked_symbols",),
    "coverage": ("related_tests",),
    "testmap": ("related_tests",),
    "auditmap": ("audit_queue",),
    "churn": ("files",),
    "ownership": ("files",),
    "calls": ("outgoing_calls", "incoming_calls"),
    "symbols": ("symbols",),
    "reviewqueue": ("queue",),
    "todo": ("todos",),
    "leaks": ("analysis.findings",),
    "nullrisks": ("analysis.findings",),
    "rawptrs": ("analysis.findings",),
    "casts": ("analysis.findings",),
    "crashes": ("analysis.findings",),
    "looprisks": ("analysis.findings",),
    "deadmethods": ("analysis.findings",),
    "duplicates": ("analysis.findings",),
    "gaps": ("low_confidence_edges",),
    "stats": (),
    "summarize": ("files",),
    "pipeline": (),
}

_ANALYTICS_ROW_OWNER_PATH_KEYS: tuple[str, ...] = (
    "file_path", "file", "src_file_path", "src_file",
)


def _analytics_result_row_count(mode: str, payload: dict[str, Any]) -> int:
    total = 0
    for key_path in _ANALYTICS_RESULT_KEYS.get(mode, ()):
        node: Any = payload
        for part in key_path.split("."):
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(part)
        if isinstance(node, list):
            total += len(node)
    return total


def _normalize_analytics_target(target: str | None) -> str:
    if target is None:
        return ""
    normalized = str(target).strip().replace("\\", "/").strip("/")
    parts = Path(normalized).parts
    if not normalized or "\x00" in normalized or Path(str(target)).is_absolute() or ".." in parts:
        raise SourceGraphError("invalid_target")
    return normalized


def _path_in_analytics_scope(file_path: str, scope: str) -> bool:
    normalized = str(file_path).replace("\\", "/")
    return normalized == scope or normalized.startswith(f"{scope}/")


def _analytics_scope_sql_predicate(scope: str) -> tuple[str, tuple[str, str]]:
    """SQL ``WHERE`` fragment + params confining ``file_path`` to ``scope``."""

    escaped = scope.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return (
        "(file_path = ? OR file_path LIKE ? ESCAPE '\\')",
        (scope, f"{escaped}/%"),
    )


def _scoped_entity_rows(
    conn: sqlite3.Connection, scope: str, *, limit: int,
) -> list[dict[str, Any]]:
    """Bounded entity corpus for analytics, optionally confined to ``scope``."""

    cap = max(1, limit)
    # ``module``/``file`` rows are containers, never a renderable analytic
    # subject for any mode -- excluding them keeps ``eligible``/``scanned``
    # aligned with what a mode can actually return, instead of inflating
    # coverage with rows no mode will ever emit.
    select = (
        "SELECT file_path, kind, name, qualname, line_start, line_end, signature, "
        "evidence_label, confidence FROM entities WHERE kind NOT IN ('file', 'module') "
    )
    order_by = (
        "ORDER BY CASE WHEN kind IN ('function','method') THEN 0 ELSE 1 END, "
        "file_path, line_start, qualname LIMIT ?"
    )
    if scope:
        predicate, params = _analytics_scope_sql_predicate(scope)
        rows = conn.execute(
            select + f"AND {predicate} " + order_by, (*params, cap),
        ).fetchall()
    else:
        rows = conn.execute(select + order_by, (cap,)).fetchall()
    return [dict(row) for row in rows]


def _scoped_repo_aggregates(conn: sqlite3.Connection, scope: str) -> dict[str, Any]:
    """Truthful ``files_by_language``/``entities_by_kind``/``edges`` for ``scope``.

    Computed directly against the same ``files``/``entities``/``edges``
    tables a per-mode analytic's own (potentially repository-wide)
    aggregation draws from, confined to ``scope`` by owning ``file_path`` --
    the engine's own scope boundary, not a name-keyed patch applied after
    the fact. Edge scope follows the *owning* file only (the file the edge
    was recorded against), so an import/call edge owned by an in-scope file
    is kept even when its target (``dst_name``/``dst_qualname``) lives
    outside scope -- excluding it would understate what the in-scope file
    actually does.
    """

    predicate, params = _analytics_scope_sql_predicate(scope)
    files_by_language = dict(conn.execute(
        f"SELECT language, COUNT(*) FROM files WHERE {predicate} GROUP BY language", params,
    ).fetchall())
    entities_by_kind = dict(conn.execute(
        f"SELECT kind, COUNT(*) FROM entities WHERE {predicate} GROUP BY kind", params,
    ).fetchall())
    (edge_count,) = conn.execute(
        f"SELECT COUNT(*) FROM edges WHERE {predicate}", params,
    ).fetchone()
    return {
        "files_by_language": files_by_language,
        "entities_by_kind": entities_by_kind,
        "edges": edge_count,
    }


def _analytics_cursor_digest(mode: str, query: str, target: str, budget: int) -> str:
    payload = f"{mode}\x1f{query}\x1f{target}\x1f{budget}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _encode_analytics_cursor(
    offset: int, *, mode: str, query: str, target: str, budget: int,
) -> str:
    return f"{offset}:{_analytics_cursor_digest(mode, query, target, budget)}"


def _decode_analytics_cursor(
    cursor: str | None, *, mode: str, query: str, target: str, budget: int,
) -> int:
    if cursor is None:
        return 0
    raw = str(cursor).strip()
    if not raw:
        return 0
    offset_text, separator, digest = raw.partition(":")
    if not separator or not digest or not offset_text.isdigit():
        raise SourceGraphError("invalid_cursor")
    if digest != _analytics_cursor_digest(mode, query, target, budget):
        raise SourceGraphError("invalid_cursor")
    return int(offset_text)


def _analytics_row_in_scope(row: Any, scope: str) -> bool:
    """True when ``row`` belongs to ``scope`` by its *owning* file only.

    Only the file that recorded/owns this row (``file_path``/``src_file*``)
    decides scope. A row's *target* (``dst_file_path``/``dst_file``) is
    never used to disqualify it: an import or call edge legitimately
    points outside the scope it was recorded in (an in-scope file
    importing/calling an out-of-scope symbol), and that must still surface
    as scoped evidence for the file that owns it -- requiring the target to
    also be in scope would silently drop real out-of-scope import/call
    edges instead of reporting them.
    """

    if not isinstance(row, dict):
        return True
    found = [
        str(row[key]) for key in _ANALYTICS_ROW_OWNER_PATH_KEYS
        if isinstance(row.get(key), str) and row.get(key)
    ]
    if not found:
        return True
    return all(_path_in_analytics_scope(path, scope) for path in found)


def _scrub_analytics_repo_wide_counts(
    node: Any, corpus: list[dict[str, Any]], aggregates: dict[str, Any],
) -> None:
    """Recursively retarget any repo-wide analytic aggregate to scope.

    A per-mode analytic frequently nests a repository-wide summary object
    (e.g. ``{"repository": {"files": N, "entities": M, "edges": K,
    "files_by_language": {...}, "entities_by_kind": {...}}}``) anywhere in
    its payload, not just at the top level. Wherever a ``files``/``entities``
    int counter, a ``files_by_language``/``entities_by_kind`` breakdown, or
    an ``edges`` int total appears, by name, it is replaced with the
    truthful value for the already-scoped ``corpus``/``aggregates`` -- the
    engine's sole source of scope truth -- so nesting can never smuggle a
    repository-wide number past the scope this call actually requested.
    """

    if isinstance(node, dict):
        if isinstance(node.get("files"), int):
            node["files"] = len({
                str(row.get("file_path")) for row in corpus if row.get("file_path")
            })
        if isinstance(node.get("entities"), int):
            node["entities"] = len(corpus)
        if isinstance(node.get("files_by_language"), dict):
            node["files_by_language"] = dict(aggregates["files_by_language"])
        if isinstance(node.get("entities_by_kind"), dict):
            node["entities_by_kind"] = dict(aggregates["entities_by_kind"])
        if isinstance(node.get("edges"), int):
            node["edges"] = aggregates["edges"]
        for value in node.values():
            _scrub_analytics_repo_wide_counts(value, corpus, aggregates)
    elif isinstance(node, list):
        for item in node:
            _scrub_analytics_repo_wide_counts(item, corpus, aggregates)


def _enforce_analytics_target_scope(
    payload: dict[str, Any], mode: str, corpus: list[dict[str, Any]], scope: str,
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Re-assert engine-owned scope over a per-mode analytic's own payload.

    A per-mode analytic may compute some fields (e.g. repository-wide
    aggregates or edge scans) without consulting the already-scoped
    ``matches`` it was handed. This is the engine's last word: any
    row-shaped list registered for this mode is filtered back down to
    ``scope`` by owning file, and any nested repo-wide aggregate (counts,
    ``files_by_language``, ``entities_by_kind``, ``edges``) is replaced with
    the truthful value for the same scoped corpus/DB boundary every mode
    shares, so a caller can never observe a wider scope than it explicitly
    requested.
    """

    if not scope:
        return payload
    for key_path in _ANALYTICS_RESULT_KEYS.get(mode, ()):
        parts = key_path.split(".")
        node: Any = payload
        for part in parts[:-1]:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(part)
        if not isinstance(node, dict):
            continue
        last = parts[-1]
        value = node.get(last)
        if isinstance(value, list):
            node[last] = [row for row in value if _analytics_row_in_scope(row, scope)]
    _scrub_analytics_repo_wide_counts(payload, corpus, _scoped_repo_aggregates(conn, scope))
    return payload


def analytics_query(
    repo_root: Path,
    mode: str,
    query: str,
    budget: int = 64,
    *,
    target: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Run one repository-neutral analytic mode over canonical graph rows.

    ``target``, when given, is an exact bounded path/prefix scope enforced
    inside the engine: every candidate row considered for this call is
    already confined to that scope before the per-mode analytic ever runs,
    and any row or nested count a per-mode analytic still reports outside
    that scope is stripped or corrected back to scope afterward -- so no
    downstream caller can widen it or observe a conflicting second filter.
    ``cursor`` deterministically pages through that same scoped,
    budget-bounded corpus, but only for a mode whose result is an actual
    row list AND whose current page truthfully returned at least one row:
    a mode that only ever produces one aggregate snapshot never mints or
    accepts a cursor (a second page would just repeat the same snapshot),
    and a page that returned nothing never advances into a further page
    either (repeating that same empty result). A cursor is only valid for
    the exact ``(mode, query, target, budget)`` tuple that minted it. The
    returned ``coverage`` block reports how many rows were actually
    scanned/eligible in scope, how many this page truthfully returned,
    and the budget this call actually resolved to (``effective_budget``,
    always equal to ``returned``) alongside what was asked for
    (``requested_budget``).
    """

    if mode not in sganalytics.ANALYTIC_MODES:
        raise SourceGraphError(f"invalid_analytic_mode:{mode}")
    requested_budget = int(budget)
    budget = max(1, min(requested_budget, MAX_BUDGET_ROWS))
    byte_cap = max(512, budget * 768)
    normalized_target = _normalize_analytics_target(target)
    is_pageable_mode = bool(_ANALYTICS_RESULT_KEYS.get(mode))
    conn = connect(resolve_db_path(repo_root), read_only=True)
    try:
        offset = _decode_analytics_cursor(
            cursor, mode=mode, query=query, target=normalized_target, budget=budget,
        )
        if offset and not is_pageable_mode:
            # This mode never mints a cursor (its result is one aggregate
            # snapshot, not a row page), so a nonzero offset can only come
            # from a forged or stale cursor.
            raise SourceGraphError("invalid_cursor")
        # ``find`` re-clamps its own limit to MAX_BUDGET_ROWS, so a find-backed
        # corpus is really bounded there, not at MAX_ANALYTICS_CORPUS_ROWS. Track
        # the cap that actually bounded this corpus so ``eligible_capped`` reports
        # whether the retrieval was truncated, whatever the effective clamp was --
        # not a MAX_ANALYTICS_CORPUS_ROWS threshold that find() can never reach.
        find_rows = find(conn, query, limit=MAX_ANALYTICS_CORPUS_ROWS)
        corpus_cap = min(MAX_ANALYTICS_CORPUS_ROWS, MAX_BUDGET_ROWS)
        corpus_capped = len(find_rows) >= corpus_cap
        if normalized_target:
            corpus = [
                row for row in find_rows
                if _path_in_analytics_scope(str(row.get("file_path") or ""), normalized_target)
            ]
        else:
            corpus = find_rows
        if not corpus:
            corpus = _scoped_entity_rows(
                conn, normalized_target, limit=MAX_ANALYTICS_CORPUS_ROWS,
            )
            # The scoped-entity fallback honours the full MAX_ANALYTICS_CORPUS_ROWS
            # limit (it does not route through find's tighter clamp).
            corpus_cap = MAX_ANALYTICS_CORPUS_ROWS
            corpus_capped = len(corpus) >= corpus_cap
        total_eligible = len(corpus)
        if offset and offset >= total_eligible:
            raise SourceGraphError("invalid_cursor")
        page = corpus[offset:offset + budget] if is_pageable_mode else corpus[:budget]
        page_len = len(page)
        pending_next_offset = offset + page_len
        if normalized_target and not corpus:
            # An explicit scope with zero eligible rows must stay empty --
            # never let the per-mode analytic's own no-match fallback widen
            # the response back out to a repository-wide default.
            payload: dict[str, Any] = {
                "mode": mode, "query": query, "budget": budget,
                "scope": "target_scope_empty",
            }
        else:
            payload = sganalytics.query(
                conn, repo_root, mode=mode, query_text=query,
                matches=page, budget=budget,
            )
            payload = _enforce_analytics_target_scope(payload, mode, corpus, normalized_target, conn)
        payload["target"] = normalized_target or None
        # Reserve room for the coverage/cursor block BEFORE fitting content
        # to the byte cap, then fit the fully assembled payload (content
        # plus coverage/cursor) to the cap again as a final guarantee --
        # so the cap describes what is actually serialized on the wire,
        # not just the content that existed before coverage/cursor were
        # attached (coverage/cursor themselves stay protected from trim).
        # ``returned``/``next_cursor`` are not known until the payload is
        # assembled, so the reserve uses a same-shape placeholder cursor
        # (an offset no smaller than any real one this call could mint).
        placeholder_cursor = _encode_analytics_cursor(
            total_eligible, mode=mode, query=query, target=normalized_target, budget=budget,
        )
        reserve_shell = {
            "cursor": cursor,
            "next_cursor": placeholder_cursor,
            "coverage": {
                "scanned": total_eligible,
                "eligible": total_eligible,
                "eligible_capped": corpus_capped,
                "returned": 0,
                "requested_budget": requested_budget,
                "effective_budget": 0,
            },
        }
        reserve_bytes = len(json.dumps(reserve_shell, ensure_ascii=False).encode("utf-8"))
        content_cap = max(256, byte_cap - reserve_bytes)
        pre_fit_truncated = bool(payload.get("truncated"))
        payload = _fit_payload_bytes(payload, content_cap)
        byte_trimmed = bool(payload.get("truncated")) and not pre_fit_truncated
        returned = _analytics_result_row_count(mode, payload)
        analysis = payload.get("analysis") if isinstance(payload, dict) else None
        # ``scanned`` must equal what the analytic really examined, never the
        # whole eligible corpus. Every mode is handed only ``page`` (at most
        # ``budget`` rows sliced from ``corpus`` above), so no single call can
        # examine more of the corpus than that page. A per-symbol filter mode
        # reports its own ``analysis.symbols_scanned`` (the exact page rows it
        # read); every other mode -- the page-sliced tags/hotspots/complexity/
        # bottlenecks views and the aggregate snapshots alike -- ranks or
        # aggregates over that same ``page``, so ``page_len`` is the truthful
        # examined count for all of them.
        scans_per_symbol = (
            isinstance(analysis, dict) and isinstance(analysis.get("symbols_scanned"), int)
        )
        examined = int(analysis["symbols_scanned"]) if scans_per_symbol else page_len
        # Mint a cursor only for a mode that genuinely pages the corpus row by
        # row: a slice mode still emitting rows this page, or a per-symbol
        # scanner that examined this page (and may find nothing on a clean page
        # yet still have eligible rows to scan past it). A whole-scope aggregate
        # returns the same result for every page, so it must never paginate into
        # a duplicate. The old ``returned > 0`` gate also stopped a clean filter
        # page, stranding every eligible symbol past it; ``scans_per_symbol``
        # restores forward progress there while still refusing an aggregate's
        # duplicate second page. Issuance then follows from eligible rows alone.
        paginates_corpus = scans_per_symbol or returned > 0
        next_cursor = (
            _encode_analytics_cursor(
                pending_next_offset, mode=mode, query=query,
                target=normalized_target, budget=budget,
            )
            if is_pageable_mode and paginates_corpus and pending_next_offset < total_eligible
            else None
        )
        payload["cursor"] = cursor
        payload["next_cursor"] = next_cursor
        payload["coverage"] = {
            "scanned": examined,
            "eligible": total_eligible,
            "eligible_capped": corpus_capped,
            "returned": returned,
            "requested_budget": requested_budget,
            "effective_budget": returned,
        }
        payload["truncated"] = next_cursor is not None or byte_trimmed
        payload = _fit_payload_bytes(payload, byte_cap)
        return payload
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
        file_freshness: dict[str, dict[str, Any]] = {}
        for match in matches:
            if remaining <= 0:
                break
            if match["file_path"] in seen_files:
                continue
            seen_files.add(match["file_path"])
            ctx = context(conn, match["file_path"])
            fresh = _file_freshness_state(conn, repo_root, match["file_path"])
            file_freshness[match["file_path"]] = fresh
            ctx["freshness"] = fresh
            ctx["entities"] = ctx["entities"][: max(1, remaining)]
            for entity in ctx["entities"][: min(4, remaining)]:
                if entity.get("kind") in {"function", "method", "class", "struct"}:
                    entity["source"] = _source_snippet(
                        repo_root, {**entity, "file_path": match["file_path"]}, max_chars=2400,
                        fresh=fresh["state"] == "fresh",
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
            "freshness": _overall_freshness(file_freshness),
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


# ---------------------------------------------------------------------------
# NF15 single-file index / remove (transactional, fail-closed, generation-safe)
# ---------------------------------------------------------------------------

def _validate_single_file_path(repo_root: Path, path: str) -> Path:
    """Validate and resolve a single relative file path for index/remove.

    Rejects absolute, traversal (..), symlink (lexically before resolve),
    out-of-repo, and excluded paths. Returns the resolved absolute path.
    """
    repo_root = repo_root.resolve()
    if not isinstance(path, str):
        raise SourceGraphError(
            f"source_graph_single_file_path_not_string:{type(path).__name__}"
        )
    if "\x00" in path:
        raise SourceGraphError("source_graph_single_file_path_null_byte")
    rel = Path(path)
    # --- Lexical checks before any filesystem access ---
    # ``WindowsPath('/etc/passwd')`` is rooted but not considered absolute
    # because it has no drive. Treat both path dialects as untrusted input so
    # a POSIX absolute path is rejected consistently on Windows too.
    if rel.is_absolute() or PurePosixPath(path).is_absolute():
        raise SourceGraphError(
            f"source_graph_single_file_absolute:{path}"
        )
    # Windows drive/UNC absolute path: PurePosixPath.is_absolute
    # ignores drive letters on POSIX; PureWindowsPath recognizes
    # them so that "C:\\..." and "//server/share/..." are caught.
    try:
        win_abs = PureWindowsPath(path).is_absolute()
    except Exception:
        pass
    else:
        if win_abs:
            raise SourceGraphError(
                f"source_graph_single_file_absolute:{path}"
            )
    if ".." in rel.parts:
        raise SourceGraphError(
            f"source_graph_single_file_traversal:{path}"
        )
    # Check for symlink components lexically before resolve.
    if _has_symlink_component(repo_root / rel):
        raise SourceGraphError(
            f"source_graph_single_file_symlink:{path}"
        )
    # Resolve and verify containment.
    try:
        candidate = (repo_root / rel).resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SourceGraphError(
            f"source_graph_single_file_unresolvable:{path}"
        ) from exc
    if repo_root not in candidate.parents and candidate != repo_root:
        raise SourceGraphError(
            f"source_graph_single_file_outside_repository:{path}"
        )
    # Check exclude dirs and globs.
    policy = load_ignore_policy(repo_root)
    rel_posix = rel.as_posix()
    # Check if any parent directory is excluded.
    for part in rel_posix.split("/")[:-1]:
        if part in policy.exclude_dirs or part.endswith(".egg-info"):
            raise SourceGraphError(
                f"source_graph_single_file_excluded_dir:{part}"
            )
    if _glob_ignored(rel_posix, policy.exclude_globs):
        raise SourceGraphError(
            f"source_graph_single_file_excluded_glob:{rel_posix}"
        )
    return candidate


def index_file(repo_root: Path, path: str, expected_hash: str) -> dict[str, Any]:
    """Index exactly one file into the canonical Source Graph.

    The file must already exist on disk, must pass the bounded path-safety
    checks, must be an indexed extension, and its sha256 must match
    ``expected_hash``. Everything else fails closed. Only the target file's
    rows are mutated inside a single exclusive transaction; all other rows
    and the generation authority are preserved unchanged.

    Returns a summary dict with ``ok``, ``file_path``, ``source_hash``,
    ``language``, ``status``, ``entities``, and ``edges``.
    """
    repo_root = repo_root.resolve()
    resolved = _validate_single_file_path(repo_root, path)
    rel = Path(path).as_posix()

    # Reject unsupported extensions (no LanguageSpec entry).
    suffix = resolved.suffix.casefold()
    if suffix not in _INDEXED_EXTENSION_SET:
        raise SourceGraphError(
            f"source_graph_single_file_unsupported_extension:{suffix}"
        )

    # Compute the actual file hash.
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise SourceGraphError(
            f"source_graph_single_file_unreadable:{path}"
        ) from exc
    source_hash = sgast.sha256_bytes(raw)

    # Fail closed on hash mismatch.
    if not isinstance(expected_hash, str) or not expected_hash:
        raise SourceGraphError(
            "source_graph_single_file_expected_hash_required"
        )
    if source_hash != expected_hash:
        raise SourceGraphError(
            f"source_graph_single_file_hash_mismatch:"
            f"expected={expected_hash[:16]} actual={source_hash[:16]}"
        )

    # Extraction.
    extraction = sgast.extract_file(repo_root, resolved, build_revision=BUILD_REVISION)
    if extraction.file_path != rel:
        raise SourceGraphError(
            f"source_graph_single_file_path_mismatch:"
            f"expected={rel} returned={extraction.file_path}"
        )

    # Acquire writer lease, open connection, mutate exactly one file.
    with index_write_lease(repo_root) as acquired:
        if not acquired:
            raise SourceGraphBuildInProgressError(
                f"source_graph_build_in_progress:{repo_root}"
            )
        db_path = resolve_db_path(repo_root)
        conn = connect(db_path)
        try:
            with conn:
                _invalidate_file(conn, rel)
                try:
                    path_stat = resolved.stat()
                    file_size = int(path_stat.st_size)
                    mtime_ns = int(path_stat.st_mtime_ns)
                except OSError:
                    file_size = -1
                    mtime_ns = -1
                inserted_entities, inserted_edges, _dropped = _write_extraction(
                    conn, extraction, file_size=file_size, mtime_ns=mtime_ns,
                )
                # Re-run cross-file edge resolution exactly as a full build's
                # tail does. Re-extracting one file drops its own edges back to
                # unresolved and can invalidate edges other files aimed at it; a
                # later incremental build sees changed==0 and skips resolution,
                # so leaving it unresolved here would strand those edges
                # permanently. Resolution is idempotent and generation-safe.
                _resolve_cpp_cross_file_edges(conn)
                _resolve_python_imported_calls(conn, repo_root)
                _resolve_javascript_import_bindings(conn)
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('single_file_last_mutation', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps({
                        "finished_at": _now_iso(),
                        "file_path": rel,
                        "source_hash": source_hash,
                        "operation": "index",
                        "build_revision": BUILD_REVISION,
                    }),),
                )
        finally:
            conn.close()
    return {
        "ok": True,
        "file_path": rel,
        "source_hash": source_hash,
        "language": extraction.language,
        "status": extraction.status,
        "entities": inserted_entities,
        "edges": inserted_edges,
    }


def remove_file(repo_root: Path, path: str) -> dict[str, Any]:
    """Remove exactly one file from the canonical Source Graph.

    The path must pass the same bounded path-safety checks as ``index_file``.
    Removal is idempotent: when the file is not in the index, the call still
    succeeds without error. Only the target file's rows are mutated inside a
    single exclusive transaction.
    """
    repo_root = repo_root.resolve()
    _validate_single_file_path(repo_root, path)
    rel = Path(path).as_posix()

    with index_write_lease(repo_root) as acquired:
        if not acquired:
            raise SourceGraphBuildInProgressError(
                f"source_graph_build_in_progress:{repo_root}"
            )
        db_path = resolve_db_path(repo_root)
        conn = connect(db_path)
        try:
            with conn:
                # Count before we delete.
                entity_count = conn.execute(
                    "SELECT COUNT(*) FROM entities WHERE file_path=?", (rel,)
                ).fetchone()[0]
                _invalidate_file(conn, rel)
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('single_file_last_mutation', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps({
                        "finished_at": _now_iso(),
                        "file_path": rel,
                        "operation": "remove",
                        "build_revision": BUILD_REVISION,
                    }),),
                )
        finally:
            conn.close()
    return {
        "ok": True,
        "file_path": rel,
        "removed_entities": entity_count,
    }


def _has_symlink_component(path: Path) -> bool:
    """Check for symlink components in path, lexically before resolve."""
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts[len(current.parts):]:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


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
    "index_file",
    "remove_file",
    "slice_",
    "struct",
    "summary",
    "trace",
    "neighbors",
    "record_recommendation_roundtrip",
    "resolve_db_path",
    "shortest_path",
]
