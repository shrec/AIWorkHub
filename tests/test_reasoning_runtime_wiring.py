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


def test_claude_cli_argv_uses_claude_code_haiku_spelling_haiku_unchanged_elsewhere(
    tmp_path: Path,
) -> None:
    """NF-2026-01019: only the Claude CLI ``--model`` argv site is respelled.

    The canonical workforce id ``claude-haiku-4.5`` keeps driving effort and
    context-capacity resolution unchanged; only the argv token Claude Code's
    CLI receives is translated to the spelling it accepts.
    """

    assert runtime_adapters._canonical_model_id("claude-haiku-4.5") == "claude-haiku-4.5"
    assert runtime_adapters._canonical_model_id("haiku") == "claude-haiku-4.5"
    assert "max" not in runtime_adapters._claude_effort_keys("claude-haiku-4.5")
    assert "high" in runtime_adapters._claude_effort_keys("claude-haiku-4.5")
    assert runtime_adapters.resolve_context_capacity("claude_cli", "claude-haiku-4.5") == 200_000

    # The VS Code LM route never builds Claude CLI argv, so it is structurally
    # unreachable by ``_claude_cli_model_id``; its plan and canonical model
    # identity for claude-haiku-4.5 stay exactly as verified.
    vscode_lm_plan = runtime_adapters.build_runtime_command(
        "vscode_lm", "Prompt", tmp_path, model="claude-haiku-4.5"
    )
    assert vscode_lm_plan.argv == []
    assert vscode_lm_plan.validation_reason == "vscode_lm_requires_process_launcher_bridge_context"
    assert runtime_adapters.resolve_context_capacity("vscode_lm", "claude-haiku-4.5") is None
    assert runtime_adapters.route_reasoning_capability("vscode_lm", "claude-haiku-4.5") is None


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

    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: False)
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
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: False)
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
    assert "write tests" not in plan.argv
    assert plan.stdin_text == "write tests"
    assert plan.context_capacity == 1_000_000
    receipt = plan.reasoning_receipt
    assert receipt is not None
    assert receipt["applied"] is True
    assert receipt["flag_emitted"] is True
    assert receipt["applied_key"] == "max"
    assert receipt["profile"] == "canonical_maximum"
    assert receipt["context_capacity"] == 1_000_000


@pytest.mark.parametrize(
    ("card", "expected_difficulty", "expected_effort"),
    [
        ({"risk_tier": "low", "difficulty": "bounded"}, "bounded", "high"),
        ({"risk_tier": "low"}, "standard", "max"),
        (
            {"risk_tier": "low", "rework_predecessor": {"request_id": "R-prev"}},
            "complex",
            "max",
        ),
        (
            {
                "risk_tier": "low",
                "difficulty": "bounded",
                "rework_predecessor": {"request_id": "R-prev"},
            },
            "bounded",
            "high",
        ),
    ],
)
def test_reasoning_receipt_carries_the_card_difficulty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    card: dict[str, object],
    expected_difficulty: str,
    expected_effort: str,
) -> None:
    decision = runtime_adapters.resolve_adapter_reasoning(
        "claude_cli", card, model="claude-opus-5"
    )
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: False)
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
    assert plan.argv[-2:] == ["--effort", expected_effort]
    receipt = plan.reasoning_receipt
    assert receipt is not None
    assert receipt["difficulty"] == expected_difficulty


def test_build_runtime_command_places_codex_xhigh_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    decision = runtime_adapters.resolve_adapter_reasoning(
        "codex_cli", {"work_kind": "security"}, model="gpt-5.5"
    )
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: False)
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
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: False)
    monkeypatch.setattr(runtime_adapters.shutil, "which", lambda _name: sys.executable)
    plan = runtime_adapters.build_runtime_command(
        "opencode_cli",
        "write tests",
        tmp_path,
        model="anthropic/claude-sonnet-4-5",
    )
    assert plan.launchable is True
    assert "--variant" not in plan.argv


# --- card difficulty drives the derived effort request ----------------------


def _claude_decision(
    card: dict[str, object], *, is_reviewer: bool = False
) -> reasoning_policy.ReasoningDecision:
    decision = runtime_adapters.resolve_adapter_reasoning(
        "claude_cli", card, is_reviewer=is_reviewer, model="claude-opus-5"
    )
    assert decision is not None
    return decision


