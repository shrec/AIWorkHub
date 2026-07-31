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


__all__ = ["TranscriptStoreError", "delete_document_index", "insert_document"]
