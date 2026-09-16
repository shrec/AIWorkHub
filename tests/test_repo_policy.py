from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import repo_policy, runtime_adapters, workforce_catalog


def _initialized_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".aiworkhub/config").mkdir(parents=True)
    (root / ".aiworkhub/project.json").write_text("{}\n", encoding="utf-8")
    return root


def _native_cli_sandbox_backend() -> str:
    """A sandbox_backend value that does NOT itself fail-close a native CLI.

    ``_provider_status`` deliberately marks a native (non-VS-Code-LM) CLI
    adapter unlaunchable on Windows unless ``sandbox_backend`` is exactly
    ``WINDOWS_APPCONTAINER_BACKEND`` -- "bubblewrap" (a POSIX-only sandbox)
    would trip that gate on Windows and mask whatever the test actually
    means to exercise.
    """

    if os.name == "nt":
        return repo_policy.WINDOWS_APPCONTAINER_BACKEND
    return "bubblewrap"


def test_ensure_policy_is_owner_only_idempotent_and_valid(tmp_path: Path) -> None:
    root = _initialized_root(tmp_path)
    path, created = repo_policy.ensure_policy(root)
    assert created is True
    assert path == root / repo_policy.POLICY_RELATIVE_PATH
    policy = repo_policy.load_policy(root)
    assert policy["configured"] is True
    assert policy["tools"]["raw_discovery_forbidden"] == ["grep", "rg", "find", "tree"]
    assert policy["retention"]["worktree_max_bytes"] == 5 * 1024 * 1024 * 1024
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    same, created_again = repo_policy.ensure_policy(root)
    assert same == path
    assert created_again is False


def test_grok_kilo_preflight_uses_local_xai_auth_without_exposing_it(
    monkeypatch, tmp_path: Path
) -> None:
    root = _initialized_root(tmp_path)
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id: SimpleNamespace(ok=True, executable="kilo", reason="ready"),
    )
    monkeypatch.setattr(
        repo_policy.kilo_auth,
        "auth_status",
        lambda **_kwargs: {
            "launchable": True,
            "authenticated": True,
            "credential_present": True,
            "access_observed": True,
            "quota_observed": False,
            "quota_state": "unavailable_from_provider_api",
            "blocker_reason": "",
        },
    )

    status = repo_policy._provider_status(
        root,
        "grok_kilo_cli",
        repo_policy.load_policy(root),
        _native_cli_sandbox_backend(),
        "",
    )

    assert status["launchable"] is True
    assert status["access_observed"] is True
    assert status["status"] == "ready_unverified"
    assert status["policy_provider"] == "xai"
    assert "token" not in json.dumps(status, sort_keys=True).lower()


def test_codex_preflight_uses_exact_secret_free_capability_receipt(
    monkeypatch, tmp_path: Path
) -> None:
    root = _initialized_root(tmp_path)
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id: SimpleNamespace(
            ok=True, executable="/safe/codex", reason="ready"
        ),
    )
    monkeypatch.setattr(
        repo_policy.codex_auth,
        "capability_status",
        lambda _executable=None: {
            "launchable": True,
            "authenticated": True,
            "access_observed": True,
            "observed_models": ["gpt-5.5", "gpt-5.3-codex"],
            "quota_observed": False,
            "quota_state": "unavailable_from_provider_api",
            "blocker_reason": "",
            "cache_hit": False,
            "cache_ttl_seconds": 300.0,
            "model_catalog_complete": True,
            "private_account": "must-not-propagate",
        },
    )

    status = repo_policy._provider_status(
        root,
        "codex_cli",
        repo_policy.load_policy(root),
        _native_cli_sandbox_backend(),
        "",
    )

    assert status["launchable"] is True
    assert status["access_observed"] is True
    assert status["observed_models"] == ["gpt-5.5", "gpt-5.3-codex"]
    assert status["quota_observed"] is False
    assert status["status"] == "ready_unverified"
    assert status["cache_hit"] is False
    assert status["cache_ttl_seconds"] == 300.0
    assert status["model_catalog_complete"] is True
    assert "private_account" not in status
    assert "/safe/codex" not in json.dumps(status, sort_keys=True)


def test_legacy_allow_all_policy_gains_grok_but_custom_denial_does_not(
    tmp_path: Path,
) -> None:
    root = _initialized_root(tmp_path)
    legacy = json.loads(json.dumps(repo_policy.DEFAULT_POLICY))
    legacy["providers"]["allowed_adapters"].remove("grok_kilo_cli")
    path = repo_policy.policy_path(root)
    path.write_text(json.dumps(legacy), encoding="utf-8")

    migrated = repo_policy.load_policy(root)
    assert "grok_kilo_cli" in migrated["providers"]["allowed_adapters"]

    legacy["providers"]["allowed_adapters"].remove("claude_cli")
    path.write_text(json.dumps(legacy), encoding="utf-8")
    customized = repo_policy.load_policy(root)
    assert "grok_kilo_cli" not in customized["providers"]["allowed_adapters"]


def test_legacy_policy_without_worktree_cap_receives_safe_default(tmp_path: Path) -> None:
    root = _initialized_root(tmp_path)
    value = json.loads(json.dumps(repo_policy.DEFAULT_POLICY))
    del value["retention"]["worktree_max_bytes"]
    repo_policy.policy_path(root).write_text(json.dumps(value), encoding="utf-8")
    loaded = repo_policy.load_policy(root)
    assert loaded["retention"]["worktree_max_bytes"] == 5 * 1024 * 1024 * 1024


def test_policy_fails_closed_if_mandatory_discovery_denies_are_removed(tmp_path: Path) -> None:
    root = _initialized_root(tmp_path)
    value = json.loads(json.dumps(repo_policy.DEFAULT_POLICY))
    value["tools"]["raw_discovery_forbidden"] = ["grep"]
    repo_policy.policy_path(root).write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(repo_policy.RepoPolicyError, match="mandatory_raw_discovery_denies_missing"):
        repo_policy.load_policy(root)


def test_launch_policy_requires_source_graph_for_initialized_code_task(tmp_path: Path) -> None:
    root = _initialized_root(tmp_path)
    repo_policy.ensure_policy(root)
    card = {"allowed_writes": ["src/change.py"]}
    blocked = repo_policy.validate_launch(root, card, "claude_cli")
    assert blocked == {
        "ok": False,
        "reason": "repo_policy_source_graph_required_for_code",
    }
    card["project_context"] = {
        "task_type": "code",
        "source_graph": {"required": True},
    }
    assert repo_policy.validate_launch(root, card, "claude_cli")["ok"] is True


def test_launch_policy_enforces_provider_scope_and_required_checks(tmp_path: Path) -> None:
    root = _initialized_root(tmp_path)
    value = json.loads(json.dumps(repo_policy.DEFAULT_POLICY))
    value["providers"]["allowed_adapters"] = ["claude_cli"]
    value["validation"]["required_check_ids"] = ["repo-test"]
    repo_policy.policy_path(root).write_text(json.dumps(value), encoding="utf-8")
    card = {
        "allowed_writes": [],
        "project_context": {"task_type": "research", "source_graph": {"required": False}},
    }
    denied = repo_policy.validate_launch(root, card, "codex_cli")
    assert denied["reason"] == "adapter_denied_by_repo_policy:codex_cli"
    missing = repo_policy.validate_launch(root, card, "claude_cli")
    assert missing["reason"] == "repo_policy_required_checks_missing:repo-test"

    (root / ".aiworkhub/quality.json").write_text(
        json.dumps(
            {
                "checks": [
                    {"id": "repo-test", "kind": "test", "command": ["python", "-m", "pytest"]}
                ]
            }
        ),
        encoding="utf-8",
    )
    assert repo_policy.validate_launch(root, card, "claude_cli")["ok"] is True

    card.update({"callback_required": True, "callback_supported": False})
    callback_blocked = repo_policy.validate_launch(root, card, "claude_cli")
    assert callback_blocked["reason"] == "repo_policy_callback_route_required"


