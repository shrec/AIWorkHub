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

import posixpath
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from pathlib import PurePosixPath
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
# Files whose presence decides pytest's rootdir (NF-2026-02371 F1).
PYTEST_ROOTDIR_FILES = frozenset(
    {"pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg"}
)
VALIDATION_ENV_SKIPPED = "validation_env_skipped"
SKIP_REASONS_UNREADABLE = "skip_reasons_unreadable"
# NF-2026-02371: env-skip authored only by canonical-unchanged test files.
ENV_SKIP_CANONICAL = "validation_env_skip_canonical"
# NF-2026-02374: env-skip in a test file the candidate changed; routed, marked.
ENV_SKIP_CANDIDATE_CHANGED = "validation_env_skip_candidate_changed"
ENV_SKIP_CANDIDATE_AUTHORED = "env_skip_candidate_authored"
# Row keys whose skipped-path list routes the row's command to the host stage.
ENV_SKIP_ROUTED_KEYS: tuple[str, ...] = (ENV_SKIP_CANONICAL, ENV_SKIP_CANDIDATE_CHANGED)
HOST_STAGE_COMMAND = "host_stage_command"
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


def skip_location_path(location: str) -> str | None:
    """Repo-relative POSIX path of one ``-rs`` skip location; ``None`` if unparseable.

    ``path:line`` and module-level ``path`` both read as ``path``.  An absolute,
    drive-qualified or ``..`` location, or a non-``.py`` file, cannot be compared
    to the candidate's repo-relative changed paths, so it is refused.
    """
    text = str(location or "").strip().replace("\\", "/")
    head, separator, tail = text.rpartition(":")
    if separator and tail.isascii() and tail.isdigit():
        text = head
    pure = PurePosixPath(text)
    if (
        not text
        or not text.endswith(".py")
        or ":" in text
        or text.startswith(("-", "@"))
        or pure.is_absolute()
        or ".." in pure.parts
    ):
        return None
    return posixpath.normpath(text)


def environment_skip_paths(skips: list[dict[str, Any]]) -> list[str] | None:
    """Distinct files named by env-skip entries, in order; ``None`` if any is unparseable."""
    paths: list[str] = []
    for item in skips:
        path = skip_location_path(str(item.get("location") or ""))
        if path is None:
            return None
        if path not in paths:
            paths.append(path)
    return paths or None


def conftest_chain(path: str) -> list[str]:
    """Every ``conftest.py`` pytest may load for *path*, root first."""
    parents = PurePosixPath(path).parts[:-1]
    return ["/".join((*parents[:depth], "conftest.py")) for depth in range(len(parents) + 1)]


def env_skip_path_split(
    skips: list[dict[str, Any]], changed_paths: Collection[str]
) -> tuple[list[str], list[str]] | None:
    """``(skipping files, candidate-changed ones)``; ``None`` when the row must be withheld.

    Withheld (``None``): no skips, a truncated report, an unparseable location,
    a changed pytest rootdir file, or a changed ``conftest.py`` on any skipping
    file's path.  Otherwise the second list names the skipping files the
    candidate itself changed (NF-2026-02374); it is empty for a canonical skip.
    """
    if not skips or len(skips) >= MAX_REPORTED_SKIPS:
        return None  # truncated report: an unlisted skip could be candidate-authored
    paths = environment_skip_paths(skips)
    if paths is None:
        return None
    changed = {posixpath.normpath(str(item).replace("\\", "/")) for item in changed_paths}
    # A changed ini file can move pytest's rootdir, so skip locations stop being repo-relative.
    if any(PurePosixPath(path).name in PYTEST_ROOTDIR_FILES for path in changed):
        return None
    for path in paths:
        if any(conftest in changed for conftest in conftest_chain(path)):
            return None
    return paths, [path for path in paths if path in changed]


def canonical_env_skip_paths(
    skips: list[dict[str, Any]], changed_paths: Collection[str]
) -> list[str] | None:
    """The skipping files when none of them nor their conftests was changed, else ``None``."""
    split = env_skip_path_split(skips, changed_paths)
    if split is None or split[1]:
        return None
    return split[0]


