"""Content anchors verify, relocate or refuse an off-by-N replace range.

NF-2026-01533 (measured): models derive 1-based line numbers by counting
un-numbered Source Graph text, and ``prepare`` bound whatever range it was
handed, so a range a few lines off was applied silently -- GLM 5.3 in
EntryLink shifted 0/-2/-1/-1 with the correct replacement text.  The caller
now passes the exact text of the first and last line it means to replace;
``semantic_edit.prepare_line_target`` is the one definition that keeps,
relocates or refuses the range, and every surface passes the anchors through.

Run: python -m pytest -q tests/test_semantic_edit_anchor.py
"""

from __future__ import annotations

import dataclasses
import inspect
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from aiworkhub import semantic_edit
from aiworkhub import worker_ai_tools_mcp as worker_tools

_PATH = "src/module.py"
# Forty distinct lines: every shift these tests take stays inside the file.
_LINES = [f"line_{number:02d} = {number}\n" for number in range(1, 41)]


def _write(root: Path, lines: list[str] = _LINES) -> Path:
    target = root / _PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes("".join(lines).encode("utf-8"))
    return target


def _anchors(first: Any, last: Any) -> dict[str, Any]:
    return {"expected_first_line": first, "expected_last_line": last}


def _prepare(root: Path, start: int, end: int, **anchors: Any) -> semantic_edit.PreparedLineTarget:
    return semantic_edit.prepare_line_target(
        root, path=_PATH, start_line=start, end_line=end,
        allowed_writes=("src/*.py",), **anchors,
    )


def _mismatch(root: Path, start: int, end: int, **anchors: Any) -> dict[str, Any]:
    with pytest.raises(semantic_edit.SemanticEditAnchorMismatch) as excinfo:
        _prepare(root, start, end, **anchors)
    assert str(excinfo.value) == f"semantic_edit_anchor_mismatch:{_PATH}"
    return excinfo.value.detail


def _context(root: Path, *, ledger: Path | None = None) -> worker_tools.WorkerToolContext:
    key = None
    if ledger is not None:
        key = ledger.with_suffix(".key")
        key.write_bytes(b"k" * 32)
    return worker_tools.WorkerToolContext(
        task_id="TASK", runner="runner", topic="topic", request_id="request",
        repo=root, authority_repo=root, source_graph_targets=(_PATH,),
        session_topic="topic", audit_ledger_path=ledger, audit_hmac_key_path=key,
        allowed_writes=("src/*.py",),
    )


def _session(root: Path, **kwargs: Any) -> worker_tools.WorkerSemanticEditSession:
    return worker_tools.WorkerSemanticEditSession(_context(root, **kwargs))


# ---------------------------------------------------------------------------
# Verify: matching anchors change nothing.
# ---------------------------------------------------------------------------

def test_matching_anchors_bind_the_target_an_unanchored_prepare_binds(tmp_path: Path) -> None:
    _write(tmp_path)
    plain = _prepare(tmp_path, 10, 12)
    # The comparison is on stripped text: indentation and the line end the
    # caller copied along with the line are not a mismatch.
    anchored = _prepare(tmp_path, 10, 12, **_anchors("line_10 = 10", "  line_12 = 12\n"))

    assert anchored == plain
    assert anchored.anchor_relocated_from is None
    assert (anchored.start_line, anchored.end_line) == (10, 12)
    assert anchored.current_sha256 == plain.current_sha256
    assert anchored.fragment_sha256 == plain.fragment_sha256
    assert anchored.receipt(target_id="t") == plain.receipt(target_id="t")
    assert "anchor_relocated_from" not in anchored.receipt()

    # The session receipt is the same bytes apart from its fresh target_id.
    def receipt(**anchors: Any) -> dict[str, Any]:
        reply = _session(tmp_path).prepare(file_path=_PATH, start_line=10, end_line=12, **anchors)
        assert reply.pop("target_id")
        return reply

    assert receipt(**_anchors("line_10 = 10", "line_12 = 12")) == receipt()


# ---------------------------------------------------------------------------
# Relocate: the measured GLM shifts move to the one range the anchors name.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shift", [-2, -1, 1])
def test_a_glm_shift_relocates_and_apply_replaces_exactly_the_intended_lines(
    tmp_path: Path, shift: int,
) -> None:
    """The model's range is ``shift`` lines off the one its anchors name."""
    target = _write(tmp_path)
    session = _session(tmp_path)

    prepared = session.prepare(
        file_path=_PATH, start_line=20 + shift, end_line=22 + shift,
        **_anchors("line_20 = 20", "line_22 = 22"),
    )
    assert prepared["ok"] is True, prepared
    assert (prepared["start_line"], prepared["end_line"]) == (20, 22)
    assert prepared["anchor_relocated_from"] == {
        "start_line": 20 + shift, "end_line": 22 + shift,
    }
    assert prepared["fragment"] == "".join(_LINES[19:22])

    applied = session.apply(
        target_id=prepared["target_id"], new="LINE_20\nLINE_21\nLINE_22\n",
        idempotency_key=f"glm-shift{shift}",
    )
    assert applied["ok"] is True, applied
    assert (applied["start_line"], applied["end_line"]) == (20, 22)
    assert applied["preimage_verified"] is True
    assert target.read_bytes().decode("utf-8") == "".join(
        _LINES[:19] + ["LINE_20\n", "LINE_21\n", "LINE_22\n"] + _LINES[22:]
    )


