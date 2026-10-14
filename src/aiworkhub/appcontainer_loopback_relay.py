"""Port-scoped loopback relay for one AppContainer worker (NF-2026-01503).

A contained worker cannot reach a model provider on the host's loopback.
Measured 2026-10-13: internetClient reaches public hosts, but host loopback
times out, also with privateNetworkClientServer, while loopback between two
processes of the SAME container works.  ``CheckNetIsolation LoopbackExempt``
would open every host loopback port -- the owner's unauthenticated ``opencode
serve`` among them -- so it is forbidden, as is any new capability SID.  When
the owner's custom provider ``baseURL`` is plain-http loopback, three pieces
stand in for it instead:

* the launcher picks a free host loopback port P, rewrites only the projected
  ``baseURL`` to ``http://127.0.0.1:P/<same path>`` and records the ORIGINAL
  host:port as the relay target (:class:`LoopbackRelayPlan`).  The target
  comes only from the owner's provider config read on the host, never from
  anything the container can write;
* inside the container the worker's MCP pipe shim also listens on 127.0.0.1:P
  and joins each accepted connection to a new client of the relay pipe, to
  which it first names the connection's TCP client port
  (``windows_appcontainer.worker_pipe_shim_argv``);
* on the host :class:`AppContainerLoopbackRelay` serves that pipe, joining
  each connection it admits to a fresh connection to the recorded target and
  to nothing else.

The guarantee is that only the worker's own job is served.  Every concurrent
worker of one repo and adapter shares the container SID, so neither the SID
nor a name keeps a sibling out; the job does.  A relay pipe client outside the
worker's job is disconnected unread, and so is a connection to P whose TCP
client -- found in the host's TCP table by the port the shim names -- is
outside it, so a sibling cannot use this worker's shim.  The relay pipe's
DACL lets the container read and write it but never add an instance, so a
sibling cannot stand in for the host's end either.  And P is watched: a
listener on it outside the worker's job -- one that took P after the launcher
released it -- terminates the worker's job within one poll
(``_WATCH_SECONDS``).  The residual is that window: a request the worker
sends to P before the poll finds the squatter can still reach it.
"""

from __future__ import annotations

import ctypes
import ipaddress
import socket
import struct
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Callable, Mapping

try:
    from . import windows_appcontainer
except ImportError:  # direct-script entrypoint
    import windows_appcontainer  # type: ignore[no-redef]

__all__ = [
    "BRIDGE_SPEC_KEY",
    "DIAL_TIMEOUT_SECONDS",
    "MAX_RELAY_CONNECTIONS",
    "AppContainerLoopbackRelay",
    "LoopbackRelayPlan",
    "LoopbackRelaySpec",
    "free_loopback_port",
    "listener_pids",
    "relay_for_bridge",
    "relay_target",
    "tcp_client_pid",
    "validated_relay_spec",
]

# The worker MCP bridge spec key that carries one relay.
BRIDGE_SPEC_KEY = "loopback_relay"
# Connections served at once; a client beyond them is disconnected unserved.
MAX_RELAY_CONNECTIONS = 16
# Only the dial is timed.  An established connection never is: a model
# response streams for as long as the provider takes to produce it.
DIAL_TIMEOUT_SECONDS = 5.0
# Every live connection's instance, the listening one and one being refused.
_PIPE_INSTANCES = MAX_RELAY_CONNECTIONS + 2
# The relay pipe's container ACE: read and write data, never an instance of
# its own (windows_appcontainer._CONTAINER_PIPE_ACCESS).
_RELAY_PIPE_ACCESS = "0x12008b"
# The shim names each connection's TCP client port first, in this many bytes.
_HEADER_BYTES = 2
# How often the watchdog lists the listeners on P.
_WATCH_SECONDS = 0.25
_COPY_BYTES = 65536
_CLOSE_GRACE_SECONDS = 5.0
_SPEC_KEYS = frozenset(("pipe", "port", "target_host", "target_port"))
# GetExtendedTcpTable, IPv4 only: the shim listens on 127.0.0.1, and only a
# listener on 127.0.0.1:P itself keeps it off P.  Measured 2026-10-14: its
# exclusive bind succeeds beside 0.0.0.0:P and beside [::]:P, dual-mode or
# not, and then takes their connections to 127.0.0.1:P.
_AF_INET = 2
_TCP_TABLE_OWNER_PID_LISTENER = 3
_TCP_TABLE_OWNER_PID_CONNECTIONS = 4
_ERROR_INSUFFICIENT_BUFFER = 122
_TABLE_ATTEMPTS = 8
# MIB_TCPROW_OWNER_PID: the state, then each address and port as the stack
# keeps them -- network byte order, a port in the first two of its four
# bytes -- then the owning pid.
_TCP_ROW = struct.Struct("<I4s4s4s4sI")
_LOOPBACK = socket.inet_aton("127.0.0.1")
_ANY = socket.inet_aton("0.0.0.0")


