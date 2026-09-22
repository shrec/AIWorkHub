"""Compact-by-default summary folds for four oversized manager MCP tools.

Measured on a real manager session: aiworkhub_environment_preflight 35-39KB
per call, aiworkhub_task_show detail=summary 15-25KB, aiworkhub_agent_task_status
detail=summary 14-22KB (embedded task_card alone 11.3KB), aiworkhub_manager_
workforce_rank ~170KB (79 candidates, 67 excluded, each carrying full
score_components). These tests pin synthetic fixtures shaped like those
measurements through the compact "summary" default of each tool and assert
the new size bounds, that every summary carries detail + detail_request, and
that detail="full" (and, where applicable, "evidence") stay byte-for-byte
today's payload.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import mcp_summary_folds, server  # noqa: E402

_PREFLIGHT_SUMMARY_MAX_BYTES = 8 * 1024
_TASK_SHOW_SUMMARY_MAX_BYTES = 8 * 1024
_AGENT_TASK_STATUS_SUMMARY_MAX_BYTES = 6 * 1024
_WORKFORCE_RANK_SUMMARY_MAX_BYTES = 20 * 1024


def _filler(n: int) -> str:
    return "x" * n


# ---------------------------------------------------------------------------
# mcp_summary_folds unit coverage
# ---------------------------------------------------------------------------


def test_fold_terminal_failure_keeps_the_facts_a_reviewer_reads() -> None:
    folded = mcp_summary_folds.fold_terminal_failure({
        "substatus": "validation_failed",
        "reason": "pytest failed",
        "evidence": {
            "failure_class": "defect",
            "error": "AssertionError: x != y",
            "stderr_tail": _filler(5000),
        },
    })

    assert folded["summarized"] is True
    assert len(folded["sha256"]) == 64
    assert folded["substatus"] == "validation_failed"
    assert folded["reason"] == "pytest failed"
    assert folded["category"] == "defect"
    assert folded["first_error"] == "AssertionError: x != y"
    assert len(json.dumps(folded)) < 1000


def test_fold_terminal_failure_is_not_a_dict_passes_through() -> None:
    assert mcp_summary_folds.fold_terminal_failure(None) is None
    assert mcp_summary_folds.fold_terminal_failure("x") == "x"


def test_fold_rework_predecessor_reports_delta_reused_outcome() -> None:
    folded = mcp_summary_folds.fold_rework_predecessor({
        "schema_id": "aiworkhub.rework_predecessor.v1",
        "task_id": "T1",
        "request_id": "req-1",
        "changed_path_hashes": {"a.py": "sha"},
        "rework_delta": {"schema_id": "aiworkhub.rework_delta_descriptor.v1"},
        "workspace": {"filler": _filler(3000)},
    })

    assert folded["summarized"] is True
    assert folded["task_id"] == "T1"
    assert folded["request_id"] == "req-1"
    assert folded["outcome"] == "delta_reused"
    assert folded["reason"] == "sealed delta artifact reused"


def test_fold_rework_predecessor_reports_pinned_no_delta_outcome() -> None:
    folded = mcp_summary_folds.fold_rework_predecessor({
        "request_id": "req-2",
        "changed_path_hashes": {"a.py": "sha"},
    })

    assert folded["outcome"] == "pinned_no_delta"
    assert folded["task_id"] is None


def test_fold_rework_predecessor_reports_no_changes_outcome() -> None:
    folded = mcp_summary_folds.fold_rework_predecessor({"request_id": "req-3"})

    assert folded["outcome"] == "no_changes"


def test_fold_task_card_facts_folds_only_the_named_three_fields() -> None:
    card = {
        "task_id": "T1",
        "objective": "read me exactly",
        "terminal_failure": {"substatus": "validation_failed"},
        "rework_predecessor": {"request_id": "req-1"},
        "template_provenance": {
            "schema_id": "aiworkhub.template_provenance.v1",
            "expanded_contract_digest": "d" * 64,
            "expanded_contract": {"allowed_writes": ["a.py"], "filler": _filler(500)},
        },
    }

    folded = mcp_summary_folds.fold_task_card_facts(card)

    assert folded["objective"] == "read me exactly"
    assert folded["terminal_failure"]["summarized"] is True
    assert folded["rework_predecessor"]["summarized"] is True
    assert folded["template_provenance"]["schema_id"] == "aiworkhub.template_provenance.v1"
    assert folded["template_provenance"]["expanded_contract"]["summarized"] is True
    assert "allowed_writes" not in folded["template_provenance"]["expanded_contract"]
    # The stored card is never mutated by the render.
    assert isinstance(card["terminal_failure"], dict) and "summarized" not in card["terminal_failure"]


def test_fold_task_card_contract_folds_static_text_and_counts_allowed_writes() -> None:
    card = {
        "task_id": "T1",
        "title": "Fixture",
        "status": "finished",
        "substatus": "review_ready",
        "runner": "claude_sonnet-5",
        "topic": "coding",
        "objective": _filler(2000),
        "acceptance": ["a", "b"],
        "allowed_writes": ["src/x.py", "src/y.py", "tests/test_x.py"],
    }

    folded = mcp_summary_folds.fold_task_card_contract(card)

    assert folded["objective"]["summarized"] is True
    assert folded["acceptance"]["summarized"] is True
    assert folded["allowed_writes"] == {"summarized": True, "count": 3}
    # Identity/lifecycle fields are untouched.
    assert folded["task_id"] == "T1"
    assert folded["title"] == "Fixture"
    assert folded["status"] == "finished"
    assert folded["substatus"] == "review_ready"
    assert folded["runner"] == "claude_sonnet-5"
    assert folded["topic"] == "coding"


def test_fold_preflight_summary_filters_providers_and_observability_by_adapter() -> None:
    report = {
        "ok": True,
        "providers": [
            {"adapter_id": "claude_cli", "status": "ready", "launchable": True},
            {"adapter_id": "codex_cli", "status": "unavailable", "launchable": False, "reason": "x"},
        ],
        "provider_observability": {
            "quota_observable_any": False,
            "adapters": [
                {"adapter_id": "claude_cli", "quota_observability_reason": "x"},
                {"adapter_id": "codex_cli", "quota_observability_reason": "y"},
            ],
        },
    }

    unfiltered = mcp_summary_folds.fold_preflight_summary(report, adapter_id=None)
    assert unfiltered["providers"] == {"claude_cli": "ready", "codex_cli": "unavailable"}
    assert len(unfiltered["provider_observability"]["adapters"]) == 2

    filtered = mcp_summary_folds.fold_preflight_summary(report, adapter_id="claude_cli")
    assert filtered["providers"] == {"claude_cli": "ready"}
    assert [row["adapter_id"] for row in filtered["provider_observability"]["adapters"]] == ["claude_cli"]


def test_fold_preflight_summary_folds_refresh_job_report_to_identity_plus_state() -> None:
    report = {
        "source_graph": {
            "ready_for_code": True,
            "refresh_job": {"state": "running", "report": {"filler": _filler(4000)}},
        },
    }

    folded = mcp_summary_folds.fold_preflight_summary(report, adapter_id=None)

    refresh_job = folded["source_graph"]["refresh_job"]
    assert refresh_job["state"] == "running"
    assert refresh_job["report"]["summarized"] is True
    assert len(refresh_job["report"]["sha256"]) == 64


def test_fold_workforce_rank_summary_reduces_excluded_and_counts_reasons() -> None:
    result = {
        "ok": True,
        "selected_worker_id": "w0",
        "candidates": [
            {"worker_id": "w0", "adapter_id": "a", "model": "m0", "excluded": False, "score_components": {"x": 1}},
            {
                "worker_id": "w1", "adapter_id": "a", "model": "m1", "excluded": True,
                "exclusion_reasons": ["worker_unavailable"], "score_components": {"x": 2},
            },
            {
                "worker_id": "w2", "adapter_id": "b", "model": "m2", "excluded": True,
                "exclusion_reasons": ["worker_unavailable", "risk_too_high"], "score_components": {"x": 3},
            },
        ],
    }

    folded = mcp_summary_folds.fold_workforce_rank_summary(result)

    assert [row["worker_id"] for row in folded["candidates"]] == ["w0"]
    assert folded["candidates"][0]["score_components"] == {"x": 1}
    assert {row["worker_id"] for row in folded["excluded_candidates"]} == {"w1", "w2"}
    assert set(folded["excluded_candidates"][0]) == {
        "worker_id", "adapter_id", "model",
    }
    assert folded["excluded_candidates_by_reason"] == {
        "worker_unavailable": 2, "risk_too_high": 1,
    }
    assert folded["selected_worker_id"] == "w0"


# ---------------------------------------------------------------------------
# aiworkhub_task_show
# ---------------------------------------------------------------------------


def _task_show_card() -> dict:
    return {
        "task_id": "TASK_SIZE_FIXTURE",
        "title": "Synthetic size fixture",
        "status": "finished",
        "substatus": "validation_failed",
        "runner": "claude_sonnet-5",
        "topic": "coding",
        "objective": _filler(3500),
        "acceptance": ["fact " + str(i) for i in range(5)],
        "validation": ["python3 -m pytest -q"],
        "read_first": ["src/aiworkhub/core.py"],
        "allowed_writes": ["src/aiworkhub/core.py", "tests/test_x.py"],
        "terminal_failure": {
            "substatus": "validation_failed",
            "reason": "pytest failed",
            "evidence": {
                "failure_class": "defect",
                "error": "AssertionError: x != y",
                "stderr_tail": _filler(7700),
            },
        },
        "rework_predecessor": {
            "schema_id": "aiworkhub.rework_predecessor.v1",
            "request_id": "req-0001",
            "workspace": {"path": "/tmp/x", "request_id": "req-0001", "filler": _filler(3000)},
            "changed_path_hashes": {"a.py": "sha", "b.py": "sha2"},
            "residual_identities": [],
            "pinned_at": 123.0,
        },
        "template_provenance": {
            "schema_id": "aiworkhub.template_provenance.v1",
            "expanded_contract_digest": "d" * 64,
            "expanded_contract": {
                "allowed_writes": ["a.py"],
                "validation": ["cmd"],
                "filler": _filler(1700),
            },
        },
        "terminal_review": {
            "evidence": {
                "validation": [{"command": "pytest", "returncode": 0}],
                # A sub-key CARD_EVIDENCE_PATHS does not list -- must fold
                # generically, not just the six named paths.
                "acceptance_review": {"verdict": "accepted", "filler": _filler(4792)},
            },
        },
    }


def _envelope(card: dict) -> dict:
    return {
        "ok": True,
        "returncode": 0,
        "command": ["show", card.get("task_id", "")],
        "stdout": json.dumps(card),
        "stderr": "",
    }


def test_task_show_summary_folds_the_three_named_fields_and_stays_bounded(monkeypatch) -> None:
    card = _task_show_card()
    monkeypatch.setattr(server.core, "show_task", lambda task_id, full=False: _envelope(card))

    result = server.aiworkhub_task_show("TASK_SIZE_FIXTURE")

    assert result["detail"] == "summary"
    assert result["detail_request"]["evidence"]["detail"] == "evidence"
    assert result["detail_request"]["full"]["detail"] == "full"
    stdout_bytes = result["stdout"].encode("utf-8")
    assert len(stdout_bytes) <= _TASK_SHOW_SUMMARY_MAX_BYTES, len(stdout_bytes)

    rendered = json.loads(result["stdout"])
    assert rendered["terminal_failure"]["summarized"] is True
    assert rendered["terminal_failure"]["substatus"] == "validation_failed"
    assert rendered["terminal_failure"]["category"] == "defect"
    assert rendered["rework_predecessor"]["summarized"] is True
    assert rendered["rework_predecessor"]["request_id"] == "req-0001"
    assert rendered["template_provenance"]["expanded_contract"]["summarized"] is True
    # terminal_review.evidence sub-keys CARD_EVIDENCE_PATHS doesn't list fold too.
    assert rendered["terminal_review"]["evidence"]["acceptance_review"]["summarized"] is True
    # objective is not in this tool's fold list and stays exact.
    assert rendered["objective"] == card["objective"]


def test_task_show_evidence_and_full_keep_the_three_fields_exact(monkeypatch) -> None:
    card = _task_show_card()
    monkeypatch.setattr(server.core, "show_task", lambda task_id, full=False: _envelope(card))

    evidence = server.aiworkhub_task_show("TASK_SIZE_FIXTURE", detail="evidence")
    assert json.loads(evidence["stdout"]) == card

    full = server.aiworkhub_task_show("TASK_SIZE_FIXTURE", detail="full")
    assert full == _envelope(card)

    full_flag = server.aiworkhub_task_show("TASK_SIZE_FIXTURE", full=True)
    assert full_flag == _envelope(card)


# ---------------------------------------------------------------------------
# aiworkhub_agent_task_status
# ---------------------------------------------------------------------------


def _agent_task_status_result() -> dict:
    card = _task_show_card()
    card.update({
        "project_context": {"bundle_bytes": 500, "bundle_sha256": "e" * 64, "filler": _filler(900)},
        "forbidden": ["secrets/**"],
        "required_outputs": ["src/aiworkhub/core.py"],
        "mandatory_changed_outputs": [],
    })
    return {
        "ok": True,
        "request_id": "req-0001",
        "task_id": "TASK_SIZE_FIXTURE",
        "state": "processing",
        "task_card": card,
        "latest_event": {
            "event": "progress",
            "validation": [{"command": "pytest", "returncode": 1, "stderr_tail": _filler(500)}],
            "quality_gate": {"passed": False, "reason": "blocked", "checks": []},
            "usage": {
                "total_tokens": 12345,
                "usage_samples": [
                    {"seq": i, "input_tokens": 100, "output_tokens": 50, "note": _filler(280)}
                    for i in range(60)
                ],
            },
            "semantic_edit_coverage": {"measured": True, "coverage_ratio": 0.8, "filler": _filler(1600)},
            "evidence_record": {"verdict": "accepted", "filler": _filler(1300)},
            "read_efficiency": {"evidence_observed": True, "filler": _filler(1000)},
        },
    }


def test_agent_task_status_summary_folds_static_contract_and_stays_bounded(monkeypatch) -> None:
    status_result = _agent_task_status_result()

    class FakeManager:
        def status(self, request_id: str) -> dict:
            assert request_id == "req-0001"
            return status_result

    monkeypatch.setattr(server.process_launcher, "default_manager", lambda: FakeManager())

    result = server.aiworkhub_agent_task_status("req-0001")

    assert result["detail"] == "summary"
    assert result["detail_request"]["evidence"]["detail"] == "evidence"
    assert result["detail_request"]["full"]["detail"] == "full"
    size = len(json.dumps(result, separators=(",", ":")).encode("utf-8"))
    assert size <= _AGENT_TASK_STATUS_SUMMARY_MAX_BYTES, size

    card = result["task_card"]
    # Static contract text is folded to identity.
    assert card["objective"]["summarized"] is True
    assert card["acceptance"]["summarized"] is True
    assert card["validation"]["summarized"] is True
    assert card["read_first"]["summarized"] is True
    assert card["project_context"]["summarized"] is True
    assert card["template_provenance"]["summarized"] is True
    assert card["allowed_writes"] == {"summarized": True, "count": 2}
    # Outcome/provenance byproducts are folded too (shared with task_show).
    assert card["terminal_failure"]["summarized"] is True
    assert card["rework_predecessor"]["summarized"] is True
    # terminal_review.evidence sub-keys CARD_EVIDENCE_PATHS doesn't list fold too.
    assert card["terminal_review"]["evidence"]["acceptance_review"]["summarized"] is True
    # Identity/lifecycle fields stay exact.
    assert card["task_id"] == "TASK_SIZE_FIXTURE"
    assert card["title"] == "Synthetic size fixture"
    assert card["status"] == "finished"
    assert card["substatus"] == "validation_failed"
    assert card["runner"] == "claude_sonnet-5"
    assert card["topic"] == "coding"

    # The latest event's large telemetry blocks fold too.
    latest_event = result["latest_event"]
    assert latest_event["usage"]["usage_samples"]["summarized"] is True
    assert latest_event["usage"]["usage_samples"]["count"] == 60
    assert latest_event["semantic_edit_coverage"]["summarized"] is True
    assert latest_event["evidence_record"]["summarized"] is True
    assert latest_event["read_efficiency"]["summarized"] is True


def test_agent_task_status_full_keeps_the_task_card_exact(monkeypatch) -> None:
    status_result = _agent_task_status_result()

    class FakeManager:
        def status(self, request_id: str) -> dict:
            return status_result

    monkeypatch.setattr(server.process_launcher, "default_manager", lambda: FakeManager())

    result = server.aiworkhub_agent_task_status("req-0001", detail="full")

    assert result == status_result


# ---------------------------------------------------------------------------
# aiworkhub_environment_preflight
# ---------------------------------------------------------------------------


def _preflight_report() -> dict:
    def capabilities_block(n: int) -> dict:
        return {
            f"capability_{j}": {
                "capability": f"capability_{j}",
                "state": "supported" if j == 0 else "unknown",
                "evidence_class": "declared_from_code_path",
                "evidence": _filler(180),
                "reason": _filler(180),
            }
            for j in range(n)
        }

    providers = [
        {
            "adapter_id": f"adapter_{i}",
            "status": "ready" if i == 0 else "unavailable",
            "launchable": i == 0,
            "sandbox_backend": "landlock",
            "coverage_required": True,
            "reason": "" if i == 0 else "credential_absent",
            "diagnostics": _filler(3000),
        }
        for i in range(10)
    ]
    route_families = {
        f"family_{i}": {
            "route_family": f"family_{i}",
            "transport": "stdio",
            "protocol": "jsonrpc",
            "protocol_version": "2.0",
            "model_families": [f"model_{i}"],
            "documentation_urls": [f"https://example.invalid/{i}"],
            "documentation_retrieved_at": "2026-01-01T00:00:00Z",
            "documentation_digest": "d" * 64,
            "last_verification": "2026-01-01T00:00:00Z",
            "capabilities": capabilities_block(6),
        }
        for i in range(6)
    }
    capability_exclusions = {
        f"capability_{j}": [
            {
                "adapter_id": f"adapter_{i}",
                "route_family": f"family_{i % 6}",
                "capability": f"capability_{j}",
                "state": "unknown",
                "evidence_class": "unverified",
                "reason": "capability_not_declared_for_route_family",
                "evidence": _filler(120),
            }
            for i in range(10)
        ]
        for j in range(6)
    }
    return {
        "ok": True,
        "status": "degraded",
        "errors": [],
        "warnings": [],
        "providers": providers,
        "provider_summary": {
            "launchable_route_count": 1,
            "unavailable_route_count": 9,
            "coverage_status": "degraded",
            "unavailable_routes": [
                {"adapter_id": f"adapter_{i}", "status": "unavailable", "reason": "credential_absent"}
                for i in range(1, 10)
            ],
            "excluded_routes": [],
            "capability_exclusions": capability_exclusions,
        },
        "provider_observability": {
            "quota_observable_any": False,
            "adapters": [
                {
                    "adapter_id": f"adapter_{i}",
                    "route_family": f"family_{i % 6}",
                    "installed": i == 0,
                    "install_evidence": _filler(120),
                    "reachable": i == 0,
                    "reachability_evidence": _filler(120),
                    "access_observed": i == 0,
                    "quota_observable": False,
                    "quota_observability_reason": "not_observable",
                    "observable_signals": ["credential_presence"],
                    "capabilities": capabilities_block(6),
                }
                for i in range(10)
            ],
        },
        "provider_route_contracts": {
            "schema_id": "aiworkhub.provider_route_contracts.v1",
            "capability_vocabulary": [f"capability_{j}" for j in range(6)],
            "evidence_classes": ["declared_from_code_path", "unverified"],
            "route_families": route_families,
            "adapters": {f"adapter_{i}": f"family_{i % 6}" for i in range(10)},
            "note": "synthetic",
        },
        "source_graph": {
            "ready_for_code": True,
            "refreshable_for_code": True,
            "last_error": "",
            "refresh_job": {"state": "idle", "report": {"filler": _filler(20000)}},
        },
        "sandbox": {"enforceable": True, "backend": "landlock", "native_cli_enforceable": True},
    }


def test_environment_preflight_summary_is_bounded_and_filters_by_adapter(monkeypatch) -> None:
    report = _preflight_report()
    monkeypatch.setattr(server.core, "repo_root", lambda: Path("."))
    monkeypatch.setattr(
        server.repo_policy, "build_preflight",
        lambda root, adapter_id=None: report,
    )

    result = server.aiworkhub_environment_preflight()

    assert result["detail"] == "summary"
    assert result["detail_request"]["full"]["detail"] == "full"
    size = len(json.dumps(result, separators=(",", ":")).encode("utf-8"))
    assert size <= _PREFLIGHT_SUMMARY_MAX_BYTES, size
    assert len(result["providers"]) == 10
    assert result["source_graph"]["refresh_job"]["report"]["summarized"] is True
    assert result["provider_summary"]["launchable_route_count"] == (
        report["provider_summary"]["launchable_route_count"]
    )
    # provider_summary.unavailable_routes folds to a count plus a preview of ids.
    unavailable = result["provider_summary"]["unavailable_routes"]
    assert unavailable["count"] == 9
    assert unavailable["adapter_ids"] == [f"adapter_{i}" for i in range(1, 6)]

    # provider_observability.adapters folds to one compact row per adapter,
    # even with no adapter_id filter.
    adapters = result["provider_observability"]["adapters"]
    assert len(adapters) == 10
    assert set(adapters[0]) == {
        "adapter_id", "installed", "reachable", "access_observed", "quota_observable", "reason",
    }

    # provider_route_contracts.route_families folds to one row per family.
    route_families = result["provider_route_contracts"]["route_families"]
    assert isinstance(route_families, list)
    assert len(route_families) == 6
    family_0 = next(row for row in route_families if row["family"] == "family_0")
    assert set(family_0) == {"family", "status", "count"}
    assert family_0["status"] == "2026-01-01T00:00:00Z"
    assert family_0["count"] == 1

    # provider_summary.capability_exclusions folds to counts plus a preview of ids.
    capability_exclusions = result["provider_summary"]["capability_exclusions"]
    assert capability_exclusions["capability_0"]["count"] == 10
    assert len(capability_exclusions["capability_0"]["adapter_ids"]) <= 5

    filtered = server.aiworkhub_environment_preflight(adapter_id="adapter_3")
    assert list(filtered["providers"]) == ["adapter_3"]
    assert [row["adapter_id"] for row in filtered["provider_observability"]["adapters"]] == ["adapter_3"]
    filtered_size = len(json.dumps(filtered, separators=(",", ":")).encode("utf-8"))
    assert filtered_size <= _PREFLIGHT_SUMMARY_MAX_BYTES, filtered_size


def test_environment_preflight_full_and_evidence_equal_build_preflight_exactly(monkeypatch) -> None:
    report = _preflight_report()
    monkeypatch.setattr(server.core, "repo_root", lambda: Path("."))
    monkeypatch.setattr(
        server.repo_policy, "build_preflight",
        lambda root, adapter_id=None: report,
    )

    assert server.aiworkhub_environment_preflight(detail="full") == report
    assert server.aiworkhub_environment_preflight(detail="evidence") == report


def _preflight_bootstrap_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".aiworkhub/config").mkdir(parents=True)
    (root / ".aiworkhub/project.json").write_text("{}\n", encoding="utf-8")
    return root


def _stub_preflight_health_checks(monkeypatch) -> None:
    from types import SimpleNamespace

    from aiworkhub import repo_policy as repo_policy_module
    from aiworkhub import source_graph_daemon, task_reconciler

    monkeypatch.setattr(
        repo_policy_module.task_store, "storage_readiness",
        lambda _root: SimpleNamespace(ready=True, reason="ready", repo_id="repo_test"),
    )
    monkeypatch.setattr(
        repo_policy_module.source_graph_daemon, "daemon_health",
        lambda _root: {
            "ok": True,
            "status": source_graph_daemon.STATUS_READY,
            "running": True,
            "registered": True,
            "readable_generation": True,
            "last_success_at": "2026-08-03T12:00:00+00:00",
            "stale_reason": "",
            "build_revision": "aiworkhub.source_graph.semantic.v5",
            "files_seen": 3,
        },
    )
    monkeypatch.setattr(
        repo_policy_module.task_store, "callback_bridge_health",
        lambda _root: {"ok": True, "backlog_count": 0, "retry_count": 0},
    )
    monkeypatch.setattr(
        repo_policy_module.worker_workspace, "finalization_preflight_probe_nonblocking",
        lambda _root, _adapter: {
            "ok": True, "status": "ready", "reason": "", "phase": "preflight_finalization",
        },
    )
    monkeypatch.setattr(
        task_reconciler, "reconciler_health",
        lambda _root: {
            "ok": True, "running": True, "authority_state": "active_owner", "active_owner": True,
        },
    )


def test_environment_preflight_summary_is_bounded_on_real_build_preflight(
    tmp_path: Path, monkeypatch,
) -> None:
    """Built from the real repo_policy.build_preflight, not a hand-typed report --
    the static route-contract/policy/provider-observability text this pulls in is
    what a smaller hand-written fixture missed."""
    root = _preflight_bootstrap_repo(tmp_path)
    _stub_preflight_health_checks(monkeypatch)
    monkeypatch.setattr(server.core, "repo_root", lambda: root)

    result = server.aiworkhub_environment_preflight()
    assert result["detail"] == "summary"
    size = len(json.dumps(result, separators=(",", ":")).encode("utf-8"))
    assert size <= _PREFLIGHT_SUMMARY_MAX_BYTES, size

    filtered = server.aiworkhub_environment_preflight(adapter_id="claude_cli")
    filtered_size = len(json.dumps(filtered, separators=(",", ":")).encode("utf-8"))
    assert filtered_size <= _PREFLIGHT_SUMMARY_MAX_BYTES, filtered_size


# ---------------------------------------------------------------------------
# aiworkhub_manager_workforce_rank
# ---------------------------------------------------------------------------


def _workforce_rank_result() -> dict:
    # manager_ai_tools.workforce_rank's real payload carries no "ok" key --
    # the MCP layer must gate folding on shape (candidates: list), not on it.
    def candidate(i: int, excluded: bool) -> dict:
        row = {
            "worker_id": f"worker_{i}",
            "adapter_id": f"adapter_{i % 5}",
            "model": f"model_{i}",
            "provider": "anthropic",
            "excluded": excluded,
            "score_components": {
                "accepted_rate": 0.5,
                "sample_count": 10,
                "evidence_sources": {"accepted_rate": "conservative_prior"},
                "outcome_evidence": {"accepted_rate_source": "conservative_prior", "sample_count": 0},
                "filler": _filler(600),
            },
        }
        if excluded:
            row["exclusion_reasons"] = ["worker_unavailable"]
            row["availability_reason"] = "route_observation_circuit_open"
            row["route_observation"] = {"prior_observation_count": 53}
        return row

    candidates = [candidate(i, excluded=(i >= 10)) for i in range(30)]
    return {
        "selected_worker_id": "worker_0",
        "selected_adapter_id": "adapter_0",
        "candidates": candidates,
        "economic_advisory": {
            "schema_id": "aiworkhub.economic_routing_advisory.v1",
            "recommended_worker_id": "worker_1",
            "ranked_worker_ids": ["worker_1", "worker_2"],
            "automatic_selection_changed": False,
            "shadow_eligible": False,
        },
        "manager": {"session_id": "s1"},
        "surface": "manager_mcp",
    }


def test_workforce_rank_summary_reduces_excluded_and_stays_bounded(monkeypatch) -> None:
    result = _workforce_rank_result()
    monkeypatch.setattr(
        server.manager_ai_tools, "workforce_rank",
        lambda **kwargs: result,
    )

    folded = server.aiworkhub_manager_workforce_rank(task_id="T1", kinds=["code"])

    assert folded["detail"] == "summary"
    assert folded["detail_request"]["full"]["detail"] == "full"
    size = len(json.dumps(folded, separators=(",", ":")).encode("utf-8"))
    assert size <= _WORKFORCE_RANK_SUMMARY_MAX_BYTES, size

    assert len(folded["candidates"]) == 10
    # Only the selected worker and the next two keep their full row.
    assert all(row["excluded"] is False for row in folded["candidates"][:3])
    assert set(folded["candidates"][3]) == {"worker_id", "adapter_id", "model", "score"}
    assert len(folded["excluded_candidates"]) == 10
    assert folded["excluded_candidates_by_reason"] == {"worker_unavailable": 20}
    assert set(folded["excluded_candidates"][0]) == {
        "worker_id", "adapter_id", "model",
    }
    # Selection fields and economic_advisory survive intact.
    assert folded["selected_worker_id"] == "worker_0"
    assert folded["selected_adapter_id"] == "adapter_0"
    assert folded["economic_advisory"] == result["economic_advisory"]
    assert folded["manager"] == {"session_id": "s1"}
    assert folded["surface"] == "manager_mcp"


def test_workforce_rank_full_equals_todays_payload_exactly(monkeypatch) -> None:
    result = _workforce_rank_result()
    monkeypatch.setattr(
        server.manager_ai_tools, "workforce_rank",
        lambda **kwargs: result,
    )

    full = server.aiworkhub_manager_workforce_rank(task_id="T1", kinds=["code"], detail="full")

    assert full == result


def test_workforce_rank_summary_is_bounded_on_real_rank_task_output(
    tmp_path: Path, monkeypatch,
) -> None:
    """Candidates come from the real ranking pipeline (workforce_catalog.rank_task),
    not a hand-typed result -- a fixture smaller than what that pipeline actually
    produces for score_components/route_observation is why the 41KB real payload
    slipped past the old hand-written fixture test."""
    from aiworkhub import workforce_catalog, workforce_router

    root = tmp_path / "repo"
    (root / ".aiworkhub/config").mkdir(parents=True)
    (root / ".aiworkhub/project.json").write_text("{}\n", encoding="utf-8")

    # rank_task validates adapter ids against the router's real set, so the
    # fixture cycles through it instead of inventing names.
    adapters = sorted(workforce_router.SUPPORTED_ADAPTERS)

    def worker(i: int) -> dict:
        excluded = i % 3 == 0
        return {
            "worker_id": f"worker_{i}",
            "adapter_id": adapters[i % len(adapters)],
            "model": f"model_{i}",
            "provider": "anthropic",
            "enabled": True,
            "supports": ["code", "research"],
            "tools": ["filesystem"],
            "max_context_tokens": 100000,
            "max_risk": "low" if excluded else "high",
            "quality_ceiling": 1.0,
            "manager_score_adjustment": 0.0,
            "available": True,
            "outcomes": {"sample_count": 0},
            "cost_per_accepted_outcome": {
                "code": {"high": {
                    "state": "MEASURED",
                    "matched_decided_tasks": 40,
                    "accepted_outcomes": 28,
                    "acceptance_rate": 0.7,
                    "cost_coverage": 1.0,
                    "cost_per_accepted_outcome_usd": round(4.5 + i * 0.01, 4),
                }},
            },
            "availability_reason": "",
            "route_observation": {"prior_observation_count": 20, "available": True},
        }

    workers = [worker(i) for i in range(40)]
    task = workforce_router.TaskRequirements.build(
        task_id="T-real", repo_id="repo", kinds=["code"], risk="high",
        tool_needs=["filesystem"],
    )
    result = workforce_catalog.rank_task(root, task, catalog={"workers": workers})
    monkeypatch.setattr(server.manager_ai_tools, "workforce_rank", lambda **kwargs: result)

    folded = server.aiworkhub_manager_workforce_rank(task_id="T-real", kinds=["code"])

    assert folded["detail"] == "summary"
    size = len(json.dumps(folded, separators=(",", ":")).encode("utf-8"))
    assert size <= _WORKFORCE_RANK_SUMMARY_MAX_BYTES, size
    assert len(folded["excluded_candidates"]) <= 10
    assert folded["excluded_candidates_by_reason"]
    assert set(folded["candidates"][-1]) == {"worker_id", "adapter_id", "model", "score"}


def test_invalid_detail_is_refused_on_all_four_tools(monkeypatch) -> None:
    monkeypatch.setattr(server.core, "show_task", lambda task_id, full=False: _envelope(_task_show_card()))
    monkeypatch.setattr(server.core, "repo_root", lambda: Path("."))
    monkeypatch.setattr(server.repo_policy, "build_preflight", lambda root, adapter_id=None: {})
    monkeypatch.setattr(server.manager_ai_tools, "workforce_rank", lambda **kwargs: {"ok": True, "candidates": []})

    class FakeManager:
        def status(self, request_id: str) -> dict:
            return {"ok": True, "task_card": {}, "latest_event": {}}

    monkeypatch.setattr(server.process_launcher, "default_manager", lambda: FakeManager())

    for call in (
        lambda: server.aiworkhub_task_show("X", detail="bogus"),
        lambda: server.aiworkhub_agent_task_status("X", detail="bogus"),
        lambda: server.aiworkhub_environment_preflight(detail="bogus"),
        lambda: server.aiworkhub_manager_workforce_rank(task_id="X", kinds=["code"], detail="bogus"),
    ):
        result = call()
        assert result["ok"] is False
        assert result["error"] == "invalid_detail"
