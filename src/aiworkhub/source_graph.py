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
import ast
import concurrent.futures
import errno
import fnmatch
import hashlib
import io
import json
import multiprocessing
import os
import functools
import re
import secrets
import shlex
import shutil
import sqlite3
import stat
import sys
import time
import tokenize
from collections import deque
from concurrent.futures.process import BrokenProcessPool
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from . import parallelism
from . import platform_io
from .platform_io import PublicationDurabilityError
from . import source_graph_ast as sgast
from . import source_graph_analytics as sganalytics
from . import source_graph_insights as sginsights
from . import source_graph_languages as sglanguages
from . import source_graph_lsp as sglsp
from .sqlite_readonly import connect_readonly
from .repository_state import HUB_DIRNAME, RepositoryStateError, inspect_repository
from .storage_registry import (
    StorageRegistryError,
    load_storage_registry,
    resolve_database_path,
)


def atomic_replace(source: Path, destination: Path) -> None:
    """Publish a probed generation through the strongest host primitive.

    POSIX hosts can bind publication to an open file identity.  Windows does
    not yet expose that descriptor-relative primitive here, but it does provide
    atomic path replacement; the Source Graph staging name is private,
    unpredictable, opened with ``O_EXCL``, and re-verified immediately before
    this call.  Failing the whole Windows index after that proof made the
    cross-platform runtime unusable without adding any protection.
    """

    if platform_io.is_windows():
        platform_io.durable_atomic_replace(source, destination)
    else:
        platform_io.identity_bound_durable_atomic_replace(source, destination)

SCHEMA_ID = "aiworkhub.source_graph.v1"
BUILD_REVISION = "aiworkhub.source_graph.semantic.v6"
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
SOURCE_GRAPH_AUTHENTICATED_FILE_BYTE_LIMIT = 64 * 1024 * 1024
PYTHON_IMPORT_REPARSE_MAX_NODES = 100_000
PYTHON_IMPORT_REPARSE_MAX_DEPTH = 256
PYTHON_IMPORT_REPARSE_MAX_SOURCE_CHARS = 4 * 1024 * 1024
PYTHON_IMPORT_REPARSE_MAX_TOKENS = 25_000
_PYTHON_DOTTED_CALL_RE = re.compile(
    r"\b([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)+)\s*\("
)
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
CREATE INDEX IF NOT EXISTS idx_entities_qualname ON entities(qualname);

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
    ,source_col INTEGER NOT NULL DEFAULT -1
    ,receiver_name TEXT NOT NULL DEFAULT ''
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

-- Task 3: durable per-edge LSP provenance. Migration-safe by construction --
-- ``connect`` runs this script on every write open, so a generation
-- published before the feature existed gains the tables the first time a
-- writer touches it, and read-only callers check for them before querying.
-- ``prior_evidence_label``/``prior_confidence`` record what lexical
-- extraction produced, so revoking a binding restores the edge exactly.
-- ``edge_identity`` names the one edge at the position a binding owns (see
-- ``_lsp_edge_identity``). ``connect`` adds it to an older table, whose rows
-- keep it empty: legacy provenance that recorded only a position.
CREATE TABLE IF NOT EXISTS lsp_edge_provenance (
    source_path TEXT NOT NULL,
    source_line INTEGER NOT NULL,
    source_column INTEGER NOT NULL,
    source_hash TEXT NOT NULL,
    edge_identity TEXT NOT NULL DEFAULT '',
    target_path TEXT NOT NULL,
    target_hash TEXT NOT NULL,
    target_qualname TEXT NOT NULL,
    target_name TEXT NOT NULL,
    target_line_start INTEGER NOT NULL,
    target_range TEXT NOT NULL,
    prior_evidence_label TEXT NOT NULL DEFAULT '',
    prior_confidence REAL NOT NULL DEFAULT 0.0,
    server_command TEXT NOT NULL,
    server_version TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    result_config_digest TEXT NOT NULL DEFAULT '',
    classification TEXT NOT NULL,
    latency_ms INTEGER NOT NULL DEFAULT -1,
    resolved_at TEXT NOT NULL,
    schema_id TEXT NOT NULL,
    PRIMARY KEY(source_path, source_line, source_column)
);
CREATE INDEX IF NOT EXISTS idx_lsp_provenance_target
    ON lsp_edge_provenance(target_path);

CREATE TABLE IF NOT EXISTS lsp_file_receipt (
    source_path TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL,
    language TEXT NOT NULL,
    server_command TEXT NOT NULL,
    server_version TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    edge_count INTEGER NOT NULL DEFAULT 0,
    -- 1 only when every position was asked and every answer arrived; an
    -- incomplete receipt keeps its verified bindings but is never reused.
    complete INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT -1,
    resolved_at TEXT NOT NULL,
    schema_id TEXT NOT NULL
);
"""


class SourceGraphError(RuntimeError):
    """Base class for canonical Source Graph failures."""


class RepositoryUnresolvedError(SourceGraphError):
    """The repository identity could not be resolved (no manifest/registry)."""


class SourceGraphBuildInProgressError(SourceGraphError):
    """Another process currently owns this repository's index writer lease."""


class SourceGraphLockUnavailableError(SourceGraphError):
    """Advisory lock failed for a reason other than genuine contention."""

    def __init__(
        self,
        *,
        operation: str,
        errno_value: int | None,
        path: str,
        phase: str = "acquire",
    ) -> None:
        self.operation = operation
        self.errno = errno_value
        self.path = path
        self.phase = phase
        if errno_value is None:
            self.errno_name = "unknown"
        else:
            self.errno_name = errno.errorcode.get(errno_value, str(errno_value))
        super().__init__(
            "source_graph_lock_unavailable:"
            f"phase={phase} operation={operation} "
            f"errno={self.errno_name} path={path}"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "reason": "source_graph_lock_unavailable",
            "phase": self.phase,
            "operation": self.operation,
            "errno": self.errno_name,
            "errno_value": self.errno,
            "path": self.path,
        }


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
        # A partition database carries a composed-view marker naming its base
        # index; a read-only open of it attaches that base (read-only) and
        # installs precedence views so the reviewer queries a candidate-accurate
        # composed index without any full-index clone. An ordinary Source Graph
        # database has no marker and is left exactly as connect_readonly opened
        # it. A missing/shifted base fails closed rather than serving a partial.
        from . import source_graph_partition as _sgp
        _sgp.attach_composed_base_if_marked(conn, db_path)
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
        edge_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(edges)")
        }
        if "source_col" not in edge_columns:
            conn.execute(
                "ALTER TABLE edges ADD COLUMN source_col INTEGER NOT NULL DEFAULT -1"
            )
        if "receiver_name" not in edge_columns:
            conn.execute(
                "ALTER TABLE edges ADD COLUMN receiver_name TEXT NOT NULL DEFAULT ''"
            )
        provenance_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(lsp_edge_provenance)")
        }
        if "edge_identity" not in provenance_columns:
            conn.execute(
                "ALTER TABLE lsp_edge_provenance "
                "ADD COLUMN edge_identity TEXT NOT NULL DEFAULT ''"
            )
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


_LOCK_CONTENTION_ERRNOS = {errno.EACCES, errno.EAGAIN}
for _lock_errno_name in ("EWOULDBLOCK", "EDEADLK", "EDEADLOCK"):
    _lock_errno_value = getattr(errno, _lock_errno_name, None)
    if _lock_errno_value is not None:
        _LOCK_CONTENTION_ERRNOS.add(_lock_errno_value)


def _portable_lock_path(repo_root: Path, lock_path: Path) -> str:
    try:
        return lock_path.resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except ValueError:
        return lock_path.name


def _is_lock_contention(exc: BaseException) -> bool:
    if isinstance(exc, BlockingIOError):
        return True
    if not isinstance(exc, OSError):
        return False
    return exc.errno in _LOCK_CONTENTION_ERRNOS


def _lock_unavailable(
    exc: OSError, *, repo_root: Path, lock_path: Path, operation: str
) -> SourceGraphLockUnavailableError:
    return SourceGraphLockUnavailableError(
        operation=operation,
        errno_value=getattr(exc, "errno", None),
        path=_portable_lock_path(repo_root, lock_path),
        phase="acquire",
    )


def _index_write_lease_platform() -> str:
    return os.name


