"""WorkHub Mail code-free pairing and LAN discovery (NF-2026-01818), stdlib only.

Nothing is typed and no secret or code crosses the network.  The initiator A
and the responder B run commit -> reveal -> SAS over the existing listener:

    A -> B  /v1/pair/offer    {machine_id, label, port, commit = SHA256(pkA || nA)}
    B -> A                    {pkB, nB}
    A -> B  /v1/pair/reveal   {pkA, nA}            B verifies the commit
    B's owner calls accept(request_id) or reject(request_id)
    B -> A  /v1/pair/confirm  MACed with the new secret; A verifies and stores B

Both sides derive ``secret = HKDF-SHA256(g^xy, transcript)`` over the RFC 7919
ffdhe2048 group and show the same 6-digit SAS taken from ``SHA256(transcript)``;
the transcript binds both machine ids, labels, public values and nonces, so a
relay that substitutes a DH value makes the two SAS differ.  Discovery is a UDP
probe answered only by an enabled listener; replies are hints, never trusted.
Pending requests live under the hub home, expire after ``REQUEST_TTL_SECONDS``,
are capped at ``MAX_PENDING_REQUESTS`` and are rate limited per source IP.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import select
import socket
import threading
import time
from pathlib import Path
from typing import Any

from . import hub_mail, hub_mail_net as net

PROTO = 1
DEFAULT_PORT = 47731
PORT_ENV = "AIWORKHUB_HUB_MAIL_PORT"
REQUEST_SCHEMA_ID = "aiworkhub.hub_mail.pair_request.v1"
DISCOVER_PROBE_ID = "aiworkhub.hub_mail.discover.v1"
DISCOVER_REPLY_ID = "aiworkhub.hub_mail.discover.reply.v1"
TRANSCRIPT_CONTEXT = "aiworkhub.hub_mail.pair.transcript.v1"
HKDF_SALT = b"aiworkhub.hub_mail.pair.hkdf.v1"
CONFIRM_CONTEXT = b"aiworkhub.hub_mail.pair.confirm.v1|"
REQUEST_TTL_SECONDS = 600
MAX_PENDING_REQUESTS = 8
MAX_REQUEST_SCAN = 4 * MAX_PENDING_REQUESTS
OFFER_RATE_LIMIT = 10
OFFER_RATE_WINDOW_SECONDS = 60.0
DISCOVER_RATE_LIMIT = 30
DISCOVER_RATE_WINDOW_SECONDS = 10.0
MAX_RATE_SOURCES = 1024
MAX_DATAGRAM_BYTES = 1024
MAX_DISCOVERED = 64
MAX_DISCOVER_TARGETS = 16
DISCOVER_TIMEOUT_SECONDS = 1.5
REDISCOVER_TIMEOUT_SECONDS = 0.6
BROADCAST_ADDRESS = "255.255.255.255"
DH_KEY_BYTES = 256
DH_EXPONENT_BITS = 512
SAS_MODULUS = 10**6
SECRET_BYTES = 32

# RFC 7919 ffdhe2048: a safe prime, g = 2 generates the order-q subgroup.
FFDHE2048_P = int(
    "ffffffffffffffffadf85458a2bb4a9aafdc5620273d3cf1d8b9c583ce2d3695"
    "a9e13641146433fbcc939dce249b3ef97d2fe363630c75d8f681b202aec4617a"
    "d3df1ed5d5fd65612433f51f5f066ed0856365553ded1af3b557135e7f57c935"
    "984f0c70e0e68b77e2a689daf3efe8721df158a136ade73530acca4f483a797a"
    "bc0ab182b324fb61d108a94bb2c8e3fbb96adab760d7f4681d4f42a3de394df4"
    "ae56ede76372bb190b07a7c8ee0a6d709e02fce1cdf7e2ecc03404cd28342f61"
    "9172fe9ce98583ff8e4f1232eef28183c3fe3b1b4c6fad733bb5fcbc2ec22005"
    "c58ef1837d1683b2c6f34a26c1b2effa886b423861285c97ffffffffffffffff",
    16,
)
FFDHE2048_Q = (FFDHE2048_P - 1) // 2
FFDHE2048_G = 2

_PUBLIC_RE = re.compile(r"^[0-9a-f]{512}$")
_OFFER_KEYS = frozenset({"proto", "machine_id", "machine_label", "port", "commit", "tls_cert_sha256"})
_REVEAL_KEYS = frozenset({"request_id", "pk", "nonce"})
_CONFIRM_KEYS = frozenset({"request_id", "machine_id", "machine_label", "hubs", "mac"})
_PROBE_KEYS = frozenset({"p", "n"})
_REPLY_KEYS = frozenset({"p", "n", "machine_id", "label", "port", "proto"})
_REQUEST_LOCK = threading.Lock()


# ---------------------------------------------------------------- crypto


def default_port() -> int:
    """The listener's preferred port: ``AIWORKHUB_HUB_MAIL_PORT`` or 47731."""

    try:
        port = int(os.environ.get(PORT_ENV, ""))
    except ValueError:
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def dh_keypair() -> tuple[int, int]:
    private = secrets.randbits(DH_EXPONENT_BITS) | (1 << (DH_EXPONENT_BITS - 1))
    return private, pow(FFDHE2048_G, private, FFDHE2048_P)


