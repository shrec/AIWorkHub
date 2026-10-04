"""Deterministic fake-Windows proof for the NF-2026-00452 supervisor spec."""

from __future__ import annotations

import contextlib
import json
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

# NF-2026-01039: the same pattern as tests/test_windows_appcontainer.py.  The
# tests carrying this marker need real host Win32 privileges (icacls on a real
# DACL, a real junction) that the AppContainer validation lane does not have
# when it runs this suite inside a container.  On a host -- and on any
# non-Windows CI runner, where the detector is always False -- they run and
# must pass.
requires_host_win32_privileges = pytest.mark.skipif(
    windows_appcontainer.current_process_is_appcontainer(),
    reason="requires host Win32 privileges; not available inside an AppContainer",
)


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


def _sandbox_request_dir(tmp_path, request_id="request-1"):
    """``<repo>\\.aiworkhub\\runtime\\worktrees\\<request>``: production's layout.

    ``request_scoped_grants`` (ce33d1c/71c2fe4) refuses a worker cwd that does
    not resolve strictly inside a sandbox root, so every supervisor fixture
    that reaches the grant plan puts its worktree, HOME and temp here.
    """
    return tmp_path / ".aiworkhub" / "runtime" / "worktrees" / request_id


def _supervisor_spec(tmp_path, **extra):
    process_dir = tmp_path / "supervisor"
    process_dir.mkdir(parents=True, exist_ok=True)
    worktree = _sandbox_request_dir(tmp_path) / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    spec = {
        "argv": ["fake-worker", "--run"],
        "cwd": str(worktree),
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
    # create_workspace's shape: <sandbox root>/<request>/{worktree,home}, with
    # the request temp inside the isolated HOME.
    request_dir = _sandbox_request_dir(tmp_path)
    sandbox_root = request_dir.parent
    worktree, home = request_dir / "worktree", request_dir / "home"
    temp = home / "tmp"
    for directory in (worktree, home, temp):
        directory.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for key in ("TMP", "TEMP", "TMPDIR"):
        monkeypatch.setenv(key, str(temp))
    for key in ("XDG_STATE_HOME", "CODEX_HOME"):
        monkeypatch.delenv(key, raising=False)
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
    grants = list(request.filesystem_grants)
    assert [item for item in grants if item.access != "traverse"] == [
        # The shim and the one package it runs -- not the whole npm dir.
        grant(str(shim), "read_execute", persistent=True),
        grant(str(package_dir), "read_execute", persistent=True),
        grant(str(worktree), "modify"),
        grant(str(home), "modify"),
        grant(str(temp), "modify"),
    ]
    # request_scoped_grants' documented order: every leaf first (cwd, then the
    # HOME/temp env keys), then non-inheritable traverse nearest-first up to and
    # including the sandbox root.  The chain stops at the root, whose traverse
    # is persistent (shared by every launch of this SID); ce33d1c replaced the
    # NF-2026-01004 volume-root traverse with the per-session sandbox drive, so
    # nothing above the root -- not even the volume root -- is ever granted.
    assert [item for item in grants if item.access == "traverse"] == [
        grant(str(request_dir), "traverse"),
        grant(str(sandbox_root), "traverse", persistent=True),
    ]
    assert grant(str(worktree.parent), "traverse") in grants
    assert not any(item.path == str(worktree.anchor) for item in grants)


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


def test_supervisor_grants_no_provider_inside_a_system_tree(monkeypatch, tmp_path) -> None:
    """ALL APPLICATION PACKAGES already reads Program Files and %SystemRoot%."""
    program_files, windows, profile = (
        tmp_path / n for n in ("Program Files", "Windows", "profile")
    )
    node = program_files / "nodejs" / "node.exe"
    system_exe = windows / "System32" / "tool.exe"
    for exe in (node, system_exe):
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
    shim, package_dir = _npm_shim(profile / "AppData" / "Roaming", "claude", "@anthropic-ai\\claude-code")
    monkeypatch.setattr(
        windows_appcontainer,
        "_sensitive_roots",
        lambda: (
            [os.path.normcase(str(profile))],
            [os.path.normcase(str(program_files)), os.path.normcase(str(windows))],
        ),
    )
    assert worker_supervisor._provider_install_grants(str(node)) == []
    assert worker_supervisor._provider_install_grants(str(system_exe)) == []
    # A per-user npm install is granted exactly as before.
    grant = windows_appcontainer.ContainerGrant
    assert worker_supervisor._provider_install_grants(str(shim)) == [
        grant(str(shim), "read_execute", persistent=True),
        grant(str(package_dir), "read_execute", persistent=True),
    ]


@pytest.mark.parametrize("name", ["Admin Owned Tools", "d" * 200])
def test_supervisor_status_keeps_the_one_time_grant_command_whole(
    monkeypatch, tmp_path, name
) -> None:
    detail = windows_appcontainer._all_packages_grant_hint(str(tmp_path / name))
    command = detail[detail.index("icacls "):]

    def _denied(_request):
        raise windows_appcontainer.AppContainerError(
            windows_appcontainer.AppContainerReason.FILESYSTEM_GRANT_FAILED,
            detail=detail,
            operation="grant_path_access",
            win_error=5,
        )

    statuses, popen_calls = _patch_supervisor_seams(monkeypatch)
    monkeypatch.setattr(worker_supervisor.windows_appcontainer, "launch_appcontainer", _denied)

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
    # The status keeps 500 chars; the whole detail, command last, fits.
    assert statuses[-1]["error"] == f"AppContainerError:filesystem_grant_failed: {detail}"
    assert statuses[-1]["error"].endswith(command)


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
    assert "CODEX_HOME" not in spawned[0]["env"]


def test_appcontainer_launch_env_routes_temp_inside_isolated_home(
    monkeypatch, tmp_path
) -> None:
    repository_temp = tmp_path / "repo-runtime" / "worker" / "request-1" / "tmp"
    isolated_home = tmp_path / "worktrees" / "request-1" / "home"
    monkeypatch.setattr(
        process_launcher,
        "sanitized_env",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        process_launcher,
        "worker_temp_environment",
        lambda *_args, **_kwargs: {
            "TMPDIR": str(repository_temp),
            "TMP": str(repository_temp),
            "TEMP": str(repository_temp),
        },
    )
    monkeypatch.setattr(
        process_launcher,
        "worker_validation_affordance_env",
        lambda *_args, **_kwargs: {},
    )

    env = process_launcher.worker_launch_env(
        "opencode_cli",
        repo=tmp_path / "repo",
        request_id="request-1",
        home=isolated_home,
        sandbox_backend="windows_appcontainer",
    )

    expected = str(isolated_home / "tmp")
    assert {env[key] for key in ("TMPDIR", "TMP", "TEMP")} == {expected}
    assert env["BUN_TMPDIR"] == expected
    assert (isolated_home / "tmp").is_dir()


def test_windows_codex_worker_uses_its_request_local_codex_home(
    monkeypatch, tmp_path
) -> None:
    """Codex must not infer its config home from Windows known-folder APIs."""
    monkeypatch.setattr(sys, "platform", "win32")
    _patch_launch_seams(
        monkeypatch,
        tmp_path,
        sandbox_backend="windows_appcontainer",
        canonical_repo_id=CANONICAL_REPO_ID,
    )
    monkeypatch.setattr(
        process_launcher,
        "worker_launch_env",
        lambda adapter_id, **kwargs: {"HOME": str(kwargs["home"])},
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
        adapter_id="codex_cli",
        model=None,
        owner_prompt="do the thing",
        timeout_seconds=60,
    )

    assert result["ok"] is True, result
    assert spawned[0]["env"]["HOME"] == str(tmp_path / "workspace-home")
    assert spawned[0]["env"]["CODEX_HOME"] == str(
        tmp_path / "workspace-home" / ".codex"
    )


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
        def __init__(self, pipe, spec, writable_roots) -> None:
            self.record = {
                "pipe": pipe, "spec": spec, "writable": list(writable_roots), "events": [],
            }
            bridges.append(self.record)

        def start(self, job) -> None:
            self.record["job"] = job
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
    # What the container can write: exactly the request's modify grants.
    assert bridge["writable"] == [
        g.path for g in launches[0].filesystem_grants if g.access == "modify"
    ]
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
    # The bridge owns the pipe from construction; it is closed, never started.
    (bridge,) = bridges
    assert bridge["events"] == ["close"]


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
    # Strictly inside a sandbox root: request_scoped_grants silently omits an
    # ambient HOME/temp outside one (ce33d1c/71c2fe4).
    request_dir = _sandbox_request_dir(tmp_path)
    sandbox_root = request_dir.parent
    worktree, home, scratch = (request_dir / n for n in ("wt", "home", "scratch"))
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
    grants = list(request.filesystem_grants)
    assert [item for item in grants if item.access != "traverse"] == [
        grant(str(home), "modify"),
        grant(str(scratch), "modify"),
        # The root, never the cd subdir a candidate could have made a junction.
        grant(str(worktree), "read_execute"),
    ]
    # request_scoped_grants' documented order: traverse nearest-first from the
    # leaves' parent up to and including the sandbox root, never above it --
    # the per-session sandbox drive replaced the NF-2026-01004 volume-root ACE.
    assert [item for item in grants if item.access == "traverse"] == [
        grant(str(request_dir), "traverse"),
        grant(str(sandbox_root), "traverse", persistent=True),
    ]
    assert grant(str(worktree.parent), "traverse") in grants
    assert not any(g.path == str(worktree.anchor) for g in grants)
    # Only the shared sandbox root outlives the launch; every request grant is
    # revoked with it.
    assert [g for g in grants if g.persistent] == [
        grant(str(sandbox_root), "traverse", persistent=True)
    ]
    # Not a Python: no PYTHONPATH, and no shim facts, appear.
    assert "PYTHONPATH" not in request.environment
    assert windows_appcontainer.APPCONTAINER_ANCESTORS_ENV not in request.environment


def test_appcontainer_validation_disables_the_agent_shell_rewrite(
    tmp_path: Path, monkeypatch, identity_osfhandle
) -> None:
    """NF-2026-01037: the validation lane must keep git.exe reachable, so it
    builds its AppContainerRequest with agent_shell=False -- unlike the
    worker supervisor lane, which keeps the default agent-shell rewrite."""
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

    worker_workspace._run_appcontainer_validation(
        ["pytest", "-q"],
        workspace=SimpleNamespace(repo=tmp_path, path=tmp_path, home=tmp_path),
        adapter_id="claude_cli",
        cwd=tmp_path,
        env={"PATH": "x"},
        timeout_seconds=30,
    )

    assert launches[0].request.agent_shell is False


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
    # Strictly inside a sandbox root (ce33d1c/71c2fe4); the interpreter stays
    # outside it, where a persistent read grant is allowed.
    request_dir = _sandbox_request_dir(tmp_path)
    request_dir.mkdir(parents=True)
    sandbox_root = request_dir.parent
    worktree, home, scratch = (request_dir / n for n in ("wt", "home", "scratch"))
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
    grants = list(request.filesystem_grants)
    assert [item for item in grants if item.access != "traverse"] == [
        grant(str(home), "modify"),
        grant(str(scratch), "modify"),
        grant(str(worktree), "read_execute"),
        grant(str(venv / "Scripts"), "read_execute", persistent=True),
        grant(str(venv / "pyvenv.cfg"), "read_execute", persistent=True),
        grant(str(venv / "Lib" / "site-packages"), "read_execute", persistent=True),
        grant(str(base), "read_execute", persistent=True),
        grant(windows_appcontainer.APPCONTAINER_PYTHON_SITE, "read_execute", persistent=True),
    ]
    # request_scoped_grants' documented order: traverse nearest-first up to and
    # including the sandbox root, never above it -- the per-session sandbox
    # drive replaced the NF-2026-01004 volume-root ACE.
    assert [item for item in grants if item.access == "traverse"] == [
        grant(str(request_dir), "traverse"),
        grant(str(sandbox_root), "traverse", persistent=True),
    ]
    assert grant(str(worktree.parent), "traverse") in grants
    assert not any(g.path == str(worktree.anchor) for g in grants)
    # NF-40: the mkdir/realpath shim first, ahead of every candidate component.
    assert request.environment["PYTHONPATH"].split(os.pathsep) == [
        windows_appcontainer.APPCONTAINER_PYTHON_SITE, str(worktree), "."
    ]


def _planted_and_canonical_venvs(tmp_path: Path):
    """The review's steering attack, laid out: the worker planted
    ``<worktree>\\.venv`` whose ``home =`` names a sibling request's worktree;
    the canonical repository has its own venv."""
    worktree, repo = tmp_path / "wt", tmp_path / "repo"
    target = tmp_path / "sibling_request_worktree"
    target.mkdir()
    for root, home in ((worktree, target), (repo, tmp_path / "Python312")):
        (root / ".venv" / "Scripts").mkdir(parents=True)
        (root / ".venv" / "Scripts" / "python.exe").write_bytes(b"MZ")
        (root / ".venv" / "pyvenv.cfg").write_text(f"home = {home}\n", encoding="utf-8")
    return worktree, repo, target


def test_the_appcontainer_lane_never_resolves_the_worktree_interpreter(tmp_path: Path) -> None:
    worktree, repo, _target = _planted_and_canonical_venvs(tmp_path)
    workspace = SimpleNamespace(path=worktree, repo=repo)
    declared = [".venv/Scripts/python.exe", "-m", "pytest"]

    local, _ = worker_workspace._normalize_validation_interpreter_argv(workspace, declared)
    assert Path(local[0]).parent.parent.parent == worktree.resolve()  # other lanes: unchanged
    contained, receipt = worker_workspace._normalize_validation_interpreter_argv(
        workspace, declared, workspace_local=False
    )
    assert Path(contained[0]) == (repo / ".venv" / "Scripts" / "python.exe").absolute()
    assert receipt["source"] == "canonical_repository"


def test_run_validations_resolves_appcontainer_interpreters_outside_the_worktree(
    tmp_path: Path, monkeypatch
) -> None:
    seen: list[dict] = []

    class _Stop(Exception):
        pass

    def _record(workspace, argv, **kwargs):
        seen.append(kwargs)
        raise _Stop

    monkeypatch.setattr(worker_workspace, "_normalize_validation_interpreter_argv", _record)
    monkeypatch.setattr(worker_workspace, "provision_validation_exec_scratch", lambda _ws, **_: tmp_path)
    workspace = SimpleNamespace(path=tmp_path, repo=tmp_path, home=tmp_path)
    with pytest.raises(_Stop):
        worker_workspace.run_validations(
            workspace, [".venv/Scripts/python.exe -m pytest -q"],
            backend="windows_appcontainer", adapter_id="claude_cli",
        )
    assert seen == [{"workspace_local": False}]


def test_a_planted_venv_steers_no_persistent_grant(
    tmp_path: Path, monkeypatch, identity_osfhandle
) -> None:
    """Belt and braces: even handed the planted interpreter, the lane refuses
    before any grant or launch."""
    worktree, _repo, _target = _planted_and_canonical_venvs(tmp_path)
    launches: list[_FakeValidationLaunch] = []
    _stub_repo_id(monkeypatch)
    _install_fake_launch(
        monkeypatch, stdout=b"", stderr=b"",
        outcome=windows_appcontainer.AppContainerLifecycleResult(
            windows_appcontainer.AppContainerLifecycleState.EXITED, exit_code=0
        ),
        sink=launches,
    )
    with pytest.raises(OSError, match="windows_appcontainer_validation_launch_failed"):
        worker_workspace._run_appcontainer_validation(
            [str(worktree / ".venv" / "Scripts" / "python.exe"), "-m", "pytest"],
            workspace=SimpleNamespace(repo=tmp_path, path=worktree, home=tmp_path / "home"),
            adapter_id="claude_cli",
            cwd=worktree,
            env={"TMP": str(tmp_path / "scratch")},
            timeout_seconds=30,
        )
    assert launches == []


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


# ---------------------------------------------------------------------------
# NF-40: the request root, and git outside the container
# ---------------------------------------------------------------------------


def _request_layout(tmp_path: Path, request_id: str = "a" * 32):
    """``create_workspace``'s shape: <root>/<request_id>/{worktree,home}, with
    <root> a sandbox root ``request_scoped_grants`` recognises."""
    request_root = _sandbox_request_dir(tmp_path, request_id)
    worktree, home = request_root / "worktree", request_root / "home"
    worktree.mkdir(parents=True)
    home.mkdir()
    scratch = home / f"aiworkhub_validation_exec_{request_id}"
    scratch.mkdir()
    workspace = SimpleNamespace(
        repo=tmp_path, path=worktree, home=home, request_id=request_id
    )
    return workspace, request_root, scratch


def test_the_request_root_is_read_when_it_holds_only_the_worktree_and_home(tmp_path):
    """pytest stats the parent of its rootdir before it collects anything."""
    workspace, request_root, _scratch = _request_layout(tmp_path)
    assert worker_workspace._appcontainer_request_root(workspace) == request_root

    (request_root / "unexpected").mkdir()
    assert worker_workspace._appcontainer_request_root(workspace) == workspace.path


@pytest.mark.parametrize("change", ["request_id", "home", "name"])
def test_any_other_workspace_shape_reads_only_the_worktree(tmp_path, change):
    workspace, _request_root, _scratch = _request_layout(tmp_path)
    if change == "request_id":
        workspace.request_id = "b" * 32
    elif change == "home":
        workspace.home = tmp_path / "elsewhere"
    else:
        renamed = workspace.path.with_name("wt")
        workspace.path.rename(renamed)
        workspace.path = renamed
    assert worker_workspace._appcontainer_request_root(workspace) == workspace.path


def test_appcontainer_validation_grants_the_request_root_read_only_after_home_and_temp(
    tmp_path: Path, monkeypatch, identity_osfhandle
) -> None:
    """HOME and temp come first so the protected directories beneath them keep
    their modify access; the root, walked after, can only add read."""
    launches: list[_FakeValidationLaunch] = []
    _stub_repo_id(monkeypatch)
    _install_fake_launch(
        monkeypatch, stdout=b"", stderr=b"",
        outcome=windows_appcontainer.AppContainerLifecycleResult(
            windows_appcontainer.AppContainerLifecycleState.EXITED, exit_code=0
        ),
        sink=launches,
    )
    workspace, request_root, scratch = _request_layout(tmp_path)
    env = {"HOME": str(workspace.home), "TMPDIR": str(scratch), "TMP": str(scratch)}

    worker_workspace._run_appcontainer_validation(
        ["node", "--version"], workspace=workspace, adapter_id="claude_cli",
        cwd=workspace.path, env=env, timeout_seconds=30,
    )

    grant = windows_appcontainer.ContainerGrant
    request = launches[0].request
    grants = list(request.filesystem_grants)
    # NF-2026-01341: a TMPDIR-bearing node command starts under the host
    # interpreter, whose persistent install-root grants are not request grants.
    assert [
        item for item in grants if item.access != "traverse" and not item.persistent
    ] == [
        grant(str(workspace.home), "modify"),
        grant(str(scratch), "modify"),
        grant(str(request_root), "read_execute"),
    ]
    # The request root's parent is the sandbox root itself: the one traverse
    # left once the root's own traverse gives way to read_execute, persistent
    # because every launch of this SID shares it.  The chain stops there --
    # the per-session sandbox drive replaced the NF-2026-01004 volume-root ACE.
    assert [item for item in grants if item.access == "traverse"] == [
        grant(str(request_root.parent), "traverse", persistent=True),
    ]
    assert not any(item.path == str(request_root.anchor) for item in grants)
    assert tuple(request.capability_sids) == ()  # offline


def test_a_candidate_pythonpath_cannot_shadow_the_container_shim(
    tmp_path: Path, monkeypatch, identity_osfhandle
) -> None:
    """The shim is first; a candidate ``sitecustomize`` on its own PYTHONPATH
    component is never the one Python imports."""
    launches: list[_FakeValidationLaunch] = []
    _stub_repo_id(monkeypatch)
    _install_fake_launch(
        monkeypatch, stdout=b"", stderr=b"",
        outcome=windows_appcontainer.AppContainerLifecycleResult(
            windows_appcontainer.AppContainerLifecycleState.EXITED, exit_code=0
        ),
        sink=launches,
    )
    workspace, _request_root, scratch = _request_layout(tmp_path)
    (workspace.path / "src").mkdir()
    (workspace.path / "src" / "sitecustomize.py").write_text("raise SystemExit\n", encoding="utf-8")
    python = tmp_path / "Python312" / "python.exe"
    python.parent.mkdir()
    python.write_bytes(b"MZ")

    worker_workspace._run_appcontainer_validation(
        [str(python), "-P", "-m", "pytest"], workspace=workspace, adapter_id="claude_cli",
        cwd=workspace.path,
        env={"TMPDIR": str(scratch), "PYTHONPATH": str(workspace.path / "src")},
        timeout_seconds=30,
    )

    request = launches[0].request
    assert request.environment["PYTHONPATH"].split(os.pathsep) == [
        windows_appcontainer.APPCONTAINER_PYTHON_SITE, str(workspace.path / "src")
    ]
    # The host's own lstat of exactly the directories above the request root:
    # nothing the container can write, and nothing inside the request.
    import json

    ancestors = json.loads(request.environment[windows_appcontainer.APPCONTAINER_ANCESTORS_ENV])
    assert list(ancestors) == [
        os.path.normcase(str(parent)) for parent in workspace.path.parent.parents
    ]
    persistent = [g.path for g in request.filesystem_grants if g.persistent]
    assert windows_appcontainer.APPCONTAINER_PYTHON_SITE in persistent
    for path in persistent:  # nothing persistent under a container-writable root
        assert not Path(path).is_relative_to(workspace.path.parent)


_HARDENED_DIFF_CHECK = worker_workspace._HOST_READONLY_GIT_COMMANDS[("diff", "--check")]


def test_only_an_exact_diff_check_from_the_worktree_root_is_hardened() -> None:
    git = r"C:\Program Files\Git\mingw64\bin\git.exe"
    assert worker_workspace._host_readonly_git_argv([git, "diff", "--check"], None) == [
        git, *_HARDENED_DIFF_CHECK
    ]
    for argv, cd in (
        ([git, "diff", "--check"], "sub"),
        ([git, "status"], None),
        ([git, "diff", "--check", "HEAD"], None),
        ([git, "-c", "core.fsmonitor=x", "diff", "--check"], None),
    ):
        assert worker_workspace._host_readonly_git_argv(argv, cd) == argv
    for flag in (
        "--no-pager", "--attr-source=HEAD", "core.fsmonitor=false", "credential.helper=",
        "protocol.allow=never", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all",
    ):
        assert flag in _HARDENED_DIFF_CHECK
    assert f"core.hooksPath={os.devnull}" in _HARDENED_DIFF_CHECK


def _no_subprocess(monkeypatch):
    def _refuse(*_args, **_kwargs):
        raise AssertionError("no git may run")

    monkeypatch.setattr(worker_workspace.subprocess, "run", _refuse)


def test_host_git_refuses_everything_but_the_hardened_allowlist(tmp_path, monkeypatch) -> None:
    _no_subprocess(monkeypatch)
    workspace, _request_root, scratch = _request_layout(tmp_path)
    git = str(tmp_path / "bin" / "git.exe")
    for argv in (
        [git, "diff", "--check"],  # never unhardened
        ["git", *_HARDENED_DIFF_CHECK],  # never a PATH search
        [git, "status"],
        [git, *_HARDENED_DIFF_CHECK, "HEAD"],
        [str(tmp_path / "bin" / "sh.exe"), *_HARDENED_DIFF_CHECK],
        # a git the container could have written is never run on the host
        [str(scratch / "git.exe"), *_HARDENED_DIFF_CHECK],
        [str(workspace.path / "git.exe"), *_HARDENED_DIFF_CHECK],
    ):
        with pytest.raises(OSError, match="windows_host_git_not_allowlisted"):
            worker_workspace._run_host_readonly_git(
                argv, workspace=workspace,
                writable=(workspace.path, workspace.home, scratch), timeout_seconds=5,
            )


def _git_available():
    import shutil

    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    return git


def _linked_worktree(tmp_path: Path, files: dict[str, bytes] | None = None):
    """A canonical repository with one commit and a detached linked worktree."""
    git = _git_available()
    repo, worktree = tmp_path / "repo", tmp_path / "wt"
    repo.mkdir()
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([git, "init", "-q"], cwd=repo, check=True, capture_output=True)
    for relative, content in (files or {"a.txt": b"clean\n"}).items():
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / relative).write_bytes(content)
    for argv in (["add", "-A"], ["commit", "-q", "-m", "base"]):
        subprocess.run([git, *ident, *argv], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [git, "worktree", "add", "-q", "--detach", str(worktree)],
        cwd=repo, check=True, capture_output=True,
    )
    workspace = SimpleNamespace(repo=repo, path=worktree, home=tmp_path / "home")
    return workspace, git


