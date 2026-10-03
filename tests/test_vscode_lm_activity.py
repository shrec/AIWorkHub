"""Request-owned visible tool activity: bounded, credential-redacted, advisory."""

import json
import os
from pathlib import Path

import pytest

from aiworkhub.vscode_lm_activity import ActivityReader, redact_text


def journal(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / ("a" * 32) / "home"
    home.mkdir(parents=True, mode=0o700)
    return home, home / ".aiworkhub_vscode_lm_activity.jsonl"


def row(sequence: int, **extra: object) -> dict[str, object]:
    return {"schema_id": "aiworkhub.vscode_lm.activity.v1", "request_id": "a" * 32,
            "repo_id": "repo_test", "sequence": sequence, "kind": "tool",
            "call_id": "call-1", "tool_name": "read", "tool_state": "completed",
            "tool_transport": "native", "updated_at": "2026-10-03T00:00:00Z", **extra}


def write(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows),
                    encoding="utf-8")
    path.chmod(0o600)


@pytest.mark.parametrize("windows", [False, True])
def test_private_checks_dispatch_through_platform_interface(monkeypatch, windows: bool) -> None:
    import stat
    from types import SimpleNamespace

    from aiworkhub import platform_io, vscode_lm_activity as activity

    class LocalOS:
        def __getattr__(self, name):
            assert name != "name", "activity must not decide platforms through raw os.name"
            return getattr(os, name)

    platform_calls: list[bool] = []

    def is_windows() -> bool:
        platform_calls.append(windows)
        return windows

    monkeypatch.setattr(activity, "os", LocalOS())
    monkeypatch.setattr(activity, "is_windows", is_windows, raising=False)
    monkeypatch.setattr(activity, "current_user_uid", lambda: None if windows else 1000)
    assert platform_io.os is os  # The local facade never changes the central interface's OS.
    metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=1000, st_nlink=1)
    ActivityReader._private(metadata)
    assert platform_calls == [windows]
    for mode, owner in ((0o600, 1001), (0o644, 1000)):
        metadata.st_mode = stat.S_IFREG | mode
        metadata.st_uid = owner
        if windows:
            ActivityReader._private(metadata)
        else:
            with pytest.raises(activity.ActivityCaptureError, match="activity_owner"):
                ActivityReader._private(metadata)


def test_uid_authority_failure_is_typed_advisory_capture_error(tmp_path: Path, monkeypatch) -> None:
    from aiworkhub import vscode_lm_activity as activity

    home, path = journal(tmp_path)
    write(path, [row(1)])

    def unavailable_uid():
        raise OSError("current_user_uid_unavailable")

    monkeypatch.setattr(activity, "current_user_uid", unavailable_uid)
    with pytest.raises(activity.ActivityCaptureError) as rejected:
        ActivityReader(path, home, "a" * 32, "repo_test").drain()
    assert isinstance(rejected.value.__cause__, OSError)


def test_reader_rapid_unicode_events_and_incremental_no_duplicates(tmp_path: Path) -> None:
    home, path = journal(tmp_path)
    write(path, [row(i, output_preview="ქართული 😀 <script>") for i in range(1, 101)])
    reader = ActivityReader(path, home, "a" * 32, "repo_test")
    events = reader.drain()
    assert [event["sequence"] for event in events] == list(range(1, 101))
    assert events[-1]["output_preview"] == "ქართული 😀 <script>"
    assert reader.drain() == []
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row(101)) + "\n")
    assert [event["sequence"] for event in reader.drain()] == [101]


@pytest.mark.parametrize("value", [
    'Authorization: Bearer short-secret', 'password=short-secret',
    '{"nested":{"api_key":"short-secret"}}',
    'https://name:short-secret@example.com/x?token=short-secret',
    'postgresql://name:short-secret@example.com/db', '--password short-secret',
    'Authorization: Basic short-secret',
    'sk-abcdefghijklmnop',
])
def test_recognized_credentials_redacted_before_relay(value: str) -> None:
    safe = redact_text(value)
    assert "short-secret" not in safe
    assert "sk-abcdefghijklmnop" not in safe
    assert "[redacted]" in safe