def encode_public(value: int) -> str:
    return value.to_bytes(DH_KEY_BYTES, "big").hex()


def decode_public(text: Any) -> int:
    """A peer's public value from its fixed-width hex form, or a named refusal."""

    if not isinstance(text, str) or not _PUBLIC_RE.fullmatch(text):
        raise hub_mail.HubMailError("pair_dh_value_invalid")
    return int(text, 16)


def check_public(value: int) -> None:
    """Refuse a peer value unless ``1 < y < p-1`` and ``y**q % p == 1``."""

    if not 1 < value < FFDHE2048_P - 1:
        raise hub_mail.HubMailError("pair_dh_value_invalid")
    if pow(value, FFDHE2048_Q, FFDHE2048_P) != 1:
        raise hub_mail.HubMailError("pair_dh_value_invalid")


def commit_of(public_hex: str, nonce_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(public_hex) + bytes.fromhex(nonce_hex)).hexdigest()


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int = SECRET_BYTES) -> bytes:
    pseudo_random_key = hmac.new(salt, ikm, hashlib.sha256).digest()
    output, block, counter = b"", b"", 1
    while len(output) < length:
        block = hmac.new(pseudo_random_key, block + info + bytes([counter]), hashlib.sha256).digest()
        output += block
        counter += 1
    return output[:length]


def party(machine_id: str, label: str, tls_cert_sha256: str) -> dict[str, str]:
    return {"machine_id": machine_id, "machine_label": net.clean_label(label),
            "tls_cert_sha256": tls_cert_sha256}


def transcript(
    initiator: dict[str, str], responder: dict[str, str], *,
    pk_a: str, pk_b: str, nonce_a: str, nonce_b: str,
) -> bytes:
    return net._canonical({
        "context": TRANSCRIPT_CONTEXT, "proto": PROTO, "a": initiator, "b": responder,
        "pk_a": pk_a, "pk_b": pk_b, "n_a": nonce_a, "n_b": nonce_b,
    })


def derive(private: int, peer_public: int, bound: bytes) -> tuple[bytes, str]:
    """``(secret, SAS)`` from our exponent, the peer's checked value and the transcript."""

    shared = pow(peer_public, private, FFDHE2048_P).to_bytes(DH_KEY_BYTES, "big")
    digest = hashlib.sha256(bound).digest()
    sas = f"{int.from_bytes(digest[:8], 'big') % SAS_MODULUS:06d}"
    return hkdf_sha256(shared, HKDF_SALT, bound), sas


def confirm_mac(secret: bytes, request_id: str, fields: dict[str, Any]) -> str:
    message = CONFIRM_CONTEXT + request_id.encode("ascii") + b"|" + net._canonical(fields)
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- rate limit


