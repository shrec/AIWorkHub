from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("capture_manager_stream", ROOT / "scripts" / "capture_manager_stream.py")
cms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cms)


def _replay_spawn(lines: list[str]):
    script = "import sys\nfor line in sys.argv[1:]:\n    print(line, flush=True)\n"

    def spawn(argv, cwd, stdin_text=None, env=None):
        return cms._spawn_cli([sys.executable, "-c", script, *lines], cwd, stdin_text, env)

    return spawn


def _plan_builder(backend_id, prompt, repo, *, model):
    return SimpleNamespace(launchable=True, argv=[sys.executable], cwd=str(repo), stdin_text=None, validation_reason="")


def test_redact_line_replaces_paths_email_and_uuids_stably():
    repo = Path("D:/Work/Repo")
    spellings = cms.path_spellings((repo / "sub", "<workdir>"), (repo, "<repo>"))
    ids: dict[str, str] = {}
    raw = json.dumps(
        {
            "cwd": "D:\\Work\\Repo\\sub",
            "file": "d:/work/repo/README.md",
            "by": "owner@example.org",
            "session_id": "0f8fad5b-d9cb-469f-a165-70867728950e",
            "parent": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
            "again": "0F8FAD5B-D9CB-469F-A165-70867728950E",
        }
    )
    line = cms.redact_line(raw, spellings, ids)
    data = json.loads(line)
    assert data["cwd"] == "<workdir>"
    assert data["file"] == "<repo>/README.md"
    assert data["by"] == "<email>"
    assert data["session_id"] == data["again"] == "00000000-0000-4000-8000-000000000001"
    assert data["parent"] == "00000000-0000-4000-8000-000000000002"
    for leaked in ("Work", "Repo", "example.org", "0f8fad5b", "7c9e6679"):
        assert leaked not in line


def test_a_user_name_is_replaced_only_as_a_whole_word():
    line = cms.redact_line("ann wrote annotations for ann", [("ann", "<user>")], {})
    assert line == "<user> wrote annotations for <user>"


def test_opencode_session_ids_are_replaced_stably():
    ids: dict[str, str] = {}
    line = '{"sessionID":"ses_3aF9kQ2xZ","parent":"ses_7Hm1Pq","again":"ses_3aF9kQ2xZ"}'
    decoded = json.loads(cms.redact_line(line, [], ids))
    assert "ses_3aF9kQ2xZ" not in json.dumps(decoded)
    assert "ses_7Hm1Pq" not in json.dumps(decoded)
    assert decoded["sessionID"] == decoded["again"] == "ses_fixture1"
    assert decoded["parent"] == "ses_fixture2"
    assert cms.redact_line('{"id":"ses_7Hm1Pq"}', [], ids) == '{"id":"ses_fixture2"}'


def test_capture_tees_redacted_lines_and_keeps_the_stream(tmp_path: Path):
    workdir = tmp_path / "work"
    out = tmp_path / "out" / "claude_cli.jsonl"
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "0f8fad5b-d9cb-469f-a165-70867728950e", "cwd": str(workdir)}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}}),
        "",
        json.dumps({"type": "result", "session_id": "0f8fad5b-d9cb-469f-a165-70867728950e", "usage": {"input_tokens": 3, "output_tokens": 1}}),
    ]
    events = cms.capture(
        "claude_cli", "fixture-model", out, workdir=workdir / ".." / "work",
        spawn=_replay_spawn(lines), plan_builder=_plan_builder,
    )
    written = out.read_text(encoding="utf-8").splitlines()
    assert len(written) == 3
    assert [event["type"] for event in events] == ["assistant_text", "turn_end"]
    assert str(workdir) not in out.read_text(encoding="utf-8")
    assert json.loads(written[0])["cwd"] == "<workdir>"
    assert json.loads(written[0])["session_id"] == json.loads(written[2])["session_id"] == "00000000-0000-4000-8000-000000000001"


def test_default_workdir_is_inside_the_repository():
    assert cms.default_workdir("codex_cli") == ROOT / ".aiworkhub" / "runtime" / "fixture_capture" / "codex_cli"


def test_an_empty_spelling_is_never_a_pattern():
    assert cms.redact_line('{"a":"b"}', [("", "<user>")], {}) == '{"a":"b"}'
    assert all(spelling for spelling, _ in cms.path_spellings(("", "<x>"), (Path("D:/Work"), "<repo>")))


def test_user_names_cover_the_account_and_the_home_basename(monkeypatch):
    monkeypatch.setattr(cms.getpass, "getuser", lambda: "jsmith")
    names = cms._user_names(Path("C:/Users/jsmith.CORP"))
    assert names == ["jsmith.CORP", "jsmith"]
    assert cms.redact_line("by jsmith", [(name, "<user>") for name in names], {}) == "by <user>"
    assert cms._user_names(Path("C:/Users/jsmith")) == ["jsmith"]

    def no_account():
        raise KeyError("USERNAME")

    monkeypatch.setattr(cms.getpass, "getuser", no_account)
    assert cms._user_names(Path(Path.cwd().anchor)) == []


def test_a_wrong_argument_count_prints_usage_and_returns_2(capsys):
    assert cms.main([]) == 2
    assert capsys.readouterr().err.startswith("Usage: ")


