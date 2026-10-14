"""Claude Code workers and the Manager picker run the pinned 5.5 family.

A Claude Code alias is not guaranteed to be the newest model of its family
(measured on Claude Code 2.1.280: ``sonnet`` resolved to claude-sonnet-5 and
``haiku`` to 4.5), so ``runtime_adapters._claude_cli_model_id`` puts the
explicit id ``cli_model_discovery.CLAUDE_CLI_PINNED_MODELS`` pins on
``--model`` for every bare alias and every legacy canonical spelling; any
other explicit id passes through unchanged. A claude_cli workforce route
validates the pinned id, its alias and a legacy pin alike, while vscode_lm
keeps its exact Copilot ids.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import cli_model_discovery, process_launcher, runtime_adapters  # noqa: E402


@pytest.fixture(autouse=True)
def _exercise_portable_adapter_planning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep command-shape tests independent of the executing host OS."""

    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: False)


def _executable(tmp_path: Path, name: str) -> Path:
    executable = tmp_path / name
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable.resolve()


def _stream_json(path: Path, *events: dict) -> None:
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def _claude_cli_model_argument(monkeypatch, tmp_path: Path, model: str) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = _executable(tmp_path, "claude")
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda binary: str(executable))

    plan = runtime_adapters.build_runtime_command(
        "claude_cli",
        "Prompt",
        repo,
        model=model,
    )

    model_index = plan.argv.index("--model")
    return plan.argv[model_index + 1]


def _init_stream(path: Path, model: str) -> None:
    _stream_json(
        path,
        {"type": "system", "subtype": "init", "model": model, "session_id": "s1"},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )


def test_every_claude_cli_alias_is_pinned_to_one_explicit_id():
    pinned = cli_model_discovery.CLAUDE_CLI_PINNED_MODELS
    assert tuple(pinned) == cli_model_discovery.CLAUDE_CLI_ALIASES
    assert dict(pinned) == {
        "opus": "claude-opus-5-5",
        "sonnet": "claude-sonnet-5-5",
        "haiku": "claude-haiku-5-5",
        "fable": "claude-fable-5-1",
    }


@pytest.mark.parametrize(
    ("model", "pinned"),
    [
        ("opus", "claude-opus-5-5"),
        ("claude-opus-5", "claude-opus-5-5"),
        ("claude-opus-5-5", "claude-opus-5-5"),
        ("sonnet", "claude-sonnet-5-5"),
        ("claude-sonnet-5", "claude-sonnet-5-5"),
        ("claude-sonnet-5-5", "claude-sonnet-5-5"),
        ("haiku", "claude-haiku-5-5"),
        ("claude-haiku-4.5", "claude-haiku-5-5"),
        ("claude-haiku-5-5", "claude-haiku-5-5"),
        ("fable", "claude-fable-5-1"),
        ("claude-fable-5-1", "claude-fable-5-1"),
    ],
)
def test_claude_cli_argv_uses_the_pinned_id_for_every_alias_and_legacy_spelling(
    monkeypatch, tmp_path, model, pinned
):
    assert _claude_cli_model_argument(monkeypatch, tmp_path, model) == pinned


def test_claude_cli_argv_passes_an_explicit_unknown_id_through_unchanged(
    monkeypatch, tmp_path
):
    assert (
        _claude_cli_model_argument(monkeypatch, tmp_path, "claude-sonnet-4.6")
        == "claude-sonnet-4.6"
    )


def test_claude_sonnet_5_validates_the_pinned_sonnet_id_on_claude_cli():
    # Measured 2026-10-14: this launch was refused with
    # workforce_model_mismatch:...:expected=claude-sonnet-5:got=claude-sonnet-5-5.
    assert (
        process_launcher.validate_workforce_identity(
            "claude_sonnet-5", "claude_cli", "claude-sonnet-5-5"
        )
        == "claude-sonnet-5-5"
    )


@pytest.mark.parametrize(
    ("runner", "model", "pinned"),
    [
        ("claude_opus-5", "opus", "claude-opus-5-5"),
        ("claude_opus-5", "claude-opus-5", "claude-opus-5-5"),
        ("claude_opus-5", "claude-opus-5-5", "claude-opus-5-5"),
        ("claude_sonnet-5", "sonnet", "claude-sonnet-5-5"),
        ("claude_sonnet-5", "claude-sonnet-5", "claude-sonnet-5-5"),
        ("claude_haiku", "haiku", "claude-haiku-5-5"),
        ("claude_haiku", "claude-haiku-4.5", "claude-haiku-5-5"),
        ("claude_haiku", "claude-haiku-5-5", "claude-haiku-5-5"),
        ("claude_haiku-4.5", "claude-haiku-4.5", "claude-haiku-5-5"),
        ("claude_haiku-4.5", "claude-haiku-5-5", "claude-haiku-5-5"),
    ],
)
def test_a_claude_cli_route_resolves_alias_and_legacy_pins_to_the_pinned_id(
    runner, model, pinned
):
    assert process_launcher.validate_workforce_identity(runner, "claude_cli", model) == pinned


