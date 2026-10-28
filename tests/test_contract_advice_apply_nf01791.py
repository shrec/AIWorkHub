"""NF-2026-01791: opt-in ``apply_contract_advice`` merges high-score testmap tests.

Without the flag a preview is exactly what it was. With it, the tests Source
Graph ``testmap`` relates to the card at a score at or above
``APPLY_ADVICE_SCORE_FLOOR`` are appended to ``allowed_writes`` (and, for
``allowed_writes+validation``, named by one regression command), and the
returned ``plan_digest`` signs that merged plan so ``needfix_convert`` with the
digest alone commits it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import card_contract_advice, core, needfix_store

SRC = "src/aiworkhub/thing.py"
HIGH_A = "tests/test_thing_a.py"
HIGH_B = "tests/test_thing_b.py"
ALREADY_ALLOWED = "tests/test_thing_c.py"
LOW = "tests/test_thing_low.py"
EXISTING_COMMAND = "python -m pytest -q tests/test_thing_repro.py"


def _digest(plan: Any) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode("utf-8")).hexdigest()


class _RecordingStore:
    """Signs exactly the plan it is given and, like the real store, refuses unknown keys."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def preview_convert(
        self, repo_root: object, needfix_id: str, task_plan: Any = None
    ) -> dict[str, Any]:
        self.calls.append(json.loads(json.dumps(task_plan)))
        if isinstance(task_plan, dict) and card_contract_advice.APPLY_CONTRACT_ADVICE_KEY in (
            task_plan
        ):
            raise ValueError("task_plan_unknown_fields:apply_contract_advice")
        signed = dict(task_plan or {})
        return {
            "needfix_id": needfix_id,
            "already_converted": False,
            "claimable": True,
            "convertible": True,
            "current_status": "accepted",
            "task_plan": signed,
            "plan_digest": _digest(signed),
        }


def _suggested() -> list[dict[str, Any]]:
    return [
        {"test": HIGH_A, "score": 120, "production_path": SRC},
        {"test": HIGH_B, "score": card_contract_advice.APPLY_ADVICE_SCORE_FLOOR},
        {"test": ALREADY_ALLOWED, "score": 150, "production_path": SRC},
        {"test": LOW, "score": card_contract_advice.APPLY_ADVICE_SCORE_FLOOR - 1},
    ]