@contextmanager
def index_write_lease(repo_root: Path):
    """Try to own the single cross-process writer lease for ``repo_root``.

    OS advisory locks are released automatically when a process exits, so a
    crashed/reloaded VS Code child cannot leave stale ownership. The lock is
    repository-local and therefore preserves multi-repository isolation.
    Genuine nonblocking contention yields False; unsupported or broken lock
    backends raise SourceGraphLockUnavailableError and never proceed unlocked.
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
            if _index_write_lease_platform() == "nt":
                import msvcrt

                # Windows may retain a byte-range lock for a very short window
                # while another build handle is closing. Avoid surfacing that
                # release race as a false permanent build-in-progress state,
                # while remaining bounded when another writer is genuinely
                # active. Non-contention failures fail immediately.
                deadline = time.monotonic() + 1.0
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if not _is_lock_contention(exc):
                            raise _lock_unavailable(
                                exc,
                                repo_root=repo_root,
                                lock_path=lock_path,
                                operation="msvcrt.locking",
                            ) from exc
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.01)
                        handle.seek(0)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        except OSError as exc:
            if _is_lock_contention(exc):
                acquired = False
            else:
                raise _lock_unavailable(
                    exc,
                    repo_root=repo_root,
                    lock_path=lock_path,
                    operation=(
                        "msvcrt.locking"
                        if _index_write_lease_platform() == "nt"
                        else "flock"
                    ),
                ) from exc
        yield acquired
    finally:
        if acquired:
            try:
                handle.seek(0)
                if _index_write_lease_platform() == "nt":
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


def _is_repository_root_generated_data_jsonl(relative_path: str) -> bool:
    rel = relative_path.replace("\\", "/").strip("/")
    return rel.startswith("data/") and rel.casefold().endswith(".jsonl")


def iter_source_files(repo_root: Path) -> list[Path]:
    repo_root = repo_root.resolve()
    policy = load_ignore_policy(repo_root)
    out: list[Path] = []
    traversal_errors: list[str] = []
    indexed_extensions = policy.indexed_extensions

    def capture_traversal_error(exc: OSError) -> None:
        location = str(exc.filename or repo_root)
        try:
            location = Path(location).relative_to(repo_root).as_posix()
        except ValueError:
            pass
        location = location.replace("\n", " ")[:256]
        detail = str(exc).replace("\n", " ")[:512]
        traversal_errors.append(f"{location}: {detail or type(exc).__name__}")

    for current, dirnames, filenames in os.walk(
        repo_root,
        followlinks=False,
        onerror=capture_traversal_error,
    ):
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
    if traversal_errors:
        evidence = "; ".join(traversal_errors[:8])[:2048]
        raise SourceGraphError(f"source_graph_traversal_error:{evidence}")
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
    phase_seconds: dict[str, float] = field(default_factory=dict)

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
            "phase_seconds": self.phase_seconds,
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


# How far below the best ratio ever observed counts as degraded. Same width as
# the build-over-build rule, so one step of decay is tolerated and a drift away
# from the best is not.
_RESOLVED_RATIO_FLOOR_TOLERANCE = 0.05
_RESOLVED_RATIO_BEST_KEY = "resolved_ratio_best_observed"


def _resolved_ratio_high_water_mark(
    previous: dict[str, Any] | None, current: float | None
) -> float | None:
    """Return the best resolved ratio observed, raised if this build beat it.

    Pure: the mark rides in the scorecard record that is already persisted and
    already handed back as ``previous``, so nothing new is stored and this
    function stays a measurement rather than a write.

    The mark only ever rises. A build that resolves less than before must not
    quietly redefine what "before" was -- that is precisely how a ratchet
    stops ratcheting.
    """

    recorded: float | None = None
    raw = ((previous or {}).get("edges") or {}).get(_RESOLVED_RATIO_BEST_KEY)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        recorded = float(raw)
    if current is None:
        return recorded
    if recorded is None or current > recorded:
        return current
    return recorded


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
        "file_path LIKE 'build/%' OR file_path LIKE 'data/%'"
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

    # A call to a language builtin can never resolve to repository code, and no
    # lens uses one: deadmethods counts only RESOLVED incoming calls, and gaps
    # has to filter them out explicitly. Measured here, 47,046 of 212,603 call
    # edges name a Python builtin -- str 11,970, isinstance 4,834, setattr
    # 4,512, len 4,382 -- so resolved_ratio was reporting a fifth of the graph
    # as unresolved work when it was never work at all.
    #
    # They are reported out of the denominator, not deleted: an edge saying
    # "this function calls str" is true, and destroying true rows to improve a
    # number is how a metric starts steering the data instead of describing it.
    # resolved_ratio itself is left exactly as it was, so history stays
    # comparable and the floor keeps guarding the same series.
    builtin_names = sorted(sganalytics._PYTHON_BUILTIN_NAMES)
    unresolvable_edges = int(conn.execute(
        "SELECT COUNT(*) FROM edges WHERE kind='calls' AND dst_qualname IS NULL "
        f"AND dst_name IN ({','.join('?' * len(builtin_names))})",
        builtin_names,
    ).fetchone()[0]) if builtin_names else 0
    resolvable_edges = max(0, total_edges - unresolvable_edges)
    resolved_ratio_resolvable = (
        resolved_edges / resolvable_edges if resolvable_edges else None
    )
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

    # The delta rule alone is blind twice over: a decay of four points per
    # build never trips it however far it travels, and a FIRST build has no
    # previous at all, so an index that comes up at two percent resolution
    # reports healthy. The floor closes both, and it is observed rather than
    # invented -- the best ratio this repository has ever reached, recorded as
    # a high-water mark and never lowered. Nothing is bounded here that was not
    # first measured.
    resolved_ratio_best = _resolved_ratio_high_water_mark(previous, resolved_ratio)
    if (
        resolved_ratio is not None
        and resolved_ratio_best is not None
        and resolved_ratio < resolved_ratio_best - _RESOLVED_RATIO_FLOOR_TOLERANCE
    ):
        degraded_reasons.append("resolved_edge_ratio_below_observed_floor")

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
            "language_builtin_targets": unresolvable_edges,
            "resolvable": resolvable_edges,
            "resolved_ratio_resolvable": (
                round(resolved_ratio_resolvable, 6)
                if resolved_ratio_resolvable is not None else None
            ),
            "resolved_ratio": round(resolved_ratio, 6) if resolved_ratio is not None else None,
            "resolved_ratio_best_observed": (
                round(resolved_ratio_best, 6)
                if resolved_ratio_best is not None else None
            ),
            "cross_language": cross_language_edges,
            "junk_destination": junk_destination_edges,
        },
        "artifacts": {
            "entities": artifact_entities,
            "total_entities": entity_count,
            "entity_share": round(artifact_ratio, 6) if artifact_ratio is not None else None,
            "path_families": ["eval", "artifacts", "coverage", "tmp", "dist", "build", "data"],
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


_FTS_DELETE_CHUNK_SIZE = 500


def _delete_entities_fts_rows(conn: sqlite3.Connection, entity_ids: list[int]) -> None:
    """Remove entities_fts rows for entity_ids in a bounded number of scans.

    ``entity_id`` is UNINDEXED in the ``entities_fts`` schema, so any DELETE
    filtering on it costs SQLite one full virtual-table scan no matter how
    many rows that scan matches. Issuing one statement per id -- the
    previous ``executemany`` -- therefore paid one full scan PER DELETED
    ENTITY: 79.5s to remove entities a single set-based statement removes in
    0.109s (see NF-2026-00635). Chunking ids into a bounded ``IN (...)``
    clause turns that into ``ceil(len(entity_ids) / chunk)`` scans for the
    whole batch instead -- one scan for any file whose entity count fits in
    a single chunk. The chunk size stays comfortably under SQLite's default
    999 host-parameter ceiling.
    """

    for start in range(0, len(entity_ids), _FTS_DELETE_CHUNK_SIZE):
        chunk = entity_ids[start:start + _FTS_DELETE_CHUNK_SIZE]
        placeholders = ",".join("?" * len(chunk))
        conn.execute(
            f"DELETE FROM entities_fts WHERE entity_id IN ({placeholders})",
            chunk,
        )


def _invalidate_file(conn: sqlite3.Connection, rel: str) -> None:
    """Remove every entity/edge/FTS row a file owns before re-indexing it.

    Called for changed files (before re-extraction) AND for files that
    were indexed before but no longer exist on disk (rename/delete), so a
    stale edge from a moved-away file can never survive a rebuild.
    """

    ids = [row[0] for row in conn.execute("SELECT id FROM entities WHERE file_path=?", (rel,))]
    if ids:
        _delete_entities_fts_rows(conn, ids)
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
            edge.source_col, edge.receiver_name,
        )
        if identity in seen_edges:
            continue
        seen_edges.add(identity)
        conn.execute(
            "INSERT INTO edges(file_path, kind, src_qualname, dst_name, dst_qualname, line, "
            "evidence_label, extractor, confidence, source_hash, build_revision, "
            "source_col, receiver_name) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (edge.file_path, edge.kind, edge.src_qualname, edge.dst_name, edge.dst_qualname,
             edge.line, edge.evidence_label, edge.extractor, edge.confidence,
             edge.source_hash, edge.build_revision, edge.source_col, edge.receiver_name),
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


def _python_dotted_calls(source_line: str) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(part.strip() for part in match.group(1).split("."))
        for match in _PYTHON_DOTTED_CALL_RE.finditer(source_line)
    )


def _python_imported_member_accesses(
    source: str,
) -> dict[tuple[object, ...], frozenset[str]]:
    """Return lexically proven module targets for ``alias.member`` accesses."""

    # A Python code point occupies at most four UTF-8 bytes, so this character
    # ceiling also keeps reparsed source substantially below the authenticated
    # 64 MiB read limit without allocating a second encoded copy.
    if len(source) > PYTHON_IMPORT_REPARSE_MAX_SOURCE_CHARS:
        return {}
    try:
        token_count = 0
        for _token in tokenize.generate_tokens(io.StringIO(source).readline):
            token_count += 1
            if token_count > PYTHON_IMPORT_REPARSE_MAX_TOKENS:
                return {}
        tree = ast.parse(source)
        pending: list[tuple[ast.AST, int]] = [(tree, 0)]
        visited = 0
        while pending:
            node, depth = pending.pop()
            visited += 1
            if (
                visited > PYTHON_IMPORT_REPARSE_MAX_NODES
                or depth > PYTHON_IMPORT_REPARSE_MAX_DEPTH
            ):
                return {}
            pending.extend(
                (child, depth + 1) for child in ast.iter_child_nodes(node)
            )
    except (SyntaxError, tokenize.TokenError, RecursionError, MemoryError):
        return {}
    found: dict[tuple[object, ...], set[str]] = {}

    _nested_scopes = (
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.ClassDef,
        ast.Lambda,
        ast.ListComp,
        ast.SetComp,
        ast.DictComp,
        ast.GeneratorExp,
    )

    def assigned_names(node: ast.AST) -> set[str]:
        return {
            child.id
            for child in ast.walk(node)
            if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del))
        }

    def scope_declarations(node: ast.AST) -> set[str]:
        names: set[str] = set()

        def collect(child: ast.AST) -> None:
            if child is not node and isinstance(child, _nested_scopes):
                return
            if isinstance(child, (ast.Global, ast.Nonlocal)):
                names.update(child.names)
                return
            for nested in ast.iter_child_nodes(child):
                collect(nested)

        collect(node)
        return names

    def bound_names(node: ast.AST) -> set[str]:
        names: set[str] = set()

        def collect(child: ast.AST) -> None:
            if child is not node and isinstance(child, _nested_scopes):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(child.name)
                return
            if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                names.add(child.id)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                names.update(alias.asname or alias.name.split(".")[0] for alias in child.names)
            for nested in ast.iter_child_nodes(child):
                collect(nested)

        collect(node)
        return names - scope_declarations(node)

    def merge_envs(
        env: dict[str, str | None],
        paths: list[dict[str, str | None]],
    ) -> None:
        if not paths:
            return
        keys = set().union(*(path.keys() for path in paths))
        env.clear()
        for name in keys:
            values = [path.get(name) for path in paths]
            first = values[0]
            env[name] = first if first is not None and all(value == first for value in values) else None

    def invalidate(target: ast.AST | None, env: dict[str, str | None]) -> None:
        if target is not None:
            for name in assigned_names(target):
                env[name] = None

    def expression(
        node: ast.AST | None,
        env: dict[str, str | None],
        *,
        lexical_env: dict[str, str | None] | None = None,
        call_func: bool = False,
    ) -> None:
        if node is None:
            return
        nested_parent = lexical_env if lexical_env is not None else env
        if isinstance(node, ast.Lambda):
            for item in (*node.args.defaults, *node.args.kw_defaults):
                expression(item, env, lexical_env=nested_parent)
            child_env = dict(nested_parent)
            arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            for argument in arguments:
                child_env[argument.arg] = None
            if node.args.vararg:
                child_env[node.args.vararg.arg] = None
            if node.args.kwarg:
                child_env[node.args.kwarg.arg] = None
            expression(node.body, child_env)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            if not node.generators:
                return
            expression(node.generators[0].iter, env, lexical_env=nested_parent)
            child_env = dict(nested_parent)
            for index, generator in enumerate(node.generators):
                if index:
                    expression(generator.iter, child_env)
                invalidate(generator.target, child_env)
                for condition in generator.ifs:
                    expression(condition, child_env)
            if isinstance(node, ast.DictComp):
                expression(node.key, child_env)
                expression(node.value, child_env)
            else:
                expression(node.elt, child_env)
            return
        if isinstance(node, ast.NamedExpr):
            expression(node.value, env, lexical_env=nested_parent)
            invalidate(node.target, env)
            return
        if isinstance(node, ast.Call):
            expression(node.func, env, lexical_env=nested_parent, call_func=True)
            for argument in (*node.args, *(keyword.value for keyword in node.keywords)):
                expression(argument, env, lexical_env=nested_parent)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and call_func:
            target = env.get(node.id)
            if target is not None:
                key = ("calls", int(node.lineno), int(node.col_offset), node.id, "")
                found.setdefault(key, set()).add(target)
                found.setdefault((key[0], key[1], key[3]), set()).add(target)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
        ):
            target = env.get(node.value.id)
            if target is not None:
                key = (
                    "calls" if call_func else "references",
                    int(node.lineno),
                    int(node.col_offset),
                    node.attr,
                    node.value.id,
                )
                found.setdefault(key, set()).add(target)
                found.setdefault((key[0], key[1], key[3]), set()).add(target)
                if call_func:
                    reference_key = (
                        "references",
                        int(node.lineno),
                        int(node.col_offset),
                        node.attr,
                        node.value.id,
                    )
                    found.setdefault(reference_key, set()).add(target)
                    found.setdefault(
                        (reference_key[0], reference_key[1], reference_key[3]), set()
                    ).add(target)
        for child in ast.iter_child_nodes(node):
            expression(child, env, lexical_env=nested_parent)

    def block(
        statements: list[ast.stmt],
        env: dict[str, str | None],
        *,
        lexical_env: dict[str, str | None] | None = None,
    ) -> None:
        nested_parent = lexical_env if lexical_env is not None else env
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for item in statement.decorator_list:
                    expression(item, env, lexical_env=nested_parent)
                for item in (*statement.args.defaults, *statement.args.kw_defaults):
                    expression(item, env, lexical_env=nested_parent)
                arguments = (
                    *statement.args.posonlyargs,
                    *statement.args.args,
                    *statement.args.kwonlyargs,
                )
                annotations = [
                    argument.annotation
                    for argument in arguments
                    if argument.annotation is not None
                ]
                if statement.args.vararg and statement.args.vararg.annotation is not None:
                    annotations.append(statement.args.vararg.annotation)
                if statement.args.kwarg and statement.args.kwarg.annotation is not None:
                    annotations.append(statement.args.kwarg.annotation)
                if statement.returns is not None:
                    annotations.append(statement.returns)
                for item in annotations:
                    expression(item, env, lexical_env=nested_parent)
                child_env = dict(nested_parent)
                for name in bound_names(statement):
                    child_env[name] = None
                for argument in arguments:
                    child_env[argument.arg] = None
                if statement.args.vararg:
                    child_env[statement.args.vararg.arg] = None
                if statement.args.kwarg:
                    child_env[statement.args.kwarg.arg] = None
                block(statement.body, child_env)
                env[statement.name] = None
                continue
            if isinstance(statement, ast.ClassDef):
                for item in (*statement.decorator_list, *statement.bases):
                    expression(item, env, lexical_env=nested_parent)
                class_env = dict(nested_parent)
                block(statement.body, class_env, lexical_env=nested_parent)
                env[statement.name] = None
                continue
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    local = alias.asname or alias.name.split(".")[0]
                    env[local] = alias.name if alias.asname else alias.name.split(".")[0]
                continue
            if isinstance(statement, ast.ImportFrom):
                for alias in statement.names:
                    module = f"{'.' * statement.level}{statement.module or ''}"
                    env[alias.asname or alias.name] = f"{module}.{alias.name}"
                continue
            if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                expression(getattr(statement, "value", None), env, lexical_env=nested_parent)
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for target in targets:
                    invalidate(target, env)
                continue
            if isinstance(statement, ast.Delete):
                for target in statement.targets:
                    expression(target, env, lexical_env=nested_parent)
                    invalidate(target, env)
                continue
            if isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    expression(item.context_expr, env, lexical_env=nested_parent)
                    invalidate(item.optional_vars, env)
                block(statement.body, env, lexical_env=lexical_env)
                continue
            if isinstance(statement, ast.If):
                expression(statement.test, env, lexical_env=nested_parent)
                body_env = dict(env)
                else_env = dict(env)
                block(statement.body, body_env, lexical_env=lexical_env)
                block(statement.orelse, else_env, lexical_env=lexical_env)
                merge_envs(env, [body_env, else_env])
                continue
            if isinstance(statement, ast.While):
                expression(statement.test, env, lexical_env=nested_parent)
                zero_env = dict(env)
                body_env = dict(env)
                block(statement.body, body_env, lexical_env=lexical_env)
                else_env: dict[str, str | None] = {}
                merge_envs(else_env, [zero_env, body_env])
                block(statement.orelse, else_env, lexical_env=lexical_env)
                merge_envs(env, [zero_env, body_env, else_env])
                continue
            if isinstance(statement, (ast.For, ast.AsyncFor)):
                expression(statement.iter, env, lexical_env=nested_parent)
                zero_env = dict(env)
                body_env = dict(env)
                invalidate(statement.target, body_env)
                block(statement.body, body_env, lexical_env=lexical_env)
                else_env = {}
                merge_envs(else_env, [zero_env, body_env])
                block(statement.orelse, else_env, lexical_env=lexical_env)
                merge_envs(env, [zero_env, body_env, else_env])
                continue
            if isinstance(statement, ast.Match):
                expression(statement.subject, env, lexical_env=nested_parent)
                paths = [dict(env)]
                for case in statement.cases:
                    case_env = dict(env)
                    for child in ast.walk(case.pattern):
                        if isinstance(child, ast.MatchAs) and child.name is not None:
                            case_env[child.name] = None
                        elif isinstance(child, ast.MatchStar) and child.name is not None:
                            case_env[child.name] = None
                        elif isinstance(child, ast.MatchMapping) and child.rest is not None:
                            case_env[child.rest] = None
                    expression(case.guard, case_env, lexical_env=nested_parent)
                    block(case.body, case_env, lexical_env=lexical_env)
                    paths.append(case_env)
                merge_envs(env, paths)
                continue
            if isinstance(statement, (ast.Try, ast.TryStar)):
                body_env = dict(env)
                block(statement.body, body_env, lexical_env=lexical_env)
                normal_env = dict(body_env)
                block(statement.orelse, normal_env, lexical_env=lexical_env)
                paths = [normal_env]
                for handler in statement.handlers:
                    handler_env = dict(env)
                    if handler.type is not None:
                        expression(handler.type, handler_env, lexical_env=nested_parent)
                    if handler.name is not None:
                        handler_env[handler.name] = None
                    block(handler.body, handler_env, lexical_env=lexical_env)
                    paths.append(handler_env)
                if statement.finalbody:
                    for path in paths:
                        block(statement.finalbody, path, lexical_env=lexical_env)
                merge_envs(env, paths)
                continue
            expression(statement, env, lexical_env=nested_parent)

    try:
        block(tree.body, {})
    except (RecursionError, MemoryError):
        return {}
    return {key: frozenset(targets) for key, targets in found.items()}


def _indexed_python_source(
    conn: sqlite3.Connection, repo_root: Path, file_path: str
) -> str:
    """Return only authenticated bytes that match the exact indexed file hash."""

    row = conn.execute(
        "SELECT source_hash FROM files WHERE file_path=? AND language='python'",
        (file_path,),
    ).fetchone()
    if row is None or not isinstance(row["source_hash"], str):
        return ""
    try:
        resolved = _validate_single_file_path(repo_root, file_path)
        snapshot = _open_authenticated_regular_file_snapshot(
            repo_root, Path(file_path)
        )
    except SourceGraphError:
        return ""
    if resolved.relative_to(repo_root).as_posix() != Path(file_path).as_posix():
        return ""
    if snapshot.source_hash != str(row["source_hash"]):
        return ""
    try:
        return snapshot.raw.decode("utf-8", errors="strict")
    except UnicodeError:
        return ""


def _resolve_python_imported_references(
    conn: sqlite3.Connection,
    repo_root: Path,
    *,
    changed_files: set[str] | None = None,
    affected_names: set[str] | None = None,
) -> int:
    """Resolve ``alias.member`` reads to unique indexed module attributes.

    The extractor deliberately cannot prove a cross-file module receiver while
    processing one file.  Here, the import entity proves the local alias and
    its signature proves the module.  We additionally reparse the caller so a
    same-line unrelated attribute (or a call) cannot borrow that evidence.
    """

    conn.execute(
        "UPDATE edges SET dst_qualname=NULL "
        "WHERE extractor=? AND kind='references' AND dst_qualname IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM entities en WHERE en.qualname=edges.dst_qualname)",
        (sgast.EXTRACTOR_ID,),
    )
    unresolved_sql = (
        "SELECT e.id, e.file_path, e.line, e.source_col, e.receiver_name, "
        "e.dst_name FROM edges e "
        "JOIN files f ON f.file_path=e.file_path "
        "WHERE e.extractor=? AND f.language='python' AND e.kind='references' "
        "AND e.dst_qualname IS NULL "
    )
    unresolved_params: list[Any] = [sgast.EXTRACTOR_ID]
    if changed_files is not None and affected_names is not None:
        unresolved_sql += (
            "AND (e.file_path IN (SELECT value FROM json_each(?)) "
            "OR e.dst_name IN (SELECT value FROM json_each(?))) "
        )
        unresolved_params.extend(
            (json.dumps(sorted(changed_files)), json.dumps(sorted(affected_names)))
        )
    unresolved_sql += "ORDER BY e.id"
    unresolved = conn.execute(unresolved_sql, unresolved_params).fetchall()
    accesses_by_file: dict[
        str, dict[tuple[object, ...], frozenset[str]]
    ] = {}
    candidates_by_name: dict[str, list[sqlite3.Row]] = {}
    resolved = 0
    for edge in unresolved:
        file_path = str(edge["file_path"])
        accesses = accesses_by_file.get(file_path)
        if accesses is None:
            source = _indexed_python_source(conn, repo_root, file_path)
            accesses = _python_imported_member_accesses(source)
            accesses_by_file[file_path] = accesses
        name = str(edge["dst_name"])
        targets = accesses.get(
            (
                "references",
                int(edge["line"] or 0),
                int(edge["source_col"]),
                name,
                str(edge["receiver_name"]),
            ),
            frozenset(),
        )
        if len(targets) != 1:
            continue
        module_target = next(iter(targets))
        candidates = candidates_by_name.get(name)
        if candidates is None:
            candidates = conn.execute(
                "SELECT e.file_path, e.qualname FROM entities e "
                "JOIN files f ON f.file_path=e.file_path "
                "WHERE e.name=? AND e.kind IN ('attribute','function','class') "
                "AND f.language='python' AND e.qualname=e.file_path || '.' || e.name "
                "ORDER BY e.file_path, e.qualname",
                (name,),
            ).fetchall()
            candidates_by_name[name] = candidates
        matched = [
            candidate for candidate in candidates
            if _import_target_matches_file(module_target, str(candidate["file_path"]))
        ]
        if len(matched) != 1:
            continue
        cur = conn.execute(
            "UPDATE edges SET dst_qualname=? WHERE id=? AND dst_qualname IS NULL",
            (matched[0]["qualname"], edge["id"]),
        )
        resolved += int(cur.rowcount or 0)
    return resolved


def _resolve_python_imported_calls(
    conn: sqlite3.Connection,
    repo_root: Path,
    *,
    changed_files: set[str] | None = None,
    affected_names: set[str] | None = None,
) -> int:
    """Bind Python calls only when imports and exact call syntax agree.

    Python AST extraction intentionally leaves cross-file targets unresolved.
    This pass resolves imported functions without guessing receiver types: a
    direct call must name the imported symbol, while an attribute call must
    use the exact local import alias on its recorded source line.  The target
    must map to exactly one Python callable entity in the imported module.

    Like the C++ resolver, recomputing after every build must also clear a
    binding whose target has disappeared. A Python call edge only fills when
    its ``dst_qualname`` resolves to a real entity, so any managed edge whose
    ``dst_qualname`` no longer names an existing entity -- an incremental build
    renamed/deleted the callee while the caller file was untouched -- is reset
    to NULL first, then re-resolved below. Without this, a stale binding to a
    vanished symbol would read as EXTRACTED against an entity that is gone.
    """

    incremental_scope = changed_files is not None and affected_names is not None
    changed_files_json = json.dumps(sorted(changed_files or ()))
    affected_names_json = json.dumps(sorted(affected_names or ()))

    # A changed caller is invalidated and rewritten before this pass.  A
    # changed/removed callee can only invalidate bindings with the same
    # extracted destination name.  Restricting the reset to those names avoids
    # a repository-wide edge scan for every one-file incremental refresh while
    # retaining exact rename/delete semantics.  Full/legacy callers keep the
    # conservative global pass by omitting the scope arguments.
    if not incremental_scope:
        conn.execute(
            "UPDATE edges SET dst_qualname=NULL "
            "WHERE extractor=? AND kind='calls' AND dst_qualname IS NOT NULL "
            "AND evidence_label IN (?, ?, ?) "
            "AND NOT EXISTS (SELECT 1 FROM entities en WHERE en.qualname = edges.dst_qualname)",
            (
                sgast.EXTRACTOR_ID,
                sgast.EXTRACTED,
                sgast.INFERRED,
                sgast.AMBIGUOUS,
            ),
        )
    elif affected_names:
        conn.execute(
            "UPDATE edges SET dst_qualname=NULL "
            "WHERE extractor=? AND kind='calls' AND dst_qualname IS NOT NULL "
            "AND evidence_label IN (?, ?, ?) "
            "AND dst_name IN (SELECT value FROM json_each(?)) "
            "AND NOT EXISTS (SELECT 1 FROM entities en WHERE en.qualname = edges.dst_qualname)",
            (
                sgast.EXTRACTOR_ID,
                sgast.EXTRACTED,
                sgast.INFERRED,
                sgast.AMBIGUOUS,
                affected_names_json,
            ),
        )

    unresolved_sql = (
        "SELECT e.id, e.file_path, e.line, e.source_col, e.receiver_name, "
        "e.dst_name, e.evidence_label "
        "FROM edges e JOIN files f ON f.file_path=e.file_path "
        "WHERE e.extractor=? AND f.language='python' AND e.kind='calls' "
        "AND e.dst_qualname IS NULL AND e.evidence_label IN (?, ?, ?) "
    )
    unresolved_params: list[Any] = [
        sgast.EXTRACTOR_ID,
        sgast.EXTRACTED,
        sgast.INFERRED,
        sgast.AMBIGUOUS,
    ]
    if incremental_scope:
        unresolved_sql += (
            "AND (e.file_path IN (SELECT value FROM json_each(?)) "
            "OR e.dst_name IN (SELECT value FROM json_each(?))) "
        )
        unresolved_params.extend((changed_files_json, affected_names_json))
    unresolved_sql += "ORDER BY e.id"
    unresolved = conn.execute(unresolved_sql, unresolved_params).fetchall()
    imports_by_file: dict[str, list[sqlite3.Row]] = {}
    accesses_by_file: dict[
        str, dict[tuple[object, ...], frozenset[str]]
    ] = {}
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
        matching_imports: list[str] = []
        if str(edge["evidence_label"]) == sgast.EXTRACTED:
            matching_imports = [
                str(item["signature"])
                for item in imports
                if str(item["name"]) == name
                and str(item["signature"]).lstrip(".").rsplit(".", 1)[-1] == name
            ]
        else:
            accesses = accesses_by_file.get(file_path)
            if accesses is None:
                source = _indexed_python_source(conn, repo_root, file_path)
                accesses = _python_imported_member_accesses(source)
                source_lines = source.splitlines()
                for access_line in {
                    key[1] for key in accesses if key[0] == "calls"
                }:
                    if 0 < access_line <= len(source_lines):
                        # Preserve the established once-per-source-line
                        # tokenization contract for callers that instrument
                        # this bounded parsing pass.
                        _python_dotted_calls(source_lines[access_line - 1])
                accesses_by_file[file_path] = accesses
            line_number = int(edge["line"] or 0)
            matching_imports.extend(
                accesses.get(
                    (
                        "calls",
                        line_number,
                        int(edge["source_col"]),
                        name,
                        str(edge["receiver_name"]),
                    ),
                    frozenset(),
                )
            )
        if not matching_imports:
            continue

        target_names = (
            {name}
            if str(edge["receiver_name"])
            else {
                target.lstrip(".").rsplit(".", 1)[-1]
                for target in matching_imports
            }
        )
        if len(target_names) != 1:
            continue
        target_name = next(iter(target_names))
        candidates = candidates_by_name.get(target_name)
        if candidates is None:
            candidates = conn.execute(
                "SELECT e.file_path, e.qualname FROM entities e "
                "JOIN files f ON f.file_path=e.file_path "
                "WHERE e.name=? AND e.kind IN ('function','class') "
                "AND f.language='python' "
                "ORDER BY e.file_path, e.qualname",
                (target_name,),
            ).fetchall()
            candidates_by_name[target_name] = candidates

        matched_candidates = []
        for candidate in candidates:
            candidate_file = str(candidate["file_path"])
            for target in matching_imports:
                module_target = target
                if not str(edge["receiver_name"]):
                    module_target = target.rsplit(".", 1)[0]
                if _import_target_matches_file(module_target, candidate_file):
                    matched_candidates.append(candidate)
                    break
        if len(matched_candidates) != 1:
            continue
        if str(edge["evidence_label"]) == sgast.AMBIGUOUS:
            cur = conn.execute(
                "UPDATE edges SET dst_qualname=?, evidence_label=?, confidence=1.0 "
                "WHERE id=? AND dst_qualname IS NULL",
                (
                    matched_candidates[0]["qualname"],
                    sgast.EXTRACTED,
                    edge["id"],
                ),
            )
        else:
            cur = conn.execute(
                "UPDATE edges SET dst_qualname=? WHERE id=? AND dst_qualname IS NULL",
                (matched_candidates[0]["qualname"], edge["id"]),
            )
        resolved += int(cur.rowcount or 0)
        if str(edge["receiver_name"]):
            conn.execute(
                "UPDATE edges SET dst_qualname=? WHERE file_path=? "
                "AND kind='references' AND line=? AND source_col=? "
                "AND receiver_name=? AND dst_name=? AND dst_qualname IS NULL",
                (
                    matched_candidates[0]["qualname"],
                    file_path,
                    int(edge["line"] or 0),
                    int(edge["source_col"]),
                    str(edge["receiver_name"]),
                    name,
                ),
            )
    return resolved


@functools.lru_cache(maxsize=2048)
def _SELF_CALL_RE(name: str) -> re.Pattern[str]:
    """``self.<name>(`` on the recorded line -- the receiver IS the evidence."""
    return re.compile(r"\bself\." + re.escape(name) + r"\s*\(")


def _python_owner_class(qualname: str, file_path: str) -> str | None:
    """Return the class a python entity qualname belongs to, or ``None``.

    Qualnames are ``<file_path>.<Class>.<method>``; anything shallower is a
    module-level function and owns no class.
    """

    if not qualname.startswith(file_path):
        return None
    parts = qualname[len(file_path):].lstrip(".").split(".")
    return f"{file_path}.{parts[0]}" if len(parts) >= 2 else None


def _resolve_python_self_method_calls(
    conn: sqlite3.Connection,
    repo_root: Path,
    *,
    changed_files: set[str] | None = None,
    affected_names: set[str] | None = None,
) -> int:
    """Bind ``self.method()`` to the method of the caller's OWN class.

    The import resolver cannot see these: a self-call names no module and has
    no import to agree with, so every one stayed unresolved.

    Measured on this repository before this pass: 10,436 unresolved python call
    edges named something defined exactly once in the whole repo -- which looks
    like a free 10k of resolution and is not. The most frequent of those names
    are ``exists`` (1132), ``join`` (928), ``sha256`` (836), ``stat`` (565),
    ``open`` (524, a builtin), ``time``, ``sleep``, ``search``: stdlib calls
    that happen to collide with one repo definition. Binding on uniqueness
    alone would have manufactured thousands of edges pointing at the wrong
    code, and a fabricated edge is worse than an absent one -- an absent edge
    is visibly absent.

    So uniqueness is a precondition, never the evidence. The evidence is the
    receiver: the call site must literally read ``self.<name>(``, and the one
    definition must be a method of the SAME class the caller is defined in.
    Under those two facts the binding is not a guess. 622 edges qualify.

    Inheritance is deliberately NOT followed. Of the 327 recorded ``inherits``
    edges, all 239 unresolved ones name classes outside the repository
    (RuntimeError, ctypes.Structure, Enum, unittest.TestCase), so an in-repo
    ancestor adds nothing measurable here -- and a mixin the extractor never
    saw would make the walk a guess again.
    """

    # The AST extractor now proves direct method receivers (including flow
    # invalidation) and emits those calls as EXTRACTED.  Consequently every
    # remaining null/INFERRED ``self`` call is specifically one for which the
    # receiver was *not* proven: a rebound name, staticmethod parameter, or
    # nested-function shadow.  Textual post-processing cannot recover that
    # lexical proof and must never upgrade such an edge.
    return 0

    incremental_scope = changed_files is not None and affected_names is not None
    changed_files_json = json.dumps(sorted(changed_files or ()))
    affected_names_json = json.dumps(sorted(affected_names or ()))

    # Same rename/delete semantics as the import resolver: a binding whose
    # target no longer exists is cleared before anything is re-resolved, so a
    # vanished method never reads as EXTRACTED against an entity that is gone.
    if not incremental_scope:
        conn.execute(
            "UPDATE edges SET dst_qualname=NULL "
            "WHERE extractor=? AND kind='calls' AND dst_qualname IS NOT NULL "
            "AND evidence_label=? "
            "AND NOT EXISTS (SELECT 1 FROM entities en WHERE en.qualname = edges.dst_qualname)",
            (sgast.EXTRACTOR_ID, sgast.INFERRED),
        )

    sql = (
        "WITH defs AS ("
        "  SELECT en.name AS n, COUNT(*) AS c, MIN(en.qualname) AS qn, "
        "         MIN(en.file_path) AS fp, MIN(en.kind) AS k "
        "  FROM entities en JOIN files f ON f.file_path=en.file_path "
        "  WHERE f.language='python' AND en.kind IN ('function','method') "
        "  GROUP BY en.name) "
        "SELECT e.id, e.file_path, e.line, e.dst_name, e.src_qualname, "
        "       d.qn AS target_qualname, d.fp AS target_file, d.k AS target_kind "
        "FROM edges e JOIN files f ON f.file_path=e.file_path "
        "JOIN defs d ON d.n = e.dst_name AND d.c = 1 "
        "WHERE e.extractor=? AND f.language='python' AND e.kind='calls' "
        "AND e.dst_qualname IS NULL AND e.evidence_label=? "
    )
    params: list[Any] = [sgast.EXTRACTOR_ID, sgast.INFERRED]
    if incremental_scope:
        sql += (
            "AND (e.file_path IN (SELECT value FROM json_each(?)) "
            "OR e.dst_name IN (SELECT value FROM json_each(?))) "
        )
        params.extend((changed_files_json, affected_names_json))
    sql += "ORDER BY e.id"

    lines_by_file: dict[str, tuple[str, ...]] = {}
    resolved = 0
    for edge in conn.execute(sql, params).fetchall():
        if str(edge["target_kind"]) != "method":
            continue
        file_path = str(edge["file_path"])
        caller_class = _python_owner_class(str(edge["src_qualname"]), file_path)
        if caller_class is None:
            continue
        target_class = _python_owner_class(
            str(edge["target_qualname"]), str(edge["target_file"])
        )
        if target_class is None or target_class != caller_class:
            continue

        lines = lines_by_file.get(file_path)
        if lines is None:
            try:
                lines = tuple(
                    (repo_root / file_path)
                    .read_text(encoding="utf-8", errors="strict")
                    .splitlines()
                )
            except (OSError, UnicodeError):
                lines = ()
            lines_by_file[file_path] = lines
        line_number = int(edge["line"] or 0)
        if not (0 < line_number <= len(lines)):
            continue
        if not _SELF_CALL_RE(str(edge["dst_name"])).search(lines[line_number - 1]):
            continue

        cur = conn.execute(
            "UPDATE edges SET dst_qualname=? WHERE id=? AND dst_qualname IS NULL",
            (str(edge["target_qualname"]), edge["id"]),
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
    build_started = time.monotonic()
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
    phase_seconds: dict[str, float] = {}
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
        phase_seconds["extraction"] = extraction_seconds

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

        removed_paths = set(existing).difference(seen_rel)
        resolver_changed_paths = {
            extraction.file_path for extraction, _, _ in pending_extractions
        }.union(removed_paths)
        python_changed_paths = {
            path
            for path in resolver_changed_paths
            if sglanguages.language_for_path(Path(path)) == "python"
        }
        affected_python_names: set[str] = set()
        if python_changed_paths:
            # Only an added/removed/renamed function identity can change a
            # cross-file binding.  A body-only edit keeps the same qualname and
            # must not fan out to every caller of every function in a large
            # module.  Compare old/new identities before invalidation; deleted
            # and parse-failed files naturally have an empty new set.
            old_python_functions = {
                (str(row["file_path"]), str(row["name"]), str(row["qualname"]))
                for row in conn.execute(
                    "SELECT e.file_path, e.name, e.qualname FROM entities e "
                    "JOIN files f ON f.file_path=e.file_path "
                    "WHERE f.language='python' AND e.kind='function' "
                    "AND e.file_path IN (SELECT value FROM json_each(?))",
                    (json.dumps(sorted(python_changed_paths)),),
                )
            }
            new_python_functions: set[tuple[str, str, str]] = set()
            for extraction, _, _ in pending_extractions:
                if extraction.file_path not in python_changed_paths:
                    continue
                new_python_functions.update(
                    (extraction.file_path, entity.name, entity.qualname)
                    for entity in extraction.entities
                    if entity.kind == "function"
                )
            affected_python_names.update(
                name
                for _, name, _ in old_python_functions.symmetric_difference(
                    new_python_functions
                )
            )

        # Recompute the bounded 90-day git history for the changed corpus
        # BEFORE opening the write transaction. The walk shells out to
        # ``git log`` over the whole repository (measured multi-second) and must
        # never run while the index write transaction holds the connection --
        # that is 90%+ of a build spent under the writer lock. The recorded HEAD
        # oid is the generation key, so an unchanged HEAD does no walk at all;
        # the resulting plan is applied with SQLite-only work once the write
        # transaction is open (``persist_git_metrics`` below).
        git_metrics_started = time.monotonic()
        git_metrics_plan = sginsights.compute_git_metrics(
            conn, repo_root, sorted(seen_rel), limit=10000,
        )
        phase_seconds["git_metrics"] = max(
            0.0, time.monotonic() - git_metrics_started
        )
        merge_started = time.monotonic()
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
            phase_seconds["merge"] = max(0.0, time.monotonic() - merge_started)
            resolution_started = time.monotonic()
            if changed or removed:
                # Revoke before the resolvers run, while every surviving
                # binding's edge still carries what it wrote: they may clear or
                # re-resolve that destination (see ``_lsp_reconcile_generation``).
                _lsp_reconcile_generation(conn)
                _resolve_cpp_cross_file_edges(conn)
                if python_changed_paths:
                    _resolve_python_imported_references(
                        conn, repo_root,
                        changed_files=python_changed_paths,
                        affected_names=affected_python_names,
                    )
                    _resolve_python_imported_calls(
                        conn,
                        repo_root,
                        changed_files=python_changed_paths,
                        affected_names=affected_python_names,
                    )
                    _resolve_python_self_method_calls(
                        conn,
                        repo_root,
                        changed_files=python_changed_paths,
                        affected_names=affected_python_names,
                    )
                _resolve_javascript_import_bindings(conn)
            # Task 3: revoke LSP evidence for every changed/deleted source or
            # target, and re-attach bindings re-extraction or a resolver
            # dropped, inside this merge -- plain SQL, so it holds even when no
            # server is configured or the post-publish enrichment lease is busy.
            _lsp_reconcile_generation(conn)
            phase_seconds["resolution"] = max(
                0.0, time.monotonic() - resolution_started
            )
            # SQLite-only apply of the plan computed above; no subprocess runs
            # while this write transaction holds the connection.
            sginsights.persist_git_metrics(conn, git_metrics_plan)
            finished_at = _now_iso()
            skipped_files = [
                entry["file"] for entry in errors
                if entry.get("status") == "index_write_skipped"
            ]
            # A full build replaces any bootstrap-only generation identity.
            # Clearing the marker in the same transaction as last_build prevents
            # later single-file mutations from rewriting this authoritative receipt.
            conn.execute(
                "DELETE FROM meta WHERE key='single_file_generation_metadata'"
            )
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
        quality_started = time.monotonic()
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
        phase_seconds["quality"] = max(0.0, time.monotonic() - quality_started)
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
    phase_seconds["hash"] = hash_seconds
    phase_seconds["total"] = max(0.0, time.monotonic() - build_started)
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
        phase_seconds=phase_seconds,
    )


def probe_generation(
    db_path: Path, *, expected_report: BuildReport | None = None
) -> dict[str, Any]:
    """Strictly probe one exact generation through representative read paths."""

    conn = connect(db_path, read_only=True)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='last_build'"
        ).fetchone()
        if row is None:
            raise SourceGraphError("source_graph_generation_probe:no_metadata")
        generation = json.loads(str(row["value"]))
        if not isinstance(generation, dict):
            raise SourceGraphError("source_graph_generation_probe:metadata_not_object")
        finished_at = generation.get("finished_at")
        build_revision = generation.get("build_revision")
        files_seen = generation.get("files_seen")
        if (
            not isinstance(finished_at, str)
            or not finished_at
            or not isinstance(build_revision, str)
            or not build_revision
            or not isinstance(files_seen, int)
            or isinstance(files_seen, bool)
            or files_seen < 0
        ):
            raise SourceGraphError("source_graph_generation_probe:incomplete")

        representative_file = conn.execute(
            "SELECT file_path FROM files ORDER BY file_path LIMIT 1"
        ).fetchone()
        representative_entity = conn.execute(
            "SELECT name, qualname FROM entities "
            "ORDER BY file_path, line_start LIMIT 1"
        ).fetchone()

        # File/context and bodygrep use the files table and its ordered path
        # access. Empty generations are valid, so only require a context result
        # when a representative file exists.
        if representative_file is not None:
            file_context = context(
                conn, str(representative_file["file_path"])
            )
            if not file_context.get("found"):
                raise SourceGraphError("source_graph_generation_probe:file")

        # Focus depends on the FTS virtual table and entity-id mapping. Execute
        # MATCH directly because find() deliberately falls back to LIKE after
        # an FTS error, which is unsuitable for a publication/readiness probe.
        fts_term = (
            str(
                representative_entity["qualname"]
                or representative_entity["name"]
            )
            if representative_entity is not None
            else "__aiworkhub_generation_probe_no_match__"
        )
        fts_row = conn.execute(
            "SELECT e.id FROM entities_fts AS f "
            "JOIN entities AS e ON e.id = f.entity_id "
            "WHERE entities_fts MATCH ? LIMIT 1",
            (_fts_phrase(fts_term),),
        ).fetchone()
        if representative_entity is not None and fts_row is None:
            raise SourceGraphError("source_graph_generation_probe:fts")

        if expected_report is not None and (
            generation.get("finished_at") != expected_report.finished_at
            or generation.get("build_revision") != expected_report.build_revision
        ):
            raise SourceGraphError("source_graph_generation_probe:mismatch")
        return generation
    finally:
        conn.close()


def _staging_prefix(canonical_path: Path) -> str:
    return f".{canonical_path.name}.building-"


def _cleanup_abandoned_staging(canonical_path: Path) -> None:
    """Remove unpublished candidates while holding the repository writer lease."""

    staging_prefix = _staging_prefix(canonical_path)
    for abandoned in canonical_path.parent.iterdir():
        if abandoned.name.startswith(staging_prefix) and abandoned.is_file():
            try:
                abandoned.unlink()
            except OSError:
                pass


@contextmanager
def _staged_generation(canonical_path: Path, *, copy_existing: bool):
    """Yield an isolated candidate and remove it unless publication consumed it."""

    staging_path = canonical_path.with_name(
        f"{_staging_prefix(canonical_path)}{secrets.token_hex(16)}"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(staging_path, flags, 0o600)
    try:
        if copy_existing:
            if not canonical_path.is_file():
                raise SourceGraphError(
                    f"source_graph_generation_missing:{canonical_path}"
                )
            with canonical_path.open("rb") as source, os.fdopen(
                descriptor, "wb", closefd=True
            ) as destination:
                descriptor = -1
                shutil.copyfileobj(source, destination)
        else:
            os.close(descriptor)
            descriptor = -1
        yield staging_path
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            staging_path.unlink(missing_ok=True)
        except OSError:
            pass


def _publish_staged_generation(
    staging_path: Path,
    canonical_path: Path,
    *,
    expected_report: BuildReport | None = None,
) -> dict[str, Any]:
    """Probe a closed candidate read-only, then publish it in one rename."""

    staging_stat = staging_path.lstat()
    if not stat.S_ISREG(staging_stat.st_mode):
        raise SourceGraphError("source_graph_generation_probe:unsafe_staging")
    generation = probe_generation(staging_path, expected_report=expected_report)
    verified_stat = staging_path.lstat()
    if (
        not stat.S_ISREG(verified_stat.st_mode)
        or (verified_stat.st_dev, verified_stat.st_ino)
        != (staging_stat.st_dev, staging_stat.st_ino)
    ):
        raise SourceGraphError("source_graph_generation_probe:staging_identity_changed")
    try:
        atomic_replace(staging_path, canonical_path)
    except PublicationDurabilityError as exc:
        try:
            probe_generation(canonical_path, expected_report=expected_report)
        except Exception as probe_exc:
            raise SourceGraphError(
                "source_graph_generation_publication_durability_uncertain:"
                "published=true:canonical_probe_failed"
            ) from probe_exc
        raise SourceGraphError(
            "source_graph_generation_publication_durability_uncertain:"
            "published=true:canonical_probe_succeeded"
        ) from exc
    return generation


def _ensure_single_file_generation_metadata(conn: sqlite3.Connection) -> None:
    """Make a fresh schema-only private database probeable after mutation.

    Partition builders intentionally start from an empty schema and populate it
    through the single-file APIs.  Such databases have no full-build report to
    preserve, so establish a truthful minimal generation identity there.  A
    copied canonical generation already has authoritative metadata and is left
    byte-for-byte unchanged by this helper.
    """

    files_seen = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
    synthetic = conn.execute(
        "SELECT 1 FROM meta WHERE key='single_file_generation_metadata'"
    ).fetchone()
    last_build = conn.execute(
        "SELECT value FROM meta WHERE key='last_build'"
    ).fetchone()
    if last_build and not synthetic:
        return
    if last_build:
        generation = json.loads(str(last_build[0]))
        generation["files_seen"] = files_seen
        conn.execute(
            "UPDATE meta SET value=? WHERE key='last_build'",
            (json.dumps(generation),),
        )
        return
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('last_build', ?)",
        (
            json.dumps(
                {
                    "finished_at": _now_iso(),
                    "build_revision": BUILD_REVISION,
                    "files_seen": files_seen,
                }
            ),
        ),
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('single_file_generation_metadata', 'true')"
    )


def _persist_failed_build_metadata(
    staging_path: Path,
    canonical_path: Path,
    failure: SourceGraphBuildFailedError,
) -> None:
    """Retain failure truth without publishing the failed generation.

    Full builds now happen in an isolated database and only a validated success
    replaces the readable generation.  A containment-floor failure is therefore
    discarded with its staging file, but its ``last_build`` row is still the
    authoritative diagnostic promised by the build contract.  Copy only that
    internally-produced metadata row into the canonical database while the
    repository writer lease is held; no candidate entities or edges are exposed.
    """
    with closing(connect(staging_path, read_only=True)) as staging:
        row = staging.execute(
            "SELECT value FROM meta WHERE key='last_build'"
        ).fetchone()
    if row is None:
        raise SourceGraphError("source_graph_failed_build_metadata_missing")
    payload = json.loads(str(row[0]))
    if not isinstance(payload, dict) or int(payload.get("files_skipped", 0)) <= 0:
        raise SourceGraphError("source_graph_failed_build_metadata_invalid")
    payload["status"] = "failed"
    payload["failure_reason"] = str(failure)
    with closing(connect(canonical_path)) as canonical:
        canonical.execute(
            "INSERT INTO meta(key, value) VALUES('last_build', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(payload),),
        )
        canonical.commit()


def build_index(repo_root: Path, *, db_path: Path | None = None, incremental: bool = True) -> BuildReport:
    """Build and atomically publish one independently readable generation."""

    repo_root = repo_root.resolve()
    with index_write_lease(repo_root) as acquired:
        if not acquired:
            raise SourceGraphBuildInProgressError(
                f"source_graph_build_in_progress:{repo_root}"
            )
        if db_path is not None:
            return _build_index_locked(repo_root, db_path=db_path, incremental=incremental)

        canonical_path = resolve_db_path(repo_root)
        _cleanup_abandoned_staging(canonical_path)
        with _staged_generation(
            canonical_path,
            copy_existing=incremental and canonical_path.exists(),
        ) as staging_path:
            try:
                report = _build_index_locked(
                    repo_root, db_path=staging_path, incremental=incremental
                )
            except SourceGraphBuildFailedError as exc:
                try:
                    _persist_failed_build_metadata(staging_path, canonical_path, exc)
                except Exception as metadata_exc:
                    raise SourceGraphBuildFailedError(
                        f"{exc}; failed_build_metadata_persist_failed:"
                        f"{type(metadata_exc).__name__}:{metadata_exc}"
                    ) from metadata_exc
                raise
            _publish_staged_generation(
                staging_path, canonical_path, expected_report=report
            )
            published = replace(report, db_path=str(canonical_path))
    # Task 3 (LSP batch resolution): the full build's enrichment pass runs
    # here -- after the generation is published AND the writer lease released
    # -- so no language server is ever spawned inside the merge transaction.
    # It batches every unresolved edge per server group, takes a short lease
    # of its own to publish what it verified, and never fails the build that
    # produced it.
    _lsp_enrich_after_publish(
        repo_root, restrict=None, languages=_lsp_configured_languages()
    )
    return published


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


# A qualified/camelCase identifier query that normalizes to more than this many
# tokens is over-constrained by the all-tokens AND pass: the extra tokens are
# namespace/path/owner context that never co-occur in a single FTS row, so the
# AND returns an authoritative-looking zero while a shorter core of the same
# query still matches. Above this length the OR broadening is allowed as a
# fallback so a long focus query cannot silently report zero where its short
# equivalent returns indexed matches (NF-2026-00641).
_IDENTIFIER_AND_TOKEN_LIMIT = 5


def find(
    conn: sqlite3.Connection,
    term: str,
    *,
    limit: int = 24,
    retrieval: str = "ranked",
) -> list[dict[str, Any]]:
    """Ranked symbol lookup: phrase -> AND -> (OR) -> LIKE.

    ``retrieval="or_terms"`` is the server-side zero-hit fallback step: it
    skips the phrase and AND passes that already missed and runs the any-token
    OR expression directly (then the LIKE pass), so a wrapper can broaden a
    miss in the same turn instead of asking the model to retype the query.
    """

    term = (term or "").strip()
    if not term:
        return []
    limit = max(1, min(int(limit), MAX_BUDGET_ROWS))
    # A composed view unions two independent entity-id spaces, so the FTS
    # id-join below cannot run against its ``entities`` view. When a base index
    # is attached, delegate to the composed finder, which runs the same FTS
    # expressions per schema with partition-wins precedence.
    from . import source_graph_partition as _sgp
    if _sgp.is_composed(conn):
        return _sgp.composed_find(conn, term, limit=limit)
    rows = []
    tokens = _query_tokens(term)
    if retrieval == "or_terms":
        expressions = [_fts_terms(term, operator="OR") if len(tokens) > 1 else _fts_phrase(term)]
    else:
        expressions = [_fts_phrase(term)]
        if len(tokens) > 1:
            expressions.append(_fts_terms(term, operator="AND"))
            if not _looks_like_identifier_query(term) or len(tokens) > _IDENTIFIER_AND_TOKEN_LIMIT:
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
# ``int()`` enforces Python's default max-str-digits limit (4300), but the
# cursor line offset is at most ``_BODYGREP_SKIP_ALL_LINES`` (19 decimal
# digits).  A longer ASCII-decimal field is always forged, so bound it here
# and never hand it to ``int()``.
_BODYGREP_MAX_CURSOR_LINE_DIGITS = len(str(_BODYGREP_SKIP_ALL_LINES))


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
    # ``str.isdigit()`` accepts Unicode digits (e.g. U+00B2, U+2464) that
    # ``int()`` cannot convert; require an exact ASCII-decimal field so the
    # line offset is always int-convertible and never leaks a ValueError.
    if (
        len(after_line_text) > _BODYGREP_MAX_CURSOR_LINE_DIGITS
        or not (after_line_text.isascii() and after_line_text.isdigit())
        or digest != _bodygrep_cursor_digest(term, target, budget)
    ):
        raise SourceGraphError("invalid_cursor")
    try:
        after_file = bytes.fromhex(af_hex).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise SourceGraphError("invalid_cursor") from exc
    # ``after_line_text`` is already a bounded ASCII-decimal field (the guard
    # above rejects anything else), so this conversion cannot raise.
    after_line = int(after_line_text)
    return (after_file, after_line)


# Line boundaries ``str.splitlines()`` recognizes.  ``\r\n`` is the only
# two-code-point boundary; every other boundary is a single code point.
_BODYGREP_LINE_BOUNDARY_CHARS = (
    "\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029",
)
# Maximum characters handed to ``str.splitlines(keepends=True)`` at a time.
# This keeps only a bounded number of line objects alive while staying on the
# C-level splitter instead of a per-code-point Python loop.
_BODYGREP_SPLIT_CHUNK_CHARS = 16384


def _iter_splitlines(text: str):
    """Yield lines lazily, exactly matching ``str.splitlines()``.

    The bodygrep hot path must not materialize a full ``splitlines`` list while
    the full decoded string is still alive.  Chunking the text and delegating
    each chunk to the C-level ``str.splitlines(keepends=True)`` keeps the hot
    loop fast (a per-code-point Python loop is ~40x slower on non-ASCII text)
    while only a bounded number of line objects exist at once.

    Absolute ``text`` indices are tracked so a single very long line is never
    rebuilt incrementally (which would be O(n^2)); it is sliced once when it
    terminates.  The one multi-code-point boundary ``\r\n`` is re-joined when a
    chunk ends in a bare ``\r`` and the next begins with ``\n``.
    """
    length = len(text)
    if length == 0:
        return
    chunk = _BODYGREP_SPLIT_CHUNK_CHARS
    line_start = 0
    prev_cr = False
    for start in range(0, length, chunk):
        pos = start
        for part in text[start:start + chunk].splitlines(keepends=True):
            part_len = len(part)
            if prev_cr and part.startswith("\n"):
                # The previous chunk ended in a bare ``\r``; this ``\n``
                # completes a single ``\r\n`` boundary, so no empty line begins
                # here -- just consume the ``\n`` as half of that boundary.
                prev_cr = False
                line_start = pos + part_len
            elif part.endswith(_BODYGREP_LINE_BOUNDARY_CHARS):
                terminator_len = 2 if part.endswith("\r\n") else 1
                yield text[line_start:pos + part_len - terminator_len]
                line_start = pos + part_len
                # A bare ``\r`` terminator at a chunk edge pairs with the next
                # chunk's leading ``\n`` into one ``\r\n`` boundary; a part
                # already ending in ``\r\n`` ends in ``\n`` and cannot set this.
                prev_cr = part.endswith("\r")
            else:
                # Unterminated tail: the line continues into the next chunk.
                prev_cr = False
            pos += part_len
    if line_start < length:
        yield text[line_start:length]


def _bodygrep_bytes_proves_no_match(needle: str, raw: bytes) -> bool:
    """Return True only when ``raw`` provably cannot hold a casefold match.

    This is the sound pre-decode rejection for the scan hot path.  When both the
    (already casefolded) needle and the raw bytes are pure ASCII,
    ``str.casefold()`` on any line is exactly ASCII lowercasing, so a
    case-insensitive ASCII byte substring search is exact: absent there means no
    line can match.  The search is a copy-free ``re.IGNORECASE`` bytes scan
    (bytes patterns fold only ASCII letters), so no full-size lowercase copy of
    ``raw`` is ever materialized on the rejection path.  Any non-ASCII needle or
    non-ASCII bytes cannot be proven
    absent this way (casefold can expand ``\\u00df`` to ``ss`` or map ``U+212A``
    to ``k``), so those candidates fall through to the decode path and keep
    Unicode casefold semantics exact.
    """
    if not needle.isascii() or not raw.isascii():
        return False
    return re.search(re.escape(needle.encode("ascii")), raw, re.IGNORECASE) is None


BODYGREP_MATCH_KINDS: tuple[str, ...] = ("literal", "token_and_line", "token_and_file")


def bodygrep_query(
    repo_root: Path,
    term: str,
    budget: int = 64,
    *,
    target: str | None = None,
    cursor: str | None = None,
    match_kind: str = "literal",
) -> dict[str, Any]:
    """Search literal/body text only inside canonical indexed source files.

    The graph stores symbols and edges, not whole file bodies.  This bounded
    mode closes that deliberate storage gap without shelling out to grep or
    silently scanning ignored/unindexed paths.  It reports scan limits so a
    zero hit remains truthful rather than looking like full-repository proof.

    ``match_kind`` is the server-side fallback for whitespace phrases that do
    not occur verbatim (source_graph-5): ``token_and_line`` matches a line
    holding every query token, ``token_and_file`` a file holding every token
    (rows are the lines holding at least one).  Both are labelled on every row
    and on the payload so they can never be mistaken for literal evidence.
    """

    term = (term or "").strip()
    if match_kind not in BODYGREP_MATCH_KINDS:
        raise SourceGraphError(f"bodygrep_match_kind_invalid:{match_kind}")
    if not term:
        return {
            "mode": "bodygrep", "query": term, "budget": 0, "matches": [],
            "candidate_files": [], "files_scanned": 0, "bytes_scanned": 0,
            "scan_truncated": False, "cursor": cursor, "next_cursor": None,
            "truncated": False,
        }
    budget = max(1, min(int(budget), MAX_BUDGET_ROWS))
    tokens = _query_tokens(term) if match_kind != "literal" else []
    if match_kind != "literal" and not tokens:
        raise SourceGraphError("bodygrep_token_query_empty")
    # A token-mode cursor is bound to the kind as well as the term, so a page
    # minted by one kind can never resume a scan of another.
    cursor_term = term if match_kind == "literal" else f"{match_kind}:{term}"
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
        cursor, term=cursor_term, target=normalized_target, budget=budget,
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
    if match_kind != "literal":
        # Pre-decode filter for the token kinds: a file that lacks the longest
        # token cannot hold every token, so the byte-level rejection stays sound.
        needle = max(tokens, key=len)
    matches: list[dict[str, Any]] = []
    files_scanned = 0
    bytes_scanned = 0
    # Files skipped because a single one exceeds the whole byte cap. Reported
    # rather than silently dropped: a caller must be able to tell "not present"
    # from "not looked at".
    oversized_skipped: list[str] = []
    last_scanned_file: str | None = None
    next_cursor: str | None = None
    for file_path in paths:
        candidate = (repo_root / file_path).resolve()
        if not candidate.is_relative_to(repo_root):
            continue
        # Decide on SIZE before paying for the read. Reading first meant an
        # 8.68 MB data artifact was fully loaded only to be rejected, and the
        # walk is ORDER BY file_path, so data/ is reached before src/.
        try:
            file_size = candidate.stat().st_size
        except OSError:
            continue
        if file_size > scan_byte_cap:
            # A file larger than the ENTIRE byte cap can never fit on any page,
            # at any position, so neither scanning it nor stopping on it can
            # help: one discards the page's whole budget, the other discards the
            # rest of the repository. It is skipped and named instead.
            #
            # The walk is ORDER BY file_path, so data/ is reached before src/.
            # Measured before this fix: bodygrep for "looprisks" -- present in
            # three files -- returned 0 matches at budget 20 unscoped, having
            # stopped 37 files in, because one 8.68 MB artifact under data/
            # exhausted the 5.24 MB budget alone. The same term returned 15
            # matches at budget 64 and 7 with target=src. A literal that exists
            # must never come back as a confident zero (NF-2026-00567).
            #
            # Skipping keeps the "always make progress" property the previous
            # scan-at-least-one rule was reaching for: the walk continues past
            # this file rather than ending on it, so a cursor still advances.
            oversized_skipped.append(file_path)
            scan_truncated = True
            continue
        if (
            not normalized_target
            and _is_repository_root_generated_data_jsonl(file_path)
        ):
            continue
        # A file that would merely overflow the REMAINING budget is ordinary
        # paging: mint a cursor and let the caller continue from it. At least
        # one in-budget file is always scanned per page, so the cursor moves.
        if files_scanned > 0 and bytes_scanned + file_size > scan_byte_cap:
            scan_truncated = True
            next_cursor = _encode_bodygrep_cursor(
                file_path, 0, term=term, target=normalized_target, budget=budget,
            )
            break
        try:
            raw = candidate.read_bytes()
        except OSError:
            continue
        bytes_scanned += len(raw)
        files_scanned += 1
        last_scanned_file = file_path
        # Reject files that provably cannot match before paying for a UTF-8
        # decode (or any line split), falling back to decode whenever the byte
        # filter cannot prove absence so Unicode casefold semantics stay exact.
        if _bodygrep_bytes_proves_no_match(needle, raw):
            # Release the candidate before the next read so two raw buffers are
            # never simultaneously live on the no-match fast path.
            del raw
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            del raw
            continue
        # Drop the raw bytes now: a matching file no longer needs them, and
        # holding raw + full decoded text + a splitlines list simultaneously is
        # the allocation shape this hot path must avoid.
        del raw
        # Skip lines already returned on a prior page for the resume file.
        start_after = resume_line if file_path == resume_file else 0
        hit_budget = False

        def match_row(line_number: int, line: str) -> dict[str, Any]:
            row = {
                "file_path": file_path,
                "kind": "body_match",
                "name": term,
                "qualname": f"{file_path}:{line_number}",
                "line_start": line_number,
                "line_end": line_number,
                "signature": line.strip()[:320],
                "evidence_label": "EXTRACTED",
                "confidence": 1.0,
            }
            if match_kind != "literal":
                row["match_kind"] = match_kind
            return row

        # token_and_file holds candidate rows (lines with at least one token)
        # until the whole file has proven it holds every token.
        held_rows: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        for line_number, line in enumerate(_iter_splitlines(text), start=1):
            if line_number <= start_after:
                continue
            folded = line.casefold()
            if match_kind == "literal":
                if needle not in folded:
                    continue
            elif match_kind == "token_and_line":
                if not all(token in folded for token in tokens):
                    continue
            else:
                present = [token for token in tokens if token in folded]
                if not present:
                    continue
                seen_tokens.update(present)
                if len(held_rows) < budget:
                    held_rows.append(match_row(line_number, line))
                continue
            matches.append(match_row(line_number, line))
            if len(matches) >= budget:
                scan_truncated = True
                hit_budget = True
                # Resume inside this same file past the last line returned, so
                # remaining matches here are not skipped and not duplicated.
                next_cursor = _encode_bodygrep_cursor(
                    file_path, line_number,
                    term=cursor_term, target=normalized_target, budget=budget,
                )
                break
        if match_kind == "token_and_file" and seen_tokens >= set(tokens):
            for row in held_rows:
                matches.append(row)
                if len(matches) >= budget:
                    scan_truncated = True
                    hit_budget = True
                    # A file-level proof cannot resume mid-file (the token set
                    # would restart), so the page resumes at the next file and
                    # the remaining rows of this one are declared truncated.
                    next_cursor = _encode_bodygrep_cursor(
                        file_path, _BODYGREP_SKIP_ALL_LINES,
                        term=cursor_term, target=normalized_target, budget=budget,
                    )
                    break
        # End the decoded string's lifetime before the next candidate is read:
        # a matching file's full text must not stay live alongside the next
        # candidate's raw bytes (and, once decoded, its text).
        del text
        if hit_budget:
            break
    if next_cursor is None and scan_truncated and last_scanned_file is not None:
        # The scan stopped at the file-count cap with every scanned file
        # complete. Resume strictly past the last scanned file so the caller
        # reaches the files the cap cut off.
        next_cursor = _encode_bodygrep_cursor(
            last_scanned_file, _BODYGREP_SKIP_ALL_LINES,
            term=cursor_term, target=normalized_target, budget=budget,
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
        # Named, not silent: a caller can tell "the literal is not there"
        # from "these files were never opened".
        "oversized_files_skipped": len(oversized_skipped),
        "oversized_files": oversized_skipped[:8],
        "target": normalized_target or None,
        "cursor": cursor,
        "next_cursor": next_cursor,
        "truncated": bool(output_truncated or scan_truncated or next_cursor is not None),
    }
    if match_kind != "literal":
        payload["match_kind"] = match_kind
        payload["query_tokens"] = list(tokens)
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
    """Atomically publish a self-check without mutating the canonical generation."""

    repo_root = repo_root.resolve()
    with index_write_lease(repo_root) as acquired:
        if not acquired:
            raise SourceGraphBuildInProgressError(
                f"source_graph_build_in_progress:{repo_root}"
            )
        canonical_path = resolve_db_path(repo_root)
        _cleanup_abandoned_staging(canonical_path)
        with _staged_generation(
            canonical_path, copy_existing=True
        ) as staging_path:
            conn = connect(staging_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO meta(key, value) "
                        "VALUES('recommendation_roundtrip', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (json.dumps(payload, ensure_ascii=False, sort_keys=True),),
                    )
            finally:
                conn.close()
            _publish_staged_generation(staging_path, canonical_path)


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


# Keys a fitted payload never loses: the query receipt, scope provenance,
# truncation/scan truth and paging state.  A trimmed reply must still say what
# was asked, where, and that it was trimmed.
_FIT_PROTECTED_KEYS: frozenset[str] = frozenset({
    "mode", "query", "budget", "target", "query_tokens", "query_tokens_source",
    "candidate_files", "truncated", "coverage", "cursor", "next_cursor",
    "freshness", "scope", "requested_target", "retrieval_reason",
    "retrieval_expression", "match_kind", "fit_dropped", "bundle_type",
    "files_scanned", "bytes_scanned", "scan_truncated", "scan_file_cap",
    "scan_byte_cap", "oversized_files_skipped", "refresh", "refreshed_files",
})
# Explicit fit priority (source_graph-1).  Sections that restate or decorate
# the primary rows are dropped WHOLE, least valuable first, before a single
# primary row is touched; ``git_signals`` goes first, ``related_tests`` is the
# last secondary list halved, and ``matches`` (rows with their line ranges)
# are halved only when nothing else is left.
_FIT_DROP_ORDER: tuple[str, ...] = (
    "git_signals", "todos", "risks", "hot_symbols", "ranked_symbols",
    "recommended_next_steps", "task_evidence", "insights", "entry_symbols",
    "oversized_files",
)
_FIT_SECONDARY_LISTS: tuple[str, ...] = (
    "neighbors", "cross_file_edges", "call_edges", "edges", "entities",
    "dependency_edges", "impacted_files", "outgoing_calls", "incoming_calls",
    "related_tests",
)
_FIT_PRIMARY_LISTS: tuple[str, ...] = (
    "matches", "sections", "contexts", "rows", "symbols", "results", "items",
)
# Per-row metric decorations (folded from the old ranked_symbols) that are
# stripped from primary rows before any primary row is dropped: a row without
# its priority score is still a usable line range; a dropped row is not.
_FIT_ROW_DECORATIONS: tuple[str, ...] = (
    "metrics_evidence", "line_span", "loop_count", "branch_count",
    "risk_reasons", "outgoing_calls", "incoming_calls", "priority_score",
)


def _fit_payload_bytes(payload: dict[str, Any], byte_cap: int) -> dict[str, Any]:
    """Deterministically trim a payload to ``byte_cap`` in explicit priority.

    Order: (1) whole restating sections in ``_FIT_DROP_ORDER``; (2) the
    largest long string (a body/preview) halved; (3) secondary top-level lists
    in ``_FIT_SECONDARY_LISTS`` order, halved (dropped when a single row is
    left); (4) per-row metric decorations stripped from primary rows, once;
    (5) the largest nested list inside primary rows, then the largest primary
    list, halved; (6) any remaining unprotected field, largest first.  Every
    loss sets ``truncated`` and names dropped sections in ``fit_dropped`` so a
    reader knows what is missing rather than guessing.
    """

    def encoded_size() -> int:
        return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def note_dropped(name: str) -> None:
        dropped = payload.get("fit_dropped")
        if not isinstance(dropped, list):
            dropped = []
            payload["fit_dropped"] = dropped
        if name not in dropped:
            dropped.append(name)
        payload["truncated"] = True

    # A key that restates the primary rows of a focus reply is the ANSWER of
    # some other mode: hotspots/complexity/bottlenecks return their ranking in
    # ``ranked_symbols``, coverage/testmap in ``related_tests``, todo in
    # ``todos``, churn/ownership/summarize in ``files``, calls in
    # ``outgoing_calls``/``incoming_calls``.  Dropping one of those whole would
    # return an empty reply that still claims hits, so the mode's own entry in
    # ``_ANALYTICS_RESULT_KEYS`` (the same registry paging and scope
    # enforcement read) promotes it to a primary list here.  ``a.b`` result
    # paths protect their top-level container.
    mode = str(payload.get("mode") or "")
    primary_keys = tuple(dict.fromkeys(
        _FIT_PRIMARY_LISTS
        + tuple(
            key.split(".", 1)[0]
            for key in _ANALYTICS_RESULT_KEYS.get(mode, ())
        )
    ))
    drop_order = tuple(key for key in _FIT_DROP_ORDER if key not in primary_keys)
    secondary_lists = tuple(
        key for key in _FIT_SECONDARY_LISTS if key not in primary_keys
    )

    stripped_row_metrics = False
    while encoded_size() > byte_cap:
        # 1. restating sections, whole, least valuable first
        section = next((key for key in drop_order if key in payload), None)
        if section is not None:
            del payload[section]
            note_dropped(section)
            continue

        # 2. long strings anywhere (bodies, previews, signatures)
        strings: list[tuple[int, dict[str, Any], str]] = []
        removable: list[tuple[int, dict[str, Any], str]] = []
        nested_lists: list[tuple[int, list[Any]]] = []

        def visit(value: Any, *, top: bool) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in _FIT_PROTECTED_KEYS:
                        continue
                    removable.append(
                        (len(json.dumps(item, ensure_ascii=False)), value, key)
                    )
                    if isinstance(item, str) and len(item) > 256:
                        strings.append((len(item), value, key))
                    elif isinstance(item, list):
                        if item and not (top and key in primary_keys):
                            nested_lists.append(
                                (len(json.dumps(item, ensure_ascii=False)), item)
                            )
                        for child in item:
                            visit(child, top=False)
                    else:
                        visit(item, top=False)
            elif isinstance(value, list):
                for child in value:
                    visit(child, top=False)

        visit(payload, top=True)
        if strings:
            _, owner, key = max(strings, key=lambda item: item[0])
            text = str(owner[key])
            owner[key] = text[: max(256, len(text) // 2)]
            payload["truncated"] = True
            continue

        # 3. secondary top-level lists, least valuable first
        secondary = next(
            (
                key for key in secondary_lists
                if isinstance(payload.get(key), list) and payload[key]
            ),
            None,
        )
        if secondary is not None:
            rows = payload[secondary]
            if len(rows) <= 1:
                del payload[secondary]
                note_dropped(secondary)
            else:
                del rows[len(rows) // 2:]
                payload["truncated"] = True
            continue

        # 4. metric decorations on primary rows, once
        if not stripped_row_metrics:
            stripped_row_metrics = True
            stripped = False
            for key in primary_keys:
                rows = payload.get(key)
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    for decoration in _FIT_ROW_DECORATIONS:
                        if decoration in row:
                            del row[decoration]
                            stripped = True
            if stripped:
                note_dropped("row_metrics")
                continue

        # 5. nested lists inside primary rows, then the primary lists
        if nested_lists:
            _, target = max(nested_lists, key=lambda item: item[0])
            del target[len(target) // 2:]
            payload["truncated"] = True
            continue
        primary = [
            (len(json.dumps(payload[key], ensure_ascii=False)), payload[key])
            for key in primary_keys
            if isinstance(payload.get(key), list) and payload[key]
        ]
        if primary:
            _, target = max(primary, key=lambda item: item[0])
            del target[len(target) // 2:]
            payload["truncated"] = True
            continue

        # 6. Nothing left is a compressible string or a shrinkable list --
        # every remaining non-protected value is small fixed scaffolding.
        # The byte cap is still a hard requirement, so drop whole fields,
        # largest encoded size first, until the cap is met or only
        # protected keys remain.
        if removable:
            _, owner, key = max(removable, key=lambda item: item[0])
            del owner[key]
            if owner is payload:
                note_dropped(key)
            else:
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
    retrieval: str = "ranked",
    include: tuple[str, ...] = (),
) -> dict[str, Any]:
    budget = max(1, min(int(budget), MAX_BUDGET_ROWS))
    byte_cap = max(512, budget * 512)
    db_path = resolve_db_path(repo_root)
    conn = connect(db_path, read_only=True)
    try:
        lookup = str(target or query).strip()
        matches = find(conn, lookup, limit=budget, retrieval=retrieval)
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
        if retrieval != "ranked":
            payload["retrieval_expression"] = retrieval
        if mode == "focus" and matches:
            # ``focus_insights`` returns the SAME matches with per-symbol
            # metrics folded onto each row; the update replaces the list.
            payload.update(sginsights.focus_insights(
                conn, repo_root, matches, budget=budget,
                include_git="git" in include,
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


def focus(
    repo_root: Path,
    query: str,
    budget: int = 64,
    *,
    retrieval: str = "ranked",
    include: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Ranked focus.  ``include=("git",)`` opts into per-file git signals."""

    return _query_payload(
        repo_root, "focus", query, budget, retrieval=retrieval, include=include,
    )


def slice_(
    repo_root: Path,
    query: str,
    budget: int = 64,
    *,
    target: str | None = None,
    retrieval: str = "ranked",
) -> dict[str, Any]:
    return _query_payload(
        repo_root, "slice", query, budget, target=target, retrieval=retrieval,
    )


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

# Modes whose sole result list holds RELATED evidence deliberately living
# outside the source target: coverage/testmap map in-scope source subjects to
# the tests exercising them, and those tests live in tests/, not under the
# source file. This list is therefore neither scope-filtered by owning file nor
# paged by the source-corpus offset (see _enforce_analytics_target_scope and
# analytics_query); the target still confines the source corpus the relations
# are derived from. auditmap is intentionally excluded: its result rows
# (audit_queue) are the in-scope source files themselves, which stay scoped.
_ANALYTICS_RELATED_EVIDENCE_MODES: frozenset[str] = frozenset({"coverage", "testmap"})


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


def _drop_analytics_result_prefix(
    payload: dict[str, Any], mode: str, offset: int
) -> dict[str, Any]:
    """Take the requested page out of the analytic's own ranked output.

    Paging used to slice the analytic's INPUT, so page two was a fresh ranking
    of the next arbitrary corpus rows rather than the next rows of one ranking.
    The analytic is now asked for ``offset + budget`` rows of a single ranking
    and this drops the rows already served, walking the same registered result
    keys ``_enforce_analytics_target_scope`` uses so the two cannot disagree
    about where a mode's rows live.
    """

    if offset <= 0:
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
            node[last] = value[offset:]
    return payload


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
    # A related-evidence mode (coverage/testmap) deliberately reports test rows
    # that live OUTSIDE the source target -- a source-file target selects source
    # subjects, but the tests exercising them live in tests/. Filtering that
    # derived list back to the owning-file scope stripped every real
    # relationship and reported returned=0 (NF-2026-00554). The target still
    # confines the source corpus these tests are computed FROM (the scoped
    # ``matches``/``files`` handed to the analytic); only the cross-scope result
    # list is exempt here. Every other mode's result rows are still re-asserted
    # to scope by owning file below.
    if mode not in _ANALYTICS_RELATED_EVIDENCE_MODES:
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


_ANALYTICS_SEMANTIC_SYMBOL_KINDS = frozenset({"annotation", "attribute", "decorator"})


def _include_semantic_symbols(
    payload: dict[str, Any], corpus: list[dict[str, Any]], *, limit: int,
) -> dict[str, Any]:
    """Add addressable non-executable semantic facts to symbols-mode rows."""

    symbols = payload.get("symbols")
    if not isinstance(symbols, list):
        symbols = []
    by_qualname = {
        str(row.get("qualname") or ""): row
        for row in symbols
        if isinstance(row, dict) and row.get("qualname")
    }
    for row in corpus:
        qualname = str(row.get("qualname") or "")
        if (
            str(row.get("kind") or "") not in _ANALYTICS_SEMANTIC_SYMBOL_KINDS
            or not qualname
            or qualname in by_qualname
        ):
            continue
        by_qualname[qualname] = {
            **row,
            "line_span": max(
                1, int(row.get("line_end") or 1) - int(row.get("line_start") or 1) + 1,
            ),
            "incoming_calls": 0,
            "outgoing_calls": 0,
            "loop_count": 0,
            "branch_count": 0,
            "priority_score": 0,
            "risk_reasons": [],
            "metrics_evidence": "not_applicable",
        }
    payload["symbols"] = sorted(
        by_qualname.values(),
        key=lambda row: (-int(row.get("priority_score") or 0), str(row.get("qualname") or "")),
    )[: max(1, limit)]
    return payload


_CALLS_EDGE_SELECT = (
    "SELECT DISTINCT e.src_qualname AS caller_symbol, e.file_path AS caller_file, "
    "e.dst_name AS callee_symbol, e.dst_qualname, t.file_path AS callee_file, "
    "e.line, e.evidence_label, e.confidence FROM edges e "
    "LEFT JOIN entities t ON t.qualname=e.dst_qualname "
)

_CALLS_QUERY_SYMBOL_CANDIDATE_CAP = 8

# Kinds that can actually own or receive a call edge. An ``import`` row
# re-binds a definition that lives in another file, and an
# ``annotation``/``attribute``/``decorator`` row only mentions one, so none of
# them can make a callable name ambiguous -- treating them as rival symbols
# would turn every imported function into an unanswerable ambiguous lookup.
_CALLS_DEFINITION_KINDS: frozenset[str] = frozenset(
    {"function", "method", "class", "struct"}
)


def _calls_edge_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("caller_symbol"), row.get("caller_file"),
        row.get("callee_symbol"), row.get("dst_qualname"), row.get("line"),
    )


def _definition_name_is_unique(conn: sqlite3.Connection, name: str) -> bool:
    """True when exactly one *definition* in the repository owns ``name``."""

    placeholders = ",".join("?" for _ in _CALLS_DEFINITION_KINDS)
    (count,) = conn.execute(
        "SELECT COUNT(DISTINCT qualname) FROM entities "
        f"WHERE name = ? AND kind IN ({placeholders})",
        (name, *sorted(_CALLS_DEFINITION_KINDS)),
    ).fetchone()
    return int(count) == 1


def _resolve_calls_query_symbol(
    conn: sqlite3.Connection, query: str, scope: str,
) -> tuple[list[dict[str, Any]], str]:
    """Resolve ``query`` to the exact symbol ``calls`` was asked about.

    Only an EXACT identifier resolves: the whole query must equal an
    entity's ``name`` or its ``qualname`` (either the stored ``::`` form or
    the dotted form the tool surface prints). A multi-word or partial query
    stays deliberately unresolved rather than being guessed at -- ``calls``
    publishes edges as fact, so a near miss would be published as a
    confident answer about a symbol nobody named.
    """

    term = str(query or "").strip()
    if not term or any(character.isspace() for character in term):
        return [], "unresolvable_query"
    clauses = [
        "kind NOT IN ('file', 'module')",
        "(name = ? OR qualname = ? OR REPLACE(qualname, '::', '.') = ?)",
    ]
    params: list[Any] = [term, term, term]
    if scope:
        predicate, scope_params = _analytics_scope_sql_predicate(scope)
        clauses.append(predicate)
        params.extend(scope_params)
    params.append(_CALLS_QUERY_SYMBOL_CANDIDATE_CAP + 1)
    rows = conn.execute(
        "SELECT file_path, kind, name, qualname, line_start, line_end FROM entities "
        "WHERE " + " AND ".join(clauses)
        + " ORDER BY file_path, line_start, qualname LIMIT ?",
        params,
    ).fetchall()
    by_qualname: dict[str, dict[str, Any]] = {}
    for row in rows:
        by_qualname.setdefault(str(row["qualname"] or ""), dict(row))
    candidates = list(by_qualname.values())
    definitions = [
        row for row in candidates
        if str(row.get("kind") or "") in _CALLS_DEFINITION_KINDS
    ]
    # A definition outranks a mention of it: an unscoped query naming an
    # imported function matched both the function and every ``import`` row
    # binding it, which read as ambiguity where there was exactly one symbol.
    # Only when nothing in range is a definition does the wider set decide.
    candidates = definitions or candidates
    if not candidates:
        return [], "no_exact_symbol_in_scope"
    if len(candidates) > 1:
        return candidates, "ambiguous_query_symbol"
    return candidates, "exact_symbol_match"


def _calls_edges_for_symbol(
    conn: sqlite3.Connection, symbol: dict[str, Any], *, limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Call edges whose caller or callee IS ``symbol``, not merely its file."""

    cap = max(1, int(limit))
    qualname = str(symbol.get("qualname") or "")
    name = str(symbol.get("name") or "")
    outgoing = [dict(row) for row in conn.execute(
        _CALLS_EDGE_SELECT
        + "WHERE e.kind='calls' AND e.src_qualname = ? "
        "ORDER BY e.confidence DESC, e.file_path, e.line LIMIT ?",
        (qualname, cap),
    )]
    incoming = [dict(row) for row in conn.execute(
        _CALLS_EDGE_SELECT
        + "WHERE e.kind='calls' AND e.dst_qualname = ? "
        "ORDER BY e.confidence DESC, e.file_path, e.line LIMIT ?",
        (qualname, cap),
    )]
    if name and len(incoming) < cap and _definition_name_is_unique(conn, name):
        # An unresolved call edge records its callee's NAME but not the owner
        # that name refers to. Attributing such an edge to this symbol is only
        # safe when the repository defines exactly one definition with that
        # name; otherwise a same-named function in another module would be
        # published as a caller of this one -- the false confidence being fixed.
        seen = {_calls_edge_key(row) for row in incoming}
        for row in conn.execute(
            _CALLS_EDGE_SELECT
            + "WHERE e.kind='calls' AND e.dst_qualname IS NULL AND e.dst_name = ? "
            "ORDER BY e.confidence DESC, e.file_path, e.line LIMIT ?",
            (name, cap),
        ):
            edge = dict(row)
            if _calls_edge_key(edge) in seen:
                continue
            incoming.append(edge)
            if len(incoming) >= cap:
                break
    return outgoing[:cap], incoming[:cap]


def _bind_calls_to_query_symbol(
    payload: dict[str, Any], conn: sqlite3.Connection, *,
    query: str, scope: str, limit: int,
) -> dict[str, Any]:
    """Make ``calls`` answer about the QUERY symbol, or say that it cannot.

    ``calls`` derived its edges from the scoped FILE list alone, so every
    call recorded anywhere in src/aiworkhub/task_store.py came back in line
    order for query ``review_feedback_identity`` and, byte for byte, for any
    unrelated word -- both labelled ``scope="query_matches"``, a confident
    claim about a symbol the rows were never selected for. The query now
    either resolves to exactly one symbol, in which case the edges returned
    are the ones whose caller or callee IS that symbol (callers in other
    files included -- that is the point of asking), or it does not resolve,
    in which case the same file-level rows are still returned but under a
    label that says what they actually are.
    """

    if not isinstance(payload, dict):
        return payload
    candidates, resolution = _resolve_calls_query_symbol(conn, query, scope)
    query_symbol: dict[str, Any] = {
        "requested": str(query or "").strip(),
        "resolved": None,
        "resolution": resolution,
    }
    if resolution == "exact_symbol_match":
        symbol = candidates[0]
        outgoing, incoming = _calls_edges_for_symbol(conn, symbol, limit=limit)
        payload["scope"] = "query_symbol_matches"
        payload["outgoing_calls"] = outgoing
        payload["incoming_calls"] = incoming
        payload["files"] = [str(symbol.get("file_path") or "")]
        query_symbol.update({
            "resolved": str(symbol.get("qualname") or ""),
            "file_path": symbol.get("file_path"),
            "line_start": symbol.get("line_start"),
            "line_end": symbol.get("line_end"),
        })
    elif resolution == "ambiguous_query_symbol":
        # Several distinct symbols answer to this name, so no single edge set
        # is the answer. Returning one of them would be exactly the confident
        # false match this mode used to publish, so the caller is handed the
        # candidate qualnames to re-ask with instead.
        payload["scope"] = "query_symbol_ambiguous"
        payload["outgoing_calls"] = []
        payload["incoming_calls"] = []
        query_symbol["candidates"] = [
            str(row.get("qualname") or "") for row in candidates
        ]
    else:
        payload["scope"] = "file_level_call_edges"
    payload["query_symbol"] = query_symbol
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
    # coverage/testmap answer "which tests relate to these in-scope source
    # subjects": their one result list is a bounded snapshot derived from the
    # WHOLE scoped source corpus at once, not a row-by-row page of it. A cursor
    # keyed on the source-corpus offset would page past that complete result
    # and re-derive the same bounded snapshot -- untruthful truncation -- so
    # these modes report returned/effective from their result list but never
    # mint or accept a cursor (NF-2026-00554).
    is_pageable_mode = (
        bool(_ANALYTICS_RESULT_KEYS.get(mode))
        and mode not in _ANALYTICS_RELATED_EVIDENCE_MODES
    )
    conn = connect(resolve_db_path(repo_root), read_only=True)
    try:
        cursor_provided = bool(cursor is not None and str(cursor).strip())
        offset = _decode_analytics_cursor(
            cursor, mode=mode, query=query, target=normalized_target, budget=budget,
        )
        if cursor_provided and not is_pageable_mode:
            # This mode never mints a cursor (its result is one aggregate
            # snapshot, not a row page), so ANY provided cursor -- including
            # one that decodes to a valid zero offset -- can only be forged or
            # stale. Guarding on ``offset`` alone silently accepted a
            # zero-offset cursor for coverage/testmap (NF-2026-00554).
            raise SourceGraphError("invalid_cursor")
        # An explicit target is a SCOPE, not a filter. The corpus was previously
        # built the other way round -- ``find(query)`` decided membership and the
        # target then filtered what that query happened to match -- so a lens saw
        # only the symbols whose names matched the caller's words, and a caller
        # who described the scope accurately got the narrowest answer. Measured
        # on this repository, ``complexity`` over src/aiworkhub/source_graph.py:
        #
        #   query "source_graph"  -> eligible 7    top symbol 3 branches
        #   query "zzqqxx"        -> eligible 159  top symbol 16 branches
        #
        # The nonsense query found index_write_lease, the genuinely most complex
        # function in that file; the accurate one never saw it. A mode whose job
        # is "rank this scope" cannot have its membership decided by name match
        # (NF-2026-00564).
        #
        # The scoped corpus also escapes find's tighter clamp. ``find`` re-clamps
        # to MAX_BUDGET_ROWS, so a find-backed corpus could never exceed 200 rows
        # -- 0.9% of this repository -- whatever MAX_ANALYTICS_CORPUS_ROWS said.
        #
        # The query is not discarded: it still reaches every per-mode analytic as
        # ``query_text``, so a mode that ranks by relevance still can. What it no
        # longer does is decide who is in scope.
        if normalized_target:
            corpus = _scoped_entity_rows(
                conn, normalized_target, limit=MAX_ANALYTICS_CORPUS_ROWS,
            )
            corpus_cap = MAX_ANALYTICS_CORPUS_ROWS
        else:
            corpus = find(conn, query, limit=MAX_ANALYTICS_CORPUS_ROWS)
            corpus_cap = min(MAX_ANALYTICS_CORPUS_ROWS, MAX_BUDGET_ROWS)
            if not corpus:
                # No scope and nothing matched: the repository itself is the
                # corpus, which is what the per-mode analytic reports as
                # ``repository_fallback``. Only the UNSCOPED case falls back --
                # an explicit target with no rows stays empty below, so a scope
                # can never be widened back out to the whole repository.
                corpus = _scoped_entity_rows(
                    conn, normalized_target, limit=MAX_ANALYTICS_CORPUS_ROWS,
                )
                corpus_cap = MAX_ANALYTICS_CORPUS_ROWS
        corpus_capped = len(corpus) >= corpus_cap
        total_eligible = len(corpus)
        if offset and offset >= total_eligible:
            raise SourceGraphError("invalid_cursor")
        # The analytic sees the WHOLE scoped corpus, never a pre-sliced window.
        # Slicing the input first made ``budget`` decide the population instead
        # of the response size, so a ranked mode answered a different question
        # at every budget. Measured on complexity over source_graph.py:
        #
        #   budget 5   -> top resolve_db_path    (2 branches)
        #   budget 20  -> top index_write_lease  (16 branches)
        #   budget 60  -> top bodygrep_query     (39 branches)
        #
        # "The most complex symbol here" cannot depend on how many rows the
        # caller asked for. Ranking the full corpus costs 18 ms for this file
        # and 5 ms for all of src/, so the pre-slice bought nothing either.
        #
        # Paging then applies to the analytic's RANKED OUTPUT rather than to the
        # input order: the mode is asked for ``offset + budget`` rows of one
        # consistent ranking and the window is taken from that, so page two is
        # the next rows of the same ranking instead of a fresh ranking of the
        # next arbitrary input rows (NF-2026-00564 / NF-2026-00566).
        analytic_budget = offset + budget if is_pageable_mode else budget
        page_len = max(0, min(budget, total_eligible - offset))
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
                matches=corpus, budget=analytic_budget,
            )
            if mode == "symbols":
                payload = _include_semantic_symbols(
                    payload, corpus, limit=analytic_budget,
                )
            if mode == "calls":
                # ``calls`` publishes edges as fact, so it has to answer about
                # the symbol it was asked about rather than about whatever the
                # scoped file happens to call (NF-2026-00861). This runs before
                # paging and before scope re-assertion so a page is a page of
                # that symbol's own edges, and so the engine still gets the last
                # word on scope afterwards.
                payload = _bind_calls_to_query_symbol(
                    payload, conn, query=query, scope=normalized_target,
                    limit=analytic_budget,
                )
            if offset:
                payload = _drop_analytics_result_prefix(payload, mode, offset)
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
        # ``scanned`` equals what the analytic examined. Filter modes receive the
        # whole scoped corpus and report that via ``analysis.symbols_scanned``
        # (equal to ``eligible`` when every in-scope row was read). Modes that
        # still slice their own input to one page and omit a scan count use
        # ``page_len``. Returned rows stay budget-bounded.
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
        # A related-evidence mode (coverage/testmap) never mints a cursor, so the
        # top-level truncated flag is the only truthful way to tell a caller that
        # more tests relate to the scope than this bounded snapshot returned. The
        # per-mode analytic already counted the full bounded population in
        # ``candidate_test_files``; a returned list shorter than that count -- from
        # the display cap or a byte trim -- is a truncated result (NF-2026-00554).
        related_evidence_truncated = False
        if mode in _ANALYTICS_RELATED_EVIDENCE_MODES:
            mapping = payload.get("structural_mapping")
            if isinstance(mapping, dict) and isinstance(
                mapping.get("candidate_test_files"), int
            ):
                related_evidence_truncated = returned < int(
                    mapping["candidate_test_files"]
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
        payload["truncated"] = (
            next_cursor is not None or byte_trimmed or related_evidence_truncated
        )
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
        if insights:
            # A bundle already carries every matched file as a ``sections``
            # context, so its insight block restates only the SCORED symbols
            # -- exactly what ``ranked_symbols`` used to hold, in the same
            # ``(-priority_score, qualname)`` order, now with the hot/risk
            # projections folded onto the row.  Folding the whole match list
            # here instead would ADD ~25% to the block (measured 12,852 ->
            # 15,902 bytes on a 32-row query) rather than remove a duplicate.
            insights["matches"] = sorted(
                (row for row in insights["matches"] if "priority_score" in row),
                key=lambda row: (-int(row["priority_score"]), str(row.get("qualname") or "")),
            )
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
    conn = connect(db_path, read_only=True)
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


@dataclass(frozen=True, slots=True)
class _AuthenticatedFileSnapshot:
    raw: bytes
    source_hash: str
    file_size: int
    mtime_ns: int


def _open_nofollow_directory_component(
    part: str,
    *,
    parent_fd: int,
    dir_flags: int,
    error_display: str | Path,
) -> int:
    """Open one directory component and transfer ownership to the caller."""

    try:
        next_fd = os.open(part, dir_flags, dir_fd=parent_fd)
    except OSError as exc:
        reason = "symlink" if exc.errno == errno.ELOOP else "unreadable"
        raise SourceGraphError(
            f"source_graph_single_file_{reason}:{error_display}"
        ) from exc
    try:
        dir_stat = os.fstat(next_fd)
    except OSError as exc:
        os.close(next_fd)
        raise SourceGraphError(
            f"source_graph_single_file_unreadable:{error_display}"
        ) from exc
    if not stat.S_ISDIR(dir_stat.st_mode):
        os.close(next_fd)
        raise SourceGraphError(
            f"source_graph_single_file_non_directory:{error_display}"
        )
    return next_fd


def _read_authenticated_snapshot_from_verified_fd(
    fd: int,
    rel_display: str,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> _AuthenticatedFileSnapshot:
    """Read and size-bound a snapshot from an fd already proven regular.

    ``expected_identity`` is the ``(st_dev, st_ino)`` a caller observed via a
    pre-open ``lstat`` on a host with no ``dir_fd``/``O_NOFOLLOW`` (Windows):
    when given, a mismatch means the path was swapped between that lstat and
    this open, and the read is refused rather than trusting whatever now sits
    there.
    """

    try:
        file_stat = os.fstat(fd)
    except OSError as exc:
        raise SourceGraphError(
            f"source_graph_single_file_unreadable:{rel_display}"
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise SourceGraphError(
            f"source_graph_single_file_non_regular:{rel_display}"
        )
    if expected_identity is not None and (
        not file_stat.st_ino
        or (file_stat.st_dev, file_stat.st_ino) != expected_identity
    ):
        raise SourceGraphError(f"source_graph_single_file_symlink:{rel_display}")
    file_size = int(file_stat.st_size)
    limit = SOURCE_GRAPH_AUTHENTICATED_FILE_BYTE_LIMIT
    if file_size > limit:
        raise SourceGraphError(
            f"source_graph_single_file_too_large:{rel_display}:"
            f"size={file_size} limit={limit}"
        )
    raw = bytearray()
    try:
        while True:
            read_size = min(1024 * 1024, limit + 1 - len(raw))
            if read_size <= 0:
                raise SourceGraphError(
                    f"source_graph_single_file_too_large:{rel_display}:"
                    f"size>{limit} limit={limit}"
                )
            chunk = os.read(fd, read_size)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > limit:
                raise SourceGraphError(
                    f"source_graph_single_file_too_large:{rel_display}:"
                    f"size>{limit} limit={limit}"
                )
            if len(raw) > file_size:
                raise SourceGraphError(
                    f"source_graph_single_file_unstable_size:{rel_display}"
                )
    except OSError as exc:
        raise SourceGraphError(
            f"source_graph_single_file_unreadable:{rel_display}"
        ) from exc
    if len(raw) != file_size:
        raise SourceGraphError(
            f"source_graph_single_file_unstable_size:{rel_display}"
        )
    authenticated = bytes(raw)
    return _AuthenticatedFileSnapshot(
        raw=authenticated,
        source_hash=sgast.sha256_bytes(authenticated),
        file_size=file_size,
        mtime_ns=int(file_stat.st_mtime_ns),
    )


def _dir_fd_walk_supported() -> bool:
    """True when this host can walk a path with ``dir_fd``-relative opens.

    Windows offers neither ``O_NOFOLLOW`` nor ``dir_fd``-relative ``os.open``,
    so this is false there and the caller degrades to the ``lstat``-walk
    fallback below instead of failing the whole feature closed.
    """

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if isinstance(nofollow, bool) or not isinstance(nofollow, int) or nofollow <= 0:
        return False
    if not isinstance(getattr(os, "O_DIRECTORY", None), int):
        return False
    return os.open in os.supports_dir_fd


def _windows_reparse_or_symlink(st: os.stat_result) -> bool:
    """Mirrors ``terminal_authority._windows_link_identity``'s reparse check."""

    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(
        stat.S_ISLNK(st.st_mode)
        or (reparse and getattr(st, "st_file_attributes", 0) & reparse)
    )


def _open_authenticated_regular_file_snapshot_dir_fd(
    repo_root: Path,
    rel_path: Path,
) -> _AuthenticatedFileSnapshot:
    """POSIX path: a real no-follow, race-free walk via ``dir_fd``.

    Only reached when ``_dir_fd_walk_supported()`` has already confirmed both
    flags exist; ``getattr`` here is for the type checker (these names are
    unresolvable on a Windows platform stub), not a runtime fallback.
    """

    nofollow = getattr(os, "O_NOFOLLOW")
    directory = getattr(os, "O_DIRECTORY")
    cloexec = getattr(os, "O_CLOEXEC", None)
    dir_flags = os.O_RDONLY | nofollow | directory
    if isinstance(cloexec, int):
        dir_flags |= cloexec

    descriptors: list[int] = []
    rel_display = rel_path.as_posix()
    try:
        try:
            root_fd = os.open(os.sep, dir_flags)
        except OSError as exc:
            raise SourceGraphError(
                f"source_graph_single_file_unreadable:{repo_root}"
            ) from exc
        descriptors.append(root_fd)

        parent_fd = root_fd
        for part in repo_root.parts[1:]:
            next_fd = _open_nofollow_directory_component(
                part,
                parent_fd=parent_fd,
                dir_flags=dir_flags,
                error_display=repo_root,
            )
            descriptors.append(next_fd)
            parent_fd = next_fd

        parts = rel_path.parts
        for part in parts[:-1]:
            next_fd = _open_nofollow_directory_component(
                part,
                parent_fd=parent_fd,
                dir_flags=dir_flags,
                error_display=rel_display,
            )
            descriptors.append(next_fd)
            parent_fd = next_fd

        file_flags = os.O_RDONLY | nofollow
        if isinstance(cloexec, int):
            file_flags |= cloexec
        try:
            fd = os.open(parts[-1], file_flags, dir_fd=parent_fd)
        except OSError as exc:
            reason = "symlink" if exc.errno == errno.ELOOP else "unreadable"
            raise SourceGraphError(
                f"source_graph_single_file_{reason}:{rel_display}"
            ) from exc
        descriptors.append(fd)
    except Exception:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise

    try:
        return _read_authenticated_snapshot_from_verified_fd(fd, rel_display)
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _open_authenticated_regular_file_snapshot_lstat_walk(
    repo_root: Path,
    rel_path: Path,
) -> _AuthenticatedFileSnapshot:
    """Best-effort fallback where the host has neither ``dir_fd`` nor
    ``O_NOFOLLOW`` (Windows).

    Each path component -- including ``repo_root``'s OWN ancestry all the way
    up to the filesystem anchor, exactly like the ``dir_fd`` walk's
    root-to-``repo_root`` component loop above, not just the components below
    it -- is ``lstat``-checked for a symlink/reparse point before the walk
    trusts it, mirroring ``terminal_authority._windows_link_identity``. A
    single ``lstat`` on the full path would let the OS silently follow a
    symlinked ANCESTOR (``lstat`` only refuses the final component), so this
    walks one component at a time instead. Unlike the ``dir_fd`` walk, a
    check and the next descent are not atomic with each other -- Windows has
    no directory-relative open to close that window -- so the final file's
    identity is re-verified against its own pre-open ``lstat`` after opening,
    refusing a swap performed in that last, narrower window.
    """

    rel_display = rel_path.as_posix()

    def _check_directory_component(candidate: Path, error_display: object) -> None:
        try:
            component_stat = candidate.lstat()
        except OSError as exc:
            raise SourceGraphError(
                f"source_graph_single_file_unreadable:{error_display}"
            ) from exc
        if _windows_reparse_or_symlink(component_stat):
            raise SourceGraphError(
                f"source_graph_single_file_symlink:{error_display}"
            )
        if not stat.S_ISDIR(component_stat.st_mode):
            raise SourceGraphError(
                f"source_graph_single_file_non_directory:{error_display}"
            )

    current = Path(repo_root.anchor)
    for part in repo_root.parts[1:]:
        current = current / part
        _check_directory_component(current, repo_root)

    parts = rel_path.parts
    for part in parts[:-1]:
        current = current / part
        _check_directory_component(current, rel_display)

    file_path = current / parts[-1]
    try:
        link_stat = file_path.lstat()
    except OSError as exc:
        raise SourceGraphError(
            f"source_graph_single_file_unreadable:{rel_display}"
        ) from exc
    if _windows_reparse_or_symlink(link_stat):
        raise SourceGraphError(f"source_graph_single_file_symlink:{rel_display}")
    if not stat.S_ISREG(link_stat.st_mode):
        raise SourceGraphError(
            f"source_graph_single_file_non_regular:{rel_display}"
        )

    try:
        fd = os.open(file_path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError as exc:
        raise SourceGraphError(
            f"source_graph_single_file_unreadable:{rel_display}"
        ) from exc
    try:
        return _read_authenticated_snapshot_from_verified_fd(
            fd,
            rel_display,
            expected_identity=(link_stat.st_dev, link_stat.st_ino),
        )
    finally:
        os.close(fd)


def _open_authenticated_regular_file_snapshot(
    repo_root: Path,
    rel_path: Path,
) -> _AuthenticatedFileSnapshot:
    """Read one full-component no-follow regular-file snapshot."""

    if _dir_fd_walk_supported():
        return _open_authenticated_regular_file_snapshot_dir_fd(repo_root, rel_path)
    return _open_authenticated_regular_file_snapshot_lstat_walk(repo_root, rel_path)


# ---------------------------------------------------------------------------
# Task 3: bounded LSP enrichment, run outside the SQLite merge transaction
# ---------------------------------------------------------------------------
#
# Coordinate convention -- one conversion, in one place, and it is not here:
#   * ``DefinitionQuery.line`` and ``LspDefinitionResult.source_line`` carry
#     1-based graph lines, the same numbers ``edges.line`` stores.
#     ``sglsp.graph_line_to_lsp`` is the single 1-based -> 0-based conversion
#     and it lives at the wire boundary inside the transport. Nothing in this
#     module shifts a source line, so a first-line edge is queryable and a
#     binding lands on exactly the edge that asked for it.
#   * ``DefinitionQuery.column`` and ``source_column`` are 0-based UTF-8 byte
#     offsets. ``edges.source_col`` is -1 where an extractor records no
#     column, so the wire column is ``max(source_col, 0)`` and binding matches
#     on that same expression -- the mapping stays one-to-one either way.
#   * ``target_range`` stays in LSP 0-based coordinates, counted in the
#     negotiated position encoding. ``entities.line_start`` is 1-based, so
#     ``target_range[0] + 1`` is the one target-side shift.
#
# Receipt/provenance lifecycle -- one invariant every writer keeps:
#   * a file's ``lsp_edge_provenance`` rows are exactly the bindings its
#     ``edges`` carry, and its receipt (when present) counts exactly them;
#   * a binding owns exactly one edge -- the one its ``edge_identity`` names
#     -- and only that edge is ever carried, re-attached or restored. Another
#     edge on the same token, resolved lexically to the binding's target or
#     to any other, is never touched (see ``_lsp_owned_edge``);
#   * a file with provenance always has a receipt; a receipt is reusable only
#     when ``complete`` -- every position was asked and every answer arrived;
#   * ``_lsp_reconcile_generation`` restores that invariant in plain SQL
#     inside every merge (full build, ``index_file``, ``remove_file``), so a
#     changed/deleted source or target is revoked with no server running.
#
# Publication -- the canonical generation is never written in place:
#   * a pass publishes at most one staged generation, and only when evidence
#     changes; a refresh that reused every receipt stays read-only;
#   * a pass that raised is written into health (``failed_passes``) by a
#     staged publication of its own, so no earlier green can outlive it;
#   * a receipt binds the observed identity of the server's bytes, so a
#     server replaced at the same path is never mistaken for the old one.

LSP_ENRICHMENT_SCHEMA_ID = "aiworkhub.source_graph.lsp_enrichment.v4"
LSP_HEALTH_META_KEY = "lsp_health"
LSP_PROVENANCE_TABLE = "lsp_edge_provenance"
LSP_RECEIPT_TABLE = "lsp_file_receipt"
LSP_SERVER_VERSION_ENV = "AIWORKHUB_LSP_SERVER_VERSION"
# Task 3 never discovers a language server: enrichment runs only where the
# environment names one, so an unconfigured repository indexes exactly as it
# did before this feature existed.  Live server qualification is Task 4.
# Each group is (spec language, environment override, indexed languages). The
# languages in a group share one server AND one bounded workspace, because a
# TypeScript server resolving a ``.js`` caller needs the ``.ts`` declaration
# in the same workspace to resolve it to.
LSP_SERVER_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("python", "AIWORKHUB_LSP_PYTHON_SERVER", ("python",)),
    ("typescript", "AIWORKHUB_LSP_TYPESCRIPT_SERVER", ("javascript", "typescript")),
)
# Edge units (one per definition position). Every attempted position lands in
# exactly one classification, so ``attempted`` always equals their sum.
# ``unpositioned`` counts unresolved edges an extractor recorded no exact
# call-site coordinate for: they are never asked at a guessed one.
LSP_EDGE_CLASSIFICATIONS: tuple[str, ...] = (
    "internal", "external", "ambiguous", "unresolved", "stale", "unavailable",
)
LSP_EDGE_COUNTERS: tuple[str, ...] = (
    "attempted", "skipped", "unpositioned", "enriched", "reused", "revoked",
    "discarded",
) + LSP_EDGE_CLASSIFICATIONS
# File units (one per caller file considered by a batch).
LSP_FILE_COUNTERS: tuple[str, ...] = (
    "files_attempted", "files_skipped", "files_reused", "files_revoked",
    "files_deferred", "files_incomplete",
)
LSP_SUM_COUNTERS: tuple[str, ...] = (
    LSP_EDGE_COUNTERS + LSP_FILE_COUNTERS
    # Pass units: ``failed_passes`` counts passes that raised after publish.
    + ("batches", "latency_ms_total", "failed_passes")
)
LSP_MAX_COUNTERS: tuple[str, ...] = ("latency_ms_max",)
LSP_HEALTH_COUNTERS: tuple[str, ...] = LSP_SUM_COUNTERS + LSP_MAX_COUNTERS
LSP_MAX_QUERIES_PER_FILE = 256
LSP_MAX_FILES_PER_BATCH = 512
LSP_REQUEST_TIMEOUT_S = sglsp.DEFAULT_REQUEST_TIMEOUT_S
LSP_BATCH_TIMEOUT_S = sglsp.DEFAULT_BATCH_TIMEOUT_S
# How long a pass waits out another writer's merge before it publishes.
LSP_LEASE_WAIT_S = 15.0
LSP_LEASE_POLL_S = 0.05
# Server identity: how far above an executed file its package manifest may
# sit, how long a file must sit unchanged before its digest may be cached,
# and how many digests one process keeps.
LSP_MANIFEST_SEARCH_DEPTH = 3
LSP_DIGEST_SETTLE_S = 2.0
LSP_MAX_EXECUTABLE_DIGEST_ENTRIES = 64
_LSP_EXECUTABLE_DIGEST_CACHE: dict[tuple[Any, ...], str] = {}
# A module/import row can start on the same line as the declaration a server
# pointed at, so neither may ever stand in for a canonical definition.
LSP_NON_DECLARATION_KINDS: frozenset[str] = frozenset({"module", "import"})


@dataclass(frozen=True)
class _LspBinding:
    """One verified caller edge -> canonical declaration, with its evidence."""

    source_path: str
    source_line: int
    source_column: int
    source_hash: str
    target_path: str
    target_hash: str
    target_qualname: str
    target_name: str
    target_line_start: int
    target_range: tuple[int, ...]
    classification: str
    result_config_digest: str
    prior_evidence_label: str = ""
    prior_confidence: float = 0.0
    # Which edge at the position the binding owns (``_lsp_edge_identity``);
    # a verified answer has none until ``_lsp_bind_edge`` picks its edge.
    edge_identity: str = ""


@dataclass(frozen=True)
class _LspFilePlan:
    """One file whose positions need a language server this run."""

    rel: str
    source_hash: str
    queries: tuple[sglsp.DefinitionQuery, ...]
    had_evidence: bool
    complete: bool


@dataclass(frozen=True)
class _LspFileOutcome:
    """What the commit step should publish for one file.

    ``action`` is ``"publish"`` (replace the file's evidence with
    ``bindings``) or ``"clear"`` (revoke everything, keep no receipt).
    """

    rel: str
    source_hash: str
    action: str
    bindings: tuple[_LspBinding, ...] = ()
    complete: bool = False
    latency_ms: int = -1


@dataclass
class _LspGroupRun:
    """One server group's pass: resolved with no lease held, not yet published."""

    label: str
    languages: tuple[str, ...]
    spec: sglsp.LspServerSpec
    counters: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(LSP_HEALTH_COUNTERS, 0)
    )
    outcomes: dict[str, _LspFileOutcome] = field(default_factory=dict)
    digest: str = ""
    status: str = "skipped"
    reason: str = ""
    files: int = 0
    latency_ms: int = -1

    def summary(self) -> dict[str, Any]:
        status = self.status
        if status == "skipped" and self.counters["files_reused"]:
            status = "reused"
        return _lsp_summary(
            status,
            self.label,
            reason=self.reason,
            counters=self.counters,
            files=self.files,
            latency_ms=self.latency_ms,
        )


def _lsp_group_for_language(
    language: str,
) -> tuple[str, str, tuple[str, ...]] | None:
    """The server group that indexes ``language``; ``None`` when unsupported."""

    for group in LSP_SERVER_GROUPS:
        if language in group[2]:
            return group
    return None


def _lsp_file_digest(path: str) -> str | None:
    """sha256 of one regular file's bytes, or ``None`` when it cannot be read.

    Hashing the same large server binary on every refresh would be wasted
    work, so a digest is cached under the exact stat identity of the
    descriptor that was read: device, inode, size and both timestamps. A file
    replaced or rewritten at the same path presents a new identity and is
    hashed again. Timestamps only move once per clock tick, so a file changed
    moments ago is never cached -- a second write inside the same tick could
    otherwise present an identical stat and be served the first one's digest.
    """

    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            return None
        key = (
            path, status.st_dev, status.st_ino, status.st_size,
            status.st_mtime_ns, status.st_ctime_ns,
        )
        cached = _LSP_EXECUTABLE_DIGEST_CACHE.get(key)
        if cached is not None:
            return cached
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1 << 20):
            digest.update(chunk)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    value = digest.hexdigest()
    if time.time() - max(status.st_mtime, status.st_ctime) > LSP_DIGEST_SETTLE_S:
        if len(_LSP_EXECUTABLE_DIGEST_CACHE) >= LSP_MAX_EXECUTABLE_DIGEST_ENTRIES:
            _LSP_EXECUTABLE_DIGEST_CACHE.clear()
        _LSP_EXECUTABLE_DIGEST_CACHE[key] = value
    return value


