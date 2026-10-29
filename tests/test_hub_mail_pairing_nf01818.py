"""NF-2026-01818: code-free WorkHub Mail pairing, discovery and the auto listener.

Every simulated machine is its own tmp home with its own listener on
127.0.0.1; discovery probes explicit loopback targets, so nothing leaves the
machine and no real ``~/.aiworkhub`` is touched.
"""

from __future__ import annotations

import json
import re
import secrets
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import hub_mail, hub_mail_net, hub_mail_pairing, shared_router  # noqa: E402

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

    def machine(name: str, label: str, hub_id: str | None = None) -> Path:
        home = tmp_path / name
        home.mkdir(exist_ok=True)
        if hub_id is not None:
            root = tmp_path / "repos" / f"{name}-{hub_id[-6:]}"
            root.mkdir(parents=True)
            roots[str(root.resolve())] = hub_id
            registry = shared_router.registry_dir(home)
            registry.mkdir(parents=True, exist_ok=True)
            (registry / f"{hub_id}.json").write_text(
                json.dumps({"schema_id": shared_router.SCHEMA_ID, "repo_id": hub_id,
                            "repo_root": str(root), "extension_host_pid": 0, "repo_name": name}),
                encoding="utf-8",
            )
        hub_mail_net.set_machine_label(label, home)
        return home

    def listen(home: Path, **options) -> hub_mail_net.Listener:
        options.setdefault("discovery_port", 0)
        listener = hub_mail_net.Listener(
            bind_host=LOOPBACK, port=0, home=home, retry_interval=3600, **options
        )
        listener.start()
        opened.append(listener)
        return listener

    yield SimpleNamespace(machine=machine, listen=listen, tmp_path=tmp_path)
    for listener in opened:
        listener.close()


def _two(world):
    a = world.machine("a", "Alpha", HUB_A)
    b = world.machine("b", "Beta", HUB_B)
    return a, b, world.listen(a), world.listen(b)


def _targets(listener: hub_mail_net.Listener) -> list[tuple[str, int]]:
    return [(LOOPBACK, listener.record["discovery_port"])]


def _pair_both(a: Path, b: Path, listener_b: hub_mail_net.Listener) -> dict:
    found = hub_mail_net.discover(0.5, targets=_targets(listener_b), home=a)
    assert found["ok"] is True and found["count"] == 1, found
    started = hub_mail_net.pair(found["found"][0]["address"], home=a)
    assert started["ok"] is True, started
    accepted = hub_mail_net.accept(started["request_id"], home=b)
    assert accepted["ok"] is True and accepted["confirm"] == "delivered", accepted
    return started


def _send(home: Path, sender: str, to: str) -> dict:
    local = {"hub_id": sender, "repo_root": f"/repos/{sender}", "aiworkhub_version": "test"}
    return hub_mail.send(local=local, to_hub_id=to, kind="problem_report", subject="hello",
                         body="details", home=home)


def _inbox(home: Path, hub_id: str) -> list[str]:
    directory = hub_mail.box_dir(hub_id, "inbox", home)
    return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []


def _pending_files(home: Path) -> list[str]:
    directory = hub_mail_pairing.requests_dir(home)
    return sorted(p.name for p in directory.glob("*.json")) if directory.is_dir() else []


def _limiter(limit: int = 100) -> hub_mail_pairing.RateLimiter:
    return hub_mail_pairing.RateLimiter(limit, 60.0)


def _offer_request(public_hex: str, nonce: str, *, machine_id: str | None = None, commit: str | None = None) -> dict:
    return {
        "proto": hub_mail_pairing.PROTO,
        "machine_id": machine_id or secrets.token_hex(16),
        "machine_label": "Mallory",
        "port": 4000,
        "commit": commit or hub_mail_pairing.commit_of(public_hex, nonce),
        "tls_cert_sha256": "",
    }


