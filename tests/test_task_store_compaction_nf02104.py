"""NF-2026-02104: explicit bounded compaction of finished cards' evidence.

Runs on a tmp canonical store. Attempt bundles are written with plain file
writes (no chmod) and re-verified by ``attempt_artifacts.verify_json_bundle``
exactly as production receipts are.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import attempt_artifacts, core, mcp_summary_folds, task_store
from aiworkhub import task_store_compaction as compaction

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
OLD = "2026-10-01T00:00:00+00:00"
RECENT = "2026-11-05T23:00:00+00:00"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert task_store.initialize_repository(repo)["ok"]
    return repo


def _blob(tag: str, size: int = 6000) -> str:
    unit = f"{tag}-evidence-line\n"
    return (unit * (size // len(unit) + 1))[:size]


@pytest.fixture(autouse=True)
def _default_process_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bundles live under the repository default root, never an ambient override.
    monkeypatch.delenv("AIWORKHUB_PROCESS_DIR", raising=False)


def _bundle(repo: Path, request_id: str, *, root: Path | None = None) -> dict[str, Any]:
    base = root if root is not None else (
        repo / ".aiworkhub" / "runtime" / "process_logs" / "processes" / "attempt-artifacts"
    )
    bundle = base / request_id
    bundle.mkdir(parents=True)
    entries = []
    for role in sorted(attempt_artifacts.REQUIRED_BUNDLE_ROLES):
        name = attempt_artifacts.ROLE_FILENAMES[role]
        data = json.dumps({"role": role, "body": _blob(role, 500)}, sort_keys=True).encode("utf-8")
        (bundle / name).write_bytes(data)
        entries.append(
            attempt_artifacts.ArtifactEntry(
                path=name,
                sha256=hashlib.sha256(data).hexdigest(),
                byte_count=len(data),
                media_type="application/json",
                role=role,
            )
        )
    manifest = attempt_artifacts.AttemptArtifactManifest(attempt_id=request_id, artifacts=entries)
    manifest_path = bundle / attempt_artifacts.MANIFEST_FILENAME
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")
    return {
        "schema_id": "aiworkhub.attempt_artifact_bundle_receipt.v1",
        "attempt_id": request_id,
        "manifest_path": str(manifest_path),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "artifact_count": len(entries),
        "verified": True,
    }


def _card(task_id: str, request_id: str, receipt: dict[str, Any], **extra: Any) -> dict[str, Any]:
    card = {
        "task_id": task_id,
        "status": "finished",
        "accepted_at": OLD,
        "accepted_request_id": request_id,
        "allowed_writes": ["src/a.py", "tests/test_a.py"],
        "validation": ["python -m pytest -q tests/test_a.py"],
        "accepted_outcome_receipt": {"sha256": "a" * 64, "request_id": request_id},
        "manager_ready_receipt": {"sha256": "b" * 64},
        "terminal_review": {
            "outcome": "review_ready",
            "evidence": {
                "attempt_artifact_manifest": receipt,
                "request_identity": {"request_id": request_id, "task_id": task_id},
                "changed_path_hashes": {"src/a.py": "c" * 64},
                "validation": [{"command": "pytest", "returncode": 0, "stdout": _blob("v")}],
                "quality_gate": {"verdict": "pass", "rows": [_blob("q")]},
                "small_note": {"ok": True},
                "unbundled_blob": {"notes": _blob("unbundled")},
            },
        },
        "accept_evidence": {
            "verdict": "accepted",
            "quality_gate": {"status": "pass", "detail": _blob("ag")},
            "validation": {"stdout": _blob("av")},
            "checks": [_blob("ac")],
        },
        "review_feedback": _blob("feedback"),
        "recovery_feedback": {"body": _blob("recovery")},
        "history": [{"event": "accepted", "detail": _blob("history")}],
        "quality_review": {"verdict": "pass", "report": _blob("quality")},
    }
    card.update(extra)
    return card


def _insert(
    repo: Path,
    task_id: str,
    card: dict[str, Any],
    *,
    status: str = "finished",
    worker_status: str = "done",
    request_id: str = "",
    receipt: dict[str, Any] | None = None,
    event_receipt: dict[str, Any] | None = None,
) -> None:
    db = task_store.canonical_db_path(repo)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id,runner,topic,status,worker_status,card_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (task_id, "runner", "topic", status, worker_status, json.dumps(card, sort_keys=True), OLD, OLD),
        )
        if request_id:
            terminal = {
                "request_id": request_id,
                "evidence": {
                    "attempt_artifact_manifest": event_receipt if event_receipt is not None else receipt,
                    "validation": [{"returncode": 0, "stdout": _blob("ev")}],
                    "worker_mcp_gate": {"verdict": "pass", "calls": [_blob("mcp")]},
                },
            }
            accept = {"request_id": request_id, "accept_evidence": {"validation": {"stdout": _blob("eav")}}}
            for event, payload in (("terminal_review", terminal), ("accept_review", accept)):
                conn.execute(
                    "INSERT INTO task_events(task_id,event,runner,payload_json,created_at) VALUES(?,?,?,?,?)",
                    (task_id, event, "manager", json.dumps(payload, sort_keys=True), OLD),
                )
        conn.commit()
    finally:
        conn.close()


def _finished(repo: Path, task_id: str, request_id: str, **extra: Any) -> dict[str, Any]:
    receipt = _bundle(repo, request_id)
    card = _card(task_id, request_id, receipt, **extra)
    _insert(repo, task_id, card, request_id=request_id, receipt=receipt)
    return card


def _snapshot(repo: Path, task_ids: list[str] | None = None) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(task_store.canonical_db_path(repo))
    try:
        rows = conn.execute("SELECT task_id, status, worker_status, card_json, updated_at FROM tasks ORDER BY task_id").fetchall()
        rows += conn.execute("SELECT event_id, task_id, event, payload_json FROM task_events ORDER BY event_id").fetchall()
    finally:
        conn.close()
    if task_ids is None:
        return rows
    return [row for row in rows if row[0] in task_ids or row[1] in task_ids]


def _auto_vacuum(repo: Path) -> int:
    conn = sqlite3.connect(task_store.canonical_db_path(repo))
    try:
        return int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
    finally:
        conn.close()


def _run(repo: Path, **kwargs: Any) -> dict[str, Any]:
    return compaction.compact_finished_cards(repo, now=NOW, **kwargs)


def test_finished_card_with_verified_manifest_is_compacted_and_still_readable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    original = _finished(repo, "done-1", "req-done-1")
    _finished(repo, "done-2", "req-done-2")

    report = _run(repo, dry_run=False, batch_size=1)

    assert report["ok"] is True
    assert report["cards_compacted"] == 2
    # One terminal_review event per card; accept_review carries only the
    # manager-side accept_evidence, which no bundle stores.
    assert report["events_compacted"] == 2
    assert report["write_batches"] == 2
    assert report["bytes_saved"] > 0
    assert report["vacuum"]["converted"] is True
    assert _auto_vacuum(repo) == 2
    assert task_store.storage_readiness(repo).ready

    card = task_store.get_task(repo, "done-1")
    assert card is not None and card["status"] == "finished"
    evidence = card["terminal_review"]["evidence"]
    validation = evidence["validation"]
    assert validation["summarized"] is True
    size, digest = core._evidence_identity(original["terminal_review"]["evidence"]["validation"])
    assert (validation["bytes"], validation["sha256"]) == (size, digest)
    assert validation["durable_attempt_id"] == "req-done-1"
    assert evidence["quality_gate"]["verdict"] == "pass"
    assert evidence["quality_gate"]["summarized"] is True
    assert evidence["small_note"] == {"ok": True}
    # Not in the bundle allowlist: left exact even though it is large.
    assert evidence["unbundled_blob"] == original["terminal_review"]["evidence"]["unbundled_blob"]
    for key in ("attempt_artifact_manifest", "request_identity", "changed_path_hashes"):
        assert evidence[key] == original["terminal_review"]["evidence"][key]
    for key in compaction.MANAGER_BLOCKS:
        if key in original:
            assert card[key] == original[key], key
    for key in ("allowed_writes", "validation", "accepted_outcome_receipt", "manager_ready_receipt", "accepted_request_id"):
        assert card[key] == original[key]

    events = {row["event"]: json.loads(row["payload"]) for row in task_store.get_task_events(repo, "done-1")}
    assert events["terminal_review"]["evidence"]["validation"]["summarized"] is True
    assert events["terminal_review"]["evidence"]["worker_mcp_gate"]["verdict"] == "pass"
    assert events["terminal_review"]["evidence"]["worker_mcp_gate"]["summarized"] is True
    assert events["terminal_review"]["evidence"]["attempt_artifact_manifest"]["attempt_id"] == "req-done-1"
    assert events["accept_review"]["accept_evidence"]["validation"] == {"stdout": _blob("eav")}
    # The task_show full-detail bound still renders the compacted card.
    bounded = mcp_summary_folds.bound_task_card(card, repo=repo)
    assert bounded["task_id"] == "done-1"


def test_unverified_processing_young_and_lineage_cards_are_untouched(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    # Tampered bundle: the receipt no longer re-verifies.
    receipt = _bundle(repo, "req-tampered")
    Path(receipt["manifest_path"]).with_name("validation.json").write_text("{}", encoding="utf-8")
    _insert(repo, "unverified", _card("unverified", "req-tampered", receipt), request_id="req-tampered", receipt=receipt)
    # Live processing card with a perfectly valid bundle.
    receipt = _bundle(repo, "req-live")
    _insert(
        repo,
        "processing",
        _card("processing", "req-live", receipt, status="processing"),
        status="processing",
        worker_status="claimed",
        request_id="req-live",
        receipt=receipt,
    )
    # Finished too recently.
    receipt = _bundle(repo, "req-young")
    _insert(repo, "young", _card("young", "req-young", receipt, accepted_at=RECENT), request_id="req-young", receipt=receipt)
    # Rework predecessor (by request id) and dependency of pending cards.
    _finished(repo, "predecessor", "req-pred")
    _finished(repo, "dependency", "req-dep")
    _insert(
        repo,
        "successor",
        {"task_id": "successor", "rework_predecessor": {"task_id": "other", "request_id": "req-pred"}},
        status="pending",
        worker_status="unclaimed",
    )
    _insert(repo, "dependent", {"task_id": "dependent", "depends_on": ["dependency"]}, status="pending", worker_status="unclaimed")
    # Rework successor still in review although its worker finished.
    _finished(repo, "pred-review", "req-pred-review")
    _insert(
        repo,
        "successor-review",
        {"task_id": "successor-review", "rework_predecessor": {"task_id": "pred-review"}},
        status="review",
        worker_status="done",
    )
    untouched = [
        "unverified",
        "processing",
        "young",
        "predecessor",
        "dependency",
        "successor",
        "dependent",
        "pred-review",
        "successor-review",
    ]
    before = _snapshot(repo, untouched)
    # One eligible card proves the run itself did write.
    _finished(repo, "eligible", "req-eligible")

    report = _run(repo, dry_run=False)

    assert report["cards_compacted"] == 1
    assert report["skipped_not_durable"] == 1
    assert report["skipped_protected_lineage"] == 3
    assert _snapshot(repo, untouched) == before
    eligible = task_store.get_task(repo, "eligible")
    assert eligible is not None and eligible["terminal_review"]["evidence"]["validation"]["summarized"] is True


def test_dry_run_measures_savings_and_writes_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _finished(repo, "done-1", "req-done-1")
    before = _snapshot(repo)
    mode = _auto_vacuum(repo)

    report = _run(repo)

    assert report["dry_run"] is True
    assert report["planned"]["cards"] == 1
    # Only the terminal_review event carries bundle-backed evidence.
    assert report["planned"]["events"] == 1
    assert report["planned"]["bytes_saved"] > 10_000
    assert report["cards_compacted"] == report["events_compacted"] == report["write_batches"] == 0
    assert report["vacuum"]["converted"] is False
    assert _snapshot(repo) == before
    assert _auto_vacuum(repo) == mode


def test_second_apply_is_a_noop(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _finished(repo, "done-1", "req-done-1")
    first = _run(repo, dry_run=False)
    assert first["cards_compacted"] == 1
    after_first = _snapshot(repo)

    second = _run(repo, dry_run=False)

    assert second["planned"] == {"cards": 0, "events": 0, "bytes_saved": 0}
    assert second["cards_compacted"] == second["events_compacted"] == second["write_batches"] == 0
    assert second["vacuum"]["converted"] is False
    assert _snapshot(repo) == after_first


def test_invalid_bounds_are_refused_and_cli_defaults_to_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path)
    _finished(repo, "done-1", "req-done-1")
    with pytest.raises(compaction.CompactionError, match="batch_size_out_of_range"):
        compaction.compact_finished_cards(repo, batch_size=0)
    before = _snapshot(repo)

    assert compaction.main(["--repo", str(repo), "--max-age-seconds", "0"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True and report["planned"]["cards"] == 1
    assert _snapshot(repo) == before


def _event_rows(repo: Path, task_id: str) -> list[tuple[Any, ...]]:
    return [row for row in _snapshot(repo, [task_id]) if len(row) == 4]


def test_receipts_outside_root_or_for_another_attempt_are_not_trusted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    # A correctly built, verifiable bundle that lives OUTSIDE the canonical
    # attempt-artifact root, under a look-alike ``attempt-artifacts`` dir.
    outside = _bundle(repo, "req-outside", root=tmp_path / "elsewhere" / "attempt-artifacts")
    _insert(repo, "outside", _card("outside", "req-outside", outside), request_id="req-outside", receipt=outside)
    # The card's own attempt verifies, but its terminal_review event embeds the
    # (equally valid) receipt of a different attempt.
    own = _bundle(repo, "req-own")
    foreign = _bundle(repo, "req-foreign")
    _insert(
        repo,
        "mixed",
        _card("mixed", "req-own", own),
        request_id="req-own",
        receipt=own,
        event_receipt=foreign,
    )
    outside_before = _snapshot(repo, ["outside"])
    mixed_events_before = _event_rows(repo, "mixed")

    report = _run(repo, dry_run=False)

    assert report["skipped_not_durable"] == 1
    assert _snapshot(repo, ["outside"]) == outside_before
    assert report["cards_compacted"] == 1
    assert report["skipped_event_unbound"] == 1
    assert report["events_compacted"] == 0
    assert _event_rows(repo, "mixed") == mixed_events_before
    mixed = task_store.get_task(repo, "mixed")
    assert mixed is not None and mixed["terminal_review"]["evidence"]["validation"]["summarized"] is True


def test_manager_blocks_are_byte_identical_after_apply(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    original = _finished(repo, "done-1", "req-done-1")
    accept_event_before = [row for row in _event_rows(repo, "done-1") if row[2] == "accept_review"]

    report = _run(repo, dry_run=False)

    assert report["cards_compacted"] == 1
    conn = sqlite3.connect(task_store.canonical_db_path(repo))
    try:
        stored = json.loads(conn.execute("SELECT card_json FROM tasks WHERE task_id='done-1'").fetchone()[0])
    finally:
        conn.close()
    for key in compaction.MANAGER_BLOCKS:
        if key in original:
            assert json.dumps(stored[key], sort_keys=True) == json.dumps(original[key], sort_keys=True), key
    assert {"accept_evidence", "review_feedback", "recovery_feedback", "history", "quality_review"} <= set(original)
    assert [row for row in _event_rows(repo, "done-1") if row[2] == "accept_review"] == accept_event_before
