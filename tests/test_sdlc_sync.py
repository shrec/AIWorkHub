"""RM-2026-00076 C: the reconciler's SDLC sync records provable stages unattended.

The positive path drives the real canonical producers the stage gate reads --
a claimed card, an attempt bundle, the launcher's terminal event, the
finalizer's ``mark_terminal_review`` and the coordinator's
``task_engine.accept_review`` -- in a bootstrapped repository under tmp_path.
Only the attribution and control-band modules are replaced, by spies.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    attempt_artifacts,
    core,
    process_event_ledger,
    process_launcher,
    process_launcher_acceptance,
    sdlc_case_store,
    sdlc_control_bands,
    sdlc_deploy_proof,
    sdlc_sync,
    task_engine,
    task_store,
)
from aiworkhub.repository_state import bootstrap_repository  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_git_facts():
    sdlc_deploy_proof._clear_git_facts()
    yield
    sdlc_deploy_proof._clear_git_facts()

RUNNER = "codex_worker"
TOPIC = "sdlc-sync"
PROMOTED = "src/feature.py"
TASK_ID = "T-SYNC"
FIX_TASK_ID = "needfix-NF-2026-00001-r1"
NOW = "2026-09-29T00:00:00+00:00"
ADAPTER = "codex_exec"
MODEL = "gpt-sdlc-test"
BRIDGE_REPO = "bridge-repo"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    bootstrap_repository(root, repo_name="sdlc-sync")
    readiness = task_store.storage_readiness(root)
    if not readiness.ready:
        task_store.initialize_repository(root)
        readiness = task_store.storage_readiness(root)
    assert readiness.ready
    return SimpleNamespace(root=root, repo_id=readiness.repo_id)


@pytest.fixture
def spies(repo, monkeypatch):
    calls = SimpleNamespace(attributed=[], evaluated=0, filed=[])

    # Exact canonical signatures, so a drift in sync_once's calls fails here.
    def attribute_needfix(root, repository_id, needfix_id):
        assert repository_id == repo.repo_id
        calls.attributed.append(needfix_id)
        return SimpleNamespace(attributed=True, reason="")

    def evaluate(root, repository_id):
        assert repository_id == repo.repo_id
        calls.evaluated += 1
        return {"evaluation": calls.evaluated}

    def file_breaches(root, repository_id, report):
        assert repository_id == repo.repo_id
        calls.filed.append(report)
        return []

    monkeypatch.setattr(sdlc_sync.sdlc_attribution, "attribute_needfix", attribute_needfix)
    monkeypatch.setattr(sdlc_sync.sdlc_control_bands, "evaluate", evaluate)
    monkeypatch.setattr(sdlc_sync.sdlc_control_bands, "file_breaches", file_breaches)
    return calls


def _card(task_id: str) -> dict:
    return {
        "task_id": task_id,
        "runner": RUNNER,
        "topic": TOPIC,
        "objective": "Record provable SDLC stages from the reconciler scan.",
        "acceptance": ["A created task reaches plan and design ready unattended."],
        "validation": ["python3 -m pytest -q tests/test_sdlc_sync.py"],
        "allowed_writes": [PROMOTED],
        "forbidden": ["Forcing a stage the gate refused."],
        "risk_tier": "medium",
        "coordinator_provider": "claude",
        "claim_epoch": 1,
    }


def _append_task_event(root: Path, task_id: str, event: str) -> None:
    conn = sqlite3.connect(str(task_store.canonical_db_path(root)))
    try:
        conn.execute(
            "INSERT INTO task_events (task_id, event, runner, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, event, RUNNER, "{}", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _create_task(root: Path, task_id: str) -> None:
    """Seed one claimed canonical card and the event a creation leaves behind."""
    card = _card(task_id)
    conn = sqlite3.connect(str(task_store.canonical_db_path(root)))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, status, worker_status, objective, "
            "card_json, created_at, updated_at, claimed_by, claimed_at, started_at) "
            "VALUES (?, ?, ?, 'processing', 'claimed', ?, ?, ?, ?, ?, ?, ?)",
            (task_id, RUNNER, TOPIC, card["objective"], json.dumps(card), NOW, NOW, RUNNER, NOW, NOW),
        )
        conn.commit()
    finally:
        conn.close()
    _append_task_event(root, task_id, "created")


def _gate() -> dict:
    receipts = [{
        "path_sha256": process_launcher.semantic_edit_path_identifier(PROMOTED),
        "range_count": 1,
        "file_bytes": 6,
        "old_region_bytes": 3,
        "replacement_bytes": 4,
        "model_reemitted_old_bytes": 0,
    }]
    return {
        "gated": True,
        "task_type": "code",
        "required_tools": ["source_graph"],
        "satisfied": True,
        "observation_only": False,
        "verification": {"ok": True, "semantic_edit_apply_receipts": receipts},
    }


def _append_terminal_event(root: Path, attempt: SimpleNamespace) -> None:
    stdout = root / f"stdout-{attempt.request_id}.jsonl"
    metadata = {
        "adapter_id": ADAPTER,
        "model": MODEL,
        "task_id": attempt.task_id,
        "claim_epoch": 1,
        "vscode_lm_bridge": {"repo_id": BRIDGE_REPO},
    }
    reasoning = process_launcher._reasoning_context_attempt_from_output(
        stdout, metadata, attempt.request_id
    )
    semantic_edit = process_launcher._semantic_edit_evidence_from_output(
        stdout, worker_mcp_gate=attempt.gate
    )
    event = {
        "request_id": attempt.request_id,
        "task_id": attempt.task_id,
        "runner": RUNNER,
        "topic": TOPIC,
        "adapter_id": ADAPTER,
        "model": MODEL,
        "state": "review_ready",
        "changed_paths": [PROMOTED],
        "attempt_artifact_manifest": attempt.manifest,
        "worker_mcp_gate": attempt.gate,
        "semantic_edit": semantic_edit,
        "semantic_edit_coverage": process_launcher._semantic_edit_coverage(
            [PROMOTED], worker_mcp_gate=attempt.gate, runtime_evidence=semantic_edit
        ),
        **({"reasoning_context_attempt": reasoning} if reasoning else {}),
    }
    process_event_ledger.append_event(
        root / process_launcher.PROCESS_LOG_DEFAULT_REL,
        {"schema_id": "aiworkhub.task_mcp.process_event.v1", "timestamp": NOW, **event},
    )


def _seal_and_accept(root: Path, task_id: str) -> None:
    """The finalizer seals one candidate and the coordinator accepts exactly it."""
    request_id = f"req-{task_id}"
    content = b"gated\n"
    target = root / PROMOTED
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    base_oid = f"base-{task_id}"
    gate = _gate()
    validations = [{"command": "python3 -m pytest -q", "returncode": 0}]
    required_outputs = [{"path": PROMOTED, "sha256": digest, "bytes": len(content)}]
    bundle_dir = root / process_launcher.PROCESS_DIR_DEFAULT_REL / "attempt-artifacts" / request_id
    manifest = attempt_artifacts.persist_json_bundle(
        bundle_dir,
        attempt_id=request_id,
        payloads={
            "metadata": {
                "schema_id": "aiworkhub.attempt_metadata.v1",
                "request_identity": {
                    "request_id": request_id, "task_id": task_id, "runner": RUNNER, "topic": TOPIC,
                },
                "adapter_id": ADAPTER,
                "model": MODEL,
                "execution_mode": "provider_worker",
                "sandbox_backend": "landlock",
                "provider_stream_mode": "terminal_events",
                "workspace": {"base_oid": base_oid},
            },
            "diff": {
                "schema_id": "aiworkhub.attempt_diff_index.v1",
                "changed_paths": [PROMOTED],
                "changed_path_hashes": {PROMOTED: digest},
                "required_outputs": required_outputs,
            },
            "validation": {
                "schema_id": "aiworkhub.attempt_validation.v1",
                "checks": validations,
                "quality_gate": None,
                "worker_mcp_gate": gate,
            },
            "usage": {"schema_id": "aiworkhub.attempt_usage.v1"},
            "review": {
                "schema_id": "aiworkhub.attempt_review.v1",
                "target_state": "review_ready",
                "error": "",
                "kind": "worker_candidate",
            },
        },
    )
    ok, state = task_store.mark_terminal_review(
        root,
        task_id,
        runner=RUNNER,
        substatus="review_ready",
        evidence={
            "request_id": request_id,
            "request_identity": {
                "request_id": request_id, "task_id": task_id, "runner": RUNNER, "claim_epoch": 1,
            },
            "validation": validations,
            "required_outputs": required_outputs,
            "changed_paths": [PROMOTED],
            "changed_path_hashes": {PROMOTED: digest},
            "worker_mcp_gate": gate,
            "attempt_artifact_manifest": manifest,
            "workspace": {"base_oid": base_oid},
        },
    )
    assert (ok, state) == (True, "review")
    _append_terminal_event(
        root,
        SimpleNamespace(task_id=task_id, request_id=request_id, manifest=manifest, gate=gate),
    )
    receipt = process_launcher_acceptance.accepted_outcome_receipt(
        root,
        task_id=task_id,
        request_id=request_id,
        claim_epoch=1,
        base_oid=base_oid,
        promoted_paths=[PROMOTED],
        changed_path_hashes={PROMOTED: digest},
        attempt_artifact_manifest=manifest,
    )
    accepted = task_engine.accept_review(
        root,
        task_id,
        runner=RUNNER,
        topic=TOPIC,
        request_id=request_id,
        evidence={"promoted_paths": [PROMOTED]},
        accepted_outcome_receipt=receipt,
    )
    assert accepted["ok"] is True, accepted


def _row_counts(root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rel, tables in (
        (sdlc_case_store.CASES_DB_REL, ("cases", "stage_receipts")),
        (sdlc_sync.STATE_DB_REL, ("sync_cursor", "task_stage_state")),
    ):
        path = root.joinpath(*rel)
        if not path.is_file():
            continue
        conn = sqlite3.connect(str(path))
        try:
            for table in tables:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if "stage_receipts" in tables:
                counts["ready_receipts"] = conn.execute(
                    "SELECT COUNT(*) FROM stage_receipts WHERE state='ready'"
                ).fetchone()[0]
        finally:
            conn.close()
    return counts


def test_created_task_gets_one_case_and_plan_design_ready_then_build_test_on_accept(repo, spies):
    _create_task(repo.root, TASK_ID)

    first = sdlc_sync.sync_once(repo.root, repo.repo_id)

    assert first["ok"] is True and first["failures"] == []
    assert first["parts"]["cases"]["created"] == 1
    assert first["stages"][TASK_ID] == {
        "plan": "ready", "design": "ready", "build": "unknown", "test": "unknown",
        "deploy": "unknown", "maintain": "unknown",
    }
    # The refused stage stays unknown with the gate's typed reason; nothing is forced.
    assert first["refusals"][TASK_ID]["build"].startswith("stage_evidence_refused:build:")
    bound = sdlc_case_store.case_for_task(repo.root, repo.repo_id, TASK_ID)
    assert bound["state"] == "bound"
    assert bound["case_id"] == core._sdlc_task_case_id(TASK_ID)
    counts = _row_counts(repo.root)
    assert counts["cases"] == 1
    assert counts["ready_receipts"] == 2

    # A second pass with no new events writes nothing and reports the same states.
    second = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert _row_counts(repo.root) == counts
    assert second["stages"] == first["stages"]
    assert second["refusals"] == first["refusals"]
    assert second["parts"]["cases"]["scanned"] == 0

    _seal_and_accept(repo.root, TASK_ID)
    third = sdlc_sync.sync_once(repo.root, repo.repo_id)

    assert third["failures"] == []
    assert third["stages"][TASK_ID] == {
        "plan": "ready", "design": "ready", "build": "ready", "test": "ready",
        "deploy": "unknown", "maintain": "unknown",
    }
    # No release is confirmed yet, so Deploy is refused by name and Maintain waits.
    assert third["refusals"][TASK_ID] == {
        "deploy": "stage_evidence_refused:deploy:release_not_confirmed",
    }
    after = _row_counts(repo.root)
    assert after["cases"] == 1
    assert after["ready_receipts"] == 4
    # Not a fix card: nothing to attribute, but the accept is a decision.
    assert spies.attributed == []
    assert spies.evaluated == 1


def test_attribution_only_for_accepted_fix_cards_and_bands_once_per_decision(repo, spies):
    _create_task(repo.root, TASK_ID)
    _create_task(repo.root, FIX_TASK_ID)
    _append_task_event(repo.root, FIX_TASK_ID, "accept_review")
    _append_task_event(repo.root, TASK_ID, "accept_review")

    first = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert first["failures"] == []
    assert spies.attributed == ["NF-2026-00001"]
    assert spies.evaluated == 1 and len(spies.filed) == 1

    sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert spies.attributed == ["NF-2026-00001"]
    assert spies.evaluated == 1

    # A new event that is not a decision neither attributes nor re-runs bands.
    _append_task_event(repo.root, TASK_ID, "heartbeat")
    third = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert third["parts"]["bands"]["state"] == "idle"
    assert spies.attributed == ["NF-2026-00001"] and spies.evaluated == 1

    # A rejection is a decision: bands run exactly once more, still no attribution.
    _append_task_event(repo.root, TASK_ID, "reject_review")
    sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert spies.attributed == ["NF-2026-00001"]
    assert spies.evaluated == 2 and len(spies.filed) == 2


def test_failed_attribution_is_retried_after_the_cursor_moves_past_the_accept(
    repo, spies, monkeypatch
):
    second_fix = "needfix-NF-2026-00002-r1"
    _create_task(repo.root, FIX_TASK_ID)
    _create_task(repo.root, second_fix)
    _append_task_event(repo.root, FIX_TASK_ID, "accept_review")
    _append_task_event(repo.root, second_fix, "accept_review")
    working = sdlc_sync.sdlc_attribution.attribute_needfix

    def flaky(root, repository_id, needfix_id):
        if needfix_id == "NF-2026-00001":
            raise RuntimeError("attribution exploded")
        return working(root, repository_id, needfix_id)

    monkeypatch.setattr(sdlc_sync.sdlc_attribution, "attribute_needfix", flaky)
    first = sdlc_sync.sync_once(repo.root, repo.repo_id)

    assert first["failures"] == ["sdlc_sync:attribution:failed"]
    assert first["parts"]["attribution"]["failed"] == {"NF-2026-00001": "RuntimeError"}
    # The loop did not abort: the other NeedFix in the same pass is attributed.
    assert spies.attributed == ["NF-2026-00002"]
    # The event cursor still advanced, so case creation is never stalled.
    assert first["parts"]["cursor"]["state"] == "advanced"

    monkeypatch.setattr(sdlc_sync.sdlc_attribution, "attribute_needfix", working)
    second = sdlc_sync.sync_once(repo.root, repo.repo_id)

    assert second["failures"] == []
    assert second["parts"]["cases"]["scanned"] == 0
    assert spies.attributed == ["NF-2026-00002", "NF-2026-00001"]
    conn = sqlite3.connect(str(repo.root.joinpath(*sdlc_sync.STATE_DB_REL)))
    try:
        assert conn.execute("SELECT COUNT(*) FROM pending_attribution").fetchone()[0] == 0
    finally:
        conn.close()


def test_failed_attribution_survives_a_failed_post_loop_state_write(repo, spies, monkeypatch):
    _create_task(repo.root, FIX_TASK_ID)
    _append_task_event(repo.root, FIX_TASK_ID, "accept_review")
    working_attr = sdlc_sync.sdlc_attribution.attribute_needfix
    working_write = sdlc_sync._write_state

    def exploding_attr(root, repository_id, needfix_id):
        raise RuntimeError("attribution exploded")

    def exploding_done(path, **kwargs):
        if kwargs.get("pending_done") is not None:
            raise sqlite3.OperationalError("disk I/O error")
        return working_write(path, **kwargs)

    monkeypatch.setattr(sdlc_sync.sdlc_attribution, "attribute_needfix", exploding_attr)
    monkeypatch.setattr(sdlc_sync, "_write_state", exploding_done)
    first = sdlc_sync.sync_once(repo.root, repo.repo_id)

    assert "sdlc_sync:attribution:failed" in first["failures"]
    assert spies.attributed == []

    monkeypatch.setattr(sdlc_sync.sdlc_attribution, "attribute_needfix", working_attr)
    monkeypatch.setattr(sdlc_sync, "_write_state", working_write)
    second = sdlc_sync.sync_once(repo.root, repo.repo_id)

    assert second["failures"] == []
    assert second["parts"]["cases"]["scanned"] == 0
    assert spies.attributed == ["NF-2026-00001"]
    conn = sqlite3.connect(str(repo.root.joinpath(*sdlc_sync.STATE_DB_REL)))
    try:
        assert conn.execute("SELECT COUNT(*) FROM pending_attribution").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("part", "target", "name"),
    [
        ("cases", sdlc_case_store, "case_for_task"),
        ("stages", sdlc_sync, "_record_stages"),
    ],
)
def test_a_failing_part_is_reported_once_and_the_other_parts_still_run(
    repo, spies, monkeypatch, part, target, name
):
    _create_task(repo.root, TASK_ID)
    _create_task(repo.root, FIX_TASK_ID)
    _append_task_event(repo.root, FIX_TASK_ID, "accept_review")

    def explode(*_args, **_kwargs):
        raise RuntimeError("part exploded")

    monkeypatch.setattr(target, name, explode)
    result = sdlc_sync.sync_once(repo.root, repo.repo_id)

    assert result["ok"] is False
    assert result["failures"] == [f"sdlc_sync:{part}:failed"]
    assert result["parts"][part] == {"state": "failed", "reason": "RuntimeError"}
    assert spies.attributed == ["NF-2026-00001"]
    assert spies.evaluated == 1
    # The window was not fully recorded, so the cursor did not move past it.
    assert "cursor" not in result["parts"] or result["parts"]["cursor"]["state"] == "read"


def test_real_control_bands_signature_runs_without_failure(repo):
    # No stub: pins sync_once to the canonical evaluate/file_breaches signatures.
    _create_task(repo.root, TASK_ID)
    _append_task_event(repo.root, TASK_ID, "accept_review")

    result = sdlc_sync.sync_once(repo.root, repo.repo_id)

    assert "sdlc_sync:bands:failed" not in result["failures"]
    assert result["parts"]["bands"]["state"] == "ran"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env=sdlc_deploy_proof.scrubbed_git_env(),
    )
    return completed.stdout.decode().strip()


def _append_ledger(root: Path, line: dict) -> None:
    with root.joinpath(*sdlc_deploy_proof.LEDGER_REL).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def test_a_newly_confirmed_release_retries_deploy_for_an_accepted_task(repo):
    # No stub anywhere: the real gate, release ledger, git and control bands.
    _create_task(repo.root, TASK_ID)
    sdlc_sync.sync_once(repo.root, repo.repo_id)
    _seal_and_accept(repo.root, TASK_ID)
    accepted = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert accepted["refusals"][TASK_ID]["deploy"].endswith(":release_not_confirmed")

    _git(repo.root, "init", "-q")
    for key, value in (
        ("user.email", "fixture@example.com"), ("user.name", "Fixture"),
        ("commit.gpgsign", "false"), ("core.autocrlf", "false"),
    ):
        _git(repo.root, "config", key, value)
    _git(repo.root, "add", PROMOTED)
    _git(repo.root, "commit", "-q", "-m", "release 1.0.0")
    built_at = (datetime.now(timezone.utc) + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _append_ledger(repo.root, {
        "kind": "built", "version": "1.0.0",
        "release_commit": _git(repo.root, "rev-parse", "HEAD"),
        "vsix_sha256": "b" * 64, "built_at": built_at, "previous_vsix": None,
        "target": "vscode_local",
    })

    # Built but unconfirmed: the ledger holds no confirmed release, nothing is retried.
    unconfirmed = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert unconfirmed["parts"]["release"] == {"state": "idle"}
    assert unconfirmed["stages"][TASK_ID]["deploy"] == "unknown"

    _append_ledger(repo.root, {
        "kind": "installed", "version": "1.0.0", "installed_at": built_at,
        "server_version": "1.0.0",
    })
    confirmed = sdlc_sync.sync_once(repo.root, repo.repo_id)

    assert confirmed["failures"] == []
    assert confirmed["parts"]["release"] == {"state": "ran", "retried": 1, "deployed": 1}
    assert confirmed["stages"][TASK_ID]["deploy"] == "ready"
    assert confirmed["stages"][TASK_ID]["maintain"] == "ready"
    assert TASK_ID not in confirmed["refusals"]
    counts = _row_counts(repo.root)
    assert counts["ready_receipts"] == 6

    # An unchanged ledger is not re-tried and writes nothing.
    again = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert again["parts"]["release"] == {"state": "idle"}
    assert again["parts"]["maintain"] == {"state": "idle"}
    assert _row_counts(repo.root) == counts


def _publish(
    root: Path, version: str, *, content: bytes | None = None, built_at: str | None = None
) -> str:
    """Commit ``PROMOTED`` (``content`` in place of the accepted bytes) and confirm it."""
    target = root / PROMOTED
    accepted_bytes = target.read_bytes()
    if content is not None:
        target.write_bytes(content)
    _git(root, "init", "-q")
    for key, value in (
        ("user.email", "fixture@example.com"), ("user.name", "Fixture"),
        ("commit.gpgsign", "false"), ("core.autocrlf", "false"),
    ):
        _git(root, "config", key, value)
    _git(root, "add", PROMOTED)
    _git(root, "commit", "-q", "--allow-empty", "-m", f"release {version}")
    commit = _git(root, "rev-parse", "HEAD")
    target.write_bytes(accepted_bytes)
    built_at = built_at or (
        datetime.now(timezone.utc) + timedelta(seconds=5)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    _append_ledger(root, {
        "kind": "built", "version": version, "release_commit": commit,
        "vsix_sha256": "b" * 64, "built_at": built_at, "previous_vsix": None,
        "target": "vscode_local",
    })
    _append_ledger(root, {
        "kind": "installed", "version": version, "installed_at": built_at,
        "server_version": version,
    })
    return commit


def _accepted_and_synced(repo: SimpleNamespace, task_id: str = TASK_ID) -> None:
    _create_task(repo.root, task_id)
    sdlc_sync.sync_once(repo.root, repo.repo_id)
    _seal_and_accept(repo.root, task_id)
    accepted = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert accepted["refusals"][task_id]["deploy"].endswith(":release_not_confirmed")


def test_maintain_is_retried_alone_once_a_band_breach_clears(repo, monkeypatch):
    real_evaluate = sdlc_control_bands.evaluate
    breach = {"on": True}

    def evaluate(root, repository_id):
        if not breach["on"]:
            return real_evaluate(root, repository_id)
        band = sdlc_control_bands.MetricBand(
            metric_id="first_pass_acceptance", direction="lower_is_bad", status="ok",
            tier="needfix", severity="high", value=0.1, baseline=0.9, n=20,
            baseline_n=100, z=-4.0, window_task_ids=(TASK_ID,),
        )
        return sdlc_control_bands.BandReport(
            schema_id=sdlc_control_bands.SCHEMA_ID, repository_id=repository_id,
            config_sha256="d" * 64, metrics=(band,),
        )

    monkeypatch.setattr(sdlc_control_bands, "evaluate", evaluate)
    monkeypatch.setattr(sdlc_control_bands, "file_breaches", lambda *_args: [])
    _accepted_and_synced(repo)
    _publish(repo.root, "1.0.0")

    breached = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert breached["stages"][TASK_ID]["deploy"] == "ready"
    assert breached["stages"][TASK_ID]["maintain"] == "unknown"
    assert breached["refusals"][TASK_ID] == {
        "maintain": "stage_evidence_refused:maintain:band_breached",
    }

    still = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert still["parts"]["release"] == {"state": "idle"}
    assert still["parts"]["maintain"] == {"state": "ran", "retried": 1, "maintained": 0}
    assert still["stages"][TASK_ID]["maintain"] == "unknown"

    breach["on"] = False

    def no_git(*_args, **_kwargs):
        raise AssertionError("a Maintain retry must not re-run git")

    evaluated: list[str] = []
    counted = sdlc_control_bands.evaluate

    def count_evaluate(root, repository_id):
        evaluated.append(repository_id)
        return counted(root, repository_id)

    monkeypatch.setattr(sdlc_deploy_proof, "_git", no_git)
    monkeypatch.setattr(sdlc_control_bands, "evaluate", count_evaluate)
    cleared = sdlc_sync.sync_once(repo.root, repo.repo_id)
    # The ledger is unchanged: only Maintain was requested again.
    assert cleared["failures"] == []
    assert cleared["parts"]["release"] == {"state": "idle"}
    assert cleared["parts"]["maintain"] == {"state": "ran", "retried": 1, "maintained": 1}
    assert cleared["stages"][TASK_ID]["maintain"] == "ready"
    assert TASK_ID not in cleared["refusals"]
    # One band evaluation for the whole pass, not one per task.
    assert evaluated == [repo.repo_id]
    assert sdlc_sync.sync_once(repo.root, repo.repo_id)["parts"]["maintain"] == {"state": "idle"}


def test_a_transient_deploy_refusal_in_the_window_withholds_the_release_digest(
    repo, monkeypatch
):
    _create_task(repo.root, TASK_ID)
    sdlc_sync.sync_once(repo.root, repo.repo_id)
    _seal_and_accept(repo.root, TASK_ID)
    _publish(repo.root, "1.0.0")

    # The accept event is in this pass's window: run_stages sees the git failure.
    with monkeypatch.context() as patched:
        patched.setattr(sdlc_deploy_proof, "_git", lambda *_args: None)
        failed = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert failed["refusals"][TASK_ID]["deploy"].endswith(":release_blob_unverifiable")
    assert failed["parts"]["release"]["state"] == "deferred"
    state_path = repo.root.joinpath(*sdlc_sync.STATE_DB_REL)
    assert sdlc_sync._read_release(state_path).get(sdlc_sync.RELEASE_DIGEST, "") == ""

    retried = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert retried["parts"]["release"] == {"state": "ran", "retried": 1, "deployed": 1}
    assert retried["stages"][TASK_ID]["deploy"] == "ready"
    assert sdlc_sync._read_release(state_path)[sdlc_sync.RELEASE_DIGEST] != ""
    assert sdlc_sync.sync_once(repo.root, repo.repo_id)["parts"]["release"] == {"state": "idle"}


def test_a_deferred_task_store_still_unrecords_the_digest_for_a_transient_window_task(
    repo, monkeypatch
):
    _accepted_and_synced(repo, "T-SYNC-A")
    _publish(repo.root, "1.0.0", built_at="2999-01-01T00:00:00Z")
    state_path = repo.root.joinpath(*sdlc_sync.STATE_DB_REL)
    recorded = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert recorded["parts"]["release"]["state"] == "ran"
    assert sdlc_sync._read_release(state_path)[sdlc_sync.RELEASE_DIGEST] != ""

    _create_task(repo.root, "T-SYNC-B")
    sdlc_sync.sync_once(repo.root, repo.repo_id)
    _seal_and_accept(repo.root, "T-SYNC-B")
    sdlc_deploy_proof._clear_git_facts()
    # The window refuses T-SYNC-B transiently while the task store is not ready.
    with monkeypatch.context() as patched:
        patched.setattr(sdlc_deploy_proof, "_git", lambda *_args: None)
        patched.setattr(sdlc_sync, "_read_cards", lambda *_args: None)
        failed = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert failed["refusals"]["T-SYNC-B"]["deploy"].endswith(":release_blob_unverifiable")
    assert failed["parts"]["release"] == {"state": "deferred", "reason": "task_store_not_ready"}
    assert sdlc_sync._read_release(state_path).get(sdlc_sync.RELEASE_DIGEST, "") == ""

    # The unchanged ledger does not idle past the refused window task.
    assert sdlc_sync.sync_once(repo.root, repo.repo_id)["parts"]["release"]["state"] != "idle"


def test_a_transient_window_task_before_the_resume_point_is_retried_before_the_digest(
    repo, monkeypatch
):
    monkeypatch.setattr(sdlc_sync, "MAX_TASKS_PER_PASS", 1)
    for task_id in ("T-SYNC-B", "T-SYNC-C"):
        _create_task(repo.root, task_id)
        _seal_and_accept(repo.root, task_id)
    _create_task(repo.root, "T-SYNC-A")
    for _ in range(10):
        if sdlc_sync.sync_once(repo.root, repo.repo_id)["parts"]["cases"]["scanned"] == 0:
            break
    _publish(repo.root, "1.0.0")
    state_path = repo.root.joinpath(*sdlc_sync.STATE_DB_REL)

    first = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert first["parts"]["release"] == {"state": "draining", "retried": 1, "deployed": 1}
    assert sdlc_sync._read_release(state_path)[sdlc_sync.RELEASE_RESUME] == "T-SYNC-B"

    # T-SYNC-A sorts before the resume point and is refused transiently in the window.
    _seal_and_accept(repo.root, "T-SYNC-A")
    # T-SYNC-B's proof remembered the release blob; git can only fail on a fact not yet known.
    sdlc_deploy_proof._clear_git_facts()
    with monkeypatch.context() as patched:
        patched.setattr(sdlc_deploy_proof, "_git", lambda *_args: None)
        failed = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert failed["refusals"]["T-SYNC-A"]["deploy"].endswith(":release_blob_unverifiable")
    assert failed["parts"]["release"]["state"] == "deferred"
    release = sdlc_sync._read_release(state_path)
    assert release.get(sdlc_sync.RELEASE_DIGEST, "") == ""
    assert release.get(sdlc_sync.RELEASE_RESUME, "") == ""

    # Unchanged ledger: the next pass restarts from the first undeployed task.
    retried = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert retried["parts"]["release"] == {"state": "draining", "retried": 1, "deployed": 1}
    assert retried["stages"]["T-SYNC-A"]["deploy"] == "ready"
    assert sdlc_sync._read_release(state_path).get(sdlc_sync.RELEASE_DIGEST, "") == ""

    drained = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert drained["parts"]["release"] == {"state": "ran", "retried": 1, "deployed": 1}
    assert sdlc_sync._read_release(state_path)[sdlc_sync.RELEASE_DIGEST] != ""
    assert sdlc_sync.sync_once(repo.root, repo.repo_id)["parts"]["release"] == {"state": "idle"}


def test_a_transient_window_task_accepted_after_the_newest_build_does_not_defer_the_digest(
    repo, monkeypatch
):
    _create_task(repo.root, TASK_ID)
    sdlc_sync.sync_once(repo.root, repo.repo_id)
    _seal_and_accept(repo.root, TASK_ID)
    # Built long before this task's acceptance: the release loop never retries it.
    _publish(repo.root, "1.0.0", built_at="2000-01-01T00:00:00Z")

    with monkeypatch.context() as patched:
        patched.setattr(sdlc_deploy_proof, "_git", lambda *_args: None)
        result = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert result["refusals"][TASK_ID]["deploy"].endswith(":release_blob_unverifiable")
    assert result["parts"]["release"] == {"state": "ran", "retried": 0, "deployed": 0}
    state_path = repo.root.joinpath(*sdlc_sync.STATE_DB_REL)
    assert sdlc_sync._read_release(state_path)[sdlc_sync.RELEASE_DIGEST] != ""


def test_reconfirming_a_version_from_a_new_commit_retries_deploy(repo):
    _accepted_and_synced(repo)
    _publish(repo.root, "1.0.0", content=b"stale bytes\n")

    stale = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert stale["parts"]["release"] == {"state": "ran", "retried": 1, "deployed": 0}
    assert stale["refusals"][TASK_ID]["deploy"].endswith(":release_commit_missing_promoted_hash")
    assert sdlc_sync.sync_once(repo.root, repo.repo_id)["parts"]["release"] == {"state": "idle"}

    # Same version, same target, a new release commit: the confirmed triples changed.
    _publish(repo.root, "1.0.0")
    rebuilt = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert rebuilt["parts"]["release"] == {"state": "ran", "retried": 1, "deployed": 1}
    assert rebuilt["stages"][TASK_ID]["deploy"] == "ready"


def test_more_undeployed_tasks_than_a_pass_allows_drain_over_several_passes(repo, monkeypatch):
    monkeypatch.setattr(sdlc_sync, "MAX_TASKS_PER_PASS", 2)
    task_ids = [f"T-SYNC-{index}" for index in range(5)]
    for task_id in task_ids:
        _create_task(repo.root, task_id)
        _seal_and_accept(repo.root, task_id)
    for _ in range(10):
        result = sdlc_sync.sync_once(repo.root, repo.repo_id)
        if result["parts"]["cases"]["scanned"] == 0:
            break
    assert {result["stages"][task_id]["test"] for task_id in task_ids} == {"ready"}
    assert {result["stages"][task_id]["deploy"] for task_id in task_ids} == {"unknown"}

    _publish(repo.root, "1.0.0")
    summaries = [
        sdlc_sync.sync_once(repo.root, repo.repo_id)["parts"]["release"] for _ in range(4)
    ]

    assert summaries == [
        {"state": "draining", "retried": 2, "deployed": 2},
        {"state": "draining", "retried": 2, "deployed": 2},
        {"state": "ran", "retried": 1, "deployed": 1},
        {"state": "idle"},
    ]
    final = sdlc_sync.sync_once(repo.root, repo.repo_id)
    assert {final["stages"][task_id]["deploy"] for task_id in task_ids} == {"ready"}


def test_sweep_state_db_is_opened_through_the_wal_writer(repo, monkeypatch):
    opened = []
    real_writer = sdlc_case_store.connect_writer

    def connect_writer(path):
        opened.append(Path(path))
        return real_writer(path)

    monkeypatch.setattr(sdlc_sync.sdlc_case_store, "connect_writer", connect_writer)
    _create_task(repo.root, TASK_ID)
    sdlc_sync.sync_once(repo.root, repo.repo_id)

    state_path = repo.root.joinpath(*sdlc_sync.STATE_DB_REL)
    assert state_path in opened
    conn = sqlite3.connect(str(state_path))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()
