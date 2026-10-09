from __future__ import annotations

import re

import pytest

from aiworkhub.task_templates import (
    AUDITED_CUSTOM_ESCAPE,
    CALLER_VALIDATION_TEMPLATE_NAME,
    TEMPLATE_IDS,
    TaskTemplateError,
    classify_task_card,
)

_RAW_CARD = {
    "allowed_writes": ["src/odd.txt"],
    "required_outputs": ["src/odd.txt"],
    "validation": ["python -m pytest -q tests/test_missing.py"],
    "work_kind": "generic",
}

_VALID_FIELDS = {
    "expand_error",
    "allowed_writes",
    "required_outputs",
    "validation",
    "read_only",
    "work_kind",
    "read_first",
    "validation_roles",
    "minimality_contract",
}


def test_custom_escape_invalid_names_field_and_accepted_value():
    with pytest.raises(TaskTemplateError) as excinfo:
        classify_task_card(**_RAW_CARD, custom_escape="not-audited")
    expected = (
        f"custom_escape_invalid:custom_template_escape expected "
        f"{AUDITED_CUSTOM_ESCAPE!r}, got {'not-audited'!r}"
    )
    assert str(excinfo.value) == expected


def test_custom_escape_invalid_via_template_provenance_branch():
    with pytest.raises(TaskTemplateError) as excinfo:
        classify_task_card(
            **_RAW_CARD,
            template_provenance={"template_name": "custom"},
            custom_escape="not-audited",
        )
    expected = (
        f"custom_escape_invalid:custom_template_escape expected "
        f"{AUDITED_CUSTOM_ESCAPE!r}, got {'not-audited'!r}"
    )
    assert str(excinfo.value) == expected


def test_template_unclassified_names_closest_template_and_field():
    with pytest.raises(TaskTemplateError) as excinfo:
        classify_task_card(**_RAW_CARD)
    message = str(excinfo.value)
    pattern = (
        r"^template_unclassified:no registered template matched; "
        r"closest (?P<template>\S+): (?P<field>\S+) differs; or pass "
        r"custom_template_escape='audited_custom_unclassified'$"
    )
    match = re.match(pattern, message)
    assert match is not None
    assert match.group("template") in set(TEMPLATE_IDS) | {
        CALLER_VALIDATION_TEMPLATE_NAME
    }
    assert match.group("field") in _VALID_FIELDS
    assert len(message.encode("utf-8")) <= 512


def test_bare_refusal_codes_remain_matchable_prefixes():
    with pytest.raises(TaskTemplateError, match="^custom_escape_invalid"):
        classify_task_card(**_RAW_CARD, custom_escape="not-audited")
    with pytest.raises(TaskTemplateError, match="^template_unclassified"):
        classify_task_card(**_RAW_CARD)
