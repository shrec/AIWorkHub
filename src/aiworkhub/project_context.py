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
}
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
    if isinstance(value, list):
        return len(value) + sum(_json_hit_count(item) for item in value)
    if isinstance(value, dict):
        if value.get("schema_id") == "aiworkhub.task_mcp.bounded_json_preview.v1":
            original_hit_count = value.get("original_hit_count")
            if (
                isinstance(original_hit_count, int)
                and not isinstance(original_hit_count, bool)
                and original_hit_count >= 0
            ):
                return original_hit_count
        total = 0
        saw_result_container = False
        for key, item in value.items():
            if key in {"items", "results", "matches", "rows", "symbols", "files", "sections"}:
                saw_result_container = True
                total += _json_hit_count(item)
            elif isinstance(item, (dict, list)):
                total += _json_hit_count(item)
        if total:
            return total
        if saw_result_container:
            return 0
        return 1 if value else 0
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
                for key in sorted(value, key=str)[:10]
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
    """Drop duplicate/empty optional payloads without hiding tool outcomes."""

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
            and name in {"ai_memory", "kb"}
        ):
            optimized["content"] = ""
            optimized["content_suppressed"] = True
            optimized["suppression_reason"] = "zero_hit_optional_tool"
            optimized["bytes_before_suppression"] = int(section.get("bytes") or 0)
            optimized.update(_safe_tool_result(name, "", truncated=False))
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


def _source_graph_direct(repo: Path, contract: dict[str, Any]) -> tuple[str, bool]:
    """Query the canonical AIWorkHub Source Graph in-process.

    This is a direct import call into :mod:`aiworkhub.source_graph` -- no
    subprocess and no repository-provided helper-script dependency.
    The repository (and therefore the durable database under
    ``<repo>/.aiworkhub/source_graph``) is resolved from ``repo`` via
    repository identity, never a fixed path or ambient ``cwd``.
    """

    from . import source_graph as _source_graph_mod

    source = contract["source_graph"]
    mode = source["mode"]
    target = source["targets"][0] if source["targets_origin"] == "declared" and source["targets"] else source["query"]
    try:
        if mode == "bundle":
            payload = _source_graph_mod.bundle(repo, source["bundle_type"], target, source["budget"])
        elif mode == "slice":
            payload = _source_graph_mod.slice_(repo, target, source["budget"])
        elif mode == "context":
            payload = _source_graph_mod.context_query(repo, target, source["budget"])
        elif mode == "impact":
            payload = _source_graph_mod.impact(repo, target, source["budget"])
        elif mode == "trace":
            payload = _source_graph_mod.trace(repo, target, source["budget"])
        else:
            payload = _source_graph_mod.focus(repo, target, source["budget"])
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
        raise ProjectContextError("source_graph_required_empty_result")
    sections.append(source_section)

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
