"""Adapter readiness truth (NF-2026-00270).

Every launchable adapter used to report ``ready_unverified`` -- always, for
every route -- so the status carried no information: it could not distinguish an
adapter that will work from one whose quota is gone. Quota is never observed for
any route today, which is exactly why an HTTP 401 (NF-2026-00326) could not be
classified either.

These regressions pin the honest behaviour on the two production seams:

* ``repo_policy._provider_status`` reports ``ready`` ONLY when the provider quota
  was actually observed; a launchable, access-observed route whose quota is
  unobserved is ``ready_unverified`` and carries the reason WHY, never a bare
  universal "ready".
* ``repo_policy.provider_observability_report`` lists which routes can never be
  verified here and names, per route family, why their quota is unobservable --
  so a universal ``ready_unverified`` is backed by an explicit reason rather than
  presented as if it were a signal.

No price table and no cost estimation is involved; readiness is a quota-observed
axis, and an unobserved quota stays unobserved rather than being reported ready.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from aiworkhub import repo_policy, runtime_adapters


def _ok_resolution() -> runtime_adapters.ExecutableResolution:
    return runtime_adapters.ExecutableResolution(
        adapter_id="claude_cli",
        executable="/usr/bin/claude",
        ok=True,
        reason="",
    )


_POLICY = {"providers": {"allowed_adapters": ["claude_cli"]}}


def test_route_is_ready_unverified_until_quota_is_actually_observed(monkeypatch) -> None:
    # A launchable, access-observed claude_cli route whose provider quota was
    # NOT observed. The status must be ready_unverified, not "ready", and it must
    # carry the reason quota was not observed -- a status every adapter always
    # has is not a signal, so the honest value names its own limitation.
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id, *a, **k: _ok_resolution(),
    )
    monkeypatch.setattr(
        repo_policy.claude_auth,
        "auth_status",
        lambda executable: {
            "launchable": True,
            "authenticated": True,
            "access_observed": True,
            "quota_observed": False,
            "quota_state": repo_policy.QUOTA_STATE_UNAVAILABLE,
        },
    )

    result = repo_policy._provider_status(
        Path("."), "claude_cli", _POLICY, "landlock", ""
    )

    assert result["launchable"] is True
    assert result["access_observed"] is True
    assert result["quota_observed"] is False
    assert result["status"] == repo_policy.READINESS_READY_UNVERIFIED
    assert result["status"] != repo_policy.READINESS_READY
    # The reason names WHY it is unverified -- the unobserved quota -- so the
    # value is not a blanket universal.
    assert result["reason"] == f"quota_unobserved:{repo_policy.QUOTA_STATE_UNAVAILABLE}"


def test_route_is_ready_only_when_quota_was_observed(monkeypatch) -> None:
    # The mirror case: the same launchable, access-observed route reports "ready"
    # ONLY once the provider quota is actually observed. This is what makes
    # ready_unverified informative rather than universal -- "ready" is reachable,
    # but exclusively through an observed quota.
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id, *a, **k: _ok_resolution(),
    )
    monkeypatch.setattr(
        repo_policy.claude_auth,
        "auth_status",
        lambda executable: {
            "launchable": True,
            "authenticated": True,
            "access_observed": True,
            "quota_observed": True,
            "quota_state": "observed_from_provider_api",
        },
    )

    result = repo_policy._provider_status(
        Path("."), "claude_cli", _POLICY, "landlock", ""
    )

    assert result["quota_observed"] is True
    assert result["status"] == repo_policy.READINESS_READY


def test_observability_report_lists_which_routes_can_never_be_verified_and_why(
    tmp_path: Path,
) -> None:
    # The report names, for every configured local adapter, that its quota is not
    # observable here and the per-family reason why -- so the universal
    # ready_unverified is backed by an explicit cause, route by route, instead of
    # a single blanket unknown.
    report = repo_policy.provider_observability_report(tmp_path)
    adapters = report["adapters"]

    # Every launchable/local adapter is accounted for.
    assert {item["adapter_id"] for item in adapters} == set(
        runtime_adapters.LOCAL_ADAPTERS
    )
    # Not one route can have its quota verified here today...
    assert report["quota_observable_any"] is False
    for item in adapters:
        # ...and each says so by name, never leaving a silent gap.
        assert item["quota_observable"] is False
        assert item["quota_observability_reason"]
        # The reachability evidence is a real observation, not a bare unknown.
        assert item["reachability_evidence"]


def test_each_route_family_names_a_distinct_quota_reason() -> None:
    # The "why" is route-family specific, not a single copied string: an editor
    # route, a Copilot BYOK route and the Claude CLI cannot observe quota for
    # different, individually-named reasons.
    reasons = {
        repo_policy.describe_provider_observability(Path("."), adapter)[
            "quota_observability_reason"
        ]
        for adapter in ("vscode_lm", "deepseek_copilot_cli", "claude_cli", "codex_cli")
    }
    # Four families -> four distinct named reasons.
    assert len(reasons) == 4
    for reason in reasons:
        assert reason and reason != repo_policy.QUOTA_STATE_UNAVAILABLE


def test_provider_observability_is_wired_into_the_preflight_surface(
    monkeypatch, tmp_path: Path
) -> None:
    # The report is only a signal if a surface emits it. build_preflight -- the
    # environment preflight that reports route readiness -- must carry the
    # per-route observability report, so ready_unverified is never presented as a
    # bare universal without the named reason each route can never be verified.
    root = tmp_path / "repo"
    (root / ".aiworkhub/config").mkdir(parents=True)
    (root / ".aiworkhub/project.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        repo_policy.task_store,
        "storage_readiness",
        lambda _root: SimpleNamespace(ready=True, reason="ready", repo_id="repo_test"),
    )
    monkeypatch.setattr(
        repo_policy.source_graph_daemon,
        "daemon_health",
        lambda _root: {"ok": True, "readable_generation": True},
    )
    monkeypatch.setattr(
        repo_policy.task_store,
        "callback_bridge_health",
        lambda _root: {"ok": True},
    )
    monkeypatch.setattr(
        repo_policy.worker_workspace, "select_sandbox_backend", lambda: "landlock"
    )
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id, *a, **k: _ok_resolution(),
    )

    report = repo_policy.build_preflight(root)

    assert "provider_observability" in report
    observability = report["provider_observability"]
    # Every local route is named in the surface, and none can be verified here...
    assert {item["adapter_id"] for item in observability["adapters"]} == set(
        runtime_adapters.LOCAL_ADAPTERS
    )
    assert observability["quota_observable_any"] is False
    # ...each with its own named reason -- the surface is not a blanket unknown.
    for item in observability["adapters"]:
        assert item["quota_observability_reason"]
