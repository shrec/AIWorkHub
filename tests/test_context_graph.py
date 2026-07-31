from __future__ import annotations

import sqlite3
from pathlib import Path

from aiworkhub import context_graph, context_writes, feature_settings, storage_registry, task_store


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    return repo


def _enable(repo: Path) -> None:
    result = feature_settings.update(
        repo,
        changes={"context_graph": True},
        expected_revision=feature_settings.load(repo)["revision"],
    )
    assert result["features"]["context_graph"] is True


def _append(repo: Path, *, suffix: str, content: str, task_id: str = "TASK-1") -> dict:
    return context_graph.append_event(
        repo,
        thread_id="thread-1",
        session_id="session-1",
        provider="codex",
        role="assistant",
        event_type="message",
        content=content,
        source_ref=f"test:{suffix}",
        idempotency_key=f"context-graph-test-{suffix}",
        task_id=task_id,
        metadata={"ordinal": suffix},
        occurred_at=f"2026-07-31T00:00:0{suffix}+00:00",
    )


def _projection(repo: Path) -> tuple[list[tuple], list[tuple]]:
    registry = storage_registry.load_storage_registry(repo)
    db = storage_registry.resolve_database_path(registry, "transcript")
    con = sqlite3.connect(db)
    try:
        nodes = con.execute(
            "SELECT node_id,node_type,label,state,source_event_id,metadata_json,updated_at "
            "FROM context_nodes ORDER BY node_id"
        ).fetchall()
        edges = con.execute(
            "SELECT edge_id,src_node_id,relation,dst_node_id,source_event_id,"
            "metadata_json,created_at FROM context_edges ORDER BY edge_id"
        ).fetchall()
        return nodes, edges
    finally:
        con.close()


def test_disabled_context_graph_fails_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    result = context_graph.search(repo, "anything")

    assert result == {
        "ok": False,
        "status": "disabled",
        "error": "feature_disabled:context_graph",
        "feature": "context_graph",
    }


def test_event_ingestion_is_idempotent_searchable_and_repo_local(tmp_path: Path) -> None:
    first_repo = _repo(tmp_path, "first")
    second_repo = _repo(tmp_path, "second")
    _enable(first_repo)
    _enable(second_repo)

    first = _append(first_repo, suffix="1", content="alpha routing decision")
    duplicate = _append(first_repo, suffix="1", content="alpha routing decision")
    _append(second_repo, suffix="2", content="alpha belongs to the other repository")

    assert first["ok"] is True and first["idempotent"] is False
    assert duplicate["ok"] is True and duplicate["idempotent"] is True
    assert duplicate["event_id"] == first["event_id"]
    local = context_graph.search(first_repo, "alpha")
    assert local["count"] == 1
    assert local["results"][0]["content"] == "alpha routing decision"
    runtime = context_graph.status(first_repo)
    assert runtime["ready"] is True
    assert runtime["capture_scope"] == "manager_only"
    assert runtime["capture_adapters"]["codex"] == "final_items"
    assert runtime["events"] == 1
    assert runtime["nodes"] == 6
    assert runtime["edges"] == 5


def test_exact_range_related_edges_and_deterministic_rebuild(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _enable(repo)
    events = [
        _append(repo, suffix=str(index), content=f"message {index}")
        for index in range(1, 4)
    ]

    exact = context_graph.get_range(
        repo,
        thread_id="thread-1",
        around_event_id=events[1]["event_id"],
        before=1,
        after=1,
    )
    assert [item["content"] for item in exact["events"]] == [
        "message 1",
        "message 2",
        "message 3",
    ]
    related = context_graph.related(repo, node_id="task:TASK-1")
    assert related["count"] == 3
    assert {item["relation"] for item in related["relations"]} == {"FOR_TASK"}

    before = _projection(repo)
    registry = storage_registry.load_storage_registry(repo)
    db = storage_registry.resolve_database_path(registry, "transcript")
    con = sqlite3.connect(db)
    try:
        con.execute("DELETE FROM context_edges")
        con.execute("DELETE FROM context_nodes")
        con.commit()
    finally:
        con.close()
    rebuilt = context_graph.rebuild_projection(repo)

    assert rebuilt == {"ok": True, "events_projected": 3, "last_event_id": 3}
    assert _projection(repo) == before


def test_session_write_atomically_feeds_context_graph(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _enable(repo)
    actor = {
        "role": "manager",
        "actor_id": "manager-1",
        "task_id": "TASK-9",
        "provider": "codex",
        "session_id": "thread-9",
    }

    written = context_writes.session_write(
        repo,
        actor=actor,
        action="checkpoint",
        topic="release",
        content="context graph checkpoint evidence",
        idempotency_key="session:context-graph:0001",
        provenance="test",
    )

    assert written["ok"] is True
    found = context_graph.search(repo, "checkpoint evidence")
    assert found["count"] == 1
    assert found["results"][0]["thread_id"] == "thread-9"
    assert found["results"][0]["task_id"] == "TASK-9"
    assert found["results"][0]["source_ref"] == f"session_document:{written['document_id']}"


def test_worker_session_write_never_enters_manager_context_graph(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _enable(repo)
    worker = {
        "role": "worker",
        "actor_id": "worker-1",
        "task_id": "TASK-10",
        "provider": "deepseek",
        "session_id": "worker-session-1",
    }

    written = context_writes.session_write(
        repo,
        actor=worker,
        action="checkpoint",
        topic="worker evidence",
        content="worker-only session evidence",
        idempotency_key="session:worker-context-graph:0001",
        provenance="test",
    )

    assert written["ok"] is True
    assert context_graph.search(repo, "worker-only")["count"] == 0
    assert context_graph.status(repo)["events"] == 0
