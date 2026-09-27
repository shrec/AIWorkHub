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
