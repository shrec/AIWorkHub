"""Regression and reproduction tests for NF-2026-01659.

Verifies that ``needfix_preview_convert`` and ``needfix_convert`` reuse
``worker_workspace._exact_required_card_inputs`` so any ``read_first`` or
``immutable_inputs`` entry that ``agent_launch_task`` would refuse with
``workspace_required_input_missing`` is refused at preview and before card
creation with the identical ``field``/``index``/``path`` reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aiworkhub import core, needfix_store, worker_workspace


@pytest.fixture
def repo_with_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    needfix_store.initialize_repository(tmp_path)
    for rel in (
        "src/aiworkhub/process_launcher.py",
        "src/aiworkhub/windows_appcontainer.py",
        "src/aiworkhub/target_output.py",
    ):
        full = tmp_path / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("# fixture\n", encoding="utf-8")
    core.previewed_plan_cache_invalidate()
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    return tmp_path


def _accepted_needfix(repo: Path) -> dict[str, Any]:
    return needfix_store.add_needfix(
        repo,
        title="NF-2026-01659 read_first preflight",
        description="Refuse missing read_first before creating a card.",
        status="accepted",
        kind="bug",
    )


def _base_plan(**overrides: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "runner": "opencode_go_gemini-4-argon-contributor",
        "topic": "needfix-convert",
        "allowed_writes": ["src/aiworkhub/target_output.py"],
        "required_outputs": ["src/aiworkhub/target_output.py"],
    }
    plan.update(overrides)
    return plan


def test_measured_repro_annotation_suffix_refused_in_preview_and_convert(
    repo_with_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _accepted_needfix(repo_with_files)
    plan = _base_plan(
        read_first=[
            "src/aiworkhub/process_launcher.py:540-620",
            "src/aiworkhub/windows_appcontainer.py:256-300 (ContainerGrant; note)",
        ]
    )
    expected_reason = (
        "workspace_required_input_missing:field=read_first:index=1:"
        "path=src/aiworkhub/windows_appcontainer.py:256-300 (ContainerGrant; note)"
    )

    preview = core.needfix_preview_convert(rec["id"], task_plan=plan)
    assert preview["claimable"] is True
    assert preview["convertible"] is False
    assert preview["refusal_reason"] == expected_reason

    created_cards: list[dict[str, Any]] = []
    monkeypatch.setattr(
        core,
        "create_task",
        lambda **kwargs: created_cards.append(kwargs) or {"ok": True, "task_id": kwargs["task_id"]},
    )

    with pytest.raises(needfix_store.NeedFixValidationError) as exc_with_plan:
        core.needfix_convert(
            rec["id"], task_plan=plan, plan_digest=preview["plan_digest"]
        )
    assert str(exc_with_plan.value) == expected_reason

    with pytest.raises(needfix_store.NeedFixValidationError) as exc_digest_only:
        core.needfix_convert(rec["id"], plan_digest=preview["plan_digest"])
    assert str(exc_digest_only.value) == expected_reason

    assert created_cards == []
    after = needfix_store.get_needfix(repo_with_files, rec["id"])
    assert after["status"] == "accepted"
    assert after["converted_task_id"] is None


@pytest.mark.parametrize(
    ("read_first", "expected_index", "expected_path"),
    [
        (
            ["src/aiworkhub/does_not_exist.py"],
            0,
            "src/aiworkhub/does_not_exist.py",
        ),
        (
            ["src/aiworkhub/does_not_exist.py:42"],
            0,
            "src/aiworkhub/does_not_exist.py",
        ),
        (
            [
                "src/aiworkhub/process_launcher.py:540-620",
                "src/aiworkhub/does_not_exist.py:10-20",
            ],
            1,
            "src/aiworkhub/does_not_exist.py",
        ),
    ],
)
def test_missing_read_first_with_optional_line_range_refuses_identically(
    repo_with_files: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_first: list[str],
    expected_index: int,
    expected_path: str,
) -> None:
    rec = _accepted_needfix(repo_with_files)
    plan = _base_plan(read_first=read_first)
    expected_prefix = (
        f"workspace_required_input_missing:field=read_first:index={expected_index}:"
        f"path={expected_path}"
    )

    preview = core.needfix_preview_convert(rec["id"], task_plan=plan)
    assert preview["convertible"] is False
    assert expected_prefix in preview["refusal_reason"]
    assert preview["refusal_reason"] == expected_prefix

    created_cards: list[dict[str, Any]] = []
    monkeypatch.setattr(
        core,
        "create_task",
        lambda **kwargs: created_cards.append(kwargs) or {"ok": True, "task_id": kwargs["task_id"]},
    )

    with pytest.raises(needfix_store.NeedFixValidationError) as excinfo:
        core.needfix_convert(
            rec["id"], task_plan=plan, plan_digest=preview["plan_digest"]
        )
    assert str(excinfo.value) == preview["refusal_reason"]
    assert created_cards == []
    after = needfix_store.get_needfix(repo_with_files, rec["id"])
    assert after["status"] == "accepted"
    assert after["converted_task_id"] is None


def test_missing_immutable_inputs_refuses_in_preview_and_convert(
    repo_with_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _accepted_needfix(repo_with_files)
    plan = _base_plan(
        immutable_inputs=[
            "src/aiworkhub/process_launcher.py",
            "src/aiworkhub/missing_artifact.json",
        ]
    )
    expected = (
        "workspace_required_input_missing:field=immutable_inputs:index=1:"
        "path=src/aiworkhub/missing_artifact.json"
    )

    preview = core.needfix_preview_convert(rec["id"], task_plan=plan)
    assert preview["convertible"] is False
    assert preview["refusal_reason"] == expected

    created_cards: list[dict[str, Any]] = []
    monkeypatch.setattr(
        core,
        "create_task",
        lambda **kwargs: created_cards.append(kwargs) or {"ok": True, "task_id": kwargs["task_id"]},
    )

    with pytest.raises(needfix_store.NeedFixValidationError, match=expected):
        core.needfix_convert(
            rec["id"], task_plan=plan, plan_digest=preview["plan_digest"]
        )
    assert created_cards == []


def test_valid_path_and_line_range_entries_stay_convertible_and_convert(
    repo_with_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _accepted_needfix(repo_with_files)
    plan = _base_plan(
        read_first=[
            "src/aiworkhub/process_launcher.py",
            "src/aiworkhub/process_launcher.py:540",
            "src/aiworkhub/windows_appcontainer.py:256-300",
        ]
    )

    preview = core.needfix_preview_convert(rec["id"], task_plan=plan)
    assert preview["claimable"] is True
    assert preview["convertible"] is True
    assert "refusal_reason" not in preview

    created_cards: list[dict[str, Any]] = []

    def _fake_create_task(**kwargs: Any) -> dict[str, Any]:
        created_cards.append(kwargs)
        return {"ok": True, "task_id": kwargs["task_id"]}

    monkeypatch.setattr(core, "create_task", _fake_create_task)
    committed = core.needfix_convert(rec["id"], plan_digest=preview["plan_digest"])
    assert committed["converted_task_id"] == f"needfix-{rec['id']}"
    assert len(created_cards) == 1
    assert created_cards[0]["read_first"] == plan["read_first"]


def test_preview_reuses_worker_workspace_exact_required_card_inputs(
    repo_with_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _accepted_needfix(repo_with_files)
    calls: list[tuple[Path, tuple[str, ...]]] = []
    original = worker_workspace._exact_required_card_inputs

    def _spy(
        repo: Path, card: Any, allowed: tuple[str, ...]
    ) -> tuple[str, ...]:
        calls.append((repo, allowed))
        return original(repo, card, allowed)

    monkeypatch.setattr(worker_workspace, "_exact_required_card_inputs", _spy)
    plan = _base_plan(read_first=["src/aiworkhub/process_launcher.py:1-10"])
    preview = needfix_store.preview_convert(repo_with_files, rec["id"], plan)
    assert preview["convertible"] is True
    assert len(calls) == 1
    assert calls[0][0] == repo_with_files.resolve()
    assert calls[0][1] == ("src/aiworkhub/target_output.py",)
