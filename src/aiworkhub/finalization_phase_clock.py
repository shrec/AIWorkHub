"""NF-2026-02046: named sub-phases of isolated-request finalization.

``finalization_phase_durations_ms.evidence_and_transition`` used to be an
unnamed residual (total minus validation minus workspace scope).  A measured
reviewer finalization reported exactly 20000.0 ms there -- the advisory-lock
bound -- with nothing naming which call waited.  The sub-phases that ran
inside that residual are now timed where they run (request-lock wait, the
terminal task transition, attempt-artifact persistence, review registration,
usage recording and context/evidence parsing), so a lock or store wait is
attributed to the call that waited rather than surfacing as an anonymous
remainder.  ``evidence_and_transition`` keeps only what none of them measured.

Measurement only: nothing here changes finalization behaviour.
"""

from __future__ import annotations

import time
from typing import Any, Callable

FINALIZATION_EVIDENCE_SUBPHASES: tuple[str, ...] = (
    "request_lock_wait",
    "terminal_transition",
    "attempt_artifacts",
    "review_registration",
    "usage_record",
    "context_evidence",
)


class _FinalizationPhaseClock:
    """Accumulate wall time per named finalization sub-phase (measurement only)."""

    __slots__ = ("_ms",)

    def __init__(self) -> None:
        self._ms: dict[str, float] = dict.fromkeys(FINALIZATION_EVIDENCE_SUBPHASES, 0.0)

    def add(self, phase: str, started: float) -> None:
        if phase not in self._ms:
            raise KeyError(f"finalization_phase_unknown:{phase}")
        self._ms[phase] += max(0.0, time.monotonic() - started) * 1000.0

    def call(self, phase: str, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            self.add(phase, started)

    def transition(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """``call("terminal_transition", fn, ...)``: the terminal task transition."""
        return self.call("terminal_transition", fn, *args, **kwargs)

    def snapshot(self) -> dict[str, float]:
        return {name: round(value, 3) for name, value in self._ms.items()}


def _finalization_phase_durations(
    *,
    total_ms: float,
    validation_ms: float,
    scope_ms: float,
    clock: _FinalizationPhaseClock,
) -> dict[str, float]:
    """Named finalization phases; ``evidence_and_transition`` is only the rest.

    The values never sum past ``total_ms`` (bar rounding): the named
    sub-phases are disjoint from validation and scope, and the residual is
    what none of them measured.
    """

    named = clock.snapshot()
    residual = max(
        0.0, total_ms - validation_ms - scope_ms - sum(named.values())
    )
    return {
        "workspace_scope": round(scope_ms, 3),
        "validation": round(validation_ms, 3),
        **named,
        "evidence_and_transition": round(residual, 3),
    }