def test_unified_preflight_is_portable_and_truthful_about_unobserved_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _initialized_root(tmp_path)
    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: False)
    monkeypatch.setattr(
        repo_policy.task_store,
        "storage_readiness",
        lambda _root: SimpleNamespace(ready=True, reason="ready", repo_id="repo_test"),
    )
    monkeypatch.setattr(
        repo_policy.source_graph_daemon,
        "daemon_health",
        lambda _root: {
            "ok": True,
            "status": "ready",
                "running": True,
                "registered": True,
                "readable_generation": True,
                "last_success_at": "2026-07-30T14:00:00+00:00",
            "stale_reason": "",
            "build_revision": "aiworkhub.source_graph.semantic.v5",
            "files_seen": 4,
        },
    )
    monkeypatch.setattr(
        repo_policy.worker_workspace, "select_sandbox_backend", lambda: "bubblewrap"
    )
    monkeypatch.setattr(
        repo_policy.task_store,
        "callback_bridge_health",
        lambda _root: {
            "ok": True,
            "backlog_count": 0,
            "retry_count": 0,
            "last_delivered_at": "2026-07-30T14:05:00+00:00",
            "last_dead_letter_at": "",
            "last_dead_letter_error": "",
        },
    )
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id: runtime_adapters.ExecutableResolution(
            adapter_id, "/private/host/bin/model", True, ""
        ),
    )
    monkeypatch.setattr(
        repo_policy.deepseek_credentials,
        "credential_status",
        lambda repo=None: {"launchable": True, "blocker_reason": ""},
    )
    monkeypatch.setattr(
        repo_policy.glm_credentials,
        "credential_status",
        lambda repo=None: {"launchable": True, "blocker_reason": ""},
    )
    monkeypatch.setattr(
        repo_policy.vscode_lm_bridge,
        "bridge_readiness",
        lambda *args, **kwargs: {
            "launchable": True,
            "access_observed": True,
            "access_state": "granted",
            "blocker_reason": "",
            "window_id": "window_test",
            "live_host_count": 1,
            "stale_host_count": 0,
            "observed_models": ["glm-5.2", "deepseek-v4-pro"],
        },
    )
    monkeypatch.setattr(
        repo_policy.claude_auth,
        "auth_status",
        lambda executable=None: {
            "launchable": True,
            "authenticated": True,
            "auth_method": "claude.ai",
            "subscription_type": "max",
            "blocker_reason": "",
        },
    )

    report = repo_policy.build_preflight(root)
    assert report["ok"] is True
    by_adapter = {item["adapter_id"]: item for item in report["providers"]}
    # NF-2026-00264: access being observed is not quota being observed. This
    # fixture grants access and says nothing about quota, so the honest status
    # is ready_unverified — the catalog must not assert "ready" on a provider
    # whose remaining quota it has never seen.
    assert by_adapter["deepseek_copilot_cli"]["status"] == "ready_unverified"
    assert by_adapter["deepseek_copilot_cli"]["quota_observed"] is False
    assert by_adapter["glm_vscode_lm"]["access_observed"] is True
    assert by_adapter["vscode_lm"]["status"] == "ready_unverified"
    assert by_adapter["vscode_lm"]["observed_models"] == [
        "glm-5.2",
        "deepseek-v4-pro",
    ]
    assert by_adapter["claude_cli"]["status"] == "ready_unverified"
    assert by_adapter["claude_cli"]["access_observed"] is True
    assert by_adapter["claude_cli"]["sandbox_backend"] == "bubblewrap"
    assert by_adapter["vscode_lm"]["sandbox_backend"] == "vscode_lm_in_process"
    assert report["sandbox"]["backend"] == "bubblewrap"
    assert report["sandbox"]["enforceable"] is True
    assert report["sandbox"]["reason"] == ""
    assert report["sandbox"]["native_cli_backend"] == "bubblewrap"
    assert report["sandbox"]["route_aware"] is True
    assert report["source_graph"]["ready_for_code"] is True
    assert report["callback"]["backlog_count"] == 0
    serialized = json.dumps(report, sort_keys=True)
    assert "/private/host" not in serialized
    assert "executable" not in serialized


def test_preflight_filters_disabled_observed_models_without_hiding_reachability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _initialized_root(tmp_path)
    model_settings_state = {
        "providers": {},
        "adapters": {},
        "models": {"copilot": {"vscode_lm": {"gpt-5.5": False}}},
    }
    monkeypatch.setattr(
        repo_policy.model_settings,
        "load",
        lambda _root: model_settings_state,
    )
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id: runtime_adapters.ExecutableResolution(
            adapter_id, "codex", True, ""
        ),
    )
    monkeypatch.setattr(
        repo_policy.vscode_lm_bridge,
        "bridge_readiness",
        lambda *args, **kwargs: {
            "launchable": True,
            "access_observed": True,
            "observed_models": ["gpt-5.5", "gpt-5.3-codex"],
            "model_catalog_complete": True,
        },
    )

    status = repo_policy._provider_status(
        root,
        "vscode_lm",
        repo_policy.load_policy(root),
        "bubblewrap",
        "",
    )

    assert status["provider_observed_models"] == ["gpt-5.5", "gpt-5.3-codex"]
    assert status["observed_models"] == ["gpt-5.3-codex"]
    assert status["observed_models_excluded_by_repository_model_policy"] == 1
    assert status["access_observed"] is True
    assert status["launchable"] is True


