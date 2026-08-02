"""B833 dynamic, task-scoped worker MCP tool surface.

This module is a SEPARATE, minimal stdio MCP server -- distinct from the
coordinator's ``server.py`` (AIWorkHub MCP). It is what the parent launcher
(``process_launcher.py`` + ``worker_workspace.py``) injects into a claimed
worker's Claude/Codex/Copilot process so the model can make bounded, live
calls for Source Graph discovery, Session Manager current-state, AI Memory
search and KB lookups WITHOUT shelling out to ``AITools/*.py`` and WITHOUT any
access to task mutation, process launch, coordinator state, arbitrary shell,
or a caller-selected repository/path.

Hard invariants:
  * Every tool call is bound to an immutable ``task_id`` / ``runner`` /
    ``topic`` / ``request_id`` / ``repo`` supplied via environment variables
    at process start (``load_context_from_env``). No tool function accepts a
    caller-supplied repository path, database path, or task identity -- the
    worker-controlled arguments are limited to query/mode/budget/target
    (target itself is validated against the coordinator-declared allowlist
    when one was declared).
  * No task mutation, process launch, or coordinator tool is exposed here.
    Those live only in ``server.py`` / ``process_launcher.py`` and are never
    imported by this module.
  * Every call is recorded to a per-request, HMAC-authenticated, append-only
    audit ledger (``_append_audit``). ``verify_audit_ledger`` independently
    re-verifies every entry's HMAC using the same per-request secret before
    counting it -- a worker cannot fabricate a "live call" record merely by
    writing text that looks like one, because it cannot forge the HMAC
    without the secret, and any tampered line is dropped by the verifier
    rather than trusted.
  * No tool ever shells out to a script or spawns a subprocess: Source Graph,
    Session Manager, AI Memory and KB are all queried in-process (direct
    ``sqlite3`` reads against the canonical registry-resolved database, or a
    direct call into ``aiworkhub.source_graph``), bounded and output-capped,
    mirroring the bounded-context pattern already used by
    ``project_context.py`` for the coordinator's own precomputed bundle.

Binding note (B834 authority repair): this module receives TWO separate,
immutable repository bindings, never one conflated path:

  * ``ENV_REPO`` / ``ctx.repo`` -- the isolated writable worktree
    (``workspace.path``). A full ``git worktree add --detach`` checkout, so
    every ``AITools/*.py`` script is present there, but its own SQLite index
    databases (``source_graph.db``, ``session.db``, ``ai_memory.db``) are
    gitignored/absent in a fresh worktree. Retained for identity/consistency
    checks; not used to source authoritative index data.
  * ``ENV_AUTHORITY_REPO`` / ``ctx.authority_repo`` -- the coordinator-owned
    host repository the worktree was created FROM. Every Source Graph,
    Session Manager, AI Memory and KB call in this module resolves its
    database path from that repository's own ``.aiworkhub`` canonical
    registry and reads it directly (in-process ``sqlite3``, read-only) --
    never by shelling out to ``AITools/*.py``. B878 removed the last
    subprocess-based lookups: ``AITools/ai_memory/ai_memory.py`` resolves its
    database relative to its own ``__file__`` with no path-override flag, so
    it could never be pointed at the canonical
    ``.aiworkhub/memory/memory.sqlite`` location once a repository has no
    adjacent ``AITools/ai_memory/ai_memory.db`` at all -- a canonical-only
    repository could never satisfy that script's hardcoded path, so it had to
    stop being invoked altogether, not just be pointed at a different file.

Authority-state discipline: before running any query, this module resolves
the target database path from the coordinator-owned
``.aiworkhub/config/storage.json`` registry (read from ``ctx.authority_repo``,
never from ``ctx.repo``) and requires the resolved file to actually exist and
be non-empty -- a missing or empty database is a hard, fail-closed violation,
never silently substituted with a fresh/empty one and never counted as a
successful live call. Resolution is canonical-only: a component's registry
entry must be marked ``authority.canonical_active`` true, or the lookup fails
closed -- there is no ``legacy_source`` fallback and no automatic legacy
discovery. The resolved ``authority_source`` (always ``"canonical"`` for a
successful lookup) and ``authority_state`` are recorded on both the tool
result and the audit entry so the ledger records real authority identity, not
just a bare ok/fail bit.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import errno
import os
import re
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

try:
    from .platform_io import chmod_fd
except ImportError:  # minimal copied worker package / direct-script mode
    def chmod_fd(fd: int, mode: int) -> None:
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            fchmod(fd, mode)

from .repository_state import RepositoryStateError


SERVER_NAME = "aiworkhub_worker_ai_tools"

AUDIT_ENTRY_SCHEMA_ID = "aiworkhub.task_mcp.worker_mcp_audit_entry.v1"
AUDIT_LEDGER_VERIFICATION_SCHEMA_ID = "aiworkhub.task_mcp.worker_mcp_audit_verification.v1"
RUNTIME_SCHEMA_ID = "aiworkhub.task_mcp.worker_mcp_runtime.v1"

ENV_TASK_ID = "AIWORKHUB_WORKER_MCP_TASK_ID"
ENV_RUNNER = "AIWORKHUB_WORKER_MCP_RUNNER"
ENV_TOPIC = "AIWORKHUB_WORKER_MCP_TOPIC"
ENV_REQUEST_ID = "AIWORKHUB_WORKER_MCP_REQUEST_ID"
ENV_REPO = "AIWORKHUB_WORKER_MCP_REPO"
ENV_AUTHORITY_REPO = "AIWORKHUB_WORKER_MCP_AUTHORITY_REPO"
ENV_SOURCE_GRAPH_TARGETS = "AIWORKHUB_WORKER_MCP_SOURCE_GRAPH_TARGETS"
ENV_SESSION_TOPIC = "AIWORKHUB_WORKER_MCP_SESSION_TOPIC"
ENV_AUDIT_LEDGER_PATH = "AIWORKHUB_WORKER_MCP_AUDIT_LEDGER_PATH"
ENV_AUDIT_HMAC_KEY_PATH = "AIWORKHUB_WORKER_MCP_AUDIT_HMAC_KEY_PATH"
ENV_QUALITY_REVIEW_PACKET_PATH = "AIWORKHUB_WORKER_MCP_QUALITY_REVIEW_PACKET_PATH"
# The interpreter's own import-path variable (never an AIWORKHUB_* identity
# binding) -- carries the portable ".../src" import root so `python -m
# aiworkhub.worker_ai_tools_mcp` resolves regardless of the launcher's cwd.
ENV_PYTHONPATH = "PYTHONPATH"

BOUND_ENV_VARS: tuple[str, ...] = (
    ENV_TASK_ID, ENV_RUNNER, ENV_TOPIC, ENV_REQUEST_ID, ENV_REPO, ENV_AUTHORITY_REPO,
    ENV_SOURCE_GRAPH_TARGETS, ENV_SESSION_TOPIC,
    ENV_AUDIT_LEDGER_PATH, ENV_AUDIT_HMAC_KEY_PATH,
)

STORAGE_REGISTRY_RELATIVE_PATH = Path(".aiworkhub") / "config" / "storage.json"

SOURCE_GRAPH_MODES: tuple[str, ...] = (
    "focus", "slice", "context", "file", "function", "class", "body", "bodygrep",
    "impact", "trace", "deps", "bundle",
    "tags", "hotspots", "coverage", "churn", "reviewqueue", "ownership",
    "testmap", "calls", "symbols", "bottlenecks", "auditmap", "complexity",
    "stats", "summarize", "pipeline",
    "todo", "leaks", "nullrisks", "rawptrs", "casts", "crashes",
    "looprisks", "deadmethods", "duplicates", "gaps",
)
SOURCE_GRAPH_BUNDLE_TYPES: tuple[str, ...] = (
    "bugfix", "feature", "refactor", "audit", "optimize", "explore",
)
SourceGraphMode = Literal[
    "focus", "slice", "context", "file", "function", "class", "body", "bodygrep",
    "impact", "trace", "deps", "bundle",
    "tags", "hotspots", "coverage", "churn", "reviewqueue", "ownership",
    "testmap", "calls", "symbols", "bottlenecks", "auditmap", "complexity",
    "stats", "summarize", "pipeline",
    "todo", "leaks", "nullrisks", "rawptrs", "casts", "crashes",
    "looprisks", "deadmethods", "duplicates", "gaps",
]
SourceGraphBundleType = Literal["bugfix", "feature", "refactor", "audit", "optimize", "explore"]
WORKFLOW_STAGES: tuple[str, ...] = (
    "orientation", "implementation", "validation", "review", "rework", "unspecified",
)
WorkflowStage = Literal[
    "orientation", "implementation", "validation", "review", "rework", "unspecified",
]
MAX_QUERY_BYTES = 512
MAX_KEY_BYTES = 256
MIN_BUDGET = 8
MAX_BUDGET = 160
MIN_LIMIT = 1
MAX_LIMIT = 20
MAX_TOOL_OUTPUT_BYTES = 16 * 1024
MAX_RAW_TOOL_OUTPUT_BYTES = 512 * 1024
MAX_DECLARED_INPUT_PREVIEW_BYTES = 12 * 1024
MAX_DECLARED_INPUT_HASH_BYTES = 8 * 1024 * 1024
MAX_QUALITY_REVIEW_PACKET_BYTES = 256 * 1024
MAX_QUALITY_REVIEW_FINDINGS = 50
SQLITE_QUERY_TIMEOUT_SECONDS = 5
SESSION_SNIPPET_CHARS = 280


class WorkerToolError(RuntimeError):
    """A bounded, fail-closed worker MCP tool failure."""


@dataclass(frozen=True, slots=True)
class WorkerToolContext:
    """Immutable per-request binding read once from the injected environment."""

    task_id: str
    runner: str
    topic: str
    request_id: str
    repo: Path
    authority_repo: Path
    source_graph_targets: tuple[str, ...]
    session_topic: str
    audit_ledger_path: Path | None
    audit_hmac_key_path: Path | None
    quality_review_packet_path: Path | None = None


def load_context_from_env(env: Any = None) -> WorkerToolContext:
    """Bind this server's tool calls to the coordinator-injected identity.

    Fails closed (raises ``WorkerToolError``) when any required identity
    variable is absent -- a worker MCP server must never fall back to a
    guessed or ambient repository/task identity. ``repo`` (isolated worktree)
    and ``authority_repo`` (coordinator-owned host repository) are two
    separate required bindings; neither has a fallback to the other.
    """

    source = env if env is not None else os.environ
    missing = [
        name for name in (ENV_TASK_ID, ENV_RUNNER, ENV_TOPIC, ENV_REPO, ENV_AUTHORITY_REPO)
        if not source.get(name)
    ]
    if missing:
        raise WorkerToolError(f"worker_mcp_context_missing:{','.join(missing)}")
    repo = Path(str(source[ENV_REPO])).resolve()
    if not repo.is_dir():
        raise WorkerToolError(f"worker_mcp_repo_not_directory:{repo}")
    authority_repo = Path(str(source[ENV_AUTHORITY_REPO])).resolve()
    if not authority_repo.is_dir():
        raise WorkerToolError(f"worker_mcp_authority_repo_not_directory:{authority_repo}")
    targets_raw = str(source.get(ENV_SOURCE_GRAPH_TARGETS) or "[]")
    try:
        targets_parsed = json.loads(targets_raw)
    except json.JSONDecodeError:
        targets_parsed = []
    targets = tuple(
        str(item) for item in targets_parsed if isinstance(item, str) and item.strip()
    ) if isinstance(targets_parsed, list) else ()
    ledger_raw = source.get(ENV_AUDIT_LEDGER_PATH) or ""
    key_raw = source.get(ENV_AUDIT_HMAC_KEY_PATH) or ""
    review_packet_raw = source.get(ENV_QUALITY_REVIEW_PACKET_PATH) or ""
    return WorkerToolContext(
        task_id=str(source[ENV_TASK_ID]),
        runner=str(source[ENV_RUNNER]),
        topic=str(source[ENV_TOPIC]),
        request_id=str(source.get(ENV_REQUEST_ID) or ""),
        repo=repo,
        authority_repo=authority_repo,
        source_graph_targets=targets,
        session_topic=str(source.get(ENV_SESSION_TOPIC) or source[ENV_TOPIC]),
        audit_ledger_path=Path(str(ledger_raw)) if ledger_raw else None,
        audit_hmac_key_path=Path(str(key_raw)) if key_raw else None,
        quality_review_packet_path=(
            Path(str(review_packet_raw)) if review_packet_raw else None
        ),
    )


# ---------------------------------------------------------------------------
# Bounded, read-only, in-process SQLite queries (never a subprocess, never a
# fixed AITools script path -- see module docstring, B878)
# ---------------------------------------------------------------------------

_FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _fts_match_expr(raw: str) -> str | None:
    """Convert free text into a literal, injection-safe FTS5 MATCH expression.

    Each Unicode word token is emitted as its own double-quoted phrase so
    punctuation (``-``, ``:``, ``(``, ``)``, ``"``, ``*``) already present in
    a topic/query string can never be parsed as FTS5 query grammar. Returns
    ``None`` when the input has no searchable token.
    """
    tokens = _FTS_TOKEN_RE.findall(raw)
    if not tokens:
        return None
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def _open_readonly_db(path: Path, *, tool: str) -> sqlite3.Connection:
    try:
        con = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True,
            timeout=SQLITE_QUERY_TIMEOUT_SECONDS,
        )
        con.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise WorkerToolError(f"tool_db_unopenable:{tool}:{exc}") from exc
    return con


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _bounded_text(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _json_hit_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value) + sum(_json_hit_count(item) for item in value)
    if isinstance(value, dict):
        total = 0
        saw_container = False
        for key, item in value.items():
            if key in {"items", "results", "matches", "rows", "symbols", "files", "sections"}:
                saw_container = True
                total += _json_hit_count(item)
            elif isinstance(item, (dict, list)):
                total += _json_hit_count(item)
        if total:
            return total
        return 0 if saw_container else (1 if value else 0)
    return 0


def _canonical_json_output(name: str, text: str, *, max_bytes: int) -> tuple[str, bool]:
    """Strip Source Graph's one bounded banner line, canonicalize the JSON."""

    start = text.find("{")
    if start < 0:
        raise WorkerToolError(f"tool_malformed_json:{name}:missing_object")
    prefix = text[:start].strip()
    if prefix and not prefix.startswith("[*] Language:"):
        raise WorkerToolError(f"tool_malformed_json:{name}:unexpected_prefix")
    try:
        payload = json.loads(text[start:])
    except json.JSONDecodeError as exc:
        raise WorkerToolError(f"tool_malformed_json:{name}:{exc.msg}") from exc
    if not isinstance(payload, dict):
        raise WorkerToolError(f"tool_malformed_json:{name}:object_required")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    encoded = canonical.encode("utf-8")
    if len(encoded) <= max_bytes:
        return canonical, False
    wrapper = {
        "schema_id": "aiworkhub.task_mcp.bounded_json_preview.v1",
        "truncated": True,
        "original_bytes": len(encoded),
        "original_hit_count": _json_hit_count(payload),
        "preview": "",
    }
    overhead = len(json.dumps(wrapper, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    wrapper["preview"] = encoded[: max(0, max_bytes - overhead - 8)].decode("utf-8", errors="ignore")
    bounded = json.dumps(wrapper, ensure_ascii=False, sort_keys=True)
    while len(bounded.encode("utf-8")) > max_bytes and wrapper["preview"]:
        wrapper["preview"] = wrapper["preview"][:-64]
        bounded = json.dumps(wrapper, ensure_ascii=False, sort_keys=True)
    return bounded, True


# ---------------------------------------------------------------------------
# Authority database resolution (coordinator-owned storage.json registry)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AuthorityBinding:
    """A resolved, existence-checked, non-empty authority database path."""

    db_path: Path
    authority_source: str  # "legacy" | "canonical"
    authority_state: str   # raw storage.json authority.state, e.g. "shadow"


_STORAGE_REGISTRY_CACHE: dict[str, dict[str, Any]] = {}


def _load_storage_registry(authority_repo: Path) -> dict[str, Any]:
    cache_key = str(authority_repo)
    cached = _STORAGE_REGISTRY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    path = authority_repo / STORAGE_REGISTRY_RELATIVE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    _STORAGE_REGISTRY_CACHE[cache_key] = payload
    return payload


def _storage_registry_entry(authority_repo: Path, component: str, db_id: str) -> dict[str, Any] | None:
    registry = _load_storage_registry(authority_repo)
    for entry in registry.get("databases") or []:
        if isinstance(entry, dict) and entry.get("component") == component and entry.get("id") == db_id:
            return entry
    return None


def _resolve_authority_db(ctx: WorkerToolContext, *, component: str, db_id: str) -> AuthorityBinding:
    """Resolve a component's authoritative, canonical-only database path.

    Reads ONLY ``ctx.authority_repo`` (never ``ctx.repo``, the isolated
    worktree). Always resolves the registry's own ``canonical_durable_path``
    rooted under ``<authority_repo>/.aiworkhub`` -- there is no
    ``legacy_source`` fallback and no automatic legacy discovery. Fails
    closed (raises ``WorkerToolError``) when the registry entry is missing,
    is not marked ``authority.canonical_active``, or the resolved canonical
    database file does not exist or is empty -- this is what stops the
    isolated worktree's (or any other) missing/empty database, or a
    not-yet-cutover registry entry, from ever being mistaken for a
    successful authoritative lookup.
    """

    entry = _storage_registry_entry(ctx.authority_repo, component, db_id)
    if entry is None:
        raise WorkerToolError(f"authority_registry_entry_missing:{component}.{db_id}")
    authority = entry.get("authority") or {}
    state = str(authority.get("state") or "unknown")
    if not authority.get("canonical_active"):
        raise WorkerToolError(f"authority_component_not_canonical_active:{component}.{db_id}:{state}")
    registry = _load_storage_registry(ctx.authority_repo)
    durable_root = str(registry.get("durable_root") or ".aiworkhub")
    rel = entry.get("canonical_durable_path")
    base = ctx.authority_repo / durable_root
    if not rel:
        raise WorkerToolError(f"authority_db_path_undeclared:{component}.{db_id}:canonical")
    db_path = (base / str(rel)).resolve()
    if not db_path.is_relative_to(ctx.authority_repo.resolve()):
        raise WorkerToolError(f"authority_db_path_escapes_repo:{component}.{db_id}")
    if not db_path.is_file() or db_path.stat().st_size <= 0:
        raise WorkerToolError(f"authority_db_absent_or_empty:{component}.{db_id}:canonical:{state}")
    return AuthorityBinding(db_path=db_path, authority_source="canonical", authority_state=state)


def _resolve_source_graph_db(ctx: WorkerToolContext) -> AuthorityBinding:
    """Resolve the canonical Source Graph database -- AIWorkHub's SOLE authority.

    Unlike the other components resolved by ``_resolve_authority_db``, Source
    Graph is never read from a legacy/canonical toggle: task B849 made
    ``aiworkhub.source_graph`` the sole implementation and storage authority,
    so this always resolves through ``aiworkhub.storage_registry`` to
    ``<authority_repo>/.aiworkhub/source_graph/source_graph.sqlite`` and never
    falls back to ``AITools/source_graph.db``.
    """

    from . import storage_registry as _storage_registry_mod
    try:
        registry = _storage_registry_mod.load_storage_registry(ctx.authority_repo)
        db_path = _storage_registry_mod.resolve_database_path(registry, "source_graph")
    except (RepositoryStateError, _storage_registry_mod.StorageRegistryError) as exc:
        raise WorkerToolError(f"authority_registry_unresolved:source_graph.source_graph:{exc}") from exc
    if not db_path.is_file() or db_path.stat().st_size <= 0:
        raise WorkerToolError("authority_db_absent_or_empty:source_graph.source_graph:canonical")
    return AuthorityBinding(db_path=db_path, authority_source="canonical", authority_state="sole_authority")


def _source_graph_index_identity(db_path: Path, *, default_revision: str) -> dict[str, str]:
    """Return bounded canonical index identity without exposing its path.

    ``last_build.finished_at`` changes after every successful incremental
    refresh, so it is also the cache-generation boundary.  A database created
    by an older runtime may not have the row yet; that remains a truthful empty
    timestamp rather than turning a supported query into a false failure.
    """

    identity = {"build_revision": default_revision[:96], "finished_at": ""}
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='last_build'").fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return identity
    if not row or not isinstance(row[0], str):
        return identity
    try:
        payload = json.loads(row[0])
    except json.JSONDecodeError:
        return identity
    if not isinstance(payload, dict):
        return identity
    revision = str(payload.get("build_revision") or default_revision)[:96]
    finished_at = str(payload.get("finished_at") or "")[:64]
    return {"build_revision": revision, "finished_at": finished_at}


def _source_graph_evidence_counts(value: Any) -> dict[str, int]:
    """Count unique bounded evidence identities returned by one query.

    The result is deliberately structural: it counts only rows that expose a
    stable file/symbol or call-edge identity.  Generic nested lists are not
    guessed into entities, and duplicate projections of the same row count
    once.
    """

    entities: set[tuple[str, str, str, int]] = set()
    edges: set[tuple[str, str, str, str, int]] = set()
    files: set[str] = set()

    def safe_line(raw: Any) -> int:
        try:
            return max(0, int(raw or 0))
        except (TypeError, ValueError):
            return 0

    def visit(item: Any) -> None:
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not isinstance(item, dict):
            return
        file_path = str(item.get("file_path") or "")[:512]
        if file_path:
            files.add(file_path)
        src = str(item.get("src_qualname") or item.get("src") or "")[:512]
        dst = str(
            item.get("dst_qualname") or item.get("dst_name") or item.get("dst") or ""
        )[:512]
        if src and dst:
            edges.add((
                file_path,
                str(item.get("kind") or "")[:64],
                src,
                dst,
                safe_line(item.get("line")),
            ))
        else:
            qualname = str(item.get("qualname") or "")[:512]
            if file_path and qualname:
                entities.add((
                    file_path,
                    str(item.get("kind") or "")[:64],
                    qualname,
                    safe_line(item.get("line_start") or item.get("line")),
                ))
        for child in item.values():
            if isinstance(child, (dict, list)):
                visit(child)

    visit(value)
    return {
        "entity_rows": len(entities),
        "edge_rows": len(edges),
        "file_rows": len(files),
    }


# ---------------------------------------------------------------------------
# Per-request HMAC-authenticated audit ledger
# ---------------------------------------------------------------------------

def _append_line_0600(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        # The worker sandbox deliberately blocks metadata-changing syscalls
        # (including fchmod) after launch.  The coordinator pre-creates this
        # request-private ledger as 0600 before entering the sandbox, so an
        # EPERM/EACCES here is safe only when fstat proves that the invariant
        # already holds.  Previously the denied fchmod aborted before write;
        # _append_audit swallowed that OSError and every genuine MCP call left
        # an empty ledger.
        try:
            chmod_fd(fd, 0o600)
        except OSError as exc:
            mode = os.fstat(fd).st_mode & 0o777
            if exc.errno not in {errno.EPERM, errno.EACCES} or mode != 0o600:
                raise
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _touch_0600(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        chmod_fd(fd, 0o600)
    finally:
        os.close(fd)


def _hmac_entry(entry: dict[str, Any], key: bytes) -> str:
    canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _append_audit(
    ctx: WorkerToolContext,
    *,
    tool: str,
    ok: bool,
    cache_hit: bool,
    hit_count: int,
    bytes_returned: int,
    violation: str = "",
    authority_source: str = "",
    authority_state: str = "",
    payload: Mapping[str, Any] | None = None,
) -> None:
    if ctx.audit_ledger_path is None or ctx.audit_hmac_key_path is None:
        return
    try:
        key = ctx.audit_hmac_key_path.read_bytes()
    except OSError:
        return
    entry = {
        "schema_id": AUDIT_ENTRY_SCHEMA_ID,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_id": ctx.task_id,
        "runner": ctx.runner,
        "topic": ctx.topic,
        "request_id": ctx.request_id,
        "tool": tool,
        "ok": bool(ok),
        "cache_hit": bool(cache_hit),
        "hit_count": max(0, int(hit_count)),
        "bytes_returned": max(0, int(bytes_returned)),
        "violation": violation[:160],
        "authority_source": authority_source[:32],
        "authority_state": authority_state[:64],
        "authority_repo": str(ctx.authority_repo),
    }
    if payload is not None:
        encoded_payload = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        if len(encoded_payload) > MAX_TOOL_OUTPUT_BYTES:
            return
        entry["payload"] = dict(payload)
    digest = _hmac_entry(entry, key)
    line = json.dumps({**entry, "hmac_sha256": digest}, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        _append_line_0600(ctx.audit_ledger_path, line)
    except OSError:
        return


def verify_audit_ledger(
    ledger_path: Path,
    key_path: Path,
    *,
    task_id: str,
    runner: str,
    topic: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Independently re-verify every ledger entry's HMAC before counting it.

    A tampered or forged line (wrong/missing HMAC, or an HMAC computed with a
    different key) is dropped rather than trusted -- this is what makes the
    ledger an authenticated record of real MCP tool calls rather than
    something a worker's own final text could fake.
    """

    result: dict[str, Any] = {
        "schema_id": AUDIT_LEDGER_VERIFICATION_SCHEMA_ID,
        "ok": False,
        "entries_total": 0,
        "entries_verified": 0,
        "entries_tampered": 0,
        "call_count_by_tool": {},
        "successful_call_count_by_tool": {},
        "bounded_bytes_returned": 0,
        "bounded_bytes_by_tool": {},
        "cache_hits": 0,
        "cache_hits_by_tool": {},
        "policy_violations": 0,
        "fresh_source_graph_calls": 0,
        "live_source_graph_calls": 0,
        "source_graph_hit_count": 0,
        "source_graph_zero_hit_calls": 0,
        "source_graph_failed_calls": 0,
        "source_graph_mode_counts": {},
        "source_graph_mode_sequence": [],
        "source_graph_stage_counts": {},
        "source_graph_stage_sequence": [],
        "source_graph_mode_stage_counts": {},
        "source_graph_stage_attributed_calls": 0,
        "source_graph_latency": {
            "count": 0, "total_ms": 0.0, "min_ms": None, "max_ms": None,
            "p50_ms": None, "p95_ms": None,
        },
        "source_graph_call_gaps": {
            "count": 0, "total_seconds": 0.0, "min_seconds": None,
            "max_seconds": None, "p50_seconds": None, "p95_seconds": None,
        },
        "source_graph_evidence_rows": {
            "entity_rows": 0, "edge_rows": 0, "file_rows": 0,
        },
        "source_graph_index_revision_counts": {},
        "source_graph_index_sequence": [],
        "authority_index_identity": [],
        "verified_payloads": [],
        "reason": "",
    }
    try:
        key = key_path.read_bytes()
    except OSError:
        result["reason"] = "audit_key_unreadable"
        return result
    try:
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        result["reason"] = "audit_ledger_unreadable"
        return result

    call_count: dict[str, int] = {}
    successful_call_count: dict[str, int] = {}
    bounded_bytes_by_tool: dict[str, int] = {}
    cache_hits_by_tool: dict[str, int] = {}
    authority_seen: set[tuple[str, str, str, str]] = set()
    source_graph_latencies: list[float] = []
    source_graph_call_times: list[float] = []
    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("schema_id") != AUDIT_ENTRY_SCHEMA_ID:
            continue
        digest = entry.pop("hmac_sha256", None)
        if not isinstance(digest, str):
            continue
        expected = _hmac_entry(entry, key)
        result["entries_total"] += 1
        if not hmac.compare_digest(expected, digest):
            result["entries_tampered"] += 1
            continue
        if (
            str(entry.get("task_id")) != task_id
            or str(entry.get("runner")) != runner
            or str(entry.get("topic")) != topic
            or (request_id is not None and str(entry.get("request_id")) != request_id)
        ):
            continue
        result["entries_verified"] += 1
        tool = str(entry.get("tool") or "unknown")
        call_count[tool] = call_count.get(tool, 0) + 1
        payload = entry.get("payload")
        if tool == "source_graph":
            source_hits = max(0, int(entry.get("hit_count") or 0))
            result["source_graph_hit_count"] += source_hits
            if source_hits == 0:
                result["source_graph_zero_hit_calls"] += 1
            if not entry.get("ok"):
                result["source_graph_failed_calls"] += 1
            mode = str(payload.get("mode") or "") if isinstance(payload, dict) else ""
            if mode in SOURCE_GRAPH_MODES:
                mode_counts = result["source_graph_mode_counts"]
                mode_counts[mode] = int(mode_counts.get(mode) or 0) + 1
                if len(result["source_graph_mode_sequence"]) < 64:
                    result["source_graph_mode_sequence"].append(mode)
            stage = (
                str(payload.get("workflow_stage") or "unspecified")
                if isinstance(payload, dict) else "unspecified"
            )
            if stage not in WORKFLOW_STAGES:
                stage = "unspecified"
            stage_counts = result["source_graph_stage_counts"]
            stage_counts[stage] = int(stage_counts.get(stage) or 0) + 1
            if stage != "unspecified":
                result["source_graph_stage_attributed_calls"] += 1
            if len(result["source_graph_stage_sequence"]) < 64:
                result["source_graph_stage_sequence"].append(stage)
            if mode in SOURCE_GRAPH_MODES:
                mode_stage = result["source_graph_mode_stage_counts"].setdefault(stage, {})
                mode_stage[mode] = int(mode_stage.get(mode) or 0) + 1
            latency = payload.get("latency_ms") if isinstance(payload, dict) else None
            if isinstance(latency, (int, float)) and 0 <= float(latency) <= 3_600_000:
                source_graph_latencies.append(round(float(latency), 3))
            evidence_counts = (
                payload.get("evidence_counts") if isinstance(payload, dict) else None
            )
            if isinstance(evidence_counts, dict):
                for evidence_key in ("entity_rows", "edge_rows", "file_rows"):
                    result["source_graph_evidence_rows"][evidence_key] += max(
                        0, int(evidence_counts.get(evidence_key) or 0)
                    )
            revision = (
                str(payload.get("index_revision") or "")[:96]
                if isinstance(payload, dict) else ""
            )
            if revision:
                revisions = result["source_graph_index_revision_counts"]
                revisions[revision] = int(revisions.get(revision) or 0) + 1
                if len(result["source_graph_index_sequence"]) < 64:
                    result["source_graph_index_sequence"].append({
                        "revision": revision,
                        "finished_at": str(payload.get("index_finished_at") or "")[:64],
                    })
            try:
                timestamp = datetime.fromisoformat(
                    str(entry.get("timestamp") or "").replace("Z", "+00:00")
                )
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                source_graph_call_times.append(timestamp.timestamp())
            except (ValueError, OverflowError):
                pass
        returned_bytes = max(0, int(entry.get("bytes_returned") or 0))
        result["bounded_bytes_returned"] += returned_bytes
        bounded_bytes_by_tool[tool] = bounded_bytes_by_tool.get(tool, 0) + returned_bytes
        if entry.get("cache_hit"):
            result["cache_hits"] += 1
            cache_hits_by_tool[tool] = cache_hits_by_tool.get(tool, 0) + 1
        if entry.get("violation"):
            result["policy_violations"] += 1
        authority_source = str(entry.get("authority_source") or "")
        authority_state = str(entry.get("authority_state") or "")
        authority_repo = str(entry.get("authority_repo") or "")
        if (
            entry.get("ok")
            and not entry.get("violation")
            and authority_source == "canonical"
            and authority_state in {"canonical_active", "sole_authority"}
        ):
            successful_call_count[tool] = successful_call_count.get(tool, 0) + 1
        if authority_source or authority_state:
            authority_seen.add((tool, authority_source, authority_state, authority_repo))
        if (
            tool == "quality_review_submit"
            and entry.get("ok")
            and isinstance(payload, dict)
            and len(result["verified_payloads"]) < 12
        ):
            result["verified_payloads"].append(payload)
        authoritative_source_graph = (
            authority_source == "canonical"
            and authority_state in {"canonical_active", "sole_authority"}
        )
        # A fresh call is real tool-use telemetry even when its bounded query
        # returns zero rows.  Keep it distinct from the non-empty "live" count
        # used by the fail-closed completion gate.
        if (
            tool == "source_graph"
            and entry.get("ok")
            and not entry.get("violation")
            and not entry.get("cache_hit")
            and authoritative_source_graph
        ):
            result["fresh_source_graph_calls"] += 1
        # A "live" source_graph call must be a genuinely fresh, non-empty,
        # successful authoritative lookup -- a cache hit or a zero-hit
        # response is real telemetry but must NOT satisfy the completion
        # gate merely because ``ok`` happens to be true (B834: previously any
        # ok=True source_graph entry counted, including empty/cached ones).
        if (
            tool == "source_graph"
            and entry.get("ok")
            and not entry.get("violation")
            and not entry.get("cache_hit")
            and int(entry.get("hit_count") or 0) > 0
            and authoritative_source_graph
        ):
            result["live_source_graph_calls"] += 1
    result["call_count_by_tool"] = call_count
    result["successful_call_count_by_tool"] = successful_call_count
    result["bounded_bytes_by_tool"] = bounded_bytes_by_tool
    result["cache_hits_by_tool"] = cache_hits_by_tool
    result["authority_index_identity"] = sorted(
        f"{t}:{src}:{state}:{repo}" for t, src, state, repo in authority_seen
    )
    if source_graph_latencies:
        ordered = sorted(source_graph_latencies)

        def percentile(fraction: float) -> float:
            index = min(
                len(ordered) - 1,
                max(0, int(round((len(ordered) - 1) * fraction))),
            )
            return ordered[index]

        result["source_graph_latency"] = {
            "count": len(ordered),
            "total_ms": round(sum(ordered), 3),
            "min_ms": ordered[0],
            "max_ms": ordered[-1],
            "p50_ms": percentile(0.50),
            "p95_ms": percentile(0.95),
            "samples_ms": ordered[:64],
            "samples_truncated": len(ordered) > 64,
        }
    if len(source_graph_call_times) > 1:
        ordered_times = sorted(source_graph_call_times)
        gaps = [
            round(max(0.0, later - earlier), 3)
            for earlier, later in zip(ordered_times, ordered_times[1:])
        ]

        def gap_percentile(fraction: float) -> float:
            ordered_gaps = sorted(gaps)
            index = min(
                len(ordered_gaps) - 1,
                max(0, int(round((len(ordered_gaps) - 1) * fraction))),
            )
            return ordered_gaps[index]

        result["source_graph_call_gaps"] = {
            "count": len(gaps),
            "total_seconds": round(sum(gaps), 3),
            "min_seconds": min(gaps),
            "max_seconds": max(gaps),
            "p50_seconds": gap_percentile(0.50),
            "p95_seconds": gap_percentile(0.95),
            "samples_seconds": gaps[:64],
            "samples_truncated": len(gaps) > 64,
        }
    result["ok"] = True
    return result


# ---------------------------------------------------------------------------
# Bounded worker-safe tool implementations (pure functions of an explicit ctx)
# ---------------------------------------------------------------------------

_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


def _violation(ctx: WorkerToolContext, tool: str, reason: str) -> dict[str, Any]:
    _append_audit(ctx, tool=tool, ok=False, cache_hit=False, hit_count=0, bytes_returned=0, violation=reason)
    return {"ok": False, "tool": tool, "reason": reason}


def _bounded_query(value: Any, *, max_bytes: int = MAX_QUERY_BYTES) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or "\x00" in text or len(text.encode("utf-8")) > max_bytes:
        return None
    return text


_SOURCE_GRAPH_FILE_KEYS: tuple[str, ...] = (
    "file", "file_path", "file_name", "caller_file", "callee_file",
)


def _filter_by_scope(value: Any, scope: str) -> Any:
    """Constrain a Source Graph JSON payload to entries under ``scope``.

    Recurses through lists/dicts: a bare string list entry (e.g.
    ``relevant_files``) is kept only if it starts with ``scope``; a dict
    carrying a recognized file-path key (e.g. a ``risks``/``suspects`` row)
    is kept or dropped wholesale by that same prefix check; any other dict is
    walked deeper so unrelated scalar fields (``target``, ``mode``, ...) pass
    through untouched.
    """

    normalized_scope = scope.rstrip("/")

    def in_scope(candidate: str) -> bool:
        return candidate == normalized_scope or candidate.startswith(f"{normalized_scope}/")

    if isinstance(value, list):
        kept: list[Any] = []
        for item in value:
            if isinstance(item, str):
                if in_scope(item):
                    kept.append(item)
                continue
            filtered_item = _filter_by_scope(item, scope)
            if filtered_item is not None:
                kept.append(filtered_item)
        return kept
    if isinstance(value, dict):
        for key in _SOURCE_GRAPH_FILE_KEYS:
            file_value = value.get(key)
            if isinstance(file_value, str):
                return value if in_scope(file_value) else None
        return {
            key: _filter_by_scope(item, scope) if isinstance(item, (list, dict)) else item
            for key, item in value.items()
        }
    return value


def _declared_input_file_payload(ctx: WorkerToolContext, relative_path: str) -> dict[str, Any] | None:
    """Return a bounded exact-file receipt from the isolated worker tree.

    Source Graph deliberately indexes structure, so a declared JSON/JSONL/XML
    input can be a valid task authority while producing no semantic entity.
    This fallback is intentionally narrower than arbitrary file access: the
    caller has already passed the exact coordinator allowlist check, the path
    must equal the ``file`` query, stay beneath the immutable worker root, and
    contain no symlink component.  It never performs discovery or globbing.
    """

    if not relative_path or "\x00" in relative_path:
        return None
    raw = Path(relative_path)
    if raw.is_absolute() or ".." in raw.parts:
        return None
    root = ctx.repo.resolve()
    candidate = root / raw
    current = root
    try:
        for part in raw.parts:
            if part in {"", "."}:
                continue
            current = current / part
            if current.is_symlink():
                return None
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if resolved == root or root not in resolved.parents or not resolved.is_file():
        return None

    try:
        size = resolved.stat().st_size
        preview = bytearray()
        digest = hashlib.sha256()
        hashed_bytes = 0
        with resolved.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                if len(preview) < MAX_DECLARED_INPUT_PREVIEW_BYTES:
                    remaining = MAX_DECLARED_INPUT_PREVIEW_BYTES - len(preview)
                    preview.extend(chunk[:remaining])
                if hashed_bytes + len(chunk) <= MAX_DECLARED_INPUT_HASH_BYTES:
                    digest.update(chunk)
                    hashed_bytes += len(chunk)
                else:
                    hashed_bytes = MAX_DECLARED_INPUT_HASH_BYTES + 1
                    break
    except OSError:
        return None

    hash_complete = hashed_bytes <= MAX_DECLARED_INPUT_HASH_BYTES and hashed_bytes == size
    preview_text = bytes(preview).decode("utf-8", errors="replace")
    return {
        "mode": "file",
        "query": relative_path,
        "budget": 1,
        "matches": [{
            "file_path": relative_path,
            "kind": "declared_input_file",
            "size": size,
            "sha256": digest.hexdigest() if hash_complete else None,
            "hash_complete": hash_complete,
            "preview": preview_text,
            "preview_bytes": len(preview),
            "preview_truncated": size > len(preview),
            "authority": "worker_workspace_declared_input",
            "fallback_reason": "declared_input_unindexed",
        }],
        "candidate_files": [relative_path],
        "fallback_reason": "declared_input_unindexed",
        "truncated": size > len(preview),
    }


def source_graph_query(
    ctx: WorkerToolContext,
    *,
    mode: SourceGraphMode,
    query: str,
    budget: int = 64,
    target: str | None = None,
    bundle_type: SourceGraphBundleType = "explore",
    workflow_stage: WorkflowStage = "unspecified",
) -> dict[str, Any]:
    """Bounded Source Graph discovery and repository analytics.

    ``query`` is ALWAYS the semantic search term passed to Source Graph --
    omitting ``target`` never silently substitutes ``targets[0]`` for it
    (B834 repair: the B833 candidate discarded ``query`` whenever the
    coordinator had declared any targets at all). ``target``, when given,
    must be one of the coordinator-declared allowlist entries and constrains
    the RETURNED scope: matching file-path-bearing entries in the response
    are kept, everything else is dropped. Omitting ``target`` returns the
    unscoped query result.
    """

    tool = "source_graph"
    started = time.perf_counter()
    from . import feature_settings

    if not feature_settings.enabled(ctx.authority_repo, "source_graph"):
        return feature_settings.disabled_result("source_graph")
    if mode not in SOURCE_GRAPH_MODES:
        result = _violation(ctx, tool, f"invalid_mode:{mode}")
        result["allowed_modes"] = list(SOURCE_GRAPH_MODES)
        result["example"] = {"mode": "focus", "query": "symbol or behavior", "budget": 64}
        return result
    if bundle_type not in SOURCE_GRAPH_BUNDLE_TYPES:
        result = _violation(ctx, tool, f"invalid_bundle_type:{bundle_type}")
        result["allowed_bundle_types"] = list(SOURCE_GRAPH_BUNDLE_TYPES)
        return result
    if workflow_stage not in WORKFLOW_STAGES:
        result = _violation(ctx, tool, f"invalid_workflow_stage:{workflow_stage}")
        result["allowed_workflow_stages"] = list(WORKFLOW_STAGES)
        return result
    bounded_query = _bounded_query(query)
    if bounded_query is None:
        return _violation(ctx, tool, "invalid_query")
    try:
        budget = max(MIN_BUDGET, min(int(budget), MAX_BUDGET))
    except (TypeError, ValueError):
        return _violation(ctx, tool, "invalid_budget")

    scope: str | None = None
    if target is not None:
        bounded_target = _bounded_query(target, max_bytes=256)
        if bounded_target is None:
            return _violation(ctx, tool, "invalid_target")
        if ctx.source_graph_targets and bounded_target not in ctx.source_graph_targets:
            return _violation(ctx, tool, "target_not_allowed")
        scope = bounded_target

    try:
        binding = _resolve_source_graph_db(ctx)
    except WorkerToolError as exc:
        return _violation(ctx, tool, str(exc)[:160])

    from . import source_graph as _source_graph_mod
    index_identity = _source_graph_index_identity(
        binding.db_path, default_revision=_source_graph_mod.BUILD_REVISION,
    )
    cache_key = (
        "source_graph", ctx.task_id, ctx.request_id, str(ctx.authority_repo),
        mode, bounded_query, scope, budget, bundle_type,
        index_identity["build_revision"], index_identity["finished_at"],
    )
    cached = _CACHE.get(cache_key)
    if cached is not None:
        _append_audit(
            ctx, tool=tool, ok=True, cache_hit=True,
            hit_count=cached["hit_count"], bytes_returned=cached["bytes"],
            authority_source=cached["authority_source"], authority_state=cached["authority_state"],
            payload={
                "mode": mode,
                "workflow_stage": workflow_stage,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "index_revision": index_identity["build_revision"],
                "index_finished_at": index_identity["finished_at"],
                "evidence_counts": cached["evidence_counts"],
            },
        )
        return {**cached["result"], "cache_hit": True}

    try:
        if mode == "bundle":
            payload = _source_graph_mod.bundle(ctx.authority_repo, bundle_type, bounded_query, budget)
        elif mode == "slice":
            payload = _source_graph_mod.slice_(ctx.authority_repo, bounded_query, budget)
        elif mode == "context":
            payload = _source_graph_mod.context_query(ctx.authority_repo, bounded_query, budget)
        elif mode == "file":
            payload = _source_graph_mod.file_query(ctx.authority_repo, bounded_query, budget)
        elif mode == "function":
            payload = _source_graph_mod.function_query(ctx.authority_repo, bounded_query, budget)
        elif mode == "class":
            payload = _source_graph_mod.class_query(ctx.authority_repo, bounded_query, budget)
        elif mode == "body":
            payload = _source_graph_mod.body_query(ctx.authority_repo, bounded_query, budget)
        elif mode == "bodygrep":
            payload = _source_graph_mod.bodygrep_query(ctx.authority_repo, bounded_query, budget)
        elif mode == "impact":
            payload = _source_graph_mod.impact(ctx.authority_repo, bounded_query, budget)
        elif mode == "trace":
            payload = _source_graph_mod.trace(ctx.authority_repo, bounded_query, budget)
        elif mode == "deps":
            payload = _source_graph_mod.deps_query(ctx.authority_repo, bounded_query, budget)
        elif mode == "focus":
            payload = _source_graph_mod.focus(ctx.authority_repo, bounded_query, budget)
        else:
            payload = _source_graph_mod.analytics_query(
                ctx.authority_repo, mode, bounded_query, budget,
            )
    except _source_graph_mod.SourceGraphError as exc:
        return _violation(ctx, tool, str(exc)[:160])
    if (
        mode == "file"
        and scope is not None
        and bounded_query == scope
        and _json_hit_count(payload) == 0
    ):
        exact_payload = _declared_input_file_payload(ctx, scope)
        if exact_payload is not None:
            payload = exact_payload
    if scope is not None:
        payload = _filter_by_scope(payload, scope) or {}
        if isinstance(payload, dict) and payload:
            payload["scope"] = "target"
    raw_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    truncated = len(raw_text.encode("utf-8")) > MAX_RAW_TOOL_OUTPUT_BYTES
    try:
        text, json_truncated = _canonical_json_output(tool, raw_text, max_bytes=MAX_TOOL_OUTPUT_BYTES)
    except WorkerToolError as exc:
        return _violation(ctx, tool, str(exc)[:160])
    truncated = truncated or json_truncated

    payload = json.loads(text)
    hit_count = _json_hit_count(payload)
    evidence_counts = _source_graph_evidence_counts(payload)
    bytes_returned = len(text.encode("utf-8"))
    result = {
        "ok": True,
        "tool": tool,
        "mode": mode,
        "query": bounded_query,
        "target": scope,
        "budget": budget,
        "truncated": truncated,
        "hit_count": hit_count,
        "bytes": bytes_returned,
        "content": text,
        "cache_hit": False,
        "authority_source": binding.authority_source,
        "authority_state": binding.authority_state,
        "authority_repo": str(ctx.authority_repo),
        "index_revision": index_identity["build_revision"],
        "index_finished_at": index_identity["finished_at"],
        "evidence_counts": evidence_counts,
    }
    _CACHE[cache_key] = {
        "result": result, "hit_count": hit_count, "bytes": bytes_returned,
        "authority_source": binding.authority_source, "authority_state": binding.authority_state,
        "evidence_counts": evidence_counts,
    }
    _append_audit(
        ctx, tool=tool, ok=True, cache_hit=False, hit_count=hit_count, bytes_returned=bytes_returned,
        authority_source=binding.authority_source, authority_state=binding.authority_state,
        payload={
            "mode": mode,
            "workflow_stage": workflow_stage,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "index_revision": index_identity["build_revision"],
            "index_finished_at": index_identity["finished_at"],
            "evidence_counts": evidence_counts,
        },
    )
    return result


def session_current_state(ctx: WorkerToolContext, *, limit: int = 12) -> dict[str, Any]:
    """Bounded Session Manager current-state, scoped to this task's topic.

    Self-contained (B878): reads the canonical transcript-graph database
    directly (``documents`` / ``documents_fts``) in-process, bounded to
    ``limit`` rows ordered most-recent-first. This is a deliberately
    simplified reimplementation of ``AITools/transcript_graph.py``'s
    ``current_state`` (no authority-class ranking, supersession, or conflict
    detection) -- that engine is a large, standalone module this worker
    surface must not depend on or shell out to; what it preserves is the
    bounded, evidence-with-source-id contract the caller actually relies on.
    The ``sessions`` component's own database is still resolved and required
    to exist (matching the registry's authority contract for that
    component) even though this simplified query does not read its rows.
    """

    tool = "session_current_state"
    from . import feature_settings

    if not feature_settings.enabled(ctx.authority_repo, "session_manager"):
        return feature_settings.disabled_result("session_manager")
    try:
        limit = max(MIN_LIMIT, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        return _violation(ctx, tool, "invalid_limit")
    try:
        session_binding = _resolve_authority_db(ctx, component="sessions", db_id="session")
        graph_binding = _resolve_authority_db(ctx, component="sessions", db_id="transcript")
    except WorkerToolError as exc:
        return _violation(ctx, tool, str(exc)[:160])
    try:
        con = _open_readonly_db(graph_binding.db_path, tool=tool)
    except WorkerToolError as exc:
        return _violation(ctx, tool, str(exc)[:160])
    try:
        rows: list[sqlite3.Row] = []
        match_expr = _fts_match_expr(ctx.session_topic)
        if match_expr is not None:
            try:
                rows = con.execute(
                    "SELECT d.source_id AS source_id, d.timestamp AS timestamp, "
                    "d.kind AS kind, d.content AS content FROM documents d "
                    "JOIN documents_fts f ON d.doc_id = f.rowid "
                    "WHERE documents_fts MATCH ? ORDER BY d.timestamp DESC LIMIT ?",
                    (match_expr, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            rows = con.execute(
                "SELECT source_id, timestamp, kind, content FROM documents "
                "ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
    except sqlite3.Error as exc:
        return _violation(ctx, tool, f"tool_query_failed:{tool}:{exc}"[:160])
    finally:
        con.close()

    evidence = [
        {
            "source_id": row["source_id"], "timestamp": row["timestamp"], "kind": row["kind"],
            "snippet": (row["content"] or "")[:SESSION_SNIPPET_CHARS],
        }
        for row in rows
    ]
    state = "unknown" if not evidence else ("current" if len(evidence) == 1 else "superseded")
    payload = {"topic": ctx.session_topic, "state": state, "evidence_count": len(evidence), "evidence": evidence}
    text, truncated = _bounded_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True), MAX_TOOL_OUTPUT_BYTES,
    )
    hit_count = len(evidence)
    bytes_returned = len(text.encode("utf-8"))
    _append_audit(
        ctx, tool=tool, ok=True, cache_hit=False, hit_count=hit_count, bytes_returned=bytes_returned,
        authority_source=session_binding.authority_source, authority_state=session_binding.authority_state,
    )
    return {
        "ok": True, "tool": tool, "topic": ctx.session_topic, "limit": limit,
        "truncated": truncated, "hit_count": hit_count, "bytes": bytes_returned,
        "content": text, "cache_hit": False,
        "authority_source": session_binding.authority_source, "authority_state": session_binding.authority_state,
    }


def ai_memory_search(ctx: WorkerToolContext, *, query: str, limit: int = 8) -> dict[str, Any]:
    """Bounded AI Memory search.

    Self-contained (B878): reads the canonical memory database's
    ``memories`` / ``memories_fts`` tables directly, in-process, read-only.
    ``AITools/ai_memory/ai_memory.py`` resolves its database relative to its
    own ``__file__`` with no path-override flag -- a canonical-only
    repository (no adjacent ``AITools/ai_memory/ai_memory.db``) could never
    satisfy that hardcoded path, so this tool no longer shells out to it.
    """

    tool = "ai_memory"
    from . import feature_settings

    if not feature_settings.enabled(ctx.authority_repo, "ai_memory"):
        return feature_settings.disabled_result("ai_memory")
    bounded = _bounded_query(query)
    if bounded is None:
        return _violation(ctx, tool, "invalid_query")
    try:
        limit = max(MIN_LIMIT, min(int(limit), 10))
    except (TypeError, ValueError):
        return _violation(ctx, tool, "invalid_limit")
    try:
        binding = _resolve_authority_db(ctx, component="memory", db_id="memory")
    except WorkerToolError as exc:
        return _violation(ctx, tool, str(exc)[:160])
    try:
        con = _open_readonly_db(binding.db_path, tool=tool)
    except WorkerToolError as exc:
        return _violation(ctx, tool, str(exc)[:160])
    try:
        rows: list[sqlite3.Row] = []
        match_expr = _fts_match_expr(bounded)
        if match_expr is not None:
            if _table_exists(con, "context_entity_state"):
                rows = con.execute(
                    "SELECT m.key AS key, m.value AS value, m.tags AS tags, m.scope AS scope "
                    "FROM memories m JOIN memories_fts f ON m.id = f.rowid "
                    "LEFT JOIN context_entity_state s ON s.entity_type='memory' AND s.entity_id=m.id "
                    "WHERE memories_fts MATCH ? AND COALESCE(s.status,'active')='active' "
                    "ORDER BY rank LIMIT ?",
                    (match_expr, limit),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT m.key AS key, m.value AS value, m.tags AS tags, m.scope AS scope "
                    "FROM memories m JOIN memories_fts f ON m.id = f.rowid "
                    "WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
                    (match_expr, limit),
                ).fetchall()
    except sqlite3.Error as exc:
        return _violation(ctx, tool, f"tool_query_failed:{tool}:{exc}"[:160])
    finally:
        con.close()

    payload = {"results": [dict(row) for row in rows], "count": len(rows)}
    text, truncated = _bounded_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), 8 * 1024)
    hit_count = len(rows)
    bytes_returned = len(text.encode("utf-8"))
    _append_audit(
        ctx, tool=tool, ok=True, cache_hit=False, hit_count=hit_count, bytes_returned=bytes_returned,
        authority_source=binding.authority_source, authority_state=binding.authority_state,
    )
    return {
        "ok": True, "tool": tool, "query": bounded, "limit": limit,
        "truncated": truncated, "hit_count": hit_count, "bytes": bytes_returned,
        "content": text, "cache_hit": False,
        "authority_source": binding.authority_source, "authority_state": binding.authority_state,
    }


def _ai_memory_exact(ctx: WorkerToolContext, *, key: str, related: bool) -> dict[str, Any]:
    tool = "ai_memory_related" if related else "ai_memory_get"
    bounded = _bounded_query(key, max_bytes=MAX_KEY_BYTES)
    if bounded is None:
        return _violation(ctx, tool, "invalid_key")
    try:
        binding = _resolve_authority_db(ctx, component="memory", db_id="memory")
        con = _open_readonly_db(binding.db_path, tool=tool)
    except WorkerToolError as exc:
        return _violation(ctx, tool, str(exc)[:160])
    try:
        has_state = _table_exists(con, "context_entity_state")
        state_join = (
            "LEFT JOIN context_entity_state s ON s.entity_type='memory' AND s.entity_id=m.id "
            if has_state else ""
        )
        state_filter = "AND COALESCE(s.status,'active')='active' " if has_state else ""
        row = con.execute(
            "SELECT m.id,m.key,m.value,m.tags,m.scope FROM memories m " + state_join +
            "WHERE m.key=? " + state_filter + "ORDER BY m.id DESC LIMIT 1",
            (bounded,),
        ).fetchone()
        if not related:
            payload = {"memory": dict(row) if row is not None else None, "count": 1 if row is not None else 0}
        elif row is None:
            payload = {"related": [], "count": 0}
        else:
            seen = {bounded}
            candidates: list[dict[str, Any]] = []
            for tag in (item.strip() for item in str(row["tags"] or "").split(",") if item.strip()):
                rows = con.execute(
                    "SELECT m.id,m.key,m.value,m.tags,m.scope FROM memories m " + state_join +
                    "WHERE m.key<>? AND (',' || m.tags || ',') LIKE ? " + state_filter +
                    "ORDER BY m.id DESC LIMIT 8",
                    (bounded, f"%,{tag},%"),
                ).fetchall()
                for candidate in rows:
                    if candidate["key"] not in seen:
                        seen.add(candidate["key"])
                        candidates.append(dict(candidate))
                        if len(candidates) >= 8:
                            break
                if len(candidates) >= 8:
                    break
            payload = {"related": candidates, "count": len(candidates)}
    except sqlite3.Error as exc:
        return _violation(ctx, tool, f"tool_query_failed:{tool}:{exc}"[:160])
    finally:
        con.close()
    text, truncated = _bounded_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), 8 * 1024)
    hit_count = int(payload.get("count") or 0)
    bytes_returned = len(text.encode("utf-8"))
    _append_audit(
        ctx, tool=tool, ok=True, cache_hit=False, hit_count=hit_count,
        bytes_returned=bytes_returned, authority_source=binding.authority_source,
        authority_state=binding.authority_state,
    )
    return {
        "ok": True, "tool": tool, "key": bounded, "truncated": truncated,
        "hit_count": hit_count, "bytes": bytes_returned, "content": text,
        "cache_hit": False, "authority_source": binding.authority_source,
        "authority_state": binding.authority_state,
    }


def ai_memory_get(ctx: WorkerToolContext, *, key: str) -> dict[str, Any]:
    """Bounded exact lookup of one active canonical AI Memory entry."""
    from . import feature_settings

    if not feature_settings.enabled(ctx.authority_repo, "ai_memory"):
        return feature_settings.disabled_result("ai_memory")
    return _ai_memory_exact(ctx, key=key, related=False)


def ai_memory_related(ctx: WorkerToolContext, *, key: str) -> dict[str, Any]:
    """Bounded active memories sharing one or more normalized tags."""
    from . import feature_settings

    if not feature_settings.enabled(ctx.authority_repo, "ai_memory"):
        return feature_settings.disabled_result("ai_memory")
    return _ai_memory_exact(ctx, key=key, related=True)


def _kb_invoke(ctx: WorkerToolContext, *, subcommand: str, argument: str, tool_label: str) -> dict[str, Any]:
    """Bounded, in-process KB lookup against the canonical ``entries``/
    ``links`` tables (B878: no more shelling out to ``AITools/kb.py``)."""

    bounded = _bounded_query(argument, max_bytes=MAX_KEY_BYTES if subcommand != "search" else MAX_QUERY_BYTES)
    if bounded is None:
        return _violation(ctx, tool_label, f"invalid_{subcommand}_argument")
    try:
        binding = _resolve_authority_db(ctx, component="kb", db_id="kb")
    except WorkerToolError as exc:
        return _violation(ctx, tool_label, str(exc)[:160])
    try:
        con = _open_readonly_db(binding.db_path, tool=tool_label)
    except WorkerToolError as exc:
        return _violation(ctx, tool_label, str(exc)[:160])
    try:
        payload = _kb_query(con, subcommand=subcommand, argument=bounded)
    except sqlite3.Error as exc:
        return _violation(ctx, tool_label, f"tool_query_failed:{tool_label}:{exc}"[:160])
    finally:
        con.close()

    text, truncated = _bounded_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), 8 * 1024)
    hit_count = int(payload.get("count") or 0)
    bytes_returned = len(text.encode("utf-8"))
    _append_audit(
        ctx, tool=tool_label, ok=True, cache_hit=False, hit_count=hit_count, bytes_returned=bytes_returned,
        authority_source=binding.authority_source, authority_state=binding.authority_state,
    )
    return {
        "ok": True, "tool": tool_label, subcommand: bounded,
        "truncated": truncated, "hit_count": hit_count, "bytes": bytes_returned,
        "content": text, "cache_hit": False,
        "authority_source": binding.authority_source, "authority_state": binding.authority_state,
    }


def _kb_query(con: sqlite3.Connection, *, subcommand: str, argument: str) -> dict[str, Any]:
    has_state = _table_exists(con, "context_entity_state")
    if subcommand == "search":
        match_expr = _fts_match_expr(argument)
        rows: list[sqlite3.Row] = []
        if match_expr is not None:
            state_join = (
                "LEFT JOIN context_entity_state s ON s.entity_type='kb' AND s.entity_id=e.id "
                if has_state else ""
            )
            state_filter = "AND COALESCE(s.status,'active')='active' " if has_state else ""
            rows = con.execute(
                "SELECT e.key AS key, e.title AS title, e.category AS category, "
                "e.tags AS tags, e.body AS body FROM entries e "
                "JOIN entries_fts f ON e.id = f.rowid " + state_join +
                "WHERE entries_fts MATCH ? " + state_filter + "ORDER BY rank LIMIT 8",
                (match_expr,),
            ).fetchall()
        return {"results": [dict(row) for row in rows], "count": len(rows)}

    if subcommand == "get":
        if has_state:
            row = con.execute(
                "SELECT e.key,e.title,e.body,e.category,e.tags,e.source_refs FROM entries e "
                "LEFT JOIN context_entity_state s ON s.entity_type='kb' AND s.entity_id=e.id "
                "WHERE e.key=? AND COALESCE(s.status,'active')='active'", (argument,),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT key, title, body, category, tags, source_refs FROM entries WHERE key=?",
                (argument,),
            ).fetchone()
        return {"entry": dict(row) if row is not None else None, "count": 1 if row is not None else 0}

    # subcommand == "related"
    row = con.execute("SELECT key, tags FROM entries WHERE key=?", (argument,)).fetchone()
    if row is None:
        return {"related": [], "count": 0}
    seen = {argument}
    related: list[dict[str, Any]] = []
    for tag in (t.strip() for t in (row["tags"] or "").split(",") if t.strip()):
        for candidate in con.execute(
            "SELECT key, title, category FROM entries WHERE key != ? "
            "AND (',' || tags || ',') LIKE ? LIMIT 5",
            (argument, f"%,{tag},%"),
        ).fetchall():
            if candidate["key"] not in seen:
                seen.add(candidate["key"])
                related.append({"via": tag, **dict(candidate)})
    for link in con.execute(
        "SELECT to_key AS k, relation FROM links WHERE from_key=? "
        "UNION SELECT from_key AS k, relation FROM links WHERE to_key=?",
        (argument, argument),
    ).fetchall():
        if link["k"] in seen:
            continue
        entry = con.execute(
            "SELECT key, title, category FROM entries WHERE key=?", (link["k"],),
        ).fetchone()
        if entry is not None:
            seen.add(link["k"])
            related.append({"via": f"link:{link['relation']}", **dict(entry)})
    return {"related": related, "count": len(related)}


def kb_search(ctx: WorkerToolContext, *, query: str, limit: int = 8) -> dict[str, Any]:
    """Bounded KB full-text search."""

    from . import feature_settings

    if not feature_settings.enabled(ctx.authority_repo, "knowledge_base"):
        return feature_settings.disabled_result("knowledge_base")
    del limit  # kb.py search's own --limit is fixed at 8 by _kb_invoke; kept for a stable tool signature
    return _kb_invoke(ctx, subcommand="search", argument=query, tool_label="kb")


def kb_get(ctx: WorkerToolContext, *, key: str) -> dict[str, Any]:
    """Bounded KB exact-key lookup."""

    from . import feature_settings

    if not feature_settings.enabled(ctx.authority_repo, "knowledge_base"):
        return feature_settings.disabled_result("knowledge_base")
    return _kb_invoke(ctx, subcommand="get", argument=key, tool_label="kb")


def kb_related(ctx: WorkerToolContext, *, key: str) -> dict[str, Any]:
    """Bounded KB related-entries lookup."""

    from . import feature_settings

    if not feature_settings.enabled(ctx.authority_repo, "knowledge_base"):
        return feature_settings.disabled_result("knowledge_base")
    return _kb_invoke(ctx, subcommand="related", argument=key, tool_label="kb")


def _context_write_intent(
    ctx: WorkerToolContext, *, component: str, action: str,
    payload: dict[str, Any], idempotency_key: str, provenance: str,
) -> dict[str, Any]:
    """Append a proposal to the authenticated request ledger only.

    This deliberately never resolves or opens a canonical database.  The
    verified manager owns disposition and canonical application.
    """

    from . import feature_settings

    feature = {
        "session": "session_manager",
        "memory": "ai_memory",
        "kb": "knowledge_base",
    }.get(component)
    if feature and not feature_settings.enabled(ctx.authority_repo, feature):
        return feature_settings.disabled_result(feature)
    if ctx.audit_ledger_path is None or ctx.audit_hmac_key_path is None:
        return {"ok": False, "error": "worker_context_intent_runtime_unavailable"}
    # Lazy by design: minimal/bundled read-only worker packages used for
    # discovery portability can still import this module.  A runtime that
    # advertises the write-intent tools must ship the complete package.
    from . import context_write_intents
    try:
        return context_write_intents.append_intent(
            ledger_path=ctx.audit_ledger_path,
            key_path=ctx.audit_hmac_key_path,
            task_id=ctx.task_id,
            runner=ctx.runner,
            topic=ctx.topic,
            request_id=ctx.request_id,
            authority_repo=ctx.authority_repo,
            component=component,  # type: ignore[arg-type]
            action=action,
            payload=payload,
            idempotency_key=idempotency_key,
            provenance=provenance,
        )
    except context_write_intents.ContextWriteIntentError as exc:
        return {"ok": False, "error": str(exc)[:240]}
    except OSError as exc:
        return {"ok": False, "error": f"worker_context_intent_failed:{type(exc).__name__}"}


def session_write_intent(
    ctx: WorkerToolContext, *, action: str, content: str,
    idempotency_key: str, provenance: str,
) -> dict[str, Any]:
    return _context_write_intent(
        ctx, component="session", action=action,
        payload={"topic": ctx.session_topic, "content": content},
        idempotency_key=idempotency_key, provenance=provenance,
    )


def ai_memory_write_intent(
    ctx: WorkerToolContext, *, action: str, key: str, value: str = "",
    tags: str = "", scope: str = "project", idempotency_key: str,
    provenance: str,
) -> dict[str, Any]:
    return _context_write_intent(
        ctx, component="memory", action=action,
        payload={"key": key, "value": value, "tags": tags, "scope": scope},
        idempotency_key=idempotency_key, provenance=provenance,
    )


def kb_write_intent(
    ctx: WorkerToolContext, *, action: str, key: str, title: str = "",
    body: str = "", category: str = "", tags: str = "",
    source_refs: str = "", replacement_key: str = "",
    idempotency_key: str, provenance: str,
) -> dict[str, Any]:
    return _context_write_intent(
        ctx, component="kb", action=action,
        payload={
            "key": key, "title": title, "body": body, "category": category,
            "tags": tags, "source_refs": source_refs,
            "replacement_key": replacement_key,
        },
        idempotency_key=idempotency_key, provenance=provenance,
    )


def quality_review_submit(
    ctx: WorkerToolContext,
    *,
    packet_sha256: str,
    lens: str,
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Submit findings for the exact coordinator-bound review packet.

    This tool is inert for ordinary workers: the launcher must bind one
    immutable packet path into the worker runtime. The model cannot choose a
    target task/provider, and the signed audit payload carries the worker's
    immutable request/task identity rather than caller-supplied identity.
    """

    from . import quality_evidence, quality_reviewer

    tool = "quality_review_submit"
    path = ctx.quality_review_packet_path
    if path is None:
        return _violation(ctx, tool, "quality_review_packet_not_bound")
    try:
        if path.is_symlink() or not path.is_file():
            return _violation(ctx, tool, "quality_review_packet_invalid")
        if path.stat().st_size > MAX_QUALITY_REVIEW_PACKET_BYTES:
            return _violation(ctx, tool, "quality_review_packet_too_large")
        packet = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _violation(ctx, tool, "quality_review_packet_unreadable")
    if not isinstance(packet, dict) or packet.get("schema_id") != quality_reviewer.PACKET_SCHEMA_ID:
        return _violation(ctx, tool, "quality_review_packet_schema_mismatch")
    if packet.get("packet_sha256") != packet_sha256:
        return _violation(ctx, tool, "quality_review_packet_digest_mismatch")
    try:
        # Recompute the digest instead of trusting the file's digest field.
        quality_reviewer.verify_reviewer_receipt(
            {
                "schema_id": quality_reviewer.RECEIPT_SCHEMA_ID,
                "packet_sha256": packet_sha256,
                "target": dict(packet.get("target") or {}),
                "reviewer": {
                    "request_id": ctx.request_id,
                    "task_id": ctx.task_id,
                    "provider": ctx.runner,
                },
                "report": {
                    "lens": lens,
                    "read_only": True,
                    "can_mutate_repo": False,
                    "findings": findings,
                },
            },
            packet=packet,
            expected_reviewer_request_id=ctx.request_id,
            expected_reviewer_task_id=ctx.task_id,
            observed_provider=ctx.runner,
            observed_terminal_state="review_ready",
            audit_verified=True,
        )
    except quality_reviewer.ReviewerEvidenceError as exc:
        return _violation(ctx, tool, str(exc))
    normalized, errors = quality_evidence.normalize_reviewer_reports(
        [
            {
                "lens": lens,
                "provider": ctx.runner,
                "read_only": True,
                "can_mutate_repo": False,
                "findings": findings,
            }
        ]
    )
    if errors or len(normalized) != 1:
        return _violation(ctx, tool, (errors[0] if errors else "reviewer_report_invalid"))
    receipt = {
        "schema_id": quality_reviewer.RECEIPT_SCHEMA_ID,
        "packet_sha256": packet_sha256,
        "target": {
            key: (packet.get("target") or {}).get(key)
            for key in ("request_id", "task_id", "claim_epoch")
        },
        "reviewer": {
            "request_id": ctx.request_id,
            "task_id": ctx.task_id,
        },
        "report": {
            **normalized[0],
            # The provider is deliberately omitted from the signed model
            # report. The coordinator inserts the adapter/provider observed
            # in its own process registry during receipt verification.
            "provider": "",
        },
    }
    _append_audit(
        ctx,
        tool=tool,
        ok=True,
        cache_hit=False,
        hit_count=len(findings),
        bytes_returned=0,
        authority_source="runtime",
        authority_state="process_bound",
        payload=receipt,
    )
    return {
        "ok": True,
        "tool": tool,
        "status": "submitted",
        "packet_sha256": packet_sha256,
        "finding_count": len(findings),
    }


# ---------------------------------------------------------------------------
# Per-request MCP runtime generation (host-side; called BEFORE the sandboxed
# adapter process starts, so it may write freely under the isolated home)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class WorkerMcpRuntime:
    server_name: str
    env: dict[str, str]
    claude_mcp_config_path: Path
    copilot_mcp_config_path: Path
    codex_config_toml_path: Path
    audit_ledger_path: Path
    audit_hmac_key_path: Path
    tool_names: tuple[str, ...]
    package_import_root: Path


def resolve_host_package_import_root() -> Path:
    """Return this module's own package import root: the real directory that
    must be on ``sys.path`` for ``import aiworkhub`` to resolve, derived from
    the running module's actual, installed ``__file__`` location.

    This is intentionally the ONLY thing this function computes -- the parent
    of the ``aiworkhub`` package directory this file lives in. It never counts
    a fixed number of additional parents to guess a repository root, and it
    never assumes a monorepo-specific package subpath or a
    standalone ``<repo>/src/aiworkhub`` layout: whichever of those (or a
    bundled/installed location entirely outside any project repository) is
    true on this host, ``Path(__file__).resolve().parent.parent`` is correct
    by construction, because it is the direct parent of THIS package
    directory, not an inferred ancestor of some other root.
    """
    return Path(__file__).resolve().parent.parent


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_json_0600(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        chmod_fd(fd, 0o600)
        os.write(fd, data.encode("utf-8"))
    finally:
        os.close(fd)


def generate_worker_mcp_runtime(
    *,
    home: Path,
    request_id: str,
    task_id: str,
    runner: str,
    topic: str,
    repo: Path,
    authority_repo: Path,
    source_graph_targets: list[str] | tuple[str, ...],
    session_topic: str,
    package_import_root: Path,
    python_executable: str | None = None,
    quality_review_packet_path: Path | None = None,
) -> WorkerMcpRuntime:
    """Provision this request's isolated MCP config, env and audit ledger.

    Everything this writes lives under ``home`` (the isolated per-request
    HOME), mode 0700/0600. The generated per-adapter config files embed the
    binding env directly (rather than relying on ambient env inheritance
    into the MCP server subprocess), so each config is self-contained and
    portable across the three adapter injection shapes.

    ``repo`` and ``authority_repo`` are two independent, already-resolved
    bindings the caller computes (see ``worker_workspace.provision_worker_mcp_runtime``
    for the backend-aware sandbox-path rewrite -- under bubblewrap neither
    value is the real host path; each is instead the bound sandbox alias
    that will become visible only once the sandboxed process actually starts,
    so this function cannot validate them as existing directories on the
    host. The caller validates the real host paths before rewriting them.

    B869 launch repair: every generated config launches this server as the
    package module ``python -m aiworkhub.worker_ai_tools_mcp`` -- never the
    bare file path. Executing the file directly makes Python treat it as
    ``__main__`` with no known parent package, so its own
    ``from .repository_state import ...`` relative import raises
    ``ImportError: attempted relative import with no known parent package``
    before the server can even bind ``ctx``.

    B870 V2 portability repair: ``env[PYTHONPATH]`` is set VERBATIM to the
    caller-supplied ``package_import_root`` -- this function never derives it
    itself, never counts ``Path(__file__)`` parents, and never assumes a
    repository layout (no monorepo-specific subpath, no fixed parent
    depth). The caller (``worker_workspace.provision_worker_mcp_runtime``)
    resolves the real host package root via ``resolve_host_package_import_root()``
    and, only for the bubblewrap backend, substitutes the dedicated
    ``SANDBOX_PACKAGE_IMPORT_ROOT`` alias that ``sandbox_argv`` binds that same
    real host directory to (read-only) in the SAME mount namespace the worker
    adapter process (and the MCP server subprocess it spawns) runs under. For
    landlock and direct/no-sandbox invocation the real host path is passed
    through unchanged -- Landlock confines writes only, so no read-side alias
    is ever needed there. This works identically whether the package lives in
    a standalone ``<repo>/src/aiworkhub`` checkout, nested inside a monorepo,
    or bundled/installed entirely outside ``authority_repo``, because the
    value is never rebased onto ``authority_repo`` at all.
    """

    runtime_dir = (home / "task_mcp_worker_runtime").resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime_dir, 0o700)

    key_path = runtime_dir / "audit_hmac.key"
    if not key_path.exists():
        _touch_0600(key_path)
        os.truncate(key_path, 0)
        with open(key_path, "wb") as handle:
            handle.write(secrets.token_bytes(32))
        os.chmod(key_path, 0o600)
    ledger_path = runtime_dir / "audit_ledger.jsonl"
    _touch_0600(ledger_path)

    env = {
        ENV_TASK_ID: task_id,
        ENV_RUNNER: runner,
        ENV_TOPIC: topic,
        ENV_REQUEST_ID: request_id,
        ENV_REPO: str(repo),
        ENV_AUTHORITY_REPO: str(authority_repo),
        ENV_SOURCE_GRAPH_TARGETS: json.dumps(list(source_graph_targets)),
        ENV_SESSION_TOPIC: session_topic,
        ENV_AUDIT_LEDGER_PATH: str(ledger_path),
        ENV_AUDIT_HMAC_KEY_PATH: str(key_path),
    }
    if quality_review_packet_path is not None:
        env[ENV_QUALITY_REVIEW_PACKET_PATH] = str(quality_review_packet_path)

    module_file = Path(__file__).resolve()
    package_module = f"{module_file.parent.name}.{module_file.stem}"
    env[ENV_PYTHONPATH] = str(package_import_root)

    py = python_executable or sys.executable
    launch_args = ["-m", package_module]
    mcp_config = {
        "mcpServers": {
            SERVER_NAME: {"command": py, "args": launch_args, "env": env}
        }
    }

    claude_path = runtime_dir / "claude_mcp_config.json"
    _write_json_0600(claude_path, mcp_config)
    copilot_path = runtime_dir / "copilot_mcp_config.json"
    _write_json_0600(copilot_path, mcp_config)

    codex_home = (home / ".codex").resolve()
    codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(codex_home, 0o700)
    codex_config_path = codex_home / "config.toml"
    lines = [
        f"[mcp_servers.{SERVER_NAME}]",
        f"command = {_toml_str(py)}",
        f"args = [{', '.join(_toml_str(a) for a in launch_args)}]",
        "",
        f"[mcp_servers.{SERVER_NAME}.env]",
    ]
    for name, value in env.items():
        lines.append(f"{name} = {_toml_str(value)}")
    toml_text = "\n".join(lines) + "\n"
    flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(codex_config_path, flags, 0o600)
    try:
        chmod_fd(fd, 0o600)
        os.write(fd, toml_text.encode("utf-8"))
    finally:
        os.close(fd)

    return WorkerMcpRuntime(
        server_name=SERVER_NAME,
        env=env,
        claude_mcp_config_path=claude_path,
        copilot_mcp_config_path=copilot_path,
        codex_config_toml_path=codex_config_path,
        audit_ledger_path=ledger_path,
        audit_hmac_key_path=key_path,
        tool_names=MCP_TOOL_NAMES,
        package_import_root=package_import_root,
    )


# ---------------------------------------------------------------------------
# MCP tool registration (FastMCP-like server; see server.py for the sibling
# coordinator-facing pattern this mirrors)
# ---------------------------------------------------------------------------

MCP_TOOL_NAMES: tuple[str, ...] = (
    "aiworkhub_worker_source_graph_query",
    "aiworkhub_worker_session_current_state",
    "aiworkhub_worker_ai_memory_search",
    "aiworkhub_worker_ai_memory_get",
    "aiworkhub_worker_ai_memory_related",
    "aiworkhub_worker_kb_search",
    "aiworkhub_worker_kb_get",
    "aiworkhub_worker_kb_related",
    "aiworkhub_worker_session_write_intent",
    "aiworkhub_worker_ai_memory_write_intent",
    "aiworkhub_worker_kb_write_intent",
    "aiworkhub_worker_quality_review_submit",
)


def register_tools(mcp: Any, ctx: WorkerToolContext) -> tuple[str, ...]:
    """Register every worker-safe tool, bound to ``ctx``, on ``mcp``.

    No registered tool accepts a repository path, database path, task_id,
    runner or topic argument -- every one of those is closed over from
    ``ctx`` and cannot be overridden by the caller.
    """

    @mcp.tool(name="aiworkhub_worker_source_graph_query")
    def _source_graph_query(
        mode: SourceGraphMode, query: str, budget: int = 64,
        target: str | None = None, bundle_type: SourceGraphBundleType = "explore",
        workflow_stage: WorkflowStage = "unspecified",
    ) -> dict[str, Any]:
        """Bounded Source Graph discovery for this task."""
        return source_graph_query(
            ctx, mode=mode, query=query, budget=budget,
            target=target, bundle_type=bundle_type, workflow_stage=workflow_stage,
        )

    @mcp.tool(name="aiworkhub_worker_session_current_state")
    def _session_current_state(limit: int = 12) -> dict[str, Any]:
        """Bounded Session Manager current-state for this task's topic."""
        return session_current_state(ctx, limit=limit)

    @mcp.tool(name="aiworkhub_worker_ai_memory_search")
    def _ai_memory_search(query: str, limit: int = 8) -> dict[str, Any]:
        """Bounded AI Memory search."""
        return ai_memory_search(ctx, query=query, limit=limit)

    @mcp.tool(name="aiworkhub_worker_ai_memory_get")
    def _ai_memory_get(key: str) -> dict[str, Any]:
        """Bounded AI Memory exact-key lookup."""
        return ai_memory_get(ctx, key=key)

    @mcp.tool(name="aiworkhub_worker_ai_memory_related")
    def _ai_memory_related(key: str) -> dict[str, Any]:
        """Bounded AI Memory related-entry lookup."""
        return ai_memory_related(ctx, key=key)

    @mcp.tool(name="aiworkhub_worker_kb_search")
    def _kb_search(query: str, limit: int = 8) -> dict[str, Any]:
        """Bounded KB full-text search."""
        return kb_search(ctx, query=query, limit=limit)

    @mcp.tool(name="aiworkhub_worker_kb_get")
    def _kb_get(key: str) -> dict[str, Any]:
        """Bounded KB exact-key lookup."""
        return kb_get(ctx, key=key)

    @mcp.tool(name="aiworkhub_worker_kb_related")
    def _kb_related(key: str) -> dict[str, Any]:
        """Bounded KB related-entries lookup."""
        return kb_related(ctx, key=key)

    @mcp.tool(name="aiworkhub_worker_session_write_intent")
    def _session_write_intent(
        action: str, content: str, idempotency_key: str, provenance: str,
    ) -> dict[str, Any]:
        """Submit a bounded Session proposal for explicit manager review."""
        return session_write_intent(
            ctx, action=action, content=content,
            idempotency_key=idempotency_key, provenance=provenance,
        )

    @mcp.tool(name="aiworkhub_worker_ai_memory_write_intent")
    def _ai_memory_write_intent(
        action: str, key: str, idempotency_key: str, provenance: str,
        value: str = "", tags: str = "", scope: str = "project",
    ) -> dict[str, Any]:
        """Submit a bounded AI Memory proposal for explicit manager review."""
        return ai_memory_write_intent(
            ctx, action=action, key=key, value=value, tags=tags, scope=scope,
            idempotency_key=idempotency_key, provenance=provenance,
        )

    @mcp.tool(name="aiworkhub_worker_kb_write_intent")
    def _kb_write_intent(
        action: str, key: str, idempotency_key: str, provenance: str,
        title: str = "", body: str = "", category: str = "", tags: str = "",
        source_refs: str = "", replacement_key: str = "",
    ) -> dict[str, Any]:
        """Submit a bounded KB proposal for explicit manager review."""
        return kb_write_intent(
            ctx, action=action, key=key, title=title, body=body,
            category=category, tags=tags, source_refs=source_refs,
            replacement_key=replacement_key, idempotency_key=idempotency_key,
            provenance=provenance,
        )

    @mcp.tool(name="aiworkhub_worker_quality_review_submit")
    def _quality_review_submit(
        packet_sha256: str,
        lens: str,
        findings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Submit findings for the exact bound anti-anchored packet."""
        return quality_review_submit(
            ctx,
            packet_sha256=packet_sha256,
            lens=lens,
            findings=findings,
        )

    return MCP_TOOL_NAMES


def build_server(ctx: WorkerToolContext) -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError:
        from .stdio_fastmcp import FallbackFastMCP as FastMCP

    mcp = FastMCP("AIWorkHub Worker AI Tools")
    register_tools(mcp, ctx)
    return mcp


def main() -> None:
    ctx = load_context_from_env(os.environ)
    server = build_server(ctx)
    server.run()


if __name__ == "__main__":
    main()


__all__ = [
    "AUDIT_ENTRY_SCHEMA_ID",
    "AUDIT_LEDGER_VERIFICATION_SCHEMA_ID",
    "AuthorityBinding",
    "BOUND_ENV_VARS",
    "ENV_AUDIT_HMAC_KEY_PATH",
    "ENV_AUDIT_LEDGER_PATH",
    "ENV_AUTHORITY_REPO",
    "ENV_REPO",
    "ENV_PYTHONPATH",
    "ENV_REQUEST_ID",
    "ENV_RUNNER",
    "ENV_SESSION_TOPIC",
    "ENV_SOURCE_GRAPH_TARGETS",
    "ENV_TASK_ID",
    "ENV_TOPIC",
    "ENV_QUALITY_REVIEW_PACKET_PATH",
    "MCP_TOOL_NAMES",
    "RUNTIME_SCHEMA_ID",
    "SERVER_NAME",
    "SOURCE_GRAPH_BUNDLE_TYPES",
    "SOURCE_GRAPH_MODES",
    "STORAGE_REGISTRY_RELATIVE_PATH",
    "WorkerMcpRuntime",
    "WorkerToolContext",
    "WorkerToolError",
    "ai_memory_search",
    "build_server",
    "generate_worker_mcp_runtime",
    "kb_get",
    "kb_related",
    "kb_search",
    "load_context_from_env",
    "register_tools",
    "session_write_intent",
    "ai_memory_write_intent",
    "kb_write_intent",
    "quality_review_submit",
    "resolve_host_package_import_root",
    "session_current_state",
    "source_graph_query",
    "verify_audit_ledger",
]
