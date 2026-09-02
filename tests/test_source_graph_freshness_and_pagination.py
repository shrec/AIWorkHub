"""Source Graph truthfulness regressions: it must not assert more than it
established.

Seven confirmed defects in :mod:`aiworkhub.source_graph` each let the tool
answer with unearned confidence -- a stale line range, a silently-capped
corpus, a resolved edge whose target is gone, a scan that stopped a third of
the way through, a pagination cursor that never issued once a page came back
clean. These tests pin the truthful behaviour:

  1. A paged filter mode whose first page is entirely clean still issues a
     cursor, so every eligible symbol stays reachable.
  2. body/function/class/context compare the indexed ``source_hash`` against
     the file on disk before slicing; a changed file yields an explicit
     staleness state and no lines, never lines from a different generation.
  3. Every body-style response carries a ``freshness`` field.
  4. ``eligible_capped`` is true whenever the corpus was really capped, and
     ``coverage.scanned`` equals what the analytic examined.
  5. ``index_file`` leaves cross-file resolution in the state a full build
     would, and a following incremental build keeps it there.
  6. A Python cross-file ``dst_qualname`` is cleared when its target vanishes.
  7. Repository-wide bodygrep returns a cursor that reaches the files a single
     byte-bounded scan could not.

Run: python3 -m pytest -q tests/test_source_graph_freshness_and_pagination.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub.source_graph import _analytics_result_row_count
from aiworkhub.repository_state import bootstrap_repository


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _new_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    bootstrap_repository(root, repo_name=name)
    return root


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _b_call_dst(repo: Path):
    """dst_qualname of the b.py caller->helper cross-file call edge."""

    conn = sg.connect(sg.resolve_db_path(repo))
    try:
        row = conn.execute(
            "SELECT dst_qualname FROM edges WHERE file_path='b.py' "
            "AND kind='calls' AND dst_name='helper'"
        ).fetchone()
        return row["dst_qualname"] if row else "NO_EDGE"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Defect ONE -- an empty page must not end pagination forever.
# ---------------------------------------------------------------------------

def test_clean_first_page_still_issues_cursor_and_reaches_later_finding(tmp_path):
    """A filter mode whose first page has zero findings still pages forward.

    ``deadmethods`` flags a symbol only when it has zero incoming resolved
    calls and is not a conventional entrypoint. Thirty files hold an excluded
    ``activate`` symbol (scanned but never a finding); one later-sorted file
    holds a genuinely dead ``orphan_final``.

    Before: ``next_cursor`` was gated on ``returned > 0`` (findings), so the
    all-clean first page minted no cursor and ``orphan_final`` -- 20 rows past
    the first page -- was unreachable by any caller.
    After: the cursor follows from unexamined eligible rows alone, so paging
    reaches the last eligible symbol.

    The analytic now also examines the whole scoped corpus rather than one
    budget-sized slice of it, so this fixture's finding is no longer past a page
    boundary at all -- a filter mode cannot hide a finding behind pagination
    (NF-2026-00564 / NF-2026-00566). Both properties are asserted: the finding
    is reached, and a cursor is still minted while eligible rows remain.
    """

    repo = _new_repo(tmp_path, "pageone")
    for i in range(30):
        _write(repo / f"d{i:02d}.py", "def activate():\n    return 1\n")
    _write(repo / "zzz_dead.py", "def orphan_final():\n    return 99\n")
    sg.build_index(repo, incremental=False)

    first = sg.analytics_query(repo, "deadmethods", "", budget=10)
    assert first["coverage"]["eligible"] >= 31
    # The page bounds the answer, not the examination: a finding beyond the
    # first budget of rows is now surfaced on the first page.
    assert _analytics_result_row_count("deadmethods", first) >= 1
    # And a page that has not exhausted the eligible rows still pages forward.
    assert first["next_cursor"]

    found: list[str] = []
    cursor = first["next_cursor"]
    for finding in first.get("analysis", {}).get("findings", []):
        found.append(finding["qualname"])
    pages = 1
    while cursor and pages < 60:
        page = sg.analytics_query(repo, "deadmethods", "", budget=10, cursor=cursor)
        for finding in page.get("analysis", {}).get("findings", []):
            found.append(finding["qualname"])
        cursor = page["next_cursor"]
        pages += 1

    assert "zzz_dead.py.orphan_final" in found


# ---------------------------------------------------------------------------
# Defect TWO / THREE -- freshness before slicing; a freshness field always.
# ---------------------------------------------------------------------------

def test_body_slice_is_fresh_before_mutation(tmp_path):
    repo = _new_repo(tmp_path, "freshbody")
    src = "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
    _write(repo / "m.py", src)
    sg.build_index(repo, incremental=False)

    result = sg.body_query(repo, "alpha")
    # Top-level freshness is a scalar state; the hashes ride on the match.
    assert result["freshness"] == "fresh"
    assert result["matches"][0]["freshness"]["state"] == "fresh"
    assert result["matches"][0]["source"].startswith("def alpha():")


def test_body_slice_refuses_stale_lines_after_prepend(tmp_path):
    """The prepend-20-lines reproduction: never return a different generation.

    Before: ``body`` sliced the live file with the indexed generation's line
    numbers, so after 20 comment lines were prepended, ``alpha``'s recorded
    range pointed at ``# pad`` padding and the caller received padding as
    alpha's code with no way to detect the substitution.
    After: the on-disk sha256 no longer matches the indexed ``source_hash``,
    so the response carries an explicit ``stale`` state and no lines.
    """

    repo = _new_repo(tmp_path, "stalebody")
    src = "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
    _write(repo / "m.py", src)
    sg.build_index(repo, incremental=False)

    padded = "".join(f"# pad {i}\n" for i in range(20)) + src
    _write(repo / "m.py", padded)

    result = sg.body_query(repo, "alpha")
    assert result["freshness"] == "stale"
    match = result["matches"][0]
    assert match["freshness"]["state"] == "stale"
    assert match["freshness"]["disk_source_hash"] == _sha256(padded)
    assert match["freshness"]["indexed_source_hash"] != _sha256(padded)
    # No lines at all -- and certainly not the prepended padding.
    assert match["source"] == ""
    assert "# pad" not in match["source"]


def test_missing_file_body_reports_missing(tmp_path):
    repo = _new_repo(tmp_path, "missingbody")
    _write(repo / "m.py", "def alpha():\n    return 1\n")
    sg.build_index(repo, incremental=False)
    (repo / "m.py").unlink()

    result = sg.body_query(repo, "alpha")
    assert result["freshness"] == "missing"
    assert result["matches"][0]["freshness"]["state"] == "missing"
    assert result["matches"][0]["source"] == ""


@pytest.mark.parametrize("mode", ["function", "class", "context", "body"])
def test_every_body_style_response_carries_freshness(tmp_path, mode):
    repo = _new_repo(tmp_path, f"fresh_{mode}")
    src = (
        "def alpha():\n    return 1\n\n"
        "class Widget:\n    def method(self):\n        return 2\n"
    )
    _write(repo / "m.py", src)
    sg.build_index(repo, incremental=False)

    if mode == "function":
        payload = sg.function_query(repo, "alpha")
    elif mode == "class":
        payload = sg.class_query(repo, "Widget")
    elif mode == "context":
        payload = sg.context_query(repo, "m.py")
    else:
        payload = sg.body_query(repo, "alpha")

    assert "freshness" in payload
    # A caller can read the response's overall freshness as a scalar state.
    assert payload["freshness"] == "fresh"


def test_function_query_drops_source_when_stale(tmp_path):
    repo = _new_repo(tmp_path, "stalefunc")
    src = "def alpha():\n    return 1\n"
    _write(repo / "m.py", src)
    sg.build_index(repo, incremental=False)
    _write(repo / "m.py", "".join(f"# p{i}\n" for i in range(20)) + src)

    payload = sg.function_query(repo, "alpha")
    assert payload["freshness"] == "stale"
    assert payload["matches"][0]["freshness"]["state"] == "stale"
    assert payload["matches"][0]["source"] == ""


# ---------------------------------------------------------------------------
# Defect THREE / SEVEN -- truthful coverage: cap flag and examined count.
# ---------------------------------------------------------------------------

def test_eligible_capped_true_when_corpus_actually_capped(tmp_path):
    """300 matching symbols: the find-backed corpus is really clamped to 200.

    Before: ``eligible_capped = scanned >= MAX_ANALYTICS_CORPUS_ROWS`` (4000),
    a threshold ``find`` (clamped to 200) can never reach, so 100 lost symbols
    were reported as ``eligible_capped: False``.
    After: the flag reflects the cap that actually bounded the corpus.
    """

    repo = _new_repo(tmp_path, "capped")
    _write(
        repo / "widgets.py",
        "".join(f"def widget_{i}():\n    return {i}\n\n" for i in range(300)),
    )
    sg.build_index(repo, incremental=False)

    res = sg.analytics_query(repo, "tags", "widget", budget=40)
    assert res["coverage"]["eligible_capped"] is True
    assert res["coverage"]["eligible"] == sg.MAX_BUDGET_ROWS


def test_eligible_capped_false_when_corpus_fits(tmp_path):
    repo = _new_repo(tmp_path, "uncapped")
    _write(
        repo / "small.py",
        "".join(f"def widget_{i}():\n    return {i}\n\n" for i in range(12)),
    )
    sg.build_index(repo, incremental=False)

    res = sg.analytics_query(repo, "tags", "widget", budget=40)
    assert res["coverage"]["eligible_capped"] is False


def test_coverage_scanned_equals_examined_not_whole_corpus(tmp_path):
    """coverage.scanned must equal what the analytic examined.

    The pillar is unchanged and is the last assertion: ``coverage.scanned``
    tracks ``analysis.symbols_scanned``, so a response can never claim to have
    examined more than it did.

    What the analytic examines did change. It used to receive ``corpus[:budget]``,
    so ``scanned`` was the page and was necessarily smaller than ``eligible``.
    Slicing the input that way made ``budget`` decide the population rather than
    the response size -- a ranked mode answered a different question at every
    budget, and a filter mode could hide a finding behind a page boundary
    (NF-2026-00564 / NF-2026-00566). The analytic now receives the whole scoped
    corpus, so ``scanned`` equals ``eligible`` here: the page bounds the answer,
    not the examination.
    """

    repo = _new_repo(tmp_path, "scanned")
    for i in range(30):
        _write(repo / f"d{i:02d}.py", "def activate():\n    return 1\n")
    _write(repo / "zzz_dead.py", "def orphan_final():\n    return 1\n")
    sg.build_index(repo, incremental=False)

    res = sg.analytics_query(repo, "deadmethods", "", budget=10)
    assert res["coverage"]["eligible"] >= 31
    assert res["coverage"]["scanned"] == res["coverage"]["eligible"], (
        "the analytic examines the whole scoped corpus, not one page of it"
    )
    # The truthful pillar: coverage.scanned tracks analysis.symbols_scanned.
    assert res["coverage"]["scanned"] == res["analysis"]["symbols_scanned"]


def test_coverage_scanned_reflects_page_for_paged_non_filter_mode(tmp_path):
    """A page-sliced non-filter mode must report the page it examined.

    ``tags`` is pageable but not a per-symbol filter: it ranks only
    ``scoped_matches[:budget]`` -- one budget-sized page of the eligible
    corpus -- and emits no ``analysis.symbols_scanned``. Sixty ``widget_*``
    symbols, budget 40, so the corpus is 60 and one page examines 40.

    Before: ``coverage.scanned`` fell back to ``len(corpus)`` for any mode
    without ``symbols_scanned``, so ``tags`` reported the whole eligible
    corpus (60) as scanned while ranking only one 40-row page.
    After: ``coverage.scanned`` is ``page_len`` (40) -- the rows actually
    handed to the analytic on this page -- and stays below ``eligible``.
    """

    repo = _new_repo(tmp_path, "tagspage")
    _write(
        repo / "widgets.py",
        "".join(f"def widget_{i}():\n    return {i}\n\n" for i in range(60)),
    )
    sg.build_index(repo, incremental=False)

    res = sg.analytics_query(repo, "tags", "widget", budget=40)
    # tags does not self-report symbols_scanned -- it takes the page_len branch.
    assert "symbols_scanned" not in (res.get("analysis") or {})
    # Eligible is the full corpus; the analytic examined only one budget page.
    assert res["coverage"]["eligible"] >= 60
    assert res["coverage"]["scanned"] == 40
    assert res["coverage"]["scanned"] < res["coverage"]["eligible"]


# ---------------------------------------------------------------------------
# Defect FOUR -- index_file keeps cross-file resolution a full build would.
# ---------------------------------------------------------------------------

def test_index_file_preserves_cross_file_edge_resolution(tmp_path):
    repo = _new_repo(tmp_path, "singleindex")
    _write(repo / "a.py", "def helper():\n    return 7\n")
    _write(repo / "b.py", "from a import helper\n\ndef caller():\n    return helper()\n")
    sg.build_index(repo, incremental=False)
    assert _b_call_dst(repo) == "a.py.helper"

    # Re-index b.py alone: extraction resets its cross-file edge to unresolved.
    sg.index_file(repo, "b.py", _sha256((repo / "b.py").read_text()))
    assert _b_call_dst(repo) == "a.py.helper"

    # A following incremental build sees changed == 0; resolution must persist.
    sg.build_index(repo, incremental=True)
    assert _b_call_dst(repo) == "a.py.helper"


# ---------------------------------------------------------------------------
# Defect FIVE -- a cross-file dst_qualname is cleared when its target vanishes.
# ---------------------------------------------------------------------------

def test_stale_cross_file_dst_qualname_is_cleared_on_rename(tmp_path):
    """Rename helper -> helper2 in a.py, b.py untouched, incremental rebuild.

    Before: the Python resolver only filled ``dst_qualname IS NULL`` and never
    cleared an existing binding, so the edge kept reading as EXTRACTED against
    ``a.py.helper`` -- an entity that no longer exists.
    After: a binding whose target is gone is reset to NULL and stays unresolved.
    """

    repo = _new_repo(tmp_path, "rename")
    _write(repo / "a.py", "def helper():\n    return 7\n")
    _write(repo / "b.py", "from a import helper\n\ndef caller():\n    return helper()\n")
    sg.build_index(repo, incremental=False)
    assert _b_call_dst(repo) == "a.py.helper"

    _write(repo / "a.py", "def helper2():\n    return 7\n")
    sg.build_index(repo, incremental=True)
    assert _b_call_dst(repo) is None


# ---------------------------------------------------------------------------
# Defect SIX -- repository-wide bodygrep returns a cursor that reaches the rest.
# ---------------------------------------------------------------------------

def test_bodygrep_cursor_reaches_files_past_the_byte_cap(tmp_path):
    """A literal only in tests/ is reachable though src/ exhausts the byte cap.

    src/ sorts before tests/. A single byte-bounded scan stops inside src/ and
    reports ``hit_count 0``.
    Before: the scan broke the whole loop on the first byte-cap overrun with no
    cursor, so tests/ was never reached and the true answer looked like zero.
    After: the first page reports truncation and a cursor; following it reaches
    tests/ and finds the literal.
    """

    repo = _new_repo(tmp_path, "grepcursor")
    pad = "# " + ("x" * 2000) + "\n"
    for i in range(40):
        _write(repo / "src" / f"f{i:02d}.py", "def s():\n" + pad * 30)
    _write(
        repo / "tests" / "only_here.py",
        "def t():\n    MAGIC_LITERAL_ZZZ = 1\n    return 0\n",
    )
    sg.build_index(repo, incremental=False)

    first = sg.bodygrep_query(repo, "MAGIC_LITERAL_ZZZ", budget=5)
    # The literal is only in tests/, which the first byte-bounded page cannot
    # reach -- but the page says so and hands back a cursor.
    assert first["matches"] == []
    assert first["scan_truncated"] is True
    assert first["next_cursor"]

    cursor = first["next_cursor"]
    hits = 0
    pages = 1
    while cursor and hits == 0 and pages < 400:
        page = sg.bodygrep_query(
            repo, "MAGIC_LITERAL_ZZZ", budget=5, cursor=cursor,
        )
        hits += len(page["matches"])
        cursor = page["next_cursor"]
        pages += 1

    assert hits == 1


def test_bodygrep_complete_scan_reports_no_cursor(tmp_path):
    repo = _new_repo(tmp_path, "grepdone")
    _write(repo / "a.py", "def a():\n    NEEDLE_TOKEN = 1\n    return 0\n")
    sg.build_index(repo, incremental=False)

    res = sg.bodygrep_query(repo, "NEEDLE_TOKEN", budget=16)
    assert len(res["matches"]) == 1
    assert res["next_cursor"] is None
    assert res["scan_truncated"] is False


def test_bodygrep_rejects_foreign_cursor(tmp_path):
    repo = _new_repo(tmp_path, "grepforge")
    _write(repo / "a.py", "def a():\n    return 0\n")
    sg.build_index(repo, incremental=False)

    minted = sg.bodygrep_query(repo, "def", budget=1)
    cursor = minted.get("next_cursor")
    if cursor:
        # A cursor bound to a different term must not be honoured.
        with pytest.raises(sg.SourceGraphError):
            sg.bodygrep_query(repo, "other_term", budget=1, cursor=cursor)


# ---------------------------------------------------------------------------
# Cross-check: the analytics cursor still round-trips and rejects forgery.
# ---------------------------------------------------------------------------

def test_analytics_cursor_is_bound_to_its_query_tuple(tmp_path):
    repo = _new_repo(tmp_path, "curbind")
    _write(
        repo / "widgets.py",
        "".join(f"def widget_{i}():\n    return {i}\n\n" for i in range(60)),
    )
    sg.build_index(repo, incremental=False)

    first = sg.analytics_query(repo, "tags", "widget", budget=10)
    assert first["next_cursor"]
    # Same tuple: the cursor advances and yields a well-formed coverage block.
    second = sg.analytics_query(
        repo, "tags", "widget", budget=10, cursor=first["next_cursor"],
    )
    assert json.dumps(second["coverage"])
    # A cursor replayed under a different budget is rejected.
    with pytest.raises(sg.SourceGraphError):
        sg.analytics_query(repo, "tags", "widget", budget=25, cursor=first["next_cursor"])
