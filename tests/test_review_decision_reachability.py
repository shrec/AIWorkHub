"""Four reachability gates over the accept/reject decision surface.

Each thing asserted here was already built and already tested -- and could not
be reached by anyone. These tests assert the *reach*, not the behaviour the
underlying modules already prove:

  * ``completion_inbox.review_packet`` has an MCP surface, and that surface
    supplies the ``accept_preview`` fold the packet module may not compute
    itself (it holds no launch authority, by its own asserted invariants).
  * a NeedFix can be drafted from the review evidence that justifies it,
    mechanically, with the description left to the manager.
  * accept and reject each append ONE session document for the decision, so a
    successor's mandated session query is no longer empty by construction.
"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import (  # noqa: E402
    completion_inbox,
    core,
    learning_commit_store,
    needfix_store,
    process_launcher_accept_review,
    server,
    storage_registry,
    task_store,
)

SESSION_ID = "019f5097-6dbe-7172-870a-945afc5f3bfa"
NOW = "2026-07-20T00:00:00+00:00"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _review_card(
    task_id: str = "T-DRAFT",
    request_id: str = "R-DRAFT",
    *,
    with_manifest: bool = True,
    manager_ready: bool = False,
) -> dict:
    """A review_ready card carrying exactly the evidence a decision is taken on.

    ``with_manifest`` False drops ``attempt_artifact_manifest``: its presence is
    what makes ``reject_review`` bind and revalidate the predecessor workspace,
    which a fixture card has never had. The drafter reads the manifest only for
    ``evidence_refs``, which the drafter tests cover directly.
    """
    card = {
        "task_id": task_id,
        "runner": "claude_coding",
        "topic": "coding",
        "status": "review",
        "terminal_review": {
            "substatus": "review_ready",
            "evidence": {
                "error": "quality_gate_failed:ruff",
                "request_identity": {"request_id": request_id, "task_id": task_id},
                "changed_paths": ["src/aiworkhub/service.py", "tests/test_service.py"],
                "changed_path_hashes": {"src/aiworkhub/service.py": "a" * 64},
                "attempt_artifact_manifest": {
                    "manifest_path": "/repo/.aiworkhub/attempt-artifacts/x/manifest.json",
                },
                "validation": [
                    {"declared_command": "pytest tests/test_service.py",
                     "returncode": 1, "stdout_tail": "1 failed"},
                ],
                "quality_gate": {
                    "passed": False,
                    "blocking_checks": ["ruff"],
                    "checks": [
                        {"check_id": "ruff", "kind": "lint", "status": "failed",
                         "command": "ruff check src", "summary": "1 error"},
                        {"check_id": "mypy", "kind": "typecheck", "status": "passed",
                         "command": "mypy src", "summary": "ok"},
                    ],
                    "quality_verdict": {
                        "reviewer_reports": [
                            {
                                "lens": "correctness",
                                "findings": [
                                    {"id": "F-1", "severity": "high",
                                     "disposition": "defect", "category": "general",
                                     "summary": "off by one",
                                     "evidence": "src/aiworkhub/service.py:42 loop bound"},
                                    {"id": "F-2", "severity": "low",
                                     "disposition": "observation",
                                     "category": "excess_scope",
                                     "summary": "extra helper",
                                     "evidence": "src/aiworkhub/service.py:80"},
                                ],
                            },
                        ],
                    },
                },
            },
        },
    }
    if not with_manifest:
        card["terminal_review"]["evidence"].pop("attempt_artifact_manifest")
    if manager_ready:
        card["claim_epoch"] = 1
        card["manager_ready_receipt"] = {
            "manager_ready": {
                "schema_id": "aiworkhub.manager_ready_receipt.v1",
                "target_task_id": task_id,
                "target_request_id": request_id,
                "claim_epoch": "1",
                "reviews": [],
            }
        }
    return card


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    monkeypatch.setenv("AIWORKHUB_REPO", str(root))
    monkeypatch.setenv("AIWORKHUB_ALLOW_WRITES", "1")
    token = tmp_path / "coordinator.token"
    token.write_text("coord-token\n", encoding="utf-8")
    os.chmod(token, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN_FILE", str(token))
    monkeypatch.setenv("BITNN_TASKCTL_COORDINATOR_TOKEN", "coord-token")
    return root


@pytest.fixture
def manager_route(monkeypatch):
    """A verified Codex manager route, the shape ``_decision_actor`` reads."""
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(
        core,
        "_codex_manager_identity",
        lambda: {"provider": "codex", "session_id": SESSION_ID, "thread_id": SESSION_ID},
    )


def _insert_review_card(root: Path, card: dict) -> None:
    readiness = task_store.storage_readiness(root)
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id,runner,topic,mode,status,worker_status,priority,"
            "objective,card_json,created_at,updated_at,claimed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (card["task_id"], card["runner"], card["topic"], "solo", "review", "review",
             "normal", "obj", json.dumps(card), NOW, NOW, card["runner"]),
        )
        conn.commit()
    finally:
        conn.close()


def _session_documents(root: Path) -> list[dict]:
    registry = storage_registry.load_storage_registry(root)
    path = storage_registry.resolve_database_path(registry, "transcript")
    if not path.is_file():
        return []
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM documents ORDER BY rowid").fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# GAP 1 -- review_packet has an MCP surface that supplies accept_preview
# ---------------------------------------------------------------------------


def test_review_packet_is_a_registered_mcp_tool():
    """Built, tested, and until now uncallable: no manager could reach it."""
    names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert "aiworkhub_review_packet" in names


def test_review_packet_tool_computes_and_injects_the_accept_preview(monkeypatch):
    """The packet module may not call ``accept_preview``; the tool must.

    ``completion_inbox`` asserts it holds no launch authority, which is why the
    fold is an injected dict there. If the wrapper failed to compute it the
    packet would silently report ``evaluated: False`` -- a green-looking answer
    for a question nobody asked.
    """
    calls: list[tuple[str, str]] = []

    class _Manager:
        def status(self, request_id):  # pragma: no cover -- task_id is supplied
            raise AssertionError("status must not be called when task_id is given")

        def accept_preview(self, request_id, task_id):
            calls.append((request_id, task_id))
            return {
                "evaluated": True,
                "blocked": True,
                "blockers": [{"kind": "required_reviewer_missing", "error": "x"}],
                "reviewer_request_ids": ["rid-1"],
                "reviewer_request_id_source": "bound_children",
            }

    monkeypatch.setattr(server.process_launcher, "default_manager", lambda: _Manager())
    monkeypatch.setattr(
        server.core, "show_task", lambda task_id: _fake_show(task_id)
    )
    packet = server.aiworkhub_review_packet("R-DRAFT", task_id="T-DRAFT")

    assert calls == [("R-DRAFT", "T-DRAFT")]
    assert packet["schema_id"] == completion_inbox.REVIEW_PACKET_SCHEMA_ID
    assert packet["accept_preview"]["evaluated"] is True
    assert packet["accept_preview"]["blocked"] is True
    assert packet["accept_preview"]["blockers"][0]["kind"] == "required_reviewer_missing"
    assert packet["task_id_source"] == "caller"
    assert packet["mutation"] == {
        "queue_mutated": False,
        "write_gate_bypassed": False,
        "write_command_invoked": False,
        "agent_or_process_launched": False,
    }


def _fake_show(task_id: str):
    return core.TaskCtlResult(
        command=["show", task_id],
        returncode=0,
        stdout=json.dumps(_review_card(task_id=task_id, manager_ready=True)),
        stderr="",
    )


def test_review_packet_tool_resolves_a_missing_task_id_from_the_request(monkeypatch):
    """One id in, one packet out: the tool is request-scoped."""

    class _Manager:
        def status(self, request_id):
            return {
                "ok": True,
                "task_id": "T-DRAFT",
                "task_card": _review_card(manager_ready=True),
            }

        def accept_preview(self, request_id, task_id):
            assert task_id == "T-DRAFT"
            return {"evaluated": True, "blocked": False, "blockers": []}

    monkeypatch.setattr(server.process_launcher, "default_manager", lambda: _Manager())
    monkeypatch.setattr(server.core, "show_task", _fake_show)
    packet = server.aiworkhub_review_packet("R-DRAFT")
    assert packet["task_id"] == "T-DRAFT"
    assert packet["task_id_source"] == "request_status"


# ---------------------------------------------------------------------------
# GAP 2 -- a NeedFix can be drafted from the evidence that justifies it
# ---------------------------------------------------------------------------


def test_needfix_draft_is_a_registered_mcp_tool():
    names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert "aiworkhub_manager_needfix_draft" in names


def test_draft_carries_every_field_mechanically_and_no_description():
    draft = needfix_store.draft_from_review_evidence(
        _review_card(), request_id="R-DRAFT"
    )
    assert draft["ok"] is True and draft["readonly"] is True
    assert draft["task_id"] == "T-DRAFT"

    by_source = {row["source"]: row for row in draft["candidates"]}
    assert set(by_source) == {"quality_gate_check", "reviewer_finding"}

    check = by_source["quality_gate_check"]
    assert check["check_id"] == "ruff"
    assert check["title"] == "quality gate check failed: ruff"
    assert check["kind"] == "technical_debt"          # fixed map: lint
    assert check["severity"] == "high"                # it is a blocking check
    assert check["scope_files"] == [
        "src/aiworkhub/service.py", "tests/test_service.py"
    ]
    assert check["evidence"]["returncode"] == 1
    assert check["evidence"]["declared_command"] == "pytest tests/test_service.py"
    assert check["evidence"]["error"] == "quality_gate_failed:ruff"

    finding = by_source["reviewer_finding"]
    assert finding["finding_id"] == "F-1"
    assert finding["title"] == "off by one"
    assert finding["kind"] == "bug"                   # fixed map: general
    assert finding["severity"] == "high"
    assert finding["evidence"]["path"] == "src/aiworkhub/service.py"
    assert finding["evidence"]["line"] == "42"
    assert finding["scope_files"] == ["src/aiworkhub/service.py"]

    for row in draft["candidates"]:
        assert row["description"] == ""
        assert row["provenance"] == {
            "origin": "server_draft", "request_id": "R-DRAFT", "verified": False,
        }
        assert row["evidence_refs"] == [
            "file:/repo/.aiworkhub/attempt-artifacts/x/manifest.json", "R-DRAFT",
        ]
    assert draft["description_owner"] == "manager"


def test_draft_only_takes_defects_unless_a_finding_is_named():
    """An observation is not a defect, and the drafter does not promote one."""
    unfiltered = needfix_store.draft_from_review_evidence(
        _review_card(), request_id="R-DRAFT"
    )
    assert [row.get("finding_id") for row in unfiltered["candidates"]] == [None, "F-1"]

    named = needfix_store.draft_from_review_evidence(
        _review_card(), request_id="R-DRAFT", finding_id="F-2"
    )
    assert [row["finding_id"] for row in named["candidates"]] == ["F-2"]
    assert named["candidates"][0]["kind"] == "refactor"   # fixed map: excess_scope
    assert named["candidates"][0]["severity"] == "low"


def test_draft_narrows_to_one_check_and_names_an_unknown_one():
    one = needfix_store.draft_from_review_evidence(
        _review_card(), request_id="R-DRAFT", check_id="mypy"
    )
    assert [row["check_id"] for row in one["candidates"]] == ["mypy"]
    assert one["candidates"][0]["severity"] == "medium"   # passed, not blocking

    missing = needfix_store.draft_from_review_evidence(
        _review_card(), request_id="R-DRAFT", check_id="nope"
    )
    assert missing["ok"] is False and missing["error"] == "check_id_not_on_card"


def test_draft_never_raises_on_an_unusable_card():
    assert needfix_store.draft_from_review_evidence(
        None, request_id="R"  # type: ignore[arg-type]
    )["error"] == "card_unreadable"
    empty = needfix_store.draft_from_review_evidence({}, request_id="R")
    assert empty["ok"] is True and empty["candidates"] == []
    assert empty["candidates"] == [] and empty["candidate_count"] == 0


def test_a_drafted_candidate_is_filing_ready_for_the_existing_store(repo):
    """Everything but the description passes the store's own validation."""
    draft = needfix_store.draft_from_review_evidence(
        _review_card(), request_id="R-DRAFT"
    )
    candidate = draft["candidates"][0]
    row = needfix_store.add_needfix(
        repo,
        title=candidate["title"],
        description="Manager judgement goes here.",
        kind=candidate["kind"],
        severity=candidate["severity"],
        scope_files=candidate["scope_files"],
        evidence=candidate["evidence"],
        evidence_refs=candidate["evidence_refs"],
        provenance=candidate["provenance"],
    )
    assert row["kind"] == candidate["kind"]
    assert row["scope_files"] == candidate["scope_files"]
    assert row["evidence"]["check_id"] == "ruff"
    # ``add_needfix`` is the manager path, so it stamps verified True over the
    # draft's unverified provenance -- filing is the manager's act, not the
    # drafter's.
    assert row["provenance"]["origin"] == "server_draft"
    assert row["provenance"]["verified"] is True

    captured = needfix_store.capture_proposal(
        repo,
        title=candidate["title"] + " (captured)",
        description="Nothing lost when the reviewer card is archived.",
        kind=candidate["kind"],
        severity=candidate["severity"],
        provenance=candidate["provenance"],
        evidence=candidate["evidence"],
    )
    assert captured["status"] == "captured"
    assert captured["provenance"]["verified"] is False