def _host_diff_check(workspace, git):
    return worker_workspace._run_host_readonly_git(
        [git, *_HARDENED_DIFF_CHECK], workspace=workspace, writable=(), timeout_seconds=60,
    )


def test_host_git_checks_the_candidate_with_attributes_from_head(tmp_path) -> None:
    """A candidate ``.gitattributes`` cannot switch the whitespace check off --
    plain ``git diff --check`` would honour it and pass."""
    workspace, git = _linked_worktree(tmp_path)
    assert _host_diff_check(workspace, git).returncode == 0

    (workspace.path / "a.txt").write_bytes(b"clean\ntrailing   \n")
    (workspace.path / ".gitattributes").write_bytes(b"* -whitespace\n")
    plain = subprocess.run([git, "diff", "--check"], cwd=workspace.path, capture_output=True)
    assert plain.returncode == 0  # the bypass is real

    result = _host_diff_check(workspace, git)
    assert result.returncode == 2
    assert "a.txt:2: trailing whitespace." in result.stdout
    # The location stays; git's echo of the line -- file content -- does not.
    assert "trailing   " not in result.stdout


def test_host_git_executes_nothing_the_candidate_configured(tmp_path, monkeypatch) -> None:
    """The candidate can write its HOME and its worktree, never the canonical
    .git.  Config in HOME (global) and attributes in the worktree name drivers,
    an external diff and an fsmonitor hook; none of them may run on the host."""
    workspace, git = _linked_worktree(tmp_path)
    sentinel = tmp_path / "executed"
    probe = tmp_path / "probe.py"
    probe.write_text(
        f"import pathlib, sys\npathlib.Path({str(sentinel)!r}).write_text('x')\n"
        "sys.stdout.write(sys.stdin.read())\n",
        encoding="utf-8",
    )
    command = f'"{Path(sys.executable).as_posix()}" "{probe.as_posix()}"'
    home = tmp_path / "home"
    home.mkdir()
    config = home / ".gitconfig"
    config.write_text(
        f"[core]\n\tfsmonitor = {command}\n"
        f"[diff]\n\texternal = {command}\n"
        f'[diff "evil"]\n\tcommand = {command}\n\ttextconv = {command}\n'
        f'[filter "evil"]\n\tclean = {command}\n',
        encoding="utf-8",
    )
    (workspace.path / ".gitattributes").write_bytes(b"* filter=evil diff=evil\n")
    (workspace.path / "a.txt").write_bytes(b"changed\n")
    for key in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(key, str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))

    subprocess.run([git, "diff"], cwd=workspace.path, capture_output=True, stdin=subprocess.DEVNULL)
    if not sentinel.exists():
        pytest.skip("this host's git ran none of the planted commands; nothing to disprove")
    sentinel.unlink()

    result = _host_diff_check(workspace, git)
    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()


