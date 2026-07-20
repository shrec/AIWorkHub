#!/usr/bin/env python3
"""Smoke test: DRYRUN-ONLY CLI adapter contract (B105 v1).

Proves:
  * launch is impossible / not implemented -- no env flag can enable it
  * only allowlisted commands validate; everything else is blocked
  * env VALUES (incl. secrets) are redacted in audit/plan output
  * the write gate is OFF by default; parent task queue is not mutated
  * the module contains NO process-launch/exec/shell code

Isolation: writes only to a per-test mktemp audit path; honors GEOAI_REPO /
GEOAI_TASK_MCP_AUDIT_LOG_PATH env overrides; mutates no shared artifact. Safe
under parallel execution.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Bootstrap import path for standalone runs (shell also exports PYTHONPATH).
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Patterns that would indicate a real process launch. None may appear in the
# adapter module source.
_FORBIDDEN_SOURCE_PATTERNS = (
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "import subprocess",
    "os.system",
    "os.popen",
    "os.exec",
    "os.fork",
    "os.spawn",
    "Popen(",
    "shell=True",
    "pty.spawn",
)


def _repo_root() -> Path:
    return Path(os.environ.get("GEOAI_REPO", str(Path(__file__).resolve().parents[3])))


def main() -> int:
    repo = _repo_root()

    with tempfile.TemporaryDirectory(prefix="geoai_cli_adapter_dryrun_") as tmpdir:
        audit_path = Path(tmpdir) / "audit.jsonl"
        os.environ["GEOAI_TASK_MCP_AUDIT_LOG_PATH"] = str(audit_path)
        os.environ["GEOAI_TASK_MCP_ALLOW_WRITES"] = "0"
        os.environ["GEOAI_REPO"] = str(repo)

        from geoai_task_mcp import cli_adapter_dryrun as mod
        from geoai_task_mcp import core

        # --- 1. Launch is impossible / not implemented ---
        assert mod.LAUNCH_IMPLEMENTED is False
        assert mod.launch_enabled() is False
        # Even setting a would-be enable flag cannot turn launch on.
        os.environ["GEOAI_TASK_MCP_ALLOW_LAUNCH"] = "1"
        os.environ["GEOAI_TASK_MCP_ALLOW_WRITES"] = "1"
        assert mod.launch_enabled() is False, "launch must stay impossible even with flags set"
        assert mod.LAUNCH_IMPLEMENTED is False
        # restore write gate OFF for the rest of the test
        os.environ["GEOAI_TASK_MCP_ALLOW_WRITES"] = "0"
        os.environ.pop("GEOAI_TASK_MCP_ALLOW_LAUNCH", None)

        # --- 2. Module imports no launcher into its own namespace ---
        assert not hasattr(mod, "subprocess"), "adapter must not import subprocess"
        assert not hasattr(mod, "Popen")

        # --- 3. Source scan: no process-launch/exec/shell code ---
        src = Path(mod.__file__).read_text(encoding="utf-8")
        for pat in _FORBIDDEN_SOURCE_PATTERNS:
            assert pat not in src, f"forbidden launch pattern in adapter source: {pat!r}"

        # --- 4. Allowlist validation: valid launchable binaries ---
        v_claude = mod.validate_command(["claude", "-p", "do task"])
        assert v_claude.ok is True and v_claude.decision == "allow_dryrun", v_claude
        v_codex = mod.validate_command(["codex", "exec", "review"])
        assert v_codex.ok is True, v_codex

        # valid taskctl worker subcommand
        v_tc = mod.validate_command(
            ["python3", "AITools/taskctl.py", "auto-pickup", "--runner", "r"]
        )
        assert v_tc.ok is True, v_tc

        # --- 5. Allowlist validation: blocked cases ---
        v_rm = mod.validate_command(["rm", "-rf", "/"])
        assert v_rm.ok is False and v_rm.decision == "blocked", v_rm
        v_empty = mod.validate_command([])
        assert v_empty.ok is False, v_empty
        # taskctl subcommand NOT on the worker allowlist (e.g. done) is blocked
        v_done = mod.validate_command(["python3", "AITools/taskctl.py", "done", "X"])
        assert v_done.ok is False, v_done
        # manual-only adapter is never a local launchable command
        v_ds = mod.validate_command(["deepseek_manual"])
        assert v_ds.ok is False and v_ds.is_manual_only is True, v_ds

        # --- 6. Secret redaction: values (incl. secrets) never leak ---
        fake_env = {
            "OPENAI_API_KEY": "sk-SECRET-VALUE-123",
            "GEOAI_TASK_MCP_TOKEN": "tok-DEADBEEF",
            "ANTHROPIC_AUTH": "priv-XYZ",
            "PYTHONPATH": "/opt/x",
            "PATH": "/usr/bin",
            "EMPTYVAR": "",
        }
        redacted = mod.redact_env(fake_env)
        blob = json.dumps(redacted)
        for secret_value in ("sk-SECRET-VALUE-123", "tok-DEADBEEF", "priv-XYZ", "/opt/x"):
            assert secret_value not in blob, f"env value leaked: {secret_value!r}"
        assert redacted["OPENAI_API_KEY"] == "<redacted-secret>", redacted
        assert redacted["GEOAI_TASK_MCP_TOKEN"] == "<redacted-secret>", redacted
        assert redacted["ANTHROPIC_AUTH"] == "<redacted-secret>", redacted
        assert redacted["PYTHONPATH"] == "<set>", redacted
        assert redacted["EMPTYVAR"] == "<unset>", redacted

        # --- 7. build_argv_template returns a template, executes nothing ---
        argv_tpl = mod.build_argv_template("claude_cli", "PROMPT", repo=str(repo))
        assert argv_tpl == ["claude", "-p", "PROMPT", "--cwd", str(repo)], argv_tpl
        assert mod.build_argv_template("deepseek_manual", "PROMPT") == [], "manual has no argv"

        # --- 8. plan_dryrun (valid) -> plan + audit, no execution ---
        plan_ok = mod.plan_dryrun(
            task_id="T1", runner="claude_worker", topic="task_mcp",
            adapter_id="claude_cli", argv=["claude", "-p", "hi"], audit=True,
        )
        assert plan_ok["ok"] is True
        assert plan_ok["mode"] == "dryrun_only"
        assert plan_ok["would_run_argv"] == ["claude", "-p", "hi"]
        assert plan_ok["decision"] == "launch_planned"
        assert plan_ok["launch_enabled"] is False
        assert plan_ok["launch_implemented"] is False
        assert plan_ok["authority_flags"]["process_launch"] is False
        assert plan_ok["authority_flags"]["agent_launch"] is False
        assert plan_ok["authority_flags"]["shell_invocation"] is False
        assert plan_ok["authority_flags"]["write_gate_enabled"] is True

        # --- 9. plan_dryrun (invalid) -> blocked, no argv would run ---
        plan_bad = mod.plan_dryrun(
            task_id="T2", runner="claude_worker", topic="task_mcp",
            adapter_id="claude_cli", argv=["curl", "http://x"], audit=True,
        )
        assert plan_bad["ok"] is False
        assert plan_bad["would_run_argv"] == []
        assert plan_bad["decision"] == "launch_blocked_invalid_command"

        # --- 10. Audit log written, append-only, no secret values present ---
        assert audit_path.exists(), "audit entry should have been appended"
        audit_raw = audit_path.read_text(encoding="utf-8")
        lines = [l for l in audit_raw.splitlines() if l.strip()]
        assert len(lines) == 2, f"expected 2 audit entries, got {len(lines)}"
        for line in lines:
            entry = json.loads(line)
            # env is recorded as NAMES/status only -- never a value
            checked = entry.get("caller_info", {}).get("env_vars_checked", {})
            for name, status in checked.items():
                assert status in ("<set>", "<unset>", "<redacted-secret>"), (
                    f"env {name} leaked value: {status!r}"
                )
        for secret_value in ("sk-SECRET-VALUE-123", "tok-DEADBEEF", "priv-XYZ"):
            assert secret_value not in audit_raw, f"secret leaked into audit log: {secret_value!r}"

        # --- 11. Write gate stayed OFF; contract summary consistent ---
        assert core.writes_allowed() is False
        summary = mod.contract_summary()
        assert summary["launch_implemented"] is False
        assert summary["launch_enabled"] is False
        assert summary["writes_allowed_default_off"] is True

        # --- 12. Parent task queue unmutated ---
        verify = core.run_taskctl(["verify"])
        assert verify.returncode == 0, f"parent verify failed: {verify.stderr}"

        print("PASS: cli_adapter_dryrun_smoke")
        print("  launch impossible / not implemented (flags cannot enable): OK")
        print("  allowlist validation (allow + blocked + manual): OK")
        print("  secret env values redacted (plan + audit): OK")
        print("  write gate OFF by default: OK")
        print("  no launch/exec/shell code in adapter source: OK")
        print("  parent task queue unmutated: OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
