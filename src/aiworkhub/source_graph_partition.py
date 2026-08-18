"""Partitioned indexing and composed views for the canonical Source Graph.

Owner mandate (card NF-302-SG-PARTITION): a reviewer must get a
candidate-accurate index WITHOUT the old full-index clone. Cloning the entire
canonical database with the SQLite backup API and re-indexing only the changed
files made preparation cost proportional to repository size -- a dead end, not
an inefficiency. This module replaces that clone with two composable pieces:

  * A **partition** is an index over a bounded, explicitly declared scope -- a
    set of files -- built and stored independently of the base index. Building
    a partition costs time and bytes proportional to that scope ALONE. It never
    reads, copies, or writes the base index. :func:`build_partition` reuses the
    exact-file :func:`aiworkhub.source_graph.index_file` /
    :func:`aiworkhub.source_graph.remove_file` primitives against a fresh,
    empty database bound context-locally through
    ``source_graph.database_path_override``.

  * A **composed view** binds one base index (opened read-only) plus one or
    more partitions and answers queries as if it were a single index.
    Precedence is explicit and enforced by ``TEMP`` views installed over the
    live connection:

      - a file present in a partition resolves from the partition; the base
        rows for that file are invisible (partition wins per file);
      - a file absent from every partition resolves from the base;
      - a file declared in a partition's scope but deleted resolves to nothing
        (excluded from both sides).

    The declared scope -- every changed path, including deletions that leave no
    partition row -- is what hides the base rows, so a delete is precedence-
    correct even though it stores no row.

Cross-boundary edge resolution (:func:`resolve_cross_boundary`) links the two
sides in BOTH directions and NAMES the cases it cannot resolve rather than
guessing: see that function's docstring for the exact resolved/unresolved
contract.

Immutability and identity: a :class:`ComposedView` is a frozen value bound to
``packet_sha256`` / ``target_request_id`` / ``target_task_id`` and to a base
fingerprint (size + mtime). :meth:`ComposedView.verify_binding` refuses a view
whose identity does not match a caller's packet, and every open re-checks that
the base has not shifted underneath the view (:class:`PartitionBaseShiftError`).
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from . import source_graph as sg

COMPOSED_VIEW_META_KEY = "composed_view"
COMPOSED_VIEW_SCHEMA_ID = "aiworkhub.source_graph.composed_view.v1"
_SCOPE_TABLE = "_partition_scope"
_COMPOSED_TABLES: tuple[str, ...] = ("files", "entities", "edges")


class PartitionError(RuntimeError):
    """Base class for partition / composed-view failures."""


class PartitionBindingError(PartitionError):
    """A composed view was asked to serve a packet it is not bound to."""


class PartitionBaseShiftError(PartitionError):
    """The base index changed underneath an open (or opening) composed view."""


# ---------------------------------------------------------------------------
# Partition build -- scope-only, never touches the base index
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PartitionBuildReport:
    """What one :func:`build_partition` produced -- the measurable deliverable.

    ``bytes_written`` and ``wall_seconds`` are the cost of preparing the
    partition; both scale with ``scope`` (the changed files), never with the
    base index size, because the base is never read here.
    """

    partition_db_path: str
    scope: tuple[str, ...]
    files_indexed: int
    files_deleted: int
    files_skipped: int
    entities: int
    edges: int
    bytes_written: int
    wall_seconds: float

    def to_json(self) -> dict[str, Any]:
        return {
            "partition_db_path": self.partition_db_path,
            "scope": list(self.scope),
            "files_indexed": self.files_indexed,
            "files_deleted": self.files_deleted,
            "files_skipped": self.files_skipped,
            "entities": self.entities,
            "edges": self.edges,
            "bytes_written": self.bytes_written,
            "wall_seconds": self.wall_seconds,
        }


def _admits(policy: sg.SourceGraphIgnorePolicy, relative: str) -> bool:
    """Mirror the canonical :func:`source_graph.iter_source_files` admission.

    The base index admits a file only when the repository policy admits it, so
    the partition gates on exactly the SAME rule -- otherwise a changed path the
    base deliberately omits (a disabled language, an excluded directory, a
    repository exclude glob) would still get a partition row and the two sides
    would no longer be built alike. This checks the policy's
    ``indexed_extensions`` (which already reflects disabled languages), the
    excluded directory basenames, and the repository exclude globs at both the
    directory and file level. A path the policy rejects is skipped, exactly as a
    non-indexable ``.txt`` is -- never a build failure. Chosen over admitting a
    declared path unconditionally so the partition can never diverge from the
    base's contents.
    """

    posix = Path(relative).as_posix()
    if Path(relative).suffix.casefold() not in policy.indexed_extensions:
        return False
    parts = posix.split("/")
    for depth, name in enumerate(parts[:-1]):
        if name in policy.exclude_dirs or name.endswith(".egg-info"):
            return False
        if sg._glob_ignored(
            "/".join(parts[: depth + 1]), policy.exclude_globs, is_dir=True
        ):
            return False
    return not sg._glob_ignored(posix, policy.exclude_globs)


def build_partition(
    repo_root: Path,
    changed_paths: Sequence[Mapping[str, str]],
    partition_db_path: Path,
    *,
    base_db_path: Path | None = None,
) -> PartitionBuildReport:
    """Build an index over ``changed_paths`` ALONE into ``partition_db_path``.

    ``changed_paths`` is the packet-shaped ``[{"path": rel, "sha256": hex}]``
    list. For each entry:

      * an existing file the repository policy admits is indexed exactly
        (hash-bound) through :func:`source_graph.index_file`;
      * an existing file the policy does NOT admit -- a non-indexable suffix, a
        disabled language, an excluded directory or exclude glob -- is skipped
        and never blocks the build. Admission is deliberately routed through the
        SAME :func:`source_graph.load_ignore_policy` the base uses (see
        :func:`_admits`), so the partition and the base are built by one rule
        and a path the base omits never gets a partition row;
      * a path that no longer exists is a deletion -- removed via
        :func:`source_graph.remove_file` so the partition carries no row for
        it, while its scope entry still hides the base row at query time.

    The database is created fresh and empty; the base index is NEVER read,
    copied, or opened here. When ``base_db_path`` is given, a composed-view
    marker (base fingerprint + full declared scope) is written so a later
    read-only open composes the two indexes; without it the partition stands
    alone. The base fingerprint is size + mtime_ns only, with no content hash
    (see :func:`_base_fingerprint`): an in-place base replacement preserving
    both is a documented, accepted blind spot, not a detected shift. Source
    Graph contract failures (hash mismatch, unreadable file) propagate unchanged
    as :class:`source_graph.SourceGraphError`; path-safety violations raise
    :class:`PartitionError`.
    """

    repo_root = Path(repo_root).resolve()
    partition_db_path = Path(partition_db_path)
    policy = sg.load_ignore_policy(repo_root)
    started = time.monotonic()

    # Create the empty schema up front so the composed marker can always be
    # written and readiness checks always find the schema, even for a scope of
    # only deletions or non-indexable paths.
    schema_conn = sg.connect(partition_db_path)
    schema_conn.close()

    scope: list[str] = []
    files_indexed = files_deleted = files_skipped = 0
    entities = edges = 0
    with sg.database_path_override(partition_db_path):
        for changed in changed_paths:
            relative = str(changed.get("path") or "")
            expected_hash = str(changed.get("sha256") or "")
            if not relative:
                raise PartitionError("partition_changed_path_missing")
            scope.append(relative)
            raw = repo_root / relative
            candidate = raw.resolve(strict=False)
            if not candidate.is_relative_to(repo_root):
                raise PartitionError(
                    f"quality_review_candidate_path_escapes_workspace:{relative}"
                )
            # ``resolve`` canonicalizes the final component, so a symlink must
            # be tested on the UNRESOLVED path (as worker_ai_tools_mcp does on
            # db_path); a post-resolve ``is_symlink`` can never fire.
            if raw.is_symlink():
                raise PartitionError(
                    f"quality_review_candidate_path_symlink:{relative}"
                )
            if candidate.is_file():
                if not _admits(policy, relative):
                    files_skipped += 1
                    continue
                result = sg.index_file(repo_root, relative, expected_hash)
                files_indexed += 1
                entities += int(result.get("entities") or 0)
                edges += int(result.get("edges") or 0)
            elif candidate.exists():
                raise PartitionError(
                    f"quality_review_candidate_path_not_file:{relative}"
                )
            else:
                sg.remove_file(repo_root, relative)
                files_deleted += 1

    write_composed_marker(partition_db_path, base_db_path, scope)
    try:
        bytes_written = int(partition_db_path.stat().st_size)
    except OSError:
        bytes_written = 0
    return PartitionBuildReport(
        partition_db_path=str(partition_db_path),
        scope=tuple(scope),
        files_indexed=files_indexed,
        files_deleted=files_deleted,
        files_skipped=files_skipped,
        entities=entities,
        edges=edges,
        bytes_written=bytes_written,
        wall_seconds=max(0.0, time.monotonic() - started),
    )


# ---------------------------------------------------------------------------
# Composed-view marker -- how a read-only open discovers its base + scope
# ---------------------------------------------------------------------------

def _base_fingerprint(base_db_path: Path) -> dict[str, Any]:
    """Cheap base identity: path + size + mtime_ns, with NO content hash.

    Content-hashing a 100+ MB base on every open would reintroduce exactly the
    base-size-proportional cost this module exists to remove, so the shift check
    is deliberately size + mtime_ns only. The accepted, documented blind spot:
    an in-place base replacement that preserves BOTH size and mtime_ns is not
    detected as a shift. The security review rated this low residual risk -- the
    base is published atomically (a new inode with a fresh mtime), not rewritten
    in place -- and it is recorded here so the next reader need not rediscover it.
    """

    resolved = Path(base_db_path).resolve()
    stat = resolved.stat()
    return {
        "db_path": str(resolved),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def write_composed_marker(
    partition_db_path: Path,
    base_db_path: Path | None,
    scope: Sequence[str],
) -> None:
    """Persist the base fingerprint and full declared scope in partition meta.

    ``base_db_path`` of ``None`` records a standalone partition (no base to
    compose); a later read-only open then serves partition rows only.
    """

    base_info = _base_fingerprint(base_db_path) if base_db_path is not None else None
    payload = {
        "schema_id": COMPOSED_VIEW_SCHEMA_ID,
        "base": base_info,
        "scope": list(dict.fromkeys(str(item) for item in scope)),
    }
    conn = sqlite3.connect(str(partition_db_path), timeout=30.0)
    try:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (
                COMPOSED_VIEW_META_KEY,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def read_composed_marker(db_path: Path) -> dict[str, Any] | None:
    """Read the composed-view marker; ``None`` when absent or unreadable."""

    try:
        conn = sqlite3.connect(
            f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True
        )
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key=?", (COMPOSED_VIEW_META_KEY,)
            ).fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return None
    if row is None:
        return None
    try:
        payload = json.loads(str(row[0]))
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _verify_base_unshifted(base: Mapping[str, Any]) -> Path:
    """Return the base path only if its fingerprint still matches; else raise."""

    base_path = Path(str(base.get("db_path") or ""))
    try:
        stat = base_path.stat()
    except OSError as exc:
        raise PartitionBaseShiftError(f"composed_base_missing:{base_path}") from exc
    if int(stat.st_size) != int(base.get("size", -1)) or int(
        stat.st_mtime_ns
    ) != int(base.get("mtime_ns", -1)):
        raise PartitionBaseShiftError(f"composed_base_shifted:{base_path}")
    return base_path


# ---------------------------------------------------------------------------
# Composed schema installation over a live connection
# ---------------------------------------------------------------------------

def _install_composed_schema(
    conn: sqlite3.Connection,
    base_path: Path,
    partition_aliases: Sequence[str],
    scope: Sequence[str],
    *,
    attach: Mapping[str, Path] | None = None,
) -> None:
    """Attach the base read-only and install precedence ``TEMP`` views.

    ``partition_aliases`` names every attached schema that contributes
    partition rows, in precedence order (earlier wins). ``attach`` maps any
    alias that is not already an open schema to the partition file to attach
    for it. The connection may have ``query_only`` set; it is toggled off only
    long enough to build the ``TEMP`` scope table and views, then restored.
    """

    conn.execute("PRAGMA query_only=OFF")
    conn.execute(
        "ATTACH DATABASE ? AS base",
        (f"{Path(base_path).resolve().as_uri()}?mode=ro",),
    )
    for alias, path in (attach or {}).items():
        conn.execute(
            f"ATTACH DATABASE ? AS {alias}",
            (f"{Path(path).resolve().as_uri()}?mode=ro",),
        )
    conn.execute(
        f"CREATE TEMP TABLE {_SCOPE_TABLE}(file_path TEXT PRIMARY KEY)"
    )
    conn.executemany(
        f"INSERT OR IGNORE INTO {_SCOPE_TABLE}(file_path) VALUES(?)",
        [(str(item),) for item in dict.fromkeys(scope)],
    )
    for table in _COMPOSED_TABLES:
        parts = [f"SELECT * FROM {alias}.{table}" for alias in partition_aliases]
        parts.append(
            f"SELECT * FROM base.{table} "
            f"WHERE file_path NOT IN (SELECT file_path FROM temp.{_SCOPE_TABLE})"
        )
        conn.execute(
            f"CREATE TEMP VIEW {table} AS " + " UNION ALL ".join(parts)
        )
    conn.execute("PRAGMA query_only=ON")


def attach_composed_base_if_marked(
    conn: sqlite3.Connection, db_path: Path
) -> bool:
    """Compose ``conn`` (opened on a partition DB) with its recorded base.

    Called from :func:`source_graph.connect` for every read-only open. A DB
    without the composed marker (every ordinary Source Graph database) is left
    untouched and this returns ``False``. A marked DB attaches its base
    read-only and installs the precedence views; a missing or shifted base
    fails closed with :class:`PartitionBaseShiftError` rather than silently
    serving partition rows alone.
    """

    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (COMPOSED_VIEW_META_KEY,)
        ).fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return False
    try:
        marker = json.loads(str(row[0]))
    except (json.JSONDecodeError, TypeError):
        return False
    base = marker.get("base") if isinstance(marker, dict) else None
    if not isinstance(base, dict):
        return False
    base_path = _verify_base_unshifted(base)
    scope = [str(item) for item in (marker.get("scope") or [])]
    # The partition IS this connection's own ``main`` schema.
    _install_composed_schema(conn, base_path, ["main"], scope)
    return True


def is_composed(conn: sqlite3.Connection) -> bool:
    """True when a ``base`` schema is attached (a composed view is active)."""

    try:
        return any(
            str(row[1]) == "base" for row in conn.execute("PRAGMA database_list")
        )
    except sqlite3.Error:
        return False


def _partition_schemas(conn: sqlite3.Connection) -> list[str]:
    """Attached schemas that contribute partition rows, in precedence order."""

    schemas = [str(row[1]) for row in conn.execute("PRAGMA database_list")]
    return [name for name in schemas if name not in ("base", "temp")]


def _schema_has_fts(conn: sqlite3.Connection, schema: str) -> bool:
    try:
        return conn.execute(
            f"SELECT 1 FROM {schema}.sqlite_master "
            "WHERE type='table' AND name='entities_fts' LIMIT 1"
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def composed_find(
    conn: sqlite3.Connection, term: str, *, limit: int = 24
) -> list[dict[str, Any]]:
    """FTS ``find`` across a composed view with per-file precedence.

    ``find`` in ``source_graph`` joins ``entities_fts`` to ``entities`` by row
    id; the composed ``entities`` ``TEMP`` view unions two independent id
    spaces, so that join cannot be run against it. Instead each partition
    schema and the base run the same FTS expressions against their OWN
    ``entities_fts``/``entities`` pair, partition results are emitted first
    (partition wins), and base rows for any file in scope are excluded.
    """

    term = (term or "").strip()
    if not term:
        return []
    limit = max(1, min(int(limit), sg.MAX_BUDGET_ROWS))
    expressions = [sg._fts_phrase(term)]
    if len(sg._query_tokens(term)) > 1:
        expressions.append(sg._fts_terms(term, operator="AND"))
        if not sg._looks_like_identifier_query(term):
            expressions.append(sg._fts_terms(term, operator="OR"))

    ordered = [(schema, False) for schema in _partition_schemas(conn)]
    ordered.append(("base", True))
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for schema, is_base in ordered:
        if not _schema_has_fts(conn, schema):
            continue
        base_filter = (
            f" AND e.file_path NOT IN (SELECT file_path FROM temp.{_SCOPE_TABLE})"
            if is_base
            else ""
        )
        rows: list[sqlite3.Row] = []
        for expression in expressions:
            try:
                # FTS5's ``MATCH`` must name the table with the bare
                # ``entities_fts`` token (a schema-qualified ``base.entities_fts
                # MATCH`` is rejected as an unknown column); it binds to the one
                # FTS table in this query's FROM clause, so per-schema queries
                # never cross-contaminate even with both indexes attached.
                rows = conn.execute(
                    f"SELECT e.file_path, e.kind, e.name, e.qualname, e.line_start, "
                    f"e.line_end, e.signature, e.evidence_label, e.confidence "
                    f"FROM {schema}.entities_fts "
                    f"JOIN {schema}.entities e "
                    f"ON e.id = {schema}.entities_fts.entity_id "
                    f"WHERE entities_fts MATCH ?{base_filter} "
                    "ORDER BY CASE WHEN lower(e.qualname)=lower(?) THEN 0 "
                    "WHEN lower(e.name)=lower(?) THEN 1 "
                    "WHEN lower(e.name) LIKE lower(?) THEN 2 ELSE 3 END, "
                    "e.file_path, e.line_start LIMIT ?",
                    (expression, term, term, f"{term}%", limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if rows:
                break
        for row in rows:
            data = dict(row)
            key = (str(data.get("file_path")), str(data.get("qualname")))
            if key in seen:
                continue
            seen.add(key)
            out.append(data)
            if len(out) >= limit:
                return out
    return out


# ---------------------------------------------------------------------------
# Cross-boundary edge resolution
# ---------------------------------------------------------------------------

_RESOLVABLE_EDGE_KINDS: tuple[str, ...] = ("calls", "inherits")


def _composed_entity_index(
    conn: sqlite3.Connection,
) -> tuple[dict[str, list[dict[str, str]]], set[str]]:
    """Return ``(name -> [candidate,...], set(all composed qualnames))``.

    A candidate records the ``side`` (``partition`` or ``base``), ``file_path``
    and ``qualname`` of one composed definition, so a resolver can name which
    side of the boundary it resolved into.
    """

    by_name: dict[str, list[dict[str, str]]] = {}
    qualnames: set[str] = set()
    kinds = "('function','method','class','struct','union','enum','namespace')"
    for schema in _partition_schemas(conn):
        try:
            rows = conn.execute(
                f"SELECT file_path, name, qualname FROM {schema}.entities "
                f"WHERE kind IN {kinds}"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for row in rows:
            by_name.setdefault(str(row[1]), []).append(
                {"side": "partition", "file_path": str(row[0]), "qualname": str(row[2])}
            )
            qualnames.add(str(row[2]))
    base_rows = conn.execute(
        f"SELECT file_path, name, qualname FROM base.entities "
        f"WHERE kind IN {kinds} "
        f"AND file_path NOT IN (SELECT file_path FROM temp.{_SCOPE_TABLE})"
    ).fetchall()
    for row in base_rows:
        by_name.setdefault(str(row[1]), []).append(
            {"side": "base", "file_path": str(row[0]), "qualname": str(row[2])}
        )
        qualnames.add(str(row[2]))
    return by_name, qualnames


def resolve_cross_boundary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Resolve ``calls``/``inherits`` edges that cross the partition/base line.

    RESOLVED cases (each proven by a test):

      * ``partition_to_base`` -- a call from a CHANGED file to an unchanged
        symbol whose only definition lives in the base.
      * ``base_to_partition`` -- a call from an UNCHANGED file whose recorded
        target no longer names any composed entity (its definition MOVED into
        the partition) and whose name has exactly one partition definition.
      * ``partition_to_partition`` -- a changed-file call whose target's sole
        definition is in another changed file.

    An edge is a resolution candidate only when its current ``dst_qualname`` is
    absent from the composed entity set (unresolved, or resolved to a
    definition the composition hid). Resolution is by unique name match.

    NAMED LIMITATIONS (returned in ``unresolved`` rather than guessed):

      * ``ambiguous`` -- the name has more than one composed definition; a
        lexical resolver cannot pick one without type/import evidence this
        function deliberately does not infer.
      * ``no_candidate`` -- the name matches no composed definition (an external
        symbol, a builtin, or dynamic dispatch).
    """

    by_name, composed_qualnames = _composed_entity_index(conn)
    placeholders = ",".join("?" for _ in _RESOLVABLE_EDGE_KINDS)
    resolved: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []

    def consider(side: str, edge: sqlite3.Row) -> None:
        dst_qualname = edge["dst_qualname"]
        if dst_qualname is not None and str(dst_qualname) in composed_qualnames:
            return  # already resolves to a live composed definition
        dst_name = str(edge["dst_name"])
        candidates = by_name.get(dst_name, [])
        record = {
            "src_qualname": str(edge["src_qualname"]),
            "dst_name": dst_name,
            "src_file": str(edge["file_path"]),
            "kind": str(edge["kind"]),
        }
        if len(candidates) == 1:
            target = candidates[0]
            resolved.append({
                **record,
                "dst_qualname": target["qualname"],
                "dst_file": target["file_path"],
                "direction": f"{side}_to_{target['side']}",
            })
        elif not candidates:
            unresolved.append({**record, "reason": "no_candidate"})
        else:
            unresolved.append({**record, "reason": "ambiguous"})

    for schema in _partition_schemas(conn):
        try:
            edges = conn.execute(
                f"SELECT file_path, kind, src_qualname, dst_name, dst_qualname "
                f"FROM {schema}.edges WHERE kind IN ({placeholders})",
                _RESOLVABLE_EDGE_KINDS,
            ).fetchall()
        except sqlite3.Error:
            edges = []
        for edge in edges:
            consider("partition", edge)
    base_edges = conn.execute(
        f"SELECT file_path, kind, src_qualname, dst_name, dst_qualname "
        f"FROM base.edges WHERE kind IN ({placeholders}) "
        f"AND file_path NOT IN (SELECT file_path FROM temp.{_SCOPE_TABLE})",
        _RESOLVABLE_EDGE_KINDS,
    ).fetchall()
    for edge in base_edges:
        consider("base", edge)

    return {
        "resolved": resolved,
        "unresolved": unresolved,
        "resolved_count": len(resolved),
        "unresolved_count": len(unresolved),
    }


