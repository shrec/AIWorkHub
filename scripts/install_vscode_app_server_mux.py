#!/usr/bin/env python3
"""Dry-run/check/print-config installer for the App Server mux (B409).

This script NEVER mutates VS Code settings, the installed OpenAI extension,
systemd, the live callback DB, or any running process. It only prints:

- the exact ``chatgpt.cliExecutable`` value Codex should apply,
- an environment-readiness check (module present, real ``codex`` on PATH),
- and the exact rollback value/steps.

Codex owns applying the setting, reloading the VS Code extension host, the
live canary against the extension-owned thread, recovery, and finally
enabling ``aiworkhub-callback-bridge.service`` -- none of that happens here.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MUX_MODULE_PATH = REPO_ROOT / "src" / "aiworkhub" / "app_server_mux.py"
CONSOLE_SCRIPT_NAME = "aiworkhub-app-server-mux"
DEFAULT_CONSOLE_SCRIPT = REPO_ROOT / "scripts" / CONSOLE_SCRIPT_NAME
SETTING_KEY = "chatgpt.cliExecutable"
ENV_REAL_EXECUTABLE = "AIWORKHUB_APP_SERVER_MUX_REAL_EXECUTABLE"


def find_real_codex_executable() -> str | None:
    bundled = list(
        (Path.home() / ".vscode-server" / "extensions").glob(
            "openai.chatgpt-*/bin/linux-x86_64/codex"
        )
    )
    executable = [p for p in bundled if p.is_file() and os.access(p, os.X_OK)]
    if executable:
        return str(max(executable, key=lambda p: p.stat().st_mtime_ns))
    return shutil.which("codex")


def find_console_script() -> str | None:
    return shutil.which(CONSOLE_SCRIPT_NAME)


def build_wrapper_command(wrapper_executable: str | None = None) -> str:
    """Return the single executable path required by VS Code's schema."""
    return wrapper_executable or find_console_script() or str(DEFAULT_CONSOLE_SCRIPT)


def build_settings_snippet(wrapper_executable: str | None = None) -> dict:
    return {SETTING_KEY: build_wrapper_command(wrapper_executable)}


def build_rollback_snippet() -> dict:
    real = find_real_codex_executable() or "codex"
    return {SETTING_KEY: real}


def check_environment() -> dict:
    real_codex = find_real_codex_executable()
    console_script = find_console_script()
    return {
        "mux_module_exists": MUX_MODULE_PATH.is_file(),
        "mux_module_path": str(MUX_MODULE_PATH),
        "console_script_found_on_path": console_script,
        "configured_wrapper_path": build_wrapper_command(),
        "real_codex_found_on_path": real_codex,
        "python_executable": sys.executable,
        "env_real_executable_override": os.environ.get(ENV_REAL_EXECUTABLE, ""),
        "ready_for_codex_to_apply": bool(
            MUX_MODULE_PATH.is_file()
            and real_codex
            and Path(build_wrapper_command()).is_file()
            and os.access(build_wrapper_command(), os.X_OK)
        ),
    }


def render_report(wrapper_executable: str | None = None) -> dict:
    return {
        "mode": "dry_run_print_config_only",
        "no_mutation_performed": True,
        "check": check_environment(),
        "apply_setting": build_settings_snippet(wrapper_executable),
        "real_executable_pin": real_executable_pin(),
        "rollback_setting": build_rollback_snippet(),
        "manual_apply_instructions": [
            f"Set VS Code user/workspace setting `{SETTING_KEY}` to the "
            "`apply_setting` value above (Codex applies this, not this script).",
            "Ensure the real Codex binary stays reachable on PATH, or set "
            f"`{ENV_REAL_EXECUTABLE}` in the mux's own process environment "
            "to an explicit path.",
            "Reload/restart the VS Code OpenAI extension host so it re-reads "
            "the new `chatgpt.cliExecutable` value and spawns its App Server "
            "child through the mux.",
            "Run the Codex-owned live canary against the extension-owned "
            "thread before enabling aiworkhub-callback-bridge.service.",
        ],
        "rollback_instructions": [
            f"Set `{SETTING_KEY}` back to the `rollback_setting` value above "
            "(the bare real Codex binary) and reload the extension host.",
            "The callback bridge service remains disabled throughout this "
            "installer's scope -- no service action is required for rollback.",
        ],
    }


def real_executable_pin() -> dict:
    real = find_real_codex_executable() or "codex"
    return {
        "path": str(Path.home() / ".aiworkhub" / "app_server_mux" / "real_executable"),
        "value": real,
        "required_mode": "0600",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Print environment readiness only (no apply/rollback snippet).",
    )
    parser.add_argument(
        "--print-config", action="store_true",
        help="Print the full apply/rollback report (default action).",
    )
    parser.add_argument(
        "--wrapper-executable", default=None,
        help="Override the single executable path used in the printed VS Code setting.",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.check and not args.print_config:
        print(json.dumps(check_environment(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(json.dumps(render_report(args.wrapper_executable), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
