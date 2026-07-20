#!/usr/bin/env python3
"""B109 read-only MCP tool structuredContent result-schema freeze (v1).

Complements B108 (input-schema freeze). B108 froze every tool's ``inputSchema``
(byte-canonical sha256 over ``tools/list``). B109 freezes the OUTPUT side: the
canonical key/type SKELETON of every read-only tool's ``structuredContent``
result, so output-contract drift (a renamed/added/removed key, or a changed
value type) is caught the same way an input-schema drift is.

Why a skeleton and not the ``outputSchema``: FastMCP generates only a generic
``{"type":"object","additionalProperties":true}`` output schema for tools that
return ``dict[str, Any]`` (verified via ``tools/list``), which cannot detect key
drift. So this harness snapshots the ACTUAL ``structuredContent`` a real MCP
client receives and reduces it to a value->type skeleton.

Method (faithfully extends B108):
  * REAL client path: ``mcp.shared.memory.create_connected_server_and_client_
    session`` -> ``initialize`` -> ``tools/list`` -> ``tools/call`` -- the same
    path a VS Code / Claude / Codex stdio MCP client uses.
  * PURE IN-PROCESS, NO PROCESS LAUNCH: 7 of the 11 read-only tools normally
    shell out to ``AITools/taskctl.py`` via ``core.run_taskctl`` -> ``subprocess
    .run``. To honor "schema introspection with NO process launch at all", this
    harness (a) replaces ``core.run_taskctl`` with a deterministic in-process
    stub that returns a canned ``TaskCtlResult`` (never spawns), and (b) installs
    a TRIPWIRE over ``core.subprocess.run`` that records + rejects any real
    launch. ``launch_attempts`` MUST stay 0.
  * DETERMINISTIC FINGERPRINTS: the skeleton records value TYPES (not values),
    sorts every dict key, and canonicalizes lists to their sorted-unique element
    skeletons. This normalizes ALL volatile leaf values -- absolute/tmp paths,
    pids, timestamps, argv/command values, ordering -- so the sha256 is byte-
    stable across runs, machines, and env (``ALLOW_WRITES`` flips only bool
    VALUES, never the skeleton). Determinism is re-proven across two independent
    client sessions.
  * NO WRITES: the MCP-owned audit state dir is isolated to a private mktemp
    path and asserted byte-identical + empty before/after every read-only round,
    with ``GEOAI_TASK_MCP_ALLOW_WRITES`` UNSET and =1. With ``run_taskctl``
    stubbed it is structurally impossible for the harness to mutate the parent
    queue; the shell wrapper runs ``taskctl verify`` for the end-to-end queue
    integrity gate (that single verify is the ONLY process launch, and it lives
    OUTSIDE this in-process introspection harness).

``frozen_output_contract_v1`` is true ONLY IF every check passes; otherwise
false with ``failing_check`` naming the first failure. Never enables writes for
real, never launches a process/agent, makes no network call, logs no secrets.

Usage:
    PYTHONPATH=tools/geoai-task-mcp/src GEOAI_REPO=/home/shrek/GeoAI \
    python3 tools/geoai-task-mcp/tests/mcp_readonly_result_schema_freeze.py \
        [--out result.json] [--emit-frozen]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from mcp.shared.memory import (  # noqa: E402
    create_connected_server_and_client_session as connect_client,
)

from geoai_task_mcp import cli_adapter_readonly_tool as ro  # noqa: E402
from geoai_task_mcp import core  # noqa: E402
from geoai_task_mcp.core import TaskCtlResult  # noqa: E402
from geoai_task_mcp import server  # noqa: E402


# --- frozen read-only contract v1 (same 11 read-only tools as B108) --------
READONLY_TOOLS: tuple[str, ...] = (
    "geoai_task_health",
    "geoai_task_review_queue",
    "geoai_task_list",
    "geoai_task_show",
    "geoai_task_pending_for_runner",
    "geoai_task_collision_guard",
    "geoai_task_usage_report",
    "geoai_task_audit_log_read",
    "geoai_cli_adapter_plan_readonly",
    "geoai_cli_adapter_audit_summary_readonly",
    "geoai_cli_adapter_report_readonly",
)

# Read-only tools_call payloads (client path) -- required args supplied.
# Reused verbatim from B108 so the two freezes describe the same call surface.
READONLY_CALL_ARGS: dict[str, dict[str, Any]] = {
    "geoai_task_health": {},
    "geoai_task_review_queue": {},
    "geoai_task_list": {"status": "pending", "limit": 3},
    "geoai_task_show": {"task_id": "CLAUDE_TASK_MCP_READONLY_TOOL_RESULT_SCHEMA_FREEZE_B109_V1"},
    "geoai_task_pending_for_runner": {"runner": "claude_task_mcp_result_schema_b109"},
    "geoai_task_collision_guard": {"print_json": True},
    "geoai_task_usage_report": {},
    "geoai_task_audit_log_read": {"max_entries": 10},
    "geoai_cli_adapter_plan_readonly": {
        "task_id": "B109-PLAN", "runner": "b109_freeze", "topic": "task_mcp",
        "adapter_id": "claude_cli", "argv": ["claude", "-p", "hi"],
    },
    "geoai_cli_adapter_audit_summary_readonly": {"max_entries": 10},
    "geoai_cli_adapter_report_readonly": {
        "task_id": "B109-REPORT", "runner": "b109_freeze", "topic": "task_mcp",
        "adapter_id": "claude_cli", "argv": ["codex", "exec", "review"],
    },
}

# Byte-canonical sha256 of the value->type skeleton of each read-only tool's
# structuredContent result, frozen at B109. Drift in ANY tool's output key set
# or value type flips frozen_output_contract_v1 to false -> this is the freeze.
FROZEN_RESULT_SKELETON_FINGERPRINTS: dict[str, str] = {
    "geoai_cli_adapter_audit_summary_readonly": "38556ccf570e39d958d93d3859b90928dfa39367e991a63ace1441567d7d40a5",
    "geoai_cli_adapter_plan_readonly": "f15bce6176e5ee3a51b18bcdbf4fc7b7f633ecddda984446b98fd6e35342e2c5",
    "geoai_cli_adapter_report_readonly": "c1c81f65e4d04bdd81ef1b84fd810d41f23eabbda763a65936b83dfaa3141e05",
    "geoai_task_audit_log_read": "a3ed606211c4752400a8719f8f711d160e9f51a8a8d4b0c1f112cf1e52670b4e",
    "geoai_task_collision_guard": "2c83150c951e941ac4354831d3a09f4c1398816f94f259fbd8c173958033719f",
    "geoai_task_health": "e61b8cb033c374b08000c2585607d8cccdb1a91da4055af0e033441cf1201c14",
    "geoai_task_list": "2c83150c951e941ac4354831d3a09f4c1398816f94f259fbd8c173958033719f",
    "geoai_task_pending_for_runner": "87a32b68a9a85471f84885c1b59da774b3b5ad5f16d2864d922d8570de65766d",
    "geoai_task_review_queue": "2c83150c951e941ac4354831d3a09f4c1398816f94f259fbd8c173958033719f",
    "geoai_task_show": "2c83150c951e941ac4354831d3a09f4c1398816f94f259fbd8c173958033719f",
    "geoai_task_usage_report": "2c83150c951e941ac4354831d3a09f4c1398816f94f259fbd8c173958033719f",
}

# The generic FastMCP output schema fingerprint per tool (supplementary static
# check; catches a return-annotation change e.g. dict -> non-dict).
FROZEN_OUTPUT_SCHEMA_FINGERPRINTS: dict[str, str] = {
    "geoai_cli_adapter_audit_summary_readonly": "5fc4ac244ce411f881fb112af8fce5c896d55e4bf19a253c4b2f363a095a67b1",
    "geoai_cli_adapter_plan_readonly": "0e26f78662f85cf082a1b9a02a8ab8f20465fddde3cc0c64afa98b53483993eb",
    "geoai_cli_adapter_report_readonly": "9db21818742f39cddd1089f8373dab352f3489fd555866ae7866772659cd70ca",
    "geoai_task_audit_log_read": "fdd2305a613bfd24f01c484b593ef0d36accb5df3f49a30c696b4b2d37f1bf54",
    "geoai_task_collision_guard": "fcb8ae84a30dc9801d116a3d294f67a613436a9ff8de4969b0097bef429d66f4",
    "geoai_task_health": "9c2b1d290ffa8d3e2cb8555afaf7117cf45e613d588cbb5e787b191f9251f1dc",
    "geoai_task_list": "6bc17623c20a41768e84146af7271946f2d89119181c35882f3a63c2330f3361",
    "geoai_task_pending_for_runner": "9574847f6b6c2d8eaef5271fe4486ba39c46210bdfaef31a87b0ec303c12f474",
    "geoai_task_review_queue": "90a02a5eab8f75261a4d9c191a7e1f939d44052dbf46a5a8ffd0bfba2e12ec86",
    "geoai_task_show": "49abd9371460804368fc330a3c9eb5ad061dcc81d62794b08c4b163ef4ac31b5",
    "geoai_task_usage_report": "e9583569f8f66b1ebf56609cee43b4c3ffbb9b5ee70322a7a8d40032150f3563",
}

# Read-only tool modules that MUST hold no process-launch code (defense in
# depth; core.py legitimately owns the taskctl subprocess and is excluded).
LAUNCH_FREE_MODULES = (
    "server.py",
    "cli_adapter_readonly_tool.py",
    "cli_adapter_dryrun.py",
)
LAUNCH_PATTERNS = (
    "subprocess", "os.system", "os.popen", "os.exec", "os.fork",
    "os.spawn", "Popen(", "shell=True", "pty.spawn",
)


# --- canonical value->type skeleton (the normalization) --------------------
def _skeleton(v: Any) -> Any:
    """Reduce a structuredContent value to a canonical key/type skeleton.

    This IS the volatile-value normalization: every leaf becomes a stable type
    token (never its value), so absolute/tmp paths, pids, timestamps, argv and
    command strings, and env-dependent booleans are all erased. Dict keys are
    sorted; lists collapse to their sorted-unique element skeletons; empty
    containers get stable tokens. The result is byte-stable across runs.
    """
    if isinstance(v, bool):        # bool before int (bool subclasses int)
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "number"
    if v is None:
        return "null"
    if isinstance(v, str):
        return "str"
    if isinstance(v, dict):
        if not v:
            return {"__object__": "empty"}
        return {k: _skeleton(v[k]) for k in sorted(v)}
    if isinstance(v, list):
        if not v:
            return {"__list_of__": []}
        uniq = sorted({
            json.dumps(_skeleton(x), sort_keys=True, separators=(",", ":"))
            for x in v
        })
        return {"__list_of__": [json.loads(u) for u in uniq]}
    return "unknown:" + type(v).__name__


def _canon_fp(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _snapshot_dir(d: Path) -> list[tuple[str, int, str]]:
    """Byte-level snapshot (relpath, size, sha256) of every file under d."""
    if not d.exists():
        return []
    return sorted(
        (str(p.relative_to(d)), p.stat().st_size,
         hashlib.sha256(p.read_bytes()).hexdigest())
        for p in d.rglob("*") if p.is_file()
    )


# --- in-process stub + launch tripwire (NO process launch at all) ----------
def _install_no_launch_stubs() -> dict[str, Any]:
    """Replace run_taskctl with a canned in-process stub and trip any launch.

    Returns a mutable state dict holding the launch-attempt log and the
    originals (for restore). After this, NO read-only tool can spawn a process.
    """
    st: dict[str, Any] = {
        "launch_attempts": [],
        "orig_run_taskctl": core.run_taskctl,
        "orig_subprocess_run": core.subprocess.run,
    }

    def _stub_run_taskctl(args: list[str], **_kw: Any) -> TaskCtlResult:
        # Deterministic, launch-free stand-in for the taskctl subprocess.
        return TaskCtlResult(
            command=["python3", "AITools/taskctl.py", *list(args)],
            returncode=0,
            stdout="",
            stderr="",
        )

    def _tripwire(*a: Any, **k: Any) -> Any:
        st["launch_attempts"].append(str(a[0]) if a else str(k.get("args")))
        raise AssertionError("process launch attempted during schema freeze")

    core.run_taskctl = _stub_run_taskctl  # type: ignore[assignment]
    core.subprocess.run = _tripwire       # type: ignore[assignment]
    return st


def _restore_no_launch_stubs(st: dict[str, Any]) -> None:
    core.run_taskctl = st["orig_run_taskctl"]
    core.subprocess.run = st["orig_subprocess_run"]


# --- client-path capture ---------------------------------------------------
async def _capture_via_client() -> dict[str, Any]:
    """Real client path: tools/list + call every read-only tool.

    Returns {'output_schemas': {name: schema}, 'skeletons': {name: skeleton},
    'errors': [name...]}.
    """
    out_schemas: dict[str, Any] = {}
    skeletons: dict[str, Any] = {}
    errors: list[str] = []
    async with connect_client(server.mcp._mcp_server) as client:
        listed = await client.list_tools()
        out_schemas = {t.name: t.outputSchema for t in listed.tools}
        for name in READONLY_TOOLS:
            r = await client.call_tool(name, READONLY_CALL_ARGS[name])
            if r.isError or r.structuredContent is None:
                errors.append(name)
                continue
            skeletons[name] = _skeleton(r.structuredContent)
    return {"output_schemas": out_schemas, "skeletons": skeletons, "errors": errors}


async def _run_readonly_round(state_dir: Path) -> dict[str, Any]:
    """Call every read-only tool; prove the MCP-owned state dir is unchanged."""
    before = _snapshot_dir(state_dir)
    async with connect_client(server.mcp._mcp_server) as client:
        for name in READONLY_TOOLS:
            r = await client.call_tool(name, READONLY_CALL_ARGS[name])
            if r.isError:
                return {"ok": False, "reason": f"tool_error:{name}"}
    after = _snapshot_dir(state_dir)
    return {
        "ok": True,
        "state_before": before,
        "state_after": after,
        "state_byte_identical": before == after,
        "state_empty": after == [],
    }


def run_freeze() -> dict[str, Any]:
    """Execute the full read-only output-contract freeze checks."""
    checks: dict[str, bool] = {}
    detail: dict[str, Any] = {}
    failing_check: str | None = None

    prev_audit = os.environ.get("GEOAI_TASK_MCP_AUDIT_LOG_PATH")
    prev_cards = os.environ.get("GEOAI_TASK_MCP_COLLISION_CARDS_PATH")
    prev_allow = os.environ.get("GEOAI_TASK_MCP_ALLOW_WRITES")
    state_dir = Path(tempfile.mkdtemp(prefix="geoai_b109_freeze_"))
    # Isolate audit + collision-card sources to private, nonexistent temp paths
    # so both the no-write proof and the skeleton are free of shared state.
    os.environ["GEOAI_TASK_MCP_AUDIT_LOG_PATH"] = str(state_dir / "audit.jsonl")
    os.environ["GEOAI_TASK_MCP_COLLISION_CARDS_PATH"] = str(state_dir / "nonexistent_cards.jsonl")

    stubs = _install_no_launch_stubs()
    try:
        # --- capture over two independent client sessions ------------------
        os.environ.pop("GEOAI_TASK_MCP_ALLOW_WRITES", None)
        cap_a = asyncio.run(_capture_via_client())
        cap_b = asyncio.run(_capture_via_client())

        visible = set(cap_a["output_schemas"])
        ro_visible = [n for n in READONLY_TOOLS if n in visible]
        checks["readonly_tools_visible"] = set(ro_visible) == set(READONLY_TOOLS)
        detail["readonly_tools_visible"] = sorted(ro_visible)
        detail["total_tools_visible"] = len(visible)

        # every read-only tool produced a structuredContent skeleton
        captured = set(cap_a["skeletons"])
        checks["result_skeletons_captured"] = (
            not cap_a["errors"] and captured == set(READONLY_TOOLS)
        )
        detail["capture_errors"] = sorted(cap_a["errors"])
        # THE SNAPSHOT: canonical key/type skeleton for every read-only tool.
        detail["result_skeletons"] = {k: cap_a["skeletons"][k] for k in sorted(cap_a["skeletons"])}

        # result-skeleton fingerprints vs frozen
        cur_fp = {n: _canon_fp(s) for n, s in cap_a["skeletons"].items()}
        fp_b = {n: _canon_fp(s) for n, s in cap_b["skeletons"].items()}
        detail["current_result_skeleton_fingerprints"] = {k: cur_fp[k] for k in sorted(cur_fp)}
        detail["frozen_result_skeleton_fingerprints"] = FROZEN_RESULT_SKELETON_FINGERPRINTS
        skel_mismatch = sorted(
            n for n in FROZEN_RESULT_SKELETON_FINGERPRINTS
            if cur_fp.get(n) != FROZEN_RESULT_SKELETON_FINGERPRINTS[n]
        )
        checks["result_skeleton_fingerprints_match_frozen"] = not skel_mismatch
        checks["result_skeleton_deterministic_across_sessions"] = cur_fp == fp_b
        detail["result_skeleton_fingerprint_mismatches"] = skel_mismatch

        # supplementary: FastMCP output-schema fingerprints vs frozen
        cur_os = {n: _canon_fp(s) for n, s in cap_a["output_schemas"].items()
                  if n in READONLY_TOOLS}
        detail["current_output_schema_fingerprints"] = {k: cur_os[k] for k in sorted(cur_os)}
        detail["frozen_output_schema_fingerprints"] = FROZEN_OUTPUT_SCHEMA_FINGERPRINTS
        os_mismatch = sorted(
            n for n in FROZEN_OUTPUT_SCHEMA_FINGERPRINTS
            if cur_os.get(n) != FROZEN_OUTPUT_SCHEMA_FINGERPRINTS[n]
        )
        checks["output_schema_fingerprints_match_frozen"] = not os_mismatch
        detail["output_schema_fingerprint_mismatches"] = os_mismatch

        # --- no-write proof, ALLOW_WRITES unset ----------------------------
        os.environ.pop("GEOAI_TASK_MCP_ALLOW_WRITES", None)
        r_unset = asyncio.run(_run_readonly_round(state_dir))
        checks["no_write_allow_unset"] = bool(
            r_unset.get("ok") and r_unset.get("state_byte_identical")
            and r_unset.get("state_empty")
        )
        detail["round_allow_unset"] = r_unset

        # --- no-write proof, ALLOW_WRITES=1 --------------------------------
        os.environ["GEOAI_TASK_MCP_ALLOW_WRITES"] = "1"
        r_set = asyncio.run(_run_readonly_round(state_dir))
        checks["no_write_allow_set"] = bool(
            r_set.get("ok") and r_set.get("state_byte_identical")
            and r_set.get("state_empty")
        )
        detail["round_allow_set"] = r_set

        # --- no process launch --------------------------------------------
        src_dir = Path(SRC) / "geoai_task_mcp"
        launch_hits: dict[str, list[str]] = {}
        for mod in LAUNCH_FREE_MODULES:
            text = (src_dir / mod).read_text(encoding="utf-8")
            hits = [p for p in LAUNCH_PATTERNS if p in text]
            if hits:
                launch_hits[mod] = hits
        checks["no_process_launch"] = (
            not stubs["launch_attempts"]
            and not launch_hits
            and ro.launch_enabled() is False
            and ro.LAUNCH_IMPLEMENTED is False
        )
        detail["launch_attempts"] = len(stubs["launch_attempts"])
        detail["run_taskctl_stubbed"] = True
        detail["launch_free_module_hits"] = launch_hits
        detail["launch_enabled"] = ro.launch_enabled()
        detail["launch_implemented"] = ro.LAUNCH_IMPLEMENTED
        detail["normalization"] = {
            "strategy": "value->type-token skeleton; dict keys sorted; lists -> "
                        "sorted-unique element skeletons; empty containers tokenized",
            "volatile_values_erased": [
                "absolute_paths", "tmp_paths", "pids", "timestamps",
                "argv_values", "command_values", "env_dependent_bools", "ordering",
            ],
        }
    finally:
        _restore_no_launch_stubs(stubs)
        for k, prev in (
            ("GEOAI_TASK_MCP_AUDIT_LOG_PATH", prev_audit),
            ("GEOAI_TASK_MCP_COLLISION_CARDS_PATH", prev_cards),
            ("GEOAI_TASK_MCP_ALLOW_WRITES", prev_allow),
        ):
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        shutil.rmtree(state_dir, ignore_errors=True)

    for name, passed in checks.items():
        if not passed:
            failing_check = name
            break

    frozen = failing_check is None
    return {
        "eval_id": "mcp_readonly_tool_result_schema_freeze_b109_v1",
        "task_id": "CLAUDE_TASK_MCP_READONLY_TOOL_RESULT_SCHEMA_FREEZE_B109_V1",
        "mode": "mcp_readonly_output_contract_freeze_no_writes",
        "complements": "CLAUDE_TASK_MCP_CLIENT_SMOKE_CONTRACT_FREEZE_B108_V1",
        "client_path": "mcp.ClientSession over in-memory streams (initialize/tools_list/tools_call)",
        "frozen_output_contract_v1": frozen,
        "failing_check": failing_check,
        "readonly_tool_count": len(READONLY_TOOLS),
        "checks": checks,
        "authority_flags": {
            "contract_frozen": False,
            "process_launch_authority": False,
            "write_gate_enabled": False,
            "runtime_authority": False,
            "default_authority": False,
        },
        "detail": detail,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="write result JSON to this path")
    ap.add_argument("--emit-frozen", action="store_true",
                    help="print the current skeleton/output-schema fingerprints "
                         "(bootstrap the frozen baselines) and exit 0")
    args = ap.parse_args()
    result = run_freeze()

    if args.emit_frozen:
        print("RESULT_SKELETON_FINGERPRINTS")
        print(json.dumps(result["detail"]["current_result_skeleton_fingerprints"],
                         indent=2, sort_keys=True))
        print("OUTPUT_SCHEMA_FINGERPRINTS")
        print(json.dumps(result["detail"]["current_output_schema_fingerprints"],
                         indent=2, sort_keys=True))
        return 0

    text = json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["frozen_output_contract_v1"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
