"""NF-2026-02019: one slow sdlc_sync pass can never stall the reconciler again.

Measured 2026-11-03: reconciler status showed ``scan_phase=sdlc_sync`` with
``scan_phase_age_s=4250``. sdlc_sync is the scan's last phase, so the next
scan's retries, orphan-claim release and review recovery waited on it. A pass
now names its running sub-stage and stops between tasks at a wall-clock
deadline, persisting what it recorded and resuming the same window next pass.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

_TESTS = Path(__file__).resolve().parent
_SRC = _TESTS.parent / "src"
for _path in (_SRC, _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import test_reconciler_phase_timing_nf01866 as phase_timing  # noqa: E402
import test_sdlc_sync as base  # noqa: E402
from test_sdlc_sync import _fresh_git_facts, repo, spies  # noqa: E402,F401

from aiworkhub import sdlc_sync  # noqa: E402
from aiworkhub import task_reconciler as tr  # noqa: E402

TASK_IDS = ["T-BUDGET-1", "T-BUDGET-2", "T-BUDGET-3"]
PARTS = ["cases", "stages", "release", "maintain", "attribution", "bands"]


def _state(root: Path) -> SimpleNamespace:
    conn = sqlite3.connect(str(root.joinpath(*sdlc_sync.STATE_DB_REL)))
    try:
        cursor = conn.execute(
            "SELECT event_id FROM sync_cursor WHERE name=?", (sdlc_sync.TASK_EVENTS_CURSOR,)
        ).fetchone()
        rows = {task_id for (task_id,) in conn.execute("SELECT task_id FROM task_stage_state")}
        marker = conn.execute(
            "SELECT value FROM sync_release_cursor WHERE name=?", (sdlc_sync.STAGES_RESUME,)
        ).fetchone()
    finally:
        conn.close()
    return SimpleNamespace(
        cursor=cursor[0] if cursor else None,
        rows=rows,
        marker=marker[0] if marker else "",
    )


def _last_event_id(root: Path) -> int:
    conn = sqlite3.connect(str(base.task_store.canonical_db_path(root)))
    try:
        return int(conn.execute("SELECT MAX(event_id) FROM task_events").fetchone()[0])
    finally:
        conn.close()


def test_a_pass_past_its_deadline_defers_and_the_next_resumes_the_window(repo, spies, monkeypatch):
    for task_id in TASK_IDS:
        base._create_task(repo.root, task_id)
    clock = {"now": 1000.0}
    monkeypatch.setattr(sdlc_sync, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    seen: list[str] = []

    def _slow_record(root, repository_id, case_id, task_id, card):
        seen.append(task_id)
        clock["now"] += 60.0  # one slow task spends the whole budget
        states = {stage: "unknown" for stage in sdlc_sync.SYNC_STAGES}
        return {**states, "plan": "ready"}, {}

    monkeypatch.setattr(sdlc_sync, "_record_stages", _slow_record)

    first = sdlc_sync.sync_once(repo.root, repo.repo_id, deadline=clock["now"] + 30.0)

    assert first["ok"] is True and first["failures"] == []
    stages = first["parts"]["stages"]
    assert stages["state"] == "deferred"
    assert stages["reason"] == sdlc_sync.PASS_BUDGET_EXHAUSTED
    assert stages["remaining"] == 2 and stages["tasks"] == 1
    # Parts not yet started once the budget is spent do not run.
    for part in ("release", "maintain"):
        assert first["parts"][part] == {
            "state": "deferred", "reason": sdlc_sync.PASS_BUDGET_EXHAUSTED,
        }
    assert "cursor" not in first["parts"] or first["parts"]["cursor"] == {"state": "read"}
    assert seen == TASK_IDS[:1]
    after_first = _state(repo.root)
    assert after_first.cursor is None  # the task-events cursor did not move
    assert after_first.rows == {TASK_IDS[0]}  # the recorded task's outcome persisted
    assert after_first.marker  # where to resume

    second = sdlc_sync.sync_once(repo.root, repo.repo_id, deadline=clock["now"] + 1000.0)

    assert second["ok"] is True
    assert second["parts"]["stages"] == {"state": "ok", "tasks": 2, "ready": 2}
    assert second["parts"]["cursor"]["state"] == "advanced"
    # Each task recorded exactly once across both passes, in window order.
    assert seen == TASK_IDS
    after_second = _state(repo.root)
    assert after_second.cursor == _last_event_id(repo.root)
    assert after_second.rows == set(TASK_IDS)
    assert after_second.marker == ""  # cleared once the window completed

    third = sdlc_sync.sync_once(repo.root, repo.repo_id, deadline=clock["now"] + 1000.0)
    assert third["parts"]["stages"] == {"state": "ok", "tasks": 0, "ready": 0}
    assert seen == TASK_IDS


def test_without_a_deadline_the_whole_window_is_recorded_in_one_pass(repo, spies, monkeypatch):
    for task_id in TASK_IDS:
        base._create_task(repo.root, task_id)
    seen: list[str] = []

    def _record(root, repository_id, case_id, task_id, card):
        seen.append(task_id)
        return {stage: "unknown" for stage in sdlc_sync.SYNC_STAGES}, {}

    monkeypatch.setattr(sdlc_sync, "_record_stages", _record)
    result = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert result["parts"]["stages"]["state"] == "ok"
    assert seen == TASK_IDS
    assert _state(repo.root).cursor == _last_event_id(repo.root)


def test_on_stage_names_each_part_and_a_raising_one_never_fails_the_pass(repo, spies):
    base._create_task(repo.root, TASK_IDS[0])
    called: list[str] = []

    def _boom(part: str) -> None:
        called.append(part)
        raise RuntimeError("status write failed")

    result = sdlc_sync.sync_once(repo.root, repo.repo_id, on_stage=_boom)
    assert result["ok"] is True and result["failures"] == []
    assert called == PARTS


def test_run_scan_reports_sdlc_sync_sub_stages_and_passes_a_deadline(monkeypatch, tmp_path):
    phase_timing._patch_phases(monkeypatch)
    monkeypatch.setattr(tr, "_scan_operational_retry_relaunch", lambda *_a, **_k: {})
    captured: dict = {}

    def _sync(repo_root, repository_id, *, on_stage=None, deadline=None):
        captured["deadline"] = deadline
        captured["called_at"] = time.monotonic()
        on_stage("cases")
        on_stage("stages")
        return {"state": "ok"}

    monkeypatch.setattr(tr.sdlc_sync, "sync_once", _sync)
    seen: list[str] = []
    result = tr.run_scan(phase_timing._Mgr(tmp_path), include_gc=False, on_phase=seen.append)

    assert seen.index("sdlc_sync") < seen.index("sdlc_sync:cases") < seen.index("sdlc_sync:stages")
    assert seen[-3:] == ["sdlc_sync", "sdlc_sync:cases", "sdlc_sync:stages"]
    # Durations keep the single sdlc_sync total.
    assert "sdlc_sync" in result["phase_durations_ms"]
    assert not any(name.startswith("sdlc_sync:") for name in result["phase_durations_ms"])
    assert tr.SDLC_SYNC_PASS_BUDGET_S == 120.0
    remaining = captured["deadline"] - captured["called_at"]
    assert 0 < remaining <= tr.SDLC_SYNC_PASS_BUDGET_S


def test_a_raising_on_phase_does_not_break_sub_stage_reporting(monkeypatch, tmp_path):
    phase_timing._patch_phases(monkeypatch)
    monkeypatch.setattr(tr, "_scan_operational_retry_relaunch", lambda *_a, **_k: {})

    def _sync(repo_root, repository_id, *, on_stage=None, deadline=None):
        on_stage("cases")
        return {"state": "ok"}

    monkeypatch.setattr(tr.sdlc_sync, "sync_once", _sync)

    def _boom(_name: str) -> None:
        raise RuntimeError("status write failed")

    result = tr.run_scan(phase_timing._Mgr(tmp_path), include_gc=False, on_phase=_boom)
    assert result["ok"] is True
    assert result["sdlc_sync"] == {"state": "ok"}


def test_a_transient_deploy_refusal_in_a_deferred_window_still_withholds_the_digest(
    repo, monkeypatch
):
    # The resumed pass does not replay the task whose Deploy refusal was
    # transient, so the resume marker carries it to that pass's release part.
    base._create_task(repo.root, base.TASK_ID)
    sdlc_sync.sync_once(repo.root, repo.repo_id)
    base._seal_and_accept(repo.root, base.TASK_ID)
    base._create_task(repo.root, TASK_IDS[0])
    base._publish(repo.root, "1.0.0")
    clock = {"now": 1000.0}
    monkeypatch.setattr(sdlc_sync, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    real_record = sdlc_sync._record_stages

    def _slow_record(*args):
        clock["now"] += 60.0  # one task spends the whole budget
        return real_record(*args)

    monkeypatch.setattr(sdlc_sync, "_record_stages", _slow_record)
    with monkeypatch.context() as patched:
        patched.setattr(base.sdlc_deploy_proof, "_git", lambda *_args: None)
        deferred = sdlc_sync.sync_once(repo.root, repo.repo_id, deadline=clock["now"] + 30.0)
    assert deferred["parts"]["stages"]["state"] == "deferred"
    assert deferred["refusals"][base.TASK_ID]["deploy"].endswith(":release_blob_unverifiable")

    resumed = sdlc_sync.sync_once(repo.root, repo.repo_id, deadline=clock["now"] + 1000.0)
    assert resumed["parts"]["stages"]["state"] == "ok"
    assert resumed["parts"]["release"]["state"] == "deferred"

    retried = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert retried["stages"][base.TASK_ID]["deploy"] == "ready"
