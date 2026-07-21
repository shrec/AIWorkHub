#!/usr/bin/env python3
"""mcp_unified_contract_freeze_gate.py

CLAUDE_TASK_MCP_UNIFIED_CONTRACT_FREEZE_GATE_B110_V1

Single read-only aggregator gate that composes the two independent MCP
contract freezes into ONE binding so they cannot silently diverge:

  * B108 froze the inputSchema of every tool
    (mcp_client_smoke_contract_freeze.py -> detail.frozen_schema_fingerprints)
  * B109 froze the structuredContent output-schema AND value->type result
    skeleton of the 11 read-only tools
    (mcp_readonly_result_schema_freeze.py ->
     detail.frozen_output_schema_fingerprints,
     detail.frozen_result_skeleton_fingerprints)

This gate is a pure JSON aggregator. It does NOT launch the MCP server, an
agent, or a model, and it writes nothing except an optional verdict file. The
caller (test_mcp_unified_contract_freeze_gate_b110_v1.sh) runs the two freeze
harnesses fresh into mktemp result files and hands their paths here; this gate
re-derives the live fingerprints from those fresh results and fails on ANY
input-or-output drift versus the single frozen manifest.

Architecture note (neural-control-first): this is a deterministic ABCR/validator
tooth over a deterministic executor surface. It is NOT a runtime cognitive
router. Adapter/runner/topic SELECTION must become a learned router
(MCP_NEURAL_LAUNCH_ROUTING_MIGRATION_V1); this freeze is only the drift-
protection evidence extractor for that surface.

Read-only. AIWORKHUB_ALLOW_WRITES is irrelevant to this gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any


MANIFEST_ID = "mcp_unified_contract_freeze_manifest_b110_v1"
GATE_MODE = "mcp_unified_contract_freeze_no_launch"


def _canonical_manifest_fingerprint(manifest: dict[str, Any]) -> str:
    """Recompute the manifest self-fingerprint from its binding content.

    Must match the manifest_fingerprint_spec baked into the manifest:
    sha256(json.dumps({readonly_tools, write_gated_tools, per_readonly_tool,
    write_gated_input_schema_fp}, sort_keys=True, separators=(',',':'))).
    """
    binding = {
        "readonly_tools": sorted(manifest["readonly_tools"]),
        "write_gated_tools": sorted(manifest["write_gated_tools"]),
        "per_readonly_tool": manifest["per_readonly_tool"],
        "write_gated_input_schema_fp": manifest["write_gated_input_schema_fp"],
    }
    blob = json.dumps(binding, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def evaluate(manifest: dict[str, Any], b108: dict[str, Any], b109: dict[str, Any]) -> dict[str, Any]:
    """Return a verdict dict. ``ok`` is True iff there is zero drift.

    Every failure is appended to ``drift`` (never raises on data-driven
    mismatch) so one gate reports every divergence at once.
    """
    drift: list[dict[str, Any]] = []

    def fail(check: str, tool: str | None, expected: Any, got: Any) -> None:
        drift.append({"check": check, "tool": tool, "expected": expected, "got": got})

    # --- 0. manifest identity -------------------------------------------------
    if manifest.get("manifest_id") != MANIFEST_ID:
        fail("manifest_id", None, MANIFEST_ID, manifest.get("manifest_id"))

    # --- 1. harness self-freeze integrity (both freezes must pass on their own)
    if b108.get("frozen_contract_v1") is not True:
        fail("b108_frozen_contract_v1", None, True, b108.get("frozen_contract_v1"))
    if b108.get("failing_check") is not None:
        fail("b108_failing_check", None, None, b108.get("failing_check"))
    if b108["detail"].get("schema_fingerprint_mismatches"):
        fail("b108_schema_fingerprint_mismatches", None, [],
             b108["detail"]["schema_fingerprint_mismatches"])

    if b109.get("frozen_output_contract_v1") is not True:
        fail("b109_frozen_output_contract_v1", None, True, b109.get("frozen_output_contract_v1"))
    if b109.get("failing_check") is not None:
        fail("b109_failing_check", None, None, b109.get("failing_check"))
    if b109["detail"].get("result_skeleton_fingerprint_mismatches"):
        fail("b109_result_skeleton_mismatches", None, [],
             b109["detail"]["result_skeleton_fingerprint_mismatches"])
    if b109["detail"].get("output_schema_fingerprint_mismatches"):
        fail("b109_output_schema_mismatches", None, [],
             b109["detail"]["output_schema_fingerprint_mismatches"])
    if b109["detail"].get("current_result_skeleton_fingerprints") != \
            b109["detail"].get("frozen_result_skeleton_fingerprints"):
        fail("b109_current_ne_frozen_skeleton", None, "current==frozen", "current!=frozen")
    if b109["detail"].get("current_output_schema_fingerprints") != \
            b109["detail"].get("frozen_output_schema_fingerprints"):
        fail("b109_current_ne_frozen_output", None, "current==frozen", "current!=frozen")

    # --- 2. tool-set agreement across manifest + both freezes ------------------
    m_ro = sorted(manifest["readonly_tools"])
    m_wg = sorted(manifest["write_gated_tools"])
    b108_ro = sorted(b108["detail"]["readonly_tools_visible"])
    b108_wg = sorted(b108["detail"]["write_gated_tools_visible"])
    b109_ro = sorted(b109["detail"]["readonly_tools_visible"])
    if b108_ro != m_ro:
        fail("readonly_set_b108", None, m_ro, b108_ro)
    if b109_ro != m_ro:
        fail("readonly_set_b109", None, m_ro, b109_ro)
    if b108_wg != m_wg:
        fail("write_gated_set_b108", None, m_wg, b108_wg)

    # --- 3. live fingerprints re-derived from the FRESH harness results --------
    live_input = b108["detail"]["frozen_schema_fingerprints"]          # 11 ro + 4 wg
    live_output = b109["detail"]["frozen_output_schema_fingerprints"]  # 11 ro
    live_skel = b109["detail"]["frozen_result_skeleton_fingerprints"]  # 11 ro

    per_ro = manifest["per_readonly_tool"]
    wg_input = manifest["write_gated_input_schema_fp"]

    # --- 4. per read-only tool: input + output-schema + result-skeleton --------
    for tool in m_ro:
        bound = per_ro.get(tool, {})
        if live_input.get(tool) != bound.get("input_schema_fp"):
            fail("input_schema_drift", tool, bound.get("input_schema_fp"), live_input.get(tool))
        if live_output.get(tool) != bound.get("output_schema_fp"):
            fail("output_schema_drift", tool, bound.get("output_schema_fp"), live_output.get(tool))
        if live_skel.get(tool) != bound.get("result_skeleton_fp"):
            fail("result_skeleton_drift", tool, bound.get("result_skeleton_fp"), live_skel.get(tool))

    # --- 5. write-gated tools: input-schema freeze -----------------------------
    for tool in m_wg:
        if live_input.get(tool) != wg_input.get(tool):
            fail("write_gated_input_drift", tool, wg_input.get(tool), live_input.get(tool))

    # --- 6. manifest self-integrity (tamper detection) -------------------------
    recomputed = _canonical_manifest_fingerprint(manifest)
    if recomputed != manifest.get("manifest_fingerprint"):
        fail("manifest_fingerprint", None, manifest.get("manifest_fingerprint"), recomputed)

    # --- 7. no-write proof carried from both harnesses -------------------------
    for label, doc in (("b108", b108), ("b109", b109)):
        for rk in ("round_allow_unset", "round_allow_set"):
            r = doc["detail"].get(rk, {})
            if not (r.get("state_byte_identical") is True and r.get("state_empty") is True
                    and r.get("state_after") == r.get("state_before") == []):
                fail(f"{label}_no_write", rk, "empty+byte_identical", r)

    # --- 8. no-launch proof carried from both harnesses ------------------------
    if b108["detail"].get("server_launch_pattern_hits") != []:
        fail("b108_launch_pattern_hits", None, [], b108["detail"].get("server_launch_pattern_hits"))
    if b108["detail"].get("launch_enabled") is not False or b108["detail"].get("launch_implemented") is not False:
        fail("b108_launch_flags", None, False, "launch_enabled/implemented")
    if b109["detail"].get("launch_attempts") != 0:
        fail("b109_launch_attempts", None, 0, b109["detail"].get("launch_attempts"))
    if b109["detail"].get("run_taskctl_stubbed") is not True:
        fail("b109_run_taskctl_stubbed", None, True, b109["detail"].get("run_taskctl_stubbed"))
    if b109["detail"].get("launch_enabled") is not False or b109["detail"].get("launch_implemented") is not False:
        fail("b109_launch_flags", None, False, "launch_enabled/implemented")

    # --- 9. authority flags all false across manifest + both freezes -----------
    for label, doc in (("manifest", manifest), ("b108", b108), ("b109", b109)):
        for k, v in doc.get("authority_flags", {}).items():
            if v is not False:
                fail(f"{label}_authority_flag", k, False, v)

    ok = len(drift) == 0
    by_family: dict[str, int] = {}
    for d in drift:
        by_family[d["check"]] = by_family.get(d["check"], 0) + 1

    return {
        "gate_id": "mcp_unified_contract_freeze_gate_b110_v1",
        "task_id": "CLAUDE_TASK_MCP_UNIFIED_CONTRACT_FREEZE_GATE_B110_V1",
        "mode": GATE_MODE,
        "frozen_unified_contract_v1": ok,
        "ok": ok,
        "readonly_tool_count": len(m_ro),
        "write_gated_tool_count": len(m_wg),
        "manifest_fingerprint": manifest.get("manifest_fingerprint"),
        "manifest_fingerprint_recomputed": recomputed,
        "checks": {
            "harness_self_freeze_ok": not any(
                d["check"].startswith(("b108_", "b109_")) and "drift" not in d["check"]
                for d in drift),
            "input_schema_bound": not any(d["check"] == "input_schema_drift" for d in drift),
            "output_schema_bound": not any(d["check"] == "output_schema_drift" for d in drift),
            "result_skeleton_bound": not any(d["check"] == "result_skeleton_drift" for d in drift),
            "write_gated_input_bound": not any(d["check"] == "write_gated_input_drift" for d in drift),
            "manifest_integrity_ok": not any(d["check"] == "manifest_fingerprint" for d in drift),
            "no_write_proven": not any(d["check"].endswith("_no_write") for d in drift),
            "no_launch_proven": not any("launch" in d["check"] for d in drift),
            "authority_flags_all_false": not any(d["check"].endswith("authority_flag") for d in drift),
            "tool_sets_agree": not any("set_" in d["check"] for d in drift),
        },
        "drift_count": len(drift),
        "drift_by_check": by_family,
        "drift": drift,
        "failing_check": (drift[0]["check"] if drift else None),
        "authority_flags": {
            "contract_frozen": False,
            "default_authority": False,
            "process_launch_authority": False,
            "runtime_authority": False,
            "write_gate_enabled": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MCP unified contract-freeze gate (B110)")
    ap.add_argument("--manifest", required=True, help="unified contract freeze manifest JSON")
    ap.add_argument("--b108", required=True, help="fresh B108 client-smoke freeze result JSON")
    ap.add_argument("--b109", required=True, help="fresh B109 result-schema freeze result JSON")
    ap.add_argument("--out", default=None, help="optional verdict JSON output path")
    args = ap.parse_args(argv)

    with open(args.manifest) as f:
        manifest = json.load(f)
    with open(args.b108) as f:
        b108 = json.load(f)
    with open(args.b109) as f:
        b109 = json.load(f)

    verdict = evaluate(manifest, b108, b109)

    if args.out:
        with open(args.out, "w") as f:
            json.dump(verdict, f, indent=2, sort_keys=True)
            f.write("\n")

    print(json.dumps({
        "ok": verdict["ok"],
        "frozen_unified_contract_v1": verdict["frozen_unified_contract_v1"],
        "drift_count": verdict["drift_count"],
        "failing_check": verdict["failing_check"],
    }, sort_keys=True))
    if not verdict["ok"]:
        for d in verdict["drift"][:20]:
            print(f"DRIFT {d['check']} tool={d['tool']}", file=sys.stderr)
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
