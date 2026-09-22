from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import sqlite3
from types import SimpleNamespace

from aiworkhub import (
    attempt_artifacts,
    core,
    process_event_ledger,
    process_launcher,
    process_launcher_acceptance,
    server,
    task_engine,
    task_store,
)
from aiworkhub.repository_state import bootstrap_repository
from aiworkhub.sdlc_case_store import CASES_DB_REL, STAGES

RUNNER = "worker"
TOPIC = "sdlc"
PROMOTED = "src/feature.py"
# Structured Plan content; its approval is the bound canonical task, never this.
PLAN_PAYLOAD = {
    "intent": "x",
    "problem": "an observed gap",
    "owner": "manager",
    "expected_outcome": "the gap is closed",
    "risk": "low",
    "evidence_refs": ["file:README.md"],
}
DESIGN_PAYLOAD = {
    "acceptance_criteria": ["an unproven ready request is refused"],
    "constraints": [],
    "affected_contracts": ["aiworkhub_manager_sdlc_stage_record"],
    "alternatives": [],
}
SELF_ATTESTED = {"passed": True, "verified": True, "sha256": "a" * 64}
CASE_TOOLS = (
    "aiworkhub_manager_sdlc_case_create",
    "aiworkhub_manager_sdlc_stage_record",
    "aiworkhub_manager_sdlc_case_get",
    "aiworkhub_manager_sdlc_stage_packet",
    "aiworkhub_manager_sdlc_case_create_for_task",
    "aiworkhub_manager_sdlc_case_for_task",
)


def _cases_db(case_repo):
    return case_repo.root.joinpath(*CASES_DB_REL)


def _authorize_manager(monkeypatch):
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(
        core,
        "_claude_manager_identity",
        lambda: {"provider": "claude", "origin_thread_id": "t1"},
    )


def _bootstrap_repo(root, name):
    root.mkdir(exist_ok=True)
    bootstrap_repository(root, repo_name=name)
    readiness = task_store.storage_readiness(root)
    if not readiness.ready:
        task_store.initialize_repository(root)
        readiness = task_store.storage_readiness(root)
    assert readiness.ready
    return SimpleNamespace(root=root, repo_id=readiness.repo_id)


def case_repo(tmp_path, monkeypatch):
    repo = _bootstrap_repo(tmp_path, "sdlc-case-mcp-test")
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    monkeypatch.delenv(process_launcher.PROCESS_LOG_ENV, raising=False)
    monkeypatch.delenv(process_launcher.PROCESS_DIR_ENV, raising=False)
    return repo


