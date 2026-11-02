"""NF-2026-01956: read_first entries with a ``:N``/``:N-M`` suffix classify.

``reject_review``'s ``read_first_amendment`` accepts read_first entries with a
line-range suffix, so a docs_change card declaring ``docs/x.md:270-290`` must
classify (and re-authenticate) exactly as its bare-path twin does. Only the
valid suffix grammar is stripped; anything else still fails closed.
"""

from __future__ import annotations

from typing import Any

import pytest

from aiworkhub import task_templates
from aiworkhub.task_templates import TaskTemplateError, classify_task_card, expand_template

DOC = "docs/x.md"


def _docs_card(read_first: list[str]) -> dict[str, Any]:
    expanded = expand_template("docs_change", production_paths=[DOC], test_paths=[])
    assert expanded["read_first"] == [DOC]
    return {
        "allowed_writes": list(expanded["allowed_writes"]),
        "required_outputs": list(expanded["required_outputs"]),
        "validation": list(expanded["validation"]),
        "validation_roles": list(expanded["validation_roles"]),
        "work_kind": expanded["work_kind"],
        "read_only": expanded["read_only"],
        "read_first": read_first,
        "minimality_contract": expanded.get("minimality_contract"),
    }


def _classify(card: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return classify_task_card(**card, **extra)


def test_bare_path_still_classifies_as_docs_change() -> None:
    result = _classify(_docs_card([DOC]))
    assert result["template_name"] == "docs_change"


@pytest.mark.parametrize("entry", [f"{DOC}:270-290", f"{DOC}:5", f"{DOC}:7-7"])
def test_line_range_suffix_classifies_as_docs_change(entry: str) -> None:
    card = _docs_card([entry])
    result = _classify(card)
    assert result["template_name"] == "docs_change"
    assert result["expanded_contract"]["read_first"] == [entry]
    # Launch re-authenticates the persisted card against a fresh expansion;
    # the suffixed entry must survive that comparison too.
    again = _classify(card, template_provenance=dict(result))
    assert again["template_name"] == "docs_change"
    assert again["expanded_contract_digest"] == result["expanded_contract_digest"]


@pytest.mark.parametrize(
    "entry",
    [
        f"{DOC}:abc",
        f"{DOC}:9-3",
        f"{DOC}:0",
        f"{DOC}:0-4",
        f"{DOC}:",
        f"{DOC}:3-",
        f"{DOC}:-3",
        f"{DOC}:1-2-3",
        f"{DOC}:270-290\n",
        f"{DOC}:٢٧٠-٢٩٠",
        "docs/other.md:5",
    ],
)
def test_invalid_suffix_is_not_stripped(entry: str) -> None:
    if not entry.startswith("docs/other.md"):
        assert task_templates._read_first_entry_path(entry) == entry
    with pytest.raises(TaskTemplateError) as excinfo:
        _classify(_docs_card([entry]))
    assert "template_unclassified" in str(excinfo.value)


@pytest.mark.parametrize(
    ("entry", "path"),
    [
        (f"{DOC}:270-290", DOC),
        (f"{DOC}:5", DOC),
        (DOC, DOC),
        (f"{DOC}:abc", f"{DOC}:abc"),
        (f"{DOC}:9-3", f"{DOC}:9-3"),
    ],
)
def test_suffix_grammar(entry: str, path: str) -> None:
    assert task_templates._read_first_entry_path(entry) == path


def test_other_fields_are_not_loosened() -> None:
    card = _docs_card([f"{DOC}:1-2"])
    card["allowed_writes"] = [f"{DOC}:1-2"]
    with pytest.raises(TaskTemplateError):
        _classify(card)
    card = _docs_card([f"{DOC}:1-2"])
    card["required_outputs"] = [f"{DOC}:1-2"]
    with pytest.raises(TaskTemplateError):
        _classify(card)


def test_read_first_count_must_still_match() -> None:
    with pytest.raises(TaskTemplateError):
        _classify(_docs_card([f"{DOC}:1-2", f"{DOC}:3-4"]))
    with pytest.raises(TaskTemplateError):
        _classify(_docs_card([]))
