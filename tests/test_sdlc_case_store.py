from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from aiworkhub.repository_state import bootstrap_repository
from aiworkhub.sdlc_case_store import (
    CASES_DB_REL,
    SdlcCaseConflict,
    SdlcCaseValidationError,
    append_stage,
    create_case,
    read_case,
    stage_packet,
)
from aiworkhub import task_store

PLAN_PAYLOAD = {"intent": "x", "evidence_refs": ["file:README.md"]}


@pytest.fixture
def case_repo(tmp_path):
    bootstrap_repository(tmp_path, repo_name="sdlc-case-test")
    readiness = task_store.storage_readiness(tmp_path)
    if not readiness.ready:
        task_store.initialize_repository(tmp_path)
        readiness = task_store.storage_readiness(tmp_path)
    assert readiness.ready
    create_case(tmp_path, readiness.repo_id, "C1", "R-create", {})
    return SimpleNamespace(root=tmp_path, repo_id=readiness.repo_id)


def test_create_case_records_one_case(case_repo):
    packet = read_case(case_repo.root, case_repo.repo_id, "C1")
    assert packet["case_id"] == "C1"
    assert packet["repo_id"] == case_repo.repo_id
    db = case_repo.root.joinpath(*CASES_DB_REL)
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 1
    finally:
        conn.close()


def test_append_stage_records_digest(case_repo):
    first = append_stage(
        case_repo.root, case_repo.repo_id, "C1", "plan", "ready", PLAN_PAYLOAD, "R1"
    )
    assert len(first["receipt_sha256"]) == 64
    assert first["idempotent"] is False


def test_same_request_replay_is_idempotent(case_repo):
    first = append_stage(
        case_repo.root, case_repo.repo_id, "C1", "plan", "ready", PLAN_PAYLOAD, "R1"
    )
    second = append_stage(
        case_repo.root, case_repo.repo_id, "C1", "plan", "ready", PLAN_PAYLOAD, "R1"
    )
    assert second["receipt_sha256"] == first["receipt_sha256"]
    assert second["idempotent"] is True


def test_changed_request_bytes_conflict(case_repo):
    append_stage(
        case_repo.root, case_repo.repo_id, "C1", "plan", "ready", PLAN_PAYLOAD, "R1"
    )
    with pytest.raises(SdlcCaseConflict):
        append_stage(
            case_repo.root,
            case_repo.repo_id,
            "C1",
            "plan",
            "ready",
            {"intent": "y", "evidence_refs": ["file:README.md"]},
            "R1",
        )


def test_create_case_replay_is_idempotent(case_repo):
    first = create_case(case_repo.root, case_repo.repo_id, "C1", "R-create", {})
    assert first["idempotent"] is True
    with pytest.raises(SdlcCaseConflict):
        create_case(case_repo.root, case_repo.repo_id, "C1", "R-create", {"task_id": "T1"})


def test_cross_repository_case_is_refused(case_repo):
    with pytest.raises(SdlcCaseConflict):
        read_case(case_repo.root, "repo_foreign", "C1")


def test_cross_repository_append_is_refused(case_repo):
    before = case_repo.root.joinpath(*CASES_DB_REL).stat().st_mtime_ns
    with pytest.raises(SdlcCaseConflict):
        append_stage(
            case_repo.root, "repo_foreign", "C1", "plan", "ready", PLAN_PAYLOAD, "R-x"
        )
    after = case_repo.root.joinpath(*CASES_DB_REL).stat().st_mtime_ns
    assert after == before


def test_deploy_requires_test_predecessor(case_repo):
    for stage in ("plan", "design", "build"):
        append_stage(
            case_repo.root,
            case_repo.repo_id,
            "C1",
            stage,
            "ready",
            {"evidence_refs": ["file:README.md"]},
            "R-" + stage,
        )
    with pytest.raises(SdlcCaseValidationError, match="test"):
        append_stage(
            case_repo.root,
            case_repo.repo_id,
            "C1",
            "deploy",
            "ready",
            {"target": "staging"},
            "R-deploy",
        )


def test_unknown_does_not_count_as_ready(case_repo):
    for stage in ("plan", "design", "build"):
        append_stage(
            case_repo.root,
            case_repo.repo_id,
            "C1",
            stage,
            "ready",
            {"evidence_refs": ["file:README.md"]},
            "R-" + stage,
        )
    append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "test",
        "unknown",
        {"evidence_refs": ["file:README.md"]},
        "R-test",
    )
    with pytest.raises(SdlcCaseValidationError, match="test"):
        append_stage(
            case_repo.root,
            case_repo.repo_id,
            "C1",
            "deploy",
            "ready",
            {"target": "staging"},
            "R-deploy",
        )


