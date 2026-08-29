from __future__ import annotations

import hashlib
import json

import pytest

from aiworkhub import quality_evidence as qe
from aiworkhub import quality_review as qr
from aiworkhub import quality_review_ingest
from aiworkhub import quality_reviewer, runtime_adapters

CANDIDATE_PATH = "src/aiworkhub/quality_review.py"
TASK_ID = "NF-2026-00259-PACKET-DELIVERY"

# The exact capability set given to a reviewer on the vscode_lm_in_process
# sandbox: a Source Graph query tool and a submit tool, and NO file-read tool.
BLIND_TOOLSET = frozenset(
    {
        "aiworkhub_worker_source_graph_query",
        "aiworkhub_worker_quality_review_submit",
    }
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
        objective="deliver the packet as content",
        acceptance=["a blind reviewer can cite an exact path and line"],
        required_outputs=[CANDIDATE_PATH],
        validation=["python3 -m pytest -q"],
        scoped_audits={lens: _scoped_audit(lens, [CANDIDATE_PATH])},
    )


# --- Acceptance 1: content delivered, packet_sha256 unchanged ----------------


def test_deliver_packet_content_preserves_sha256_and_needs_no_file_read() -> None:
    packet = _packet()
    delivery = qr.deliver_packet_content(
        packet, packet_path="/runtime/quality_review_packet.json"
    )
    assert delivery["requires_file_read"] is False
    # packet_sha256 is the same contract it is today.
    assert delivery["packet_sha256"] == packet["packet_sha256"]
    # The delivered content round-trips to the identical packet, digest and all.
    reconstructed = json.loads(delivery["packet_content"])
    assert reconstructed == packet
    assert reconstructed["packet_sha256"] == packet["packet_sha256"]
    # The path survives only as a convenience.
    assert delivery["packet_path"] == "/runtime/quality_review_packet.json"


def test_deliver_packet_content_rejects_tampered_digest() -> None:
    packet = _packet()
    packet["packet_sha256"] = "0" * 64
    with pytest.raises(quality_reviewer.ReviewerEvidenceError):
        qr.deliver_packet_content(packet)


def test_prompt_embeds_content_and_never_requires_a_file_read() -> None:
    packet = _packet()
    prompt = qr.build_reviewer_prompt_with_content(
        packet, lens="correctness", reviewer_tool_names=BLIND_TOOLSET
    )
    assert qr._INLINE_PACKET_PREFIX in prompt
    assert packet["packet_sha256"] in prompt
    # No forced path read for a blind reviewer.
    assert "Read the packet file first" not in prompt
    assert qr._CONVENIENCE_PREFIX not in prompt


def test_prompt_keeps_path_convenience_only_for_sighted_reviewers() -> None:
    packet = _packet()
    sighted = qr.build_reviewer_prompt_with_content(
        packet,
        lens="correctness",
        reviewer_tool_names={"Read", "aiworkhub_worker_quality_review_submit"},
        packet_path="/runtime/quality_review_packet.json",
    )
    assert qr._CONVENIENCE_PREFIX in sighted
    assert "authoritative and requires no file read" in sighted
    blind = qr.build_reviewer_prompt_with_content(
        packet,
        lens="correctness",
        reviewer_tool_names=BLIND_TOOLSET,
        packet_path="/runtime/quality_review_packet.json",
    )
    assert qr._CONVENIENCE_PREFIX not in blind


# --- Acceptance 2: a blind reviewer cites an exact packet-permitted path/line -


def test_blind_reviewer_can_cite_exact_path_and_line_from_content() -> None:
    packet = _packet()
    # This is the exact capability set of a vscode_lm_in_process reviewer.
    assert qr.capability_set_has_file_read(BLIND_TOOLSET) is False

    prompt = qr.build_reviewer_prompt_with_content(
        packet, lens="correctness", reviewer_tool_names=BLIND_TOOLSET
    )
    # The reviewer reaches the packet with NO file-read tool: it reads the
    # content straight out of its own prompt.
    inline = qr.extract_inline_packet(prompt)
    assert inline is not None
    permitted = [
        row["path"] for row in inline["candidate"]["changed_paths"]
    ]
    assert CANDIDATE_PATH in permitted

    # It then produces a defect finding citing that exact packet-permitted path
    # and line, and the canonical normalizer accepts it.
    finding = {
        "severity": "medium",
        "disposition": "defect",
        "summary": "capability set omits a file-read tool",
        "evidence": f"{CANDIDATE_PATH}:42 delivers the packet as content",
        "path": CANDIDATE_PATH,
        "line_start": 42,
        "line_end": 42,
    }
    normalized = quality_reviewer.normalize_packet_findings(
        packet, lens="correctness", findings=[finding]
    )
    assert normalized[0]["evidence_reference"] == {
        "kind": "source",
        "path": CANDIDATE_PATH,
        "line_start": 42,
        "line_end": 42,
    }
    assert normalized[0]["actionable"] is True


