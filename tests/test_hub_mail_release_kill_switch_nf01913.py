"""NF-2026-01913: WorkHub Mail's LAN listener is switched off in release 0.12.84.

One module constant in ``hub_mail_net`` gates every spawn path (``start_listener``,
``ensure_listener`` and ``listener_action('start')``); status and stop keep working.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from aiworkhub import hub_mail_net, runtime_temp

pytestmark = pytest.mark.real_hub_mail_listener


@pytest.fixture(autouse=True)
def kill_switch_set(monkeypatch):
    monkeypatch.setattr(hub_mail_net, "LISTENER_DISABLED_IN_RELEASE", True)

    def forbidden_popen(*args, **kwargs):
        raise AssertionError(f"listener spawned while disabled: {args!r}")

    monkeypatch.setattr(hub_mail_net.subprocess, "Popen", forbidden_popen)


def _write_live_pidfile(home) -> None:
    record = {
        "schema_id": hub_mail_net.LISTENER_SCHEMA_ID, "pid": os.getpid(),
        "start_ticks": runtime_temp.process_start_ticks(os.getpid()), "token": "0" * 32,
        "bind_host": "127.0.0.1", "port": 9, "address": "127.0.0.1:9",
    }
    path = hub_mail_net.pidfile_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def test_the_switch_ships_set():
    tree = ast.parse(Path(hub_mail_net.__file__).read_text(encoding="utf-8"))
    values = [
        node.value.value for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        and any(isinstance(t, ast.Name) and t.id == "LISTENER_DISABLED_IN_RELEASE" for t in node.targets)
    ]
    assert values == [True]


def test_start_listener_is_refused_before_any_side_effect(tmp_path):
    started = hub_mail_net.start_listener(bind_host="127.0.0.1", port=0, home=tmp_path)

    assert started == {"ok": False, "error": "hub_mail_disabled_in_release"}
    assert not hub_mail_net.listener_config_path(tmp_path).exists()
    assert not hub_mail_net.pidfile_path(tmp_path).exists()
    assert hub_mail_net.listener_status(tmp_path)["running"] is False


def test_start_listener_refuses_even_an_invalid_bind(tmp_path):
    started = hub_mail_net.start_listener(bind_host="", port=0, home=tmp_path)

    assert started == {"ok": False, "error": "hub_mail_disabled_in_release"}


def test_listener_action_start_is_refused(tmp_path):
    started = hub_mail_net.listener_action("start", bind_host="127.0.0.1", port=0, home=tmp_path)

    assert started == {"ok": False, "error": "hub_mail_disabled_in_release"}
    assert not hub_mail_net.listener_config_path(tmp_path).exists()


def test_ensure_listener_spawns_nothing_from_an_enabled_config(tmp_path):
    hub_mail_net._write_config(tmp_path, enabled=True, bind_host="127.0.0.1", port=0)
    spawned_before = list(hub_mail_net._SPAWNED)

    assert hub_mail_net.ensure_listener(tmp_path) is None

    assert hub_mail_net._SPAWNED == spawned_before
    assert not hub_mail_net.pidfile_path(tmp_path).exists()


def test_ensure_listener_stops_a_live_listener(tmp_path, monkeypatch):
    hub_mail_net._write_config(tmp_path, enabled=True, bind_host="127.0.0.1", port=9)
    _write_live_pidfile(tmp_path)
    assert hub_mail_net.listener_status(tmp_path)["running"] is True
    stopped: list = []
    monkeypatch.setattr(hub_mail_net, "stop_listener", lambda home=None, **kw: stopped.append(home) or {})

    assert hub_mail_net.ensure_listener(tmp_path) is None

    assert stopped == [tmp_path]


def test_ensure_listener_swallows_a_failing_stop(tmp_path, monkeypatch):
    _write_live_pidfile(tmp_path)

    def failing_stop(home=None, **kw):
        raise OSError("stop failed")

    monkeypatch.setattr(hub_mail_net, "stop_listener", failing_stop)

    assert hub_mail_net.ensure_listener(tmp_path) is None


def test_ensure_listener_without_a_live_listener_does_not_stop(tmp_path, monkeypatch):
    hub_mail_net._write_config(tmp_path, enabled=True, bind_host="127.0.0.1", port=9)
    monkeypatch.setattr(
        hub_mail_net, "stop_listener",
        lambda *a, **kw: pytest.fail("stop_listener called with no live listener"),
    )

    assert hub_mail_net.ensure_listener(tmp_path) is None


def test_status_and_stop_still_work(tmp_path):
    status = hub_mail_net.listener_action("status", home=tmp_path)
    assert status["running"] is False

    stopped = hub_mail_net.listener_action("stop", home=tmp_path)
    assert stopped["stopped"] is False and stopped["running"] is False
    assert stopped["enabled"] is False
