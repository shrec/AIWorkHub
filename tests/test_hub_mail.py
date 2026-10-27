"""WorkHub Mail phase 1: two hubs on one machine exchange signed mail.

Both hubs are fake shared-router registry records under a tmp_path home, so
nothing here ever touches the real ``~/.aiworkhub``.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import stat
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, hub_mail, server, shared_router
from aiworkhub import runtime_adapters as ra

HUB_A = "repo_" + "a" * 32
HUB_B = "repo_" + "b" * 32
HUB_C = "repo_" + "c" * 32


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert Path.home() == home
    roots = {}
    for hub_id, name in ((HUB_A, "geoai"), (HUB_B, "aiworkhub")):
        root = tmp_path / name
        root.mkdir()
        roots[str(root.resolve())] = hub_id
        registry = shared_router.registry_dir()
        registry.mkdir(parents=True, exist_ok=True)
        (registry / f"{hub_id}.json").write_text(
            json.dumps(
                {
                    "schema_id": shared_router.SCHEMA_ID,
                    "repo_id": hub_id,
                    "repo_root": str(root),
                    "extension_host_pid": 0,
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(
        shared_router, "_read_manifest_repo_id", lambda root: roots.get(str(Path(root).resolve()), "")
    )
    return home


def _local(home: Path, hub_id: str) -> dict[str, str]:
    name = "geoai" if hub_id == HUB_A else "aiworkhub"
    local, refusal = hub_mail.local_hub((home.parent / name).resolve())
    assert refusal is None and local["hub_id"] == hub_id
    return local


def _send(home: Path, sender: str = HUB_A, to: str = HUB_B, **overrides) -> dict:
    fields = {"kind": "problem_report", "subject": "Task MCP refuses", "body": "details"}
    fields.update(overrides)
    return hub_mail.send(local=_local(home, sender), to_hub_id=to, home=home, **fields)


def _box(home: Path, hub_id: str, box: str) -> list[Path]:
    directory = hub_mail.box_dir(hub_id, box, home)
    return sorted(directory.iterdir()) if directory.is_dir() else []


def test_two_hubs_exchange_a_report_and_a_threaded_status_receipt(home):
    sent = _send(home, attachments=[{"name": "trace.txt", "text": "Traceback ..."}])
    assert sent["ok"] is True and sent["delivery"] == "delivered"
    report_id = sent["mail"]["message_id"]
    assert sent["mail"]["untrusted_data"] is True
    assert [p.name for p in _box(home, HUB_A, "outbox")] == [f"{report_id}.json"]

    listing = hub_mail.inbox(local_hub_id=HUB_B, home=home)
    assert listing["ok"] is True and listing["quarantined_count"] == 0
    [mail] = listing["mails"]
    assert mail["message_id"] == report_id and mail["kind"] == "problem_report"
    assert mail["from_hub"]["hub_id"] == HUB_A and mail["untrusted_data"] is True
    shown = hub_mail.show(local_hub_id=HUB_B, message_id=report_id, home=home)
    assert shown["mail"]["body"] == "details" and shown["mail"]["untrusted_data"] is True
    assert shown["mail"]["attachments"][0]["text"] == "Traceback ..."

    reply = _send(
        home, sender=HUB_B, to=HUB_A, kind="status_receipt", subject="Re: refuses",
        body="fixed", status="fixed_in", fixed_in="0.9.1", in_reply_to=report_id,
    )
    assert reply["ok"] is True, reply
    [received] = hub_mail.inbox(local_hub_id=HUB_A, home=home)["mails"]
    assert received["in_reply_to"] == report_id
    assert received["thread_id"] == report_id
    assert received["status"] == "fixed_in" and received["fixed_in"] == "0.9.1"
    assert [p.name for p in _box(home, HUB_B, "outbox")] == [f"{received['message_id']}.json"]

    archived = hub_mail.archive(local_hub_id=HUB_B, message_id=report_id, home=home)
    assert archived["ok"] is True and archived["mail"]["untrusted_data"] is True
    assert hub_mail.inbox(local_hub_id=HUB_B, home=home)["mails"] == []
    again = hub_mail.inbox(local_hub_id=HUB_B, home=home, include_archived=True)
    assert [m["box"] for m in again["mails"]] == ["archive"]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"kind": "contract_handoff"}, "kind_unknown"),
        ({"body": "x" * (hub_mail.MAX_BODY_BYTES + 1)}, "body_too_large"),
        ({"subject": "s" * (hub_mail.MAX_SUBJECT_CHARS + 1)}, "subject_too_long"),
        ({"attachments": [{"name": f"a{i}", "text": "t"} for i in range(9)]}, "too_many_attachments"),
        (
            {"attachments": [{"name": "big", "text": "y" * (hub_mail.MAX_ATTACHMENT_BYTES + 1)}]},
            "attachment_too_large",
        ),
        ({"attachments": [{"name": "a", "text": "t", "sha256": "0" * 64}]}, "attachment_sha256_mismatch"),
        ({"to": HUB_C}, "unknown_recipient"),
        ({"to": HUB_A}, "send_to_self_refused"),
        ({"kind": "status_receipt"}, "status_invalid"),
        ({"kind": "status_receipt", "status": "done"}, "status_invalid"),
        ({"kind": "status_receipt", "status": "fixed_in"}, "fixed_in_version_required"),
        ({"kind": "note", "status": "accepted"}, "status_not_allowed_for_kind"),
        ({"in_reply_to": "f" * 32}, "in_reply_to_unknown"),
    ],
)
def test_send_refuses_fail_closed_and_writes_nothing(home, overrides, reason):
    result = _send(home, **overrides)
    assert result == {"ok": False, "error": reason}
    assert _box(home, HUB_B, "inbox") == []
    assert _box(home, HUB_A, "outbox") == []


def _forge(home: Path, base: dict, *, resign: bool = True, **changes) -> str:
    """Drop a crafted envelope straight into hub B's inbox."""
    envelope = json.loads(json.dumps(base))
    message_id = uuid.uuid4().hex
    envelope.update(message_id=message_id, thread_id=message_id, **changes)
    if resign:
        envelope["signature"] = hub_mail.sign(envelope, hub_mail.load_or_create_key(home))
    inbox = hub_mail.box_dir(HUB_B, "inbox", home)
    (inbox / f"{message_id}.json").write_bytes(hub_mail.envelope_bytes(envelope))
    return message_id


