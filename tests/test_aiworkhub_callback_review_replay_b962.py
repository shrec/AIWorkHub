from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from aiworkhub import callback_store, task_engine, task_store


@pytest.mark.parametrize(
    "substatus",
    [
        "review_ready",
        "validation_failed",
        "scope_rejected",
        "launch_failed",
        "cancelled",
        "process_lost",
    ],
)
def test_repeated_terminal_transition_enqueues_once_per_launch_episode(
    tmp_path: Path, monkeypatch, substatus: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    readiness = task_store.storage_readiness(repo)
    task_id = f"CALLBACK_RETRY_EPISODE_{substatus.upper()}_B962"
    runner = "deepseek_retry_episode_worker"
    topic = "task_mcp"
    origin = str(uuid.uuid4())
    now = callback_store.utc_now()
    card = {
        "schema_id": "aiworkhub.task_card.v1",
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "status": "pending",
        "worker_status": "unclaimed",
        "callback_required": True,
        "coordinator_provider": "codex",
        "origin_thread_id": origin,
    }
    conn = callback_store.open_db(Path(readiness.canonical_db))
    try:
        callback_store.init_db(conn)
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, origin_thread_id, archived_at) "
            "VALUES (?, ?, ?, 'pending', 'unclaimed', 'normal', ?, ?, ?, ?, ?, '')",
            (task_id, runner, topic, "retry callback regression", json.dumps(card), now, now, origin),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")

    assert task_engine.claim_start_exact(
        repo, task_id, runner, topic, request_id="request-1"
    )["ok"]
    assert task_engine.mark_terminal_review(
        repo, task_id, runner, substatus, evidence={"request_id": "request-1"}
    )["callback_enqueued"]

    conn = callback_store.open_db(Path(readiness.canonical_db))
    try:
        batch = callback_store.claim_pending_callback_batch(conn, provider="codex")
        assert batch is not None
        callback_store.mark_batch_delivered(
            conn, batch["batch_id"], batch["lease_id"]
        )
        stored = json.loads(conn.execute(
            "SELECT card_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()["card_json"])
        stored.update(status="pending", worker_status="unclaimed")
        conn.execute(
            "UPDATE tasks SET status='pending', worker_status='unclaimed', claimed_by='', card_json=? "
            "WHERE task_id=?",
            (json.dumps(stored), task_id),
        )
        conn.commit()
    finally:
        conn.close()

    assert task_engine.claim_start_exact(
        repo, task_id, runner, topic, request_id="request-2"
    )["ok"]
    assert task_engine.mark_terminal_review(
        repo, task_id, runner, substatus, evidence={"request_id": "request-2"}
    )["callback_enqueued"]

    conn = callback_store.open_db(Path(readiness.canonical_db))
    try:
        rows = conn.execute(
            "SELECT transition, episode_id, request_id, state FROM callback_outbox "
            "WHERE task_id=? ORDER BY outbox_id", (task_id,)
        ).fetchall()
        expected = callback_store.resolve_callback_transition(substatus)
        assert [dict(row) for row in rows] == [
            {"transition": expected, "episode_id": "1", "request_id": "request-1", "state": "delivered"},
            {"transition": expected, "episode_id": "2", "request_id": "request-2", "state": "pending"},
        ]
        assert callback_store.enqueue_callback(
            conn, task_id, origin, substatus, provider="codex",
            episode_id="2", request_id="request-2",
        ) is False
    finally:
        conn.close()


def test_legacy_callback_required_false_cannot_suppress_review_wake(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    readiness = task_store.storage_readiness(repo)
    task_id = "CALLBACK_FALSE_MUST_STILL_WAKE_B962"
    runner = "legacy_worker"
    origin = str(uuid.uuid4())
    now = callback_store.utc_now()
    card = {
        "task_id": task_id,
        "runner": runner,
        "topic": "task_mcp",
        "callback_required": False,
        "coordinator_provider": "codex",
        "origin_thread_id": origin,
    }
    conn = callback_store.open_db(Path(readiness.canonical_db))
    try:
        callback_store.init_db(conn)
        conn.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,created_at,updated_at,origin_thread_id) "
            "VALUES(?,?,?,'processing','processing',?,?,?,?)",
            (task_id, runner, "task_mcp", json.dumps(card), now, now, origin),
        )
        conn.commit()
    finally:
        conn.close()

    result = task_engine.mark_terminal_review(
        repo, task_id, runner, "validation_failed", evidence={}
    )
    assert result["ok"] is True
    assert result["callback_enqueued"] is True


def test_review_repair_ignores_worker_substatus_and_legacy_opt_out(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    readiness = task_store.storage_readiness(repo)
    task_id = "CALLBACK_ANY_REVIEW_ROW_B962"
    origin = str(uuid.uuid4())
    now = callback_store.utc_now()
    card = {
        "task_id": task_id,
        "callback_required": False,
        "coordinator_provider": "codex",
        "origin_thread_id": origin,
        "terminal_substatus": "brand_new_failure_kind",
    }
    conn = callback_store.open_db(Path(readiness.canonical_db))
    try:
        callback_store.init_db(conn)
        conn.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,created_at,updated_at,origin_thread_id) "
            "VALUES(?,?,?,'review','brand_new_failure_kind',?,?,?,?)",
            (task_id, "worker", "task_mcp", json.dumps(card), now, now, origin),
        )
        conn.commit()
        assert callback_store.seed_missing_review_callbacks(conn, provider="codex") == 1
        row = conn.execute(
            "SELECT transition,state FROM callback_outbox WHERE task_id=?", (task_id,)
        ).fetchone()
        assert dict(row) == {"transition": "review_ready", "state": "pending"}
    finally:
        conn.close()