def _lsp_package_manifest(real_path: str) -> str:
    """The nearest ``package.json`` a few directories above a file, or ``""``."""

    directory = os.path.dirname(real_path)
    for _depth in range(LSP_MANIFEST_SEARCH_DEPTH):
        candidate = os.path.join(directory, "package.json")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return ""


def _lsp_server_identity(command: tuple[str, ...]) -> str:
    """Immutable identity of the exact bytes a server command runs.

    The declared ``AIWORKHUB_LSP_SERVER_VERSION`` is optional, so it can never
    be what revokes a receipt when the executable behind an unchanged command
    is replaced. This binds what the command resolves to: the executable
    (``PATH`` lookup and symlinks followed, as ``exec`` follows them), every
    absolute-path file argument (the script an interpreter runs), and the
    nearest ``package.json`` above each -- the manifest an npm upgrade
    rewrites even when the entry shim it installs is byte-identical. Anything
    else a server loads is what the declared version is for. A file that
    cannot be read identifies nothing, so that run gets a nonce no stored
    receipt can ever match: it is re-asked every time and never reused.
    """

    executable = command[0] if command else ""
    if executable and os.sep not in executable and not (
        os.altsep and os.altsep in executable
    ):
        executable = shutil.which(executable) or ""
    members = [executable, *(
        argument for argument in command[1:]
        if os.path.isabs(argument) and os.path.isfile(argument)
    )]
    paths: list[str] = []
    for member in members:
        real = os.path.realpath(member) if member else ""
        paths.append(real)
        manifest = _lsp_package_manifest(real) if real else ""
        if manifest:
            paths.append(manifest)
    fingerprint: list[str] = []
    for path in paths:
        digest = _lsp_file_digest(path) if path else None
        if digest is None:
            return f"unidentified:{secrets.token_hex(16)}"
        fingerprint.append(f"{path}\0{digest}")
    joined = "\n".join(fingerprint).encode("utf-8", "surrogateescape")
    return "sha256:" + hashlib.sha256(joined).hexdigest()


