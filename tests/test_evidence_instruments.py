from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

from aiworkhub import agent_tool_instructions as instructions
from aiworkhub import evidence_instruments as evidence


def test_review_evidence_audit_recomputes_hash_and_size(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    path = candidate / "result.py"
    path.write_bytes(b"answer = 42\n")
    path.chmod(0o644)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    report = evidence.review_evidence_audit(
        tmp_path,
        candidate,
        changed_paths=["result.py"],
        stored_hashes={"result.py": digest},
        required_outputs=[{"path": "result.py", "sha256": digest, "bytes": 12}],
    )
    assert report["status"] == "pass"
    assert report["required_outputs_verified"] == 1
    actual_mode = stat.S_IMODE(path.stat().st_mode)
    manifest_token = evidence.review_evidence_audit(
        tmp_path,
        candidate,
        changed_paths=["result.py"],
        stored_hashes={"result.py": digest},
        required_outputs=[{
            "path": "result.py",
            "sha256": f"file:{actual_mode:o}:{digest}",
            "bytes": 12,
        }],
    )
    assert manifest_token["status"] == "pass"
    # A sandboxed filesystem refuses the setuid bit outright (landlock returns
    # EPERM) and a nosuid mount drops it silently, so a card whose validation
    # ran this file failed on green code.  Assert the round-trip only when the
    # bit is actually on disk, and keep the coverage everywhere it can run.
    setuid_applied = False
    if os.name != "nt":
        try:
            path.chmod(0o4755)
        except OSError:
            pass
        setuid_applied = stat.S_IMODE(path.stat().st_mode) == 0o4755
    if setuid_applied:
        executable_token = evidence.review_evidence_audit(
            tmp_path,
            candidate,
            changed_paths=["result.py"],
            stored_hashes={"result.py": digest},
            required_outputs=[{
                "path": "result.py",
                "sha256": f"file:4755:{digest}",
                "bytes": 12,
            }],
        )
        assert executable_token["status"] == "pass"
    path.chmod(0o644)
    actual_mode = stat.S_IMODE(path.stat().st_mode)
    malformed_values = (
        f"file:{actual_mode:o}:{digest[:-1]}",
        f"file:{actual_mode:o}:{'0' * 64}",
        f"file:{actual_mode ^ 0o100:o}:{digest}",
        f"file:xyz:{digest}",
        f"file:888:{digest}",
        f"file:{actual_mode:o}:extra:{digest}",
        f"file:{actual_mode:o}:{digest.upper()}",
        f" file:{actual_mode:o}:{digest}",
    )
    for malformed in malformed_values:
        malformed_token = evidence.review_evidence_audit(
            tmp_path,
            candidate,
            changed_paths=["result.py"],
            stored_hashes={"result.py": digest},
            required_outputs=[{
                "path": "result.py",
                "sha256": malformed,
                "bytes": 12,
            }],
        )
        assert malformed_token["blockers"] == [
            "required_output_hash_mismatch:result.py"
        ]
    failed = evidence.review_evidence_audit(
        tmp_path,
        candidate,
        changed_paths=["result.py"],
        stored_hashes={"result.py": "0" * 64},
    )
    assert failed["blocking"] is True


def test_contract_consistency_uses_generated_projections(tmp_path: Path) -> None:
    for provider in instructions.PROVIDERS:
        path = tmp_path / provider
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(instructions.render_projection(provider), encoding="utf-8")
    report = evidence.contract_consistency_check(tmp_path)
    assert report["status"] == "pass"
    (tmp_path / "CLAUDE.md").write_text("# drift\n", encoding="utf-8")
    report = evidence.contract_consistency_check(tmp_path)
    assert "projection_drift:CLAUDE.md" in report["blockers"]


def test_retrieval_eval_measures_wrapper_rank(tmp_path: Path) -> None:
    config = tmp_path / ".aiworkhub/source-graph-retrieval-eval.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "cases": [{
            "id": "one", "query": "symbol", "mode": "focus", "k": 2,
            "expected_paths": ["src/right.py"],
        }]
    }), encoding="utf-8")

    def query_fn(**_kwargs):
        return {
            "ok": True,
            "content": json.dumps({
                "ranked_symbols": [
                    {"file_path": "src/wrong.py"},
                    {"file_path": "src/right.py"},
                ]
            }),
        }

    report = evidence.source_graph_retrieval_eval(tmp_path, query_fn=query_fn)
    assert report["precision_at_k"] == 0.5
    assert report["recall_at_k"] == 1.0
    assert report["mrr"] == 0.5
    assert report["success_at_k"] == 1.0
    assert report["mean_returned_bytes"] > 0
    assert report["mean_latency_ms"] >= 0
    assert report["p95_latency_ms"] >= 0
    assert report["accepted_outcome_coverage"] is None
    assert report["accepted_outcome_measurement_pending"] is True
    assert report["blocking"] is False
    assert report["cases"][0]["returned_bytes"] > 0
    assert report["cases"][0]["accepted_outcome_observed"] is False


