"""Collect the standalone ``tests/mcp_*.py`` MCP contract and smoke gates.

Eleven of these harnesses existed, each with real teeth, and NONE of them ran:
``pytest`` collects ``test_*.py`` only, they are named ``mcp_*.py``, and no CI
workflow invokes any of them -- only
``scripts/audit_task_mcp_finishline_status_b116_v1.py`` even mentions two by
name. Two (B108 and B109) had been RED for months without saying so, which is
the whole point: a gate nobody runs cannot tell you it is red.

Each harness is a ``__main__`` script that exits non-zero on failure, so it is
driven here as a subprocess rather than imported -- importing one would run its
module-level assertions at COLLECTION time, where a failure is a collection
error instead of a test failure.

``test_every_mcp_gate_file_is_accounted_for`` is the part that keeps this from
happening again: a new ``tests/mcp_*.py`` that is neither driven nor explicitly
declared unrun fails immediately, so "written and never run" cannot recur
silently.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent

# Gates that run here, with the wall-clock each took on 2026-09-08 so a future
# reader can see what they cost. The two stdio harnesses spawn real MCP server
# subprocesses over OS pipes, which is exactly what they exist to prove.
COLLECTED_GATES: tuple[tuple[str, int], ...] = (
    ("mcp_client_smoke_contract_freeze.py", 300),          # B108, ~11s
    ("mcp_readonly_result_schema_freeze.py", 300),         # B109, ~16s
    ("mcp_review_queue_summarizer_smoke.py", 180),         # B110 summarizer, ~2s
    ("mcp_review_summarizer_server_wiring_smoke.py", 180), # B111 wiring, ~2s
    ("mcp_review_summarizer_real_queue_integration.py", 300),  # B112, ~4s
    ("mcp_codex_handoff_e2e.py", 300),                     # B116, ~3s
    ("mcp_stdio_client_smoke.py", 600),                    # B109 stdio, ~25s
    ("mcp_stdio_concurrent_client_smoke.py", 600),         # B110 stdio, ~41s
)

# Gates deliberately NOT driven here, each with the reason. This list is the
# declaration, not an excuse: the accounting test below fails if a file is in
# neither list, and a reason that stops being true is a reason to move the file
# into COLLECTED_GATES or delete it.
UNCOLLECTED_GATES: dict[str, str] = {
    "mcp_unified_contract_freeze_gate.py": (
        "B110 aggregator over the B108/B109 outputs and the committed manifest "
        "eval/mcp_unified_contract_freeze_manifest_b110_v1.json. That manifest "
        "is stale after the 2026-09-08 re-freeze AND binds a "
        "result_skeleton_fp for all 11 read-only tools -- including the two "
        "B109 has now MEASURED to be repository-state dependent rather than "
        "contract (see STATE_DEPENDENT_RESULT_TOOLS). Regenerating it honestly "
        "means writing eval/ and rewriting its driver "
        "test_mcp_unified_contract_freeze_gate_b110_v1.sh, which still points "
        "at tools/geoai-task-mcp/ and AITools/taskctl.py -- neither of which "
        "exists in this repository."
    ),
    "mcp_unified_contract_manifest_selftest.py": (
        "B111 byte-identity self-test for the same committed manifest, blocked "
        "on the same regeneration; its FROZEN_MANIFEST_SCAFFOLD also encodes "
        "the 11-skeleton binding B109 no longer produces."
    ),
}


@pytest.fixture(scope="session")
def gate_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One INITIALIZED, isolated repository for every gate in this file.

    Three of these harnesses prove "no write happened" partly by asserting the
    task queue is verify-intact before and after; on an UNINITIALIZED
    repository ``taskctl verify`` is non-zero and that proof reads as a
    failure. MEASURED 2026-09-08: pointed at a fresh checkout with no
    ``.aiworkhub``, B108 and both stdio smokes fail ``no_write_allow_unset``
    for exactly that reason and nothing else -- which is what a CI runner is.
    An initialized temp repo is also what every one of these harnesses says it
    wants: each docstring promises it never addresses the real parent queue.
    """
    from aiworkhub import task_store

    root = tmp_path_factory.mktemp("mcp_gate_repo")
    assert task_store.initialize_repository(root)["ok"]
    return root


def _gate_env(repo: Path) -> dict[str, str]:
    """The env every harness documents in its own usage line.

    The write gate stays OFF: every gate here proves a read-only contract, and
    several of them prove it by snapshotting the queue before and after.
    """
    env = dict(os.environ)
    env["AIWORKHUB_REPO"] = str(repo)
    env["AIWORKHUB_ALLOW_WRITES"] = "0"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO / "src"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return env


@pytest.mark.parametrize(
    "script,timeout", COLLECTED_GATES, ids=[name for name, _ in COLLECTED_GATES]
)
def test_mcp_gate_script_passes(script: str, timeout: int, gate_repo: Path) -> None:
    path = TESTS / script
    assert path.is_file(), f"{script} is listed as collected but does not exist"
    completed = subprocess.run(
        [sys.executable, str(path)],
        cwd=str(REPO),
        env=_gate_env(gate_repo),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    assert completed.returncode == 0, (
        f"{script} exited {completed.returncode}\n"
        f"--- last 4000 chars of output ---\n{completed.stdout[-4000:]}"
    )


def test_every_mcp_gate_file_is_accounted_for() -> None:
    """A new gate must be run or declared unrun, and say why.

    This is the guard against the exact failure this file exists to close: a
    harness with real teeth, written, committed, and never invoked by anything.
    """
    present = {path.name for path in TESTS.glob("mcp_*.py")}
    declared = {name for name, _ in COLLECTED_GATES} | set(UNCOLLECTED_GATES)
    assert present - declared == set(), (
        "these tests/mcp_*.py gates are neither driven nor declared unrun: "
        f"{sorted(present - declared)}"
    )
    assert declared - present == set(), (
        "these gates are declared but no longer exist: "
        f"{sorted(declared - present)}"
    )
    assert all(reason.strip() for reason in UNCOLLECTED_GATES.values())
