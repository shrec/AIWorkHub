"""NF-2026-02430: a Codex/Claude-created card's callbacks stay on its creator.

A card whose ``origin_thread_id`` is a Codex/Claude UUID is filed under its own
provider and that UUID even when the seated Manager Chat session stamped
``manager_chat_session_id`` on it, and ``mirror_pending_to_manager_chat`` leaves
its lane row alone. A stamped card with an ``mls-`` or empty origin still
routes to Manager Chat.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import callback_store  # noqa: E402

CLAUDE_UUID = "8c878507-e8f0-43a9-8076-d5ab04da3952"
SEATED_CHAT = "mls-" + "ff" * 16
CREATOR_CHAT = "mls-" + "1a" * 16


@pytest.fixture()
def conn(tmp_path: Path) -> Any:
    connection = sqlite3.connect(str(tmp_path / "task_queue.sqlite"), isolation_level=None)
    connection.row_factory = sqlite3.Row
    callback_store.init_db(connection)
    try:
        yield connection
    finally:
        connection.close()


def _insert_review_card(conn: sqlite3.Connection, task_id: str, card: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO tasks (task_id, status, card_json, created_at, updated_at) "
        "VALUES (?, 'review', ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
        (task_id, json.dumps({"claim_epoch": 0, **card})),
    )


def _outbox_rows(conn: sqlite3.Connection, task_id: str) -> list[tuple[str, str, str]]:
    return [
        (str(row["provider"]), str(row["origin_thread_id"]), str(row["state"]))
        for row in conn.execute(
            "SELECT provider, origin_thread_id, state FROM callback_outbox "
            "WHERE task_id=? ORDER BY outbox_id",
            (task_id,),
        ).fetchall()
    ]


def _insert_uuid_card_with_stamp(conn: sqlite3.Connection, task_id: str) -> None:
    _insert_review_card(
        conn,
        task_id,
        {
            "coordinator_provider": "claude",
            "origin_thread_id": CLAUDE_UUID,
            "manager_chat_session_id": SEATED_CHAT,
        },
    )


def test_uuid_origin_card_is_filed_under_its_own_provider_despite_a_stamp(conn: sqlite3.Connection) -> None:
    _insert_uuid_card_with_stamp(conn, "CARD_UUID")

    assert callback_store.manager_chat_callback_route(conn, "CARD_UUID", "claude", CLAUDE_UUID) == (
        "claude",
        CLAUDE_UUID,
    )
    assert callback_store.enqueue_callback(
        conn, "CARD_UUID", CLAUDE_UUID, "review_ready", provider="claude", episode_id="0"
    )

    assert _outbox_rows(conn, "CARD_UUID") == [("claude", CLAUDE_UUID, "pending")]


def test_mirror_leaves_a_uuid_origin_card_on_its_creator(conn: sqlite3.Connection) -> None:
    _insert_uuid_card_with_stamp(conn, "CARD_UUID_MIRROR")
    assert callback_store.enqueue_callback(
        conn, "CARD_UUID_MIRROR", CLAUDE_UUID, "review_ready", provider="claude", episode_id="0"
    )

    assert callback_store.mirror_pending_to_manager_chat(conn, SEATED_CHAT) == 0
    assert _outbox_rows(conn, "CARD_UUID_MIRROR") == [("claude", CLAUDE_UUID, "pending")]


@pytest.mark.parametrize(
    "origin",
    [pytest.param(CREATOR_CHAT, id="mls_origin"), pytest.param("", id="empty_origin")],
)
def test_stamped_card_with_mls_or_empty_origin_still_routes_to_manager_chat(
    conn: sqlite3.Connection, origin: str
) -> None:
    card: dict[str, Any] = {"coordinator_provider": "claude", "manager_chat_session_id": SEATED_CHAT}
    if origin:
        card["origin_thread_id"] = origin
    _insert_review_card(conn, "CARD_CHAT", card)

    assert callback_store.enqueue_callback(
        conn, "CARD_CHAT", origin or "claude-thread", "review_ready", provider="claude", episode_id="0"
    )

    assert _outbox_rows(conn, "CARD_CHAT") == [("manager_chat", SEATED_CHAT, "pending")]
