"""Deterministic fake-Windows proof for the NF-2026-00452 supervisor spec."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import aiworkhub.process_launcher as process_launcher
import aiworkhub.process_launcher_launch_isolated as module
import aiworkhub.repository_state as repository_state
import aiworkhub.windows_appcontainer as windows_appcontainer
import aiworkhub.worker_supervisor as worker_supervisor
import aiworkhub.worker_workspace as worker_workspace

CANONICAL_REPO_ID = "repo_57de971f505d4a50a7729a99c32615de"


def test_fake_windows_spec_carries_backend_and_repo_id() -> None:
    spec = module._appcontainer_supervisor_identity(
        repo_id=CANONICAL_REPO_ID,
        worker_kind="glm53",
        platform="win32",
    )
    assert spec == {
        "backend": "windows_appcontainer",
        "repo_id": CANONICAL_REPO_ID,
        "worker_kind": "glm53",
    }


def test_fake_windows_spec_normalizes_native_worker_kind() -> None:
    spec = module._appcontainer_supervisor_identity(
        repo_id=CANONICAL_REPO_ID,
        worker_kind="GLM-53 Native",
        platform="win32",
    )
    assert spec["worker_kind"] == "glm53_native"


def test_non_windows_identity_omits_appcontainer_backend() -> None:
    spec = module._appcontainer_supervisor_identity(
        repo_id=CANONICAL_REPO_ID,
        worker_kind="glm53",
        platform="linux",
    )
    assert spec == {
        "repo_id": CANONICAL_REPO_ID,
        "worker_kind": "glm53",
    }


def test_missing_repo_id_fails_before_spawn() -> None:
    with pytest.raises(ValueError, match="repo_id"):
        module._appcontainer_supervisor_identity(
            repo_id="",
            worker_kind="glm53",
            platform="win32",
        )


class _FakeManager:
    """Minimal ProcessManager stand-in covering only the seams
    launch_isolated reaches before it would hand a process to the OS."""

    def __init__(self, tmp_path: Path, *, repo: str) -> None:
        self.repo = repo
        self.process_dir = tmp_path / "process"
        self._lock = threading.Lock()
        self._live: dict[str, object] = {}

    def _preflight_card(self, task_id, runner, topic, adapter_id, **_kwargs):
        return {}

    def _with_dependency_inputs(self, card):
        return card

    def _resolve_provider_env(self, adapter_id, model):
        return {}, model

    def _launch_reservation(self, _payload):
        return contextlib.nullcontext()

    def _build_adapter(self, **_kwargs):
        return SimpleNamespace(launchable=True, argv=["fake-worker"], reason="")

    def _terminal_authority_grant_path(self, request_id):
        return self.process_dir / f"{request_id}.authority.json"

    def _terminal_authority_key(self):
        return "fake-authority-key"

    def _popen(self, *_args, **_kwargs):
        return SimpleNamespace(pid=4242)

    def _append_event(self, _event):
        return {"state": "running"}

    def _monitor(self, _live):
        return None

    def _blocked(
        self, task_id, runner, topic, adapter_id, reason, **kwargs
    ):
        return {
            "ok": False,
            "task_id": task_id,
            "runner": runner,
            "topic": topic,
            "adapter_id": adapter_id,
            "reason": reason,
            "state": kwargs.get("state", "blocked"),
        }


def _patch_launch_seams(monkeypatch, tmp_path, *, sandbox_backend, canonical_repo_id):
    def _set(name, value):
        monkeypatch.setattr(process_launcher, name, value, raising=True)

    _set("launch_gates_open", lambda: True)
    _set("_validate_adapter_identity", lambda runner, adapter_id: None)
    _set("_enforce_quality_review_launch_binding", lambda topic, binding: None)
    _set("_validation_only_replay_authorization", lambda card, task_id: None)
    _set(
        "validate_workforce_identity",
        lambda runner, adapter_id, model, risk_tier=None, repo=None: model,
    )
    _set("_memory_launch_admission", lambda: {"admit": True})
    _set("_external_readonly_dirs", lambda card, adapter_id: [])
    _set("_task_authority_repo", lambda repo, card: "authority-repo")
    _set("_launch_project_context", lambda repo, card, binding: None)
    _set("_sandbox_backend_for_adapter", lambda adapter_id: sandbox_backend)
    _set("_VSCODE_LM_IN_PROCESS_ADAPTERS", frozenset())
    _set("_touch_0600", lambda path: None)
    _set("chmod_path", lambda path, mode: None)
    _set("_worker_mcp_source_graph_targets", lambda context_result: [])
    _set("_worker_mcp_session_topic", lambda context_result, topic: "session-topic")
    _set(
        "_provision_worker_mcp_runtime_for_authority",
        lambda workspace, **_kwargs: SimpleNamespace(
            server_name="worker-mcp",
            tool_names=[],
            audit_ledger_path=tmp_path / "ledger.json",
            audit_hmac_key_path=tmp_path / "hmac.key",
            claude_mcp_config_path=tmp_path / "claude.json",
            copilot_mcp_config_path=tmp_path / "copilot.json",
            codex_config_toml_path=tmp_path / "codex.toml",
            kilo_config_path=tmp_path / "kilo.json",
            package_import_root=tmp_path,
        ),
    )
    monkeypatch.setattr(
        process_launcher.worker_ai_tools_mcp,
        "appcontainer_mcp_bridge",
        lambda runtime, *, request_id, home, stderr_path: (
            tmp_path / "bridge.json",
            {"pipe": f"pipe-{request_id}", "stderr_path": str(stderr_path)},
        ),
        raising=True,
    )
    _set("_launch_source_graph_request", lambda card, binding: None)
    _set("build_worker_prompt", lambda **_kwargs: "prompt-text")
    _set(
        "create_workspace",
        lambda repo, request_id, card, adapter_id: SimpleNamespace(
            repo=repo,
            path=tmp_path / "workspace",
            home=tmp_path / "workspace-home",
            allowed_writes=[],
            parent_baseline=None,
            as_metadata=lambda: {},
        ),
    )
    _set("build_residual_contract_manifest", lambda workspace, card: [])
    _set(
        "_materialize_worker_rework_overlay",
        lambda workspace, *, task_id, card: (None, None),
    )
    _set(
        "_materialize_crash_retry_packet",
        lambda process_dir, workspace, *, task_id, card, rework_overlay_packet: (
            None,
            None,
        ),
    )
    _set("worker_launch_env", lambda adapter_id, **_kwargs: {})
    _set(
        "sandbox_argv",
        lambda workspace, adapter_id, argv, *, backend, package_import_root: list(
            argv
        ),
    )
    _set("_worker_launch_cwd", lambda path: str(path))
    _set("_path_manifest", lambda repo, paths: {})
    monkeypatch.setattr(
        process_launcher.worker_ai_tools_mcp,
        "resolve_host_package_import_root",
        lambda: tmp_path,
        raising=True,
    )
    monkeypatch.setattr(
        process_launcher.core, "_lifecycle_state", lambda card: "queued", raising=True
    )
    monkeypatch.setattr(
        process_launcher.task_engine,
        "claim_start_exact",
        lambda *_args, **_kwargs: {"ok": True},
        raising=True,
    )
    _set(
        "_committed_claim_card",
        lambda claim, *, request_id, task_id, runner, topic: {"claim_epoch": 1},
    )
    _set("_project_context_delivery", lambda context_result, prompt_hash: {})
    # Refusal bookkeeping.  A launch that fails closed still has to record a
    # recoverable blocked episode and release its request resources, so these
    # collaborators are reached by the fail-closed tests below.
    monkeypatch.setattr(
        process_launcher.task_engine,
        "mark_launch_failed",
        lambda *_args, **_kwargs: {"ok": True},
        raising=True,
    )
    monkeypatch.setattr(
        process_launcher.task_engine,
        "record_launch_blocker",
        lambda *_args, **_kwargs: {"ok": True},
        raising=True,
    )
    _set("_release_launch_request_resources", lambda **_kwargs: [])
    _set("unlink_if_regular", lambda _path: None)
    _set("_write_terminal_authority_grant", lambda *_args, **_kwargs: None)
    _set("_worker_supervisor_script", lambda: tmp_path / "supervisor_script.py")
    _set("_pid_start_ticks", lambda pid: 999)
    monkeypatch.setattr(
        process_launcher.project_context.repository_state,
        "inspect_repository",
        lambda repo: SimpleNamespace(
            manifest=SimpleNamespace(repo_id=canonical_repo_id)
        ),
        raising=True,
    )


def _supervisor_specs(spec_writes):
    return [
        payload
        for path, payload in spec_writes
        if str(path).endswith("supervisor-spec.json")
    ]


def _launch_and_capture(
    monkeypatch,
    tmp_path,
    *,
    adapter_id,
    sandbox_backend,
    canonical_repo_id=CANONICAL_REPO_ID,
    inspect_repository=None,
):
    """Run the real ``launch_isolated`` and return ``(result, writes)``.

    Refusals are a first-class outcome here, so this never asserts success;
    the callers that need a launched spec go through ``_run_launch_isolated``.
    """
    _patch_launch_seams(
        monkeypatch,
        tmp_path,
        sandbox_backend=sandbox_backend,
        canonical_repo_id=canonical_repo_id,
    )
    if inspect_repository is not None:
        monkeypatch.setattr(
            process_launcher.project_context.repository_state,
            "inspect_repository",
            inspect_repository,
            raising=True,
        )
    spec_writes: list[tuple[object, dict]] = []

    def _record_write(path, payload):
        spec_writes.append((path, payload))

    monkeypatch.setattr(process_launcher, "write_json_0600", _record_write, raising=True)

    manager = _FakeManager(tmp_path, repo="repo_self_unused")
    result = module.launch_isolated(
        manager,
        task_id="task-1",
        runner="runner-1",
        topic="topic-1",
        adapter_id=adapter_id,
        model=None,
        owner_prompt="do the thing",
        timeout_seconds=60,
    )
    return result, spec_writes


def _run_launch_isolated(
    monkeypatch,
    tmp_path,
    *,
    adapter_id,
    sandbox_backend,
    canonical_repo_id=CANONICAL_REPO_ID,
):
    result, spec_writes = _launch_and_capture(
        monkeypatch,
        tmp_path,
        adapter_id=adapter_id,
        sandbox_backend=sandbox_backend,
        canonical_repo_id=canonical_repo_id,
    )
    assert result["ok"] is True, result
    return _supervisor_specs(spec_writes)[0]


def test_launch_isolated_windows_appcontainer_spec_carries_identity(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    spec = _run_launch_isolated(
        monkeypatch,
        tmp_path,
        adapter_id="GLM-53 Native",
        sandbox_backend="windows_appcontainer",
    )
    assert spec["execution_backend"] == "windows_appcontainer"
    assert spec["repo_id"] == CANONICAL_REPO_ID
    assert spec["worker_kind"] == "glm53_native"


def test_launch_isolated_non_appcontainer_spec_omits_identity_keys(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    spec = _run_launch_isolated(
        monkeypatch,
        tmp_path,
        adapter_id="glm53",
        sandbox_backend="landlock",
    )
    assert "execution_backend" not in spec
    assert "repo_id" not in spec
    assert "worker_kind" not in spec


def test_launch_isolated_editor_hosted_spec_omits_identity_keys_on_windows(
    monkeypatch, tmp_path
) -> None:
    """An editor-hosted route on Windows never acquires AppContainer keys."""
    monkeypatch.setattr(sys, "platform", "win32")
    spec = _run_launch_isolated(
        monkeypatch,
        tmp_path,
        adapter_id="glm_vscode_lm",
        sandbox_backend="vscode_lm_in_process",
    )
    assert "execution_backend" not in spec
    assert "repo_id" not in spec
    assert "worker_kind" not in spec


def test_launch_isolated_bridges_the_contained_claude_worker_mcp(
    monkeypatch, tmp_path
) -> None:
    """NF-2026-00034: the supervisor gets the host-side server to run."""
    monkeypatch.setattr(sys, "platform", "win32")
    spec = _run_launch_isolated(
        monkeypatch, tmp_path, adapter_id="claude_cli", sandbox_backend="windows_appcontainer"
    )
    bridge = spec["worker_mcp_bridge"]
    assert bridge["pipe"].startswith("pipe-")
    assert bridge["stderr_path"].endswith(".worker-mcp.stderr.log")


@pytest.mark.parametrize(
    ("platform", "adapter_id", "sandbox_backend"),
    [
        ("linux", "claude_cli", "landlock"),
        ("linux", "claude_cli", "bubblewrap"),
        ("win32", "GLM-53 Native", "windows_appcontainer"),
    ],
)
def test_launch_isolated_bridges_nothing_else(
    monkeypatch, tmp_path, platform, adapter_id, sandbox_backend
) -> None:
    """The Linux routes, and every other adapter, keep their spec exactly."""
    monkeypatch.setattr(sys, "platform", platform)
    spec = _run_launch_isolated(
        monkeypatch, tmp_path, adapter_id=adapter_id, sandbox_backend=sandbox_backend
    )
    assert "worker_mcp_bridge" not in spec


# ── Fail-closed launcher identity ──────────────────────────────────────────
# Every refusal below has to happen at the ``supervisor_spec`` phase, before
# ``_popen`` is reached.  Asserting that no supervisor spec was written is how
# these tests prove "before provider launch" rather than merely "eventually
# failed": the spec write is the last thing that happens before the spawn.


def _raise_repository_state_error(_repo):
    raise process_launcher.project_context.repository_state.RepositoryStateError(
        "repository_manifest_unreadable"
    )


def test_launch_isolated_refuses_when_canonical_repo_identity_is_unavailable(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    result, spec_writes = _launch_and_capture(
        monkeypatch,
        tmp_path,
        adapter_id="GLM-53 Native",
        sandbox_backend="windows_appcontainer",
        inspect_repository=_raise_repository_state_error,
    )

    assert result["ok"] is False
    assert result["reason"].startswith(
        "windows_appcontainer_repo_identity_unavailable:"
    )
    assert _supervisor_specs(spec_writes) == []


def test_launch_isolated_refuses_a_blank_canonical_repo_id(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    result, spec_writes = _launch_and_capture(
        monkeypatch,
        tmp_path,
        adapter_id="GLM-53 Native",
        sandbox_backend="windows_appcontainer",
        canonical_repo_id="",
    )

    assert result["ok"] is False
    assert result["reason"].startswith("windows_appcontainer_identity_invalid:")
    assert _supervisor_specs(spec_writes) == []


def test_launch_isolated_refuses_the_appcontainer_backend_off_windows(
    monkeypatch, tmp_path
) -> None:
    """The backend token must never be written where it cannot be applied.

    ``_appcontainer_supervisor_identity`` shapes a ``backend`` key only on
    ``win32``.  Defaulting the token in when it is absent would announce a
    confinement the host cannot build, so the launch is refused instead.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    result, spec_writes = _launch_and_capture(
        monkeypatch,
        tmp_path,
        adapter_id="GLM-53 Native",
        sandbox_backend="windows_appcontainer",
    )

    assert result["ok"] is False
    assert result["reason"].startswith(
        "windows_appcontainer_backend_platform_mismatch:"
    )
    assert _supervisor_specs(spec_writes) == []