def test_invalid_inbox_files_are_quarantined_with_a_reason_never_dropped(home):
    sent = _send(home)
    good_id = sent["mail"]["message_id"]
    inbox_dir = hub_mail.box_dir(HUB_B, "inbox", home)
    base = json.loads((inbox_dir / f"{good_id}.json").read_bytes())
    expected = {
        _forge(home, base, resign=False, body="tampered"): "bad_signature",
        _forge(home, base, created_at="2020-01-01T00:00:00Z", expires_at="2020-01-02T00:00:00Z"): "expired",
        _forge(home, base, to_hub={"hub_id": HUB_C}): "wrong_to_hub",
        _forge(home, base, kind="contract_handoff"): "kind_unknown",
        _forge(home, base, body="z" * (hub_mail.MAX_BODY_BYTES + 1)): "body_too_large",
        _forge(home, base, kind="status_receipt"): "status_invalid",
        _forge(home, base, kind="status_receipt", status="fixed_in"): "fixed_in_version_required",
        _forge(home, base, attachments=[{"name": "a", "text": "t", "sha256": "0" * 64}]): "attachment_sha256_mismatch",
        _forge(home, base, attachments=[{"name": f"a{i}", "text": "t", "sha256": hashlib.sha256(b"t").hexdigest()} for i in range(9)]): "too_many_attachments",
        _forge(home, base, attachments=[{"name": "a", "text": "y" * (hub_mail.MAX_ATTACHMENT_BYTES + 1), "sha256": hashlib.sha256(b"y" * (hub_mail.MAX_ATTACHMENT_BYTES + 1)).hexdigest()}]): "attachment_too_large",
    }
    (inbox_dir / "garbage.json").write_text("{not json", encoding="utf-8")

    listing = hub_mail.inbox(local_hub_id=HUB_B, home=home)
    assert [m["message_id"] for m in listing["mails"]] == [good_id]
    assert listing["quarantined_count"] == len(expected) + 1
    assert listing["quarantine_total"] == len(expected) + 1
    reasons = {item["file"]: item["reason"] for item in listing["quarantined"]}
    assert reasons.pop("garbage.json") == "json_invalid"
    assert reasons == {f"{mid}.json": reason for mid, reason in expected.items()}
    quarantine = hub_mail.box_dir(HUB_B, "quarantine", home)
    for name, reason in {f"{m}.json": r for m, r in expected.items()}.items():
        assert (quarantine / name).is_file()
        sidecar = json.loads((quarantine / f"{name}.reason.json").read_text(encoding="utf-8"))
        assert sidecar["reason"] == reason
    assert [p.name for p in _box(home, HUB_B, "inbox")] == [f"{good_id}.json"]