def _manual_offer(home: Path, *, source: str = LOOPBACK, limiter=None, **overrides):
    private, public = hub_mail_pairing.dh_keypair()
    public_hex, nonce = hub_mail_pairing.encode_public(public), secrets.token_hex(16)
    request = {**_offer_request(public_hex, nonce), **overrides}
    status, reply = hub_mail_pairing.handle_offer(
        request, source=source, home=home, limiter=limiter or _limiter(), own_tls="",
    )
    return status, reply, public_hex, nonce


# ---------------------------------------------------------------- crypto


def test_ffdhe2048_group_is_the_rfc_7919_safe_prime():
    p, q = hub_mail_pairing.FFDHE2048_P, hub_mail_pairing.FFDHE2048_Q
    assert p.bit_length() == 2048 and p % 8 == 7
    assert hex(p).startswith("0xffffffffffffffffadf85458a2bb4a9a")
    assert hex(p).endswith("886b423861285c97ffffffffffffffff")
    assert pow(2, q, p) == 1
    assert all(pow(base, p - 1, p) == 1 for base in (2, 3, 5))
    assert all(pow(base, q, p) in {1, p - 1} for base in (3, 5, 7))


def test_hkdf_matches_the_rfc_5869_vector():
    okm = hub_mail_pairing.hkdf_sha256(
        bytes.fromhex("0b" * 22), bytes.fromhex("000102030405060708090a0b0c"),
        bytes.fromhex("f0f1f2f3f4f5f6f7f8f9"), 42,
    )
    assert okm.hex() == (
        "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"
    )


def test_sas_is_six_digits_and_binds_every_transcript_field():
    a, b = hub_mail_pairing.dh_keypair(), hub_mail_pairing.dh_keypair()
    one = hub_mail_pairing.party("1" * 32, "A", "")
    two = hub_mail_pairing.party("2" * 32, "B", "")
    fields = {"pk_a": hub_mail_pairing.encode_public(a[1]), "pk_b": hub_mail_pairing.encode_public(b[1]),
              "nonce_a": "a" * 32, "nonce_b": "b" * 32}
    bound = hub_mail_pairing.transcript(one, two, **fields)
    secret_a, sas_a = hub_mail_pairing.derive(a[0], b[1], bound)
    secret_b, sas_b = hub_mail_pairing.derive(b[0], a[1], bound)
    assert (secret_a, sas_a) == (secret_b, sas_b)
    assert re.fullmatch(r"[0-9]{6}", sas_a) and len(secret_a) == 32
    for changed in (
        hub_mail_pairing.transcript(one, hub_mail_pairing.party("2" * 32, "Eve", ""), **fields),
        hub_mail_pairing.transcript(one, two, **{**fields, "nonce_b": "c" * 32}),
        hub_mail_pairing.transcript(two, one, **fields),
    ):
        assert hub_mail_pairing.derive(a[0], b[1], changed)[0] != secret_a


# ---------------------------------------------------------------- end to end


def test_two_hubs_discover_pair_accept_and_exchange_mail_without_a_code(world):
    a, b, listener_a, listener_b = _two(world)
    assert not hub_mail_net.pair_requests(b)["requests"]
    found = hub_mail_net.discover(0.5, targets=_targets(listener_b), home=a)
    entry = found["found"][0]
    assert entry["machine_id"] == hub_mail_net.machine_id(b) and entry["label"] == "Beta"
    assert entry["port"] == listener_b.record["port"] and entry["paired"] is False
    assert found["untrusted_hints"] is True

    started = hub_mail_net.pair(entry["address"], home=a)
    assert started["ok"] is True and re.fullmatch(r"[0-9]{6}", started["sas"])
    incoming = hub_mail_net.pair_requests(b)["requests"]
    assert len(incoming) == 1 and incoming[0]["direction"] == "incoming"
    assert incoming[0]["sas"] == started["sas"] and incoming[0]["request_id"] == started["request_id"]
    assert hub_mail_net.pair_requests(a)["requests"][0]["direction"] == "outgoing"
    assert hub_mail_net.peers(a)["count"] == 0 and hub_mail_net.peers(b)["count"] == 0

    accepted = hub_mail_net.accept(started["request_id"], home=b)
    assert accepted["ok"] is True and accepted["confirm"] == "delivered"
    stored_a = hub_mail_net.load_peer(hub_mail_net.machine_id(b), a)
    stored_b = hub_mail_net.load_peer(hub_mail_net.machine_id(a), b)
    assert stored_a["secret"] == stored_b["secret"] and len(stored_a["secret"]) == 64
    assert stored_a["address"] == listener_b.address and stored_b["address"] == listener_a.address
    assert [hub["hub_id"] for hub in stored_a["hubs"]] == [HUB_B]
    assert [hub["hub_id"] for hub in stored_b["hubs"]] == [HUB_A]
    assert "secret" not in json.dumps([started, accepted, hub_mail_net.peers(a)])
    assert not _pending_files(a) and not _pending_files(b)

    sent = _send(a, HUB_A, HUB_B)
    assert sent["ok"] is True, sent
    assert len(_inbox(b, HUB_B)) == 1
    assert hub_mail_net.discover(0.5, targets=_targets(listener_b), home=a)["found"][0]["paired"] is True


