"""Review independence is a recorded ladder, not a vendor check.

The ladder degrades ``cross_provider`` -> ``cross_model_same_provider`` ->
``same_model_fresh_context``.  It never refuses on provider identity, so a
single-provider (or single-model) installation still completes a review, and the
rung used is recorded on the acceptance evidence.  These tests drive the exact
functions the reviewer launch path in ``process_launcher`` calls
(``quality_review.resolve_independence_rung`` /
``quality_review.independence_acceptance_line`` /
``quality_review.assemble_reviewer_prompt`` and
``runtime_adapters.provider_for_adapter``), one assertion per rung, plus the
anti-anchoring / content-delivery invariants that must hold on every rung.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from aiworkhub import quality_review as qr
from aiworkhub import quality_reviewer, runtime_adapters

CANDIDATE_PATH = "src/aiworkhub/quality_review.py"
TASK_ID = "NF-2026-00278-UNJAM-REVIEW-PATH"


def _rung(worker_adapter: str, reviewer_adapter: str) -> dict[str, object]:
    """Resolve a rung exactly as the launch path does: provider by adapter,
    model surface by adapter route."""

    return qr.resolve_independence_rung(
        worker_provider=runtime_adapters.provider_for_adapter(worker_adapter),
        reviewer_provider=runtime_adapters.provider_for_adapter(reviewer_adapter),
        worker_model=worker_adapter,
        reviewer_model=reviewer_adapter,
    )


def _scoped_audit(lens: str, paths: list[str]) -> dict[str, object]:
    payload = {
        "task_id": TASK_ID,
        "review_lens": {"lens_kind": lens},
        "changed_paths": [{"path": path} for path in paths],
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_id": "aiworkhub.scoped_audit.v1",
        "fingerprint": fingerprint,
        "packet": payload,
    }


def _packet(lens: str = "correctness") -> dict[str, object]:
    digest = hashlib.sha256(b"candidate-bytes").hexdigest()
    return quality_reviewer.build_review_packet(
        request_id="req-1",
        task_id=TASK_ID,
        claim_epoch=1,
        worker_provider="claude",
        changed_path_hashes={CANDIDATE_PATH: digest},
        objective="wire packet delivery and record the independence ladder",
        acceptance=["the rung used is recorded on the acceptance evidence"],
        required_outputs=[CANDIDATE_PATH],
        validation=["python3 -m pytest -q"],
        scoped_audits={lens: _scoped_audit(lens, [CANDIDATE_PATH])},
    )


# --- The ladder shape --------------------------------------------------------


def test_ladder_is_best_available_first() -> None:
    assert qr.INDEPENDENCE_LADDER == (
        "cross_provider",
        "cross_model_same_provider",
        "same_model_fresh_context",
    )


# --- Rung 1: cross_provider --------------------------------------------------


def test_cross_provider_rung_is_recorded() -> None:
    record = _rung("claude_cli", "glm_vscode_lm")
    assert record["rung"] == qr.RUNG_CROSS_PROVIDER
    assert record["rung_index"] == 0
    assert record["cross_provider"] is True
    assert record["fresh_context_required"] is False
    # Recorded on the acceptance evidence.
    assert "cross_provider" in qr.independence_acceptance_line(record)


# --- Rung 2: cross_model_same_provider ---------------------------------------


def test_cross_model_same_provider_rung_is_recorded() -> None:
    # Both GLM routes are one provider but different model surfaces.
    assert runtime_adapters.provider_for_adapter("glm_vscode_lm") == "glm"
    assert runtime_adapters.provider_for_adapter("glm_copilot_cli") == "glm"
    record = _rung("glm_copilot_cli", "glm_vscode_lm")
    assert record["rung"] == qr.RUNG_CROSS_MODEL_SAME_PROVIDER
    assert record["rung_index"] == 1
    assert record["cross_provider"] is False
    assert "cross_model_same_provider" in qr.independence_acceptance_line(record)


# --- Rung 3: same_model_fresh_context ----------------------------------------


def test_same_model_fresh_context_rung_is_recorded() -> None:
    record = _rung("claude_cli", "claude_cli")
    assert record["rung"] == qr.RUNG_SAME_MODEL_FRESH_CONTEXT
    assert record["rung_index"] == 2
    assert record["fresh_context_required"] is True
    assert "same_model_fresh_context" in qr.independence_acceptance_line(record)


# --- A single-provider installation still reaches acceptance -----------------


def test_single_provider_installation_reaches_acceptance() -> None:
    # A user who has only one provider and one model gets a real review: the
    # ladder degrades to same_model_fresh_context rather than refusing, and the
    # rung is recorded on the acceptance evidence.  There is no refusal path.
    record = qr.resolve_independence_rung(
        worker_provider="claude",
        reviewer_provider="claude",
        worker_model="claude_cli",
        reviewer_model="claude_cli",
    )
    assert record["rung"] == qr.RUNG_SAME_MODEL_FRESH_CONTEXT
    assert record["rung"] in qr.INDEPENDENCE_LADDER
    assert qr.independence_acceptance_line(record)


def test_acceptance_line_rejects_a_vendor_check_rung() -> None:
    with pytest.raises(quality_reviewer.ReviewerEvidenceError):
        qr.independence_acceptance_line({"rung": "vendor_check"})


# --- Invariants preserved on every rung, over the real prompt-assembly path ---


def test_blind_reviewer_receives_content_on_the_real_assembly_path() -> None:
    packet = _packet()
    # glm_vscode_lm runs in-process with no file-read tool: it is the exact
    # adapter whose live GLM reviewer returned process_limit before this fix.
    prompt = qr.assemble_reviewer_prompt(
        packet,
        lens="correctness",
        adapter_id="glm_vscode_lm",
        packet_path="/runtime/quality_review_packet.json",
    )
    inline = qr.extract_inline_packet(prompt)
    assert inline is not None
    permitted = [row["path"] for row in inline["candidate"]["changed_paths"]]
    assert CANDIDATE_PATH in permitted
    # A blind reviewer is never handed only an unreadable path.
    assert qr._CONVENIENCE_PREFIX not in prompt


@pytest.mark.parametrize(
    "adapter_id",
    ["glm_vscode_lm", "claude_cli"],
)
def test_anti_anchoring_holds_on_every_rung(adapter_id: str) -> None:
    packet = _packet()
    prompt = qr.assemble_reviewer_prompt(
        packet,
        lens="correctness",
        adapter_id=adapter_id,
        packet_path="/runtime/quality_review_packet.json",
    )
    # The reviewer prompt affirmatively withholds the worker's rationale,
    # self-verdict and final answer -- on the blind (content) rung and the
    # sighted (path) rung alike -- and binds the packet_sha256 contract.
    assert (
        "not given the worker's rationale, self-verdict, or final answer" in prompt
    )
    assert packet["packet_sha256"] in prompt
    # The delivered packet body itself carries no worker rationale/verdict/answer
    # field a reviewer could be anchored by.
    body = {k: v for k, v in packet.items() if k != "packet_sha256"}
    serialized = json.dumps(body).lower()
    assert "rationale" not in serialized
    assert "self_verdict" not in serialized
    assert "final_answer" not in serialized


def test_packet_sha256_is_unchanged_and_tamper_is_rejected() -> None:
    packet = _packet()
    # packet_sha256 is the same contract it is today: deriving the digest over
    # the packet body reproduces it exactly.
    body = {k: v for k, v in packet.items() if k != "packet_sha256"}
    recomputed = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    ).hexdigest()
    assert recomputed == packet["packet_sha256"]
    tampered = dict(packet)
    tampered["packet_sha256"] = "0" * 64
    with pytest.raises(quality_reviewer.ReviewerEvidenceError):
        qr.assemble_reviewer_prompt(
            tampered, lens="correctness", adapter_id="glm_vscode_lm"
        )
