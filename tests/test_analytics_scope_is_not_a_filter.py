"""NF-2026-00564/566: the lens answered a different question at every call.

``analytics_query`` built its corpus query-first and then filtered by target, so
an explicit scope was a filter over whatever the caller's words happened to
match rather than the population under analysis. Measured on this repository
before the fix, ``complexity`` over ``src/aiworkhub/source_graph.py``:

    query "source_graph"   -> eligible 7    top symbol 3 branches
    query "zzqqxx"         -> eligible 159  top symbol 16 branches

The nonsense query found the genuinely most complex function; the accurate one
never saw it. A caller who described the scope correctly got the narrowest
answer.

Underneath that, three further narrowings were sized by ``budget`` and all ran
BEFORE ranking -- the engine sliced ``corpus[:budget]``, ``_scope_matches`` took
``budget * 4`` rows, and ``symbol_metrics`` scored ``min(budget, 80)`` of them.
So the answer moved with the page size:

    budget 5   -> top resolve_db_path    (2 branches)
    budget 20  -> top index_write_lease  (16 branches)
    budget 60  -> top bodygrep_query     (39 branches)

These tests pin the two properties that make a ranked answer meaningful: it
depends on neither the query text nor the page size. They are written against a
built fixture index rather than this repository so they stay true as the tree
changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import source_graph as sg  # noqa: E402
from aiworkhub.repository_state import bootstrap_repository  # noqa: E402

_RANKED_MODES = ("complexity", "bottlenecks", "hotspots")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(scope="module")
def scoped_repo(tmp_path_factory) -> Path:
    """One package whose most complex symbol is NOT named after the package."""

    repo = tmp_path_factory.mktemp("scope") / "repo"
    repo.mkdir()
    bootstrap_repository(repo, repo_name="scope")

    # Thirty trivial helpers whose names DO match the obvious query, so a
    # query-first corpus finds these and stops.
    body = "\n\n".join(
        f"def widget_helper_{i:02d}(value):\n    return value + {i}" for i in range(30)
    )
    # One genuinely complex function whose name matches nothing.
    tangled = (
        "def zzz_tangled(alpha, beta, items):\n"
        "    total = 0\n"
        + "".join(
            f"    if alpha > {i}:\n"
            f"        for item in items:\n"
            f"            if beta < {i}:\n"
            f"                total += item\n"
            for i in range(12)
        )
        + "    return total\n"
    )
    _write(repo / "pkg" / "widget.py", body + "\n\n\n" + tangled + "\n")
    sg.build_index(repo, incremental=False)
    return repo


def _top(result: dict) -> dict:
    rows = result.get("ranked_symbols") or []
    return rows[0] if rows else {}


@pytest.mark.parametrize("mode", _RANKED_MODES)
def test_the_scope_decides_membership_not_the_query(scoped_repo, mode):
    """Every query over one scope must see the same population."""
    target = "pkg/widget.py"
    eligible = {
        query: (
            sg.analytics_query(scoped_repo, mode, query, budget=10, target=target)
            ["coverage"]["eligible"]
        )
        for query in ("widget", "widget helper", "zzqqxx", "")
    }
    assert len(set(eligible.values())) == 1, (
        f"{mode}: scope membership moved with the query text: {eligible}"
    )


def test_an_accurate_query_does_not_hide_the_complex_symbol(scoped_repo):
    """The defect in one line: the matching query used to miss the real answer."""
    target = "pkg/widget.py"
    for query in ("widget", "widget helper", "zzqqxx"):
        result = sg.analytics_query(
            scoped_repo, "complexity", query, budget=10, target=target
        )
        assert _top(result).get("name") == "zzz_tangled", (
            f"query {query!r} ranked {_top(result).get('name')!r} first"
        )


@pytest.mark.parametrize("mode", _RANKED_MODES)
def test_the_ranking_does_not_move_with_the_page_size(scoped_repo, mode):
    """'The most complex symbol here' cannot depend on how many rows I asked for."""
    target = "pkg/widget.py"
    # budget=1 is deliberately not sampled: it returns zero rows on this engine,
    # both before and after this change, so it is a separate pre-existing defect
    # rather than a page-size dependence. Pinning it here would lock that in.
    tops = {
        budget: _top(
            sg.analytics_query(scoped_repo, mode, "zzqqxx", budget=budget, target=target)
        ).get("name")
        for budget in (2, 5, 20, 31)
    }
    assert len(set(tops.values())) == 1, f"{mode}: top symbol moved with budget: {tops}"


def test_paging_walks_one_ranking_without_gaps_or_repeats(scoped_repo):
    """Page two must be the next rows of the same ranking, not a new ranking."""
    target = "pkg/widget.py"
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        page = sg.analytics_query(
            scoped_repo, "complexity", "zzqqxx", budget=5, target=target, cursor=cursor
        )
        seen.extend(row["name"] for row in page.get("ranked_symbols") or [])
        cursor = page.get("next_cursor")
        if not cursor:
            break

    assert seen, "paging returned nothing"
    assert len(seen) == len(set(seen)), "paging repeated a row"

    single = [
        row["name"]
        for row in sg.analytics_query(
            scoped_repo, "complexity", "zzqqxx", budget=len(seen), target=target
        ).get("ranked_symbols")
        or []
    ]
    assert seen == single, "paged order diverged from one unpaged ranking"


def test_an_explicit_empty_scope_is_never_widened(scoped_repo):
    """The unscoped no-match fallback must not leak into a scoped call."""
    result = sg.analytics_query(
        scoped_repo, "complexity", "zzqqxx", budget=10, target="pkg/absent.py"
    )
    assert result["coverage"]["eligible"] == 0
    assert not (result.get("ranked_symbols") or [])