def test_host_git_never_follows_a_rewritten_git_pointer(tmp_path, monkeypatch) -> None:
    """The worktree's .git file is candidate-writable: pointing it at a git dir
    the candidate built (with its own config) is refused before git starts."""
    workspace, git = _linked_worktree(tmp_path)
    evil = tmp_path / "evil_gitdir"
    evil.mkdir()
    (evil / "gitdir").write_text(str(workspace.path / ".git") + "\n", encoding="utf-8")
    (evil / "commondir").write_text(str(workspace.repo / ".git") + "\n", encoding="utf-8")
    (evil / "HEAD").write_text("0" * 40 + "\n", encoding="utf-8")
    (evil / "config.worktree").write_text("[core]\n\tfsmonitor = evil\n", encoding="utf-8")
    # Git hides the pointer, and Windows refuses to truncate a hidden file by
    # recreating it; rewrite it in place, as a worker would.
    with open(workspace.path / ".git", "r+", encoding="utf-8") as pointer:
        pointer.truncate(0)
        pointer.write(f"gitdir: {evil}\n")
    _no_subprocess(monkeypatch)

    with pytest.raises(OSError, match="windows_host_git_worktree_unverified"):
        _host_diff_check(workspace, git)


def test_host_git_runs_from_the_proven_record_with_only_canonical_config(
    tmp_path, monkeypatch
) -> None:
    workspace, git = _linked_worktree(tmp_path)
    seen: dict = {}

    def _record(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "evil")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.fsmonitor'='evil'")
    monkeypatch.setattr(worker_workspace.subprocess, "run", _record)
    _host_diff_check(workspace, git)

    admin = worker_workspace._verified_worktree_admin_dir(workspace.repo, workspace.path)
    assert admin.parent == (workspace.repo / ".git" / "worktrees").resolve()
    env = seen["env"]
    assert seen["cwd"] == admin
    assert (env["GIT_DIR"], env["GIT_WORK_TREE"]) == (str(admin), str(workspace.path))
    # No global config: the request HOME is candidate-writable.  The system
    # config is the install's own (and carries core.autocrlf), so it stays.
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert "GIT_CONFIG_NOSYSTEM" not in env
    assert env["GIT_NO_LAZY_FETCH"] == "1"  # offline, with protocol.allow=never
    assert env["GIT_OPTIONAL_LOCKS"] == "0"  # writes nothing
    assert "GIT_EXTERNAL_DIFF" not in env and "GIT_CONFIG_PARAMETERS" not in env
    assert seen["argv"] == [git, *_HARDENED_DIFF_CHECK]
    assert seen["shell"] is False


