"""Durable, repository-native persistence for tool recipe manifests.

``tool_recipes`` owns a complete, tested description and validation layer --
typed manifests, fail-closed parameter validation, deterministic argv
rendering, discovery, cache eligibility and invocation receipts -- but it had
no way to keep a manifest between two calls. Nothing could register a recipe,
so the dashboard's Tool Recipes panel constructed ``RecipeRegistry(())`` on
every refresh and then reported that the registry was empty: the emptiness was
an artifact of the caller, not a fact about the repository.

This module is that missing durable layer. It follows the same
repository-native SQLite shape as ``skill_registry_store``, which closed
exactly this gap for skills, and deliberately does not invent a second storage
idiom:

* Canonical, additive schema: :func:`ensure_schema` only ever issues
  ``CREATE TABLE IF NOT EXISTS`` / ``CREATE ... INDEX IF NOT EXISTS``, so
  opening an older database never rewrites or drops an existing row.
* Exact-identity, immutable rows: ``(recipe_id, version)`` is the primary key,
  so a version once written can never be overwritten; two versions of one
  recipe both persist side by side.
* A digest that can never be rebound: the manifest digest carries a ``UNIQUE``
  index, so the same digest can never be bound to a second id/version.
* Fail-closed reads: a stored manifest whose recomputed digest does not match
  its persisted digest is rejected on read and never silently repaired.
* Bounded reads: a load can never materialize an unbounded registry.
* Reads route through :func:`sqlite_readonly.connect_readonly` (percent-encoded
  ``mode=ro`` URI plus ``PRAGMA query_only=ON``) and never create the database.

One digest, not two. ``skill_registry_store`` needs a second ``state_digest``
because a skill record carries runtime authorization state (evidence,
lifecycle, counters) that ``skill_digest`` does not cover, so a tamper confined
to that state would go unseen. A :class:`~aiworkhub.tool_recipes.Recipe` has no
runtime state at all -- it is entirely content, and ``recipe_digest`` hashes
every persisted field -- so one digest already spans the whole payload and a
second would hash the same bytes twice. As there, the digest lives in the SAME
row as the payload it covers and is unkeyed, so it is DETECTION, not
authentication: it catches accidental corruption, a truncated or partial write
and naive hand-editing of a single column, and it does NOT resist an adversary
who can already write the row -- such a writer recomputes the digest and both
checks pass. No keyed MAC is added, because the key would have to live in the
same repository as the data.

Manifests are persisted through the library's own canonical form
(``tool_recipes.recipe_payload`` / ``tool_recipes.recipe_from_mapping``), so a
manifest read off disk is validated exactly as strictly as one written in
Python, and round-trips with its digest unchanged.

This module persists manifests; it never runs anything. ``tool_recipes``
deliberately contains no execution engine, and giving it durable storage does
not add one: nothing here spawns a subprocess, opens a socket, or renders an
argv vector for execution.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import tool_recipes
from .sqlite_readonly import connect_readonly
from .tool_recipes import Recipe, RecipeRegistry

SCHEMA_ID = "aiworkhub.tool_recipes_store.v1"
TOOL_RECIPES_DB_REL = (".aiworkhub", "tasking", "tool_recipes.sqlite")

# Bounded reads: never materialize an unbounded registry from disk.
MAX_LOAD_LIMIT = 1000
DEFAULT_LOAD_LIMIT = MAX_LOAD_LIMIT


class ToolRecipeStoreError(Exception):
    """Base error for the durable tool recipe store."""


class ToolRecipeStoreConflictError(ToolRecipeStoreError):
    """An immutable-identity or digest-rebinding write was rejected."""


class ToolRecipeStoreIntegrityError(ToolRecipeStoreError):
    """A stored manifest's digest does not match its recomputed digest."""


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tool_recipes (
    recipe_id TEXT NOT NULL,
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (recipe_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_recipes_digest
    ON tool_recipes(digest);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path(repo_root: str | Path) -> Path:
    return Path(repo_root).joinpath(*TOOL_RECIPES_DB_REL)


def _connect(repo_root: str | Path) -> sqlite3.Connection:
    """Open the canonical recipe database read-write, creating it if absent."""
    path = _db_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the tool_recipes table and digest index if they do not exist.

    Additive only: existing rows are never rewritten, so an older database is
    upgraded in place without data loss.
    """
    conn.executescript(_SCHEMA_SQL)


def initialize_repository(repo_root: str | Path) -> dict[str, Any]:
    """Idempotently ensure the canonical recipe store exists for ``repo_root``."""
    conn = _connect(repo_root)
    try:
        ensure_schema(conn)
        count = int(conn.execute("SELECT COUNT(*) FROM tool_recipes").fetchone()[0])
        return {
            "schema_id": SCHEMA_ID,
            "initialized": True,
            "db_path": str(_db_path(repo_root)),
            "existing_count": count,
        }
    finally:
        conn.close()


def _canonical_payload_json(recipe: Recipe) -> str:
    """Return the canonical, sorted JSON payload bytes for one manifest.

    This is the exact serialization :func:`tool_recipes.recipe_digest` hashes,
    so the stored bytes and the stored digest can never disagree about which
    canonical form they describe.
    """
    return tool_recipes.canonical_json(tool_recipes.recipe_payload(recipe))


def _serialize(recipe: Recipe) -> tuple[str, str, str, str]:
    """Return ``(recipe_id, version, digest, payload_json)``.

    The digest is computed from the manifest as it will be reconstructed from
    the persisted JSON, so the write-time digest and every later read-time
    digest derive from the exact same canonical bytes.
    """
    if not isinstance(recipe, Recipe):
        raise ToolRecipeStoreError("put_recipe requires a tool_recipes.Recipe")
    payload_json = _canonical_payload_json(recipe)
    reconstructed = tool_recipes.recipe_from_mapping(json.loads(payload_json))
    return (
        reconstructed.id,
        reconstructed.version,
        tool_recipes.recipe_digest(reconstructed),
        payload_json,
    )


def _row_to_recipe(row: sqlite3.Row) -> Recipe:
    """Reconstruct and verify one persisted row, failing closed on tampering.

    Reconstruction runs the ordinary manifest constructors, so a payload that
    would produce an unsafe literal, a non-literal executable or an unknown
    argv slot raises :class:`~aiworkhub.tool_recipes.RecipeError` rather than
    yielding a Recipe. The digest check then rejects a payload that was edited
    without also rewriting the digest that spans it.
    """
    recipe = tool_recipes.recipe_from_mapping(json.loads(row["payload_json"]))
    recomputed = tool_recipes.recipe_digest(recipe)
    if recomputed != row["digest"]:
        raise ToolRecipeStoreIntegrityError(
            f"stored digest for {row['recipe_id']!r}@{row['version']!r} does not "
            "match its recomputed manifest digest"
        )
    return recipe


def put_recipe(
    repo_root: str | Path,
    recipe: Recipe,
    *,
    _connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Persist one validated manifest with exact-identity immutability.

    Rejects a second write to the same ``(id, version)`` and rejects binding an
    already-stored manifest digest to a different id/version.
    """
    recipe_id, version, digest, payload_json = _serialize(recipe)
    conn = _connection or _connect(repo_root)
    try:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM tool_recipes WHERE recipe_id=? AND version=?",
            (recipe_id, version),
        ).fetchone():
            raise ToolRecipeStoreConflictError(
                f"recipe {recipe_id!r}@{version!r} is already stored and immutable"
            )
        owner = conn.execute(
            "SELECT recipe_id,version FROM tool_recipes WHERE digest=?",
            (digest,),
        ).fetchone()
        if owner is not None:
            raise ToolRecipeStoreConflictError(
                f"digest {digest} is already bound to "
                f"{owner['recipe_id']!r}@{owner['version']!r} and cannot be rebound"
            )
        conn.execute(
            "INSERT INTO tool_recipes "
            "(recipe_id,version,digest,payload_json,created_at) "
            "VALUES (?,?,?,?,?)",
            (recipe_id, version, digest, payload_json, _utcnow()),
        )
        conn.commit()
        return {
            "schema_id": SCHEMA_ID,
            "recipe_id": recipe_id,
            "version": version,
            "digest": digest,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if _connection is None:
            conn.close()


def get_recipe(
    repo_root: str | Path, recipe_id: str, version: str
) -> Recipe | None:
    """Return one persisted manifest, or ``None`` when it is absent.

    Fails closed with :class:`ToolRecipeStoreIntegrityError` if the stored
    digest does not match the recomputed manifest digest. Never creates the
    database.
    """
    path = _db_path(repo_root)
    if not path.exists():
        return None
    try:
        conn = connect_readonly(path)
    except (sqlite3.Error, OSError, ValueError):
        return None
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM tool_recipes WHERE recipe_id=? AND version=?",
            (recipe_id, version),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return _row_to_recipe(row)


def list_recipes(
    repo_root: str | Path, *, limit: int = DEFAULT_LOAD_LIMIT, offset: int = 0
) -> list[Recipe]:
    """Return persisted manifests, bounded and fail-closed. Never creates the DB.

    An absent database, a file that is not a database, or a query that the
    engine refuses yields ``[]`` -- a repository that has registered no recipes
    is a legitimate state, not an error. A row that IS present but whose digest
    does not cover its payload is a different thing entirely, and
    :func:`_row_to_recipe` raises rather than serving it.
    """
    path = _db_path(repo_root)
    if not path.exists():
        return []
    bounded = max(1, min(int(limit), MAX_LOAD_LIMIT))
    # ``connect_readonly`` issues ``PRAGMA query_only=ON`` on the new
    # connection, which touches the file, so a corrupt or non-database file can
    # fail here rather than at the first SELECT. Both are the same fact -- there
    # is nothing readable at this path -- so both degrade to an empty result.
    try:
        conn = connect_readonly(path)
    except (sqlite3.Error, OSError, ValueError):
        return []
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM tool_recipes ORDER BY recipe_id ASC, version ASC "
            "LIMIT ? OFFSET ?",
            (bounded, max(0, int(offset))),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [_row_to_recipe(row) for row in rows]


def stored_digest(
    repo_root: str | Path, recipe_id: str, version: str
) -> str | None:
    """Return the digest persisted for one manifest, without verifying it.

    Read-only and non-verifying by design: this is the raw stored column, for
    an audit caller that needs to SEE a mismatch rather than be stopped by one.
    Runtime readers use :func:`get_recipe` or :func:`list_recipes`, which fail
    closed.
    """
    path = _db_path(repo_root)
    if not path.exists():
        return None
    try:
        conn = connect_readonly(path)
    except (sqlite3.Error, OSError, ValueError):
        return None
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT digest FROM tool_recipes WHERE recipe_id=? AND version=?",
            (recipe_id, version),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return None if row is None else str(row["digest"])


def load_registry(
    repo_root: str | Path, *, limit: int = DEFAULT_LOAD_LIMIT
) -> RecipeRegistry:
    """Load persisted manifests into a :class:`RecipeRegistry`.

    An absent or unreadable store yields an EMPTY registry rather than an
    error, so the dashboard never fails on a repository that has registered no
    recipes -- and, unlike a registry the caller constructs empty by hand, that
    emptiness is now a measured fact about the store.

    A stored manifest whose digest is tampered still fails closed (via
    :func:`list_recipes` -> :func:`_row_to_recipe`): it is not dropped from the
    registry, it aborts the whole load. Callers that must not fail -- the
    dashboard among them -- catch that and degrade explicitly, so a corrupt
    row is never silently served as if it were absent.
    """
    return RecipeRegistry(list_recipes(repo_root, limit=limit))
