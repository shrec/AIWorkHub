"""WorkHub Mail end to end: discover or enter an address -> pair -> accept -> send -> inbox.

Two simulated machines, each its own tmp home with its own listener on
127.0.0.1, so nothing leaves the loopback interface and no real
``~/.aiworkhub`` is touched. No step takes a typed secret: the only value the
owners compare is the 6-digit SAS that both screens show.
"""

from __future__ import annotations

import inspect
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import hub_mail, hub_mail_net, shared_router  # noqa: E402

HUB_A = "repo_" + "a" * 32
HUB_B = "repo_" + "b" * 32
LOOPBACK = "127.0.0.1"


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
    monkeypatch.setattr(hub_mail_net, "_REDISCOVER_ATTEMPTS", {})
    opened: list[hub_mail_net.Listener] = []

    def machine(name: str, label: str, hub_id: str) -> Path:
        home = tmp_path / name
        home.mkdir()
        root = tmp_path / "repos" / f"{name}-{hub_id[-6:]}"
        root.mkdir(parents=True)
        roots[str(root.resolve())] = hub_id
        registry = shared_router.registry_dir(home)
        registry.mkdir(parents=True, exist_ok=True)
        (registry / f"{hub_id}.json").write_text(
            json.dumps({"schema_id": shared_router.SCHEMA_ID, "repo_id": hub_id, "repo_root": str(root),
                        "extension_host_pid": 0, "repo_name": name}),
            encoding="utf-8",
        )
        hub_mail_net.set_machine_label(label, home)
        return home

    def listen(home: Path) -> hub_mail_net.Listener:
        listener = hub_mail_net.Listener(
            bind_host=LOOPBACK, port=0, home=home, retry_interval=3600, discovery_port=0
        )
        listener.start()
        opened.append(listener)
        return listener

    yield SimpleNamespace(machine=machine, listen=listen)
    for listener in opened:
        listener.close()


def _local(hub_id: str) -> dict[str, str]:
    return {"hub_id": hub_id, "repo_root": f"/repos/{hub_id}", "aiworkhub_version": "test"}


def _send(home: Path, sender: str, to: str, subject: str = "hello") -> dict:
    return hub_mail.send(local=_local(sender), to_hub_id=to, kind="problem_report", subject=subject,
                         body="details", home=home)


def _record_wire(monkeypatch) -> list[bytes]:
    wire: list[bytes] = []
    real_exchange = hub_mail_net._exchange

    def recording(address, method, path, body, headers, **options):
        wire.extend([path.encode(), body, json.dumps(headers).encode()])
        status, reply_headers, data, observed = real_exchange(address, method, path, body, headers, **options)
        wire.extend([data, json.dumps(reply_headers).encode()])
        return status, reply_headers, data, observed

    monkeypatch.setattr(hub_mail_net, "_exchange", recording)
    return wire


def test_no_step_of_the_flow_takes_a_typed_secret():
    assert not hasattr(hub_mail_net, "pair_begin") and not hasattr(hub_mail_net, "pair_complete")
    assert set(inspect.signature(hub_mail_net.pair).parameters) == {"address", "home", "tls"}
    assert set(inspect.signature(hub_mail_net.accept).parameters) == {"request_id", "home"}
    assert set(inspect.signature(hub_mail_net.reject).parameters) == {"request_id", "home"}
    assert set(inspect.signature(hub_mail_net.discover).parameters) == {"timeout", "targets", "home"}