def _reconciler_ready_preflight_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Everything except the reconciler is healthy, so it alone moves status."""

    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: False)
    monkeypatch.setattr(
        repo_policy.task_store,
        "storage_readiness",
        lambda _root: SimpleNamespace(ready=True, reason="ready", repo_id="repo_test"),
    )
    monkeypatch.setattr(
        repo_policy.task_store, "callback_bridge_health", lambda _root: {"ok": True}
    )
    monkeypatch.setattr(
        repo_policy.workspace_hygiene,
        "inventory",
        lambda _root, refresh_sizes=False: {},
    )
    monkeypatch.setattr(
        repo_policy.source_graph_daemon,
        "daemon_health",
        lambda _root: {
            "ok": True,
            "status": "ready",
            "running": True,
            "registered": True,
            "readable_generation": 7,
            "last_success_at": "2026-09-07T00:00:00Z",
            "build_revision": "rev",
            "files_seen": 12,
            "index_age_seconds": 1,
            "stale_after_seconds": 600,
        },
    )


def _preflight_with_reconciler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, health: dict, name: str = "a"
) -> dict:
    from aiworkhub import task_reconciler

    root = _initialized_root(tmp_path / name)
    _reconciler_ready_preflight_deps(monkeypatch)
    monkeypatch.setattr(task_reconciler, "reconciler_health", lambda _root: health)
    return repo_policy.build_preflight(root)


def test_preflight_blocks_when_reconciler_authority_acquisition_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A green aggregate over a dead reconciler is the defect this prevents.

    Field report, AIWorkHub 0.10.95 on Windows: ``authority_state:
    acquisition_failed``, ``active_owner: false``, ``standby: false``,
    ``durable_status_present: false`` -- and the overall preflight still said
    ``ready``, because it never consulted the reconciler at all.
    """

    report = _preflight_with_reconciler(
        monkeypatch,
        tmp_path,
        {
            "ok": False,
            "running": True,
            "authority_state": "acquisition_failed",
            "active_owner": False,
            "standby": False,
            "acquisition_attempts": 230,
            "acquisition_backoff_seconds": 4.0,
            "last_acquisition_error": "reconciler_lock_unsafe:D:\\Dev\\x\\locks",
            "durable_status_present": False,
            "durable_scan_stale": True,
        },
    )

    assert report["status"] == "blocked"
    assert report["ok"] is False
    assert "worker_reconciler_authority_failed" in report["errors"]
    assert report["reconciler"]["status"] == "blocked"
    assert report["reconciler"]["acquisition_attempts"] == 230
    assert report["reconciler"]["acquisition_backoff_seconds"] == 4.0
    assert "reconciler_lock_unsafe" in report["reconciler"]["last_acquisition_error"]


def test_preflight_degrades_when_the_reconciler_was_never_measured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absence of evidence is not evidence of health."""

    report = _preflight_with_reconciler(
        monkeypatch,
        tmp_path,
        {"ok": False, "running": False, "durable_status_present": False},
    )

    assert report["status"] == "degraded"
    assert "worker_reconciler_unmeasured" in report["warnings"]
    assert report["reconciler"]["status"] == "not_measured"
    # Unmeasured is not an error: it withholds a verdict, it does not invent one.
    assert "worker_reconciler_authority_failed" not in report["errors"]


def test_preflight_degrades_on_a_stale_measured_reconciler_and_is_ready_when_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stale = _preflight_with_reconciler(
        monkeypatch,
        tmp_path,
        {
            "ok": False,
            "running": False,
            "durable_status_present": True,
            "durable_scan_stale": True,
            "authority_state": "active_owner",
        },
    )
    assert stale["status"] == "degraded"
    assert "worker_reconciler_degraded" in stale["warnings"]

    live = _preflight_with_reconciler(
        monkeypatch,
        tmp_path,
        {
            "ok": True,
            "running": True,
            "authority_state": "active_owner",
            "active_owner": True,
        },
        name="live",
    )
    # This fixture's provider routes are independently degraded, so assert the
    # reconciler's own contribution rather than the whole aggregate; the
    # end-to-end "ready" transition is covered by the full-coverage fixtures in
    # tests/test_aiworkhub_preflight_truth_b1461.py.
    assert live["reconciler"]["status"] == "ready"
    assert "worker_reconciler_degraded" not in live["warnings"]
    assert "worker_reconciler_unmeasured" not in live["warnings"]
    assert "worker_reconciler_authority_failed" not in live["errors"]


def test_preflight_carries_a_reduced_lock_guarantee_instead_of_flattening_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A host that could only grant a weaker lock must say so in the report."""

    report = _preflight_with_reconciler(
        monkeypatch,
        tmp_path,
        {
            "ok": True,
            "running": True,
            "authority_state": "active_owner",
            "active_owner": True,
            "parent_authority_backend": "none",
            "reduced_guarantees": ["lock_parent_not_pinned_to_a_descriptor"],
        },
    )

    assert "worker_reconciler_degraded" not in report["warnings"]
    assert report["reconciler"]["parent_authority_backend"] == "none"
    assert report["reconciler"]["reduced_guarantees"] == [
        "lock_parent_not_pinned_to_a_descriptor"
    ]


def _windows_native_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    sandbox_error: str,
    sandbox_backend: str = "",
    installed: bool = True,
    name: str = "row",
) -> dict:
    """One native CLI row measured on a fake Windows host."""

    root = _initialized_root(tmp_path / name)
    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: True)
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id: runtime_adapters.ExecutableResolution(
            adapter_id,
            "claude" if installed else "",
            installed,
            "" if installed else "not_found_on_path",
        ),
    )
    monkeypatch.setattr(
        repo_policy.claude_auth,
        "auth_status",
        lambda executable=None: {
            "launchable": True,
            "authenticated": True,
            "blocker_reason": "",
        },
    )
    return repo_policy._provider_status(
        root,
        "claude_cli",
        repo_policy.load_policy(root),
        sandbox_backend,
        sandbox_error,
        model_policy={"providers": {}, "adapters": {}, "models": {}},
    )


