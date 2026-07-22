"""B893: reloadless, repository-isolated MCP runtime repair.

Verifies tools/geoai-task-mcp/vscode-extension/extension.js no longer
requires "Developer: Reload Window" after a runtime/VSIX version mismatch --
it instead runs one bounded restart of the *same* repository's own MCP
child and reconnects the already-open dashboard tab automatically. The
deep behavioral coverage (bounded restart, automatic reconnect, degraded-
with-reason on a failed repair, cross-repository isolation, and
deterministic Linux/Windows/macOS `findPythonCommand` branches) lives in
test/reloadless-runtime-repair.test.js, which is real Node.js code
exercising the real extension.js -- this file runs it via subprocess and
adds static-content guardrails for the contract's forbidden patterns.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

VSCODE_EXT_DIR = Path(__file__).resolve().parents[1] / "vscode-extension"
EXTENSION_JS = VSCODE_EXT_DIR / "extension.js"
RELOADLESS_TEST_JS = VSCODE_EXT_DIR / "test" / "reloadless-runtime-repair.test.js"
MULTIREPO_TEST_JS = VSCODE_EXT_DIR / "test" / "multirepo-connecting.test.js"


def _read_extension_js() -> str:
    return EXTENSION_JS.read_text(encoding="utf-8")


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


def test_manual_reload_instruction_is_never_emitted() -> None:
    # forbidden: manual_reload_requirement -- the extension must never tell
    # the user to reload the window, and must never hardcode reloadRequired
    # to true anywhere in the runtime-info path.
    ext = _read_extension_js()
    assert "Developer: Reload Window" not in ext
    assert "reloadRequired: true" not in ext
    assert "reloadRequired = true" not in ext
    # The payload builder must be a single, always-false source of truth.
    assert "reloadRequired: false," in ext


def test_bounded_reloadless_repair_wiring_present() -> None:
    ext = _read_extension_js()
    for marker in [
        "MCP_MAX_RUNTIME_REPAIR_ATTEMPTS",
        "async attemptRuntimeRepair(reason)",
        "runtime_repair_budget_exhausted",
        "runtimeRepairAttempts",
        "async function pushRuntimeInfo(view)",
        "async function checkRuntimeHealth(client)",
    ]:
        assert marker in ext, f"missing reloadless-repair wiring marker: {marker}"
    # The already-open dashboard tab must reconnect on its own after a
    # successful repair -- a fresh snapshot push, not a user-facing prompt.
    assert "await pushSnapshot(view);" in ext


def test_forbidden_fallback_and_shell_patterns_absent() -> None:
    ext = _read_extension_js()
    # forbidden: global_repo_fallback / geoai_hardcoded_path / cross_repo_callback_or_queue
    assert "GeoAI" not in ext
    assert "/home/shrek" not in ext
    assert "H:/Dev" not in ext
    # A user-home lookup is legitimate for Codex's own ~/.codex/config.toml;
    # what remains forbidden is deriving a repository binding/state root from
    # the home directory (the cross-repo fallback fixed by B893).
    assert 'AIWORKHUB_REPO_ROOT: os.homedir()' not in ext
    assert 'path.join(os.homedir(), ".aiworkhub")' not in ext
    # forbidden: shell_true -- child_process.spawn must never run through a shell.
    assert "shell: true" not in ext
    assert "shell:true" not in ext
    # forbidden: browser_dashboard_reintroduction
    assert "openExternal" not in ext
    assert "simpleBrowser" not in ext
    # forbidden: git_commit_or_push -- this extension never shells out to git.
    assert "git commit" not in ext
    assert "git push" not in ext
    # Repair restarts the SAME client -- there is exactly one childProcess.spawn
    # call site in the whole module, so a restart can only ever respawn the
    # repository it was already bound to.
    assert ext.count("childProcess.spawn(") == 1


def test_repo_isolated_child_env_wiring_unchanged() -> None:
    # Repair must reuse the existing per-repository identity plumbing --
    # never introduce a second, host-global spawn path.
    ext = _read_extension_js()
    for marker in [
        "AIWORKHUB_REPO_ROOT",
        "AIWORKHUB_REPO_ID",
        "AIWORKHUB_WINDOW_ID",
        "AIWORKHUB_CLAIM_EPISODE",
        'path.join(root, ".aiworkhub", "runtime")',
    ]:
        assert marker in ext


def test_terminal_review_semantics_from_b891_untouched() -> None:
    # No change to terminal-review semantics from B891: the Live Output /
    # task-detail result-and-validation wiring must still be present exactly
    # as before -- this task only touches runtime-mismatch handling.
    ext = _read_extension_js()
    for marker in [
        "async function pushTaskDetail(view, taskId)",
        "async function pushLiveOutput(view, taskId, cursor)",
        'liveOutput: "aiworkhub_dashboard_task_live_output"',
        "Raw provider output",
        "detail-live-output-block",
        "Result and validation",
    ]:
        assert marker in ext, f"terminal-review marker missing/changed: {marker}"


def test_platform_python_resolution_branches_present() -> None:
    # Linux/macOS/Windows path+process branches must exist deterministically
    # (exercised for real, per-platform, in the Node regression below).
    ext = _read_extension_js()
    for marker in [
        'path.join(root, ".venv", "Scripts", "python.exe")',
        'path.join(root, ".venv", "bin", "python3")',
        '{ command: "py", argsPrefix: ["-3"] }',
        '{ command: "python3", argsPrefix: [] }',
        'process.platform === "win32"',
    ]:
        assert marker in ext


def test_reloadless_runtime_repair_node_regression_passes() -> None:
    assert RELOADLESS_TEST_JS.exists(), "test/reloadless-runtime-repair.test.js is required output for B893"
    result = _run_node(RELOADLESS_TEST_JS)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "reloadless runtime-repair regression passed" in result.stdout


def test_multirepo_isolation_node_regression_still_passes() -> None:
    # Two simultaneous workspaces must still bind to distinct canonical repo
    # identities/children after this change -- unchanged pre-existing
    # coverage, run here so a B893 regression in shared code paths is caught.
    result = _run_node(MULTIREPO_TEST_JS)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "two-repo Connecting regression passed" in result.stdout


def test_eval_evidence_file_is_valid_json() -> None:
    eval_path = (
        Path(__file__).resolve().parents[1]
        / "eval"
        / "aiworkhub_vscode_reloadless_runtime_b893_v2.json"
    )
    payload = json.loads(eval_path.read_text(encoding="utf-8"))
    assert payload["task_id"] == "CLAUDE_SONNET5_AIWORKHUB_RELOADLESS_MULTIREPO_RUNTIME_B893_V2"
    assert "extension.js" in " ".join(payload["changed_paths"])


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", str(Path(__file__))]))
