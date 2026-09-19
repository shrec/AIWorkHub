"""Reasoning-effort and context-capacity wiring: real launch-path proof.

These tests pin the canonical :mod:`aiworkhub.reasoning_policy` decision to the
actual CLI worker launch for Claude, Codex and OpenCode:

* Claude-5 selects its verified ``max`` ceiling for a MAXIMUM profile (never a
  silent downgrade to ``high``); a standard-tier model selects ``high`` and an
  unverified model fails closed to no flag.
* Codex selects its verified ``xhigh`` ceiling.
* OpenCode declares no rankable effort control, so no ``--variant`` is guessed.
* A non-applied decision (capability ceiling / provider default) never places a
  control flag on argv and never claims ``applied`` in the receipt.
* ``context_capacity`` is the verified provider/model context window, never the
  task card's ``token_budget.cap_tokens`` spend cap.
* The real ``ProcessManager._launch_isolated`` path derives the decision and
  capacity from the card/model, emits the verified tokens, and calls the bounded
  ``probe_release`` version probe for the Claude CLI.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

import pytest

from aiworkhub import process_launcher, reasoning_policy, runtime_adapters


# --- pure wiring decisions ---------------------------------------------------


def test_claude5_maximum_maps_to_max_effort_flag() -> None:
    decision = runtime_adapters.resolve_adapter_reasoning(
        "claude_cli", {"work_kind": "security"}, model="claude-opus-5"
    )
    assert decision is not None
    route = decision.route_effort
    assert route is not None
    assert route.applied is True
    assert route.applied_key == "max"
    assert runtime_adapters._effort_flag_tokens("claude_cli", "max") == [
        "--effort",
        "max",
    ]


def test_claude5_high_maps_to_high_never_downgraded() -> None:
    capability = runtime_adapters.route_reasoning_capability(
        "claude_cli", "claude-opus-5"
    )
    assert capability is not None
    assert "max" in capability.effort_keys
    high = reasoning_policy.normalize_for_route(
        reasoning_policy.ReasoningProfile.HIGH, capability
    )
    assert high.applied is True
    assert high.applied_key == "high"
    maximum = reasoning_policy.normalize_for_route(
        reasoning_policy.ReasoningProfile.MAXIMUM, capability
    )
    assert maximum.applied is True
    assert maximum.applied_key == "max"


def test_claude_standard_tier_uses_high_ceiling() -> None:
    decision = runtime_adapters.resolve_adapter_reasoning(
        "claude_cli", {"work_kind": "security"}, model="claude-haiku-4.5"
    )
    assert decision is not None
    route = decision.route_effort
    assert route is not None
    assert route.applied is True
    assert route.applied_key == "high"
    assert "max" not in runtime_adapters._claude_effort_keys("claude-haiku-4.5")


def test_claude_unverified_model_fails_closed_to_no_decision() -> None:
    assert (
        runtime_adapters.resolve_adapter_reasoning(
            "claude_cli", {"work_kind": "security"}, model=None
        )
        is None
    )
    assert (
        runtime_adapters.resolve_adapter_reasoning(
            "claude_cli", {"work_kind": "security"}, model="claude-unknown-model"
        )
        is None
    )
    assert runtime_adapters.route_reasoning_capability("claude_cli", None) is None


def test_claude_workforce_alias_resolves_to_max_effort_ladder() -> None:
    for alias in ("opus", "sonnet"):
        keys = runtime_adapters._claude_effort_keys(alias)
        assert "max" in keys
        decision = runtime_adapters.resolve_adapter_reasoning(
            "claude_cli", {"work_kind": "security"}, model=alias
        )
        assert decision is not None
        route = decision.route_effort
        assert route is not None
        assert route.applied is True
        assert route.applied_key == "max"
    haiku = runtime_adapters._claude_effort_keys("haiku")
    assert "max" not in haiku
    assert "high" in haiku


def test_codex_maximum_maps_to_xhigh_effort_flag() -> None:
    decision = runtime_adapters.resolve_adapter_reasoning(
        "codex_cli", {"work_kind": "security"}, model="gpt-5.5"
    )
    assert decision is not None
    route = decision.route_effort
    assert route is not None
    assert route.applied is True
    assert route.applied_key == "xhigh"
    assert runtime_adapters._effort_flag_tokens("codex_cli", "xhigh") == [
        "-c",
        'model_reasoning_effort="xhigh"',
    ]


def test_opencode_declares_no_effort_control_so_no_variant() -> None:
    assert (
        runtime_adapters.route_reasoning_capability(
            "opencode_cli", "anthropic/claude-sonnet-4-5"
        )
        is None
    )
    assert (
        runtime_adapters.resolve_adapter_reasoning(
            "opencode_cli",
            {"work_kind": "security"},
            model="anthropic/claude-sonnet-4-5",
        )
        is None
    )
    assert runtime_adapters._effort_flag_tokens("opencode_cli", "high") == []


def test_context_capacity_is_verified_model_window_not_spend_cap() -> None:
    assert runtime_adapters.resolve_context_capacity("claude_cli", "claude-opus-5") == 1_000_000
    assert runtime_adapters.resolve_context_capacity("claude_cli", "claude-sonnet-5") == 1_000_000
    assert runtime_adapters.resolve_context_capacity("claude_cli", "claude-haiku-4.5") == 200_000
    assert runtime_adapters.resolve_context_capacity("codex_cli", "gpt-5.5") == 921_000
    # Verified workforce aliases resolve to the same canonical model ids.
    assert runtime_adapters.resolve_context_capacity("claude_cli", "opus") == 1_000_000
    assert runtime_adapters.resolve_context_capacity("claude_cli", "sonnet") == 1_000_000
    assert runtime_adapters.resolve_context_capacity("claude_cli", "haiku") == 200_000
    # Unknown capability fails closed; never conflated with a spend cap.
    assert runtime_adapters.resolve_context_capacity("claude_cli", None) is None
    assert runtime_adapters.resolve_context_capacity("claude_cli", "claude-unknown") is None
    assert runtime_adapters.resolve_context_capacity("codex_cli", "gpt-unknown") is None
    assert (
        runtime_adapters.resolve_context_capacity(
            "opencode_cli", "anthropic/claude-sonnet-4-5"
        )
        is None
    )


def test_non_applied_capability_ceiling_never_emits_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ceiling_cap = reasoning_policy.RouteCapability(
        route_id="claude_cli",
        provider_family=reasoning_policy.ProviderFamily.CLAUDE,
        supports_effort_control=True,
        effort_keys=("low", "medium"),
    )
    request = reasoning_policy.EffortRequest(
        role=reasoning_policy.TaskRole.IMPLEMENTER,
        risk_tier=reasoning_policy.RiskTier.CRITICAL,
        work_kind=reasoning_policy.WorkKind.SECURITY,
        difficulty=reasoning_policy.Difficulty.STANDARD,
        provider_family=reasoning_policy.ProviderFamily.CLAUDE,
    )
    decision = reasoning_policy.resolve_reasoning_effort(request, ceiling_cap)
    assert decision.route_effort is not None
    assert decision.route_effort.applied is False
    assert (
        decision.route_effort.status
        is reasoning_policy.ControlStatus.CAPABILITY_CEILING
    )

    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _name: sys.executable)
    plan = runtime_adapters.build_runtime_command(
        "claude_cli",
        "write tests",
        tmp_path,
        model="claude-opus-5",
        reasoning_decision=decision,
        context_capacity=None,
    )
    assert plan.launchable is True
    assert "--effort" not in plan.argv
    receipt = plan.reasoning_receipt
    assert receipt is not None
    assert receipt["applied"] is False
    assert receipt["flag_emitted"] is False
    assert receipt["status"] == "capability_ceiling"
    assert receipt["context_capacity"] is None


def test_build_runtime_command_places_claude_max_effort_and_capacity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    decision = runtime_adapters.resolve_adapter_reasoning(
        "claude_cli", {"work_kind": "security"}, model="claude-opus-5"
    )
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _name: sys.executable)
    plan = runtime_adapters.build_runtime_command(
        "claude_cli",
        "write tests",
        tmp_path,
        model="claude-opus-5",
        reasoning_decision=decision,
        context_capacity=1_000_000,
    )
    assert plan.launchable is True
    assert plan.argv[-2:] == ["--effort", "max"]
    assert plan.argv[2] == "write tests"
    assert plan.context_capacity == 1_000_000
    receipt = plan.reasoning_receipt
    assert receipt is not None
    assert receipt["applied"] is True
    assert receipt["flag_emitted"] is True
    assert receipt["applied_key"] == "max"
    assert receipt["profile"] == "canonical_maximum"
    assert receipt["context_capacity"] == 1_000_000


def test_build_runtime_command_places_codex_xhigh_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    decision = runtime_adapters.resolve_adapter_reasoning(
        "codex_cli", {"work_kind": "security"}, model="gpt-5.5"
    )
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _name: sys.executable)
    plan = runtime_adapters.build_runtime_command(
        "codex_cli",
        "write tests",
        tmp_path,
        model="gpt-5.5",
        reasoning_decision=decision,
        context_capacity=921_000,
    )
    assert plan.launchable is True
    assert 'model_reasoning_effort="xhigh"' in plan.argv
    assert plan.context_capacity == 921_000


def test_opencode_build_never_emits_variant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _name: sys.executable)
    plan = runtime_adapters.build_runtime_command(
        "opencode_cli",
        "write tests",
        tmp_path,
        model="anthropic/claude-sonnet-4-5",
    )
    assert plan.launchable is True
    assert "--variant" not in plan.argv


# --- bounded cross-platform release probe -----------------------------------


def test_probe_release_ok_fast_exit() -> None:
    result = runtime_adapters.probe_release(sys.executable, args=["--version"])
    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["returncode"] == 0
    assert result["release"]


def test_probe_release_overflow_has_no_evidence() -> None:
    result = runtime_adapters.probe_release(
        sys.executable, args=["-c", "print('x' * 100000)"], limit=64,
    )
    assert result["ok"] is False
    assert result["status"] == "overflow"
    assert result["release"] == ""


def test_probe_release_nonzero_exit_has_no_evidence() -> None:
    result = runtime_adapters.probe_release(
        sys.executable, args=["-c", "import sys; sys.exit(3)"],
    )
    assert result["ok"] is False
    assert result["status"] == "nonzero_exit"
    assert result["returncode"] == 3
    assert result["release"] == ""


def test_probe_release_spawn_failure_has_no_evidence() -> None:
    result = runtime_adapters.probe_release("/definitely/not/a/real/executable")
    assert result["ok"] is False
    assert result["status"] == "spawn_failed"
    assert result["release"] == ""


# --- real launch path -------------------------------------------------------


class _StubManager:
    """``launch_isolated`` takes ``self`` as an ordinary parameter."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.events: list[dict[str, object]] = []

    def _append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        event.setdefault("request_id", "R-stub")
        return event

    def _blocked(self, *args: object, **kwargs: object) -> dict[str, object]:
        return process_launcher.ProcessManager._blocked(self, *args, **kwargs)