# ---------------------------------------------------------------------------
# GAP 3 -- one session document per decision
# ---------------------------------------------------------------------------


def test_decision_event_writes_one_session_document(repo, manager_route):
    result = learning_commit_store.record_decision_event(
        repo,
        task_id="T-DEC",
        request_id="R-DEC",
        decision="rejected",
        changed_path_hashes={"src/a.py": "b" * 64},
        review_feedback={"instruction": "rework"},
        failure_category="candidate_code",
    )
    assert result["state"] == "applied", result
    assert result["provenance"] == "manager_rejected_review"
    assert result["idempotency_key"] == "T-DEC:R-DEC:rejected"

    documents = _session_documents(repo)
    assert len(documents) == 1
    payload = json.loads(documents[0]["content"])
    assert payload["schema_id"] == learning_commit_store.DECISION_EVENT_SCHEMA_ID
    assert payload["decision"] == "rejected"
    assert payload["task_id"] == "T-DEC" and payload["request_id"] == "R-DEC"
    assert payload["changed_paths"] == [{"path": "src/a.py", "sha256": "b" * 64}]
    assert payload["failure_category"] == "candidate_code"
    assert len(payload["review_feedback_sha256"]) == 64


def test_decision_event_is_idempotent_per_task_request_decision(repo, manager_route):
    for _ in range(3):
        result = learning_commit_store.record_decision_event(
            repo, task_id="T-DEC", request_id="R-DEC", decision="accepted",
            changed_paths=["src/a.py"],
        )
        assert result["state"] == "applied"
    assert len(_session_documents(repo)) == 1


