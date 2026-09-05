from __future__ import annotations

import json

from pathlib import Path

import pytest
import aiworkhub.task_templates as task_templates_module

from aiworkhub.quality_evidence import normalize_behavioral_contract
from aiworkhub.task_templates import (
    AUDITED_CUSTOM_ESCAPE,
    MAX_PATHS_PER_FIELD,
    REGISTRY_VERSION,
    SCHEMA_ID,
    TEMPLATE_IDS,
    TaskTemplateError,
    _NODE_SUFFIXES,
    _NODE_TEST_SUFFIXES,
    _canonical_work_kind,
    _looks_like_test_path,
    _partition_write_set,
    _validation_roles_for,
    classify_task_card,
    expand_template,
    expanded_contract_digest,
    reject_unchanged_public_test_outputs,
    resolve_template,
    split_command_argv,
    template_full_id,
    template_provenance_payload,
    validate_custom_validation_roles,
    validate_template_provenance,
)
from aiworkhub.worker_workspace import validation_argv

def test_registry_has_exactly_seven_stable_template_ids():
    assert TEMPLATE_IDS == (
        "read_only_analysis",
        "bugfix_with_regression",
        "implementation_with_tests",
        "test_only",
        "docs_change",
        "validation_replay",
        "cross_boundary_bugfix",
    )


def test_full_ids_bind_version_and_digest_and_resolve_round_trip():
    for name in TEMPLATE_IDS:
        full_id = template_full_id(name)
        prefix = f"{name}@v{REGISTRY_VERSION}:"
        assert full_id.startswith(prefix)
        digest = full_id[len(prefix):]
        assert len(digest) == 64
        int(digest, 16)
        assert template_full_id(name) == full_id
        assert resolve_template(full_id) is resolve_template(name)
        assert resolve_template(name).name == name


def test_forged_full_id_fails_closed():
    digest = template_full_id("bugfix_with_regression").split(":", 1)[1]
    flipped = "0" if digest[-1] != "0" else "1"
    forged = f"bugfix_with_regression@v{REGISTRY_VERSION}:{digest[:-1]}{flipped}"
    with pytest.raises(TaskTemplateError, match="template_digest_mismatch"):
        resolve_template(forged)


def test_stale_full_id_fails_closed():
    digest = template_full_id("test_only").split(":", 1)[1]
    for version in ("v0", "v2", "v99"):
        with pytest.raises(TaskTemplateError, match="template_version_stale"):
            resolve_template(f"test_only@{version}:{digest}")


def test_malformed_or_unknown_template_ids_fail_closed():
    with pytest.raises(TaskTemplateError, match="template_id_malformed"):
        resolve_template("test_only@v1")
    with pytest.raises(TaskTemplateError, match="template_id_malformed"):
        resolve_template(f"test_only@v{REGISTRY_VERSION}:nothex")
    with pytest.raises(TaskTemplateError, match="template_id_malformed"):
        resolve_template(f"test_only@current:{'a' * 64}")
    with pytest.raises(TaskTemplateError, match="template_id_empty"):
        resolve_template("")
    with pytest.raises(TaskTemplateError, match="template_unknown"):
        resolve_template("no_such_template")
    with pytest.raises(TaskTemplateError, match="template_unknown"):
        resolve_template(f"ghost@v{REGISTRY_VERSION}:{'b' * 64}")


@pytest.mark.parametrize("bad", [None, 7, ["test_only"], b"test_only"])
def test_non_string_template_id_fails_closed(bad):
    with pytest.raises(TaskTemplateError, match="template_id_not_string"):
        resolve_template(bad)


def test_bugfix_requires_explicit_production_and_test_paths():
    with pytest.raises(TaskTemplateError, match="missing_production_paths"):
        expand_template("bugfix_with_regression", test_paths=["tests/test_a.py"])
    with pytest.raises(TaskTemplateError, match="missing_test_paths"):
        expand_template("bugfix_with_regression", production_paths=["src/a.py"])
    with pytest.raises(TaskTemplateError, match="missing_production_paths"):
        expand_template("bugfix_with_regression")


def test_bugfix_outputs_exactly_cover_atomic_write_set():
    card = expand_template(
        "bugfix_with_regression",
        production_paths=["src/a.py", "src/b.py"],
        test_paths=["tests/test_a.py"],
    )
    expected = ["src/a.py", "src/b.py", "tests/test_a.py"]
    assert card["allowed_writes"] == expected
    assert card["required_outputs"] == expected
    assert card["write_set"] == expected
    assert card["write_set"] == expected
    assert card["read_only"] is False
    assert card["read_first"] == expected
    assert card["validation"] == [
        "python -m pytest -q tests/test_a.py",
        "python -m ruff check src/a.py src/b.py tests/test_a.py",
        "git diff --check",
    ]


def test_implementation_with_tests_matches_bugfix_contract():
    card = expand_template(
        "implementation_with_tests",
        production_paths=["src/mod.py"],
        test_paths=["tests/test_mod.py"],
    )
    expected = ["src/mod.py", "tests/test_mod.py"]
    assert card["required_outputs"] == []
    assert card["allowed_writes"] == expected
    assert card["allowed_writes"] == expected
    assert card["validation"] == [
        "python -m pytest -q tests/test_mod.py",
        "python -m ruff check src/mod.py tests/test_mod.py",
        "git diff --check",
    ]


def test_test_only_requires_tests_and_rejects_production_paths():
    with pytest.raises(TaskTemplateError, match="missing_test_paths"):
        expand_template("test_only")
    with pytest.raises(
        TaskTemplateError, match="incompatible_scope_production_paths"
    ):
        expand_template(
            "test_only",
            production_paths=["src/a.py"],
            test_paths=["tests/test_a.py"],
        )
    card = expand_template("test_only", test_paths=["tests/test_a.py"])
    assert card["required_outputs"] == []
    assert card["allowed_writes"] == ["tests/test_a.py"]
    assert card["allowed_writes"] == ["tests/test_a.py"]
    assert card["read_first"] == ["tests/test_a.py"]
    assert card["validation"] == [
        "python -m pytest -q tests/test_a.py",
        "python -m ruff check tests/test_a.py",
        "git diff --check",
    ]


def test_docs_change_requires_docs_paths_and_rejects_test_paths():
    with pytest.raises(TaskTemplateError, match="missing_production_paths"):
        expand_template("docs_change")
    with pytest.raises(TaskTemplateError, match="incompatible_scope_test_paths"):
        expand_template(
            "docs_change",
            production_paths=["README.md"],
            test_paths=["tests/test_x.py"],
        )
    card = expand_template("docs_change", production_paths=["docs/guide.md"])
    assert card["required_outputs"] == []
    assert card["allowed_writes"] == ["docs/guide.md"]
    assert card["allowed_writes"] == ["docs/guide.md"]
    assert card["task_type"] == "code"
    assert card["work_kind"] == "generic"
    assert card["validation"] == ["git diff --check"]


def test_read_only_analysis_emits_no_writes_outputs_or_validations():
    card = expand_template("read_only_analysis", production_paths=["src/a.py"])
    assert card["read_only"] is True
    assert card["allowed_writes"] == []
    assert card["required_outputs"] == []
    assert card["write_set"] == []
    assert card["validation"] == []
    assert card["read_first"] == ["src/a.py"]
    bare = expand_template("read_only_analysis")
    assert bare["read_first"] == []
    assert bare["validation"] == []


def test_read_only_analysis_rejects_test_paths_scope():
    with pytest.raises(TaskTemplateError, match="incompatible_scope_test_paths"):
        expand_template(
            "read_only_analysis",
            production_paths=["src/a.py"],
            test_paths=["tests/test_a.py"],
        )


def test_validation_replay_runs_commands_without_writes():
    card = expand_template(
        "validation_replay",
        production_paths=["src/a.py"],
        test_paths=["tests/test_a.py"],
    )
    assert card["read_only"] is True
    assert card["allowed_writes"] == []
    assert card["required_outputs"] == []
    assert card["validation"] == [
        "python -m pytest -q tests/test_a.py",
        "python -m ruff check src/a.py tests/test_a.py",
        "git diff --check",
    ]
    with pytest.raises(TaskTemplateError, match="missing_test_paths"):
        expand_template("validation_replay")


@pytest.mark.parametrize(
    "bad", [None, 7, 3.14, b"src/a.py", Path("src/a.py"), ["nested"]]
)
def test_non_string_path_entries_fail_closed(bad):
    with pytest.raises(
        TaskTemplateError, match="invalid_production_path_not_string"
    ):
        expand_template("read_only_analysis", production_paths=[bad])


@pytest.mark.parametrize(
    "path",
    [" src/a.py", "src /a.py", "src/a.py ", "src/a b.py", "src/a\tb.py"],
)
def test_whitespace_paths_fail_closed(path):
    with pytest.raises(TaskTemplateError, match="invalid_production_path"):
        expand_template("read_only_analysis", production_paths=[path])


@pytest.mark.parametrize(
    "path", ["src/\ta.py", "src/a\nb.py", "src/a\x00.py", "src/a\x7f.py"]
)
def test_control_character_paths_fail_closed(path):
    with pytest.raises(
        TaskTemplateError, match="invalid_production_path_control_character"
    ):
        expand_template("read_only_analysis", production_paths=[path])


@pytest.mark.parametrize(
    "path",
    [
        "src/$(id).py",
        "src/a;b.py",
        "src/a|b.py",
        "src/a&b.py",
        "src/a`id`.py",
        "src/'x'.py",
        'src/"x".py',
        "src/$HOME/x.py",
        "src/a(x).py",
        "src/a>b.py",
        "src/a<b.py",
    ],
)
def test_shell_sensitive_paths_fail_closed(path):
    with pytest.raises(
        TaskTemplateError, match="invalid_production_path_unsafe_token"
    ):
        expand_template("read_only_analysis", production_paths=[path])


