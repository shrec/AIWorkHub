"""NF-2026-01503: the host end of an AppContainer worker's loopback relay.

The fakes stand in for the Win32 pipe primitives, the worker's job, the host's
TCP table and the target socket only.  Every relay pipe instance below is a
``WorkerPipe`` subclass, so ``accept`` and ``_client_in_job`` -- the MCP pipe's
own job check -- run exactly as in production, against a fake kernel32.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import http.server
import itertools
import json
import os
import queue
import socket
import struct
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from aiworkhub import appcontainer_loopback_relay as relay_module
from aiworkhub import windows_appcontainer as wac

_SID = "S-1-15-2-2390138238-917833039-2980490063-148298555-1665516221-1143707983-1249757738"
_OWNER = "S-1-5-21-389243392-615521012-1854199069-1001"
_TARGET = ("127.0.0.1", 20128)
_WORKER_JOB, _SIBLING_JOB = object(), object()
_NOBODY = object()  # no live process owns the connection: no TCP table row
_MCP_PIPE = "\\\\.\\pipe\\aiworkhub-worker-req-" + "a" * 32
_RELAY_PIPE = "\\\\.\\pipe\\aiworkhub-worker-req-" + "b" * 32


def _wait_until(predicate, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


class _Client:
    """One process connecting to the relay pipe, the job it runs in, and the
    port of the TCP client whose connection it carries."""

    def __init__(self, job: object, pid: int, sends: tuple[bytes, ...]) -> None:
        self.job, self.pid = job, pid
        self.port = 0
        self.outbound: queue.Queue[bytes] = queue.Queue()
        for chunk in sends:
            self.outbound.put(chunk)
        self.received = bytearray()
        self.dropped = threading.Event()  # its server end disconnected or closed

    def hang_up(self) -> None:
        self.outbound.put(b"")


class _Kernel:
    """The kernel32 calls ``WorkerPipe.accept`` and ``_client_in_job`` make."""

    def __init__(self, instance: _Instance) -> None:
        self._instance = instance

    def GetNamedPipeClientProcessId(self, _handle, pid) -> bool:
        pid._obj.value = self._instance.client.pid
        return True

    def OpenProcess(self, _access, _inherit, pid) -> int:
        return pid

    def IsProcessInJob(self, process, job, inside) -> bool:
        inside._obj.value = self._instance.hub.jobs[process] is job
        return True

    def CloseHandle(self, _handle) -> bool:
        return True

    def DisconnectNamedPipe(self, _handle) -> bool:
        client = self._instance.client
        self._instance.disconnected.append(client)
        client.dropped.set()
        self._instance.client = None
        return True


class _Instance(wac.WorkerPipe):
    """One relay pipe instance: the real accept and job check, fake I/O."""

    def __init__(self, hub: _Hub, name: str, sid: str, first: bool) -> None:
        self.hub, self.name, self.sid, self.first = hub, name, sid, first
        self._handle = object()
        self._k = _Kernel(self)
        self.client: _Client | None = None
        self.disconnected: list[_Client] = []
        self.shut = threading.Event()
        self.closed = 0

    def _io(self, start, operation, deadline=None) -> int:
        assert operation == "connect_worker_pipe"  # read and write are faked below
        while not self.shut.is_set():
            try:
                self.client = self.hub.connecting.get(timeout=0.01)
            except queue.Empty:
                continue
            return 0
        return -1

    def read(self) -> bytes:
        while not self.shut.is_set():
            try:
                return self.client.outbound.get(timeout=0.01)
            except queue.Empty:
                continue
        return b""

    def write(self, data: bytes) -> None:
        if self.shut.is_set():
            raise BrokenPipeError(self.name)
        self.client.received += data

    def shutdown(self) -> None:
        self.shut.set()

    def close(self, timeout: float = 5.0) -> bool:
        self.shut.set()
        self.closed += 1
        if self.client is not None:
            self.client.dropped.set()
        return True


class _Hub:
    """Every instance the relay creates and the clients waiting to connect --
    and the host as the relay sees it: each process's job, the owner of each
    TCP client port, who listens on P, and every job terminated."""

    def __init__(self) -> None:
        self.connecting: queue.Queue[_Client] = queue.Queue()
        self.instances: list[_Instance] = []
        self.jobs: dict[int, object] = {}
        self.tcp_owners: dict[int, int] = {}
        self.lookups: list[tuple[int, int]] = []
        self.listening: list[int] = []
        self.watched: list[int] = []
        self.terminated: list[object] = []
        self._pids = itertools.count(1000)
        self._ports = itertools.count(40000)

    def instance(self, name: str, sid: str, first: bool) -> _Instance:
        self.instances.append(_Instance(self, name, sid, first))
        return self.instances[-1]

    def process(self, job: object) -> int:
        pid = next(self._pids)
        self.jobs[pid] = job
        return pid

    def connect(
        self,
        job: object,
        *sends: bytes,
        tcp_job: object = None,
        port: int | None = None,
        header: bool = True,
    ) -> _Client:
        """A shim of ``job`` carrying the connection of a TCP client on
        ``port``, owned by a process of ``tcp_job`` -- the shim's own job
        unless given, nobody for _NOBODY.  ``header`` puts that port in front
        of the first send, as the real shim does."""
        port = next(self._ports) if port is None else port
        tcp_job = job if tcp_job is None else tcp_job
        if tcp_job is not _NOBODY:
            self.tcp_owners[port] = self.process(tcp_job)
        if header:
            sends = (port.to_bytes(2, "big") + (sends[0] if sends else b""), *sends[1:])
        client = _Client(job, self.process(job), sends)
        client.port = port
        self.connecting.put(client)
        return client

    # In place of the host's TCP table, kernel32's job check and the job's end.

    def client_pid(self, local_port: int, remote_port: int) -> int | None:
        self.lookups.append((local_port, remote_port))
        return self.tcp_owners.get(local_port)

    def in_job(self, pid: int, job: object) -> bool:
        return self.jobs.get(pid) is job

    def listener_pids(self, port: int) -> list[int]:
        self.watched.append(port)
        return list(self.listening)

    def terminate_job(self, job: object) -> None:
        self.terminated.append(job)


class _Socket:
    """The target connection: the dial, what was sent, and the close."""

    def __init__(self, address, timeout) -> None:
        self.address, self.dial_timeout, self.timeout = address, timeout, timeout
        self.sent = bytearray()
        self.replies: queue.Queue[bytes] = queue.Queue()
        self.shut: list[int] = []
        self.closed = threading.Event()

    def settimeout(self, value) -> None:
        self.timeout = value

    def sendall(self, data: bytes) -> None:
        if self.closed.is_set():
            raise OSError("closed")
        self.sent += data

    def recv(self, _size: int) -> bytes:
        while not self.closed.is_set():
            try:
                return self.replies.get(timeout=0.01)
            except queue.Empty:
                continue
        raise OSError("closed")

    def shutdown(self, how: int) -> None:
        self.shut.append(how)

    def close(self) -> None:
        self.closed.set()


class _Dialer:
    def __init__(self, refuse: bool) -> None:
        self.sockets: list[_Socket] = []
        self._refuse = refuse

    def __call__(self, address, timeout=None) -> _Socket:
        if self._refuse:
            raise ConnectionRefusedError(10061, "refused")
        self.sockets.append(_Socket(address, timeout))
        return self.sockets[-1]


def _relay(*, refuse: bool = False, listener_pids=None, terminate_job=None):
    """A relay over the fakes: the hub's host lookups unless replaced."""
    hub, dialer = _Hub(), _Dialer(refuse)
    spec = relay_module.LoopbackRelaySpec(_RELAY_PIPE, 50123, *_TARGET)
    relay = relay_module.AppContainerLoopbackRelay(
        spec,
        _SID,
        pipe_instance=hub.instance,
        connect=dialer,
        client_pid=hub.client_pid,
        in_job=hub.in_job,
        listener_pids=listener_pids or hub.listener_pids,
        terminate_job=terminate_job or hub.terminate_job,
    )
    return relay, hub, dialer