def test_decision_event_refuses_to_fabricate_a_session_identity(repo, monkeypatch):
    monkeypatch.setattr(core, "_claude_manager_identity", lambda: None)
    monkeypatch.setattr(core, "_codex_manager_identity", lambda: None)
    result = learning_commit_store.record_decision_event(
        repo, task_id="T-DEC", request_id="R-DEC", decision="accepted",
    )
    assert result["state"] == "skipped"
    assert result["reason"] == "manager_session_identity_unverified"
    assert _session_documents(repo) == []


def test_decision_event_never_raises_when_the_store_is_unusable(tmp_path, manager_route, monkeypatch):
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    result = learning_commit_store.record_decision_event(
        tmp_path / "no-such-repo",
        task_id="T", request_id="R", decision="accepted",
    )
    assert result["state"] == "failed" and result["error"]


def test_reject_review_reply_carries_the_decision_event_and_the_draft(
    repo, manager_route
):
    """The rejection reply is where both become reachable at all.

    ``begin_claim_episode`` clears ``terminal_review`` in this same transition,
    so the failing checks and reviewer findings exist for exactly this long.
    """
    card = _review_card(task_id="T-REJ", request_id="R-REJ", with_manifest=False)
    _insert_review_card(repo, card)
    result = core.reject_review("T-REJ", "ruff is red", to="pending")
    assert result["ok"] is True, result

    event = result["session_decision_event"]
    assert event["state"] == "applied", event
    assert event["provenance"] == "manager_rejected_review"
    documents = _session_documents(repo)
    assert len(documents) == 1
    payload = json.loads(documents[0]["content"])
    assert payload["decision"] == "rejected" and payload["task_id"] == "T-REJ"
    assert payload["failure_category"]

    draft = result["needfix_candidates"]
    assert draft["ok"] is True
    assert {row["source"] for row in draft["candidates"]} == {
        "quality_gate_check", "reviewer_finding"
    }
    assert all(row["description"] == "" for row in draft["candidates"])