@pytest.mark.parametrize("risk", ["low", "medium", "high"])
def test_declared_bounded_claude_card_resolves_to_high(risk: str) -> None:
    decision = _claude_decision(
        {"work_kind": "generic", "risk_tier": risk, "difficulty": "bounded"}
    )
    assert decision.request.difficulty is reasoning_policy.Difficulty.BOUNDED
    assert decision.profile is reasoning_policy.ReasoningProfile.HIGH
    assert decision.rationale.baseline_reason == "claude_bounded_implementer_high"
    assert decision.route_effort is not None
    assert decision.route_effort.applied_key == "high"


@pytest.mark.parametrize(
    ("card", "is_reviewer"),
    [
        ({"risk_tier": "critical", "difficulty": "bounded"}, False),
        ({"work_kind": "security", "risk_tier": "low", "difficulty": "bounded"}, False),
        ({"risk_tier": "low", "difficulty": "bounded"}, True),
    ],
)
def test_declared_bounded_critical_security_and_reviewer_cards_stay_maximum(
    card: dict[str, object], is_reviewer: bool
) -> None:
    decision = _claude_decision(card, is_reviewer=is_reviewer)
    assert decision.request.difficulty is reasoning_policy.Difficulty.BOUNDED
    assert decision.profile is reasoning_policy.ReasoningProfile.MAXIMUM
    assert decision.route_effort is not None
    assert decision.route_effort.applied_key == "max"


def test_undeclared_claude_card_keeps_the_standard_maximum_rule() -> None:
    decision = _claude_decision({"work_kind": "generic", "risk_tier": "low"})
    assert decision.request.difficulty is reasoning_policy.Difficulty.STANDARD
    assert decision.profile is reasoning_policy.ReasoningProfile.MAXIMUM
    assert (
        decision.rationale.baseline_reason
        == "claude_repository_coding_default_maximum"
    )


def test_undeclared_rework_card_is_complex_and_maximum() -> None:
    decision = _claude_decision(
        {"risk_tier": "low", "rework_predecessor": {"request_id": "R-prev"}}
    )
    assert decision.request.difficulty is reasoning_policy.Difficulty.COMPLEX
    assert decision.profile is reasoning_policy.ReasoningProfile.MAXIMUM
    assert "complex_difficulty" in decision.rationale.reason_codes


def test_declared_difficulty_wins_on_a_rework_card() -> None:
    rework = {"request_id": "R-prev"}
    bounded = _claude_decision(
        {"risk_tier": "low", "difficulty": "bounded", "rework_predecessor": rework}
    )
    assert bounded.request.difficulty is reasoning_policy.Difficulty.BOUNDED
    assert bounded.profile is reasoning_policy.ReasoningProfile.HIGH
    standard = _claude_decision(
        {"risk_tier": "low", "difficulty": "standard", "rework_predecessor": rework}
    )
    assert standard.request.difficulty is reasoning_policy.Difficulty.STANDARD
    assert "complex_difficulty" not in standard.rationale.reason_codes


@pytest.mark.parametrize("value", [None, "", "easy", "unknown", 3, ["bounded"]])
def test_an_unrecognized_card_difficulty_is_treated_as_undeclared(
    value: object,
) -> None:
    plain = _claude_decision({"risk_tier": "low", "difficulty": value})
    assert plain.request.difficulty is reasoning_policy.Difficulty.STANDARD
    assert plain.profile is reasoning_policy.ReasoningProfile.MAXIMUM
    rework = _claude_decision(
        {
            "risk_tier": "low",
            "difficulty": value,
            "rework_predecessor": {"request_id": "R-prev"},
        }
    )
    assert rework.request.difficulty is reasoning_policy.Difficulty.COMPLEX


def test_declared_card_difficulty_ignores_case_and_surrounding_space() -> None:
    decision = _claude_decision({"risk_tier": "low", "difficulty": " Bounded "})
    assert decision.request.difficulty is reasoning_policy.Difficulty.BOUNDED
    assert decision.profile is reasoning_policy.ReasoningProfile.HIGH


