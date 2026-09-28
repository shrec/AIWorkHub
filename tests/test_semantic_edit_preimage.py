"""NF-2026-00351: the range layer's preimage gate and terminator handling.

Two guarantees the ~60x semantic-edit saving rests on are pinned here by
driving ``apply_line_ranges`` directly -- no file IO, no wrapper -- so the
behaviour is asserted at the exact layer that owns it:

* the range-fragment preimage check is never silently skipped.  A supplied hash
  is verified and a mismatch raises; a missing hash (``None``, ``""`` or an
  absent key) still applies the edit but is reported as unverified in the
  returned accounting, so a caller can never mistake an unchecked write for a
  checked one;
* lines are numbered on ``\\n`` only (NF-2026-01077), so the trailing terminator
  is CRLF, LF or nothing: LF and CRLF round-trip through a range edit, while a
  bare ``\\r``, ``\\f``, U+0085, U+2028 and the other characters ``str.splitlines``
  breaks on are line content -- they neither split a line nor end one.

The existing refusals (wrong hash, overlapping ranges, size caps) are exercised
here too and, canonically, by ``tests/test_semantic_edit_protocol.py``.
"""

from __future__ import annotations

import hashlib

import pytest

from aiworkhub import semantic_edit


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- U2: the staleness gate is not opt-in ---------------------------------

def test_correct_fragment_hash_applies_and_is_reported_verified() -> None:
    new_text, metrics = semantic_edit.apply_line_ranges(
        "a\nb\nc\n",
        [{"start_line": 2, "end_line": 2, "new": "B", "fragment_sha256": _sha("b\n")}],
    )
    assert new_text == "a\nB\nc\n"
    assert metrics["preimage_verified"] is True
    assert metrics["preimage_unverified_range_count"] == 0
    assert metrics["preimage_verified_range_count"] == 1


def test_wrong_fragment_hash_is_refused() -> None:
    with pytest.raises(
        semantic_edit.SemanticEditError,
        match="semantic_edit_fragment_hash_mismatch:0",
    ):
        semantic_edit.apply_line_ranges(
            "a\nb\nc\n",
            [{"start_line": 2, "end_line": 2, "new": "B", "fragment_sha256": _sha("WRONG")}],
        )


@pytest.mark.parametrize(
    "item",
    [
        pytest.param({"start_line": 2, "end_line": 2, "new": "B", "fragment_sha256": None}, id="none"),
        pytest.param({"start_line": 2, "end_line": 2, "new": "B", "fragment_sha256": ""}, id="empty"),
        pytest.param({"start_line": 2, "end_line": 2, "new": "B"}, id="absent"),
    ],
)
def test_missing_fragment_hash_applies_but_is_reported_unverified(item: dict) -> None:
    # The edit is not refused (a caller may legitimately omit the hash), but the
    # accounting states plainly that the preimage went unchecked -- the one
    # outcome the objective forbids is a silent, unlabelled apply.
    new_text, metrics = semantic_edit.apply_line_ranges("a\nb\nc\n", [item])
    assert new_text == "a\nB\nc\n"
    assert metrics["preimage_verified"] is False
    assert metrics["preimage_unverified_range_count"] == 1
    assert metrics["preimage_verified_range_count"] == 0


def test_mixed_ranges_report_only_the_unverified_one() -> None:
    new_text, metrics = semantic_edit.apply_line_ranges(
        "a\nb\nc\n",
        [
            {"start_line": 1, "end_line": 1, "new": "A", "fragment_sha256": _sha("a\n")},
            {"start_line": 3, "end_line": 3, "new": "C"},
        ],
    )
    assert new_text == "A\nb\nC\n"
    assert metrics["preimage_verified"] is False
    assert metrics["preimage_verified_range_count"] == 1
    assert metrics["preimage_unverified_range_count"] == 1


# --- U1: the terminator is CRLF, LF or nothing (NF-2026-01077) --------------
# Replaces ``test_bare_cr_file_keeps_its_line_structure`` and
# ``test_every_splitlines_terminator_round_trips``: they pinned ``str.splitlines``
# numbering, where a bare CR, \v, \f, \x1c-\x1e, U+0085, U+2028 or U+2029 ended a
# line.  That numbering is the defect -- ranges from Source Graph, git and editors
# count ``\n`` only -- so those characters are now line content, pinned below.

@pytest.mark.parametrize("term", ["\r\n", "\n"], ids=repr)
def test_lf_and_crlf_terminators_round_trip_through_a_range_edit(term: str) -> None:
    new_text, _metrics = semantic_edit.apply_line_ranges(
        f"a{term}b{term}",
        [{"start_line": 1, "end_line": 1, "new": "X", "fragment_sha256": _sha(f"a{term}")}],
    )
    assert new_text == f"X{term}b{term}"


# Every character ``str.splitlines`` breaks on besides ``\n``.  Lines are numbered
# on ``\n`` only, as Source Graph, git and editors number them, so these are line
# content: they neither split a line nor end one.
_OTHER_LINE_BREAKS = [
    "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85",
    "\N{LINE SEPARATOR}", "\N{PARAGRAPH SEPARATOR}",
]