def test_rejecting_a_request_stores_no_peer_and_accept_then_refuses(world):
    a, b, _, listener_b = _two(world)
    found = hub_mail_net.discover(0.5, targets=_targets(listener_b), home=a)
    started = hub_mail_net.pair(found["found"][0]["address"], home=a)
    assert hub_mail_net.reject(started["request_id"], home=b) == {
        "ok": True, "rejected": True, "request_id": started["request_id"],
    }
    assert not _pending_files(b)
    assert hub_mail_net.accept(started["request_id"], home=b) == {"ok": False, "error": "pair_request_unknown"}
    assert hub_mail_net.reject(started["request_id"], home=b)["error"] == "pair_request_unknown"
    assert hub_mail_net.peers(a)["count"] == 0 and hub_mail_net.peers(b)["count"] == 0


def test_a_relay_substituting_its_dh_value_changes_the_sas_and_nothing_is_stored(world, monkeypatch):
    a, b, _, listener_b = _two(world)
    _, relay_public = hub_mail_pairing.dh_keypair()
    relay_hex = hub_mail_pairing.encode_public(relay_public)
    real_exchange = hub_mail_net._exchange

    def relay(address, method, path, body, headers, **options):
        status, reply_headers, data, observed = real_exchange(address, method, path, body, headers, **options)
        if path == "/v1/pair/offer":
            data = hub_mail_net._canonical({**hub_mail_net._json_object(data), "pk": relay_hex})
        return status, reply_headers, data, observed

    monkeypatch.setattr(hub_mail_net, "_exchange", relay)
    found = hub_mail_net.discover(0.5, targets=_targets(listener_b), home=a)
    started = hub_mail_net.pair(found["found"][0]["address"], home=a)
    assert started["ok"] is True
    shown_on_b = hub_mail_net.pair_requests(b)["requests"][0]["sas"]
    assert shown_on_b != started["sas"]

    accepted = hub_mail_net.accept(started["request_id"], home=b)
    assert accepted["ok"] is False and accepted["error"].endswith("pair_confirm_invalid"), accepted
    assert hub_mail_net.peers(a)["count"] == 0 and hub_mail_net.peers(b)["count"] == 0
    assert not _pending_files(a) and not _pending_files(b)


# ---------------------------------------------------------------- refusals


def test_a_commit_that_does_not_match_the_reveal_is_refused_and_stores_nothing(world):
    b = world.machine("b", "Beta")
    status, reply, public_hex, nonce = _manual_offer(b, commit=hub_mail_pairing.commit_of("00" * 256, "11" * 16))
    assert status == 200
    other_nonce = secrets.token_hex(16)
    refused = hub_mail_pairing.handle_reveal(
        {"request_id": reply["request_id"], "pk": public_hex, "nonce": other_nonce}, home=b,
    )
    assert refused == (403, {"ok": False, "error": "pair_commit_mismatch"})
    assert not _pending_files(b) and hub_mail_net.peers(b)["count"] == 0
    assert hub_mail_net.accept(reply["request_id"], home=b)["error"] == "pair_request_unknown"