@pytest.mark.parametrize(
    "path", ["src/*.py", "tests/test_?.py", "src/[ab].py"]
)
def test_globbed_paths_fail_closed(path):
    with pytest.raises(
        TaskTemplateError, match="invalid_production_path_glob_character"
    ):
        expand_template("read_only_analysis", production_paths=[path])


@pytest.mark.parametrize(
    "path", ["../outside.py", "src/../../outside.py", "src/..", ".."]
)
def test_escaping_paths_fail_closed(path):
    with pytest.raises(TaskTemplateError, match="invalid_production_path_escape"):
        expand_template("read_only_analysis", production_paths=[path])


@pytest.mark.parametrize("path", ["/etc/passwd", "/tmp/x.py"])
def test_absolute_paths_fail_closed(path):
    with pytest.raises(
        TaskTemplateError, match="invalid_production_path_absolute"
    ):
        expand_template("read_only_analysis", production_paths=[path])


@pytest.mark.parametrize(
    "path", ["src\\a.py", "C:\\Users\\shrek"]
)
def test_backslash_paths_fail_closed(path):
    with pytest.raises(
        TaskTemplateError, match="invalid_production_path_backslash"
    ):
        expand_template("read_only_analysis", production_paths=[path])


@pytest.mark.parametrize(
    "path",
    ["./src/a.py", "src//a.py", "src/a.py/", "src/./a.py", "src/.", "src/a.py/."],
)
def test_non_normalized_paths_fail_closed(path):
    with pytest.raises(
        TaskTemplateError, match="invalid_production_path_not_normalized"
    ):
        expand_template("read_only_analysis", production_paths=[path])


def test_home_token_path_fails_closed():
    with pytest.raises(
        TaskTemplateError, match="invalid_production_path_home_token"
    ):
        expand_template("read_only_analysis", production_paths=["~/notes.txt"])


def test_duplicate_paths_fail_closed():
    with pytest.raises(
        TaskTemplateError, match="invalid_production_path_duplicate"
    ):
        expand_template(
            "read_only_analysis", production_paths=["src/a.py", "src/a.py"]
        )
    with pytest.raises(TaskTemplateError, match="duplicate_path_across_fields"):
        expand_template(
            "bugfix_with_regression",
            production_paths=["src/shared.py"],
            test_paths=["src/shared.py"],
        )


def test_path_fields_must_be_bounded_lists():
    for bad in ("src/a.py", b"src/a.py", 3, {"src": 1}):
        with pytest.raises(TaskTemplateError, match="invalid_production_paths"):
            expand_template("read_only_analysis", production_paths=bad)
    too_many = [f"src/file{index:03d}.py" for index in range(129)]
    with pytest.raises(TaskTemplateError, match="invalid_production_paths"):
        expand_template("read_only_analysis", production_paths=too_many)
    too_long = ["src/" + "x" * 500 + ".py"]
    with pytest.raises(
        TaskTemplateError, match="invalid_production_path_too_long"
    ):
        expand_template("read_only_analysis", production_paths=too_long)


def test_expansion_is_pure_and_deterministic():
    kwargs = {
        "production_paths": ["src/a.py"],
        "test_paths": ["tests/test_a.py"],
    }
    first = expand_template("bugfix_with_regression", **kwargs)
    second = expand_template("bugfix_with_regression", **kwargs)
    via_full_id = expand_template(
        template_full_id("bugfix_with_regression"), **kwargs
    )
    assert first == second == via_full_id


def test_expanded_card_carries_identity_fields():
    card = expand_template("docs_change", production_paths=["README.md"])
    assert card["schema_id"] == SCHEMA_ID
    assert card["template_id"] == "docs_change"
    assert card["template_full_id"] == template_full_id("docs_change")
    assert card["registry_version"] == REGISTRY_VERSION
    assert card["definition_digest"] == template_full_id("docs_change").split(
        ":", 1
    )[1]


def test_validation_commands_preserve_exact_argv_semantics():
    card = expand_template(
        "implementation_with_tests",
        production_paths=["src/mod.py"],
        test_paths=["tests/test_mod.py"],
    )
    for command in card["validation"]:
        argv = split_command_argv(command)
        assert " ".join(argv) == command
        assert all(" " not in token for token in argv)
    assert split_command_argv(card["validation"][0]) == [
        "python",
        "-m",
        "pytest",
        "-q",
        "tests/test_mod.py",
    ]
    assert split_command_argv(card["validation"][-1]) == ["git", "diff", "--check"]


def test_title_and_objective_overrides_must_be_bounded_text():
    card = expand_template(
        "docs_change",
        production_paths=["README.md"],
        title="  Doc refresh  ",
        objective="Refresh the usage docs.",
    )
    assert card["title"] == "Doc refresh"
    assert card["objective"] == "Refresh the usage docs."
    for bad in (7, "", "   ", "x" * 301):
        with pytest.raises(TaskTemplateError, match="invalid_title"):
            expand_template("docs_change", production_paths=["README.md"], title=bad)
    for bad in (7, "", "   "):
        with pytest.raises(TaskTemplateError, match="invalid_objective"):
            expand_template(
                "docs_change", production_paths=["README.md"], objective=bad
            )


def test_split_command_argv_rejects_non_string():
    with pytest.raises(TaskTemplateError, match="invalid_command"):
        split_command_argv(7)


_TEMPLATE_PATHS = {
    "read_only_analysis": {"production_paths": ["src/a.py"]},
    "bugfix_with_regression": {
        "production_paths": ["src/a.py"],
        "test_paths": ["tests/test_a.py"],
    },
    "implementation_with_tests": {
        "production_paths": ["src/mod.py"],
        "test_paths": ["tests/test_mod.py"],
    },
    "test_only": {"test_paths": ["tests/test_a.py"]},
    "docs_change": {"production_paths": ["docs/guide.md"]},
    "validation_replay": {
        "production_paths": ["src/a.py"],
        "test_paths": ["tests/test_a.py"],
    },
    "cross_boundary_bugfix": {
        "production_paths": ["src/a.py", "src/a.js"],
        "test_paths": ["tests/test_a.py", "tests/a.test.js"],
    },
}


def test_expansion_emits_one_to_one_roles_that_satisfy_behavioral_contract():
    for name in TEMPLATE_IDS:
        card = expand_template(name, **_TEMPLATE_PATHS[name])
        assert len(card["validation_roles"]) == len(card["validation"])
        kind, roles = normalize_behavioral_contract(
            card["work_kind"],
            card["validation"],
            card["validation_roles"],
        )
        assert kind == card["work_kind"]
        assert roles == card["validation_roles"]


def test_bugfix_roles_cover_reproduction_and_regression():
    card = expand_template(
        "bugfix_with_regression",
        production_paths=["src/a.py"],
        test_paths=["tests/test_a.py"],
    )
    assert card["work_kind"] == "bugfix"
    assert len(card["validation_roles"]) == len(card["validation"]) == 3
    assert "reproduction" in card["validation_roles"]
    assert "regression" in card["validation_roles"]
    normalize_behavioral_contract(
        card["work_kind"], card["validation"], card["validation_roles"]
    )


def test_specialized_role_coverage_is_one_to_one_and_contract_valid():
    cases = {
        "bugfix": (
            ["pytest tests/test_a.py", "ruff check src/a.py", "git diff --check"],
            ["reproduction", "regression", "generic"],
        ),
        "refactor": (["pytest tests/test_a.py"], ["parity"]),
        "performance": (
            ["bench baseline", "bench delta", "git diff --check"],
            ["baseline", "delta", "generic"],
        ),
        "security": (["pytest tests/test_neg.py"], ["negative_fixture"]),
        "data_ml": (
            ["pytest tests/test_schema.py", "pytest tests/test_dist.py"],
            ["schema", "distribution"],
        ),
        "generic": (["git diff --check"], ["generic"]),
    }
    for work_kind, (commands, expected) in cases.items():
        roles = _validation_roles_for(work_kind, commands)
        assert roles == expected
        normalize_behavioral_contract(work_kind, commands, roles)


def test_non_canonical_template_work_kinds_map_to_generic():
    assert _canonical_work_kind("analysis") == "generic"
    assert _canonical_work_kind("implementation") == "generic"
    assert _canonical_work_kind("test") == "generic"
    assert _canonical_work_kind("docs") == "generic"
    assert _canonical_work_kind("replay") == "generic"
    assert _canonical_work_kind("bugfix") == "bugfix"
    card = expand_template("read_only_analysis", production_paths=["src/a.py"])
    assert card["work_kind"] == "generic"
    assert card["validation_roles"] == []
    docs = expand_template("docs_change", production_paths=["docs/guide.md"])
    assert docs["work_kind"] == "generic"
    assert docs["task_type"] == "code"


def test_validation_roles_seed_through_live_path_and_normalized_authority():
    # Roles are seeded on the live expansion path and validated by the one
    # normalized authority (normalize_behavioral_contract), not by a duplicate
    # in-module fail-closed guard.
    card = expand_template(
        "bugfix_with_regression",
        production_paths=["src/a.py"],
        test_paths=["tests/test_a.py"],
    )
    assert len(card["validation_roles"]) == len(card["validation"])
    kind, roles = normalize_behavioral_contract(
        card["work_kind"], card["validation"], card["validation_roles"]
    )
    assert kind == "bugfix"
    assert roles == card["validation_roles"]
    # The seed helper no longer raises; it fills available slots and defers the
    # authoritative contract check to normalize_behavioral_contract.
    assert _validation_roles_for(
        "bugfix", ["python -m pytest -q tests/test_a.py"]
    ) == ["reproduction"]


_CANONICAL_TASK_TYPES = ("code", "data_classification", "research")


