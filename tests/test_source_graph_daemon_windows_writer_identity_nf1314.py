"""Creation identity authorizes standby observation, never foreign cleanup."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from aiworkhub import platform_io, runtime_temp, source_graph_daemon
from aiworkhub.source_graph_daemon import SourceGraphDaemon
from test_source_graph_daemon_slot_contention_health_nf01175 import (
    _drive_lost_race,
    _init_repo,
)


def _writer(root):
    return {
        "repo_root": source_graph_daemon._registry_key(root),
        "state": "running",
        "identity_kind": "windows_creation",
        "pid": 7654,
        "pgid": 0,
        "session_id": 0,
        "start_ticks": 987654321,
        "owner_token": "private-writer-owner",
    }


def test_windows_exact_writer_contention_is_standby(tmp_path, monkeypatch):
    root = _init_repo(tmp_path, "verified_writer")
    daemon = SourceGraphDaemon(root)
    retained = _writer(root)
    actions = _drive_lost_race(monkeypatch, daemon, windows=True, retained=retained)
    monkeypatch.setattr(source_graph_daemon, "_proc_identity", lambda pid: {
        "pid": pid, "pgid": 0, "session_id": 0, "start_ticks": retained["start_ticks"],
    })
    assert daemon._execute_build(incremental=True) == {"kind": "standby"}
    assert actions == ["terminate", "drain"]  # Only the losing owning handle.
    assert daemon._build_process is None
    assert source_graph_daemon._leader_owner_matches(retained) is False


@pytest.mark.parametrize("changed", [
    {"start_ticks": 0}, {"start_ticks": 987654320}, {"start_ticks": None},
    {"start_ticks": "malformed"}, {"identity_kind": "owner_handle"},
    {"owner_token": ""}, {"owner_token": None}, {"owner_token": {}},
    {"repo_root": "another-repository"}, {"state": "stopping"},
    {"pgid": 1}, {"session_id": 1},
])
def test_windows_unverified_slot_stays_fenced(tmp_path, monkeypatch, changed):
    root = _init_repo(tmp_path, "unverified_writer")
    daemon = SourceGraphDaemon(root)
    retained = {**_writer(root), **changed}
    actions = _drive_lost_race(monkeypatch, daemon, windows=True, retained=retained)
    monkeypatch.setattr(source_graph_daemon, "_proc_identity", lambda pid: {
        "pid": pid, "pgid": 0, "session_id": 0, "start_ticks": 987654321,
    })
    result = daemon._execute_build(incremental=True)
    assert result["kind"] == "error"
    assert result["error"] in {"build_start_fenced", "index_subprocess:identity_slot_owned"}
    assert actions in ([], ["terminate", "drain"])


def test_windows_unknown_creation_identity_is_not_a_match(tmp_path, monkeypatch):
    root = _init_repo(tmp_path, "unknown_writer")
    daemon = SourceGraphDaemon(root)
    retained = _writer(root)
    _drive_lost_race(monkeypatch, daemon, windows=True, retained=retained)
    assert daemon._execute_build(incremental=True)["kind"] == "error"


def test_windows_publication_records_creation_kind(tmp_path, monkeypatch):
    root = _init_repo(tmp_path, "publication")
    daemon = SourceGraphDaemon(root)
    retained = _writer(root)
    _drive_lost_race(monkeypatch, daemon, windows=True, retained=retained)
    monkeypatch.setattr(source_graph_daemon, "_proc_identity", lambda pid: {
        "pid": pid, "pgid": 0, "session_id": 0, "start_ticks": 987654321,
    })
    published = []
    monkeypatch.setattr(source_graph_daemon, "_publish_build_identity_if_unowned",
                        lambda _root, identity: published.append(identity) or False)
    assert daemon._execute_build(incremental=True) == {"kind": "standby"}
    assert published[0]["identity_kind"] == "windows_creation"
    assert published[0]["start_ticks"] > 0


@pytest.mark.parametrize("stamp,alive", [(None, True), (0, True), (-1, True), (123, False)])
def test_platform_unknown_or_dead_windows_identity_fails_closed(monkeypatch, stamp, alive):
    monkeypatch.setattr(platform_io, "is_windows", lambda: True)
    monkeypatch.setattr(runtime_temp, "process_start_ticks", lambda _pid: stamp)
    monkeypatch.setattr(platform_io, "windows_pid_is_alive", lambda _pid: alive)
    assert platform_io.process_creation_identity(42) is None


def test_platform_posix_preserves_existing_kernel_identity(monkeypatch):
    identity = {"pid": 42, "pgid": 42, "session_id": 42, "start_ticks": 123}
    monkeypatch.setattr(platform_io, "is_windows", lambda: False)
    monkeypatch.setattr(platform_io, "linux_proc_identity", lambda _pid: identity)
    assert platform_io.process_creation_identity(42) is identity


@pytest.mark.skipif(os.name != "nt", reason="Real Windows GetProcessTimes integration")
def test_real_windows_owned_child_creation_and_reuse_guard():
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             **platform_io.background_process_launch_kwargs())
    try:
        identity = source_graph_daemon._proc_identity(child.pid)
        assert identity is not None
        assert identity["pid"] == child.pid
        assert identity["start_ticks"] == runtime_temp.process_start_ticks(child.pid) > 0
        retained = {**identity, "owner_token": "owned-test", "identity_kind": "windows_creation"}
        assert source_graph_daemon._identity_matches(retained)
        assert not source_graph_daemon._identity_matches({**retained, "start_ticks": 0})
        assert not source_graph_daemon._identity_matches({
            **retained, "start_ticks": retained["start_ticks"] + 1,
        })
        assert not source_graph_daemon._cross_instance_identity_supported()
        assert not source_graph_daemon._leader_owner_matches(retained)
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=10)