def _outside_subgroup() -> int:
    p, q = hub_mail_pairing.FFDHE2048_P, hub_mail_pairing.FFDHE2048_Q
    return next(y for y in range(3, 100) if pow(y, q, p) != 1)


@pytest.mark.parametrize(
    "value",
    [
        0,
        1,
        hub_mail_pairing.FFDHE2048_P - 1,
        hub_mail_pairing.FFDHE2048_P,
        hub_mail_pairing.FFDHE2048_P + 5,
        _outside_subgroup(),
    ],
    ids=["zero", "one", "p-1", "p", "above-p", "outside-subgroup"],
)
def test_a_dh_value_outside_the_group_is_refused_and_stores_nothing(world, value):
    b = world.machine("b", "Beta")
    status, reply, _, _ = _manual_offer(b)
    assert status == 200
    nonce = secrets.token_hex(16)
    public_hex = value.to_bytes(256, "big").hex()
    refused = hub_mail_pairing.handle_reveal(
        {"request_id": reply["request_id"], "pk": public_hex, "nonce": nonce}, home=b,
    )
    assert refused == (400, {"ok": False, "error": "pair_dh_value_invalid"})
    assert not _pending_files(b) and hub_mail_net.peers(b)["count"] == 0


def test_a_malformed_dh_value_is_refused(world):
    b = world.machine("b", "Beta")
    _, reply, _, _ = _manual_offer(b)
    refused = hub_mail_pairing.handle_reveal(
        {"request_id": reply["request_id"], "pk": "zz" * 256, "nonce": secrets.token_hex(16)}, home=b,
    )
    assert refused[0] == 400 and not _pending_files(b)


def test_a_request_past_its_ttl_is_refused_and_deleted(world, monkeypatch):
    b = world.machine("b", "Beta")
    status, reply, public_hex, nonce = _manual_offer(b)
    assert status == 200 and len(_pending_files(b)) == 1
    later = time.time() + hub_mail_pairing.REQUEST_TTL_SECONDS + 1
    monkeypatch.setattr(hub_mail_net, "_clock", lambda: later)
    refused = hub_mail_pairing.handle_reveal(
        {"request_id": reply["request_id"], "pk": public_hex, "nonce": nonce}, home=b,
    )
    assert refused == (404, {"ok": False, "error": "pair_request_unknown"})
    assert not _pending_files(b) and hub_mail_net.peers(b)["count"] == 0


def test_an_expired_revealed_request_cannot_be_accepted(world, monkeypatch):
    b = world.machine("b", "Beta")
    _, reply, public_hex, nonce = _manual_offer(b)
    revealed = hub_mail_pairing.handle_reveal(
        {"request_id": reply["request_id"], "pk": public_hex, "nonce": nonce}, home=b,
    )
    assert revealed == (200, {"ok": True, "state": "pending_accept"})
    assert hub_mail_net.pair_requests(b)["requests"][0]["sas"]
    monkeypatch.setattr(hub_mail_net, "_clock", lambda: time.time() + hub_mail_pairing.REQUEST_TTL_SECONDS + 1)
    assert hub_mail_net.accept(reply["request_id"], home=b)["error"] == "pair_request_unknown"
    assert not _pending_files(b) and hub_mail_net.peers(b)["count"] == 0


def test_the_ninth_concurrent_pending_request_is_refused_without_state(world):
    b = world.machine("b", "Beta")
    limiter = _limiter(1000)
    for _ in range(hub_mail_pairing.MAX_PENDING_REQUESTS):
        status, _, _, _ = _manual_offer(b, limiter=limiter)
        assert status == 200
    before = _pending_files(b)
    status, reply, _, _ = _manual_offer(b, limiter=limiter)
    assert (status, reply) == (429, {"ok": False, "error": "too_many_pending_requests"})
    assert _pending_files(b) == before and len(before) == hub_mail_pairing.MAX_PENDING_REQUESTS