def _lsp_server_spec(
    group: tuple[str, str, tuple[str, ...]],
) -> sglsp.LspServerSpec | None:
    """Resolve the configured server for ``group``; ``None`` when unset.

    ``version`` is the observed identity of the bytes the command runs,
    after the declared ``AIWORKHUB_LSP_SERVER_VERSION`` when one is set.
    Receipts and provenance record it and the config digest covers it, so a
    server replaced at the same path -- with nothing declared -- revokes
    every receipt its predecessor's answers earned.
    """

    name, env_name, _languages = group
    try:
        command = tuple(shlex.split(os.environ.get(env_name, "")))
    except ValueError:
        return None
    if not command:
        return None
    declared = os.environ.get(LSP_SERVER_VERSION_ENV, "").strip()
    identity = _lsp_server_identity(command)
    return sglsp.LspServerSpec(
        command=command,
        version=f"{declared}+{identity}" if declared else identity,
        language=name,
    )


def _lsp_server_installed(command: tuple[str, ...]) -> bool:
    """Is the configured server present on this host at all?

    Executability, spawn failures and protocol errors stay the transport's
    fail-closed business -- they arrive as results carrying ``failure``.
    This bounded existence check only avoids materialising a workspace for a
    server that is not installed.
    """

    executable = command[0] if command else ""
    if not executable:
        return False
    if os.sep in executable or (os.altsep and os.altsep in executable):
        return Path(executable).is_file()
    return shutil.which(executable) is not None