# ---------------------------------------------------------------------------
# GAP 3 (accept side) -- the reply assembly is wired, and only once
# ---------------------------------------------------------------------------


def _accept_review_node() -> ast.FunctionDef:
    source = Path(process_launcher_accept_review.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "accept_review":
            return node
    raise AssertionError("accept_review not found")


def _accepted_reply_keys(node: ast.FunctionDef) -> dict[str, str]:
    for stmt in ast.walk(node):
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == "accepted_reply"
            and isinstance(stmt.value, ast.Dict)
        ):
            return {
                key.value: (
                    value.id if isinstance(value, ast.Name) else type(value).__name__
                )
                for key, value in zip(stmt.value.keys, stmt.value.values)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    raise AssertionError("accepted_reply literal not found")


def test_accept_reply_carries_the_draft_and_the_decision_event():
    """One reply definition, so the cleanup-failed path cannot drift.

    ``accept_review`` returns ``accepted_reply`` twice -- once plainly and once
    with a cleanup error spread over it -- so asserting on the single literal
    covers both exits.
    """
    keys = _accepted_reply_keys(_accept_review_node())
    assert keys["needfix_candidates"] == "surviving_findings"
    assert keys["session_decision_event"] == "session_decision_event"


def test_accept_side_calls_each_projection_exactly_once():
    node = _accept_review_node()
    calls = [
        f"{call.func.value.id}.{call.func.attr}"
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
    ]
    assert calls.count("_needfix_store.draft_from_review_evidence") == 1
    assert calls.count("_learning_store.record_decision_event") == 1
