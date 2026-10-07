"""NF-2026-01383: Manager Chat seat identity fails closed.

The Manager Chat seat MCP server only owns manager authority when
``AIWORKHUB_MANAGER_SEAT_BACKEND`` and ``AIWORKHUB_MANAGER_SEAT_TOKEN``
match the per-backend 0600 token file and the active selected Manager
Chat record; these tests pin that fail-closed contract end to end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import core


@pytest.fixture(autouse=True)
def _clear_seat_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIWORKHUB_MANAGER_SEAT_BACKEND", raising=False)
    monkeypatch.delenv("AIWORKHUB_MANAGER_SEAT_TOKEN", raising=False)


def _seat_token_file(root: Path, backend: str) -> Path:
    return root / ".aiworkhub" / "runtime" / "manager-seat" / f"seat-token-{backend}"


def _provision_seat(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    backend: str,
    token: str,
) -> None:
    monkeypatch.setenv("AIWORKHUB_MANAGER_SEAT_BACKEND", backend)
    monkeypatch.setenv("AIWORKHUB_MANAGER_SEAT_TOKEN", token)
    seat_token = _seat_token_file(root, backend)
    seat_token.parent.mkdir(parents=True, exist_ok=True)
    seat_token.write_text(token, encoding="utf-8")


def _bind_record(
    monkeypatch: pytest.MonkeyPatch,
    record: dict[str, str] | None,
) -> None:
    monkeypatch.setattr(core, "_active_manager_chat_record", lambda: record)


def test_matching_token_and_record_is_a_manager_chat_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    record = {"backend_id": "claude_cli", "model": "opus-4",
               "session_id": "mls-seat-1"}
    _bind_record(monkeypatch, record)
    _provision_seat(monkeypatch, tmp_path, "claude_cli", "a" * 64)
    assert core._manager_chat_seat_identity() == {
        "provider": "manager_chat",
        "session_id": "mls-seat-1",
        "thread_id": "mls-seat-1",
        "window_id": "",
        "route_state": "ready",
        "callback_supported": "true",
        "backend_id": "claude_cli",
        "model": "opus-4",
    }


def test_wrong_token_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    _bind_record(
        monkeypatch,
        {"backend_id": "codex_cli", "model": "gpt-5", "session_id": "mls-seat-2"},
    )
    _provision_seat(monkeypatch, tmp_path, "codex_cli", "f" * 64)
    monkeypatch.setenv("AIWORKHUB_MANAGER_SEAT_TOKEN", "e" * 64)
    assert core._manager_chat_seat_identity() is None


def test_record_backend_mismatch_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    _bind_record(
        monkeypatch,
        {"backend_id": "codex_cli", "model": "gpt-5", "session_id": "mls-seat-3"},
    )
    _provision_seat(monkeypatch, tmp_path, "opencode_cli", "a" * 64)
    assert core._manager_chat_seat_identity() is None


def test_no_selected_record_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    _bind_record(monkeypatch, None)
    _provision_seat(monkeypatch, tmp_path, "claude_cli", "b" * 64)
    assert core._manager_chat_seat_identity() is None


def test_traversal_backend_is_refused_before_any_file_or_record_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    _bind_record(
        monkeypatch,
        {"backend_id": "../x", "model": "m", "session_id": "mls-seat-4"},
    )
    monkeypatch.setenv("AIWORKHUB_MANAGER_SEAT_BACKEND", "../x")
    monkeypatch.setenv("AIWORKHUB_MANAGER_SEAT_TOKEN", "c" * 64)
    assert core._manager_chat_seat_identity() is None


def test_verify_coordinator_capability_trusts_the_manager_chat_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    _bind_record(
        monkeypatch,
        {"backend_id": "claude_cli", "model": "opus-4", "session_id": "mls-seat-5"},
    )
    _provision_seat(monkeypatch, tmp_path, "claude_cli", "d" * 64)
    assert core._verify_coordinator_capability(None) == (
        True,
        "trusted_manager_chat_seat_route",
    )


def test_decision_actor_records_the_manager_chat_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiworkhub import learning_commit_store

    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    _bind_record(
        monkeypatch,
        {"backend_id": "codex_cli", "model": "gpt-5", "session_id": "mls-seat-6"},
    )
    _provision_seat(monkeypatch, tmp_path, "codex_cli", "e" * 64)
    actor = learning_commit_store._decision_actor("T1")
    assert actor is not None
    assert actor["provider"] == "manager_chat"
    assert actor["session_id"] == "mls-seat-6"
