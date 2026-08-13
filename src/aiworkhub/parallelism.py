"""Shared, bounded CPU-capacity policy for internal worker pools."""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator


_POOL_DEPTH: ContextVar[int] = ContextVar("aiworkhub_pool_depth", default=0)


def get_cpu_capacity() -> int:
    """Return process-visible CPU capacity, preferring affinity to host size."""

    try:
        affinity = os.sched_getaffinity(0)
        if affinity:
            return len(affinity)
    except (AttributeError, OSError, NotImplementedError):
        pass
    return max(1, int(os.cpu_count() or 1))


def pool_is_nested() -> bool:
    """Return whether the current execution context already owns a pool."""

    return _POOL_DEPTH.get() > 0


@contextmanager
def worker_pool_scope() -> Iterator[None]:
    """Mark a bounded pool lifetime so nested sizing remains serial."""

    token = _POOL_DEPTH.set(_POOL_DEPTH.get() + 1)
    try:
        yield
    finally:
        _POOL_DEPTH.reset(token)


@dataclass(frozen=True, slots=True)
class WorkerSelection:
    available_cpus: int
    selected_workers: int
    reserve: int
    ceiling: int
    nested: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "available_cpus": self.available_cpus,
            "selected_workers": self.selected_workers,
            "reserve": self.reserve,
            "ceiling": self.ceiling,
            "nested": self.nested,
            "reason": self.reason,
        }


def compute_worker_count(
    *,
    candidate_count: int,
    reserve: int = 1,
    ceiling: int | None = None,
    soft_max: int = 8,
    min_candidates: int = 2,
) -> tuple[int, WorkerSelection]:
    """Select a deterministic, bounded width and return truthful telemetry."""

    capacity = get_cpu_capacity()
    reserve = max(0, int(reserve))
    hard_ceiling = max(1, int(ceiling if ceiling is not None else soft_max))
    nested = pool_is_nested()

    if nested:
        reason = "nested_invocation_serial"
        workers = 1
    elif capacity <= 1:
        reason = "single_cpu_serial"
        workers = 1
    elif candidate_count < max(1, int(min_candidates)):
        reason = "below_min_candidates"
        workers = 1
    else:
        workers = min(
            max(1, capacity - reserve),
            hard_ceiling,
            max(1, int(candidate_count)),
        )
        reason = "capacity_based" if workers > 1 else "effective_single_worker"

    selection = WorkerSelection(
        available_cpus=capacity,
        selected_workers=int(workers),
        reserve=reserve,
        ceiling=hard_ceiling,
        nested=nested,
        reason=reason,
    )
    return int(workers), selection
