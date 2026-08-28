"""Tests for CAAS (Continuous Audit as a Service) enforcement by construction.

These tests are hermetic: they import only ``aiworkhub.caas_enforcement`` and
stdlib, construct no ProcessManager, and launch no process.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import aiworkhub.caas_enforcement as caas_enforcement
from aiworkhub.caas_enforcement import (
    ALLOWED_CORRECTION_SITES,
    CAAS_EXPANSION,
    FORBIDDEN_EXPANSIONS,
    CaasComplianceError,
    CaasComplianceState,
    CaasEnforcer,
    CorrectionOccurrence,
    Enforceability,
    assert_compliant,
    enforce_caas,
    scan_paths_for_forbidden_expansion,
    scan_text_for_forbidden_expansion,
)

#: Repository root, so the discrimination tests scan the real correction docs.
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_canonical_expansion_is_continuous_audit_as_a_service():
    assert CAAS_EXPANSION == "Continuous Audit as a Service"
    assert "Continuous Automated Assurance System" in FORBIDDEN_EXPANSIONS


def test_compliant_state_passes_evaluation():
    report = CaasEnforcer().evaluate(CaasComplianceState())
    assert report.compliant
    assert not report.violations()


def test_by_construction_guard_catches_non_compliant_without_manual_check():
    """The transition is called; no checker is invoked by hand, yet the
    non-compliant state is caught. This is the "by construction" property."""

    calls: list[str] = []

    def state_getter(state: CaasComplianceState) -> CaasComplianceState:
        return state

    @enforce_caas(state_getter)
    def promote(state: CaasComplianceState) -> str:
        # Body must never run for a non-compliant state.
        calls.append("promoted")
        return "promoted"

    # A read-only violation (P3): the audit layer must not be able to write.
    bad_state = CaasComplianceState(audit_layer_read_only=False)

    with pytest.raises(CaasComplianceError) as excinfo:
        promote(bad_state)

    # The transition body never executed, and nobody called the enforcer.
    assert calls == []
    assert "CAAS-P3" in str(excinfo.value)
    assert any(r.property.id == "CAAS-P3" for r in excinfo.value.report.violations())


def test_guard_allows_a_compliant_transition():
    @enforce_caas(lambda state: state)
    def promote(state: CaasComplianceState) -> str:
        return "promoted"

    assert promote(CaasComplianceState()) == "promoted"


def test_assert_compliant_raises_on_missing_provenance():
    state = CaasComplianceState(findings_have_provenance=False)
    with pytest.raises(CaasComplianceError):
        assert_compliant(state)


def test_doc_scan_detects_forbidden_expansion(tmp_path):
    good = tmp_path / "good.md"
    good.write_text("CAAS is Continuous Audit as a Service.", encoding="utf-8")
    bad = tmp_path / "bad.md"
    bad.write_text("CAAS is a Continuous Automated Assurance System.", encoding="utf-8")

    hits = scan_paths_for_forbidden_expansion([good, bad])
    assert str(good) not in hits
    assert hits[str(bad)] == ["Continuous Automated Assurance System"]

    # And the derived state fails P1 automatically.
    state = CaasComplianceState.from_docs([good, bad])
    report = CaasEnforcer().evaluate(state)
    assert not report.compliant
    assert any(r.property.id == "CAAS-P1" for r in report.violations())


def test_report_names_unenforceable_properties_by_name():
    report = CaasEnforcer().evaluate(CaasComplianceState())
    unenforceable = report.unenforceable()
    ids = {p.id for p in unenforceable}

    # P7 cannot be enforced from this repository; it must be named, not hidden.
    assert "CAAS-P7" in ids
    p7 = next(p for p in unenforceable if p.id == "CAAS-P7")
    assert p7.enforceability is Enforceability.EXTERNAL
    assert "UltrafastSecp256k1" in p7.note

    # The report stays compliant despite the external gap (it cannot gate here).
    assert report.compliant


def test_partial_property_p6_names_the_needfix_store_residual():
    prop = next(p for p in CaasEnforcer().properties if p.id == "CAAS-P6")
    assert prop.enforceability is Enforceability.PARTIAL
    assert "task_store.py" in prop.note


# --- Discrimination: the exemption is DECLARED, not inferred. -----------------
#
# An occurrence of the forbidden expansion is a violation unless the
# (repo-relative path, line, column, exact phrase) occurrence is declared. There is no
# cue list and no proximity window: intent is not guessed from nearby words.
# Both directions are tested, plus the two cases that prove the design — a doc
# that looks like a correction but is not declared must FAIL, and a case-variant
# must FAIL.

BAD = "Continuous Automated Assurance System"


def test_allowlist_entries_are_exact_reviewable_occurrences():
    assert [(site.path, site.line, site.column) for site in ALLOWED_CORRECTION_SITES] == [
        ("README.md", 60, 41),
        ("docs/CAAS_PROTOCOL.md", 4, 42),
        ("docs/CAAS_PROTOCOL.md", 50, 39),
    ]
    assert all(site.phrase == BAD and site.reason.strip() for site in ALLOWED_CORRECTION_SITES)


def test_allowlisted_path_reports_additional_occurrence(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    readme = repo / "README.md"
    declared_line = "x" * 40 + BAD
    readme.write_text(
        "\n" * 59 + declared_line + "\nAdditional misuse: " + BAD,
        encoding="utf-8",
    )
    monkeypatch.setattr(caas_enforcement, "_REPO_ROOT", repo)

    assert scan_paths_for_forbidden_expansion([readme]) == {str(readme): [BAD]}


def test_duplicate_declaration_fails_closed(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    readme = repo / "README.md"
    readme.write_text("\n" * 59 + "x" * 40 + BAD, encoding="utf-8")
    declaration = CorrectionOccurrence("README.md", 60, 41, BAD, "correction")
    monkeypatch.setattr(caas_enforcement, "_REPO_ROOT", repo)
    monkeypatch.setattr(
        caas_enforcement, "ALLOWED_CORRECTION_SITES", (declaration, declaration)
    )

    assert scan_paths_for_forbidden_expansion([readme]) == {str(readme): [BAD]}


def test_repo_correction_docs_are_not_reported_as_violations():
    readme = REPO_ROOT / "README.md"
    protocol = REPO_ROOT / "docs" / "CAAS_PROTOCOL.md"

    assert readme.read_text(encoding="utf-8").count(BAD) == 1
    assert protocol.read_text(encoding="utf-8").count(BAD) == 2

    hits = scan_paths_for_forbidden_expansion([readme, protocol])
    assert hits == {}, f"declared correction docs wrongly flagged: {hits}"

    state = CaasComplianceState.from_docs([readme, protocol])
    assert CaasEnforcer().evaluate(state).compliant


def test_genuine_misuse_in_an_undeclared_file_is_flagged(tmp_path):
    """The forbidden expansion in a file that is NOT a declared correction site
    is flagged even when it quotes and condemns the phrase exactly as the real
    docs do. This proves the exemption is DECLARED, not inferred from the words:
    the identical correction sentence fails purely because the file is not on the
    allowlist."""

    misuse = tmp_path / "brochure.md"
    misuse.write_text(
        'The expansion "Continuous Automated Assurance System" is wrong; the '
        "canonical expansion is Continuous Audit as a Service.",
        encoding="utf-8",
    )
    assert scan_paths_for_forbidden_expansion([misuse]) == {str(misuse): [BAD]}

    # And the derived state fails P1 automatically.
    state = CaasComplianceState.from_docs([misuse])
    report = CaasEnforcer().evaluate(state)
    assert not report.compliant
    assert any(r.property.id == "CAAS-P1" for r in report.violations())


def test_unquoted_assertion_is_flagged():
    """Plain prose asserting the forbidden expansion is a violation at text level,
    where there is no file identity and therefore no possible exemption."""

    unquoted = "CAAS means Continuous Automated Assurance System in our stack."
    assert scan_text_for_forbidden_expansion(unquoted) == [BAD]


def test_quoted_with_not_in_paragraph_but_undeclared_still_fails(tmp_path):
    """The exact case the old cue list let through: the word "not" sits in the
    same paragraph but the phrase is not condemned, and the file is not a declared
    site. It must FAIL — no proximity heuristic can exempt it now, because there
    is no heuristic."""

    text = (
        'We do not cut corners: our platform is a "Continuous Automated '
        'Assurance System" trusted across every team.'
    )
    # Text level: reported (no file, no exemption).
    assert scan_text_for_forbidden_expansion(text) == [BAD]

    # Path level: an undeclared file, so still reported despite the nearby "not".
    doc = tmp_path / "marketing.md"
    doc.write_text(text, encoding="utf-8")
    assert scan_paths_for_forbidden_expansion([doc]) == {str(doc): [BAD]}


def test_case_variant_forbidden_expansion_still_fails(tmp_path):
    """A re-cased forbidden expansion must not slip through: matching is
    case-insensitive on both sides. This closes the case-variant hole where a
    lowercased phrase failed open."""

    lowered = BAD.lower()
    assert lowered != BAD  # a genuine case variant, not the canonical spelling

    assert scan_text_for_forbidden_expansion(
        f"CAAS means {lowered} in our stack."
    ) == [BAD]

    doc = tmp_path / "lower.md"
    doc.write_text(f"a {lowered} for teams", encoding="utf-8")
    assert scan_paths_for_forbidden_expansion([doc]) == {str(doc): [BAD]}
