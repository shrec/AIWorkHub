"""Tests for scripts/build_accepted_task_eval.py."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_accepted_task_eval as builder  # noqa: E402

from aiworkhub import eval_artifact_gate, task_engine, task_store  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

FAMILIES = ("task_mcp", "review_pipeline", "quality_evidence")
RISK_TIERS = ("low", "medium", "high")
COMPLEXITY_PROMOTED_COUNTS = {"trivial": 1, "small": 4, "moderate": 12}


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _scenario_id(family: str, risk_tier: str, complexity: str) -> str:
    return f"{family}_{risk_tier}_{complexity}".upper()


def _seed_accepted_scenario(
    repo: Path, *, family: str, risk_tier: str, complexity: str, promoted_count: int,
) -> tuple[str, str]:
    """Seed one genuinely-authenticatable accepted task episode.

    Mirrors ``tests/test_attempt_trajectory_export.py::_seed_genuine_accepted_evidence``:
    real promoted files on disk, a receipt whose digest and content actually
    re-verify against ``task_engine``'s canonical authority -- never a
    fabricated JSON blob.
    """
    scenario = _scenario_id(family, risk_tier, complexity)
    task_id = f"ACCEPTED_EVAL_FIXTURE_{scenario}_TASK"
    request_id = f"req-fixture-{scenario.lower()}"
    runner = f"fixture_runner_{family}"
    # promoted_paths must equal sorted(set(...)) per task_engine's canonical
    # authority, so pad the index to keep lexicographic order == numeric order.
    relative_paths = sorted(
        f"src/fixture/{scenario.lower()}_{i:03d}.py" for i in range(promoted_count)
    )
    changed_path_hashes: dict[str, str] = {}
    for relative in relative_paths:
        full = repo / relative
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(f"# fixture {relative}\n", encoding="utf-8")
        changed_path_hashes[relative] = hashlib.sha256(full.read_bytes()).hexdigest()
    manifest = {"artifacts": ["metadata.json"]}
    base_oid = f"base-oid-{scenario.lower()}"
    claim_epoch = 1
    receipt = {
        "schema_id": task_engine.ACCEPTED_OUTCOME_RECEIPT_SCHEMA,
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": claim_epoch,
        "base_oid": base_oid,
        "promoted_paths": relative_paths,
        "changed_path_hashes": changed_path_hashes,
        "attempt_artifact_manifest_id": _digest(manifest),
        "repository_revision": "sha256:"
        + _digest({"base_oid": base_oid, "changed_path_hashes": changed_path_hashes}),
    }
    receipt["receipt_id"] = "sha256:" + _digest(receipt)
    card = {
        "runner": runner,
        "topic": family,
        "risk_tier": risk_tier,
        "status": "finished",
        "claim_epoch": claim_epoch,
        "accepted_request_id": request_id,
        "accept_evidence": {"accepted_outcome_receipt": receipt},
        "terminal_review": {
            "evidence": {
                "request_identity": {"request_id": request_id},
                "changed_paths": relative_paths,
                "changed_path_hashes": changed_path_hashes,
                "attempt_artifact_manifest": manifest,
                "workspace": {"base_oid": base_oid},
            }
        },
    }
    now = "2026-09-01T00:00:00+00:00"
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, started_at, "
            "origin_thread_id) VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, runner, family, "finished", "finished", json.dumps(card),
                now, now, runner, now, now, f"thread-{scenario.lower()}",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return task_id, request_id


def seed_fixture_repository(repo: Path) -> list[tuple[str, str]]:
    """Seed 27 accepted scenarios across 3 families x 3 risk tiers x 3 complexities."""
    task_store.initialize_repository(repo)
    seeded = []
    for family in FAMILIES:
        for risk_tier in RISK_TIERS:
            for complexity, promoted_count in COMPLEXITY_PROMOTED_COUNTS.items():
                seeded.append(_seed_accepted_scenario(
                    repo, family=family, risk_tier=risk_tier,
                    complexity=complexity, promoted_count=promoted_count,
                ))
    return seeded


# --------------------------------------------------------------------------
# complexity_tier: pure classification
# --------------------------------------------------------------------------

def test_complexity_tier_buckets_by_promoted_path_count() -> None:
    assert builder.complexity_tier(0) == "trivial"
    assert builder.complexity_tier(1) == "trivial"
    assert builder.complexity_tier(5) == "small"
    assert builder.complexity_tier(15) == "moderate"
    assert builder.complexity_tier(16) == "large"


# --------------------------------------------------------------------------
# select_representative: bounds, determinism, stratification
# --------------------------------------------------------------------------

def _fake_candidate(index: int, *, family: str = "fam", risk_tier: str = "low", complexity: str = "trivial") -> dict:
    return {
        "task_id": f"TASK_{index:03d}",
        "request_id": f"req-{index:03d}",
        "trajectory": {},
        "family": family,
        "risk_tier": risk_tier,
        "complexity": complexity,
    }


def test_select_representative_raises_below_minimum() -> None:
    candidates = [_fake_candidate(i) for i in range(builder.MIN_ROWS - 1)]
    with pytest.raises(builder.InsufficientAcceptedTrajectoriesError):
        builder.select_representative(candidates)


def test_select_representative_accepts_exact_minimum() -> None:
    candidates = [_fake_candidate(i) for i in range(builder.MIN_ROWS)]
    selected = builder.select_representative(candidates)
    assert len(selected) == builder.MIN_ROWS


def test_select_representative_caps_at_maximum() -> None:
    candidates = [
        _fake_candidate(i, family=f"fam{i % 5}", risk_tier=RISK_TIERS[i % 3], complexity="small")
        for i in range(builder.MAX_ROWS + 30)
    ]
    selected = builder.select_representative(candidates)
    assert len(selected) == builder.MAX_ROWS


def test_select_representative_covers_every_stratum_when_capped() -> None:
    candidates = []
    for i in range(builder.MAX_ROWS + 30):
        family = FAMILIES[i % len(FAMILIES)]
        risk_tier = RISK_TIERS[i % len(RISK_TIERS)]
        complexity = list(COMPLEXITY_PROMOTED_COUNTS)[i % len(COMPLEXITY_PROMOTED_COUNTS)]
        candidates.append(_fake_candidate(i, family=family, risk_tier=risk_tier, complexity=complexity))
    selected = builder.select_representative(candidates)
    observed_strata = {(c["family"], c["risk_tier"], c["complexity"]) for c in selected}
    all_strata = {(c["family"], c["risk_tier"], c["complexity"]) for c in candidates}
    assert observed_strata == all_strata


def test_select_representative_is_deterministic_regardless_of_input_order() -> None:
    candidates = [
        _fake_candidate(i, family=FAMILIES[i % 3], risk_tier=RISK_TIERS[i % 3], complexity="small")
        for i in range(30)
    ]
    reversed_candidates = list(reversed(candidates))
    first = builder.select_representative(candidates)
    second = builder.select_representative(reversed_candidates)
    assert [(c["task_id"], c["request_id"]) for c in first] == [
        (c["task_id"], c["request_id"]) for c in second
    ]


# --------------------------------------------------------------------------
# discover_candidates: authentication, forged/tampered exclusion
# --------------------------------------------------------------------------

def test_discover_candidates_empty_repository_yields_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    assert builder.discover_candidates(repo) == []


def test_discover_candidates_excludes_tampered_receipt_but_keeps_genuine(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    genuine_task_id, genuine_request_id = _seed_accepted_scenario(
        repo, family="task_mcp", risk_tier="low", complexity="trivial", promoted_count=1,
    )
    tampered_task_id, _tampered_request_id = _seed_accepted_scenario(
        repo, family="task_mcp", risk_tier="low", complexity="small", promoted_count=4,
    )
    # Tamper with the tampered scenario's first promoted file's bytes after
    # the receipt was sealed. Locate it deterministically.
    scenario = _scenario_id("task_mcp", "low", "small")
    (repo / "src" / "fixture" / f"{scenario.lower()}_000.py").write_text(
        "# tampered after promotion\n", encoding="utf-8",
    )

    candidates = builder.discover_candidates(repo)
    task_ids = {candidate["task_id"] for candidate in candidates}
    assert genuine_task_id in task_ids
    assert tampered_task_id not in task_ids


# --------------------------------------------------------------------------
# build_rows / build_summary: redaction, digest-binding, schema
# --------------------------------------------------------------------------

def test_rows_carry_no_absolute_paths_or_secrets(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_fixture_repository(repo)
    candidates = builder.discover_candidates(repo)
    rows = builder.build_rows(candidates)
    canonical = "\n".join(json.dumps(row, sort_keys=True) for row in rows)
    assert str(repo.resolve()) not in canonical
    assert "src/fixture" not in canonical
    for row in rows:
        assert "promoted_paths" not in row
        assert set(builder._REQUIRED_ROW_FIELDS) <= set(row.keys())


def test_row_sha256_is_self_consistent_digest_binding(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_fixture_repository(repo)
    candidates = builder.discover_candidates(repo)
    rows = builder.build_rows(candidates)
    for row in rows:
        recomputed = builder._digest({k: v for k, v in row.items() if k != "row_sha256"})
        assert row["row_sha256"] == recomputed


def test_build_summary_bounds_verdict() -> None:
    rows_ok = [builder._row_from_candidate(_full_candidate(i)) for i in range(builder.MIN_ROWS)]
    summary_ok = builder.build_summary(rows_ok)
    assert summary_ok["verdict"] == "PASS"
    assert summary_ok["record_count"] == builder.MIN_ROWS

    summary_empty = builder.build_summary([])
    assert summary_empty["verdict"] == "FAIL"
    assert summary_empty["record_count"] == 0


def _full_candidate(index: int) -> dict:
    candidate = _fake_candidate(index)
    candidate["trajectory"] = {
        "topic": "fam",
        "runner": "runner",
        "task_status": "finished",
        "outcome": {"state": "accepted", "accepted_outcome_receipt": {
            "receipt_id": f"sha256:{index:064d}", "promoted_paths": ["a.py"],
        }},
        "validations": {"state": "recorded"},
        "usage": {"state": "measured"},
        "events": [],
        "reviews": [],
    }
    return candidate


# --------------------------------------------------------------------------
# rebuild: end-to-end, fail-closed on empty corpora, registry evaluation
# --------------------------------------------------------------------------

def test_rebuild_fails_closed_on_empty_task_store(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    with pytest.raises(builder.InsufficientAcceptedTrajectoriesError):
        builder.rebuild(repo)
    assert not (repo / builder.SUMMARY_RELATIVE_PATH).exists()


def test_rebuild_end_to_end_registers_with_eval_artifact_gate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_fixture_repository(repo)

    summary, rows = builder.rebuild(repo)

    assert builder.MIN_ROWS <= len(rows) <= builder.MAX_ROWS
    assert summary["verdict"] == "PASS"
    assert (repo / builder.SUMMARY_RELATIVE_PATH).is_file()
    assert (repo / builder.ROWS_RELATIVE_PATH).is_file()

    report = eval_artifact_gate.evaluate(repo)
    assert report["passed"] is True
    entry = next(row for row in report["artifacts"] if row["id"] == builder.ARTIFACT_ID)
    assert entry["status"] == "passed"
    assert entry["eligible_row_count"] == len(rows)


def test_rebuild_covers_multiple_families_risk_tiers_and_complexities(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_fixture_repository(repo)

    _summary, rows = builder.rebuild(repo)

    assert len({row["task_family"] for row in rows}) >= 3
    assert len({row["risk_tier"] for row in rows}) >= 3
    assert len({row["outcome_complexity"] for row in rows}) >= 3


def test_rebuild_is_deterministic(tmp_path: Path) -> None:
    repo_a = tmp_path / "repo_a"
    repo_a.mkdir()
    seed_fixture_repository(repo_a)
    repo_b = tmp_path / "repo_b"
    repo_b.mkdir()
    seed_fixture_repository(repo_b)

    summary_a, rows_a = builder.rebuild(repo_a)
    summary_b, rows_b = builder.rebuild(repo_b)

    assert summary_a == summary_b
    assert rows_a == rows_b


def test_registry_preserves_other_registered_artifacts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_fixture_repository(repo)
    registry_path = repo / builder.REGISTRY_RELATIVE_PATH
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps({"schema_id": eval_artifact_gate.SCHEMA_ID, "artifacts": [
            {"id": "other-artifact", "summary_path": "eval/x.json", "rows_path": "eval/x.jsonl"},
        ]}),
        encoding="utf-8",
    )

    builder.rebuild(repo)

    document = json.loads(registry_path.read_text(encoding="utf-8"))
    ids = {entry["id"] for entry in document["artifacts"]}
    assert {"other-artifact", builder.ARTIFACT_ID} <= ids


# --------------------------------------------------------------------------
# check: offline drift detection, no task store / model access
# --------------------------------------------------------------------------

def test_check_reports_missing_artifact(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    report = builder.check(repo)
    assert report["passed"] is False
    assert "artifact_missing" in report["reasons"]


def test_check_passes_on_freshly_built_corpus(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_fixture_repository(repo)
    builder.rebuild(repo)

    report = builder.check(repo)

    assert report["passed"] is True
    assert report["reasons"] == []


def test_check_detects_row_digest_drift(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_fixture_repository(repo)
    builder.rebuild(repo)
    rows_path = repo / builder.ROWS_RELATIVE_PATH
    lines = rows_path.read_text(encoding="utf-8").splitlines()
    first_row = json.loads(lines[0])
    first_row["runner"] = "tampered-runner"
    lines[0] = json.dumps(first_row)
    rows_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = builder.check(repo)

    assert report["passed"] is False
    assert any(reason.startswith("row_digest_drift:") for reason in report["reasons"])


def test_check_detects_duplicate_trajectory_identity(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_fixture_repository(repo)
    builder.rebuild(repo)
    rows_path = repo / builder.ROWS_RELATIVE_PATH
    lines = rows_path.read_text(encoding="utf-8").splitlines()
    lines.append(lines[0])
    rows_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = builder.check(repo)

    assert report["passed"] is False
    assert any(reason.startswith("duplicate_trajectory_identity:") for reason in report["reasons"])


def test_check_never_touches_task_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_fixture_repository(repo)
    builder.rebuild(repo)

    def _boom(*_args, **_kwargs):
        raise AssertionError("check() must never query the live task store")

    monkeypatch.setattr(task_store, "list_task_cards", _boom)
    report = builder.check(repo)
    assert report["passed"] is True


# --------------------------------------------------------------------------
# Production artifact pair: the actually-committed corpus in this repo
# --------------------------------------------------------------------------

def test_production_artifact_pair_is_registered_and_passes_gate() -> None:
    summary_path = REPO_ROOT / builder.SUMMARY_RELATIVE_PATH
    rows_path = REPO_ROOT / builder.ROWS_RELATIVE_PATH
    assert summary_path.is_file()
    assert rows_path.is_file()

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    rows = [
        json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert builder.MIN_ROWS <= len(rows) <= builder.MAX_ROWS
    assert summary["record_count"] == len(rows)
    assert summary["verdict"] == "PASS"

    registry = json.loads((REPO_ROOT / builder.REGISTRY_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert any(entry.get("id") == builder.ARTIFACT_ID for entry in registry["artifacts"])

    report = eval_artifact_gate.evaluate(REPO_ROOT)
    entry = next(row for row in report["artifacts"] if row["id"] == builder.ARTIFACT_ID)
    assert entry["status"] == "passed"


def test_production_artifact_pair_passes_offline_check() -> None:
    report = builder.check(REPO_ROOT)
    assert report["passed"] is True, report["reasons"]


def test_production_artifact_pair_provenance_matches_live_store_when_reachable() -> None:
    """Regression for exactly the defect a synthetic-identity corpus produces.

    A digest-self-consistent row can still be fabricated -- ``check()``
    alone cannot tell, since it only ever recomputes a row's own digest over
    its own claimed content. This re-authenticates every committed row
    against the live canonical task store, which is the only authority that
    can tell a genuine accepted outcome from a fabricated one. When the live
    store is not reachable from the current environment (for example a
    sparse worker checkout with no runtime task database), that specific
    unavailability is reported and this test is skipped rather than passing
    or failing for an unrelated reason.
    """
    rows_path = REPO_ROOT / builder.ROWS_RELATIVE_PATH
    rows = [
        json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    try:
        builder.verify_provenance(REPO_ROOT, rows)
    except builder.AcceptedTaskEvalError as exc:
        if str(exc).startswith("task_store_unavailable:"):
            pytest.skip(f"live canonical task store unreachable in this environment: {exc}")
        raise


# --------------------------------------------------------------------------
# verify_provenance: the explicit canonical-source re-authentication step
# --------------------------------------------------------------------------

def test_verify_provenance_accepts_rows_matching_live_store(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_fixture_repository(repo)
    candidates = builder.discover_candidates(repo)
    rows = builder.build_rows(candidates)

    builder.verify_provenance(repo, rows)  # must not raise


def test_verify_provenance_rejects_row_absent_from_live_store(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_fixture_repository(repo)
    candidates = builder.discover_candidates(repo)
    rows = builder.build_rows(candidates)
    forged_row = dict(rows[0])
    forged_row["task_id"] = "ACCEPTED_TASK_EVAL_CORPUS_DOES_NOT_EXIST"
    forged_row["request_id"] = "req-corpus-does-not-exist"
    forged_row["row_sha256"] = builder._digest(
        {key: value for key, value in forged_row.items() if key != "row_sha256"}
    )

    with pytest.raises(builder.AcceptedTaskEvalError, match="provenance_absent_from_sealed_source"):
        builder.verify_provenance(repo, [forged_row])


def test_verify_provenance_rejects_receipt_mismatch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_fixture_repository(repo)
    candidates = builder.discover_candidates(repo)
    rows = builder.build_rows(candidates)
    tampered_row = dict(rows[0])
    tampered_row["accepted_outcome_receipt_id"] = "sha256:" + "0" * 64

    with pytest.raises(builder.AcceptedTaskEvalError, match="provenance_receipt_mismatch"):
        builder.verify_provenance(repo, [tampered_row])


def test_rebuild_fails_closed_and_writes_nothing_when_a_row_is_forged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_fixture_repository(repo)
    real_build_rows = builder.build_rows

    def _forged_build_rows(candidates):
        rows = real_build_rows(candidates)
        forged = dict(rows[0])
        forged["task_id"] = "FORGED_TASK_NOT_IN_STORE"
        forged["request_id"] = "req-forged-not-in-store"
        forged["row_sha256"] = builder._digest(
            {key: value for key, value in forged.items() if key != "row_sha256"}
        )
        return rows[1:] + [forged]

    monkeypatch.setattr(builder, "build_rows", _forged_build_rows)

    with pytest.raises(builder.AcceptedTaskEvalError, match="provenance_absent_from_sealed_source"):
        builder.rebuild(repo)
    assert not (repo / builder.SUMMARY_RELATIVE_PATH).exists()
    assert not (repo / builder.ROWS_RELATIVE_PATH).exists()


def test_verify_provenance_cli_flag_rejects_forged_committed_row(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_fixture_repository(repo)
    builder.rebuild(repo)
    rows_path = repo / builder.ROWS_RELATIVE_PATH
    lines = rows_path.read_text(encoding="utf-8").splitlines()
    forged = json.loads(lines[0])
    forged["task_id"] = "FORGED_TASK_NOT_IN_STORE"
    forged["row_sha256"] = builder._digest(
        {key: value for key, value in forged.items() if key != "row_sha256"}
    )
    lines[0] = json.dumps(forged)
    rows_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    exit_code = builder.main(["--repo-root", str(repo), "--verify-provenance"])

    assert exit_code == 1


def test_verify_provenance_cli_flag_passes_genuine_committed_rows(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    seed_fixture_repository(repo)
    builder.rebuild(repo)

    exit_code = builder.main(["--repo-root", str(repo), "--verify-provenance"])

    assert exit_code == 0


# --------------------------------------------------------------------------
# discover_candidates on a multi-thousand-card store: measured, bounded
# whole-store query count regardless of candidate count (the O(N^2) rework)
# --------------------------------------------------------------------------

def _seed_noise_cards(repo: Path, *, count: int) -> None:
    """Insert ``count`` cards with an accepted_request_id but no receipt.

    Cheap by design: no promoted files, no receipt computation. Each still
    carries enough shape for ``discover_candidates`` to attempt (and fail)
    authentication, exercising the same per-card store-touching path a real
    multi-thousand-card store would -- without the cost of making every one
    of them a genuinely authenticatable accepted outcome.
    """
    now = "2026-09-01T00:00:00+00:00"
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, started_at, "
            "origin_thread_id) VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"NOISE_TASK_{i:06d}", "noise_runner", "noise", "finished", "finished",
                    json.dumps({
                        "runner": "noise_runner", "topic": "noise", "risk_tier": "low",
                        "status": "finished", "accepted_request_id": f"req-noise-{i:06d}",
                    }),
                    now, now, "noise_runner", now, now, f"thread-noise-{i:06d}",
                )
                for i in range(count)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_discover_candidates_bounds_whole_store_lookups_on_multi_thousand_card_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before the fix, ``export_attempt_trajectory`` re-ran
    ``latest_manager_decisions``/``list_usage_events`` -- both whole-table
    scans -- once per candidate card, turning an N-card rebuild into N
    whole-store rescans (O(N^2) total). This seeds a multi-thousand-card
    store and asserts each whole-store query runs a small constant number of
    times, independent of N.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    genuine_task_id, _genuine_request_id = _seed_accepted_scenario(
        repo, family="task_mcp", risk_tier="low", complexity="trivial", promoted_count=1,
    )
    _seed_noise_cards(repo, count=3000)

    call_counts = {"decisions": 0, "usage": 0}
    real_decisions = task_store.latest_manager_decisions
    real_usage = task_store.list_usage_events

    def _counted_decisions(*args, **kwargs):
        call_counts["decisions"] += 1
        return real_decisions(*args, **kwargs)

    def _counted_usage(*args, **kwargs):
        call_counts["usage"] += 1
        return real_usage(*args, **kwargs)

    monkeypatch.setattr(task_store, "latest_manager_decisions", _counted_decisions)
    monkeypatch.setattr(task_store, "list_usage_events", _counted_usage)

    candidates = builder.discover_candidates(repo)

    assert call_counts["decisions"] == 1
    assert call_counts["usage"] == 1
    assert {candidate["task_id"] for candidate in candidates} == {genuine_task_id}
