from __future__ import annotations

import hashlib
import json
from pathlib import Path

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