def test_redelivery_is_idempotent_and_a_conflict_is_quarantined(home):
    sent = _send(home)
    message_id = sent["mail"]["message_id"]
    original = hub_mail.box_dir(HUB_B, "inbox", home) / f"{message_id}.json"
    data = original.read_bytes()

    assert hub_mail.deliver(data, to_hub_id=HUB_B, message_id=message_id, home=home) == "already_delivered"
    assert _box(home, HUB_B, "inbox") == [original]

    other = json.loads(data)
    other["body"] = "a different envelope"
    other["signature"] = hub_mail.sign(other, hub_mail.load_or_create_key(home))
    outcome = hub_mail.deliver(
        hub_mail.envelope_bytes(other), to_hub_id=HUB_B, message_id=message_id, home=home
    )
    assert outcome == "duplicate_conflict"
    assert original.read_bytes() == data
    assert _box(home, HUB_B, "inbox") == [original]
    sidecars = [p for p in _box(home, HUB_B, "quarantine") if p.name.endswith(".reason.json")]
    [sidecar] = sidecars
    assert json.loads(sidecar.read_text(encoding="utf-8"))["reason"] == "duplicate_conflict"
    copy = sidecar.with_name(sidecar.name[: -len(".reason.json")])
    assert json.loads(copy.read_bytes())["body"] == "a different envelope"


def test_secrets_are_redacted_before_signing_on_both_sides_of_the_spool(home):
    secret = "sk-" + "Abc123xyz" * 3
    sent = _send(
        home,
        subject=f"token {secret}",
        body=f"the key is {secret} here",
        attachments=[{"name": "env.txt", "text": f"OPENAI={secret}\n"}],
    )
    assert sent["ok"] is True
    message_id = sent["mail"]["message_id"]
    key = hub_mail.load_or_create_key(home)
    for hub_id, box in ((HUB_A, "outbox"), (HUB_B, "inbox")):
        raw = (hub_mail.box_dir(hub_id, box, home) / f"{message_id}.json").read_bytes()
        assert secret.encode() not in raw
        assert raw.count(b"<redacted>") == 3
        envelope = json.loads(raw)
        assert envelope["signature"] == hub_mail.sign(envelope, key)
        [attachment] = envelope["attachments"]
        assert attachment["sha256"] == hashlib.sha256(attachment["text"].encode()).hexdigest()


def test_key_is_created_once_exclusive_and_writes_leave_no_tmp(home):
    first = hub_mail.load_or_create_key(home)
    path = hub_mail.key_path(home)
    assert len(first) == hub_mail.KEY_BYTES and path.read_bytes() == first
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # A second hub under the same home reuses the key rather than minting one.
    assert _send(home, sender=HUB_B, to=HUB_A)["ok"] is True
    assert hub_mail.load_or_create_key(home) == first
    assert path.read_bytes() == first
    assert _send(home)["ok"] is True
    assert [p for p in hub_mail.mail_root(home).rglob("*") if p.name.endswith(".tmp")] == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not enforced on Windows")
