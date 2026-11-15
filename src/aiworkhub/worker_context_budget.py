"""Byte budget for the project_context section of a worker prompt.

NF-2026-02285: measured project_context was 23810 B on an initial launch and
20381 B again on the rework delta. The initial section is bounded to
MAX_INITIAL_PROJECT_CONTEXT_BYTES with dropped evidence replaced by named
retrieval pointers; a rework delta re-ships only a digest/pointer to the
context already delivered. Card-required sections (development_rules) are
always carried verbatim and never silently dropped.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

MAX_INITIAL_PROJECT_CONTEXT_BYTES = 8 * 1024
# Rework cap for the *budgeted* part of project_context (pointer/digest). The
# verbatim development_rules section is required on every launch and is
# counted OUTSIDE this cap (reported as project_context_required_bytes).
MAX_REWORK_PROJECT_CONTEXT_BYTES = 1024
PROJECT_CONTEXT_BUNDLE_MARKER = "PROJECT_CONTEXT_BUNDLE:\n"
PROJECT_CONTEXT_POINTER_MARKER = "PROJECT_CONTEXT_POINTER:\n"
PROJECT_CONTEXT_EXCERPT_MARKER = "\nPROJECT_CONTEXT_EXCERPT:\n"
PROJECT_CONTEXT_POINTER_SCHEMA_ID = "aiworkhub.worker_project_context_pointer.v1"
# Card-required evidence is never replaced by a pointer: development rules are
# a contract the worker must see verbatim. The rest is ranked by how directly
# it serves the card's own scope; ties fall back to name order.
REQUIRED_CONTEXT_SECTIONS = ("development_rules",)
_CONTEXT_SECTION_PRIORITY = (
    "development_rules",
    "project_tools",
    "source_graph",
    "skills",
    "ai_memory",
    "kb",
    "session",
)
_CONTEXT_HINT_QUERY_CHARS = 120
_REWORK_SECTION_NAME_CAP = 12


@dataclass(frozen=True)
class RenderedProjectContext:
    """The project_context text plus its budget accounting."""

    text: str
    mode: str
    max_bytes: int
    required_bytes: int
    budgeted_bytes: int
    over_budget: bool

    def report(self) -> dict[str, Any]:
        return {
            "project_context_mode": self.mode,
            "project_context_max_bytes": self.max_bytes,
            "project_context_required_bytes": self.required_bytes,
            "project_context_budgeted_bytes": self.budgeted_bytes,
            "project_context_over_budget": self.over_budget,
        }


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def context_retrieval_hint(name: str, card: Mapping[str, Any] | None) -> dict[str, Any]:
    """Name the live tool and query that re-derives one omitted section."""

    card = card if isinstance(card, Mapping) else {}
    declared = card.get("project_context")
    declared = declared if isinstance(declared, Mapping) else {}
    section = declared.get(name)
    section = section if isinstance(section, Mapping) else {}
    topic = str(card.get("topic") or "")

    def bounded(value: Any) -> str:
        return str(value or "")[:_CONTEXT_HINT_QUERY_CHARS]

    if name == "source_graph":
        return {
            "tool": "aiworkhub_worker_source_graph_query",
            "mode": bounded(section.get("mode") or "focus"),
            "query": bounded(section.get("query") or topic),
        }
    if name == "kb":
        return {"tool": "aiworkhub_worker_kb_search", "query": bounded(section.get("query") or topic)}
    if name == "ai_memory":
        return {
            "tool": "aiworkhub_worker_ai_memory_search",
            "query": bounded(section.get("query") or topic),
        }
    if name == "session":
        return {
            "tool": "aiworkhub_worker_session_current_state",
            "topic": bounded(section.get("topic") or topic),
        }
    if name == "development_rules":
        return {"source": ".aiworkhub/config/development_rules.json"}
    if name == "project_tools":
        return {"source": "tools/"}
    return {"tool": "aiworkhub_worker_kb_search", "query": bounded(topic or name)}


def _default_hints(card: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    return [
        context_retrieval_hint(name, card)
        for name in ("source_graph", "kb", "ai_memory", "session")
    ]


def _context_section_order(names: Iterable[str]) -> list[str]:
    rank = {name: index for index, name in enumerate(_CONTEXT_SECTION_PRIORITY)}
    return sorted(names, key=lambda name: (rank.get(name, len(rank)), name))


def _parse_context_evidence(bundle: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return (payload, evidence) for a well-formed bundle, else None."""

    if not bundle.startswith(PROJECT_CONTEXT_BUNDLE_MARKER):
        return None
    try:
        payload = json.loads(bundle[len(PROJECT_CONTEXT_BUNDLE_MARKER) :])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("evidence"), dict):
        return None
    return payload, payload["evidence"]


