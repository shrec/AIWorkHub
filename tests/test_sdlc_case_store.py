from __future__ import annotations

import hashlib
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
    case_for_task,
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


def _insert_task(root, task_id):
    """Seed a canonical task row directly, since task_store exposes no writer."""
    db = task_store.canonical_db_path(root)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, created_at, updated_at) VALUES (?, ?, ?)",
            (task_id, "2026-09-20T00:00:00+00:00", "2026-09-20T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def bare_repo(tmp_path):
    """A bootstrapped repository with no SDLC cases yet."""
    bootstrap_repository(tmp_path, repo_name="sdlc-bare-test")
    readiness = task_store.storage_readiness(tmp_path)
    if not readiness.ready:
        task_store.initialize_repository(tmp_path)
        readiness = task_store.storage_readiness(tmp_path)
    assert readiness.ready
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


def test_case_for_task_unknown_for_absent_link(case_repo):
    packet = case_for_task(case_repo.root, case_repo.repo_id, "T-none")
    assert packet["state"] == "unknown"
    assert packet["case_id"] is None
    assert packet["task_id"] == "T-none"
    assert packet["links"] == {}
    assert packet["stages"] == {}


def test_case_for_task_resolves_exact_bound_case(bare_repo):
    _insert_task(bare_repo.root, "T1")
    create_case(bare_repo.root, bare_repo.repo_id, "C-bound", "R-bound", {"task_id": "T1"})
    packet = case_for_task(bare_repo.root, bare_repo.repo_id, "T1")
    assert packet["state"] == "bound"
    assert packet["case_id"] == "C-bound"
    assert packet["task_id"] == "T1"
    assert packet["links"] == {"task_id": "T1"}


def test_nonexistent_task_refused_before_store_mutation(bare_repo):
    db = bare_repo.root.joinpath(*CASES_DB_REL)
    assert not db.exists()
    with pytest.raises(SdlcCaseValidationError, match="task"):
        create_case(bare_repo.root, bare_repo.repo_id, "C-new", "R-new", {"task_id": "T-missing"})
    assert not db.exists()


def test_foreign_task_link_refused_across_repositories(tmp_path):
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    bootstrap_repository(repo_a, repo_name="sdlc-foreign-a")
    bootstrap_repository(repo_b, repo_name="sdlc-foreign-b")
    ra = task_store.storage_readiness(repo_a)
    if not ra.ready:
        task_store.initialize_repository(repo_a)
        ra = task_store.storage_readiness(repo_a)
    rb = task_store.storage_readiness(repo_b)
    if not rb.ready:
        task_store.initialize_repository(repo_b)
        rb = task_store.storage_readiness(repo_b)
    assert ra.ready and rb.ready
    _insert_task(repo_a, "T1")
    # T1 exists only in repo_a; binding it while writing repo_b is foreign.
    with pytest.raises(SdlcCaseValidationError, match="task"):
        create_case(repo_b, rb.repo_id, "C-new", "R-new", {"task_id": "T1"})
    assert not repo_b.joinpath(*CASES_DB_REL).exists()


def test_two_case_ids_cannot_bind_same_task(bare_repo):
    _insert_task(bare_repo.root, "T1")
    create_case(bare_repo.root, bare_repo.repo_id, "C-A", "R-A", {"task_id": "T1"})
    with pytest.raises(SdlcCaseConflict, match="task"):
        create_case(bare_repo.root, bare_repo.repo_id, "C-B", "R-B", {"task_id": "T1"})
    packet = case_for_task(bare_repo.root, bare_repo.repo_id, "T1")
    assert packet["case_id"] == "C-A"
    conn = sqlite3.connect(str(bare_repo.root.joinpath(*CASES_DB_REL)))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM cases WHERE task_id=?", ("T1",)
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_task_binding_replay_idempotent_and_conflict_fails(bare_repo):
    _insert_task(bare_repo.root, "T1")
    first = create_case(bare_repo.root, bare_repo.repo_id, "C-A", "R-A", {"task_id": "T1"})
    assert first["idempotent"] is False
    replay = create_case(bare_repo.root, bare_repo.repo_id, "C-A", "R-A", {"task_id": "T1"})
    assert replay["idempotent"] is True
    assert replay["receipt_sha256"] == first["receipt_sha256"]
    with pytest.raises(SdlcCaseConflict):
        create_case(bare_repo.root, bare_repo.repo_id, "C-A", "R-B", {"task_id": "T1"})


def test_legacy_unlinked_case_preserves_digest_and_has_no_receipts(bare_repo):
    repo_id = bare_repo.repo_id
    canonical = {
        "case_id": "C-legacy",
        "links": {},
        "repo_id": repo_id,
        "request_id": "R-legacy",
    }
    legacy_digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    db = bare_repo.root.joinpath(*CASES_DB_REL)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE cases ("
            "case_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL,"
            "request_id TEXT NOT NULL, links_json TEXT NOT NULL,"
            "canonical_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE stage_receipts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,"
            "repo_id TEXT NOT NULL, request_id TEXT NOT NULL, stage TEXT NOT NULL,"
            "state TEXT NOT NULL, payload_json TEXT NOT NULL,"
            "receipt_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,"
            "UNIQUE(case_id, request_id),"
            "FOREIGN KEY(case_id) REFERENCES cases(case_id))"
        )
        conn.execute(
            "INSERT INTO cases (case_id, repo_id, request_id, links_json,"
            " canonical_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("C-legacy", repo_id, "R-legacy", json.dumps({}), legacy_digest,
             "2026-09-20T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    # A pre-migration lookup on the legacy schema still types UNKNOWN.
    pre = case_for_task(bare_repo.root, repo_id, "T-none")
    assert pre["state"] == "unknown"

    # Creating an unrelated case migrates the schema in place.
    create_case(bare_repo.root, repo_id, "C2", "R2", {})

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT canonical_sha256, links_json, task_id FROM cases WHERE case_id='C-legacy'"
        ).fetchone()
        assert row[0] == legacy_digest
        assert json.loads(row[1]) == {}
        assert row[2] is None
    finally:
        conn.close()

    packet = read_case(bare_repo.root, repo_id, "C-legacy")
    assert packet["links"] == {}
    for stage in packet["stages"].values():
        assert stage["state"] == "unknown"
        assert "receipt_sha256" not in stage


def test_concurrent_binding_does_not_duplicate(bare_repo):
    _insert_task(bare_repo.root, "T1")

    def script(case_id):
        return (
            "import json\n"
            "from pathlib import Path\n"
            "from aiworkhub.sdlc_case_store import create_case\n"
            f"root = Path({str(bare_repo.root)!r})\n"
            f"repo_id = {bare_repo.repo_id!r}\n"
            f"case_id = {case_id!r}\n"
            "try:\n"
            "    result = create_case(root, repo_id, case_id, 'R-race', {'task_id': 'T1'})\n"
            "    print(json.dumps({'ok': True, 'idempotent': result['idempotent']}))\n"
            "except Exception as exc:\n"
            "    print(json.dumps({'ok': False, 'error': type(exc).__name__, 'message': str(exc)}))\n"
        )

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script(case_id)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for case_id in ("C-A", "C-B")
    ]
    parsed = []
    for proc in procs:
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, err
        parsed.append(json.loads(out))
    assert sum(1 for row in parsed if row.get("ok")) == 1
    conn = sqlite3.connect(str(bare_repo.root.joinpath(*CASES_DB_REL)))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM cases WHERE task_id=?", ("T1",)
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()
    packet = case_for_task(bare_repo.root, bare_repo.repo_id, "T1")
    assert packet["state"] == "bound"


def test_create_case_initializes_schema_on_existing_schemaless_file(bare_repo):
    _insert_task(bare_repo.root, "T1")
    db = bare_repo.root.joinpath(*CASES_DB_REL)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    created = create_case(
        bare_repo.root, bare_repo.repo_id, "C-schema", "R-schema", {"task_id": "T1"}
    )
    assert created["idempotent"] is False

    packet = case_for_task(bare_repo.root, bare_repo.repo_id, "T1")
    assert packet["state"] == "bound"
    assert packet["case_id"] == "C-schema"

    replay = create_case(
        bare_repo.root, bare_repo.repo_id, "C-schema", "R-schema", {"task_id": "T1"}
    )
    assert replay["idempotent"] is True
    assert replay["receipt_sha256"] == created["receipt_sha256"]
