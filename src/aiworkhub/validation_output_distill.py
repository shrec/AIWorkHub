"""Deterministic validation-output distiller.

Distills raw pytest/validation output into a compact, deterministic form:
preserving command, exit status, pass/fail/error/skip counts, failing test
node ids, repository-relative file:line frames, and assertion/exception
summary lines, while dropping non-repository frames (site-packages, _pytest,
pluggy, stdlib) unless they are the only frames available for a failure.

Fails closed: output that cannot be confidently parsed -- or whose extracted
failing node ids do not account for the failed/error count of the final
summary line -- falls back to the raw bounded tail unchanged, with
``distilled`` set to False. A non-zero exit status is never reported as a
pass, and a distilled output never hides a failure.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections import Counter
from dataclasses import dataclass

# Same bound as the reviewer packet's validation output tail; applied before
# any pattern runs so untrusted output cannot make the parser do unbounded work.
MAX_RAW_TAIL_CHARS = 4096
# Per-line cap applied before any per-line pattern runs.
MAX_PARSED_LINE_CHARS = 4000
# Per-failure budget: location lines and ``E`` lines kept next to each node id.
MAX_FRAMES_PER_FAILURE = 4
MAX_ASSERTION_LINES = 4
MAX_LINE_CHARS = 200

_SUMMARY_RE = re.compile(
    r"^(?P<body>\d+ [a-z]+(?:, \d+ [a-z]+)*) in \d+(?:\.\d+)?s(?: \(\d+:\d{2}:\d{2}\))?$"
)
_COUNT_ITEM_RE = re.compile(r"(?P<count>\d+) (?P<label>[a-z]+)")
_FRAME_RE = re.compile(r"^(?P<path>(?:[A-Za-z]:)?[^\s:]+\.py):(?P<line>\d+)")

_COUNT_LABELS = {
    "passed": "passed",
    "failed": "failed",
    "error": "errors",
    "errors": "errors",
    "skipped": "skipped",
    "xfailed": "xfailed",
    "xpassed": "xpassed",
}
_NODE_PREFIXES = ("FAILED ", "ERROR ")
_NON_REPO_MARKERS = ("site-packages", "_pytest", "pluggy", "/lib/python", "<frozen")
# Windows stdlib lives at <prefix>/Python312/Lib/<module>, with no "/lib/python" segment.
_WINDOWS_STDLIB_RE = re.compile(r"/python\d*/lib/")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_REPO_ROOT_MARKERS = (("src", "aiworkhub"), ("tests",))


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/")


def _is_repo_frame(path: str) -> bool:
    normalized = _normalize_path(path).lower()
    if _WINDOWS_STDLIB_RE.search(normalized):
        return False
    return not any(marker in normalized for marker in _NON_REPO_MARKERS)


@dataclass
class DistilledResult:
    """Deterministic distillation result for one validation run's output."""

    distilled: bool
    command: str
    exit_status: int | None
    counts: dict[str, int]
    failing_node_ids: list[str]
    frames_by_node: dict[str, list[str]]
    assertion_summaries: dict[str, str]
    raw_sha256: str
    raw_byte_length: int
    text: str

    def to_text(self) -> str:
        """Return the deterministic text form (distilled or raw-fallback)."""

        return self.text


def _bounded_lines(output: str) -> list[str]:
    return [line[:MAX_PARSED_LINE_CHARS] for line in output.splitlines()]


def _extract_counts(lines: list[str]) -> dict[str, int]:
    """Parse ONLY the final ``... in N.NNs`` summary line (``=`` decoration optional)."""

    for line in reversed(lines):
        candidate = line.strip().strip("=").strip()
        match = _SUMMARY_RE.match(candidate)
        if not match:
            continue
        counts: dict[str, int] = {}
        for item in _COUNT_ITEM_RE.finditer(match.group("body")):
            label = _COUNT_LABELS.get(item.group("label"))
            if label is not None:
                counts[label] = counts.get(label, 0) + int(item.group("count"))
        return counts
    return {}


