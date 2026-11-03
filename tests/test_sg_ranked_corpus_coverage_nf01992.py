"""NF-2026-01992: ranked analytic modes see the whole repository.

Before: the repository corpus for an unscoped analytic was
``ORDER BY kind, file_path, line_start LIMIT MAX_ANALYTICS_CORPUS_ROWS`` and
the per-mode ranking then read only its first rows, so on a repository with
more function rows than the cap, every symbol in a late-sorting file was never
ranked -- the hottest symbol of the repo could live in ``w_*.py`` and never
appear. ``coverage.eligible`` also reported ``len(corpus)`` rather than the
real in-scope population.

After: ranked modes (bottlenecks/complexity/hotspots) select the capped
corpus by a SQL-side score computed over ALL eligible rows, ``eligible`` is
the full count, and ``eligible_capped`` is true only when rows were dropped.

Churn with a free-text query whose matched files carry no history rows now
returns an explicit machine-readable ``history.reason``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub.repository_state import bootstrap_repository


FILLER_PER_FILE = 200
HOT_CALLERS = 12


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _complex_function() -> str:
    body = "".join(
        f"    if x == {i}:\n        x += {i}\n" for i in range(30)
    )
    return f"def most_complex(x):\n{body}    return x\n"


@pytest.fixture(scope="module")
def big_repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("rankedcorpus") / "repo"
    root.mkdir()
    bootstrap_repository(root, repo_name="rankedcorpus")
    filler_total = sg.MAX_ANALYTICS_CORPUS_ROWS + 200
    files = -(-filler_total // FILLER_PER_FILE)
    for f in range(files):
        _write(
            root / f"a_{f:03d}.py",
            "".join(
                f"def filler_{f}_{i}():\n    return {i}\n\n"
                for i in range(FILLER_PER_FILE)
            ),
        )
    callers = "".join(
        f"def caller_{i}():\n    return hot_target()\n\n" for i in range(HOT_CALLERS)
    )
    _write(root / "w_hot.py", "def hot_target():\n    return 1\n\n" + callers)
    _write(root / "w_complex.py", _complex_function())
    sg.build_index(root, incremental=False)
    return root


def _eligible_rows(repo: Path) -> int:
    conn = sg.connect(sg.resolve_db_path(repo), read_only=True)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM entities WHERE kind NOT IN ('file', 'module')"
        ).fetchone()[0])
    finally:
        conn.close()


def test_fixture_exceeds_the_corpus_cap(big_repo):
    assert _eligible_rows(big_repo) > sg.MAX_ANALYTICS_CORPUS_ROWS


def test_bottlenecks_ranks_late_sorting_hottest_symbol_first(big_repo):
    res = sg.analytics_query(big_repo, "bottlenecks", "", budget=10)
    ranked = res["ranked_symbols"]
    assert ranked, res
    assert ranked[0]["qualname"] == "w_hot.py.hot_target"
    assert int(ranked[0]["incoming_calls"]) == HOT_CALLERS


def test_complexity_ranks_late_sorting_most_complex_symbol_first(big_repo):
    res = sg.analytics_query(big_repo, "complexity", "", budget=10)
    ranked = res["ranked_symbols"]
    assert ranked, res
    assert ranked[0]["qualname"] == "w_complex.py.most_complex"
    assert int(ranked[0]["branch_count"]) >= 30


def test_hotspots_sees_late_sorting_symbols(big_repo):
    res = sg.analytics_query(big_repo, "hotspots", "", budget=10)
    names = [row["qualname"] for row in res["ranked_symbols"]]
    assert "w_hot.py.hot_target" in names or "w_complex.py.most_complex" in names


def test_coverage_eligible_is_true_count_and_capped_only_when_dropped(big_repo):
    res = sg.analytics_query(big_repo, "bottlenecks", "", budget=10)
    coverage = res["coverage"]
    assert coverage["eligible"] == _eligible_rows(big_repo)
    assert coverage["eligible"] > sg.MAX_ANALYTICS_CORPUS_ROWS
    assert coverage["eligible_capped"] is True


def test_eligible_capped_false_when_scope_fits(big_repo):
    res = sg.analytics_query(big_repo, "bottlenecks", "", budget=10, target="w_hot.py")
    coverage = res["coverage"]
    assert coverage["eligible"] == HOT_CALLERS + 1
    assert coverage["eligible_capped"] is False
    assert res["ranked_symbols"][0]["qualname"] == "w_hot.py.hot_target"


def test_paging_a_ranked_mode_yields_one_consistent_ranking(big_repo):
    whole = sg.analytics_query(big_repo, "bottlenecks", "", budget=15)
    expected = [row["qualname"] for row in whole["ranked_symbols"]]
    assert len(expected) == 15

    paged: list[str] = []
    cursor = None
    for _ in range(3):
        page = sg.analytics_query(big_repo, "bottlenecks", "", budget=5, cursor=cursor)
        paged.extend(row["qualname"] for row in page["ranked_symbols"])
        cursor = page["next_cursor"]
        assert cursor
    assert paged == expected


def _small_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    bootstrap_repository(root, repo_name=name)
    _write(
        root / "widgets.py",
        "".join(f"def widget_{i}():\n    return {i}\n\n" for i in range(5)),
    )
    _write(root / "other.py", "def unrelated():\n    return 0\n")
    sg.build_index(root, incremental=False)
    return root


def test_churn_free_text_without_history_explains_the_empty_list(tmp_path):
    repo = _small_repo(tmp_path, "churnempty")
    res = sg.analytics_query(repo, "churn", "widget", budget=10)
    history = res["history"]
    assert res["files"] == []
    assert isinstance(history["reason"], str) and history["reason"]
    if history["available"]:
        assert history["reason"] == "query_matched_no_history_files"
        assert history["matched_files"] >= 1


def test_churn_with_history_still_ranks(tmp_path):
    repo = _small_repo(tmp_path, "churnrows")
    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO file_history (file_path, commit_touches_90d, "
            "lines_added_90d, lines_deleted_90d, authors_90d, primary_author_90d, "
            "evidence) VALUES ('widgets.py', 3, 40, 2, 1, 'dev', 'test')"
        )
        conn.commit()
    finally:
        conn.close()
    res = sg.analytics_query(repo, "churn", "widget", budget=10)
    assert [row["file_path"] for row in res["files"]] == ["widgets.py"]
    assert res["files"][0]["churn_90d"] == 42
    assert res["history"]["reason"] is None
