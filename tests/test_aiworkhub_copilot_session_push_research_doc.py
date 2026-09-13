"""Assert the AIWorkHub Copilot exact-session research report exists and is well-formed.

This is the only test/code artifact produced by the research task
``DEEPSEEK_AIWORKHUB_COPILOT_SESSION_PUSH_RESEARCH_V4``. It verifies that the
markdown report exists and contains the four required section headers:

    ## Findings
    ## Verified claims
    ## Unverified or rejected claims
    ## Recommendation
"""

from pathlib import Path

REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "research"
    / "AIWORKHUB_COPILOT_EXACT_SESSION_PUSH_2026-09-12.md"
)

REQUIRED_SECTIONS = (
    "## Findings",
    "## Verified claims",
    "## Unverified or rejected claims",
    "## Recommendation",
)


def test_report_exists() -> None:
    assert REPORT_PATH.is_file(), f"report missing: {REPORT_PATH}"


def test_report_contains_required_sections() -> None:
    text = REPORT_PATH.read_text(encoding="utf-8")
    for section in REQUIRED_SECTIONS:
        assert section in text, f"missing required section header: {section}"
