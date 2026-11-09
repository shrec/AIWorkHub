"""NF-2026-01969: pip --user console-script shims in the validation lane.

The validation lane replaces HOME, so a ``pip install --user cmake`` shim that
the coordinator resolved onto argv[0] could no longer import its own package.
The lane now forwards the caller's PYTHONUSERBASE (validation adapter only),
probes a rewritten argv head with ``--version`` before trusting it, and every
validation record names that choice in ``toolchain_authority_receipt``.
"""

from __future__ import annotations

import getpass
import inspect
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from aiworkhub import build_lane, toolchain_authority, worker_workspace

POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX-only: the user-site shim, the POSIX lane PATH and the "
    "USER/LOGNAME fallback exist only in the POSIX sanitized_env branch",
)
# Generic logins the guard allowlist also accepts; a real login never belongs here.
_PLACEHOLDER_LOGINS = frozenset({"owner", "worker", "user", "dev", "u", "probe-user"})
AGENT_ADAPTERS = ("claude_cli", "codex_cli", "opencode_cli", "grok_kilo_cli")


def _script(directory: Path, name: str, *, exit_code: int, stdout: str = "") -> Path:
    """A tiny launchable ``name`` that prints ``stdout`` and exits ``exit_code``."""
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        path = directory / f"{name}.bat"
        body = "@echo off\r\n"
        body += f"echo {stdout}\r\n" if stdout else ""
        body += f"exit /b {exit_code}\r\n"
    else:
        path = directory / name
        body = "#!/bin/sh\n"
        body += f"echo '{stdout}'\n" if stdout else ""
        body += f"exit {exit_code}\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(body)
    return path


def _lane_env(path_dir: Path) -> dict[str, str]:
    return {**os.environ, "PATH": str(path_dir)}


# --- sanitized_env: PYTHONUSERBASE and the neutral USER/LOGNAME fallback ----


@POSIX_ONLY
def test_validation_env_carries_caller_user_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    user_base = tmp_path / "userbase"
    user_base.mkdir()
    monkeypatch.setenv("PYTHONUSERBASE", str(user_base))

    env = worker_workspace.sanitized_env("validation", home=tmp_path / "home")

    assert env["PYTHONUSERBASE"] == str(user_base.resolve())
    assert env["HOME"] == str((tmp_path / "home").resolve())


@POSIX_ONLY
def test_missing_user_base_is_not_exported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PYTHONUSERBASE", str(tmp_path / "absent"))

    env = worker_workspace.sanitized_env("validation", home=tmp_path / "home")

    assert "PYTHONUSERBASE" not in env
    assert toolchain_authority.validation_user_base_bwrap_binds("validation") == ()


@pytest.mark.parametrize("adapter_id", AGENT_ADAPTERS)
def test_agent_adapters_never_receive_user_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter_id: str
) -> None:
    user_base = tmp_path / "userbase"
    user_base.mkdir()
    monkeypatch.setenv("PYTHONUSERBASE", str(user_base))

    env = worker_workspace.sanitized_env(adapter_id, home=tmp_path / "home")

    assert "PYTHONUSERBASE" not in env
    assert toolchain_authority.validation_user_base_env(adapter_id) == {}
    assert toolchain_authority.validation_user_base_bwrap_binds(adapter_id) == ()


@POSIX_ONLY
def test_bubblewrap_binds_user_site_read_only_for_validation_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    user_base = (tmp_path / "userbase").resolve()
    site_packages = user_base / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    (user_base / "share").mkdir()
    monkeypatch.setenv("PYTHONUSERBASE", str(user_base))
    worktree = tmp_path / "worktree"
    (worktree / ".git").mkdir(parents=True)
    workspace = worker_workspace.WorkerWorkspace(
        request_id="nf01969",
        repo=tmp_path,
        path=worktree,
        home=tmp_path / "home",
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )

    validation = worker_workspace.sandbox_argv(
        workspace, "validation", ["true"], backend="bubblewrap"
    )
    agent = worker_workspace.sandbox_argv(
        workspace, "claude_cli", ["true"], backend="bubblewrap"
    )

    triples = [tuple(validation[i : i + 3]) for i in range(len(validation) - 2)]
    assert ("--ro-bind", str(site_packages), str(site_packages)) in triples
    assert ("--ro-bind", str(user_base), str(user_base)) not in triples
    assert ("--bind", str(site_packages), str(site_packages)) not in triples
    assert str(site_packages) not in agent


