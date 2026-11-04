from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aiworkhub import claude_code_usage


@pytest.fixture(autouse=True)
def _no_collect_memo(monkeypatch):
    # The section memo would hide every on-disk change made inside one test;
    # the memo has its own dedicated test below.
    monkeypatch.setattr(claude_code_usage, "_COLLECT_MEMO_TTL_SECONDS", 0.0)
    claude_code_usage._COLLECT_MEMO.clear()
    yield
    claude_code_usage._COLLECT_MEMO.clear()


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

    def _counting_parser(path, previous=None):
        nonlocal calls
        calls += 1
        return real_parser(path, previous)

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


def _parse_recorder(monkeypatch) -> list[Path]:
    parsed: list[Path] = []
    real_parser = claude_code_usage._parse_transcript_file

    def _recording_parser(path, previous=None):
        parsed.append(Path(path))
        return real_parser(path, previous)

    monkeypatch.setattr(claude_code_usage, "_parse_transcript_file", _recording_parser)
    return parsed


def _write_transcripts(project_dir: Path, count: int) -> None:
    for index in range(count):
        (project_dir / f"session-{index:04d}.jsonl").write_text(
            _usage_line(f"msg_{index}", input_tokens=index + 1, output_tokens=1) + "\n",
            encoding="utf-8",
        )


def _fresh_totals(repo_root: Path) -> dict:
    claude_code_usage._FILE_CACHE.clear()
    claude_code_usage._persistent_cache_path(repo_root).unlink(missing_ok=True)
    return claude_code_usage.collect_claude_code_usage(repo_root)


def test_scan_larger_than_cache_bound_is_not_reparsed_in_process(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)
    file_count = claude_code_usage._FILE_CACHE_MAX_ENTRIES + 50
    _write_transcripts(project_dir, file_count)
    claude_code_usage._FILE_CACHE.clear()
    parsed = _parse_recorder(monkeypatch)

    first = claude_code_usage.collect_claude_code_usage(repo_root)
    assert len(parsed) == file_count

    # Drop the persisted copy so only the in-process cache can avoid a parse.
    claude_code_usage._persistent_cache_path(repo_root).unlink()
    parsed.clear()
    second = claude_code_usage.collect_claude_code_usage(repo_root)

    assert parsed == []
    assert second == first


def test_persistent_cache_survives_in_process_cache_loss(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)
    _write_transcripts(project_dir, 5)
    sub_dir = project_dir / "session-0000" / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "sub.jsonl").write_text(_usage_line("sub_1") + "\n", encoding="utf-8")
    claude_code_usage._FILE_CACHE.clear()
    first = claude_code_usage.collect_claude_code_usage(repo_root)
    assert claude_code_usage._persistent_cache_path(repo_root).is_file()

    claude_code_usage._FILE_CACHE.clear()
    parsed = _parse_recorder(monkeypatch)
    second = claude_code_usage.collect_claude_code_usage(repo_root)

    assert parsed == []
    assert second == first
    assert _fresh_totals(repo_root) == second
    assert len(parsed) == 6


def test_appending_to_one_transcript_reparses_only_that_file(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)
    _write_transcripts(project_dir, 6)
    claude_code_usage._FILE_CACHE.clear()
    claude_code_usage.collect_claude_code_usage(repo_root)
    parsed = _parse_recorder(monkeypatch)

    target = project_dir / "session-0003.jsonl"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(_usage_line("msg_extra", input_tokens=500, output_tokens=1) + "\n")

    for clear_in_process in (False, True):
        if clear_in_process:
            claude_code_usage._FILE_CACHE.clear()
            with target.open("a", encoding="utf-8") as handle:
                handle.write(_usage_line("msg_extra_2", input_tokens=7, output_tokens=1) + "\n")
        parsed.clear()
        result = claude_code_usage.collect_claude_code_usage(repo_root)
        assert parsed == [target]
        parsed.clear()
        assert result == _fresh_totals(repo_root)


