"""B823: stale active-turn owner recovery for durable sideband callbacks."""
from __future__ import annotations

import contextlib
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import app_server_mux, callback_bridge  # noqa: E402
from aiworkhub.app_server_mux import (  # noqa: E402
    SIDEBAND_OWNER_LEASE_SECONDS,
    describe_sideband_owner_freshness,
    find_owning_sideband_instances,
    sideband_instances_dir,
)
from aiworkhub.callback_bridge import (  # noqa: E402
    CallbackBridge,
    SidebandCallbackClient,
    SidebandOwnerNotFoundError,
    SidebandThreadBusyError,
)


# B925: sideband instances/clients are repository-bound; one valid repo_id
# shared by the fake endpoint's registry and the client so ownership resolves.
_REPO_ID = "repo_b823_test"


class _Endpoint:
    def __init__(self, sideband_dir: Path, thread_id: str, responder, *, heartbeat_age: float = 0.0):
        self.sideband_dir = sideband_dir
        self.thread_id = thread_id
        self.responder = responder
        self.instance_id = uuid.uuid4().hex[:8]
        self.generation_id = uuid.uuid4().hex
        self.socket_path = sideband_dir / f"{self.instance_id}.sock"
        self.capability_path = sideband_dir / f"{self.instance_id}.cap"
        self.registry_path = sideband_instances_dir(sideband_dir) / f"{self.instance_id}.json"
        self.capability = "cap-" + uuid.uuid4().hex
        self.calls: list[dict] = []
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._heartbeat_age = heartbeat_age

    def start(self) -> None:
        app_server_mux.ensure_private_dir(self.sideband_dir)
        app_server_mux.ensure_private_dir(sideband_instances_dir(self.sideband_dir))
        _write_0600(self.capability_path, self.capability)
        self._write_registry()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            self._server.bind(str(self.socket_path))
        finally:
            os.umask(old_umask)
        self._server.listen(8)
        self._server.settimeout(0.1)
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _write_registry(self) -> None:
        descriptor = {
            "instance_id": self.instance_id,
            "generation_id": self.generation_id,
            "pid": os.getpid(),
            "pid_start_time": app_server_mux._proc_start_time(os.getpid()),
            "socket_path": str(self.socket_path),
            "capability_path": str(self.capability_path),
            "owned_thread_ids": [self.thread_id],
            "repo_id": _REPO_ID,
            "heartbeat_at": time.time() - self._heartbeat_age,
            "owner_lease_seconds": SIDEBAND_OWNER_LEASE_SECONDS,
            "ready": True,
        }
        _write_0600(self.registry_path, json.dumps(descriptor, ensure_ascii=False))

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            request = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
            self.calls.append(request)
            conn.sendall((json.dumps(self.responder(request)) + "\n").encode("utf-8"))
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            with contextlib.suppress(OSError):
                self._server.close()
        if self._thread is not None:
            self._thread.join(timeout=1)


def _write_0600(path: Path, text: str) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def _sideband_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="b823-"))


def test_stale_generation_is_ignored_and_reconnect_generation_receives_exactly_once() -> None:
    sideband_dir = _sideband_dir()
    thread_id = "thread-" + uuid.uuid4().hex
    stale = _Endpoint(sideband_dir, thread_id, lambda _request: pytest.fail("stale owner called"), heartbeat_age=9999)
    fresh = _Endpoint(
        sideband_dir,
        thread_id,
        lambda _request: {"ok": True, "response": {"id": "w", "result": {"turn": {"id": "turn-fresh"}}}},
    )
    stale.start()
    fresh.start()
    try:
        owners = find_owning_sideband_instances(sideband_dir, thread_id, _REPO_ID)
        assert [owner.generation_id for owner in owners] == [fresh.generation_id]
        result = SidebandCallbackClient(repo_id=_REPO_ID, sideband_dir=sideband_dir, timeout=2).deliver_callback(
            thread_id, "TASK_B823_RECOVER", "review_ready"
        )
        assert result["result"]["turn"]["id"] == "turn-fresh"
        assert stale.calls == []
        assert [call["method"] for call in fresh.calls] == ["turn/start"]
    finally:
        stale.stop()
        fresh.stop()


