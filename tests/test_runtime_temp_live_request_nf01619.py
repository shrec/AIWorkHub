"""NF-2026-01619: a live request's temp dir must survive dead-owner GC.

An auth-retry relaunch re-provisions the request dir from a short-lived
finalizing process.  Re-provisioning must not re-stamp a live owner, and both
GC paths (``identify_dead_owner_dirs`` and the janitor's scratch step) must
treat a live supervisor or child recorded in ``<request_id>.supervisor.json``
as a live request.  Ages are injected through ``storage_retention._janitor_mtime``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from aiworkhub import runtime_temp, storage_retention

_HOUR = 3600.0
_NOW = 2_000_000_000.0
_REQUEST_ID = "req-nf01619"
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


def _supervisor_path(repo: Path) -> Path:
    return repo / runtime_temp._PROCESS_DIR_DEFAULT_REL / f"{_REQUEST_ID}.supervisor.json"


def _write_supervisor(repo: Path, payload: object) -> None:
    path = _supervisor_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))


def _dir(repo: Path, namespace: str) -> Path:
    return repo / ".aiworkhub" / "temp" / namespace / _REQUEST_ID


def _identified(repo: Path) -> list[Path]:
    return [row["path"] for row in runtime_temp.identify_dead_owner_dirs(repo)]


def _sweep(repo: Path) -> dict:
    return storage_retention._janitor_scratch(repo, _NOW)


def _assert_kept(repo: Path) -> None:
    assert _dir(repo, runtime_temp.WORKER_NAMESPACE) not in _identified(repo)
    result = _sweep(repo)
    assert result["errors"] == []
    assert result["removed"] == 0
    assert _dir(repo, runtime_temp.VALIDATION_NAMESPACE).is_dir()
    assert _dir(repo, runtime_temp.WORKER_NAMESPACE).is_dir()


def test_t1_live_supervisor_keeps_dir_with_dead_owner(repo: Path) -> None:
    _provision_from_exiting_process(repo)
    sleeper = _sleeper()
    try:
        _write_supervisor(
            repo,
            {
                "supervisor_pid": sleeper.pid,
                "supervisor_pid_start_ticks": runtime_temp.process_start_ticks(sleeper.pid),
            },
        )
        _assert_kept(repo)
    finally:
        _stop(sleeper)


def test_t1_live_child_alone_keeps_dir(repo: Path) -> None:
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


def test_t2_reprovision_after_relaunch_keeps_dir_over_two_sweeps(repo: Path) -> None:
    _provision_from_exiting_process(repo)
    sleeper = _sleeper()
    try:
        _write_supervisor(
            repo,
            {
                "supervisor_pid": sleeper.pid,
                "supervisor_pid_start_ticks": runtime_temp.process_start_ticks(sleeper.pid),
            },
        )
        _provision_from_exiting_process(repo)
        _assert_kept(repo)
        _assert_kept(repo)
    finally:
        _stop(sleeper)


def test_t3_dead_owner_supervisor_and_child_is_reclaimed(repo: Path) -> None:
    _provision_from_exiting_process(repo)
    _write_supervisor(
        repo,
        {
            "supervisor_pid": _dead_pid(),
            "supervisor_pid_start_ticks": 1,
            "child_pid": _dead_pid(),
            "child_pid_start_ticks": 1,
        },
    )
    worker_dir = _dir(repo, runtime_temp.WORKER_NAMESPACE)

    assert worker_dir in _identified(repo)
    result = _sweep(repo)
    assert result["errors"] == []
    assert result["removed"] == 1
    assert not _dir(repo, runtime_temp.VALIDATION_NAMESPACE).exists()


def test_t3_missing_supervisor_json_gives_no_protection(repo: Path) -> None:
    _provision_from_exiting_process(repo)

    assert _dir(repo, runtime_temp.WORKER_NAMESPACE) in _identified(repo)


def test_recycled_supervisor_pid_with_other_start_ticks_is_reclaimed(repo: Path) -> None:
    _provision_from_exiting_process(repo)
    sleeper = _sleeper()
    try:
        ticks = runtime_temp.process_start_ticks(sleeper.pid)
        if ticks is None:
            pytest.skip("host cannot supply process start ticks")
        _write_supervisor(
            repo, {"supervisor_pid": sleeper.pid, "supervisor_pid_start_ticks": ticks + 1}
        )
        assert _dir(repo, runtime_temp.WORKER_NAMESPACE) in _identified(repo)
    finally:
        _stop(sleeper)


def _owner_manifest_bytes(repo: Path, pid: object, starttime: object) -> bytes:
    return json.dumps(
        {
            "schema_id": runtime_temp.OWNER_SCHEMA_ID,
            "request_id": _REQUEST_ID,
            "repo_id": runtime_temp._repo_fingerprint(repo),
            "pid": pid,
            "starttime": starttime,
            "created_at": "2026-01-01T00:00:00+00:00",
            "namespace": runtime_temp.WORKER_NAMESPACE,
        }
    ).encode("utf-8")


def test_t4_reprovision_with_live_owner_leaves_owner_json_byte_identical(repo: Path) -> None:
    sleeper = _sleeper()
    try:
        ticks = runtime_temp.process_start_ticks(sleeper.pid)
        if ticks is None:
            pytest.skip("host cannot supply process start ticks")
        first = runtime_temp.provision_request_temp(repo, _REQUEST_ID)
        owner_path = first.root / runtime_temp.OWNER_MANIFEST_NAME
        owner_path.write_bytes(_owner_manifest_bytes(repo, sleeper.pid, ticks))
        before = owner_path.read_bytes()

        second = runtime_temp.provision_request_temp(repo, _REQUEST_ID)

        assert second.root == first.root
        assert owner_path.read_bytes() == before
        assert json.loads(before)["pid"] == sleeper.pid
    finally:
        _stop(sleeper)


def test_process_dir_constants_match_process_launcher() -> None:
    from aiworkhub import process_launcher

    assert runtime_temp._PROCESS_DIR_ENV == process_launcher.PROCESS_DIR_ENV
    assert runtime_temp._PROCESS_DIR_DEFAULT_REL == process_launcher.PROCESS_DIR_DEFAULT_REL


@pytest.mark.parametrize(
    "payload",
    [
        b'{"supervisor_pid": Infinity}',
        b'{"supervisor_pid": 1, "supervisor_pid_start_ticks": 1e999}',
        b"[" * 100000,
    ],
    ids=["infinity_pid", "overflow_ticks", "deep_nesting"],
)
def test_non_finite_or_deeply_nested_supervisor_json_does_not_raise(
    repo: Path, payload: bytes
) -> None:
    _provision_from_exiting_process(repo)
    path = _supervisor_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)

    identified = _identified(repo)

    if payload != b'{"supervisor_pid": 1, "supervisor_pid_start_ticks": 1e999}':
        assert _dir(repo, runtime_temp.WORKER_NAMESPACE) not in identified


def test_non_finite_owner_pid_does_not_break_provisioning(repo: Path) -> None:
    first = runtime_temp.provision_request_temp(repo, _REQUEST_ID)
    owner_path = first.root / runtime_temp.OWNER_MANIFEST_NAME
    owner_path.write_bytes(
        _owner_manifest_bytes(repo, 0, 0).replace(b'"pid": 0', b'"pid": 1e999')
    )

    second = runtime_temp.provision_request_temp(repo, _REQUEST_ID)

    assert second.root == first.root


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO")
def test_fifo_supervisor_json_does_not_block_and_keeps_dir(repo: Path) -> None:
    _provision_from_exiting_process(repo)
    path = _supervisor_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)
    outcome: dict[str, list[Path]] = {}
    worker = threading.Thread(
        target=lambda: outcome.setdefault("identified", _identified(repo)), daemon=True
    )

    worker.start()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert _dir(repo, runtime_temp.WORKER_NAMESPACE) not in outcome["identified"]


def test_reprovision_with_dead_owner_restamps_owner_json(repo: Path) -> None:
    _provision_from_exiting_process(repo)
    owner_path = _dir(repo, runtime_temp.WORKER_NAMESPACE) / runtime_temp.OWNER_MANIFEST_NAME
    dead_pid = json.loads(owner_path.read_text())["pid"]

    runtime_temp.provision_request_temp(repo, _REQUEST_ID)

    assert json.loads(owner_path.read_text())["pid"] == os.getpid() != dead_pid


@pytest.mark.parametrize("payload", ["{not json", "[]", '"text"', ""])
def test_t5_corrupt_supervisor_json_keeps_dir(repo: Path, payload: str) -> None:
    _provision_from_exiting_process(repo)
    _write_supervisor(repo, payload)

    _assert_kept(repo)


def test_t5_unreadable_supervisor_json_keeps_dir(repo: Path) -> None:
    _provision_from_exiting_process(repo)
    # A directory in place of the file makes the read fail with an OSError.
    _supervisor_path(repo).mkdir(parents=True)

    _assert_kept(repo)


@pytest.mark.parametrize(
    "body",
    [b'"pid": ' + b"1" * 4400, b'"pad": ' + b"[" * 16000],
    ids=["int_over_digit_limit", "deep_nesting"],
)
def test_unparseable_owner_json_is_restamped_and_gc_does_not_raise(
    repo: Path, body: bytes
) -> None:
    first = runtime_temp.provision_request_temp(repo, _REQUEST_ID)
    owner_path = first.root / runtime_temp.OWNER_MANIFEST_NAME
    corrupt = b'{"schema_id": "' + runtime_temp.OWNER_SCHEMA_ID.encode() + b'", ' + body + b"}"
    owner_path.write_bytes(corrupt)

    second = runtime_temp.provision_request_temp(repo, _REQUEST_ID)

    assert second.root == first.root
    assert json.loads(owner_path.read_text())["pid"] == os.getpid()
    owner_path.write_bytes(corrupt)
    _identified(repo)
    assert storage_retention._janitor_scratch(repo, _NOW)["ok"] is True
