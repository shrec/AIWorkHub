from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import (  # noqa: E402
    context_writes,
    core,
    manager_ai_tools,
    storage_registry,
    task_store,
)

NOW = datetime.now(timezone.utc)


def _ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


def _manager_route(root: Path) -> dict:
    session_id = "019f5097-6dbe-7172-870a-945afc5f3bfa"
    return {
        "ok": True,
        "role": "manager",
        "provider": "codex",
        "repo": str(root),
        "manager_route": {"provider": "codex", "session_id": session_id, "thread_id": session_id},
    }


def _store(tmp_path: Path, monkeypatch, *, normalize: bool = True) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(root))
    db = storage_registry.resolve_database_path(storage_registry.load_storage_registry(root), "memory")
    if normalize:
        context_writes._open(root, "memory").close()
    return root, db


def _add(db: Path, key: str, value: str, *, tags: str = "", created: str | None = None,
         updated: str | None = None) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO memories(key,value,tags,scope,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (key, value, tags, "project", created, updated),
        )
        con.commit()
    finally:
        con.close()


def _by_reason(report: dict, reason: str) -> list[dict]:
    return [c for c in report["candidates"] if c["reason"] == reason]


def _sha(db: Path) -> str:
    return hashlib.sha256(db.read_bytes()).hexdigest()


def test_each_reason_fires_and_nothing_is_written(tmp_path, monkeypatch):
    root, db = _store(tmp_path, monkeypatch)
    _add(db, "old.updated", "ancient alpha", updated=_ago(200), created=_ago(300))
    _add(db, "old.created", "ancient beta", created=_ago(300))
    _add(db, "fresh.k", "fresh gamma", updated=_ago(1), created=_ago(1))
    for i in range(7):
        _add(db, f"unknown.{i}", f"nothing{i} known{i}")
    _add(db, "dup.key", "first body", updated=_ago(1))
    _add(db, "dup.key", "second body", updated=_ago(1))
    words = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    _add(db, "near.a", words, tags="shared,x", updated=_ago(1))
    _add(db, "near.b", words + " lambda", tags="y,shared", updated=_ago(1))
    _add(db, "far.c", "completely different words here", tags="shared", updated=_ago(1))
    before = _sha(db)

    report = manager_ai_tools.memory_review_report(limit=50, stale_days=90)

    assert _sha(db) == before
    assert report["ok"] is True
    assert report["schema_id"] == "aiworkhub.memory_review.v1"
    assert report["surface"] == "manager_mcp"
    assert report["timestamps_available"] is True
    stale = {c["keys"][0]: c for c in _by_reason(report, "stale")}
    assert set(stale) == {"old.updated", "old.created"}
    assert stale["old.updated"]["evidence"]["timestamp_column"] == "updated_at"
    assert stale["old.created"]["evidence"]["timestamp_column"] == "created_at"
    assert [c["keys"] for c in _by_reason(report, "key_collision")] == [["dup.key"]]
    assert _by_reason(report, "key_collision")[0]["evidence"]["active_rows"] == 2
    near = _by_reason(report, "near_duplicate")
    assert [c["keys"] for c in near] == [["near.a", "near.b"]]
    assert near[0]["evidence"]["jaccard"] >= 0.8
    assert near[0]["evidence"]["shared_tags"] == ["shared"]
    assert report["candidate_counts"] == {"stale": 2, "key_collision": 1, "near_duplicate": 1}


def test_unknown_age_is_counted_with_examples_and_never_dated(tmp_path, monkeypatch):
    _root, db = _store(tmp_path, monkeypatch)
    for i in range(8):
        _add(db, f"unknown.{i}", f"nothing{i} known{i}")

    report = manager_ai_tools.memory_review_report()

    assert report["unknown_age"]["count"] == 8
    assert len(report["unknown_age"]["example_keys"]) == 5
    assert set(report["unknown_age"]["example_keys"]) <= {f"unknown.{i}" for i in range(8)}
    assert report["candidates"] == []
    assert "age_days" not in json.dumps(report)


def test_limit_is_clamped_to_one_through_fifty(tmp_path, monkeypatch):
    _root, db = _store(tmp_path, monkeypatch)
    for i in range(60):
        _add(db, f"old.{i:02d}", f"ancient{i} text{i}", updated=_ago(100 + i))

    assert len(manager_ai_tools.memory_review_report(limit=1000)["candidates"]) == 50
    assert len(manager_ai_tools.memory_review_report(limit=0)["candidates"]) == 1
    assert len(manager_ai_tools.memory_review_report(limit=-5)["candidates"]) == 1
    clamped = manager_ai_tools.memory_review_report(limit=1000)
    assert clamped["limit"] == 50
    assert clamped["candidates_omitted"] == 10
    assert clamped["candidate_counts"]["stale"] == 60


def test_superseded_and_archived_memories_are_not_reviewed(tmp_path, monkeypatch):
    _root, db = _store(tmp_path, monkeypatch)
    _add(db, "old.k", "ancient text", updated=_ago(200))
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO context_entity_state VALUES('memory',(SELECT id FROM memories),'archived',NULL,?)",
            (_ago(1),),
        )
        con.commit()
    finally:
        con.close()

    report = manager_ai_tools.memory_review_report()

    assert report["scanned"] == 0
    assert report["candidates"] == []


def test_store_without_timestamp_columns_reports_unavailable_without_error(tmp_path, monkeypatch):
    _root, db = _store(tmp_path, monkeypatch, normalize=False)
    con = sqlite3.connect(db)
    try:
        con.executescript(
            "DROP TABLE memories;"
            "CREATE TABLE memories(id INTEGER PRIMARY KEY,key TEXT,value TEXT,tags TEXT,scope TEXT);"
            "INSERT INTO memories(key,value,tags,scope) VALUES('a','one','t','project');"
            "INSERT INTO memories(key,value,tags,scope) VALUES('b','two','t','project');"
        )
        con.commit()
    finally:
        con.close()
    before = _sha(db)

    report = manager_ai_tools.memory_review_report()

    assert _sha(db) == before
    assert report["ok"] is True
    assert report["timestamps_available"] is False
    assert report["unknown_age"]["count"] == 2
    assert _by_reason(report, "stale") == []
    cols = [r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(memories)")]
    assert "created_at" not in cols


def test_report_stays_under_sixteen_kib_and_truncates_values(tmp_path, monkeypatch):
    _root, db = _store(tmp_path, monkeypatch)
    for i in range(50):
        _add(db, f"old.{i:02d}", f"w{i} " + "x" * 5000, updated=_ago(100 + i))
    con = sqlite3.connect(db)
    try:
        report = manager_ai_tools.memory_review_from_connection(con, limit=50)
    finally:
        con.close()

    assert len(json.dumps(report, ensure_ascii=False, sort_keys=True).encode("utf-8")) <= 16 * 1024
    assert report["truncated"] is True
    assert report["candidates_omitted"] > 0
    assert all(len(c["evidence"]["value"]) <= 200 for c in report["candidates"])
