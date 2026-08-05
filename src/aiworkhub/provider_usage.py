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
            try:
                roots.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    samples: list[dict[str, Any]] = []
    sample_count = 0

    def consume_usage(
        usage: dict[str, Any],
        *,
        event_type: str,
        cost_sources: tuple[dict[str, Any], ...],
    ) -> None:
        nonlocal sample_count
        details = usage.get("input_tokens_details") or usage.get(
            "prompt_tokens_details"
        )
        if not isinstance(details, dict):
            details = {}
        recognized_keys = {
            "input_tokens",
            "prompt_tokens",
            "input",
            "output_tokens",
            "completion_tokens",
            "output",
            "reasoning_output_tokens",
            "cache_read_input_tokens",
            "cached_input_tokens",
            "prompt_cache_hit_tokens",
            "cache_creation_input_tokens",
            "cache_write_input_tokens",
        }
        if not any(key in usage for key in recognized_keys) and "cached_tokens" not in details:
            return

        input_tokens = _as_int(
            usage.get("input_tokens")
            or usage.get("prompt_tokens")
            or usage.get("input")
        )
        output_tokens = _as_int(
            usage.get("output_tokens")
            or usage.get("completion_tokens")
            or usage.get("output")
        )
        reasoning_output_tokens = _as_int(usage.get("reasoning_output_tokens"))
        cached_input_tokens = _as_int(
            usage.get("cache_read_input_tokens")
            or usage.get("cached_input_tokens")
            or usage.get("prompt_cache_hit_tokens")
            or details.get("cached_tokens")
        )
        cache_write_input_tokens = _as_int(usage.get("cache_write_input_tokens"))
        cache_creation_input_tokens = _as_int(
            usage.get("cache_creation_input_tokens")
            or cache_write_input_tokens
        )
        cache_observed = any(
            key in usage
            for key in (
                "cache_read_input_tokens",
                "cached_input_tokens",
                "prompt_cache_hit_tokens",
                "cache_creation_input_tokens",
                "cache_write_input_tokens",
            )
        ) or "cached_tokens" in details

        result["usage_observed"] = True
        result["cache_metrics_observed"] = bool(
            result["cache_metrics_observed"] or cache_observed
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
            result[key] = max(int(result[key]), value)

        for source in (*cost_sources, usage):
            for key in ("total_cost_usd", "cost_usd"):
                if key in source and source.get(key) is not None:
                    result["cost_observed"] = True
                    result["cost_usd"] = max(
                        float(result["cost_usd"] or 0.0),
                        _as_float(source.get(key)),
                    )
                    break

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

    def walk(value: Any, *, root: dict[str, Any], event_type: str, depth: int = 0) -> None:
        if depth > MAX_WALK_DEPTH:
            return
        if isinstance(value, dict):
            local_type = str(value.get("type") or event_type or "")
            for key in ("model", "model_id"):
                observed_model = _model_identity(value.get(key))
                if observed_model and not result["model_observed"]:
                    result["model_observed"] = True
                    result["observed_model"] = observed_model
            usage = value.get("usage")
            if isinstance(usage, dict):
                consume_usage(usage, event_type=local_type, cost_sources=(root, value))
            for alternate in ("token_usage", "tokens"):
                nested_usage = value.get(alternate)
                if isinstance(nested_usage, dict):
                    consume_usage(
                        nested_usage,
                        event_type=local_type,
                        cost_sources=(root, value),
                    )
            for key, nested in list(value.items())[:MAX_WALK_ITEMS]:
                if key not in {"usage", "token_usage", "tokens"}:
                    walk(nested, root=root, event_type=local_type, depth=depth + 1)
        elif isinstance(value, list):
            for nested in value[:MAX_WALK_ITEMS]:
                walk(nested, root=root, event_type=event_type, depth=depth + 1)

    for candidate in roots[:MAX_JSON_EVENTS]:
        if isinstance(candidate, dict):
            walk(
                candidate,
                root=candidate,
                event_type=str(candidate.get("type") or ""),
            )
        elif isinstance(candidate, list):
            synthetic_root: dict[str, Any] = {}
            walk(candidate, root=synthetic_root, event_type="")

    result["usage_sample_count"] = sample_count
    result["usage_samples"] = samples
    result["usage_samples_truncated"] = sample_count > len(samples)
    return result


def cumulative_total_tokens(summary: dict[str, Any], adapter_id: str) -> int | None:
    """Return the authoritative cumulative total for live/post-hoc caps."""

    if not summary.get("usage_observed"):
        return None
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