@POSIX_ONLY
def test_posix_user_and_logname_fallback_is_neutral(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)

    env = worker_workspace.sanitized_env("validation", home=tmp_path / "home")

    assert env["USER"] == "user"
    assert env["LOGNAME"] == "user"


def test_sanitized_env_source_names_no_personal_user() -> None:
    login = getpass.getuser()
    if login.lower() in _PLACEHOLDER_LOGINS:
        pytest.skip("the login is a generic placeholder, so it proves nothing")
    source = inspect.getsource(worker_workspace.sanitized_env)

    assert re.search(rf"\b{re.escape(login)}\b", source) is None
    assert '"user"' in source


# --- probe and selection -----------------------------------------------------


def test_failed_rewrite_is_kept_and_lane_path_is_never_consulted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broken = _script(tmp_path / "rewritten", "cmake", exit_code=3)
    _script(tmp_path / "lane", "cmake", exit_code=0, stdout="cmake version 3.30")
    calls: list[str] = []

    def runner(executable: str, env: Any) -> dict[str, Any]:
        calls.append(executable)
        return {"exit_code": 3, "timed_out": False, "duration_seconds": 0.0,
                "stderr_head": "boom"}

    def no_which(*args: object, **kwargs: object) -> str:
        raise AssertionError("the lane PATH must never be consulted")

    monkeypatch.setattr(shutil, "which", no_which)
    fact = {"canonical_path": str(broken), "fingerprint": "abc"}

    argv, receipt = toolchain_authority.select_validation_executable(
        ["cmake", "--build", "build"],
        [str(broken), "--build", "build"],
        _lane_env(tmp_path / "lane"),
        toolchain_authority.ExecutableProbeCache(runner),
        fact=fact,
    )

    assert argv == [str(broken), "--build", "build"]
    assert calls == [str(broken)]
    assert receipt["declared_argv"] == ["cmake", "--build", "build"]
    assert receipt["resolved_path"] == str(broken)
    assert receipt["probe"]["exit_code"] == 3
    assert receipt["probe"]["stderr_head"] == "boom"
    assert receipt["choice_reason"] == "rewritten_probe_failed"
    assert receipt["canonical_path"] == str(broken)
    assert receipt["fingerprint"] == "abc"
    assert not {"rewritten_path", "lane_path_candidate", "lane_path_probe"} & set(receipt)


def test_successful_rewrite_is_kept_and_probed_once_per_path(tmp_path: Path) -> None:
    calls: list[str] = []

    def runner(executable: str, env: Any) -> dict[str, Any]:
        calls.append(executable)
        return {"exit_code": 0, "timed_out": False, "duration_seconds": 0.0,
                "stderr_head": ""}

    probes = toolchain_authority.ExecutableProbeCache(runner)
    rewritten = str(tmp_path / "bin" / "cmake")
    for _ in range(3):
        argv, receipt = toolchain_authority.select_validation_executable(
            ["cmake", "--version"], [rewritten, "--version"], {"PATH": ""}, probes
        )
        assert argv == [rewritten, "--version"]
        assert receipt["choice_reason"] == "rewritten_probe_succeeded"
        assert receipt["probe"]["exit_code"] == 0

    assert calls == [rewritten]
    assert probes.probed_paths == (rewritten,)


def test_unrewritten_and_runtime_root_heads_are_recorded_without_probe() -> None:
    def runner(executable: str, env: Any) -> dict[str, Any]:
        raise AssertionError(f"unexpected probe of {executable}")

    probes = toolchain_authority.ExecutableProbeCache(runner)
    fact = {"canonical_path": "/opt/tool", "fingerprint": "abc"}

    same, plain = toolchain_authority.select_validation_executable(
        ["make", "check"], ["make", "check"], {}, probes, fact=fact
    )
    _, rooted = toolchain_authority.select_validation_executable(
        ["ruff", "check"], ["/venv/bin/ruff", "check"], {}, probes, probe=False
    )
    _, interpreter = toolchain_authority.select_validation_executable(
        ["python", "-V"], [sys.executable, "-V"], {}, probes
    )

    assert same == ["make", "check"]
    assert plain["choice_reason"] == "declared_argv_not_rewritten"
    assert plain["canonical_path"] == "/opt/tool"
    for receipt in (plain, rooted, interpreter):
        assert {"declared_argv", "resolved_path", "probe", "choice_reason"} <= set(receipt)
        assert receipt["probe"] is None
        assert receipt["choice_reason"]
    assert rooted["choice_reason"] == "trusted_runtime_root_rewrite_not_probed"
    assert interpreter["choice_reason"] == "coordinator_interpreter_not_probed"