def test_the_first_instance_claims_the_name_before_anything_is_served() -> None:
    relay, hub, _ = _relay()
    (first,) = hub.instances
    assert (first.name, first.sid, first.first) == (_RELAY_PIPE, _SID, True)
    relay.start(_WORKER_JOB)
    hub.connect(_WORKER_JOB, b"x")
    _wait_until(lambda: len(hub.instances) == 2)
    # The next listening instance exists before the served one can end, and
    # only the first ever claims the name.
    assert [instance.first for instance in hub.instances] == [True, False]
    assert {instance.name for instance in hub.instances} == {_RELAY_PIPE}
    relay.close()


def test_a_client_outside_the_workers_job_is_disconnected_and_never_dialed() -> None:
    relay, hub, dialer = _relay()
    stranger = hub.connect(_SIBLING_JOB, b"GET /v1/models HTTP/1.1\r\n\r\n")
    hub.connect(_WORKER_JOB, b"POST /v1/chat/completions HTTP/1.1\r\n\r\n")
    relay.start(_WORKER_JOB)
    _wait_until(lambda: len(dialer.sockets) == 1 and bool(dialer.sockets[0].sent))

    assert hub.instances[0].disconnected == [stranger]
    assert stranger.dropped.is_set() and stranger.received == b""
    assert stranger.outbound.qsize() == 1  # not one byte of it was read
    assert bytes(dialer.sockets[0].sent) == b"POST /v1/chat/completions HTTP/1.1\r\n\r\n"
    assert (relay.served, relay.refused) == (1, 0)
    relay.close()


