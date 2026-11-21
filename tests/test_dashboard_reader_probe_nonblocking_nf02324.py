"""NF-2026-02324: the reader's ``opencode models`` refresh is a polled child.

The dashboard reader is single-threaded (NF-2026-02287): its fence turns
repo_policy's refresh thread into a job drained inline after each reply. The
``opencode models`` probe used to run there with a blocking ``subprocess.run``
(measured 20.79 s per refresh), stalling every request meanwhile. It now
starts a child process and later drains collect it with ``poll()``.

Every test drives a fake ``opencode`` created in ``tmp_path`` (a tiny Python
script behind a ``.cmd`` shim on Windows, a shebang script on POSIX); the real
provider CLI is never run.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import dashboard_reader, opencode_auth, platform_io, repo_policy, workforce_catalog

MODEL_OUTPUT = "openai/gpt-4o\nanthropic/claude-sonnet-4\n"
OWNER_MODEL = "custom-nf02324/owner-model"
LAST_GOOD_MODEL = "openai/gpt-4o-mini"
PUBLISH_WAIT_SECONDS = 60.0


@pytest.fixture
def reader_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A known repository with the reader's single-thread fence installed.

    Every attribute the fence replaces is registered with ``monkeypatch``
    first, so the fence is undone after the test.
    """
    from aiworkhub import dashboard

    repo = tmp_path / "repo"
    (repo / ".aiworkhub").mkdir(parents=True)
    (repo / ".aiworkhub" / "project.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.setenv("AIWORKHUB_REPO", str(repo))
    for module_name in dashboard_reader.FENCED_THREAD_MODULES:
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "threading", module.threading)
    monkeypatch.setattr(dashboard, "_snapshot_read_worker_limit", dashboard._snapshot_read_worker_limit)
    monkeypatch.setattr(repo_policy, "_POLLED_PROBE_THREAD_CLASS", None)
    monkeypatch.setattr(repo_policy, "_OPENCODE_LAST_GOOD_MODELS", {})
    monkeypatch.setattr(repo_policy, "_OPENCODE_LAST_PROBE", {})
    monkeypatch.setattr(opencode_auth, "owner_custom_provider_models", lambda: [OWNER_MODEL])
    repo_policy.reset_provider_probe_caches()
    dashboard_reader.install_single_thread_fence()
    try:
        yield repo
    finally:
        dashboard_reader.stop_polled_work()
        repo_policy.reset_provider_probe_caches()


