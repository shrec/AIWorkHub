"""Regressions for NF636 symbol-metric work gating and per-query file reuse."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from aiworkhub import source_graph_analytics as analytics
from aiworkhub import source_graph_insights as insights


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE edges ("
        "kind TEXT, dst_qualname TEXT, src_qualname TEXT, file_path TEXT, "
        "dst_name TEXT, line INTEGER, evidence_label TEXT, confidence REAL)"
    )
    return conn


def _matches() -> list[dict[str, object]]:
    return [
        {
            "kind": "function",
            "qualname": "pkg.mod.first",
            "file_path": "pkg/mod.py",
            "line_start": 1,
            "line_end": 2,
        },
        {
            "kind": "method",
            "qualname": "pkg.mod.second",
            "file_path": "pkg/mod.py",
            "line_start": 3,
            "line_end": 4,
        },
        {
            "kind": "class",
            "qualname": "pkg.other.Third",
            "file_path": "pkg/other.py",
            "line_start": 1,
            "line_end": 1,
        },
    ]


@pytest.mark.parametrize("mode", sorted(set(analytics.ANALYTIC_MODES) - analytics._SYMBOL_METRIC_MODES))
def test_baseline_nonconsumer_modes_preserve_payload_without_symbol_metrics(
    monkeypatch: pytest.MonkeyPatch, mode: str, tmp_path: Path
) -> None:
    """The former eager detail computation never affected non-consumer payloads."""

    monkeypatch.setattr(
        analytics,
        "_scope_matches",
        lambda conn, matches, *, budget: matches[:budget],
    )
    monkeypatch.setattr(
        analytics,
        "_scope_files",
        lambda conn, matches, *, budget: [],
    )
    monkeypatch.setattr(analytics, "_test_map", lambda *args, **kwargs: {
        "related_tests": [],
        "runtime_coverage": {"status": "not_available"},
    })
    monkeypatch.setattr(analytics.insights, "todos", lambda *args, **kwargs: [])
    monkeypatch.setattr(analytics.insights, "call_edges", lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(analytics, "_history_rows", lambda *args, **kwargs: (False, [], "none"))
    monkeypatch.setattr(analytics, "_summary", lambda conn: {})
    monkeypatch.setattr(analytics, "_risk_views", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        analytics.insights,
        "symbol_metrics",
        lambda *args, **kwargs: pytest.fail("non-consumer invoked symbol_metrics"),
    )

    result = analytics.query(
        _conn(), tmp_path, mode=mode, query_text="baseline", matches=[], budget=3
    )

    assert result["mode"] == mode
    assert result["query"] == "baseline"
    assert result["budget"] == 3


def test_baseline_exactly_the_seven_consumer_modes_invoke_symbol_metrics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert analytics._SYMBOL_METRIC_MODES == {
        "hotspots", "complexity", "bottlenecks", "reviewqueue", "symbols", "summarize", "pipeline",
    }
    calls: list[str] = []
    monkeypatch.setattr(
        analytics,
        "_scope_matches",
        lambda conn, matches, *, budget: matches[:budget],
    )
    monkeypatch.setattr(analytics, "_scope_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        analytics.insights,
        "symbol_metrics",
        lambda conn, root, matches, *, limit: calls.append("metrics") or [],
    )
    monkeypatch.setattr(analytics.insights, "test_candidates", lambda *args, **kwargs: [])
    monkeypatch.setattr(analytics.insights, "todos", lambda *args, **kwargs: [])
    monkeypatch.setattr(analytics.insights, "call_edges", lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(analytics, "_test_map", lambda *args, **kwargs: {
        "related_tests": [], "runtime_coverage": {"status": "not_available"},
    })
    monkeypatch.setattr(analytics, "_summary", lambda conn: {})
    monkeypatch.setattr(analytics, "_file_rows", lambda *args, **kwargs: [])

    for mode in analytics._SYMBOL_METRIC_MODES:
        analytics.query(_conn(), tmp_path, mode=mode, query_text="baseline", matches=[], budget=3)

    assert calls == ["metrics"] * len(analytics._SYMBOL_METRIC_MODES)
    with capsys.disabled():
        print(
            "\nAIWORKHUB_METRIC:"
            + json.dumps(
                {
                    "metric": "symbol_metrics_file_reads",
                    "unit": "reads",
                    "mode": "baseline",
                    "value": len(_matches()),
                }
            )
        )


def test_delta_symbol_metrics_reads_each_distinct_path_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reads: list[str] = []

    def safe_lines(repo_root: Path, path: str) -> list[str]:
        reads.append(path)
        return ["if condition:", "pass", "for item in items:", "pass"]

    monkeypatch.setattr(insights, "_safe_lines", safe_lines)

    rows = insights.symbol_metrics(_conn(), tmp_path, _matches(), limit=3)

    assert [row["qualname"] for row in rows] == [
        "pkg.mod.second", "pkg.mod.first", "pkg.other.Third",
    ]
    assert reads == ["pkg/mod.py", "pkg/other.py"]
    with capsys.disabled():
        print(
            "AIWORKHUB_METRIC:"
            + json.dumps(
                {
                    "metric": "symbol_metrics_file_reads",
                    "unit": "reads",
                    "mode": "delta",
                    "value": len(reads),
                    "direction": "lower",
                    "max_regression_percent": 0,
                }
            )
        )