@pytest.mark.parametrize(
    "sent",
    [
        b"CONNECT evil.example:443 HTTP/1.1\r\nHost: evil.example:443\r\n\r\n",
        b"GET http://10.0.0.5:8080/ HTTP/1.1\r\nHost: 10.0.0.5:8080\r\n\r\n",
        b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03",  # a TLS ClientHello
    ],
    ids=["connect-elsewhere", "absolute-url", "tls"],
)
def test_every_client_is_dialed_to_the_recorded_target_whatever_it_sends(sent: bytes) -> None:
    relay, hub, dialer = _relay()
    relay.start(_WORKER_JOB)
    client = hub.connect(_WORKER_JOB, sent)
    _wait_until(lambda: bool(dialer.sockets) and bytes(dialer.sockets[0].sent) == sent)

    (sock,) = dialer.sockets
    assert sock.address == _TARGET
    assert sock.dial_timeout == relay_module.DIAL_TIMEOUT_SECONDS
    assert sock.timeout is None  # an established connection is never timed
    sock.replies.put(b"HTTP/1.1 200 OK\r\n\r\n")
    _wait_until(lambda: bytes(client.received) == b"HTTP/1.1 200 OK\r\n\r\n")
    relay.close()


@pytest.mark.parametrize("side", ["client", "target"])
def test_eof_from_either_side_closes_both_directions(side: str) -> None:
    relay, hub, dialer = _relay()
    relay.start(_WORKER_JOB)
    client = hub.connect(_WORKER_JOB, b"ping")
    _wait_until(lambda: bool(dialer.sockets) and bytes(dialer.sockets[0].sent) == b"ping")
    sock, served = dialer.sockets[0], hub.instances[0]

    if side == "client":
        client.hang_up()
    else:
        sock.replies.put(b"")
    _wait_until(lambda: sock.closed.is_set() and served.closed >= 1)
    assert sock.shut == [socket.SHUT_RDWR]
    assert client.dropped.is_set()
    _wait_until(lambda: relay.live == 0)
    relay.close()
    assert relay.error == ""


def test_the_connection_cap_refuses_one_more_until_a_slot_frees() -> None:
    cap = relay_module.MAX_RELAY_CONNECTIONS
    assert cap == 16
    relay, hub, dialer = _relay()
    relay.start(_WORKER_JOB)
    clients = [hub.connect(_WORKER_JOB) for _ in range(cap)]
    _wait_until(lambda: len(dialer.sockets) == cap)
    assert relay.live == cap

    over = hub.connect(_WORKER_JOB, b"one too many")
    _wait_until(lambda: relay.refused == 1)
    assert over.dropped.is_set() and over.outbound.qsize() == 1
    assert len(dialer.sockets) == cap

    clients[0].hang_up()
    _wait_until(lambda: relay.live == cap - 1)
    hub.connect(_WORKER_JOB, b"after")
    _wait_until(lambda: len(dialer.sockets) == cap + 1)
    _wait_until(lambda: bytes(dialer.sockets[-1].sent) == b"after")
    assert (relay.served, relay.refused) == (cap + 1, 1)
    relay.close()


def test_close_is_idempotent_and_ends_every_connection_and_the_pipe() -> None:
    relay, hub, dialer = _relay()
    relay.start(_WORKER_JOB)
    clients = [hub.connect(_WORKER_JOB, b"x") for _ in range(3)]
    _wait_until(lambda: len(dialer.sockets) == 3 and all(s.sent for s in dialer.sockets))

    relay.close()
    assert all(sock.closed.is_set() for sock in dialer.sockets)
    assert all(client.dropped.is_set() for client in clients)
    assert all(instance.shut.is_set() and instance.closed for instance in hub.instances)
    assert relay.live == 0 and relay.error == ""
    assert relay._accepting is not None and not relay._accepting.is_alive()
    closes = [instance.closed for instance in hub.instances]
    relay.close()
    assert [instance.closed for instance in hub.instances] == closes


def test_a_relay_closed_before_it_starts_removes_its_pipe() -> None:
    relay, hub, dialer = _relay()
    relay.close()
    relay.close()
    (first,) = hub.instances
    assert first.shut.is_set() and first.closed == 1
    assert dialer.sockets == []


def test_a_target_that_refuses_the_dial_closes_the_client() -> None:
    relay, hub, _ = _relay(refuse=True)
    relay.start(_WORKER_JOB)
    client = hub.connect(_WORKER_JOB, b"ping")
    _wait_until(client.dropped.is_set)
    _wait_until(lambda: relay.live == 0)
    assert "ConnectionRefusedError" in relay.error
    relay.close()


# -- whose connection it is: the TCP client behind the shim -------------------


@pytest.mark.parametrize("owner", ["sibling", "nobody"])
def test_a_connection_whose_tcp_client_is_outside_the_job_is_refused_never_dialed(
    owner: str,
) -> None:
    relay, hub, dialer = _relay()
    relay.start(_WORKER_JOB)
    # The shim is the worker's own, but the TCP client it carries is a
    # same-SID sibling's process -- or no live process owns the connection.
    client = hub.connect(
        _WORKER_JOB,
        b"POST /v1/chat/completions HTTP/1.1\r\n\r\n",
        tcp_job=_SIBLING_JOB if owner == "sibling" else _NOBODY,
    )
    _wait_until(lambda: relay.refused == 1 and client.dropped.is_set())

    assert hub.lookups == [(client.port, 50123)]  # 127.0.0.1:port -> 127.0.0.1:P
    assert dialer.sockets == [] and client.received == b""
    assert relay.served == 0 and relay.error == ""
    _wait_until(lambda: relay.live == 0)
    relay.close()


