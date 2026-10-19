"""Launch admission refuses before claim: provider launch limit and stale vscode_lm host.

Both refusals are raised by ``ProcessManager`` admission ahead of any task claim, so a
refused card stays pending and unclaimed.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    process_launcher,
    runtime_adapters,
    runtime_executable_registry,
    vscode_lm_bridge,
)

_TASK_ID = "TASK_B1"
_OPENCODE_RUNNER = "opencode_worker_b1"
_COPILOT_RUNNER = "copilot_worker_b1"
_OPENCODE_MODEL = "opencode-go/grok-4.6"
_LIMIT_REASON = "provider_launch_limit_reached:opencode-go:1"
_STALE_REASON = "vscode_lm_unavailable:vscode_lm_host_stale"
_REPO_ID = "repo_" + ("a" * 32)
_STALE_AGE_SECONDS = 600.0


@pytest.fixture(autouse=True)
def _isolated_launch_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        process_launcher.storage_retention,
        "schedule_repository_cleanup",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setenv(
        "AIWORKHUB_AUDIT_LOG_PATH",
        str(tmp_path / "isolated-runtime" / "process_logs" / "audit.jsonl"),
    )
    monkeypatch.setenv("AIWORKHUB_TOOLCHAIN_AUTHORITY_HMAC_KEY", "hex:" + ("5a" * 32))
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    monkeypatch.setenv(process_launcher.MAX_PROCESSES_ENV, "8")
    monkeypatch.delenv(process_launcher.PROVIDER_LAUNCH_LIMITS_ENV, raising=False)
    monkeypatch.setattr(
        process_launcher,
        "_available_memory_bytes",
        lambda: process_launcher.MEMORY_LAUNCH_REQUIRED_BYTES * 4,
    )
    # A host-installed executable registration must not decide whether a plan builds.
    monkeypatch.setattr(runtime_executable_registry, "_host_registration_root", lambda: None)


def _collision(**_: Any) -> dict[str, Any]:
    return {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""}


def _reservation(request_id: str, model: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "task_id": f"RESERVED_{request_id.upper().replace('-', '_')}",
        "runner": _OPENCODE_RUNNER,
        "topic": "task_mcp",
        "adapter_id": "opencode_cli",
        "model": model,
        **extra,
    }


@dataclass
class _Rig:
    """A ProcessManager built like the concurrency tests in test_process_launcher.py."""

    manager: process_launcher.ProcessManager
    runner: str
    card: dict[str, Any]
    spawns: list[tuple[Any, ...]] = field(default_factory=list)
    claims: list[tuple[Any, ...]] = field(default_factory=list)

    def launch(self, adapter_id: str, model: str | None) -> dict[str, Any]:
        return self.manager.launch(
            task_id=_TASK_ID,
            runner=self.runner,
            topic="task_mcp",
            adapter_id=adapter_id,
            model=model,
            timeout_seconds=30,
        )

    def seed_active(
        self, model: str, request_id: str = "other-provider-run", **extra: Any
    ) -> None:
        self.manager._append_event({
            "request_id": request_id,
            "task_id": f"OTHER_{request_id.upper().replace('-', '_')}",
            "runner": "opencode_other",
            "topic": "task_mcp",
            "adapter_id": "opencode_cli",
            "model": model,
            "state": "running",
            "pid": os.getpid(),
            **extra,
        })

    def states(self, request_id: str) -> list[str]:
        return [
            row["state"] for row in self.manager._events() if row["request_id"] == request_id
        ]

    def assert_card_unclaimed(self) -> None:
        assert self.claims == []
        shown = json.loads(self.manager._show_task(_TASK_ID)["stdout"])
        assert (shown["status"], shown["worker_status"], shown["claimed_by"]) == (
            "pending",
            "unclaimed",
            "",
        )


@pytest.fixture
def make_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def make(runner: str) -> _Rig:
        repo = tmp_path / "repo"
        repo.mkdir()
        card: dict[str, Any] = {
            "task_id": _TASK_ID,
            "runner": runner,
            "topic": "task_mcp",
            "status": "pending",
            "worker_status": "unclaimed",
            "claimed_by": "",
            "allowed_writes": ["out/result.json"],
            "priority": "high",
        }
        spawns: list[tuple[Any, ...]] = []

        def show_task(task_id: str) -> dict[str, Any]:
            if task_id != _TASK_ID:
                return {"returncode": 1, "stdout": "", "stderr": "task_not_found"}
            return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

        def build_plan(**_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(
                argv=[sys.executable, "-c", "pass"],
                cwd=str(repo),
                launchable=True,
                reason="",
            )

        def reach_spawn(*args: Any, **_kwargs: Any) -> Any:
            spawns.append(args)
            raise OSError("fixture spawn reached")

        manager = process_launcher.ProcessManager(
            repo=repo,
            process_log_path=tmp_path / "events.jsonl",
            process_dir=tmp_path / "processes",
            show_task=show_task,
            collision_guard=_collision,
            adapter_builder=build_plan,
            popen_factory=reach_spawn,
            isolation_enabled=False,
        )
        rig = _Rig(manager=manager, runner=runner, card=card, spawns=spawns)

        def claim(*args: Any, **kwargs: Any) -> dict[str, Any]:
            rig.claims.append((args, kwargs))
            card.update(status="processing", worker_status="claimed", claimed_by=runner)
            return {}

        monkeypatch.setattr(process_launcher.task_engine, "claim_start_exact", claim)
        return rig

    return make


@pytest.fixture
def bridge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    monkeypatch.setattr(vscode_lm_bridge, "_repo_id", lambda _repo: _REPO_ID)
    return root


@pytest.fixture
def aged_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    real_readiness = vscode_lm_bridge.bridge_readiness

    def readiness(repo: Path, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("now", time.time() + _STALE_AGE_SECONDS)
        return real_readiness(repo, **kwargs)

    monkeypatch.setattr(vscode_lm_bridge, "bridge_readiness", readiness)


def _publish_host(
    bridge_root: Path,
    name: str,
    models: list[str],
    *,
    window_id: str | None = None,
) -> None:
    vscode_lm_bridge._atomic_json(
        bridge_root / "hosts" / _REPO_ID / f"{name}.json",
        {
            "schema_id": vscode_lm_bridge.HOST_SCHEMA_ID,
            "repo_id": _REPO_ID,
            "window_id": window_id or name,
            "models": models,
            "permission_granted": True,
        },
    )


# --- per-provider launch limit -------------------------------------------------------


def test_second_opencode_go_launch_is_refused_while_one_is_active(make_rig) -> None:
    rig = make_rig(_OPENCODE_RUNNER)
    rig.seed_active(_OPENCODE_MODEL)

    result = rig.launch("opencode_cli", "opencode-go/glm-5")

    assert result["ok"] is False
    assert result["blocked_reason"] == _LIMIT_REASON
    assert rig.spawns == []
    assert rig.manager._live == {}
    assert rig.states(result["request_id"]) == ["blocked"]
    rig.assert_card_unclaimed()


def test_reservation_is_refused_before_the_claiming_body_runs(make_rig) -> None:
    rig = make_rig(_OPENCODE_RUNNER)
    rig.seed_active(_OPENCODE_MODEL)
    body_ran: list[bool] = []

    with pytest.raises(process_launcher.LaunchRejected) as refused:
        with rig.manager._launch_reservation(_reservation("req-refused", "opencode-go/glm-5")):
            body_ran.append(True)

    assert str(refused.value) == _LIMIT_REASON
    assert body_ran == []
    assert rig.states("req-refused") == []
    rig.assert_card_unclaimed()


def test_a_held_reservation_occupies_the_provider_slot(make_rig) -> None:
    rig = make_rig(_OPENCODE_RUNNER)

    with rig.manager._launch_reservation(_reservation("req-first", _OPENCODE_MODEL)):
        pass
    assert rig.states("req-first") == ["starting"]

    with pytest.raises(process_launcher.LaunchRejected) as refused:
        with rig.manager._launch_reservation(_reservation("req-second", "opencode-go/glm-5")):
            pytest.fail("a refused reservation must not run its body")

    assert str(refused.value) == _LIMIT_REASON
    assert rig.states("req-second") == []


def test_reviewer_reservation_is_refused_with_the_same_reason(make_rig) -> None:
    rig = make_rig(_OPENCODE_RUNNER)
    rig.seed_active(_OPENCODE_MODEL)
    rows_before = len(rig.manager._events())

    reservation = rig.manager._reserve_quality_reviewer_attempt(
        reviewer_task_id="REVIEW_B1",
        runner="opencode_reviewer_b1",
        adapter_id="opencode_cli",
        target_request_id="b" * 32,
        target_task_id=_TASK_ID,
        lens="correctness",
        model="opencode-go/glm-5",
        timeout_seconds=600,
    )

    assert reservation == {"ok": False, "error": _LIMIT_REASON}
    assert len(rig.manager._events()) == rows_before


def test_active_opencode_go_request_does_not_limit_other_providers(make_rig) -> None:
    rig = make_rig(_OPENCODE_RUNNER)
    rig.seed_active(_OPENCODE_MODEL)

    assert rig.manager._provider_launch_refusal(_OPENCODE_MODEL) == _LIMIT_REASON
    for model in (
        "claude-sonnet-5",
        "xai/grok-4.6",
        "omniroute/opencode-go/grok-4.6",
        "opencode-go",
        None,
    ):
        assert rig.manager._provider_launch_refusal(model) == ""

    with rig.manager._launch_reservation(_reservation("req-xai", "xai/grok-4.6")):
        pass
    assert rig.states("req-xai") == ["starting"]


def test_active_request_of_another_provider_does_not_limit_opencode_go(make_rig) -> None:
    rig = make_rig(_OPENCODE_RUNNER)
    rig.seed_active("xai/grok-4.6")
    rig.seed_active("claude-sonnet-5", request_id="claude-run")

    assert rig.manager._provider_launch_refusal(_OPENCODE_MODEL) == ""


@pytest.mark.parametrize("model", ["xai/grok-4.6", "plain-model-without-provider"])
def test_other_provider_and_slashless_launches_reach_spawn(make_rig, model: str) -> None:
    rig = make_rig(_OPENCODE_RUNNER)
    rig.seed_active(_OPENCODE_MODEL)

    result = rig.launch("opencode_cli", model)

    assert result["ok"] is False
    assert "fixture spawn reached" in result["blocked_reason"]
    assert len(rig.spawns) == 1
    assert rig.states(result["request_id"]) == ["starting", "blocked"]


def test_terminal_request_does_not_hold_the_provider_slot(make_rig) -> None:
    rig = make_rig(_OPENCODE_RUNNER)
    rig.seed_active(_OPENCODE_MODEL, state="cancelled")

    assert rig.manager._provider_launch_refusal(_OPENCODE_MODEL) == ""


def test_validation_only_replay_neither_fills_nor_needs_a_provider_slot(make_rig) -> None:
    rig = make_rig(_OPENCODE_RUNNER)
    rig.seed_active(_OPENCODE_MODEL, request_id="replay-run", provider_launched=False)
    assert rig.manager._provider_launch_refusal(_OPENCODE_MODEL) == ""

    rig.seed_active(_OPENCODE_MODEL)
    assert rig.manager._provider_launch_refusal(_OPENCODE_MODEL) == _LIMIT_REASON
    with rig.manager._launch_reservation(
        _reservation("req-replay", _OPENCODE_MODEL, provider_launched=False)
    ):
        pass
    assert rig.states("req-replay") == ["starting"]


def _cancel_requested(rig: _Rig, request_id: str = "other-provider-run") -> None:
    # The shape ProcessManager.cancel() appends: sparse, with no model.
    rig.manager._append_event({
        "request_id": request_id,
        "task_id": f"OTHER_{request_id.upper().replace('-', '_')}",
        "runner": "opencode_other",
        "topic": "task_mcp",
        "adapter_id": "opencode_cli",
        "state": "cancel_requested",
        "pid": os.getpid(),
        "reason": "owner_cancelled",
        "cancel_path": "x",
    })


def test_cancel_requested_run_keeps_its_provider_slot(make_rig) -> None:
    rig = make_rig(_OPENCODE_RUNNER)
    rig.seed_active(_OPENCODE_MODEL)
    _cancel_requested(rig)
    assert rig.manager._active_count() == 1

    assert rig.manager._provider_launch_refusal("opencode-go/glm-5") == _LIMIT_REASON
    with pytest.raises(process_launcher.LaunchRejected) as refused:
        with rig.manager._launch_reservation(_reservation("req-2", "opencode-go/glm-5")):
            pytest.fail("a refused reservation must not run its body")
    assert str(refused.value) == _LIMIT_REASON
    assert rig.states("req-2") == []
    reservation = rig.manager._reserve_quality_reviewer_attempt(
        reviewer_task_id="REVIEW_B1",
        runner="opencode_reviewer_b1",
        adapter_id="opencode_cli",
        target_request_id="b" * 32,
        target_task_id=_TASK_ID,
        lens="correctness",
        model="opencode-go/glm-5",
        timeout_seconds=600,
    )
    assert reservation == {"ok": False, "error": _LIMIT_REASON}
    result = rig.launch("opencode_cli", "opencode-go/glm-5")
    assert result["blocked_reason"] == _LIMIT_REASON
    assert rig.spawns == []


def test_cancel_requested_validation_replay_still_takes_no_slot(make_rig) -> None:
    rig = make_rig(_OPENCODE_RUNNER)
    rig.seed_active(_OPENCODE_MODEL, request_id="replay-run", provider_launched=False)
    _cancel_requested(rig, "replay-run")
    assert rig.manager._provider_launch_refusal(_OPENCODE_MODEL) == ""


def test_global_concurrency_limit_is_checked_before_the_provider_limit(
    make_rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(process_launcher.MAX_PROCESSES_ENV, "1")
    rig = make_rig(_OPENCODE_RUNNER)
    rig.seed_active(_OPENCODE_MODEL)

    result = rig.launch("opencode_cli", _OPENCODE_MODEL)
    assert result["blocked_reason"] == "concurrency_limit_reached"

    with pytest.raises(process_launcher.LaunchRejected) as refused:
        with rig.manager._launch_reservation(_reservation("req-global", _OPENCODE_MODEL)):
            pass
    assert str(refused.value) == "concurrency_limit_reached"
    assert rig.spawns == []


def test_env_override_raises_a_limit_and_adds_a_provider(
    make_rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(process_launcher.PROVIDER_LAUNCH_LIMITS_ENV, "opencode-go=2,xai=1")
    rig = make_rig(_OPENCODE_RUNNER)

    rig.seed_active(_OPENCODE_MODEL, request_id="run-1")
    assert rig.manager._provider_launch_refusal(_OPENCODE_MODEL) == ""
    rig.seed_active(_OPENCODE_MODEL, request_id="run-2")
    assert (
        rig.manager._provider_launch_refusal(_OPENCODE_MODEL)
        == "provider_launch_limit_reached:opencode-go:2"
    )

    assert rig.manager._provider_launch_refusal("xai/grok-4.6") == ""
    rig.seed_active("xai/grok-4.6", request_id="run-xai")
    assert (
        rig.manager._provider_launch_refusal("XAI/grok-4.6")
        == "provider_launch_limit_reached:xai:1"
    )


def test_default_limit_is_one_opencode_go_request() -> None:
    assert process_launcher._PROVIDER_LAUNCH_LIMITS_DEFAULT == {"opencode-go": 1}
    limits = process_launcher._provider_launch_limits()
    assert limits == {"opencode-go": 1}
    limits["opencode-go"] = 99
    assert process_launcher._provider_launch_limits() == {"opencode-go": 1}


def test_env_override_parses_valid_entries_and_ignores_junk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        process_launcher.PROVIDER_LAUNCH_LIMITS_ENV,
        "opencode-go=3, XAI=2 ,bad,=4,foo=0,bar=-1,baz=x,qux=1.5",
    )

    assert process_launcher._provider_launch_limits() == {"opencode-go": 3, "xai": 2}


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        ",,,",
        "opencode-go",
        "opencode-go=",
        "opencode-go=0",
        "opencode-go=-2",
        "opencode-go=two",
        "opencode-go=1.0",
        "xai=٢",
        "xai=10000",
        "=1",
        "a/b=2",
        ("x" * 65) + "=2",
    ],
)
def test_malformed_or_non_positive_override_entries_leave_the_defaults(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(process_launcher.PROVIDER_LAUNCH_LIMITS_ENV, raw)

    assert process_launcher._provider_launch_limits() == {"opencode-go": 1}


def test_override_entries_past_the_bound_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    junk = ["junk"] * process_launcher._PROVIDER_LAUNCH_LIMITS_MAX_ENTRIES
    monkeypatch.setenv(
        process_launcher.PROVIDER_LAUNCH_LIMITS_ENV, ",".join([*junk, "xai=2"])
    )

    assert process_launcher._provider_launch_limits() == {"opencode-go": 1}


@pytest.mark.parametrize(
    ("model", "key"),
    [
        ("opencode-go/grok-4.6", "opencode-go"),
        ("OpenCode-Go/Grok", "opencode-go"),
        ("xai/grok-4.6", "xai"),
        ("omniroute/opencode-go/grok-4.6", "omniroute"),
        ("claude-sonnet-5", None),
        ("opencode-go", None),
        ("/grok", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_provider_key_is_the_lowercase_prefix_before_the_first_slash(
    model: object, key: str | None
) -> None:
    assert process_launcher._provider_launch_key(model) == key


def test_launch_rejected_str_is_unchanged_by_the_diagnostic() -> None:
    rejected = process_launcher.LaunchRejected("x", diagnostic={"a": 1})

    assert str(rejected) == "x"
    assert rejected.args == ("x",)
    assert rejected.diagnostic == {"a": 1}
    assert process_launcher.LaunchRejected("x").diagnostic == {}
    assert process_launcher.LaunchRejected("x", diagnostic=None).diagnostic == {}


def test_launch_rejected_keeps_its_own_copy_of_the_diagnostic() -> None:
    source = {"a": 1}
    rejected = process_launcher.LaunchRejected("x", diagnostic=source)
    source["a"] = 2

    assert rejected.diagnostic == {"a": 1}


# --- vscode_lm stale-host diagnostic -------------------------------------------------


def test_stale_vscode_lm_host_refusal_carries_the_stale_host_diagnostic(
    make_rig, bridge_dir: Path, aged_clock: None
) -> None:
    _publish_host(bridge_dir, "window-stale-1", ["gpt-5.1", "claude-sonnet-5"])
    rig = make_rig(_COPILOT_RUNNER)

    result = rig.launch(runtime_adapters.VSCODE_LM_ADAPTER, "gpt-5.1")

    assert result["ok"] is False
    assert result["blocked_reason"] == _STALE_REASON
    diagnostic = result["diagnostic"]
    assert diagnostic["blocker_reason"] == "vscode_lm_host_stale"
    [stale] = diagnostic["stale_hosts"]
    assert stale["window_id"] == "window-stale-1"
    assert stale["age_seconds"] == pytest.approx(_STALE_AGE_SECONDS, abs=60)
    assert stale["models"] == ["gpt-5.1", "claude-sonnet-5"]
    assert diagnostic["live_host_count"] == 0
    assert diagnostic["observed_models"] == []
    assert diagnostic["freshest_age_seconds"] == stale["age_seconds"]

    assert rig.spawns == []
    assert rig.states(result["request_id"]) == ["blocked"]
    blocked_row = next(
        row for row in rig.manager._events() if row["request_id"] == result["request_id"]
    )
    assert blocked_row["blocked_reason"] == _STALE_REASON
    assert blocked_row["diagnostic"]["stale_hosts"][0]["window_id"] == "window-stale-1"
    rig.assert_card_unclaimed()


@pytest.mark.parametrize(
    ("adapter_id", "model", "reason"),
    [
        (
            runtime_adapters.VSCODE_LM_ADAPTER,
            "gpt-5.1",
            "vscode_lm_unavailable:vscode_lm_host_stale",
        ),
        (
            runtime_adapters.DEEPSEEK_VSCODE_LM_ADAPTER,
            None,
            "deepseek_vscode_lm_unavailable:vscode_lm_host_stale",
        ),
        (
            runtime_adapters.GLM_VSCODE_LM_ADAPTER,
            None,
            "glm_vscode_lm_unavailable:vscode_lm_host_stale",
        ),
    ],
)
def test_every_readiness_refusal_keeps_its_reason_and_names_the_stale_host(
    make_rig,
    bridge_dir: Path,
    aged_clock: None,
    adapter_id: str,
    model: str | None,
    reason: str,
) -> None:
    _publish_host(bridge_dir, "window-stale-1", ["gpt-5.1"])
    rig = make_rig(_COPILOT_RUNNER)

    with pytest.raises(process_launcher.LaunchRejected) as refused:
        rig.manager._resolve_provider_env(adapter_id, model)

    assert str(refused.value) == reason
    diagnostic = refused.value.diagnostic
    assert diagnostic["blocker_reason"] == "vscode_lm_host_stale"
    assert [host["window_id"] for host in diagnostic["stale_hosts"]] == ["window-stale-1"]
    assert diagnostic["stale_hosts"][0]["age_seconds"] == pytest.approx(
        _STALE_AGE_SECONDS, abs=60
    )
    assert diagnostic["live_host_count"] == 0


def test_missing_host_refusal_has_no_stale_hosts(make_rig, bridge_dir: Path) -> None:
    rig = make_rig(_COPILOT_RUNNER)

    with pytest.raises(process_launcher.LaunchRejected) as refused:
        rig.manager._resolve_provider_env(runtime_adapters.VSCODE_LM_ADAPTER, "gpt-5.1")

    assert str(refused.value) == "vscode_lm_unavailable:vscode_lm_host_unavailable"
    assert refused.value.diagnostic["blocker_reason"] == "vscode_lm_host_unavailable"
    assert refused.value.diagnostic["stale_hosts"] == []


def test_bridge_readiness_lists_a_stale_host_with_window_id_and_age(
    tmp_path: Path, bridge_dir: Path
) -> None:
    _publish_host(bridge_dir, "window-stale-1", ["gpt-5.1", "claude-sonnet-5"])

    readiness = vscode_lm_bridge.bridge_readiness(
        tmp_path,
        model="gpt-5.1",
        adapter_id=runtime_adapters.VSCODE_LM_ADAPTER,
        now=time.time() + _STALE_AGE_SECONDS,
    )

    assert readiness["launchable"] is False
    assert readiness["blocker_reason"] == "vscode_lm_host_stale"
    assert readiness["stale_host_count"] == 1
    [stale] = readiness["stale_hosts"]
    assert set(stale) == {"window_id", "age_seconds", "models"}
    assert stale["window_id"] == "window-stale-1"
    assert vscode_lm_bridge.HOST_TTL_SECONDS < stale["age_seconds"]
    assert stale["age_seconds"] == pytest.approx(_STALE_AGE_SECONDS, abs=60)
    assert stale["models"] == ["gpt-5.1", "claude-sonnet-5"]


def test_bridge_readiness_lists_no_stale_host_for_a_live_or_absent_host(
    tmp_path: Path, bridge_dir: Path
) -> None:
    absent = vscode_lm_bridge.bridge_readiness(
        tmp_path,
        model="gpt-5.1",
        adapter_id=runtime_adapters.VSCODE_LM_ADAPTER,
    )
    assert absent["blocker_reason"] == "vscode_lm_host_unavailable"
    assert absent["stale_hosts"] == []
    assert absent["stale_host_count"] == 0

    _publish_host(bridge_dir, "window-live-1", ["gpt-5.1"])
    live = vscode_lm_bridge.bridge_readiness(
        tmp_path,
        model="gpt-5.1",
        adapter_id=runtime_adapters.VSCODE_LM_ADAPTER,
        now=time.time() + 5,
    )
    assert live["launchable"] is True
    assert live["blocker_reason"] == ""
    assert live["stale_hosts"] == []


def test_bridge_readiness_bounds_the_stale_host_listing(
    tmp_path: Path, bridge_dir: Path
) -> None:
    many_models = [f"model-{index}" for index in range(40)]
    for index in range(10):
        _publish_host(bridge_dir, f"window-{index}", many_models)

    readiness = vscode_lm_bridge.bridge_readiness(
        tmp_path,
        model="gpt-5.1",
        adapter_id=runtime_adapters.VSCODE_LM_ADAPTER,
        now=time.time() + _STALE_AGE_SECONDS,
    )

    assert readiness["stale_host_count"] == 10
    assert len(readiness["stale_hosts"]) == 8
    for stale in readiness["stale_hosts"]:
        assert len(stale["models"]) == 16


def test_bridge_readiness_truncates_an_oversized_window_id_and_model_name(
    tmp_path: Path, bridge_dir: Path
) -> None:
    _publish_host(bridge_dir, "window-long", ["m" * 300], window_id="w" * 300)

    readiness = vscode_lm_bridge.bridge_readiness(
        tmp_path,
        model="gpt-5.1",
        adapter_id=runtime_adapters.VSCODE_LM_ADAPTER,
        now=time.time() + _STALE_AGE_SECONDS,
    )

    [stale] = readiness["stale_hosts"]
    assert stale["window_id"] == "w" * 128
    assert stale["models"] == ["m" * 128]
