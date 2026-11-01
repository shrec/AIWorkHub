"""Host-wide heavy-run slots shared by every repository (NF-2026-01893).

Each slot is an advisory OS lock on ``slot-{i}.lock`` under one per-user
directory, so detached recipe runs from different repositories contend for the
same bounded set. OS locks vanish with the holding process, so a crashed
holder never leaks a slot.
"""

from __future__ import annotations

import functools
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# parallelism and platform_io are imported per call: worker_workspace imports this
# module before _load_runtime_temp has checked platform_io and its private
# siblings are regular files (NF-00841).


def default_slots_dir() -> Path:
    return Path.home() / ".aiworkhub" / "host-slots"


def slot_count(cpu_capacity: int | None = None) -> int:
    """Return how many heavy runs may hold a slot at once.

    ``capacity`` defaults to ``parallelism.get_cpu_capacity()``. One run is
    budgeted ``per_run = max(2, capacity // 4)`` cores and one run's worth of
    capacity stays free for the host: ``max(1, capacity // per_run - 1)``.
    """

    from . import parallelism

    capacity = parallelism.get_cpu_capacity() if cpu_capacity is None else cpu_capacity
    per_run = max(2, capacity // 4)
    return max(1, capacity // per_run - 1)


class HostSlot:
    """One held slot; ``release`` is idempotent."""

    def __init__(self, index: int, fd: int) -> None:
        self.index = index
        self._fd: int | None = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        from . import platform_io

        try:
            platform_io.unlock_fd(fd)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> HostSlot:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def try_acquire(
    slots_dir: Path | None = None, count: int | None = None
) -> HostSlot | None:
    """Take the first free slot without blocking; ``None`` when all are held."""

    from . import platform_io

    directory = default_slots_dir() if slots_dir is None else Path(slots_dir)
    total = slot_count() if count is None else count
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(total):
        fd = platform_io.open_lock_file(directory / f"slot-{index}.lock")
        try:
            platform_io.lock_fd(fd, blocking=False)
        except OSError:
            os.close(fd)
            continue
        except BaseException:
            os.close(fd)
            raise
        return HostSlot(index, fd)
    return None


HELD_ENV = "AIWORKHUB_HOST_HEAVY_SLOT_HELD"
POLL_SECONDS = 2.0


def wait_acquire(
    max_wait_seconds: float,
    *,
    poll_seconds: float = POLL_SECONDS,
    slots_dir: Path | None = None,
    count: int | None = None,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[HostSlot | None, float, str]:
    """Wait at most ``max_wait_seconds`` for a slot; never raises ``OSError``.

    Returns ``(slot_or_None, waited_seconds, reason)`` where reason is
    ``acquired`` (first try), ``acquired_after_wait``, ``wait_timeout`` or
    ``slots_dir_unusable`` (fail open: the caller runs unslotted).
    """

    started = clock()
    first_try = True
    while True:
        try:
            slot = try_acquire(slots_dir, count)
        except OSError:
            return None, max(0.0, clock() - started), "slots_dir_unusable"
        waited = max(0.0, clock() - started)
        if slot is not None:
            return slot, waited, "acquired" if first_try else "acquired_after_wait"
        remaining = max_wait_seconds - waited
        if remaining <= 0:
            return None, waited, "wait_timeout"
        first_try = False
        sleep(min(poll_seconds, remaining))


def slotted(max_wait_seconds: float) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Hold one host slot while the wrapped batch function runs.

    The wait is bounded by the call's ``timeout_seconds`` keyword (default
    ``max_wait_seconds``). Anything but an immediate acquire is recorded as
    ``host_slot_wait`` on every returned row; a timeout or unusable slots
    directory runs the batch unslotted. Under ``HELD_ENV`` no slot is taken.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if os.environ.get(HELD_ENV) == "1":
                return fn(*args, **kwargs)
            total = slot_count()
            limit = kwargs.get("timeout_seconds", max_wait_seconds)
            slot, waited, reason = wait_acquire(max(0, limit), count=total)
            try:
                result = fn(*args, **kwargs)
            finally:
                if slot is not None:
                    slot.release()
            if reason != "acquired" and isinstance(result, list):
                note = {
                    "slots": total,
                    "waited_seconds": round(waited, 3),
                    "acquired": slot is not None,
                    "reason": reason,
                }
                for row in result:
                    if isinstance(row, dict):
                        row["host_slot_wait"] = dict(note)
            return result

        return wrapper

    return decorate
