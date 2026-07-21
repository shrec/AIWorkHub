from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aiworkhub import fresh_task_store


REPO = Path(__file__).resolve().parents[3]
TASK_ID = fresh_task_store.TASK_ID


def _storage(repo_id: str) -> dict:
    return {
        "schema_id": "aiworkhub.storage_registry.v1",
        "registry_version": 1,
        "repo_id": repo_id,
        "portable": True,
        "host_absolute_paths_allowed": False,
        "durable_root": ".aiworkhub",
        "registry_path": "config/storage.json",
        "databases": [
            {
                "id": "task_queue",
                "component": "task_mcp",
                "schema_version": 1,
                "canonical_durable_path": "tasking/task_queue.sqlite",
                "legacy_source": "bitnnv2/data/tasking/task_queue_v1.sqlite",
                "integrity": {
                    "state": "canonical_inventory_hash_supplied",
                    "rollback_source_sha256": "0" * 64,
                    "sparse_worktree_missing_is_not_absence": True,
                },
                "authority": {
                    "state": "shadow",
                    "canonical_active": False,
                    "legacy_active": False,
                    "live_cutover": False,
                },
                "migration": {
                    "generation": 4,
                    "source_read_only": True,
                    "rollback_source_hash": "0" * 64,
                    "cutover_performed": False,
                    "legacy_deleted": False,
                },
            },
            {
                "id": "kb",
                "component": "kb",
                "schema_version": 1,
                "canonical_durable_path": "kb/knowledge.sqlite",
                "legacy_source": "AITools/kb.db",
                "integrity": {"state": "unchanged", "rollback_source_sha256": "1" * 64},
                "authority": {"state": "shadow", "canonical_active": False, "legacy_active": False, "live_cutover": False},
                "migration": {"generation": 0, "source_read_only": True, "rollback_source_hash": "1" * 64, "cutover_performed": False, "legacy_deleted": False},
            },
        ],
    }


