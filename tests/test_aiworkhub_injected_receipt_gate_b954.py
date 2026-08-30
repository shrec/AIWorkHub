"""Project-context injection and continuous worker tool-use gate coverage.

The measured defect: correctly-finished work went to validation_failed with
``required_aiworkhub_mcp_calls_missing:session_current_state,ai_memory`` even
though the launcher had injected those sections with a verified hash receipt
(executed=true), because the gate only ever credited LIVE worker calls. This
locks the acceptance criteria: an executed, non-degraded section whose bundle
receipt is acknowledged satisfies optional context tools (zero hit_count is
still valid), and canonical supervisor Source Graph satisfies initial
orientation;
tampered / repo-mismatched / unacknowledged receipts and degraded sections stay
fail-closed; the terminal evidence names the satisfaction source per tool.
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher as pl  # noqa: E402
from aiworkhub import project_context  # noqa: E402
from aiworkhub import worker_ai_tools_mcp as wm  # noqa: E402


def _sha(text: str = "bundle-b954") -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _receipt_payload(
    bundle_sha: str,
    *,
    section_count: int = 2,
    acknowledged: bool = True,
    request_id: str = "req",
) -> dict:
    return {
        "schema_id": project_context.RECEIPT_SCHEMA_ID,
        "acknowledged": acknowledged,
        "bundle_sha256": bundle_sha,
        "prompt_sha256": "",
        "section_count": section_count,
        "request_id": request_id,
    }


def _write_receipt(
    tmp_path: Path,
    bundle_sha: str,
    *,
    section_count: int = 2,
    acknowledged: bool = True,
    request_id: str = "req",
) -> Path:
    stdout = tmp_path / "worker.stdout.log"
    receipt = _receipt_payload(
        bundle_sha,
        section_count=section_count,
        acknowledged=acknowledged,
        request_id=request_id,
    )
    stdout.write_text(
        json.dumps({
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "PROJECT_CONTEXT_RECEIPT: " + json.dumps(receipt),
            },
        }) + "\n",
        encoding="utf-8",
    )
    return stdout


def _metadata(
    tmp_path: Path,
    *,
    bundle_sha: str,
    sections: list[dict],
    stdout: Path,
    runtime: bool = True,
    task_type: str = "code",
) -> dict:
    return {
        "task_id": "TASK_B954", "runner": "codex", "topic": "representation",
        "stdout_path": str(stdout),
        "worker_mcp": (
            {"audit_ledger_path": str(tmp_path / "ledger.jsonl"), "audit_hmac_key_path": str(tmp_path / "key.bin")}
            if runtime else {}
        ),
        "project_context": {
            "task_context_policy": {"task_type": task_type},
            "bundle_sha256": bundle_sha,
            "sections": sections,
        },
    }


def _patch_verify(monkeypatch, *, live_source_graph: int = 0, successful: dict | None = None, policy_violations: int = 0) -> None:
    payload = dict(successful or {})

    def fake_verify(*_a, **_k):
        return {
            "ok": True,
            "live_source_graph_calls": live_source_graph,
            "successful_call_count_by_tool": dict(payload),
            "policy_violations": policy_violations,
            "reason": "",
        }

    monkeypatch.setattr(pl.worker_ai_tools_mcp, "verify_audit_ledger", fake_verify)


def _sections(*specs) -> list[dict]:
    out = []
    for name, executed, hit_count, degraded in specs:
        out.append({
            "name": name, "requested": True, "executed": executed,
            "hit_count": hit_count, "degraded_reason": degraded,
        })
    return out


def test_verbatim_worker_prompt_echo_cannot_acknowledge_injected_orientation(
    tmp_path,
    monkeypatch,
):
    _patch_verify(monkeypatch)
    request_id = "canonical-request-b954"
    bundle = "PROJECT_CONTEXT_BUNDLE:\n" + json.dumps({
        "evidence": {"source_graph": {"matches": [], "truncated": False}},
        "repo_identity": {"repo_id": "repo-b954", "scope_root": "."},
    })
    bundle_sha = _sha(bundle)
    prompt = pl.build_worker_prompt(
        task_id="TASK_B954",
        runner="codex",
        topic="representation",
        request_id=request_id,
        project_context_bundle=bundle,
    )
    stdout = tmp_path / "prompt-echo.stdout.log"
    stdout.write_text(prompt + "\n", encoding="utf-8")

    gate = pl._worker_mcp_live_call_gate(
        _metadata(
            tmp_path,
            bundle_sha=bundle_sha,
            sections=_sections(("source_graph", True, 0, "")),
            stdout=stdout,
        ),
        request_id,
    )

    assert f'\"request_id\":\"{request_id}\"' in prompt
    assert gate["satisfied"] is False
    assert gate["missing_tools"] == ["source_graph"]


def test_authenticated_assistant_receipt_binds_exact_request_and_satisfies_orientation(
    tmp_path,
    monkeypatch,
):
    _patch_verify(monkeypatch)
    request_id = "canonical-request-b954"
    bundle = "PROJECT_CONTEXT_BUNDLE:\n" + json.dumps({
        "evidence": {"source_graph": {"matches": [], "truncated": False}},
        "repo_identity": {"repo_id": "repo-b954", "scope_root": "."},
    })
    bundle_sha = _sha(bundle)
    prompt = pl.build_worker_prompt(
        task_id="TASK_B954",
        runner="codex",
        topic="representation",
        request_id=request_id,
        project_context_bundle=bundle,
    )
    receipt = _receipt_payload(bundle_sha, section_count=1, request_id=request_id)
    stdout = tmp_path / "authenticated-worker.stdout.log"
    stdout.write_text(json.dumps({
        "type": "item.completed",
        "item": {
            "type": "agent_message",
            "text": "PROJECT_CONTEXT_RECEIPT: " + json.dumps(receipt),
        },
    }) + "\n", encoding="utf-8")

    gate = pl._worker_mcp_live_call_gate(
        _metadata(
            tmp_path,
            bundle_sha=bundle_sha,
            sections=_sections(("source_graph", True, 0, "")),
            stdout=stdout,
        ),
        request_id,
    )

    assert f'\"request_id\":\"{request_id}\"' in prompt
    assert gate["satisfied"] is True
    assert gate["verification"]["live_source_graph_calls"] == 0
    assert gate["satisfaction_by_tool"]["source_graph"] == (
        "supervisor_injected_orientation"
    )


# --- PASS: injected+verified sections, no live call ------------------------

def test_injected_session_and_source_graph_satisfy_initial_orientation(tmp_path, monkeypatch):
    _patch_verify(monkeypatch)  # empty ledger, zero live calls
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=2)
    sections = _sections(
        ("source_graph", True, 5, ""),
        ("session_current_state", True, 8, ""),
    )
    gate = pl._worker_mcp_live_call_gate(_metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req")
    assert gate["satisfied"] is True
    assert gate["missing_tools"] == []
    assert gate["injected_context_acknowledged"] is True
    assert gate["satisfaction_by_tool"]["source_graph"] == "supervisor_injected_orientation"
    assert gate["satisfaction_by_tool"]["session_current_state"] == "injected_receipt"


def test_injected_ai_memory_zero_hit_is_valid(tmp_path, monkeypatch):
    _patch_verify(monkeypatch)
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=2)
    sections = _sections(
        ("source_graph", True, 3, ""),
        ("ai_memory", True, 0, ""),   # zero hits, but executed + acknowledged
    )
    gate = pl._worker_mcp_live_call_gate(_metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req")
    assert gate["satisfied"] is True
    assert gate["missing_tools"] == []
    assert gate["satisfaction_by_tool"]["source_graph"] == "supervisor_injected_orientation"
    assert gate["satisfaction_by_tool"]["ai_memory"] == "injected_receipt"


def test_research_task_reports_acknowledged_injected_context_without_becoming_gated(
    tmp_path,
    monkeypatch,
):
    _patch_verify(
        monkeypatch,
        live_source_graph=1,
        successful={"source_graph": 1},
    )
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=1)
    sections = _sections(("source_graph", True, 3, ""))

    gate = pl._worker_mcp_live_call_gate(
        _metadata(
            tmp_path,
            bundle_sha=bundle,
            sections=sections,
            stdout=stdout,
            task_type="research",
        ),
        "req",
    )

    assert gate["gated"] is False
    assert gate["satisfied"] is True
    assert gate["injected_context_acknowledged"] is True
    assert gate["observation_only"] is True
    assert gate["telemetry_observed"] is True
    assert gate["verification"]["live_source_graph_calls"] == 1


# --- FAIL: not injected / degraded, no live call ---------------------------

def test_section_not_executed_and_no_live_call_fails(tmp_path, monkeypatch):
    _patch_verify(monkeypatch)
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=1)
    sections = _sections(
        ("source_graph", True, 3, ""),
        ("ai_memory", False, 0, ""),   # requested but not executed (not injected)
    )
    gate = pl._worker_mcp_live_call_gate(_metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req")
    assert gate["satisfied"] is False
    assert gate["missing_tools"] == ["ai_memory"]
    assert gate["reason"] == "required_aiworkhub_mcp_calls_missing:ai_memory"


def test_degraded_injected_section_requires_live_recovery(tmp_path, monkeypatch):
    _patch_verify(monkeypatch)
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=2)
    sections = _sections(
        ("source_graph", True, 3, ""),
        ("session_current_state", True, 0, "session_store_unavailable"),  # degraded
    )
    gate = pl._worker_mcp_live_call_gate(_metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req")
    assert gate["satisfied"] is False
    assert gate["missing_tools"] == ["session_current_state"]


def test_degraded_supervisor_source_graph_stays_fail_closed(tmp_path, monkeypatch):
    _patch_verify(monkeypatch)
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=1)
    sections = _sections(("source_graph", True, 0, "cached_or_stale"))
    gate = pl._worker_mcp_live_call_gate(
        _metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req"
    )
    assert gate["satisfied"] is False
    assert gate["missing_tools"] == ["source_graph"]


def test_supervisor_source_graph_requires_authenticated_audit_authority(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        pl.worker_ai_tools_mcp,
        "verify_audit_ledger",
        lambda *_a, **_k: {
            "ok": False,
            "reason": "audit_unavailable",
            "live_source_graph_calls": 0,
            "successful_call_count_by_tool": {},
            "policy_violations": 0,
        },
    )
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=1)
    sections = _sections(("source_graph", True, 0, ""))

    gate = pl._worker_mcp_live_call_gate(
        _metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req"
    )

    assert gate["injected_context_acknowledged"] is True
    assert gate["satisfied"] is False
    assert gate["missing_tools"] == ["source_graph"]
    assert "source_graph" not in gate["satisfaction_by_tool"]
    assert gate["reason"] == "audit_unavailable"


# --- FAIL: tampered / repo-mismatched receipt stays fail-closed ------------

def test_receipt_sha_mismatch_fails_closed(tmp_path, monkeypatch):
    _patch_verify(monkeypatch)
    stored = _sha("real-bundle")
    # The worker echoes a DIFFERENT bundle_sha256 (tampered / another repo's bundle).
    stdout = _write_receipt(tmp_path, _sha("forged-bundle"), section_count=2)
    sections = _sections(
        ("source_graph", True, 3, ""),
        ("session_current_state", True, 8, ""),
    )
    gate = pl._worker_mcp_live_call_gate(_metadata(tmp_path, bundle_sha=stored, sections=sections, stdout=stdout), "req")
    assert gate["injected_context_acknowledged"] is False
    assert gate["satisfied"] is False
    assert set(gate["missing_tools"]) == {"source_graph", "session_current_state"}


def test_unacknowledged_receipt_fails_closed(tmp_path, monkeypatch):
    _patch_verify(monkeypatch)
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=2, acknowledged=False)
    sections = _sections(("source_graph", True, 3, ""), ("ai_memory", True, 0, ""))
    gate = pl._worker_mcp_live_call_gate(_metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req")
    assert gate["injected_context_acknowledged"] is False
    assert gate["satisfied"] is False


@pytest.mark.parametrize(
    "acknowledged",
    ["false", 1, 0, [], [True], {}, {"value": True}, None],
    ids=[
        "string",
        "nonzero-number",
        "zero-number",
        "empty-list",
        "nonempty-list",
        "empty-object",
        "nonempty-object",
        "null",
    ],
)
def test_non_boolean_acknowledgement_never_acknowledges_authenticated_receipt(
    tmp_path,
    monkeypatch,
    acknowledged,
):
    _patch_verify(monkeypatch)
    bundle = _sha()
    receipt = _receipt_payload(bundle, section_count=1, request_id="req")
    receipt["acknowledged"] = acknowledged
    stdout = tmp_path / "invalid-acknowledgement.stdout.log"
    stdout.write_text(
        json.dumps({
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "PROJECT_CONTEXT_RECEIPT: " + json.dumps(receipt),
            },
        }) + "\n",
        encoding="utf-8",
    )
    sections = _sections(("source_graph", True, 0, ""))

    gate = pl._worker_mcp_live_call_gate(
        _metadata(
            tmp_path,
            bundle_sha=bundle,
            sections=sections,
            stdout=stdout,
        ),
        "req",
    )

    assert gate["injected_context_acknowledged"] is False
    assert gate["satisfied"] is False
    assert gate["missing_tools"] == ["source_graph"]
    assert "source_graph" not in gate["satisfaction_by_tool"]


# --- PASS: no injection, verified live calls -------------------------------

def test_no_injection_but_live_calls_pass(tmp_path, monkeypatch):
    _patch_verify(monkeypatch, live_source_graph=1, successful={"session_current_state": 1})
    bundle = _sha()
    stdout = tmp_path / "no_receipt.log"
    stdout.write_text("worker output without any receipt\n", encoding="utf-8")
    sections = _sections(
        ("source_graph", False, 0, ""),
        ("session_current_state", False, 0, ""),
    )
    gate = pl._worker_mcp_live_call_gate(_metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req")
    assert gate["satisfied"] is True
    assert gate["satisfaction_by_tool"]["source_graph"] == "live_worker_call"
    assert gate["satisfaction_by_tool"]["session_current_state"] == "live_worker_call"


def test_live_source_graph_call_takes_precedence_over_supervisor_orientation(
    tmp_path,
    monkeypatch,
):
    _patch_verify(monkeypatch, live_source_graph=1, successful={"source_graph": 1})
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=1)
    sections = _sections(("source_graph", True, 0, ""))
    gate = pl._worker_mcp_live_call_gate(
        _metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req"
    )
    assert gate["satisfied"] is True
    assert gate["satisfaction_by_tool"]["source_graph"] == "live_worker_call"


def test_supervisor_receipt_from_another_request_cannot_be_replayed(
    tmp_path,
    monkeypatch,
):
    _patch_verify(monkeypatch)
    bundle = _sha()
    stdout = _write_receipt(
        tmp_path,
        bundle,
        section_count=1,
        request_id="first-request",
    )
    sections = _sections(("source_graph", True, 0, ""))
    gate = pl._worker_mcp_live_call_gate(
        _metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout),
        "second-request",
    )
    assert gate["satisfied"] is False
    assert gate["injected_context_acknowledged"] is True
    assert gate["missing_tools"] == ["source_graph"]
    assert "source_graph" not in gate["satisfaction_by_tool"]


def test_fresh_zero_hit_source_graph_call_satisfies_invocation_gate(
    tmp_path,
    monkeypatch,
):
    """NF184: evidence usefulness must not erase authenticated invocation truth."""
    monkeypatch.setattr(
        pl.worker_ai_tools_mcp,
        "verify_audit_ledger",
        lambda *_a, **_k: {
            "ok": True,
            "live_source_graph_calls": 1,
            "fresh_source_graph_calls": 1,
            "source_graph_hit_count": 0,
            "source_graph_zero_hit_calls": 1,
            "successful_call_count_by_tool": {"source_graph": 1},
            "policy_violations": 0,
            "reason": "",
        },
    )
    stdout = tmp_path / "zero-hit-worker.log"
    stdout.write_text("authenticated worker call\n", encoding="utf-8")
    metadata = _metadata(
        tmp_path,
        bundle_sha=_sha(),
        sections=_sections(("source_graph", False, 0, "")),
        stdout=stdout,
    )

    gate = pl._worker_mcp_live_call_gate(metadata, "req")

    assert gate["satisfied"] is True
    assert gate["missing_tools"] == []
    assert gate["stale_tools"] == []
    assert gate["satisfaction_by_tool"]["source_graph"] == "live_worker_call"
    assert gate["verification"]["source_graph_zero_hit_calls"] == 1


def test_live_calls_with_denied_request_recover_as_policy_warning(tmp_path, monkeypatch):
    _patch_verify(monkeypatch, live_source_graph=1, policy_violations=1)
    stdout = tmp_path / "worker.log"
    stdout.write_text("live worker\n", encoding="utf-8")
    gate = pl._worker_mcp_live_call_gate(
        _metadata(tmp_path, bundle_sha=_sha(), sections=[], stdout=stdout), "req"
    )
    assert gate["satisfied"] is True
    assert gate["reason"] == ""
    assert gate["policy_warning"] is True
    assert gate["policy_warning_count"] == 1
    assert gate["warnings"] == ["denied_aiworkhub_tool_requests_recovered:1"]


def test_observation_only_task_surfaces_authenticated_policy_warning(tmp_path, monkeypatch):
    _patch_verify(monkeypatch, policy_violations=1)
    stdout = tmp_path / "research-worker.log"
    stdout.write_text("read-only research output\n", encoding="utf-8")
    metadata = _metadata(tmp_path, bundle_sha=_sha(), sections=[], stdout=stdout)
    metadata["project_context"]["task_context_policy"]["task_type"] = "research"

    gate = pl._worker_mcp_live_call_gate(metadata, "req")

    assert gate["gated"] is False
    assert gate["satisfied"] is True
    assert gate["policy_warning"] is True
    assert gate["policy_warning_count"] == 1
    assert gate["verification"]["policy_violations"] == 1


def test_policy_warning_never_overrides_a_missing_required_source_graph_call(tmp_path, monkeypatch):
    _patch_verify(monkeypatch, policy_violations=1)
    stdout = tmp_path / "worker.log"
    stdout.write_text("worker with denied request but no valid source graph call\n", encoding="utf-8")
    gate = pl._worker_mcp_live_call_gate(
        _metadata(tmp_path, bundle_sha=_sha(), sections=[], stdout=stdout), "req"
    )
    assert gate["satisfied"] is False
    assert gate["missing_tools"] == ["source_graph"]
    assert gate["reason"] == "required_aiworkhub_mcp_calls_missing:source_graph"
    assert gate["policy_warning"] is True
    assert gate["policy_warning_count"] == 1


# --- evidence surfaces the satisfaction source per tool --------------------

def test_satisfaction_source_distinguishes_injected_vs_live(tmp_path, monkeypatch):
    # source_graph via a live call, session_current_state via injection.
    _patch_verify(monkeypatch, live_source_graph=1)
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=1)
    sections = _sections(("session_current_state", True, 8, ""))
    gate = pl._worker_mcp_live_call_gate(_metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req")
    assert gate["satisfied"] is True
    assert gate["satisfaction_by_tool"]["source_graph"] == "live_worker_call"
    assert gate["satisfaction_by_tool"]["session_current_state"] == "injected_receipt"


def test_gate_result_never_leaks_paths(tmp_path, monkeypatch):
    _patch_verify(monkeypatch)
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=2)
    sections = _sections(("source_graph", True, 3, ""), ("session_current_state", True, 8, ""))
    gate = pl._worker_mcp_live_call_gate(_metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req")
    blob = json.dumps(gate, default=str)
    assert str(stdout) not in blob
    assert str(tmp_path) not in blob


def test_cached_only_supervisor_section_never_satisfies_orientation(tmp_path, monkeypatch):
    # A ledger full of auditable prefetch/cache observations (zero genuine live
    # calls) must still fail the gate: provenance is auditable, never
    # authoritative. One genuine provider call counts exactly once.
    def fake_verify(*_a, **_k):
        return {
            "ok": True,
            "reason": "",
            "live_source_graph_calls": 0,
            "fresh_source_graph_calls": 0,
            "provenance_counts": {"prefetch": 5, "cache": 3},
            "successful_call_count_by_tool": {"source_graph": 8},
            "policy_violations": 0,
        }

    monkeypatch.setattr(pl.worker_ai_tools_mcp, "verify_audit_ledger", fake_verify)
    bundle = _sha()
    stdout = _write_receipt(tmp_path, bundle, section_count=1)
    sections = _sections(("source_graph", True, 3, "cache_receipt_only"))
    gate = pl._worker_mcp_live_call_gate(
        _metadata(tmp_path, bundle_sha=bundle, sections=sections, stdout=stdout), "req",
    )
    assert gate["satisfied"] is False
    assert gate["missing_tools"] == []
    assert gate["stale_tools"] == ["source_graph"]
    assert gate["satisfaction_by_tool"]["source_graph"] == "stale_or_cached"


# --- Source Graph cache: generation bound, capacity bound, race safety -----
#
# The measured defect: ``_CACHE`` was an unbounded plain dict, so a long-lived
# worker process kept every entry it ever produced -- including entries from
# index generations that a rebuild or exact-file mutation had already
# superseded -- and concurrent tool calls mutated it without a lock. These lock
# the replacement: obsolete generations are evicted deterministically, the
# total stays capped, a late store from a superseded generation can never
# supersede a newer one, an ambiguous (degraded) index identity is never cached
# or replayed at all, and current-generation exact-key hits still return the
# stored entry unchanged.


def _key(
    *,
    task_id: str = "TASK_B954",
    request_id: str = "req-1",
    repo: str = "/repo",
    mode: str = "focus",
    query: str = "q",
    target: str | None = None,
    cursor: str | None = None,
    budget: int = 48,
    bundle_type: str = "explore",
    packet_sha256: str = "",
    overlay_sha256: str = "",
    build_revision: str = "rev-1",
    finished_at: str = "2026-08-24T00:00:00+00:00",
):
    """Build a key through the SAME production helper ``source_graph_query`` uses.

    Deliberately not a hand-copied tuple literal: if the key layout changes,
    these tests move with production instead of silently drifting.
    """
    return wm._source_graph_cache_key(
        task_id=task_id, request_id=request_id, repo=repo, mode=mode,
        query=query, target=target, cursor=cursor, budget=budget,
        bundle_type=bundle_type, packet_sha256=packet_sha256,
        overlay_sha256=overlay_sha256,
        index_identity={"build_revision": build_revision, "finished_at": finished_at},
    )


def _ctx(tmp_path: Path) -> "wm.WorkerToolContext":
    return wm.WorkerToolContext(
        task_id="TASK_B954", runner="claude", topic="cache", request_id="req-1",
        repo=tmp_path, authority_repo=tmp_path,
        source_graph_targets=(), session_topic="cache",
        audit_ledger_path=None, audit_hmac_key_path=None,
    )


def _stub_engine(monkeypatch, *, identity: dict, payloads: list[dict]) -> list[str]:
    """Drive a real ``source_graph_query`` call off a stubbed engine.

    Everything below the cache is replaced -- feature flag, authority binding,
    index identity, database context and the engine call itself -- so what the
    tests observe is the production caching decision, not sqlite behaviour.
    Returns the list of live engine calls actually made.
    """
    from aiworkhub import feature_settings, source_graph

    monkeypatch.setattr(feature_settings, "enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(
        wm, "_resolve_source_graph_db",
        lambda ctx: wm.AuthorityBinding(
            db_path=Path("/nonexistent/source_graph.db"),
            authority_source="canonical",
            authority_state="canonical_active",
            authority_repo=ctx.authority_repo,
        ),
    )
    monkeypatch.setattr(
        wm, "_source_graph_index_identity", lambda *_a, **_k: dict(identity),
    )

    @contextmanager
    def _no_db(*_a, **_k):
        yield

    monkeypatch.setattr(wm, "_with_source_graph_db", _no_db)
    live: list[str] = []

    def _focus(_repo, query, _budget):
        live.append(query)
        return dict(payloads[min(len(live) - 1, len(payloads) - 1)])

    monkeypatch.setattr(source_graph, "focus", _focus)
    return live


# --- the production invocation actually caches under the named key ---------

def test_source_graph_query_caches_under_the_named_production_key(tmp_path, monkeypatch):
    wm._source_graph_cache_clear()
    identity = {"build_revision": "rev-1", "finished_at": "2026-08-24T00:00:00+00:00"}
    # Big enough that the compact replay receipt is genuinely smaller than the
    # cached bytes, so the hit exercises the real receipt path rather than the
    # replay-verbatim fallback.
    payload = {"matches": [{"file": f"pkg/module_{i}.py", "symbol": f"sym_{i}"} for i in range(24)]}
    live = _stub_engine(monkeypatch, identity=identity, payloads=[payload])

    first = wm.source_graph_query(_ctx(tmp_path), mode="focus", query="q", budget=48)
    assert first["cache_hit"] is False
    assert live == ["q"]

    expected = _key(repo=str(tmp_path), build_revision="rev-1", finished_at=identity["finished_at"])
    assert list(wm._CACHE) == [expected]
    assert isinstance(expected, wm._SourceGraphCacheKey)
    assert expected.authority == (
        "source_graph", "TASK_B954", "req-1", str(tmp_path), "", "",
    )
    assert expected.generation == (identity["finished_at"], "rev-1")

    second = wm.source_graph_query(_ctx(tmp_path), mode="focus", query="q", budget=48)
    assert second["cache_hit"] is True
    assert second["cache_receipt"] is True
    assert second["content_sha256"] == first["content_sha256"]
    assert second["replay_original_bytes"] == first["bytes"]
    assert second["hit_count"] == first["hit_count"]
    assert json.loads(second["content"])["content_sha256"] == first["content_sha256"]
    assert live == ["q"], "a current-generation exact-key hit must not re-query"


def test_a_degraded_index_identity_is_never_cached_or_replayed(tmp_path, monkeypatch):
    # Empty ``finished_at`` is what ``_source_graph_index_identity`` returns for
    # an unreadable/missing ``meta`` row: two different index states alias to it,
    # so caching under it could replay pre-mutation bytes as current.
    wm._source_graph_cache_clear()
    live = _stub_engine(
        monkeypatch,
        identity={"build_revision": "rev-1", "finished_at": ""},
        payloads=[{"matches": [{"file": "before.py"}]}, {"matches": [{"file": "after.py"}]}],
    )

    first = wm.source_graph_query(_ctx(tmp_path), mode="focus", query="q", budget=48)
    assert wm._CACHE == {}

    second = wm.source_graph_query(_ctx(tmp_path), mode="focus", query="q", budget=48)
    assert second["cache_hit"] is False
    assert live == ["q", "q"], "an ambiguous generation must fall back to a live query"
    assert "after.py" in second["content"]
    assert second["content_sha256"] != first["content_sha256"]
    assert wm._CACHE == {}


def test_sentinel_and_unreadable_generations_are_all_indefinite():
    definite = wm._source_graph_generation_is_definite
    assert definite({"build_revision": "rev-1", "finished_at": "2026-08-24T00:00:00+00:00"}) is True
    assert definite({"build_revision": "rev-1", "finished_at": ""}) is False
    assert definite({"build_revision": "rev-1", "finished_at": "  "}) is False
    assert definite({"build_revision": "rev-1", "finished_at": "UNKNOWN"}) is False
    assert definite({"build_revision": "rev-1", "finished_at": None}) is False
    assert definite({"build_revision": "", "finished_at": "2026-08-24T00:00:00+00:00"}) is False
    assert definite({}) is False


# --- generation rollover is deterministic and authority-scoped -------------

def test_obsolete_generation_entries_are_evicted_on_rollover():
    wm._source_graph_cache_clear()
    stale = [_key(query=f"q{i}", finished_at="t1") for i in range(5)]
    for key in stale:
        wm._source_graph_cache_store(key, {"content_sha256": "old"})
    assert len(wm._CACHE) == 5

    rolled = _key(query="q0", build_revision="rev-2", finished_at="t2")
    assert wm._source_graph_cache_store(rolled, {"content_sha256": "new"}) is True

    assert list(wm._CACHE) == [rolled]
    for key in stale:
        assert wm._source_graph_cache_get(key) is None
    assert wm._source_graph_cache_get(rolled)["content_sha256"] == "new"


def test_a_changed_build_revision_alone_is_a_rollover():
    wm._source_graph_cache_clear()
    before = _key(build_revision="rev-1", finished_at="t1")
    wm._source_graph_cache_store(before, {"content_sha256": "old"})
    after = _key(build_revision="rev-2", finished_at="t1")
    assert wm._source_graph_cache_store(after, {"content_sha256": "new"}) is True
    assert list(wm._CACHE) == [after]


def test_rollover_eviction_is_authority_scoped():
    wm._source_graph_cache_clear()
    peers = {
        "task": _key(task_id="OTHER_TASK", finished_at="t1"),
        "request": _key(request_id="req-2", finished_at="t1"),
        "repo": _key(repo="/other-repo", finished_at="t1"),
        "packet": _key(packet_sha256="packet-sha", finished_at="t1"),
        "overlay": _key(overlay_sha256="overlay-sha", finished_at="t1"),
    }
    mine = _key(finished_at="t1")
    for key in (*peers.values(), mine):
        wm._source_graph_cache_store(key, {"content_sha256": "old"})

    wm._source_graph_cache_store(_key(finished_at="t2"), {"content_sha256": "new"})

    assert wm._source_graph_cache_get(mine) is None
    for name, key in peers.items():
        assert wm._source_graph_cache_get(key) is not None, f"{name} scope was cross-evicted"


# --- the generation fence: a late old-generation store changes nothing -----

def test_a_late_old_generation_store_never_supersedes_a_newer_one():
    wm._source_graph_cache_clear()
    current = _key(query="q", finished_at="t2")
    wm._source_graph_cache_store(current, {"content_sha256": "new"})

    late = _key(query="q-late", finished_at="t1")
    assert wm._source_graph_cache_store(late, {"content_sha256": "old"}) is False
    assert list(wm._CACHE) == [current]
    assert wm._source_graph_cache_get(late) is None
    assert wm._source_graph_cache_get(current)["content_sha256"] == "new"


def test_a_late_old_generation_store_is_fenced_per_authority():
    # The fence is scoped like the eviction it protects: another authority's
    # older generation is still perfectly current FOR THAT AUTHORITY.
    wm._source_graph_cache_clear()
    wm._source_graph_cache_store(_key(finished_at="t2"), {"content_sha256": "mine-new"})
    peer = _key(request_id="req-2", finished_at="t1")
    assert wm._source_graph_cache_store(peer, {"content_sha256": "peer-old"}) is True
    assert wm._source_graph_cache_get(peer)["content_sha256"] == "peer-old"


def test_concurrent_generation_stores_settle_on_the_newest_deterministically():
    wm._source_graph_cache_clear()
    stamps = [f"t{n:02d}" for n in range(16)]
    start = threading.Barrier(len(stamps))

    def roll(stamp: str) -> None:
        start.wait()
        for i in range(4):
            wm._source_graph_cache_store(
                _key(query=f"q{i}", finished_at=stamp), {"content_sha256": stamp},
            )

    threads = [threading.Thread(target=roll, args=(stamp,)) for stamp in stamps]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Whatever the interleaving, the newest generation wins and no older one
    # survives beside it or replaces it after arriving late.
    assert {key.generation for key in wm._CACHE} == {(stamps[-1], "rev-1")}
    assert len(wm._CACHE) == 4
    assert all(entry["content_sha256"] == stamps[-1] for entry in wm._CACHE.values())


# --- capacity bound, and what it may and may not do -----------------------

def test_total_entries_stay_bounded_and_drop_least_recently_used_first():
    wm._source_graph_cache_clear()
    cap = wm._MAX_SOURCE_GRAPH_CACHE_ENTRIES
    assert cap == 256
    keys = [_key(query=f"q{i}") for i in range(cap * 3)]
    for key in keys:
        wm._source_graph_cache_store(key, {"content_sha256": key.query})

    assert len(wm._CACHE) == cap
    assert list(wm._CACHE) == keys[-cap:]
    assert wm._source_graph_cache_get(keys[0]) is None


def test_capacity_may_evict_another_authority_only_as_a_harmless_miss():
    wm._source_graph_cache_clear()
    cap = wm._MAX_SOURCE_GRAPH_CACHE_ENTRIES
    victim = _key(request_id="req-victim", query="q")
    wm._source_graph_cache_store(victim, {"content_sha256": "victim"})
    for i in range(cap):
        wm._source_graph_cache_store(
            _key(request_id="req-flood", query=f"q{i}"), {"content_sha256": f"flood{i}"},
        )

    assert len(wm._CACHE) == cap
    # A miss, never a crossed read: the evicted authority gets None and the
    # flooding authority's bytes are never handed back under the victim's key.
    assert wm._source_graph_cache_get(victim) is None
    assert all(key.request_id == "req-flood" for key in wm._CACHE)


def test_get_never_returns_another_authoritys_entry():
    wm._source_graph_cache_clear()
    identities = {
        "mine": _key(),
        "task": _key(task_id="OTHER_TASK"),
        "request": _key(request_id="req-2"),
        "repo": _key(repo="/other-repo"),
        "packet": _key(packet_sha256="packet-sha"),
        "overlay": _key(overlay_sha256="overlay-sha"),
    }
    # Same mode/query/budget everywhere: only the authority binding differs.
    for name, key in identities.items():
        wm._source_graph_cache_store(key, {"content_sha256": name})

    for name, key in identities.items():
        assert wm._source_graph_cache_get(key)["content_sha256"] == name
    assert len({key.authority for key in wm._CACHE}) == len(identities)


def test_current_generation_hit_returns_the_stored_entry_unchanged():
    wm._source_graph_cache_clear()
    cap = wm._MAX_SOURCE_GRAPH_CACHE_ENTRIES
    hot = _key(query="hot")
    entry = {
        "result": {"content": "{}", "output_cap_bytes": 8192},
        "hit_count": 3,
        "bytes": 2,
        "content_sha256": _sha("hot-content"),
        "authority_source": "canonical",
        "authority_state": "sole_authority",
        "evidence_counts": {"entity_rows": 1},
    }
    wm._source_graph_cache_store(hot, entry)

    # Reading keeps it hot, so capacity pressure evicts the cold entries first
    # and the receipt fields a cache hit replays stay byte-identical.
    for i in range(cap - 1):
        wm._source_graph_cache_store(_key(query=f"cold{i}"), {"content_sha256": "cold"})
        assert wm._source_graph_cache_get(hot) is entry
    wm._source_graph_cache_store(_key(query="overflow"), {"content_sha256": "overflow"})

    assert len(wm._CACHE) == cap
    replayed = wm._source_graph_cache_get(hot)
    assert replayed is entry
    assert replayed["content_sha256"] == _sha("hot-content")
    assert replayed["hit_count"] == 3
    assert replayed["authority_state"] == "sole_authority"


def test_concurrent_get_and_store_never_duplicate_or_cross_authorities():
    wm._source_graph_cache_clear()
    workers, per_worker = 8, 12
    assert workers * per_worker <= wm._MAX_SOURCE_GRAPH_CACHE_ENTRIES
    mismatches: list[str] = []
    start = threading.Barrier(workers)

    def hammer(n: int) -> None:
        start.wait()
        for i in range(per_worker):
            key = _key(request_id=f"req-{n}", query=f"q{i}")
            expected = f"{n}:{i}"
            wm._source_graph_cache_store(key, {"content_sha256": expected})
            for _ in range(25):
                got = wm._source_graph_cache_get(key)
                if got is None or got["content_sha256"] != expected:
                    mismatches.append(f"{expected} -> {got}")
                    return

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert mismatches == []
    assert len(wm._CACHE) == workers * per_worker
    for n in range(workers):
        for i in range(per_worker):
            key = _key(request_id=f"req-{n}", query=f"q{i}")
            assert wm._CACHE[key]["content_sha256"] == f"{n}:{i}"
