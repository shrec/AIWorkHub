from pathlib import Path

from aiworkhub import process_launcher


def test_visible_claude_model_prefers_editor_auth(monkeypatch):
    monkeypatch.setattr(
        process_launcher.vscode_lm_bridge,
        "bridge_readiness",
        lambda *args, **kwargs: {"launchable": True},
    )

    assert process_launcher._prefer_editor_auth_adapter(
        Path("/repo"),
        runner="claude_sonnet5_task_v1",
        adapter_id="claude_cli",
        model="claude-sonnet-5",
    ) == "vscode_lm"


def test_unavailable_editor_model_preserves_explicit_cli_fallback(monkeypatch):
    monkeypatch.setattr(
        process_launcher.vscode_lm_bridge,
        "bridge_readiness",
        lambda *args, **kwargs: {"launchable": False},
    )

    assert process_launcher._prefer_editor_auth_adapter(
        Path("/repo"),
        runner="claude_sonnet5_task_v1",
        adapter_id="claude_cli",
        model="claude-sonnet-5",
    ) == "claude_cli"


def test_non_claude_runner_is_never_rewritten(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("bridge readiness must not be queried")

    monkeypatch.setattr(
        process_launcher.vscode_lm_bridge,
        "bridge_readiness",
        unexpected,
    )

    assert process_launcher._prefer_editor_auth_adapter(
        Path("/repo"),
        runner="deepseek_v4pro_task_v1",
        adapter_id="deepseek_copilot_cli",
        model="deepseek-v4-pro",
    ) == "deepseek_copilot_cli"
