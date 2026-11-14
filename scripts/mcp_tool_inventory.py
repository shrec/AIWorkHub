#!/usr/bin/env python3
"""Measure the AIWorkHub MCP tool surface (NF-2026-02270).

For every tool the AIWorkHub MCP server registers, prints its name, role (the
``mcp_tool_metadata.MCP_TOOL_PROFILES`` entry: how each profile exposes it),
subsystem prefix, the byte size of its ``tools/list`` entry, and its call count
over the last 30 days from the repository MCP audit ledger. Totals per profile
(tool count and ``tools/list`` bytes) close the report.

Call counts are honest about coverage: the audit ledger
(``core._audit_log_path``) records write-gate actions, not every tool call. A
tool whose name never appears in the ledger is unmeasured and reported as
``null``, never ``0``; an absent or unreadable ledger leaves every count null and
says why.

The JSON report is written under ``<repo>/.scratch/`` only (untracked scratch
space); any other ``--out`` is refused. The server is imported with
``AIWORKHUB_MCP_SURFACE=all`` so every tool registers; listing per profile then
re-reads the profile at list time, exactly as the server does. No tool is called.

Usage:
    python scripts/mcp_tool_inventory.py [--repo PATH] [--src PATH] [--writes on|off] [--out NAME]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_ID = "aiworkhub.mcp_tool_inventory.v1"
WINDOW_DAYS = 30
SCRATCH_DIRNAME = ".scratch"
DEFAULT_REPORT_NAME = "mcp_tool_inventory.json"
# Bounded ledger read: a ledger past this many bytes is read up to the cap and
# the report says the window may be incomplete.
LEDGER_MAX_BYTES = 256 * 1024 * 1024
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _import_server(src: Path, writes: str | None) -> tuple[Any, Any, Any]:
    """Import ``aiworkhub.server`` from ``src`` with every tool registered."""
    os.environ["AIWORKHUB_MCP_SURFACE"] = "all"
    if writes is not None:
        os.environ["AIWORKHUB_ALLOW_WRITES"] = "1" if writes == "on" else "0"
    sys.path.insert(0, str(src))
    server = importlib.import_module("aiworkhub.server")
    metadata = importlib.import_module("aiworkhub.mcp_tool_metadata")
    core = importlib.import_module("aiworkhub.core")
    return server, metadata, core


def _registered_names(server: Any) -> list[str]:
    mcp = server.mcp
    if getattr(server, "_MCP_SDK_AVAILABLE", False):
        manager = getattr(mcp, "_tool_manager", None)
        tools = getattr(manager, "_tools", None)
        if isinstance(tools, Mapping):
            return [str(name) for name in tools]
        return [tool.name for tool in asyncio.run(mcp.list_tools())]
    return list(mcp.registered_tools)


def _listing(server: Any, surface: str) -> list[dict[str, Any]]:
    """The ``tools/list`` entries ``surface`` publishes, as wire JSON objects."""
    previous = os.environ.get("AIWORKHUB_MCP_SURFACE")
    os.environ["AIWORKHUB_MCP_SURFACE"] = surface
    try:
        if getattr(server, "_MCP_SDK_AVAILABLE", False):
            tools = asyncio.run(server.mcp.list_tools())
            return [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]
        return list(server._stdio_tools_list(server.mcp._tools)["tools"])
    finally:
        if previous is None:
            os.environ.pop("AIWORKHUB_MCP_SURFACE", None)
        else:
            os.environ["AIWORKHUB_MCP_SURFACE"] = previous


def entry_bytes(entry: Mapping[str, Any]) -> int:
    """Byte size of one ``tools/list`` entry as compact UTF-8 JSON."""
    return len(json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def subsystem_prefix(name: str) -> str:
    """``aiworkhub_<subsystem>`` for AIWorkHub-prefixed names, else the first segment."""
    parts = name.split("_")
    if parts[0] == "aiworkhub" and len(parts) > 2:
        return "_".join(parts[:2])
    return parts[0]


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _ledger_lines(path: Path, max_bytes: int) -> Iterator[str]:
    read = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            read += len(line.encode("utf-8"))
            if read > max_bytes:
                raise OverflowError(read)
            yield line


def ledger_call_counts(
    ledger: Path, names: list[str], *, now: datetime, max_bytes: int = LEDGER_MAX_BYTES
) -> tuple[dict[str, int | None], dict[str, Any]]:
    """Per-tool call counts over the window; ``None`` for a tool the ledger never records."""
    counts: dict[str, int | None] = {name: None for name in names}
    status: dict[str, Any] = {
        "path_name": ledger.name,
        "window_days": WINDOW_DAYS,
        "state": "absent",
        "entries_read": 0,
        "entries_malformed": 0,
        "entries_in_window": 0,
        "truncated": False,
    }
    try:
        present = ledger.is_file()
    except OSError as exc:
        status["state"] = "unreadable"
        status["error"] = type(exc).__name__
        return counts, status
    if not present:
        return counts, status
    known = set(names)
    recorded: set[str] = set()
    in_window: dict[str, int] = {}
    since = now - timedelta(days=WINDOW_DAYS)
    try:
        for line in _ledger_lines(ledger, max_bytes):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                status["entries_malformed"] += 1
                continue
            status["entries_read"] += 1
            tool = entry.get("tool_name") if isinstance(entry, Mapping) else None
            if not isinstance(tool, str) or tool not in known:
                continue
            recorded.add(tool)
            stamp = _parse_timestamp(entry.get("timestamp"))
            if stamp is not None and since <= stamp <= now:
                in_window[tool] = in_window.get(tool, 0) + 1
                status["entries_in_window"] += 1
    except OverflowError:
        status["truncated"] = True
    except OSError as exc:
        status["state"] = "unreadable"
        status["error"] = type(exc).__name__
        return {name: None for name in names}, status
    status["state"] = "read"
    status["tools_recorded"] = len(recorded)
    for tool in recorded:
        counts[tool] = in_window.get(tool, 0)
    return counts, status


def build_inventory(
    server: Any, metadata: Any, ledger: Path, *, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    names = _registered_names(server)
    surfaces = (metadata.MCP_PROFILE_ALL, *metadata.MCP_PROFILES)
    listings = {surface: _listing(server, surface) for surface in surfaces}
    all_bytes = {entry["name"]: entry_bytes(entry) for entry in listings[metadata.MCP_PROFILE_ALL]}
    counts, ledger_status = ledger_call_counts(ledger, names, now=now)
    facet_aliases = metadata.MCP_DEPRECATED_FACET_ALIASES
    tools = []
    for name in names:
        role = {
            profile: metadata.tool_profile_visibility(name, profile)
            for profile in metadata.MCP_PROFILES
            if metadata.tool_profile_visibility(name, profile) is not None
        }
        row: dict[str, Any] = {
            "name": name,
            "role": role,
            "profiled": name in metadata.MCP_TOOL_PROFILES,
            "subsystem": subsystem_prefix(name),
            "schema_bytes": all_bytes.get(name),
            "call_count_30d": counts.get(name),
        }
        if name in facet_aliases:
            row["deprecated_alias_of"] = facet_aliases[name]["surviving_tool"]
        if name in metadata.MCP_TOOL_ALIASES:
            row["duplicate_alias_of"] = metadata.MCP_TOOL_ALIASES[name]
        if name in metadata.MCP_FOLD_KEPT_REASONS:
            row["fold_kept_reason"] = metadata.MCP_FOLD_KEPT_REASONS[name]
        tools.append(row)
    totals = {
        surface: {
            "tool_count": len(entries),
            "schema_bytes": sum(entry_bytes(entry) for entry in entries),
        }
        for surface, entries in listings.items()
    }
    snapshot_readers = [
        {"name": row["name"], "role": row["role"], "schema_bytes": row["schema_bytes"]}
        for row in tools
        if row["name"].endswith("_snapshot")
    ]
    return {
        "schema_id": SCHEMA_ID,
        "generated_at": now.isoformat(),
        "backend": "mcp_sdk" if getattr(server, "_MCP_SDK_AVAILABLE", False) else "stdlib_fallback",
        "writes_allowed": os.environ.get("AIWORKHUB_ALLOW_WRITES", "0") == "1",
        "registered_tool_count": len(names),
        "totals": totals,
        "ledger": ledger_status,
        "deprecated_facet_aliases": {
            alias: {"surviving_tool": spec["surviving_tool"], "facet": dict(spec["facet"])}
            for alias, spec in facet_aliases.items()
        },
        "snapshot_readers": snapshot_readers,
        "tools": tools,
    }


def _scratch_target(repo: Path, out: str) -> Path:
    scratch = (repo / SCRATCH_DIRNAME).resolve()
    target = (scratch / out).resolve()
    if target == scratch or scratch not in target.parents:
        raise SystemExit(f"refused: --out {out!r} must name a file under {SCRATCH_DIRNAME}/")
    return target


def write_report(report: Mapping[str, Any], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, target)


def _print_table(report: Mapping[str, Any]) -> None:
    print(f"{'name':<56} {'role':<34} {'subsystem':<22} {'bytes':>7} {'calls30d':>8}")
    for row in report["tools"]:
        role = ",".join(f"{profile}:{vis}" for profile, vis in row["role"].items()) or "-"
        calls = "null" if row["call_count_30d"] is None else str(row["call_count_30d"])
        size = "-" if row["schema_bytes"] is None else str(row["schema_bytes"])
        print(f"{row['name']:<56} {role:<34} {row['subsystem']:<22} {size:>7} {calls:>8}")
    print(f"registered={report['registered_tool_count']} backend={report['backend']} writes_allowed={report['writes_allowed']}")
    for surface, total in report["totals"].items():
        print(f"surface={surface} tools_listed={total['tool_count']} tools_list_bytes={total['schema_bytes']}")
    print(f"ledger_state={report['ledger']['state']} entries_in_window={report['ledger']['entries_in_window']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repo", type=Path, default=_REPO_ROOT, help="repository whose ledger is read and whose .scratch/ receives the report")
    parser.add_argument("--src", type=Path, default=_REPO_ROOT / "src", help="directory holding the aiworkhub package to measure")
    parser.add_argument("--writes", choices=("on", "off"), default=None, help="register write-gated tools (on) or not (off); default: inherit AIWORKHUB_ALLOW_WRITES")
    parser.add_argument("--out", default=DEFAULT_REPORT_NAME, help=f"report file name under {SCRATCH_DIRNAME}/")
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    target = _scratch_target(repo, args.out)
    os.environ["AIWORKHUB_REPO_ROOT"] = str(repo)
    server, metadata, core = _import_server(args.src.resolve(), args.writes)
    report = build_inventory(server, metadata, core._audit_log_path(repo))
    write_report(report, target)
    _print_table(report)
    print(f"report={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
