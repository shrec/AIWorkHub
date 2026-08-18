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


# In the CAAS documentation the owner-canonical phrase is everywhere, so the
# forbidden expansion legitimately appears in this repository's own correction
# text. Which appearances are corrections is DECLARED here, not inferred from
# nearby words: an occurrence of a forbidden expansion is a violation UNLESS the
# ``(repository-relative POSIX path, exact phrase)`` pair is listed below. There
# is no cue list and no proximity window — intent is not a substring question.
#
# Each entry records why the site is allowed. Adding a site is a deliberate,
# reviewable act, which is exactly the property we want: exempting a misuse must
# require someone to write down that they are doing it. Keying on the phrase (not
# the surrounding sentence) means the exemption survives the document being
# rewritten around the quote.
ALLOWED_CORRECTION_SITES: dict[tuple[str, str], str] = {
    ("README.md", "Continuous Automated Assurance System"): (
        "The README quotes the forbidden expansion in order to correct it to the "
        "owner-canonical 'Continuous Audit as a Service'."
    ),
    ("docs/CAAS_PROTOCOL.md", "Continuous Automated Assurance System"): (
        "The protocol contract names the forbidden expansion in order to declare "
        "it wrong and point at the UltrafastSecp256k1 README that carries it."
    ),
}

#: Repository root, used to key a scanned file against ALLOWED_CORRECTION_SITES.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _relative_site(path: Path) -> str | None:
    """Return ``path`` as a repo-relative POSIX string, or ``None`` if outside.

    Only files inside this repository can be declared correction sites; anything
    else (a temp file, another checkout) can never match the allowlist and so is
    always subject to the check.
    """

    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return None


def scan_text_for_forbidden_expansion(text: str) -> list[str]:
    """Return every forbidden CAAS expansion present in ``text``.

    This is pure detection with no notion of intent. Matching is
    case-insensitive on both sides, so a lowercased or otherwise re-cased
    forbidden phrase cannot slip through. Text handed here has no file identity,
    so nothing is exempt at this level; the declared-correction allowlist is
    applied one level up in :func:`scan_paths_for_forbidden_expansion`, where the
    file is known. This keeps the correction docs passing without teaching the
    check to guess which sentences mean to condemn the phrase.

    Overlapping variants are attributed to the longest forbidden expansion that
    occupies a span: a shorter phrase that is only ever a substring of a longer
    one (e.g. "Continuous Automated Assurance" inside "…Assurance System") is not
    counted separately, so one wrong phrase yields one finding.
    """

    # Compare case-folded throughout so case variants match; report the canonical
    # spelling. Longest first, so a longer phrase claims its character span before
    # any shorter substring is evaluated.
    haystack = text.casefold()
    order = sorted(FORBIDDEN_EXPANSIONS, key=len, reverse=True)
    claimed: list[tuple[int, int]] = []
    found: list[str] = []
    for bad in order:
        needle = bad.casefold()
        present = False
        start = haystack.find(needle)
        while start != -1:
            end = start + len(needle)
            covered = any(s <= start and end <= e for s, e in claimed)
            if not covered:
                claimed.append((start, end))
                present = True
            start = haystack.find(needle, start + 1)
        if present:
            found.append(bad)
    return found


def scan_paths_for_forbidden_expansion(paths: Iterable[Any]) -> dict[str, list[str]]:
    """Scan repo-controlled doc paths, returning {path: [forbidden expansions]}.

    An occurrence is dropped only when its ``(repo-relative path, phrase)`` pair
    is a declared correction site in :data:`ALLOWED_CORRECTION_SITES`. Nothing
    else is exempt: a forbidden expansion in any file not on that list — quoted,
    condemned, or otherwise — is reported, full stop.
    """

    hits: dict[str, list[str]] = {}
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = _relative_site(path)
        reported = [
            bad
            for bad in scan_text_for_forbidden_expansion(text)
            if (rel, bad) not in ALLOWED_CORRECTION_SITES
        ]
        if reported:
            hits[str(path)] = reported
    return hits
