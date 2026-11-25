"""NF-2026-02442: a reject lesson derives its own canonical evidence reference.

``resolve_short_form`` used to derive ``evidence_ids`` only for an accepted
decision, so a rejected short-form learning commit reached AI Memory only with a
hand-supplied ``extra_evidence_ids``. A rejection now derives a canonical
``file:`` id from the sealed rework-delta artifact ``reject_review`` retained for
that exact request, verified by ``rework_delta_artifact.has_verified_rework_delta``;
the evidence requirement stays mandatory.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from _taskdb_compat import upsert_card
from aiworkhub import (
    core,
    evidence_levels,
    feature_settings,
    learning_commit_store,
    manager_ai_tools,
    rework_delta_artifact,
    task_store,
    worker_workspace,
)

SESSION_ID = "019f5097-6dbe-7172-870a-945afc5f3bfa"
TASK_ID = "TASK-NF02442"
REQUEST_ID = "0123456789abcdef0123456789abcdef"
OTHER_REQUEST_ID = "fedcba9876543210fedcba9876543210"
CLAIM_EPOCH = 1
CHANGED_PATH = "src/aiworkhub/skill_miner.py"


@pytest.fixture(autouse=True)
def _drain_learning_mining(monkeypatch):
    yield
    assert manager_ai_tools.wait_for_learning_mining(timeout=60)["idle"]


def _manager_route(root: Path) -> dict:
    return {
        "ok": True,
        "role": "manager",
        "provider": "codex",
        "repo": str(root),
        "manager_route": {
            "provider": "codex",
            "session_id": SESSION_ID,
            "thread_id": SESSION_ID,
        },
    }


def _setup_repo(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    feature_settings.update(
        root, changes={"context_graph": True}, expected_revision=0
    )
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(root))
    monkeypatch.setattr(core, "writes_allowed", lambda: True)
    monkeypatch.setattr(core, "repo_root", lambda: root)
    monkeypatch.setenv(
        worker_workspace.RUNTIME_ROOT_ENV, str(root / ".aiworkhub" / "runtime")
    )
    # The MCP server does not run with the repository as its cwd.
    monkeypatch.chdir(tmp_path)
    return root


def _seal(root: Path, artifact_dir: Path | None = None) -> dict[str, str]:
    """A real sealed rework-delta artifact, exactly as ``reject_review`` pins."""
    return rework_delta_artifact.seal_rework_delta_artifact(
        root,
        TASK_ID,
        REQUEST_ID,
        CLAIM_EPOCH,
        [(CHANGED_PATH, b"retained = 'reject evidence'\n")],
        artifact_dir or root / ".aiworkhub" / "runtime" / "rework_deltas",
    )


def _store_card(root: Path, card: dict) -> None:
    con = sqlite3.connect(str(task_store.canonical_db_path(root)))
    con.row_factory = sqlite3.Row
    try:
        upsert_card(con, card)
    finally:
        con.close()


def _rejected_card(
    root: Path,
    *,
    sealed: dict[str, str] | None,
    pinned_digest: str | None = None,
    predecessor_request_id: str = REQUEST_ID,
) -> None:
    """Store a card carrying the exact ``core.reject_review`` predecessor shape."""
    predecessor: dict = {
        "schema_id": "aiworkhub.rework_predecessor.v1",
        "request_id": predecessor_request_id,
        "task_id": TASK_ID,
        "claim_epoch": CLAIM_EPOCH,
        "changed_path_hashes": {CHANGED_PATH: "a" * 64},
    }
    if sealed is not None:
        digest = pinned_digest or sealed["digest"]
        pred_rework_delta = {
            "schema_id": "aiworkhub.rework_delta_descriptor.v1",
            "sealed": True,
            "authority_repo": str(root.resolve()),
            "task_id": TASK_ID,
            "request_id": predecessor_request_id,
            "claim_epoch": CLAIM_EPOCH,
            "artifact_path": sealed["path"],
            "artifact_sha256": sealed["digest"],
        }
        predecessor.update(
            {
                "delta_artifact": {"path": sealed["path"], "digest": digest},
                "rework_delta": pred_rework_delta,
            }
        )
    _store_card(
        root,
        {
            "task_id": TASK_ID,
            "runner": "worker",
            "topic": "learning",
            "mode": "edit",
            "status": "pending",
            "worker_status": "pending",
            "rework_predecessor": predecessor,
            "review_feedback": {
                "schema_id": "aiworkhub.rework_feedback_delta.v1",
                "instruction": "consult the fact on the object",
                "predecessor_request_id": REQUEST_ID,
                "predecessor_changed_paths": [CHANGED_PATH],
            },
        },
    )


def _expected_reference(sealed: dict[str, str]) -> str:
    return f"file:.aiworkhub/runtime/rework_deltas/{sealed['digest']}.json"


def test_rejection_derives_a_canonical_file_reference_from_outside_the_repo_cwd(
    tmp_path, monkeypatch
):
    root = _setup_repo(tmp_path, monkeypatch)
    assert Path.cwd().resolve() != root.resolve()
    sealed = _seal(root)
    _rejected_card(root, sealed=sealed)

    resolved = learning_commit_store.resolve_short_form(
        root, task_id=TASK_ID, request_id=REQUEST_ID
    )

    assert resolved["outcome"] == "rejected"
    assert resolved["evidence_ids"] == [_expected_reference(sealed)]
    assert "evidence_reference_refusal" not in resolved


def test_rejected_short_form_reaches_ai_memory_without_extra_evidence(
    tmp_path, monkeypatch
):
    from aiworkhub import server

    root = _setup_repo(tmp_path, monkeypatch)
    _rejected_card(root, sealed=_seal(root))

    result = server.aiworkhub_manager_learning_commit(
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        lesson_candidate="a reject lesson must cite the evidence it retained",
        promote_ai_memory=True,
    )

    assert result["ok"] is True, result
    assert result["outcome"] == "rejected"
    assert result["projections"]["ai_memory"]["state"] == "applied"


@pytest.mark.parametrize(
    "case",
    ["no_predecessor", "other_request", "no_artifact", "artifact_gone"],
)
def test_missing_retained_evidence_still_refuses_with_a_named_reason(
    tmp_path, monkeypatch, case
):
    from aiworkhub import server

    root = _setup_repo(tmp_path, monkeypatch)
    if case == "no_predecessor":
        _store_card(
            root,
            {
                "task_id": TASK_ID,
                "runner": "worker",
                "topic": "learning",
                "mode": "edit",
                "status": "pending",
                "worker_status": "pending",
                "review_feedback": {
                    "schema_id": "aiworkhub.rework_feedback_delta.v1",
                    "instruction": "x",
                    "predecessor_request_id": REQUEST_ID,
                    "predecessor_changed_paths": ["src/aiworkhub/a.py"],
                },
            },
        )
    elif case == "other_request":
        _rejected_card(
            root, sealed=_seal(root), predecessor_request_id=OTHER_REQUEST_ID
        )
    elif case == "no_artifact":
        _rejected_card(root, sealed=None)
    else:
        sealed = _seal(root)
        Path(sealed["path"]).unlink()
        _rejected_card(root, sealed=sealed)

    resolved = learning_commit_store.resolve_short_form(
        root, task_id=TASK_ID, request_id=REQUEST_ID
    )
    assert resolved["evidence_ids"] == []
    assert resolved["evidence_reference_refusal"].startswith(
        "learning_commit_rejection_evidence_missing"
    )

    result = server.aiworkhub_manager_learning_commit(
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        lesson_candidate="no retained evidence, no promotion",
        promote_ai_memory=True,
    )
    assert result["ok"] is False
    assert "evidence_id" in str(result["error"])
    assert result["resolved_fields"]["evidence_reference_refusal"].startswith(
        "learning_commit_rejection_evidence_missing"
    )


def test_a_hand_supplied_id_still_satisfies_the_gate_when_nothing_is_retained(
    tmp_path, monkeypatch
):
    from aiworkhub import server

    root = _setup_repo(tmp_path, monkeypatch)
    _rejected_card(root, sealed=None)

    result = server.aiworkhub_manager_learning_commit(
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        lesson_candidate="a supplied id keeps working",
        promote_ai_memory=True,
        extra_evidence_ids=["file:tests/test_reject_lesson_evidence_nf02442.py"],
    )

    assert result["ok"] is True, result


def test_a_pinned_digest_that_does_not_match_the_sealed_artifact_is_not_cited(
    tmp_path, monkeypatch
):
    root = _setup_repo(tmp_path, monkeypatch)
    _rejected_card(root, sealed=_seal(root), pinned_digest="0" * 64)

    resolved = learning_commit_store.resolve_short_form(
        root, task_id=TASK_ID, request_id=REQUEST_ID
    )

    assert resolved["evidence_ids"] == []
    assert resolved["evidence_reference_refusal"].startswith(
        "learning_commit_rejection_evidence_tampered"
    )


def test_an_artifact_whose_bytes_changed_is_not_cited(tmp_path, monkeypatch):
    root = _setup_repo(tmp_path, monkeypatch)
    sealed = _seal(root)
    Path(sealed["path"]).write_bytes(b'{"forged": true}')
    _rejected_card(root, sealed=sealed)

    resolved = learning_commit_store.resolve_short_form(
        root, task_id=TASK_ID, request_id=REQUEST_ID
    )

    assert resolved["evidence_ids"] == []
    assert resolved["evidence_reference_refusal"].startswith(
        "learning_commit_rejection_evidence_tampered"
    )


def test_an_artifact_outside_the_repository_is_not_cited(tmp_path, monkeypatch):
    root = _setup_repo(tmp_path, monkeypatch)
    sealed = _seal(root, artifact_dir=tmp_path / "elsewhere")
    _rejected_card(root, sealed=sealed)

    resolved = learning_commit_store.resolve_short_form(
        root, task_id=TASK_ID, request_id=REQUEST_ID
    )

    assert resolved["evidence_ids"] == []
    assert resolved["evidence_reference_refusal"] == (
        "learning_commit_rejection_evidence_outside_repository"
    )


def test_the_accepted_path_is_unchanged(tmp_path, monkeypatch):
    root = _setup_repo(tmp_path, monkeypatch)
    reference = (
        ".aiworkhub/runtime/process_logs/attempt-artifacts/"
        f"{REQUEST_ID}/manifest.json"
    )
    record = evidence_levels.EvidenceRecord(
        evidence_level=evidence_levels.EvidenceLevel.FIXED_AND_VERIFIED,
        severity="NONE",
        confidence="HIGH",
        reference=f"file:{reference}",
        verified_by="codex",
        message="manager verified exact accepted outcome",
    ).to_dict()
    _store_card(
        root,
        {
            "task_id": TASK_ID,
            "runner": "worker",
            "topic": "learning",
            "mode": "edit",
            "status": "finished",
            "worker_status": "done",
            "accepted_request_id": REQUEST_ID,
            "accept_evidence": {
                "acceptance_evidence_record": record,
                "promoted_paths": [CHANGED_PATH],
            },
        },
    )

    resolved = learning_commit_store.resolve_short_form(
        root, task_id=TASK_ID, request_id=REQUEST_ID
    )

    assert resolved["outcome"] == "accepted"
    assert resolved["evidence_ids"] == [f"file:{reference}"]
    assert "evidence_reference_refusal" not in resolved