def _seed_task(root, task_id):
    """Seed a canonical task row directly, since task_store exposes no writer."""
    conn = sqlite3.connect(str(task_store.canonical_db_path(root)))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, created_at, updated_at) VALUES (?, ?, ?)",
            (task_id, "2026-09-20T00:00:00+00:00", "2026-09-20T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_contract_task(root, task_id):
    """Seed a claimed canonical card with a falsifiable contract and no candidate."""
    card = {
        "task_id": task_id,
        "runner": RUNNER,
        "topic": TOPIC,
        "objective": "Record only SDLC stages the server can prove.",
        "acceptance": ["An unproven ready request is refused."],
        "validation": ["python3 -m pytest -q tests/test_sdlc_case_mcp.py"],
        "allowed_writes": [PROMOTED],
        "claim_epoch": 1,
    }
    conn = sqlite3.connect(str(task_store.canonical_db_path(root)))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, status, worker_status, objective, "
            "card_json, created_at, updated_at, claimed_by) "
            "VALUES (?, ?, ?, 'processing', 'claimed', ?, ?, ?, ?, ?)",
            (
                task_id, RUNNER, TOPIC, card["objective"], json.dumps(card),
                "2026-09-20T00:00:00+00:00", "2026-09-20T00:00:00+00:00", RUNNER,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _native_attempt(root, task_id, request_id, digest, validation, required_outputs):
    """Seal one attempt's bundle and terminal event through the launcher's producers."""
    gate = {
        "gated": True,
        "task_type": "code",
        "satisfied": True,
        "verification": {
            "ok": True,
            "semantic_edit_apply_receipts": [{
                "path_sha256": process_launcher.semantic_edit_path_identifier(PROMOTED),
                "range_count": 1,
            }],
        },
    }
    manifest = attempt_artifacts.persist_json_bundle(
        root / process_launcher.PROCESS_DIR_DEFAULT_REL / "attempt-artifacts" / request_id,
        attempt_id=request_id,
        payloads={
            "metadata": {
                "schema_id": "aiworkhub.attempt_metadata.v1",
                "request_identity": {
                    "request_id": request_id, "task_id": task_id, "runner": RUNNER, "topic": TOPIC,
                },
                "adapter_id": "codex_exec",
                "model": "gpt-sdlc-test",
            },
            "diff": {
                "schema_id": "aiworkhub.attempt_diff_index.v1",
                "changed_paths": [PROMOTED],
                "changed_path_hashes": {PROMOTED: digest},
                "required_outputs": required_outputs,
            },
            "validation": {
                "schema_id": "aiworkhub.attempt_validation.v1",
                "checks": validation,
                "worker_mcp_gate": gate,
            },
            "usage": {"schema_id": "aiworkhub.attempt_usage.v1"},
            "review": {"schema_id": "aiworkhub.attempt_review.v1", "target_state": "review_ready"},
        },
    )
    semantic_edit = process_launcher._semantic_edit_evidence_from_output(
        root / "absent-stdout.jsonl", worker_mcp_gate=gate
    )
    process_event_ledger.append_event(
        root / process_launcher.PROCESS_LOG_DEFAULT_REL,
        {
            "schema_id": "aiworkhub.task_mcp.process_event.v1",
            "request_id": request_id,
            "task_id": task_id,
            "runner": RUNNER,
            "adapter_id": "codex_exec",
            "state": "review_ready",
            "attempt_artifact_manifest": manifest,
            "worker_mcp_gate": gate,
            "semantic_edit": semantic_edit,
            "semantic_edit_coverage": process_launcher._semantic_edit_coverage(
                [PROMOTED], worker_mcp_gate=gate, runtime_evidence=semantic_edit
            ),
        },
    )
    return manifest, gate


def _seal_and_accept(root, task_id):
    """Drive the real terminal-review and acceptance producers for one candidate."""
    request_id = f"req-{task_id}"
    content = b"proven\n"
    (root / PROMOTED).parent.mkdir(parents=True, exist_ok=True)
    (root / PROMOTED).write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    validation = [{"command": "python3 -m pytest -q", "returncode": 0}]
    required_outputs = [{"path": PROMOTED, "sha256": digest, "bytes": len(content)}]
    manifest, gate = _native_attempt(
        root, task_id, request_id, digest, validation, required_outputs
    )
    ok, state = task_store.mark_terminal_review(
        root,
        task_id,
        runner=RUNNER,
        substatus="review_ready",
        evidence={
            "request_id": request_id,
            "request_identity": {"request_id": request_id},
            "validation": validation,
            "required_outputs": required_outputs,
            "changed_paths": [PROMOTED],
            "changed_path_hashes": {PROMOTED: digest},
            "worker_mcp_gate": gate,
            "attempt_artifact_manifest": manifest,
            "workspace": {"base_oid": "base-oid"},
        },
    )
    assert (ok, state) == (True, "review")
    receipt = process_launcher_acceptance.accepted_outcome_receipt(
        root,
        task_id=task_id,
        request_id=request_id,
        claim_epoch=1,
        base_oid="base-oid",
        promoted_paths=[PROMOTED],
        changed_path_hashes={PROMOTED: digest},
        attempt_artifact_manifest=manifest,
    )
    accepted = task_engine.accept_review(
        root,
        task_id,
        runner=RUNNER,
        topic=TOPIC,
        request_id=request_id,
        evidence={"promoted_paths": [PROMOTED]},
        accepted_outcome_receipt=receipt,
    )
    assert accepted["ok"] is True, accepted
    return receipt


def test_manager_case_write_respects_write_gate(monkeypatch, tmp_path):
    repo = case_repo(tmp_path, monkeypatch)
    monkeypatch.delenv("AIWORKHUB_ALLOW_WRITES", raising=False)
    result = server.aiworkhub_manager_sdlc_case_create("C1", "R-create", {})
    assert result["ok"] is False
    assert not _cases_db(repo).exists()


def test_unverified_manager_leaves_store_absent(monkeypatch, tmp_path):
    repo = case_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: None)
    result = server.aiworkhub_manager_sdlc_case_create("C1", "R-create", {})
    assert result["ok"] is False
    assert not _cases_db(repo).exists()


def test_write_gate_off_leaves_existing_store_byte_identical(monkeypatch, tmp_path):
    repo = case_repo(tmp_path, monkeypatch)
    _authorize_manager(monkeypatch)
    created = server.aiworkhub_manager_sdlc_case_create("C1", "R-create", {})
    assert created["ok"] is True
    path = _cases_db(repo)
    before = path.read_bytes()
    monkeypatch.delenv("AIWORKHUB_ALLOW_WRITES", raising=False)
    result = server.aiworkhub_manager_sdlc_stage_record(
        "C1", "plan", "ready", PLAN_PAYLOAD, "R1"
    )
    assert result["ok"] is False
    assert path.read_bytes() == before


def test_cross_repo_authority_refused_before_mutation(monkeypatch, tmp_path):
    repo = case_repo(tmp_path, monkeypatch)
    _authorize_manager(monkeypatch)
    monkeypatch.setattr(
        task_store,
        "storage_readiness",
        lambda root: SimpleNamespace(
            ready=True, repo_id="repo_" + "a" * 32, reason="ready"
        ),
    )
    result = server.aiworkhub_manager_sdlc_case_create("C1", "R-create", {})
    assert result["ok"] is False
    assert not _cases_db(repo).exists()


def test_authenticated_create_and_plan_round_trip_digest(monkeypatch, tmp_path):
    repo = case_repo(tmp_path, monkeypatch)
    _seed_contract_task(repo.root, "T1")
    _authorize_manager(monkeypatch)
    created = server.aiworkhub_manager_sdlc_case_create("C1", "R-create", {"task_id": "T1"})
    assert created["ok"] is True
    assert created["repo_id"] == repo.repo_id
    recorded = server.aiworkhub_manager_sdlc_stage_record(
        "C1", "plan", "ready", PLAN_PAYLOAD, "R1"
    )
    assert recorded["ok"] is True
    digest = recorded["receipt_sha256"]
    packet = server.aiworkhub_manager_sdlc_stage_packet("C1", "plan")
    assert packet["receipt_sha256"] == digest
    assert packet["state"] == "ready"
    assert packet["evidence_sha256"] == recorded["evidence_sha256"]
    assert packet["repo_id"] == repo.repo_id
    case = server.aiworkhub_manager_sdlc_case_get("C1")
    assert case["stages"]["plan"]["receipt_sha256"] == digest
    assert case["repo_id"] == repo.repo_id


def test_unknown_stage_is_typed_refusal(monkeypatch, tmp_path):
    case_repo(tmp_path, monkeypatch)
    _authorize_manager(monkeypatch)
    server.aiworkhub_manager_sdlc_case_create("C1", "R-create", {})
    recorded = server.aiworkhub_manager_sdlc_stage_record(
        "C1", "not-a-stage", "ready", PLAN_PAYLOAD, "R-bad"
    )
    assert recorded["ok"] is False
    assert recorded["reason"]
    packet = server.aiworkhub_manager_sdlc_stage_packet("C1", "not-a-stage")
    assert packet["ok"] is False
    assert packet["reason"]


def test_missing_case_and_stage_are_typed_unknown(monkeypatch, tmp_path):
    case_repo(tmp_path, monkeypatch)
    missing = server.aiworkhub_manager_sdlc_case_get("missing")
    assert missing["ok"] is False
    assert missing["reason"]
    packet = server.aiworkhub_manager_sdlc_stage_packet("missing", "plan")
    assert packet["state"] == "unknown"


def test_public_case_tools_registered_without_repo_id():
    for name in CASE_TOOLS:
        fn = getattr(server, name)
        params = inspect.signature(fn).parameters
        assert "repo_id" not in params


def test_task_case_tools_accept_only_task_and_request_identity():
    create = server.aiworkhub_manager_sdlc_case_create_for_task
    read = server.aiworkhub_manager_sdlc_case_for_task
    assert list(inspect.signature(create).parameters) == ["task_id", "request_id"]
    assert list(inspect.signature(read).parameters) == ["task_id"]
    schemas = {tool.name: tool.inputSchema for tool in asyncio.run(server.mcp.list_tools())}
    create_schema = schemas["aiworkhub_manager_sdlc_case_create_for_task"]
    read_schema = schemas["aiworkhub_manager_sdlc_case_for_task"]
    assert set(create_schema["properties"]) == {"task_id", "request_id"}
    assert set(create_schema["required"]) == {"task_id", "request_id"}
    assert set(read_schema["properties"]) == {"task_id"}
    assert set(read_schema["required"]) == {"task_id"}


def test_task_case_create_is_deterministic_and_replay_idempotent(monkeypatch, tmp_path):
    repo = case_repo(tmp_path, monkeypatch)
    _seed_task(repo.root, "T1")
    _seed_task(repo.root, "T2")
    _authorize_manager(monkeypatch)
    created = server.aiworkhub_manager_sdlc_case_create_for_task("T1", "R-bind")
    assert created["ok"] is True
    assert created["idempotent"] is False
    assert created["repo_id"] == repo.repo_id
    assert created["links"] == {"task_id": "T1"}
    replay = server.aiworkhub_manager_sdlc_case_create_for_task("T1", "R-bind")
    assert replay["ok"] is True
    assert replay["idempotent"] is True
    assert replay["case_id"] == created["case_id"]
    assert replay["receipt_sha256"] == created["receipt_sha256"]
    other = server.aiworkhub_manager_sdlc_case_create_for_task("T2", "R-bind")
    assert other["ok"] is True
    assert other["case_id"] != created["case_id"]
    bound = server.aiworkhub_manager_sdlc_case_for_task("T1")
    assert bound["state"] == "bound"
    assert bound["case_id"] == created["case_id"]
    assert bound["task_id"] == "T1"
    assert bound["repo_id"] == repo.repo_id
    assert bound["links"] == {"task_id": "T1"}


def test_task_bound_stages_stay_unknown_until_recorded(monkeypatch, tmp_path):
    repo = case_repo(tmp_path, monkeypatch)
    _seed_contract_task(repo.root, "T1")
    _authorize_manager(monkeypatch)
    created = server.aiworkhub_manager_sdlc_case_create_for_task("T1", "R-bind")
    bound = server.aiworkhub_manager_sdlc_case_for_task("T1")
    assert tuple(bound["stages"]) == STAGES
    assert {packet["state"] for packet in bound["stages"].values()} == {"unknown"}
    recorded = server.aiworkhub_manager_sdlc_stage_record(
        created["case_id"], "plan", "ready", PLAN_PAYLOAD, "R-plan"
    )
    assert recorded["ok"] is True
    after = server.aiworkhub_manager_sdlc_case_for_task("T1")
    assert after["stages"]["plan"]["state"] == "ready"
    assert after["stages"]["plan"]["receipt_sha256"] == recorded["receipt_sha256"]
    assert {
        packet["state"] for stage, packet in after["stages"].items() if stage != "plan"
    } == {"unknown"}


def test_task_case_duplicate_binding_is_refused(monkeypatch, tmp_path):
    repo = case_repo(tmp_path, monkeypatch)
    _seed_task(repo.root, "T1")
    _seed_task(repo.root, "T2")
    _authorize_manager(monkeypatch)
    created = server.aiworkhub_manager_sdlc_case_create_for_task("T1", "R1")
    assert created["ok"] is True
    other_request = server.aiworkhub_manager_sdlc_case_create_for_task("T1", "R2")
    assert other_request["ok"] is False
    assert other_request["reason"] == "request_id conflict"
    other_case = server.aiworkhub_manager_sdlc_case_create("C-other", "R3", {"task_id": "T1"})
    assert other_case["ok"] is False
    assert other_case["reason"] == "task already bound"
    assert server.aiworkhub_manager_sdlc_case_for_task("T1")["case_id"] == created["case_id"]
    legacy = server.aiworkhub_manager_sdlc_case_create("C-legacy", "R0", {"task_id": "T2"})
    assert legacy["ok"] is True
    late = server.aiworkhub_manager_sdlc_case_create_for_task("T2", "R4")
    assert late["ok"] is False
    assert late["reason"] == "task already bound"
    assert server.aiworkhub_manager_sdlc_case_for_task("T2")["case_id"] == "C-legacy"


def test_unbound_task_read_is_typed_unknown_and_creates_no_store(monkeypatch, tmp_path):
    repo = case_repo(tmp_path, monkeypatch)
    _seed_task(repo.root, "T1")
    _seed_task(repo.root, "T2")
    unbound = server.aiworkhub_manager_sdlc_case_for_task("T1")
    assert unbound["state"] == "unknown"
    assert unbound["case_id"] is None
    assert unbound["task_id"] == "T1"
    assert unbound["repo_id"] == repo.repo_id
    assert unbound["links"] == {}
    assert unbound["stages"] == {}
    assert server.aiworkhub_manager_sdlc_case_for_task("T-missing")["state"] == "unknown"
    assert not _cases_db(repo).exists()
    _authorize_manager(monkeypatch)
    assert server.aiworkhub_manager_sdlc_case_create_for_task("T2", "R2")["ok"] is True
    still_unbound = server.aiworkhub_manager_sdlc_case_for_task("T1")
    assert still_unbound["state"] == "unknown"
    assert still_unbound["case_id"] is None
    assert server.aiworkhub_manager_sdlc_case_for_task("")["ok"] is False


def test_task_case_create_refuses_unknown_foreign_and_blank_inputs_before_mutation(
    monkeypatch, tmp_path
):
    primary = case_repo(tmp_path / "primary", monkeypatch)
    foreign = _bootstrap_repo(tmp_path / "foreign", "sdlc-foreign-mcp")
    _seed_task(primary.root, "T1")
    _seed_task(foreign.root, "T-foreign")
    _authorize_manager(monkeypatch)
    for task_id in ("T-foreign", "T-missing"):
        refused = server.aiworkhub_manager_sdlc_case_create_for_task(task_id, "R1")
        assert refused["ok"] is False
        assert refused["reason"] == "task not found"
    for task_id, request_id in (("", "R1"), ("T1", "")):
        refused = server.aiworkhub_manager_sdlc_case_create_for_task(task_id, request_id)
        assert refused["ok"] is False
        assert refused["reason"]
    assert not _cases_db(primary).exists()
    assert not _cases_db(foreign).exists()


def test_task_case_create_denied_write_leaves_store_absent(monkeypatch, tmp_path):
    repo = case_repo(tmp_path, monkeypatch)
    _seed_task(repo.root, "T1")
    _authorize_manager(monkeypatch)
    monkeypatch.delenv("AIWORKHUB_ALLOW_WRITES", raising=False)
    gated = server.aiworkhub_manager_sdlc_case_create_for_task("T1", "R1")
    assert gated["ok"] is False
    assert "AIWORKHUB_ALLOW_WRITES" in gated["stderr"]
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: None)
    unverified = server.aiworkhub_manager_sdlc_case_create_for_task("T1", "R1")
    assert unverified["ok"] is False
    assert unverified["stderr"] == "manager_identity_required:sdlc_case"
    assert not _cases_db(repo).exists()