class RateLimiter:
    """At most ``limit`` admitted events per source inside a sliding window."""

    def __init__(self, limit: int, window: float, max_sources: int = MAX_RATE_SOURCES) -> None:
        self._limit, self._window, self._max_sources = limit, window, max_sources
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, source: str, now: float) -> bool:
        with self._lock:
            hits = [moment for moment in self._hits.get(source, ()) if now - moment < self._window]
            if len(hits) >= self._limit:
                self._hits[source] = hits
                return False
            hits.append(now)
            self._hits[source] = hits
            if len(self._hits) > self._max_sources:
                live = {key: value for key, value in self._hits.items()
                        if value and now - value[-1] < self._window}
                self._hits = dict(list(live.items())[-self._max_sources:])
            return True


# ---------------------------------------------------------------- pending store


def requests_dir(home: Path | None = None) -> Path:
    return hub_mail.mail_root(home) / ".pair_requests"


def _request_path(request_id: Any, home: Path | None) -> Path:
    if not net._HEX32_RE.fullmatch(str(request_id or "")):
        raise hub_mail.HubMailError("pair_request_invalid")
    return requests_dir(home) / f"{request_id}.json"


def save_request(record: dict[str, Any], home: Path | None) -> None:
    net._write_json(_request_path(record["request_id"], home), record)


def delete_request(request_id: Any, home: Path | None) -> bool:
    try:
        path = _request_path(request_id, home)
    except hub_mail.HubMailError:
        return False
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def load_request(request_id: Any, home: Path | None, *, kind: str | None = None) -> dict[str, Any] | None:
    """One live request; an expired one is deleted and reads as unknown."""

    try:
        path = _request_path(request_id, home)
    except hub_mail.HubMailError:
        return None
    record = net._read_json(path)
    if not record or record.get("schema_id") != REQUEST_SCHEMA_ID or record.get("request_id") != request_id:
        return None
    if net._clock() >= float(record.get("expires_at") or 0):
        path.unlink(missing_ok=True)
        return None
    if kind is not None and record.get("kind") != kind:
        return None
    return record


def list_requests(home: Path | None = None, *, kind: str | None = None) -> list[dict[str, Any]]:
    directory = requests_dir(home)
    if not directory.is_dir():
        return []
    paths = sorted(p for p in directory.glob("*.json") if not p.name.startswith("."))
    records = (load_request(path.stem, home, kind=kind) for path in paths[:MAX_REQUEST_SCAN])
    return [record for record in records if record is not None]


def sweep_requests(home: Path | None = None) -> dict[str, Any]:
    """Delete every expired request; the listener loop runs this."""

    before = len(list(requests_dir(home).glob("*.json"))) if requests_dir(home).is_dir() else 0
    list_requests(home)
    after = len(list(requests_dir(home).glob("*.json"))) if requests_dir(home).is_dir() else 0
    return {"ok": True, "removed": max(0, before - after)}


def _public_request(record: dict[str, Any]) -> dict[str, Any]:
    peer = record["peer"]
    shown = record["state"] in {"revealed", "awaiting_accept", "accepted"}
    return {
        "request_id": record["request_id"],
        "direction": "incoming" if record["kind"] == "in" else "outgoing",
        "state": record["state"],
        "machine_id": peer["machine_id"],
        "machine_label": peer["machine_label"],
        "address": record["address"],
        "sas": record.get("sas", "") if shown else "",
        "expires_at": net._iso(float(record["expires_at"])),
    }


def pair_requests(home: Path | None = None) -> dict[str, Any]:
    """Pending pairing requests with the SAS the owner compares; never a secret."""

    try:
        listing = [_public_request(record) for record in list_requests(home)]
    except (hub_mail.HubMailError, OSError) as exc:
        return {"ok": False, "error": f"pair_requests_unreadable:{type(exc).__name__}"}
    return {"ok": True, "schema_id": net.PAIRING_SCHEMA_ID, "untrusted_labels": True,
            "count": len(listing), "requests": listing}


