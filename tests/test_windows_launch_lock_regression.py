from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from aiworkhub import core
from aiworkhub.process_launcher import LaunchRejected, ProcessManager


def _manager(tmp_path: Path) -> ProcessManager:
    return ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "processes.jsonl",
        process_dir=tmp_path / "processes",
        isolation_enabled=False,
    )


def test_long_request_finalization_does_not_block_launch_registry(tmp_path: Path) -> None:
    finalizer = _manager(tmp_path)
    launcher = _manager(tmp_path)

    with finalizer._request_lock("finished-request"):
        started = time.monotonic()
        with launcher._registry_lock():
            pass
        elapsed = time.monotonic() - started

    assert elapsed < 1.0


def test_long_review_promotion_does_not_block_launch_registry(tmp_path: Path) -> None:
    reviewer = _manager(tmp_path)
    launcher = _manager(tmp_path)

    with reviewer._promotion_lock():
        started = time.monotonic()
        with launcher._registry_lock():
            pass
        elapsed = time.monotonic() - started

    assert elapsed < 1.0


def test_launch_reservation_releases_registry_before_expensive_setup(
    tmp_path: Path,
) -> None:
    provisioner = _manager(tmp_path)
    concurrent_launcher = _manager(tmp_path)
    event = {
        "request_id": "a" * 32,
        "task_id": "TASK_A",
        "runner": "worker_a",
        "topic": "code",
        "adapter_id": "glm_vscode_lm",
    }

    with provisioner._launch_reservation(event):
        assert provisioner._active_count() == 1
        started = time.monotonic()
        with concurrent_launcher._registry_lock():
            pass
        elapsed = time.monotonic() - started

    assert elapsed < 1.0


def test_launch_reservation_blocks_duplicate_task_before_pid_exists(
    tmp_path: Path,
) -> None:
    first = _manager(tmp_path)
    second = _manager(tmp_path)
    event = {
        "request_id": "b" * 32,
        "task_id": "TASK_DUPLICATE",
        "runner": "worker_a",
        "topic": "code",
        "adapter_id": "deepseek_vscode_lm",
    }

    with first._launch_reservation(event):
        with pytest.raises(LaunchRejected, match="duplicate_reserved_task"):
            with second._launch_reservation({**event, "request_id": "c" * 32}):
                pass


def test_same_request_finalizers_remain_serialized(tmp_path: Path) -> None:
    first = _manager(tmp_path)
    second = _manager(tmp_path)
    entered = threading.Event()

    def contender() -> None:
        with second._request_lock("same-request"):
            entered.set()

    with first._request_lock("same-request"):
        thread = threading.Thread(target=contender, daemon=True)
        thread.start()
        assert not entered.wait(0.15)
    thread.join(timeout=5)
    assert entered.is_set()


def test_finalizer_uses_request_lock_not_global_registry(
    tmp_path: Path, monkeypatch,
) -> None:
    manager = _manager(tmp_path)
    observed: list[str] = []

    @contextmanager
    def request_lock(request_id: str):
        observed.append(request_id)
        yield

    @contextmanager
    def forbidden_registry_lock():
        raise AssertionError("finalizer_must_not_hold_global_launch_registry")
        yield

    monkeypatch.setattr(manager, "_request_lock", request_lock)
    monkeypatch.setattr(manager, "_registry_lock", forbidden_registry_lock)
    monkeypatch.setattr(manager, "_request_events", lambda request_id: [])

    assert manager._finalize_isolated_request("request-1") is None
    assert observed == ["request-1"]


def test_review_acceptance_uses_promotion_lock_not_launch_registry(
    tmp_path: Path, monkeypatch,
) -> None:
    manager = _manager(tmp_path)
    entered: list[bool] = []

    @contextmanager
    def promotion_lock():
        entered.append(True)
        yield

    @contextmanager
    def forbidden_registry_lock():
        raise AssertionError("review_must_not_hold_global_launch_registry")
        yield

    monkeypatch.setattr(manager, "_promotion_lock", promotion_lock)
    monkeypatch.setattr(manager, "_registry_lock", forbidden_registry_lock)
    monkeypatch.setattr(manager, "_request_events", lambda request_id: [])

    result = manager.accept_review("request-1", "task-1")

    assert result["error"] == "request_not_found"
    assert entered == [True]


def test_create_and_launch_share_dotted_dashed_topic_grammar() -> None:
    for value in ("quality.review-v1", "source.graph:semantic-v2"):
        assert core._TASK_IDENTITY_RE.fullmatch(value)
        assert core._is_malformed_identity_token(value) is None

    for value in ("../escape", "bad topic", "topic;rm"):
        assert core._is_malformed_identity_token(value) == "invalid_characters"
