import copy
import dataclasses
import inspect
import json
from pathlib import Path

import pytest

from aiworkhub import development_rules as dr


def _manifest_mapping():
    return {
        "schema": dr.SCHEMA_ID,
        "schema_version": dr.SCHEMA_VERSION,
        "languages": ["python", "rust"],
        "rules": [
            {
                "id": "perf_generic",
                "topic": "perf_hot_loop",
                "kind": "performance",
                "applicability": {},
                "payload": {
                    "max_allocations_per_iteration": 100,
                    "max_time_complexity": "o_n",
                    "max_space_complexity": "o_1",
                    "min_concurrency_headroom_fraction": 0.25,
                    "measurement_required": True,
                    "measurement_evidence_id": "benchmark_report",
                },
            },
            {
                "id": "perf_python_specific",
                "topic": "perf_hot_loop",
                "kind": "performance",
                "applicability": {"languages": ["python"]},
                "payload": {
                    "max_allocations_per_iteration": 10,
                    "max_time_complexity": "o_1",
                    "max_space_complexity": "o_1",
                    "min_concurrency_headroom_fraction": 0.5,
                    "measurement_required": True,
                    "measurement_evidence_id": "benchmark_report",
                },
            },
            {
                "id": "ownership_rule",
                "kind": "ownership_lifetime",
                "applicability": {"languages": ["rust"]},
                "payload": {
                    "requires_raii": True,
                    "requires_context_manager": False,
                    "requires_explicit_cleanup": True,
                    "forbidden_raw_ownership_patterns": ["manual_free", "raw_pointer_delete"],
                },
            },
            {
                "id": "concurrency_rule",
                "kind": "concurrency",
                "applicability": {},
                "payload": {"model": "threads_io_bound", "min_headroom_fraction": 0.2},
            },
            {
                "id": "evidence_rule",
                "kind": "evidence",
                "applicability": {"tasks": ["release"]},
                "payload": {"required_evidence_types": ["test_report", "benchmark_report"], "min_count": 2},
            },
            {
                "id": "coding_convention_rule",
                "kind": "coding_convention",
                "applicability": {"paths": ["src/**/*.py"]},
                "payload": {"guideline_ids": ["naming_snake_case"], "severity": "warning"},
            },
            {
                "id": "forbidden_pattern_rule",
                "kind": "forbidden_pattern",
                "applicability": {},
                "forbid": ["eval_usage"],
                "payload": {"severity": "error", "rationale_id": "security_risk"},
            },
        ],
    }


def _rule_by_id(manifest, rule_id):
    for rule in manifest.rules:
        if rule.id == rule_id:
            return rule
    raise AssertionError(f"rule {rule_id!r} not found")


def test_repository_manifest_pins_self_hosting_break_glass_contract():
    manifest_path = Path(__file__).resolve().parents[1] / ".aiworkhub/config/development_rules.json"
    manifest = dr.parse_manifest_bytes(manifest_path.read_bytes())
    rule = _rule_by_id(manifest, "self_hosting_break_glass")

    assert rule.topic == "self_hosting_break_glass"
    assert set(rule.allow) == {
        "independent_manager_validation",
        "measured_task_system_blocker",
        "minimal_intermediate_fix",
        "replacement_artifact_install",
        "return_to_canonical_task_flow",
    }
    assert set(rule.forbid) == {
        "continued_use_of_known_broken_plugin",
        "scope_expansion_during_break_glass",
        "silent_task_system_bypass",
        "unmeasured_break_glass",
    }
    assert set(rule.payload.guideline_ids) == {
        "record_blocker_evidence",
        "preserve_unrelated_work",
        "restore_task_system_only",
        "validate_replacement_independently",
    }
    assert rule.payload.severity is dr.Severity.ERROR


# --- deterministic digest ---------------------------------------------------


def test_digest_stable_across_mapping_key_order():
    mapping = _manifest_mapping()
    reordered = json.loads(json.dumps(mapping))
    reordered["languages"] = list(reversed(reordered["languages"]))
    reordered["rules"] = list(reversed(reordered["rules"]))
    reordered_with_new_key_order = {
        "rules": reordered["rules"],
        "languages": reordered["languages"],
        "schema_version": reordered["schema_version"],
        "schema": reordered["schema"],
    }

    digest_a = dr.canonical_digest(dr.parse_manifest(mapping))
    digest_b = dr.canonical_digest(dr.parse_manifest(reordered_with_new_key_order))

    assert digest_a == digest_b
    assert len(digest_a) == 64
    int(digest_a, 16)


