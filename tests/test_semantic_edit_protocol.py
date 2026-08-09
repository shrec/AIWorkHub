from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aiworkhub import semantic_edit
from aiworkhub import process_launcher
from aiworkhub import worker_ai_tools_mcp as worker_tools


def test_prepare_returns_only_fragment_and_truthful_byte_accounting(tmp_path: Path) -> None:
    target = tmp_path / "src" / "module.py"
    target.parent.mkdir()
    target.write_bytes(b"before\ndef target():\n    return 1\nafter\n")

    prepared = semantic_edit.prepare_line_target(
        tmp_path,
        path="src/module.py",
        start_line=2,
        end_line=3,
        allowed_writes=("src/*.py",),
    )
    receipt = prepared.receipt(target_id="target")

    assert receipt["fragment"] == "def target():\n    return 1\n"
    assert receipt["fragment_bytes"] < receipt["file_bytes"]
    assert receipt["whole_file_bytes_not_returned_by_tool"] == (
        receipt["file_bytes"] - receipt["fragment_bytes"]
    )
    assert receipt["token_savings_claimed"] is False


def test_empty_file_has_one_hash_bound_virtual_line(tmp_path: Path) -> None:
    target = tmp_path / "out" / "result.txt"
    target.parent.mkdir()
    target.write_bytes(b"")

    prepared = semantic_edit.prepare_line_target(
        tmp_path,
        path="out/result.txt",
        start_line=1,
        end_line=1,
        allowed_writes=("out/*.txt",),
    )
    assert prepared.fragment == ""
    assert prepared.file_bytes == 0
    assert prepared.fragment_bytes == 0

    next_text, metrics = semantic_edit.apply_line_ranges(
        "",
        [{
            "start_line": 1,
            "end_line": 1,
            "new": "created\n",
            "fragment_sha256": hashlib.sha256(b"").hexdigest(),
        }],
    )
    assert next_text == "created\n"
    assert metrics["old_region_bytes"] == 0
    assert metrics["replacement_bytes"] == len(b"created\n")


def test_empty_file_rejects_every_nonvirtual_line_range(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_bytes(b"")

    with pytest.raises(semantic_edit.SemanticEditError, match="out_of_bounds:1:2:0"):
        semantic_edit.prepare_line_target(
            tmp_path,
            path="out.txt",
            start_line=1,
            end_line=2,
            allowed_writes=("out.txt",),
        )


def test_worker_semantic_edit_is_hash_bound_atomic_and_idempotent(tmp_path: Path) -> None:
    repo = tmp_path / "worktree"
    target = repo / "src" / "module.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"before\ndef target():\n    return 1\nafter\n")
    ctx = worker_tools.WorkerToolContext(
        task_id="TASK",
        runner="runner",
        topic="topic",
        request_id="request",
        repo=repo,
        authority_repo=repo,
        source_graph_targets=("src/module.py",),
        session_topic="topic",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        allowed_writes=("src/*.py",),
    )
    session = worker_tools.WorkerSemanticEditSession(ctx)

    prepared = session.prepare(file_path="src/module.py", start_line=2, end_line=3)
    assert prepared["ok"] is True
    result = session.apply(
        target_id=prepared["target_id"],
        new="def target():\n    return 2",
        idempotency_key="edit-1",
    )
    assert result["ok"] is True
    assert result["model_reemitted_old_bytes"] == 0
    assert result["file_bytes"] == len("before\ndef target():\n    return 1\nafter\n".encode())
    assert target.read_text(encoding="utf-8") == "before\ndef target():\n    return 2\nafter\n"

    replay = session.apply(
        target_id=prepared["target_id"],
        new="ignored on replay",
        idempotency_key="edit-1",
    )
    assert replay["idempotent_replay"] is True
    assert replay["after_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()


def test_worker_semantic_edit_rejects_stale_and_out_of_scope(tmp_path: Path) -> None:
    repo = tmp_path / "worktree"
    target = repo / "src" / "module.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"one\ntwo\n")
    ctx = worker_tools.WorkerToolContext(
        task_id="TASK",
        runner="runner",
        topic="topic",
        request_id="request",
        repo=repo,
        authority_repo=repo,
        source_graph_targets=("src/module.py",),
        session_topic="topic",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        allowed_writes=("src/*.py",),
    )
    session = worker_tools.WorkerSemanticEditSession(ctx)
    denied = session.prepare(file_path="README.md", start_line=1, end_line=1)
    assert denied == {
        "ok": False,
        "tool": "semantic_edit_prepare",
        "reason": "semantic_edit_path_not_allowed:README.md",
    }

    prepared = session.prepare(file_path="src/module.py", start_line=1, end_line=1)
    target.write_bytes(b"changed\ntwo\n")
    stale = session.apply(
        target_id=prepared["target_id"], new="new", idempotency_key="edit-2"
    )
    assert stale["ok"] is False
    assert "semantic_edit_stale_file" in stale["reason"]
    assert target.read_text(encoding="utf-8") == "changed\ntwo\n"


def test_terminal_semantic_edit_evidence_is_bounded_and_byte_only(tmp_path: Path) -> None:
    stdout = tmp_path / "stdout.jsonl"
    stdout.write_text(
        json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "edit_protocol": "aiworkhub.vscode_lm.semantic_edit_response.v3",
            "semantic_edit_metrics": [{
                "path": "src/module.py",
                "file_bytes": 1000,
                "range_count": 1,
                "old_region_bytes": 80,
                "replacement_bytes": 60,
                "model_reemitted_old_bytes": 0,
            }],
        }) + "\n",
        encoding="utf-8",
    )

    evidence = process_launcher._semantic_edit_evidence_from_output(stdout)

    assert evidence == {
        "schema_id": "aiworkhub.semantic_edit_runtime_evidence.v1",
        "observed": True,
        "file_count": 1,
        "range_count": 1,
        "file_bytes": 1000,
        "old_region_bytes": 80,
        "replacement_bytes": 60,
        "model_reemitted_old_bytes": 0,
        "token_savings_claimed": False,
    }


