"""NF-2026-02084: the one failure-marker table for validation output.

``mcp_summary_folds.first_failure_from_status`` (which lines of a failing row
to show) and ``validation_auto_rework.build_reason`` (which failing cases to
list) both read this table, so a framework added here reaches both. Pure
functions over already-bounded text: they never run anything and never decide
a verdict.
"""

from __future__ import annotations

import re

# (framework, pattern, names_a_failing_case). A line matching any pattern is a
# failure-marker line; the case-header subset names one failing test/assertion.
FAILURE_MARKERS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("pytest", re.compile(r"^FAILED \S"), True),
    ("pytest", re.compile(r"^_{3,} test"), False),
    ("pytest", re.compile(r"^E {3}"), False),
    ("pytest", re.compile(r"AssertionError"), False),
    ("catch2", re.compile(r"^\S.*(?::\d+:|\(\d+\):) FAILED"), True),
    ("catch2", re.compile(r"FAILED:"), False),
    ("ctest", re.compile(r"\*\*\*Failed"), True),
    ("ctest", re.compile(r"tests failed out of"), False),
    ("gtest", re.compile(r"^\s*\[  FAILED  \] (?!\d+ tests?, listed below)"), True),
    ("generic", re.compile(r"\berror:"), False),
)
CASE_HEADER_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    pattern for _framework, pattern, names_case in FAILURE_MARKERS if names_case
)

FAILURE_CONTEXT_BEFORE = 5
FAILURE_CONTEXT_AFTER = 10
CASE_HEADER_LIMIT = 20
CASE_HEADER_CHARS = 160
# Bounds the scan of one tail; tails are already byte-capped upstream.
MAX_SCAN_LINES = 20_000
# gtest repeats a failed case in its summary without the "(12 ms)" suffix.
_DURATION_SUFFIX = re.compile(r"\s*\(\d+(?:\.\d+)?\s*(?:ms|s|sec)\)\s*$")


def is_failure_marker(line: str) -> bool:
    return any(pattern.search(line) for _framework, pattern, _case in FAILURE_MARKERS)


def failure_excerpt(text: str) -> str | None:
    """The first failure-marker line with a few surrounding lines; ``None`` without one."""
    lines = str(text or "").splitlines()[:MAX_SCAN_LINES]
    for index, line in enumerate(lines):
        if is_failure_marker(line):
            start = max(0, index - FAILURE_CONTEXT_BEFORE)
            return "\n".join(lines[start:index + 1 + FAILURE_CONTEXT_AFTER]).rstrip()
    return None


def failing_case_headers(text: str) -> tuple[list[str], int]:
    """Every distinct failing-case header line in order: first ``CASE_HEADER_LIMIT`` and the omitted count."""
    headers: list[str] = []
    seen: set[str] = set()
    for raw in str(text or "").splitlines()[:MAX_SCAN_LINES]:
        if not any(pattern.search(raw) for pattern in CASE_HEADER_MARKERS):
            continue
        line = raw.strip()
        key = _DURATION_SUFFIX.sub("", line)
        if key in seen:
            continue
        seen.add(key)
        headers.append(line[:CASE_HEADER_CHARS])
    return headers[:CASE_HEADER_LIMIT], max(0, len(headers) - CASE_HEADER_LIMIT)


__all__ = [
    "CASE_HEADER_CHARS",
    "CASE_HEADER_LIMIT",
    "CASE_HEADER_MARKERS",
    "FAILURE_CONTEXT_AFTER",
    "FAILURE_CONTEXT_BEFORE",
    "FAILURE_MARKERS",
    "MAX_SCAN_LINES",
    "failing_case_headers",
    "failure_excerpt",
    "is_failure_marker",
]
