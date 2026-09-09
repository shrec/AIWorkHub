"""The declared-exception channel: a fallback that is recorded, not hidden.

The semantic-edit mandate has three legitimate exceptions -- a new file, a
change spanning most of a file, and an adapter without these tools -- so a
blanket deny of the raw editor would be wrong.  What is wrong is a fallback
that leaves no trace: measured over 2,648 gate-verified attempts that changed
at least one file, 1,500 made zero semantic-edit applies, and nothing in the
run said whether that was an exception or a shortcut.

``aiworkhub_worker_semantic_edit_exception_declare`` is the channel that
answers it.  These tests run it through the REAL HMAC ledger rather than a
stub, because the whole value of the record is that it is authenticated, and
they carry it the rest of the way into the coverage record that reads it.

Nothing here is a gate: every test that touches acceptance asserts that it
still measures rather than refuses.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from aiworkhub import process_launcher
from aiworkhub import worker_ai_tools_mcp as worker_tools


def _ctx(tmp_path: Path, *, allowed_writes=("src/*.py",)):
    repo = tmp_path / "worktree"
    repo.mkdir(exist_ok=True)
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
        allowed_writes=tuple(allowed_writes),
    )
    return ctx, ledger, key_path


def _verify(ledger: Path, key_path: Path):
    return worker_tools.verify_audit_ledger(
        ledger,
        key_path,
        task_id="TASK",
        runner="runner",
        topic="topic",
        request_id="request",
    )


def test_a_declaration_reaches_the_authenticated_ledger_in_the_shape_ingest_expects(
    tmp_path: Path,
) -> None:
    """End to end through the real HMAC ledger, not a hand-built dict."""
    ctx, ledger, key_path = _ctx(tmp_path)

    result = worker_tools.semantic_edit_exception_declare(
        ctx,
        path="src/module.py",
        exception="new_file",
        reason="created by this card",
    )

    assert result["ok"] is True
    assert result["recorded"] is True
    assert result["is_gate"] is False

    verified = _verify(ledger, key_path)
    assert verified["ok"] is True
    assert verified["successful_call_count_by_tool"][
        "semantic_edit_exception_declare"
    ] == 1
    assert verified["semantic_edit_exception_declarations"] == [{
        "path_sha256": hashlib.sha256(b"src/module.py").hexdigest(),
        "exception": "new_file",
        "reason": "created by this card",
    }]


def test_the_declaration_joins_on_the_same_key_an_apply_receipt_uses(
    tmp_path: Path,
) -> None:
    """A record that cannot be joined to a path is not evidence of anything."""
    ctx, ledger, key_path = _ctx(tmp_path)
    worker_tools.semantic_edit_exception_declare(
        ctx, path="src/module.py", exception="spans_most_of_file", reason="",
    )

    verified = _verify(ledger, key_path)
    declared = verified["semantic_edit_exception_declarations"][0]

    assert declared["path_sha256"] == process_launcher.semantic_edit_path_identifier(
        "src/module.py"
    )


def test_the_verified_record_carries_no_path_text(tmp_path: Path) -> None:
    """The apply receipt's privacy boundary, applied to the declaration too."""
    ctx, ledger, key_path = _ctx(tmp_path)
    worker_tools.semantic_edit_exception_declare(
        ctx, path="src/module.py", exception="new_file", reason="",
    )

    serialized = json.dumps(_verify(ledger, key_path), sort_keys=True)

    assert "src/module.py" not in serialized


def test_a_declaration_moves_a_path_from_undeclared_to_declared(
    tmp_path: Path,
) -> None:
    """The whole point: the coverage record reads this and stops calling it raw.

    The gate dict here is the REAL ``verify_audit_ledger`` output, so this is
    the actual producer/consumer pair rather than two hand-written shapes that
    happen to agree.
    """
    ctx, ledger, key_path = _ctx(tmp_path)
    worker_tools.semantic_edit_exception_declare(
        ctx,
        path="src/module.py",
        exception="spans_most_of_file",
        reason="rewrote the whole dispatch table",
    )
    verification = _verify(ledger, key_path)

    root = tmp_path / "candidate"
    (root / "src").mkdir(parents=True)
    (root / "src" / "module.py").write_bytes(b"x" * 400)
    workspace = SimpleNamespace(
        path=root,
        workspace_baseline={"src/module.py": "old-hash"},
        tree_baseline=None,
    )

    record = process_launcher._semantic_edit_coverage(
        ["src/module.py"],
        workspace=workspace,
        worker_mcp_gate={"verification": verification},
        granted_tool_names=("aiworkhub_worker_semantic_edit_apply",),
    )

    assert record["undeclared_raw_only_count"] == 0
    declared = record["declared_exceptions"]
    assert len(declared) == 1
    assert declared[0]["exception"] == "spans_most_of_file"
    # Still a measurement, never a refusal.
    assert record["measurement_only"] is True


