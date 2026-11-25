"""NF-2026-01966: a takeover finalizer validating a normally exited worker reads ``finalizing``.

A takeover finalizer (reconcile scan or task_status, ``lock_blocking=False``)
never appends the ``finalizing`` ledger row, so the latest row still says
``starting``/``running`` while it validates for minutes. With the supervisor
already exited and the finalizer's own heartbeat fresh, the request is being
worked on and must not be reported ``lost``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _ensure_deepseek_credentials_stub() -> None:
    """Install a stub only when the real module is genuinely unimportable."""
    import importlib
    import types

    try:
        importlib.import_module("aiworkhub.deepseek_credentials")
        return
    except ImportError:
        pass

    stub = types.ModuleType("aiworkhub.deepseek_credentials")

    class CredentialError(Exception):
        def __init__(self, reason: str = "deepseek_credential_stub_environment") -> None:
            super().__init__(reason)
            self.reason = reason

    def load_credential(repo=None):  # noqa: ANN001, ARG001
        raise CredentialError("deepseek_credential_stub_environment")

    def adapter_readiness(repo=None):  # noqa: ANN001, ARG001
        return {"ok": True, "readonly": True, "adapters": []}

    stub.CredentialError = CredentialError
    stub.load_credential = load_credential
    stub.adapter_readiness = adapter_readiness
    sys.modules["aiworkhub.deepseek_credentials"] = stub


_ensure_deepseek_credentials_stub()

from aiworkhub import process_launcher  # noqa: E402

REQUEST_ID = "req-nf01966-takeover"
TASK_ID = "TASK_NF01966"
DEAD_PID = 2_147_483_001
DEAD_TICKS = 999_999_998
TAKEOVER_ROW_STATES = ("starting", "running")
NORMAL_EXIT_PHASE = {"liveness_phase": "host_validation_running", "worker_exit": "normal"}


def _own_ticks() -> int:
    ticks = process_launcher._pid_start_ticks(os.getpid())
    if ticks is None:
        pytest.skip("validation_unsupported_in_sandbox:pid_start_ticks_unavailable")
    return ticks


def _stale_heartbeat_epoch() -> float:
    window = process_launcher.heartbeat_lease_seconds() + process_launcher.lost_recovery_grace_seconds()
    return time.time() - window - 10.0


def _finalizer_section(**overrides) -> dict:
    section = {
        "pid": os.getpid(),
        "pid_start_ticks": overrides.pop("pid_start_ticks", None) or _own_ticks(),
        "heartbeat_at_epoch": time.time() - 1.0,
        "phase": "validating",
        "total": 4,
        "elapsed_seconds": 726.5,
    }
    section.update(overrides)
    return section


def _supervisor_record(finalizer: dict | None, **extra) -> dict:
    now = time.time()
    record = {
        "state": "exited",
        "exit_code": 0,
        "started_at_epoch": now - 900,
        "finished_at_epoch": now - 730,
        "heartbeat_at_epoch": now - 730,
        "last_output_change_epoch": now - 740,
        "heartbeat_seq": 11,
        **extra,
    }
    if finalizer is not None:
        record[process_launcher.FINALIZER_STATUS_SECTION] = finalizer
    return record


def _snapshot(directory: Path, state: str, record: dict) -> dict:
    status_path = directory / "supervisor.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(record), encoding="utf-8")
    row = {
        "request_id": REQUEST_ID,
        "task_id": TASK_ID,
        "state": state,
        "pid": DEAD_PID,
        "pid_start_ticks": DEAD_TICKS,
        "supervisor_status_path": str(status_path),
    }
    return process_launcher.ProcessManager._liveness_snapshot(row)


_DEAD_FINALIZERS = {
    "dead_pid": lambda: _finalizer_section(pid=DEAD_PID, pid_start_ticks=DEAD_TICKS),
    "reused_pid": lambda: _finalizer_section(pid_start_ticks=_own_ticks() + 1),
    "stale_heartbeat": lambda: _finalizer_section(heartbeat_at_epoch=_stale_heartbeat_epoch()),
}


def test_takeover_row_states_are_a_named_subset_of_the_active_states():
    assert process_launcher.TAKEOVER_FINALIZATION_ROW_STATES == frozenset(TAKEOVER_ROW_STATES)
    assert process_launcher.TAKEOVER_FINALIZATION_ROW_STATES <= process_launcher.ACTIVE_PROCESS_STATES
    assert not process_launcher.TAKEOVER_FINALIZATION_ROW_STATES & process_launcher.FINALIZATION_PENDING_STATES


@pytest.mark.parametrize("state", TAKEOVER_ROW_STATES)
def test_running_row_exited_supervisor_live_finalizer_reads_finalizing(tmp_path, state):
    liveness = _snapshot(tmp_path, state, _supervisor_record(_finalizer_section()))

    assert liveness["liveness_state"] == process_launcher.FINALIZING_LIVENESS_STATE == "finalizing"
    assert liveness["supervisor_alive"] is False
    assert liveness["finalization_progress"] == {
        "phase": "validating", "total": 4, "elapsed_seconds": 726.5,
    }
    assert {key: liveness.get(key) for key in NORMAL_EXIT_PHASE} == NORMAL_EXIT_PHASE


@pytest.mark.parametrize("state", TAKEOVER_ROW_STATES)
def test_takeover_finalizer_after_a_nonzero_exit_is_finalizing_without_the_phase(tmp_path, state):
    liveness = _snapshot(tmp_path, state, _supervisor_record(_finalizer_section(), exit_code=1))

    assert liveness["liveness_state"] == "finalizing"
    assert not set(NORMAL_EXIT_PHASE) & set(liveness)


@pytest.mark.parametrize("name", sorted(_DEAD_FINALIZERS))
@pytest.mark.parametrize("state", TAKEOVER_ROW_STATES)
def test_running_row_exited_supervisor_dead_or_stale_finalizer_reads_lost(tmp_path, state, name):
    baseline = _snapshot(tmp_path / "baseline", state, _supervisor_record(None))
    liveness = _snapshot(tmp_path / "case", state, _supervisor_record(_DEAD_FINALIZERS[name]()))

    assert baseline["liveness_state"] == "lost"
    assert liveness["liveness_state"] == "lost"
    assert "finalization_progress" not in liveness
    assert not set(NORMAL_EXIT_PHASE) & set(liveness)


@pytest.mark.parametrize("supervisor_state", ["running", "starting", "mystery"])
@pytest.mark.parametrize("state", TAKEOVER_ROW_STATES)
def test_running_row_with_a_non_exited_supervisor_ignores_the_section(state, supervisor_state):
    record = _supervisor_record(_finalizer_section(), state=supervisor_state)

    assert process_launcher.finalizer_liveness({"state": state}, record) == {}


@pytest.mark.parametrize("state", ["review_ready", "blocked", "cancel_requested"])
def test_terminal_and_cancel_rows_ignore_the_section(tmp_path, state):
    record = _supervisor_record(_finalizer_section())

    assert process_launcher.finalizer_liveness({"state": state}, record) == {}
    liveness = _snapshot(tmp_path, state, record)
    assert liveness.get("liveness_state") != "finalizing"
    assert "finalization_progress" not in liveness
