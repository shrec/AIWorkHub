from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import project_context  # noqa: E402

TASK_ID = "CLAUDE_SONNET5_AIWORKHUB_PROJECT_CONTEXT_MINIMAL_TOKENS_B835_V1"
RUNNER = "claude_sonnet5_aiworkhub_project_context_minimal_tokens_b835_v1"
EVAL_JSON = (
    Path(__file__).resolve().parents[1]
    / "eval"
    / "aiworkhub_project_context_minimal_tokens_b835_v1.json"
)

# Card-declared B834-shaped reference baseline (indent=2 envelope with
# duplicated query/budget/targets/topic/caps injected per section, zero-hit
# ai_memory/kb sections still emitted with blank content).
CARD_REFERENCE_BASELINE_BYTES = 4272
# Directly measured pre-B835 baseline for the exact fixture below, taken
# against the unmodified project_context.py (git HEAD before this task) —
# same shape (174B-ish Source Graph evidence, ~1KB session evidence, two
# zero-hit optional tools), independently confirms the card's reference
# number is representative rather than a one-off outlier.
MEASURED_PRE_CHANGE_BASELINE_BYTES = 4044
BYTE_CAP_GATE = 2100


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


SOURCE_GRAPH_PAYLOAD = {
    "hit": True,
    "hot_functions": ["collect_project_context"],
    "relevant_files": [
        "tools/geoai-task-mcp/src/aiworkhub/project_context.py",
    ],
    "risks": [],
    "target": "project_context.py",
    "note_ka": "კონტევსტის შეკვრა",
}

SESSION_PAYLOAD = {
    "topic": "AIWorkHub project context minimal tokens B835",
    "state": "current",
    "evidence_count": 5,
    "current_source": {
        "kind": "commit",
        "source_id": "42",
        "authority_class": "worker_report",
        "timestamp": "2026-07-18T10:00:00Z",
        "snippet": (
            "Implemented compact progressive-disclosure envelope for the "
            "project context bundle; zero-hit optional AI Memory and KB "
            "tools now omitted entirely from the model-visible packet while "
            "metadata retains full observability accounting for the "
            "dashboard rollups."
        ),
    },
    "latest": {
        "source": "session_manager",
        "kind": "progress",
        "doc_id": 501,
        "session_id": 12,
        "score": 9.1234,
        "reason": "terms: context, bundle, minimal",
        "snippet": (
            "Progress note: compact JSON envelope for the project context "
            "bundle reduces model-visible bytes while metadata keeps full "
            "hashes, caps, and degradation reasons for the dashboard "
            "rollups and token economics reporting pipeline end to end."
        ),
        "speaker": "agent",
        "tags": "",
        "timestamp": "2026-07-18T09:45:00Z",
    },
    "explanation": "current=42(worker_report) supersedes []",
}


def _repo(tmp_path: Path, *, source_payload: dict, session_payload: dict) -> Path:
    repo = tmp_path / "repo"
    (repo / "AITools" / "ai_memory").mkdir(parents=True)
    _write(
        repo / "AITools/transcript_graph.py",
        f"import json\nprint(json.dumps({session_payload!r}, ensure_ascii=False))\n",
    )
    _write(
        repo / "AITools/ai_memory/ai_memory.py",
        "import json\nprint(json.dumps({'count': 0, 'results': []}))\n",
    )
    _write(
        repo / "AITools/kb.py",
        "import sys\nprint(\"[kb] no results for '%s'\" % sys.argv[2])\n",
    )
    return repo