def _routing_stubs(monkeypatch, tmp_path, git: str):
    workspace, _request_root, scratch = _request_layout(tmp_path)
    container: list[list[str]] = []
    monkeypatch.setattr(worker_workspace, "provision_validation_exec_scratch", lambda _ws, **_: scratch)
    monkeypatch.setattr(worker_workspace, "cleanup_validation_exec_scratch", lambda _path: None)
    monkeypatch.setattr(worker_workspace, "python_candidate_authority", lambda _ws: {"digest": ""})
    monkeypatch.setattr(
        worker_workspace, "_normalize_validation_interpreter_argv",
        lambda _ws, tokens, **_kwargs: (list(tokens), None),
    )
    monkeypatch.setattr(
        worker_workspace, "_normalize_trusted_validation_executable_argv_with_authority",
        lambda tokens, _repo: ([git if tokens[0] == "git" else tokens[0], *tokens[1:]], (), None),
    )

    def _container(argv, **kwargs):
        container.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(worker_workspace, "_run_appcontainer_validation", _container)
    return workspace, container


def test_run_validations_runs_diff_check_on_the_host_and_everything_else_contained(
    tmp_path, monkeypatch
) -> None:
    git = str(tmp_path / "bin" / "git.exe")
    workspace, container = _routing_stubs(monkeypatch, tmp_path, git)
    host: list[list[str]] = []

    def _host(argv, **kwargs):
        host.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(worker_workspace, "_run_host_readonly_git", _host)
    rows = worker_workspace.run_validations(
        workspace, ["git diff --check", "node --version"],
        backend="windows_appcontainer", adapter_id="claude_cli",
    )

    assert host == [[git, *_HARDENED_DIFF_CHECK]]
    assert container == [["node", "--version"]]
    assert [row["execution_boundary"] for row in rows] == [
        worker_workspace.HOST_READONLY_GIT_BOUNDARY, "windows_appcontainer"
    ]
    assert rows[0]["executed_argv"] == [git, *_HARDENED_DIFF_CHECK]
    assert rows[0]["declared_argv"] == ["git", "diff", "--check"]


