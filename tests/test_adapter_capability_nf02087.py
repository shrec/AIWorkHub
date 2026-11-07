from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import adapter_capability, process_launcher

SRC = Path(__file__).resolve().parent.parent / "src"


def _card(**overrides):
    card = {
        "runner": "claude_sonnet-5",
        "topic": "nf02087",
        "validation": ["python -m pytest -q"],
    }
    card.update(overrides)
    return card


@pytest.mark.parametrize(
    "command,expected",
    [
        ("cmake -S . -B build", "native_toolchain:cmake"),
        ("C:\\tools\\NMake.exe /f x", "native_toolchain:nmake"),
        ("FOO=1 make test", "native_toolchain:make"),
        ("bash scripts/x.sh", "shell:bash"),
        ("python -m pytest -q tests/x.py", ""),
    ],
)
def test_card_capability_blocker_commands(command, expected):
    card = {"validation": [command]}
    assert adapter_capability.card_capability_blocker(card) == expected


@pytest.mark.parametrize(
    "validation",
    [None, [], "not-a-list", [123, None]],
)
def test_card_capability_blocker_missing_or_empty_validation(validation):
    card = {} if validation is None else {"validation": validation}
    assert adapter_capability.card_capability_blocker(card) == ""


def test_card_capability_blocker_unbalanced_quote_falls_back_to_split():
    card = {"validation": ['cmake --flag="unterminated']}
    assert adapter_capability.card_capability_blocker(card) == "native_toolchain:cmake"


def test_vscode_lm_capability_blocker_reroutes_to_claude_cli(tmp_path, monkeypatch):
    def fake_bridge_readiness(repo, **kwargs):
        return {"ok": True}

    monkeypatch.setattr(
        process_launcher.vscode_lm_bridge, "derived_candidate_blocker", lambda repo, candidate, model: ""
    )

    card = _card(validation=["cmake --build build"])
    identity = process_launcher.derive_launch_identity(tmp_path, card, runner="claude_sonnet-5", topic="nf02087")
    assert identity["adapter_id"] == "claude_cli"
    assert any(
        rejection == "vscode_lm:capability:native_toolchain:cmake"
        for rejection in identity["adapter_rejections"]
    )


def test_vscode_lm_capability_blocker_allows_python_only_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        process_launcher.vscode_lm_bridge, "derived_candidate_blocker", lambda repo, candidate, model: ""
    )

    card = _card(validation=["python -m pytest -q"])
    identity = process_launcher.derive_launch_identity(tmp_path, card, runner="claude_sonnet-5", topic="nf02087")
    assert identity["adapter_id"] == "vscode_lm"


def test_reject_incapable_appends_one_capability_rejection_for_blocked_card():
    rejections: list[str] = []
    blocked = adapter_capability.reject_incapable(
        {"validation": ["cmake --build build"]}, "vscode_lm", rejections
    )
    assert blocked is True
    assert rejections == ["vscode_lm:capability:native_toolchain:cmake"]


def test_reject_incapable_leaves_rejections_unchanged_for_clean_card():
    rejections = ["existing"]
    blocked = adapter_capability.reject_incapable(
        {"validation": ["python -m pytest -q"]}, "vscode_lm", rejections
    )
    assert blocked is False
    assert rejections == ["existing"]