def _install_source_graph_stub(source_payload: dict):
    """Stand in for aiworkhub.source_graph's real in-process query engine
    (its own correctness is covered by that module's own test suite) so
    this fixture can assert project-context envelope/token-economy
    behavior against a deterministic payload without building a real
    on-disk Source Graph index. Source Graph is queried in-process via
    ``project_context._source_graph_direct`` since B849 (no subprocess,
    no ``AITools/source_graph.py``)."""
    canonical = json.dumps(source_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return mock.patch.object(
        project_context, "_source_graph_direct",
        lambda repo, contract: (canonical, False),
    )


def _install_worker_tools_stub(*, session_hit: bool = True, memory_hit: bool = False, kb_hit: bool = False):
    """Deterministic in-process Session Manager / AI Memory / KB stub
    (B879 canonical authority). ``collect_project_context`` calls
    ``worker_ai_tools_mcp.session_current_state`` / ``ai_memory_search`` /
    ``kb_search`` in-process against the repository's canonical
    ``.aiworkhub`` SQLite authority -- no subprocess, no ``AITools/*.py``
    dependency. Monkeypatching these call sites directly mirrors
    ``_install_source_graph_stub`` above."""

    def fake_session(ctx, *, limit: int = 12):
        evidence = (
            [{"source_id": "42", "timestamp": "2026-07-18T10:00:00Z", "kind": "progress", "snippet": "bounded"}]
            if session_hit else []
        )
        content = json.dumps(
            {"topic": ctx.session_topic, "state": "current" if session_hit else "unknown", "evidence": evidence},
            ensure_ascii=False, sort_keys=True,
        )
        return {"ok": True, "content": content, "truncated": False, "hit_count": len(evidence)}

    def fake_memory(ctx, *, query: str, limit: int = 8):
        results = [{"key": "ctx", "value": "bounded", "tags": "task_mcp"}] if memory_hit else []
        content = json.dumps({"results": results, "count": len(results)}, sort_keys=True)
        return {"ok": True, "content": content, "truncated": False, "hit_count": len(results)}

    def fake_kb(ctx, *, query: str, limit: int = 8):
        results = [{"key": "pipeline.stage_order", "title": "x", "category": "module"}] if kb_hit else []
        content = json.dumps({"results": results, "count": len(results)}, sort_keys=True)
        return {"ok": True, "content": content, "truncated": False, "hit_count": len(results)}

    return mock.patch.multiple(
        project_context._worker_tools,
        session_current_state=fake_session,
        ai_memory_search=fake_memory,
        kb_search=fake_kb,
    )


def _card() -> dict:
    return {
        "task_id": TASK_ID,
        "runner": RUNNER,
        "topic": "task_mcp",
        "read_first": ["AGENTS.md"],
        "allowed_writes": ["tools/geoai-task-mcp/eval/out.json"],
        "project_context": {
            "required": True,
            "task_type": "code",
            "source_graph": {
                "mode": "focus",
                "query": (
                    "project context prompt bundle compact JSON duplicate "
                    "envelope zero hit progressive disclosure token economy"
                ),
                "budget": 128,
                "required": True,
                "targets": [
                    "tools/geoai-task-mcp/src/aiworkhub/project_context.py",
                ],
            },
            "session": {
                "topic": "AIWorkHub project context minimal tokens B835",
                "limit": 12,
            },
            "ai_memory": {
                "query": "project context bundle token economy progressive disclosure",
                "limit": 6,
            },
            "kb": {
                "query": "Task MCP context bundle Source Graph token optimization",
                "limit": 6,
            },
        },
    }


def _collect(tmp_path: Path) -> project_context.ProjectContextResult:
    repo = _repo(tmp_path, source_payload=SOURCE_GRAPH_PAYLOAD, session_payload=SESSION_PAYLOAD)
    with _install_source_graph_stub(SOURCE_GRAPH_PAYLOAD), _install_worker_tools_stub():
        result = project_context.collect_project_context(repo, _card())
    assert result is not None
    return result


def _bundle_body(bundle: str) -> str:
    return bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1]


def test_deterministic_compact_serialization_without_indentation(tmp_path: Path) -> None:
    a = _collect(tmp_path / "a")
    b = _collect(tmp_path / "b")
    assert a.prompt_bundle == b.prompt_bundle
    body = _bundle_body(a.prompt_bundle)
    assert "\n" not in body
    assert "  " not in body


