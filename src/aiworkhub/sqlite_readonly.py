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

A read-only open can still be refused by storage rather than by SQL, and
NF-2026-01162 is that case: a ``mode=ro`` reader of a WAL database is
expected to create and write the ``-shm`` wal-index, so a database whose
directory or sidecars this seat cannot write fails with ``attempt to write a
readonly database`` for a caller that only ever reads. A third, narrower
guarantee covers it:

3. When that failure is observed AND the sidecar state is provably
   unwritable, the open is retried with ``immutable=1``, which reads the main
   database file directly and needs no sidecar. That read is DEGRADED -- an
   ``immutable`` reader cannot see uncheckpointed WAL frames -- so the
   fallback is recorded on the connection and named by :func:`fallback_mode`
   for callers to report; it is never presented as a normal read.

Callers preserve their own ``timeout`` argument by passing it here, and their
own row-factory behaviour by setting it after the call (this helper never
touches either).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

# sqlite3.connect()'s documented default timeout; preserved for call sites
# that previously omitted an explicit timeout argument.
DEFAULT_TIMEOUT = 5.0

# The single typed name for the one degraded open this helper will fall back
# to. Callers report it verbatim so a response built on an ``immutable``
# snapshot says so instead of passing for a normal read.
FALLBACK_IMMUTABLE = "sqlite_readonly_immutable_fallback"

# SQLite's error text for the shapes the read-only WAL case takes: the reader
# has to create the ``-shm`` wal-index (or recover a non-empty ``-wal``) and
# the storage will not let it. This is only a precondition -- the fallback
# additionally requires PROOF that the sidecar state is unwritable, so a
# corrupt, missing or merely locked database still fails as it does today.
_READONLY_STORAGE_MARKERS = (
    "attempt to write a readonly database",
    "unable to open database file",
    "disk i/o error",
)

# The first statement that actually touches the database file. ``PRAGMA
# query_only`` does not, so without this the read-only WAL failure surfaces on
# the CALLER's first query -- long past the point where this helper could
# still fall back. It is issued ONLY for a WAL-mode database, so every other
# read-only open behaves exactly as it did before.
_PROBE_SQL = "SELECT 1 FROM sqlite_master LIMIT 1"

_SQLITE_HEADER_MAGIC = b"SQLite format 3\x00"
_WAL_FORMAT_VERSION = 2


class ReadonlyConnection(sqlite3.Connection):
    """A read-only connection that remembers how it had to be opened.

    ``sqlite3.Connection`` carries no instance dictionary, so the fallback
    cannot be recorded on a plain connection. This subclass exists only to
    hold :attr:`readonly_fallback`; it adds no behaviour, and every caller
    still receives something that *is* a ``sqlite3.Connection``.
    """

    #: ``None`` for a normal ``mode=ro`` open, else :data:`FALLBACK_IMMUTABLE`.
    readonly_fallback: str | None = None


def fallback_mode(conn: sqlite3.Connection) -> str | None:
    """Name the degraded open behind ``conn``, or ``None`` if it is normal.

    Readable after ``conn.close()`` -- it is a plain attribute, not a query --
    so a caller can close its connection before building its response.
    """

    return getattr(conn, "readonly_fallback", None)


def connect_readonly(path: str | Path, *, timeout: float = DEFAULT_TIMEOUT) -> sqlite3.Connection:
    """Open ``path`` read-only via a percent-encoded URI plus ``query_only``.

    ``Path(path).resolve().as_uri()`` percent-encodes characters such as
    ``#`` so ``?mode=ro`` is parsed as a query component rather than being
    swallowed into a fragment. ``PRAGMA query_only = ON`` then provides a
    second, independent read-only guarantee at the engine level.

    NF-2026-01162: a ``mode=ro`` reader of a WAL database is still expected to
    create and write the ``-shm`` wal-index, so a database whose directory or
    sidecar files this process cannot write fails with ``attempt to write a
    readonly database`` even though the caller only ever reads. When -- and
    only when -- that failure is observed AND the sidecar state is provably
    unwritable, the open is retried with ``immutable=1``, which reads the main
    database file directly and needs no sidecar at all. Neither URI ever names
    ``mode=rw`` or ``mode=rwc``, so no path through here can open the database
    writable. The fallback is DEGRADED, not equivalent: an ``immutable`` reader
    cannot see uncheckpointed WAL frames, and it also takes no locks, so if the
    canonical writer checkpoints while the degraded read runs SQLite may return
    inconsistent rows or raise ``SQLITE_CORRUPT``. That is why the open is
    recorded on the connection and reported by callers (see
    :func:`fallback_mode`) rather than presented as a normal read.
    """

    resolved = Path(path).resolve()
    base_uri = resolved.as_uri()
    probe = _is_wal_mode(resolved)
    try:
        return _open(f"{base_uri}?mode=ro", timeout=timeout, probe=probe, fallback=None)
    except sqlite3.Error as exc:
        if not _readonly_storage_failure(exc, resolved):
            raise
    return _open(
        f"{base_uri}?mode=ro&immutable=1",
        timeout=timeout,
        probe=probe,
        fallback=FALLBACK_IMMUTABLE,
    )


