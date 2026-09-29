"""Tests for the deterministic SDLC control-band detector."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import needfix_store, sdlc_control_bands, task_store

_REPO = "repo-control-bands"
_LEDGER_DIR = (".aiworkhub", "runtime", "process_logs")


def _receipt(task_id: str, request_id: str, digest: str = "a" * 64, claim_epoch: int = 1) -> dict:
    hashes = {"fixed.txt": digest}
    unsigned = {
        "schema_id": needfix_store.ACCEPTED_OUTCOME_RECEIPT_SCHEMA_ID,
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": claim_epoch,
        "base_oid": "base",
        "promoted_paths": ["fixed.txt"],
        "changed_path_hashes": hashes,
        "attempt_artifact_manifest_id": digest,
        "repository_revision": "sha256:" + hashlib.sha256(json.dumps(
            {"base_oid": "base", "changed_path_hashes": hashes},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest(),
    }
    return {
        **unsigned,
        "receipt_id": "sha256:" + hashlib.sha256(json.dumps(
            unsigned, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest(),
    }


def _seed(task_db: Path, rows: list[tuple[int, str, str, dict]]) -> None:
    conn = sqlite3.connect(str(task_db))
    try:
        conn.executescript(task_store.SCHEMA)
        task_store.ensure_event_indexes(conn)
        conn.executemany(
            "INSERT INTO task_events(event_id, task_id, event, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (event_id, task_id, event, json.dumps(payload), "2026-09-20T00:00:00Z")
                for event_id, task_id, event, payload in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _use_store(monkeypatch: pytest.MonkeyPatch, task_db: Path) -> None:
    monkeypatch.setattr(
        task_store, "storage_readiness",
        lambda root: SimpleNamespace(ready=True, reason="", canonical_db=task_db),
    )


def _build_rate_population(
    task_db: Path, *, window_first_pass: int, window_total: int,
    baseline_first_pass: int, baseline_total: int,
) -> None:
    """Seed ``baseline_total`` older decided tasks then ``window_total`` newer ones."""

    counter = itertools.count(1)
    rows: list[tuple[int, str, str, dict]] = []

    def emit(task_id: str, rejections: int) -> None:
        request_id = f"R-{task_id}"
        for _ in range(rejections):
            rows.append((next(counter), task_id, "reject_review", {}))
        rows.append((next(counter), task_id, "accept_review", {
            "request_id": request_id,
            "accepted_outcome_receipt": _receipt(task_id, request_id),
        }))

    for i in range(baseline_total):
        emit(f"BASE-{i:04d}", 0 if i < baseline_first_pass else 1)
    for i in range(window_total):
        emit(f"WIN-{i:04d}", 0 if i < window_first_pass else 1)

    _seed(task_db, rows)


def _write_config(repo_root: Path, metrics: list[dict]) -> Path:
    config_dir = repo_root / ".aiworkhub" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "sdlc_bands.json"
    path.write_text(json.dumps({"metrics": metrics}), encoding="utf-8")
    return path


def _first_pass_metric(**overrides) -> dict:
    metric = {
        "id": "first_pass_acceptance", "direction": "lower_is_bad",
        "window_cards": 25, "baseline_cards": 100, "min_baseline": 30,
    }
    metric.update(overrides)
    return metric


def _band(report, metric_id: str = "first_pass_acceptance"):
    matches = [band for band in report.metrics if band.metric_id == metric_id]
    assert len(matches) == 1
    return matches[0]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _use_store(monkeypatch, repo_root / "tasks.db")
    needfix_store.initialize_repository(repo_root)
    return repo_root


# n=25, p0=0.8 => se = sqrt(0.8*0.2/25) = 0.08, so each sigma step is 2 window first-passes.


def test_two_sigma_window_is_needfix_medium_and_identical_second_call_is_a_no_op(repo):
    _write_config(repo, [_first_pass_metric()])
    _build_rate_population(
        repo / "tasks.db",
        window_first_pass=16, window_total=25, baseline_first_pass=80, baseline_total=100,
    )

    needfix_store.initialize_repository(repo)
    needfix_path = repo / ".aiworkhub" / "tasking" / "needfix.sqlite"
    needfix_bytes_before = needfix_path.read_bytes()

    report = sdlc_control_bands.evaluate(repo, _REPO)

    assert needfix_path.read_bytes() == needfix_bytes_before

    band = _band(report)
    assert band.status == "ok"
    assert band.tier == "needfix"
    assert band.severity == "medium"
    assert band.z == pytest.approx(-2.0, abs=1e-6)
    assert band.n == 25

    filed_once = sdlc_control_bands.file_breaches(repo, _REPO, report)
    assert len(filed_once) == 1
    assert needfix_store.count_needfix(repo) == 1
    needfix_id = filed_once[0]
    events_after_first = needfix_store.list_events(repo, needfix_id)
    assert not any(event.get("event") == "updated" for event in events_after_first)

    filed_twice = sdlc_control_bands.file_breaches(repo, _REPO, report)
    assert filed_twice == [needfix_id]
    assert needfix_store.count_needfix(repo) == 1
    events_after_second = needfix_store.list_events(repo, needfix_id)
    assert not any(event.get("event") == "updated" for event in events_after_second)

    row = needfix_store.get_needfix(repo, needfix_id)
    assert row["evidence"]["metric_id"] == "first_pass_acceptance"
    assert row["evidence"]["z"] == pytest.approx(-2.0, abs=1e-6)
    assert row["evidence"]["config_sha256"] == report.config_sha256


def test_three_sigma_files_severity_high(repo):
    _write_config(repo, [_first_pass_metric()])
    _build_rate_population(
        repo / "tasks.db",
        window_first_pass=14, window_total=25, baseline_first_pass=80, baseline_total=100,
    )

    report = sdlc_control_bands.evaluate(repo, _REPO)
    band = _band(report)
    assert band.z == pytest.approx(-3.0, abs=1e-6)
    assert band.tier == "needfix"
    assert band.severity == "high"

    filed = sdlc_control_bands.file_breaches(repo, _REPO, report)
    assert len(filed) == 1
    row = needfix_store.get_needfix(repo, filed[0])
    assert row["severity"] == "high"


def test_one_sigma_returns_log_tier_and_writes_nothing(repo):
    _write_config(repo, [_first_pass_metric()])
    _build_rate_population(
        repo / "tasks.db",
        window_first_pass=18, window_total=25, baseline_first_pass=80, baseline_total=100,
    )

    report = sdlc_control_bands.evaluate(repo, _REPO)
    band = _band(report)
    assert band.z == pytest.approx(-1.0, abs=1e-6)
    assert band.tier == "log"
    assert band.severity is None

    filed = sdlc_control_bands.file_breaches(repo, _REPO, report)
    assert filed == []
    assert needfix_store.count_needfix(repo) == 0


def test_breach_in_good_direction_never_files(repo):
    _write_config(repo, [_first_pass_metric()])
    # Window first-pass rate is *better* than baseline (0.96 vs 0.8): a 2-sigma
    # improvement in a lower_is_bad metric must never file.
    _build_rate_population(
        repo / "tasks.db",
        window_first_pass=24, window_total=25, baseline_first_pass=80, baseline_total=100,
    )

    report = sdlc_control_bands.evaluate(repo, _REPO)
    band = _band(report)
    assert band.z > 0
    assert band.tier is None
    assert band.severity is None

    filed = sdlc_control_bands.file_breaches(repo, _REPO, report)
    assert filed == []
    assert needfix_store.count_needfix(repo) == 0


def test_baseline_below_min_baseline_is_insufficient_population(repo):
    _write_config(repo, [_first_pass_metric()])
    # Only 10 older tasks exist at all, far below min_baseline=30.
    _build_rate_population(
        repo / "tasks.db",
        window_first_pass=16, window_total=25, baseline_first_pass=8, baseline_total=10,
    )

    report = sdlc_control_bands.evaluate(repo, _REPO)
    band = _band(report)
    assert band.status == "insufficient_population"
    assert band.tier is None
    assert band.n == 0
    assert band.baseline_n == 10

    filed = sdlc_control_bands.file_breaches(repo, _REPO, report)
    assert filed == []
    assert needfix_store.count_needfix(repo) == 0


@pytest.mark.parametrize("baseline_first_pass", [0, 30])
def test_p0_of_zero_or_one_does_not_divide_by_zero(repo, baseline_first_pass):
    _write_config(repo, [_first_pass_metric(baseline_cards=30)])
    _build_rate_population(
        repo / "tasks.db",
        window_first_pass=20, window_total=25,
        baseline_first_pass=baseline_first_pass, baseline_total=30,
    )

    report = sdlc_control_bands.evaluate(repo, _REPO)
    band = _band(report)
    assert band.status == "ok"
    assert band.z is not None
    assert math.isfinite(band.z)


def test_missing_config_uses_defaults(repo):
    # No .aiworkhub/config/sdlc_bands.json is written at all.
    _build_rate_population(
        repo / "tasks.db",
        window_first_pass=16, window_total=20, baseline_first_pass=80, baseline_total=100,
    )

    report = sdlc_control_bands.evaluate(repo, _REPO)

    expected_sha256 = hashlib.sha256(json.dumps(
        {"metrics": list(sdlc_control_bands.DEFAULT_METRICS)}, sort_keys=True,
    ).encode("utf-8")).hexdigest()
    assert report.config_sha256 == expected_sha256

    metric_ids = {band.metric_id for band in report.metrics}
    assert metric_ids == {
        "first_pass_acceptance", "review_rounds_per_accepted_task", "validation_failed_rate",
    }
    fpa = _band(report)
    assert len(fpa.window_task_ids) == sdlc_control_bands.DEFAULT_WINDOW_CARDS


def test_malformed_config_raises_typed_error(repo):
    config_dir = repo / ".aiworkhub" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "sdlc_bands.json").write_text("{not valid json", encoding="utf-8")

    with pytest.raises(sdlc_control_bands.ConfigError):
        sdlc_control_bands.evaluate(repo, _REPO)


def test_malformed_config_missing_metrics_key_raises_typed_error(repo):
    config_dir = repo / ".aiworkhub" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "sdlc_bands.json").write_text(json.dumps({"nope": []}), encoding="utf-8")

    with pytest.raises(sdlc_control_bands.ConfigError):
        sdlc_control_bands.evaluate(repo, _REPO)


def test_config_sha256_appears_in_evidence(repo):
    config_path = _write_config(repo, [_first_pass_metric()])
    _build_rate_population(
        repo / "tasks.db",
        window_first_pass=14, window_total=25, baseline_first_pass=80, baseline_total=100,
    )
    expected_sha256 = hashlib.sha256(config_path.read_bytes()).hexdigest()

    report = sdlc_control_bands.evaluate(repo, _REPO)
    assert report.config_sha256 == expected_sha256

    filed = sdlc_control_bands.file_breaches(repo, _REPO, report)
    row = needfix_store.get_needfix(repo, filed[0])
    assert row["evidence"]["config_sha256"] == expected_sha256


def test_evaluate_performs_no_writes(repo):
    _write_config(repo, [_first_pass_metric()])
    _build_rate_population(
        repo / "tasks.db",
        window_first_pass=16, window_total=25, baseline_first_pass=80, baseline_total=100,
    )
    needfix_store.initialize_repository(repo)
    needfix_path = repo / ".aiworkhub" / "tasking" / "needfix.sqlite"
    before = needfix_path.read_bytes()
    before_count = needfix_store.count_needfix(repo)

    sdlc_control_bands.evaluate(repo, _REPO)

    assert needfix_path.read_bytes() == before
    assert needfix_store.count_needfix(repo) == before_count


def test_all_default_metrics_evaluate_without_error(repo):
    _build_rate_population(
        repo / "tasks.db",
        window_first_pass=16, window_total=20, baseline_first_pass=80, baseline_total=100,
    )
    counter = itertools.count(100_000)
    rows = []
    for i in range(40):
        task_id = f"WIN-{i:04d}" if i < 20 else f"BASE-{i - 20:04d}"
        substatus = "validation_failed" if i % 5 == 0 else "review_ready"
        rows.append((next(counter), task_id, "terminal_review", {"substatus": substatus}))
    _seed(repo / "tasks.db", rows)

    report = sdlc_control_bands.evaluate(repo, _REPO)

    assert {band.metric_id for band in report.metrics} == {
        "first_pass_acceptance", "review_rounds_per_accepted_task", "validation_failed_rate",
    }
    for band in report.metrics:
        assert band.status in ("ok", "insufficient_population")
        assert band.tier in (None, "log", "needfix")


def test_repeated_breach_upgrades_severity_without_duplicate_row(repo):
    medium_report = sdlc_control_bands.BandReport(
        schema_id=sdlc_control_bands.SCHEMA_ID, repository_id=_REPO,
        config_sha256="a" * 64,
        metrics=(
            sdlc_control_bands.MetricBand(
                metric_id="first_pass_acceptance", direction="lower_is_bad", status="ok",
                tier="needfix", severity="medium", value=0.72, baseline=0.82,
                n=25, baseline_n=100, z=-2.0, window_task_ids=("WIN-0000",),
            ),
        ),
    )
    filed_once = sdlc_control_bands.file_breaches(repo, _REPO, medium_report)
    assert len(filed_once) == 1
    needfix_id = filed_once[0]
    row = needfix_store.get_needfix(repo, needfix_id)
    assert row["severity"] == "medium"

    high_report = sdlc_control_bands.BandReport(
        schema_id=sdlc_control_bands.SCHEMA_ID, repository_id=_REPO,
        config_sha256="a" * 64,
        metrics=(
            sdlc_control_bands.MetricBand(
                metric_id="first_pass_acceptance", direction="lower_is_bad", status="ok",
                tier="needfix", severity="high", value=0.64, baseline=0.82,
                n=25, baseline_n=100, z=-3.0, window_task_ids=("WIN-0000",),
            ),
        ),
    )
    filed_twice = sdlc_control_bands.file_breaches(repo, _REPO, high_report)
    assert filed_twice == [needfix_id]
    assert needfix_store.count_needfix(repo) == 1
    row_after = needfix_store.get_needfix(repo, needfix_id)
    assert row_after["severity"] == "high"
    assert row_after["evidence"]["z"] == pytest.approx(-3.0, abs=1e-6)
