from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiworkhub import provider_usage


_SECRET = "sk-SANITIZED_NOT_A_REAL_SECRET"
_REASONING = "private chain-of-thought must not be retained"


def _write_jsonl(path: Path, events: list[object]) -> Path:
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    return path


def _opencode_text(*, session_id: str = "ses_canary") -> dict[str, object]:
    return {
        "type": "text",
        "timestamp": 1757400000000,
        "sessionID": session_id,
        "part": {
            "id": "prt_text",
            "sessionID": session_id,
            "messageID": "msg_canary",
            "type": "text",
            "text": "sanitized assistant text",
            "tokens": {
                "total": 999999,
                "input": 888888,
                "output": 777777,
                "reasoning": 1,
                "cache": {"read": 9, "write": 8},
            },
        },
    }


def _opencode_step_finish(
    *,
    total: int,
    input_tokens: int,
    output_tokens: int,
    reasoning: int,
    cache_read: int,
    cache_write: int,
    cost: float | None,
    session_id: str = "ses_canary",
    part_id: str = "prt_finish",
    parent_id: str | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "type": "step_finish",
        "timestamp": 1757400000001,
        "sessionID": session_id,
        "id": part_id,
        "messageID": "msg_canary",
        "reason": "stop",
        "tokens": {
            "total": total,
            "input": input_tokens,
            "output": output_tokens,
            "reasoning": reasoning,
            "cache": {"write": cache_write, "read": cache_read},
        },
    }
    if cost is not None:
        event["cost"] = cost
    if parent_id is not None:
        event["parentID"] = parent_id
    return event


def _canary_step_finish() -> dict[str, object]:
    return _opencode_step_finish(
        total=4368,
        input_tokens=3072,
        output_tokens=800,
        reasoning=224,
        cache_read=1024,
        cache_write=64,
        cost=0,
    )


def test_opencode_canary_normalizes_tokens_cache_and_zero_cost(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "opencode-canary.stdout.log",
        [_opencode_text(), _canary_step_finish()],
    )
    usage = provider_usage.read_provider_usage(path)
    assert usage["usage_observed"] is True
    assert usage["total_tokens_observed"] is True
    assert usage["total_tokens"] == 4368
    assert usage["input_tokens"] == 3072
    assert usage["output_tokens"] == 800
    assert usage["reasoning_output_tokens"] == 224
    assert usage["cached_input_tokens"] == 1024
    assert usage["cache_write_input_tokens"] == 64
    assert usage["cache_creation_input_tokens"] == 64
    assert usage["cache_metrics_observed"] is True
    assert usage["cost_observed"] is True
    assert usage["cost_usd"] == pytest.approx(0.0)
    dumped = json.dumps(usage)
    assert _SECRET not in dumped
    assert _REASONING not in dumped
    assert "sanitized assistant text" not in dumped


def test_opencode_text_nested_numbers_are_not_usage(tmp_path: Path) -> None:
    usage = provider_usage.read_provider_usage(
        _write_jsonl(tmp_path / "opencode-text-only.stdout.log", [_opencode_text()])
    )
    assert usage["usage_observed"] is False
    assert usage["total_tokens_observed"] is False
    assert usage["input_tokens"] == 0
    assert usage["cost_observed"] is False
    assert usage["cost_usd"] is None


def test_opencode_reasoning_and_secrets_are_not_emitted(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "opencode-reasoning.stdout.log",
        [
            {
                "type": "reasoning",
                "sessionID": "ses_canary",
                "text": _REASONING,
                "tokens": {
                    "total": 50,
                    "input": 40,
                    "output": 10,
                    "reasoning": 50,
                    "cache": {"read": 0, "write": 0},
                },
                "cost": 1.25,
                "api_key": _SECRET,
            },
            _canary_step_finish(),
        ],
    )
    usage = provider_usage.read_provider_usage(path)
    dumped = json.dumps(usage)
    assert _REASONING not in dumped
    assert _SECRET not in dumped
    assert usage["total_tokens"] == 4368
    assert usage["cost_usd"] == pytest.approx(0.0)


