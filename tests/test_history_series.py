"""Tests for the canonical-store-backed dashboard history series.

Every test here seeds a real canonical store and asserts on a measured
number, because the defect this module fixes was precisely a projection that
looked healthy while reading the wrong population.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from aiworkhub import history_series, sqlite_readonly, task_store


def _seed(repo: Path, cards, *, tasks_extra=None) -> None:
    """Create a canonical store from ``(task_id, topic, day, [events])``.

    Each event is ``(event, payload_dict)`` appended in order, so the highest
    ``event_id`` for a task is its latest decision.
    """
    task_store.initialize_repository(repo)
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        for task_id, topic, day, events in cards:
            created = f"{day}T00:00:00+00:00"
            card = {"task_id": task_id, "runner": "codex_worker", "topic": topic}
            card.update((tasks_extra or {}).get(task_id, {}))
            conn.execute(
                "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
                "priority, objective, card_json, created_at, updated_at, "
                "claimed_by, claimed_at, started_at, completed_at) "
                "VALUES (?, 'codex_worker', ?, 'finished', 'done', '', '', ?, ?, "
                "?, '', '', ?, ?)",
                (
                    task_id,
                    topic,
                    json.dumps(card),
                    created,
                    created,
                    f"{day}T00:01:00+00:00",
                    f"{day}T00:03:00+00:00",
                ),
            )
            for event, payload in events:
                conn.execute(
                    "INSERT INTO task_events(task_id, event, runner, "
                    "payload_json, created_at) VALUES (?, ?, 'codex', ?, ?)",
                    (task_id, event, json.dumps(payload), created),
                )
        conn.commit()
    finally:
        conn.close()


def _terminal(substatus: str, *, nothing_measured=None) -> dict:
    payload: dict = {"substatus": substatus}
    if nothing_measured is not None:
        payload["deterministic_verification"] = {
            "evidence_verdict": {"nothing_measured": nothing_measured}
        }
    return payload


def _usage(tokens: int, *, cost=None, role="worker", model="m1", provider="p1") -> dict:
    payload = {
        "total_tokens": tokens,
        "role": role,
        "model": model,
        "provider": provider,
    }
    if cost is None:
        payload["cost_observed"] = False
    else:
        payload["cost_observed"] = True
        payload["cost_usd"] = cost
    return payload


def _build(repo: Path):
    return history_series.build_history_series(repo, use_cache=False)


# ---------------------------------------------------------------------------
# The reviewer-child rule -- the defect that cost a full engineering day.
# ---------------------------------------------------------------------------


def test_a_reviewer_child_never_enters_a_work_card_series(tmp_path: Path):
    """529 of 870 lifetime accepts were reviewer children.

    Summing them into the work numerator manufactured an apparent acceptance
    collapse that never happened. Here reviewer children are accepted 2/2 and
    work cards 1/2: the mixed figure (3/4 = 75%) must appear in no work-card
    series, and the reviewer children must still be visible separately.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(
        repo,
        [
            ("WORK_OK", "task_mcp", "2026-08-01", [("accept_review", {})]),
            ("WORK_NO", "task_mcp", "2026-08-01", [("reject_review", {})]),
            ("REV_A", "quality_review", "2026-08-01", [("accept_review", {})]),
            ("REV_B", "quality_review", "2026-08-01", [("accept_review", {})]),
        ],
    )

    series = _build(repo)
    totals = series["daily_decisions"]["totals"]

    assert totals["work_card"] == {
        "accepted": 1,
        "rejected": 1,
        "decided": 2,
        "acceptance_rate": 0.5,
    }
    # The reviewer children are separated, never lost.
    assert totals["reviewer_child"]["accepted"] == 2
    assert totals["reviewer_child"]["decided"] == 2
    # The mixed accounting appears nowhere in the work series.
    assert totals["work_card"]["accepted"] != 3
    assert totals["work_card"]["acceptance_rate"] != 0.75
    assert series["excluded_topics"] == ["quality_review"]


