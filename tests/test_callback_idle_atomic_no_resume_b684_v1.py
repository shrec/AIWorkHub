"""B684: sideband callback delivery repair -- direct atomic ``turn/start``,
never ``thread/resume``, never ``turn/steer``.

Reproduces the measured B683 defect: sideband delivery against the owner
thread's actual (very large) history made ``thread/resume`` repeatedly
exhaust the mux's bounded 45-second sideband deadline -- the outbox batch
reached ``attempts>=8``, ``hard_failure_count=1``,
``last_failure_kind='busy'``/``not_ready`` while the durable event stayed
parked forever, never delivered. The fix removes the ``thread/resume``
round trip (and the ``turn/steer`` alternative it fed into) from the
sideband path entirely: ``SidebandCallbackClient`` now sends exactly one
``turn/start`` and lets the App Server's own synchronous response be the
atomic concurrency decision -- durable busy park on an active/busy/
activeTurnNotSteerable/already-in-progress rejection, bounded hard failure
only for a genuinely malformed/denied/protocol-shaped one.

A self-contained fake sideband socket endpoint stands in for
``AppServerMux`` here (never a mocked ``subprocess.run`` -- a REAL
``AF_UNIX`` socket server on a background thread, speaking the exact same
``{"cap", "method", "params"}`` -> ``{"ok", "response"}``/``{"ok",
"error"}`` wire ``app_server_mux.py`` itself serves) so each test can
script ``turn/start``/``thread/resume`` timing and error shape directly,
without depending on any fixture outside this task's ``allowed_writes``.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "AITools"))

from geoai_task_mcp import app_server_mux  # noqa: E402
from geoai_task_mcp.callback_bridge import (  # noqa: E402
    DEFAULT_MAX_RETRIES,
    AppServerError,
    BusyThreadError,
    CallbackBridge,
    SidebandCallbackClient,
    SidebandThreadBusyError,
    _is_sideband_turn_start_busy_rejection,
)

import taskdb  # noqa: E402


# ---------------------------------------------------------------------------
# Self-contained fake sideband endpoint (no AppServerMux, no subprocess)
# ---------------------------------------------------------------------------

class _FakeSidebandEndpoint:
    """A real ``AF_UNIX`` socket server registering itself as one live mux
    instance owning ``thread_id`` -- lets a test script exactly what each
    sideband method returns (and how long it takes) without spinning up a
    real ``AppServerMux`` + fake App Server subprocess pair."""

    def __init__(self, sideband_dir: Path, thread_id: str, responder):
        self.sideband_dir = sideband_dir
        self.thread_id = thread_id
        self._responder = responder
        self.calls: list[str] = []
        self._calls_lock = threading.Lock()

        app_server_mux.ensure_private_dir(sideband_dir)
        instances_dir = app_server_mux.sideband_instances_dir(sideband_dir)
        app_server_mux.ensure_private_dir(instances_dir)

        self.instance_id = uuid.uuid4().hex[:8]
        self.socket_path = sideband_dir / f"{self.instance_id}.sock"
        self.capability_path = sideband_dir / f"{self.instance_id}.cap"
        self.registry_path = instances_dir / f"{self.instance_id}.json"
        self.capability_token = "fake-cap-" + uuid.uuid4().hex

        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # Mode set at file-creation time (the ``os.open``/socket-bind-umask
        # mode argument) rather than a separate ``chmod`` call -- this
        # sandboxed test environment rejects a standalone ``chmod``
        # syscall even for a file/socket this same process just created
        # (see ``app_server_mux._write_owner_only_file``/``_bind_socket``
        # for the identical production pattern).
        fd = os.open(str(self.capability_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, self.capability_token.encode("utf-8"))
        finally:
            os.close(fd)

        pid = os.getpid()
        descriptor = {
            "instance_id": self.instance_id,
            "pid": pid,
            "pid_start_time": app_server_mux._proc_start_time(pid),
            "socket_path": str(self.socket_path),
            "capability_path": str(self.capability_path),
            "owned_thread_ids": [self.thread_id],
        }
        fd = os.open(str(self.registry_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(descriptor, ensure_ascii=False).encode("utf-8"))
        finally:
            os.close(fd)

        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous_umask = os.umask(0o177)
        try:
            self._server.bind(str(self.socket_path))
        finally:
            os.umask(previous_umask)
        self._server.listen(8)
        self._server.settimeout(0.2)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(120)
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            if not data:
                return
            request = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
            method = request.get("method")
            params = request.get("params")
            with self._calls_lock:
                self.calls.append(method)
            envelope = self._responder(method, params)
            conn.sendall((json.dumps(envelope, ensure_ascii=False) + "\n").encode("utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            with contextlib.suppress(OSError):
                self._server.close()
        if self._thread is not None:
            self._thread.join(timeout=2)


def _fresh_sideband_dir() -> Path:
    # A short prefix -- AF_UNIX socket paths are capped at ~108 bytes and
    # this sandboxed environment's own temp root is already long, so every
    # extra prefix character risks overflowing that limit (see
    # ``test_app_server_mux.py``'s ``_MuxHarness`` for the same
    # constraint/workaround).
    return Path(tempfile.mkdtemp(prefix="b684-"))


def _idle_turn_start_ok(turn_id: str = "turn-1"):
    def _respond(method, _params):
        if method == "turn/start":
            return {"ok": True, "response": {"id": "w1", "result": {"turn": {"id": turn_id, "status": "inProgress"}}}}
        return {"ok": False, "error": "method_not_allowed"}
    return _respond


# ---------------------------------------------------------------------------
# Core repair: no thread/resume, no turn/steer -- exactly one turn/start
# ---------------------------------------------------------------------------

def test_deliver_callback_sends_only_turn_start_never_resume_or_steer():
    sideband_dir = _fresh_sideband_dir()
    thread_id = f"thread-{uuid.uuid4()}"
    endpoint = _FakeSidebandEndpoint(sideband_dir, thread_id, _idle_turn_start_ok())
    endpoint.start()
    try:
        client = SidebandCallbackClient(sideband_dir=sideband_dir, timeout=5)
        result = client.deliver_callback(
            thread_id, "TASK_B684_IDLE", "review_ready",
            client_user_message_id="fixed-b684-1", cwd="/home/shrek/GeoAI",
        )
        assert result["result"]["turn"]["status"] == "inProgress"
        assert endpoint.calls == ["turn/start"]
        assert "thread/resume" not in endpoint.calls
        assert "turn/steer" not in endpoint.calls
    finally:
        endpoint.stop()


def test_deliver_callback_batch_sends_only_turn_start_never_resume_or_steer():
    sideband_dir = _fresh_sideband_dir()
    thread_id = f"thread-{uuid.uuid4()}"
    endpoint = _FakeSidebandEndpoint(sideband_dir, thread_id, _idle_turn_start_ok())
    endpoint.start()
    try:
        client = SidebandCallbackClient(sideband_dir=sideband_dir, timeout=5)
        members = [
            {"task_id": "T1", "state": "review_ready", "event_id": "e1", "request_id": "r1"},
            {"task_id": "T2", "state": "blocked", "event_id": "e2", "request_id": "r2"},
        ]
        result = client.deliver_callback_batch(
            thread_id, members, client_user_message_id="fixed-b684-batch-1",
        )
        assert result["result"]["turn"]["status"] == "inProgress"
        assert endpoint.calls == ["turn/start"]
    finally:
        endpoint.stop()


# ---------------------------------------------------------------------------
# The exact measured B683 timing defect: a slow thread/resume must never be
# waited on, because it is never sent at all.
# ---------------------------------------------------------------------------

def test_delivery_never_waits_on_a_slow_thread_resume_the_b683_defect():
    """B683: against the real production thread, ``thread/resume`` alone
    repeatedly exhausted the mux's bounded 45-second sideband deadline
    (the thread's history was very large). Here the fake endpoint would
    sleep past any reasonable client timeout if ``thread/resume`` were
    ever sent -- proving the fix (turn/start-only, no resume probe) keeps
    delivery fast regardless, since that slow path is structurally never
    reached."""
    sideband_dir = _fresh_sideband_dir()
    thread_id = f"thread-{uuid.uuid4()}"

    def _respond(method, _params):
        if method == "turn/start":
            return {"ok": True, "response": {"id": "w1", "result": {"turn": {"id": "turn-fast", "status": "inProgress"}}}}
        if method == "thread/resume":
            time.sleep(5.0)  # would blow well past a short client timeout
            return {"ok": False, "error": "not_ready", "detail": "sideband_response_timeout"}
        return {"ok": False, "error": "method_not_allowed"}

    endpoint = _FakeSidebandEndpoint(sideband_dir, thread_id, _respond)
    endpoint.start()
    try:
        # A timeout far shorter than the simulated slow-resume sleep above:
        # if the old resume-then-decide path were still in effect, this
        # call would itself time out (SidebandUnavailableError/socket
        # timeout) well before returning.
        client = SidebandCallbackClient(sideband_dir=sideband_dir, timeout=1.0)
        started = time.monotonic()
        result = client.deliver_callback(thread_id, "TASK_B684_FAST", "review_ready")
        elapsed = time.monotonic() - started
        assert elapsed < 1.0
        assert result["result"]["turn"]["id"] == "turn-fast"
        assert "thread/resume" not in endpoint.calls
    finally:
        endpoint.stop()


# ---------------------------------------------------------------------------
# Busy-park classification: active/busy/activeTurnNotSteerable/already-in-
# progress rejections durably park; malformed/protocol failures stay hard.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "message",
    [
        "turn_start_forbidden_on_active_thread: a steerable/active thread must never receive a second turn/start",
        "thread is busy with another turn",
        "turn already in progress",
        "thread not_ready: handshake in progress",
    ],
)
def test_turn_start_active_busy_message_variants_durably_park(message):
    sideband_dir = _fresh_sideband_dir()
    thread_id = f"thread-{uuid.uuid4()}"

    def _respond(method, _params):
        assert method == "turn/start"
        return {"ok": True, "response": {"id": "w1", "error": {"code": -32000, "message": message}}}

    endpoint = _FakeSidebandEndpoint(sideband_dir, thread_id, _respond)
    endpoint.start()
    try:
        client = SidebandCallbackClient(sideband_dir=sideband_dir, timeout=5)
        with pytest.raises(SidebandThreadBusyError):
            client.deliver_callback(thread_id, "TASK_B684_BUSY", "review_ready")
    finally:
        endpoint.stop()


def test_turn_start_active_turn_not_steerable_structured_data_durably_parks():
    """The same ``CodexErrorInfo.activeTurnNotSteerable`` structured shape
    ``turn/steer`` rejections use must also be recognized on a ``turn/start``
    error even if the free-text message itself carries none of the
    substring markers."""
    sideband_dir = _fresh_sideband_dir()
    thread_id = f"thread-{uuid.uuid4()}"

    def _respond(method, _params):
        assert method == "turn/start"
        return {
            "ok": True,
            "response": {
                "id": "w1",
                "error": {
                    "code": -32001,
                    "message": "rejected",
                    "data": {"codexErrorInfo": {"activeTurnNotSteerable": {"turnKind": "review"}}},
                },
            },
        }

    endpoint = _FakeSidebandEndpoint(sideband_dir, thread_id, _respond)
    endpoint.start()
    try:
        client = SidebandCallbackClient(sideband_dir=sideband_dir, timeout=5)
        with pytest.raises(SidebandThreadBusyError):
            client.deliver_callback(thread_id, "TASK_B684_NOT_STEERABLE", "review_ready")
    finally:
        endpoint.stop()


def test_sideband_thread_busy_error_is_a_busy_thread_error_subclass():
    assert issubclass(SidebandThreadBusyError, BusyThreadError)


def test_is_sideband_turn_start_busy_rejection_pure_function():
    assert _is_sideband_turn_start_busy_rejection({"message": "thread is busy"}) is True
    assert _is_sideband_turn_start_busy_rejection({"message": "turn_start_forbidden_on_active_thread"}) is True
    assert _is_sideband_turn_start_busy_rejection(
        {"message": "x", "data": {"codexErrorInfo": {"activeTurnNotSteerable": {}}}}
    ) is True
    assert _is_sideband_turn_start_busy_rejection({"message": "invalid params: threadId required"}) is False
    assert _is_sideband_turn_start_busy_rejection({"message": "unauthorized"}) is False


def test_turn_start_malformed_response_is_hard_failure_not_busy_park():
    """A response missing ``result.turn.id`` is a protocol/malformed
    failure -- must raise the base ``AppServerError`` and must NOT be a
    ``BusyThreadError`` (it must retain the bounded hard-failure path)."""
    sideband_dir = _fresh_sideband_dir()
    thread_id = f"thread-{uuid.uuid4()}"

    def _respond(method, _params):
        assert method == "turn/start"
        return {"ok": True, "response": {"id": "w1", "result": {}}}

    endpoint = _FakeSidebandEndpoint(sideband_dir, thread_id, _respond)
    endpoint.start()
    try:
        client = SidebandCallbackClient(sideband_dir=sideband_dir, timeout=5)
        with pytest.raises(AppServerError) as excinfo:
            client.deliver_callback(thread_id, "TASK_B684_MALFORMED", "review_ready")
        assert not isinstance(excinfo.value, BusyThreadError)
    finally:
        endpoint.stop()


def test_turn_start_denied_error_is_hard_failure_not_busy_park():
    sideband_dir = _fresh_sideband_dir()
    thread_id = f"thread-{uuid.uuid4()}"

    def _respond(method, _params):
        assert method == "turn/start"
        return {"ok": True, "response": {"id": "w1", "error": {"code": -32403, "message": "permission denied"}}}

    endpoint = _FakeSidebandEndpoint(sideband_dir, thread_id, _respond)
    endpoint.start()
    try:
        client = SidebandCallbackClient(sideband_dir=sideband_dir, timeout=5)
        with pytest.raises(AppServerError) as excinfo:
            client.deliver_callback(thread_id, "TASK_B684_DENIED", "review_ready")
        assert not isinstance(excinfo.value, BusyThreadError)
    finally:
        endpoint.stop()


# ---------------------------------------------------------------------------
# End-to-end via CallbackBridge.run_once(): reproduces the exact measured
# B683 shape (attempts>=8, hard_failure_count unmoved, last_failure_kind
# 'busy'), then proves eventual delivery once the thread goes idle.
# ---------------------------------------------------------------------------

def _seed_review_task(conn, task_id: str, thread_id: str) -> None:
    taskdb.upsert_card(conn, {
        "schema_id": "geoai.machine_task_card.v1",
        "task_id": task_id,
        "status": "review",
        "worker_status": "review",
        "runner": "r",
        "topic": "task_mcp",
        "priority": "high",
        "objective": "b684 sideband atomic idle turn/start e2e",
        "allowed_writes": [],
        "acceptance": [],
        "validation": [],
        "origin_thread_id": thread_id,
    })


def _clear_not_before(db_path: Path) -> None:
    conn = taskdb.open_db(db_path)
    conn.execute("UPDATE callback_batches SET not_before_at=''")
    conn.commit()
    conn.close()


def test_run_once_reproduces_b683_shape_durable_park_never_dead_letters():
    """The exact measured B683 batch shape: repeated claims against a
    thread whose ``turn/start`` the App Server rejects as
    active/busy reach ``attempts>=8`` while ``hard_failure_count`` never
    moves and ``last_failure_kind`` stays ``'busy'`` -- the event remains
    durable, parked, never dead-lettered."""
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        db_path = repo / "task_queue.sqlite"
        conn = taskdb.open_db(db_path)
        taskdb.init_db(conn)
        thread_id = str(uuid.uuid4())
        _seed_review_task(conn, "E2E_B684_LARGE_THREAD_BUSY", thread_id)
        conn.close()

        sideband_dir = _fresh_sideband_dir()

        def _respond(method, _params):
            assert method == "turn/start"
            return {
                "ok": True,
                "response": {"id": "w1", "error": {"code": -32000, "message": "thread is busy with another turn"}},
            }

        endpoint = _FakeSidebandEndpoint(sideband_dir, thread_id, _respond)
        endpoint.start()
        try:
            bridge = CallbackBridge(
                repo=repo, db_path=db_path, state_path=repo / "state.json",
                transport="sideband", sideband_dir=sideband_dir,
                lease_seconds=30, app_server_timeout=5, lease_margin_seconds=1,
            )
            for _ in range(8):
                result = bridge.run_once()
                assert result["ok"] is False
                assert result["action"] == "deferred_or_failed"
                _clear_not_before(db_path)

            stats = taskdb.callback_outbox_stats(taskdb.open_db(db_path))
            assert stats["by_state"]["dead_letter"] == 0
            assert stats["by_state"]["pending"] == 1
            assert stats["batches"]["by_state"]["dead_letter"] == 0
            assert stats["batches"]["by_state"]["pending"] == 1

            conn = taskdb.open_db(db_path)
            row = conn.execute(
                "SELECT hard_failure_count, attempts, last_failure_kind FROM callback_batches"
            ).fetchone()
            conn.close()
            assert row["attempts"] >= 8
            assert row["hard_failure_count"] == 0
            assert row["last_failure_kind"] == "busy"
        finally:
            endpoint.stop()


def test_run_once_delivers_once_thread_goes_idle_same_durable_batch():
    """Eventual delivery: once the SAME durable batch's owner thread's
    ``turn/start`` starts succeeding (the extension's turn finished), the
    parked event delivers exactly once -- never dead-lettered by the
    intervening busy parks, never a duplicate turn."""
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        db_path = repo / "task_queue.sqlite"
        conn = taskdb.open_db(db_path)
        taskdb.init_db(conn)
        thread_id = str(uuid.uuid4())
        _seed_review_task(conn, "E2E_B684_EVENTUAL_IDLE", thread_id)
        conn.close()

        sideband_dir = _fresh_sideband_dir()
        state = {"busy": True}

        def _respond(method, _params):
            assert method == "turn/start"
            if state["busy"]:
                return {"ok": True, "response": {"id": "w1", "error": {"code": -32000, "message": "thread is busy"}}}
            return {"ok": True, "response": {"id": "w1", "result": {"turn": {"id": "turn-idle-now", "status": "inProgress"}}}}

        endpoint = _FakeSidebandEndpoint(sideband_dir, thread_id, _respond)
        endpoint.start()
        try:
            bridge = CallbackBridge(
                repo=repo, db_path=db_path, state_path=repo / "state.json",
                transport="sideband", sideband_dir=sideband_dir,
                lease_seconds=30, app_server_timeout=5, lease_margin_seconds=1,
            )
            first = bridge.run_once()
            assert first["ok"] is False and first["action"] == "deferred_or_failed"
            _clear_not_before(db_path)

            state["busy"] = False
            second = bridge.run_once()
            assert second["ok"] is True
            assert second["action"] == "delivered"
            assert second["task_ids"] == ["E2E_B684_EVENTUAL_IDLE"]

            stats = taskdb.callback_outbox_stats(taskdb.open_db(db_path))
            assert stats["by_state"]["delivered"] == 1
            assert stats["by_state"]["dead_letter"] == 0
            assert stats["batches"]["by_state"]["delivered"] == 1
        finally:
            endpoint.stop()


def test_run_once_genuine_protocol_failure_still_bounded_dead_letters():
    """A genuine malformed/protocol turn/start response (not a busy
    rejection) must retain the existing bounded retry/dead-letter route --
    distinct from the unbounded busy park proven above."""
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        db_path = repo / "task_queue.sqlite"
        conn = taskdb.open_db(db_path)
        taskdb.init_db(conn)
        thread_id = str(uuid.uuid4())
        _seed_review_task(conn, "E2E_B684_PROTOCOL_FAILURE", thread_id)
        conn.close()

        sideband_dir = _fresh_sideband_dir()

        def _respond(method, _params):
            assert method == "turn/start"
            return {"ok": True, "response": {"id": "w1", "result": {}}}  # missing turn.id

        endpoint = _FakeSidebandEndpoint(sideband_dir, thread_id, _respond)
        endpoint.start()
        try:
            bridge = CallbackBridge(
                repo=repo, db_path=db_path, state_path=repo / "state.json",
                transport="sideband", sideband_dir=sideband_dir,
                lease_seconds=30, app_server_timeout=5, lease_margin_seconds=1,
            )
            for _ in range(DEFAULT_MAX_RETRIES):
                result = bridge.run_once()
                assert result["ok"] is False
                _clear_not_before(db_path)

            stats = taskdb.callback_outbox_stats(taskdb.open_db(db_path))
            assert stats["by_state"]["dead_letter"] == 1
            assert stats["batches"]["by_state"]["dead_letter"] == 1

            conn = taskdb.open_db(db_path)
            row = conn.execute(
                "SELECT hard_failure_count, last_failure_kind FROM callback_batches"
            ).fetchone()
            conn.close()
            assert row["last_failure_kind"] == "transient_error"
        finally:
            endpoint.stop()