def test_corrupt_or_wrong_version_cache_falls_back_to_full_parse(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)
    _write_transcripts(project_dir, 4)
    expected = _fresh_totals(repo_root)
    cache_path = claude_code_usage._persistent_cache_path(repo_root)
    good = json.loads(cache_path.read_text(encoding="utf-8"))
    some_key = next(iter(good["entries"]))

    bad_entry = json.loads(json.dumps(good))
    bad_entry["entries"][some_key]["api_calls"] = "many"
    wrong_version = json.loads(json.dumps(good))
    wrong_version["version"] = claude_code_usage._CACHE_VERSION + 1
    for entry in wrong_version["entries"].values():
        entry["api_calls"] = 999

    variants = [
        b"{not json at all",
        b"",
        b"\xff\xfe\x00garbage",
        json.dumps([1, 2, 3]).encode(),
        json.dumps(wrong_version).encode(),
        json.dumps(bad_entry).encode(),
    ]
    parsed = _parse_recorder(monkeypatch)
    for payload in variants:
        cache_path.write_bytes(payload)
        claude_code_usage._FILE_CACHE.clear()
        parsed.clear()
        result = claude_code_usage.collect_claude_code_usage(repo_root)
        assert len(parsed) == 4
        assert result == expected
        assert json.loads(cache_path.read_text(encoding="utf-8")) == good

    cache_path.unlink()
    claude_code_usage._FILE_CACHE.clear()
    assert claude_code_usage.collect_claude_code_usage(repo_root) == expected


def test_unwritable_cache_location_never_fails_the_call(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)
    _write_transcripts(project_dir, 2)
    # A regular file where the cache directory belongs makes every write fail.
    (repo_root / ".aiworkhub").write_text("not a directory", encoding="utf-8")
    claude_code_usage._FILE_CACHE.clear()

    result = claude_code_usage.collect_claude_code_usage(repo_root)

    assert result["totals"]["main"]["api_calls"] == 2


def test_deleted_transcript_is_dropped_from_persisted_cache(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)
    _write_transcripts(project_dir, 3)
    claude_code_usage._FILE_CACHE.clear()
    claude_code_usage.collect_claude_code_usage(repo_root)
    cache_path = claude_code_usage._persistent_cache_path(repo_root)
    removed = project_dir / "session-0001.jsonl"
    assert str(removed) in json.loads(cache_path.read_text(encoding="utf-8"))["entries"]

    removed.unlink()
    result = claude_code_usage.collect_claude_code_usage(repo_root)

    entries = json.loads(cache_path.read_text(encoding="utf-8"))["entries"]
    assert str(removed) not in entries
    assert len(entries) == 2
    assert result["totals"]["sessions"] == 2


def test_unreadable_transcript_is_neither_cached_nor_persisted(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)
    _write_transcripts(project_dir, 3)
    locked = project_dir / "session-0001.jsonl"
    claude_code_usage._FILE_CACHE.clear()
    real_parser = claude_code_usage._parse_transcript_file

    def _locked_parser(path, previous=None):
        if Path(path) == locked:
            return None
        return real_parser(path, previous)

    monkeypatch.setattr(claude_code_usage, "_parse_transcript_file", _locked_parser)

    result = claude_code_usage.collect_claude_code_usage(repo_root)

    assert result["totals"]["sessions"] == 3
    assert result["totals"]["main"]["api_calls"] == 2
    cache_path = claude_code_usage._persistent_cache_path(repo_root)
    entries = json.loads(cache_path.read_text(encoding="utf-8"))["entries"]
    assert str(locked) not in entries
    assert len(entries) == 2
    assert str(locked) not in claude_code_usage._FILE_CACHE

    # Once the file is readable again it is parsed and counted.
    monkeypatch.setattr(claude_code_usage, "_parse_transcript_file", real_parser)
    assert claude_code_usage.collect_claude_code_usage(repo_root)["totals"]["main"]["api_calls"] == 3


def test_deeply_nested_cache_file_is_ignored_and_rebuilt(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)
    _write_transcripts(project_dir, 3)
    expected = _fresh_totals(repo_root)
    cache_path = claude_code_usage._persistent_cache_path(repo_root)
    good = json.loads(cache_path.read_text(encoding="utf-8"))

    cache_path.write_text("[" * 100000, encoding="utf-8")
    claude_code_usage._FILE_CACHE.clear()
    parsed = _parse_recorder(monkeypatch)
    result = claude_code_usage.collect_claude_code_usage(repo_root)

    assert len(parsed) == 3
    assert result == expected
    assert json.loads(cache_path.read_text(encoding="utf-8")) == good


