"""NF-2026-01749: an opencode run that writes its tool calls as plain-text XML is a typed route failure."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import terminal_failure_classification as tfc

_REASON = "provider_text_tool_call_protocol"
_TOOL_CALL_TEXT = (
    "I will inspect the file.\n<tool_call>\n<function=read>\n"
    "<parameter=path>src/app.py</parameter>\n</function>\n</tool_call>"
)


def _compact(event: dict[str, Any]) -> str:
    return json.dumps(event, separators=(",", ":"))


def _text(text: str, *, kind: str = "text", part_type: str = "text") -> str:
    return _compact({
        "type": kind,
        "sessionID": "ses_1",
        "part": {"id": "prt_1", "sessionID": "ses_1", "messageID": "msg_1", "type": part_type, "text": text},
    })


def _tool_use() -> str:
    return _compact({
        "type": "tool_use",
        "sessionID": "ses_1",
        "part": {"id": "prt_2", "sessionID": "ses_1", "type": "tool", "tool": "bash", "state": {"status": "completed"}},
    })


def _step_finish() -> str:
    return _compact({"type": "step_finish", "sessionID": "ses_1", "part": {"type": "step-finish", "reason": "stop"}})


def _stream(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _disposition(tmp_path: Path, stdout: str) -> dict[str, Any]:
    stdout_path = tmp_path / "stdout.log"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path = tmp_path / "stderr.log"
    stderr_path.write_text("", encoding="utf-8")
    return tfc.failure_disposition_from_paths(
        state="worker_failed",
        error=tfc.supervisor_failure_reason("exited", 1).render(),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


def test_opencode_xml_text_without_a_tool_part_is_classified(tmp_path: Path) -> None:
    disposition = _disposition(tmp_path, _stream(_text(_TOOL_CALL_TEXT), _step_finish()))
    assert disposition["provider_code"] == _REASON
    assert disposition["failure_class"] == tfc.FAILURE_CLASS_TRANSIENT
    assert disposition["cause"] == tfc.CAUSE_PROVIDER_TRANSIENT
    assert disposition["evidence_authority"] == tfc.EVIDENCE_AUTHORITY_PROVIDER_ENVELOPE


@pytest.mark.parametrize(
    "text",
    [
        "<function_call>{\"name\":\"read\"}</function_call>",
        "<invoke name=\"read\"><parameter name=\"path\">a.py</parameter></invoke>",
        "<TOOL_CALL>x</TOOL_CALL>",
        "<function_calls><invoke name=\"x\"></invoke></function_calls>",
    ],
)
def test_the_other_plain_text_tool_call_shapes_are_classified(tmp_path: Path, text: str) -> None:
    disposition = _disposition(tmp_path, _stream(_text(text)))
    assert disposition["provider_code"] == _REASON


def test_the_signal_names_the_code_and_a_registered_envelope() -> None:
    signal = tfc.provider_terminal_signal(_stream(_text(_TOOL_CALL_TEXT)))
    assert signal["code"] == _REASON
    assert signal["envelope"] in tfc._PROVIDER_TERMINAL_ENVELOPES
    assert signal["status"] is None


def test_disposition_allows_a_distinct_route_retry_and_refuses_the_same_route(tmp_path: Path) -> None:
    disposition = _disposition(tmp_path, _stream(_text(_TOOL_CALL_TEXT)))
    assert disposition["action"] == tfc.ACTION_ROUTE_RETRY
    assert disposition["retry_scope"] == tfc.RETRY_SCOPE_DISTINCT_ROUTE
    assert disposition["cause_owner"] == tfc.CAUSE_OWNER_PROVIDER_ROUTE
    assert tfc.distinct_route_retry_allowed(disposition=disposition, attempts_made=0)
    assert not tfc.distinct_route_retry_allowed(
        disposition=disposition, attempts_made=tfc.MAX_DISTINCT_ROUTE_RETRIES + 1
    )
    assert not tfc.same_route_retry_allowed(disposition=disposition)
    assert not tfc.same_route_retry_allowed(disposition=disposition, circuit_allows=True)
    assert not tfc.same_route_retry_allowed(disposition=disposition, reset_evidence={"retry_after_seconds": 5})


def test_a_structured_tool_part_means_the_xml_is_prose(tmp_path: Path) -> None:
    disposition = _disposition(tmp_path, _stream(_tool_use(), _text(_TOOL_CALL_TEXT), _step_finish()))
    assert disposition["provider_code"] != _REASON
    assert disposition["cause"] != tfc.CAUSE_PROVIDER_TRANSIENT


def test_a_structured_tool_part_after_the_text_also_clears_it() -> None:
    signal = tfc.provider_terminal_signal(_stream(_text(_TOOL_CALL_TEXT), _tool_use()))
    assert signal["code"] != _REASON


def test_non_opencode_streams_with_the_same_text_are_not_classified(tmp_path: Path) -> None:
    claude = _compact({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": _TOOL_CALL_TEXT}]}})
    codex = _compact({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": _TOOL_CALL_TEXT}})
    for name, line in (("claude", claude), ("codex", codex)):
        folder = tmp_path / name
        folder.mkdir()
        disposition = _disposition(folder, _stream(line))
        assert disposition["provider_code"] != _REASON, name
        assert disposition["cause"] != tfc.CAUSE_PROVIDER_TRANSIENT, name


@pytest.mark.parametrize(
    "line",
    [
        _text(_TOOL_CALL_TEXT, part_type="reasoning"),
        _text(_TOOL_CALL_TEXT, kind="message.part.updated"),
        _text("plain prose that mentions tool_call without any markup"),
        _text("<tool_call> opened and never closed"),
        _text("<tool_calls>x</tool_calls>"),
    ],
)
def test_only_a_closed_tool_call_block_in_a_text_part_counts(tmp_path: Path, line: str) -> None:
    assert _disposition(tmp_path, _stream(line))["provider_code"] != _REASON


def test_a_provider_error_outranks_the_xml_text(tmp_path: Path) -> None:
    error = _compact({
        "type": "error",
        "error": {"type": "provider.quota", "message": "Upstream request failed: Insufficient account funds", "status": 402},
    })
    disposition = _disposition(tmp_path, _stream(_text(_TOOL_CALL_TEXT), error))
    assert disposition["provider_code"] != _REASON
    assert disposition["cause"] != tfc.CAUSE_PROVIDER_TRANSIENT


def test_a_tool_part_outside_the_tail_window_still_clears_it(tmp_path: Path) -> None:
    padding = ("x" * 1000 + "\n") * (tfc.MAX_TAIL_READ_BYTES // 1000 + 2)
    stdout = _tool_use() + "\n" + padding + _text(_TOOL_CALL_TEXT) + "\n"
    assert len(stdout.encode("utf-8")) > tfc.MAX_TAIL_READ_BYTES
    assert _disposition(tmp_path, stdout)["provider_code"] != _REASON


def test_a_long_stream_with_no_tool_part_anywhere_is_still_classified(tmp_path: Path) -> None:
    padding = ("x" * 1000 + "\n") * (tfc.MAX_TAIL_READ_BYTES // 1000 + 2)
    stdout = padding + _text(_TOOL_CALL_TEXT) + "\n"
    assert _disposition(tmp_path, stdout)["provider_code"] == _REASON


def test_whole_stream_scan_is_bounded_per_line_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "stdout.log"
    path.write_text(_stream(_text(_TOOL_CALL_TEXT)), encoding="utf-8")
    assert tfc.opencode_stream_has_tool_part(path) is False
    path.write_text(_stream(_text(_TOOL_CALL_TEXT), _tool_use()), encoding="utf-8")
    assert tfc.opencode_stream_has_tool_part(path) is True
    monkeypatch.setattr(tfc, "MAX_OPENCODE_SCAN_LINE_BYTES", 64)
    path.write_text(_stream(_text(_TOOL_CALL_TEXT)), encoding="utf-8")
    assert tfc.opencode_stream_has_tool_part(path) is True
    assert tfc.opencode_stream_has_tool_part(tmp_path / "missing.log") is True


def test_the_reason_is_registered_and_placed() -> None:
    assert tfc.PROVIDER_TEXT_TOOL_CALL_PROTOCOL == _REASON
    assert _REASON not in tfc.unclassified_reason_constants()
    assert tfc.unclassified_reason_constants() == ()
    assert tfc.REASON_DISPOSITION[_REASON] == tfc.FAILURE_CLASS_TRANSIENT
    assert tfc.PROVIDER_CODE_DISPOSITION[_REASON] == tfc.FAILURE_CLASS_TRANSIENT
    assert tfc.recognised_reason(_REASON) is not None


def test_the_reason_alone_selects_the_same_distinct_route_retry() -> None:
    disposition = tfc.failure_disposition(reason=_REASON, terminal_state="worker_failed")
    assert disposition["cause"] == tfc.CAUSE_PROVIDER_TRANSIENT
    assert disposition["action"] == tfc.ACTION_ROUTE_RETRY
    assert disposition["retry_scope"] == tfc.RETRY_SCOPE_DISTINCT_ROUTE


def test_a_tool_part_past_the_signal_line_cap_still_clears_it(tmp_path: Path) -> None:
    padding = "x\n" * (tfc._MAX_SIGNAL_LINES + 10)
    stdout = _text(_TOOL_CALL_TEXT) + "\n" + padding + _tool_use() + "\n"
    assert len(stdout.encode("utf-8")) < tfc.MAX_TAIL_READ_BYTES
    assert _disposition(tmp_path, stdout)["provider_code"] != _REASON


@pytest.mark.parametrize(
    "messages",
    [
        [{"parts": [{"type": "text", "text": "hi"}]}] * 64 + [{"parts": [{"type": "tool"}]}],
        [{"parts": [{"type": "text", "text": "hi"}] * 64 + [{"type": "tool"}]}],
    ],
)
def test_a_tool_part_past_the_message_caps_still_clears_it(messages: list[dict[str, Any]]) -> None:
    listing = _compact({"type": "session.messages", "sessionID": "ses_1", "messages": messages})
    assert tfc.provider_terminal_signal(_stream(_text(_TOOL_CALL_TEXT), listing))["code"] != _REASON
