from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _ensure_deepseek_credentials_stub() -> None:
    import importlib
    import types

    try:
        importlib.import_module("aiworkhub.deepseek_credentials")
        return
    except ImportError:
        pass

    stub = types.ModuleType("aiworkhub.deepseek_credentials")

    class CredentialError(Exception):
        def __init__(self, reason: str = "deepseek_credential_stub_environment") -> None:
            super().__init__(reason)
            self.reason = reason

    def load_credential(repo=None):  # noqa: ANN001, ARG001
        raise CredentialError("deepseek_credential_stub_environment")

    def adapter_readiness(repo=None):  # noqa: ANN001, ARG001
        return {"ok": True, "readonly": True, "adapters": []}

    stub.CredentialError = CredentialError
    stub.load_credential = load_credential
    stub.adapter_readiness = adapter_readiness
    sys.modules["aiworkhub.deepseek_credentials"] = stub


_ensure_deepseek_credentials_stub()

from aiworkhub import dashboard, process_launcher, project_context, worker_ai_tools_mcp  # noqa: E402


def _write_tool(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    try:
        path.chmod(0o755)
    except PermissionError:
        pass


def _stub_source_graph_direct(
    monkeypatch: pytest.MonkeyPatch, payload: str, *, capture: dict | None = None
) -> None:
    """Deterministic in-process Source Graph stub (B849 canonical authority).

    Since B849, ``collect_project_context`` calls
    ``aiworkhub.source_graph`` directly in-process -- no subprocess, no
    ``AITools/source_graph.py`` dependency. B866 proved the fixture
    pattern: monkeypatch ``project_context._source_graph_direct`` rather
    than shelling out to a fake script.
    """

    def fake_direct(repo: Path, contract: dict) -> tuple[str, bool]:
        if capture is not None:
            capture["contract"] = contract
        return payload, False

    monkeypatch.setattr(project_context, "_source_graph_direct", fake_direct)


def _stub_worker_tools_direct(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_hit: bool = True,
    memory_hit: bool = True,
    kb_hit: bool = True,
) -> None:
    """Deterministic in-process Session Manager / AI Memory / KB stub
    (B879 canonical authority). ``collect_project_context`` calls
    ``worker_ai_tools_mcp.session_current_state`` / ``ai_memory_search`` /
    ``kb_search`` in-process against the repository's canonical
    ``.aiworkhub`` SQLite authority -- no subprocess, no ``AITools/*.py``
    dependency. Monkeypatching these call sites directly mirrors the
    accepted ``_stub_source_graph_direct`` pattern for Source Graph.
    """

    def fake_session(ctx, *, limit: int = 12):
        evidence = (
            [{"source_id": "1", "timestamp": "2026-01-01T00:00:00Z", "kind": "progress", "snippet": "bounded"}]
            if session_hit else []
        )
        content = json.dumps(
            {"state": "partial_state" if session_hit else "unknown", "topic": ctx.session_topic, "evidence": evidence},
            sort_keys=True,
        )
        return {"ok": True, "content": content, "truncated": False, "hit_count": len(evidence)}

    def fake_memory(ctx, *, query: str, limit: int = 8):
        results = [{"key": "ctx", "value": "bounded", "tags": "decision"}] if memory_hit else []
        content = json.dumps({"count": len(results), "results": results}, sort_keys=True)
        return {"ok": True, "content": content, "truncated": False, "hit_count": len(results)}

    def fake_kb(ctx, *, query: str, limit: int = 8):
        results = [{"key": "module.task_mcp_context", "title": "task_mcp_context", "category": "module"}] if kb_hit else []
        content = json.dumps({"results": results, "count": len(results)}, sort_keys=True)
        return {"ok": True, "content": content, "truncated": False, "hit_count": len(results)}

    monkeypatch.setattr(project_context._worker_tools, "session_current_state", fake_session)
    monkeypatch.setattr(project_context._worker_tools, "ai_memory_search", fake_memory)
    monkeypatch.setattr(project_context._worker_tools, "kb_search", fake_kb)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "AITools" / "ai_memory").mkdir(parents=True)
    _write_tool(
        repo / "AITools/transcript_graph.py",
        """
import json, sys
print(json.dumps({"state":"partial_state","evidence":[{"id":1}],"topic":sys.argv[2]}))
""",
    )
    _write_tool(
        repo / "AITools/ai_memory/ai_memory.py",
        """
import json, sys
print(json.dumps({"count":1,"results":[{"key":"ctx","value":"bounded","tags":["decision"]}]}))
""",
    )
    _write_tool(
        repo / "AITools/kb.py",
        """
import sys
print("[module] task_mcp_context — bounded Source Graph context")
""",
    )
    return repo