def test_a_reviewer_child_never_enters_the_work_card_outcome_or_depth_series(
    tmp_path: Path,
):
    """The exclusion holds across every series, not only the headline one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(
        repo,
        [
            ("WORK_OK", "task_mcp", "2026-08-01", [("accept_review", {})]),
            (
                "REV_A",
                "quality_review",
                "2026-08-01",
                [("accept_review", {}), ("terminal_review", _terminal("review_ready"))],
            ),
        ],
    )

    series = _build(repo)

    work_runners = {
        row["runner"] for row in series["model_outcomes"]["work_card"]["runners"]
    }
    reviewer_rows = series["model_outcomes"]["reviewer_child"]["runners"]
    assert sum(row["decided"] for row in series["model_outcomes"]["work_card"]["runners"]) == 1
    assert sum(row["decided"] for row in reviewer_rows) == 1
    assert work_runners == {"codex_worker"}

    depth = series["retry_economics"]["rejection_depth"]["by_population"]
    assert depth["work_card"]["0"]["cards"] == 1
    assert depth["reviewer_child"]["0"]["cards"] == 1

    composition = series["terminal_composition"]["by_population"]
    assert composition["reviewer_child"]["baseline"] == 1
    assert composition["work_card"]["baseline"] == 0


def test_a_decision_with_no_task_row_is_unknown_topic_never_work(tmp_path: Path):
    """A card whose ``tasks`` row is gone cannot be established as work."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(repo, [("WORK_OK", "task_mcp", "2026-08-01", [("accept_review", {})])])
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, "
            "created_at) VALUES ('GHOST', 'accept_review', 'x', '{}', "
            "'2026-08-01T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    totals = _build(repo)["daily_decisions"]["totals"]

    assert totals["work_card"]["accepted"] == 1
    assert totals["unknown_topic"]["accepted"] == 1


# ---------------------------------------------------------------------------
# One vote per distinct card.
# ---------------------------------------------------------------------------


def test_a_card_rejected_many_times_in_one_day_is_one_card(tmp_path: Path):
    """A card rejected 46 times in one day is one card, not 46 data points."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(
        repo,
        [
            (
                "WORK_LOOP",
                "task_mcp",
                "2026-08-01",
                [("reject_review", {}) for _ in range(46)],
            )
        ],
    )

    series = _build(repo)

    assert series["daily_decisions"]["totals"]["work_card"]["rejected"] == 1
    assert series["daily_decisions"]["days"][0]["work_card"]["rejected"] == 1
    # The depth series is where the 46 attempts are legitimately visible.
    depth = series["retry_economics"]["rejection_depth"]["by_population"]["work_card"]
    assert depth["46"]["cards"] == 1
    assert depth["46"]["eventually_accepted"] == 0


def test_a_cards_latest_decision_wins_for_the_day(tmp_path: Path):
    """Rejected then accepted on the same day counts once, as accepted."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(
        repo,
        [
            (
                "WORK_LANDED",
                "task_mcp",
                "2026-08-01",
                [("reject_review", {}), ("accept_review", {})],
            )
        ],
    )

    totals = _build(repo)["daily_decisions"]["totals"]

    assert totals["work_card"]["accepted"] == 1
    assert totals["work_card"]["rejected"] == 0


# ---------------------------------------------------------------------------
# Resolution: one number for success, a full taxonomy for failure.
# ---------------------------------------------------------------------------


