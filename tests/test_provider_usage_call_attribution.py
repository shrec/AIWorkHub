from __future__ import annotations

import json
from pathlib import Path

from aiworkhub import provider_usage


def _write_jsonl(path: Path, events: list[object]) -> Path:
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    return path


def _usage_event(input_tokens: int, output_tokens: int, cache_read: int) -> dict:
    return {
        "type": "assistant",
        "message": {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": 0,
            }
        },
    }


_A = (100, 10, 0)
_B = (200, 20, 50)


def test_consecutive_identical_usage_snapshots_are_deduped(tmp_path: Path) -> None:
    repeated = provider_usage.read_provider_usage(
        _write_jsonl(
            tmp_path / "repeated.jsonl",
            [
                _usage_event(*_A),
                _usage_event(*_A),
                _usage_event(*_A),
                _usage_event(*_A),
                _usage_event(*_B),
            ],
        )
    )
    distinct = provider_usage.read_provider_usage(
        _write_jsonl(
            tmp_path / "distinct.jsonl",
            [_usage_event(*_A), _usage_event(*_B)],
        )
    )
    interleaved = provider_usage.read_provider_usage(
        _write_jsonl(
            tmp_path / "interleaved.jsonl",
            [_usage_event(*_A), _usage_event(*_B), _usage_event(*_A)],
        )
    )

    assert repeated["usage_sample_count"] == 5
    assert repeated["usage_samples_deduped"] == 3
    assert len(repeated["usage_samples"]) == 2
    assert distinct["usage_samples_deduped"] == 0
    assert len(distinct["usage_samples"]) == 2
    assert interleaved["usage_samples_deduped"] == 0
    assert len(interleaved["usage_samples"]) == 3
    for key in (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
    ):
        assert repeated[key] == distinct[key]
    assert repeated["input_tokens"] == 200
    assert repeated["output_tokens"] == 20
    assert repeated["cached_input_tokens"] == 50


def _tool_use(tool_id: str, name: str) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {}}


def _tool_result(tool_use_id: str, content: object) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}


def test_tool_result_bytes_are_attributed_by_tool_name(tmp_path: Path) -> None:
    bash_content = [{"type": "text", "text": "ok"}]
    path = _write_jsonl(
        tmp_path / "tools.jsonl",
        [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        _tool_use("t1", "mcp__x__read"),
                        _tool_use("t2", "Bash"),
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        _tool_result("t1", "abcd"),
                        _tool_result("t2", bash_content),
                        _tool_result("missing", "xyz"),
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {"content": [_tool_use("t3", "mcp__x__read")]},
            },
            {
                "type": "user",
                "message": {"content": [_tool_result("t3", "é" * 3)]},
            },
        ],
    )
    stats = provider_usage.read_provider_usage(path)["tool_result_bytes_by_tool"]

    assert stats == {
        "mcp__x__read": {"calls": 2, "bytes": 4 + 6},
        "Bash": {
            "calls": 1,
            "bytes": len(json.dumps(bash_content, ensure_ascii=False).encode("utf-8")),
        },
        "unknown": {"calls": 1, "bytes": 3},
    }


def test_tool_names_beyond_the_cap_fold_into_other(tmp_path: Path) -> None:
    uses = [_tool_use(f"t{i}", f"tool_{i:02d}") for i in range(65)]
    results = [_tool_result(f"t{i}", "x") for i in range(65)]
    path = _write_jsonl(
        tmp_path / "many-tools.jsonl",
        [
            {"type": "assistant", "message": {"content": uses}},
            {"type": "user", "message": {"content": results}},
        ],
    )
    stats = provider_usage.read_provider_usage(path)["tool_result_bytes_by_tool"]

    assert len(stats) == 65
    assert "tool_63" in stats
    assert "tool_64" not in stats
    assert stats["other"] == {"calls": 1, "bytes": 1}


def test_claude_init_event_is_summarized(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "init.jsonl",
        [
            {
                "type": "system",
                "subtype": "init",
                "tools": ["Bash", "Read"],
                "mcp_servers": [{"name": "x"}],
                "slash_commands": [],
                "agents": ["planner"],
                "skills": [],
                "plugins": [" alpha ", {"name": "beta"}, "  ", {"id": "unnamed"}],
            }
        ],
    )
    summary = provider_usage.read_provider_usage(path)["init_summary"]

    assert summary == {
        "tools": 2,
        "mcp_servers": 1,
        "slash_commands": 0,
        "agents": 1,
        "skills": 0,
        "plugins": ["alpha", "beta"],
    }


def test_init_plugin_names_are_capped_at_32(tmp_path: Path) -> None:
    plugins = [f"plugin-{i}" for i in range(40)]
    path = _write_jsonl(
        tmp_path / "init-plugins.jsonl",
        [{"type": "system", "subtype": "init", "plugins": plugins}],
    )
    names = provider_usage.read_provider_usage(path)["init_summary"]["plugins"]

    assert len(names) == 32
    assert names[0] == "plugin-0"
    assert names[-1] == "plugin-31"