# ── Supervisor backend dispatch ────────────────────────────────────────────


class _FakeAppContainerLaunch:
    """Records every lifecycle call one fake AppContainer launch receives."""

    def __init__(self, *, exit_code: int = 0, running_polls: int = 0) -> None:
        self.pid = 4242
        self.command_line = "fake-worker"
        self.exit_code = exit_code
        self.running_polls = running_polls
        self.terminated = False
        self.close_count = 0

    def wait(self, _timeout_ms, **_kwargs):
        if self.running_polls > 0 and not self.terminated:
            self.running_polls -= 1
            return windows_appcontainer.AppContainerLifecycleResult(
                state=windows_appcontainer.AppContainerLifecycleState.RUNNING
            )
        return windows_appcontainer.AppContainerLifecycleResult(
            state=windows_appcontainer.AppContainerLifecycleState.EXITED,
            exit_code=self.exit_code,
            terminated=self.terminated,
        )

    def terminate(self, exit_code):
        self.terminated = True
        self.exit_code = exit_code
        return windows_appcontainer.AppContainerLifecycleResult(
            state=windows_appcontainer.AppContainerLifecycleState.EXITED,
            exit_code=exit_code,
            terminated=True,
        )

    def close(self):
        self.close_count += 1


class _FakeCapture:
    """Stand-in for _BoundedTailWriter: drains the pipe, writes no file."""

    def __init__(self, _path, _cap) -> None:
        self.received_bytes = 0
        self.dropped_bytes = 0
        self.error = ""

    def drain(self, stream) -> None:
        try:
            while stream.read(65536):
                pass
        finally:
            stream.close()