def _lsp_summary(
    status: str,
    language: str,
    *,
    reason: str = "",
    counters: dict[str, int] | None = None,
    files: int = 0,
    latency_ms: int = -1,
) -> dict[str, Any]:
    """The typed enrichment outcome ``index_file``/``build_index`` return."""

    counts = counters or {}
    return {
        "schema_id": LSP_ENRICHMENT_SCHEMA_ID,
        "status": status,
        "reason": reason,
        "language": language,
        "attempted": int(counts.get("attempted", 0)),
        "enriched": int(counts.get("enriched", 0)),
        "reused": int(counts.get("reused", 0)),
        "revoked": int(counts.get("revoked", 0)),
        "skipped": int(counts.get("skipped", 0)),
        "unavailable": int(counts.get("unavailable", 0)),
        "files_deferred": int(counts.get("files_deferred", 0)),
        "files": int(files),
        "latency_ms": int(latency_ms),
    }


def _lsp_meta_document(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return {}
    try:
        document = json.loads(str(row["value"]))
    except (TypeError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _lsp_store_meta_document(
    conn: sqlite3.Connection, key: str, document: dict[str, Any]
) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(document, sort_keys=True)),
    )


def _lsp_tables_present(conn: sqlite3.Connection) -> bool:
    """Do this generation's provenance tables exist?

    ``connect`` creates them through ``SCHEMA`` on every write open, so a
    generation published before this feature existed migrates the first time
    a writer touches it. Read-only callers must tolerate their absence rather
    than raise on an older database.
    """

    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('lsp_edge_provenance', 'lsp_file_receipt')"
    ).fetchall()
    return len(rows) == 2


