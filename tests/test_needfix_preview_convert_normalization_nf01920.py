"""NF-2026-01920: needfix_preview_convert normalizes fillable plan shape errors.

Measured: 20-23% of needfix_preview_convert calls failed on a missing
runner/topic, an ``acceptance`` string, or stray ``action``/``task_id`` keys,
and were retried by hand. The MCP tool now fills runner/topic from the
workforce_rank top-1 launch contract, coerces the string, drops the stray keys
with a warning, lists every change, and ``plan_digest`` binds the normalized
plan so needfix_convert commits it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiworkhub import card_contract_advice, core, needfix_store, server  # noqa: E402

NEEDFIX_ID = "NF-2026-01920"
SCOPE = "src/aiworkhub/example_nf01920.py"
ROW: dict[str, Any] = {
    "id": NEEDFIX_ID,
    "title": "Normalize preview_convert plan shapes",
    "description": "Fill runner/topic, coerce acceptance, drop stray keys.",
    "scope_files": [SCOPE],
    "reopen_generation": 0,
    "kind": "improvement",
    "status": "accepted",
}
RANKED_RUNNER = "claude-worker-nf01920"


class _Store:
    """The two store calls core makes, over the real normalize/digest policy."""

    NeedFixError = needfix_store.NeedFixError
    NeedFixConflictError = needfix_store.NeedFixConflictError
    NeedFixValidationError = needfix_store.NeedFixValidationError
    normalize_task_plan = staticmethod(needfix_store.normalize_task_plan)
    plan_digest = staticmethod(needfix_store.plan_digest)

    def __init__(self) -> None:
        self.built_cards: list[dict[str, Any]] = []

    def preview_convert(self, repo_root: Any, needfix_id: str, task_plan: Any = None) -> dict[str, Any]:
        card = needfix_store.normalize_task_plan(ROW, task_plan)
        return {
            "needfix_id": needfix_id,
            "already_converted": False,
            "claimable": True,
            "convertible": True,
            "task_plan": needfix_store._signed_task_plan(card),
            "plan_digest": needfix_store.plan_digest(card),
            "task_id": card["task_id"],
        }

    def convert_needfix(
        self, repo_root: Any, needfix_id: str, create_task_fn: Any, *, task_card_builder: Any
    ) -> dict[str, Any]:
        card = task_card_builder(dict(ROW))
        self.built_cards.append(card)
        return {"ok": True, "needfix_id": needfix_id, "converted_task_id": card["task_id"]}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Store:
    double = _Store()
    monkeypatch.setattr(core, "_needfix_store_module", lambda: double)
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(card_contract_advice, "contract_advice", lambda *_a, **_k: {})
    return double


@pytest.fixture
def rank_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _rank(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "selected_execution_runner": RANKED_RUNNER,
            "launch_contract": {"runner": RANKED_RUNNER, "task_id": kwargs["task_id"]},
        }

    monkeypatch.setattr(server.manager_ai_tools, "workforce_rank", _rank)
    return calls


def _shape_error_plan() -> dict[str, Any]:
    return {
        "required_outputs": [SCOPE],
        "acceptance": "plan shapes normalize",
        "action": "convert",
        "task_id": "caller-chosen-id",
    }


def test_shape_errors_preview_ok_list_each_normalization_and_convert_commits(
    store: _Store, rank_calls: list[dict[str, Any]]
) -> None:
    preview = server.needfix_preview_convert(NEEDFIX_ID, task_plan=_shape_error_plan())

    assert preview["convertible"] is True, preview
    assert preview["plan_normalizations"] == [
        {"field": "acceptance", "normalization": "str_to_list"},
        {
            "field": "action",
            "normalization": "dropped_stray_key",
            "warning": "task_plan.action is not a plan field and was ignored",
        },
        {
            "field": "task_id",
            "normalization": "dropped_stray_key",
            "warning": "task_plan.task_id is not a plan field and was ignored",
        },
        {"field": "runner", "normalization": "filled", "filled_from": "workforce_rank:top1"},
        {"field": "topic", "normalization": "filled", "filled_from": "needfix_conversion_default"},
    ]
    assert preview["filled_from"] == {
        "runner": "workforce_rank:top1",
        "topic": "needfix_conversion_default",
    }
    assert len(preview["warnings"]) == 2
    assert preview["task_plan"]["runner"] == RANKED_RUNNER
    assert preview["task_plan"]["topic"] == "needfix_conversion"
    assert rank_calls == [
        {"task_id": f"needfix-{NEEDFIX_ID}", "kinds": ["code"], "risk": "medium"}
    ]
    # The minted id is never the caller's stray task_id.
    assert preview["task_id"] == f"needfix-{NEEDFIX_ID}"

    committed = server.needfix_convert(NEEDFIX_ID, plan_digest=preview["plan_digest"])

    assert committed["converted_task_id"] == f"needfix-{NEEDFIX_ID}"
    card = store.built_cards[-1]
    assert card["acceptance"] == ["plan shapes normalize"]
    assert card["runner"] == RANKED_RUNNER
    assert card["topic"] == "needfix_conversion"
    assert "action" not in card


def test_digest_binds_the_normalized_plan(store: _Store, rank_calls: list[dict[str, Any]]) -> None:
    shaped = server.needfix_preview_convert(NEEDFIX_ID, task_plan=_shape_error_plan())
    clean = server.needfix_preview_convert(
        NEEDFIX_ID,
        task_plan={
            "required_outputs": [SCOPE],
            "acceptance": ["plan shapes normalize"],
            "runner": RANKED_RUNNER,
            "topic": "needfix_conversion",
        },
    )

    assert shaped["plan_digest"] == clean["plan_digest"]
    assert "plan_normalizations" not in clean
    assert len(rank_calls) == 1  # a plan naming runner and topic is never ranked

    # Resending the raw plan normalizes identically, so the digest still binds.
    committed = server.needfix_convert(
        NEEDFIX_ID, task_plan=_shape_error_plan(), plan_digest=shaped["plan_digest"]
    )
    assert committed["converted_task_id"] == f"needfix-{NEEDFIX_ID}"


def test_explicit_runner_and_topic_are_never_overridden(
    store: _Store, rank_calls: list[dict[str, Any]]
) -> None:
    plan = {"required_outputs": [SCOPE], "runner": "codex", "topic": "release-x"}

    preview = server.needfix_preview_convert(NEEDFIX_ID, task_plan=plan)

    assert preview["task_plan"]["runner"] == "codex"
    assert preview["task_plan"]["topic"] == "release-x"
    assert "filled_from" not in preview
    assert rank_calls == []


def test_a_runner_the_ranking_cannot_name_still_fails_closed(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        server.manager_ai_tools, "workforce_rank", lambda **_k: {"ok": False, "launch_contract": None}
    )

    with pytest.raises(needfix_store.NeedFixValidationError, match="runner"):
        server.needfix_preview_convert(NEEDFIX_ID, task_plan={"required_outputs": [SCOPE]})


def test_other_unknown_fields_and_wrong_types_are_still_refused(
    store: _Store, rank_calls: list[dict[str, Any]]
) -> None:
    with pytest.raises(needfix_store.NeedFixValidationError, match="unsupported fields"):
        server.needfix_preview_convert(
            NEEDFIX_ID, task_plan={**_shape_error_plan(), "bogus": 1}
        )
    with pytest.raises(needfix_store.NeedFixValidationError, match="acceptance"):
        server.needfix_preview_convert(
            NEEDFIX_ID, task_plan={"required_outputs": [SCOPE], "acceptance": 7}
        )


def test_shape_coercion_is_deterministic_and_leaves_the_input_untouched() -> None:
    raw = _shape_error_plan()
    first = needfix_store.coerce_task_plan_shape(raw)
    second = needfix_store.coerce_task_plan_shape(dict(raw))

    assert first == second
    assert raw == _shape_error_plan()
    assert needfix_store.coerce_task_plan_shape(None) == (None, [])
    assert needfix_store.coerce_task_plan_shape("not a plan") == ("not a plan", [])
    blank, notes = needfix_store.coerce_task_plan_shape({"acceptance": "   "})
    assert blank == {"acceptance": "   "} and notes == []  # validation still refuses it


def test_plan_less_preview_is_unchanged(store: _Store, rank_calls: list[dict[str, Any]]) -> None:
    with pytest.raises(needfix_store.NeedFixValidationError, match="runner"):
        server.needfix_preview_convert(NEEDFIX_ID)
    assert rank_calls == []


def test_namespace_double_matches_the_store_surface_core_uses() -> None:
    # Guard the double: every attribute core reads from the store module exists.
    for name in (
        "preview_convert",
        "convert_needfix",
        "normalize_task_plan",
        "plan_digest",
        "NeedFixError",
        "NeedFixConflictError",
    ):
        assert hasattr(needfix_store, name), name
    assert callable(needfix_store._signed_task_plan)
