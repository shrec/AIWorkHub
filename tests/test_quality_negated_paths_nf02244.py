"""NF-2026-02244: declared quality checks support '!' negated path patterns."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiworkhub import quality_evidence as qe

REPO_ROOT = Path(__file__).resolve().parents[1]


def _check(paths: list[str] | None) -> dict:
    entry: dict = {"id": "probe", "kind": "test", "command": ["npm", "test"]}
    if paths is not None:
        entry["paths"] = paths
    return entry


def _extension_check() -> dict:
    config = json.loads((REPO_ROOT / ".aiworkhub" / "quality.json").read_text(encoding="utf-8"))
    (entry,) = [c for c in config["checks"] if c["id"] == "extension-regression"]
    return entry


def _applicable(entry: dict, changed: tuple[str, ...]) -> tuple[bool, str]:
    return qe._declared_check_applicability(
        entry, changed_paths=changed, effective_risk_tier=qe.RISK_LOW
    )


def test_repo_extension_check_validates() -> None:
    entry = _extension_check()
    qe._validate_declared_check(entry)
    assert "!vscode-extension/**/*.md" in entry["paths"]
    assert "!vscode-extension/*.md" in entry["paths"]


def test_markdown_only_change_skips_extension_regression() -> None:
    entry = _extension_check()
    assert _applicable(entry, ("vscode-extension/README.md",)) == (
        False,
        qe.APPLICABILITY_SKIP_CHANGED_PATHS,
    )
    assert _applicable(entry, ("README.md", "vscode-extension/README.md")) == (
        False,
        qe.APPLICABILITY_SKIP_CHANGED_PATHS,
    )
    assert _applicable(entry, ("vscode-extension/docs/guide.md",))[0] is False


def test_source_change_makes_extension_regression_applicable() -> None:
    entry = _extension_check()
    assert _applicable(entry, ("vscode-extension/README.md", "vscode-extension/src/x.js")) == (
        True,
        "",
    )
    assert _applicable(entry, ("vscode-extension\\src\\x.js",)) == (True, "")


def test_negated_only_paths_is_malformed() -> None:
    with pytest.raises(qe.MalformedConfigError):
        qe._validate_declared_check(_check(["!docs/**", "!*.md"]))


@pytest.mark.parametrize("pattern", ["!", "!/abs/**", "!../escape/**", "!a/../b"])
def test_negated_pattern_safety_checks_apply_after_strip(pattern: str) -> None:
    with pytest.raises(qe.MalformedConfigError):
        qe._validate_declared_check(_check(["src/**", pattern]))


def test_patterns_without_negation_behave_as_before() -> None:
    entry = _check(["src/*.py", "tests/**"])
    qe._validate_declared_check(entry)
    assert _applicable(entry, ("src/a.py",)) == (True, "")
    assert _applicable(entry, ("docs/a.md",)) == (False, qe.APPLICABILITY_SKIP_CHANGED_PATHS)
    assert _applicable(entry, ()) == (True, "")
    assert _applicable(_check(None), ("docs/a.md",)) == (True, "")
