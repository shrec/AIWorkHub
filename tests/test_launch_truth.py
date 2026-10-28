"""Launch truth: a launch that cannot proceed fails at launch with the reason,
the configured concurrency cap is authoritative, and provider readiness reports
what is actually observable (NF-2026-00265, NF-2026-00262, NF-2026-00270,
NF-2026-00238).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import process_launcher, quality_review, repo_policy, runtime_adapters


def _initialized_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".aiworkhub/config").mkdir(parents=True)
    (root / ".aiworkhub/project.json").write_text("{}\n", encoding="utf-8")
    return root


# --------------------------------------------------------------------------
# NF-2026-00265: a reviewer launch against a non-review_ready target must fail
# at launch with the reason, not return ok and silently never start.
# --------------------------------------------------------------------------
def test_reviewer_launch_against_review_ready_target_may_launch() -> None:
    verdict = quality_review.assess_reviewer_launch_target(
        target_card={"terminal_review": {"substatus": "review_ready"}}
    )
    assert verdict["can_launch"] is True
    assert verdict["fails_at_launch"] is False


def test_reviewer_launch_against_non_review_ready_target_fails_at_launch() -> None:
    verdict = quality_review.assess_reviewer_launch_target(
        target_card={"terminal_review": {"substatus": "worker_failed"}}
    )
    assert verdict["can_launch"] is False
    assert verdict["fails_at_launch"] is True
    assert verdict["reason"] == "reviewer_target_not_review_ready:worker_failed"


def test_reviewer_launch_target_missing_terminal_review_fails_named() -> None:
    verdict = quality_review.assess_reviewer_launch_target(target_card={})
    assert verdict["can_launch"] is False
    assert verdict["reason"] == "reviewer_target_not_review_ready:<none>"


# --------------------------------------------------------------------------
# NF-2026-00262: AIWORKHUB_MAX_PROCESSES is authoritative and the effective cap
# is reported; two cards with the same runner are not serialised.
# --------------------------------------------------------------------------
def test_configured_max_processes_is_the_effective_cap() -> None:
    resolved = repo_policy.resolve_workforce_cap({"AIWORKHUB_MAX_PROCESSES": "4"})
    assert resolved["effective_cap"] == 4
    assert resolved["configured"] is True
    assert resolved["source"] == "env"

    # A different configured value is authoritative, not a fixed default.
    assert (
        repo_policy.resolve_workforce_cap({"AIWORKHUB_MAX_PROCESSES": "8"})[
            "effective_cap"
        ]
        == 8
    )


def _launcher(tmp_path: Path) -> process_launcher.ProcessManager:
    return process_launcher.ProcessManager(
        repo=tmp_path,
        process_log_path=tmp_path / "processes.jsonl",
        process_dir=tmp_path / "processes",
        isolation_enabled=False,
    )


def _launch(task_id: str, runner: str = "claude_opus-5") -> dict[str, str]:
    # No ``provider/model`` prefix, so the provider limit never applies and only
    # the workforce cap can refuse the reservation.
    return {
        "request_id": f"req-{task_id}",
        "task_id": task_id,
        "runner": runner,
        "topic": "code",
        "adapter_id": "deepseek_vscode_lm",
    }


def _reserve(launcher: process_launcher.ProcessManager, task_id: str, **kw: str) -> None:
    # The ``starting`` row outlives the block, so the slot stays held after it.
    with launcher._launch_reservation(_launch(task_id, **kw)):
        pass


def test_two_cards_same_runner_not_serialised_when_capacity_allows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIWORKHUB_MAX_PROCESSES", "4")
    launcher = _launcher(tmp_path)
    with launcher._launch_reservation(_launch("CARD_A")):
        # The second card shares the first one's runner while it is live.
        with launcher._launch_reservation(_launch("CARD_B")):
            assert launcher._active_count() == 2
    latest = launcher._latest_by_request()
    for request_id in ("req-CARD_A", "req-CARD_B"):
        assert latest[request_id]["state"] == "starting"
        assert latest[request_id]["runner"] == "claude_opus-5"
    assert process_launcher._configured_limit() == 4


def test_configured_cap_admits_beyond_the_default_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fifth and sixth launches would be refused under a fixed cap of 4.
    monkeypatch.setenv("AIWORKHUB_MAX_PROCESSES", "8")
    launcher = _launcher(tmp_path)
    for index in range(6):
        _reserve(launcher, f"CARD_{index}")
    assert launcher._active_count() == 6


def test_launcher_refuses_only_at_the_configured_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIWORKHUB_MAX_PROCESSES", "3")
    launcher = _launcher(tmp_path)
    # The third same-runner card is admitted with two running: below the cap.
    for index in range(3):
        _reserve(launcher, f"CARD_{index}")
    assert launcher._active_count() == 3
    with pytest.raises(process_launcher.LaunchRejected, match="concurrency_limit_reached"):
        _reserve(launcher, "CARD_OVER")
    # A different runner is refused the same way: the refusal is the count.
    with pytest.raises(process_launcher.LaunchRejected, match="concurrency_limit_reached"):
        _reserve(launcher, "CARD_OTHER", runner="other_runner")
    assert launcher._active_count() == 3
    assert "req-CARD_OVER" not in launcher._latest_by_request()


def test_unset_max_processes_falls_back_with_named_reason(monkeypatch) -> None:
    monkeypatch.setattr(repo_policy.os, "cpu_count", lambda: 8)
    resolved = repo_policy.resolve_workforce_cap({})
    assert resolved["effective_cap"] == repo_policy.default_max_processes() == 6
    assert resolved["configured"] is False
    assert resolved["reason"] == "max_processes_unset_core_derived:8"


def test_unset_branch_reports_the_core_count_its_cap_came_from(monkeypatch) -> None:
    samples = iter((8, 16))
    monkeypatch.setattr(repo_policy.os, "cpu_count", lambda: next(samples))
    resolved = repo_policy.resolve_workforce_cap({})
    assert resolved["effective_cap"] == 6
    assert resolved["reason"] == "max_processes_unset_core_derived:8"


def test_core_derived_default_tracks_observed_core_count(monkeypatch) -> None:
    cases = {
        16: 14,
        2: 1,
        1: 1,
        None: 1,
        1000: repo_policy.MAX_PROCESSES_CEILING,
    }
    for cores, expected in cases.items():
        monkeypatch.setattr(repo_policy.os, "cpu_count", lambda cores=cores: cores)
        assert repo_policy.default_max_processes() == expected
        resolved = repo_policy.resolve_workforce_cap({})
        assert resolved["effective_cap"] == expected
        assert resolved["reason"] == f"max_processes_unset_core_derived:{cores or 1}"


def test_core_derived_default_never_reaches_zero(monkeypatch) -> None:
    monkeypatch.setattr(repo_policy.os, "cpu_count", lambda: 0)
    assert repo_policy.default_max_processes() == 1


def test_core_derived_default_applies_to_invalid_value(monkeypatch) -> None:
    monkeypatch.setattr(repo_policy.os, "cpu_count", lambda: 16)
    resolved = repo_policy.resolve_workforce_cap({"AIWORKHUB_MAX_PROCESSES": "many"})
    assert resolved["effective_cap"] == 14
    assert resolved["configured"] is False
    assert resolved["reason"].startswith("max_processes_invalid_default_applied:")


def test_core_derived_default_is_overridden_by_explicit_env(monkeypatch) -> None:
    for cores in (2, 16, None, 1000):
        monkeypatch.setattr(repo_policy.os, "cpu_count", lambda cores=cores: cores)
        resolved = repo_policy.resolve_workforce_cap({"AIWORKHUB_MAX_PROCESSES": "8"})
        assert resolved["effective_cap"] == 8
        assert resolved["configured"] is True
        assert resolved["reason"] == "max_processes_configured"


def test_launcher_admits_by_the_reported_workforce_cap(monkeypatch) -> None:
    # NF-2026-01359: admission and the reported cap come from one resolver.
    monkeypatch.setenv("AIWORKHUB_MAX_PROCESSES", "64")
    resolved = repo_policy.resolve_workforce_cap()
    assert process_launcher._configured_limit() == resolved["effective_cap"] == 32
    assert resolved["reason"] == "max_processes_above_ceiling_clamped:64->32"
    monkeypatch.delenv("AIWORKHUB_MAX_PROCESSES")
    assert process_launcher._configured_limit() == repo_policy.default_max_processes()


# --------------------------------------------------------------------------
# NF-2026-00270 / NF-2026-00238: provider readiness reports what is observable
# for each configured adapter, names why quota is not observable rather than a
# blanket unknown, and states glm_vscode_lm reachability with evidence.
# --------------------------------------------------------------------------
def test_observability_report_covers_every_configured_adapter(tmp_path: Path) -> None:
    root = _initialized_root(tmp_path)
    report = repo_policy.provider_observability_report(root)
    ids = {item["adapter_id"] for item in report["adapters"]}
    assert ids == set(repo_policy.DEFAULT_POLICY["providers"]["allowed_adapters"])


def test_quota_unobservability_is_named_per_adapter_not_a_blanket_unknown(
    tmp_path: Path,
) -> None:
    root = _initialized_root(tmp_path)
    report = repo_policy.provider_observability_report(root)
    for item in report["adapters"]:
        # Each adapter names what IS observable...
        assert item["observable_signals"], item["adapter_id"]
        # ...and names why quota is not, rather than returning "unknown".
        reason = item["quota_observability_reason"]
        assert reason
        assert reason != "unknown"
        assert reason != "ready_unknown"
    # The reasons are adapter-family specific, not one opaque value repeated.
    distinct_reasons = {
        item["quota_observability_reason"] for item in report["adapters"]
    }
    assert len(distinct_reasons) >= 4
    assert report["quota_observable_any"] is False


def test_glm_vscode_lm_reachability_is_stated_with_evidence(tmp_path: Path) -> None:
    root = _initialized_root(tmp_path)
    entry = repo_policy.describe_provider_observability(root, "glm_vscode_lm")
    assert entry["adapter_id"] == "glm_vscode_lm"
    assert entry["route_family"] == "editor_hosted_vscode_lm"
    # Reachability is stated as a concrete boolean with non-empty evidence,
    # established before any claim about the glm_vscode_lm launch path.
    assert isinstance(entry["reachable"], bool)
    assert entry["reachability_evidence"]