def _line_counter(monkeypatch) -> list[bytes]:
    """Record every raw line handed to the per-line JSON parser."""

    seen: list[bytes] = []
    real_parse_line = claude_code_usage._parse_line

    def _counting_parse_line(raw_line, state):
        seen.append(bytes(raw_line))
        return real_parse_line(raw_line, state)

    monkeypatch.setattr(claude_code_usage, "_parse_line", _counting_parse_line)
    return seen


def _append(path: Path, text: str) -> None:
    with path.open("ab") as handle:
        handle.write(text.encode("utf-8"))


def test_appended_lines_are_tail_parsed_and_match_cold_parse(monkeypatch, tmp_path) -> None:
    """NF-2026-02056: a growing transcript is not re-parsed from byte 0."""

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)
    transcript = project_dir / "session-a.jsonl"
    transcript.write_text(
        "".join(_usage_line(f"msg_{i}", input_tokens=i + 1) + "\n" for i in range(200)),
        encoding="utf-8",
    )
    claude_code_usage._FILE_CACHE.clear()
    claude_code_usage.collect_claude_code_usage(repo_root)

    appended = "".join(
        _usage_line(f"new_{i}", input_tokens=1000 + i, cache_read_input_tokens=7) + "\n"
        for i in range(3)
    )
    _append(transcript, appended)
    seen = _line_counter(monkeypatch)
    tails_before = claude_code_usage.CACHE_STATS["tail_parses"]
    fulls_before = claude_code_usage.CACHE_STATS["full_parses"]
    bytes_before = claude_code_usage.CACHE_STATS["tail_bytes_read"]

    second = claude_code_usage.collect_claude_code_usage(repo_root)

    assert len(seen) == 3
    assert b"".join(seen) == appended.encode("utf-8")
    assert claude_code_usage.CACHE_STATS["tail_parses"] == tails_before + 1
    assert claude_code_usage.CACHE_STATS["full_parses"] == fulls_before
    assert claude_code_usage.CACHE_STATS["tail_bytes_read"] - bytes_before == len(
        appended.encode("utf-8")
    )
    assert second["totals"]["main"]["api_calls"] == 203
    seen.clear()
    assert second == _fresh_totals(repo_root)
    assert len(seen) == 203


def test_streamed_message_rewritten_across_tail_boundary_keeps_last_write(
    monkeypatch, tmp_path
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)
    transcript = project_dir / "session-a.jsonl"
    transcript.write_text(
        _usage_line("msg_1", input_tokens=10, output_tokens=1) + "\n"
        + _usage_line("msg_2", input_tokens=20, output_tokens=2) + "\n",
        encoding="utf-8",
    )
    claude_code_usage._FILE_CACHE.clear()
    first = claude_code_usage.collect_claude_code_usage(repo_root)
    assert first["totals"]["main"]["output_tokens"] == 3

    _append(
        transcript,
        _usage_line("msg_1", input_tokens=10, output_tokens=50) + "\n"
        + _usage_line("msg_3", input_tokens=30, output_tokens=3) + "\n",
    )
    seen = _line_counter(monkeypatch)
    second = claude_code_usage.collect_claude_code_usage(repo_root)

    assert len(seen) == 2
    assert second["totals"]["main"]["api_calls"] == 3
    assert second["totals"]["main"]["output_tokens"] == 50 + 2 + 3
    assert second["totals"]["main"]["input_tokens"] == 10 + 20 + 30
    assert second == _fresh_totals(repo_root)


def test_trailing_partial_line_is_counted_but_not_consumed(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)
    transcript = project_dir / "session-a.jsonl"
    whole = _usage_line("msg_2", input_tokens=20, output_tokens=2)
    transcript.write_text(
        _usage_line("msg_1", input_tokens=10) + "\n" + whole[:40], encoding="utf-8"
    )
    claude_code_usage._FILE_CACHE.clear()

    first = claude_code_usage.collect_claude_code_usage(repo_root)
    assert first["totals"]["main"]["malformed_lines"] == 1
    assert first["totals"]["main"]["api_calls"] == 1
    assert first == _fresh_totals(repo_root)

    _append(transcript, whole[40:] + "\n")
    seen = _line_counter(monkeypatch)
    second = claude_code_usage.collect_claude_code_usage(repo_root)

    # Only the completed line is read, and read whole.
    assert seen == [(whole + "\n").encode("utf-8")]
    assert second["totals"]["main"]["malformed_lines"] == 0
    assert second["totals"]["main"]["api_calls"] == 2
    assert second == _fresh_totals(repo_root)


