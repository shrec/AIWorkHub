"""SCAN-E3FF: app_server_mux liveness -- a component that cannot serve must
stop advertising that it can, and a transient error must not become permanent.

Each test here pins one of the five confirmed defects from the 2026-08-16
adversarial scan of ``src/aiworkhub/app_server_mux.py``. The mux itself is
driven with real objects (a real ``_write_owner_only_file`` on a real temp
dir, a real ``socket.socketpair``, a real accept loop over a scripted
listener) -- no behaviour under test is mocked away.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import app_server_mux  # noqa: E402
from aiworkhub.app_server_mux import AppServerMux  # noqa: E402


def _bare_mux(tmp_path: Path) -> AppServerMux:
    """An unstarted, repo-unbound mux -- no child, no socket, no threads.
    Individual tests set only the attributes their path exercises."""
    return AppServerMux(
        ["app-server", "--listen", "stdio://"],
        real_executable=[sys.executable, "unused"],
        sideband_dir=tmp_path / "sideband",
        repo_id=None,
        deferred_repo_binding=True,
    )


class _LiveChildStub:
    def poll(self):
        return None


class _ScriptedListener:
    """A stand-in listener whose ``accept`` raises a scripted sequence of
    errors, then behaves like an idle poll (``socket.timeout``)."""

    def __init__(self, script):
        self._script = list(script)
        self.closed = False

    def settimeout(self, _timeout):
        pass

    def accept(self):
        if self._script:
            raise self._script.pop(0)
        time.sleep(0.02)  # mimic a real settimeout-bounded idle poll
        raise socket.timeout()

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# ONE (HIGH): a transient owner-file write failure must not wedge forever.
#
# Before: on a write/fsync/verify failure the exclusive-create temp file
# (fixed pid+thread-ident name) was left on disk, so every later O_EXCL open
# raised FileExistsError, which a suppressed OSError swallowed -- one ENOSPC
# froze heartbeat_at and 90s later the mux stopped being a routing candidate.
# After: any failure unlinks the temp, so the next write (heartbeat) succeeds.
# ---------------------------------------------------------------------------

def test_failed_owner_write_leaves_no_temp_so_next_write_succeeds(tmp_path, monkeypatch):
    priv = tmp_path / "priv"
    priv.mkdir(mode=0o700)
    target = priv / "registry.json"

    real_fsync = os.fsync
    state = {"fail": True}

    def flaky_fsync(fd):
        if state["fail"]:
            state["fail"] = False
            raise OSError(errno.EIO, "simulated fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(app_server_mux.os, "fsync", flaky_fsync)

    with pytest.raises(OSError):
        app_server_mux._write_owner_only_file(target, b"first", max_bytes=4096)

    # The exclusive-create temp must not survive the failure.
    assert list(priv.glob("registry.json.*.tmp")) == []

    # Same thread -> same temp name; a leaked temp would make this second
    # O_EXCL open fail with FileExistsError forever.
    app_server_mux._write_owner_only_file(target, b"second", max_bytes=4096)
    assert target.read_bytes() == b"second"


def test_transient_heartbeat_failure_recovers_and_refreshes_heartbeat_at(tmp_path, monkeypatch):
    mux = _bare_mux(tmp_path)
    mux._repo_id = "repo_heartbeat"
    mux._server_socket = object()  # non-None so _write_registry actually writes
    mux._registry_path = tmp_path / "reg.json"

    real_fsync = os.fsync
    state = {"fail": True}

    def flaky_fsync(fd):
        if state["fail"]:
            state["fail"] = False
            raise OSError(errno.ENOSPC, "simulated full disk")
        return real_fsync(fd)

    monkeypatch.setattr(app_server_mux.os, "fsync", flaky_fsync)

    # First heartbeat tick fails -- surfaced, exactly like an ENOSPC on a full
    # root disk. The heartbeat loop suppresses it per-tick and tries again.
    with pytest.raises(OSError):
        mux._write_registry()
    assert list(tmp_path.glob("reg.json.*.tmp")) == []

    # The very next tick must succeed and refresh heartbeat_at, rather than
    # failing forever on a leaked temp.
    mux._write_registry()
    descriptor = json.loads(mux._registry_path.read_text(encoding="utf-8"))
    assert isinstance(descriptor["heartbeat_at"], (int, float))
    assert descriptor["heartbeat_at"] > 0


# ---------------------------------------------------------------------------
# TWO (HIGH): the accept loop must not die while still advertising ready.
#
# Before: every OSError from accept() broke the loop with nothing closing the
# listener or clearing ready -- the instance kept publishing a fresh ready
# descriptor while accepting nothing.
# After: a fatal accept error closes the listener and clears ready.
# ---------------------------------------------------------------------------

def test_fatal_accept_error_closes_listener_and_clears_advertised_ready(tmp_path):
    mux = _bare_mux(tmp_path)
    mux._repo_id = "repo_accept"
    mux._registry_path = tmp_path / "reg.json"
    listener = _ScriptedListener([OSError(errno.EBADF, "bad file descriptor")])
    mux._server_socket = listener
    mux._ready_event.set()

    mux._accept_sideband_loop()  # fatal error -> fail-closed, then returns

    assert mux.ready is False
    assert listener.closed is True
    descriptor = json.loads(mux._registry_path.read_text(encoding="utf-8"))
    assert descriptor["ready"] is False


# ---------------------------------------------------------------------------
# FOUR (accept, MEDIUM/principle): a recoverable accept error must NOT
# terminate the loop -- the mux keeps serving instead of going dead-but-ready.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("recoverable", ["ECONNABORTED", "EINTR", "EMFILE"])
def test_recoverable_accept_error_does_not_terminate_loop(tmp_path, recoverable):
    code = getattr(errno, recoverable)
    mux = _bare_mux(tmp_path)
    listener = _ScriptedListener([OSError(code, recoverable)])
    mux._server_socket = listener
    mux._ready_event.set()

    thread = threading.Thread(target=mux._accept_sideband_loop, daemon=True)
    thread.start()
    try:
        time.sleep(0.5)  # the recoverable error is consumed; the loop keeps polling
        assert mux.ready is True
        assert listener.closed is False
        assert thread.is_alive()
    finally:
        mux._stop_event.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


# ---------------------------------------------------------------------------
# THREE (MEDIUM): a sideband request has a real end-to-end deadline.
#
# Before: SIDEBAND_REQUEST_DEADLINE_SECONDS was a per-recv timeout while
# _read_bounded_line looped recv until a newline, so a client that never sent
# one lived ~34 days holding a thread + descriptor. After: a silent client is
# dropped within the deadline and its thread + descriptor are released.
# ---------------------------------------------------------------------------

def test_silent_client_dropped_within_end_to_end_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(app_server_mux, "SIDEBAND_REQUEST_DEADLINE_SECONDS", 0.5)
    mux = _bare_mux(tmp_path)

    srv_conn, client = socket.socketpair()
    try:
        started = time.monotonic()
        thread = threading.Thread(target=mux._handle_sideband_conn, args=(srv_conn,))
        thread.start()
        # client never sends a newline (in fact never sends anything).
        thread.join(timeout=5)
        elapsed = time.monotonic() - started

        assert not thread.is_alive()          # its thread is released...
        assert srv_conn.fileno() == -1        # ...and its descriptor closed
        assert elapsed < 3.0                  # within the deadline, not 34 days
    finally:
        with contextlib.suppress(OSError):
            srv_conn.close()
        client.close()


# ---------------------------------------------------------------------------
# FIVE (LOW): a failed child write clears the pending record and returns a
# JSON error rather than closing the connection bare.
# ---------------------------------------------------------------------------

def test_failed_child_write_clears_pending_and_raises_not_ready(tmp_path):
    mux = _bare_mux(tmp_path)
    mux._child = _LiveChildStub()
    mux._child_epoch = "epoch-five"
    mux._ready_event.set()

    def boom(_raw):
        raise OSError(errno.EIO, "child write failed")

    mux._write_to_child = boom

    with pytest.raises(app_server_mux.SidebandNotReady) as exc_info:
        mux.dispatch_sideband("thread/resume", {"threadId": "t"})

    assert str(exc_info.value).startswith("child_write_failed")
    assert mux._pending_by_id == {}  # no leaked pending flight


def test_failed_child_write_returns_json_error_to_client_not_bare_close(tmp_path):
    mux = _bare_mux(tmp_path)
    mux._child = _LiveChildStub()
    mux._child_epoch = "epoch-five-e2e"
    mux._ready_event.set()
    mux._capability_token = "cap-token-for-test"

    def boom(_raw):
        raise OSError(errno.EIO, "child write failed")

    mux._write_to_child = boom

    srv_conn, client = socket.socketpair()
    try:
        thread = threading.Thread(target=mux._handle_sideband_conn, args=(srv_conn,))
        thread.start()
        request = {"cap": "cap-token-for-test", "method": "thread/resume", "params": {"threadId": "t"}}
        client.sendall((json.dumps(request) + "\n").encode("utf-8"))
        client.shutdown(socket.SHUT_WR)

        client.settimeout(5)
        data = b""
        while b"\n" not in data:
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert data, "client received a bare close instead of a JSON error"
        response = json.loads(data.decode("utf-8"))
        assert response["ok"] is False
        assert response["error"] == "not_ready"
        assert response["detail"].startswith("child_write_failed")
    finally:
        with contextlib.suppress(OSError):
            srv_conn.close()
        client.close()


# ---------------------------------------------------------------------------
# FOUR (MEDIUM): ensure_private_dir validates with lstat before it changes any
# mode, and never follows a symlink when doing so.
# ---------------------------------------------------------------------------

def test_ensure_private_dir_never_chmods_through_a_symlink(tmp_path):
    if not hasattr(os, "symlink") or not hasattr(os, "getuid"):
        pytest.skip("POSIX symlink/uid semantics required")

    # A directory an attacker would love this process to chmod 0700 for them.
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o755)
    victim_mode_before = os.stat(victim).st_mode

    # The sideband path is a symlink pointing at the victim directory.
    link = tmp_path / "sideband_link"
    os.symlink(victim, link, target_is_directory=True)

    with pytest.raises(PermissionError):
        app_server_mux.ensure_private_dir(link)

    # The symlink target's mode must be completely untouched.
    assert os.stat(victim).st_mode == victim_mode_before
    # lstat still shows a symlink (never replaced/traversed for a chmod).
    assert os.path.islink(link)


def test_ensure_private_dir_relaxes_a_preexisting_loose_real_dir(tmp_path):
    if not hasattr(os, "getuid"):
        pytest.skip("POSIX mode semantics required")
    import stat as _stat
    probe = tmp_path / "probe"
    probe.mkdir(mode=0o700)
    try:
        app_server_mux._chmod_dir_nofollow(probe, 0o700)
    except OSError:
        pytest.skip("sandbox rejects directory fchmod")
    d = tmp_path / "loose"
    d.mkdir(mode=0o755)
    # A real, self-owned directory that merely pre-existed with loose perms is
    # tightened to 0700 (defense-in-depth via an O_NOFOLLOW fd), not rejected.
    app_server_mux.ensure_private_dir(d)
    assert _stat.S_IMODE(os.lstat(d).st_mode) == 0o700


# ---------------------------------------------------------------------------
# FIVE (LOW), rework: every early-return reply is sent under a real budget.
#
# _read_bounded_line shrinks the socket timeout toward the request deadline as
# it reads. The authorized path resets to a full budget before it replies, but
# the two early-return branches (a peer that fails the uid check, and an
# oversized request) used to reply under whatever near-zero timeout the read
# left behind. _send_json swallows the resulting socket.timeout, so the peer
# saw a bare connection close instead of the JSON error the branch exists to
# deliver -- the same illegibility Finding FIVE set out to remove. The fix
# resets the timeout inside _send_json, so all three exits share one budget.
#
# Both tests below are deterministic: a controlled read leaves the socket with
# a near-zero timeout, and a socket wrapper models the real kernel behaviour of
# a sendall that cannot complete under that near-zero budget (socket.timeout),
# so the reply is delivered only when the fix has restored a real budget first.
# ---------------------------------------------------------------------------

class _StarveOnTinyTimeoutSocket:
    """Wraps a real socket. A ``sendall`` issued while the socket's timeout is
    below one second models the kernel behaviour of a send that cannot complete
    under a near-zero budget: it raises ``socket.timeout`` instead of delivering
    the frame. Every other call -- ``settimeout``, ``gettimeout``, ``recv``,
    ``getsockopt``, ``fileno``, ``close`` -- delegates to the real socket, so
    the read path (which legitimately shrinks the timeout toward the deadline)
    and the descriptor lifecycle are unchanged."""

    _STARVE_FLOOR = 1.0

    def __init__(self, sock):
        self._sock = sock

    def sendall(self, data, *args):
        timeout = self._sock.gettimeout()
        if timeout is not None and timeout < self._STARVE_FLOOR:
            raise socket.timeout("send starved by leftover request deadline")
        return self._sock.sendall(data, *args)

    def __getattr__(self, name):
        return getattr(self._sock, name)


def _early_return_client_reply(tmp_path, monkeypatch, *, peer_ok, read_result):
    """Drive ``_handle_sideband_conn`` to one of its early-return branches with
    the socket left in the near-zero-timeout state a deadline-consuming read
    produces, and return the raw bytes the client received."""
    mux = _bare_mux(tmp_path)
    srv_raw, client = socket.socketpair()
    srv = _StarveOnTinyTimeoutSocket(srv_raw)

    # Reach the branch under test: force the uid decision, and reproduce the
    # exact side effect of a read that ran the request deadline nearly to zero.
    monkeypatch.setattr(mux, "_peer_uid_ok", lambda conn: peer_ok)

    def deadline_consuming_read(conn, deadline):
        conn.settimeout(0.001)  # request arrived with the deadline nearly spent
        return read_result

    monkeypatch.setattr(mux, "_read_bounded_line", deadline_consuming_read)

    try:
        thread = threading.Thread(target=mux._handle_sideband_conn, args=(srv,))
        thread.start()
        client.settimeout(5)
        data = b""
        with contextlib.suppress(OSError):
            while b"\n" not in data:
                chunk = client.recv(4096)
                if not chunk:
                    break
                data += chunk
        thread.join(timeout=5)
        assert not thread.is_alive()
        return data
    finally:
        with contextlib.suppress(OSError):
            srv_raw.close()
        client.close()


def test_unauthorized_peer_reply_survives_a_deadline_consuming_read(tmp_path, monkeypatch):
    data = _early_return_client_reply(
        tmp_path, monkeypatch, peer_ok=False, read_result=(b"", False)
    )
    assert data, "unauthorized peer received a bare close instead of a JSON error"
    response = json.loads(data.decode("utf-8"))
    assert response["ok"] is False
    assert response["error"] == "unauthorized_peer"


def test_overflow_reply_survives_a_deadline_consuming_read(tmp_path, monkeypatch):
    data = _early_return_client_reply(
        tmp_path, monkeypatch, peer_ok=True, read_result=(None, True)
    )
    assert data, "overflowing client received a bare close instead of a JSON error"
    response = json.loads(data.decode("utf-8"))
    assert response["ok"] is False
    assert response["error"] == "request_too_large"


# ---------------------------------------------------------------------------
# FIVE (LOW), rework: a write to an already-closed child stdin also returns a
# JSON error, not a bare close.
#
# The child process is still alive (``poll()`` returns None), but its stdin
# descriptor has been closed -- exactly what ``_shutdown_child_stdin`` does from
# ``_pump_extension_to_child``'s finally on extension EOF while the child runs
# on. The unbuffered ``io.FileIO`` then raises ValueError("I/O operation on
# closed file"), not OSError. Before the fix that ValueError slipped past the
# owner's ``except OSError`` and ``_handle_sideband_conn``'s ``except
# (OSError, socket.timeout)``, so the finally closed the connection bare -- the
# same illegibility Finding FIVE exists to remove. ``_write_to_child`` now
# normalises it to an OSError-family failure at the boundary, so the client
# still gets the ``not_ready`` JSON error and the pending record is cleared.
# ---------------------------------------------------------------------------

class _ChildWithClosedStdin:
    """A live child (``poll()`` is None) whose stdin descriptor is closed."""

    def __init__(self, stdin):
        self.stdin = stdin

    def poll(self):
        return None


def test_closed_child_stdin_dispatch_raises_not_ready(tmp_path):
    import io

    mux = _bare_mux(tmp_path)
    stdin = io.FileIO(str(tmp_path / "child_stdin"), "wb")  # unbuffered, like prod
    stdin.close()
    assert stdin.closed
    mux._child = _ChildWithClosedStdin(stdin)
    mux._child_epoch = "epoch-closed-stdin"
    mux._ready_event.set()

    with pytest.raises(app_server_mux.SidebandNotReady) as exc_info:
        mux.dispatch_sideband("thread/resume", {"threadId": "t"})

    assert str(exc_info.value).startswith("child_write_failed")
    assert mux._pending_by_id == {}  # the closed write leaked no pending flight


def test_closed_child_stdin_returns_json_error_to_client_not_bare_close(tmp_path):
    import io

    mux = _bare_mux(tmp_path)
    stdin = io.FileIO(str(tmp_path / "child_stdin"), "wb")
    stdin.close()
    assert stdin.closed
    mux._child = _ChildWithClosedStdin(stdin)
    mux._child_epoch = "epoch-closed-stdin-e2e"
    mux._ready_event.set()
    mux._capability_token = "cap-token-for-test"

    srv_conn, client = socket.socketpair()
    try:
        thread = threading.Thread(target=mux._handle_sideband_conn, args=(srv_conn,))
        thread.start()
        request = {"cap": "cap-token-for-test", "method": "thread/resume", "params": {"threadId": "t"}}
        client.sendall((json.dumps(request) + "\n").encode("utf-8"))
        client.shutdown(socket.SHUT_WR)

        client.settimeout(5)
        data = b""
        while b"\n" not in data:
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert data, "client received a bare close instead of a JSON error"
        response = json.loads(data.decode("utf-8"))
        assert response["ok"] is False
        assert response["error"] == "not_ready"
        assert response["detail"].startswith("child_write_failed")
    finally:
        with contextlib.suppress(OSError):
            srv_conn.close()
        client.close()


