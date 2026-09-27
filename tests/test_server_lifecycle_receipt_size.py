from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import server  # noqa: E402

_RECEIPT_BUDGET_BYTES = 8 * 1024


def _huge_card(task_id: str, *, status: str = "superseded") -> dict:
    return {
        "task_id": task_id,
        "runner": "codex",
        "topic": "metrics",
        "status": status,
        "worker_status": status,
        "terminal_review": {
            "substatus": "review_ready",
            "validation_log": "x" * 460_000,
        },
    }


def _canonical_envelope(card: dict, *, command: list[str]) -> dict:
    return {
        "ok": True,
        "returncode": 0,
        "command": command,
        "stdout": json.dumps(card, ensure_ascii=False),
        "stderr": "",
    }


def test_task_supersede_returns_a_bounded_receipt_not_the_full_card(monkeypatch) -> None:
    task_id = "AIWORKHUB_01181"
    card = _huge_card(task_id)
    assert len(json.dumps(card)) >= 400_000

    calls: list[dict] = []

    def fake_supersede_task(*, task_id, reason="", by=""):
        calls.append({"task_id": task_id, "reason": reason, "by": by})
        return _canonical_envelope(card, command=["supersede", task_id])

    monkeypatch.setattr(server.core, "supersede_task", fake_supersede_task)

    result = server.aiworkhub_task_supersede(task_id)
    encoded = json.dumps(result)

    assert len(encoded) < _RECEIPT_BUDGET_BYTES
    assert result["ok"] is True
    assert result["status"] == "superseded"
    assert "card_sha256" in result
    assert "card_bytes" in result
    assert "stdout" not in result
    assert "card" not in result
    assert len(calls) == 1


def test_task_supersede_include_card_full_returns_the_exact_card(monkeypatch) -> None:
    task_id = "AIWORKHUB_01181"
    card = _huge_card(task_id)

    monkeypatch.setattr(
        server.core,
        "supersede_task",
        lambda **kwargs: _canonical_envelope(card, command=["supersede", task_id]),
    )

    result = server.aiworkhub_task_supersede(task_id, include_card="full")

    assert result["card"] == card


def test_task_supersede_rejects_invalid_include_card_before_any_write(monkeypatch) -> None:
    calls: list[dict] = []

    def unexpected_supersede(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("core.supersede_task must not run for an invalid include_card")

    monkeypatch.setattr(server.core, "supersede_task", unexpected_supersede)

    result = server.aiworkhub_task_supersede("AIWORKHUB_01181", include_card="bogus")

    assert result["ok"] is False
    assert result["error"] == "invalid_include_card"
    assert calls == []


def _seed_task_db(tmp_path: Path, task_id: str, card: dict) -> Path:
    db_path = tmp_path / "task.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(server.task_store.SCHEMA)
    conn.execute(
        "INSERT INTO tasks (task_id, runner, topic, status, worker_status, card_json, "
        "created_at, updated_at, claimed_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id, "codex", "metrics", "review", "review",
            json.dumps(card), "now", "now", "codex",
        ),
    )
    conn.commit()
    conn.close()
    return db_path


def _bind_repo(monkeypatch, tmp_path: Path, db_path: Path) -> None:
    readiness = server.task_store.StorageReadiness(True, "ready", "repo-one", str(db_path))
    monkeypatch.setattr(server.core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(server.task_store, "_require_ready", lambda repo: (readiness, db_path))
    monkeypatch.setattr(server.task_store, "storage_readiness", lambda repo: readiness)
    monkeypatch.setattr(server.core, "writes_allowed", lambda: True)
    monkeypatch.setattr(server.core, "_verified_manager_actor", lambda: "codex")


def test_manager_task_supersede_is_bounded_even_with_a_huge_stored_card(
    monkeypatch, tmp_path: Path
) -> None:
    task_id = "AIWORKHUB_MGR_SUPERSEDE"
    card = _huge_card(task_id, status="review")
    db_path = _seed_task_db(tmp_path, task_id, card)
    _bind_repo(monkeypatch, tmp_path, db_path)

    result = server.aiworkhub_manager_task_supersede(task_id, reason="orphan")
    encoded = json.dumps(result)

    assert result["ok"] is True
    assert "validation_log" not in encoded
    assert len(encoded) < _RECEIPT_BUDGET_BYTES


def test_manager_task_archive_is_bounded_even_with_a_huge_stored_card(
    monkeypatch, tmp_path: Path
) -> None:
    task_id = "AIWORKHUB_MGR_ARCHIVE"
    card = _huge_card(task_id, status="review")
    db_path = _seed_task_db(tmp_path, task_id, card)
    _bind_repo(monkeypatch, tmp_path, db_path)

    result = server.aiworkhub_manager_task_archive(task_id, reason="done")
    encoded = json.dumps(result)

    assert result["ok"] is True
    assert "validation_log" not in encoded
    assert len(encoded) < _RECEIPT_BUDGET_BYTES