def _launch(manager: _StubManager, **overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "task_id": "T-1",
        "runner": "codex_cli",
        "topic": "aiworkhub",
        "adapter_id": "codex_cli",
        "model": None,
        "owner_prompt": "prompt",
        "timeout_seconds": 600,
    }
    kwargs.update(overrides)
    return process_launcher.ProcessManager._launch_isolated(manager, **kwargs)


_PROBE_OK: dict[str, object] = {
    "ok": True,
    "status": "ok",
    "release": "1.2.3",
    "returncode": 0,
}


def _run_claude_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    model: str | None,
    committed_card: dict[str, object],
    probe_result: dict[str, object] | None = _PROBE_OK,
    runner: str = "claude_cli",
    use_real_workforce_identity: bool = False,
) -> tuple[dict[str, object], dict[str, object], list[str]]:
    from aiworkhub.repository_state import bootstrap_repository
    from aiworkhub.worker_workspace import WorkerWorkspace

    authority = tmp_path / "authority"
    workspace_root = tmp_path / "R-cli"
    workspace = workspace_root / "worktree"
    home = workspace_root / "home"
    process_dir = tmp_path / "processes"
    for directory in (authority, workspace, home, process_dir):
        directory.mkdir(parents=True, exist_ok=True)
    bootstrap_repository(authority, repo_name="authority")
    bootstrap_repository(workspace, repo_name="workspace")
    (authority / "src").mkdir(exist_ok=True)
    (workspace / "src").mkdir(exist_ok=True)
    (authority / "src/changed.py").write_text(
        "def canonical_symbol():\n    return 'old'\n", encoding="utf-8",
    )
    (home / "claude.json").write_text("{}", encoding="utf-8")
    worker_workspace = WorkerWorkspace(
        request_id="R-cli",
        repo=authority,
        path=workspace,
        home=home,
        allowed_writes=("src/changed.py",),
        parent_baseline={},
        workspace_baseline={},
    )

    class _LaunchManager(_StubManager):
        def __init__(self) -> None:
            super().__init__(authority)
            self.process_dir = process_dir
            self._live: dict[str, object] = {}
            self._lock = contextlib.nullcontext()

        def _preflight_card(
            self, *_args: object, **_kwargs: object,
        ) -> dict[str, object]:
            return {"request_id": "R-cli", "allowed_writes": ["src/changed.py"]}

        def _with_dependency_inputs(
            self, card: dict[str, object],
        ) -> dict[str, object]:
            return dict(card)

        def _resolve_provider_env(
            self, _adapter_id: str, model: str | None,
        ) -> tuple[dict[str, str], str | None]:
            return {}, model

        def _launch_reservation(
            self, _event: dict[str, object],
        ) -> contextlib.AbstractContextManager[None]:
            return contextlib.nullcontext()

        def _terminal_authority_grant_path(self, request_id: str) -> Path:
            return process_dir / f"{request_id}.authority.json"

        def _terminal_authority_key(self) -> bytes:
            return b"test-key"

        def _build_adapter(self, **kwargs: object) -> object:
            return runtime_adapters.build_adapter_command(**kwargs)

        def _popen(self, *_args: object, **_kwargs: object) -> object:
            return type("FakeProcess", (), {"pid": 4321})()

        def _monitor(self, _live: object) -> None:
            return None

    class _TaskEngine:
        @staticmethod
        def claim_start_exact(
            *_args: object, **_kwargs: object,
        ) -> dict[str, object]:
            return {"ok": True, "card": {"claim_epoch": 1}}

    class _FakeThread:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.args = args
            self.kwargs = kwargs

        def start(self) -> None:
            return None

    class _Runtime:
        server_name = "test-worker-mcp"
        tool_names = ("aiworkhub_worker_source_graph_query",)
        audit_ledger_path = None
        audit_hmac_key_path = None
        claude_mcp_config_path = home / "claude.json"
        copilot_mcp_config_path = home / "copilot.json"
        codex_config_toml_path = home / "config.toml"
        kilo_config_path = home / "kilo.json"
        package_import_root = tmp_path

    written: list[tuple[Path, dict[str, object]]] = []
    probe_calls: list[str] = []

    def _write_json_0600(path: Path, data: dict[str, object]) -> None:
        written.append((path, data))
        path.write_text(json.dumps(data))

    def _probe(executable: str, **_kwargs: object) -> dict[str, object]:
        probe_calls.append(executable)
        return dict(probe_result or {})

    monkeypatch.setattr(process_launcher, "launch_gates_open", lambda: True)
    monkeypatch.setattr(process_launcher, "task_engine", _TaskEngine)
    monkeypatch.setattr(process_launcher, "_validate_adapter_identity", lambda *_a: None)
    if not use_real_workforce_identity:
        monkeypatch.setattr(
            process_launcher,
            "validate_workforce_identity",
            lambda _runner, _adapter_id, model, **_kwargs: model,
        )
    monkeypatch.setattr(
        process_launcher, "_memory_launch_admission", lambda: {"admit": True},
    )
    monkeypatch.setattr(process_launcher, "_external_readonly_dirs", lambda *_a: [])
    monkeypatch.setattr(process_launcher, "_task_authority_repo", lambda *_a: authority)
    monkeypatch.setattr(process_launcher, "_launch_project_context", lambda *_a: None)
    monkeypatch.setattr(process_launcher, "create_workspace", lambda *_a: worker_workspace)
    monkeypatch.setattr(
        process_launcher, "build_residual_contract_manifest", lambda *_a: [],
    )
    monkeypatch.setattr(
        process_launcher,
        "_materialize_worker_rework_overlay",
        lambda *_a, **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        process_launcher,
        "_materialize_crash_retry_packet",
        lambda *_a, **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        process_launcher,
        "_provision_worker_mcp_runtime_for_authority",
        lambda *_a, **_kwargs: _Runtime(),
    )
    monkeypatch.setattr(
        process_launcher, "_worker_mcp_source_graph_targets", lambda _context: (),
    )
    monkeypatch.setattr(process_launcher, "_worker_mcp_session_topic", lambda *_a: "nf897")
    monkeypatch.setattr(process_launcher, "build_worker_prompt", lambda **_kwargs: "write tests")
    monkeypatch.setattr(process_launcher, "worker_launch_env", lambda *_a, **_k: {})
    monkeypatch.setattr(
        process_launcher, "sandbox_argv", lambda _w, _a, argv, **_k: argv,
    )
    monkeypatch.setattr(process_launcher, "_worker_launch_cwd", lambda path: str(path))
    monkeypatch.setattr(
        process_launcher, "_worker_supervisor_script", lambda: tmp_path / "supervisor.py",
    )
    monkeypatch.setattr(process_launcher, "_touch_0600", lambda path: path.write_text(""))
    monkeypatch.setattr(process_launcher, "chmod_path", lambda *_a: None)
    monkeypatch.setattr(process_launcher, "write_json_0600", _write_json_0600)
    monkeypatch.setattr(
        process_launcher, "_write_terminal_authority_grant", lambda *_a, **_k: None,
    )
    monkeypatch.setattr(process_launcher, "_pid_start_ticks", lambda _pid: 123)
    monkeypatch.setattr(
        process_launcher, "process_group_launch_kwargs", lambda _name: {},
    )
    monkeypatch.setattr(process_launcher.threading, "Thread", _FakeThread)
    monkeypatch.setattr(
        process_launcher,
        "_committed_claim_card",
        lambda claim, **_kwargs: {
            "request_id": "R-cli",
            "claim_epoch": int(dict(claim["card"])["claim_epoch"]),
            "allowed_writes": ["src/changed.py"],
            **committed_card,
        },
    )
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _name: sys.executable)
    monkeypatch.setattr(runtime_adapters, "probe_release", _probe)

    manager = _LaunchManager()
    result = _launch(
        manager,
        runner=runner,
        adapter_id="claude_cli",
        topic="nf897",
        model=model,
        timeout_seconds=30,
    )
    metadata = next(
        data
        for (_path, data) in written
        if data.get("schema_id") == "aiworkhub.task_mcp.isolated_request.v1"
    )
    return result, metadata, probe_calls


