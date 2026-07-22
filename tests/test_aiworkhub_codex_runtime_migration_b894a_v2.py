from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXTENSION_JS = ROOT / "vscode-extension" / "extension.js"

# B894a: selective Codex config.toml PYTHONPATH runtime migration. The
# migration logic lives entirely in the VS Code extension host (extension.js
# is JavaScript, not Python) -- there is no separate Python implementation to
# import. This test drives the real extension.js pure text-transform
# function (`__testInternals.migrateCodexConfigTomlText`) through a bounded
# Node subprocess, the same way vscode-extension/test/*.test.js exercises it
# in-process, so the behavior verified here is the exact code the extension
# ships (never a reimplementation/duplicate that could drift).

NODE_DRIVER = r"""
"use strict";
const Module = require("module");

const extensionPath = process.argv[2];
const originalText = process.argv[3];
const newRuntimeDir = process.argv[4];

const fakeVscode = {
  workspace: {
    workspaceFolders: [],
    getConfiguration: () => ({ get: () => "", inspect: () => ({}), update: async () => {} }),
    registerWebviewPanelSerializer: () => ({ dispose: () => {} }),
    registerWebviewViewProvider: () => ({ dispose: () => {} }),
  },
  window: {
    createOutputChannel: () => ({ appendLine: () => {}, dispose: () => {} }),
    setStatusBarMessage: () => {},
    showErrorMessage: () => {},
    showInformationMessage: () => {},
    showQuickPick: async () => undefined,
  },
  commands: { registerCommand: () => ({ dispose: () => {} }) },
  Uri: { joinPath: (...parts) => ({ fsPath: parts.map((p) => p.fsPath || p).join("/") }) },
  ViewColumn: { Active: 1 },
  ConfigurationTarget: { Global: 1 },
};
const originalLoad = Module._load;
Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") return fakeVscode;
  return originalLoad.call(this, request, parent, isMain);
};
let extensionModule;
try {
  delete require.cache[require.resolve(extensionPath)];
  extensionModule = require(extensionPath);
} finally {
  Module._load = originalLoad;
}

const { migrateCodexConfigTomlText, resolveCodexConfigTomlPath, splitCodexPythonPathValue } =
  extensionModule.__testInternals;

const mode = process.argv[5] || "migrate";
if (mode === "resolve_path") {
  const env = JSON.parse(process.argv[6] || "{}");
  // os.homedir() (used internally as the CODEX_HOME-absent fallback) reads
  // the real process environment, not the `env` argument object -- apply
  // the requested overrides to this child process's own env first so the
  // fallback path is deterministic under test.
  if (Object.prototype.hasOwnProperty.call(env, "HOME")) process.env.HOME = env.HOME;
  if (Object.prototype.hasOwnProperty.call(env, "USERPROFILE")) process.env.USERPROFILE = env.USERPROFILE;
  process.stdout.write(JSON.stringify({ path: resolveCodexConfigTomlPath(env) }));
} else if (mode === "split") {
  process.stdout.write(JSON.stringify({ parts: splitCodexPythonPathValue(originalText) }));
} else {
  const result = migrateCodexConfigTomlText(originalText, newRuntimeDir);
  process.stdout.write(JSON.stringify(result));
}
"""


def _require_node() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node executable not available in this environment")
    return node


