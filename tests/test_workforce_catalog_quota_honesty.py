"""Quota honesty for workforce catalog readiness.

The catalog must never report ``readiness_status == "ready"`` for a worker
whose provider quota was not observed. ``repo_policy._provider_status`` is the
single source of the per-adapter ``status`` that ``workforce_catalog`` copies
verbatim into ``readiness_status``, so this file proves both ends of that chain:

* a launchable, access-observed but quota-unobserved route reports the
  distinct value ``"ready_unverified"`` (never ``"ready"``), with the quota
  reason carried alongside it; and
* a launchable, access-observed route with an observed healthy quota still
  reports ``"ready"`` — the change is honest, not blanket pessimism.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiworkhub import repo_policy, runtime_adapters, workforce_catalog

_POLICY = {"providers": {"allowed_adapters": ["claude_cli"]}}


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".aiworkhub/config").mkdir(parents=True)
    (root / ".aiworkhub/project.json").write_text("{}\n", encoding="utf-8")
    return root


def _unobserved_auth(_executable=None) -> dict[str, Any]:
    return {"launchable": True, "authenticated": True, "blocker_reason": ""}


def _observed_healthy_auth(_executable=None) -> dict[str, Any]:
    return {
        "launchable": True,
        "authenticated": True,
        "quota_observed": True,
        "quota_state": "healthy",
        "blocker_reason": "",
    }


def _patch_runtime(monkeypatch, auth) -> None:
    monkeypatch.setattr(repo_policy, "_is_windows_host", lambda: False)
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda adapter_id: runtime_adapters.ExecutableResolution(
            adapter_id, "/host/bin/model", True, ""
        ),
    )
    monkeypatch.setattr(repo_policy.claude_auth, "auth_status", auth)


def _claude_provider(root: Path) -> dict[str, Any]:
    return repo_policy._provider_status(
        root,
        "claude_cli",
        policy=_POLICY,
        sandbox_backend="bubblewrap",
        sandbox_error="",
    )


def test_unobserved_quota_reports_ready_unverified_never_ready(monkeypatch, tmp_path):
    root = _root(tmp_path)
    _patch_runtime(monkeypatch, _unobserved_auth)
    provider = _claude_provider(root)

    assert provider["launchable"] is True
    assert provider["access_observed"] is True
    assert provider["quota_observed"] is False
    assert provider["quota_state"] == "unavailable_from_provider_api"
    assert provider["status"] == "ready_unverified"
    assert provider["status"] != "ready"
    # The reason the quota could not be observed travels with the value.
    assert provider["reason"] == "quota_unobserved:unavailable_from_provider_api"


def test_observed_healthy_quota_still_reports_ready(monkeypatch, tmp_path):
    root = _root(tmp_path)
    _patch_runtime(monkeypatch, _observed_healthy_auth)
    provider = _claude_provider(root)

    assert provider["launchable"] is True
    assert provider["access_observed"] is True
    assert provider["quota_observed"] is True
    assert provider["status"] == "ready"


def test_catalog_ready_unverified_when_quota_unobserved(monkeypatch, tmp_path):
    root = _root(tmp_path)
    _patch_runtime(monkeypatch, _unobserved_auth)
    provider = _claude_provider(root)

    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight={"providers": [provider]},
    )
    worker = next(
        row for row in snapshot["workers"] if row["worker_id"] == "claude-sonnet-5"
    )
    assert worker["readiness_status"] == "ready_unverified"
    assert worker["readiness_status"] != "ready"
    assert worker["available"] is True
    assert worker["quota_observed"] is False
    assert worker["quota_state"] == "unavailable_from_provider_api"


def test_catalog_ready_when_quota_observed_healthy(monkeypatch, tmp_path):
    root = _root(tmp_path)
    _patch_runtime(monkeypatch, _observed_healthy_auth)
    provider = _claude_provider(root)

    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight={"providers": [provider]},
    )
    worker = next(
        row for row in snapshot["workers"] if row["worker_id"] == "claude-sonnet-5"
    )
    assert worker["readiness_status"] == "ready"
    assert worker["available"] is True


def test_catalog_codex_workers_report_ready_unverified_when_quota_unobserved(tmp_path):
    root = _root(tmp_path)
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight={
            "providers": [
                {
                    "adapter_id": "codex_cli",
                    "launchable": True,
                    "access_observed": True,
                    "observed_models": ["gpt-5.5", "gpt-5.3-codex"],
                    "status": "ready_unverified",
                }
            ]
        },
    )
    gpt55 = next(
        row for row in snapshot["workers"] if row["worker_id"] == "gpt-5.5"
    )
    codex53 = next(
        row for row in snapshot["workers"] if row["worker_id"] == "gpt-5.3-codex"
    )
    assert gpt55["readiness_status"] == "ready_unverified"
    assert gpt55["readiness_status"] != "ready"
    assert gpt55["available"] is True
    assert codex53["readiness_status"] == "ready_unverified"
    assert codex53["quota_observed"] is False
    assert codex53["quota_state"] == "unavailable_from_provider_api"


def test_codex_provider_capability_keeps_quota_explicitly_unobserved(
    monkeypatch, tmp_path
):
    root = _root(tmp_path)
    monkeypatch.setattr(
        repo_policy.runtime_adapters,
        "resolve_executable",
        lambda _adapter: type("Resolution", (), {
            "ok": True, "executable": "/safe/codex", "reason": ""
        })(),
    )
    monkeypatch.setattr(
        repo_policy.codex_auth,
        "capability_status",
        lambda _executable=None: {
            "launchable": True,
            "authenticated": True,
            "access_observed": True,
            "observed_models": ["gpt-5.5"],
            "quota_observed": False,
            "quota_state": "unavailable_from_provider_api",
            "blocker_reason": "",
        },
    )

    provider = repo_policy._provider_status(
        root,
        "codex_cli",
        policy={"providers": {"allowed_adapters": ["codex_cli"]}},
        sandbox_backend="bubblewrap",
        sandbox_error="",
    )
    assert provider["launchable"] is True
    assert provider["access_observed"] is True
    assert provider["quota_observed"] is False
    assert provider["quota_state"] == "unavailable_from_provider_api"
    assert provider["status"] == "ready_unverified"


def test_available_stays_boolean_and_readiness_status_distinguishes(
    monkeypatch, tmp_path
):
    """``available`` remains a transport-launchable boolean for compatibility.

    A quota-unverified worker is still ``available`` (launchable and route
    open), so callers must distinguish verified from unverified readiness
    through ``readiness_status`` ("ready" vs "ready_unverified"), never through
    ``available`` alone.
    """
    root = _root(tmp_path)
    _patch_runtime(monkeypatch, _unobserved_auth)
    unverified = _claude_provider(root)
    _patch_runtime(monkeypatch, _observed_healthy_auth)
    verified = _claude_provider(root)

    unverified_snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight={"providers": [unverified]},
    )
    verified_snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight={"providers": [verified]},
    )
    unverified_worker = next(
        row
        for row in unverified_snapshot["workers"]
        if row["worker_id"] == "claude-sonnet-5"
    )
    verified_worker = next(
        row
        for row in verified_snapshot["workers"]
        if row["worker_id"] == "claude-sonnet-5"
    )
    assert unverified_worker["available"] is True
    assert verified_worker["available"] is True
    assert unverified_worker["readiness_status"] == "ready_unverified"
    assert verified_worker["readiness_status"] == "ready"
    assert unverified_worker["readiness_status"] != verified_worker["readiness_status"]