def _card(*, task_type: str = "code", source_required: bool | None = None) -> dict:
    source = {
        "mode": "bundle",
        "bundle_type": "feature",
        "query": "Natural language fallback",
        "targets": ["tools/geoai-task-mcp/src/aiworkhub/process_launcher.py"],
        "budget": 32,
    }
    if source_required is not None:
        source["required"] = source_required
    return {
        "task_id": "TASK_B437",
        "runner": "codex_b437",
        "topic": "task_mcp",
        "status": "pending",
        "worker_status": "unclaimed",
        "claimed_by": "",
        "mode": "data_classification" if task_type == "data_classification" else "code",
        "immutable_input_shard": "lexicon/shards/immutable-001.jsonl",
        "read_first": ["tools/geoai-task-mcp/src/aiworkhub/process_launcher.py"],
        "allowed_writes": ["tools/geoai-task-mcp/eval/out.json"],
        "project_context": {
            "required": True,
            "task_type": task_type,
            "source_graph": source,
            "session": {"topic": "Task MCP Source Graph Session Manager AI Memory KB", "limit": 3},
            "ai_memory": {"query": "Task MCP Source Graph", "limit": 2},
            "kb": {"query": "Task MCP Source Graph", "limit": 2},
        },
    }


def test_exact_source_graph_targets_policy_and_metadata_are_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    captured: dict = {}
    _stub_source_graph_direct(
        monkeypatch,
        json.dumps({"sections": [{"items": [{"file": "process_launcher.py"}]}]}),
        capture=captured,
    )
    _stub_worker_tools_direct(monkeypatch)
    result = project_context.collect_project_context(repo, _card())
    assert result is not None
    payload = json.loads(result.prompt_bundle.split("PROJECT_CONTEXT_BUNDLE:\n", 1)[1])
    source = payload["evidence"]["source_graph"]
    assert source["sections"][0]["items"][0]["file"] == "process_launcher.py"
    assert captured["contract"]["source_graph"]["targets_origin"] == "declared"
    assert captured["contract"]["source_graph"]["targets"][0] == "tools/geoai-task-mcp/src/aiworkhub/process_launcher.py"
    assert result.metadata["sections"][0]["requested"] is True
    assert result.metadata["sections"][0]["hit_count"] > 0
    assert "process_launcher.py" not in json.dumps(result.metadata)
    assert result.metadata["estimated_raw_context_vs_bundle_bytes"]["label"].endswith("not_token_or_cost_truth")


def test_required_empty_code_source_graph_fails_before_claim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")
    monkeypatch.setattr(os, "chmod", lambda *args, **kwargs: None)
    monkeypatch.setattr(os, "fchmod", lambda *args, **kwargs: None, raising=False)
    repo = _repo(tmp_path)
    _stub_source_graph_direct(monkeypatch, "{}")
    claims: list[tuple] = []
    monkeypatch.setattr(process_launcher.core, "claim_start_exact", lambda *args: claims.append(args) or {"ok": True})
    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=lambda _task_id: {"returncode": 0, "stdout": json.dumps(_card()), "stderr": ""},
        collision_guard=lambda **_kwargs: {"returncode": 0, "stdout": "{}", "stderr": ""},
        adapter_builder=lambda **_kwargs: SimpleNamespace(argv=[sys.executable, "-c", "pass"], cwd=str(repo), launchable=True, reason=""),
        isolation_enabled=False,
    )
    blocked = manager.launch(
        task_id="TASK_B437",
        runner="codex_b437",
        topic="task_mcp",
        adapter_id="codex_cli",
        timeout_seconds=30,
    )
    assert blocked["ok"] is False
    assert "source_graph_required_empty_result" in blocked["blocked_reason"]
    assert claims == []