def test_the_per_source_rate_limit_refuses_without_state(world):
    b = world.machine("b", "Beta")
    limiter = _limiter(3)
    for _ in range(3):
        assert _manual_offer(b, source="10.0.0.5", limiter=limiter)[0] == 200
    before = _pending_files(b)
    status, reply, _, _ = _manual_offer(b, source="10.0.0.5", limiter=limiter)
    assert (status, reply) == (429, {"ok": False, "error": "rate_limited"})
    assert _pending_files(b) == before
    assert _manual_offer(b, source="10.0.0.6", limiter=limiter)[0] == 200


def test_accept_before_the_reveal_is_refused(world):
    b = world.machine("b", "Beta")
    _, reply, _, _ = _manual_offer(b)
    assert hub_mail_net.accept(reply["request_id"], home=b) == {"ok": False, "error": "pair_not_revealed"}
    assert hub_mail_net.peers(b)["count"] == 0


@pytest.mark.parametrize(
    "overrides",
    [{"proto": 2}, {"port": 0}, {"port": True}, {"machine_id": "nope"}, {"commit": "short"}, {"extra": 1}],
)
def test_a_malformed_offer_is_refused(world, overrides):
    b = world.machine("b", "Beta")
    private, public = hub_mail_pairing.dh_keypair()
    request = {**_offer_request(hub_mail_pairing.encode_public(public), secrets.token_hex(16)), **overrides}
    status, reply = hub_mail_pairing.handle_offer(request, source=LOOPBACK, home=b, limiter=_limiter(), own_tls="")
    assert status == 400 and reply["ok"] is False and not _pending_files(b)


