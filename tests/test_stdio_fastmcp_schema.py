from __future__ import annotations

import io
import json
from typing import Literal

from aiworkhub import stdio_fastmcp
from aiworkhub.stdio_fastmcp import _dispatch, _schema_for


def test_literal_and_optional_literal_are_exposed_as_enums() -> None:
    def tool(
        mode: Literal["focus", "slice", "bundle"],
        kind: Literal["audit", "explore"] | None = None,
    ) -> dict[str, object]:
        return {}

    schema = _schema_for(tool)
    assert schema["properties"]["mode"] == {
        "type": "string",
        "enum": ["focus", "slice", "bundle"],
    }
    assert schema["properties"]["kind"] == {
        "type": "string",
        "enum": ["audit", "explore"],
    }


def test_resource_discovery_is_empty_but_protocol_compatible() -> None:
    assert _dispatch("worker", {}, "resources/list", {}) == {"resources": []}
    assert _dispatch("worker", {}, "resources/templates/list", {}) == {
        "resourceTemplates": []
    }
    initialized = _dispatch("worker", {}, "initialize", {})
    assert initialized["capabilities"]["resources"] == {}


def test_worker_stdio_writes_georgian_as_binary_utf8(monkeypatch) -> None:
    output = io.BytesIO()

    class LocaleBoundTextStream:
        buffer = output

        def write(self, _text):
            raise UnicodeEncodeError("charmap", "ქართული", 0, 1, "unsupported")

        def flush(self):
            raise AssertionError("text flush must not be used")

    monkeypatch.setattr(stdio_fastmcp.sys, "stdout", LocaleBoundTextStream())
    stdio_fastmcp._write({
        "jsonrpc": "2.0", "id": 1, "result": {"text": "ქართული პასუხი"},
    })

    decoded = json.loads(output.getvalue().decode("utf-8"))
    assert decoded["result"]["text"] == "ქართული პასუხი"


def test_unknown_tool_error_does_not_terminate_following_requests(monkeypatch) -> None:
    request_stream = io.BytesIO(
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        b'"params":{"name":"real_tol","arguments":{}}}\n'
        b'{"jsonrpc":"2.0","id":2,"method":"ping","params":{}}\n'
    )
    response_stream = io.BytesIO()

    class Input:
        buffer = request_stream

    class Output:
        buffer = response_stream

    monkeypatch.setattr(stdio_fastmcp.sys, "stdin", Input())
    monkeypatch.setattr(stdio_fastmcp.sys, "stdout", Output())
    stdio_fastmcp._run("worker", {"real_tool": lambda: {}})

    responses = [
        json.loads(line)
        for line in response_stream.getvalue().decode("utf-8").splitlines()
    ]
    error = json.loads(responses[0]["error"]["message"])
    assert error["suggestions"] == ["real_tool"]
    assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}