def env_skip_canonical_fields(
    row: Mapping[str, Any], skips: list[dict[str, Any]], paths: list[str]
) -> dict[str, Any] | None:
    """Green-row evidence for a canonical-authored env-skip; ``None`` without a command."""
    command = row.get("declared_command") or row.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    prior = row.get("known_unknown")
    known = [dict(item) for item in prior if isinstance(item, Mapping)] if isinstance(prior, list) else []
    known.append(
        {
            "kind": ENV_SKIP_CANONICAL,
            "paths": list(paths),
            "reason": "environment skip in canonical-unchanged test file; host stage decides",
        }
    )
    return {
        ENV_SKIP_CANONICAL: list(paths),
        "known_unknown": known,
        "env_skipped_tests": [dict(item) for item in skips],
        HOST_STAGE_COMMAND: command,
    }


def env_skip_candidate_fields(
    row: Mapping[str, Any], skips: list[dict[str, Any]], paths: list[str], authored: list[str]
) -> dict[str, Any] | None:
    """Green-row evidence for an env-skip in a candidate-changed test file (NF-2026-02374).

    Same routing evidence as :func:`env_skip_canonical_fields`, under the
    ``validation_env_skip_candidate_changed`` kind, plus the explicit
    ``env_skip_candidate_authored`` marker listing the changed skipping files.
    The accept-time host stage decides; nothing runs here.
    """
    command = row.get("declared_command") or row.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    prior = row.get("known_unknown")
    known = [dict(item) for item in prior if isinstance(item, Mapping)] if isinstance(prior, list) else []
    known.append(
        {
            "kind": ENV_SKIP_CANDIDATE_CHANGED,
            "paths": list(paths),
            ENV_SKIP_CANDIDATE_AUTHORED: list(authored),
            "reason": "environment skip in candidate-changed test file; host stage decides",
        }
    )
    return {
        ENV_SKIP_CANDIDATE_CHANGED: list(paths),
        ENV_SKIP_CANDIDATE_AUTHORED: list(authored),
        "known_unknown": known,
        "env_skipped_tests": [dict(item) for item in skips],
        HOST_STAGE_COMMAND: command,
    }


def apply_env_skip_audit(
    row: Mapping[str, Any],
    output: str,
    *,
    changed_paths: Callable[[], Iterable[str]] | None = None,
) -> dict[str, Any]:
    """The single lane entry point: audit one pytest row, return it (rewritten).

    Only an exit-0 row can be rewritten; any other row is returned unchanged.
    The caller gates on its pytest-command predicate, so non-pytest rows never
    reach this function.  ``row["pytest_outcomes"]`` (the lane's own parse of
    the final summary line) supplies the skipped count for the fail-closed
    check.

    NF-2026-02371: *changed_paths* is a zero-arg callable returning the
    candidate's changed paths; it is called lazily, only for a non-timed-out
    row with env skips.  An env-skip whose every skipping file and conftest on
    its path is unchanged stays green with ``known_unknown`` evidence and a
    ``host_stage_command``.  Without the callable (default), when it raises, a
    timed-out row, or an unparseable location, the row is withheld.

    NF-2026-02374: an env-skip whose skipping file the candidate changed (no
    changed conftest on its path, no changed rootdir file) is routed to the
    accept-time host stage the same way, marked candidate-authored, instead of
    a withhold whose lane replay would repeat the same skip forever.
    """
    updated = dict(row)
    if updated.get("returncode") != 0:
        return updated
    outcomes = updated.get("pytest_outcomes")
    expected = outcomes.get("skipped") if isinstance(outcomes, Mapping) else None
    skips = environment_skips(output, expected if isinstance(expected, int) else None)
    if skips:
        delta: frozenset[str] | None = None
        if changed_paths is not None and not updated.get("timed_out"):
            try:
                delta = frozenset(changed_paths())
            # Broad on purpose: WorkspaceError cannot be imported (circular) and any failure must withhold.
            except Exception:
                delta = None
        split = env_skip_path_split(skips, delta) if delta is not None else None
        fields: dict[str, Any] | None = None
        if split is not None and not split[1]:
            fields = env_skip_canonical_fields(updated, skips, split[0])
        elif split is not None:
            fields = env_skip_candidate_fields(updated, skips, split[0], split[1])
        updated.update(fields if fields is not None else env_skip_row_fields(skips))
    return updated
