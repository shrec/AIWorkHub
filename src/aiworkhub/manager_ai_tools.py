"""Manager-bound access to AIWorkHub's canonical project intelligence.

The task-scoped worker MCP and the manager MCP deliberately expose separate
tool names, but both call the same bounded in-process implementations and the
same repository-local canonical databases.  A caller cannot select another
repository or manufacture a manager identity.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import core
from . import context_writes
from . import context_importer
from . import context_graph
from . import feature_settings
from . import learning_commit_store
from . import needfix_ingest
from . import repository_state
from . import semantic_edit
from . import storage_registry
from . import task_decomposition
from . import workforce_catalog
from . import workforce_router
from . import worker_ai_tools_mcp as worker_tools


def _manager_context(*, topic: str = "management", target: str | None = None) -> tuple[worker_tools.WorkerToolContext | None, dict[str, Any]]:
    route = core.manager_bootstrap()
    identity = route.get("manager_route") if isinstance(route, dict) else None
    if route.get("role") != "manager" or not isinstance(identity, dict):
        return None, {"ok": False, "error": "verified_manager_identity_required"}
    session_id = str(identity.get("thread_id") or identity.get("session_id") or "").strip()
    provider = str(identity.get("provider") or route.get("provider") or "manager").strip()
    if not session_id:
        return None, {"ok": False, "error": "manager_session_identity_missing"}
    root = Path(str(route.get("repo") or core.repo_root())).resolve()
    context = worker_tools.WorkerToolContext(
        task_id=f"manager:{session_id}",
        runner=f"{provider}_manager",
        topic=str(topic or "management")[:128],
        request_id=session_id,
        repo=root,
        authority_repo=root,
        # A manager query's ``target`` is a QUERY SCOPE, not a security
        # allowlist, and the two must not be conflated.  Passing it through as
        # ``source_graph_targets`` made the manager's own selector its only
        # permitted path: a ``body``/``function`` target is a QUALNAME, the
        # resolved symbol's FILE never equals it, and the selector enforcement
        # then refused the manager's primary discovery surface with
        # ``symbol_out_of_scope`` -- the same qualname-as-path confusion
        # NF-2026-00348 removed one level down.  The scope still reaches the
        # engine through ``source_graph_query(target=...)``; what it must not do
        # is narrow the authority of a role that has repository-wide authority
        # by definition.  Task-scoped allowlists belong to WORKERS, which is
        # where they are still enforced.
        source_graph_targets=(),
        session_topic=str(topic or "management")[:128],
        audit_ledger_path=None,
        audit_hmac_key_path=None,
    )
    return context, {"provider": provider, "session_id": session_id, "repo": str(root)}


def _invoke(call: Callable[[worker_tools.WorkerToolContext], dict[str, Any]], *, topic: str = "management", target: str | None = None) -> dict[str, Any]:
    context, manager = _manager_context(topic=topic, target=target)
    if context is None:
        return manager
    result = dict(call(context))
    result["manager"] = manager
    result["surface"] = "manager_mcp"
    return result


def _write_invoke(
    call: Callable[[Path, dict[str, str]], dict[str, Any]], *, topic: str = "management",
    task_id: str = "",
) -> dict[str, Any]:
    context, manager = _manager_context(topic=topic)
    if context is None:
        return manager
    if not core.writes_allowed():
        return {"ok": False, "error": "write_gate_closed", "surface": "manager_mcp", "manager": manager}
    actor = {
        "role": "manager",
        "actor_id": manager["session_id"],
        "task_id": str(task_id or "")[:256],
        "provider": manager["provider"],
        "session_id": manager["session_id"],
    }
    try:
        result = dict(call(context.authority_repo, actor))
    except (
        context_writes.ContextWriteError,
        learning_commit_store.LearningCommitStoreError,
        needfix_ingest.NeedFixIngestError,
        workforce_catalog.WorkforceCatalogError,
    ) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    except sqlite3.IntegrityError as exc:
        result = {
            "ok": False,
            "error": "context_write_integrity_error",
            "sqlite_errorname": str(getattr(exc, "sqlite_errorname", "SQLITE_CONSTRAINT")),
            "constraint": str(exc).split(":", 1)[0][:120],
            "recovery_action": "retry_with_same_idempotency_key_or_use_update",
        }
    except (OSError, sqlite3.Error, storage_registry.StorageRegistryError) as exc:
        result = {"ok": False, "error": f"context_write_failed:{type(exc).__name__}"}
    result["manager"] = manager
    result["surface"] = "manager_mcp"
    return result


class _CandidateOverlayError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclasses.dataclass(frozen=True)
class _ReviewCandidate:
    request_id: str
    task_id: str
    path: Path
    hashes: dict[str, str | None]


_CANDIDATE_OVERLAY_DIR = (
    Path(repository_state.HUB_DIRNAME) / "runtime" / "manager_candidate_overlay"
)
_CANDIDATE_OVERLAY_SCHEMA = "aiworkhub.manager_candidate_overlay.v1"


def _resolve_review_candidate(request_id: str) -> _ReviewCandidate:
    """Resolve a review_ready request to its verified, sealed candidate worktree.

    Read-only: nothing is written, staged or touched in the worktree, and any
    identity, state, shape or hash discrepancy raises instead of degrading.
    """

    from . import process_launcher
    from .launch_zero_delta import split_lifecycle_tail

    manager = process_launcher.default_manager()
    events = manager._request_events(request_id)
    if not events:
        raise _CandidateOverlayError("candidate_request_not_found")
    task_id = str(split_lifecycle_tail(events)[0].get("task_id") or "")
    if not task_id:
        raise _CandidateOverlayError("candidate_task_missing")
    if not any(
        str(event.get("task_id") or "") == task_id
        and str(event.get("state") or "") == "review_ready"
        for event in events
    ):
        raise _CandidateOverlayError("candidate_not_review_ready")
    try:
        card = process_launcher._parse_card(manager._show_task(task_id), task_id)
    except (process_launcher.LaunchRejected, sqlite3.Error) as exc:
        raise _CandidateOverlayError("candidate_task_lookup_failed") from exc
    if not (
        str(card.get("status") or "") == "review"
        and str(card.get("worker_status") or "") == "review"
        and str(card.get("terminal_substatus") or "") == "review_ready"
    ):
        raise _CandidateOverlayError("candidate_not_review_ready")
    evidence = (card.get("terminal_review") or {}).get("evidence") or {}
    try:
        workspace = process_launcher.WorkerWorkspace.from_metadata(
            dict(evidence["workspace"])
        )
        if (
            workspace.repo != Path(manager.repo).resolve()
            or workspace.request_id != request_id
        ):
            raise process_launcher.WorkspaceError("identity_mismatch")
        process_launcher.assert_gc_safe_workspace_shape(
            request_id, workspace.path, workspace.home, repo=manager.repo
        )
    except (KeyError, TypeError, ValueError, process_launcher.WorkspaceError) as exc:
        raise _CandidateOverlayError("candidate_workspace_identity_mismatch") from exc
    if workspace.path.is_symlink() or not workspace.path.is_dir():
        raise _CandidateOverlayError("candidate_workspace_missing")
    sealed = evidence.get("changed_path_hashes")
    if not isinstance(sealed, dict) or not sealed:
        raise _CandidateOverlayError("candidate_hashes_missing")
    for relative in sealed:
        text = str(relative).replace("\\", "/")
        pure = PurePosixPath(text)
        if (
            not pure.parts
            or pure.is_absolute()
            or Path(text).is_absolute()
            or ".." in pure.parts
        ):
            raise _CandidateOverlayError("candidate_path_unsafe")
        current = workspace.path
        for part in pure.parts:
            current = current / part
            if current.is_symlink():
                raise _CandidateOverlayError("candidate_path_symlink")
    if process_launcher._changed_path_hashes(workspace, list(sealed)) != sealed:
        raise _CandidateOverlayError("candidate_hashes_drifted")
    return _ReviewCandidate(
        request_id=request_id,
        task_id=task_id,
        path=workspace.path,
        hashes={str(path): digest for path, digest in sealed.items()},
    )


def _candidate_request_finished(manager: Any, request_id: str) -> bool:
    """True only when the request is definitively not review_ready any more."""

    from . import process_launcher
    from .launch_zero_delta import split_lifecycle_tail

    events = manager._request_events(request_id)
    if not events:
        return True
    task_id = str(split_lifecycle_tail(events)[0].get("task_id") or "")
    if not task_id:
        return False
    card = process_launcher._parse_card(manager._show_task(task_id), task_id)
    return not (
        str(card.get("status") or "") == "review"
        and str(card.get("terminal_substatus") or "") == "review_ready"
    )


def _sweep_candidate_overlays(overlay_root: Path, keep_request_id: str) -> int:
    """Remove request dirs of finished requests; return how many were removed.

    Best effort and never raises: a failed lookup keeps the dir, symlinks are
    never followed, and an OSError during removal is retried on the next sweep.
    """

    import shutil

    removed = 0
    try:
        from . import process_launcher

        manager = process_launcher.default_manager()
        with os.scandir(overlay_root) as scan:
            entries = list(scan)
    except Exception:
        return 0
    for entry in entries:
        try:
            if entry.name == keep_request_id:
                continue
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            if not _candidate_request_finished(manager, entry.name):
                continue
            shutil.rmtree(entry.path)
            removed += 1
        except Exception:
            continue
    return removed


_CANDIDATE_OVERLAY_PARTIAL_LIMIT = 20


def _overlay_partial_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.stem + ".partial.json")


def _build_failed_reason(step: str, exc: BaseException) -> str:
    """``candidate_overlay_build_failed:<step>:<path-or-dash>:<ExceptionClass>``."""

    from . import source_graph_partition

    path = "-"
    if isinstance(exc, source_graph_partition.PartitionError):
        # PartitionError messages are ``<code>`` or ``<code>:<relative path>``.
        path = str(exc).partition(":")[2].strip()[:160] or "-"
    return f"candidate_overlay_build_failed:{step}:{path}:{type(exc).__name__}"


def _read_overlay_partial(db_path: Path) -> dict[str, Any] | None:
    """The bounded failed-file record stored beside a built overlay, if any."""

    path = _overlay_partial_path(db_path)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        files = [
            {str(k): str(v) for k, v in item.items()}
            for item in raw
            if isinstance(item, dict)
        ]
    except (OSError, ValueError, AttributeError, TypeError):
        return {"failed_files": [], "error": "partial_record_unreadable"}
    return {
        "failed_files": files[:_CANDIDATE_OVERLAY_PARTIAL_LIMIT],
        "failed_count": len(files),
    }


def _prune_superseded_overlays(cache_dir: Path, db_path: Path) -> None:
    """Drop older overlay partitions and the base pins only they referenced."""

    from . import source_graph_partition

    try:
        with os.scandir(cache_dir) as scan:
            entries = list(scan)
        for entry in entries:
            if (
                entry.name.startswith("overlay_")
                and entry.name.endswith((".sqlite", ".partial.json"))
                and entry.name not in (db_path.name, _overlay_partial_path(db_path).name)
                and not entry.is_symlink()
                and entry.is_file(follow_symlinks=False)
            ):
                try:
                    os.unlink(entry.path)
                except OSError:
                    continue
        marker = source_graph_partition.read_composed_marker(db_path)
        base = (marker or {}).get("base")
        pin = base.get("pin") if isinstance(base, dict) else None
        name = pin.get("name") if isinstance(pin, dict) else None
        if isinstance(name, str) and name:
            source_graph_partition._prune_base_pins(cache_dir, keep=name)
    except OSError:
        return


def _candidate_overlay_binding(
    ctx: worker_tools.WorkerToolContext, request_id: str
) -> worker_tools.AuthorityBinding:
    """Composed read-only Source Graph view of a review_ready candidate.

    A partition over the candidate's changed paths alone is composed with the
    canonical index opened read-only (never copied or written), cached beside
    the manager runtime state, and rebuilt only when the sealed candidate
    snapshot changes.
    """

    from . import source_graph
    from . import source_graph_partition

    candidate = _resolve_review_candidate(request_id)
    try:
        canonical = worker_tools._canonical_source_graph_binding(ctx)
    except worker_tools.WorkerToolError as exc:
        raise _CandidateOverlayError("candidate_canonical_index_unavailable") from exc
    changed_paths = [
        {"path": path, "sha256": digest or ""}
        for path, digest in sorted(candidate.hashes.items())
    ]
    snapshot_digest = hashlib.sha256(
        json.dumps(
            {
                "schema": _CANDIDATE_OVERLAY_SCHEMA,
                "request_id": candidate.request_id,
                "task_id": candidate.task_id,
                "files": changed_paths,
            },
            ensure_ascii=True,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    root = Path(ctx.authority_repo)
    cache_dir = root / _CANDIDATE_OVERLAY_DIR / candidate.request_id
    probe = root
    for part in cache_dir.relative_to(root).parts:
        probe = probe / part
        if probe.is_symlink():
            raise _CandidateOverlayError("candidate_overlay_cache_symlink")
    db_path = cache_dir / f"overlay_{snapshot_digest[:16]}.sqlite"
    step = "prepare"
    try:
        with worker_tools._SOURCE_GRAPH_DB_OVERRIDE_LOCK:
            cache_dir.mkdir(parents=True, exist_ok=True)
            if db_path.is_symlink():
                raise _CandidateOverlayError("candidate_overlay_cache_symlink")
            ready = (
                db_path.is_file()
                and db_path.stat().st_size > 0
                and worker_tools._rework_overlay_snapshot_digest(db_path)
                == snapshot_digest
            )
            if not ready:
                _sweep_candidate_overlays(
                    root / _CANDIDATE_OVERLAY_DIR, candidate.request_id
                )
                temporary = cache_dir / f".{db_path.name}.{secrets.token_hex(8)}.tmp"
                partial_path = _overlay_partial_path(db_path)
                partial_tmp = temporary.with_name(temporary.name + ".partial")
                try:
                    step = "build_partition"
                    report = source_graph_partition.build_partition(
                        candidate.path,
                        changed_paths,
                        temporary,
                        base_db_path=canonical.db_path,
                    )
                    step = "write_snapshot_digest"
                    worker_tools._write_rework_overlay_snapshot_digest(
                        temporary, snapshot_digest
                    )
                    step = "publish"
                    if report.failed_files:
                        partial_tmp.write_text(
                            json.dumps(
                                list(report.failed_files),
                                ensure_ascii=True,
                                sort_keys=True,
                            ),
                            encoding="utf-8",
                        )
                        os.replace(partial_tmp, partial_path)
                    else:
                        partial_path.unlink(missing_ok=True)
                    os.replace(temporary, db_path)
                finally:
                    temporary.unlink(missing_ok=True)
                    partial_tmp.unlink(missing_ok=True)
                _prune_superseded_overlays(cache_dir, db_path)
    except (
        source_graph_partition.PartitionError,
        source_graph.SourceGraphError,
        OSError,
        sqlite3.Error,
    ) as exc:
        raise _CandidateOverlayError(_build_failed_reason(step, exc)) from exc
    return worker_tools.AuthorityBinding(
        db_path=db_path,
        authority_source="candidate_overlay",
        authority_state="manager_review_readonly",
        authority_repo=candidate.path,
        target_request_id=candidate.request_id,
        target_task_id=candidate.task_id,
        packet_sha256=snapshot_digest,
    )


def source_graph_query(
    *,
    mode: worker_tools.SourceGraphMode,
    query: str,
    budget: int = 64,
    target: str | None = None,
    cursor: str | None = None,
    continuation_cursor: str | None = None,
    bundle_type: worker_tools.SourceGraphBundleType = "explore",
    workflow_stage: worker_tools.WorkflowStage = "unspecified",
    compact_replay: bool = True,
    request_id: str | None = None,
) -> dict[str, Any]:
    if mode == "diff" and request_id is not None:
        # ``diff`` reads the registered worktree itself, never a sealed
        # overlay: ``request_id`` only names it when ``target`` does not.
        wanted = str(request_id).strip()
        if target is not None and str(target).strip() != wanted:
            return _invoke(lambda _ctx: {
                "ok": False, "error": "diff_target_request_id_mismatch",
                "request_id": wanted, "target": target,
            }, target=target)
        target, request_id = wanted, None

    def call(ctx: worker_tools.WorkerToolContext) -> dict[str, Any]:
        if request_id is not None:
            wanted = str(request_id).strip()
            try:
                if not wanted:
                    raise _CandidateOverlayError("request_id_required")
                binding = _candidate_overlay_binding(ctx, wanted)
            except _CandidateOverlayError as exc:
                return {
                    "ok": False,
                    "error": "candidate_overlay_unavailable",
                    "reason": exc.reason,
                    "request_id": wanted,
                }
            ctx = dataclasses.replace(ctx, source_graph_binding_override=binding)
            partial = _read_overlay_partial(binding.db_path)
        else:
            partial = None
        reply = worker_tools.source_graph_query(
            ctx, mode=mode, query=query, budget=budget,
            target=target, cursor=cursor,
            continuation_cursor=continuation_cursor,
            bundle_type=bundle_type, workflow_stage=workflow_stage,
            compact_replay=compact_replay,
        )
        if partial is not None and isinstance(reply, dict):
            reply = {**reply, "candidate_overlay_partial": partial}
        return reply

    return _invoke(call, target=target)


def session_current_state(*, topic: str = "management", limit: int = 12) -> dict[str, Any]:
    return _invoke(
        lambda ctx: worker_tools.session_current_state(ctx, limit=limit),
        topic=topic,
    )


def _repair_memory_fts(repo: Path) -> dict[str, Any]:
    """Repair a missing ``memories_fts`` on the canonical memory DB; never raise.

    The repair is DDL on the canonical store, so it passes the same manager
    write gate as ``_write_invoke``: with the gate closed the store is not
    opened at all and the search reports its own result.
    ``context_writes.ensure_memories_fts`` is a bounded no-op once the index
    exists.  Any failure comes back as a bounded outcome for the caller to
    attach: the repair is best-effort and must not crash the search it serves.
    """
    try:
        if not feature_settings.enabled(repo, "ai_memory"):
            return {"ok": True, "created": False, "reason": "ai_memory_disabled"}
        if not core.writes_allowed():
            return {"ok": True, "created": False, "reason": "write_gate_closed"}
        outcome = context_writes.ensure_memories_fts(repo)
    except Exception as exc:
        return {"ok": False, "error": "fts_repair_raised", "detail": f"{type(exc).__name__}: {exc}"[:160]}
    if not isinstance(outcome, dict):
        return {"ok": False, "error": "fts_repair_invalid_outcome"}
    return outcome


def ai_memory_search(*, query: str, limit: int = 8) -> dict[str, Any]:
    def _search(ctx: worker_tools.WorkerToolContext) -> dict[str, Any]:
        # The worker-facing tool only reads the canonical store.  The manager
        # holds its authority, so a legacy or stranded store is repaired here,
        # once, before the query.  A failed repair leaves the search's own
        # ``fts_unavailable`` result in place, with the repair error attached.
        repair = _repair_memory_fts(ctx.authority_repo)
        result = dict(worker_tools.ai_memory_search(ctx, query=query, limit=limit))
        if not repair.get("ok"):
            result["fts_repair"] = repair
        return result

    return _invoke(_search)


def ai_memory_get(*, key: str) -> dict[str, Any]:
    return _invoke(lambda ctx: worker_tools.ai_memory_get(ctx, key=key))


def ai_memory_related(*, key: str) -> dict[str, Any]:
    return _invoke(lambda ctx: worker_tools.ai_memory_related(ctx, key=key))


MEMORY_REVIEW_SCHEMA_ID = "aiworkhub.memory_review.v1"
_MEMORY_REVIEW_SCAN_CAP = 5000
_MEMORY_REVIEW_MAX_BYTES = 16 * 1024
_MEMORY_REVIEW_VALUE_CHARS = 200
_MEMORY_REVIEW_UNKNOWN_EXAMPLES = 5
_MEMORY_REVIEW_JACCARD = 0.8
_MEMORY_REVIEW_COMPARISON_CAP = 200_000
_MEMORY_REVIEW_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _clamp_int(raw: Any, *, default: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _review_text(raw: Any) -> tuple[str, bool]:
    text = str(raw or "")
    if len(text) > _MEMORY_REVIEW_VALUE_CHARS:
        return text[:_MEMORY_REVIEW_VALUE_CHARS], True
    return text, False


def _parse_memory_timestamp(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def memory_review_from_connection(
    con: sqlite3.Connection, *, limit: int = 20, stale_days: int = 90,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Propose supersede candidates from active memories; never writes."""
    limit = _clamp_int(limit, default=20, low=1, high=50)
    stale_days = _clamp_int(stale_days, default=90, low=1, high=36500)
    now = now or datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "ok": True, "schema_id": MEMORY_REVIEW_SCHEMA_ID, "limit": limit,
        "stale_days": stale_days, "timestamps_available": False, "scanned": 0,
        "scan_cap": _MEMORY_REVIEW_SCAN_CAP, "scan_truncated": False,
        "unknown_age": {"count": 0, "example_keys": []},
        "unparseable_timestamps": 0,
        "candidate_counts": {"stale": 0, "key_collision": 0, "near_duplicate": 0},
        "candidates": [], "candidates_omitted": 0, "truncated": False,
    }
    tables = {
        str(row[0]) for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "memories" not in tables:
        return report
    columns = {str(row[1]) for row in con.execute("PRAGMA table_info(memories)")}
    ts_columns = [c for c in ("updated_at", "created_at") if c in columns]
    report["timestamps_available"] = bool(ts_columns)
    updated_sql = "m.updated_at" if "updated_at" in columns else "NULL"
    created_sql = "m.created_at" if "created_at" in columns else "NULL"
    if "context_entity_state" in tables:
        state_join = (
            "LEFT JOIN context_entity_state s ON s.entity_type='memory' AND s.entity_id=m.id "
            "WHERE COALESCE(s.status,'active')='active' "
        )
    else:
        state_join = ""
    rows = con.execute(
        f"SELECT m.id,m.key,m.value,m.tags,{updated_sql},{created_sql} FROM memories m "
        f"{state_join}ORDER BY m.id DESC LIMIT ?",
        (_MEMORY_REVIEW_SCAN_CAP + 1,),
    ).fetchall()
    if len(rows) > _MEMORY_REVIEW_SCAN_CAP:
        rows = rows[:_MEMORY_REVIEW_SCAN_CAP]
        report["scan_truncated"] = True
    report["scanned"] = len(rows)
    value_truncated = False

    def clip(raw: Any) -> str:
        nonlocal value_truncated
        text, cut = _review_text(raw)
        value_truncated = value_truncated or cut
        return text

    stale: list[tuple[float, dict[str, Any]]] = []
    unknown_keys: list[str] = []
    unknown_count = 0
    unparseable = 0
    by_key: dict[str, list[Any]] = {}
    by_tag: dict[str, list[tuple[Any, frozenset[str]]]] = {}
    cutoff_days = float(stale_days)
    for row in rows:
        memory_id, key, value, tags, updated_at, created_at = row
        key = str(key or "")
        by_key.setdefault(key, []).append(row)
        tokens = frozenset(_MEMORY_REVIEW_TOKEN_RE.findall(str(value or "").lower()))
        if tokens:
            for tag in {t.strip().lower() for t in str(tags or "").split(",") if t.strip()}:
                by_tag.setdefault(tag, []).append((row, tokens))
        stamp_column, stamp = next(
            ((name, raw) for name, raw in (("updated_at", updated_at), ("created_at", created_at)) if raw),
            ("", None),
        )
        if stamp is None:
            unknown_count += 1
            if len(unknown_keys) < _MEMORY_REVIEW_UNKNOWN_EXAMPLES and key not in unknown_keys:
                unknown_keys.append(key)
            continue
        parsed = _parse_memory_timestamp(stamp)
        if parsed is None:
            unparseable += 1
            continue
        age_days = (now - parsed).total_seconds() / 86400.0
        if age_days > cutoff_days:
            stale.append((age_days, {
                "reason": "stale", "keys": [key],
                "evidence": {
                    "memory_id": int(memory_id), "timestamp_column": stamp_column,
                    "timestamp": str(stamp), "age_days": round(age_days, 1),
                    "stale_days": stale_days, "value": clip(value),
                },
            }))
    report["unknown_age"] = {
        "count": unknown_count, "example_keys": [clip(k) for k in unknown_keys],
    }
    report["unparseable_timestamps"] = unparseable

    candidates: list[dict[str, Any]] = [
        item for _age, item in sorted(stale, key=lambda pair: (-pair[0], pair[1]["keys"][0]))
    ]
    counts = report["candidate_counts"]
    counts["stale"] = len(candidates)

    collisions = []
    for key in sorted(k for k, group in by_key.items() if len(group) > 1):
        group = by_key[key]
        collisions.append({
            "reason": "key_collision", "keys": [clip(key)],
            "evidence": {
                "active_rows": len(group),
                "memories": [
                    {"memory_id": int(r[0]), "value": clip(r[2])} for r in group[:5]
                ],
            },
        })
    counts["key_collision"] = len(collisions)
    candidates.extend(collisions)

    duplicates: dict[tuple[int, int], dict[str, Any]] = {}
    comparisons = 0
    for tag in sorted(by_tag):
        group = by_tag[tag]
        for i, (row_a, tokens_a) in enumerate(group):
            for row_b, tokens_b in group[i + 1:]:
                if comparisons >= _MEMORY_REVIEW_COMPARISON_CAP:
                    report["truncated"] = True
                    break
                comparisons += 1
                if str(row_a[1]) == str(row_b[1]):
                    continue
                smaller, larger = sorted((len(tokens_a), len(tokens_b)))
                if smaller / larger < _MEMORY_REVIEW_JACCARD:
                    continue
                jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
                if jaccard < _MEMORY_REVIEW_JACCARD:
                    continue
                pair = tuple(sorted((int(row_a[0]), int(row_b[0]))))
                entry = duplicates.get(pair)
                if entry is None:
                    first, second = (row_a, row_b) if int(row_a[0]) < int(row_b[0]) else (row_b, row_a)
                    duplicates[pair] = {
                        "reason": "near_duplicate", "keys": [clip(first[1]), clip(second[1])],
                        "evidence": {
                            "jaccard": round(jaccard, 3), "shared_tags": [tag],
                            "memory_ids": [int(first[0]), int(second[0])],
                            "values": [clip(first[2]), clip(second[2])],
                        },
                    }
                else:
                    entry["evidence"]["shared_tags"].append(tag)
    ordered_duplicates = [duplicates[pair] for pair in sorted(duplicates)]
    counts["near_duplicate"] = len(ordered_duplicates)
    candidates.extend(ordered_duplicates)

    report["candidates"] = candidates[:limit]
    report["candidates_omitted"] = max(0, len(candidates) - limit)
    report["truncated"] = report["truncated"] or value_truncated
    while report["candidates"] and len(
        json.dumps(report, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ) > _MEMORY_REVIEW_MAX_BYTES:
        report["candidates"].pop()
        report["candidates_omitted"] += 1
        report["truncated"] = True
    return report


def memory_review_report(*, limit: int = 20, stale_days: int = 90) -> dict[str, Any]:
    def _review(ctx: worker_tools.WorkerToolContext) -> dict[str, Any]:
        if not feature_settings.enabled(ctx.authority_repo, "ai_memory"):
            return feature_settings.disabled_result("ai_memory")
        try:
            binding = worker_tools._resolve_authority_db(ctx, component="memory", db_id="memory")
            con = worker_tools._open_readonly_db(binding.db_path, tool="ai_memory_review")
        except worker_tools.WorkerToolError as exc:
            return {"ok": False, "error": str(exc)[:160]}
        try:
            return memory_review_from_connection(con, limit=limit, stale_days=stale_days)
        except sqlite3.Error as exc:
            return {"ok": False, "error": f"memory_review_query_failed:{exc}"[:160]}
        finally:
            con.close()

    return _invoke(_review)


def kb_search(*, query: str, limit: int = 8) -> dict[str, Any]:
    return _invoke(lambda ctx: worker_tools.kb_search(ctx, query=query, limit=limit))


def kb_get(*, key: str) -> dict[str, Any]:
    return _invoke(lambda ctx: worker_tools.kb_get(ctx, key=key))


def kb_related(*, key: str) -> dict[str, Any]:
    return _invoke(lambda ctx: worker_tools.kb_related(ctx, key=key))


def context_graph_search(*, query: str, limit: int = 12) -> dict[str, Any]:
    context, manager = _manager_context()
    if context is None:
        return manager
    try:
        result = context_graph.search(context.authority_repo, query, limit=limit)
        context_graph.record_query_telemetry(
            context.authority_repo, operation="search", result=result
        )
    except (context_graph.ContextGraphError, OSError, sqlite3.Error) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    return {**result, "manager": manager, "surface": "manager_mcp"}


def context_graph_range(
    *, thread_id: str, around_event_id: int, before: int = 5, after: int = 5,
) -> dict[str, Any]:
    context, manager = _manager_context()
    if context is None:
        return manager
    try:
        result = context_graph.get_range(
            context.authority_repo,
            thread_id=thread_id,
            around_event_id=around_event_id,
            before=before,
            after=after,
        )
        context_graph.record_query_telemetry(
            context.authority_repo, operation="range", result=result
        )
    except (context_graph.ContextGraphError, OSError, sqlite3.Error) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    return {**result, "manager": manager, "surface": "manager_mcp"}


def context_graph_related(*, node_id: str, limit: int = 20) -> dict[str, Any]:
    context, manager = _manager_context()
    if context is None:
        return manager
    try:
        result = context_graph.related(context.authority_repo, node_id=node_id, limit=limit)
        context_graph.record_query_telemetry(
            context.authority_repo, operation="related", result=result
        )
    except (context_graph.ContextGraphError, OSError, sqlite3.Error) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    return {**result, "manager": manager, "surface": "manager_mcp"}


def context_graph_event_write(
    *, role: str, event_type: str, content: str, source_ref: str,
    idempotency_key: str, task_id: str = "", metadata: dict[str, Any] | None = None,
    thread_id: str = "", session_id: str = "", provider: str = "",
) -> dict[str, Any]:
    context, manager = _manager_context()
    if context is None:
        return manager
    if not core.writes_allowed():
        return {"ok": False, "error": "write_gate_closed", "manager": manager, "surface": "manager_mcp"}
    bound_thread = thread_id.strip() or str(manager["session_id"])
    bound_session = session_id.strip() or bound_thread
    bound_provider = provider.strip() or str(manager["provider"])
    try:
        result = context_graph.append_event(
            context.authority_repo,
            thread_id=bound_thread,
            session_id=bound_session,
            provider=bound_provider,
            role=role,
            event_type=event_type,
            content=content,
            source_ref=source_ref,
            idempotency_key=idempotency_key,
            task_id=task_id,
            metadata=metadata,
        )
    except (context_graph.ContextGraphError, OSError, sqlite3.Error) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    return {**result, "manager": manager, "surface": "manager_mcp"}


def context_graph_rebuild() -> dict[str, Any]:
    context, manager = _manager_context()
    if context is None:
        return manager
    if not core.writes_allowed():
        return {"ok": False, "error": "write_gate_closed", "manager": manager, "surface": "manager_mcp"}
    try:
        result = context_graph.rebuild_projection(context.authority_repo)
    except (context_graph.ContextGraphError, OSError, sqlite3.Error) as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    return {**result, "manager": manager, "surface": "manager_mcp"}


def workforce_catalog_read() -> dict[str, Any]:
    def call(ctx: worker_tools.WorkerToolContext) -> dict[str, Any]:
        return workforce_catalog.build_routing_catalog(ctx.authority_repo)

    return _invoke(
        call
    )


def workforce_rank(
    *,
    task_id: str,
    kinds: list[str],
    risk: str = "medium",
    context_tokens: int = 0,
    tool_needs: list[str] | None = None,
    quality_floor: float = 0.0,
) -> dict[str, Any]:
    def call(ctx: worker_tools.WorkerToolContext) -> dict[str, Any]:
        task = workforce_router.TaskRequirements.build(
            task_id=task_id,
            repo_id=core.repository_current().get("repo_id") or "unknown",
            kinds=kinds,
            risk=risk,
            context_tokens=context_tokens,
            tool_needs=tool_needs or [],
            quality_floor=quality_floor,
        )
        snapshot = workforce_catalog.build_routing_catalog(ctx.authority_repo)
        return workforce_catalog.rank_task(ctx.authority_repo, task, catalog=snapshot)

    return _invoke(call)


def task_decomposition_preview(
    *,
    parent_task_id: str,
    objective: str,
    source_graph_receipt: dict[str, Any],
    children: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a pure Source Graph-grounded child-DAG proposal."""

    def call(ctx: worker_tools.WorkerToolContext) -> dict[str, Any]:
        try:
            return task_decomposition.build_proposal(
                ctx.authority_repo,
                parent_task_id=parent_task_id,
                objective=objective,
                source_graph_receipt=source_graph_receipt,
                children=children,
            )
        except task_decomposition.TaskDecompositionError as exc:
            return {"ok": False, "error": str(exc)[:500]}

    return _invoke(call, topic="task_decomposition")


def workforce_upsert(*, worker: dict[str, Any]) -> dict[str, Any]:
    return _write_invoke(
        lambda repo, actor: workforce_catalog.upsert_worker(repo, worker, actor=actor),
        topic="workforce",
    )


def session_write(
    *, action: context_writes.SessionAction, topic: str, content: str,
    idempotency_key: str, provenance: str, document_id: int | None = None,
) -> dict[str, Any]:
    context, manager = _manager_context(topic=topic)
    if context is not None and not feature_settings.enabled(context.authority_repo, "session_manager"):
        return {**feature_settings.disabled_result("session_manager"), "manager": manager, "surface": "manager_mcp"}
    return _write_invoke(
        lambda repo, actor: context_writes.session_write(
            repo, actor=actor, action=action, topic=topic, content=content,
            idempotency_key=idempotency_key, provenance=provenance,
            document_id=document_id,
        ),
        topic=topic,
    )


def ai_memory_write(
    *, action: context_writes.MemoryAction, key: str, value: str = "",
    tags: str = "", scope: str = "project", idempotency_key: str,
    provenance: str,
) -> dict[str, Any]:
    context, manager = _manager_context()
    if context is not None and not feature_settings.enabled(context.authority_repo, "ai_memory"):
        return {**feature_settings.disabled_result("ai_memory"), "manager": manager, "surface": "manager_mcp"}
    return _write_invoke(
        lambda repo, actor: context_writes.memory_write(
            repo, actor=actor, action=action, key=key, value=value, tags=tags,
            scope=scope, idempotency_key=idempotency_key, provenance=provenance,
        )
    )


def kb_write(
    *, action: context_writes.KbAction, key: str, title: str = "", body: str = "",
    category: str = "", tags: str = "", source_refs: str = "",
    replacement_key: str = "", idempotency_key: str, provenance: str,
) -> dict[str, Any]:
    context, manager = _manager_context()
    if context is not None and not feature_settings.enabled(context.authority_repo, "knowledge_base"):
        return {**feature_settings.disabled_result("knowledge_base"), "manager": manager, "surface": "manager_mcp"}
    return _write_invoke(
        lambda repo, actor: context_writes.kb_write(
            repo, actor=actor, action=action, key=key, title=title, body=body,
            category=category, tags=tags, source_refs=source_refs,
            replacement_key=replacement_key, idempotency_key=idempotency_key,
            provenance=provenance,
        )
    )


_ALREADY_PROPOSED_REASON_CODES = frozenset(
    {"skill_registry.immutable_identity", "skill_registry.immutable_digest"}
)


def _auto_mine_and_propose_skills(repo: Path) -> dict[str, Any]:
    """Best-effort skill mining after one accepted learning commit.

    Bounded and advisory: every failure here is caught and reported, never
    raised, so a mining or proposal defect can never flip the commit's own
    ``ok``/``failures`` -- the same contract ``skill_evidence`` above already
    keeps. ``manager_skill_tools.propose`` (full form, unchanged) is the only
    path to a stored record, and only a candidate whose ``draft_incomplete``
    is empty ever reaches it; partial judgement is reported, never guessed.
    A candidate whose identity or content digest the registry already holds
    is not a failure -- ``propose`` refuses the duplicate with reason_code
    ``immutable_identity``/``immutable_digest``, and that refusal is reported
    as ``already_proposed``, never as ``refused_by_reason``. This is what
    makes re-mining a cluster that gained members since its last proposal
    safe: the newer draft's content differs, but its identity (bound to the
    family's stable representative, never its growing membership) does not.
    """
    from . import manager_skill_tools, skill_miner

    started = time.monotonic()
    proposed: list[str] = []
    already_proposed: list[str] = []
    refused: dict[str, str] = {}
    candidate_count = 0
    try:
        report = skill_miner.mine(repo, include_sensitivity=False)
        candidates = report.get("candidates", [])
        candidate_count = len(candidates)
        for candidate in candidates:
            identity = str(candidate.get("candidate_id") or "")
            incomplete = candidate.get("draft_incomplete") or []
            if incomplete:
                refused[identity] = "draft_incomplete:" + ",".join(incomplete)
                continue
            draft = skill_miner.candidate_proposal_payload(candidate)
            propose_result = manager_skill_tools.propose(**draft)
            if propose_result.get("ok") is True:
                proposed.append(identity)
            elif propose_result.get("reason_code") in _ALREADY_PROPOSED_REASON_CODES:
                already_proposed.append(identity)
            else:
                refused[identity] = str(propose_result.get("error") or "propose_refused")[:200]
    except Exception as exc:  # noqa: BLE001 -- mining is advisory, never fatal to the commit
        refused["mining"] = f"skill_mining_failed:{type(exc).__name__}"
    return {
        "candidates": candidate_count,
        "proposed": proposed,
        "already_proposed": already_proposed,
        "refused_by_reason": refused,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
    }


def learning_commit(
    *,
    task_id: str,
    request_id: str,
    repo_area: str,
    outcome: str,
    evidence_ids: list[str],
    idempotency_key: str,
    provenance: str,
    root_cause_candidate: str = "",
    invariant_candidate: str = "",
    lesson_candidate: str = "",
    edge_candidates: list[dict[str, str]] | None = None,
    promote_ai_memory: bool = False,
    promote_context_graph: bool = False,
    promote_kb: bool = False,
) -> dict[str, Any]:
    """Commit one explicit manager learning decision after adjudication."""
    data = {
        "task_id": task_id,
        "repo_area": repo_area,
        "outcome": outcome,
        "evidence_ids": evidence_ids,
        "root_cause_candidate": root_cause_candidate or None,
        "invariant_candidate": invariant_candidate or None,
        "lesson_candidate": lesson_candidate or None,
        "edge_candidates": edge_candidates or [],
        "promotion_eligible_ai_memory": bool(promote_ai_memory),
        "promotion_eligible_context_graph": bool(promote_context_graph),
        "promotion_eligible_kb": bool(promote_kb),
    }
    result = _write_invoke(
        lambda repo, actor: learning_commit_store.commit_learning(
            repo,
            actor=actor,
            request_id=request_id,
            data=data,
            idempotency_key=idempotency_key,
            provenance=provenance,
        ),
        topic="learning_commit",
        task_id=task_id,
    )
    if result.get("commit_id") and result.get("outcome") == "accepted":
        from . import manager_skill_tools, recipe_miner

        # Applicability evidence, never a claim that the procedure was invoked.
        # Retry even an idempotent learning commit: the optional projection may
        # have failed after its durable decision was committed.
        try:
            evidence = manager_skill_tools.add_learning_commit_evidence(
                task_id=result["task_id"], request_id=result["request_id"]
            )
        except Exception as exc:  # noqa: BLE001 - durable learning survives projection failure
            evidence = {"ok": False, "reason": f"skill_evidence_failed:{type(exc).__name__}",
                        "recorded": []}
        reason = evidence.get("reason")
        failed = (
            evidence.get("ok") is not True
            or bool(evidence.get("unlinked"))
            or bool(reason and reason != "card_declares_no_selection_vocabulary")
        )
        result["skill_evidence"] = {**evidence, "state": "failed" if failed else "completed"}
        if failed:
            result["ok"] = False
            result["failures"] = {
                **result.get("failures", {}),
                "skill_evidence": evidence.get("reason") or "skill_evidence_unlinked",
            }
        context, _manager_info = _manager_context()
        result["skill_mining"] = _auto_mine_and_propose_skills(
            context.authority_repo if context is not None else core.repo_root()
        )
        result["recipe_mining"] = recipe_miner.auto_mine_and_propose(
            context.authority_repo if context is not None else core.repo_root()
        )
    return result


def needfix_markdown_preview(
    *, source_paths: list[str] | None = None, follow_links: bool = True,
) -> dict[str, Any]:
    def call(context: worker_tools.WorkerToolContext) -> dict[str, Any]:
        try:
            return needfix_ingest.preview(
                context.authority_repo,
                source_paths=source_paths,
                follow_links=follow_links,
            )
        except needfix_ingest.NeedFixIngestError as exc:
            return {"ok": False, "error": str(exc)[:240]}

    return _invoke(call, topic="needfix_markdown_intake")


def needfix_markdown_commit(
    *, source_paths: list[str] | None, preview_id: str, follow_links: bool = True,
) -> dict[str, Any]:
    return _write_invoke(
        lambda repo, _actor: needfix_ingest.commit(
            repo,
            source_paths=source_paths,
            preview_id=preview_id,
            follow_links=follow_links,
        ),
        topic="needfix_markdown_intake",
    )


def context_import(
    *, component: context_importer.Component, operation: context_importer.Operation,
    source_path: str = "", idempotency_key: str = "", import_id: str = "",
    provenance: str = "", limit: int = 10_000,
) -> dict[str, Any]:
    context, manager = _manager_context(topic="context_import")
    if context is None:
        return manager
    if operation != "dry_run" and not core.writes_allowed():
        return {
            "ok": False, "error": "write_gate_closed",
            "surface": "manager_mcp", "manager": manager,
        }
    try:
        result = context_importer.import_context(
            context.authority_repo,
            component=component,
            operation=operation,
            source_path=source_path,
            idempotency_key=idempotency_key,
            import_id=import_id,
            limit=limit,
            actor_id=manager["session_id"],
            provider=manager["provider"],
            provenance=provenance,
        )
    except context_importer.ContextImportError as exc:
        result = {"ok": False, "error": str(exc)[:240]}
    except (OSError, sqlite3.Error, storage_registry.StorageRegistryError) as exc:
        result = {"ok": False, "error": f"context_import_failed:{type(exc).__name__}"}
    result["manager"] = manager
    result["surface"] = "manager_mcp"
    return result


# ---------------------------------------------------------------------------
# Manager-seat semantic editing.
#
# The worker MCP has had ``semantic_edit_prepare``/``_apply`` from the start;
# the manager MCP had no semantic-edit tool at all, so the one seat whose
# charter is "small precise corrections" made them by whole-string rewrite and
# left no ``semantic_edit_apply_receipt`` in any authenticated ledger.  The
# coverage measurement that counts changed paths against apply receipts was
# therefore structurally blind to every edit the manager made: not "0%", but
# unmeasurable.
#
# Nothing here is a second implementation.  ``semantic_edit`` holds the
# hash-bound prepare, ``semantic_edit_applier`` holds the whole-file-preimage
# applier, and ``worker_ai_tools_mcp.WorkerSemanticEditSession`` holds the
# session, idempotency, receipt and ledger contract.  The manager seat gets a
# ``WorkerToolContext`` built from its VERIFIED route with an audit ledger
# bound, and drives that same session.  One definition, two seats, identical
# guarantees.
# ---------------------------------------------------------------------------

MANAGER_SEMANTIC_EDIT_TOPIC = "semantic_edit"

# The manager seat's scope answer.  A worker writes into a sandboxed worktree
# under a card's ``allowed_writes``; the manager writes into the CANONICAL tree
# and has no card, so a copy of the worker's rule would authorize nothing.
#
# The boundary implemented here is: every existing UTF-8 file in the verified
# repository working tree, EXCEPT AIWorkHub's own ``.aiworkhub`` state.  Three
# of the four refusals are already enforced by the shared primitives and are
# inherited rather than restated -- ``normalize_relative_path`` refuses an
# absolute path, a ``..`` escape and ``.git``; ``resolve_existing_file``
# refuses a symlink at any path component and any target that does not resolve
# under the root; ``read_utf8_file`` refuses a non-UTF-8 or oversized file.
#
# The one refusal this seat adds is ``.aiworkhub/**``.  That subtree is not
# source the manager corrects: it is the storage registry, the canonical
# databases, the credential and worktree runtime state, and the audit ledgers
# -- including THIS surface's own ledger -- that the manager's review reads as
# evidence.  A line-range edit there is never a correction; it is a rewrite of
# the evidence a decision is measured against.  It also makes
# "never edit a candidate worktree in place" mechanical, because candidate
# worktrees live under ``.aiworkhub/runtime/worktrees``.
#
# No narrower rule than that is implemented, deliberately.  The manager already
# writes to the canonical tree through ``scripts/manager_semantic_edit.py`` and
# through ordinary file tools, both of which accept any path in the repository;
# a narrower rule here would not remove that authority, it would only push the
# seat back to the unrecorded raw edit this surface exists to replace.  So this
# is strictly NARROWER than what the seat can already do, and widens nothing.
MANAGER_SEMANTIC_EDIT_SCOPE = (
    "canonical_repository_working_tree_excluding_aiworkhub_state"
)

# The three exceptions the repository's semantic-edit rule allows, taken from
# the finalizer's own tuple rather than restated here: the manager's declared
# code and the coverage record's vocabulary must be the same list or a manager
# declaration would read back as ``unknown_exception_code``.
_MANAGER_EDIT_SESSIONS: dict[
    tuple[str, str, str], worker_tools.WorkerSemanticEditSession
] = {}
_MANAGER_EDIT_LOCK = threading.Lock()
_MAX_MANAGER_EDIT_SESSIONS = 16
_MANAGER_SEMANTIC_EDIT_RUNTIME_REL = (
    Path(repository_state.HUB_DIRNAME) / "runtime" / "manager_semantic_edit"
)


def semantic_edit_policy_exceptions() -> tuple[str, ...]:
    """The closed vocabulary a fallback record may name.

    Imported lazily from ``process_launcher`` so this module keeps its light
    import graph, and read from there rather than copied so the declaring side
    and the reading side can never drift apart.  If that module cannot be
    imported the vocabulary is reported as empty, which makes every declared
    code read as unknown -- unmeasured with a named reason, never silently
    accepted.
    """

    try:
        from . import process_launcher
    except Exception:  # pragma: no cover - import-time environment failure
        return ()
    return tuple(
        str(code) for code in getattr(
            process_launcher, "SEMANTIC_EDIT_POLICY_EXCEPTIONS", ()
        )
    )


def _manager_edit_scope_refusal(relative: str) -> str:
    """``""`` when ``relative`` is inside the manager seat's scope."""

    parts = PurePosixPath(relative).parts
    if parts and parts[0] == repository_state.HUB_DIRNAME:
        return f"manager_semantic_edit_path_is_aiworkhub_state:{relative}"
    return ""


def _provision_manager_edit_ledger(root: Path) -> tuple[Path, Path]:
    """This repository's manager-seat HMAC ledger and key (0700 dir, 0600 files).

    Same shape and same appender as the per-request worker ledger, so
    ``verify_audit_ledger`` reads a manager apply receipt with no special case;
    the ledger row's ``runner`` already ends in ``_manager``, which the ledger
    stage inference has always understood.
    """

    runtime_dir = (root / _MANAGER_SEMANTIC_EDIT_RUNTIME_REL).resolve()
    key_path = runtime_dir / "audit_hmac.key"
    ledger_path = runtime_dir / "audit_ledger.jsonl"
    if not key_path.exists() or key_path.stat().st_size < 32:
        worker_tools._touch_0600(key_path)
        os.truncate(key_path, 0)
        with open(key_path, "wb") as handle:
            handle.write(secrets.token_bytes(32))
        os.chmod(key_path, 0o600)
    worker_tools._touch_0600(ledger_path)
    return ledger_path, key_path


def _manager_edit_session() -> tuple[
    worker_tools.WorkerSemanticEditSession | None, dict[str, Any]
]:
    """The verified manager seat's semantic-edit session, or a refusal dict.

    The session is cached per ``(provider, session_id, repo)`` because a
    prepared ``target_id`` must survive until the matching apply, and every
    component of that key comes from the verified route -- never from a caller
    argument.
    """

    context, manager = _manager_context(topic=MANAGER_SEMANTIC_EDIT_TOPIC)
    if context is None:
        return None, manager
    try:
        ledger_path, key_path = _provision_manager_edit_ledger(
            context.authority_repo
        )
    except OSError as exc:
        return None, {
            "ok": False,
            "error": f"manager_semantic_edit_ledger_unavailable:{type(exc).__name__}",
            "surface": "manager_mcp",
            "manager": manager,
        }
    key = (manager["provider"], manager["session_id"], manager["repo"])
    with _MANAGER_EDIT_LOCK:
        session = _MANAGER_EDIT_SESSIONS.get(key)
        if session is None:
            bound = dataclasses.replace(
                context,
                # ``path_is_allowed`` is a pure allowlist and an empty one
                # authorizes nothing, so the seat's scope is expressed as
                # "every repository-relative path" here and the one refusal
                # this seat adds is applied by ``prepare`` below, before any
                # target is ever minted.  ``apply`` can only act on a target
                # ``prepare`` minted, so no unchecked path can reach it.
                allowed_writes=("*",),
                audit_ledger_path=ledger_path,
                audit_hmac_key_path=key_path,
            )
            session = worker_tools.WorkerSemanticEditSession(bound)
            _MANAGER_EDIT_SESSIONS[key] = session
            while len(_MANAGER_EDIT_SESSIONS) > _MAX_MANAGER_EDIT_SESSIONS:
                _MANAGER_EDIT_SESSIONS.pop(next(iter(_MANAGER_EDIT_SESSIONS)))
    return session, manager


def _manager_edit_actor(manager: dict[str, Any]) -> dict[str, str]:
    """The receipt's actor, derived from the verified route and nothing else.

    Identical in shape and in origin to ``_write_invoke``'s actor: every field
    comes from ``core.manager_bootstrap()``'s verified identity, and no tool
    argument can reach it.
    """

    return {
        "role": "manager",
        "actor_id": str(manager["session_id"]),
        "provider": str(manager["provider"]),
        "session_id": str(manager["session_id"]),
        "repo": str(manager["repo"]),
    }


def _manager_edit_result(
    result: dict[str, Any], manager: dict[str, Any]
) -> dict[str, Any]:
    payload = dict(result)
    payload["manager"] = manager
    payload["surface"] = "manager_mcp"
    payload["actor"] = _manager_edit_actor(manager)
    payload["scope"] = MANAGER_SEMANTIC_EDIT_SCOPE
    payload["token_savings_claimed"] = False
    return payload


def semantic_edit_prepare(
    *, file_path: str, start_line: int, end_line: int,
    include_fragment: bool = False,
    expected_first_line: str | None = None,
    expected_last_line: str | None = None,
) -> dict[str, Any]:
    """MANAGER: bind one line range's whole-file and fragment sha256."""

    session, manager = _manager_edit_session()
    if session is None:
        return manager
    tool = "semantic_edit_prepare"
    try:
        relative = semantic_edit.normalize_relative_path(file_path)
    except semantic_edit.SemanticEditError as exc:
        return _manager_edit_result(
            {"ok": False, "tool": tool, "reason": str(exc)[:240]}, manager
        )
    refusal = _manager_edit_scope_refusal(relative)
    if refusal:
        return _manager_edit_result(
            {"ok": False, "tool": tool, "reason": refusal}, manager
        )
    return _manager_edit_result(
        session.prepare(
            file_path=relative,
            start_line=start_line,
            end_line=end_line,
            include_fragment=include_fragment,
            expected_first_line=expected_first_line,
            expected_last_line=expected_last_line,
        ),
        manager,
    )


def semantic_edit_apply(
    *, target_id: str, new: str, idempotency_key: str,
    allow_escape_level_change: bool = False,
) -> dict[str, Any]:
    """MANAGER: replace exactly the prepared range, refusing a moved file."""

    session, manager = _manager_edit_session()
    if session is None:
        return manager
    if not core.writes_allowed():
        # The same gate every other manager write answers to.  It is a
        # narrowing, not a widening: the seat can already edit this tree by
        # other means with no gate at all.
        return {
            "ok": False,
            "error": "write_gate_closed",
            "tool": "semantic_edit_apply",
            "surface": "manager_mcp",
            "manager": manager,
        }
    return _manager_edit_result(
        session.apply(
            target_id=target_id, new=new, idempotency_key=idempotency_key,
            allow_escape_level_change=allow_escape_level_change,
        ),
        manager,
    )


def _manager_edit_file_lines(root: Path, relative: str) -> int | None:
    """Line count of ``relative``, or ``None`` when it cannot be measured."""

    try:
        target = semantic_edit.resolve_existing_file(root, relative)
        _data, text = semantic_edit.read_utf8_file(target, relative)
    except (OSError, semantic_edit.SemanticEditError):
        return None
    return len(text.splitlines())


def semantic_edit_fallback_record(
    *, path: str, reason: str, exception: str = "",
    start_line: int = 0, end_line: int = 0,
) -> dict[str, Any]:
    """MANAGER RECORD: "I did not use semantic edit here, for this reason".

    A RECORD, never a gate.  It refuses no edit, it is written after the fact,
    and nothing in acceptance consults it -- its only consumer is the coverage
    evidence, where a raw edit with no record shows up as exactly that.  So it
    returns ``ok: True`` even for an out-of-vocabulary code or an unusable
    path: a record that can be refused is a record that hides edits.

    What can be derived is derived, from this seat's own evidence, and only the
    genuinely judgemental case needs the caller's word:

    * ``new_file`` -- derived when the path is absent from the canonical tree.
    * ``adapter_without_tools`` -- derived when this seat's own semantic-edit
      surface is unusable (no verified route, or no ledger).
    * ``spans_most_of_file`` -- derived when a supplied ``start_line``/
      ``end_line`` span covers a strict majority of the file's lines.  "Most"
      is taken at its literal meaning; the threshold is the definition of the
      word, not a tuned constant, and it is reported with the counts it was
      computed from.

    A declared code is recorded either way and marked ``corroborated`` or not,
    with the basis named.  Absence of corroboration reads as unmeasured with a
    reason, never as a refutation.
    """

    session, manager = _manager_edit_session()
    tool = "semantic_edit_exception_declare"
    vocabulary = semantic_edit_policy_exceptions()
    declared = str(exception or "").strip()[:64]
    reason_text = str(reason or "").strip()[:200]
    record: dict[str, Any] = {
        "ok": True,
        "tool": tool,
        "schema_id": "aiworkhub.manager_semantic_edit_fallback_record.v1",
        "is_gate": False,
        "allowed_exceptions": list(vocabulary),
        "token_savings_claimed": False,
    }
    if session is None:
        # No verified route or no ledger: the surface itself is unavailable, so
        # the record has nowhere authenticated to go.  Say so, and derive the
        # exception that the unavailability itself justifies.
        record.update({
            "recorded": False,
            "unrecorded_reason": str(manager.get("error") or "manager_route_unverified")[:120],
            "path": str(path or "")[:512],
            "reason": reason_text,
            "exception": declared or "adapter_without_tools",
            "derived": [{
                "path": str(path or "")[:512],
                "exception": "adapter_without_tools",
                "basis": "manager_semantic_edit_surface_unavailable",
                "source": "runtime_derivation",
            }],
            "declared": [],
            "manager": manager,
            "surface": "manager_mcp",
        })
        return record

    root = session.ctx.authority_repo
    try:
        relative = semantic_edit.normalize_relative_path(path)
    except semantic_edit.SemanticEditError as exc:
        record.update({
            "recorded": False,
            "unrecorded_reason": str(exc)[:120],
            "path": str(path or "")[:512],
            "reason": reason_text,
            "exception": declared,
            "derived": [],
            "declared": [],
        })
        return _manager_edit_result(record, manager)

    file_lines = _manager_edit_file_lines(root, relative)
    path_exists = file_lines is not None
    span_lines = 0
    if isinstance(start_line, int) and isinstance(end_line, int):
        if not isinstance(start_line, bool) and not isinstance(end_line, bool):
            if start_line >= 1 and end_line >= start_line:
                span_lines = end_line - start_line + 1

    derived: list[dict[str, Any]] = []
    if not path_exists:
        derived.append({
            "path": relative,
            "exception": "new_file",
            "basis": "path_absent_from_canonical_tree",
            "source": "runtime_derivation",
        })
    elif span_lines and file_lines and span_lines * 2 > file_lines:
        derived.append({
            "path": relative,
            "exception": "spans_most_of_file",
            "basis": f"span_covers_a_strict_majority_of_file_lines:{span_lines}/{file_lines}",
            "source": "runtime_derivation",
        })

    declared_rows: list[dict[str, Any]] = []
    if declared:
        if declared not in vocabulary:
            corroborated, corroboration = False, "unknown_exception_code"
        elif declared == "new_file":
            corroborated = not path_exists
            corroboration = "path_presence_in_canonical_tree"
        elif declared == "adapter_without_tools":
            # The seat answered, so the surface is usable and the code is not
            # corroborated -- stated, not silently dropped.
            corroborated = False
            corroboration = "manager_semantic_edit_surface_is_available"
        elif not span_lines or not file_lines:
            corroborated = False
            corroboration = "no_span_supplied_for_a_span_claim"
        else:
            corroborated = span_lines * 2 > file_lines
            corroboration = f"span_lines_vs_file_lines:{span_lines}/{file_lines}"
        declared_rows.append({
            "path": relative,
            "exception": declared,
            "reason": reason_text,
            "source": "manager_declaration",
            "corroborated": bool(corroborated),
            "corroboration": corroboration,
        })

    effective = declared or (derived[0]["exception"] if derived else "")
    record.update({
        "path": relative,
        "reason": reason_text,
        "exception": effective,
        "declared_exception_valid": bool(declared) and declared in vocabulary,
        "path_exists": path_exists,
        "file_lines": file_lines,
        "span_lines": span_lines,
        "derived": derived,
        "declared": declared_rows,
    })
    # The ledger payload is the shape ``verify_audit_ledger`` already reads for
    # a worker declaration: path, exception, reason.  Nothing else crosses.
    recorded = worker_tools._append_audit(
        session.ctx,
        tool=tool,
        ok=True,
        cache_hit=False,
        hit_count=1,
        bytes_returned=0,
        authority_source="canonical",
        authority_state="manager_declared_exception",
        payload={
            "path": relative,
            "exception": effective,
            "reason": reason_text,
        },
    )
    record["recorded"] = bool(recorded)
    if not recorded:
        record["unrecorded_reason"] = "manager_audit_ledger_append_failed"
    return _manager_edit_result(record, manager)


__all__ = [
    "MANAGER_SEMANTIC_EDIT_SCOPE",
    "ai_memory_get",
    "ai_memory_related",
    "ai_memory_search",
    "ai_memory_write",
    "context_graph_event_write",
    "context_graph_range",
    "context_graph_rebuild",
    "context_graph_related",
    "context_graph_search",
    "context_import",
    "kb_get",
    "kb_related",
    "kb_search",
    "kb_write",
    "learning_commit",
    "needfix_markdown_commit",
    "needfix_markdown_preview",
    "semantic_edit_apply",
    "semantic_edit_fallback_record",
    "semantic_edit_policy_exceptions",
    "semantic_edit_prepare",
    "session_current_state",
    "session_write",
    "source_graph_query",
]
