from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import project_context  # noqa: E402

MANIFEST = _ROOT / ".aiworkhub/config/development_rules.json"

PATHS_19 = [
    "src/aiworkhub/project_context.py",
    "src/aiworkhub/process_launcher.py",
    "src/aiworkhub/development_rules.py",
    "src/aiworkhub/dashboard.py",
    "src/aiworkhub/skill_registry_store.py",
    "src/aiworkhub/manager_ai_tools.py",
    "tests/test_development_rules_budget_nf01535.py",
    "tests/test_worker_project_context_b434_v1.py",
    "tests/test_dashboard.py",
    "tests/test_learning_commit_store.py",
    "tests/test_skill_selection_vocabulary.py",
    "vscode-extension/extension.js",
    *[f"vscode-extension/test/test_{index}.js" for index in range(7)],
]


def _build(tmp_path: Path, paths: list[str] | None = None) -> dict:
    target = tmp_path / ".aiworkhub/config/development_rules.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(MANIFEST.read_bytes())
    card = {"allowed_writes": list(PATHS_19 if paths is None else paths)}
    return project_context._development_rules_section(tmp_path, card, workspace_root=tmp_path)


def test_cap_declared_and_sum_fits_bundle() -> None:
    caps = project_context.TOOL_CAPS
    assert caps["development_rules"]["bytes"] == 16384
    assert sum(cap["bytes"] for cap in caps.values()) <= project_context.MAX_BUNDLE_BYTES


def test_nineteen_path_mixed_language_card_builds(tmp_path: Path) -> None:
    assert len(PATHS_19) == 19
    section = _build(tmp_path)
    cap = project_context.TOOL_CAPS["development_rules"]["bytes"]
    assert len(section["content"].encode("utf-8")) <= cap
    payload = json.loads(section["content"])
    assert "degraded" not in payload
    assert section["truncated"] is False
    assert sum(len(context["paths"]) for context in payload["contexts"]) == 19


def test_overflow_degrades_to_rule_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    full = json.loads(_build(tmp_path)["content"])
    ids = {rule["id"] for rule in full["rules"]}
    full_size = len(json.dumps(full, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    bare = {**full, "degraded": True, "omitted_requirement_rule_ids": sorted(ids),
            "rules": [{**rule, "requirements": None} for rule in full["rules"]]}
    bare_size = len(json.dumps(bare, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    assert bare_size < full_size
    cap = (full_size + bare_size) // 2
    monkeypatch.setitem(project_context.TOOL_CAPS, "development_rules", {"bytes": cap, "rows": 64})

    section = _build(tmp_path)

    assert len(section["content"].encode("utf-8")) <= cap
    assert section["truncated"] is True
    payload = json.loads(section["content"])
    assert payload["degraded"] is True
    omitted = payload["omitted_requirement_rule_ids"]
    assert omitted and omitted == sorted(omitted) and set(omitted) <= ids
    assert {rule["id"] for rule in payload["rules"]} == ids
    for rule in payload["rules"]:
        assert (rule["requirements"] is None) == (rule["id"] in omitted)
    assert sum(len(context["paths"]) for context in payload["contexts"]) == 19


def test_ids_only_overflow_still_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(project_context.TOOL_CAPS, "development_rules", {"bytes": 1024, "rows": 64})
    with pytest.raises(project_context.ProjectContextError, match="development_rules_section_budget_overflow"):
        _build(tmp_path, paths=PATHS_19[:2])
