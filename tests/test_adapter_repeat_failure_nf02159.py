"""NF-2026-02159 / NF-2026-02146: a vscode_lm candidate that already failed this card is skipped."""

from __future__ import annotations

import pytest

from aiworkhub import adapter_capability, process_launcher

_SIGNATURE = "a" * 64


def _card(**overrides):
    card = {
        "runner": "claude_sonnet-5",
        "topic": "nf02159",
        "validation": ["python -m pytest -q"],
        "adapter_id": "vscode_lm",
    }
    card.update(overrides)
    return card


def _reworked_after_validation_failures(count: int) -> dict:
    """A card validation_auto_rework returned to pending after ``count`` red attempts."""
    return _card(
        rework_predecessor={
            "schema_id": "aiworkhub.rework_predecessor.v1",
            "request_id": "req-last",
            "terminal_substatus": "validation_failed",
        },
        review_feedback={
            "schema_id": "aiworkhub.rework_feedback_delta.v1",
            "instruction": f"validation_auto_rework signature={_SIGNATURE} auto_return={count}\n"
            "reason_code=validation_auto_rework actor=aiworkhub_validation_auto_rework",
        },
    )


def _worker_failed(error: str) -> dict:
    return _card(
        terminal_substatus="worker_failed",
        terminal_review={
            "substatus": "worker_failed",
            "evidence": {"adapter_id": "vscode_lm", "error": error},
        },
    )


@pytest.fixture(autouse=True)
def _bridge_ready(monkeypatch):
    monkeypatch.setattr(
        process_launcher.vscode_lm_bridge, "derived_candidate_blocker", lambda repo, candidate, model: ""
    )


def _derive(tmp_path, card):
    return process_launcher.derive_launch_identity(
        tmp_path, card, runner="claude_sonnet-5", topic="nf02159"
    )


# NF-2026-01343: the claude_* tuple now leads with claude_cli, so derivation
# stops at the first pinnable candidate and never evaluates vscode_lm at all.
# Whether vscode_lm WOULD be rejected for a card's history is pinned directly
# on adapter_capability.reject_incapable below; these tests pin what derivation
# really records.


def test_two_consecutive_validation_failures_derive_claude_cli(tmp_path):
    identity = _derive(tmp_path, _reworked_after_validation_failures(2))
    assert identity["adapter_id"] == "claude_cli"
    assert identity["adapter_rejections"] == []


def test_third_validation_failure_also_derives_claude_cli(tmp_path):
    identity = _derive(tmp_path, _reworked_after_validation_failures(3))
    assert identity["adapter_id"] == "claude_cli"


def test_terminal_review_evidence_derives_claude_cli(tmp_path):
    card = _card(
        terminal_substatus="validation_failed",
        terminal_review={
            "substatus": "validation_failed",
            "evidence": {
                "adapter_id": "vscode_lm",
                "validation_auto_rework": {"decision": "auto_return", "prior_auto_returns": 1},
            },
        },
    )
    identity = _derive(tmp_path, card)
    assert identity["adapter_id"] == "claude_cli"
    assert identity["adapter_rejections"] == []


def test_single_validation_failure_derives_claude_cli(tmp_path):
    identity = _derive(tmp_path, _reworked_after_validation_failures(1))
    assert identity["adapter_id"] == "claude_cli"
    assert not any("repeated_validation_failure" in r for r in identity["adapter_rejections"])


def test_manager_rework_without_auto_return_header_derives_claude_cli(tmp_path):
    card = _reworked_after_validation_failures(2)
    card["review_feedback"] = {"instruction": "please fix the failing test"}
    assert _derive(tmp_path, card)["adapter_id"] == "claude_cli"


def test_card_wide_validation_failures_derive_claude_cli(tmp_path):
    card = _reworked_after_validation_failures(2)
    card["adapter_id"] = "claude_cli"
    identity = _derive(tmp_path, card)
    assert identity["adapter_id"] == "claude_cli"
    assert identity["adapter_rejections"] == []


