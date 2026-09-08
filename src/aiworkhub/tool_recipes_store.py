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

This module persists manifests and the receipts of invocations something ELSE
ran; it never runs anything itself. ``tool_recipes`` deliberately contains no
execution engine, and neither durable storage nor a receipts table adds one:
nothing here spawns a subprocess, opens a socket, or renders an argv vector for
execution. ``recipe_runner`` is the one module that executes, and it hands the
receipt it measured to :func:`put_receipt`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import tool_recipes
from .sqlite_readonly import connect_readonly
from .tool_recipes import InvocationReceipt, Recipe, RecipeRegistry

SCHEMA_ID = "aiworkhub.tool_recipes_store.v1"
RECEIPT_SCHEMA_ID = "aiworkhub.tool_recipe_receipts.v1"
TOOL_RECIPES_DB_REL = (".aiworkhub", "tasking", "tool_recipes.sqlite")

# Bounded reads: never materialize an unbounded registry from disk.
MAX_LOAD_LIMIT = 1000
DEFAULT_LOAD_LIMIT = MAX_LOAD_LIMIT

# Receipts are runtime evidence, not content: many accumulate per manifest, so
# they carry their own tighter read bound.  Nothing prunes them here -- a read
# is bounded, the table is not truncated behind the caller's back.
MAX_RECEIPT_LOAD_LIMIT = 500
DEFAULT_RECEIPT_LOAD_LIMIT = 50

# Usage aggregation is grouped per manifest, so its bound is a number of
# RECIPES rather than a number of receipts: the whole point is to answer for
# every registered recipe at once, including the ones nobody has ever run.
#
# Deliberately the SAME ceiling as the manifest read. A lower one would silently
# understate ``used_count`` on a store whose used recipes outnumbered it -- the
# caller would compare a full registry against a truncated usage list and report
# recipes as unused because the aggregation stopped, not because nobody ran
# them. Two bounds on one join must agree, or the join reports the bound.
MAX_USAGE_LIMIT = MAX_LOAD_LIMIT
DEFAULT_USAGE_LIMIT = 200


class ToolRecipeStoreError(Exception):
    """Base error for the durable tool recipe store."""


class ToolRecipeStoreConflictError(ToolRecipeStoreError):
    """An immutable-identity or digest-rebinding write was rejected."""


class ToolRecipeStoreIntegrityError(ToolRecipeStoreError):
    """A stored manifest's digest does not match its recomputed digest."""


