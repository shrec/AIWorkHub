#!/usr/bin/env python3
"""NF-2026-01454 Layer-2 canary: one read-only OpenCode worker through the
real stdio MCP API of THIS working tree, on the Windows AppContainer backend.

Evidence sought: the worker reaches a model turn AND its ``awh`` worker MCP
server answers at least one tool call through the host-side bridge (the
audit ledger in the request's task_mcp_worker_runtime records it), then the
card lands in a terminal state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

REPO = Path(__file__).resolve().parents[1]
TERMINAL_STATES = {
    "review_ready",
    "exited",
    "exited_without_review",
    "timed_out",
    "cancelled",
    "worker_failed",
    "scope_rejected",
    "validation_failed",
    "promotion_conflict",
    "finalize_failed",
    "monitor_error",
    "blocked",
}


def _payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {"ok": False, "error": "MCP tool returned no JSON object"}


async def run(args: argparse.Namespace) -> int:
    env = get_default_environment()
    env.update(
        {
            "PYTHONPATH": str(REPO / "src"),
            "AIWORKHUB_REPO": str(REPO),
            "AIWORKHUB_REPO_ROOT": str(REPO),
            "AIWORKHUB_ALLOW_WRITES": "1",
            "AIWORKHUB_ALLOW_LAUNCH": "1",
            "AIWORKHUB_MCP_STDIO_BACKEND": "stdlib",
        }
    )
    params = StdioServerParameters(
        command=str(Path(sys.executable)),
        args=["-m", "aiworkhub.server"],
        env=env,
        cwd=str(REPO),
    )
    read_timeout = timedelta(seconds=max(300, args.timeout + 300))
    async with stdio_client(params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=read_timeout
        ) as session:
            await session.initialize()
            boot = _payload(
                await session.call_tool("aiworkhub_manager_bootstrap", {})
            )
            print("bootstrap ok:", boot.get("ok"), flush=True)
            create = _payload(
                await session.call_tool(
                    "aiworkhub_task_create",
                    {
                        "task_id": args.task_id,
                        "title": "OpenCode AppContainer MCP bridge canary (read-only)",
                        "runner": args.runner,
                        "topic": args.topic,
                        "objective": args.prompt,
                        "acceptance": [
                            "The worker calls awh_aiworkhub_worker_session_current_state exactly once",
                            "No file is modified",
                        ],
                        "allowed_writes": [],
                        "read_only": True,
                    },
                )
            )
            print(
                "create ok:", create.get("ok"), "| receipt:",
                json.dumps(create, ensure_ascii=False)[:1200], flush=True,
            )
            if not create.get("ok"):
                return 4
            launch = _payload(
                await session.call_tool(
                    "aiworkhub_agent_launch_task",
                    {
                        "task_id": args.task_id,
                        "owner_prompt": args.prompt,
                        "topic": args.topic,
                        "runner": args.runner,
                        "adapter_id": args.adapter_id,
                        "model": args.model,
                        "timeout_seconds": args.timeout,
                    },
                )
            )
            print("launch:", json.dumps(launch, ensure_ascii=False)[:2000], flush=True)
            if not launch.get("ok"):
                return 2
            deadline = asyncio.get_event_loop().time() + args.timeout + 240
            status: dict[str, Any] = {}
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(20)
                status = _payload(
                    await session.call_tool(
                        "aiworkhub_agent_task_status",
                        {"request_id": args.task_id, "detail": "evidence"},
                    )
                )
                state = str(status.get("state") or status.get("status") or "")
                print("state:", state, flush=True)
                if state in TERMINAL_STATES:
                    break
            print(
                "final:",
                json.dumps(status, ensure_ascii=False)[:4000],
                flush=True,
            )
            collect = _payload(
                await session.call_tool(
                    "aiworkhub_agent_collect_result", {"request_id": args.task_id}
                )
            )
            print(
                "collect ok:", collect.get("ok"), flush=True
            )
            logs = str(collect.get("log_excerpt") or collect.get("stdout") or "")[:3000]
            print("logs:", logs, flush=True)
            state = str(status.get("state") or status.get("status") or "")
            return 0 if state in TERMINAL_STATES else 3


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--topic", default="canary")
    parser.add_argument("--runner", default="opencode")
    parser.add_argument("--adapter-id", default="opencode_cli")
    parser.add_argument("--model", default="opencode-go/grok-4.6")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
