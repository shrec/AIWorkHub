"""NF-2026-01642: the stdlib MCP tools/list publishes what the FastMCP path registers."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub.mcp_tool_metadata import derive_annotations, split_docstring  # noqa: E402

_PAYLOAD_PREFIX = "NF01642_PAYLOAD="
_REJECT_REVIEW = "aiworkhub_task_reject_review"
_REJECT_REVIEW_TRUNCATED = "Write-gated Codex action: reject a reviewed task with exact feedback and"
_HINTED_PARAMS = {
    "aiworkhub_task_create": (
        "task_id", "title", "runner", "topic", "objective", "acceptance", "allowed_writes",
        "forbidden", "required_outputs", "allow_empty_required_outputs",
        "allow_unchanged_required_outputs", "validation", "priority", "task_type", "depends_on",
        "read_first", "immutable_inputs", "read_only", "max_live_tokens", "work_kind",
        "validation_roles", "risk_tier", "difficulty", "skill_task_family", "skill_stage",
        "skill_triggers", "skill_applicability", "skill_path_scope", "custom_template_escape",
        "apply_contract_patch", "echo_card", "wave_goal_binding",
    ),
    "needfix_add": ("status", "kind", "severity"),
    "needfix_update": ("kind", "severity"),
}

_CHILD_PREAMBLE = """
try:
    import sitecustomize  # noqa: F401 - -S skips site; the AppContainer lane shim must still load
except ImportError:
    pass
import asyncio
import inspect
import io
import json
import typing
from aiworkhub import server
"""

_STDLIB_BODY = """
assert not server._MCP_SDK_AVAILABLE
tools = server.mcp._tools
reply = server._stdio_dispatch("AIWorkHub MCP", tools, "tools/list", None, "", 1)
sink = io.BytesIO()
server._stdio_write_message(sink, {"jsonrpc": "2.0", "id": 1, "result": reply})


def sample(
    plain: str,
    nested: typing.Optional[typing.Annotated[str, server._task_create_hint("nested hint", ["a"])]] = None,
    hinted: typing.Annotated[str, server._task_create_hint("own wins", ["b"])] = "x",
    documented: int = 3,
    bare: bool = False,
):
    '''Summary line.

    More detail.

    Args:
        hinted: docstring text loses to the own hint
        documented: documented text
            continued here.
        gone: not a parameter

    Returns:
        A dict.
    '''


def read_only_sample():
    '''READ-ONLY: lists things.'''


def sample_purge():
    '''Removes things.'''


def plain_sample():
    '''Plain.'''


