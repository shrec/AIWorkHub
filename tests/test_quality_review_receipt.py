"""Contract tests for the extracted quality-review receipt module.

``quality_review_receipt`` was split out of ``process_launcher`` as a pure
move.  These tests pin the two things that could silently rot afterwards: the
exact fail-closed schema the receipt must satisfy, and the fact that
``process_launcher`` still exposes the very same function objects rather than a
divergent copy.
"""

import pytest

from aiworkhub import process_launcher, quality_review_receipt, quality_reviewer, task_store
from aiworkhub.worker_workspace import WorkspaceError

PROVIDER = "codex_cli"
SHA_A = "a" * 64
SHA_B = "b" * 64


def _finding(**overrides):
    finding = {
        "summary": "unchecked index",
        "evidence": "process_launcher.py:120",
        "severity": "high",
        "disposition": "defect",
        "actionable": True,
    }
    finding.update(overrides)
    return finding


def _receipt(**overrides):
    receipt = {
        "schema_id": quality_reviewer.RECEIPT_SCHEMA_ID,
        "packet_sha256": SHA_A,
        "submission_id": SHA_B,
        "target": {"request_id": "req-target", "task_id": "T-1", "claim_epoch": 3},
        "reviewer": {"request_id": "req-rev", "task_id": "T-2", "provider": PROVIDER},
        "report": {
            "lens": "correctness",
            "provider": PROVIDER,
            "read_only": True,
            "can_mutate_repo": False,
            "findings": [_finding()],
        },
        "authority": {
            "process_identity_verified": True,
            "audit_verified": True,
            "terminal_state": "review_ready",
        },
        "physical_submission_count": 1,
        "logical_submission_count": 1,
    }
    receipt.update(overrides)
    return receipt


def _enforce(receipt, provider=PROVIDER):
    return quality_review_receipt._enforce_quality_review_receipt_schema(receipt, provider)


def _reason(excinfo):
    return str(excinfo.value)


# --------------------------------------------------------------------------
# The move itself: process_launcher must expose the same objects, not copies.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "_enforce_quality_review_receipt_schema",
        "_verified_quality_review_receipt",
    ],
)
def test_process_launcher_reexports_the_same_object(name):
    assert getattr(process_launcher, name) is getattr(quality_review_receipt, name)


def test_bool_safe_int_rule_stays_the_stores_own_authority():
    # A private reimplementation here would admit an epoch the store rejects.
    assert quality_review_receipt._is_bool_safe_int is task_store.is_bool_safe_int
    assert process_launcher._is_bool_safe_int is task_store.is_bool_safe_int


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_canonical_receipt_is_accepted_and_returned_unchanged():
    receipt = _receipt()
    assert _enforce(receipt) is receipt


def test_findings_may_be_empty():
    receipt = _receipt()
    receipt["report"]["findings"] = []
    assert _enforce(receipt) is receipt


def test_optional_finding_keys_are_carried_through():
    receipt = _receipt()
    receipt["report"]["findings"] = [_finding(category="correctness", id="F-1")]
    assert _enforce(receipt) is receipt


# --------------------------------------------------------------------------
# Exhaustive key sets: unknown keys and missing keys both fail closed.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "section, reason",
    [
        (None, "quality_review_receipt_top_level_keys_invalid"),
        ("target", "quality_review_target_keys_invalid"),
        ("reviewer", "quality_review_reviewer_keys_invalid"),
        ("report", "quality_review_report_keys_invalid"),
        ("authority", "quality_review_authority_keys_invalid"),
    ],
)
def test_unknown_key_is_refused_at_every_level(section, reason):
    receipt = _receipt()
    target = receipt if section is None else receipt[section]
    target["smuggled"] = "x"
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == reason


@pytest.mark.parametrize(
    "section, key, reason",
    [
        (None, "submission_id", "quality_review_receipt_top_level_keys_invalid"),
        ("target", "claim_epoch", "quality_review_target_keys_invalid"),
        ("reviewer", "provider", "quality_review_reviewer_keys_invalid"),
        ("report", "findings", "quality_review_report_keys_invalid"),
        ("authority", "audit_verified", "quality_review_authority_keys_invalid"),
    ],
)
def test_missing_key_is_refused_at_every_level(section, key, reason):
    receipt = _receipt()
    target = receipt if section is None else receipt[section]
    del target[key]
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == reason


@pytest.mark.parametrize("section", ["target", "reviewer", "report", "authority"])
def test_non_dict_section_is_refused(section):
    receipt = _receipt(**{section: ["not", "a", "dict"]})
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == "quality_review_receipt_shape_invalid"


# --------------------------------------------------------------------------
# Identity: schema id and the two 64-hex digests.
# --------------------------------------------------------------------------


