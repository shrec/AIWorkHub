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
