"""Read-only Claude manager-route probe (RM-2026-00082).

One walk answers both "is this MCP server a verified Claude Code VS Code
manager" and, when it is not, which step refused.  ``core._claude_manager_identity``
and ``core._claude_windows_manager_identity`` return ``probe.identity``, so the
identity and the diagnosis can never disagree, and ``manager_bootstrap``
reports ``probe.report()`` whenever the resolved provider is not Claude.

Every platform seam (``_windows_process_tree``, ``_windows_process_owner_sid``,
``repo_root``, ``_UUID_RE``) is looked up on ``core`` at call time, so the
monkeypatch points existing tests use keep working.

Observations are bounded, non-secret facts: hop image basenames, the hop
count, the descriptor ``kind``/``entrypoint`` truncated to 64 characters and
whether the descriptor cwd matched this repository.  Never the descriptor
body, the session id or any path.
"""

from __future__ import annotations

import json
import ntpath
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import platform_io

# How many of THIS interpreter's own re-exec stubs may sit between the MCP
# server and the Claude process that owns it. A Windows venv contributes exactly
# one (Scripts\python.exe re-executes the base interpreter); the limit is two so
# a launcher shim may add one more, and no further, before the chain is refused.
CLAUDE_LAUNCHER_HOP_LIMIT = 2

# NF-2026-01816: the stable launcher (NF-2026-01357) runs the server as its own
# CHILD to hot-swap runtime generations, so on Windows the chain is claude.exe ->
# venv stub -> supervisor -> venv stub -> server. The supervisor and its stub are
# two more hops, granted only to the process the supervisor marked as its server
# (AIWORKHUB_MCP_SUPERVISED == own pid), never to a child that inherits the mark.
SUPERVISOR_HOPS = 2

# NF-2026-01005: the official Windows Python launcher. ``"command": "py"`` in a
# ``.mcp.json`` keeps ``py.exe`` alive as the parent of the interpreter it
# spawns, so it is a re-exec hop exactly like the venv redirector. Matched by
# exact image name only, and it still counts against the hop limit.
WINDOWS_PYTHON_LAUNCHER_IMAGES = frozenset({"py.exe", "pyw.exe"})

# The Codex fail-closed guard looks further than the seat walk: ANY same-user
# ancestor named claude within this many hops means the process belongs to a
# Claude chat, whatever sits in between (a shell, cmd.exe, a launcher).
CLAUDE_ANCESTRY_SCAN_LIMIT = 8

OBSERVATION_MAX_CHARS = 64

# Stable refusal vocabulary, in walk order; ``verified`` is the only success.
REASONS = (
    "no_parent",
    "owner_sid_mismatch",
    "hop_limit_exceeded",
    "foreign_hop_image",
    "no_claude_ancestor",
    "descriptor_unreadable",
    "descriptor_pid_mismatch",
    "descriptor_kind",
    "descriptor_entrypoint",
    "descriptor_session_id",
    "descriptor_cwd_mismatch",
    "verified",
)


@dataclass(frozen=True)
class ClaudeRouteProbe:
    identity: dict[str, str] | None
    reason: str
    observations: dict[str, Any] = field(default_factory=dict)

    def report(self) -> dict[str, Any]:
        """The ``claude_route_probe`` block bootstrap publishes."""
        return {"reason": self.reason, "observations": dict(self.observations)}


def _core() -> Any:
    from . import core  # local import: core imports this module

    return core


def _windows() -> bool:
    return platform_io.is_windows()


def _bounded(value: object) -> str:
    return value[:OBSERVATION_MAX_CHARS] if isinstance(value, str) else ""