def test_the_rare_failure_classes_survive_a_taxonomy_bound(tmp_path: Path):
    """A bound cuts the COMMON classes and names them; never the rare tail.

    ``launch_failed`` 0.5%, ``timed_out`` 0.4%, ``liveness_lost`` 0.2% and
    ``scope_rejected`` 0.1% are the classes worth learning from. A silent
    top-N would delete exactly those and keep only what is already obvious.
    """
    counts = {
        "validation_failed": 1613,
        "worker_failed": 544,
        "finalize_failed": 187,
        "cancelled": 146,
        "launch_failed": 30,
        "timed_out": 24,
        "liveness_lost": 12,
        "scope_rejected": 9,
    }

    kept, bound = history_series._bounded_taxonomy(counts, 4)

    assert bound["applied"] is True
    assert bound["cut_from"] == "most_common"
    # The rare tail survived in full.
    assert set(kept) == {"scope_rejected", "liveness_lost", "timed_out", "launch_failed"}
    # ... and what was cut is named, never silently dropped.
    assert [item["substatus"] for item in bound["omitted"]] == [
        "validation_failed",
        "worker_failed",
        "finalize_failed",
        "cancelled",
    ]
    assert bound["classes"] == 8


def test_an_unbounded_taxonomy_reports_no_cut(tmp_path: Path):
    kept, bound = history_series._bounded_taxonomy({"a": 5, "b": 1}, 40)

    assert bound["applied"] is False
    assert bound["omitted"] == []
    # Ordered most-common-first for a reader when nothing was cut.
    assert list(kept) == ["a", "b"]


def test_success_is_one_number_and_failure_keeps_full_resolution(tmp_path: Path):
    """``review_ready`` collapses to a baseline count; failures do not."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(
        repo,
        [
            ("A", "task_mcp", "2026-08-01", [("terminal_review", _terminal("review_ready"))]),
            ("B", "task_mcp", "2026-08-01", [("terminal_review", _terminal("review_ready"))]),
            ("C", "task_mcp", "2026-08-01", [("terminal_review", _terminal("validation_failed"))]),
            ("D", "task_mcp", "2026-08-01", [("terminal_failure", _terminal("timed_out"))]),
        ],
    )

    composition = _build(repo)["terminal_composition"]

    assert composition["baseline"]["substatus"] == "review_ready"
    assert composition["baseline"]["events"] == 2
    assert composition["baseline"]["share"] == 0.5
    # Each failure class keeps its own count, including the rare one.
    assert composition["failures"]["by_substatus"] == {
        "validation_failed": 1,
        "timed_out": 1,
    }
    assert composition["failures"]["events"] == 2


def test_a_failure_series_carries_its_cause_not_only_its_count(tmp_path: Path):
    """``validation_failed`` says what; ``nothing_measured`` says why."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(
        repo,
        [
            (
                "A",
                "task_mcp",
                "2026-08-01",
                [("terminal_review", _terminal("validation_failed", nothing_measured=True))],
            ),
            (
                "B",
                "task_mcp",
                "2026-08-01",
                [("terminal_review", _terminal("validation_failed", nothing_measured=False))],
            ),
            (
                "C",
                "task_mcp",
                "2026-08-01",
                [("terminal_review", _terminal("validation_failed"))],
            ),
        ],
    )

    causes = _build(repo)["terminal_composition"]["evidence_cause_by_substatus"]

    assert causes["validation_failed"] == {
        "evidence_measured": 1,
        "nothing_measured": 1,
        "verdict_absent": 1,
    }


# ---------------------------------------------------------------------------
# Honest denominators.
# ---------------------------------------------------------------------------


