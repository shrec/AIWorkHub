from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from aiworkhub import host_heavy_slots, worker_workspace
from aiworkhub.host_heavy_slots import HELD_ENV, slotted, try_acquire, wait_acquire

_SUITE_HELD = os.environ.get(HELD_ENV)

SLOTS = 2


@pytest.fixture(autouse=True)
def slots_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "host-slots"
    monkeypatch.setattr(host_heavy_slots, "default_slots_dir", lambda: directory)
    monkeypatch.setattr(host_heavy_slots, "slot_count", lambda cpu_capacity=None: SLOTS)
    monkeypatch.delenv(HELD_ENV, raising=False)
    return directory


def _rows(**kwargs: object) -> list[dict[str, object]]:
    return [{"command": "x", "returncode": 0}]


def _hold_all() -> list[host_heavy_slots.HostSlot]:
    held = [try_acquire(count=SLOTS) for _ in range(SLOTS)]
    assert all(slot is not None for slot in held)
    return [slot for slot in held if slot is not None]


def test_run_validations_is_slotted() -> None:
    assert hasattr(worker_workspace.run_validations, "__wrapped__")


def test_waits_for_a_free_slot_then_runs() -> None:
    held = _hold_all()
    timer = threading.Timer(0.3, held[0].release)
    timer.start()
    try:
        rows = slotted(max_wait_seconds=30)(_rows)(timeout_seconds=30)
    finally:
        timer.join()
        for slot in held:
            slot.release()
    assert rows[0]["host_slot_wait"]["reason"] == "acquired_after_wait"
    assert rows[0]["host_slot_wait"]["acquired"] is True
    assert rows[0]["host_slot_wait"]["slots"] == SLOTS


def test_free_slot_rows_equal_undecorated_result() -> None:
    assert slotted(max_wait_seconds=5)(_rows)() == _rows()


@pytest.mark.parametrize("fail", [False, True])
def test_slot_released_on_every_exit_path(fail: bool) -> None:
    def body(**kwargs: object) -> list[dict[str, object]]:
        assert try_acquire(count=1) is None
        if fail:
            raise RuntimeError("boom")
        return _rows()

    wrapped = slotted(max_wait_seconds=5)(body)
    if fail:
        with pytest.raises(RuntimeError):
            wrapped()
    else:
        wrapped()
    again = try_acquire(count=1)
    assert again is not None
    again.release()


def test_held_env_takes_no_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HELD_ENV, "1")
    held = _hold_all()
    try:
        assert slotted(max_wait_seconds=0)(_rows)() == _rows()
    finally:
        for slot in held:
            slot.release()


def test_unusable_slots_dir_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(host_heavy_slots, "default_slots_dir", lambda: blocker / "slots")
    rows = slotted(max_wait_seconds=5)(_rows)()
    assert rows[0]["host_slot_wait"]["reason"] == "slots_dir_unusable"
    assert rows[0]["host_slot_wait"]["acquired"] is False


def test_wait_acquire_times_out_with_injected_clock() -> None:
    held = _hold_all()
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    try:
        slot, waited, reason = wait_acquire(
            5.0, poll_seconds=2.0, count=SLOTS, sleep=sleep, clock=lambda: now[0]
        )
    finally:
        for item in held:
            item.release()
    assert slot is None
    assert reason == "wait_timeout"
    assert waited == 5.0
    assert sleeps == [2.0, 2.0, 1.0]


def test_wait_timeout_runs_unslotted_and_records_it() -> None:
    held = _hold_all()
    try:
        rows = slotted(max_wait_seconds=0)(_rows)()
    finally:
        for slot in held:
            slot.release()
    assert rows[0]["host_slot_wait"]["reason"] == "wait_timeout"
    assert rows[0]["host_slot_wait"]["acquired"] is False


def test_suite_marks_the_test_process_as_holding_a_slot() -> None:
    assert _SUITE_HELD == "1"


def test_held_env_other_than_one_does_not_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HELD_ENV, "0")
    held = _hold_all()
    try:
        rows = slotted(max_wait_seconds=0)(_rows)()
    finally:
        for slot in held:
            slot.release()
    assert rows[0]["host_slot_wait"]["reason"] == "wait_timeout"
