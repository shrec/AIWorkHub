"""NF-2026-01175: a lost Windows identity-slot race must not mask a real build error.

Two MCP server processes each run a Source Graph daemon. On Windows the one
that loses the build identity-slot race is refused with
``index_subprocess:identity_slot_owned``. That refusal is contention, not a new
index fault, so daemon health keeps the earlier real ``last_error`` and records
the race in ``last_slot_contention_at`` instead.

Every test uses its own ``tmp_path`` repository and daemon instance.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import source_graph, source_graph_daemon, task_store, worker_ai_tools_mcp  # noqa: E402
from aiworkhub.source_graph_daemon import SourceGraphDaemon  # noqa: E402

REAL_ERROR = "index_subprocess:sqlite:database disk image is malformed"


def _init_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    result = task_store.initialize_repository(root)
    assert result["ok"], result
    return root


def _drive_lost_race(monkeypatch, daemon: SourceGraphDaemon, *, windows: bool, retained):
    """Run the real subprocess ``_execute_build`` path up to a lost slot race."""
    daemon._build_execution = source_graph_daemon.BUILD_EXECUTION_SUBPROCESS
    actions: list[str] = []

    class Spawned:
        pid = 4242

    monkeypatch.setattr(source_graph_daemon.platform_io, "is_windows", lambda: windows)
    monkeypatch.setattr(
        source_graph_daemon, "_recover_dead_windows_build_identity", lambda *_args: False
    )
    monkeypatch.setattr(source_graph_daemon, "_cross_instance_identity_supported", lambda: False)
    monkeypatch.setattr(daemon, "_new_process_group_popen_kwargs", lambda: {})
    monkeypatch.setattr(
        source_graph_daemon.subprocess, "Popen", lambda *_args, **_kwargs: Spawned()
    )
    monkeypatch.setattr(source_graph_daemon, "_proc_identity", lambda _pid: None)
    monkeypatch.setattr(
        source_graph_daemon, "_publish_build_identity_if_unowned", lambda *_args: False
    )
    monkeypatch.setattr(source_graph_daemon, "_read_build_identity", lambda _root: retained)
    monkeypatch.setattr(daemon, "_has_prior_build", lambda: True)
    monkeypatch.setattr(daemon, "_terminate_build_process", lambda: actions.append("terminate"))
    monkeypatch.setattr(
        daemon, "_drain_after_stop", lambda _process, _pgid: actions.append("drain")
    )

    def _no_build(*_args, **_kwargs):
        raise AssertionError("a lost race must not start a build")

    monkeypatch.setattr(source_graph, "build_index", _no_build)
    return actions


def _fail_with_real_error(daemon: SourceGraphDaemon) -> None:
    daemon._execute_build = lambda *, incremental: {"kind": "error", "error": REAL_ERROR}
    assert daemon._run_one_build() is True
    daemon._execute_build = SourceGraphDaemon._execute_build.__get__(daemon)


def test_lost_slot_race_keeps_earlier_real_build_error(tmp_path, monkeypatch):
    root = _init_repo(tmp_path, "keeps_real_error")
    daemon = SourceGraphDaemon(root)
    assert daemon.health()["last_slot_contention_at"] == ""
    _drive_lost_race(monkeypatch, daemon, windows=True, retained=None)

    _fail_with_real_error(daemon)
    assert daemon._run_one_build() is True

    health = daemon.health()
    assert REAL_ERROR in health["last_error"]
    assert "identity_slot_owned" not in health["last_error"]
    assert health["last_slot_contention_at"] != ""
    assert health["status"] == source_graph_daemon.STATUS_DEGRADED
    assert health["ok"] is False


def test_lost_slot_race_without_earlier_error_reports_contention(tmp_path, monkeypatch):
    root = _init_repo(tmp_path, "no_earlier_error")
    daemon = SourceGraphDaemon(root)
    _drive_lost_race(monkeypatch, daemon, windows=True, retained=None)

    assert daemon._run_one_build() is True

    health = daemon.health()
    assert health["last_error"] == "RuntimeError:index_subprocess:identity_slot_owned"
    assert health["last_slot_contention_at"] != ""
    assert health["status"] == source_graph_daemon.STATUS_DEGRADED


def test_success_after_masked_contention_clears_last_error(tmp_path, monkeypatch):
    root = _init_repo(tmp_path, "success_after_contention")
    daemon = SourceGraphDaemon(root)
    _drive_lost_race(monkeypatch, daemon, windows=True, retained=None)
    _fail_with_real_error(daemon)
    assert daemon._run_one_build() is True
    contention_at = daemon.health()["last_slot_contention_at"]
    assert contention_at != ""

    monkeypatch.setattr(source_graph, "record_recommendation_roundtrip", lambda *_args: None)
    monkeypatch.setattr(
        worker_ai_tools_mcp,
        "source_graph_recommendation_roundtrip_gate",
        lambda _context: {"ok": True, "status": "ok"},
    )
    daemon._execute_build = lambda *, incremental: {
        "kind": "success",
        "report": {"files_seen": 3, "files_changed": 1, "files_removed": 0},
    }
    assert daemon._run_one_build() is True

    health = daemon.health()
    assert health["last_error"] == ""
    assert health["status"] == source_graph_daemon.STATUS_READY
    assert health["last_slot_contention_at"] == contention_at


def test_windows_lost_race_still_refuses_build_with_error_kind(tmp_path, monkeypatch):
    root = _init_repo(tmp_path, "windows_refusal")
    daemon = SourceGraphDaemon(root)
    actions = _drive_lost_race(monkeypatch, daemon, windows=True, retained=None)

    outcome = daemon._execute_build(incremental=True)

    assert outcome["kind"] == "error"
    assert outcome["error"] == "index_subprocess:identity_slot_owned"
    assert outcome["slot_contention"] is True
    assert actions == ["terminate", "drain"]
    assert daemon._build_process is None


def test_posix_verified_owner_is_still_standby(tmp_path, monkeypatch):
    root = _init_repo(tmp_path, "posix_standby")
    daemon = SourceGraphDaemon(root)
    retained = {"repo_root": source_graph_daemon._registry_key(root), "state": "running"}
    actions = _drive_lost_race(monkeypatch, daemon, windows=False, retained=retained)
    monkeypatch.setattr(source_graph_daemon, "_identity_matches", lambda _retained: True)

    assert daemon._execute_build(incremental=True) == {"kind": "standby"}
    assert actions == ["terminate", "drain"]

    assert daemon._run_one_build() is True
    health = daemon.health()
    assert health["status"] == source_graph_daemon.STATUS_STANDBY
    assert health["last_error"] == ""
    assert health["last_slot_contention_at"] == ""


def test_lost_race_marker_is_structured_not_text(tmp_path, monkeypatch):
    root = _init_repo(tmp_path, "structured_marker")
    daemon = SourceGraphDaemon(root)
    monkeypatch.setattr(daemon, "_has_prior_build", lambda: True)
    _fail_with_real_error(daemon)
    assert REAL_ERROR in daemon.health()["last_error"]

    daemon._execute_build = lambda *, incremental: {
        "kind": "error",
        "error": "index_subprocess:identity_slot_owned",
    }
    assert daemon._run_one_build() is True

    health = daemon.health()
    assert health["last_error"] == "RuntimeError:index_subprocess:identity_slot_owned"
    assert health["last_slot_contention_at"] == ""


def test_fence_sentinel_is_not_masked_by_lost_race(tmp_path, monkeypatch):
    root = _init_repo(tmp_path, "fence_sentinel")
    daemon = SourceGraphDaemon(root)
    _drive_lost_race(monkeypatch, daemon, windows=True, retained=None)

    monkeypatch.setattr(
        source_graph_daemon, "_read_build_identity", lambda _root: {"state": "stopping"}
    )
    assert daemon._run_one_build() is True
    assert daemon.health()["last_error"] == "build_start_fenced"

    monkeypatch.setattr(source_graph_daemon, "_read_build_identity", lambda _root: None)
    assert daemon._run_one_build() is True

    health = daemon.health()
    assert "identity_slot_owned" in health["last_error"]
    assert "build_start_fenced" not in health["last_error"]
    assert health["last_slot_contention_at"] != ""


def test_unregistered_daemon_health_carries_slot_contention_key(tmp_path):
    root = _init_repo(tmp_path, "unregistered_health")

    health = source_graph_daemon.daemon_health(root)

    assert health["registered"] is False
    assert health["last_slot_contention_at"] == ""
