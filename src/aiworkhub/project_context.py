"""Fail-closed project-context bundles for Task MCP workers.

A task card may declare ``project_context``.  When present, this module
validates that structured contract and asks only the repository's existing
read-only context tools for bounded evidence.  It never infers routing from
task names and never exposes raw context in process events or metadata.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import repository_state
from . import worker_ai_tools_mcp as _worker_tools


LEGACY_SCHEMA_ID = "aiworkhub.task_mcp.project_context_bundle.v1"
SCHEMA_ID = "aiworkhub.task_mcp.project_context_bundle.v2"
ENCODING_SCHEMA_ID = "aiworkhub.task_mcp.project_context_encoding.v2"
RECEIPT_SCHEMA_ID = "aiworkhub.task_mcp.worker_context_receipt.v1"
MAX_QUERY_BYTES = 512
MAX_TOPIC_BYTES = 128
MAX_BUDGET = 160
MAX_TOOL_OUTPUT_BYTES = 24 * 1024
MAX_BUNDLE_BYTES = 64 * 1024
MAX_TARGETS = 12
MAX_TARGET_BYTES = 256
MAX_SECTION_ROWS = 40
TOOL_CAPS: dict[str, dict[str, int]] = {
    "source_graph": {"bytes": 12 * 1024, "rows": 24},
    "session_current_state": {"bytes": 6 * 1024, "rows": 12},
    "ai_memory": {"bytes": 4 * 1024, "rows": 8},
    "kb": {"bytes": 4 * 1024, "rows": 8},
    # skill_registry already bounds a runtime packet to MAX_PACKET_BYTES (8 KiB)
    # and MAX_PACKET_SELECTED rows; this is the section's own independent cap.
    "skills": {"bytes": 8 * 1024, "rows": 32},
}
SKILL_PACKET_SCHEMA_ID = "aiworkhub.task_mcp.skill_runtime_packet.v1"
SKILL_SELECT_LIMIT = 4
# The injected focus orientation is bounded by the same envelope a worker's own
# focus/slice call receives, so injecting it never costs more prompt bytes than
# the first live orientation call it replaces (worker_prompt-1).
SOURCE_GRAPH_ORIENTATION_SCHEMA_ID = "aiworkhub.task_mcp.source_graph_orientation.v1"
SOURCE_GRAPH_ORIENTATION_BYTES = int(_worker_tools.SOURCE_GRAPH_ORIENTATION_OUTPUT_BYTES)
# Entity rows shared across every indexed target before byte fitting, and the
# per-target floor/ceiling (the ceiling is ``file_query``'s own entity limit).
ORIENTATION_ENTITY_ALLOWANCE = 48
ORIENTATION_ENTITIES_PER_TARGET_MIN = 4
ORIENTATION_ENTITIES_PER_TARGET_MAX = 16
ORIENTATION_FOCUS_MATCHES = 12
ORIENTATION_INSIGHT_ROWS = 6
ORIENTATION_DIRECTORY_SYMBOLS = 12
ORIENTATION_SIGNATURE_CHARS = 120
# An empty orientation with one of these reasons is a truthful "nothing to
# orient on" (the card names no path Source Graph could hold), not a failure.
ORIENTATION_EMPTY_EXEMPT_REASONS = frozenset({"no_targets", "targets_not_on_disk"})
# Keys whose list/mapping value holds result rows. A row inside one of these
# is a hit; a list under any other key never is.
_RESULT_CONTAINER_KEYS = frozenset({
    "items", "results", "matches", "rows", "symbols", "files", "sections",
    "entities", "contexts", "ranked_symbols", "direct_matches",
})
# Retrieval provenance the engine restores even on a scoped miss. These mark
# the payload as a retrieval envelope but never count as hits.
_ECHO_KEYS = frozenset({
    "query_tokens", "query_tokens_source", "candidate_files", "tags",
    "targets", "requested_target", "unindexed_targets",
})
SOURCE_GRAPH_MODES = (
    "focus", "slice", "context", "file", "function", "class", "body", "bodygrep",
    "impact", "trace", "deps", "bundle",
    "tags", "hotspots", "coverage", "churn", "reviewqueue", "ownership",
    "testmap", "calls", "symbols", "bottlenecks", "auditmap", "complexity",
    "stats", "summarize", "pipeline",
    "todo", "leaks", "nullrisks", "rawptrs", "casts", "crashes",
    "looprisks", "deadmethods", "duplicates", "gaps",
)
SOURCE_GRAPH_BUNDLE_TYPES = ("bugfix", "feature", "refactor", "audit", "optimize", "explore")
TASK_CONTEXT_KINDS = ("code", "data_classification", "research")


class ProjectContextError(RuntimeError):
    """Project-context preflight failed."""


@dataclass(frozen=True, slots=True)
class ProjectContextResult:
    prompt_bundle: str
    metadata: dict[str, Any]
    worker_source_graph_targets: tuple[str, ...] = ()
    worker_session_topic: str = ""


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_str(value: Any, *, field: str, max_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectContextError(f"{field}_must_be_nonempty_string")
    if "\x00" in value:
        raise ProjectContextError(f"{field}_contains_nul")
    encoded = value.encode("utf-8")
    if len(encoded) > max_bytes:
        raise ProjectContextError(f"{field}_too_large:{len(encoded)}>{max_bytes}")
    return value.strip()


def _bounded_int(value: Any, *, field: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProjectContextError(f"{field}_must_be_integer")
    if value < minimum or value > maximum:
        raise ProjectContextError(f"{field}_out_of_range:{value}")
    return value


def _task_context_kind(card: dict[str, Any], raw: dict[str, Any]) -> str:
    explicit = raw.get("task_type") or raw.get("context_kind")
    if explicit is not None:
        if explicit not in TASK_CONTEXT_KINDS:
            raise ProjectContextError(
                "project_context.task_type_invalid:allowed="
                + "|".join(TASK_CONTEXT_KINDS)
            )
        return str(explicit)
    mode = str(card.get("mode") or "").lower()
    objective = str(card.get("objective") or "").lower()
    if "classification" in mode or "data-classification" in objective or "data classification" in objective:
        return "data_classification"
    if "research" in mode:
        return "research"
    return "code"


def _bounded_targets(raw: Any, *, field: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ProjectContextError(f"{field}_must_be_list")
    targets: list[str] = []
    seen: set[str] = set()
    for value in raw[:MAX_TARGETS]:
        target = _bounded_str(value, field=f"{field}.item", max_bytes=MAX_TARGET_BYTES)
        if "\n" in target or "\r" in target:
            raise ProjectContextError(f"{field}.item_contains_newline")
        if target not in seen:
            seen.add(target)
            targets.append(target)
    return targets


def _derive_targets(card: dict[str, Any]) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()
    # Immutable/dependency inputs are first-class worker evidence.  They must
    # be available to the task-scoped Source Graph ``file`` mode even when a
    # non-code format (JSON/JSONL/XML/etc.) has no semantic entities in the
    # current index.  Keep them ahead of write scopes so the bounded target
    # cap cannot accidentally discard declared inputs in favour of outputs.
    for key in ("read_first", "immutable_inputs", "allowed_writes"):
        value = card.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, str):
                continue
            normalized = item.strip().replace("\\", "/")
            if not normalized or "*" in normalized or "?" in normalized:
                continue
            if normalized.startswith(".git") or "\x00" in normalized:
                continue
            if normalized not in seen:
                seen.add(normalized)
                targets.append(normalized)
            if len(targets) >= MAX_TARGETS:
                return targets
    return targets


def _scope_values(card: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("read_first", "immutable_inputs", "allowed_writes"):
        raw = card.get(key)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, str) and item.strip():
                values.append(item.strip().replace("\\", "/"))
    return values


def _static_scope_prefix(raw: str) -> str | None:
    if "\x00" in raw or raw.startswith("/") or raw == ".git" or raw.startswith(".git/"):
        return None
    parts = raw.split("/")
    if ".." in parts:
        return None
    wildcard_indexes = [raw.find(ch) for ch in ("*", "?", "[") if raw.find(ch) >= 0]
    wildcard_at = min(wildcard_indexes) if wildcard_indexes else -1
    prefix = raw[:wildcard_at] if wildcard_at >= 0 else raw
    if wildcard_at >= 0 and "/" in prefix:
        prefix = prefix.rsplit("/", 1)[0]
    return prefix.strip("/") or "."


def _git_boundary_for_scope(repo: Path, raw: str) -> Path | None:
    prefix = _static_scope_prefix(raw)
    if prefix is None:
        return None
    root = repo.resolve()
    candidate = (root / prefix).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    search = candidate if candidate.suffix == "" else candidate.parent
    for current in (search, *search.parents):
        if current == root or root in current.parents:
            if (current / ".git").exists():
                return current
        if current == root:
            break
    return root


def resolve_task_repository_root(repo: Path, card: dict[str, Any]) -> Path:
    """Resolve the one git repository boundary owned by this task scope."""

    root = repo.resolve()
    boundaries: set[Path] = set()
    for raw in _scope_values(card):
        boundary = _git_boundary_for_scope(root, raw)
        if boundary is not None:
            boundaries.add(boundary.resolve())
    if not boundaries:
        return root
    if len(boundaries) > 1:
        labels = ",".join(
            sorted(str(path.relative_to(root) if path != root else Path(".")) for path in boundaries)
        )
        raise ProjectContextError(f"task_repo_scope_ambiguous:{labels}")
    return next(iter(boundaries))


def _rebase_targets(repo: Path, authority_repo: Path, targets: list[str]) -> list[str]:
    root = repo.resolve()
    authority = authority_repo.resolve()
    if authority == root:
        return targets
    rebased: list[str] = []
    seen: set[str] = set()
    for raw in targets:
        prefix = _static_scope_prefix(raw)
        if prefix is None:
            continue
        candidate = (root / prefix).resolve()
        if candidate == authority:
            rel = "."
        elif authority in candidate.parents:
            rel = candidate.relative_to(authority).as_posix()
        else:
            continue
        if rel not in seen:
            seen.add(rel)
            rebased.append(rel)
    return rebased


def _safe_tool_result(name: str, payload: str, *, truncated: bool, degraded: str = "") -> dict[str, Any]:
    return {
        "tool": name,
        "bytes": len(payload.encode("utf-8")),
        "sha256": _sha256_text(payload),
        "truncated": truncated,
        "degraded_reason": degraded,
    }


def _tool_cap(name: str, cap: str, default: int) -> int:
    return int(TOOL_CAPS.get(name, {}).get(cap) or default)


def _json_hit_count(value: Any) -> int:
    """Count result rows in a canonical tool payload.

    A hit is one row inside a *result container*: a list (or an id-keyed
    mapping) stored under one of ``_RESULT_CONTAINER_KEYS`` at any depth,
    reached through nested dicts and list elements but never through an
    echo key. Echo keys (``query_tokens``, ``candidate_files``, ...) are
    retrieval provenance the engine restores even on a scoped miss: they
    mark the payload as a retrieval envelope but never count. Counting them
    reported ``hit_count == len(query_tokens)`` for every zero-hit focus
    bundle and defeated ``source_graph_required_empty_result``
    (worker_prompt-1). A payload with neither a container nor an echo key
    is opaque: a non-empty one counts as a single hit so unknown tool
    shapes stay lenient, an empty one as zero. A bounded preview reports
    the count of the payload it previews.
    """

    if isinstance(value, list):
        return _row_count(value)
    if isinstance(value, dict):
        if value.get("schema_id") == "aiworkhub.task_mcp.bounded_json_preview.v1":
            original_hit_count = value.get("original_hit_count")
            if (
                isinstance(original_hit_count, int)
                and not isinstance(original_hit_count, bool)
                and original_hit_count >= 0
            ):
                return original_hit_count
        total, saw_envelope = _container_rows(value)
        if saw_envelope:
            return total
        return 1 if value else 0
    return 0


def _container_rows(value: dict[str, Any]) -> tuple[int, bool]:
    """Rows under result containers reachable from ``value``.

    The second element says whether a result container or an echo key was
    seen at all, which is what separates "looked and found nothing" from an
    opaque payload of unknown shape.
    """

    total = 0
    saw_envelope = False
    for key, item in value.items():
        if key in _ECHO_KEYS:
            saw_envelope = True
            continue
        if key in _RESULT_CONTAINER_KEYS:
            saw_envelope = True
            total += _row_count(item)
        elif isinstance(item, dict):
            nested, nested_saw = _container_rows(item)
            total += nested
            saw_envelope = saw_envelope or nested_saw
        elif isinstance(item, list):
            # A list under any other key is not a result container; only the
            # containers its dict rows may themselves hold count.
            for element in item:
                if isinstance(element, dict):
                    nested, nested_saw = _container_rows(element)
                    total += nested
                    saw_envelope = saw_envelope or nested_saw
    return total, saw_envelope


def _row_count(container: Any) -> int:
    if isinstance(container, list):
        return len(container) + sum(
            _container_rows(row)[0] for row in container if isinstance(row, dict)
        )
    if isinstance(container, dict):
        nested, saw_envelope = _container_rows(container)
        # An id-keyed mapping with no nested container is itself the row set.
        return nested if saw_envelope else len(container)
    return 0


def _content_hit_count(name: str, text: str) -> int:
    if not text.strip():
        return 0
    if name in {"source_graph", "session_current_state", "ai_memory"}:
        try:
            return _json_hit_count(json.loads(text))
        except json.JSONDecodeError:
            return 0
    if name == "kb":
        lowered = text.lower()
        if "no results" in lowered or "0 result" in lowered:
            return 0
        return max(1, sum(1 for line in text.splitlines() if line.strip().startswith("[")))
    return 1


def _section(
    *,
    name: str,
    content: str,
    truncated: bool,
    degraded: str = "",
    requested: bool = True,
    executed: bool = True,
    hit_count: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    cap_bytes = _tool_cap(name, "bytes", MAX_TOOL_OUTPUT_BYTES)
    cap_rows = _tool_cap(name, "rows", MAX_SECTION_ROWS)
    if len(content.encode("utf-8")) > cap_bytes:
        content, cap_truncated = _bounded_text(content, cap_bytes)
        truncated = truncated or cap_truncated
    if hit_count is None:
        hit_count = _content_hit_count(name, content) if executed and not degraded else 0
    return {
        "name": name,
        "requested": requested,
        "executed": executed,
        "hit_count": hit_count,
        "byte_cap": cap_bytes,
        "row_cap": cap_rows,
        "cap_enforced": True,
        "content": content,
        **extra,
        **_safe_tool_result(name, content, truncated=truncated, degraded=degraded),
    }


def _bounded_text(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _canonical_json_output(
    name: str,
    text: str,
    *,
    max_bytes: int = MAX_TOOL_OUTPUT_BYTES,
) -> tuple[str, bool]:
    # Source Graph currently emits one bounded language-status banner before
    # its JSON payload.  Accept only that prefix shape, then canonicalize the
    # single complete JSON value so banners never enter the model prompt.
    start = text.find("{")
    if start < 0:
        raise ProjectContextError(f"context_tool_malformed_json:{name}:missing_object")
    prefix = text[:start].strip()
    if prefix and not prefix.startswith("[*] Language:"):
        raise ProjectContextError(f"context_tool_malformed_json:{name}:unexpected_prefix")
    try:
        payload = json.loads(text[start:])
    except json.JSONDecodeError as exc:
        raise ProjectContextError(f"context_tool_malformed_json:{name}:{exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ProjectContextError(f"context_tool_malformed_json:{name}:object_required")
    # Compact separators here (no space padding) keep the canonicalized tool
    # payload dense; it is still embedded once as a JSON-text string value in
    # the outer bundle, so shaving inner whitespace is pure savings.
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = canonical.encode("utf-8")
    if len(encoded) <= max_bytes:
        return canonical, False
    priority_keys = (
        "ranked_symbols",
        "related_tests",
        "risks",
        "todos",
        "recommended_next_steps",
    )
    identity_keys = (
        "schema_id", "ok", "tool", "mode", "query", "target", "hit_count", "budget",
    )
    semantic_keys = (
        "name", "qualname", "file_path", "kind", "signature",
        "line_start", "line_end", "priority_score", "risk_reasons",
        "metrics_evidence", "confidence", "evidence_label",
    )

    def ordered_mapping_keys(value: dict[Any, Any]) -> list[Any]:
        ordered = [key for key in semantic_keys if key in value]
        ordered.extend(key for key in sorted(value, key=str) if key not in ordered)
        return ordered

    def preview_value(value: Any, depth: int = 0) -> Any:
        if depth >= 3:
            if isinstance(value, list):
                return {"truncated": True, "original_items": len(value)}
            if isinstance(value, dict):
                return {"truncated": True, "original_keys": len(value)}
        if isinstance(value, str):
            return value if len(value) <= 768 else value[:768] + "…"
        if isinstance(value, list):
            return [preview_value(item, depth + 1) for item in value[:3]]
        if isinstance(value, dict):
            return {
                str(key): preview_value(value[key], depth + 1)
                for key in ordered_mapping_keys(value)[:10]
            }
        return value

    wrapper = {
        "schema_id": "aiworkhub.task_mcp.bounded_json_preview.v1",
        "truncated": True,
        "original_bytes": len(encoded),
        "original_sha256": hashlib.sha256(encoded).hexdigest(),
        "original_hit_count": _json_hit_count(payload),
        "preview_semantics": "structure_aware_priority_preserving",
        "preview": {},
    }
    preview: dict[str, Any] = wrapper["preview"]
    ordered_keys = [key for key in (*identity_keys, *priority_keys) if key in payload]
    ordered_keys.extend(key for key in sorted(payload) if key not in ordered_keys)
    omitted = 0
    for key in ordered_keys:
        candidate_value = preview_value(payload[key])
        preview[key] = candidate_value
        candidate = json.dumps(
            wrapper, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if len(candidate.encode("utf-8")) <= max_bytes:
            continue
        preview.pop(key, None)
        omitted += 1
        if key in priority_keys:
            value = payload[key]
            preview[key] = {
                "truncated": True,
                "original_items": len(value) if isinstance(value, (list, dict)) else 1,
            }
            candidate = json.dumps(
                wrapper, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if len(candidate.encode("utf-8")) > max_bytes:
                preview.pop(key, None)
    wrapper["omitted_key_count"] = omitted
    wrapper["priority_keys_present"] = [key for key in priority_keys if key in preview]
    bounded = json.dumps(wrapper, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    while len(bounded.encode("utf-8")) > max_bytes:
        removable = next(
            (key for key in reversed(list(preview)) if key not in priority_keys),
            None,
        )
        if removable is None:
            break
        preview.pop(removable, None)
        wrapper["omitted_key_count"] += 1
        bounded = json.dumps(wrapper, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return bounded, True


def _suppress_irrelevant_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate/empty payloads without hiding tool outcomes.

    A zero-hit executed section carries no bootstrap value: the launcher has
    already measured that the canonical query returned nothing for this
    request, so its model-visible content is emptied while ``executed``,
    ``hit_count`` and ``degraded_reason`` stay in metadata -- that is the
    evidence the launcher gate credits as the tool having run. Session
    Manager joins AI Memory / KB / skills here (worker_prompt-2): its
    zero-hit envelope was injected into every request and bought nothing.
    Source Graph is never suppressed -- its zero-hit orientation names why
    it is empty, which the worker must see.
    """

    out: list[dict[str, Any]] = []
    seen_relevant: set[str] = set()
    for section in sections:
        optimized = dict(section)
        name = str(optimized.get("name") or "")
        relevant = int(optimized.get("hit_count") or 0) > 0
        required_source = name == "source_graph" and bool(optimized.get("required"))
        content_sha = str(optimized.get("sha256") or "")
        if (
            optimized.get("executed")
            and not relevant
            and name in {"session_current_state", "ai_memory", "kb", "skills"}
        ):
            optimized["content"] = ""
            optimized["content_suppressed"] = True
            optimized["suppression_reason"] = "zero_hit_optional_tool"
            optimized["bytes_before_suppression"] = int(section.get("bytes") or 0)
            # Carry the degraded reason through suppression. Emptying the
            # content must not also erase WHY it is empty: "the tool ran and
            # found nothing" and "the tool failed" are different facts, and a
            # reader that cannot tell them apart reads a failure as a clean
            # zero. The other suppression branch below applies only to a
            # relevant, non-degraded duplicate, so it has no reason to carry.
            optimized.update(
                _safe_tool_result(
                    name,
                    "",
                    truncated=False,
                    degraded=str(section.get("degraded_reason") or ""),
                )
            )
        elif relevant and not required_source and content_sha and content_sha in seen_relevant:
            optimized["content"] = ""
            optimized["content_suppressed"] = True
            optimized["suppression_reason"] = "relevance_duplicate"
            optimized["bytes_before_suppression"] = int(section.get("bytes") or 0)
            optimized.update(_safe_tool_result(name, "", truncated=False))
        else:
            optimized["content_suppressed"] = False
            optimized["suppression_reason"] = ""
            if relevant and content_sha:
                seen_relevant.add(content_sha)
        out.append(optimized)
    return out


