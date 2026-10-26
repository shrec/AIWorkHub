"""NF-2026-01718: every worker launch carries a reasoning-effort receipt."""

from __future__ import annotations

from typing import Any

import pytest

from aiworkhub import dashboard, runtime_adapters, vscode_lm_bridge

RECEIPT_KEYS = {
    "applied",
    "flag_emitted",
    "status",
    "profile",
    "applied_key",
    "provider_family",
    "difficulty",
    "context_capacity",
}


@pytest.mark.parametrize("adapter_id", ["opencode_cli", "grok_kilo_cli"])
def test_unsupported_receipt_for_routes_without_effort_control(adapter_id: str) -> None:
    card = {"work_kind": "security", "risk_tier": "critical"}
    assert runtime_adapters.resolve_adapter_reasoning(adapter_id, card) is None
    receipt = runtime_adapters.unsupported_reasoning_receipt(
        adapter_id, card, context_capacity=128_000
    )
    assert set(receipt) == RECEIPT_KEYS
    assert receipt["applied"] is False
    assert receipt["flag_emitted"] is False
    assert receipt["status"] == "unsupported"
    assert receipt["profile"] == "canonical_maximum"
    assert receipt["applied_key"] is None
    assert receipt["difficulty"] == "standard"
    assert receipt["context_capacity"] == 128_000


def test_unsupported_receipt_reviewer_uses_target_card() -> None:
    receipt = runtime_adapters.unsupported_reasoning_receipt(
        "opencode_cli",
        {"risk_tier": "low"},
        is_reviewer=True,
        target_card={"risk_tier": "critical"},
        lens="correctness",
    )
    assert receipt["profile"] == "canonical_maximum"
    assert receipt["context_capacity"] is None


def test_opencode_plan_reasoning_receipt_stays_none() -> None:
    plan = runtime_adapters.RuntimeAdapterPlan(
        adapter_id="opencode_cli",
        argv=["opencode"],
        cwd=None,
        executable="opencode",
        launchable=True,
        manual_only=False,
        validation_ok=True,
        validation_reason="ok",
    )
    assert plan.reasoning_receipt is None


def _decision(route_effort: Any) -> dict[str, Any]:
    return {
        "request": {"provider_family": "glm", "difficulty": "standard"},
        "profile": "canonical_high",
        "route_effort": route_effort,
    }


def test_bridge_receipt_applied() -> None:
    receipt = vscode_lm_bridge.bridge_reasoning_receipt(
        _decision(
            {
                "status": "applied",
                "applied": True,
                "applied_key": "high",
                "option_key": "reasoningEffort",
            }
        ),
        {"capacity_tokens": 200_000},
    )
    assert receipt == {
        "applied": True,
        "flag_emitted": True,
        "status": "applied",
        "profile": "canonical_high",
        "applied_key": "high",
        "provider_family": "glm",
        "difficulty": "standard",
        "context_capacity": 200_000,
    }


def test_bridge_receipt_unsupported_without_option_key() -> None:
    receipt = vscode_lm_bridge.bridge_reasoning_receipt(
        _decision(
            {
                "status": "unsupported",
                "applied": False,
                "applied_key": None,
                "option_key": None,
            }
        ),
        {"capacity_tokens": None},
    )
    assert receipt is not None
    assert receipt["applied"] is False
    assert receipt["flag_emitted"] is False
    assert receipt["status"] == "unsupported"
    assert receipt["applied_key"] is None
    assert receipt["context_capacity"] is None


def test_bridge_receipt_none_route_effort_is_unsupported() -> None:
    receipt = vscode_lm_bridge.bridge_reasoning_receipt(_decision(None), {})
    assert receipt is not None
    assert receipt["applied"] is False
    assert receipt["status"] == "unsupported"
    assert receipt["profile"] == "canonical_high"


@pytest.mark.parametrize(
    "decision",
    [None, "bad", [], {}, {"request": None, "profile": "x"}, _decision("not-a-dict")],
)
def test_bridge_receipt_malformed_is_none(decision: object) -> None:
    assert vscode_lm_bridge.bridge_reasoning_receipt(decision, None) is None


def test_dashboard_renders_unsupported_receipt_as_default() -> None:
    receipt = runtime_adapters.unsupported_reasoning_receipt(
        "opencode_cli", {"work_kind": "repository_coding"}
    )
    compact = dashboard._compact_reasoning_effort({"reasoning_effort": receipt})
    assert compact["effort"] == "default"
    assert compact["effort_status"] == "unsupported"
    assert compact["effort_profile"] == receipt["profile"]
