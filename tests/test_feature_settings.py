from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, dashboard_mcp_app, feature_settings, source_graph, source_graph_daemon, task_store, vscode_lm_bridge  # noqa: E402


def _repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    task_store.initialize_repository(root)
    return root


@pytest.fixture(autouse=True)
def _stop_daemons():
    roots: list[Path] = []
    yield roots
    for root in roots:
        source_graph_daemon.stop_daemon(root)


def test_defaults_are_repo_local_and_do_not_write(tmp_path: Path) -> None:
    first = _repo(tmp_path, "first")
    second = _repo(tmp_path, "second")

    initial = feature_settings.load(first)
    assert initial["revision"] == 0
    assert initial["configured"] is False
    assert initial["features"]["source_graph"] is True
    assert initial["features"]["context_graph"] is False
    assert not feature_settings.settings_path(first).exists()

    changed = feature_settings.update(
        first,
        changes={"source_graph": False, "context_graph": True},
        expected_revision=0,
    )
    assert changed["revision"] == 1
    assert changed["features"]["source_graph"] is False
    assert changed["features"]["context_graph"] is True
    assert feature_settings.load(second)["features"] == feature_settings.DEFAULT_FEATURES


def test_stale_revision_and_unknown_keys_fail_closed(tmp_path: Path) -> None:
    root = _repo(tmp_path, "repo")
    feature_settings.update(root, changes={"ai_memory": False}, expected_revision=0)

    with pytest.raises(feature_settings.FeatureSettingsError, match="revision_conflict"):
        feature_settings.update(root, changes={"knowledge_base": False}, expected_revision=0)
    with pytest.raises(feature_settings.FeatureSettingsError, match="unknown_key"):
        feature_settings.update(root, changes={"shell_command": True}, expected_revision=1)


def test_malformed_and_symlink_settings_are_rejected(tmp_path: Path) -> None:
    root = _repo(tmp_path, "repo")
    path = feature_settings.settings_path(root)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(feature_settings.FeatureSettingsError, match="schema_invalid"):
        feature_settings.load(root)

    path.unlink()
    target = tmp_path / "outside.json"
    target.write_text(json.dumps({}), encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(feature_settings.FeatureSettingsError, match="regular_file"):
        feature_settings.load(root)


def test_dashboard_update_stops_and_restarts_source_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stop_daemons: list[Path]
) -> None:
    root = _repo(tmp_path, "repo")
    _stop_daemons.append(root)
    monkeypatch.setattr(core, "repo_root", lambda: root)

    started = core.source_graph_ensure_started()
    assert started["daemon_started"] is True

    disabled = dashboard_mcp_app.settings_update_view(
        {"source_graph": False, "context_graph": True}, 0
    )
    assert disabled["ok"] is True
    assert disabled["features"]["context_graph"] is True
    assert disabled["context_graph_runtime"]["ready"] is True
    assert disabled["source_graph_lifecycle"]["stopped"] is True
    assert core.source_graph_health()["status"] == "disabled"

    loaded = dashboard_mcp_app.settings_view()
    assert loaded["context_graph_runtime"]["ready"] is True
    assert loaded["context_graph_runtime"]["events"] == 0

    enabled = dashboard_mcp_app.settings_update_view({"source_graph": True}, 1)
    assert enabled["ok"] is True
    assert enabled["source_graph_lifecycle"]["daemon_started"] is True
    assert source_graph_daemon.get_daemon(root) is not None


def test_disabled_tool_family_returns_explicit_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path, "repo")
    feature_settings.update(root, changes={"source_graph": False}, expected_revision=0)
    monkeypatch.setenv("AIWORKHUB_REPO_ROOT", str(root))
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))

    result = core.source_graph_refresh_now()
    assert result == {
        "ok": True,
        "status": "disabled",
        "triggered": False,
        "reason": "disabled_by_repository_settings",
        "repo": str(root.resolve()),
    }


def test_dashboard_exposes_and_updates_source_graph_languages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path, "repo")
    source_graph.ensure_ignore_config(root)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(core, "source_graph_refresh_now", lambda: {"ok": True, "triggered": True})

    viewed = dashboard_mcp_app.settings_view()
    policy = viewed["source_graph_policy"]
    assert policy["language_count"] == 34
    assert policy["enabled_count"] == 34

    changed = dashboard_mcp_app.source_graph_settings_update_view(
        {"cpp": False, "json": False},
        policy["revision"],
    )
    assert changed["ok"] is True
    assert changed["enabled_count"] == 32
    assert changed["source_graph_refresh"]["triggered"] is True
    enabled = {row["id"]: row["enabled"] for row in changed["languages"]}
    assert enabled["cpp"] is False
    assert enabled["json"] is False


def test_dashboard_exposes_and_updates_repository_model_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path, "repo")
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setattr(
        vscode_lm_bridge,
        "bridge_readiness",
        lambda *_args, **_kwargs: {
            "launchable": True,
            "blocker_reason": "",
            "observed_models": ["gpt-5.6-sol", "glm-5.3"],
        },
    )

    viewed = dashboard_mcp_app.settings_view()
    model_policy = viewed["model_policy"]
    assert model_policy["revision"] == 0
    assert model_policy["configured"] is False
    assert model_policy["catalog"]["workers"]
    assert all(
        row["effective_enabled"] for row in model_policy["catalog"]["workers"]
    )
    assert model_policy["catalog"]["discovered_model_count"] == 2
    copilot_models = {
        row["model"]
        for row in model_policy["catalog"]["workers"]
        if row["provider"] == "copilot"
    }
    assert {"gpt-5.6-sol", "glm-5.3"}.issubset(copilot_models)

    copilot_disabled = dashboard_mcp_app.model_settings_update_view(
        provider="copilot",
        enabled=False,
        expected_revision=0,
    )
    assert copilot_disabled["ok"] is True
    assert copilot_disabled["providers"] == {"copilot": False}

    reloaded = dashboard_mcp_app.settings_view()["model_policy"]
    copilot_rows = [
        row for row in reloaded["catalog"]["workers"]
        if row["provider"] == "copilot"
    ]
    assert copilot_rows
    assert all(row["effective_enabled"] is False for row in copilot_rows)

    disabled = dashboard_mcp_app.model_settings_update_view(
        provider="zhipu",
        enabled=False,
        expected_revision=1,
    )
    assert disabled["ok"] is True
    assert disabled["revision"] == 2
    assert disabled["providers"] == {"copilot": False, "zhipu": False}

    reloaded = dashboard_mcp_app.settings_view()["model_policy"]
    legacy_zhipu_rows = [
        row for row in reloaded["catalog"]["workers"]
        if row.get("vendor_provider") == "zhipu"
    ]
    assert legacy_zhipu_rows
    assert all(row["effective_enabled"] is False for row in legacy_zhipu_rows)

    conflict = dashboard_mcp_app.model_settings_update_view(
        provider="zhipu",
        enabled=True,
        expected_revision=0,
    )
    assert conflict["ok"] is False
    assert "revision_conflict" in conflict["error"]
    assert conflict["current_revision"] == 2