def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _evidence_value_offsets(bundle: str) -> dict[str, int]:
    """Offsets of payload["evidence"][name] values in a possibly broken bundle.

    A minimal tokenizer tracks the key that opened every container, so only a
    key directly inside the top-level "evidence" object counts -- never a
    same-named key nested in another section or in a string.
    """

    wanted = set(REQUIRED_CONTEXT_SECTIONS)
    offsets: dict[str, int] = {}
    stack: list[str | None] = []
    pending: str | None = None
    index = len(PROJECT_CONTEXT_BUNDLE_MARKER) if bundle.startswith(PROJECT_CONTEXT_BUNDLE_MARKER) else 0
    while index < len(bundle):
        char = bundle[index]
        if char == '"':
            try:
                text, end = json.decoder.scanstring(bundle, index + 1)
            except ValueError:
                break
            after = _skip_space(bundle, end)
            if after < len(bundle) and bundle[after] == ":":
                if stack == [None, "evidence"] and text in wanted and text not in offsets:
                    offsets[text] = _skip_space(bundle, after + 1)
                pending = text
                index = after + 1
            else:
                index = end
            continue
        if char in "{[":
            stack.append(pending)
            pending = None
        elif char in "}]":
            if stack:
                stack.pop()
            pending = None
        elif char == ",":
            pending = None
        index += 1
    return offsets


def _extract_required_sections(bundle: str) -> tuple[dict[str, Any], list[tuple[int, int]]]:
    """Recover required sections (and their spans) from an unparseable bundle."""

    decoder = json.JSONDecoder()
    found: dict[str, Any] = {}
    spans: list[tuple[int, int]] = []
    for name, position in _evidence_value_offsets(bundle).items():
        try:
            value, end = decoder.raw_decode(bundle, position)
        except ValueError:
            continue
        found[name] = value
        spans.append((position, end))
    return found, sorted(spans)


def _without_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """text with each span replaced by a short note (it is carried verbatim above)."""

    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(text[cursor:start])
        parts.append('"[carried verbatim in the pointer above]"')
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _mark_unrecovered(
    pointer: dict[str, Any], required: Mapping[str, Any], card: Mapping[str, Any] | None
) -> None:
    missing = [name for name in REQUIRED_CONTEXT_SECTIONS if name not in required]
    if missing:
        pointer["required_sections_unrecovered"] = missing
        pointer["required_sections_retrieve"] = [
            context_retrieval_hint(name, card) for name in missing
        ]