# ---------------------------------------------------------------------------
# ComposedView -- an immutable, identity-bound binding of base + partitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ComposedView:
    """An immutable binding of one base index plus one or more partitions.

    The view is a value: it holds paths and an identity, never an open handle,
    so the base cannot shift underneath it silently -- every :meth:`open`
    re-verifies the base fingerprint. Identity binding
    (``packet_sha256`` / ``target_request_id`` / ``target_task_id``) lets a
    reviewer refuse a view that does not match its own packet.
    """

    base_db_path: Path
    partition_db_paths: tuple[Path, ...]
    scope: frozenset[str]
    base_fingerprint: dict[str, Any] = field(default_factory=dict)
    packet_sha256: str = ""
    target_request_id: str = ""
    target_task_id: str = ""

    @classmethod
    def bind(
        cls,
        base_db_path: Path,
        partition_db_paths: Sequence[Path],
        *,
        packet_sha256: str = "",
        target_request_id: str = "",
        target_task_id: str = "",
    ) -> "ComposedView":
        """Bind a base and partitions, reading each partition's declared scope.

        Partition scopes must be DISJOINT: two partitions that both claim a
        file would make precedence ambiguous, so that is rejected rather than
        silently last-wins.
        """

        base = Path(base_db_path).resolve()
        partitions = tuple(Path(item).resolve() for item in partition_db_paths)
        scope: set[str] = set()
        for partition in partitions:
            marker = read_composed_marker(partition)
            declared = [
                str(item)
                for item in ((marker or {}).get("scope") or [])
            ]
            overlap = scope.intersection(declared)
            if overlap:
                raise PartitionError(
                    f"partition_scope_overlap:{','.join(sorted(overlap))}"
                )
            scope.update(declared)
        return cls(
            base_db_path=base,
            partition_db_paths=partitions,
            scope=frozenset(scope),
            base_fingerprint=_base_fingerprint(base),
            packet_sha256=packet_sha256,
            target_request_id=target_request_id,
            target_task_id=target_task_id,
        )

    def verify_binding(
        self,
        packet_sha256: str,
        target_request_id: str,
        target_task_id: str,
    ) -> None:
        """Raise :class:`PartitionBindingError` unless the identity matches.

        This is the reviewer's refusal: a view whose binding does not match the
        reviewer's own packet must never answer that reviewer's queries.
        """

        if (
            self.packet_sha256 != packet_sha256
            or self.target_request_id != target_request_id
            or self.target_task_id != target_task_id
        ):
            raise PartitionBindingError("composed_view_binding_mismatch")

    def assert_base_unshifted(self) -> None:
        """Raise :class:`PartitionBaseShiftError` if the base moved/changed."""

        _verify_base_unshifted(self.base_fingerprint)

    @contextmanager
    def open(self) -> Iterator[sqlite3.Connection]:
        """Yield a read-only connection that answers as one composed index.

        The base is opened read-only and can never be written through this
        handle; each partition is attached read-only; precedence ``TEMP`` views
        make unqualified ``files``/``entities``/``edges`` resolve with
        partition-wins semantics.
        """

        self.assert_base_unshifted()
        conn = sqlite3.connect("file::memory:", uri=True, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            aliases = [f"p{index}" for index in range(len(self.partition_db_paths))]
            attach = {
                alias: path
                for alias, path in zip(aliases, self.partition_db_paths)
            }
            _install_composed_schema(
                conn, self.base_db_path, aliases, self.scope, attach=attach
            )
            yield conn
        finally:
            conn.close()

    # -- Bounded query helpers over the composed connection ------------------

    def find(self, term: str, *, limit: int = 24) -> list[dict[str, Any]]:
        with self.open() as conn:
            return composed_find(conn, term, limit=limit)

    def context(self, file_path: str) -> dict[str, Any]:
        with self.open() as conn:
            return sg.context(conn, file_path)

    def func(self, name: str, *, limit: int = 24) -> list[dict[str, Any]]:
        with self.open() as conn:
            return sg.func(conn, name, limit=limit)

    def struct(self, name: str, *, limit: int = 24) -> list[dict[str, Any]]:
        with self.open() as conn:
            return sg.struct(conn, name, limit=limit)

    def resolve_cross_boundary(self) -> dict[str, Any]:
        with self.open() as conn:
            return resolve_cross_boundary(conn)


__all__ = [
    "COMPOSED_VIEW_META_KEY",
    "COMPOSED_VIEW_SCHEMA_ID",
    "ComposedView",
    "PartitionBaseShiftError",
    "PartitionBindingError",
    "PartitionBuildReport",
    "PartitionError",
    "attach_composed_base_if_marked",
    "build_partition",
    "composed_find",
    "is_composed",
    "read_composed_marker",
    "resolve_cross_boundary",
    "write_composed_marker",
]