def _validate_contract(card: dict[str, Any]) -> dict[str, Any] | None:
    raw = card.get("project_context")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProjectContextError("project_context_must_be_object")
    required = raw.get("required", False)
    if not isinstance(required, bool):
        raise ProjectContextError("project_context.required_must_be_bool")
    task_type = _task_context_kind(card, raw)

    source = raw.get("source_graph")
    if source is None and task_type == "data_classification":
        source = {"mode": "focus", "query": "data_classification", "budget": 16}
    if not isinstance(source, dict):
        raise ProjectContextError("project_context.source_graph_must_be_object")
    mode = source.get("mode")
    if mode not in SOURCE_GRAPH_MODES:
        raise ProjectContextError("project_context.source_graph.mode_invalid")
    source_query = _bounded_str(
        source.get("query"), field="project_context.source_graph.query", max_bytes=MAX_QUERY_BYTES
    )
    source_budget = _bounded_int(
        source.get("budget", 64),
        field="project_context.source_graph.budget",
        minimum=8,
        maximum=MAX_BUDGET,
    )
    bundle_type = source.get("bundle_type", "explore")
    if bundle_type not in SOURCE_GRAPH_BUNDLE_TYPES:
        raise ProjectContextError("project_context.source_graph.bundle_type_invalid")
    source_required = source.get("required")
    if source_required is None:
        source_required = task_type != "data_classification"
    if not isinstance(source_required, bool):
        raise ProjectContextError("project_context.source_graph.required_must_be_bool")
    explicit_target_values = source.get("targets") or source.get("exact_targets")
    exact_targets = _bounded_targets(
        explicit_target_values,
        field="project_context.source_graph.targets",
    )
    targets_origin = "declared" if exact_targets else "derived"
    if not exact_targets:
        exact_targets = _derive_targets(card)

    session = raw.get("session")
    if not isinstance(session, dict):
        raise ProjectContextError("project_context.session_must_be_object")
    session_topic = _bounded_str(
        session.get("topic"), field="project_context.session.topic", max_bytes=MAX_TOPIC_BYTES
    )
    session_limit = _bounded_int(
        session.get("limit", 8),
        field="project_context.session.limit",
        minimum=1,
        maximum=20,
    )

    kb = raw.get("kb")
    kb_query = None
    kb_limit = 5
    if kb is not None:
        if not isinstance(kb, dict):
            raise ProjectContextError("project_context.kb_must_be_object")
        kb_query = _bounded_str(
            kb.get("query"), field="project_context.kb.query", max_bytes=MAX_QUERY_BYTES
        )
        kb_limit = _bounded_int(
            kb.get("limit", 5),
            field="project_context.kb.limit",
            minimum=1,
            maximum=10,
        )

    ai_memory = raw.get("ai_memory")
    memory_query = None
    memory_limit = 5
    if ai_memory is not None:
        if not isinstance(ai_memory, dict):
            raise ProjectContextError("project_context.ai_memory_must_be_object")
        memory_query = _bounded_str(
            ai_memory.get("query"), field="project_context.ai_memory.query", max_bytes=MAX_QUERY_BYTES
        )
        memory_limit = _bounded_int(
            ai_memory.get("limit", 5),
            field="project_context.ai_memory.limit",
            minimum=1,
            maximum=10,
        )

    input_shard = raw.get("input_shard") or card.get("immutable_input_shard")
    if input_shard is not None:
        input_shard = _bounded_str(
            input_shard, field="project_context.input_shard", max_bytes=MAX_TARGET_BYTES
        )

    return {
        "required": required,
        "task_type": task_type,
        "source_graph": {
            "mode": mode,
            "query": source_query,
            "budget": source_budget,
            "bundle_type": bundle_type,
            "required": source_required,
            "targets": exact_targets,
            "targets_origin": targets_origin,
        },
        "session": {"topic": session_topic, "limit": session_limit},
        "kb": {"query": kb_query, "limit": kb_limit} if kb_query else None,
        "ai_memory": {"query": memory_query, "limit": memory_limit} if memory_query else None,
        "input_shard": input_shard,
    }