def test_task_case_create_denied_write_leaves_existing_store_byte_identical(monkeypatch, tmp_path):
    repo = case_repo(tmp_path, monkeypatch)
    _seed_task(repo.root, "T1")
    _seed_task(repo.root, "T2")
    _authorize_manager(monkeypatch)
    assert server.aiworkhub_manager_sdlc_case_create_for_task("T1", "R1")["ok"] is True
    path = _cases_db(repo)
    before = path.read_bytes()
    monkeypatch.delenv("AIWORKHUB_ALLOW_WRITES", raising=False)
    assert server.aiworkhub_manager_sdlc_case_create_for_task("T2", "R2")["ok"] is False
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: None)
    assert server.aiworkhub_manager_sdlc_case_create_for_task("T2", "R2")["ok"] is False
    assert path.read_bytes() == before


def test_task_case_tools_refuse_unready_task_store_before_mutation(monkeypatch, tmp_path):
    repo = case_repo(tmp_path, monkeypatch)
    _seed_task(repo.root, "T1")
    _authorize_manager(monkeypatch)
    monkeypatch.setattr(
        task_store,
        "storage_readiness",
        lambda root: SimpleNamespace(ready=False, repo_id="", reason="missing"),
    )
    for result in (
        server.aiworkhub_manager_sdlc_case_create_for_task("T1", "R1"),
        server.aiworkhub_manager_sdlc_case_for_task("T1"),
    ):
        assert result["ok"] is False
        assert "canonical_task_store_not_ready" in result["stderr"]
    assert not _cases_db(repo).exists()


