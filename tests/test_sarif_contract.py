"""Tests for aiworkhub.sarif_contract."""

from __future__ import annotations

import pytest

from aiworkhub.sarif_contract import (
    InvalidFindingError,
    ScopeProvenance,
    UnsafePathError,
    VcsProvenance,
    build_sarif_log,
    compute_fingerprint,
    finding_to_sarif_result,
    is_safe_relative_path,
    is_safe_scope_root,
)


def test_empty_clean_run_has_valid_empty_results() -> None:
    scope = ScopeProvenance(scope_root=".")
    log = build_sarif_log([], scope)

    assert log["version"] == "2.1.0"
    run = log["runs"][0]
    assert run["results"] == []
    assert run["properties"]["scopeRoot"] == "."
    assert run["properties"]["analyzedFindingCount"] == 0


def test_fingerprint_deterministic_across_prose_reordering() -> None:
    finding_a = {
        "rule_id": "RULE1",
        "path": "src/foo.py",
        "line": 10,
        "column": 5,
        "severity": "high",
        "message": "first version of the message",
    }
    finding_b = {
        "severity": "high",
        "message": "a completely reworded message describing same issue",
        "column": 5,
        "line": 10,
        "path": "src/foo.py",
        "rule_id": "RULE1",
    }
    assert compute_fingerprint(finding_a) == compute_fingerprint(finding_b)


def test_fingerprint_excludes_severity() -> None:
    base = {"rule_id": "RULE1", "path": "src/foo.py", "line": 1, "column": 1}
    fp_high = compute_fingerprint({**base, "severity": "high"})
    fp_low = compute_fingerprint({**base, "severity": "low"})
    assert fp_high == fp_low


def test_fingerprint_fails_closed_on_traversal_path() -> None:
    finding = {"rule_id": "RULE1", "path": "../../etc/passwd", "line": 1, "column": 1}
    with pytest.raises(UnsafePathError):
        compute_fingerprint(finding)


def test_fingerprint_fails_closed_on_absolute_path() -> None:
    finding = {"rule_id": "RULE1", "path": "/etc/passwd", "line": 1, "column": 1}
    with pytest.raises(UnsafePathError):
        compute_fingerprint(finding)


def test_finding_to_sarif_result_rejects_traversal_path() -> None:
    finding = {"rule_id": "RULE1", "path": "../secret.txt", "line": 1, "column": 1}
    with pytest.raises(UnsafePathError):
        finding_to_sarif_result(finding)


def test_synthetic_location_marker_for_locationless_finding() -> None:
    finding = {"rule_id": "RULE1", "severity": "medium", "message": "no location"}
    result = finding_to_sarif_result(finding)
    assert result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == (
        "synthetic:no-location"
    )
    assert result["properties"]["syntheticLocation"] is True


def test_sensitive_fields_excluded_from_sarif_result() -> None:
    finding = {
        "rule_id": "RULE1",
        "path": "src/foo.py",
        "line": 1,
        "column": 1,
        "poc": "exploit code here",
        "secret": "api-key-12345",
        "raw_exploit": "malicious payload",
        "payload": "more sensitive data",
    }
    result = finding_to_sarif_result(finding)
    serialized = str(result)
    assert "exploit code here" not in serialized
    assert "api-key-12345" not in serialized
    assert "malicious payload" not in serialized
    assert "more sensitive data" not in serialized


def test_vcs_provenance_included_in_run() -> None:
    scope = ScopeProvenance(
        scope_root=".",
        vcs=VcsProvenance(
            repository_uri="https://example.com/repo.git",
            revision_id="abc123",
            branch="main",
        ),
    )
    log = build_sarif_log([], scope)
    run = log["runs"][0]
    assert run["versionControlProvenance"][0]["repositoryUri"] == (
        "https://example.com/repo.git"
    )
    assert run["versionControlProvenance"][0]["revisionId"] == "abc123"
    assert run["versionControlProvenance"][0]["branch"] == "main"


def test_is_safe_relative_path_rejects_traversal_and_absolute() -> None:
    assert is_safe_relative_path("src/foo.py") is True
    assert is_safe_relative_path("../etc/passwd") is False
    assert is_safe_relative_path("/etc/passwd") is False
    assert is_safe_relative_path("") is False
    assert is_safe_relative_path("C:\\Windows") is False


def test_scope_root_validation_accepts_dot_and_safe_paths() -> None:
    assert is_safe_scope_root(".") is True
    assert is_safe_scope_root("src/pkg") is True
    assert is_safe_scope_root("../outside") is False
    assert is_safe_scope_root("/abs/path") is False


def test_scope_provenance_rejects_unsafe_scope_root() -> None:
    with pytest.raises(UnsafePathError):
        ScopeProvenance(scope_root="../escape")


def test_missing_rule_id_raises_invalid_finding_error() -> None:
    finding = {"path": "src/foo.py", "line": 1, "column": 1}
    with pytest.raises(InvalidFindingError):
        compute_fingerprint(finding)


def test_non_positive_line_column_rejected() -> None:
    with pytest.raises(InvalidFindingError):
        compute_fingerprint({"rule_id": "R", "path": "a.py", "line": 0, "column": 1})
    with pytest.raises(InvalidFindingError):
        compute_fingerprint({"rule_id": "R", "path": "a.py", "line": 1, "column": -1})
    with pytest.raises(InvalidFindingError):
        compute_fingerprint({"rule_id": "R", "path": "a.py", "line": "nan", "column": 1})