def test_claude_cli_stays_derived_after_switch_to_claude_cli(tmp_path):
    card = _reworked_after_validation_failures(3)
    card["adapter_id"] = "claude_cli"
    card["terminal_substatus"] = "validation_failed"
    card["terminal_review"] = {
        "substatus": "validation_failed",
        "evidence": {"adapter_id": "claude_cli"},
    }
    identity = _derive(tmp_path, card)
    assert identity["adapter_id"] == "claude_cli"
    assert identity["adapter_rejections"] == []


@pytest.mark.parametrize(
    "error",
    [
        "vscode_lm_semantic_edit_stage_required",
        "coordinates_shifted_requires_fresh_worker_pair",
    ],
)
def test_vscode_lm_staging_failure_retry_derives_claude_cli(tmp_path, error):
    identity = _derive(tmp_path, _worker_failed(error))
    assert identity["adapter_id"] == "claude_cli"
    assert identity["adapter_rejections"] == []


def test_staging_failure_recorded_in_substatus_is_recognised(tmp_path):
    card = _card(terminal_substatus="worker_failed:vscode_lm_semantic_edit_stage_required")
    identity = _derive(tmp_path, card)
    assert identity["adapter_id"] == "claude_cli"


def test_other_worker_failure_derives_claude_cli(tmp_path):
    identity = _derive(tmp_path, _worker_failed("vscode_lm_response_timeout"))
    assert identity["adapter_id"] == "claude_cli"


def test_staging_marker_without_worker_failed_derives_claude_cli(tmp_path):
    card = _worker_failed("vscode_lm_semantic_edit_stage_required")
    card["terminal_substatus"] = "review_ready"
    card["terminal_review"]["substatus"] = "review_ready"
    assert _derive(tmp_path, card)["adapter_id"] == "claude_cli"


def test_clean_card_derives_claude_cli(tmp_path):
    identity = _derive(tmp_path, _card())
    assert identity["adapter_id"] == "claude_cli"
    assert identity["adapter_rejections"] == []


def test_reject_incapable_is_pure_and_records_one_rejection():
    card = _reworked_after_validation_failures(2)
    snapshot = repr(card)
    rejections: list[str] = []
    assert adapter_capability.reject_incapable(card, "vscode_lm", rejections) is True
    assert rejections == ["vscode_lm:repeated_validation_failure"]
    assert repr(card) == snapshot


def test_capability_blocker_takes_precedence_and_adds_a_single_rejection():
    card = _reworked_after_validation_failures(2)
    card["validation"] = ["cmake --build build"]
    rejections: list[str] = []
    assert adapter_capability.reject_incapable(card, "vscode_lm", rejections) is True
    assert rejections == ["vscode_lm:capability:native_toolchain:cmake"]


def test_non_vscode_lm_candidate_is_never_history_rejected():
    card = _reworked_after_validation_failures(2)
    card["adapter_id"] = "claude_cli"
    rejections: list[str] = []
    assert adapter_capability.reject_incapable(card, "claude_cli", rejections) is False
    assert rejections == []


@pytest.mark.parametrize(
    "error",
    ["vscode_lm_semantic_edit_stage_required", "coordinates_shifted_requires_fresh_worker_pair"],
)
def test_reject_incapable_records_the_vscode_lm_staging_failure(error):
    # NF-2026-01343: derivation no longer reaches vscode_lm for claude_* runners,
    # so the staging-failure cause is pinned on reject_incapable directly.
    rejections: list[str] = []
    card = _worker_failed(error)
    assert adapter_capability.reject_incapable(card, "vscode_lm", rejections) is True
    assert rejections == ["vscode_lm:vscode_lm_staging_failure"]


def test_reject_incapable_keeps_vscode_lm_after_another_worker_failure():
    rejections: list[str] = []
    card = _worker_failed("vscode_lm_response_timeout")
    assert adapter_capability.reject_incapable(card, "vscode_lm", rejections) is False
    assert rejections == []
