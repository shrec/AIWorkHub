"""NF-2026-01482: a derived vscode_lm candidate must have a launchable bridge.

Policy and circuit-breaker approval alone used to be enough for
``derive_launch_identity`` to resolve the ``vscode_lm`` adapter; a bridge host
that was never started, or had gone stale, was invisible to derivation. These
tests pin the bridge-readiness gate in the adapter loop directly against
``process_launcher.vscode_lm_bridge.bridge_readiness``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import process_launcher  # noqa: E402


def _card(**overrides):
    card = {
        "task_id": "T_LAUNCH_VSCODE_LM_STALE",
        "runner": "claude_sonnet-5",
        "topic": "coding",
        "task_type": "code",
        "allowed_writes": ["src/a.py"],
    }
    card.update(overrides)
    return card


def _reject_claude_cli(monkeypatch):
    """Deny claude_cli by policy so a claude_* derivation reaches vscode_lm."""
    real_validate = process_launcher.repo_policy.validate_launch

    def fake_validate(repo, card, adapter):
        if adapter == "claude_cli":
            return {"ok": False, "reason": "policy_denied_for_test"}
        return real_validate(repo, card, adapter)

    monkeypatch.setattr(process_launcher.repo_policy, "validate_launch", fake_validate)


def test_stale_bridge_host_rejects_vscode_lm_when_claude_cli_is_denied(
    tmp_path, monkeypatch
):
    def fake_bridge_readiness(repo, **kwargs):
        return {
            "launchable": False,
            "blocker_reason": "vscode_lm_host_stale",
            "resolved_model": "",
        }

    monkeypatch.setattr(
        process_launcher.vscode_lm_bridge, "bridge_readiness", fake_bridge_readiness
    )
    _reject_claude_cli(monkeypatch)

    with pytest.raises(process_launcher.LaunchRejected) as excinfo:
        process_launcher.derive_launch_identity(tmp_path, _card())

    message = str(excinfo.value)
    assert message.startswith("launch_adapter_underivable:runner=claude_sonnet-5:")
    assert "claude_cli:policy_denied_for_test" in message
    assert "vscode_lm:vscode_lm_unavailable:vscode_lm_host_stale" in message


def test_live_bridge_host_keeps_vscode_lm(tmp_path, monkeypatch):
    def fake_bridge_readiness(repo, **kwargs):
        return {
            "launchable": True,
            "blocker_reason": "",
            "resolved_model": kwargs.get("model") or "",
        }

    monkeypatch.setattr(
        process_launcher.vscode_lm_bridge, "bridge_readiness", fake_bridge_readiness
    )
    _reject_claude_cli(monkeypatch)

    derived = process_launcher.derive_launch_identity(tmp_path, _card())

    assert derived["adapter_id"] == "vscode_lm"
    assert derived["adapter_rejections"] == ["claude_cli:policy_denied_for_test"]


def test_explicit_vscode_lm_adapter_id_ignores_a_stale_host(tmp_path, monkeypatch):
    calls = []

    def fake_bridge_readiness(repo, **kwargs):
        calls.append(kwargs)
        return {
            "launchable": False,
            "blocker_reason": "vscode_lm_host_stale",
            "resolved_model": "",
        }

    monkeypatch.setattr(
        process_launcher.vscode_lm_bridge, "bridge_readiness", fake_bridge_readiness
    )

    derived = process_launcher.derive_launch_identity(
        tmp_path, _card(), adapter_id="vscode_lm"
    )

    assert derived["adapter_id"] == "vscode_lm"
    assert derived["adapter_rejections"] == []
    assert calls == []


def test_bridge_readiness_error_rejects_vscode_lm_when_claude_cli_is_denied(
    tmp_path, monkeypatch
):
    def raising_bridge_readiness(repo, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        process_launcher.vscode_lm_bridge, "bridge_readiness", raising_bridge_readiness
    )
    _reject_claude_cli(monkeypatch)

    with pytest.raises(process_launcher.LaunchRejected) as excinfo:
        process_launcher.derive_launch_identity(tmp_path, _card())

    message = str(excinfo.value)
    assert message.startswith("launch_adapter_underivable:runner=claude_sonnet-5:")
    assert "claude_cli:policy_denied_for_test" in message
    assert "vscode_lm:vscode_lm_unavailable:bridge_readiness_error" in message


def test_claude_family_derivation_never_probes_the_bridge_when_claude_cli_is_first(
    tmp_path, monkeypatch
):
    calls = []

    def fake_bridge_readiness(repo, **kwargs):
        calls.append(kwargs)
        return {
            "launchable": True,
            "blocker_reason": "",
            "resolved_model": kwargs.get("model") or "",
        }

    monkeypatch.setattr(
        process_launcher.vscode_lm_bridge, "bridge_readiness", fake_bridge_readiness
    )

    derived = process_launcher.derive_launch_identity(tmp_path, _card())

    assert derived["adapter_id"] == "claude_cli"
    assert derived["adapter_rejections"] == []
    assert calls == []


def test_glm_runner_with_a_stale_bridge_host_derives_glm_copilot_cli(tmp_path, monkeypatch):
    calls = []

    def fake_bridge_readiness(repo, **kwargs):
        calls.append(kwargs)
        return {
            "launchable": False,
            "blocker_reason": "vscode_lm_host_stale",
            "resolved_model": "",
        }

    monkeypatch.setattr(
        process_launcher.vscode_lm_bridge, "bridge_readiness", fake_bridge_readiness
    )

    derived = process_launcher.derive_launch_identity(tmp_path, _card(runner="glm_5"))

    assert derived["adapter_id"] == "glm_copilot_cli"
    assert "vscode_lm:vscode_lm_unavailable:vscode_lm_host_stale" in derived["adapter_rejections"]
    assert "glm_vscode_lm:vscode_lm_unavailable:vscode_lm_host_stale" in derived["adapter_rejections"]
    assert [call.get("adapter_id") for call in calls] == ["vscode_lm", "glm_vscode_lm"]


def test_deepseek_runner_with_a_stale_bridge_host_derives_deepseek_copilot_cli(tmp_path, monkeypatch):
    calls = []

    def fake_bridge_readiness(repo, **kwargs):
        calls.append(kwargs)
        return {
            "launchable": False,
            "blocker_reason": "vscode_lm_host_stale",
            "resolved_model": "",
        }

    monkeypatch.setattr(
        process_launcher.vscode_lm_bridge, "bridge_readiness", fake_bridge_readiness
    )

    derived = process_launcher.derive_launch_identity(tmp_path, _card(runner="deepseek_v4"))

    assert derived["adapter_id"] == "deepseek_copilot_cli"
    assert "vscode_lm:vscode_lm_unavailable:vscode_lm_host_stale" in derived["adapter_rejections"]
    assert "deepseek_vscode_lm:vscode_lm_unavailable:vscode_lm_host_stale" in derived["adapter_rejections"]
    assert [call.get("adapter_id") for call in calls] == ["vscode_lm", "deepseek_vscode_lm"]
