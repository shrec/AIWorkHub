from __future__ import annotations

import inspect
from types import SimpleNamespace

from aiworkhub import core, server, task_store
from aiworkhub.repository_state import bootstrap_repository
from aiworkhub.sdlc_case_store import CASES_DB_REL

PLAN_PAYLOAD = {"intent": "x", "evidence_refs": ["file:README.md"]}
CASE_TOOLS = (
    "aiworkhub_manager_sdlc_case_create",
    "aiworkhub_manager_sdlc_stage_record",
    "aiworkhub_manager_sdlc_case_get",
    "aiworkhub_manager_sdlc_stage_packet",
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


def case_repo(tmp_path, monkeypatch):
    bootstrap_repository(tmp_path, repo_name="sdlc-case-mcp-test")
    readiness = task_store.storage_readiness(tmp_path)
    if not readiness.ready:
        task_store.initialize_repository(tmp_path)
        readiness = task_store.storage_readiness(tmp_path)
    assert readiness.ready
    monkeypatch.setattr(core, "repo_root", lambda: tmp_path)
    return SimpleNamespace(root=tmp_path, repo_id=readiness.repo_id)


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
