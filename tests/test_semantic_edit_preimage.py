"""NF-2026-00351: the range layer's preimage gate and terminator handling.

Two guarantees the ~60x semantic-edit saving rests on are pinned here by
driving ``apply_line_ranges`` directly -- no file IO, no wrapper -- so the
behaviour is asserted at the exact layer that owns it:

* the range-fragment preimage check is never silently skipped.  A supplied hash
  is verified and a mismatch raises; a missing hash (``None``, ``""`` or an
  absent key) still applies the edit but is reported as unverified in the
  returned accounting, so a caller can never mistake an unchecked write for a
  checked one;
* the trailing terminator is derived from what the old text ends with, so every
  terminator ``str.splitlines`` can produce -- including a bare ``\\r`` and the
  Unicode line breaks -- round-trips instead of being dropped by a hand-written
  ladder that only named CRLF and LF.

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


# --- U1: the terminator is derived, not laddered --------------------------

def test_bare_cr_file_keeps_its_line_structure() -> None:
    new_text, _metrics = semantic_edit.apply_line_ranges(
        "a\rb\rc\r",
        [{"start_line": 1, "end_line": 1, "new": "X", "fragment_sha256": _sha("a\r")}],
    )
    # The regression: lines 1 and 2 must not be glued into "Xb\rc\r".
    assert new_text == "X\rb\rc\r"
    assert new_text != "Xb\rc\r"


# Every terminator ``str.splitlines`` can produce.  A two-case ladder that only
# names CRLF and LF fails every row below except the first two.
_TERMINATORS = [
    "\r\n",
    "\n",
    "\r",
    "\v",
    "\f",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    " ",
    " ",
]


@pytest.mark.parametrize("term", _TERMINATORS, ids=[repr(t) for t in _TERMINATORS])
def test_every_splitlines_terminator_round_trips(term: str) -> None:
    text = f"a{term}b{term}"
    # Sanity: this terminator really is one splitlines treats as a line break.
    assert text.splitlines(keepends=True) == [f"a{term}", f"b{term}"]

    new_text, _metrics = semantic_edit.apply_line_ranges(
        text,
        [{"start_line": 1, "end_line": 1, "new": "X", "fragment_sha256": _sha(f"a{term}")}],
    )
    assert new_text == f"X{term}b{term}"


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
