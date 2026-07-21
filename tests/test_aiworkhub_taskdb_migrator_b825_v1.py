from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]

from aiworkhub import fresh_task_store
from aiworkhub import task_store_migration as migrator
from aiworkhub.repository_state import bootstrap_repository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_repo(tmp_path: Path, repo_id: str = "repo_b825_test_00000001") -> Path:
    root = tmp_path / repo_id
    root.mkdir()
    bootstrap_repository(root, repo_id=repo_id, repo_name=repo_id, created_at="2026-07-20T00:00:00+00:00")
    (root / "bitnnv2" / "data" / "tasking").mkdir(parents=True)
    return root


def _seed_db(path: Path, task_id: str = "SAME_TASK_ID") -> None:
    now = _utc_now()
    card = {
        "task_id": task_id,
        "runner": "codex_b825",
        "topic": "task_mcp",
        "mode": "migration",
        "status": "review",
        "worker_status": "review",
        "priority": "high",
        "objective": "preserve identity",
        "origin_thread_id": "11111111-2222-4333-8444-555555555555",
        "allowed_writes": ["x"],
        "forbidden": ["y"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(fresh_task_store._FRESH_SCHEMA)
        conn.execute(
            """
            INSERT INTO tasks(
              task_id, runner, mode, status, worker_status, priority, objective,
              card_json, created_at, updated_at, origin_thread_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                card["runner"],
                card["mode"],
                card["status"],
                card["worker_status"],
                card["priority"],
                card["objective"],
                json.dumps(card, sort_keys=True),
                now,
                now,
                card["origin_thread_id"],
            ),
        )
        for event in ("card_upserted", "review"):
            conn.execute(
                "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, event, "codex_b825", json.dumps({"kept": True}), now),
            )
        conn.execute(
            "INSERT INTO task_artifacts(task_id, path, kind, summary_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, "artifact.json", "evidence", "{}", now),
        )
        conn.execute(
            """
            INSERT INTO callback_outbox(
              task_id, origin_thread_id, transition, episode_id, state,
              created_at, updated_at, batch_id
            ) VALUES (?, ?, 'review_ready', '0', 'pending', ?, ?, 'batch-a')
            """,
            (task_id, card["origin_thread_id"], now, now),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO callback_batches(
              batch_id, origin_thread_id, state, created_at, updated_at, member_count
            ) VALUES ('batch-a', '11111111-2222-4333-8444-555555555555', 'pending', ?, ?, 1)
            """,
            (now, now),
        )
        conn.commit()


def _paths(root: Path) -> migrator.TaskStorePaths:
    return migrator.resolve_task_store_paths(root)


def test_shadow_uses_sqlite_backup_api_with_active_wal_and_preserves_parity(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    paths = _paths(root)
    _seed_db(paths.source_db)
    writer = sqlite3.connect(paths.source_db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            ("SAME_TASK_ID", "concurrent_committed", "test", "{}", _utc_now()),
        )
        writer.commit()
        result = migrator.create_shadow(root)
    finally:
        writer.close()
    assert result["cutover_performed"] is False
    assert result["paths"]["canonical_db"].endswith(".aiworkhub/tasking/task_queue.sqlite")
    assert result["parity"]["ok"] is True
    assert result["parity"]["source"]["counts"]["task_events"] == 3


def test_interrupted_backup_does_not_publish_partial_shadow(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    paths = _paths(root)
    _seed_db(paths.source_db)
    paths.shadow_db.parent.mkdir(parents=True, exist_ok=True)
    paths.shadow_db.write_bytes(b"old-shadow")

    def fail_after_first_call(_status: int, _remaining: int, _total: int) -> None:
        raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError, match="simulated interruption"):
        migrator.sqlite_online_backup(paths.source_db, paths.shadow_db, pages=1, progress=fail_after_first_call)
    assert paths.shadow_db.read_bytes() == b"old-shadow"


def test_mismatch_rejection_when_source_changes_after_shadow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    paths = _paths(root)
    _seed_db(paths.source_db)
    real_backup = migrator.sqlite_online_backup

    def backup_then_change(source: Path, destination: Path, **kwargs: object) -> None:
        real_backup(source, destination, **kwargs)
        with sqlite3.connect(source) as conn:
            conn.execute(
                "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                ("SAME_TASK_ID", "after_shadow", "test", "{}", _utc_now()),
            )
            conn.commit()

    monkeypatch.setattr(migrator, "sqlite_online_backup", backup_then_change)
    with pytest.raises(migrator.ParityMismatchError):
        migrator.create_shadow(root)


def test_explicit_cutover_is_atomic_and_keeps_legacy_as_rollback_source(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    paths = _paths(root)
    _seed_db(paths.source_db)
    migrator.create_shadow(root)
    with pytest.raises(migrator.CutoverRefusedError):
        migrator.cutover_shadow(root)
    result = migrator.cutover_shadow(root, allow_cutover=True)
    assert result["cutover_performed"] is True
    assert paths.source_db.exists()
    assert paths.canonical_db.exists()
    assert not paths.shadow_db.exists()
    assert result["parity"]["ok"] is True


def test_two_repositories_with_identical_task_ids_do_not_cross_queues(tmp_path: Path) -> None:
    roots = [
        _make_repo(tmp_path, "repo_b825_test_00000001"),
        _make_repo(tmp_path, "repo_b825_test_00000002"),
    ]
    snapshots = []
    for index, root in enumerate(roots):
        paths = _paths(root)
        _seed_db(paths.source_db, task_id="IDENTICAL_TASK")
        with sqlite3.connect(paths.source_db) as conn:
            conn.execute(
                "UPDATE tasks SET runner=? WHERE task_id='IDENTICAL_TASK'",
                (f"runner_{index}",),
            )
            conn.commit()
        result = migrator.create_shadow(root)
        snapshots.append(result["parity"]["target"])

    assert snapshots[0]["repo_id"] != snapshots[1]["repo_id"]
    assert snapshots[0]["db_path"] != snapshots[1]["db_path"]
    assert snapshots[0]["task_identities"][0]["task_id"] == snapshots[1]["task_identities"][0]["task_id"]
    assert snapshots[0]["task_identities"][0]["status"] == "review"


def test_migration_uses_explicit_source_and_ignores_ambient_legacy_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _make_repo(tmp_path)
    override = tmp_path / "compat.sqlite"
    monkeypatch.setenv("BITNN_TASK_QUEUE_DB", str(override))
    paths = migrator.resolve_task_store_paths(root, source_db="explicit/source.sqlite")
    assert paths.source_db == (root / "explicit/source.sqlite").resolve()
    assert paths.source_db != override


def test_evidence_json_is_valid() -> None:
    payload = json.loads(
        (REPO / "eval/aiworkhub_taskdb_migrator_b825_v1.json").read_text()
    )
    assert payload["task_id"] == "CODEX_GPT55_AIWORKHUB_TASKDB_MIGRATOR_B825_V1"
    assert payload["default_action"] == "shadow_only"
