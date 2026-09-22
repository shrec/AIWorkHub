from __future__ import annotations

import json
from pathlib import Path

from aiworkhub import claude_code_usage


def _usage_line(
    message_id: str,
    *,
    input_tokens: int = 100,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    output_tokens: int = 10,
    model: str = "claude-sonnet-5",
    timestamp: str = "2026-09-22T00:00:00.000Z",
    text: str = "Sure, here is the answer you asked for.",
) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": timestamp,
        "sessionId": "session-under-test",
        "message": {
            "id": message_id,
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read_input_tokens,
                "cache_creation_input_tokens": cache_creation_input_tokens,
                "output_tokens": output_tokens,
            },
        },
    })


def _setup_project(monkeypatch, tmp_path, repo_root, *, dirname: str | None = None) -> Path:
    config_dir = tmp_path / "claude_home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    slug = claude_code_usage._slugify(repo_root)
    project_dir = config_dir / "projects" / (dirname if dirname is not None else slug)
    project_dir.mkdir(parents=True)
    return project_dir


def test_missing_project_dir_is_not_found(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_home_empty"))

    result = claude_code_usage.collect_claude_code_usage(repo_root)

    assert result["status"] == "not_found"
    assert result["project_dirs"] == []
    assert result["sessions"] == []
    assert result["total_count"] == 0
    assert result["returned_count"] == 0
    assert result["truncated"] is False
    assert result["totals"]["sessions"] == 0


def test_duplicate_message_id_is_counted_once(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)

    lines = [
        _usage_line("msg_1", input_tokens=10, output_tokens=1),
        _usage_line("msg_1", input_tokens=10, output_tokens=5),  # streamed rewrite, same id
        _usage_line("msg_2", input_tokens=20, output_tokens=2),
    ]
    (project_dir / "session-a.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = claude_code_usage.collect_claude_code_usage(repo_root)

    assert result["status"] == "measured"
    row = result["sessions"][0]
    assert row["main_calls"] == 2
    assert row["output_tokens"] == 5 + 2
    assert row["input_tokens"] == 10 + 20
    assert result["totals"]["main"]["api_calls"] == 2


def test_subagent_files_are_counted_and_split(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)

    (project_dir / "session-a.jsonl").write_text(
        _usage_line("main_1", input_tokens=100, output_tokens=10) + "\n", encoding="utf-8"
    )
    subagents_dir = project_dir / "session-a" / "subagents"
    subagents_dir.mkdir(parents=True)
    (subagents_dir / "sub-1.jsonl").write_text(
        _usage_line("sub_1", input_tokens=30, output_tokens=3) + "\n", encoding="utf-8"
    )
    (subagents_dir / "sub-2.jsonl").write_text(
        _usage_line("sub_2", input_tokens=40, output_tokens=4) + "\n", encoding="utf-8"
    )

    result = claude_code_usage.collect_claude_code_usage(repo_root)

    assert result["totals"]["subagent_transcripts"] == 2
    assert result["totals"]["sessions"] == 1
    assert result["totals"]["main"]["api_calls"] == 1
    assert result["totals"]["subagent"]["api_calls"] == 2
    assert result["totals"]["subagent"]["input_tokens"] == 70
    row = result["sessions"][0]
    assert row["main_calls"] == 1
    assert row["subagent_calls"] == 2
    assert row["input_tokens"] == 170


def test_malformed_line_is_skipped_and_counted(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)

    content = "\n".join([
        _usage_line("msg_1", input_tokens=5, output_tokens=1),
        "{not valid json, truncated mid-write",
        _usage_line("msg_2", input_tokens=7, output_tokens=2),
    ]) + "\n"
    (project_dir / "session-a.jsonl").write_text(content, encoding="utf-8")

    result = claude_code_usage.collect_claude_code_usage(repo_root)

    assert result["totals"]["main"]["malformed_lines"] == 1
    assert result["totals"]["main"]["api_calls"] == 2


def test_case_insensitive_match_without_prefix_match(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    slug = claude_code_usage._slugify(repo_root)

    config_dir = tmp_path / "claude_home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    projects_dir = config_dir / "projects"
    projects_dir.mkdir(parents=True)

    upper_dir = projects_dir / slug.upper()
    upper_dir.mkdir()
    (upper_dir / "session-a.jsonl").write_text(
        _usage_line("msg_1", input_tokens=1, output_tokens=1) + "\n", encoding="utf-8"
    )

    # Must never prefix-match: a longer sibling name is a different project.
    prefix_dir = projects_dir / f"{slug}-EXTRA"
    prefix_dir.mkdir()
    (prefix_dir / "session-b.jsonl").write_text(
        _usage_line("msg_2", input_tokens=999, output_tokens=999) + "\n", encoding="utf-8"
    )

    result = claude_code_usage.collect_claude_code_usage(repo_root)

    assert result["status"] == "measured"
    assert result["project_dirs"] == [str(upper_dir)]
    assert result["total_count"] == 1
    assert result["sessions"][0]["input_tokens"] == 1


def test_unchanged_file_is_not_reparsed(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)
    (project_dir / "session-a.jsonl").write_text(
        _usage_line("msg_1", input_tokens=1, output_tokens=1) + "\n", encoding="utf-8"
    )

    calls = 0
    real_parser = claude_code_usage._parse_transcript_file

    def _counting_parser(path):
        nonlocal calls
        calls += 1
        return real_parser(path)

    monkeypatch.setattr(claude_code_usage, "_parse_transcript_file", _counting_parser)

    claude_code_usage.collect_claude_code_usage(repo_root)
    claude_code_usage.collect_claude_code_usage(repo_root)

    assert calls == 1


def test_changed_file_is_reparsed(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)
    transcript = project_dir / "session-a.jsonl"
    transcript.write_text(
        _usage_line("msg_1", input_tokens=1, output_tokens=1) + "\n", encoding="utf-8"
    )

    first = claude_code_usage.collect_claude_code_usage(repo_root)
    assert first["sessions"][0]["input_tokens"] == 1

    transcript.write_text(
        _usage_line("msg_1", input_tokens=1, output_tokens=1) + "\n"
        + _usage_line("msg_2", input_tokens=2, output_tokens=2) + "\n",
        encoding="utf-8",
    )

    second = claude_code_usage.collect_claude_code_usage(repo_root)
    assert second["sessions"][0]["input_tokens"] == 3


def test_message_content_never_appears_in_output(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)

    secret_marker = "SECRET-MESSAGE-CONTENT-MUST-NOT-LEAK-93a7f1"
    (project_dir / "session-a.jsonl").write_text(
        _usage_line("msg_1", input_tokens=1, output_tokens=1, text=secret_marker) + "\n",
        encoding="utf-8",
    )

    result = claude_code_usage.collect_claude_code_usage(repo_root)

    assert secret_marker not in json.dumps(result)


def test_sessions_are_ranked_by_context_and_bounded_to_ten(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)

    for index in range(12):
        tokens = (index + 1) * 10
        (project_dir / f"session-{index:02d}.jsonl").write_text(
            _usage_line(f"msg_{index}", input_tokens=tokens, output_tokens=1) + "\n",
            encoding="utf-8",
        )

    result = claude_code_usage.collect_claude_code_usage(repo_root)

    assert result["total_count"] == 12
    assert result["returned_count"] == 10
    assert result["truncated"] is True
    assert len(result["sessions"]) == 10
    returned_tokens = [row["context_tokens"] for row in result["sessions"]]
    assert returned_tokens == sorted(returned_tokens, reverse=True)
    assert result["sessions"][0]["session_id"] == "session-11"


def test_synthetic_model_entries_are_skipped(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)

    lines = [
        _usage_line("msg_1", input_tokens=10, output_tokens=1),
        _usage_line(
            "msg_synthetic", input_tokens=999, output_tokens=999, model="<synthetic>"
        ),
    ]
    (project_dir / "session-a.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = claude_code_usage.collect_claude_code_usage(repo_root)

    assert result["totals"]["main"]["api_calls"] == 1
    assert result["totals"]["main"]["input_tokens"] == 10
    assert result["sessions"][0]["models"] == ["claude-sonnet-5"]


def test_growing_file_keeps_exactly_one_cache_entry(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)
    transcript = project_dir / "session-a.jsonl"
    key = str(transcript)

    for count in range(1, 6):
        transcript.write_text(
            "\n".join(
                _usage_line(f"msg_{i}", input_tokens=1, output_tokens=1)
                for i in range(count)
            )
            + "\n",
            encoding="utf-8",
        )
        claude_code_usage.collect_claude_code_usage(repo_root)

    assert list(claude_code_usage._FILE_CACHE).count(key) == 1
    assert claude_code_usage._FILE_CACHE[key].usage.api_calls == 5
