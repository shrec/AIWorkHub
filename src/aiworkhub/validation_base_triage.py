"""NF-2026-01923: run a failing declared validation on the card's base commit.

``validation_auto_rework`` (NF-01922) returned every red declared validation to
the same worker, even a command that fails identically on the commit the card
started from: the worker then loops on a failure it did not cause. This module
answers one question before that return: does the failing command also fail on
``base_oid``?

* ``base_failed``     -- every failing command fails on the base too: the
  failure is environmental (or pre-existing), never the worker's to fix.
* ``base_passed``     -- at least one failing command passes on the base: the
  candidate broke it, the NF-01922 auto-return applies unchanged.
* ``base_unrunnable`` -- the base could not be judged (no base_oid, the
  workspace could not be created, the receipt is unrunnable, anything raised).
  Unknown is never classified as environment; the reason is carried along.

The base workspace follows ``finding_reproduction.manager_reproducer``: a
scratch workspace pinned to ``base_oid`` with an empty write scope, the failing
commands run through the launcher's own receipt runner, and the workspace is
cleaned up in ``finally``. ``process_launcher`` is imported lazily, at call
time, and is never modified.

Judged results (failed/passed) are cached per (command, base tree, adapter)
under the repository's gitignored ``.aiworkhub/runtime/cache/validation_base_runs``
with a TTL and an entry cap; an expired or malformed entry is deleted on read.
An unrunnable result is never cached: an error state is re-checked.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import validation_failed_packet
from .worker_workspace import ValidationRunError, WorkspaceError

SCHEMA_ID = "aiworkhub.validation_base_triage.v1"
# v2: entries carry base_failed_node_ids; v1 entries (no ids) are misses, deleted on read.
CACHE_SCHEMA_ID = "aiworkhub.validation_base_run_cache.v2"
_NEW_FAILURES_MAX = 20
BASE_FAILED = "base_failed"
BASE_PASSED = "base_passed"
BASE_UNRUNNABLE = "base_unrunnable"
OUTCOMES = (BASE_FAILED, BASE_PASSED, BASE_UNRUNNABLE)
CACHE_PARTS = (".aiworkhub", "runtime", "cache", "validation_base_runs")
CACHE_TTL_SECONDS = 6 * 60 * 60
CACHE_MAX_ENTRIES = 256
TAIL_MAX_BYTES = validation_failed_packet._TAIL_MAX_BYTES
_REASON_MAX = 200
_GIT_TIMEOUT_SECONDS = 60
_TREE_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_OID_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")


def cache_dir(repo: str | Path) -> Path:
    """The in-repo cache directory; never a system temp or home location."""
    return Path(repo).joinpath(*CACHE_PARTS)


def cache_key(command: str, base_tree: str, adapter_id: str) -> str:
    payload = json.dumps([str(command), str(base_tree), str(adapter_id)], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _last_bytes(text: str, limit: int = TAIL_MAX_BYTES) -> str:
    return validation_failed_packet._last_bytes(str(text or ""), limit)


def _entry_path(repo: Path, key: str) -> Path:
    return cache_dir(repo) / f"{key}.json"


def _cache_read(repo: Path, key: str, now: float) -> dict[str, Any] | None:
    path = _entry_path(repo, key)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        entry = json.loads(raw)
    except ValueError:
        entry = None
    valid = (
        isinstance(entry, dict)
        and entry.get("schema_id") == CACHE_SCHEMA_ID
        and entry.get("key") == key
        and entry.get("outcome") in (BASE_FAILED, BASE_PASSED)
        and isinstance(entry.get("expires_at"), (int, float))
        and float(entry["expires_at"]) > now
    )
    if not valid:
        # Expired or malformed: deleted on read, never served.
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return entry


def _prune(directory: Path, now: float) -> None:
    """Delete expired entries, then the oldest beyond CACHE_MAX_ENTRIES."""
    live: list[tuple[float, Path]] = []
    for path in directory.glob("*.json"):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            expires = float(entry.get("expires_at"))
        except (OSError, ValueError, TypeError, AttributeError):
            expires = 0.0
        if expires <= now:
            try:
                path.unlink()
            except OSError:
                pass
        else:
            live.append((expires, path))
    live.sort()
    for _expires, path in live[: max(0, len(live) - CACHE_MAX_ENTRIES)]:
        try:
            path.unlink()
        except OSError:
            pass


def _cache_write(repo: Path, key: str, entry: Mapping[str, Any], now: float) -> None:
    directory = cache_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        **dict(entry),
        "schema_id": CACHE_SCHEMA_ID,
        "key": key,
        "written_at": now,
        "expires_at": now + CACHE_TTL_SECONDS,
    }
    final = _entry_path(repo, key)
    scratch = directory / f".{key}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        scratch.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(scratch, final)
    finally:
        if scratch.exists():
            try:
                scratch.unlink()
            except OSError:
                pass
    _prune(directory, now)


def base_tree(repo: str | Path, base_oid: str) -> str:
    """The tree hash of ``base_oid``; raises when git cannot resolve it."""
    if not _OID_RE.match(str(base_oid or "")):
        raise ValueError("base_oid_invalid")
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{base_oid}^{{tree}}"],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )
    tree = (completed.stdout or "").strip()
    if completed.returncode != 0 or not _TREE_RE.match(tree):
        raise LookupError("base_tree_unresolved")
    return tree


def _process_launcher() -> Any:
    # Re-bound at call time, as in finding_reproduction.manager_reproducer.
    from . import process_launcher as _pl

    return _pl


def _row_tail(row: Mapping[str, Any]) -> str:
    stderr_tail = str(row.get("stderr_tail") or "")
    stdout_tail = str(row.get("stdout_tail") or "")
    return _last_bytes("\n".join(part for part in (stderr_tail, stdout_tail) if part))


def _row_outcome(row: Any) -> tuple[str, str]:
    """(outcome, reason) for one receipt row of the base run."""
    if not isinstance(row, Mapping):
        return BASE_UNRUNNABLE, "receipt_row_missing"
    if row.get("timed_out"):
        return BASE_UNRUNNABLE, "receipt_timed_out"
    for key in ("validation_environment_disposition", "environment_blocked", "restriction"):
        if row.get(key):
            return BASE_UNRUNNABLE, f"receipt_{key}"
    returncode = row.get("returncode")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        return BASE_UNRUNNABLE, "receipt_returncode_missing"
    return (BASE_PASSED, "") if returncode == 0 else (BASE_FAILED, "")


def run_base(
    repo: Path, card: Mapping[str, Any], adapter_id: str, base_oid: str, commands: Sequence[str]
) -> list[dict[str, Any]]:
    """Run ``commands`` in a scratch workspace pinned to ``base_oid``."""
    _pl = _process_launcher()
    base_card = {key: value for key, value in dict(card).items() if key != "rework_predecessor"}
    tag = uuid.uuid4().hex[:16]
    workspace = dataclasses.replace(
        _pl.create_workspace(repo, f"vbase_{tag}", base_card, adapter_id, pinned_base_oid=base_oid),
        allowed_writes=(),
    )
    try:
        try:
            rows = _pl._run_validations_with_toolchain_receipt(
                workspace,
                list(commands),
                {},
                **_pl._validation_route_kwargs({"adapter_id": adapter_id}),
            )
        except ValidationRunError as exc:
            # A failing command RAISES with its rows retained (as in
            # process_launcher_validation); those rows are exactly what is judged.
            # ValidationEnvironmentBlocked rows keep their restriction keys, so
            # _row_outcome still reads them as unrunnable.
            rows = exc.results
    finally:
        _pl.cleanup_workspace(workspace.repo, workspace.path, workspace.home)
    return [dict(row) if isinstance(row, Mapping) else {} for row in (rows or [])]


def _unrunnable(base: dict[str, Any], reason: str) -> dict[str, Any]:
    return {**base, "outcome": BASE_UNRUNNABLE, "reason": str(reason)[:_REASON_MAX]}


def failed_node_ids(row: Any) -> frozenset[str] | None:
    """The strict pytest failure identity of one row, or None when it cannot be parsed."""
    # ponytail: identity is pytest-only; a non-pytest command never parses, so it always returns to the worker.
    from .process_launcher_validation import pytest_failed_node_ids

    if not isinstance(row, Mapping):
        return None
    try:
        return pytest_failed_node_ids(row)
    except WorkspaceError:
        return None


def _judge(result: dict[str, Any], candidate_row: Any) -> dict[str, Any]:
    """Compare a raw base_failed result's ids with the candidate's; adds the verdict."""
    if result["outcome"] != BASE_FAILED:
        return result
    stored = result.pop("base_failed_node_ids", None)
    base_ids = frozenset(str(i) for i in stored) if isinstance(stored, list) else None
    candidate_ids = failed_node_ids(candidate_row)
    if not base_ids or not candidate_ids:
        return {**result, "outcome": BASE_UNRUNNABLE, "reason": "failure_identity_unparsed"}
    new = sorted(candidate_ids - base_ids)
    if new:
        return {**result, "outcome": BASE_PASSED, "reason": "candidate_new_failures",
                "new_failures": new[:_NEW_FAILURES_MAX]}
    return result


def triage(
    repo: str | Path,
    *,
    card: Mapping[str, Any],
    base_oid: str,
    adapter_id: str,
    commands: Sequence[str],
    candidate_rows: Mapping[str, Mapping[str, Any]] | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Classify the failing ``commands`` by their base-commit result. Never raises.

    ``candidate_rows`` maps each command to the candidate's terminal validation
    row; a base failure counts as "identical" only when the candidate's failing
    pytest node ids are a subset of the base's.
    """
    base: dict[str, Any] = {
        "schema_id": SCHEMA_ID,
        "base_oid": str(base_oid or "") or None,
        "base_tree": None,
        "adapter_id": str(adapter_id or ""),
        "commands": [],
        "cache_hits": 0,
        "base_runs": 0,
    }
    try:
        return _triage(Path(repo), base, card, str(base_oid or ""), str(adapter_id or ""),
                       [str(c) for c in commands if c], dict(candidate_rows or {}), now)
    except Exception as exc:  # noqa: BLE001 -- unknown is base_unrunnable, never environment
        return _unrunnable(base, type(exc).__name__)


