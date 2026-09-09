from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiworkhub import cost_ledger, provider_usage


def _write_jsonl(path: Path, events: list[object]) -> Path:
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    return path


def _kilo_step_finish(
    *,
    input_tokens: int,
    output_tokens: int,
    reasoning: int,
    cache_read: int,
    cache_write: int,
    cost: float | None,
) -> dict[str, object]:
    part: dict[str, object] = {
        "id": "prt_sanitized",
        "sessionID": "ses_sanitized",
        "messageID": "msg_sanitized",
        "type": "step-finish",
        "reason": "stop",
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
            "reasoning": reasoning,
            "cache": {"read": cache_read, "write": cache_write},
        },
        "metrics": {"generation": "grok", "source": "xai"},
    }
    if cost is not None:
        part["cost"] = cost
    return {
        "type": "step-finish",
        "timestamp": 1757400000000,
        "sessionID": "ses_sanitized",
        "part": part,
    }


def test_kilo_step_finish_normalizes_tokens_cache_and_cost(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "kilo.stdout.log",
        [
            _kilo_step_finish(
                input_tokens=139148,
                output_tokens=842,
                reasoning=210,
                cache_read=128000,
                cache_write=416,
                cost=0.1372,
            )
        ],
    )
    usage = provider_usage.read_provider_usage(path)
    assert usage["usage_observed"] is True
    assert usage["input_tokens"] == 139148
    assert usage["output_tokens"] == 842
    assert usage["reasoning_output_tokens"] == 210
    assert usage["cached_input_tokens"] == 128000
    assert usage["cache_write_input_tokens"] == 416
    assert usage["cache_creation_input_tokens"] == 416
    assert usage["cache_metrics_observed"] is True
    assert usage["cost_observed"] is True
    assert usage["cost_usd"] == pytest.approx(0.1372)


def test_kilo_repeated_step_finish_snapshots_use_max_not_sum(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "kilo-cumulative.stdout.log",
        [
            _kilo_step_finish(
                input_tokens=100000,
                output_tokens=400,
                reasoning=80,
                cache_read=90000,
                cache_write=200,
                cost=0.10,
            ),
            _kilo_step_finish(
                input_tokens=139148,
                output_tokens=842,
                reasoning=210,
                cache_read=128000,
                cache_write=416,
                cost=0.1372,
            ),
        ],
    )
    usage = provider_usage.read_provider_usage(path)
    assert usage["input_tokens"] == 139148
    assert usage["output_tokens"] == 842
    assert usage["reasoning_output_tokens"] == 210
    assert usage["cached_input_tokens"] == 128000
    assert usage["cache_write_input_tokens"] == 416
    assert usage["cost_usd"] == pytest.approx(0.2372)
    assert usage["usage_sample_count"] == 2


def test_kilo_decreasing_direct_costs_are_summed(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "kilo-decreasing.stdout.log",
        [
            _kilo_step_finish(
                input_tokens=139148,
                output_tokens=400,
                reasoning=210,
                cache_read=90000,
                cache_write=416,
                cost=0.175874,
            ),
            _kilo_step_finish(
                input_tokens=100000,
                output_tokens=842,
                reasoning=80,
                cache_read=128000,
                cache_write=200,
                cost=0.062028,
            ),
        ],
    )
    usage = provider_usage.read_provider_usage(path)
    assert usage["input_tokens"] == 139148
    assert usage["output_tokens"] == 842
    assert usage["reasoning_output_tokens"] == 210
    assert usage["cached_input_tokens"] == 128000
    assert usage["cache_write_input_tokens"] == 416
    assert usage["cost_observed"] is True
    assert usage["cost_usd"] == pytest.approx(0.237902)
    assert usage["usage_sample_count"] == 2


def test_kilo_duplicate_usage_and_tokens_do_not_double_direct_cost(
    tmp_path: Path,
) -> None:
    event = _kilo_step_finish(
        input_tokens=10,
        output_tokens=2,
        reasoning=1,
        cache_read=4,
        cache_write=0,
        cost=0.175874,
    )
    part = event["part"]
    assert isinstance(part, dict)
    tokens = part["tokens"]
    assert isinstance(tokens, dict)
    part["usage"] = dict(tokens)
    usage = provider_usage.read_provider_usage(
        _write_jsonl(tmp_path / "kilo-dup-cost.stdout.log", [event])
    )
    assert usage["cost_observed"] is True
    assert usage["cost_usd"] == pytest.approx(0.175874)
    assert usage["input_tokens"] == 10


def test_kilo_absent_cost_stays_unknown_and_observed_zero_is_distinct(
    tmp_path: Path,
) -> None:
    absent = provider_usage.read_provider_usage(
        _write_jsonl(
            tmp_path / "kilo-absent-cost.stdout.log",
            [
                _kilo_step_finish(
                    input_tokens=10,
                    output_tokens=2,
                    reasoning=1,
                    cache_read=4,
                    cache_write=0,
                    cost=None,
                )
            ],
        )
    )
    assert absent["usage_observed"] is True
    assert absent["cost_observed"] is False
    assert absent["cost_usd"] is None

    zero = provider_usage.read_provider_usage(
        _write_jsonl(
            tmp_path / "kilo-zero-cost.stdout.log",
            [
                _kilo_step_finish(
                    input_tokens=10,
                    output_tokens=2,
                    reasoning=1,
                    cache_read=4,
                    cache_write=0,
                    cost=0.0,
                )
            ],
        )
    )
    assert zero["cost_observed"] is True
    assert zero["cost_usd"] == pytest.approx(0.0)


