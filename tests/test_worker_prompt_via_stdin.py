"""Tests for carrying the worker prompt through stdin instead of argv (NF-2026-00042)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import runtime_adapters, windows_appcontainer, worker_supervisor  # noqa: E402

_OTHER_ARGV_PROMPT_ADAPTERS = (
    "deepseek_copilot_cli",
    "glm_copilot_cli",
    "grok_kilo_cli",
    "opencode_cli",
)


@pytest.fixture(autouse=True)
def _exercise_portable_adapter_planning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep command-shape tests independent of the executing host OS."""

    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: False)


def _fake_executable(tmp_path: Path, name: str) -> Path:
    executable = tmp_path / name
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable.resolve()


def test_claude_and_codex_plans_carry_the_prompt_in_stdin_text_not_argv(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    prompt = "Implement the focused change; keep $TOKEN literal"
    claude_executable = _fake_executable(tmp_path, "claude")
    codex_executable = _fake_executable(tmp_path, "codex")

    claude_plan = runtime_adapters.build_runtime_command(
        "claude_cli",
        prompt,
        repo,
        executable_overrides={"claude_cli": str(claude_executable)},
    )
    assert prompt not in claude_plan.argv
    assert claude_plan.stdin_text == prompt
    assert "-p" in claude_plan.argv

    codex_plan = runtime_adapters.build_runtime_command(
        "codex_cli",
        prompt,
        repo,
        executable_overrides={"codex_cli": str(codex_executable)},
    )
    assert prompt not in codex_plan.argv
    assert codex_plan.argv[-1] == "-"
    assert codex_plan.stdin_text == prompt


def test_other_local_adapters_keep_the_prompt_in_argv_and_carry_no_stdin_text(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    prompt = "Prompt"
    for adapter_id in _OTHER_ARGV_PROMPT_ADAPTERS:
        executable = _fake_executable(tmp_path, f"exe-{adapter_id}")
        plan = runtime_adapters.build_runtime_command(
            adapter_id,
            prompt,
            repo,
            model="opencode/big-pickle" if adapter_id == "opencode_cli" else None,
            executable_overrides={adapter_id: str(executable)},
        )
        assert plan.launchable is True, plan.validation_reason
        assert plan.stdin_text is None
        assert prompt in plan.argv


def test_forty_thousand_character_prompt_stays_under_the_command_line_limit(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = _fake_executable(tmp_path, "claude")
    prompt = "x" * 40_000

    plan = runtime_adapters.build_runtime_command(
        "claude_cli",
        prompt,
        repo,
        executable_overrides={"claude_cli": str(executable)},
    )

    assert plan.launchable is True
    assert plan.stdin_text == prompt
    command_line = windows_appcontainer.build_command_line(plan.argv)
    assert len(command_line) < 32767


def _minimal_supervisor_spec(tmp_path: Path, argv: list[str]) -> dict[str, Any]:
    process_dir = tmp_path / "processes"
    process_dir.mkdir(mode=0o700)
    return {
        "argv": argv,
        "cwd": str(tmp_path),
        "timeout_seconds": 10,
        "status_path": str(process_dir / "status.json"),
        "cancel_path": str(process_dir / "cancel.json"),
        "stdout_path": str(process_dir / "stdout.log"),
        "stderr_path": str(process_dir / "stderr.log"),
    }


def test_supervisor_plain_branch_delivers_stdin_text_to_a_real_child_that_sees_eof(
    tmp_path: Path,
) -> None:
    argv = [
        sys.executable,
        "-c",
        "import sys; data = sys.stdin.read(); sys.stdout.write(data + '<EOF>')",
    ]
    spec = _minimal_supervisor_spec(tmp_path, argv)

    code = worker_supervisor.supervise(spec, stdin_text="hello from the launcher")

    assert code == 0
    assert Path(spec["stdout_path"]).read_text(encoding="utf-8") == "hello from the launcher<EOF>"


def test_supervisor_plain_branch_is_unchanged_when_stdin_text_is_none(tmp_path: Path) -> None:
    argv = [
        sys.executable,
        "-c",
        "import sys; data = sys.stdin.read(); sys.stdout.write(repr(data))",
    ]
    spec = _minimal_supervisor_spec(tmp_path, argv)

    code = worker_supervisor.supervise(spec)

    assert code == 0
    assert Path(spec["stdout_path"]).read_text(encoding="utf-8") == "''"


def _read_native_handle(handle: int) -> bytes:
    """Read a stdin handle the launcher passed; on Windows it is an OS HANDLE, not an fd."""
    if os.name == "nt":
        import msvcrt

        handle = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    with os.fdopen(handle, "rb", buffering=0, closefd=False) as stream:
        return stream.read()


def test_appcontainer_branch_uses_a_pipe_for_stdin_text_and_the_null_device_otherwise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests: list[Any] = []

    class _FakeLaunch:
        job = object()
        pid = 4242

    def fake_launch_appcontainer(request: Any) -> Any:
        requests.append(request)
        return _FakeLaunch()

    monkeypatch.setattr(
        worker_supervisor.windows_appcontainer, "launch_appcontainer", fake_launch_appcontainer
    )
    spec = {"repo_id": "repo-test", "worker_kind": "claude_cli"}
    argv = [sys.executable, "-c", "pass"]

    worker_supervisor._launch_appcontainer_process(argv, str(tmp_path), spec)
    assert _read_native_handle(requests[0].stdin_handle) == b""

    worker_supervisor._launch_appcontainer_process(
        argv, str(tmp_path), spec, stdin_text="the prompt"
    )
    assert _read_native_handle(requests[1].stdin_handle) == b"the prompt"
