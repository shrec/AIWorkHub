import pytest

from aiworkhub import process_launcher


def test_claude_subscription_adapter_remains_first_party_cli():
    process_launcher._validate_adapter_identity(
        "claude_sonnet5_task_v1", "claude_cli"
    )


def test_copilot_workforce_is_explicit_and_editor_only():
    process_launcher._validate_adapter_identity(
        "copilot_claude_sonnet46_task_v1", "vscode_lm"
    )
    with pytest.raises(process_launcher.LaunchRejected, match="runner_adapter_mismatch"):
        process_launcher._validate_adapter_identity(
            "copilot_claude_sonnet46_task_v1", "claude_cli"
        )


def test_claude_and_copilot_are_not_silently_interchanged():
    with pytest.raises(process_launcher.LaunchRejected, match="runner_adapter_mismatch"):
        process_launcher._validate_adapter_identity(
            "claude_sonnet5_task_v1", "deepseek_copilot_cli"
        )


def test_claude_subscription_auth_is_checked_before_launch(monkeypatch):
    manager = object.__new__(process_launcher.ProcessManager)
    monkeypatch.setattr(
        process_launcher.claude_auth,
        "auth_status",
        lambda: {
            "launchable": False,
            "blocker_reason": "claude_authentication_required",
        },
    )

    with pytest.raises(
        process_launcher.LaunchRejected,
        match="claude_authentication_unavailable:claude_authentication_required",
    ):
        manager._resolve_provider_env("claude_cli", "claude-sonnet-5")


def test_claude_subscription_auth_never_becomes_copilot_env(monkeypatch):
    manager = object.__new__(process_launcher.ProcessManager)
    monkeypatch.setattr(
        process_launcher.claude_auth,
        "auth_status",
        lambda: {"launchable": True, "blocker_reason": ""},
    )

    provider_env, model = manager._resolve_provider_env(
        "claude_cli", "claude-sonnet-5"
    )

    assert provider_env is None
    assert model == "claude-sonnet-5"