def test_the_same_path_without_a_declaration_is_reported_as_undeclared(
    tmp_path: Path,
) -> None:
    """The contrast case, so the previous test proves the declaration did it."""
    root = tmp_path / "candidate"
    (root / "src").mkdir(parents=True)
    (root / "src" / "module.py").write_bytes(b"x" * 400)
    workspace = SimpleNamespace(
        path=root,
        workspace_baseline={"src/module.py": "old-hash"},
        tree_baseline=None,
    )

    record = process_launcher._semantic_edit_coverage(
        ["src/module.py"],
        workspace=workspace,
        worker_mcp_gate={
            "verification": {
                "ok": True,
                "semantic_edit_apply_receipts": [],
                "semantic_edit_exception_declarations": [],
            }
        },
        granted_tool_names=("aiworkhub_worker_semantic_edit_apply",),
    )

    assert record["undeclared_raw_only_count"] == 1


def test_an_unknown_exception_code_is_recorded_rather_than_refused(
    tmp_path: Path,
) -> None:
    """A record that can be refused is a record that hides edits.

    The closed vocabulary is enforced where it can be reported -- the coverage
    record marks an unknown code ``unknown_exception_code`` -- not here, where
    refusing would delete the evidence that a fallback happened at all.
    """
    ctx, ledger, key_path = _ctx(tmp_path)

    result = worker_tools.semantic_edit_exception_declare(
        ctx, path="src/module.py", exception="i_was_in_a_hurry", reason="",
    )

    assert result["ok"] is True
    assert result["recorded"] is True
    verified = _verify(ledger, key_path)
    assert verified["semantic_edit_exception_declarations"][0]["exception"] == (
        "i_was_in_a_hurry"
    )


def test_a_path_outside_allowed_writes_is_recorded_and_flagged_not_refused(
    tmp_path: Path,
) -> None:
    ctx, ledger, key_path = _ctx(tmp_path, allowed_writes=("src/*.py",))

    result = worker_tools.semantic_edit_exception_declare(
        ctx, path="docs/README.md", exception="new_file", reason="",
    )

    assert result["ok"] is True
    assert result["recorded"] is True
    assert result["path_in_allowed_writes"] is False
    assert len(_verify(ledger, key_path)["semantic_edit_exception_declarations"]) == 1


def test_a_declaration_that_cannot_name_a_path_is_not_written_to_the_ledger(
    tmp_path: Path,
) -> None:
    """Unjoinable rows are worse than absent: they inflate the count and join
    to nothing.  Reported to the caller, kept out of the authenticated file."""
    ctx, ledger, key_path = _ctx(tmp_path)

    result = worker_tools.semantic_edit_exception_declare(
        ctx, path="../escape.py", exception="new_file", reason="",
    )

    assert result["ok"] is True
    assert result["recorded"] is False
    assert result["unrecorded_reason"]
    assert not ledger.exists() or _verify(ledger, key_path)[
        "semantic_edit_exception_declarations"
    ] == []


def test_reason_and_exception_are_bounded(tmp_path: Path) -> None:
    ctx, ledger, key_path = _ctx(tmp_path)

    worker_tools.semantic_edit_exception_declare(
        ctx, path="src/module.py", exception="e" * 200, reason="r" * 900,
    )

    declared = _verify(ledger, key_path)["semantic_edit_exception_declarations"][0]
    assert len(declared["exception"]) == 64
    assert len(declared["reason"]) == 200


def test_the_tool_is_on_the_worker_surface(tmp_path: Path) -> None:
    """A tool that is implemented but never registered cannot be used."""
    assert (
        "aiworkhub_worker_semantic_edit_exception_declare"
        in worker_tools.MCP_TOOL_NAMES
    )

    registered: list[str] = []

    class _Recorder:
        def tool(self, *, name: str, **_kwargs):
            registered.append(name)

            def decorate(func):
                return func

            return decorate

    ctx, _ledger, _key = _ctx(tmp_path)
    worker_tools.register_tools(_Recorder(), ctx)

    assert "aiworkhub_worker_semantic_edit_exception_declare" in registered