def _supervisor_spec(tmp_path, **extra):
    process_dir = tmp_path / "supervisor"
    process_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "argv": ["fake-worker", "--run"],
        "cwd": str(tmp_path),
        "timeout_seconds": 30,
        "status_path": str(process_dir / "status.json"),
        "cancel_path": str(process_dir / "cancel.json"),
        "stdout_path": str(process_dir / "stdout.log"),
        "stderr_path": str(process_dir / "stderr.log"),
        "adapter_id": "glm53_native",
    }
    spec.update(extra)
    return spec


def _patch_supervisor_seams(monkeypatch):
    """Neutralise every seam except the one under test: backend dispatch.

    ``subprocess.Popen`` is replaced by a detonator.  Any AppContainer spec
    that reaches the plain-subprocess branch fails the test by construction
    rather than by an assertion someone has to remember to write.
    """
    statuses: list[dict] = []
    popen_calls: list[tuple] = []

    def _explode(*args, **kwargs):
        popen_calls.append((args, kwargs))
        raise AssertionError(
            "windows_appcontainer spec reached plain subprocess.Popen"
        )

    monkeypatch.setattr(
        worker_supervisor, "_write_json_0600", lambda _path, payload: statuses.append(payload)
    )
    monkeypatch.setattr(worker_supervisor, "_BoundedTailWriter", _FakeCapture)
    monkeypatch.setattr(worker_supervisor.signal, "signal", lambda *_a, **_k: None)
    monkeypatch.setattr(worker_supervisor.subprocess, "Popen", _explode)
    return statuses, popen_calls