def test_digest_matches_between_mapping_and_bytes_apis():
    mapping = _manifest_mapping()
    from_mapping = dr.parse_manifest(mapping)
    from_bytes = dr.parse_manifest_bytes(json.dumps(mapping).encode("utf-8"))

    assert dr.canonical_digest(from_mapping) == dr.canonical_digest(from_bytes)


def test_digest_changes_with_content():
    mapping_a = _manifest_mapping()
    mapping_b = _manifest_mapping()
    mapping_b["rules"][0]["payload"]["max_allocations_per_iteration"] = 1

    digest_a = dr.canonical_digest(dr.parse_manifest(mapping_a))
    digest_b = dr.canonical_digest(dr.parse_manifest(mapping_b))

    assert digest_a != digest_b


# --- valid multi-language resolution ---------------------------------------


def test_resolve_multi_language_returns_distinct_scopes():
    manifest = dr.parse_manifest(_manifest_mapping())

    python_resolved = dr.resolve(manifest, language="python")
    rust_resolved = dr.resolve(manifest, language="rust")

    python_ids = {rule.id for rule in python_resolved.rules}
    rust_ids = {rule.id for rule in rust_resolved.rules}

    assert "ownership_rule" in rust_ids
    assert "ownership_rule" not in python_ids
    assert "concurrency_rule" in python_ids and "concurrency_rule" in rust_ids


def test_resolve_path_and_task_and_risk_applicability():
    manifest = dr.parse_manifest(_manifest_mapping())

    by_path = dr.resolve(manifest, path="src/pkg/module.py")
    assert "coding_convention_rule" in {r.id for r in by_path.rules}

    by_bad_path = dr.resolve(manifest, path="other/module.py")
    assert "coding_convention_rule" not in {r.id for r in by_bad_path.rules}

    by_task = dr.resolve(manifest, task="release")
    assert "evidence_rule" in {r.id for r in by_task.rules}

    without_task = dr.resolve(manifest)
    assert "evidence_rule" not in {r.id for r in without_task.rules}

    by_risk_str = dr.resolve(manifest, risk="high")
    assert by_risk_str.risk is dr.RiskLevel.HIGH


# --- exact-match precedence --------------------------------------------------


def test_resolve_exact_match_precedence_over_generic():
    manifest = dr.parse_manifest(_manifest_mapping())

    python_resolved = dr.resolve(manifest, language="python")
    perf_rules = [r for r in python_resolved.rules if r.topic == "perf_hot_loop"]
    assert [r.id for r in perf_rules] == ["perf_python_specific"]

    rust_resolved = dr.resolve(manifest, language="rust")
    perf_rules_rust = [r for r in rust_resolved.rules if r.topic == "perf_hot_loop"]
    assert [r.id for r in perf_rules_rust] == ["perf_generic"]


def test_resolve_does_not_broaden_authority_without_context():
    manifest = dr.parse_manifest(_manifest_mapping())

    resolved = dr.resolve(manifest)
    resolved_ids = {r.id for r in resolved.rules}

    assert "perf_python_specific" not in resolved_ids
    assert "ownership_rule" not in resolved_ids
    assert "evidence_rule" not in resolved_ids
    assert "perf_generic" in resolved_ids


# --- resolve() context validation fails closed ------------------------------


def test_resolve_rejects_non_string_language():
    manifest = dr.parse_manifest(_manifest_mapping())
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.resolve(manifest, language=1)
    assert excinfo.value.reason is dr.ReasonCode.WRONG_TYPE


def test_resolve_rejects_unknown_language():
    manifest = dr.parse_manifest(_manifest_mapping())
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.resolve(manifest, language="cobol")
    assert excinfo.value.reason is dr.ReasonCode.UNKNOWN_LANGUAGE_REFERENCE


def test_resolve_rejects_unsafe_traversal_path():
    manifest = dr.parse_manifest(_manifest_mapping())
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.resolve(manifest, path="../escape")
    assert excinfo.value.reason is dr.ReasonCode.UNSAFE_PATH_SELECTOR


@pytest.mark.parametrize("bad_path", ["src/**", "src/*.py", "src/file?.py", "src/[abc].py"])
def test_resolve_rejects_glob_metacharacters_in_concrete_path(bad_path):
    manifest = dr.parse_manifest(_manifest_mapping())
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.resolve(manifest, path=bad_path)
    assert excinfo.value.reason is dr.ReasonCode.UNSAFE_PATH_SELECTOR


