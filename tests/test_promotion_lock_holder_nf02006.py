"""NF-2026-02006: the promotion lock names its holder.

Measured 2026-11-02/03: ``accept_review`` from one manager seat failed three
times with ``windows_advisory_lock_timeout after 20s (last errno EACCES)``
while the other seat's accept held ``ProcessManager._promotion_lock`` for its
whole combined-tree validation. The waiter could not tell a busy peer from a
stale lock. The holder now records itself beside the lock and a timed-out
waiter gets a typed ``promotion_busy`` refusal naming it.

No test waits on the real 20 s bound: the timeout is injected by replacing the
``lock_fd`` that ``process_launcher`` calls.
"""

from __future__ import annotations

import errno
import json
import os
import sys
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    platform_io,
    process_launcher,
    process_launcher_accept_review,
    promotion_lock_holder,
)

TASK_ID = "T-nf02006"
REQUEST_ID = "R-nf02006"
HOLDER_TASK = "T-holder"
HOLDER_REQUEST = "R-holder"


class _Manager:
    _request_lock = process_launcher.ProcessManager._request_lock
    _promotion_lock = process_launcher.ProcessManager._promotion_lock

    def __init__(self, process_dir: Path) -> None:
        self.process_dir = process_dir

    def _request_events(self, request_id: str) -> list[dict]:
        return [{"request_id": request_id, "task_id": TASK_ID}]

    def _show_task(self, task_id: str) -> dict:
        return {"returncode": 0, "stdout": "{}"}


def _sidecar(process_dir: Path) -> Path:
    return process_dir / ".promotion.lock.holder.json"


