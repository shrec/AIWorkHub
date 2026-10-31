"""NF-2026-01890: the test suite must not leak real Hub Mail listeners.

``mark_enabled`` writes enabled=true on first use in a fresh tmp home and
``ensure_listener`` then spawned a detached ``hub_mail_net serve`` on
0.0.0.0:47731. The autouse fixture in ``conftest`` stubs ``ensure_listener``
unless a test opts in with ``@pytest.mark.real_hub_mail_listener``.
"""

from __future__ import annotations

import socket

import pytest

from aiworkhub import hub_mail_net


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_ensure_listener_spawns_nothing_by_default(tmp_path):
    spawned_before = list(hub_mail_net._SPAWNED)

    assert hub_mail_net.mark_enabled(tmp_path) is True
    assert hub_mail_net.ensure_listener(tmp_path) is None

    assert hub_mail_net._SPAWNED == spawned_before
    assert not hub_mail_net.pidfile_path(tmp_path).exists()
    assert hub_mail_net.listener_status(tmp_path)["running"] is False


def test_default_stub_is_not_the_production_function(request):
    assert request.node.get_closest_marker("real_hub_mail_listener") is None
    assert hub_mail_net.ensure_listener.__name__ != "ensure_listener"


@pytest.mark.real_hub_mail_listener
def test_opted_in_listener_binds_loopback_and_stop_leaves_no_live_process(tmp_path):
    assert hub_mail_net.ensure_listener.__name__ == "ensure_listener"
    port = _free_port()
    hub_mail_net._write_config(tmp_path, enabled=True, bind_host="127.0.0.1", port=port)
    spawned_before = len(hub_mail_net._SPAWNED)
    pid = 0
    try:
        started = hub_mail_net.ensure_listener(tmp_path)
        assert started is not None and started.get("ok") is True, started
        assert started["bind_host"] == "127.0.0.1"
        assert len(hub_mail_net._SPAWNED) == spawned_before + 1
        status = hub_mail_net.listener_status(tmp_path)
        assert status["running"] is True
        pid = status["pid"]
        assert pid > 0
    finally:
        stopped = hub_mail_net.stop_listener(tmp_path)

    assert stopped["stopped"] is True
    assert stopped["running"] is False
    assert not hub_mail_net.pidfile_path(tmp_path).exists()
    assert not hub_mail_net._pid_alive(pid)
