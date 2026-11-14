"""NF-2026-02263: an in-flight accept says which phase it is in and how old it is.

Measured 2026-11-14: a marker for needfix-NF-2026-02246 was held ~65 minutes
by a live foreign server process (pid 44204). Every manager accept answered
``accept_in_progress`` with only ``started_at`` and ``holder_pid`` -- no phase,
no age -- while ``accept_preview`` said ``blocked`` False. A second marker from
2026-11-07, whose holder (pid 45160) was long gone, also lingered.

The holder now republishes its marker with a ``phase`` and ``updated_at`` as it
advances; the refusal carries ``phase``, ``age_seconds`` and
``updated_age_seconds``; preview and accept agree; and a marker whose holder is
proven gone is still cleared by the next accept. No path kills a live holder.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher  # noqa: E402
from aiworkhub import process_launcher_accept_review as par  # noqa: E402

import test_process_launcher_accept_review as _accept_env  # noqa: E402
from test_accept_review_in_flight_nf01626 import (  # noqa: E402,F401 -- fixtures
    REQUEST_ID,
    TASK_ID,
    _events,
    _exited_process,
    _first_accept_held_in_validation,
    _no_claim_outlives_its_test,
    _plant,
    _tree,
    _waiting_child,
    accept_env,
)

_PROGRESS_PHASES = {"revalidating", "combined_tree_gate"}


def _ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _marker(*, pid: int, ticks: Any, started_at: str, **extra: Any) -> dict[str, Any]:
    return {
        "schema_id": par.ACCEPT_IN_FLIGHT_SCHEMA_ID,
        "task_id": TASK_ID,
        "request_id": REQUEST_ID,
        "pid": pid,
        "pid_start_ticks": ticks,
        "started_at": started_at,
        "server_version": "0.12.98",
        **extra,
    }


def _facts(reply: dict[str, Any]) -> dict[str, Any]:
    return {key: reply[key] for key in ("holder_pid", "started_at", "phase", "updated_at")}


def test_a_live_foreign_holder_refusal_names_its_phase_and_ages(accept_env):
    """The 65-minute hold: the refusal says where the holder is and since when."""
    env = accept_env
    env.release.set()
    with _waiting_child() as holder:
        planted = _plant(
            env.marker,
            _marker(
                pid=holder.pid,
                ticks=process_launcher._pid_start_ticks(holder.pid),
                started_at=_ago(65),
                phase="combined_tree_gate",
                updated_at=_ago(60),
            ),
        )
        before = _tree(env.root)

        refused = _accept_env._accept(env.manager)
        preview = par.accept_preview(env.manager, REQUEST_ID, TASK_ID)

        assert _tree(env.root) == before, "a refusal or a preview wrote to disk"
        assert holder.poll() is None, "the live holder was disturbed"
    assert refused["ok"] is False
    assert refused["error"] == "accept_in_progress"
    assert refused["holder_pid"] == holder.pid
    assert refused["phase"] == "combined_tree_gate"
    assert 65 * 60 - 5 <= refused["age_seconds"] < 66 * 60
    assert 60 * 60 - 5 <= refused["updated_age_seconds"] < 61 * 60
    assert env.marker.read_bytes() == planted
    assert env.validating == []

    # Preview and accept agree: blocked, by the same holder in the same phase.
    assert preview["ok"] is True
    assert preview["blocked"] is True
    assert [row["kind"] for row in preview["blockers"]] == ["accept_in_progress"]
    assert _facts(preview["accept_in_progress"]) == _facts(refused)
    assert preview["accept_in_progress"]["updated_age_seconds"] >= 60 * 60 - 5


def test_a_marker_from_an_older_server_reads_as_phase_unreported(accept_env):
    env = accept_env
    env.release.set()
    with _waiting_child() as holder:
        _plant(
            env.marker,
            _marker(
                pid=holder.pid,
                ticks=process_launcher._pid_start_ticks(holder.pid),
                started_at=_ago(10),
            ),
        )
        refused = par.accept_in_flight(env.manager, TASK_ID, REQUEST_ID)
    assert refused["phase"] == "unreported"
    assert refused["updated_at"] == refused["started_at"]
    assert refused["updated_age_seconds"] == refused["age_seconds"] >= 10 * 60 - 5


def test_an_unparseable_timestamp_reports_no_age():
    """Unknown is never a number: a bad stamp is None, not zero."""
    reply = par._accept_in_progress(
        TASK_ID, REQUEST_ID,
        {"pid": 1, "started_at": "not-a-time", "updated_at": "2026-11-14T01:05:00"},
        verified=False,
    )
    assert reply["age_seconds"] is None
    assert reply["updated_age_seconds"] is None  # naive: no zone, no age


def test_the_running_holder_publishes_its_phase_and_releases_its_marker(accept_env):
    env = accept_env
    with _first_accept_held_in_validation(env) as (first, _worker):
        on_disk = json.loads(env.marker.read_text(encoding="utf-8"))
        duplicate = _accept_env._accept(env.manager)
        preview = par.accept_preview(env.manager, REQUEST_ID, TASK_ID)
    assert on_disk["phase"] in _PROGRESS_PHASES, on_disk
    assert on_disk["updated_at"] >= on_disk["started_at"]
    assert duplicate["error"] == "accept_in_progress"
    assert duplicate["phase"] == on_disk["phase"]
    assert duplicate["holder_identity"] == "verified"
    assert preview["blocked"] is True
    assert preview["accept_in_progress"]["phase"] == on_disk["phase"]
    assert first.get("result", {}).get("ok") is True, first
    # The rewritten marker is still this accept's own, so its release removes it.
    assert not env.marker.exists()
    assert not env.marker.with_name(f"{env.marker.name}.next").exists()
    log = env.marker.parent / par._ACCEPT_IN_FLIGHT_EVENTS_NAME
    assert not log.exists() or all(
        row["event"] != "phase_update_failed" for row in _events(env.marker)
    )
    assert par.accept_in_flight(env.manager, TASK_ID, REQUEST_ID) is None


def test_a_dead_holder_marker_is_cleared_by_the_next_accept(accept_env):
    """The 2026-11-07 leftover: its holder is gone, so the next accept clears it."""
    env = accept_env
    env.release.set()
    pid, ticks = _exited_process()
    stale = _marker(
        pid=pid, ticks=ticks, started_at="2026-11-07T10:00:00+00:00",
        server_version="0.12.92", phase="revalidating",
        updated_at="2026-11-07T10:05:00+00:00",
    )
    _plant(env.marker, stale)
    assert par.accept_in_flight(env.manager, TASK_ID, REQUEST_ID) is None
    assert par.accept_preview(env.manager, REQUEST_ID, TASK_ID).get(
        "accept_in_progress"
    ) is None

    result = _accept_env._accept(env.manager)

    assert result["ok"] is True, result
    assert env.validating
    assert not env.marker.exists()
    events = _events(env.marker)
    assert [row["event"] for row in events] == ["stale_marker_cleared"]
    assert events[0]["stale_marker"] == stale


def test_a_rewrite_never_replaces_bytes_another_holder_wrote(tmp_path):
    marker = tmp_path / par._ACCEPT_IN_FLIGHT_DIRNAME / "m.json"
    foreign = _plant(marker, _marker(pid=os.getpid(), ticks=None, started_at=_ago(1)))

    kept = par._rewrite_accept_marker(marker, b"{}", {"pid": 1, "phase": "promoting"})

    assert kept == b"{}"
    assert marker.read_bytes() == foreign


def test_a_failed_rewrite_is_recorded_and_keeps_the_old_bytes(tmp_path, monkeypatch):
    marker = tmp_path / par._ACCEPT_IN_FLIGHT_DIRNAME / "m.json"
    own = _plant(marker, _marker(pid=os.getpid(), ticks=None, started_at=_ago(1)))

    def _denied(*_args: Any) -> None:
        raise PermissionError(13, "sharing violation")

    monkeypatch.setattr(par.os, "replace", _denied)
    monkeypatch.setattr(par, "_ACCEPT_IN_FLIGHT_RETRY_SECONDS", 0)

    kept = par._rewrite_accept_marker(
        marker, own, {**json.loads(own), "phase": "promoting"}
    )

    assert kept == own
    assert marker.read_bytes() == own
    assert not marker.with_name(f"{marker.name}.next").exists()
    rows = _events(marker)
    assert [(row["event"], row["phase"]) for row in rows] == [
        ("phase_update_failed", "promoting")
    ]


@pytest.mark.parametrize("phase", ["promoting", "recording"])
def test_a_successful_rewrite_returns_the_new_bytes(tmp_path, phase):
    marker = tmp_path / par._ACCEPT_IN_FLIGHT_DIRNAME / "m.json"
    own = _plant(marker, _marker(pid=os.getpid(), ticks=None, started_at=_ago(1)))
    payload = {**json.loads(own), "phase": phase}

    written = par._rewrite_accept_marker(marker, own, payload)

    assert written == marker.read_bytes()
    assert json.loads(written)["phase"] == phase


def test_a_failure_after_the_replace_returns_the_new_bytes(tmp_path, monkeypatch):
    """An OSError once ``os.replace`` landed must not report the stale bytes."""
    marker = tmp_path / par._ACCEPT_IN_FLIGHT_DIRNAME / "m.json"
    own = _plant(marker, _marker(pid=os.getpid(), ticks=None, started_at=_ago(1)))
    payload = {**json.loads(own), "phase": "committing"}
    real_lock = par._accept_marker_dir_lock

    @contextlib.contextmanager
    def _lock_failing_on_release(directory: Path):
        with real_lock(directory):
            yield
        raise PermissionError(13, "lock release denied")

    monkeypatch.setattr(par, "_accept_marker_dir_lock", _lock_failing_on_release)

    returned = par._rewrite_accept_marker(marker, own, payload)

    assert returned == marker.read_bytes() != own
    assert json.loads(returned)["phase"] == "committing"
    assert [row["event"] for row in _events(marker)] == ["phase_update_failed"]
