"""NF-2026-00982: a failed check's cut summary keeps its failure-marker lines.

The extension-regression run-all output names the failing file on a line such
as ``<file> failed with exit code N``. Head+tail truncation used to elide that
line whenever the output was longer than the cap; these tests pin that the
line survives, and that passing summaries are unchanged.
"""

from aiworkhub.quality_evidence import (
    MAX_SUMMARY_CHARS,
    SUMMARY_TRUNCATION_MARKER,
    _bounded_summary,
)

EXIT_LINE = "vscode-extension/test/extension.test.js failed with exit code 1"


def _long_run_all_output(exit_line: str | None) -> str:
    """Build run-all output of roughly three caps, optionally with an exit line mid-way."""

    count = (3 * MAX_SUMMARY_CHARS) // 24 + 1
    lines = [f"passing case {i:08d} ok" for i in range(count)]
    if exit_line is not None:
        lines[count // 2] = exit_line
    return "\n".join(lines) + "\n"


def test_failing_run_all_summary_keeps_failed_with_exit_code_line() -> None:
    text = _long_run_all_output(EXIT_LINE)
    assert len(text) > 2 * MAX_SUMMARY_CHARS

    summary = _bounded_summary(text, status="failed")

    assert len(summary) == MAX_SUMMARY_CHARS
    assert SUMMARY_TRUNCATION_MARKER in summary
    assert EXIT_LINE in summary.splitlines()


def test_failure_marker_lines_keep_two_lines_of_context() -> None:
    lines = [f"passing case {i:08d} ok" for i in range(3 * MAX_SUMMARY_CHARS // 24 + 1)]
    mid = len(lines) // 2
    lines[mid] = EXIT_LINE
    text = "\n".join(lines) + "\n"

    summary_lines = _bounded_summary(text, status="failed").splitlines()

    assert EXIT_LINE in summary_lines
    at = summary_lines.index(EXIT_LINE)
    assert summary_lines[at - 1] == lines[mid - 1]
    assert summary_lines[at - 2] == lines[mid - 2]
    assert summary_lines[at + 1] == lines[mid + 1]
    assert summary_lines[at + 2] == lines[mid + 2]


def test_exit_line_survives_a_flood_of_other_failure_markers() -> None:
    noise = [f"Error: noise {i:08d}" for i in range(MAX_SUMMARY_CHARS // 10)]
    tail = [f"passing case {i:08d} ok" for i in range(3 * MAX_SUMMARY_CHARS // 24)]
    text = "\n".join(noise + [EXIT_LINE] + tail) + "\n"
    assert len(text) > 2 * MAX_SUMMARY_CHARS

    summary = _bounded_summary(text, status="failed")

    assert len(summary) == MAX_SUMMARY_CHARS
    assert EXIT_LINE in summary.splitlines()


def test_passing_summary_is_unchanged_byte_for_byte() -> None:
    text = _long_run_all_output(EXIT_LINE)
    note = f"\n[{SUMMARY_TRUNCATION_MARKER}: head retained]"
    expected = text[: max(0, MAX_SUMMARY_CHARS - len(note))] + note

    assert _bounded_summary(text, status="") == expected
    assert _bounded_summary(text, status="passed") == expected


def test_short_failing_summary_is_untouched() -> None:
    text = f"passing case 00000001 ok\n{EXIT_LINE}\n"
    assert len(text) <= MAX_SUMMARY_CHARS

    assert _bounded_summary(text, status="failed") == text


def test_failure_block_is_wrapped_before_the_head() -> None:
    text = _long_run_all_output(EXIT_LINE)

    summary = _bounded_summary(text, status="failed")

    assert summary.startswith(f"[{SUMMARY_TRUNCATION_MARKER}: failure lines]\n")
    assert summary.index("[end failure lines]") < summary.index("passing case 00000000 ok")
    assert len(summary) <= MAX_SUMMARY_CHARS


def test_distant_failure_windows_are_separated_by_a_gap_line() -> None:
    lines = [f"passing case {i:08d} ok" for i in range(3 * MAX_SUMMARY_CHARS // 24 + 1)]
    lines[5] = "Error: early failure"
    lines[len(lines) // 2] = EXIT_LINE
    text = "\n".join(lines) + "\n"

    summary_lines = _bounded_summary(text, status="failed").splitlines()
    block = summary_lines[1 : summary_lines.index("[end failure lines]")]

    assert "Error: early failure" in block
    assert EXIT_LINE in block
    assert "..." in block
    assert block.index("...") < block.index(EXIT_LINE)
    assert len("\n".join(summary_lines)) <= MAX_SUMMARY_CHARS
