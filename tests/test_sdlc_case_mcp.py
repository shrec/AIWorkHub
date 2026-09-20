from __future__ import annotations

import asyncio
import inspect
import sqlite3
from types import SimpleNamespace

from aiworkhub import core, server, task_store
from aiworkhub.repository_state import bootstrap_repository
from aiworkhub.sdlc_case_store import CASES_DB_REL, STAGES

PLAN_PAYLOAD = {"intent": "x", "evidence_refs": ["file:README.md"]}
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
    _authorize_manager(monkeypatch)
    created = server.aiworkhub_manager_sdlc_case_create("C1", "R-create", {})
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
    _seed_task(repo.root, "T1")
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