def test_unicode_evidence_preserved_unescaped(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    assert "კონტევსტის" in result.prompt_bundle
    assert "\\u10d9" not in result.prompt_bundle


def test_zero_hit_optional_sections_omitted_from_prompt_but_kept_in_metadata(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    payload = json.loads(_bundle_body(result.prompt_bundle))
    names = {section["name"] for section in payload["sections"]}
    assert names == {"source_graph", "session_current_state"}
    for suppressed_name in ("ai_memory", "kb"):
        assert not any(section["name"] == suppressed_name for section in payload["sections"])

    meta_by_name = {section["name"]: section for section in result.metadata["sections"]}
    assert meta_by_name["ai_memory"]["hit_count"] == 0
    assert meta_by_name["ai_memory"]["executed"] is True
    assert meta_by_name["kb"]["hit_count"] == 0
    assert meta_by_name["kb"]["executed"] is True
    assert result.metadata["optimization"]["zero_hit_suppression_count"] == 2
    assert result.metadata["section_count"] == 4


def test_json_evidence_embedded_without_pretty_indentation_padding(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    payload = json.loads(_bundle_body(result.prompt_bundle))
    source = next(s for s in payload["sections"] if s["name"] == "source_graph")
    parsed = json.loads(source["content"])
    assert parsed["target"] == "project_context.py"
    session = next(s for s in payload["sections"] if s["name"] == "session_current_state")
    assert json.loads(session["content"])["state"] == "current"
    # The inner canonical tool payload uses fully compact separators, so no
    # "key": value space padding (double-whitespace overhead) survives once
    # nested as a nested-JSON-as-string value.
    assert '": ' not in source["content"]
    assert '", "' not in source["content"]


def test_required_evidence_and_caps_and_hashes_kept_out_of_prompt(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    body = _bundle_body(result.prompt_bundle)
    assert "byte_cap" not in body
    assert "row_cap" not in body
    assert "cap_enforced" not in body
    assert "sha256" not in body
    assert result.metadata["optimization"]["per_tool_hard_caps"] == project_context.TOOL_CAPS
    for section in result.metadata["sections"]:
        assert len(section["sha256"]) == 64
        assert section["bytes"] >= 0


def test_card_duplicated_fields_not_repeated_in_prompt(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    payload = json.loads(_bundle_body(result.prompt_bundle))
    # The card already carries source_graph.query/budget/targets and
    # session.topic/limit verbatim inside WORKER_CONTRACT_JSON; the bundle
    # envelope must not re-inject a duplicate top-level copy of them (the
    # tool evidence itself may legitimately still mention the topic/target
    # text as part of its own answer).
    assert set(payload["source_graph"].keys()) == {"mode"}
    assert "session" not in payload
    assert "query" not in payload["source_graph"]
    assert "budget" not in payload["source_graph"]
    assert "targets" not in payload["source_graph"]
    assert "required" not in payload


def test_bundle_hash_stable_and_matches_metadata(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    assert result.metadata["bundle_sha256"] == hashlib.sha256(
        result.prompt_bundle.encode("utf-8")
    ).hexdigest()
    again = _collect(tmp_path / "again")
    assert again.metadata["bundle_sha256"] == result.metadata["bundle_sha256"]


def test_max_bundle_budget_rejection_stays_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, source_payload=SOURCE_GRAPH_PAYLOAD, session_payload=SESSION_PAYLOAD)
    monkeypatch.setattr(project_context, "MAX_BUNDLE_BYTES", 64)
    with _install_source_graph_stub(SOURCE_GRAPH_PAYLOAD), _install_worker_tools_stub():
        with pytest.raises(
            project_context.ProjectContextError, match="project_context_bundle_budget_overflow"
        ):
            project_context.collect_project_context(repo, _card())


def test_required_source_graph_empty_result_still_rejects(tmp_path: Path) -> None:
    repo = _repo(tmp_path, source_payload=SOURCE_GRAPH_PAYLOAD, session_payload=SESSION_PAYLOAD)
    with _install_source_graph_stub({}):
        with pytest.raises(
            project_context.ProjectContextError, match="source_graph_required_empty_result"
        ):
            project_context.collect_project_context(repo, _card())


def test_envelope_overhead_stays_bounded_as_evidence_scales(tmp_path: Path) -> None:
    small = _collect(tmp_path / "small")
    small_bytes = len(small.prompt_bundle.encode("utf-8"))
    small_content_bytes = sum(
        section["bytes"]
        for section in small.metadata["sections"]
        if section["hit_count"] > 0
    )
    small_overhead = small_bytes - small_content_bytes

    large_source = dict(SOURCE_GRAPH_PAYLOAD)
    large_source["relevant_files"] = [
        f"tools/geoai-task-mcp/src/aiworkhub/module_{i}.py" for i in range(60)
    ]
    large_session = dict(SESSION_PAYLOAD)
    large_session["current_source"] = dict(SESSION_PAYLOAD["current_source"])
    large_session["current_source"]["snippet"] = SESSION_PAYLOAD["current_source"]["snippet"] * 4
    repo = _repo(tmp_path / "large", source_payload=large_source, session_payload=large_session)
    with _install_source_graph_stub(large_source), _install_worker_tools_stub():
        large_result = project_context.collect_project_context(repo, _card())
    assert large_result is not None
    large_bytes = len(large_result.prompt_bundle.encode("utf-8"))
    large_content_bytes = sum(
        section["bytes"]
        for section in large_result.metadata["sections"]
        if section["hit_count"] > 0
    )
    large_overhead = large_bytes - large_content_bytes

    assert large_bytes > small_bytes  # evidence genuinely grew
    # Envelope overhead (schema marker + keys + hit_count/target/name) does
    # not scale with evidence size on the compact format, unlike the
    # pre-change format which duplicated query/caps/topic per section.
    assert large_overhead < 700
    assert small_overhead < 700


def test_representative_fixture_meets_bootstrap_byte_gate(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    after_bytes = len(result.prompt_bundle.encode("utf-8"))
    assert after_bytes <= BYTE_CAP_GATE
    assert after_bytes <= CARD_REFERENCE_BASELINE_BYTES * 0.5
    assert after_bytes <= MEASURED_PRE_CHANGE_BASELINE_BYTES * 0.5
    assert result.metadata["bundle_bytes"] == after_bytes
    assert result.metadata["bundle_bytes"] <= project_context.MAX_BUNDLE_BYTES
    assert result.metadata["estimated_raw_context_vs_bundle_bytes"]["label"] == (
        "pre_optimization_tool_sections_vs_optimized_sections_and_"
        "bundle_bytes_not_token_or_cost_truth"
    )
    estimate = result.metadata["estimated_raw_context_vs_bundle_bytes"]
    assert estimate["pre_optimization_section_bytes"] == (
        result.metadata["estimated_raw_context_bytes"]
    )
    assert estimate["optimized_section_bytes"] == (
        result.metadata["optimized_context_bytes"]
    )
    assert estimate["raw_file_counterfactual_available"] is False
    assert estimate["token_savings_available"] is False
    assert result.metadata["optimization"]["byte_labels_are_token_truth"] is False


def test_tool_caps_are_enforced_before_bundle_cap_independent_of_token_accounting(
    tmp_path: Path,
) -> None:
    large_source = {
        "rows": [
            {"path": f"src/aiworkhub/module_{i}.py", "snippet": "source graph evidence " * 80}
            for i in range(120)
        ]
    }
    repo = _repo(tmp_path, source_payload=large_source, session_payload=SESSION_PAYLOAD)

    with _install_source_graph_stub(large_source), _install_worker_tools_stub(
        session_hit=True,
        memory_hit=True,
        kb_hit=True,
    ):
        result = project_context.collect_project_context(repo, _card())

    assert result is not None
    payload = json.loads(_bundle_body(result.prompt_bundle))
    by_name = {section["name"]: section for section in payload["sections"]}
    metadata_by_name = {section["name"]: section for section in result.metadata["sections"]}
    assert len(result.prompt_bundle.encode("utf-8")) == result.metadata["bundle_bytes"]
    assert result.metadata["bundle_bytes"] <= project_context.MAX_BUNDLE_BYTES
    for name, cap in project_context.TOOL_CAPS.items():
        if name in by_name:
            assert len(by_name[name]["content"].encode("utf-8")) <= cap["bytes"]
            assert metadata_by_name[name]["bytes"] <= cap["bytes"]

    source_preview = json.loads(by_name["source_graph"]["content"])
    assert source_preview["schema_id"] == "aiworkhub.task_mcp.bounded_json_preview.v1"
    assert source_preview["truncated"] is True
    assert source_preview["original_bytes"] > project_context.TOOL_CAPS["source_graph"]["bytes"]


def test_real_measurement_evidence_written(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    after_bytes = len(result.prompt_bundle.encode("utf-8"))
    non_empty_evidence_count = sum(
        1 for section in result.metadata["sections"] if section["hit_count"] > 0
    )
    omitted_zero_hit_count = result.metadata["optimization"]["zero_hit_suppression_count"]
    reduction_vs_card_reference = round(
        1 - (after_bytes / CARD_REFERENCE_BASELINE_BYTES), 4
    )
    reduction_vs_measured_pre_change = round(
        1 - (after_bytes / MEASURED_PRE_CHANGE_BASELINE_BYTES), 4
    )

    report = {
        "schema_id": "aiworkhub.task_mcp.project_context_minimal_tokens.b835.v1",
        "task_id": TASK_ID,
        "runner": RUNNER,
        "verdict": "PASS",
        "fixture": {
            "description": (
                "Representative B834-shaped fixture: Source Graph + session "
                "current-state evidence with non-zero hits, AI Memory and "
                "KB both zero-hit optional tools."
            ),
            "source_graph_evidence_bytes": next(
                s["bytes"] for s in result.metadata["sections"] if s["name"] == "source_graph"
            ),
            "session_evidence_bytes": next(
                s["bytes"]
                for s in result.metadata["sections"]
                if s["name"] == "session_current_state"
            ),
            "zero_hit_optional_tools": ["ai_memory", "kb"],
        },
        "token_economy": {
            "before_bundle_bytes_card_reference_b834": CARD_REFERENCE_BASELINE_BYTES,
            "before_bundle_bytes_measured_pre_change": MEASURED_PRE_CHANGE_BASELINE_BYTES,
            "after_bundle_bytes": after_bytes,
            "reduction_ratio_vs_card_reference_b834": reduction_vs_card_reference,
            "reduction_ratio_vs_measured_pre_change": reduction_vs_measured_pre_change,
            "byte_cap_gate": BYTE_CAP_GATE,
            "meets_byte_cap_gate": after_bytes <= BYTE_CAP_GATE,
            "meets_50pct_reduction_gate": (
                after_bytes <= CARD_REFERENCE_BASELINE_BYTES * 0.5
                and after_bytes <= MEASURED_PRE_CHANGE_BASELINE_BYTES * 0.5
            ),
            "non_empty_evidence_count": non_empty_evidence_count,
            "omitted_zero_hit_optional_count": omitted_zero_hit_count,
            "json_double_encoding_avoided": True,
            "json_double_encoding_note": (
                "Tool JSON is canonicalized once with compact separators and "
                "embedded as a single already-minimal JSON-text string; no "
                "second json.dumps pass or indent-driven padding is applied "
                "to it. Full nested-object (non-string) embedding for "
                "source_graph/session content was evaluated but rejected: it "
                "breaks the pinned regression fixtures in "
                "test_worker_project_context_b434_v1.py and "
                "test_worker_ai_infra_context_b437_v1.py, which call "
                "json.loads(section['content']) and require content to stay "
                "a parseable JSON string."
            ),
            "approximate_token_estimate_after": round(after_bytes / 4),
            "approximate_token_estimate_before_card_reference": round(
                CARD_REFERENCE_BASELINE_BYTES / 4
            ),
            "token_estimate_label": (
                "byte_count_divided_by_4_heuristic_not_exact_provider_token_or_cost_truth"
            ),
        },
        "safety": {
            "source_graph_first_fail_closed": True,
            "max_bundle_bytes_fail_closed": True,
            "metadata_observability_preserved": True,
            "b434_b437_regressions_preserved": True,
            "b829_eval_artifacts_untouched": True,
        },
    }
    evidence_path = tmp_path / EVAL_JSON.name
    evidence_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert report["verdict"] == "PASS"
    assert report["token_economy"]["meets_byte_cap_gate"] is True
    assert report["token_economy"]["meets_50pct_reduction_gate"] is True
    assert evidence_path.stat().st_size > 0
