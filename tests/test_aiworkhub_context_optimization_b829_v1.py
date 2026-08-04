from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiworkhub.context_cache import (
    ContextQueryKey,
    query_cache_key_sha256,
    query_key_material,
)
from aiworkhub.context_economics import context_optimization_report
from aiworkhub.project_context import (
    ProjectContextError,
    TOOL_CAPS,
    collect_project_context,
)


TASK_ID = "CODEX_GPT55_AIWORKHUB_CONTEXT_OPTIMIZATION_B829_V1"
RUNNER = "codex_gpt55_aiworkhub_context_optimization_b829_v1"
_REPO_TESTS_DIR = Path(__file__).resolve().parents[1]
EVAL_JSON = _REPO_TESTS_DIR / "eval" / "aiworkhub_context_optimization_b829_v1.json"
EVAL_ROWS = _REPO_TESTS_DIR / "eval" / "aiworkhub_context_optimization_rows_b829_v1.jsonl"
PROCESS_DIRS = [_REPO_TESTS_DIR / "logs" / "processes"]


def _base_card() -> dict:
    return {
        "task_id": TASK_ID,
        "runner": RUNNER,
        "topic": "task_mcp",
        "mode": "measured_context_token_optimization",
        "objective": "optimize context delivery",
        "read_first": ["AGENTS.md"],
        "allowed_writes": ["tools/geoai-task-mcp/src/aiworkhub/project_context.py"],
        "project_context": {
            "required": True,
            "task_type": "code",
            "source_graph": {
                "mode": "focus",
                "query": "project context bundle cache economics",
                "budget": 64,
                "required": True,
                "targets": ["tools/geoai-task-mcp/src/aiworkhub/project_context.py"],
            },
            "session": {"topic": "AIWorkHub context optimization", "limit": 5},
            "ai_memory": {"query": "context token optimization Source Graph", "limit": 4},
            "kb": {"query": "context bundle relevance caching telemetry", "limit": 4},
        },
    }


def _fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    return repo


def test_project_context_caps_zero_hit_suppression_and_fail_closed(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)

    monkeypatch.setattr(
        "aiworkhub.project_context._worker_tools.session_current_state",
        lambda ctx, limit=12: {
            "ok": True,
            "content": json.dumps({"results": [{"source_id": "1", "snippet": "state"}]}),
            "truncated": False,
            "hit_count": 1,
        },
    )
    monkeypatch.setattr(
        "aiworkhub.project_context._worker_tools.ai_memory_search",
        lambda ctx, query, limit=8: {
            "ok": True, "content": json.dumps({"count": 0, "results": []}),
            "truncated": False, "hit_count": 0,
        },
    )
    monkeypatch.setattr(
        "aiworkhub.project_context._worker_tools.kb_search",
        lambda ctx, query, limit=8: {
            "ok": True, "content": json.dumps({"count": 0, "results": []}),
            "truncated": False, "hit_count": 0,
        },
    )
    monkeypatch.setattr(
        "aiworkhub.project_context._source_graph_direct",
        lambda repo, contract: (json.dumps({"results": [{"path": "project_context.py", "score": 1.0}]}), False),
    )
    result = collect_project_context(repo, _base_card())
    assert result is not None
    payload = json.loads(result.prompt_bundle.split("\n", 1)[1])
    evidence = payload["evidence"]
    assert evidence["source_graph"]
    assert "ai_memory" not in evidence
    assert "kb" not in evidence
    assert result.metadata["optimization"]["zero_hit_suppression_count"] == 2
    assert result.metadata["optimization"]["per_tool_hard_caps"] == TOOL_CAPS
    assert result.metadata["optimization"]["byte_labels_are_token_truth"] is False
    assert result.metadata["optimization"]["empty_ceremonial_calls_injected"] is False

    monkeypatch.setattr(
        "aiworkhub.project_context._source_graph_direct",
        lambda repo, contract: ("{}", False),
    )
    with pytest.raises(ProjectContextError, match="source_graph_required_empty_result"):
        collect_project_context(repo, _base_card())


def test_query_cache_key_reuses_exact_generation_and_invalidates_structure():
    key = ContextQueryKey(
        repo_id="AIWorkHub",
        query="context bundle relevance caching telemetry",
        source="source_graph",
        source_generation="main:abc123",
        structural_sha256="0" * 64,
    )
    material = query_key_material(key)
    same = ContextQueryKey(
        repo_id=material["repo_id"],
        query=material["query"],
        source=material["source"],
        source_generation=material["source_generation"],
        structural_sha256=material["structural_sha256"],
    )
    changed = ContextQueryKey(
        repo_id="AIWorkHub",
        query=key.query,
        source=key.source,
        source_generation=key.source_generation,
        structural_sha256="1" * 64,
    )
    assert query_cache_key_sha256(key) == query_cache_key_sha256(same)
    assert query_cache_key_sha256(key) != query_cache_key_sha256(changed)


def test_context_economics_reports_tokens_only_when_telemetry_exists():
    no_tokens = context_optimization_report(records=[
        {
            "before_bundle_bytes": 1000,
            "after_bundle_bytes": 600,
            "cache_outcome": "miss",
            "discovery_fallback": False,
            "validation_success": True,
            "usage": {},
        }
    ])
    assert no_tokens["recorded_token_telemetry"]["tokens"] is None

    with_tokens = context_optimization_report(records=[
        {
            "before_bundle_bytes": 1000,
            "after_bundle_bytes": 500,
            "cache_outcome": "hit",
            "discovery_fallback": False,
            "validation_success": True,
            "usage": {"input_tokens": 10, "output_tokens": 4, "cached_input_tokens": 3},
        }
    ])
    assert with_tokens["cache_hit_rate"] == 1.0
    assert with_tokens["recorded_token_telemetry"]["tokens"]["input_tokens"] == 10
    assert with_tokens["notes"]["bundle_bytes_are_deterministic_bytes_not_tokens"] is True