def test_windows_native_route_names_the_measured_appcontainer_cause(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """NF-2026-00876: the legacy blocker text alone was never a diagnosis.

    ``select_sandbox_backend`` measured which of three things refused the
    host and encoded it in its bounded error; the row threw that away and
    published one constant, so every Windows refusal read identically.
    """

    row = _windows_native_row(
        monkeypatch,
        tmp_path,
        sandbox_error=(
            "windows_appcontainer_sandbox_unavailable:win32_appcontainer_unavailable"
        ),
    )

    # Still fail-closed: the diagnosis is additional, never a relaxation.
    assert row["launchable"] is False
    assert row["platform_excluded"] is True
    assert row["status"] == "sandbox_unavailable"
    assert row["sandbox_backend"] == ""
    # The compatibility blocker code keeps its place AND gets its own field...
    assert row["reason"] == runtime_adapters.WINDOWS_NATIVE_CLI_REQUIRES_APPCONTAINER
    assert (
        row["sandbox_blocker_code"]
        == runtime_adapters.WINDOWS_NATIVE_CLI_REQUIRES_APPCONTAINER
    )
    # ...and the exact measured cause now travels beside it.
    assert (
        row["sandbox_unavailable_cause"]
        == repo_policy.SANDBOX_CAUSE_HOST_APPCONTAINER_UNAVAILABLE
    )
    assert row["sandbox_unavailable_detail"] == (
        "windows_appcontainer_sandbox_unavailable:win32_appcontainer_unavailable"
    )


def test_unwired_execution_path_is_distinct_from_host_and_binary_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Three different repairs, so they must not share one indistinguishable row."""

    prefix = repo_policy.WINDOWS_APPCONTAINER_SELECTION_PREFIX
    unwired = _windows_native_row(
        monkeypatch,
        tmp_path,
        sandbox_error=f"{prefix}:{repo_policy.SANDBOX_CAUSE_EXECUTION_PATH_NOT_WIRED}",
        name="unwired",
    )
    hostless = _windows_native_row(
        monkeypatch,
        tmp_path,
        sandbox_error=(
            f"{prefix}:{repo_policy.SANDBOX_CAUSE_HOST_APPCONTAINER_UNAVAILABLE}"
        ),
        name="hostless",
    )
    missing_binary = _windows_native_row(
        monkeypatch,
        tmp_path,
        sandbox_error=f"{prefix}:{repo_policy.SANDBOX_CAUSE_EXECUTION_PATH_NOT_WIRED}",
        installed=False,
        name="missing",
    )

    assert (
        unwired["sandbox_unavailable_cause"]
        == repo_policy.SANDBOX_CAUSE_EXECUTION_PATH_NOT_WIRED
    )
    assert (
        hostless["sandbox_unavailable_cause"]
        == repo_policy.SANDBOX_CAUSE_HOST_APPCONTAINER_UNAVAILABLE
    )
    assert unwired["sandbox_unavailable_cause"] != hostless["sandbox_unavailable_cause"]
    # One stable blocker code across all three -- the code is the compatibility
    # surface, the cause is the diagnosis.
    assert {
        row["sandbox_blocker_code"] for row in (unwired, hostless, missing_binary)
    } == {runtime_adapters.WINDOWS_NATIVE_CLI_REQUIRES_APPCONTAINER}
    # And a missing binary stays a separate, still-visible fact.
    assert unwired["installed"] is True
    assert missing_binary["installed"] is False
    assert (
        missing_binary["sandbox_unavailable_cause"]
        == repo_policy.SANDBOX_CAUSE_EXECUTION_PATH_NOT_WIRED
    )


def test_a_native_route_never_republishes_a_host_path_from_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The selection error is an exception string; other branches carry paths."""

    row = _windows_native_row(
        monkeypatch,
        tmp_path,
        sandbox_error="bubblewrap_unusable:/usr/bin/bwrap",
    )

    # The host path goes; the family token this build DOES name stays. Dropping
    # both told a Windows operator less than a POSIX one about the same error.
    assert (
        row["sandbox_unavailable_cause"]
        == repo_policy.SANDBOX_FAMILY_BUBBLEWRAP_UNUSABLE
    )
    assert (
        row["sandbox_unavailable_detail"]
        == repo_policy.SANDBOX_FAMILY_BUBBLEWRAP_UNUSABLE
    )
    assert "/usr/bin/bwrap" not in json.dumps(row, sort_keys=True)
    # A refusal that named no cause at all says so rather than inventing one,
    # and it carries no detail: there is no measurement to describe, so the
    # field is pinned empty rather than echoing the prefix back as a diagnosis.
    silent = _windows_native_row(
        monkeypatch,
        tmp_path,
        sandbox_error=repo_policy.WINDOWS_APPCONTAINER_SELECTION_PREFIX,
        name="silent",
    )
    assert silent["sandbox_unavailable_cause"] == repo_policy.SANDBOX_CAUSE_UNREPORTED
    assert silent["sandbox_unavailable_detail"] == ""
    # A prefix that named only whitespace after its colon is the same fact and
    # must not be told apart from the silent one by its detail.
    blank = _windows_native_row(
        monkeypatch,
        tmp_path,
        sandbox_error=f"{repo_policy.WINDOWS_APPCONTAINER_SELECTION_PREFIX}:   ",
        name="blank",
    )
    assert blank["sandbox_unavailable_cause"] == repo_policy.SANDBOX_CAUSE_UNREPORTED
    assert blank["sandbox_unavailable_detail"] == ""


def test_a_well_shaped_but_unknown_selection_cause_is_never_published(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Closed vocabulary means member-of-set, never looks-like-a-member.

    Admitting any bare lowercase token would publish whatever future
    ``select_sandbox_backend`` branches learn to emit, including causes
    carrying facts this build never agreed to expose, so membership is the
    only gate and an unknown token is refused with no detail at all.
    """

    unknown = "win32_token_handle_leaked_by_broker"
    assert unknown not in repo_policy.WINDOWS_APPCONTAINER_SELECTION_CAUSES
    row = _windows_native_row(
        monkeypatch,
        tmp_path,
        sandbox_error=f"{repo_policy.WINDOWS_APPCONTAINER_SELECTION_PREFIX}:{unknown}",
    )

    assert row["sandbox_unavailable_cause"] == repo_policy.SANDBOX_CAUSE_UNRECOGNIZED
    assert row["sandbox_unavailable_detail"] == ""
    assert unknown not in json.dumps(row, sort_keys=True)
    # And refusing to diagnose it never relaxes the refusal itself.
    assert row["launchable"] is False
    assert row["platform_excluded"] is True
    assert (
        row["sandbox_blocker_code"]
        == runtime_adapters.WINDOWS_NATIVE_CLI_REQUIRES_APPCONTAINER
    )


def test_a_capable_appcontainer_host_admits_without_bypassing_other_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The backend answers one question; it must not answer the others."""

    root = _initialized_root(tmp_path)
    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: True)
    monkeypatch.setattr(
        repo_policy.claude_auth,
        "auth_status",
        lambda executable=None: {
            "launchable": True,
            "authenticated": True,
            "blocker_reason": "",
        },
    )
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id: runtime_adapters.ExecutableResolution(
            adapter_id, "claude", True, ""
        ),
    )
    policy = repo_policy.load_policy(root)
    permissive = {"providers": {}, "adapters": {}, "models": {}}

    admitted = repo_policy._provider_status(
        root,
        "claude_cli",
        policy,
        repo_policy.WINDOWS_APPCONTAINER_BACKEND,
        "",
        model_policy=permissive,
    )
    assert admitted["launchable"] is True
    assert admitted["platform_excluded"] is False
    assert admitted["coverage_required"] is True
    assert admitted["sandbox_backend"] == repo_policy.WINDOWS_APPCONTAINER_BACKEND
    assert admitted["sandbox_blocker_code"] == ""
    assert admitted["sandbox_unavailable_cause"] == ""

    provider, _adapter = repo_policy.model_settings.policy_identity_for_adapter(
        "claude_cli"
    )
    model_policy_denied = repo_policy._provider_status(
        root,
        "claude_cli",
        policy,
        repo_policy.WINDOWS_APPCONTAINER_BACKEND,
        "",
        model_policy={"providers": {provider: False}, "adapters": {}, "models": {}},
    )
    assert model_policy_denied["launchable"] is False
    assert model_policy_denied["status"] == "repository_model_policy_disabled"

    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id: runtime_adapters.ExecutableResolution(
            adapter_id, "", False, "not_found_on_path"
        ),
    )
    uninstalled = repo_policy._provider_status(
        root,
        "claude_cli",
        policy,
        repo_policy.WINDOWS_APPCONTAINER_BACKEND,
        "",
        model_policy=permissive,
    )
    assert uninstalled["installed"] is False
    assert uninstalled["launchable"] is False


