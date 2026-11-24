"""NF-2026-02417: a write-gated reconcile never decides a dead finalizer.

``end_if_finalizer_dead`` fences a dead finalizer and re-finalizes it through
``manager.retry_finalization``. A process whose write gate is closed
(``core.writes_allowed()`` False) is refused by that retry with
``write_gate_closed``; recording the refusal as ``finalize_failed`` blocked a
card whose worker exited 0. The gated process now leaves the row untouched so
the next write-enabled reconcile re-finalizes it.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, finalize_retry_dispatch, process_launcher  # noqa: E402

SUPERVISOR = (4101, 11)
CHILD = (4102, 12)
FINALIZER = (4103, 13)
RID, TID, RUNNER = "a3f1c2d4e5b64a7e8c9d0e1f2a3b4c5d", "write-gate-card", "codex_a"


def _manager(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "process_log.jsonl",
        process_dir=tmp_path / "proc",
        isolation_enabled=False,
    )


def _seed_dead_finalizer(mgr, tmp_path: Path) -> None:
    status_path = tmp_path / "supervisor_status.json"
    process_launcher.write_json_0600(status_path, {
        "state": "exited",
        "exit_code": 0,
        "child_pid": CHILD[0],
        "child_pid_start_ticks": CHILD[1],
        "heartbeat_at_epoch": time.time() - 2000,
        process_launcher.FINALIZER_STATUS_SECTION: {
            "pid": FINALIZER[0],
            "pid_start_ticks": FINALIZER[1],
            "heartbeat_at_epoch": time.time(),
            "phase": process_launcher.FINALIZER_VALIDATING_PHASE,
            "total": 2,
            "elapsed_seconds": 2007.0,
        },
    })
    mgr._append_event({
        "request_id": RID, "task_id": TID, "runner": RUNNER, "topic": "finalization",
        "state": "running", "pid": SUPERVISOR[0], "pid_start_ticks": SUPERVISOR[1],
        "supervisor_status_path": str(status_path),
    })
    mgr._append_event({"request_id": RID, "task_id": TID, "runner": RUNNER, "state": "finalizing"})


@pytest.fixture()
def dead_finalizer(tmp_path, monkeypatch):
    """A ``finalizing`` row whose supervisor, child and finalizer are all dead."""
    mgr = _manager(tmp_path)
    _seed_dead_finalizer(mgr, tmp_path)
    monkeypatch.setattr(process_launcher, "_pid_matches", lambda pid, ticks: False)
    terminal: list[str] = []

    def fake_terminal(repo, task_id, runner, substatus, *, evidence=None, request_id=""):
        terminal.append(substatus)
        return {"ok": True, "callback_enqueued": True}

    monkeypatch.setattr(process_launcher.task_engine, "mark_terminal_failure", fake_terminal)
    return mgr, terminal


def _states(mgr) -> list[str]:
    return [str(event.get("state")) for event in mgr._request_events(RID)]


def test_write_gated_reconcile_leaves_a_dead_finalizer_finalizing(dead_finalizer, monkeypatch):
    mgr, terminal = dead_finalizer
    monkeypatch.setattr(core, "writes_allowed", lambda: False)
    retries: list[tuple] = []
    monkeypatch.setattr(mgr, "retry_finalization", lambda *a, **k: retries.append(a) or {
        "ok": False, "error": "write_gate_closed",
    })
    before = list(mgr._request_events(RID))

    assert finalize_retry_dispatch.end_if_finalizer_dead(mgr, RID) is None

    assert mgr._request_events(RID) == before  # no fence event, no finalize_failed event
    assert _states(mgr)[-1] == "finalizing"
    assert "finalize_failed" not in _states(mgr)
    assert retries == []
    assert terminal == []


def test_write_gated_reconcile_pass_keeps_the_row_finalizing(dead_finalizer, monkeypatch):
    mgr, terminal = dead_finalizer
    monkeypatch.setattr(core, "writes_allowed", lambda: False)

    mgr._reconcile_persisted_requests()

    assert mgr._latest_by_request()[RID]["state"] == "finalizing"
    assert terminal == []


def test_write_enabled_reconcile_still_re_finalizes_through_retry_finalization(dead_finalizer, monkeypatch):
    mgr, _terminal = dead_finalizer
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    retries: list[tuple] = []

    def retry(request_id, task_id):
        retries.append((request_id, task_id))
        return {"ok": True}

    monkeypatch.setattr(mgr, "retry_finalization", retry)

    result = finalize_retry_dispatch.end_if_finalizer_dead(mgr, RID)

    assert retries == [(RID, TID)]
    assert result is not None
    assert result["finalization_end_reason"] == "finalize_failed:finalizer_dead"


def test_write_gated_reconcile_leaves_a_hung_finalizer_to_a_write_enabled_pass(
    dead_finalizer, monkeypatch
):
    mgr, terminal = dead_finalizer
    monkeypatch.setattr(core, "writes_allowed", lambda: False)
    monkeypatch.setattr(
        finalize_retry_dispatch,
        "dead_finalizer_reason",
        lambda latest, **_: finalize_retry_dispatch.VALIDATION_HUNG_REASON,
    )
    generic: list[str] = []
    monkeypatch.setattr(mgr, "_finalize_after_process_exit", lambda rid: generic.append(rid))

    assert finalize_retry_dispatch.reconcile_pending_finalization(mgr, RID, "finalizing") == 0

    assert generic == []  # the hung finalizer's row never reaches the generic finalizer
    assert terminal == []
    assert _states(mgr)[-1] == "finalizing"
