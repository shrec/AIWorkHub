"""NF-2026-00945: an SDLC stage is ready only on server-resolved canonical evidence.

Every positive case drives the real canonical producers -- a claimed card,
the attempt bundle from ``attempt_artifacts.persist_json_bundle``, the
launcher's own semantic-edit, coverage and reasoning-context functions feeding
a terminal event appended through ``process_event_ledger``,
``task_store.mark_terminal_review``, the accepted-outcome receipt from
``process_launcher_acceptance`` and ``task_engine.accept_review``, and
``learning_commit_store.record_disposition`` -- and nothing patches the gate
to success. The one test that wraps ``decide`` passes its real decision
through and only simulates a concurrent writer after it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import (
    attempt_artifacts,
    learning_commit_store,
    process_event_ledger,
    process_launcher,
    process_launcher_acceptance,
    runtime_adapters,
    sdlc_stage_evidence,
    task_engine,
    task_store,
    vscode_lm_worker,
)
from aiworkhub.repository_state import bootstrap_repository
from aiworkhub.sdlc_case_store import (
    CASES_DB_REL,
    STAGES,
    SdlcCaseConflict,
    SdlcCaseValidationError,
    SdlcStageEvidenceRefusal,
    append_stage,
    create_case,
    read_case,
    stage_packet,
)

RUNNER = "codex_worker"
TOPIC = "sdlc-evidence"
PROMOTED = "src/feature.py"
TASK_ID = "T-GATE"
CASE_ID = "C-GATE"
NOW = "2026-09-21T00:00:00+00:00"
ADAPTER = "codex_exec"
MODEL = "gpt-sdlc-test"
VSCODE_MODEL = "glm-5.2"
BRIDGE_REPO = "bridge-repo"
ABSENT = object()
PLAN = {
    "intent": "Gate SDLC stage transitions on canonical evidence.",
    "problem": "Six consecutive ready receipts were accepted with an empty payload.",
    "owner": "manager",
    "expected_outcome": "ready means the server proved it from native receipts.",
    "risk": "Legacy ready rows could read as verified completion.",
    "evidence_refs": ["file:docs/spec.md", "task:T-GATE"],
}
DESIGN = {
    "acceptance_criteria": ["An empty ready payload fails closed at every stage."],
    "constraints": ["Evidence reads stay bounded and read-only."],
    "affected_contracts": ["sdlc_case_store.append_stage"],
    "alternatives": ["Blanket-deny every ready transition (rejected)."],
}
SELF_ATTESTED = {"passed": True, "verified": True, "sha256": "a" * 64}


def _bootstrap(root: Path, name: str) -> SimpleNamespace:
    root.mkdir(parents=True, exist_ok=True)
    bootstrap_repository(root, repo_name=name)
    readiness = task_store.storage_readiness(root)
    if not readiness.ready:
        task_store.initialize_repository(root)
        readiness = task_store.storage_readiness(root)
    assert readiness.ready
    return SimpleNamespace(root=root, repo_id=readiness.repo_id)


def _contract(task_id: str) -> dict:
    return {
        "task_id": task_id,
        "runner": RUNNER,
        "topic": TOPIC,
        "objective": "Gate SDLC stage transitions on canonical evidence.",
        "acceptance": ["An empty ready payload fails closed at every stage."],
        "validation": ["python3 -m pytest -q tests/test_sdlc_stage_evidence.py"],
        "allowed_writes": [PROMOTED],
        "claim_epoch": 1,
    }


def _insert_claimed(root: Path, card: dict) -> None:
    """Seed one claimed canonical card; task_store exposes no creation writer."""
    conn = sqlite3.connect(str(task_store.canonical_db_path(root)))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, status, worker_status, objective, "
            "card_json, created_at, updated_at, claimed_by, claimed_at, started_at) "
            "VALUES (?, ?, ?, 'processing', 'claimed', ?, ?, ?, ?, ?, ?, ?)",
            (
                card["task_id"], RUNNER, TOPIC, card["objective"], json.dumps(card),
                NOW, NOW, RUNNER, NOW, NOW,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _gate(*, gated: bool = True, verified: bool = True) -> dict:
    """A worker-MCP gate as the finalizer seals it, with one joined apply receipt."""
    receipts = [{
        "path_sha256": process_launcher.semantic_edit_path_identifier(PROMOTED),
        "range_count": 1,
        "file_bytes": 6,
        "old_region_bytes": 3,
        "replacement_bytes": 4,
        "model_reemitted_old_bytes": 0,
    }]
    return {
        "gated": gated,
        "task_type": "code" if gated else "research",
        "required_tools": ["source_graph"] if gated else [],
        "satisfied": True,
        "observation_only": not gated,
        "verification": {
            "ok": verified,
            "semantic_edit_apply_receipts": receipts if verified else [],
        },
    }


def _host_receipt(request_id: str, **overrides) -> dict:
    """What the VS Code LM host reports for one acknowledged, effort-applied send."""
    return {
        "schema_id": vscode_lm_worker.REASONING_CONTEXT_ATTEMPT_SCHEMA_ID,
        "request_id": request_id,
        "repo_id": BRIDGE_REPO,
        "requested_model": VSCODE_MODEL,
        "host_model": {
            "id": "glm-5.2", "family": "glm", "name": "GLM", "vendor": "zai", "version": "5.2",
        },
        "requested_profile": "canonical_high",
        "send_state": "sent",
        "send_turn_count": 2,
        "provider_request_acknowledged": True,
        "option_status": "applied",
        "option_key": "reasoningEffort",
        "option_value": "high",
        "context_capacity_tokens": 131072,
        "context_capacity_source": "model.maxInputTokens",
        "provider_internal_state": "unknown",
        "unknown_reason": None,
        **overrides,
    }


def _append_terminal_event(root: Path, attempt: SimpleNamespace, **overrides) -> dict:
    """The finalizer's review_ready event, built by the launcher's own producers."""
    stdout = root / f"stdout-{attempt.request_id}.jsonl"
    if attempt.host_receipt is not ABSENT:
        result = {"type": "result", "subtype": "success", "is_error": False}
        if attempt.host_receipt is not None:
            result["reasoning_context_attempt"] = attempt.host_receipt
        stdout.write_text(json.dumps(result) + "\n", encoding="utf-8")
    metadata = {
        "adapter_id": attempt.adapter_id,
        "model": attempt.model,
        "task_id": attempt.task_id,
        "claim_epoch": 1,
        "vscode_lm_bridge": {"repo_id": BRIDGE_REPO},
    }
    reasoning = process_launcher._reasoning_context_attempt_from_output(
        stdout, metadata, attempt.request_id
    )
    semantic_edit = process_launcher._semantic_edit_evidence_from_output(
        stdout, worker_mcp_gate=attempt.gate
    )
    event = {
        "request_id": attempt.request_id,
        "task_id": attempt.task_id,
        "runner": RUNNER,
        "topic": TOPIC,
        "adapter_id": attempt.adapter_id,
        "model": attempt.model,
        "state": "review_ready",
        "changed_paths": [PROMOTED],
        "attempt_artifact_manifest": attempt.manifest,
        "worker_mcp_gate": attempt.gate,
        "semantic_edit": semantic_edit,
        "semantic_edit_coverage": process_launcher._semantic_edit_coverage(
            [PROMOTED],
            worker_mcp_gate=attempt.gate,
            runtime_evidence=semantic_edit,
        ),
        **({"reasoning_context_attempt": reasoning} if reasoning else {}),
        **overrides,
    }
    return process_event_ledger.append_event(
        root / process_launcher.PROCESS_LOG_DEFAULT_REL,
        {"schema_id": "aiworkhub.task_mcp.process_event.v1", "timestamp": NOW, **event},
    )


def _seal(
    root: Path,
    task_id: str,
    *,
    returncode: int = 0,
    adapter_id: str = ADAPTER,
    model: str = MODEL,
    gate: dict | None = None,
    host_receipt=ABSENT,
    terminal_event: bool = True,
) -> SimpleNamespace:
    """The finalizer seals one review-ready candidate the way the launcher does."""
    request_id = f"req-{task_id}"
    content = b"gated\n"
    target = root / PROMOTED
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    base_oid = f"base-{task_id}"
    gate = _gate() if gate is None else gate
    validations = [{"command": "python3 -m pytest -q", "returncode": returncode}]
    required_outputs = [{"path": PROMOTED, "sha256": digest, "bytes": len(content)}]
    bundle_dir = root / process_launcher.PROCESS_DIR_DEFAULT_REL / "attempt-artifacts" / request_id
    manifest = attempt_artifacts.persist_json_bundle(
        bundle_dir,
        attempt_id=request_id,
        payloads={
            "metadata": {
                "schema_id": "aiworkhub.attempt_metadata.v1",
                "request_identity": {
                    "request_id": request_id, "task_id": task_id, "runner": RUNNER, "topic": TOPIC,
                },
                "adapter_id": adapter_id,
                "model": model,
                "execution_mode": "provider_worker",
                "sandbox_backend": "landlock",
                "provider_stream_mode": "terminal_events",
                "workspace": {"base_oid": base_oid},
            },
            "diff": {
                "schema_id": "aiworkhub.attempt_diff_index.v1",
                "changed_paths": [PROMOTED],
                "changed_path_hashes": {PROMOTED: digest},
                "required_outputs": required_outputs,
            },
            "validation": {
                "schema_id": "aiworkhub.attempt_validation.v1",
                "checks": validations,
                "quality_gate": None,
                "worker_mcp_gate": gate,
            },
            "usage": {"schema_id": "aiworkhub.attempt_usage.v1"},
            "review": {
                "schema_id": "aiworkhub.attempt_review.v1",
                "target_state": "review_ready",
                "error": "",
                "kind": "worker_candidate",
            },
        },
    )
    ok, state = task_store.mark_terminal_review(
        root,
        task_id,
        runner=RUNNER,
        substatus="review_ready",
        evidence={
            "request_id": request_id,
            "request_identity": {
                "request_id": request_id, "task_id": task_id, "runner": RUNNER, "claim_epoch": 1,
            },
            "validation": validations,
            "required_outputs": required_outputs,
            "changed_paths": [PROMOTED],
            "changed_path_hashes": {PROMOTED: digest},
            "worker_mcp_gate": gate,
            "attempt_artifact_manifest": manifest,
            "workspace": {"base_oid": base_oid},
        },
    )
    assert (ok, state) == (True, "review")
    attempt = SimpleNamespace(
        task_id=task_id, request_id=request_id, digest=digest, manifest=manifest,
        base_oid=base_oid, adapter_id=adapter_id, model=model, gate=gate,
        host_receipt=host_receipt, bundle_dir=bundle_dir,
    )
    if terminal_event:
        _append_terminal_event(root, attempt)
    return attempt


def _accept(root: Path, task_id: str, sealed: SimpleNamespace) -> dict:
    """The coordinator accepts exactly that candidate through the canonical path."""
    receipt = process_launcher_acceptance.accepted_outcome_receipt(
        root,
        task_id=task_id,
        request_id=sealed.request_id,
        claim_epoch=1,
        base_oid=sealed.base_oid,
        promoted_paths=[PROMOTED],
        changed_path_hashes={PROMOTED: sealed.digest},
        attempt_artifact_manifest=sealed.manifest,
    )
    accepted = task_engine.accept_review(
        root,
        task_id,
        runner=RUNNER,
        topic=TOPIC,
        request_id=sealed.request_id,
        evidence={"promoted_paths": [PROMOTED]},
        accepted_outcome_receipt=receipt,
    )
    assert accepted["ok"] is True, accepted
    return receipt


def _mutate_card(root: Path, task_id: str, mutate) -> None:
    """Rewrite a canonical card in place, as a concurrent or hostile writer would."""
    conn = sqlite3.connect(str(task_store.canonical_db_path(root)))
    try:
        card = json.loads(
            conn.execute("SELECT card_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]
        )
        mutate(card)
        conn.execute(
            "UPDATE tasks SET card_json=? WHERE task_id=?", (json.dumps(card), task_id)
        )
        conn.commit()
    finally:
        conn.close()


def _pointers(ns: SimpleNamespace) -> dict:
    return {"task_id": ns.task_id, "request_id": ns.request_id, "claim_epoch": 1}


def _ready_payload(ns: SimpleNamespace, stage: str) -> dict:
    return {"plan": PLAN, "design": DESIGN}.get(stage) or _pointers(ns)


def _record(ns, stage, payload, request_id=None, state="ready"):
    return append_stage(
        ns.root, ns.repo_id, ns.case_id, stage, state, payload, request_id or f"R-{stage}"
    )


def _record_through(ns: SimpleNamespace, last: str) -> None:
    for stage in STAGES[: STAGES.index(last) + 1]:
        _record(ns, stage, _ready_payload(ns, stage))


def _stage_rows(ns: SimpleNamespace) -> list[tuple]:
    conn = sqlite3.connect(str(ns.root.joinpath(*CASES_DB_REL)))
    try:
        return conn.execute(
            "SELECT stage, state, evidence_sha256 FROM stage_receipts "
            "WHERE case_id=? ORDER BY id",
            (ns.case_id,),
        ).fetchall()
    finally:
        conn.close()


def _insert_legacy_receipt(ns, stage, state, payload, request_id) -> str:
    """A receipt as the store wrote it before the gate: no evidence at all."""
    canonical = {
        "case_id": ns.case_id,
        "payload": payload,
        "repo_id": ns.repo_id,
        "request_id": request_id,
        "stage": stage,
        "state": state,
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    conn = sqlite3.connect(str(ns.root.joinpath(*CASES_DB_REL)))
    try:
        conn.execute(
            "INSERT INTO stage_receipts (case_id, repo_id, request_id, stage, state, "
            "payload_json, receipt_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ns.case_id, ns.repo_id, request_id, stage, state,
                json.dumps(payload, sort_keys=True, separators=(",", ":")), digest, NOW,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return digest


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # The gate reads the launcher's ledger and bundles where the launcher writes
    # them; an inherited override would point it at another repository's.
    monkeypatch.delenv(process_launcher.PROCESS_LOG_ENV, raising=False)
    monkeypatch.delenv(process_launcher.PROCESS_DIR_ENV, raising=False)
    return _bootstrap(tmp_path / "repo", "sdlc-evidence-test")


@pytest.fixture
def contracted(repo):
    """A case bound to a claimed canonical card that has no candidate yet."""
    _insert_claimed(repo.root, _contract(TASK_ID))
    create_case(repo.root, repo.repo_id, CASE_ID, "R-case", {"task_id": TASK_ID})
    return SimpleNamespace(
        root=repo.root,
        repo_id=repo.repo_id,
        task_id=TASK_ID,
        case_id=CASE_ID,
        request_id=f"req-{TASK_ID}",
    )


@pytest.fixture
def accepted(contracted):
    """The same case once a real candidate was sealed and canonically accepted."""
    sealed = _seal(contracted.root, TASK_ID)
    receipt = _accept(contracted.root, TASK_ID, sealed)
    return SimpleNamespace(**vars(contracted), receipt=receipt, sealed=sealed)


def test_real_candidate_proves_supported_stages_end_to_end(accepted):
    receipts = {}
    for stage in ("plan", "design", "build", "test"):
        receipt = _record(accepted, stage, _ready_payload(accepted, stage))
        assert receipt["idempotent"] is False
        assert len(receipt["evidence_sha256"]) == 64
        receipts[stage] = receipt
    case = read_case(accepted.root, accepted.repo_id, accepted.case_id)
    for stage in ("plan", "design", "build", "test"):
        packet = case["stages"][stage]
        assert packet["state"] == "ready", packet
        assert packet["evidence_sha256"] == receipts[stage]["evidence_sha256"]
        assert packet["evidence"]["schema_id"] == sdlc_stage_evidence.SCHEMA_ID
    design = case["stages"]["design"]["evidence"]
    build = case["stages"]["build"]["evidence"]
    test = case["stages"]["test"]["evidence"]
    assert design["contract_sha256"] == build["contract_sha256"]
    assert build["request_id"] == accepted.request_id
    assert build["attempt_artifact_manifest_sha256"] == accepted.receipt[
        "attempt_artifact_manifest_id"
    ]
    assert "unresolved" not in build
    assert build["route"] == {
        "runner": RUNNER,
        "adapter_id": ADAPTER,
        "model": MODEL,
        "execution_mode": "provider_worker",
        "sandbox_backend": "landlock",
        "attempt_manifest_sha256": accepted.sealed.manifest["manifest_sha256"],
    }
    edit = build["semantic_edit"]
    assert (edit["state"], edit["source"]) == ("verified", "worker_mcp_gate.verification")
    assert (edit["apply_receipt_count"], edit["apply_receipts_joined"]) == (1, 1)
    assert edit["runtime_observed"] is True
    assert build["effective_effort_context"] == {
        "state": "not_applicable",
        "reason": "adapter_emits_no_effort_context_receipt",
        "adapter_id": ADAPTER,
    }
    assert test["candidate_sha256"] == build["candidate_sha256"]
    assert test["accepted_outcome_receipt_id"] == accepted.receipt["receipt_id"]
    assert test["verification"]["reason"] == "evidence_verdict_passed"
    for stage in ("deploy", "maintain"):
        assert case["stages"][stage]["state"] == "unknown"
    assert case["cycle"] == {
        "state": "incomplete",
        "blocking_stage": "deploy",
        "reason": "no_stage_receipt",
    }


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {},
            {
                "plan": "stage_evidence_refused:plan:plan_content_missing:intent",
                "design": "stage_evidence_refused:design:design_content_missing:acceptance_criteria",
                "build": "stage_evidence_refused:build:identity_pointer_missing:task_id",
                "test": "stage_evidence_refused:test:identity_pointer_missing:task_id",
                "deploy": "missing ready predecessor: plan (no_stage_receipt)",
                "maintain": "missing ready predecessor: plan (no_stage_receipt)",
            },
        ),
        (
            SELF_ATTESTED,
            {
                stage: f"stage_evidence_refused:{stage}:self_attested_verdict:passed"
                for stage in STAGES
            },
        ),
    ],
    ids=["empty", "self_attested"],
)
def test_six_ready_requests_without_canonical_proof_fail_closed(contracted, payload, expected):
    """The reported bypass: six consecutive ready states with nothing behind them."""
    for stage in STAGES:
        with pytest.raises(SdlcCaseValidationError) as refused:
            _record(contracted, stage, payload, request_id=f"R-bypass-{stage}")
        assert str(refused.value).startswith(expected[stage]), str(refused.value)
    assert _stage_rows(contracted) == []
    case = read_case(contracted.root, contracted.repo_id, contracted.case_id)
    assert {packet["state"] for packet in case["stages"].values()} == {"unknown"}
    assert case["cycle"]["state"] == "incomplete"


@pytest.mark.parametrize("stage", STAGES)
def test_caller_asserted_verdicts_are_refused_even_beside_real_evidence(accepted, stage):
    _record_through(accepted, "test")
    base = {} if stage in ("deploy", "maintain") else _ready_payload(accepted, stage)
    for key, value in (
        ("verified", True),
        ("passed", True),
        ("candidate_sha256", "a" * 64),
        ("release_receipt", {"ok": True}),
    ):
        with pytest.raises(SdlcStageEvidenceRefusal) as refused:
            _record(accepted, stage, {**base, key: value}, request_id=f"R-{stage}-{key}")
        assert refused.value.decision.code == f"self_attested_verdict:{key}"
    assert len(_stage_rows(accepted)) == 4


def test_deploy_and_maintain_name_missing_producers_and_never_pass(accepted):
    _record_through(accepted, "test")
    with pytest.raises(SdlcStageEvidenceRefusal) as unknown_target:
        _record(accepted, "deploy", {"target": "production"})
    assert unknown_target.value.decision.code == "deploy_target_unknown"
    assert unknown_target.value.decision.evidence["missing_producers"] == list(
        sdlc_stage_evidence.MISSING_PRODUCERS["deploy"]
    )
    with pytest.raises(SdlcStageEvidenceRefusal) as no_receipt:
        _record(accepted, "deploy", {}, request_id="R-deploy-bare")
    assert no_receipt.value.decision.code == "missing_producer:release_build_provenance_receipt"
    with pytest.raises(SdlcCaseValidationError, match="missing ready predecessor: deploy"):
        _record(accepted, "maintain", {})
    case = read_case(accepted.root, accepted.repo_id, accepted.case_id)
    assert case["cycle"]["state"] == "incomplete"
    assert case["cycle"]["blocking_stage"] == "deploy"
    assert [row[0] for row in _stage_rows(accepted)] == ["plan", "design", "build", "test"]


def test_maintain_resolves_available_evidence_but_never_passes(accepted):
    before = sdlc_stage_evidence.decide(
        sdlc_stage_evidence.EvidenceReader(accepted.root, accepted.repo_id),
        stage="maintain",
        payload={},
        task_id=accepted.task_id,
        predecessors={},
    )
    assert before.code == "missing_producer:observed_outcome_metrics"
    assert before.evidence["learning"] == {
        "state": "unknown",
        "reason": "learning_disposition_missing",
    }
    assert before.evidence["accepted_outcome_receipt_id"] == accepted.receipt["receipt_id"]
    learning_commit_store.record_disposition(
        accepted.root,
        task_id=accepted.task_id,
        request_id=accepted.request_id,
        disposition=learning_commit_store.DISPOSITION_NO_NEW_LESSON,
        reason="mechanical_change_only",
    )
    after = sdlc_stage_evidence.decide(
        sdlc_stage_evidence.EvidenceReader(accepted.root, accepted.repo_id),
        stage="maintain",
        payload={},
        task_id=accepted.task_id,
        predecessors={},
    )
    assert after.code == "missing_producer:observed_outcome_metrics"
    assert after.evidence["learning"]["state"] == "recorded"
    assert after.evidence["learning"]["disposition"] == "no_new_lesson"
    assert after.evidence["missing_producers"] == list(
        sdlc_stage_evidence.MISSING_PRODUCERS["maintain"]
    )
    assert not after.ready and after.fingerprint == ""


def test_an_arbitrary_not_applicable_policy_cannot_skip_a_stage(accepted):
    _record_through(accepted, "test")
    with pytest.raises(SdlcStageEvidenceRefusal) as refused:
        _record(
            accepted,
            "deploy",
            {"reason": "nothing to deploy", "policy_ref": "policy:skip-deploy"},
            state="not_applicable",
        )
    decision = refused.value.decision
    assert decision.code == "not_applicable_policy_unverifiable"
    assert decision.evidence["missing_producers"] == ["sdlc_not_applicable_policy_registry"]
    assert [row[0] for row in _stage_rows(accepted)] == ["plan", "design", "build", "test"]
    assert read_case(accepted.root, accepted.repo_id, accepted.case_id)["stages"]["deploy"][
        "state"
    ] == "unknown"


def test_legacy_ready_rows_stay_for_audit_but_prove_nothing(contracted):
    digests = {
        stage: _insert_legacy_receipt(contracted, stage, "ready", {}, f"R-legacy-{stage}")
        for stage in STAGES
    }
    _insert_legacy_receipt(
        contracted,
        "maintain",
        "not_applicable",
        {"policy_ref": "policy:skip", "reason": "legacy"},
        "R-legacy-maintain-na",
    )
    case = read_case(contracted.root, contracted.repo_id, contracted.case_id)
    for stage in STAGES[:-1]:
        packet = case["stages"][stage]
        assert packet["state"] == "unknown"
        assert packet["recorded_state"] == "ready"
        assert packet["reason"] == "legacy_unverified"
        assert packet["receipt_sha256"] == digests[stage]
    assert case["stages"]["maintain"]["reason"] == "not_applicable_unverified"
    assert case["cycle"]["state"] == "incomplete"
    replay = _record(contracted, "plan", {}, request_id="R-legacy-plan")
    assert replay["idempotent"] is True
    assert replay["receipt_sha256"] == digests["plan"]
    assert replay["evidence_sha256"] is None
    with pytest.raises(
        SdlcCaseValidationError, match=r"missing ready predecessor: plan \(legacy_unverified\)"
    ):
        _record(contracted, "design", DESIGN)
    proven = _record(contracted, "plan", PLAN, request_id="R-plan-proven")
    assert len(proven["evidence_sha256"]) == 64
    assert stage_packet(contracted.root, contracted.repo_id, contracted.case_id, "plan")[
        "state"
    ] == "ready"
    assert len(_stage_rows(contracted)) == len(STAGES) + 2


def test_task_claim_and_candidate_swaps_fail_closed(accepted):
    _record_through(accepted, "design")
    for key, value in (("task_id", "T-OTHER"), ("claim_epoch", 2), ("request_id", "req-other")):
        with pytest.raises(SdlcStageEvidenceRefusal) as refused:
            _record(accepted, "build", {**_pointers(accepted), key: value}, f"R-swap-{key}")
        assert refused.value.decision.code == f"identity_swap:{key}"
    with pytest.raises(SdlcStageEvidenceRefusal) as plan_swap:
        _record(accepted, "plan", {**PLAN, "task_id": "T-OTHER"}, "R-plan-swap")
    assert plan_swap.value.decision.code == "identity_swap:task_id"
    _record(accepted, "build", _pointers(accepted))
    with pytest.raises(SdlcStageEvidenceRefusal) as test_swap:
        _record(accepted, "test", {**_pointers(accepted), "request_id": "req-other"})
    assert test_swap.value.decision.code == "identity_swap:request_id"
    _mutate_card(accepted.root, accepted.task_id, lambda card: card.update(claim_epoch=2))
    reader = sdlc_stage_evidence.EvidenceReader(accepted.root, accepted.repo_id)
    reclaimed = sdlc_stage_evidence.decide(
        reader, stage="build", payload=_pointers(accepted), task_id=accepted.task_id,
        predecessors={},
    )
    assert reclaimed.code == "claim_epoch_mismatch"
    build = stage_packet(accepted.root, accepted.repo_id, accepted.case_id, "build")
    assert build["state"] == "unknown"
    assert build["reason"] == "evidence_stale"
    assert build["evidence_code"] == "claim_epoch_mismatch"


def test_evidence_from_another_repository_never_counts(tmp_path, accepted):
    foreign = _bootstrap(tmp_path / "foreign", "sdlc-evidence-foreign")
    reader = sdlc_stage_evidence.EvidenceReader(accepted.root, foreign.repo_id)
    assert reader.task(accepted.task_id) == "cross_repository_evidence"
    decision = sdlc_stage_evidence.decide(
        reader, stage="plan", payload=PLAN, task_id=accepted.task_id, predecessors={}
    )
    assert decision.code == "cross_repository_evidence"
    assert decision.next_action == sdlc_stage_evidence.NEXT_ACTIONS["store"]
    with pytest.raises(SdlcCaseConflict, match="cross_repository"):
        append_stage(
            accepted.root, foreign.repo_id, accepted.case_id, "plan", "ready", PLAN, "R-x"
        )
    _insert_claimed(foreign.root, _contract("T-FOREIGN"))
    with pytest.raises(SdlcCaseValidationError, match="task not found"):
        create_case(
            accepted.root, accepted.repo_id, "C-FOREIGN", "R-foreign", {"task_id": "T-FOREIGN"}
        )


def test_a_later_edit_of_a_promoted_path_makes_the_tested_candidate_stale(accepted):
    _record_through(accepted, "test")
    (accepted.root / PROMOTED).write_bytes(b"edited after acceptance\n")
    packet = stage_packet(accepted.root, accepted.repo_id, accepted.case_id, "test")
    assert packet["state"] == "unknown"
    assert packet["recorded_state"] == "ready"
    assert packet["reason"] == "evidence_stale"
    assert packet["evidence_code"] == (
        "acceptance_receipt_invalid:accepted_outcome_receipt_canonical_hash_mismatch"
    )
    assert packet["next_action"] == sdlc_stage_evidence.NEXT_ACTIONS["test"]
    assert packet["receipt_sha256"]
    assert stage_packet(accepted.root, accepted.repo_id, accepted.case_id, "build")[
        "state"
    ] == "ready"
    assert read_case(accepted.root, accepted.repo_id, accepted.case_id)["cycle"][
        "blocking_stage"
    ] == "test"


def test_a_replaced_candidate_invalidates_the_build_that_named_the_old_one(accepted):
    _record_through(accepted, "build")
    bundle_dir = accepted.sealed.bundle_dir
    payloads = {
        role: json.loads((bundle_dir / name).read_text(encoding="utf-8"))
        for role, name in attempt_artifacts.ROLE_FILENAMES.items()
        if (bundle_dir / name).exists()
    }
    payloads["metadata"]["sandbox_backend"] = "bubblewrap"
    manifest = attempt_artifacts.persist_json_bundle(
        bundle_dir, attempt_id=accepted.request_id, payloads=payloads
    )
    _append_terminal_event(
        accepted.root, SimpleNamespace(**{**vars(accepted.sealed), "manifest": manifest})
    )

    def resealed(card):
        card["terminal_review"]["evidence"]["attempt_artifact_manifest"] = manifest

    _mutate_card(accepted.root, accepted.task_id, resealed)
    reader = sdlc_stage_evidence.EvidenceReader(accepted.root, accepted.repo_id)
    assert sdlc_stage_evidence.decide(
        reader, stage="build", payload=_pointers(accepted), task_id=accepted.task_id,
        predecessors={},
    ).ready
    build = stage_packet(accepted.root, accepted.repo_id, accepted.case_id, "build")
    assert build["state"] == "unknown"
    assert build["reason"] == "evidence_changed"
    with pytest.raises(
        SdlcCaseValidationError, match=r"missing ready predecessor: build \(evidence_changed\)"
    ):
        _record(accepted, "test", _pointers(accepted))


def test_a_changed_contract_stales_design_and_refuses_the_old_candidate(accepted):
    _record_through(accepted, "design")
    _mutate_card(
        accepted.root,
        accepted.task_id,
        lambda card: card["acceptance"].append("A criterion added after sealing."),
    )
    design = stage_packet(accepted.root, accepted.repo_id, accepted.case_id, "design")
    assert (design["state"], design["reason"]) == ("unknown", "evidence_changed")
    with pytest.raises(
        SdlcCaseValidationError, match=r"missing ready predecessor: design \(evidence_changed\)"
    ):
        _record(accepted, "build", _pointers(accepted))
    reader = sdlc_stage_evidence.EvidenceReader(accepted.root, accepted.repo_id)
    decision = sdlc_stage_evidence.decide(
        reader, stage="build", payload=_pointers(accepted), task_id=accepted.task_id,
        predecessors={},
    )
    assert decision.code == "candidate_contract_mismatch"


def test_a_contradicting_predecessor_fails_closed(accepted):
    reader = sdlc_stage_evidence.EvidenceReader(accepted.root, accepted.repo_id)
    test = sdlc_stage_evidence.decide(
        reader,
        stage="test",
        payload=_pointers(accepted),
        task_id=accepted.task_id,
        predecessors={"build": {"task_id": accepted.task_id, "candidate_sha256": "0" * 64}},
    )
    assert test.code == "predecessor_mismatch:build.candidate_sha256"
    build = sdlc_stage_evidence.decide(
        reader,
        stage="build",
        payload=_pointers(accepted),
        task_id=accepted.task_id,
        predecessors={"design": {"task_id": "T-OTHER", "contract_sha256": "0" * 64}},
    )
    assert build.code == "predecessor_mismatch:design.task_id"


def test_mutated_stored_evidence_is_refused_as_proof(accepted):
    _record_through(accepted, "design")
    conn = sqlite3.connect(str(accepted.root.joinpath(*CASES_DB_REL)))
    try:
        conn.execute(
            "UPDATE stage_receipts SET evidence_json=replace(evidence_json, ?, ?) "
            "WHERE stage='plan'",
            (accepted.task_id, "T-FORGED"),
        )
        conn.commit()
    finally:
        conn.close()
    case = read_case(accepted.root, accepted.repo_id, accepted.case_id)
    assert case["stages"]["plan"]["reason"] == "evidence_integrity_mismatch"
    assert case["stages"]["design"]["reason"] == "stale_predecessor"
    assert case["stages"]["design"]["stale_predecessor"] == "plan"


def test_failed_or_rewritten_verdicts_cannot_prove_test(contracted):
    sealed = _seal(contracted.root, TASK_ID, returncode=1)
    _accept(contracted.root, TASK_ID, sealed)
    _record_through(contracted, "build")
    with pytest.raises(SdlcStageEvidenceRefusal) as failed:
        _record(contracted, "test", _pointers(contracted))
    assert failed.value.decision.code == "validation_not_passed:evidence_verdict_failed"

    def forge(card):
        card["terminal_review"]["deterministic_verification"].update(
            {"pass": True, "reason": "evidence_verdict_passed"}
        )

    _mutate_card(contracted.root, TASK_ID, forge)
    with pytest.raises(SdlcStageEvidenceRefusal) as forged:
        _record(contracted, "test", _pointers(contracted), request_id="R-test-forged")
    assert forged.value.decision.code == "verification_record_contradicted"
    assert [row[0] for row in _stage_rows(contracted)] == ["plan", "design", "build"]


def test_unaccepted_candidate_proves_build_but_not_test(contracted):
    _seal(contracted.root, TASK_ID)
    _record_through(contracted, "build")
    with pytest.raises(SdlcStageEvidenceRefusal) as refused:
        _record(contracted, "test", _pointers(contracted))
    assert refused.value.decision.code == "candidate_not_accepted:review"
    assert refused.value.decision.next_action == sdlc_stage_evidence.NEXT_ACTIONS["test"]


def test_path_traversal_in_promoted_or_sealed_paths_fails_closed(accepted):
    _record_through(accepted, "build")

    def escape_receipt(card):
        card["accept_evidence"]["accepted_outcome_receipt"]["promoted_paths"] = [
            "../outside.py"
        ]

    _mutate_card(accepted.root, accepted.task_id, escape_receipt)
    with pytest.raises(SdlcStageEvidenceRefusal) as promoted:
        _record(accepted, "test", _pointers(accepted))
    assert promoted.value.decision.code == "evidence_path_traversal"

    def escape_candidate(card):
        sealed = card["terminal_review"]["evidence"]
        sealed["changed_paths"] = ["../outside.py"]
        sealed["changed_path_hashes"] = {"../outside.py": "0" * 64}

    _mutate_card(accepted.root, accepted.task_id, escape_candidate)
    decision = sdlc_stage_evidence.decide(
        sdlc_stage_evidence.EvidenceReader(accepted.root, accepted.repo_id),
        stage="build",
        payload=_pointers(accepted),
        task_id=accepted.task_id,
        predecessors={},
    )
    assert decision.code == "evidence_path_traversal"


@pytest.mark.parametrize(
    ("refs", "code"),
    [
        (["file:../secret.txt"], "evidence_ref_path_traversal"),
        (["file:/etc/passwd"], "evidence_ref_path_traversal"),
        (["file:" + "a" * 300], "evidence_ref_oversized"),
        (["task:T-1"] * 33, "evidence_ref_oversized"),
        (["gopher:hole"], "evidence_ref_malformed"),
        (["task:has space"], "evidence_ref_malformed"),
        ([7], "evidence_ref_malformed"),
        ([], "evidence_refs_missing"),
    ],
)
def test_malformed_oversized_and_traversing_refs_are_refused(contracted, refs, code):
    with pytest.raises(SdlcStageEvidenceRefusal) as refused:
        _record(contracted, "plan", {**PLAN, "evidence_refs": refs})
    assert refused.value.decision.code == code
    assert _stage_rows(contracted) == []


def test_an_oversized_task_card_is_never_parsed(accepted, monkeypatch):
    monkeypatch.setattr(sdlc_stage_evidence, "MAX_TASK_CARD_CHARS", 64)
    with pytest.raises(SdlcStageEvidenceRefusal) as refused:
        _record(accepted, "plan", PLAN)
    assert refused.value.decision.code == "evidence_oversized:task_card"


def test_an_unbound_case_has_no_approval_authority(repo):
    create_case(repo.root, repo.repo_id, "C-UNBOUND", "R-unbound", {})
    unbound = SimpleNamespace(root=repo.root, repo_id=repo.repo_id, case_id="C-UNBOUND")
    with pytest.raises(SdlcStageEvidenceRefusal) as refused:
        _record(unbound, "plan", PLAN)
    assert refused.value.decision.code == "approval_authority_missing"
    assert refused.value.decision.next_action == sdlc_stage_evidence.NEXT_ACTIONS["binding"]


def test_exact_replay_is_idempotent_and_changed_bytes_conflict(accepted):
    _record_through(accepted, "build")
    first = _record(accepted, "test", _pointers(accepted))
    replay = _record(accepted, "test", _pointers(accepted))
    assert replay["idempotent"] is True
    assert (replay["receipt_sha256"], replay["evidence_sha256"]) == (
        first["receipt_sha256"],
        first["evidence_sha256"],
    )
    with pytest.raises(SdlcCaseConflict, match="request_id conflict"):
        _record(accepted, "test", {**_pointers(accepted), "evidence_refs": ["task:T-GATE"]})
    assert [row[0] for row in _stage_rows(accepted)] == ["plan", "design", "build", "test"]


def test_evidence_replaced_between_resolution_and_commit_is_not_recorded(
    accepted, monkeypatch
):
    _record_through(accepted, "build")
    real_decide = sdlc_stage_evidence.decide

    def decide_then_replace(reader, **kwargs):
        decision = real_decide(reader, **kwargs)
        if kwargs["stage"] == "test":
            assert decision.ready, decision
            _mutate_card(
                accepted.root,
                accepted.task_id,
                lambda card: card.update(accepted_by="rival-coordinator"),
            )
        return decision

    monkeypatch.setattr(sdlc_stage_evidence, "decide", decide_then_replace)
    with pytest.raises(SdlcCaseConflict, match="stage_evidence_changed_during_commit"):
        _record(accepted, "test", _pointers(accepted))
    assert [row[0] for row in _stage_rows(accepted)] == ["plan", "design", "build"]


def test_evidence_resolution_is_read_only_and_spawns_nothing(accepted, monkeypatch):
    _record_through(accepted, "test")

    def canonical_state():
        conn = sqlite3.connect(str(task_store.canonical_db_path(accepted.root)))
        try:
            return (
                conn.execute("SELECT task_id, status, card_json, updated_at FROM tasks").fetchall(),
                conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            )
        finally:
            conn.close()

    before = canonical_state()

    def no_process(*args, **kwargs):
        raise AssertionError("evidence resolution must not spawn a process")

    monkeypatch.setattr(subprocess, "Popen", no_process)
    reader = sdlc_stage_evidence.EvidenceReader(accepted.root, accepted.repo_id)
    for stage in ("plan", "design", "build", "test"):
        decision = sdlc_stage_evidence.decide(
            reader,
            stage=stage,
            payload=_ready_payload(accepted, stage),
            task_id=accepted.task_id,
            predecessors={},
        )
        assert decision.ready, decision
    assert canonical_state() == before


def _decide_build(ns: SimpleNamespace) -> sdlc_stage_evidence.StageDecision:
    return sdlc_stage_evidence.decide(
        sdlc_stage_evidence.EvidenceReader(ns.root, ns.repo_id),
        stage="build",
        payload=_pointers(ns),
        task_id=ns.task_id,
        predecessors={},
    )


def test_vscode_lm_build_proves_the_effective_effort_context_receipt(contracted):
    _seal(
        contracted.root,
        TASK_ID,
        adapter_id=runtime_adapters.VSCODE_LM_ADAPTER,
        model=VSCODE_MODEL,
        host_receipt=_host_receipt(contracted.request_id),
    )
    _record_through(contracted, "build")
    build = stage_packet(contracted.root, contracted.repo_id, contracted.case_id, "build")
    assert build["state"] == "ready", build
    evidence = build["evidence"]
    assert (evidence["route"]["adapter_id"], evidence["route"]["model"]) == (
        runtime_adapters.VSCODE_LM_ADAPTER,
        VSCODE_MODEL,
    )
    assert evidence["effective_effort_context"] == {
        "state": "verified",
        "source": "reasoning_context_attempt",
        "requested_profile": "canonical_high",
        "option_status": "applied",
        "send_turn_count": 2,
        "context_capacity_tokens": 131072,
        "context_capacity_source": "model.maxInputTokens",
        "host_model_id": "glm-5.2",
    }


@pytest.mark.parametrize(
    ("host_receipt", "code"),
    [
        (None, "effective_effort_context_unknown:receipt_absent"),
        (
            {"repo_id": "another-repo"},
            "effective_effort_context_unknown:receipt_identity_mismatch",
        ),
        (
            {"send_turn_count": 0},
            "effective_effort_context_unknown:receipt_inconsistent",
        ),
    ],
    ids=["absent", "foreign_repo", "never_sent"],
)
def test_an_unknown_effort_context_is_refused_not_passed(contracted, host_receipt, code):
    receipt = None if host_receipt is None else _host_receipt(
        contracted.request_id, **host_receipt
    )
    _seal(
        contracted.root,
        TASK_ID,
        adapter_id=runtime_adapters.VSCODE_LM_ADAPTER,
        model=VSCODE_MODEL,
        host_receipt=receipt,
    )
    _record_through(contracted, "design")
    with pytest.raises(SdlcStageEvidenceRefusal) as refused:
        _record(contracted, "build", _pointers(contracted))
    assert refused.value.decision.code == code
    assert refused.value.decision.next_action == sdlc_stage_evidence.NEXT_ACTIONS["build"]
    assert [row[0] for row in _stage_rows(contracted)] == ["plan", "design"]


def test_a_missing_or_foreign_effort_receipt_fails_closed(contracted):
    attempt = _seal(
        contracted.root,
        TASK_ID,
        adapter_id=runtime_adapters.VSCODE_LM_ADAPTER,
        model=VSCODE_MODEL,
        host_receipt=_host_receipt(contracted.request_id),
        terminal_event=False,
    )
    _append_terminal_event(contracted.root, attempt, reasoning_context_attempt=None)
    assert _decide_build(contracted).code == "effective_effort_context_missing"
    other_claim = process_launcher._reasoning_context_attempt_from_output(
        contracted.root / f"stdout-{attempt.request_id}.jsonl",
        {
            "adapter_id": attempt.adapter_id,
            "model": attempt.model,
            "task_id": TASK_ID,
            "claim_epoch": 2,
            "vscode_lm_bridge": {"repo_id": BRIDGE_REPO},
        },
        attempt.request_id,
    )
    _append_terminal_event(contracted.root, attempt, reasoning_context_attempt=other_claim)
    assert _decide_build(contracted).code == "effective_effort_context_identity_mismatch"
    _append_terminal_event(contracted.root, attempt)
    assert _decide_build(contracted).ready


def test_an_effort_receipt_on_an_adapter_that_never_emits_one_is_contradicted(contracted):
    attempt = _seal(contracted.root, TASK_ID, terminal_event=False)
    forged = {
        "schema_id": process_launcher.REASONING_CONTEXT_ATTEMPT_EVENT_SCHEMA_ID,
        "identity": {"task_id": TASK_ID, "request_id": attempt.request_id},
        "receipt": _host_receipt(attempt.request_id),
    }
    _append_terminal_event(contracted.root, attempt, reasoning_context_attempt=forged)
    assert _decide_build(contracted).code == "effective_effort_context_contradicted"


def test_terminal_event_must_exist_and_agree_with_the_sealed_candidate(contracted):
    attempt = _seal(contracted.root, TASK_ID, terminal_event=False)
    _record_through(contracted, "design")
    with pytest.raises(SdlcStageEvidenceRefusal) as missing:
        _record(contracted, "build", _pointers(contracted))
    assert missing.value.decision.code == "build_terminal_event_missing"
    for overrides, code in (
        ({"task_id": "T-OTHER"}, "build_terminal_event_foreign"),
        ({"adapter_id": "another_adapter"}, "route_mismatch"),
        ({"worker_mcp_gate": _gate(verified=False)}, "build_terminal_event_contradicted"),
        ({"attempt_artifact_manifest": {"attempt_id": "x"}}, "build_terminal_event_contradicted"),
        ({"semantic_edit_coverage": None}, "semantic_edit_coverage_missing"),
    ):
        _append_terminal_event(contracted.root, attempt, **overrides)
        assert _decide_build(contracted).code == code, overrides
    _append_terminal_event(contracted.root, attempt)
    assert _record(contracted, "build", _pointers(contracted))["evidence_sha256"]


def test_attempt_bundle_tampering_staleness_and_relocation_fail_closed(
    contracted, tmp_path, monkeypatch
):
    attempt = _seal(contracted.root, TASK_ID)
    assert _decide_build(contracted).ready
    monkeypatch.setenv(process_launcher.PROCESS_DIR_ENV, str(tmp_path / "elsewhere"))
    assert _decide_build(contracted).code == "attempt_bundle_foreign"
    monkeypatch.delenv(process_launcher.PROCESS_DIR_ENV)

    def restamp(card):
        sealed = card["terminal_review"]["evidence"]["attempt_artifact_manifest"]
        sealed["manifest_sha256"] = "0" * 64

    original = dict(attempt.manifest)
    _mutate_card(contracted.root, TASK_ID, restamp)
    assert _decide_build(contracted).code == "attempt_bundle_stale"

    def restore(card):
        card["terminal_review"]["evidence"]["attempt_artifact_manifest"] = original

    _mutate_card(contracted.root, TASK_ID, restore)
    assert _decide_build(contracted).ready
    metadata = attempt.bundle_dir / attempt_artifacts.ROLE_FILENAMES["metadata"]
    metadata.write_bytes(metadata.read_bytes().replace(RUNNER.encode(), b"rival_worker"))
    assert _decide_build(contracted).code == "attempt_bundle_invalid"
    (attempt.bundle_dir / attempt_artifacts.MANIFEST_FILENAME).unlink()
    assert _decide_build(contracted).code == "attempt_bundle_missing"


def test_a_card_gate_that_differs_from_the_bundle_is_contradicted(contracted):
    _seal(contracted.root, TASK_ID)

    def swap_gate(card):
        card["terminal_review"]["evidence"]["worker_mcp_gate"] = _gate(gated=False)

    _mutate_card(contracted.root, TASK_ID, swap_gate)
    assert _decide_build(contracted).code == "worker_mcp_gate_contradicted"


@pytest.mark.parametrize(
    ("gate", "expected"),
    [
        (_gate(gated=True, verified=False), "edit_ledger_unverified"),
        (_gate(gated=False, verified=False), "worker_mcp_gate_not_gated"),
    ],
    ids=["gated_unverified", "not_gated"],
)
def test_edit_evidence_is_verified_or_verifiably_not_owed(contracted, gate, expected):
    _seal(contracted.root, TASK_ID, gate=gate)
    decision = _decide_build(contracted)
    if expected == "edit_ledger_unverified":
        assert decision.code == expected
        return
    assert decision.ready, decision
    edit = decision.evidence["semantic_edit"]
    assert (edit["state"], edit["reason"], edit["task_type"]) == (
        "not_applicable",
        expected,
        "research",
    )
    assert edit["coverage"]["measured"] is False
    assert edit["coverage"]["unmeasured_reason"] == "ledger_unverified"