def _preflight_on_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    select_backend: Callable[[], str],
    windows: bool = False,
    name: str = "report",
) -> dict:
    """A whole preflight report built on one measured host.

    ``select_backend`` stands in for ``select_sandbox_backend``: it either
    returns the backend selection actually settled on or raises the bounded
    refusal selection would have raised.  Both outcomes have to be reachable
    from one fixture, because "selection succeeded with the wrong backend" and
    "selection refused" are exactly the two facts the surfaces must not blur.
    """

    root = _initialized_root(tmp_path / name)
    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: windows)
    monkeypatch.setattr(
        repo_policy.task_store,
        "storage_readiness",
        lambda _root: SimpleNamespace(ready=True, reason="ready", repo_id="repo_test"),
    )
    monkeypatch.setattr(
        repo_policy.source_graph_daemon,
        "daemon_health",
        lambda _root: {
            "ok": True,
            "status": "ready",
            "running": True,
            "registered": True,
            "readable_generation": True,
            "last_success_at": "2026-09-14T10:00:00+00:00",
            "stale_reason": "",
            "build_revision": "aiworkhub.source_graph.semantic.v6",
            "files_seen": 4,
        },
    )
    monkeypatch.setattr(
        repo_policy.task_store,
        "callback_bridge_health",
        lambda _root: {"ok": True, "backlog_count": 0, "retry_count": 0},
    )
    monkeypatch.setattr(
        repo_policy.worker_workspace, "select_sandbox_backend", select_backend
    )
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id: runtime_adapters.ExecutableResolution(
            adapter_id, "claude", True, ""
        ),
    )
    monkeypatch.setattr(
        repo_policy.claude_auth,
        "auth_status",
        lambda executable=None: {
            "launchable": True,
            "authenticated": True,
            "blocker_reason": "",
        },
    )
    return repo_policy.build_preflight(root, adapter_id="claude_cli")


def _preflight_after_selection_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    sandbox_error: str,
    windows: bool = False,
    name: str = "report",
) -> dict:
    """A whole preflight report built on a host whose selection refused."""

    def _refuse() -> str:
        raise repo_policy.worker_workspace.WorkspaceError(sandbox_error)

    return _preflight_on_host(
        monkeypatch,
        tmp_path,
        select_backend=_refuse,
        windows=windows,
        name=name,
    )


def test_global_sandbox_block_never_republishes_selection_host_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """NF-2026-00876: the per-route cause was vouched, the global block was not.

    ``select_sandbox_backend`` puts a host path after ``bubblewrap_unusable``
    and an environment value after ``invalid_sandbox_backend``, and the report
    handed both straight to the dashboard through ``native_cli_reason``. The
    family is the whole publishable fact.
    """

    pathful = _preflight_after_selection_refused(
        monkeypatch,
        tmp_path,
        sandbox_error="bubblewrap_unusable:/usr/bin/bwrap",
        name="pathful",
    )
    block = pathful["sandbox"]
    assert block["enforceable"] is False
    assert block["native_cli_reason"] == repo_policy.SANDBOX_FAMILY_BUBBLEWRAP_UNUSABLE
    # The selected route feeds this field, so the route row is bounded too.
    assert block["reason"] == repo_policy.SANDBOX_FAMILY_BUBBLEWRAP_UNUSABLE
    assert "/usr/bin/bwrap" not in json.dumps(pathful, sort_keys=True, default=str)

    leaked_env_value = "s3cret-backend-name-from-the-environment"
    envful = _preflight_after_selection_refused(
        monkeypatch,
        tmp_path,
        sandbox_error=f"{repo_policy.SANDBOX_FAMILY_INVALID_BACKEND}:{leaked_env_value}",
        name="envful",
    )
    assert (
        envful["sandbox"]["native_cli_reason"]
        == repo_policy.SANDBOX_FAMILY_INVALID_BACKEND
    )
    assert leaked_env_value not in json.dumps(envful, sort_keys=True, default=str)

    # A family this build does not name is refused outright rather than
    # published on the strength of looking like a token.
    unknown = _preflight_after_selection_refused(
        monkeypatch,
        tmp_path,
        sandbox_error="future_sandbox_probe_failed:C:\\Users\\shrek\\key.pem",
        name="unknown",
    )
    assert (
        unknown["sandbox"]["native_cli_reason"] == repo_policy.SANDBOX_CAUSE_UNRECOGNIZED
    )
    assert "key.pem" not in json.dumps(unknown, sort_keys=True, default=str)


