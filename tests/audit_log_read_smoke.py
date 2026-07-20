#!/usr/bin/env python3
"""Smoke test: verify audit log read tool returns correct summaries.

Rules:
- GEOAI_TASK_MCP_ALLOW_WRITES=0 (default) — reads only.
- Tool must return structured summary with counts and last entries.
- Must NOT mutate parent task queue.
- Must NOT enable writes, workflow switch, or process launch.
- All authority flags must remain false.
- Test is temp-dir isolated — no fixed repo path pollution.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def _repo_root() -> Path:
    return Path(os.environ.get("GEOAI_REPO", str(Path(__file__).resolve().parents[2])))


def main() -> int:
    repo = _repo_root()

    with tempfile.TemporaryDirectory(prefix="geoai_audit_read_smoke_") as tmpdir:
        audit_path = Path(tmpdir) / "audit.jsonl"
        os.environ["GEOAI_TASK_MCP_AUDIT_LOG_PATH"] = str(audit_path)
        os.environ["GEOAI_TASK_MCP_ALLOW_WRITES"] = "0"
        os.environ["GEOAI_REPO"] = str(repo)

        # Import core AFTER env is set
        from geoai_task_mcp import core

        # --- 1. Verify writes are off ---
        health = core.health()
        assert health["ok"], f"health failed: {health}"
        assert health["writes_allowed"] is False

        # --- 2. Read empty audit log ---
        result_empty = core.read_audit_log(max_entries=10)
        assert result_empty["ok"] is True, result_empty
        assert result_empty["audit_log_exists"] is False, result_empty
        assert result_empty["total_entries"] == 0, result_empty
        assert result_empty["last_entries"] == [], result_empty
        assert result_empty["authority_flags"]["write_gate_enabled"] is True
        assert result_empty["authority_flags"]["workflow_switch"] is False
        assert result_empty["authority_flags"]["process_launch"] is False
        assert result_empty["authority_flags"]["agent_launch"] is False

        # --- 3. Pre-populate audit log with synthetic entries ---
        synthetic_entries = [
            {"timestamp": "2026-07-04T10:00:00+00:00", "tool_name": "auto-pickup", "action": "blocked_write", "blocked_reason": "write gate off", "caller_info": {"pid": 100, "env_vars_checked": {"GEOAI_TASK_MCP_ALLOW_WRITES": "<unset>"}}},
            {"timestamp": "2026-07-04T10:01:00+00:00", "tool_name": "done", "action": "blocked_write", "blocked_reason": "write gate off", "caller_info": {"pid": 101, "env_vars_checked": {"GEOAI_TASK_MCP_ALLOW_WRITES": "<unset>"}}},
            {"timestamp": "2026-07-04T10:02:00+00:00", "tool_name": "auto-pickup", "action": "blocked_write", "blocked_reason": "write gate off", "caller_info": {"pid": 102, "env_vars_checked": {"GEOAI_TASK_MCP_ALLOW_WRITES": "<set>"}}},
            {"timestamp": "2026-07-04T10:03:00+00:00", "tool_name": "review", "action": "blocked_write", "blocked_reason": "write gate off", "caller_info": {"pid": 103, "env_vars_checked": {"GEOAI_TASK_MCP_ALLOW_WRITES": "<unset>"}}},
            {"timestamp": "2026-07-04T10:04:00+00:00", "tool_name": "export-jsonl", "action": "blocked_write", "blocked_reason": "write gate off", "caller_info": {"pid": 104, "env_vars_checked": {"GEOAI_TASK_MCP_ALLOW_WRITES": "<unset>"}}},
        ]
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "w", encoding="utf-8") as fh:
            for entry in synthetic_entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # --- 4. Read audit log summary ---
        result = core.read_audit_log(max_entries=3)
        assert result["ok"] is True, result
        assert result["audit_log_exists"] is True, result
        assert result["total_entries"] == 5, f"expected 5, got {result['total_entries']}"
        assert result["file_size_bytes"] > 0, result
        assert result["writes_allowed"] is False

        # Counts by tool
        assert result["entries_by_tool"]["auto-pickup"] == 2, result["entries_by_tool"]
        assert result["entries_by_tool"]["done"] == 1
        assert result["entries_by_tool"]["review"] == 1
        assert result["entries_by_tool"]["export-jsonl"] == 1

        # Counts by action
        assert result["entries_by_action"]["blocked_write"] == 5, result["entries_by_action"]

        # Last entries limited to max_entries=3
        assert len(result["last_entries"]) == 3, f"expected 3, got {len(result['last_entries'])}"
        assert result["last_entries"][-1]["tool_name"] == "export-jsonl", result["last_entries"][-1]

        # --- 5. Verify authority flags ---
        af = result["authority_flags"]
        assert af["write_gate_enabled"] is True
        assert af["workflow_switch"] is False
        assert af["process_launch"] is False
        assert af["agent_launch"] is False

        # --- 6. Verify no env values leaked ---
        for entry in result["last_entries"]:
            env_checked = entry.get("caller_info", {}).get("env_vars_checked", {})
            for var_name, status in env_checked.items():
                assert status in ("<set>", "<unset>"), (
                    f"env var {var_name} leaked value: {status!r}"
                )

        # --- 7. Verify read_audit_log does NOT enable writes ---
        assert os.environ.get("GEOAI_TASK_MCP_ALLOW_WRITES", "0") == "0"
        health2 = core.health()
        assert health2["writes_allowed"] is False

        # --- 8. Verify parent queue NOT mutated ---
        verify = core.run_taskctl(["verify"])
        assert verify.returncode == 0, f"verify failed: {verify.stderr}"

        # --- 9. max_entries=0 returns empty last_entries ---
        result_zero = core.read_audit_log(max_entries=0)
        assert result_zero["total_entries"] == 5
        assert result_zero["last_entries"] == []

        print("PASS: audit_log_read_smoke")
        print(f"  empty log read: OK")
        print(f"  populated log (5 entries): OK")
        print(f"  counts by tool: {result['entries_by_tool']}")
        print(f"  last_entries capped at 3: OK")
        print(f"  authority flags all false (except write_gate_enabled): OK")
        print(f"  no env values leaked: OK")
        print(f"  writes remained off: OK")
        print(f"  parent queue unmutated: OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