def test_opencode_sse_replay_and_export_do_not_double_count(tmp_path: Path) -> None:
    step = _canary_step_finish()
    export = {
        "info": {"id": "ses_canary", "time": {"completed": 1757400000500}},
        "messages": [
            {
                "id": "msg_canary",
                "sessionID": "ses_canary",
                "parts": [dict(step)],
            }
        ],
    }
    path = tmp_path / "opencode-replay.stdout.log"
    path.write_text(
        "data: "
        + json.dumps(_opencode_text())
        + "\n"
        + "data: "
        + json.dumps(step)
        + "\n"
        + "data: "
        + json.dumps(step)
        + "\n"
        + json.dumps(export)
        + "\n",
        encoding="utf-8",
    )
    usage = provider_usage.read_provider_usage(path)
    assert usage["input_tokens"] == 3072
    assert usage["total_tokens"] == 4368
    assert usage["cost_usd"] == pytest.approx(0.0)
    assert usage["usage_sample_count"] == 1


def test_opencode_absent_cost_stays_unknown_and_zero_is_observed(
    tmp_path: Path,
) -> None:
    absent = provider_usage.read_provider_usage(
        _write_jsonl(
            tmp_path / "opencode-absent-cost.stdout.log",
            [
                _opencode_step_finish(
                    total=10,
                    input_tokens=8,
                    output_tokens=2,
                    reasoning=0,
                    cache_read=0,
                    cache_write=0,
                    cost=None,
                )
            ],
        )
    )
    assert absent["usage_observed"] is True
    assert absent["total_tokens"] == 10
    assert absent["cost_observed"] is False
    assert absent["cost_usd"] is None

    zero = provider_usage.read_provider_usage(
        _write_jsonl(
            tmp_path / "opencode-zero-cost.stdout.log",
            [
                _opencode_step_finish(
                    total=10,
                    input_tokens=8,
                    output_tokens=2,
                    reasoning=0,
                    cache_read=0,
                    cache_write=0,
                    cost=0.0,
                )
            ],
        )
    )
    assert zero["cost_observed"] is True
    assert zero["cost_usd"] == pytest.approx(0.0)
    assert zero["cached_input_tokens"] == 0
    assert zero["cache_metrics_observed"] is True


def test_opencode_malformed_truncated_and_oversized_fail_closed(
    tmp_path: Path,
) -> None:
    malformed = tmp_path / "opencode-malformed.stdout.log"
    malformed.write_text(
        '{"type":"step_finish","tokens":{"total":12,"input":8\n'
        + json.dumps(_canary_step_finish())[12:]
        + "\n",
        encoding="utf-8",
    )
    broken = provider_usage.read_provider_usage(malformed)
    assert broken["usage_observed"] is False
    assert broken["cost_observed"] is False
    assert broken["cost_usd"] is None

    oversized = _write_jsonl(
        tmp_path / "opencode-oversized.stdout.log",
        [_canary_step_finish()],
    )
    limited = provider_usage.read_provider_usage(oversized, max_bytes=8)
    assert limited["usage_observed"] is False
    assert limited["cost_observed"] is False


def test_opencode_child_session_and_token_deltas_fail_closed(tmp_path: Path) -> None:
    usage = provider_usage.read_provider_usage(
        _write_jsonl(
            tmp_path / "opencode-child-delta.stdout.log",
            [
                _opencode_step_finish(
                    total=4368,
                    input_tokens=3072,
                    output_tokens=800,
                    reasoning=224,
                    cache_read=1024,
                    cache_write=64,
                    cost=0,
                    session_id="ses_child",
                    part_id="prt_child",
                    parent_id="ses_canary",
                ),
                {
                    "type": "token.delta",
                    "sessionID": "ses_canary",
                    "tokens": {
                        "total": 99,
                        "input": 90,
                        "output": 9,
                        "reasoning": 0,
                        "cache": {"read": 1, "write": 1},
                    },
                    "cost": 4.5,
                },
            ],
        )
    )
    assert usage["usage_observed"] is False
    assert usage["cost_observed"] is False
    assert usage["cost_usd"] is None
    assert usage["total_tokens"] == 0