class ToolRecipeStoreUnsupportedError(ToolRecipeStoreError):
    """The engine cannot answer a query this store needs (missing JSON1)."""


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
CREATE TABLE IF NOT EXISTS tool_recipe_receipts (
    receipt_digest TEXT NOT NULL PRIMARY KEY,
    recipe_id TEXT NOT NULL,
    version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_recipe_receipts_created
    ON tool_recipe_receipts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_recipe_receipts_recipe
    ON tool_recipe_receipts(recipe_id, version);
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


# ---------------------------------------------------------------------------
# Invocation receipts.
#
# A manifest describes an invocation; a receipt records that one actually ran.
# Until something could persist a receipt the dashboard's ``receipts`` key
# never existed, so the panel's invocation/cache/context sections read
# ``no_sample`` STRUCTURALLY -- not because nothing had run, but because there
# was nowhere for the evidence to land.
#
# The same posture as the manifest table above: exact-identity immutability
# (the receipt digest is the primary key, and a receipt is content that is
# never edited in place), fail-closed reads (a payload whose recomputed digest
# does not match its stored digest is refused, not repaired), bounded reads,
# and read-only connections that never create the database.  Persisting a
# receipt still runs nothing: the caller that ran the invocation hands over
# what it measured.
# ---------------------------------------------------------------------------


def _serialize_receipt(receipt: InvocationReceipt) -> tuple[str, str, str, str]:
    """Return ``(receipt_digest, recipe_id, version, payload_json)``.

    The digest is recomputed from the canonical payload that will be stored,
    so the written bytes and the written digest can never disagree, and a
    receipt whose own ``digest`` field does not cover its content is refused
    before any write rather than persisted as evidence.
    """
    if not isinstance(receipt, InvocationReceipt):
        raise ToolRecipeStoreError("put_receipt requires a tool_recipes.InvocationReceipt")
    payload = tool_recipes.receipt_payload(receipt)
    digest = tool_recipes.receipt_digest(payload)
    if digest != receipt.digest:
        raise ToolRecipeStoreIntegrityError(
            "receipt digest does not cover its own canonical payload"
        )
    return digest, receipt.recipe_id, receipt.recipe_version, tool_recipes.canonical_json(payload)


def put_receipt(
    repo_root: str | Path,
    receipt: InvocationReceipt,
    *,
    _connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Persist one invocation receipt, keyed by its own digest.

    Idempotent by identity rather than by a check: the digest spans every
    field, so re-persisting the same receipt is the same row and is reported
    as ``stored=False`` instead of failing. Two genuinely different runs carry
    different timing and therefore different digests, so neither can overwrite
    the other.
    """
    digest, recipe_id, version, payload_json = _serialize_receipt(receipt)
    conn = _connection or _connect(repo_root)
    try:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT 1 FROM tool_recipe_receipts WHERE receipt_digest=?", (digest,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO tool_recipe_receipts "
                "(receipt_digest,recipe_id,version,payload_json,created_at) "
                "VALUES (?,?,?,?,?)",
                (digest, recipe_id, version, payload_json, _utcnow()),
            )
        conn.commit()
        return {
            "schema_id": RECEIPT_SCHEMA_ID,
            "receipt_digest": digest,
            "recipe_id": recipe_id,
            "version": version,
            "stored": existing is None,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if _connection is None:
            conn.close()


def _row_to_receipt_payload(row: sqlite3.Row) -> dict[str, Any]:
    """Return one persisted receipt payload, failing closed on tampering."""
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise ToolRecipeStoreIntegrityError("stored receipt payload is not an object")
    recomputed = tool_recipes.receipt_digest(payload)
    if recomputed != row["receipt_digest"]:
        raise ToolRecipeStoreIntegrityError(
            f"stored digest for receipt {row['receipt_digest']!r} does not match "
            "its recomputed payload digest"
        )
    payload["digest"] = str(row["receipt_digest"])
    payload["created_at"] = str(row["created_at"])
    return payload


def list_receipts(
    repo_root: str | Path,
    *,
    limit: int = DEFAULT_RECEIPT_LOAD_LIMIT,
    offset: int = 0,
    recipe_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return persisted receipts newest-first, bounded and fail-closed.

    Receipts come back as canonical mappings rather than reconstructed
    dataclasses: every consumer (the dashboard projection, the manager read
    surface) wants JSON-safe evidence, and rebuilding a frozen dataclass only
    to project it back to a mapping would hash the same bytes twice.

    An absent, unreadable or not-yet-migrated database yields ``[]`` -- a
    repository that has run no recipe is a legitimate state. A row that IS
    present but whose digest does not cover its payload raises instead.
    """
    path = _db_path(repo_root)
    if not path.exists():
        return []
    bounded = max(1, min(int(limit), MAX_RECEIPT_LOAD_LIMIT))
    try:
        conn = connect_readonly(path)
    except (sqlite3.Error, OSError, ValueError):
        return []
    conn.row_factory = sqlite3.Row
    try:
        if recipe_id is None:
            rows = conn.execute(
                "SELECT * FROM tool_recipe_receipts "
                "ORDER BY created_at DESC, receipt_digest ASC LIMIT ? OFFSET ?",
                (bounded, max(0, int(offset))),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tool_recipe_receipts WHERE recipe_id=? "
                "ORDER BY created_at DESC, receipt_digest ASC LIMIT ? OFFSET ?",
                (str(recipe_id), bounded, max(0, int(offset))),
            ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [_row_to_receipt_payload(row) for row in rows]


def receipt_count(repo_root: str | Path) -> int:
    """Return how many receipts are stored, or ``0`` when there is no store.

    Counting is separate from listing because the count is the whole answer
    for a caller that only needs to know whether evidence exists, and it must
    not be inferred from the length of a BOUNDED list -- that is exactly the
    "returned 8, therefore 8" mistake the dashboard's own count fields exist
    to avoid.
    """
    path = _db_path(repo_root)
    if not path.exists():
        return 0
    try:
        conn = connect_readonly(path)
    except (sqlite3.Error, OSError, ValueError):
        return 0
    try:
        row = conn.execute("SELECT COUNT(*) FROM tool_recipe_receipts").fetchone()
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
    return 0 if row is None else int(row[0])


# ---------------------------------------------------------------------------
# Usage aggregation.
#
# ``receipt_count`` answers "how many runs exist". That is the question the
# panel could already answer, and it is not the question the owner asked: a
# registry of 22 recipes reporting "Measured" was reporting that ROWS existed,
# and every one of the 7 receipts behind that word had been produced by a
# verification agent proving the plumbing worked. Nothing had been USED.
#
# Answering "used" needs three things the count cannot supply: per recipe,
# WHO ran it and how many distinct actors that is; WHEN it was first and last
# run; and what it cost. All three already live inside ``payload_json`` -- the
# actor because ``tool_recipes.ActorIdentity`` is now part of the receipt -- so
# this reads them with SQLite's own JSON1 operators and aggregates IN THE
# ENGINE rather than materializing every receipt in Python to fold it. The
# alternative was an unbounded read of the whole receipts table on every
# dashboard refresh, which is the shape this module exists to avoid.
#
# Nothing here is denormalized into new columns, so an older database needs no
# migration and no backfill: a receipt written before the actor existed simply
# has no ``$.actor`` to extract, ``json_extract`` returns NULL, and it counts
# as an unattributed run -- which is exactly what it was.
# ---------------------------------------------------------------------------

# The actor key a distinct-actor count groups on. ``NULLIF(..., '')`` collapses
# the two ways a run can be unattributable -- a receipt predating the actor
# field (NULL) and one whose route could not be verified ('') -- into SQL NULL,
# which COUNT(DISTINCT) excludes. Without it, "unattributed" would itself count
# as an actor and every never-used recipe would report one user.
_ACTOR_KEY_SQL = "NULLIF(json_extract(payload_json,'$.actor.key'),'')"

# Grouped by ``(recipe_id, version)``, which is the store's own primary key for
# a manifest and the identity a receipt records. Grouping by ``recipe_id`` alone
# would fold two registered versions into one row, and a caller joining that
# row back onto the registry -- which lists one entry PER VERSION -- would count
# one used recipe twice. That is not hypothetical here: the operator recipes
# exist at 1.0.0 and 2.0.0 precisely because their argv changed, and the point
# of the split is to see that the old version stopped being used.
_USAGE_SQL = f"""
SELECT recipe_id,
       version,
       COUNT(*) AS runs,
       COUNT(DISTINCT {_ACTOR_KEY_SQL}) AS distinct_actors,
       SUM(CASE WHEN {_ACTOR_KEY_SQL} IS NULL THEN 1 ELSE 0 END)
           AS unattributed_runs,
       MIN(created_at) AS first_run,
       MAX(created_at) AS last_run,
       SUM(COALESCE(json_extract(payload_json,'$.returned_digest_bytes'),0))
           AS returned_digest_bytes
FROM tool_recipe_receipts
{{where}}
GROUP BY recipe_id, version
ORDER BY runs DESC, recipe_id ASC, version ASC
LIMIT ?
"""

_EXIT_SQL = """
SELECT recipe_id,
       version,
       COALESCE(json_extract(payload_json,'$.exit.status'),'') AS status,
       json_extract(payload_json,'$.exit.exit_code') AS exit_code,
       COUNT(*) AS runs
FROM tool_recipe_receipts
{where}
GROUP BY recipe_id, version, status, exit_code
ORDER BY recipe_id ASC, version ASC, runs DESC
"""


def _exit_label(status: str, exit_code: Any) -> str:
    """Name one cell of the exit distribution.

    ``completed:0`` and ``completed:1`` are different outcomes and must not
    fold together -- "the tool ran and said no" is the evidence a recipe exists
    to produce. A status with no code (a spawn failure, a timeout) has no
    second half to report, so it is named by its status alone rather than by a
    fabricated code.
    """
    label = status or "unknown"
    if exit_code is None:
        return label
    return f"{label}:{int(exit_code)}"


def usage_by_recipe(
    repo_root: str | Path,
    *,
    limit: int = DEFAULT_USAGE_LIMIT,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Return per-recipe run evidence, newest-activity-heaviest first.

    One row per ``(recipe_id, version)`` that has AT LEAST ONE receipt, which is
    the store's own manifest identity: a caller joining these rows back onto
    ``list_recipes`` -- which lists one entry per version -- gets an exact
    match. Versions with none are
    deliberately absent here rather than emitted as zero rows: this function
    reads the receipts table and knows nothing about what is registered, and
    inventing a row for a recipe it cannot see would be this module claiming a
    fact about a table it did not read. The caller that holds both -- the
    registry and this -- is the one that can honestly report "registered but
    never run", and :func:`aiworkhub.manager_recipe_tools.usage` does exactly
    that.

    ``since`` filters on ``created_at``, which is the stored ISO-8601 UTC
    timestamp, so a lexicographic comparison is a chronological one.

    An absent or unreadable database yields ``[]`` -- a repository that has run
    no recipe is a legitimate state. An engine that cannot run the query is
    NOT: it raises :class:`ToolRecipeStoreUnsupportedError`, so a missing JSON1
    surfaces as a broken surface rather than as a silent "nothing was used".
    """
    path = _db_path(repo_root)
    if not path.exists():
        return []
    bounded = max(1, min(int(limit), MAX_USAGE_LIMIT))
    try:
        conn = connect_readonly(path)
    except (sqlite3.Error, OSError, ValueError):
        return []
    conn.row_factory = sqlite3.Row
    where = ""
    params: list[Any] = []
    if since is not None and str(since).strip():
        where = "WHERE created_at >= ?"
        params.append(str(since).strip())
    try:
        rows = conn.execute(
            _USAGE_SQL.format(where=where), (*params, bounded)
        ).fetchall()
        exits = conn.execute(_EXIT_SQL.format(where=where), tuple(params)).fetchall()
    except sqlite3.Error as exc:
        raise ToolRecipeStoreUnsupportedError(
            f"recipe usage aggregation is unavailable: {exc}"
        ) from exc
    finally:
        conn.close()
    wanted = {(str(row["recipe_id"]), str(row["version"])) for row in rows}
    distribution: dict[tuple[str, str], dict[str, int]] = {}
    for row in exits:
        key = (str(row["recipe_id"]), str(row["version"]))
        if key not in wanted:
            continue
        label = _exit_label(str(row["status"]), row["exit_code"])
        bucket = distribution.setdefault(key, {})
        bucket[label] = bucket.get(label, 0) + int(row["runs"])
    usage: list[dict[str, Any]] = []
    for row in rows:
        recipe_id = str(row["recipe_id"])
        version = str(row["version"])
        runs = int(row["runs"])
        unattributed = int(row["unattributed_runs"] or 0)
        usage.append(
            {
                "recipe_id": recipe_id,
                "version": version,
                "runs": runs,
                "distinct_actors": int(row["distinct_actors"] or 0),
                "attributed_runs": runs - unattributed,
                "unattributed_runs": unattributed,
                "first_run": str(row["first_run"] or ""),
                "last_run": str(row["last_run"] or ""),
                "returned_digest_bytes": int(row["returned_digest_bytes"] or 0),
                "exit_distribution": dict(
                    sorted(distribution.get((recipe_id, version), {}).items())
                ),
            }
        )
    return usage


def actor_totals(
    repo_root: str | Path, *, since: str | None = None
) -> dict[str, Any]:
    """Return the repository-wide actor totals behind :func:`usage_by_recipe`.

    Separate from the per-recipe rows because it is a different denominator:
    two recipes each run by the same one manager session is ONE distinct actor
    overall and one apiece, and summing the per-recipe column would report two.
    A bounded per-recipe list also cannot be summed into a repository total
    without restating its own bound as a measurement.
    """
    path = _db_path(repo_root)
    empty = {"runs": 0, "distinct_actors": 0, "attributed_runs": 0, "unattributed_runs": 0}
    if not path.exists():
        return empty
    try:
        conn = connect_readonly(path)
    except (sqlite3.Error, OSError, ValueError):
        return empty
    where = ""
    params: tuple[Any, ...] = ()
    if since is not None and str(since).strip():
        where = "WHERE created_at >= ?"
        params = (str(since).strip(),)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS runs, "
            f"COUNT(DISTINCT {_ACTOR_KEY_SQL}) AS distinct_actors, "
            f"SUM(CASE WHEN {_ACTOR_KEY_SQL} IS NULL THEN 1 ELSE 0 END) AS unattributed "
            f"FROM tool_recipe_receipts {where}",
            params,
        ).fetchone()
    except sqlite3.Error as exc:
        raise ToolRecipeStoreUnsupportedError(
            f"recipe actor totals are unavailable: {exc}"
        ) from exc
    finally:
        conn.close()
    if row is None:
        return empty
    runs = int(row[0] or 0)
    unattributed = int(row[2] or 0)
    return {
        "runs": runs,
        "distinct_actors": int(row[1] or 0),
        "attributed_runs": runs - unattributed,
        "unattributed_runs": unattributed,
    }
