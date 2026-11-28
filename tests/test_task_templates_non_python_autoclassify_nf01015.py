"""NF-2026-01015: non-Python cards auto-classify to ``custom_validation``."""

from __future__ import annotations

import pytest

from aiworkhub import task_templates as tt
from aiworkhub.task_templates import (
    AUDITED_CUSTOM_ESCAPE,
    CALLER_VALIDATION_TEMPLATE_NAME,
    CUSTOM_TEMPLATE_NAME,
    TaskTemplateError,
    classify_task_card,
    validate_template_provenance,
)

CPP_WRITES = ["src/main.cpp", "CMakeLists.txt"]
CPP_OUTPUTS = ["src/main.cpp"]
CPP_VALIDATION = ["cmake --build build", "ctest --test-dir build"]


def _classify_cpp(**overrides: object) -> dict:
    kwargs: dict = {
        "allowed_writes": list(CPP_WRITES),
        "required_outputs": list(CPP_OUTPUTS),
        "validation": list(CPP_VALIDATION),
    }
    kwargs.update(overrides)
    return dict(classify_task_card(**kwargs))


def test_cpp_cmake_card_classifies_to_custom_validation_without_escape() -> None:
    provenance = _classify_cpp()
    assert provenance["template_name"] == CALLER_VALIDATION_TEMPLATE_NAME
    assert provenance["classification_reason"] == tt.AUTO_CALLER_VALIDATION_REASON
    assert "auto_selected" in provenance["classification_reason"]
    assert provenance["template_full_id"].startswith(f"{CALLER_VALIDATION_TEMPLATE_NAME}@v")
    assert "escape" not in provenance
    contract = provenance["expanded_contract"]
    assert contract["allowed_writes"] == CPP_WRITES
    assert contract["required_outputs"] == CPP_OUTPUTS
    assert contract["validation"] == CPP_VALIDATION
    assert contract["read_only"] is False


def test_auto_selected_provenance_authenticates_against_its_contract() -> None:
    provenance = _classify_cpp()
    card = dict(provenance["expanded_contract"])
    validated = validate_template_provenance(dict(provenance), expanded_card=card)
    assert validated["template_name"] == CALLER_VALIDATION_TEMPLATE_NAME
    card["template_provenance"] = dict(provenance)
    assert tt._authenticated_current_provenance(card) is not None


def test_auto_selection_is_deterministic() -> None:
    assert _classify_cpp() == _classify_cpp()


@pytest.mark.parametrize(
    "writes,outputs",
    [
        (["src/tool.py"], ["src/tool.py"]),
        (["src/main.cpp", "src/stub.pyi"], ["src/main.cpp"]),
        (["src/main.cpp"], ["src/gen.py"]),
    ],
)
def test_python_card_without_match_still_raises_template_unclassified(
    writes: list[str], outputs: list[str]
) -> None:
    with pytest.raises(TaskTemplateError, match=r"^template_unclassified"):
        classify_task_card(
            allowed_writes=writes,
            required_outputs=outputs,
            validation=list(CPP_VALIDATION),
        )


def test_writable_card_without_required_outputs_is_not_auto_selected() -> None:
    with pytest.raises(TaskTemplateError, match=r"^template_unclassified"):
        _classify_cpp(required_outputs=[])


def test_read_only_card_is_not_auto_selected() -> None:
    with pytest.raises(TaskTemplateError):
        _classify_cpp(read_only=True)


def test_mismatched_declared_read_first_is_not_auto_selected() -> None:
    with pytest.raises(TaskTemplateError, match=r"^template_unclassified"):
        _classify_cpp(read_first=["docs/unrelated.md"])


def test_audited_escape_path_is_unchanged() -> None:
    provenance = _classify_cpp(custom_escape=AUDITED_CUSTOM_ESCAPE)
    assert provenance["template_name"] == CUSTOM_TEMPLATE_NAME
    assert provenance["classification_reason"] == "audited_custom_escape"