def test_run_validations_never_runs_any_other_git_command_anywhere(tmp_path, monkeypatch) -> None:
    """Not in the container, where git cannot start, and not on the host: a
    typed environment block, not a candidate failure."""
    git = str(tmp_path / "bin" / "git.exe")
    workspace, container = _routing_stubs(monkeypatch, tmp_path, git)
    (workspace.path / "src").mkdir()
    _no_subprocess(monkeypatch)

    with pytest.raises(worker_workspace.ValidationEnvironmentBlocked) as excinfo:
        worker_workspace.run_validations(
            workspace, ["git status", "cd src && git diff --check"],
            backend="windows_appcontainer", adapter_id="claude_cli",
        )

    assert container == []
    rows = excinfo.value.results
    assert all(row["returncode"] is None for row in rows)
    assert all("windows_host_git_not_allowlisted" in row["launch_error_message"] for row in rows)


# ---------------------------------------------------------------------------
# NF-40 security review: host git never follows a link the candidate planted
# ---------------------------------------------------------------------------

windows_only = pytest.mark.skipif(os.name != "nt", reason="needs NTFS junctions")
_SECRET = b"SECRET_CONTENT_LINE_WITH_TRAILING_SPACE   \n"


def _junction(link: Path, target: Path) -> None:
    """A directory junction, which needs no privilege -- as the reviewer made it."""
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def _git_spy(monkeypatch):
    """Count every subprocess the code under test starts from here on."""
    started: list[list[str]] = []
    real = worker_workspace.subprocess.run

    def _spy(argv, *args, **kwargs):
        started.append(list(argv))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(worker_workspace.subprocess, "run", _spy)
    return started


def _leak_setup(tmp_path: Path):
    workspace, git = _linked_worktree(
        tmp_path, {"a.txt": b"clean\n", "pkg/leak.py": b"x = 1\n"}
    )
    outside = tmp_path / "outside_secret"
    outside.mkdir()
    (outside / "leak.py").write_bytes(_SECRET)
    return workspace, git, outside


@windows_only
def test_host_git_refuses_a_planted_junction_and_never_runs_git(tmp_path, monkeypatch):
    """The review's reproduction, live: without the guard, the hardened
    command prints the outside file; with it, git never starts."""
    workspace, git, outside = _leak_setup(tmp_path)
    import shutil

    shutil.rmtree(workspace.path / "pkg")
    _junction(workspace.path / "pkg", outside)
    plain = subprocess.run(
        [git, *_HARDENED_DIFF_CHECK], cwd=workspace.path, capture_output=True, text=True
    )
    assert "SECRET_CONTENT_LINE" in plain.stdout  # the leak is real

    started = _git_spy(monkeypatch)
    result = _host_diff_check(workspace, git)

    assert started == []
    assert result.returncode == 1
    assert result.stderr == "host_git_worktree_reparse_point_refused:pkg"
    assert "SECRET" not in result.stdout + result.stderr


@windows_only
def test_host_git_refuses_a_nested_junction(tmp_path, monkeypatch):
    workspace, git, outside = _leak_setup(tmp_path)
    (workspace.path / "pkg" / "deeper").mkdir()
    _junction(workspace.path / "pkg" / "deeper" / "j", outside)
    started = _git_spy(monkeypatch)

    result = _host_diff_check(workspace, git)

    assert started == []
    assert result.stderr == "host_git_worktree_reparse_point_refused:pkg/deeper/j"


@windows_only
def test_host_git_refuses_a_junction_above_the_worktree(tmp_path, monkeypatch):
    """The worktree root itself, or anything up to the request root."""
    workspace, git, outside = _leak_setup(tmp_path)
    started = _git_spy(monkeypatch)
    moved = tmp_path / "moved"
    workspace.path.rename(moved)
    _junction(workspace.path, moved)

    result = _host_diff_check(workspace, git)

    assert started == []
    assert result.stderr == "host_git_worktree_reparse_point_refused:."


@pytest.mark.parametrize("kind", ["directory", "file"])
def test_host_git_refuses_a_symlink(tmp_path, monkeypatch, kind):
    workspace, git, outside = _leak_setup(tmp_path)
    link = workspace.path / ("pkg2" if kind == "directory" else "a_link.py")
    target = outside if kind == "directory" else outside / "leak.py"
    try:
        os.symlink(target, link, target_is_directory=kind == "directory")
    except OSError:
        pytest.skip("this host cannot create symlinks without privilege")
    started = _git_spy(monkeypatch)

    result = _host_diff_check(workspace, git)

    assert started == []
    assert result.stderr == f"host_git_worktree_reparse_point_refused:{link.name}"


def test_host_git_refuses_any_file_level_reparse_point(tmp_path, monkeypatch):
    """Any tag -- not only links: a file whose attributes carry the reparse bit
    (fake Win32 lstat, so it is deterministic on every platform)."""
    workspace, git, _outside = _leak_setup(tmp_path)
    tagged = workspace.path / "pkg" / "leak.py"
    real_lstat = os.lstat

    def _lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        if Path(path) == tagged:
            return SimpleNamespace(
                st_mode=info.st_mode, st_nlink=1,
                st_file_attributes=0x400,  # FILE_ATTRIBUTE_REPARSE_POINT
            )
        return info

    started = _git_spy(monkeypatch)
    monkeypatch.setattr(worker_workspace.os, "lstat", _lstat)
    result = _host_diff_check(workspace, git)

    assert started == []
    assert result.stderr == "host_git_worktree_reparse_point_refused:pkg/leak.py"


def test_host_git_refuses_a_hard_linked_file(tmp_path, monkeypatch):
    workspace, git, outside = _leak_setup(tmp_path)
    (workspace.path / "pkg" / "leak.py").unlink()
    os.link(outside / "leak.py", workspace.path / "pkg" / "leak.py")
    started = _git_spy(monkeypatch)

    result = _host_diff_check(workspace, git)

    assert started == []
    assert result.stderr == "host_git_worktree_hard_link_refused:pkg/leak.py"
    assert "SECRET" not in result.stdout + result.stderr