def test_finding_id_disambiguates_locationless_findings() -> None:
    finding_1 = {"rule_id": "RULE1", "finding_id": "instance-1", "severity": "low"}
    finding_2 = {"rule_id": "RULE1", "finding_id": "instance-2", "severity": "low"}
    finding_none = {"rule_id": "RULE1", "severity": "low"}

    fp1 = compute_fingerprint(finding_1)
    fp2 = compute_fingerprint(finding_2)
    fp_none = compute_fingerprint(finding_none)

    assert fp1 != fp2
    assert fp1 != fp_none
    assert fp2 != fp_none


def test_results_sorted_by_fingerprint_regardless_of_input_order() -> None:
    scope = ScopeProvenance(scope_root=".")
    finding_a = {"rule_id": "AAA", "path": "a.py", "line": 1, "column": 1}
    finding_b = {"rule_id": "ZZZ", "path": "b.py", "line": 2, "column": 2}
    finding_c = {"rule_id": "MMM", "path": "c.py", "line": 3, "column": 3}

    log_order1 = build_sarif_log([finding_a, finding_b, finding_c], scope)
    log_order2 = build_sarif_log([finding_c, finding_a, finding_b], scope)
    log_order3 = build_sarif_log([finding_b, finding_c, finding_a], scope)

    assert log_order1 == log_order2 == log_order3


def test_build_sarif_log_deterministic_under_fingerprint_collision_order() -> None:
    scope = ScopeProvenance(scope_root=".")
    finding_a = {
        "rule_id": "RULE1",
        "path": "src/foo.py",
        "line": 1,
        "column": 1,
        "severity": "high",
        "message": "message a",
    }
    finding_b = {
        "rule_id": "RULE1",
        "path": "src/foo.py",
        "line": 1,
        "column": 1,
        "severity": "low",
        "message": "message b",
    }

    log_ab = build_sarif_log([finding_a, finding_b], scope)
    log_ba = build_sarif_log([finding_b, finding_a], scope)

    assert log_ab == log_ba


def test_rule_id_rejects_non_string_types() -> None:
    for bad_rule_id in ({}, [], 123, True, 1.5):
        with pytest.raises(InvalidFindingError):
            compute_fingerprint(
                {"rule_id": bad_rule_id, "path": "a.py", "line": 1, "column": 1}
            )


def test_line_column_reject_bool_and_float() -> None:
    with pytest.raises(InvalidFindingError):
        compute_fingerprint({"rule_id": "R", "path": "a.py", "line": True, "column": 1})
    with pytest.raises(InvalidFindingError):
        compute_fingerprint({"rule_id": "R", "path": "a.py", "line": 1.2, "column": 1})
    with pytest.raises(InvalidFindingError):
        compute_fingerprint({"rule_id": "R", "path": "a.py", "line": 1, "column": False})


def test_finding_id_rejects_non_string_types() -> None:
    for bad_finding_id in ({}, [], 123, True):
        with pytest.raises(InvalidFindingError):
            compute_fingerprint(
                {
                    "rule_id": "R",
                    "path": "a.py",
                    "line": 1,
                    "column": 1,
                    "finding_id": bad_finding_id,
                }
            )


def test_path_rejects_non_string_types() -> None:
    with pytest.raises(InvalidFindingError):
        compute_fingerprint({"rule_id": "R", "path": 123, "line": 1, "column": 1})
    with pytest.raises(InvalidFindingError):
        finding_to_sarif_result(
            {"rule_id": "R", "path": ["a.py"], "line": 1, "column": 1}
        )


def test_rule_id_emitted_normalized_and_matches_fingerprint_identity() -> None:
    padded = {"rule_id": " R ", "path": "a.py", "line": 1, "column": 1}
    trimmed = {"rule_id": "R", "path": "a.py", "line": 1, "column": 1}

    result_padded = finding_to_sarif_result(padded)
    result_trimmed = finding_to_sarif_result(trimmed)

    assert result_padded["ruleId"] == "R"
    assert result_trimmed["ruleId"] == "R"
    assert (
        result_padded["partialFingerprints"]["structuralIdentity/v1"]
        == result_trimmed["partialFingerprints"]["structuralIdentity/v1"]
        == compute_fingerprint(trimmed)
    )


def test_is_safe_relative_path_no_longer_accepts_scope_root_kwarg() -> None:
    with pytest.raises(TypeError):
        is_safe_relative_path("src/foo.py", scope_root=".")  # type: ignore[call-arg]


def test_vcs_provenance_rejects_empty_and_whitespace_fields() -> None:
    with pytest.raises(InvalidFindingError):
        VcsProvenance(repository_uri="")
    with pytest.raises(InvalidFindingError):
        VcsProvenance(revision_id="   ")
    with pytest.raises(InvalidFindingError):
        VcsProvenance(branch="\t\n")


def test_vcs_provenance_rejects_control_characters() -> None:
    with pytest.raises(InvalidFindingError):
        VcsProvenance(repository_uri="https://example.com/repo.git\x00evil")
    with pytest.raises(InvalidFindingError):
        VcsProvenance(revision_id="abc123\ninjected")
    with pytest.raises(InvalidFindingError):
        VcsProvenance(branch="main\x7f")


def test_vcs_provenance_rejects_non_string_fields() -> None:
    with pytest.raises(InvalidFindingError):
        VcsProvenance(repository_uri=123)  # type: ignore[arg-type]


def test_vcs_provenance_rejects_overlong_field() -> None:
    with pytest.raises(InvalidFindingError):
        VcsProvenance(revision_id="a" * 2049)


def test_vcs_provenance_accepts_legitimate_non_scheme_reference() -> None:
    vcs = VcsProvenance(repository_uri="git@example.com:org/repo.git", revision_id="abc123")
    assert vcs.repository_uri == "git@example.com:org/repo.git"
