"""NF-2026-02074: NeedFix preview ranks the runner at the card's derived risk tier.

Measured on NF-2026-02072: an omitted runner was filled from workforce_rank
top-1 ranked at ``medium`` (the plan declared no tier), picked claude_haiku,
and the created card then derived ``high`` from its own paths, so the launch
was refused with ``workforce_route_risk_incapable``. The rank must use the
tier create derives, with a declared tier as a floor, never a ceiling.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import quality_evidence, server  # noqa: E402

NEEDFIX_ID = "NF-2026-02074"
# "policy" in a production path is an authority_boundary signal (floor: high);
# server.py plus a test file derives only medium.
HIGH_WRITES = ["src/aiworkhub/example_policy_nf02074.py", "tests/test_example_nf02074.py"]
VALIDATION = ["python -m pytest -n 0 -q tests/test_example_nf02074.py"]


def _snapshot(scope_files: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": NEEDFIX_ID,
        "title": "nf02075 title",
        "description": "nf02075 description",
        "scope_files": list(scope_files or []),
        "reopen_generation": 0,
    }


def _patch_snapshot(monkeypatch: pytest.MonkeyPatch, scope_files: list[str] | None) -> None:
    monkeypatch.setattr(
        server.needfix_store,
        "get_needfix",
        lambda _root, _nid: _snapshot(scope_files),
    )


@pytest.fixture
def rank_risks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    risks: list[str] = []

    def _rank(**kwargs: Any) -> dict[str, Any]:
        risks.append(kwargs["risk"])
        return {"launch_contract": {"runner": "ranked-runner-nf02074"}}

    monkeypatch.setattr(server.manager_ai_tools, "workforce_rank", _rank)
    # Hermetic: a NeedFix with no recorded scope, so the plan alone decides.
    _patch_snapshot(monkeypatch, None)
    return risks


def _plan(**overrides: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "task_type": "code",
        "allowed_writes": list(HIGH_WRITES),
        "validation": list(VALIDATION),
        "read_only": False,
    }
    plan.update(overrides)
    return plan


def _create_derived_tier(plan: dict[str, Any], declared: str | None = None) -> str:
    """The derivation core.create_task runs, over the same plan fields."""

    signals = quality_evidence.derive_risk_signals(
        {
            "task_type": plan["task_type"],
            "validation": plan["validation"],
            "read_only": plan["read_only"],
        },
        plan["allowed_writes"],
    )
    return quality_evidence.resolve_risk_profile(
        declared or quality_evidence.RISK_LOW, signals=signals
    )["effective_tier"]


def test_undeclared_tier_ranks_at_the_derived_high_not_medium(rank_risks: list[str]) -> None:
    contract = server._needfix_rank_launch_contract(NEEDFIX_ID, _plan())

    assert rank_risks == ["high"]
    assert contract == {"runner": "ranked-runner-nf02074"}


def test_rank_risk_equals_the_tier_create_derives(rank_risks: list[str]) -> None:
    for plan in (
        _plan(),
        _plan(allowed_writes=["docs/nf02074.md"], validation=[], task_type="research"),
        _plan(allowed_writes=["src/aiworkhub/example_nf02074.py"]),
    ):
        server._needfix_rank_launch_contract(NEEDFIX_ID, plan)
        assert rank_risks[-1] == _create_derived_tier(plan), plan
    assert _create_derived_tier(_plan()) == "high"


@pytest.mark.parametrize("declared", ["low", "medium"])
def test_lower_declared_tier_never_lowers_the_rank_risk(
    rank_risks: list[str], declared: str
) -> None:
    plan = _plan(risk_tier=declared)

    server._needfix_rank_launch_contract(NEEDFIX_ID, plan)

    assert rank_risks == ["high"]
    assert rank_risks[0] == _create_derived_tier(plan, declared)


def test_declared_critical_is_kept(rank_risks: list[str]) -> None:
    server._needfix_rank_launch_contract(NEEDFIX_ID, _plan(risk_tier="critical"))

    assert rank_risks == ["critical"]


def test_derivation_failure_keeps_declared_tier_or_medium(
    rank_risks: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: Any, **_k: Any) -> list[str]:
        raise quality_evidence.MalformedConfigError("boom")

    monkeypatch.setattr(quality_evidence, "derive_risk_signals", _boom)

    server._needfix_rank_launch_contract(NEEDFIX_ID, _plan())
    server._needfix_rank_launch_contract(NEEDFIX_ID, _plan(risk_tier="high"))

    assert rank_risks == ["medium", "high"]


def _create_derived_tier_for_conversion(scope_files: list[str], plan: dict[str, Any]) -> str:
    """The tier create derives over the card conversion really builds."""

    card = server.needfix_store.normalize_task_plan(
        _snapshot(scope_files), {"runner": "r-nf02075", "topic": "t-nf02075", **plan}
    )
    return _create_derived_tier(
        {
            "task_type": card["task_type"],
            "validation": card.get("validation", []),
            "read_only": card["read_only"],
            "allowed_writes": card.get("allowed_writes", []),
        },
        card.get("risk_tier"),
    )


def test_omitted_allowed_writes_inherits_scope_files_for_the_rank(
    rank_risks: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = list(HIGH_WRITES)
    _patch_snapshot(monkeypatch, scope)
    # No allowed_writes, task_type or runner: conversion inherits scope_files
    # and task_type='code'; the raw plan alone would rank over a test file.
    plan = {"required_outputs": ["tests/test_example_nf02074.py"]}

    contract = server._needfix_rank_launch_contract(NEEDFIX_ID, plan)

    assert contract == {"runner": "ranked-runner-nf02074"}
    assert rank_risks == ["high"]
    assert rank_risks[0] == _create_derived_tier_for_conversion(scope, plan)
    assert plan == {"required_outputs": ["tests/test_example_nf02074.py"]}


def test_plan_allowed_writes_override_scope_files_for_the_rank(
    rank_risks: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = list(HIGH_WRITES)
    _patch_snapshot(monkeypatch, scope)
    plan = {
        "allowed_writes": ["docs/nf02075.md"],
        "required_outputs": ["docs/nf02075.md"],
        "validation": [],
        "task_type": "research",
    }

    server._needfix_rank_launch_contract(NEEDFIX_ID, plan)

    assert rank_risks[0] == _create_derived_tier_for_conversion(scope, plan)
    assert rank_risks[0] != "high"


def test_unreadable_snapshot_falls_back_to_the_raw_plan(
    rank_risks: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unreadable(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise server.needfix_store.NeedFixNotFoundError(NEEDFIX_ID)

    monkeypatch.setattr(server.needfix_store, "get_needfix", _unreadable)

    contract = server._needfix_rank_launch_contract(NEEDFIX_ID, _plan())

    assert contract == {"runner": "ranked-runner-nf02074"}
    assert rank_risks == [_create_derived_tier(_plan())] == ["high"]
