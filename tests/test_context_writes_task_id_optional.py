"""task_id is genuinely optional in the context_mutations audit trail.

The code called task_id optional (``required=False`` in ``_identity``) while the
schema declared it ``TEXT NOT NULL``; the two only agreed by the accident that
``_bounded`` never returns ``None``.  These tests pin the honest contract:

* a manager write made with no task in scope stores ``NULL``;
* a write made inside a task records that exact task_id;
* the schema itself makes task_id nullable and nothing else;
* any sqlite integrity failure is re-raised naming the offending column;
* legacy stores holding ``task_id=''`` migrate to nullable and read ``NULL``.

Task NF-2026-00268-CONTEXT-WRITE.
"""

from __future__ import annotations

import sqlite3

import pytest

from aiworkhub import context_writes


def _seed_memory_db(path):
    """Create a canonical ``memory`` db with an empty ``memories`` table."""
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE memories(id INTEGER PRIMARY KEY,key TEXT,value TEXT,tags TEXT,scope TEXT)"
        )
        con.commit()
    finally:
        con.close()


def _point_registry_at(monkeypatch, path):
    monkeypatch.setattr(
        context_writes.storage_registry, "load_storage_registry", lambda repo: object()
    )
    monkeypatch.setattr(
        context_writes.storage_registry,
        "resolve_database_path",
        lambda registry, db_id: path,
    )


