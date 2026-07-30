from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aiworkhub import context_writes, storage_registry, task_store


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


def test_session_write_is_canonical_audited_and_idempotent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    kwargs = dict(
        actor=_actor(), action="checkpoint", topic="release",
        content="callback gate passed", idempotency_key="session:test:0001",
        provenance="manager verified runtime test",
    )
    first = context_writes.session_write(repo, **kwargs)
    second = context_writes.session_write(repo, **kwargs)
    assert first["ok"] and not first["idempotent"]
    assert second["ok"] and second["idempotent"]
    db = storage_registry.resolve_database_path(storage_registry.load_storage_registry(repo), "transcript")
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM context_mutations").fetchone()[0] == 1
    finally:
        con.close()


def test_memory_write_supports_update_supersede_and_archive(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = dict(actor=_actor(), key="decision.routing", tags="routing", scope="project", provenance="ADR-1")
    remembered = context_writes.memory_write(
        repo, **base, action="remember", value="A", idempotency_key="memory:test:0001",
    )
    updated = context_writes.memory_write(
        repo, **base, action="update", value="B", idempotency_key="memory:test:0002",
    )
    superseded = context_writes.memory_write(
        repo, **base, action="supersede", value="C", idempotency_key="memory:test:0003",
    )
    archived = context_writes.memory_write(
        repo, **base, action="archive", idempotency_key="memory:test:0004",
    )
    assert remembered["memory_id"] == updated["memory_id"]
    assert superseded["memory_id"] != remembered["memory_id"]
    assert archived["status"] == "archived"


def test_kb_write_upserts_supersedes_and_never_hard_deletes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = dict(actor=_actor(), provenance="docs/adr/0001.md", category="architecture", tags="routing", source_refs="ADR-1")
    first = context_writes.kb_write(
        repo, **base, action="upsert", key="routing.v1", title="Routing", body="A",
        idempotency_key="kb:test:0001",
    )
    replacement = context_writes.kb_write(
        repo, **base, action="supersede", key="routing.v1", replacement_key="routing.v2",
        title="Routing v2", body="B", idempotency_key="kb:test:0002",
    )
    archived = context_writes.kb_write(
        repo, **base, action="archive", key="routing.v2",
        idempotency_key="kb:test:0003",
    )
    assert first["entry_id"] != replacement["entry_id"]
    assert archived["status"] == "archived"
    db = storage_registry.resolve_database_path(storage_registry.load_storage_registry(repo), "kb")
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2
        states = dict(con.execute("SELECT entity_id,status FROM context_entity_state WHERE entity_type='kb'"))
        assert states[first["entry_id"]] == "superseded"
        assert states[replacement["entry_id"]] == "archived"
    finally:
        con.close()


def test_invalid_or_duplicate_inputs_fail_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(context_writes.ContextWriteError, match="invalid_idempotency_key"):
        context_writes.session_write(
            repo, actor=_actor(), action="event", topic="x", content="y",
            idempotency_key="short", provenance="test",
        )
    context_writes.memory_write(
        repo, actor=_actor(), action="remember", key="same", value="A",
        idempotency_key="memory:test:1001", provenance="test",
    )
    with pytest.raises(context_writes.ContextWriteError, match="memory_key_exists_use_update"):
        context_writes.memory_write(
            repo, actor=_actor(), action="remember", key="same", value="B",
            idempotency_key="memory:test:1002", provenance="test",
        )
