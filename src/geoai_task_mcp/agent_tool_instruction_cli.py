"""B821 CLI surface: safe, dry-run-first agent tool-instruction activation.

Usage:
  python -m geoai_task_mcp.agent_tool_instruction_cli preview [--repo ROOT] [--provider P]
  python -m geoai_task_mcp.agent_tool_instruction_cli inspect [--repo ROOT] --provider P
  python -m geoai_task_mcp.agent_tool_instruction_cli apply  [--repo ROOT] --provider P [--write]

Default is dry-run / preview.  ``apply --write`` requires
``GEOAI_TASK_MCP_ALLOW_WRITES=1`` in the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from geoai_task_mcp import agent_tool_instruction_mcp as mcp_surface
from geoai_task_mcp import agent_tool_instructions as instr


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Safe agent tool-instruction activation (dry-run first).",
    )
    p.add_argument(
        "--repo",
        default=os.getcwd(),
        help="Repository root (default: cwd)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # preview: --provider optional (defaults to all three)
    sp = sub.add_parser("preview", help="preview agent tool-use policy blocks")
    sp.add_argument(
        "--provider",
        choices=list(instr.PROVIDERS),
        default=None,
        help="Target provider file (default: all three)",
    )

    # inspect: --provider required
    sp = sub.add_parser("inspect", help="inspect agent tool-use policy blocks")
    sp.add_argument(
        "--provider",
        choices=list(instr.PROVIDERS),
        required=True,
        help="Target provider file",
    )

    # apply: --provider required, --write optional
    sp = sub.add_parser("apply", help="apply agent tool-use policy blocks")
    sp.add_argument(
        "--provider",
        choices=list(instr.PROVIDERS),
        required=True,
        help="Target provider file",
    )
    sp.add_argument(
        "--write",
        action="store_true",
        default=False,
        help="Actually write (default: dry-run)",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        # argparse invalid choice / missing required arg -> exit 1
        return 1 if exc.code != 0 else 0
    repo = args.repo

    try:
        if args.command == "preview":
            provider = getattr(args, "provider", None)
            result = mcp_surface.preview(repo_root=repo, provider=provider)
        elif args.command == "inspect":
            result = mcp_surface.inspect_document(
                repo_root=repo, provider=args.provider,
            )
        elif args.command == "apply":
            result = mcp_surface.apply(
                repo_root=repo,
                provider=args.provider,
                write=args.write,
            )
        else:
            print(json.dumps({"ok": False, "error": f"unknown command: {args.command}"}))
            return 2
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if not result.get("ok", False):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