def test_global_and_per_route_windows_causes_are_the_same_measurement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two surfaces, one answer -- and the vouched cause still travels."""

    report = _preflight_after_selection_refused(
        monkeypatch,
        tmp_path,
        sandbox_error=(
            f"{repo_policy.WINDOWS_APPCONTAINER_SELECTION_PREFIX}"
            f":{repo_policy.SANDBOX_CAUSE_HOST_APPCONTAINER_UNAVAILABLE}"
        ),
        windows=True,
        name="windows",
    )
    block = report["sandbox"]
    row = {item["adapter_id"]: item for item in report["providers"]}["claude_cli"]

    assert (
        block["native_cli_cause"]
        == repo_policy.SANDBOX_CAUSE_HOST_APPCONTAINER_UNAVAILABLE
    )
    assert block["native_cli_cause"] == row["sandbox_unavailable_cause"]
    assert block["native_cli_reason"] == row["sandbox_unavailable_detail"]
    assert row["launchable"] is False
    assert block["reason"] == runtime_adapters.WINDOWS_NATIVE_CLI_REQUIRES_APPCONTAINER


def test_the_publication_guard_scrubs_any_future_unvouched_sandbox_field() -> None:
    """The guard, not a per-field derivation, is what closed this defect.

    The first repair vouched the per-route cause and its own tests passed while
    one surviving projection of the raw error still reached the dashboard. The
    boundary check exists so the next field wired to selection text is scrubbed
    rather than shipped.
    """

    scrubbed = repo_policy._vouched_sandbox_block(
        {
            "native_cli_reason": "bubblewrap_unusable:/usr/bin/bwrap",
            "native_cli_cause": "win32_token_handle_leaked_by_broker",
            "backend": "C:\\Users\\shrek\\AppData\\bwrap.exe",
            "enforceable": False,
            "route_aware": True,
        }
    )

    assert scrubbed["native_cli_reason"] == repo_policy.SANDBOX_CAUSE_UNRECOGNIZED
    assert scrubbed["native_cli_cause"] == repo_policy.SANDBOX_CAUSE_UNRECOGNIZED
    # An identifier field is EMPTIED, not handed a reason token: that token
    # names no backend, so it was no more resolvable than the path it replaced.
    assert scrubbed["backend"] == ""
    # Non-string facts are untouched: the guard bounds text, not verdicts.
    assert scrubbed["enforceable"] is False
    assert scrubbed["route_aware"] is True
    # Every string it does publish is a declared member, never a shape match.
    for field in ("native_cli_reason", "native_cli_cause"):
        assert scrubbed[field] in repo_policy.SANDBOX_PUBLISHABLE_SELECTION_REASONS


def test_the_publication_guard_refuses_an_unknown_separator_free_field() -> None:
    """Scrubbing on the separator alone left the real leak shape publishable.

    ``select_sandbox_backend`` raises ``invalid_sandbox_backend:<env value>``,
    and an environment value need not contain a path separator to be a secret.
    The guard therefore refuses by default: a key it does not recognise as a
    backend/adapter identifier must carry a DECLARED publishable reason or be
    scrubbed, so the next sandbox field added to the block cannot inherit a
    bypass simply by being new.
    """

    leaked = "invalid_sandbox_backend:s3cret-env-value"
    assert "/" not in leaked and "\\" not in leaked
    scrubbed = repo_policy._vouched_sandbox_block(
        {
            "native_cli_detail": leaked,
            "some_future_sandbox_note": "seccomp_probe_returned_EPERM",
            "native_cli_reason": repo_policy.SANDBOX_FAMILY_BUBBLEWRAP_UNUSABLE,
            "backend": repo_policy.WINDOWS_APPCONTAINER_BACKEND,
            "selected_adapter": "claude_cli",
            "enforceable": False,
        }
    )

    assert scrubbed["native_cli_detail"] == repo_policy.SANDBOX_CAUSE_UNRECOGNIZED
    assert (
        scrubbed["some_future_sandbox_note"] == repo_policy.SANDBOX_CAUSE_UNRECOGNIZED
    )
    assert "s3cret-env-value" not in json.dumps(scrubbed, sort_keys=True)
    # Refusing by default must not start scrubbing the identifier fields the
    # block has always published, nor the reasons it declared publishable.
    assert scrubbed["native_cli_reason"] == repo_policy.SANDBOX_FAMILY_BUBBLEWRAP_UNUSABLE
    assert scrubbed["backend"] == repo_policy.WINDOWS_APPCONTAINER_BACKEND
    assert scrubbed["selected_adapter"] == "claude_cli"
    assert scrubbed["enforceable"] is False


def _posix_native_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    sandbox_error: str,
    name: str = "posix",
) -> dict:
    """One native CLI row measured on a host that is not Windows."""

    root = _initialized_root(tmp_path / name)
    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: False)
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id: runtime_adapters.ExecutableResolution(
            adapter_id, "claude", True, ""
        ),
    )
    monkeypatch.setattr(
        repo_policy.claude_auth,
        "auth_status",
        lambda executable=None: {
            "launchable": True,
            "authenticated": True,
            "blocker_reason": "",
        },
    )
    return repo_policy._provider_status(
        root,
        "claude_cli",
        repo_policy.load_policy(root),
        "",
        sandbox_error,
        model_policy={"providers": {}, "adapters": {}, "models": {}},
    )


def test_a_posix_sandbox_blocked_route_states_code_cause_and_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A blocker that exists only in ``status`` is not a published blocker.

    Windows rows gained a code, a cause and a detail; every other host kept a
    row whose status said ``sandbox_unavailable`` beside three empty fields,
    so a caller matching on ``sandbox_blocker_code`` could not tell a Linux
    sandbox refusal from a route with no sandbox blocker at all.
    """

    pathful = _posix_native_row(
        monkeypatch,
        tmp_path,
        sandbox_error="bubblewrap_unusable:/usr/bin/bwrap",
        name="pathful",
    )

    assert pathful["launchable"] is False
    assert pathful["status"] == repo_policy.SANDBOX_STATUS_UNAVAILABLE
    # Not a platform exclusion: this host COULD run the route, and the missing
    # sandbox is the only thing standing in the way.
    assert pathful["platform_excluded"] is False
    assert (
        pathful["sandbox_blocker_code"]
        == repo_policy.SANDBOX_BLOCKER_ENFORCEABLE_SANDBOX_UNAVAILABLE
    )
    assert (
        pathful["sandbox_unavailable_cause"]
        == repo_policy.SANDBOX_FAMILY_BUBBLEWRAP_UNUSABLE
    )
    assert (
        pathful["sandbox_unavailable_detail"]
        == repo_policy.SANDBOX_FAMILY_BUBBLEWRAP_UNUSABLE
    )
    assert "/usr/bin/bwrap" not in json.dumps(pathful, sort_keys=True)

    # Silence is still a stated fact rather than an empty field.
    silent = _posix_native_row(
        monkeypatch, tmp_path, sandbox_error="", name="silent"
    )
    assert (
        silent["sandbox_blocker_code"]
        == repo_policy.SANDBOX_BLOCKER_ENFORCEABLE_SANDBOX_UNAVAILABLE
    )
    assert silent["sandbox_unavailable_cause"] == repo_policy.SANDBOX_CAUSE_UNREPORTED
    assert silent["sandbox_unavailable_detail"] == repo_policy.SANDBOX_CAUSE_UNREPORTED
    for row in (pathful, silent):
        for field in ("sandbox_unavailable_cause", "sandbox_unavailable_detail"):
            assert row[field] in repo_policy.SANDBOX_PUBLISHABLE_SELECTION_REASONS


def test_the_closed_secure_sandbox_probe_vocabulary_is_not_collapsed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A kernel without Landlock is a different repair from a host without seccomp.

    ``secure_sandbox_unavailable`` is the one family whose trailing token comes
    from a closed probe vocabulary rather than from the host, so reducing it to
    the family threw away the only actionable fact while the two open families
    kept theirs reduced on purpose.
    """

    landlock = _posix_native_row(
        monkeypatch,
        tmp_path,
        sandbox_error="secure_sandbox_unavailable:bubblewrap_unusable:landlock_unsupported",
        name="landlock",
    )
    seccomp = _posix_native_row(
        monkeypatch,
        tmp_path,
        sandbox_error="secure_sandbox_unavailable:bubblewrap_unusable:seccomp_unavailable",
        name="seccomp",
    )

    assert (
        landlock["sandbox_unavailable_cause"]
        == repo_policy.SANDBOX_CAUSE_LANDLOCK_UNSUPPORTED
    )
    assert landlock["sandbox_unavailable_detail"] == (
        "secure_sandbox_unavailable:bubblewrap_unusable:landlock_unsupported"
    )
    assert (
        seccomp["sandbox_unavailable_cause"]
        == repo_policy.SANDBOX_CAUSE_SECCOMP_UNAVAILABLE
    )
    assert (
        landlock["sandbox_unavailable_cause"] != seccomp["sandbox_unavailable_cause"]
    )
    for row in (landlock, seccomp):
        assert (
            row["sandbox_unavailable_detail"]
            in repo_policy.SANDBOX_PUBLISHABLE_SELECTION_REASONS
        )

    # Membership, never shape: an unknown probe token after the same family is
    # host detail and collapses back to the family with nothing carried over.
    probe_path = _posix_native_row(
        monkeypatch,
        tmp_path,
        sandbox_error="secure_sandbox_unavailable:bubblewrap_unusable:/tmp/probe.log",
        name="probe",
    )
    assert (
        probe_path["sandbox_unavailable_cause"]
        == repo_policy.SANDBOX_FAMILY_SECURE_SANDBOX_UNAVAILABLE
    )
    assert "/tmp/probe.log" not in json.dumps(probe_path, sort_keys=True)


def test_posix_preflight_and_its_summary_agree_with_the_route_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One measurement, three surfaces: row, global block and summary."""

    report = _preflight_after_selection_refused(
        monkeypatch,
        tmp_path,
        sandbox_error=(
            "secure_sandbox_unavailable:bubblewrap_unusable:landlock_unsupported"
        ),
        name="posix_report",
    )
    row = {item["adapter_id"]: item for item in report["providers"]}["claude_cli"]
    block = report["sandbox"]
    projected = {
        item["adapter_id"]: item
        for item in report["provider_summary"]["unavailable_routes"]
    }["claude_cli"]

    assert row["status"] == repo_policy.SANDBOX_STATUS_UNAVAILABLE
    assert block["native_cli_cause"] == row["sandbox_unavailable_cause"]
    assert block["native_cli_reason"] == row["sandbox_unavailable_detail"]
    assert block["reason"] == row["reason"]
    assert projected["blocker_code"] == row["sandbox_blocker_code"]
    assert projected["cause"] == row["sandbox_unavailable_cause"]
    assert projected["detail"] == row["sandbox_unavailable_detail"]
    for field in ("blocker_code", "cause", "detail"):
        assert projected[field]


