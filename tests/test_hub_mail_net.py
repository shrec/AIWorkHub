"""WorkHub Mail phase 2: two (or three) machines exchange mail over loopback.

Every simulated machine is its own tmp home with its own router registry and
its own listener on 127.0.0.1 port 0, so nothing here touches the real
``~/.aiworkhub`` or leaves the loopback interface.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import inspect
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, hub_mail, hub_mail_net, runtime_temp, server, shared_router  # noqa: E402
from aiworkhub import runtime_adapters as ra  # noqa: E402

HUB_A = "repo_" + "a" * 32
HUB_B = "repo_" + "b" * 32
HUB_C = "repo_" + "c" * 32
HUB_B2 = "repo_" + "d" * 32
HUB_LATE = "repo_" + "e" * 32
HUB_GHOST = "repo_" + "f" * 32


@pytest.fixture
def world(tmp_path, monkeypatch):
    default_home = tmp_path / "default_home"
    default_home.mkdir()
    monkeypatch.setenv("HOME", str(default_home))
    monkeypatch.setenv("USERPROFILE", str(default_home))
    roots: dict[str, str] = {}
    monkeypatch.setattr(
        shared_router, "_read_manifest_repo_id", lambda root: roots.get(str(Path(root).resolve()), "")
    )
    opened: list[hub_mail_net.Listener] = []

    def register(home: Path, hub_id: str, repo_name: str | None) -> Path:
        root = tmp_path / "repos" / f"{home.name}-{hub_id[-6:]}"
        root.mkdir(parents=True)
        roots[str(root.resolve())] = hub_id
        registry = shared_router.registry_dir(home)
        registry.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_id": shared_router.SCHEMA_ID,
            "repo_id": hub_id,
            "repo_root": str(root),
            "extension_host_pid": 0,
        }
        if repo_name is not None:
            record["repo_name"] = repo_name
        (registry / f"{hub_id}.json").write_text(json.dumps(record), encoding="utf-8")
        return root

    def machine(name: str, label: str, *hubs: tuple[str, str | None]) -> Path:
        home = tmp_path / name
        home.mkdir(exist_ok=True)
        for hub_id, repo_name in hubs:
            register(home, hub_id, repo_name)
        hub_mail_net.set_machine_label(label, home)
        return home

    def listen(home: Path, port: int = 0, **options) -> hub_mail_net.Listener:
        listener = hub_mail_net.Listener(
            bind_host="127.0.0.1", port=port, home=home, retry_interval=3600, **options
        )
        listener.start()
        opened.append(listener)
        return listener

    yield SimpleNamespace(
        default_home=default_home, register=register, machine=machine, listen=listen
    )
    for listener in opened:
        listener.close()


def _local(hub_id: str) -> dict[str, str]:
    return {"hub_id": hub_id, "repo_root": f"/repos/{hub_id}", "aiworkhub_version": "test"}


def _send(home: Path, sender: str, to: str, **overrides) -> dict:
    fields = {"kind": "problem_report", "subject": "Task MCP refuses", "body": "details"}
    fields.update(overrides)
    return hub_mail.send(local=_local(sender), to_hub_id=to, home=home, **fields)


def _pair(client: Path, server_home: Path, listener: hub_mail_net.Listener, **options) -> str:
    code = hub_mail_net.pair_begin(server_home)["code"]
    result = hub_mail_net.pair_complete(listener.address, code, home=client, **options)
    assert result["ok"] is True, result
    return code


def _inbox_files(home: Path, hub_id: str) -> list[str]:
    directory = hub_mail.box_dir(hub_id, "inbox", home)
    return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []


def _envelope(from_hub: str, to_hub: str, key: bytes, **changes) -> dict:
    moment = datetime.now(timezone.utc)
    message_id = uuid.uuid4().hex
    envelope = {
        "schema_id": hub_mail.SCHEMA_ID,
        "message_id": message_id,
        "thread_id": message_id,
        "in_reply_to": None,
        "from_hub": {"hub_id": from_hub, "repo_root": "/r", "aiworkhub_version": "test"},
        "to_hub": {"hub_id": to_hub},
        "kind": "note",
        "status": None,
        "fixed_in": None,
        "subject": "forged?",
        "body": "b",
        "attachments": [],
        "created_at": hub_mail._format_time(moment),
        "expires_at": hub_mail._format_time(moment + timedelta(days=1)),
        "signature": "0" * 64,
    }
    envelope.update(changes)
    envelope["signature"] = hub_mail.sign(envelope, key)
    return envelope


def _raw(address: str, method: str, path: str, body: bytes = b"", headers: dict | None = None):
    host, port = hub_mail_net.parse_address(address)
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        return response.status, json.loads(data or b"{}"), {k.lower(): v for k, v in response.getheaders()}
    finally:
        connection.close()


def _other_code(code: str) -> str:
    while True:
        text = base64.b32encode(secrets.token_bytes(hub_mail_net.PAIR_CODE_BYTES)).decode().rstrip("=")
        if text != code.replace("-", ""):
            return "-".join(text[i : i + 4] for i in range(0, len(text), 4))


def test_two_machines_pair_then_exchange_a_report_and_a_threaded_receipt(world):
    a = world.machine("machine_a", "alpha", (HUB_A, "geoai"))
    b = world.machine("machine_b", "bravo", (HUB_B, "aiworkhub"))
    world.listen(a)
    _pair(a, b, world.listen(b))
    node_a = {"machine_id": hub_mail_net.machine_id(a), "machine_label": "alpha"}
    node_b = {"machine_id": hub_mail_net.machine_id(b), "machine_label": "bravo"}

    sent = _send(a, HUB_A, HUB_B, attachments=[{"name": "trace.txt", "text": "Traceback ..."}])
    assert sent["ok"] is True and sent["delivery"] == "delivered", sent
    assert sent["to_node"] == node_b
    report_id = sent["mail"]["message_id"]
    outbox = hub_mail.box_dir(HUB_A, "outbox", a)
    assert sorted(p.name for p in outbox.iterdir()) == [f"{report_id}.json"]

    listing = hub_mail.inbox(local_hub_id=HUB_B, home=b)
    assert listing["ok"] is True and listing["quarantined_count"] == 0
    [mail] = listing["mails"]
    assert mail["message_id"] == report_id and mail["kind"] == "problem_report"
    assert mail["untrusted_data"] is True and mail["from_node"] == node_a
    shown = hub_mail.show(local_hub_id=HUB_B, message_id=report_id, home=b)
    assert shown["mail"]["from_node"] == node_a and shown["mail"]["untrusted_data"] is True
    assert shown["mail"]["attachments"][0]["text"] == "Traceback ..."

    reply = _send(
        b, HUB_B, HUB_A, kind="status_receipt", subject="Re: refuses", body="fixed",
        status="fixed_in", fixed_in="0.13.0", in_reply_to=report_id,
    )
    assert reply["ok"] is True and reply["delivery"] == "delivered", reply
    [received] = hub_mail.inbox(local_hub_id=HUB_A, home=a)["mails"]
    assert received["in_reply_to"] == report_id and received["thread_id"] == report_id
    assert received["status"] == "fixed_in" and received["fixed_in"] == "0.13.0"
    assert received["untrusted_data"] is True and received["from_node"] == node_b


def test_pairing_code_never_crosses_the_wire_and_both_sides_derive_one_secret(world, monkeypatch):
    a = world.machine("machine_a", "alpha", (HUB_A, "geoai"))
    b = world.machine("machine_b", "bravo", (HUB_B, "aiworkhub"))
    world.listen(a)
    listener_b = world.listen(b)
    wire: list[bytes] = []
    real_exchange = hub_mail_net._exchange

    def recording(address, method, path, body, headers, **options):
        wire.extend([body, json.dumps(headers).encode()])
        status, reply_headers, data, observed = real_exchange(address, method, path, body, headers, **options)
        wire.extend([data, json.dumps(reply_headers).encode()])
        return status, reply_headers, data, observed

    monkeypatch.setattr(hub_mail_net, "_exchange", recording)
    begun = hub_mail_net.pair_begin(b)
    code = begun["code"]
    assert len(code.replace("-", "")) * 5 >= 100
    assert begun["max_failed_proofs"] == 5
    assert hub_mail_net.pair_complete(listener_b.address, code, home=a)["ok"] is True
    hub_mail_net.refresh_peers(a)
    assert _send(a, HUB_A, HUB_B)["delivery"] == "delivered"
    assert len(wire) >= 12
    for blob in wire:
        assert code.encode() not in blob
        assert code.replace("-", "").encode() not in blob

    id_a, id_b = hub_mail_net.machine_id(a), hub_mail_net.machine_id(b)
    stored_a = json.loads(hub_mail_net.peer_path(id_b, a).read_text(encoding="utf-8"))
    stored_b = json.loads(hub_mail_net.peer_path(id_a, b).read_text(encoding="utf-8"))
    assert stored_a["secret"] == stored_b["secret"] == hub_mail_net.derive_secret(code, id_a, id_b).hex()

    again = hub_mail_net.pair_complete(listener_b.address, code, home=a)
    assert again == {"ok": False, "error": "peer_refused:pair_code_unavailable"}


def test_pairing_code_expires_after_ten_minutes(world, monkeypatch):
    a = world.machine("machine_a", "alpha", (HUB_A, "geoai"))
    b = world.machine("machine_b", "bravo", (HUB_B, "aiworkhub"))
    world.listen(a)
    listener_b = world.listen(b)
    code = hub_mail_net.pair_begin(b)["code"]
    real = time.time
    monkeypatch.setattr(hub_mail_net, "_clock", lambda: real() + hub_mail_net.PAIR_CODE_TTL_SECONDS + 1)
    result = hub_mail_net.pair_complete(listener_b.address, code, home=a)
    assert result == {"ok": False, "error": "peer_refused:pair_code_expired"}
    assert hub_mail_net.list_peers(a) == [] and hub_mail_net.list_peers(b) == []


def test_pairing_code_dies_after_five_failed_proofs(world):
    a = world.machine("machine_a", "alpha", (HUB_A, "geoai"))
    b = world.machine("machine_b", "bravo", (HUB_B, "aiworkhub"))
    world.listen(a)
    listener_b = world.listen(b)
    code = hub_mail_net.pair_begin(b)["code"]
    wrong = _other_code(code)
    for _ in range(hub_mail_net.PAIR_MAX_FAILURES):
        result = hub_mail_net.pair_complete(listener_b.address, wrong, home=a)
        assert result == {"ok": False, "error": "peer_refused:pair_proof_invalid"}
    result = hub_mail_net.pair_complete(listener_b.address, code, home=a)
    assert result == {"ok": False, "error": "peer_refused:pair_code_unavailable"}
    assert hub_mail_net.list_peers(a) == [] and hub_mail_net.list_peers(b) == []


def test_listener_refuses_unauthenticated_requests_and_writes_nothing(world):
    a = world.machine("machine_a", "alpha", (HUB_A, "geoai"))
    b = world.machine("machine_b", "bravo", (HUB_B, "aiworkhub"))
    world.listen(a)
    listener_b = world.listen(b)
    _pair(a, b, listener_b)
    peer_b = hub_mail_net.load_peer(hub_mail_net.machine_id(b), a)
    data = hub_mail.envelope_bytes(_envelope(HUB_A, HUB_B, bytes.fromhex(peer_b["secret"])))
    good = hub_mail_net._signed_headers(peer_b, a, "POST", "/v1/mail", data)
    now = int(time.time())
    skew = hub_mail_net.CLOCK_SKEW_SECONDS
    cases = [
        ({}, data, "auth_missing"),
        ({**good, hub_mail_net.HEADER_SIGNATURE: "0" * 64}, data, "bad_request_signature"),
        (good, data.replace(b'"b"', b'"c"'), "bad_request_signature"),
        ({**good, hub_mail_net.HEADER_PEER: "f" * 32}, data, "unknown_peer"),
        (hub_mail_net._signed_headers(peer_b, a, "POST", "/v1/mail", data, timestamp=str(now - skew - 5)),
         data, "timestamp_out_of_window"),
        (hub_mail_net._signed_headers(peer_b, a, "POST", "/v1/mail", data, timestamp=str(now + skew + 5)),
         data, "timestamp_out_of_window"),
    ]
    for headers, body, error in cases:
        status, payload, reply_headers = _raw(listener_b.address, "POST", "/v1/mail", body, headers)
        assert (status, payload) == (401, {"ok": False, "error": error}), error
        assert hub_mail_net.HEADER_SIGNATURE.lower() not in reply_headers

    host, port = hub_mail_net.parse_address(listener_b.address)
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.putrequest("POST", "/v1/mail")
        connection.putheader("Content-Length", str(hub_mail.MAX_ENVELOPE_BYTES + 1))
        for key, value in good.items():
            connection.putheader(key, value)
        connection.endheaders()
        response = connection.getresponse()
        assert (response.status, json.loads(response.read())) == (413, {"ok": False, "error": "body_too_large"})
    finally:
        connection.close()
    assert _inbox_files(b, HUB_B) == []

    hubs = hub_mail_net._signed_headers(peer_b, a, "GET", "/v1/hubs", b"")
    status, payload, reply_headers = _raw(listener_b.address, "GET", "/v1/hubs", b"", hubs)
    assert status == 200 and payload["hubs"] == [{"hub_id": HUB_B, "repo_name": "aiworkhub"}]
    assert hub_mail_net.HEADER_SIGNATURE.lower() in reply_headers
    status, payload, _ = _raw(listener_b.address, "GET", "/v1/hubs", b"", hubs)
    assert (status, payload) == (401, {"ok": False, "error": "nonce_replayed"})
    assert _raw(listener_b.address, "GET", "/v1/secrets")[:2] == (404, {"ok": False, "error": "not_found"})

    # The same bytes, correctly signed, are accepted: the refusals were the auth.
    status, payload, _ = _raw(listener_b.address, "POST", "/v1/mail", data, good)
    assert status == 200 and payload["delivery"] == "delivered"
    assert _inbox_files(b, HUB_B) == [f"{json.loads(data)['message_id']}.json"]


def test_a_peer_cannot_pass_as_a_local_hub_or_another_peer(world):
    a = world.machine("machine_a", "alpha", (HUB_A, "geoai"))
    b = world.machine("machine_b", "bravo", (HUB_B, "aiworkhub"), (HUB_B2, "tools"))
    c = world.machine("machine_c", "charlie", (HUB_C, "other"))
    world.listen(a)
    world.listen(c)
    listener_b = world.listen(b)
    _pair(a, b, listener_b)
    _pair(c, b, listener_b)
    peer_b = hub_mail_net.load_peer(hub_mail_net.machine_id(b), a)
    secret_ab = bytes.fromhex(peer_b["secret"])

    as_local = _envelope(HUB_B2, HUB_B, secret_ab)
    as_other_peer = _envelope(HUB_C, HUB_B, secret_ab)
    genuine = _envelope(HUB_A, HUB_B, secret_ab)
    for envelope in (as_local, as_other_peer, genuine):
        assert hub_mail_net.post_mail(peer_b, hub_mail.envelope_bytes(envelope), home=a) == "delivered"
    ghost = _envelope(HUB_GHOST, HUB_B, secret_ab)
    inbox_dir = hub_mail.box_dir(HUB_B, "inbox", b)
    (inbox_dir / f"{ghost['message_id']}.json").write_bytes(hub_mail.envelope_bytes(ghost))
    local = _send(b, HUB_B2, HUB_B, kind="note", subject="same machine", body="phase 1")
    assert local["ok"] is True and local["delivery"] == "delivered" and "to_node" not in local

    listing = hub_mail.inbox(local_hub_id=HUB_B, home=b)
    reasons = {item["file"]: item["reason"] for item in listing["quarantined"]}
    assert reasons == {
        f"{as_local['message_id']}.json": "bad_signature",
        f"{as_other_peer['message_id']}.json": "bad_signature",
        f"{ghost['message_id']}.json": "unknown_sender",
    }
    mails = {mail["message_id"]: mail for mail in listing["mails"]}
    assert set(mails) == {genuine["message_id"], local["mail"]["message_id"]}
    assert mails[genuine["message_id"]]["from_node"]["machine_id"] == hub_mail_net.machine_id(a)
    assert "from_node" not in mails[local["mail"]["message_id"]]


def test_store_and_forward_delivers_exactly_once_and_never_drops_expired_mail(world):
    a = world.machine("machine_a", "alpha", (HUB_A, "geoai"))
    b = world.machine("machine_b", "bravo", (HUB_B, "aiworkhub"))
    world.listen(a)
    listener_b = world.listen(b)
    _pair(a, b, listener_b)
    port = int(listener_b.record["port"])
    listener_b.close()

    sent = _send(a, HUB_A, HUB_B)
    assert sent["ok"] is True and sent["delivery"] == "queued", sent
    copy = hub_mail.box_dir(HUB_A, "outbox", a) / f"{sent['mail']['message_id']}.json"
    pending = copy.with_name(copy.name + hub_mail_net.PENDING_SUFFIX)
    assert copy.is_file() and pending.is_file()
    state = json.loads(pending.read_text(encoding="utf-8"))
    assert state["state"] == "pending" and state["last_error"].startswith("peer_unreachable")
    assert hub_mail_net.retry_pending(a)["queued"] == [copy.name]
    assert _inbox_files(b, HUB_B) == []

    world.listen(b, port=port)
    assert hub_mail_net.retry_pending(a)["delivered"] == [copy.name]
    assert not pending.exists() and copy.is_file()
    assert hub_mail_net.retry_pending(a)["delivered"] == []
    assert _inbox_files(b, HUB_B) == [copy.name]
    assert [m["message_id"] for m in hub_mail.inbox(local_hub_id=HUB_B, home=b)["mails"]] == [copy.stem]

    hub_mail_net.stop_listener(b)
    late = _send(a, HUB_A, HUB_B, subject="never arrives")
    assert late["delivery"] == "queued"
    late_copy = hub_mail.box_dir(HUB_A, "outbox", a) / f"{late['mail']['message_id']}.json"
    # The inbox call retries queued mail too; by then this one is past expires_at.
    hub_mail.inbox(local_hub_id=HUB_A, home=a, now=datetime(2100, 1, 1, tzinfo=timezone.utc))
    failed = late_copy.with_name(late_copy.name + hub_mail_net.FAILED_SUFFIX)
    assert failed.is_file() and late_copy.is_file()
    assert not late_copy.with_name(late_copy.name + hub_mail_net.PENDING_SUFFIX).exists()
    assert json.loads(failed.read_text(encoding="utf-8"))["reason"] == "expired"
    assert _inbox_files(b, HUB_B) == [copy.name]


def test_peers_listing_never_returns_a_secret(world):
    a = world.machine("machine_a", "alpha", (HUB_A, "geoai"))
    b = world.machine("machine_b", "bravo", (HUB_B, "aiworkhub"))
    world.listen(a)
    _pair(a, b, world.listen(b))
    listing = hub_mail_net.peers(a)
    [peer] = listing["peers"]
    assert set(peer) == {
        "machine_id", "machine_label", "address", "hubs", "paired_at", "last_seen", "tls_cert_sha256",
    }
    assert peer["machine_id"] == hub_mail_net.machine_id(b) and peer["machine_label"] == "bravo"
    assert peer["hubs"] == [{"hub_id": HUB_B, "repo_name": "aiworkhub"}]
    assert peer["paired_at"] and peer["last_seen"]
    secret = json.loads(hub_mail_net.peer_path(peer["machine_id"], a).read_text(encoding="utf-8"))["secret"]
    assert secret not in json.dumps(listing)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not enforced on Windows")
def test_peer_key_machine_id_and_pidfile_are_0600(world):
    a = world.machine("machine_a", "alpha", (HUB_A, "geoai"))
    b = world.machine("machine_b", "bravo", (HUB_B, "aiworkhub"))
    world.listen(a)
    _pair(a, b, world.listen(b))
    hub_mail.load_or_create_key(a)
    for path in (
        hub_mail_net.peer_path(hub_mail_net.machine_id(b), a),
        hub_mail_net.machine_id_path(a),
        hub_mail.key_path(a),
        hub_mail_net.pidfile_path(a),
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


def test_labels_are_display_only_and_new_hubs_appear_after_one_refresh(world):
    hostile = "x" * 100 + "\x07‮\n"
    a = world.machine("machine_a", "alpha", (HUB_A, "geoai"))
    b = world.machine("machine_b", "twin", (HUB_B, "n" * 80 + "\x1b[31m"))
    c = world.machine("machine_c", "twin", (HUB_C, "other"))
    hub_mail_net.node_path(b).write_text(json.dumps({"machine_label": hostile}), encoding="utf-8")
    hub_mail_net.node_path(c).write_text(json.dumps({"machine_label": hostile}), encoding="utf-8")
    world.listen(a)
    listener_b, listener_c = world.listen(b), world.listen(c)
    _pair(a, b, listener_b)
    _pair(a, c, listener_c)

    peers = {peer["machine_id"]: peer for peer in hub_mail_net.peers(a)["peers"]}
    peer_b = peers[hub_mail_net.machine_id(b)]
    assert peer_b["machine_label"] == "x" * 64
    assert peer_b["hubs"] == [{"hub_id": HUB_B, "repo_name": "n" * 64}]
    assert peers[hub_mail_net.machine_id(c)]["machine_label"] == peer_b["machine_label"]

    to_b = _send(a, HUB_A, HUB_B, subject="for b")
    to_c = _send(a, HUB_A, HUB_C, subject="for c")
    assert to_b["delivery"] == to_c["delivery"] == "delivered"
    assert _inbox_files(b, HUB_B) == [f"{to_b['mail']['message_id']}.json"]
    assert _inbox_files(c, HUB_C) == [f"{to_c['mail']['message_id']}.json"]
    assert hub_mail.box_dir(HUB_C, "inbox", b).is_dir() is False

    world.register(b, HUB_LATE, "late")
    assert _send(a, HUB_A, HUB_LATE) == {"ok": False, "error": "unknown_recipient"}
    refreshed = hub_mail_net.refresh_peers(a)
    assert all(item["ok"] for item in refreshed["refreshed"]), refreshed
    peer_b = {p["machine_id"]: p for p in hub_mail_net.peers(a)["peers"]}[hub_mail_net.machine_id(b)]
    assert {hub["hub_id"] for hub in peer_b["hubs"]} == {HUB_B, HUB_LATE}
    assert _send(a, HUB_A, HUB_LATE)["delivery"] == "delivered"


def test_listener_binds_only_an_explicit_host_and_one_per_home(world):
    a = world.machine("machine_a", "alpha", (HUB_A, "geoai"))
    for function in (hub_mail_net.Listener, hub_mail_net.start_listener):
        parameter = inspect.signature(function).parameters["bind_host"]
        assert parameter.default is inspect.Parameter.empty
    with pytest.raises(hub_mail.HubMailError, match="bind_host_required"):
        hub_mail_net.Listener(bind_host="", port=0, home=a)
    assert hub_mail_net.listener_action("start", home=a) == {"ok": False, "error": "bind_host_required"}

    listener = world.listen(a)
    assert listener._server.server_address[0] == "127.0.0.1"
    assert listener.address.startswith("127.0.0.1:")
    status = hub_mail_net.listener_status(a)
    assert status["running"] is True and status["pid"] == os.getpid()
    with pytest.raises(hub_mail.HubMailError, match="listener_already_running"):
        hub_mail_net.Listener(bind_host="127.0.0.1", port=0, home=a).start()
    listener.close()
    assert hub_mail_net.listener_status(a)["running"] is False
    assert not hub_mail_net.pidfile_path(a).exists()


def test_start_status_stop_run_one_listener_process_per_home(world):
    a = world.machine("machine_a", "alpha", (HUB_A, "geoai"))
    started = hub_mail_net.start_listener(bind_host="127.0.0.1", port=0, home=a)
    try:
        assert started["ok"] is True and started["running"] is True, started
        assert started["pid"] != os.getpid() and started["address"].startswith("127.0.0.1:")
        status = hub_mail_net.listener_action("status", home=a)
        assert status["running"] is True and status["enabled"] is True
        again = hub_mail_net.start_listener(bind_host="127.0.0.1", port=0, home=a)
        assert again["already_running"] is True and again["pid"] == started["pid"]
    finally:
        stopped = hub_mail_net.stop_listener(a)
    assert stopped["stopped"] is True, stopped
    final = hub_mail_net.listener_status(a)
    assert final["running"] is False and final["enabled"] is False


def test_the_listener_records_the_start_token_and_its_start_identity(world):
    a = world.machine("machine_a", "alpha", (HUB_A, "geoai"))
    token = secrets.token_hex(16)
    world.listen(a, start_token=token)
    record = json.loads(hub_mail_net.pidfile_path(a).read_text(encoding="utf-8"))
    assert record["token"] == token and record["pid"] == os.getpid()
    assert record["start_ticks"] == runtime_temp.process_start_ticks(os.getpid())
    assert hub_mail_net.listener_status(a)["running"] is True


def test_a_stale_pidfile_with_a_recycled_pid_is_never_signalled(world, monkeypatch):
    a = world.machine("machine_a", "alpha", (HUB_A, "geoai"))
    own_ticks = runtime_temp.process_start_ticks(os.getpid())
    stale = {
        "schema_id": hub_mail_net.LISTENER_SCHEMA_ID, "pid": os.getpid(),
        "start_ticks": (own_ticks or 0) + 1, "token": "0" * 32,
        "bind_host": "127.0.0.1", "port": 9, "address": "127.0.0.1:9",
    }

    def write_stale() -> None:
        path = hub_mail_net.pidfile_path(a)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stale), encoding="utf-8")

    def forbidden_kill(*args):
        raise AssertionError(f"os.kill called for a recycled pid: {args}")

    monkeypatch.setattr(hub_mail_net.os, "kill", forbidden_kill)
    write_stale()
    status = hub_mail_net.listener_status(a)
    assert status["running"] is False and status["stale_pidfile"] is True and status["pid"] == 0
    stopped = hub_mail_net.stop_listener(a)
    assert stopped["stopped"] is False and not hub_mail_net.pidfile_path(a).exists()

    write_stale()
    started = hub_mail_net.start_listener(bind_host="127.0.0.1", port=0, home=a)
    try:
        assert started["ok"] is True and started["running"] is True, started
        assert started["pid"] != os.getpid()
    finally:
        assert hub_mail_net.stop_listener(a)["stopped"] is True


def _fake_udp_route(monkeypatch, answer: str) -> list:
    dialed: list = []

    class FakeUDP:
        def __init__(self, family, kind):
            assert kind == socket.SOCK_DGRAM
            self.family = family

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def connect(self, target):
            dialed.append((self.family, target))

        def getsockname(self):
            return (answer, 50000)

    monkeypatch.setattr(hub_mail_net.socket, "socket", FakeUDP)
    return dialed


def test_a_wildcard_bind_advertises_the_routed_interface_not_127_0_1_1(monkeypatch):
    monkeypatch.setattr(hub_mail_net.socket, "gethostbyname", lambda name: "127.0.1.1")
    dialed = _fake_udp_route(monkeypatch, "192.168.1.20")
    assert hub_mail_net._advertised_host("0.0.0.0") == "192.168.1.20"
    assert dialed == [(socket.AF_INET, ("192.0.2.1", 9))]


def test_a_wildcard_bind_with_only_loopback_advertises_nothing_and_pairing_refuses(world, monkeypatch):
    a = world.machine("machine_a", "alpha", (HUB_A, "geoai"))
    b = world.machine("machine_b", "bravo", (HUB_B, "aiworkhub"))
    code = hub_mail_net.pair_begin(b)["code"]
    monkeypatch.setattr(hub_mail_net.socket, "gethostbyname", lambda name: "127.0.1.1")
    _fake_udp_route(monkeypatch, "127.0.0.1")
    assert hub_mail_net._advertised_host("0.0.0.0") == ""
    _fake_udp_route(monkeypatch, "::1")
    assert hub_mail_net._advertised_host("::") == ""

    hub_mail_net._claim_pidfile({
        "schema_id": hub_mail_net.LISTENER_SCHEMA_ID, "pid": os.getpid(),
        "start_ticks": runtime_temp.process_start_ticks(os.getpid()), "token": secrets.token_hex(16),
        "bind_host": "0.0.0.0", "port": 9, "address": "",
    }, a)
    assert hub_mail_net.listener_status(a)["running"] is True
    assert hub_mail_net.pair_complete("127.0.0.1:9", code, home=a) == {
        "ok": False, "error": "listener_address_unroutable",
    }


def test_an_explicit_bind_host_is_advertised_unchanged(monkeypatch):
    monkeypatch.setattr(hub_mail_net.socket, "gethostbyname", lambda name: "127.0.1.1")
    for host in ("192.168.1.5", "127.0.0.1", "fd00::5"):
        assert hub_mail_net._advertised_host(host) == host


def test_a_v6_wildcard_never_advertises_an_ipv4_address(monkeypatch):
    monkeypatch.setattr(hub_mail_net.socket, "gethostbyname", lambda name: "192.168.1.20")
    _fake_udp_route(monkeypatch, "")
    assert hub_mail_net._advertised_host("::") == ""
    assert hub_mail_net._advertised_host("0.0.0.0") == "192.168.1.20"
    _fake_udp_route(monkeypatch, "fd00::7")
    assert hub_mail_net._advertised_host("::") == "fd00::7"


def test_a_pinned_peer_refuses_a_mismatched_certificate(monkeypatch):
    presented = b"some other certificate"

    class FakeTLSConnection:
        def __init__(self, host, port, timeout=None, context=None):
            self.sock = None

        def connect(self):
            self.sock = SimpleNamespace(getpeercert=lambda binary_form=False: presented)

        def close(self):
            self.sock = None

    monkeypatch.setattr(hub_mail_net.http.client, "HTTPSConnection", FakeTLSConnection)
    with pytest.raises(hub_mail_net.PeerRefused, match="tls_cert_mismatch"):
        hub_mail_net._open("127.0.0.1:9", tls=True, pin="0" * 64, timeout=1)
    _, observed = hub_mail_net._open(
        "127.0.0.1:9", tls=True, pin=hashlib.sha256(presented).hexdigest(), timeout=1
    )
    assert observed == hashlib.sha256(presented).hexdigest()


def _self_signed(directory: Path, name: str) -> tuple[str, str]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl CLI unavailable to mint a throwaway test certificate")
    cert, key = directory / f"{name}.pem", directory / f"{name}.key"
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
         "-out", str(cert), "-days", "1", "-subj", "/CN=aiworkhub-test"],
        check=True, capture_output=True, timeout=120,
    )
    return str(cert), str(key)


def test_tls_pairing_pins_the_certificate_and_refuses_a_swapped_one(world, tmp_path):
    first, second = _self_signed(tmp_path, "first"), _self_signed(tmp_path, "second")
    a = world.machine("machine_a", "alpha", (HUB_A, "geoai"))
    b = world.machine("machine_b", "bravo", (HUB_B, "aiworkhub"))
    world.listen(a)
    listener_b = world.listen(b, tls_cert=first[0], tls_key=first[1])
    _pair(a, b, listener_b, tls=True)
    [peer] = hub_mail_net.peers(a)["peers"]
    assert peer["tls_cert_sha256"] == hub_mail_net.cert_sha256(first[0])
    assert _send(a, HUB_A, HUB_B)["delivery"] == "delivered"

    port = int(listener_b.record["port"])
    listener_b.close()
    world.listen(b, port=port, tls_cert=second[0], tls_key=second[1])
    [result] = hub_mail_net.refresh_peers(a)["refreshed"]
    assert result == {"machine_id": peer["machine_id"], "ok": False, "error": "tls_cert_mismatch"}
    queued = _send(a, HUB_A, HUB_B)
    assert queued["delivery"] == "queued" and queued["last_error"] == "tls_cert_mismatch"


_NET_TOOLS = (
    "aiworkhub_manager_hub_mail_pair_begin",
    "aiworkhub_manager_hub_mail_pair_complete",
    "aiworkhub_manager_hub_mail_peers",
    "aiworkhub_manager_hub_mail_listener",
)


def test_new_mcp_tools_document_every_parameter():
    for name in _NET_TOOLS:
        function = getattr(server, name)
        doc = inspect.getdoc(function) or ""
        assert "Args:" in doc, name
        args = doc.split("Args:", 1)[1]
        for parameter in inspect.signature(function).parameters:
            assert f"{parameter}:" in args, (name, parameter)


def test_manager_seat_holds_only_the_peers_read():
    assert "aiworkhub_manager_hub_mail_peers" in ra.MANAGER_SEAT_MCP_TOOLS
    writes = set(_NET_TOOLS) - {"aiworkhub_manager_hub_mail_peers"}
    assert not writes & set(ra.MANAGER_SEAT_MCP_TOOLS)
    seat = set(ra.resolve_manager_seat_codex_tools())
    assert "aiworkhub_manager_hub_mail_peers" in seat and not writes & seat


def _manager_route(root: Path) -> dict:
    return {
        "ok": True,
        "role": "manager",
        "repo": str(root),
        "manager_route": {"provider": "codex", "thread_id": "00000000-0000-4000-8000-000000000001"},
    }


def test_mcp_tools_are_gated_by_the_write_gate_and_the_verified_route(world, monkeypatch):
    root = world.register(world.default_home, HUB_A, "geoai")
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(root))
    monkeypatch.setattr(core, "writes_allowed", lambda: False)
    closed = [
        server.aiworkhub_manager_hub_mail_pair_begin(),
        server.aiworkhub_manager_hub_mail_pair_complete(address="127.0.0.1:9", code="x"),
        server.aiworkhub_manager_hub_mail_listener(action="start", bind_host="127.0.0.1", port=0),
        server.aiworkhub_manager_hub_mail_listener(action="stop"),
    ]
    assert [result["error"] for result in closed] == ["write_gate_closed"] * 4
    assert not hub_mail_net.pairing_path().exists()
    listing = server.aiworkhub_manager_hub_mail_peers()
    assert listing["ok"] is True and listing["peers"] == []
    assert server.aiworkhub_manager_hub_mail_listener(action="status")["running"] is False

    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    begun = server.aiworkhub_manager_hub_mail_pair_begin(machine_label="desk\x07")
    assert begun["ok"] is True and begun["machine_label"] == "desk"
    assert server.aiworkhub_manager_hub_mail_listener(action="start") == {
        "ok": False, "error": "bind_host_required",
    }
    assert server.aiworkhub_manager_hub_mail_listener(action="restart")["error"] == "listener_action_invalid"

    monkeypatch.setattr(
        core, "manager_bootstrap",
        lambda: {"ok": True, "role": "worker_or_unverified_client", "manager_route": {}},
    )
    for result in (
        server.aiworkhub_manager_hub_mail_peers(),
        server.aiworkhub_manager_hub_mail_pair_begin(),
        server.aiworkhub_manager_hub_mail_listener(action="status"),
    ):
        assert result["error"] == "verified_manager_identity_required"