def test_resolve_rejects_malformed_language_identifier():
    manifest = dr.parse_manifest(_manifest_mapping())
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.resolve(manifest, language="Bad-Lang!")
    assert excinfo.value.reason is dr.ReasonCode.MALFORMED_IDENTIFIER


def test_resolve_rejects_malformed_task():
    manifest = dr.parse_manifest(_manifest_mapping())
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.resolve(manifest, task="Bad-Task!")
    assert excinfo.value.reason is dr.ReasonCode.MALFORMED_IDENTIFIER


# --- exact-over-glob selector precedence and ambiguous ties ----------------


def _precedence_manifest_mapping():
    mapping = _manifest_mapping()
    mapping["rules"].extend(
        [
            {
                "id": "path_exact_rule",
                "topic": "path_precedence",
                "kind": "forbidden_pattern",
                "applicability": {"paths": ["src/core.py"]},
                "payload": {"severity": "error", "rationale_id": "exact_match"},
            },
            {
                "id": "path_glob_rule",
                "topic": "path_precedence",
                "kind": "forbidden_pattern",
                "applicability": {"paths": ["src/**"]},
                "payload": {"severity": "warning", "rationale_id": "glob_match"},
            },
        ]
    )
    return mapping


def test_resolve_exact_path_selector_outranks_glob_selector():
    manifest = dr.parse_manifest(_precedence_manifest_mapping())

    resolved = dr.resolve(manifest, path="src/core.py")
    precedence_rules = [r for r in resolved.rules if r.topic == "path_precedence"]

    assert [r.id for r in precedence_rules] == ["path_exact_rule"]


def _ambiguous_tie_manifest_mapping():
    mapping = _manifest_mapping()
    mapping["rules"].extend(
        [
            {
                "id": "tie_allow_rule",
                "topic": "tie_topic",
                "kind": "forbidden_pattern",
                "applicability": {"paths": ["src/**"]},
                "allow": ["risky_call"],
                "payload": {"severity": "warning", "rationale_id": "tie_allow"},
            },
            {
                "id": "tie_forbid_rule",
                "topic": "tie_topic",
                "kind": "forbidden_pattern",
                "applicability": {"paths": ["src/core.*"]},
                "forbid": ["risky_call"],
                "payload": {"severity": "error", "rationale_id": "tie_forbid"},
            },
        ]
    )
    return mapping


def test_resolve_ambiguous_equal_precedence_contradiction_fails_closed():
    manifest = dr.parse_manifest(_ambiguous_tie_manifest_mapping())

    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.resolve(manifest, path="src/core.py")
    assert excinfo.value.reason is dr.ReasonCode.AMBIGUOUS_RULE_PRECEDENCE


# --- immutability -------------------------------------------------------------


def test_manifest_and_rule_are_frozen():
    manifest = dr.parse_manifest(_manifest_mapping())
    rule = manifest.rules[0]

    with pytest.raises(dataclasses.FrozenInstanceError):
        manifest.schema = "tampered"
    with pytest.raises(dataclasses.FrozenInstanceError):
        rule.id = "tampered"
    with pytest.raises(AttributeError):
        manifest.rules.append(rule)
    with pytest.raises(AttributeError):
        rule.allow.append("x")


def test_resolved_rule_set_is_frozen():
    manifest = dr.parse_manifest(_manifest_mapping())
    resolved = dr.resolve(manifest, language="python")

    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.rules = ()
    with pytest.raises(AttributeError):
        resolved.rules.append(manifest.rules[0])


# --- malformed / contradictory inputs fail closed with typed reasons --------


def test_unknown_top_level_field_fails_closed():
    mapping = _manifest_mapping()
    mapping["unexpected"] = True
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.UNKNOWN_FIELD


def test_check_keys_heterogeneous_unknown_keys_fail_closed_at_top_level():
    mapping = {"x": 1, 1: "y"}
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr._check_keys(mapping, frozenset({"schema"}), required=frozenset(), path="$")
    assert excinfo.value.reason is dr.ReasonCode.UNKNOWN_FIELD


def test_check_keys_heterogeneous_unknown_keys_fail_closed_when_nested():
    mapping = _manifest_mapping()
    mapping["rules"][0]["applicability"] = {"languages": ["python"], "bad": 1, 2: "also_bad"}
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.UNKNOWN_FIELD