def descriptor_probe(
    owner_pid: int, descriptor: object, observations: dict[str, Any] | None = None
) -> ClaudeRouteProbe:
    """Validate a Claude per-PID session descriptor and bind it to this repo.

    Shared by the POSIX ``/proc`` path and the Windows-native path so both
    fail closed on the same malformed, foreign or stale descriptor evidence.
    """
    seen: dict[str, Any] = observations if observations is not None else {}
    if not isinstance(descriptor, dict):
        return ClaudeRouteProbe(None, "descriptor_unreadable", seen)
    seen["descriptor_kind"] = _bounded(descriptor.get("kind"))
    seen["descriptor_entrypoint"] = _bounded(descriptor.get("entrypoint"))
    try:
        pid_matches = int(descriptor.get("pid") or -1) == owner_pid
    except (TypeError, ValueError):
        pid_matches = False
    if not pid_matches:
        return ClaudeRouteProbe(None, "descriptor_pid_mismatch", seen)
    if descriptor.get("kind") != "interactive":
        return ClaudeRouteProbe(None, "descriptor_kind", seen)
    if descriptor.get("entrypoint") != "claude-vscode":
        return ClaudeRouteProbe(None, "descriptor_entrypoint", seen)
    core = _core()
    session_id = str(descriptor.get("sessionId") or "").strip()
    if not core._UUID_RE.fullmatch(session_id):
        return ClaudeRouteProbe(None, "descriptor_session_id", seen)
    try:
        cwd_matches = Path(str(descriptor.get("cwd") or "")).resolve() == core.repo_root()
    except (OSError, RuntimeError):
        cwd_matches = False
    seen["cwd_matches"] = cwd_matches
    if not cwd_matches:
        return ClaudeRouteProbe(None, "descriptor_cwd_mismatch", seen)
    identity = {
        "provider": "claude",
        "session_id": session_id,
        "window_id": f"claude_vscode_{owner_pid}",
    }
    return ClaudeRouteProbe(identity, "verified", seen)


def claude_descriptor_identity(parent_pid: int, descriptor: object) -> dict[str, str] | None:
    return descriptor_probe(parent_pid, descriptor).identity


def _read_descriptor(owner_pid: int, observations: dict[str, Any]) -> ClaudeRouteProbe:
    try:
        descriptor_path = Path.home() / ".claude" / "sessions" / f"{owner_pid}.json"
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ClaudeRouteProbe(None, "descriptor_unreadable", observations)
    return descriptor_probe(owner_pid, descriptor, observations)


def probe_claude_windows_route() -> ClaudeRouteProbe:
    """Windows-native Claude Code VS Code manager verification.

    There is no ``/proc`` on Windows. Confirm the owning Claude process through
    a native Toolhelp snapshot (same-user SID at every hop and a live ``claude``
    image name), then validate ``~/.claude/sessions/<pid>.json`` exactly like
    the POSIX path. Missing, malformed, foreign, stale or unverifiable evidence
    fails closed.

    The chain is walked rather than read one level deep because a Windows venv
    ``Scripts\\python.exe`` is a REDIRECTOR: it re-executes the base interpreter
    as a separate process, so this server's direct parent is that stub and never
    ``claude.exe``. Measured: pid 2652 (the server) <- pid 38272 (the venv stub)
    <- pid 29612 (claude.exe, whose session descriptor validates cleanly).
    POSIX has no such hop, because ``.venv/bin/python`` is a symlink the loader
    follows in place.

    The official Windows Python launcher is the other such hop (NF-2026-01005):
    ``"command": "py"`` makes ``py.exe`` spawn ``python.exe`` as a CHILD and
    stay alive as its parent, so the chain is claude.exe -> py.exe -> python.exe.

    Only a hop that is THIS interpreter's own image name or exactly ``py.exe``
    / ``pyw.exe``, owned by the same user, is skipped, and at most
    ``CLAUDE_LAUNCHER_HOP_LIMIT`` of them, plus ``SUPERVISOR_HOPS`` for the
    server the stable launcher supervises (NF-2026-01816): the evidence that
    grants the seat is still one exact ``claude`` ancestor with a valid,
    repository-bound descriptor.
    """
    core = _core()
    hops: list[str] = []
    observations: dict[str, Any] = {"platform": "windows", "hop_count": 0, "hop_images": hops}

    def refuse(reason: str) -> ClaudeRouteProbe:
        observations["hop_count"] = len(hops)
        return ClaudeRouteProbe(None, reason, observations)

    parent_pid = os.getppid()
    if parent_pid <= 1:
        return refuse("no_parent")
    current_sid = core._windows_process_owner_sid(os.getpid())
    if current_sid is None:
        return refuse("owner_sid_mismatch")
    tree = core._windows_process_tree()
    if tree is None:
        return refuse("no_parent")
    interpreter_image = ntpath.basename(sys.executable).lower()
    supervised = os.environ.get("AIWORKHUB_MCP_SUPERVISED") == str(os.getpid())
    observations["supervised"] = supervised
    hop_limit = CLAUDE_LAUNCHER_HOP_LIMIT + (SUPERVISOR_HOPS if supervised else 0)
    candidate = parent_pid
    for _hop in range(hop_limit + 1):
        entry = tree.get(candidate)
        if entry is None:
            return refuse("no_claude_ancestor" if hops else "no_parent")
        candidate_parent, candidate_image = entry
        image = str(candidate_image or "").lower()
        hops.append(_bounded(ntpath.basename(image)))
        if core._windows_process_owner_sid(candidate) != current_sid:
            return refuse("owner_sid_mismatch")
        if not image:
            return refuse("foreign_hop_image")
        if "claude" in image:
            observations["hop_count"] = len(hops)
            return _read_descriptor(candidate, observations)
        if image != interpreter_image and image not in WINDOWS_PYTHON_LAUNCHER_IMAGES:
            # Not our own re-exec stub or the py launcher: this chain does not
            # belong to Claude.
            return refuse("foreign_hop_image")
        candidate = candidate_parent
    return refuse("hop_limit_exceeded")


