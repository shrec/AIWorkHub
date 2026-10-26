"""NF-2026-01685: a mid-run OAuth expiry takes the single bounded auth retry.

The measured stream carries the structured ``authentication_failed`` code only on
the provider-owned assistant API-error event; the terminal ``result`` event has a
null status and prose.  The detector must keep that code and the session, and the
classifier must accept the status-less shape, without ever trusting prose.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import claude_auth, process_launcher  # noqa: E402
from test_claude_auth_relaunch_nf01354 import (  # noqa: E402
    _metadata,
    _refreshable,
    _retry,
    _wait_exit,
)
from test_claude_cli_prompt_delivery_nf01159 import (  # noqa: E402
    _PROMPT,
    _harness,
    _launch,
    _reap,
    _script,
    _wait_for,
)

_SESSION = "242f0000-session-nf01685"

_ASSISTANT = {
    "type": "assistant",
    "error": "authentication_failed",
    "is_api_error_message": True,
    "session_id": _SESSION,
}
_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "terminal_reason": "api_error",
    "api_error_status": None,
    "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
    "session_id": _SESSION,
}


def _stream(*events: dict) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def _failure_from(tmp_path: Path, *events: dict) -> dict | None:
    log = tmp_path / "stdout.log"
    log.write_text(_stream(*events), encoding="utf-8")
    return process_launcher._provider_auth_failure_from_output(log)


def test_measured_stream_yields_authentication_failed_with_session(tmp_path: Path) -> None:
    failure = _failure_from(tmp_path, _ASSISTANT, _RESULT)

    assert failure is not None
    assert failure["schema_id"] == "aiworkhub.provider_launch_failure.v1"
    assert failure["error_code"] == "authentication_failed"
    assert failure["session_id"] == _SESSION
    assert failure["http_status"] == 0
    assert (
        claude_auth.classify_runtime_auth_failure(
            http_status=failure["http_status"],
            error_code=failure["error_code"],
            session_id=failure["session_id"],
        )
        is not None
    )


def test_classifier_accepts_status_zero_only_with_code_and_session() -> None:
    accepted = claude_auth.classify_runtime_auth_failure(
        http_status=0, error_code="authentication_failed", session_id="s"
    )
    assert accepted is not None and accepted["http_status"] == 0
    unchanged = claude_auth.classify_runtime_auth_failure(
        http_status=401, error_code="authentication_failed", session_id="s"
    )
    assert unchanged is not None and unchanged["http_status"] == 401

    for kwargs in (
        {"http_status": 403, "error_code": "authentication_failed", "session_id": "s"},
        {"http_status": 500, "error_code": "authentication_failed", "session_id": "s"},
        {"http_status": False, "error_code": "authentication_failed", "session_id": "s"},
        {"http_status": 0, "error_code": "authentication_failed", "session_id": ""},
        {"http_status": 0, "error_code": "authentication_failed", "session_id": None},
        {"http_status": 0, "error_code": "rate_limit", "session_id": "s"},
    ):
        assert claude_auth.classify_runtime_auth_failure(**kwargs) is None, kwargs


def test_prose_only_and_non_api_error_events_never_qualify(tmp_path: Path) -> None:
    assert _failure_from(tmp_path, _RESULT) is None
    assert _failure_from(tmp_path, {**_ASSISTANT, "is_api_error_message": False}) is None
    no_flag = {k: v for k, v in _ASSISTANT.items() if k != "is_api_error_message"}
    assert _failure_from(tmp_path, no_flag) is None
    prose = {
        "type": "assistant",
        "is_api_error_message": True,
        "message": {"content": [{"type": "text", "text": "authentication_failed"}]},
        "session_id": _SESSION,
    }
    assert _failure_from(tmp_path, prose, _RESULT) is None


_MEASURED_CLAUDE = f"""\
import pathlib
import sys

record = pathlib.Path(sys.argv[1])
sys.stdin.buffer.read()
first = not record.with_suffix(".1").exists()
if first:
    print({json.dumps(json.dumps(_ASSISTANT))}, flush=True)
    print({json.dumps(json.dumps(_RESULT))}, flush=True)
record.with_suffix(".1" if first else ".2").write_text("x", encoding="utf-8")
sys.exit(1 if first else 0)
"""


def test_auth_retry_fires_exactly_once_for_the_measured_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = tmp_path / "claude-stdin"
    child = _script(tmp_path, "measured_claude.py", _MEASURED_CLAUDE)
    harness = _harness(
        monkeypatch,
        tmp_path,
        child_argv=[sys.executable, str(child), str(record)],
        stdin_text=_PROMPT,
    )
    try:
        assert _launch(harness)["ok"] is True
        assert _wait_for(record.with_suffix(".1"))
        _wait_exit(harness)
        metadata = _metadata(harness)
        failure = process_launcher._provider_auth_failure_from_output(
            Path(metadata["stdout_path"])
        )
        assert failure is not None and failure["error_code"] == "authentication_failed"
        _refreshable(monkeypatch)

        event = _retry(harness, metadata, failure)

        assert event is not None and event["state"] == "running", event
        assert _wait_for(record.with_suffix(".2"))
        _wait_exit(harness)
        persisted = _metadata(harness)
        assert persisted["claude_auth_retry_count"] == 1
        assert _retry(harness, persisted, failure) is None
        assert len(harness.manager.popen_calls) == 2
    finally:
        _reap(harness)
    assert len(harness.manager.popen_calls) == 2