def test_retrieval_eval_distinguishes_missing_from_invalid_registry(tmp_path: Path) -> None:
    missing = evidence.source_graph_retrieval_eval(
        tmp_path,
        query_fn=lambda **_kwargs: {},
    )
    assert missing["status"] == "not_configured"
    assert missing["configured"] is False
    assert missing["reason"].startswith("retrieval_registry_missing:")

    config = tmp_path / ".aiworkhub/source-graph-retrieval-eval.json"
    config.parent.mkdir()
    config.write_text("{not-json}\n", encoding="utf-8")
    invalid = evidence.source_graph_retrieval_eval(
        tmp_path,
        query_fn=lambda **_kwargs: {},
    )
    assert invalid["status"] == "configuration_invalid"
    assert invalid["configured"] is True
    assert invalid["repair_hint"]


def test_retrieval_eval_fails_declared_quality_minimum(tmp_path: Path) -> None:
    config = tmp_path / ".aiworkhub/source-graph-retrieval-eval.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "minimums": {"recall_at_k": 1.0},
        "cases": [{
            "id": "one", "query": "symbol", "mode": "focus", "k": 2,
            "expected_paths": ["src/right.py"],
        }],
    }), encoding="utf-8")

    report = evidence.source_graph_retrieval_eval(
        tmp_path,
        query_fn=lambda **_kwargs: {
            "ok": True,
            "content": json.dumps({"candidate_files": ["src/wrong.py"]}),
        },
    )

    assert report["status"] == "below_gate"
    assert report["blocking"] is True
    assert report["gate_failures"] == ["recall_at_k:0.0<1.0"]


def test_retrieval_eval_accepted_outcome_coverage_reflects_authenticated_receipt(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".aiworkhub/source-graph-retrieval-eval.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "cases": [
            {
                "id": "authenticated", "query": "symbol", "mode": "focus", "k": 2,
                "expected_paths": ["src/right.py"],
                "accepted_outcome_task_id": "T1", "accepted_outcome_request_id": "r1",
                "accepted_outcome_receipt": {"schema_id": "aiworkhub.accepted_outcome_receipt.v1"},
            },
            {
                "id": "refused", "query": "symbol", "mode": "focus", "k": 2,
                "expected_paths": ["src/right.py"],
                "accepted_outcome_task_id": "T2", "accepted_outcome_request_id": "r2",
                "accepted_outcome_receipt": {"schema_id": "tampered"},
            },
            {
                "id": "missing_authority", "query": "symbol", "mode": "focus", "k": 2,
                "expected_paths": ["src/right.py"],
                "accepted_outcome_task_id": "T3", "accepted_outcome_request_id": "r3",
                "accepted_outcome_receipt": {"schema_id": "aiworkhub.accepted_outcome_receipt.v1"},
            },
            {
                "id": "not_claimed", "query": "symbol", "mode": "focus", "k": 2,
                "expected_paths": ["src/right.py"],
            },
        ],
    }), encoding="utf-8")

    def query_fn(**_kwargs):
        return {
            "ok": True,
            "content": json.dumps({"ranked_symbols": [{"file_path": "src/right.py"}]}),
        }

    def authority_factory(task_id: str, request_id: str):
        if task_id == "T1":
            return lambda receipt: (dict(receipt), "")
        if task_id == "T2":
            return lambda receipt: (None, "repository_revision_mismatch")
        return None  # T3: no canonical evidence exists for this attempt

    report = evidence.source_graph_retrieval_eval(
        tmp_path, query_fn=query_fn, acceptance_authority_factory=authority_factory,
    )

    by_id = {row["id"]: row for row in report["cases"]}
    assert by_id["authenticated"]["accepted_outcome_status"] == "accepted"
    assert by_id["authenticated"]["accepted_outcome_observed"] is True
    assert by_id["refused"]["accepted_outcome_status"] == "refused"
    assert by_id["refused"]["accepted_outcome_observed"] is False
    assert by_id["refused"]["accepted_outcome_reason"] == "repository_revision_mismatch"
    assert by_id["missing_authority"]["accepted_outcome_status"] == "pending"
    assert by_id["missing_authority"]["accepted_outcome_observed"] is False
    assert by_id["not_claimed"]["accepted_outcome_status"] == "not_claimed"

    # Coverage is computed only from the two definitively evaluated cases
    # (one accepted, one refused) -- pending/not_claimed cases never dilute
    # or zero it out, and it is a real fraction, not a hardcoded floor.
    assert report["accepted_outcome_coverage"] == 0.5
    assert report["accepted_outcome_measurement_pending"] is False
    # accepted_outcome_coverage_measures_a_real_case: three cases declared a
    # claim (authenticated, refused, missing_authority); the fourth declared
    # nothing, so it never inflates the declared count.
    assert report["accepted_outcome_claims_declared"] == 3
    assert report["accepted_outcome_measurement_pending_reason"] is None