def _lsp_indexed_language(conn: sqlite3.Connection, rel: str) -> str:
    row = conn.execute(
        "SELECT language FROM files WHERE file_path=?", (rel,)
    ).fetchone()
    return str(row["language"]) if row is not None else ""


def _lsp_indexed_hash(conn: sqlite3.Connection, rel: str) -> str:
    row = conn.execute(
        "SELECT source_hash FROM files WHERE file_path=?", (rel,)
    ).fetchone()
    return str(row["source_hash"]) if row is not None else ""


def _lsp_indexed_sources(
    conn: sqlite3.Connection, languages: tuple[str, ...]
) -> tuple[sglsp.IndexedSource, ...]:
    """The bounded workspace this server group is allowed to see."""

    placeholders = ",".join("?" for _ in languages)
    return tuple(
        sglsp.IndexedSource(
            relative_path=str(row["file_path"]),
            source_hash=str(row["source_hash"]),
        )
        for row in conn.execute(
            "SELECT file_path, source_hash FROM files "
            f"WHERE language IN ({placeholders}) ORDER BY file_path",
            languages,
        )
    )


def _lsp_group_files(
    conn: sqlite3.Connection,
    languages: tuple[str, ...],
    restrict: frozenset[str] | None,
) -> tuple[tuple[str, str], ...]:
    """Every candidate caller file; the file cap applies to *asked* files."""

    return tuple(
        (item.relative_path, item.source_hash)
        for item in _lsp_indexed_sources(conn, languages)
        if restrict is None or item.relative_path in restrict
    )


def _lsp_config_digest(
    spec: sglsp.LspServerSpec, sources: tuple[sglsp.IndexedSource, ...]
) -> str:
    """Digest the exact enrichment inputs: server, version and bounded tree.

    The tree is every workspace member's path AND indexed content hash, so a
    receipt is reusable only for a true no-op. Changing, adding or deleting
    any member -- including a file a null answer never named, which may now
    declare what the caller asked for -- or the server identity changes this
    digest. That is what revokes a receipt taken under the old tree, and what
    refuses a batch whose tree moved while the server was running.
    """

    return sglsp.config_digest(
        command=spec.command,
        version=spec.version,
        include=tuple(
            f"{item.source_hash} {item.relative_path}" for item in sources
        ),
        exclude=sglsp.PYRIGHT_EXCLUDE,
        position_encoding="",
    )


def _lsp_query_positions(
    conn: sqlite3.Connection, rel: str
) -> tuple[list[tuple[int, int]], int]:
    """Every exact position a non-reusable file must (re-)ask, in order.

    That is each unresolved edge AND each position the file's existing
    bindings sit on: publishing replaces the file's evidence wholesale, so a
    bound position that was not re-asked would silently lose its binding.
    Resolved lexical edges are never asked, so enrichment can only ever add
    to what extraction already proved. An unresolved edge with no recorded
    line or column has no exact call-site coordinate: it is counted (the
    second value) and never asked at a guessed one.
    """

    positions: set[tuple[int, int]] = set()
    unpositioned = 0
    for row in conn.execute(
        "SELECT line, source_col FROM edges WHERE file_path=? "
        "AND dst_qualname IS NULL",
        (rel,),
    ):
        line = int(row["line"] or 0)
        column = row["source_col"]
        if line <= 0 or column is None or int(column) < 0:
            unpositioned += 1
            continue
        positions.add((line, int(column)))
    for binding in _lsp_stored_bindings(conn, rel):
        positions.add((binding.source_line, binding.source_column))
    return sorted(positions), unpositioned


def _lsp_terminal_name(name: str) -> str:
    """The last identifier of a dotted/scoped/path-like reference name."""

    parts = [part for part in re.split(r"[^\w$]+", str(name)) if part]
    return parts[-1] if parts else ""


def _lsp_position_name(
    conn: sqlite3.Connection, rel: str, line: int, column: int
) -> str | None:
    """The single reference name recorded at one caller position."""

    names = {
        str(row["dst_name"])
        for row in conn.execute(
            "SELECT dst_name FROM edges WHERE file_path=? AND line=? "
            "AND max(source_col, 0)=?",
            (rel, int(line), int(column)),
        )
    }
    return names.pop() if len(names) == 1 else None


def _lsp_char_units(char: str, encoding: str) -> int:
    """How many code units of the negotiated encoding one character takes."""

    if encoding == "utf-8":
        return len(char.encode("utf-8"))
    if encoding == "utf-32":
        return 1
    return 2 if ord(char) > 0xFFFF else 1


def _lsp_span_text(
    raw: bytes, target_range: tuple[int, ...], encoding: str
) -> str | None:
    """The declaration identifier a definition range covers, exactly.

    The range must cover one whole identifier on one line, and that token
    must be the first occurrence of its name on the line -- where a
    declaration's own name sits (``def name(``, ``class Name``, ``function
    name``, ``name = ...``). A parameter or local sharing both the line and
    the name, as in ``def thing(thing=None):``, therefore never stands in for
    the declaration; anything else fails closed.
    """

    if len(target_range) != 4:
        return None
    start_line, start_char, end_line, end_char = (int(v) for v in target_range)
    if start_line < 0 or start_line != end_line or not 0 <= start_char < end_char:
        return None
    lines = raw.split(b"\n")
    if start_line >= len(lines):
        return None
    try:
        text = lines[start_line].decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Map code-unit offsets onto character indexes; an offset that lands
    # inside one character names no identifier at all.
    boundaries: dict[int, int] = {}
    units = 0
    for index, char in enumerate(text):
        boundaries[units] = index
        units += _lsp_char_units(char, encoding)
    boundaries[units] = len(text)
    start = boundaries.get(start_char)
    end = boundaries.get(end_char)
    if start is None or end is None:
        return None
    identifier = text[start:end]
    if not re.fullmatch(r"[\w$]+", identifier):
        return None
    first = re.search(rf"(?<![\w$]){re.escape(identifier)}(?![\w$])", text)
    return identifier if first is not None and first.start() == start else None


def _lsp_canonical_declaration(
    conn: sqlite3.Connection, target_rel: str, line_start: int, name: str
) -> sqlite3.Row | None:
    """Exactly one canonical declaration named ``name`` starts on the line."""

    if line_start <= 0 or not name:
        return None
    rows = [
        row
        for row in conn.execute(
            "SELECT name, qualname, kind FROM entities "
            "WHERE file_path=? AND line_start=? AND name=? ORDER BY qualname",
            (target_rel, int(line_start), name),
        )
        if str(row["kind"]) not in LSP_NON_DECLARATION_KINDS
    ]
    return rows[0] if len(rows) == 1 else None


def _lsp_canonical_relpath(
    repo_root: Path, workspace_root: Path, uri: str
) -> str | None:
    """Map one definition URI back to a canonical repository-relative path.

    A bounded private workspace is what the server actually sees, so a real
    repo-internal definition comes back under the workspace root and never
    under the repository. Remap that case first; the workspace copy was
    written only from repository bytes whose hash matched the index, and the
    caller re-authenticates the repository file before trusting it.

    The repository branch stays lexical on purpose: resolving it would follow
    a symlinked target straight past the checks that exist to refuse one.
    """

    # Reuse the transport's own URI decoding rather than re-deriving it here.
    target = sglsp._uri_to_path(uri)
    if target is None or not target.is_absolute():
        return None
    lexical = Path(os.path.abspath(os.fspath(target)))
    roots = (
        Path(os.path.abspath(os.fspath(workspace_root))),
        Path(os.path.abspath(os.fspath(repo_root))),
    )
    for root in roots:
        try:
            relative = lexical.relative_to(root)
        except ValueError:
            continue
        rel = relative.as_posix()
        return rel if rel and rel != "." else None
    return None