def test_not_applicable_requires_reason_and_policy(case_repo):
    with pytest.raises(SdlcCaseValidationError, match="policy_ref"):
        append_stage(
            case_repo.root,
            case_repo.repo_id,
            "C1",
            "plan",
            "not_applicable",
            {"reason": "already approved"},
            "R-na",
        )


def test_not_applicable_requires_nonempty_reason(case_repo):
    with pytest.raises(SdlcCaseValidationError, match="reason"):
        append_stage(
            case_repo.root,
            case_repo.repo_id,
            "C1",
            "plan",
            "not_applicable",
            {"policy_ref": "policy:skip"},
            "R-na-reason",
        )


def test_stage_packet_unknown_when_missing(case_repo):
    packet = stage_packet(case_repo.root, case_repo.repo_id, "C1", "plan")
    assert packet["state"] == "unknown"
    assert packet["stage"] == "plan"
    assert not packet.get("receipt_sha256")


def test_read_packets_are_bounded_and_source_linked(case_repo):
    append_stage(
        case_repo.root, case_repo.repo_id, "C1", "plan", "ready", PLAN_PAYLOAD, "R1"
    )
    packet = stage_packet(case_repo.root, case_repo.repo_id, "C1", "plan")
    assert packet["state"] == "ready"
    assert "file:README.md" in packet["source_refs"]
    assert packet["receipt_sha256"]
    case = read_case(case_repo.root, case_repo.repo_id, "C1")
    plan = case["stages"]["plan"]
    assert plan["state"] == "ready"
    assert "file:README.md" in plan["source_refs"]


def test_two_processes_same_request_id_one_receipt(case_repo):
    script = (
        "import json\n"
        "from pathlib import Path\n"
        "from aiworkhub.sdlc_case_store import append_stage\n"
        f"root = Path({str(case_repo.root)!r})\n"
        f"repo_id = {case_repo.repo_id!r}\n"
        "payload = {'intent': 'x', 'evidence_refs': ['file:README.md']}\n"
        "try:\n"
        "    result = append_stage(root, repo_id, 'C1', 'plan', 'ready', payload, 'R-race')\n"
        "    print(json.dumps({'ok': True, 'receipt_sha256': result['receipt_sha256'],"
        " 'idempotent': result['idempotent']}))\n"
        "except Exception as exc:\n"
        "    print(json.dumps({'ok': False, 'error': type(exc).__name__, 'message': str(exc)}))\n"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    parsed = []
    for proc in procs:
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, err
        parsed.append(json.loads(out))
    receipts = {row["receipt_sha256"] for row in parsed if row.get("ok")}
    assert len(receipts) == 1
    db = case_repo.root.joinpath(*CASES_DB_REL)
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT COUNT(*), payload_json FROM stage_receipts WHERE request_id=?",
            ("R-race",),
        ).fetchone()
        assert row[0] == 1
        json.loads(row[1])
    finally:
        conn.close()


def test_downstream_ready_stales_after_predecessor_regression(case_repo):
    for stage in ("plan", "design", "build", "test", "deploy"):
        append_stage(
            case_repo.root,
            case_repo.repo_id,
            "C1",
            stage,
            "ready",
            {"evidence_refs": ["file:README.md"]},
            "R-" + stage,
        )
    append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "test",
        "blocked",
        {"reason": "failed", "evidence_refs": ["file:README.md"]},
        "R-test-blocked",
    )
    test_packet = stage_packet(case_repo.root, case_repo.repo_id, "C1", "test")
    assert test_packet["state"] == "blocked"
    deploy_packet = stage_packet(case_repo.root, case_repo.repo_id, "C1", "deploy")
    assert deploy_packet["state"] != "ready"
    assert deploy_packet["state"] in {"unknown", "blocked"}
    assert deploy_packet["reason"] == "stale_predecessor"
    assert deploy_packet["stale_predecessor"] == "test"
    case = read_case(case_repo.root, case_repo.repo_id, "C1")
    assert case["stages"]["test"]["state"] == "blocked"
    assert case["stages"]["deploy"]["state"] != "ready"
    assert case["stages"]["deploy"]["reason"] == "stale_predecessor"
    db = case_repo.root.joinpath(*CASES_DB_REL)
    conn = sqlite3.connect(str(db))
    try:
        deploy_ready = conn.execute(
            "SELECT COUNT(*) FROM stage_receipts WHERE case_id=? AND stage=? AND state=?",
            ("C1", "deploy", "ready"),
        ).fetchone()[0]
        test_rows = conn.execute(
            "SELECT COUNT(*) FROM stage_receipts WHERE case_id=? AND stage=?",
            ("C1", "test"),
        ).fetchone()[0]
        assert deploy_ready == 1
        assert test_rows == 2
    finally:
        conn.close()


