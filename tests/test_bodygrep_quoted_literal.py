"""NF-2026-01051: a quoted exact phrase matched its own quote characters.

Seats naturally wrap an exact phrase in quotes when they ask Source Graph to
find it verbatim. ``bodygrep_query`` searched those quote characters
literally, so a caller-supplied ``"without a landed successor"`` reported
zero hits even though the unquoted phrase was present in the indexed source,
pushing seats toward a second query or a raw-grep fallback.

Fixed at the one shared entry: when the whole (whitespace-stripped) term is
wrapped in one matching pair of double or single quotes and the inner text is
non-empty, the inner text is searched as the exact literal. The response
still echoes the caller's original ``query`` and adds ``query_unquoted`` /
``query_literal`` so a caller can tell a phrase was unwrapped. Everything
else -- an inner quote, a lone leading/trailing quote, an empty pair, and
stripping only ONE outer layer -- is pinned unchanged.
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

_LINE = 'x = f"is one of {s} without a landed successor."'
_PHRASE = "without a landed successor"
_RECORD_LINE = 'y = record["derived_state"]'


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(scope="module")
def repo_with_quoted_phrase(tmp_path_factory) -> Path:
    """A tmp repo indexed in-test, never the live index."""

    repo = tmp_path_factory.mktemp("bodygrep_quoted") / "repo"
    repo.mkdir()
    bootstrap_repository(repo, repo_name="bodygrep_quoted")
    _write(repo / "src" / "sample.py", f"{_LINE}\n{_RECORD_LINE}\n")
    sg.build_index(repo, incremental=False)
    return repo


def test_double_quoted_phrase_matches_the_unquoted_literal(repo_with_quoted_phrase):
    result = sg.bodygrep_query(repo_with_quoted_phrase, f'"{_PHRASE}"', budget=20)
    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["kind"] == "body_match"
    assert match["signature"] == _LINE
    assert "match_kind" not in match
    assert "match_kind" not in result


def test_single_quoted_phrase_matches_the_unquoted_literal(repo_with_quoted_phrase):
    result = sg.bodygrep_query(repo_with_quoted_phrase, f"'{_PHRASE}'", budget=20)
    assert len(result["matches"]) == 1
    assert result["matches"][0]["signature"] == _LINE


def test_double_quoted_result_keeps_original_query_and_reports_unquoting(
    repo_with_quoted_phrase,
):
    original = f'"{_PHRASE}"'
    result = sg.bodygrep_query(repo_with_quoted_phrase, original, budget=20)
    assert result["query"] == original
    assert result["query_unquoted"] is True
    assert result["query_literal"] == _PHRASE


def test_single_quoted_result_keeps_original_query_and_reports_unquoting(
    repo_with_quoted_phrase,
):
    original = f"'{_PHRASE}'"
    result = sg.bodygrep_query(repo_with_quoted_phrase, original, budget=20)
    assert result["query"] == original
    assert result["query_unquoted"] is True
    assert result["query_literal"] == _PHRASE


def test_whitespace_padded_quoted_phrase_is_unquoted(repo_with_quoted_phrase):
    # bodygrep_query strips surrounding whitespace before unquoting, so quotes
    # padded by whitespace still wrap the whole term.
    result = sg.bodygrep_query(repo_with_quoted_phrase, f'  "{_PHRASE}"  ', budget=20)
    assert result["query_unquoted"] is True
    assert result["query_literal"] == _PHRASE
    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["kind"] == "body_match"
    assert match["signature"] == _LINE
    assert "match_kind" not in match
    assert "match_kind" not in result


def test_inner_quote_is_left_unchanged(repo_with_quoted_phrase):
    """A quote embedded inside the term, not wrapping it, is not unquoting."""
    term = 'record["derived_state"]'
    result = sg.bodygrep_query(repo_with_quoted_phrase, term, budget=20)
    assert "query_unquoted" not in result
    assert result["query"] == term
    assert len(result["matches"]) == 1
    assert result["matches"][0]["signature"] == _RECORD_LINE


def test_lone_leading_quote_is_left_unchanged(repo_with_quoted_phrase):
    term = f'"{_PHRASE}'
    result = sg.bodygrep_query(repo_with_quoted_phrase, term, budget=20)
    assert "query_unquoted" not in result
    assert result["query"] == term


def test_lone_trailing_quote_is_left_unchanged(repo_with_quoted_phrase):
    term = f'{_PHRASE}"'
    result = sg.bodygrep_query(repo_with_quoted_phrase, term, budget=20)
    assert "query_unquoted" not in result
    assert result["query"] == term


def test_empty_quote_pair_is_left_unchanged(repo_with_quoted_phrase):
    result = sg.bodygrep_query(repo_with_quoted_phrase, '""', budget=20)
    assert "query_unquoted" not in result
    assert result["query"] == '""'


def test_only_one_outer_quote_pair_is_stripped(repo_with_quoted_phrase):
    """Inner text that itself starts/ends with a quote is not peeled again."""
    inner = f"'{_PHRASE}'"
    original = f'"{inner}"'
    result = sg.bodygrep_query(repo_with_quoted_phrase, original, budget=20)
    assert result["query"] == original
    assert result["query_unquoted"] is True
    assert result["query_literal"] == inner


def test_unquoted_term_is_unaffected(repo_with_quoted_phrase):
    result = sg.bodygrep_query(repo_with_quoted_phrase, _PHRASE, budget=20)
    assert "query_unquoted" not in result
    assert len(result["matches"]) == 1
    assert result["matches"][0]["signature"] == _LINE


def test_token_fallback_uses_the_unquoted_phrases_tokens(repo_with_quoted_phrase):
    result = sg.bodygrep_query(
        repo_with_quoted_phrase,
        f'"{_PHRASE}"',
        budget=20,
        match_kind="token_and_line",
    )
    assert result["query_tokens"] == ["without", "a", "landed", "successor"]


# NF-2026-01563: with zero direct hits the token_and_file fallback returned
# every line of a file holding ANY query token, so a phrase of common words
# ("was rejected by the manager", "def get_archived") came back as kilobytes of
# lines matching only "was" / "by" / "the" / "def" (a substring test, so "def"
# also lands inside DEFAULT). A line now scores by the distinct non-stopword
# query tokens it holds and needs min(2, distinctive) of them; when none does
# the page is empty and says why instead of being padded to the budget.

_NOISE_FILE = "src/noise/changelog.py"
_BUILTINS_FILE = "src/noise/builtins.py"
_COMMON_PHRASE = "was rejected by the manager"
_RANK_PHRASE = "archived card lookup"
_NOISE_LINES = (
    "# the cache was warmed by the loader",
    "# the lease was renewed by the scheduler",
    "# a request was dropped by the router",
    "# the manager approved the change",
    "# a draft was rejected earlier",
)


@pytest.fixture(scope="module")
def repo_with_common_words(tmp_path_factory) -> Path:
    """A tmp repo indexed in-test; each file is a different fallback shape."""

    repo = tmp_path_factory.mktemp("bodygrep_common_words") / "repo"
    repo.mkdir()
    bootstrap_repository(repo, repo_name="bodygrep_common_words")
    _write(repo / _NOISE_FILE, "X = 1\n" + "".join(f"{line}\n" for line in _NOISE_LINES))
    _write(
        repo / _BUILTINS_FILE,
        "DEFAULT_LIMIT = 10\n"
        "def get_value(key):\n"
        "    return key\n"
        "# archived rows are skipped here\n",
    )
    # Both files hold every rank-phrase token; only b_together has them on one line.
    _write(
        repo / "src/rank/a_partial.py",
        "# archived cards live elsewhere\n# lookup helper\n# card lookup\nX = 1\n",
    )
    _write(repo / "src/rank/b_together.py", "X = 1\n# lookup of the archived card by id\n")
    # Sixteen two-token lines fill the budget before the one three-token line.
    _write(
        repo / "src/crowd/crowded.py",
        "".join(f"# archived card {n}\n" for n in range(1, 17)) + "# lookup archived card\n",
    )
    sg.build_index(repo, incremental=False)
    return repo


def test_common_word_phrase_yields_no_fallback_lines(repo_with_common_words):
    repo = repo_with_common_words
    # Premise: no line holds the phrase, nor even all five of its tokens, so
    # only the file-level fallback is left to answer it.
    assert sg.bodygrep_query(
        repo, _COMMON_PHRASE, budget=8, target=_NOISE_FILE,
    )["matches"] == []
    assert sg.bodygrep_query(
        repo, _COMMON_PHRASE, budget=8, target=_NOISE_FILE,
        match_kind="token_and_line",
    )["matches"] == []

    result = sg.bodygrep_query(
        repo, _COMMON_PHRASE, budget=8, target=_NOISE_FILE,
        match_kind="token_and_file",
    )

    # The file holds every token, but only "rejected" and "manager" are
    # distinctive and no single line holds both, so no line is padded in.
    assert result["match_kind"] == "token_and_file"
    assert result["matches"] == []
    assert result["candidate_files"] == []
    assert result["next_cursor"] is None


def test_empty_fallback_page_explains_itself_in_one_line(repo_with_common_words):
    result = sg.bodygrep_query(
        repo_with_common_words, _COMMON_PHRASE, budget=8,
        target=_NOISE_FILE, match_kind="token_and_file",
    )
    assert "\n" not in result["hint"]
    assert "2+ distinctive query tokens" in result["hint"]
    assert "ignored as common: was, by, the" in result["hint"]


def test_query_of_only_common_words_matches_nothing(repo_with_common_words):
    result = sg.bodygrep_query(
        repo_with_common_words, "was by the", budget=8,
        match_kind="token_and_file", target=_NOISE_FILE,
    )
    assert result["matches"] == []
    assert "ignored as common: was, by, the" in result["hint"]


def test_single_distinctive_token_still_scores_its_line(repo_with_common_words):
    # "def" (also inside DEFAULT) and "get" are common, so only the line holding
    # "archived" qualifies: min(2, one distinctive token) is one.
    result = sg.bodygrep_query(
        repo_with_common_words, "def get_archived", budget=8,
        match_kind="token_and_file", target=_BUILTINS_FILE,
    )
    assert [(row["line_start"], row["token_hits"]) for row in result["matches"]] == [(4, 1)]
    assert "hint" not in result


def test_co_occurring_tokens_return_that_line_first(repo_with_common_words):
    result = sg.bodygrep_query(
        repo_with_common_words, _RANK_PHRASE, budget=8,
        match_kind="token_and_file", target="src/rank",
    )
    # a_partial.py sorts first but only pairs the tokens; the single-token line
    # "# lookup helper" is dropped, and the line holding all three leads the page.
    assert [
        (row["file_path"], row["line_start"], row["token_hits"])
        for row in result["matches"]
    ] == [
        ("src/rank/b_together.py", 2, 3),
        ("src/rank/a_partial.py", 1, 2),
        ("src/rank/a_partial.py", 3, 2),
    ]
    assert {row["match_kind"] for row in result["matches"]} == {"token_and_file"}
    assert "hint" not in result


def test_best_line_survives_a_crowded_file_within_budget(repo_with_common_words):
    result = sg.bodygrep_query(
        repo_with_common_words, _RANK_PHRASE, budget=8,
        match_kind="token_and_file", target="src/crowd/crowded.py",
    )
    # Eight two-token lines arrive before the three-token line on line 17; it
    # must still lead the page, evicting the latest of the equal-score lines.
    assert [(row["line_start"], row["token_hits"]) for row in result["matches"]] == (
        [(17, 3)] + [(line, 2) for line in range(1, 8)]
    )


def test_file_missing_a_token_is_not_reported_as_common_words(repo_with_common_words):
    result = sg.bodygrep_query(
        repo_with_common_words, "archived zebra", budget=8,
        match_kind="token_and_file", target="src/rank",
    )
    assert result["matches"] == []
    assert "hint" not in result


def test_literal_and_token_and_line_hits_ignore_the_common_word_rule(
    repo_with_common_words,
):
    literal = sg.bodygrep_query(
        repo_with_common_words, "the manager approved", budget=8, target=_NOISE_FILE,
    )
    assert [row["line_start"] for row in literal["matches"]] == [5]
    assert "match_kind" not in literal["matches"][0]
    assert "token_hits" not in literal["matches"][0]

    by_line = sg.bodygrep_query(
        repo_with_common_words, "was by the", budget=8,
        match_kind="token_and_line", target=_NOISE_FILE,
    )
    assert [row["line_start"] for row in by_line["matches"]] == [2, 3, 4]
    assert all("token_hits" not in row for row in by_line["matches"])