def test_missing_required_field_fails_closed():
    mapping = _manifest_mapping()
    del mapping["languages"]
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.MISSING_FIELD


def test_wrong_schema_fails_closed():
    mapping = _manifest_mapping()
    mapping["schema"] = "not.the.schema"
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.UNKNOWN_SCHEMA


def test_malformed_schema_version_fails_closed():
    mapping = _manifest_mapping()
    mapping["schema_version"] = "1.0"
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.MALFORMED_VERSION


def test_unsupported_schema_version_fails_closed():
    mapping = _manifest_mapping()
    mapping["schema_version"] = "2.0.0"
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.UNSUPPORTED_SCHEMA_VERSION


def test_malformed_identifier_fails_closed():
    mapping = _manifest_mapping()
    mapping["rules"][0]["id"] = "Bad-ID!"
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.MALFORMED_IDENTIFIER


def test_duplicate_rule_id_fails_closed():
    mapping = _manifest_mapping()
    mapping["rules"][1]["id"] = mapping["rules"][0]["id"]
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.DUPLICATE_IDENTIFIER


def test_duplicate_identifier_within_list_fails_closed():
    mapping = _manifest_mapping()
    mapping["languages"] = ["python", "python"]
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.DUPLICATE_IDENTIFIER


def test_bool_as_int_fails_closed_for_int_field():
    mapping = _manifest_mapping()
    mapping["rules"][0]["payload"]["max_allocations_per_iteration"] = True
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.BOOL_AS_INT


def test_bool_as_int_fails_closed_for_float_field():
    mapping = _manifest_mapping()
    mapping["rules"][0]["payload"]["min_concurrency_headroom_fraction"] = False
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.BOOL_AS_INT


def test_int_as_bool_fails_closed():
    mapping = _manifest_mapping()
    mapping["rules"][0]["payload"]["measurement_required"] = 1
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.WRONG_TYPE


def test_contradictory_allow_forbid_fails_closed():
    mapping = _manifest_mapping()
    mapping["rules"][6]["allow"] = ["eval_usage"]
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.CONTRADICTORY_RULE


def test_unsafe_absolute_path_selector_fails_closed():
    mapping = _manifest_mapping()
    mapping["rules"][5]["applicability"]["paths"] = ["/etc/passwd"]
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.UNSAFE_PATH_SELECTOR


def test_unsafe_traversal_path_selector_fails_closed():
    mapping = _manifest_mapping()
    mapping["rules"][5]["applicability"]["paths"] = ["../secret/*.py"]
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.UNSAFE_PATH_SELECTOR


def test_unbounded_numeric_limit_fails_closed_from_mapping():
    mapping = _manifest_mapping()
    mapping["rules"][0]["payload"]["min_concurrency_headroom_fraction"] = float("inf")
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.UNBOUNDED_LIMIT


def test_unbounded_numeric_limit_fails_closed_from_json_bytes():
    mapping = _manifest_mapping()
    payload = mapping["rules"][0]["payload"]
    raw = json.dumps(mapping).replace(
        json.dumps(payload["min_concurrency_headroom_fraction"]), "Infinity"
    )
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest_bytes(raw.encode("utf-8"))
    assert excinfo.value.reason is dr.ReasonCode.UNBOUNDED_LIMIT


def test_out_of_range_limit_fails_closed():
    mapping = _manifest_mapping()
    mapping["rules"][0]["payload"]["max_allocations_per_iteration"] = dr.MAX_ALLOCATIONS_PER_ITERATION + 1
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.OUT_OF_RANGE


def test_unknown_language_reference_fails_closed():
    mapping = _manifest_mapping()
    mapping["rules"][0]["applicability"] = {"languages": ["cobol"]}
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.UNKNOWN_LANGUAGE_REFERENCE


def test_empty_rules_fails_closed():
    mapping = _manifest_mapping()
    mapping["rules"] = []
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.EMPTY_COLLECTION


def test_empty_required_collection_in_payload_fails_closed():
    mapping = _manifest_mapping()
    mapping["rules"][4]["payload"]["required_evidence_types"] = []
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.EMPTY_COLLECTION


def test_invalid_enum_value_fails_closed():
    mapping = _manifest_mapping()
    mapping["rules"][0]["kind"] = "bogus_kind"
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.INVALID_ENUM_VALUE


