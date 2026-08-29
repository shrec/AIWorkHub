"""Provider-output read-efficiency parsing for process launcher evidence."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any

from . import read_efficiency


def _strict_read_command_event(
    command: Any,
    output: Any,
    *,
    timestamp: float,
) -> dict[str, Any] | None:
    """Recognize a small, explicit set of shell-free-equivalent reads.

    This parser is observability-only: it never executes provider text.  It
    deliberately ignores pipelines, compound commands and ambiguous shell
    syntax instead of guessing.  POSIX shell wrappers and PowerShell
    ``-Command`` wrappers are unwrapped once, then only exact ``sed -n``,
    ``head -n``, ``cat`` and ``Get-Content`` shapes are accepted.
    """

    if not isinstance(command, str) or not command.strip():
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    executable = Path(tokens[0]).name.lower()
    if executable in {"bash", "sh", "zsh"}:
        try:
            flag_index = next(
                index for index, token in enumerate(tokens) if token in {"-c", "-lc"}
            )
            tokens = shlex.split(tokens[flag_index + 1], posix=True)
        except (StopIteration, IndexError, ValueError):
            return None
    elif executable in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        try:
            command_index = next(
                index for index, token in enumerate(tokens)
                if token.lower() in {"-command", "-c"}
            )
            tokens = shlex.split(tokens[command_index + 1], posix=True)
        except (StopIteration, IndexError, ValueError):
            return None
    path = ""
    offset: int | None = None
    limit: int | None = None
    drop_output_first_line = False
    composite_read = False
    # Codex commonly pairs an exact line-count probe with one bounded sed read
    # of the same declared path.  Recognize only that exact, side-effect-free
    # compound shape; every other compound command remains unclassified.
    if (
        len(tokens) == 8
        and Path(tokens[0]).name.lower() == "wc"
        and tokens[1] == "-l"
        and tokens[3] == "&&"
        and Path(tokens[4]).name.lower() == "sed"
        and tokens[5] == "-n"
        and tokens[2] == tokens[7]
    ):
        match = re.fullmatch(r"(\d+),(\d+)p", tokens[6])
        if not match:
            return None
        start, end = int(match.group(1)), int(match.group(2))
        if start < 1 or end < start:
            return None
        path, offset, limit = tokens[7], start, end - start + 1
        drop_output_first_line = True
        composite_read = True
    elif not tokens or any(
        token in {"|", ";", "&&", "||", ">", ">>"} for token in tokens
    ):
        return None

    range_unit = "lines"
    executable = Path(tokens[0]).name.lower()
    if composite_read:
        pass
    elif executable == "sed" and len(tokens) == 4 and tokens[1] == "-n":
        match = re.fullmatch(r"(\d+),(\d+)p", tokens[2])
        if not match:
            return None
        start, end = int(match.group(1)), int(match.group(2))
        if start < 1 or end < start:
            return None
        path, offset, limit = tokens[3], start, end - start + 1
    elif executable == "head" and len(tokens) == 4 and tokens[1] in {"-n", "--lines"}:
        try:
            limit = int(tokens[2])
        except ValueError:
            return None
        if limit <= 0:
            return None
        path, offset = tokens[3], 1
    elif executable == "cat" and len(tokens) == 2:
        path = tokens[1]
        range_unit = "file"
    elif executable in {"get-content", "gc"}:
        remaining = tokens[1:]
        if "-Path" in remaining:
            path_index = remaining.index("-Path") + 1
        elif "-LiteralPath" in remaining:
            path_index = remaining.index("-LiteralPath") + 1
        else:
            path_index = 0
        if path_index >= len(remaining):
            return None
        path = remaining[path_index]
        lowered = [token.lower() for token in remaining]
        if "-totalcount" in lowered:
            count_index = lowered.index("-totalcount") + 1
            try:
                limit = int(remaining[count_index])
            except (IndexError, ValueError):
                return None
            if limit <= 0:
                return None
            offset = 1
    else:
        return None

    if not path or path.startswith("-"):
        return None
    output_text = output if isinstance(output, str) else ""
    if drop_output_first_line:
        _line_count, separator, after_first_line = output_text.partition("\n")
        if not separator:
            return None
        output_text = after_first_line
    encoded = output_text.encode("utf-8")
    return {
        "event_type": "read",
        "path": path,
        "offset": offset,
        "limit": limit,
        "range_unit": range_unit,
        "content_sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes_returned": len(encoded),
        "timestamp": timestamp,
        "classification_source": "strict_provider_command_shape",
    }


def _provider_read_efficiency_from_output(path: Path) -> dict[str, Any]:
    """Return a bounded, path-free read-efficiency summary from JSON output.

    Only explicit provider tool-use records and strict read-command shapes are
    accepted.  Raw commands, paths and contents never leave this function.
    Missing provider evidence is labelled unobserved rather than reported as
    a measured zero.
    """

    empty = {
        "schema_id": "aiworkhub.provider_read_efficiency.v2",
        "evidence_observed": False,
        "provider_records_scanned": 0,
        "recognized_read_events": 0,
        "recognized_source_graph_events": 0,
        "measurement_label": "observed_provider_events_and_bytes_only_no_token_or_cost_claim",
    }
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
            return empty
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return empty

    candidates: list[Any] = []
    try:
        candidates.append(json.loads(raw))
    except json.JSONDecodeError:
        for line in raw.splitlines()[:20000]:
            try:
                candidates.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    events: list[dict[str, Any]] = []
    pending_reads: dict[str, dict[str, Any]] = {}
    read_tool_names = {"read", "read_file", "readfile", "file_read"}

    def tool_payload(node: dict[str, Any]) -> dict[str, Any]:
        for key in ("input", "arguments", "tool_input", "args"):
            value = node.get(key)
            if isinstance(value, dict):
                return value
        return {}

    def walk(node: Any, *, ordinal: int, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            node_type = str(node.get("type") or "").strip().lower()
            name = str(
                node.get("name") or node.get("tool_name") or node.get("tool") or ""
            ).strip()
            normalized_name = name.lower().replace("-", "_")
            payload = tool_payload(node)
            if node_type in {"tool_use", "tool_call", "mcp_tool_call"}:
                if str(node.get("status") or "").lower() == "in_progress":
                    return
                if "source_graph_query" in normalized_name:
                    events.append({
                        "event_type": "source_graph",
                        "source_graph_mode": str(payload.get("mode") or "")[:40],
                        "source_graph_timestamp": float(ordinal),
                    })
                elif normalized_name in read_tool_names:
                    read_path = payload.get("file_path") or payload.get("path")
                    if read_path:
                        event = {
                            "event_type": "read",
                            "path": str(read_path),
                            "offset": payload.get("offset"),
                            "limit": payload.get("limit"),
                            "timestamp": float(ordinal),
                            "classification_source": "provider_read_tool",
                        }
                        events.append(event)
                        tool_id = str(node.get("id") or node.get("tool_use_id") or "")
                        if tool_id:
                            pending_reads[tool_id] = event
            if node_type == "tool_result":
                tool_id = str(node.get("tool_use_id") or node.get("id") or "")
                pending_event = pending_reads.get(tool_id)
                content = node.get("content")
                if pending_event is not None and content is not None:
                    if isinstance(content, str):
                        result_text = content
                    else:
                        try:
                            result_text = json.dumps(
                                content, ensure_ascii=False, sort_keys=True,
                            )
                        except (TypeError, ValueError):
                            result_text = ""
                    encoded = result_text.encode("utf-8")
                    pending_event["content_sha256"] = hashlib.sha256(encoded).hexdigest()
                    pending_event["bytes_returned"] = len(encoded)
            for value in list(node.values())[:256]:
                walk(value, ordinal=ordinal, depth=depth + 1)
        elif isinstance(node, list):
            for value in node[:256]:
                walk(value, ordinal=ordinal, depth=depth + 1)

    for ordinal, candidate in enumerate(candidates[:20000]):
        if isinstance(candidate, dict):
            item = candidate.get("item")
            # Codex emits the same command twice: ``item.started`` carries an
            # empty result and ``item.completed`` carries the authoritative
            # output.  Counting both inflates reads and fabricates an unknown
            # repetition for every successful command.
            if (
                str(candidate.get("type") or "") == "item.completed"
                and isinstance(item, dict)
                and str(item.get("type") or "") == "command_execution"
            ):
                event = _strict_read_command_event(
                    item.get("command"), item.get("aggregated_output"),
                    timestamp=float(ordinal),
                )
                if event is not None:
                    events.append(event)
            walk(candidate, ordinal=ordinal)

    read_events = [event for event in events if event.get("event_type") == "read"]
    graph_events = [
        event for event in events if event.get("event_type") == "source_graph"
    ]
    report = read_efficiency.analyze_read_efficiency(
        events, correlation_window=64,
    ).to_dict()
    report.pop("events", None)
    return {
        **empty,
        **report,
        "evidence_observed": bool(read_events or graph_events),
        "provider_records_scanned": len(candidates),
        "recognized_read_events": len(read_events),
        "recognized_source_graph_events": len(graph_events),
    }