def test_an_unknown_cost_is_never_summed_as_zero(tmp_path: Path):
    """Only records declaring ``cost_observed`` contribute to the cost sum."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(
        repo,
        [
            ("A", "task_mcp", "2026-08-01", [("usage_record", _usage(100, cost=1.5))]),
            ("B", "task_mcp", "2026-08-01", [("usage_record", _usage(200))]),
            ("C", "task_mcp", "2026-08-01", [("usage_record", _usage(300))]),
        ],
    )

    quality = _build(repo)["usage"]["cost_quality"]

    assert quality["cost_observed_records"] == 1
    assert quality["cost_unknown_records"] == 2
    assert quality["coverage"] == pytest.approx(1 / 3, rel=1e-3)
    assert quality["cost_usd_observed"] == pytest.approx(1.5)
    # The full spend is unknown, and says so rather than reporting the
    # observed lower bound as a total.
    assert quality["cost_usd_total"] is None
    assert quality["zero_cost_is_free"] is False
    assert quality["absent_metrics_are_unknown_not_zero"] is True


def test_a_bucket_whose_cost_is_partial_declares_itself_a_lower_bound(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(
        repo,
        [
            ("A", "task_mcp", "2026-08-01", [("usage_record", _usage(100, cost=2.0))]),
            ("B", "task_mcp", "2026-08-01", [("usage_record", _usage(100))]),
        ],
    )

    by_role = _build(repo)["usage"]["by_role"]

    assert by_role["worker"]["cost_is_lower_bound"] is True
    assert by_role["worker"]["cost_coverage"] == 0.5


def test_a_transport_with_no_token_telemetry_is_reported_not_dropped(tmp_path: Path):
    """2,189 vscode_lm/copilot records carry zero telemetry.

    That absence is itself a problem metric. Dropping the records would
    quietly turn an unmeasurable class into a free one.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(
        repo,
        [
            ("A", "task_mcp", "2026-08-01", [("usage_record", _usage(0, provider="vscode_lm"))]),
            ("B", "task_mcp", "2026-08-01", [("usage_record", _usage(0, provider="vscode_lm"))]),
            ("C", "task_mcp", "2026-08-01", [("usage_record", _usage(500, provider="claude"))]),
        ],
    )

    absence = _build(repo)["usage"]["telemetry_absence"]

    assert absence["zero_token_records"] == 2
    assert absence["share_of_records"] == pytest.approx(2 / 3, rel=1e-3)
    rows = {row["provider"]: row for row in absence["by_adapter"]}
    assert rows["vscode_lm"]["zero_token_records"] == 2
    assert rows["vscode_lm"]["zero_token_share"] == 1.0
    # A transport with full telemetry is not listed as an absence.
    assert "claude" not in rows


def test_an_absent_risk_tier_is_unknown_not_a_tier(tmp_path: Path):
    """``risk_tier`` is only derived at create, so history carries none."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(
        repo,
        [
            ("A", "task_mcp", "2026-08-01", []),
            ("B", "task_mcp", "2026-08-01", []),
            ("C", "task_mcp", "2026-08-01", []),
        ],
        tasks_extra={"A": {"risk_tier": "high"}},
    )

    work = _build(repo)["risk_tier_distribution"]["by_population"]["work_card"]

    assert work["cards"] == 3
    assert work["risk_tier_known"] == 1
    assert work["risk_tier_unknown"] == 2
    assert work["coverage"] == pytest.approx(1 / 3, rel=1e-3)
    assert work["tiers"] == {"high": 1}
    # The two untiered cards are not defaulted into any bucket.
    assert sum(work["tiers"].values()) == 1


def test_an_unmeasured_history_is_unknown_never_an_empty_one():
    payload = history_series.history_series_unmeasured("storage_not_ready")

    assert payload["measured"] is False
    assert payload["reason"] == "storage_not_ready"
    assert payload["absent_metrics_are_unknown_not_zero"] is True
    assert payload["window"]["observed_days"] is None
    assert payload["query_cost"]["total_ms"] is None
    # No series claims to be measured.
    for key in (
        "daily_outcomes",
        "daily_decisions",
        "terminal_composition",
        "usage",
        "retry_economics",
        "model_outcomes",
        "latency",
        "risk_tier_distribution",
    ):
        assert payload[key]["measured"] is False, key


def test_an_unready_store_reports_unmeasured(tmp_path: Path):
    repo = tmp_path / "empty"
    repo.mkdir()

    payload = history_series.build_history_series(repo)

    assert payload["measured"] is False
    assert payload["reason"]


def test_a_rate_over_an_empty_denominator_is_unknown_not_zero():
    assert history_series._rate(0, 0) is None
    assert history_series._rate(5, 0) is None
    # A genuine zero numerator over a real denominator IS zero.
    assert history_series._rate(0, 4) == 0.0


def test_a_negative_duration_is_an_anomaly_not_a_fast_card(tmp_path: Path):
    """36 live cards have ``completed_at`` before ``started_at``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(repo, [("A", "task_mcp", "2026-08-01", [])])
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET completed_at='2026-08-01T00:00:30+00:00' "
            "WHERE task_id='A'"
        )
        conn.commit()
    finally:
        conn.close()

    latency = _build(repo)["latency"]

    assert latency["anomalies"]["negative_run"] == 1
    # It is excluded from the distribution rather than clamped to zero.
    assert latency["run_latency"]["by_population"]["work_card"]["measured"] is False