def test_foreign_schema_id_is_refused():
    receipt = _receipt(schema_id="aiworkhub.something_else.v1")
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == "quality_review_receipt_schema_mismatch"


@pytest.mark.parametrize(
    "value",
    ["A" * 64, "a" * 63, "a" * 65, "", None, 0, "g" * 64, " " + "a" * 63],
)
@pytest.mark.parametrize(
    "key, reason",
    [
        ("packet_sha256", "quality_review_packet_sha256_invalid"),
        ("submission_id", "quality_review_submission_id_invalid"),
    ],
)
def test_digest_must_be_lowercase_64_hex(key, reason, value):
    receipt = _receipt(**{key: value})
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == reason


# --------------------------------------------------------------------------
# Bool-safe integers: True is not 1 here.
# --------------------------------------------------------------------------


def test_bool_claim_epoch_is_refused():
    receipt = _receipt()
    receipt["target"]["claim_epoch"] = True
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == "quality_review_claim_epoch_invalid"


@pytest.mark.parametrize(
    "key, reason",
    [
        ("physical_submission_count", "quality_review_physical_submission_count_invalid"),
        ("logical_submission_count", "quality_review_logical_submission_count_invalid"),
    ],
)
@pytest.mark.parametrize("value", [True, 0, 2, "1", None])
def test_submission_counts_must_be_bool_safe_one(key, reason, value):
    receipt = _receipt(**{key: value})
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == reason


# --------------------------------------------------------------------------
# Provider binding: the observed adapter identity is authoritative.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "section, reason",
    [
        ("reviewer", "quality_review_reviewer_provider_mismatch"),
        ("report", "quality_review_report_provider_mismatch"),
    ],
)
def test_provider_must_match_the_observed_adapter(section, reason):
    receipt = _receipt()
    receipt[section]["provider"] = "some_other_cli"
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == reason


# --------------------------------------------------------------------------
# Findings.
# --------------------------------------------------------------------------


def test_findings_must_be_a_list():
    receipt = _receipt()
    receipt["report"]["findings"] = {"not": "a list"}
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == "quality_review_report_findings_invalid"


def test_non_dict_finding_names_its_index():
    receipt = _receipt()
    receipt["report"]["findings"] = [_finding(), "nope"]
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == "quality_review_finding_1_invalid"


@pytest.mark.parametrize("missing", sorted({"actionable", "evidence", "severity", "summary"}))
def test_finding_below_the_required_floor_is_refused(missing):
    finding = _finding()
    del finding[missing]
    receipt = _receipt()
    receipt["report"]["findings"] = [finding]
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == "quality_review_finding_0_keys_invalid"


def test_finding_above_the_canonical_ceiling_is_refused():
    receipt = _receipt()
    receipt["report"]["findings"] = [_finding(not_a_canonical_key="x")]
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == "quality_review_finding_0_keys_invalid"


def test_required_floor_is_derived_from_the_canonical_vocabularies():
    # The floor must stay DERIVED so it cannot drift from the reviewer
    # boundary: canonical required emit-set minus everything the input schema
    # declares optional.
    expected = quality_reviewer.QUALITY_REVIEW_FINDING_REQUIRED_KEYS - (
        quality_reviewer.QUALITY_REVIEW_FINDING_INPUT_KEYS
        - quality_reviewer.QUALITY_REVIEW_FINDING_INPUT_REQUIRED_KEYS
    )
    assert quality_review_receipt._QUALITY_REVIEW_FINDING_RECEIPT_REQUIRED_KEYS == expected
    # The floor is a genuine subset of the ceiling, or nothing could pass.
    assert expected <= quality_reviewer.QUALITY_REVIEW_FINDING_KEYS


def test_category_is_not_required_of_a_receipt_finding():
    # The regression the derived floor exists to prevent: requiring the full
    # reviewer emit-set refused 1732 of 1902 real findings for ``category``.
    finding = _finding()
    assert "category" not in finding
    receipt = _receipt()
    receipt["report"]["findings"] = [finding]
    assert _enforce(receipt) is receipt


@pytest.mark.parametrize("severity", ["", "sev1", "HIGH", None, "info"])
def test_unknown_severity_is_refused(severity):
    receipt = _receipt()
    receipt["report"]["findings"] = [_finding(severity=severity)]
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == "quality_review_finding_0_severity_invalid"


@pytest.mark.parametrize("disposition", ["", "bug", "DEFECT", None])
def test_unknown_disposition_is_refused(disposition):
    receipt = _receipt()
    receipt["report"]["findings"] = [_finding(disposition=disposition)]
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == "quality_review_finding_0_disposition_invalid"