def test_retrieval_eval_accepted_outcome_stays_pending_without_authority_factory(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".aiworkhub/source-graph-retrieval-eval.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "cases": [{
            "id": "one", "query": "symbol", "mode": "focus", "k": 2,
            "expected_paths": ["src/right.py"],
            "accepted_outcome_task_id": "T1", "accepted_outcome_request_id": "r1",
            "accepted_outcome_receipt": {"schema_id": "aiworkhub.accepted_outcome_receipt.v1"},
        }],
    }), encoding="utf-8")

    report = evidence.source_graph_retrieval_eval(
        tmp_path,
        query_fn=lambda **_kwargs: {
            "ok": True,
            "content": json.dumps({"ranked_symbols": [{"file_path": "src/right.py"}]}),
        },
    )

    assert report["cases"][0]["accepted_outcome_status"] == "pending"
    assert report["accepted_outcome_coverage"] is None
    assert report["accepted_outcome_measurement_pending"] is True
    # The claim was declared (all three fields present); it just could not be
    # authenticated -- a different, louder condition than nobody claiming
    # anything at all.
    assert report["accepted_outcome_claims_declared"] == 1
    assert report["accepted_outcome_measurement_pending_reason"] == (
        "declared_claims_present_but_none_authenticated"
    )


def test_retrieval_eval_zero_declaring_cases_is_reported_loudly(tmp_path: Path) -> None:
    config = tmp_path / ".aiworkhub/source-graph-retrieval-eval.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "cases": [{
            "id": "one", "query": "symbol", "mode": "focus", "k": 2,
            "expected_paths": ["src/right.py"],
        }],
    }), encoding="utf-8")

    report = evidence.source_graph_retrieval_eval(
        tmp_path,
        query_fn=lambda **_kwargs: {
            "ok": True,
            "content": json.dumps({"ranked_symbols": [{"file_path": "src/right.py"}]}),
        },
    )

    # No case in the registry declares any accepted-outcome claim field at
    # all: this must not read the same as a healthy "nothing to report yet".
    assert report["accepted_outcome_claims_declared"] == 0
    assert report["accepted_outcome_measurement_pending"] is True
    assert report["accepted_outcome_measurement_pending_reason"] == (
        "no_registered_case_declares_an_accepted_outcome_claim"
    )


def test_accepted_outcome_authority_exception_is_pending_not_refused(tmp_path: Path) -> None:
    config = tmp_path / ".aiworkhub/source-graph-retrieval-eval.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "cases": [{
            "id": "throws", "query": "symbol", "mode": "focus", "k": 2,
            "expected_paths": ["src/right.py"],
            "accepted_outcome_task_id": "T1", "accepted_outcome_request_id": "r1",
            "accepted_outcome_receipt": {"schema_id": "aiworkhub.accepted_outcome_receipt.v1"},
        }],
    }), encoding="utf-8")

    def authority_factory(task_id: str, request_id: str):
        def _authority(receipt):
            raise RuntimeError("authority backend unavailable")
        return _authority

    report = evidence.source_graph_retrieval_eval(
        tmp_path,
        query_fn=lambda **_kwargs: {
            "ok": True,
            "content": json.dumps({"ranked_symbols": [{"file_path": "src/right.py"}]}),
        },
        acceptance_authority_factory=authority_factory,
    )

    row = report["cases"][0]
    # An authority that throws failed to answer; it never actively refused
    # the receipt, so this must not count as a measured zero.
    assert row["accepted_outcome_status"] == "pending"
    assert row["accepted_outcome_observed"] is False
    assert "acceptance_authority_error" in row["accepted_outcome_reason"]
    assert report["accepted_outcome_coverage"] is None
    assert report["accepted_outcome_measurement_pending"] is True


