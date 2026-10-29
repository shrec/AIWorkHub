"""NF-2026-01813: no test reads the developer's live Manager Chat seat by default."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiworkhub import callback_store, core

_SESSION_ID = "mls-" + "ab" * 16


def _write_fake_live_seat(repo: Path) -> None:
    state = repo / ".aiworkhub" / "runtime" / "manager_loop"
    (state / "sessions").mkdir(parents=True)
    (state / "selected.json").write_text(json.dumps({"session_id": _SESSION_ID}), encoding="utf-8")
    (state / "sessions" / f"{_SESSION_ID}.json").write_text(
        json.dumps({"session_id": _SESSION_ID, "backend_id": "claude", "model": "opus"}),
        encoding="utf-8",
    )


@pytest.fixture
def live_seat_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_fake_live_seat(repo)
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    monkeypatch.setattr(callback_store, "manager_chat_record_holds_seat", lambda *a, **k: True)
    return repo


def test_unmarked_test_sees_no_seat_even_when_a_live_seat_is_on_disk(live_seat_repo: Path) -> None:
    assert core._active_manager_chat_record() is None


@pytest.mark.live_manager_chat_seat
def test_marked_test_reaches_the_real_function(live_seat_repo: Path) -> None:
    assert core._active_manager_chat_record() == {
        "session_id": _SESSION_ID,
        "backend_id": "claude",
        "model": "opus",
        "status": "active",
    }


def test_a_local_patch_still_overrides_the_hermetic_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    record = {"session_id": _SESSION_ID, "backend_id": "b", "model": "m", "status": "active"}
    monkeypatch.setattr(core, "_active_manager_chat_record", lambda: record)
    assert core._active_manager_chat_record() is record