def test_unknown_field_in_performance_payload_rejects_executable_command():
    mapping = _manifest_mapping()
    mapping["rules"][0]["payload"]["command"] = "rm -rf /"
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.UNKNOWN_FIELD


def test_malformed_json_bytes_fails_closed():
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest_bytes(b"{not json")
    assert excinfo.value.reason is dr.ReasonCode.MALFORMED_JSON


def test_deeply_nested_json_bytes_fails_closed_instead_of_recursion_error():
    deeply_nested = b"[" * 200_000 + b"]" * 200_000
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest_bytes(deeply_nested)
    assert excinfo.value.reason is dr.ReasonCode.MALFORMED_JSON


def test_non_mapping_input_fails_closed():
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(["not", "a", "mapping"])
    assert excinfo.value.reason is dr.ReasonCode.WRONG_TYPE


class _HostileKey:
    """A mapping key whose __repr__ raises, to prove diagnostics never invoke it."""

    def __hash__(self):
        return 0

    def __eq__(self, other):
        return self is other

    def __repr__(self):
        raise RuntimeError("hostile repr must never be invoked")


def test_unknown_field_diagnostics_never_invoke_hostile_key_repr():
    mapping = _manifest_mapping()
    mapping[_HostileKey()] = "value"
    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.parse_manifest(mapping)
    assert excinfo.value.reason is dr.ReasonCode.UNKNOWN_FIELD


# --- foundation constraints ---------------------------------------------------