def test_opencode_declared_total_is_authoritative(tmp_path: Path) -> None:
    usage = provider_usage.read_provider_usage(
        _write_jsonl(tmp_path / "opencode-total.stdout.log", [_canary_step_finish()])
    )
    assert provider_usage.cumulative_total_tokens(usage, "opencode") == 4368
    assert provider_usage.live_total_tokens(usage, "opencode") == 4368


def test_opencode_distinct_steps_sum_once_with_replay(tmp_path: Path) -> None:
    step_one = _opencode_step_finish(
        total=100,
        input_tokens=80,
        output_tokens=15,
        reasoning=5,
        cache_read=10,
        cache_write=2,
        cost=1.25,
        part_id="prt_step_a",
    )
    step_two = _opencode_step_finish(
        total=50,
        input_tokens=30,
        output_tokens=17,
        reasoning=3,
        cache_read=4,
        cache_write=1,
        cost=0.75,
        part_id="prt_step_b",
    )
    usage = provider_usage.read_provider_usage(
        _write_jsonl(
            tmp_path / "opencode-two-steps.stdout.log",
            [step_one, step_two, step_one, step_two],
        )
    )
    assert usage["usage_observed"] is True
    assert usage["total_tokens"] == 150
    assert usage["input_tokens"] == 110
    assert usage["output_tokens"] == 32
    assert usage["reasoning_output_tokens"] == 8
    assert usage["cached_input_tokens"] == 14
    assert usage["cache_write_input_tokens"] == 3
    assert usage["cache_creation_input_tokens"] == 3
    assert usage["cost_observed"] is True
    assert usage["cost_usd"] == pytest.approx(2.0)
    assert usage["usage_sample_count"] == 2


def test_opencode_info_parent_id_export_is_not_root_usage(tmp_path: Path) -> None:
    tokens = {
        "total": 10,
        "input": 8,
        "output": 2,
        "reasoning": 0,
        "cache": {"read": 0, "write": 0},
    }
    child_part = {
        "type": "step-finish",
        "sessionID": "ses_child",
        "messageID": "msg_child",
        "cost": 1.5,
        "tokens": tokens,
    }
    child_export = {
        "info": {"id": "ses_child", "parentID": "ses_parent"},
        "messages": [{"sessionID": "ses_child", "parts": [child_part]}],
    }
    child_usage = provider_usage.read_provider_usage(
        _write_jsonl(
            tmp_path / "opencode-child-info-parent.stdout.log",
            [child_export],
        )
    )
    assert child_usage["usage_observed"] is False
    assert child_usage["cost_observed"] is False
    assert child_usage["cost_usd"] is None
    assert child_usage["total_tokens"] == 0
    assert child_usage["input_tokens"] == 0

    root_part = {
        "type": "step-finish",
        "sessionID": "ses_root",
        "messageID": "msg_root",
        "cost": 1.5,
        "tokens": tokens,
    }
    root_export = {
        "info": {"id": "ses_root"},
        "messages": [{"sessionID": "ses_root", "parts": [root_part]}],
    }
    root_usage = provider_usage.read_provider_usage(
        _write_jsonl(
            tmp_path / "opencode-root-info-export.stdout.log",
            [root_export],
        )
    )
    assert root_usage["usage_observed"] is True
    assert root_usage["cost_observed"] is True
    assert root_usage["total_tokens"] == 10
    assert root_usage["input_tokens"] == 8
    assert root_usage["output_tokens"] == 2
    assert root_usage["cost_usd"] == pytest.approx(1.5)