@pytest.mark.parametrize("extra", [
    {"request_id": "b" * 32}, {"repo_id": "foreign"}, {"sequence": 2},
    {"sequence": True}, {"tool_state": "success"}, {"cancel_token": "secret"},
    {"capture_end": 1}, {"preview_truncated": "yes"}, {"elapsed_ms": -1},
])
def test_foreign_replayed_or_unrecognized_rows_fail_closed(tmp_path: Path, extra: dict) -> None:
    home, path = journal(tmp_path)
    write(path, [{**row(1), **extra}])
    with pytest.raises(RuntimeError, match="activity"):
        ActivityReader(path, home, "a" * 32, "repo_test").drain()


def test_missing_is_unknown_partial_line_deferred_and_final_rejected(tmp_path: Path) -> None:
    home, path = journal(tmp_path)
    reader = ActivityReader(path, home, "a" * 32, "repo_test")
    assert reader.drain() == []
    assert reader.availability == "unknown"
    path.write_text(json.dumps(row(1)), encoding="utf-8")
    path.chmod(0o600)
    assert reader.drain() == []
    with pytest.raises(RuntimeError, match="activity_incomplete"):
        reader.drain(final=True)


def test_journal_symlink_and_parent_escape_are_rejected(tmp_path: Path) -> None:
    home, path = journal(tmp_path)
    target = tmp_path / "outside"
    write(target, [row(1)])
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("host cannot create symlinks")
    with pytest.raises(RuntimeError, match="activity"):
        ActivityReader(path, home, "a" * 32, "repo_test").drain()
    with pytest.raises(RuntimeError, match="activity_path"):
        ActivityReader(target, home, "a" * 32, "repo_test")


@pytest.mark.skipif(os.name == "nt", reason="POSIX private-mode contract")
def test_nonprivate_journal_rejected(tmp_path: Path) -> None:
    home, path = journal(tmp_path)
    write(path, [row(1)])
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="activity_owner"):
        ActivityReader(path, home, "a" * 32, "repo_test").drain()


def test_hardlink_and_oversized_journal_rejected(tmp_path: Path) -> None:
    home, path = journal(tmp_path)
    write(path, [row(1)])
    os.link(path, tmp_path / "alias")
    with pytest.raises(RuntimeError, match="activity_owner"):
        ActivityReader(path, home, "a" * 32, "repo_test").drain()
    (tmp_path / "alias").unlink()
    path.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(RuntimeError, match="activity_journal_limit"):
        ActivityReader(path, home, "a" * 32, "repo_test").drain()


def test_existing_cursor_rejects_replacement_and_status_is_explicit(tmp_path: Path) -> None:
    home, path = journal(tmp_path)
    write(path, [row(1, kind="status", capture_status="limited", dropped_events=4, capture_end=True)])
    reader = ActivityReader(path, home, "a" * 32, "repo_test")
    reader.drain(final=True)
    assert reader.availability == "limited" and reader.capture_end is True
    assert reader.dropped_events == 4
    replacement = home / "replacement"
    write(replacement, [row(2, output_preview="padding " * 100)])
    replacement.replace(path)
    with pytest.raises(RuntimeError, match="activity_identity_changed"):
        reader.drain()


@pytest.mark.parametrize("key", sorted({
    "schema_id", "request_id", "repo_id", "kind", "call_id", "tool_name", "tool_state",
    "tool_transport", "updated_at", "input_preview", "output_preview", "error_code",
    "capture_status", "redaction_coverage", "sequence", "elapsed_ms", "dropped_events",
    "capture_end", "preview_truncated",
}))
@pytest.mark.parametrize("bad", [[], {}, None, "\ud800"])
def test_malformed_scalar_metadata_is_typed_capture_rejection(tmp_path: Path, key: str, bad: object) -> None:
    from aiworkhub.vscode_lm_activity import ActivityCaptureError
    home, path = journal(tmp_path)
    reader = ActivityReader(path, home, "a" * 32, "repo_test")
    raw = json.dumps({**row(1), key: bad}, ensure_ascii=True).encode("utf-8")
    with pytest.raises(ActivityCaptureError, match="activity"):
        reader._row(raw, 1)  # noqa: SLF001


def test_optional_metadata_defaults_and_string_metadata_redaction(tmp_path: Path) -> None:
    home, path = journal(tmp_path)
    reader = ActivityReader(path, home, "a" * 32, "repo_test")
    value = row(1, call_id="password=short-secret", tool_name="Bearer short-secret")
    value.pop("tool_transport")
    value.pop("updated_at")
    event = reader._row(json.dumps(value).encode(), 1)  # noqa: SLF001
    assert "short-secret" not in json.dumps(event)
    assert event.get("tool_transport", "unknown") == "unknown"