def test_every_template_emits_canonical_create_task_type():
    for name in TEMPLATE_IDS:
        card = expand_template(name, **_TEMPLATE_PATHS[name])
        assert card["task_type"] in _CANONICAL_TASK_TYPES


@pytest.mark.parametrize(
    "path",
    [
        "--collect-only",
        "--exit-zero",
        "-q",
        "src/--collect-only.py",
        "tests/--exit-zero",
    ],
)
def test_leading_hyphen_path_tokens_fail_closed_before_command_generation(path):
    with pytest.raises(
        TaskTemplateError, match="invalid_production_path_leading_hyphen"
    ):
        expand_template("read_only_analysis", production_paths=[path])
    with pytest.raises(TaskTemplateError, match="invalid_test_path_leading_hyphen"):
        expand_template("test_only", test_paths=[path])
    with pytest.raises(
        TaskTemplateError, match="invalid_production_path_leading_hyphen"
    ):
        expand_template(
            "bugfix_with_regression",
            production_paths=[path],
            test_paths=["tests/test_a.py"],
        )


def test_cross_boundary_bugfix_keeps_python_and_node_commands_separated():
    card = expand_template(
        "cross_boundary_bugfix",
        production_paths=["src/a.py", "src/a.js"],
        test_paths=["tests/test_a.py", "tests/a.test.js"],
    )
    assert card["validation"] == [
        "python -m pytest -q tests/test_a.py",
        "python -m ruff check src/a.py tests/test_a.py",
        "node --test tests/a.test.js",
        "git diff --check",
    ]
    python_commands = [command for command in card["validation"] if command.startswith("python ")]
    node_commands = [command for command in card["validation"] if command.startswith("node ")]
    assert all(".js" not in command for command in python_commands)
    assert all(".py" not in command for command in node_commands)
    assert card["work_kind"] == "bugfix"
    assert "reproduction" in card["validation_roles"]
    assert "regression" in card["validation_roles"]

def test_generic_python_production_plus_test_classifies_compatibly():
    card = expand_template(
        "implementation_with_tests",
        production_paths=["src/mod.py"],
        test_paths=["tests/test_mod.py"],
    )
    generic_roles = ["generic"] * len(card["validation"])
    assert generic_roles == card["validation_roles"]
    provenance = classify_task_card(
        allowed_writes=card["allowed_writes"],
        required_outputs=card["required_outputs"],
        validation=card["validation"],
        validation_roles=generic_roles,
        work_kind="generic",
        read_only=False,
        read_first=card["read_first"],
    )
    assert provenance["template_name"] == "implementation_with_tests"
    assert provenance["template_full_id"] == card["template_full_id"]
    assert provenance["registry_version"] == REGISTRY_VERSION
    assert provenance["definition_digest"] == card["definition_digest"]
    assert (
        provenance["classification_reason"]
        == "compatible_generic_python_production_plus_test"
    )
    assert provenance["expanded_contract_digest"] == expanded_contract_digest(card)
    validate_template_provenance(provenance, expanded_card=card)
    mismatched = ["reproduction"] + ["generic"] * (len(card["validation"]) - 1)
    with pytest.raises(TaskTemplateError, match="template_unclassified"):
        classify_task_card(
            allowed_writes=card["allowed_writes"],
            required_outputs=card["required_outputs"],
            validation=card["validation"],
            validation_roles=mismatched,
            work_kind="generic",
            read_only=False,
            read_first=card["read_first"],
        )


def test_unclassified_raw_card_fails_closed_without_audited_escape():
    raw = {
        "allowed_writes": ["src/odd.txt"],
        "required_outputs": ["src/odd.txt"],
        "validation": ["python -m pytest -q tests/test_missing.py"],
        "work_kind": "generic",
    }
    with pytest.raises(TaskTemplateError, match="template_unclassified"):
        classify_task_card(**raw)
    with pytest.raises(TaskTemplateError, match="custom_escape_invalid"):
        classify_task_card(**raw, custom_escape="not-audited")
    provenance = classify_task_card(**raw, custom_escape=AUDITED_CUSTOM_ESCAPE)
    assert provenance["template_name"] == "custom"
    assert provenance["classification_reason"] == "audited_custom_escape"
    with pytest.raises(TaskTemplateError, match="template_expanded_contract_required"):
        validate_template_provenance(provenance)

    forged_expanded = {**provenance, "expanded_contract_digest": "f" * 64}
    with pytest.raises(TaskTemplateError, match="template_expanded_contract_mismatch"):
        validate_template_provenance(
            forged_expanded, expanded_card=provenance["expanded_contract"]
        )

    forged_digest = "f" * 64
    forged = {
        **provenance,
        "template_full_id": f"custom@v{REGISTRY_VERSION}:{forged_digest}",
        "definition_digest": forged_digest,
    }
    with pytest.raises(TaskTemplateError, match="template_digest_mismatch"):
        validate_template_provenance(
            forged, expanded_card=provenance["expanded_contract"]
        )


@pytest.mark.parametrize("version", [0, REGISTRY_VERSION + 1])
def test_custom_provenance_rejects_stale_and_future_registry_versions(version):
    raw = {
        "allowed_writes": ["src/odd.txt"],
        "required_outputs": ["src/odd.txt"],
        "validation": ["python -m pytest -q tests/test_missing.py"],
        "work_kind": "generic",
    }
    provenance = classify_task_card(**raw, custom_escape=AUDITED_CUSTOM_ESCAPE)
    forged = {
        **provenance,
        "template_full_id": f"custom@v{version}:{provenance['definition_digest']}",
        "registry_version": version,
    }
    with pytest.raises(TaskTemplateError, match="template_provenance_invalid|template_version_stale"):
        validate_template_provenance(forged)


@pytest.mark.parametrize("bad", [7, None, {"cmd": "x"}, b"pytest"])
def test_custom_validation_and_roles_reject_non_string_items(bad):
    with pytest.raises(TaskTemplateError, match="invalid_validation_not_string"):
        validate_custom_validation_roles([bad], ["generic"])
    with pytest.raises(TaskTemplateError, match="invalid_validation_roles_not_string"):
        validate_custom_validation_roles(["git diff --check"], [bad])


def test_custom_validation_rejects_unsafe_embedded_paths():
    with pytest.raises(TaskTemplateError, match="invalid_validation_embedded_path"):
        validate_custom_validation_roles(
            ["python -m pytest -q ../secret.py"], ["generic"]
        )


def test_custom_validation_rejects_bare_dot_token():
    with pytest.raises(TaskTemplateError, match="invalid_validation_embedded_path"):
        validate_custom_validation_roles(
            ["python -m pytest -q ."], ["generic"]
        )


def test_custom_validation_rejects_bare_parent_token():
    with pytest.raises(TaskTemplateError, match="invalid_validation_embedded_path"):
        validate_custom_validation_roles(
            ["python -m pytest -q .."], ["generic"]
        )


def test_custom_validation_allows_non_path_command_tokens():
    validate_custom_validation_roles(["python -m pytest -q"], ["generic"])


def test_custom_validation_and_validation_argv_accept_pytest_node_id():
    command = "pytest tests/test_x.py::test_foo"
    assert validation_argv(command) == ["pytest", "tests/test_x.py::test_foo"]
    validate_custom_validation_roles([command], ["generic"])


def test_custom_validation_rejects_node_id_traversal_and_spoofing():
    for command in (
        "pytest ../secret.py::test_foo",
        "pytest tests/../secret.py::test_foo",
        "pytest tests/test_x.py:test_foo",
        "pytest tests/test_x.py::../test_foo",
        "pytest tests/test_x.py::test_foo;id",
    ):
        with pytest.raises(TaskTemplateError, match="invalid_validation_embedded_path"):
            validate_custom_validation_roles([command], ["generic"])


@pytest.mark.parametrize("compiler", ["g++", "clang++"])
def test_custom_validation_accepts_joined_and_separated_include_roots(compiler):
    joined = f"{compiler} -Isrc/cpu/include -fsyntax-only src/cpu/src/scalar.cpp"
    separated = f"{compiler} -I src/cpu/include -fsyntax-only src/cpu/src/scalar.cpp"
    # Both include-root spellings resolve to the same validated path payload.
    validate_custom_validation_roles([joined], ["generic"])
    validate_custom_validation_roles([separated], ["generic"])


@pytest.mark.parametrize(
    "unsafe",
    [
        "g++ -I/etc/include -fsyntax-only src/cpu/src/scalar.cpp",
        "g++ -I../secret/include -fsyntax-only src/cpu/src/scalar.cpp",
        "g++ -Isrc/../../secret -fsyntax-only src/cpu/src/scalar.cpp",
        "clang++ -I src/../../secret -fsyntax-only src/cpu/src/scalar.cpp",
        "g++ -I.. -fsyntax-only src/cpu/src/scalar.cpp",
    ],
)
def test_custom_validation_rejects_unsafe_include_roots(unsafe):
    with pytest.raises(TaskTemplateError, match="invalid_validation_embedded_path"):
        validate_custom_validation_roles([unsafe], ["generic"])


def test_custom_validation_include_root_does_not_skip_all_dash_tokens():
    # A dash-prefixed token carrying an unsafe absolute path must still be
    # rejected rather than skipped as a bare option.
    with pytest.raises(TaskTemplateError, match="invalid_validation_embedded_path"):
        validate_custom_validation_roles(
            ["g++ -isystem/etc/passwd src/cpu/src/scalar.cpp"], ["generic"]
        )


@pytest.mark.parametrize("compiler", ["g++", "clang++"])
def test_custom_validation_consumes_separated_include_operand(compiler):
    # Empty commands carry no tokens. A separated ``-I`` consumes and validates
    # its next token as the real include path, even a bare single-segment
    # directory that is not itself path-like, matching the joined ``-Idir`` form.
    validate_custom_validation_roles(
        ["", f"{compiler} -I include -c src/cpu/src/scalar.cpp"], ["generic"]
    )
    validate_custom_validation_roles(
        [f"{compiler} -I src/cpu/include -fsyntax-only src/cpu/src/scalar.cpp"],
        ["generic"],
    )


