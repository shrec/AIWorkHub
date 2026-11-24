"""NF-2026-01343: claude_* runners derive claude_cli before vscode_lm.

Measured: vscode_lm on claude_* runners 0/20 successful episodes, claude_cli
469 accepted. The prefixed family now uses the same order as the bare
``claude`` runner.
"""
from __future__ import annotations

import pytest

from aiworkhub import process_launcher, workforce_catalog


@pytest.mark.parametrize("runner", ["claude_haiku", "claude_sonnet-5", "claude_opus-5"])
def test_prefixed_claude_family_prefers_claude_cli(runner):
    assert process_launcher.adapter_identity_tuple(runner) == ("claude_cli", "vscode_lm")


def test_bare_claude_and_other_families_are_unchanged():
    assert process_launcher.adapter_identity_tuple("claude") == ("claude_cli", "vscode_lm")
    assert process_launcher.adapter_identity_tuple("codex_gpt-5.5") == ("vscode_lm", "codex_cli")
    assert process_launcher.adapter_identity_tuple("copilot_x") == ("vscode_lm",)
    assert process_launcher.adapter_identity_tuple("opencode_x") == ("opencode_cli",)


def test_derivation_for_a_launchable_claude_card_picks_claude_cli(tmp_path, monkeypatch):
    # Neutral policy and no route circuit open, so only tuple order decides.
    monkeypatch.setattr(
        workforce_catalog.task_store,
        "route_terminal_observations",
        lambda _root, *, adapter_id, model, since_iso: [],
    )
    monkeypatch.setattr(
        process_launcher.repo_policy,
        "validate_launch",
        lambda _repo, _card, _adapter: {"ok": True},
    )
    card = {
        "task_id": "T_NF01343",
        "runner": "claude_haiku",
        "topic": "coding",
        "task_type": "code",
        "allowed_writes": ["src/a.py"],
    }
    derived = process_launcher.derive_launch_identity(tmp_path, card)
    assert derived["adapter_id"] == "claude_cli"
    assert "adapter_circuit_skipped" not in derived["derived_from"]