def test_invalid_escape_still_refused() -> None:
    with pytest.raises(TaskTemplateError, match=r"^custom_escape_invalid"):
        _classify_cpp(custom_escape="something_else")


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest -q tests/test_missing.py",
        "python3 -m json.tool data/report.json",
        "pytest -q tests/test_missing.py",
        "py -m pytest -q",
        ".venv/bin/python -m pytest -q",
        "C:/Python312/python.exe -m pytest -q",
        "PYTHONPATH=src python3.12 -m pytest -q",
        "env FOO=1 pytest -q",
        "/usr/bin/env python3 -m pytest -q x",
        "uv run pytest",
        ".venv/Scripts/python.exe -m pytest",
    ],
)
def test_non_python_writes_with_python_validation_stay_unclassified(
    command: str,
) -> None:
    # Absolute interpreter paths are refused earlier by the validation path
    # gate (invalid_validation_*); every other spelling reaches the classifier
    # and must end in template_unclassified. Neither is ever auto-selected.
    with pytest.raises(
        TaskTemplateError, match=r"^(template_unclassified|invalid_validation)"
    ):
        classify_task_card(
            allowed_writes=["src/odd.txt"],
            required_outputs=["src/odd.txt"],
            validation=[command],
        )
    assert tt._is_python_validation_command(command) is True
    card_view = {
        "allowed_writes": ["src/odd.txt"],
        "required_outputs": ["src/odd.txt"],
        "validation": [command],
        "read_only": False,
    }
    assert tt._auto_select_caller_validation(card_view, minimality_contract=None) is None


@pytest.mark.parametrize(
    "command", ["uv run pytest", ".venv/Scripts/python.exe -m pytest"]
)
def test_relative_python_launchers_raise_template_unclassified(command: str) -> None:
    with pytest.raises(TaskTemplateError, match=r"^template_unclassified"):
        classify_task_card(
            allowed_writes=["src/odd.txt"],
            required_outputs=["src/odd.txt"],
            validation=[command],
        )


@pytest.mark.parametrize(
    "head",
    ["/usr/bin/env", "C:\\Windows\\env.exe", "ENV", "pythonw", "py.test", "poetry",
     "tox", "nox", "coverage", "/usr/bin/python3", "pythonw3.12"],
)
def test_head_normalization_counts_python_launchers(head: str) -> None:
    command = f"{head} python3 -m pytest" if "env" in head.lower() else f"{head} x"
    assert tt._is_python_validation_command(command) is True


def test_non_python_validation_heads_are_not_python() -> None:
    for command in CPP_VALIDATION + ["make -C build test", "cargo test", "npm test"]:
        assert tt._is_python_validation_command(command) is False


def test_mixed_validation_with_one_python_command_stays_unclassified() -> None:
    with pytest.raises(TaskTemplateError, match=r"^template_unclassified"):
        _classify_cpp(validation=["cmake --build build", "python3 -m pytest -q"])


def test_card_declaring_validation_roles_is_not_auto_selected() -> None:
    with pytest.raises(TaskTemplateError, match=r"^template_unclassified"):
        _classify_cpp(validation_roles=["generic", "generic"])


def test_failed_custom_validation_roles_check_falls_back_to_unclassified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_check = tt.validate_custom_validation_roles
    calls: list[object] = []

    def _refusing_check(validation: object, validation_roles: object) -> None:
        if validation_roles is None and validation == CPP_VALIDATION:
            calls.append(validation)
            raise TaskTemplateError("invalid_validation")
        real_check(validation, validation_roles)

    card_view = {
        "allowed_writes": list(CPP_WRITES),
        "required_outputs": list(CPP_OUTPUTS),
        "validation": list(CPP_VALIDATION),
        "read_only": False,
    }
    monkeypatch.setattr(tt, "validate_custom_validation_roles", _refusing_check)
    # None makes classify_task_card raise template_unclassified, not a new code.
    assert tt._auto_select_caller_validation(card_view, minimality_contract=None) is None
    assert calls


@pytest.mark.parametrize(
    "command",
    ["env -i pytest -q", "env -u FOO python3 -m pytest", "/usr/bin/env -S cmake --build b"],
)
def test_env_option_flags_fail_closed(command: str) -> None:
    assert tt._is_python_validation_command(command) is True
