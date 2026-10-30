"""NF-2026-01851: file mode says why an exact path has no row."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository

TOOL_SOURCE = "".join(f"# line {i}\n" for i in range(1, 20)) + (
    "def cmd_run():\n    return 1\n"
)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "field"
    root.mkdir()
    bootstrap_repository(root, repo_name="field")
    (root / "scripts").mkdir()
    (root / "scripts" / "tool.py").write_text(TOOL_SOURCE, encoding="utf-8")
    (root / "runtime").mkdir()
    (root / "runtime" / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (root / "skipme").mkdir()
    (root / "skipme" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "generated").mkdir()
    (root / "generated" / "b.py").write_text("y = 1\n", encoding="utf-8")
    (root / "notes.txt").write_text("plain notes\n", encoding="utf-8")
    config = sg.ignore_config_path(root)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps({
            "schema_id": sg.POLICY_SCHEMA_ID,
            "revision": 1,
            "exclude_dirs": ["skipme"],
            "exclude_globs": ["generated/**"],
            "disabled_languages": [],
        }),
        encoding="utf-8",
    )
    sg.build_index(root, incremental=False)
    return root


def _resolved_path(result: dict) -> str:
    assert result["contexts"], result
    return result["contexts"][0]["file_path"]


@pytest.mark.parametrize("spelling", ["scripts/tool.py", "scripts\\tool.py", "./scripts/tool.py"])
def test_spellings_resolve_to_the_indexed_row(repo, spelling):
    result = sg.file_query(repo, spelling, line_range=(20, 21))
    assert _resolved_path(result) == "scripts/tool.py"
    assert "cmd_run" in result["contexts"][0]["source_preview"]
    assert "reason" not in result


def test_absolute_in_repo_path_resolves(repo):
    result = sg.file_query(repo, str(repo.resolve() / "scripts" / "tool.py"))
    assert _resolved_path(result) == "scripts/tool.py"


def test_case_variant_resolves_on_case_insensitive_filesystem(repo, monkeypatch):
    monkeypatch.setattr(sg, "_filesystem_case_insensitive", lambda _root: True)
    result = sg.file_query(repo, "Scripts/Tool.py")
    assert _resolved_path(result) == "scripts/tool.py"
    monkeypatch.setattr(sg, "_filesystem_case_insensitive", lambda _root: False)
    assert sg.file_query(repo, "Scripts/Tool.py")["contexts"] == []


def test_typo_is_not_found(repo):
    result = sg.file_query(repo, "scripts/tooll.py", line_range=(1, 5))
    assert result["found"] is False
    assert result["reason"] == "not_found"
    assert result["exists_on_disk"] is False
    assert result["contexts"] == [] and result["matches"] == []
    assert "source_preview" not in result


def test_path_outside_repo_is_not_found(repo, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n", encoding="utf-8")
    result = sg.file_query(repo, "../outside.py")
    assert result["reason"] == "not_found"
    assert result["exists_on_disk"] is False
    assert "source_preview" not in result


@pytest.mark.parametrize(
    ("path", "kind", "rule"),
    [
        ("notes.txt", "suffix", ".txt"),
        ("skipme/a.py", "directory", "skipme"),
        ("generated/b.py", "glob", "generated/**"),
    ],
)
def test_excluded_path_names_the_rule_and_serves_no_content(repo, path, kind, rule):
    result = sg.file_query(repo, path, line_range=(1, 1))
    assert result["found"] is False
    assert result["reason"] == "excluded_by_policy"
    assert result["rule_kind"] == kind
    assert result["rule"] == rule
    assert result["exists_on_disk"] is True
    assert result["contexts"] == [] and result["matches"] == []
    assert "source_preview" not in result


def test_disabled_language_is_named(repo):
    config = sg.ignore_config_path(repo)
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["disabled_languages"] = ["python"]
    config.write_text(json.dumps(payload), encoding="utf-8")
    result = sg.file_query(repo, "scripts/other.py")
    assert result["reason"] == "not_found"
    (repo / "scripts" / "other.py").write_text("z = 1\n", encoding="utf-8")
    result = sg.file_query(repo, "scripts/other.py")
    assert result["reason"] == "excluded_by_policy"
    assert result["rule_kind"] == "language"
    assert result["rule"] == "python"


def test_admitted_unindexed_path_serves_bounded_window_marked_unindexed(repo):
    fresh = repo / "scripts" / "fresh.py"
    fresh.write_text("".join(f"row {i}\n" for i in range(1, 60)), encoding="utf-8")
    result = sg.file_query(repo, "scripts\\fresh.py", line_range=(30, 32))
    assert result["found"] is False
    assert result["reason"] == "not_yet_indexed"
    assert result["exists_on_disk"] is True
    assert result["freshness"] == "unindexed"
    assert result["source_preview_line_start"] <= 30
    assert result["source_preview_line_end"] >= 32
    assert "row 31" in result["source_preview"]
    assert result["contexts"] == []


def test_admitted_unindexed_path_without_range_serves_no_content(repo):
    (repo / "scripts" / "fresh.py").write_text("a = 1\n", encoding="utf-8")
    result = sg.file_query(repo, "scripts/fresh.py")
    assert result["reason"] == "not_yet_indexed"
    assert "source_preview" not in result
