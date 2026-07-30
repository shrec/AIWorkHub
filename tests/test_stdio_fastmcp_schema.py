from __future__ import annotations

from typing import Literal

from aiworkhub.stdio_fastmcp import _schema_for


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
