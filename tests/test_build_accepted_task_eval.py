"""Tests for scripts/build_accepted_task_eval.py."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from functools import partial
from pathlib import Path
from typing import NamedTuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_accepted_task_eval as builder  # noqa: E402

from aiworkhub import (  # noqa: E402
    eval_artifact_gate, sdlc_attribution, task_engine, task_store,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

FAMILIES = ("task_mcp", "review_pipeline", "quality_evidence")
RISK_TIERS = ("low", "medium", "high")
# The fewest promoted paths that still land in each ``complexity_tier``.
COMPLEXITY_PROMOTED_COUNTS = {"trivial": 1, "small": 2, "moderate": 6}
# One scenario above the corpus floor, so a rebuild that excludes one still
# clears it. Every historical provenance pass runs two git calls per promoted
# path, so all but one ``small`` and one ``moderate`` scenario are trivial.
SEEDED_COMPLEXITIES = ("small", "moderate") + ("trivial",) * (builder.MIN_ROWS - 1)


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> str:
    env = os.environ.copy()
    for key in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_PREFIX",
    ):
        env.pop(key, None)
    # Pinned dates keep fixture commit oids (and so receipt ids) reproducible.
    env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = "2026-09-01T00:00:00+00:00"
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        input=input_bytes,
        env=env,
    )
    return result.stdout.decode().strip()


def _init_git_base(repo: Path) -> str:
    _git(repo, "init")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / ".keep").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".keep")
    _git(repo, "commit", "-m", "base")
    return _git(repo, "rev-parse", "HEAD")


def _git_commit_paths(repo: Path, message: str, *paths: str) -> str:
    _git(repo, "add", "--", *paths)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _scenario_id(family: str, risk_tier: str, complexity: str, ordinal: int | None = None) -> str:
    stem = f"{family}_{risk_tier}_{complexity}"
    return (stem if ordinal is None else f"{stem}_{ordinal:02d}").upper()


def _seed_accepted_scenario(
    repo: Path, *, family: str, risk_tier: str, complexity: str, promoted_count: int,
    base_oid: str | None = None, ordinal: int | None = None,
) -> tuple[str, str]:
    """Seed one genuinely-authenticatable accepted task episode.

    Mirrors ``tests/test_attempt_trajectory_export.py::_seed_genuine_accepted_evidence``:
    real promoted files on disk, a receipt whose digest and content actually
    re-verify against ``task_engine``'s canonical authority -- never a
    fabricated JSON blob.
    """
    scenario = _scenario_id(family, risk_tier, complexity, ordinal)
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
    if base_oid is None:
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
    """Seed one accepted scenario per ``SEEDED_COMPLEXITIES`` entry.

    Families and risk tiers rotate through every value, so the store spans
    every family, risk tier and complexity tier. Promoted bytes are committed
    after a real ``base_oid`` so every scenario also passes the historical
    default of ``verify_provenance`` that rebuild now filters candidates by.
    """
    return seed_historical_fixture_repository(repo)


def seed_historical_fixture_repository(repo: Path) -> list[tuple[str, str]]:
    task_store.initialize_repository(repo)
    base_oid = _init_git_base(repo)
    seeded = []
    for ordinal, complexity in enumerate(SEEDED_COMPLEXITIES):
        seeded.append(_seed_accepted_scenario(
            repo, family=FAMILIES[ordinal % len(FAMILIES)],
            risk_tier=RISK_TIERS[ordinal // len(FAMILIES) % len(RISK_TIERS)],
            complexity=complexity, promoted_count=COMPLEXITY_PROMOTED_COUNTS[complexity],
            base_oid=base_oid, ordinal=ordinal,
        ))
    _git_commit_paths(repo, "promote fixtures", "src")
    return seeded


# --------------------------------------------------------------------------
# shared stores: seeding and above all rebuilding (two historical git passes
# per rebuild) dominated this module while every test did both itself, so the
# store is seeded and its corpus rebuilt once per module. A test that only
# reads takes the shared store or corpus; a test that writes takes a private
# copy, never the shared one.
# --------------------------------------------------------------------------

class SeededStore(NamedTuple):
    repo: Path
    scenarios: list[tuple[str, str]]


class BuiltCorpus(NamedTuple):
    repo: Path
    summary: dict
    rows: list[dict]


def _copy_repository(source: Path, destination: Path) -> Path:
    """Copy a fixture repository's bytes: task store, git history and corpus.

    Not ``shutil.copytree``, which ``copystat``s every directory: sandboxed
    validation denies ``utime``/``chmod``, and a copy needs the bytes only.
    """
    for directory, _subdirectories, files in os.walk(source):
        target = destination / Path(directory).relative_to(source)
        target.mkdir(parents=True, exist_ok=True)
        for name in files:
            shutil.copyfile(Path(directory, name), target / name)
    return destination


@pytest.fixture(scope="module")
def seeded_store(tmp_path_factory: pytest.TempPathFactory) -> SeededStore:
    repo = tmp_path_factory.mktemp("seeded") / "repo"
    repo.mkdir()
    return SeededStore(repo, seed_fixture_repository(repo))


@pytest.fixture(scope="module")
def seeded_candidates(seeded_store: SeededStore) -> list[dict]:
    return builder.discover_candidates(seeded_store.repo)


@pytest.fixture
def seeded_copy(seeded_store: SeededStore, tmp_path: Path) -> Path:
    return _copy_repository(seeded_store.repo, tmp_path / "repo")


@pytest.fixture(scope="module")
def built_corpus(
    seeded_store: SeededStore, tmp_path_factory: pytest.TempPathFactory,
) -> BuiltCorpus:
    repo = _copy_repository(seeded_store.repo, tmp_path_factory.mktemp("built") / "repo")
    summary, rows = builder.rebuild(repo)
    return BuiltCorpus(repo, summary, rows)


@pytest.fixture
def built_copy(built_corpus: BuiltCorpus, tmp_path: Path) -> Path:
    return _copy_repository(built_corpus.repo, tmp_path / "repo")


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

def test_rows_carry_no_absolute_paths_or_secrets(
    seeded_store: SeededStore, seeded_candidates: list[dict],
) -> None:
    repo = seeded_store.repo
    rows = builder.build_rows(seeded_candidates)
    canonical = "\n".join(json.dumps(row, sort_keys=True) for row in rows)
    assert str(repo.resolve()) not in canonical
    assert "src/fixture" not in canonical
    for row in rows:
        assert "promoted_paths" not in row
        assert set(builder._REQUIRED_ROW_FIELDS) <= set(row.keys())


def test_row_sha256_is_self_consistent_digest_binding(seeded_candidates: list[dict]) -> None:
    rows = builder.build_rows(seeded_candidates)
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


def test_rebuild_end_to_end_registers_with_eval_artifact_gate(built_corpus: BuiltCorpus) -> None:
    repo, summary, rows = built_corpus

    assert builder.MIN_ROWS <= len(rows) <= builder.MAX_ROWS
    assert summary["verdict"] == "PASS"
    assert (repo / builder.SUMMARY_RELATIVE_PATH).is_file()
    assert (repo / builder.ROWS_RELATIVE_PATH).is_file()

    report = eval_artifact_gate.evaluate(repo)
    assert report["passed"] is True
    entry = next(row for row in report["artifacts"] if row["id"] == builder.ARTIFACT_ID)
    assert entry["status"] == "passed"
    assert entry["eligible_row_count"] == len(rows)


def test_rebuild_covers_multiple_families_risk_tiers_and_complexities(
    built_corpus: BuiltCorpus,
) -> None:
    rows = built_corpus.rows

    assert len({row["task_family"] for row in rows}) >= 3
    assert len({row["risk_tier"] for row in rows}) >= 3
    assert len({row["outcome_complexity"] for row in rows}) >= 3


def test_rebuild_is_deterministic(built_corpus: BuiltCorpus, tmp_path: Path) -> None:
    """Two independently seeded stores rebuild to the same corpus, byte for byte.

    Corpus A is the module's shared rebuild; corpus B is seeded and rebuilt here.
    """
    repo_b = tmp_path / "repo_b"
    repo_b.mkdir()
    seed_fixture_repository(repo_b)

    summary_a, rows_a = built_corpus.summary, built_corpus.rows
    summary_b, rows_b = builder.rebuild(repo_b)

    assert summary_a == summary_b
    assert rows_a == rows_b
    for written in (builder.SUMMARY_RELATIVE_PATH, builder.ROWS_RELATIVE_PATH):
        assert (built_corpus.repo / written).read_bytes() == (repo_b / written).read_bytes()


def test_registry_preserves_other_registered_artifacts(seeded_copy: Path) -> None:
    repo = seeded_copy
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


def test_check_passes_on_freshly_built_corpus(built_corpus: BuiltCorpus) -> None:
    repo = built_corpus.repo

    report = builder.check(repo)

    assert report["passed"] is True
    assert report["reasons"] == []


def test_check_detects_row_digest_drift(built_copy: Path) -> None:
    repo = built_copy
    rows_path = repo / builder.ROWS_RELATIVE_PATH
    lines = rows_path.read_text(encoding="utf-8").splitlines()
    first_row = json.loads(lines[0])
    first_row["runner"] = "tampered-runner"
    lines[0] = json.dumps(first_row)
    rows_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = builder.check(repo)

    assert report["passed"] is False
    assert any(reason.startswith("row_digest_drift:") for reason in report["reasons"])


def test_check_detects_duplicate_trajectory_identity(built_copy: Path) -> None:
    repo = built_copy
    rows_path = repo / builder.ROWS_RELATIVE_PATH
    lines = rows_path.read_text(encoding="utf-8").splitlines()
    lines.append(lines[0])
    rows_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = builder.check(repo)

    assert report["passed"] is False
    assert any(reason.startswith("duplicate_trajectory_identity:") for reason in report["reasons"])


def test_check_never_touches_task_store(
    built_corpus: BuiltCorpus, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = built_corpus.repo

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

def _live_authority(repo: Path):
    return partial(task_engine._validate_accepted_outcome_receipt, repo)


def test_verify_provenance_accepts_rows_matching_live_store(
    seeded_store: SeededStore, seeded_candidates: list[dict],
) -> None:
    repo = seeded_store.repo
    rows = builder.build_rows(seeded_candidates)

    builder.verify_provenance(
        repo, rows, accepted_outcome_authority=_live_authority(repo),
    )


def test_verify_provenance_rejects_row_absent_from_live_store(
    seeded_store: SeededStore, seeded_candidates: list[dict],
) -> None:
    repo = seeded_store.repo
    rows = builder.build_rows(seeded_candidates)
    forged_row = dict(rows[0])
    forged_row["task_id"] = "ACCEPTED_TASK_EVAL_CORPUS_DOES_NOT_EXIST"
    forged_row["request_id"] = "req-corpus-does-not-exist"
    forged_row["row_sha256"] = builder._digest(
        {key: value for key, value in forged_row.items() if key != "row_sha256"}
    )

    with pytest.raises(builder.AcceptedTaskEvalError, match="provenance_absent_from_sealed_source"):
        builder.verify_provenance(
            repo, [forged_row], accepted_outcome_authority=_live_authority(repo),
        )


def test_verify_provenance_rejects_receipt_mismatch(
    seeded_store: SeededStore, seeded_candidates: list[dict],
) -> None:
    repo = seeded_store.repo
    rows = builder.build_rows(seeded_candidates)
    tampered_row = dict(rows[0])
    tampered_row["accepted_outcome_receipt_id"] = "sha256:" + "0" * 64

    with pytest.raises(builder.AcceptedTaskEvalError, match="provenance_receipt_mismatch"):
        builder.verify_provenance(
            repo, [tampered_row], accepted_outcome_authority=_live_authority(repo),
        )

def test_rebuild_fails_closed_and_writes_nothing_when_a_row_is_forged(
    seeded_copy: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = seeded_copy
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


def _fail_historical_authority_for(
    monkeypatch: pytest.MonkeyPatch, failing_task_ids: set[str],
) -> None:
    """Stub the historical authority so the named candidates fail it."""
    real_authority = builder.historical_accepted_outcome_authority

    def _stubbed(repo, card, task_id, request_id, receipt):
        if task_id in failing_task_ids:
            return None, "accepted_outcome_receipt_historical_hash_mismatch"
        return real_authority(repo, card, task_id, request_id, receipt)

    monkeypatch.setattr(builder, "historical_accepted_outcome_authority", _stubbed)


def test_rebuild_excludes_and_reports_candidate_failing_historical_authority(
    seeded_store: SeededStore, seeded_copy: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NF-2026-01129: rebuild never writes a row its own default check rejects."""
    repo = seeded_copy
    seeded = seeded_store.scenarios
    failing = seeded[0]
    _fail_historical_authority_for(monkeypatch, {failing[0]})

    summary, rows = builder.rebuild(repo)

    written = {(row["task_id"], row["request_id"]) for row in rows}
    assert failing not in written
    assert written == set(seeded) - {failing}
    excluded = summary["excluded_candidates"]
    assert [(entry["task_id"], entry["request_id"]) for entry in excluded] == [failing]
    assert "accepted_outcome_receipt_historical_hash_mismatch" in excluded[0]["reason"]
    on_disk = json.loads((repo / builder.SUMMARY_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert on_disk["excluded_candidates"] == excluded
    committed = [
        json.loads(line)
        for line in (repo / builder.ROWS_RELATIVE_PATH).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert committed == rows
    builder.verify_provenance(repo, committed)
    assert builder.check(repo)["passed"] is True


def test_rebuild_enforces_minimum_rows_after_historical_exclusion(
    seeded_store: SeededStore, seeded_copy: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = seeded_copy
    seeded = seeded_store.scenarios
    keep = builder.MIN_ROWS - 1
    _fail_historical_authority_for(monkeypatch, {task_id for task_id, _ in seeded[keep:]})

    with pytest.raises(builder.InsufficientAcceptedTrajectoriesError):
        builder.rebuild(repo)
    assert not (repo / builder.SUMMARY_RELATIVE_PATH).exists()
    assert not (repo / builder.ROWS_RELATIVE_PATH).exists()


def test_verify_provenance_cli_flag_rejects_forged_committed_row(built_copy: Path) -> None:
    repo = built_copy
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


def test_verify_provenance_cli_flag_passes_genuine_committed_rows(
    built_corpus: BuiltCorpus,
) -> None:
    repo = built_corpus.repo

    exit_code = builder.main(["--repo-root", str(repo), "--verify-provenance"])

    assert exit_code == 0


def _insert_accepted_card(
    repo: Path,
    *,
    task_id: str,
    request_id: str,
    files: dict[str, bytes],
    base_oid: str,
    family: str = "task_mcp",
) -> dict:
    relative_paths = sorted(files)
    changed_path_hashes = {
        relative: hashlib.sha256(payload).hexdigest() for relative, payload in files.items()
    }
    for relative, payload in files.items():
        full = repo / relative
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(payload)
    manifest = {"artifacts": ["metadata.json"]}
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
        "runner": "hist_runner",
        "topic": family,
        "risk_tier": "low",
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
                task_id, "hist_runner", family, "finished", "finished", json.dumps(card),
                now, now, "hist_runner", now, now, "thread-hist",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return receipt


def _provenance_row(task_id: str, request_id: str, receipt: dict) -> dict:
    return {
        "task_id": task_id,
        "request_id": request_id,
        "accepted_outcome_receipt_id": receipt["receipt_id"],
    }


def test_verify_provenance_survives_later_canonical_edits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    base_oid = _init_git_base(repo)
    original = b"promoted-bytes\n"
    receipt = _insert_accepted_card(
        repo,
        task_id="HIST_LATER_TASK",
        request_id="req-hist-later",
        files={"src/later.py": original},
        base_oid=base_oid,
    )
    _git_commit_paths(repo, "promote", "src")
    (repo / "src" / "later.py").write_bytes(b"later-canonical-edit\n")

    builder.verify_provenance(
        repo, [_provenance_row("HIST_LATER_TASK", "req-hist-later", receipt)],
    )
    card = task_store.get_task(repo, "HIST_LATER_TASK")
    authenticated, reason = task_engine._validate_accepted_outcome_receipt(
        repo, card, "HIST_LATER_TASK", "req-hist-later", receipt,
    )
    assert authenticated is None
    assert reason == "accepted_outcome_receipt_canonical_hash_mismatch"

def test_verify_provenance_rejects_isolated_git_blobs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    base_oid = _init_git_base(repo)
    payload = b"isolated-blob-bytes\n"
    receipt = _insert_accepted_card(
        repo,
        task_id="HIST_ISOLATED_TASK",
        request_id="req-hist-isolated",
        files={"src/isolated.py": payload},
        base_oid=base_oid,
    )
    _git(repo, "hash-object", "-w", "--stdin", input_bytes=payload)

    with pytest.raises(builder.AcceptedTaskEvalError, match="provenance_absent_from_sealed_source"):
        builder.verify_provenance(
            repo, [_provenance_row("HIST_ISOLATED_TASK", "req-hist-isolated", receipt)],
        )


def test_verify_provenance_rejects_committed_symlink_matching_digest(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    base_oid = _init_git_base(repo)
    payload = b"sealed-file-bytes"
    receipt = _insert_accepted_card(
        repo,
        task_id="HIST_SYMLINK_TASK",
        request_id="req-hist-symlink",
        files={"src/linked.py": payload},
        base_oid=base_oid,
    )
    linked = repo / "src" / "linked.py"
    linked.unlink()
    try:
        linked.symlink_to(payload.decode("ascii"))
    except OSError as exc:
        pytest.skip(
            f"validation_unsupported_in_sandbox:symlink_privilege_not_held:{exc}"
        )
    _git_commit_paths(repo, "promote-symlink", "src")

    with pytest.raises(builder.AcceptedTaskEvalError, match="provenance_absent_from_sealed_source"):
        builder.verify_provenance(
            repo, [_provenance_row("HIST_SYMLINK_TASK", "req-hist-symlink", receipt)],
        )


def test_verify_provenance_rejects_hashes_split_across_commits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    base_oid = _init_git_base(repo)
    first = b"alpha-correct\n"
    second = b"beta-correct\n"
    receipt = _insert_accepted_card(
        repo,
        task_id="HIST_SPLIT_TASK",
        request_id="req-hist-split",
        files={"src/a.py": first, "src/b.py": second},
        base_oid=base_oid,
    )
    (repo / "src" / "b.py").write_bytes(b"beta-wrong\n")
    _git_commit_paths(repo, "only a.py matches", "src")
    (repo / "src" / "a.py").write_bytes(b"alpha-wrong\n")
    (repo / "src" / "b.py").write_bytes(second)
    _git_commit_paths(repo, "only b.py matches", "src")

    with pytest.raises(builder.AcceptedTaskEvalError, match="provenance_absent_from_sealed_source"):
        builder.verify_provenance(
            repo, [_provenance_row("HIST_SPLIT_TASK", "req-hist-split", receipt)],
        )


def test_verify_provenance_rejects_path_traversal(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    base_oid = _init_git_base(repo)
    receipt = _insert_accepted_card(
        repo,
        task_id="HIST_TRAV_TASK",
        request_id="req-hist-trav",
        files={"../escape.py": b"escaped\n"},
        base_oid=base_oid,
    )

    with pytest.raises(builder.AcceptedTaskEvalError, match="provenance_absent_from_sealed_source"):
        builder.verify_provenance(
            repo, [_provenance_row("HIST_TRAV_TASK", "req-hist-trav", receipt)],
        )


def test_verify_provenance_rejects_matching_commit_not_after_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    _git(repo, "init")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    payload = b"only-at-base\n"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "base_only.py").write_bytes(payload)
    _git(repo, "add", "src")
    _git(repo, "commit", "-m", "files at base")
    base_oid = _git(repo, "rev-parse", "HEAD")
    receipt = _insert_accepted_card(
        repo,
        task_id="HIST_BASE_TASK",
        request_id="req-hist-base",
        files={"src/base_only.py": payload},
        base_oid=base_oid,
    )
    (repo / "src" / "base_only.py").write_bytes(b"changed-after-base\n")
    _git_commit_paths(repo, "later edit", "src")

    with pytest.raises(builder.AcceptedTaskEvalError, match="provenance_absent_from_sealed_source"):
        builder.verify_provenance(
            repo, [_provenance_row("HIST_BASE_TASK", "req-hist-base", receipt)],
        )


# --------------------------------------------------------------------------
# discover_candidates on a multi-thousand-card store: measured, bounded
# whole-store query count regardless of candidate count (the O(N^2) rework)
# --------------------------------------------------------------------------

def _seed_noise_cards(repo: Path, *, count: int, accepted: int) -> None:
    """Insert ``count`` cards without a receipt; the first ``accepted`` of them
    carry an accepted_request_id.

    Cheap by design: no promoted files, no receipt computation. Each card with
    an accepted_request_id still carries enough shape for
    ``discover_candidates`` to attempt (and fail) authentication, exercising
    the same per-card store-touching path a real multi-thousand-card store
    would -- without the cost of making every one of them a genuinely
    authenticatable accepted outcome. The rest were never accepted, so
    discovery skips them before that path, as it skips most of a real store.
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
                        "status": "finished",
                        **({"accepted_request_id": f"req-noise-{i:06d}"} if i < accepted else {}),
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
    times, independent of N, while every candidate card still took the
    per-card export that used to repeat them.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    genuine_task_id, _genuine_request_id = _seed_accepted_scenario(
        repo, family="task_mcp", risk_tier="low", complexity="trivial", promoted_count=1,
    )
    accepted = 50
    _seed_noise_cards(repo, count=3000, accepted=accepted)

    call_counts = {"decisions": 0, "usage": 0, "exports": 0}
    real_decisions = task_store.latest_manager_decisions
    real_usage = task_store.list_usage_events
    real_export = builder.trajectory_export.export_attempt_trajectory

    def _counted_decisions(*args, **kwargs):
        call_counts["decisions"] += 1
        return real_decisions(*args, **kwargs)

    def _counted_usage(*args, **kwargs):
        call_counts["usage"] += 1
        return real_usage(*args, **kwargs)

    def _counted_export(*args, **kwargs):
        call_counts["exports"] += 1
        return real_export(*args, **kwargs)

    monkeypatch.setattr(task_store, "latest_manager_decisions", _counted_decisions)
    monkeypatch.setattr(task_store, "list_usage_events", _counted_usage)
    monkeypatch.setattr(builder.trajectory_export, "export_attempt_trajectory", _counted_export)

    candidates = builder.discover_candidates(repo)

    assert call_counts["exports"] == accepted + 1
    assert call_counts["decisions"] == 1
    assert call_counts["usage"] == 1
    assert {candidate["task_id"] for candidate in candidates} == {genuine_task_id}


# --------------------------------------------------------------------------
# the canonical-commit helper has exactly one definition, and src/ owns it
# --------------------------------------------------------------------------

def test_the_script_uses_the_shared_holding_commit_helper() -> None:
    """Moved, not copied: two copies of this answer eventually disagree."""
    assert builder.sdlc_attribution.first_holding_commit is (
        sdlc_attribution.first_holding_commit
    )
    for gone in (
        "_historical_descendant_commits", "_commit_path_sha256",
        "_git_run", "_git_clean_env", "_safe_git_oid",
    ):
        assert not hasattr(builder, gone), f"{gone} is still defined in the script"


def test_the_script_reexports_the_shared_historical_authority() -> None:
    """NF-2026-01132: the SDLC test stage and provenance ask one historical question."""
    assert builder.historical_accepted_outcome_authority is (
        sdlc_attribution.historical_accepted_outcome_authority
    )


def test_the_moved_helper_module_never_imports_from_scripts() -> None:
    source = Path(sdlc_attribution.__file__).read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
    offenders = [
        name for name in imported if name == "scripts" or name.startswith("scripts.")
    ]
    assert offenders == []
    shell_true_calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and any(
            keyword.arg == "shell"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        )
    ]
    assert shell_true_calls == []


def test_provenance_still_finds_the_first_commit_holding_every_promoted_hash(
    tmp_path: Path,
) -> None:
    """The shared helper answers exactly what the inline loop used to answer."""
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    base_oid = _init_git_base(repo)
    receipt = _insert_accepted_card(
        repo,
        task_id="HIST_SHARED_TASK",
        request_id="req-hist-shared",
        files={"src/shared.py": b"promoted-bytes\n"},
        base_oid=base_oid,
    )
    _git_commit_paths(repo, "promote", "src")

    commit, failure = sdlc_attribution.first_holding_commit(
        repo, base_oid, receipt["promoted_paths"], receipt["changed_path_hashes"],
    )

    assert failure == ""
    assert commit == _git(repo, "rev-parse", "HEAD")
    builder.verify_provenance(
        repo, [_provenance_row("HIST_SHARED_TASK", "req-hist-shared", receipt)],
    )


def test_an_unreadable_base_oid_is_not_reported_as_a_hash_mismatch(tmp_path: Path) -> None:
    """A history nobody could read is not a history that answered "no"."""
    repo = tmp_path / "repo"
    repo.mkdir()

    commit, failure = sdlc_attribution.first_holding_commit(
        repo, "0123456789abcdef", ["src/x.py"], {"src/x.py": "a" * 64},
    )

    assert commit is None
    assert failure == sdlc_attribution.GIT_UNAVAILABLE