def test_supervisor_appcontainer_spec_launches_through_the_broker(
    monkeypatch, tmp_path
) -> None:
    launches: list[windows_appcontainer.AppContainerRequest] = []
    launch = _FakeAppContainerLaunch(exit_code=0)

    def _fake_launch(request):
        launches.append(request)
        return launch

    statuses, popen_calls = _patch_supervisor_seams(monkeypatch)
    monkeypatch.setattr(
        worker_supervisor.windows_appcontainer,
        "launch_appcontainer",
        _fake_launch,
    )

    code = worker_supervisor.supervise(
        _supervisor_spec(
            tmp_path,
            execution_backend="windows_appcontainer",
            repo_id=CANONICAL_REPO_ID,
            worker_kind="glm53_native",
        )
    )

    assert code == 0
    assert popen_calls == []
    assert len(launches) == 1
    assert launches[0].repo_id == CANONICAL_REPO_ID
    assert launches[0].worker_kind == "glm53_native"
    assert list(launches[0].argv) == ["fake-worker", "--run"]
    assert statuses[-1]["state"] == "exited"
    assert statuses[-1]["exit_code"] == 0
    # The Job-owned handles are released exactly once on the way out.
    assert launch.close_count == 1


def _npm_shim(tmp_path, name, package):
    """A copy of the real npm cmd-shim layout: shim + the package it runs."""
    npm = tmp_path / "npm"
    package_dir = npm / "node_modules" / Path(*package.split("\\"))
    (package_dir / "bin").mkdir(parents=True)
    shim = npm / f"{name}.cmd"
    shim.write_text(
        "@ECHO off\nGOTO start\n:find_dp0\nSET dp0=%~dp0\nEXIT /b\n:start\n"
        "SETLOCAL\nCALL :find_dp0\n"
        f'"%dp0%\\node_modules\\{package}\\bin\\{name}.exe"   %*\n',
        encoding="utf-8",
    )
    return shim, package_dir


@pytest.mark.parametrize(
    ("name", "package"),
    [("claude", "@anthropic-ai\\claude-code"), ("opencode", "opencode-ai")],
)
def test_supervisor_worker_launch_gets_grants_and_only_internet_client(
    monkeypatch, tmp_path, name, package
) -> None:
    """NF-2026-00025 / NF-2026-00033, the worker half of the split."""
    shim, package_dir = _npm_shim(tmp_path, name, package)
    worktree, home, temp = (tmp_path / n for n in ("worktree", "home", "tmp"))
    for directory in (worktree, home, temp):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for key in ("TMP", "TEMP", "TMPDIR"):
        monkeypatch.setenv(key, str(temp))
    launches: list[windows_appcontainer.AppContainerRequest] = []

    def _fake_launch(request):
        launches.append(request)
        return _FakeAppContainerLaunch(exit_code=0)

    _patch_supervisor_seams(monkeypatch)
    monkeypatch.setattr(
        worker_supervisor.windows_appcontainer, "launch_appcontainer", _fake_launch
    )
    spec = _supervisor_spec(
        tmp_path,
        execution_backend="windows_appcontainer",
        repo_id=CANONICAL_REPO_ID,
        worker_kind=f"{name}_cli",
        argv=[str(shim), "--version"],
        cwd=str(worktree),
    )

    assert worker_supervisor.supervise(spec) == 0

    request = launches[0]
    # cmd.exe cannot run a batch file inside the container, so the shim's own
    # native target runs, with the worker's arguments untouched.
    assert list(request.argv) == [str(package_dir / "bin" / f"{name}.exe"), "--version"]
    # Outbound internet only: never inbound listening, never the LAN.
    assert tuple(request.capability_sids) == ("internetClient",)
    grant = windows_appcontainer.ContainerGrant
    assert list(request.filesystem_grants) == [
        # The shim and the one package it runs -- not the whole npm dir.
        grant(str(shim), "read_execute", persistent=True),
        grant(str(package_dir), "read_execute", persistent=True),
        grant(str(worktree), "modify"),
        grant(str(home), "modify"),
        grant(str(temp), "modify"),
    ]


def test_a_non_npm_shim_is_run_as_is_and_granted_only_itself(tmp_path) -> None:
    # Codex's own shim resolves its exe at run time: not an npm shim.
    shim = tmp_path / "codex.cmd"
    shim.write_text('@echo off\n"%CODEX_BIN%" %*\n', encoding="utf-8")
    assert worker_supervisor._resolve_npm_shim(str(shim)) is None
    assert worker_supervisor._native_worker_argv([str(shim), "-x"]) == [str(shim), "-x"]
    assert worker_supervisor._provider_install_grants(str(shim)) == [
        windows_appcontainer.ContainerGrant(str(shim), "read_execute", persistent=True)
    ]


ESCAPING_SHIM_TARGETS = [
    # Literal parent segments.
    "..\\..\\evil.exe",
    "@scope\\..\\x.exe",
    # '/' inside the PACKAGE segment: "a/../../../../.." is no ".." segment
    # when split on '\\' alone, yet Win32 walks it out of node_modules.
    "a/../../../../..\\x",
    # '/' inside the tail.
    "pkg\\bin/../../../../Windows/System32/cmd.exe",
    # Mixed separators.
    "pkg/..\\..\\x.exe",
    "@scope/pkg\\../../../x.exe",
    # ':' -- a drive-relative path or an alternate data stream.
    "C:x.exe",
    "pkg\\x.exe:stream",
    # Trailing dots/spaces, which Win32 strips.
    "pkg\\.. \\..\\x.exe",
    "pkg\\bin.\\x.exe",
    # A bare package with no file under it.
    "pkg",
]


@pytest.mark.parametrize("relative", ESCAPING_SHIM_TARGETS)
def test_an_escaping_npm_shim_is_refused_not_followed(tmp_path, relative) -> None:
    shim = tmp_path / "npm" / "tool.cmd"
    shim.parent.mkdir()
    shim.write_text(f'@echo off\n"%dp0%\\node_modules\\{relative}" %*\n', encoding="utf-8")
    for probe in (
        lambda: worker_supervisor._resolve_npm_shim(str(shim)),
        lambda: worker_supervisor._native_worker_argv([str(shim), "-x"]),
        lambda: worker_supervisor._provider_install_grants(str(shim)),
    ):
        with pytest.raises(ValueError, match="npm_shim_target_outside_node_modules"):
            probe()