def test_key_file_mode_is_0600(home):
    hub_mail.load_or_create_key(home)
    assert stat.S_IMODE(hub_mail.key_path(home).stat().st_mode) == 0o600


_TOOLS = (
    "aiworkhub_manager_hub_mail_send",
    "aiworkhub_manager_hub_mail_inbox",
    "aiworkhub_manager_hub_mail_show",
    "aiworkhub_manager_hub_mail_archive",
)


def test_mcp_tools_are_registered_and_document_every_parameter():
    for name in _TOOLS:
        function = getattr(server, name)
        doc = inspect.getdoc(function) or ""
        assert "Args:" in doc, name
        args = doc.split("Args:", 1)[1]
        for parameter in inspect.signature(function).parameters:
            assert f"{parameter}:" in args, (name, parameter)


def test_manager_seat_holds_only_the_hub_mail_reads():
    assert "aiworkhub_manager_hub_mail_inbox" in ra.MANAGER_SEAT_MCP_TOOLS
    assert "aiworkhub_manager_hub_mail_show" in ra.MANAGER_SEAT_MCP_TOOLS
    assert "aiworkhub_manager_hub_mail_send" not in ra.MANAGER_SEAT_MCP_TOOLS
    assert "aiworkhub_manager_hub_mail_archive" not in ra.MANAGER_SEAT_MCP_TOOLS
    seat = ra.resolve_manager_seat_codex_tools()
    assert {"aiworkhub_manager_hub_mail_inbox", "aiworkhub_manager_hub_mail_show"} <= set(seat)
    assert not {"aiworkhub_manager_hub_mail_send", "aiworkhub_manager_hub_mail_archive"} & set(seat)


def _manager_route(root: Path) -> dict:
    return {
        "ok": True,
        "role": "manager",
        "repo": str(root),
        "manager_route": {"provider": "codex", "thread_id": "00000000-0000-4000-8000-000000000001"},
    }


def test_mcp_tools_are_gated_and_mark_every_mail_untrusted(home, monkeypatch):
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(home.parent / "geoai"))
    monkeypatch.setattr(core, "writes_allowed", lambda: False)
    closed = server.aiworkhub_manager_hub_mail_send(
        to_hub_id=HUB_B, kind="note", subject="s", body="b"
    )
    assert closed["error"] == "write_gate_closed"
    assert server.aiworkhub_manager_hub_mail_archive(message_id="a" * 32)["error"] == "write_gate_closed"
    assert _box(home, HUB_B, "inbox") == []

    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    sent = server.aiworkhub_manager_hub_mail_send(
        to_hub_id=HUB_B, kind="note", subject="hello", body="from geoai"
    )
    assert sent["ok"] is True and sent["mail"]["untrusted_data"] is True
    message_id = sent["mail"]["message_id"]

    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(home.parent / "aiworkhub"))
    listing = server.aiworkhub_manager_hub_mail_inbox()
    assert listing["untrusted_data"] is True
    assert [m["untrusted_data"] for m in listing["mails"]] == [True]
    shown = server.aiworkhub_manager_hub_mail_show(message_id=message_id)
    assert shown["mail"]["untrusted_data"] is True and shown["mail"]["body"] == "from geoai"
    archived = server.aiworkhub_manager_hub_mail_archive(message_id=message_id)
    assert archived["ok"] is True and archived["mail"]["untrusted_data"] is True

    monkeypatch.setattr(
        core, "manager_bootstrap",
        lambda: {"ok": True, "role": "worker_or_unverified_client", "manager_route": {}},
    )
    assert server.aiworkhub_manager_hub_mail_inbox()["error"] == "verified_manager_identity_required"