def test_data_classification_source_graph_can_be_non_gating_with_input_shard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    _stub_source_graph_direct(monkeypatch, "{}")
    _stub_worker_tools_direct(monkeypatch)
    result = project_context.collect_project_context(
        repo,
        _card(task_type="data_classification", source_required=False),
    )
    assert result is not None
    assert result.metadata["task_context_policy"]["primary_context"] == "immutable_input_shard"
    source_meta = next(section for section in result.metadata["sections"] if section["name"] == "source_graph")
    assert source_meta["hit_count"] == 0
    assert result.metadata["task_context_policy"]["source_graph_required"] is False


def test_common_prompt_delivery_and_receipt_are_distinct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    _stub_source_graph_direct(
        monkeypatch, json.dumps({"sections": [{"items": [{"symbol": "ProcessManager"}]}]})
    )
    _stub_worker_tools_direct(monkeypatch)
    context_result = project_context.collect_project_context(repo, _card())
    assert context_result is not None
    prompts = [
        process_launcher.build_worker_prompt(
            task_id="TASK_B437",
            runner=runner,
            topic="task_mcp",
            card=_card(),
            project_context_bundle=context_result.prompt_bundle,
        )
        for runner in ("codex_b437", "claude_b437", "deepseek_b437")
    ]
    assert prompts[0].split("PROJECT_CONTEXT_BUNDLE:", 1)[1] == prompts[1].split("PROJECT_CONTEXT_BUNDLE:", 1)[1]
    assert "PROJECT_CONTEXT_RECEIPT" in prompts[2]
    assert context_result.metadata["bundle_sha256"] in prompts[2]
    output = tmp_path / "stdout.log"
    output.write_text(
        "PROJECT_CONTEXT_RECEIPT: "
        + json.dumps({
            "schema_id": project_context.RECEIPT_SCHEMA_ID,
            "acknowledged": True,
            "bundle_sha256": context_result.metadata["bundle_sha256"],
            "prompt_sha256": "abc",
            "section_count": context_result.metadata["section_count"],
        })
        + "\n",
        encoding="utf-8",
    )
    receipt = process_launcher._project_context_receipt_from_output(
        output,
        expected_bundle_sha256=context_result.metadata["bundle_sha256"],
    )
    assert receipt["acknowledged"] is True
    assert receipt["bundle_sha256"] == context_result.metadata["bundle_sha256"]

    codex_event = tmp_path / "codex.stdout.log"
    codex_event.write_text(json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": output.read_text(encoding="utf-8")},
    }) + "\n", encoding="utf-8")
    assert process_launcher._project_context_receipt_from_output(
        codex_event,
        expected_bundle_sha256=context_result.metadata["bundle_sha256"],
    )["acknowledged"] is True

    deepseek_event = tmp_path / "deepseek.stdout.log"
    deepseek_event.write_text(json.dumps({
        "type": "assistant.message",
        "data": {"content": "done\n" + output.read_text(encoding="utf-8")},
    }) + "\n", encoding="utf-8")
    assert process_launcher._project_context_receipt_from_output(
        deepseek_event,
        expected_bundle_sha256=context_result.metadata["bundle_sha256"],
    )["acknowledged"] is True

    mismatch = process_launcher._project_context_receipt_from_output(
        deepseek_event,
        expected_bundle_sha256="0" * 64,
    )
    assert mismatch["acknowledged"] is False
    assert mismatch["reason"] == "receipt_bundle_sha256_mismatch"