def test_host_git_still_runs_on_a_clean_worktree(tmp_path, monkeypatch):
    workspace, git, _outside = _leak_setup(tmp_path)
    started = _git_spy(monkeypatch)

    result = _host_diff_check(workspace, git)

    assert result.returncode == 0, result.stderr
    assert started == [[git, *_HARDENED_DIFF_CHECK]]


def test_host_git_fails_closed_past_the_walk_bound(tmp_path, monkeypatch):
    workspace, git, _outside = _leak_setup(tmp_path)
    monkeypatch.setattr(worker_workspace, "_HOST_GIT_WALK_LIMIT", 2)
    started = _git_spy(monkeypatch)

    with pytest.raises(OSError, match="host_git_worktree_walk_limit_exceeded:2"):
        _host_diff_check(workspace, git)
    assert started == []


def test_host_git_refuses_while_a_container_can_still_write_the_worktree(
    tmp_path, monkeypatch
):
    """Its own worker's modify grant goes only after that worker's job is
    closed; while one is present, host git does not run at all."""
    workspace, git, _outside = _leak_setup(tmp_path)
    seen: list[str] = []

    def _writers(path):
        seen.append(path)
        return ["S-1-15-2-1-2-3"]

    monkeypatch.setattr(windows_appcontainer, "appcontainer_writers", _writers)
    started = _git_spy(monkeypatch)

    with pytest.raises(OSError, match="host_git_worktree_still_container_writable:S-1-15-2-1-2-3"):
        _host_diff_check(workspace, git)
    assert started == [] and seen == [str(workspace.path)]


@windows_only
@requires_host_win32_privileges
def test_appcontainer_writers_names_a_package_sid_with_any_write_right(tmp_path):
    root = tmp_path / "root"
    (root / "child").mkdir(parents=True)
    assert windows_appcontainer.appcontainer_writers(str(root)) == []

    def _icacls(*args):
        subprocess.run(["icacls", str(root), *args], check=True, capture_output=True)

    _icacls("/grant", "*S-1-15-2-1:(OI)(CI)(RX)")
    assert windows_appcontainer.appcontainer_writers(str(root)) == []  # read only
    _icacls("/grant", "*S-1-15-2-1:(OI)(CI)(M)")
    assert windows_appcontainer.appcontainer_writers(str(root)) == ["S-1-15-2-1"]
    # Inherited counts too: the child is just as writable.
    assert windows_appcontainer.appcontainer_writers(str(root / "child")) == ["S-1-15-2-1"]


@windows_only
@requires_host_win32_privileges
def test_run_validations_fails_the_gate_on_a_planted_junction(tmp_path, monkeypatch):
    """A candidate-made link is a failed gate (validation_failed), not an
    environment block, and nothing of the target reaches the record."""
    git = str(tmp_path / "bin" / "git.exe")
    workspace, container = _routing_stubs(monkeypatch, tmp_path, git)
    outside = tmp_path / "outside_secret"
    outside.mkdir()
    (outside / "leak.py").write_bytes(_SECRET)
    _junction(workspace.path / "pkg", outside)
    admin = tmp_path / "admin"
    admin.mkdir()
    monkeypatch.setattr(worker_workspace, "_verified_worktree_admin_dir", lambda _r, _p: admin)
    _no_subprocess(monkeypatch)

    with pytest.raises(worker_workspace.ValidationRunError) as excinfo:
        worker_workspace.run_validations(
            workspace, ["git diff --check"], backend="windows_appcontainer",
            adapter_id="claude_cli",
        )

    assert not isinstance(excinfo.value, worker_workspace.ValidationEnvironmentBlocked)
    (row,) = excinfo.value.results
    assert row["returncode"] == 1 and "launch_error" not in row
    assert row["stderr_tail"] == "host_git_worktree_reparse_point_refused:pkg"
    assert "SECRET" not in json.dumps(row)
    assert container == []


# NF-2026-01009: the command the NeedFix reproduced, and the exact form
# measured working inside the container on Node v22.16.0.
_NODE_TEST_ARGV = ["node", "--test", "vscode-extension/test/x.test.js"]
_NODE_TEST_FILE = "vscode-extension/test/x.test.js"


def _run_node_lane_validation(
    tmp_path: Path,
    monkeypatch,
    argv: list[str],
    *,
    node_version: str,
    state=None,
):
    """Drive ``_run_appcontainer_validation`` with no AppContainer and no node.

    ``native_handle`` is patched rather than ``msvcrt.get_osfhandle`` (what
    ``identity_osfhandle`` does) so this proof runs wherever the suite runs --
    the point of keeping the argv rewrite pure and the probe injectable.
    """
    launches: list[_FakeValidationLaunch] = []
    probed: list[str] = []
    _stub_repo_id(monkeypatch)
    monkeypatch.setattr(windows_appcontainer, "native_handle", lambda fd: fd)
    _install_fake_launch(
        monkeypatch,
        stdout=b"",
        stderr=b"",
        outcome=windows_appcontainer.AppContainerLifecycleResult(
            state or windows_appcontainer.AppContainerLifecycleState.EXITED,
            exit_code=None if state else 0,
        ),
        sink=launches,
    )

    def _fake_probe(executable: str) -> str:
        probed.append(executable)
        return node_version

    monkeypatch.setattr(
        worker_workspace, "_appcontainer_node_version", _fake_probe
    )
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    workspace = SimpleNamespace(
        repo=tmp_path, path=worktree, home=tmp_path / "home"
    )
    result = worker_workspace._run_appcontainer_validation(
        argv,
        workspace=workspace,
        adapter_id="claude_cli",
        cwd=worktree,
        env={"PATH": "x"},
        timeout_seconds=30,
    )
    return result, launches, probed


def test_node_validation_argv_inserts_preserve_symlinks_idempotently() -> None:
    """The measured working form, and rewriting it again is a no-op."""
    once = worker_workspace._appcontainer_node_validation_argv(
        _NODE_TEST_ARGV, "v22.16.0"
    )
    assert once == [
        "node",
        "--preserve-symlinks",
        "--preserve-symlinks-main",
        "--test",
        "--experimental-test-isolation=none",
        _NODE_TEST_FILE,
    ]
    assert (
        worker_workspace._appcontainer_node_validation_argv(once, "v22.16.0")
        == once
    )
    # Already carrying one half is not a reason to add it twice.
    half = ["node", "--preserve-symlinks-main", "--test", _NODE_TEST_FILE]
    rewritten = worker_workspace._appcontainer_node_validation_argv(
        half, "v22.16.0"
    )
    assert rewritten.count("--preserve-symlinks") == 1
    assert rewritten.count("--preserve-symlinks-main") == 1


def test_node_validation_argv_preserves_symlinks_without_test() -> None:
    """The EPERM-lstat half applies to every node command; isolation does not."""
    assert worker_workspace._appcontainer_node_validation_argv(
        ["node.exe", "scripts/build.js"], "v22.16.0"
    ) == [
        "node.exe",
        "--preserve-symlinks",
        "--preserve-symlinks-main",
        "scripts/build.js",
    ]


@pytest.mark.parametrize(
    ("node_version", "expected"),
    [
        ("v24.1.0", "--test-isolation=none"),
        ("v23.6.0", "--test-isolation=none"),
        ("v23.5.0", "--experimental-test-isolation=none"),
        ("v22.16.0", "--experimental-test-isolation=none"),
        ("v22.8.0", "--experimental-test-isolation=none"),
        ("v22.7.1", None),
        ("v20.11.0", None),
        ("", None),
        ("not a version", None),
    ],
)
def test_node_validation_argv_isolation_flag_follows_the_node_version(
    node_version: str, expected: str | None
) -> None:
    """22.8 experimental, >=23.6 stable, older or unknown left alone."""
    rewritten = worker_workspace._appcontainer_node_validation_argv(
        _NODE_TEST_ARGV, node_version
    )
    isolation = [part for part in rewritten if "test-isolation" in part]
    assert isolation == ([expected] if expected else [])
    # The preserve-symlinks half never depends on the version.
    assert rewritten[1:3] == ["--preserve-symlinks", "--preserve-symlinks-main"]