def test_rm44_canonical_second_ingress_reaches_durable_submit_boundary() -> None:
    packet = _packet()
    raw_finding = {
        "severity": "medium",
        "disposition": "defect",
        "summary": "capability set omits a file-read tool",
        "evidence": f"{CANDIDATE_PATH}:42 delivers the packet as content",
        "path": CANDIDATE_PATH,
        "line_start": 42,
        "line_end": 42,
    }
    submitted: list[dict[str, object]] = []

    def normalize(report: dict[str, object]) -> dict[str, object]:
        return {
            "lens": "correctness",
            "findings": quality_reviewer.normalize_packet_findings(
                packet,
                lens="correctness",
                findings=report.get("findings") or [],
            ),
        }

    def durable_submit(report: dict[str, object]) -> None:
        submitted.append(normalize(report))

    provider_report = {"lens": "correctness", "findings": [raw_finding]}
    event = json.dumps({"type": "result", "result": json.dumps(provider_report)})
    result = quality_review_ingest.ingest_structured_final(
        [event],
        expected_lens="correctness",
        normalize=normalize,
        submit=durable_submit,
    )

    assert result.status == "submitted"
    assert result.submitted is True
    assert submitted == [result.report]
    assert submitted[0]["findings"][0]["actionable"] is True
    assert submitted[0]["findings"][0]["evidence_reference"] == {
        "kind": "source",
        "path": CANDIDATE_PATH,
        "line_start": 42,
        "line_end": 42,
    }


def test_capability_set_and_adapter_file_read_classification() -> None:
    assert qr.capability_set_has_file_read({"Read"}) is True
    assert qr.capability_set_has_file_read({"aiworkhub_worker_file_read"}) is True
    assert qr.capability_set_has_file_read(BLIND_TOOLSET) is False

    assert runtime_adapters.adapter_provides_file_read("claude_cli") is True
    assert runtime_adapters.adapter_provides_file_read("glm_vscode_lm") is False
    # A Copilot CLI that fell back to the in-process bridge is blind too.
    assert (
        runtime_adapters.adapter_provides_file_read(
            "glm_copilot_cli", adapter_fallback_used=True
        )
        is False
    )
    assert runtime_adapters.adapter_provides_file_read("glm_copilot_cli") is True


# --- Acceptance 3: not forced to submit after a single zero-hit query ---------


def test_single_zero_hit_query_does_not_force_submission() -> None:
    # One zero-hit orientation query: the reviewer may keep inspecting.
    assert qr.reviewer_submit_forced(1, prior_hit_total=0) is False
    assert qr.reviewer_may_query_source_graph_again(1, prior_hit_total=0) is True
    # The inspection phase still terminates.
    assert (
        qr.reviewer_submit_forced(
            qr.REVIEWER_MAX_INSPECTION_QUERIES, prior_hit_total=0
        )
        is True
    )
    # A reviewer that has already found evidence past the minimum is not stuck.
    assert (
        qr.reviewer_may_query_source_graph_again(
            qr.REVIEWER_MIN_INSPECTION_QUERIES, prior_hit_total=3
        )
        is False
    )


# --- Acceptance 4: reviewer worktree indexing / parent-repo scoping ----------


def test_zero_row_worktree_with_baseline_scopes_to_parent_repository() -> None:
    baseline = [{"path": f"src/pkg/mod_{i}.py", "sha256": "x"} for i in range(6)]
    scope = qr.choose_reviewer_source_graph_scope(
        worktree_evidence={"entity_rows": 0, "edge_rows": 0, "file_rows": 0},
        workspace_baseline=baseline,
        parent_repo="/repo",
    )
    assert scope["scope"] == "parent_repository"
    assert scope["parent_repo"] == "/repo"
    assert scope["why"]


def test_indexed_worktree_uses_its_own_scope() -> None:
    scope = qr.choose_reviewer_source_graph_scope(
        worktree_evidence={"entity_rows": 5, "edge_rows": 4, "file_rows": 1},
        workspace_baseline=[{"path": "src/pkg/mod.py", "sha256": "x"}],
        parent_repo="/repo",
    )
    assert scope["scope"] == "reviewer_worktree"
    assert scope["why"]


# --- Acceptance 5: refuse when no independent sighted reviewer exists ---------


def _fleet() -> list[dict[str, object]]:
    return [
        {"provider": "glm", "adapter_id": "glm_vscode_lm", "availability": "available"},
        {
            "provider": "gpt",
            "adapter_id": "glm_copilot_cli",
            "availability": "available",
            "adapter_fallback_used": True,
        },
        {
            "provider": "deepseek",
            "adapter_id": "deepseek_copilot_cli",
            "availability": "provider_refused",
        },
    ]


def test_all_blind_or_refused_reviewers_refuse_with_per_provider_reasons() -> None:
    result = qr.assess_reviewer_availability(
        worker_provider="claude",
        candidates=_fleet(),
        packet_delivered_as_content=False,
    )
    assert result["can_launch"] is False
    assert result["refusal_reason"] == qr.REFUSAL_NO_INDEPENDENT_SIGHTED
    assert result["reasons_by_provider"]["glm"] == qr.REASON_BLIND_NO_FILE_READ
    assert result["reasons_by_provider"]["gpt"] == qr.REASON_BLIND_NO_FILE_READ
    assert result["reasons_by_provider"]["deepseek"] == qr.REASON_PROVIDER_REFUSED