def _parse_node_line(line: str) -> str | None:
    """Return the node id of a ``FAILED``/``ERROR`` summary line, else ``None``.

    The ``[...]`` parametrize part may hold any character, spaces included.
    """

    rest = ""
    for prefix in _NODE_PREFIXES:
        if line.startswith(prefix):
            rest = line[len(prefix) :]
            break
    if not rest or rest[0].isspace():
        return None
    depth = 0
    end = 0
    while end < len(rest):
        char = rest[end]
        if char == "[":
            depth += 1
        elif char == "]" and depth:
            depth -= 1
        elif char.isspace() and depth == 0:
            break
        end += 1
    node = rest[:end] if depth == 0 else rest.split(" - ", 1)[0].strip()
    return node or None


def _extract_failing_nodes(lines: list[str]) -> list[str]:
    nodes: list[str] = []
    for line in lines:
        node = _parse_node_line(line.strip())
        if node is not None and node not in nodes:
            nodes.append(node)
    return nodes


def _block_header_name(line: str) -> str | None:
    """Return the test name of a ``___ name ___`` failure block header."""

    stripped = line.strip()
    body = stripped.lstrip("_")
    if len(stripped) - len(body) < 3 or not body.startswith(" "):
        return None
    inner = body.rstrip("_")
    if len(body) - len(inner) < 3 or not inner.endswith(" "):
        return None
    return inner.strip() or None


def _split_failure_blocks(lines: list[str]) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    current: str | None = None
    buf: list[str] = []
    for line in lines:
        if line.strip().startswith("==="):
            # A "=== warnings summary ===" style section ends the last failure block.
            if current is not None:
                blocks.append((current, "\n".join(buf)))
            current = None
            buf = []
            continue
        name = _block_header_name(line)
        if name is not None:
            if current is not None:
                blocks.append((current, "\n".join(buf)))
            current = name
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        blocks.append((current, "\n".join(buf)))
    return blocks


def _repo_relative(path: str) -> str:
    """Return a repo-relative path; never a host prefix, user name or ``..``."""

    parts = posixpath.normpath(_normalize_path(path)).split("/")
    basename = parts[-1]
    if ".." in parts or not _is_repo_frame(path):
        return basename
    best = -1
    for marker in _REPO_ROOT_MARKERS:
        width = len(marker)
        for index in range(len(parts) - width):
            if tuple(parts[index : index + width]) == marker:
                best = max(best, index)
    if best >= 0:
        return "/".join(parts[best:])
    if parts[0] == "" or _DRIVE_RE.match(parts[0]):
        return basename
    return "/".join(parts)


def _extract_frames(block: str) -> tuple[list[str], list[str]]:
    """Return unique ``path:line: what`` lines as ``(repo, non_repo)``, in order."""

    repo: list[str] = []
    other: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        match = _FRAME_RE.match(stripped)
        if not match:
            continue
        rest = stripped[match.end() :].lstrip(": ").strip()
        frame = f"{_repo_relative(match.group('path'))}:{match.group('line')}"
        if rest:
            frame = f"{frame}: {rest}"[:MAX_LINE_CHARS]
        target = repo if _is_repo_frame(match.group("path")) else other
        if frame not in target:
            target.append(frame)
    return repo, other


def _assertion_lines(block: str) -> list[str]:
    """Return the block's ``E`` lines (bounded), else its last error-looking line."""

    lines = [
        line.strip()
        for line in block.splitlines()
        if line.strip().startswith(("E ", "E\t"))
    ]
    if not lines:
        for line in reversed(block.splitlines()):
            stripped = line.strip()
            if "Error" in stripped and ":" in stripped:
                lines = [stripped]
                break
    return [line[:MAX_LINE_CHARS] for line in lines[:MAX_ASSERTION_LINES]]


def _node_name(node: str) -> str:
    base = node.split("[", 1)[0]
    return base.rsplit("::", 1)[-1] + node[len(base) :]


def _unique_block_for_node(
    node: str, blocks: list[tuple[str, str]], name_counts: Counter[str]
) -> str | None:
    """Return the node's block only when the match is unambiguous.

    Blocks are keyed by header name alone, so two failing nodes sharing a
    function name, or two blocks matching one name, attach nothing.
    """

    name = _node_name(node)
    if name_counts[name] != 1:
        return None
    matches = [
        text for key, text in blocks if key == name or key.endswith("." + name)
    ]
    return matches[0] if len(matches) == 1 else None