def run_migration(tmp_path: Path, original_text: str, new_runtime_dir: str) -> dict:
    node = _require_node()
    driver = tmp_path / "driver.js"
    driver.write_text(NODE_DRIVER, encoding="utf8")
    proc = subprocess.run(
        [node, str(driver), str(EXTENSION_JS), original_text, new_runtime_dir, "migrate"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
    return json.loads(proc.stdout)


def run_split(tmp_path: Path, value: str) -> list:
    node = _require_node()
    driver = tmp_path / "driver_split.js"
    driver.write_text(NODE_DRIVER, encoding="utf8")
    proc = subprocess.run(
        [node, str(driver), str(EXTENSION_JS), value, "", "split"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
    return json.loads(proc.stdout)["parts"]


def run_resolve_path(tmp_path: Path, env: dict) -> str:
    node = _require_node()
    driver = tmp_path / "driver_resolve.js"
    driver.write_text(NODE_DRIVER, encoding="utf8")
    proc = subprocess.run(
        [node, str(driver), str(EXTENSION_JS), "", "", "resolve_path", json.dumps(env)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
    return json.loads(proc.stdout)["path"]


OLD_RUNTIME_LINUX = "/home/dev/.vscode-server/extensions/publisher.aiworkhub-0.6.19/runtime"
NEW_RUNTIME_LINUX = "/home/dev/.vscode-server/extensions/publisher.aiworkhub-0.6.20/runtime"
OLD_RUNTIME_MACOS = "/Users/dev/.vscode/extensions/publisher.aiworkhub-0.6.19/runtime"
NEW_RUNTIME_MACOS = "/Users/dev/.vscode/extensions/publisher.aiworkhub-0.6.20/runtime"
NEW_RUNTIME_WINDOWS = "C:\\Users\\dev\\.vscode\\extensions\\publisher.aiworkhub-0.6.20\\runtime"
# TOML basic-string on-disk (escaped) form of a Windows path: each backslash
# doubled, exactly as a real config.toml would store it.
OLD_RUNTIME_WINDOWS_ESCAPED = "C:\\\\Users\\\\dev\\\\.vscode\\\\extensions\\\\publisher.aiworkhub-0.6.19\\\\runtime"


def test_extension_js_exists_and_defines_migration_internals():
    assert EXTENSION_JS.exists(), "vscode-extension/extension.js must exist"
    text = EXTENSION_JS.read_text(encoding="utf8")
    for symbol in [
        "migrateCodexConfigTomlText",
        "resolveCodexConfigTomlPath",
        "migrateCodexConfigTomlRuntimePath",
        "CODEX_OWNED_RUNTIME_SEGMENT_RE",
    ]:
        assert symbol in text, f"extension.js must define {symbol}"


def test_real_aiworkhub_and_ultrafast_blocks_both_migrate_distinct_repo_env(tmp_path):
    original = "\n".join(
        [
            "[mcp_servers.AIWorkHub]",
            'command = "python3"',
            'args = ["-m", "aiworkhub.server"]',
            "",
            "[mcp_servers.AIWorkHub.env]",
            f'PYTHONPATH = "{OLD_RUNTIME_LINUX}"',
            'AIWORKHUB_REPO = "/repo/one"',
            'AIWORKHUB_REPO_ROOT = "/repo/one"',
            'AIWORKHUB_REPO_ID = "repo_aaaa"',
            "",
            "[mcp_servers.AIWorkHub_Ultrafast]",
            'command = "python3"',
            'args = ["-m", "aiworkhub.server"]',
            "",
            "[mcp_servers.AIWorkHub_Ultrafast.env]",
            f'PYTHONPATH = "{OLD_RUNTIME_LINUX}"',
            'AIWORKHUB_REPO = "/repo/two"',
            'AIWORKHUB_REPO_ID = "repo_bbbb"',
            "",
        ]
    )
    result = run_migration(tmp_path, original, NEW_RUNTIME_LINUX)
    assert result["changed"] is True
    assert sorted(result["migrated"]) == ["AIWorkHub", "AIWorkHub_Ultrafast"]
    content = result["content"]
    assert OLD_RUNTIME_LINUX not in content
    assert content.count(f'PYTHONPATH = "{NEW_RUNTIME_LINUX}"') == 2
    # Repo identity for both blocks is retained exactly -- never invented or
    # overwritten by the migration.
    assert 'AIWORKHUB_REPO = "/repo/one"' in content
    assert 'AIWORKHUB_REPO_ROOT = "/repo/one"' in content
    assert 'AIWORKHUB_REPO_ID = "repo_aaaa"' in content
    assert 'AIWORKHUB_REPO = "/repo/two"' in content
    assert 'AIWORKHUB_REPO_ID = "repo_bbbb"' in content


def test_mixed_case_custom_table_name_preserved_exactly(tmp_path):
    original = "\n".join(
        [
            "[mcp_servers.MyAiWorkHubCustom]",
            'command = "python3"',
            'args = ["-m", "aiworkhub.server"]',
            "",
            "[mcp_servers.MyAiWorkHubCustom.env]",
            f'PYTHONPATH = "{OLD_RUNTIME_MACOS}"',
            "",
        ]
    )
    result = run_migration(tmp_path, original, NEW_RUNTIME_MACOS)
    assert result["migrated"] == ["MyAiWorkHubCustom"]
    assert "[mcp_servers.MyAiWorkHubCustom]" in result["content"]
    assert "[mcp_servers.MyAiWorkHubCustom.env]" in result["content"]
    assert f'PYTHONPATH = "{NEW_RUNTIME_MACOS}"' in result["content"]


def test_windows_path_form_and_crlf_line_endings_preserved(tmp_path):
    original = "\r\n".join(
        [
            "[mcp_servers.AIWorkHub]",
            'command = "py"',
            'args = ["-m", "aiworkhub.server"]',
            "",
            "[mcp_servers.AIWorkHub.env]",
            f'PYTHONPATH = "{OLD_RUNTIME_WINDOWS_ESCAPED}"',
            'AIWORKHUB_REPO = "C:\\\\repo"',
            "",
        ]
    )
    assert "\r\n" in original
    result = run_migration(tmp_path, original, NEW_RUNTIME_WINDOWS)
    assert result["changed"] is True
    assert result["migrated"] == ["AIWorkHub"]
    content = result["content"]
    assert content.count("\r\n") == original.count("\r\n"), "CRLF endings must be preserved exactly"
    assert 'AIWORKHUB_REPO = "C:\\\\repo"' in content


def test_custom_and_unrelated_blocks_stay_byte_identical(tmp_path):
    custom_block = "\n".join(
        [
            "[mcp_servers.SomeOtherTool]",
            'command = "node"',
            'args = ["-y", "some-other-mcp-server"]',
            "",
            "[mcp_servers.SomeOtherTool.env]",
            'PYTHONPATH = "/opt/my/custom/pythonpath"',
            "",
        ]
    )
    result = run_migration(tmp_path, custom_block, NEW_RUNTIME_LINUX)
    assert result["changed"] is False
    assert result["content"] == custom_block

    not_installed_yet = "\n".join(
        [
            "[mcp_servers.AIWorkHub]",
            'command = "python3"',
            'args = ["-m", "aiworkhub.server"]',
            "",
            "[mcp_servers.AIWorkHub.env]",
            'PYTHONPATH = "/opt/my/custom/pythonpath"',
            "",
        ]
    )
    result2 = run_migration(tmp_path, not_installed_yet, NEW_RUNTIME_LINUX)
    assert result2["changed"] is False
    assert result2["content"] == not_installed_yet


def test_mixed_file_only_owned_block_pythonpath_bytes_change(tmp_path):
    mixed = "\n".join(
        [
            "# hand-written custom entry -- never touched",
            "[mcp_servers.Handwritten]",
            'command = "/usr/local/bin/my-tool"',
            'args = ["--flag"]',
            "",
            "[mcp_servers.Handwritten.env]",
            'PYTHONPATH = "/opt/my/custom/pythonpath"',
            'SOME_OTHER_VAR = "keep-me"',
            "",
            "[mcp_servers.AIWorkHub]",
            'command = "python3"',
            'args = ["-m", "aiworkhub.server"]',
            "",
            "[mcp_servers.AIWorkHub.env]",
            f'PYTHONPATH = "{OLD_RUNTIME_LINUX}"',
            'AIWORKHUB_REPO_ID = "repo_cccc"',
            "",
        ]
    )
    result = run_migration(tmp_path, mixed, NEW_RUNTIME_LINUX)
    assert result["migrated"] == ["AIWorkHub"]
    content = result["content"]
    assert 'PYTHONPATH = "/opt/my/custom/pythonpath"' in content
    assert 'SOME_OTHER_VAR = "keep-me"' in content
    assert "# hand-written custom entry -- never touched" in content
    assert f'PYTHONPATH = "{NEW_RUNTIME_LINUX}"' in content

    original_lines = mixed.split("\n")
    migrated_lines = content.split("\n")
    assert len(original_lines) == len(migrated_lines)
    for orig_line, new_line in zip(original_lines, migrated_lines):
        if orig_line == new_line:
            continue
        assert "PYTHONPATH =" in orig_line, f"unexpected drift: {orig_line!r} -> {new_line!r}"


def test_idempotent_when_already_current(tmp_path):
    already_current = "\n".join(
        [
            "[mcp_servers.AIWorkHub]",
            'command = "python3"',
            'args = ["-m", "aiworkhub.server"]',
            "",
            "[mcp_servers.AIWorkHub.env]",
            f'PYTHONPATH = "{NEW_RUNTIME_LINUX}"',
            "",
        ]
    )
    result = run_migration(tmp_path, already_current, NEW_RUNTIME_LINUX)
    assert result["changed"] is False
    assert result["content"] == already_current


def test_split_never_misplits_windows_drive_letter_colon(tmp_path):
    assert run_split(tmp_path, "C:\\Users\\dev\\runtime") == ["C:\\Users\\dev\\runtime"]
    assert run_split(tmp_path, "C:\\a\\runtime;C:\\b\\extra") == ["C:\\a\\runtime", "C:\\b\\extra"]
    assert run_split(tmp_path, "/a/runtime:/b/extra") == ["/a/runtime", "/b/extra"]


def test_resolve_codex_config_toml_path_uses_codex_home_and_never_a_global_fallback(tmp_path):
    codex_home = str(tmp_path / "custom_codex_home")
    resolved = run_resolve_path(tmp_path, {"CODEX_HOME": codex_home})
    assert resolved == str(Path(codex_home) / "config.toml")

    home_dir = str(tmp_path / "fake_home")
    resolved_default = run_resolve_path(tmp_path, {"HOME": home_dir, "USERPROFILE": home_dir})
    assert resolved_default.endswith(str(Path(".codex") / "config.toml"))
    # Never a hardcoded/global repo-relative fallback: the resolved path must
    # be rooted under the (repo-independent) home/CODEX_HOME directory only.
    assert "geoai" not in resolved_default.lower()


def test_no_process_kill_or_shell_true_in_migration_feature():
    text = EXTENSION_JS.read_text(encoding="utf8")
    feature_block_start = text.index("Codex config.toml PYTHONPATH runtime migration (B894a)")
    end = text.index("function installedExtensionVersion")
    feature_block = text[feature_block_start:end]
    assert "shell: true" not in feature_block
    assert "shell:true" not in feature_block
    assert ".kill(" not in feature_block
    assert "childProcess.spawn" not in feature_block


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
