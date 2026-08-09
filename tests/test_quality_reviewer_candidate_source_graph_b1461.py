from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import process_launcher
from aiworkhub import quality_reviewer
from aiworkhub import source_graph
from aiworkhub import worker_ai_tools_mcp as worker_tools


def _ctx(
    runtime: Path,
    *,
    repo: Path,
    authority_repo: Path,
    packet_path: Path | None,
) -> worker_tools.WorkerToolContext:
    runtime.mkdir(parents=True, exist_ok=True)
    ledger = runtime / "audit.jsonl"
    ledger.write_text("", encoding="utf-8")
    key = runtime / "audit.key"
    key.write_bytes(b"k" * 32)
    return worker_tools.WorkerToolContext(
        task_id="REVIEW_TASK_1" if packet_path else "WORKER_TASK_1",
        runner="claude_sonnet5" if packet_path else "codex_worker",
        topic="quality_review" if packet_path else "implementation",
        request_id="b" * 32,
        repo=repo,
        authority_repo=authority_repo,
        source_graph_targets=(),
        session_topic="quality_review",
        audit_ledger_path=ledger,
        audit_hmac_key_path=key,
        quality_review_packet_path=packet_path,
    )


def test_ordinary_worker_source_graph_remains_canonical(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "worker"
    authority = tmp_path / "canonical"
    repo.mkdir()
    authority.mkdir()
    db = tmp_path / "canonical.sqlite"
    db.write_bytes(b"not-empty")
    ctx = _ctx(tmp_path / "runtime", repo=repo, authority_repo=authority, packet_path=None)
    monkeypatch.setattr(
        worker_tools,
        "_resolve_source_graph_db",
        lambda _ctx: worker_tools.AuthorityBinding(
            db_path=db,
            authority_source="canonical",
            authority_state="sole_authority",
            authority_repo=authority,
        ),
    )
    observed: list[Path] = []
    monkeypatch.setattr(
        source_graph,
        "focus",
        lambda root, query, budget: observed.append(root) or {"matches": [{"name": query}]},
    )
    worker_tools._CACHE.clear()

    result = worker_tools.source_graph_query(ctx, mode="focus", query="authority", budget=8)

    assert result["ok"] is True
    assert result["authority_source"] == "canonical"
    assert result["authority_repo"] == str(authority)
    assert observed == [authority]


def test_quality_reviewer_source_graph_uses_packet_bound_candidate_overlay(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()
    (canonical / "module.py").write_text("def canonical_only():\n    return 1\n", encoding="utf-8")
    candidate_file = candidate / "module.py"
    candidate_file.write_text(
        "def canonical_only():\n    return 1\n\n"
        "def candidate_only_symbol():\n    return 2\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    packet = quality_reviewer.build_review_packet(
        request_id="target-request-1",
        task_id="TARGET_TASK_1",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes={
            "module.py": hashlib.sha256(candidate_file.read_bytes()).hexdigest()
        },
    )
    packet_path = runtime / "quality_review_packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    ctx = _ctx(runtime, repo=candidate, authority_repo=canonical, packet_path=packet_path)
    worker_tools._CACHE.clear()

    result = worker_tools.source_graph_query(
        ctx,
        mode="function",
        query="candidate_only_symbol",
        budget=16,
    )

    assert result["ok"] is True
    assert result["hit_count"] > 0
    assert "candidate_only_symbol" in result["content"]
    assert result["authority_source"] == "candidate_overlay"
    assert result["authority_state"] == "quality_review_readonly"
    assert result["authority_repo"] == str(candidate.resolve())
    assert result["target_request_id"] == "target-request-1"
    assert result["target_task_id"] == "TARGET_TASK_1"
    assert result["packet_sha256"] == packet["packet_sha256"]

    audit = worker_tools.verify_audit_ledger(
        ctx.audit_ledger_path,
        ctx.audit_hmac_key_path,
        task_id=ctx.task_id,
        runner=ctx.runner,
        topic=ctx.topic,
        request_id=ctx.request_id,
    )
    assert audit["live_source_graph_calls"] == 1
    assert audit["authority_index_identity"] == [
        f"source_graph:candidate_overlay:quality_review_readonly:{candidate.resolve()}"
    ]


def test_review_packet_source_evidence_centers_nf3_late_changed_symbol(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()
    prefix = "# unchanged filler\n" * 260
    assert len(prefix.encode("utf-8")) > 4_000
    original = prefix + "def unchanged_tail():\n    return None\n"
    changed = (
        prefix
        + "def _v3_planned_outputs():\n"
        + "    return ['tests/test_quality_reviewer_candidate_source_graph_b1461.py']\n"
    )
    (canonical / "module.py").write_text(original, encoding="utf-8")
    candidate_file = candidate / "module.py"
    candidate_file.write_text(changed, encoding="utf-8")
    digest = hashlib.sha256(candidate_file.read_bytes()).hexdigest()
    manager = SimpleNamespace(
        repo=canonical,
        _QUALITY_REVIEW_SOURCE_TOTAL_MAX_BYTES=60_000,
        _QUALITY_REVIEW_SOURCE_MAX_BYTES=4_000,
        _QUALITY_REVIEW_SOURCE_CONTEXT_LINES=3,
    )
    workspace = SimpleNamespace(path=candidate)

    evidence = process_launcher.ProcessManager._quality_review_source_evidence(
        manager, workspace, {"module.py": digest}
    )
    packet = quality_reviewer.build_review_packet(
        request_id="target-request-1",
        task_id="TARGET_TASK_1",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes={"module.py": digest},
        source_evidence=evidence,
    )
    row = packet["candidate"]["source_evidence"][0]

    filler_count = row["excerpt"].count("# unchanged filler")
    assert 0 < filler_count <= manager._QUALITY_REVIEW_SOURCE_CONTEXT_LINES
    assert "_v3_planned_outputs" in row["excerpt"][:200]
    assert (
        "tests/test_quality_reviewer_candidate_source_graph_b1461.py" in row["excerpt"]
    )
    assert row["segments"][0]["candidate_start_line"] > 250
    assert row["candidate_sha256"] == digest


def test_review_packet_many_nf3_hunks_stays_per_path_bounded(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    candidate.mkdir()
    baseline_lines: list[str] = []
    candidate_lines: list[str] = []
    changed_hunks = 80
    for hunk_index in range(changed_hunks):
        baseline_lines.append(f"def unchanged_block_{hunk_index}():\n")
        baseline_lines.append(f"    return {hunk_index}\n")
        candidate_lines.append(f"def unchanged_block_{hunk_index}():\n")
        candidate_lines.append(f"    return {hunk_index}\n")
        baseline_lines.append(f"NF3_VALUE_{hunk_index} = 'old'\n")
        candidate_lines.append(
            f"NF3_VALUE_{hunk_index} = '_v3_planned_outputs_{hunk_index}'\n"
        )
        for filler_index in range(8):
            filler = f"# stable spacer {hunk_index}:{filler_index}\n"
            baseline_lines.append(filler)
            candidate_lines.append(filler)
    (canonical / "module.py").write_text("".join(baseline_lines), encoding="utf-8")
    candidate_file = candidate / "module.py"
    candidate_file.write_text("".join(candidate_lines), encoding="utf-8")
    digest = hashlib.sha256(candidate_file.read_bytes()).hexdigest()
    manager = SimpleNamespace(
        repo=canonical,
        _QUALITY_REVIEW_SOURCE_TOTAL_MAX_BYTES=60_000,
        _QUALITY_REVIEW_SOURCE_MAX_BYTES=4_000,
        _QUALITY_REVIEW_SOURCE_CONTEXT_LINES=3,
    )
    workspace = SimpleNamespace(path=candidate)

    evidence = process_launcher.ProcessManager._quality_review_source_evidence(
        manager, workspace, {"module.py": digest}
    )
    packet = quality_reviewer.build_review_packet(
        request_id="target-request-1",
        task_id="TARGET_TASK_1",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes={"module.py": digest},
        source_evidence=evidence,
    )
    row = packet["candidate"]["source_evidence"][0]
    omitted = int(row["omission_reason"].removeprefix("changed_hunks_omitted:"))

    assert packet["packet_sha256"]
    assert row["candidate_sha256"] == digest
    assert row["excerpt_bytes"] <= manager._QUALITY_REVIEW_SOURCE_MAX_BYTES
    assert (
        len(row["excerpt"].encode("utf-8"))
        <= manager._QUALITY_REVIEW_SOURCE_MAX_BYTES
    )
    assert sum(segment["excerpt_bytes"] for segment in row["segments"]) == row[
        "excerpt_bytes"
    ]
    assert row["segments"][-1]["truncated"] is True
    assert omitted >= changed_hunks - len(row["segments"])
    assert omitted > 0


@pytest.mark.skipif(os.name == "nt", reason="Windows forbids control characters in paths")
def test_review_packet_escapes_control_character_path_in_hunk_header(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    raw_path = "dir/weird\n@@ injected\x1f.py"
    baseline_file = canonical / raw_path
    candidate_file = candidate / raw_path
    baseline_file.parent.mkdir(parents=True)
    candidate_file.parent.mkdir(parents=True)
    baseline_file.write_text("VALUE = 'old'\n", encoding="utf-8")
    candidate_file.write_text("VALUE = '_v3_planned_outputs'\n", encoding="utf-8")
    digest = hashlib.sha256(candidate_file.read_bytes()).hexdigest()
    manager = SimpleNamespace(
        repo=canonical,
        _QUALITY_REVIEW_SOURCE_TOTAL_MAX_BYTES=60_000,
        _QUALITY_REVIEW_SOURCE_MAX_BYTES=4_000,
        _QUALITY_REVIEW_SOURCE_CONTEXT_LINES=3,
    )
    workspace = SimpleNamespace(path=candidate)

    evidence = process_launcher.ProcessManager._quality_review_source_evidence(
        manager, workspace, {raw_path: digest}
    )
    packet = quality_reviewer.build_review_packet(
        request_id="target-request-1",
        task_id="TARGET_TASK_1",
        claim_epoch=1,
        worker_provider="codex_cli",
        changed_path_hashes={raw_path: digest},
        source_evidence=evidence,
    )
    row = packet["candidate"]["source_evidence"][0]

    assert row["path"] == raw_path
    assert row["candidate_sha256"] == digest
    assert 'path:"dir/weird\\n@@ injected\\u001f.py"' in row["excerpt"]
    assert raw_path not in row["excerpt"]
    assert "\n@@ injected" not in row["excerpt"]
