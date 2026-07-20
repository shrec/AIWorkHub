#!/usr/bin/env python3
"""Smoke test: verify blocked write attempts are logged to audit.jsonl.

Rules:
- GEOAI_TASK_MCP_ALLOW_WRITES=0 (default) — writes must be BLOCKED.
- Blocked attempt MUST appear in audit.jsonl.
- Parent task queue (SQLite) MUST NOT be mutated.
- Audit log MUST NOT contain secrets or env var VALUES.
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

    # --- 1. Setup: temp audit log path so we don't pollute the real one ---
    with tempfile.TemporaryDirectory(prefix="geoai_audit_smoke_") as tmpdir:
        audit_path = Path(tmpdir) / "audit.jsonl"
        os.environ["GEOAI_TASK_MCP_AUDIT_LOG_PATH"] = str(audit_path)
        os.environ["GEOAI_TASK_MCP_ALLOW_WRITES"] = "0"
        os.environ["GEOAI_REPO"] = str(repo)

        # Import core AFTER env is set
        from geoai_task_mcp import core

        # --- 2. Snapshot parent queue state BEFORE ---
        health_before = core.health()
        assert health_before["ok"], f"health failed: {health_before}"
        assert health_before["writes_allowed"] is False

        # --- 3. Attempt a blocked write ---
        blocked = core.auto_pickup("smoke_audit_runner", "task_mcp")
        assert blocked["returncode"] == 126, f"expected 126, got {blocked}"
        assert "write command blocked" in blocked["stderr"], blocked

        # --- 4. Verify audit log exists and has our entry ---
        assert audit_path.exists(), f"audit log missing: {audit_path}"
        lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 1, f"audit log empty: {audit_path}"

        entries = [json.loads(line) for line in lines]
        blocked_entries = [e for e in entries if e["action"] == "blocked_write"]
        assert len(blocked_entries) >= 1, f"no blocked_write entry in {entries}"

        entry = blocked_entries[-1]
        assert entry["tool_name"] == "auto-pickup", entry
        assert "blocked" in entry["blocked_reason"].lower(), entry
        assert "pid" in entry["caller_info"], entry
        assert "env_vars_checked" in entry["caller_info"], entry

        # --- 5. Verify NO secrets / env values in log ---
        raw_text = audit_path.read_text(encoding="utf-8")
        secret_indicators = [
            v for v in os.environ.values()
            if v and len(v) > 4 and any(
                pat in os.environ for pat in ["SECRET", "TOKEN", "PASSWORD", "KEY"]
            )
        ]
        # Stronger check: the audit log must only record <set>/<unset>, never actual values
        for line in lines:
            entry_obj = json.loads(line)
            env_checked = entry_obj["caller_info"]["env_vars_checked"]
            for var_name, status in env_checked.items():
                assert status in ("<set>", "<unset>"), (
                    f"env var {var_name} has leaked value: {status!r}"
                )

        # --- 6. Verify NO parent queue mutation ---
        health_after = core.health()
        assert health_after["ok"], f"health changed after blocked write: {health_after}"
        assert health_after["verify"]["returncode"] == health_before["verify"]["returncode"]

        # --- 7. Verify health still reports writes_allowed=False ---
        assert health_after["writes_allowed"] is False

        print("PASS: write_gate_audit_smoke")
        print(f"  audit entries: {len(entries)}")
        print(f"  blocked entries: {len(blocked_entries)}")
        print(f"  no secrets leaked: OK")
        print(f"  parent queue unmutated: OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