def _triage(
    repo: Path, base: dict[str, Any], card: Mapping[str, Any], base_oid: str, adapter_id: str,
    commands: list[str], candidate_rows: Mapping[str, Any], now: Callable[[], float],
) -> dict[str, Any]:
    if not commands:
        return _unrunnable(base, "no_failing_command")
    if not base_oid:
        return _unrunnable(base, "base_oid_missing")
    if not adapter_id:
        return _unrunnable(base, "adapter_id_missing")
    tree = base_tree(repo, base_oid)
    base["base_tree"] = tree
    results: dict[str, dict[str, Any]] = {}
    pending: list[str] = []
    for command in dict.fromkeys(commands):
        entry = _cache_read(repo, cache_key(command, tree, adapter_id), now())
        if entry is None:
            pending.append(command)
        else:
            base["cache_hits"] += 1
            results[command] = {
                "command": command,
                "outcome": entry["outcome"],
                "base_returncode": entry.get("base_returncode"),
                "base_tail": _last_bytes(str(entry.get("base_tail") or "")),
                "base_failed_node_ids": entry.get("base_failed_node_ids"),
                "cached": True,
            }
    if pending:
        base["base_runs"] = 1
        try:
            rows = run_base(repo, card, adapter_id, base_oid, pending)
        except Exception as exc:  # noqa: BLE001 -- create/run failures are unrunnable
            return _unrunnable(base, f"base_run_raised:{type(exc).__name__}")
        for index, command in enumerate(pending):
            row = rows[index] if index < len(rows) else None
            outcome, reason = _row_outcome(row)
            ids = failed_node_ids(row) if outcome == BASE_FAILED else None
            result: dict[str, Any] = {
                "command": command,
                "outcome": outcome,
                "base_returncode": row.get("returncode") if isinstance(row, Mapping) else None,
                "base_tail": _row_tail(row) if isinstance(row, Mapping) else "",
                "base_failed_node_ids": sorted(ids) if ids else None,
                "cached": False,
            }
            if reason:
                result["reason"] = reason
            results[command] = result
            if outcome != BASE_UNRUNNABLE:
                cached_entry: dict[str, Any] = {
                    "outcome": outcome, "command": command, "base_tree": tree,
                    "adapter_id": adapter_id, "base_returncode": result["base_returncode"],
                    "base_tail": result["base_tail"],
                }
                if ids:
                    cached_entry["base_failed_node_ids"] = sorted(ids)
                try:
                    _cache_write(repo, cache_key(command, tree, adapter_id), cached_entry, now())
                except OSError:
                    pass  # an unwritable cache only costs a re-run next time
    ordered = [
        _judge(results[command], candidate_rows.get(command)) for command in dict.fromkeys(commands)
    ]
    for entry in ordered:
        entry.pop("base_failed_node_ids", None)
    base["commands"] = ordered
    outcomes = {entry["outcome"] for entry in ordered}
    if BASE_PASSED in outcomes:
        reason = next((str(e.get("reason") or "") for e in ordered if e["outcome"] == BASE_PASSED), "")
        return {**base, "outcome": BASE_PASSED, "reason": reason}
    if outcomes == {BASE_FAILED}:
        return {**base, "outcome": BASE_FAILED, "reason": ""}
    reason = next((str(e.get("reason") or "") for e in ordered if e["outcome"] == BASE_UNRUNNABLE), "")
    return _unrunnable(base, reason or "receipt_unrunnable")


def needfix_dedupe_identity(command: str, base_tree_hash: str) -> tuple[str, str]:
    """(title, description) that depend only on (command, base tree): the store's dedupe key.

    The command is shown truncated, so the sha256 of the FULL command keeps two
    long commands that share a prefix on separate NeedFixes.
    """
    full = str(command)
    short = full[:120]
    digest = hashlib.sha256(full.encode("utf-8")).hexdigest()[:12]
    title = f"Declared validation fails on the base commit: {short} [cmd {digest}]"
    description = (
        "A card's declared validation failed on the candidate AND identically on the card's "
        "base commit, so it was classified as environment (NF-2026-01923) and not returned "
        f"to the worker.\ncommand: {short} [cmd {digest}]\nbase_tree: {base_tree_hash}\n"
        "Fix the baseline or the validation environment; the card cannot pass until then."
    )
    return title, description