def test_receipt_survives_long_stream_suffix(tmp_path: Path) -> None:
    output = tmp_path / "long.stdout.log"
    bundle_sha = "a" * 64
    receipt = {
        "schema_id": project_context.RECEIPT_SCHEMA_ID,
        "acknowledged": True,
        "bundle_sha256": bundle_sha,
        "prompt_sha256": "b" * 64,
        "section_count": 4,
    }
    output.write_text(
        "PROJECT_CONTEXT_RECEIPT: " + json.dumps(receipt) + "\n" + ("x" * (128 * 1024)),
        encoding="utf-8",
    )
    parsed = process_launcher._project_context_receipt_from_output(
        output, expected_bundle_sha256=bundle_sha,
    )
    assert parsed["acknowledged"] is True
    assert parsed["section_count"] == 4


def test_audit_append_accepts_seccomp_denied_fchmod_on_existing_0600_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if os.name == "nt":
        pytest.skip("requires POSIX fchmod and mode semantics")
    ledger = tmp_path / "audit.jsonl"
    ledger.touch(mode=0o600)

    def denied(_fd: int, _mode: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "fchmod", denied, raising=False)
    worker_ai_tools_mcp._append_line_0600(ledger, '{"ok":true}\n')
    assert ledger.read_text(encoding="utf-8") == '{"ok":true}\n'
    assert ledger.stat().st_mode & 0o777 == 0o600


def test_dashboard_exposes_compact_ai_infra_without_raw_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process_log = tmp_path / "events.jsonl"
    event = {
        "schema_id": "aiworkhub.task_mcp.process_event.v1",
        "timestamp": "2026-07-16T00:00:00+00:00",
        "request_id": "run-b437",
        "task_id": "TASK_B437",
        "runner": "codex_b437",
        "topic": "task_mcp",
        "adapter_id": "codex_cli",
        "state": "review_ready",
        "project_context": {
            "schema_id": project_context.SCHEMA_ID,
            "bundle_sha256": "b" * 64,
            "bundle_bytes": 200,
            "estimated_raw_context_vs_bundle_bytes": {
                "label": "deterministic_byte_estimate_not_token_or_cost_truth",
                "raw_context_bytes": 120,
                "bundle_bytes": 200,
                "delta_bytes": -80,
            },
            "sections": [
                {"name": "source_graph", "requested": True, "executed": True, "hit_count": 2, "bytes": 80, "sha256": "s" * 64, "truncated": False, "degraded_reason": ""},
                {"name": "session_current_state", "requested": True, "executed": True, "hit_count": 1, "bytes": 20, "sha256": "m" * 64, "truncated": False, "degraded_reason": ""},
                {"name": "ai_memory", "requested": True, "executed": True, "hit_count": 1, "bytes": 10, "sha256": "a" * 64, "truncated": False, "degraded_reason": ""},
                {"name": "kb", "requested": True, "executed": True, "hit_count": 1, "bytes": 10, "sha256": "k" * 64, "truncated": False, "degraded_reason": ""},
            ],
        },
        "project_context_delivery": {"injected": True, "bundle_sha256": "b" * 64, "prompt_sha256": "p" * 64, "bundle_bytes": 200},
        "project_context_acknowledgement": {"schema_id": project_context.RECEIPT_SCHEMA_ID, "acknowledged": False, "reason": "receipt_not_found"},
        "raw_context_content": "MUST_NOT_RENDER",
    }
    process_log.write_text(json.dumps(event) + "\n", encoding="utf-8")
    monkeypatch.setenv(dashboard.process_launcher.PROCESS_LOG_ENV, str(process_log))
    report = dashboard.read_process_runs()
    row = report["processes"][0]
    assert row["ai_infra_context"]["source_graph"]["hit_count"] == 2
    assert row["ai_infra_context"]["injected"] is True
    assert row["ai_infra_context"]["acknowledged"] is False
    assert "MUST_NOT_RENDER" not in json.dumps(row)

    class Provider:
        def get_task(self, task_id: str) -> dict:
            return {"task_id": task_id, "topic": "task_mcp", "runner": "codex_b437"}

        def get_agent_processes(self) -> dict:
            return report

    detail = dashboard.build_task_detail("TASK_B437", Provider())
    assert detail is not None
    assert detail["task"]["ai_infra_context"]["kb"]["hit_count"] == 1
    assert "MUST_NOT_RENDER" not in json.dumps(detail)