def reject(request_id: str, *, home: Path | None = None) -> dict[str, Any]:
    """Drop a pending request (either direction); nothing is stored."""

    if not delete_request(request_id, home):
        return {"ok": False, "error": "pair_request_unknown"}
    return {"ok": True, "rejected": True, "request_id": request_id}


# ---------------------------------------------------------------- responder (B)


def _refuse(status: int, error: str) -> tuple[int, dict[str, Any]]:
    return status, {"ok": False, "error": error}


def _is_hex(value: Any, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def handle_offer(
    request: dict[str, Any], *, source: str, home: Path | None, limiter: RateLimiter, own_tls: str,
) -> tuple[int, dict[str, Any]]:
    """B: take A's commit and answer with B's value; the owner decides later."""

    now = net._clock()
    if not limiter.allow(source, now):
        return _refuse(429, "rate_limited")
    if set(request) != _OFFER_KEYS:
        return _refuse(400, "pair_request_invalid")
    port, tls = request["port"], request["tls_cert_sha256"]
    if (
        not _is_hex(request["machine_id"], net._HEX32_RE)
        or not isinstance(request["machine_label"], str)
        or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
        or not _is_hex(request["commit"], net._HEX64_RE)
        or not isinstance(tls, str) or (tls and not net._HEX64_RE.fullmatch(tls))
    ):
        return _refuse(400, "pair_request_invalid")
    if request["proto"] != PROTO:
        return _refuse(400, "pair_proto_unsupported")
    own_id = net.machine_id(home)
    if request["machine_id"] == own_id:
        return _refuse(400, "pair_self_refused")
    private, public = dh_keypair()
    request_id, nonce = secrets.token_hex(16), secrets.token_hex(16)
    label = net.machine_label(home)
    record = {
        "schema_id": REQUEST_SCHEMA_ID, "kind": "in", "request_id": request_id, "state": "offered",
        "created_at": now, "expires_at": now + REQUEST_TTL_SECONDS,
        "peer": {"machine_id": request["machine_id"], "machine_label": net.clean_label(request["machine_label"]),
                 "tls_cert_sha256": tls},
        "address": net.format_address(source, port),
        "commit": request["commit"], "x": format(private, "x"), "pk": encode_public(public),
        "nonce": nonce, "own_tls": own_tls,
    }
    with _REQUEST_LOCK:
        if len(list_requests(home, kind="in")) >= MAX_PENDING_REQUESTS:
            return _refuse(429, "too_many_pending_requests")
        save_request(record, home)
    return 200, {"ok": True, "proto": PROTO, "request_id": request_id, "machine_id": own_id,
                 "machine_label": label, "pk": record["pk"], "nonce": nonce, "tls_cert_sha256": own_tls}


def handle_reveal(request: dict[str, Any], *, home: Path | None) -> tuple[int, dict[str, Any]]:
    """B: check A's value and commit, derive the secret and SAS, wait for the owner."""

    if (
        set(request) != _REVEAL_KEYS
        or not _is_hex(request["request_id"], net._HEX32_RE)
        or not _is_hex(request["nonce"], net._HEX32_RE)
    ):
        return _refuse(400, "pair_request_invalid")
    request_id = request["request_id"]
    record = load_request(request_id, home, kind="in")
    if record is None:
        return _refuse(404, "pair_request_unknown")
    if record["state"] != "offered":
        return _refuse(409, "pair_request_state_invalid")
    try:
        peer_public = decode_public(request["pk"])
        check_public(peer_public)
    except hub_mail.HubMailError as exc:
        delete_request(request_id, home)
        return _refuse(400, str(exc))
    if not hmac.compare_digest(commit_of(request["pk"], request["nonce"]), record["commit"]):
        delete_request(request_id, home)
        return _refuse(403, "pair_commit_mismatch")
    peer, own_id = record["peer"], net.machine_id(home)
    bound = transcript(
        party(peer["machine_id"], peer["machine_label"], peer["tls_cert_sha256"]),
        party(own_id, net.machine_label(home), record["own_tls"]),
        pk_a=request["pk"], pk_b=record["pk"], nonce_a=request["nonce"], nonce_b=record["nonce"],
    )
    secret, sas = derive(int(record["x"], 16), peer_public, bound)
    record.update(state="revealed", secret=secret.hex(), sas=sas, x="")
    save_request(record, home)
    return 200, {"ok": True, "state": "pending_accept"}


def _forget_peer(peer_id: str, secret: str, home: Path | None) -> None:
    """Remove a just-stored peer unless it was re-paired with another secret meanwhile."""

    with net._PEER_LOCK:
        current = net.load_peer(peer_id, home)
        if current is not None and current["secret"] == secret:
            net.peer_path(peer_id, home).unlink(missing_ok=True)


def accept(request_id: str, *, home: Path | None = None) -> dict[str, Any]:
    """B's owner approves a request whose SAS matched A's: store A and confirm to it."""

    try:
        return _accept(request_id, home)
    except hub_mail.HubMailError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"peer_store_write_failed:{type(exc).__name__}"}