def _fake_opencode(
    tmp_path: Path, name: str, *, sleep: float, output: str, exit_code: int = 0
) -> tuple[str, Path]:
    """Create a fake ``opencode``; return (executable, file of child pids).

    POSIX: a shebang script. Windows: a console-script launcher ``.exe`` (the
    one pip installs scripts with: launcher stub + shebang + zipped
    ``__main__.py``), so no ``cmd.exe`` shim is involved and the launcher ->
    interpreter pair is a real process tree; a ``.cmd`` shim only when the
    launcher stub is unavailable.
    """
    folder = tmp_path / f"fake-{name}"
    folder.mkdir()
    pid_file = folder / "pids.txt"
    source = (
        "import os, sys, time\n"
        f"with open({str(pid_file)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(f'{os.getpid()}\\n')\n"
        f"time.sleep({sleep!r})\n"
        f"sys.stdout.write({output!r})\n"
        "sys.stdout.flush()\n"
        f"sys.exit({exit_code!r})\n"
    )
    if os.name != "nt":
        launcher = folder / "opencode"
        launcher.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
        os.chmod(launcher, 0o755)
        return str(launcher), pid_file
    stub = _windows_launcher_stub()
    if stub is not None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("__main__.py", source)
        launcher = folder / "opencode.exe"
        launcher.write_bytes(stub + b'#!"' + os.fsencode(sys.executable) + b'"\r\n' + archive.getvalue())
        return str(launcher), pid_file
    script = folder / "fake_opencode.py"
    script.write_text(source, encoding="utf-8")
    launcher = folder / "opencode.cmd"
    launcher.write_bytes(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n'.encode("utf-8"))
    return str(launcher), pid_file


def _windows_launcher_stub() -> bytes | None:
    try:
        from pip._vendor import distlib
    except ImportError:
        return None
    stub = Path(distlib.__file__).parent / "t64.exe"
    return stub.read_bytes() if stub.is_file() else None


def _lookup(executable: str) -> dict[str, Any]:
    """What a dashboard snapshot reads: the cached listing, never blocking."""
    with repo_policy.nonblocking_provider_probes():
        listed, record = repo_policy._cached_opencode_models(executable)
    return {"listed": listed, "record": record}


def _probing(result: dict[str, Any]) -> bool:
    return (result["record"] or {}).get("state") == repo_policy.OPENCODE_PROBE_STATE_PROBING


def _drain_until_published(executable: str) -> dict[str, Any]:
    deadline = time.monotonic() + PUBLISH_WAIT_SECONDS
    while time.monotonic() < deadline:
        dashboard_reader.drain_deferred_work()
        result = _lookup(executable)
        if not _probing(result):
            return result
        time.sleep(0.05)
    raise AssertionError("the polled opencode probe never published a result")


def _polling_jobs() -> list[Any]:
    return [job for job in dashboard_reader._DEFERRED if job.is_polling()]


def _runtime_files(repo: Path) -> list[Path]:
    root = repo / ".aiworkhub" / "runtime"
    return sorted(path for path in root.rglob("*") if path.is_file()) if root.exists() else []


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        return bool(platform_io.windows_pid_is_alive(pid))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _assert_pids_gone(pid_file: Path) -> None:
    pids = [int(line) for line in pid_file.read_text(encoding="utf-8").split()] if pid_file.exists() else []
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and any(_pid_alive(pid) for pid in pids):
        time.sleep(0.05)
    assert not [pid for pid in pids if _pid_alive(pid)], pids


class _TimedStream(io.RawIOBase):
    """A reply stream that records when each reply line was written."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[tuple[float, dict[str, Any]]] = []

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        for raw in bytes(data).splitlines():
            if raw.strip():
                self.lines.append((time.monotonic(), json.loads(raw)))
        return len(data)


def test_serve_answers_the_next_request_while_the_probe_child_runs(
    reader_repo: Path, tmp_path: Path
) -> None:
    sleep = 6.0
    executable, pid_file = _fake_opencode(tmp_path, "slow", sleep=sleep, output=MODEL_OUTPUT)
    tools = {"probe": lambda: _lookup(executable)}
    requests = b"".join(
        json.dumps({"id": index, "tool": "probe"}).encode("utf-8") + b"\n" for index in (1, 2)
    )
    stdout = _TimedStream()
    threads_before = threading.active_count()
    started = time.monotonic()
    assert dashboard_reader.serve(io.BytesIO(requests), stdout, tools) == 0  # type: ignore[arg-type]
    assert threading.active_count() == threads_before
    assert [reply["id"] for _, reply in stdout.lines] == [1, 2]
    (_, first), (second_at, second) = stdout.lines
    # The drain after reply 1 started the child; reply 2 came while it ran,
    # served from the cache's existing (probing) value, never a guess.
    assert second_at - started < sleep / 3, second_at - started
    assert first["result"] == {"listed": [], "record": {"state": "probing"}}
    assert second["result"] == {"listed": [], "record": {"state": "probing"}}
    # serve() ending killed the unfinished child and removed its files.
    assert _polling_jobs() == []
    _assert_pids_gone(pid_file)
    assert _runtime_files(reader_repo) == []


def test_drain_starts_without_waiting_and_a_later_drain_publishes_rows(
    reader_repo: Path, tmp_path: Path
) -> None:
    sleep = 3.0
    executable, pid_file = _fake_opencode(tmp_path, "rows", sleep=sleep, output=MODEL_OUTPUT)
    expected_rows = workforce_catalog.parse_opencode_models_output(MODEL_OUTPUT.encode("utf-8"))
    assert expected_rows, "the fake output must parse into model rows"

    assert _probing(_lookup(executable))
    began = time.monotonic()
    dashboard_reader.drain_deferred_work()
    assert time.monotonic() - began < sleep / 3
    assert len(_polling_jobs()) == 1
    probe_dir = reader_repo / ".aiworkhub" / "runtime" / "opencode_models_probe"
    assert {path.parent for path in _runtime_files(reader_repo)} == {probe_dir}
    assert _probing(_lookup(executable))

    published = _drain_until_published(executable)
    assert published["record"] == {"state": "ok", "count": len(expected_rows)}
    assert published["listed"] == expected_rows + [OWNER_MODEL]
    assert repo_policy._OPENCODE_LAST_GOOD_MODELS[executable] == expected_rows
    assert repo_policy._opencode_probe_record(executable) == published["record"]
    # The next snapshot is served from the cache without another probe.
    assert _lookup(executable) == published
    assert _polling_jobs() == []
    _assert_pids_gone(pid_file)
    assert _runtime_files(reader_repo) == []


def test_a_hanging_probe_is_killed_and_last_known_good_is_served(
    reader_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repo_policy, "_OPENCODE_PROBE_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(repo_policy, "_OPENCODE_PROBE_RETRY_TIMEOUT_SECONDS", 1)
    executable, pid_file = _fake_opencode(tmp_path, "hang", sleep=120.0, output=MODEL_OUTPUT)
    repo_policy._OPENCODE_LAST_GOOD_MODELS[executable] = [LAST_GOOD_MODEL]

    assert _probing(_lookup(executable))
    published = _drain_until_published(executable)
    assert published["record"] == {
        "state": "failed",
        "reason": "timeout",
        "served_last_known_good": True,
    }
    assert published["listed"] == [LAST_GOOD_MODEL, OWNER_MODEL]
    assert repo_policy._opencode_probe_record(executable) == published["record"]
    assert _lookup(executable) == published
    _assert_pids_gone(pid_file)
    # The first attempt and its one retry each ran (and were killed).
    assert len(pid_file.read_text(encoding="utf-8").split()) == 2
    assert _runtime_files(reader_repo) == []


@pytest.mark.parametrize(
    ("output", "exit_code", "expected_record"),
    [
        (MODEL_OUTPUT, 0, {"state": "ok", "count": 2}),
        ("", 3, {"state": "failed", "reason": "exit_3", "served_last_known_good": False}),
        ("", 0, {"state": "failed", "reason": "empty", "served_last_known_good": False}),
    ],
    ids=["rows", "exit_3", "empty_with_retry"],
)
def test_polled_result_equals_the_blocking_result(
    reader_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
    exit_code: int,
    expected_record: dict[str, Any],
) -> None:
    executable, pid_file = _fake_opencode(tmp_path, "same", sleep=0.0, output=output, exit_code=exit_code)
    blocking_listed, blocking_record = repo_policy._opencode_models_with_record(executable)
    assert blocking_record == expected_record
    blocking_last_good = dict(repo_policy._OPENCODE_LAST_GOOD_MODELS)
    blocking_runs = len(pid_file.read_text(encoding="utf-8").split())
    assert blocking_runs == (2 if expected_record.get("reason") == "empty" else 1)
    pid_file.unlink()
    monkeypatch.setattr(repo_policy, "_OPENCODE_LAST_GOOD_MODELS", {})
    monkeypatch.setattr(repo_policy, "_OPENCODE_LAST_PROBE", {})

    assert _probing(_lookup(executable))
    published = _drain_until_published(executable)
    assert published == {"listed": blocking_listed, "record": blocking_record}
    assert repo_policy._OPENCODE_LAST_GOOD_MODELS == blocking_last_good
    # The same one retry on empty, and no retry otherwise.
    assert len(pid_file.read_text(encoding="utf-8").split()) == blocking_runs
    assert _runtime_files(reader_repo) == []


def test_a_replaced_probe_keeps_the_blocking_path(
    reader_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, int]] = []

    def stub(executable: str, timeout: int) -> tuple[list[str], str | None]:
        calls.append((executable, timeout))
        return ["openai/gpt-4o"], None

    monkeypatch.setattr(repo_policy, "_opencode_models_probe_once", stub)
    executable, pid_file = _fake_opencode(tmp_path, "stubbed", sleep=0.0, output=MODEL_OUTPUT)
    assert _probing(_lookup(executable))
    published = _drain_until_published(executable)
    assert published["record"] == {"state": "ok", "count": 1}
    assert calls == [(executable, repo_policy._OPENCODE_PROBE_TIMEOUT_SECONDS)]
    assert not pid_file.exists(), "a replaced probe must never start the executable"
    assert _runtime_files(reader_repo) == []


def test_without_the_fence_the_refresh_is_never_polled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The threaded MCP-server path: the reader never enabled the polled probe.
    repo = tmp_path / "repo"
    (repo / ".aiworkhub").mkdir(parents=True)
    (repo / ".aiworkhub" / "project.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(repo))
    monkeypatch.setattr(repo_policy, "_POLLED_PROBE_THREAD_CLASS", None)
    assert repo_policy._polled_probe_runtime_dir() is None
    monkeypatch.setattr(repo_policy, "_POLLED_PROBE_THREAD_CLASS", dashboard_reader._DeferredThread)
    # Enabled, but this module's ``threading`` is not the reader fence.
    assert repo_policy.threading is threading
    assert repo_policy._polled_probe_runtime_dir() is None