@pytest.mark.parametrize(
    "declared",
    [
        ["--test-isolation=process"],
        ["--test-isolation", "process"],
        ["--experimental-test-isolation=process"],
        ["--experimental-test-isolation", "process"],
    ],
)
def test_node_validation_argv_never_overrides_a_declared_isolation(
    declared: list[str],
) -> None:
    """A card that asked for child isolation keeps it, in either flag form."""
    argv = ["node", "--test", *declared, _NODE_TEST_FILE]
    assert worker_workspace._appcontainer_node_validation_argv(
        argv, "v22.16.0"
    ) == [
        "node",
        "--preserve-symlinks",
        "--preserve-symlinks-main",
        "--test",
        *declared,
        _NODE_TEST_FILE,
    ]


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["python.exe", "-m", "pytest", "-q"],
        ["pytest", "-q"],
        ["git", "diff", "--check"],
        ["npm", "--test", "run", "test"],
        ["nodemon", "--test", _NODE_TEST_FILE],
    ],
)
def test_node_validation_argv_leaves_every_non_node_command_alone(
    argv: list[str],
) -> None:
    """Only a ``node``/``node.exe`` basename is rewritten -- nothing near it."""
    assert (
        worker_workspace._appcontainer_node_validation_argv(argv, "v22.16.0")
        == argv
    )


@pytest.mark.parametrize(
    "executable",
    ["NODE.EXE", "Node", os.path.join("tools", "nodejs", "node.exe")],
)
def test_node_validation_argv_matches_the_node_basename_case_insensitively(
    executable: str,
) -> None:
    assert worker_workspace._appcontainer_node_validation_argv(
        [executable, "--test", _NODE_TEST_FILE], "v23.6.0"
    ) == [
        executable,
        "--preserve-symlinks",
        "--preserve-symlinks-main",
        "--test",
        "--test-isolation=none",
        _NODE_TEST_FILE,
    ]


def test_appcontainer_validation_launches_the_rewritten_node_argv(
    tmp_path: Path, monkeypatch
) -> None:
    """What the container runs is the rewrite, and the evidence records it."""
    result, launches, probed = _run_node_lane_validation(
        tmp_path, monkeypatch, list(_NODE_TEST_ARGV), node_version="v22.16.0"
    )

    expected = [
        "node",
        "--preserve-symlinks",
        "--preserve-symlinks-main",
        "--test",
        "--experimental-test-isolation=none",
        _NODE_TEST_FILE,
    ]
    assert list(launches[0].request.argv) == expected
    assert result.args == expected
    assert probed == ["node"]


def test_appcontainer_validation_timeout_reports_the_rewritten_node_argv(
    tmp_path: Path, monkeypatch
) -> None:
    """A timeout's ``cmd`` must be what actually ran, not what was declared."""
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        _run_node_lane_validation(
            tmp_path,
            monkeypatch,
            list(_NODE_TEST_ARGV),
            node_version="v23.6.0",
            state=windows_appcontainer.AppContainerLifecycleState.TIMEOUT,
        )

    assert list(excinfo.value.cmd) == [
        "node",
        "--preserve-symlinks",
        "--preserve-symlinks-main",
        "--test",
        "--test-isolation=none",
        _NODE_TEST_FILE,
    ]


def test_appcontainer_validation_node_lane_leaves_a_python_argv_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    """The Python adaptation still owns the Python lane: env, never argv."""
    base = tmp_path / "Python312"
    base.mkdir()
    venv = tmp_path / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Lib" / "site-packages").mkdir(parents=True)
    python = venv / "Scripts" / "python.exe"
    python.write_bytes(b"MZ")
    (venv / "pyvenv.cfg").write_text(f"home = {base}\n", encoding="utf-8")
    argv = [str(python), "-m", "pytest", "-q"]

    result, launches, probed = _run_node_lane_validation(
        tmp_path, monkeypatch, list(argv), node_version="v22.16.0"
    )

    assert list(launches[0].request.argv) == argv
    assert result.args == argv
    # No node argv, so the version probe is never even reached.
    assert probed == []
    pythonpath = launches[0].request.environment["PYTHONPATH"]
    assert pythonpath.split(os.pathsep)[0] == (
        windows_appcontainer.APPCONTAINER_PYTHON_SITE
    )


@pytest.mark.parametrize(
    "argv",
    [["pytest", "-q"], ["git", "diff", "--check"], ["npm", "--test", "run"]],
)
def test_appcontainer_validation_node_lane_leaves_other_commands_unchanged(
    tmp_path: Path, monkeypatch, argv: list[str]
) -> None:
    """Every other boundary command reaches the container byte for byte."""
    result, launches, probed = _run_node_lane_validation(
        tmp_path, monkeypatch, list(argv), node_version="v22.16.0"
    )

    assert list(launches[0].request.argv) == argv
    assert result.args == argv
    assert probed == []