def _advice(root: object, plan: Any) -> dict[str, Any]:
    allowed = list(plan.get("allowed_writes") or [])
    suggested = _suggested()
    return {
        "advisory": True,
        "testmap": {
            "status": "ok",
            "score_floor": card_contract_advice.RELATED_TEST_SCORE_FLOOR,
            "suggested_validation_tests": suggested,
            "allowed_writes_missing_tests": [
                entry["test"] for entry in suggested if entry["test"] not in allowed
            ],
        },
        "ratchet": {"status": "not_applicable", "reason": "stub"},
    }


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_RecordingStore]:
    recording = _RecordingStore()
    core.previewed_plan_cache_invalidate()
    monkeypatch.setattr(core, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(core, "_needfix_store_module", lambda: recording)
    monkeypatch.setattr(card_contract_advice, "contract_advice", _advice)
    yield recording
    core.previewed_plan_cache_invalidate()


def _plan(**overrides: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "runner": "opencode_go_gemini-4-argon-contributor",
        "topic": "needfix-convert",
        "work_kind": "bugfix",
        "allowed_writes": [SRC, ALREADY_ALLOWED],
        "required_outputs": [SRC],
        "validation": [EXISTING_COMMAND],
        "validation_roles": ["reproduction"],
    }
    plan.update(overrides)
    return plan


def test_allowed_writes_mode_signs_the_merged_plan(store: _RecordingStore) -> None:
    preview = core.needfix_preview_convert(
        "NF-2026-01791", task_plan=_plan(apply_contract_advice="allowed_writes")
    )

    signed = preview["task_plan"]
    assert signed["allowed_writes"] == [SRC, ALREADY_ALLOWED, HIGH_A, HIGH_B]
    assert signed["validation"] == [EXISTING_COMMAND]
    assert signed["validation_roles"] == ["reproduction"]
    assert card_contract_advice.APPLY_CONTRACT_ADVICE_KEY not in signed
    assert preview["plan_digest"] == _digest(signed)
    assert preview["plan_digest_accepted_alone"] is True
    assert preview["contract_advice_applied"] == {
        "score_floor": 100,
        "added_allowed_writes": [HIGH_A, HIGH_B],
        "added_validation": [],
    }
    # The flag never reaches the store; the last preview is the one that signed.
    assert len(store.calls) == 2
    assert all(card_contract_advice.APPLY_CONTRACT_ADVICE_KEY not in c for c in store.calls)
    assert store.calls[-1] == signed
    # The existing advisory field stays.
    assert preview["contract_advice"]["advisory"] is True


def test_allowed_writes_plus_validation_appends_one_regression_command(
    store: _RecordingStore,
) -> None:
    preview = core.needfix_preview_convert(
        "NF-2026-01791",
        task_plan=_plan(apply_contract_advice="allowed_writes+validation"),
    )

    command = f"python -m pytest -q -p no:cacheprovider {HIGH_A} {HIGH_B} {ALREADY_ALLOWED}"
    signed = preview["task_plan"]
    assert signed["allowed_writes"] == [SRC, ALREADY_ALLOWED, HIGH_A, HIGH_B]
    assert signed["validation"] == [EXISTING_COMMAND, command]
    assert signed["validation_roles"] == ["reproduction", "regression"]
    assert preview["plan_digest"] == _digest(signed)
    assert preview["contract_advice_applied"] == {
        "score_floor": card_contract_advice.APPLY_ADVICE_SCORE_FLOOR,
        "added_allowed_writes": [HIGH_A, HIGH_B],
        "added_validation": [command],
    }
    assert preview["validation_roles_requirement"]["satisfied"] is True


def test_validation_mode_fills_generic_roles_when_none_were_declared(
    store: _RecordingStore,
) -> None:
    plan = _plan(apply_contract_advice="allowed_writes+validation", work_kind="generic")
    del plan["validation_roles"]

    preview = core.needfix_preview_convert("NF-2026-01791", task_plan=plan)

    assert preview["task_plan"]["validation_roles"] == ["generic", "regression"]
    assert len(preview["task_plan"]["validation"]) == 2


def test_nothing_above_the_floor_previews_once_and_reports_empty(
    store: _RecordingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def low_only(root: object, plan: Any) -> dict[str, Any]:
        advice = _advice(root, plan)
        advice["testmap"]["suggested_validation_tests"] = [
            {"test": LOW, "score": 4, "production_path": SRC}
        ]
        return advice

    monkeypatch.setattr(card_contract_advice, "contract_advice", low_only)

    preview = core.needfix_preview_convert(
        "NF-2026-01791",
        task_plan=_plan(apply_contract_advice="allowed_writes+validation"),
    )

    assert len(store.calls) == 1
    assert preview["task_plan"]["allowed_writes"] == [SRC, ALREADY_ALLOWED]
    assert preview["contract_advice_applied"] == {
        "score_floor": 100,
        "added_allowed_writes": [],
        "added_validation": [],
    }


@pytest.mark.parametrize("value", ["everything", "", True, None, ["allowed_writes"]])
def test_unknown_flag_value_is_refused_with_a_stable_reason(
    store: _RecordingStore, value: Any
) -> None:
    preview = core.needfix_preview_convert(
        "NF-2026-01791", task_plan=_plan(apply_contract_advice=value)
    )

    assert preview["convertible"] is False
    assert preview["refusal_reason"] == "apply_contract_advice_unknown_mode"
    assert preview["allowed_apply_contract_advice"] == list(
        card_contract_advice.APPLY_CONTRACT_ADVICE_MODES
    )
    assert "plan_digest" not in preview
    assert preview["plan_digest_accepted_alone"] is False
    assert store.calls == []


def test_without_the_flag_the_preview_is_unchanged(store: _RecordingStore) -> None:
    plan = _plan()

    preview = core.needfix_preview_convert("NF-2026-01791", task_plan=plan)

    bare = _RecordingStore().preview_convert(None, "NF-2026-01791", task_plan=plan)
    expected = {
        **bare,
        "plan_digest_accepted_alone": True,
        "validation_roles_requirement": core._validation_roles_requirement(bare["task_plan"]),
        "contract_advice": _advice(None, bare["task_plan"]),
    }
    assert json.dumps(preview, sort_keys=False) == json.dumps(expected, sort_keys=False)
    assert store.calls == [plan]


@pytest.mark.parametrize("plan", [None, _plan()], ids=["no_plan", "plan"])
def test_helper_without_the_flag_returns_the_store_preview_itself(plan: Any) -> None:
    sentinel: dict[str, Any] = {"plan_digest": "d" * 64}
    seen: list[Any] = []

    class _Store:
        def preview_convert(self, root: object, nid: str, task_plan: Any = None) -> Any:
            seen.append(task_plan)
            return sentinel

    result = card_contract_advice.preview_convert_applying_advice(
        "/repo", _Store(), "NF-2026-01791", plan
    )

    assert result is sentinel
    assert len(seen) == 1 and seen[0] is plan


# --- end to end with the real store: the digest alone commits the merged plan --------


@pytest.fixture
def real_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    needfix_store.initialize_repository(tmp_path)
    (tmp_path / SRC).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / SRC).write_text('"""The production file the card edits."""\n', encoding="utf-8")
    core.previewed_plan_cache_invalidate()
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(card_contract_advice, "contract_advice", _advice)
    yield tmp_path
    core.previewed_plan_cache_invalidate()


def test_convert_with_the_digest_alone_commits_the_merged_plan(
    real_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = needfix_store.add_needfix(
        real_repo,
        title="NF-2026-01791 apply contract advice",
        description="Merge high-score testmap tests into the previewed plan.",
        status="accepted",
        kind="bug",
    )
    plan = {
        "runner": "opencode_go_gemini-4-argon-contributor",
        "topic": "needfix-convert",
        "allowed_writes": [SRC],
        "required_outputs": [SRC],
        "apply_contract_advice": "allowed_writes",
    }

    preview = core.needfix_preview_convert(rec["id"], task_plan=plan)

    assert preview["convertible"] is True, preview
    assert preview["task_plan"]["allowed_writes"] == [SRC, HIGH_A, HIGH_B, ALREADY_ALLOWED]
    created: list[dict[str, Any]] = []

    def fake_create_task(**kwargs: Any) -> dict[str, Any]:
        created.append(kwargs)
        return {"ok": True, "task_id": kwargs["task_id"]}

    monkeypatch.setattr(core, "create_task", fake_create_task)
    committed = core.needfix_convert(rec["id"], plan_digest=preview["plan_digest"])

    assert committed["converted_task_id"] == f"needfix-{rec['id']}"
    assert len(created) == 1
    assert created[0]["allowed_writes"] == [SRC, HIGH_A, HIGH_B, ALREADY_ALLOWED]
    assert "apply_contract_advice" not in created[0]
