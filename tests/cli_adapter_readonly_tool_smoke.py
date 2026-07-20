#!/usr/bin/env python3
"""Smoke test: READ-ONLY MCP tool contract over the CLI adapter dryrun (B106).

Proves:
  * launch is impossible / not implemented -- no env flag can enable it
  * the plan tool is strictly READ-ONLY: it appends NO audit entry and mutates
    NO queue state, even with GEOAI_TASK_MCP_ALLOW_WRITES=1
  * only allowlisted commands validate; everything else is blocked
  * env VALUES (incl. secrets) are redacted in plan / report / audit output
  * the audit-summary view is read-only (reads, never appends)
  * register(mcp) exposes exactly the read-only tool surface, no more
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
# read-only tool module source.
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


class _StubMCP:
    """Minimal FastMCP-like stub capturing tool registrations."""

    def __init__(self) -> None:
        self.registered: list[str] = []

    def tool(self, name: str | None = None, description: str | None = None):
        def deco(fn):
            self.registered.append(name or fn.__name__)
            return fn

        return deco


def _repo_root() -> Path:
    return Path(os.environ.get("GEOAI_REPO", str(Path(__file__).resolve().parents[3])))


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return len([l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()])


def main() -> int:
    repo = _repo_root()

    with tempfile.TemporaryDirectory(prefix="geoai_cli_adapter_readonly_") as tmpdir:
        audit_path = Path(tmpdir) / "audit.jsonl"
        os.environ["GEOAI_TASK_MCP_AUDIT_LOG_PATH"] = str(audit_path)
        os.environ["GEOAI_TASK_MCP_ALLOW_WRITES"] = "0"
        os.environ["GEOAI_REPO"] = str(repo)

        from geoai_task_mcp import cli_adapter_readonly_tool as mod
        from geoai_task_mcp import core

        # Seed the audit log with one pre-existing entry via the parent writer,
        # so we can later prove the read-only tool does NOT append.
        core.write_audit_entry(
            tool_name="seed", action="seed_entry", blocked_reason="", repo=repo
        )
        assert _count_lines(audit_path) == 1, "seed entry should exist"

        # --- 1. Launch is impossible / not implemented ---
        assert mod.READONLY is True
        assert mod.LAUNCH_IMPLEMENTED is False
        assert mod.launch_enabled() is False
        # Even setting would-be enable flags cannot turn launch on.
        os.environ["GEOAI_TASK_MCP_ALLOW_LAUNCH"] = "1"
        os.environ["GEOAI_TASK_MCP_ALLOW_WRITES"] = "1"
        assert mod.launch_enabled() is False, "launch must stay impossible even with flags set"
        assert mod.LAUNCH_IMPLEMENTED is False

        # --- 2. Module imports no launcher into its own namespace ---
        assert not hasattr(mod, "subprocess"), "read-only tool must not import subprocess"
        assert not hasattr(mod, "Popen")

        # --- 3. Source scan: no process-launch/exec/shell code ---
        src = Path(mod.__file__).read_text(encoding="utf-8")
        for pat in _FORBIDDEN_SOURCE_PATTERNS:
            assert pat not in src, f"forbidden launch pattern in tool source: {pat!r}"

        # --- 4. READ-ONLY plan (valid): NO audit append even with ALLOW_WRITES=1 ---
        before = _count_lines(audit_path)
        plan_ok = mod.plan_command_readonly(
            task_id="T1", runner="claude_worker", topic="task_mcp",
            adapter_id="claude_cli", argv=["claude", "-p", "hi"],
            env={"OPENAI_API_KEY": "sk-SECRET-1", "PATH": "/usr/bin"},
        )
        after = _count_lines(audit_path)
        assert after == before, f"plan tool must NOT write audit (before={before} after={after})"
        assert plan_ok["ok"] is True
        assert plan_ok["mode"] == "dryrun_only"
        assert plan_ok["readonly"] is True
        assert plan_ok["audit_written"] is False
        assert "audit_entry_action" not in plan_ok, "read-only plan must not report an audit write"
        assert plan_ok["would_run_argv"] == ["claude", "-p", "hi"]
        assert plan_ok["decision"] == "launch_planned"
        assert plan_ok["launch_enabled"] is False
        assert plan_ok["launch_implemented"] is False
        af = plan_ok["authority_flags"]
        assert af["process_launch"] is False
        assert af["agent_launch"] is False
        assert af["shell_invocation"] is False
        assert af["queue_write"] is False
        assert af["audit_write"] is False
        assert af["readonly"] is True
        # secret value never present in the plan blob
        assert "sk-SECRET-1" not in json.dumps(plan_ok), "secret leaked into plan"
        assert plan_ok["redacted_env"]["OPENAI_API_KEY"] == "<redacted-secret>"
        assert plan_ok["redacted_env"]["PATH"] == "<set>"

        # restore write gate OFF for the remainder
        os.environ["GEOAI_TASK_MCP_ALLOW_WRITES"] = "0"
        os.environ.pop("GEOAI_TASK_MCP_ALLOW_LAUNCH", None)

        # --- 5. READ-ONLY plan (invalid) -> blocked, still no write ---
        before = _count_lines(audit_path)
        plan_bad = mod.plan_command_readonly(
            task_id="T2", runner="claude_worker", topic="task_mcp",
            adapter_id="claude_cli", argv=["curl", "http://x"],
        )
        assert _count_lines(audit_path) == before, "blocked plan must not write audit"
        assert plan_bad["ok"] is False
        assert plan_bad["would_run_argv"] == []
        assert plan_bad["decision"] == "launch_blocked_invalid_command"
        assert plan_bad["readonly"] is True

        # --- 6. Blocked / manual-only cases still resolve read-only ---
        for argv in (["rm", "-rf", "/"], [], ["deepseek_manual"],
                     ["python3", "AITools/taskctl.py", "done", "X"]):
            p = mod.plan_command_readonly(
                task_id="T", runner="r", topic="task_mcp",
                adapter_id="claude_cli", argv=argv,
            )
            assert p["ok"] is False, f"expected block for {argv!r}"
        assert _count_lines(audit_path) == before, "no blocked case may append audit"

        # --- 7. Secret redaction helper: values never leak ---
        redacted = mod.redact_env({
            "GEOAI_TASK_MCP_TOKEN": "tok-DEADBEEF",
            "ANTHROPIC_AUTH": "priv-XYZ",
            "PYTHONPATH": "/opt/x",
            "EMPTYVAR": "",
        })
        blob = json.dumps(redacted)
        for secret_value in ("tok-DEADBEEF", "priv-XYZ", "/opt/x"):
            assert secret_value not in blob, f"env value leaked: {secret_value!r}"
        assert redacted["GEOAI_TASK_MCP_TOKEN"] == "<redacted-secret>"
        assert redacted["ANTHROPIC_AUTH"] == "<redacted-secret>"
        assert redacted["PYTHONPATH"] == "<set>"
        assert redacted["EMPTYVAR"] == "<unset>"

        # --- 8. Audit summary view is READ-ONLY (reads, never appends) ---
        before = _count_lines(audit_path)
        summary = mod.audit_summary_readonly(max_entries=50)
        assert _count_lines(audit_path) == before, "audit summary must not append"
        assert summary["readonly"] is True
        assert summary["ok"] is True
        assert summary["audit_log_exists"] is True
        assert summary["total_entries"] == before
        assert summary["authority_flags"]["process_launch"] is False
        assert summary["authority_flags"]["audit_write"] is False

        # --- 9. Combined report returns BOTH plan and audit summary, no write ---
        before = _count_lines(audit_path)
        report = mod.readonly_tool_report(
            task_id="T3", runner="claude_worker", topic="task_mcp",
            adapter_id="claude_cli", argv=["codex", "exec", "review"],
        )
        assert _count_lines(audit_path) == before, "report must not append audit"
        assert report["readonly"] is True
        assert report["mode"] == "mcp_readonly_cli_plan_tool_no_process_launch"
        assert report["plan"]["ok"] is True
        assert report["plan"]["decision"] == "launch_planned"
        assert report["audit_summary"]["readonly"] is True
        assert report["authority_flags"]["process_launch"] is False

        # --- 10. register(mcp) exposes exactly the read-only tool surface ---
        stub = _StubMCP()
        names = mod.register(stub)
        assert set(stub.registered) == set(names), (stub.registered, names)
        assert len(names) == 3
        assert set(names) == set(mod.READONLY_TOOLS.keys())

        # --- 11. contract summary consistent ---
        cs = mod.contract_summary()
        assert cs["readonly"] is True
        assert cs["launch_implemented"] is False
        assert cs["launch_enabled"] is False
        assert cs["mode"] == "mcp_readonly_cli_plan_tool_no_process_launch"
        assert cs["audit_write"] is False and cs["queue_write"] is False

        # --- 12. Write gate off; parent task queue unmutated ---
        assert core.writes_allowed() is False
        verify = core.run_taskctl(["verify"])
        assert verify.returncode == 0, f"parent verify failed: {verify.stderr}"

        # --- 13. No secret ever reached the audit log ---
        audit_raw = audit_path.read_text(encoding="utf-8")
        for secret_value in ("sk-SECRET-1", "tok-DEADBEEF", "priv-XYZ"):
            assert secret_value not in audit_raw, f"secret leaked into audit log: {secret_value!r}"

        print("PASS: cli_adapter_readonly_tool_smoke")
        print("  launch impossible / not implemented (flags cannot enable): OK")
        print("  plan tool strictly read-only (no audit append, even ALLOW_WRITES=1): OK")
        print("  allowlist validation (allow + blocked + manual): OK")
        print("  secret env values redacted (plan + report + audit): OK")
        print("  audit-summary view read-only: OK")
        print("  register(mcp) exposes exactly 3 read-only tools: OK")
        print("  no launch/exec/shell code in tool source: OK")
        print("  parent task queue unmutated: OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