def _make_repo(tmp_path: Path, repo_id: str = "repo_test_b832") -> Path:
    repo = tmp_path / "repo"
    (repo / ".aiworkhub" / "config").mkdir(parents=True)
    (repo / "bitnnv2" / "data" / "tasking").mkdir(parents=True)
    (repo / "AITools").mkdir()
    shutil.copyfile(REPO / "AITools" / "taskdb.py", repo / "AITools" / "taskdb.py")
    (repo / ".aiworkhub" / "project.json").write_text(
        json.dumps({
            "schema_id": "aiworkhub.project_manifest.v1",
            "repo_id": repo_id,
            "repo_name": "Temp",
            "layout": {"durable": {"tasking": "tasking"}},
        }),
        encoding="utf-8",
    )
    (repo / ".aiworkhub" / "config" / "storage.json").write_text(
        json.dumps(_storage(repo_id), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return repo


def _taskdb():
    sys.path.insert(0, str(REPO / "AITools"))
    import taskdb  # type: ignore[import-not-found]

    return taskdb


def _seed_legacy(repo: Path, *, blocker: str | None = None, inflight: bool = False) -> Path:
    taskdb = _taskdb()
    db = repo / "bitnnv2" / "data" / "tasking" / "task_queue_v1.sqlite"
    with taskdb.open_db(db) as conn:
        taskdb.init_db(conn)
        taskdb.upsert_card(conn, {
            "task_id": TASK_ID,
            "runner": "codex",
            "mode": "fresh_repo_local_taskdb_cutover",
            "status": "processing",
            "worker_status": "processing",
            "objective": "cut over",
            "allowed_writes": ["AITools/taskdb.py"],
            "forbidden": ["worker_execute_production_cutover"],
        })
        if blocker:
            taskdb.upsert_card(conn, {
                "task_id": blocker,
                "runner": "other",
                "mode": "x",
                "status": blocker,
                "worker_status": blocker,
                "objective": "block",
                "allowed_writes": ["x"],
                "forbidden": ["y"],
            })
        if inflight:
            conn.execute(
                """
                INSERT INTO callback_outbox(task_id, origin_thread_id, transition, episode_id, request_id,
                                            state, created_at, updated_at, lease_id, lease_expires_at)
                VALUES('T', '12345678-1234-4234-9234-123456789abc', 'review_ready', '1', 'abc12345',
                       'inflight', 'now', 'now', 'lease-1', 'later')
                """
            )
            conn.commit()
    return db


def _count(db: Path, table: str) -> int:
    with sqlite3.connect(db) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_preflight_refuses_other_active_or_inflight_callback(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _seed_legacy(repo, blocker="processing", inflight=True)

    report = fresh_task_store.preflight(repo)

    assert report["ok"] is False
    assert report["active_blockers"][0]["task_id"] == "processing"
    assert report["inflight_callback_leases"]["callback_outbox"][0]["lease_id"] == "lease-1"


def test_preflight_ignores_expired_callback_lease(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    db = _seed_legacy(repo)
    expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            INSERT INTO callback_outbox(task_id, origin_thread_id, transition, episode_id, request_id,
                                        state, created_at, updated_at, lease_id, lease_expires_at)
            VALUES('OLD', '12345678-1234-4234-9234-123456789abc', 'review_ready', '1', 'abc12345',
                   'inflight', 'now', 'now', 'expired-lease', ?)
            """,
            (expired,),
        )
        conn.commit()
    report = fresh_task_store.preflight(repo)
    assert report["ok"] is True
    assert report["inflight_callback_leases"]["callback_outbox"] == []


def test_fresh_cutover_archives_legacy_and_publishes_empty_canonical_db(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    legacy = _seed_legacy(repo)
    before_registry = json.loads((repo / ".aiworkhub" / "config" / "storage.json").read_text())

    dry = fresh_task_store.cutover(repo, execute=False)
    result = fresh_task_store.cutover(repo, execute=True)

    archive = repo / ".aiworkhub" / fresh_task_store.ARCHIVE_REL
    canonical = repo / ".aiworkhub" / fresh_task_store.CANONICAL_REL
    after_registry = json.loads((repo / ".aiworkhub" / "config" / "storage.json").read_text())

    assert dry["execute_required"] is True
    assert result["cutover_performed"] is True
    assert result["legacy_imported"] is False
    assert result["archive"]["quick_check"] == "ok"
    assert result["archive"]["byte_size"] == archive.stat().st_size
    assert result["archive"]["sha256"] == fresh_task_store.sha256_file(archive)
    assert _count(legacy, "tasks") == 1
    assert _count(archive, "tasks") == 1
    assert fresh_task_store.empty_counts(canonical) == {
        "tasks": 0,
        "task_events": 0,
        "callback_outbox": 0,
        "callback_batches": 0,
    }
    task_record = next(item for item in after_registry["databases"] if item["id"] == "task_queue")
    assert task_record["authority"] == {
        "canonical_active": True,
        "legacy_active": False,
        "live_cutover": True,
        "state": "canonical_active",
    }
    assert task_record["migration"]["cutover_performed"] is True
    assert task_record["migration"]["generation"] == 5
    assert task_record["migration"]["archive_path"] == fresh_task_store.ARCHIVE_REL.as_posix()
    assert task_record["migration"]["archive_sha256"] == result["archive"]["sha256"]
    assert [item for item in before_registry["databases"] if item["id"] != "task_queue"] == [
        item for item in after_registry["databases"] if item["id"] != "task_queue"
    ]


def test_rollback_restores_legacy_authority_without_overwriting_archive(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _seed_legacy(repo)
    fresh_task_store.cutover(repo, execute=True)
    canonical = repo / ".aiworkhub" / fresh_task_store.CANONICAL_REL
    assert _count(canonical, "tasks") == 0

    dry = fresh_task_store.rollback(repo, execute=False)
    result = fresh_task_store.rollback(repo, execute=True)
    registry = json.loads((repo / ".aiworkhub" / "config" / "storage.json").read_text())
    task_record = next(item for item in registry["databases"] if item["id"] == "task_queue")

    assert dry["execute_required"] is True
    assert result["action"] == "rollback_complete"
    assert result["legacy_sha256"]
    assert _count(canonical, "tasks") == 0
    assert task_record["authority"]["state"] == "legacy_active_restored"
    assert task_record["authority"]["canonical_active"] is False
    assert task_record["authority"]["legacy_active"] is True
    assert task_record["authority"]["live_cutover"] is False
    assert task_record["migration"]["rollback_performed"] is True


def test_taskctl_execute_requires_coordinator_token(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _seed_legacy(repo)

    denied = subprocess.run(
        [sys.executable, str(REPO / "AITools" / "taskctl.py"), "task-db-fresh-cutover", "--repo-root", str(repo), "--execute"],
        cwd=REPO,
        text=True,
        capture_output=True,
    )

    assert denied.returncode != 0
    assert "explicit --runner codex is required" in denied.stdout

    token_file = tmp_path / "token"
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("secret\n")
    env = {
        **os.environ,
        "BITNN_TASKCTL_COORDINATOR_TOKEN": "secret",
        "BITNN_TASKCTL_COORDINATOR_TOKEN_FILE": str(token_file),
        "PYTHONPATH": str(REPO / "tools" / "aiworkhub" / "src"),
    }
    ok = subprocess.run(
        [
            sys.executable,
            str(REPO / "AITools" / "taskctl.py"),
            "task-db-fresh-cutover",
            "--repo-root",
            str(repo),
            "--runner",
            "codex",
            "--execute",
        ],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
    )

    assert ok.returncode == 0, ok.stderr + ok.stdout
    assert json.loads(ok.stdout)["action"] == "fresh_cutover_complete"


def test_taskdb_default_binds_to_active_registry_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BITNN_TASK_QUEUE_DB", raising=False)
    repo = _make_repo(tmp_path)
    module_path = repo / "AITools" / "taskdb.py"

    spec = importlib.util.spec_from_file_location("temp_taskdb_b832", module_path)
    assert spec and spec.loader
    shadow_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shadow_module)
    assert shadow_module.DEFAULT_DB == repo / "bitnnv2" / "data" / "tasking" / "task_queue_v1.sqlite"

    registry_path = repo / ".aiworkhub" / "config" / "storage.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    task_record = next(item for item in registry["databases"] if item["id"] == "task_queue")
    task_record["authority"].update({
        "state": "canonical_active",
        "canonical_active": True,
        "legacy_active": False,
        "live_cutover": True,
    })
    task_record["migration"]["cutover_performed"] = True
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    spec2 = importlib.util.spec_from_file_location("temp_taskdb_b832_active", module_path)
    assert spec2 and spec2.loader
    active_module = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(active_module)
    assert active_module.DEFAULT_DB == repo / ".aiworkhub" / "tasking" / "task_queue.sqlite"

    monkeypatch.setenv("BITNN_TASK_QUEUE_DB", str(tmp_path / "explicit.sqlite"))
    spec3 = importlib.util.spec_from_file_location("temp_taskdb_b832_env", module_path)
    assert spec3 and spec3.loader
    env_module = importlib.util.module_from_spec(spec3)
    spec3.loader.exec_module(env_module)
    assert env_module.DEFAULT_DB == tmp_path / "explicit.sqlite"


def test_taskctl_fresh_canonical_init_does_not_reimport_legacy_jsonl(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _seed_legacy(repo)
    fresh_task_store.cutover(repo, execute=True)
    canonical = repo / ".aiworkhub" / fresh_task_store.CANONICAL_REL
    assert fresh_task_store.empty_counts(canonical)["tasks"] == 0

    taskctl_text = (REPO / "AITools" / "taskctl.py").read_text(encoding="utf-8")
    (repo / "AITools" / "taskctl.py").write_text(taskctl_text, encoding="utf-8")
    cards = repo / "bitnnv2" / "data" / "tasking" / "machine_task_cards_v1.jsonl"
    cards.write_text(json.dumps({
        "task_id": "LEGACY_MUST_NOT_RETURN",
        "runner": "legacy",
        "status": "pending",
        "worker_status": "unclaimed",
        "objective": "legacy",
        "allowed_writes": ["x"],
        "forbidden": ["y"],
    }) + "\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(REPO / "tools" / "geoai-task-mcp" / "src")}
    env.pop("BITNN_TASK_QUEUE_DB", None)
    proc = subprocess.run(
        [sys.executable, str(repo / "AITools" / "taskctl.py"), "init-db"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "tasks=0" in proc.stdout
    assert fresh_task_store.empty_counts(canonical)["tasks"] == 0