def test_node_version_probe_is_bounded_shell_free_and_cached(
    tmp_path: Path, monkeypatch
) -> None:
    """One host call per resolved node, no shell, and a real timeout."""
    monkeypatch.setattr(worker_workspace, "_APPCONTAINER_NODE_VERSION_CACHE", {})
    calls: list[dict] = []

    def _fake_run(argv, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        return subprocess.CompletedProcess(list(argv), 0, "v22.16.0\n", "")

    monkeypatch.setattr(worker_workspace.subprocess, "run", _fake_run)
    node = tmp_path / "node.exe"
    node.write_bytes(b"MZ")

    assert worker_workspace._appcontainer_node_version(str(node)) == "v22.16.0"
    assert worker_workspace._appcontainer_node_version(str(node)) == "v22.16.0"

    assert len(calls) == 1
    assert calls[0]["argv"] == [str(node.resolve()), "--version"]
    assert calls[0]["shell"] is False
    assert calls[0]["capture_output"] is True
    assert 0 < calls[0]["timeout"] <= 10


@pytest.mark.parametrize(
    "outcome",
    [
        OSError("node is not executable"),
        subprocess.TimeoutExpired(["node", "--version"], 10),
        subprocess.CompletedProcess(["node", "--version"], 1, "", "boom"),
        subprocess.CompletedProcess(["node", "--version"], 0, "", ""),
    ],
)
def test_node_version_probe_reports_unknown_for_every_failure(
    tmp_path: Path, monkeypatch, outcome
) -> None:
    """Any failure is an unknown version, and unknown adds no isolation flag."""
    monkeypatch.setattr(worker_workspace, "_APPCONTAINER_NODE_VERSION_CACHE", {})
    calls: list[int] = []

    def _fake_run(argv, **_kwargs):
        calls.append(1)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(worker_workspace.subprocess, "run", _fake_run)
    node = tmp_path / "node.exe"
    node.write_bytes(b"MZ")

    assert worker_workspace._appcontainer_node_version(str(node)) == ""
    assert worker_workspace._appcontainer_node_validation_argv(
        _NODE_TEST_ARGV, ""
    ) == [
        "node",
        "--preserve-symlinks",
        "--preserve-symlinks-main",
        "--test",
        _NODE_TEST_FILE,
    ]
    # A failed probe is cached too: a card declaring several node commands must
    # not pay the probe timeout again for each one.
    assert worker_workspace._appcontainer_node_version(str(node)) == ""
    assert len(calls) == 1


def test_node_version_probe_reports_unknown_for_an_unresolvable_executable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(worker_workspace, "_APPCONTAINER_NODE_VERSION_CACHE", {})

    def _never(*_args, **_kwargs):
        raise AssertionError("an unresolvable node is never launched")

    monkeypatch.setattr(worker_workspace.subprocess, "run", _never)

    assert worker_workspace._appcontainer_node_version(
        str(tmp_path / "missing" / "node.exe")
    ) == ""


def _exited(code: int = 0):
    return windows_appcontainer.AppContainerLifecycleResult(
        windows_appcontainer.AppContainerLifecycleState.EXITED, exit_code=code
    )


def test_appcontainer_validation_scratch_lives_under_the_request_home(
    tmp_path: Path, monkeypatch
) -> None:
    r"""NF-2026-01341: the repository temp is outside every sandbox root, so the
    container got no grant there and fell back to the shared ``AC\Temp``; and
    the request directory itself must keep only ``worktree`` and ``home``."""
    monkeypatch.delenv(worker_workspace.VALIDATION_EXEC_SCRATCH_ROOT_ENV, raising=False)
    request_dir = _sandbox_request_dir(tmp_path, "req-nf1341")
    worktree, home = request_dir / "worktree", request_dir / "home"
    worktree.mkdir(parents=True)
    home.mkdir()
    workspace = SimpleNamespace(
        request_id="req-nf1341", repo=tmp_path, path=worktree, home=home
    )

    scratch = worker_workspace.provision_validation_exec_scratch(
        workspace, backend=worker_workspace.WINDOWS_APPCONTAINER_BACKEND
    )
    try:
        assert scratch.parent == home.resolve()
        assert sorted(os.listdir(request_dir)) == ["home", "worktree"]
    finally:
        worker_workspace.cleanup_validation_exec_scratch(scratch)


def test_appcontainer_validation_starts_a_non_python_command_under_the_trampoline(
    tmp_path: Path, monkeypatch, identity_osfhandle
) -> None:
    """NF-2026-01341: the host interpreter's sitecustomize restores TEMP/TMP
    from TMPDIR before the declared command runs; the record keeps that
    command."""
    if not windows_appcontainer.is_python_executable(sys.executable):
        pytest.skip("the trampoline needs a python*.exe host interpreter")
    launches: list[_FakeValidationLaunch] = []
    _stub_repo_id(monkeypatch)
    _install_fake_launch(
        monkeypatch, stdout=b"", stderr=b"", outcome=_exited(), sink=launches
    )
    request_dir = _sandbox_request_dir(tmp_path)
    worktree, scratch = request_dir / "worktree", request_dir / "home" / "vx"
    worktree.mkdir(parents=True)
    scratch.mkdir(parents=True)
    declared = [r"C:\Program Files\CMake\bin\cmake.exe", "--workflow", "--preset", "ci"]

    result = worker_workspace._run_appcontainer_validation(
        declared,
        workspace=SimpleNamespace(repo=tmp_path, path=worktree, home=scratch.parent),
        adapter_id="claude_cli",
        cwd=worktree,
        env={"PATH": "x", "TMPDIR": str(scratch), "TEMP": str(scratch), "TMP": str(scratch)},
        timeout_seconds=30,
    )

    assert result.args == declared
    request = launches[0].request
    assert list(request.argv) == [
        sys.executable,
        "-c",
        worker_workspace._APPCONTAINER_TEMP_TRAMPOLINE,
        *declared,
    ]
    assert request.environment["PYTHONPATH"].split(os.pathsep)[0] == (
        windows_appcontainer.APPCONTAINER_PYTHON_SITE
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows exit codes and site shim")
def test_appcontainer_temp_trampoline_and_site_shim_on_the_host(tmp_path: Path) -> None:
    trampoline = [sys.executable, "-c", worker_workspace._APPCONTAINER_TEMP_TRAMPOLINE]
    for code in (0, 3, -1073741819):  # the last is 0xC0000005
        child = [sys.executable, "-c", f"import os;os._exit({code})"]
        assert subprocess.run([*trampoline, *child]).returncode == code & 0xFFFFFFFF
    env = {
        **os.environ,
        "PYTHONPATH": windows_appcontainer.APPCONTAINER_PYTHON_SITE,
        "TMPDIR": str(tmp_path),
        "TEMP": "ac-temp",
        "TMP": "ac-temp",
    }
    show = [sys.executable, "-c", "import os;print(os.environ['TEMP'], os.environ['TMP'])"]
    env.pop(worker_workspace.VALIDATION_EXEC_SCRATCH_ROOT_ENV, None)
    unscoped = subprocess.run(show, env=env, capture_output=True, text=True, check=True)
    assert unscoped.stdout.split() == ["ac-temp", "ac-temp"]
    env[worker_workspace.VALIDATION_EXEC_SCRATCH_ROOT_ENV] = str(tmp_path)
    scoped = subprocess.run(show, env=env, capture_output=True, text=True, check=True)
    assert scoped.stdout.split() == [str(tmp_path)] * 2


@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("AIWORKHUB_LIVE_APPCONTAINER_PROBE") != "1",
    reason="live AppContainer probe: Windows and AIWORKHUB_LIVE_APPCONTAINER_PROBE=1",
)
def test_live_appcontainer_nested_child_sees_the_request_temp(tmp_path: Path) -> None:
    r"""NF-2026-01341, measured: without the trampoline a nested child sees
    ``...\AC\Temp``; with it, the request's own scratch."""
    import msvcrt
    import shutil

    repo = Path(__file__).resolve().parents[1]
    root = (repo / ".aiworkhub" / "runtime" / "worktrees").resolve()
    live = root / f"live-{os.urandom(4).hex()}"
    worktree, home = live / "worktree", live / "home"
    scratch = home / "vx"
    scratch.mkdir(parents=True)
    worktree.mkdir()
    env = {
        "SystemRoot": os.environ["SystemRoot"],
        "PATH": os.environ["SystemRoot"] + r"\System32",
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMPDIR": str(scratch),
        "TEMP": str(scratch),
        "TMP": str(scratch),
        worker_workspace.VALIDATION_EXEC_SCRATCH_ROOT_ENV: str(scratch),
        "PYTHONPATH": windows_appcontainer.APPCONTAINER_PYTHON_SITE,
    }
    grants = windows_appcontainer.request_scoped_grants(
        env, str(worktree)
    ) + windows_appcontainer.python_read_grants(
        sys.executable, env["PYTHONPATH"], covered=[str(worktree), str(home)]
    )
    echo = [os.environ["ComSpec"], "/d", "/c", "echo %TEMP%"]
    trampoline = [sys.executable, "-c", worker_workspace._APPCONTAINER_TEMP_TRAMPOLINE]
    seen = {}
    try:
        for name, argv in {"bare": echo, "trampoline": [*trampoline, *echo]}.items():
            out = tmp_path / f"{name}.txt"
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            launch = None
            try:
                os.set_inheritable(fd, True)
                handle = msvcrt.get_osfhandle(fd)
                launch = windows_appcontainer.launch_appcontainer(
                    windows_appcontainer.AppContainerRequest(
                        argv=argv,
                        repo_id="aiworkhub-probe",
                        worker_kind="probe",
                        working_directory=str(worktree),
                        environment=env,
                        stdout_handle=handle,
                        stderr_handle=handle,
                        filesystem_grants=grants,
                        agent_shell=False,
                    )
                )
                result = launch.wait(60_000, terminate_on_timeout=True)
                assert result.exit_code == 0, (name, out.read_text(errors="replace"))
            finally:
                if launch is not None:
                    launch.close()
                os.close(fd)
            seen[name] = out.read_text(errors="replace").strip()
    finally:
        shutil.rmtree(live, ignore_errors=True)
    print(f"\nTEMP seen: {seen}")
    assert r"\ac\temp" in seen["bare"].lower()
    assert seen["trampoline"].lower().endswith(r"\home\vx")
