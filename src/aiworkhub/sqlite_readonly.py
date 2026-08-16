"""Fail-closed read-only SQLite connection helper.

Every read-only SQLite open in AIWorkHub must route through
:func:`connect_readonly`. Building the URI by raw f-string -- e.g.
``sqlite3.connect(f"file:{path}?mode=ro", uri=True)`` -- is *not* a read-only
open. In SQLite URI syntax ``#`` begins a fragment, so any path containing
``#`` swallows the whole ``?mode=ro`` query. The database then opens
READ-WRITE with create-if-missing, and because the fragment is stripped it
opens a DIFFERENT FILE than the caller named.

Two independent guarantees are applied here:

1. The path is percent-encoded via :meth:`pathlib.Path.as_uri`, so ``#``
   becomes ``%23`` and the ``?mode=ro`` query component survives URI parsing.
2. ``PRAGMA query_only = ON`` is issued immediately after connecting, so a
   write is refused even if URI parsing were bypassed.

Callers preserve their own ``timeout`` argument by passing it here, and their
own row-factory behaviour by setting it after the call (this helper never
touches either).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# sqlite3.connect()'s documented default timeout; preserved for call sites
# that previously omitted an explicit timeout argument.
DEFAULT_TIMEOUT = 5.0


def connect_readonly(path: str | Path, *, timeout: float = DEFAULT_TIMEOUT) -> sqlite3.Connection:
    """Open ``path`` read-only via a percent-encoded URI plus ``query_only``.

    ``Path(path).resolve().as_uri()`` percent-encodes characters such as
    ``#`` so ``?mode=ro`` is parsed as a query component rather than being
    swallowed into a fragment. ``PRAGMA query_only = ON`` then provides a
    second, independent read-only guarantee at the engine level.
    """
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.execute("PRAGMA query_only=ON")
    return conn
