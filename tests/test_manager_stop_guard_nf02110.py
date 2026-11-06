"""The direct-chat manager's Stop guard and watcher heartbeat (NF-2026-02110).

``tool_guard_hook --stop`` blocks a manager's stop while a card it dispatched
awaits a review decision, or is processing with no live
``manager_callback_watch`` heartbeat; it allows every other stop and fails open.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aiworkhub import tool_guard_hook as hook

SESSION = "claude-session-A"
OTHER = "claude-session-B"

Row = tuple[str, str, "str | None", "dict | None"]


def _db(root: Path, rows: list[Row], *, origin_column: bool = True) -> Path:
    path = root.joinpath(*hook.TASK_QUEUE_DB_PARTS)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        origin = ", origin_thread_id TEXT" if origin_column else ""
        conn.execute(
            "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, "
            "status TEXT NOT NULL DEFAULT 'pending', worker_status TEXT, "
            "card_json TEXT NOT NULL DEFAULT '{}', "
            f"archived_at TEXT NOT NULL DEFAULT ''{origin})"
        )
        conn.execute("CREATE INDEX idx_task_store_tasks_status ON tasks(status)")
        for task_id, status, origin_thread_id, card in rows:
            card_json = json.dumps(card or {})
            if origin_column:
                conn.execute(
                    "INSERT INTO tasks (task_id, status, card_json, origin_thread_id) "
                    "VALUES (?, ?, ?, ?)",
                    (task_id, status, card_json, origin_thread_id),
                )
            else:
                conn.execute(
                    "INSERT INTO tasks (task_id, status, card_json) VALUES (?, ?, ?)",
                    (task_id, status, card_json),
                )
        conn.commit()
    finally:
        conn.close()
    return path


def _heartbeat(root: Path, *, pid: int, age_seconds: float = 0.0, poll_interval: float = 2.0) -> Path:
    path = hook.manager_watch_heartbeat_path(root, SESSION)
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    path.write_text(
        json.dumps({"pid": pid, "updated_at": stamp.isoformat(), "poll_interval": poll_interval}),
        encoding="utf-8",
    )
    return path


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _payload(**extra: object) -> dict:
    return {"session_id": SESSION, "stop_hook_active": False, "hook_event_name": "Stop", **extra}


def test_blocks_on_an_undecided_review_card_of_this_session(tmp_path: Path) -> None:
    _db(tmp_path, [("NF-1", "review", SESSION, None), ("NF-2", "done", SESSION, None)])
    reason = hook.stop_decision(_payload(), tmp_path)
    assert reason is not None
    assert "NF-1" in reason and "NF-2" not in reason
    assert "aiworkhub_claude_callback_wait" in reason
    assert "ack" in reason


def test_does_not_block_on_another_sessions_card(tmp_path: Path) -> None:
    _db(tmp_path, [("NF-1", "review", OTHER, None), ("NF-2", "processing", OTHER, None)])
    assert hook.stop_decision(_payload(), tmp_path) is None


def test_blocks_on_processing_card_without_a_heartbeat(tmp_path: Path) -> None:
    _db(tmp_path, [("NF-3", "processing", SESSION, None)])
    reason = hook.stop_decision(_payload(), tmp_path)
    assert reason is not None
    assert "NF-3" in reason
    assert "-m aiworkhub.manager_callback_watch" in reason
    repo = tmp_path.as_posix()
    assert f"--repo {repo}" in reason or f'--repo "{repo}"' in reason
    assert "--provider claude" in reason
    assert f"--origin-thread-id {SESSION}" in reason
    assert "--timeout 86400" in reason


def test_allows_processing_card_with_a_fresh_live_heartbeat(tmp_path: Path) -> None:
    _db(tmp_path, [("NF-3", "processing", SESSION, None)])
    _heartbeat(tmp_path, pid=os.getpid())
    assert hook.stop_decision(_payload(), tmp_path) is None


def test_blocks_on_a_stale_heartbeat(tmp_path: Path) -> None:
    _db(tmp_path, [("NF-3", "processing", SESSION, None)])
    _heartbeat(tmp_path, pid=os.getpid(), age_seconds=60.0, poll_interval=2.0)
    reason = hook.stop_decision(_payload(), tmp_path)
    assert reason is not None and "manager_callback_watch" in reason


def test_blocks_on_a_heartbeat_whose_process_is_gone(tmp_path: Path) -> None:
    _db(tmp_path, [("NF-3", "processing", SESSION, None)])
    _heartbeat(tmp_path, pid=_dead_pid())
    assert hook.stop_decision(_payload(), tmp_path) is not None


def test_review_card_wins_over_a_live_heartbeat(tmp_path: Path) -> None:
    _db(tmp_path, [("NF-1", "review_ready", SESSION, None), ("NF-3", "processing", SESSION, None)])
    _heartbeat(tmp_path, pid=os.getpid())
    reason = hook.stop_decision(_payload(), tmp_path)
    assert reason is not None and "NF-1" in reason and "review" in reason


def test_stop_hook_active_allows_the_stop(tmp_path: Path) -> None:
    _db(tmp_path, [("NF-1", "review", SESSION, None)])
    assert hook.stop_decision(_payload(stop_hook_active=True), tmp_path) is None


def test_missing_database_allows_the_stop_and_creates_nothing(tmp_path: Path) -> None:
    assert hook.stop_decision(_payload(), tmp_path) is None
    assert not tmp_path.joinpath(*hook.TASK_QUEUE_DB_PARTS).exists()
    assert not (tmp_path / ".aiworkhub").exists()


def test_corrupt_database_allows_the_stop(tmp_path: Path) -> None:
    path = tmp_path.joinpath(*hook.TASK_QUEUE_DB_PARTS)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is not an sqlite database" * 64)
    assert hook.stop_decision(_payload(), tmp_path) is None


def test_foreign_schema_allows_the_stop(tmp_path: Path) -> None:
    path = tmp_path.joinpath(*hook.TASK_QUEUE_DB_PARTS)
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, state TEXT)")
    conn.commit()
    conn.close()
    assert hook.stop_decision(_payload(), tmp_path) is None


def test_card_json_origin_is_the_fallback_when_the_column_is_empty(tmp_path: Path) -> None:
    _db(
        tmp_path,
        [
            ("NF-5", "review", None, {"origin_thread_id": SESSION}),
            ("NF-6", "review", None, {"origin_thread_id": OTHER}),
        ],
    )
    reason = hook.stop_decision(_payload(), tmp_path)
    assert reason is not None and "NF-5" in reason and "NF-6" not in reason


def test_store_without_an_origin_column_reads_card_json(tmp_path: Path) -> None:
    _db(tmp_path, [("NF-7", "review", None, {"origin_thread_id": SESSION})], origin_column=False)
    reason = hook.stop_decision(_payload(), tmp_path)
    assert reason is not None and "NF-7" in reason


def test_archived_cards_are_ignored(tmp_path: Path) -> None:
    path = _db(tmp_path, [("NF-8", "review", SESSION, None)])
    conn = sqlite3.connect(path)
    conn.execute("UPDATE tasks SET archived_at = '2026-11-06T00:00:00Z'")
    conn.commit()
    conn.close()
    assert hook.stop_decision(_payload(), tmp_path) is None


def test_reason_lists_at_most_ten_cards(tmp_path: Path) -> None:
    _db(tmp_path, [(f"NF-{index:02d}", "review", SESSION, None) for index in range(13)])
    reason = hook.stop_decision(_payload(), tmp_path)
    assert reason is not None
    assert "NF-09" in reason and "NF-10" not in reason and "+3 more" in reason


def test_heartbeat_path_is_sanitised_and_rejects_empty_names(tmp_path: Path) -> None:
    path = hook.manager_watch_heartbeat_path(tmp_path, "../a b")
    assert path is not None
    assert path.parent == tmp_path.joinpath(*hook.MANAGER_WATCH_PARTS)
    assert path.name == ".._a_b.json"
    assert hook.manager_watch_heartbeat_path(tmp_path, "..") is None
    assert hook.manager_watch_heartbeat_path(tmp_path, "") is None
    assert hook.manager_watch_heartbeat_path(tmp_path, None) is None


def _run_stop(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    root: Path,
    data: bytes,
) -> str:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))
    assert hook.main(["--stop"]) == 0
    return capsys.readouterr().out


def test_main_stop_prints_one_block_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _db(tmp_path, [("NF-1", "review", SESSION, None)])
    out = _run_stop(monkeypatch, capsys, tmp_path, json.dumps(_payload()).encode())
    decision = json.loads(out)
    assert decision["decision"] == "block"
    assert "NF-1" in decision["reason"]


@pytest.mark.parametrize(
    "data",
    [b"", b"not json", b"[1, 2]", json.dumps(_payload(stop_hook_active=True)).encode()],
)
def test_main_stop_is_silent_when_it_allows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    data: bytes,
) -> None:
    _db(tmp_path, [("NF-1", "review", SESSION, None)])
    assert _run_stop(monkeypatch, capsys, tmp_path, data) == ""


def test_main_stop_fails_open_on_an_unexpected_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(payload: object, root: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(hook, "stop_decision", boom)
    assert _run_stop(monkeypatch, capsys, tmp_path, json.dumps(_payload()).encode()) == ""


def test_watcher_command_uses_forward_slash_paths_that_survive_git_bash(
    tmp_path: Path,
) -> None:
    python = tmp_path / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    cmd = hook.manager_watch_command(tmp_path, SESSION)
    assert "\\" not in cmd
    parts = shlex.split(cmd)
    assert parts[0] == python.as_posix()
    assert parts[parts.index("--repo") + 1] == tmp_path.as_posix()


def test_watcher_command_single_quotes_shell_metacharacters(tmp_path: Path) -> None:
    root = tmp_path / "a $(x) `y` b"
    cmd = hook.manager_watch_command(root, "s$id")
    assert f"--repo {shlex.quote(root.as_posix())}" in cmd
    assert "--origin-thread-id 's$id'" in cmd
    parts = shlex.split(cmd)
    assert parts[parts.index("--repo") + 1] == root.as_posix()