def test_seed_missing_review_callbacks_backfills_once(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = repo / "task_queue.sqlite"
    conn = callback_store.open_db(db_path)
    try:
        callback_store.init_db(conn)
        task_id = "CALLBACK_REVIEW_REPLAY_B962"
        origin_thread_id = str(uuid.uuid4())
        now = callback_store.utc_now()
        card = {
            "schema_id": "aiworkhub.task_card.v1",
            "task_id": task_id,
            "runner": "deepseek_v4flash",
            "topic": "task_mcp",
            "status": "review",
            "worker_status": "review",
            "callback_required": True,
            "coordinator_provider": "codex",
            "origin_thread_id": origin_thread_id,
            "claim_epoch": 0,
            "terminal_substatus": "validation_failed",
        }
        conn.execute(
            """
            INSERT INTO tasks (
              task_id, runner, topic, status, worker_status, priority, objective,
              card_json, created_at, updated_at, origin_thread_id, archived_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
            """,
            (
                task_id,
                "deepseek_v4flash",
                "task_mcp",
                "review",
                "review",
                "low",
                "callback replay regression",
                json.dumps(card, ensure_ascii=False, sort_keys=True),
                now,
                now,
                origin_thread_id,
            ),
        )
        conn.commit()

        assert callback_store.seed_missing_review_callbacks(conn, provider="codex") == 1
        assert callback_store.seed_missing_review_callbacks(conn, provider="codex") == 0
        row = conn.execute(
            "SELECT provider, origin_thread_id, transition, state FROM callback_outbox WHERE task_id=?",
            (task_id,),
        ).fetchone()
        assert dict(row) == {
            "provider": "codex",
            "origin_thread_id": origin_thread_id,
            "transition": "validation_failed",
            "state": "pending",
        }
    finally:
        conn.close()


def test_seed_missing_review_callbacks_can_retarget_verified_reloaded_route(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = repo / "task_queue.sqlite"
    conn = callback_store.open_db(db_path)
    try:
        callback_store.init_db(conn)
        old_origin = str(uuid.uuid4())
        new_origin = str(uuid.uuid4())
        task_id = "CALLBACK_REVIEW_REPLAY_RELOADED_ROUTE_B962"
        now = callback_store.utc_now()
        card = {
            "schema_id": "aiworkhub.task_card.v1",
            "task_id": task_id,
            "runner": "claude_worker",
            "topic": "task_mcp",
            "status": "review",
            "worker_status": "review",
            "callback_required": True,
            "coordinator_provider": "claude",
            "origin_thread_id": old_origin,
            "claim_epoch": 0,
        }
        conn.execute(
            """
            INSERT INTO tasks (
              task_id, runner, topic, status, worker_status, priority, objective,
              card_json, created_at, updated_at, origin_thread_id, archived_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
            """,
            (
                task_id,
                "claude_worker",
                "task_mcp",
                "review",
                "review",
                "low",
                "callback replay route retarget regression",
                json.dumps(card, ensure_ascii=False, sort_keys=True),
                now,
                now,
                old_origin,
            ),
        )
        conn.commit()

        assert callback_store.seed_missing_review_callbacks(
            conn, provider="claude", origin_thread_id=new_origin
        ) == 1
        row = conn.execute(
            "SELECT provider, origin_thread_id, transition, state FROM callback_outbox WHERE task_id=?",
            (task_id,),
        ).fetchone()
        assert dict(row) == {
            "provider": "claude",
            "origin_thread_id": new_origin,
            "transition": "review_ready",
            "state": "pending",
        }
    finally:
        conn.close()


def test_verified_route_recovers_superseded_blocked_terminal_callback(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = repo / "task_queue.sqlite"
    conn = callback_store.open_db(db_path)
    try:
        callback_store.init_db(conn)
        origin = str(uuid.uuid4())
        task_id = "CALLBACK_BLOCKED_TIMEOUT_RECOVERY_B962"
        now = callback_store.utc_now()
        card = {
            "task_id": task_id,
            "coordinator_provider": "codex",
            "origin_thread_id": origin,
            "claim_epoch": 2,
            "terminal_substatus": "timed_out",
        }
        conn.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,"
            "created_at,updated_at,origin_thread_id,archived_at) "
            "VALUES(?,?,?,'blocked','timed_out',?,?,?,?, '')",
            (
                task_id,
                "worker",
                "task_mcp",
                json.dumps(card),
                now,
                now,
                origin,
            ),
        )
        conn.execute(
            "INSERT INTO callback_outbox(task_id,provider,origin_thread_id,transition,"
            "episode_id,state,created_at,updated_at) VALUES(?,?,?,?,?,'superseded',?,?)",
            (task_id, "codex", origin, "timed_out", "2", now, now),
        )
        conn.commit()

        assert callback_store.seed_missing_review_callbacks(
            conn, provider="codex", origin_thread_id=origin
        ) == 1
        row = conn.execute(
            "SELECT transition,state,recovery_count,last_error FROM callback_outbox "
            "WHERE task_id=?",
            (task_id,),
        ).fetchone()
        assert dict(row) == {
            "transition": "timed_out",
            "state": "pending",
            "recovery_count": 1,
            "last_error": "verified_route_superseded_recovery",
        }
        assert callback_store.seed_missing_review_callbacks(
            conn, provider="codex", origin_thread_id=origin
        ) == 0
    finally:
        conn.close()