def test_discover_pair_accept_send_inbox_between_two_homes_with_zero_typed_secrets(world, monkeypatch):
    a = world.machine("a", "Alpha", HUB_A)
    b = world.machine("b", "Beta", HUB_B)
    world.listen(a)
    listener_b = world.listen(b)
    wire = _record_wire(monkeypatch)

    found = hub_mail_net.discover(0.5, targets=[(LOOPBACK, listener_b.record["discovery_port"])], home=a)
    assert found["ok"] is True and found["count"] == 1 and found["untrusted_hints"] is True
    [entry] = found["found"]
    assert entry["machine_id"] == hub_mail_net.machine_id(b) and entry["paired"] is False
    assert entry["address"] == listener_b.address

    started = hub_mail_net.pair(entry["address"], home=a)
    assert started["ok"] is True and re.fullmatch(r"[0-9]{6}", started["sas"]), started
    assert not {"code", "secret", "password"} & set(started)
    [incoming] = hub_mail_net.pair_requests(b)["requests"]
    assert incoming["direction"] == "incoming" and incoming["sas"] == started["sas"]
    assert hub_mail_net.peers(a)["count"] == 0 and hub_mail_net.peers(b)["count"] == 0

    accepted = hub_mail_net.accept(incoming["request_id"], home=b)
    assert accepted["ok"] is True and accepted["confirm"] == "delivered", accepted
    assert hub_mail_net.peers(a)["count"] == 1 and hub_mail_net.peers(b)["count"] == 1

    sent = _send(a, HUB_A, HUB_B, "from alpha")
    assert sent["ok"] is True and sent["delivery"] == "delivered", sent
    [mail] = hub_mail.inbox(local_hub_id=HUB_B, home=b)["mails"]
    assert mail["subject"] == "from alpha" and mail["from_node"]["machine_id"] == hub_mail_net.machine_id(a)
    reply = _send(b, HUB_B, HUB_A, "from beta")
    assert reply["ok"] is True and reply["delivery"] == "delivered", reply
    assert [m["subject"] for m in hub_mail.inbox(local_hub_id=HUB_A, home=a)["mails"]] == ["from beta"]

    secret = hub_mail_net.load_peer(hub_mail_net.machine_id(b), a)["secret"]
    assert secret == hub_mail_net.load_peer(hub_mail_net.machine_id(a), b)["secret"] and len(secret) == 64
    assert len(wire) >= 15
    for blob in wire:
        assert secret.encode() not in blob
        assert b'"code"' not in blob
    assert "/v1/pair/offer" in b"".join(wire).decode(errors="replace")
    assert secret not in json.dumps([found, started, accepted, hub_mail_net.peers(a), hub_mail_net.peers(b)])


def test_a_manually_entered_host_port_pairs_without_any_discovery(world, monkeypatch):
    a = world.machine("a", "Alpha", HUB_A)
    b = world.machine("b", "Beta", HUB_B)
    world.listen(a)
    listener_b = world.listen(b)
    monkeypatch.setattr(hub_mail_net, "DISCOVERY_TARGETS", [])

    started = hub_mail_net.pair(f"{LOOPBACK}:{listener_b.record['port']}", home=a)
    assert started["ok"] is True and started["peer"]["address"] == listener_b.address
    accepted = hub_mail_net.accept(started["request_id"], home=b)
    assert accepted["ok"] is True and accepted["confirm"] == "delivered", accepted

    assert _send(a, HUB_A, HUB_B)["delivery"] == "delivered"
    assert len(hub_mail.inbox(local_hub_id=HUB_B, home=b)["mails"]) == 1


def test_a_rejected_request_leaves_no_peer_and_mail_has_nowhere_to_go(world):
    a = world.machine("a", "Alpha", HUB_A)
    b = world.machine("b", "Beta", HUB_B)
    world.listen(a)
    listener_b = world.listen(b)

    started = hub_mail_net.pair(listener_b.address, home=a)
    assert started["ok"] is True
    rejected = hub_mail_net.reject(started["request_id"], home=b)
    assert rejected["ok"] is True, rejected
    assert hub_mail_net.pair_requests(b)["requests"] == []
    assert hub_mail_net.peers(a)["count"] == 0 and hub_mail_net.peers(b)["count"] == 0
    assert _send(a, HUB_A, HUB_B) == {"ok": False, "error": "unknown_recipient"}
    assert hub_mail_net.accept(started["request_id"], home=b)["ok"] is False


def test_forgetting_a_machine_ends_the_pairing_on_that_side(world):
    a = world.machine("a", "Alpha", HUB_A)
    b = world.machine("b", "Beta", HUB_B)
    world.listen(a)
    listener_b = world.listen(b)
    started = hub_mail_net.pair(listener_b.address, home=a)
    assert hub_mail_net.accept(started["request_id"], home=b)["confirm"] == "delivered"
    assert _send(a, HUB_A, HUB_B)["delivery"] == "delivered"

    forgotten = hub_mail_net.forget_peer(hub_mail_net.machine_id(b), home=a)
    assert forgotten["ok"] is True and hub_mail_net.peers(a)["count"] == 0
    assert _send(a, HUB_A, HUB_B) == {"ok": False, "error": "unknown_recipient"}
