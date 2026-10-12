"""Claude Code workers follow the newest model via its own family aliases.

Claude Code's ``--model`` help documents that an alias (``opus``, ``sonnet``,
``haiku``) always resolves to the latest model of that family, so
``runtime_adapters._claude_cli_model_id`` maps every canonical Claude
workforce id -- and every bare alias -- to that family alias instead of a
pinned full id. An explicit, owner-pinned id that is neither a bare alias nor
a canonical workforce id passes through unchanged.
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


@pytest.mark.parametrize(
    ("model", "alias"),
    [
        ("sonnet", "sonnet"),
        ("claude-sonnet-5", "sonnet"),
        ("opus", "opus"),
        ("claude-opus-5", "opus"),
        ("haiku", "haiku"),
        ("claude-haiku-4.5", "haiku"),
    ],
)
def test_claude_cli_argv_uses_the_family_alias_for_bare_and_canonical_models(
    monkeypatch, tmp_path, model, alias
):
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
    assert plan.argv[model_index + 1] == alias


def test_claude_cli_argv_passes_an_explicit_non_canonical_id_through_unchanged(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = _executable(tmp_path, "claude")
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda binary: str(executable))

    plan = runtime_adapters.build_runtime_command(
        "claude_cli",
        "Prompt",
        repo,
        model="claude-opus-5-5",
    )

    model_index = plan.argv.index("--model")
    assert plan.argv[model_index + 1] == "claude-opus-5-5"


def test_record_usage_records_the_claude_cli_init_resolution(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    stdout_path = tmp_path / "stdout.jsonl"
    _stream_json(
        stdout_path,
        {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5", "session_id": "s1"},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )

    manager = process_launcher.ProcessManager(repo=repo)
    manager._record_usage("req-1", "task-1", "runner-1", "claude_cli", "sonnet", stdout_path)

    models = {entry["model"]: entry["label"] for entry in cli_model_discovery.claude_models(repo)}
    assert models["sonnet"] == "claude-sonnet-5-5 (sonnet)"


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

    models = {entry["model"]: entry["label"] for entry in cli_model_discovery.claude_models(repo)}
    assert models["sonnet"] == "sonnet"


def test_record_usage_records_nothing_and_does_not_raise_on_a_corrupt_stream(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    stdout_path = tmp_path / "stdout.jsonl"
    stdout_path.write_text("not json at all\n{broken\n", encoding="utf-8")

    manager = process_launcher.ProcessManager(repo=repo)
    manager._record_usage("req-3", "task-3", "runner-1", "claude_cli", "sonnet", stdout_path)

    models = {entry["model"]: entry["label"] for entry in cli_model_discovery.claude_models(repo)}
    assert models["sonnet"] == "sonnet"


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
