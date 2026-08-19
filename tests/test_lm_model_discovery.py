"""End-to-end regression for editor-discovered model vocabulary.

Every assertion is driven from a discovery result -- the output of
``discover_callable_model_names`` over a fabricated ``selectChatModels`` report
-- never from a literal list of the six GLM ids. That keeps this test from
re-introducing the hardcoding the card removes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    repository_state,
    runtime_adapters,
    task_store,
    vscode_lm_bridge,
    workforce_catalog,
)

_EXTENSION_JS = Path(__file__).resolve().parents[1] / "vscode-extension" / "extension.js"

# The six working GLM models the owner's Copilot custom endpoint (Z.AI,
# vendor=customendpoint) exposes.  They are the *report* the editor emits, not
# an allowlist the assertions read back: discovery is what recovers them.
_GLM_IDS = ["glm-5.3", "glm-5.2", "glm-5.1", "glm-5", "glm-5-turbo", "glm-4.7"]


def _editor_report() -> list[dict[str, str]]:
    reported = [
        {"id": mid, "family": mid, "vendor": "customendpoint", "name": mid.upper()}
        for mid in _GLM_IDS
    ]
    # Entries the measured non-callable filters exclude, plus a malformed id the
    # requested-name regex must reject.
    reported += [
        {"id": "claude-haiku-4.5", "family": "claude-haiku-4.5", "vendor": "copilotcli"},
        {"id": "claude-sonnet-5", "family": "claude-sonnet-5", "vendor": "claude-code"},
        {"id": "copilot-utility", "family": "copilot-utility", "vendor": "copilot"},
        {"id": "bad model!", "family": "bad model!", "vendor": "customendpoint"},
    ]
    return reported


def _discovered() -> list[str]:
    discovered = runtime_adapters.discover_callable_model_names(_editor_report())
    # Discovery is the source: exactly the six callable GLM ids survive.
    assert sorted(discovered) == sorted(_GLM_IDS)
    return discovered


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    return repo


def _config_repo(tmp_path: Path) -> Path:
    root = tmp_path / "cfg"
    (root / ".aiworkhub/config").mkdir(parents=True)
    (root / ".aiworkhub/project.json").write_text("{}\n", encoding="utf-8")
    return root


def test_discovery_recovers_only_callable_regex_valid_names() -> None:
    discovered = _discovered()
    assert len(discovered) == 6
    # A brand-new endpoint model appears purely from the report, no code change.
    novel = runtime_adapters.discover_callable_model_names(
        [{"id": "glm-6-preview", "family": "glm-6-preview", "vendor": "customendpoint"}]
    )
    assert novel == ["glm-6-preview"]


def test_all_six_glm_ids_resolve_from_a_discovery_result() -> None:
    discovered = _discovered()
    for name in discovered:
        resolved, error = runtime_adapters.resolve_glm_model(name, discovered)
        assert error is None
        assert resolved == name


def test_unreported_model_is_refused_by_name_with_reason() -> None:
    discovered = _discovered()
    resolved, error = runtime_adapters.resolve_glm_model("glm-9.9", discovered)
    assert resolved is None
    assert "glm-9.9" in error
    assert "not_reported_by_editor" in error


def test_resolve_without_discovery_gates_family_and_names_cold_start() -> None:
    ok, error = runtime_adapters.resolve_glm_model("glm-5.3")
    assert (ok, error) == ("glm-5.3", None)
    rejected, reason = runtime_adapters.resolve_glm_model("gpt-5.5")
    assert rejected is None
    assert "not_glm_family_or_malformed" in reason
    default, error = runtime_adapters.resolve_glm_model(None)
    assert error is None
    assert default == runtime_adapters.GLM_COLD_START_FALLBACK_MODEL


def test_non_callable_filters_are_preserved() -> None:
    assert runtime_adapters.editor_model_is_callable(
        vendor="customendpoint", model_id="glm-5.3", family="glm-5.3"
    )
    assert not runtime_adapters.editor_model_is_callable(vendor="copilotcli", model_id="x", family="x")
    assert not runtime_adapters.editor_model_is_callable(vendor="claude-code", model_id="x", family="x")
    assert not runtime_adapters.editor_model_is_callable(
        vendor="copilot", model_id="copilot-utility-2", family="copilot-utility-2"
    )
    assert runtime_adapters.editor_requested_name_ok("glm-5-turbo")
    assert not runtime_adapters.editor_requested_name_ok("bad model!")


def test_bridge_readiness_resolves_every_discovered_glm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "bridge"
    monkeypatch.setenv(vscode_lm_bridge.BRIDGE_ROOT_ENV, str(root))
    repo = _repo(tmp_path)
    repo_id = repository_state.inspect_repository(repo).manifest.repo_id
    discovered = _discovered()
    host = root / "hosts" / repo_id / "window_test.json"
    vscode_lm_bridge._atomic_json(  # noqa: SLF001 - contract-level host fixture
        host,
        {
            "schema_id": vscode_lm_bridge.HOST_SCHEMA_ID,
            "repo_id": repo_id,
            "window_id": "window_test",
            "models": discovered,
            "model_metadata": [
                {"canonical": m, "id": m, "family": m, "access_state": "unknown"}
                for m in discovered
            ],
            "permission_granted": False,
        },
    )
    for name in discovered:
        ready = vscode_lm_bridge.bridge_readiness(
            repo, model=name, adapter_id="glm_vscode_lm"
        )
        assert ready["launchable"] is True
        assert ready["resolved_model"] == name
    unseen = vscode_lm_bridge.bridge_readiness(
        repo, model="glm-9.9", adapter_id="glm_vscode_lm"
    )
    assert unseen["launchable"] is False
    assert unseen["blocker_reason"] == "vscode_lm_model_not_visible"
    assert unseen["model"] == "glm-9.9"


def test_workforce_catalog_carries_one_row_per_discovered_model(tmp_path: Path) -> None:
    root = _config_repo(tmp_path)
    discovered = _discovered()
    snapshot = workforce_catalog.build_catalog(
        root,
        cards=[],
        process_rows=[],
        preflight={
            "providers": [
                {
                    "adapter_id": "glm_vscode_lm",
                    "launchable": True,
                    "status": "ready",
                    "access_observed": True,
                    "observed_models": discovered,
                }
            ]
        },
    )
    glm_rows = [row for row in snapshot["workers"] if row["provider"] == "zhipu"]
    # One row per discovered model, not a single seeded glm-5.2 row.
    assert {row["model"] for row in glm_rows} == set(discovered)
    assert len(glm_rows) == len(discovered)
    assert all(row.get("discovered_from_editor") for row in glm_rows)
    assert all(row["available"] for row in glm_rows)


def test_single_vocabulary_declaration_is_shared_across_consumers() -> None:
    # The Python consumers reference the ONE declaration object.
    assert (
        vscode_lm_bridge.EDITOR_REQUESTED_MODEL_RE
        is runtime_adapters.EDITOR_REQUESTED_MODEL_RE
    )
    assert (
        vscode_lm_bridge.discover_callable_model_names
        is runtime_adapters.discover_callable_model_names
    )
    assert (
        vscode_lm_bridge.GLM_COLD_START_FALLBACK_MODEL
        == runtime_adapters.GLM_COLD_START_FALLBACK_MODEL
    )
    assert workforce_catalog.runtime_adapters is runtime_adapters
    # The VS Code extension mirror must not drift from the Python declaration.
    js = _EXTENSION_JS.read_text(encoding="utf-8")
    for vendor in runtime_adapters.EDITOR_NONCALLABLE_VENDORS:
        assert f'"{vendor}"' in js
    for prefix in runtime_adapters.EDITOR_NONCALLABLE_ID_PREFIXES:
        assert f'"{prefix}"' in js
    expected_regex = runtime_adapters.EDITOR_REQUESTED_MODEL_RE.pattern.replace("/", r"\/")
    assert expected_regex in js
    assert "vscodeLmDiscoverCallableNames(models)" in js
