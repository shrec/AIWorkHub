"""NF-2026-01447: the manager task/process/storage MCP tools stay compact.

Measured 2026-10-09 over 6 manager transcripts (3.13 MB of AIWorkHub tool
results): aiworkhub_agent_task_status 546 KB over 48 calls (avg 11 KB, max
33 KB); aiworkhub_task_show 357 KB over 31 (avg 11.5 KB); needfix_show 194 KB
over 51 (avg 4 KB, max 12 KB); one aiworkhub_task_retry_terminal reply of
67,395 chars (the whole requeued card, review_feedback and rework_predecessor
included); dashboard_storage_retention_preview 22 KB; manager_bootstrap up to
11 KB with contract_delivered=false; and a convert preview whose ~6 KB plan
the manager then resent twice because convert refused ``plan_digest`` alone.

Each test below pins one of those surfaces through its compact default on a
fixture shaped like the measurement -- a large ``review_feedback`` body, a
large NeedFix ``description``, 40 reclaim candidates -- and asserts both the
size bound and that the explicit escape hatch (``detail``/``include_card``/
``include_items``/``include_plan``/``include_diagnostics``/``full``) still
returns everything.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, dashboard_mcp_app, needfix_store, server  # noqa: E402

_RETRY_TERMINAL_RECEIPT_MAX_BYTES = 2 * 1024
_TASK_SHOW_SUMMARY_MAX_BYTES = 4 * 1024
_AGENT_TASK_STATUS_SUMMARY_MAX_BYTES = 3 * 1024
_AGENT_LIST_PROCESSES_SUMMARY_MAX_BYTES = 4096
_STORAGE_PREVIEW_SUMMARY_MAX_BYTES = 4 * 1024
_MANAGER_BOOTSTRAP_COMPACT_MAX_BYTES = 4 * 1024
_NEEDFIX_SHOW_SUMMARY_MAX_BYTES = 3 * 1024
_CONVERT_PREVIEW_MAX_BYTES = 3 * 1024


def _filler(n: int) -> str:
    return "x" * n


def _size(value: object) -> int:
    """Response bytes as the server serializes them: compact separators.

    Matches the measurement in tests/test_mcp_summary_defaults_compact.py and
    the ``separators=(",", ":")`` the task read wrappers themselves render
    with, so a bound here is the bound a manager actually pays.
    """

    return len(
        json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
    )


def _size_canonical(value: object) -> int:
    """The canonical byte count ``core._evidence_identity`` reports."""

    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return len(canonical.encode("utf-8"))


# ---------------------------------------------------------------------------
# Shared card fixtures: a card whose review_feedback is the payload.
# ---------------------------------------------------------------------------


def _large_review_feedback() -> dict:
    return {
        "schema_id": "aiworkhub.rework_feedback_delta.v1",
        "instruction": _filler(20000),
        "reason_identity": {"bytes": 20000, "sha256": "a" * 64},
        "predecessor_request_id": "req-0001",
        "predecessor_changed_paths": [f"src/aiworkhub/mod_{i:03d}.py" for i in range(256)],
        "residual_identities": [
            {"path": f"src/aiworkhub/mod_{i:03d}.py", "sha256": "b" * 64} for i in range(24)
        ],
    }


def _reviewed_card() -> dict:
    return {
        "task_id": "NF01447_FIXTURE",
        "title": "Synthetic manager-context fixture",
        "status": "blocked",
        "worker_status": "timed_out",
        "substatus": "timed_out",
        "runner": "claude_opus-5",
        "topic": "aiworkhub",
        "launch_request_id": "req-0002",
        "created_at": "2026-10-09T00:00:00+00:00",
        "updated_at": "2026-10-09T01:00:00+00:00",
        "objective": _filler(900),
        "acceptance": ["resolve NF-2026-01447"],
        "validation": ["python -m pytest -q tests/a.py"],
        "read_first": ["src/aiworkhub/server.py"],
        "allowed_writes": ["src/aiworkhub/server.py", "src/aiworkhub/core.py"],
        "required_outputs": ["src/aiworkhub/server.py"],
        "review_feedback": _large_review_feedback(),
        "recovery_feedback": {"instruction": _filler(6000), "schema_id": "recovery.v1"},
        "history": [
            {"event": "reject_review", "at": f"2026-10-0{i % 9 + 1}", "note": _filler(300)}
            for i in range(20)
        ],
        "rework_predecessor": {
            "schema_id": "aiworkhub.rework_predecessor.v1",
            "request_id": "req-0001",
            "changed_path_hashes": {f"src/m{i}.py": "c" * 64 for i in range(40)},
            "workspace": {"path": "/tmp/x", "filler": _filler(3000)},
        },
        "terminal_review": {
            "evidence": {
                "quality_gate": {
                    "passed": False,
                    "quality_verdict": {
                        "reviewer_reports": [
                            {
                                "lens": "correctness",
                                "findings": [
                                    {"id": f"f{i}", "disposition": "defect", "summary": _filler(400)}
                                    for i in range(2)
                                ],
                            },
                            {"lens": "security", "findings": [{"id": "s0", "disposition": "defect"}]},
                        ]
                    },
                }
            }
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


# ---------------------------------------------------------------------------
# 1. aiworkhub_task_retry_terminal: include_card, default none.
# ---------------------------------------------------------------------------


def _retry_terminal_envelope(card: dict) -> dict:
    envelope = _envelope(card)
    envelope["command"] = ["retry-terminal", card["task_id"]]
    envelope.update({
        "retried_request_id": "req-0002",
        "terminal_substatus": "timed_out",
        "request_linkage": "card_launch_request_id",
        "workspace_retention": {"retained": True, "path": "/tmp/retained"},
        "idempotent": False,
    })
    return envelope


def test_retry_terminal_default_answers_a_receipt_not_the_whole_card(monkeypatch) -> None:
    card = _reviewed_card()
    envelope = _retry_terminal_envelope(card)
    monkeypatch.setattr(server.core, "retry_terminal_task", lambda **_kwargs: dict(envelope))

    receipt = server.aiworkhub_task_retry_terminal(
        task_id="NF01447_FIXTURE",
        request_id="req-0002",
        terminal_substatus="timed_out",
    )

    assert receipt["schema_id"] == "aiworkhub.retry_terminal_receipt.v1"
    assert receipt["include_card"] == "none"
    assert "card" not in receipt
    assert "stdout" not in receipt and "command" not in receipt
    # The facts the retry decision and its audit need stay exact.
    assert receipt["task_id"] == "NF01447_FIXTURE"
    assert receipt["status"] == "blocked"
    assert receipt["worker_status"] == "timed_out"
    assert receipt["retried_request_id"] == "req-0002"
    assert receipt["terminal_substatus"] == "timed_out"
    assert receipt["workspace_retention"] == {"retained": True, "path": "/tmp/retained"}
    assert receipt["idempotent"] is False
    assert len(receipt["card_sha256"]) == 64
    assert receipt["card_bytes"] == _size_canonical(card)

    size = _size(receipt)
    assert size <= _RETRY_TERMINAL_RECEIPT_MAX_BYTES, size
    # The card this reply used to echo verbatim.
    assert _size(envelope) > 40 * 1024


def test_retry_terminal_include_card_full_returns_the_requeued_card(monkeypatch) -> None:
    card = _reviewed_card()
    monkeypatch.setattr(
        server.core, "retry_terminal_task", lambda **_kwargs: _retry_terminal_envelope(card)
    )

    receipt = server.aiworkhub_task_retry_terminal(
        task_id="NF01447_FIXTURE",
        request_id="req-0002",
        terminal_substatus="timed_out",
        include_card="full",
    )

    assert receipt["include_card"] == "full"
    assert receipt["card"] == card


def test_retry_terminal_refuses_an_unknown_include_card_before_the_write(monkeypatch) -> None:
    calls: list[dict] = []

    def _never(**kwargs: object) -> dict:
        calls.append(dict(kwargs))
        return {}

    monkeypatch.setattr(server.core, "retry_terminal_task", _never)

    refusal = server.aiworkhub_task_retry_terminal(
        task_id="NF01447_FIXTURE",
        request_id="req-0002",
        terminal_substatus="timed_out",
        include_card="everything",
    )

    assert refusal == {
        "ok": False,
        "error": "invalid_include_card",
        "allowed": ["none", "summary", "full"],
        "received": "everything",
    }
    assert calls == []


# ---------------------------------------------------------------------------
# 2. aiworkhub_task_show / aiworkhub_agent_task_status defaults.
# ---------------------------------------------------------------------------


def test_task_show_default_folds_the_manager_authored_bodies(monkeypatch) -> None:
    card = _reviewed_card()
    monkeypatch.setattr(server.core, "show_task", lambda task_id, full=False: _envelope(card))

    result = server.aiworkhub_task_show("NF01447_FIXTURE")

    assert result["detail"] == "summary"
    assert len(result["card_sha256"]) == 64
    assert result["card_bytes"] == _size_canonical(card)
    assert result["review_findings_count"] == 3

    stdout_bytes = result["stdout"].encode("utf-8")
    assert len(stdout_bytes) <= _TASK_SHOW_SUMMARY_MAX_BYTES, len(stdout_bytes)

    rendered = json.loads(result["stdout"])
    feedback = rendered["review_feedback"]
    assert feedback["summarized"] is True
    assert len(feedback["sha256"]) == 64
    assert feedback["bytes"] == _size_canonical(card["review_feedback"])
    assert feedback["predecessor_request_id"] == "req-0001"
    assert feedback["predecessor_changed_paths_count"] == 256
    assert feedback["residual_identities_count"] == 24
    assert "instruction" not in feedback
    assert rendered["recovery_feedback"]["summarized"] is True
    history = rendered["history"]
    assert history["summarized"] is True
    assert history["count"] == 20
    assert history["bytes"] == _size_canonical(card["history"])
    assert len(history["sha256"]) == 64
    assert rendered["rework_predecessor"]["summarized"] is True


def test_task_show_full_and_evidence_still_carry_every_byte(monkeypatch) -> None:
    card = _reviewed_card()
    monkeypatch.setattr(server.core, "show_task", lambda task_id, full=False: _envelope(card))

    evidence = server.aiworkhub_task_show("NF01447_FIXTURE", detail="evidence")
    assert json.loads(evidence["stdout"]) == card

    full = server.aiworkhub_task_show("NF01447_FIXTURE", detail="full")
    assert full == _envelope(card)


def _agent_status(card: dict) -> dict:
    return {
        "ok": True,
        "request_id": "req-0002",
        "task_id": card["task_id"],
        "state": "blocked",
        "terminal_substatus": "timed_out",
        "task_card": card,
        "latest_event": {
            "event": "terminal",
            "validation": [{"command": "pytest", "returncode": 1}],
        },
    }


def test_agent_task_status_default_stays_under_three_kilobytes(monkeypatch) -> None:
    card = _reviewed_card()
    status = _agent_status(card)

    class FakeManager:
        def status(self, request_id: str) -> dict:
            assert request_id == "req-0002"
            return status

    monkeypatch.setattr(server.process_launcher, "default_manager", lambda: FakeManager())

    result = server.aiworkhub_agent_task_status("req-0002")

    size = _size(result)
    assert size <= _AGENT_TASK_STATUS_SUMMARY_MAX_BYTES, size
    assert _size(status) > 40 * 1024

    folded = result["task_card"]
    assert folded["review_feedback"]["summarized"] is True
    assert "instruction" not in folded["review_feedback"]
    assert folded["recovery_feedback"]["summarized"] is True
    assert folded["history"]["count"] == 20
    assert folded["objective"]["summarized"] is True
    # Identity and lifecycle facts stay exact.
    assert folded["task_id"] == "NF01447_FIXTURE"
    assert folded["status"] == "blocked"
    assert folded["worker_status"] == "timed_out"
    assert result["review_findings_count"] == 3
    assert result["card_bytes"] == _size_canonical(card)


def test_agent_task_status_full_is_the_raw_status_payload(monkeypatch) -> None:
    status = _agent_status(_reviewed_card())

    class FakeManager:
        def status(self, request_id: str) -> dict:
            return status

    monkeypatch.setattr(server.process_launcher, "default_manager", lambda: FakeManager())

    assert server.aiworkhub_agent_task_status("req-0002", detail="full") == status


# ---------------------------------------------------------------------------
# 3. dashboard storage_retention_preview: counts, bytes, top 10 candidates.
# ---------------------------------------------------------------------------


def _storage_preview_payload(candidate_count: int = 40) -> dict:
    candidates = [
        {
            "id": f"wt-{index:03d}",
            "head": "a" * 40,
            "size_bytes": (index + 1) * 1_000_000,
            "modified_at_epoch": 1_700_000_000 + index,
            "ownership_source": "task_lifecycle",
            "owner_task_id": f"task-{index:03d}",
            "reclaim_authority": "removable_safe",
        }
        for index in range(candidate_count)
    ]
    return {
        "ok": True,
        "schema_id": "aiworkhub.storage_retention.v1",
        "dry_run": True,
        "complete": True,
        "repository_scoped": True,
        "policy_days": 7,
        "max_bytes": 10_000_000_000,
        "current_bytes": 50_000_000_000,
        "worktree_current_bytes": 40_000_000_000,
        "footprint": {"observed_total_bytes": 50_000_000_000},
        "projected_bytes": 1_000_000,
        "candidate_count": len(candidates),
        "candidate_bytes": sum(item["size_bytes"] for item in candidates),
        "protected_count": 12,
        "protected": [
            {"id": f"wt-p{index:03d}", "reason": "pinned_predecessor_lineage"}
            for index in range(12)
        ],
        "pinned_predecessor_bytes": 2_000_000,
        "pinned_predecessors": [
            {"task_id": f"task-p{index}", "request_id": "r" * 32} for index in range(6)
        ],
        "preview_digest": "d" * 64,
        "candidates": candidates,
        "registration_health": {
            "preview_digest": "e" * 64,
            "stale_count": 9,
            "stale": [{"name": f"reg-{index}", "path": _filler(200)} for index in range(9)],
        },
        "unattributed_or_foreign_worktree_bytes": 0,
        "unattributed_or_foreign_worktree_count": 0,
        "unattributed_alert": None,
    }


def _storage_batches() -> dict:
    rows = [
        {
            "batch_id": f"batch-{index:03d}",
            "created_at": "2026-10-09T00:00:00+00:00",
            "restore_deadline": "2026-10-16T00:00:00+00:00",
            "status": "quarantined",
            "quarantined_count": 3,
            "restored_count": 0,
            "bytes": 4_000_000,
            "purge_eligible": False,
            "reapable_empty": False,
        }
        for index in range(15)
    ]
    return {"ok": True, "batches": rows, "count": len(rows)}


@pytest.fixture()
def storage_preview(monkeypatch, tmp_path):
    payload = _storage_preview_payload()
    batches = _storage_batches()
    monkeypatch.setattr(dashboard_mcp_app.core, "repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        dashboard_mcp_app.storage_retention, "preview", lambda _root: json.loads(json.dumps(payload))
    )
    monkeypatch.setattr(
        dashboard_mcp_app.storage_retention,
        "list_batches",
        lambda _root: json.loads(json.dumps(batches)),
    )
    monkeypatch.setattr(
        dashboard_mcp_app, "_readonly_authority_flags", lambda: {"writes_allowed": False}
    )
    return payload, batches


def test_storage_retention_preview_default_is_counts_bytes_and_top_candidates(
    storage_preview,
) -> None:
    payload, batches = storage_preview

    result = dashboard_mcp_app.storage_retention_preview_view()

    # The quarantine digest is unchanged: it is computed inside
    # storage_retention over the candidate identities, not over this render.
    assert result["preview_digest"] == payload["preview_digest"]
    assert result["candidate_count"] == 40
    assert result["candidate_bytes"] == payload["candidate_bytes"]
    assert result["current_bytes"] == payload["current_bytes"]

    assert len(result["candidates"]) == 10
    assert result["candidates_order"] == "size_bytes_desc"
    assert result["candidates_omitted"] == 30
    assert [item["id"] for item in result["candidates"]] == [
        f"wt-{index:03d}" for index in range(39, 29, -1)
    ]

    assert "protected" not in result
    assert result["protected_omitted"] == 12
    assert result["protected_count"] == 12
    assert "pinned_predecessors" not in result
    assert result["pinned_predecessors_count"] == 6
    assert result["pinned_predecessor_bytes"] == 2_000_000

    assert result["registration_health"]["preview_digest"] == "e" * 64
    assert result["registration_health"]["stale_count"] == 9
    assert "stale" not in result["registration_health"]

    assert "batches" not in result["quarantine"]
    assert result["quarantine"]["batches_omitted"] == len(batches["batches"])
    assert result["quarantine"]["count"] == len(batches["batches"])

    assert result["include_items"] is False
    assert result["items_request"]["include_items"] is True

    size = _size(result)
    assert size <= _STORAGE_PREVIEW_SUMMARY_MAX_BYTES, size


def test_storage_retention_preview_include_items_returns_every_list(storage_preview) -> None:
    payload, batches = storage_preview

    result = dashboard_mcp_app.storage_retention_preview_view(include_items=True)

    assert result["candidates"] == payload["candidates"]
    assert result["protected"] == payload["protected"]
    assert result["pinned_predecessors"] == payload["pinned_predecessors"]
    assert result["registration_health"] == payload["registration_health"]
    assert result["quarantine"]["batches"] == batches["batches"]
    assert "candidates_omitted" not in result
    assert _size(result) > 10 * 1024


# ---------------------------------------------------------------------------
# 4. aiworkhub_agent_list_processes: counts only, no process rows (NF-1359).
# ---------------------------------------------------------------------------


def test_agent_list_processes_default_stays_counts_only(monkeypatch) -> None:
    rows = [
        {
            "request_id": f"req-{index:04d}",
            "state": "finished",
            "terminal_substatus": "validation_failed",
            "timestamp": "2026-10-09T00:00:00+00:00",
            "task_card": {"objective": _filler(2000)},
        }
        for index in range(120)
    ]

    class FakeManager:
        def list_processes(self, limit: int = 100) -> dict:
            return {
                "ok": True,
                "processes": rows[:limit],
                "total_requests": len(rows),
                "concurrency_limit": 6,
                "workforce_cap": 8,
            }

    monkeypatch.setattr(server.process_launcher, "default_manager", lambda: FakeManager())

    result = server.aiworkhub_agent_list_processes()

    # NF-2026-01359 contract: the default carries no process rows at all.
    assert result["returned_count"] == 0
    assert "processes" not in result
    size = _size(result)
    assert size <= _AGENT_LIST_PROCESSES_SUMMARY_MAX_BYTES, size

    assert result["scanned_count"] == 100
    assert result["total_count"] == 120
    assert result["state_counts"] == {"finished": 100}
    assert result["concurrency_limit"] == 6
    assert result["workforce_cap"] == 8
    assert result["full_detail_available"] is True

    full = server.aiworkhub_agent_list_processes(detail="full")
    assert full["processes"] == rows[:100]


# ---------------------------------------------------------------------------
# 5. aiworkhub_manager_bootstrap: diagnostics behind include_diagnostics.
# ---------------------------------------------------------------------------


def _bootstrap_reply(*, contract_delivered: bool) -> dict:
    return {
        "ok": True,
        "schema_id": "aiworkhub.manager_bootstrap.v2",
        "role": "manager",
        "provider": "claude",
        "repo": "/repo",
        "storage_ready": True,
        "contract_delivered": contract_delivered,
        "contract_sha256": "f" * 64,
        "workforce_cap": {"cap": 8, "source": "repo_policy"},
        "task_hygiene": {
            "ran": True,
            "due": False,
            "last": {
                "at": "2026-10-09T00:00:00+00:00",
                "tasks": [{"task_id": f"t{index}", "note": _filler(300)} for index in range(20)],
            },
            "reconciler": {"passes": 7, "trace": [_filler(400) for _ in range(8)]},
        },
        "task_health": {
            "pending": 3,
            "blocked": 1,
            "rows": [{"task_id": f"t{index}", "detail": _filler(400)} for index in range(12)],
        },
        "dispatcher": {"ensured": True, "status": "ready"},
    }


def test_manager_bootstrap_compacts_diagnostics_when_the_contract_is_suppressed(
    monkeypatch,
) -> None:
    reply = _bootstrap_reply(contract_delivered=False)
    monkeypatch.setattr(
        server.core, "manager_bootstrap", lambda **_kwargs: json.loads(json.dumps(reply))
    )

    compact = server.aiworkhub_manager_bootstrap()

    hygiene = compact["task_hygiene"]
    assert "last" not in hygiene
    assert "reconciler" not in hygiene
    assert hygiene["ran"] is True
    assert hygiene["diagnostics_omitted"] == ["last", "reconciler"]
    assert len(hygiene["diagnostics_sha256"]) == 64
    assert hygiene["diagnostics_bytes"] == _size_canonical(reply["task_hygiene"])

    health = compact["task_health"]
    assert "rows" not in health
    assert health["pending"] == 3 and health["blocked"] == 1
    assert health["diagnostics_omitted"] == ["rows"]

    assert compact["include_diagnostics"] is False
    assert compact["diagnostics_request"]["include_diagnostics"] is True
    size = _size(compact)
    assert size <= _MANAGER_BOOTSTRAP_COMPACT_MAX_BYTES, size


def test_manager_bootstrap_keeps_diagnostics_on_request_and_with_the_contract(
    monkeypatch,
) -> None:
    suppressed = _bootstrap_reply(contract_delivered=False)
    monkeypatch.setattr(
        server.core, "manager_bootstrap", lambda **_kwargs: json.loads(json.dumps(suppressed))
    )
    assert server.aiworkhub_manager_bootstrap(include_diagnostics=True) == suppressed

    delivered = _bootstrap_reply(contract_delivered=True)
    monkeypatch.setattr(
        server.core, "manager_bootstrap", lambda **_kwargs: json.loads(json.dumps(delivered))
    )
    assert server.aiworkhub_manager_bootstrap() == delivered


# ---------------------------------------------------------------------------
# 6. needfix_show: bodies over 1 KB fold to sha256 plus length.
# ---------------------------------------------------------------------------


def _needfix_row(*, description: str) -> dict:
    return {
        "id": "NF-2026-01447",
        "title": "Manager MCP tools fill the manager context",
        "status": "accepted",
        "kind": "bug",
        "severity": "high",
        "readiness_score": 80,
        "duplicate_parent_id": None,
        "converted_task_id": None,
        "created_at": "2026-10-09T00:00:00+00:00",
        "updated_at": "2026-10-09T01:00:00+00:00",
        "archived_at": None,
        "tags": ["token-economy"],
        "scope_files": ["src/aiworkhub/server.py"],
        "scope_symbols": ["aiworkhub_task_show"],
        "evidence_refs": ["file:.aiworkhub/runtime/process_logs/audit.jsonl"],
        "description": description,
        "scope": "manager MCP read surfaces",
        "provenance": {"origin": "manager", "verified": True},
        "evidence": {
            "measurements": [
                {"tool": f"tool-{index}", "bytes": 11000, "note": _filler(300)}
                for index in range(12)
            ]
        },
    }


def test_needfix_show_default_folds_bodies_over_the_cap(monkeypatch) -> None:
    row = _needfix_row(description=_filler(9000))
    monkeypatch.setattr(server.core, "needfix_show", lambda needfix_id: json.loads(json.dumps(row)))

    result = server.needfix_show("NF-2026-01447")

    assert result["description"] == {
        "summarized": True,
        "bytes": _size_canonical(row["description"]),
        "length": 9000,
        "sha256": hashlib.sha256(
            json.dumps(row["description"], sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    assert result["evidence"]["summarized"] is True
    assert result["evidence"]["length"] == 1
    # Identity, lifecycle and the small bodies stay exact.
    assert result["id"] == "NF-2026-01447"
    assert result["status"] == "accepted"
    assert result["scope"] == "manager MCP read surfaces"
    assert result["provenance"] == {"origin": "manager", "verified": True}

    size = _size(result)
    assert size <= _NEEDFIX_SHOW_SUMMARY_MAX_BYTES, size


def test_needfix_show_full_returns_every_byte(monkeypatch) -> None:
    row = _needfix_row(description=_filler(9000))
    monkeypatch.setattr(server.core, "needfix_show", lambda needfix_id: json.loads(json.dumps(row)))

    assert server.needfix_show("NF-2026-01447", full=True) == row


# ---------------------------------------------------------------------------
# 7. NeedFix transition receipts keep the compact item (mcp-output-2).
# ---------------------------------------------------------------------------


def test_needfix_transition_receipt_keeps_the_compact_item_by_default() -> None:
    row = _needfix_row(description=_filler(9000))
    steps = [
        {
            "event": "accepted",
            "event_id": 7,
            "status_before": "triaged",
            "status_after": "accepted",
        }
    ]

    receipt = dashboard_mcp_app._needfix_transition_receipt(
        row, action="accept", steps=list(steps), include_item=False
    )

    assert receipt["item"] == dashboard_mcp_app._bounded_needfix_row(row, include_detail=False)
    assert "item_omitted" not in receipt
    assert "description" not in receipt["item"]
    assert receipt["item"]["id"] == "NF-2026-01447"
    assert receipt["status_after"] == "accepted"

    detailed = dashboard_mcp_app._needfix_transition_receipt(
        row, action="accept", steps=list(steps), include_item=True
    )
    # _bounded_needfix_row's own 8000-byte Webview cap is unchanged.
    assert detailed["item"]["description"] == row["description"][:8000]
    assert detailed["item"]["provenance"] == row["provenance"]


# ---------------------------------------------------------------------------
# 8/9. needfix convert: plan_digest alone, and the roles requirement at preview.
# ---------------------------------------------------------------------------


_BUGFIX_PLAN = {
    "title": "Resolve NF-2026-01447",
    "runner": "claude_opus-5",
    "topic": "aiworkhub",
    "work_kind": "bugfix",
    "objective": _filler(6000),
    "acceptance": ["Resolve NeedFix NF-2026-01447"],
    "allowed_writes": ["src/aiworkhub/server.py", "src/aiworkhub/core.py"],
    "required_outputs": ["src/aiworkhub/server.py"],
    "validation": [
        "python -m pytest -q tests/test_manager_context_compact_nf01447.py",
        "python -m pytest -q tests/test_mcp_summary_defaults_compact.py",
    ],
    "validation_roles": ["reproduction", "regression"],
}


class _FakeNeedFixStore:
    """The exact needfix_store surfaces ``core.needfix_*`` calls."""

    NeedFixError = needfix_store.NeedFixError
    NeedFixConflictError = needfix_store.NeedFixConflictError
    NeedFixValidationError = needfix_store.NeedFixValidationError

    def __init__(self, preview_plan: object, *, forced_digest: str | None = None) -> None:
        self.preview_plan = preview_plan
        self._forced_digest = forced_digest
        self.built_cards: list[dict] = []

    def normalize_task_plan(self, needfix_snapshot: dict, task_plan: object) -> dict:
        if not task_plan:
            raise self.NeedFixValidationError(
                "needfix conversion requires an explicit workforce launch "
                "contract; missing task_plan fields: ['runner', 'topic']"
            )
        return {"task_id": f"needfix-{needfix_snapshot['id']}", **dict(task_plan)}

    def plan_digest(self, card: dict) -> str:
        body = {key: value for key, value in card.items() if key != "task_id"}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def preview_convert(self, repo_root: object, needfix_id: str, task_plan: object = None) -> dict:
        plan = self.preview_plan if task_plan is None else task_plan
        digest = self._forced_digest
        if digest is None:
            digest = self.plan_digest(self.normalize_task_plan({"id": needfix_id}, plan))
        return {
            "needfix_id": needfix_id,
            "already_converted": False,
            "claimable": True,
            "current_status": "accepted",
            "task_plan": plan,
            "plan_digest": digest,
        }

    def convert_needfix(
        self,
        repo_root: object,
        needfix_id: str,
        create_task_fn: object,
        *,
        task_card_builder: object = None,
    ) -> dict:
        card = task_card_builder({"id": needfix_id, "title": "fixture"})
        self.built_cards.append(card)
        return {
            "needfix_id": needfix_id,
            "converted_task_id": card["task_id"],
            "already_converted": False,
            "task_plan": card,
        }


@pytest.fixture()
def needfix_store_double(monkeypatch, tmp_path):
    core.previewed_plan_cache_invalidate()
    monkeypatch.setattr(core, "repo_root", lambda: str(tmp_path))

    def _install(preview_plan: object, *, forced_digest: str | None = None) -> _FakeNeedFixStore:
        store = _FakeNeedFixStore(preview_plan, forced_digest=forced_digest)
        monkeypatch.setattr(core, "_needfix_store_module", lambda: store)
        return store

    yield _install
    core.previewed_plan_cache_invalidate()


def test_convert_accepts_plan_digest_alone_and_reuses_the_previewed_plan(
    needfix_store_double,
) -> None:
    store = needfix_store_double(dict(_BUGFIX_PLAN))
    hits_before = core.previewed_plan_cache_stats()["hits"]

    preview = core.needfix_preview_convert("NF-2026-01447")
    assert preview["plan_digest_accepted_alone"] is True
    stats = core.previewed_plan_cache_stats()
    assert stats["stored"] == 1
    assert stats["capacity"] == core.PREVIEWED_PLAN_CACHE_MAX_ENTRIES

    committed = core.needfix_convert("NF-2026-01447", plan_digest=preview["plan_digest"])

    assert committed["converted_task_id"] == "needfix-NF-2026-01447"
    built = store.built_cards[-1]
    assert built["runner"] == _BUGFIX_PLAN["runner"]
    assert built["topic"] == _BUGFIX_PLAN["topic"]
    assert built["objective"] == _BUGFIX_PLAN["objective"]
    assert core.previewed_plan_cache_stats()["hits"] == hits_before + 1


def test_preview_surfaces_the_work_kind_validation_roles_requirement(
    needfix_store_double,
) -> None:
    needfix_store_double(dict(_BUGFIX_PLAN))

    satisfied = core.needfix_preview_convert("NF-2026-01447")["validation_roles_requirement"]

    assert satisfied["work_kind"] == "bugfix"
    assert satisfied["required_roles"] == ["reproduction", "regression"]
    assert satisfied["declared_roles"] == ["reproduction", "regression"]
    assert satisfied["satisfied"] is True


def test_preview_names_the_missing_roles_instead_of_failing_at_commit(
    needfix_store_double,
) -> None:
    plan = {key: value for key, value in _BUGFIX_PLAN.items() if key != "validation_roles"}
    needfix_store_double(plan)

    requirement = core.needfix_preview_convert("NF-2026-01447")["validation_roles_requirement"]

    assert requirement["work_kind"] == "bugfix"
    assert requirement["required_roles"] == ["reproduction", "regression"]
    assert requirement["declared_roles"] == []
    assert requirement["satisfied"] is False
    assert requirement["refusal"].startswith("behavioral_validation_roles_missing:")
    assert "reproduction" in requirement["refusal"]
    assert "regression" in requirement["refusal"]


@pytest.mark.parametrize("plan_less", [None, {}])
def test_a_plan_less_preview_stores_nothing_and_convert_refuses(
    needfix_store_double, plan_less
) -> None:
    digest = "9" * 64
    needfix_store_double(plan_less, forced_digest=digest)

    preview = core.needfix_preview_convert("NF-2026-01447")

    assert preview["plan_digest"] == digest
    assert preview["plan_digest_accepted_alone"] is False
    assert core.previewed_plan_cache_stats()["stored"] == 0

    with pytest.raises(needfix_store.NeedFixError, match="previewed_plan_unavailable"):
        core.needfix_convert("NF-2026-01447", plan_digest=digest)


def test_convert_still_refuses_a_digest_that_no_longer_matches(needfix_store_double) -> None:
    needfix_store_double(dict(_BUGFIX_PLAN))
    core.needfix_preview_convert("NF-2026-01447")

    with pytest.raises(needfix_store.NeedFixConflictError, match="digest mismatch"):
        core.needfix_convert(
            "NF-2026-01447",
            task_plan=dict(_BUGFIX_PLAN),
            plan_digest="0" * 64,
        )


def test_preview_receipt_does_not_echo_the_objective(needfix_store_double) -> None:
    needfix_store_double(dict(_BUGFIX_PLAN))

    receipt = server.needfix_preview_convert("NF-2026-01447")

    plan = receipt["task_plan"]
    assert plan["summarized"] is True
    assert "objective" not in plan
    assert plan["title"] == _BUGFIX_PLAN["title"]
    assert plan["allowed_writes_count"] == 2
    assert plan["required_outputs_count"] == 1
    assert plan["work_kind"] == "bugfix"
    assert len(receipt["plan_digest"]) == 64
    assert receipt["plan_request"]["include_plan"] is True
    size = _size(receipt)
    assert size <= _CONVERT_PREVIEW_MAX_BYTES, size

    verbose = server.needfix_preview_convert("NF-2026-01447", include_plan=True)
    assert verbose["task_plan"] == _BUGFIX_PLAN
    assert _size(verbose) > 6 * 1024


@pytest.mark.parametrize(("kind", "family"), [("bug", "bugfix"), ("other", None)])
def test_convert_stamps_the_bugfix_skill_family_only_for_bug_needfixes(
    monkeypatch, tmp_path, kind, family
) -> None:
    core.previewed_plan_cache_invalidate()
    monkeypatch.setattr(core, "repo_root", lambda: str(tmp_path))
    needfix_store.initialize_repository(tmp_path)
    row = needfix_store.add_needfix(
        tmp_path, title="Skill family", description="d", status="accepted", kind=kind
    )
    created: list[dict] = []

    def _fake_create_task(**kwargs) -> dict:
        created.append(kwargs)
        return {"ok": True, "task_id": kwargs["task_id"]}

    monkeypatch.setattr(core, "create_task", _fake_create_task)
    plan = {
        "runner": "claude_sonnet-5",
        "topic": "aiworkhub",
        "allowed_writes": ["src/x.py"],
        "required_outputs": ["src/x.py"],
    }

    preview = core.needfix_preview_convert(row["id"], task_plan=plan)
    assert preview["convertible"] is True
    committed = core.needfix_convert(row["id"], plan_digest=preview["plan_digest"])

    assert committed["converted_task_id"] == created[0]["task_id"]
    assert created[0]["skill_task_family"] == family
    core.previewed_plan_cache_invalidate()
