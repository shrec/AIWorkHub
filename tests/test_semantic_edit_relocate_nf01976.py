"""A lone anchor checks in place; apply relocates a range an earlier apply shifted.

NF-2026-01976 (field report 0.12.84), two friction points:

1. ``semantic_edit._validated_anchors`` refused one anchor on a multi-line
   range.  It is now an in-place check of its own line of the requested range:
   a match binds that range, a mismatch raises ``SemanticEditAnchorMismatch``
   and the range never moves, because one anchor cannot tell a shifted range
   from a resized one.
2. ``semantic_edit_applier.replace_prepared_range`` refused any whole-file
   change since prepare, so a second range prepared in the same file went stale
   once the first was applied.  It now finds the untouched fragment by its
   prepared hash: one match applies there, none or several refuse stale.

Run: python -m pytest -q tests/test_semantic_edit_relocate_nf01976.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aiworkhub import semantic_edit, semantic_edit_applier

_PATH = "src/module.py"
_ALLOWED = ("src/*.py",)
_LINES = [f"line_{number:02d} = {number}\n" for number in range(1, 21)]


def _write(root: Path, lines: list[str] = _LINES) -> Path:
    target = root / _PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes("".join(lines).encode("utf-8"))
    return target


def _prepare(root: Path, start: int, end: int, **anchors: Any) -> semantic_edit.PreparedLineTarget:
    return semantic_edit.prepare_line_target(
        root, path=_PATH, start_line=start, end_line=end, allowed_writes=_ALLOWED, **anchors,
    )


def _apply(root: Path, target: semantic_edit.PreparedLineTarget, new: str) -> dict[str, Any]:
    _text, metrics = semantic_edit_applier.replace_prepared_range(
        root, target, new, allowed_writes=_ALLOWED,
    )
    return metrics


# ---------------------------------------------------------------------------
# 1. One anchor on a multi-line range is an in-place check.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "anchors",
    [{"expected_first_line": "line_05 = 5"}, {"expected_last_line": "line_08 = 8"}],
    ids=["first_only", "last_only"],
)
def test_one_matching_anchor_binds_the_requested_multi_line_range(
    tmp_path: Path, anchors: dict[str, Any],
) -> None:
    _write(tmp_path)
    bound = _prepare(tmp_path, 5, 8, **anchors)
    assert (bound.start_line, bound.end_line, bound.anchor_relocated_from) == (5, 8, None)
    assert bound == _prepare(tmp_path, 5, 8)
    assert bound.fragment == "".join(_LINES[4:8])


@pytest.mark.parametrize(
    ("anchors", "nearby"),
    [
        ({"expected_first_line": "line_07 = 7"}, (7, 10)),
        ({"expected_last_line": "line_06 = 6"}, (3, 6)),
    ],
    ids=["first_only", "last_only"],
)
def test_one_wrong_anchor_is_a_mismatch_even_when_its_text_is_nearby(
    tmp_path: Path, anchors: dict[str, Any], nearby: tuple[int, int],
) -> None:
    target = _write(tmp_path)
    with pytest.raises(semantic_edit.SemanticEditAnchorMismatch) as excinfo:
        _prepare(tmp_path, 5, 8, **anchors)
    assert str(excinfo.value) == f"semantic_edit_anchor_mismatch:{_PATH}"
    detail = excinfo.value.detail
    assert detail["actual_first_line"] == "line_05 = 5"
    assert detail["actual_last_line"] == "line_08 = 8"
    assert "5\tline_05 = 5" in detail["numbered_context"]
    # The anchor's real place is offered as a candidate, never bound.
    assert {"start_line": nearby[0], "end_line": nearby[1]} in detail["candidates"]
    assert detail["next_call"]
    assert target.read_bytes() == "".join(_LINES).encode("utf-8")


def test_both_anchors_still_relocate_a_shifted_multi_line_range(tmp_path: Path) -> None:
    _write(tmp_path)
    moved = _prepare(tmp_path, 5, 8, expected_first_line="line_07 = 7", expected_last_line="line_10 = 10")
    assert (moved.start_line, moved.end_line, moved.anchor_relocated_from) == (7, 10, (5, 8))


# ---------------------------------------------------------------------------
# 2. A second range prepared in the same file survives the first apply.
# ---------------------------------------------------------------------------

def test_a_later_range_shifted_by_an_earlier_apply_is_relocated(tmp_path: Path) -> None:
    target = _write(tmp_path)
    early = _prepare(tmp_path, 3, 4)
    late = _prepare(tmp_path, 12, 14)

    first = _apply(tmp_path, early, "line_03 = 30\nextra_a = 1\nextra_b = 2\nline_04 = 40\n")
    assert "relocated_from" not in first and "relocated_to" not in first

    second = _apply(tmp_path, late, "line_12 = 120\n")
    assert second["relocated_from"] == [12, 14]
    assert second["relocated_to"] == [14, 16]
    assert "mode_restored" in second

    expected = [
        *_LINES[:2],
        "line_03 = 30\n", "extra_a = 1\n", "extra_b = 2\n", "line_04 = 40\n",
        *_LINES[4:11],
        "line_12 = 120\n",
        *_LINES[14:],
    ]
    assert target.read_bytes() == "".join(expected).encode("utf-8")


def test_a_later_range_moved_up_by_a_deletion_is_relocated(tmp_path: Path) -> None:
    target = _write(tmp_path)
    early = _prepare(tmp_path, 2, 5)
    late = _prepare(tmp_path, 19, 20)

    _apply(tmp_path, early, "")
    second = _apply(tmp_path, late, "tail = 0\n")

    assert (second["relocated_from"], second["relocated_to"]) == ([19, 20], [15, 16])
    expected = [_LINES[0], *_LINES[5:18], "tail = 0\n"]
    assert target.read_bytes() == "".join(expected).encode("utf-8")


def test_a_fragment_the_first_apply_changed_is_refused_stale(tmp_path: Path) -> None:
    target = _write(tmp_path)
    early = _prepare(tmp_path, 3, 5)
    late = _prepare(tmp_path, 5, 6)

    _apply(tmp_path, early, "line_03 = 3\nline_04 = 4\nline_05 = 50\n")
    after_first = target.read_bytes()

    with pytest.raises(semantic_edit.SemanticEditError) as excinfo:
        _apply(tmp_path, late, "nope\n")
    assert str(excinfo.value) == f"semantic_edit_stale_file:{_PATH}"
    assert target.read_bytes() == after_first


def test_a_fragment_that_now_occurs_twice_is_refused_ambiguous(tmp_path: Path) -> None:
    target = _write(tmp_path)
    early = _prepare(tmp_path, 2, 2)
    late = _prepare(tmp_path, 10, 10)

    _apply(tmp_path, early, "line_02 = 2\nline_10 = 10\n")
    after_first = target.read_bytes()

    with pytest.raises(semantic_edit.SemanticEditError) as excinfo:
        _apply(tmp_path, late, "line_10 = 100\n")
    assert str(excinfo.value) == f"semantic_edit_stale_file:{_PATH}:ambiguous"
    assert target.read_bytes() == after_first


def test_the_syntax_guard_runs_on_a_relocated_apply(tmp_path: Path) -> None:
    target = _write(tmp_path)
    early = _prepare(tmp_path, 1, 1)
    late = _prepare(tmp_path, 10, 10)

    _apply(tmp_path, early, "line_01 = 1\nline_00 = 0\n")
    after_first = target.read_bytes()

    with pytest.raises(semantic_edit.SemanticEditError):
        _apply(tmp_path, late, "def broken(:\n")
    assert target.read_bytes() == after_first


def test_an_unchanged_file_applies_in_place_without_relocation_metrics(tmp_path: Path) -> None:
    target = _write(tmp_path)
    metrics = _apply(tmp_path, _prepare(tmp_path, 10, 11), "line_10 = 0\n")
    assert "relocated_from" not in metrics and "relocated_to" not in metrics
    expected = [*_LINES[:9], "line_10 = 0\n", *_LINES[11:]]
    assert target.read_bytes() == "".join(expected).encode("utf-8")


def test_relocation_does_not_widen_the_path_check(tmp_path: Path) -> None:
    _write(tmp_path)
    late = _prepare(tmp_path, 10, 10)
    _apply(tmp_path, _prepare(tmp_path, 1, 1), "line_01 = 1\nline_00 = 0\n")
    with pytest.raises(semantic_edit.SemanticEditError) as excinfo:
        semantic_edit_applier.replace_prepared_range(
            tmp_path, late, "line_10 = 0\n", allowed_writes=("docs/*.md",),
        )
    assert str(excinfo.value) == f"semantic_edit_path_not_allowed:{_PATH}"