def _read(path, sql, params=()):
    con = sqlite3.connect(path)
    try:
        con.row_factory = sqlite3.Row
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def test_write_without_task_stores_null(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    _seed_memory_db(db)
    _point_registry_at(monkeypatch, db)

    context_writes.memory_write(
        tmp_path,
        actor={"role": "manager", "actor_id": "mgr-1", "provider": "claude", "session_id": "s1"},
        action="remember",
        key="no-task-key",
        value="v",
        idempotency_key="idem-no-task-0001",
        provenance="prov",
    )

    rows = _read(
        db, "SELECT task_id FROM context_mutations WHERE component='memory'"
    )
    assert len(rows) == 1
    # Absence of a task is a real, honest NULL -- not the empty-string lie.
    assert rows[0]["task_id"] is None


def test_write_inside_task_records_exact_task_id(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    _seed_memory_db(db)
    _point_registry_at(monkeypatch, db)

    context_writes.memory_write(
        tmp_path,
        actor={
            "role": "worker",
            "actor_id": "w-1",
            "provider": "claude",
            "session_id": "s1",
            "task_id": "NF-2026-00268-CONTEXT-WRITE",
        },
        action="remember",
        key="with-task-key",
        value="v",
        idempotency_key="idem-with-task-0001",
        provenance="prov",
    )

    rows = _read(
        db, "SELECT task_id FROM context_mutations WHERE component='memory'"
    )
    assert len(rows) == 1
    assert rows[0]["task_id"] == "NF-2026-00268-CONTEXT-WRITE"


def test_schema_makes_only_task_id_nullable(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    _seed_memory_db(db)
    _point_registry_at(monkeypatch, db)

    con = context_writes._open(tmp_path, "memory")
    try:
        info = {row[1]: bool(row[3]) for row in con.execute("PRAGMA table_info(context_mutations)")}
    finally:
        con.close()

    assert info["task_id"] is False  # genuinely nullable now
    # Every other declared NOT NULL column keeps its guarantee.
    for column in (
        "idempotency_key",
        "component",
        "action",
        "entity_key",
        "actor_role",
        "actor_id",
        "provider",
        "session_id",
        "provenance",
        "payload_sha256",
        "created_at",
    ):
        assert info[column] is True, column


def test_integrity_error_names_offending_column(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    _seed_memory_db(db)
    _point_registry_at(monkeypatch, db)

    # Force a NOT NULL violation on a *different* column (provider) so the
    # reporter no longer has to guess which of a dozen NOT NULL columns failed.
    monkeypatch.setattr(
        context_writes,
        "_identity",
        lambda actor: {
            "role": "manager",
            "actor_id": "mgr-1",
            "task_id": None,
            "provider": None,
            "session_id": "s1",
        },
    )

    with pytest.raises(context_writes.ContextWriteError) as excinfo:
        context_writes.memory_write(
            tmp_path,
            actor={"role": "manager"},
            action="remember",
            key="k",
            value="v",
            idempotency_key="idem-integrity-0001",
            provenance="prov",
        )

    message = str(excinfo.value)
    assert "provider" in message           # the exact offending column
    assert "component=memory" in message   # the component
    assert "action=remember" in message    # the action
    # It is not a raw sqlite IntegrityError leaking through.
    assert not isinstance(excinfo.value, sqlite3.IntegrityError)


def test_legacy_empty_string_store_migrates_to_null(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    con = sqlite3.connect(db)
    try:
        con.execute(
            "CREATE TABLE memories(id INTEGER PRIMARY KEY,key TEXT,value TEXT,tags TEXT,scope TEXT)"
        )
        # Legacy schema: task_id declared NOT NULL, task-less write stored as ''.
        con.execute(
            "CREATE TABLE context_mutations("
            "id INTEGER PRIMARY KEY,idempotency_key TEXT UNIQUE NOT NULL,component TEXT NOT NULL,"
            "action TEXT NOT NULL,entity_key TEXT NOT NULL,actor_role TEXT NOT NULL,"
            "actor_id TEXT NOT NULL,task_id TEXT NOT NULL,provider TEXT NOT NULL,"
            "session_id TEXT NOT NULL,provenance TEXT NOT NULL,payload_sha256 TEXT NOT NULL,"
            "created_at TEXT NOT NULL)"
        )
        con.execute(
            "INSERT INTO context_mutations(idempotency_key,component,action,entity_key,actor_role,"
            "actor_id,task_id,provider,session_id,provenance,payload_sha256,created_at) VALUES("
            "'legacy-key-0001','memory','remember','memory:1','manager','mgr','','claude','s0',"
            "'prov','deadbeef','2020-01-01T00:00:00+00:00')"
        )
        con.commit()
    finally:
        con.close()

    _point_registry_at(monkeypatch, db)

    # A task-less write on the legacy store must now succeed rather than raise
    # SQLITE_CONSTRAINT_NOTNULL: the schema is migrated to a nullable task_id.
    context_writes.memory_write(
        tmp_path,
        actor={"role": "manager", "actor_id": "mgr-2", "provider": "claude", "session_id": "s1"},
        action="remember",
        key="fresh-key",
        value="v",
        idempotency_key="idem-legacy-0002",
        provenance="prov",
    )

    info = {row[1]: bool(row[3]) for row in _open_info(db)}
    assert info["task_id"] is False

    legacy = _read(
        db, "SELECT task_id FROM context_mutations WHERE idempotency_key='legacy-key-0001'"
    )
    fresh = _read(
        db, "SELECT task_id FROM context_mutations WHERE idempotency_key='idem-legacy-0002'"
    )
    # Historical '' is normalised to NULL; the new task-less write is NULL too.
    assert legacy[0]["task_id"] is None
    assert fresh[0]["task_id"] is None


def _open_info(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA table_info(context_mutations)").fetchall()
    finally:
        con.close()


def _seed_legacy_context_mutations(path, task_ids):
    """Create a legacy ``context_mutations`` (task_id NOT NULL) with ``task_ids`` rows."""
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE context_mutations("
            "id INTEGER PRIMARY KEY,idempotency_key TEXT UNIQUE NOT NULL,component TEXT NOT NULL,"
            "action TEXT NOT NULL,entity_key TEXT NOT NULL,actor_role TEXT NOT NULL,"
            "actor_id TEXT NOT NULL,task_id TEXT NOT NULL,provider TEXT NOT NULL,"
            "session_id TEXT NOT NULL,provenance TEXT NOT NULL,payload_sha256 TEXT NOT NULL,"
            "created_at TEXT NOT NULL)"
        )
        for i, task_id in enumerate(task_ids):
            con.execute(
                "INSERT INTO context_mutations(idempotency_key,component,action,entity_key,actor_role,"
                "actor_id,task_id,provider,session_id,provenance,payload_sha256,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"legacy-{i:04d}", "memory", "remember", f"memory:{i}", "manager", "mgr",
                 task_id, "claude", "s0", "prov", "deadbeef", "2020-01-01T00:00:00+00:00"),
            )
        con.commit()
    finally:
        con.close()


class _SabotageConn:
    """Proxy that lets a chosen migration statement fail, to test atomicity.

    ``skip_substr`` silently no-ops the first matching statement (forcing the
    copied-row-count guard to fire); ``raise_substr`` raises a genuine sqlite
    error part-way through.  Everything else -- including ``commit``/``rollback``
    -- forwards to the real connection.
    """

    def __init__(self, real, *, skip_substr=None, raise_substr=None):
        self._real = real
        self._skip = skip_substr
        self._raise = raise_substr

    def execute(self, sql, *args):
        if self._raise is not None and self._raise in sql:
            raise sqlite3.OperationalError("injected mid-migration failure")
        if self._skip is not None and self._skip in sql:
            return self._real.execute("SELECT 1 WHERE 0")
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _assert_original_intact_and_complete(path, expected_task_ids):
    """The audit table must survive a failed migration untouched and complete."""
    con = sqlite3.connect(path)
    try:
        con.row_factory = sqlite3.Row
        # The legacy scratch table left by a partial rebuild must be gone.
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_mutations_legacy_notnull'"
        ).fetchone() is None
        rows = con.execute(
            "SELECT task_id FROM context_mutations ORDER BY id"
        ).fetchall()
        # Every original row is still present with its exact value -- no audit
        # evidence stranded or lost.
        assert [r["task_id"] for r in rows] == list(expected_task_ids)
        # The schema is unchanged too: task_id is still the legacy NOT NULL.
        info = {r[1]: bool(r[3]) for r in con.execute("PRAGMA table_info(context_mutations)")}
        assert info["task_id"] is True
    finally:
        con.close()


def test_migration_rolls_back_on_row_count_mismatch(tmp_path):
    # The audit-table rebuild is atomic: if the copied count does not match the
    # source count, the whole transaction rolls back and the original survives.
    db = tmp_path / "memory.db"
    _seed_legacy_context_mutations(db, task_ids=["", "T-1", "T-2"])

    real = sqlite3.connect(db)
    real.row_factory = sqlite3.Row
    real.execute("PRAGMA busy_timeout=5000")
    # Skip the copy so the new table ends up empty and the count guard fires.
    proxy = _SabotageConn(real, skip_substr="INSERT INTO context_mutations(id,")
    try:
        with pytest.raises(context_writes.ContextWriteError) as excinfo:
            context_writes._normalize_context_mutations_schema(proxy)
        assert "row_count_mismatch" in str(excinfo.value)
    finally:
        real.close()

    _assert_original_intact_and_complete(db, ["", "T-1", "T-2"])


def test_migration_rolls_back_on_midway_exception(tmp_path):
    # A genuine failure part-way through (here: the final DROP) must also roll
    # back, leaving the original audit table intact and complete.
    db = tmp_path / "memory.db"
    _seed_legacy_context_mutations(db, task_ids=["", "T-9"])

    real = sqlite3.connect(db)
    real.row_factory = sqlite3.Row
    real.execute("PRAGMA busy_timeout=5000")
    proxy = _SabotageConn(real, raise_substr="DROP TABLE context_mutations_legacy_notnull")
    try:
        with pytest.raises(sqlite3.OperationalError):
            context_writes._normalize_context_mutations_schema(proxy)
    finally:
        real.close()

    _assert_original_intact_and_complete(db, ["", "T-9"])