def test_absent_or_stale_evidence_is_pending_not_refused(tmp_path: Path) -> None:
    config = tmp_path / ".aiworkhub/source-graph-retrieval-eval.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "cases": [
            {
                "id": "evidence_never_sealed", "query": "symbol", "mode": "focus", "k": 2,
                "expected_paths": ["src/right.py"],
                "accepted_outcome_task_id": "T1", "accepted_outcome_request_id": "r1",
                "accepted_outcome_receipt": {"schema_id": "aiworkhub.accepted_outcome_receipt.v1"},
            },
            {
                "id": "promoted_file_edited_after_acceptance",
                "query": "symbol", "mode": "focus", "k": 2,
                "expected_paths": ["src/right.py"],
                "accepted_outcome_task_id": "T2", "accepted_outcome_request_id": "r2",
                "accepted_outcome_receipt": {"schema_id": "aiworkhub.accepted_outcome_receipt.v1"},
            },
            {
                "id": "identity_tampered", "query": "symbol", "mode": "focus", "k": 2,
                "expected_paths": ["src/right.py"],
                "accepted_outcome_task_id": "T3", "accepted_outcome_request_id": "r3",
                "accepted_outcome_receipt": {"schema_id": "aiworkhub.accepted_outcome_receipt.v1"},
            },
        ],
    }), encoding="utf-8")

    def authority_factory(task_id: str, request_id: str):
        # The exact canonical reason strings task_engine's own receipt
        # authority returns for each situation -- reused verbatim here so
        # this test exercises the real vocabulary, not a stand-in for it.
        if task_id == "T1":
            return lambda receipt: (None, "accepted_outcome_receipt_sealed_evidence_missing")
        if task_id == "T2":
            return lambda receipt: (None, "accepted_outcome_receipt_canonical_hash_mismatch")
        return lambda receipt: (None, "accepted_outcome_receipt_identity_mismatch")

    report = evidence.source_graph_retrieval_eval(
        tmp_path,
        query_fn=lambda **_kwargs: {
            "ok": True,
            "content": json.dumps({"ranked_symbols": [{"file_path": "src/right.py"}]}),
        },
        acceptance_authority_factory=authority_factory,
    )

    by_id = {row["id"]: row for row in report["cases"]}
    # Absent evidence (never sealed) and stale evidence (a promoted path
    # edited since acceptance) are both "could not find or trust the
    # evidence any more" -- pending, never a false-negative refusal.
    assert by_id["evidence_never_sealed"]["accepted_outcome_status"] == "pending"
    assert by_id["promoted_file_edited_after_acceptance"]["accepted_outcome_status"] == "pending"
    # An actual identity/tamper rejection of the receipt itself is still
    # refused.
    assert by_id["identity_tampered"]["accepted_outcome_status"] == "refused"

    # The one genuine rejection drives coverage; the two pending cases are
    # excluded rather than silently counted as failures.
    assert report["accepted_outcome_coverage"] == 0.0
    assert report["accepted_outcome_measurement_pending"] is False


def _sealed_current_card_and_receipt(
    *,
    task_id: str,
    request_id: str,
    card_claim_epoch: int,
    receipt_claim_epoch: int,
    receipt_base_oid: str = "b" * 40,
    card_base_oid: str = "b" * 40,
) -> tuple[dict[str, object], dict[str, object]]:
    hashes = {"src/right.py": "a" * 64}
    manifest = {"schema_id": "aiworkhub.attempt_artifact_manifest.v1", "entries": []}
    manifest_id = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    current_card = {
        "claim_epoch": card_claim_epoch,
        "terminal_review": {
            "evidence": {
                "changed_paths": ["src/right.py"],
                "changed_path_hashes": hashes,
                "attempt_artifact_manifest": manifest,
                "workspace": {"base_oid": card_base_oid},
            },
        },
    }
    receipt = {
        "schema_id": "aiworkhub.accepted_outcome_receipt.v1",
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": receipt_claim_epoch,
        "base_oid": receipt_base_oid,
        "promoted_paths": ["src/right.py"],
        "changed_path_hashes": hashes,
        "attempt_artifact_manifest_id": manifest_id,
    }
    return current_card, receipt


