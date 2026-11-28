"""Regression tests for fix batch 11 (NF-2026-00794 and siblings)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from aiworkhub import (
    quality_reviewer,
    roadmap_store,
    skill_registry_store,
    sqlite_readonly,
    tool_recipes_store,
)
from aiworkhub.quality_reviewer import ReviewerEvidenceError

PATH = "src/module.py"


def _findings_packet(
    *, segments: list[dict] | None = None, checks: dict[str, str] | None = None
) -> dict:
    scoped = {
        lens: {"known_unknowns": [], "packet": {"changed_paths": [{"path": PATH}]}}
        for lens in ("correctness", "security", "code_quality")
    }
    return {
        "candidate": {
            "changed_paths": [{"path": PATH, "sha256": "a" * 64}],
            "source_evidence": [{"path": PATH, "segments": list(segments or [])}],
            "scoped_audits": scoped,
        },
        "mechanical_checks": [
            {"check_id": check, "status": status} for check, status in (checks or {}).items()
        ],
    }


def _delete_segment(start: int, end: int) -> dict:
    return {
        "kind": "delete",
        "candidate_start_line": 19,
        "candidate_end_line": 19,
        "changed_start_line": 0,
        "changed_end_line": 0,
        "baseline_start_line": start,
        "baseline_end_line": end,
        "excerpt_bytes": 0,
        "truncated": False,
    }


# --- (1) NF-2026-00794: stale review_ready candidate verifies from its sealed artifact.


def test_old_candidate_verifies_against_sealed_artifact_not_live_worktree(tmp_path: Path) -> None:
    sealed_bytes = b"def f():\n    return 1\n"
    live = tmp_path / "live"
    sealed = tmp_path / "sealed"
    for root, data in ((live, b"def f():\n    return 2\n"), (sealed, sealed_bytes)):
        (root / "src").mkdir(parents=True)
        (root / PATH).write_bytes(data)
    packet = quality_reviewer.build_review_packet(
        request_id="req1",
        task_id="task1",
        claim_epoch=1,
        worker_provider="adapter-a",
        changed_path_hashes={PATH: hashlib.sha256(sealed_bytes).hexdigest()},
    )
    packet_file = tmp_path / "packet.json"
    packet_file.write_text(json.dumps(packet), encoding="utf-8")

    with pytest.raises(ReviewerEvidenceError, match="quality_review_candidate_hash_mismatch"):
        quality_reviewer.verify_review_packet_candidate(packet_file, live)

    verified = quality_reviewer.verify_review_packet_candidate(
        packet_file, live, candidate_root=sealed
    )
    assert verified["changed_paths"] == [
        {"path": PATH, "sha256": hashlib.sha256(sealed_bytes).hexdigest()}
    ]
    # A tampered snapshot still fails closed with the same stable code.
    (sealed / PATH).write_bytes(b"tampered\n")
    with pytest.raises(ReviewerEvidenceError, match="quality_review_candidate_hash_mismatch"):
        quality_reviewer.verify_review_packet_candidate(
            packet_file, live, candidate_root=sealed
        )


# --- (2) NF-2026-00934: baseline-bound deletion findings and capped caller lists.


def test_deletion_finding_binds_to_authenticated_baseline_lines_only() -> None:
    packet = _findings_packet(segments=[_delete_segment(20, 24)])
    finding = {
        "severity": "high",
        "summary": "Deleted guard drops the null check",
        "evidence": "the removed guard was the only null check",
        "evidence_reference": {"kind": "baseline", "path": PATH, "line_start": 21, "line_end": 22},
    }
    result = quality_reviewer.normalize_packet_findings(
        packet, lens="correctness", findings=[finding]
    )
    assert result[0]["evidence_reference"]["kind"] == "baseline"
    assert result[0]["evidence_level"] == "static_evidence"

    outside = dict(finding, evidence_reference=dict(finding["evidence_reference"], line_end=30))
    with pytest.raises(ReviewerEvidenceError, match="baseline_reference_unbound"):
        quality_reviewer.normalize_packet_findings(packet, lens="correctness", findings=[outside])
    with pytest.raises(ReviewerEvidenceError, match="baseline_reference_unbound"):
        quality_reviewer.normalize_packet_findings(
            _findings_packet(), lens="correctness", findings=[finding]
        )


def test_capped_caller_list_carries_no_unconsumed_known_unknown_key() -> None:
    # The cap is stated by ``complete``/``omitted``; the former
    # ``known_unknowns`` key had no consumer and was removed.
    capped = quality_reviewer._caller_context_rows({"rows": [], "complete": False, "omitted": 3})
    assert capped == {"rows": [], "complete": False, "omitted": 3}
    assert not hasattr(quality_reviewer, "caller_context_known_unknown")


# --- (3) NF-2026-00935: lens-specific admissibility for blocking findings.


def test_security_blocker_needs_a_code_path_anchor_not_a_check_id() -> None:
    packet = _findings_packet(checks={"lint:ruff": "failed"})
    finding = {
        "severity": "high",
        "summary": "Injection reachable",
        "evidence": "flagged by lint:ruff",
        "check_id": "lint:ruff",
    }
    with pytest.raises(ReviewerEvidenceError, match="security_blocking_anchor_required"):
        quality_reviewer.normalize_packet_findings(packet, lens="security", findings=[finding])
    # The same check anchor is an observed failing state for correctness.
    assert quality_reviewer.normalize_packet_findings(
        packet, lens="correctness", findings=[finding]
    )
    anchored = {"severity": "high", "summary": "Injection reachable", "evidence": f"{PATH}:12"}
    assert quality_reviewer.normalize_packet_findings(
        packet, lens="security", findings=[anchored]
    )


def test_blocking_anchor_on_a_passed_check_is_rejected() -> None:
    finding = {
        "severity": "high",
        "summary": "Wrong result",
        "evidence": "flagged by lint:ruff",
        "check_id": "lint:ruff",
    }
    passed = _findings_packet(checks={"lint:ruff": "passed"})
    with pytest.raises(ReviewerEvidenceError, match="blocking_check_not_failed"):
        quality_reviewer.normalize_packet_findings(passed, lens="correctness", findings=[finding])
    failed = _findings_packet(checks={"lint:ruff": "failed"})
    assert quality_reviewer.normalize_packet_findings(
        failed, lens="correctness", findings=[finding]
    )


def test_blocking_performance_claim_needs_a_measured_metric() -> None:
    packet = _findings_packet()
    unmeasured = {"severity": "high", "summary": "Quadratic slowdown", "evidence": f"{PATH}:12"}
    with pytest.raises(ReviewerEvidenceError, match="performance_metric_required"):
        quality_reviewer.normalize_packet_findings(packet, lens="code_quality", findings=[unmeasured])
    measured = dict(unmeasured, evidence=f"{PATH}:12 runs 4000 iterations per call")
    assert quality_reviewer.normalize_packet_findings(
        packet, lens="code_quality", findings=[measured]
    )
    # A non-blocking performance note is not held to the blocking bar.
    assert quality_reviewer.normalize_packet_findings(
        packet, lens="code_quality", findings=[dict(unmeasured, severity="medium")]
    )


# --- (4) NF-2026-00685: one shared SQLite store connection helper.


def test_stores_share_one_connection_helper(tmp_path: Path) -> None:
    assert skill_registry_store.connect_store_db is roadmap_store.connect_store_db
    assert tool_recipes_store.connect_store_db is roadmap_store.connect_store_db
    for module in (roadmap_store, skill_registry_store, tool_recipes_store):
        conn = module._connect(tmp_path / module.__name__.rsplit(".", 1)[1])
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.row_factory is sqlite3.Row
            assert conn.isolation_level is None
        finally:
            conn.close()


# --- (5) NF-2026-00883: roadmap auto-close is still open; wiring it into the
# accept path (process_launcher_accept_review.py) belongs to another card.


def test_roadmap_store_carries_no_unwired_closure_reconcile() -> None:
    assert not hasattr(roadmap_store, "reconcile_closure")
    assert not hasattr(roadmap_store, "closure_readiness")


# --- (6) NF-2026-00514: loaders refuse a store replaced between check and open.


@pytest.mark.parametrize(
    ("module", "load"),
    [
        (tool_recipes_store, lambda repo: tool_recipes_store.list_recipes(repo)),
        (skill_registry_store, lambda repo: skill_registry_store.list_records(repo)),
    ],
)
def test_loader_rejects_store_replaced_between_check_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module, load
) -> None:
    module.initialize_repository(tmp_path)
    db = module._db_path(tmp_path)
    original = sqlite_readonly.connect_readonly
    blocked: list[BaseException] = []

    def replace_then_open(path, *args, **kwargs):
        decoy = db.with_name("decoy.sqlite")
        shutil.copyfile(db, decoy)
        try:
            os.replace(decoy, db)
        except PermissionError as exc:
            # Windows: the authenticated handle is held without
            # FILE_SHARE_DELETE, so the swap cannot happen at all.
            blocked.append(exc)
            decoy.unlink()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(sqlite_readonly, "connect_readonly", replace_then_open)
    from aiworkhub import platform_io

    if platform_io.is_windows():
        assert load(tmp_path) == []
        assert blocked, "the held store handle must deny replacement"
    else:
        with pytest.raises(
            roadmap_store.StorePathAuthenticationError, match="store_path_replaced"
        ):
            load(tmp_path)
    monkeypatch.setattr(sqlite_readonly, "connect_readonly", original)
    assert load(tmp_path) == []


def test_loader_rejects_symlinked_store(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    tool_recipes_store.initialize_repository(real)
    repo = tmp_path / "repo"
    link = tool_recipes_store._db_path(repo)
    link.parent.mkdir(parents=True)
    try:
        os.symlink(tool_recipes_store._db_path(real), link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted on this host")
    with pytest.raises(roadmap_store.StorePathAuthenticationError, match="store_path_indirection"):
        tool_recipes_store.list_recipes(repo)


def test_loader_rejects_store_whose_opened_descriptor_identity_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A swap-and-restore leaves both path lstats equal; the identity bound to
    # the opened descriptor (os.fstat) must still refuse it.
    tool_recipes_store.initialize_repository(tmp_path)
    assert tool_recipes_store.list_recipes(tmp_path) == []
    real_fstat = os.fstat

    def other_inode(fd):
        info = real_fstat(fd)
        fields = list(info)
        fields[1] = info.st_ino + 1  # st_ino
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", other_inode)
    with pytest.raises(roadmap_store.StorePathAuthenticationError, match="store_path_replaced"):
        tool_recipes_store.list_recipes(tmp_path)


def test_hardlinked_store_is_refused(tmp_path: Path) -> None:
    tool_recipes_store.initialize_repository(tmp_path)
    db = tool_recipes_store._db_path(tmp_path)
    try:
        os.link(db, tmp_path / "alias.sqlite")
    except (OSError, NotImplementedError):
        pytest.skip("hard links are not permitted on this host")
    with pytest.raises(roadmap_store.StorePathAuthenticationError, match="store_path_hardlinked"):
        tool_recipes_store.list_recipes(tmp_path)


# --- (1, MCP path) the reviewer packet read verifies the launched candidate root.


def _mcp_ctx(repo: Path, packet_file: Path):
    from aiworkhub import worker_ai_tools_mcp

    return worker_ai_tools_mcp.WorkerToolContext(
        task_id="task1",
        runner="claude",
        topic="fix-batch-11",
        request_id="req1",
        repo=repo,
        authority_repo=repo,
        source_graph_targets=(),
        session_topic="fix-batch-11",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        quality_review_packet_path=packet_file,
    )


def _mcp_packet(tmp_path: Path, reviewed: bytes) -> tuple[Path, Path]:
    worktree = tmp_path / "candidate"
    (worktree / "src").mkdir(parents=True)
    (worktree / PATH).write_bytes(reviewed)
    packet = quality_reviewer.build_review_packet(
        request_id="req1",
        task_id="task1",
        claim_epoch=1,
        worker_provider="adapter-a",
        changed_path_hashes={PATH: hashlib.sha256(reviewed).hexdigest()},
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    packet_file = runtime / "packet.json"
    packet_file.write_text(json.dumps(packet), encoding="utf-8")
    return worktree, packet_file


def test_mcp_packet_read_verifies_old_candidate_from_sealed_snapshot(tmp_path: Path) -> None:
    from aiworkhub import worker_ai_tools_mcp

    reviewed = b"def f():\n    return 1\n"
    worktree, packet_file = _mcp_packet(tmp_path, reviewed)

    # Launch: the live bytes match, so the request's snapshot is sealed.
    fresh = worker_ai_tools_mcp.quality_review_packet_read(_mcp_ctx(worktree, packet_file))
    assert fresh["ok"] is True, fresh
    sealed = quality_reviewer.sealed_review_candidate_root(packet_file, "req1")
    assert (sealed / PATH).read_bytes() == reviewed

    # The live worktree moves on: the old review_ready candidate still verifies.
    (worktree / PATH).write_bytes(b"def f():\n    return 2\n")
    old = worker_ai_tools_mcp.quality_review_packet_read(_mcp_ctx(worktree, packet_file))
    assert old["ok"] is True, old
    assert old["packet_sha256"] == fresh["packet_sha256"]

    # A tampered sealed snapshot still fails closed with the stable code.
    (sealed / PATH).write_bytes(b"tampered\n")
    tampered = worker_ai_tools_mcp.quality_review_packet_read(_mcp_ctx(worktree, packet_file))
    assert tampered["ok"] is False
    assert tampered["reason"] == "quality_review_candidate_hash_mismatch"

    # An unresolvable candidate root fails closed with a named refusal.
    missing = worker_ai_tools_mcp.quality_review_packet_read(
        _mcp_ctx(tmp_path / "absent", packet_file)
    )
    assert missing == {
        "ok": False,
        "tool": "quality_review_packet_read",
        "reason": "quality_review_candidate_root_unresolved",
    }


def test_mcp_packet_read_without_seal_refuses_drifted_worktree(tmp_path: Path) -> None:
    from aiworkhub import worker_ai_tools_mcp

    worktree, packet_file = _mcp_packet(tmp_path, b"def f():\n    return 1\n")
    (worktree / PATH).write_bytes(b"def f():\n    return 2\n")
    stale = worker_ai_tools_mcp.quality_review_packet_read(_mcp_ctx(worktree, packet_file))
    assert stale["ok"] is False
    assert stale["reason"] == "quality_review_candidate_hash_mismatch"
    # Nothing was sealed from bytes the packet does not authenticate.
    assert not quality_reviewer.sealed_review_candidate_root(packet_file, "req1").exists()


# --- review round: performance blockers need a measuring check with a metric.


def _perf_packet(check_id: str, **row: str) -> dict:
    packet = _findings_packet(checks={check_id: "failed"})
    packet["mechanical_checks"][0].update(row)
    return packet


def test_performance_blocker_check_anchor_needs_measuring_check_with_metric() -> None:
    def finding(check_id: str) -> dict:
        return {
            "severity": "high",
            "summary": "Quadratic slowdown in the hot path",
            "evidence": f"flagged by {check_id}",
            "check_id": check_id,
        }

    # Any failed check (a lint) no longer admits a performance blocker.
    with pytest.raises(ReviewerEvidenceError, match="performance_metric_required"):
        quality_reviewer.normalize_packet_findings(
            _perf_packet("lint:ruff", output="took 1200 ms"),
            lens="code_quality",
            findings=[finding("lint:ruff")],
        )
    # A failed benchmark whose output carries no measured metric: refused.
    with pytest.raises(ReviewerEvidenceError, match="performance_metric_required"):
        quality_reviewer.normalize_packet_findings(
            _perf_packet("bench:hot_loop", output="regressed"),
            lens="code_quality",
            findings=[finding("bench:hot_loop")],
        )
    # A failed benchmark with a measured metric in its own output: admitted.
    assert quality_reviewer.normalize_packet_findings(
        _perf_packet("bench:hot_loop", output="p95 1200 ms vs 300 ms budget"),
        lens="code_quality",
        findings=[finding("bench:hot_loop")],
    )
    # The kind may name it too: a failed test check with a metric is admitted.
    assert quality_reviewer.normalize_packet_findings(
        _perf_packet("ci:case_7", kind="pytest", stdout_tail="4000 queries issued"),
        lens="code_quality",
        findings=[finding("ci:case_7")],
    )


def test_performance_check_anchor_is_not_admitted_by_evidence_text_metric() -> None:
    # An unmeasured check anchor cannot borrow a metric from the finding text.
    finding = {
        "severity": "high",
        "summary": "Quadratic slowdown in the hot path",
        "evidence": "flagged by lint:ruff, took 1200 ms",
        "check_id": "lint:ruff",
    }
    with pytest.raises(ReviewerEvidenceError, match="performance_metric_required"):
        quality_reviewer.normalize_packet_findings(
            _perf_packet("lint:ruff", output="regressed"),
            lens="code_quality",
            findings=[finding],
        )


def test_measuring_check_needs_an_exact_token_not_a_substring() -> None:
    for check_id in ("latest_attestation", "perfunctory_lint", "contest:lint"):
        assert not quality_reviewer._is_measuring_check("", check_id)
    assert quality_reviewer._is_measuring_check("", "bench:hot_loop")
    assert quality_reviewer._is_measuring_check("pytest", "ci:case_7")
    packet = _perf_packet("latest_attestation", output="took 1200 ms")
    assert quality_reviewer._measured_failed_checks(packet) == set()


def test_correctness_blocker_mentioning_slow_is_not_performance_gated() -> None:
    finding = {
        "severity": "high",
        "summary": "Slow path returns the wrong value",
        "evidence": f"{PATH}:12",
    }
    assert quality_reviewer.normalize_packet_findings(
        _findings_packet(), lens="correctness", findings=[finding]
    )
    # The same unmeasured claim under the performance-owning lens is refused.
    with pytest.raises(ReviewerEvidenceError, match="performance_metric_required"):
        quality_reviewer.normalize_packet_findings(
            _findings_packet(), lens="code_quality", findings=[finding]
        )


# --- review round: POSIX descriptor binding considers only descriptors SQLite opened.


def test_posix_descriptor_binding_ignores_preexisting_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiworkhub import platform_io

    tool_recipes_store.initialize_repository(tmp_path)
    db = tool_recipes_store._db_path(tmp_path)
    identity = roadmap_store._store_path_identity(tmp_path.absolute(), db.absolute())
    unrelated = (identity[0], identity[1] + 99)
    monkeypatch.setattr(platform_io, "is_windows", lambda: False)

    def snapshots(before: dict, after: dict):
        calls = iter((before, after))
        monkeypatch.setattr(roadmap_store, "_open_descriptor_identities", lambda: next(calls))

    # An fd the process ALREADY held on the store inode, and SQLite opened no
    # matching descriptor: formerly a false accept, now refused.
    snapshots({7: identity}, {7: identity, 9: unrelated})
    with pytest.raises(roadmap_store.StorePathAuthenticationError, match="store_path_replaced"):
        tool_recipes_store.list_recipes(tmp_path)

    # An unrelated pre-existing descriptor does not disturb a genuine open.
    snapshots({7: unrelated}, {7: unrelated, 9: identity})
    assert tool_recipes_store.list_recipes(tmp_path) == []

    # A reused descriptor number now naming the store counts as newly opened.
    snapshots({7: unrelated}, {7: identity})
    assert tool_recipes_store.list_recipes(tmp_path) == []

    snapshots({}, None)
    with pytest.raises(
        roadmap_store.StorePathAuthenticationError, match="store_descriptor_identity_unavailable"
    ):
        tool_recipes_store.list_recipes(tmp_path)