def test_launch_isolated_claude5_security_card_emits_max_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    result, metadata, probe_calls = _run_claude_launch(
        monkeypatch,
        tmp_path,
        model="claude-opus-5",
        committed_card={
            "work_kind": "security",
            "risk_tier": "critical",
            "token_budget": {"cap_tokens": 200_000},
        },
    )
    assert result["ok"] is True
    assert result["state"] == "running"
    assert result["model"] == "claude-opus-5"

    argv = list(metadata["worker_argv"])
    assert "--effort" in argv
    assert argv[argv.index("--effort") + 1] == "max"
    assert argv[argv.index("-p") + 1] == "write tests"

    receipt = dict(metadata["reasoning_effort"])
    assert receipt["applied"] is True
    assert receipt["flag_emitted"] is True
    assert receipt["applied_key"] == "max"
    assert receipt["profile"] == "canonical_maximum"
    assert receipt["context_capacity"] == 1_000_000
    assert metadata["token_budget"] == {"cap_tokens": 200_000}
    assert receipt["context_capacity"] != metadata["token_budget"]["cap_tokens"]

    assert metadata["claude_cli_release"] == "1.2.3"
    assert probe_calls == [str(Path(sys.executable).resolve())]


@pytest.mark.parametrize(
    ("runner", "model", "expected_model"),
    [
        ("claude_opus-5", "opus", "claude-opus-5"),
        ("claude_sonnet-5", "sonnet", "claude-sonnet-5"),
    ],
)
def test_launch_isolated_real_workforce_alias_emits_max_effort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner: str,
    model: str,
    expected_model: str,
) -> None:
    result, metadata, _probe_calls = _run_claude_launch(
        monkeypatch,
        tmp_path,
        model=model,
        runner=runner,
        use_real_workforce_identity=True,
        committed_card={
            "work_kind": "security",
            "risk_tier": "critical",
            "token_budget": {"cap_tokens": 200_000},
        },
    )
    assert result["ok"] is True
    assert result["state"] == "running"
    # The real workforce identity route normalizes the catalog alias to the
    # canonical model id, which is what lands in the argv and receipt.
    assert result["model"] == expected_model

    argv = list(metadata["worker_argv"])
    assert "--effort" in argv
    assert argv[argv.index("--effort") + 1] == "max"
    assert argv[argv.index("-p") + 1] == "write tests"

    receipt = dict(metadata["reasoning_effort"])
    assert receipt["applied"] is True
    assert receipt["flag_emitted"] is True
    assert receipt["applied_key"] == "max"
    assert receipt["profile"] == "canonical_maximum"
    assert receipt["context_capacity"] == 1_000_000
    assert metadata["token_budget"] == {"cap_tokens": 200_000}
    assert receipt["context_capacity"] != metadata["token_budget"]["cap_tokens"]


def test_launch_isolated_unverified_claude_model_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    result, metadata, probe_calls = _run_claude_launch(
        monkeypatch,
        tmp_path,
        model="claude-unverified-model",
        committed_card={
            "work_kind": "security",
            "risk_tier": "critical",
            "token_budget": {"cap_tokens": 200_000},
        },
    )
    assert result["ok"] is True
    argv = list(metadata["worker_argv"])
    assert "--effort" not in argv
    assert metadata["reasoning_effort"] is None
    assert "--model" in argv and "claude-unverified-model" in argv
    assert metadata["claude_cli_release"] == "1.2.3"
    assert len(probe_calls) == 1


def test_launch_isolated_failing_probe_never_gates_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    result, metadata, probe_calls = _run_claude_launch(
        monkeypatch,
        tmp_path,
        model="claude-opus-5",
        committed_card={
            "work_kind": "security",
            "risk_tier": "critical",
            "token_budget": {"cap_tokens": 200_000},
        },
        probe_result={
            "ok": False,
            "status": "spawn_failed",
            "release": "",
            "returncode": None,
        },
    )
    assert result["ok"] is True
    assert result["state"] == "running"
    assert metadata["claude_cli_release"] is None
    assert len(probe_calls) == 1