def test_advanced_claim_epoch_does_not_publish_a_measured_zero(tmp_path: Path) -> None:
    current_card, receipt = _sealed_current_card_and_receipt(
        task_id="T1",
        request_id="r1",
        card_claim_epoch=4,
        receipt_claim_epoch=3,
    )
    config = tmp_path / ".aiworkhub/source-graph-retrieval-eval.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "cases": [{
            "id": "reclaimed", "query": "symbol", "mode": "focus", "k": 2,
            "expected_paths": ["src/right.py"],
            "accepted_outcome_task_id": "T1", "accepted_outcome_request_id": "r1",
            "accepted_outcome_receipt": receipt,
        }],
    }), encoding="utf-8")

    def authority_factory(task_id: str, request_id: str):
        # The canonical authority folds an advanced claim_epoch -- the card
        # was re-claimed after this receipt was sealed -- into the same
        # identity_mismatch code it uses for a tampered receipt. Authenticated
        # current-card evidence is what distinguishes that staleness from a
        # same-task/same-request forged field.
        def _authority(raw_receipt):
            return None, "accepted_outcome_receipt_identity_mismatch"

        _authority.current_card = current_card
        return _authority

    report = evidence.source_graph_retrieval_eval(
        tmp_path,
        query_fn=lambda **_kwargs: {
            "ok": True,
            "content": json.dumps({"ranked_symbols": [{"file_path": "src/right.py"}]}),
        },
        acceptance_authority_factory=authority_factory,
    )

    row = report["cases"][0]
    # Authenticated current-card evidence shows only claim_epoch advanced,
    # so a re-claim is stale evidence, not a forged claim.
    assert row["accepted_outcome_status"] == "pending"
    assert row["accepted_outcome_observed"] is False
    assert row["accepted_outcome_reason"] == "accepted_outcome_receipt_identity_mismatch"
    # A re-claim must never silently drop coverage to a measured 0.0.
    assert report["accepted_outcome_coverage"] is None
    assert report["accepted_outcome_measurement_pending"] is True


def test_cross_task_identity_mismatch_still_refuses(tmp_path: Path) -> None:
    config = tmp_path / ".aiworkhub/source-graph-retrieval-eval.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "cases": [{
            "id": "foreign", "query": "symbol", "mode": "focus", "k": 2,
            "expected_paths": ["src/right.py"],
            "accepted_outcome_task_id": "T1", "accepted_outcome_request_id": "r1",
            "accepted_outcome_receipt": {
                "schema_id": "aiworkhub.accepted_outcome_receipt.v1",
                "task_id": "T9-foreign-task", "request_id": "r1",
            },
        }],
    }), encoding="utf-8")

    def authority_factory(task_id: str, request_id: str):
        return lambda receipt: (None, "accepted_outcome_receipt_identity_mismatch")

    report = evidence.source_graph_retrieval_eval(
        tmp_path,
        query_fn=lambda **_kwargs: {
            "ok": True,
            "content": json.dumps({"ranked_symbols": [{"file_path": "src/right.py"}]}),
        },
        acceptance_authority_factory=authority_factory,
    )

    row = report["cases"][0]
    # The receipt names a different task than the one it is declared
    # against -- a genuine tampered/foreign-identity rejection.
    assert row["accepted_outcome_status"] == "refused"
    assert row["accepted_outcome_reason"] == "accepted_outcome_receipt_identity_mismatch"
    assert report["accepted_outcome_coverage"] == 0.0
    assert report["accepted_outcome_measurement_pending"] is False


