from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from aiworkhub import context_writes, storage_registry, task_store
from aiworkhub import worker_ai_tools_mcp as worker_tools


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    db = storage_registry.resolve_database_path(
        storage_registry.load_storage_registry(repo), "transcript"
    )
    con = sqlite3.connect(db)
    try:
        con.executescript(
            "DROP TABLE documents_fts; DROP TABLE documents;"
            "CREATE TABLE documents("
            "doc_id INTEGER PRIMARY KEY AUTOINCREMENT,source TEXT NOT NULL,"
            "source_id TEXT NOT NULL,session_id INTEGER,timestamp TEXT,kind TEXT,"
            "speaker TEXT,content TEXT NOT NULL,tags TEXT);"
            "CREATE VIRTUAL TABLE documents_fts USING fts5("
            "content,kind,tags,content='documents',content_rowid='doc_id');"
        )
        con.commit()
    finally:
        con.close()
    return repo


def _actor() -> dict[str, str]:
    return {
        "role": "manager",
        "actor_id": "thread-topic",
        "task_id": "TASK-TOPIC",
        "provider": "codex",
        "session_id": "thread-topic",
    }


def _ctx(repo: Path, topic: str) -> worker_tools.WorkerToolContext:
    return worker_tools.WorkerToolContext(
        task_id="TASK-TOPIC",
        runner="codex_topic_test",
        topic="aiworkhub_context_integrity",
        request_id="request-topic",
        repo=repo,
        authority_repo=repo,
        source_graph_targets=(),
        session_topic=topic,
        audit_ledger_path=None,
        audit_hmac_key_path=None,
    )


def test_session_current_state_exact_topic_zero_hit_stays_empty(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    context_writes.session_write(
        repo,
        actor=_actor(),
        action="checkpoint",
        topic="other-topic",
        content="target-topic appears in content but is not the canonical topic",
        idempotency_key="session:topic:zero:0001",
        provenance="topic isolation regression",
    )

    result = worker_tools.session_current_state(_ctx(repo, "target-topic"), limit=5)
    payload = json.loads(result["content"])

    assert result["ok"] is True
    assert result["hit_count"] == 0
    assert payload["topic"] == "target-topic"
    assert payload["state"] == "unknown"
    assert payload["evidence"] == []


def test_session_current_state_same_topic_rows_are_current_with_provenance(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    for index in range(2):
        context_writes.session_write(
            repo,
            actor=_actor(),
            action="checkpoint",
            topic="target-topic",
            content=f"same topic durable evidence {index}",
            idempotency_key=f"session:topic:multi:000{index}",
            provenance="topic isolation regression",
        )

    result = worker_tools.session_current_state(_ctx(repo, "target-topic"), limit=5)
    payload = json.loads(result["content"])

    assert result["ok"] is True
    assert result["hit_count"] == 2
    assert payload["state"] == "current"
    assert payload["evidence_count"] == 2
    assert payload["authority"]["source"] == "canonical"
    assert payload["authority"]["repo"] == str(repo)
    assert {item["topic"] for item in payload["evidence"]} == {"target-topic"}
    assert {item["source"] for item in payload["evidence"]} == {"aiworkhub"}
    assert {item["speaker"] for item in payload["evidence"]} == {"manager"}
    assert all(":thread-topic:target-topic" in item["source_id"] for item in payload["evidence"])
