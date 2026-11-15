"""NF-2026-02243: a dispose-time reproduction gives up in 120 s and names why.

Dismissing one reviewer finding waited the old 600 s deadline on a
reproduction that never finished, blocking the manager for ten minutes with no
evidence. The default deadline is now 120 s, and the deadline receipt's
candidate side keeps detail ``timeout`` but gains ``timeout_phase``:
``sandbox_startup`` when the probe was never handed to the sandbox runner,
``probe`` once it was. The fakes are the NF-2026-02095 ones: a sandbox runner
that blocks behind ``release``, never a real workspace build.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import finding_reproduction as fr  # noqa: E402


def _load_nf02095() -> Any:
    path = Path(__file__).resolve().with_name("test_dispose_reproduction_deadline_nf02095.py")
    spec = importlib.util.spec_from_file_location("_nf02095_fakes", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_FAKES = _load_nf02095()
DEADLINE = _FAKES.DEADLINE


def test_default_deadline_is_120_seconds() -> None:
    assert fr.DISPOSE_REPRODUCTION_DEADLINE_SECONDS == 120.0


@pytest.fixture
def short_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fr, "DISPOSE_REPRODUCTION_DEADLINE_SECONDS", DEADLINE)
    monkeypatch.setattr(fr, "DISPOSE_PROGRESS_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(fr, "_DISPOSE_RUNS", fr.DisposeRunRegistry())


def _blocked_before_probe(sandbox: Any):
    """The NF-2026-02095 target, but candidate workspace creation blocks."""

    def resolve(spec: fr.ReproductionSpec) -> fr.ReproductionTarget:
        target = sandbox.target(spec)

        def create() -> str:
            sandbox.release.wait(30.0)
            with sandbox._guard:
                sandbox.created.append("candidate")
            return "candidate"

        return dataclasses.replace(target, create_candidate=create)

    return resolve


def _assert_phase(receipt: dict[str, Any], phase: str) -> None:
    _FAKES._assert_deadline_receipt(receipt, "finding-1")
    assert receipt["candidate"]["detail"] == fr.TIMEOUT_DETAIL
    assert receipt["candidate"]["timeout_phase"] == phase


@pytest.mark.usefixtures("short_deadline")
def test_a_run_blocked_before_the_probe_starts_times_out_in_sandbox_startup() -> None:
    sandbox = _FAKES._SlowSandbox()
    reproduce = fr.dispose_reproducer(
        coordinator_key=lambda: _FAKES.KEY, resolve_target=_blocked_before_probe(sandbox)
    )
    try:
        receipt = _FAKES._call(reproduce, "finding-1")
    finally:
        sandbox.drain()
    _assert_phase(receipt, fr.TIMEOUT_PHASE_SANDBOX_STARTUP)


@pytest.mark.usefixtures("short_deadline")
def test_a_run_blocked_after_the_probe_starts_times_out_in_probe() -> None:
    sandbox = _FAKES._SlowSandbox()
    reproduce = fr.dispose_reproducer(
        coordinator_key=lambda: _FAKES.KEY, resolve_target=sandbox.target
    )
    try:
        receipt = _FAKES._call(reproduce, "finding-1")
        assert sandbox.created == ["candidate"]
    finally:
        sandbox.drain()
    _assert_phase(receipt, fr.TIMEOUT_PHASE_PROBE)