def test_terminal_semantic_edit_evidence_accepts_authenticated_cli_receipt(
    tmp_path: Path,
) -> None:
    missing_stdout = tmp_path / "missing.jsonl"
    evidence = process_launcher._semantic_edit_evidence_from_output(
        missing_stdout,
        worker_mcp_gate={
            "verification": {
                "semantic_edit_apply_receipts": [{
                    "file_bytes": 8453,
                    "range_count": 1,
                    "old_region_bytes": 26,
                    "replacement_bytes": 92,
                    "model_reemitted_old_bytes": 0,
                    "token_savings_claimed": False,
                }],
            },
        },
    )

    assert evidence["observed"] is True
    assert evidence["file_count"] == 1
    assert evidence["file_bytes"] == 8453
    assert evidence["replacement_bytes"] == 92
    assert evidence["token_savings_claimed"] is False


def test_verified_audit_exposes_only_semantic_edit_byte_receipt(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "worktree"
    target = repo / "src" / "module.py"
    target.parent.mkdir(parents=True)
    original = "before\ndef target():\n    return 1\nafter\n"
    target.write_bytes(original.encode("utf-8"))
    ledger = tmp_path / "audit.jsonl"
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"k" * 32)
    ctx = worker_tools.WorkerToolContext(
        task_id="TASK",
        runner="runner",
        topic="topic",
        request_id="request",
        repo=repo,
        authority_repo=tmp_path,
        source_graph_targets=("src/module.py",),
        session_topic="topic",
        audit_ledger_path=ledger,
        audit_hmac_key_path=key_path,
        allowed_writes=("src/*.py",),
    )
    session = worker_tools.WorkerSemanticEditSession(ctx)
    prepared = session.prepare(file_path="src/module.py", start_line=2, end_line=3)
    applied = session.apply(
        target_id=prepared["target_id"],
        new="def target():\n    return 2",
        idempotency_key="edit-audit-1",
    )
    assert applied["ok"] is True

    verified = worker_tools.verify_audit_ledger(
        ledger,
        key_path,
        task_id="TASK",
        runner="runner",
        topic="topic",
        request_id="request",
    )

    assert verified["successful_call_count_by_tool"]["semantic_edit_prepare"] == 1
    assert verified["successful_call_count_by_tool"]["semantic_edit_apply"] == 1
    assert verified["semantic_edit_apply_receipts"] == [{
        "file_bytes": len(original.encode("utf-8")),
        "range_count": 1,
        "old_region_bytes": len("def target():\n    return 1\n".encode("utf-8")),
        "replacement_bytes": len("def target():\n    return 2\n".encode("utf-8")),
        "model_reemitted_old_bytes": 0,
        "token_savings_claimed": False,
    }]
    serialized = json.dumps(verified, sort_keys=True)
    assert "edit-audit-1" not in serialized
    assert "src/module.py" not in serialized
