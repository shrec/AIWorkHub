"""NF-2026-01958: a range edit keeps the file's line ending."""

from __future__ import annotations

import hashlib

import pytest

from aiworkhub import semantic_edit


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _apply(text: str, start: int, end: int, new: str, old: str, **extra: object) -> str:
    item = {"start_line": start, "end_line": end, "new": new, "fragment_sha256": _sha(old)}
    item.update(extra)
    result, _metrics = semantic_edit.apply_line_ranges(text, [item])
    return result


def _has_bare_lf(text: str) -> bool:
    return text.replace("\r\n", "").count("\n") > 0


def test_multiline_replacement_in_crlf_file_is_written_with_crlf() -> None:
    text = "a\r\nb\r\nc\r\n"
    result = _apply(text, 2, 2, "x\ny\nz", "b\r\n")
    assert result == "a\r\nx\r\ny\r\nz\r\nc\r\n"
    assert not _has_bare_lf(result)


def test_multiline_replacement_in_lf_file_is_unchanged_behavior() -> None:
    text = "a\nb\nc\n"
    result = _apply(text, 2, 2, "x\ny\nz", "b\n")
    assert result == "a\nx\ny\nz\nc\n"
    assert "\r" not in result


def test_crlf_replacement_text_in_crlf_file_does_not_double_cr() -> None:
    text = "a\r\nb\r\nc\r\n"
    result = _apply(text, 2, 2, "x\r\ny\r\n", "b\r\n")
    assert result == "a\r\nx\r\ny\r\nc\r\n"
    assert "\r\r\n" not in result


def test_mixed_replacement_endings_normalize_to_crlf() -> None:
    text = "a\r\nb\r\nc\r\n"
    result = _apply(text, 1, 2, "x\r\ny\nz", "a\r\nb\r\n")
    assert result == "x\r\ny\r\nz\r\nc\r\n"


def test_unterminated_last_line_falls_back_to_file_dominant_ending() -> None:
    text = "a\r\nb\r\nc"
    result = _apply(text, 3, 3, "x\ny", "c")
    assert result == "a\r\nb\r\nx\r\ny"
    assert not _has_bare_lf(result)


def test_file_without_line_endings_uses_lf() -> None:
    result = _apply("only", 1, 1, "x\ny", "only")
    assert result == "x\ny"


@pytest.mark.parametrize("term", ["\n", "\r\n"])
def test_preserve_false_still_uses_range_ending_for_inner_lines(term: str) -> None:
    text = f"a{term}b{term}"
    result = _apply(text, 1, 1, f"x\ny{term}", f"a{term}", preserve_trailing_newline=False)
    assert result == f"x{term}y{term}b{term}"