def _sqlite_connect(uri: str, *, timeout: float) -> sqlite3.Connection:
    """The one place this module hands a URI to SQLite.

    Named rather than inlined so the storage refusal this helper exists to
    survive can be reproduced deterministically. A directory whose write
    permission is actually revoked is not reproducible everywhere (Windows
    ACLs, and sandboxes that deny ``chmod`` outright), and neither is the
    shared-memory setup a WAL reader needs; injecting the refusal at this
    seam reproduces the same failure with the same error, without either.
    """

    return sqlite3.connect(uri, uri=True, timeout=timeout, factory=ReadonlyConnection)


def _open(
    uri: str, *, timeout: float, probe: bool, fallback: str | None
) -> sqlite3.Connection:
    conn = _sqlite_connect(uri, timeout=timeout)
    opened = False
    try:
        conn.execute("PRAGMA query_only=ON")
        if probe:
            conn.execute(_PROBE_SQL).fetchone()
        opened = True
    finally:
        if not opened:
            conn.close()
    conn.readonly_fallback = fallback
    return conn


def _is_wal_mode(db_path: Path) -> bool:
    """Does ``db_path``'s 100-byte header declare WAL journalling?

    Bytes 18 and 19 are the file-format read/write versions; ``2`` means WAL.
    A plain 20-byte read, so the common non-WAL open pays nothing beyond it
    and never runs the probe query or the writability checks below.
    """

    try:
        with open(db_path, "rb") as handle:
            header = handle.read(20)
    except OSError:
        return False
    if len(header) < 20 or not header.startswith(_SQLITE_HEADER_MAGIC):
        return False
    return _WAL_FORMAT_VERSION in (header[18], header[19])


def _readonly_storage_failure(exc: sqlite3.Error, db_path: Path) -> bool:
    """Is ``exc`` the read-only-WAL case, on a database that really exists?"""

    if not any(marker in str(exc).lower() for marker in _READONLY_STORAGE_MARKERS):
        return False
    try:
        if not db_path.is_file():
            return False
    except OSError:
        return False
    return _sidecar_state_unwritable(db_path)


def _sidecar_state_unwritable(db_path: Path) -> bool:
    """Can a read-only reader NOT create or write this database's sidecars?"""

    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        try:
            if not sidecar.exists():
                continue
            if not sidecar.is_file():
                return True
        except OSError:
            return True
        if not _writable_file(sidecar):
            return True
    return not _writable_directory(db_path.parent)


def _writable_file(path: Path) -> bool:
    try:
        with open(path, "r+b"):
            return True
    except OSError:
        return False


def _writable_directory(directory: Path) -> bool:
    """Can this process actually create a file in ``directory``?

    ``os.access(directory, os.W_OK)`` is not an answer on Windows: it reports
    FILE_ATTRIBUTE_READONLY, a flag that does not govern directory writes, so
    an ACL-denied directory still reports writable. The only portable answer
    is to try. This runs ONLY after a read-only open already failed with a
    storage error, never on the success path, and it writes nothing into the
    database or its sidecars.
    """

    try:
        handle = tempfile.NamedTemporaryFile(
            dir=str(directory), prefix=".aiworkhub-ro-probe.", delete=False
        )
    except OSError:
        return False
    try:
        handle.close()
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
    return True