def test_task_case_tools_refuse_cross_repository_authority_before_mutation(monkeypatch, tmp_path):
    repo = case_repo(tmp_path, monkeypatch)
    _seed_task(repo.root, "T1")
    _authorize_manager(monkeypatch)
    monkeypatch.setattr(
        task_store,
        "storage_readiness",
        lambda root: SimpleNamespace(ready=True, repo_id="repo_" + "a" * 32, reason="ready"),
    )
    for result in (
        server.aiworkhub_manager_sdlc_case_create_for_task("T1", "R1"),
        server.aiworkhub_manager_sdlc_case_for_task("T1"),
    ):
        assert result["ok"] is False
        assert result["reason"] == "cross_repository"
    assert not _cases_db(repo).exists()


def test_six_stage_ready_bypass_is_refused_through_mcp(monkeypatch, tmp_path):
    repo = case_repo(tmp_path, monkeypatch)
    _seed_contract_task(repo.root, "T1")
    _authorize_manager(monkeypatch)
    created = server.aiworkhub_manager_sdlc_case_create_for_task("T1", "R-bind")
    assert created["ok"] is True
    case_id = created["case_id"]
    for label, payload in (("empty", {}), ("asserted", SELF_ATTESTED)):
        for stage in STAGES:
            recorded = server.aiworkhub_manager_sdlc_stage_record(
                case_id, stage, "ready", payload, f"R-{label}-{stage}"
            )
            assert recorded["ok"] is False, recorded
            assert recorded["reason"].startswith(
                ("stage_evidence_refused:", "missing ready predecessor:")
            ), recorded
            if label == "asserted":
                assert recorded["reason"].startswith(
                    f"stage_evidence_refused:{stage}:self_attested_verdict:passed"
                )
            assert "receipt_sha256" not in recorded
    skipped = server.aiworkhub_manager_sdlc_stage_record(
        case_id,
        "deploy",
        "not_applicable",
        {"reason": "nothing to ship", "policy_ref": "policy:any"},
        "R-skip-deploy",
    )
    assert skipped["ok"] is False
    assert skipped["reason"].startswith(
        "stage_evidence_refused:deploy:not_applicable_policy_unverifiable"
    )
    conn = sqlite3.connect(str(_cases_db(repo)))
    try:
        assert conn.execute("SELECT COUNT(*) FROM stage_receipts").fetchone()[0] == 0
    finally:
        conn.close()
    bound = server.aiworkhub_manager_sdlc_case_for_task("T1")
    assert {packet["state"] for packet in bound["stages"].values()} == {"unknown"}
    assert bound["cycle"]["state"] == "incomplete"