@pytest.mark.parametrize(
    "malformed",
    [
        "g++ -I -c src/cpu/src/scalar.cpp",
        "clang++ -I -fsyntax-only src/cpu/src/scalar.cpp",
        "g++ -fsyntax-only src/cpu/src/scalar.cpp -I",
        "g++ -I",
        "g++ -I ../secret -c src/cpu/src/scalar.cpp",
    ],
)
def test_custom_validation_rejects_malformed_separated_include_option(malformed):
    # A separated ``-I`` whose operand is another option, is missing entirely
    # (dangling), or is an unsafe traversal path is malformed and rejected --
    # never skipped as a bare dash-prefixed token.
    with pytest.raises(TaskTemplateError, match="invalid_validation_embedded_path"):
        validate_custom_validation_roles([malformed], ["generic"])


def test_unchanged_required_public_test_outputs_fail_closed():
    with pytest.raises(
        TaskTemplateError, match="unchanged_required_public_test_output"
    ):
        reject_unchanged_public_test_outputs(
            ["tests/test_a.py"], ["src/a.py", "tests/test_a.py"]
        )


@pytest.mark.parametrize(
    ("allow_path", "required_path"),
    [
        ("./tests/test_a.py", "tests/test_a.py"),
        ("tests//test_a.py", "tests/test_a.py"),
        ("tests\\test_a.py", "tests/test_a.py"),
        ("tests/test_a.py/", "tests/test_a.py"),
        ("tests/test_a.py\\", "tests/test_a.py"),
        ("tests/test_a.py", "./tests/test_a.py"),
        ("tests/test_a.py", "tests//test_a.py"),
        ("tests/test_a.py", "tests\\test_a.py"),
        ("tests/test_a.py", "tests/test_a.py/"),
    ],
)
def test_unchanged_required_public_test_equivalent_spellings_fail_closed(
    allow_path, required_path
):
    with pytest.raises(
        TaskTemplateError, match="unchanged_required_public_test_output"
    ):
        reject_unchanged_public_test_outputs(
            [allow_path], ["src/a.py", required_path]
        )


def test_unchanged_required_non_test_output_is_allowed():
    reject_unchanged_public_test_outputs(
        ["./src//a.py"], ["src/a.py", "tests/test_a.py"]
    )


def test_template_provenance_payload_is_immutable_identity():
    card = expand_template(
        "bugfix_with_regression",
        production_paths=["src/a.py"],
        test_paths=["tests/test_a.py"],
    )
    payload = template_provenance_payload(card, classification_reason="explicit_template")
    assert payload["schema_id"] == "aiworkhub.task_template_provenance.v1"
    assert payload["template_name"] == "bugfix_with_regression"
    assert payload["template_full_id"] == template_full_id("bugfix_with_regression")
    assert SCHEMA_ID == "aiworkhub.task_templates.v1"
    assert payload["expanded_contract_digest"] == expanded_contract_digest(card)
    assert validate_template_provenance(payload, expanded_card=card) == payload


def test_validate_current_provenance_rejects_caller_chosen_expanded_digest():
    card = expand_template(
        "implementation_with_tests",
        production_paths=["src/a.py"],
        test_paths=["tests/test_a.py"],
    )
    provenance = template_provenance_payload(
        card, classification_reason="explicit_template"
    )
    forged = {**provenance, "expanded_contract_digest": "f" * 64}

    with pytest.raises(TaskTemplateError, match="template_expanded_contract_mismatch"):
        validate_template_provenance(forged, expanded_card=card)


def test_current_generated_provenance_reclassifies_unchanged_card():
    card = expand_template(
        "implementation_with_tests",
        production_paths=["src/a.py"],
        test_paths=["tests/test_a.py"],
    )
    provenance = template_provenance_payload(
        card, classification_reason="explicit_template"
    )
    kwargs = {
        key: card[key]
        for key in (
            "allowed_writes",
            "required_outputs",
            "validation",
            "validation_roles",
            "work_kind",
            "read_only",
            "read_first",
            "minimality_contract",
        )
    }
    assert classify_task_card(**kwargs, template_provenance=provenance) == provenance


def test_validate_provenance_rejects_unbound_legacy_expanded_digest():
    card = expand_template(
        "implementation_with_tests",
        production_paths=["src/a.py"],
        test_paths=["tests/test_a.py"],
    )
    provenance = template_provenance_payload(
        card, classification_reason="explicit_template"
    )
    legacy_digest = task_templates_module._legacy_definition_digest(
        task_templates_module.TEMPLATE_SPECS["implementation_with_tests"]
    )
    forged = {
        **provenance,
        "template_full_id": f"implementation_with_tests@v1:{legacy_digest}",
        "definition_digest": legacy_digest,
        "expanded_contract_digest": "0" * 64,
    }
    with pytest.raises(TaskTemplateError, match="template_expanded_contract_mismatch"):
        validate_template_provenance(forged, expanded_card=card)


def test_classify_empty_optional_fields_do_not_wildcard_digest():
    card = expand_template(
        "implementation_with_tests",
        production_paths=["src/mod.py"],
        test_paths=["tests/test_mod.py"],
    )
    stored = {
        "allowed_writes": card["allowed_writes"],
        "required_outputs": card["required_outputs"],
        "validation": card["validation"],
        "validation_roles": card["validation_roles"],
        "work_kind": "generic",
        "read_only": False,
        "read_first": card["read_first"],
    }
    provenance = classify_task_card(**stored)
    stored_digest = expanded_contract_digest(stored)
    assert provenance["expanded_contract_digest"] == stored_digest
    assert stored_digest == expanded_contract_digest(card)
    with pytest.raises(TaskTemplateError, match="template_unclassified"):
        classify_task_card(**{**stored, "read_first": []})
    with pytest.raises(TaskTemplateError, match="template_unclassified"):
        classify_task_card(**{**stored, "read_first": None})
    with pytest.raises(TaskTemplateError, match="template_unclassified"):
        classify_task_card(**{**stored, "validation_roles": []})
    with pytest.raises(TaskTemplateError, match="template_unclassified"):
        classify_task_card(**{**stored, "validation_roles": None})
    omitted = dict(stored)
    omitted.pop("read_first")
    omitted.pop("validation_roles")
    with pytest.raises(TaskTemplateError, match="template_unclassified"):
        classify_task_card(**omitted)


def test_classify_rejects_forged_or_stale_explicit_minimality_contract():
    card = expand_template(
        "implementation_with_tests",
        production_paths=["src/mod.py"],
        test_paths=["tests/test_mod.py"],
    )
    kwargs = {
        key: card[key]
        for key in (
            "allowed_writes",
            "required_outputs",
            "validation",
            "validation_roles",
            "work_kind",
            "read_only",
            "read_first",
        )
    }

    accepted = classify_task_card(
        **kwargs, minimality_contract=card["minimality_contract"]
    )
    assert accepted["expanded_contract_digest"] == expanded_contract_digest(card)

    with pytest.raises(TaskTemplateError, match="template_unclassified"):
        classify_task_card(**kwargs, minimality_contract="forged or stale contract")


def test_classify_binds_matched_minimality_contract_before_provenance(monkeypatch):
    card = expand_template(
        "implementation_with_tests",
        production_paths=["src/mod.py"],
        test_paths=["tests/test_mod.py"],
    )
    original_contract = card["minimality_contract"]
    original_digest = expanded_contract_digest(card)
    real_payload = template_provenance_payload

    def _change_contract_after_match(stored, *, classification_reason):
        monkeypatch.setattr(
            task_templates_module,
            "CANONICAL_MINIMALITY_CONTRACT",
            "future canonical minimality contract",
        )
        assert stored["minimality_contract"] == original_contract
        return real_payload(stored, classification_reason=classification_reason)

    monkeypatch.setattr(
        task_templates_module, "template_provenance_payload", _change_contract_after_match
    )
    provenance = classify_task_card(
        allowed_writes=card["allowed_writes"],
        required_outputs=card["required_outputs"],
        validation=card["validation"],
        validation_roles=card["validation_roles"],
        work_kind=card["work_kind"],
        read_only=card["read_only"],
        read_first=card["read_first"],
    )
    assert provenance["expanded_contract_digest"] == original_digest


def test_omitted_contract_rejects_arbitrary_well_formed_provenance():
    card = expand_template(
        "implementation_with_tests",
        production_paths=["src/mod.py"],
        test_paths=["tests/test_mod.py"],
    )
    persisted = {
        key: card[key]
        for key in (
            "allowed_writes",
            "required_outputs",
            "validation",
            "validation_roles",
            "work_kind",
            "read_only",
            "read_first",
        )
    }
    forged_definition = "11" * 32
    persisted["template_provenance"] = {
        "schema_id": task_templates_module.PROVENANCE_SCHEMA_ID,
        "template_name": "implementation_with_tests",
        "template_full_id": (
            "implementation_with_tests@v1:" + forged_definition
        ),
        "registry_version": 1,
        "definition_digest": forged_definition,
        "classification_reason": "explicit_template",
        "expanded_contract_digest": "22" * 32,
    }

    with pytest.raises(TaskTemplateError, match="template_legacy_identity_invalid"):
        classify_task_card(**persisted)


