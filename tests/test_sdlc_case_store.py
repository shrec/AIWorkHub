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
    STAGES,
    SdlcCaseConflict,
    SdlcCaseValidationError,
    SdlcStageEvidenceRefusal,
    append_stage,
    case_for_task,
    create_case,
    read_case,
    stage_packet,
)
from aiworkhub import task_store

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
    "acceptance_criteria": ["the gap stays closed"],
    "constraints": [],
    "affected_contracts": ["sdlc_case_store"],
    "alternatives": [],
}
BUILD_POINTERS = {"task_id": "T-CASE", "request_id": "req-T-CASE", "claim_epoch": 1}


def _insert_contract_task(root, task_id):
    """Seed a claimed canonical card with a falsifiable contract and no candidate."""
    card = {
        "task_id": task_id,
        "runner": "worker",
        "topic": "sdlc",
        "objective": "Close the observed gap.",
        "acceptance": ["the gap stays closed"],
        "validation": ["python3 -m pytest -q"],
        "allowed_writes": ["src/gap.py"],
        "claim_epoch": 1,
    }
    conn = sqlite3.connect(str(task_store.canonical_db_path(root)))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, status, worker_status, objective, "
            "card_json, created_at, updated_at, claimed_by) "
            "VALUES (?, 'worker', 'sdlc', 'processing', 'claimed', ?, ?, ?, ?, 'worker')",
            (
                task_id,
                card["objective"],
                json.dumps(card),
                "2026-09-20T00:00:00+00:00",
                "2026-09-20T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def case_repo(tmp_path):
    bootstrap_repository(tmp_path, repo_name="sdlc-case-test")
    readiness = task_store.storage_readiness(tmp_path)
    if not readiness.ready:
        task_store.initialize_repository(tmp_path)
        readiness = task_store.storage_readiness(tmp_path)
    assert readiness.ready
    _insert_contract_task(tmp_path, "T-CASE")
    create_case(tmp_path, readiness.repo_id, "C1", "R-create", {"task_id": "T-CASE"})
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
    assert len(first["evidence_sha256"]) == 64
    assert first["idempotent"] is False


def test_same_request_replay_is_idempotent(case_repo):
    first = append_stage(
        case_repo.root, case_repo.repo_id, "C1", "plan", "ready", PLAN_PAYLOAD, "R1"
    )
    second = append_stage(
        case_repo.root, case_repo.repo_id, "C1", "plan", "ready", PLAN_PAYLOAD, "R1"
    )
    assert second["receipt_sha256"] == first["receipt_sha256"]
    assert second["evidence_sha256"] == first["evidence_sha256"]
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
    first = create_case(
        case_repo.root, case_repo.repo_id, "C1", "R-create", {"task_id": "T-CASE"}
    )
    assert first["idempotent"] is True
    with pytest.raises(SdlcCaseConflict):
        create_case(case_repo.root, case_repo.repo_id, "C1", "R-create", {})


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


def test_deploy_requires_every_predecessor(case_repo):
    append_stage(case_repo.root, case_repo.repo_id, "C1", "plan", "ready", PLAN_PAYLOAD, "R-plan")
    append_stage(
        case_repo.root, case_repo.repo_id, "C1", "design", "ready", DESIGN_PAYLOAD, "R-design"
    )
    with pytest.raises(SdlcCaseValidationError, match="missing ready predecessor: build"):
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
    append_stage(case_repo.root, case_repo.repo_id, "C1", "plan", "ready", PLAN_PAYLOAD, "R-plan")
    append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "design",
        "unknown",
        {"evidence_refs": ["file:README.md"]},
        "R-design",
    )
    with pytest.raises(SdlcCaseValidationError, match=r"predecessor: design \(unknown\)"):
        append_stage(
            case_repo.root, case_repo.repo_id, "C1", "build", "ready", BUILD_POINTERS, "R-build"
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
    assert packet["reason"] == "no_stage_receipt"
    assert not packet.get("receipt_sha256")
    missing = stage_packet(case_repo.root, case_repo.repo_id, "C-missing", "plan")
    assert (missing["state"], missing["reason"]) == ("unknown", "case_not_found")


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
        f"payload = {PLAN_PAYLOAD!r}\n"
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


def _count_rows(case_repo, stage, state=None):
    conn = sqlite3.connect(str(case_repo.root.joinpath(*CASES_DB_REL)))
    try:
        if state is None:
            return conn.execute(
                "SELECT COUNT(*) FROM stage_receipts WHERE case_id=? AND stage=?",
                ("C1", stage),
            ).fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM stage_receipts WHERE case_id=? AND stage=? AND state=?",
            ("C1", stage, state),
        ).fetchone()[0]
    finally:
        conn.close()