def _port(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError(f"loopback_relay_port_invalid:{value!r}")
    return value


def _dialable_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
    return mapped is None and address.is_loopback and str(address) == host


def relay_target(host: object, port: object) -> tuple[str, int]:
    """``(host, port)`` when the host relay may dial it, else ValueError.

    The host is ``localhost``, which the host resolves to loopback itself, or a
    loopback address literal in its canonical spelling (``127.0.0.1``,
    ``::1``): never another name a resolver could send off this machine, never
    an unspecified address.  The port is 1-65535.
    """
    if not isinstance(host, str) or not _dialable_loopback(host):
        raise ValueError(f"loopback_relay_target_not_loopback:{host!r}")
    return host, _port(port)


def free_loopback_port() -> int:
    """A host loopback port no socket holds at this moment: P.

    It is released at once, so another process can take it before the shim
    listens.  The shim's exclusive listener then fails, and the worker's
    requests to P would reach whoever holds it -- so the relay's watchdog
    terminates the worker's job at its first poll that finds a listener on P
    outside that job, at most ``_WATCH_SECONDS`` after the relay starts or
    the listener appears.  That window is the residual: a request the worker
    sends to P inside it can still reach the squatter.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass(frozen=True)
class LoopbackRelaySpec:
    """One relay, as the worker MCP bridge spec carries it."""

    pipe: str  # the relay pipe: a worker pipe name, never the MCP pipe
    port: int  # P, where the contained shim listens
    target_host: str  # the owner's original loopback host and port
    target_port: int

    def as_bridge(self) -> dict[str, object]:
        return {
            "pipe": self.pipe,
            "port": self.port,
            "target_host": self.target_host,
            "target_port": self.target_port,
        }


class LoopbackRelayPlan:
    """The launcher's half of one worker's relay.

    ``opencode_auth.project_opencode_custom_provider`` calls it with the
    original host and port of the owner's loopback ``baseURL`` and rewrites
    the URL to the P it returns; the first call names the relay pipe and picks
    P.  :meth:`seal` hands the relay to the MCP bridge.  The launch projects
    twice, and a later projection may only repeat the recorded target -- a
    loopback provider first seen after the bridge was built is refused -- so
    the shim and the worker config can never disagree about the relay.
    """

    def __init__(
        self, request_id: str, *, pick_port: Callable[[], int] = free_loopback_port
    ) -> None:
        self._request_id = request_id
        self._pick_port = pick_port
        self._spec: LoopbackRelaySpec | None = None
        self._sealed = False

    def __call__(self, host: str, port: int) -> int:
        target_host, target_port = relay_target(host, port)
        if self._spec is None:
            if self._sealed:
                raise ValueError("loopback_relay_not_in_the_bridge")
            self._spec = LoopbackRelaySpec(
                windows_appcontainer.new_worker_pipe_name(self._request_id),
                _port(self._pick_port()),
                target_host,
                target_port,
            )
        elif (self._spec.target_host, self._spec.target_port) != (target_host, target_port):
            raise ValueError("loopback_relay_target_changed")
        return self._spec.port

    def seal(self) -> LoopbackRelaySpec | None:
        """The relay the bridge carries; None when no loopback provider needs one."""
        self._sealed = True
        return self._spec


def validated_relay_spec(bridge: Mapping[str, Any]) -> LoopbackRelaySpec | None:
    """The relay a worker MCP bridge spec carries, checked before the worker
    launches; None when it carries none.

    The relay pipe is a worker pipe name other than the MCP pipe, P and the
    target port are 1-65535 and the target host is loopback
    (:func:`relay_target`).  Anything else raises ValueError.
    """
    value = bridge.get(BRIDGE_SPEC_KEY)
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != _SPEC_KEYS:
        raise ValueError("invalid_loopback_relay")
    pipe = value["pipe"]
    if (
        not isinstance(pipe, str)
        or not windows_appcontainer._WORKER_PIPE_NAME.match(pipe)
        or pipe == bridge.get("pipe")
    ):
        raise ValueError(f"invalid_loopback_relay_pipe:{pipe!r}")
    target_host, target_port = relay_target(value["target_host"], value["target_port"])
    return LoopbackRelaySpec(pipe, _port(value["port"]), target_host, target_port)


def relay_for_bridge(
    bridge: Mapping[str, Any], container_sid: str
) -> AppContainerLoopbackRelay | None:
    """The host relay a bridge spec asks for, validated and with its pipe
    already created; None when it asks for none."""
    spec = validated_relay_spec(bridge)
    return None if spec is None else AppContainerLoopbackRelay(spec, container_sid)


def _relay_pipe_instance(name: str, container_sid: str, first: bool) -> Any:
    return windows_appcontainer.WorkerPipe(
        name,
        container_sid,
        max_instances=_PIPE_INSTANCES,
        first=first,
        container_access=_RELAY_PIPE_ACCESS,
    )


def _tcp_rows(table_class: int) -> list[tuple[bytes, int, bytes, int, int]]:
    """This host's IPv4 TCP table of ``table_class``, one row per endpoint:
    local address and port, remote address and port, owning pid."""
    get_table = windows_appcontainer._load_windows_dll("iphlpapi").GetExtendedTcpTable
    get_table.restype = wintypes.DWORD
    get_table.argtypes = [
        wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
        wintypes.ULONG, ctypes.c_int, wintypes.ULONG,
    ]
    size = wintypes.DWORD(0)
    for _attempt in range(_TABLE_ATTEMPTS):  # the table may grow between two calls
        buffer = ctypes.create_string_buffer(max(size.value, 4))
        size.value = len(buffer)
        status = get_table(buffer, ctypes.byref(size), False, _AF_INET, table_class, 0)
        if status == _ERROR_INSUFFICIENT_BUFFER:
            continue
        if status != 0:
            raise OSError(f"loopback_relay_tcp_table_failed:{status}")
        raw = buffer.raw
        end = 4 + struct.unpack_from("<I", raw)[0] * _TCP_ROW.size
        if end > len(raw):
            raise OSError("loopback_relay_tcp_table_truncated")
        return [
            (local, int.from_bytes(lport[:2], "big"), remote, int.from_bytes(rport[:2], "big"), pid)
            for _state, local, lport, remote, rport, pid in _TCP_ROW.iter_unpack(raw[4:end])
        ]
    raise OSError("loopback_relay_tcp_table_kept_growing")


def tcp_client_pid(local_port: int, remote_port: int) -> int | None:
    """The pid owning this host's 127.0.0.1:``local_port`` ->
    127.0.0.1:``remote_port`` TCP connection; None when no live process owns
    one."""
    wanted = (_LOOPBACK, local_port, _LOOPBACK, remote_port)
    for local, lport, remote, rport, pid in _tcp_rows(_TCP_TABLE_OWNER_PID_CONNECTIONS):
        if pid and (local, lport, remote, rport) == wanted:
            return pid
    return None


def listener_pids(port: int) -> list[int]:
    """The pid of every listener on 127.0.0.1:``port`` or 0.0.0.0:``port``."""
    return [
        pid
        for local, lport, _remote, _rport, pid in _tcp_rows(_TCP_TABLE_OWNER_PID_LISTENER)
        if lport == port and local in (_LOOPBACK, _ANY)
    ]


def _terminate_job(job: Any) -> None:
    windows_appcontainer._load_win32().terminate_job(job, 1)


class AppContainerLoopbackRelay:
    """Host end of one worker's loopback relay (see the module note).

    A multi-instance relay pipe -- DACL from ``worker_pipe_sddl``, granting
    the container read and write data only, local clients only -- whose every
    instance serves one client process inside the worker's job and
    disconnects anyone else: ``WorkerPipe.accept``, the MCP pipe's own check.
    A served client names its TCP client's port first, and the connection is
    relayed only when ``client_pid`` finds 127.0.0.1:port -> 127.0.0.1:P
    owned by a process ``in_job`` places in the worker's job; otherwise it is
    refused, its pipe closed and nothing dialed.  Each relayed connection
    gets a fresh connection to the recorded target and to nothing else,
    whatever it sends; the two are copied both ways until either side ends or
    fails, then both are closed.  At most ``MAX_RELAY_CONNECTIONS`` are live,
    and a client beyond them is disconnected unserved.  From :meth:`start`
    until :meth:`close` a watchdog lists the listeners on P
    (``listener_pids``) every ``_WATCH_SECONDS`` and ends the worker's job
    (``terminate_job``) at the first one outside it.  The name is held from
    construction -- the first instance is ``FILE_FLAG_FIRST_PIPE_INSTANCE``,
    created before the worker exists -- until :meth:`close`, because the next
    listening instance is created before a connected one can end.
    """

    def __init__(
        self,
        spec: LoopbackRelaySpec,
        container_sid: str,
        *,
        pipe_instance: Callable[[str, str, bool], Any] = _relay_pipe_instance,
        connect: Callable[..., Any] = socket.create_connection,
        client_pid: Callable[[int, int], int | None] = tcp_client_pid,
        in_job: Callable[[int, Any], bool] = windows_appcontainer.process_in_job,
        listener_pids: Callable[[int], list[int]] = listener_pids,
        terminate_job: Callable[[Any], None] = _terminate_job,
    ) -> None:
        self.spec = spec
        self._sid = container_sid
        self._pipe_instance = pipe_instance
        self._connect = connect
        self._client_pid = client_pid
        self._in_job = in_job
        self._listener_pids = listener_pids
        self._terminate_job = terminate_job
        self._lock = threading.Lock()
        self._closed = False
        self._stopping = threading.Event()
        self._job: Any = None
        self._listening: Any = pipe_instance(spec.pipe, container_sid, True)
        self._links: set[_Link] = set()
        self._accepting: threading.Thread | None = None
        self._watching: threading.Thread | None = None
        self.served = 0  # connections relayed
        self.refused = 0  # connections disconnected unserved: the cap, the job check
        self.error = ""

    @property
    def live(self) -> int:
        """Connections being served right now."""
        with self._lock:
            return len(self._links)

    def start(self, job: Any) -> None:
        """Serve the clients of ``job``, the worker's own job object, and
        watch P for a listener outside it."""
        self._job = job
        self._accepting = threading.Thread(target=self._accept_loop, daemon=True)
        self._watching = threading.Thread(target=self._watch, daemon=True)
        self._accepting.start()
        self._watching.start()

    def _accept_loop(self) -> None:
        connected: Any = None
        try:
            while self._listening.accept(self._job):
                connected = self._listening
                with self._lock:
                    if self._closed:
                        return
                    self._listening = self._pipe_instance(self.spec.pipe, self._sid, False)
                    if len(self._links) < MAX_RELAY_CONNECTIONS:
                        link = _Link(self, connected)  # served once _admit passes it
                        self._links.add(link)
                        link.start()
                        connected = None
                    else:
                        self.refused += 1
                if connected is not None:
                    connected.close()  # beyond the cap: disconnected unserved
                    connected = None
        except Exception as exc:
            self._note(exc)
        finally:
            if connected is not None:
                connected.close()

    def _dial(self) -> Any:
        """A new connection to the recorded target: the only address dialed."""
        return self._connect(
            (self.spec.target_host, self.spec.target_port), timeout=DIAL_TIMEOUT_SECONDS
        )

    def _admit(self, header: bytes) -> bool:
        """Whether the connection a served client names in ``header`` -- its
        TCP client's port, two bytes big endian -- is owned by a process of
        the worker's job.  Counted as served when it is; as refused when it
        is not, when the header is short, or when the lookup fails."""
        admitted = False
        if len(header) == _HEADER_BYTES:
            try:
                pid = self._client_pid(int.from_bytes(header, "big"), self.spec.port)
                admitted = pid is not None and self._in_job(pid, self._job)
            except Exception as exc:
                self._note(exc)
        with self._lock:
            if admitted:
                self.served += 1
            else:
                self.refused += 1
        return admitted

    def _foreign_listeners(self) -> list[int]:
        return [
            pid for pid in self._listener_pids(self.spec.port) if not self._in_job(pid, self._job)
        ]

    def _watch(self) -> None:
        """Until close, every ``_WATCH_SECONDS``: a listener on P outside the
        worker's job -- P taken after the launcher released it -- could
        receive the worker's requests, so it ends the worker's job.  So does
        a TCP table that cannot be read: unknown is never safe."""
        try:
            while not self._stopping.is_set():
                # Read twice before acting, so a listener of the job that
                # exits between a table read and its job check -- the shim,
                # as the worker ends -- is not taken for a squatter.
                if self._foreign_listeners():
                    foreign = self._foreign_listeners()
                    if foreign:
                        self._end_worker(f"loopback_relay_port_squatted:{foreign[0]}")
                        return
                self._stopping.wait(_WATCH_SECONDS)
        except Exception as exc:
            self._end_worker(f"loopback_relay_watch_failed:{type(exc).__name__}:{exc}")

    def _end_worker(self, reason: str) -> None:
        """Note ``reason`` over any earlier note, since it is why the worker
        ended, then terminate the worker's job -- unless already closing."""
        if self._stopping.is_set():
            return
        self._note(reason, overriding=True)
        try:
            self._terminate_job(self._job)
        except Exception as exc:
            self._note(f"{reason};terminate_job_failed:{type(exc).__name__}:{exc}", overriding=True)

    def _forget(self, link: _Link) -> None:
        with self._lock:
            self._links.discard(link)

    def _note(self, failure: BaseException | str, *, overriding: bool = False) -> None:
        text = failure if isinstance(failure, str) else f"{type(failure).__name__}:{failure}"
        with self._lock:  # never held by a caller: links and the watchdog note concurrently
            self.error = text[:500] if overriding else self.error or text[:500]

    def close(self) -> None:
        """Stop accepting and watching, close every live connection and the
        pipe.  Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            listening, links = self._listening, list(self._links)
        self._stopping.set()
        listening.shutdown()  # a waiting accept returns at once
        for link in links:
            link.close()
        deadline = time.monotonic() + _CLOSE_GRACE_SECONDS
        threads = [thread for link in links for thread in list(link.threads)]
        threads += [thread for thread in (self._accepting, self._watching) if thread is not None]
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        if not listening.close():
            self._note("loopback_relay_pipe_still_in_use_at_close")


class _Link:
    """One served client of the relay pipe, joined to its own target connection."""

    def __init__(self, relay: AppContainerLoopbackRelay, pipe: Any) -> None:
        self._relay = relay
        self._pipe = pipe
        self._sock: Any = None
        self._lock = threading.Lock()
        self._closed = False
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        self._spawn(self._client_to_target)

    def _spawn(self, target: Callable[[], None]) -> None:
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.threads.append(thread)

    def _client_to_target(self) -> None:
        try:
            # First the TCP client port the shim names: the connection is
            # relayed only if a process of the worker's own job owns it, and
            # is otherwise closed with nothing dialed.  Payload read along
            # with the header is held and sent first; it cannot choose where
            # the connection goes, which is the recorded target whatever it
            # says.
            received = b""
            while len(received) < _HEADER_BYTES and (chunk := self._pipe.read()):
                received += chunk
            if self._closed or not self._relay._admit(received[:_HEADER_BYTES]):
                return
            sock = self._relay._dial()
            with self._lock:
                if self._closed:
                    sock.close()
                    return
                sock.settimeout(None)
                self._sock = sock
                self._spawn(self._target_to_client)
            if received[_HEADER_BYTES:]:
                sock.sendall(received[_HEADER_BYTES:])
            while chunk := self._pipe.read():
                sock.sendall(chunk)
        except Exception as exc:
            self._fail(exc)
        finally:
            self.close()

    def _target_to_client(self) -> None:
        try:
            while chunk := self._sock.recv(_COPY_BYTES):
                self._pipe.write(chunk)
        except Exception as exc:
            self._fail(exc)
        finally:
            self.close()

    def _fail(self, exc: BaseException) -> None:
        if not self._closed:  # not the other direction's close, seen from here
            self._relay._note(exc)

    def close(self) -> None:
        """Close both directions once: the target connection and the pipe instance."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if not self._pipe.close():
            self._relay._note("loopback_relay_pipe_still_in_use_at_close")
        self._relay._forget(self)