def _accept(request_id: str, home: Path | None) -> dict[str, Any]:
    record = load_request(request_id, home, kind="in")
    if record is None:
        raise hub_mail.HubMailError("pair_request_unknown")
    if record["state"] not in {"revealed", "accepted"}:
        raise hub_mail.HubMailError("pair_not_revealed")
    retry = record["state"] == "accepted"
    peer, secret_hex = record["peer"], record["secret"]
    now = net._iso(net._clock())
    net._store_peer(
        {
            "schema_id": net.PEER_SCHEMA_ID, "machine_id": peer["machine_id"],
            "machine_label": peer["machine_label"], "address": record["address"], "hubs": [],
            "secret": secret_hex, "paired_at": now, "last_seen": now,
            "tls_cert_sha256": peer["tls_cert_sha256"],
        },
        home,
    )
    record["state"] = "accepted"
    save_request(record, home)
    secret = bytes.fromhex(secret_hex)
    fields = {"request_id": request_id, "machine_id": net.machine_id(home),
              "machine_label": net.machine_label(home), "hubs": net.local_hub_list(home)}
    body = net._canonical({**fields, "mac": confirm_mac(secret, request_id, fields)})
    pin = peer["tls_cert_sha256"]
    try:
        status, headers, data, _ = net._exchange(
            record["address"], "POST", "/v1/pair/confirm", body, {"Content-Type": "application/json"},
            tls=bool(pin), pin=pin or None,
        )
    except net.PeerUnreachable as exc:
        return {"ok": True, "confirm": "pending", "confirm_error": str(exc),
                "peer": _stored_view(peer["machine_id"], home)}
    except net.PeerRefused as exc:
        return _confirm_refused(record, str(exc), home)
    if status >= 500:
        return {"ok": True, "confirm": "pending", "confirm_error": f"peer_error:{status}",
                "peer": _stored_view(peer["machine_id"], home)}
    reply = net._json_object(data)
    if status != 200:
        # A retried accept whose first confirm reached A but lost its reply: A
        # already stored us and dropped the request, so a signed call settles it.
        if retry and status == 404 and _refresh_hubs(peer["machine_id"], home):
            delete_request(request_id, home)
            return {"ok": True, "confirm": "delivered", "peer": _stored_view(peer["machine_id"], home)}
        return _confirm_refused(record, f"peer_refused:{net._error_code(reply, status)}", home)
    expected = net._response_mac(secret, status, request_id, data)
    if not hmac.compare_digest(headers.get(net.HEADER_SIGNATURE.lower(), ""), expected):
        return _confirm_refused(record, "pair_confirm_response_invalid", home)
    delete_request(request_id, home)
    _refresh_hubs(peer["machine_id"], home)
    return {"ok": True, "confirm": "delivered", "peer": _stored_view(peer["machine_id"], home)}


def _confirm_refused(record: dict[str, Any], error: str, home: Path | None) -> dict[str, Any]:
    _forget_peer(record["peer"]["machine_id"], record["secret"], home)
    delete_request(record["request_id"], home)
    return {"ok": False, "error": error}