def test_a_forward_slash_that_stays_inside_the_package_is_accepted(tmp_path) -> None:
    shim, package_dir = _npm_shim(tmp_path, "tool", "@scope\\tool")
    shim.write_text(
        '@echo off\n"%dp0%\\node_modules\\@scope/tool\\bin/tool.exe" %*\n',
        encoding="utf-8",
    )
    target, package = worker_supervisor._resolve_npm_shim(str(shim))
    assert package == package_dir
    assert target == package_dir / "bin" / "tool.exe"


def test_supervisor_refuses_an_escaping_shim_before_any_grant_or_launch(
    monkeypatch, tmp_path
) -> None:
    shim = tmp_path / "npm" / "claude.cmd"
    shim.parent.mkdir()
    shim.write_text(
        '@echo off\n"%dp0%\\node_modules\\a/../../../../..\\x" %*\n', encoding="utf-8"
    )
    statuses, popen_calls = _patch_supervisor_seams(monkeypatch)
    monkeypatch.setattr(
        worker_supervisor.windows_appcontainer,
        "launch_appcontainer",
        lambda _request: pytest.fail("an escaping shim must never be launched"),
    )

    code = worker_supervisor.supervise(
        _supervisor_spec(
            tmp_path,
            execution_backend="windows_appcontainer",
            repo_id=CANONICAL_REPO_ID,
            worker_kind="claude_cli",
            argv=[str(shim), "--version"],
        )
    )

    assert code == 126
    assert popen_calls == []
    assert statuses[-1]["state"] == "spawn_failed"
    assert "npm_shim_target_outside_node_modules" in statuses[-1]["error"]


def test_supervisor_native_executable_grants_only_the_file(tmp_path) -> None:
    exe = tmp_path / "bin" / "kilo.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    assert worker_supervisor._provider_install_grants(str(exe)) == [
        windows_appcontainer.ContainerGrant(str(exe), "read_execute", persistent=True)
    ]


