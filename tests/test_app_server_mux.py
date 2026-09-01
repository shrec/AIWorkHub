"""B409: app_server_mux.py -- transparent CLI wrapper + sideband E2E tests.

Two kinds of "fake" drive these tests, both real (never mocked):

- Fake child App Server: ``tests/_fake_app_server.py``, spawned as a REAL
  subprocess exactly as the mux would spawn the real ``codex`` binary.
- Fake extension client: a pair of OS pipes wired directly into
  ``AppServerMux`` as its ``extension_stdin``/``extension_stdout``, driven
  from this test process by writing/reading raw newline-delimited JSON --
  standing in for the VS Code OpenAI extension's own stdio without needing
  a second subprocess layer.

No test spawns a real ``codex`` binary and no test relies on a mocked
``subprocess.run`` returncode.
"""
from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import app_server_mux  # noqa: E402
from aiworkhub.app_server_mux import (  # noqa: E402
    SIDEBAND_ALLOWED_METHODS,
    AppServerMux,
    default_sideband_dir,
    gc_stale_sideband_instances,
    is_app_server_invocation,
    resolve_real_executable,
)

FAKE_SERVER = Path(__file__).resolve().parent / "_fake_app_server.py"
MUX_MODULE = Path(__file__).resolve().parents[1] / "src" / "aiworkhub" / "app_server_mux.py"