def _raw_fallback(
    command: str,
    exit_status: int | None,
    raw_sha256: str,
    raw_byte_length: int,
    tail: str,
) -> DistilledResult:
    text = (
        f"command: {command}\n"
        f"exit_status: {exit_status}\n"
        "distilled: False\n"
        f"raw_sha256: {raw_sha256}\n"
        f"raw_byte_length: {raw_byte_length}\n"
        f"--- raw tail ---\n{tail}"
    )
    return DistilledResult(
        distilled=False,
        command=command,
        exit_status=exit_status,
        counts={},
        failing_node_ids=[],
        frames_by_node={},
        assertion_summaries={},
        raw_sha256=raw_sha256,
        raw_byte_length=raw_byte_length,
        text=text,
    )


def distill_validation_output(
    *,
    command: str,
    exit_status: int | None,
    output: str,
) -> DistilledResult:
    """Distill raw validation output into a compact deterministic form.

    Fails closed: when the final summary line cannot be found, when a failure
    signal has no parseable node id, or when the failed/error count differs
    from the number of extracted failing nodes, falls back to the raw bounded
    tail unchanged with ``distilled=False``. The raw sha256 and byte length
    are always recorded. A non-zero exit status is never reported as a pass.
    """

    raw_bytes = output.encode("utf-8", errors="replace")
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    raw_byte_length = len(raw_bytes)

    # Bound the untrusted input before any pattern runs.
    tail = output[-MAX_RAW_TAIL_CHARS:]
    lines = _bounded_lines(tail)

    counts = _extract_counts(lines)
    failing_nodes = _extract_failing_nodes(lines)
    failed_count = counts.get("failed", 0) + counts.get("errors", 0)
    # Fail open: every counted failure must be an extracted node id, and a
    # non-zero exit with no failure at all is an unknown failure.
    unexplained = failed_count != len(failing_nodes) or (
        exit_status not in (0, None) and not failed_count
    )
    if not counts or unexplained:
        return _raw_fallback(command, exit_status, raw_sha256, raw_byte_length, tail)

    blocks = _split_failure_blocks(lines)
    name_counts = Counter(_node_name(node) for node in failing_nodes)
    frames_by_node: dict[str, list[str]] = {}
    assertion_summaries: dict[str, str] = {}
    for node in failing_nodes:
        block = _unique_block_for_node(node, blocks, name_counts)
        frames_by_node[node] = []
        if block is None:
            continue
        repo_frames, other_frames = _extract_frames(block)
        frames_by_node[node] = (repo_frames or other_frames)[:MAX_FRAMES_PER_FAILURE]
        summary = _assertion_lines(block)
        if summary:
            assertion_summaries[node] = "\n".join(summary)

    counts_text = ", ".join(f"{label}={counts[label]}" for label in sorted(counts))
    out = [f"command: {command}"] if command else []
    out += [
        f"exit_status: {exit_status}",
        "distilled: True",
        f"counts: {counts_text}",
        f"raw_sha256: {raw_sha256}",
        f"raw_byte_length: {raw_byte_length}",
    ]
    if failing_nodes:
        out.append("failing_node_ids:")
        for node in failing_nodes:
            out.append(f"  - {node}")
            out.extend(f"      {frame}" for frame in frames_by_node[node])
            summary_text = assertion_summaries.get(node)
            if summary_text:
                out.extend(f"      {line}" for line in summary_text.splitlines())

    return DistilledResult(
        distilled=True,
        command=command,
        exit_status=exit_status,
        counts=counts,
        failing_node_ids=failing_nodes,
        frames_by_node=frames_by_node,
        assertion_summaries=assertion_summaries,
        raw_sha256=raw_sha256,
        raw_byte_length=raw_byte_length,
        text="\n".join(out),
    )


def distill_tail(
    value: object, *, command: str = "", exit_status: int | None = None
) -> tuple[str, bool]:
    """Distill one raw stdout/stderr tail for packet embedding.

    Returns ``(text, distilled)``. Fails closed: when the output cannot be
    confidently parsed, returns the raw text unchanged with ``distilled=False``.
    """

    text = value if isinstance(value, str) else ""
    if not text.strip():
        return text, False
    result = distill_validation_output(
        command=command, exit_status=exit_status, output=text
    )
    # Never grow the output: a digest not smaller than the raw text adds nothing.
    if not result.distilled or len(result.text) >= len(text):
        return text, False
    return result.to_text(), True
