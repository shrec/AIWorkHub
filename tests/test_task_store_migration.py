"""Adversarial regressions for fail-closed task-store migration (NF-2026-00223)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aiworkhub import fresh_task_store
from aiworkhub import task_store_migration as migrator
from aiworkhub.repository_state import bootstrap_repository


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_repo(tmp_path: Path, repo_id: str = "repo_nf202600223_00000001") -> Path:
    root = tmp_path / repo_id
    root.mkdir()
    bootstrap_repository(
        root,
        repo_id=repo_id,
        repo_name=repo_id,
        created_at="2026-07-20T00:00:00+00:00",
    )
    (root / "bitnnv2" / "data" / "tasking").mkdir(parents=True)
    return root


def _seed_db(path: Path, task_id: str = "SAME_TASK_ID") -> None:
    now = _utc_now()
    card = {
        "task_id": task_id,
        "runner": "codex_nf",
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
                (task_id, event, "codex_nf", json.dumps({"kept": True}), now),
            )
        conn.execute(
            "INSERT INTO task_artifacts(task_id, path, kind, summary_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
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


def test_create_shadow_fails_closed_when_source_absent(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    paths = _paths(root)
    assert not paths.source_db.exists()
    with pytest.raises(migrator.SourceAbsentError):
        migrator.create_shadow(root)
    # A missing source must never materialise as an empty DB or an empty shadow.
    assert not paths.source_db.exists()
    assert not paths.shadow_db.exists()


def test_cutover_preserves_populated_canonical_when_source_absent(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    paths = _paths(root)
    _seed_db(paths.canonical_db)
    with pytest.raises(migrator.SourceAbsentError):
        migrator.cutover_shadow(root, allow_cutover=True)
    assert paths.canonical_db.exists()
    assert migrator.parity_snapshot(paths.canonical_db)["counts"]["tasks"] == 1
    assert not paths.rollback_db.exists()


def test_cutover_fails_closed_when_shadow_absent(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    paths = _paths(root)
    _seed_db(paths.source_db)
    _seed_db(paths.canonical_db)
    assert not paths.shadow_db.exists()
    with pytest.raises(migrator.ShadowAbsentError):
        migrator.cutover_shadow(root, allow_cutover=True)
    assert paths.canonical_db.exists()
    assert migrator.parity_snapshot(paths.canonical_db)["counts"]["tasks"] == 1


def test_rerun_cutover_generates_durable_non_overwriting_rollbacks(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    paths = _paths(root)
    _seed_db(paths.source_db)
    migrator.create_shadow(root)
    first = migrator.cutover_shadow(root, allow_cutover=True)
    assert first["rollback_db"] == ""  # no pre-existing canonical store to roll back

    migrator.create_shadow(root)
    second = migrator.cutover_shadow(root, allow_cutover=True)
    assert second["rollback_db"] != ""
    assert Path(second["rollback_db"]).exists()

    migrator.create_shadow(root)
    third = migrator.cutover_shadow(root, allow_cutover=True)
    assert third["rollback_db"] != second["rollback_db"]
    assert Path(second["rollback_db"]).exists()
    assert Path(third["rollback_db"]).exists()


def test_cutover_fails_closed_with_live_wal_connection_on_canonical(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    paths = _paths(root)
    _seed_db(paths.source_db)
    migrator.create_shadow(root)
    _seed_db(paths.canonical_db)
    live = sqlite3.connect(paths.canonical_db)
    try:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("BEGIN IMMEDIATE")
        live.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("SAME_TASK_ID", "uncommitted", "test", "{}", _utc_now()),
        )
        with pytest.raises(migrator.WalUnsafeError):
            migrator.cutover_shadow(root, allow_cutover=True)
    finally:
        live.rollback()
        live.close()
    # The populated canonical store must survive the refused cutover intact.
    assert paths.canonical_db.exists()
    snapshot = migrator.parity_snapshot(paths.canonical_db)
    assert snapshot["counts"]["tasks"] == 1
    assert snapshot["counts"]["task_events"] == 2


def test_parity_snapshot_fails_closed_on_absent_source(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    paths = _paths(root)
    with pytest.raises(migrator.SourceAbsentError):
        migrator.parity_snapshot(paths.source_db)


def test_readonly_open_fails_closed_when_source_deleted_after_precheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOCTOU regression: deletion between the existence precheck and the open
    must not silently recreate an empty, all-zero source database."""
    root = _make_repo(tmp_path)
    paths = _paths(root)
    _seed_db(paths.source_db)
    assert paths.source_db.exists()

    real_connect = sqlite3.connect

    def deleting_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        # The file is present when ``_connect`` runs its existence precheck;
        # remove it at the exact moment a plain read-write open would recreate
        # an empty database. mode=ro must raise instead of recreating it.
        if paths.source_db.exists():
            paths.source_db.unlink()
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(migrator.sqlite3, "connect", deleting_connect)

    with pytest.raises(migrator.SourceAbsentError):
        migrator._connect(paths.source_db, readonly=True)

    # Fail closed: the absent source must not be recreated as an empty DB.
    assert not paths.source_db.exists()


def test_readonly_open_maps_non_missing_failure_to_database_error(
    tmp_path: Path,
) -> None:
    """A readonly open that fails for a reason other than a missing source must
    surface as a migration database error, not a silent empty source."""
    not_a_db = tmp_path / "not_a_db"
    not_a_db.mkdir()
    with pytest.raises(migrator.MigrationDatabaseError):
        migrator._connect(not_a_db, readonly=True)


def _corrupt_db(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is definitely not a sqlite database file")
    return path


def test_parity_snapshot_maps_corrupt_db_to_migration_error(tmp_path: Path) -> None:
    """A corrupt database must surface as MigrationDatabaseError with the
    underlying sqlite3 error preserved, not leak a raw sqlite3.DatabaseError."""
    corrupt = _corrupt_db(tmp_path / "corrupt.sqlite")
    with pytest.raises(migrator.MigrationDatabaseError) as exc_info:
        migrator.parity_snapshot(corrupt)
    assert isinstance(exc_info.value.__cause__, sqlite3.DatabaseError)


def test_compare_parity_maps_corrupt_target_to_migration_error(tmp_path: Path) -> None:
    """Both sides of parity comparison fail closed on a corrupt database."""
    root = _make_repo(tmp_path)
    paths = _paths(root)
    _seed_db(paths.source_db)
    _corrupt_db(paths.canonical_db)
    with pytest.raises(migrator.MigrationDatabaseError) as exc_info:
        migrator.compare_parity(paths.source_db, paths.canonical_db)
    assert isinstance(exc_info.value.__cause__, sqlite3.DatabaseError)


def test_create_shadow_maps_corrupt_source_to_migration_error(tmp_path: Path) -> None:
    """An online backup of a corrupt source must fail closed, never leak a raw
    sqlite3.DatabaseError, and never leave a shadow behind."""
    root = _make_repo(tmp_path)
    paths = _paths(root)
    _corrupt_db(paths.source_db)
    with pytest.raises(migrator.MigrationDatabaseError) as exc_info:
        migrator.create_shadow(root)
    assert isinstance(exc_info.value.__cause__, sqlite3.DatabaseError)
    assert not paths.shadow_db.exists()
