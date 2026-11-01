"""NF-2026-01911: a contended registry lock must not strand a reviewer launch.

``_reviewer_spawn_transition`` retries a transient advisory-lock timeout, and the
timeout that finally escapes names the holder recorded in the lock's sidecar.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from aiworkhub import platform_io, process_launcher

HOLD_THREAD_NAME = "registry-lock-holder"


def _manager(tmp_path: Path) -> process_launcher.ProcessManager:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "proc" / "process_events.jsonl",
        process_dir=tmp_path / "proc",
        isolation_enabled=False,
        collision_guard=lambda **_kwargs: {"returncode": 0},
    )


def _seed_starting(manager: process_launcher.ProcessManager, request_id: str) -> None:
    manager._append_event(
        {
            "request_id": request_id,
            "task_id": "REVIEWER_NF01911",
            "runner": "deepseek_v4-pro",
            "topic": "quality_review",
            "adapter_id": "vscode_lm",
            "state": "starting",
            "reservation_expires_at_epoch": time.time() + 120.0,
            "quality_review_attempt": {
                "target_request_id": "target-req",
                "target_task_id": "TARGET_TASK",
                "lens": "correctness",
            },
        }
    )


def _hold_registry_lock(
    manager: process_launcher.ProcessManager,
    acquired: threading.Event,
    release: threading.Event,
) -> None:
    with manager._registry_lock():
        acquired.set()
        release.wait(timeout=30.0)


@contextmanager
def _held_by_thread(
    manager: process_launcher.ProcessManager,
) -> Iterator[threading.Event]:
    acquired = threading.Event()
    release = threading.Event()
    holder = threading.Thread(
        target=_hold_registry_lock,
        args=(manager, acquired, release),
        name=HOLD_THREAD_NAME,
        daemon=True,
    )
    holder.start()
    try:
        assert acquired.wait(timeout=10.0)
        yield release
    finally:
        release.set()
        holder.join(timeout=10.0)


def test_spawn_transition_retries_through_a_transient_registry_lock_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform_io, "ADVISORY_LOCK_MAX_WAIT_SECONDS", 0.3)
    timeouts: list[TimeoutError] = []
    real_lock_fd = process_launcher.lock_fd

    def counting_lock_fd(fd: int, *, blocking: bool = True) -> None:
        try:
            real_lock_fd(fd, blocking=blocking)
        except TimeoutError as exc:
            timeouts.append(exc)
            raise

    monkeypatch.setattr(process_launcher, "lock_fd", counting_lock_fd)
    holder_manager = _manager(tmp_path)
    contender = _manager(tmp_path)
    _seed_starting(contender, "nf01911-retry")

    with _held_by_thread(holder_manager) as release:
        threading.Timer(0.5, release.set).start()
        assert contender._reviewer_spawn_transition("nf01911-retry", binding={}) is True

    assert timeouts, "the first attempt never met a contended lock"
    assert contender._latest_by_request()["nf01911-retry"]["state"] == (
        "provider_spawn_committed"
    )
    lock_path = Path(f"{contender.process_log_path}.lock")
    assert platform_io.describe_lock_holder(lock_path) == "holder=unknown"


def test_exhausted_registry_lock_timeout_names_the_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform_io, "ADVISORY_LOCK_MAX_WAIT_SECONDS", 0.2)
    holder_manager = _manager(tmp_path)
    contender = _manager(tmp_path)
    _seed_starting(contender, "nf01911-exhausted")

    with _held_by_thread(holder_manager):
        with pytest.raises(TimeoutError) as excinfo:
            contender._reviewer_spawn_transition("nf01911-exhausted", binding={})

    message = str(excinfo.value)
    assert "advisory_lock_timeout" in message
    assert f"holder={os.getpid()}:" in message
    assert "_hold_registry_lock" in message
    assert "held_ms=" in message
    assert contender._latest_by_request()["nf01911-exhausted"]["state"] == "starting"


def test_timeout_without_a_holder_record_reports_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def contended(fd: int, *, blocking: bool = True) -> None:
        raise TimeoutError("posix_advisory_lock_timeout after 20s (last errno EAGAIN)")

    monkeypatch.setattr(process_launcher, "lock_fd", contended)
    manager = _manager(tmp_path)

    with pytest.raises(TimeoutError, match=r"EAGAIN\) holder=unknown"):
        with manager._registry_lock():
            pytest.fail("the lock must not be acquired")


def test_non_finite_holder_time_reports_unknown(tmp_path: Path) -> None:
    lock_path = tmp_path / "registry.lock"
    sidecar = platform_io.lock_holder_sidecar_path(lock_path)
    for stamp in ("Infinity", "-Infinity", "NaN"):
        sidecar.write_text(
            f'{{"pid": 1, "label": "x", "acquired_at": {stamp}}}', encoding="utf-8"
        )
        assert platform_io.describe_lock_holder(lock_path) == "holder=unknown"