def test_a_short_header_is_refused_never_looked_up_nor_dialed() -> None:
    relay, hub, dialer = _relay()
    relay.start(_WORKER_JOB)
    client = hub.connect(_WORKER_JOB, b"\x9c", header=False)  # one byte of the two
    client.hang_up()
    _wait_until(lambda: relay.refused == 1 and client.dropped.is_set())

    assert hub.lookups == [] and dialer.sockets == []
    assert relay.served == 0
    relay.close()


def test_an_in_job_client_is_relayed_byte_exact_once_its_header_is_stripped() -> None:
    relay, hub, dialer = _relay()
    relay.start(_WORKER_JOB)
    # The two header bytes arrive in two reads, the second one with payload.
    client = hub.connect(
        _WORKER_JOB,
        b"\x9c",
        b"\x41POST /v1/chat/completions HTTP/1.1\r\n",
        b"Content-Length: 2\r\n\r\n{}",
        port=0x9C41,
        header=False,
    )
    _wait_until(lambda: bool(dialer.sockets) and dialer.sockets[0].sent.endswith(b"{}"))

    assert hub.lookups == [(0x9C41, 50123)]
    (sock,) = dialer.sockets
    assert bytes(sock.sent) == b"POST /v1/chat/completions HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}"
    sock.replies.put(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    _wait_until(lambda: bytes(client.received) == b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    assert (relay.served, relay.refused) == (1, 0)
    relay.close()


def test_the_relay_pipe_lets_the_container_read_and_write_but_never_add_an_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(wac, "WorkerPipe", lambda *args, **kwargs: made.append((args, kwargs)))
    relay_module._relay_pipe_instance(_RELAY_PIPE, _SID, False)

    ((args, kwargs),) = made
    assert args == (_RELAY_PIPE, _SID)
    assert kwargs == {
        "max_instances": relay_module._PIPE_INSTANCES,
        "first": False,
        "container_access": "0x12008b",
    }
    sddl = wac.worker_pipe_sddl(_OWNER, _SID, "0x12008b")
    assert sddl == f"D:P(A;;GA;;;{_OWNER})(A;;0x12008b;;;{_SID})"
    assert "GW" not in sddl
    # FILE_GENERIC_READ | FILE_WRITE_DATA, without FILE_APPEND_DATA -- which
    # on a pipe is FILE_CREATE_PIPE_INSTANCE.
    assert 0x12008B == 0x120089 | 0x2 and not 0x12008B & 0x4


# -- the watchdog on P ---------------------------------------------------------


def test_a_listener_on_p_outside_the_job_terminates_the_worker_once() -> None:
    relay, hub, _ = _relay()
    squatter = hub.process(_SIBLING_JOB)
    hub.listening[:] = [hub.process(_WORKER_JOB), squatter]  # the shim, and a squatter
    relay.start(_WORKER_JOB)
    _wait_until(lambda: relay._watching is not None and not relay._watching.is_alive())

    assert hub.terminated == [_WORKER_JOB]
    assert relay.error == f"loopback_relay_port_squatted:{squatter}"
    assert set(hub.watched) == {50123}
    relay.close()
    assert hub.terminated == [_WORKER_JOB]


def test_a_listener_of_the_workers_own_job_never_terminates_it() -> None:
    relay, hub, _ = _relay()
    hub.listening[:] = [hub.process(_WORKER_JOB)]  # the shim on P
    relay.start(_WORKER_JOB)
    _wait_until(lambda: len(hub.watched) >= 3)
    relay.close()
    assert hub.terminated == [] and relay.error == ""


def test_close_stops_the_watchdog() -> None:
    relay, hub, _ = _relay()
    relay.start(_WORKER_JOB)
    _wait_until(lambda: bool(hub.watched))
    relay.close()
    assert relay._watching is not None and not relay._watching.is_alive()
    polls = len(hub.watched)
    hub.listening[:] = [hub.process(_SIBLING_JOB)]  # too late: nobody watches now
    time.sleep(3 * relay_module._WATCH_SECONDS)
    assert len(hub.watched) == polls and hub.terminated == []


def test_a_job_listener_gone_by_the_second_read_is_not_a_squatter() -> None:
    # The shim exiting as the worker ends: listed by one table read, already
    # unopenable -- so outside the job -- and gone from the next read.
    reads: list[int] = []

    def exiting_shim(port: int) -> list[int]:
        reads.append(port)
        return [7] if len(reads) == 1 else []

    relay, hub, _ = _relay(listener_pids=exiting_shim)
    relay.start(_WORKER_JOB)
    _wait_until(lambda: len(reads) >= 3)
    relay.close()
    assert hub.terminated == [] and relay.error == ""


def test_a_tcp_table_that_cannot_be_read_terminates_the_worker() -> None:
    def unreadable(port: int) -> list[int]:
        raise OSError("loopback_relay_tcp_table_failed:5")

    relay, hub, _ = _relay(listener_pids=unreadable)
    relay.start(_WORKER_JOB)
    _wait_until(lambda: bool(hub.terminated))
    assert hub.terminated == [_WORKER_JOB]
    assert relay.error == "loopback_relay_watch_failed:OSError:loopback_relay_tcp_table_failed:5"
    relay.close()


def test_a_termination_that_fails_is_noted_with_the_squat() -> None:
    def denied(job: object) -> None:
        raise OSError("terminate_job failed (win_error=5)")

    relay, hub, _ = _relay(terminate_job=denied)
    squatter = hub.process(_SIBLING_JOB)
    hub.listening[:] = [squatter]
    relay.start(_WORKER_JOB)
    _wait_until(lambda: relay._watching is not None and not relay._watching.is_alive())
    assert relay.error == (
        f"loopback_relay_port_squatted:{squatter};"
        "terminate_job_failed:OSError:terminate_job failed (win_error=5)"
    )
    relay.close()


def test_the_default_terminate_job_ends_the_job_with_exit_code_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ended: list[tuple[object, int]] = []
    api = types.SimpleNamespace(terminate_job=lambda job, code: ended.append((job, code)))
    monkeypatch.setattr(wac, "_load_win32", lambda: api)
    relay_module._terminate_job(_WORKER_JOB)
    assert ended == [(_WORKER_JOB, 1)]


# -- the host's TCP table -------------------------------------------------------


def _tcp_row(local: str, lport: int, remote: str, rport: int, pid: int, state: int = 5) -> bytes:
    """One MIB_TCPROW_OWNER_PID as the stack keeps it: addresses and ports in
    network byte order, each port in the first two of its four bytes and the
    other two uninitialized."""
    return (
        struct.pack("<I", state)
        + socket.inet_aton(local) + lport.to_bytes(2, "big") + b"\xde\xad"
        + socket.inet_aton(remote) + rport.to_bytes(2, "big") + b"\xbe\xef"
        + struct.pack("<I", pid)
    )


class _GetExtendedTcpTable:
    """iphlpapi's GetExtendedTcpTable over fixed rows.  ``grow`` rows appear
    once the first call is answered too small, as connections that open
    between two calls do; ``count`` overstates the rows it holds."""

    def __init__(
        self,
        rows: list[bytes],
        *,
        grow: tuple[bytes, ...] = (),
        status: int = 0,
        count: int | None = None,
    ) -> None:
        self.rows, self._grow, self._status, self._count = list(rows), list(grow), status, count
        self.calls: list[tuple[int, int]] = []

    def __call__(self, buffer, size, order, family, table_class, reserved) -> int:
        self.calls.append((family, table_class))
        if self._status:
            return self._status
        count = len(self.rows) if self._count is None else self._count
        table = struct.pack("<I", count) + b"".join(self.rows)
        if size._obj.value < len(table):
            size._obj.value = len(table)
            self.rows += self._grow
            self._grow = []
            return 122  # ERROR_INSUFFICIENT_BUFFER
        ctypes.memmove(buffer, table, len(table))
        return 0


def _host_tcp_table(monkeypatch: pytest.MonkeyPatch, table: _GetExtendedTcpTable) -> list[str]:
    loaded: list[str] = []
    dll = types.SimpleNamespace(GetExtendedTcpTable=table)
    monkeypatch.setattr(wac, "_load_windows_dll", lambda name: loaded.append(name) or dll)
    return loaded


def test_tcp_client_pid_is_the_owner_of_exactly_that_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = _GetExtendedTcpTable(
        [
            _tcp_row("127.0.0.1", 50123, "127.0.0.1", 40001, 900),  # the shim's end
            _tcp_row("127.0.0.1", 40001, "127.0.0.1", 50123, 0, state=11),  # TIME_WAIT
            _tcp_row("10.0.0.5", 40001, "127.0.0.1", 50123, 901),
            _tcp_row("127.0.0.1", 40001, "127.0.0.1", 50124, 902),
            _tcp_row("127.0.0.1", 40001, "127.0.0.1", 50123, 903),  # the client's end
        ]
    )
    loaded = _host_tcp_table(monkeypatch, table)

    assert relay_module.tcp_client_pid(40001, 50123) == 903
    assert relay_module.tcp_client_pid(40002, 50123) is None
    assert loaded == ["iphlpapi", "iphlpapi"]
    assert set(table.calls) == {(2, 4)}  # AF_INET, TCP_TABLE_OWNER_PID_CONNECTIONS


def test_listener_pids_are_who_listens_on_p_at_loopback_or_any(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = _GetExtendedTcpTable(
        [
            _tcp_row("127.0.0.1", 50123, "0.0.0.0", 0, 900, state=2),
            _tcp_row("0.0.0.0", 50123, "0.0.0.0", 0, 901, state=2),
            _tcp_row("10.0.0.5", 50123, "0.0.0.0", 0, 902, state=2),
            _tcp_row("127.0.0.1", 50124, "0.0.0.0", 0, 903, state=2),
        ],
        grow=(_tcp_row("127.0.0.1", 50123, "0.0.0.0", 0, 904, state=2),),
    )
    _host_tcp_table(monkeypatch, table)

    assert relay_module.listener_pids(50123) == [900, 901, 904]
    # AF_INET, TCP_TABLE_OWNER_PID_LISTENER: too small, grown meanwhile, read.
    assert table.calls == [(2, 3)] * 3


@pytest.mark.parametrize(
    ("table", "error"),
    [
        # Measured: inside an AppContainer the table is ERROR_ACCESS_DENIED.
        (_GetExtendedTcpTable([], status=5), "loopback_relay_tcp_table_failed:5"),
        (_GetExtendedTcpTable([], count=3), "loopback_relay_tcp_table_truncated"),
    ],
    ids=["denied", "truncated"],
)
def test_a_tcp_table_that_cannot_be_read_whole_is_an_error(
    monkeypatch: pytest.MonkeyPatch, table: _GetExtendedTcpTable, error: str
) -> None:
    _host_tcp_table(monkeypatch, table)
    with pytest.raises(OSError, match=error):
        relay_module.listener_pids(50123)


# -- what the relay may dial, and what the supervisor accepts -----------------


@pytest.mark.parametrize(
    ("host", "port"),
    [("127.0.0.1", 20128), ("127.5.6.7", 1), ("::1", 65535), ("localhost", 80)],
)
def test_relay_target_accepts_loopback_only(host: str, port: int) -> None:
    assert relay_module.relay_target(host, port) == (host, port)


@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("10.0.0.5", 80),
        ("api.example.com", 443),
        ("foo.localhost", 80),  # a name some other resolver could answer
        ("0.0.0.0", 80),
        ("::", 80),
        ("127.1", 80),  # loopback, but not the canonical spelling
        ("::ffff:127.0.0.1", 80),
        ("LOCALHOST", 80),
        (2130706433, 80),
        (None, 80),
        ("127.0.0.1", 0),
        ("127.0.0.1", 65536),
        ("127.0.0.1", True),
        ("127.0.0.1", "80"),
    ],
)
def test_relay_target_refuses_everything_else(host: object, port: object) -> None:
    with pytest.raises(ValueError):
        relay_module.relay_target(host, port)


def _bridge_spec(**relay: object) -> dict[str, object]:
    return {
        "pipe": _MCP_PIPE,
        relay_module.BRIDGE_SPEC_KEY: {
            "pipe": _RELAY_PIPE,
            "port": 50123,
            "target_host": "localhost",
            "target_port": 20128,
            **relay,
        },
    }


def test_a_bridge_without_a_relay_has_none() -> None:
    assert relay_module.validated_relay_spec({"pipe": _MCP_PIPE}) is None
    assert relay_module.relay_for_bridge({"pipe": _MCP_PIPE}, _SID) is None


def test_a_valid_relay_spec_reads_back_exactly() -> None:
    spec = relay_module.validated_relay_spec(_bridge_spec())
    assert spec == relay_module.LoopbackRelaySpec(_RELAY_PIPE, 50123, "localhost", 20128)
    assert relay_module.validated_relay_spec(
        {"pipe": _MCP_PIPE, relay_module.BRIDGE_SPEC_KEY: spec.as_bridge()}
    ) == spec


@pytest.mark.parametrize(
    "relay",
    [
        {"target_host": "10.0.0.5"},
        {"target_host": "api.example.com"},
        {"target_host": "0.0.0.0"},
        {"target_port": 0},
        {"target_port": 70000},
        {"port": 0},
        {"port": "50123"},
        {"pipe": _MCP_PIPE},  # the MCP pipe itself
        {"pipe": "\\\\.\\pipe\\someone-else"},
        {"extra": 1},
    ],
)
def test_an_invalid_relay_spec_is_refused(relay: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        relay_module.validated_relay_spec(_bridge_spec(**relay))
    with pytest.raises(ValueError):
        relay_module.relay_for_bridge(_bridge_spec(**relay), _SID)


@pytest.mark.parametrize("value", ["a-string", ["pipe"], {"pipe": _RELAY_PIPE}])
def test_a_malformed_relay_spec_is_refused(value: object) -> None:
    with pytest.raises(ValueError):
        relay_module.validated_relay_spec({"pipe": _MCP_PIPE, relay_module.BRIDGE_SPEC_KEY: value})


# -- the launcher's plan ------------------------------------------------------


def test_a_plan_nobody_calls_seals_to_no_relay() -> None:
    assert relay_module.LoopbackRelayPlan("req").seal() is None


def test_the_plan_records_the_first_target_and_answers_one_port() -> None:
    picks: list[None] = []
    plan = relay_module.LoopbackRelayPlan("req", pick_port=lambda: picks.append(None) or 50123)
    assert plan("localhost", 20128) == 50123
    assert plan("localhost", 20128) == 50123  # the launch's second projection
    assert len(picks) == 1
    spec = plan.seal()
    assert spec is not None
    assert (spec.port, spec.target_host, spec.target_port) == (50123, "localhost", 20128)
    assert spec.pipe.startswith("\\\\.\\pipe\\aiworkhub-worker-req-")
    assert plan("localhost", 20128) == 50123  # sealed: the same target only
    with pytest.raises(ValueError, match="target_changed"):
        plan("127.0.0.1", 20128)


def test_a_plan_sealed_without_a_target_refuses_a_late_one() -> None:
    plan = relay_module.LoopbackRelayPlan("req", pick_port=lambda: 50123)
    assert plan.seal() is None
    with pytest.raises(ValueError, match="not_in_the_bridge"):
        plan("localhost", 20128)


@pytest.mark.parametrize("host", ["10.0.0.5", "foo.localhost", "0.0.0.0"])
def test_a_plan_refuses_a_target_the_relay_may_not_dial(host: str) -> None:
    plan = relay_module.LoopbackRelayPlan("req", pick_port=lambda: 50123)
    with pytest.raises(ValueError):
        plan(host, 20128)
    assert plan.seal() is None


def test_free_loopback_port_is_a_port_nobody_holds() -> None:
    port = relay_module.free_loopback_port()
    assert 1 <= port <= 65535
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))


