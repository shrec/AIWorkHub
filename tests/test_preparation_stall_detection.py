"""NF-2026-00320: a frozen preparation heartbeat must be detectable WITHOUT a
pid and must yield a named reason. A launch that never spawned a process has no
pid to read liveness from -- the preparation heartbeat epoch is the only
progress signal, and when it stops advancing the launch is stuck.
"""

from __future__ import annotations

from aiworkhub import process_launcher as pl

PHASE = "reviewer_source_graph_prewarm_started"


def test_frozen_heartbeat_with_no_process_is_detected_without_a_pid():
    # Freeze the heartbeat far in the past; NO process ever spawned, so there
    # is no pid anywhere in this call. Detection must still fire.
    launch_epoch = 1000.0
    now = launch_epoch + 12 * 60  # twelve minutes, exactly the measured stall
    result = pl.derive_preparation_stall(
        now_epoch=now,
        preparation_heartbeat_epoch=launch_epoch,
        preparation_phase=PHASE,
        stall_seconds=180.0,
    )
    assert result["preparation_stalled"] is True
    assert result["preparation_heartbeat_age_seconds"] == 12 * 60
    # A named reason: non-empty, names the stall AND the exact frozen phase.
    assert result["reason"]
    assert result["reason"].startswith("preparation_heartbeat_stalled:")
    assert PHASE in result["reason"]


def test_fresh_heartbeat_is_not_a_stall():
    result = pl.derive_preparation_stall(
        now_epoch=1050.0,
        preparation_heartbeat_epoch=1000.0,
        preparation_phase=PHASE,
        stall_seconds=180.0,
    )
    assert result["preparation_stalled"] is False
    assert result["reason"] == ""


def test_never_published_heartbeat_is_not_a_stall():
    # Nothing has stopped advancing if nothing ever advanced.
    result = pl.derive_preparation_stall(
        now_epoch=1_000_000.0,
        preparation_heartbeat_epoch=None,
        preparation_phase=PHASE,
        stall_seconds=180.0,
    )
    assert result["preparation_stalled"] is False
    assert result["preparation_heartbeat_age_seconds"] is None
    assert result["reason"] == ""


def test_missing_phase_still_produces_a_named_reason():
    result = pl.derive_preparation_stall(
        now_epoch=2000.0,
        preparation_heartbeat_epoch=1000.0,
        preparation_phase=None,
        stall_seconds=180.0,
    )
    assert result["preparation_stalled"] is True
    assert "unknown_preparation_phase" in result["reason"]


def test_default_threshold_is_bounded():
    # The env-driven threshold stays inside sane bounds even when misconfigured.
    assert 30.0 <= pl.preparation_stall_seconds() <= 3600.0
