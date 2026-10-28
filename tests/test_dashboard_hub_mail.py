"""Dashboard WorkHub Mail popup bridge: read-only inbox, peers and listener.

Every hub, peer and pidfile lives under a tmp_path home, so nothing here ever
touches the real ``~/.aiworkhub``.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import dashboard_mcp_app, hub_mail, hub_mail_net, runtime_temp, shared_router

HUB_A = "repo_" + "a" * 32
HUB_B = "repo_" + "b" * 32
PEER_HUB = "repo_" + "12345678" + "c" * 24
OTHER_PEER_HUB = "repo_" + "87654321" + "d" * 24
PEER_ONE = "1a2b3c4d" + "0" * 24
PEER_TWO = "5e6f7a8b" + "0" * 24
PEER_SECRET = "5ec4e7" + "ab" * 29
PIDFILE_TOKEN = "70ce4" + "f" * 27
LIST_TOOL = "aiworkhub_dashboard_hub_mail"
DETAIL_TOOL = "aiworkhub_dashboard_hub_mail_detail"


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
                    "repo_name": name,
                    "extension_host_pid": 0,
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(
        shared_router, "_read_manifest_repo_id", lambda root: roots.get(str(Path(root).resolve()), "")
    )
    # The dashboard repository is hub B.
    monkeypatch.setattr(
        dashboard_mcp_app.dashboard, "_default_repo_root", lambda: (tmp_path / "aiworkhub").resolve()
    )
    return home


def _local(home: Path, hub_id: str) -> dict[str, str]:
    name = "geoai" if hub_id == HUB_A else "aiworkhub"
    local, refusal = hub_mail.local_hub((home.parent / name).resolve())
    assert refusal is None
    return local


def _send(home: Path, subject: str, *, minutes_ago: int = 0, body: str = "details") -> str:
    sent = hub_mail.send(
        local=_local(home, HUB_A), to_hub_id=HUB_B, kind="problem_report",
        subject=subject, body=body, home=home,
        now=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    assert sent["ok"] is True, sent
    return sent["mail"]["message_id"]


def _seed_peer(home: Path, machine_id: str, label: str, hubs: list[dict[str, str]]) -> None:
    path = hub_mail_net.peer_path(machine_id, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_id": hub_mail_net.PEER_SCHEMA_ID,
                "machine_id": machine_id,
                "machine_label": label,
                "secret": PEER_SECRET,
                "address": "192.0.2.7:8765",
                "hubs": hubs,
                "paired_at": "2026-10-01T00:00:00Z",
                "last_seen": "2026-10-27T12:00:00Z",
                "tls_cert_sha256": "",
            }
        ),
        encoding="utf-8",
    )


def _seed_pidfile(home: Path) -> None:
    pid = os.getpid()
    path = hub_mail_net.pidfile_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_id": hub_mail_net.LISTENER_SCHEMA_ID,
                "pid": pid,
                "start_ticks": runtime_temp.process_start_ticks(pid),
                "token": PIDFILE_TOKEN,
                "bind_host": "127.0.0.1",
                "port": 8765,
                "address": "",
                "tls": False,
                "tls_cert_sha256": "",
                "started_at": "2026-10-28T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )


def test_inbox_lists_newest_first_without_bodies_and_reports_counts(home):
    older = _send(home, "older report", minutes_ago=5, body="SECRET BODY ONE")
    newer = _send(home, "newer report", minutes_ago=1, body="SECRET BODY TWO")

    view = dashboard_mcp_app.hub_mail_view()

    assert view["ok"] is True and view["server_tool"] == LIST_TOOL and view["authority"] == "readonly"
    assert view["hub"] == {"hub_id": HUB_B, "repo_name": "aiworkhub"}
    assert [mail["message_id"] for mail in view["mails"]] == [newer, older]
    for mail in view["mails"]:
        assert mail["untrusted_data"] is True
        assert "body" not in mail and "attachments" not in mail
        assert mail["from_hub"] == {"hub_id": HUB_A, "repo_name": "geoai"}
        assert mail["kind"] == "problem_report" and mail["attachment_count"] == 0
        assert {"subject", "created_at", "status", "in_reply_to"} <= set(mail)
    text = json.dumps(view)
    assert "SECRET BODY" not in text
    for key in ("quarantined_count", "invalid_count", "scan_truncated", "unscanned_count"):
        assert key in view
    assert view["outbound"] == {"queued": 0, "delivery_failed": 0, "scan_truncated": False}
    assert set(view["node"]) == {"machine_id", "machine_label"}


def test_no_peers_and_no_listener_is_ok_and_writes_nothing(home):
    view = dashboard_mcp_app.hub_mail_view()

    assert view["ok"] is True
    assert view["peers"] == [] and view["mails"] == []
    assert view["listener"]["state"] == "stopped" and view["listener"]["running"] is False
    # A read never mints the machine key or the machine id.
    assert not hub_mail.key_path(home).exists()
    assert not hub_mail_net.machine_id_path(home).exists()


def test_dashboard_never_returns_peer_secret_key_or_pidfile_token(home):
    message_id = _send(home, "hello")
    _seed_peer(home, PEER_ONE, "lab", [{"hub_id": PEER_HUB, "repo_name": "geoai"}])
    _seed_pidfile(home)
    machine_key = hub_mail.load_or_create_key(home).hex()

    listing = json.dumps(dashboard_mcp_app.hub_mail_view())
    detail = json.dumps(dashboard_mcp_app.hub_mail_detail_view(message_id))

    for text in (listing, detail):
        assert PEER_SECRET not in text
        assert PIDFILE_TOKEN not in text
        assert machine_key not in text
    view = json.loads(listing)
    assert view["listener"]["state"] == "running"
    assert view["listener"]["bind"] == "127.0.0.1:8765" and view["listener"]["pid"] == os.getpid()
    assert "token" not in view["listener"]
    [peer] = view["peers"]
    assert "secret" not in peer and peer["tls_pinned"] is False
    assert peer["untrusted_data"] is True


def test_dashboard_counts_an_invalid_inbox_file_without_moving_it(home):
    inbox = hub_mail.box_dir(HUB_B, "inbox", home)
    inbox.mkdir(parents=True, exist_ok=True)
    bad = inbox / ("e" * 32 + ".json")
    bad.write_bytes(b"{not json")

    view = dashboard_mcp_app.hub_mail_view()

    assert view["ok"] is True and view["invalid_count"] == 1
    assert bad.is_file() and bad.read_bytes() == b"{not json"
    assert not hub_mail.box_dir(HUB_B, "quarantine", home).exists()
    # The detail path is read-only too.
    assert dashboard_mcp_app.hub_mail_detail_view("e" * 32)["ok"] is False
    assert bad.is_file()

    # The manager path keeps quarantining as before.
    manager = hub_mail.inbox(local_hub_id=HUB_B, home=home)
    assert manager["quarantined_count"] == 1 and manager["quarantined"][0]["reason"] == "json_invalid"
    assert not bad.exists()
    assert (hub_mail.box_dir(HUB_B, "quarantine", home) / bad.name).is_file()


def test_connected_machines_show_label_id_prefix_hubs_and_last_seen(home):
    _seed_peer(home, PEER_ONE, "lab", [{"hub_id": PEER_HUB, "repo_name": "geoai"}])
    _seed_peer(home, PEER_TWO, "lab", [{"hub_id": OTHER_PEER_HUB, "repo_name": "geoai"}])

    view = dashboard_mcp_app.hub_mail_view()

    assert [peer["machine_id"] for peer in view["peers"]] == [PEER_ONE, PEER_TWO]
    assert [peer["machine_id"][:8] for peer in view["peers"]] == ["1a2b3c4d", "5e6f7a8b"]
    for peer in view["peers"]:
        assert peer["machine_label"] == "lab"
        assert peer["last_seen"] == "2026-10-27T12:00:00Z"
        assert peer["address"] == "192.0.2.7:8765" and peer["paired_at"] == "2026-10-01T00:00:00Z"
    assert view["peers"][0]["hubs"] == [{"hub_id": PEER_HUB, "repo_name": "geoai"}]
    assert view["peers"][1]["hubs"] == [{"hub_id": OTHER_PEER_HUB, "repo_name": "geoai"}]


@pytest.mark.parametrize("bad_id", ["", "../../etc/passwd", "C" * 32, "c" * 31, "c" * 33, None])
def test_detail_refuses_a_malformed_message_id_before_any_read(home, monkeypatch, bad_id):
    def _forbidden(*_args, **_kwargs):
        raise AssertionError("filesystem read attempted for an invalid message_id")

    monkeypatch.setattr(dashboard_mcp_app, "_hub_mail_local", _forbidden)
    monkeypatch.setattr(hub_mail, "show", _forbidden)

    result = dashboard_mcp_app.hub_mail_detail_view(bad_id)

    assert result == {"ok": False, "error": "invalid_message_id", "server_tool": DETAIL_TOOL, "authority": "readonly"}


def test_detail_returns_the_body_of_an_inbox_or_archive_mail_as_untrusted(home):
    message_id = _send(home, "trace", body="Traceback ...")

    shown = dashboard_mcp_app.hub_mail_detail_view(message_id)
    assert shown["ok"] is True and shown["untrusted_data"] is True
    assert shown["mail"]["untrusted_data"] is True and shown["mail"]["body"] == "Traceback ..."
    assert shown["mail"]["box"] == "inbox"

    assert hub_mail.archive(local_hub_id=HUB_B, message_id=message_id, home=home)["ok"] is True
    archived = dashboard_mcp_app.hub_mail_detail_view(message_id)
    assert archived["ok"] is True and archived["mail"]["box"] == "archive"
    assert archived["mail"]["body"] == "Traceback ..."
    assert dashboard_mcp_app.hub_mail_view()["mails"] == []
    assert [m["box"] for m in dashboard_mcp_app.hub_mail_view(include_archived=True)["mails"]] == ["archive"]


def test_hub_mail_tools_are_registered_with_documented_args():
    registered: dict[str, object] = {}

    class _Mcp:
        def tool(self, name):
            def _decorate(fn):
                registered[name] = fn
                return fn
            return _decorate

    names = dashboard_mcp_app.register(_Mcp())

    assert dashboard_mcp_app.HUB_MAIL_READ_TOOLS == {
        LIST_TOOL: dashboard_mcp_app.hub_mail_view,
        DETAIL_TOOL: dashboard_mcp_app.hub_mail_detail_view,
    }
    for tool in (LIST_TOOL, DETAIL_TOOL):
        assert tool in names and tool in registered
    assert "limit:" in dashboard_mcp_app.hub_mail_view.__doc__
    assert "include_archived:" in dashboard_mcp_app.hub_mail_view.__doc__
    assert "message_id:" in dashboard_mcp_app.hub_mail_detail_view.__doc__
