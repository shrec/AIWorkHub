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

import json
import os
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

from geoai_task_mcp import app_server_mux  # noqa: E402
from geoai_task_mcp.app_server_mux import (  # noqa: E402
    SIDEBAND_ALLOWED_METHODS,
    AppServerMux,
    default_sideband_dir,
    is_app_server_invocation,
    resolve_real_executable,
)

FAKE_SERVER = Path(__file__).resolve().parent / "_fake_app_server.py"
MUX_MODULE = Path(__file__).resolve().parents[1] / "src" / "geoai_task_mcp" / "app_server_mux.py"


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


# ---------------------------------------------------------------------------
# Invocation-shape detection / executable resolution
# ---------------------------------------------------------------------------

def test_is_app_server_invocation_true_for_app_server_subcommand():
    assert is_app_server_invocation(["app-server", "--listen", "stdio://"]) is True


def test_is_app_server_invocation_false_for_exec():
    assert is_app_server_invocation(["exec", "--foo", "bar"]) is False
    assert is_app_server_invocation([]) is False


def test_resolve_real_executable_env_override(monkeypatch):
    monkeypatch.setenv(app_server_mux.ENV_REAL_EXECUTABLE, "/usr/local/bin/codex-real")
    assert resolve_real_executable() == "/usr/local/bin/codex-real"


def test_resolve_real_executable_default(monkeypatch):
    monkeypatch.delenv(app_server_mux.ENV_REAL_EXECUTABLE, raising=False)
    monkeypatch.setenv(app_server_mux.ENV_SIDEBAND_DIR, "/nonexistent/geoai-sideband-test")
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
        result = subprocess.run(
            [sys.executable, str(MUX_MODULE), "exec", "--foo", "bar baz"],
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
        env[app_server_mux.ENV_SIDEBAND_DIR] = str(sideband_dir)
        result = subprocess.run(
            [sys.executable, str(MUX_MODULE), "login"],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert not sideband_dir.exists()


# ---------------------------------------------------------------------------
# In-process mux harness: OS pipes stand in for the extension's stdio
# ---------------------------------------------------------------------------

class _MuxHarness:
    """Wires a real ``AppServerMux`` to OS pipes (fake extension) and a
    real ``_fake_app_server.py`` subprocess (fake child App Server)."""

    def __init__(
        self, tmp_path: Path, child_args: list[str] | None = None,
        *, sideband_dir: Path | None = None,
    ):
        # A short, independently-rooted temp dir -- AF_UNIX socket paths
        # are capped at ~108 bytes and pytest's own `tmp_path` fixture
        # nests a long test-name-derived path that overflows that limit.
        self.sideband_dir = sideband_dir if sideband_dir is not None else Path(tempfile.mkdtemp(prefix="asmux-"))
        self._owns_sideband_dir = sideband_dir is None
        ext_read_fd, self._to_mux_write_fd = os.pipe()
        self._from_mux_read_fd, ext_write_fd = os.pipe()
        self.mux = AppServerMux(
            ["app-server", "--listen", "stdio://"],
            real_executable=_fake_child_executable(child_args),
            extension_stdin=os.fdopen(ext_read_fd, "rb", buffering=0),
            extension_stdout=os.fdopen(ext_write_fd, "wb", buffering=0),
            sideband_dir=self.sideband_dir,
        )
        self._to_mux = os.fdopen(self._to_mux_write_fd, "wb", buffering=0)
        self._from_mux = os.fdopen(self._from_mux_read_fd, "rb", buffering=0)
        self._closed = False

    def start(self) -> None:
        self.mux.start()

    def send_as_extension(self, message: dict) -> None:
        payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        self._to_mux.write(payload)
        self._to_mux.flush()

    def recv_as_extension(self, timeout: float = 5.0) -> dict:
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
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        try:
            sock.connect(str(self.mux.socket_path))
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
        for fh in (self._to_mux, self._from_mux):
            try:
                fh.close()
            except OSError:
                pass
        self.mux.shutdown()
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
    assert isinstance(sideband_id, str) and sideband_id.startswith("geoai-sideband-")

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
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect(str(harness.mux.socket_path))
        sock.sendall(b"{not json\n")
        sock.shutdown(socket.SHUT_WR)
        raw = sock.recv(4096)
    finally:
        sock.close()
    assert json.loads(raw.decode("utf-8")) == {"ok": False, "error": "malformed_request"}


def test_sideband_rejects_oversized_request(harness):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect(str(harness.mux.socket_path))
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
    assert mode == 0o600


def test_sideband_capability_file_mode_is_owner_only(harness):
    mode = stat.S_IMODE(os.stat(harness.mux.capability_path).st_mode)
    assert mode == 0o600


def test_sideband_registry_file_mode_is_owner_only(harness):
    mode = stat.S_IMODE(os.stat(harness.mux.registry_path).st_mode)
    assert mode == 0o600


def test_sideband_directory_mode_is_owner_only(harness):
    mode = stat.S_IMODE(os.stat(harness.sideband_dir).st_mode)
    assert mode == 0o700


def test_sideband_rejects_wrong_uid_peer(harness, monkeypatch):
    monkeypatch.setattr(AppServerMux, "_peer_uid_ok", lambda self, conn: False)
    result = harness.sideband_call("thread/resume", {"threadId": "t"})
    assert result == {"ok": False, "error": "unauthorized_peer"}


# ---------------------------------------------------------------------------
# Duplicate suppression
# ---------------------------------------------------------------------------

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