def test_only_stale_owner_parks_durable_callback_without_hard_failure() -> None:
    sideband_dir = _sideband_dir()
    thread_id = "thread-" + uuid.uuid4().hex
    stale = _Endpoint(sideband_dir, thread_id, lambda _request: pytest.fail("expired owner called"), heartbeat_age=9999)
    stale.start()
    try:
        with pytest.raises(SidebandOwnerNotFoundError):
            SidebandCallbackClient(repo_id=_REPO_ID, sideband_dir=sideband_dir, timeout=1).deliver_callback(
                thread_id, "TASK_B823_STALE_ONLY", "review_ready"
            )
        assert stale.calls == []
        freshness = describe_sideband_owner_freshness(sideband_dir, thread_id, _REPO_ID)
        assert freshness["owner_count"] == 1
        assert freshness["fresh_owner_count"] == 0
        assert freshness["owners"][0]["fresh"] is False
    finally:
        stale.stop()


def test_genuine_fresh_active_owner_remains_parked_and_is_not_double_delivered() -> None:
    sideband_dir = _sideband_dir()
    thread_id = "thread-" + uuid.uuid4().hex

    def busy(_request):
        return {"ok": True, "response": {"id": "w", "error": {"message": "thread is busy with another turn"}}}

    endpoint = _Endpoint(sideband_dir, thread_id, busy)
    endpoint.start()
    try:
        with pytest.raises(SidebandThreadBusyError):
            SidebandCallbackClient(repo_id=_REPO_ID, sideband_dir=sideband_dir, timeout=2).deliver_callback(
                thread_id, "TASK_B823_ACTIVE", "review_ready"
            )
        assert [call["method"] for call in endpoint.calls] == ["turn/start"]
        assert find_owning_sideband_instances(sideband_dir, thread_id, _REPO_ID)[0].is_owner_fresh is True
    finally:
        endpoint.stop()


def test_status_exposes_redacted_parked_age_and_owner_generation_freshness() -> None:
    sideband_dir = _sideband_dir()
    thread_id = "thread-" + uuid.uuid4().hex
    endpoint = _Endpoint(sideband_dir, thread_id, lambda _request: {"ok": False, "error": "not_ready"})
    endpoint.start()
    try:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE callback_batches (
                batch_id TEXT, origin_thread_id TEXT, state TEXT, last_failure_kind TEXT,
                updated_at TEXT, not_before_at TEXT, attempts INTEGER
            )"""
        )
        conn.execute(
            "INSERT INTO callback_batches VALUES (?, ?, 'pending', 'busy', ?, '', 3)",
            ("batch-b823", thread_id, "2026-07-20T00:00:00+00:00"),
        )
        bridge = CallbackBridge.__new__(CallbackBridge)
        bridge._sideband_dir = sideband_dir
        bridge._sideband_repo_id = _REPO_ID
        status = bridge._sideband_owner_freshness_status(conn)
        parked = status["parked_batches"][0]
        assert parked["origin_thread_id"].endswith(thread_id[-4:])
        assert thread_id not in json.dumps(status)
        assert parked["parked_reason"] == "busy"
        assert parked["parked_age_seconds"] is not None
        assert parked["owner_freshness"]["fresh_owner_count"] == 1
        assert parked["owner_freshness"]["owners"][0]["generation_id"] == endpoint.generation_id
    finally:
        endpoint.stop()


def test_callback_bridge_does_not_reimplement_or_monkeypatch_taskdb() -> None:
    source = Path(callback_bridge.__file__).read_text(encoding="utf-8")
    forbidden = [
        "_ensure_taskdb_callback_compat",
        "setattr(taskdb",
        "CREATE TABLE IF NOT EXISTS callback_batches",
        "ALTER TABLE callback_batches",
    ]
    for marker in forbidden:
        assert marker not in source
