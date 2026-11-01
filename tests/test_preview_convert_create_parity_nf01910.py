"""NF-2026-01910: preview/convert refuse plans that task creation or launch later refuses."""

from __future__ import annotations

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
