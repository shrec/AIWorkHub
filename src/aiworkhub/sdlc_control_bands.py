"""Deterministic, model-free control-band detector over SDLC outcome metrics."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import needfix_store, task_store
from .sdlc_outcome_metrics import (
    MAX_LIMIT,
    DecidedTaskCohort,
    aggregate,
    read_decided_task_cohort,
)
from .sqlite_readonly import connect_readonly

SCHEMA_ID = "aiworkhub.sdlc_control_bands.v1"
CONFIG_REL = (".aiworkhub", "config", "sdlc_bands.json")

DEFAULT_WINDOW_CARDS = 20
DEFAULT_BASELINE_CARDS = 100
DEFAULT_MIN_BASELINE = 30

_MIN_STDDEV = 0.25
# Float64-rounding tolerance so an exact z=2.0 fixture still lands in tier "needfix".
_TIER_EPSILON = 1e-9
_DIRECTIONS = frozenset({"lower_is_bad", "upper_is_bad"})
_METRIC_IDS = frozenset(
    {"first_pass_acceptance", "review_rounds_per_accepted_task", "validation_failed_rate"}
)

DEFAULT_METRICS: tuple[dict[str, Any], ...] = (
    {
        "id": "first_pass_acceptance", "direction": "lower_is_bad",
        "window_cards": DEFAULT_WINDOW_CARDS, "baseline_cards": DEFAULT_BASELINE_CARDS,
        "min_baseline": DEFAULT_MIN_BASELINE,
    },
    {
        "id": "review_rounds_per_accepted_task", "direction": "upper_is_bad",
        "window_cards": DEFAULT_WINDOW_CARDS, "baseline_cards": DEFAULT_BASELINE_CARDS,
        "min_baseline": DEFAULT_MIN_BASELINE,
    },
    {
        "id": "validation_failed_rate", "direction": "upper_is_bad",
        "window_cards": DEFAULT_WINDOW_CARDS, "baseline_cards": DEFAULT_BASELINE_CARDS,
        "min_baseline": DEFAULT_MIN_BASELINE,
    },
)


class ConfigError(ValueError):
    """``sdlc_bands.json`` is present but malformed or otherwise unusable."""


@dataclass(frozen=True)
class MetricBand:
    """One metric's recent-window-vs-baseline verdict."""

    metric_id: str
    direction: str
    status: str  # "ok" | "insufficient_population"
    tier: str | None  # None | "log" | "needfix"
    severity: str | None  # None | "medium" | "high"
    value: float | None
    baseline: float | None
    n: int
    baseline_n: int
    z: float | None
    window_task_ids: tuple[str, ...]


@dataclass(frozen=True)
class BandReport:
    """A full control-band evaluation for one repository."""

    schema_id: str
    repository_id: str
    config_sha256: str
    metrics: tuple[MetricBand, ...]