def test_same_task_same_request_tampered_expected_field_stays_refused(
    tmp_path: Path,
) -> None:
    current_card, receipt = _sealed_current_card_and_receipt(
        task_id="T1",
        request_id="r1",
        card_claim_epoch=3,
        receipt_claim_epoch=3,
        receipt_base_oid="c" * 40,
        card_base_oid="b" * 40,
    )
    config = tmp_path / ".aiworkhub/source-graph-retrieval-eval.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "cases": [{
            "id": "tampered_base_oid", "query": "symbol", "mode": "focus", "k": 2,
            "expected_paths": ["src/right.py"],
            "accepted_outcome_task_id": "T1", "accepted_outcome_request_id": "r1",
            "accepted_outcome_receipt": receipt,
        }],
    }), encoding="utf-8")

    def query_fn(**_kwargs):
        return {
            "ok": True,
            "content": json.dumps({"ranked_symbols": [{"file_path": "src/right.py"}]}),
        }

    def authority_factory_with_card(task_id: str, request_id: str):
        def _authority(raw_receipt):
            return None, "accepted_outcome_receipt_identity_mismatch"

        _authority.current_card = current_card
        return _authority

    report = evidence.source_graph_retrieval_eval(
        tmp_path,
        query_fn=query_fn,
        acceptance_authority_factory=authority_factory_with_card,
    )
    row = report["cases"][0]
    # Same task/request, but base_oid was forged against the current card.
    assert row["accepted_outcome_status"] == "refused"
    assert row["accepted_outcome_reason"] == "accepted_outcome_receipt_identity_mismatch"
    assert report["accepted_outcome_coverage"] == 0.0
    assert report["accepted_outcome_measurement_pending"] is False

    def authority_factory_without_card(task_id: str, request_id: str):
        return lambda raw_receipt: (None, "accepted_outcome_receipt_identity_mismatch")

    closed = evidence.source_graph_retrieval_eval(
        tmp_path,
        query_fn=query_fn,
        acceptance_authority_factory=authority_factory_without_card,
    )
    # Missing current-card evidence fails closed even when task_id/request_id
    # still match -- it cannot be proved claim_epoch-only.
    assert closed["cases"][0]["accepted_outcome_status"] == "refused"
    assert closed["accepted_outcome_coverage"] == 0.0


def test_accepted_outcome_incomplete_claim_is_named_not_conflated_with_no_claim(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".aiworkhub/source-graph-retrieval-eval.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "cases": [{
            "id": "partial", "query": "symbol", "mode": "focus", "k": 2,
            "expected_paths": ["src/right.py"],
            "accepted_outcome_task_id": "T1",
            "accepted_outcome_request_id": "r1",
        }],
    }), encoding="utf-8")

    report = evidence.source_graph_retrieval_eval(
        tmp_path,
        query_fn=lambda **_kwargs: {
            "ok": True,
            "content": json.dumps({"ranked_symbols": [{"file_path": "src/right.py"}]}),
        },
        acceptance_authority_factory=lambda task_id, request_id: (
            lambda receipt: (dict(receipt), "")
        ),
    )

    row = report["cases"][0]
    # task_id/request_id were declared but the receipt was not -- a malformed
    # claim, distinct from a case that never claimed anything.
    assert row["accepted_outcome_status"] == "not_claimed"
    assert row["accepted_outcome_reason"] == "accepted_outcome_claim_incomplete"