def test_classify_poisoned_live_template_does_not_reseal_stored_digest(monkeypatch):
    card = expand_template(
        "implementation_with_tests",
        production_paths=["src/mod.py"],
        test_paths=["tests/test_mod.py"],
    )
    stored = {
        "allowed_writes": card["allowed_writes"],
        "required_outputs": card["required_outputs"],
        "validation": card["validation"],
        "validation_roles": card["validation_roles"],
        "work_kind": "generic",
        "read_only": False,
        "read_first": card["read_first"],
    }
    original = classify_task_card(**stored)
    stored_digest = expanded_contract_digest(stored)
    assert original["expanded_contract_digest"] == stored_digest
    real_expand = expand_template

    def _poisoned_expand(template_id, **kwargs):
        expanded = real_expand(template_id, **kwargs)
        expanded["read_first"] = [*expanded["read_first"], "src/poison.py"]
        expanded["validation_roles"] = [*expanded["validation_roles"], "poisoned"]
        return expanded

    monkeypatch.setattr(task_templates_module, "expand_template", _poisoned_expand)
    with pytest.raises(TaskTemplateError, match="template_unclassified"):
        classify_task_card(**stored)
    assert expanded_contract_digest(stored) == stored_digest
    assert original["expanded_contract_digest"] == stored_digest


def test_builtin_provenance_cannot_self_authenticate_embedded_contract():
    card = expand_template(
        "implementation_with_tests",
        production_paths=["src/mod.py"],
        test_paths=["tests/test_mod.py"],
    )
    forged_card = {**card, "validation": ["git diff --check"]}
    forged = dict(
        template_provenance_payload(
            forged_card, classification_reason="explicit_template"
        )
    )

    with pytest.raises(
        TaskTemplateError, match="template_expanded_contract_required"
    ):
        validate_template_provenance(forged)

    provenance = template_provenance_payload(
        card, classification_reason="explicit_template"
    )
    assert validate_template_provenance(provenance) == provenance
    assert validate_template_provenance(provenance, expanded_card=card) == provenance


def test_custom_provenance_requires_audited_escape_and_exact_expanded_card():
    card = {
        "allowed_writes": ["src/odd.txt"],
        "required_outputs": ["src/odd.txt"],
        "validation": ["python -m pytest -q tests/test_missing.py"],
        "validation_roles": ["generic"],
        "work_kind": "generic",
        "read_only": False,
        "read_first": [],
    }
    provenance = classify_task_card(
        **card, custom_escape=AUDITED_CUSTOM_ESCAPE
    )

    with pytest.raises(TaskTemplateError, match="custom_escape_invalid"):
        classify_task_card(**card, template_provenance=provenance)

    forged = {**card, "validation": ["git diff --check"]}
    with pytest.raises(TaskTemplateError, match="template_expanded_contract_mismatch"):
        classify_task_card(
            **forged,
            template_provenance=provenance,
            custom_escape=AUDITED_CUSTOM_ESCAPE,
        )

    assert classify_task_card(
        **card,
        template_provenance=provenance,
        custom_escape=AUDITED_CUSTOM_ESCAPE,
    ) == provenance


_NODE_TEST_PATH_CASES = (
    ("app.test.tsx", True),
    ("foo.spec.mjs", True),
    ("bar.test.jsx", True),
    ("src/app.tsx", False),
    ("lib/foo.mjs", False),
    ("pkg/bar.jsx", False),
)


@pytest.mark.parametrize(("path", "is_test"), _NODE_TEST_PATH_CASES)
def test_node_test_spec_suffixes_partition_and_classify(path, is_test):
    assert _looks_like_test_path(path) is is_test
    production, tests = _partition_write_set(
        ["src/mod.py", "tests/test_mod.py", path]
    )
    if is_test:
        assert path in tests
        assert path not in production
        card = expand_template(
            "cross_boundary_bugfix",
            production_paths=production,
            test_paths=tests,
        )
        assert any(
            command.startswith("node --test ") and path in command
            for command in card["validation"]
        )
        provenance = classify_task_card(
            allowed_writes=card["allowed_writes"],
            required_outputs=card["required_outputs"],
            validation=card["validation"],
            validation_roles=card["validation_roles"],
            work_kind=card["work_kind"],
            read_only=card["read_only"],
            read_first=card["read_first"],
        )
        assert provenance["template_name"] == "cross_boundary_bugfix"
        return
    assert path in production
    assert path not in tests
    card = expand_template(
        "cross_boundary_bugfix",
        production_paths=production,
        test_paths=tests,
    )
    node_commands = [
        command for command in card["validation"] if command.startswith("node ")
    ]
    assert all(path not in command for command in node_commands)
    provenance = classify_task_card(
        allowed_writes=card["allowed_writes"],
        required_outputs=card["required_outputs"],
        validation=card["validation"],
        validation_roles=card["validation_roles"],
        work_kind=card["work_kind"],
        read_only=card["read_only"],
        read_first=card["read_first"],
    )
    assert provenance["template_name"] == "cross_boundary_bugfix"


def test_node_test_suffixes_derived_from_node_suffixes():
    expected = tuple(
        f"{marker}{suffix}"
        for marker in (".test", ".spec")
        for suffix in _NODE_SUFFIXES
    )
    assert _NODE_TEST_SUFFIXES == expected
    for suffix in expected:
        assert _looks_like_test_path(f"outside{suffix}")
        assert not _looks_like_test_path(f"src/prod{suffix[suffix.rfind('.'):]}")


def test_custom_validation_and_roles_reject_unbounded_lists():
    too_many_commands = ["git diff --check"] * (MAX_PATHS_PER_FIELD + 1)
    too_many_roles = ["generic"] * (MAX_PATHS_PER_FIELD + 1)
    with pytest.raises(TaskTemplateError, match=r"^invalid_validation$"):
        validate_custom_validation_roles(too_many_commands, ["generic"])
    with pytest.raises(TaskTemplateError, match=r"^invalid_validation_roles$"):
        validate_custom_validation_roles(["git diff --check"], too_many_roles)
    validate_custom_validation_roles(
        ["git diff --check"] * MAX_PATHS_PER_FIELD,
        ["generic"] * MAX_PATHS_PER_FIELD,
    )


def test_real_suffixless_directory_targets_route_to_python_toolchain():
    card = expand_template("test_only", test_paths=["tests", "tests/unit"])
    assert card["validation"] == [
        "python -m pytest -q tests tests/unit",
        "python -m ruff check tests tests/unit",
        "git diff --check",
    ]


@pytest.mark.parametrize("test_path", ["tests", "tests/unit"])
def test_test_directory_targets_partition_and_provenance_round_trip(test_path):
    assert _looks_like_test_path(test_path)
    production, tests = _partition_write_set(["src/a.py", test_path])
    assert production == ["src/a.py"]
    assert tests == [test_path]

    card = expand_template(
        "bugfix_with_regression",
        production_paths=production,
        test_paths=tests,
    )
    provenance = classify_task_card(
        allowed_writes=card["allowed_writes"],
        required_outputs=card["required_outputs"],
        validation=card["validation"],
        validation_roles=card["validation_roles"],
        work_kind=card["work_kind"],
        read_only=card["read_only"],
        read_first=card["read_first"],
    )
    assert provenance["template_name"] == "bugfix_with_regression"
    assert validate_template_provenance(provenance, expanded_card=card) == provenance


@pytest.mark.parametrize("test_path", ["tests", "tests/unit"])
def test_unchanged_required_test_directory_output_fails_closed(test_path):
    card = expand_template(
        "bugfix_with_regression",
        production_paths=["src/a.py"],
        test_paths=[test_path],
    )
    with pytest.raises(
        TaskTemplateError, match="unchanged_required_public_test_output"
    ):
        reject_unchanged_public_test_outputs([test_path], card["required_outputs"])


def test_suffixless_ordinary_files_never_route_to_pytest_or_ruff():
    card = expand_template(
        "implementation_with_tests",
        production_paths=["Makefile", "LICENSE", "Dockerfile", "src/app.py"],
        test_paths=["tests/test_app.py"],
    )
    assert card["validation"] == [
        "python -m pytest -q tests/test_app.py",
        "python -m ruff check src/app.py tests/test_app.py",
        "git diff --check",
    ]
    assert card["allowed_writes"] == [
        "Makefile",
        "LICENSE",
        "Dockerfile",
        "src/app.py",
        "tests/test_app.py",
    ]
    for command in card["validation"]:
        if command.startswith("python "):
            assert "Makefile" not in command
            assert "LICENSE" not in command
            assert "Dockerfile" not in command


def test_non_python_assets_never_route_to_pytest_or_ruff():
    card = expand_template(
        "implementation_with_tests",
        production_paths=[
            "config.json",
            "pyproject.toml",
            "README.md",
            "assets/logo.png",
            "src/app.py",
        ],
        test_paths=["tests/test_app.py", "tests/data.json"],
    )
    assert card["validation"] == [
        "python -m pytest -q tests/test_app.py",
        "python -m ruff check src/app.py tests/test_app.py",
        "git diff --check",
    ]
    non_python_assets = (
        "config.json",
        "pyproject.toml",
        "README.md",
        ".png",
        "data.json",
    )
    for command in card["validation"]:
        if command.startswith("python "):
            for asset in non_python_assets:
                assert asset not in command


def test_mixed_python_and_javascript_targets_stay_language_separated():
    card = expand_template(
        "implementation_with_tests",
        production_paths=["src/app.py"],
        test_paths=["tests/test_app.py", "tests/app.test.js"],
    )
    assert card["validation"] == [
        "python -m pytest -q tests/test_app.py",
        "python -m ruff check src/app.py tests/test_app.py",
        "node --test tests/app.test.js",
        "git diff --check",
    ]
    python_commands = [c for c in card["validation"] if c.startswith("python ")]
    node_commands = [c for c in card["validation"] if c.startswith("node ")]
    assert all(".js" not in command for command in python_commands)
    assert all(".py" not in command for command in node_commands)