def test_absent_disposition_fails_closed_even_though_it_is_off_the_floor():
    # ``disposition`` is not in the required key floor, so an absent key must
    # be caught by value instead.
    finding = _finding()
    del finding["disposition"]
    receipt = _receipt()
    receipt["report"]["findings"] = [finding]
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == "quality_review_finding_0_disposition_invalid"


@pytest.mark.parametrize(
    "disposition, actionable",
    [
        ("defect", False),
        ("observation", True),
        ("process_limit", True),
        ("defect", "yes"),
        ("observation", 0),
    ],
)
def test_actionable_must_equal_disposition_is_defect(disposition, actionable):
    receipt = _receipt()
    receipt["report"]["findings"] = [
        _finding(disposition=disposition, actionable=actionable)
    ]
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == "quality_review_finding_0_actionable_invalid"


@pytest.mark.parametrize(
    "disposition, actionable",
    [("defect", True), ("observation", False), ("process_limit", False)],
)
def test_consistent_actionable_flag_is_accepted(disposition, actionable):
    receipt = _receipt()
    receipt["report"]["findings"] = [
        _finding(disposition=disposition, actionable=actionable)
    ]
    assert _enforce(receipt) is receipt


# --------------------------------------------------------------------------
# Authority and read-only report.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key, reason",
    [
        ("process_identity_verified", "quality_review_authority_process_identity_invalid"),
        ("audit_verified", "quality_review_authority_audit_invalid"),
    ],
)
@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_authority_flags_must_be_exactly_true(key, reason, value):
    receipt = _receipt()
    receipt["authority"][key] = value
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == reason


@pytest.mark.parametrize("state", ["finished", "", None, "REVIEW_READY"])
def test_terminal_state_must_be_review_ready(state):
    receipt = _receipt()
    receipt["authority"]["terminal_state"] = state
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == "quality_review_authority_terminal_state_invalid"


@pytest.mark.parametrize(
    "read_only, can_mutate",
    [(False, False), (True, True), (None, False), (1, False), (True, None)],
)
def test_report_must_declare_read_only_and_no_repo_mutation(read_only, can_mutate):
    receipt = _receipt()
    receipt["report"]["read_only"] = read_only
    receipt["report"]["can_mutate_repo"] = can_mutate
    with pytest.raises(WorkspaceError) as excinfo:
        _enforce(receipt)
    assert _reason(excinfo) == "quality_review_report_not_read_only"


# --------------------------------------------------------------------------
# Retained reviewer workspace must be provably read-only and empty.
# --------------------------------------------------------------------------


def _terminal_evidence(**overrides):
    evidence = {
        "changed_paths": [],
        "changed_path_hashes": {},
        "workspace": {"allowed_writes": []},
    }
    evidence.update(overrides)
    return evidence


@pytest.mark.parametrize("value", [["src/x.py"], "not-a-list", None, {}])
def test_changed_paths_must_be_an_empty_list(value):
    with pytest.raises(WorkspaceError) as excinfo:
        process_launcher._enforce_readonly_retained_workspace(
            _terminal_evidence(changed_paths=value)
        )
    assert _reason(excinfo) == "quality_review_changed_paths_not_empty"


@pytest.mark.parametrize("value", [{"src/x.py": SHA_A}, [], None, "x"])
def test_changed_path_hashes_must_be_an_empty_dict(value):
    with pytest.raises(WorkspaceError) as excinfo:
        process_launcher._enforce_readonly_retained_workspace(
            _terminal_evidence(changed_path_hashes=value)
        )
    assert _reason(excinfo) == "quality_review_changed_path_hashes_not_empty"


@pytest.mark.parametrize("value", [None, "x", []])
def test_workspace_metadata_must_be_a_mapping(value):
    with pytest.raises(WorkspaceError) as excinfo:
        process_launcher._enforce_readonly_retained_workspace(
            _terminal_evidence(workspace=value)
        )
    assert _reason(excinfo) == "quality_review_workspace_metadata_missing"


@pytest.mark.parametrize("value", [["src/x.py"], "src/x.py", None, {}])
def test_workspace_allowed_writes_must_be_an_empty_list(value):
    with pytest.raises(WorkspaceError) as excinfo:
        process_launcher._enforce_readonly_retained_workspace(
            _terminal_evidence(workspace={"allowed_writes": value})
        )
    assert _reason(excinfo) == "quality_review_workspace_allowed_writes_not_empty"


def test_unreconstructable_workspace_fails_closed():
    # Metadata that passes the cheap checks but cannot be rebuilt must still
    # refuse rather than fall through to a trusted receipt.
    with pytest.raises(WorkspaceError) as excinfo:
        process_launcher._enforce_readonly_retained_workspace(
            _terminal_evidence(workspace={"allowed_writes": []})
        )
    assert _reason(excinfo) == "quality_review_workspace_reconstruction_failed"
