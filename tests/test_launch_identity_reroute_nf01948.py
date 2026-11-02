"""NF-2026-01948: launch honours the card's ``identity_reroute`` adapter/model.

``reroute_launch_identity`` writes ``card['identity_reroute']`` with
``to_runner``/``to_adapter_id``/``to_model``; ``derive_launch_identity`` used to
ignore it and re-derive the first pinnable adapter in tuple order. These tests
pin that the reroute supplies the identity, that explicit launch arguments still
win, that a reroute for another runner is ignored, and that a rerouted adapter
still faces the policy/circuit/vscode_lm checks.
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import process_launcher  # noqa: E402

RUNNER = "claude_sonnet-5"
REROUTE_MODEL = "claude-sonnet-5-reroute-pinned"


def _card(reroute=None, **overrides):
    card = {
        "task_id": "T_LAUNCH_IDENTITY_REROUTE",
        "runner": RUNNER,
        "topic": "coding",
        "task_type": "code",
        "allowed_writes": ["src/a.py"],
    }
    if reroute is not None:
        card["identity_reroute"] = reroute
    card.update(overrides)
    return card


def _reroute(to_adapter_id="claude_cli", to_model=REROUTE_MODEL, to_runner=RUNNER):
    return {
        "schema_id": "aiworkhub.identity_reroute.v1",
        "from_runner": "codex_gpt-5",
        "to_runner": to_runner,
        "to_adapter_id": to_adapter_id,
        "to_model": to_model,
        "reason": "test",
    }


def _bridge(monkeypatch, *, launchable=True, blocker=""):
    def fake_bridge_readiness(repo, **kwargs):
        return {
            "launchable": launchable,
            "blocker_reason": blocker,
            "resolved_model": kwargs.get("model") or "",
        }

    monkeypatch.setattr(
        process_launcher.vscode_lm_bridge, "bridge_readiness", fake_bridge_readiness
    )


def test_baseline_without_reroute_picks_vscode_lm(tmp_path, monkeypatch):
    _bridge(monkeypatch)
    derived = process_launcher.derive_launch_identity(tmp_path, _card())
    assert derived["adapter_id"] == "vscode_lm"
    assert "identity_reroute" not in derived["derived_from"].values()


def test_reroute_supplies_adapter_and_model(tmp_path, monkeypatch):
    _bridge(monkeypatch)
    derived = process_launcher.derive_launch_identity(tmp_path, _card(_reroute()))
    assert derived["adapter_id"] == "claude_cli"
    assert derived["model"] == REROUTE_MODEL
    assert derived["derived_from"]["adapter_id"] == "identity_reroute"
    assert derived["derived_from"]["model"] == "identity_reroute"
    assert "identity_reroute_rejected" not in derived["derived_from"]


def test_explicit_adapter_and_model_override_reroute(tmp_path, monkeypatch):
    _bridge(monkeypatch)
    derived = process_launcher.derive_launch_identity(
        tmp_path, _card(_reroute()), adapter_id="vscode_lm", model="explicit-model"
    )
    assert derived["adapter_id"] == "vscode_lm"
    assert derived["model"] == "explicit-model"
    assert "identity_reroute" not in derived["derived_from"].values()


def test_explicit_other_adapter_ignores_reroute_model(tmp_path, monkeypatch):
    _bridge(monkeypatch)
    derived = process_launcher.derive_launch_identity(
        tmp_path, _card(_reroute()), adapter_id="vscode_lm"
    )
    assert derived["adapter_id"] == "vscode_lm"
    assert derived["model"] != REROUTE_MODEL
    assert "identity_reroute" not in derived["derived_from"].values()


def test_explicit_model_overrides_reroute_model_only(tmp_path, monkeypatch):
    _bridge(monkeypatch)
    derived = process_launcher.derive_launch_identity(
        tmp_path, _card(_reroute()), model="explicit-model"
    )
    assert derived["adapter_id"] == "claude_cli"
    assert derived["model"] == "explicit-model"
    assert derived["derived_from"]["adapter_id"] == "identity_reroute"
    assert "model" not in derived["derived_from"]


def test_reroute_for_other_runner_is_ignored(tmp_path, monkeypatch):
    _bridge(monkeypatch)
    derived = process_launcher.derive_launch_identity(
        tmp_path, _card(_reroute(to_runner="codex_gpt-5"))
    )
    assert derived["adapter_id"] == "vscode_lm"
    assert derived["model"] != REROUTE_MODEL
    assert "identity_reroute" not in derived["derived_from"].values()
    assert "identity_reroute_rejected" not in derived["derived_from"]


def test_non_mapping_reroute_is_ignored(tmp_path, monkeypatch):
    _bridge(monkeypatch)
    derived = process_launcher.derive_launch_identity(tmp_path, _card("claude_cli"))
    assert derived["adapter_id"] == "vscode_lm"


def test_unpinnable_reroute_adapter_falls_back_and_records_reason(tmp_path, monkeypatch):
    _bridge(monkeypatch)
    derived = process_launcher.derive_launch_identity(
        tmp_path, _card(_reroute(to_adapter_id="not_a_real_adapter"))
    )
    assert derived["adapter_id"] == "vscode_lm"
    assert derived["model"] != REROUTE_MODEL
    assert derived["derived_from"]["adapter_id"] != "identity_reroute"
    rejected = derived["derived_from"]["identity_reroute_rejected"]
    assert rejected.startswith("not_a_real_adapter:identity_reroute_adapter_not_pinnable")
    assert rejected in derived["adapter_rejections"]


def test_policy_rejected_reroute_adapter_falls_back(tmp_path, monkeypatch):
    _bridge(monkeypatch)
    real_validate = process_launcher.repo_policy.validate_launch

    def fake_validate(repo, card, adapter):
        if adapter == "claude_cli":
            return {"ok": False, "reason": "policy_denied_for_test"}
        return real_validate(repo, card, adapter)

    monkeypatch.setattr(process_launcher.repo_policy, "validate_launch", fake_validate)
    derived = process_launcher.derive_launch_identity(tmp_path, _card(_reroute()))
    assert derived["adapter_id"] == "vscode_lm"
    assert derived["model"] != REROUTE_MODEL
    assert derived["derived_from"]["identity_reroute_rejected"] == (
        "claude_cli:policy_denied_for_test"
    )


def test_circuit_open_reroute_adapter_falls_back(tmp_path, monkeypatch):
    _bridge(monkeypatch)
    seen = []

    def fake_circuit(repo, adapter, model):
        seen.append((adapter, model))
        if adapter == "claude_cli":
            return {"state": "open", "failure_kind": "credit_exhausted"}
        return {}

    monkeypatch.setattr(process_launcher, "_derivation_route_circuit", fake_circuit)
    derived = process_launcher.derive_launch_identity(tmp_path, _card(_reroute()))
    assert ("claude_cli", REROUTE_MODEL) in seen
    assert derived["adapter_id"] == "vscode_lm"
    assert derived["derived_from"]["identity_reroute_rejected"].startswith(
        "claude_cli:route_circuit_open:credit_exhausted"
    )


def test_vscode_lm_reroute_with_stale_bridge_falls_back(tmp_path, monkeypatch):
    _bridge(monkeypatch, launchable=False, blocker="vscode_lm_host_stale")
    derived = process_launcher.derive_launch_identity(
        tmp_path, _card(_reroute(to_adapter_id="vscode_lm"))
    )
    assert derived["adapter_id"] == "claude_cli"
    assert derived["model"] != REROUTE_MODEL
    assert derived["derived_from"]["identity_reroute_rejected"] == (
        "vscode_lm:vscode_lm_unavailable:vscode_lm_host_stale"
    )
