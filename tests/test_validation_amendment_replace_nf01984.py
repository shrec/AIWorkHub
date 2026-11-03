"""NF-2026-01984: validation_amendment_mode replace/remove on reject_review and
recover_blocked_rework, so a manager can drop or swap an environment-broken
declared validation instead of creating a workaround card."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, task_store  # noqa: E402

from test_validation_amendment_nf01151 import _init_repo, _insert_task  # noqa: E402

_RUNNER = "claude_task_mcp_runtime_wiring"
_TOPIC = "task_mcp"
_OLD_REPRO = "pytest -q tests/test_old_repro_nf01984.py"
_OLD_REGRESSION = "pytest -q tests/test_old_regression_nf01984.py"
_NEW_REPRO = "pytest -q tests/test_new_repro_nf01984.py"
_NEW_REGRESSION = "pytest -q tests/test_new_regression_nf01984.py"


def _reviewed_card(monkeypatch, tmp_path, task_id: str, extra_card: dict) -> Path:
    root = _init_repo(tmp_path)
    _insert_task(root, task_id, _RUNNER, _TOPIC, extra_card=extra_card)
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_verified_manager_actor", lambda: "nf01984-manager")
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)
    assert core.claim_start_exact(task_id, _RUNNER, _TOPIC)["ok"] is True
    assert core.mark_review(task_id, runner=_RUNNER, topic=_TOPIC)["ok"] is True
    return root


def _bugfix_card() -> dict:
    return {
        "work_kind": "bugfix",
        "validation": [_OLD_REPRO, _OLD_REGRESSION],
        "validation_roles": ["reproduction", "regression"],
    }


def test_replace_drops_old_commands_and_installs_new_ones_with_roles(monkeypatch, tmp_path):
    task_id = "NF01984_REPLACE"
    root = _reviewed_card(monkeypatch, tmp_path, task_id, _bugfix_card())

    result = core.reject_review(
        task_id,
        "swap environment-broken validation",
        topic=_TOPIC,
        validation_amendment=[_NEW_REPRO, _NEW_REGRESSION],
        validation_amendment_mode="replace",
        validation_roles_amendment=["reproduction", "regression"],
    )

    assert result["ok"] is True, result
    assert result["validation_amendment_mode"] == "replace"
    assert result["validation_amendment_applied"] == [_NEW_REPRO, _NEW_REGRESSION]
    assert "validation_removed" not in result
    card = task_store.get_task(root, task_id)
    assert card["validation"] == [_NEW_REPRO, _NEW_REGRESSION]
    assert card["validation_roles"] == ["reproduction", "regression"]
    history = card["validation_amendments"]
    assert len(history) == 1
    entry = history[0]
    assert entry["mode"] == "replace"
    assert entry["before"] == [_OLD_REPRO, _OLD_REGRESSION]
    assert entry["after"] == [_NEW_REPRO, _NEW_REGRESSION]
    assert entry["at"]


def test_append_mode_is_unchanged(monkeypatch, tmp_path):
    task_id = "NF01984_APPEND"
    root = _reviewed_card(
        monkeypatch,
        tmp_path,
        task_id,
        {"validation": ["pytest -q tests/existing.py"], "validation_roles": ["generic"]},
    )

    result = core.reject_review(
        task_id,
        "add coverage",
        topic=_TOPIC,
        validation_amendment=["pytest -q tests/existing.py", _NEW_REPRO],
        validation_amendment_mode="append",
    )

    assert result["ok"] is True, result
    assert result["validation_amendment_applied"] == [_NEW_REPRO]
    assert "validation_amendment_mode" not in result
    card = task_store.get_task(root, task_id)
    assert card["validation"] == ["pytest -q tests/existing.py", _NEW_REPRO]
    assert card["validation_roles"] == ["generic", "generic"]
    assert "validation_amendments" not in card


def test_remove_unknown_command_is_refused_and_card_untouched(monkeypatch, tmp_path):
    task_id = "NF01984_REMOVE_UNKNOWN"
    root = _reviewed_card(monkeypatch, tmp_path, task_id, _bugfix_card())
    before = task_store.get_task(root, task_id)

    result = core.reject_review(
        task_id,
        "drop broken validation",
        topic=_TOPIC,
        validation_amendment=["pytest -q tests/test_not_on_card_nf01984.py"],
        validation_amendment_mode="remove",
    )

    assert result["ok"] is False
    assert result["stderr"] == "validation_amendment_remove_unknown"
    assert task_store.get_task(root, task_id) == before


def test_remove_existing_command_drops_its_role_entry(monkeypatch, tmp_path):
    task_id = "NF01984_REMOVE"
    broken = "pytest -q tests/test_env_broken_nf01984.py"
    root = _reviewed_card(
        monkeypatch,
        tmp_path,
        task_id,
        {
            "work_kind": "bugfix",
            "validation": [_OLD_REPRO, broken, _OLD_REGRESSION],
            "validation_roles": ["reproduction", "generic", "regression"],
        },
    )

    result = core.reject_review(
        task_id,
        "drop environment-broken validation",
        topic=_TOPIC,
        validation_amendment=[broken],
        validation_amendment_mode="remove",
    )

    assert result["ok"] is True, result
    assert result["validation_amendment_mode"] == "remove"
    assert result["validation_removed"] == [broken]
    card = task_store.get_task(root, task_id)
    assert card["validation"] == [_OLD_REPRO, _OLD_REGRESSION]
    assert card["validation_roles"] == ["reproduction", "regression"]
    entry = card["validation_amendments"][-1]
    assert entry["mode"] == "remove"
    assert entry["before"] == [_OLD_REPRO, broken, _OLD_REGRESSION]
    assert entry["after"] == [_OLD_REPRO, _OLD_REGRESSION]


def test_replace_breaking_bugfix_role_gate_is_refused_and_card_untouched(
    monkeypatch, tmp_path
):
    task_id = "NF01984_REPLACE_GATE"
    root = _reviewed_card(monkeypatch, tmp_path, task_id, _bugfix_card())
    before = task_store.get_task(root, task_id)

    result = core.reject_review(
        task_id,
        "swap validation",
        topic=_TOPIC,
        validation_amendment=[_NEW_REPRO],
        validation_amendment_mode="replace",
        validation_roles_amendment=["generic"],
    )

    assert result["ok"] is False
    assert result["stderr"].startswith("behavioral_validation_roles_missing:")
    assert task_store.get_task(root, task_id) == before


def test_remove_breaking_bugfix_role_gate_is_refused(monkeypatch, tmp_path):
    task_id = "NF01984_REMOVE_GATE"
    root = _reviewed_card(monkeypatch, tmp_path, task_id, _bugfix_card())
    before = task_store.get_task(root, task_id)

    result = core.reject_review(
        task_id,
        "drop validation",
        topic=_TOPIC,
        validation_amendment=[_OLD_REGRESSION],
        validation_amendment_mode="remove",
    )

    assert result["ok"] is False
    assert result["stderr"] == "behavioral_validation_roles_missing:regression"
    assert task_store.get_task(root, task_id) == before


def test_replace_without_roles_on_card_with_roles_is_refused(monkeypatch, tmp_path):
    task_id = "NF01984_REPLACE_NO_ROLES"
    root = _reviewed_card(monkeypatch, tmp_path, task_id, _bugfix_card())
    before = task_store.get_task(root, task_id)

    result = core.reject_review(
        task_id,
        "swap validation",
        topic=_TOPIC,
        validation_amendment=[_NEW_REPRO, _NEW_REGRESSION],
        validation_amendment_mode="replace",
    )

    assert result["ok"] is False
    assert result["stderr"] == "validation_amendment_roles_invalid"
    assert task_store.get_task(root, task_id) == before


@pytest.mark.parametrize(
    ("to", "mode", "expected"),
    [
        ("blocked", "replace", "validation_amendment_mode_requires_pending"),
        ("archived", "remove", "validation_amendment_mode_requires_pending"),
        ("pending", "swap", "validation_amendment_mode_invalid"),
    ],
)
def test_mode_refused_before_write(monkeypatch, to, mode, expected):
    monkeypatch.setattr(
        core,
        "_live_card",
        lambda task_id: ({"task_id": task_id, "topic": "t", "validation": [_OLD_REPRO]}, None),
    )
    monkeypatch.setattr(
        core,
        "_canonical_write_gate",
        lambda *a, **kw: pytest.fail("write gate must not run when the mode is refused"),
    )

    result = core.reject_review(
        "T1",
        "reason",
        topic="t",
        to=to,
        validation_amendment=[_OLD_REPRO],
        validation_amendment_mode=mode,
    )

    assert result["ok"] is False
    assert result["stderr"] == expected


def test_apply_validation_amendment_mode_append_delegates_unchanged():
    card = {"validation": ["pytest -q tests/a.py"], "validation_roles": ["generic"]}
    applied = task_store.apply_validation_amendment_mode(
        card, ["pytest -q tests/b.py"], mode="append"
    )
    assert applied == ["pytest -q tests/b.py"]
    assert card == {
        "validation": ["pytest -q tests/a.py", "pytest -q tests/b.py"],
        "validation_roles": ["generic", "generic"],
    }


def test_apply_validation_amendment_mode_remove_to_empty_is_refused():
    card = {"validation": ["pytest -q tests/a.py"]}
    with pytest.raises(ValueError, match="validation_amendment_empty_result"):
        task_store.apply_validation_amendment_mode(
            card, ["pytest -q tests/a.py"], mode="remove"
        )
    assert card == {"validation": ["pytest -q tests/a.py"]}


def test_core_recover_blocked_rework_forwards_mode_and_roles(monkeypatch):
    calls = []
    card = {"task_id": "T_BLOCKED", "topic": "blocked_rework", "validation": [_OLD_REPRO]}
    monkeypatch.setattr(core, "_live_card", lambda task_id: (card, None))
    monkeypatch.setattr(core, "_canonical_write_gate", lambda *a, **kw: None)

    def recover(root, task_id, **kwargs):
        calls.append(kwargs)
        kwargs["applied_out"].extend(kwargs["validation_amendment"])
        return True, "recovered"

    monkeypatch.setattr(task_store, "recover_blocked_rework", recover)
    monkeypatch.setattr(task_store, "get_task", lambda root, task_id: card)
    monkeypatch.setattr(core, "_reconcile_retained_workspaces", lambda result: result)

    result = core.recover_blocked_rework(
        "T_BLOCKED",
        topic="blocked_rework",
        validation_amendment=[_NEW_REPRO],
        validation_amendment_mode="replace",
        validation_roles_amendment=["generic"],
    )

    assert result["ok"] is True, result
    assert calls[0]["validation_amendment_mode"] == "replace"
    assert calls[0]["validation_roles_amendment"] == ["generic"]
    assert result["validation_amendment_mode"] == "replace"
    assert result["validation_amendment_applied"] == [_NEW_REPRO]
