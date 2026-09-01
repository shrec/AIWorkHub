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

import base64
import hashlib
import hmac
import json
import errno
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
from typing import Any, Literal, Mapping, NamedTuple, Sequence

try:
    from .platform_io import chmod_fd
except ImportError:  # minimal copied worker package / direct-script mode
    from platform_io import chmod_fd

from .repository_state import RepositoryStateError
from . import quality_reviewer
from . import semantic_edit
from .sqlite_readonly import connect_readonly


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
ENV_ALLOWED_WRITES = "AIWORKHUB_WORKER_MCP_ALLOWED_WRITES"
ENV_SESSION_TOPIC = "AIWORKHUB_WORKER_MCP_SESSION_TOPIC"
ENV_AUDIT_LEDGER_PATH = "AIWORKHUB_WORKER_MCP_AUDIT_LEDGER_PATH"
ENV_AUDIT_HMAC_KEY_PATH = "AIWORKHUB_WORKER_MCP_AUDIT_HMAC_KEY_PATH"
ENV_QUALITY_REVIEW_PACKET_PATH = "AIWORKHUB_WORKER_MCP_QUALITY_REVIEW_PACKET_PATH"
ENV_REWORK_OVERLAY_PATH = "AIWORKHUB_REWORK_OVERLAY_PATH"
ENV_PROVIDER_CALL_ID = "AIWORKHUB_WORKER_MCP_PROVIDER_CALL_ID"
ENV_PROVENANCE = "AIWORKHUB_WORKER_MCP_PROVENANCE"
# The interpreter's own import-path variable (never an AIWORKHUB_* identity
# binding) -- carries the portable ".../src" import root so `python -m
# aiworkhub.worker_ai_tools_mcp` resolves regardless of the launcher's cwd.
ENV_PYTHONPATH = "PYTHONPATH"

BOUND_ENV_VARS: tuple[str, ...] = (
    ENV_TASK_ID, ENV_RUNNER, ENV_TOPIC, ENV_REQUEST_ID, ENV_REPO, ENV_AUTHORITY_REPO,
    ENV_SOURCE_GRAPH_TARGETS, ENV_ALLOWED_WRITES, ENV_SESSION_TOPIC,
    ENV_AUDIT_LEDGER_PATH, ENV_AUDIT_HMAC_KEY_PATH,
    ENV_PROVIDER_CALL_ID, ENV_PROVENANCE,
)

# Bounded identity: a provider_call_id must be a short, printable, non-empty
# string with no control/space characters. This mirrors route_identity.py's
# ``_MAX_PROVIDER_LEN = 32`` so native and text paths share one bound.
MAX_PROVIDER_CALL_ID_LEN = 32
PROVENANCE_VALUES: frozenset[str] = frozenset({
    "prefetch", "live", "cache", "continuation",
})

# ``\Z`` (not ``$``) and ``fullmatch`` together reject any trailing newline or
# whitespace so a spoofed identity like ``pci_ok\n`` can never reach the ledger.
_UID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,%d}\Z" % (MAX_PROVIDER_CALL_ID_LEN - 1))


def validate_provider_call_id(value: object) -> str:
    """Return a bounded, authenticated-shaped ``provider_call_id`` or fail closed.

    Accepts an existing provider identifier (``codex``/``claude``/``copilot``
    call ids, ``pci_...`` synthesized ids) as long as it is bounded and
    printable. Only a genuine ``str`` is accepted; scalar values (``int``,
    ``bool``, ``float``) fail closed so a spoofed numeric identity can never be
    coerced into the ledger. Rejects empty, oversized, malformed or
    whitespace-bearing values with a named error.
    """

    if value is None:
        raise WorkerToolError("worker_mcp_provider_call_id_missing")
    if type(value) is not str:
        raise WorkerToolError("worker_mcp_provider_call_id_malformed")
    text = value
    if not text:
        raise WorkerToolError("worker_mcp_provider_call_id_empty")
    if len(text) > MAX_PROVIDER_CALL_ID_LEN:
        raise WorkerToolError("worker_mcp_provider_call_id_oversized")
    if not _UID_RE.fullmatch(text):
        raise WorkerToolError("worker_mcp_provider_call_id_malformed")
    return text


