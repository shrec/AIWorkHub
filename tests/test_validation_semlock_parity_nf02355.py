"""NF-2026-02355: the validation and worker lanes get the same /dev/shm SemLock rule.

The hub validation sandbox used to grant ``--semlock-dev-shm`` only when the
static detector saw SemLock in the declared command, so indirect SemLock use
(a library, conftest or helper) that worked in the worker sandbox went red at
hub validation.  The flag decision is pure argv construction, so these tests
run on every OS with the single existing gate monkeypatched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import sandbox_cpu_budget, validation_runner
from aiworkhub.worker_workspace import WorkspaceError

_VALIDATION_FLAG = sandbox_cpu_budget.VALIDATION_SEMLOCK_DEV_SHM_FLAG
_WORKER_FLAG = sandbox_cpu_budget.WORKER_SEMLOCK_DEV_SHM_FLAG
_ROOT = "/dev/shm"


def _gate(monkeypatch: pytest.MonkeyPatch, root: str | None) -> None:
    monkeypatch.setattr(validation_runner, "landlock_semlock_dev_shm_root", lambda *a: root)


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    path = tmp_path / "scratch"
    path.mkdir()
    return path


def test_unrequested_validation_launch_gains_the_rule(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gate(monkeypatch, _ROOT)
    decide = sandbox_cpu_budget.landlock_semlock_dev_shm_flags
    unrequested = decide(False, scratch)
    assert unrequested == [_VALIDATION_FLAG, _ROOT]
    assert unrequested == decide(True, scratch)


def test_worker_lane_is_unchanged_and_shares_the_root(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gate(monkeypatch, _ROOT)
    decide = sandbox_cpu_budget.landlock_semlock_dev_shm_flags
    worker = decide(False, None)
    validation = decide(False, scratch)
    assert worker == [_WORKER_FLAG, _ROOT]
    assert worker[1] == validation[1]
    assert _WORKER_FLAG not in validation


def test_closed_gate_yields_nothing_for_every_lane(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gate(monkeypatch, None)
    decide = sandbox_cpu_budget.landlock_semlock_dev_shm_flags
    assert decide(False, None) == []
    assert decide(False, scratch) == []
    assert decide(True, scratch) == []


@pytest.mark.parametrize("root", [_ROOT, None])
def test_requested_without_exec_scratch_still_fails_closed(
    root: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gate(monkeypatch, root)
    with pytest.raises(WorkspaceError, match="landlock_semlock_dev_shm_requires_validation"):
        sandbox_cpu_budget.landlock_semlock_dev_shm_flags(True, None)


def test_every_validation_launch_consults_the_single_gate(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def gate(*_args: object) -> str:
        calls.append(1)
        return _ROOT

    monkeypatch.setattr(validation_runner, "landlock_semlock_dev_shm_root", gate)
    decide = sandbox_cpu_budget.landlock_semlock_dev_shm_flags
    decide(False, scratch)
    decide(True, scratch)
    assert calls == [1, 1]