def test_a_credential_blocker_is_never_published_as_a_sandbox_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sandbox block answers one question and must not borrow another's answer.

    The block took the selected row's reason whenever that row was not
    launchable, so a credential blocker landed on a surface whose whole
    vocabulary is sandbox selection -- where the publication guard can only
    refuse it as an unrecognised cause.
    """

    root = _initialized_root(tmp_path / "credential")
    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: False)
    monkeypatch.setattr(
        repo_policy.task_store,
        "storage_readiness",
        lambda _root: SimpleNamespace(ready=True, reason="ready", repo_id="repo_test"),
    )
    monkeypatch.setattr(
        repo_policy.task_store,
        "callback_bridge_health",
        lambda _root: {"ok": True, "backlog_count": 0, "retry_count": 0},
    )
    monkeypatch.setattr(
        repo_policy.source_graph_daemon,
        "daemon_health",
        lambda _root: {
            "ok": True,
            "status": "ready",
            "running": True,
            "registered": True,
            "readable_generation": True,
            "last_success_at": "2026-09-14T10:00:00+00:00",
            "stale_reason": "",
            "build_revision": "aiworkhub.source_graph.semantic.v6",
            "files_seen": 4,
        },
    )
    monkeypatch.setattr(
        repo_policy.worker_workspace, "select_sandbox_backend", lambda: "bubblewrap"
    )
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id: runtime_adapters.ExecutableResolution(
            adapter_id, "claude", True, ""
        ),
    )
    monkeypatch.setattr(
        repo_policy.claude_auth,
        "auth_status",
        lambda executable=None: {
            "launchable": False,
            "authenticated": False,
            "blocker_reason": "subscription_credential_absent",
        },
    )

    report = repo_policy.build_preflight(root, adapter_id="claude_cli")
    row = {item["adapter_id"]: item for item in report["providers"]}["claude_cli"]

    assert row["reason"] == "subscription_credential_absent"
    assert row["sandbox_blocker_code"] == ""
    # The sandbox WAS selected, so the block must not restate a credential fact
    # in sandbox vocabulary -- but it published ``enforceable: false`` beside an
    # empty reason, a verdict with its subject withheld. It now names the SHAPE
    # of the blocker from its own closed vocabulary and leaves the blocker
    # itself on the route row that owns it.
    block = report["sandbox"]
    assert block["enforceable"] is False
    assert block["reason"] == repo_policy.SANDBOX_REASON_ROUTE_BLOCKED_OUTSIDE_SANDBOX
    assert block["reason"] in repo_policy.SANDBOX_PUBLISHABLE_SELECTION_REASONS
    assert "subscription_credential_absent" not in json.dumps(block, sort_keys=True)
    assert block["native_cli_backend"] == "bubblewrap"


def test_a_foreign_family_refusal_on_windows_agrees_across_every_surface(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One derivation, or the block contradicts the row it claims to summarise.

    Selection can refuse a Windows host with a family that is not the Windows
    one: ``invalid_sandbox_backend:<env value>`` is raised from reading the
    environment. Taking the global cause from the Windows derivation while the
    global reason kept the general one published ``selection_cause_unrecognized``
    beside ``invalid_sandbox_backend`` -- with the route row carrying neither.
    """

    leaked = "s3cret-env-value"
    report = _preflight_after_selection_refused(
        monkeypatch,
        tmp_path,
        sandbox_error=f"{repo_policy.SANDBOX_FAMILY_INVALID_BACKEND}:{leaked}",
        windows=True,
        name="windows_foreign_family",
    )
    block = report["sandbox"]
    row = {item["adapter_id"]: item for item in report["providers"]}["claude_cli"]

    assert block["native_cli_cause"] == row["sandbox_unavailable_cause"]
    assert block["native_cli_reason"] == row["sandbox_unavailable_detail"]
    # The family itself IS a token this build names, so both surfaces keep it
    # and drop only the environment value after it. Answering an
    # `invalid_sandbox_backend` refusal with `selection_cause_unrecognized` told
    # a Windows operator less than a POSIX one about the identical error.
    assert block["native_cli_cause"] == repo_policy.SANDBOX_FAMILY_INVALID_BACKEND
    assert block["native_cli_reason"] == repo_policy.SANDBOX_FAMILY_INVALID_BACKEND
    # Declining to diagnose it never relaxes the refusal, and the stable
    # compatibility blocker code remains the token callers match on.
    assert row["launchable"] is False
    assert row["platform_excluded"] is True
    assert (
        row["sandbox_blocker_code"]
        == runtime_adapters.WINDOWS_NATIVE_CLI_REQUIRES_APPCONTAINER
    )
    assert block["reason"] == runtime_adapters.WINDOWS_NATIVE_CLI_REQUIRES_APPCONTAINER
    assert block["native_cli_enforceable"] is False
    assert leaked not in json.dumps(report, sort_keys=True, default=str)