def test_new_ready_predecessor_does_not_revive_old_deploy(case_repo):
    for stage in ("plan", "design", "build", "test", "deploy"):
        append_stage(
            case_repo.root,
            case_repo.repo_id,
            "C1",
            stage,
            "ready",
            {"evidence_refs": ["file:README.md"]},
            "R-" + stage,
        )
    old_deploy = stage_packet(case_repo.root, case_repo.repo_id, "C1", "deploy")
    old_sha = old_deploy["receipt_sha256"]
    append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "test",
        "blocked",
        {"reason": "failed", "evidence_refs": ["file:README.md"]},
        "R-test-blocked",
    )
    append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "test",
        "ready",
        {"evidence_refs": ["file:tests/test_sdlc_case_store.py"]},
        "R-test-ready-2",
    )
    deploy_packet = stage_packet(case_repo.root, case_repo.repo_id, "C1", "deploy")
    assert deploy_packet["state"] == "unknown"
    assert deploy_packet["reason"] == "stale_predecessor"
    assert deploy_packet["stale_predecessor"] == "test"
    assert deploy_packet["receipt_sha256"] == old_sha
    case = read_case(case_repo.root, case_repo.repo_id, "C1")
    assert case["stages"]["test"]["state"] == "ready"
    assert case["stages"]["deploy"]["state"] == "unknown"
    assert case["stages"]["deploy"]["receipt_sha256"] == old_sha
    new_deploy = append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "deploy",
        "ready",
        {"evidence_refs": ["file:README.md", "file:deploy"]},
        "R-deploy-2",
    )
    refreshed = stage_packet(case_repo.root, case_repo.repo_id, "C1", "deploy")
    assert refreshed["state"] == "ready"
    assert refreshed["receipt_sha256"] == new_deploy["receipt_sha256"]
    assert refreshed["receipt_sha256"] != old_sha
    case = read_case(case_repo.root, case_repo.repo_id, "C1")
    assert case["stages"]["deploy"]["state"] == "ready"
    assert case["stages"]["deploy"]["receipt_sha256"] != old_sha
    db = case_repo.root.joinpath(*CASES_DB_REL)
    conn = sqlite3.connect(str(db))
    try:
        deploy_ready = conn.execute(
            "SELECT COUNT(*) FROM stage_receipts WHERE case_id=? AND stage=? AND state=?",
            ("C1", "deploy", "ready"),
        ).fetchone()[0]
        test_rows = conn.execute(
            "SELECT COUNT(*) FROM stage_receipts WHERE case_id=? AND stage=?",
            ("C1", "test"),
        ).fetchone()[0]
        assert deploy_ready == 2
        assert test_rows == 3
    finally:
        conn.close()


def test_oversized_links_are_refused(case_repo):
    links = {f"k{i}": f"v{i}" for i in range(64)}
    with pytest.raises(SdlcCaseValidationError, match="links"):
        create_case(case_repo.root, case_repo.repo_id, "C-links", "R-links", links)


def test_read_case_uses_latest_receipt_per_stage(case_repo):
    last = None
    for i in range(24):
        last = append_stage(
            case_repo.root,
            case_repo.repo_id,
            "C1",
            "plan",
            "ready",
            {"intent": f"rev-{i}", "evidence_refs": ["file:README.md"]},
            f"R-plan-{i}",
        )
    case = read_case(case_repo.root, case_repo.repo_id, "C1")
    assert case["stages"]["plan"]["receipt_sha256"] == last["receipt_sha256"]
    packet = stage_packet(case_repo.root, case_repo.repo_id, "C1", "plan")
    assert packet["receipt_sha256"] == last["receipt_sha256"]
    db = case_repo.root.joinpath(*CASES_DB_REL)
    conn = sqlite3.connect(str(db))
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM stage_receipts WHERE case_id=? AND stage=?",
                ("C1", "plan"),
            ).fetchone()[0]
            == 24
        )
    finally:
        conn.close()


