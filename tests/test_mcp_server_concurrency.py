from __future__ import annotations

import asyncio
import threading

from aiworkhub import server


def test_slow_sync_mcp_tool_does_not_block_independent_read() -> None:
    """A provider-bound sync tool must not monopolize FastMCP's event loop."""

    slow_started = threading.Event()
    release_slow = threading.Event()

    @server.mcp.tool(name="test_concurrency_slow_sync_b199")
    def slow_sync_tool() -> dict[str, bool]:
        slow_started.set()
        release_slow.wait(timeout=5)
        return {"slow": True}

    @server.mcp.tool(name="test_concurrency_fast_read_b199")
    def fast_read_tool() -> dict[str, bool]:
        return {"fast": True}

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        slow_task = asyncio.create_task(
            server.mcp.call_tool("test_concurrency_slow_sync_b199", {})
        )
        assert await asyncio.to_thread(slow_started.wait, 1.0)

        fast_result = await asyncio.wait_for(
            server.mcp.call_tool("test_concurrency_fast_read_b199", {}),
            timeout=1.0,
        )
        assert fast_result
        assert loop.time() - started_at < 1.0

        release_slow.set()
        assert await asyncio.wait_for(slow_task, timeout=1.0)

    try:
        asyncio.run(scenario())
    finally:
        release_slow.set()