def test_a_latency_distribution_with_no_sample_is_unmeasured(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(repo, [("A", "task_mcp", "2026-08-01", [])])

    reviewer = _build(repo)["latency"]["queue_latency"]["by_population"][
        "reviewer_child"
    ]

    assert reviewer["measured"] is False
    assert reviewer["samples"] == 0
    assert reviewer["p50_seconds"] is None


# ---------------------------------------------------------------------------
# Retry economics.
# ---------------------------------------------------------------------------


def test_retry_share_of_tokens_counts_every_attempt_after_the_first(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(
        repo,
        [
            (
                "A",
                "task_mcp",
                "2026-08-01",
                [
                    ("usage_record", _usage(100)),
                    ("usage_record", _usage(300)),
                ],
            ),
            ("B", "task_mcp", "2026-08-01", [("usage_record", _usage(100))]),
        ],
    )

    retry = _build(repo)["retry_economics"]

    assert retry["attempts"]["first_attempt"]["records"] == 2
    assert retry["attempts"]["first_attempt"]["total_tokens"] == 200
    assert retry["attempts"]["retry"]["records"] == 1
    assert retry["attempts"]["retry"]["total_tokens"] == 300
    assert retry["retry_share"]["of_tokens"] == 0.6


def test_eventual_acceptance_is_reported_per_rejection_depth(tmp_path: Path):
    """Depth is the problematic shape: what a card cost before it landed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(
        repo,
        [
            ("FIRST_TRY", "task_mcp", "2026-08-01", [("accept_review", {})]),
            (
                "LANDED_LATE",
                "task_mcp",
                "2026-08-01",
                [("reject_review", {}), ("reject_review", {}), ("accept_review", {})],
            ),
            (
                "NEVER_LANDED",
                "task_mcp",
                "2026-08-01",
                [("reject_review", {}), ("reject_review", {})],
            ),
        ],
    )

    depth = _build(repo)["retry_economics"]["rejection_depth"]["by_population"][
        "work_card"
    ]

    assert depth["0"] == {
        "cards": 1,
        "eventually_accepted": 1,
        "eventual_acceptance_rate": 1.0,
    }
    assert depth["2"]["cards"] == 2
    assert depth["2"]["eventually_accepted"] == 1
    assert depth["2"]["eventual_acceptance_rate"] == 0.5


def test_a_model_rate_always_travels_with_its_sample_count(tmp_path: Path):
    """A 1-of-1 runner must not read as a 100% model without its n."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(repo, [("A", "task_mcp", "2026-08-01", [("accept_review", {})])])

    rows = _build(repo)["model_outcomes"]["work_card"]["runners"]

    assert rows == [
        {
            "runner": "codex_worker",
            "accepted": 1,
            "rejected": 0,
            "decided": 1,
            "sample_count": 1,
            "acceptance_rate": 1.0,
        }
    ]


# ---------------------------------------------------------------------------
# Bounded, read-only, reproducible.
# ---------------------------------------------------------------------------


def test_the_series_never_write_to_the_canonical_store(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(repo, [("A", "task_mcp", "2026-08-01", [("accept_review", {})])])
    db_path = task_store.canonical_db_path(repo)
    before = Path(db_path).read_bytes()

    _build(repo)

    assert Path(db_path).read_bytes() == before
    conn = sqlite_readonly.connect_readonly(db_path)
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        conn.close()


def test_every_series_names_the_query_that_derives_it(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(repo, [("A", "task_mcp", "2026-08-01", [("accept_review", {})])])

    series = _build(repo)

    for key in (
        "daily_outcomes",
        "daily_decisions",
        "terminal_composition",
        "usage",
        "retry_economics",
        "model_outcomes",
        "latency",
        "risk_tier_distribution",
    ):
        assert series[key]["query_ref"], key
    # The exact SQL ships with the payload so a reviewer can re-derive it.
    for ref in history_series.HISTORY_SERIES_QUERIES:
        assert ref in series["queries"]
        assert "SELECT" in series["queries"][ref]


def test_the_query_cost_is_measured_and_reported(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(repo, [("A", "task_mcp", "2026-08-01", [("accept_review", {})])])

    cost = _build(repo)["query_cost"]

    assert cost["measured"] is True
    assert cost["query_count"] == len(history_series.HISTORY_SERIES_QUERIES)
    assert set(cost["by_query_ms"]) == set(history_series.HISTORY_SERIES_QUERIES)
    assert cost["total_ms"] >= 0


def test_an_unchanged_store_is_answered_from_cache(tmp_path: Path):
    """The store is 358 MB; a refresh must not rescan it every call."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(repo, [("A", "task_mcp", "2026-08-01", [("accept_review", {})])])
    history_series._cache.clear()

    cold = history_series.build_history_series(repo)
    warm = history_series.build_history_series(repo)

    assert cold["query_cost"]["cache"] == "miss"
    assert warm["query_cost"]["cache"] == "hit"
    assert warm["daily_decisions"]["totals"] == cold["daily_decisions"]["totals"]


def test_a_changed_store_invalidates_the_cache(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(repo, [("A", "task_mcp", "2026-08-01", [("accept_review", {})])])
    history_series._cache.clear()

    first = history_series.build_history_series(repo)
    assert first["daily_decisions"]["totals"]["work_card"]["accepted"] == 1

    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, "
            "priority, objective, card_json, created_at, updated_at) "
            "VALUES ('B', 'codex_worker', 'task_mcp', 'finished', 'done', '', "
            "'', '{}', '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, "
            "created_at) VALUES ('B', 'accept_review', 'x', '{}', "
            "'2026-08-01T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    second = history_series.build_history_series(repo)

    assert second["query_cost"]["cache"] == "miss"
    assert second["daily_decisions"]["totals"]["work_card"]["accepted"] == 2


def test_the_window_reports_the_days_it_actually_observed(tmp_path: Path):
    """The whole point: days of history, not a bounded run count."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(
        repo,
        [
            ("A", "task_mcp", "2026-08-01", [("terminal_review", _terminal("review_ready"))]),
            ("B", "task_mcp", "2026-08-02", [("terminal_review", _terminal("review_ready"))]),
            ("C", "task_mcp", "2026-08-03", [("terminal_review", _terminal("review_ready"))]),
        ],
    )

    series = _build(repo)

    assert series["window"]["observed_days"] == 3
    assert series["window"]["first_day"] == "2026-08-01"
    assert series["window"]["last_day"] == "2026-08-03"
    assert series["daily_outcomes"]["day_count"] == 3
    assert [day["day"] for day in series["daily_outcomes"]["days"]] == [
        "2026-08-01",
        "2026-08-02",
        "2026-08-03",
    ]


def test_the_window_excludes_events_older_than_its_bound(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(
        repo,
        [
            ("OLD", "task_mcp", "2020-01-01", [("accept_review", {})]),
            ("NEW", "task_mcp", "2026-08-01", [("accept_review", {})]),
        ],
    )

    from datetime import datetime, timezone

    series = history_series.build_history_series(
        repo,
        window_days=30,
        now=datetime(2026, 8, 15, tzinfo=timezone.utc),
        use_cache=False,
    )

    assert series["window"]["since"] == "2026-07-16"
    assert series["daily_decisions"]["totals"]["work_card"]["accepted"] == 1