def test_launch_isolated_gives_the_appcontainer_worker_its_isolated_home(
    monkeypatch, tmp_path
) -> None:
    """HOME=None would seed the user's REAL profile, which the supervisor
    would then grant the container modify access to.  With HOME isolated, the
    supervisor also needs the real LOCALAPPDATA handed over (CreateProcessW
    error 203 otherwise)."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\u\AppData\Local")
    _patch_launch_seams(
        monkeypatch,
        tmp_path,
        sandbox_backend="windows_appcontainer",
        canonical_repo_id=CANONICAL_REPO_ID,
    )
    env_kwargs: list[dict] = []
    monkeypatch.setattr(
        process_launcher,
        "worker_launch_env",
        lambda adapter_id, **kwargs: env_kwargs.append(kwargs) or {},
    )
    monkeypatch.setattr(process_launcher, "write_json_0600", lambda *_a: None)
    spawned: list[dict] = []

    class _Manager(_FakeManager):
        def _popen(self, *_args, **kwargs):
            spawned.append(kwargs)
            return super()._popen()

    result = module.launch_isolated(
        _Manager(tmp_path, repo="repo_self_unused"),
        task_id="task-1",
        runner="runner-1",
        topic="topic-1",
        adapter_id="claude_cli",
        model=None,
        owner_prompt="do the thing",
        timeout_seconds=60,
    )

    assert result["ok"] is True, result
    assert env_kwargs[0]["home"] == tmp_path / "workspace-home"
    assert spawned[0]["env"]["LOCALAPPDATA"] == r"C:\Users\u\AppData\Local"


def test_supervisor_appcontainer_setup_failure_never_falls_back_to_popen(
    monkeypatch, tmp_path
) -> None:
    def _fail(_request):
        raise OSError("appcontainer_create_profile_failed:5")

    statuses, popen_calls = _patch_supervisor_seams(monkeypatch)
    monkeypatch.setattr(
        worker_supervisor.windows_appcontainer, "launch_appcontainer", _fail
    )

    code = worker_supervisor.supervise(
        _supervisor_spec(
            tmp_path,
            execution_backend="windows_appcontainer",
            repo_id=CANONICAL_REPO_ID,
            worker_kind="glm53_native",
        )
    )

    assert code == 126
    assert popen_calls == []
    assert statuses[-1]["state"] == "spawn_failed"
    assert statuses[-1]["spawn_phase"] == "child_spawn"
    assert "appcontainer_create_profile_failed" in statuses[-1]["error"]


class _RecordingPipe:
    instances: list["_RecordingPipe"] = []

    def __init__(self, name, sid) -> None:
        self.name, self.sid, self.closed = name, sid, 0
        _RecordingPipe.instances.append(self)

    def close(self) -> bool:
        self.closed += 1
        return True


def _patch_bridge_seams(monkeypatch):
    _RecordingPipe.instances = []
    bridges: list[dict] = []

    class _RecordingBridge:
        def __init__(self, pipe, job, spec, cwd) -> None:
            self.record = {"pipe": pipe, "job": job, "spec": spec, "cwd": cwd, "events": []}
            bridges.append(self.record)

        def start(self) -> None:
            self.record["events"].append("start")

        def close(self) -> None:
            self.record["events"].append("close")

    monkeypatch.setattr(worker_supervisor.windows_appcontainer, "WorkerPipe", _RecordingPipe)
    monkeypatch.setattr(
        worker_supervisor.windows_appcontainer,
        "container_sid",
        lambda repo_id, worker_kind: f"S-1-15-2-{len(repo_id)}-{len(worker_kind)}",
    )
    monkeypatch.setattr(worker_supervisor, "_WorkerMcpBridge", _RecordingBridge)
    return bridges


_BRIDGE_SPEC = {
    "pipe": "\\\\.\\pipe\\aiworkhub-worker-r-" + "a" * 32,
    "command": "python.exe",
    "args": ["-m", "aiworkhub.worker_ai_tools_mcp"],
    "env": {},
    "stderr_path": "unused",
    "withheld_directories": ["C:\\home\\task_mcp_worker_runtime"],
}


def test_supervisor_bridges_the_worker_mcp_and_ends_it_with_the_worker(
    monkeypatch, tmp_path
) -> None:
    bridges = _patch_bridge_seams(monkeypatch)
    launches: list[windows_appcontainer.AppContainerRequest] = []
    launch = _FakeAppContainerLaunch(exit_code=0)
    launch.job = "the-launch-job"

    def _fake_launch(request):
        # The pipe already exists when the worker starts.
        assert len(_RecordingPipe.instances) == 1
        launches.append(request)
        return launch

    _patch_supervisor_seams(monkeypatch)
    monkeypatch.setattr(worker_supervisor.windows_appcontainer, "launch_appcontainer", _fake_launch)
    code = worker_supervisor.supervise(
        _supervisor_spec(
            tmp_path,
            execution_backend="windows_appcontainer",
            repo_id=CANONICAL_REPO_ID,
            worker_kind="claude_cli",
            worker_mcp_bridge=_BRIDGE_SPEC,
        )
    )

    assert code == 0
    pipe = _RecordingPipe.instances[0]
    assert pipe.name == _BRIDGE_SPEC["pipe"]
    assert pipe.sid == f"S-1-15-2-{len(CANONICAL_REPO_ID)}-{len('claude_cli')}"
    assert tuple(launches[0].withheld_directories) == ("C:\\home\\task_mcp_worker_runtime",)
    (bridge,) = bridges
    assert bridge["pipe"] is pipe and bridge["job"] == "the-launch-job"
    assert bridge["events"] == ["start", "close"]
    assert launch.close_count == 1


def test_supervisor_removes_the_pipe_when_the_launch_fails(monkeypatch, tmp_path) -> None:
    bridges = _patch_bridge_seams(monkeypatch)

    def _fail(_request):
        raise OSError("appcontainer_create_profile_failed:5")

    statuses, _ = _patch_supervisor_seams(monkeypatch)
    monkeypatch.setattr(worker_supervisor.windows_appcontainer, "launch_appcontainer", _fail)
    code = worker_supervisor.supervise(
        _supervisor_spec(
            tmp_path,
            execution_backend="windows_appcontainer",
            repo_id=CANONICAL_REPO_ID,
            worker_kind="claude_cli",
            worker_mcp_bridge=_BRIDGE_SPEC,
        )
    )
    assert code == 126
    assert statuses[-1]["state"] == "spawn_failed"
    assert [pipe.closed for pipe in _RecordingPipe.instances] == [1]
    assert bridges == []


def test_supervisor_without_a_bridge_spec_opens_no_pipe(monkeypatch, tmp_path) -> None:
    bridges = _patch_bridge_seams(monkeypatch)
    _patch_supervisor_seams(monkeypatch)
    requests: list = []
    monkeypatch.setattr(
        worker_supervisor.windows_appcontainer,
        "launch_appcontainer",
        lambda request: requests.append(request) or _FakeAppContainerLaunch(),
    )
    assert worker_supervisor.supervise(
        _supervisor_spec(
            tmp_path,
            execution_backend="windows_appcontainer",
            repo_id=CANONICAL_REPO_ID,
            worker_kind="glm53_native",
        )
    ) == 0
    assert _RecordingPipe.instances == [] and bridges == []
    assert tuple(requests[0].withheld_directories) == ()


@pytest.mark.parametrize(
    ("missing_key", "expected"),
    [
        ("repo_id", "appcontainer_spec_missing_repo_id"),
        ("worker_kind", "appcontainer_spec_missing_worker_kind"),
    ],
)
def test_supervisor_refuses_an_appcontainer_spec_without_identity(
    monkeypatch, tmp_path, missing_key, expected
) -> None:
    """A profile that cannot be named is a refusal, never a guess."""
    identity = {
        "repo_id": CANONICAL_REPO_ID,
        "worker_kind": "glm53_native",
        "adapter_id": "",
    }
    identity[missing_key] = ""

    statuses, popen_calls = _patch_supervisor_seams(monkeypatch)
    monkeypatch.setattr(
        worker_supervisor.windows_appcontainer,
        "launch_appcontainer",
        lambda _request: pytest.fail("launch must not be attempted"),
    )

    code = worker_supervisor.supervise(
        _supervisor_spec(
            tmp_path, execution_backend="windows_appcontainer", **identity
        )
    )

    assert code == 126
    assert popen_calls == []
    assert statuses[-1]["state"] == "spawn_failed"
    assert expected in statuses[-1]["error"]


def test_supervisor_refuses_a_near_miss_backend_instead_of_spawning_plainly(
    monkeypatch, tmp_path
) -> None:
    """``appcontainer`` is not ``windows_appcontainer``.

    A spelling the dispatch does not recognise used to fall through to the
    plain-subprocess branch, producing a worker that reads as confined and is
    held by a Job Object alone.
    """
    statuses, popen_calls = _patch_supervisor_seams(monkeypatch)

    code = worker_supervisor.supervise(
        _supervisor_spec(
            tmp_path,
            execution_backend="appcontainer",
            repo_id=CANONICAL_REPO_ID,
            worker_kind="glm53_native",
        )
    )

    assert code == 126
    assert popen_calls == []
    assert statuses[-1]["state"] == "spawn_failed"
    assert statuses[-1]["spawn_phase"] == "execution_backend_unsupported"
    assert "unsupported_execution_backend:appcontainer" in statuses[-1]["error"]


def test_supervisor_cancellation_terminates_the_appcontainer_tree(
    monkeypatch, tmp_path
) -> None:
    launch = _FakeAppContainerLaunch(exit_code=0, running_polls=1)
    statuses, popen_calls = _patch_supervisor_seams(monkeypatch)
    monkeypatch.setattr(
        worker_supervisor.windows_appcontainer,
        "launch_appcontainer",
        lambda _request: launch,
    )
    spec = _supervisor_spec(
        tmp_path,
        execution_backend="windows_appcontainer",
        repo_id=CANONICAL_REPO_ID,
        worker_kind="glm53_native",
    )
    Path(spec["cancel_path"]).write_text("{}\n", encoding="utf-8")

    code = worker_supervisor.supervise(spec)

    assert code == 125
    assert popen_calls == []
    assert launch.terminated is True
    assert launch.close_count == 1
    assert statuses[-1]["state"] == "cancelled"
    # The cancel sentinel is consumed so a later request cannot inherit it.
    assert not Path(spec["cancel_path"]).exists()


# ---------------------------------------------------------------------------
# windows_appcontainer as a first-class backend in all three consumer sites
#
# select_sandbox_backend() returns WINDOWS_APPCONTAINER_BACKEND on a capable
# Windows host, but provision_worker_mcp_runtime, sandbox_argv and
# run_validations each used to refuse that exact token -- so every native-CLI
# card died as launch_failed:unsupported_sandbox_backend:windows_appcontainer
# before the model was ever called.
# ---------------------------------------------------------------------------


APPCONTAINER = worker_workspace.WINDOWS_APPCONTAINER_BACKEND


@pytest.mark.parametrize(
    "adapter_id",
    [
        "claude_cli",
        "codex_cli",
        "glm_vscode_lm",
        "deepseek_vscode_lm",
        "opencode_cli",
        "grok_kilo_cli",
        "GLM-53 Native",
        "Codex CLI",
    ],
)
def test_validation_worker_kind_matches_the_supervisor_container_identity(
    adapter_id: str,
) -> None:
    """A validation command has to land in the worker's own container SID.

    ``derive_container_identity`` digests the *raw* ``worker_kind`` it is
    handed, so the validation lane must normalize an adapter id exactly the way
    the supervisor already does rather than pass one that merely happens to be
    in normal form today.
    """
    assert windows_appcontainer.appcontainer_worker_kind(
        adapter_id
    ) == module._appcontainer_supervisor_identity(
        repo_id=CANONICAL_REPO_ID,
        worker_kind=adapter_id,
        platform="win32",
    )["worker_kind"]


def test_sandbox_argv_returns_the_real_argv_unchanged_under_appcontainer() -> None:
    argv = [r"C:\Python\python.exe", "-m", "pytest", "-q"]
    wrapped = worker_workspace.sandbox_argv(
        SimpleNamespace(), "validation", argv, backend=APPCONTAINER
    )
    # Confinement comes from the container profile and job object, so there is
    # no bwrap-style prefix and no namespace remapping to apply here.
    assert wrapped == argv
    assert wrapped is not argv


def test_sandbox_argv_still_refuses_a_backend_nobody_implements() -> None:
    with pytest.raises(
        worker_workspace.WorkspaceError,
        match="unsupported_sandbox_backend:made_up",
    ):
        worker_workspace.sandbox_argv(
            SimpleNamespace(), "validation", ["x"], backend="made_up"
        )


def test_provision_worker_mcp_runtime_accepts_the_appcontainer_backend(
    tmp_path: Path,
) -> None:
    """The launch-time guard must let the token through to the next check."""
    not_a_directory = tmp_path / "authority-repo"
    not_a_directory.write_text("", encoding="utf-8")
    workspace = SimpleNamespace(
        repo=not_a_directory, path=tmp_path, home=tmp_path
    )
    with pytest.raises(worker_workspace.WorkspaceError) as excinfo:
        worker_workspace.provision_worker_mcp_runtime(
            workspace,
            request_id="request",
            task_id="task",
            runner="runner",
            topic="topic",
            backend=APPCONTAINER,
            source_graph_targets=(),
            session_topic="topic",
        )
    assert "unsupported_sandbox_backend" not in str(excinfo.value)
    assert str(excinfo.value).startswith("authority_repo_not_directory")


class _FakeValidationLaunch:
    """Deterministic stand-in for one job-owned AppContainer child."""

    def __init__(
        self,
        request,
        *,
        stdout: bytes,
        stderr: bytes,
        outcome,
    ) -> None:
        self.request = request
        self._outcome = outcome
        self.terminated = False
        self.closed = False
        self.waited_ms: list[int] = []
        # get_osfhandle is patched to identity in these tests, so the request
        # carries the production side's own inheritable write descriptors.
        os.write(request.stdout_handle, stdout)
        os.write(request.stderr_handle, stderr)

    def wait(self, timeout_ms: int, **_kwargs):
        self.waited_ms.append(timeout_ms)
        return self._outcome

    def terminate(self, exit_code: int = 1):
        self.terminated = True
        self.closed = True
        return self._outcome

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def identity_osfhandle(monkeypatch):
    """Make ``msvcrt.get_osfhandle`` hand back the descriptor itself.

    The production helper passes real Win32 handles; treating the descriptor as
    the handle lets the fake child write through the exact same pipe the
    production side created and then closes, so EOF still arrives naturally.
    """
    msvcrt = pytest.importorskip("msvcrt")
    monkeypatch.setattr(msvcrt, "get_osfhandle", lambda fd: fd)
    return msvcrt


def _install_fake_launch(monkeypatch, *, stdout, stderr, outcome, sink):
    def _fake_launch_appcontainer(request):
        launch = _FakeValidationLaunch(
            request, stdout=stdout, stderr=stderr, outcome=outcome
        )
        sink.append(launch)
        return launch

    monkeypatch.setattr(
        windows_appcontainer, "launch_appcontainer", _fake_launch_appcontainer
    )


def _stub_repo_id(monkeypatch):
    monkeypatch.setattr(
        repository_state,
        "inspect_repository",
        lambda repo, **_kwargs: SimpleNamespace(
            manifest=SimpleNamespace(repo_id=CANONICAL_REPO_ID)
        ),
    )


def test_appcontainer_validation_returns_a_completed_process(
    tmp_path: Path, monkeypatch, identity_osfhandle
) -> None:
    launches: list[_FakeValidationLaunch] = []
    _stub_repo_id(monkeypatch)
    _install_fake_launch(
        monkeypatch,
        stdout=b"7 passed\n",
        stderr=b"",
        outcome=windows_appcontainer.AppContainerLifecycleResult(
            windows_appcontainer.AppContainerLifecycleState.EXITED, exit_code=0
        ),
        sink=launches,
    )

    result = worker_workspace._run_appcontainer_validation(
        ["pytest", "-q"],
        workspace=SimpleNamespace(repo=tmp_path, path=tmp_path, home=tmp_path),
        adapter_id="claude_cli",
        cwd=tmp_path,
        env={"PATH": "x"},
        timeout_seconds=30,
    )

    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 0
    assert result.stdout == "7 passed\n"
    assert result.stderr == ""
    assert result.args == ["pytest", "-q"]
    request = launches[0].request
    assert list(request.argv) == ["pytest", "-q"]
    assert request.repo_id == CANONICAL_REPO_ID
    assert request.worker_kind == windows_appcontainer.appcontainer_worker_kind(
        "claude_cli"
    )
    assert launches[0].closed


def test_appcontainer_validation_gets_grants_but_no_network(
    tmp_path: Path, monkeypatch, identity_osfhandle
) -> None:
    """NF-2026-00033, the validation half of the split: candidate code runs
    offline.  NF-2026-00025: the worktree root read-only, HOME/temp modify."""
    launches: list[_FakeValidationLaunch] = []
    _stub_repo_id(monkeypatch)
    _install_fake_launch(
        monkeypatch,
        stdout=b"",
        stderr=b"",
        outcome=windows_appcontainer.AppContainerLifecycleResult(
            windows_appcontainer.AppContainerLifecycleState.EXITED, exit_code=0
        ),
        sink=launches,
    )
    worktree, home, scratch = (tmp_path / n for n in ("wt", "home", "scratch"))
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMP": str(scratch),
        "TEMP": str(scratch),
        "PATH": "x",
    }

    worker_workspace._run_appcontainer_validation(
        ["pytest", "-q"],
        workspace=SimpleNamespace(repo=tmp_path, path=worktree, home=home),
        adapter_id="claude_cli",
        cwd=worktree / "pkg",
        env=env,
        timeout_seconds=30,
    )

    request = launches[0].request
    assert tuple(request.capability_sids) == ()
    grant = windows_appcontainer.ContainerGrant
    assert list(request.filesystem_grants) == [
        # The root, never the cd subdir a candidate could have made a junction.
        grant(str(worktree), "read_execute"),
        grant(str(home), "modify"),
        grant(str(scratch), "modify"),
    ]
    assert not any(g.persistent for g in request.filesystem_grants)


def test_appcontainer_validation_python_gets_its_interpreter_read_only_and_no_network(
    tmp_path: Path, monkeypatch, identity_osfhandle
) -> None:
    """NF-2026-00034: ``python -m pytest`` needs the interpreter it runs."""
    launches: list[_FakeValidationLaunch] = []
    _stub_repo_id(monkeypatch)
    _install_fake_launch(
        monkeypatch,
        stdout=b"",
        stderr=b"",
        outcome=windows_appcontainer.AppContainerLifecycleResult(
            windows_appcontainer.AppContainerLifecycleState.EXITED, exit_code=0
        ),
        sink=launches,
    )
    base = tmp_path / "Python312"
    base.mkdir()
    venv = tmp_path / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Lib" / "site-packages").mkdir(parents=True)
    python = venv / "Scripts" / "python.exe"
    python.write_bytes(b"MZ")
    (venv / "pyvenv.cfg").write_text(f"home = {base}\n", encoding="utf-8")
    worktree, home, scratch = (tmp_path / n for n in ("wt", "home", "scratch"))
    env = {
        "HOME": str(home), "USERPROFILE": str(home), "TMP": str(scratch),
        "TEMP": str(scratch), "PYTHONPATH": os.pathsep.join([str(worktree), "."]),
    }

    worker_workspace._run_appcontainer_validation(
        [str(python), "-m", "pytest", "-q"],
        workspace=SimpleNamespace(repo=tmp_path, path=worktree, home=home),
        adapter_id="claude_cli",
        cwd=worktree,
        env=env,
        timeout_seconds=30,
    )

    request = launches[0].request
    assert tuple(request.capability_sids) == ()
    grant = windows_appcontainer.ContainerGrant
    assert list(request.filesystem_grants) == [
        grant(str(worktree), "read_execute"),
        grant(str(home), "modify"),
        grant(str(scratch), "modify"),
        grant(str(python), "read_execute", persistent=True),
        grant(str(venv / "pyvenv.cfg"), "read_execute", persistent=True),
        grant(str(venv / "Lib" / "site-packages"), "read_execute", persistent=True),
        grant(str(base), "read_execute", persistent=True),
    ]


def test_appcontainer_validation_timeout_carries_partial_output(
    tmp_path: Path, monkeypatch, identity_osfhandle
) -> None:
    launches: list[_FakeValidationLaunch] = []
    _stub_repo_id(monkeypatch)
    _install_fake_launch(
        monkeypatch,
        stdout=b"collected 2 items\n",
        stderr=b"slow\n",
        outcome=windows_appcontainer.AppContainerLifecycleResult(
            windows_appcontainer.AppContainerLifecycleState.TIMEOUT
        ),
        sink=launches,
    )

    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        worker_workspace._run_appcontainer_validation(
            ["pytest", "-q"],
            workspace=SimpleNamespace(repo=tmp_path, path=tmp_path, home=tmp_path),
            adapter_id="claude_cli",
            cwd=tmp_path,
            env={},
            timeout_seconds=5,
        )

    assert excinfo.value.timeout == 5
    assert excinfo.value.output == "collected 2 items\n"
    assert excinfo.value.stderr == "slow\n"
    assert launches[0].terminated
    assert launches[0].closed


def test_appcontainer_validation_never_lets_appcontainer_error_escape(
    tmp_path: Path, monkeypatch, identity_osfhandle
) -> None:
    _stub_repo_id(monkeypatch)

    def _refuse(request):
        raise windows_appcontainer.AppContainerError(
            windows_appcontainer.AppContainerReason.LAUNCH_FAILED,
            detail="create_process refused",
        )

    monkeypatch.setattr(windows_appcontainer, "launch_appcontainer", _refuse)

    with pytest.raises(OSError) as excinfo:
        worker_workspace._run_appcontainer_validation(
            ["pytest", "-q"],
            workspace=SimpleNamespace(repo=tmp_path, path=tmp_path, home=tmp_path),
            adapter_id="claude_cli",
            cwd=tmp_path,
            env={},
            timeout_seconds=5,
        )

    assert not isinstance(excinfo.value, windows_appcontainer.AppContainerError)
    assert "windows_appcontainer_validation_launch_failed" in str(excinfo.value)