def test_nested_cache_outside_usage_containers_is_not_usage(tmp_path: Path) -> None:
    usage = provider_usage.read_provider_usage(
        _write_jsonl(
            tmp_path / "unrelated.stdout.log",
            [
                {
                    "type": "tool-end",
                    "duration_ms": 139148,
                    "bytes": 4096,
                    "cache": {"read": 999, "write": 8},
                    "metrics": {"cost": 9.9},
                    "part": {"count": 7, "cache": {"read": 50, "write": 3}},
                }
            ],
        )
    )
    assert usage["usage_observed"] is False
    assert usage["cache_metrics_observed"] is False
    assert usage["cached_input_tokens"] == 0
    assert usage["cost_observed"] is False
    assert usage["cost_usd"] is None


def test_claude_codex_and_openai_usage_shapes_remain(tmp_path: Path) -> None:
    claude = provider_usage.read_provider_usage(
        _write_jsonl(
            tmp_path / "claude.stdout.log",
            [
                {
                    "type": "result",
                    "total_cost_usd": 0.42,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_read_input_tokens": 20,
                        "cache_creation_input_tokens": 5,
                    },
                }
            ],
        )
    )
    assert claude["input_tokens"] == 100
    assert claude["cached_input_tokens"] == 20
    assert claude["cache_creation_input_tokens"] == 5
    assert claude["cache_metrics_observed"] is True
    assert claude["cost_observed"] is True
    assert claude["cost_usd"] == pytest.approx(0.42)

    codex = provider_usage.read_provider_usage(
        _write_jsonl(
            tmp_path / "codex.stdout.log",
            [
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 129189,
                        "cached_input_tokens": 111232,
                        "cache_write_input_tokens": 12,
                        "output_tokens": 1113,
                        "reasoning_output_tokens": 285,
                    },
                }
            ],
        )
    )
    assert codex["reasoning_output_tokens"] == 285
    assert codex["cache_write_input_tokens"] == 12
    assert codex["cache_creation_input_tokens"] == 12
    assert codex["cache_metrics_observed"] is True
    assert codex["cost_observed"] is False
    assert codex["cost_usd"] is None

    openai = provider_usage.read_provider_usage(
        _write_jsonl(
            tmp_path / "openai.stdout.log",
            [
                {
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 4,
                        "prompt_tokens_details": {"cached_tokens": 3},
                    }
                }
            ],
        )
    )
    assert openai["input_tokens"] == 10
    assert openai["output_tokens"] == 4
    assert openai["cached_input_tokens"] == 3
    assert openai["cache_metrics_observed"] is True
    assert openai["cost_observed"] is False


def test_focused_cost_ledger_projection_observes_kilo_cache_and_cost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parsed = provider_usage.read_provider_usage(
        _write_jsonl(
            tmp_path / "kilo.stdout.log",
            [
                _kilo_step_finish(
                    input_tokens=139148,
                    output_tokens=842,
                    reasoning=210,
                    cache_read=128000,
                    cache_write=416,
                    cost=0.1372,
                )
            ],
        )
    )
    events = [
        {
            "task_id": "T-kilo",
            "runner": "grok_kilo_cli",
            "topic": "code",
            "model": "grok-4.6",
            "provider": "xai",
            "role": "worker",
            "source": "task_mcp_launcher",
            "note": "task_mcp_request:9651be6066db458cb0f8226ad5e69c39",
            "input_tokens": parsed["input_tokens"],
            "output_tokens": parsed["output_tokens"],
            "reasoning_output_tokens": parsed["reasoning_output_tokens"],
            "total_tokens": (
                int(parsed["input_tokens"])
                + int(parsed["output_tokens"])
                + int(parsed["reasoning_output_tokens"])
            ),
            "cached_input_tokens": parsed["cached_input_tokens"],
            "cache_creation_input_tokens": parsed["cache_creation_input_tokens"],
            "cache_write_input_tokens": parsed["cache_write_input_tokens"],
            "cache_metrics_observed": parsed["cache_metrics_observed"],
            "cost_usd": parsed["cost_usd"],
            "cost_observed": parsed["cost_observed"],
            "usage_observed": parsed["usage_observed"],
            "created_at": "2026-09-09T11:00:00+00:00",
        }
    ]
    monkeypatch.setattr(
        cost_ledger.task_store,
        "list_usage_events",
        lambda _root, limit=10_000: events,
    )
    monkeypatch.setattr(
        cost_ledger.task_store,
        "latest_manager_decisions",
        lambda _root: {},
    )
    result = cost_ledger.build_cost_ledger(repo_root=tmp_path, include_tasks=True)
    row = result["tasks"][0]
    assert row["cache_metrics_observed"] is True
    assert row["cost_observed"] is True
    assert row["cached_input_tokens"] == 128000
    assert row["cost_usd"] == pytest.approx(0.1372)
    encoded = json.dumps(result).lower()
    assert "not causal savings" in encoded
    assert "savings_usd" not in encoded
    assert result["retry_economics"]["association_only"] is True
