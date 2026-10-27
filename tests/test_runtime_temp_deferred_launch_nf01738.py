"""NF-2026-01738: a deferred launch's temp dir must survive dead-owner GC.

A deferred launch provisions ``.aiworkhub/temp/<ns>/<request_id>`` from a
process that exits before the supervisor writes ``<request_id>.supervisor.json``
(or while that status still records pid 0).  Reclaiming the dir then breaks the
live worker: Landlock binds its rule to the inode, so a recreated dir is not
the granted one.  The dir must stay while the supervisor is pending or names no
process, and must be reclaimed once the recorded supervisor/child are dead.
Ages are injected through ``storage_retention._janitor_mtime``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import runtime_temp, storage_retention, terminal_log_retention

_HOUR = 3600.0
_NOW = 2_000_000_000.0
_REQUEST_ID = "req-nf01738"
_SRC_ROOT = Path(runtime_temp.__file__).resolve().parents[1]
_PROVISION_CODE = (
    "import sys\n"
    "from pathlib import Path\n"
    "from aiworkhub import runtime_temp\n"
    "runtime_temp.provision_request_temp(\n"
    "    Path(sys.argv[1]), sys.argv[2], namespace=sys.argv[3]\n"
    ")\n"
)


@pytest.fixture
def repo(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.delenv(runtime_temp._PROCESS_DIR_ENV, raising=False)
    monkeypatch.delenv(runtime_temp.TEMP_ROOT_ENV, raising=False)
    monkeypatch.setattr(storage_retention, "_janitor_mtime", lambda path: _NOW - 49 * _HOUR)
    return tmp_path.resolve()


def _provision_from_exiting_process(repo: Path) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(_SRC_ROOT), env.get("PYTHONPATH", "")) if part
    )
    for namespace in (runtime_temp.WORKER_NAMESPACE, runtime_temp.VALIDATION_NAMESPACE):
        subprocess.run(
            [sys.executable, "-c", _PROVISION_CODE, str(repo), _REQUEST_ID, namespace],
            check=True,
            env=env,
        )


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _process_dir(repo: Path) -> Path:
    return repo / runtime_temp._PROCESS_DIR_DEFAULT_REL


def _write_spec(repo: Path) -> None:
    path = _process_dir(repo) / f"{_REQUEST_ID}{runtime_temp._SUPERVISOR_SPEC_SUFFIX}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"argv": ["worker"]}))


def _write_supervisor(repo: Path, payload: dict) -> None:
    path = _process_dir(repo) / f"{_REQUEST_ID}.supervisor.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _dir(repo: Path, namespace: str) -> Path:
    return repo / ".aiworkhub" / "temp" / namespace / _REQUEST_ID


def _owner_pid(repo: Path) -> int:
    owner = _dir(repo, runtime_temp.WORKER_NAMESPACE) / runtime_temp.OWNER_MANIFEST_NAME
    return int(json.loads(owner.read_text())["pid"])


def _identified(repo: Path) -> list[Path]:
    return [row["path"] for row in runtime_temp.identify_dead_owner_dirs(repo)]


def _assert_kept(repo: Path) -> None:
    assert _dir(repo, runtime_temp.WORKER_NAMESPACE) not in _identified(repo)
    count, _released, failures = terminal_log_retention._dead_owner_temp_gc(repo)
    assert failures == []
    assert count == 0
    assert _dir(repo, runtime_temp.WORKER_NAMESPACE).is_dir()
    assert _dir(repo, runtime_temp.VALIDATION_NAMESPACE).is_dir()


def _assert_reclaimed(repo: Path) -> None:
    assert _dir(repo, runtime_temp.WORKER_NAMESPACE) in _identified(repo)
    count, _released, failures = terminal_log_retention._dead_owner_temp_gc(repo)
    assert failures == []
    assert count == 2
    assert not _dir(repo, runtime_temp.WORKER_NAMESPACE).exists()
    assert not _dir(repo, runtime_temp.VALIDATION_NAMESPACE).exists()


def test_provisioning_owner_is_dead_in_every_case(repo: Path) -> None:
    _provision_from_exiting_process(repo)

    assert runtime_temp._pid_alive(_owner_pid(repo)) is False


def test_deferred_launch_without_supervisor_json_survives_sweep(repo: Path) -> None:
    _provision_from_exiting_process(repo)
    _write_spec(repo)

    _assert_kept(repo)
    _assert_kept(repo)


@pytest.mark.parametrize(
    "payload",
    [
        {"supervisor_pid": 0, "child_pid": 0},
        {"supervisor_pid": 0, "supervisor_pid_start_ticks": 0},
        {"state": "starting", "child_pid": None},
        {},
    ],
    ids=["both_zero", "supervisor_zero", "no_pids_starting", "empty_status"],
)
def test_supervisor_json_naming_no_process_survives_sweep(
    repo: Path, payload: dict
) -> None:
    _provision_from_exiting_process(repo)
    _write_spec(repo)
    _write_supervisor(repo, payload)

    _assert_kept(repo)


def test_live_supervisor_holds_dir_after_provisioner_exits(repo: Path) -> None:
    _provision_from_exiting_process(repo)
    _write_spec(repo)
    sleeper = _sleeper()
    try:
        _write_supervisor(
            repo,
            {
                "supervisor_pid": sleeper.pid,
                "supervisor_pid_start_ticks": runtime_temp.process_start_ticks(sleeper.pid),
                "child_pid": 0,
            },
        )
        _assert_kept(repo)
    finally:
        _stop(sleeper)


def test_live_child_holds_dir_after_supervisor_and_provisioner_exit(repo: Path) -> None:
    _provision_from_exiting_process(repo)
    sleeper = _sleeper()
    try:
        _write_supervisor(
            repo,
            {
                "supervisor_pid": _dead_pid(),
                "supervisor_pid_start_ticks": 1,
                "child_pid": sleeper.pid,
                "child_pid_start_ticks": runtime_temp.process_start_ticks(sleeper.pid),
            },
        )
        _assert_kept(repo)
    finally:
        _stop(sleeper)


def test_deferred_then_running_then_terminal_lifecycle(repo: Path) -> None:
    _provision_from_exiting_process(repo)
    _write_spec(repo)
    _assert_kept(repo)

    _write_supervisor(repo, {"state": "starting", "supervisor_pid": 0, "child_pid": 0})
    _assert_kept(repo)

    supervisor = _sleeper()
    ticks = runtime_temp.process_start_ticks(supervisor.pid)
    try:
        _write_supervisor(
            repo,
            {
                "state": "running",
                "supervisor_pid": supervisor.pid,
                "supervisor_pid_start_ticks": ticks,
                "child_pid": 0,
            },
        )
        _assert_kept(repo)
    finally:
        _stop(supervisor)

    _write_supervisor(
        repo,
        {
            "state": "exited",
            "exit_code": 0,
            "supervisor_pid": supervisor.pid,
            "supervisor_pid_start_ticks": ticks,
            "child_pid": _dead_pid(),
            "child_pid_start_ticks": 1,
        },
    )
    _assert_reclaimed(repo)


def test_terminal_request_without_spec_or_status_is_reclaimed(repo: Path) -> None:
    _provision_from_exiting_process(repo)

    _assert_reclaimed(repo)


def test_spec_suffix_matches_launch_isolated_writer() -> None:
    from aiworkhub import process_launcher_launch_isolated

    source = Path(process_launcher_launch_isolated.__file__).read_text(encoding="utf-8")

    assert (
        f'self.process_dir / f"{{request_id}}{runtime_temp._SUPERVISOR_SPEC_SUFFIX}"'
        in source
    )