def test_http_pair_steps_refuse_garbage_with_named_errors(world):
    _, b, _, listener_b = _two(world)
    import http.client

    def post(path: str, body: bytes) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(LOOPBACK, listener_b.record["port"], timeout=10)
        try:
            connection.request("POST", path, body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    assert post("/v1/pair/offer", b"not json") == (400, {"error": "pair_request_invalid", "ok": False})
    assert post("/v1/pair/reveal", json.dumps(
        {"request_id": "1" * 32, "pk": "0" * 512, "nonce": "2" * 32}).encode()
    ) == (404, {"error": "pair_request_unknown", "ok": False})
    assert post("/v1/pair/confirm", b"{}")[0] == 400
    assert not _pending_files(b)


# ---------------------------------------------------------------- listener


def test_two_contenders_on_one_home_only_one_binds_the_other_is_standby(world):
    home = world.machine("a", "Alpha")
    active = world.listen(home)
    contender = hub_mail_net.Listener(bind_host=LOOPBACK, port=0, home=home, retry_interval=3600)
    started = contender.start(standby_ok=True)
    assert started["state"] == "standby" and started["bound"] is False and started["port"] == 0
    assert started["active_port"] == active.record["port"]
    assert contender.state == "standby" and contender._server is None and contender._discovery is None
    assert hub_mail_net.listener_status(home)["port"] == active.record["port"]
    with pytest.raises(hub_mail.HubMailError, match="listener_already_running"):
        hub_mail_net.Listener(bind_host=LOOPBACK, port=0, home=home).start()
    contender.close()
    assert hub_mail_net.listener_status(home)["running"] is True
    active.close()
    successor = hub_mail_net.Listener(bind_host=LOOPBACK, port=0, home=home, retry_interval=3600)
    assert successor.start(standby_ok=True)["port"] > 0
    successor.close()


def test_a_busy_port_falls_back_to_a_free_one_and_status_reports_it(world):
    home = world.machine("a", "Alpha")
    busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        busy.bind((LOOPBACK, 0))
        busy.listen(1)
        taken = busy.getsockname()[1]
        listener = hub_mail_net.Listener(bind_host=LOOPBACK, port=taken, home=home, retry_interval=3600)
        record = listener.start()
        try:
            status = hub_mail_net.listener_status(home)
            assert record["port_fallback"] is True and record["requested_port"] == taken
            assert status["port"] == record["port"] != taken and status["port_fallback"] is True
        finally:
            listener.close()
    finally:
        busy.close()


def test_a_fresh_install_opens_no_port_until_first_use(world, monkeypatch):
    home = world.machine("a", "Alpha")
    spawned: list[dict] = []
    monkeypatch.setattr(hub_mail_net, "start_listener", lambda **kw: spawned.append(kw) or {"ok": True})
    assert hub_mail_net.ensure_listener(home) is None and not spawned
    assert hub_mail_net.listener_status(home)["enabled"] is False

    assert hub_mail_net.mark_enabled(home) is True and hub_mail_net.mark_enabled(home) is False
    assert hub_mail_net.listener_status(home)["enabled"] is True
    hub_mail_net.ensure_listener(home)
    assert spawned == [{"bind_host": "0.0.0.0", "port": hub_mail_pairing.DEFAULT_PORT, "home": home,
                        "tls_cert": None, "tls_key": None}]


def test_the_default_port_is_overridable_by_env_and_config(world, monkeypatch):
    home = world.machine("a", "Alpha")
    spawned: list[dict] = []
    monkeypatch.setattr(hub_mail_net, "start_listener", lambda **kw: spawned.append(kw) or {"ok": True})
    monkeypatch.setenv(hub_mail_pairing.PORT_ENV, "48123")
    hub_mail_net.mark_enabled(home)
    hub_mail_net.ensure_listener(home)
    assert spawned[-1]["port"] == 48123
    hub_mail_net._ENSURE_ATTEMPTS.clear()
    hub_mail_net._write_config(home, enabled=True, bind_host="0.0.0.0", port=49000)
    hub_mail_net.ensure_listener(home)
    assert spawned[-1]["port"] == 49000


def test_an_owner_who_stopped_the_listener_is_not_re_enabled_by_first_use(world):
    home = world.machine("a", "Alpha")
    hub_mail_net._write_config(home, enabled=False)
    assert hub_mail_net.mark_enabled(home) is False
    assert hub_mail_net.listener_status(home)["enabled"] is False
    assert hub_mail_net.ensure_listener(home) is None


def test_send_and_discover_persist_the_enabled_flag(world):
    home = world.machine("a", "Alpha", HUB_A)
    other = world.machine("c", "Gamma")
    assert not hub_mail_net.listener_config_path(home).exists()
    _send(home, HUB_A, HUB_B)
    assert hub_mail_net.listener_status(home)["enabled"] is True
    assert not hub_mail_net.listener_config_path(other).exists()
    hub_mail_net.discover(0.05, targets=[(LOOPBACK, 9)], home=other)
    assert hub_mail_net.listener_status(other)["enabled"] is True


# ---------------------------------------------------------------- discovery


def test_only_an_enabled_listener_answers_a_probe(world):
    a, b, _, listener_b = _two(world)
    assert hub_mail_net.discover(0.4, targets=_targets(listener_b), home=a)["count"] == 1
    hub_mail_net._write_config(b, enabled=False)
    assert hub_mail_net.discover(0.4, targets=_targets(listener_b), home=a)["count"] == 0


def test_discovery_ignores_replies_that_do_not_answer_this_probe(world):
    nonce, own = "a" * 32, "1" * 32
    good = {"p": hub_mail_pairing.DISCOVER_REPLY_ID, "n": nonce, "machine_id": "2" * 32,
            "label": "Eve\x00", "port": 4000, "proto": 1}
    parse = hub_mail_pairing._parse_reply
    assert parse(json.dumps(good).encode(), nonce, own)["label"] == "Eve"
    for bad in (
        {**good, "n": "b" * 32}, {**good, "machine_id": own}, {**good, "port": 0}, {**good, "port": "80"},
        {**good, "p": "other"}, {**good, "extra": 1}, {k: v for k, v in good.items() if k != "proto"},
    ):
        assert parse(json.dumps(bad).encode(), nonce, own) is None
    assert parse(b"x" * 5000, nonce, own) is None and parse(b"[]", nonce, own) is None


def test_a_probe_with_a_bad_shape_gets_no_reply(world):
    home = world.machine("a", "Alpha")
    limiter = _limiter()
    reply = hub_mail_pairing.discovery_reply
    probe = {"p": hub_mail_pairing.DISCOVER_PROBE_ID, "n": "a" * 32}
    answer = reply(json.dumps(probe).encode(), source=LOOPBACK, home=home, port=4000, limiter=limiter)
    assert json.loads(answer)["port"] == 4000
    for bad in (b"junk", json.dumps({**probe, "n": "zz"}).encode(), json.dumps({**probe, "x": 1}).encode(),
                b"x" * 2000):
        assert reply(bad, source=LOOPBACK, home=home, port=4000, limiter=limiter) is None
    tight = _limiter(1)
    assert reply(json.dumps(probe).encode(), source="9.9.9.9", home=home, port=1, limiter=tight) is not None
    assert reply(json.dumps(probe).encode(), source="9.9.9.9", home=home, port=1, limiter=tight) is None


# ---------------------------------------------------------------- peers


def test_a_0_12_76_peers_file_loads_and_send_signs_with_the_unchanged_secret(world):
    a, b, listener_a, listener_b = _two(world)
    secret = secrets.token_hex(32)
    id_a, id_b = hub_mail_net.machine_id(a), hub_mail_net.machine_id(b)

    def legacy(owner: Path, peer_id: str, label: str, address: str, hub_id: str) -> None:
        path = hub_mail_net.peer_path(peer_id, owner)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_id": "aiworkhub.hub_mail.peer.v1", "machine_id": peer_id, "machine_label": label,
            "address": address, "hubs": [{"hub_id": hub_id, "repo_name": label}], "secret": secret,
            "paired_at": "2026-01-01T00:00:00Z", "last_seen": "2026-01-01T00:00:00Z",
            "tls_cert_sha256": "",
        }), encoding="utf-8")

    legacy(a, id_b, "Beta", listener_b.address, HUB_B)
    legacy(b, id_a, "Alpha", listener_a.address, HUB_A)
    assert hub_mail_net.load_peer(id_b, a)["secret"] == secret
    assert hub_mail_net.peers(a)["count"] == 1
    sent = _send(a, HUB_A, HUB_B)
    assert sent["ok"] is True, sent
    assert len(_inbox(b, HUB_B)) == 1
    assert hub_mail_net.load_peer(id_b, a)["secret"] == secret