@pytest.mark.parametrize("shift", [-2, -1, 1])
def test_a_relocated_target_is_the_exact_target_plus_where_it_came_from(
    tmp_path: Path, shift: int,
) -> None:
    _write(tmp_path)
    moved = _prepare(
        tmp_path, 20 + shift, 22 + shift, **_anchors("line_20 = 20", "line_22 = 22"),
    )
    assert moved.anchor_relocated_from == (20 + shift, 22 + shift)
    assert dataclasses.replace(moved, anchor_relocated_from=None) == _prepare(tmp_path, 20, 22)


@pytest.mark.parametrize("shift", [16, -16])
def test_the_furthest_relocation_is_sixteen_lines(tmp_path: Path, shift: int) -> None:
    _write(tmp_path)
    moved = _prepare(tmp_path, 20 + shift, 20 + shift, **_anchors("line_20 = 20", "line_20 = 20"))
    assert (moved.start_line, moved.end_line) == (20, 20)
    assert moved.anchor_relocated_from == (20 + shift, 20 + shift)


@pytest.mark.parametrize("shift", [17, -17])
def test_a_match_beyond_sixteen_lines_is_a_candidate_not_a_relocation(
    tmp_path: Path, shift: int,
) -> None:
    _write(tmp_path)
    detail = _mismatch(
        tmp_path, 20 + shift, 20 + shift, **_anchors("line_20 = 20", "line_20 = 20"),
    )
    assert detail["candidates"] == [{"start_line": 20, "end_line": 20}]


def test_one_blank_anchor_still_relocates(tmp_path: Path) -> None:
    _write(tmp_path, ["a\n", "b\n", "\n", "c\n"])
    moved = _prepare(tmp_path, 1, 2, **_anchors("b", ""))
    assert (moved.start_line, moved.end_line, moved.anchor_relocated_from) == (2, 3, (1, 2))
    assert moved.fragment == "b\n\n"


def test_crlf_lines_compare_and_display_without_their_terminator(tmp_path: Path) -> None:
    _write(tmp_path, ["a\r\n", "b\r\n", "c\r\n", "d\r\n"])
    moved = _prepare(tmp_path, 1, 2, **_anchors("b", "c"))
    assert (moved.start_line, moved.end_line) == (2, 3)
    assert moved.fragment == "b\r\nc\r\n"

    detail = _mismatch(tmp_path, 1, 1, **_anchors("z", "z"))
    assert detail["actual_first_line"] == "a"
    assert detail["numbered_context"] == ["1\ta", "2\tb", "3\tc", "4\td"]


def test_lines_are_numbered_on_newline_only(tmp_path: Path) -> None:
    # ``str.splitlines`` would also break on the bare CR and on U+2028 and
    # number ``three`` 5; the editor's one line model numbers it 3.
    _write(tmp_path, ["one\n", "two\rTWO II\n", "three\n", "four\n"])
    moved = _prepare(tmp_path, 1, 1, **_anchors("three", "three"))
    assert (moved.start_line, moved.anchor_relocated_from) == (3, (1, 1))
    assert moved.fragment == "three\n"


# ---------------------------------------------------------------------------
# Refuse: ambiguous or absent anchors bind nothing and say what is there.
# ---------------------------------------------------------------------------

def test_ambiguous_anchors_refuse_with_the_real_lines_and_bind_nothing(tmp_path: Path) -> None:
    lines = ["top\n", "    pass\n", "middle\n", "    pass\n", "bottom\n"]
    target = _write(tmp_path, lines)
    session = _session(tmp_path)

    refused = session.prepare(
        file_path=_PATH, start_line=3, end_line=3, **_anchors("pass", "pass"),
    )

    assert refused == {
        "ok": False,
        "tool": "semantic_edit_prepare",
        "reason": "semantic_edit_anchor_mismatch:src/module.py",
        "actual_first_line": "middle",
        "actual_last_line": "middle",
        "numbered_context": [
            "1\ttop", "2\t    pass", "3\tmiddle", "4\t    pass", "5\tbottom",
        ],
        "candidates": [
            {"start_line": 2, "end_line": 2}, {"start_line": 4, "end_line": 4},
        ],
        "next_call": refused["next_call"],
    }
    assert "semantic_edit_prepare" in refused["next_call"]
    assert "expected_first_line" in refused["next_call"]
    # No target was bound, so no apply can write anything.
    assert session._targets == {}
    assert target.read_bytes() == "".join(lines).encode("utf-8")


