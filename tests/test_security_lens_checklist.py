from __future__ import annotations

import pytest

from aiworkhub import quality_reviewer

_CHECKLIST_CLASSES = (
    "Path traversal and path normalization",
    "Subprocess argument injection",
    "Secrets in logs, receipts, events or error messages",
    "Route and tool authorization",
)


def _packet() -> dict:
    body = {
        "candidate": {
            "scoped_audits": {
                lens: {
                    "known_unknowns": [],
                    "packet": {"changed_paths": [{"path": "src/module.py"}]},
                }
                for lens in ("correctness", "security", "code_quality")
            },
            "path": "src/module.py",
        },
    }
    return {"packet_sha256": quality_reviewer._canonical_digest(body), **body}


def _prompt(lens: str) -> str:
    return quality_reviewer.build_review_prompt(_packet(), lens=lens)


def test_checklist_is_a_str_constant_naming_all_four_classes() -> None:
    checklist = quality_reviewer.SECURITY_LENS_CHECKLIST
    assert isinstance(checklist, str)
    for name in _CHECKLIST_CLASSES:
        assert name in checklist


def test_security_prompt_contains_checklist_once_right_after_lens_line() -> None:
    prompt = _prompt("security")
    checklist = quality_reviewer.SECURITY_LENS_CHECKLIST
    assert prompt.count(checklist) == 1
    assert "Review lens: security.\n" + checklist in prompt


@pytest.mark.parametrize("lens", ["correctness", "code_quality"])
def test_other_lenses_omit_checklist_and_keep_lens_line_adjacent(lens: str) -> None:
    prompt = _prompt(lens)
    assert quality_reviewer.SECURITY_LENS_CHECKLIST not in prompt
    for name in _CHECKLIST_CLASSES:
        assert name not in prompt
    assert f"Review lens: {lens}.\nJudge the candidate" in prompt


def test_removing_checklist_from_security_prompt_restores_the_plain_layout() -> None:
    prompt = _prompt("security")
    stripped = prompt.replace(quality_reviewer.SECURITY_LENS_CHECKLIST, "", 1)
    assert "Review lens: security.\nJudge the candidate" in stripped
    assert len(stripped) == len(prompt) - len(quality_reviewer.SECURITY_LENS_CHECKLIST)