def test_a_send_after_an_address_change_rediscovers_and_updates_the_stored_address(world, monkeypatch):
    a, b, _, listener_b = _two(world)
    _pair_both(a, b, listener_b)
    id_b = hub_mail_net.machine_id(b)
    stored = hub_mail_net.load_peer(id_b, a)
    hub_mail_net._update_peer(id_b, stored["secret"], a, address=f"{LOOPBACK}:1")
    assert hub_mail_net.load_peer(id_b, a)["address"] == f"{LOOPBACK}:1"
    monkeypatch.setattr(hub_mail_net, "DISCOVERY_TARGETS", _targets(listener_b))

    sent = _send(a, HUB_A, HUB_B)
    assert sent["ok"] is True, sent
    assert len(_inbox(b, HUB_B)) == 1
    refreshed = hub_mail_net.load_peer(id_b, a)
    assert refreshed["address"] == listener_b.address and refreshed["secret"] == stored["secret"]


def test_a_discovery_hint_is_not_trusted_without_the_pairing_secret(world, monkeypatch):
    a, b, _, listener_b = _two(world)
    _pair_both(a, b, listener_b)
    id_b = hub_mail_net.machine_id(b)
    stored = hub_mail_net.load_peer(id_b, a)
    forged = {**stored, "address": f"{LOOPBACK}:1", "secret": secrets.token_hex(32)}
    hub_mail_net._write_json(hub_mail_net.peer_path(id_b, a), forged)
    monkeypatch.setattr(hub_mail_net, "DISCOVERY_TARGETS", _targets(listener_b))

    _send(a, HUB_A, HUB_B)
    assert hub_mail_net.load_peer(id_b, a)["address"] == f"{LOOPBACK}:1"
    assert not _inbox(b, HUB_B)