def test_suffixless_files_under_tests_never_route_to_pytest_or_ruff():
    card = expand_template(
        "test_only",
        test_paths=["tests/unit", "tests/Makefile", "tests/fixtures/sample"],
    )
    assert card["validation"] == [
        "python -m pytest -q tests/unit",
        "python -m ruff check tests/unit",
        "git diff --check",
    ]
    assert card["allowed_writes"] == [
        "tests/unit",
        "tests/Makefile",
        "tests/fixtures/sample",
    ]
    for command in card["validation"]:
        if command.startswith("python "):
            assert "Makefile" not in command
            assert "fixtures" not in command
            assert "sample" not in command


def test_lowercase_suffixless_file_under_tests_never_routes():
    # Regression: a lowercase suffixless ordinary file directly under tests/
    # (tests/data, tests/fixture, tests/notes) is not a directory target and must
    # never reach pytest or Ruff.  The removed first-character casing heuristic
    # wrongly classified these lowercase leaves as directories; the explicit
    # allowlist authority routes only the sanctioned tests/unit directory.
    card = expand_template(
        "test_only",
        test_paths=["tests/unit", "tests/data", "tests/fixture", "tests/notes"],
    )
    assert card["validation"] == [
        "python -m pytest -q tests/unit",
        "python -m ruff check tests/unit",
        "git diff --check",
    ]
    assert card["allowed_writes"] == [
        "tests/unit",
        "tests/data",
        "tests/fixture",
        "tests/notes",
    ]
    for command in card["validation"]:
        if command.startswith("python "):
            for leaf in ("data", "fixture", "notes"):
                assert leaf not in command


def test_underscore_prefixed_leaf_under_tests_never_routes():
    # Regression: an underscore-prefixed suffixless leaf under tests/
    # (tests/_helpers) is not an explicitly sanctioned directory target, so it is
    # deterministically excluded from pytest/Ruff rather than guessed from name
    # casing.  The sanctioned tests and tests/unit directory targets still route.
    card = expand_template(
        "test_only",
        test_paths=["tests", "tests/unit", "tests/_helpers"],
    )
    assert card["validation"] == [
        "python -m pytest -q tests tests/unit",
        "python -m ruff check tests tests/unit",
        "git diff --check",
    ]
    assert card["allowed_writes"] == ["tests", "tests/unit", "tests/_helpers"]
    for command in card["validation"]:
        if command.startswith("python "):
            assert "_helpers" not in command


@pytest.mark.parametrize(
    "allow_path",
    [
        "tests/../tests/test_a.py",
        "tests/sub/../test_a.py",
        "./tests/../tests/test_a.py",
        "tests/nested/deeper/../../test_a.py",
    ],
)
def test_unchanged_required_public_test_traversal_aliases_fail_closed(allow_path):
    with pytest.raises(
        TaskTemplateError, match="unchanged_required_public_test_output"
    ):
        reject_unchanged_public_test_outputs(
            [allow_path], ["src/a.py", "tests/test_a.py"]
        )


@pytest.mark.parametrize(
    ("allow_path", "required_path"),
    [
        ("Tests/test_a.py", "tests/test_a.py"),
        ("tests/Test_A.py", "tests/test_a.py"),
        ("TESTS/TEST_A.PY", "tests/test_a.py"),
        ("tests/test_a.py", "Tests/Test_A.py"),
        ("Tests/../Tests/Test_A.py", "tests/test_a.py"),
    ],
)
def test_unchanged_required_public_test_windows_case_aliases_fail_closed(
    allow_path, required_path
):
    with pytest.raises(
        TaskTemplateError, match="unchanged_required_public_test_output"
    ):
        reject_unchanged_public_test_outputs(
            [allow_path], ["src/a.py", required_path]
        )


def test_unchanged_required_traversal_to_non_test_output_is_allowed():
    # Resolves to src/a.py, which is not a public test output, so it is allowed.
    reject_unchanged_public_test_outputs(
        ["tests/../src/a.py"], ["src/a.py", "tests/test_a.py"]
    )


# --- NF-2026-00456: allowed_writes/write_set is authenticated read/write
# *scope*, never an implicit assertion that every path in it must change.
# required_outputs is normally the narrow subset a downstream finalizer must
# observe as changed. Bugfix templates default it to their complete write
# scope; other templates retain the empty default. An explicit mandatory list
# remains authoritative. ---------------------------------------------------


def test_mandatory_changed_outputs_default_to_empty_required_outputs():
    card = expand_template(
        "implementation_with_tests",
        production_paths=["src/app_container.py", "src/app_container_config.py"],
        test_paths=["tests/test_app_container.py"],
    )
    assert card["allowed_writes"] == [
        "src/app_container.py",
        "src/app_container_config.py",
        "tests/test_app_container.py",
    ]
    assert card["write_set"] == card["allowed_writes"]
    assert card["required_outputs"] == []


def test_mandatory_changed_output_out_of_scope_fails_closed():
    with pytest.raises(
        TaskTemplateError, match="mandatory_changed_output_out_of_scope"
    ):
        expand_template(
            "implementation_with_tests",
            production_paths=["src/app_container.py"],
            test_paths=["tests/test_app_container.py"],
            mandatory_changed_outputs=["src/not_in_scope.py"],
        )


@pytest.mark.parametrize(
    ("production_paths", "test_paths", "mandatory_changed_outputs"),
    [
        (
            ["src/app_container.py", "src/app_container_config.py"],
            ["tests/test_app_container.py"],
            ["src/app_container.py"],
        ),
        (
            ["src/model_settings_modal.py", "src/model_settings.css"],
            ["tests/test_model_settings_modal.py"],
            ["src/model_settings_modal.py"],
        ),
    ],
    ids=["appcontainer_style", "model_settings_style"],
)
def test_optional_authorized_path_stays_unchanged_eligible(
    production_paths, test_paths, mandatory_changed_outputs
):
    """Reproduce the AppContainer/Model-Settings regressions: an unrelated
    production or test path a worker is merely authorized to touch must not
    become a mandatory-change target just because it shares write scope with
    the declared fix file."""
    card = expand_template(
        "implementation_with_tests",
        production_paths=production_paths,
        test_paths=test_paths,
        mandatory_changed_outputs=mandatory_changed_outputs,
    )
    assert card["allowed_writes"] == [*production_paths, *test_paths]
    assert card["required_outputs"] == mandatory_changed_outputs
    for path in card["allowed_writes"]:
        if path not in mandatory_changed_outputs:
            assert path not in card["required_outputs"]


def test_mandatory_changed_outputs_can_select_a_test_path():
    card = expand_template(
        "bugfix_with_regression",
        production_paths=["src/a.py"],
        test_paths=["tests/test_a.py"],
        mandatory_changed_outputs=["tests/test_a.py"],
    )
    assert card["required_outputs"] == ["tests/test_a.py"]
    assert "src/a.py" not in card["required_outputs"]


def test_bugfix_explicit_empty_mandatory_outputs_is_authoritative():
    card = expand_template(
        "bugfix_with_regression",
        production_paths=["src/a.py"],
        test_paths=["tests/test_a.py"],
        mandatory_changed_outputs=[],
    )
    assert card["required_outputs"] == []


def test_bugfix_default_test_output_remains_unchanged_terminal_eligible():
    card = expand_template(
        "bugfix_with_regression",
        production_paths=["src/a.py"],
        test_paths=["tests/test_a.py"],
    )
    with pytest.raises(
        TaskTemplateError, match="unchanged_required_public_test_output"
    ):
        reject_unchanged_public_test_outputs(
            ["tests/test_a.py"], card["required_outputs"]
        )


def test_duplicate_mandatory_changed_outputs_fail_closed():
    with pytest.raises(
        TaskTemplateError, match="invalid_mandatory_changed_output_path_duplicate"
    ):
        expand_template(
            "bugfix_with_regression",
            production_paths=["src/a.py"],
            test_paths=["tests/test_a.py"],
            mandatory_changed_outputs=["src/a.py", "src/a.py"],
        )


def test_mandatory_changed_outputs_partially_out_of_scope_fails_closed():
    with pytest.raises(
        TaskTemplateError, match="mandatory_changed_output_out_of_scope"
    ):
        expand_template(
            "bugfix_with_regression",
            production_paths=["src/a.py"],
            test_paths=["tests/test_a.py"],
            mandatory_changed_outputs=["src/a.py", "src/not_in_scope.py"],
        )


def test_bounded_text_names_the_cause_and_the_limit():
    """A one-word refusal made composing a card a bisection against the API.

    Three different causes -- wrong type, empty, over the limit -- all raised a
    bare ``invalid_objective``, and the limit itself was never stated. The path
    validator in the same module already names its cause; this pins that the
    text validator does too.
    """
    from aiworkhub.task_templates import MAX_OBJECTIVE_LENGTH, _bounded_text

    with pytest.raises(TaskTemplateError) as too_long:
        _bounded_text("x" * (MAX_OBJECTIVE_LENGTH + 1), "objective", MAX_OBJECTIVE_LENGTH)
    message = str(too_long.value)
    assert "too_long" in message
    assert str(MAX_OBJECTIVE_LENGTH) in message
    assert str(MAX_OBJECTIVE_LENGTH + 1) in message

    with pytest.raises(TaskTemplateError, match="invalid_objective:empty"):
        _bounded_text("   ", "objective", MAX_OBJECTIVE_LENGTH)

    with pytest.raises(TaskTemplateError, match="invalid_objective:not_a_string:int"):
        _bounded_text(123, "objective", MAX_OBJECTIVE_LENGTH)


def test_bounded_text_still_accepts_multiline_objectives():
    """Newlines are legal here; only paths reject control characters."""
    from aiworkhub.task_templates import MAX_OBJECTIVE_LENGTH, _bounded_text

    text = "first line\nsecond line"
    assert _bounded_text(text, "objective", MAX_OBJECTIVE_LENGTH) == text


# --- package gates are the template's job, not the card author's -------------


PACKAGE_GATE = "tests/test_module_size_ratchet.py"