def _stored_view(peer_id: str, home: Path | None) -> dict[str, Any]:
    stored = net.load_peer(peer_id, home)
    return net.public_peer(stored) if stored is not None else {}


def _refresh_hubs(peer_id: str, home: Path | None) -> bool:
    """Learn the peer's hubs over a signed call; ``True`` proves it holds the stored secret."""

    peer = net.load_peer(peer_id, home)
    if peer is None:
        return False
    try:
        reply = net.call_peer(peer, home=home, method="GET", path="/v1/hubs")
    except hub_mail.HubMailError:
        return False
    if reply.get("machine_id") != peer_id:
        return False
    net._update_peer(peer_id, peer["secret"], home, hubs=net.clean_hubs(reply.get("hubs")))
    return True


# ---------------------------------------------------------------- initiator (A)


def handle_confirm(request: dict[str, Any], *, home: Path | None) -> tuple[int, dict[str, Any], bytes | None]:
    """A: verify B's confirm MAC with the secret derived locally, then store B."""

    if (
        set(request) != _CONFIRM_KEYS
        or not _is_hex(request["request_id"], net._HEX32_RE)
        or not _is_hex(request["machine_id"], net._HEX32_RE)
        or not isinstance(request["machine_label"], str)
        or not isinstance(request["hubs"], list)
        or not _is_hex(request["mac"], net._HEX64_RE)
    ):
        return (*_refuse(400, "pair_request_invalid"), None)
    request_id = request["request_id"]
    record = load_request(request_id, home, kind="out")
    if record is None:
        return (*_refuse(404, "pair_request_unknown"), None)
    if record["state"] != "awaiting_accept":
        return (*_refuse(409, "pair_request_state_invalid"), None)
    peer, secret = record["peer"], bytes.fromhex(record["secret"])
    fields = {key: request[key] for key in ("request_id", "machine_id", "machine_label", "hubs")}
    if request["machine_id"] != peer["machine_id"] or not hmac.compare_digest(
        request["mac"], confirm_mac(secret, request_id, fields)
    ):
        delete_request(request_id, home)
        return (*_refuse(403, "pair_confirm_invalid"), None)
    now = net._iso(net._clock())
    net._store_peer(
        {
            "schema_id": net.PEER_SCHEMA_ID, "machine_id": peer["machine_id"],
            "machine_label": peer["machine_label"], "address": record["address"],
            "hubs": net.clean_hubs(request["hubs"]), "secret": record["secret"],
            "paired_at": now, "last_seen": now, "tls_cert_sha256": peer["tls_cert_sha256"],
        },
        home,
    )
    delete_request(request_id, home)
    return 200, {"ok": True}, secret


def _local_listener(home: Path | None) -> dict[str, Any]:
    net.mark_enabled(home)
    status = net.listener_status(home)
    if not status["running"]:
        try:
            net.ensure_listener(home)
        except (hub_mail.HubMailError, OSError):
            pass
        status = net.listener_status(home)
    if not status["running"] or not status["port"]:
        raise hub_mail.HubMailError("local_listener_not_running")
    return status


def pair(address: str, *, home: Path | None = None, tls: bool = False) -> dict[str, Any]:
    """A: start code-free pairing with the node at ``address``; returns the SAS to compare."""

    try:
        return _pair(address, home, tls)
    except hub_mail.HubMailError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"pair_state_write_failed:{type(exc).__name__}"}


