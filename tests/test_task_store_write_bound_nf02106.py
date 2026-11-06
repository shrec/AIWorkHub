"""NF-2026-02106: write-time bound of task_events payloads and scheduled compaction.

Phase B of NF-2026-02104. The ``terminal_review`` and ``accept_review`` event
rows store their bulky evidence blocks as ``{summarized, bytes, sha256}``
identities while the card keeps the full blocks, and the janitor sweep runs
phase A (``task_store_compaction``) non-dry-run in bounded batches.

The terminal/accept harness of ``test_task_engine`` and the bundle harness of
``test_task_store_compaction_nf02104`` are loaded from their own modules rather
than copied.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from aiworkhub import sdlc_outcome_metrics, storage_retention, task_engine, task_store
from aiworkhub import task_store_compaction as compaction


def _sibling(name: str) -> ModuleType:
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"_nf02106_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ENGINE = _sibling("test_task_engine")
_PHASE_A = _sibling("test_task_store_compaction_nf02104")

TASK_ID = "TASK_B891"
RUNNER = "codex_worker_b891"
REQUEST_ID = "req-terminal-nf02106"
_FOLDED = ("validation", "quality_gate", "worker_mcp_gate")


def _blob(tag: str, size: int = 6000) -> str:
    return _PHASE_A._blob(tag, size)


def _identity(value: Any) -> tuple[int, str]:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return len(canonical), hashlib.sha256(canonical).hexdigest()


def _event_payloads(repo: Path, event: str) -> list[dict[str, Any]]:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM task_events WHERE task_id=? AND event=? ORDER BY event_id",
            (TASK_ID, event),
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(row[0]) for row in rows]


def _terminal_evidence() -> dict[str, Any]:
    return {
        "request_id": REQUEST_ID,
        "adapter_id": "adapter-nf02106",
        "model": "provider/model-nf02106",
        "error": "",
        "request_identity": {
            "request_id": REQUEST_ID,
            "task_id": TASK_ID,
            "runner": RUNNER,
            "topic": "task_mcp",
        },
        "workspace": {"request_id": REQUEST_ID, "path": "/tmp/ws", "base_oid": "base-oid"},
        "changed_path_hashes": {"out.txt": "c" * 64},
        "attempt_artifact_manifest": {"attempt_id": REQUEST_ID, "manifest_sha256": "d" * 64},
        "validation": [{"command": "pytest", "returncode": 0, "stdout": _blob("validation")}],
        "quality_gate": {"passed": True, "verdict": "pass", "checks": [{"summary": _blob("quality")}]},
        "worker_mcp_gate": {"satisfied": True, "status": "satisfied", "calls": [_blob("mcp")]},
        "small_note": {"ok": True},
    }


def _reviewed_repo(tmp_path: Path) -> Path:
    repo = _ENGINE._repo_with_task(tmp_path)
    ok, state = task_store.mark_terminal_review(
        repo, TASK_ID, runner=RUNNER, substatus="review_ready", evidence=_terminal_evidence()
    )
    assert (ok, state) == (True, "review")
    return repo


def _field(doc: dict[str, Any], dotted: str) -> Any:
    node: Any = doc
    for part in dotted.split("."):
        assert isinstance(node, dict) and part in node, dotted
        node = node[part]
    return node


# --- 1. the event payload is folded while the card keeps the full block -------


def test_terminal_review_event_is_folded_while_card_keeps_full_block(tmp_path: Path) -> None:
    repo = _reviewed_repo(tmp_path)
    card = task_store.get_task(repo, TASK_ID)
    assert card is not None
    full = card["terminal_review"]["evidence"]
    [event] = _event_payloads(repo, "terminal_review")

    for key in _FOLDED:
        assert full[key] == _terminal_evidence()[key]  # the card stays full
        stand_in = event["evidence"][key]
        assert stand_in["summarized"] is True
        assert len(json.dumps(stand_in)) < len(json.dumps(full[key]))
    # Small blocks and every non-foldable key stay exact.
    assert event["evidence"]["small_note"] == {"ok": True}
    assert {k: v for k, v in event["evidence"].items() if k not in _FOLDED} == {
        k: v for k, v in full.items() if k not in _FOLDED
    }
    # The row is exactly the card's block under the one bound, which is also
    # the attestation blocked-rework recovery checks against the card.
    assert event == compaction.bound_event_payload("terminal_review", card["terminal_review"])


def test_accept_review_event_is_folded_while_card_keeps_full_block(tmp_path: Path) -> None:
    repo = _ENGINE._repo_with_review_ready_task(tmp_path)
    acceptance = _ENGINE._acceptance_kwargs(repo)
    acceptance["evidence"]["validation"] = [{"returncode": 0, "stdout": _blob("accept-validation")}]
    acceptance["evidence"]["quality_gate"] = {"passed": True, "detail": _blob("accept-quality")}
    result = task_engine.accept_review(
        repo, TASK_ID, runner=RUNNER, topic="task_mcp", request_id="req-accept-b1", **acceptance
    )
    assert result["ok"] is True, result

    card = task_store.get_task(repo, TASK_ID)
    assert card is not None
    full = card["accept_evidence"]
    [event] = _event_payloads(repo, "accept_review")
    for key in ("validation", "quality_gate"):
        assert full[key] == acceptance["evidence"][key]
        assert event[key]["summarized"] is True
        assert (event[key]["bytes"], event[key]["sha256"]) == _identity(full[key])
    assert event["promoted_paths"] == full["promoted_paths"] == ["out.txt"]
    assert event == compaction.bound_event_payload("accept_review", {"request_id": "req-accept-b1", **full})


# --- 2. every field an enumerated reader extracts survives verbatim ------------


def test_every_listed_terminal_review_reader_field_survives(tmp_path: Path) -> None:
    repo = _reviewed_repo(tmp_path)
    card = task_store.get_task(repo, TASK_ID)
    assert card is not None
    [event] = _event_payloads(repo, "terminal_review")

    for dotted in sorted(compaction.EVENT_READ_FIELDS["terminal_review"]):
        assert _field(event, dotted) == _field(card["terminal_review"], dotted), dotted
    # A real reader: the review_ready ledger read-back of changed path hashes.
    assert task_store.review_ready_changed_path_hashes(repo, TASK_ID, REQUEST_ID) == {"out.txt": "c" * 64}


def test_every_listed_accept_review_reader_field_survives(tmp_path: Path) -> None:
    repo = _ENGINE._repo_with_review_ready_task(tmp_path)
    acceptance = _ENGINE._acceptance_kwargs(repo)
    acceptance["evidence"]["validation"] = [{"returncode": 0, "stdout": _blob("accept-validation")}]
    assert task_engine.accept_review(
        repo, TASK_ID, runner=RUNNER, topic="task_mcp", request_id="req-accept-b1", **acceptance
    )["ok"] is True
    [event] = _event_payloads(repo, "accept_review")
    unbound = {"request_id": "req-accept-b1", **task_store.get_task(repo, TASK_ID)["accept_evidence"]}

    for field in sorted(compaction.EVENT_READ_FIELDS["accept_review"]):
        assert event[field] == unbound[field], field
    assert event["accepted_outcome_receipt"] == acceptance["accepted_outcome_receipt"]
    # A real reader gives the same answer on the bound and the unbound payload.
    for payload in (event, unbound):
        row = {"event": "accept_review", "task_id": TASK_ID, "payload_json": json.dumps(payload)}
        assert sdlc_outcome_metrics.accepted_outcome_identity(row, "repo") == (
            sdlc_outcome_metrics.accepted_outcome_identity(
                {**row, "payload_json": json.dumps(unbound)}, "repo"
            )
        )


def test_reader_fields_are_disjoint_from_the_folded_keys() -> None:
    for fields in compaction.EVENT_READ_FIELDS.values():
        for dotted in fields:
            assert dotted.rsplit(".", 1)[-1] not in compaction._FOLDABLE_EVIDENCE_KEYS
    assert set(compaction.BOUND_EVENTS) == set(compaction.COMPACTED_EVENTS)
    # Any other event, and an absent evidence block, pass through exactly.
    payload = {"validation": [_blob("x")], "reason": "r"}
    assert compaction.bound_event_payload("reject_review", payload) == payload
    assert compaction.bound_event_payload("terminal_review", {"substatus": "x"}) == {"substatus": "x"}


# --- 3. the sha256 identity matches the full block -----------------------------


def test_identity_sha256_matches_the_full_card_block(tmp_path: Path) -> None:
    repo = _reviewed_repo(tmp_path)
    card = task_store.get_task(repo, TASK_ID)
    assert card is not None
    [event] = _event_payloads(repo, "terminal_review")
    for key in _FOLDED:
        full = card["terminal_review"]["evidence"][key]
        stand_in = event["evidence"][key]
        assert (stand_in["bytes"], stand_in["sha256"]) == _identity(full)
    # The card block is not mutated by the bound, and the bound is idempotent.
    before = json.dumps(card["terminal_review"], sort_keys=True)
    once = compaction.bound_event_payload("terminal_review", card["terminal_review"])
    assert json.dumps(card["terminal_review"], sort_keys=True) == before
    assert compaction.bound_event_payload("terminal_review", once) == once


# --- 4. a cleanup run invokes compaction with the bound; failures are reported --


def _stub_other_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, stub in {
        "_janitor_worktrees": lambda root: {"gc_scanned": 0},
        "_janitor_base_pins": lambda root: {"ok": True},
        "_prune_decided_rework_deltas": lambda root, now: {"removed": 0},
        "_janitor_logs": lambda root: {"ok": True},
        "_janitor_unattributed": lambda root, base, now: {"worktrees": {}},
        "_janitor_registrations": lambda root, base: {"pruned": 0},
        "_janitor_records": lambda root: {"tasks": {}},
        "_janitor_scratch": lambda root, now: {"ok": True, "removed": 0, "bytes": 0, "errors": []},
    }.items():
        monkeypatch.setattr(storage_retention, name, stub)


def test_cleanup_invokes_bounded_non_dry_run_compaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_other_steps(monkeypatch)
    calls: list[dict[str, Any]] = []
    scheduled: list[Any] = []

    def fake(repo_root: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"repo": Path(repo_root), "active": storage_retention._JANITOR_ACTIVE.active, **kwargs})
        return {"ok": True, "cards_compacted": 0}

    monkeypatch.setattr(compaction, "compact_finished_cards", fake)
    monkeypatch.setattr(
        storage_retention, "schedule_repository_cleanup", lambda *a, **k: scheduled.append(a)
    )
    result = storage_retention._run_repository_cleanup(tmp_path, base=tmp_path, now=2_000_000_000.0)

    assert calls == [
        {
            "repo": tmp_path.resolve(),
            "active": True,
            "dry_run": False,
            "batch_size": compaction.DEFAULT_BATCH_SIZE,
        }
    ]
    assert 1 <= compaction.DEFAULT_BATCH_SIZE <= compaction.MAX_BATCH_SIZE
    assert result["task_store"] == {"ok": True, "cards_compacted": 0}
    assert result["ok"] is True
    assert scheduled == []  # running inside the janitor schedules no further sweep


def test_cleanup_reports_a_compaction_exception_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_other_steps(monkeypatch)

    def boom(repo_root: Any, **kwargs: Any) -> dict[str, Any]:
        raise compaction.CompactionError("unfinished_card_unreadable:T1")

    monkeypatch.setattr(compaction, "compact_finished_cards", boom)
    result = storage_retention._run_repository_cleanup(tmp_path, base=tmp_path, now=2_000_000_000.0)

    assert result["task_store"] == {
        "ok": False,
        "error": "CompactionError: unfinished_card_unreadable:T1",
    }
    assert result["ok"] is False
    assert result["scratch"]["ok"] is True  # the remaining steps still ran
    assert storage_retention._JANITOR_ACTIVE.active is False


def test_cleanup_reports_a_not_ok_compaction_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_other_steps(monkeypatch)
    monkeypatch.setattr(
        compaction,
        "compact_finished_cards",
        lambda repo_root, **kwargs: {"ok": False, "vacuum": {"error": "vacuum_failed:OperationalError:x"}},
    )
    result = storage_retention._run_repository_cleanup(tmp_path, base=tmp_path, now=2_000_000_000.0)
    assert result["task_store"] == {"ok": False, "error": "CompactionError: vacuum_failed:OperationalError:x"}
    assert result["ok"] is False


def test_cleanup_without_a_ready_store_is_a_named_skip(tmp_path: Path) -> None:
    outcome = storage_retention._janitor_task_store(tmp_path / "no-store")
    assert outcome["ok"] is True
    assert outcome["skipped"] == "storage_not_ready"


# --- 5. a second cleanup run is idempotent --------------------------------------


def _store_bytes(repo: Path) -> tuple[list[tuple[str, str]], list[tuple[int, str]]]:
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        cards = conn.execute("SELECT task_id, card_json FROM tasks ORDER BY task_id").fetchall()
        events = conn.execute("SELECT event_id, payload_json FROM task_events ORDER BY event_id").fetchall()
    finally:
        conn.close()
    return [tuple(row) for row in cards], [tuple(row) for row in events]


def test_second_cleanup_run_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AIWORKHUB_PROCESS_DIR", raising=False)
    _stub_other_steps(monkeypatch)
    repo = _PHASE_A._repo(tmp_path)
    request_id = "a" * 32
    receipt = _PHASE_A._bundle(repo, request_id)
    _PHASE_A._insert(
        repo, "T-OLD", _PHASE_A._card("T-OLD", request_id, receipt), request_id=request_id, receipt=receipt
    )
    original = _store_bytes(repo)

    first = storage_retention._run_repository_cleanup(repo, base=tmp_path, now=2_000_000_000.0)
    assert first["task_store"]["ok"] is True, first["task_store"]
    assert first["task_store"]["dry_run"] is False
    assert first["task_store"]["batch_size"] == compaction.DEFAULT_BATCH_SIZE
    assert first["task_store"]["cards_compacted"] == 1
    assert first["task_store"]["bytes_saved"] > 0
    compacted = _store_bytes(repo)
    assert compacted != original

    second = storage_retention._run_repository_cleanup(repo, base=tmp_path, now=2_000_000_000.0)
    assert second["task_store"]["ok"] is True
    for counter in ("cards_compacted", "events_compacted", "bytes_saved", "write_conflicts"):
        assert second["task_store"][counter] == 0, counter
    assert _store_bytes(repo) == compacted
