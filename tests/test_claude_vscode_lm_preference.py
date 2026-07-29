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
