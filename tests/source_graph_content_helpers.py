"""Decode a single-page ``source_graph_query`` reply's structured ``content``.

NF-2026-01448 made ``content`` a dict with per-row defaults elided; the exact
inverse lives in ``worker_ai_tools_mcp._source_graph_payload``. Tests decode
through it instead of ``json.loads(content)`` or substring-matching the dict.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aiworkhub import worker_ai_tools_mcp


def content_payload(reply: Mapping[str, Any]) -> dict[str, Any]:
    payload = worker_ai_tools_mcp._source_graph_payload(reply)
    assert payload is not None, "reply content is not a decodable single-page payload"
    return payload


def content_text(reply: Mapping[str, Any]) -> str:
    return json.dumps(content_payload(reply), sort_keys=True, ensure_ascii=False)