def test_plan_change_refuses_test_until_predecessors_refresh(case_repo):
    evidence = {"evidence_refs": ["file:README.md"]}
    for stage in ("plan", "design", "build"):
        append_stage(
            case_repo.root,
            case_repo.repo_id,
            "C1",
            stage,
            "ready",
            evidence,
            "R-" + stage,
        )
    append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "plan",
        "ready",
        {"intent": "changed", "evidence_refs": ["file:README.md"]},
        "R-plan-2",
    )
    design_packet = stage_packet(case_repo.root, case_repo.repo_id, "C1", "design")
    build_packet = stage_packet(case_repo.root, case_repo.repo_id, "C1", "build")
    assert design_packet["state"] == "unknown"
    assert build_packet["state"] == "unknown"
    with pytest.raises(SdlcCaseValidationError, match="predecessor"):
        append_stage(
            case_repo.root,
            case_repo.repo_id,
            "C1",
            "test",
            "ready",
            evidence,
            "R-test",
        )
    test_packet = stage_packet(case_repo.root, case_repo.repo_id, "C1", "test")
    assert test_packet["state"] != "ready"
    append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "design",
        "ready",
        evidence,
        "R-design-2",
    )
    with pytest.raises(SdlcCaseValidationError, match="predecessor"):
        append_stage(
            case_repo.root,
            case_repo.repo_id,
            "C1",
            "test",
            "ready",
            evidence,
            "R-test",
        )
    append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "build",
        "ready",
        evidence,
        "R-build-2",
    )
    ready_test = append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "test",
        "ready",
        evidence,
        "R-test",
    )
    refreshed = stage_packet(case_repo.root, case_repo.repo_id, "C1", "test")
    assert refreshed["state"] == "ready"
    assert refreshed["receipt_sha256"] == ready_test["receipt_sha256"]
    replay = append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "test",
        "ready",
        evidence,
        "R-test",
    )
    assert replay["idempotent"] is True
    assert replay["receipt_sha256"] == ready_test["receipt_sha256"]
    case = read_case(case_repo.root, case_repo.repo_id, "C1")
    assert case["stages"]["test"]["state"] == "ready"
    assert "file:README.md" in case["stages"]["test"]["source_refs"]


def test_blocked_retest_redeploy_requires_fresh_predecessors(case_repo):
    evidence = {"evidence_refs": ["file:README.md"]}
    for stage in ("plan", "design", "build", "test", "deploy"):
        append_stage(
            case_repo.root,
            case_repo.repo_id,
            "C1",
            stage,
            "ready",
            evidence,
            "R-" + stage,
        )
    old_deploy = stage_packet(case_repo.root, case_repo.repo_id, "C1", "deploy")
    old_sha = old_deploy["receipt_sha256"]
    append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "test",
        "blocked",
        {"reason": "failed", "evidence_refs": ["file:README.md"]},
        "R-test-blocked",
    )
    assert stage_packet(case_repo.root, case_repo.repo_id, "C1", "deploy")["state"] != "ready"
    append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "plan",
        "ready",
        {"intent": "changed", "evidence_refs": ["file:README.md"]},
        "R-plan-2",
    )
    assert stage_packet(case_repo.root, case_repo.repo_id, "C1", "design")["state"] == "unknown"
    assert stage_packet(case_repo.root, case_repo.repo_id, "C1", "build")["state"] == "unknown"
    with pytest.raises(SdlcCaseValidationError, match="predecessor"):
        append_stage(
            case_repo.root,
            case_repo.repo_id,
            "C1",
            "test",
            "ready",
            evidence,
            "R-test-2",
        )
    with pytest.raises(SdlcCaseValidationError, match="predecessor"):
        append_stage(
            case_repo.root,
            case_repo.repo_id,
            "C1",
            "deploy",
            "ready",
            evidence,
            "R-deploy-2",
        )
    append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "design",
        "ready",
        evidence,
        "R-design-2",
    )
    append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "build",
        "ready",
        evidence,
        "R-build-2",
    )
    append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "test",
        "ready",
        evidence,
        "R-test-2",
    )
    deploy_packet = stage_packet(case_repo.root, case_repo.repo_id, "C1", "deploy")
    assert deploy_packet["state"] == "unknown"
    assert deploy_packet["reason"] == "stale_predecessor"
    assert deploy_packet["receipt_sha256"] == old_sha
    new_deploy = append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "deploy",
        "ready",
        evidence,
        "R-deploy-2",
    )
    refreshed = stage_packet(case_repo.root, case_repo.repo_id, "C1", "deploy")
    assert refreshed["state"] == "ready"
    assert refreshed["receipt_sha256"] == new_deploy["receipt_sha256"]
    assert refreshed["receipt_sha256"] != old_sha
    case = read_case(case_repo.root, case_repo.repo_id, "C1")
    assert case["stages"]["test"]["state"] == "ready"
    assert case["stages"]["deploy"]["state"] == "ready"
    assert "file:README.md" in case["stages"]["deploy"]["source_refs"]