def _target_path(repo: Path, target: str) -> Path | None:
    """Resolve a contract target inside ``repo``, or ``None`` when it escapes."""

    try:
        root = repo.resolve()
        candidate = (root / target).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


def _compact_entity(row: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "kind": str(row.get("kind") or ""),
        "name": str(row.get("name") or ""),
        "line_start": row.get("line_start"),
        "line_end": row.get("line_end"),
    }
    signature = str(row.get("signature") or "")
    if signature:
        compact["signature"] = signature[:ORIENTATION_SIGNATURE_CHARS]
    return compact


def _compact_symbol(row: dict[str, Any]) -> dict[str, Any]:
    qualname = str(row.get("qualname") or row.get("name") or "")
    compact: dict[str, Any] = {
        "qualname": qualname,
        "kind": str(row.get("kind") or ""),
        "line_start": row.get("line_start"),
        "line_end": row.get("line_end"),
    }
    file_path = str(row.get("file_path") or "")
    if file_path and not qualname.startswith(file_path):
        compact["file_path"] = file_path
    return compact


def _orientation_file_row(
    sg: Any, repo: Path, target: str, budget: int,
) -> dict[str, Any] | None:
    """One indexed target as the worker's ``file`` mode would show it, minus
    the raw source preview (``file``/``body`` modes and bounded reads exist
    for that). ``None`` when the index does not hold ``target``."""

    payload = sg.file_query(repo, target, budget)
    contexts = payload.get("contexts") if isinstance(payload, dict) else None
    context = contexts[0] if isinstance(contexts, list) and contexts else None
    if not isinstance(context, dict) or not context.get("found"):
        return None
    file_row = context.get("file") if isinstance(context.get("file"), dict) else {}
    entities = [
        row for row in (context.get("entities") or [])
        # The module row repeats the path the target already names, and the
        # import rows (first by line, so they would fill the whole allowance)
        # are dependency evidence the ``deps`` mode owns, not definitions.
        if isinstance(row, dict) and str(row.get("kind") or "") not in {"module", "import"}
    ]
    return {
        "target": target,
        "language": str(file_row.get("language") or ""),
        "status": str(file_row.get("status") or ""),
        "entities": [_compact_entity(row) for row in entities],
    }


