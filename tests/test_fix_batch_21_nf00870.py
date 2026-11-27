"""Fix batch 21 regressions: NF-2026-00870."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import declared_invariants as di

_REPO = Path(__file__).resolve().parents[1]
_MANIFEST = _REPO / ".aiworkhub" / "config" / "development_rules.json"


def _manifest():
    from aiworkhub.development_rules import parse_manifest_bytes

    return parse_manifest_bytes(_MANIFEST.read_bytes())


# --------------------------------------------------------------------------- #
# NF-2026-00870: declared JS/TS scope is reported unscanned; detector map total
# --------------------------------------------------------------------------- #


def test_js_ts_scope_of_a_detected_rule_is_reported_unscanned_not_covered():
    coverage = di.rule_detector_coverage(_manifest())

    assert coverage["scanned_languages"] == ["python"]
    single = [
        row for row in coverage["unscanned_languages"] if row["rule"] == "single_definition"
    ]
    # Every executing single_definition detector reads Python only.
    assert {row["forbids"] for row in single} == set(di.RULE_DETECTORS["single_definition"])
    for row in single:
        assert {"javascript", "typescript"} <= set(row["languages"])
        assert "python" not in row["languages"]
    assert "single_definition" not in coverage["rules_fully_covered"]
    assert "single_definition" in coverage["rules_partly_covered"]
    detected = {row["forbids"]: row for row in coverage["detected"] if row["rule"] == "single_definition"}
    assert detected["copied_helper"]["scanned_languages"] == ["python"]


def test_an_executing_detector_outside_the_manifest_without_a_reason_is_refused(monkeypatch):
    manifest = _manifest()
    reasons = dict(di._DETECTORS_WITHOUT_A_DECLARED_RULE)
    reasons.pop("sqlite_context_managers_close")
    monkeypatch.setattr(di, "_DETECTORS_WITHOUT_A_DECLARED_RULE", reasons)

    with pytest.raises(ValueError, match="answer to no manifest rule"):
        di.rule_detector_coverage(manifest)


def test_every_executing_detector_is_mapped_or_explained_exactly_once():
    mapped = {name for tokens in di.RULE_DETECTORS.values() for name in tokens.values()}
    explained = set(di._DETECTORS_WITHOUT_A_DECLARED_RULE)
    assert not mapped & explained
    assert mapped | explained == set(di.INVARIANT_NAMES)


def test_detected_rule_scoped_only_to_unscanned_languages_is_not_covered(monkeypatch):
    manifest = _manifest()
    real = di._rule_languages

    def languages(rule, owner):
        if rule.id == "single_definition":
            return ("javascript", "typescript")
        return real(rule, owner)

    monkeypatch.setattr(di, "_rule_languages", languages)
    coverage = di.rule_detector_coverage(manifest)

    assert coverage["scanned_languages"] == ["python"]
    assert "single_definition" in coverage["rules_not_covered"]
    assert "single_definition" not in coverage["rules_partly_covered"]
    assert "single_definition" not in coverage["rules_fully_covered"]
    assert not [row for row in coverage["detected"] if row["rule"] == "single_definition"]
    rows = [row for row in coverage["unscanned_languages"] if row["rule"] == "single_definition"]
    assert {row["forbids"] for row in rows} == set(di.RULE_DETECTORS["single_definition"])
    assert all(row["languages"] == ["javascript", "typescript"] for row in rows)
    # The obligation is still counted: moving it out of "detected" never shrinks the total.
    assert coverage["obligations"] == len(di._obligations(manifest))