def test_anchors_found_nowhere_refuse_with_no_candidates(tmp_path: Path) -> None:
    target = _write(tmp_path)
    with pytest.raises(semantic_edit.SemanticEditError) as excinfo:
        _prepare(tmp_path, 10, 11, **_anchors("line_10 = 10", "line_99 = 99"))

    assert isinstance(excinfo.value, semantic_edit.SemanticEditAnchorMismatch)
    detail = excinfo.value.detail
    assert set(detail) == {
        "actual_first_line", "actual_last_line", "numbered_context", "candidates", "next_call",
    }
    assert detail["actual_first_line"] == "line_10 = 10"
    assert detail["actual_last_line"] == "line_11 = 11"
    # start-3 .. end+3, one "<n><TAB><line>" row per line.
    assert detail["numbered_context"] == [
        f"{number}\tline_{number:02d} = {number}" for number in range(7, 15)
    ]
    assert detail["candidates"] == []
    assert target.read_bytes() == "".join(_LINES).encode("utf-8")


def test_candidates_are_the_five_nearest_matches(tmp_path: Path) -> None:
    # Every odd line is ``pass``: the request sits between two equal matches.
    _write(tmp_path, [
        "    pass\n" if number % 2 else f"x{number}\n" for number in range(1, 31)
    ])
    detail = _mismatch(tmp_path, 16, 16, **_anchors("pass", "pass"))
    assert detail["candidates"] == [
        {"start_line": start, "end_line": start} for start in (15, 17, 13, 19, 11)
    ]


def test_numbered_context_is_capped_at_sixty_rows(tmp_path: Path) -> None:
    _write(tmp_path, [f"row {number}\n" for number in range(1, 101)])
    context = _mismatch(tmp_path, 10, 90, **_anchors("absent", "absent"))["numbered_context"]
    assert len(context) == 60
    assert (context[0], context[-1]) == ("7\trow 7", "66\trow 66")


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_both_empty_anchors_never_relocate(tmp_path: Path, blank: str) -> None:
    # Line 2 is the only blank line, so a unique shift exists -- but blank
    # lines identify nothing, so the range is refused rather than moved.
    lines = ["a\n", "\n", "b\n", "c\n", "d\n"]
    target = _write(tmp_path, lines)

    detail = _mismatch(tmp_path, 1, 1, **_anchors(blank, blank))
    assert detail["actual_first_line"] == "a"

    # Where they are true they still verify the range.
    kept = _prepare(tmp_path, 2, 2, **_anchors(blank, blank))
    assert (kept.start_line, kept.end_line, kept.anchor_relocated_from) == (2, 2, None)
    assert target.read_bytes() == "".join(lines).encode("utf-8")


@pytest.mark.parametrize(
    "anchors",
    [
        {"expected_first_line": "line_10 = 10"},
        {"expected_last_line": "line_10 = 10"},
        {"expected_first_line": "", "expected_last_line": None},
    ],
    ids=["first_only", "last_only", "empty_first_only"],
)
def test_a_single_anchor_is_refused(tmp_path: Path, anchors: dict[str, Any]) -> None:
    _write(tmp_path)
    with pytest.raises(semantic_edit.SemanticEditError) as excinfo:
        _prepare(tmp_path, 10, 10, **anchors)
    assert str(excinfo.value) == "semantic_edit_anchor_incomplete:src/module.py"

    refused = _session(tmp_path).prepare(file_path=_PATH, start_line=10, end_line=10, **anchors)
    assert refused == {
        "ok": False,
        "tool": "semantic_edit_prepare",
        "reason": "semantic_edit_anchor_incomplete:src/module.py",
    }


def test_a_non_text_anchor_is_refused(tmp_path: Path) -> None:
    _write(tmp_path)
    with pytest.raises(semantic_edit.SemanticEditError) as excinfo:
        _prepare(tmp_path, 10, 10, **_anchors(10, "line_10 = 10"))
    assert str(excinfo.value) == "semantic_edit_anchor_invalid:src/module.py"


def test_an_out_of_bounds_range_is_refused_before_anchors_are_read(tmp_path: Path) -> None:
    _write(tmp_path)
    with pytest.raises(semantic_edit.SemanticEditError) as excinfo:
        _prepare(tmp_path, 40, 41, **_anchors("line_39 = 39", "line_40 = 40"))
    assert str(excinfo.value) == "semantic_edit_line_range_out_of_bounds:40:41:40"