def _orientation_directory_row(
    sg: Any, repo: Path, target: str, query: str, budget: int,
) -> dict[str, Any] | None:
    """A directory target as its top-ranked in-scope symbols (``symbols``
    analytic scoped to the directory), or ``None`` when nothing is indexed
    beneath it."""

    try:
        payload = sg.analytics_query(
            repo, "symbols", query, min(budget, ORIENTATION_DIRECTORY_SYMBOLS),
            target=target,
        )
    except sg.SourceGraphError:
        # ``file_query`` already opened the same database for this target, so
        # what remains here is a target the analytic scope grammar refuses.
        return None
    symbols = [
        row for row in (payload.get("symbols") or []) if isinstance(row, dict)
    ] if isinstance(payload, dict) else []
    if not symbols:
        return None
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    eligible = coverage.get("eligible")
    return {
        "target": target,
        "kind": "directory",
        "symbols": [_compact_symbol(row) for row in symbols[:ORIENTATION_DIRECTORY_SYMBOLS]],
        "symbol_count": int(eligible) if isinstance(eligible, int) and not isinstance(eligible, bool) else len(symbols),
    }


def _orientation_empty_reason(targets: list[str], unindexed_on_disk: int) -> str:
    if not targets:
        return "no_targets"
    if unindexed_on_disk == 0:
        return "targets_not_on_disk"
    return "targets_unindexed"


def _orientation_rows_key(row: dict[str, Any]) -> str:
    """A file row carries ``entities``; a directory row carries ``symbols``."""

    return "entities" if "entities" in row else "symbols"