def _pair(address: str, home: Path | None, tls: bool) -> dict[str, Any]:
    net.parse_address(address)
    own_id = net.machine_id(home)
    status = _local_listener(home)
    own = party(own_id, net.machine_label(home), str(status.get("tls_cert_sha256") or ""))
    private, public = dh_keypair()
    public_hex, nonce = encode_public(public), secrets.token_hex(16)
    body = net._canonical({
        "proto": PROTO, "machine_id": own_id, "machine_label": own["machine_label"],
        "port": int(status["port"]), "commit": commit_of(public_hex, nonce),
        "tls_cert_sha256": own["tls_cert_sha256"],
    })
    with _REQUEST_LOCK:
        if len(list_requests(home, kind="out")) >= MAX_PENDING_REQUESTS:
            raise hub_mail.HubMailError("too_many_pending_requests")
    code, _, data, observed = net._exchange(
        address, "POST", "/v1/pair/offer", body, {"Content-Type": "application/json"}, tls=tls, pin=None,
    )
    reply = net._json_object(data)
    if code != 200:
        raise net.PeerRefused(f"peer_refused:{net._error_code(reply, code)}")
    peer_id, request_id = str(reply.get("machine_id") or ""), str(reply.get("request_id") or "")
    peer_tls = reply.get("tls_cert_sha256")
    if (
        not net._HEX32_RE.fullmatch(peer_id) or peer_id == own_id
        or not net._HEX32_RE.fullmatch(request_id)
        or not _is_hex(reply.get("nonce"), net._HEX32_RE)
        or not isinstance(reply.get("machine_label"), str)
        or not isinstance(peer_tls, str) or (peer_tls and not net._HEX64_RE.fullmatch(peer_tls))
    ):
        raise net.PeerRefused("pair_response_invalid")
    peer_public = decode_public(reply.get("pk"))
    check_public(peer_public)
    if tls and not hmac.compare_digest(peer_tls, observed):
        raise net.PeerRefused("tls_cert_mismatch")
    peer = party(peer_id, reply["machine_label"], peer_tls)
    bound = transcript(own, peer, pk_a=public_hex, pk_b=reply["pk"], nonce_a=nonce, nonce_b=reply["nonce"])
    secret, sas = derive(private, peer_public, bound)
    now = net._clock()
    record = {
        "schema_id": REQUEST_SCHEMA_ID, "kind": "out", "request_id": request_id, "state": "awaiting_accept",
        "created_at": now, "expires_at": now + REQUEST_TTL_SECONDS,
        "peer": {"machine_id": peer_id, "machine_label": peer["machine_label"],
                 "tls_cert_sha256": observed if tls else ""},
        "address": address, "secret": secret.hex(), "sas": sas,
    }
    save_request(record, home)
    reveal = net._canonical({"request_id": request_id, "pk": public_hex, "nonce": nonce})
    try:
        code, _, data, _ = net._exchange(
            address, "POST", "/v1/pair/reveal", reveal, {"Content-Type": "application/json"},
            tls=tls, pin=observed if tls else None,
        )
        if code != 200:
            raise net.PeerRefused(f"peer_refused:{net._error_code(net._json_object(data), code)}")
    except hub_mail.HubMailError:
        delete_request(request_id, home)
        raise
    return {"ok": True, "schema_id": net.PAIRING_SCHEMA_ID, "state": "awaiting_peer_accept",
            "request_id": request_id, "sas": sas, "untrusted_labels": True,
            "peer": {"machine_id": peer_id, "machine_label": peer["machine_label"], "address": address},
            "expires_at": net._iso(record["expires_at"])}


# ---------------------------------------------------------------- discovery


def discovery_reply(
    data: bytes, *, source: str, home: Path | None, port: int, limiter: RateLimiter,
) -> bytes | None:
    """The unicast answer to one probe, or ``None``; only an enabled listener answers."""

    if len(data) > MAX_DATAGRAM_BYTES:
        return None
    probe = net._json_object(data)
    if set(probe) != _PROBE_KEYS or probe["p"] != DISCOVER_PROBE_ID or not _is_hex(probe["n"], net._HEX32_RE):
        return None
    if (net._read_json(net.listener_config_path(home)) or {}).get("enabled") is False:
        return None
    if not limiter.allow(source, net._clock()):
        return None
    return net._canonical({
        "p": DISCOVER_REPLY_ID, "n": probe["n"], "machine_id": net.machine_id(home),
        "label": net.machine_label(home), "port": port, "proto": PROTO,
    })


def source_host(source: str) -> str:
    """A datagram or connection source without scope id or IPv4-mapped prefix."""

    host = source.split("%", 1)[0]
    return host[7:] if host.lower().startswith("::ffff:") else host