def test_rediscovery_runs_at_most_once_per_backoff_window(world, monkeypatch):
    a, b, _, listener_b = _two(world)
    _pair_both(a, b, listener_b)
    id_b = hub_mail_net.machine_id(b)
    peer = hub_mail_net.load_peer(id_b, a)
    calls: list[float] = []
    real = hub_mail_pairing.discover

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(hub_mail_pairing, "discover", counting)
    monkeypatch.setattr(hub_mail_net, "DISCOVERY_TARGETS", [(LOOPBACK, 9)])
    dead = {**peer, "address": f"{LOOPBACK}:1"}
    assert hub_mail_net._rediscover(dead, a) is None
    assert hub_mail_net._rediscover(dead, a) is None
    assert len(calls) == 1


@pytest.mark.parametrize("targets", [[(LOOPBACK, 70000)], [(LOOPBACK, "x")], [(LOOPBACK,)], [42], 7])
def test_a_malformed_discovery_target_is_a_named_refusal(world, targets):
    a = world.machine("a", "Alpha")
    assert hub_mail_net.discover(0.1, targets=targets, home=a) == {
        "ok": False, "error": "discover_target_invalid",
    }


def _confirm_lost(monkeypatch, *, delivered: bool) -> None:
    """The first /v1/pair/confirm raises PeerUnreachable, after reaching A when ``delivered``."""

    real_exchange = hub_mail_net._exchange
    lost: list[str] = []

    def exchange(address, method, path, body, headers, **options):
        if path != "/v1/pair/confirm" or lost:
            return real_exchange(address, method, path, body, headers, **options)
        lost.append(path)
        if delivered:
            real_exchange(address, method, path, body, headers, **options)
        raise hub_mail_net.PeerUnreachable("connection_reset")

    monkeypatch.setattr(hub_mail_net, "_exchange", exchange)


def test_a_retried_accept_after_a_lost_confirm_reply_keeps_the_pairing(world, monkeypatch):
    a, b, _, listener_b = _two(world)
    found = hub_mail_net.discover(0.5, targets=_targets(listener_b), home=a)
    started = hub_mail_net.pair(found["found"][0]["address"], home=a)
    _confirm_lost(monkeypatch, delivered=True)

    first = hub_mail_net.accept(started["request_id"], home=b)
    assert first["ok"] is True and first["confirm"] == "pending", first
    assert hub_mail_net.load_peer(hub_mail_net.machine_id(b), a) is not None
    again = hub_mail_net.accept(started["request_id"], home=b)
    assert again["ok"] is True and again["confirm"] == "delivered", again
    assert hub_mail_net.load_peer(hub_mail_net.machine_id(a), b) is not None
    assert not _pending_files(a) and not _pending_files(b)
    assert _send(a, HUB_A, HUB_B)["ok"] is True and len(_inbox(b, HUB_B)) == 1


def test_a_retried_accept_the_initiator_never_stored_still_forgets_the_peer(world, monkeypatch):
    a, b, _, listener_b = _two(world)
    found = hub_mail_net.discover(0.5, targets=_targets(listener_b), home=a)
    started = hub_mail_net.pair(found["found"][0]["address"], home=a)
    _confirm_lost(monkeypatch, delivered=False)

    assert hub_mail_net.accept(started["request_id"], home=b)["confirm"] == "pending"
    assert hub_mail_net.reject(started["request_id"], home=a)["ok"] is True
    again = hub_mail_net.accept(started["request_id"], home=b)
    assert again["ok"] is False and again["error"].endswith("pair_request_unknown"), again
    assert hub_mail_net.load_peer(hub_mail_net.machine_id(a), b) is None
    assert hub_mail_net.load_peer(hub_mail_net.machine_id(b), a) is None
    assert not _pending_files(b)