def _orientation_size(payload: dict[str, Any]) -> int:
    return len(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _fit_orientation(payload: dict[str, Any], cap: int) -> dict[str, Any]:
    """Deterministically trim the orientation to ``cap`` bytes.

    Least-specific evidence goes first: focus insights, then focus matches,
    then entity signatures, then the longest entity list (halved, never
    below the per-target floor), then whole matches, then the last target
    row. Identical input always yields the identical trimmed payload.
    """

    insight_keys = ("related_tests", "ranked_symbols", "recommended_next_steps")
    files: list[dict[str, Any]] = payload["files"]
    orientation: dict[str, Any] = payload["orientation"]
    trimmed = False
    while _orientation_size(payload) > cap:
        if any(key in payload for key in insight_keys):
            for key in insight_keys:
                payload.pop(key, None)
        elif len(payload["matches"]) > ORIENTATION_ENTITIES_PER_TARGET_MIN:
            payload["matches"] = payload["matches"][: len(payload["matches"]) // 2]
        elif any("signature" in entity for row in files for entity in row.get("entities", ())):
            for row in files:
                for entity in row.get("entities", ()):
                    entity.pop("signature", None)
        elif any(
            len(row.get(_orientation_rows_key(row)) or ()) > ORIENTATION_ENTITIES_PER_TARGET_MIN
            for row in files
        ):
            longest = max(files, key=lambda row: len(row.get(_orientation_rows_key(row)) or ()))
            key = _orientation_rows_key(longest)
            rows = longest[key]
            keep = max(ORIENTATION_ENTITIES_PER_TARGET_MIN, len(rows) // 2)
            longest[f"{key}_omitted"] = int(longest.get(f"{key}_omitted") or 0) + (len(rows) - keep)
            longest[key] = rows[:keep]
        elif payload["matches"]:
            payload["matches"] = []
        elif len(files) > 1:
            files.pop()
            orientation["omitted_targets"] = int(orientation.get("omitted_targets") or 0) + 1
        elif files and files[0].get(_orientation_rows_key(files[0])):
            key = _orientation_rows_key(files[0])
            files[0][f"{key}_omitted"] = int(files[0].get(f"{key}_omitted") or 0) + len(files[0][key])
            files[0][key] = []
        else:
            break
        trimmed = True
    if trimmed:
        payload["truncated"] = True
    return payload


def _focus_orientation(sg: Any, repo: Path, contract: dict[str, Any]) -> dict[str, Any]:
    """Build the injected orientation deterministically from the card's targets.

    The orientation inputs are card facts the coordinator already holds:
    ``read_first`` / ``immutable_inputs`` / ``allowed_writes`` become the
    contract targets (declared or derived, bounded by ``MAX_TARGETS``). Each
    target is previewed through the same ``file`` mode a worker would call
    (``symbols`` scoped to the directory for a directory target), and the
    manager's free-text focus query rides along as a second slice only when
    it hits. Running ``focus`` alone on that free text -- 72% path tokens --
    produced an empty section in 680/680 bundles while every worker re-ran
    orientation from zero (worker_prompt-1). The whole payload is fitted to
    ``SOURCE_GRAPH_ORIENTATION_BYTES`` so the prompt cannot grow past what
    the first live focus call it replaces would have cost.
    """

    source = contract["source_graph"]
    query = str(source["query"])
    budget = int(source["budget"])
    targets = [str(target) for target in source["targets"]][:MAX_TARGETS]
    # ``file_query`` derives its entity ceiling from ``budget // 2``; keep the
    # preview budget high enough to reach the per-target ceiling.
    preview_budget = min(MAX_BUDGET, max(budget, 2 * ORIENTATION_ENTITIES_PER_TARGET_MAX))
    files: list[dict[str, Any]] = []
    unindexed: list[str] = []
    unindexed_on_disk = 0
    for target in targets:
        row = _orientation_file_row(sg, repo, target, preview_budget)
        resolved = _target_path(repo, target) if row is None else None
        if row is None and resolved is not None and resolved.is_dir():
            row = _orientation_directory_row(sg, repo, target, query, budget)
        if row is None:
            unindexed.append(target)
            if resolved is not None and resolved.is_file():
                unindexed_on_disk += 1
            continue
        files.append(row)

    focus_payload = sg.focus(repo, query, budget)
    if not isinstance(focus_payload, dict):
        focus_payload = {}
    focus_matches = [
        row for row in (focus_payload.get("matches") or []) if isinstance(row, dict)
    ]

    per_target = max(
        ORIENTATION_ENTITIES_PER_TARGET_MIN,
        min(
            ORIENTATION_ENTITIES_PER_TARGET_MAX,
            ORIENTATION_ENTITY_ALLOWANCE // max(1, len(files)),
        ),
    )
    for row in files:
        entities = row.get("entities")
        if isinstance(entities, list) and len(entities) > per_target:
            row["entities_omitted"] = len(entities) - per_target
            row["entities"] = entities[:per_target]

    orientation: dict[str, Any] = {
        "schema_id": SOURCE_GRAPH_ORIENTATION_SCHEMA_ID,
        "targets_origin": str(source["targets_origin"]),
        "targets": len(targets),
        "indexed": len(files),
        "unindexed": len(unindexed),
        "unindexed_on_disk": unindexed_on_disk,
        "focus_hit_count": len(focus_matches),
    }
    if not files and not focus_matches:
        orientation["empty_reason"] = _orientation_empty_reason(targets, unindexed_on_disk)
    payload: dict[str, Any] = {
        "mode": "focus",
        "query": query,
        "budget": budget,
        "orientation": orientation,
        "files": files,
        "unindexed_targets": unindexed,
        "matches": [_compact_symbol(row) for row in focus_matches[:ORIENTATION_FOCUS_MATCHES]],
        "query_tokens": [str(token) for token in (focus_payload.get("query_tokens") or [])],
        "candidate_files": [
            str(path) for path in (focus_payload.get("candidate_files") or [])
        ][:ORIENTATION_INSIGHT_ROWS],
        "truncated": bool(focus_payload.get("truncated")),
    }
    if focus_matches:
        # The metrics used to arrive as a separate ``ranked_symbols`` list.
        # That list was a second copy of the matches -- measured at 25-45% of
        # a focus payload -- and was folded into the match rows themselves, so
        # reading it here would now silently yield nothing and every worker's
        # injected orientation would quietly lose its priority/risk hints.
        # Rank from the rows that carry the score, in the engine's own
        # ``(-priority_score, qualname)`` order, so the ordering survives the
        # fold rather than depending on the caller's arrival order.
        scored = sorted(
            (
                row
                for row in focus_matches
                if isinstance(row.get("priority_score"), int)
                and not isinstance(row.get("priority_score"), bool)
            ),
            key=lambda row: (
                -int(row["priority_score"]),
                str(row.get("qualname") or ""),
            ),
        )
        ranked = [
            {
                "qualname": str(row.get("qualname") or ""),
                "priority_score": row.get("priority_score"),
                "risk_reasons": [str(reason) for reason in (row.get("risk_reasons") or [])][:3],
            }
            for row in scored
        ][:ORIENTATION_INSIGHT_ROWS]
        if ranked:
            payload["ranked_symbols"] = ranked
        tests = [
            str(row.get("file_path"))
            for row in (focus_payload.get("related_tests") or [])
            if isinstance(row, dict) and row.get("file_path")
        ][:ORIENTATION_INSIGHT_ROWS]
        if tests:
            payload["related_tests"] = tests
        steps = [str(step) for step in (focus_payload.get("recommended_next_steps") or [])][:4]
        if steps:
            payload["recommended_next_steps"] = steps
    return _fit_orientation(payload, SOURCE_GRAPH_ORIENTATION_BYTES)


def _source_graph_empty_reason(source_text: str) -> str:
    """The orientation's own ``empty_reason``, or ``""`` for any other payload."""

    try:
        payload = json.loads(source_text)
    except (TypeError, ValueError):
        return ""
    orientation = payload.get("orientation") if isinstance(payload, dict) else None
    if not isinstance(orientation, dict):
        return ""
    return str(orientation.get("empty_reason") or "")


def _orientation_metadata(source_text: str) -> dict[str, Any] | None:
    """Counts-only view of the orientation block for process metadata.

    Never paths or query text: metadata is redacted evidence (B434/B437).
    """

    try:
        payload = json.loads(source_text)
    except (TypeError, ValueError):
        return None
    orientation = payload.get("orientation") if isinstance(payload, dict) else None
    if not isinstance(orientation, dict):
        return None
    return {
        key: value
        for key, value in orientation.items()
        if isinstance(value, (int, str)) and not isinstance(value, bool)
    }


def _source_graph_direct(repo: Path, contract: dict[str, Any]) -> tuple[str, bool]:
    """Query the canonical AIWorkHub Source Graph in-process.

    This is a direct import call into :mod:`aiworkhub.source_graph` -- no
    subprocess and no repository-provided helper-script dependency.
    The repository (and therefore the durable database under
    ``<repo>/.aiworkhub/source_graph``) is resolved from ``repo`` via
    repository identity, never a fixed path or ambient ``cwd``.

    ``focus`` -- the orientation mode every launcher-made card declares --
    is built by :func:`_focus_orientation` from the contract targets; every
    other mode runs the one declared query exactly as written.
    """

    from . import source_graph as _source_graph_mod

    source = contract["source_graph"]
    mode = source["mode"]
    query = source["query"]
    target = (
        source["targets"][0]
        if source["targets_origin"] == "declared" and source["targets"]
        else None
    )
    try:
        if mode == "bundle":
            payload = _source_graph_mod.bundle(
                repo, source["bundle_type"], query, source["budget"]
            )
        elif mode == "slice":
            payload = _source_graph_mod.slice_(
                repo, query, source["budget"], target=target,
            )
        elif mode == "context":
            payload = _source_graph_mod.context_query(repo, query, source["budget"])
        elif mode == "file":
            payload = _source_graph_mod.file_query(repo, target or query, source["budget"])
        elif mode == "function":
            payload = _source_graph_mod.function_query(repo, query, source["budget"])
        elif mode == "class":
            payload = _source_graph_mod.class_query(repo, query, source["budget"])
        elif mode == "body":
            payload = _source_graph_mod.body_query(repo, query, source["budget"])
        elif mode == "bodygrep":
            payload = _source_graph_mod.bodygrep_query(
                repo, query, source["budget"], target=target,
            )
        elif mode == "impact":
            payload = _source_graph_mod.impact(repo, query, source["budget"])
        elif mode == "trace":
            payload = _source_graph_mod.trace(repo, query, source["budget"])
        elif mode == "deps":
            payload = _source_graph_mod.deps_query(repo, query, source["budget"])
        elif mode == "focus":
            payload = _focus_orientation(_source_graph_mod, repo, contract)
        elif mode in _source_graph_mod.SOURCE_GRAPH_MODES:
            payload = _source_graph_mod.analytics_query(
                repo, mode, query, source["budget"]
            )
        else:
            raise ProjectContextError(f"source_graph_mode_unimplemented:{mode}")
    except (_source_graph_mod.SourceGraphError, sqlite3.Error, OSError) as exc:
        raise ProjectContextError(f"source_graph_query_failed:{exc}") from exc
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return canonical, False


def _worker_tool_context(repo: Path, card: dict[str, Any], contract: dict[str, Any]) -> _worker_tools.WorkerToolContext:
    """Bind this precomputation pass to the same canonical, in-process
    Session Manager / AI Memory / KB readers ``worker_ai_tools_mcp`` exposes
    to a live worker MCP call -- no subprocess, no direct SQLite reader
    duplicated here. ``repo`` is the coordinator-owned host repository (never
    an isolated worktree), so it is bound as both ``repo`` and
    ``authority_repo``; there is no per-request audit ledger for this
    precomputation pass (``audit_ledger_path=None`` is a documented no-op for
    ``_append_audit``).
    """

    return _worker_tools.WorkerToolContext(
        task_id=str(card.get("task_id") or ""),
        runner=str(card.get("runner") or ""),
        topic=str(card.get("topic") or ""),
        request_id="",
        repo=repo,
        authority_repo=repo,
        source_graph_targets=tuple(contract["source_graph"]["targets"]),
        session_topic=contract["session"]["topic"],
        audit_ledger_path=None,
        audit_hmac_key_path=None,
    )


def _canonical_tool_result(result: dict[str, Any], tool: str) -> tuple[str, bool, int]:
    """Unwrap a bounded ``worker_ai_tools_mcp`` tool result, or raise.

    Those tools never raise -- a missing/non-canonical authority database or
    an invalid argument comes back as ``{"ok": False, "reason": ...}`` (see
    ``_violation`` in ``worker_ai_tools_mcp``) -- so this is what turns that
    into the same fail-closed/degrade contract this module's callers expect.
    """

    if not result.get("ok"):
        raise ProjectContextError(f"context_tool_failed:{tool}:{result.get('reason')}")
    return str(result["content"]), bool(result.get("truncated")), int(result.get("hit_count") or 0)


def _prompt_evidence_value(section: dict[str, Any]) -> Any:
    """Return model-visible evidence without nested JSON string escaping.

    Canonical tool JSON is embedded as an object/list. Plain-text tool output
    stays a string. Exceptional delivery facts wrap the evidence only when
    they are actually present, keeping the common path compact while
    preserving truncation/degradation truth.
    """

    content = str(section.get("content") or "")
    value: Any = content
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, (dict, list)):
        value = parsed

    annotations: dict[str, Any] = {}
    if section.get("truncated"):
        annotations["truncated"] = True
    if section.get("degraded_reason"):
        annotations["degraded_reason"] = section["degraded_reason"]
    if not section.get("requested", True):
        annotations["requested"] = False
    if not section.get("executed", True):
        annotations["executed"] = False
    if annotations:
        return {"data": value, **annotations}
    return value


def _degrade_or_raise(required: bool, reason: str) -> tuple[str, bool, str]:
    if required:
        raise ProjectContextError(reason)
    return "", False, reason


def _template_skill_task_family(card: dict[str, Any]) -> str:
    """Return the skill family the card's authenticated template declares.

    A card created from a built-in template records ``template_provenance``
    naming that template. The template's DECLARED work kind is a real family
    (``analysis``, ``implementation``, ``test``, ``docs``, ``replay``,
    ``bugfix``) that ``_canonical_work_kind`` flattens to ``generic`` on the
    card itself, which is why reading ``work_kind`` here would match no skill.
    Resolving it on the read side keeps card shape and create-idempotency
    untouched, and gives every already-created template card a real family.
    """
    provenance = card.get("template_provenance")
    if not isinstance(provenance, dict):
        return ""
    name = provenance.get("template_name")
    if not isinstance(name, str) or not name:
        return ""
    from . import task_templates

    spec = task_templates.TEMPLATE_SPECS.get(name)
    if spec is None:
        return ""
    return task_templates.skill_task_family(spec.work_kind)


def _production_path_scope(card: dict[str, Any]) -> str:
    """Return the scope of a card whose write set spans production AND tests.

    A card that writes ``src/aiworkhub/x.py`` and ``tests/test_x.py`` shares no
    directory prefix at all, so the whole-write-set reduction answers "". Its
    scope is nonetheless not ambiguous: the tests follow the code, and the code
    is where the card is about. Measured over the 4,628 live cards, adding this
    fallback takes the share with a single derivable scope from 5% to 30%; the
    remaining cards genuinely span several production roots and must declare
    ``skill_path_scope`` themselves rather than be given a wildcard.
    """
    from . import skill_registry, task_templates

    paths = [
        item
        for item in (card.get("allowed_writes") or card.get("read_first") or [])
        if isinstance(item, str)
    ]
    production, _tests = task_templates._partition_write_set(paths)
    return skill_registry.common_path_scope(production)


def _skill_selection_context(card: dict[str, Any]) -> dict[str, Any] | None:
    """Return the card's selection context, or ``None`` when it declares none."""
    from . import skill_registry

    resolved = dict(card)
    if not str(resolved.get("skill_task_family") or "").strip():
        family = _template_skill_task_family(card)
        if family:
            resolved["skill_task_family"] = family
    if not str(resolved.get("skill_path_scope") or "").strip():
        scope = skill_registry.common_path_scope(
            resolved.get("allowed_writes") or []
        ) or _production_path_scope(resolved)
        if scope:
            resolved["skill_path_scope"] = scope
    return skill_registry.card_selection_context(resolved)


def _skills_section(repo: Path, card: dict[str, Any]) -> dict[str, Any] | None:
    """Return the bounded skill runtime packet section, or ``None``.

    ``None`` means the card declared no selection vocabulary, so no selection
    ran -- the same shape as an absent ``ai_memory``/``kb`` contract. A card
    that DID declare vocabulary always produces a section, even with zero
    matches, so the zero-hit suppression rule can record that the surface
    looked and found nothing instead of hiding that it ran at all.
    """
    from . import skill_registry

    try:
        context = _skill_selection_context(card)
    except skill_registry.SkillRegistryError as exc:
        # A stored card carrying a token this build no longer knows must not
        # take the worker's whole context bundle down with it.
        return _section(
            name="skills",
            content="",
            truncated=False,
            degraded=f"skill_vocabulary_rejected:{exc.code}",
            hit_count=0,
        )
    if context is None:
        return None

    from . import skill_registry_store

    try:
        # load_registry, never list_records: it applies the demotion rule, so a
        # record that self-certified under a weaker reading of independence is
        # loaded as proposed and select() -- which serves ACTIVE only -- will
        # not inject it.
        candidates = skill_registry_store.load_registry(repo).records()
        receipt = skill_registry.select(candidates, context, limit=SKILL_SELECT_LIMIT)
        packet = skill_registry.build_runtime_packet(candidates, receipt)
        # Persist WHICH skills this card received. Without it the retirement
        # report measured cards_with_persisted_packet 0 across 4,681 cards and
        # every skill read injected_cards 0, so a skill could never be shown to
        # have reached a worker. Reported, never raising: a store failure
        # returns {"ok": False, "reason": ...} and the context bundle is
        # unaffected. request_id is empty on purpose -- the launcher mints it
        # on the line AFTER this collection, so the receipt is card-keyed and
        # the decision resolves it from the card's latest receipt.
        skill_registry_store.record_selection_reported(
            repo,
            task_id=str(card.get("task_id") or ""),
            request_id="",
            packet=packet,
            context=context,
        )
    except (
        skill_registry.SkillRegistryError,
        skill_registry_store.SkillStoreError,
        sqlite3.Error,
        OSError,
        ValueError,
    ) as exc:
        # Named, not swallowed: the failing type reaches the worker and the
        # metadata as degraded_reason, so a zero packet is never mistaken for
        # "the store had nothing to say".
        return _section(
            name="skills",
            content="",
            truncated=False,
            degraded=f"skill_selection_failed:{type(exc).__name__}",
            hit_count=0,
        )
    payload = {"schema_id": SKILL_PACKET_SCHEMA_ID, **packet.as_mapping()}
    content = (
        ""
        if not packet.skills
        else json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return _section(
        name="skills",
        task_family=str(context["task_family"]),
        stage=str(context["stage"]),
        content=content,
        truncated=False,
        # The packet row count is the hit count. It is taken from the packet
        # rather than inferred from the JSON shape, so an empty packet reads as
        # zero hits instead of one non-empty object.
        hit_count=len(packet.skills),
    )


def collect_project_context(repo: Path, card: dict[str, Any]) -> ProjectContextResult | None:
    """Return a compact trusted bundle, or ``None`` when no contract exists."""

    repo = repo.resolve()
    contract = _validate_contract(card)
    if contract is None:
        return None
    authority_repo = resolve_task_repository_root(repo, card)
    try:
        repo_id = repository_state.inspect_repository(authority_repo).manifest.repo_id
    except repository_state.RepositoryStateError:
        # Some unit-level callers intentionally exercise context shaping on an
        # uninitialized temporary repository. Production Task MCP repositories
        # are initialized and always provide the concrete identity here.
        repo_id = ""
    contract["source_graph"]["targets"] = _rebase_targets(
        repo, authority_repo, contract["source_graph"]["targets"]
    )
    required = bool(contract["required"])
    ctx = _worker_tool_context(authority_repo, card, contract)

    sections: list[dict[str, Any]] = []

    try:
        source_text, source_truncated = _source_graph_direct(authority_repo, contract)
        source_text, json_truncated = _canonical_json_output(
            "source_graph",
            source_text,
            max_bytes=_tool_cap("source_graph", "bytes", MAX_TOOL_OUTPUT_BYTES),
        )
        source_truncated = source_truncated or json_truncated
    except ProjectContextError as exc:
        source_text, source_truncated, degraded = _degrade_or_raise(
            required and contract["source_graph"]["required"], str(exc)
        )
    else:
        degraded = ""
    source_section = _section(
        name="source_graph",
        mode=contract["source_graph"]["mode"],
        query=contract["source_graph"]["query"],
        target=(
            contract["source_graph"]["targets"][0]
            if contract["source_graph"]["targets_origin"] == "declared"
            and contract["source_graph"]["targets"]
            else contract["source_graph"]["query"]
        ),
        targets=contract["source_graph"]["targets"],
        targets_origin=contract["source_graph"]["targets_origin"],
        required=contract["source_graph"]["required"],
        content=source_text,
        truncated=source_truncated,
        degraded=degraded,
    )
    if (
        required
        and contract["source_graph"]["required"]
        and contract["task_type"] == "code"
        and source_section["hit_count"] <= 0
    ):
        # Fail closed: a required code orientation that found nothing blocks
        # the launch before any claim (the launcher turns this into
        # ``blocked_reason``). The one truthful exception is an orientation
        # that says the card names nothing Source Graph could hold -- no
        # targets, or only paths that do not exist yet -- which is a zero-hit
        # section the worker sees, not a silent one. A card naming on-disk
        # files the index does not know stays blocked: the worker's mandatory
        # live Source Graph use would be blind on the card's own files.
        empty_reason = _source_graph_empty_reason(source_text)
        if empty_reason not in ORIENTATION_EMPTY_EXEMPT_REASONS:
            raise ProjectContextError(
                "source_graph_required_empty_result"
                + (f":{empty_reason}" if empty_reason else "")
            )
    sections.append(source_section)
    source_orientation = _orientation_metadata(source_text)

    try:
        session_result = _worker_tools.session_current_state(ctx, limit=contract["session"]["limit"])
        session_text, session_truncated, session_hit_count = _canonical_tool_result(
            session_result, "session_current_state"
        )
    except ProjectContextError as exc:
        session_text, session_truncated, degraded = _degrade_or_raise(required, str(exc))
        session_hit_count = 0
    else:
        degraded = ""
    sections.append(_section(
        name="session_current_state",
        topic=contract["session"]["topic"],
        content=session_text,
        truncated=session_truncated,
        degraded=degraded,
        hit_count=session_hit_count,
    ))

    if contract.get("ai_memory") is not None:
        try:
            memory_result = _worker_tools.ai_memory_search(
                ctx, query=contract["ai_memory"]["query"], limit=contract["ai_memory"]["limit"],
            )
            memory_text, memory_truncated, memory_hit_count = _canonical_tool_result(memory_result, "ai_memory")
        except ProjectContextError as exc:
            memory_text, memory_truncated, degraded = _degrade_or_raise(False, str(exc))
            memory_hit_count = 0
        else:
            degraded = ""
        sections.append(_section(
            name="ai_memory",
            query=contract["ai_memory"]["query"],
            content=memory_text,
            truncated=memory_truncated,
            degraded=degraded,
            hit_count=memory_hit_count,
        ))

    if contract.get("kb") is not None:
        try:
            kb_result = _worker_tools.kb_search(ctx, query=contract["kb"]["query"], limit=contract["kb"]["limit"])
            kb_text, kb_truncated, kb_hit_count = _canonical_tool_result(kb_result, "kb")
        except ProjectContextError as exc:
            kb_text, kb_truncated, degraded = _degrade_or_raise(False, str(exc))
            kb_hit_count = 0
        else:
            degraded = ""
        sections.append(_section(
            name="kb",
            query=contract["kb"]["query"],
            content=kb_text,
            truncated=kb_truncated,
            degraded=degraded,
            hit_count=kb_hit_count,
        ))

    # Repository skill packet: the card's declared selection vocabulary, run
    # against the ACTIVE records in the skills store. Absent vocabulary means
    # no section at all (like an absent ai_memory/kb contract); declared
    # vocabulary with zero matches means an executed, zero-hit section that the
    # same suppression rule empties. Never raises: a worker's context bundle
    # does not depend on the skills store being present or well-formed.
    skills_section = _skills_section(authority_repo, card)
    if skills_section is not None:
        sections.append(skills_section)

    if required and not any(s["content"].strip() for s in sections):
        raise ProjectContextError("project_context_required_empty_evidence")

    raw_sections = sections
    sections = _suppress_irrelevant_sections(raw_sections)

    # Progressive-disclosure bootstrap packet: a compact schema marker, only
    # the task-context policy the model cannot already read off
    # WORKER_CONTRACT_JSON (primary_context steer + the immutable input shard,
    # which is not otherwise echoed into the contract), and non-empty
    # evidence. Query/budget/targets/session-topic/caps/hashes are already in
    # the card or metadata, so they are not repeated here.
    scope_root = (
        "."
        if authority_repo == repo
        else authority_repo.relative_to(repo).as_posix()
    )
    prompt_payload: dict[str, Any] = {"schema_id": SCHEMA_ID}
    if contract["task_type"] != "code" or contract.get("input_shard"):
        policy: dict[str, Any] = {
            "primary_context": (
                "immutable_input_shard"
                if contract["task_type"] == "data_classification"
                else "source_graph"
            ),
        }
        if contract.get("input_shard"):
            policy["immutable_input_shard"] = contract["input_shard"]
        prompt_payload["task_context_policy"] = policy
    prompt_payload["repo_identity"] = {
        "repo_id": repo_id,
        "scope_root": scope_root,
    }

    prompt_evidence: dict[str, Any] = {}
    legacy_prompt_sections: list[dict[str, Any]] = []
    for section in sections:
        if section.get("content_suppressed") or not str(section.get("content") or "").strip():
            # Zero-hit optional tools and empty/degraded evidence carry no
            # bootstrap value; their facts still live in metadata below.
            continue
        legacy_entry: dict[str, Any] = {
            "name": section["name"],
            "hit_count": section["hit_count"],
            "content": section["content"],
        }
        if section["name"] == "source_graph" and section.get("target"):
            legacy_entry["target"] = section["target"]
        if section.get("truncated"):
            legacy_entry["truncated"] = True
        if section.get("degraded_reason"):
            legacy_entry["degraded_reason"] = section["degraded_reason"]
        if not section.get("requested", True):
            legacy_entry["requested"] = False
        if not section.get("executed", True):
            legacy_entry["executed"] = False
        legacy_prompt_sections.append(legacy_entry)
        prompt_evidence[str(section["name"])] = _prompt_evidence_value(section)
    prompt_payload["evidence"] = prompt_evidence

    legacy_prompt_payload: dict[str, Any] = {
        "schema_id": LEGACY_SCHEMA_ID,
        "source_graph": {"mode": contract["source_graph"]["mode"]},
        "repo_identity": {"repo_id": repo_id, "scope_root": scope_root},
        "sections": legacy_prompt_sections,
    }
    if "task_context_policy" in prompt_payload:
        legacy_prompt_payload["task_context_policy"] = prompt_payload[
            "task_context_policy"
        ]
    legacy_bundle = "PROJECT_CONTEXT_BUNDLE:\n" + json.dumps(
        legacy_prompt_payload,
        ensure_ascii=False,
        sort_keys=True,
    )

    bundle = (
        "PROJECT_CONTEXT_BUNDLE:\n"
        + json.dumps(
            prompt_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    encoded = bundle.encode("utf-8")
    if len(encoded) > MAX_BUNDLE_BYTES:
        if required:
            raise ProjectContextError(f"project_context_bundle_budget_overflow:{len(encoded)}")
        bundle = encoded[:MAX_BUNDLE_BYTES].decode("utf-8", errors="replace")
    metadata = {
        "schema_id": SCHEMA_ID,
        "required": required,
        "task_context_policy": {
            "task_type": contract["task_type"],
            "source_graph_required": contract["source_graph"]["required"],
            "primary_context": (
                "immutable_input_shard"
                if contract["task_type"] == "data_classification"
                else "source_graph"
            ),
            "immutable_input_shard_sha256": (
                _sha256_text(contract["input_shard"]) if contract.get("input_shard") else ""
            ),
        },
        "bundle_sha256": _sha256_text(bundle),
        "bundle_bytes": len(bundle.encode("utf-8")),
        # Counts only (targets/indexed/unindexed/focus hits/empty_reason):
        # how the injected orientation was built, without its paths or query.
        "source_graph_orientation": source_orientation,
        "repo_identity": {
            "repo_id": repo_id,
            "repo_root": str(authority_repo),
            "scope_root": (
                "."
                if authority_repo == repo
                else authority_repo.relative_to(repo).as_posix()
            ),
        },
        # Historical key retained for readers of older process events. This
        # population is tool-section payload before optional zero-hit/dedup
        # suppression; it is not raw repository-file or counterfactual read
        # volume.
        "estimated_raw_context_bytes": sum(section["bytes"] for section in raw_sections),
        "pre_optimization_section_bytes": sum(
            section["bytes"] for section in raw_sections
        ),
        "optimized_context_bytes": sum(section["bytes"] for section in sections),
        "optimization": {
            "schema_id": "aiworkhub.task_mcp.project_context_optimization.v1",
            "per_tool_hard_caps": TOOL_CAPS,
            "zero_hit_suppression_count": sum(
                1 for section in sections
                if section.get("suppression_reason") == "zero_hit_optional_tool"
            ),
            "relevance_deduplication_count": sum(
                1 for section in sections
                if section.get("suppression_reason") == "relevance_duplicate"
            ),
            "suppressed_bytes": sum(
                int(section.get("bytes_before_suppression") or 0)
                for section in sections
                if section.get("content_suppressed")
            ),
            "byte_labels_are_token_truth": False,
            "adaptive_optional_tools": True,
            "empty_ceremonial_calls_injected": False,
            "prompt_encoding": {
                "schema_id": ENCODING_SCHEMA_ID,
                "legacy_v1_bundle_bytes": len(legacy_bundle.encode("utf-8")),
                "nested_v2_bundle_bytes": len(bundle.encode("utf-8")),
                "delta_bytes": (
                    len(bundle.encode("utf-8"))
                    - len(legacy_bundle.encode("utf-8"))
                ),
                "reduction_percent": round(
                    100.0
                    * (
                        1.0
                        - len(bundle.encode("utf-8"))
                        / len(legacy_bundle.encode("utf-8"))
                    ),
                    3,
                ),
                "json_string_reencoding_avoided": True,
                "token_savings_available": False,
            },
        },
        "estimated_raw_context_vs_bundle_bytes": {
            "label": (
                "pre_optimization_tool_sections_vs_optimized_sections_and_"
                "bundle_bytes_not_token_or_cost_truth"
            ),
            "population_definition": (
                "tool_section_payload_before_optional_suppression_not_raw_"
                "repository_files_or_counterfactual_reads"
            ),
            "pre_optimization_section_bytes": sum(
                section["bytes"] for section in raw_sections
            ),
            "optimized_section_bytes": sum(section["bytes"] for section in sections),
            "raw_context_bytes": sum(section["bytes"] for section in raw_sections),
            "optimized_context_bytes": sum(section["bytes"] for section in sections),
            "bundle_bytes": len(bundle.encode("utf-8")),
            "optimization_delta_bytes": (
                sum(section["bytes"] for section in raw_sections)
                - sum(section["bytes"] for section in sections)
            ),
            "envelope_delta_bytes": (
                len(bundle.encode("utf-8"))
                - sum(section["bytes"] for section in sections)
            ),
            "delta_bytes": sum(section["bytes"] for section in raw_sections) - len(bundle.encode("utf-8")),
            "raw_file_counterfactual_available": False,
            "token_savings_available": False,
        },
        "section_count": len(sections),
        "sections": [
            {
                "name": section["name"],
                "requested": section["requested"],
                "executed": section["executed"],
                "hit_count": section["hit_count"],
                "bytes": section["bytes"],
                "sha256": section["sha256"],
                "truncated": section["truncated"],
                "degraded_reason": section["degraded_reason"],
            }
            for section in sections
        ],
    }
    return ProjectContextResult(
        prompt_bundle=bundle,
        metadata=metadata,
        worker_source_graph_targets=tuple(contract["source_graph"]["targets"]),
        worker_session_topic=contract["session"]["topic"],
    )


__all__ = [
    "ProjectContextError",
    "ProjectContextResult",
    "SCHEMA_ID",
    "collect_project_context",
    "resolve_task_repository_root",
]
