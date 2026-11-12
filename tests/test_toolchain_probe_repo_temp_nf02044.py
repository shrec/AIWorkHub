"""NF-2026-02044: the ``--version`` probe cwd lives under the repo temp root.

``probe_executable_version`` must never create its throwaway cwd in the
system temp directory: it reuses ``worker_workspace.configured_temp_root``,
creates that root when missing, removes the probe directory afterwards, and
fails closed (nothing launched) when an explicit repository's temp root is
unavailable. With ``repo=None`` and no repository known it keeps the
pre-change default temp directory.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import runtime_temp, toolchain_authority, worker_workspace


@pytest.fixture(autouse=True)
def _no_ambient_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("AIWORKHUB_REPO_ROOT", "AIWORKHUB_REPO", runtime_temp.TEMP_ROOT_ENV):
        monkeypatch.delenv(name, raising=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".aiworkhub").mkdir(parents=True)
    (repo / ".aiworkhub" / "project.json").write_text("{}", encoding="utf-8")
    return repo


class _RecordingPopen:
    calls: list[dict[str, Any]] = []

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        cwd = Path(kwargs["cwd"])
        type(self).calls.append(
            {"argv": list(argv), "cwd": cwd, "cwd_existed": cwd.is_dir(), "shell": kwargs["shell"]}
        )
        self.returncode = 0
        self.pid = 0

    def __enter__(self) -> _RecordingPopen:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def communicate(self, timeout: float | None = None) -> tuple[None, bytes]:
        return None, b""


@pytest.fixture
def recording_popen(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingPopen]:
    _RecordingPopen.calls = []
    monkeypatch.setattr(toolchain_authority.subprocess, "Popen", _RecordingPopen)
    return _RecordingPopen


def test_probe_cwd_is_inside_repo_temp_root_and_removed(
    tmp_path: Path, recording_popen: type[_RecordingPopen]
) -> None:
    repo = _repo(tmp_path)
    root = worker_workspace.configured_temp_root(repo)
    assert not root.exists()  # created on demand

    probe = toolchain_authority.probe_executable_version("tool", {}, repo=repo)

    assert toolchain_authority.probe_succeeded(probe), probe
    (call,) = recording_popen.calls
    assert call["argv"] == ["tool", "--version"]
    assert call["shell"] is False
    assert call["cwd_existed"] is True
    cwd = call["cwd"].resolve()
    assert cwd.parent == root.resolve()
    assert repo.resolve() in cwd.parents
    assert cwd.name.startswith("aiworkhub-probe-")
    assert not cwd.exists()
    assert root.is_dir()
    assert list(root.iterdir()) == []


def test_probe_resolves_repo_from_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recording_popen: type[_RecordingPopen]
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)

    probe = toolchain_authority.probe_executable_version("tool", {})

    assert toolchain_authority.probe_succeeded(probe), probe
    (call,) = recording_popen.calls
    assert call["cwd"].resolve().parent == worker_workspace.configured_temp_root(repo).resolve()
    assert not call["cwd"].exists()


def test_probe_fails_closed_when_explicit_repo_root_unavailable(
    tmp_path: Path, recording_popen: type[_RecordingPopen]
) -> None:
    repo = _repo(tmp_path)
    root = worker_workspace.configured_temp_root(repo)
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_text("not a directory", encoding="utf-8")

    probe = toolchain_authority.probe_executable_version("tool", {}, repo=repo)

    assert recording_popen.calls == []
    assert probe["launch_error"] == "probe_temp_root_unavailable"
    assert probe["exit_code"] is None
    assert probe["stderr_head"]
    assert not toolchain_authority.probe_succeeded(probe)


def test_probe_without_repository_keeps_default_temp_fallback(
    tmp_path: Path, recording_popen: type[_RecordingPopen]
) -> None:
    probe = toolchain_authority.probe_executable_version("tool", {})

    assert toolchain_authority.probe_succeeded(probe), probe
    assert "launch_error" not in probe
    (call,) = recording_popen.calls
    assert call["cwd_existed"] is True
    assert call["cwd"].name.startswith("aiworkhub-probe-")
    assert not call["cwd"].exists()
    assert tmp_path.resolve() not in call["cwd"].resolve().parents


def test_real_probe_without_repository_succeeds() -> None:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}

    probe = toolchain_authority.probe_executable_version(sys.executable, env)

    assert probe["exit_code"] == 0, probe
    assert "launch_error" not in probe


def test_probe_temp_root_unavailable_is_a_named_error() -> None:
    with pytest.raises(toolchain_authority.ProbeTempRootUnavailable, match="repository_identity"):
        toolchain_authority._probe_temp_root(None)


def test_real_probe_leaves_repo_temp_root_empty(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}

    probe = toolchain_authority.probe_executable_version(sys.executable, env, repo=repo)

    assert probe["exit_code"] == 0, probe
    assert "launch_error" not in probe
    root = worker_workspace.configured_temp_root(repo)
    assert root.is_dir()
    assert list(root.iterdir()) == []


def test_probe_launch_error_still_removes_probe_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)

    def _boom(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        raise FileNotFoundError(2, "missing", argv[0])

    monkeypatch.setattr(toolchain_authority.subprocess, "Popen", _boom)
    probe = toolchain_authority.probe_executable_version("missing-tool", {}, repo=repo)

    assert probe["launch_error"] == "FileNotFoundError"
    assert list(worker_workspace.configured_temp_root(repo).iterdir()) == []
