from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub.quality_evidence import normalize_behavioral_contract
from aiworkhub.task_templates import (
    REGISTRY_VERSION,
    SCHEMA_ID,
    TEMPLATE_IDS,
    TaskTemplateError,
    _canonical_work_kind,
    _validation_roles_for,
    expand_template,
    resolve_template,
    split_command_argv,
    template_full_id,
)


def test_registry_has_exactly_six_stable_template_ids():
    assert TEMPLATE_IDS == (
        "read_only_analysis",
        "bugfix_with_regression",
        "implementation_with_tests",
        "test_only",
        "docs_change",
        "validation_replay",
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


def test_validation_roles_unsatisfiable_when_commands_cannot_cover_required():
    with pytest.raises(TaskTemplateError, match="validation_roles_unsatisfiable"):
        _validation_roles_for("bugfix", ["python -m pytest -q tests/test_a.py"])


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