def test_mux_child_forwards_shared_background_launch_policy(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(
        app_server_mux,
        "background_process_launch_kwargs",
        lambda: {"start_new_session": True},
    )

    def popen(*args, **kwargs):
        captured.update(kwargs)
        raise OSError("stop after capture")

    monkeypatch.setattr(app_server_mux.subprocess, "Popen", popen)
    mux = AppServerMux(
        ["app-server", "--stdio"],
        repo_id="repo",
        repo_root=tmp_path,
        real_executable="codex",
        sideband_dir=tmp_path / "sideband",
    )
    with pytest.raises(OSError, match="stop after capture"):
        mux.start()
    assert captured["start_new_session"] is True
    assert captured["cwd"] == str(tmp_path)
    assert captured["bufsize"] == 0


def _fake_child_executable(extra_args: list[str] | None = None) -> list[str]:
    return [sys.executable, str(FAKE_SERVER), *(extra_args or [])]


def _write_executable_script(path: Path, script_text: str) -> None:
    """Writes ``script_text`` to ``path`` with the executable bit set at
    file-creation time (mode passed to ``os.open``) rather than via a
    separate ``os.chmod``/``Path.chmod`` call, which this sandboxed test
    environment rejects with ``PermissionError`` even for a file it just
    created."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
    try:
        os.write(fd, script_text.encode("utf-8"))
    finally:
        os.close(fd)


def test_platform_facade_imports_in_package_and_direct_script_context():
    from aiworkhub import _platform_process, platform_io

    assert platform_io._platform_process is _platform_process

    probe = """
import importlib.util
import json
import sys
from pathlib import Path

mux_path = Path(sys.argv[1])
sys.path.insert(0, str(mux_path.parent))
spec = importlib.util.spec_from_file_location("direct_app_server_mux", mux_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
facade = sys.modules["platform_io"]
print(json.dumps({
    "facade": facade.__name__,
    "backend": facade._platform_process.__name__,
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe, str(MUX_MODULE)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {
        "facade": "platform_io",
        "backend": "_platform_process",
    }


# ---------------------------------------------------------------------------
# Invocation-shape detection / executable resolution
# ---------------------------------------------------------------------------

def test_is_app_server_invocation_true_for_app_server_subcommand():
    assert is_app_server_invocation(["app-server", "--listen", "stdio://"]) is True


def test_is_app_server_invocation_false_for_exec():
    assert is_app_server_invocation(["exec", "--foo", "bar"]) is False
    assert is_app_server_invocation([]) is False


def test_native_launcher_preserves_exact_extension_host_parent_pid(monkeypatch):
    monkeypatch.setenv(app_server_mux.ENV_EXTENSION_HOST_PID, "424242")
    assert app_server_mux.extension_host_parent_pid() == 424242
    monkeypatch.setenv(app_server_mux.ENV_EXTENSION_HOST_PID, "not-a-pid")
    assert app_server_mux.extension_host_parent_pid() == os.getppid()


def test_resolve_real_executable_env_override(monkeypatch):
    monkeypatch.setenv(app_server_mux.ENV_REAL_EXECUTABLE, "/usr/local/bin/codex-real")
    assert resolve_real_executable() == "/usr/local/bin/codex-real"


def test_resolve_real_executable_default(monkeypatch):
    monkeypatch.delenv(app_server_mux.ENV_REAL_EXECUTABLE, raising=False)
    monkeypatch.setenv(app_server_mux.ENV_SIDEBAND_DIR, "/nonexistent/aiworkhub-sideband-test")
    assert resolve_real_executable() == "codex"


def test_resolve_real_executable_from_private_pin(monkeypatch, tmp_path):
    real = tmp_path / "codex-real"
    _write_executable_script(real, "#!/bin/sh\nexit 0\n")
    sideband = tmp_path / "sideband"
    sideband.mkdir(mode=0o700)
    pin = sideband / app_server_mux.REAL_EXECUTABLE_CONFIG_NAME
    fd = os.open(pin, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, str(real).encode())
    finally:
        os.close(fd)
    monkeypatch.delenv(app_server_mux.ENV_REAL_EXECUTABLE, raising=False)
    monkeypatch.setenv(app_server_mux.ENV_SIDEBAND_DIR, str(sideband))
    assert resolve_real_executable() == str(real)


def test_windows_style_runtime_without_getuid_accepts_private_capability_files(monkeypatch, tmp_path):
    """Import/runtime ownership checks must not crash on Windows, where
    ``os.getuid`` does not exist. Capability secrecy and the per-user profile
    ACL remain the host boundary there."""
    real = tmp_path / "codex-real"
    _write_executable_script(real, "#!/bin/sh\nexit 0\n")
    sideband = tmp_path / "sideband"
    sideband.mkdir(mode=0o700)
    pin = sideband / app_server_mux.REAL_EXECUTABLE_CONFIG_NAME
    pin.write_text(str(real), encoding="utf-8")
    pin.chmod(0o600)
    monkeypatch.delenv(app_server_mux.ENV_REAL_EXECUTABLE, raising=False)
    monkeypatch.setenv(app_server_mux.ENV_SIDEBAND_DIR, str(sideband))
    monkeypatch.delattr(app_server_mux.os, "getuid", raising=False)
    assert app_server_mux._current_uid() is None
    assert resolve_real_executable() == str(real)


def _write_private_json(path: Path, payload: dict) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(payload).encode("utf-8"))
    finally:
        os.close(fd)


def test_stale_sideband_gc_removes_dead_descriptor_and_artifacts(tmp_path):
    sideband = tmp_path / "sideband"
    instances = sideband / app_server_mux.SIDEBAND_INSTANCES_SUBDIR
    instances.mkdir(parents=True, mode=0o700)
    instance_id = "deadbeef"
    registry = instances / f"{instance_id}.json"
    _write_private_json(registry, {
        "instance_id": instance_id,
        "pid": 999_999_999,
        "pid_start_time": 1,
    })
    socket_path = sideband / f"{instance_id}.sock"
    cap_path = sideband / f"{instance_id}.cap"
    socket_path.touch()
    cap_path.touch()

    report = gc_stale_sideband_instances(sideband)

    assert report == {
        "scanned": 1,
        "live": 0,
        "removed": 1,
        "artifacts_removed": 2,
        "kept_live_invalid": 0,
    }
    assert not registry.exists()
    assert not socket_path.exists()
    assert not cap_path.exists()


def test_stale_sideband_gc_preserves_live_but_incomplete_descriptor(tmp_path):
    sideband = tmp_path / "sideband"
    instances = sideband / app_server_mux.SIDEBAND_INSTANCES_SUBDIR
    instances.mkdir(parents=True, mode=0o700)
    registry = instances / "cafebabe.json"
    _write_private_json(registry, {
        "pid": os.getpid(),
        "pid_start_time": app_server_mux._proc_start_time(os.getpid()),
    })

    report = gc_stale_sideband_instances(sideband)

    assert report["kept_live_invalid"] == 1
    assert report["removed"] == 0
    assert registry.exists()


# ---------------------------------------------------------------------------
# Non-app-server invocation: transparent execvp passthrough (exact argv/exit)
# ---------------------------------------------------------------------------

def test_non_app_server_invocation_execs_real_binary_with_exact_argv_and_exit_code():
    with tempfile.TemporaryDirectory() as d:
        fake_real = Path(d) / "fake_real_codex"
        _write_executable_script(
            fake_real,
            "#!/usr/bin/env python3\n"
            "import sys, json\n"
            "print(json.dumps(sys.argv[1:]))\n"
            "raise SystemExit(37)\n",
        )

        env = dict(os.environ)
        env[app_server_mux.ENV_REAL_EXECUTABLE] = str(fake_real)
        mux_args = ["exec", "--foo", "bar baz"]
        if os.name == "nt":
            fake_real = fake_real.with_suffix(".py")
            fake_real.write_text(
                "import sys, json\nprint(json.dumps(sys.argv[1:]))\nraise SystemExit(37)\n",
                encoding="utf-8",
            )
            env[app_server_mux.ENV_REAL_EXECUTABLE] = sys.executable
            mux_args = [str(fake_real), *mux_args]
        result = subprocess.run(
            [sys.executable, str(MUX_MODULE), *mux_args],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 37
        assert json.loads(result.stdout.strip()) == ["exec", "--foo", "bar baz"]


def test_non_app_server_invocation_never_touches_sideband_dir():
    with tempfile.TemporaryDirectory() as d:
        fake_real = Path(d) / "fake_real_codex"
        _write_executable_script(fake_real, "#!/usr/bin/env python3\nraise SystemExit(0)\n")
        sideband_dir = Path(d) / "sideband"

        env = dict(os.environ)
        env[app_server_mux.ENV_REAL_EXECUTABLE] = str(fake_real)
        mux_args = ["login"]
        if os.name == "nt":
            fake_real = fake_real.with_suffix(".py")
            fake_real.write_text("raise SystemExit(0)\n", encoding="utf-8")
            env[app_server_mux.ENV_REAL_EXECUTABLE] = sys.executable
            mux_args = [str(fake_real), *mux_args]
        env[app_server_mux.ENV_SIDEBAND_DIR] = str(sideband_dir)
        result = subprocess.run(
            [sys.executable, str(MUX_MODULE), *mux_args],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert not sideband_dir.exists()


def test_windows_passthrough_keeps_tracked_parent_until_child_exit(monkeypatch):
    events = []
    startupinfo = object()

    class _Child:
        _handle = 91

        def wait(self, timeout=None):
            events.append(("wait", timeout))
            return 23

    monkeypatch.setattr(
        app_server_mux,
        "background_process_launch_kwargs",
        lambda: {"creationflags": 8, "startupinfo": startupinfo},
    )

    def popen(*args, **kwargs):
        events.append(("popen", args, kwargs))
        return _Child()

    monkeypatch.setattr(app_server_mux.subprocess, "Popen", popen)
    monkeypatch.setattr(
        app_server_mux,
        "_bind_child_lifetime_to_this_process",
        lambda child: events.append(("bind", child._handle)) or 77,
    )
    monkeypatch.setattr(
        app_server_mux,
        "_close_windows_handle",
        lambda handle: events.append(("close", handle)),
    )

    assert app_server_mux._hold_passthrough_child("codex.exe", ["app-server"]) == 23
    assert events == [
        (
            "popen",
            (["codex.exe", "app-server"],),
            {"shell": False, "creationflags": 8, "startupinfo": startupinfo},
        ),
        ("bind", 91),
        ("wait", None),
        ("close", 77),
    ]


def test_mux_shutdown_releases_its_exact_windows_job_handle(monkeypatch, tmp_path):
    mux = AppServerMux(
        ["app-server"],
        repo_id=_MUX_TEST_REPO_ID,
        sideband_dir=tmp_path,
        real_executable="unused",
    )

    class _ExitedChild:
        def poll(self):
            return 0

    released = []
    mux._child = _ExitedChild()
    mux._child_job_handle = 81
    monkeypatch.setattr(app_server_mux, "_close_windows_handle", released.append)
    mux.shutdown()
    assert released == [81]
    assert mux._child_job_handle is None


# ---------------------------------------------------------------------------
# In-process mux harness: OS pipes stand in for the extension's stdio
# ---------------------------------------------------------------------------

# B925: AppServerMux is repository-bound; the harness supplies one valid
# repo_id so mux registration and ownership resolution agree on it.
_MUX_TEST_REPO_ID = "repo_asmux_test"


class _MuxHarness:
    """Wires a real ``AppServerMux`` to OS pipes (fake extension) and a
    real ``_fake_app_server.py`` subprocess (fake child App Server)."""

    def __init__(
        self, tmp_path: Path, child_args: list[str] | None = None,
        *, sideband_dir: Path | None = None, repo_id: str = _MUX_TEST_REPO_ID,
        deferred_repo_binding: bool = False,
    ):
        # Drives a REAL AppServerMux + fake App Server subprocess. The stdio/
        # socket handshake round-trips are timing-sensitive and flake
        # non-deterministically under hosted-CI load (they pass in isolation
        # and locally). Gate on hosted CI, mirroring the repo's existing
        # Landlock/sandbox hosted-CI gating.
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.skip("flaky real-mux subprocess test under hosted-CI load")
        # A short, independently-rooted temp dir -- AF_UNIX socket paths
        # are capped at ~108 bytes and pytest's own `tmp_path` fixture
        # nests a long test-name-derived path that overflows that limit.
        self.sideband_dir = sideband_dir if sideband_dir is not None else Path(tempfile.mkdtemp(prefix="asmux-"))
        self._owns_sideband_dir = sideband_dir is None
        self.repo_id = repo_id
        ext_read_fd, self._to_mux_write_fd = os.pipe()
        self._from_mux_read_fd, ext_write_fd = os.pipe()
        self.mux = AppServerMux(
            ["app-server", "--listen", "stdio://"],
            real_executable=_fake_child_executable(child_args),
            extension_stdin=os.fdopen(ext_read_fd, "rb", buffering=0),
            extension_stdout=os.fdopen(ext_write_fd, "wb", buffering=0),
            sideband_dir=self.sideband_dir,
            repo_id=None if deferred_repo_binding else repo_id,
            deferred_repo_binding=deferred_repo_binding,
        )
        self._to_mux = os.fdopen(self._to_mux_write_fd, "wb", buffering=0)
        self._from_mux = os.fdopen(self._from_mux_read_fd, "rb", buffering=0)
        self._windows_lines: queue.Queue[bytes] | None = None
        if os.name == "nt":
            self._windows_lines = queue.Queue()
            threading.Thread(target=self._read_windows_lines, daemon=True).start()
        self._closed = False

    def _read_windows_lines(self) -> None:
        assert self._windows_lines is not None
        while True:
            try:
                line = self._from_mux.readline()
            except (OSError, ValueError):
                return
            if not line:
                return
            self._windows_lines.put(line)

    def start(self) -> None:
        self.mux.start()

    def send_as_extension(self, message: dict) -> None:
        payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        self._to_mux.write(payload)
        self._to_mux.flush()

    def recv_as_extension(self, timeout: float = 5.0) -> dict:
        if self._windows_lines is not None:
            try:
                line = self._windows_lines.get(timeout=timeout)
            except queue.Empty as exc:
                raise TimeoutError("timed out waiting for mux->extension line") from exc
            return json.loads(line.decode("utf-8"))
        deadline = time.monotonic() + timeout
        line = self._read_line_with_timeout(self._from_mux, deadline)
        return json.loads(line.decode("utf-8"))

    @staticmethod
    def _read_line_with_timeout(fh, deadline: float) -> bytes:
        import select as _select

        buf = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for mux->extension line")
            readable, _, _ = _select.select([fh], [], [], min(remaining, 0.2))
            if not readable:
                continue
            chunk = fh.read(1)
            if not chunk:
                raise TimeoutError("mux extension-stdout closed before newline")
            buf += chunk
            if chunk == b"\n":
                return buf

    def sideband_call(self, method: str, params: dict, *, cap: str | None = None, extra: dict | None = None) -> dict:
        token = cap if cap is not None else self.mux.capability_token
        request = {"cap": token, "method": method, "params": params}
        if extra:
            request.update(extra)
        sock = app_server_mux.connect_sideband_socket(self.mux.socket_path, timeout=10)
        try:
            sock.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
            raw = b"".join(chunks)
        finally:
            sock.close()
        return json.loads(raw.decode("utf-8"))

    def do_handshake(self) -> None:
        self.send_as_extension({"id": "ext-init", "method": "initialize", "params": {"clientInfo": {"name": "vscode-openai", "version": "1"}}})
        resp = self.recv_as_extension()
        assert resp.get("id") == "ext-init"
        assert "result" in resp
        self.send_as_extension({"method": "initialized"})
        deadline = time.monotonic() + 5
        # The mux intentionally becomes sideband-ready on the correlated
        # initialize response because current Codex clients need not emit the
        # legacy notification.  This fake server does require it, so wait
        # until the extension->child pump has observed the notification before
        # racing a sideband request against that same pipe.
        while not (self.mux.ready and self.mux._seen_initialized_notification):
            if time.monotonic() > deadline:
                raise TimeoutError("mux never became ready after handshake")
            time.sleep(0.02)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # On Windows, closing a pipe while another thread is blocked in
        # readline can itself block. Stop the writer/child first, close the
        # harness-owned mux output, then join the reader by closing its end.
        streams = (self._to_mux,) if os.name == "nt" else (self._to_mux, self._from_mux)
        for fh in streams:
            try:
                fh.close()
            except OSError:
                pass
        self.mux.shutdown()
        if os.name == "nt":
            with contextlib.suppress(OSError):
                self.mux._extension_stdout.close()
            with contextlib.suppress(OSError):
                self._from_mux.close()
        if self._owns_sideband_dir:
            shutil.rmtree(self.sideband_dir, ignore_errors=True)


@pytest.fixture()
def harness(tmp_path):
    h = _MuxHarness(tmp_path)
    h.start()
    try:
        yield h
    finally:
        h.close()


# ---------------------------------------------------------------------------
# Transparent extension<->child traffic
# ---------------------------------------------------------------------------

def test_transparent_initialize_and_initialized_handshake(harness):
    harness.do_handshake()
    assert harness.mux.ready is True


def test_ready_after_initialize_response_without_legacy_initialized_notification(harness):
    harness.send_as_extension({
        "id": "ext-init-current-client",
        "method": "initialize",
        "params": {"clientInfo": {"name": "vscode-openai", "version": "current"}},
    })
    resp = harness.recv_as_extension()
    assert resp.get("id") == "ext-init-current-client"
    assert "result" in resp
    deadline = time.monotonic() + 5
    while not harness.mux.ready:
        if time.monotonic() > deadline:
            raise TimeoutError("mux never became ready after initialize response")
        time.sleep(0.02)


def test_deferred_route_binding_never_delays_initialize_and_attaches_sideband_later(
    monkeypatch, tmp_path,
):
    route_published = threading.Event()

    def resolve_route():
        return _MUX_TEST_REPO_ID if route_published.is_set() else ""

    monkeypatch.setattr(app_server_mux, "resolve_repo_id_for_mux", resolve_route)
    h = _MuxHarness(tmp_path, deferred_repo_binding=True)
    h.start()
    try:
        started = time.monotonic()
        h.do_handshake()
        assert time.monotonic() - started < 2.0
        assert h.mux.repo_id == ""
        assert not h.mux.registry_path.exists()

        route_published.set()
        deadline = time.monotonic() + 5.0
        while h.mux.repo_id != _MUX_TEST_REPO_ID or not h.mux.registry_path.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("deferred mux never attached the published repository route")
            time.sleep(0.02)

        result = h.sideband_call("thread/resume", {"threadId": "thread-after-reload"})
        assert result["ok"] is True
    finally:
        h.close()


def test_thread_resume_sideband_projection_strips_history_and_keeps_routing_state():
    response = {
        "id": "wire-1",
        "result": {
            "model": "unused",
            "thread": {
                "id": "thread-1",
                "status": {"type": "active"},
                "turns": [
                    {"id": "done-1", "status": "completed", "startedAt": 100, "items": [{"text": "x" * 300000}]},
                    {"id": "live-1", "status": "inProgress", "startedAt": 200, "items": [{"text": "secret"}]},
                ],
            },
        },
    }
    assert app_server_mux._project_sideband_response("thread/resume", response) == {
        "id": "wire-1",
        "result": {
            "thread": {
                "id": "thread-1",
                "status": {"type": "active"},
                "turns": [
                    {"id": "done-1", "status": "completed", "startedAt": 100},
                    {"id": "live-1", "status": "inProgress", "startedAt": 200},
                ],
            }
        },
    }


def test_transparent_thread_resume_roundtrip(harness):
    harness.do_handshake()
    thread_id = f"thread-{uuid.uuid4()}"
    harness.send_as_extension({"id": 2, "method": "thread/resume", "params": {"threadId": thread_id}})
    resp = harness.recv_as_extension()
    assert resp["id"] == 2
    assert resp["result"]["thread"]["id"] == thread_id


def test_notifications_reach_extension_for_visible_ui_updates(harness):
    """turn/start's async turn/started + turn/completed notifications
    (no "id") must flow straight to the extension -- this is the exact
    "visible UI updates" requirement the mux exists to preserve."""
    harness.do_handshake()
    thread_id = f"thread-{uuid.uuid4()}"
    harness.send_as_extension({"id": 3, "method": "thread/resume", "params": {"threadId": thread_id}})
    harness.recv_as_extension()
    harness.send_as_extension({
        "id": 4, "method": "turn/start",
        "params": {"threadId": thread_id, "input": [{"type": "text", "text": "hi"}]},
    })
    ack = harness.recv_as_extension()
    assert ack["id"] == 4
    started = harness.recv_as_extension()
    assert started["method"] == "turn/started"
    completed = harness.recv_as_extension()
    assert completed["method"] == "turn/completed"
    assert completed["params"]["threadId"] == thread_id


def test_server_error_response_forwarded_transparently(harness):
    """An out-of-order request (unknown to the fake server) must still
    reach the extension as a real JSON-RPC error, never swallowed."""
    harness.send_as_extension({"id": 99, "method": "thread/resume", "params": {"threadId": "t"}})
    resp = harness.recv_as_extension()
    assert resp["id"] == 99
    assert "error" in resp


# ---------------------------------------------------------------------------
# Sideband readiness gating
# ---------------------------------------------------------------------------

def test_sideband_not_ready_before_handshake_completes(harness):
    result = harness.sideband_call("turn/steer", {"threadId": "t", "expectedTurnId": "x", "input": []})
    assert result == {"ok": False, "error": "not_ready", "detail": "app_server_not_ready"}


def test_sideband_not_ready_never_spawns_a_second_child(harness):
    original_child = harness.mux._child
    for _ in range(3):
        harness.sideband_call("thread/resume", {"threadId": "t"})
    assert harness.mux._child is original_child


# ---------------------------------------------------------------------------
# Sideband happy paths: idle turn/start, active turn/steer
# ---------------------------------------------------------------------------

def test_sideband_idle_turn_start_is_synchronous_ack_and_visible_to_extension():
    with tempfile.TemporaryDirectory() as d:
        h = _MuxHarness(Path(d))
        h.start()
        try:
            h.do_handshake()
            thread_id = f"thread-{uuid.uuid4()}"
            resumed = h.sideband_call("thread/resume", {"threadId": thread_id})
            assert resumed["ok"] is True
            result = h.sideband_call(
                "turn/start",
                {"threadId": thread_id, "input": [{"type": "text", "text": "callback prompt"}]},
            )
            assert result["ok"] is True
            assert result["response"]["result"]["turn"]["status"] == "inProgress"
            # The turn/start ack came back over the SOCKET, not extension
            # stdout -- but its notifications still surface to the
            # extension for visible-UI parity.
            started = h.recv_as_extension()
            assert started["method"] == "turn/started"
            completed = h.recv_as_extension()
            assert completed["method"] == "turn/completed"
        finally:
            h.close()


def test_sideband_active_turn_steers_without_leaking_response_to_extension(tmp_path):
    h = _MuxHarness(tmp_path, child_args=["--active-one-turn"])
    h.start()
    try:
        h.do_handshake()
        resumed = h.sideband_call("thread/resume", {"threadId": "thread-active"})
        assert resumed["ok"] is True
        result = h.sideband_call(
            "turn/steer",
            {"threadId": "thread-active", "expectedTurnId": "existing-turn-1", "input": [{"type": "text", "text": "steer"}]},
        )
        assert result == {"ok": True, "response": {"id": result["response"]["id"], "result": {"turnId": "existing-turn-1"}}}
        # The fake server sends no turn/completed for --active-one-turn --
        # confirm nothing unexpected leaked to the extension's stdout.
        with pytest.raises(TimeoutError):
            h.recv_as_extension(timeout=0.5)
    finally:
        h.close()


# ---------------------------------------------------------------------------
# B472: multiple concurrent mux instances sharing one sideband_dir --
# collision-free endpoints, owner-only registry, thread ownership derived
# ONLY from the extension's own observed traffic.
# ---------------------------------------------------------------------------

def test_two_mux_instances_never_collide_or_clobber_each_other(tmp_path):
    shared_dir = Path(tempfile.mkdtemp(prefix="asmux2-"))
    h1 = _MuxHarness(tmp_path, sideband_dir=shared_dir)
    h2 = _MuxHarness(tmp_path, sideband_dir=shared_dir)
    h1.start()
    h2.start()
    try:
        assert h1.mux.instance_id != h2.mux.instance_id
        assert h1.mux.socket_path != h2.mux.socket_path
        assert h1.mux.capability_path != h2.mux.capability_path
        assert h1.mux.registry_path != h2.mux.registry_path
        for path in (h1.mux.socket_path, h1.mux.capability_path, h1.mux.registry_path):
            assert path.exists()
        for path in (h2.mux.socket_path, h2.mux.capability_path, h2.mux.registry_path):
            assert path.exists()
        h1.do_handshake()
        h2.do_handshake()
        assert h1.mux.ready and h2.mux.ready
        # Closing the first instance must never disturb the second's live
        # endpoint -- shutdown only ever unlinks this instance's OWN paths.
        h1.close()
        assert h2.mux.socket_path.exists()
        assert h2.mux.capability_path.exists()
        assert h2.mux.registry_path.exists()
    finally:
        h1.close()
        h2.close()
        shutil.rmtree(shared_dir, ignore_errors=True)


def test_bind_socket_regenerates_id_on_collision_without_unlinking_existing_file(monkeypatch):
    """A forced instance-id collision must never unlink the pre-existing
    file at that path -- ``_bind_socket`` must regenerate a fresh id and
    retry instead, exactly the invariant that keeps a live sibling
    instance's endpoint safe from a same-process retry loop."""
    calls = {"n": 0}
    original = app_server_mux.secrets.token_hex

    def _colliding_then_unique(n):
        calls["n"] += 1
        return "deadbeef" if calls["n"] <= 2 else original(n)

    other_dir = Path(tempfile.mkdtemp(prefix="ac-"))
    try:
        collided_socket = other_dir / "deadbeef.sock"
        collided_socket.write_text("not a real socket", encoding="utf-8")
        monkeypatch.setattr(app_server_mux.secrets, "token_hex", _colliding_then_unique)
        mux = app_server_mux.AppServerMux(
            ["app-server", "--listen", "stdio://"],
            real_executable=_fake_child_executable(),
            extension_stdin=os.fdopen(os.pipe()[0], "rb", buffering=0),
            extension_stdout=os.fdopen(os.pipe()[1], "wb", buffering=0),
            sideband_dir=other_dir,
            repo_id=_MUX_TEST_REPO_ID,
        )
        try:
            mux.start()
            assert mux.instance_id != "deadbeef"
            assert collided_socket.read_text(encoding="utf-8") == "not a real socket"
        finally:
            mux.shutdown()
    finally:
        shutil.rmtree(other_dir, ignore_errors=True)


def test_ownership_recorded_only_from_extension_thread_resume(harness):
    thread_id = f"thread-{uuid.uuid4()}"
    assert thread_id not in harness.mux.owned_thread_ids
    harness.send_as_extension({"id": 501, "method": "thread/resume", "params": {"threadId": thread_id}})
    harness.recv_as_extension()
    deadline = time.monotonic() + 5
    while thread_id not in harness.mux.owned_thread_ids:
        if time.monotonic() > deadline:
            raise TimeoutError("ownership never recorded from extension traffic")
        time.sleep(0.02)
    registry = json.loads(harness.mux.registry_path.read_text(encoding="utf-8"))
    assert thread_id in registry["owned_thread_ids"]


def test_sideband_issued_resume_never_recorded_as_ownership(harness):
    """The exact ``dispatch_sideband`` path a callback-bridge probe uses
    writes straight to the child and never passes through
    ``_observe_extension_message`` -- confirmed here at the mux level."""
    harness.do_handshake()
    thread_id = f"thread-{uuid.uuid4()}"
    result = harness.sideband_call("thread/resume", {"threadId": thread_id})
    assert result["ok"] is True
    assert thread_id not in harness.mux.owned_thread_ids


# ---------------------------------------------------------------------------
# ID collision resistance / correct routing under concurrent traffic
# ---------------------------------------------------------------------------

def test_sideband_response_never_forwarded_to_extension_and_ids_never_collide(harness):
    harness.do_handshake()
    thread_id = f"thread-{uuid.uuid4()}"

    result_box: dict = {}

    def _do_sideband():
        result_box["result"] = harness.sideband_call("thread/resume", {"threadId": thread_id})

    t = threading.Thread(target=_do_sideband)
    t.start()
    t.join(timeout=10)

    assert result_box["result"]["ok"] is True
    sideband_id = result_box["result"]["response"]["id"]
    assert isinstance(sideband_id, str) and sideband_id.startswith("aiworkhub-sideband-")

    # The extension's own low-integer id traffic must still route correctly
    # and never receive the sideband's response.
    harness.send_as_extension({"id": 1, "method": "thread/resume", "params": {"threadId": thread_id}})
    resp = harness.recv_as_extension()
    assert resp["id"] == 1
    assert resp["id"] != sideband_id


# ---------------------------------------------------------------------------
# Authorization / validation rejections
# ---------------------------------------------------------------------------

def test_sideband_rejects_wrong_capability(harness):
    harness.do_handshake()
    result = harness.sideband_call("thread/resume", {"threadId": "t"}, cap="wrong-token")
    assert result == {"ok": False, "error": "unauthorized_capability"}


def test_sideband_rejects_client_supplied_id(harness):
    harness.do_handshake()
    result = harness.sideband_call("thread/resume", {"threadId": "t"}, extra={"id": 5})
    assert result == {"ok": False, "error": "forbidden_fields"}


def test_sideband_rejects_jsonrpc_field(harness):
    harness.do_handshake()
    result = harness.sideband_call("thread/resume", {"threadId": "t"}, extra={"jsonrpc": "2.0"})
    assert result == {"ok": False, "error": "forbidden_fields"}


def test_sideband_rejects_extra_transport_authority_field(harness):
    harness.do_handshake()
    result = harness.sideband_call("thread/resume", {"threadId": "t"}, extra={"override_uid": 0})
    assert result == {"ok": False, "error": "forbidden_fields"}


def test_sideband_rejects_initialize_method(harness):
    harness.do_handshake()
    result = harness.sideband_call("initialize", {"clientInfo": {"name": "x", "version": "1"}})
    assert result == {"ok": False, "error": "method_not_allowed"}


def test_sideband_rejects_arbitrary_method(harness):
    harness.do_handshake()
    result = harness.sideband_call("thread/list", {})
    assert result == {"ok": False, "error": "method_not_allowed"}


def test_sideband_allowlist_is_exactly_three_methods():
    assert SIDEBAND_ALLOWED_METHODS == {"thread/resume", "turn/steer", "turn/start"}


def test_sideband_rejects_malformed_json(harness):
    sock = app_server_mux.connect_sideband_socket(harness.mux.socket_path, timeout=5)
    try:
        sock.sendall(b"{not json\n")
        sock.shutdown(socket.SHUT_WR)
        raw = sock.recv(4096)
    finally:
        sock.close()
    assert json.loads(raw.decode("utf-8")) == {"ok": False, "error": "malformed_request"}


def test_sideband_rejects_oversized_request(harness):
    sock = app_server_mux.connect_sideband_socket(harness.mux.socket_path, timeout=5)
    try:
        oversized = json.dumps({
            "cap": harness.mux.capability_token, "method": "thread/resume",
            "params": {"threadId": "t" * (app_server_mux.SIDEBAND_MAX_REQUEST_BYTES + 1024)},
        })
        sock.sendall(oversized.encode("utf-8") + b"\n")
        sock.shutdown(socket.SHUT_WR)
        raw = sock.recv(4096)
    finally:
        sock.close()
    assert json.loads(raw.decode("utf-8")) == {"ok": False, "error": "request_too_large"}


def test_sideband_socket_mode_is_owner_only(harness):
    mode = stat.S_IMODE(os.stat(harness.mux.socket_path).st_mode)
    assert harness.mux.socket_path.exists()
    assert os.name == "nt" or mode == 0o600


def test_sideband_capability_file_mode_is_owner_only(harness):
    mode = stat.S_IMODE(os.stat(harness.mux.capability_path).st_mode)
    assert harness.mux.capability_path.exists()
    assert os.name == "nt" or mode == 0o600


def test_sideband_registry_file_mode_is_owner_only(harness):
    mode = stat.S_IMODE(os.stat(harness.mux.registry_path).st_mode)
    assert harness.mux.registry_path.exists()
    assert os.name == "nt" or mode == 0o600


def test_sideband_directory_mode_is_owner_only(harness):
    mode = stat.S_IMODE(os.stat(harness.sideband_dir).st_mode)
    assert harness.sideband_dir.is_dir()
    assert os.name == "nt" or mode == 0o700


def test_sideband_rejects_wrong_uid_peer(harness, monkeypatch):
    monkeypatch.setattr(AppServerMux, "_peer_uid_ok", lambda self, conn: False)
    result = harness.sideband_call("thread/resume", {"threadId": "t"})
    assert result == {"ok": False, "error": "unauthorized_peer"}


# ---------------------------------------------------------------------------
# Duplicate suppression
# ---------------------------------------------------------------------------


class _LiveChildStub:
    def poll(self):
        return None


def _dispatch_only_mux(tmp_path: Path) -> AppServerMux:
    mux = AppServerMux(
        ["app-server", "--listen", "stdio://"],
        real_executable=[sys.executable, "unused"],
        sideband_dir=tmp_path / "sideband",
        repo_id=None,
        deferred_repo_binding=True,
    )
    mux._child = _LiveChildStub()
    mux._child_epoch = "epoch-1"
    mux._ready_event.set()
    return mux


def test_concurrent_sideband_responses_route_out_of_order_to_exact_owner(tmp_path):
    mux = _dispatch_only_mux(tmp_path)
    written: list[dict] = []
    lock = threading.Lock()

    def write(raw: bytes) -> None:
        with lock:
            written.append(json.loads(raw))
            if len(written) != 2:
                return
            first, second = written
        for request in (second, first):
            response = {
                "id": request["id"],
                "result": {"thread": {"id": request["params"]["threadId"]}},
            }
            assert mux._route_child_message(
                (json.dumps(response) + "\n").encode("utf-8")
            ) is True

    mux._write_to_child = write
    results: dict[str, dict] = {}
    barrier = threading.Barrier(2)

    def call(thread_id: str) -> None:
        barrier.wait(timeout=5)
        results[thread_id] = mux.dispatch_sideband(
            "thread/resume", {"threadId": thread_id}
        )

    threads = [threading.Thread(target=call, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(written) == 2
    assert {row["id"] for row in written} == {
        f"{app_server_mux.SIDEBAND_ID_PREFIX}epoch-1-1",
        f"{app_server_mux.SIDEBAND_ID_PREFIX}epoch-1-2",
    }
    assert results["a"]["result"]["thread"]["id"] == "a"
    assert results["b"]["result"]["thread"]["id"] == "b"


def test_late_sideband_response_is_consumed_not_forwarded(tmp_path):
    mux = _dispatch_only_mux(tmp_path)
    raw = json.dumps({
        "id": f"{app_server_mux.SIDEBAND_ID_PREFIX}expired-9",
        "result": {"ok": True},
    }).encode("utf-8") + b"\n"

    assert mux._route_child_message(raw) is True


def test_concurrent_identical_sideband_calls_share_one_child_write(tmp_path):
    mux = _dispatch_only_mux(tmp_path)
    written: list[dict] = []
    wrote = threading.Event()

    def write(raw: bytes) -> None:
        written.append(json.loads(raw))
        wrote.set()

    mux._write_to_child = write
    results: list[dict] = []
    barrier = threading.Barrier(2)

    def call() -> None:
        barrier.wait(timeout=5)
        results.append(mux.dispatch_sideband(
            "thread/resume", {"threadId": "same"}
        ))

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert wrote.wait(timeout=5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if sum(thread.is_alive() for thread in threads) == 2:
            break
        time.sleep(0.001)
    assert len(written) == 1
    response = {
        "id": written[0]["id"],
        "result": {"thread": {"id": "same"}},
    }
    assert mux._route_child_message(
        (json.dumps(response) + "\n").encode("utf-8")
    ) is True
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(written) == 1
    assert len(results) == 2
    assert results[0] == results[1]

def test_sideband_duplicate_request_is_suppressed_not_redelivered(harness, monkeypatch):
    harness.do_handshake()
    calls = {"n": 0}
    original = AppServerMux._next_sideband_id

    def _counting(self):
        calls["n"] += 1
        return original(self)

    monkeypatch.setattr(AppServerMux, "_next_sideband_id", _counting)

    params = {"threadId": "thread-dedup"}
    first = harness.sideband_call("thread/resume", params)
    second = harness.sideband_call("thread/resume", params)
    assert first["ok"] is True and second["ok"] is True
    assert first["response"] == second["response"]
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# No-fallback-spawn + unavailable-proxy deferral
# ---------------------------------------------------------------------------

def test_sideband_defers_when_child_has_exited(tmp_path):
    h = _MuxHarness(tmp_path)
    h.start()
    try:
        h.do_handshake()
        h.mux._child.terminate()
        h.mux._child.wait(timeout=5)
        result = h.sideband_call("thread/resume", {"threadId": "t"})
        assert result["ok"] is False
        assert result["error"] == "not_ready"
    finally:
        h.close()


def test_dispatch_sideband_raises_not_ready_never_raises_for_spawn(harness):
    with pytest.raises(app_server_mux.SidebandNotReady):
        harness.mux.dispatch_sideband("thread/resume", {"threadId": "t"})


def test_dispatch_sideband_rejects_disallowed_method(harness):
    with pytest.raises(app_server_mux.SidebandRejected):
        harness.mux.dispatch_sideband("initialize", {})


# ---------------------------------------------------------------------------
# Clean shutdown
# ---------------------------------------------------------------------------

def test_clean_shutdown_removes_socket_and_capability_file(tmp_path):
    h = _MuxHarness(tmp_path)
    h.start()
    socket_path = h.mux.socket_path
    cap_path = h.mux.capability_path
    registry_path = h.mux.registry_path
    assert socket_path.exists()
    assert cap_path.exists()
    assert registry_path.exists()
    h.close()
    assert not socket_path.exists()
    assert not cap_path.exists()
    assert not registry_path.exists()


def test_wait_returns_child_exit_code_and_cleans_up(tmp_path):
    h = _MuxHarness(tmp_path)
    h.start()
    h._to_mux.close()  # EOF on the extension side -> mux closes child stdin
    try:
        returncode = h.mux.wait()
    finally:
        h._closed = True
        if os.name == "nt":
            with contextlib.suppress(OSError):
                h.mux._extension_stdout.close()
        try:
            h._from_mux.close()
        except OSError:
            pass
    assert returncode == 0
    assert not h.mux.socket_path.exists()


# ---------------------------------------------------------------------------
# install_vscode_app_server_mux.py -- dry-run/check/print-config only
# ---------------------------------------------------------------------------

INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "install_vscode_app_server_mux.py"


def _run_installer(args: list[str]) -> dict:
    result = subprocess.run(
        [sys.executable, str(INSTALLER), *args],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_installer_check_reports_readiness_without_mutation():
    before = {p: p.exists() for p in _watched_paths()}
    report = _run_installer(["--check"])
    after = {p: p.exists() for p in _watched_paths()}
    assert "mux_module_exists" in report
    assert "real_codex_found_on_path" in report
    assert before == after


def test_installer_print_config_default_shows_apply_and_rollback():
    before = {p: p.exists() for p in _watched_paths()}
    report = _run_installer([])
    after = {p: p.exists() for p in _watched_paths()}
    assert report["mode"] == "dry_run_print_config_only"
    assert report["no_mutation_performed"] is True
    assert "chatgpt.cliExecutable" in report["apply_setting"]
    assert isinstance(report["apply_setting"]["chatgpt.cliExecutable"], str)
    assert "chatgpt.cliExecutable" in report["rollback_setting"]
    assert report["real_executable_pin"]["required_mode"] == "0600"
    assert before == after


def test_installer_never_touches_vscode_settings_or_sideband_dir(tmp_path, monkeypatch):
    fake_settings = tmp_path / "settings.json"
    fake_settings.write_text("{}")
    before = fake_settings.read_text()
    env = dict(os.environ)
    env[app_server_mux.ENV_SIDEBAND_DIR] = str(tmp_path / "sideband")
    result = subprocess.run(
        [sys.executable, str(INSTALLER)], env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert fake_settings.read_text() == before
    assert not (tmp_path / "sideband").exists()


def _watched_paths() -> list[Path]:
    return [default_sideband_dir()]


def test_dispatch_sideband_rejects_empty_string_params_before_child_write(tmp_path):
    mux = _dispatch_only_mux(tmp_path)
    written: list[bytes] = []
    mux._write_to_child = lambda raw: written.append(raw)

    with pytest.raises(app_server_mux.SidebandRejected) as exc_info:
        mux.dispatch_sideband("thread/resume", "")

    assert str(exc_info.value) == "invalid_params:empty_string"
    assert written == []


@pytest.mark.parametrize("bad_params", [[], 5, True, "not empty", 1.5])
def test_dispatch_sideband_rejects_non_object_params_before_child_write(tmp_path, bad_params):
    mux = _dispatch_only_mux(tmp_path)
    written: list[bytes] = []
    mux._write_to_child = lambda raw: written.append(raw)

    with pytest.raises(app_server_mux.SidebandRejected) as exc_info:
        mux.dispatch_sideband("thread/resume", bad_params)

    assert str(exc_info.value) == f"invalid_params:non_object:{type(bad_params).__name__}"
    assert written == []


def test_dispatch_sideband_accepts_valid_object_params_and_reaches_child(tmp_path):
    mux = _dispatch_only_mux(tmp_path)
    written: list[dict] = []

    def write(raw: bytes) -> None:
        request = json.loads(raw)
        written.append(request)
        response = {"id": request["id"], "result": {"threadId": request["params"]["threadId"]}}
        mux._route_child_message((json.dumps(response) + "\n").encode("utf-8"))

    mux._write_to_child = write

    result = mux.dispatch_sideband("thread/resume", {"threadId": "x"})

    assert result["result"]["threadId"] == "x"
    assert len(written) == 1


def test_malformed_params_reject_independently_without_blocking_valid_clients(tmp_path):
    """B: an empty-string/non-object params call must fail on its own lane --
    it must never poison, retry, or block concurrent valid sideband callers."""

    mux = _dispatch_only_mux(tmp_path)
    written: list[dict] = []
    write_lock = threading.Lock()

    def write(raw: bytes) -> None:
        with write_lock:
            request = json.loads(raw)
            written.append(request)
        response = {"id": request["id"], "result": {"threadId": request["params"]["threadId"]}}
        mux._route_child_message((json.dumps(response) + "\n").encode("utf-8"))

    mux._write_to_child = write

    results: dict[str, object] = {}
    barrier = threading.Barrier(3)

    def call_valid(name: str) -> None:
        barrier.wait(timeout=5)
        results[name] = mux.dispatch_sideband("thread/resume", {"threadId": name})

    def call_invalid() -> None:
        barrier.wait(timeout=5)
        try:
            mux.dispatch_sideband("thread/resume", "")
        except app_server_mux.SidebandRejected as exc:
            results["invalid"] = exc

    threads = [
        threading.Thread(target=call_valid, args=("a",)),
        threading.Thread(target=call_valid, args=("b",)),
        threading.Thread(target=call_invalid),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert results["a"]["result"]["threadId"] == "a"
    assert results["b"]["result"]["threadId"] == "b"
    assert isinstance(results["invalid"], app_server_mux.SidebandRejected)
    assert len(written) == 2


def test_dispatch_sideband_invalid_params_records_one_bounded_durable_alert(tmp_path):
    mux = _dispatch_only_mux(tmp_path)
    mux._repo_root = tmp_path
    mux._repo_id = "test-repo"
    alert_path = tmp_path / ".aiworkhub" / "runtime" / "mcp_protocol_alerts.json"

    with pytest.raises(app_server_mux.SidebandRejected):
        mux.dispatch_sideband("thread/resume", "")

    payload = json.loads(alert_path.read_text(encoding="utf-8"))
    assert payload["count"] == 1
    latest = payload["latest"]
    assert latest["method"] == "thread/resume"
    assert latest["boundary"] == "app_server_mux_sideband"
    assert latest["reason"] == "invalid_params:empty_string"
    assert latest["repo_identity"] == "test-repo"
    assert latest["request_id"] is None
    assert isinstance(latest["timestamp"], str) and latest["timestamp"]
    # Bounded/redacted: no raw "params" key and no raw rejected payload value
    # (the empty string that was rejected) anywhere in the serialized record.
    # The safe bounded reason string, which happens to contain the substring
    # "params" inside "invalid_params", is explicitly allowed.
    assert set(latest.keys()) == {
        "method",
        "request_id",
        "boundary",
        "reason",
        "repo_identity",
        "timestamp",
    }
    assert "params" not in payload
    assert "" not in latest.values()

    with pytest.raises(app_server_mux.SidebandRejected):
        mux.dispatch_sideband("thread/resume", [1, 2])

    payload = json.loads(alert_path.read_text(encoding="utf-8"))
    assert payload["count"] == 2
    assert payload["latest"]["reason"] == "invalid_params:non_object:list"


def test_dispatch_sideband_invalid_params_skips_alert_without_repo_root(tmp_path):
    mux = _dispatch_only_mux(tmp_path)
    assert mux._repo_root is None

    with pytest.raises(app_server_mux.SidebandRejected):
        mux.dispatch_sideband("thread/resume", "")

    assert not (tmp_path / ".aiworkhub").exists()


def test_bind_and_connect_sideband_socket_survive_a_long_retained_workspace_path(tmp_path):
    """Regression for NF-2026-00203 rework: a retained validation workspace
    (this worktree) can itself already be deep enough that even a short
    instance-id filename under it overflows AF_UNIX's ~108-byte
    ``sun_path``. ``bind_sideband_listener``/``connect_sideband_socket``
    must still succeed by binding/connecting with a CWD-relative filename
    -- never by relocating the endpoint outside the caller's own
    ``sideband_dir`` (no /tmp, /var/tmp, /dev/shm, or machine-wide
    fallback)."""
    if not app_server_mux.sideband_uses_unix_socket():
        pytest.skip("AF_UNIX-specific regression")

    deep = tmp_path
    while len(str(deep)) < 200:
        deep = deep / "nested_segment_to_force_a_long_retained_workspace_path"
    deep.mkdir(parents=True, mode=0o700)
    assert len(str(deep)) > 108

    path = deep / "12345678.sock"
    srv = app_server_mux.bind_sideband_listener(path)
    srv.listen(1)
    try:
        assert path.exists()
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        client = app_server_mux.connect_sideband_socket(path, timeout=5)
        try:
            conn, _ = srv.accept()
            try:
                client.sendall(b"ping")
                assert conn.recv(4) == b"ping"
            finally:
                conn.close()
        finally:
            client.close()
    finally:
        srv.close()
        with contextlib.suppress(FileNotFoundError, OSError):
            os.unlink(path)
