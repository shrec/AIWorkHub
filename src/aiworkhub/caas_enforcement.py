"""CAAS enforcement — Continuous Audit as a Service, upheld by construction.

CAAS stands for **Continuous Audit as a Service** (the owner-canonical
expansion; ``docs/CAAS_PROTOCOL.md`` is the contract). This module checks the
protocol's automatically-enforceable properties as part of the normal
lifecycle: a transition wrapped with :func:`enforce_caas` runs the check on
every call and refuses to execute against a non-compliant state, so nobody has
to remember to run a checker and a repository cannot drift out of compliance
silently.

Properties that cannot be enforced automatically from this repository (for
example the wrong expansion in the separate UltrafastSecp256k1 README) are
reported by name via :meth:`ComplianceReport.unenforceable`, never treated as
covered.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any

#: The owner-canonical expansion of CAAS. Do not change without the owner.
CAAS_EXPANSION = "Continuous Audit as a Service"

#: Expansions that are known-wrong and must never appear in repository-controlled
#: documentation. The first appears in the separate UltrafastSecp256k1 README,
#: which this repository cannot edit and only names.
FORBIDDEN_EXPANSIONS: tuple[str, ...] = (
    "Continuous Automated Assurance System",
    "Continuous Automated Assurance",
)


class Enforceability(Enum):
    """How far a CAAS property can be enforced from this repository."""

    AUTOMATIC = "automatic"
    PARTIAL = "partial"
    EXTERNAL = "external"


@dataclass(frozen=True)
class CaasProperty:
    """One named property from the CAAS protocol."""

    id: str
    title: str
    enforceability: Enforceability
    note: str


# The protocol properties, mirrored from docs/CAAS_PROTOCOL.md. Keep the ids in
# sync with that document; the ids are the names used when reporting gaps.
PROTOCOL_PROPERTIES: tuple[CaasProperty, ...] = (
    CaasProperty(
        "CAAS-P1",
        "Canonical expansion in repository-controlled docs",
        Enforceability.AUTOMATIC,
        "No forbidden CAAS expansion may appear in repo-controlled documentation.",
    ),
    CaasProperty(
        "CAAS-P2",
        "Enforcement by construction",
        Enforceability.AUTOMATIC,
        "The check runs as part of the lifecycle, not as a remembered step.",
    ),
    CaasProperty(
        "CAAS-P3",
        "Audit layer is read-only",
        Enforceability.AUTOMATIC,
        "The audit layer cannot mutate repository code.",
    ),
    CaasProperty(
        "CAAS-P4",
        "Narrow, bounded audit scope",
        Enforceability.AUTOMATIC,
        "Audit passes run on a bounded scope, never a whole-repository sweep.",
    ),
    CaasProperty(
        "CAAS-P5",
        "Structured findings carry provenance",
        Enforceability.AUTOMATIC,
        "Every finding is structured and names the audit pass that produced it.",
    ),
    CaasProperty(
        "CAAS-P6",
        "Findings are durable in NeedFix",
        Enforceability.PARTIAL,
        "Sink binding and provenance are enforced here; binding to the canonical "
        "MCP-bound NeedFix store in src/aiworkhub/task_store.py is owned by "
        "another card and is not done from here.",
    ),
    CaasProperty(
        "CAAS-P7",
        "Upstream expansion correction",
        Enforceability.EXTERNAL,
        "The wrong expansion 'Continuous Automated Assurance System' lives in the "
        "separate UltrafastSecp256k1 README; this repository cannot fix it and "
        "only names it.",
    ),
)


@dataclass
class CaasComplianceState:
    """Facts about the current lifecycle state that the enforcer evaluates.

    Defaults describe a compliant state; a transition builds this object from
    the real facts it is about to act on. The boolean facts map one-to-one onto
    the automatically-enforced properties.

    Independently observed vs. attested — "by construction" means something
    different for each, so it is stated plainly here:

    - ``docs_expansion_ok`` (CAAS-P1) is *independently observed* when the state
      is built via :meth:`from_docs`, which scans the real doc text with
      :func:`scan_paths_for_forbidden_expansion` rather than trusting a caller
      flag.
    - ``enforced_by_construction``, ``audit_layer_read_only``,
      ``audit_scopes_bounded``, ``findings_have_provenance`` and
      ``findings_sink_bound`` (CAAS-P2..P6) are *attested self-reports*: the
      guarded caller assembles them from what it is about to do. The gate makes
      those facts refuse the transition when false; it does not itself re-derive
      them from the lifecycle. Independent observation of P2..P6 is a named
      residual, not something this gate proves.
    """

    docs_expansion_ok: bool = True
    enforced_by_construction: bool = True
    audit_layer_read_only: bool = True
    audit_scopes_bounded: bool = True
    findings_have_provenance: bool = True
    findings_sink_bound: bool = True

    @classmethod
    def from_docs(cls, paths: Iterable[Any], **overrides: bool) -> "CaasComplianceState":
        """Derive ``docs_expansion_ok`` by scanning repo-controlled doc paths."""

        hits = scan_paths_for_forbidden_expansion(paths)
        return cls(docs_expansion_ok=not hits, **overrides)


# Automatic checks keyed by property id. Each returns (compliant, detail).
# Properties absent from this table are not automatically checkable and are
# reported by name instead of gating any transition.
_AUTO_CHECKS: dict[str, Callable[[CaasComplianceState], tuple[bool, str]]] = {
    "CAAS-P1": lambda s: (
        s.docs_expansion_ok,
        "no forbidden expansion in repo-controlled docs"
        if s.docs_expansion_ok
        else "a forbidden CAAS expansion is present in repo-controlled docs",
    ),
    "CAAS-P2": lambda s: (
        s.enforced_by_construction,
        "enforcement runs via the lifecycle guard",
    ),
    "CAAS-P3": lambda s: (
        s.audit_layer_read_only,
        "audit layer has no repository write path"
        if s.audit_layer_read_only
        else "audit layer claims a repository write path",
    ),
    "CAAS-P4": lambda s: (
        s.audit_scopes_bounded,
        "audit scopes are bounded"
        if s.audit_scopes_bounded
        else "an audit scope is a whole-repository sweep",
    ),
    "CAAS-P5": lambda s: (
        s.findings_have_provenance,
        "findings carry provenance"
        if s.findings_have_provenance
        else "a finding is missing provenance",
    ),
    "CAAS-P6": lambda s: (
        s.findings_sink_bound,
        "a findings sink is bound (canonical NeedFix-store binding pending)"
        if s.findings_sink_bound
        else "no findings sink is bound",
    ),
}


@dataclass(frozen=True)
class PropertyResult:
    """The outcome of evaluating a single property against a state."""

    property: CaasProperty
    checked: bool
    compliant: bool
    detail: str


@dataclass
class ComplianceReport:
    """The result of evaluating every protocol property against a state."""

    results: list[PropertyResult] = field(default_factory=list)

    @property
    def compliant(self) -> bool:
        """True when no automatically-checked property is violated."""

        return all(r.compliant for r in self.results if r.checked)

    def violations(self) -> list[PropertyResult]:
        """Automatically-checked properties that failed."""

        return [r for r in self.results if r.checked and not r.compliant]

    def unenforceable(self) -> list[CaasProperty]:
        """Properties this repository cannot enforce automatically, by name."""

        return [r.property for r in self.results if not r.checked]

    def summary(self) -> str:
        enforced = [r.property.id for r in self.results if r.checked]
        gaps = [p.id for p in self.unenforceable()]
        return (
            f"compliant={self.compliant} "
            f"enforced={enforced} unenforceable={gaps}"
        )


class CaasEnforcer:
    """Evaluates a lifecycle state against the CAAS protocol properties."""

    def __init__(self, properties: Iterable[CaasProperty] = PROTOCOL_PROPERTIES):
        self._properties = tuple(properties)

    @property
    def properties(self) -> tuple[CaasProperty, ...]:
        return self._properties

    def evaluate(self, state: CaasComplianceState) -> ComplianceReport:
        results: list[PropertyResult] = []
        for prop in self._properties:
            check = _AUTO_CHECKS.get(prop.id)
            if check is None:
                # Not automatically checkable from this repository: report it by
                # name rather than letting a gap read as covered.
                results.append(
                    PropertyResult(prop, checked=False, compliant=True, detail=prop.note)
                )
                continue
            ok, detail = check(state)
            results.append(
                PropertyResult(prop, checked=True, compliant=ok, detail=detail)
            )
        return ComplianceReport(results)


class CaasComplianceError(RuntimeError):
    """Raised when a guarded lifecycle transition meets a non-compliant state."""

    def __init__(self, report: ComplianceReport):
        self.report = report
        violated = ", ".join(r.property.id for r in report.violations())
        super().__init__(f"CAAS compliance violated: {violated or '(none)'}")


#: The default enforcer used by :func:`enforce_caas` and :func:`assert_compliant`.
DEFAULT_ENFORCER = CaasEnforcer()


def assert_compliant(
    state: CaasComplianceState, enforcer: CaasEnforcer = DEFAULT_ENFORCER
) -> ComplianceReport:
    """Evaluate ``state`` and raise :class:`CaasComplianceError` if non-compliant."""

    report = enforcer.evaluate(state)
    if not report.compliant:
        raise CaasComplianceError(report)
    return report


def enforce_caas(
    state_getter: Callable[..., CaasComplianceState],
    enforcer: CaasEnforcer = DEFAULT_ENFORCER,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that runs CAAS enforcement automatically on every call.

    ``state_getter`` receives the wrapped call's arguments and returns the
    :class:`CaasComplianceState` to check. The wrapped lifecycle transition
    cannot execute against a non-compliant state: enforcement is structural, not
    a step someone remembers to run. This is what "CAAS by construction" means.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            state = state_getter(*args, **kwargs)
            assert_compliant(state, enforcer)
            return fn(*args, **kwargs)

        wrapper.__caas_guarded__ = True  # type: ignore[attr-defined]
        return wrapper

    return decorator


@dataclass(frozen=True)
class CorrectionOccurrence:
    """One reviewable, exact occurrence of forbidden text used as a correction."""

    path: str
    line: int
    column: int
    phrase: str
    reason: str


# Corrections are authorized by exact path, 1-based line and column, and exact
# content. A moved, re-cased, duplicated, or otherwise stale declaration matches
# nothing and therefore suppresses nothing.
ALLOWED_CORRECTION_SITES: tuple[CorrectionOccurrence, ...] = (
    CorrectionOccurrence(
        "README.md", 60, 41, "Continuous Automated Assurance System",
        "The README quotes the upstream expansion while correcting it.",
    ),
    CorrectionOccurrence(
        "docs/CAAS_PROTOCOL.md", 4, 42, "Continuous Automated Assurance System",
        "The protocol declares this expansion wrong.",
    ),
    CorrectionOccurrence(
        "docs/CAAS_PROTOCOL.md", 50, 39, "Continuous Automated Assurance System",
        "The named external residual identifies the upstream text to correct.",
    ),
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _relative_site(path: Path) -> str | None:
    """Return path as a repo-relative POSIX string, or None if outside."""

    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return None


def _forbidden_occurrences(text: str) -> list[tuple[str, int, int]]:
    """Return longest-attributed occurrences as (phrase, line, column)."""

    haystack = text.casefold()
    claimed: list[tuple[int, int]] = []
    matches: list[tuple[int, str, int, int]] = []
    for bad in sorted(FORBIDDEN_EXPANSIONS, key=len, reverse=True):
        needle = bad.casefold()
        start = haystack.find(needle)
        while start != -1:
            end = start + len(needle)
            if not any(s <= start and end <= e for s, e in claimed):
                claimed.append((start, end))
                line = text.count("\n", 0, start) + 1
                line_start = text.rfind("\n", 0, start) + 1
                matches.append((start, bad, line, start - line_start + 1))
            start = haystack.find(needle, start + 1)
    return [(bad, line, column) for _, bad, line, column in sorted(matches)]


def scan_text_for_forbidden_expansion(text: str) -> list[str]:
    """Return each forbidden expansion present in text.

    Matching is case-insensitive. Overlapping variants are attributed to the
    longest expansion occupying a span.
    """

    present = {bad for bad, _, _ in _forbidden_occurrences(text)}
    return [
        bad
        for bad in sorted(FORBIDDEN_EXPANSIONS, key=len, reverse=True)
        if bad in present
    ]


def _declared_locators(relative_path: str | None) -> set[tuple[str, int, int]]:
    """Return valid unique locators; invalid declarations fail closed."""

    candidates = [
        site
        for site in ALLOWED_CORRECTION_SITES
        if isinstance(site, CorrectionOccurrence) and site.path == relative_path
    ]
    counts: dict[tuple[str, int, int], int] = {}
    for site in candidates:
        locator = (site.phrase, site.line, site.column)
        valid = (
            site.path == Path(site.path).as_posix()
            and not Path(site.path).is_absolute()
            and ".." not in Path(site.path).parts
            and site.phrase in FORBIDDEN_EXPANSIONS
            and site.line > 0
            and site.column > 0
            and bool(site.reason.strip())
        )
        if valid:
            counts[locator] = counts.get(locator, 0) + 1
    return {locator for locator, count in counts.items() if count == 1}


def scan_paths_for_forbidden_expansion(paths: Iterable[Any]) -> dict[str, list[str]]:
    """Scan paths and report every unmatched forbidden expansion occurrence."""

    hits: dict[str, list[str]] = {}
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        allowed = _declared_locators(_relative_site(path))
        lines = text.splitlines()
        reported = [
            bad
            for bad, line, column in _forbidden_occurrences(text)
            if (bad, line, column) not in allowed
            or lines[line - 1][column - 1 : column - 1 + len(bad)] != bad
        ]
        if reported:
            hits[str(path)] = reported
    return hits
