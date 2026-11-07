"""NF-2026-02052: finalization reuses the worker's verified full-snapshot run.

``aiworkhub_worker_validation_run(index="all")`` runs the card's declared
commands on the SAME combined canonical-plus-candidate snapshot the
coordinator's finalizer builds (``run_full_snapshot_validations``), with the
coordinator's own request metadata as authority and route, so the tree
identity it records is the one finalization recomputes.  The result is one
reuse record, HMAC-signed with the request's audit key and written only into
the server-private runtime directory beside the audit ledger -- never the
worktree or HOME.

Finalization loads that record (``load_verified_reuse``) and passes it as
``reuse=`` to ``run_full_snapshot_validations``.  Every doubt -- no record,
bad signature, other request or claim epoch, a record from the sparse
worktree, a red row, a row-count or tree-identity mismatch after late edits --
returns ``None`` here or is refused by ``_reusable_finalizer_rows``, and the
finalizer runs the validations itself exactly as before.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .process_launcher_validation import (
    WORKER_VALIDATION_RUN_REUSE_SOURCE,
    declared_validation_commands,
)
from .request_metadata_limits import REQUEST_METADATA_MAX_BYTES

RECORD_SCHEMA_ID = "aiworkhub.worker_validation_reuse.v1"
RECORD_FILENAME = "worker_validation_reuse.json"
FULL_SNAPSHOT_LANE = "combined_full_snapshot"
MAX_RECORD_BYTES = 4 * 1024 * 1024
# The coordinator's request.json: one shared ceiling (NF-2026-02154).
MAX_METADATA_BYTES = REQUEST_METADATA_MAX_BYTES
MAX_KEY_BYTES = 4096

FullSnapshotRunner = Callable[..., tuple[list[dict[str, Any]], dict[str, Any]]]


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def _read_bounded(path: Path, limit: int) -> bytes | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _read_key(key_path: Path) -> bytes | None:
    key = _read_bounded(key_path, MAX_KEY_BYTES)
    return key if key else None


def record_path(ledger_path: Path) -> Path:
    """The record lives beside the audit ledger: the server-private runtime dir."""

    return Path(ledger_path).parent / RECORD_FILENAME


def sign_record(record: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    return {
        "record": dict(record),
        "hmac_sha256": hmac.new(key, _canonical(record), hashlib.sha256).hexdigest(),
    }


def discard_record(ledger_path: Path) -> None:
    """Drop any earlier record so a newer, unreusable run never leaves a stale one."""

    try:
        record_path(ledger_path).unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def write_record(
    *, key_path: Path, ledger_path: Path, record: Mapping[str, Any]
) -> Path | None:
    """Sign ``record`` and replace the request's record atomically; ``None`` on failure."""

    key = _read_key(Path(key_path))
    if key is None:
        return None
    target = record_path(ledger_path)
    temporary = target.with_name(f".{RECORD_FILENAME}.{os.getpid()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(_canonical(sign_record(record, key)))
        os.replace(temporary, target)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        return None
    return target


def _verified_record(key_path: Path, ledger_path: Path) -> dict[str, Any] | None:
    key = _read_key(key_path)
    raw = _read_bounded(record_path(ledger_path), MAX_RECORD_BYTES)
    if key is None or raw is None:
        return None
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(envelope, dict):
        return None
    record = envelope.get("record")
    signature = envelope.get("hmac_sha256")
    if not isinstance(record, dict) or not isinstance(signature, str):
        return None
    expected = hmac.new(key, _canonical(record), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    return record


def load_verified_reuse(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    """The ``reuse=`` mapping for this exact request and claim epoch, or ``None``.

    Only shape, signature, request, claim epoch and lane are checked here; the
    tree identity, base OID, all-passed flag and row count stay with
    ``_reusable_finalizer_rows``, the one owner of that policy.
    """

    try:
        worker = metadata.get("worker_mcp")
        if not isinstance(worker, Mapping):
            return None
        key_path = Path(str(worker["audit_hmac_key_path"]))
        ledger_path = Path(str(worker["audit_ledger_path"]))
    except (KeyError, TypeError, ValueError):
        return None
    request_id = str(metadata.get("request_id") or "")
    claim_epoch = metadata.get("claim_epoch")
    if not request_id or type(claim_epoch) is not int:
        return None
    record = _verified_record(key_path, ledger_path)
    if record is None:
        return None
    snapshot = record.get("full_validation_snapshot")
    rows = record.get("validations")
    if (
        record.get("schema_id") != RECORD_SCHEMA_ID
        or record.get("request_id") != request_id
        or type(record.get("claim_epoch")) is not int
        or record.get("claim_epoch") != claim_epoch
        or record.get("lane") != FULL_SNAPSHOT_LANE
        or not isinstance(record.get("candidate_bytes_sha256"), str)
        or not isinstance(snapshot, dict)
        or not isinstance(rows, list)
    ):
        return None
    return {
        "full_validation_snapshot": snapshot,
        "validations": rows,
        "reused_from": WORKER_VALIDATION_RUN_REUSE_SOURCE,
    }


def load_request_metadata(
    *,
    request_id: str,
    key_path: Path | None,
    candidates: Sequence[Path],
) -> dict[str, Any] | None:
    """The coordinator's own request metadata, bound to THIS server's audit key.

    The first candidate file whose ``request_id`` matches and whose
    ``worker_mcp.audit_hmac_key_path`` resolves to this server's key wins; any
    other file is ignored, so another request's metadata can never be used.
    """

    if key_path is None or not request_id:
        return None
    try:
        own_key = Path(key_path).resolve()
    except OSError:
        return None
    for candidate in candidates:
        raw = _read_bounded(Path(candidate), MAX_METADATA_BYTES)
        if raw is None:
            continue
        try:
            metadata = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict) or metadata.get("request_id") != request_id:
            continue
        worker = metadata.get("worker_mcp")
        bound = worker.get("audit_hmac_key_path") if isinstance(worker, dict) else None
        try:
            if not bound or Path(str(bound)).resolve() != own_key:
                continue
        except OSError:
            continue
        if not isinstance(metadata.get("workspace"), dict):
            continue
        return metadata
    return None


def metadata_candidates(authority_repo: Path, request_id: str) -> list[Path]:
    """Where the coordinator keeps ``<request_id>.request.json``.

    A request id that is not exactly 32 lowercase hex digits never becomes a
    filename: the result is empty and no metadata is found.
    """

    if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
        return []

    from .process_launcher import PROCESS_DIR_DEFAULT_REL, PROCESS_DIR_ENV

    name = f"{request_id}.request.json"
    paths: list[Path] = []
    override = os.environ.get(PROCESS_DIR_ENV)
    if override:
        paths.append(Path(override) / name)
    paths.append(Path(authority_repo) / PROCESS_DIR_DEFAULT_REL / name)
    return paths


def _default_full_snapshot_runner() -> FullSnapshotRunner:
    from .process_launcher import _run_full_snapshot_validations

    return _run_full_snapshot_validations


def _worker_row(position: int, command: str, row: Mapping[str, Any]) -> dict[str, Any]:
    returncode = row.get("returncode")
    timed_out = bool(row.get("timed_out"))
    if timed_out:
        failure_class = "timed_out"
    elif returncode == 0:
        failure_class = "passed"
    elif returncode is None:
        failure_class = "tool_unavailable"
    else:
        failure_class = "nonzero_exit"
    tail = str(row.get("stdout") or row.get("output") or "")[-2048:]
    stderr = str(row.get("stderr") or "")[-2048:]
    return {
        "index": position,
        "command": command,
        "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
        "resolved": True,
        "returncode": returncode,
        "timed_out": timed_out,
        "failure_class": failure_class,
        "diagnostic_tail": (tail + ("\n" + stderr if stderr else ""))[-2048:],
        "lane": FULL_SNAPSHOT_LANE,
        "unchanged_since_last_run": False,
    }


def _memoized_result(
    *,
    key_path: Path,
    ledger_path: Path,
    metadata: Mapping[str, Any],
    commands: Sequence[str],
    candidate_digest: str,
) -> dict[str, Any] | None:
    """The earlier verified run for these exact candidate bytes, or ``None``."""

    prior = _verified_record(key_path, ledger_path)
    if prior is None:
        return None
    rows = prior.get("validations")
    snapshot = prior.get("full_validation_snapshot")
    claim_epoch = metadata.get("claim_epoch")
    if (
        prior.get("schema_id") != RECORD_SCHEMA_ID
        or prior.get("request_id") != str(metadata.get("request_id") or "")
        or type(claim_epoch) is not int
        or type(prior.get("claim_epoch")) is not int
        or prior.get("claim_epoch") != claim_epoch
        or prior.get("lane") != FULL_SNAPSHOT_LANE
        or prior.get("candidate_bytes_sha256") != candidate_digest
        or not isinstance(rows, list)
        or len(rows) != len(commands)
        or not all(isinstance(row, Mapping) for row in rows)
    ):
        return None
    return {
        "ok": True,
        "results": [
            {
                **_worker_row(position, str(commands[position]), rows[position]),
                "unchanged_since_last_run": True,
            }
            for position in range(len(commands))
        ],
        "tree_identity_sha256": (
            snapshot.get("tree_identity_sha256") if isinstance(snapshot, dict) else None
        ),
        "reuse_record_written": True,
        "memoized": True,
    }


def run_worker_full_snapshot(
    *,
    metadata: Mapping[str, Any],
    commands: Sequence[str],
    candidate_digest: str,
    key_path: Path,
    ledger_path: Path,
    run_full_snapshot: FullSnapshotRunner | None = None,
    enforce_scope: Callable[[Any], list[str]] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run every declared command on the combined full snapshot and seal the record.

    Returns ``{"ok": True, "results": [...], "record_path": ...}`` on a run, or
    ``{"ok": False, "reason": ...}`` naming why the full-snapshot lane could
    not run -- the caller decides what to do then; nothing here falls back.
    Unless ``force``, an earlier verified record for the same request, claim
    epoch and candidate bytes is returned as-is (``memoized``) without rerunning.
    """

    from . import worker_workspace

    if not force:
        memo = _memoized_result(
            key_path=key_path,
            ledger_path=ledger_path,
            metadata=metadata,
            commands=commands,
            candidate_digest=candidate_digest,
        )
        if memo is not None:
            return memo
    discard_record(ledger_path)
    try:
        declared = declared_validation_commands(metadata)
    except worker_workspace.WorkspaceError as exc:
        return {
            "ok": False,
            "reason": f"metadata_validation_invalid:{type(exc).__name__}",
        }
    if list(declared) != [str(c) for c in commands]:
        return {"ok": False, "reason": "metadata_validation_commands_mismatch"}
    claim_epoch = metadata.get("claim_epoch")
    if type(claim_epoch) is not int:
        return {"ok": False, "reason": "metadata_claim_epoch_invalid"}
    try:
        workspace = worker_workspace.WorkerWorkspace.from_metadata(
            dict(metadata["workspace"])
        )
        scope = enforce_scope or worker_workspace.enforce_scope
        candidate = list(scope(workspace))
    except (KeyError, TypeError, ValueError, OSError) as exc:
        return {"ok": False, "reason": f"workspace_unavailable:{type(exc).__name__}"}
    except worker_workspace.WorkspaceError as exc:
        return {"ok": False, "reason": f"workspace_unavailable:{type(exc).__name__}"}
    if not candidate:
        return {"ok": False, "reason": "no_candidate_changes"}
    runner = run_full_snapshot or _default_full_snapshot_runner()
    snapshot: dict[str, Any] | None
    try:
        rows, snapshot = runner(workspace, metadata, metadata, candidate)
    except worker_workspace.ValidationRunError as exc:
        rows, snapshot = list(exc.results or []), None
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"full_snapshot_failed:{type(exc).__name__}"}
    results = [
        _worker_row(position, str(commands[position]), rows[position])
        if position < len(rows) and isinstance(rows[position], Mapping)
        else {
            "index": position,
            "command": str(commands[position]),
            "returncode": None,
            "failure_class": "tool_unavailable",
            "reason": "full_snapshot_returned_no_row",
            "lane": FULL_SNAPSHOT_LANE,
        }
        for position in range(len(commands))
    ]
    written: Path | None = None
    if snapshot is not None and snapshot.get("tree_identity_sha256"):
        written = write_record(
            key_path=key_path,
            ledger_path=ledger_path,
            record={
                "schema_id": RECORD_SCHEMA_ID,
                "request_id": str(metadata.get("request_id") or ""),
                "claim_epoch": claim_epoch,
                "lane": FULL_SNAPSHOT_LANE,
                "candidate_bytes_sha256": candidate_digest,
                "full_validation_snapshot": dict(snapshot),
                "validations": [dict(row) for row in rows],
            },
        )
    return {
        "ok": True,
        "results": results,
        "tree_identity_sha256": (snapshot or {}).get("tree_identity_sha256"),
        "reuse_record_written": written is not None,
    }


def run_for_worker_context(
    ctx: Any,
    commands: Sequence[str],
    candidate_digest: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """The worker server's validation_run(all) lane on the combined full snapshot.

    ``ctx`` is the server's ``WorkerToolContext``.  Without a server-private
    runtime directory, or without coordinator metadata bound to this server's
    audit key, the lane does not run and says why (``ok`` False).
    """

    key_path = getattr(ctx, "audit_hmac_key_path", None)
    ledger_path = getattr(ctx, "audit_ledger_path", None)
    request_id = str(getattr(ctx, "request_id", "") or "")
    if key_path is None or ledger_path is None:
        return {"ok": False, "reason": "no_server_private_runtime_dir"}
    metadata = load_request_metadata(
        request_id=request_id,
        key_path=key_path,
        candidates=metadata_candidates(Path(ctx.authority_repo), request_id),
    )
    if metadata is None:
        discard_record(ledger_path)
        return {"ok": False, "reason": "coordinator_request_metadata_unavailable"}
    return run_worker_full_snapshot(
        metadata=metadata,
        commands=commands,
        candidate_digest=candidate_digest,
        key_path=Path(key_path),
        ledger_path=Path(ledger_path),
        force=bool(force),
    )
