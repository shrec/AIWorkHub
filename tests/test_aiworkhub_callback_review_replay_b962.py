from __future__ import annotations

import json
import uuid
from pathlib import Path

from aiworkhub import callback_store


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