def test_truncated_or_rewritten_transcript_falls_back_to_full_parse(
    monkeypatch, tmp_path
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)
    transcript = project_dir / "session-a.jsonl"
    transcript.write_text(
        "".join(_usage_line(f"msg_{i}", input_tokens=5) + "\n" for i in range(4)),
        encoding="utf-8",
    )
    claude_code_usage._FILE_CACHE.clear()
    claude_code_usage.collect_claude_code_usage(repo_root)

    rewrites = [
        # Truncated: shorter than the consumed offset.
        _usage_line("msg_0", input_tokens=5) + "\n",
        # Rewritten: longer, but the prefix before the old offset differs.
        "".join(_usage_line(f"other_{i}", input_tokens=9) + "\n" for i in range(6)),
    ]
    for content in rewrites:
        transcript.write_text(content, encoding="utf-8")
        tails_before = claude_code_usage.CACHE_STATS["tail_parses"]
        fulls_before = claude_code_usage.CACHE_STATS["full_parses"]
        result = claude_code_usage.collect_claude_code_usage(repo_root)
        assert claude_code_usage.CACHE_STATS["full_parses"] == fulls_before + 1
        assert claude_code_usage.CACHE_STATS["tail_parses"] == tails_before
        expected_calls = content.count("\n")
        assert result["totals"]["main"]["api_calls"] == expected_calls
        assert result == _fresh_totals(repo_root)


def test_collect_memo_skips_all_file_io_within_ttl(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)
    transcript = project_dir / "session-a.jsonl"
    transcript.write_text(_usage_line("msg_1", input_tokens=1) + "\n", encoding="utf-8")
    monkeypatch.setattr(claude_code_usage, "_COLLECT_MEMO_TTL_SECONDS", 30.0)
    claude_code_usage._FILE_CACHE.clear()

    first = claude_code_usage.collect_claude_code_usage(repo_root)
    _append(transcript, _usage_line("msg_2", input_tokens=2) + "\n")

    def _no_io(*_args, **_kwargs):
        raise AssertionError("memo hit must not touch the filesystem")

    monkeypatch.setattr(claude_code_usage, "_matching_project_dirs", _no_io)
    monkeypatch.setattr(claude_code_usage, "_parse_files", _no_io)
    hits_before = claude_code_usage.CACHE_STATS["memo_hits"]
    second = claude_code_usage.collect_claude_code_usage(repo_root)

    assert second == first
    assert claude_code_usage.CACHE_STATS["memo_hits"] == hits_before + 1
    # The caller gets a copy: mutating it never corrupts the memo.
    second["totals"]["main"]["api_calls"] = -1
    assert claude_code_usage.collect_claude_code_usage(repo_root) == first

    monkeypatch.undo()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_home"))
    claude_code_usage._COLLECT_MEMO.clear()
    fresh = claude_code_usage.collect_claude_code_usage(repo_root)
    assert fresh["totals"]["main"]["api_calls"] == 2
    assert os.path.getsize(transcript) > 0


def test_collect_memo_never_keeps_a_transiently_locked_zero(monkeypatch, tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    project_dir = _setup_project(monkeypatch, tmp_path, repo_root)
    transcript = project_dir / "session-a.jsonl"
    transcript.write_text(_usage_line("msg_1", input_tokens=1) + "\n", encoding="utf-8")
    monkeypatch.setattr(claude_code_usage, "_COLLECT_MEMO_TTL_SECONDS", 30.0)
    claude_code_usage._FILE_CACHE.clear()
    real_parser = claude_code_usage._parse_transcript_file
    monkeypatch.setattr(
        claude_code_usage, "_parse_transcript_file", lambda path, previous=None: None
    )

    locked = claude_code_usage.collect_claude_code_usage(repo_root)
    assert locked["totals"]["main"]["api_calls"] == 0

    monkeypatch.setattr(claude_code_usage, "_parse_transcript_file", real_parser)
    readable = claude_code_usage.collect_claude_code_usage(repo_root)
    assert readable["totals"]["main"]["api_calls"] == 1