# -- live canary --------------------------------------------------------------


def _live_appcontainer_unavailable() -> str:
    if os.name != "nt":
        return "the relay joins a Windows AppContainer to its host"
    if wac.current_process_is_appcontainer():
        return "a contained test run cannot launch an AppContainer"
    probe = wac.probe()
    return "" if probe.available else f"AppContainer unavailable: {probe.detail}"


class _ChatCompletions(http.server.BaseHTTPRequestHandler):
    """An OpenAI-compatible stub that answers POST /v1/chat/completions."""

    reply = b""
    seen: list[tuple[str, bytes]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.seen.append((self.path, body))
        found = self.path == "/v1/chat/completions"
        payload = self.reply if found else b"{}"
        self.send_response(200 if found else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        pass


@contextlib.contextmanager
def _live_relay(tmp_path: Path) -> Iterator[types.SimpleNamespace]:
    """A stub provider on host loopback, the host relay to it, the worker MCP
    pipe, and ``launch`` for contained System32 programs; all torn down on
    exit."""
    unavailable = _live_appcontainer_unavailable()
    if unavailable:
        pytest.skip(unavailable)
    import msvcrt

    from aiworkhub import worker_supervisor, worker_workspace

    content = f"relay-canary-{os.urandom(8).hex()}"
    reply = {
        "id": "chatcmpl-relay-canary",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }
    stub = type("_Stub", (_ChatCompletions,), {"reply": json.dumps(reply).encode(), "seen": []})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    plan = relay_module.LoopbackRelayPlan("relaycanary")
    port = plan("127.0.0.1", server.server_address[1])
    spec = plan.seal()
    assert spec is not None
    repo_id, worker_kind = "aiworkhub-probe", "probe"
    sid = wac.container_sid(repo_id, worker_kind)
    mcp_name = wac.new_worker_pipe_name("relaycanary")
    system32 = os.path.join(os.environ["SystemRoot"], "System32")
    env = dict(os.environ)
    env.pop(worker_workspace.VALIDATION_EXEC_SCRATCH_ROOT_ENV, None)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    opened: list[int] = []
    launches: list[wac.AppContainerLaunch] = []

    def inherited(fd: int) -> int:
        opened.append(fd)
        os.set_inheritable(fd, True)
        return msvcrt.get_osfhandle(fd)

    def launch(argv: list[str], name: str, stdin: int | None = None) -> wac.AppContainerLaunch:
        launched = wac.launch_appcontainer(
            wac.AppContainerRequest(
                argv=argv,
                repo_id=repo_id,
                worker_kind=worker_kind,
                working_directory=system32,
                environment=env,
                stdin_handle=stdin,
                stdout_handle=inherited(os.open(tmp_path / f"{name}.out", flags)),
                stderr_handle=inherited(os.open(tmp_path / f"{name}.err", flags)),
                capability_sids=worker_supervisor.WORKER_NETWORK_CAPABILITIES,
                agent_shell=False,
            )
        )
        launches.append(launched)
        return launched

    stdin_read, stdin_write = os.pipe()
    shim_stdin = inherited(stdin_read)
    mcp: wac.WorkerPipe | None = None
    relay: relay_module.AppContainerLoopbackRelay | None = None
    try:
        mcp = wac.WorkerPipe(mcp_name, sid)
        relay = relay_module.AppContainerLoopbackRelay(spec, sid)
        yield types.SimpleNamespace(
            content=content,
            curl=os.path.join(system32, "curl.exe"),
            launch=launch,
            mcp=mcp,
            port=port,
            relay=relay,
            shim_argv=wac.worker_pipe_shim_argv(mcp_name, (spec.pipe, spec.port)),
            shim_stdin=shim_stdin,
            stub=stub,
        )
    finally:
        os.close(stdin_write)  # the shim's stdin ends, and the shim with it
        for launched in launches:
            launched.wait(10_000, terminate_on_timeout=True)
            launched.close()
        if relay is not None:
            relay.close()
        if mcp is not None:
            mcp.close()
        server.shutdown()
        server.server_close()
        for fd in opened:
            os.close(fd)


def _start_shim(live: types.SimpleNamespace, tmp_path: Path, argv: list[str]) -> None:
    shim = live.launch(argv, "shim", live.shim_stdin)
    # The shim connects the MCP pipe only once its listener is up.
    assert live.mcp.accept(shim.job, timeout=60), (tmp_path / "shim.err").read_text(
        errors="replace"
    )
    live.relay.start(shim.job)


def test_live_contained_curl_in_the_shims_job_reaches_a_host_loopback_stub_through_the_relay(
    tmp_path: Path,
) -> None:
    """The whole path, measured: the shim starts System32 curl.exe as its own
    child once its listener is up, so curl runs in the shim's job as OpenCode
    runs in the worker's.  curl posts to 127.0.0.1:P, the shim carries it over
    the relay pipe, the host relay finds the connection owned by a process of
    that job and delivers it to a stub on host loopback, and the reply comes
    back the same way.  curl asks for its second URL only once the first reply
    has fully arrived (--fail-early); its output lands in the shim's stdout,
    which it inherits."""
    with _live_relay(tmp_path) as live:
        url = f"http://127.0.0.1:{live.port}/v1"
        spawn = (
            f"$q=[Diagnostics.ProcessStartInfo]::new('{live.curl}','-sS --noproxy * "
            f"--max-time 30 --fail-early --data ping {url}/chat/completions {url}/models');"
            "$q.UseShellExecute=$false;[void][Diagnostics.Process]::Start($q);"
        )
        script = base64.b64decode(live.shim_argv[5]).decode("utf-16-le")
        assert script.count("$l.Start();") == 1
        script = script.replace("$l.Start();", "$l.Start();" + spawn)
        _start_shim(
            live,
            tmp_path,
            [*live.shim_argv[:5], base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
        )
        out = tmp_path / "shim.out"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not (
            len(live.stub.seen) == 2 and live.content in out.read_text(errors="replace")
        ):
            time.sleep(0.1)

        assert live.content in out.read_text(errors="replace"), (tmp_path / "shim.err").read_text(
            errors="replace"
        )
        assert live.stub.seen == [("/v1/chat/completions", b"ping"), ("/v1/models", b"ping")]
        assert live.relay.served >= 1 and live.relay.refused == 0
        assert live.relay.error == ""


def test_live_contained_curl_outside_the_shims_job_is_refused_by_the_relay(
    tmp_path: Path,
) -> None:
    """A contained System32 curl.exe in a job of its own -- as a same-SID
    sibling worker's process is -- posts to 127.0.0.1:P.  The shim, in the
    same container, carries the connection to the relay pipe, and the host
    relay finds its TCP client outside the shim's job: refused, nothing
    dialed, the stub never reached."""
    with _live_relay(tmp_path) as live:
        _start_shim(live, tmp_path, live.shim_argv)
        curl = live.launch(
            [
                live.curl, "-sS", "--noproxy", "*",
                "--max-time", "30", "-H", "Content-Type: application/json",
                "--data", '{"model":"canary","messages":[{"role":"user","content":"hi"}]}',
                f"http://127.0.0.1:{live.port}/v1/chat/completions",
            ],
            "curl",
        )
        result = curl.wait(90_000, terminate_on_timeout=True)

        # Measured inside one container: curl: (52) Empty reply from server.
        assert result.exit_code not in (None, 0), (tmp_path / "curl.err").read_text(
            errors="replace"
        )
        assert live.stub.seen == []
        assert (live.relay.served, live.relay.error) == (0, "")
        assert live.relay.refused >= 1


def test_live_contained_curl_in_the_shims_job_from_127_0_0_2_never_reaches_the_relay(
    tmp_path: Path,
) -> None:
    """The host finds a connection by 127.0.0.1 and the port the shim names,
    so a client from elsewhere in 127.0.0.0/8 -- a sibling's 127.0.0.2:X --
    could borrow a live 127.0.0.1:X connection of the worker's own.  The shim
    closes such a client before it opens the relay pipe: curl, in the shim's
    own job but bound to 127.0.0.2, gets nothing, and the relay never sees
    it."""
    with _live_relay(tmp_path) as live:
        spawn = (
            f"$q=[Diagnostics.ProcessStartInfo]::new('{live.curl}','-sS --noproxy * "
            f"--max-time 30 --interface 127.0.0.2 --data ping "
            f"http://127.0.0.1:{live.port}/v1/models');"
            "$q.UseShellExecute=$false;[void][Diagnostics.Process]::Start($q);"
        )
        script = base64.b64decode(live.shim_argv[5]).decode("utf-16-le")
        assert script.count("$l.Start();") == 1
        script = script.replace("$l.Start();", "$l.Start();" + spawn)
        _start_shim(
            live,
            tmp_path,
            [*live.shim_argv[:5], base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
        )
        err = tmp_path / "shim.err"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and "curl: (" not in err.read_text(errors="replace"):
            time.sleep(0.1)

        # curl's error lands in the shim's stderr, which it inherits.
        assert "curl: (" in err.read_text(errors="replace")
        assert live.stub.seen == []
        assert (live.relay.served, live.relay.refused, live.relay.error) == (0, 0, "")
