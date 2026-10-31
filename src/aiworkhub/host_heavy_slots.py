"""Host-wide heavy-run slots shared by every repository (NF-2026-01893).

Each slot is an advisory OS lock on ``slot-{i}.lock`` under one per-user
directory, so detached recipe runs from different repositories contend for the
same bounded set. OS locks vanish with the holding process, so a crashed
holder never leaks a slot.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import parallelism, platform_io


def default_slots_dir() -> Path:
    return Path.home() / ".aiworkhub" / "host-slots"


def slot_count(cpu_capacity: int | None = None) -> int:
    """Return how many heavy runs may hold a slot at once.

    ``capacity`` defaults to ``parallelism.get_cpu_capacity()``. One run is
    budgeted ``per_run = max(2, capacity // 4)`` cores and one run's worth of
    capacity stays free for the host: ``max(1, capacity // per_run - 1)``.
    """

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