def test_content_delivery_makes_blind_adapters_viable_reviewers() -> None:
    result = qr.assess_reviewer_availability(
        worker_provider="claude",
        candidates=_fleet(),
        packet_delivered_as_content=True,
    )
    assert result["can_launch"] is True
    assert "glm" in result["viable_reviewers"]
    assert "gpt" in result["viable_reviewers"]
    # A paid-out provider stays unusable even with content delivery.
    assert result["reasons_by_provider"]["deepseek"] == qr.REASON_PROVIDER_REFUSED


def test_single_provider_review_is_accepted_via_the_ladder() -> None:
    # RENAMED from test_same_provider_review_is_never_accepted.
    #
    # BEFORE: this asserted a reviewer sharing the worker's provider was never
    # usable -- can_launch False with reasons_by_provider["glm"] ==
    # REASON_SAME_PROVIDER.  That vendor check contradicted the independence
    # ladder (which never refuses on provider identity) and left a
    # single-provider installation -- a user with only Claude, or only Codex --
    # unable to complete any review at all.
    #
    # WHY THE VENDOR CHECK WAS REMOVED: what makes a review independent is the
    # anti-anchored packet, the sealed candidate, the separate read-only process
    # and the authenticated packet_sha256 submission -- all of which hold on
    # every rung.  Vendor identity was never one of them.  A same-provider
    # reviewer is recorded at the same_model_fresh_context rung and completes.
    #
    # WHICH TESTS NOW CARRY THE PROTECTION THIS ONE USED TO PROVIDE:
    #   * test_review_independence_ladder::test_anti_anchoring_holds_on_every_rung
    #     and ::test_packet_sha256_is_unchanged_and_tamper_is_rejected keep the
    #     reviewer from ever receiving the worker's rationale/verdict/answer and
    #     bind the packet_sha256 contract on every rung;
    #   * test_process_limit_only_report_still_fails_its_lens (below) keeps an
    #     uninspected (process_limit-only) review from being accepted;
    #   * test_review_independence_ladder::
    #     test_single_provider_installation_reaches_acceptance records the rung a
    #     single-provider review runs at.

    # Exactly one provider is offered, and it shares the worker's provider.
    # Content delivery makes it sighted, so it is a viable reviewer and the
    # launch proceeds instead of refusing.
    only_provider = qr.assess_reviewer_availability(
        worker_provider="glm",
        candidates=[
            {"provider": "glm", "adapter_id": "glm_vscode_lm", "availability": "available"},
        ],
        packet_delivered_as_content=True,
    )
    assert only_provider["can_launch"] is True
    assert only_provider["viable_reviewers"] == ["glm"]
    assert only_provider["refusal_reason"] is None
    assert only_provider["reasons_by_provider"]["glm"] == qr.REASON_AVAILABLE

    # A same-provider reviewer alongside an unusable one still launches on the
    # same-provider reviewer; the unusable provider keeps its true reason.
    result = qr.assess_reviewer_availability(
        worker_provider="glm",
        candidates=[
            {"provider": "glm", "adapter_id": "glm_vscode_lm", "availability": "available"},
            {
                "provider": "deepseek",
                "adapter_id": "deepseek_copilot_cli",
                "availability": "quota_unobserved",
            },
        ],
        packet_delivered_as_content=True,
    )
    assert result["can_launch"] is True
    assert "glm" in result["viable_reviewers"]
    assert result["reasons_by_provider"]["glm"] == qr.REASON_AVAILABLE
    assert result["reasons_by_provider"]["deepseek"] == qr.REASON_QUOTA_UNOBSERVED


# --- Forbidden guard: the 0.9.74 blind-reviewer gate is not weakened ---------


def test_process_limit_only_report_still_fails_its_lens() -> None:
    # Even after content delivery un-blinds adapters, a report that inspected
    # nothing (all findings process_limit) must still fail its lens.
    report = {
        "lens": qe.LENS_CORRECTNESS,
        "provider": "glm",
        "read_only": True,
        "can_mutate_repo": False,
        "findings": [
            {
                "id": "pl-1",
                "severity": qe.SEVERITY_LOW,
                "disposition": qe.FINDING_DISPOSITION_PROCESS_LIMIT,
                "summary": "reviewer could not inspect the packet",
                "evidence": "no file-read tool was available for the packet path",
            }
        ],
    }
    verdict = qe.fold_quality_verdict(
        [],
        risk_profile=qe.resolve_risk_profile(qe.RISK_MEDIUM),
        reviewer_reports=[report],
        combined_tree_checks=[
            qe.EvidenceCheck(
                check_id="union-tests", kind="test", status=qe.STATUS_PASSED, summary=""
            )
        ],
        worker_provider="claude",
    )
    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:correctness" in verdict["blocking_evidence"]
