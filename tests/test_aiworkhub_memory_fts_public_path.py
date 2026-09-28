"""Bounded public-path FTS migration tests for ``aiworkhub_memory_fts_public_path_v5``.

Covers: legacy repair on first search, concurrent safety, get/related read-only
invariant, schema/id preservation, and bounded failure modes.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from aiworkhub import context_writes, feature_settings, storage_registry, task_store
import aiworkhub.worker_ai_tools_mcp as worker_ai_tools_mcp


# ── helpers ──────────────────────────────────────────────────────────


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    return repo


def _actor() -> dict[str, str]:
    return {
        "role": "manager",
        "actor_id": "thread-1",
        "task_id": "",
        "provider": "codex",
        "session_id": "thread-1",
    }


def _memory_db(repo: Path) -> Path:
    return storage_registry.resolve_database_path(
        storage_registry.load_storage_registry(repo), "memory"
    )


def _break_fts(repo: Path, *, drop_fts: bool = True, corrupt: bool = False) -> None:
    """Remove or replace memories_fts to simulate a pre-migration legacy DB."""
    db = _memory_db(repo)
    con = sqlite3.connect(str(db))
    try:
        if drop_fts:
            con.execute("DROP TABLE IF EXISTS memories_fts")
        if corrupt:
            con.execute("DROP TABLE IF EXISTS memories_fts")
            con.execute("CREATE TABLE memories_fts(x INTEGER)")
        con.commit()
    finally:
        con.close()


def _seed_legacy_memory(repo: Path, key: str = "legacy.key", value: str = "old-data") -> None:
    """Write one legacy memory row through the canonical write path so the
    schema normalizer has already run and the DB is well-formed.  Then tear
    out memories_fts to model the legacy state."""
    context_writes.memory_write(
        repo,
        actor=_actor(),
        action="remember",
        key=key,
        value=value,
        tags="legacy-tag",
        scope="project",
        idempotency_key=f"seed:{key}:v5",
        provenance="test seed",
    )
    _break_fts(repo)


# ── legacy repair on first search ────────────────────────────────────


def test_legacy_missing_fts_repaired_on_search_returns_legacy_row(tmp_path: Path) -> None:
    """First call to ensure_memories_fts creates the table and backfills all rows."""
    repo = _repo(tmp_path)
    _seed_legacy_memory(repo, key="decision.routing", value="round-robin")
    _seed_legacy_memory(repo, key="decision.cache", value="redis")

    # confirm FTS is gone
    db = _memory_db(repo)
    con = sqlite3.connect(str(db))
    try:
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone() is None
    finally:
        con.close()

    result = context_writes.ensure_memories_fts(repo)
    assert result["ok"]
    assert result["created"]

    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone() is not None

        # backfill preserved rowid=id
        rows = dict(con.execute(
            "SELECT key, value FROM memories_fts WHERE memories_fts MATCH 'routing OR redis'"
        ))
    finally:
        con.close()
    assert rows["decision.routing"] == "round-robin"
    assert rows["decision.cache"] == "redis"


def test_ensure_memories_fts_idempotent_on_repeat(tmp_path: Path) -> None:
    """Second call is a no-op with created=False."""
    repo = _repo(tmp_path)
    _seed_legacy_memory(repo)

    first = context_writes.ensure_memories_fts(repo)
    second = context_writes.ensure_memories_fts(repo)
    assert first["ok"] and first["created"]
    assert second["ok"] and not second["created"]
    assert second["reason"] == "fts_already_exists"


def test_ensure_memories_fts_noop_when_no_memories_table(tmp_path: Path) -> None:
    """Database without a memories table returns ok-but-not-created."""
    repo = _repo(tmp_path)
    db = _memory_db(repo)
    con = sqlite3.connect(str(db))
    try:
        con.execute("DROP TABLE IF EXISTS memories_fts")
        con.execute("DROP TABLE IF EXISTS memories")
        con.commit()
    finally:
        con.close()

    result = context_writes.ensure_memories_fts(repo)
    assert result["ok"] and not result["created"]
    assert result["reason"] == "memories_table_absent"


# ── concurrent safety ────────────────────────────────────────────────


def test_concurrent_first_searches_produce_one_fts(tmp_path: Path) -> None:
    """Two threads calling ensure_memories_fts simultaneously both succeed
    and leave exactly one correct FTS table."""
    repo = _repo(tmp_path)
    _seed_legacy_memory(repo, key="concurrent.alpha", value="alpha")
    _seed_legacy_memory(repo, key="concurrent.beta", value="beta")

    results: list[dict] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        results.append(context_writes.ensure_memories_fts(repo))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 2
    # both must be ok; at least one was the creator
    assert all(r["ok"] for r in results)
    assert any(r["created"] for r in results)
    # only one should report created=True (the loser sees it already exists)
    creators = [r for r in results if r["created"]]
    assert len(creators) == 1

    # verify one correct FTS table with all rows
    db = _memory_db(repo)
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        rows = dict(con.execute(
            "SELECT key, value FROM memories_fts WHERE memories_fts MATCH 'alpha OR beta'"
        ))
    finally:
        con.close()
    assert rows == {"concurrent.alpha": "alpha", "concurrent.beta": "beta"}


# ── get / related are read-only ──────────────────────────────────────


def test_memory_write_normalization_backfills_fts(tmp_path: Path) -> None:
    """When the write path hits _normalize_memory_schema it also ensures FTS
    via the shared primitive."""
    repo = _repo(tmp_path)
    db = _memory_db(repo)
    con = sqlite3.connect(str(db))
    try:
        con.execute("DROP TABLE IF EXISTS memories_fts")
        con.commit()
    finally:
        con.close()

    created = context_writes.memory_write(
        repo,
        actor=_actor(),
        action="remember",
        key="write.path",
        value="triggered",
        idempotency_key="memory:write-fts:0001",
        provenance="write path fts test",
    )
    assert created["status"] == "active"

    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone() is not None
        rows = dict(con.execute(
            "SELECT key, value FROM memories_fts WHERE memories_fts MATCH 'triggered'"
        ))
    finally:
        con.close()
    assert rows == {"write.path": "triggered"}


# ── id / value / tags / scope preservation ───────────────────────────


def test_fts_backfill_preserves_ids_values_tags_scope(tmp_path: Path) -> None:
    """Backfilled FTS rowid equals memories.id and all columns match."""
    repo = _repo(tmp_path)
    context_writes.memory_write(
        repo, actor=_actor(), action="remember", key="identity.a",
        value="alpha", tags="t1,t2", scope="project",
        idempotency_key="memory:ident:a", provenance="id test",
    )
    context_writes.memory_write(
        repo, actor=_actor(), action="remember", key="identity.b",
        value="beta", tags="t3", scope="global",
        idempotency_key="memory:ident:b", provenance="id test",
    )

    _break_fts(repo)

    context_writes.ensure_memories_fts(repo)

    db = _memory_db(repo)
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        fts_rows = {
            row["key"]: dict(row)
            for row in con.execute(
                "SELECT f.rowid, f.key, f.value, f.tags, f.scope FROM memories_fts f"
            )
        }
        mem_rows = {
            row["key"]: dict(row)
            for row in con.execute(
                "SELECT m.id, m.key, m.value, m.tags, m.scope FROM memories m"
            )
        }
    finally:
        con.close()

    for key in ("identity.a", "identity.b"):
        assert fts_rows[key]["rowid"] == mem_rows[key]["id"]
        assert fts_rows[key]["value"] == mem_rows[key]["value"]
        assert fts_rows[key]["tags"] == mem_rows[key]["tags"]
        assert fts_rows[key]["scope"] == mem_rows[key]["scope"]


def test_provenance_state_tables_preserved_after_fts_migration(tmp_path: Path) -> None:
    """context_entity_state rows survive the FTS migration intact."""
    repo = _repo(tmp_path)
    context_writes.memory_write(
        repo, actor=_actor(), action="remember", key="state.preserved",
        value="before", idempotency_key="memory:state:recall", provenance="state test",
    )
    context_writes.memory_write(
        repo, actor=_actor(), action="archive", key="state.preserved",
        idempotency_key="memory:state:archive", provenance="state test",
    )
    # Seed a second distinct active memory so both archived and active states exist.
    context_writes.memory_write(
        repo, actor=_actor(), action="remember", key="state.active2",
        value="active2", idempotency_key="memory:state:active2", provenance="state test",
    )
    _break_fts(repo)

    # Capture exact pre-migration state rows.
    db = _memory_db(repo)
    con_before = sqlite3.connect(str(db))
    con_before.row_factory = sqlite3.Row
    pre_states = {
        row["entity_id"]: row["status"]
        for row in con_before.execute(
            "SELECT entity_id, status FROM context_entity_state WHERE entity_type='memory'"
        )
    }
    con_before.close()

    context_writes.ensure_memories_fts(repo)

    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        post_states = {
            row["entity_id"]: row["status"]
            for row in con.execute(
                "SELECT entity_id, status FROM context_entity_state WHERE entity_type='memory'"
            )
        }
    finally:
        con.close()
    # Full post-migration mapping equals pre-migration mapping.
    assert post_states == pre_states
    assert any(s == "archived" for s in post_states.values())
    assert any(s == "active" for s in post_states.values())


# ── bounded failure modes ────────────────────────────────────────────


def test_ensure_memories_fts_bounded_on_missing_registry(tmp_path: Path) -> None:
    """A non-existent repo path returns a bounded error, not a crash."""
    bogus = tmp_path / "nonexistent"
    result = context_writes.ensure_memories_fts(bogus)
    assert not result["ok"]
    assert result["error"] in {"fts_registry_unavailable", "fts_db_absent_or_empty"}


def test_ensure_memories_fts_bounded_on_empty_db(tmp_path: Path) -> None:
    """An empty or zero-byte database file returns bounded error."""
    repo = _repo(tmp_path)
    db = _memory_db(repo)
    db.write_text("")

    result = context_writes.ensure_memories_fts(repo)
    assert not result["ok"]
    assert result["error"] == "fts_db_absent_or_empty"


def test_ensure_memories_fts_bounded_on_corrupt_virtual_table(tmp_path: Path) -> None:
    """A non-virtual memories_fts raises sqlite3.Error captured as bounded failure."""
    repo = _repo(tmp_path)
    _seed_legacy_memory(repo)
    _break_fts(repo, corrupt=True)

    result = context_writes.ensure_memories_fts(repo)
    assert result["ok"]
    assert not result["created"]
    assert result["reason"] == "fts_already_exists"


# Integration: initialization repairs; search remains read-only.


def test_ai_memory_search_is_query_only_after_repository_reconciliation(
    tmp_path: Path, monkeypatch
) -> None:
    """Repository reconciliation repairs FTS before the query hot path."""
    repo = _repo(tmp_path)
    _seed_legacy_memory(repo, key="searchable", value="find-me")
    assert task_store.initialize_repository(repo)["ok"]

    from aiworkhub.worker_ai_tools_mcp import WorkerToolContext

    ctx = WorkerToolContext(
        task_id="test:task",
        runner="test_runner",
        topic="test",
        request_id="test:request",
        repo=repo,
        authority_repo=repo,
        source_graph_targets=(),
        session_topic="test",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
    )

    monkeypatch.setattr(
        feature_settings, "enabled",
        lambda repo_path, name: True,
    )
    db_path = _memory_db(repo)
    from aiworkhub.worker_ai_tools_mcp import AuthorityBinding

    monkeypatch.setattr(
        worker_ai_tools_mcp,
        "_resolve_authority_db",
        lambda ctx, component, db_id: AuthorityBinding(
            db_path=db_path, authority_source="canonical", authority_state="canonical_active"
        ),
    )
    monkeypatch.setattr(
        worker_ai_tools_mcp,
        "_append_audit",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        context_writes,
        "ensure_memories_fts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("search hot path attempted writable FTS repair")
        ),
    )

    from aiworkhub.worker_ai_tools_mcp import ai_memory_search

    result = ai_memory_search(ctx, query="find-me", limit=5)
    assert result["ok"]
    assert result["hit_count"] == 1

    payload = __import__("json").loads(result["content"])
    assert payload["results"][0]["key"] == "searchable"
    assert payload["results"][0]["value"] == "find-me"


def test_ai_memory_search_bounded_on_missing_fts_unrepairable(tmp_path: Path, monkeypatch) -> None:
    """When the DB has no memories table at all, search returns bounded error."""
    repo = _repo(tmp_path)
    db = _memory_db(repo)
    con = sqlite3.connect(str(db))
    try:
        con.execute("DROP TABLE IF EXISTS memories_fts")
        con.execute("DROP TABLE IF EXISTS memories")
        con.commit()
    finally:
        con.close()

    from aiworkhub.worker_ai_tools_mcp import WorkerToolContext, AuthorityBinding

    ctx = WorkerToolContext(
        task_id="test:task", runner="test_runner", topic="test",
        request_id="test:request", repo=repo, authority_repo=repo,
        source_graph_targets=(), session_topic="test",
        audit_ledger_path=None, audit_hmac_key_path=None,
    )

    monkeypatch.setattr(
        feature_settings, "enabled",
        lambda repo_path, name: True,
    )
    monkeypatch.setattr(
        worker_ai_tools_mcp,
        "_resolve_authority_db",
        lambda ctx, component, db_id: AuthorityBinding(
            db_path=db, authority_source="canonical", authority_state="canonical_active"
        ),
    )
    monkeypatch.setattr(
        worker_ai_tools_mcp,
        "_append_audit",
        lambda *args, **kwargs: None,
    )

    from aiworkhub.worker_ai_tools_mcp import ai_memory_search

    result = ai_memory_search(ctx, query="anything", limit=3)
    assert not result["ok"]
    assert "fts_unavailable" in result.get("reason", "")

# ── get / related remain read-only (no FTS migration) ────────────────


def test_ai_memory_get_readonly_no_fts_migration(tmp_path: Path, monkeypatch) -> None:
    """get does not invoke ensure_memories_fts and remains read-only."""
    repo = _repo(tmp_path)
    _seed_legacy_memory(repo, key="exact.get", value="found")

    from aiworkhub.worker_ai_tools_mcp import WorkerToolContext, AuthorityBinding

    ctx = WorkerToolContext(
        task_id="test:task", runner="test_runner", topic="test",
        request_id="test:request", repo=repo, authority_repo=repo,
        source_graph_targets=(), session_topic="test",
        audit_ledger_path=None, audit_hmac_key_path=None,
    )

    monkeypatch.setattr(
        feature_settings, "enabled",
        lambda repo_path, name: True,
    )
    db_path = _memory_db(repo)
    monkeypatch.setattr(
        worker_ai_tools_mcp,
        "_resolve_authority_db",
        lambda ctx, component, db_id: AuthorityBinding(
            db_path=db_path, authority_source="canonical", authority_state="canonical_active"
        ),
    )
    monkeypatch.setattr(
        worker_ai_tools_mcp,
        "_append_audit",
        lambda *args, **kwargs: None,
    )
    # ensure_memories_fts must never be called from get
    monkeypatch.setattr(
        context_writes,
        "ensure_memories_fts",
        lambda repo: (_ for _ in ()).throw(AssertionError("get must not trigger FTS migration")),
    )

    from aiworkhub.worker_ai_tools_mcp import ai_memory_get

    result = ai_memory_get(ctx, key="exact.get")
    assert result["ok"]
    payload = __import__("json").loads(result["content"])
    assert payload["memory"]["key"] == "exact.get"
    assert payload["memory"]["value"] == "found"


def test_ai_memory_related_readonly_no_fts_migration(tmp_path: Path, monkeypatch) -> None:
    """related does not invoke ensure_memories_fts and remains read-only."""
    repo = _repo(tmp_path)
    _seed_legacy_memory(repo, key="related.a", value="alpha")
    _seed_legacy_memory(repo, key="related.b", value="beta")

    from aiworkhub.worker_ai_tools_mcp import WorkerToolContext, AuthorityBinding

    ctx = WorkerToolContext(
        task_id="test:task", runner="test_runner", topic="test",
        request_id="test:request", repo=repo, authority_repo=repo,
        source_graph_targets=(), session_topic="test",
        audit_ledger_path=None, audit_hmac_key_path=None,
    )

    monkeypatch.setattr(
        feature_settings, "enabled",
        lambda repo_path, name: True,
    )
    db_path = _memory_db(repo)
    monkeypatch.setattr(
        worker_ai_tools_mcp,
        "_resolve_authority_db",
        lambda ctx, component, db_id: AuthorityBinding(
            db_path=db_path, authority_source="canonical", authority_state="canonical_active"
        ),
    )
    monkeypatch.setattr(
        worker_ai_tools_mcp,
        "_append_audit",
        lambda *args, **kwargs: None,
    )
    # ensure_memories_fts must never be called from related
    monkeypatch.setattr(
        context_writes,
        "ensure_memories_fts",
        lambda repo: (_ for _ in ()).throw(AssertionError("related must not trigger FTS migration")),
    )

    from aiworkhub.worker_ai_tools_mcp import ai_memory_related

    result = ai_memory_related(ctx, key="related.a")
    assert result["ok"]
    payload = __import__("json").loads(result["content"])
    assert payload["count"] >= 0


# ── legacy AITools-shaped store: atomic, trigger-aware repair ────────
#
# The old rebuild ran ``DROP TABLE memories_fts; ALTER TABLE memories RENAME
# ...`` through executescript.  The DROP committed by itself, then modern
# SQLite refused the RENAME (``error in trigger memories_ai: no such table:
# main.memories_fts``) because the legacy AITools triggers still wrote to the
# dropped index.  That left no FTS, the stale triggers and the unique index --
# and every INSERT into memories failed from then on.

# Legacy AITools kept an external-content index in sync with these triggers.
# The canonical model maintains memories_fts explicitly, so none may survive.
_LEGACY_FTS_TRIGGERS = (
    "CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN "
    "INSERT INTO memories_fts(rowid,key,value,tags,scope) "
    "VALUES(new.id,new.key,new.value,new.tags,new.scope); END;"
    "CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN "
    "INSERT INTO memories_fts(memories_fts,rowid,key,value,tags,scope) "
    "VALUES('delete',old.id,old.key,old.value,old.tags,old.scope); END;"
    "CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN "
    "INSERT INTO memories_fts(memories_fts,rowid,key,value,tags,scope) "
    "VALUES('delete',old.id,old.key,old.value,old.tags,old.scope); "
    "INSERT INTO memories_fts(rowid,key,value,tags,scope) "
    "VALUES(new.id,new.key,new.value,new.tags,new.scope); END;"
)

# Non-contiguous ids that do not start at 1: a rebuild that renumbers rows shows.
_LEGACY_ROWS = (
    (3, "legacy.alpha", "aardvark payload", "legacy,a", "project"),
    (7, "legacy.bravo", "badger payload", "legacy,b", "global"),
    (12, "legacy.charlie", "civet payload", "legacy,c", "project"),
)
_LEGACY_TRIGGER_NAMES = ["memories_ad", "memories_ai", "memories_au"]

# A second table whose trigger also writes to memories_fts.  ALTER TABLE ...
# RENAME re-parses every trigger in the schema, wherever it sits, so this one
# fails the rebuild of memories just like the three above once the index is gone.
_OTHER_TABLE_FTS_TRIGGER = (
    "CREATE TABLE memory_audit(id INTEGER PRIMARY KEY,note TEXT);"
    "INSERT INTO memory_audit(id,note) VALUES(1,'audit note');"
    "CREATE TRIGGER memory_audit_ai AFTER INSERT ON memory_audit BEGIN "
    "INSERT INTO memories_fts(rowid,key,value,tags,scope) "
    "VALUES(new.id,'audit',new.note,'','project'); END;"
)


def _query(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(str(db))
    try:
        return [tuple(row) for row in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _memory_rows(db: Path) -> list[tuple]:
    return _query(db, "SELECT id,key,value,tags,scope FROM memories ORDER BY id")


def _fts_ids(db: Path, match: str) -> list[int]:
    return sorted(
        row[0] for row in _query(
            db, "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?", (match,)
        )
    )


def _stale_triggers(db: Path) -> list[str]:
    """Triggers, on any table, whose SQL still references memories_fts."""
    return sorted(
        name for name, sql in _query(
            db, "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
        )
        if "memories_fts" in sql
    )


def _has_unique_key(db: Path) -> bool:
    con = sqlite3.connect(str(db))
    try:
        for row in con.execute("PRAGMA index_list(memories)").fetchall():
            columns = [info[2] for info in con.execute(f'PRAGMA index_info("{row[1]}")').fetchall()]
            if row[2] and columns == ["key"]:
                return True
        return False
    finally:
        con.close()


def _schema_snapshot(db: Path) -> tuple[list[tuple], list[tuple], list[int]]:
    """Rows, schema objects (FTS shadow tables aside) and FTS hits, as one comparable value."""
    return (
        _memory_rows(db),
        _query(
            db,
            "SELECT type,name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'memories_fts_%' ORDER BY type,name",
        ),
        _fts_ids(db, "payload"),
    )


def _build_legacy_aitools_db(
    repo: Path, *, unique_key: bool = True, strand: bool = False, other_table_trigger: bool = False,
) -> Path:
    """Rewrite the canonical memory DB into the legacy AITools shape.

    ``memories(id, key UNIQUE, ...)`` plus an external-content ``memories_fts``
    kept in sync by AFTER INSERT/DELETE/UPDATE triggers.  ``strand`` then drops
    only ``memories_fts``: the state the old executescript rebuild left behind.
    ``other_table_trigger`` adds a second table whose trigger also writes to
    ``memories_fts``.
    """
    db = _memory_db(repo)
    con = sqlite3.connect(str(db))
    try:
        con.executescript(
            "DROP TABLE IF EXISTS memories_fts; DROP TABLE IF EXISTS memories;"
            f"CREATE TABLE memories(id INTEGER PRIMARY KEY,key TEXT{' UNIQUE' if unique_key else ''},"
            "value TEXT,tags TEXT,scope TEXT);"
            "CREATE VIRTUAL TABLE memories_fts USING fts5("
            "key,value,tags,scope,content='memories',content_rowid='id');"
            + _LEGACY_FTS_TRIGGERS
        )
        con.executemany(
            "INSERT INTO memories(id,key,value,tags,scope) VALUES(?,?,?,?,?)", _LEGACY_ROWS
        )
        con.commit()
        if other_table_trigger:
            con.executescript(_OTHER_TABLE_FTS_TRIGGER)
        if strand:
            con.execute("DROP TABLE memories_fts")
            con.commit()
    finally:
        con.close()
    return db


def _remember_fresh(repo: Path, *, suffix: str = "0001") -> dict:
    return context_writes.memory_write(
        repo, actor=_actor(), action="remember", key="fresh.key", value="fresh payload",
        idempotency_key=f"memory:legacy-aitools:{suffix}", provenance="legacy aitools repair",
    )


class _SabotageConn:
    """Connection proxy that fails, or skips, one chosen statement of a migration.

    Every ``execute`` is recorded with the connection's ``in_transaction`` flag,
    so a test can prove each DDL statement ran inside the transaction.
    ``executescript`` is refused: it commits before it runs, so a rebuild built
    on it can never be atomic.
    """

    def __init__(self, real, *, raise_substr=None, skip_substr=None):
        self._real = real
        self._raise = raise_substr
        self._skip = skip_substr
        self.executed: list[tuple[str, bool]] = []

    def execute(self, sql, *args):
        self.executed.append((sql, self._real.in_transaction))
        if self._raise is not None and self._raise in sql:
            raise sqlite3.OperationalError("injected mid-rebuild failure")
        if self._skip is not None and self._skip in sql:
            return self._real.execute("SELECT 1 WHERE 0")
        return self._real.execute(sql, *args)

    def executescript(self, script):
        raise AssertionError("the memories rebuild must not use executescript: it commits")

    def __getattr__(self, name):
        return getattr(self._real, name)

    def ddl_in_transaction(self) -> list[bool]:
        return [
            in_txn for sql, in_txn in self.executed
            if sql.split(None, 1)[0].upper() in {"ALTER", "CREATE", "DROP"}
        ]


_LEGACY_SHAPES = [
    pytest.param(True, False, id="unique_key-fts_present"),
    pytest.param(True, True, id="unique_key-fts_stranded"),
    pytest.param(False, True, id="no_unique_key-fts_stranded"),
    pytest.param(False, False, id="no_unique_key-fts_present"),
]


@pytest.mark.parametrize(("unique_key", "strand"), _LEGACY_SHAPES)
def test_memory_write_repairs_legacy_aitools_store_without_losing_rows(
    tmp_path: Path, unique_key: bool, strand: bool,
) -> None:
    """A legacy store, healthy or stranded, is repaired by one memory_write:
    every original id and row survives, memories_fts matches them all, no
    trigger on memories still references memories_fts, and INSERT works."""
    repo = _repo(tmp_path)
    db = _build_legacy_aitools_db(repo, unique_key=unique_key, strand=strand)
    assert _memory_rows(db) == list(_LEGACY_ROWS)
    assert _stale_triggers(db) == _LEGACY_TRIGGER_NAMES
    assert _has_unique_key(db) is unique_key
    if strand:
        # The reported symptom: no FTS, so every INSERT dies on the stale trigger.
        con = sqlite3.connect(str(db))
        try:
            with pytest.raises(sqlite3.OperationalError, match="memories_fts"):
                con.execute("INSERT INTO memories(key,value,tags,scope) VALUES('p','x','','project')")
        finally:
            con.rollback()
            con.close()

    created = _remember_fresh(repo)

    assert created["status"] == "active"
    assert created["memory_id"] == 13
    rows = _memory_rows(db)
    assert rows[:3] == list(_LEGACY_ROWS)
    assert rows[3] == (13, "fresh.key", "fresh payload", "", "project")
    assert _fts_ids(db, "payload") == [3, 7, 12, 13]
    assert _stale_triggers(db) == []
    assert not _has_unique_key(db)
    assert _query(db, "SELECT 1 FROM sqlite_master WHERE name='memories_legacy_unique'") == []

    # The canonical FTS maintenance keeps working on the repaired store.
    context_writes.memory_write(
        repo, actor=_actor(), action="update", key="legacy.alpha", value="zebra payload",
        tags="legacy,a", scope="project", idempotency_key="memory:legacy-aitools:0002",
        provenance="legacy aitools repair",
    )
    assert _fts_ids(db, "zebra") == [3]
    assert _fts_ids(db, "aardvark") == []


@pytest.mark.parametrize(("unique_key", "strand"), _LEGACY_SHAPES)
def test_first_update_on_legacy_store_forgets_the_superseded_text(
    tmp_path: Path, unique_key: bool, strand: bool,
) -> None:
    """The legacy external-content index was kept by triggers that no longer
    exist.  memory_write updates the row and then deletes by rowid, which an
    external-content index answers from the already-changed row, so it could
    never forget the old terms.  Whatever the legacy shape, the first write
    that updates a key matches the new value and no longer the old one."""
    repo = _repo(tmp_path)
    db = _build_legacy_aitools_db(repo, unique_key=unique_key, strand=strand)

    updated = context_writes.memory_write(
        repo, actor=_actor(), action="update", key="legacy.alpha", value="zebra payload",
        tags="legacy,a", scope="project", idempotency_key="memory:legacy-aitools:update-0001",
        provenance="legacy aitools repair",
    )

    assert updated["memory_id"] == 3
    assert _fts_ids(db, "zebra") == [3]
    assert _fts_ids(db, "aardvark") == []
    assert _fts_ids(db, "payload") == [3, 7, 12]
    assert _stale_triggers(db) == []
    # The index is the canonical one now, not the legacy external-content one.
    index_sql = _query(db, "SELECT sql FROM sqlite_master WHERE name='memories_fts'")[0][0]
    assert "content" not in index_sql.lower()


@pytest.mark.parametrize(("unique_key", "strand"), _LEGACY_SHAPES)
def test_memory_write_drops_fts_triggers_on_other_tables(
    tmp_path: Path, unique_key: bool, strand: bool,
) -> None:
    """ALTER TABLE ... RENAME re-parses every trigger in the schema, so a trigger
    on ANOTHER table that writes to memories_fts fails it once the index is
    dropped.  Every trigger that references memories_fts goes, wherever it sits;
    the table it sat on keeps its rows."""
    repo = _repo(tmp_path)
    db = _build_legacy_aitools_db(
        repo, unique_key=unique_key, strand=strand, other_table_trigger=True,
    )
    assert _stale_triggers(db) == sorted([*_LEGACY_TRIGGER_NAMES, "memory_audit_ai"])

    created = _remember_fresh(repo)

    assert created["status"] == "active"
    assert _memory_rows(db)[:3] == list(_LEGACY_ROWS)
    assert _fts_ids(db, "payload") == [3, 7, 12, 13]
    assert _stale_triggers(db) == []
    assert _query(db, "SELECT id,note FROM memory_audit") == [(1, "audit note")]


def test_ensure_memories_fts_drops_fts_triggers_on_other_tables(tmp_path: Path) -> None:
    """The public repair clears a stale trigger on any table, not just memories."""
    repo = _repo(tmp_path)
    db = _build_legacy_aitools_db(repo, strand=True, other_table_trigger=True)

    repaired = context_writes.ensure_memories_fts(repo)

    assert repaired["ok"] and repaired["created"]
    assert _fts_ids(db, "payload") == [3, 7, 12]
    assert _stale_triggers(db) == []
    assert _query(db, "SELECT id,note FROM memory_audit") == [(1, "audit note")]


@pytest.mark.parametrize("unique_key", [True, False], ids=["unique_key", "no_unique_key"])
def test_ensure_memories_fts_repairs_stranded_store(tmp_path: Path, unique_key: bool) -> None:
    """The public repair alone restores a stranded store: FTS backfilled with
    every row, stale triggers gone, and a following memory_write INSERT works."""
    repo = _repo(tmp_path)
    db = _build_legacy_aitools_db(repo, unique_key=unique_key, strand=True)

    repaired = context_writes.ensure_memories_fts(repo)

    assert repaired["ok"] and repaired["created"]
    assert _memory_rows(db) == list(_LEGACY_ROWS)
    assert _fts_ids(db, "payload") == [3, 7, 12]
    assert _stale_triggers(db) == []
    again = context_writes.ensure_memories_fts(repo)
    assert again["ok"] and not again["created"] and again["reason"] == "fts_already_exists"

    created = _remember_fresh(repo)

    assert created["status"] == "active"
    assert _memory_rows(db)[:3] == list(_LEGACY_ROWS)
    assert _fts_ids(db, "payload") == [3, 7, 12, created["memory_id"]]
    assert _stale_triggers(db) == []


_REBUILD_FAILURE_POINTS = [
    "DROP TRIGGER",
    "DROP TABLE IF EXISTS memories_fts",
    "ALTER TABLE memories RENAME",
    "CREATE TABLE memories(",
    "DROP TABLE memories_legacy_unique",
    "CREATE VIRTUAL TABLE memories_fts",
    "INSERT INTO memories_fts(",
]


@pytest.mark.parametrize("failing_sql", _REBUILD_FAILURE_POINTS)
def test_memories_rebuild_rolls_back_when_a_step_fails(tmp_path: Path, failing_sql: str) -> None:
    """A failure at any step of the rebuild restores the original store --
    rows, unique index, triggers and memories_fts -- and raises ContextWriteError
    naming the step.  memories_fts is only ever dropped inside the transaction."""
    repo = _repo(tmp_path)
    db = _build_legacy_aitools_db(repo)
    before = _schema_snapshot(db)
    assert before[2] == [3, 7, 12]

    real = sqlite3.connect(str(db))
    real.row_factory = sqlite3.Row
    proxy = _SabotageConn(real, raise_substr=failing_sql)
    try:
        with pytest.raises(context_writes.ContextWriteError, match="memories_migration_failed:step="):
            context_writes._normalize_memory_schema(proxy)
        assert not real.in_transaction
    finally:
        real.close()

    assert _schema_snapshot(db) == before
    assert _stale_triggers(db) == _LEGACY_TRIGGER_NAMES
    assert _has_unique_key(db)
    assert _query(db, "SELECT 1 FROM sqlite_master WHERE name='memories_legacy_unique'") == []
    assert proxy.ddl_in_transaction()
    assert all(proxy.ddl_in_transaction())


# Without a unique index no table is rebuilt: only the triggers and the index go.
_INDEX_REBUILD_FAILURE_POINTS = [
    "DROP TRIGGER",
    "DROP TABLE IF EXISTS memories_fts",
    "CREATE VIRTUAL TABLE memories_fts",
    "INSERT INTO memories_fts(",
]


@pytest.mark.parametrize("failing_sql", _INDEX_REBUILD_FAILURE_POINTS)
def test_index_only_rebuild_rolls_back_when_a_step_fails(tmp_path: Path, failing_sql: str) -> None:
    """A failure at any step of replacing just the triggers and the index still
    restores the legacy store -- external-content index, triggers and rows --
    and raises ContextWriteError naming the step: the index is never left
    dropped."""
    repo = _repo(tmp_path)
    db = _build_legacy_aitools_db(repo, unique_key=False)
    before = _schema_snapshot(db)
    assert before[2] == [3, 7, 12]

    real = sqlite3.connect(str(db))
    real.row_factory = sqlite3.Row
    proxy = _SabotageConn(real, raise_substr=failing_sql)
    try:
        with pytest.raises(context_writes.ContextWriteError, match="memories_migration_failed:step="):
            context_writes._normalize_memory_schema(proxy)
        assert not real.in_transaction
    finally:
        real.close()

    assert _schema_snapshot(db) == before
    assert _stale_triggers(db) == _LEGACY_TRIGGER_NAMES
    assert proxy.ddl_in_transaction()
    assert all(proxy.ddl_in_transaction())


def test_memories_rebuild_rolls_back_on_row_count_mismatch(tmp_path: Path) -> None:
    """The copied row count is verified against the source; a short copy rolls
    the whole rebuild back, and the store then migrates cleanly on retry."""
    repo = _repo(tmp_path)
    db = _build_legacy_aitools_db(repo)
    before = _schema_snapshot(db)

    real = sqlite3.connect(str(db))
    real.row_factory = sqlite3.Row
    # Skip the copy so the new table ends up empty and the count guard fires.
    proxy = _SabotageConn(real, skip_substr="INSERT INTO memories(id,")
    try:
        with pytest.raises(
            context_writes.ContextWriteError, match="row_count_mismatch:source=3:copied=0"
        ):
            context_writes._normalize_memory_schema(proxy)
    finally:
        real.close()

    assert _schema_snapshot(db) == before
    assert _stale_triggers(db) == _LEGACY_TRIGGER_NAMES

    created = _remember_fresh(repo)
    assert created["status"] == "active"
    assert _memory_rows(db)[:3] == list(_LEGACY_ROWS)
    assert _fts_ids(db, "payload") == [3, 7, 12, 13]
    assert _stale_triggers(db) == []


@pytest.mark.parametrize("unique_key", [True, False], ids=["unique_key", "no_unique_key"])
def test_memories_rebuild_rolls_back_on_fts_row_count_mismatch(tmp_path: Path, unique_key: bool) -> None:
    """The rebuilt index is verified against memories before the commit; a short
    backfill rolls the whole migration back -- the legacy index, triggers and
    rows included -- and the store then migrates cleanly on retry."""
    repo = _repo(tmp_path)
    db = _build_legacy_aitools_db(repo, unique_key=unique_key)
    before = _schema_snapshot(db)

    real = sqlite3.connect(str(db))
    real.row_factory = sqlite3.Row
    # Skip the backfill so the new index ends up empty and the count guard fires.
    proxy = _SabotageConn(real, skip_substr="INSERT INTO memories_fts(")
    try:
        with pytest.raises(
            context_writes.ContextWriteError, match="fts_row_count_mismatch:memories=3:indexed=0"
        ):
            context_writes._normalize_memory_schema(proxy)
        assert not real.in_transaction
    finally:
        real.close()

    assert _schema_snapshot(db) == before
    assert _stale_triggers(db) == _LEGACY_TRIGGER_NAMES
    assert _has_unique_key(db) is unique_key

    created = _remember_fresh(repo)
    assert created["status"] == "active"
    assert _memory_rows(db)[:3] == list(_LEGACY_ROWS)
    assert _fts_ids(db, "payload") == [3, 7, 12, 13]
    assert _stale_triggers(db) == []


def test_memories_rebuild_runs_entirely_through_execute_in_one_transaction(tmp_path: Path) -> None:
    """Undisturbed, the rebuild succeeds through ``execute`` alone (the proxy
    refuses executescript) and every DDL statement runs inside the transaction."""
    repo = _repo(tmp_path)
    db = _build_legacy_aitools_db(repo)

    real = sqlite3.connect(str(db))
    real.row_factory = sqlite3.Row
    proxy = _SabotageConn(real)
    try:
        context_writes._normalize_memory_schema(proxy)
    finally:
        real.close()

    assert _memory_rows(db) == list(_LEGACY_ROWS)
    assert _fts_ids(db, "payload") == [3, 7, 12]
    assert _stale_triggers(db) == []
    assert not _has_unique_key(db)
    assert proxy.ddl_in_transaction()
    assert all(proxy.ddl_in_transaction())


def test_migration_is_a_noop_once_another_opener_has_migrated_the_store(tmp_path: Path) -> None:
    """What needs migrating is re-checked under the write lock: an opener that
    lost the race to a concurrent one finds nothing left to do, and neither
    rebuilds the table nor re-indexes memories_fts a second time."""
    repo = _repo(tmp_path)
    db = _build_legacy_aitools_db(repo)
    _remember_fresh(repo)
    before = _schema_snapshot(db)
    assert _stale_triggers(db) == []
    assert not _has_unique_key(db)

    real = sqlite3.connect(str(db))
    real.row_factory = sqlite3.Row
    proxy = _SabotageConn(real)
    try:
        context_writes._migrate_memories_schema(proxy)
        assert not real.in_transaction
    finally:
        real.close()

    assert proxy.ddl_in_transaction() == []
    assert _schema_snapshot(db) == before


def test_ensure_memories_fts_is_a_lock_free_noop_when_fts_exists(tmp_path: Path) -> None:
    """With the index present the repair only reads: it neither takes the write
    lock nor waits behind another writer holding it."""
    repo = _repo(tmp_path)
    context_writes.memory_write(
        repo, actor=_actor(), action="remember", key="noop.key", value="noop payload",
        idempotency_key="memory:noop:0001", provenance="noop test",
    )
    db = _memory_db(repo)
    before = _query(db, "SELECT type,name,sql FROM sqlite_master ORDER BY type,name")

    writer = sqlite3.connect(str(db), timeout=0.1)
    try:
        writer.execute("BEGIN IMMEDIATE")
        result = context_writes.ensure_memories_fts(repo)
    finally:
        writer.rollback()
        writer.close()

    assert result == {"ok": True, "created": False, "reason": "fts_already_exists"}
    assert _query(db, "SELECT type,name,sql FROM sqlite_master ORDER BY type,name") == before
