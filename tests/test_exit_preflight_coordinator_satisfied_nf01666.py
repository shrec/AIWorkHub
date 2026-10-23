"""exit_preflight reports only what the post-exit live-call gate still needs (NF-2026-01666)."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import itertools
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import agent_tool_instructions as instr  # noqa: E402
from aiworkhub import process_launcher as pl  # noqa: E402
from aiworkhub import process_launcher_live_call_gate as gate_module  # noqa: E402
from aiworkhub import repo_policy  # noqa: E402
from aiworkhub import worker_ai_tools_mcp as w  # noqa: E402
from aiworkhub import worker_workspace  # noqa: E402

TASK_ID = "TASK_NF01666"
RUNNER = "claude_worker"
TOPIC = "task_mcp"
REQUEST_ID = "req-nf01666"
ALL_TOOLS = ("source_graph", "session_current_state", "ai_memory", "kb")
REQUIRED_KEY = w.PACKET_REQUIRED_MCP_TOOLS_KEY
SATISFIED_KEY = w.PACKET_COORDINATOR_SATISFIED_KEY
MCP_FINDING = "validation_required_aiworkhub_mcp_call_missing"
OLD_ACTION = (
    "no authenticated live aiworkhub_worker_{tool} call is recorded for this "
    "request; if the coordinator's project_context required it, the finalizer "
    "refuses this attempt with validation_required_aiworkhub_mcp_call_missing "
    "even when every validation row is green"
)
OLD_GATE_DETAIL_KEYS = {
    "checked", "ledger_ok", "ledger_reason", "live_source_graph_calls",
    "successful_call_count_by_tool",
}
_BASE_PACKET: dict[str, Any] = {
    "schema_id": worker_workspace.WORKER_CONTRACT_PACKET_SCHEMA,
    "request_id": REQUEST_ID,
    "allowed_writes": [],
    "required_outputs": [],
    "allow_empty_required_outputs": [],
    "allow_unchanged_required_outputs": [],
    "validation": [],
    "validation_roles": [],
    "read_only": False,
    "parent_baseline": {},
    "workspace_baseline": {},
    "inherited_rework_paths": [],
    "residual_contract_manifest": [],
}
_QUERY_IDS = itertools.count()


def _clear_launch_contexts() -> None:
    with gate_module._LAUNCH_CONTEXTS_LOCK:
        gate_module._LAUNCH_CONTEXTS.clear()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    if hasattr(os, "fchmod"):
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)
    w._validation_memo_reset()
    _clear_launch_contexts()
    yield
    _clear_launch_contexts()
    w._validation_memo_reset()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _section(
    name: str, *, executed: bool = True, hits: int = 2, degraded: str = ""
) -> dict[str, Any]:
    return {
        "name": name, "requested": True, "executed": executed,
        "hit_count": hits, "degraded_reason": degraded,
    }


def _launch(
    sections: list[dict[str, Any]] | None = None,
    *,
    task_type: str = "code",
    required: bool = False,
    delivered: bool = True,
    delivered_sha: str | None = None,
    rework: bool = False,
) -> dict[str, Any]:
    bundle_sha = _sha("bundle-nf01666")
    context: dict[str, Any] = {
        "task_context_policy": {"task_type": task_type},
        "bundle_sha256": bundle_sha,
        "sections": [_section(name) for name in ALL_TOOLS] if sections is None else sections,
    }
    if required:
        context["required"] = True
    launch: dict[str, Any] = {
        "project_context": context,
        "rework_predecessor": {"request_id": "req-predecessor"} if rework else None,
    }
    if delivered:
        launch["project_context_delivery"] = {
            "injected": True,
            "bundle_sha256": delivered_sha or bundle_sha,
            "prompt_sha256": _sha("prompt-nf01666"),
        }
    return launch


def _write_policy(tmp_path: Path, **tools: bool) -> None:
    policy = copy.deepcopy(repo_policy.DEFAULT_POLICY)
    policy["tools"].update(tools)
    path = repo_policy.policy_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy), encoding="utf-8")


def _runtime_ctx(tmp_path: Path, **packet_fields: Any) -> w.WorkerToolContext:
    worktree = tmp_path / "worktree"
    (worktree / "tests").mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    runtime = w.generate_worker_mcp_runtime(
        home=home, request_id=REQUEST_ID, task_id=TASK_ID, runner=RUNNER, topic=TOPIC,
        repo=worktree, authority_repo=tmp_path, source_graph_targets=[],
        session_topic=TOPIC, package_import_root=w.resolve_host_package_import_root(),
    )
    packet = worker_workspace.worker_contract_packet_path(home)
    packet.write_text(json.dumps({**_BASE_PACKET, **packet_fields}), encoding="utf-8")
    return w.WorkerToolContext(
        task_id=TASK_ID, runner=RUNNER, topic=TOPIC, request_id=REQUEST_ID,
        repo=worktree, authority_repo=tmp_path, source_graph_targets=(),
        session_topic=TOPIC, audit_ledger_path=runtime.audit_ledger_path,
        audit_hmac_key_path=runtime.audit_hmac_key_path, contract_packet_path=packet,
    )


def _record(
    ctx: w.WorkerToolContext,
    tool: str,
    *,
    ok: bool = True,
    cache_hit: bool = False,
    provenance: str = "",
) -> None:
    payload = None
    if tool == "source_graph":
        payload = {
            "mode": "focus",
            "workflow_stage": "orientation",
            "workflow_stage_source": "declared",
            "query_sha256": _sha(f"query-{next(_QUERY_IDS)}"),
            "index_revision": "aiworkhub.source_graph.semantic.v6",
        }
    appended = w._append_audit(
        ctx, tool=tool, ok=ok, cache_hit=cache_hit, hit_count=1 if ok else 0,
        bytes_returned=64, authority_source="canonical",
        authority_state="canonical_active", payload=payload, provenance=provenance,
    )
    assert appended is True


def _record_rows(ctx: w.WorkerToolContext, rows: tuple[tuple[Any, ...], ...]) -> None:
    for tool, ok, cache_hit, provenance in rows:
        _record(ctx, tool, ok=ok, cache_hit=cache_hit, provenance=provenance)


def _mcp_findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in result["findings"] if f["failure_class"] == MCP_FINDING]


def _tools(findings: list[dict[str, Any]]) -> set[str]:
    return {f["tool"] for f in findings}


def _gate_metadata(ctx: w.WorkerToolContext, launch: dict[str, Any]) -> dict[str, Any]:
    return {
        **launch,
        "task_id": ctx.task_id, "runner": ctx.runner, "topic": ctx.topic,
        "request_id": ctx.request_id, "stdout_path": "",
        "worker_mcp": {
            "authority_repo": str(ctx.authority_repo),
            "audit_ledger_path": str(ctx.audit_ledger_path),
            "audit_hmac_key_path": str(ctx.audit_hmac_key_path),
        },
    }


def _card(**extra: Any) -> dict[str, Any]:
    return {"request_id": REQUEST_ID, **extra}


def _context_result(launch: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(metadata=launch["project_context"])


def _hand_off(
    ctx: w.WorkerToolContext, launch: dict[str, Any], card: dict[str, Any] | None = None
) -> dict[str, Any]:
    gate_module._remember_launch_context(card or _card(), _context_result(launch))
    gate_module._apply_launch_expectation(
        SimpleNamespace(home=ctx.contract_packet_path.parent), REQUEST_ID, ctx.authority_repo
    )
    return json.loads(ctx.contract_packet_path.read_text(encoding="utf-8"))


# (a) every required tool is coordinator-satisfied and nothing was called live.


def test_all_four_tools_coordinator_satisfied_with_zero_live_calls_gives_no_finding(
    tmp_path: Path,
) -> None:
    ctx = _runtime_ctx(
        tmp_path, **{REQUIRED_KEY: list(ALL_TOOLS), SATISFIED_KEY: list(ALL_TOOLS)}
    )

    result = w.exit_preflight(ctx)

    detail = result["worker_mcp_gate"]
    assert detail["checked"] is True
    assert detail["ledger_ok"] is True
    assert detail["live_source_graph_calls"] == 0
    assert not set(ALL_TOOLS) & set(detail["successful_call_count_by_tool"])
    assert detail[REQUIRED_KEY] == sorted(ALL_TOOLS)
    assert detail[SATISFIED_KEY] == sorted(ALL_TOOLS)
    assert _mcp_findings(result) == []
    assert result["finding_count"] == 0


# (b) a required tool the coordinator did not satisfy is still reported.


@pytest.mark.parametrize("unsatisfied", ALL_TOOLS)
def test_a_required_tool_the_coordinator_did_not_satisfy_is_still_reported(
    tmp_path: Path, unsatisfied: str
) -> None:
    satisfied = [tool for tool in ALL_TOOLS if tool != unsatisfied]
    ctx = _runtime_ctx(tmp_path, **{REQUIRED_KEY: list(ALL_TOOLS), SATISFIED_KEY: satisfied})

    findings = _mcp_findings(w.exit_preflight(ctx))

    assert len(findings) == 1
    assert findings[0]["tool"] == unsatisfied
    assert findings[0]["action"] == OLD_ACTION.format(tool=unsatisfied)


def test_only_required_tools_are_reported(tmp_path: Path) -> None:
    ctx = _runtime_ctx(tmp_path, **{REQUIRED_KEY: ["ai_memory", "kb"], SATISFIED_KEY: ["kb"]})

    assert _tools(_mcp_findings(w.exit_preflight(ctx))) == {"ai_memory"}


def test_a_live_call_still_clears_a_required_tool_nothing_satisfied(tmp_path: Path) -> None:
    ctx = _runtime_ctx(tmp_path, **{REQUIRED_KEY: list(ALL_TOOLS), SATISFIED_KEY: []})
    _record(ctx, "ai_memory")
    _record(ctx, "source_graph", provenance="live")

    assert _tools(_mcp_findings(w.exit_preflight(ctx))) == {"session_current_state", "kb"}


def test_the_satisfied_list_counts_only_while_the_ledger_verifies(tmp_path: Path) -> None:
    ctx = dataclasses.replace(
        _runtime_ctx(
            tmp_path, **{REQUIRED_KEY: list(ALL_TOOLS), SATISFIED_KEY: list(ALL_TOOLS)}
        ),
        audit_ledger_path=tmp_path / "missing-ledger.jsonl",
    )

    result = w.exit_preflight(ctx)

    assert result["worker_mcp_gate"]["ledger_ok"] is False
    assert _tools(_mcp_findings(result)) == set(ALL_TOOLS)


def test_an_empty_required_list_judges_nothing(tmp_path: Path) -> None:
    ctx = _runtime_ctx(tmp_path, **{REQUIRED_KEY: [], SATISFIED_KEY: []})

    result = w.exit_preflight(ctx)

    assert _mcp_findings(result) == []
    assert result["worker_mcp_gate"][REQUIRED_KEY] == []


# (c) the worker-side findings equal the post-exit gate's missing set.

SG_LIVE = ("source_graph", True, False, "live")
SG_FAILED = ("source_graph", False, False, "live")
SG_CACHED = ("source_graph", True, True, "cache")
MEMORY_LIVE = ("ai_memory", True, False, "")

_ZERO_HIT = [
    _section("source_graph", hits=3),
    _section("session_current_state", hits=0),
    _section("ai_memory", hits=0),
    _section("kb", hits=0),
]
_DEGRADED_MEMORY = [
    _section("source_graph"),
    _section("session_current_state"),
    _section("ai_memory", degraded="timeout"),
    _section("kb"),
]
_RESEARCH = [_section("source_graph"), _section("kb")]
_NO_SOURCE_GRAPH_POLICY = {"source_graph_required_for_code": False}
_NO_MEMORY_POLICY = {"session_memory_kb_required_for_nontrivial": False}

SCENARIOS = [
    pytest.param(_launch(), (), set(), None, id="bound_and_nothing_called"),
    pytest.param(_launch(delivered=False), (), set(ALL_TOOLS), None, id="bundle_not_delivered"),
    pytest.param(
        _launch(delivered_sha=_sha("other")), (), set(ALL_TOOLS), None,
        id="delivered_sha_differs",
    ),
    pytest.param(
        _launch(_DEGRADED_MEMORY), (), {"ai_memory"}, None, id="degraded_section_stays_required"
    ),
    pytest.param(
        _launch(_ZERO_HIT, delivered=False), (), {"source_graph"}, None,
        id="zero_hit_sections_credit_all_but_source_graph",
    ),
    pytest.param(
        _launch(delivered=False, rework=True), (), {"source_graph"}, None,
        id="rework_credits_all_but_source_graph",
    ),
    pytest.param(
        _launch(delivered=False), (SG_LIVE, MEMORY_LIVE), {"session_current_state", "kb"}, None,
        id="live_calls_clear_their_own_tools",
    ),
    pytest.param(
        _launch(), (SG_FAILED,), {"source_graph"}, None,
        id="failed_source_graph_voids_orientation",
    ),
    pytest.param(_launch(), (SG_FAILED, SG_LIVE), set(), None, id="live_call_after_a_failed_one"),
    pytest.param(
        _launch(), (SG_FAILED, SG_CACHED), set(), None,
        id="failed_then_cached_keeps_orientation",
    ),
    pytest.param(_launch(), (SG_CACHED,), set(), None, id="cached_source_graph_keeps_orientation"),
    pytest.param(
        _launch(_ZERO_HIT, delivered=False), (SG_CACHED,), {"source_graph"}, None,
        id="cached_source_graph_alone_is_stale",
    ),
    pytest.param(
        _launch(task_type="research", delivered=False), (), set(), None,
        id="research_without_required_context",
    ),
    pytest.param(
        _launch(_RESEARCH, task_type="research", required=True), (), set(), None,
        id="required_research_bound",
    ),
    pytest.param(
        _launch(_RESEARCH, task_type="research", required=True, delivered=False), (),
        {"source_graph", "kb"}, None, id="required_research_unbound",
    ),
    pytest.param(
        _launch(), (), set(),
        {**_NO_SOURCE_GRAPH_POLICY, **_NO_MEMORY_POLICY}, id="policy_requires_nothing",
    ),
    pytest.param(
        _launch(delivered=False), (), {"source_graph"}, _NO_MEMORY_POLICY,
        id="policy_requires_only_source_graph",
    ),
    pytest.param(
        _launch(), (), set(), _NO_SOURCE_GRAPH_POLICY, id="policy_without_source_graph_bound"
    ),
]


@pytest.mark.parametrize(("launch", "rows", "expected", "policy"), SCENARIOS)
def test_worker_findings_equal_the_post_exit_gate_missing_set(
    tmp_path: Path,
    launch: dict[str, Any],
    rows: tuple[tuple[Any, ...], ...],
    expected: set[str],
    policy: dict[str, bool] | None,
) -> None:
    if policy is not None:
        _write_policy(tmp_path, **policy)
    expectation = gate_module._launch_gate_expectation(launch, tmp_path)
    assert expectation is not None
    ctx = _runtime_ctx(tmp_path, **expectation)
    _record_rows(ctx, rows)

    gate = gate_module._worker_mcp_live_call_gate(_gate_metadata(ctx, launch), REQUEST_ID)
    findings = _mcp_findings(w.exit_preflight(ctx))

    worker_tools = _tools(findings)
    gate_tools = set(gate["missing_tools"]) | set(gate.get("stale_tools") or [])
    assert len(findings) == len(worker_tools)
    assert worker_tools == gate_tools == expected
    assert gate["satisfied"] is (not expected)
    judged = set(gate["satisfaction_by_tool"]) | set(gate["missing_tools"])
    assert judged == set(expectation[REQUIRED_KEY])


def test_a_cached_only_source_graph_is_stale_not_missing_and_still_reported(
    tmp_path: Path,
) -> None:
    launch = _launch(_ZERO_HIT, delivered=False)
    ctx = _runtime_ctx(tmp_path, **gate_module._launch_gate_expectation(launch, tmp_path))
    _record_rows(ctx, (SG_CACHED,))

    gate = gate_module._worker_mcp_live_call_gate(_gate_metadata(ctx, launch), REQUEST_ID)

    assert gate["stale_tools"] == ["source_graph"]
    assert gate["missing_tools"] == []
    assert _tools(_mcp_findings(w.exit_preflight(ctx))) == {"source_graph"}


def test_an_unverifiable_ledger_credits_nothing_in_either_gate(tmp_path: Path) -> None:
    launch = _launch()
    ctx = dataclasses.replace(
        _runtime_ctx(tmp_path, **gate_module._launch_gate_expectation(launch, tmp_path)),
        audit_ledger_path=tmp_path / "missing-ledger.jsonl",
    )

    gate = gate_module._worker_mcp_live_call_gate(_gate_metadata(ctx, launch), REQUEST_ID)
    result = w.exit_preflight(ctx)

    assert gate["satisfied"] is False
    assert set(gate["missing_tools"]) == set(ALL_TOOLS)
    assert result["worker_mcp_gate"]["ledger_ok"] is False
    assert _tools(_mcp_findings(result)) == set(ALL_TOOLS)


@pytest.mark.parametrize(
    "failed",
    ["garbage", {"source_graph": "garbage"}, 0],
    ids=["not_a_mapping", "not_a_number", "bare_zero"],
)
def test_a_malformed_failure_record_withholds_source_graph_credit_in_both(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failed: Any
) -> None:
    launch = _launch()
    ctx = _runtime_ctx(tmp_path, **gate_module._launch_gate_expectation(launch, tmp_path))
    verification = {
        "ok": True, "reason": "", "live_source_graph_calls": 0,
        "successful_call_count_by_tool": {}, "failed_call_count_by_tool": failed,
        "policy_violations": 0, "receipt_conformance": {"blocking": False},
    }
    monkeypatch.setattr(w, "verify_audit_ledger", lambda *args, **kwargs: dict(verification))

    gate = gate_module._worker_mcp_live_call_gate(_gate_metadata(ctx, launch), REQUEST_ID)
    result = w.exit_preflight(ctx)

    assert gate["missing_tools"] == ["source_graph"]
    assert (
        gate["satisfaction_by_tool"]["source_graph"]
        == gate_module._SOURCE_GRAPH_LIVE_CALLS_ALL_FAILED
    )
    assert _tools(_mcp_findings(result)) == {"source_graph"}


def test_an_invalid_repo_policy_leaves_the_packet_unsealed_and_the_gate_refuses(
    tmp_path: Path,
) -> None:
    ctx = _runtime_ctx(tmp_path)
    launch = _launch()
    policy = repo_policy.policy_path(tmp_path)
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("{not json", encoding="utf-8")

    assert gate_module._launch_gate_expectation(launch, tmp_path) is None
    packet = _hand_off(ctx, launch)
    verdict = gate_module._worker_mcp_live_call_gate(_gate_metadata(ctx, launch), REQUEST_ID)

    assert REQUIRED_KEY not in packet
    assert SATISFIED_KEY not in packet
    assert verdict["satisfied"] is False
    assert verdict["reason"].startswith("repo_policy_invalid:")


# (d) a packet without the new lists behaves exactly as before.


def test_a_packet_without_the_lists_reports_every_gateable_tool_exactly_as_before(
    tmp_path: Path,
) -> None:
    ctx = _runtime_ctx(tmp_path)

    result = w.exit_preflight(ctx)

    findings = _mcp_findings(result)
    assert [f["tool"] for f in findings] == list(ALL_TOOLS)
    for finding in findings:
        assert finding == {
            "failure_class": MCP_FINDING,
            "path": "",
            "tool": finding["tool"],
            "action": OLD_ACTION.format(tool=finding["tool"]),
        }
    assert set(result["worker_mcp_gate"]) == OLD_GATE_DETAIL_KEYS
    assert result["finding_count"] == len(ALL_TOOLS)


def test_a_packet_without_the_lists_still_honours_live_calls(tmp_path: Path) -> None:
    ctx = _runtime_ctx(tmp_path)
    _record(ctx, "source_graph", provenance="live")
    _record(ctx, "ai_memory")

    assert _tools(_mcp_findings(w.exit_preflight(ctx))) == {"session_current_state", "kb"}


@pytest.mark.parametrize(
    "fields",
    [
        {REQUIRED_KEY: "source_graph"},
        {REQUIRED_KEY: {"source_graph": 1}},
        {REQUIRED_KEY: None},
        {SATISFIED_KEY: ["kb", 7]},
        {SATISFIED_KEY: "kb"},
        {REQUIRED_KEY: ["kb", 7], SATISFIED_KEY: [["kb"]]},
    ],
    ids=[
        "str_required", "dict_required", "null_required", "mixed_satisfied",
        "str_satisfied", "both",
    ],
)
def test_a_malformed_list_is_ignored_like_an_absent_one(
    tmp_path: Path, fields: dict[str, Any]
) -> None:
    result = w.exit_preflight(_runtime_ctx(tmp_path, **fields))

    assert [f["tool"] for f in _mcp_findings(result)] == list(ALL_TOOLS)
    assert set(result["worker_mcp_gate"]) == OLD_GATE_DETAIL_KEYS


def test_unknown_names_in_a_list_are_ignored(tmp_path: Path) -> None:
    ctx = _runtime_ctx(tmp_path, **{REQUIRED_KEY: ["kb", "bogus"]})

    assert _tools(_mcp_findings(w.exit_preflight(ctx))) == {"kb"}


def test_a_satisfied_list_alone_never_narrows_the_findings(tmp_path: Path) -> None:
    ctx = _runtime_ctx(tmp_path, **{SATISFIED_KEY: ["source_graph", "kb"]})

    assert _tools(_mcp_findings(w.exit_preflight(ctx))) == set(ALL_TOOLS)


# The launch hand-off that seals the lists into the packet.


def test_launch_hand_off_seals_both_lists_into_the_packet(tmp_path: Path) -> None:
    ctx = _runtime_ctx(tmp_path)

    packet = _hand_off(ctx, _launch())

    assert packet[REQUIRED_KEY] == list(ALL_TOOLS)
    assert packet[SATISFIED_KEY] == list(ALL_TOOLS)
    sealed = {k: v for k, v in packet.items() if k not in (REQUIRED_KEY, SATISFIED_KEY)}
    assert sealed == _BASE_PACKET
    assert _mcp_findings(w.exit_preflight(ctx)) == []


@pytest.mark.parametrize(
    ("card_extra", "credited"),
    [({}, False), ({"rework_predecessor": {"request_id": "req-predecessor"}}, True)],
    ids=["fresh", "rework"],
)
def test_the_cards_rework_predecessor_reaches_the_credit(
    tmp_path: Path, card_extra: dict[str, Any], credited: bool
) -> None:
    ctx = _runtime_ctx(tmp_path)

    packet = _hand_off(ctx, _launch(_DEGRADED_MEMORY), _card(**card_extra))

    assert ("ai_memory" in packet[SATISFIED_KEY]) is credited
    assert _tools(_mcp_findings(w.exit_preflight(ctx))) == (set() if credited else {"ai_memory"})


def test_the_parked_launch_is_applied_once(tmp_path: Path) -> None:
    ctx = _runtime_ctx(tmp_path)
    _hand_off(ctx, _launch())
    assert REQUEST_ID not in gate_module._LAUNCH_CONTEXTS
    ctx.contract_packet_path.write_text(json.dumps(_BASE_PACKET), encoding="utf-8")

    gate_module._apply_launch_expectation(
        SimpleNamespace(home=ctx.contract_packet_path.parent), REQUEST_ID, tmp_path
    )

    assert REQUIRED_KEY not in json.loads(ctx.contract_packet_path.read_text(encoding="utf-8"))


def test_a_request_nothing_was_parked_for_is_left_alone(tmp_path: Path) -> None:
    ctx = _runtime_ctx(tmp_path)
    gate_module._remember_launch_context(_card(request_id="req-other"), _context_result(_launch()))

    gate_module._apply_launch_expectation(
        SimpleNamespace(home=ctx.contract_packet_path.parent), REQUEST_ID, tmp_path
    )

    assert json.loads(ctx.contract_packet_path.read_text(encoding="utf-8")) == _BASE_PACKET
    assert "req-other" in gate_module._LAUNCH_CONTEXTS


def test_a_launch_with_no_sealed_packet_changes_nothing_and_does_not_raise(
    tmp_path: Path,
) -> None:
    home = tmp_path / "no-such-home"
    gate_module._remember_launch_context(_card(), _context_result(_launch()))

    gate_module._apply_launch_expectation(SimpleNamespace(home=home), REQUEST_ID, tmp_path)

    assert not worker_workspace.worker_contract_packet_path(home).exists()
    assert REQUEST_ID not in gate_module._LAUNCH_CONTEXTS


@pytest.mark.parametrize(
    ("card", "context_result"),
    [
        (None, None),
        ({}, SimpleNamespace(metadata={})),
        ({"request_id": REQUEST_ID}, None),
        ({"request_id": REQUEST_ID}, SimpleNamespace()),
        ({"request_id": REQUEST_ID}, SimpleNamespace(metadata="not a mapping")),
    ],
    ids=["no_card", "no_request_id", "no_result", "no_metadata", "metadata_not_a_mapping"],
)
def test_input_that_is_not_a_real_launch_parks_nothing(
    card: Any, context_result: Any
) -> None:
    gate_module._remember_launch_context(card, context_result)

    assert not gate_module._LAUNCH_CONTEXTS


def test_parked_launches_are_bounded_oldest_first() -> None:
    cap = gate_module._LAUNCH_CONTEXT_CAP
    result = _context_result(_launch())
    for index in range(cap + 8):
        gate_module._remember_launch_context({"request_id": f"req-{index}"}, result)

    assert list(gate_module._LAUNCH_CONTEXTS) == [f"req-{index}" for index in range(8, cap + 8)]


# Wiring into the launcher.


def test_launch_project_context_parks_what_it_collected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = _launch()["project_context"]
    monkeypatch.setattr(
        pl.project_context, "collect_project_context",
        lambda repo, card: SimpleNamespace(metadata=context),
    )

    result = pl._launch_project_context(tmp_path, _card(), None)

    assert result.metadata == context
    assert gate_module._LAUNCH_CONTEXTS[REQUEST_ID]["project_context"] == context
    _clear_launch_contexts()
    assert pl._launch_project_context(tmp_path, _card(), {"reviewer": "bound"}) is None
    assert not gate_module._LAUNCH_CONTEXTS


def test_provisioning_seals_the_expectation_before_the_runtime_is_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx = _runtime_ctx(tmp_path)
    gate_module._remember_launch_context(_card(), _context_result(_launch()))
    seen: dict[str, Any] = {}

    def fake_provision(workspace: Any, *, authority_repo: Path, **kwargs: Any) -> str:
        packet_path = worker_workspace.worker_contract_packet_path(workspace.home)
        seen["packet"] = json.loads(packet_path.read_text(encoding="utf-8"))
        return "runtime"

    monkeypatch.setattr(pl, "provision_worker_mcp_runtime", fake_provision)

    runtime = pl._provision_worker_mcp_runtime_for_authority(
        SimpleNamespace(home=ctx.contract_packet_path.parent),
        request_id=REQUEST_ID, task_id=TASK_ID, runner=RUNNER, topic=TOPIC,
        backend="direct", authority_repo=tmp_path, source_graph_targets=[],
        session_topic=TOPIC,
    )

    assert runtime == "runtime"
    assert seen["packet"][REQUIRED_KEY] == list(ALL_TOOLS)
    assert seen["packet"][SATISFIED_KEY] == list(ALL_TOOLS)


@pytest.mark.parametrize(
    "name",
    [
        "_project_context_delivery",
        "_remember_launch_context",
        "_apply_launch_expectation",
        "_worker_mcp_live_call_gate",
    ],
)
def test_process_launcher_re_exports_the_gate_modules_own_definitions(name: str) -> None:
    assert getattr(pl, name) is getattr(gate_module, name)


def test_packet_wire_names_are_the_ones_the_card_names() -> None:
    assert REQUIRED_KEY == "required_live_mcp_tools"
    assert SATISFIED_KEY == "coordinator_satisfied_tools"


def test_policy_says_a_clean_validation_run_makes_a_separate_preflight_unnecessary() -> None:
    text = " ".join(instr.render_worker_runtime_policy().split())

    assert (
        "A separate call is unnecessary when validation_run(index='all') already "
        "returned an exit_preflight with finding_count 0."
    ) in text