def test_downstream_ready_stales_after_predecessor_regression(case_repo):
    for stage, payload in (("plan", PLAN_PAYLOAD), ("design", DESIGN_PAYLOAD)):
        append_stage(case_repo.root, case_repo.repo_id, "C1", stage, "ready", payload, "R-" + stage)
    append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "plan",
        "blocked",
        {"reason": "failed", "evidence_refs": ["file:README.md"]},
        "R-plan-blocked",
    )
    plan_packet = stage_packet(case_repo.root, case_repo.repo_id, "C1", "plan")
    assert plan_packet["state"] == "blocked"
    design_packet = stage_packet(case_repo.root, case_repo.repo_id, "C1", "design")
    assert design_packet["state"] == "unknown"
    assert design_packet["recorded_state"] == "ready"
    assert design_packet["reason"] == "stale_predecessor"
    assert design_packet["stale_predecessor"] == "plan"
    case = read_case(case_repo.root, case_repo.repo_id, "C1")
    assert case["stages"]["plan"]["state"] == "blocked"
    assert case["stages"]["design"]["reason"] == "stale_predecessor"
    assert _count_rows(case_repo, "design", "ready") == 1
    assert _count_rows(case_repo, "plan") == 2


def test_new_ready_predecessor_does_not_revive_old_design(case_repo):
    for stage, payload in (("plan", PLAN_PAYLOAD), ("design", DESIGN_PAYLOAD)):
        append_stage(case_repo.root, case_repo.repo_id, "C1", stage, "ready", payload, "R-" + stage)
    old_sha = stage_packet(case_repo.root, case_repo.repo_id, "C1", "design")["receipt_sha256"]
    append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "plan",
        "blocked",
        {"reason": "owner withdrew", "evidence_refs": ["file:README.md"]},
        "R-plan-blocked",
    )
    append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "plan",
        "ready",
        {**PLAN_PAYLOAD, "risk": "re-approved"},
        "R-plan-ready-2",
    )
    design_packet = stage_packet(case_repo.root, case_repo.repo_id, "C1", "design")
    assert design_packet["state"] == "unknown"
    assert design_packet["reason"] == "stale_predecessor"
    assert design_packet["stale_predecessor"] == "plan"
    assert design_packet["receipt_sha256"] == old_sha
    case = read_case(case_repo.root, case_repo.repo_id, "C1")
    assert case["stages"]["plan"]["state"] == "ready"
    assert case["stages"]["design"]["state"] == "unknown"
    new_design = append_stage(
        case_repo.root, case_repo.repo_id, "C1", "design", "ready", DESIGN_PAYLOAD, "R-design-2"
    )
    refreshed = stage_packet(case_repo.root, case_repo.repo_id, "C1", "design")
    assert refreshed["state"] == "ready"
    assert refreshed["receipt_sha256"] == new_design["receipt_sha256"]
    assert refreshed["receipt_sha256"] != old_sha
    assert _count_rows(case_repo, "design", "ready") == 2
    assert _count_rows(case_repo, "plan") == 3


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
            {**PLAN_PAYLOAD, "intent": f"rev-{i}"},
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


def test_plan_change_refuses_build_until_design_refreshes(case_repo):
    for stage, payload in (("plan", PLAN_PAYLOAD), ("design", DESIGN_PAYLOAD)):
        append_stage(case_repo.root, case_repo.repo_id, "C1", stage, "ready", payload, "R-" + stage)
    append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "plan",
        "ready",
        {**PLAN_PAYLOAD, "intent": "changed"},
        "R-plan-2",
    )
    design_packet = stage_packet(case_repo.root, case_repo.repo_id, "C1", "design")
    assert design_packet["state"] == "unknown"
    assert design_packet["reason"] == "stale_predecessor"
    with pytest.raises(
        SdlcCaseValidationError, match=r"predecessor: design \(stale_predecessor\)"
    ):
        append_stage(
            case_repo.root, case_repo.repo_id, "C1", "build", "ready", BUILD_POINTERS, "R-build"
        )
    append_stage(
        case_repo.root, case_repo.repo_id, "C1", "design", "ready", DESIGN_PAYLOAD, "R-design-2"
    )
    assert stage_packet(case_repo.root, case_repo.repo_id, "C1", "design")["state"] == "ready"
    # Fresh predecessors are necessary, never sufficient: no candidate is sealed.
    with pytest.raises(SdlcStageEvidenceRefusal) as refused:
        append_stage(
            case_repo.root, case_repo.repo_id, "C1", "build", "ready", BUILD_POINTERS, "R-build"
        )
    assert refused.value.decision.code == "candidate_not_sealed:none"
    replay = append_stage(
        case_repo.root, case_repo.repo_id, "C1", "design", "ready", DESIGN_PAYLOAD, "R-design-2"
    )
    assert replay["idempotent"] is True
    case = read_case(case_repo.root, case_repo.repo_id, "C1")
    assert case["stages"]["build"]["state"] == "unknown"
    assert case["cycle"]["blocking_stage"] == "build"