def test_expiry_is_checked_against_the_clock(home):
    later = datetime(2100, 1, 1, tzinfo=timezone.utc)
    message_id = _send(home)["mail"]["message_id"]
    listing = hub_mail.inbox(local_hub_id=HUB_B, home=home, now=later)
    assert listing["mails"] == []
    assert listing["quarantined"] == [{"file": f"{message_id}.json", "reason": "expired"}]


def test_crafted_inbox_files_are_quarantined_instead_of_crashing_reads(home, monkeypatch):
    good_id = _send(home)["mail"]["message_id"]
    inbox_dir = hub_mail.box_dir(HUB_B, "inbox", home)
    base = json.loads((inbox_dir / f"{good_id}.json").read_bytes())
    non_ascii_signature = _forge(home, base, resign=False, signature="é" * 64)

    def _surrogate_body() -> str:
        message_id = _forge(home, base, resign=False)
        path = inbox_dir / f"{message_id}.json"
        raw = path.read_bytes()
        assert b'"body":"details"' in raw
        path.write_bytes(raw.replace(b'"body":"details"', b'"body":"\\ud800"'))
        return message_id

    lone_surrogate = _surrogate_body()
    unprocessable = _forge(home, base)
    real_check = hub_mail.check_envelope

    def exploding_check(envelope, **kwargs):
        if envelope.get("message_id") == unprocessable:
            raise TypeError("crafted")
        return real_check(envelope, **kwargs)

    monkeypatch.setattr(hub_mail, "check_envelope", exploding_check)
    listing = hub_mail.inbox(local_hub_id=HUB_B, home=home)
    assert listing["ok"] is True
    assert [m["message_id"] for m in listing["mails"]] == [good_id]
    assert {item["file"]: item["reason"] for item in listing["quarantined"]} == {
        f"{non_ascii_signature}.json": "signature_invalid",
        f"{lone_surrogate}.json": "encoding_invalid",
        f"{unprocessable}.json": "envelope_unprocessable",
    }

    shown_id = _surrogate_body()
    shown = hub_mail.show(local_hub_id=HUB_B, message_id=shown_id, home=home)
    assert shown == {"ok": False, "error": "message_not_found", "message_id": shown_id}
    sidecar = hub_mail.box_dir(HUB_B, "quarantine", home) / f"{shown_id}.json.reason.json"
    assert json.loads(sidecar.read_text(encoding="utf-8"))["reason"] == "encoding_invalid"
    again = hub_mail.inbox(local_hub_id=HUB_B, home=home)
    assert [m["message_id"] for m in again["mails"]] == [good_id]
    assert again["quarantined_count"] == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"subject": "s\ud800"},
        {"body": "b\ud800"},
        {"attachments": [{"name": "a", "text": "t\ud800"}]},
        {"attachments": [{"name": "n\ud800", "text": "t"}]},
    ],
)
def test_send_refuses_a_lone_surrogate_and_writes_nothing(home, overrides):
    assert _send(home, **overrides) == {"ok": False, "error": "encoding_invalid"}
    assert _box(home, HUB_B, "inbox") == []
    assert _box(home, HUB_A, "outbox") == []