def _delta(index, field, fragment, kind="input_json_delta"):
    event = {"type": "content_block_delta", "index": index, "delta": {"type": kind, field: fragment}}
    return json.dumps({"type": "stream_event", "event": event})


def _redactor(spellings):
    ids: dict[str, str] = {}
    return lambda line: cms.redact_line(line, spellings, ids)


def test_a_path_inside_a_nested_json_string_is_replaced():
    line = json.dumps({"rawInput": json.dumps({"path": "D:\\Work\\Repo\\notes.txt"})})
    redacted = cms.redact_line(line, cms.path_spellings((Path("D:/Work/Repo"), "<repo>")), {})
    assert "Work" not in redacted
    assert "Repo" not in redacted
    assert json.loads(json.loads(redacted)["rawInput"])["path"].startswith("<repo>")


def test_a_prefixed_uuid_is_replaced_with_the_same_placeholder():
    uuid = "0f8fad5b-d9cb-469f-a165-70867728950e"
    line = cms.redact_line(f"rs_{uuid} fc_{uuid}_0", [], {})
    assert line == "rs_00000000-0000-4000-8000-000000000001 fc_00000000-0000-4000-8000-000000000001_0"


def test_a_hyphen_adjacent_uuid_is_replaced():
    first = "0f8fad5b-d9cb-469f-a165-70867728950e"
    second = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
    line = cms.redact_line(f"call-{first}-1 x-{second} {first}-{second}", [], {})
    assert line == (
        "call-00000000-0000-4000-8000-000000000001-1 x-00000000-0000-4000-8000-000000000002"
        " 00000000-0000-4000-8000-000000000001-00000000-0000-4000-8000-000000000002"
    )


def test_a_claude_project_slug_of_a_root_is_replaced():
    repo = Path("D:/Work/Repo")
    spellings = cms.path_spellings((repo / ".sub" / "fixture_dir", "<workdir>"), (repo, "<repo>"))
    line = cms.redact_line("projects/D--Work-Repo--sub-fixture-dir/memory other/d--work-repo/x", spellings, {})
    assert line == "projects/<workdir>/memory other/<repo>/x"
    assert all(spelling.strip("-") for spelling, _ in cms.path_spellings(("", "<x>")))


def test_capture_redacts_a_path_split_across_delta_lines(tmp_path: Path):
    workdir = tmp_path / "zqxcapturedir"
    out = tmp_path / "out" / "claude_cli.jsonl"
    partial = json.dumps({"file_path": str(workdir / "notes.txt")})
    first = partial.index("zqxcapturedir") + 5
    second = partial.index("notes.txt")
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "0f8fad5b-d9cb-469f-a165-70867728950e", "cwd": str(workdir)}),
        *(_delta(1, "partial_json", fragment) for fragment in (partial[:first], partial[first:second], partial[second:])),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}}),
        json.dumps({"type": "result", "session_id": "0f8fad5b-d9cb-469f-a165-70867728950e", "usage": {"input_tokens": 3, "output_tokens": 1}}),
    ]
    events = cms.capture(
        "claude_cli", "fixture-model", out, workdir=workdir,
        spawn=_replay_spawn(lines), plan_builder=_plan_builder,
    )
    text = out.read_text(encoding="utf-8")
    written = text.splitlines()
    assert len(written) == 6
    assert str(workdir) not in text
    assert "zqxcapturedir" not in text
    joined = "".join(json.loads(line)["event"]["delta"]["partial_json"] for line in written[1:4])
    assert json.loads(joined) == {"file_path": "<workdir>" + str(Path("a") / "b")[1] + "notes.txt"}
    assert [event["type"] for event in events] == ["assistant_text", "turn_end"]


def test_held_delta_lines_are_written_when_the_stream_ends_raises_or_closes():
    raw = [
        _delta(0, "text", "mail owner@exa", "text_delta") + "\n",
        _delta(0, "text", "mple.org now", "text_delta") + "\n",
    ]
    sink = io.StringIO()
    assert list(cms._Tee._lines(iter(raw), sink, _redactor([]))) == raw
    written = sink.getvalue().splitlines()
    assert len(written) == 2
    assert "example.org" not in sink.getvalue()
    assert "".join(json.loads(line)["event"]["delta"]["text"] for line in written) == "mail <email> now"

    def failing():
        yield from raw
        raise OSError("pipe closed")

    sink = io.StringIO()
    with pytest.raises(OSError):
        list(cms._Tee._lines(failing(), sink, _redactor([])))
    assert len(sink.getvalue().splitlines()) == 2

    sink = io.StringIO()
    lines = cms._Tee._lines(iter(raw), sink, _redactor([]))
    assert next(lines) == raw[0]
    assert sink.getvalue() == ""
    lines.close()
    assert len(sink.getvalue().splitlines()) == 1


def test_a_delta_run_without_secrets_is_written_byte_for_byte():
    expected = [_delta(0, "text", "do", "text_delta"), _delta(0, "text", "ne", "text_delta"), '{"type":"result"}']
    raw = [line + "\n" for line in expected]
    sink = io.StringIO()
    assert list(cms._Tee._lines(iter(raw), sink, _redactor([]))) == raw
    assert sink.getvalue().splitlines() == expected