def test_declared_bounded_difficulty_does_not_change_the_codex_decision() -> None:
    bounded = runtime_adapters.resolve_adapter_reasoning(
        "codex_cli", {"risk_tier": "low", "difficulty": "bounded"}, model="gpt-5.5"
    )
    undeclared = runtime_adapters.resolve_adapter_reasoning(
        "codex_cli", {"risk_tier": "low"}, model="gpt-5.5"
    )
    assert bounded is not None and undeclared is not None
    assert bounded.request.difficulty is reasoning_policy.Difficulty.BOUNDED
    assert bounded.profile is undeclared.profile
    assert bounded.rationale.baseline_reason == "repository_coding_quality_floor_high"


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
    preflight_card: dict[str, object] | None = None,
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
            return {
                "request_id": "R-cli",
                "allowed_writes": ["src/changed.py"],
                **(preflight_card or {}),
            }

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
            return type(
                "FakeProcess", (), {"pid": 4321, "stdin": _FakeStdin()},
            )()

        def _monitor(self, _live: object) -> None:
            return None

    class _TaskEngine:
        @staticmethod
        def claim_start_exact(
            *_args: object, **_kwargs: object,
        ) -> dict[str, object]:
            return {"ok": True, "card": {"claim_epoch": 1}}

        @staticmethod
        def mark_launch_failed(
            *_args: object, **_kwargs: object,
        ) -> dict[str, object]:
            return {"ok": True}

    class _FakeThread:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.args = args
            self.kwargs = kwargs

        def start(self) -> None:
            target = self.kwargs.get("target")
            if getattr(target, "__name__", "") == "_feed_supervisor_stdin":
                target()
            return None

        def join(self, timeout: float | None = None) -> None:
            return None

        def is_alive(self) -> bool:
            return False

    runtime_dir = home / "task_mcp_worker_runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    _audit_ledger_path = runtime_dir / "audit_ledger.jsonl"
    _audit_hmac_key_path = runtime_dir / "audit_hmac.key"
    _audit_ledger_path.write_bytes(b"")
    _audit_hmac_key_path.write_bytes(b"k" * 32)

    class _Runtime:
        server_name = "test-worker-mcp"
        tool_names = ("aiworkhub_worker_source_graph_query",)
        audit_ledger_path = _audit_ledger_path
        audit_hmac_key_path = _audit_hmac_key_path
        claude_mcp_config_path = home / "claude.json"
        copilot_mcp_config_path = home / "copilot.json"
        codex_config_toml_path = home / "config.toml"
        kilo_config_path = home / "kilo.json"
        package_import_root = tmp_path

    written: list[tuple[Path, dict[str, object]]] = []
    probe_calls: list[str] = []
    stdin_writes: list[bytes] = []

    class _FakeStdin:
        def write(self, data: bytes) -> int:
            stdin_writes.append(data)
            return len(data)

        def flush(self) -> None:
            return None

        def close(self) -> None:
            return None

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
    monkeypatch.setattr(
        process_launcher, "_sandbox_backend_for_adapter", lambda _adapter_id: "landlock",
    )
    monkeypatch.setattr(runtime_adapters, "_is_windows_host", lambda: False)
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
    metadata["_worker_stdin_text"] = (
        b"".join(stdin_writes).decode("utf-8") if stdin_writes else None
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
    assert "write tests" not in argv
    assert metadata["_worker_stdin_text"] == "write tests"

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
    (
        "difficulty",
        "risk_tier",
        "expected_effort",
        "expected_profile",
        "expected_difficulty",
    ),
    [
        ("bounded", "low", "high", "canonical_high", "bounded"),
        ("bounded", "high", "high", "canonical_high", "bounded"),
        ("bounded", "critical", "max", "canonical_maximum", "bounded"),
        (None, "medium", "max", "canonical_maximum", "standard"),
        ("standard", "medium", "max", "canonical_maximum", "standard"),
    ],
)
def test_launch_isolated_card_difficulty_selects_the_claude_effort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    difficulty: str | None,
    risk_tier: str,
    expected_effort: str,
    expected_profile: str,
    expected_difficulty: str,
) -> None:
    # The effort decision is derived from the card the launcher reads before
    # the claim, so a declared difficulty has to arrive on that preflight card.
    card: dict[str, object] = {"work_kind": "generic", "risk_tier": risk_tier}
    if difficulty is not None:
        card["difficulty"] = difficulty
    result, metadata, _probe_calls = _run_claude_launch(
        monkeypatch,
        tmp_path,
        model="claude-opus-5",
        preflight_card=card,
        committed_card={**card, "token_budget": {"cap_tokens": 200_000}},
    )
    assert result["ok"] is True

    argv = list(metadata["worker_argv"])
    assert argv[argv.index("--effort") + 1] == expected_effort
    receipt = dict(metadata["reasoning_effort"])
    assert receipt["applied"] is True
    assert receipt["flag_emitted"] is True
    assert receipt["applied_key"] == expected_effort
    assert receipt["profile"] == expected_profile
    assert receipt["difficulty"] == expected_difficulty


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
    assert "write tests" not in argv
    assert metadata["_worker_stdin_text"] == "write tests"

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
