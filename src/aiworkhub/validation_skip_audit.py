"""Audit a green pytest validation for tests skipped for an environment reason.

NF-2026-02307: inside the AppContainer validation lane the sandbox tests are
skipped (the lane cannot nest the sandbox) yet the row exited 0 and was
reported green, so M6 cards passed validation while the code was broken.  A
declared pytest command whose tests were skipped *because of the sandbox* did
not prove anything; this module names those skips so ``run_validations`` can
refuse to count the row as passed and route it to the manager (host replay).

The audit is classification only and never executes anything.  Skip reasons
are authored by the candidate's tests, so the verdict can only ever *withhold*
a green; it names no trusted authority and cannot launch a host run.

Fail closed: the lane injects ``-rs`` through ``PYTEST_ADDOPTS`` but a declared
``-r<chars>`` / ``--no-summary`` on the command line wins over it.  The skipped
count pytest printed in its final summary line must therefore be fully
accounted for by parsed ``SKIPPED`` lines, otherwise the reasons are unreadable
and the row is withheld as environmental (``skip_reasons_unreadable``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# The one marker vocabulary (case-insensitive substrings of the skip reason).
ENVIRONMENT_SKIP_MARKERS: tuple[str, ...] = (
    "appcontainer",
    "sandbox lane",
    "nested sandbox",
    "inside the sandbox",
    "not available in the lane",
    "requires host",
)

SKIP_REPORT_FLAG = "-rs"
VALIDATION_ENV_SKIPPED = "validation_env_skipped"
SKIP_REASONS_UNREADABLE = "skip_reasons_unreadable"
MAX_REPORTED_SKIPS = 64
MAX_REASON_CHARS = 300
MAX_LINE_CHARS = 4096

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
_SKIP_PREFIX = "SKIPPED ["


def with_skip_report(addopts: str) -> str:
    """Return *addopts* with ``-rs`` appended unless it is already requested.

    The lane applies this ONLY to pytest invocations it runs (the caller gates
    on its pytest-command predicate); a non-pytest command never sees it.
    ``-rs`` makes pytest print the ``SKIPPED [n] path:line: reason`` lines the
    audit needs even when the declared command did not ask for them.
    """
    if SKIP_REPORT_FLAG in addopts.split():
        return addopts
    return f"{addopts} {SKIP_REPORT_FLAG}".strip()


def is_environment_reason(reason: str) -> bool:
    lowered = reason.lower()
    return any(marker in lowered for marker in ENVIRONMENT_SKIP_MARKERS)


def parse_skip_line(raw_line: str) -> tuple[int, str, str] | None:
    """``(count, location, reason)`` of one ``SKIPPED [n] loc: reason`` line.

    Linear-time string parsing (no backtracking regex): the count ends at the
    first ``"] "`` and the location at the first ``": "`` after it (a Windows
    drive colon is followed by a backslash, never a space).  A module-level
    skip reports no line number; a line without a reason yields ``""``.
    """
    line = _ANSI_ESCAPE.sub("", raw_line[:MAX_LINE_CHARS]).strip()
    if not line.startswith(_SKIP_PREFIX):
        return None
    count_text, separator, rest = line[len(_SKIP_PREFIX) :].partition("] ")
    if not separator or not (count_text.isascii() and count_text.isdigit()):
        return None
    location, _, reason = rest.partition(": ")
    return int(count_text), location.strip(), reason.strip()


def environment_skips(output: str, expected_skipped: int | None = None) -> list[dict[str, Any]]:
    """Skipped tests of a pytest run whose skip reason names the environment.

    *output* is the captured ``-rs`` style stdout.  Each entry carries the
    ``location`` (``path:line``; pytest's ``-rs`` reports the skip location,
    not a node id), the grouped ``count`` and the ``reason``.  Skips with any
    other reason (``skipif(os.name ...)``) are not reported.  When
    *expected_skipped* (pytest's own final-summary count) exceeds the sum of
    ALL parsed ``SKIPPED`` counts, whatever their reason, the reasons were not
    printed and one ``skip_reasons_unreadable`` entry is returned.  Bounded and
    deterministic.
    """
    found: list[dict[str, Any]] = []
    parsed_total = 0
    for raw_line in str(output or "").splitlines():
        parsed = parse_skip_line(raw_line)
        if parsed is None:
            continue
        count, location, reason = parsed
        parsed_total += count
        if len(found) < MAX_REPORTED_SKIPS and is_environment_reason(reason):
            found.append(
                {"location": location, "count": count, "reason": reason[:MAX_REASON_CHARS]}
            )
    if isinstance(expected_skipped, int) and expected_skipped > parsed_total:
        found.append(
            {
                "location": "",
                "count": expected_skipped - parsed_total,
                "reason": SKIP_REASONS_UNREADABLE,
            }
        )
    return found


def env_skip_row_fields(skips: list[dict[str, Any]]) -> dict[str, Any]:
    """Row fields that turn a green pytest row into an environmental non-pass.

    Reuses the lane vocabulary ``validation_environment`` (``failure_class``)
    that ``validation_runner.match_validation_environment_signature`` already
    maps to the typed ``validation_environment`` disposition, so the card goes
    to the manager instead of automatic rework.  ``returncode`` becomes
    ``None`` (as for a lane infrastructure row) so every existing failing-row
    predicate treats the row as not passed; the real exit status is kept as
    ``pytest_returncode``.
    """
    from .build_lane import VALIDATION_ENVIRONMENT_CLASS

    listed = ", ".join(f"{item['location']} ({item['reason']})" for item in skips)
    return {
        "returncode": None,
        "pytest_returncode": 0,
        "failure_class": VALIDATION_ENVIRONMENT_CLASS,
        VALIDATION_ENV_SKIPPED: True,
        "infrastructure": f"{VALIDATION_ENV_SKIPPED}:{listed}"[:1_000],
        "env_skipped_tests": [dict(item) for item in skips],
    }


def apply_env_skip_audit(row: Mapping[str, Any], output: str) -> dict[str, Any]:
    """The single lane entry point: audit one pytest row, return it (rewritten).

    Only an exit-0 row can be rewritten; any other row is returned unchanged.
    The caller gates on its pytest-command predicate, so non-pytest rows never
    reach this function.  ``row["pytest_outcomes"]`` (the lane's own parse of
    the final summary line) supplies the skipped count for the fail-closed
    check.
    """
    updated = dict(row)
    if updated.get("returncode") != 0:
        return updated
    outcomes = updated.get("pytest_outcomes")
    expected = outcomes.get("skipped") if isinstance(outcomes, Mapping) else None
    skips = environment_skips(output, expected if isinstance(expected, int) else None)
    if skips:
        updated.update(env_skip_row_fields(skips))
    return updated