def test_a_selected_non_appcontainer_backend_is_not_native_cli_enforceable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Selection succeeding is not the boundary native CLI needs on Windows.

    ``native_cli_enforceable`` was ``bool(backend)``, so a Windows host that
    selected any other backend advertised an enforceable native CLI sandbox on
    the very report whose every native row was refused as platform-excluded.
    """

    report = _preflight_on_host(
        monkeypatch,
        tmp_path,
        select_backend=lambda: repo_policy.SANDBOX_BACKEND_BUBBLEWRAP,
        windows=True,
        name="windows_other_backend",
    )
    block = report["sandbox"]
    row = {item["adapter_id"]: item for item in report["providers"]}["claude_cli"]

    assert block["native_cli_enforceable"] is False
    assert row["launchable"] is False
    assert row["platform_excluded"] is True
    assert row["status"] == repo_policy.SANDBOX_STATUS_UNAVAILABLE
    # A backend that WAS selected and is simply the wrong one is its own cause,
    # distinct from selection having failed outright.
    assert (
        row["sandbox_unavailable_cause"]
        == repo_policy.SANDBOX_CAUSE_BACKEND_NOT_APPCONTAINER
    )
    assert block["native_cli_cause"] == row["sandbox_unavailable_cause"]
    assert block["native_cli_reason"] == row["sandbox_unavailable_detail"]

    # The same selected backend off Windows is exactly what native CLI needs,
    # so the field tracks the measurement rather than the platform.
    posix = _preflight_on_host(
        monkeypatch,
        tmp_path,
        select_backend=lambda: repo_policy.SANDBOX_BACKEND_BUBBLEWRAP,
        windows=False,
        name="posix_same_backend",
    )
    posix_row = {item["adapter_id"]: item for item in posix["providers"]}["claude_cli"]

    assert posix["sandbox"]["native_cli_enforceable"] is True
    assert posix_row["launchable"] is True
    assert posix_row["sandbox_unavailable_cause"] == ""


def test_a_known_selection_family_survives_the_windows_derivation() -> None:
    """Windows must not be told LESS than POSIX about the identical refusal.

    ``select_sandbox_backend`` reads the environment on every platform, so
    ``invalid_sandbox_backend:<env value>`` reaches a Windows host exactly as it
    reaches a Linux one.  Collapsing it to ``selection_cause_unrecognized``
    discarded a family this build does name, when the secret suffix was the
    only part that ever needed dropping.
    """

    leaked = "s3cret-backend-name-from-the-environment"
    error = f"{repo_policy.SANDBOX_FAMILY_INVALID_BACKEND}:{leaked}"
    windows = repo_policy._windows_native_sandbox_cause("", error)

    assert windows == (
        repo_policy.SANDBOX_FAMILY_INVALID_BACKEND,
        repo_policy.SANDBOX_FAMILY_INVALID_BACKEND,
    )
    assert leaked not in "".join(windows)
    # One refusal, one answer: the platform changes nothing about a family
    # neither derivation owns.
    assert windows == repo_policy._bounded_sandbox_selection("", error)

    # Membership still decides. A family this build cannot name carries nothing
    # over, on Windows exactly as anywhere else.
    assert repo_policy._windows_native_sandbox_cause(
        "", "future_sandbox_probe_failed:C:\\Users\\shrek\\key.pem"
    ) == (
        repo_policy.SANDBOX_CAUSE_UNRECOGNIZED,
        repo_policy.SANDBOX_CAUSE_UNRECOGNIZED,
    )
    # And the Windows family keeps its own stricter rule inside itself: a cause
    # outside the closed vocabulary is stated without borrowing a detail that
    # was never measured.
    assert repo_policy._windows_native_sandbox_cause(
        "",
        f"{repo_policy.WINDOWS_APPCONTAINER_SELECTION_PREFIX}:winsta_probe_returned_0x5",
    ) == (repo_policy.SANDBOX_CAUSE_UNRECOGNIZED, "")
    # Fail-closed admission is untouched: only the real backend clears it.
    assert repo_policy._windows_native_sandbox_cause(
        repo_policy.WINDOWS_APPCONTAINER_BACKEND, error
    ) == ("", "")


def test_no_sandbox_block_publishes_a_verdict_without_its_subject(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two boundary invariants every measured host must satisfy at once.

    ``enforceable: false`` beside an empty ``reason`` is a verdict with its
    subject withheld -- the reader is told the sandbox cannot be enforced and
    given nothing to act on.  And a scrubbed field must stay inside ITS OWN
    vocabulary: an identifier scrubbed to a reason token named no backend and
    no adapter, so it was no more resolvable than the value it replaced.
    """

    blocks = [
        _preflight_after_selection_refused(
            monkeypatch, tmp_path, sandbox_error=error, windows=windows, name=name
        )["sandbox"]
        for error, windows, name in (
            ("bubblewrap_unusable:/usr/bin/bwrap", False, "vocab_posix_path"),
            (
                f"{repo_policy.SANDBOX_FAMILY_INVALID_BACKEND}:s3cret-env-value",
                True,
                "vocab_windows_env",
            ),
            ("", False, "vocab_silent"),
            ("future_sandbox_probe_failed:/tmp/probe.log", False, "vocab_unknown"),
        )
    ]
    # A host where selection SUCCEEDED and the route is blocked anyway is the
    # case that published the empty reason, so it belongs in the same sweep.
    blocks.append(
        _preflight_on_host(
            monkeypatch,
            tmp_path,
            select_backend=lambda: repo_policy.SANDBOX_BACKEND_BUBBLEWRAP,
            windows=True,
            name="vocab_windows_other_backend",
        )["sandbox"]
    )

    for block in blocks:
        if not block["enforceable"]:
            assert block["reason"], block
            assert block["reason"] in repo_policy.SANDBOX_PUBLISHABLE_SELECTION_REASONS
        for field in ("reason", "native_cli_reason", "native_cli_cause"):
            if block[field]:
                assert block[field] in repo_policy.SANDBOX_PUBLISHABLE_SELECTION_REASONS
        # An identifier field publishes a member of its own vocabulary or
        # nothing at all -- never a reason token.
        for field in ("backend", "selected_backend", "native_cli_backend"):
            if block[field]:
                assert block[field] in repo_policy.SANDBOX_PUBLISHABLE_BACKENDS
        if block["selected_adapter"]:
            assert block["selected_adapter"] in repo_policy._POLICY_ALLOWED_ADAPTERS


def test_an_unvouched_identifier_is_emptied_rather_than_given_a_reason_token() -> None:
    """The guard's refusal must not itself publish an unresolvable value.

    Scrubbing a backend field to ``selection_cause_unrecognized`` swapped one
    value no reader of that field can resolve for another: the token names no
    backend this build selects, and it is not a member of the vocabulary that
    field is read against. Empty is what those fields already publish for "no
    identifier", so that is what a refusal writes.
    """

    scrubbed = repo_policy._vouched_sandbox_block(
        {
            "backend": "C:\\Users\\shrek\\AppData\\bwrap.exe",
            "selected_backend": "invalid_sandbox_backend:s3cret-env-value",
            "native_cli_backend": repo_policy.SANDBOX_BACKEND_BUBBLEWRAP,
            "selected_adapter": "rogue_adapter_from_a_future_build",
            "native_cli_reason": "bubblewrap_unusable:/usr/bin/bwrap",
        }
    )

    assert scrubbed["backend"] == ""
    assert scrubbed["selected_backend"] == ""
    assert scrubbed["selected_adapter"] == ""
    # A reason field keeps the reason vocabulary, where that token IS declared.
    assert scrubbed["native_cli_reason"] == repo_policy.SANDBOX_CAUSE_UNRECOGNIZED
    # Vouched values on either side of a scrubbed neighbour are left alone.
    assert scrubbed["native_cli_backend"] == repo_policy.SANDBOX_BACKEND_BUBBLEWRAP
    assert "s3cret-env-value" not in json.dumps(scrubbed, sort_keys=True)
    assert "bwrap.exe" not in json.dumps(scrubbed, sort_keys=True)
    assert repo_policy.SANDBOX_CAUSE_UNRECOGNIZED not in (
        repo_policy.SANDBOX_PUBLISHABLE_BACKENDS | set(repo_policy._POLICY_ALLOWED_ADAPTERS)
    )


def test_build_preflight_warms_the_settings_catalog_handoff(tmp_path: Path) -> None:
    """NF-2026-... (OpenCode never appeared in model settings, take two).

    workforce_catalog.cached_preflight_snapshot / _settings_preflight_
    snapshot exist specifically so a Settings read reuses an already-built
    preflight instead of spawning a second ``opencode models`` probe -- but
    the write side, ``remember_preflight_snapshot``, was previously called
    only from ``workforce_catalog.build_catalog``, which the Settings/Workforce
    read path does not itself invoke. Measured: ``resolve_executable`` and the
    OpenCode discovery probe both worked correctly end to end, and a fresh
    ``build_preflight`` call already carried every discovered model in its own
    return value -- but a Settings read taken before anything happened to call
    ``build_catalog`` first saw an empty handoff and reported zero OpenCode
    models regardless. ``build_preflight`` is the one place every preflight
    consumer (the MCP tool, the dashboard, this test) ultimately returns from,
    so warming the handoff there closes the gap regardless of call order.
    """

    root = _initialized_root(tmp_path)
    assert workforce_catalog.cached_preflight_snapshot(root) is None

    report = repo_policy.build_preflight(root)

    cached = workforce_catalog.cached_preflight_snapshot(root)
    assert cached is not None
    cached_ids = {item["adapter_id"] for item in cached["providers"]}
    report_ids = {item["adapter_id"] for item in report["providers"]}
    assert cached_ids == report_ids