synthetic = {
    "sample": sample,
    "read_only_sample": read_only_sample,
    "sample_purge": sample_purge,
    "plain_sample": plain_sample,
}
print(PREFIX + json.dumps({
    "wire": json.loads(sink.getvalue().decode("utf-8")),
    "wire_bytes": len(sink.getvalue()),
    "wire_cap": server._FALLBACK_MAX_RESPONSE_BYTES,
    "docs": {name: inspect.getdoc(func) or "" for name, func in tools.items()},
    "synthetic": server._stdio_tools_list(synthetic)["tools"],
    "synthetic_doc": inspect.getdoc(sample),
}))
"""

_SDK_BODY = """
assert server._MCP_SDK_AVAILABLE
listed = asyncio.run(server.mcp.list_tools())
print(PREFIX + json.dumps([
    {
        "name": tool.name,
        "description": tool.description or "",
        "inputSchema": tool.inputSchema,
        "annotations": tool.annotations.model_dump(exclude_none=True) if tool.annotations else None,
    }
    for tool in listed
]))
"""


def _run_child(backend: str, body: str):
    # Extend PYTHONPATH, never replace it: the AppContainer validation lane keeps its
    # path-resolution shim and site-packages there. The -S stdlib child drops every entry
    # holding the SDK, or -S could not hide mcp/pydantic.
    inherited = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    if backend == "stdlib":
        inherited = [p for p in inherited if not (Path(p, "mcp").is_dir() or Path(p, "pydantic").is_dir())]
    env = dict(
        os.environ,
        AIWORKHUB_ALLOW_WRITES="1",
        AIWORKHUB_MCP_STDIO_BACKEND=backend,
        PYTHONPATH=os.pathsep.join([str(_SRC), *inherited]),
    )
    script = f"PREFIX = {_PAYLOAD_PREFIX!r}\n{_CHILD_PREAMBLE}{body}"
    argv = [sys.executable, *(["-S"] if backend == "stdlib" else []), "-c", script]
    completed = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr[-4000:]
    lines = [line for line in completed.stdout.splitlines() if line.startswith(_PAYLOAD_PREFIX)]
    assert len(lines) == 1, completed.stdout[-2000:]
    return json.loads(lines[0][len(_PAYLOAD_PREFIX):])


def _has_args_block(doc: str) -> bool:
    return any(line.strip() in ("Args:", "Parameters:") for line in doc.splitlines())


def _expected_description(doc: str) -> str:
    # FastMCP moves an Args block into per-parameter descriptions (split_docstring).
    return split_docstring(doc)[0].strip() if _has_args_block(doc) else doc.strip()


def _parameter_descriptions(entry: dict) -> dict:
    properties = entry["inputSchema"].get("properties", {})
    return {
        pname: spec["description"]
        for pname, spec in properties.items()
        if isinstance(spec, dict) and "description" in spec
    }


@pytest.fixture(scope="module")
def stdlib_payload() -> dict:
    return _run_child("stdlib", _STDLIB_BODY)


@pytest.fixture(scope="module")
def fallback_tools(stdlib_payload: dict) -> dict:
    wire = stdlib_payload["wire"]
    assert "error" not in wire, wire
    tools = {entry["name"]: entry for entry in wire["result"]["tools"]}
    assert tools
    assert set(tools) == set(stdlib_payload["docs"])
    return tools


def test_tools_list_response_still_fits_the_fallback_response_cap(stdlib_payload: dict) -> None:
    assert "error" not in stdlib_payload["wire"], stdlib_payload["wire"]
    assert stdlib_payload["wire_bytes"] < stdlib_payload["wire_cap"]


def test_every_description_is_the_full_docstring(stdlib_payload: dict, fallback_tools: dict) -> None:
    docs = stdlib_payload["docs"]
    mismatched = [
        name for name, entry in fallback_tools.items()
        if entry["description"] != _expected_description(docs[name])
    ]
    assert not mismatched, mismatched[:10]
    assert [name for name, doc in docs.items() if not _has_args_block(doc)]


def test_reject_review_description_is_not_truncated_to_its_first_line(
    stdlib_payload: dict, fallback_tools: dict
) -> None:
    description = fallback_tools[_REJECT_REVIEW]["description"]
    assert description == _expected_description(stdlib_payload["docs"][_REJECT_REVIEW])
    assert description != _REJECT_REVIEW_TRUNCATED
    assert description.startswith(_REJECT_REVIEW_TRUNCATED)
    assert "reject_reason_too_large" in description
    assert description.count("\n") > 3


def test_no_description_is_a_truncated_prefix_of_its_docstring(
    stdlib_payload: dict, fallback_tools: dict
) -> None:
    truncated = []
    multi_line = []
    for name, entry in fallback_tools.items():
        full = stdlib_payload["docs"][name].strip()
        if _has_args_block(full):
            continue
        if "\n" in full:
            multi_line.append(name)
        if entry["description"] != full and full.startswith(entry["description"]):
            truncated.append(name)
    assert not truncated, truncated[:10]
    assert multi_line


def test_annotations_come_from_the_shared_derivation_and_are_omitted_when_empty(
    fallback_tools: dict,
) -> None:
    mismatched = []
    for name, entry in fallback_tools.items():
        expected = derive_annotations(name, entry["description"])
        if entry.get("annotations") != expected or ("annotations" in entry) != (expected is not None):
            mismatched.append(name)
    assert not mismatched, mismatched[:10]
    assert [name for name, entry in fallback_tools.items() if entry.get("annotations") == {"readOnlyHint": True}]
    assert [name for name, entry in fallback_tools.items() if "annotations" not in entry]


def test_annotation_shapes_for_read_only_destructive_and_plain_tools(stdlib_payload: dict) -> None:
    synthetic = {entry["name"]: entry for entry in stdlib_payload["synthetic"]}
    assert synthetic["read_only_sample"]["annotations"] == {"readOnlyHint": True}
    assert synthetic["sample_purge"]["annotations"] == {"readOnlyHint": False, "destructiveHint": True}
    assert "annotations" not in synthetic["plain_sample"]
    assert "annotations" not in synthetic["sample"]


def test_docstring_args_entries_become_parameter_descriptions(
    stdlib_payload: dict, fallback_tools: dict
) -> None:
    problems = []
    checked = 0
    for name, entry in fallback_tools.items():
        documented = split_docstring(stdlib_payload["docs"][name])[1]
        properties = entry["inputSchema"]["properties"]
        for pname, text in documented.items():
            if pname not in properties or pname in _HINTED_PARAMS.get(name, ()):
                continue
            checked += 1
            if properties[pname].get("description") != text:
                problems.append((name, pname))
    assert checked
    assert not problems, problems[:10]


def test_field_hints_become_parameter_descriptions_without_changing_types(fallback_tools: dict) -> None:
    missing = [
        (tool, pname)
        for tool, pnames in _HINTED_PARAMS.items()
        for pname in pnames
        if not fallback_tools[tool]["inputSchema"]["properties"][pname].get("description")
    ]
    assert not missing, missing
    properties = fallback_tools["aiworkhub_task_create"]["inputSchema"]["properties"]
    assert properties["difficulty"]["description"].startswith(
        "Accepted difficulty values: bounded, standard, complex."
    )
    assert properties["validation"]["description"].startswith("shell-free executable commands")
    undescribed = {key: value for key, value in properties["validation"].items() if key != "description"}
    assert undescribed == {"type": "array", "items": {"type": "string"}}


def test_parameters_without_a_description_source_publish_none(
    stdlib_payload: dict, fallback_tools: dict
) -> None:
    unexpected = []
    for name, entry in fallback_tools.items():
        documented = split_docstring(stdlib_payload["docs"][name])[1]
        allowed = set(documented) | set(_HINTED_PARAMS.get(name, ()))
        for pname, spec in entry["inputSchema"]["properties"].items():
            if "description" in spec and pname not in allowed:
                unexpected.append((name, pname))
    assert not unexpected, unexpected[:10]


def test_reject_review_schema_keeps_its_names_types_and_required_list(fallback_tools: dict) -> None:
    schema = fallback_tools[_REJECT_REVIEW]["inputSchema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["task_id", "reason"]
    assert set(schema["properties"]) == {
        "task_id", "reason", "to", "residual_identities", "predecessor_request_id",
        "failure_category", "validation_amendment", "include_card",
        "allowed_writes_amendment", "lesson_candidate", "no_new_lesson_reason",
        "promote_ai_memory", "extra_evidence_ids",
    }
    assert schema["properties"]["task_id"]["type"] == "string"
    assert schema["properties"]["residual_identities"]["type"] == "array"
    assert all(str(spec.get("description", "")).strip() for spec in schema["properties"].values())


def test_parameter_description_sources_and_precedence(stdlib_payload: dict) -> None:
    sample = {entry["name"]: entry for entry in stdlib_payload["synthetic"]}["sample"]
    assert sample["inputSchema"]["properties"] == {
        "plain": {"type": "string"},
        # FastMCP puts a nested Optional[Annotated[...]] description inside
        # anyOf, never at the top level, so the fallback emits none here.
        "nested": {"type": "string"},
        "hinted": {"type": "string", "description": "own wins"},
        "documented": {"type": "integer", "description": "documented text continued here."},
        "bare": {"type": "boolean"},
    }
    assert sample["inputSchema"]["required"] == ["plain"]
    description = sample["description"]
    assert description == split_docstring(stdlib_payload["synthetic_doc"])[0].strip()
    assert description.startswith("Summary line.\n\nMore detail.")
    assert description.endswith("A dict.")
    assert "Args:" not in description and "gone" not in description


@pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="mcp SDK is not importable, so the FastMCP tools/list cannot be compared",
)
def test_fallback_matches_the_fastmcp_tools_list(fallback_tools: dict) -> None:
    reference = {entry["name"]: entry for entry in _run_child("sdk", _SDK_BODY)}
    assert set(reference) == set(fallback_tools)
    differences = []
    for name, entry in sorted(fallback_tools.items()):
        expected = reference[name]
        if entry["description"] != expected["description"].strip():
            differences.append((name, "description", entry["description"][:120], expected["description"][:120]))
        if entry.get("annotations") != expected["annotations"]:
            differences.append((name, "annotations", entry.get("annotations"), expected["annotations"]))
        if _parameter_descriptions(entry) != _parameter_descriptions(expected):
            differences.append(
                (name, "parameter descriptions", _parameter_descriptions(entry), _parameter_descriptions(expected))
            )
    assert not differences, differences[:5]


def test_no_parameter_publishes_empty_examples(fallback_tools: dict) -> None:
    empty = sorted(
        f"{name}.{pname}"
        for name, entry in fallback_tools.items()
        for pname, spec in entry["inputSchema"].get("properties", {}).items()
        if isinstance(spec, dict) and spec.get("examples") == []
    )
    assert not empty, empty[:10]
