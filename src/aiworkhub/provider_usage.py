"""Bounded normalization of provider-reported token usage.

Only structured JSON/JSONL fields are accepted as token authority.  The
normalizer deliberately does not estimate usage from bytes, text, or model
limits.  It is shared by the live supervisor and the durable process ledger so
the enforcement and accounting paths cannot silently interpret provider
events differently.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_MAX_BYTES = 32 * 1024 * 1024
MAX_JSON_EVENTS = 8192
MAX_WALK_DEPTH = 8
MAX_WALK_ITEMS = 256
MAX_USAGE_SAMPLES = 64


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _first_present(container: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in container and container.get(key) is not None:
            return container[key]
    return None


def _parse_json_fragment(raw: str) -> Any | None:
    candidate = raw.strip()
    if candidate.startswith("data:"):
        candidate = candidate[5:].strip()
    if not candidate or candidate[0] not in "{[":
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _event_type_name(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_content_event_type(event_type: str) -> bool:
    return event_type.replace("-", "_") in {"text", "reasoning", "reasoning_content"}


def _is_token_delta_event_type(event_type: str) -> bool:
    if event_type == "message_delta":
        return False
    return "delta" in event_type.replace("-", "_")


def _opencode_parent_id(sources: tuple[dict[str, Any], ...]) -> str:
    for source in sources:
        containers: list[Any] = [
            source,
            source.get("properties"),
            source.get("info"),
            source.get("part"),
        ]
        props = source.get("properties")
        if isinstance(props, dict):
            containers.append(props.get("part"))
        for container in containers:
            if not isinstance(container, dict):
                continue
            parent = container.get("parentID")
            if parent is None:
                parent = container.get("parent_id")
            if isinstance(parent, str) and parent.strip():
                return parent.strip()[:128]
    return ""


def _opencode_record_identity(
    usage: dict[str, Any],
    cost_sources: tuple[dict[str, Any], ...],
) -> tuple[Any, ...] | None:
    if "total" not in usage:
        return None
    session = ""
    part_id = ""
    message_id = ""
    cost_value = None
    for source in cost_sources:
        if not session:
            raw_session = source.get("sessionID")
            if raw_session is None:
                raw_session = source.get("session_id")
            if isinstance(raw_session, str) and raw_session.strip():
                session = raw_session.strip()[:128]
        if not part_id:
            raw_id = source.get("id")
            if isinstance(raw_id, str) and raw_id.strip():
                part_id = raw_id.strip()[:128]
        if not message_id:
            raw_message = source.get("messageID")
            if raw_message is None:
                raw_message = source.get("message_id")
            if isinstance(raw_message, str) and raw_message.strip():
                message_id = raw_message.strip()[:128]
        if cost_value is None and "cost" in source and source.get("cost") is not None:
            cost_value = source.get("cost")
    if session or part_id or message_id:
        return ("id", session, part_id, message_id)
    cache = usage.get("cache") if isinstance(usage.get("cache"), dict) else {}
    return (
        "fallback",
        usage.get("total"),
        usage.get("input"),
        usage.get("output"),
        usage.get("reasoning"),
        cache.get("read"),
        cache.get("write"),
        cost_value,
    )


def _model_identity(value: Any) -> str:
    if isinstance(value, str):
        candidate = value.strip()
        return candidate[:256] if candidate else ""
    if isinstance(value, dict):
        parts = [
            str(value.get(key) or "").strip()
            for key in ("vendor", "id", "family", "name", "version")
        ]
        candidate = "/".join(part for part in parts if part)
        return candidate[:256]
    return ""


def _empty_summary() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "total_tokens": 0,
        "total_tokens_observed": False,
        "usage_observed": False,
        "cache_metrics_observed": False,
        "cost_usd": None,
        "cost_observed": False,
        "model_observed": False,
        "observed_model": "",
        "usage_sample_count": 0,
        "usage_samples": [],
        "usage_samples_truncated": False,
        # Claude's streamed ``message_delta.usage`` objects are per-turn
        # totals, not cumulative request snapshots.  Keep their disjoint sum
        # separately so the live supervisor can enforce a request-wide cap
        # before the terminal ``result.usage`` aggregate exists.
        "completed_turn_count": 0,
        "completed_turn_input_tokens": 0,
        "completed_turn_output_tokens": 0,
        "completed_turn_reasoning_output_tokens": 0,
        "completed_turn_cached_input_tokens": 0,
        "completed_turn_cache_creation_input_tokens": 0,
        "terminal_result_usage_observed": False,
    }


def read_provider_usage(
    path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    include_samples: bool = True,
) -> dict[str, Any]:
    """Read one bounded provider output and normalize structured usage.

    Samples preserve provider-reported snapshots in arrival order.  They are
    evidence, not assumed deltas: callers that enforce a live cap use the
    maxima/cumulative summary and never sum repeated stream snapshots.
    """

    result = _empty_summary()
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > max_bytes:
            return result
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result

    roots: list[Any] = []
    try:
        roots.append(json.loads(raw))
    except json.JSONDecodeError:
        for line in raw.splitlines()[:MAX_JSON_EVENTS]:
            parsed = _parse_json_fragment(line)
            if parsed is not None:
                roots.append(parsed)

    samples: list[dict[str, Any]] = []
    sample_count = 0
    seen_nodes: set[int] = set()
    seen_usage: set[int] = set()
    seen_opencode_records: set[tuple[Any, ...]] = set()
    seen_additive_cost: set[tuple[int, int]] = set()
    additive_cost_usd = 0.0
    additive_cost_seen = False
    snapshot_cost_usd: float | None = None

    def consume_usage(
        usage: dict[str, Any],
        *,
        event_type: str,
        cost_sources: tuple[dict[str, Any], ...],
        event_index: int,
    ) -> None:
        nonlocal sample_count, snapshot_cost_usd, additive_cost_usd, additive_cost_seen
        usage_id = id(usage)
        if usage_id in seen_usage:
            return
        seen_usage.add(usage_id)
        details = usage.get("input_tokens_details") or usage.get(
            "prompt_tokens_details"
        )
        if not isinstance(details, dict):
            details = {}
        nested_cache = usage.get("cache")
        if not isinstance(nested_cache, dict):
            nested_cache = {}
        cache_nested_observed = any(key in nested_cache for key in ("read", "write"))
        recognized_keys = {
            "input_tokens",
            "prompt_tokens",
            "input",
            "output_tokens",
            "completion_tokens",
            "output",
            "reasoning_output_tokens",
            "reasoning",
            "cache_read_input_tokens",
            "cached_input_tokens",
            "prompt_cache_hit_tokens",
            "cache_creation_input_tokens",
            "cache_write_input_tokens",
            "total",
        }
        if (
            not any(key in usage for key in recognized_keys)
            and "cached_tokens" not in details
            and not cache_nested_observed
        ):
            return
        if _opencode_parent_id(cost_sources):
            return
        opencode_tokens = "total" in usage
        identity = (
            _opencode_record_identity(usage, cost_sources) if opencode_tokens else None
        )
        if identity is not None:
            if identity in seen_opencode_records:
                return
            seen_opencode_records.add(identity)

        input_tokens = _as_int(
            _first_present(usage, "input_tokens", "prompt_tokens", "input")
        )
        output_tokens = _as_int(
            _first_present(usage, "output_tokens", "completion_tokens", "output")
        )
        reasoning_output_tokens = _as_int(
            _first_present(usage, "reasoning_output_tokens", "reasoning")
        )
        cached_input_tokens = _as_int(
            _first_present(
                usage,
                "cache_read_input_tokens",
                "cached_input_tokens",
                "prompt_cache_hit_tokens",
            )
        )
        if cached_input_tokens == 0 and "cached_tokens" in details:
            cached_input_tokens = _as_int(details.get("cached_tokens"))
        if cached_input_tokens == 0 and "read" in nested_cache:
            cached_input_tokens = _as_int(nested_cache.get("read"))
        cache_write_input_tokens = _as_int(
            _first_present(usage, "cache_write_input_tokens")
        )
        if cache_write_input_tokens == 0 and "write" in nested_cache:
            cache_write_input_tokens = _as_int(nested_cache.get("write"))
        if "cache_creation_input_tokens" in usage and usage.get(
            "cache_creation_input_tokens"
        ) is not None:
            cache_creation_input_tokens = _as_int(
                usage.get("cache_creation_input_tokens")
            )
        else:
            cache_creation_input_tokens = cache_write_input_tokens
        cache_observed = any(
            key in usage
            for key in (
                "cache_read_input_tokens",
                "cached_input_tokens",
                "prompt_cache_hit_tokens",
                "cache_creation_input_tokens",
                "cache_write_input_tokens",
            )
        ) or "cached_tokens" in details or cache_nested_observed

        result["usage_observed"] = True
        result["cache_metrics_observed"] = bool(
            result["cache_metrics_observed"] or cache_observed
        )
        combine = int.__add__ if opencode_tokens else max
        if "total" in usage and usage.get("total") is not None:
            result["total_tokens_observed"] = True
            result["total_tokens"] = combine(
                int(result["total_tokens"]), _as_int(usage.get("total"))
            )
        if event_type == "message_delta":
            result["completed_turn_count"] += 1
            result["completed_turn_input_tokens"] += input_tokens
            result["completed_turn_output_tokens"] += output_tokens
            result[
                "completed_turn_reasoning_output_tokens"
            ] += reasoning_output_tokens
            result["completed_turn_cached_input_tokens"] += cached_input_tokens
            result[
                "completed_turn_cache_creation_input_tokens"
            ] += cache_creation_input_tokens
        elif event_type == "result":
            result["terminal_result_usage_observed"] = True
        for key, value in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("reasoning_output_tokens", reasoning_output_tokens),
            ("cached_input_tokens", cached_input_tokens),
            ("cache_creation_input_tokens", cache_creation_input_tokens),
            ("cache_write_input_tokens", cache_write_input_tokens),
        ):
            result[key] = combine(int(result[key]), value)

        for source in (*cost_sources, usage):
            metrics = source.get("metrics") if isinstance(source.get("metrics"), dict) else {}
            snapshot_hit = False
            snapshot_fields: tuple[tuple[str, dict[str, Any]], ...] = (
                ("total_cost_usd", source),
                ("cost_usd", source),
                ("cost", metrics),
            )
            for key, container in snapshot_fields:
                if key in container and container.get(key) is not None:
                    observed = _as_float(container.get(key))
                    snapshot_cost_usd = (
                        observed
                        if snapshot_cost_usd is None
                        else max(snapshot_cost_usd, observed)
                    )
                    snapshot_hit = True
                    break
            if snapshot_hit:
                continue
            if source is usage or "cost" not in source or source.get("cost") is None:
                continue
            cost_identity = (event_index, id(source))
            if cost_identity in seen_additive_cost:
                continue
            seen_additive_cost.add(cost_identity)
            additive_cost_usd += _as_float(source.get("cost"))
            additive_cost_seen = True
        sample_count += 1
        if include_samples and len(samples) < MAX_USAGE_SAMPLES:
            samples.append({
                "sequence": sample_count,
                "event_type": event_type[:96],
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_output_tokens": reasoning_output_tokens,
                "cached_input_tokens": cached_input_tokens,
                "cache_creation_input_tokens": cache_creation_input_tokens,
                "cache_write_input_tokens": cache_write_input_tokens,
                "cache_metrics_observed": cache_observed,
                "semantics": "provider_reported_snapshot",
            })

    def walk(
        value: Any,
        *,
        root: dict[str, Any],
        event_type: str,
        event_index: int,
        depth: int = 0,
    ) -> None:
        if depth > MAX_WALK_DEPTH:
            return
        if isinstance(value, dict):
            node_id = id(value)
            if node_id in seen_nodes:
                return
            seen_nodes.add(node_id)
            if _opencode_parent_id((value,)):
                return
            local_type = _event_type_name(value.get("type") or event_type)
            if _is_content_event_type(local_type) or _is_token_delta_event_type(
                local_type
            ):
                return
            for key in ("model", "model_id"):
                observed_model = _model_identity(value.get(key))
                if observed_model and not result["model_observed"]:
                    result["model_observed"] = True
                    result["observed_model"] = observed_model
            usage = value.get("usage")
            if isinstance(usage, dict):
                consume_usage(
                    usage,
                    event_type=local_type,
                    cost_sources=(root, value),
                    event_index=event_index,
                )
            for alternate in ("token_usage", "tokens"):
                nested_usage = value.get(alternate)
                if isinstance(nested_usage, dict):
                    consume_usage(
                        nested_usage,
                        event_type=local_type,
                        cost_sources=(root, value),
                        event_index=event_index,
                    )
            for key, nested in list(value.items())[:MAX_WALK_ITEMS]:
                if key not in {"usage", "token_usage", "tokens"}:
                    walk(
                        nested,
                        root=root,
                        event_type=local_type,
                        event_index=event_index,
                        depth=depth + 1,
                    )
        elif isinstance(value, list):
            for nested in value[:MAX_WALK_ITEMS]:
                walk(
                    nested,
                    root=root,
                    event_type=event_type,
                    event_index=event_index,
                    depth=depth + 1,
                )

    for event_index, candidate in enumerate(roots[:MAX_JSON_EVENTS]):
        if isinstance(candidate, dict):
            walk(
                candidate,
                root=candidate,
                event_type=str(candidate.get("type") or ""),
                event_index=event_index,
            )
        elif isinstance(candidate, list):
            synthetic_root: dict[str, Any] = {}
            walk(
                candidate,
                root=synthetic_root,
                event_type="",
                event_index=event_index,
            )

    if additive_cost_seen or snapshot_cost_usd is not None:
        result["cost_observed"] = True
        result["cost_usd"] = additive_cost_usd if additive_cost_seen else 0.0
        if snapshot_cost_usd is not None:
            result["cost_usd"] = float(result["cost_usd"]) + snapshot_cost_usd
    result["usage_sample_count"] = sample_count
    result["usage_samples"] = samples
    result["usage_samples_truncated"] = sample_count > len(samples)
    return result


def cumulative_total_tokens(summary: dict[str, Any], adapter_id: str) -> int | None:
    """Return the authoritative cumulative total for live/post-hoc caps."""

    if not summary.get("usage_observed"):
        return None
    if summary.get("total_tokens_observed"):
        return int(summary.get("total_tokens") or 0)
    total = int(summary.get("input_tokens") or 0) + int(
        summary.get("output_tokens") or 0
    )
    total += int(summary.get("reasoning_output_tokens") or 0)
    if adapter_id == "claude_cli":
        total += int(summary.get("cached_input_tokens") or 0)
        total += int(summary.get("cache_creation_input_tokens") or 0)
    return total


def live_total_tokens(summary: dict[str, Any], adapter_id: str) -> int | None:
    """Return request-wide usage that is safe to enforce while streaming.

    Claude emits one final ``message_delta.usage`` record per completed turn;
    those rows are deltas across the request even though each row is a total
    for that one turn.  A maximum therefore undercounts multi-turn workers.
    Once the terminal ``result.usage`` aggregate arrives, it supersedes the
    turn sum.  Other adapters keep their documented cumulative/max behavior.
    """

    if not summary.get("usage_observed"):
        return None
    if summary.get("terminal_result_usage_observed"):
        return cumulative_total_tokens(summary, adapter_id)
    if adapter_id == "claude_cli" and int(summary.get("completed_turn_count") or 0) > 0:
        return sum(
            int(summary.get(key) or 0)
            for key in (
                "completed_turn_input_tokens",
                "completed_turn_output_tokens",
                "completed_turn_reasoning_output_tokens",
                "completed_turn_cached_input_tokens",
                "completed_turn_cache_creation_input_tokens",
            )
        )
    return cumulative_total_tokens(summary, adapter_id)


__all__ = ["cumulative_total_tokens", "live_total_tokens", "read_provider_usage"]
