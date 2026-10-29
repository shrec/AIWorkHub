"""NF-2026-01817: bootstrap's manager_verified agrees with the write gates.

An open Manager Chat record is not this process's identity. With no seat token
and no Claude/Codex route, bootstrap used to report ``manager_verified`` True
while ``task_create`` and ``sdlc_case`` refused ``manager_identity_required``.

Run: python -m pytest -q tests/test_bootstrap_write_gate_verdict_nf01817.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import core, manager_route_probe  # noqa: E402

_CHAT = {
    "session_id": "mls-" + "cd" * 16,
    "backend_id": "opencode_cli",
    "model": "xai/grok-4.7",
    "status": "active",
}


@pytest.fixture
def no_identity(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(core, "_active_manager_chat_record", lambda: dict(_CHAT))
    monkeypatch.setattr(core, "_manager_chat_seat_identity", lambda: None)
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_CONTRACT_DELIVERIES", {})
    monkeypatch.setattr(
        manager_route_probe,
        "probe_claude_route",
        lambda: SimpleNamespace(report=lambda: {"reason": "hop_limit_exceeded", "observations": {}}),
    )
    return root


def test_bootstrap_does_not_verify_a_chat_whose_process_holds_no_identity(no_identity):
    reply = core.manager_bootstrap()

    assert reply["manager_verified"] is False
    assert reply["role"] == "worker_or_unverified_client"
    assert reply["provider"] != "manager_chat"
    assert reply["manager_route"] == {}
    assert reply["manager_chat_seat_observed"]["session_id"] == _CHAT["session_id"]
    assert "manager_chat_seat" not in reply
    assert reply["remedy"]
    assert "hop_limit_exceeded" in reply["remedy"]
    assert "Reload the VS Code window" in reply["remedy"]
    assert reply["capabilities"]["semantic_edit"]["available"] is False


def test_the_write_gates_agree_with_the_bootstrap_verdict(no_identity, monkeypatch):
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)

    verified = core.manager_bootstrap()["manager_verified"]
    created = core.create_task(
        "NF01817_PROBE", "probe", "manager_chat_canary", "task_mcp", "objective",
        ["accepted"], [],
    )
    sdlc = core._sdlc_case_write_authority()

    assert verified is False
    assert created["ok"] is False
    assert "manager_identity_required:task_create" in json.dumps(created)
    assert sdlc is not None
    assert "manager_identity_required:sdlc_case" in json.dumps(sdlc)


def test_the_gates_resolve_identity_through_the_one_shared_function(no_identity, monkeypatch):
    route = {"provider": "codex", "session_id": "episode_verified", "thread_id": "t"}
    monkeypatch.setattr(core, "_live_manager_identity", lambda: route)
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: {"ok": False, "gate": "stop"})

    assert core._sdlc_case_write_authority() == {"ok": False, "gate": "stop"}


def test_an_identity_less_process_cannot_claim_the_callback_lane_through_the_chat(no_identity):
    from aiworkhub import manager_lane

    assert core.stamped_manager_identity() == {}
    assert manager_lane._claim_refusal(manager_lane._verified_identity()) == manager_lane.CLAIM_UNVERIFIED