def test_a_failed_delivery_leaves_no_outbox_copy(home, monkeypatch):
    def broken_deliver(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(hub_mail, "deliver", broken_deliver)
    assert _send(home) == {"ok": False, "error": "spool_write_failed:OSError"}
    assert _box(home, HUB_A, "outbox") == []
    assert _box(home, HUB_B, "inbox") == []


def test_delivery_never_overwrites_a_file_that_appears_during_publish(home, monkeypatch):
    message_id = uuid.uuid4().hex
    target = hub_mail.box_dir(HUB_B, "inbox", home) / f"{message_id}.json"
    racer = b'{"racer":true}'
    real_link = os.link

    def racing_link(src, dst, *args, **kwargs):
        if Path(dst) == target:
            Path(dst).write_bytes(racer)
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(hub_mail.os, "link", racing_link)
    outcome = hub_mail.deliver(b'{"mine":true}', to_hub_id=HUB_B, message_id=message_id, home=home)
    assert outcome == "duplicate_conflict"
    assert target.read_bytes() == racer
    assert _box(home, HUB_B, "inbox") == [target]
    assert [p for p in hub_mail.mail_root(home).rglob("*") if p.name.endswith(".tmp")] == []


def test_concurrent_first_key_calls_agree_on_one_key(home):
    barrier = threading.Barrier(4)
    keys: list[bytes] = []
    errors: list[BaseException] = []

    def first_call() -> None:
        try:
            barrier.wait()
            keys.append(hub_mail.load_or_create_key(home))
        except BaseException as exc:  # surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=first_call) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(keys) == 4 and len(set(keys)) == 1
    assert len(keys[0]) == hub_mail.KEY_BYTES
    assert hub_mail.key_path(home).read_bytes() == keys[0]
    assert [p for p in hub_mail.mail_root(home).rglob("*") if p.name.endswith(".tmp")] == []


def test_inbox_reports_the_files_beyond_the_scan_cap(home, monkeypatch):
    for _ in range(3):
        assert _send(home)["ok"] is True
    full = hub_mail.inbox(local_hub_id=HUB_B, home=home)
    assert len(full["mails"]) == 3
    assert full["scan_truncated"] is False and full["unscanned_count"] == 0

    monkeypatch.setattr(hub_mail, "MAX_SCAN_FILES", 2)
    capped = hub_mail.inbox(local_hub_id=HUB_B, home=home)
    assert len(capped["mails"]) == 2
    assert capped["scan_truncated"] is True and capped["unscanned_count"] == 1
    assert capped["quarantined_count"] == 0


def test_fixed_in_and_attachment_names_are_redacted_before_spooling(home):
    secret = "sk-" + "Abc123xyz" * 3
    report_id = _send(home)["mail"]["message_id"]
    reply = _send(
        home, sender=HUB_B, to=HUB_A, kind="status_receipt", subject="Re", body="b",
        status="fixed_in", fixed_in=f"1.0-{secret}", in_reply_to=report_id,
        attachments=[{"name": f"env-{secret}.txt", "text": "t"}],
    )
    assert reply["ok"] is True, reply
    message_id = reply["mail"]["message_id"]
    for hub_id, box in ((HUB_B, "outbox"), (HUB_A, "inbox")):
        raw = (hub_mail.box_dir(hub_id, box, home) / f"{message_id}.json").read_bytes()
        assert secret.encode() not in raw
        envelope = json.loads(raw)
        assert "<redacted>" in envelope["fixed_in"]
        assert "<redacted>" in envelope["attachments"][0]["name"]


def test_a_deeply_nested_inbox_file_is_quarantined_instead_of_crashing_reads(home):
    good_id = _send(home)["mail"]["message_id"]
    inbox_dir = hub_mail.box_dir(HUB_B, "inbox", home)
    # Far below MAX_ENVELOPE_BYTES, far past json's recursion depth.
    nested = b"[" * 100_000 + b"]" * 100_000
    listed_id, shown_id = uuid.uuid4().hex, uuid.uuid4().hex
    (inbox_dir / f"{listed_id}.json").write_bytes(nested)
    listing = hub_mail.inbox(local_hub_id=HUB_B, home=home)
    assert listing["ok"] is True
    assert [m["message_id"] for m in listing["mails"]] == [good_id]
    assert {item["file"]: item["reason"] for item in listing["quarantined"]} == {
        f"{listed_id}.json": "envelope_unprocessable",
    }
    (inbox_dir / f"{shown_id}.json").write_bytes(nested)
    shown = hub_mail.show(local_hub_id=HUB_B, message_id=shown_id, home=home)
    assert shown == {"ok": False, "error": "message_not_found", "message_id": shown_id}