def _default_config_sha256() -> str:
    canonical = json.dumps({"metrics": list(DEFAULT_METRICS)}, sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_metric_entry(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ConfigError("each 'metrics' entry must be an object")
    metric_id = entry.get("id")
    direction = entry.get("direction")
    if not isinstance(metric_id, str) or not metric_id:
        raise ConfigError("metric 'id' must be a non-empty string")
    if metric_id not in _METRIC_IDS:
        raise ConfigError(f"metric {metric_id!r} is not a supported metric id")
    if direction not in _DIRECTIONS:
        raise ConfigError(f"metric {metric_id!r} has invalid direction: {direction!r}")
    normalized: dict[str, Any] = {"id": metric_id, "direction": direction}
    for key, default in (
        ("window_cards", DEFAULT_WINDOW_CARDS),
        ("baseline_cards", DEFAULT_BASELINE_CARDS),
        ("min_baseline", DEFAULT_MIN_BASELINE),
    ):
        value = entry.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(f"metric {metric_id!r} field {key!r} must be a positive integer")
        normalized[key] = value
    return normalized


def _load_config(repo_root: str | Path) -> tuple[tuple[dict[str, Any], ...], str]:
    """Load and validate ``sdlc_bands.json``; a missing file uses built-in defaults."""

    path = Path(repo_root, *CONFIG_REL)
    if not path.is_file():
        return DEFAULT_METRICS, _default_config_sha256()
    raw = path.read_bytes()
    config_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(f"malformed sdlc_bands.json: {exc}") from exc
    has_metrics_list = isinstance(data, dict) and isinstance(data.get("metrics"), list)
    if not has_metrics_list or not data["metrics"]:
        raise ConfigError("sdlc_bands.json must be an object with a non-empty 'metrics' list")
    metrics = tuple(_validate_metric_entry(entry) for entry in data["metrics"])
    return metrics, config_sha256


def _sample_stddev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(variance)


def _rate_z(p: float, p0: float, n: int) -> float:
    p0_clamped = min(max(p0, 0.5 / n), 1 - 0.5 / n)
    se = math.sqrt(p0_clamped * (1 - p0_clamped) / n)
    return (p - p0_clamped) / se


def _classify(z: float, direction: str) -> tuple[str | None, str | None]:
    """Only the bad direction counts: a good-direction z never yields a tier."""

    bad_z = z if direction == "upper_is_bad" else -z
    if bad_z >= 3 - _TIER_EPSILON:
        return "needfix", "high"
    if bad_z >= 2 - _TIER_EPSILON:
        return "needfix", "medium"
    if bad_z >= 1 - _TIER_EPSILON:
        return "log", None
    return None, None


def _insufficient(
    metric_id: str, direction: str, window_ids: tuple[str, ...], baseline_n: int
) -> MetricBand:
    return MetricBand(
        metric_id=metric_id, direction=direction, status="insufficient_population",
        tier=None, severity=None, value=None, baseline=None,
        n=0, baseline_n=baseline_n, z=None, window_task_ids=window_ids,
    )


def _rate_band(
    metric_id: str, direction: str, window_ids: tuple[str, ...],
    wnum: int, wden: int, bnum: int, bden: int,
) -> MetricBand:
    if wden <= 0 or bden <= 0:
        return _insufficient(metric_id, direction, window_ids, bden)
    value = wnum / wden
    baseline = bnum / bden
    z = _rate_z(value, baseline, wden)
    tier, severity = _classify(z, direction)
    return MetricBand(
        metric_id=metric_id, direction=direction, status="ok", tier=tier, severity=severity,
        value=value, baseline=baseline, n=wden, baseline_n=bden, z=z, window_task_ids=window_ids,
    )


def _group_by_task(event_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in event_rows:
        task_id = str(row.get("task_id") or "")
        if task_id:
            grouped.setdefault(task_id, []).append(row)
    return grouped


def _first_pass_band(
    cfg: dict[str, Any], event_rows: list[dict[str, Any]], cohort: DecidedTaskCohort,
    repository_id: str, window_ids: tuple[str, ...], baseline_ids: tuple[str, ...],
) -> MetricBand:
    window_cohort = DecidedTaskCohort(window_ids, cohort.complete)
    baseline_cohort = DecidedTaskCohort(baseline_ids, cohort.complete)
    window_result = aggregate(
        event_rows, [], repository_id=repository_id, cohort=window_cohort, limit=MAX_LIMIT
    )
    baseline_result = aggregate(
        event_rows, [], repository_id=repository_id, cohort=baseline_cohort, limit=MAX_LIMIT
    )
    w = window_result["first_pass_acceptance"]
    b = baseline_result["first_pass_acceptance"]
    return _rate_band(
        cfg["id"], cfg["direction"], window_ids,
        w["numerator"], w["denominator"], b["numerator"], b["denominator"],
    )


def _per_task_review_rounds(
    by_task: dict[str, list[dict[str, Any]]], task_ids: tuple[str, ...],
    complete: frozenset[str], repository_id: str,
) -> list[int]:
    """Each task's own review-round count, computed via ``aggregate`` on a singleton cohort."""

    values: list[int] = []
    for task_id in task_ids:
        if task_id not in complete:
            continue
        result = aggregate(
            by_task.get(task_id, []), [], repository_id=repository_id,
            cohort=DecidedTaskCohort((task_id,), frozenset({task_id})), limit=MAX_LIMIT,
        )
        block = result["review_rounds_per_accepted_task"]
        if block["denominator"] == 1:
            values.append(block["numerator"])
    return values


def _review_rounds_band(
    cfg: dict[str, Any], event_rows: list[dict[str, Any]], cohort: DecidedTaskCohort,
    repository_id: str, window_ids: tuple[str, ...], baseline_ids: tuple[str, ...],
) -> MetricBand:
    metric_id, direction = cfg["id"], cfg["direction"]
    by_task = _group_by_task(event_rows)
    window_values = _per_task_review_rounds(by_task, window_ids, cohort.complete, repository_id)
    baseline_values = _per_task_review_rounds(
        by_task, baseline_ids, cohort.complete, repository_id
    )
    if not window_values or not baseline_values:
        return _insufficient(metric_id, direction, window_ids, len(baseline_values))
    n = len(window_values)
    value = sum(window_values) / n
    baseline = sum(baseline_values) / len(baseline_values)
    s0 = max(_sample_stddev(baseline_values), _MIN_STDDEV)
    z = (value - baseline) / (s0 / math.sqrt(n))
    tier, severity = _classify(z, direction)
    return MetricBand(
        metric_id=metric_id, direction=direction, status="ok", tier=tier, severity=severity,
        value=value, baseline=baseline, n=n, baseline_n=len(baseline_values), z=z,
        window_task_ids=window_ids,
    )


def _terminal_outcome_counts(conn: Any, task_ids: frozenset[str]) -> tuple[int, int]:
    """(validation_failed count, terminal-outcome count) among terminal_review events for these tasks."""

    if not task_ids:
        return 0, 0
    placeholders = ",".join("?" for _ in task_ids)
    rows = conn.execute(
        f"SELECT json_extract(payload_json, '$.substatus') FROM task_events "
        f"WHERE event = 'terminal_review' AND task_id IN ({placeholders})",
        tuple(task_ids),
    ).fetchall()
    total = len(rows)
    failed = sum(1 for (substatus,) in rows if substatus == "validation_failed")
    return failed, total


def _validation_failed_band(
    cfg: dict[str, Any], conn: Any,
    window_ids: tuple[str, ...], baseline_ids: tuple[str, ...],
) -> MetricBand:
    wnum, wden = _terminal_outcome_counts(conn, frozenset(window_ids))
    bnum, bden = _terminal_outcome_counts(conn, frozenset(baseline_ids))
    return _rate_band(cfg["id"], cfg["direction"], window_ids, wnum, wden, bnum, bden)


def evaluate(repo_root: str | Path, repository_id: str) -> BandReport:
    """Pure, read-only control-band evaluation. Never writes to any store."""

    metrics_config, config_sha256 = _load_config(repo_root)

    readiness = task_store.storage_readiness(Path(repo_root))
    if not readiness.ready:
        raise task_store.StorageNotReadyError(readiness.reason)
    conn = connect_readonly(readiness.canonical_db)
    try:
        # ``limit`` bounds raw event rows, not decided cards; card counts are sliced below.
        event_rows, cohort = read_decided_task_cohort(conn, MAX_LIMIT)

        bands: list[MetricBand] = []
        for cfg in metrics_config:
            selected = cohort.selected
            window_ids = selected[: cfg["window_cards"]]
            baseline_ids = selected[cfg["window_cards"] : cfg["window_cards"] + cfg["baseline_cards"]]
            if len(baseline_ids) < cfg["min_baseline"]:
                bands.append(_insufficient(cfg["id"], cfg["direction"], window_ids, len(baseline_ids)))
                continue
            if cfg["id"] == "first_pass_acceptance":
                band = _first_pass_band(
                    cfg, event_rows, cohort, repository_id, window_ids, baseline_ids
                )
            elif cfg["id"] == "review_rounds_per_accepted_task":
                band = _review_rounds_band(
                    cfg, event_rows, cohort, repository_id, window_ids, baseline_ids
                )
            else:
                band = _validation_failed_band(cfg, conn, window_ids, baseline_ids)
            bands.append(band)
    finally:
        conn.close()

    return BandReport(
        schema_id=SCHEMA_ID, repository_id=repository_id,
        config_sha256=config_sha256, metrics=tuple(bands),
    )


def file_breaches(repo_root: str | Path, repository_id: str, report: BandReport) -> list[str]:
    """File or refresh a NeedFix per breaching metric; idempotent via the store's dedupe key."""

    filed: list[str] = []
    for band in report.metrics:
        if band.tier != "needfix":
            continue
        scope = f"sdlc_band:{band.metric_id}:{band.direction}"
        title = f"SDLC control band breach: {band.metric_id}"
        description = (
            f"Deterministic SDLC control-band detector: {band.metric_id} "
            f"({band.direction}) breached its rolling baseline control band."
        )
        evidence = {
            "metric_id": band.metric_id,
            "direction": band.direction,
            "value": band.value,
            "baseline": band.baseline,
            "n": band.n,
            "baseline_n": band.baseline_n,
            "z": band.z,
            "tier": band.tier,
            "window_task_ids": list(band.window_task_ids),
            "config_sha256": report.config_sha256,
        }
        row = needfix_store.add_needfix(
            repo_root,
            title=title,
            description=description,
            scope=scope,
            provenance={"origin": "sdlc_control_band_detector"},
            evidence=evidence,
            status="captured",
            kind="investigation",
            severity=band.severity,
            repository_id=repository_id,
        )
        severity_rank = {"medium": 1, "high": 2}
        outranks = severity_rank.get(band.severity, 0) > severity_rank.get(row.get("severity"), 0)
        if row.get("evidence") != evidence or outranks:
            needfix_store.update_needfix(
                repo_root, row["id"],
                evidence=evidence,
                severity=band.severity if outranks else row.get("severity"),
            )
        filed.append(row["id"])
    return filed
