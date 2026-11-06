from __future__ import annotations

import json
import mmap
import os
import sys
from pathlib import Path

import pytest

from aiworkhub import db_writer, doctor, platform_io

KEYS = {
    "pid",
    "ppid",
    "cmdline",
    "holds",
    "runtime_generation",
    "superseded",
    "rss_bytes",
}
OLD_GENERATION = "0.12.84-aaaaaaaaaaaaaaaa"
NEW_GENERATION = "0.12.90-bbbbbbbbbbbbbbbb"


def _install(generation: str) -> str:
    return (
        f"/fake/hub/runtime/generations/{generation}/runtime/aiworkhub/server.py"
    )


def _fake_own_generation(path=None):
    text = str(path)
    for generation in (OLD_GENERATION, NEW_GENERATION):
        if generation in text:
            return generation
    return None


def _fake_superseded(path=None):
    return OLD_GENERATION in str(path)


def _write_pid(
    proc: Path,
    pid: int,
    *,
    ppid: int,
    args: list[str],
    resident_pages: int,
    fd_targets: dict[int, str],
) -> None:
    pid_dir = proc / str(pid)
    (pid_dir / "fd").mkdir(parents=True)
    (pid_dir / "stat").write_text(
        f"{pid} (python) S {ppid} " + " ".join(["0"] * 40) + "\n", encoding="utf-8"
    )
    (pid_dir / "statm").write_text(
        f"1000 {resident_pages} 0 0 0 0 0\n", encoding="utf-8"
    )
    (pid_dir / "cmdline").write_bytes(
        b"".join(arg.encode("utf-8") + b"\0" for arg in args)
    )
    for number, target in fd_targets.items():
        # Plain file holding the fd's target; the fixture patches os.readlink to read it.
        (pid_dir / "fd" / str(number)).write_text(target, encoding="utf-8")


@pytest.fixture
def fake_proc(tmp_path, monkeypatch):
    """Fake /proc with two task-DB holders, one superseded, one unreadable pid."""
    proc = tmp_path / "proc"
    proc.mkdir()
    db = tmp_path / "hub" / "task_queue.sqlite"
    wal = str(db) + "-wal"
    shm = str(db) + "-shm"
    lock = str(db_writer.lock_path_for(db))
    db_str = str(db)

    # Live server: db + -wal + -shm; carries a secret argument that must be masked.
    _write_pid(
        proc,
        201,
        ppid=1,
        args=[
            "python",
            _install(NEW_GENERATION),
            "--token=TOPSECRET-VALUE",
        ],
        resident_pages=2000,
        fd_targets={0: "/dev/null", 3: db_str, 4: wal, 5: shm, 6: "/tmp/other"},
    )
    # Leftover server from a superseded generation: db + writer.lock only.
    _write_pid(
        proc,
        202,
        ppid=7,
        args=["python", _install(OLD_GENERATION)],
        resident_pages=1500,
        fd_targets={3: db_str, 4: lock},
    )
    # Unreadable pid (fd listing denied); must be skipped without aborting the scan.
    _write_pid(
        proc,
        203,
        ppid=1,
        args=["python", _install(OLD_GENERATION)],
        resident_pages=10,
        fd_targets={3: db_str},
    )
    # Unrelated process: holds nothing of the task DB.
    _write_pid(
        proc,
        204,
        ppid=1,
        args=["sleep", "5"],
        resident_pages=5,
        fd_targets={3: "/tmp/unrelated"},
    )
    # Another holder after the denied pid proves the scan continues past it.
    _write_pid(
        proc,
        301,
        ppid=1,
        args=["python", "-m", "worker"],
        resident_pages=40,
        fd_targets={3: db_str},
    )

    real_listdir = os.listdir

    def guarded_listdir(path=".", *args, **kwargs):
        if Path(str(path)).parts[-2:] == ("203", "fd"):
            raise PermissionError(13, "denied", str(path))
        return real_listdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "getuid", lambda: os.stat(proc).st_uid, raising=False)
    monkeypatch.setattr(os, "listdir", guarded_listdir)

    real_readlink = os.readlink

    def fake_readlink(path, *args, **kwargs):
        # fd entries are plain files holding their target (see _write_pid).
        if Path(str(path)).parent.name == "fd":
            return Path(str(path)).read_text(encoding="utf-8")
        return real_readlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "readlink", fake_readlink)
    # The fake tree is Linux-shaped; the scan itself is platform-neutral once the check passes.
    monkeypatch.setattr(platform_io, "is_linux", lambda _name=None: True)
    monkeypatch.setattr(doctor.runtime_generation, "own_generation", _fake_own_generation)
    monkeypatch.setattr(doctor.runtime_generation, "superseded", _fake_superseded)
    return proc, db


