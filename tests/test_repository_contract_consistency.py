"""Repository-root regression gate for NF-2026-00631.

Hand-syncing the managed AIWORKHUB_TOOL_USE_POLICY block let AGENTS.md,
CLAUDE.md and .github/copilot-instructions.md drift from
``agent_tool_instructions.render_projection`` while staying green. This test
runs the same check against the real repository tree so future drift fails
CI instead of only failing inside a synthetic ``tmp_path`` fixture.
"""

from __future__ import annotations

from pathlib import Path

from aiworkhub import agent_tool_instructions as instructions
from aiworkhub import evidence_instruments as evidence

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_repository_root_carriers_match_generated_projections() -> None:
    report = evidence.contract_consistency_check(REPO_ROOT)
    assert report["status"] == "pass"
    assert report["blocking"] is False
    assert report["blockers"] == []
    for provider in instructions.PROVIDERS:
        carrier = report["carriers"][provider]
        assert carrier["managed"] is True
        assert carrier["exact"] is True
        assert carrier["observed_sha256"] == carrier["expected_sha256"]


def test_repository_root_regression_fails_on_projection_drift(tmp_path: Path) -> None:
    for provider in instructions.PROVIDERS:
        source = REPO_ROOT / provider
        destination = tmp_path / provider
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    clean_report = evidence.contract_consistency_check(tmp_path)
    assert clean_report["status"] == "pass"

    drifted_provider = "CLAUDE.md"
    drifted_path = tmp_path / drifted_provider
    drifted_text = drifted_path.read_text(encoding="utf-8")
    corrupted = drifted_text.replace(
        "Stop at Codex review.", "Stop at Codex review, unless owner overrides."
    )
    assert corrupted != drifted_text
    drifted_path.write_text(corrupted, encoding="utf-8")

    drifted_report = evidence.contract_consistency_check(tmp_path)
    assert drifted_report["status"] == "fail"
    assert drifted_report["blocking"] is True
    assert f"projection_drift:{drifted_provider}" in drifted_report["blockers"]
    assert drifted_report["carriers"][drifted_provider]["exact"] is False
    for provider in instructions.PROVIDERS:
        if provider != drifted_provider:
            assert drifted_report["carriers"][provider]["exact"] is True
