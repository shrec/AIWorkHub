"""NF-2026-02050: ``import aiworkhub.worker_ai_tools_mcp`` does not load Source Graph.

Measured in 0.12.85: a module-level ``quality_review_scope`` import in
``quality_reviewer`` pulled the whole Source Graph stack into every worker MCP
start (161 -> 202 modules, 4.24 s -> 7.82 s under load), pushing worker
connects past the client's 30 s timeout. The import runs in a fresh interpreter
so the creep fails CI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

_CHILD = r"""
import json, sys
import aiworkhub.worker_ai_tools_mcp
print(json.dumps(sorted(
    name for name in sys.modules
    if name.startswith(("aiworkhub.source_graph", "aiworkhub.quality_review_scope"))
)))
"""


def test_worker_mcp_import_does_not_load_source_graph() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(SRC_ROOT), env.get("PYTHONPATH", "")) if part
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # stdin=DEVNULL: a child inheriting a pipe stdin can hang at startup on Windows.
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=str(REPO_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    assert json.loads(completed.stdout.splitlines()[-1]) == []
