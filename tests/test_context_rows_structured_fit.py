from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import project_context, worker_ai_tools_mcp  # noqa: E402

TOOL_CAP = 8 * 1024
SECTION_CAP = project_context.TOOL_CAPS["ai_memory"]["bytes"]


def _commit(index: int) -> str:
    return f"{index:02d}" + "ab" * 31


def _learning_commit(index: int) -> str:
    return json.dumps(
        {
            "commit_id": _commit(index),
            "lesson": f"lesson {index} " + "word " * 180,
            "outcome": "accepted",
            "schema_id": "aiworkhub.learning_commit.v1",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _memory_payload() -> dict:
    results = [
        {"key": f"learning.m{i}", "scope": "project", "tags": "learning_commit", "value": _learning_commit(i)}
        for i in range(5)
    ]
    return {"results": results, "count": len(results), "match_semantics": "any_token_ranked_by_coverage"}


def _kb_payload() -> dict:
    # Row 0 carries the learning commit that ai_memory row 0 already returned.
    commits = [0, 10, 11, 12, 13]
    results = [
        {"key": f"kb.k{i}", "title": "t", "category": "verified_project_invariant",
         "tags": "learning_commit", "body": _learning_commit(i)}
        for i in commits
    ]
    return {"results": results, "count": len(results), "match_semantics": "any_token_ranked_by_coverage"}


def _tool_reply(payload: dict) -> dict:
    text, truncated, kept = worker_ai_tools_mcp.fit_result_rows(payload, TOOL_CAP)
    return {"ok": True, "content": text, "truncated": truncated, "hit_count": kept}


def _data(evidence):
    return evidence["data"] if "data" in evidence else evidence


def _byte_cut_baseline(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return worker_ai_tools_mcp._bounded_text(text, SECTION_CAP)[0]


def test_fit_result_rows_drops_trailing_whole_rows() -> None:
    payload = _memory_payload()
    payload["results"] = payload["results"] * 2
    payload["count"] = 10
    text, truncated, kept = worker_ai_tools_mcp.fit_result_rows(payload, TOOL_CAP)
    fitted = json.loads(text)
    assert len(text.encode("utf-8")) <= TOOL_CAP
    assert truncated is True
    assert kept == len(fitted["results"]) == fitted["count"] < 10
    assert fitted["results_omitted"] == 10 - kept
    assert fitted["results"] == payload["results"][:kept]


def test_fit_result_rows_untouched_when_it_fits() -> None:
    payload = {"results": [{"key": "a", "value": "b"}], "count": 1}
    text, truncated, kept = worker_ai_tools_mcp.fit_result_rows(payload, TOOL_CAP)
    assert (json.loads(text), truncated, kept) == (payload, False, 1)


def test_fit_result_rows_byte_cuts_only_when_one_row_alone_exceeds_cap() -> None:
    payload = {"results": [{"key": "a", "value": "x" * 600}, {"key": "b", "value": "y" * 600}], "count": 2}
    text, truncated, kept = worker_ai_tools_mcp.fit_result_rows(payload, 400)
    assert truncated is True
    assert kept == 1
    assert len(text.encode("utf-8")) <= 400
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)


def test_bundle_carries_complete_rows_as_objects_and_dedups_kb(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        project_context, "_source_graph_direct",
        lambda _repo, contract: (json.dumps({"tool": "source_graph", "query": contract["source_graph"]["query"]}), False),
    )
    monkeypatch.setattr(
        project_context._worker_tools, "session_current_state",
        lambda ctx, *, limit=12: {
            "ok": True, "truncated": False, "hit_count": 1,
            "content": json.dumps({"topic": "t", "state": "current", "evidence": [{"source_id": "e"}]}),
        },
    )
    memory_payload, kb_payload = _memory_payload(), _kb_payload()
    monkeypatch.setattr(
        project_context._worker_tools, "ai_memory_search",
        lambda ctx, *, query, limit=8: _tool_reply(memory_payload),
    )
    monkeypatch.setattr(
        project_context._worker_tools, "kb_search",
        lambda ctx, *, query, limit=8: _tool_reply(kb_payload),
    )
    card = {
        "task_id": "TASK_ROWS", "runner": "claude_worker_ctx", "topic": "task_mcp",
        "status": "pending", "worker_status": "unclaimed", "claimed_by": "",
        "allowed_writes": ["out/result.json"],
        "project_context": {
            "required": True,
            "source_graph": {"mode": "focus", "query": "ProcessManager", "budget": 32, "bundle_type": "explore"},
            "session": {"topic": "rows", "limit": 3},
            "ai_memory": {"query": "rows", "limit": 5},
            "kb": {"query": "rows", "limit": 5},
        },
    }

    result = project_context.collect_project_context(repo, card)
    assert result is not None
    payload = json.loads(result.prompt_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1])
    memory = _data(payload["evidence"]["ai_memory"])
    kb = _data(payload["evidence"]["kb"])

    assert isinstance(memory, dict) and isinstance(kb, dict)
    assert 0 < len(memory["results"]) < 5
    assert memory["results_omitted"] == 5 - len(memory["results"])
    assert memory["count"] == len(memory["results"])
    for row in memory["results"]:
        assert isinstance(row["value"], dict)
        assert row["value"]["schema_id"] == "aiworkhub.learning_commit.v1"
    memory_ids = {row["value"]["commit_id"] for row in memory["results"]}
    assert _commit(0) in memory_ids

    assert kb["duplicates_omitted"] == 1
    assert kb["results_omitted"] == 4 - len(kb["results"])
    for row in kb["results"]:
        assert isinstance(row["body"], dict)
        assert row["body"]["commit_id"] not in memory_ids

    hits = {section["name"]: section["hit_count"] for section in result.metadata["sections"]}
    assert hits["ai_memory"] == len(memory["results"])
    assert hits["kb"] == len(kb["results"])

    baseline = json.dumps(
        {"ai_memory": _byte_cut_baseline(memory_payload), "kb": _byte_cut_baseline(kb_payload)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    structured = json.dumps(
        {"ai_memory": payload["evidence"]["ai_memory"], "kb": payload["evidence"]["kb"]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    baseline_bytes, structured_bytes = len(baseline.encode("utf-8")), len(structured.encode("utf-8"))
    print(f"byte_cut_baseline_bytes={baseline_bytes} structured_bytes={structured_bytes}")
    assert structured_bytes < baseline_bytes, (baseline_bytes, structured_bytes)