def validate_provenance(value: object) -> str:
    """Return a bounded provenance label or fail closed.

    Only ``prefetch`` (injected/predictive observations), ``live`` (a genuine
    provider call) and ``cache`` (a replayed result) are accepted; empty or
    spoofed provenance is rejected with a named error. Provenance is auditable
    but never satisfies the live Source Graph gate by itself. Only a genuine
    ``str`` is accepted — scalar values (``int``, ``bool``, ``float``) and any
    object with a custom ``__str__`` fail closed so a spoofed value can never
    be coerced into the ledger, mirroring ``validate_provider_call_id``.
    """

    if value is None:
        raise WorkerToolError("worker_mcp_provenance_missing")
    if type(value) is not str:
        raise WorkerToolError("worker_mcp_provenance_invalid")
    if value not in PROVENANCE_VALUES:
        raise WorkerToolError("worker_mcp_provenance_invalid")
    return value

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
SOURCE_GRAPH_ANALYTIC_MODES: frozenset[str] = frozenset({
    "tags", "hotspots", "coverage", "churn", "reviewqueue", "ownership",
    "testmap", "calls", "symbols", "bottlenecks", "auditmap", "complexity",
    "stats", "summarize", "pipeline",
    "todo", "leaks", "nullrisks", "rawptrs", "casts", "crashes",
    "looprisks", "deadmethods", "duplicates", "gaps",
})
# ``target`` means two incompatible things across modes, and the wrapper used
# to encode that split in two places -- which term the engine searched, and
# which modes were re-filtered by a path prefix -- that could, and did, drift
# apart.  State the distinction ONCE here so the engine call and the post-
# retrieval filter are both driven from the same set.  For a SELECTOR mode
# ``target`` is an exact symbol selector (a bare name or a ``<file>.<symbol>``
# qualname, e.g. the qualnames ``focus.recommended_next_steps`` emits): it is
# resolved by the engine and its result is never re-filtered by a path prefix,
# because a file path never starts with a qualname and that filter would drop
# every resolved symbol.  For every other non-analytic mode ``target`` is a
# PATH SCOPE applied as a prefix filter over the returned payload.  Analytic
# modes are neither -- ``analytics_query`` consumes ``target``/``cursor`` inside
# the engine (see ``SOURCE_GRAPH_ANALYTIC_MODES`` above).
SOURCE_GRAPH_SELECTOR_MODES: frozenset[str] = frozenset({"slice", "body", "function"})
# The selector modes whose resolved symbol must additionally honour the file
# allowlist: an out-of-scope symbol is refused BY NAME, never emptied.  ``slice``
# returns its resolved dependency neighbourhood verbatim (behaviour unchanged),
# so it is derived out of the file re-check here rather than maintained as a
# separate hand-written list.
SOURCE_GRAPH_SYMBOL_SELECTOR_MODES: frozenset[str] = (
    SOURCE_GRAPH_SELECTOR_MODES - {"slice"}
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
SOURCE_GRAPH_ORIENTATION_OUTPUT_BYTES = 8 * 1024
SOURCE_GRAPH_ANALYSIS_OUTPUT_BYTES = 12 * 1024
MAX_RAW_TOOL_OUTPUT_BYTES = 512 * 1024
# Signed outer-pagination continuation (NF-2026-00510).  When the exact
# canonical JSON bytes of a Source Graph response exceed a mode's outer output
# cap, the worker pages those bytes across a signed cursor instead of
# discarding the tail into a lossy preview.  Continuation state is process-local
# and bounded by TTL, entry count and retained bytes; every page (initial and
# continuation) appends an authenticated audit entry.
CONTINUATION_SCHEMA_ID = "aiworkhub.task_mcp.source_graph_continuation.v1"
SOURCE_GRAPH_CONTINUATION_TTL_SECONDS = 600.0
SOURCE_GRAPH_CONTINUATION_MAX_ENTRIES = 256
SOURCE_GRAPH_CONTINUATION_MAX_BYTES = 8 * 1024 * 1024
MAX_DECLARED_INPUT_PREVIEW_BYTES = 12 * 1024
MAX_DECLARED_INPUT_HASH_BYTES = 8 * 1024 * 1024
MAX_QUALITY_REVIEW_PACKET_BYTES = 256 * 1024
MAX_REWORK_OVERLAY_PACKET_BYTES = 12 * 1024 * 1024
MAX_REWORK_OVERLAY_FILES = 512
MAX_QUALITY_REVIEW_FINDINGS = 50
SQLITE_QUERY_TIMEOUT_SECONDS = 5
SESSION_SNIPPET_CHARS = 280


class WorkerToolError(RuntimeError):
    """A bounded, fail-closed worker MCP tool failure."""


def _source_graph_output_cap(mode: SourceGraphMode) -> int:
    """Return the response-byte ceiling appropriate for one graph mode.

    ``focus`` and ``slice`` are the low-token orientation path advertised to
    managers and workers, so they receive the smallest envelope.  Execution
    flow, impact and validation-ownership modes retain a larger analysis
    budget, while content-rich and repository-wide modes keep the existing
    global ceiling.  The structure-aware JSON renderer preserves semantic
    priority keys whenever truncation is required.
    """

    if mode in {"focus", "slice"}:
        return SOURCE_GRAPH_ORIENTATION_OUTPUT_BYTES
    if mode in {
        "context", "impact", "trace", "deps", "coverage", "testmap",
        "calls", "symbols", "bottlenecks", "auditmap", "complexity",
    }:
        return SOURCE_GRAPH_ANALYSIS_OUTPUT_BYTES
    return MAX_TOOL_OUTPUT_BYTES


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
    allowed_writes: tuple[str, ...] = ()
    rework_overlay_packet: dict[str, Any] | None = None
    rework_overlay_packet_path: Path | None = None
    provider_call_id: str = ""
    provenance: str = ""
    _supervisor_owned: bool = False


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
    allowed_raw = str(source.get(ENV_ALLOWED_WRITES) or "[]")
    try:
        allowed_parsed = json.loads(allowed_raw)
    except json.JSONDecodeError:
        allowed_parsed = []
    allowed_writes = tuple(
        str(item) for item in allowed_parsed if isinstance(item, str) and item.strip()
    ) if isinstance(allowed_parsed, list) else ()
    ledger_raw = source.get(ENV_AUDIT_LEDGER_PATH) or ""
    key_raw = source.get(ENV_AUDIT_HMAC_KEY_PATH) or ""
    review_packet_raw = source.get(ENV_QUALITY_REVIEW_PACKET_PATH) or ""
    rework_overlay_raw = source.get(ENV_REWORK_OVERLAY_PATH) or ""
    rework_packet: dict[str, Any] | None = None
    if rework_overlay_raw:
        overlay_path = Path(rework_overlay_raw).resolve()
        if overlay_path.is_symlink() or not overlay_path.is_file():
            raise WorkerToolError(f"worker_mcp_rework_overlay_path_not_file:{overlay_path}")
        if overlay_path.stat().st_size > MAX_REWORK_OVERLAY_PACKET_BYTES:
            raise WorkerToolError("worker_mcp_rework_overlay_packet_too_large")
        try:
            rework_packet = json.loads(overlay_path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerToolError("worker_mcp_rework_overlay_packet_unreadable") from exc
        if not isinstance(rework_packet, dict):
            raise WorkerToolError("worker_mcp_rework_overlay_packet_invalid")
        _verify_rework_overlay_packet(
            rework_packet,
            str(source[ENV_TASK_ID]),
            str(source.get(ENV_REQUEST_ID) or ""),
            str(source[ENV_RUNNER]),
            authority_repo,
        )
        materialize_rework_overlay_sealed_files(
            repo, rework_packet, allowed_writes=allowed_writes,
        )
    # NF389/r6: only an ABSENT key retains the backward-compatible empty
    # sentinel. A PRESENT key -- even an explicit empty string -- must route
    # through the fail-closed validators so an empty/spoofed identity can never
    # be silently coerced into the audit context.
    provider_call_id = ""
    if ENV_PROVIDER_CALL_ID in source:
        provider_call_id = validate_provider_call_id(source.get(ENV_PROVIDER_CALL_ID))
    provenance = ""
    if ENV_PROVENANCE in source:
        provenance = validate_provenance(source.get(ENV_PROVENANCE))
    return WorkerToolContext(
        task_id=str(source[ENV_TASK_ID]),
        runner=str(source[ENV_RUNNER]),
        topic=str(source[ENV_TOPIC]),
        request_id=str(source.get(ENV_REQUEST_ID) or ""),
        repo=repo,
        authority_repo=authority_repo,
        source_graph_targets=targets,
        allowed_writes=allowed_writes,
        session_topic=str(source.get(ENV_SESSION_TOPIC) or source[ENV_TOPIC]),
        audit_ledger_path=Path(str(ledger_raw)) if ledger_raw else None,
        audit_hmac_key_path=Path(str(key_raw)) if key_raw else None,
        quality_review_packet_path=(
            Path(str(review_packet_raw)) if review_packet_raw else None
        ),
        rework_overlay_packet=rework_packet,
        rework_overlay_packet_path=(overlay_path if rework_overlay_raw else None),
        provider_call_id=provider_call_id,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# Bounded, read-only, in-process SQLite queries (never a subprocess, never a
# fixed AITools script path -- see module docstring, B878)
# ---------------------------------------------------------------------------

_FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_SQLITE_LIKE_ESCAPE_RE = re.compile(r"([%_\\\\])")


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


def _sqlite_like_literal(raw: str) -> str:
    return _SQLITE_LIKE_ESCAPE_RE.sub(r"\\\1", raw)


def _open_readonly_db(path: Path, *, tool: str) -> sqlite3.Connection:
    try:
        con = connect_readonly(path, timeout=SQLITE_QUERY_TIMEOUT_SECONDS)
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
            # Retrieval provenance is useful even on a zero-hit response, but
            # it is not itself graph evidence.  Counting restored query
            # tokens as hits would turn an honest scoped miss into a false
            # positive and could satisfy the live Source Graph gate.
            if key == "query_tokens":
                saw_container = True
                continue
            if key in {
                "items", "results", "matches", "rows", "symbols", "files",
                "sections", "relevant_files", "candidate_files", "neighbors",
                "contexts", "entities", "edges", "call_edges",
                "cross_file_edges", "hot_symbols", "related_tests", "risks",
                "suspects", "todos",
            }:
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
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = canonical.encode("utf-8")
    if len(encoded) <= max_bytes:
        return canonical, False
    priority_keys = (
        "ranked_symbols",
        "related_tests",
        "risks",
        "todos",
        "recommended_next_steps",
    )
    identity_keys = (
        "schema_id", "ok", "tool", "mode", "query", "target", "hit_count", "budget",
        "coverage", "cursor", "next_cursor",
    )
    semantic_keys = (
        "name", "qualname", "file_path", "kind", "signature",
        "line_start", "line_end", "priority_score", "risk_reasons",
        "metrics_evidence", "confidence", "evidence_label",
    )

    def ordered_mapping_keys(value: dict[Any, Any]) -> list[Any]:
        ordered = [key for key in semantic_keys if key in value]
        ordered.extend(key for key in sorted(value, key=str) if key not in ordered)
        return ordered

    def preview_value(value: Any, depth: int = 0) -> Any:
        if depth >= 3:
            if isinstance(value, list):
                return {"truncated": True, "original_items": len(value)}
            if isinstance(value, dict):
                return {"truncated": True, "original_keys": len(value)}
        if isinstance(value, str):
            return value if len(value) <= 768 else value[:768] + "…"
        if isinstance(value, list):
            return [preview_value(item, depth + 1) for item in value[:3]]
        if isinstance(value, dict):
            return {
                str(key): preview_value(value[key], depth + 1)
                for key in ordered_mapping_keys(value)[:10]
            }
        return value

    wrapper: dict[str, Any] = {
        "schema_id": "aiworkhub.task_mcp.bounded_json_preview.v1",
        "truncated": True,
        "original_bytes": len(encoded),
        "original_sha256": hashlib.sha256(encoded).hexdigest(),
        "original_hit_count": _json_hit_count(payload),
        "preview_semantics": "structure_aware_priority_preserving",
        "preview": {},
    }
    preview: dict[str, Any] = wrapper["preview"]
    ordered_keys = [key for key in (*identity_keys, *priority_keys) if key in payload]
    ordered_keys.extend(key for key in sorted(payload) if key not in ordered_keys)
    omitted = 0
    for key in ordered_keys:
        preview[key] = preview_value(payload[key])
        candidate = json.dumps(
            wrapper, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if len(candidate.encode("utf-8")) <= max_bytes:
            continue
        preview.pop(key, None)
        omitted += 1
        if key in priority_keys:
            value = payload[key]
            preview[key] = {
                "truncated": True,
                "original_items": len(value) if isinstance(value, (list, dict)) else 1,
            }
            candidate = json.dumps(
                wrapper, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if len(candidate.encode("utf-8")) > max_bytes:
                preview.pop(key, None)
    wrapper["omitted_key_count"] = omitted
    wrapper["priority_keys_present"] = [key for key in priority_keys if key in preview]
    bounded = json.dumps(wrapper, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    while len(bounded.encode("utf-8")) > max_bytes:
        removable = next(
            (key for key in reversed(list(preview)) if key not in priority_keys),
            None,
        )
        if removable is None:
            break
        preview.pop(removable, None)
        wrapper["omitted_key_count"] += 1
        bounded = json.dumps(wrapper, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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
    authority_repo: Path | None = None
    target_request_id: str = ""
    target_task_id: str = ""
    packet_sha256: str = ""


_STORAGE_REGISTRY_CACHE: dict[str, dict[str, Any]] = {}
_SOURCE_GRAPH_DB_OVERRIDE_LOCK = threading.RLock()
_CANDIDATE_SOURCE_GRAPH_META_KEY = "candidate_packet_binding"


@contextmanager
def _with_source_graph_db(source_graph_module: Any, db_path: Path):
    """Context-locally bind Source Graph to one already-verified DB.

    The worker MCP server is one process per request, but FastMCP may dispatch
    concurrent calls.  ``database_path_override`` is a ``ContextVar``, so each
    caller's override is isolated from every other caller and restored in
    ``finally``.  Distinct overrides therefore never serialize on a shared
    lock.  This is required for a quality reviewer: its repository is the
    immutable combined candidate worktree while its ephemeral index lives in
    the private runtime directory, not in that read-only worktree.
    """

    override = getattr(source_graph_module, "database_path_override", None)
    if override is None:
        raise WorkerToolError("source_graph_context_override_unavailable")
    with override(db_path):
        yield


def _verify_quality_review_packet_binding(
    packet_path: Path,
    repo: Path,
) -> tuple[str, str, str, Path, list[dict[str, str]]]:
    """Verify the packet digest, target identity and every changed-path byte.

    Returns ``(packet_sha256, target_request_id, target_task_id, db_path,
    changed_paths)`` where ``db_path`` is the packet-bound private overlay the
    reviewer Source Graph reads from and ``changed_paths`` is the verified
    ``[{"path": ..., "sha256": ...}]`` list.  The exact packet/candidate-byte
    verification lives in
    :func:`quality_reviewer.verify_review_packet_candidate` and is consumed
    here by both the prewarm (build) path and the runtime (query-only) path,
    so the two can never disagree about which bytes were authorized.
    """

    from . import quality_reviewer

    try:
        verified = quality_reviewer.verify_review_packet_candidate(
            packet_path,
            repo,
            max_packet_bytes=MAX_QUALITY_REVIEW_PACKET_BYTES,
        )
    except quality_reviewer.ReviewerEvidenceError as exc:
        raise WorkerToolError(str(exc)) from exc
    packet_sha256 = verified["packet_sha256"]
    target_request_id = verified["target_request_id"]
    target_task_id = verified["target_task_id"]
    changed_paths = verified["changed_paths"]
    runtime_dir = packet_path.parent.resolve()
    db_path = runtime_dir / f"candidate_source_graph_{packet_sha256[:16]}.sqlite"
    return (
        packet_sha256, target_request_id, target_task_id, db_path, changed_paths,
    )


def _candidate_db_marker_value(
    packet_sha256: str, target_request_id: str, target_task_id: str,
) -> str:
    """Serialize the packet-bound marker stored in the overlay ``meta`` table."""

    return json.dumps(
        {
            "packet_sha256": packet_sha256,
            "target_request_id": target_request_id,
            "target_task_id": target_task_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _read_candidate_db_marker(db_path: Path) -> dict[str, Any] | None:
    """Read the packet-bound marker; ``None`` when missing or unreadable."""

    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key=?",
                (_CANDIDATE_SOURCE_GRAPH_META_KEY,),
            ).fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return None
    if row is None:
        return None
    try:
        payload = json.loads(str(row[0]))
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_candidate_db_marker(
    db_path: Path, packet_sha256: str, target_request_id: str, target_task_id: str,
) -> None:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (
                _CANDIDATE_SOURCE_GRAPH_META_KEY,
                _candidate_db_marker_value(
                    packet_sha256, target_request_id, target_task_id,
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()


_CANDIDATE_SOURCE_GRAPH_REQUIRED_TABLES = (
    "meta",
    "files",
    "entities",
    "edges",
    "entities_fts",
)


def _is_admitted_existing_changed_path(repo: Path, relative: str) -> bool:
    """Use the partition builder's canonical policy for readiness admission."""

    from . import source_graph as _source_graph_mod
    from . import source_graph_partition as _source_graph_partition

    candidate = (repo / relative).resolve(strict=False)
    return candidate.is_file() and _source_graph_partition._admits(
        _source_graph_mod.load_ignore_policy(repo), relative,
    )


def _candidate_db_is_ready(
    db_path: Path,
    repo: Path,
    packet_sha256: str,
    target_request_id: str,
    target_task_id: str,
    changed_paths: Sequence[Mapping[str, str]],
) -> bool:
    """True only when the overlay is schema-complete and exactly packet-bound.

    Marker-only readiness is insufficient: a partial, stale or tampered DB with
    a correct marker must fail closed.  The database is opened read-only and
    must (1) contain the Source Graph schema and (2) for every existing packet
    path admitted by the partition builder's canonical policy, contain the
    exact ``file_path`` and ``source_hash``. Deleted and policy-skipped paths
    legitimately have no row, including partitions with zero rows. This runs
    both before ``os.replace`` and again on
    runtime binding, so publish and query always agree on the same bytes.
    """

    try:
        if not db_path.is_file() or db_path.stat().st_size <= 0:
            return False
    except OSError:
        return False
    marker = _read_candidate_db_marker(db_path)
    if marker is None:
        return False
    if (
        str(marker.get("packet_sha256") or "") != packet_sha256
        or str(marker.get("target_request_id") or "") != target_request_id
        or str(marker.get("target_task_id") or "") != target_task_id
    ):
        return False
    if not changed_paths:
        return False
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                )
            }
            if not set(_CANDIDATE_SOURCE_GRAPH_REQUIRED_TABLES).issubset(tables):
                return False
            for changed in changed_paths:
                relative = str(changed.get("path") or "")
                expected = str(changed.get("sha256") or "")
                if not relative or not expected:
                    return False
                if not _is_admitted_existing_changed_path(repo, relative):
                    continue
                found = conn.execute(
                    "SELECT source_hash FROM files WHERE file_path=?",
                    (relative,),
                ).fetchone()
                if found is None or str(found[0]) != expected:
                    return False
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return False
    return True


class _CandidatePrewarmFlight:
    """Single-flight guard for one packet-bound candidate overlay build.

    Distinct candidate DBs share no lock and build concurrently.  Concurrent
    callers for the same packet-bound ``db_path`` wait on this flight so the
    overlay is built, verified and atomically published exactly once.
    """

    __slots__ = ("_condition", "_done", "_result", "_error")

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._done = False
        self._result: dict[str, Any] | None = None
        self._error: BaseException | None = None

    def wait(self) -> None:
        with self._condition:
            while not self._done:
                self._condition.wait()
            if self._error is not None:
                raise self._error

    def publish(self, result: dict[str, Any]) -> None:
        with self._condition:
            self._result = result
            self._done = True
            self._condition.notify_all()

    def fail(self, exc: BaseException) -> None:
        with self._condition:
            self._error = exc
            self._done = True
            self._condition.notify_all()


_CANDIDATE_PREWARM_FLIGHTS: dict[Path, _CandidatePrewarmFlight] = {}
_CANDIDATE_PREWARM_FLIGHTS_LOCK = threading.Lock()


def _candidate_prewarm_flight(
    db_path: Path,
) -> tuple[_CandidatePrewarmFlight, bool]:
    """Return ``(flight, is_owner)`` for the overlay at ``db_path``."""
    with _CANDIDATE_PREWARM_FLIGHTS_LOCK:
        flight = _CANDIDATE_PREWARM_FLIGHTS.get(db_path)
        if flight is None:
            flight = _CandidatePrewarmFlight()
            _CANDIDATE_PREWARM_FLIGHTS[db_path] = flight
            return flight, True
        return flight, False


def _release_candidate_prewarm_flight(db_path: Path) -> None:
    with _CANDIDATE_PREWARM_FLIGHTS_LOCK:
        _CANDIDATE_PREWARM_FLIGHTS.pop(db_path, None)


def _candidate_prewarm_result(
    *,
    built: bool,
    db_path: Path,
    repo: Path,
    packet_sha256: str,
    target_request_id: str,
    target_task_id: str,
) -> dict[str, Any]:
    return {
        "ok": True,
        "built": built,
        "db_path": str(db_path),
        "authority_source": "candidate_overlay",
        "authority_state": "quality_review_readonly",
        "authority_repo": str(repo.resolve()),
        "target_request_id": target_request_id,
        "target_task_id": target_task_id,
        "packet_sha256": packet_sha256,
    }


def prewarm_quality_review_source_graph(
    packet_path: Path,
    *,
    repo: Path,
    authority_repo: Path,
) -> dict[str, Any]:
    """Prebuild and atomically publish the packet-bound reviewer candidate index.

    This is the only path that ever prepares a reviewer candidate Source
    Graph index, and it never calls ``build_index``: the launcher calls it
    after the review packet is materialized and before the reviewer provider
    is launched.  The candidate is NO LONGER a clone of the canonical
    database.  Instead :func:`source_graph_partition.build_partition` indexes
    this packet's ``changed_paths`` ALONE into a fresh partition and records the
    canonical base (opened read-only) so a later reviewer query composes the two
    without ever copying the base -- preparation cost now scales with the changed
    set, not repository size.  Reconciliation still runs through the same
    exact-file ``index_file``/``remove_file`` primitives.  This function always
    re-derives the canonical binding from ``authority_repo`` itself via
    :func:`verify_quality_review_prewarm_authority`, so a forged or stale
    binding can never be smuggled in by a caller.  Runtime reviewer Source
    Graph calls remain query-only and never invoke ``build_index`` either.
    The partition build is bound context-locally through
    ``source_graph.database_path_override`` (a ``ContextVar``), so distinct
    candidate DBs build concurrently without any process-global lock.  Only
    callers for the same packet-bound ``db_path`` single-flight, so the
    candidate is verified and published exactly once.
    """

    from . import source_graph as _source_graph_mod
    from . import source_graph_partition as _source_graph_partition

    canonical = verify_quality_review_prewarm_authority(authority_repo)

    packet_sha256, target_request_id, target_task_id, db_path, changed_paths = (
        _verify_quality_review_packet_binding(packet_path, repo)
    )
    if db_path.is_symlink():
        raise WorkerToolError("quality_review_candidate_source_graph_symlink")
    if _candidate_db_is_ready(
        db_path, repo, packet_sha256, target_request_id, target_task_id,
        changed_paths,
    ):
        return _candidate_prewarm_result(
            built=False,
            db_path=db_path,
            repo=repo,
            packet_sha256=packet_sha256,
            target_request_id=target_request_id,
            target_task_id=target_task_id,
        )

    flight, is_owner = _candidate_prewarm_flight(db_path)
    if not is_owner:
        flight.wait()
        if not _candidate_db_is_ready(
            db_path, repo, packet_sha256, target_request_id, target_task_id,
            changed_paths,
        ):
            raise WorkerToolError("quality_review_candidate_source_graph_empty")
        return _candidate_prewarm_result(
            built=False,
            db_path=db_path,
            repo=repo,
            packet_sha256=packet_sha256,
            target_request_id=target_request_id,
            target_task_id=target_task_id,
        )

    try:
        temporary = db_path.parent / f".{db_path.name}.{secrets.token_hex(8)}.tmp"
        try:
            # The full-index clone is gone. Instead of cloning the canonical
            # database and reconciling changed files back onto it -- cost
            # proportional to repository size -- build a PARTITION over this
            # packet's changed paths alone, and record the base (opened
            # read-only) so a later reviewer query composes the two without any
            # copy. Cost now scales with the changed set, not the base index.
            # ``build_partition`` reconciles each indexable path through the same
            # exact-file ``index_file``/``remove_file`` primitives as before, so
            # a Source Graph contract failure still surfaces as a
            # ``SourceGraphError`` normalized by the handler below; a path-safety
            # violation keeps its existing WorkerToolError identity.
            try:
                _source_graph_partition.build_partition(
                    repo, changed_paths, temporary,
                    base_db_path=canonical.db_path,
                )
            except _source_graph_partition.PartitionError as exc:
                raise WorkerToolError(str(exc)) from exc
            _write_candidate_db_marker(
                temporary, packet_sha256, target_request_id, target_task_id,
            )
            if not _candidate_db_is_ready(
                temporary, repo, packet_sha256, target_request_id, target_task_id,
                changed_paths,
            ):
                raise WorkerToolError(
                    "quality_review_candidate_source_graph_empty"
                )
            os.replace(temporary, db_path)
        finally:
            temporary.unlink(missing_ok=True)
        if not _candidate_db_is_ready(
            db_path, repo, packet_sha256, target_request_id, target_task_id,
            changed_paths,
        ):
            raise WorkerToolError("quality_review_candidate_source_graph_empty")
        result = _candidate_prewarm_result(
            built=True,
            db_path=db_path,
            repo=repo,
            packet_sha256=packet_sha256,
            target_request_id=target_request_id,
            target_task_id=target_task_id,
        )
        flight.publish(result)
        return result
    except WorkerToolError as exc:
        flight.fail(exc)
        raise
    except (_source_graph_mod.SourceGraphError, sqlite3.Error, OSError) as exc:
        # These are prewarm-owned Source Graph/candidate-DB contract and data
        # failures (partition build I/O, extraction, hash/schema mismatch) --
        # never a provider process launch anomaly.  Wrapping them in
        # WorkerToolError lets the launcher's existing WorkerToolError catch
        # classify them truthfully as
        # "quality_review_source_graph_prewarm_failed" instead of falling
        # through to the generic unexpected-launch-error path, and every
        # ``flight.wait()`` joiner observes this same normalized error.
        wrapped = WorkerToolError(
            "quality_review_candidate_source_graph_prewarm_error:"
            + type(exc).__name__ + ":" + str(exc)[:240]
        )
        flight.fail(wrapped)
        raise wrapped from exc
    except BaseException as exc:
        flight.fail(exc)
        raise
    finally:
        _release_candidate_prewarm_flight(db_path)


def _candidate_source_graph_binding(ctx: WorkerToolContext) -> AuthorityBinding | None:
    """Return the prebuilt packet-bound reviewer candidate index, query-only.

    Ordinary workers have no quality-review packet and return ``None``.  A
    reviewer re-verifies the packet digest, target identity and every
    changed-path byte against the immutable combined worktree, then reads the
    overlay the launcher prewarmed with
    :func:`prewarm_quality_review_source_graph`.  Missing, empty, mismatched or
    corrupt overlays fail closed here -- this function never builds an index,
    so a provider Source Graph call can never wedge on synchronous index
    construction.
    """

    path = ctx.quality_review_packet_path
    if path is None:
        return None
    packet_sha256, target_request_id, target_task_id, db_path, changed_paths = (
        _verify_quality_review_packet_binding(path, ctx.repo)
    )
    if db_path.is_symlink():
        raise WorkerToolError("quality_review_candidate_source_graph_symlink")
    if not _candidate_db_is_ready(
        db_path, ctx.repo, packet_sha256, target_request_id, target_task_id,
        changed_paths,
    ):
        raise WorkerToolError("quality_review_candidate_source_graph_unavailable")
    return AuthorityBinding(
        db_path=db_path,
        authority_source="candidate_overlay",
        authority_state="quality_review_readonly",
        authority_repo=ctx.repo.resolve(),
        target_request_id=target_request_id,
        target_task_id=target_task_id,
        packet_sha256=packet_sha256,
    )


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


def _canonical_source_graph_binding_for_repo(authority_repo: Path) -> AuthorityBinding:
    """Resolve and verify the repository's sole canonical Source Graph database.

    Unlike :func:`_resolve_authority_db`, Source Graph is never read through a
    legacy/canonical toggle (task B849 made ``aiworkhub.source_graph`` the
    sole implementation and storage authority for this component), so once
    the registry entry is verified ``canonical_active`` in state
    ``canonical_active`` the resolved database is, by architecture, always
    the sole authority.  This still fails closed on every other forgery or
    corruption vector: an unresolved repository identity, a missing registry
    entry, any state other than a verified canonical cutover (rejecting
    shadow/rolled-back/forged entries), a resolved path that escapes
    ``authority_repo`` or is symlinked, a missing/empty/unreadable file, a
    schema-incomplete database, or a database whose recorded generation
    ``build_revision`` does not match this runtime's own -- a stale or
    wrong-generation database is never treated as authoritative.
    """

    from . import source_graph as _source_graph_mod
    from . import storage_registry as _storage_registry_mod

    resolved_repo = Path(authority_repo).resolve()
    try:
        registry = _storage_registry_mod.load_storage_registry(resolved_repo)
    except (RepositoryStateError, _storage_registry_mod.StorageRegistryError) as exc:
        raise WorkerToolError(
            f"authority_registry_unresolved:source_graph.source_graph:{exc}"
        ) from exc
    db = registry.databases.get("source_graph")
    if db is None:
        raise WorkerToolError("authority_registry_entry_missing:source_graph.source_graph")
    if not db.canonical_active or db.authority_state != "canonical_active":
        raise WorkerToolError(
            "authority_component_not_canonical_active:"
            f"source_graph.source_graph:{db.authority_state}"
        )
    try:
        db_path = _storage_registry_mod.resolve_database_path(registry, "source_graph")
    except _storage_registry_mod.StorageRegistryError as exc:
        raise WorkerToolError(
            f"authority_registry_unresolved:source_graph.source_graph:{exc}"
        ) from exc
    if not db_path.is_relative_to(resolved_repo):
        raise WorkerToolError("authority_db_path_escapes_repo:source_graph.source_graph")
    if db_path.is_symlink():
        raise WorkerToolError("authority_source_graph_db_symlink")
    try:
        if not db_path.is_file() or db_path.stat().st_size <= 0:
            raise WorkerToolError(
                "authority_db_absent_or_empty:source_graph.source_graph:canonical"
            )
    except OSError as exc:
        raise WorkerToolError(f"authority_source_graph_db_unreadable:{exc}") from exc
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except (OSError, sqlite3.Error) as exc:
        raise WorkerToolError(f"authority_source_graph_db_unreadable:{exc}") from exc
    try:
        conn.execute("PRAGMA query_only=ON")
        try:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                )
            }
        except sqlite3.Error as exc:
            raise WorkerToolError(f"authority_source_graph_db_unreadable:{exc}") from exc
    finally:
        conn.close()
    if not set(_CANDIDATE_SOURCE_GRAPH_REQUIRED_TABLES).issubset(tables):
        raise WorkerToolError("authority_source_graph_db_schema_incomplete")
    identity = _source_graph_index_identity(
        db_path, default_revision=_source_graph_mod.BUILD_REVISION
    )
    if not identity["finished_at"]:
        raise WorkerToolError("authority_source_graph_db_generation_unrecorded")
    if identity["build_revision"] != _source_graph_mod.BUILD_REVISION:
        raise WorkerToolError(
            f"authority_source_graph_db_wrong_revision:{identity['build_revision']}"
        )
    return AuthorityBinding(
        db_path=db_path,
        authority_source="canonical",
        authority_state="sole_authority",
        authority_repo=resolved_repo,
    )


def _canonical_source_graph_binding(ctx: WorkerToolContext) -> AuthorityBinding:
    """Resolve the repository's canonical Source Graph database."""

    return _canonical_source_graph_binding_for_repo(ctx.authority_repo)


def verify_quality_review_prewarm_authority(authority_repo: Path) -> AuthorityBinding:
    """Fail-closed canonical Source Graph authority binding for reviewer prewarm.

    The launcher calls this before reviewer prewarm and before reviewer
    runtime/provider registration, so a launch fails closed on a
    noncanonical, mismatched, missing, symlinked, unreadable, empty,
    schema-incomplete, or wrong-revision generation before either side
    effect ever runs.  ``prewarm_quality_review_source_graph`` always
    re-derives this same binding from ``authority_repo`` itself rather than
    trusting a caller-supplied value, so a forged or stale binding can never
    bypass these checks.
    """

    return _canonical_source_graph_binding_for_repo(authority_repo)


_REWORK_OVERLAY_SNAPSHOT_META_KEY = "rework_overlay_snapshot"


def _rework_overlay_snapshot_digest(db_path: Path) -> str | None:
    """Return the overlay snapshot digest recorded in a rework partition.

    ``None`` when the marker is absent or the database is unreadable; the caller
    treats that as not-ready and rebuilds the packet-scoped partition.  The
    digest fingerprints the workspace bytes the partition was last built from,
    so a later worker edit of a retained path no longer matches and forces a
    rebuild -- without ever reading or copying the base.
    """

    try:
        conn = sqlite3.connect(
            f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True
        )
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key=?",
                (_REWORK_OVERLAY_SNAPSHOT_META_KEY,),
            ).fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return None
    return str(row[0]) if row is not None else None


def _write_rework_overlay_snapshot_digest(
    db_path: Path, snapshot_digest: str
) -> None:
    """Persist the overlay snapshot digest in the rework partition ``meta``.

    ``build_partition`` always creates the ``meta`` table (and writes the
    composed-view marker into it), so this only adds the overlay's own
    change-detection key beside it.
    """

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_REWORK_OVERLAY_SNAPSHOT_META_KEY, snapshot_digest),
        )
        conn.commit()
    finally:
        conn.close()


def _rework_source_graph_binding(ctx: WorkerToolContext) -> AuthorityBinding | None:
    """Return a request-local graph with retained predecessor paths overlaid.

    The durable canonical graph remains the sole authority and is NEVER copied.
    This request-scoped view is a PARTITION over the packet's retained paths
    alone, composed against the canonical base opened read-only -- the same
    primitive the reviewer prewarm uses
    (:func:`source_graph_partition.build_partition`).  Preparation cost scales
    with the packet, not with repository size, because the base index is never
    read, copied, or opened here.  The overlay stays keyed on the packet
    ``canonical_digest``.  A later worker edit of a retained path changes the
    observed workspace snapshot, so the packet-scoped partition is rebuilt on
    the next query and the worker never falls back to stale canonical bytes --
    still without copying the base.
    """

    packet = ctx.rework_overlay_packet
    packet_path = ctx.rework_overlay_packet_path
    if packet is None:
        return None
    if packet_path is None:
        raise WorkerToolError("rework_overlay_packet_path_missing")

    from . import source_graph_partition as _source_graph_partition

    canonical = _canonical_source_graph_binding(ctx)
    runtime_dir = packet_path.parent.resolve()
    if packet_path.resolve().parent != runtime_dir:
        raise WorkerToolError("rework_overlay_runtime_invalid")
    packet_digest = str(packet.get("canonical_digest") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", packet_digest):
        raise WorkerToolError("rework_overlay_digest_invalid")
    db_path = runtime_dir / f"rework_source_graph_{packet_digest[:16]}.sqlite"

    # Resolve the packet's retained paths against the isolated workspace ONCE,
    # deriving both the partition's ``changed_paths`` input and the binding
    # snapshot.  The path-safety and symlink refusals keep their original
    # ``rework_overlay_*`` identities and fire before any build side effect;
    # ``build_partition`` re-checks the same invariants against the base's
    # admission policy.  ``changed_paths`` is the packet-shaped
    # ``[{"path", "sha256"}]`` list the partition consumes -- the rework packet
    # carries extra ``content_base64`` used by the separate in-memory overlay,
    # which the partition simply ignores, so no shape is loosened at the seam.
    repo_root = ctx.repo.resolve()
    changed_paths: list[dict[str, str]] = []
    snapshot_rows: list[dict[str, Any]] = []
    for entry in packet["files"]:
        relative = str(entry["path"])
        raw = ctx.repo / relative
        candidate = raw.resolve(strict=False)
        if not candidate.is_relative_to(repo_root):
            raise WorkerToolError(
                f"rework_overlay_path_escapes_workspace:{relative}"
            )
        # ``resolve`` canonicalizes the final component, so a symlink must be
        # tested on the UNRESOLVED path (as the partition primitive does on its
        # own candidates); a post-resolve ``is_symlink`` can never fire.
        if raw.is_symlink():
            raise WorkerToolError(f"rework_overlay_path_symlink:{relative}")
        expected_hash = str(entry.get("sha256") or "")
        sealed = None
        if entry.get("deleted") is not True:
            sealed = _sealed_rework_overlay_bytes(entry, relative)
        if sealed is not None and not candidate.exists():
            materialize_rework_overlay_sealed_files(
                ctx.repo, {"files": [entry]}, allowed_writes=ctx.allowed_writes,
            )
            candidate = raw.resolve(strict=False)
        if candidate.is_file():
            observed_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
            admission = _admit_rework_overlay_observation(
                relative=relative,
                expected_hash=expected_hash,
                observed_hash=observed_hash,
                has_sealed=sealed is not None,
                allowed_writes=ctx.allowed_writes,
            )
            if admission == "authorized_overlay":
                changed_paths.append({"path": relative, "sha256": observed_hash})
                snapshot_rows.append({
                    "path": relative,
                    "sha256": expected_hash,
                    "observed_sha256": observed_hash,
                })
            else:
                sealed_hash = expected_hash or observed_hash
                changed_paths.append({"path": relative, "sha256": sealed_hash})
                snapshot_rows.append({"path": relative, "sha256": sealed_hash})
        elif candidate.exists():
            raise WorkerToolError(f"rework_overlay_path_not_file:{relative}")
        elif expected_hash:
            changed_paths.append({"path": relative, "sha256": expected_hash})
            snapshot_rows.append({"path": relative, "sha256": expected_hash})
        else:
            changed_paths.append({"path": relative, "sha256": ""})
            snapshot_rows.append({"path": relative, "deleted": True})

    snapshot_digest = hashlib.sha256(
        json.dumps(
            {"packet": packet_digest, "files": snapshot_rows},
            ensure_ascii=True,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    with _SOURCE_GRAPH_DB_OVERRIDE_LOCK:
        if db_path.is_symlink():
            raise WorkerToolError("rework_overlay_source_graph_symlink")
        # Readiness stays "the overlay DB exists and is non-empty"; an edited
        # workspace snapshot (a later worker edit of a retained path) is treated
        # as not-ready so the packet-scoped partition is rebuilt -- never the
        # base.  ``build_partition`` writes the composed-view marker itself, so
        # a later read-only open composes the two indexes without any copy.
        ready = (
            db_path.is_file()
            and db_path.stat().st_size > 0
            and _rework_overlay_snapshot_digest(db_path) == snapshot_digest
        )
        if not ready:
            temporary = runtime_dir / f".{db_path.name}.{secrets.token_hex(8)}.tmp"
            try:
                try:
                    _source_graph_partition.build_partition(
                        ctx.repo,
                        changed_paths,
                        temporary,
                        base_db_path=canonical.db_path,
                    )
                except _source_graph_partition.PartitionError as exc:
                    raise WorkerToolError(str(exc)) from exc
                _write_rework_overlay_snapshot_digest(temporary, snapshot_digest)
                os.replace(temporary, db_path)
            finally:
                temporary.unlink(missing_ok=True)

    return AuthorityBinding(
        db_path=db_path,
        authority_source="rework_overlay",
        authority_state="request_scoped_predecessor",
        authority_repo=ctx.repo,
        target_request_id=str(packet.get("predecessor_request_id") or ""),
        target_task_id=str(packet.get("predecessor_task_id") or ""),
        packet_sha256=snapshot_digest,
    )


def _resolve_source_graph_db(ctx: WorkerToolContext) -> AuthorityBinding:
    """Resolve canonical Source Graph or a request-scoped verified overlay.

    Unlike the other components resolved by ``_resolve_authority_db``, Source
    Graph is never read from a legacy/canonical toggle: task B849 made
    ``aiworkhub.source_graph`` the sole implementation and storage authority,
    so this always resolves through ``aiworkhub.storage_registry`` to
    ``<authority_repo>/.aiworkhub/source_graph/source_graph.sqlite`` and never
    falls back to ``AITools/source_graph.db``.
    """

    candidate = _candidate_source_graph_binding(ctx)
    if candidate is not None:
        return candidate
    rework = _rework_source_graph_binding(ctx)
    if rework is not None:
        return rework
    return _canonical_source_graph_binding(ctx)


def _source_graph_index_identity(db_path: Path, *, default_revision: str) -> dict[str, str]:
    """Return bounded canonical index identity without exposing its path.

    Full refreshes advance ``last_build`` while successful exact-file
    mutations advance ``single_file_last_mutation``.  Older runtimes used
    ``single_file_last_index`` for the latter, so retain it as a read-only
    compatibility source.  The newest valid timestamp is the repository-local
    cache-generation boundary.  Missing or malformed rows remain a truthful
    empty timestamp rather than turning a supported query into a false failure.
    """

    identity = {"build_revision": default_revision[:96], "finished_at": ""}
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT key, value FROM meta WHERE key IN "
                "('last_build', 'single_file_last_mutation', 'single_file_last_index')"
            ).fetchall()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return identity
    for _key, value in rows:
        if not isinstance(value, str):
            continue
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        finished_at = str(payload.get("finished_at") or "")[:64]
        if not finished_at or finished_at <= identity["finished_at"]:
            continue
        identity = {
            "build_revision": str(
                payload.get("build_revision") or default_revision
            )[:96],
            "finished_at": finished_at,
        }
    return identity


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
    authority_repo: Path | None = None,
    payload: Mapping[str, Any] | None = None,
    provider_call_id: str = "",
    provenance: str = "",
) -> bool:
    if ctx.audit_ledger_path is None or ctx.audit_hmac_key_path is None:
        return False
    try:
        key = ctx.audit_hmac_key_path.read_bytes()
    except OSError:
        return False
    effective_provider_call_id = provider_call_id or ctx.provider_call_id
    effective_provenance = provenance or ctx.provenance
    if effective_provider_call_id:
        try:
            effective_provider_call_id = validate_provider_call_id(
                effective_provider_call_id
            )
        except WorkerToolError:
            return False
    if effective_provenance:
        try:
            effective_provenance = validate_provenance(effective_provenance)
        except WorkerToolError:
            return False
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
        "authority_repo": str(authority_repo or ctx.authority_repo),
    }
    # NF389/r6: an unbound (empty) identity stays ABSENT from the authenticated
    # ledger. The verifier's absent-vs-empty distinction then treats a missing
    # key as the backward-compatible empty sentinel while a PRESENT-but-empty
    # (or otherwise invalid) value is a hard, fail-closed identity violation.
    if effective_provider_call_id:
        entry["provider_call_id"] = effective_provider_call_id[:MAX_PROVIDER_CALL_ID_LEN]
    if effective_provenance:
        entry["provenance"] = effective_provenance
    if payload is not None:
        encoded_payload = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        if len(encoded_payload) > MAX_TOOL_OUTPUT_BYTES:
            return False
        entry["payload"] = dict(payload)
    digest = _hmac_entry(entry, key)
    line = json.dumps({**entry, "hmac_sha256": digest}, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        _append_line_0600(ctx.audit_ledger_path, line)
    except OSError:
        return False
    return True


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
        "entries_invalid_identity": 0,
        "call_count_by_tool": {},
        "successful_call_count_by_tool": {},
        "bounded_bytes_returned": 0,
        "bounded_bytes_by_tool": {},
        "cache_hits": 0,
        "cache_hits_by_tool": {},
        "compact_replay": {
            "receipt_count": 0,
            "original_bytes": 0,
            "returned_bytes": 0,
            "bytes_avoided": 0,
            "provider_tokens_saved": None,
            "provider_token_savings_measured": False,
        },
        "policy_violations": 0,
        "fresh_source_graph_calls": 0,
        "live_source_graph_calls": 0,
        "source_graph_hit_count": 0,
        "source_graph_zero_hit_calls": 0,
        "source_graph_failed_calls": 0,
        # NF389/r6 live-scoped discipline counters. The counters above stay
        # backward-compatible: they aggregate EVERY authenticated source_graph
        # entry regardless of provenance (prefetch/live/cache). These new,
        # explicitly-named ``live_*`` counters aggregate ONLY authenticated
        # provenance=="live" entries, so a launch-time prefetch or a cache
        # replay is still auditable in the totals but can never inflate the
        # live discipline score or the dashboard-facing live metrics that
        # consume them. Provenance itself never satisfies the required-tool
        # gate; only ``live_source_graph_calls`` (a fresh, authoritative,
        # non-cache live call) does.
        "live_source_graph_call_count": 0,
        "live_source_graph_success_count": 0,
        "live_source_graph_hit_count": 0,
        "live_source_graph_zero_hit_calls": 0,
        "live_source_graph_failed_calls": 0,
        "live_source_graph_repeated_query_calls": 0,
        "live_source_graph_mode_counts": {},
        "live_source_graph_mode_sequence": [],
        "live_source_graph_query_sequence": [],
        "live_source_graph_stage_counts": {},
        "source_graph_mode_counts": {},
        "source_graph_mode_sequence": [],
        "source_graph_query_sequence": [],
        "source_graph_stage_counts": {},
        "source_graph_stage_sequence": [],
        "source_graph_mode_stage_counts": {},
        "source_graph_mode_attributed_calls": 0,
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
        "semantic_edit_apply_receipts": [],
        "provider_call_ids": [],
        "provenance_counts": {},
        "provider_call_id_by_tool": {},
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
    invalid_identity_codes: list[str] = []
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
        # NF389/r6: an HMAC-valid entry is authenticated but not yet
        # schema-valid. Re-run the exact bounded identity validators on the
        # authenticated fields BEFORE any aggregation or gate counting. An
        # ABSENT key is the backward-compatible empty sentinel; a PRESENT-but-
        # invalid value (empty, oversized, malformed, control characters, or a
        # provenance outside {prefetch, live, cache}) is an authenticated
        # identity violation -- fail closed, never copied raw, never counted.
        try:
            provider_call_id = (
                validate_provider_call_id(entry["provider_call_id"])
                if "provider_call_id" in entry
                else ""
            )
            provenance = (
                validate_provenance(entry["provenance"])
                if "provenance" in entry
                else ""
            )
        except WorkerToolError as exc:
            result["entries_invalid_identity"] += 1
            if str(exc) not in invalid_identity_codes:
                invalid_identity_codes.append(str(exc))
            continue
        result["entries_verified"] += 1
        tool = str(entry.get("tool") or "unknown")
        call_count[tool] = call_count.get(tool, 0) + 1
        if provider_call_id:
            if len(result["provider_call_ids"]) < 128:
                result["provider_call_ids"].append(provider_call_id)
            by_tool = result["provider_call_id_by_tool"].setdefault(tool, [])
            if len(by_tool) < 128:
                by_tool.append(provider_call_id)
        if provenance:
            result["provenance_counts"][provenance] = int(
                result["provenance_counts"].get(provenance) or 0
            ) + 1
        payload = entry.get("payload")
        if tool == "source_graph":
            # ``is_live`` gates the live-scoped discipline counters below. A
            # prefetch/cache/absent provenance row still updates every total
            # counter, but never the live ones.
            is_live = provenance == "live"
            source_hits = max(0, int(entry.get("hit_count") or 0))
            result["source_graph_hit_count"] += source_hits
            if source_hits == 0:
                result["source_graph_zero_hit_calls"] += 1
            if not entry.get("ok"):
                result["source_graph_failed_calls"] += 1
            # Both sequences describe the SAME calls, so both must record one
            # element per call -- ``receipt_conformance_report`` refuses on a
            # length difference. A FAILED source_graph call carries no usable
            # payload mode, and appending only when the mode was recognised made
            # the mode sequence shorter than the stage sequence by exactly the
            # number of failed calls, which read as a worker labelling fault and
            # was AIWorkHub's own bookkeeping. Mirror the stage recorder: an
            # unrecognised mode is ``unspecified`` and still occupies its slot.
            mode = str(payload.get("mode") or "") if isinstance(payload, dict) else ""
            mode_recognised = mode in SOURCE_GRAPH_MODES
            if not mode_recognised:
                mode = "unspecified"
            mode_counts = result["source_graph_mode_counts"]
            mode_counts[mode] = int(mode_counts.get(mode) or 0) + 1
            if mode_recognised:
                result["source_graph_mode_attributed_calls"] += 1
            if len(result["source_graph_mode_sequence"]) < 64:
                result["source_graph_mode_sequence"].append(mode)
            query_sha256 = (
                str(payload.get("query_sha256") or "")
                if isinstance(payload, dict) else ""
            )
            if (
                len(query_sha256) == 64
                and all(char in "0123456789abcdef" for char in query_sha256)
                and len(result["source_graph_query_sequence"]) < 64
            ):
                result["source_graph_query_sequence"].append(query_sha256)
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
            if mode_recognised:
                mode_stage = result["source_graph_mode_stage_counts"].setdefault(stage, {})
                mode_stage[mode] = int(mode_stage.get(mode) or 0) + 1
            # NF389/r6: live-scoped discipline accounting. ONLY genuine,
            # authenticated provenance=="live" rows feed the discipline score
            # and the dashboard live metrics. A prefetch ("prefetch") or a
            # cache replay ("cache") row is auditable in the total counters
            # above but must never enter these live-scoped ones -- that is the
            # exact defect this repair closes (one prefetch + one live call was
            # reported as source_graph=2 in the live discipline metrics).
            if is_live:
                result["live_source_graph_call_count"] += 1
                result["live_source_graph_hit_count"] += source_hits
                if source_hits == 0:
                    result["live_source_graph_zero_hit_calls"] += 1
                if entry.get("ok") and not entry.get("violation"):
                    result["live_source_graph_success_count"] += 1
                else:
                    result["live_source_graph_failed_calls"] += 1
                live_mode_counts = result["live_source_graph_mode_counts"]
                live_mode_counts[mode] = int(live_mode_counts.get(mode) or 0) + 1
                if len(result["live_source_graph_mode_sequence"]) < 64:
                    result["live_source_graph_mode_sequence"].append(mode)
                live_stage_counts = result["live_source_graph_stage_counts"]
                live_stage_counts[stage] = int(live_stage_counts.get(stage) or 0) + 1
                if (
                    len(query_sha256) == 64
                    and all(char in "0123456789abcdef" for char in query_sha256)
                    and len(result["live_source_graph_query_sequence"]) < 64
                ):
                    result["live_source_graph_query_sequence"].append(query_sha256)
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
                        "finished_at": (
                            str(payload.get("index_finished_at") or "")[:64]
                            if isinstance(payload, dict) else ""
                        ),
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
            if isinstance(payload, dict) and payload.get("compact_replay") is True:
                replay = result["compact_replay"]
                original = max(0, int(payload.get("replay_original_bytes") or 0))
                returned = max(0, int(payload.get("replay_returned_bytes") or 0))
                avoided = max(0, int(payload.get("replay_bytes_avoided") or 0))
                replay["receipt_count"] += 1
                replay["original_bytes"] += original
                replay["returned_bytes"] += returned
                replay["bytes_avoided"] += avoided
        if entry.get("violation"):
            result["policy_violations"] += 1
        authority_source = str(entry.get("authority_source") or "")
        authority_state = str(entry.get("authority_state") or "")
        authority_repo = str(entry.get("authority_repo") or "")
        semantic_edit_authority = (
            tool == "semantic_edit_prepare"
            and authority_source == "worker_workspace"
            and authority_state == "hash_bound_fragment"
        ) or (
            tool == "semantic_edit_apply"
            and authority_source == "worker_workspace"
            and authority_state == "deterministic_apply"
        )
        review_packet_authority = (
            tool == "quality_review_packet_read"
            and authority_source == "candidate_packet"
            and authority_state == "quality_review_readonly"
        )
        if (
            entry.get("ok")
            and not entry.get("violation")
            and (
                (
                    authority_source == "canonical"
                    and authority_state in {"canonical_active", "sole_authority"}
                )
                or semantic_edit_authority
                or review_packet_authority
            )
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
        if (
            tool == "semantic_edit_apply"
            and entry.get("ok")
            and not entry.get("violation")
            and semantic_edit_authority
            and isinstance(payload, dict)
            and len(result["semantic_edit_apply_receipts"]) < 128
        ):
            # Only deterministic byte counts leave the authenticated ledger.
            # Replacement text, paths, hashes and idempotency keys remain
            # private to the worker runtime.
            result["semantic_edit_apply_receipts"].append({
                "file_bytes": max(0, int(payload.get("file_bytes") or 0)),
                "range_count": max(0, int(payload.get("range_count") or 0)),
                "old_region_bytes": max(
                    0, int(payload.get("old_region_bytes") or 0)
                ),
                "replacement_bytes": max(
                    0, int(payload.get("replacement_bytes") or 0)
                ),
                "model_reemitted_old_bytes": max(
                    0, int(payload.get("model_reemitted_old_bytes") or 0)
                ),
                "token_savings_claimed": False,
            })
        authoritative_source_graph = (
            authority_source == "canonical"
            and authority_state in {"canonical_active", "sole_authority"}
        ) or (
            authority_source == "candidate_overlay"
            and authority_state == "quality_review_readonly"
        ) or (
            authority_source == "rework_overlay"
            and authority_state
            in {"request_scoped_predecessor", "request_scoped_worktree"}
        )
        # A fresh call is real tool-use telemetry even when its bounded query
        # returns zero rows.  Result usefulness is reported independently by
        # source_graph_hit_count/source_graph_zero_hit_calls; it must not be
        # confused with whether the authenticated invocation happened.
        if (
            tool == "source_graph"
            and entry.get("ok")
            and not entry.get("violation")
            and not entry.get("cache_hit")
            and provenance == "live"
            and authoritative_source_graph
        ):
            result["fresh_source_graph_calls"] += 1
        # "Live" means a genuinely fresh, successful authoritative invocation.
        # A zero-hit result is still a real live call; query usefulness remains
        # visible through the separate hit/zero-hit counters.  Cache replays,
        # denied calls, failures and non-authoritative sources never count.
        if (
            tool == "source_graph"
            and entry.get("ok")
            and not entry.get("violation")
            and not entry.get("cache_hit")
            and provenance == "live"
            and authoritative_source_graph
        ):
            result["live_source_graph_calls"] += 1
    result["call_count_by_tool"] = call_count
    result["successful_call_count_by_tool"] = successful_call_count
    result["bounded_bytes_by_tool"] = bounded_bytes_by_tool
    result["cache_hits_by_tool"] = cache_hits_by_tool
    # Repeated-query discipline is measured over the LIVE query sequence only:
    # a cache replay of an earlier live query is itself a "cache" row and never
    # appears here, so it cannot be double-charged as a repeated live query.
    live_query_sequence = result["live_source_graph_query_sequence"]
    result["live_source_graph_repeated_query_calls"] = max(
        0, len(live_query_sequence) - len(set(live_query_sequence))
    )
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
    if result["entries_invalid_identity"]:
        result["ok"] = False
        result["reason"] = (
            "authenticated_ledger_identity_violation:"
            + ",".join(invalid_identity_codes)
        )
    else:
        result["ok"] = True
    result["receipt_conformance"] = receipt_conformance_report(result)
    result["tool_discipline"] = tool_discipline_report(result)
    return result


def receipt_conformance_report(verification: Mapping[str, Any]) -> dict[str, Any]:
    """Check internally verifiable receipt invariants without trusting prose.

    This is deliberately structural.  It does not reinterpret a provider's
    answer or claim code quality; it checks that authenticated observations
    are arithmetically and causally coherent.
    """

    blockers: list[str] = []
    calls = verification.get("call_count_by_tool") or {}
    successful = verification.get("successful_call_count_by_tool") or {}
    for tool, count in successful.items():
        if int(count or 0) > int(calls.get(tool) or 0):
            blockers.append(f"successful_calls_exceed_calls:{tool}")

    replay = verification.get("compact_replay") or {}
    original = max(0, int(replay.get("original_bytes") or 0))
    returned = max(0, int(replay.get("returned_bytes") or 0))
    avoided = max(0, int(replay.get("bytes_avoided") or 0))
    if int(replay.get("receipt_count") or 0) and original - returned != avoided:
        blockers.append("compact_replay_arithmetic_mismatch")
    if replay.get("provider_token_savings_measured") is not False:
        blockers.append("compact_replay_token_claim_not_explicitly_unmeasured")

    mode_sequence = verification.get("source_graph_mode_sequence") or []
    stage_sequence = verification.get("source_graph_stage_sequence") or []
    if len(mode_sequence) != len(stage_sequence):
        blockers.append("source_graph_mode_stage_sequence_mismatch")
    fresh = max(0, int(verification.get("fresh_source_graph_calls") or 0))
    revisions = verification.get("source_graph_index_revision_counts") or {}
    if fresh and not revisions:
        blockers.append("fresh_source_graph_revision_missing")
    if int(verification.get("entries_tampered") or 0):
        blockers.append("tampered_receipts_observed")
    if int(verification.get("entries_invalid_identity") or 0):
        blockers.append("invalid_authenticated_identity_observed")

    checks = {
        "authenticated_entries": int(verification.get("entries_verified") or 0),
        "tampered_entries": int(verification.get("entries_tampered") or 0),
        "invalid_authenticated_identity_entries": int(
            verification.get("entries_invalid_identity") or 0
        ),
        "call_accounting_coherent": not any(
            item.startswith("successful_calls_exceed_calls:") for item in blockers
        ),
        "source_graph_stage_sequence_coherent": (
            "source_graph_mode_stage_sequence_mismatch" not in blockers
        ),
        "fresh_source_graph_revision_present": (
            "fresh_source_graph_revision_missing" not in blockers
        ),
        "compact_replay_arithmetic_coherent": (
            "compact_replay_arithmetic_mismatch" not in blockers
        ),
        "provider_token_claim_boundary_preserved": (
            "compact_replay_token_claim_not_explicitly_unmeasured" not in blockers
        ),
    }
    return {
        "schema_id": "aiworkhub.receipt_conformance.v1",
        "status": "pass" if not blockers else "fail",
        "blocking": bool(blockers),
        "blockers": blockers,
        "checks": checks,
    }


def tool_discipline_report(verification: Mapping[str, Any]) -> dict[str, Any]:
    """Describe observed live Source Graph use without inventing task intent.

    Scores are observational and normalized only over evidence that exists.
    They are suitable for telemetry and ranking calibration, never an
    independent completion gate.

    NF389/r6: every input here is the LIVE-scoped counter (provenance=="live"
    only). A launch-time prefetch or a cache replay is auditable in the total
    counters but must never move a worker's discipline score, so this report
    reads exclusively from ``live_source_graph_*`` -- never the backward-
    compatible totals.
    """

    calls = max(0, int(verification.get("live_source_graph_call_count") or 0))
    failed = max(0, int(verification.get("live_source_graph_failed_calls") or 0))
    zero_hits = max(0, int(verification.get("live_source_graph_zero_hit_calls") or 0))
    queries = [str(item) for item in verification.get("live_source_graph_query_sequence") or []]
    repeated = max(0, len(queries) - len(set(queries)))
    modes = [str(item) for item in verification.get("live_source_graph_mode_sequence") or []]
    trace_index = next((index for index, mode in enumerate(modes) if mode == "trace"), None)
    deps_after_trace = bool(
        trace_index is not None and any(mode == "deps" for mode in modes[trace_index + 1:])
    )
    checks_observed = 0
    penalty = 0.0
    if calls:
        checks_observed += 3
        penalty += min(1.0, failed / calls) * 40.0
        penalty += min(1.0, zero_hits / calls) * 25.0
        penalty += min(1.0, repeated / calls) * 25.0
        if trace_index is not None:
            checks_observed += 1
            penalty += 10.0 if deps_after_trace else 0.0
    score = None if checks_observed == 0 else round(max(0.0, 100.0 - penalty), 2)
    return {
        "schema_id": "aiworkhub.tool_discipline.v1",
        "status": "not_observed" if score is None else "observed",
        "observation_only": True,
        "score": score,
        "source_graph_calls": calls,
        "failed_calls": failed,
        "zero_hit_calls": zero_hits,
        "repeated_query_calls": repeated,
        "deps_after_trace": deps_after_trace,
        "query_identity_coverage": {
            "observed": len(queries),
            "expected": calls,
        },
    }


# ---------------------------------------------------------------------------
# Bounded worker-safe tool implementations (pure functions of an explicit ctx)
# ---------------------------------------------------------------------------

_MAX_SOURCE_GRAPH_CACHE_ENTRIES = 256

# Values a degraded index meta row can leave in an identity field.
# ``_source_graph_index_identity`` deliberately answers a missing/malformed
# ``meta`` row with an EMPTY ``finished_at`` rather than failing a supported
# query, so an empty or sentinel value is a truthful "generation unknown", not
# a generation: two genuinely different index states collapse onto the same
# value. Nothing is cached or replayed under one -- see
# ``_source_graph_generation_is_definite``.
_DEGRADED_INDEX_GENERATION_VALUES: frozenset[str] = frozenset({
    "", "-", "0", "n/a", "nan", "nat", "nil", "none", "null", "pending",
    "unknown", "unset",
})


class _SourceGraphCacheKey(NamedTuple):
    """The full identity one cached Source Graph result answers for.

    A ``NamedTuple`` rather than a bare positional tuple: it is still hashable
    and ordered (so it works as a dict key), but the two projections below read
    the slots they mean by NAME instead of re-deriving them from position.
    ``_source_graph_cache_key`` is the single production constructor -- every
    caller, including the tests, builds keys through it, so the layout can
    never be hand-copied out of sync.
    """

    kind: str
    task_id: str
    request_id: str
    repo: str
    mode: str
    query: str
    target: str | None
    cursor: str | None
    budget: int
    bundle_type: str
    packet_sha256: str
    overlay_sha256: str
    build_revision: str
    index_finished_at: str

    @property
    def authority(self) -> tuple[str, ...]:
        """The task/request/repo/packet/overlay identity this entry is bound to.

        Entries differing in ANY of these fields answer different authorities
        and are never interchangeable.
        """

        return (
            self.kind, self.task_id, self.request_id, self.repo,
            self.packet_sha256, self.overlay_sha256,
        )

    @property
    def generation(self) -> tuple[str, str]:
        """Which index generation produced the entry, as an ORDERED pair.

        ``finished_at`` leads so generations compare by index time, with
        ``build_revision`` only as a deterministic tie-break -- the same
        ordering ``_source_graph_index_identity`` already uses to select the
        newest meta row. This makes "newer generation" a total comparison
        rather than a function of arrival order.
        """

        return (self.index_finished_at, self.build_revision)


_CACHE: dict[_SourceGraphCacheKey, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()


def _source_graph_cache_key(
    *,
    task_id: Any,
    request_id: Any,
    repo: Any,
    mode: str,
    query: str,
    target: str | None,
    cursor: str | None,
    budget: int,
    bundle_type: str,
    packet_sha256: str,
    overlay_sha256: str,
    index_identity: Mapping[str, Any],
) -> _SourceGraphCacheKey:
    """Build the one production Source Graph cache key.

    ``source_graph_query`` calls exactly this helper, so the key layout has a
    single definition: authority binding (task/request/repo/packet/overlay),
    the query identity that was actually evaluated, and the index generation
    that produced it. Widening the key here widens it everywhere.
    """

    return _SourceGraphCacheKey(
        kind="source_graph",
        task_id=str(task_id),
        request_id=str(request_id),
        repo=str(repo),
        mode=str(mode),
        query=str(query),
        target=target,
        cursor=cursor,
        budget=int(budget),
        bundle_type=str(bundle_type),
        packet_sha256=str(packet_sha256),
        overlay_sha256=str(overlay_sha256),
        build_revision=str(index_identity.get("build_revision") or ""),
        index_finished_at=str(index_identity.get("finished_at") or ""),
    )


def _source_graph_generation_is_definite(index_identity: Mapping[str, Any]) -> bool:
    """Is this index identity a real, distinguishable generation?

    Fail-closed: an unreadable ``meta`` table, a missing row or a sentinel
    timestamp all yield the SAME identity for two different index states, so a
    result cached under one could be replayed after a mutation the key cannot
    see. Callers must then neither read nor write the cache and simply re-run
    the live query.
    """

    for value in (
        str(index_identity.get("build_revision") or "").strip(),
        str(index_identity.get("finished_at") or "").strip(),
    ):
        if value.lower() in _DEGRADED_INDEX_GENERATION_VALUES:
            return False
    return True


def _source_graph_cache_get(key: _SourceGraphCacheKey) -> dict[str, Any] | None:
    """Return the entry stored under EXACTLY this key, refreshing its recency.

    Reads are exact-key only. Authority binding and index generation are both
    part of the key, so a lookup made under a different task/request/repo/
    packet/overlay -- or after a generation rollover -- simply misses. No read
    can ever be answered with another authority's bytes or a stale
    generation's bytes.
    """

    with _CACHE_LOCK:
        entry = _CACHE.pop(key, None)
        if entry is None:
            return None
        _CACHE[key] = entry  # reinsert at the most-recently-used tail
        return entry


def _source_graph_cache_store(
    key: _SourceGraphCacheKey, entry: dict[str, Any],
) -> bool:
    """Record ``entry`` under ``key``; return whether it was recorded.

    Three deterministic rules, all applied under one lock so concurrent
    callers observe the same order:

      * generation fence -- a store whose generation is OLDER than the one
        currently resident for the SAME authority is a late write from an index
        state that has already been superseded. It is declined outright: it
        neither evicts nor supersedes the newer generation. Its caller keeps
        the live result it already computed; only caching it is refused.
      * rollover eviction -- a store whose generation is NEWER retires every
        resident entry of that SAME authority, so a rebuild or exact-file
        mutation cannot leave the previous generation resident. This eviction
        is AUTHORITY-SCOPED: it never touches another task/request/repo/packet/
        overlay's entries.
      * capacity -- the cache as a whole is then trimmed to
        ``_MAX_SOURCE_GRAPH_CACHE_ENTRIES``, least-recently-used first, so a
        long-lived worker process cannot grow it without limit. Unlike
        rollover, this GLOBAL bound may drop an entry belonging to a different
        authority. That is only a harmless cache miss -- the next call re-runs
        the live query and gets the same bytes -- never a cross-authority read,
        because every read is exact-key.
    """

    authority = key.authority
    generation = key.generation
    with _CACHE_LOCK:
        resident = [other for other in _CACHE if other.authority == authority]
        newest = max((other.generation for other in resident), default=None)
        if newest is not None and generation < newest:
            return False
        if newest is not None and generation > newest:
            for stale in resident:
                del _CACHE[stale]
        _CACHE.pop(key, None)
        _CACHE[key] = entry
        while len(_CACHE) > _MAX_SOURCE_GRAPH_CACHE_ENTRIES:
            _CACHE.pop(next(iter(_CACHE)))
        return True


def _source_graph_cache_clear() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


_SESSION_DELTA_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_MAX_SESSION_DELTA_ENTRIES = 256
_SESSION_DELTA_LOCK = threading.Lock()


def _remember_session_state(key: tuple[Any, ...], value: dict[str, Any]) -> None:
    with _SESSION_DELTA_LOCK:
        _SESSION_DELTA_CACHE[key] = value
        while len(_SESSION_DELTA_CACHE) > _MAX_SESSION_DELTA_ENTRIES:
            _SESSION_DELTA_CACHE.pop(next(iter(_SESSION_DELTA_CACHE)))


def _prior_session_state(key: tuple[Any, ...]) -> dict[str, Any] | None:
    with _SESSION_DELTA_LOCK:
        return _SESSION_DELTA_CACHE.get(key)


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

    normalized_scope = scope.replace("\\", "/").rstrip("/")

    def in_scope(candidate: str) -> bool:
        normalized_candidate = candidate.replace("\\", "/")
        candidate_cmp = normalized_candidate.casefold() if os.name == "nt" else normalized_candidate
        scope_cmp = normalized_scope.casefold() if os.name == "nt" else normalized_scope
        return candidate_cmp == scope_cmp or candidate_cmp.startswith(f"{scope_cmp}/")

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


def _selector_forms(selector: str) -> list[str]:
    """Exact engine-resolvable forms of a symbol selector.

    ``source_graph.body`` resolves a bare name OR a qualname; ``source_graph.func``
    resolves the bare name only.  Offering the ``<file>.<symbol>`` qualname and
    its bare tail lets one selector resolve through either resolver without the
    wrapper hard-coding which resolver matches which form.  A file-path target
    (whose tail is a bare extension like ``py``) names no symbol under either
    form, so it falls through to the path-scope branch untouched.
    """

    forms = [selector]
    tail = selector.rsplit(".", 1)[-1]
    if tail and tail != selector:
        forms.append(tail)
    return forms


def _resolve_symbol_selector(engine_fn, repo, selector: str, budget: int) -> dict[str, Any]:
    """Resolve an exact symbol selector, returning the first non-empty payload.

    Returns the last (honestly empty) payload when the selector names no indexed
    symbol; the caller then treats ``target`` as a path scope instead.
    """

    payload: dict[str, Any] = {}
    for candidate in _selector_forms(selector):
        payload = engine_fn(repo, candidate, budget)
        if _json_hit_count(payload) > 0:
            return payload
    return payload


def _selector_scoped_payload(
    engine_fn, repo, scope: str | None, bounded_query: str, budget: int,
) -> tuple[dict[str, Any], bool]:
    """Selector-first resolution for a symbol-selector mode.

    Returns ``(payload, selector_resolved)``.  When ``scope`` names an indexed
    symbol the resolved payload is returned and the path-prefix filter is
    skipped downstream; otherwise ``scope`` is a path scope, the free-text query
    is searched, and the existing prefix filter/rescue applies to the result.
    """

    if scope is None:
        return engine_fn(repo, bounded_query, budget), False
    resolved = _resolve_symbol_selector(engine_fn, repo, scope, budget)
    if _json_hit_count(resolved) > 0:
        return resolved, True
    return engine_fn(repo, bounded_query, budget), False


def _selector_match_in_scope(file_path: str, targets: Any) -> bool:
    """The ONE rule every selector-scope boundary obeys for a single match.

    A resolved selector match is in scope only when it carries an attributable
    ``file_path`` AND that file is covered by the coordinator's allowlist.  A
    match with no attributable path is OUT of scope -- refused by name like any
    other, never returned, because it could not be attributed.  Stated once here
    so the enforcement gate and the declared-target fallback drive their in/out
    decision from the same statement and cannot silently disagree on the
    empty-path case the way they did before (one fail-open, one fail-closed on
    the same boundary in the same file).

    The unrestricted "no target allowlist configured" grant is NOT decided here:
    an empty ``targets`` authorizes nothing at this per-match check (``targets``
    reaches ``path_is_allowed`` which returns ``False`` for a missing/empty
    allowlist).  The grant is applied once by the caller that needs it
    (``_enforce_selector_allowlist``), mirroring the ``target_not_allowed`` gate,
    so the per-match rule stays uniformly fail-closed.
    """

    if not file_path:
        return False
    return semantic_edit.path_is_allowed(file_path, targets)


def _enforce_selector_allowlist(
    ctx: WorkerToolContext, tool: str, payload: Any, targets: Any,
) -> tuple[dict[str, Any] | None, Any]:
    """Constrain a resolved symbol selector to the file allowlist.

    Returns ``(violation, payload)``.  When every resolved match lies outside
    the allowlist the symbol is refused BY NAME -- the caller learns the symbol
    exists and is out of scope, never an empty ``no such symbol`` result.  When
    only some matches are out of scope, those rows are dropped and the rest are
    returned.  The unrestricted "no target allowlist configured" grant is applied
    once here (nothing to enforce), mirroring the ``target_not_allowed`` gate; the
    per-match decision then defers wholly to ``_selector_match_in_scope`` so an
    unattributable match is refused by name rather than kept (the fail-open path
    this boundary used to take while its sibling fallback dropped it).
    """

    if not isinstance(payload, dict):
        return None, payload
    if not targets:
        return None, payload
    matches = payload.get("matches")
    if not isinstance(matches, list) or not matches:
        return None, payload
    in_scope: list[Any] = []
    out_of_scope: list[dict[str, Any]] = []
    for match in matches:
        file_path = (
            str(match.get("file_path") or match.get("file") or "")
            if isinstance(match, dict) else ""
        )
        if _selector_match_in_scope(file_path, targets):
            in_scope.append(match)
        else:
            out_of_scope.append(match)
    if not in_scope:
        first = out_of_scope[0]
        name = str(first.get("qualname") or first.get("name") or "")
        file_path = str(first.get("file_path") or first.get("file") or "")
        reason = f"symbol_out_of_scope:{name or file_path}@{file_path}"
        return _violation(ctx, tool, reason[:160]), payload
    if len(in_scope) == len(matches):
        return None, payload
    scoped = dict(payload)
    scoped["matches"] = in_scope
    scoped["candidate_files"] = sorted({
        str(match.get("file_path") or match.get("file") or "")
        for match in in_scope
        if isinstance(match, dict) and (match.get("file_path") or match.get("file"))
    })
    return None, scoped


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


_REWORK_OVERLAY_EXTRACT_OK = frozenset({"ok", "file_evidence_only"})


def _sealed_rework_overlay_bytes(entry: Mapping[str, Any], relative: str) -> bytes | None:
    if entry.get("deleted") is True:
        return None
    expected = str(entry.get("sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise WorkerToolError(f"rework_packet_file_hash_invalid:{relative}")
    encoded = entry.get("content_base64")
    if encoded is None:
        return None
    import base64
    try:
        content = base64.b64decode(str(encoded), validate=True)
    except (ValueError, TypeError) as exc:
        raise WorkerToolError(f"rework_packet_file_content_invalid:{relative}") from exc
    if hashlib.sha256(content).hexdigest() != expected:
        raise WorkerToolError(f"rework_packet_file_content_hash_mismatch:{relative}")
    return content


def _admit_rework_overlay_observation(
    *,
    relative: str,
    expected_hash: str,
    observed_hash: str,
    has_sealed: bool,
    allowed_writes: Sequence[str],
    canonical_hash: str | None = None,
) -> Literal["match", "authorized_overlay"]:
    """Admit a workspace digest against immutable packet-sealed authority."""

    if not expected_hash:
        return "match"
    if observed_hash == expected_hash:
        if (
            not has_sealed
            and relative in allowed_writes
            and observed_hash != canonical_hash
        ):
            return "authorized_overlay"
        return "match"
    if has_sealed and relative in allowed_writes:
        return "authorized_overlay"
    raise WorkerToolError(f"rework_overlay_hash_mismatch:{relative}")


def materialize_rework_overlay_sealed_files(
    repo: Path,
    packet: Mapping[str, Any],
    *,
    allowed_writes: Sequence[str] = (),
) -> list[str]:
    """Write sealed predecessor bytes; hash-only entries stay digest-bound."""

    repo_root = repo.resolve()
    written: list[str] = []
    files = packet.get("files")
    if not isinstance(files, list):
        raise WorkerToolError("rework_packet_files_invalid")
    for entry in files:
        if not isinstance(entry, dict):
            raise WorkerToolError("rework_packet_file_entry_invalid")
        relative = str(entry.get("path") or "")
        if entry.get("deleted") is True:
            continue
        content = _sealed_rework_overlay_bytes(entry, relative)
        if content is None:
            continue
        raw = repo / relative
        candidate = raw.resolve(strict=False)
        if not candidate.is_relative_to(repo_root):
            raise WorkerToolError(f"rework_overlay_path_escapes_workspace:{relative}")
        if raw.is_symlink():
            raise WorkerToolError(f"rework_overlay_path_symlink:{relative}")
        if candidate.exists() and not candidate.is_file():
            raise WorkerToolError(f"rework_overlay_path_not_file:{relative}")
        expected = str(entry["sha256"])
        if candidate.is_file():
            observed = hashlib.sha256(candidate.read_bytes()).hexdigest()
            _admit_rework_overlay_observation(
                relative=relative,
                expected_hash=expected,
                observed_hash=observed,
                has_sealed=True,
                allowed_writes=allowed_writes,
            )
            continue
        candidate.parent.mkdir(parents=True, exist_ok=True)
        tmp = candidate.parent / f".{candidate.name}.{secrets.token_hex(8)}.tmp"
        try:
            tmp.write_bytes(content)
            os.replace(tmp, candidate)
        finally:
            tmp.unlink(missing_ok=True)
        written.append(relative)
    return written


@dataclass(frozen=True, slots=True)
class _ReworkOverlayView:
    """Read-only, request-local Source Graph delta from sealed hashes/deltas."""

    changed: Mapping[str, Any]
    deleted: frozenset[str]
    snapshot_sha256: str
    sealed_sources: Mapping[str, str] = field(default_factory=dict)
    digest_refs: Mapping[str, str] = field(default_factory=dict)
    authorized_sources: Mapping[str, str] = field(default_factory=dict)
    authorized_digests: Mapping[str, str] = field(default_factory=dict)


def _prepare_rework_overlay_view(
    ctx: WorkerToolContext,
    canonical_db_path: Path,
    *,
    build_revision: str,
) -> _ReworkOverlayView | None:
    """Extract packet-bound files from sealed bytes or digest-bound hashes."""

    packet = ctx.rework_overlay_packet
    if packet is None:
        return None
    from . import source_graph_ast as _source_graph_ast

    entries = _build_rework_overlay_map(packet)
    if not entries:
        return _ReworkOverlayView({}, frozenset(), str(packet["canonical_digest"]))

    canonical_hashes: dict[str, str] = {}
    from . import source_graph as _source_graph

    conn = _source_graph.connect(canonical_db_path, read_only=True)
    try:
        paths = tuple(sorted(entries))
        for offset in range(0, len(paths), 400):
            batch = paths[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT file_path, source_hash FROM files WHERE file_path IN ({placeholders})",
                batch,
            ).fetchall()
            canonical_hashes.update({str(row[0]): str(row[1]) for row in rows})
    finally:
        conn.close()

    repo_root = ctx.repo.resolve()
    changed: dict[str, Any] = {}
    deleted: set[str] = set()
    sealed_sources: dict[str, str] = {}
    digest_refs: dict[str, str] = {}
    authorized_sources: dict[str, str] = {}
    authorized_digests: dict[str, str] = {}
    snapshot_rows: list[dict[str, str | bool]] = []
    for relative in sorted(entries):
        entry = entries[relative]
        raw_path = ctx.repo / relative
        resolved = raw_path.resolve(strict=False)
        if not resolved.is_relative_to(repo_root):
            raise WorkerToolError(f"rework_overlay_path_escapes_workspace:{relative}")
        if raw_path.is_symlink():
            raise WorkerToolError(f"rework_overlay_path_symlink:{relative}")
        if entry.get("deleted") is True:
            deleted.add(relative)
            snapshot_rows.append({"path": relative, "deleted": True})
            continue
        expected_hash = str(entry.get("sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise WorkerToolError(f"rework_packet_file_hash_invalid:{relative}")
        sealed = _sealed_rework_overlay_bytes(entry, relative)
        if sealed is not None:
            sealed_sources[relative] = sealed.decode("utf-8", errors="replace")
            if not resolved.exists():
                materialize_rework_overlay_sealed_files(
                    ctx.repo, {"files": [entry]}, allowed_writes=ctx.allowed_writes,
                )
                resolved = raw_path.resolve(strict=False)
        if not resolved.exists():
            if sealed is None:
                digest_refs[relative] = expected_hash
                snapshot_rows.append({"path": relative, "sha256": expected_hash})
                continue
            raise WorkerToolError(f"rework_overlay_file_missing:{relative}")
        if not resolved.is_file():
            raise WorkerToolError(f"rework_overlay_path_not_file:{relative}")
        observed_bytes = resolved.read_bytes()
        observed_hash = hashlib.sha256(observed_bytes).hexdigest()
        admission = _admit_rework_overlay_observation(
            relative=relative,
            expected_hash=expected_hash,
            observed_hash=observed_hash,
            has_sealed=sealed is not None,
            allowed_writes=ctx.allowed_writes,
            canonical_hash=canonical_hashes.get(relative),
        )
        if admission == "authorized_overlay":
            authorized_sources[relative] = observed_bytes.decode(
                "utf-8", errors="replace",
            )
            authorized_digests[relative] = observed_hash
            snapshot_rows.append({
                "path": relative,
                "sha256": expected_hash,
                "observed_sha256": observed_hash,
            })
        else:
            snapshot_rows.append({"path": relative, "sha256": observed_hash})
        if sealed is None and admission != "authorized_overlay":
            digest_refs[relative] = expected_hash
            continue
        if canonical_hashes.get(relative) == observed_hash:
            continue
        extraction = _source_graph_ast.extract_file(
            repo_root, resolved, build_revision=build_revision,
        )
        if extraction.status not in _REWORK_OVERLAY_EXTRACT_OK:
            raise WorkerToolError(
                f"rework_overlay_extract_failed:{relative}:{extraction.status}"
            )
        changed[relative] = extraction

    snapshot_sha256 = hashlib.sha256(json.dumps(
        {"packet": packet["canonical_digest"], "files": snapshot_rows},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return _ReworkOverlayView(
        changed, frozenset(deleted), snapshot_sha256,
        sealed_sources, digest_refs, authorized_sources, authorized_digests,
    )


def _drop_shadowed_source_graph_rows(value: Any, shadowed: frozenset[str]) -> Any:
    """Remove canonical rows and path lists owned by the request-local overlay."""

    if isinstance(value, list):
        rows = []
        for item in value:
            if isinstance(item, str) and item in shadowed:
                continue
            filtered = _drop_shadowed_source_graph_rows(item, shadowed)
            if filtered is not None:
                rows.append(filtered)
        return rows
    if not isinstance(value, dict):
        return value
    file_path = str(value.get("file_path") or value.get("file") or "")
    if file_path in shadowed:
        return None
    return {
        key: filtered
        for key, item in value.items()
        if (filtered := _drop_shadowed_source_graph_rows(item, shadowed)) is not None
    }


def _overlay_entity_match(extraction: Any, entity: Any, source: str) -> dict[str, Any]:
    return {
        "kind": entity.kind,
        "name": entity.name,
        "qualname": entity.qualname,
        "file_path": entity.file_path,
        "line_start": entity.line_start,
        "line_end": entity.line_end,
        "signature": entity.signature,
        "evidence_label": entity.evidence_label,
        "confidence": entity.confidence,
        "source_hash": extraction.source_hash,
        "build_revision": entity.build_revision,
        "freshness": {
            "state": "worktree_overlay",
            "indexed_source_hash": extraction.source_hash,
            "disk_source_hash": extraction.source_hash,
        },
        "source": source,
        "provenance": "request_scoped_worktree",
    }


def _merge_rework_overlay_payload(
    ctx: WorkerToolContext,
    payload: dict[str, Any],
    view: _ReworkOverlayView | None,
    *,
    mode: str,
    query: str,
    target: str | None,
    budget: int,
) -> tuple[dict[str, Any], bool]:
    """Compose a canonical query result with a small in-memory worktree delta."""

    if view is None or (
        not view.changed and not view.deleted and not view.digest_refs
        and not view.authorized_digests
    ):
        return payload, False
    shadowed = frozenset((
        *view.changed.keys(), *view.deleted, *view.digest_refs,
        *view.authorized_digests,
    ))
    merged = _drop_shadowed_source_graph_rows(payload, shadowed)
    if not isinstance(merged, dict):
        merged = {}

    query_lower = query.casefold()
    query_tokens = tuple(
        token for token in re.split(r"[^a-zA-Z0-9_]+", query_lower) if token
    )
    overlay_matches: list[dict[str, Any]] = []
    overlay_contexts: list[dict[str, Any]] = []
    for relative, extraction in sorted(view.changed.items()):
        text = view.authorized_sources.get(relative)
        if text is None:
            text = view.sealed_sources.get(relative)
        if text is None:
            raise WorkerToolError(f"rework_overlay_sealed_source_missing:{relative}")
        lines = text.splitlines()
        if mode == "file":
            requested = target or query
            if requested != relative:
                continue
            file_row = {
                "file_path": relative,
                "language": extraction.language,
                "status": extraction.status,
                "source_hash": extraction.source_hash,
                "build_revision": (
                    extraction.entities[0].build_revision
                    if extraction.entities else "worktree_overlay"
                ),
                "kind": "file",
                "name": Path(relative).name,
                "qualname": relative,
                "line_start": 1,
                "line_end": 1,
                "provenance": "request_scoped_worktree",
            }
            overlay_matches.append(file_row)
            overlay_contexts.append({
                "found": True,
                "file": file_row,
                "entities": [
                    _overlay_entity_match(extraction, entity, "")
                    for entity in extraction.entities[:max(1, min(16, budget // 2))]
                ],
                "edges": [],
                "source_preview": text[:4096],
                "source_preview_bytes": len(text[:4096].encode("utf-8")),
                "source_preview_truncated": len(text.encode("utf-8")) > 4096,
            })
            continue
        if mode == "bodygrep":
            if target is not None and target != relative:
                continue
            for line_number, line in enumerate(lines, 1):
                if query_lower in line.casefold():
                    overlay_matches.append({
                        "kind": "body_match",
                        "name": query,
                        "qualname": f"{relative}:{line_number}",
                        "file_path": relative,
                        "line_start": line_number,
                        "line_end": line_number,
                        "signature": line.strip(),
                        "source_hash": extraction.source_hash,
                        "provenance": "request_scoped_worktree",
                    })
            continue
        for entity in extraction.entities:
            haystack = " ".join(
                (entity.name, entity.qualname, entity.signature)
            ).casefold()
            exact = query_lower in {entity.name.casefold(), entity.qualname.casefold()}
            if mode == "function" and entity.kind not in {"function", "method"}:
                continue
            if mode == "class" and entity.kind not in {"class", "struct", "enum"}:
                continue
            if mode in {"body", "function", "class"} and not exact:
                continue
            if mode in {"focus", "symbols"} and not all(
                token in haystack for token in query_tokens
            ):
                continue
            if mode not in {"body", "function", "class", "focus", "symbols"}:
                continue
            start = max(0, int(entity.line_start) - 1)
            end = max(start, int(entity.line_end))
            overlay_matches.append(
                _overlay_entity_match(
                    extraction, entity, "\n".join(lines[start:end]),
                )
            )

    for relative, digest in sorted(view.digest_refs.items()):
        if mode == "file":
            requested = target or query
            if requested != relative:
                continue
        elif mode not in {"focus", "symbols"}:
            continue
        elif query_tokens and not all(
            token in relative.casefold() for token in query_tokens
        ):
            continue
        overlay_matches.append({
            "file_path": relative,
            "kind": "file",
            "name": Path(relative).name,
            "qualname": relative,
            "source_hash": digest,
            "status": "file_evidence_only",
            "line_start": 1,
            "line_end": 1,
            "provenance": "digest_bound_reference",
        })

    overlay_matches.sort(key=lambda row: (
        str(row.get("file_path") or ""),
        int(row.get("line_start") or 0),
        str(row.get("qualname") or row.get("name") or ""),
    ))
    existing = merged.get("matches")
    canonical_matches = existing if isinstance(existing, list) else []
    merged["matches"] = (overlay_matches + canonical_matches)[:budget]
    merged["candidate_files"] = sorted({
        str(row.get("file_path") or "")
        for row in merged["matches"]
        if isinstance(row, dict) and str(row.get("file_path") or "")
    })
    if overlay_contexts:
        canonical_contexts = merged.get("contexts")
        merged["contexts"] = overlay_contexts + (
            canonical_contexts if isinstance(canonical_contexts, list) else []
        )
    if mode == "focus" and overlay_matches:
        ranked = merged.get("ranked_symbols")
        merged["ranked_symbols"] = overlay_matches + (
            ranked if isinstance(ranked, list) else []
        )
    packet = ctx.rework_overlay_packet or {}
    merged["overlay"] = {
        "authority_source": "rework_overlay",
        "provenance": "request_scoped_worktree",
        "predecessor_task_id": str(packet.get("predecessor_task_id") or ""),
        "predecessor_request_id": str(packet.get("predecessor_request_id") or ""),
        "snapshot_sha256": view.snapshot_sha256,
        "changed_paths": sorted(view.changed),
        "deleted_paths": sorted(view.deleted),
        "digest_bound_paths": sorted(view.digest_refs),
        "authorized_overlay_paths": sorted(view.authorized_digests),
        "authorized_overlay_digests": dict(view.authorized_digests),
    }
    if mode in {"body", "function", "class"}:
        merged["freshness"] = "worktree_overlay" if overlay_matches else "no_match"
    return merged, True


def _apply_rework_overlay_query(
    ctx: WorkerToolContext,
    mode: str,
    query: str,
    target: str | None,
    budget: int,
    *,
    canonical_payload: dict[str, Any] | None = None,
    view: _ReworkOverlayView | None = None,
) -> Any:
    """Production overlay seam; the legacy direct probe remains test-compatible."""

    if canonical_payload is not None:
        return _merge_rework_overlay_payload(
            ctx, canonical_payload, view,
            mode=mode, query=query, target=target, budget=budget,
        )
    if not ctx.rework_overlay_packet or target is None:
        return None
    entry = _build_rework_overlay_map(ctx.rework_overlay_packet).get(target)
    if entry is None:
        return None
    if entry.get("deleted") is True:
        return {"ok": False, "reason": "file_deleted_by_rework_overlay"}
    encoded = entry.get("content_base64")
    if encoded is None:
        return {
            "ok": True, "overlay": True, "mode": mode, "query": query,
            "target": target, "source_hash": str(entry.get("sha256") or ""),
            "matches": [], "candidate_files": [],
        }
    import base64
    content = base64.b64decode(str(encoded), validate=True).decode(
        "utf-8", errors="replace",
    )
    return {
        "ok": True, "overlay": True, "mode": mode, "query": query,
        "target": target, "source_hash": str(entry.get("sha256") or ""),
        "source_preview": content[:8192],
        "source_preview_truncated": len(content) > 8192,
    }


# ---------------------------------------------------------------------------
# Signed outer-pagination continuation (NF-2026-00510)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _ContinuationEntry:
    canonical_bytes: bytes
    created_at: float
    content_sha256: str
    bind: tuple[tuple[str, str], ...]
    meta: dict[str, Any]
    chunk_size: int
    page_count: int


_CONTINUATION_STORE: dict[str, _ContinuationEntry] = {}
_CONTINUATION_RETAINED_BYTES = 0
_CONTINUATION_LOCK = threading.Lock()
# Process-local signing secret.  HMAC-authenticates cursors without depending
# on the per-request audit key (which manager contexts do not carry).  It never
# leaves the process, so a cursor is unforgeable but not portable across
# processes -- exactly the bounded, process-local lifetime this feature needs.
_CONTINUATION_HMAC_KEY: bytes = secrets.token_bytes(32)


def _continuation_bind(
    ctx: WorkerToolContext,
    *,
    mode: str,
    query: str,
    target: str | None,
    budget: int,
    bundle_type: str,
    workflow_stage: str,
    content_sha256: str,
    index_identity: Mapping[str, str],
    authority_source: str,
    authority_state: str,
    authority_repo: str,
    packet_sha256: str,
    target_request_id: str,
    target_task_id: str,
) -> tuple[tuple[str, str], ...]:
    """The exact immutable authority a continuation cursor is bound to."""
    return (
        ("task_id", str(ctx.task_id)),
        ("request_id", str(ctx.request_id)),
        ("repo", str(authority_repo)),
        ("mode", mode),
        ("query", query),
        ("target", target or ""),
        ("budget", str(budget)),
        ("bundle_type", bundle_type),
        ("workflow_stage", workflow_stage),
        ("authority_source", authority_source),
        ("authority_state", authority_state),
        ("index_revision", str(index_identity["build_revision"])),
        ("index_finished_at", str(index_identity["finished_at"])),
        ("packet_sha256", packet_sha256),
        ("target_request_id", target_request_id),
        ("target_task_id", target_task_id),
        ("content_sha256", content_sha256),
    )


def _continuation_sign(body: str) -> str:
    return hmac.new(
        _CONTINUATION_HMAC_KEY, body.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _encode_continuation_cursor(fields: dict[str, Any]) -> str:
    body = json.dumps(
        fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    token = {**fields, "hmac_sha256": _continuation_sign(body)}
    return base64.urlsafe_b64encode(
        json.dumps(
            token, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).decode("ascii")


def _decode_continuation_cursor(cursor: str) -> dict[str, Any] | None:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    presented = decoded.get("hmac_sha256")
    if not isinstance(presented, str) or len(presented) != 64:
        return None
    fields = {key: value for key, value in decoded.items() if key != "hmac_sha256"}
    body = json.dumps(
        fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if not hmac.compare_digest(_continuation_sign(body), presented):
        return None
    return fields


def _continuation_evict_for(needed: int) -> None:
    """Evict oldest entries until count and retained-byte bounds both hold."""
    global _CONTINUATION_RETAINED_BYTES
    while _CONTINUATION_STORE and (
        len(_CONTINUATION_STORE) >= SOURCE_GRAPH_CONTINUATION_MAX_ENTRIES
        or _CONTINUATION_RETAINED_BYTES + needed > SOURCE_GRAPH_CONTINUATION_MAX_BYTES
    ):
        oldest_id = min(
            _CONTINUATION_STORE,
            key=lambda store_id: _CONTINUATION_STORE[store_id].created_at,
        )
        entry = _CONTINUATION_STORE.pop(oldest_id)
        _CONTINUATION_RETAINED_BYTES -= len(entry.canonical_bytes)


def _continuation_put(
    *,
    canonical_bytes: bytes,
    content_sha256: str,
    bind: tuple[tuple[str, str], ...],
    meta: dict[str, Any],
    store_id: str | None = None,
    chunk_size: int | None = None,
    page_count: int | None = None,
) -> str | None:
    """Store one pageable response and return its bounded store identifier."""
    global _CONTINUATION_RETAINED_BYTES
    if len(canonical_bytes) > SOURCE_GRAPH_CONTINUATION_MAX_BYTES:
        return None
    if chunk_size is None:
        chunk_size = max(1, len(canonical_bytes))
    if page_count is None:
        page_count = (len(canonical_bytes) + chunk_size - 1) // chunk_size
    if chunk_size <= 0 or page_count <= 0:
        return None
    with _CONTINUATION_LOCK:
        _continuation_evict_for(len(canonical_bytes))
        if store_id is None:
            while True:
                store_id = secrets.token_hex(16)
                if store_id not in _CONTINUATION_STORE:
                    break
        elif store_id in _CONTINUATION_STORE:
            return None
        _CONTINUATION_STORE[store_id] = _ContinuationEntry(
            canonical_bytes=canonical_bytes,
            created_at=time.time(),
            content_sha256=content_sha256,
            bind=bind,
            meta=meta,
            chunk_size=chunk_size,
            page_count=page_count,
        )
        _CONTINUATION_RETAINED_BYTES += len(canonical_bytes)
    return store_id


def _continuation_fetch(store_id: str) -> _ContinuationEntry | None:
    """Return a live, unexpired entry or evict it and fail closed."""
    global _CONTINUATION_RETAINED_BYTES
    with _CONTINUATION_LOCK:
        entry = _CONTINUATION_STORE.get(store_id)
        if entry is None:
            return None
        if time.time() - entry.created_at > SOURCE_GRAPH_CONTINUATION_TTL_SECONDS:
            _CONTINUATION_STORE.pop(store_id, None)
            _CONTINUATION_RETAINED_BYTES -= len(entry.canonical_bytes)
            return None
    return entry


def _continuation_remove(store_id: str) -> None:
    """Drop a fully-consumed entry and reclaim its exact retained bytes."""
    global _CONTINUATION_RETAINED_BYTES
    with _CONTINUATION_LOCK:
        entry = _CONTINUATION_STORE.pop(store_id, None)
        if entry is not None:
            _CONTINUATION_RETAINED_BYTES -= len(entry.canonical_bytes)


def _continuation_clear() -> None:
    """Test seam: drop every entry and reset retained-byte accounting."""
    global _CONTINUATION_RETAINED_BYTES
    with _CONTINUATION_LOCK:
        _CONTINUATION_STORE.clear()
        _CONTINUATION_RETAINED_BYTES = 0


def _payload_internal_truncation(payload: Any) -> bool:
    """Report only engine/payload loss, never wrapper response size."""
    if not isinstance(payload, dict):
        return False
    if bool(payload.get("truncated")) or bool(payload.get("scan_truncated")):
        return True
    return payload.get("next_cursor") not in (None, "")


def _canonical_json_bytes(name: str, text: str) -> bytes:
    """Return the exact ordered canonical JSON bytes for one tool payload."""
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
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _continuation_page_result(
    *,
    store_id: str,
    page_index: int,
    meta: dict[str, Any],
    canonical_bytes: bytes,
    chunk_size: int,
    page_count: int,
    content_sha256: str,
) -> tuple[dict[str, Any], int]:
    """Render one page (base64 chunk) and mint the next cursor when bytes remain."""
    start = page_index * chunk_size
    chunk = canonical_bytes[start:start + chunk_size]
    has_more = start + chunk_size < len(canonical_bytes)
    if has_more:
        next_cursor = _encode_continuation_cursor({
            "schema_id": CONTINUATION_SCHEMA_ID,
            "store_id": store_id,
            "page_index": page_index + 1,
            "content_sha256": content_sha256,
        })
    else:
        next_cursor = None
    result = {
        **meta,
        "truncated": bool(meta.get("internal_truncated")) or has_more,
        "outer_truncated": has_more,
        "bytes": len(chunk),
        "content": base64.b64encode(chunk).decode("ascii"),
        "content_encoding": "base64",
        "content_sha256": content_sha256,
        "page_sha256": hashlib.sha256(chunk).hexdigest(),
        "page_index": page_index,
        "page_count": page_count,
        "full_bytes": len(canonical_bytes),
        "continuation_cursor": next_cursor,
    }
    return result, len(chunk)


def _serialized_response_bytes(result: Mapping[str, Any]) -> int:
    return len(
        json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )


def _continuation_chunk_size_for_cap(
    *,
    store_id: str,
    meta: dict[str, Any],
    canonical_bytes: bytes,
    content_sha256: str,
    output_cap_bytes: int,
) -> int:
    """Return one fixed raw chunk size whose every page serializes under cap."""

    cap = int(output_cap_bytes)
    if cap <= 0 or not canonical_bytes:
        raise WorkerToolError("continuation_output_cap_too_small")

    def fits(chunk_size: int) -> bool:
        page_count = (len(canonical_bytes) + chunk_size - 1) // chunk_size
        for index in range(page_count):
            page, _bytes_returned = _continuation_page_result(
                store_id=store_id,
                page_index=index,
                meta=meta,
                canonical_bytes=canonical_bytes,
                chunk_size=chunk_size,
                page_count=page_count,
                content_sha256=content_sha256,
            )
            if _serialized_response_bytes(page) > cap:
                return False
        return True

    low, high = 1, len(canonical_bytes)
    best = 0
    while low <= high:
        mid = (low + high) // 2
        if fits(mid):
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    if best <= 0:
        raise WorkerToolError("continuation_output_cap_too_small")
    return best


def _serve_continuation(
    ctx: WorkerToolContext,
    *,
    tool: str,
    cursor: str,
    started: float,
    query_sha256: str,
    mode: str,
    query: str,
    target: str | None,
    budget: int,
    bundle_type: str,
    workflow_stage: str,
) -> dict[str, Any]:
    """Serve one continuation page from the bounded process-local store."""
    fields = _decode_continuation_cursor(cursor)
    if fields is None or fields.get("schema_id") != CONTINUATION_SCHEMA_ID:
        return _violation(ctx, tool, "invalid_continuation_cursor")
    store_id = fields.get("store_id")
    page_index = fields.get("page_index")
    content_sha256 = fields.get("content_sha256")
    if not isinstance(store_id, str) or not store_id:
        return _violation(ctx, tool, "invalid_continuation_cursor")
    if (
        not isinstance(page_index, int)
        or isinstance(page_index, bool)
        or page_index < 1
    ):
        return _violation(ctx, tool, "invalid_continuation_cursor")
    if not isinstance(content_sha256, str) or len(content_sha256) != 64:
        return _violation(ctx, tool, "invalid_continuation_cursor")

    from . import source_graph as _source_graph_mod
    try:
        overlay_view = None
        overlay_target_request_id = ""
        overlay_target_task_id = ""
        overlay_packet = ctx.rework_overlay_packet
        if overlay_packet is None:
            binding = _resolve_source_graph_db(ctx)
        else:
            overlay_target_request_id = str(
                overlay_packet.get("predecessor_request_id") or ""
            )
            overlay_target_task_id = str(
                overlay_packet.get("predecessor_task_id") or ""
            )
            candidate_binding = _candidate_source_graph_binding(ctx)
            binding = candidate_binding or _canonical_source_graph_binding(ctx)
            if candidate_binding is None:
                overlay_view = _prepare_rework_overlay_view(
                    ctx,
                    binding.db_path,
                    build_revision=_source_graph_mod.BUILD_REVISION,
                )
    except (WorkerToolError, OSError, sqlite3.Error) as exc:
        return _violation(ctx, tool, str(exc)[:160])
    query_repo = binding.authority_repo or ctx.authority_repo
    index_identity = _source_graph_index_identity(
        binding.db_path, default_revision=_source_graph_mod.BUILD_REVISION,
    )
    if overlay_view is not None and (
        overlay_view.changed or overlay_view.deleted or overlay_view.digest_refs
        or overlay_view.authorized_digests
    ):
        authority_source = "rework_overlay"
        authority_state = "request_scoped_worktree"
        packet_sha256 = overlay_view.snapshot_sha256
        target_request_id = overlay_target_request_id
        target_task_id = overlay_target_task_id
    else:
        authority_source = binding.authority_source
        authority_state = binding.authority_state
        packet_sha256 = binding.packet_sha256
        target_request_id = binding.target_request_id
        target_task_id = binding.target_task_id

    entry = _continuation_fetch(store_id)
    if entry is None:
        return _violation(ctx, tool, "continuation_unavailable")
    if not hmac.compare_digest(entry.content_sha256, content_sha256):
        return _violation(ctx, tool, "invalid_continuation_cursor")

    meta = entry.meta
    expected_bind = _continuation_bind(
        ctx,
        mode=mode,
        query=query,
        target=target,
        budget=budget,
        bundle_type=bundle_type,
        workflow_stage=workflow_stage,
        content_sha256=entry.content_sha256,
        index_identity=index_identity,
        authority_source=authority_source,
        authority_state=authority_state,
        authority_repo=str(query_repo),
        packet_sha256=packet_sha256,
        target_request_id=target_request_id,
        target_task_id=target_task_id,
    )
    if expected_bind != entry.bind:
        return _violation(ctx, tool, "continuation_authority_mismatch")

    if page_index >= entry.page_count:
        return _violation(ctx, tool, "continuation_page_out_of_range")
    result, bytes_returned = _continuation_page_result(
        store_id=store_id,
        page_index=page_index,
        meta=meta,
        canonical_bytes=entry.canonical_bytes,
        chunk_size=entry.chunk_size,
        page_count=entry.page_count,
        content_sha256=entry.content_sha256,
    )
    if result["continuation_cursor"] is None:
        _continuation_remove(store_id)
    _append_audit(
        ctx, tool=tool, ok=True, cache_hit=False,
        hit_count=int(meta.get("hit_count") or 0),
        bytes_returned=bytes_returned,
        authority_source=str(meta.get("authority_source") or ""),
        authority_state=str(meta.get("authority_state") or ""),
        authority_repo=query_repo,
        provenance="continuation",
        payload={
            "mode": meta.get("mode"),
            "query_sha256": query_sha256,
            "workflow_stage": meta.get("workflow_stage"),
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "index_revision": index_identity["build_revision"],
            "index_finished_at": index_identity["finished_at"],
            "evidence_counts": meta.get("evidence_counts"),
            "output_cap_bytes": meta.get("output_cap_bytes"),
            "target_request_id": meta.get("target_request_id"),
            "target_task_id": meta.get("target_task_id"),
            "packet_sha256": meta.get("packet_sha256"),
            "internal_truncated": bool(meta.get("internal_truncated")),
            "outer_truncated": result["outer_truncated"],
            "page_index": page_index,
        },
    )
    return result


def source_graph_query(
    ctx: WorkerToolContext,
    *,
    mode: SourceGraphMode,
    query: str,
    budget: int = 64,
    target: str | None = None,
    cursor: str | None = None,
    continuation_cursor: str | None = None,
    bundle_type: SourceGraphBundleType = "explore",
    workflow_stage: WorkflowStage = "unspecified",
    compact_replay: bool = True,
) -> dict[str, Any]:
    """Bounded Source Graph discovery and repository analytics.

    ``target`` means one of two things, decided once by
    ``SOURCE_GRAPH_SELECTOR_MODES`` so the engine call and the response filter
    can never disagree. For a SELECTOR mode (``slice``/``body``/``function``)
    ``target`` is an exact symbol selector (a bare name or a ``<file>.<symbol>``
    qualname, e.g. the qualnames ``focus.recommended_next_steps`` emits): it is
    resolved by the engine in place of the free-text ``query`` and its result is
    never re-filtered by a path prefix. A resolved ``body``/``function`` symbol
    still honours the file allowlist -- one whose file is out of scope is refused
    BY NAME, not returned empty. When a ``body``/``function`` target names no
    indexed symbol it is treated as a PATH SCOPE instead (below). For every other
    non-analytic mode ``query`` is ALWAYS the semantic search term (B834 repair:
    the B833 candidate discarded ``query`` whenever any target was declared) and
    ``target`` constrains the RETURNED scope: matching file-path-bearing entries
    are kept, everything else is dropped. In ``file`` mode an indexed exact
    target is also the requested file authority; directory targets fall back to
    the query path. Omitting ``target`` returns the unscoped query result.

    For analytic modes (``sganalytics.ANALYTIC_MODES``), ``target`` and
    ``cursor`` are instead applied INSIDE the engine (``analytics_query``):
    the engine is the sole authority for which rows are in scope and which
    page is returned, so this wrapper never re-filters or re-paginates an
    analytic payload on top of what the engine already decided. ``cursor``
    is rejected for every non-analytic mode rather than silently ignored.

    ``continuation_cursor`` is the signed outer-pagination cursor minted by a
    previous page of THIS call: it reassembles the exact canonical response
    bytes when the response exceeded the mode's outer output cap, and is
    orthogonal to the engine-level analytic ``cursor`` above.
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
    query_sha256 = hashlib.sha256(bounded_query.encode("utf-8")).hexdigest()
    try:
        budget = max(MIN_BUDGET, min(int(budget), MAX_BUDGET))
    except (TypeError, ValueError):
        return _violation(ctx, tool, "invalid_budget")

    is_analytic_mode = mode in SOURCE_GRAPH_ANALYTIC_MODES
    # Set by the symbol-selector engine dispatch below when ``target`` resolved
    # to an indexed symbol; drives the selector-vs-path decision in the filter.
    selector_resolved = False
    if cursor is not None:
        if not is_analytic_mode:
            return _violation(ctx, tool, "cursor_not_supported_for_mode")
        bounded_cursor = _bounded_query(cursor, max_bytes=64)
        if bounded_cursor is None:
            return _violation(ctx, tool, "invalid_cursor")
        cursor = bounded_cursor

    scope: str | None = None
    if target is not None:
        bounded_target = _bounded_query(target, max_bytes=256)
        if bounded_target is None:
            return _violation(ctx, tool, "invalid_target")
        if ctx.source_graph_targets and bounded_target not in ctx.source_graph_targets:
            return _violation(ctx, tool, "target_not_allowed")
        scope = bounded_target

    if continuation_cursor is not None:
        bounded_continuation = _bounded_query(continuation_cursor, max_bytes=1024)
        if bounded_continuation is None:
            return _violation(ctx, tool, "invalid_continuation_cursor")
        return _serve_continuation(
            ctx, tool=tool, cursor=bounded_continuation,
            started=started, query_sha256=query_sha256,
            mode=mode, query=bounded_query, target=scope, budget=budget,
            bundle_type=bundle_type, workflow_stage=workflow_stage,
        )

    from . import source_graph as _source_graph_mod
    try:
        if ctx.rework_overlay_packet is None:
            binding = _resolve_source_graph_db(ctx)
            overlay_view = None
        else:
            candidate_binding = _candidate_source_graph_binding(ctx)
            binding = candidate_binding or _canonical_source_graph_binding(ctx)
            overlay_view = (
                _prepare_rework_overlay_view(
                    ctx,
                    binding.db_path,
                    build_revision=_source_graph_mod.BUILD_REVISION,
                )
                if candidate_binding is None
                else None
            )
    except (WorkerToolError, OSError, sqlite3.Error) as exc:
        return _violation(ctx, tool, str(exc)[:160])

    query_repo = binding.authority_repo or ctx.authority_repo
    index_identity = _source_graph_index_identity(
        binding.db_path, default_revision=_source_graph_mod.BUILD_REVISION,
    )
    cache_key = _source_graph_cache_key(
        task_id=ctx.task_id,
        request_id=ctx.request_id,
        repo=query_repo,
        mode=mode,
        query=bounded_query,
        target=scope,
        cursor=cursor,
        budget=budget,
        bundle_type=bundle_type,
        packet_sha256=binding.packet_sha256,
        overlay_sha256=(
            overlay_view.snapshot_sha256 if overlay_view is not None else ""
        ),
        index_identity=index_identity,
    )
    # A degraded index identity is not a generation: an unreadable ``meta``
    # table or an empty/sentinel ``finished_at`` makes two different index
    # states collapse onto the same key, so a result cached under one could be
    # replayed after a mutation the key cannot see. Neither read nor write the
    # cache in that state -- fall back to the live query every time.
    generation_is_definite = _source_graph_generation_is_definite(index_identity)
    cached = _source_graph_cache_get(cache_key) if generation_is_definite else None
    if cached is not None:
        cached_result = cached["result"]
        receipt_content = json.dumps(
            {
                "schema_id": "aiworkhub.source_graph.cache_receipt.v1",
                "reuse_previous_result": True,
                "content_sha256": cached["content_sha256"],
                "original_content_bytes": cached["bytes"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        receipt_bytes = len(receipt_content.encode("utf-8"))
        use_receipt = compact_replay and receipt_bytes < int(cached["bytes"])
        returned_content = receipt_content if use_receipt else str(cached_result["content"])
        returned_bytes = receipt_bytes if use_receipt else int(cached["bytes"])
        replay_bytes_avoided = max(0, int(cached["bytes"]) - returned_bytes)
        _append_audit(
            ctx, tool=tool, ok=True, cache_hit=True,
            hit_count=cached["hit_count"], bytes_returned=returned_bytes,
            authority_source=cached["authority_source"], authority_state=cached["authority_state"],
            authority_repo=query_repo,
            provenance="cache",
            payload={
                "mode": mode,
                "query_sha256": query_sha256,
                "workflow_stage": workflow_stage,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "index_revision": index_identity["build_revision"],
                "index_finished_at": index_identity["finished_at"],
                "evidence_counts": cached["evidence_counts"],
                "output_cap_bytes": cached["result"].get("output_cap_bytes"),
                "compact_replay": use_receipt,
                "replay_original_bytes": int(cached["bytes"]),
                "replay_returned_bytes": returned_bytes,
                "replay_bytes_avoided": replay_bytes_avoided,
                "provider_tokens_saved": None,
                "provider_token_savings_measured": False,
            },
        )
        return {
            **cached_result,
            "workflow_stage": workflow_stage,
            "content": returned_content,
            "bytes": returned_bytes,
            "cache_hit": True,
            "cache_receipt": use_receipt,
            "content_sha256": cached["content_sha256"],
            "replay_original_bytes": int(cached["bytes"]),
            "replay_bytes_avoided": replay_bytes_avoided,
            "provider_tokens_saved": None,
            "provider_token_savings_measured": False,
        }

    try:
        with _with_source_graph_db(_source_graph_mod, binding.db_path):
            if mode == "bundle":
                payload = _source_graph_mod.bundle(query_repo, bundle_type, bounded_query, budget)
            elif mode == "slice":
                payload = _source_graph_mod.slice_(
                    query_repo, bounded_query, budget, target=scope,
                )
            elif mode == "context":
                payload = _source_graph_mod.context_query(query_repo, bounded_query, budget)
            elif mode == "file":
                # ``file`` is the one mode whose target can itself be the
                # exact authority being requested.  Prefer that scoped path
                # when it is indexed, while retaining the semantic-query
                # path as a fallback for directory-scoped calls.
                file_query_path = scope or bounded_query
                payload = _source_graph_mod.file_query(query_repo, file_query_path, budget)
                if (
                    scope is not None
                    and file_query_path != bounded_query
                    and _json_hit_count(payload) == 0
                ):
                    payload = _source_graph_mod.file_query(query_repo, bounded_query, budget)
            elif mode == "function":
                payload, selector_resolved = _selector_scoped_payload(
                    _source_graph_mod.function_query, query_repo, scope, bounded_query, budget,
                )
            elif mode == "class":
                payload = _source_graph_mod.class_query(query_repo, bounded_query, budget)
            elif mode == "body":
                payload, selector_resolved = _selector_scoped_payload(
                    _source_graph_mod.body_query, query_repo, scope, bounded_query, budget,
                )
            elif mode == "bodygrep":
                payload = _source_graph_mod.bodygrep_query(
                    query_repo, bounded_query, budget, target=scope,
                )
            elif mode == "impact":
                payload = _source_graph_mod.impact(query_repo, bounded_query, budget)
            elif mode == "trace":
                payload = _source_graph_mod.trace(query_repo, bounded_query, budget)
            elif mode == "deps":
                payload = _source_graph_mod.deps_query(query_repo, bounded_query, budget)
            elif mode == "focus":
                payload = _source_graph_mod.focus(query_repo, bounded_query, budget)
            else:
                payload = _source_graph_mod.analytics_query(
                    query_repo, mode, bounded_query, budget,
                    target=scope, cursor=cursor,
                )
    except _source_graph_mod.SourceGraphError as exc:
        return _violation(ctx, tool, str(exc)[:160])
    payload, overlay_applied = _apply_rework_overlay_query(
        ctx,
        mode,
        bounded_query,
        scope,
        budget,
        canonical_payload=payload,
        view=overlay_view,
    )
    if (
        mode == "file"
        and scope is not None
        and _json_hit_count(payload) == 0
    ):
        exact_payload = _declared_input_file_payload(ctx, scope)
        if exact_payload is not None:
            payload = exact_payload
    selector_scoped = scope is not None and (
        mode == "slice"
        or (mode in SOURCE_GRAPH_SYMBOL_SELECTOR_MODES and selector_resolved)
    )
    if selector_scoped:
        # A selector-mode ``target`` is an exact symbol selector (often a
        # qualname emitted by focus.recommended_next_steps), not a file-prefix
        # response filter -- a file path never starts with a qualname, so the
        # prefix filter would drop every resolved symbol.  ``slice`` returns its
        # resolved dependency neighborhood verbatim (already validated against
        # the immutable task allowlist above).  A resolved body/function symbol
        # must still honour the file allowlist: one whose file is out of scope is
        # refused BY NAME so the caller learns it exists and is out of scope,
        # never an empty result that reads as "no such symbol".
        if mode in SOURCE_GRAPH_SYMBOL_SELECTOR_MODES:
            refusal, payload = _enforce_selector_allowlist(
                ctx, tool, payload, ctx.source_graph_targets,
            )
            if refusal is not None:
                return refusal
        if isinstance(payload, dict):
            payload.setdefault("scope", "target_selector")
    elif scope is not None and is_analytic_mode:
        # ``analytics_query`` already applied ``target``/``cursor`` as the
        # sole in-engine authority (scoping, pagination, coverage truth).
        # Re-running the generic path-prefix filter here would be a second,
        # independent filter pass that can silently diverge from what the
        # engine already decided -- e.g. dropping rows the engine already
        # excluded for a different reason, or keeping stale unscoped rows
        # the engine never should have emitted. Trust the engine's payload
        # verbatim instead of re-deriving scope in the wrapper.
        pass
    elif scope is not None:
        unscoped_payload = payload
        payload = _filter_by_scope(payload, scope) or {}
        # ``_filter_by_scope`` deliberately treats bare string-list entries
        # as file paths because Source Graph emits several path-only lists.
        # Query tokens are also a string list, but they are retrieval
        # provenance rather than repository paths.  Restore that bounded
        # metadata after the security filter so a scoped zero-hit response
        # never erases the query that was actually evaluated (NF-71).
        if isinstance(payload, dict) and isinstance(unscoped_payload, dict):
            query_tokens = unscoped_payload.get("query_tokens")
            if not isinstance(query_tokens, list):
                query_tokens = _source_graph_mod._query_tokens(bounded_query)
            if isinstance(query_tokens, list):
                payload["query_tokens"] = list(query_tokens)
                payload["query_tokens_source"] = str(
                    unscoped_payload.get("query_tokens_source") or "query"
                )
        # An exact body symbol can legitimately live in a second coordinator-
        # declared target even when the model carried forward a narrower
        # orientation target.  Preserve the security boundary: broaden only
        # to an exact result whose file is still covered by the immutable
        # task-scoped target allowlist, never to arbitrary repository files.
        if mode == "body" and _json_hit_count(payload) == 0:
            matches = (
                unscoped_payload.get("matches")
                if isinstance(unscoped_payload, dict)
                else None
            )
            permitted_matches = []
            for match in matches if isinstance(matches, list) else []:
                if not isinstance(match, dict):
                    continue
                file_path = str(match.get("file_path") or match.get("file") or "")
                # Same statement as the enforcement gate: an unattributable match
                # is out of scope, never broadened in.  Both boundaries read
                # ``_selector_match_in_scope`` so they cannot diverge again.
                if _selector_match_in_scope(file_path, ctx.source_graph_targets):
                    permitted_matches.append(match)
            if permitted_matches:
                bounded_matches = permitted_matches[:budget]
                payload = {
                    "mode": "body",
                    "query": bounded_query,
                    "budget": budget,
                    "matches": bounded_matches,
                    "candidate_files": sorted({
                        str(match.get("file_path") or match.get("file") or "")
                        for match in bounded_matches
                        if str(match.get("file_path") or match.get("file") or "")
                    }),
                    "truncated": bool(unscoped_payload.get("truncated")),
                    "scope": "declared_target_fallback",
                    "requested_target": scope,
                }
        if isinstance(payload, dict) and payload:
            payload.setdefault("scope", "target")
            if mode == "focus" and _json_hit_count(payload) == 0:
                payload["retrieval_reason"] = (
                    "no_ranked_match_within_target"
                    if _json_hit_count(unscoped_payload) > 0
                    else "no_ranked_semantic_match"
                )
                payload["requested_target"] = scope
    hit_payload = (
        {key: value for key, value in payload.items() if key != "overlay"}
        if isinstance(payload, dict)
        else payload
    )
    if overlay_applied and mode in {
        "focus", "file", "function", "class", "body", "bodygrep", "symbols",
    }:
        matches = payload.get("matches") if isinstance(payload, dict) else None
        hit_count = len(matches) if isinstance(matches, list) else 0
    else:
        hit_count = _json_hit_count(hit_payload)
    evidence_counts = _source_graph_evidence_counts(payload)
    raw_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    output_cap_bytes = _source_graph_output_cap(mode)
    try:
        canonical_bytes = _canonical_json_bytes(tool, raw_text)
    except WorkerToolError as exc:
        return _violation(ctx, tool, str(exc)[:160])
    # Engine/payload loss is the ONLY internal truncation: the engine itself
    # truncated, or signalled a continuation cursor.  Response size alone is
    # never classified as internal -- that is outer pagination (below).
    internal_truncated = _payload_internal_truncation(payload)
    full_content_sha256 = hashlib.sha256(canonical_bytes).hexdigest()

    # Static response metadata shared by every page (initial or continuation).
    meta = {
        "ok": True,
        "tool": tool,
        "mode": mode,
        "workflow_stage": workflow_stage,
        "query": bounded_query,
        "target": scope,
        "budget": budget,
        "bundle_type": bundle_type,
        "hit_count": hit_count,
        "output_cap_bytes": output_cap_bytes,
        "cache_hit": False,
        "cache_receipt": False,
        "authority_source": "rework_overlay" if overlay_applied else binding.authority_source,
        "authority_state": (
            "request_scoped_worktree" if overlay_applied else binding.authority_state
        ),
        "authority_repo": str(query_repo),
        "target_request_id": (
            str(ctx.rework_overlay_packet.get("predecessor_request_id") or "")
            if overlay_applied and ctx.rework_overlay_packet is not None
            else binding.target_request_id
        ),
        "target_task_id": (
            str(ctx.rework_overlay_packet.get("predecessor_task_id") or "")
            if overlay_applied and ctx.rework_overlay_packet is not None
            else binding.target_task_id
        ),
        "packet_sha256": (
            overlay_view.snapshot_sha256
            if overlay_applied and overlay_view is not None
            else binding.packet_sha256
        ),
        "index_revision": index_identity["build_revision"],
        "index_finished_at": index_identity["finished_at"],
        "evidence_counts": evidence_counts,
        "internal_truncated": internal_truncated,
    }

    cacheable = generation_is_definite
    text = canonical_bytes.decode("utf-8")
    bytes_returned = len(canonical_bytes)
    result = {
        **meta,
        "truncated": internal_truncated,
        "outer_truncated": False,
        "bytes": bytes_returned,
        "content": text,
        "content_sha256": full_content_sha256,
    }
    if _serialized_response_bytes(result) > output_cap_bytes:
        bind = _continuation_bind(
            ctx,
            mode=mode,
            query=bounded_query,
            target=scope,
            budget=budget,
            bundle_type=bundle_type,
            workflow_stage=workflow_stage,
            content_sha256=full_content_sha256,
            index_identity=index_identity,
            authority_source=str(meta["authority_source"]),
            authority_state=str(meta["authority_state"]),
            authority_repo=str(query_repo),
            packet_sha256=str(meta["packet_sha256"]),
            target_request_id=str(meta["target_request_id"]),
            target_task_id=str(meta["target_task_id"]),
        )
        if len(canonical_bytes) > SOURCE_GRAPH_CONTINUATION_MAX_BYTES:
            return _violation(ctx, tool, "continuation_payload_too_large")
        store_id = secrets.token_hex(16)
        try:
            chunk_size = _continuation_chunk_size_for_cap(
                store_id=store_id,
                meta=meta,
                canonical_bytes=canonical_bytes,
                content_sha256=full_content_sha256,
                output_cap_bytes=output_cap_bytes,
            )
        except WorkerToolError as exc:
            return _violation(ctx, tool, str(exc)[:160])
        page_count = (len(canonical_bytes) + chunk_size - 1) // chunk_size
        stored_id = _continuation_put(
            canonical_bytes=canonical_bytes,
            content_sha256=full_content_sha256,
            bind=bind,
            meta=meta,
            store_id=store_id,
            chunk_size=chunk_size,
            page_count=page_count,
        )
        if stored_id is None:
            return _violation(ctx, tool, "continuation_payload_too_large")
        result, bytes_returned = _continuation_page_result(
            store_id=store_id,
            page_index=0,
            meta=meta,
            canonical_bytes=canonical_bytes,
            chunk_size=chunk_size,
            page_count=page_count,
            content_sha256=full_content_sha256,
        )
        # A paginated page is a partial view of the response; never cache it.
        cacheable = False

    if cacheable:
        _source_graph_cache_store(cache_key, {
            "result": result, "hit_count": hit_count, "bytes": bytes_returned,
            "content_sha256": full_content_sha256,
            "authority_source": result["authority_source"],
            "authority_state": result["authority_state"],
            "evidence_counts": evidence_counts,
        })
    _append_audit(
        ctx, tool=tool, ok=True, cache_hit=False, hit_count=hit_count, bytes_returned=bytes_returned,
        authority_source=result["authority_source"],
        authority_state=result["authority_state"],
        authority_repo=query_repo,
        # A fresh, authoritative, non-cache result is a live provider call by
        # default, but a coordinator-side launch-time prefetch carries
        # ctx.provenance == "prefetch" and must never be credited as live.
        provenance=ctx.provenance or "live",
        payload={
            "mode": mode,
            "query_sha256": query_sha256,
            "workflow_stage": workflow_stage,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "index_revision": index_identity["build_revision"],
            "index_finished_at": index_identity["finished_at"],
            "evidence_counts": evidence_counts,
            "output_cap_bytes": output_cap_bytes,
            "target_request_id": result["target_request_id"],
            "target_task_id": result["target_task_id"],
            "packet_sha256": result["packet_sha256"],
            "internal_truncated": internal_truncated,
            "outer_truncated": result["outer_truncated"],
            "page_index": result.get("page_index", 0),
        },
    )
    return result


def source_graph_recommendation_roundtrip_gate(
    ctx: WorkerToolContext,
    *,
    sample_limit: int = 6,
) -> dict[str, Any]:
    """Replay emitted guidance through the production MCP wrapper path.

    The manager MCP delegates to :func:`source_graph_query`, so this single
    probe exercises the shared manager/worker wrapper, including authority
    resolution, bounded canonical JSON, hit counting and cache behavior.  A
    direct engine replay is performed only after a wrapper miss so the defect
    is attributed to either emission/engine or wrapper handling.
    """

    from . import source_graph as _source_graph_mod

    sample_limit = max(1, min(int(sample_limit), 16))
    try:
        binding = _resolve_source_graph_db(ctx)
        conn = _source_graph_mod.connect(binding.db_path, read_only=True)
        try:
            seeds = [
                str(row["qualname"])
                for row in conn.execute(
                    "SELECT qualname FROM entities WHERE qualname != '' "
                    "ORDER BY confidence DESC, qualname, file_path LIMIT ?",
                    (sample_limit,),
                )
            ]
        finally:
            conn.close()
    except (WorkerToolError, OSError, sqlite3.Error) as exc:
        return {
            "schema_id": "aiworkhub.source_graph.recommendation_roundtrip.v1",
            "ok": False,
            "status": "probe_unavailable",
            "error": f"{type(exc).__name__}:{exc}"[:240],
            "sampled_symbols": 0,
            "emitted": 0,
            "resolved": 0,
            "resolvability_ratio": None,
            "failures": [],
        }

    emitted: dict[tuple[str, str], dict[str, str]] = {}
    seed_failures: list[dict[str, str]] = []
    for seed in seeds:
        focus_result = source_graph_query(
            ctx, mode="focus", query=seed, budget=16,
            workflow_stage="orientation",
        )
        if not focus_result.get("ok") or int(focus_result.get("hit_count") or 0) < 1:
            seed_failures.append({
                "kind": "focus_seed",
                "value": seed,
                "layer": "wrapper",
                "reason": str(focus_result.get("error") or "zero_hits")[:160],
            })
            continue
        try:
            payload = json.loads(str(focus_result.get("content") or "{}"))
        except json.JSONDecodeError:
            seed_failures.append({
                "kind": "focus_seed",
                "value": seed,
                "layer": "wrapper",
                "reason": "non_json_content",
            })
            continue
        for raw_step in payload.get("recommended_next_steps") or []:
            if not isinstance(raw_step, str) or ":" not in raw_step:
                continue
            mode, value = raw_step.split(":", 1)
            if mode in {"slice", "context"} and value.strip():
                emitted[(mode, value.strip())] = {
                    "kind": "recommended_next_step", "source": seed,
                }
        for raw_file in payload.get("candidate_files") or []:
            if isinstance(raw_file, str) and raw_file.strip():
                emitted[("file", raw_file.strip())] = {
                    "kind": "candidate_file", "source": seed,
                }

    failures = list(seed_failures)
    resolved = 0
    for (mode, value), origin in sorted(emitted.items()):
        wrapped = source_graph_query(
            ctx,
            mode=mode,  # type: ignore[arg-type]
            query=value,
            budget=16,
            workflow_stage="orientation",
        )
        if wrapped.get("ok") and int(wrapped.get("hit_count") or 0) >= 1:
            resolved += 1
            continue
        try:
            if mode == "slice":
                direct = _source_graph_mod.slice_(ctx.authority_repo, value, 16)
            elif mode == "context":
                direct = _source_graph_mod.context_query(ctx.authority_repo, value, 16)
            else:
                direct = _source_graph_mod.file_query(ctx.authority_repo, value, 16)
            direct_hits = _json_hit_count(direct)
        except (OSError, sqlite3.Error, _source_graph_mod.SourceGraphError):
            direct_hits = 0
        failures.append({
            "kind": origin["kind"],
            "value": f"{mode}:{value}",
            "source": origin["source"],
            "layer": "wrapper" if direct_hits >= 1 else "engine_or_emission",
            "reason": str(wrapped.get("error") or "zero_hits")[:160],
        })

    total = len(emitted)
    ratio = (resolved / total) if total else None
    no_guidance = bool(seeds) and total == 0
    ok = not failures and not no_guidance
    return {
        "schema_id": "aiworkhub.source_graph.recommendation_roundtrip.v1",
        "ok": ok,
        "status": "ready" if ok else "guidance_degraded",
        "build_revision": _source_graph_mod.BUILD_REVISION,
        "sampled_symbols": len(seeds),
        "emitted": total,
        "resolved": resolved,
        "resolvability_ratio": round(ratio, 6) if ratio is not None else None,
        "no_guidance_emitted": no_guidance,
        "failures": failures[:32],
        "measurement_boundary": "full_shared_mcp_wrapper_roundtrip",
    }


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
        doc_columns = {
            str(row[1])
            for row in con.execute("PRAGMA table_info(documents)").fetchall()
        }
        selected_columns = [
            column for column in (
                "doc_id", "source", "source_id", "session_id", "timestamp",
                "kind", "speaker", "content", "tags",
            )
            if column in doc_columns
        ]
        if not {"source_id", "content"}.issubset(selected_columns):
            raise WorkerToolError("transcript_schema_missing_core_columns")
        clauses: list[str] = []
        params: list[Any] = []
        if "tags" in doc_columns:
            clauses.append("tags = ?")
            params.append(ctx.session_topic)
        if "source_id" in doc_columns:
            topic_like = _sqlite_like_literal(ctx.session_topic)
            clauses.append("(source_id = ? OR source_id LIKE ? ESCAPE '\\')")
            params.extend((ctx.session_topic, f"%:{topic_like}"))
        if not clauses:
            rows = []
        else:
            order_by = "timestamp DESC"
            if "doc_id" in doc_columns:
                order_by += ", doc_id DESC"
            rows = con.execute(
                f"SELECT {','.join(selected_columns)} FROM documents "
                f"WHERE {' OR '.join(clauses)} "
                f"ORDER BY {order_by} LIMIT ?",
                (*params, limit),
            ).fetchall()
    except sqlite3.Error as exc:
        return _violation(ctx, tool, f"tool_query_failed:{tool}:{exc}"[:160])
    except WorkerToolError as exc:
        return _violation(ctx, tool, str(exc)[:160])
    finally:
        con.close()

    evidence_rows: list[tuple[dict[str, Any], str]] = []
    for ordinal, row in enumerate(rows):
        item = {
            "source": row["source"] if "source" in row.keys() else "",
            "source_id": row["source_id"],
            "session_id": row["session_id"] if "session_id" in row.keys() else None,
            "topic": row["tags"] if "tags" in row.keys() else ctx.session_topic,
            "speaker": row["speaker"] if "speaker" in row.keys() else "",
            "timestamp": row["timestamp"] if "timestamp" in row.keys() else "",
            "kind": row["kind"] if "kind" in row.keys() else "",
            "snippet": (row["content"] or "")[:SESSION_SNIPPET_CHARS],
        }
        identity = str(row["doc_id"]) if "doc_id" in row.keys() else hashlib.sha256(
            f"{ordinal}:".encode("utf-8")
            + json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        evidence_rows.append((item, identity))
    evidence = [item for item, _identity in evidence_rows]
    state = "unknown" if not evidence else "current"
    payload = {
        "topic": ctx.session_topic, "state": state, "evidence_count": len(evidence),
        "evidence": evidence,
        "authority": {
            "source": session_binding.authority_source,
            "state": session_binding.authority_state,
            "repo": str(ctx.authority_repo),
        },
    }
    text, truncated = _bounded_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True), MAX_TOOL_OUTPUT_BYTES,
    )
    content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    delta_key = (
        "session_current_state",
        ctx.task_id,
        ctx.request_id,
        str(ctx.authority_repo),
        ctx.session_topic,
        limit,
        session_binding.authority_source,
        session_binding.authority_state,
    )
    previous = _prior_session_state(delta_key)
    returned_text = text
    delta_applied = False
    unchanged_reference = False
    base_content_sha256: str | None = None
    if previous is not None:
        base_content_sha256 = str(previous.get("content_sha256") or "") or None
        prior_evidence = {
            str(identity): item
            for identity, item in (previous.get("evidence_by_identity") or {}).items()
            if str(identity) and isinstance(item, Mapping)
        }
        current_evidence = {
            identity: item
            for item, identity in evidence_rows
            if identity
        }
        unchanged_reference = base_content_sha256 == content_sha256
        removed_identities = sorted(prior_evidence.keys() - current_evidence.keys())
        delta_payload = {
            "schema_id": "aiworkhub.session_context_delta.v1",
            "topic": ctx.session_topic,
            "base_content_sha256": base_content_sha256,
            "content_sha256": content_sha256,
            "unchanged": unchanged_reference,
            "current_state": state,
            "current_evidence_count": len(evidence),
            "added_or_changed": [
                current_evidence[identity]
                for identity in sorted(current_evidence)
                if prior_evidence.get(identity) != current_evidence[identity]
            ],
            "removed_source_ids": sorted({
                str(prior_evidence[identity].get("source_id") or "")
                for identity in removed_identities
                if str(prior_evidence[identity].get("source_id") or "")
            }),
            "removed_evidence_refs": removed_identities,
            "full_content_omitted": True,
            "canonical_audit_retained": True,
        }
        candidate = json.dumps(
            delta_payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
        if len(candidate.encode("utf-8")) < len(text.encode("utf-8")):
            returned_text = candidate
            delta_applied = True
    _remember_session_state(delta_key, {
        "content_sha256": content_sha256,
        "evidence_by_identity": {
            identity: item for item, identity in evidence_rows if identity
        },
    })
    hit_count = len(evidence)
    bytes_returned = len(returned_text.encode("utf-8"))
    original_bytes = len(text.encode("utf-8"))
    replay_bytes_avoided = max(0, original_bytes - bytes_returned)
    _append_audit(
        ctx, tool=tool, ok=True, cache_hit=delta_applied,
        hit_count=hit_count, bytes_returned=bytes_returned,
        authority_source=session_binding.authority_source, authority_state=session_binding.authority_state,
        payload={
            "session_delta": delta_applied,
            "unchanged_reference": unchanged_reference,
            "content_sha256": content_sha256,
            "base_content_sha256": base_content_sha256,
            "replay_original_bytes": original_bytes,
            "replay_returned_bytes": bytes_returned,
            "replay_bytes_avoided": replay_bytes_avoided,
            "provider_tokens_saved": None,
            "provider_token_savings_measured": False,
        },
    )
    return {
        "ok": True, "tool": tool, "topic": ctx.session_topic, "limit": limit,
        "truncated": truncated, "hit_count": hit_count, "bytes": bytes_returned,
        "content": returned_text, "cache_hit": delta_applied,
        "delta_receipt": delta_applied,
        "unchanged_reference": unchanged_reference,
        "content_sha256": content_sha256,
        "base_content_sha256": base_content_sha256,
        "replay_original_bytes": original_bytes,
        "replay_bytes_avoided": replay_bytes_avoided,
        "provider_tokens_saved": None,
        "provider_token_savings_measured": False,
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
        if not _table_exists(con, "memories"):
            return _violation(ctx, tool, "fts_unavailable:memories_table_absent")
        if not _table_exists(con, "memories_fts"):
            return _violation(ctx, tool, "fts_unavailable:memories_fts_absent")
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
        payload: dict[str, Any]
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


MAX_FINDING_JSON_STRING_CHARS = 32_768


def _decode_finding_json_object(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """Decode one bounded, one-level JSON-object finding input string."""
    if len(raw) > MAX_FINDING_JSON_STRING_CHARS:
        return None, "finding_json_object_string_too_large"
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None, "finding_json_object_string_invalid"
    if not isinstance(decoded, dict):
        return None, "finding_json_object_string_invalid"
    for value in decoded.values():
        if value is not None and not isinstance(value, (bool, int, float, str)):
            return None, "finding_json_object_string_invalid"
    return decoded, None


def _record_rejected_finding_intent(
    ctx: WorkerToolContext,
    tool: str,
    reason: str,
    *,
    packet_sha256: str,
    lens: str,
    raw_findings: list[Any],
) -> dict[str, Any]:
    """Authenticate rejected intent without admitting it as verified payload."""
    intent_bytes = json.dumps(
        {
            "findings": raw_findings,
            "lens": lens,
            "packet_sha256": packet_sha256,
        },
        default=str,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    intent_sha256 = hashlib.sha256(intent_bytes).hexdigest()
    appended = _append_audit(
        ctx,
        tool=tool,
        ok=False,
        cache_hit=False,
        hit_count=0,
        bytes_returned=0,
        violation=f"{reason}|rejected_intent_sha256={intent_sha256}",
    )
    authenticated = bool(appended)
    has_audit = (
        ctx.audit_ledger_path is not None and ctx.audit_hmac_key_path is not None
    )
    if appended and has_audit:
        verification = verify_audit_ledger(
            ctx.audit_ledger_path,
            ctx.audit_hmac_key_path,
            task_id=ctx.task_id,
            runner=ctx.runner,
            topic=ctx.topic,
            request_id=ctx.request_id,
        )
        authenticated = bool(verification.get("ok")) and int(
            verification.get("entries_tampered") or 0
        ) == 0
    return {
        "ok": False,
        "tool": tool,
        "reason": reason,
        "rejected_intent_authenticated": authenticated,
        "rejected_intent_sha256": intent_sha256,
    }


def _prior_rejected_finding_intent(ctx: WorkerToolContext, tool: str) -> bool:
    """Return true when this exact reviewer already submitted a rejected intent."""
    if ctx.audit_ledger_path is None or ctx.audit_hmac_key_path is None:
        return False
    verification = verify_audit_ledger(
        ctx.audit_ledger_path,
        ctx.audit_hmac_key_path,
        task_id=ctx.task_id,
        runner=ctx.runner,
        topic=ctx.topic,
        request_id=ctx.request_id,
    )
    if not verification.get("ok") or int(verification.get("entries_tampered") or 0):
        return True
    calls = verification.get("call_count_by_tool") or {}
    return (
        isinstance(calls, dict)
        and int(calls.get(tool) or 0) > 0
        and not verification.get("verified_payloads")
    )


# One initial submission plus exactly one correction retry, and never a third
# provider attempt.  M6 measured a ~300k-input-token review run discarded at
# finalize with review_protocol:structured_report_invalid because the reviewer
# only learned its report was out of scope after the run had ended; an
# unbounded correction loop would burn the same tokens a different way.
QUALITY_REVIEW_SUBMIT_MAX_ATTEMPTS = 2

_QUALITY_REVIEW_ATTEMPT_LOCK = threading.Lock()
_QUALITY_REVIEW_ATTEMPTS: dict[tuple[str, str, str, str], int] = {}


def _quality_review_attempt_key(
    ctx: WorkerToolContext, tool: str
) -> tuple[str, str, str, str]:
    """Scope the correction budget to this exact reviewer run and ledger."""
    return (ctx.request_id, ctx.task_id, str(ctx.audit_ledger_path), tool)


def _durable_failed_submit_attempts(ctx: WorkerToolContext, tool: str) -> int:
    """Count this run's authenticated submission attempts that never landed."""
    if ctx.audit_ledger_path is None or ctx.audit_hmac_key_path is None:
        return 0
    verification = verify_audit_ledger(
        ctx.audit_ledger_path,
        ctx.audit_hmac_key_path,
        task_id=ctx.task_id,
        runner=ctx.runner,
        topic=ctx.topic,
        request_id=ctx.request_id,
    )
    if not verification.get("ok") or int(verification.get("entries_tampered") or 0):
        return 0
    calls = verification.get("call_count_by_tool") or {}
    successful = verification.get("successful_call_count_by_tool") or {}
    return max(0, int(calls.get(tool) or 0) - int(successful.get(tool) or 0))


def _spent_submit_attempts(ctx: WorkerToolContext, tool: str) -> int:
    """Report how much of the correction budget this run has already spent.

    The authenticated ledger is the durable source, so a provider cannot reset
    its own budget.  The in-process tally is kept beside it because an
    unavailable or tampered ledger authenticates nothing: without it a failing
    audit writer would hand the reviewer an unbounded retry loop.
    """
    with _QUALITY_REVIEW_ATTEMPT_LOCK:
        spent = _QUALITY_REVIEW_ATTEMPTS.get(
            _quality_review_attempt_key(ctx, tool), 0
        )
    return max(spent, _durable_failed_submit_attempts(ctx, tool))


def _charge_failed_submit_attempt(
    ctx: WorkerToolContext, tool: str, result: dict[str, Any]
) -> dict[str, Any]:
    """Spend one attempt and tell the reviewer whether a correction remains.

    The validator's own machine-readable reason is passed through untouched:
    the reviewer has to see the exact error for the one retry to be useful.
    """
    key = _quality_review_attempt_key(ctx, tool)
    durable = _durable_failed_submit_attempts(ctx, tool)
    with _QUALITY_REVIEW_ATTEMPT_LOCK:
        spent = min(
            QUALITY_REVIEW_SUBMIT_MAX_ATTEMPTS,
            max(_QUALITY_REVIEW_ATTEMPTS.get(key, 0) + 1, durable),
        )
        _QUALITY_REVIEW_ATTEMPTS[key] = spent
    return {
        **result,
        "attempt": spent,
        "corrections_remaining": QUALITY_REVIEW_SUBMIT_MAX_ATTEMPTS - spent,
        "terminal": spent >= QUALITY_REVIEW_SUBMIT_MAX_ATTEMPTS,
    }


def quality_review_submit(
    ctx: WorkerToolContext,
    *,
    packet_sha256: str,
    lens: str,
    findings: list[quality_reviewer.QualityReviewFinding | str],
) -> dict[str, Any]:
    """Submit findings for the exact coordinator-bound review packet.

    Finding objects use the single canonical quality-review finding shape
    (see quality_reviewer.QUALITY_REVIEW_FINDING_SCHEMA_DOC). Undocumented
    keys are rejected by name and an empty findings list is valid. A finding
    may also be a bounded JSON string encoding one canonical finding object.

    The report's schema and scope are validated here, inside the run, at
    submission time. A rejected attempt returns the validator's exact
    machine-readable reason plus ``attempt``/``corrections_remaining``/
    ``terminal``, so the reviewer can correct and resubmit once. The second
    failed attempt is terminal and a third attempt is refused outright.

    This tool is inert for ordinary workers: the launcher must bind one
    immutable packet path into the worker runtime. The model cannot choose a
    target task/provider, and the signed audit payload carries the worker's
    immutable request/task identity rather than caller-supplied identity.
    """

    from . import quality_evidence

    tool = "quality_review_submit"
    spent = _spent_submit_attempts(ctx, tool)
    if spent >= QUALITY_REVIEW_SUBMIT_MAX_ATTEMPTS:
        # Refuse before validating anything so no third provider attempt can
        # exist, and so an exhausted reviewer cannot append further evidence.
        return {
            "ok": False,
            "tool": tool,
            "reason": "quality_review_correction_retry_exhausted",
            "attempt": spent,
            "corrections_remaining": 0,
            "terminal": True,
        }
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
    decoded_findings: list[Any] = []
    for raw_finding in findings:
        if isinstance(raw_finding, str):
            decoded, decode_reason = _decode_finding_json_object(raw_finding)
            if decoded is None:
                return _charge_failed_submit_attempt(
                    ctx,
                    tool,
                    _record_rejected_finding_intent(
                        ctx,
                        tool,
                        decode_reason,
                        packet_sha256=packet_sha256,
                        lens=lens,
                        raw_findings=list(findings),
                    ),
                )
            decoded_findings.append(decoded)
        else:
            decoded_findings.append(raw_finding)
    try:
        normalized_findings = quality_reviewer.normalize_packet_findings(
            packet,
            lens=lens,
            findings=decoded_findings,
        )
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
                    "findings": normalized_findings,
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
        return _charge_failed_submit_attempt(
            ctx,
            tool,
            _record_rejected_finding_intent(
                ctx,
                tool,
                str(exc),
                packet_sha256=packet_sha256,
                lens=lens,
                raw_findings=list(findings),
            ),
        )
    normalized, errors = quality_evidence.normalize_reviewer_reports(
        [
            {
                "lens": lens,
                "provider": ctx.runner,
                "read_only": True,
                "can_mutate_repo": False,
                "findings": normalized_findings,
            }
        ]
    )
    if errors or len(normalized) != 1:
        return _charge_failed_submit_attempt(
            ctx,
            tool,
            _record_rejected_finding_intent(
                ctx,
                tool,
                (errors[0] if errors else "reviewer_report_invalid"),
                packet_sha256=packet_sha256,
                lens=lens,
                raw_findings=list(findings),
            ),
        )
    if not normalized_findings and _prior_rejected_finding_intent(ctx, tool):
        return _charge_failed_submit_attempt(
            ctx,
            tool,
            _record_rejected_finding_intent(
                ctx,
                tool,
                "quality_review_empty_after_rejected_intent",
                packet_sha256=packet_sha256,
                lens=lens,
                raw_findings=[],
            ),
        )
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
    receipt_bytes = json.dumps(
        receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    submission_id = hashlib.sha256(receipt_bytes).hexdigest()

    def durable_ack(*, deduplicated: bool, retry_count: int) -> dict[str, Any]:
        return {
            "ok": True,
            "tool": tool,
            "status": "submitted",
            "durable": True,
            "submission_id": submission_id,
            "logical_receipt_count": 1,
            "retry_count": max(0, int(retry_count)),
            "deduplicated": bool(deduplicated),
            "packet_sha256": packet_sha256,
            "finding_count": len(normalized_findings),
        }

    def verified_payloads() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if ctx.audit_ledger_path is None or ctx.audit_hmac_key_path is None:
            return {}, []
        verification = verify_audit_ledger(
            ctx.audit_ledger_path,
            ctx.audit_hmac_key_path,
            task_id=ctx.task_id,
            runner=ctx.runner,
            topic=ctx.topic,
            request_id=ctx.request_id,
        )
        payloads = [
            payload
            for payload in verification.get("verified_payloads") or []
            if isinstance(payload, dict)
        ]
        return verification, payloads

    # A provider may retry after losing the tool response.  Reuse the already
    # authenticated record instead of appending another physical receipt.
    before, prior_payloads = verified_payloads()
    if prior_payloads:
        if any(payload != receipt for payload in prior_payloads):
            return _charge_failed_submit_attempt(
                ctx,
                tool,
                {
                    "ok": False,
                    "tool": tool,
                    "reason": "quality_review_submission_conflict",
                    "submission_id": submission_id,
                    "durable": bool(before.get("ok")),
                },
            )
        if before.get("ok") and int(before.get("entries_tampered") or 0) == 0:
            return durable_ack(deduplicated=True, retry_count=len(prior_payloads))

    appended = _append_audit(
        ctx,
        tool=tool,
        ok=True,
        cache_hit=False,
        hit_count=len(normalized_findings),
        bytes_returned=0,
        authority_source="supervisor" if ctx._supervisor_owned else "runtime",
        authority_state="process_bound",
        payload=receipt,
    )
    if not appended:
        # The audit ledger is unavailable, so nothing about this submission can
        # be authenticated: fail closed and spend the attempt.
        return _charge_failed_submit_attempt(
            ctx,
            tool,
            {
                "ok": False,
                "tool": tool,
                "reason": "quality_review_submission_not_durable",
                "submission_id": submission_id,
                "durable": False,
            },
        )
    after, persisted_payloads = verified_payloads()
    if (
        not after.get("ok")
        or int(after.get("entries_tampered") or 0) != 0
        or not persisted_payloads
    ):
        return _charge_failed_submit_attempt(
            ctx,
            tool,
            {
                "ok": False,
                "tool": tool,
                "reason": "quality_review_submission_not_durable",
                "submission_id": submission_id,
                "durable": False,
            },
        )
    if any(payload != receipt for payload in persisted_payloads):
        return _charge_failed_submit_attempt(
            ctx,
            tool,
            {
                "ok": False,
                "tool": tool,
                "reason": "quality_review_submission_conflict",
                "submission_id": submission_id,
                "durable": True,
            },
        )
    return durable_ack(
        deduplicated=len(persisted_payloads) > 1,
        retry_count=max(0, len(persisted_payloads) - 1),
    )


def quality_review_packet_read(ctx: WorkerToolContext) -> dict[str, Any]:
    """Return only the exact coordinator-bound, canonically verified packet."""

    tool = "quality_review_packet_read"
    path = ctx.quality_review_packet_path
    if path is None:
        return _violation(ctx, tool, "quality_review_packet_not_bound")
    try:
        if path.is_symlink() or not path.is_file():
            return _violation(ctx, tool, "quality_review_packet_invalid")
        if path.stat().st_size > MAX_QUALITY_REVIEW_PACKET_BYTES:
            return _violation(ctx, tool, "quality_review_packet_too_large")
        before = path.read_bytes()
        verified = quality_reviewer.verify_review_packet_candidate(
            path,
            ctx.repo,
            max_packet_bytes=MAX_QUALITY_REVIEW_PACKET_BYTES,
        )
        after = path.read_bytes()
        if before != after:
            return _violation(ctx, tool, "quality_review_packet_changed_during_read")
        packet = json.loads(after.decode("utf-8"))
    except quality_reviewer.ReviewerEvidenceError as exc:
        return _violation(ctx, tool, str(exc))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _violation(ctx, tool, "quality_review_packet_unreadable")
    if not isinstance(packet, dict):
        return _violation(ctx, tool, "quality_review_packet_schema_mismatch")
    result = {
        "ok": True,
        "tool": tool,
        "packet_sha256": verified["packet_sha256"],
        "packet": packet,
    }
    audit_configured = (
        ctx.audit_ledger_path is not None and ctx.audit_hmac_key_path is not None
    )
    appended = _append_audit(
        ctx,
        tool=tool,
        ok=True,
        cache_hit=False,
        hit_count=1,
        bytes_returned=len(
            json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ),
        authority_source="candidate_packet",
        authority_state="quality_review_readonly",
        payload={
            "packet_sha256": verified["packet_sha256"],
            "target_request_id": verified["target_request_id"],
            "target_task_id": verified["target_task_id"],
        },
    )
    if audit_configured and not appended:
        return {
            "ok": False,
            "tool": tool,
            "reason": "quality_review_packet_read_not_durable",
        }
    return result


class WorkerSemanticEditSession:
    """Request-local handles for Source-Graph-sized deterministic edits."""

    def __init__(self, ctx: WorkerToolContext) -> None:
        self.ctx = ctx
        self._targets: dict[str, semantic_edit.PreparedLineTarget] = {}
        self._receipts: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def prepare(
        self, *, file_path: str, start_line: int, end_line: int
    ) -> dict[str, Any]:
        tool = "semantic_edit_prepare"
        try:
            target = semantic_edit.prepare_line_target(
                self.ctx.repo,
                path=file_path,
                start_line=start_line,
                end_line=end_line,
                allowed_writes=self.ctx.allowed_writes,
            )
        except semantic_edit.SemanticEditError as exc:
            return _violation(self.ctx, tool, str(exc))
        target_id = secrets.token_hex(16)
        with self._lock:
            self._targets[target_id] = target
        receipt = target.receipt(target_id=target_id)
        _append_audit(
            self.ctx,
            tool=tool,
            ok=True,
            cache_hit=False,
            hit_count=1,
            bytes_returned=target.fragment_bytes,
            authority_source="worker_workspace",
            authority_state="hash_bound_fragment",
            payload={key: value for key, value in receipt.items() if key != "fragment"},
        )
        return {"ok": True, "tool": tool, **receipt}

    def apply(
        self, *, target_id: str, new: str, idempotency_key: str
    ) -> dict[str, Any]:
        tool = "semantic_edit_apply"
        if (
            not isinstance(target_id, str)
            or not target_id
            or not isinstance(new, str)
            or not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key.encode("utf-8")) > 256
        ):
            return _violation(self.ctx, tool, "semantic_edit_apply_input_invalid")
        new_sha256 = hashlib.sha256(new.encode("utf-8")).hexdigest()
        with self._lock:
            existing = self._receipts.get(idempotency_key)
            target = self._targets.get(target_id)
        if existing is not None:
            # A replay is the SAME operation only when it names the same
            # prepared target AND the identical replacement bytes.  The cache
            # is keyed on idempotency_key alone, so a reused key that points at
            # a different target_id or different content is a caller error --
            # refuse it explicitly instead of silently discarding the new edit
            # and answering with the first call's receipt for a different file.
            if (
                existing["target_id"] != target_id
                or existing["new_sha256"] != new_sha256
            ):
                return _violation(
                    self.ctx, tool, "semantic_edit_idempotency_key_conflict"
                )
            return {**existing["receipt"], "idempotent_replay": True}
        if target is None:
            return _violation(self.ctx, tool, "semantic_edit_target_unknown")
        try:
            current = semantic_edit.prepare_line_target(
                self.ctx.repo,
                path=target.path,
                start_line=target.start_line,
                end_line=target.end_line,
                allowed_writes=self.ctx.allowed_writes,
            )
            if current.current_sha256 != target.current_sha256:
                raise semantic_edit.SemanticEditError(
                    f"semantic_edit_stale_file:{target.path}"
                )
            if current.fragment_sha256 != target.fragment_sha256:
                raise semantic_edit.SemanticEditError(
                    f"semantic_edit_stale_fragment:{target.path}"
                )
            file_path = semantic_edit.resolve_existing_file(self.ctx.repo, target.path)
            # ``mkstemp`` creates the temp file 0600 and ``os.replace`` carries
            # that mode onto the destination, so capture the file's real mode
            # first and restore it after the swap -- otherwise every apply
            # silently rewrites e.g. an executable 0755 script down to 0600.
            original_mode = os.stat(file_path).st_mode & 0o7777
            _data, current_text = semantic_edit.read_utf8_file(file_path, target.path)
            next_text, metrics = semantic_edit.apply_line_ranges(
                current_text,
                [{
                    "start_line": target.start_line,
                    "end_line": target.end_line,
                    "new": new,
                    "fragment_sha256": target.fragment_sha256,
                }],
            )
            fd, temp_name = tempfile.mkstemp(prefix=f".{file_path.name}.aiworkhub-", dir=file_path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="", closefd=False) as handle:
                    handle.write(next_text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.close(fd)
                fd = -1
                if original_mode != 0o600:
                    # Restore the destination's real mode (mkstemp made the
                    # temp 0600).  Best effort: some sandboxed filesystems
                    # forbid chmod, and the content edit must still land
                    # atomically even when the mode cannot be carried across.
                    try:
                        os.chmod(temp_name, original_mode)
                    except OSError:
                        pass
                os.replace(temp_name, file_path)
            finally:
                if fd >= 0:
                    os.close(fd)
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
        except (OSError, semantic_edit.SemanticEditError) as exc:
            return _violation(self.ctx, tool, str(exc))
        next_bytes = next_text.encode("utf-8")
        receipt = {
            "ok": True,
            "tool": tool,
            "schema_id": "aiworkhub.semantic_edit_apply_receipt.v1",
            "target_id": target_id,
            "path": target.path,
            "before_sha256": target.current_sha256,
            "after_sha256": hashlib.sha256(next_bytes).hexdigest(),
            "idempotency_key": idempotency_key,
            "idempotent_replay": False,
            "file_bytes": target.file_bytes,
            **metrics,
        }
        # The authenticated audit record is part of the apply contract, not a
        # fire-and-forget side effect: process_launcher counts semantic-edit
        # runtime evidence from these ledger receipts, so an apply whose record
        # could not be written must fail closed rather than report ok: True for
        # a change acceptance can never observe.  This mirrors
        # quality_review_submit's own fail-closed durability contract.  A ctx
        # with no ledger bound leaves audit intentionally disabled (the append
        # is a no-op returning False) and is not a durability failure.
        audit_configured = (
            self.ctx.audit_ledger_path is not None
            and self.ctx.audit_hmac_key_path is not None
        )
        appended = _append_audit(
            self.ctx,
            tool=tool,
            ok=True,
            cache_hit=False,
            hit_count=1,
            bytes_returned=len(json.dumps(receipt, sort_keys=True).encode("utf-8")),
            authority_source="worker_workspace",
            authority_state="deterministic_apply",
            payload=receipt,
        )
        if audit_configured and not appended:
            return {
                "ok": False,
                "tool": tool,
                "reason": "semantic_edit_apply_not_durable",
                "target_id": target_id,
                "path": target.path,
            }
        # Only cache a receipt the ledger actually recorded (or that needed no
        # ledger); caching a non-durable apply would let a same-key retry
        # replay a success the ledger never authenticated.
        with self._lock:
            self._receipts[idempotency_key] = {
                "receipt": receipt,
                "target_id": target_id,
                "new_sha256": new_sha256,
            }
        return receipt


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
    kilo_config_path: Path
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
    allowed_writes: list[str] | tuple[str, ...] = (),
    python_executable: str | None = None,
    quality_review_packet_path: Path | None = None,
    rework_overlay_path: Path | None = None,
    provider_call_id: str | None = None,
    provenance: str | None = None,
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
        ENV_ALLOWED_WRITES: json.dumps(list(allowed_writes)),
        ENV_SESSION_TOPIC: session_topic,
        ENV_AUDIT_LEDGER_PATH: str(ledger_path),
        ENV_AUDIT_HMAC_KEY_PATH: str(key_path),
    }
    if quality_review_packet_path is not None:
        env[ENV_QUALITY_REVIEW_PACKET_PATH] = str(quality_review_packet_path)
    if rework_overlay_path is not None:
        env[ENV_REWORK_OVERLAY_PATH] = str(rework_overlay_path)
    # NF389/r6: absent (None) identity stays unbound; a present value -- even an
    # explicit empty string -- fails closed instead of silently dropping the
    # binding.
    if provider_call_id is not None:
        env[ENV_PROVIDER_CALL_ID] = validate_provider_call_id(provider_call_id)
    if provenance is not None:
        env[ENV_PROVENANCE] = validate_provenance(provenance)

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

    kilo_config_root = (home / ".config").resolve()
    kilo_config_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(kilo_config_root, 0o700)
    kilo_path = (kilo_config_root / "kilo" / "kilo.json").resolve()
    kilo_config = {
        "mcp": {
            SERVER_NAME: {
                "type": "local",
                "command": [py, *launch_args],
                "environment": env,
                "enabled": True,
            }
        }
    }
    _write_json_0600(kilo_path, kilo_config)

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
        kilo_config_path=kilo_path,
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
    "aiworkhub_worker_semantic_edit_prepare",
    "aiworkhub_worker_semantic_edit_apply",
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
    "aiworkhub_worker_quality_review_packet_read",
    "aiworkhub_worker_quality_review_submit",
)


def register_tools(mcp: Any, ctx: WorkerToolContext) -> tuple[str, ...]:
    """Register every worker-safe tool, bound to ``ctx``, on ``mcp``.

    No registered tool accepts a repository path, database path, task_id,
    runner or topic argument -- every one of those is closed over from
    ``ctx`` and cannot be overridden by the caller.
    """

    semantic_edits = WorkerSemanticEditSession(ctx)

    @mcp.tool(name="aiworkhub_worker_source_graph_query")
    def _source_graph_query(
        mode: SourceGraphMode, query: str, budget: int = 64,
        target: str | None = None, cursor: str | None = None,
        continuation_cursor: str | None = None,
        bundle_type: SourceGraphBundleType = "explore",
        workflow_stage: WorkflowStage = "unspecified",
    ) -> dict[str, Any]:
        """Bounded Source Graph discovery for this task."""
        return source_graph_query(
            ctx, mode=mode, query=query, budget=budget,
            target=target, cursor=cursor,
            continuation_cursor=continuation_cursor,
            bundle_type=bundle_type, workflow_stage=workflow_stage,
        )

    @mcp.tool(name="aiworkhub_worker_semantic_edit_prepare")
    def _semantic_edit_prepare(
        file_path: str, start_line: int, end_line: int,
    ) -> dict[str, Any]:
        """Read only one Source Graph-selected line range and bind its hashes."""
        return semantic_edits.prepare(
            file_path=file_path, start_line=start_line, end_line=end_line
        )

    @mcp.tool(name="aiworkhub_worker_semantic_edit_apply")
    def _semantic_edit_apply(
        target_id: str, new: str, idempotency_key: str,
    ) -> dict[str, Any]:
        """Apply replacement-only code to an immutable prepared target."""
        return semantic_edits.apply(
            target_id=target_id, new=new, idempotency_key=idempotency_key
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

    @mcp.tool(
        name="aiworkhub_worker_quality_review_packet_read",
        description=(
            "Read the exact coordinator-bound sealed quality-review packet. "
            "Reviewer-only and accepts no arguments."
        ),
    )
    def _quality_review_packet_read() -> dict[str, Any]:
        """Read the exact bound packet after canonical candidate verification."""
        return quality_review_packet_read(ctx)

    @mcp.tool(
        name="aiworkhub_worker_quality_review_submit",
        description=quality_reviewer.QUALITY_REVIEW_SUBMIT_TOOL_DESCRIPTION,
    )
    def _quality_review_submit(
        packet_sha256: str,
        lens: str,
        findings: list[quality_reviewer.QualityReviewFinding],
    ) -> dict[str, Any]:
        """Submit findings for the exact bound anti-anchored packet."""
        return quality_review_submit(
            ctx,
            packet_sha256=packet_sha256,
            lens=lens,
            findings=findings,
        )

    _quality_review_submit.__doc__ = quality_reviewer.QUALITY_REVIEW_SUBMIT_TOOL_DESCRIPTION

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


__all__ = [
    "AUDIT_ENTRY_SCHEMA_ID",
    "AUDIT_LEDGER_VERIFICATION_SCHEMA_ID",
    "AuthorityBinding",
    "BOUND_ENV_VARS",
    "ENV_AUDIT_HMAC_KEY_PATH",
    "ENV_AUDIT_LEDGER_PATH",
    "ENV_AUTHORITY_REPO",
    "ENV_ALLOWED_WRITES",
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
    "quality_review_packet_read",
    "resolve_host_package_import_root",
    "session_current_state",
    "source_graph_query",
    "source_graph_recommendation_roundtrip_gate",
    "verify_audit_ledger",
    "verify_quality_review_prewarm_authority",
]


def _verify_rework_overlay_packet(
    packet: dict[str, Any],
    successor_task_id: str,
    successor_request_id: str,
    runner: str,
    authority_repo: Path,
) -> None:
    """Fail closed on identity mismatch, missing canonical digest, or foreign repo."""
    required = ["successor_task_id", "successor_request_id",
                "predecessor_task_id", "predecessor_request_id",
                "authority_repo", "files", "canonical_digest"]
    missing = [k for k in required if k not in packet]
    if missing:
        raise WorkerToolError(f"rework_packet_missing_keys:{','.join(missing)}")
    if packet["successor_task_id"] != successor_task_id:
        raise WorkerToolError(
            f"rework_packet_successor_task_id_mismatch:"
            f"{packet['successor_task_id']} vs {successor_task_id}"
        )
    if packet["successor_request_id"] != successor_request_id:
        raise WorkerToolError(
            f"rework_packet_successor_request_id_mismatch:"
            f"{packet['successor_request_id']} vs {successor_request_id}"
        )
    if packet["successor_request_id"] == packet["predecessor_request_id"]:
        raise WorkerToolError("rework_packet_identity_not_distinct")
    # A rework episode normally keeps the same canonical task ID. Distinct
    # request IDs are the attempt identity and are mandatory above.
    if not str(packet["successor_task_id"]) or not str(packet["predecessor_task_id"]):
        raise WorkerToolError("rework_packet_task_identity_missing")
    if not _REQUEST_ID_RE.fullmatch(packet["successor_request_id"]):
        raise WorkerToolError("rework_packet_invalid_successor_request_id")
    if not _REQUEST_ID_RE.fullmatch(packet["predecessor_request_id"]):
        raise WorkerToolError("rework_packet_invalid_predecessor_request_id")
    packet_authority = Path(str(packet["authority_repo"])).resolve()
    if packet_authority != authority_repo.resolve():
        raise WorkerToolError(
            f"rework_packet_authority_mismatch:{packet_authority} vs {authority_repo}"
        )
    payload = {k: packet[k] for k in required if k != "canonical_digest"}
    expected_digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    if packet["canonical_digest"] != expected_digest:
        raise WorkerToolError(
            f"rework_packet_digest_mismatch:"
            f"{packet['canonical_digest']} vs {expected_digest}"
        )
    if not isinstance(packet["files"], list):
        raise WorkerToolError("rework_packet_files_invalid")
    if len(packet["files"]) > MAX_REWORK_OVERLAY_FILES:
        raise WorkerToolError("rework_packet_file_count_exceeded")
    seen_paths: set[str] = set()
    total_content_bytes = 0
    for entry in packet["files"]:
        if not isinstance(entry, dict):
            raise WorkerToolError("rework_packet_file_entry_invalid")
        p = str(entry.get("path", ""))
        parts = p.split("/")
        if (
            not p
            or p.startswith("/")
            or "\\" in p
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise WorkerToolError(f"rework_packet_invalid_file_path:{p}")
        if p in seen_paths:
            raise WorkerToolError(f"rework_packet_duplicate_file_path:{p}")
        seen_paths.add(p)
        if "deleted" in entry and "sha256" in entry:
            raise WorkerToolError(f"rework_packet_file_both_deleted_and_sha:{p}")
        if entry.get("deleted") is True:
            if "content_base64" in entry:
                raise WorkerToolError(f"rework_packet_deleted_file_has_content:{p}")
            continue
        expected_hash = str(entry.get("sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise WorkerToolError(f"rework_packet_file_hash_invalid:{p}")
        encoded = entry.get("content_base64")
        if encoded is not None:
            import base64
            try:
                content = base64.b64decode(str(encoded), validate=True)
            except (ValueError, TypeError) as exc:
                raise WorkerToolError(
                    f"rework_packet_file_content_invalid:{p}"
                ) from exc
            total_content_bytes += len(content)
            if total_content_bytes > MAX_REWORK_OVERLAY_PACKET_BYTES:
                raise WorkerToolError("rework_packet_content_bytes_exceeded")
            if hashlib.sha256(content).hexdigest() != expected_hash:
                raise WorkerToolError(
                    f"rework_packet_file_content_hash_mismatch:{p}"
                )


def _build_rework_overlay_map(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the verified packet entries keyed by normalized relative path."""

    return {str(entry["path"]): entry for entry in packet["files"]}


if __name__ == "__main__":
    main()
