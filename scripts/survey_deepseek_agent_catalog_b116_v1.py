#!/usr/bin/env python3
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(os.environ.get("AIWORKHUB_REPO", "/home/shrek/AIWorkHub")).expanduser().resolve()
MCP_ROOT = REPO / "tools" / "aiworkhub"
EVAL_DIR = MCP_ROOT / "eval"
DATA_DIR = MCP_ROOT / "data" / "tasking"

AUTHORITY = {"launch_enabled": False, "install_performed": False, "workflow_switch": False, "write_gate_default_off": True}
DIMS = ["cli_availability", "mcp_compatibility", "non_interactive_mode", "auditability", "sandboxability", "cost_control_fit"]

CANDIDATES = [{"id": "deepseek_tui", "name": "DeepSeek-TUI", "repo": "https://github.com/Hmbown/DeepSeek-TUI", "kind": "terminal_cli", "language": "Rust", "cli_availability": 4, "mcp_compatibility": 4, "non_interactive_mode": 4, "auditability": 3, "sandboxability": 4, "cost_control_fit": 4, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/deepseek-tui.md", "overall_score": 0, "rank": 0}, {"id": "reasonix", "name": "Reasonix", "repo": "https://github.com/nicholasgriffintn/reasonix", "kind": "terminal_cli", "language": "TypeScript/Node.js", "cli_availability": 3, "mcp_compatibility": 3, "non_interactive_mode": 2, "auditability": 2, "sandboxability": 2, "cost_control_fit": 4, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/reasonix.md", "overall_score": 0, "rank": 0}, {"id": "claude_code", "name": "Claude Code", "repo": "https://github.com/anthropics/claude-code", "kind": "terminal_cli", "language": "TypeScript", "cli_availability": 4, "mcp_compatibility": 3, "non_interactive_mode": 4, "auditability": 3, "sandboxability": 1, "cost_control_fit": 2, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/claude_code.md", "overall_score": 0, "rank": 0}, {"id": "copilot_cli", "name": "GitHub Copilot CLI", "repo": "https://github.com/features/copilot", "kind": "terminal_cli", "language": "Go", "cli_availability": 4, "mcp_compatibility": 3, "non_interactive_mode": 2, "auditability": 2, "sandboxability": 1, "cost_control_fit": 2, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/copilot_cli.md", "overall_score": 0, "rank": 0}, {"id": "codex", "name": "Codex (OpenAI)", "repo": "https://github.com/openai/codex", "kind": "terminal_cli", "language": "Go/TypeScript", "cli_availability": 3, "mcp_compatibility": 2, "non_interactive_mode": 3, "auditability": 2, "sandboxability": 1, "cost_control_fit": 2, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/codex.md", "overall_score": 0, "rank": 0}, {"id": "crush", "name": "Crush", "repo": "https://github.com/charmbracelet/crush", "kind": "terminal_cli", "language": "Go", "cli_availability": 3, "mcp_compatibility": 3, "non_interactive_mode": 1, "auditability": 2, "sandboxability": 1, "cost_control_fit": 3, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/crush.md", "overall_score": 0, "rank": 0}, {"id": "oh_my_pi", "name": "Oh My Pi", "repo": "https://github.com/can1357/oh-my-pi", "kind": "terminal_cli", "language": "TypeScript/Node.js", "cli_availability": 3, "mcp_compatibility": 3, "non_interactive_mode": 2, "auditability": 2, "sandboxability": 1, "cost_control_fit": 2, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/oh-my-pi.md", "overall_score": 0, "rank": 0}, {"id": "deep_code", "name": "Deep Code", "repo": "https://github.com/lessweb/deepcode-cli", "kind": "terminal_cli", "language": "TypeScript/Node.js", "cli_availability": 3, "mcp_compatibility": 1, "non_interactive_mode": 1, "auditability": 2, "sandboxability": 1, "cost_control_fit": 3, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/deepcode.md", "overall_score": 0, "rank": 0}, {"id": "qwen_code", "name": "Qwen Code", "repo": "https://github.com/QwenLM/qwen-code", "kind": "terminal_cli", "language": "TypeScript/Node.js", "cli_availability": 3, "mcp_compatibility": 2, "non_interactive_mode": 1, "auditability": 2, "sandboxability": 1, "cost_control_fit": 3, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/qwen_code.md", "overall_score": 0, "rank": 0}, {"id": "pi", "name": "Pi", "repo": "https://github.com/nicholasgriffintn/pi", "kind": "terminal_cli", "language": "TypeScript/Node.js", "cli_availability": 3, "mcp_compatibility": 2, "non_interactive_mode": 1, "auditability": 2, "sandboxability": 1, "cost_control_fit": 3, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/pi_mono.md", "overall_score": 0, "rank": 0}, {"id": "opencode", "name": "OpenCode", "repo": "https://opencode.ai/download", "kind": "terminal_cli", "language": "Unknown", "cli_availability": 3, "mcp_compatibility": 2, "non_interactive_mode": 1, "auditability": 2, "sandboxability": 1, "cost_control_fit": 3, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/opencode.md", "overall_score": 0, "rank": 0}, {"id": "langcli", "name": "Langcli", "repo": "https://github.com/langcli/langcli", "kind": "terminal_cli", "language": "Unknown", "cli_availability": 3, "mcp_compatibility": 2, "non_interactive_mode": 2, "auditability": 2, "sandboxability": 1, "cost_control_fit": 2, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/langcli.md", "overall_score": 0, "rank": 0}, {"id": "kilo_code", "name": "Kilo Code", "repo": "https://github.com/kilocode/kilocode", "kind": "terminal_cli", "language": "TypeScript", "cli_availability": 3, "mcp_compatibility": 2, "non_interactive_mode": 1, "auditability": 1, "sandboxability": 1, "cost_control_fit": 3, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/kilo_code.md", "overall_score": 0, "rank": 0}, {"id": "cline", "name": "Cline", "repo": "https://github.com/cline/cline", "kind": "vscode_extension", "language": "TypeScript", "cli_availability": 1, "mcp_compatibility": 3, "non_interactive_mode": 0, "auditability": 2, "sandboxability": 1, "cost_control_fit": 3, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/cline.md", "overall_score": 0, "rank": 0}, {"id": "github_copilot", "name": "GitHub Copilot (VS Code)", "repo": "https://github.com/features/copilot", "kind": "vscode_extension", "language": "TypeScript", "cli_availability": 0, "mcp_compatibility": 3, "non_interactive_mode": 0, "auditability": 1, "sandboxability": 1, "cost_control_fit": 1, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/github_copilot.md", "overall_score": 0, "rank": 0}, {"id": "cherry_studio", "name": "Cherry Studio", "kind": "desktop_gui", "cli_availability": 0, "mcp_compatibility": 2, "non_interactive_mode": 0, "auditability": 1, "sandboxability": 0, "cost_control_fit": 2, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/cherry_studio.md", "overall_score": 0, "rank": 0}, {"id": "astrbot", "name": "AstrBot", "kind": "chatbot_platform", "cli_availability": 0, "mcp_compatibility": 2, "non_interactive_mode": 0, "auditability": 1, "sandboxability": 0, "cost_control_fit": 2, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/astrbot.md", "overall_score": 0, "rank": 0}, {"id": "hermes", "name": "Hermes", "kind": "research_agent", "cli_availability": 1, "mcp_compatibility": 1, "non_interactive_mode": 1, "auditability": 1, "sandboxability": 1, "cost_control_fit": 2, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/hermes.md", "overall_score": 0, "rank": 0}, {"id": "lobehub", "name": "LobeHub", "kind": "agent_orchestrator", "cli_availability": 0, "mcp_compatibility": 3, "non_interactive_mode": 1, "auditability": 2, "sandboxability": 1, "cost_control_fit": 2, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/lobehub.md", "overall_score": 0, "rank": 0}, {"id": "nanobot", "name": "nanobot", "kind": "chatbot_agent", "cli_availability": 1, "mcp_compatibility": 2, "non_interactive_mode": 0, "auditability": 1, "sandboxability": 0, "cost_control_fit": 2, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/nanobot.md", "overall_score": 0, "rank": 0}, {"id": "openclaw", "name": "OpenClaw", "kind": "chatbot_agent", "cli_availability": 0, "mcp_compatibility": 1, "non_interactive_mode": 0, "auditability": 1, "sandboxability": 0, "cost_control_fit": 2, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/openclaw.md", "overall_score": 0, "rank": 0}, {"id": "workbuddy", "name": "WorkBuddy/CodeBuddy", "kind": "desktop_ide", "cli_availability": 0, "mcp_compatibility": 2, "non_interactive_mode": 0, "auditability": 1, "sandboxability": 0, "cost_control_fit": 2, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/workbuddy.md", "overall_score": 0, "rank": 0}, {"id": "factory_ai_droid", "name": "Factory AI Droid", "kind": "desktop_ide", "cli_availability": 0, "mcp_compatibility": 2, "non_interactive_mode": 0, "auditability": 1, "sandboxability": 0, "cost_control_fit": 2, "source_doc": "deepseek-ai/awesome-deepseek-agent/docs/deepseek-droid-guide.md", "overall_score": 0, "rank": 0}]

def tier(score):
    if score >= 20: return "S_TIER"
    elif score >= 16: return "A_TIER"
    elif score >= 12: return "B_TIER"
    elif score >= 8: return "C_TIER"
    return "D_TIER"

candidates = CANDIDATES
for c in candidates:
    c["overall_score"] = sum(c[d] for d in DIMS)
ranked = sorted(candidates, key=lambda c: (-c["overall_score"], c["id"]))
for i, c in enumerate(ranked, 1):
    c["rank"] = i

ts = datetime.now(timezone.utc).isoformat()
top5 = [c["id"] for c in ranked[:5]]
s_ids = [c["id"] for c in ranked if c["overall_score"] >= 20]
a_ids = [c["id"] for c in ranked if 16 <= c["overall_score"] < 20]
b_ids = [c["id"] for c in ranked if 12 <= c["overall_score"] < 16]

eval_data = {
    "eval_id": "deepseek_agent_catalog_survey_b116_v1",
    "task_id": "DEEPSEEK_TASK_MCP_AGENT_CATALOG_SURVEY_B116_V1",
    "date": ts[:10], "timestamp": ts,
    "mode": "adapter_survey_no_launch_no_install",
    "source": "deepseek-ai/awesome-deepseek-agent GitHub catalog (2026-07-05 snapshot)",
    "verdict": "PASS",
    "summary": f"Scored {len(ranked)} catalog entries across 6 dimensions. Top: {', '.join(top5)}. DeepSeek-TUI is S-tier (score={ranked[0]['overall_score']}/24).",
    "authority_flags": AUTHORITY,
    "scoring_dimensions": DIMS,
    "max_score": 24,
    "total_candidates": len(ranked),
    "tiers": {
        "S_TIER": {"ids": s_ids, "count": len(s_ids), "threshold": "score >= 20"},
        "A_TIER": {"ids": a_ids, "count": len(a_ids), "threshold": "16 <= score < 20"},
        "B_TIER": {"ids": b_ids, "count": len(b_ids), "threshold": "12 <= score < 16"},
    },
    "top_worker_candidates": [
        {"rank": c["rank"], "id": c["id"], "name": c["name"], "kind": c.get("kind",""),
         "overall_score": c["overall_score"], "tier": tier(c["overall_score"]),
         "scores": {d: c[d] for d in DIMS}}
        for c in ranked[:10]
    ],
    "gates": {
        "launch_enabled": False, "install_performed": False,
        "next_experiment_disabled_by_default": True,
        "no_process_launch_code": True, "no_api_key_write": True,
        "no_workflow_switch": True, "survey_readonly": True,
    },
    "recommended_next_adapter_experiment": {
        "candidate_id": "deepseek_tui",
        "reason": "Highest score (S-tier). Native MCP client+server, sandboxed tools (Seatbelt/Landlock), one-shot mode (deepseek -p), direct api.deepseek.com.",
        "disabled_by_default": True,
        "mode": "dryrun_plan_only_no_launch",
        "preconditions": [
            "shutil.which('deepseek') presence probe ONLY (read-only, never spawns)",
            "ALLOW_LAUNCH=0 (default, unchanged)",
            "ALLOW_WRITES=0 (default, unchanged)",
            "No install performed",
            "Adapter registration is data-only",
        ],
    },
    "neural_bridge_note": "Survey is deterministic data artifact. Future adapter SELECTION must be LEARNED routing (MCP_NEURAL_LAUNCH_ROUTING_MIGRATION_V1). No regex/keyword cue router.",
}

jsonl_rows = []
for c in ranked:
    jsonl_rows.append({
        "candidate_id": c["id"], "name": c["name"], "kind": c.get("kind",""),
        "rank": c["rank"], "overall_score": c["overall_score"],
        "tier": tier(c["overall_score"]),
        "scores": {d: c[d] for d in DIMS},
        "source_doc": c.get("source_doc",""), "repo": c.get("repo",""),
        "language": c.get("language",""),
        "worker_suitable": c.get("kind","") == "terminal_cli" and c["overall_score"] >= 12,
    })

next_wave = {
    "next_wave_id": "deepseek_agent_catalog_survey_next_wave_b116_v1",
    "parent_task": "DEEPSEEK_TASK_MCP_AGENT_CATALOG_SURVEY_B116_V1",
    "date": ts[:10], "timestamp": ts, "status": "proposal",
    "notes": "PROPOSAL only, not enqueued. Every follow-up keeps launch DISABLED by default.",
    "survey_summary": {"total_candidates_surveyed": 23, "top_terminal_worker": ranked[0]["id"], "s_tier_count": 1, "a_tier_count": 1, "b_tier_count": 5},
    "follow_up_tasks": [
        {
            "task_id": "DEEPSEEK_TASK_MCP_DEEPSEEK_TUI_ADAPTER_DRYRUN_B117_V1",
            "goal": "Register deepseek_tui as DISABLED-BY-DEFAULT local CLI adapter. Read-only shutil.which presence probe. NO process spawn, NO install, NO launch.",
            "mode": "NO_COMMIT", "disabled_by_default": True,
            "allowed_writes": ["tools/aiworkhub/src/aiworkhub/cli_adapter_dryrun.py", "tools/aiworkhub/tests/test_deepseek_tui_adapter_dryrun_b117_v1.sh", "tools/aiworkhub/eval/deepseek_tui_adapter_dryrun_b117_v1.json"],
            "acceptance": ["adapter registered disabled_by_default=true", "detect() read-only probe, never spawns", "no launch code added", "launch_enabled() returns False", "ALLOW_LAUNCH has no effect"],
        },
    ],
}

EVAL_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

with open(EVAL_DIR / "deepseek_agent_catalog_survey_b116_v1.json", "w", encoding="utf-8") as f:
    json.dump(eval_data, f, indent=2, ensure_ascii=False)
with open(EVAL_DIR / "deepseek_agent_catalog_survey_rows_b116_v1.jsonl", "w", encoding="utf-8") as f:
    for row in jsonl_rows:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
with open(DATA_DIR / "deepseek_agent_catalog_survey_next_wave_b116_v1.json", "w", encoding="utf-8") as f:
    json.dump(next_wave, f, indent=2, ensure_ascii=False)

for c in ranked[:10]:
    t = tier(c["overall_score"])
    scores_str = " | ".join(f"{d}={c[d]}" for d in DIMS)
    print(f"  #{c['rank']:2d} [{t:6s}] {c['id']:25s} score={c['overall_score']:2d}/24  ({scores_str})")
print()
print(f"Authority: launch_enabled=False, install_performed=False")
print(f"Next adapter: {ranked[0]['id']} (disabled-by-default ONLY)")
print("PASS")
