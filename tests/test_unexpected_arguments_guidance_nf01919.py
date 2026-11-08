"""NF-2026-01919: unknown tool arguments get accepted names, a did-you-mean and two aliases."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from aiworkhub import stdio_fastmcp  # noqa: E402
from aiworkhub.tool_recovery import (  # noqa: E402
    missing_required_refusal,
    resolve_tool_arguments,
)


def _search(query: str, budget: int = 10) -> dict:
    return {"query": query, "budget": budget}


def _needfix(needfix_id: str, reason: str = "") -> dict:
    return {"needfix_id": needfix_id, "reason": reason}


def _task(task_id: str, request_id: str = "") -> dict:
    return {"task_id": task_id, "request_id": request_id}


def test_unknown_argument_lists_accepted_names_and_did_you_mean() -> None:
    _, message = resolve_tool_arguments(_search, {"query": "x", "budgt": 3})
    assert message is not None
    assert message.startswith("unexpected_arguments:")
    assert "['budgt']" in message
    assert "accepted: budget, query" in message
    assert "did_you_mean: budgt -> budget" in message


def test_unknown_argument_without_close_name_has_no_hint() -> None:
    _, message = resolve_tool_arguments(_search, {"query": "x", "zzzzzz": 1})
    assert message is not None and "did_you_mean" not in message


def test_limit_maps_to_budget_only_when_budget_exists_and_alias_does_not() -> None:
    resolved, message = resolve_tool_arguments(_search, {"query": "x", "limit": 7})
    assert message is None and resolved == {"query": "x", "budget": 7}

    def has_limit(limit: int = 1, budget: int = 2) -> None:
        return None

    resolved, message = resolve_tool_arguments(has_limit, {"limit": 7})
    assert message is None and resolved == {"limit": 7}

    def no_target(query: str) -> None:
        return None

    _, message = resolve_tool_arguments(no_target, {"query": "x", "limit": 7})
    assert message is not None and message.startswith("unexpected_arguments:")


@pytest.mark.parametrize("alias", ["id", "nf_id"])
def test_id_aliases_map_to_needfix_id(alias: str) -> None:
    resolved, message = resolve_tool_arguments(_needfix, {alias: "NF-1"})
    assert message is None and resolved == {"needfix_id": "NF-1"}


def test_alias_with_target_or_two_aliases_stay_errors() -> None:
    resolved, message = resolve_tool_arguments(_search, {"query": "x", "limit": 1, "budget": 2})
    assert message is not None and "alias_conflict" in message
    assert resolved["budget"] == 2

    _, message = resolve_tool_arguments(_needfix, {"id": "a", "nf_id": "b"})
    assert message is not None and "alias_conflict" in message

    _, message = resolve_tool_arguments(_needfix, {"id": "a", "needfix_id": "b"})
    assert message is not None and "alias_conflict" in message


def test_task_id_and_request_id_are_never_aliased() -> None:
    _, message = resolve_tool_arguments(_task, {"request_id": "r"})
    assert message is None

    def only_task(task_id: str) -> None:
        return None

    _, message = resolve_tool_arguments(only_task, {"request_id": "r"})
    assert message is not None and message.startswith("unexpected_arguments:")
    assert "task_id" in message

    def only_request(request_id: str) -> None:
        return None

    resolved, message = resolve_tool_arguments(only_request, {"task_id": "t"})
    assert message is not None and "request_id" in message
    assert "request_id" not in resolved


def test_accepted_list_is_capped() -> None:
    namespace: dict = {}
    names = [f"p{index:03d}" for index in range(60)]
    exec(f"def many({', '.join(name + '=None' for name in names)}): return None", namespace)  # noqa: S102
    _, message = resolve_tool_arguments(namespace["many"], {"bogus": 1})
    assert message is not None
    assert "p039" in message and "p040" not in message
    assert "(+20 more)" in message


def test_uninspectable_callable_accepts_everything() -> None:
    resolved, message = resolve_tool_arguments(object(), {"anything": 1})
    assert message is None and resolved == {"anything": 1}


def test_fastmcp_path_applies_aliases_and_guidance() -> None:
    tools = {"search": _search, "needfix": _needfix}
    result = stdio_fastmcp._tool_result(
        tools, {"name": "search", "arguments": {"query": "q", "limit": 4}}
    )
    assert json.loads(result["content"][0]["text"]) == {"query": "q", "budget": 4}
    result = stdio_fastmcp._tool_result(
        tools, {"name": "needfix", "arguments": {"nf_id": "NF-9"}}
    )
    assert json.loads(result["content"][0]["text"])["needfix_id"] == "NF-9"

    with pytest.raises(stdio_fastmcp.ProtocolError) as caught:
        stdio_fastmcp._tool_result(tools, {"name": "search", "arguments": {"budgt": 1}})
    assert caught.value.code == -32602
    assert caught.value.message.startswith("unexpected_arguments:")
    assert "budgt -> budget" in caught.value.message

    with pytest.raises(stdio_fastmcp.ProtocolError) as caught:
        stdio_fastmcp._tool_result(
            tools, {"name": "search", "arguments": {"query": "q", "limit": 1, "budget": 2}}
        )
    assert "alias_conflict" in caught.value.message


def test_server_stdlib_path_applies_aliases_and_guidance() -> None:
    env = dict(os.environ, AIWORKHUB_MCP_STDIO_BACKEND="stdlib", PYTHONPATH=str(SRC))
    script = """