def _parse_reply(data: bytes, nonce: str, own_id: str) -> dict[str, Any] | None:
    reply = net._json_object(data) if len(data) <= MAX_DATAGRAM_BYTES else {}
    port = reply.get("port")
    if (
        set(reply) != _REPLY_KEYS or reply["p"] != DISCOVER_REPLY_ID or reply["n"] != nonce
        or not _is_hex(reply["machine_id"], net._HEX32_RE) or reply["machine_id"] == own_id
        or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
        or isinstance(reply["proto"], bool) or not isinstance(reply["proto"], int)
    ):
        return None
    return {"machine_id": reply["machine_id"], "label": net.clean_label(reply["label"]),
            "port": port, "proto": reply["proto"]}


def _targets(targets: Any) -> list[tuple[str, int]]:
    chosen = targets if targets is not None else net.DISCOVERY_TARGETS
    if chosen is None:
        return [(BROADCAST_ADDRESS, default_port())]
    try:
        items = list(chosen)[:MAX_DISCOVER_TARGETS]
    except TypeError:
        raise hub_mail.HubMailError("discover_target_invalid") from None
    resolved: list[tuple[str, int]] = []
    for item in items:
        if isinstance(item, str):
            resolved.append(net.parse_address(item))
            continue
        host, port = item if isinstance(item, (tuple, list)) and len(item) == 2 else ("", 0)
        if (
            not isinstance(host, str) or not host
            or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
        ):
            raise hub_mail.HubMailError("discover_target_invalid")
        resolved.append((host, port))
    return resolved


def discover(
    timeout: float = DISCOVER_TIMEOUT_SECONDS, targets: Any = None, *, home: Path | None = None,
    until_machine_id: str | None = None,
) -> dict[str, Any]:
    """Probe the LAN (broadcast) or explicit ``targets`` and collect unicast replies.

    Every entry is a hint: the address is where the reply came from and nothing
    is trusted until pairing proves the machine.
    """

    try:
        net.mark_enabled(home)
        own_id = net.machine_id(home)
        probes = _targets(targets)
    except hub_mail.HubMailError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"discover_state_unreadable:{type(exc).__name__}"}
    nonce = secrets.token_hex(16)
    packet = net._canonical({"p": DISCOVER_PROBE_ID, "n": nonce})
    sockets: dict[int, socket.socket] = {}
    found: dict[str, dict[str, Any]] = {}
    try:
        for host, port in probes:
            family = socket.AF_INET6 if ":" in host else socket.AF_INET
            try:
                if family not in sockets:
                    sockets[family] = _probe_socket(family)
                sockets[family].sendto(packet, (host, port))
            except OSError:
                continue
        deadline = time.monotonic() + max(0.0, float(timeout))
        while sockets and len(found) < MAX_DISCOVERED:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select(list(sockets.values()), [], [], min(remaining, 0.1))
            except (OSError, ValueError):
                break
            for listening in ready:
                try:
                    data, origin = listening.recvfrom(MAX_DATAGRAM_BYTES + 1)
                except OSError:
                    continue
                entry = _parse_reply(data, nonce, own_id)
                if entry is None or entry["machine_id"] in found:
                    continue
                entry["address"] = net.format_address(source_host(str(origin[0])), entry["port"])
                stored = net.load_peer(entry["machine_id"], home)
                entry["paired"] = stored is not None
                entry["address_changed"] = stored is not None and stored["address"] != entry["address"]
                found[entry["machine_id"]] = entry
            if until_machine_id is not None and until_machine_id in found:
                break
    finally:
        for opened in sockets.values():
            opened.close()
    return {"ok": True, "schema_id": net.PAIRING_SCHEMA_ID, "untrusted_hints": True,
            "count": len(found), "found": sorted(found.values(), key=lambda item: item["machine_id"])}


def _probe_socket(family: int) -> socket.socket:
    probe = socket.socket(family, socket.SOCK_DGRAM)
    try:
        if family == socket.AF_INET:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        probe.bind(("::" if family == socket.AF_INET6 else "0.0.0.0", 0))
    except OSError:
        probe.close()
        raise
    return probe
