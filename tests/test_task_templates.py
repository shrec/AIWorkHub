from __future__ import annotations

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
    assert card["required_outputs"] == expected
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
    assert card["required_outputs"] == ["tests/test_a.py"]
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
    assert card["required_outputs"] == ["docs/guide.md"]
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
    validate_template_provenance(provenance)
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
    validate_template_provenance(provenance)


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
    assert validate_template_provenance(payload) == payload


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
