"""Semantic-edit coverage: the measurement that answers "does everyone use it?".

The apply receipt alone proves a worker used the tool AT LEAST ONCE.  These
tests cover the join that gives it a denominator (the candidate's changed
paths), the three policy exceptions, the difference between "unmeasured" and
"zero coverage", the ratio arithmetic, and the boundary that matters most:
this record gates nothing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from aiworkhub import dashboard
from aiworkhub import dashboard_kpis
from aiworkhub import process_launcher
from aiworkhub import worker_ai_tools_mcp as worker_tools


REPO_ROOT = Path(__file__).resolve().parents[1]


def _digest(relative: str) -> str:
    return hashlib.sha256(relative.encode("utf-8")).hexdigest()


def _workspace(tmp_path: Path, files: dict[str, bytes], baseline: dict[str, str | None]):
    root = tmp_path / "worktree"
    for relative, payload in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return SimpleNamespace(
        path=root,
        workspace_baseline=dict(baseline),
        tree_baseline=None,
    )


def _gate(receipts, *, ok: bool = True, declarations=None):
    verification = {"ok": ok, "semantic_edit_apply_receipts": list(receipts)}
    if declarations is not None:
        verification["semantic_edit_exception_declarations"] = list(declarations)
    return {"verification": verification}


def _receipt(relative: str, **overrides):
    row = {
        "path_sha256": _digest(relative),
        "file_bytes": 1000,
        "range_count": 1,
        "old_region_bytes": 40,
        "replacement_bytes": 50,
        "model_reemitted_old_bytes": 0,
        "token_savings_claimed": False,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# The join: an apply receipt has to reach the path it edited.
# ---------------------------------------------------------------------------

def test_join_separates_applied_raw_only_and_new_file_paths(tmp_path: Path) -> None:
    workspace = _workspace(
        tmp_path,
        {
            "src/applied.py": b"a" * 300,
            "src/raw.py": b"b" * 100,
            "src/brand_new.py": b"c" * 700,
        },
        baseline={
            "src/applied.py": "old-hash",
            "src/raw.py": "old-hash",
            # No baseline hash: the path did not exist when the workspace was
            # created, so the policy's "a new file" exception applies.
            "src/brand_new.py": None,
        },
    )

    record = process_launcher._semantic_edit_coverage(
        ["src/applied.py", "src/raw.py", "src/brand_new.py"],
        workspace=workspace,
        worker_mcp_gate=_gate([_receipt("src/applied.py")]),
        granted_tool_names=("aiworkhub_worker_semantic_edit_apply",),
    )

    assert record["measured"] is True
    assert record["changed_paths_count"] == 3
    assert record["paths_new_file"] == 1
    assert record["eligible_paths_count"] == 2
    assert record["paths_with_apply"] == 1
    assert record["paths_raw_only"] == ["src/raw.py"]
    assert record["paths_raw_only_count"] == 1
    assert record["apply_receipts_joined"] == 1
    assert record["apply_receipts_unjoinable"] == 0
    # The new file never enters the denominator.
    assert record["bytes_changed"] == 400
    assert record["bytes_via_apply"] == 300


def test_receipt_for_a_path_the_attempt_did_not_change_is_unjoinable(
    tmp_path: Path,
) -> None:
    workspace = _workspace(
        tmp_path,
        {"src/a.py": b"x" * 10},
        baseline={"src/a.py": "old-hash"},
    )

    record = process_launcher._semantic_edit_coverage(
        ["src/a.py"],
        workspace=workspace,
        worker_mcp_gate=_gate([
            _receipt("src/a.py"),
            _receipt("src/reverted.py"),
        ]),
        granted_tool_names=("aiworkhub_worker_semantic_edit_apply",),
    )

    assert record["apply_receipts_total"] == 2
    assert record["apply_receipts_joined"] == 1
    assert record["apply_receipts_unjoinable"] == 1


def test_identifier_rule_is_shared_with_the_authenticated_ledger(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "worktree"
    target = repo / "src" / "module.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"before\ndef target():\n    return 1\nafter\n")
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
    assert session.apply(
        target_id=prepared["target_id"],
        new="def target():\n    return 2",
        idempotency_key="edit-1",
    )["ok"] is True

    verified = worker_tools.verify_audit_ledger(
        ledger, key_path,
        task_id="TASK", runner="runner", topic="topic", request_id="request",
    )
    receipt = verified["semantic_edit_apply_receipts"][0]

    assert receipt["path_sha256"] == process_launcher.semantic_edit_path_identifier(
        "src/module.py"
    )
    # The ledger still carries no path text at all.
    assert "src/module.py" not in json.dumps(verified, sort_keys=True)


# ---------------------------------------------------------------------------
# The three policy exceptions: derived where the runtime can derive them,
# declared where only the worker can know.
# ---------------------------------------------------------------------------

def test_new_file_is_derived_from_the_missing_baseline_hash(tmp_path: Path) -> None:
    workspace = _workspace(
        tmp_path, {"src/new.py": b"n" * 50}, baseline={"src/new.py": None},
    )

    record = process_launcher._semantic_edit_coverage(
        ["src/new.py"],
        workspace=workspace,
        worker_mcp_gate=_gate([]),
        granted_tool_names=("aiworkhub_worker_semantic_edit_apply",),
    )

    assert record["derived_exceptions"] == [{
        "path": "src/new.py",
        "exception": "new_file",
        "basis": "no_baseline_hash_at_workspace_creation",
        "source": "runtime_derivation",
    }]
    assert record["undeclared_raw_only"] == []
    assert record["paths_raw_only_count"] == 0
    # Nothing eligible was changed, so there is no ratio to state.
    assert record["coverage_ratio"] is None


def test_adapter_without_the_tool_is_derived_from_its_granted_tool_list(
    tmp_path: Path,
) -> None:
    workspace = _workspace(
        tmp_path, {"src/a.py": b"a" * 20}, baseline={"src/a.py": "old-hash"},
    )

    record = process_launcher._semantic_edit_coverage(
        ["src/a.py"],
        workspace=workspace,
        worker_mcp_gate=_gate([]),
        granted_tool_names=(
            "aiworkhub_worker_source_graph_query",
            "aiworkhub_worker_session_current_state",
        ),
    )

    assert record["adapter_semantic_edit_granted"] is False
    assert record["derived_exceptions"] == [{
        "path": "src/a.py",
        "exception": "adapter_without_tools",
        "basis": "semantic_edit_apply_absent_from_granted_tool_names",
        "source": "runtime_derivation",
    }]
    # Derived, so it is NOT an undeclared raw edit.
    assert record["undeclared_raw_only"] == []
    assert record["paths_raw_only_count"] == 1
    assert record["coverage_ratio"] == 0.0


def test_unknown_granted_tool_list_never_excuses_a_raw_edit(tmp_path: Path) -> None:
    workspace = _workspace(
        tmp_path, {"src/a.py": b"a" * 20}, baseline={"src/a.py": "old-hash"},
    )

    record = process_launcher._semantic_edit_coverage(
        ["src/a.py"],
        workspace=workspace,
        worker_mcp_gate=_gate([]),
        granted_tool_names=(),
    )

    assert record["adapter_semantic_edit_granted"] is None
    assert record["derived_exceptions"] == []
    assert record["undeclared_raw_only"] == ["src/a.py"]


def test_spans_most_of_a_file_is_declared_and_explicitly_uncorroborated(
    tmp_path: Path,
) -> None:
    workspace = _workspace(
        tmp_path, {"src/a.py": b"a" * 20}, baseline={"src/a.py": "old-hash"},
    )

    record = process_launcher._semantic_edit_coverage(
        ["src/a.py"],
        workspace=workspace,
        worker_mcp_gate=_gate(
            [],
            declarations=[{
                "path_sha256": _digest("src/a.py"),
                "exception": "spans_most_of_file",
                "reason": "rewrote the whole module body",
            }],
        ),
        granted_tool_names=("aiworkhub_worker_semantic_edit_apply",),
    )

    assert record["declared_exceptions"] == [{
        "path": "src/a.py",
        "exception": "spans_most_of_file",
        "reason": "rewrote the whole module body",
        "source": "worker_declaration",
        "corroborated": False,
        "corroboration": "no_byte_evidence_for_a_raw_write",
    }]
    assert record["undeclared_raw_only"] == []
    # A declaration is evidence, not an excuse: the path stays raw-only and
    # still counts against the byte ratio.
    assert record["paths_raw_only"] == ["src/a.py"]
    assert record["coverage_ratio"] == 0.0


def test_a_declaration_that_contradicts_the_baseline_is_not_corroborated(
    tmp_path: Path,
) -> None:
    workspace = _workspace(
        tmp_path, {"src/a.py": b"a" * 20}, baseline={"src/a.py": "old-hash"},
    )

    record = process_launcher._semantic_edit_coverage(
        ["src/a.py"],
        workspace=workspace,
        worker_mcp_gate=_gate(
            [],
            declarations=[{
                "path_sha256": _digest("src/a.py"),
                "exception": "new_file",
                "reason": "claimed new",
            }],
        ),
        granted_tool_names=("aiworkhub_worker_semantic_edit_apply",),
    )

    declared = record["declared_exceptions"][0]
    assert declared["corroborated"] is False
    assert declared["corroboration"] == "baseline_hash_present_for_path"


def test_an_unknown_exception_code_is_reported_not_accepted(tmp_path: Path) -> None:
    workspace = _workspace(
        tmp_path, {"src/a.py": b"a" * 20}, baseline={"src/a.py": "old-hash"},
    )

    record = process_launcher._semantic_edit_coverage(
        ["src/a.py"],
        workspace=workspace,
        worker_mcp_gate=_gate(
            [],
            declarations=[{
                "path_sha256": _digest("src/a.py"),
                "exception": "i_was_in_a_hurry",
                "reason": "",
            }],
        ),
        granted_tool_names=("aiworkhub_worker_semantic_edit_apply",),
    )

    declared = record["declared_exceptions"][0]
    assert declared["corroborated"] is False
    assert declared["corroboration"] == "unknown_exception_code"


def test_undeclared_raw_edit_is_the_signal_this_record_exists_for(
    tmp_path: Path,
) -> None:
    workspace = _workspace(
        tmp_path,
        {"src/a.py": b"a" * 20, "src/b.py": b"b" * 20},
        baseline={"src/a.py": "old-hash", "src/b.py": "old-hash"},
    )

    record = process_launcher._semantic_edit_coverage(
        ["src/a.py", "src/b.py"],
        workspace=workspace,
        worker_mcp_gate=_gate([_receipt("src/a.py")]),
        granted_tool_names=("aiworkhub_worker_semantic_edit_apply",),
    )

    assert record["undeclared_raw_only"] == ["src/b.py"]
    assert record["undeclared_raw_only_count"] == 1


# ---------------------------------------------------------------------------
# Absence of evidence is never zero.
# ---------------------------------------------------------------------------

def test_unverified_ledger_is_unmeasured_not_zero_coverage(tmp_path: Path) -> None:
    workspace = _workspace(
        tmp_path, {"src/a.py": b"a" * 20}, baseline={"src/a.py": "old-hash"},
    )

    record = process_launcher._semantic_edit_coverage(
        ["src/a.py"],
        workspace=workspace,
        worker_mcp_gate=_gate([], ok=False),
        granted_tool_names=("aiworkhub_worker_semantic_edit_apply",),
    )

    assert record["measured"] is False
    assert record["unmeasured_reason"] == "ledger_unverified"
    assert record["coverage_ratio"] is None
    assert record["paths_raw_only"] == []


def test_absent_gate_is_unmeasured_not_zero_coverage(tmp_path: Path) -> None:
    workspace = _workspace(
        tmp_path, {"src/a.py": b"a" * 20}, baseline={"src/a.py": "old-hash"},
    )

    record = process_launcher._semantic_edit_coverage(
        ["src/a.py"], workspace=workspace, worker_mcp_gate=None,
    )

    assert record["measured"] is False
    assert record["unmeasured_reason"] == "ledger_unverified"
    assert record["coverage_ratio"] is None


def test_an_attempt_that_changed_nothing_is_unmeasured(tmp_path: Path) -> None:
    record = process_launcher._semantic_edit_coverage(
        [],
        workspace=_workspace(tmp_path, {}, baseline={}),
        worker_mcp_gate=_gate([]),
    )

    assert record["measured"] is False
    assert record["unmeasured_reason"] == "no_changed_paths"
    assert record["coverage_ratio"] is None


def test_receipts_written_before_the_identifier_existed_are_unmeasurable(
    tmp_path: Path,
) -> None:
    workspace = _workspace(
        tmp_path, {"src/a.py": b"a" * 20}, baseline={"src/a.py": "old-hash"},
    )
    legacy = _receipt("src/a.py")
    legacy.pop("path_sha256")

    record = process_launcher._semantic_edit_coverage(
        ["src/a.py"],
        workspace=workspace,
        worker_mcp_gate=_gate([legacy]),
        granted_tool_names=("aiworkhub_worker_semantic_edit_apply",),
    )

    assert record["measured"] is False
    assert record["unmeasured_reason"] == "receipts_without_path_identifier"
    assert record["apply_receipts_total"] == 1
    assert record["coverage_ratio"] is None


def test_a_verified_ledger_with_no_applies_is_a_real_zero_not_unmeasured(
    tmp_path: Path,
) -> None:
    workspace = _workspace(
        tmp_path, {"src/a.py": b"a" * 20}, baseline={"src/a.py": "old-hash"},
    )

    record = process_launcher._semantic_edit_coverage(
        ["src/a.py"],
        workspace=workspace,
        worker_mcp_gate=_gate([]),
        granted_tool_names=("aiworkhub_worker_semantic_edit_apply",),
    )

    assert record["measured"] is True
    assert record["unmeasured_reason"] == ""
    assert record["coverage_ratio"] == 0.0
    assert record["paths_raw_only"] == ["src/a.py"]


def test_bridge_observed_applies_outside_the_ledger_are_unmeasured_not_zero(
    tmp_path: Path,
) -> None:
    """The vscode_lm route reports edits on its own stream, not the ledger."""

    workspace = _workspace(
        tmp_path, {"src/a.py": b"a" * 20}, baseline={"src/a.py": "old-hash"},
    )

    record = process_launcher._semantic_edit_coverage(
        ["src/a.py"],
        workspace=workspace,
        worker_mcp_gate=_gate([]),
        granted_tool_names=("aiworkhub_worker_semantic_edit_apply",),
        runtime_evidence={
            "schema_id": "aiworkhub.semantic_edit_runtime_evidence.v1",
            "observed": True,
            "file_count": 1,
        },
    )

    assert record["measured"] is False
    assert record["unmeasured_reason"] == (
        "applies_observed_outside_the_authenticated_ledger"
    )
    assert record["coverage_ratio"] is None


def test_a_deleted_path_leaves_the_denominator_rather_than_scoring_zero(
    tmp_path: Path,
) -> None:
    workspace = _workspace(
        tmp_path, {"src/kept.py": b"k" * 40}, baseline={
            "src/kept.py": "old-hash", "src/gone.py": "old-hash",
        },
    )

    record = process_launcher._semantic_edit_coverage(
        ["src/kept.py", "src/gone.py"],
        workspace=workspace,
        worker_mcp_gate=_gate([_receipt("src/kept.py")]),
        granted_tool_names=("aiworkhub_worker_semantic_edit_apply",),
    )

    assert record["paths_deleted"] == 1
    assert record["eligible_paths_count"] == 1
    assert record["coverage_ratio"] == 1.0


# ---------------------------------------------------------------------------
# Ratio arithmetic, and the byte label that stays a byte label.
# ---------------------------------------------------------------------------

def test_coverage_ratio_is_a_byte_weighted_share_of_changed_files(
    tmp_path: Path,
) -> None:
    workspace = _workspace(
        tmp_path,
        {"src/big.py": b"b" * 750, "src/small.py": b"s" * 250},
        baseline={"src/big.py": "old-hash", "src/small.py": "old-hash"},
    )

    record = process_launcher._semantic_edit_coverage(
        ["src/big.py", "src/small.py"],
        workspace=workspace,
        worker_mcp_gate=_gate([_receipt("src/big.py")]),
        granted_tool_names=("aiworkhub_worker_semantic_edit_apply",),
    )

    assert record["bytes_changed"] == 1000
    assert record["bytes_via_apply"] == 750
    assert record["coverage_ratio"] == 0.75
    assert record["bytes_basis"] == "changed_file_size_at_finalization"
    # Bytes are bytes. Never tokens, never money.
    assert record["token_savings_claimed"] is False
    assert record["measurement_only"] is True


def test_full_coverage_is_exactly_one(tmp_path: Path) -> None:
    workspace = _workspace(
        tmp_path,
        {"src/a.py": b"a" * 11, "src/b.py": b"b" * 13},
        baseline={"src/a.py": "old-hash", "src/b.py": "old-hash"},
    )

    record = process_launcher._semantic_edit_coverage(
        ["src/a.py", "src/b.py"],
        workspace=workspace,
        worker_mcp_gate=_gate([_receipt("src/a.py"), _receipt("src/b.py")]),
        granted_tool_names=("aiworkhub_worker_semantic_edit_apply",),
    )

    assert record["coverage_ratio"] == 1.0
    assert record["paths_with_apply"] == 2
    assert record["bytes_changed"] == record["bytes_via_apply"] == 24


# ---------------------------------------------------------------------------
# This is a measurement, not a gate.
# ---------------------------------------------------------------------------

def test_no_acceptance_or_gate_module_reads_the_coverage_record() -> None:
    """The whole point of the card: measure first, never bound what is unmeasured."""

    gate_modules = (
        "process_launcher_accept_review.py",
        "process_launcher_acceptance.py",
        "quality_gates.py",
        "quality_reviewer.py",
        "quality_review_ingest.py",
        "quality_review_scope.py",
        "review_orchestrator.py",
        "review_lifecycle.py",
        "task_engine.py",
        "task_fsm.py",
        "core.py",
    )
    for name in gate_modules:
        path = REPO_ROOT / "src" / "aiworkhub" / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert "semantic_edit_coverage" not in text, name


def test_the_launcher_only_builds_and_records_the_coverage_measurement() -> None:
    text = (REPO_ROOT / "src" / "aiworkhub" / "process_launcher.py").read_text(
        encoding="utf-8"
    )
    # One definition, one call, one terminal-event field, plus the guarded
    # fallback record.  No branch, gate, or refusal consults it.
    assert text.count("_semantic_edit_coverage(") == 2
    assert text.count('"semantic_edit_coverage": semantic_edit_coverage,') == 1
    for refusal in ("raise", "return False", "satisfied"):
        for line in text.splitlines():
            if "semantic_edit_coverage" in line:
                assert refusal not in line


# ---------------------------------------------------------------------------
# Visibility: the terminal-event projection and the fleet KPI.
# ---------------------------------------------------------------------------

def _run(adapter: str, coverage: dict) -> dict:
    return {
        "adapter_id": adapter,
        "state": "review_ready",
        "ai_infra_context": {"semantic_edit_coverage": coverage},
    }


def test_dashboard_projection_carries_counts_without_path_text() -> None:
    projected = dashboard._compact_ai_infra({
        "semantic_edit_coverage": {
            "schema_id": process_launcher.SEMANTIC_EDIT_COVERAGE_SCHEMA_ID,
            "measured": True,
            "unmeasured_reason": "",
            "changed_paths_count": 3,
            "eligible_paths_count": 2,
            "paths_with_apply": 1,
            "paths_raw_only": ["src/secret_layout.py"],
            "paths_raw_only_count": 1,
            "undeclared_raw_only": ["src/secret_layout.py"],
            "undeclared_raw_only_count": 1,
            "bytes_changed": 400,
            "bytes_via_apply": 300,
            "coverage_ratio": 0.75,
            "adapter_semantic_edit_granted": True,
        },
    })

    record = projected["semantic_edit_coverage"]
    assert record["measured"] is True
    assert record["changed_paths_count"] == 3
    assert record["undeclared_raw_only_count"] == 1
    assert record["coverage_ratio"] == 0.75
    assert record["token_savings_claimed"] is False
    assert "src/secret_layout.py" not in json.dumps(projected, sort_keys=True)


def test_kpi_reports_per_adapter_shape_and_keeps_unmeasured_out_of_the_mean() -> None:
    runs = [
        _run("codex_cli", {
            "measured": True, "coverage_ratio": 1.0,
            "changed_paths_count": 2, "paths_with_apply": 2,
            "paths_raw_only_count": 0, "undeclared_raw_only_count": 0,
            "bytes_changed": 100, "bytes_via_apply": 100,
        }),
        _run("codex_cli", {
            "measured": True, "coverage_ratio": 0.0,
            "changed_paths_count": 1, "paths_with_apply": 0,
            "paths_raw_only_count": 1, "undeclared_raw_only_count": 1,
            "bytes_changed": 100, "bytes_via_apply": 0,
        }),
        _run("codex_cli", {
            "measured": True, "coverage_ratio": 0.5,
            "changed_paths_count": 2, "paths_with_apply": 1,
            "paths_raw_only_count": 1, "undeclared_raw_only_count": 0,
            "bytes_changed": 200, "bytes_via_apply": 100,
        }),
        _run("claude_cli", {
            "measured": False, "unmeasured_reason": "ledger_unverified",
        }),
    ]

    kpi = dashboard_kpis._semantic_edit_coverage_kpi(runs)

    assert kpi["measured_runs"] == 3
    assert kpi["unmeasured_runs"] == 1
    assert kpi["unmeasured_reasons"] == {"ledger_unverified": 1}
    assert kpi["semantic_only_attempts"] == 1
    assert kpi["raw_only_attempts"] == 1
    assert kpi["mixed_attempts"] == 1
    assert kpi["undeclared_raw_only"] == 1
    # (1.0 + 0.0 + 0.5) / 3 -- the unmeasured claude run is NOT a fourth 0.
    assert kpi["mean_attempt_coverage"] == 50.0
    assert kpi["byte_coverage_rate"] == 50.0
    assert kpi["token_savings_available"] is False

    by_name = {row["name"]: row for row in kpi["adapters"]}
    assert by_name["codex_cli"]["attempts"] == 3
    assert by_name["codex_cli"]["measured_attempts"] == 3
    assert by_name["codex_cli"]["paths_raw_only"] == 2
    assert by_name["claude_cli"]["measured_attempts"] == 0
    assert by_name["claude_cli"]["mean_attempt_coverage"] is None