def _utf8_prefix(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def _required_bytes(required: Mapping[str, Any]) -> int:
    return sum(_utf8_len(_compact_json(value)) for value in required.values())


def _budget_unstructured(
    bundle: str, card: Mapping[str, Any] | None, budget: int
) -> RenderedProjectContext:
    """Unparseable over-budget bundle: keep required sections, truncate the rest."""

    data = bundle.encode("utf-8")
    required, spans = _extract_required_sections(bundle)
    hints = _default_hints(card)
    pointer: dict[str, Any] = {
        "schema_id": PROJECT_CONTEXT_POINTER_SCHEMA_ID,
        "mode": "initial_budget",
        "omitted_reason": "context_budget_unstructured_bundle",
        "bundle_bytes": len(data),
        "bundle_sha256": hashlib.sha256(data).hexdigest(),
        "retrieve": hints,
        **required,
    }
    _mark_unrecovered(pointer, required, card)
    head = PROJECT_CONTEXT_POINTER_MARKER + _compact_json(pointer)
    rest = _without_spans(bundle, spans)
    tools = ",".join(str(hint["tool"]) for hint in hints if "tool" in hint)

    def marker(omitted: int) -> str:
        return f"\n[truncated: {omitted} bytes, retrieve via {tools}]"

    # The widest marker (omitted == whole bundle) is reserved exactly.
    room = (
        budget
        - _utf8_len(head)
        - _utf8_len(PROJECT_CONTEXT_EXCERPT_MARKER)
        - _utf8_len(marker(len(data)))
    )
    excerpt = _utf8_prefix(rest, room)
    omitted = _utf8_len(rest) - _utf8_len(excerpt)
    text = head + PROJECT_CONTEXT_EXCERPT_MARKER + excerpt + marker(omitted)
    size = _utf8_len(text)
    return RenderedProjectContext(
        text=text,
        mode="initial_budget",
        max_bytes=budget,
        required_bytes=_required_bytes(required),
        budgeted_bytes=size,
        over_budget=size > budget,
    )


def budget_initial_project_context(
    bundle: str, card: Mapping[str, Any] | None
) -> RenderedProjectContext:
    """Bound the initial project_context to MAX_INITIAL_PROJECT_CONTEXT_BYTES.

    A bundle already within budget is returned byte-identical. Otherwise
    lower-priority evidence sections are replaced, in priority order, by
    pointers naming the bytes and sha256 prefix that were dropped and the live
    tool/query that re-derives them -- never silently removed. Required
    sections are always kept verbatim; if they alone exceed the budget the
    result is flagged over_budget rather than dropped.
    """

    budget = MAX_INITIAL_PROJECT_CONTEXT_BYTES
    original_bytes = _utf8_len(bundle)
    if original_bytes <= budget:
        return RenderedProjectContext(bundle, "passthrough", budget, 0, original_bytes, False)
    parsed = _parse_context_evidence(bundle)
    if parsed is None:
        return _budget_unstructured(bundle, card, budget)
    payload, evidence = parsed
    pointers = {}
    for name, value in evidence.items():
        encoded = _compact_json(value).encode("utf-8")
        pointers[name] = {
            "omitted": "context_budget",
            "bytes": len(encoded),
            "sha256_prefix": hashlib.sha256(encoded).hexdigest()[:16],
            "retrieve": context_retrieval_hint(name, card),
        }
    chosen = dict(pointers)
    base = {key: value for key, value in payload.items() if key != "evidence"}
    base["context_budget"] = {
        "max_bytes": budget,
        "original_bytes": original_bytes,
        "original_sha256": hashlib.sha256(bundle.encode("utf-8")).hexdigest(),
    }

    def render() -> str:
        return PROJECT_CONTEXT_BUNDLE_MARKER + _compact_json({**base, "evidence": chosen})

    # Upgrade pointer -> full section in priority order while the bundle fits.
    # O(sections^2) serialisations over <= ~10 bounded sections.
    for name in _context_section_order(evidence):
        previous = chosen[name]
        chosen[name] = evidence[name]
        if name in REQUIRED_CONTEXT_SECTIONS:
            continue
        if _utf8_len(render()) > budget:
            chosen[name] = previous
    text = render()
    size = _utf8_len(text)
    required_bytes = _required_bytes(
        {name: evidence[name] for name in REQUIRED_CONTEXT_SECTIONS if name in evidence}
    )
    return RenderedProjectContext(
        text, "initial_budget", budget, required_bytes, size, size > budget
    )


def rework_project_context_pointer(
    bundle: str, card: Mapping[str, Any] | None
) -> RenderedProjectContext:
    """Replace unchanged project_context on a rework delta by a digest/pointer.

    development_rules travels verbatim (counted outside the 1 KiB cap); only
    the remaining evidence becomes a hash-bound pointer.
    """

    cap = MAX_REWORK_PROJECT_CONTEXT_BYTES
    data = bundle.encode("utf-8")
    parsed = _parse_context_evidence(bundle)
    if parsed is not None:
        evidence = parsed[1]
        required = {
            name: evidence[name] for name in REQUIRED_CONTEXT_SECTIONS if name in evidence
        }
    else:
        evidence = {}
        required, _spans = _extract_required_sections(bundle)
    required_bytes = _required_bytes(required)
    # The cap covers the bytes outside development_rules, so a bundle that is
    # mostly rules still passes through instead of growing into a pointer.
    if len(data) - required_bytes <= cap:
        return RenderedProjectContext(
            bundle, "passthrough", cap, required_bytes, len(data) - required_bytes, False
        )
    pointer: dict[str, Any] = {
        "schema_id": PROJECT_CONTEXT_POINTER_SCHEMA_ID,
        "mode": "rework_delta",
        "omitted_reason": "unchanged_context_already_delivered",
        "bundle_bytes": len(data),
        "bundle_sha256": hashlib.sha256(data).hexdigest(),
        "sections": [name for name in _context_section_order(evidence) if name not in required],
        "retrieve": _default_hints(card),
        **required,
    }
    _mark_unrecovered(pointer, required, card)

    def render() -> tuple[str, int]:
        text = PROJECT_CONTEXT_POINTER_MARKER + _compact_json(pointer)
        return text, _utf8_len(text) - required_bytes

    text, budgeted = render()
    if budgeted > cap:
        pointer["retrieve"] = [
            {"tool": hint["tool"]} for hint in pointer["retrieve"] if "tool" in hint
        ]
        pointer["sections"] = pointer["sections"][:_REWORK_SECTION_NAME_CAP]
        text, budgeted = render()
    return RenderedProjectContext(
        text, "rework_delta", cap, required_bytes, budgeted, budgeted > cap
    )


def render_worker_project_context(
    bundle: str, *, card: Mapping[str, Any] | None, rework: bool
) -> RenderedProjectContext:
    """The project_context a worker prompt carries (bounded per mode)."""

    bundle = bundle.strip()
    if not bundle:
        budget = MAX_REWORK_PROJECT_CONTEXT_BYTES if rework else MAX_INITIAL_PROJECT_CONTEXT_BYTES
        return RenderedProjectContext("", "empty", budget, 0, 0, False)
    if rework:
        return rework_project_context_pointer(bundle, card)
    return budget_initial_project_context(bundle, card)
