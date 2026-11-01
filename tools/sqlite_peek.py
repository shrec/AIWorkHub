#!/usr/bin/env python3
# aiworkhub-tool id: sqlite_peek
# aiworkhub-tool purpose: Inspect a SQLite database read-only: list tables with row counts, or run one SELECT (max 50 rows).
# aiworkhub-tool usage: python tools/sqlite_peek.py state.db "SELECT * FROM tasks LIMIT 5"
# aiworkhub-tool param: db:path
"""Read-only SQLite peek: no SQL lists tables; otherwise one SELECT/WITH statement."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

MAX_ROWS = 50
_READ_ONLY_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
# SQLITE_SELECT, SQLITE_READ, SQLITE_FUNCTION, SQLITE_RECURSIVE; literals because
# the named constants only exist from Python 3.11.
_ALLOWED_ACTIONS = {21, 20, 31, 33}


def _authorizer(action: int, *_args: Any) -> int:
    return 0 if action in _ALLOWED_ACTIONS else 1


def _jsonable(value: Any) -> Any:
    return f"<{len(value)} bytes>" if isinstance(value, (bytes, bytearray)) else value


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    conn.set_authorizer(_authorizer)
    return conn


def _tables(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {
        name: conn.execute(f'SELECT COUNT(*) FROM "{name.replace(chr(34), chr(34) * 2)}"').fetchone()[0]
        for name in names
    }


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) not in (1, 2):
        print("usage: sqlite_peek.py DB [SQL]", file=sys.stderr)
        return 2
    sql = argv[1].strip().rstrip(";").strip() if len(argv) == 2 else ""
    if len(argv) == 2 and _READ_ONLY_RE.match(sql) is None:
        print("refused: only one SELECT or WITH statement is allowed", file=sys.stderr)
        return 2
    db = Path(argv[0])
    if not db.is_file():
        print(f"unreadable: no such database {argv[0]}", file=sys.stderr)
        return 1
    try:
        conn = _connect(db)
        try:
            if not sql:
                print(json.dumps({"tables": _tables(conn)}, indent=2))
                return 0
            cursor = conn.execute(sql)
            columns = [item[0] for item in cursor.description or ()]
            rows = cursor.fetchmany(MAX_ROWS + 1)
        finally:
            conn.close()
    except (sqlite3.Error, ValueError) as exc:
        print(f"sqlite_error: {exc}", file=sys.stderr)
        return 1
    payload = {
        "columns": columns,
        "rows": [[_jsonable(cell) for cell in row] for row in rows[:MAX_ROWS]],
        "truncated": len(rows) > MAX_ROWS,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