def _expanded(production, tests):
    return task_templates_module.expand_template(
        "implementation_with_tests", production_paths=production, test_paths=tests
    )


def test_a_package_change_carries_its_repository_gates_without_being_asked():
    """Which gates a change trips is mechanical; a card author must not recall it.

    Two cards in a row got this wrong by hand. AIWORKHUB_01078 omitted the
    ratchets, so a new sqlite connection surfaced only in the full suite AFTER
    acceptance. AIWORKHUB_01079 then included one the sandbox cannot run and
    failed a correct candidate. Both are facts about the repository, so the
    template derives them.
    """
    expanded = _expanded(["src/aiworkhub/skill_registry.py"], ["tests/test_skill_registry.py"])
    gate = [c for c in expanded["validation"] if PACKAGE_GATE in c]
    assert len(gate) == 1
    for name in task_templates_module.PACKAGE_GATE_TESTS:
        assert name in gate[0]
    assert len(expanded["validation_roles"]) == len(expanded["validation"])


@pytest.mark.parametrize(
    ("production", "tests"),
    [
        (["scripts/foo.py"], ["tests/test_foo.py"]),
        (["vscode-extension/src/a.ts"], ["vscode-extension/src/a.test.js"]),
    ],
)
def test_a_change_outside_the_package_carries_no_package_gate(production, tests):
    """The gates measure the package; nothing else should pay for them."""
    expanded = _expanded(production, tests)
    assert not any(PACKAGE_GATE in c for c in expanded["validation"])


def test_one_package_path_among_others_is_enough():
    expanded = _expanded(
        ["scripts/foo.py", "src/aiworkhub/core.py"], ["tests/test_foo.py"]
    )
    assert any(PACKAGE_GATE in c for c in expanded["validation"])


def test_the_gate_set_excludes_every_test_a_worktree_cannot_run():
    """A worker worktree is a sparse checkout with no scripts/ directory.

    tests/test_os_dependency_boundary.py imports check_os_dependency_boundary
    from scripts/, so declaring it kills the run at collection with
    ModuleNotFoundError however correct the work is. It is the manager's gate,
    on the canonical tree.
    """
    assert "tests/test_os_dependency_boundary.py" not in task_templates_module.PACKAGE_GATE_TESTS
    src = Path(task_templates_module.__file__).resolve().parents[2] / "src"
    for name in task_templates_module.PACKAGE_GATE_TESTS:
        body = (src.parent / name).read_text(encoding="utf-8")
        assert "scripts" not in body.split('"""')[0], name


def test_the_gate_is_appended_once_even_for_many_package_paths():
    expanded = _expanded(
        ["src/aiworkhub/a.py", "src/aiworkhub/b.py"], ["tests/test_a.py"]
    )
    assert sum(1 for c in expanded["validation"] if PACKAGE_GATE in c) == 1


def test_code_changing_templates_emit_exact_canonical_minimality_contract():
    for template_id in TEMPLATE_IDS:
        spec = resolve_template(template_id)
        if spec.read_only:
            continue
        if template_id == "cross_boundary_bugfix":
            production_paths = ["src/mod.py", "src/mod.js"]
            test_paths = ["tests/test_mod.py", "tests/mod.test.js"]
        else:
            production_paths = (
                [] if not spec.production_path_policy.allowed else ["src/mod.py"]
            )
            test_paths = (
                [] if not spec.test_path_policy.allowed else ["tests/test_mod.py"]
            )
        card = expand_template(
            template_id,
            production_paths=production_paths,
            test_paths=test_paths,
        )
        assert card["minimality_contract"] == (
            task_templates_module.CANONICAL_MINIMALITY_CONTRACT
        )


def test_current_provenance_binds_persisted_omitted_minimality_contract():
    expanded = expand_template(
        "implementation_with_tests",
        production_paths=["src/mod.py"],
        test_paths=["tests/test_mod.py"],
    )
    provenance = template_provenance_payload(
        expanded, classification_reason="explicit_template"
    )
    persisted = {
        field: expanded[field]
        for field in (
            "allowed_writes",
            "required_outputs",
            "validation",
            "validation_roles",
            "work_kind",
            "read_only",
            "read_first",
        )
    }
    persisted["template_provenance"] = provenance

    assert expanded_contract_digest(persisted) == provenance[
        "expanded_contract_digest"
    ]
    with pytest.raises(TaskTemplateError, match="template_legacy_identity_invalid"):
        classify_task_card(**persisted)
    assert classify_task_card(
        **persisted,
        minimality_contract=task_templates_module.CANONICAL_MINIMALITY_CONTRACT,
    ) == provenance


def test_literal_legacy_v1_card_and_provenance_survive_registry_upgrade(monkeypatch):
    legacy_definition_digest = (
        "023c786094c75429d1ddc081b38c1bb44959c1f52c42f8efd70c680686113dcf"
    )
    legacy_expanded_digest = (
        "144074b6f7f7c8c1895e7a6f4322d65b3d21e0041f3c0d737fc6a404cb37e1f4"
    )
    legacy_card = {
        "allowed_writes": ["src/mod.py", "tests/test_mod.py"],
        "read_first": ["src/mod.py", "tests/test_mod.py"],
        "read_only": False,
        "required_outputs": [],
        "validation": [
            "python -m pytest -q tests/test_mod.py",
            "python -m ruff check src/mod.py tests/test_mod.py",
            "git diff --check",
        ],
        "validation_roles": ["generic", "generic", "generic"],
        "work_kind": "generic",
    }
    legacy_provenance = {
        "schema_id": "aiworkhub.task_template_provenance.v1",
        "template_name": "implementation_with_tests",
        "template_full_id": (
            "implementation_with_tests@v1:" + legacy_definition_digest
        ),
        "registry_version": 1,
        "definition_digest": legacy_definition_digest,
        "classification_reason": "explicit_template",
        "expanded_contract_digest": legacy_expanded_digest,
    }
    legacy_card["template_provenance"] = legacy_provenance

    monkeypatch.setattr(
        task_templates_module,
        "CANONICAL_MINIMALITY_CONTRACT",
        "A later canonical minimality contract.",
    )
    monkeypatch.setattr(task_templates_module, "REGISTRY_VERSION", 2)
    monkeypatch.setattr(task_templates_module, "REGISTRY_VERSION_TOKEN", "v2")

    spec = task_templates_module.TEMPLATE_SPECS["implementation_with_tests"]
    assert task_templates_module._legacy_definition_digest(spec) == (
        legacy_definition_digest
    )
    assert (
        expanded_contract_digest(
            legacy_card,
            trusted_legacy_definition_digest=legacy_definition_digest,
        )
        == legacy_expanded_digest
    )
    with pytest.raises(TaskTemplateError, match="template_version_stale"):
        resolve_template(legacy_provenance["template_full_id"])
    assert (
        validate_template_provenance(
            legacy_provenance, expanded_card=legacy_card
        )
        == legacy_provenance
    )
    assert classify_task_card(**legacy_card) == legacy_provenance

    altered = {**legacy_card, "read_first": ["src/other.py"]}
    with pytest.raises(TaskTemplateError, match="template_legacy_identity_invalid"):
        classify_task_card(**altered)

    for field in ("expanded_contract_digest", "definition_digest"):
        forged = json.loads(json.dumps(legacy_card))
        forged["template_provenance"][field] = "f" * 64
        if field == "definition_digest":
            forged["template_provenance"]["template_full_id"] = (
                "implementation_with_tests@v1:" + ("f" * 64)
            )
        with pytest.raises(TaskTemplateError, match="template_legacy_identity_invalid"):
            classify_task_card(**forged)

    forged_full_id = json.loads(json.dumps(legacy_card))
    forged_full_id["template_provenance"]["template_full_id"] = (
        "implementation_with_tests@v1:" + ("f" * 64)
    )
    with pytest.raises(TaskTemplateError, match="template_legacy_identity_invalid"):
        classify_task_card(**forged_full_id)

    historical_current_digest = task_templates_module._definition_digest(spec)
    arbitrary_historical_current = json.loads(json.dumps(legacy_card))
    arbitrary_historical_current["template_provenance"].update(
        definition_digest=historical_current_digest,
        template_full_id=(
            "implementation_with_tests@v1:" + historical_current_digest
        ),
    )
    with pytest.raises(TaskTemplateError, match="template_legacy_identity_invalid"):
        classify_task_card(**arbitrary_historical_current)


@pytest.mark.parametrize(
    ("template_name", "legacy_card", "legacy_provenance"),
    [
        (
            "read_only_analysis",
            {
                "allowed_writes": [],
                "read_first": ["src/mod.py"],
                "read_only": True,
                "required_outputs": [],
                "validation": [],
                "validation_roles": [],
                "work_kind": "generic",
            },
            {
                "schema_id": "aiworkhub.task_template_provenance.v1",
                "template_name": "read_only_analysis",
                "template_full_id": "read_only_analysis@v1:a13aaaf4cb57d788291f31599aae6eb5e3e84a9e3d4aa203b120eea15e269a12",
                "registry_version": 1,
                "definition_digest": "a13aaaf4cb57d788291f31599aae6eb5e3e84a9e3d4aa203b120eea15e269a12",
                "classification_reason": "explicit_template",
                "expanded_contract_digest": "f7a1f9dd2d848165ec493207bf89068640cb87ca76f960c026173e4142be33a4",
            },
        ),
        (
            "validation_replay",
            {
                "allowed_writes": [],
                "read_first": ["src/mod.py", "tests/test_mod.py"],
                "read_only": True,
                "required_outputs": [],
                "validation": [
                    "python -m pytest -q tests/test_mod.py",
                    "python -m ruff check src/mod.py tests/test_mod.py",
                    "git diff --check",
                ],
                "validation_roles": ["generic", "generic", "generic"],
                "work_kind": "generic",
            },
            {
                "schema_id": "aiworkhub.task_template_provenance.v1",
                "template_name": "validation_replay",
                "template_full_id": "validation_replay@v1:ad7d166c83379c7c7dd3e3952e123ff791b4ba9e968cd1ae8d7b7cef85a1edf5",
                "registry_version": 1,
                "definition_digest": "ad7d166c83379c7c7dd3e3952e123ff791b4ba9e968cd1ae8d7b7cef85a1edf5",
                "classification_reason": "explicit_template",
                "expanded_contract_digest": "9d17be8649778b486d6e3d5741b90ea7c63b3d9ed667d8aa832ed616c8489d15",
            },
        ),
    ],
)
def test_persisted_pre_minimality_read_only_cards_keep_authenticated_identity(
    template_name, legacy_card, legacy_provenance
):
    persisted = {**legacy_card, "template_provenance": legacy_provenance}

    assert expanded_contract_digest(persisted) == legacy_provenance[
        "expanded_contract_digest"
    ]
    assert classify_task_card(**persisted) == legacy_provenance
    assert resolve_template(legacy_provenance["template_full_id"]).name == template_name

    mutated = {**persisted, "read_first": ["src/other.py"]}
    with pytest.raises(TaskTemplateError, match="template_legacy_identity_invalid"):
        classify_task_card(**mutated)