def _write_holder(process_dir: Path, *, pid: int, held_for: float = 5.0) -> None:
    acquired = datetime.now(timezone.utc) - timedelta(seconds=held_for)
    _sidecar(process_dir).write_text(
        json.dumps(
            {
                "pid": pid,
                "task_id": HOLDER_TASK,
                "request_id": HOLDER_REQUEST,
                "operation": "accept_review",
                "acquired_at": acquired.isoformat(),
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def process_dir(tmp_path: Path) -> Path:
    path = tmp_path / "process_dir"
    path.mkdir()
    return path


@pytest.fixture
def lock_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def _timeout(fd: int, *, blocking: bool = True) -> None:
        raise platform_io.AdvisoryLockTimeout(
            "windows_advisory_lock_timeout after 20s (last errno EACCES)",
            errno=errno.EACCES,
        )

    monkeypatch.setattr(process_launcher, "lock_fd", _timeout)


def _refusal(manager: _Manager) -> process_launcher.PromotionBusy:
    with pytest.raises(process_launcher.PromotionBusy) as caught:
        with manager._promotion_lock(task_id=TASK_ID, request_id=REQUEST_ID):
            pytest.fail("the lock must not be entered after a timeout")
    return caught.value


def test_timeout_names_a_live_holder(process_dir: Path, lock_times_out: None) -> None:
    _write_holder(process_dir, pid=os.getpid(), held_for=5.0)

    busy = _refusal(_Manager(process_dir))

    # Mutation proof: with the holder read dropped (see the next test) these
    # holder assertions fail, so they really measure the record read back.
    assert isinstance(busy, process_launcher.LaunchRejected)
    payload = busy.diagnostic
    assert payload["error"] == "promotion_busy"
    assert payload["holder"]["task_id"] == HOLDER_TASK
    assert payload["holder"]["request_id"] == HOLDER_REQUEST
    assert payload["holder"]["operation"] == "accept_review"
    assert payload["holder_stale"] is False
    assert payload["held_for_s"] >= 5.0
    assert payload["retry_after_s"] > 0


def test_dropping_the_holder_read_loses_the_holder_name(
    process_dir: Path, lock_times_out: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mutation for the test above: a refusal that never reads the record
    cannot name T/R, so the live-holder assertions would fail."""
    _write_holder(process_dir, pid=os.getpid())
    monkeypatch.setattr(promotion_lock_holder, "read_promotion_holder", lambda _p: None)

    payload = _refusal(_Manager(process_dir)).diagnostic

    assert payload["holder"] is None
    assert payload["holder_stale"] is None


def test_dead_holder_pid_is_reported_stale(
    process_dir: Path, lock_times_out: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead_pid = 2_000_000_001
    probed: list[int] = []

    def _alive(pid: int) -> bool:
        probed.append(pid)
        return pid != dead_pid

    # The shared runtime liveness helper is the one consulted; it is replaced
    # here only so "dead" is deterministic rather than a racing exited child.
    monkeypatch.setattr(promotion_lock_holder, "process_is_alive", _alive)
    _write_holder(process_dir, pid=dead_pid)

    payload = _refusal(_Manager(process_dir)).diagnostic

    assert probed == [dead_pid]
    assert payload["holder"]["task_id"] == HOLDER_TASK
    assert payload["holder_stale"] is True


def test_shared_liveness_helper_is_reused() -> None:
    assert promotion_lock_holder.process_is_alive is platform_io.process_is_alive


@pytest.mark.parametrize("content", [None, "", "{not json", "[1, 2]", '{"pid": "x"}'])
def test_missing_or_unreadable_holder_never_crashes(
    process_dir: Path, lock_times_out: None, content: str | None
) -> None:
    if content is not None:
        _sidecar(process_dir).write_text(content, encoding="utf-8")

    payload = _refusal(_Manager(process_dir)).diagnostic

    assert payload["error"] == "promotion_busy"
    assert payload["retry_after_s"] > 0
    assert payload["holder_stale"] is None
    if content in (None, "", "{not json", "[1, 2]"):
        assert payload["holder"] is None
        assert payload["held_for_s"] is None


def test_acquire_writes_holder_and_release_removes_it(process_dir: Path) -> None:
    manager = _Manager(process_dir)
    assert not _sidecar(process_dir).exists()

    with manager._promotion_lock(task_id=TASK_ID, request_id=REQUEST_ID):
        record = json.loads(_sidecar(process_dir).read_text(encoding="utf-8"))
        assert record["pid"] == os.getpid()
        assert record["task_id"] == TASK_ID
        assert record["request_id"] == REQUEST_ID
        assert record["operation"] == "accept_review"
        acquired = datetime.fromisoformat(record["acquired_at"])
        assert acquired.tzinfo is not None

    assert not _sidecar(process_dir).exists()


def test_release_removes_holder_on_exception(process_dir: Path) -> None:
    manager = _Manager(process_dir)
    with pytest.raises(RuntimeError):
        with manager._promotion_lock(task_id=TASK_ID, request_id=REQUEST_ID):
            assert _sidecar(process_dir).exists()
            raise RuntimeError("boom")
    assert not _sidecar(process_dir).exists()


def test_accept_review_refused_early_never_waits_on_the_promotion_lock(
    process_dir: Path, lock_times_out: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NF-2026-02026: a busy lock no longer refuses an accept up front; the
    ``promotion_busy`` reply after validation is asserted, through the real
    body, in ``test_accept_validation_outside_lock_nf02026.py``."""
    monkeypatch.setattr(
        process_launcher,
        "_parse_card",
        lambda raw, task_id: {"terminal_review": {"evidence": {"changed_paths": ["a.py"]}}},
    )
    monkeypatch.setattr(process_launcher, "_canonical_task_status", lambda card: "review")
    monkeypatch.setattr(
        process_launcher, "_card_is_readonly_quality_review", lambda card: False
    )
    monkeypatch.setattr(process_launcher, "_card_is_readonly_research", lambda card: False)
    monkeypatch.setattr(_Manager, "_request_lock", lambda self, request_id: nullcontext())
    _write_holder(process_dir, pid=os.getpid())
    manager = _Manager(process_dir)

    first = process_launcher_accept_review.accept_review(manager, REQUEST_ID, TASK_ID)
    # The in-flight claim is released on the refusal: a retry reaches the same
    # identity refusal, never its own leftover ``accept_in_progress`` marker.
    second = process_launcher_accept_review.accept_review(manager, REQUEST_ID, TASK_ID)

    for result in (first, second):
        assert result["ok"] is False
        assert result["error"] == "request_identity_incomplete"
        assert result["task_id"] == TASK_ID
        assert result["request_id"] == REQUEST_ID
        assert "advisory_lock_timeout" not in json.dumps(result)


@pytest.mark.parametrize("pid", [2**31, 2**70, -5])
def test_out_of_range_holder_pid_is_unknown_not_a_crash(
    process_dir: Path, lock_times_out: None, monkeypatch: pytest.MonkeyPatch, pid: int
) -> None:
    probed: list[int] = []

    def _alive(candidate: int) -> bool:
        probed.append(candidate)
        # The real probe raises OverflowError / ctypes.ArgumentError here.
        raise OverflowError("pid out of range")

    monkeypatch.setattr(promotion_lock_holder, "process_is_alive", _alive)
    _write_holder(process_dir, pid=pid)

    payload = _refusal(_Manager(process_dir)).diagnostic

    assert probed == []
    assert payload["error"] == "promotion_busy"
    assert payload["holder_stale"] is None


def test_holder_echo_keeps_only_recorded_scalar_fields(
    process_dir: Path, lock_times_out: None
) -> None:
    _sidecar(process_dir).write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "task_id": HOLDER_TASK,
                "request_id": {"nested": "x" * 1000},
                "operation": True,
                "acquired_at": datetime.now(timezone.utc).isoformat(),
                "env": {"TOKEN": "secret"},
            }
        ),
        encoding="utf-8",
    )

    holder = _refusal(_Manager(process_dir)).diagnostic["holder"]

    assert set(holder) == {"pid", "task_id", "acquired_at"}
    assert holder["task_id"] == HOLDER_TASK
    assert "secret" not in json.dumps(holder)
