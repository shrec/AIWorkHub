"""Codex/Claude CLI model discovery: reads provider-owned state, never a live call."""

from __future__ import annotations

import json
from pathlib import Path

from aiworkhub import cli_model_discovery


def _write_cache(home: Path, payload: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "models_cache.json").write_text(json.dumps(payload), encoding="utf-8")


def test_codex_models_lists_only_visibility_list_entries_in_priority_order(tmp_path: Path):
    _write_cache(
        tmp_path,
        {
            "fetched_at": "2026-09-23T00:00:00Z",
            "models": [
                {"slug": "gpt-6-sol", "display_name": "GPT-6 Sol", "visibility": "list", "priority": 2},
                {"slug": "gpt-5.3-codex", "display_name": "GPT-5.3 Codex", "visibility": "hide", "priority": 0},
                {"slug": "gpt-6-astra", "display_name": "GPT-6 Astra", "visibility": "list", "priority": 0},
            ],
        },
    )

    models = cli_model_discovery.codex_models(home=tmp_path)

    assert [entry["model"] for entry in models] == ["gpt-6-astra", "gpt-6-sol"]
    assert models[0] == {"model": "gpt-6-astra", "label": "GPT-6 Astra", "priority": 0}


def test_codex_models_is_empty_for_a_missing_cache(tmp_path: Path):
    assert cli_model_discovery.codex_models(home=tmp_path / "does-not-exist") == []


def test_codex_models_is_empty_for_a_corrupt_cache(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "models_cache.json").write_text("{not json", encoding="utf-8")

    assert cli_model_discovery.codex_models(home=tmp_path) == []


def test_codex_models_falls_back_to_the_slug_when_display_name_is_missing(tmp_path: Path):
    _write_cache(tmp_path, {"models": [{"slug": "gpt-5.5", "visibility": "list"}]})

    models = cli_model_discovery.codex_models(home=tmp_path)

    assert models == [{"model": "gpt-5.5", "label": "gpt-5.5", "priority": None}]


def test_codex_models_honours_codex_home_from_the_environment(tmp_path: Path, monkeypatch):
    codex_home = tmp_path / "custom-codex-home"
    _write_cache(
        codex_home,
        {"models": [{"slug": "gpt-6-luna", "display_name": "GPT-6 Luna", "visibility": "list", "priority": 0}]},
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    models = cli_model_discovery.codex_models()

    assert [entry["model"] for entry in models] == ["gpt-6-luna"]


def test_record_claude_resolution_round_trips_into_claude_models(tmp_path: Path):
    cli_model_discovery.record_claude_resolution(tmp_path, "opus", "claude-opus-5")

    models = cli_model_discovery.claude_models(tmp_path)

    resolved = {entry["model"]: entry["label"] for entry in models}
    assert resolved["opus"] == "claude-opus-5 (opus)"
    assert resolved["sonnet"] == "sonnet", "an alias never observed resolving falls back to its bare name"
    assert [entry["model"] for entry in models] == list(cli_model_discovery.CLAUDE_CLI_ALIASES)


def test_record_claude_resolution_ignores_a_non_alias_model(tmp_path: Path):
    cli_model_discovery.record_claude_resolution(tmp_path, "claude-opus-5-pinned", "claude-opus-5")

    assert not (tmp_path / ".aiworkhub" / "config" / "claude_model_resolutions.json").exists()


def test_claude_models_is_the_bare_alias_list_for_a_missing_resolutions_file(tmp_path: Path):
    models = cli_model_discovery.claude_models(tmp_path / "does-not-exist")

    assert models == [{"model": alias, "label": alias} for alias in cli_model_discovery.CLAUDE_CLI_ALIASES]


def test_claude_models_is_the_bare_alias_list_for_a_corrupt_resolutions_file(tmp_path: Path):
    config_dir = tmp_path / ".aiworkhub" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "claude_model_resolutions.json").write_text("not json", encoding="utf-8")

    models = cli_model_discovery.claude_models(tmp_path)

    assert models == [{"model": alias, "label": alias} for alias in cli_model_discovery.CLAUDE_CLI_ALIASES]