import json
from aiworkhub import server

def fake(query: str = "", budget: int = 1, needfix_id: str = ""):
    return {"query": query, "budget": budget, "needfix_id": needfix_id}

tools = {"aiworkhub_task_create": fake}

def call(arguments):
    try:
        out = server._stdio_tools_call(tools, {"name": "aiworkhub_task_create", "arguments": arguments})
        return json.loads(out["content"][0]["text"])
    except server._StdioProtocolError as error:
        return {"code": error.code, "message": error.message}

print(json.dumps([
    call({"limit": 4}),
    call({"id": "NF-1"}),
    call({"nf_id": "NF-2"}),
    call({"budgt": 1}),
    call({"limit": 1, "budget": 2}),
    call({"id": "a", "nf_id": "b"}),
]))
"""
    result = subprocess.run(
        [sys.executable, "-S", "-c", script], env=env, capture_output=True,
        text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    limit, ident, nf_ident, typo, both, two = json.loads(result.stdout.strip().splitlines()[-1])
    assert limit["budget"] == 4
    assert ident["needfix_id"] == "NF-1"
    assert nf_ident["needfix_id"] == "NF-2"
    assert typo["code"] == -32602 and typo["message"].startswith("unexpected_arguments:")
    assert "budgt -> budget" in typo["message"]
    assert "alias_conflict" in both["message"]
    assert "alias_conflict" in two["message"]


def _two_required(template_id: str, request_id: str, budget: int = 5) -> dict:
    return {"template_id": template_id, "request_id": request_id, "budget": budget}


def test_missing_required_refusal_names_every_missing_and_accepted() -> None:
    message = missing_required_refusal(_two_required, {}, {})
    assert message is not None
    assert message.startswith("missing_required_arguments:[template_id, request_id]")
    assert "accepted: budget, request_id, template_id" in message
    assert "aliases_applied" not in message
    assert missing_required_refusal(_two_required, {"template_id": "t", "request_id": "r"}, {}) is None


def test_defaults_and_variadics_are_never_named_missing() -> None:
    def variadic(name: str, *args: int, flag: bool = False, **kwargs: int) -> None:
        return None

    message = missing_required_refusal(variadic, {}, {})
    assert message is not None
    assert message.startswith("missing_required_arguments:[name];")
    assert "args" not in message.split(";")[0] and "kwargs" not in message.split(";")[0]
    assert missing_required_refusal(variadic, {"name": "n"}, {"name": "n"}) is None


def test_unexpected_and_missing_arguments_share_one_message() -> None:
    _, message = resolve_tool_arguments(_two_required, {"template_id": "t", "bogus": 1})
    assert message is not None and message.startswith("unexpected_arguments:")
    assert "missing_required: [request_id]" in message
    _, message = resolve_tool_arguments(_two_required, {"bogus": 1})
    assert message is not None and "missing_required: [template_id, request_id]" in message


def test_alias_with_another_missing_parameter_reports_alias_and_missing() -> None:
    original = {"limit": 3, "template_id": "t"}
    resolved, problem = resolve_tool_arguments(_two_required, original)
    assert problem is None and resolved == {"budget": 3, "template_id": "t"}
    message = missing_required_refusal(_two_required, resolved, original)
    assert message is not None
    assert message.startswith("missing_required_arguments:[request_id]")
    assert "limit -> budget" in message


def test_uninspectable_callable_has_no_missing_required_refusal() -> None:
    assert missing_required_refusal(object(), {}, {}) is None


def test_fastmcp_missing_required_is_one_in_band_error_and_not_invoked() -> None:
    calls: list[dict] = []

    def tool(template_id: str, request_id: str, budget: int = 1) -> dict:
        calls.append({"template_id": template_id})
        return {}

    result = stdio_fastmcp._tool_result(
        {"tool": tool}, {"name": "tool", "arguments": {"limit": 2}}
    )
    assert result["isError"] is True and len(result["content"]) == 1
    text = result["content"][0]["text"]
    assert "template_id" in text and "request_id" in text and "limit -> budget" in text
    assert "accepted:" in text and "TypeError" not in text
    assert calls == []


def test_server_stdlib_missing_required_is_one_in_band_error_and_not_invoked() -> None:
    env = dict(os.environ, AIWORKHUB_MCP_STDIO_BACKEND="stdlib", PYTHONPATH=str(SRC))
    script = """
import json
from aiworkhub import server

calls = []

def fake(template_id: str, request_id: str, budget: int = 1):
    calls.append(1)
    return {}

tools = {"aiworkhub_task_create": fake}
out = server._stdio_tools_call(tools, {"name": "aiworkhub_task_create", "arguments": {}})
print(json.dumps({"out": out, "calls": len(calls)}))
"""
    result = subprocess.run(
        [sys.executable, "-S", "-c", script], env=env, capture_output=True,
        text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    out = payload["out"]
    assert out["isError"] is True and len(out["content"]) == 1
    text = out["content"][0]["text"]
    assert text.startswith("missing_required_arguments:[template_id, request_id]")
    assert "accepted: budget, request_id, template_id" in text
    assert payload["calls"] == 0