def test_probe_records_launch_error_and_bounds_stderr(tmp_path: Path) -> None:
    missing = toolchain_authority.probe_executable_version(
        str(tmp_path / "absent-tool"), dict(os.environ)
    )
    noisy = toolchain_authority.probe_executable_version(
        sys.executable, dict(os.environ), timeout_seconds=60
    )

    assert missing["exit_code"] is None
    assert missing["launch_error"]
    assert missing["timed_out"] is False
    assert not toolchain_authority.probe_succeeded(missing)
    assert noisy["exit_code"] == 0
    assert toolchain_authority.probe_succeeded(noisy)
    assert len(noisy["stderr_head"].encode("utf-8")) <= 512
    assert noisy["argv"] == [sys.executable, "--version"]


def test_probe_runs_in_a_fresh_empty_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    class FakePopen:
        returncode = 0

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            cwd = kwargs.get("cwd")
            seen.append({**kwargs, "cwd_listing": os.listdir(cwd) if cwd else None})

        def __enter__(self) -> FakePopen:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            return b"", b""

    monkeypatch.setattr(toolchain_authority.subprocess, "Popen", FakePopen)

    probe = toolchain_authority.probe_executable_version("/opt/tool/cmake", {})

    assert probe["exit_code"] == 0
    (kwargs,) = seen
    assert kwargs["cwd"]
    assert Path(kwargs["cwd"]).resolve() != Path(os.getcwd()).resolve()
    assert kwargs["cwd_listing"] == []
    assert not Path(kwargs["cwd"]).exists()
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_user_base_binds_only_site_packages_read_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base = tmp_path / "userbase"
    site_packages = base / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    (base / "share").mkdir()
    monkeypatch.setattr(toolchain_authority, "caller_python_user_base", lambda: base)

    binds = toolchain_authority.validation_user_base_bwrap_binds("validation")

    assert binds == ("--ro-bind", str(site_packages), str(site_packages))
    assert toolchain_authority.validation_user_base_bwrap_binds("claude_cli") == ()


def test_user_base_without_site_packages_binds_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base = tmp_path / "userbase"
    (base / "share").mkdir(parents=True)
    monkeypatch.setattr(toolchain_authority, "caller_python_user_base", lambda: base)

    assert toolchain_authority.validation_user_base_bwrap_binds("validation") == ()


@POSIX_ONLY
def test_filesystem_root_is_never_a_user_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONUSERBASE", "/")

    assert toolchain_authority.caller_python_user_base() is None
    assert toolchain_authority.validation_user_base_bwrap_binds("validation") == ()


# --- docker build lane ------------------------------------------------------


def test_docker_lane_row_carries_non_empty_receipt() -> None:
    lane = SimpleNamespace(
        lane_id="cmake-linux",
        image="example@sha256:" + "0" * 64,
        network="none",
        cache_volume="",
        timeout_seconds=60,
    )
    core = {"docker_argv": ["docker", "run", "image"], "returncode": 0}

    row = build_lane._row("lane:cmake-linux", cast(Any, lane), core)

    receipt = row["toolchain_authority_receipt"]
    assert receipt["build_lane"] == "cmake-linux"
    assert receipt["declared_argv"] == ["lane:cmake-linux"]
    assert receipt["resolved_path"] == "docker"
    assert receipt["probe"] is None
    assert receipt["choice_reason"] == "docker_build_lane"


# --- end to end through run_validations (POSIX) -----------------------------


def _shim_workspace(tmp_path: Path) -> worker_workspace.WorkerWorkspace:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    home = tmp_path / "home"
    for directory in (repo, worktree, home):
        directory.mkdir()
    return worker_workspace.WorkerWorkspace(
        request_id="nf01969",
        repo=repo,
        path=worktree,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )


def _unsandboxed_lane(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run the real lane code path with the OS sandbox wrapper removed."""
    monkeypatch.setattr(worker_workspace, "select_sandbox_backend", lambda: "bubblewrap")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(
        worker_workspace, "provision_validation_exec_scratch", lambda ws, **_: scratch
    )
    monkeypatch.setattr(worker_workspace, "cleanup_validation_exec_scratch", lambda path: None)
    monkeypatch.setattr(
        worker_workspace, "sandbox_argv", lambda ws, adapter_id, argv, **kw: list(argv)
    )


def _user_site(base_python: str, user_base: Path) -> Path:
    probe = subprocess.run(
        [base_python, "-c", "import site; print(int(site.ENABLE_USER_SITE is not False));"
         "print(site.getusersitepackages())"],
        env={**os.environ, "PYTHONUSERBASE": str(user_base)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    lines = probe.stdout.splitlines()
    if probe.returncode != 0 or len(lines) < 2 or lines[0] != "1":
        pytest.skip("the base interpreter has the user site disabled")
    return Path(lines[1])


@POSIX_ONLY
def test_pip_user_shim_runs_through_validation_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base_python = str(getattr(sys, "_base_executable", "") or sys.executable)
    if " " in base_python:
        pytest.skip("a shebang cannot name an interpreter path containing spaces")
    user_base = tmp_path / "userbase"
    site_packages = _user_site(base_python, user_base)
    site_packages.mkdir(parents=True)
    (site_packages / "nf01969_cmake_shim.py").write_text(
        "import sys\n\n\ndef main():\n"
        "    print('nf01969-shim-ok', ' '.join(sys.argv[1:]))\n"
        "    return 0\n",
        encoding="utf-8",
    )
    shim = user_base / "bin" / "cmake"
    shim.parent.mkdir(parents=True)
    fd = os.open(shim, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(
            f"#!{base_python}\nimport sys\nfrom nf01969_cmake_shim import main\n"
            "if __name__ == '__main__':\n    sys.exit(main())\n"
        )
    monkeypatch.setenv("PYTHONUSERBASE", str(user_base))
    monkeypatch.setenv("PATH", os.pathsep.join((str(shim.parent), "/usr/bin", "/bin")))
    _unsandboxed_lane(monkeypatch, tmp_path)

    rows = worker_workspace.run_validations(
        _shim_workspace(tmp_path), ["cmake --build build"]
    )

    row = rows[0]
    assert row["returncode"] == 0, row["stderr_tail"]
    assert "nf01969-shim-ok --build build" in row["stdout_head"]
    assert row["argv_rewritten"] is True
    assert row["executed_argv"][0] == str(shim.resolve())
    receipt = row["toolchain_authority_receipt"]
    assert receipt["declared_argv"] == ["cmake", "--build", "build"]
    assert receipt["resolved_path"] == str(shim.resolve())
    assert receipt["probe"]["exit_code"] == 0
    assert receipt["choice_reason"] == "rewritten_probe_succeeded"


@POSIX_ONLY
def test_broken_rewrite_is_kept_and_probed_once_through_validation_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broken = _script(tmp_path / "host-bin", "cmake", exit_code=7)
    _script(tmp_path / "lane-bin", "cmake", exit_code=0, stdout="lane-cmake")
    monkeypatch.setenv("PATH", os.pathsep.join((str(broken.parent), "/usr/bin", "/bin")))
    real_sanitized_env = worker_workspace.sanitized_env

    def lane_env(*args: Any, **kwargs: Any) -> dict[str, str]:
        env = real_sanitized_env(*args, **kwargs)
        env["PATH"] = str(tmp_path / "lane-bin")
        return env

    monkeypatch.setattr(worker_workspace, "sanitized_env", lane_env)
    probed: list[str] = []
    real_probe = toolchain_authority.probe_executable_version

    def counting_probe(executable: str, env: Any, **kwargs: Any) -> dict[str, Any]:
        probed.append(executable)
        return real_probe(executable, env, **kwargs)

    monkeypatch.setattr(toolchain_authority, "probe_executable_version", counting_probe)
    _unsandboxed_lane(monkeypatch, tmp_path)

    # A failing command fails the batch; its rows ride on the raised error.
    with pytest.raises(worker_workspace.ValidationRunError) as failure:
        worker_workspace.run_validations(
            _shim_workspace(tmp_path), ["cmake --version", "cmake --build build"]
        )
    rows = failure.value.results
    assert rows

    assert probed == [str(broken.resolve())]
    for row in rows:
        assert row["returncode"] == 7
        assert row["executed_argv"][0] == str(broken.resolve())
        receipt = row["toolchain_authority_receipt"]
        assert receipt["resolved_path"] == str(broken.resolve())
        assert receipt["probe"]["exit_code"] == 7
        assert receipt["choice_reason"] == "rewritten_probe_failed"
        assert "lane_path_probe" not in receipt
