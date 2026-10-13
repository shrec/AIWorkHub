"""NF-2026-01493: opencode_cli whole-stream recovered-exit classification."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aiworkhub import process_launcher, terminal_failure_classification
from test_supervisor_terminal_authority_b894b import (
    _build_manager,
    _card,
    _patch_success_pipeline,
    _seed_exited_request,
)


def _events_to_jsonl(events: list[dict]) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _recovered_events() -> list[dict]:
    return [
        {"type": "step_start"},
        {"type": "error", "timestamp": 1, "sessionID": "ses_1", "error": {"message": "model failed to generate response"}},
        {"type": "step_start"},
        {"type": "text", "text": "done"},
    ]


def _error_last_events() -> list[dict]:
    return [
        {"type": "step_start"},
        {"type": "text", "text": "partial"},
        {"type": "error", "timestamp": 1, "sessionID": "ses_1", "error": {"message": "fatal"}},
    ]


def test_opencode_recovered_exit_true_for_opencode_cli(tmp_path):
    stdout = _write(tmp_path, "stdout.jsonl", _events_to_jsonl(_recovered_events()))
    assert terminal_failure_classification.opencode_recovered_exit("opencode_cli", str(stdout)) is True


def test_opencode_recovered_exit_false_for_other_adapter(tmp_path):
    stdout = _write(tmp_path, "stdout.jsonl", _events_to_jsonl(_recovered_events()))
    assert terminal_failure_classification.opencode_recovered_exit("claude_cli", str(stdout)) is False


def test_opencode_recovered_exit_scans_whole_stream_past_4kb_and_400_lines(tmp_path):
    events = [{"type": "step_start"}]
    events.append({"type": "error", "timestamp": 1, "sessionID": "ses_1", "error": {"message": "transient"}})
    # More than 4096 bytes and more than 400 lines of non-error events after the error.
    for i in range(500):
        events.append({"type": "text", "text": "x" * 50, "index": i})
    stdout = _write(tmp_path, "stdout.jsonl", _events_to_jsonl(events))
    assert stdout.stat().st_size > 4096
    assert len(events) > 400
    assert terminal_failure_classification.opencode_recovered_exit("opencode_cli", str(stdout)) is True


def test_opencode_recovered_exit_false_when_error_is_last(tmp_path):
    stdout = _write(tmp_path, "stdout.jsonl", _events_to_jsonl(_error_last_events()))
    assert terminal_failure_classification.opencode_recovered_exit("opencode_cli", str(stdout)) is False


def test_opencode_recovered_exit_false_when_no_error_event(tmp_path):
    events = [{"type": "step_start"}, {"type": "text", "text": "ok"}]
    stdout = _write(tmp_path, "stdout.jsonl", _events_to_jsonl(events))
    assert terminal_failure_classification.opencode_recovered_exit("opencode_cli", str(stdout)) is False


def test_opencode_recovered_exit_false_for_missing_file(tmp_path):
    missing = tmp_path / "missing.jsonl"
    assert terminal_failure_classification.opencode_recovered_exit("opencode_cli", str(missing)) is False


def test_opencode_recovered_exit_false_for_empty_file(tmp_path):
    stdout = _write(tmp_path, "empty.jsonl", "")
    assert terminal_failure_classification.opencode_recovered_exit("opencode_cli", str(stdout)) is False


def test_opencode_recovered_exit_false_for_non_json_stdout(tmp_path):
    stdout = _write(tmp_path, "garbage.txt", "not json at all\nmore garbage\n")
    assert terminal_failure_classification.opencode_recovered_exit("opencode_cli", str(stdout)) is False


def test_opencode_recovered_exit_false_for_symlinked_stdout(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("validation_unsupported_in_sandbox: os.symlink unavailable")
    target = _write(tmp_path, "target.jsonl", _events_to_jsonl(_recovered_events()))
    link = tmp_path / "link.jsonl"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("validation_unsupported_in_sandbox: symlink creation not permitted")
    assert terminal_failure_classification.opencode_recovered_exit("opencode_cli", str(link)) is False


def test_opencode_recovered_exit_false_for_single_line_over_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_failure_classification, "MAX_OPENCODE_SCAN_LINE_BYTES", 1024)
    stdout = _write(tmp_path, "stdout.jsonl", "x" * 2000)
    assert terminal_failure_classification.opencode_recovered_exit("opencode_cli", str(stdout)) is False


def test_opencode_recovered_exit_false_for_oversized_line_after_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_failure_classification, "MAX_OPENCODE_SCAN_LINE_BYTES", 1024)
    events = [
        {"type": "error", "timestamp": 1, "sessionID": "ses_1", "error": {"message": "transient"}},
        {"type": "text", "text": "ok"},
    ]
    content = _events_to_jsonl(events) + ("x" * 2000)
    stdout = _write(tmp_path, "stdout.jsonl", content)
    assert terminal_failure_classification.opencode_recovered_exit("opencode_cli", str(stdout)) is False


def test_opencode_recovered_exit_false_for_text_mode_byte_mid_stream(tmp_path):
    stdout = tmp_path / "stdout.jsonl"
    stdout.write_bytes(b'{"type":"error"}\n{"type":"text"}\n\x1a\n{"type":"error"}\n')
    assert terminal_failure_classification.opencode_recovered_exit("opencode_cli", str(stdout)) is False


def test_recovered_exit_code_promotes_error_one_when_recovered(tmp_path):
    stdout = _write(tmp_path, "stdout.jsonl", _events_to_jsonl(_recovered_events()))
    assert terminal_failure_classification.recovered_exit_code("opencode_cli", str(stdout), 1) == 0


def test_recovered_exit_code_keeps_crash_code_even_when_recovered(tmp_path):
    stdout = _write(tmp_path, "stdout.jsonl", _events_to_jsonl(_recovered_events()))
    assert terminal_failure_classification.recovered_exit_code("opencode_cli", str(stdout), 137) == 137


def test_recovered_exit_code_keeps_one_when_error_last(tmp_path):
    stdout = _write(tmp_path, "stdout.jsonl", _events_to_jsonl(_error_last_events()))
    assert terminal_failure_classification.recovered_exit_code("opencode_cli", str(stdout), 1) == 1


def test_recovered_exit_code_keeps_one_for_other_adapter(tmp_path):
    stdout = _write(tmp_path, "stdout.jsonl", _events_to_jsonl(_recovered_events()))
    assert terminal_failure_classification.recovered_exit_code("claude_cli", str(stdout), 1) == 1


def test_finalize_isolated_request_recovers_opencode_exit_one(tmp_path, monkeypatch):
    card = _card()
    manager = _build_manager(tmp_path, card)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: True)
    _patch_success_pipeline(monkeypatch, card, {})

    request_id = "req-recovered-opencode"
    _seed_exited_request(manager, tmp_path, card, request_id=request_id)
    process_dir = tmp_path / "processes"

    status_path = process_dir / f"{request_id}.supervisor.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["exit_code"] = 1
    status_path.write_text(json.dumps(status), encoding="utf-8")

    metadata_path = process_dir / f"{request_id}.request.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["adapter_id"] = "opencode_cli"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    stdout_path = process_dir / f"{request_id}.stdout.log"
    stdout_path.write_text(_events_to_jsonl(_recovered_events()), encoding="utf-8")

    result = manager._finalize_isolated_request(request_id)

    clean_request_id = "req-clean-opencode"
    _seed_exited_request(manager, tmp_path, card, request_id=clean_request_id)

    clean_status_path = process_dir / f"{clean_request_id}.supervisor.json"
    clean_status = json.loads(clean_status_path.read_text(encoding="utf-8"))
    clean_status["exit_code"] = 0
    clean_status_path.write_text(json.dumps(clean_status), encoding="utf-8")

    clean_metadata_path = process_dir / f"{clean_request_id}.request.json"
    clean_metadata = json.loads(clean_metadata_path.read_text(encoding="utf-8"))
    clean_metadata["adapter_id"] = "opencode_cli"
    clean_metadata_path.write_text(json.dumps(clean_metadata), encoding="utf-8")

    clean_stdout_path = process_dir / f"{clean_request_id}.stdout.log"
    clean_stdout_path.write_text(_events_to_jsonl(_recovered_events()), encoding="utf-8")

    clean_result = manager._finalize_isolated_request(clean_request_id)

    assert result["state"] == clean_result["state"]
    latest = manager._latest_by_request()[request_id]
    assert latest["exit_code"] == 0
    assert latest["provider_exit_code"] == 1


@pytest.mark.parametrize(
    ("adapter_override", "events_factory"),
    [
        (None, _recovered_events),
        ("opencode_cli", _error_last_events),
    ],
)
def test_finalize_isolated_request_worker_failed_for_non_opencode_error(
    tmp_path, monkeypatch, adapter_override, events_factory
):
    card = _card()
    manager = _build_manager(tmp_path, card)
    monkeypatch.setattr(process_launcher.core, "writes_allowed", lambda: True)
    _patch_success_pipeline(monkeypatch, card, {})
    failure_calls = []

    def fake_mark_terminal_failure(
        repo, task_id, runner, substatus, *, evidence=None, request_id=""
    ):
        failure_calls.append(substatus)
        card.update({"status": "blocked", "worker_status": substatus})
        return {"ok": True}

    monkeypatch.setattr(
        process_launcher.task_engine, "mark_terminal_failure", fake_mark_terminal_failure
    )

    request_id = "req-control-claude"
    _seed_exited_request(manager, tmp_path, card, request_id=request_id)
    process_dir = tmp_path / "processes"

    status_path = process_dir / f"{request_id}.supervisor.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["exit_code"] = 1
    status_path.write_text(json.dumps(status), encoding="utf-8")

    if adapter_override is not None:
        metadata_path = process_dir / f"{request_id}.request.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["adapter_id"] = adapter_override
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    stdout_path = process_dir / f"{request_id}.stdout.log"
    stdout_path.write_text(_events_to_jsonl(events_factory()), encoding="utf-8")

    result = manager._finalize_isolated_request(request_id)

    assert result["state"] == "worker_failed"
    assert failure_calls == ["worker_failed"]
    assert manager._latest_by_request()[request_id]["exit_code"] == 1