def test_the_empty_file_virtual_line_takes_empty_anchors(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "result.txt").write_bytes(b"")

    def prepare(**anchors: Any) -> semantic_edit.PreparedLineTarget:
        return semantic_edit.prepare_line_target(
            tmp_path, path="out/result.txt", start_line=1, end_line=1,
            allowed_writes=("out/*.txt",), **anchors,
        )

    kept = prepare(**_anchors("", ""))
    assert (kept.fragment, kept.anchor_relocated_from) == ("", None)
    with pytest.raises(semantic_edit.SemanticEditAnchorMismatch) as excinfo:
        prepare(**_anchors("x", "x"))
    assert excinfo.value.detail["numbered_context"] == ["1\t"]
    assert excinfo.value.detail["candidates"] == []


# ---------------------------------------------------------------------------
# The worker session and MCP surface.
# ---------------------------------------------------------------------------

def test_the_delivery_registry_records_the_bound_range_not_the_request(tmp_path: Path) -> None:
    _write(tmp_path)
    session = _session(tmp_path)

    moved = session.prepare(
        file_path=_PATH, start_line=8, end_line=9, **_anchors("line_10 = 10", "line_11 = 11"),
    )
    assert moved["anchor_relocated_from"] == {"start_line": 8, "end_line": 9}
    assert moved["fragment"] == "line_10 = 10\nline_11 = 11\n"

    again = session.prepare(file_path=_PATH, start_line=10, end_line=11)
    assert again["fragment_omitted"] is True
    assert again["delivered_by"] == moved["target_id"]
    asked = session.prepare(file_path=_PATH, start_line=8, end_line=9)
    assert asked["fragment"] == "line_08 = 8\nline_09 = 9\n"


def test_the_mismatch_detail_rides_on_the_reply_never_the_ledger(tmp_path: Path) -> None:
    _write(tmp_path)
    ledger = tmp_path / "audit.jsonl"
    refused = _session(tmp_path, ledger=ledger).prepare(
        file_path=_PATH, start_line=10, end_line=10, **_anchors("absent", "absent"),
    )
    assert refused["numbered_context"]

    recorded = ledger.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in recorded.splitlines()]
    assert [row["violation"] for row in rows] == ["semantic_edit_anchor_mismatch:src/module.py"]
    assert "line_10" not in recorded
    # The refusal carries file content, so the ledger counts its bytes.
    assert rows[0]["bytes_returned"] > 0


class _RecordingMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, name: str | None = None, **_options: Any) -> Callable[..., Any]:
        def register(function: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name or function.__name__] = function
            return function

        return register


def test_the_worker_mcp_tool_passes_both_anchors(tmp_path: Path) -> None:
    _write(tmp_path)
    mcp = _RecordingMCP()
    worker_tools.register_tools(mcp, _context(tmp_path))
    prepare = mcp.tools["aiworkhub_worker_semantic_edit_prepare"]

    parameters = inspect.signature(prepare).parameters
    assert parameters["expected_first_line"].default is None
    assert parameters["expected_last_line"].default is None
    assert "exact text of the first/last line to replace" in (prepare.__doc__ or "")

    moved = prepare(
        file_path=_PATH, start_line=4, end_line=4, **_anchors("line_05 = 5", "line_05 = 5"),
    )
    assert moved["ok"] is True, moved
    assert (moved["start_line"], moved["anchor_relocated_from"]) == (
        5, {"start_line": 4, "end_line": 4},
    )


def test_a_bom_on_line_one_neither_blocks_nor_shows_in_an_anchor(tmp_path: Path) -> None:
    lines = ["\ufeff" + _LINES[0], *_LINES[1:]]
    _write(tmp_path, lines)

    bound = _prepare(tmp_path, 1, 2, **_anchors("line_01 = 1", "line_02 = 2"))
    assert (bound.start_line, bound.end_line) == (1, 2)

    detail = _mismatch(tmp_path, 1, 1, **_anchors("absent", "absent"))
    assert detail["actual_first_line"] == "line_01 = 1"
    assert detail["numbered_context"][0] == "1\tline_01 = 1"


def test_a_long_line_is_truncated_in_the_mismatch_detail(tmp_path: Path) -> None:
    long_line = "x = '" + "a" * 100_000 + "'\n"
    _write(tmp_path, [*_LINES[:9], long_line, *_LINES[10:]])

    detail = _mismatch(tmp_path, 10, 10, **_anchors("absent", "absent"))
    assert len(detail["actual_first_line"]) < 300
    assert detail["actual_first_line"].endswith("[truncated 100006 chars]")
    assert len(json.dumps(detail)) < 20_000