def test_read_only_current_and_legacy_receipts_disambiguate_by_format():
    import hashlib

    card = expand_template("read_only_analysis", production_paths=["src/mod.py"])
    assert "minimality_contract" not in card
    provenance = template_provenance_payload(
        card, classification_reason="explicit_template"
    )

    # For a read-only template the current and legacy definition digests
    # coincide, so a single receipt matches both identities. Authentication must
    # still resolve which expansion format it actually carries.
    spec = task_templates_module.TEMPLATE_SPECS["read_only_analysis"]
    assert task_templates_module._definition_digest(spec) == (
        task_templates_module._legacy_definition_digest(spec)
    )

    # A fresh current receipt survives a plain JSON round trip and binds to its
    # exact card even though its identity also matches the legacy digest.
    roundtripped = json.loads(json.dumps(provenance))
    validated = validate_template_provenance(roundtripped, expanded_card=card)
    assert validated["expanded_contract_digest"] == (
        provenance["expanded_contract_digest"]
    )

    # The current expansion hashes the empty minimality contract; the
    # pre-minimality legacy expansion omitted it, so the two digests differ and
    # each stays valid only for its own declared format.
    legacy_fields = {
        "allowed_writes": list(card.get("allowed_writes") or []),
        "read_first": list(card.get("read_first") or []),
        "read_only": bool(card.get("read_only")),
        "required_outputs": list(card.get("required_outputs") or []),
        "validation": list(card.get("validation") or []),
        "validation_roles": list(card.get("validation_roles") or []),
        "work_kind": str(card.get("work_kind") or "generic"),
    }
    legacy_digest = hashlib.sha256(
        json.dumps(legacy_fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert legacy_digest != provenance["expanded_contract_digest"]
    legacy_receipt = {
        key: value for key, value in roundtripped.items() if key != "expanded_contract"
    }
    legacy_receipt["expanded_contract_digest"] = legacy_digest
    legacy_validated = validate_template_provenance(legacy_receipt, expanded_card=card)
    assert legacy_validated["expanded_contract_digest"] == legacy_digest

    # A forged expansion digest that matches neither format still fails closed.
    forged = {**roundtripped, "expanded_contract_digest": "f" * 64}
    with pytest.raises(TaskTemplateError, match="template_expanded_contract_mismatch"):
        validate_template_provenance(forged, expanded_card=card)


def test_writable_plain_json_receipt_requires_authoritative_minimality():
    # A writable template's expansion carries the canonical minimality contract
    # implicitly. A plain JSON receipt has no bound source card, so it must be
    # authenticated against a card that restores that authoritative value -- the
    # exact normalization real ``create_task`` performs before the embedded
    # comparison.
    card = expand_template(
        "implementation_with_tests",
        production_paths=["src/mod.py"],
        test_paths=["tests/test_mod.py"],
    )
    assert card["minimality_contract"] == (
        task_templates_module.CANONICAL_MINIMALITY_CONTRACT
    )
    provenance = template_provenance_payload(
        card, classification_reason="explicit_template"
    )
    plain = json.loads(json.dumps(provenance))
    assert not hasattr(plain, "expanded_card")

    without_minimality = {
        field: card.get(field)
        for field in (
            "allowed_writes",
            "read_first",
            "read_only",
            "required_outputs",
            "validation",
            "validation_roles",
            "work_kind",
        )
    }
    # Dropping the authoritative minimality contract makes the genuine writable
    # receipt look like a contract mismatch, so it must fail closed.
    with pytest.raises(TaskTemplateError, match="template_expanded_contract_mismatch"):
        validate_template_provenance(plain, expanded_card=without_minimality)

    with_minimality = dict(
        without_minimality, minimality_contract=card["minimality_contract"]
    )
    validated = validate_template_provenance(plain, expanded_card=with_minimality)
    assert validated["expanded_contract_digest"] == (
        provenance["expanded_contract_digest"]
    )

    # A receipt whose embedded minimality contract is stripped no longer matches
    # the authoritative writable card and fails closed even with the correct card.
    tampered = json.loads(json.dumps(provenance))
    tampered["expanded_contract"]["minimality_contract"] = ""
    with pytest.raises(TaskTemplateError, match="template_expanded_contract_mismatch"):
        validate_template_provenance(tampered, expanded_card=with_minimality)


def test_forged_legacy_provenance_cannot_select_weaker_digest_path(monkeypatch):
    legacy_definition_digest = (
        "023c786094c75429d1ddc081b38c1bb44959c1f52c42f8efd70c680686113dcf"
    )
    forged_card = {
        "allowed_writes": ["src/mod.py", "tests/test_mod.py"],
        "read_first": ["src/mod.py", "tests/test_mod.py"],
        "read_only": False,
        "required_outputs": [],
        "validation": [],
        "validation_roles": [],
        "work_kind": "generic",
        "template_provenance": {
            "template_name": "implementation_with_tests",
            "definition_digest": legacy_definition_digest,
        },
    }
    before = expanded_contract_digest(forged_card)
    asserted = expanded_contract_digest(
        forged_card,
        trusted_legacy_definition_digest=legacy_definition_digest,
    )
    assert asserted == before
    monkeypatch.setattr(
        task_templates_module,
        "CANONICAL_MINIMALITY_CONTRACT",
        "A materially revised canonical minimality contract.",
    )
    assert expanded_contract_digest(forged_card) != before


def test_read_only_templates_do_not_promise_code_changes():
    for template_id in ("read_only_analysis", "validation_replay"):
        kwargs = (
            {"test_paths": ["tests/test_mod.py"]}
            if template_id == "validation_replay"
            else {}
        )
        assert "minimality_contract" not in expand_template(template_id, **kwargs)


def test_minimality_contract_is_deterministic_and_digest_bound():
    kwargs = {
        "production_paths": ["src/mod.py"],
        "test_paths": ["tests/test_mod.py"],
    }
    first = expand_template("implementation_with_tests", **kwargs)
    second = expand_template("implementation_with_tests", **kwargs)
    assert first == second
    assert expanded_contract_digest(first) == expanded_contract_digest(second)

    changed = dict(first)
    changed["minimality_contract"] += " changed"
    assert expanded_contract_digest(changed) != expanded_contract_digest(first)


def test_persisted_digest_is_stable_across_builtin_contract_revision(monkeypatch):
    name = "implementation_with_tests"
    kwargs = {
        "production_paths": ["src/mod.py"],
        "test_paths": ["tests/test_mod.py"],
    }
    original = expand_template(name, **kwargs)
    original_full_id = template_full_id(name)

    persisted = dict(original)
    persisted_bytes = json.dumps(
        persisted, sort_keys=True, separators=(",", ":")
    ).encode()
    stored_digest = expanded_contract_digest(json.loads(persisted_bytes))
    stored_provenance = template_provenance_payload(
        json.loads(persisted_bytes), classification_reason="explicit_template"
    )
    assert stored_provenance["expanded_contract_digest"] == stored_digest

    revised_contract = (
        task_templates_module.CANONICAL_MINIMALITY_CONTRACT + " Material revision."
    )
    monkeypatch.setattr(
        task_templates_module,
        "CANONICAL_MINIMALITY_CONTRACT",
        revised_contract,
    )

    assert expanded_contract_digest(json.loads(persisted_bytes)) == stored_digest
    assert (
        template_provenance_payload(
            json.loads(persisted_bytes), classification_reason="explicit_template"
        )["expanded_contract_digest"]
        == stored_digest
    )
    revised = expand_template(name, **kwargs)
    assert revised["minimality_contract"] == revised_contract
    assert revised["template_full_id"] != original["template_full_id"]
    assert template_full_id(name) != original_full_id


def test_code_card_digest_uses_canonical_minimality_when_implicit():
    custom_card = {
        "allowed_writes": ["src/mod.py"],
        "read_first": ["src/mod.py"],
        "read_only": False,
        "required_outputs": ["src/mod.py"],
        "validation": ["python -m pytest -q tests/test_mod.py"],
        "validation_roles": ["behavioral"],
        "work_kind": "generic",
    }
    explicit = {
        **custom_card,
        "minimality_contract": task_templates_module.CANONICAL_MINIMALITY_CONTRACT,
    }

    absent_provenance = task_templates_module._custom_escape_provenance(custom_card)
    explicit_provenance = task_templates_module._custom_escape_provenance(explicit)
    assert absent_provenance["expanded_contract_digest"] == (
        explicit_provenance["expanded_contract_digest"]
    )
    assert absent_provenance == task_templates_module._custom_escape_provenance(
        dict(custom_card)
    )