def _lsp_verify_result(
    repo_root: Path,
    workspace_root: Path,
    conn: sqlite3.Connection,
    result: sglsp.LspDefinitionResult,
    encoding: str,
) -> tuple[_LspBinding | None, str]:
    """Verify one definition against the canonical index; name its outcome.

    Only an exact in-repo canonical declaration enriches an edge: the range
    must cover exactly that declaration's identifier in the authenticated
    target bytes, and the caller's reference name must name it. A parameter,
    a local or any other token on a declaration's line never stands in for
    the declaration. External, ambiguous, symlinked, missing, hash-shifted
    and failed answers each fail closed into their own health counter.
    """

    if getattr(result, "failure", "") or (
        result.classification == sglsp.SERVER_UNAVAILABLE
    ):
        return None, "unavailable"
    classification = str(result.classification)
    if classification in {sglsp.EXTERNAL_STDLIB, sglsp.EXTERNAL_DEPENDENCY}:
        return None, "external"
    if classification == sglsp.AMBIGUOUS:
        return None, "ambiguous"
    if classification != sglsp.REPO_INTERNAL:
        return None, "unresolved"
    target_range = result.target_range
    if not result.target_uri or not target_range:
        return None, "unresolved"
    target_rel = _lsp_canonical_relpath(
        repo_root, workspace_root, str(result.target_uri)
    )
    if target_rel is None:
        return None, "external"
    try:
        _validate_single_file_path(repo_root, target_rel)
        snapshot = _open_authenticated_regular_file_snapshot(
            repo_root, Path(target_rel)
        )
    except SourceGraphError:
        # Symlinked, excluded, escaping or non-regular targets never enrich.
        return None, "external"
    if _lsp_indexed_hash(conn, target_rel) != snapshot.source_hash:
        return None, "stale"
    span = tuple(int(value) for value in target_range)
    identifier = _lsp_span_text(snapshot.raw, span, encoding)
    line_start = span[0] + 1
    declaration = _lsp_canonical_declaration(
        conn, target_rel, line_start, identifier or ""
    )
    if declaration is None:
        return None, "ambiguous"
    caller_name = _lsp_position_name(
        conn, str(result.source_path), int(result.source_line),
        int(result.source_column),
    )
    if caller_name is None or (
        _lsp_terminal_name(caller_name) != str(declaration["name"])
    ):
        return None, "ambiguous"
    return _LspBinding(
        source_path=str(result.source_path),
        source_line=int(result.source_line),
        source_column=int(result.source_column),
        source_hash=str(result.source_hash),
        target_path=target_rel,
        target_hash=snapshot.source_hash,
        target_qualname=str(declaration["qualname"]),
        target_name=str(declaration["name"]),
        target_line_start=line_start,
        target_range=span,
        classification=classification,
        result_config_digest=str(result.config_digest),
    ), "internal"


def _lsp_binding_from_row(row: sqlite3.Row) -> _LspBinding:
    """Rebuild one durable binding from its provenance row."""

    try:
        target_range = tuple(int(value) for value in json.loads(str(row["target_range"])))
    except (TypeError, ValueError):
        target_range = ()
    return _LspBinding(
        source_path=str(row["source_path"]),
        source_line=int(row["source_line"]),
        source_column=int(row["source_column"]),
        source_hash=str(row["source_hash"]),
        target_path=str(row["target_path"]),
        target_hash=str(row["target_hash"]),
        target_qualname=str(row["target_qualname"]),
        target_name=str(row["target_name"]),
        target_line_start=int(row["target_line_start"]),
        target_range=target_range,
        classification=str(row["classification"]),
        result_config_digest=str(row["result_config_digest"]),
        prior_evidence_label=str(row["prior_evidence_label"]),
        prior_confidence=float(row["prior_confidence"]),
        # A read-only open of a table ``connect`` has not migrated yet reads
        # exactly what its rows are: legacy provenance with no identity.
        edge_identity=(
            str(row["edge_identity"] or "") if "edge_identity" in row.keys() else ""
        ),
    )


def _lsp_stored_bindings(
    conn: sqlite3.Connection, rel: str
) -> tuple[_LspBinding, ...]:
    if not _lsp_tables_present(conn):
        return ()
    return tuple(
        _lsp_binding_from_row(row)
        for row in conn.execute(
            "SELECT * FROM lsp_edge_provenance WHERE source_path=? "
            "ORDER BY source_line, source_column",
            (rel,),
        )
    )


def _lsp_stored_receipt(
    conn: sqlite3.Connection, rel: str
) -> dict[str, Any] | None:
    if not _lsp_tables_present(conn):
        return None
    row = conn.execute(
        "SELECT * FROM lsp_file_receipt WHERE source_path=?", (rel,)
    ).fetchone()
    return dict(row) if row is not None else None


def _lsp_binding_supported(
    conn: sqlite3.Connection, binding: _LspBinding
) -> bool:
    """Does this generation still carry the evidence the binding names?"""

    if _lsp_indexed_hash(conn, binding.target_path) != binding.target_hash:
        return False
    declaration = _lsp_canonical_declaration(
        conn, binding.target_path, binding.target_line_start, binding.target_name
    )
    return (
        declaration is not None
        and str(declaration["qualname"]) == binding.target_qualname
    )


# Every edge column ownership and restoration read, in one place.
_LSP_EDGE_COLUMNS = (
    "id, kind, src_qualname, dst_name, receiver_name, extractor, "
    "dst_qualname, evidence_label, confidence"
)


def _lsp_edge_identity(row: sqlite3.Row) -> str:
    """Name one edge at its position by exactly what extraction wrote.

    Resolvers and bindings only ever rewrite ``dst_qualname``,
    ``evidence_label`` and ``confidence``, and re-extracting identical bytes
    reproduces every other field. With the binding's position this therefore
    names the same edge across extraction, resolution and binding -- and
    never another edge on the same token, such as the ``references`` edge
    extraction records beside an attribute call's ``calls`` edge.
    """

    return json.dumps(
        [
            str(row["kind"]), str(row["src_qualname"]), str(row["dst_name"]),
            str(row["receiver_name"]), str(row["extractor"]),
        ],
        separators=(",", ":"),
    )


def _lsp_owned_edge(
    conn: sqlite3.Connection, binding: _LspBinding
) -> sqlite3.Row | None:
    """The one edge at the binding's position that the binding owns.

    A binding owns the single edge carrying its ``edge_identity``. Legacy
    provenance recorded only a position, so it owns an edge only where
    exactly one edge there names its target: while the caller's bytes are
    unchanged, that one is the edge it bound. Anything else -- a vanished or
    duplicated identity, or a legacy position shared by same-named siblings
    -- is ambiguous and owns nothing, and every caller fails closed on it
    rather than guess which edge the server's answer was written to.
    """

    rows = conn.execute(
        f"SELECT {_LSP_EDGE_COLUMNS} FROM edges "
        "WHERE file_path=? AND line=? AND max(source_col, 0)=?",
        (binding.source_path, binding.source_line, binding.source_column),
    ).fetchall()
    if binding.edge_identity:
        owned = [
            row for row in rows
            if _lsp_edge_identity(row) == binding.edge_identity
        ]
    else:
        owned = [
            row for row in rows
            if _lsp_terminal_name(str(row["dst_name"])) == binding.target_name
        ]
    return owned[0] if len(owned) == 1 else None


def _lsp_binding_carried(
    conn: sqlite3.Connection, binding: _LspBinding
) -> bool:
    """Does the edge this binding owns carry its target right now?

    Another edge on the same token that lexical resolution sent to the same
    target is evidence of its own: it never stands in for the binding's edge.
    """

    owned = _lsp_owned_edge(conn, binding)
    return owned is not None and owned["dst_qualname"] == binding.target_qualname


def _lsp_receipt_reusable(
    conn: sqlite3.Connection,
    receipt: dict[str, Any] | None,
    bindings: tuple[_LspBinding, ...],
    source_hash: str,
    spec: sglsp.LspServerSpec,
    digest: str,
) -> bool:
    """Is a stored receipt a complete, current answer for these exact inputs?"""

    if not receipt or int(receipt.get("complete", 0) or 0) != 1:
        return False
    return (
        str(receipt.get("source_hash", "")) == source_hash
        and str(receipt.get("server_command", "")) == json.dumps(list(spec.command))
        and str(receipt.get("server_version", "")) == spec.version
        and str(receipt.get("config_digest", "")) == digest
        and int(receipt.get("edge_count", -1)) == len(bindings)
        and all(
            binding.source_hash == source_hash
            and _lsp_binding_supported(conn, binding)
            and _lsp_binding_carried(conn, binding)
            for binding in bindings
        )
    )


def _lsp_restore_edge(conn: sqlite3.Connection, binding: _LspBinding) -> int:
    """Take one binding's server evidence off the one edge it owns.

    Only the owned edge is touched (``_lsp_owned_edge``): another edge on the
    same token keeps its destination, label and confidence whatever it
    resolved to, and ambiguous legacy provenance restores nothing rather than
    guess. The owned edge is found by identity, not by its destination: a
    lexical resolver that ran earlier in the same merge may already have
    cleared or re-resolved the destination this binding wrote. Still naming
    the binding's target, it returns to exactly what extraction left it (no
    destination, the recorded prior label and confidence). Cleared or
    re-resolved, it keeps that lexical destination -- a fresh lexical
    resolution is evidence of its own -- and sheds only the label and
    confidence the server's answer put there.
    """

    owned = _lsp_owned_edge(conn, binding)
    if owned is None:
        return 0
    prior = (
        binding.prior_evidence_label or sgast.AMBIGUOUS,
        binding.prior_confidence,
    )
    if owned["dst_qualname"] == binding.target_qualname:
        conn.execute(
            "UPDATE edges SET dst_qualname=NULL, evidence_label=?, confidence=? "
            "WHERE id=?",
            (*prior, int(owned["id"])),
        )
        return 1
    if (
        str(owned["evidence_label"]) == sgast.EXTRACTED
        and float(owned["confidence"]) == 1.0
    ):
        conn.execute(
            "UPDATE edges SET evidence_label=?, confidence=? WHERE id=?",
            (*prior, int(owned["id"])),
        )
        return 1
    return 0


def _lsp_unresolved_edge_at(
    conn: sqlite3.Connection, binding: _LspBinding
) -> sqlite3.Row | None:
    """The single unresolved edge at the position whose name names the target."""

    rows = conn.execute(
        f"SELECT {_LSP_EDGE_COLUMNS} FROM edges "
        "WHERE file_path=? AND line=? AND dst_qualname IS NULL "
        "AND max(source_col, 0)=?",
        (binding.source_path, binding.source_line, binding.source_column),
    ).fetchall()
    if len(rows) != 1:
        return None
    if _lsp_terminal_name(str(rows[0]["dst_name"])) != binding.target_name:
        return None
    return rows[0]


def _lsp_bind_edge(
    conn: sqlite3.Connection,
    binding: _LspBinding,
    spec: sglsp.LspServerSpec,
    digest: str,
    latency_ms: int,
) -> int:
    """Bind exactly one unresolved edge to a verified canonical declaration.

    Exactly one unresolved edge naming the target may sit at the reported
    position, so ``callers``/``impact`` read verified canonical targets and
    nothing else. Its identity is persisted with the binding and must be
    unique at the position, so every later writer carries, re-attaches or
    restores exactly this edge and never a sibling on the same token. The
    edge's lexical label and confidence are recorded before they are
    overwritten, so revocation restores exactly what extraction produced.
    """

    row = _lsp_unresolved_edge_at(conn, binding)
    if row is None:
        return 0
    binding = replace(binding, edge_identity=_lsp_edge_identity(row))
    if _lsp_owned_edge(conn, binding) is None:
        # Another edge at the position shares this identity, so no later
        # writer could tell which of them the answer was written to.
        return 0
    conn.execute(
        "UPDATE edges SET dst_qualname=?, evidence_label=?, confidence=1.0 "
        "WHERE id=?",
        (binding.target_qualname, sgast.EXTRACTED, int(row["id"])),
    )
    conn.execute(
        "INSERT OR REPLACE INTO lsp_edge_provenance("
        "source_path, source_line, source_column, source_hash, edge_identity, "
        "target_path, target_hash, target_qualname, target_name, "
        "target_line_start, target_range, prior_evidence_label, "
        "prior_confidence, server_command, server_version, config_digest, "
        "result_config_digest, classification, latency_ms, resolved_at, "
        "schema_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            binding.source_path,
            binding.source_line,
            binding.source_column,
            binding.source_hash,
            binding.edge_identity,
            binding.target_path,
            binding.target_hash,
            binding.target_qualname,
            binding.target_name,
            binding.target_line_start,
            json.dumps(list(binding.target_range)),
            str(row["evidence_label"]),
            float(row["confidence"]),
            json.dumps(list(spec.command)),
            spec.version,
            digest,
            binding.result_config_digest,
            binding.classification,
            int(latency_ms),
            _now_iso(),
            LSP_ENRICHMENT_SCHEMA_ID,
        ),
    )
    return 1


def _lsp_rebind_edge(conn: sqlite3.Connection, binding: _LspBinding) -> bool:
    """Re-attach one preserved binding to the freshly extracted edge it owns.

    Only the owned edge is re-attached, and only while it is unresolved: a
    sibling on the same token, unresolved or not, never inherits the binding.
    The provenance row is left exactly as the server's answer wrote it, so
    this only restores edge state re-extraction dropped and can never invent
    evidence no language server produced.
    """

    owned = _lsp_owned_edge(conn, binding)
    if owned is None or owned["dst_qualname"] is not None:
        return False
    conn.execute(
        "UPDATE edges SET dst_qualname=?, evidence_label=?, confidence=1.0 "
        "WHERE id=?",
        (binding.target_qualname, sgast.EXTRACTED, int(owned["id"])),
    )
    return True


def _lsp_delete_file_evidence(conn: sqlite3.Connection, rel: str) -> None:
    conn.execute("DELETE FROM lsp_edge_provenance WHERE source_path=?", (rel,))
    conn.execute("DELETE FROM lsp_file_receipt WHERE source_path=?", (rel,))


def _lsp_clear_file(conn: sqlite3.Connection, rel: str) -> tuple[int, bool]:
    """Revoke every binding this file owns, restore its edges, drop its receipt.

    Returns ``(bindings revoked, receipt existed)``.
    """

    if not _lsp_tables_present(conn):
        return 0, False
    bindings = _lsp_stored_bindings(conn, rel)
    had_receipt = _lsp_stored_receipt(conn, rel) is not None
    for binding in bindings:
        _lsp_restore_edge(conn, binding)
    _lsp_delete_file_evidence(conn, rel)
    return len(bindings), had_receipt


def _lsp_reconcile_source(
    conn: sqlite3.Connection, rel: str, counters: dict[str, int]
) -> None:
    """Make one caller file's evidence true of this generation again."""

    indexed = _lsp_indexed_hash(conn, rel)
    receipt = _lsp_stored_receipt(conn, rel)
    bindings = _lsp_stored_bindings(conn, rel)
    if (
        not indexed
        or (receipt is not None and str(receipt["source_hash"]) != indexed)
        or any(binding.source_hash != indexed for binding in bindings)
    ):
        # The bytes this evidence described are gone or changed, and the
        # merge already re-extracted (or deleted) the file's edges: there is
        # nothing to restore, only evidence to drop -- receipt AND provenance,
        # whether or not a receipt survived.
        _lsp_delete_file_evidence(conn, rel)
        counters["revoked"] += len(bindings)
        counters["files_revoked"] += int(receipt is not None)
        return
    kept = 0
    for binding in bindings:
        if _lsp_binding_supported(conn, binding) and (
            _lsp_binding_carried(conn, binding) or _lsp_rebind_edge(conn, binding)
        ):
            kept += 1
            continue
        _lsp_restore_edge(conn, binding)
        conn.execute(
            "DELETE FROM lsp_edge_provenance WHERE source_path=? "
            "AND source_line=? AND source_column=?",
            (binding.source_path, binding.source_line, binding.source_column),
        )
        counters["revoked"] += 1
    if receipt is None:
        if kept:
            # Provenance with no receipt is a state no writer may publish.
            revoked, _had = _lsp_clear_file(conn, rel)
            counters["revoked"] += revoked
        return
    if kept == len(bindings) and int(receipt.get("edge_count", -1)) == kept:
        return
    if kept == 0:
        conn.execute("DELETE FROM lsp_file_receipt WHERE source_path=?", (rel,))
        counters["files_revoked"] += 1
        return
    # Surviving bindings stay verified and carried, but the receipt no longer
    # describes a complete answer: keep it consistent and never reusable, so
    # the next batch re-asks every position of this file.
    conn.execute(
        "UPDATE lsp_file_receipt SET edge_count=?, complete=0 WHERE source_path=?",
        (kept, rel),
    )


def _lsp_reconcile_generation(conn: sqlite3.Connection) -> None:
    """Revoke/re-attach LSP evidence inside a merge, in plain SQL.

    Runs inside every writer that mutates the graph -- full build, single
    file index and removal -- whether or not a server is configured or the
    enrichment lease is free, so a changed or deleted source or target never
    leaves a binding naming bytes this generation does not index.

    A writer that re-runs lexical resolution calls it twice. First before
    the resolvers, while every surviving binding's edge still carries exactly
    what that binding wrote: a revoked edge returns to what extraction left
    it, so the resolvers see the edge they would have seen had no server ever
    answered, and whatever they resolve it to stands. Then after them, to
    re-attach supported bindings a resolver cleared and to revoke any it
    re-resolved elsewhere. Both calls are idempotent.
    """

    if not _lsp_tables_present(conn):
        return
    counters: dict[str, int] = dict.fromkeys(LSP_HEALTH_COUNTERS, 0)
    sources = sorted({
        str(row[0])
        for row in conn.execute(
            "SELECT source_path FROM lsp_edge_provenance "
            "UNION SELECT source_path FROM lsp_file_receipt"
        )
    })
    for rel in sources:
        _lsp_reconcile_source(conn, rel, counters)
    if any(counters.values()):
        _lsp_merge_health(conn, counters)


def _lsp_write_receipt(
    conn: sqlite3.Connection,
    rel: str,
    spec: sglsp.LspServerSpec,
    digest: str,
    edge_count: int,
    latency_ms: int,
    source_hash: str,
    complete: bool,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO lsp_file_receipt("
        "source_path, source_hash, language, server_command, server_version, "
        "config_digest, edge_count, complete, latency_ms, resolved_at, "
        "schema_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            rel,
            source_hash,
            _lsp_indexed_language(conn, rel),
            json.dumps(list(spec.command)),
            spec.version,
            digest,
            int(edge_count),
            1 if complete else 0,
            int(latency_ms),
            _now_iso(),
            LSP_ENRICHMENT_SCHEMA_ID,
        ),
    )


def _lsp_merge_health(
    conn: sqlite3.Connection, counters: dict[str, int]
) -> None:
    health = _lsp_meta_document(conn, LSP_HEALTH_META_KEY)
    for name in LSP_SUM_COUNTERS:
        health[name] = int(health.get(name, 0) or 0) + int(counters.get(name, 0))
    for name in LSP_MAX_COUNTERS:
        health[name] = max(
            int(health.get(name, 0) or 0), int(counters.get(name, 0))
        )
    health["schema_id"] = LSP_ENRICHMENT_SCHEMA_ID
    health["updated_at"] = _now_iso()
    _lsp_store_meta_document(conn, LSP_HEALTH_META_KEY, health)


def _lsp_resolve_batch(
    repo_root: Path,
    spec: sglsp.LspServerSpec,
    sources: tuple[sglsp.IndexedSource, ...],
    queries: tuple[sglsp.DefinitionQuery, ...],
) -> tuple[tuple[sglsp.LspDefinitionResult, ...], Path, int, str]:
    """Run the bounded transport against a private, disposable workspace.

    Returns the results, the workspace root the server answered from, the
    wall-clock cost of the batch and the negotiated position encoding.
    """

    workspace_root = (
        resolve_db_path(repo_root).parent / "lsp" / secrets.token_hex(8)
    )
    started = time.monotonic()
    try:
        workspace = sglsp.build_bounded_workspace(
            repo_root, sources, workspace_root, server_spec=spec,
        )
        outcome = sglsp.resolve_definitions(
            repo_root=repo_root,
            workspace=workspace,
            spec=spec,
            queries=queries,
            indexed_hashes={
                item.relative_path: item.source_hash for item in sources
            },
            request_timeout_s=LSP_REQUEST_TIMEOUT_S,
            batch_timeout_s=LSP_BATCH_TIMEOUT_S,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        return (
            tuple(outcome.results), workspace.root, latency_ms,
            str(outcome.position_encoding or sglsp.DEFAULT_POSITION_ENCODING),
        )
    finally:
        shutil.rmtree(workspace_root, ignore_errors=True)


def _lsp_plan_group(
    conn: sqlite3.Connection,
    candidates: tuple[tuple[str, str], ...],
    spec: sglsp.LspServerSpec,
    digest: str,
    counters: dict[str, int],
) -> tuple[list[_LspFilePlan], dict[str, _LspFileOutcome]]:
    """Decide per file: reuse the receipt, revoke it, or ask the server.

    Reused files never occupy the per-batch file cap. Files with no evidence
    yet are asked first; every position a cap leaves unasked is counted as
    ``skipped`` and its file never receives a reusable receipt.
    """

    plans: list[_LspFilePlan] = []
    outcomes: dict[str, _LspFileOutcome] = {}
    per_file = max(int(LSP_MAX_QUERIES_PER_FILE), 1)
    for rel, source_hash in candidates:
        receipt = _lsp_stored_receipt(conn, rel)
        stored = _lsp_stored_bindings(conn, rel)
        if _lsp_receipt_reusable(conn, receipt, stored, source_hash, spec, digest):
            # A no-op refresh keeps the receipt's bindings without spawning a
            # server: same bytes, same targets, same server, same workspace.
            counters["files_reused"] += 1
            counters["reused"] += len(stored)
            continue
        positions, unpositioned = _lsp_query_positions(conn, rel)
        counters["unpositioned"] += unpositioned
        if not positions:
            counters["files_skipped"] += 1
            if receipt is not None:
                # A receipt this generation can no longer honour, on a file
                # with nothing left to ask: revoke it and fail closed.
                outcomes[rel] = _LspFileOutcome(rel, source_hash, "clear")
            continue
        asked = positions[:per_file]
        counters["skipped"] += len(positions) - len(asked)
        plans.append(_LspFilePlan(
            rel,
            source_hash,
            tuple(
                sglsp.DefinitionQuery(
                    file_path=rel, source_hash=source_hash,
                    line=line, column=column,
                )
                for line, column in asked
            ),
            receipt is not None or bool(stored),
            len(asked) == len(positions),
        ))
    plans.sort(key=lambda plan: (plan.had_evidence, plan.rel))
    budget = max(int(LSP_MAX_FILES_PER_BATCH), 1)
    for plan in plans[budget:]:
        counters["files_deferred"] += 1
        counters["skipped"] += len(plan.queries)
        if plan.had_evidence:
            # Its evidence is not reusable and will not be re-asked this run.
            outcomes[plan.rel] = _LspFileOutcome(plan.rel, plan.source_hash, "clear")
    return plans[:budget], outcomes


def _lsp_apply_outcomes(
    conn: sqlite3.Connection,
    languages: tuple[str, ...],
    spec: sglsp.LspServerSpec,
    digest: str,
    outcomes: dict[str, _LspFileOutcome],
    counters: dict[str, int],
) -> None:
    """Re-verify every binding against the generation about to be published.

    The language server ran with no write lease held, so nothing decided
    before it is trusted here. The bounded workspace digest, each caller's
    indexed hash, and each target's hash and declaration are all rechecked
    inside this exclusive write. A batch computed against generation N can
    therefore never bind itself into generation N+1.
    """

    current = _lsp_config_digest(spec, _lsp_indexed_sources(conn, languages))
    workspace_moved = current != digest
    for rel in sorted(outcomes):
        outcome = outcomes[rel]
        if _lsp_indexed_hash(conn, rel) != outcome.source_hash:
            # Another writer re-indexed (and reconciled) this caller while
            # the server ran; its evidence is that writer's business now.
            counters["discarded"] += len(outcome.bindings)
            continue
        previous = {
            (binding.source_line, binding.source_column, binding.target_qualname)
            for binding in _lsp_stored_bindings(conn, rel)
        }
        revoked, had_receipt = _lsp_clear_file(conn, rel)
        if outcome.action == "clear" or workspace_moved:
            counters["revoked"] += revoked
            counters["files_revoked"] += int(had_receipt)
            counters["discarded"] += len(outcome.bindings)
            continue
        bound: set[tuple[int, int, str]] = set()
        for binding in outcome.bindings:
            if _lsp_binding_supported(conn, binding) and _lsp_bind_edge(
                conn, binding, spec, digest, outcome.latency_ms
            ):
                bound.add((
                    binding.source_line, binding.source_column,
                    binding.target_qualname,
                ))
            else:
                counters["discarded"] += 1
        counters["enriched"] += len(bound)
        counters["revoked"] += len(previous - bound)
        if not outcome.complete:
            counters["files_incomplete"] += 1
        if outcome.complete or bound:
            # An incomplete answer keeps what it verified, under a receipt
            # that can never be reused; a complete one is reusable.
            _lsp_write_receipt(
                conn, rel, spec, digest, len(bound), outcome.latency_ms,
                outcome.source_hash, outcome.complete,
            )
        elif had_receipt:
            counters["files_revoked"] += 1


@contextmanager
def _lsp_staged_write(repo_root: Path):
    """Yield one exclusive transaction on a staged copy; publish it on success.

    The lease is taken only here -- after the index published and released
    its own, and after every server has exited -- so no LSP subprocess ever
    runs inside a SQLite merge transaction while durable evidence still lands
    atomically. The canonical generation is never written in place: writers
    raw-copy it under this lease, so an in-place commit a crash tore would be
    cloned into the next generation. Another writer's short merge is waited
    out rather than failed into; a full build is not, and the pass it runs
    after publishing supersedes this one.
    """

    deadline = time.monotonic() + LSP_LEASE_WAIT_S
    while True:
        with index_write_lease(repo_root) as acquired:
            if acquired:
                canonical_path = resolve_db_path(repo_root)
                _cleanup_abandoned_staging(canonical_path)
                with _staged_generation(
                    canonical_path, copy_existing=True
                ) as staging_path:
                    conn = connect(staging_path)
                    try:
                        conn.execute("BEGIN EXCLUSIVE")
                        yield conn
                        _ensure_single_file_generation_metadata(conn)
                        conn.commit()
                    finally:
                        conn.close()
                    _publish_staged_generation(staging_path, canonical_path)
                return
        if time.monotonic() >= deadline:
            raise SourceGraphBuildInProgressError(
                f"source_graph_build_in_progress:{repo_root}"
            )
        time.sleep(LSP_LEASE_POLL_S)


def _lsp_commit_runs(repo_root: Path, runs: list[_LspGroupRun]) -> None:
    """Publish one pass's verified evidence and every group's denominators."""

    with _lsp_staged_write(repo_root) as conn:
        for run in runs:
            if run.outcomes:
                _lsp_apply_outcomes(
                    conn, run.languages, run.spec, run.digest, run.outcomes,
                    run.counters,
                )
            _lsp_merge_health(conn, run.counters)
        # Bindings decide what ``calls``/``impact`` answer, so the generation
        # identity query caches key on must move past this commit -- a cache
        # may not keep serving a binding it revoked.
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('single_file_last_mutation', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps({
                "finished_at": _now_iso(),
                "file_path": "",
                "operation": "lsp_enrich",
                "build_revision": BUILD_REVISION,
            }),),
        )


