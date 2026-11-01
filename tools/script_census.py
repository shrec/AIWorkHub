#!/usr/bin/env python3
# aiworkhub-tool id: script_census
# aiworkhub-tool purpose: Count ad-hoc scripts (heredocs, python -c, multi-line commands) and project-tool runs in worker *.stdout.log files.
# aiworkhub-tool usage: python tools/script_census.py .aiworkhub/runtime/logs
# aiworkhub-tool param: log_dir:path
"""Census of shell commands in worker logs: how many were ad-hoc scripts."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

MAX_LOGS = 5000
_PYTHON_C_RE = re.compile(r"\bpython[\w.]*(?:\.exe)?\s+(?:-[\w]+\s+)*-c\b", re.IGNORECASE)
_POWERSHELL_COMMAND_RE = re.compile(r"(?<![\w-])-Command\b", re.IGNORECASE)
_TOOL_RE = re.compile(r"(?<![\w/\\.-])(?:\.[/\\])?tools[/\\][\w.-]+\.(?:py|ps1|sh|cmd|bat)\b")


def commands_in(line: str) -> Iterator[str]:
    """Yield the shell commands one JSONL log line records (claude or codex)."""
    try:
        event = json.loads(line)
    except ValueError:
        return
    if not isinstance(event, dict):
        return
    item = event.get("item")
    if (
        event.get("type") == "item.completed"
        and isinstance(item, dict)
        and item.get("type") == "command_execution"
        and isinstance(item.get("command"), str)
    ):
        yield item["command"]
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    for block in content if isinstance(content, list) else ():
        payload: Any = block.get("input") if isinstance(block, dict) else None
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and isinstance(payload, dict)
            and isinstance(payload.get("command"), str)
        ):
            yield payload["command"]


def is_adhoc(command: str) -> bool:
    return bool(
        "<<" in command
        or _PYTHON_C_RE.search(command)
        or command.count("\n") >= 3
        or len(command.strip().splitlines()) >= 3
        or (len(command) > 200 and _POWERSHELL_COMMAND_RE.search(command))
    )


def census(log_dir: Path) -> dict[str, int]:
    logs = commands = logs_with_commands = adhoc = adhoc_chars = invocations = 0
    for path in sorted(log_dir.rglob("*.stdout.log"))[:MAX_LOGS]:
        logs += 1
        found = 0
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                for command in commands_in(line):
                    found += 1
                    invocations += len(_TOOL_RE.findall(command))
                    if is_adhoc(command):
                        adhoc += 1
                        adhoc_chars += len(command)
        commands += found
        logs_with_commands += 1 if found else 0
    return {
        "logs": logs,
        "logs_with_commands": logs_with_commands,
        "commands": commands,
        "adhoc_scripts": adhoc,
        "adhoc_chars": adhoc_chars,
        "project_tool_invocations": invocations,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not Path(argv[0]).is_dir():
        print("usage: script_census.py LOG_DIR", file=sys.stderr)
        return 2
    print(json.dumps(census(Path(argv[0]))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