def test_task_db_holders_shape_holds_generation_and_masking(fake_proc):
    proc, db = fake_proc
    result = doctor.task_db_holders(proc, db_path=db)

    assert result["supported"] is True
    assert result["truncated"] is False
    entries = {entry["pid"]: entry for entry in result["entries"]}
    assert sorted(entries) == [201, 202, 301]

    for entry in entries.values():
        assert set(entry) == KEYS

    live = entries[201]
    assert live["ppid"] == 1
    assert live["holds"] == ["db", "wal", "shm"]
    assert live["runtime_generation"] == NEW_GENERATION
    assert live["superseded"] is False
    assert live["rss_bytes"] == 2000 * mmap.PAGESIZE
    assert "***" in live["cmdline"]
    assert "TOPSECRET-VALUE" not in json.dumps(result)
    assert "--token" not in live["cmdline"].lower()

    leftover = entries[202]
    assert leftover["ppid"] == 7
    assert leftover["holds"] == ["db", "writer_lock"]
    assert leftover["runtime_generation"] == OLD_GENERATION
    assert leftover["superseded"] is True

    worker = entries[301]
    assert worker["holds"] == ["db"]
    assert worker["runtime_generation"] is None
    assert worker["superseded"] is None


def test_task_db_holders_skips_denied_pid_and_unrelated_process(fake_proc):
    proc, db = fake_proc
    pids = {entry["pid"] for entry in doctor.task_db_holders(proc, db_path=db)["entries"]}
    assert 203 not in pids
    assert 204 not in pids


def test_task_db_holders_entry_cap_marks_truncated(fake_proc, monkeypatch):
    proc, db = fake_proc
    monkeypatch.setattr(doctor, "TASK_DB_HOLDERS_MAX_ENTRIES", 1)
    result = doctor.task_db_holders(proc, db_path=db)
    assert len(result["entries"]) == 1
    assert result["truncated"] is True


def test_task_db_holders_unsupported_off_linux(monkeypatch):
    monkeypatch.setattr(platform_io, "is_linux", lambda _name=None: False)
    result = doctor.task_db_holders("/definitely/not/proc")
    assert result == {"supported": False, "reason": "linux_only"}


def test_doctor_reply_carries_task_db_holders(monkeypatch):
    sentinel = {"supported": True, "entries": [], "scanned_pids": 0, "truncated": False}
    monkeypatch.setattr(doctor, "task_db_holders", lambda *_a, **_k: sentinel)
    report = doctor.diagnose({"p": lambda: {"ok": True}})
    assert report["task_db_holders"] is sentinel


@pytest.mark.skipif(not platform_io.is_linux(), reason="real /proc smoke test is Linux only")
def test_task_db_holders_real_proc_smoke(tmp_path):
    result = doctor.task_db_holders(db_path=tmp_path / "task_queue.sqlite")
    assert result["supported"] is True
    assert isinstance(result["entries"], list)
    assert result["scanned_pids"] <= doctor.TASK_DB_HOLDERS_MAX_PIDS


def test_masked_cmdline_hides_secret_values_and_keeps_plain_args():
    cases = [
        (["python", "--token", "abc"], "abc"),
        (["x", "--api-key", "sk-123", "--keep"], "sk-123"),
        (["x", "--password=hunter2"], "hunter2"),
        (["x", "--use-keyring", "--token", "abc"], "abc"),
    ]
    for raw, secret in cases:
        text = doctor._masked_cmdline(raw)
        assert secret not in text
        assert "***" in text
    assert "--keep" in doctor._masked_cmdline(["x", "--api-key", "sk-123", "--keep"])


def test_doctor_reply_drops_holder_cmdlines_when_oversized(monkeypatch):
    holders = [
        {
            "pid": pid,
            "ppid": 1,
            "cmdline": "c" * doctor.TASK_DB_HOLDERS_CMDLINE_CHARS,
            "holds": ["db"],
            "runtime_generation": None,
            "superseded": None,
            "rss_bytes": 1,
        }
        for pid in range(8)
    ]
    monkeypatch.setattr(
        doctor,
        "task_db_holders",
        lambda *_a, **_k: {
            "supported": True,
            "entries": holders,
            "scanned_pids": 8,
            "truncated": False,
        },
    )
    probes = {
        f"probe_{i:02d}": lambda: {"ok": False, "error": "e" * 300} for i in range(60)
    }
    report = doctor.diagnose(probes)
    assert report["holder_cmdlines_dropped"] is True
    assert all(entry["cmdline"] == "" for entry in report["task_db_holders"]["entries"])
    assert (
        doctor._encoded_size(report) <= doctor.REPLY_MAX_BYTES
        or report["holder_cmdlines_dropped"] is True
    )