def test_manifest_parses_without_touching_the_filesystem(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manifest = dr.parse_manifest(_manifest_mapping())
    assert manifest.schema == dr.SCHEMA_ID


def test_module_has_no_io_subprocess_or_network_imports():
    source = inspect.getsource(dr)
    forbidden_tokens = ("import os", "import subprocess", "import socket", "import sqlite3", "open(")
    for token in forbidden_tokens:
        assert token not in source


def test_os_dependency_boundary_is_optional_but_canonical_and_digest_stable():
    legacy = _manifest_mapping()
    assert dr.parse_manifest(legacy).os_dependency_boundary is None
    boundary = {
        "scan_root": "src/aiworkhub",
        "public_facade": "src/aiworkhub/platform_io.py",
        "sanctioned_modules": list(dr.OS_DEPENDENCY_SANCTIONED_MODULES),
        "patterns": sorted(dr.OS_DEPENDENCY_PATTERN_IDENTITIES),
        "baseline": [{"path": "src/aiworkhub/sample.py", "pattern": "sys_platform", "count": 2}],
        "measurement": {"reference_commit": "0" * 40, "reference_total": 3, "current_total": 2, "accepted_predecessor_delta": -1},
    }
    legacy["os_dependency_boundary"] = boundary
    parsed = dr.parse_manifest(legacy)
    reordered = copy.deepcopy(legacy)
    reordered["os_dependency_boundary"]["baseline"] = list(reversed(reordered["os_dependency_boundary"]["baseline"]))
    assert parsed.os_dependency_boundary is not None
    assert dr.canonical_digest(parsed) == dr.canonical_digest(dr.parse_manifest(reordered))


@pytest.mark.parametrize("field,value", [("patterns", ["sys_platform"]), ("sanctioned_modules", ["src/aiworkhub/platform_io.py"])])
def test_os_dependency_boundary_rejects_widening_or_pattern_configuration(field, value):
    mapping = _manifest_mapping()
    mapping["os_dependency_boundary"] = {
        "scan_root": "src/aiworkhub", "public_facade": "src/aiworkhub/platform_io.py",
        "sanctioned_modules": list(dr.OS_DEPENDENCY_SANCTIONED_MODULES),
        "patterns": sorted(dr.OS_DEPENDENCY_PATTERN_IDENTITIES), "baseline": [],
        "measurement": {"reference_commit": "0" * 40, "reference_total": 0, "current_total": 0, "accepted_predecessor_delta": 0},
    }
    mapping["os_dependency_boundary"][field] = value
    with pytest.raises(dr.ManifestValidationError):
        dr.parse_manifest(mapping)


def test_valid_manifest_is_round_trip_deep_copyable_without_mutation_sharing():
    mapping = _manifest_mapping()
    manifest_a = dr.parse_manifest(mapping)
    manifest_b = dr.parse_manifest(copy.deepcopy(mapping))
    assert dr.canonical_digest(manifest_a) == dr.canonical_digest(manifest_b)


# --- inert construction-template registry ----------------------------------


def _template_registry_mapping():
    return {
        "schema": dr.CONSTRUCTION_TEMPLATE_REGISTRY_SCHEMA_ID,
        "schema_version": dr.CONSTRUCTION_TEMPLATE_REGISTRY_SCHEMA_VERSION,
        "languages": ["python", "rust"],
        "templates": [
            {
                "id": "python_guard",
                "applicability": {
                    "languages": ["python"],
                    "symbol_kinds": ["function"],
                    "task_kinds": ["refactor"],
                    "risk_levels": ["medium", "high"],
                },
                "slots": [{"id": "condition", "type": "expression", "required": True}],
                "pattern": [{"literal": "if "}, {"logic_slot": "condition"}, {"literal": ":\n    pass\n"}],
            },
            {
                "id": "generic_marker",
                "slots": [],
                "pattern": [{"literal": "# inert marker\n"}],
            },
        ],
    }


def test_construction_template_registry_is_immutable_inert_and_deterministic():
    registry = dr.parse_construction_template_registry(_template_registry_mapping())
    template = next(template for template in registry.templates if template.id == "python_guard")

    assert isinstance(template.pattern[0], dr.LiteralPatternNode)
    assert isinstance(template.pattern[1], dr.LogicSlotReferenceNode)
    assert template.pattern[0].source == "if "
    with pytest.raises(dataclasses.FrozenInstanceError):
        template.id = "changed"
    with pytest.raises(AttributeError):
        template.pattern.append(template.pattern[0])


def test_construction_template_digests_ignore_mapping_key_order():
    mapping = _template_registry_mapping()

    def reorder_mapping_keys(value):
        if isinstance(value, dict):
            return {key: reorder_mapping_keys(value[key]) for key in reversed(value)}
        if isinstance(value, list):
            return [reorder_mapping_keys(item) for item in value]
        return value

    reordered = reorder_mapping_keys(mapping)
    registry_a = dr.parse_construction_template_registry(mapping)
    registry_b = dr.parse_construction_template_registry(reordered)

    assert dr.construction_template_registry_digest(registry_a) == dr.construction_template_registry_digest(registry_b)
    assert dr.construction_template_digest(registry_a.templates[0]) == dr.construction_template_digest(registry_b.templates[0])


def test_select_construction_templates_returns_stable_provenance_receipt():
    registry = dr.parse_construction_template_registry(_template_registry_mapping())
    receipt = dr.select_construction_templates(
        registry, language="python", symbol_kind="function", task_kind="refactor", risk="high"
    )

    assert receipt.template_ids == ("generic_marker", "python_guard")
    assert receipt.template_digests == tuple(
        dr.construction_template_digest(next(template for template in registry.templates if template.id == template_id))
        for template_id in receipt.template_ids
    )
    assert receipt.registry_digest == dr.construction_template_registry_digest(registry)


def test_select_construction_templates_reports_malformed_symbol_kind_name():
    registry = dr.parse_construction_template_registry(_template_registry_mapping())

    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.select_construction_templates(registry, symbol_kind="Bad-Symbol!")

    assert excinfo.value.reason is dr.ReasonCode.MALFORMED_IDENTIFIER
    assert excinfo.value.detail.startswith("symbol_kind:")


def test_select_construction_templates_reports_malformed_task_kind_name():
    registry = dr.parse_construction_template_registry(_template_registry_mapping())

    with pytest.raises(dr.ManifestValidationError) as excinfo:
        dr.select_construction_templates(registry, task_kind="Bad-Task!")

    assert excinfo.value.reason is dr.ReasonCode.MALFORMED_IDENTIFIER
    assert excinfo.value.detail.startswith("task_kind:")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda mapping: mapping["templates"][0].update({"command": "echo unsafe"}),
        lambda mapping: mapping["templates"][0]["pattern"].append({"command": "echo unsafe"}),
        lambda mapping: mapping["templates"][0]["pattern"].__setitem__(1, {"logic_slot": "missing"}),
        lambda mapping: mapping["templates"][0]["pattern"].__setitem__(1, {"literal": "no reference"}),
        lambda mapping: mapping["templates"][0]["applicability"].update({"symbol_kinds": ["Bad-Kind!"]}),
    ],
)
def test_construction_template_registry_malformed_inputs_fail_closed(mutate):
    mapping = _template_registry_mapping()
    mutate(mapping)
    with pytest.raises(dr.ManifestValidationError):
        dr.parse_construction_template_registry(mapping)