@pytest.mark.parametrize("char", _OTHER_LINE_BREAKS, ids=repr)
def test_another_line_break_inside_a_line_does_not_split_it(char: str) -> None:
    text = f"a{char}b\nc\n"
    # Sanity: splitlines would have cut line 1 right after the character.
    assert text.splitlines(keepends=True)[0] == f"a{char}"

    new_text, metrics = semantic_edit.apply_line_ranges(
        text,
        [{"start_line": 1, "end_line": 1, "new": "X", "fragment_sha256": _sha(f"a{char}b\n")}],
    )
    # No stray ``b\n`` is left behind, and the replacement is completed with the
    # LF that ended the line, not with the character.
    assert new_text == "X\nc\n"
    assert metrics["preimage_verified"] is True


@pytest.mark.parametrize("char", _OTHER_LINE_BREAKS, ids=repr)
def test_another_line_break_at_the_end_of_a_line_is_no_terminator(char: str) -> None:
    # ``b{char}`` closes the text but no line, so there is no terminator to
    # restore; ``splitlines`` would have appended the character to the replacement.
    new_text, _metrics = semantic_edit.apply_line_ranges(
        f"a\nb{char}",
        [{"start_line": 2, "end_line": 2, "new": "X", "fragment_sha256": _sha(f"b{char}")}],
    )
    assert new_text == "a\nX"


@pytest.mark.parametrize("char", _OTHER_LINE_BREAKS, ids=repr)
def test_a_text_with_only_other_line_breaks_is_a_single_line(char: str) -> None:
    text = f"a{char}b{char}"

    new_text, _metrics = semantic_edit.apply_line_ranges(
        text,
        [{"start_line": 1, "end_line": 1, "new": "X", "fragment_sha256": _sha(text)}],
    )
    assert new_text == "X"
    with pytest.raises(semantic_edit.SemanticEditError, match="out_of_bounds:2:2:1"):
        semantic_edit.apply_line_ranges(text, [{"start_line": 2, "end_line": 2, "new": "X"}])


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        ("", ""),
        ("a", ""),
        ("a\n", "\n"),
        ("a\r\n", "\r\n"),
        ("\n", "\n"),
        ("\r\n", "\r\n"),
        ("a\nb\n", "\n"),
        ("a\r\nb\r\n", "\r\n"),
        ("a\r\nb", ""),
        ("a\n\r", ""),
        *[(f"a{char}", "") for char in _OTHER_LINE_BREAKS],
        *[(f"a{char}\n", "\n") for char in _OTHER_LINE_BREAKS if char != "\r"],
        *[(f"a{char}\r\n", "\r\n") for char in _OTHER_LINE_BREAKS],
    ],
    ids=repr,
)
def test_line_terminator_is_crlf_lf_or_nothing(fragment: str, expected: str) -> None:
    assert semantic_edit._line_terminator(fragment) == expected


def test_a_replacement_ending_in_a_bare_cr_is_completed_with_the_line_terminator() -> None:
    # ``B\r`` ends no line, so it is completed like any unterminated replacement:
    # over an LF line it becomes ``B\r\n`` instead of gluing line 3 onto it.
    new_text, _metrics = semantic_edit.apply_line_ranges(
        "a\nb\nc\n",
        [{"start_line": 2, "end_line": 2, "new": "B\r", "fragment_sha256": _sha("b\n")}],
    )
    assert new_text == "a\nB\r\nc\n"


def test_last_line_without_terminator_gets_none_invented() -> None:
    new_text, _metrics = semantic_edit.apply_line_ranges(
        "a\nb",
        [{"start_line": 2, "end_line": 2, "new": "X", "fragment_sha256": _sha("b")}],
    )
    assert new_text == "a\nX"


def test_replacement_that_already_ends_with_a_terminator_is_left_alone() -> None:
    new_text, _metrics = semantic_edit.apply_line_ranges(
        "a\nb\nc\n",
        [{"start_line": 2, "end_line": 2, "new": "B\r\n", "fragment_sha256": _sha("b\n")}],
    )
    assert new_text == "a\nB\r\nc\n"


# --- existing refusals stay closed ----------------------------------------

def test_overlapping_ranges_still_raise() -> None:
    with pytest.raises(semantic_edit.SemanticEditError, match="semantic_edit_ranges_overlap"):
        semantic_edit.apply_line_ranges(
            "a\nb\nc\n",
            [
                {"start_line": 1, "end_line": 2, "new": "X"},
                {"start_line": 2, "end_line": 3, "new": "Y"},
            ],
        )


def test_replacement_size_cap_still_raises() -> None:
    oversize = "z" * (semantic_edit.MAX_REPLACEMENT_BYTES + 1)
    with pytest.raises(semantic_edit.SemanticEditError, match="semantic_edit_replacement_too_large:0"):
        semantic_edit.apply_line_ranges(
            "a\nb\nc\n",
            [{"start_line": 2, "end_line": 2, "new": oversize}],
        )


def test_too_many_ranges_still_raises() -> None:
    ranges = [
        {"start_line": i, "end_line": i, "new": "x"}
        for i in range(1, semantic_edit.MAX_RANGES_PER_FILE + 2)
    ]
    with pytest.raises(semantic_edit.SemanticEditError, match="semantic_edit_ranges_invalid"):
        semantic_edit.apply_line_ranges("x\n" * len(ranges), ranges)