def test_blocked_design_requires_a_fresh_ready_design(case_repo):
    append_stage(case_repo.root, case_repo.repo_id, "C1", "plan", "ready", PLAN_PAYLOAD, "R-plan")
    append_stage(
        case_repo.root, case_repo.repo_id, "C1", "design", "ready", DESIGN_PAYLOAD, "R-design"
    )
    old_design = stage_packet(case_repo.root, case_repo.repo_id, "C1", "design")
    append_stage(
        case_repo.root,
        case_repo.repo_id,
        "C1",
        "design",
        "blocked",
        {"reason": "review found a gap", "evidence_refs": ["file:README.md"]},
        "R-design-blocked",
    )
    assert stage_packet(case_repo.root, case_repo.repo_id, "C1", "design")["state"] == "blocked"
    with pytest.raises(SdlcCaseValidationError, match=r"predecessor: design \(blocked\)"):
        append_stage(
            case_repo.root, case_repo.repo_id, "C1", "build", "ready", BUILD_POINTERS, "R-build"
        )
    fresh = append_stage(
        case_repo.root, case_repo.repo_id, "C1", "design", "ready", DESIGN_PAYLOAD, "R-design-2"
    )
    refreshed = stage_packet(case_repo.root, case_repo.repo_id, "C1", "design")
    assert refreshed["state"] == "ready"
    assert refreshed["receipt_sha256"] == fresh["receipt_sha256"]
    assert refreshed["receipt_sha256"] != old_design["receipt_sha256"]
    # The proof is drawn from the unchanged canonical contract, not the request.
    assert refreshed["evidence_sha256"] == old_design["evidence_sha256"]


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


def test_pre_gate_ready_receipts_survive_migration_but_prove_nothing(bare_repo):
    _insert_contract_task(bare_repo.root, "T-OLD")
    create_case(bare_repo.root, bare_repo.repo_id, "C-OLD", "R-old", {"task_id": "T-OLD"})
    db = bare_repo.root.joinpath(*CASES_DB_REL)
    conn = sqlite3.connect(str(db))
    try:
        # Rebuild the receipts table exactly as the pre-gate store created it,
        # holding the six unproven ready rows the old gate accepted.
        conn.execute("DROP TABLE stage_receipts")
        conn.execute(
            "CREATE TABLE stage_receipts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,"
            "repo_id TEXT NOT NULL, request_id TEXT NOT NULL, stage TEXT NOT NULL,"
            "state TEXT NOT NULL, payload_json TEXT NOT NULL,"
            "receipt_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,"
            "UNIQUE(case_id, request_id),"
            "FOREIGN KEY(case_id) REFERENCES cases(case_id))"
        )
        for stage in STAGES:
            conn.execute(
                "INSERT INTO stage_receipts (case_id, repo_id, request_id, stage, state,"
                " payload_json, receipt_sha256, created_at) VALUES (?, ?, ?, ?, 'ready', '{}', ?, ?)",
                ("C-OLD", bare_repo.repo_id, "R-" + stage, stage, "0" * 64,
                 "2026-09-20T00:00:00+00:00"),
            )
        conn.commit()
    finally:
        conn.close()

    before = read_case(bare_repo.root, bare_repo.repo_id, "C-OLD")
    assert {packet["reason"] for packet in before["stages"].values()} == {"legacy_unverified"}

    # Creating an unrelated case migrates the receipts schema in place.
    create_case(bare_repo.root, bare_repo.repo_id, "C-NEW", "R-new", {})
    conn = sqlite3.connect(str(db))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(stage_receipts)")}
        assert {"evidence_json", "evidence_sha256"} <= columns
        unproven = conn.execute(
            "SELECT COUNT(*) FROM stage_receipts WHERE evidence_json IS NULL"
        ).fetchone()[0]
        assert unproven == len(STAGES)
    finally:
        conn.close()

    after = read_case(bare_repo.root, bare_repo.repo_id, "C-OLD")
    for stage in STAGES:
        packet = after["stages"][stage]
        assert packet["state"] == "unknown"
        assert packet["recorded_state"] == "ready"
        assert packet["reason"] == "legacy_unverified"
        assert packet["receipt_sha256"] == "0" * 64
    assert after["cycle"]["state"] == "incomplete"