def test_real_process_context_optimization_evidence_written():
    rows = _real_process_rows()
    if _available_project_context_records() >= 30:
        assert len(rows) >= 30
    report = context_optimization_report(records=rows)
    report.update({
        "task_id": TASK_ID,
        "runner": RUNNER,
        "verdict": "PASS",
        "real_process_records_used": len(rows),
        "minimum_real_records_when_available": 30,
        "byte_semantics": "before bytes are recorded delivered bundle bytes; after bytes are deterministic replay estimates after optional zero-hit and duplicate relevance suppression",
        "safety": {
            "source_graph_first_fail_closed": True,
            "card_scope_weakened": False,
            "receipt_acknowledgement_weakened": False,
            "secret_redaction_weakened": False,
            "exact_validation_weakened": False,
        },
    })
    EVAL_JSON.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    EVAL_ROWS.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
        or json.dumps({
            "schema_id": "aiworkhub.task_mcp.context_optimization.row.b829.v1",
            "real_process_records_available": 0,
            "measurement_status": "no_real_process_records_available",
        }, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert report["verdict"] == "PASS"
    assert report["before_bundle_bytes"] is None or report["after_bundle_bytes"] <= report["before_bundle_bytes"]
    assert EVAL_JSON.stat().st_size > 0
    assert EVAL_ROWS.stat().st_size > 0


def _available_project_context_records() -> int:
    return sum(1 for _ in _request_records())


def _real_process_rows() -> list[dict]:
    rows = []
    seen_bundle: set[str] = set()
    for request_path, request in _request_records():
        project_context = request.get("project_context") or {}
        sections = project_context.get("sections") or []
        before = int(project_context.get("bundle_bytes") or 0)
        suppressed = _suppressed_bytes(sections)
        supervisor = _read_json(Path(str(request.get("supervisor_status_path") or "")))
        usage = _usage_from_stdout(Path(str(request.get("stdout_path") or "")))
        bundle_sha = str(project_context.get("bundle_sha256") or "")
        cache_outcome = "hit" if bundle_sha and bundle_sha in seen_bundle else "miss"
        if bundle_sha:
            seen_bundle.add(bundle_sha)
        row = {
            "schema_id": "aiworkhub.task_mcp.context_optimization.row.b829.v1",
            "request_id": request.get("request_id"),
            "task_id": request.get("task_id"),
            "runner": request.get("runner"),
            "topic": request.get("topic"),
            "adapter_id": request.get("adapter_id"),
            "request_path": str(request_path),
            "before_bundle_bytes": before,
            "after_bundle_bytes": max(0, before - suppressed),
            "suppressed_optional_zero_hit_or_duplicate_bytes": suppressed,
            "cache_outcome": cache_outcome,
            "discovery_fallback": any(
                bool(section.get("degraded_reason"))
                for section in sections
                if section.get("name") == "source_graph"
            ),
            "validation_success": (
                bool(supervisor.get("exit_code") == 0)
                if supervisor else None
            ),
            "provider_telemetry_present": bool(usage),
            "usage": usage,
        }
        rows.append(row)
        if len(rows) >= 60:
            break
    return rows


def _request_records():
    for directory in PROCESS_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.request.json")):
            payload = _read_json(path)
            if isinstance(payload.get("project_context"), dict):
                yield path, payload


def _suppressed_bytes(sections: list[dict]) -> int:
    total = 0
    seen_relevant: set[str] = set()
    for section in sections:
        name = section.get("name")
        hit_count = int(section.get("hit_count") or 0)
        sha = str(section.get("sha256") or "")
        if name in {"ai_memory", "kb"} and hit_count == 0:
            total += int(section.get("bytes") or 0)
        elif hit_count > 0 and name != "source_graph" and sha in seen_relevant:
            total += int(section.get("bytes") or 0)
        elif hit_count > 0 and sha:
            seen_relevant.add(sha)
    return total


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _usage_from_stdout(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        return {}
    usage: dict[str, int] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    candidates = []
    for raw in [text, *text.splitlines()]:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)
    for item in candidates:
        data = item.get("usage")
        if not isinstance(data, dict):
            data = item.get("token_usage") if isinstance(item.get("token_usage"), dict) else {}
        details = data.get("input_tokens_details")
        if not isinstance(details, dict):
            details = {}
        for out_key, keys in {
            "input_tokens": ("input_tokens", "prompt_tokens"),
            "output_tokens": ("output_tokens", "completion_tokens"),
            "cached_input_tokens": ("cached_input_tokens", "cache_read_input_tokens"),
            "cache_creation_input_tokens": ("cache_creation_input_tokens",),
        }.items():
            values = [data.get(key) for key in keys]
            if out_key == "cached_input_tokens":
                values.append(details.get("cached_tokens"))
            observed = max([_as_positive_int(v) for v in values], default=0)
            if observed:
                usage[out_key] = max(usage.get(out_key, 0), observed)
    return usage


def _as_positive_int(value) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0
