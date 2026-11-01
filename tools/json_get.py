#!/usr/bin/env python3
# aiworkhub-tool id: json_get
# aiworkhub-tool purpose: Print one value from a JSON or JSONL file by dotted path (list indexes allowed), bounded output.
# aiworkhub-tool usage: python tools/json_get.py data.json items.0.name
# aiworkhub-tool param: file:path
# aiworkhub-tool param: dotted_path:str
"""Print the value at DOTTED_PATH inside a JSON or JSONL file."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_CHARS = 4000


def load(path: Path) -> Any:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("file_too_large")
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() != ".jsonl":
        try:
            return json.loads(text)
        except ValueError:
            pass
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def walk(value: Any, dotted: str) -> Any:
    if dotted in ("", "."):
        return value
    for segment in dotted.split("."):
        if isinstance(value, dict) and segment in value:
            value = value[segment]
        elif isinstance(value, list):
            try:
                value = value[int(segment)]
            except (ValueError, IndexError):
                raise KeyError(segment) from None
        else:
            raise KeyError(segment)
    return value


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) != 2:
        print("usage: json_get.py FILE DOTTED_PATH", file=sys.stderr)
        return 2
    try:
        value = walk(load(Path(argv[0])), argv[1])
    except KeyError as exc:
        print(f"path_not_found: {exc.args[0]}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"unreadable: {exc}", file=sys.stderr)
        return 1
    text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    if len(text) > MAX_OUTPUT_CHARS:
        text = f"{text[:MAX_OUTPUT_CHARS]}\n...[truncated {len(text) - MAX_OUTPUT_CHARS} chars]"
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