def test_real_linked_candidate_passes_supported_stages_through_mcp(monkeypatch, tmp_path):
    repo = case_repo(tmp_path, monkeypatch)
    _seed_contract_task(repo.root, "T1")
    receipt = _seal_and_accept(repo.root, "T1")
    _authorize_manager(monkeypatch)
    case_id = server.aiworkhub_manager_sdlc_case_create_for_task("T1", "R-bind")["case_id"]
    pointers = {"task_id": "T1", "request_id": "req-T1", "claim_epoch": 1}
    payloads = {
        "plan": PLAN_PAYLOAD,
        "design": DESIGN_PAYLOAD,
        "build": pointers,
        "test": pointers,
    }
    for stage, payload in payloads.items():
        recorded = server.aiworkhub_manager_sdlc_stage_record(
            case_id, stage, "ready", payload, f"R-{stage}"
        )
        assert recorded["ok"] is True, recorded
        assert len(recorded["evidence_sha256"]) == 64
    deploy = server.aiworkhub_manager_sdlc_stage_record(
        case_id, "deploy", "ready", {"target": "production"}, "R-deploy"
    )
    assert deploy["ok"] is False
    assert deploy["reason"].startswith("stage_evidence_refused:deploy:deploy_target_unknown")
    maintain = server.aiworkhub_manager_sdlc_stage_record(
        case_id, "maintain", "ready", {}, "R-maintain"
    )
    assert maintain["ok"] is False
    assert maintain["reason"].startswith("missing ready predecessor: deploy")
    bound = server.aiworkhub_manager_sdlc_case_for_task("T1")
    assert [bound["stages"][stage]["state"] for stage in STAGES] == (
        ["ready"] * 4 + ["unknown"] * 2
    )
    assert bound["stages"]["test"]["evidence"]["accepted_outcome_receipt_id"] == (
        receipt["receipt_id"]
    )
    build = bound["stages"]["build"]["evidence"]
    assert build["route"]["adapter_id"] == "codex_exec"
    assert build["semantic_edit"]["state"] == "verified"
    assert build["semantic_edit"]["apply_receipts_joined"] == 1
    assert build["effective_effort_context"]["state"] == "not_applicable"
    assert bound["cycle"] == {
        "state": "incomplete",
        "blocking_stage": "deploy",
        "reason": "no_stage_receipt",
    }
    packet = server.aiworkhub_manager_sdlc_stage_packet(case_id, "test")
    assert packet["state"] == "ready"
    assert packet["evidence"]["candidate_sha256"] == (
        bound["stages"]["build"]["evidence"]["candidate_sha256"]
    )
