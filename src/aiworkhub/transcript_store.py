"""Schema-compatible writes for canonical transcript stores.

AIWorkHub can adopt older repository transcript databases whose ``documents``
table carries richer provenance columns and whose FTS index uses external
content.  Fresh repositories use a smaller schema.  This adapter keeps the
public context-write/import paths compatible with both layouts without
replacing or rewriting either canonical database.
"""

from __future__ import annotations

import sqlite3
from typing import Any


class TranscriptStoreError(sqlite3.DatabaseError):
    pass


def _table_info(con: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    rows = con.execute(f'PRAGMA table_info("{table}")').fetchall()
    if not rows:
        raise TranscriptStoreError(f"transcript_table_missing:{table}")
    return rows


def insert_document(
    con: sqlite3.Connection,
    *,
    source_id: str,
    timestamp: str,
    kind: str,
    content: str,
    source: str,
    speaker: str = "",
    tags: str = "",
    session_id: int | None = None,
) -> int:
    """Insert and index one document across supported transcript schemas."""

    info = _table_info(con, "documents")
    available = {str(row[1]) for row in info}
    values: dict[str, Any] = {
        "source": source,
        "source_id": source_id,
        "session_id": session_id,
        "timestamp": timestamp,
        "kind": kind,
        "speaker": speaker,
        "content": content,
        "tags": tags,
    }
    unsupported_required = [
        str(row[1])
        for row in info
        if int(row[3] or 0)
        and row[4] is None
        and not int(row[5] or 0)
        and str(row[1]) not in values
    ]
    if unsupported_required:
        raise TranscriptStoreError(
            "transcript_schema_unsupported_required:" + ",".join(unsupported_required)
        )
    columns = [name for name in values if name in available]
    if "source_id" not in columns or "content" not in columns:
        raise TranscriptStoreError("transcript_schema_missing_core_columns")
    placeholders = ",".join("?" for _ in columns)
    cur = con.execute(
        f'INSERT INTO documents({",".join(columns)}) VALUES({placeholders})',
        tuple(values[name] for name in columns),
    )
    document_id = int(cur.lastrowid)

    fts_info = _table_info(con, "documents_fts")
    fts_values = {"content": content, "kind": kind, "tags": tags}
    fts_columns = [str(row[1]) for row in fts_info if str(row[1]) in fts_values]
    if "content" not in fts_columns:
        raise TranscriptStoreError("transcript_fts_missing_content")
    con.execute(
        f'INSERT INTO documents_fts(rowid,{",".join(fts_columns)}) '
        f'VALUES({",".join("?" for _ in range(len(fts_columns) + 1))})',
        (document_id, *(fts_values[name] for name in fts_columns)),
    )
    return document_id


def delete_document_index(con: sqlite3.Connection, document_id: int) -> None:
    """Remove one FTS row for either standalone or external-content FTS5."""

    con.execute("DELETE FROM documents_fts WHERE rowid=?", (int(document_id),))


def _id_column(info: list[sqlite3.Row]) -> str:
    """The documents table's integer primary-key column name.

    Fresh repositories use ``doc_id``; adopted legacy schemas used ``id``.
    Whichever spelling exists is the rowid alias the FTS rowid must match.
    """
    for row in info:
        if int(row[5] or 0):  # pk column
            return str(row[1])
    return "doc_id"


def update_document(
    con: sqlite3.Connection,
    document_id: int,
    *,
    content: str,
    kind: str | None = None,
    tags: str | None = None,
) -> None:
    """Rewrite one existing document's content (and optionally kind/tags).

    Works across both supported transcript schemas: the column set is
    discovered the same way ``insert_document`` discovers it, so a rich
    adopted schema keeps its provenance columns untouched while the content
    (and the caller-provided kind/tags, when the column exists) is replaced.
    The FTS row is rebuilt in the same transaction so search can never serve
    the pre-edit text after the edit committed.
    """
    info = _table_info(con, "documents")
    available = {str(row[1]) for row in info}
    if "content" not in available:
        raise TranscriptStoreError("transcript_schema_missing_core_columns")
    id_column = _id_column(info)
    # Verify the target exists BEFORE touching any index row: a typed
    # refusal must leave the store exactly as it was.
    if con.execute(
        f"SELECT 1 FROM documents WHERE {id_column}=?", (int(document_id),)
    ).fetchone() is None:
        raise TranscriptStoreError(f"transcript_document_not_found:{document_id}")
    # External-content FTS5 reads the CONTENT TABLE at delete time, so the
    # FTS row MUST be deleted while it still mirrors the OLD text: deleting
    # after the UPDATE silently leaves the old terms in the index (measured:
    # MATCH 'before' kept matching after an update-then-delete sequence).
    # A standalone FTS table is order-insensitive, so this order is safe for
    # both schemas.
    delete_document_index(con, document_id)
    assignments = ["content=?"]
    params: list[Any] = [content]
    for column, value in (("kind", kind), ("tags", tags)):
        if value is not None and column in available:
            assignments.append(f"{column}=?")
            params.append(value)
    cur = con.execute(
        f'UPDATE documents SET {",".join(assignments)} WHERE {id_column}=?',
        (*params, int(document_id)),
    )
    if cur.rowcount != 1:
        raise TranscriptStoreError(f"transcript_document_not_found:{document_id}")
    fts_info = _table_info(con, "documents_fts")
    fts_values = {"content": content}
    if kind is not None:
        fts_values["kind"] = kind
    if tags is not None:
        fts_values["tags"] = tags
    fts_columns = [str(row[1]) for row in fts_info if str(row[1]) in fts_values]
    if "content" not in fts_columns:
        raise TranscriptStoreError("transcript_fts_missing_content")
    con.execute(
        f'INSERT INTO documents_fts(rowid,{",".join(fts_columns)}) '
        f'VALUES({",".join("?" for _ in range(len(fts_columns) + 1))})',
        (int(document_id), *(fts_values[name] for name in fts_columns)),
    )


def delete_document(con: sqlite3.Connection, document_id: int) -> str:
    """Delete one document row and its FTS row; return its id column name.

    Both supported schemas are handled through the discovered primary-key
    column.  Returns the id column spelling so the caller can quote it in a
    typed refusal instead of guessing the schema.
    """
    info = _table_info(con, "documents")
    id_column = _id_column(info)
    delete_document_index(con, document_id)
    cur = con.execute(
        f'DELETE FROM documents WHERE {id_column}=?', (int(document_id),)
    )
    if cur.rowcount != 1:
        raise TranscriptStoreError(f"transcript_document_not_found:{document_id}")
    return id_column


__all__ = [
    "TranscriptStoreError",
    "delete_document",
    "delete_document_index",
    "insert_document",
    "update_document",
]
