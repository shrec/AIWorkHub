"""NF-2026-01647: a request whose finalizer is alive reports ``finalizing``, not ``lost``.

The provider supervisor has already exited while the host-side finalizer runs
the declared validations, so only the finalizer's own heartbeat (published in
the supervisor status record) can prove the request is still being worked on.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

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

REQUEST_ID = "req-nf1647-finalizing"
TASK_ID = "TASK_NF1647"
RUNNER = "claude_worker_nf1647"
COMMANDS = ["python -m pytest -q tests/test_first.py", "git diff --check"]
PROGRESS_TEXT = "validating 3 command(s), 12.5s"
DEAD_PID = 2_147_483_001
DEAD_TICKS = 999_999_998
_CLOCK_FIELDS = ("heartbeat_age_seconds", "activity_age_seconds", "runtime_seconds")
_MISSING = object()
_HEARTBEAT_THREAD = "aiworkhub-finalizer-heartbeat"


def _own_ticks() -> int:
    ticks = process_launcher._pid_start_ticks(os.getpid())
    if ticks is None:
        pytest.skip("validation_unsupported_in_sandbox:pid_start_ticks_unavailable")
    return ticks


def _supervisor_record(**extra) -> dict:
    now = time.time()
    return {
        "state": "exited",
        "exit_code": 0,
        "started_at_epoch": now - 900,
        "finished_at_epoch": now - 600,
        "heartbeat_at_epoch": now - 600,
        "last_output_change_epoch": now - 650,
        "heartbeat_seq": 7,
        **extra,
    }


def _finalizer_section(**overrides) -> dict:
    section = {
        "pid": os.getpid(),
        "pid_start_ticks": None if "pid_start_ticks" in overrides else _own_ticks(),
        "heartbeat_at_epoch": time.time() - 1.0,
        "phase": "validating",
        "total": 3,
        "elapsed_seconds": 12.5,
    }
    section.update(overrides)
    return {key: value for key, value in section.items() if value is not _MISSING}


def _stale_heartbeat_epoch() -> float:
    window = process_launcher.heartbeat_lease_seconds() + process_launcher.lost_recovery_grace_seconds()
    return time.time() - window - 10.0


def _write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle)


def _pending_row(status_path: Path, state: str = "finalizing") -> dict:
    return {
        "request_id": REQUEST_ID,
        "task_id": TASK_ID,
        "state": state,
        "pid": DEAD_PID,
        "pid_start_ticks": DEAD_TICKS,
        "supervisor_status_path": str(status_path),
    }


def _snapshot(directory: Path, record: dict, finalizer=_MISSING, *, state: str = "finalizing") -> dict:
    payload = dict(record)
    if finalizer is not _MISSING:
        payload["finalizer"] = finalizer
    status_path = directory / "supervisor.json"
    _write_status(status_path, payload)
    return process_launcher.ProcessManager._liveness_snapshot(_pending_row(status_path, state))


def _without_clock(snapshot: dict) -> dict:
    return {key: value for key, value in snapshot.items() if key not in _CLOCK_FIELDS}


def _eventually(read, accept, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while True:
        value = read()
        if accept(value) or time.monotonic() >= deadline:
            return value
        time.sleep(0.02)


def _heartbeat_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == _HEARTBEAT_THREAD]


def _record_heartbeat_writes(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    writes: list[tuple] = []
    monkeypatch.setattr(
        process_launcher, "write_finalizer_heartbeat",
        lambda path, section: writes.append((path, section)) or True,
    )
    return writes


def _require_owner_only_writer(directory: Path) -> None:
    """Skip when this lane denies the owner-only atomic writer (chmod/replace)."""
    try:
        process_launcher.write_json_0600(directory / "writer-probe.json", {"probe": True})
    except OSError as exc:
        if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (5, 1314):
            pytest.skip(f"validation_unsupported_in_sandbox:write_json_0600:{exc}")
        raise


def _require_named_pipes() -> None:
    """Skip when this lane denies the named pipes the finalize path relies on."""
    try:
        from multiprocessing.connection import Listener

        Listener(family="AF_PIPE" if os.name == "nt" else "AF_UNIX").close()
    except (PermissionError, OSError) as exc:
        pytest.skip(f"validation_unsupported_in_sandbox:named_pipe_denied:{exc}")


_DEAD_FINALIZERS = {
    "dead_pid": lambda: _finalizer_section(pid=DEAD_PID, pid_start_ticks=DEAD_TICKS),
    "reused_pid": lambda: _finalizer_section(pid_start_ticks=_own_ticks() + 1),
    "stale_heartbeat": lambda: _finalizer_section(heartbeat_at_epoch=_stale_heartbeat_epoch()),
}

_MALFORMED_FINALIZERS = {
    "section_is_a_string": lambda: "finalizing",
    "section_is_a_list": lambda: ["pid", 1],
    "section_is_an_int": lambda: 7,
    "pid_missing": lambda: _finalizer_section(pid=_MISSING),
    "pid_bool": lambda: _finalizer_section(pid=True),
    "pid_string": lambda: _finalizer_section(pid=str(os.getpid())),
    "pid_zero": lambda: _finalizer_section(pid=0),
    "pid_negative": lambda: _finalizer_section(pid=-1),
    "ticks_missing": lambda: _finalizer_section(pid_start_ticks=_MISSING),
    "ticks_none": lambda: _finalizer_section(pid_start_ticks=None),
    "ticks_empty_string": lambda: _finalizer_section(pid_start_ticks=""),
    "ticks_string": lambda: _finalizer_section(pid_start_ticks=str(_own_ticks())),
    "ticks_bool": lambda: _finalizer_section(pid_start_ticks=True),
    "ticks_float": lambda: _finalizer_section(pid_start_ticks=float(_own_ticks())),
    "ticks_infinity": lambda: _finalizer_section(pid_start_ticks=float("inf")),
    "beat_missing": lambda: _finalizer_section(heartbeat_at_epoch=_MISSING),
    "beat_none": lambda: _finalizer_section(heartbeat_at_epoch=None),
    "beat_string": lambda: _finalizer_section(heartbeat_at_epoch="now"),
    "beat_bool": lambda: _finalizer_section(heartbeat_at_epoch=True),
    "beat_nan": lambda: _finalizer_section(heartbeat_at_epoch=float("nan")),
    "beat_infinity": lambda: _finalizer_section(heartbeat_at_epoch=float("inf")),
}


# --- (a) a live finalizer with a fresh heartbeat is finalizing, never lost ---


def test_live_finalizer_with_a_fresh_heartbeat_reports_finalizing_never_lost(tmp_path):
    status_path = tmp_path / "supervisor.json"
    _write_status(status_path, _supervisor_record(finalizer=_finalizer_section()))
    before = status_path.read_bytes()

    liveness = process_launcher.ProcessManager._liveness_snapshot(_pending_row(status_path))

    assert liveness["liveness_state"] == "finalizing"
    assert liveness["liveness_state"] != "lost"
    assert liveness["finalization_progress"] == {
        "phase": "validating", "total": 3, "elapsed_seconds": 12.5,
    }
    assert liveness["supervisor_alive"] is False
    assert process_launcher.format_finalization_progress(liveness["finalization_progress"]) == (
        PROGRESS_TEXT
    )
    assert status_path.read_bytes() == before


def test_process_status_reports_the_live_finalizer_as_finalizing(tmp_path):
    manager = _build_manager(tmp_path)
    _seed_pending(manager, tmp_path, finalizer=_finalizer_section())

    status = manager.status(REQUEST_ID)

    assert status["state"] == "finalizing"
    assert status["process_alive"] is False
    assert status["liveness"]["liveness_state"] == "finalizing"
    assert status["liveness"]["finalization_progress"]["phase"] == "validating"
    assert status["liveness"]["finalization_progress"]["total"] == 3
    assert set(status["liveness"]["finalization_progress"]) == {"phase", "total", "elapsed_seconds"}


def test_heartbeat_between_lease_and_lease_plus_grace_is_still_finalizing(tmp_path):
    lease = process_launcher.heartbeat_lease_seconds()
    grace = process_launcher.lost_recovery_grace_seconds()
    section = _finalizer_section(heartbeat_at_epoch=time.time() - lease - grace / 2)

    liveness = _snapshot(tmp_path, _supervisor_record(), section)

    assert liveness["liveness_state"] == "finalizing"
    assert liveness["heartbeat_age_seconds"] > lease


@pytest.mark.parametrize("state", sorted(process_launcher.FINALIZATION_PENDING_STATES))
def test_every_finalization_pending_state_reports_finalizing(tmp_path, state):
    liveness = _snapshot(tmp_path, _supervisor_record(), _finalizer_section(), state=state)

    assert liveness["liveness_state"] == "finalizing"


@pytest.mark.parametrize("state", ["starting", "running", "review_ready", "blocked"])
def test_finalizer_section_is_ignored_outside_finalization_pending_states(tmp_path, state):
    liveness = _snapshot(tmp_path, _supervisor_record(), _finalizer_section(), state=state)

    assert liveness.get("liveness_state") != "finalizing"
    assert "finalization_progress" not in liveness


# --- (b) a dead or stale finalizer keeps today's lost result ---


@pytest.mark.parametrize("name", sorted(_DEAD_FINALIZERS))
def test_dead_or_stale_finalizer_keeps_todays_lost_result(tmp_path, name):
    record = _supervisor_record()
    baseline = _snapshot(tmp_path / "baseline", record)

    liveness = _snapshot(tmp_path / "case", record, _DEAD_FINALIZERS[name]())

    assert baseline["liveness_state"] == "lost"
    assert liveness["liveness_state"] == "lost"
    assert "finalization_progress" not in liveness
    assert _without_clock(liveness) == _without_clock(baseline)


@pytest.mark.parametrize("name", sorted(_MALFORMED_FINALIZERS))
def test_malformed_finalizer_section_fails_closed_to_lost(tmp_path, name):
    liveness = _snapshot(tmp_path, _supervisor_record(), _MALFORMED_FINALIZERS[name]())

    assert liveness["liveness_state"] == "lost"
    assert "finalization_progress" not in liveness


# --- (b2) a heartbeat dated in the future is not evidence of a live finalizer ---

_FUTURE_BEATS = {
    "an_hour_ahead": lambda: time.time() + 3600.0,
    "just_beyond_the_skew_allowance": lambda: (
        time.time() + process_launcher.FINALIZER_CLOCK_SKEW_SECONDS + 5.0
    ),
    "a_trillion_seconds_ahead": lambda: 1e12,
    "near_the_float_maximum": lambda: 1.7e308,
    "beyond_the_float_range": lambda: 10**400,
}


@pytest.mark.parametrize("name", sorted(_FUTURE_BEATS))
def test_future_finalizer_heartbeat_is_not_trusted_and_keeps_todays_lost_result(tmp_path, name):
    record = _supervisor_record()
    baseline = _snapshot(tmp_path / "baseline", record)

    liveness = _snapshot(
        tmp_path / "case", record, _finalizer_section(heartbeat_at_epoch=_FUTURE_BEATS[name]()),
    )

    assert baseline["liveness_state"] == "lost"
    assert liveness["liveness_state"] == "lost"
    assert "finalization_progress" not in liveness
    assert _without_clock(liveness) == _without_clock(baseline)


def test_finalizer_heartbeat_clock_skew_allowance_is_exactly_thirty_seconds():
    now = 1_700_000_000.0
    skew = process_launcher.FINALIZER_CLOCK_SKEW_SECONDS

    def overlay(beat: float) -> dict:
        return process_launcher.finalizer_liveness(
            {"state": "finalizing"},
            {"finalizer": _finalizer_section(heartbeat_at_epoch=beat)},
            now_epoch=now,
        )

    assert skew == 30.0
    assert overlay(now - 1.0)["liveness_state"] == "finalizing"
    assert overlay(now + skew)["liveness_state"] == "finalizing"
    assert overlay(now + skew + 0.5) == {}
    assert overlay(now + 3600.0) == {}


# --- (b3) the finalizer section is untrusted input: the reader bounds what it surfaces ---

_PHASE_CAP = process_launcher.FINALIZER_COMMAND_MAX_CHARS
_BIDI_OVERRIDE = chr(0x202E)
_ZERO_WIDTH_SPACE = chr(0x200B)
_PRINTABLE_UNICODE = chr(0x691C) + chr(0x8A3C) + " " + chr(0xE9)

_HOSTILE_PROGRESS = {
    "total_negative": {"total": -1},
    "total_bool": {"total": True},
    "total_float": {"total": 3.0},
    "total_string": {"total": "3"},
    "total_none": {"total": None},
    "total_missing": {"total": _MISSING},
    "total_above_the_cap": {"total": process_launcher.FINALIZER_PROGRESS_MAX_TOTAL + 1},
    "total_beyond_the_int_string_limit": {"total": 10**5000},
    "phase_missing": {"phase": _MISSING},
    "phase_not_a_string": {"phase": 5},
    "phase_empty": {"phase": ""},
    "phase_only_control_characters": {"phase": "\x1b\x00\n" + _BIDI_OVERRIDE},
    "elapsed_missing": {"elapsed_seconds": _MISSING},
    "elapsed_negative": {"elapsed_seconds": -1.0},
    "elapsed_nan": {"elapsed_seconds": float("nan")},
    "elapsed_infinity": {"elapsed_seconds": float("inf")},
    "elapsed_bool": {"elapsed_seconds": True},
    "elapsed_string": {"elapsed_seconds": "12"},
    "elapsed_above_the_cap": {"elapsed_seconds": process_launcher.FINALIZER_PROGRESS_MAX_SECONDS * 10},
    "elapsed_beyond_the_float_range": {"elapsed_seconds": 10**400},
}

_PHASE_CASES = {
    "printable_text_is_kept": ("validating", "validating"),
    "printable_unicode_is_kept": (_PRINTABLE_UNICODE, _PRINTABLE_UNICODE),
    "escape_sequences_lose_their_control_characters": ("valid\x1b[31mating\n", "valid[31mating"),
    "bidi_and_zero_width_characters_are_removed": (
        f"{_BIDI_OVERRIDE}evil{_ZERO_WIDTH_SPACE}\x7f\x85", "evil",
    ),
    "tabs_and_line_breaks_are_removed": ("tab\tsplit\r\nlines", "tabsplitlines"),
    "stripping_happens_before_clipping": ("a\x1b" * 500, "a" * _PHASE_CAP),
    "an_oversized_phase_is_clipped": ("x" * 10_000, "x" * _PHASE_CAP),
    "one_char_over_the_cap_is_clipped": ("x" * (_PHASE_CAP + 1), "x" * _PHASE_CAP),
    "exactly_the_cap_is_kept": ("x" * _PHASE_CAP, "x" * _PHASE_CAP),
}


@pytest.mark.parametrize("name", sorted(_HOSTILE_PROGRESS))
def test_malformed_or_unbounded_progress_is_dropped_while_the_overlay_stays_finalizing(name):
    section = _finalizer_section(**_HOSTILE_PROGRESS[name])

    overlay = process_launcher.finalizer_liveness({"state": "finalizing"}, {"finalizer": section})

    assert overlay["liveness_state"] == "finalizing"
    assert overlay["finalization_progress"] == {}
    assert process_launcher.format_finalization_progress(overlay["finalization_progress"]) == ""
    assert process_launcher.format_finalization_progress(section) == ""


@pytest.mark.parametrize("name", sorted(_PHASE_CASES))
def test_phase_is_stripped_of_control_characters_and_clipped_when_read(tmp_path, name):
    phase, expected = _PHASE_CASES[name]

    liveness = _snapshot(tmp_path, _supervisor_record(), _finalizer_section(phase=phase))

    progress = liveness["finalization_progress"]
    assert liveness["liveness_state"] == "finalizing"
    assert progress["phase"] == expected
    assert progress["phase"].isprintable()
    assert len(progress["phase"]) <= _PHASE_CAP
    text = process_launcher.format_finalization_progress(progress)
    assert text == f"{expected} 3 command(s), 12.5s"
    assert text.isprintable()


def test_unknown_and_oversized_extra_fields_never_reach_the_progress_record(tmp_path):
    section = _finalizer_section(
        command="x" * 50_000, index=7, error="y" * 50_000, nested={"deep": ["z" * 50_000]},
    )

    overlay = process_launcher.finalizer_liveness({"state": "finalizing"}, {"finalizer": section})
    liveness = _snapshot(tmp_path, _supervisor_record(), section)

    assert set(overlay["finalization_progress"]) == {"phase", "total", "elapsed_seconds"}
    assert len(json.dumps(overlay)) < 1_000
    assert liveness["finalization_progress"] == overlay["finalization_progress"]


def test_active_state_liveness_is_unchanged_by_the_finalizer_overlay(tmp_path):
    ticks = _own_ticks()
    now = time.time()
    status_path = tmp_path / "supervisor.json"
    _write_status(status_path, {
        "state": "running",
        "heartbeat_at_epoch": now - 1.0,
        "last_output_change_epoch": now - 1.0,
        "started_at_epoch": now - 30.0,
        "heartbeat_seq": 3,
        "finalizer": _finalizer_section(),
    })
    row = {**_pending_row(status_path, "running"), "pid": os.getpid(), "pid_start_ticks": ticks}

    alive = process_launcher.ProcessManager._liveness_snapshot(row)
    dead = process_launcher.ProcessManager._liveness_snapshot(
        {**row, "pid": DEAD_PID, "pid_start_ticks": DEAD_TICKS}
    )

    assert alive["liveness_state"] == "alive"
    assert alive["supervisor_alive"] is True
    assert "finalization_progress" not in alive
    assert dead["liveness_state"] == "lost"
    assert dead["supervisor_alive"] is False


def test_derive_liveness_state_stays_pure_and_unchanged():
    now = 1_000_000.0
    arguments = {"now_epoch": now, "heartbeat_at_epoch": now - 1.0, "last_output_change_epoch": now - 1.0}

    alive = process_launcher.derive_liveness_state(supervisor_alive=True, **arguments)
    lost = process_launcher.derive_liveness_state(supervisor_alive=False, **arguments)

    assert alive["liveness_state"] == "alive"
    assert lost["liveness_state"] == "lost"
    assert alive == process_launcher.derive_liveness_state(supervisor_alive=True, **arguments)
    assert set(alive) == {
        "liveness_state", "heartbeat_age_seconds", "activity_age_seconds",
        "heartbeat_lease_seconds", "quiet_warning_seconds", "lost_recovery_grace_seconds",
    }
    assert process_launcher.LIVENESS_STATES == ("alive", "quiet", "unresponsive", "lost")
    parameters = inspect.signature(process_launcher.derive_liveness_state).parameters
    assert list(parameters) == [
        "now_epoch", "supervisor_alive", "heartbeat_at_epoch", "last_output_change_epoch",
        "lease_seconds", "warning_seconds", "grace_seconds",
    ]
    assert all(parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in parameters.values())


# --- (c) reconcile_dead_processing never reconciles a finalizing request ---


def _patched_reconcile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, manager):
    core = process_launcher.core
    stale_recovery = pytest.importorskip("aiworkhub.stale_recovery")
    calls: list[dict] = []

    def spy(**kwargs):
        calls.append(kwargs)
        return False, "spy"

    monkeypatch.setattr(
        core, "_live_card", lambda task_id: ({"task_id": task_id, "topic": "coding", "claimed_at": ""}, None)
    )
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *args, **kwargs: None)
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: "codex")
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path / "repo")
    monkeypatch.setattr(process_launcher, "ProcessManager", lambda **_kwargs: manager)
    monkeypatch.setattr(stale_recovery, "reconcile_dead_processing_claim", spy)
    return core, calls


def test_reconcile_dead_processing_skips_a_request_with_a_live_finalizer(tmp_path, monkeypatch):
    manager = _build_manager(tmp_path)
    status_path = _seed_pending(manager, tmp_path, finalizer=_finalizer_section())
    before = status_path.read_bytes()
    core, calls = _patched_reconcile(monkeypatch, tmp_path, manager)

    result = core.reconcile_dead_processing_task(TASK_ID, REQUEST_ID, 1)

    assert result["ok"] is False
    assert result["stderr"] == "reconcile_failed:process_finalizing"
    assert calls == []
    assert status_path.read_bytes() == before
    assert len(manager._request_events(REQUEST_ID)) == 1


def test_reconcile_dead_processing_still_reaches_recovery_for_a_dead_finalizer(tmp_path, monkeypatch):
    manager = _build_manager(tmp_path)
    section = _finalizer_section(pid=DEAD_PID, pid_start_ticks=DEAD_TICKS)
    _seed_pending(manager, tmp_path, finalizer=section)
    core, calls = _patched_reconcile(monkeypatch, tmp_path, manager)

    result = core.reconcile_dead_processing_task(TASK_ID, REQUEST_ID, 1)

    assert len(calls) == 1
    assert calls[0]["process_status"]["liveness"]["liveness_state"] == "lost"
    assert result["ok"] is False
    assert result["stderr"] == "reconcile_failed:spy"


# --- task_status and the dashboard surface "validating N command(s), Ts" ---


@pytest.mark.parametrize("finalizer_alive", [True, False], ids=["live_finalizer", "dead_finalizer"])
def test_task_status_snapshot_shows_validating_progress_only_for_a_live_finalizer(
    tmp_path, monkeypatch, finalizer_alive,
):
    core = process_launcher.core
    section = (
        _finalizer_section() if finalizer_alive
        else _finalizer_section(pid=DEAD_PID, pid_start_ticks=DEAD_TICKS)
    )
    status_path = tmp_path / "supervisor.json"
    _write_status(status_path, _supervisor_record(finalizer=section))
    row = {**_pending_row(status_path), "timestamp": "2026-10-22T10:00:00+00:00"}
    monkeypatch.setattr(core, "_claude_session_process_rows", lambda session_id, watch: [dict(row)])
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path / "repo")

    snapshot = core._claude_session_liveness_snapshot({"session_id": "s-nf1647"}, [REQUEST_ID])

    [entry] = snapshot["requests"]
    assert entry["state"] == "finalizing"
    if finalizer_alive:
        assert entry["liveness_state"] == "finalizing"
        assert entry["validation_progress"] == PROGRESS_TEXT
    else:
        assert entry["liveness_state"] == "lost"
        assert "validation_progress" not in entry


def _dashboard():
    return pytest.importorskip("aiworkhub.dashboard")


def _ledger(tmp_path: Path, status_path: Path) -> Path:
    ledger = tmp_path / "events.jsonl"
    event = {
        "request_id": REQUEST_ID,
        "task_id": TASK_ID,
        "runner": RUNNER,
        "topic": "coding",
        "state": "finalizing",
        "pid": DEAD_PID,
        "pid_start_ticks": DEAD_TICKS,
        "timestamp": "2026-10-22T10:00:00+00:00",
        "supervisor_status_path": str(status_path),
    }
    ledger.write_text(json.dumps(event) + "\n", encoding="utf-8")
    return ledger


def test_dashboard_process_and_task_rows_show_validating_progress(tmp_path):
    dashboard = _dashboard()
    status_path = tmp_path / "supervisor.json"
    _write_status(status_path, _supervisor_record(finalizer=_finalizer_section()))

    report = dashboard.read_process_runs(process_log_path=_ledger(tmp_path, status_path))

    [process] = report["processes"]
    assert process["liveness_state"] == "finalizing"
    assert process["validation_progress"] == PROGRESS_TEXT
    groups = {"processing": [{"task_id": TASK_ID}]}
    dashboard._merge_process_liveness_into_tasks(groups, report)
    [task] = groups["processing"]
    assert task["liveness_state"] == "finalizing"
    assert task["validation_progress"] == PROGRESS_TEXT


@pytest.mark.parametrize("name", sorted(_DEAD_FINALIZERS))
def test_dashboard_rows_of_a_dead_or_stale_finalizer_carry_no_finalizing_liveness(tmp_path, name):
    dashboard = _dashboard()
    status_path = tmp_path / "supervisor.json"
    _write_status(status_path, _supervisor_record(finalizer=_DEAD_FINALIZERS[name]()))

    report = dashboard.read_process_runs(process_log_path=_ledger(tmp_path, status_path))

    [process] = report["processes"]
    assert process["state"] == "finalizing"
    assert "liveness_state" not in process
    assert "validation_progress" not in process
    groups = {"processing": [{"task_id": TASK_ID}]}
    dashboard._merge_process_liveness_into_tasks(groups, report)
    [task] = groups["processing"]
    assert "liveness_state" not in task
    assert "validation_progress" not in task


@pytest.mark.parametrize(
    "progress, expected",
    [
        ({"phase": "validating", "total": 3, "elapsed_seconds": 12.5}, "validating 3 command(s), 12.5s"),
        ({"phase": "validating", "total": 1, "elapsed_seconds": 0}, "validating 1 command(s), 0s"),
        ({"phase": "validating", "total": 0, "elapsed_seconds": 2.0}, "validating 0 command(s), 2.0s"),
        ({"phase": "validating", "total": 2, "elapsed_seconds": -0.0}, "validating 2 command(s), 0.0s"),
        ({"phase": "validating", "total": 2, "elapsed_seconds": 3.04}, "validating 2 command(s), 3.0s"),
        (
            {"phase": "validating", "total": 2, "elapsed_seconds": 1.0, "index": 7, "command": "x" * 500},
            "validating 2 command(s), 1.0s",
        ),
        (None, ""),
        ({}, ""),
        ("validating", ""),
        ({"index": 1, "total": 2, "command": "pytest -q"}, ""),
        ({"phase": "validating", "total": True, "elapsed_seconds": 1.0}, ""),
        ({"phase": "validating", "total": -1, "elapsed_seconds": 1.0}, ""),
        ({"phase": "validating", "total": "3", "elapsed_seconds": 1.0}, ""),
        ({"phase": "validating", "total": 3.0, "elapsed_seconds": 1.0}, ""),
        ({"phase": "validating", "total": None, "elapsed_seconds": 1.0}, ""),
        ({"phase": "validating", "elapsed_seconds": 1.0}, ""),
        ({"phase": "", "total": 3, "elapsed_seconds": 1.0}, ""),
        ({"phase": 5, "total": 3, "elapsed_seconds": 1.0}, ""),
        ({"total": 3, "elapsed_seconds": 1.0}, ""),
        ({"phase": "validating", "total": 3}, ""),
        ({"phase": "validating", "total": 3, "elapsed_seconds": -1.0}, ""),
        ({"phase": "validating", "total": 3, "elapsed_seconds": "12"}, ""),
        ({"phase": "validating", "total": 3, "elapsed_seconds": True}, ""),
        ({"phase": "validating", "total": 3, "elapsed_seconds": float("nan")}, ""),
        ({"phase": "validating", "total": 3, "elapsed_seconds": float("inf")}, ""),
        ({"phase": "validating", "total": 3, "elapsed_seconds": 1e12}, ""),
    ],
)
def test_format_finalization_progress(progress, expected):
    assert process_launcher.format_finalization_progress(progress) == expected


# --- the finalizer's heartbeat record and its lifecycle ---


def test_write_finalizer_heartbeat_sets_replaces_and_clears_only_its_own_section(tmp_path):
    _require_owner_only_writer(tmp_path)
    path = tmp_path / "supervisor.json"
    record = _supervisor_record()
    _write_status(path, record)
    first = {"pid": 1, "heartbeat_at_epoch": 1.0}
    second = {"pid": 2, "heartbeat_at_epoch": 2.0}

    assert process_launcher.write_finalizer_heartbeat(path, first) is True
    assert process_launcher.read_supervisor_status(path) == {**record, "finalizer": first}
    assert process_launcher.write_finalizer_heartbeat(path, second) is True
    assert process_launcher.read_supervisor_status(path) == {**record, "finalizer": second}
    assert process_launcher.write_finalizer_heartbeat(path, None) is True
    assert process_launcher.read_supervisor_status(path) == record
    assert process_launcher.write_finalizer_heartbeat(path, None) is True
    assert process_launcher.read_supervisor_status(path) == record


def test_write_finalizer_heartbeat_never_creates_or_rewrites_untrusted_evidence(tmp_path):
    absent = tmp_path / "absent.json"
    garbage = tmp_path / "garbage.json"
    garbage.write_bytes(b"{not json")

    assert process_launcher.write_finalizer_heartbeat(absent, {"pid": 1}) is False
    assert not absent.exists()
    assert process_launcher.write_finalizer_heartbeat(garbage, {"pid": 1}) is False
    assert garbage.read_bytes() == b"{not json"


def test_finalizer_heartbeat_publishes_progress_renews_and_clears_on_exit(tmp_path, monkeypatch):
    ticks = _own_ticks()
    writes = _record_heartbeat_writes(monkeypatch)
    monkeypatch.setattr(process_launcher, "FINALIZER_HEARTBEAT_INTERVAL_SECONDS", 0.02)
    status_path = tmp_path / "supervisor.json"

    with process_launcher._FinalizerHeartbeat() as heartbeat:
        heartbeat.start(status_path)
        path, first = writes[0]
        assert path == status_path
        assert (first["pid"], first["pid_start_ticks"]) == (os.getpid(), ticks)
        assert (first["phase"], first["total"]) == ("", 0)

        heartbeat.note(process_launcher.FINALIZER_VALIDATING_PHASE, 2)
        noted = _eventually(
            lambda: [write[1] for write in list(writes) if write[1] and write[1]["phase"] == "validating"],
            lambda sections: len(sections) >= 5,
        )
        assert len(noted) >= 5
        assert {section["total"] for section in noted} == {2}
        assert all(section["elapsed_seconds"] >= 0 for section in noted)
        epochs = [section["heartbeat_at_epoch"] for section in noted]
        assert max(epochs) > min(epochs)

        heartbeat.note("settling", 5)
        renewed = _eventually(
            lambda: [write[1] for write in list(writes) if write[1] and write[1]["phase"] == "settling"],
            lambda sections: len(sections) >= 2,
        )
        assert len(renewed) >= 2
        assert {section["total"] for section in renewed} == {5}
        assert all(section["elapsed_seconds"] < 5.0 for section in renewed)

    assert writes[-1] == (status_path, None)
    settled = len(writes)
    time.sleep(0.15)
    assert len(writes) == settled
    assert _eventually(_heartbeat_threads, lambda threads: not threads, timeout=2.0) == []


# --- finding 1: the published progress advances without any per-command plumbing ---


def test_finalizer_progress_advances_while_validations_run(tmp_path, monkeypatch):
    _own_ticks()
    writes = _record_heartbeat_writes(monkeypatch)
    monkeypatch.setattr(process_launcher, "FINALIZER_HEARTBEAT_INTERVAL_SECONDS", 0.02)

    def validating() -> list[dict]:
        return [
            write[1] for write in list(writes)
            if write[1] and write[1]["phase"] == process_launcher.FINALIZER_VALIDATING_PHASE
        ]

    with process_launcher._FinalizerHeartbeat() as heartbeat:
        heartbeat.start(tmp_path / "supervisor.json")
        heartbeat.note(process_launcher.FINALIZER_VALIDATING_PHASE, len(COMMANDS))
        sections = _eventually(validating, lambda found: bool(found) and found[-1]["elapsed_seconds"] >= 0.3)

    assert sections, "the validating phase was never published"
    assert {frozenset(section) for section in sections} == {
        frozenset({"pid", "pid_start_ticks", "heartbeat_at_epoch", "phase", "total", "elapsed_seconds"})
    }
    assert {section["total"] for section in sections} == {len(COMMANDS)}
    elapsed = [section["elapsed_seconds"] for section in sections]
    assert elapsed == sorted(elapsed)
    assert elapsed[0] < elapsed[-1], "the published elapsed time never advanced"
    assert elapsed[-1] >= 0.3
    assert process_launcher.format_finalization_progress(sections[-1]) == (
        f"validating {len(COMMANDS)} command(s), {elapsed[-1]}s"
    )


def test_finalizer_heartbeat_publishes_nothing_without_an_exact_process_identity(tmp_path, monkeypatch):
    writes = _record_heartbeat_writes(monkeypatch)
    monkeypatch.setattr(process_launcher, "_pid_start_ticks", lambda pid: None)

    with process_launcher._FinalizerHeartbeat() as heartbeat:
        heartbeat.start(tmp_path / "supervisor.json")
        heartbeat.note(process_launcher.FINALIZER_VALIDATING_PHASE, 2)

    assert writes == []
    assert _heartbeat_threads() == []


def test_finalizer_heartbeat_record_reads_as_finalizing_until_the_finalizer_exits(tmp_path):
    _require_owner_only_writer(tmp_path)
    _own_ticks()
    status_path = tmp_path / "supervisor.json"
    _write_status(status_path, _supervisor_record())
    row = _pending_row(status_path)

    def liveness() -> dict:
        return process_launcher.ProcessManager._liveness_snapshot(row)

    assert liveness()["liveness_state"] == "lost"
    with process_launcher._FinalizerHeartbeat() as heartbeat:
        heartbeat.start(status_path)
        heartbeat.note(process_launcher.FINALIZER_VALIDATING_PHASE, 2)
        live = _eventually(liveness, lambda snapshot: snapshot.get("liveness_state") == "finalizing")
        assert live["liveness_state"] == "finalizing"
        progress = live["finalization_progress"]
        assert (progress["phase"], progress["total"]) == ("validating", 2)
        assert process_launcher.format_finalization_progress(progress).startswith("validating 2 command(s), ")

    cleared = _eventually(liveness, lambda snapshot: snapshot.get("liveness_state") == "lost")
    assert cleared["liveness_state"] == "lost"
    assert "finalizer" not in process_launcher.read_supervisor_status(status_path)


def test_nested_heartbeat_for_the_same_request_shares_one_writer(tmp_path, monkeypatch):
    _own_ticks()
    writes = _record_heartbeat_writes(monkeypatch)
    status_path = tmp_path / "supervisor.json"

    with process_launcher._FinalizerHeartbeat("req-a") as outer:
        outer.start(status_path)
        with process_launcher._FinalizerHeartbeat("req-a") as inner:
            assert inner is outer
            published = len(writes)
            inner.start(status_path)
            assert len(writes) == published, "a nested start published a second first beat"
            assert len(_heartbeat_threads()) == 1
        assert len(_heartbeat_threads()) == 1, "the nested exit stopped the owner's heartbeat"
        assert (status_path, None) not in writes, "the nested exit cleared the owner's section"
        with process_launcher._FinalizerHeartbeat("req-b") as other_request:
            assert other_request is not outer
        with process_launcher._FinalizerHeartbeat("") as anonymous:
            assert anonymous is not outer
        assert len(_heartbeat_threads()) == 1

    assert writes[-1] == (status_path, None)
    assert _heartbeat_threads() == []


# --- the real finalize path publishes progress while validations run ---


def _card() -> dict:
    return {
        "task_id": TASK_ID,
        "runner": RUNNER,
        "topic": "coding",
        "status": "processing",
        "worker_status": "in_progress",
        "claimed_by": RUNNER,
        "claim_epoch": 1,
        "review_requested_by": "",
        "allowed_writes": ["out/result.txt"],
    }


def _build_manager(tmp_path: Path, card: dict | None = None) -> process_launcher.ProcessManager:
    card = card if card is not None else _card()
    return process_launcher.ProcessManager(
        repo=tmp_path / "repo",
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=lambda task_id: {"returncode": 0, "stdout": json.dumps(card), "stderr": ""},
        collision_guard=lambda **_kwargs: {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""},
        adapter_builder=lambda **_kwargs: SimpleNamespace(
            argv=[], cwd=str(tmp_path), launchable=True, reason=""
        ),
        isolation_enabled=True,
    )


def _seed_pending(manager, tmp_path: Path, *, state: str = "finalizing", finalizer=_MISSING) -> Path:
    status_path = tmp_path / "processes" / f"{REQUEST_ID}.supervisor.json"
    payload = _supervisor_record()
    if finalizer is not _MISSING:
        payload["finalizer"] = finalizer
    _write_status(status_path, payload)
    manager._append_event({
        "request_id": REQUEST_ID,
        "task_id": TASK_ID,
        "runner": RUNNER,
        "topic": "coding",
        "adapter_id": "claude_cli",
        "state": state,
        "pid": DEAD_PID,
        "pid_start_ticks": DEAD_TICKS,
        "supervisor_status_path": str(status_path),
    })
    return status_path


def _running_supervisor_record(**extra) -> dict:
    """A supervisor record still claiming ``running`` whose own heartbeat lease has long expired."""
    record = _supervisor_record(state="running", heartbeat_at_epoch=_stale_heartbeat_epoch(), **extra)
    for terminal_only in ("exit_code", "finished_at_epoch"):
        del record[terminal_only]
    return record


def _seed_isolated_request(
    manager,
    tmp_path: Path,
    card: dict,
    *,
    status: dict | None = None,
    pid: int = DEAD_PID,
    pid_start_ticks: int = DEAD_TICKS,
    state: str = "running",
) -> Path:
    process_dir = tmp_path / "processes"
    process_dir.mkdir(parents=True, exist_ok=True)
    status_path = process_dir / f"{REQUEST_ID}.supervisor.json"
    metadata_path = process_dir / f"{REQUEST_ID}.request.json"
    stdout_path = process_dir / f"{REQUEST_ID}.stdout.log"
    stderr_path = process_dir / f"{REQUEST_ID}.stderr.log"
    for log in (stdout_path, stderr_path):
        os.close(os.open(log, os.O_CREAT | os.O_WRONLY, 0o600))
    _write_status(status_path, status or _supervisor_record(child_pid=DEAD_PID, child_pid_start_ticks=DEAD_TICKS))
    workspace = {
        "request_id": REQUEST_ID,
        "repo": str(manager.repo),
        "path": str(tmp_path / "workspace" / REQUEST_ID),
        "home": str(tmp_path / "home" / REQUEST_ID),
        "allowed_writes": list(card["allowed_writes"]),
        "parent_baseline": {},
        "workspace_baseline": {},
    }
    output = Path(workspace["path"]) / "out/result.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("worker-result\n", encoding="utf-8")
    process_launcher.write_json_0600(metadata_path, {
        "request_id": REQUEST_ID,
        "task_id": card["task_id"],
        "claim_epoch": card["claim_epoch"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": "claude_cli",
        "model": None,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "cancel_path": str(process_dir / f"{REQUEST_ID}.cancel.json"),
        "metadata_path": str(metadata_path),
        "validation": list(COMMANDS),
        "sandbox_backend": "landlock",
        "workspace": workspace,
    })
    manager._append_event({
        "request_id": REQUEST_ID,
        "task_id": card["task_id"],
        "runner": card["runner"],
        "topic": card["topic"],
        "adapter_id": "claude_cli",
        "state": state,
        "pid": pid,
        "pid_start_ticks": pid_start_ticks,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "metadata_path": str(metadata_path),
        "supervisor_status_path": str(status_path),
    })
    return status_path


def _merged_request(manager) -> dict:
    merged: dict = {}
    for event in manager._request_events(REQUEST_ID):
        merged.update({key: value for key, value in event.items() if value is not None})
    return merged


def _probe_validations(manager, monkeypatch) -> list[dict]:
    """Abort the finalizer at its first declared validation, recording what a reader sees there."""
    probes: list[dict] = []

    def validation_probe(*_args, **_kwargs):
        merged = _merged_request(manager)
        probes.append({
            "state": merged.get("state"),
            "threads": len(_heartbeat_threads()),
            "liveness": _eventually(
                lambda: process_launcher.ProcessManager._liveness_snapshot(merged),
                lambda snapshot: snapshot.get("liveness_state") == "finalizing",
            ),
        })
        raise RuntimeError("nf1647_probe_taken")

    monkeypatch.setattr(process_launcher, "enforce_scope", lambda workspace, **_kwargs: ["out/result.txt"])
    monkeypatch.setattr(process_launcher, "_run_declared_validations", validation_probe)
    monkeypatch.setattr(process_launcher, "_run_full_snapshot_validations", validation_probe)
    monkeypatch.setattr(process_launcher.task_engine, "mark_terminal_failure", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(process_launcher.task_engine, "mark_terminal_review", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: True)
    return probes


def _until_probed(probes: list[dict], call):
    try:
        return call()
    except Exception:
        # The probe aborts validation on purpose; only what the heartbeat published and then cleared is under test.
        if not probes:
            raise
        return None


def _assert_validating(probe: dict) -> None:
    assert probe["state"] in process_launcher.FINALIZATION_PENDING_STATES
    liveness = probe["liveness"]
    assert liveness.get("liveness_state") == "finalizing", liveness
    progress = liveness["finalization_progress"]
    assert (progress["phase"], progress["total"]) == (process_launcher.FINALIZER_VALIDATING_PHASE, len(COMMANDS))
    assert set(progress) == {"phase", "total", "elapsed_seconds"}
    assert process_launcher.format_finalization_progress(progress).startswith(
        f"validating {len(COMMANDS)} command(s), "
    )


def _authorize_retry(monkeypatch) -> None:
    """Let ``retry_finalization`` reach its finalizing transition for a seeded ``finalize_failed`` request."""
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: True)
    monkeypatch.setattr(process_launcher, "assert_gc_safe_workspace_shape", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_launcher, "_calling_manager_identity", lambda *_args, **_kwargs: {"provider": "codex"})
    monkeypatch.setattr(
        process_launcher._toolchain_authority, "refresh_drifted_receipt", lambda *_args, **_kwargs: (None, None)
    )
    monkeypatch.setattr(process_launcher.task_engine, "retry_finalize_failed", lambda *_args, **_kwargs: {"ok": True})


def test_finalize_isolated_request_publishes_validation_progress_and_clears_it(tmp_path, monkeypatch):
    _require_named_pipes()
    _require_owner_only_writer(tmp_path)
    _own_ticks()
    card = {**_card(), "validation": list(COMMANDS)}
    manager = _build_manager(tmp_path, card)
    status_path = _seed_isolated_request(manager, tmp_path, card)
    probes = _probe_validations(manager, monkeypatch)

    _until_probed(probes, lambda: manager._finalize_isolated_request(REQUEST_ID))

    assert len(probes) == 1, "the finalizer never reached its declared validations"
    _assert_validating(probes[0])
    record = _eventually(lambda: process_launcher.read_supervisor_status(status_path), bool)
    assert record
    assert "finalizer" not in record
    assert _heartbeat_threads() == []


# --- finding 4: the heartbeat is live before the terminate grace window opens ---


class _Aborted(Exception):
    """Raised by the terminate spy so the finalizer stops exactly where it would have signalled."""


@pytest.mark.parametrize("scenario", ["lease_lost_supervisor", "dead_supervisor_with_a_live_child"])
def test_finalizer_heartbeat_is_live_before_a_process_group_is_terminated(tmp_path, monkeypatch, scenario):
    _require_named_pipes()
    _require_owner_only_writer(tmp_path)
    ticks = _own_ticks()
    card = {**_card(), "validation": list(COMMANDS)}
    manager = _build_manager(tmp_path, card)
    if scenario == "lease_lost_supervisor":
        supervisor, terminated = (os.getpid(), ticks), os.getpid()
    else:
        supervisor, terminated = (DEAD_PID, DEAD_TICKS), 4242
        verdict = process_launcher._pid_identity_evidence(*supervisor).verdict
        if verdict is process_launcher.PidIdentityVerdict.UNKNOWN:
            pytest.skip("validation_unsupported_in_sandbox:pid_identity_unknown")
        monkeypatch.setattr(process_launcher, "_identity_verified_pid", lambda *_args, **_kwargs: terminated)
    status_path = _seed_isolated_request(
        manager, tmp_path, card,
        status=_running_supervisor_record(child_pid=DEAD_PID, child_pid_start_ticks=DEAD_TICKS),
        pid=supervisor[0], pid_start_ticks=supervisor[1],
    )
    observed: list[dict] = []

    def terminate_spy(pid, **_kwargs):
        merged = _merged_request(manager)
        observed.append({
            "pid": pid,
            "state": merged.get("state"),
            "section": process_launcher.read_supervisor_status(status_path).get("finalizer"),
            "liveness": process_launcher.ProcessManager._liveness_snapshot(merged),
        })
        raise _Aborted

    monkeypatch.setattr(process_launcher, "_terminate_process_group", terminate_spy)

    with pytest.raises(_Aborted):
        manager._finalize_isolated_request(REQUEST_ID)

    [seen] = observed
    assert seen["pid"] == terminated
    assert seen["state"] in process_launcher.FINALIZATION_PENDING_STATES
    assert isinstance(seen["section"], dict), "no finalizer heartbeat was published before the terminate call"
    assert seen["section"].get("pid") == os.getpid()
    assert seen["liveness"].get("liveness_state") == "finalizing", seen["liveness"]
    record = _eventually(lambda: process_launcher.read_supervisor_status(status_path), bool)
    assert record
    assert "finalizer" not in record
    assert _heartbeat_threads() == []


# --- finding 5: retry_finalization is the other writer of finalization_started_at ---


def test_retry_finalization_publishes_the_heartbeat_before_the_request_reads_finalizing(tmp_path, monkeypatch):
    _require_named_pipes()
    _require_owner_only_writer(tmp_path)
    _own_ticks()
    card = {**_card(), "validation": list(COMMANDS)}
    manager = _build_manager(tmp_path, card)
    status_path = _seed_isolated_request(manager, tmp_path, card, state="finalize_failed")
    _authorize_retry(monkeypatch)
    seen: dict = {}
    append_event = manager._append_event

    def append_spy(event, *args, **kwargs):
        if event.get("state") == "finalizing":
            seen["section_when_finalizing_is_appended"] = (
                process_launcher.read_supervisor_status(status_path).get("finalizer")
            )
        return append_event(event, *args, **kwargs)

    def finalize_stub(request_id, supervisor_returncode):
        merged = _merged_request(manager)
        seen["finalize_call"] = (request_id, supervisor_returncode)
        seen["state"] = merged.get("state")
        seen["liveness"] = process_launcher.ProcessManager._liveness_snapshot(merged)
        seen["threads"] = len(_heartbeat_threads())
        return {"state": "review_ready", "workspace_retained": True}

    monkeypatch.setattr(manager, "_append_event", append_spy)
    monkeypatch.setattr(manager, "_finalize_isolated_request", finalize_stub)

    result = manager.retry_finalization(REQUEST_ID, TASK_ID)

    assert result["ok"] is True, result
    assert seen["finalize_call"] == (REQUEST_ID, 0)
    section = seen["section_when_finalizing_is_appended"]
    assert isinstance(section, dict), "the retry appended its finalizing row before it published a heartbeat"
    assert section.get("pid") == os.getpid()
    assert seen["state"] in process_launcher.FINALIZATION_PENDING_STATES
    assert seen["liveness"].get("liveness_state") == "finalizing", seen["liveness"]
    assert seen["threads"] == 1
    record = _eventually(lambda: process_launcher.read_supervisor_status(status_path), bool)
    assert record
    assert "finalizer" not in record
    assert _heartbeat_threads() == []


def test_retry_finalization_and_its_nested_finalize_share_one_heartbeat(tmp_path, monkeypatch):
    _require_named_pipes()
    _require_owner_only_writer(tmp_path)
    _own_ticks()
    card = {**_card(), "validation": list(COMMANDS)}
    manager = _build_manager(tmp_path, card)
    status_path = _seed_isolated_request(manager, tmp_path, card, state="finalize_failed")
    _authorize_retry(monkeypatch)
    probes = _probe_validations(manager, monkeypatch)
    starters: list[int] = []
    start = process_launcher._FinalizerHeartbeat.start

    def recording_start(self, *args, **kwargs):
        starters.append(id(self))
        return start(self, *args, **kwargs)

    monkeypatch.setattr(process_launcher._FinalizerHeartbeat, "start", recording_start)

    _until_probed(probes, lambda: manager.retry_finalization(REQUEST_ID, TASK_ID))

    assert len(probes) == 1, "the retried finalization never reached its declared validations"
    [probe] = probes
    assert len(starters) >= 2, "both the retry and the nested finalize should ask for a heartbeat"
    assert len(set(starters)) == 1, "the nested finalize started a second heartbeat writer"
    assert probe["threads"] == 1
    _assert_validating(probe)
    record = _eventually(lambda: process_launcher.read_supervisor_status(status_path), bool)
    assert record
    assert "finalizer" not in record
    assert _heartbeat_threads() == []