def test_ab_report_excludes_unobserved_and_measures_complete_pair(tmp_path: Path) -> None:
    path = tmp_path / ".aiworkhub/prompt-ab-canary.jsonl"
    path.parent.mkdir()
    rows = [
        {"pair_id": "p", "variant": "A", "task_family": "x", "model": "m", "adapter": "a", "usage_observed": True, "input_tokens": 100, "output_tokens": 40, "total_tokens": 140, "elapsed_ms": 1000, "accepted": True},
        {"pair_id": "p", "variant": "B", "task_family": "x", "model": "m", "adapter": "a", "usage_observed": True, "input_tokens": 90, "output_tokens": 20, "total_tokens": 110, "elapsed_ms": 800, "accepted": True},
        {"pair_id": "q", "variant": "A", "task_family": "x", "model": "m", "adapter": "a", "usage_observed": False},
        {"pair_id": "q", "variant": "B", "task_family": "x", "model": "m", "adapter": "a", "usage_observed": True},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    report = evidence.prompt_bundle_ab_report(tmp_path)
    assert report["pair_count"] == 1
    assert report["pairs"][0]["metrics"]["output_tokens"]["delta_percent"] == -50.0
    assert report["excluded"] == [{"pair_id": "q", "reason": "usage_unobserved"}]


def test_risk_mode_precision_uses_only_explicit_adjudicated_predictions(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".aiworkhub/risk-mode-adjudication.jsonl"
    path.parent.mkdir()
    rows = [
        {
            "mode": "crashes",
            "language": "cpp",
            "predicted": True,
            "adjudicated": True,
            "correct": True,
        },
        {
            "mode": "crashes",
            "language": "cpp",
            "predicted": True,
            "adjudicated": True,
            "correct": False,
        },
        {
            "mode": "crashes",
            "language": "cpp",
            "predicted": False,
            "adjudicated": True,
            "correct": True,
        },
        {
            "mode": "crashes",
            "language": "python",
            "predicted": True,
            "adjudicated": False,
            "correct": True,
        },
    ]
    path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )

    report = evidence.risk_mode_precision_bench(tmp_path)

    assert report["status"] == "measured"
    assert report["measured"] is True
    assert report["buckets"] == [
        {
            "mode": "crashes",
            "language": "cpp",
            "tp": 1,
            "fp": 1,
            "adjudicated": 2,
            "precision": 0.5,
        }
    ]


def test_quality_ratchet_and_coverage_projection(tmp_path: Path) -> None:
    source = tmp_path / "large.py"
    source.write_text("a\nb\n", encoding="utf-8")
    config = tmp_path / ".aiworkhub/quality-ratchet.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"violation_ids": ["old"], "max_lines": {"large.py": 1}}), encoding="utf-8")
    ratchet = evidence.quality_gate_ratchet(tmp_path, violations=[{"id": "old"}, {"id": "new"}])
    assert ratchet["blocking"] is True
    assert ratchet["new_violation_ids"] == ["new"]
    coverage = tmp_path / "coverage.json"
    coverage.write_text(json.dumps({"files": {"large.py": {"summary": {"covered_lines": 1, "missing_lines": 1, "percent_covered": 50}}}}), encoding="utf-8")
    applied = evidence.coverage_import_apply(tmp_path, artifact_path="coverage.json")
    assert applied["file_count"] == 1
    projected = evidence.runtime_coverage_for_paths(tmp_path, ["large.py"])
    assert projected["status"] == "available"
    assert projected["files"][0]["percent_covered"] == 50.0


def test_suite_profile_detects_stable_exact_argv(tmp_path: Path) -> None:
    report = evidence.suite_profile(
        tmp_path,
        argv=[sys.executable, "-c", "print('ok')"],
        repeats=2,
    )
    assert report["status"] == "measured"
    assert report["flake_observed"] is False
    assert all(row["returncode"] == 0 for row in report["runs"])


def test_suite_profile_collects_per_test_metrics(tmp_path: Path) -> None:
    (tmp_path / "test_sample.py").write_text(
        "def test_sample():\n    assert 2 + 2 == 4\n", encoding="utf-8"
    )
    report = evidence.suite_profile(
        tmp_path,
        argv=[sys.executable, "-m", "pytest", "-q", "test_sample.py"],
        repeats=2,
    )
    assert report["per_test_observed"] is True
    # pytest reports nodeids relative to ITS rootdir, not to the cwd it was
    # given.  Under a worker sandbox tmp_path lives inside the repository
    # (.aiworkhub/temp/validation/...), so pytest finds the repo pyproject.toml
    # as rootdir and prefixes the path -- and every card whose validation list
    # included this file failed deterministically.  Assert the identity this
    # test is actually about (the per-test row is keyed by the sample's nodeid)
    # without depending on where tmp_path happens to be.
    assert report["tests"][0]["nodeid"].endswith("test_sample.py::test_sample")
    assert report["tests"][0]["run_count"] == 2
    assert report["tests"][0]["flake_observed"] is False


def test_quality_gate_ratchet_distinguishes_absent_unreadable_and_shape_invalid(
    tmp_path: Path,
) -> None:
    # Absent baseline: not_configured, never blocking.
    absent = evidence.quality_gate_ratchet(tmp_path, violations=[])
    assert absent["status"] == "not_configured"
    assert absent["blocking"] is False
    assert absent["reason"] == "baseline_absent"

    config = tmp_path / ".aiworkhub" / "quality-ratchet.json"
    config.parent.mkdir()

    # Present but unreadable (corrupt/oversized): blocking error, distinguishable
    # reason -- a corrupt baseline can no longer silently erase the ratchet.
    config.write_text("{ not json", encoding="utf-8")
    unreadable = evidence.quality_gate_ratchet(tmp_path, violations=[])
    assert unreadable["status"] == "error"
    assert unreadable["blocking"] is True
    assert unreadable["reason"].startswith("baseline_unreadable:")

    # Parses but is not a Mapping: blocking, shape-invalid reason.
    config.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    shape = evidence.quality_gate_ratchet(tmp_path, violations=[])
    assert shape["status"] == "error"
    assert shape["blocking"] is True
    assert shape["reason"] == "baseline_shape_invalid"

    # An invalid/escaping path is a misconfiguration, not a corrupt baseline.
    bad_path = evidence.quality_gate_ratchet(
        tmp_path, violations=[], baseline_path="../escape.json"
    )
    assert bad_path["status"] == "not_configured"
    assert bad_path["blocking"] is False
    assert bad_path["reason"].startswith("baseline_path_invalid:")


def test_runtime_coverage_shape_invalid_and_zero_matches(tmp_path: Path) -> None:
    target = tmp_path / ".aiworkhub" / "source_graph" / "runtime_coverage.json"
    target.parent.mkdir(parents=True)

    # A non-Mapping document is shape-invalid, never an exception.
    target.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    doc_shape = evidence.runtime_coverage_for_paths(tmp_path, ["a.py"])
    assert doc_shape["reason"] == "runtime_coverage_projection_shape_invalid"
    assert doc_shape["status"] != "available"

    # "files" that is not a list is likewise shape-invalid.
    target.write_text(json.dumps({"files": {"a.py": {}}}), encoding="utf-8")
    files_shape = evidence.runtime_coverage_for_paths(tmp_path, ["a.py"])
    assert files_shape["reason"] == "runtime_coverage_projection_shape_invalid"
    assert files_shape["status"] != "available"

    # Zero matched rows is not_available with matched_files 0 -- never
    # "available", so an unmeasured path cannot masquerade as measured.
    target.write_text(
        json.dumps({"files": [{"file_path": "other.py", "covered_lines": 1, "missing_lines": 0}]}),
        encoding="utf-8",
    )
    zero = evidence.runtime_coverage_for_paths(tmp_path, ["a.py"])
    assert zero["status"] == "not_available"
    assert zero["reason"] == "no_runtime_coverage_for_requested_paths"
    assert zero["matched_files"] == 0

    # Non-Mapping rows are filtered; a real match still measures.
    target.write_text(
        json.dumps(
            {"files": ["garbage", {"file_path": "a.py", "covered_lines": 3, "missing_lines": 1}]}
        ),
        encoding="utf-8",
    )
    matched = evidence.runtime_coverage_for_paths(tmp_path, ["a.py"])
    assert matched["status"] == "available"
    assert matched["matched_files"] == 1
    assert matched["line_coverage"] == 75.0


# --- NF-2026-00322: absent is not invalid --------------------------------


def test_absent_artifact_is_missing_not_invalid(tmp_path: Path) -> None:
    # risk_mode_precision_bench and prompt_bundle_ab_report both read through
    # _regular; an absent artifact must report a distinct *_missing code, not
    # the artifact_invalid a corrupt one reports.
    missing = evidence.risk_mode_precision_bench(tmp_path)
    assert missing["measured"] is False
    assert missing["reason"].startswith("artifact_missing:")

    ab_missing = evidence.prompt_bundle_ab_report(tmp_path)
    assert ab_missing["reason"].startswith("artifact_missing:")

    # A corrupt artifact -- present but not a regular file (a directory here) --
    # reports artifact_invalid. Missing and corrupt therefore differ, in both
    # the full string and its reason code.
    corrupt = tmp_path / ".aiworkhub" / "risk-mode-adjudication.jsonl"
    corrupt.parent.mkdir(parents=True)
    corrupt.mkdir()
    corrupt_report = evidence.risk_mode_precision_bench(tmp_path)
    assert corrupt_report["reason"].startswith("artifact_invalid:")

    assert missing["reason"] != corrupt_report["reason"]
    assert missing["reason"].split(":", 1)[0] != corrupt_report["reason"].split(":", 1)[0]


def test_missing_adopts_the_sibling_retrieval_eval_vocabulary(tmp_path: Path) -> None:
    # The absent-registry sibling already names absence with a *_missing code;
    # the generic artifact path adopts the same vocabulary instead of inventing
    # a second one.
    def _never_called(**_kwargs: object) -> dict[str, object]:
        raise AssertionError("query_fn must not run when the registry is absent")

    sibling = evidence.source_graph_retrieval_eval(tmp_path, query_fn=_never_called)
    assert sibling["reason"].startswith("retrieval_registry_missing:")

    missing = evidence.risk_mode_precision_bench(tmp_path)
    assert missing["reason"].split(":", 1)[0].endswith("_missing")
    assert sibling["reason"].split(":", 1)[0].endswith("_missing")
