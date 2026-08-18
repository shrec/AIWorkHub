"""Launch truth: a launch that cannot proceed fails at launch with the reason,
the configured concurrency cap is authoritative, and provider readiness reports
what is actually observable (NF-2026-00265, NF-2026-00262, NF-2026-00270,
NF-2026-00238).
"""

from __future__ import annotations

from pathlib import Path

from aiworkhub import quality_review, repo_policy, runtime_adapters


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


def test_two_cards_same_runner_not_serialised_when_capacity_allows() -> None:
    env = {"AIWORKHUB_MAX_PROCESSES": "4"}
    first = repo_policy.workforce_admission(
        running_total=1, runner="claude_opus-5", env=env
    )
    second = repo_policy.workforce_admission(
        running_total=2, runner="claude_opus-5", env=env
    )
    assert first["admit"] is True
    assert second["admit"] is True
    # The cap that applied is reported, and it is not a per-runner cap of 1.
    assert first["effective_cap"] == 4
    assert first["serialised_by_runner"] is False
    assert second["serialised_by_runner"] is False


def test_configured_cap_admits_beyond_the_default_ceiling() -> None:
    # Would be rejected under a fixed default of 4; the configured 8 is the cap.
    verdict = repo_policy.workforce_admission(
        running_total=5, runner="r", env={"AIWORKHUB_MAX_PROCESSES": "8"}
    )
    assert verdict["admit"] is True
    assert verdict["effective_cap"] == 8


def test_workforce_admission_rejects_only_at_the_configured_cap() -> None:
    verdict = repo_policy.workforce_admission(
        running_total=4, runner="r", env={"AIWORKHUB_MAX_PROCESSES": "4"}
    )
    assert verdict["admit"] is False
    assert verdict["reason"].startswith("workforce_cap_reached")


def test_unset_max_processes_falls_back_with_named_reason() -> None:
    resolved = repo_policy.resolve_workforce_cap({})
    assert resolved["effective_cap"] == repo_policy.DEFAULT_MAX_PROCESSES
    assert resolved["configured"] is False
    assert resolved["reason"] == "max_processes_unset_default_applied"


# --------------------------------------------------------------------------
# NF-2026-00270 / NF-2026-00238: provider readiness reports what is observable
# for each configured adapter, names why quota is not observable rather than a
# blanket unknown, and states glm_vscode_lm reachability with evidence.
# --------------------------------------------------------------------------
def test_observability_report_covers_every_configured_adapter(tmp_path: Path) -> None:
    root = _initialized_root(tmp_path)
    report = repo_policy.provider_observability_report(root)
    ids = {item["adapter_id"] for item in report["adapters"]}
    assert ids == set(runtime_adapters.LOCAL_ADAPTERS)


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
