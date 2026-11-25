"""NF-2026-02403: preview and convert refuse an invalid work_kind before auto-promotion.

Measured 2026-11-24: a plan with work_kind 'feature' previewed convertible, was
auto-promoted, and only core task creation then failed with the bare
'invalid_work_kind'. The refusal now happens at preview, before any NeedFix is
promoted, and it names every allowed work_kind.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import core, needfix_store, quality_evidence, server  # noqa: E402

INVALID_WORK_KIND = "feature"
GENERIC_WORK_KIND = "generic"
SCOPE = "src/aiworkhub/target_output.py"


def _plan(work_kind: str) -> dict[str, Any]:
    return {
        "runner": "claude_sonnet-5",
        "topic": "needfix_conversion",
        "allowed_writes": [SCOPE],
        "required_outputs": [SCOPE],
        "work_kind": work_kind,
    }


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    needfix_store.initialize_repository(tmp_path)
    full = tmp_path / SCOPE
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("# fixture\n", encoding="utf-8")
    core.previewed_plan_cache_invalidate()
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def accepted(repo: Path) -> dict[str, Any]:
    return needfix_store.add_needfix(
        repo,
        title="NF-2026-02403 work_kind preview gate",
        description="Refuse an invalid work_kind before a NeedFix is auto-promoted.",
        status="accepted",
        kind="bug",
    )


@pytest.fixture
def captured(repo: Path) -> dict[str, Any]:
    return needfix_store.add_needfix(
        repo,
        title="NF-2026-02403 one-call convert",
        description="A captured NeedFix must not be promoted by a refused one-call convert.",
        kind="bug",
    )


def test_preview_refuses_an_invalid_work_kind(accepted: dict[str, Any]) -> None:
    preview = core.needfix_preview_convert(accepted["id"], task_plan=_plan(INVALID_WORK_KIND))

    assert preview["convertible"] is False
    reason = preview["refusal_reason"]
    assert reason.startswith("invalid_work_kind")
    for work_kind in quality_evidence.WORK_KINDS:
        assert work_kind in reason


def test_one_call_convert_commits_nothing_and_keeps_captured(
    repo: Path, captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[dict[str, Any]] = []
    monkeypatch.setattr(
        core,
        "create_task",
        lambda **kwargs: created.append(kwargs) or {"ok": True, "task_id": kwargs["task_id"]},
    )

    reply = server.needfix_convert(captured["id"], task_plan=_plan(INVALID_WORK_KIND))

    assert reply["committed"] is False
    # The captured row's status refusal comes first; the work_kind gate is appended after it.
    assert "invalid_work_kind: allowed work_kind values" in reply["refusal_reason"]
    assert created == []
    assert needfix_store.get_needfix(repo, captured["id"])["status"] == "captured"


def test_generic_work_kind_stays_convertible(accepted: dict[str, Any]) -> None:
    preview = core.needfix_preview_convert(accepted["id"], task_plan=_plan(GENERIC_WORK_KIND))

    assert preview["convertible"] is True
    assert "refusal_reason" not in preview


def _bugfix_plan(roles: list[str] | None) -> dict[str, Any]:
    required = list(quality_evidence._REQUIRED_BEHAVIORAL_ROLES["bugfix"])
    plan = _plan("bugfix")
    plan["validation"] = [f"pytest -q tests/test_role_{index}.py" for index in range(len(required))]
    if roles is not None:
        plan["validation_roles"] = roles
    return plan


def test_bugfix_with_its_required_roles_stays_convertible(accepted: dict[str, Any]) -> None:
    roles = list(quality_evidence._REQUIRED_BEHAVIORAL_ROLES["bugfix"])

    preview = core.needfix_preview_convert(accepted["id"], task_plan=_bugfix_plan(roles))

    assert preview["convertible"] is True
    assert "refusal_reason" not in preview


def test_bugfix_without_its_required_roles_names_the_allowed_roles(accepted: dict[str, Any]) -> None:
    preview = core.needfix_preview_convert(accepted["id"], task_plan=_bugfix_plan(None))

    assert preview["convertible"] is False
    reason = preview["refusal_reason"]
    assert reason.startswith("behavioral_validation_roles_missing")
    for role in quality_evidence.VALIDATION_ROLES:
        assert role in reason
