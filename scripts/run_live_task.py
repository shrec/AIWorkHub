#!/usr/bin/env python3
"""Launch and supervise one exact AIWorkHub task through the real stdio MCP API."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client


REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "tools/aiworkhub/src"
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
    env.update({
        "PYTHONPATH": str(SRC),
        "AIWORKHUB_REPO": str(REPO),
        "AIWORKHUB_ALLOW_WRITES": "1",
        "AIWORKHUB_ALLOW_LAUNCH": "1",
        "AIWORKHUB_MAX_PROCESSES": str(args.max_processes),
    })
    codex_inner_sandbox = os.environ.get("AIWORKHUB_CODEX_INNER_SANDBOX_MODE")
    if codex_inner_sandbox:
        env["AIWORKHUB_CODEX_INNER_SANDBOX_MODE"] = codex_inner_sandbox
    params = StdioServerParameters(
        command="/usr/bin/python3",
        args=["-m", "aiworkhub.server"],
        env=env,
        cwd=str(REPO),
    )
    read_timeout = timedelta(seconds=max(120, args.timeout + 60))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=read_timeout) as session:
            await session.initialize()
            launch_result = await session.call_tool(
                "aiworkhub_agent_launch_task",
                {
                    "task_id": args.task_id,
                    "runner": args.runner,
                    "topic": args.topic,
                    "adapter_id": args.adapter,
                    "model": args.model,
                    "owner_prompt": args.owner_prompt,
                    "timeout_seconds": args.timeout,
                },
                read_timeout_seconds=read_timeout,
            )
            launch = _payload(launch_result)
            print(json.dumps({"launch": launch}, ensure_ascii=False, indent=2), flush=True)
            if not launch.get("ok"):
                return 2
            request_id = str(launch["request_id"])
            deadline = time.monotonic() + args.timeout + 30
            while time.monotonic() < deadline:
                await asyncio.sleep(args.poll_seconds)
                status_result = await session.call_tool(
                    "aiworkhub_agent_task_status",
                    {"request_id": request_id},
                    read_timeout_seconds=read_timeout,
                )
                status = _payload(status_result)
                print(json.dumps({
                    "request_id": request_id,
                    "state": status.get("state"),
                    "task_state": status.get("task_state"),
                    "process_alive": status.get("process_alive"),
                }, ensure_ascii=False), flush=True)
                if status.get("state") in TERMINAL_STATES:
                    collected_result = await session.call_tool(
                        "aiworkhub_agent_collect_result",
                        {"request_id": request_id, "max_log_bytes": 65536},
                        read_timeout_seconds=read_timeout,
                    )
                    collected = _payload(collected_result)
                    print(json.dumps({"result": collected}, ensure_ascii=False, indent=2), flush=True)
                    return 0 if collected.get("review_ready") else 3
            await session.call_tool(
                "aiworkhub_agent_cancel_task",
                {"request_id": request_id, "reason": "live_client_deadline"},
                read_timeout_seconds=read_timeout,
            )
            return 4


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--runner", required=True)
    parser.add_argument("--topic", required=True)
    parser.add_argument(
        "--adapter",
        choices=("claude_cli", "codex_cli", "deepseek_copilot_cli"),
        required=True,
    )
    parser.add_argument("--model")
    parser.add_argument("--owner-prompt", default="")
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--max-processes", type=int, default=8)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