def _proc_read(pid: int, name: str) -> bytes:
    return Path(f"/proc/{pid}/{name}").read_bytes()


def _probe_posix_route() -> ClaudeRouteProbe:
    observations: dict[str, Any] = {"platform": "posix", "hop_count": 0, "hop_images": []}
    parent_pid = os.getppid()
    supervised = os.environ.get("AIWORKHUB_MCP_SUPERVISED") == str(os.getpid())
    observations["supervised"] = supervised
    try:
        if supervised:
            # NF-2026-01816: the stable launcher's supervisor is the parent of
            # the server it marked; claude is the supervisor's own parent.
            status = _proc_read(parent_pid, "status").decode("utf-8")
            parent_pid = int(next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:")))
        raw = _proc_read(parent_pid, "cmdline")
        cmdline = raw.replace(b"\0", b" ").decode("utf-8")
    except (OSError, UnicodeDecodeError, StopIteration, ValueError, IndexError):
        return ClaudeRouteProbe(None, "no_parent", observations)
    observations["hop_count"] = 1
    observations["hop_images"] = [_bounded(PurePosixPath(cmdline.split(" ", 1)[0]).name)]
    if "/claude " not in cmdline:
        return ClaudeRouteProbe(None, "no_claude_ancestor", observations)
    return _read_descriptor(parent_pid, observations)


def probe_claude_route() -> ClaudeRouteProbe:
    """The Claude manager-route verification for this host, with its reason."""
    return probe_claude_windows_route() if _windows() else _probe_posix_route()


def _posix_claude_ancestry_present() -> bool:
    pid = os.getppid()
    for _ in range(CLAUDE_ANCESTRY_SCAN_LIMIT):
        if pid <= 1:
            return False
        proc = Path(f"/proc/{pid}")
        try:
            if proc.stat().st_uid != os.geteuid():
                return False
            argv = proc.joinpath("cmdline").read_bytes().split(b"\0")[:2]
            status = proc.joinpath("status").read_text(encoding="utf-8")
            parent = int(next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:")))
        except (OSError, UnicodeDecodeError, StopIteration, ValueError, IndexError):
            return True  # unreadable ancestry cannot prove the absence of Claude
        if any("claude" in PurePosixPath(arg.decode("utf-8", "replace")).name.lower() for arg in argv):
            return True
        pid = parent
    return False


def claude_ancestry_present() -> bool:
    """True when this process's same-user bounded ancestry contains a claude image.

    The Codex routes that do not themselves prove a Codex parent (shared
    registry, extension route, VS Code env) consult this before granting Codex:
    a Claude Code chat's MCP is never a Codex manager, whatever the shared
    registry says. Unreadable ancestry fails closed (True); the walk stops at
    the first process owned by another user.
    """
    if not _windows():
        return _posix_claude_ancestry_present()
    core = _core()
    current_sid = core._windows_process_owner_sid(os.getpid())
    tree = core._windows_process_tree()
    if current_sid is None or tree is None:
        return True
    pid = os.getppid()
    for _ in range(CLAUDE_ANCESTRY_SCAN_LIMIT):
        entry = tree.get(pid)
        if pid <= 1 or entry is None or core._windows_process_owner_sid(pid) != current_sid:
            return False
        if "claude" in str(entry[1] or "").lower():
            return True
        pid = entry[0]
    return False