def _lsp_record_failure(repo_root: Path, reason: str) -> None:
    """Put a pass that raised on the durable record, so green cannot survive it.

    The one publication that carries no new evidence, and it happens only
    when a pass has already failed.
    """

    with _lsp_staged_write(repo_root) as conn:
        _lsp_merge_health(conn, {"failed_passes": 1})
        health = _lsp_meta_document(conn, LSP_HEALTH_META_KEY)
        health["last_failure"] = {"reason": reason, "at": _now_iso()}
        _lsp_store_meta_document(conn, LSP_HEALTH_META_KEY, health)


def _lsp_classify_batch(
    repo_root: Path,
    workspace_root: Path,
    conn: sqlite3.Connection,
    plans: list[_LspFilePlan],
    results: tuple[sglsp.LspDefinitionResult, ...],
    encoding: str,
    counters: dict[str, int],
) -> tuple[dict[str, list[_LspBinding]], set[str]]:
    """Classify exactly one outcome per asked position.

    A position with no result, or whose result carries a transport
    ``failure``, is ``unavailable`` and marks its file incomplete: a crash or
    timeout must never be cached as a server that answered "nothing".
    """

    by_position: dict[tuple[str, int, int], sglsp.LspDefinitionResult] = {}
    for result in results:
        key = (str(result.source_path), int(result.source_line), int(result.source_column))
        by_position.setdefault(key, result)
    verified: dict[str, list[_LspBinding]] = {}
    failed: set[str] = set()
    for plan in plans:
        for query in plan.queries:
            result = by_position.get((plan.rel, query.line, query.column))
            if result is None:
                binding, outcome = None, "unavailable"
            else:
                binding, outcome = _lsp_verify_result(
                    repo_root, workspace_root, conn, result, encoding
                )
            counters[outcome] += 1
            if outcome == "unavailable":
                failed.add(plan.rel)
            if binding is not None:
                verified.setdefault(plan.rel, []).append(binding)
    return verified, failed


def _lsp_enrich_group(
    repo_root: Path,
    group: tuple[str, str, tuple[str, ...]],
    spec: sglsp.LspServerSpec,
    restrict: frozenset[str] | None,
    label: str,
) -> _LspGroupRun:
    """Plan and resolve one server group's positions; publish nothing.

    Planning reads a published generation and the server runs with no lease
    held. What the group verified waits in the returned run for
    ``_lsp_commit_runs``, which re-verifies all of it inside its own lease.
    """

    _name, _env_name, languages = group
    run = _LspGroupRun(label, languages, spec)
    counters = run.counters
    db_path = resolve_db_path(repo_root)
    if not db_path.is_file():
        run.reason = "no_index"
        return run
    status = "skipped"
    reason = ""
    latency_ms = -1
    conn = connect(db_path, read_only=True)
    try:
        candidates = _lsp_group_files(conn, languages, restrict)
        if not candidates:
            run.reason = "no_files"
            return run
        sources = _lsp_indexed_sources(conn, languages)
        digest = _lsp_config_digest(spec, sources)
        plans, outcomes = _lsp_plan_group(
            conn, candidates, spec, digest, counters
        )
        queries = tuple(query for plan in plans for query in plan.queries)
        counters["files_attempted"] += len(plans)
        counters["attempted"] += len(queries)
        if plans and not _lsp_server_installed(spec.command):
            counters["unavailable"] += len(queries)
            status, reason = "unavailable", "server_missing"
            for plan in plans:
                outcomes[plan.rel] = _LspFileOutcome(plan.rel, plan.source_hash, "clear")
        elif plans:
            counters["batches"] += 1
            try:
                results, workspace_root, latency_ms, encoding = _lsp_resolve_batch(
                    repo_root, spec, sources, queries
                )
            except (sglsp.LspTransportError, OSError, ValueError):
                counters["unavailable"] += len(queries)
                status, reason = "unavailable", "transport"
                for plan in plans:
                    outcomes[plan.rel] = _LspFileOutcome(
                        plan.rel, plan.source_hash, "clear"
                    )
            else:
                counters["latency_ms_total"] += max(latency_ms, 0)
                counters["latency_ms_max"] = max(latency_ms, 0)
                verified, failed = _lsp_classify_batch(
                    repo_root, workspace_root, conn, plans, results, encoding,
                    counters,
                )
                status = "enriched"
                if counters["unavailable"] == len(queries):
                    status, reason = "unavailable", "transport"
                elif failed:
                    reason = "partial"
                for plan in plans:
                    outcomes[plan.rel] = _LspFileOutcome(
                        plan.rel,
                        plan.source_hash,
                        "publish",
                        tuple(verified.get(plan.rel, ())),
                        plan.complete and plan.rel not in failed,
                        latency_ms,
                    )
        if not plans and not counters["files_reused"]:
            status = "skipped"
            reason = reason or (
                "deferred" if counters["files_deferred"] else "no_unresolved"
            )
    finally:
        conn.close()
    run.digest, run.outcomes = digest, outcomes
    run.status, run.reason = status, reason
    run.files, run.latency_ms = len(candidates), latency_ms
    return run


def _lsp_aggregate_summary(
    summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fold per-group summaries into the one a full build reports."""

    merged = _lsp_summary("skipped", "")
    for name in (
        "attempted", "enriched", "reused", "revoked", "skipped", "unavailable",
        "files_deferred", "files",
    ):
        merged[name] = sum(int(item[name]) for item in summaries)
    merged["latency_ms"] = max(
        (int(item["latency_ms"]) for item in summaries), default=-1
    )
    statuses = {str(item["status"]) for item in summaries}
    for status in ("error", "unavailable", "enriched", "reused"):
        if status in statuses:
            merged["status"] = status
            break
    merged["languages"] = sorted(
        {str(item["language"]) for item in summaries if item["language"]}
    )
    return merged


def _lsp_enrich_paths(
    repo_root: Path,
    restrict: frozenset[str] | None,
    languages: tuple[str, ...],
) -> dict[str, Any]:
    """Enrich the requested languages' unresolved edges, one batch each.

    Every group is resolved before anything is published, and the pass then
    publishes at most one generation -- only when some group has evidence to
    change. A refresh that reused every receipt, or had nothing to ask, stays
    read-only: it never copies the canonical database to say so.
    """

    groups: list[tuple[str, str, tuple[str, ...]]] = []
    for language in languages:
        group = _lsp_group_for_language(language)
        if group is not None and group not in groups:
            groups.append(group)
    label = languages[0] if len(languages) == 1 else ""
    configured = [
        (group, spec)
        for group, spec in ((item, _lsp_server_spec(item)) for item in groups)
        if spec is not None
    ]
    if not configured:
        # C++ and every other language without a configured server keep their
        # lexical edges untouched and write nothing at all.
        return _lsp_summary(
            "skipped", label, reason="server" if groups else "language",
        )
    runs = [
        _lsp_enrich_group(repo_root, group, spec, restrict, label)
        for group, spec in configured
    ]
    if any(run.outcomes for run in runs):
        _lsp_commit_runs(repo_root, runs)
    summaries = [run.summary() for run in runs]
    if len(summaries) == 1:
        return summaries[0]
    return _lsp_aggregate_summary(summaries)


def _lsp_configured_languages() -> tuple[str, ...]:
    """Every language a configured server group can currently enrich."""

    return tuple(
        language
        for group in LSP_SERVER_GROUPS
        if _lsp_server_spec(group) is not None
        for language in group[2]
    )


def _lsp_enrich_after_publish(
    repo_root: Path,
    *,
    restrict: frozenset[str] | None,
    languages: tuple[str, ...],
) -> dict[str, Any]:
    """Enrich a published generation's unresolved edges, never raising.

    Indexing has already succeeded by the time this runs; a language server,
    a workspace or a receipt that misbehaves must degrade into typed health
    rather than fail the index that produced it. Degrading is not forgetting:
    a pass that raised is written into health before this returns, so the
    green an earlier pass earned can never outlive it.
    """

    if not languages:
        return _lsp_summary("skipped", "", reason="language")
    try:
        return _lsp_enrich_paths(repo_root, restrict, languages)
    except Exception as exc:  # enrichment never breaks a published index
        reason = type(exc).__name__
    try:
        _lsp_record_failure(repo_root, reason)
    except Exception as exc:  # recording the failure may not break it either
        reason = f"{reason}:failure_unrecorded:{type(exc).__name__}"
    return _lsp_summary("error", "", reason=reason)


def lsp_health(repo_root: Path) -> dict[str, Any]:
    """Typed LSP enrichment health with explicit denominators and latency.

    Edge-unit counters (``attempted`` and its classifications, ``skipped``,
    ``unpositioned``, ``enriched``, ``reused``, ``revoked``, ``discarded``)
    count edges; ``files_*`` counters count caller files. ``unpositioned``
    edges had no exact call-site coordinate and were never asked, so they are
    never part of ``attempted``, which always equals the sum of its
    classifications (``classified``). Zero attempted
    coverage is never green, and neither is coverage a cap left partial or a
    server that failed to answer. Nor is coverage that never landed: an
    ``internal`` answer is counted before commit, and a moved workspace, a
    concurrent writer or a later revocation can discard it, so green requires
    ``bound_edges`` -- bindings the published generation actually carries.
    Latency carries its own denominator (``batches``) so an average can never
    be mistaken for a measurement nobody took.

    The counters accumulate what enrichment published; a refresh that reused
    every receipt changed no evidence and publishes nothing. A pass that
    raised is on the record as ``failed_passes`` (with ``last_failure``) and,
    exactly like ``unavailable``, it keeps health non-green until a full
    rebuild starts a new record: a later pass that succeeds does not launder
    one that did not.
    """

    health: dict[str, Any] = {name: 0 for name in LSP_HEALTH_COUNTERS}
    stored: dict[str, Any] = {}
    bound_edges = 0
    db_path = resolve_db_path(repo_root)
    if db_path.is_file():
        conn = connect(db_path, read_only=True)
        try:
            stored = _lsp_meta_document(conn, LSP_HEALTH_META_KEY)
            if _lsp_tables_present(conn):
                bound_edges = int(conn.execute(
                    "SELECT COUNT(*) FROM lsp_edge_provenance"
                ).fetchone()[0])
        finally:
            conn.close()
        for name in LSP_HEALTH_COUNTERS:
            health[name] = int(stored.get(name, 0) or 0)
    batches = int(health["batches"])
    health["bound_edges"] = bound_edges
    health["classified"] = sum(
        int(health[name]) for name in LSP_EDGE_CLASSIFICATIONS
    )
    health["units"] = {
        "edges": list(LSP_EDGE_COUNTERS),
        "files": list(LSP_FILE_COUNTERS),
    }
    health["latency_ms_avg"] = (
        int(health["latency_ms_total"]) // batches if batches > 0 else -1
    )
    last_failure = stored.get("last_failure")
    health["last_failure"] = last_failure if isinstance(last_failure, dict) else {}
    health["schema_id"] = LSP_ENRICHMENT_SCHEMA_ID
    # Green measures committed, usable coverage: ``internal`` is counted
    # before commit, so it can never stand in for a binding that landed.
    health["green"] = bool(
        health["attempted"] > 0
        and bound_edges > 0
        and health["unavailable"] == 0
        and health["skipped"] == 0
        and health["files_deferred"] == 0
        and health["failed_passes"] == 0
    )
    return health


def lsp_provenance(
    repo_root: Path, *, source_path: str = ""
) -> tuple[dict[str, Any], ...]:
    """Durable per-edge provenance for every verified canonical binding."""

    db_path = resolve_db_path(repo_root)
    if not db_path.is_file():
        return ()
    conn = connect(db_path, read_only=True)
    try:
        if not _lsp_tables_present(conn):
            return ()
        if source_path:
            rows = conn.execute(
                "SELECT * FROM lsp_edge_provenance WHERE source_path=? "
                "ORDER BY source_line, source_column",
                (source_path,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM lsp_edge_provenance "
                "ORDER BY source_path, source_line, source_column"
            ).fetchall()
    finally:
        conn.close()
    return tuple(dict(row) for row in rows)


def lsp_receipt(repo_root: Path, source_path: str) -> dict[str, Any] | None:
    """One file's durable enrichment receipt, or ``None`` when it has none."""

    db_path = resolve_db_path(repo_root)
    if not db_path.is_file():
        return None
    conn = connect(db_path, read_only=True)
    try:
        return _lsp_stored_receipt(conn, source_path)
    finally:
        conn.close()


def index_file(repo_root: Path, path: str, expected_hash: str) -> dict[str, Any]:
    """Index exactly one file into the canonical Source Graph.

    The file must already exist on disk, must pass the bounded path-safety
    checks, must be an indexed extension, and its sha256 must match
    ``expected_hash``. Everything else fails closed. Only the target file's
    rows are mutated inside a single exclusive transaction; all other rows
    and the generation authority are preserved unchanged.

    Returns a summary dict with ``ok``, ``file_path``, ``source_hash``,
    ``language``, ``status``, ``entities``, ``edges``, and ``lsp``.
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

    # Fail closed on hash mismatch.
    if not isinstance(expected_hash, str) or not expected_hash:
        raise SourceGraphError(
            "source_graph_single_file_expected_hash_required"
        )
    snapshot = _open_authenticated_regular_file_snapshot(repo_root, Path(path))
    source_hash = snapshot.source_hash
    if source_hash != expected_hash:
        raise SourceGraphError(
            f"source_graph_single_file_hash_mismatch:"
            f"expected={expected_hash[:16]} actual={source_hash[:16]}"
        )

    # Extraction consumes exactly the authenticated bytes above.
    extraction = sgast.extract_file_from_bytes(
        repo_root,
        resolved,
        snapshot.raw,
        build_revision=BUILD_REVISION,
    )
    if extraction.file_path != rel:
        raise SourceGraphError(
            f"source_graph_single_file_path_mismatch:"
            f"expected={rel} returned={extraction.file_path}"
        )
    if extraction.source_hash != source_hash:
        raise SourceGraphError(
            f"source_graph_single_file_extraction_hash_mismatch:{rel}"
        )
    if extraction.status not in {"ok", "file_evidence_only"}:
        raise SourceGraphError(
            f"source_graph_single_file_extraction_failed:{rel}:{extraction.status}"
        )

    # Acquire the writer lease, mutate an isolated copy, then publish it.
    with index_write_lease(repo_root) as acquired:
        if not acquired:
            raise SourceGraphBuildInProgressError(
                f"source_graph_build_in_progress:{repo_root}"
            )
        canonical_path = resolve_db_path(repo_root)
        _cleanup_abandoned_staging(canonical_path)
        with _staged_generation(
            canonical_path,
            copy_existing=canonical_path.is_file(),
        ) as staging_path:
            conn = connect(staging_path)
            try:
                conn.execute("BEGIN EXCLUSIVE")
                python_file = extraction.language == "python"
                old_python_functions = (
                    {
                        (str(row["name"]), str(row["qualname"]))
                        for row in conn.execute(
                            "SELECT name, qualname FROM entities "
                            "WHERE file_path=? AND kind='function'",
                            (rel,),
                        )
                    }
                    if python_file
                    else set()
                )
                _invalidate_file(conn, rel)
                inserted_entities, inserted_edges, _dropped = _write_extraction(
                    conn,
                    extraction,
                    file_size=snapshot.file_size,
                    mtime_ns=snapshot.mtime_ns,
                )
                # Re-indexing this file also makes it a new *target*. Its own
                # receipt dies with its old bytes, and every caller binding this
                # generation no longer supports is revoked now, rather than
                # waiting for each caller to be re-indexed -- and before the
                # resolvers below may clear or re-resolve the destination it
                # wrote (see ``_lsp_reconcile_generation``). A receipt kept for
                # identical bytes describes edges just re-extracted as
                # unresolved, so they are re-attached here too.
                _lsp_reconcile_generation(conn)
                # Re-run cross-file edge resolution exactly as a full build's
                # tail does. Re-extracting one file drops its own edges back to
                # unresolved and can invalidate edges other files aimed at it; a
                # later incremental build sees changed==0 and skips resolution,
                # so leaving it unresolved here would strand those edges
                # permanently. Resolution is idempotent and generation-safe.
                _resolve_cpp_cross_file_edges(conn)
                if python_file:
                    new_python_functions = {
                        (entity.name, entity.qualname)
                        for entity in extraction.entities
                        if entity.kind == "function"
                    }
                    _resolve_python_imported_calls(
                        conn,
                        repo_root,
                        changed_files={rel},
                        affected_names={
                            name
                            for name, _ in old_python_functions.symmetric_difference(
                                new_python_functions
                            )
                        },
                    )
                    _resolve_python_imported_references(
                        conn, repo_root, changed_files={rel},
                        affected_names={
                            name
                            for name, _ in old_python_functions.symmetric_difference(
                                new_python_functions
                            )
                        },
                    )
                _resolve_javascript_import_bindings(conn)
                # A binding that still verifies but whose edge the resolvers
                # above just cleared is re-attached here: the post-publish pass
                # never runs when no server is configured. All of it is plain
                # SQL -- no language server runs inside this transaction.
                _lsp_reconcile_generation(conn)
                _ensure_single_file_generation_metadata(conn)
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
                conn.commit()
            finally:
                conn.close()
            _publish_staged_generation(staging_path, canonical_path)
    # Task 3 (LSP batch resolution): run enrichment only after the staged
    # generation has been published and the write lease released, so no LSP
    # subprocess is ever spawned inside the SQLite merge transaction. The
    # helper fails closed into typed health counters and never raises here.
    lsp_summary = _lsp_enrich_after_publish(
        repo_root,
        restrict=frozenset({rel}),
        languages=(extraction.language,),
    )
    return {
        "ok": True,
        "file_path": rel,
        "source_hash": source_hash,
        "language": extraction.language,
        "status": extraction.status,
        "entities": inserted_entities,
        "edges": inserted_edges,
        "lsp": lsp_summary,
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
        canonical_path = resolve_db_path(repo_root)
        _cleanup_abandoned_staging(canonical_path)
        with _staged_generation(
            canonical_path, copy_existing=canonical_path.is_file()
        ) as staging_path:
            conn = connect(staging_path)
            try:
                conn.execute("BEGIN EXCLUSIVE")
                # Count before we delete.
                entity_count = conn.execute(
                    "SELECT COUNT(*) FROM entities WHERE file_path=?", (rel,)
                ).fetchone()[0]
                _invalidate_file(conn, rel)
                # A deleted source revokes its own receipt and, in this same
                # generation, every caller binding that named it as a target.
                # Waiting for each caller to be re-indexed would leave those
                # callers pointing at a declaration that no longer exists.
                _lsp_reconcile_generation(conn)
                _ensure_single_file_generation_metadata(conn)
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
                conn.commit()
            finally:
                conn.close()
            _publish_staged_generation(staging_path, canonical_path)
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
    "LSP_ENRICHMENT_SCHEMA_ID",
    "LSP_HEALTH_META_KEY",
    "LSP_PROVENANCE_TABLE",
    "LSP_RECEIPT_TABLE",
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
    "lsp_health",
    "lsp_provenance",
    "lsp_receipt",
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