def test_vscode_lm_routes_keep_their_exact_copilot_ids():
    assert (
        process_launcher.validate_workforce_identity(
            "claude_sonnet-5", "vscode_lm", "claude-sonnet-5"
        )
        == "claude-sonnet-5"
    )
    assert (
        process_launcher.validate_workforce_identity(
            "claude_haiku-4.5", "vscode_lm", "claude-haiku-4.5"
        )
        == "claude-haiku-4.5"
    )
    # A documented alias on a vscode_lm route still names its Copilot id.
    assert (
        process_launcher.validate_workforce_identity("claude_sonnet-5", "vscode_lm", "sonnet")
        == "claude-sonnet-5"
    )
    assert (
        process_launcher.validate_workforce_identity("claude_haiku-4.5", "vscode_lm", "haiku")
        == "claude-haiku-4.5"
    )
    with pytest.raises(process_launcher.LaunchRejected, match="workforce_model_mismatch"):
        process_launcher.validate_workforce_identity(
            "claude_sonnet-5", "vscode_lm", "claude-sonnet-5-5"
        )


def test_the_manager_picker_keeps_the_alias_and_labels_the_pinned_id(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    # A recorded resolution is observed evidence; it never relabels the picker.
    cli_model_discovery.record_claude_resolution(repo, "sonnet", "claude-sonnet-5")

    assert cli_model_discovery.claude_models(repo) == [
        {"model": "opus", "label": "claude-opus-5-5 (opus)"},
        {"model": "sonnet", "label": "claude-sonnet-5-5 (sonnet)"},
        {"model": "haiku", "label": "claude-haiku-5-5 (haiku)"},
        {"model": "fable", "label": "claude-fable-5-1 (fable)"},
    ]


@pytest.mark.parametrize("model", ["sonnet", "claude-sonnet-5", "claude-sonnet-5-5"])
def test_record_usage_records_the_init_resolution_under_its_alias(tmp_path, model):
    repo = tmp_path / "repo"
    repo.mkdir()
    stdout_path = tmp_path / "stdout.jsonl"
    _init_stream(stdout_path, "claude-sonnet-5-5")

    manager = process_launcher.ProcessManager(repo=repo)
    manager._record_usage("req-1", "task-1", "runner-1", "claude_cli", model, stdout_path)

    resolutions = cli_model_discovery._read_resolutions(repo)
    assert set(resolutions) == {"sonnet"}
    assert resolutions["sonnet"]["resolved"] == "claude-sonnet-5-5"


def test_record_usage_records_nothing_for_an_explicit_unknown_id(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    stdout_path = tmp_path / "stdout.jsonl"
    _init_stream(stdout_path, "claude-sonnet-4.6")

    manager = process_launcher.ProcessManager(repo=repo)
    manager._record_usage(
        "req-4", "task-4", "runner-1", "claude_cli", "claude-sonnet-4.6", stdout_path
    )

    assert cli_model_discovery._read_resolutions(repo) == {}


def test_record_usage_records_nothing_without_an_init_event(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    stdout_path = tmp_path / "stdout.jsonl"
    _stream_json(
        stdout_path,
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )

    manager = process_launcher.ProcessManager(repo=repo)
    manager._record_usage("req-2", "task-2", "runner-1", "claude_cli", "sonnet", stdout_path)

    assert cli_model_discovery._read_resolutions(repo) == {}


def test_record_usage_records_nothing_and_does_not_raise_on_a_corrupt_stream(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    stdout_path = tmp_path / "stdout.jsonl"
    stdout_path.write_text("not json at all\n{broken\n", encoding="utf-8")

    manager = process_launcher.ProcessManager(repo=repo)
    manager._record_usage("req-3", "task-3", "runner-1", "claude_cli", "sonnet", stdout_path)

    assert cli_model_discovery._read_resolutions(repo) == {}


def test_claude_cli_init_model_reads_the_first_init_event(tmp_path):
    stdout_path = tmp_path / "stdout.jsonl"
    _stream_json(
        stdout_path,
        {"type": "system", "subtype": "init", "model": "claude-opus-5-5", "session_id": "s1"},
        {"type": "result", "subtype": "success"},
    )

    assert cli_model_discovery.claude_init_model(stdout_path) == "claude-opus-5-5"


def test_claude_cli_init_model_is_empty_without_an_init_event(tmp_path):
    stdout_path = tmp_path / "stdout.jsonl"
    _stream_json(stdout_path, {"type": "result", "subtype": "success"})

    assert cli_model_discovery.claude_init_model(stdout_path) == ""


def test_claude_cli_init_model_is_empty_for_a_corrupt_stream(tmp_path):
    stdout_path = tmp_path / "stdout.jsonl"
    stdout_path.write_text("not json at all\n{broken\n", encoding="utf-8")

    assert cli_model_discovery.claude_init_model(stdout_path) == ""


def test_claude_cli_init_model_is_empty_for_a_missing_file(tmp_path):
    assert cli_model_discovery.claude_init_model(tmp_path / "does-not-exist.jsonl") == ""
