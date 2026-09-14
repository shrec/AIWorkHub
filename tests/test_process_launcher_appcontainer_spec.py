"""Deterministic fake-Windows proof for the NF-2026-00452 supervisor spec."""

from __future__ import annotations

import contextlib
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import aiworkhub.process_launcher as process_launcher
import aiworkhub.process_launcher_launch_isolated as module
import aiworkhub.windows_appcontainer as windows_appcontainer
import aiworkhub.worker_supervisor as worker_supervisor

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
