"""NF-2026-01910: preview/convert refuse plans that task creation or launch later refuses."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import core, needfix_store

TARGET = "src/aiworkhub/near.py"
OTHER = "src/aiworkhub/other.py"


@pytest.fixture
def needfix_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    needfix_store.initialize_repository(tmp_path)
    target = tmp_path / TARGET
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def f():\n    pass\n", encoding="utf-8")
    core.previewed_plan_cache_invalidate()
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    yield tmp_path
    core.previewed_plan_cache_invalidate()


def _accepted_needfix(repo: Path) -> dict[str, Any]:
    return needfix_store.add_needfix(
        repo,
        title="NF-2026-01910 preview create parity",
        description="Preview must refuse what task_create or launch refuses.",
        status="accepted",
        kind="bug",
    )


def _plan(**overrides: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "runner": "opencode_go_gemini-4-argon-contributor",
        "topic": "needfix-convert",
        "allowed_writes": [TARGET],
        "required_outputs": [TARGET],
        "validation": ["python -m pytest a.py -q"],
    }
    plan.update(overrides)
    return plan


def test_reserved_quality_review_topic_is_not_convertible(needfix_repo: Path) -> None:
    rec = _accepted_needfix(needfix_repo)

    preview = needfix_store.preview_convert(
        needfix_repo, rec["id"], _plan(topic="quality_review")
    )

    assert preview["convertible"] is False, preview
    assert "reserved_topic:quality_review" in preview["refusal_reason"]


def test_forbidden_path_named_in_objective_is_not_convertible(
    needfix_repo: Path,
) -> None:
    rec = _accepted_needfix(needfix_repo)
    plan = _plan(objective=f"Fix the bug by editing {OTHER} as well.", forbidden=[OTHER])

    preview = needfix_store.preview_convert(needfix_repo, rec["id"], plan)

    assert preview["convertible"] is False, preview
    assert "contradictory_task_path_contract" in preview["refusal_reason"]


def test_forbidden_path_overlapping_allowed_writes_is_not_convertible(
    needfix_repo: Path,
) -> None:
    rec = _accepted_needfix(needfix_repo)

    preview = needfix_store.preview_convert(
        needfix_repo, rec["id"], _plan(forbidden=[TARGET])
    )

    assert preview["convertible"] is False, preview
    assert "contradictory_task_path_contract" in preview["refusal_reason"]


def test_unrelated_forbidden_path_stays_convertible(needfix_repo: Path) -> None:
    rec = _accepted_needfix(needfix_repo)

    preview = needfix_store.preview_convert(
        needfix_repo, rec["id"], _plan(forbidden=["CHANGELOG.md"])
    )

    assert preview["convertible"] is True, preview
    assert "refusal_reason" not in preview


def test_convert_refuses_reserved_topic_without_creating_a_card(
    needfix_repo: Path,
) -> None:
    rec = _accepted_needfix(needfix_repo)
    plan = _plan(topic="quality_review")
    calls: list[dict[str, Any]] = []

    def create_task(card: dict[str, Any]) -> dict[str, Any]:
        calls.append(card)
        return {"task_id": card["task_id"]}

    with pytest.raises(needfix_store.NeedFixValidationError) as excinfo:
        needfix_store.convert_needfix(
            needfix_repo,
            rec["id"],
            create_task,
            task_card_builder=lambda snap: needfix_store.normalize_task_plan(snap, plan),
        )

    assert "reserved_topic:quality_review" in str(excinfo.value)
    assert calls == []
    assert needfix_store.get_needfix(needfix_repo, rec["id"])["status"] == "accepted"


def test_policy_module_plan_converts_to_the_scope_create_stores(
    needfix_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NF-2026-01946: preview signs the caller's plan, convert commits it, and
    the created card carries the same derived scope a direct create stores."""
    from aiworkhub import task_store, task_templates
    from aiworkhub.agent_tool_instructions import DERIVED_ARTIFACTS
    from aiworkhub.policy_derived_writes import POLICY_SOURCE

    task_store.initialize_repository(needfix_repo)
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {"provider": "claude", "session_id": "SESSION-A", "route_state": "verified"},
    )
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **k: None)
    monkeypatch.setattr(core, "_verify_coordinator_capability", lambda _runner: (True, "ok"))
    policy = needfix_repo / POLICY_SOURCE
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("POLICY = None\n", encoding="utf-8")
    rec = _accepted_needfix(needfix_repo)
    plan = _plan(
        runner="claude-worker",
        allowed_writes=[POLICY_SOURCE],
        required_outputs=[POLICY_SOURCE],
        callback_required=False,
    )

    preview = core.needfix_preview_convert(rec["id"], task_plan=plan)
    assert preview["convertible"] is True, preview
    assert preview["task_plan"]["allowed_writes"] == [POLICY_SOURCE]

    committed = core.needfix_convert(rec["id"], plan_digest=preview["plan_digest"])
    task_id = committed["converted_task_id"]
    direct = core.create_task(
        task_id="NF01946-DIRECT",
        title="direct create of the same scope",
        runner="claude-worker",
        topic="needfix-convert",
        objective="Compare the converted card's scope with a direct create.",
        acceptance=["same derived scope"],
        allowed_writes=[POLICY_SOURCE],
        required_outputs=[POLICY_SOURCE],
        validation=plan["validation"],
        custom_template_escape=task_templates.AUDITED_CUSTOM_ESCAPE,
        callback_required=False,
    )
    assert direct.get("created") is True, direct

    conn = core._canonical_connect()
    try:
        cards = {
            row[0]: json.loads(row[1])
            for row in conn.execute(
                "SELECT task_id, card_json FROM tasks WHERE task_id IN (?, ?)",
                (task_id, "NF01946-DIRECT"),
            )
        }
    finally:
        conn.close()
    expected = [POLICY_SOURCE, *DERIVED_ARTIFACTS]
    for card in cards.values():
        assert card["allowed_writes"] == expected
        assert card["allowed_writes_derived"] == list(DERIVED_ARTIFACTS)
    assert set(cards) == {task_id, "NF01946-DIRECT"}
    assert needfix_store.get_needfix(needfix_repo, rec["id"])["status"] == "task_created"
