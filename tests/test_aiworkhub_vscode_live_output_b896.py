"""B896: normalize Claude CLI stream_event/content_block_delta signature_delta
in the VS Code dashboard Live Output structured-event formatter.

The actual formatter (timelineEventFromObject / claudeStreamContentBlockDelta)
lives in vscode-extension/media/app.js -- the Webview-side renderer for the
liveOutput message extension.js already forwards unchanged (see
extension.js::pushLiveOutput). The deep behavioral coverage (signature_delta
dropped from the feed with zero timeline rows, text_delta still rendering as
readable text, adjacent result/error rows unaffected, newest-first ordering
preserved) lives in test/live-output-formatting.test.js, which is real
Node.js code exercising the real app.js -- this file runs it via subprocess
and adds static-content guardrails for the contract.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

VSCODE_EXT_DIR = Path(__file__).resolve().parents[1] / "vscode-extension"
EXTENSION_JS = VSCODE_EXT_DIR / "extension.js"
APP_JS = VSCODE_EXT_DIR / "media" / "app.js"
LIVE_OUTPUT_TEST_JS = VSCODE_EXT_DIR / "test" / "live-output-formatting.test.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _run_node(script: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["node", str(script)],
        cwd=str(VSCODE_EXT_DIR),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_extension_js_syntax_is_valid() -> None:
    result = subprocess.run(
        ["node", "--check", str(EXTENSION_JS)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_app_js_syntax_is_valid() -> None:
    result = subprocess.run(
        ["node", "--check", str(APP_JS)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_signature_delta_recognition_wiring_present() -> None:
    app = _read(APP_JS)
    for marker in [
        "function claudeStreamContentBlockDelta(event)",
        'event.type !== "stream_event"',
        'inner.type !== "content_block_delta"',
        'streamDelta.type === "signature_delta"',
        "return null;",
        'streamDelta.type === "text_delta"',
    ]:
        assert marker in app, f"missing signature_delta wiring marker: {marker}"


def test_null_timeline_events_are_skipped_not_pushed() -> None:
    app = _read(APP_JS)
    # The dropped-event sentinel (null) must be filtered before it reaches
    # the rendered timeline array, not just returned and ignored downstream.
    assert "const timelineEvent = timelineEventFromObject(parsedEvent, parsedEvent);" in app
    assert "if (timelineEvent) {" in app


def test_live_output_message_contract_unchanged() -> None:
    # No regression to the existing requestLiveOutput/liveOutput host-side
    # wiring -- this task only touches the Webview-side formatter.
    ext = _read(EXTENSION_JS)
    for marker in [
        "async function pushLiveOutput(view, taskId, cursor)",
        'liveOutput: "aiworkhub_dashboard_task_live_output"',
        '"requestLiveOutput"',
    ]:
        assert marker in ext, f"live output wiring marker missing/changed: {marker}"


def test_raw_provider_output_affordance_still_present() -> None:
    # The bounded, explicit "Raw provider output" details toggle (the
    # diagnostics affordance signature_delta payloads remain reachable
    # through) must still exist and be untouched.
    app = _read(APP_JS)
    assert "detailLiveOutputRawContent.textContent = decoded;" in app
    assert "Raw provider output" in _read(VSCODE_EXT_DIR / "extension.js")


def test_newest_first_ordering_unchanged() -> None:
    app = _read(APP_JS)
    assert "for (const event of events.slice(-200).reverse())" in app


def test_live_output_formatting_node_regression_passes() -> None:
    assert LIVE_OUTPUT_TEST_JS.exists(), "test/live-output-formatting.test.js is required for B896"
    result = _run_node(LIVE_OUTPUT_TEST_JS)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "live-output-formatting.test.js: ok" in result.stdout


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", str(Path(__file__))]))
