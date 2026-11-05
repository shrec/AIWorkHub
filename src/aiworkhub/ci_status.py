"""Base-commit CI status for review packets and accept flows (NF-2026-02080).

Resolves the latest GitHub Actions run *per workflow* for a commit via
``gh run list --commit <sha> --json ...`` with a short timeout. The data is
per workflow run (not per job). Never raises: any failure (gh missing,
unauthenticated, offline, no GitHub remote, bad json, timeout, undecodable
output) yields an ``unavailable`` section with a reason code instead, and
nothing here ever blocks an accept -- red or stuck bases only add warnings.

Only a fully settled result (at least one run, every run ``completed``) is
cached per sha; queued/in-progress/empty/unavailable answers are re-fetched.
"""

from __future__ import annotations

import calendar
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

CI_STATUS_SCHEMA_ID = "aiworkhub.ci_status.base_ci.v1"

_GH_TIMEOUT_SECONDS = 10
_GIT_TIMEOUT_SECONDS = 5
_IN_PROGRESS_AGE_WARN_SECONDS = 2 * 60 * 60
_MAX_RUNS = 20
_CACHE_MAX_ENTRIES = 64
_TEXT_MAX = 300
_SHA_RE = re.compile(r"[0-9a-fA-F]{7,64}")

# One definition each of what counts as red and as not-yet-finished.
RED_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "startup_failure", "action_required", "cancelled"}
)
PENDING_STATUSES = frozenset(
    {"in_progress", "queued", "waiting", "pending", "requested"}
)

# Only settled results land here; insertion order doubles as eviction order.
_cache: dict[str, dict[str, Any]] = {}


def _unavailable(reason: str, sha: str = "") -> dict[str, Any]:
    return {
        "schema_id": CI_STATUS_SCHEMA_ID,
        "state": "unavailable",
        "available": False,
        "reason": reason,
        "sha": sha,
        "runs": [],
    }


def _text(value: Any) -> str:
    return str(value if value is not None else "")[:_TEXT_MAX]


def _run_gh(repo_root: Any, sha: str) -> tuple[int, str, str]:
    """Run ``gh run list`` for ``sha``; returns (returncode, stdout, stderr).

    Negative return codes are local failures: -1 gh missing, -2 timeout,
    -3 other OS error.
    """
    try:
        proc = subprocess.run(
            [
                "gh",
                "run",
                "list",
                "--commit",
                sha,
                "--limit",
                "50",
                "--json",
                "workflowName,name,status,conclusion,url,createdAt,headSha",
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GH_TIMEOUT_SECONDS,
            check=False,
            cwd=str(repo_root) if repo_root else None,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return -1, "", "gh_not_found"
    except subprocess.TimeoutExpired:
        return -2, "", "gh_timeout"
    except (OSError, subprocess.SubprocessError, ValueError):
        return -3, "", "gh_os_error"


def _resolve_sha(repo_root: Any, base_ref: str) -> str:
    """Full commit sha for ``base_ref`` in ``repo_root``; '' when unresolvable."""
    ref = str(base_ref or "").strip()
    if not ref or ref.startswith("-"):
        return ""
    if _SHA_RE.fullmatch(ref):
        return ref.lower()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            cwd=str(repo_root) if repo_root else None,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    out = (proc.stdout or "").strip()
    return out.lower() if proc.returncode == 0 and _SHA_RE.fullmatch(out) else ""


def _age_seconds(created_at: str) -> float | None:
    """Seconds since an ISO-8601 UTC GitHub timestamp (``...Z``); None if bad."""
    if not created_at:
        return None
    try:
        epoch = calendar.timegm(time.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ"))
        return max(0.0, time.time() - epoch)
    except (ValueError, OverflowError, TypeError):
        return None


def _failure_reason(stderr: str) -> str:
    lowered = (stderr or "").lower()
    if "auth" in lowered or "login" in lowered or "logged in" in lowered:
        return "gh_unauthenticated"
    if (
        "remote" in lowered
        or "repository" in lowered
        or "could not determine" in lowered
    ):
        return "no_github_remote"
    return "gh_run_failed"


def _latest_per_workflow(rows: list[Any]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        workflow = _text(row.get("workflowName") or row.get("name"))
        created_at = _text(row.get("createdAt"))
        existing = latest.get(workflow)
        if existing is not None and existing["created_at"] >= created_at:
            continue
        latest[workflow] = {
            "workflow": workflow,
            "status": _text(row.get("status")),
            "conclusion": _text(row.get("conclusion")),
            "url": _text(row.get("url")),
            "created_at": created_at,
            "age_seconds": _age_seconds(created_at),
        }
    return sorted(latest.values(), key=lambda run: run["workflow"])[:_MAX_RUNS]


def _settled(runs: list[dict[str, Any]]) -> bool:
    return bool(runs) and all(run["status"] == "completed" for run in runs)


def _fetch(repo_root: Any, base_ref: str, force: bool) -> dict[str, Any]:
    sha = _resolve_sha(repo_root, base_ref)
    if not sha:
        return _unavailable("no_commit_sha")
    if not force and sha in _cache:
        return _cache[sha]
    returncode, stdout, stderr = _run_gh(repo_root, sha)
    local_failures = {-1: "gh_not_found", -2: "gh_timeout", -3: "gh_os_error"}
    if returncode in local_failures:
        return _unavailable(local_failures[returncode], sha)
    if returncode != 0:
        return _unavailable(_failure_reason(stderr), sha)
    try:
        parsed = json.loads(stdout or "[]")
    except (json.JSONDecodeError, TypeError, ValueError):
        parsed = None
    if not isinstance(parsed, list):
        return _unavailable("gh_bad_json", sha)
    runs = _latest_per_workflow(parsed)
    result = {
        "schema_id": CI_STATUS_SCHEMA_ID,
        "state": "ok",
        "available": True,
        "reason": "",
        "sha": sha,
        "runs": runs,
    }
    if _settled(runs):
        while len(_cache) >= _CACHE_MAX_ENTRIES:
            _cache.pop(next(iter(_cache)))
        _cache[sha] = result
    return result


def fetch_base_ci_status(
    repo_root: Any, base_ref: str = "HEAD", *, force: bool = False
) -> dict[str, Any]:
    """The base_ci section for ``base_ref`` (default HEAD) in ``repo_root``.

    Never raises; any unexpected failure becomes ``state: unavailable`` with
    the exception type as reason. ``force=True`` bypasses the cache.
    """
    try:
        result = _fetch(repo_root, str(base_ref or "HEAD"), force)
        return {**result, "runs": [dict(run) for run in result["runs"]]}
    except Exception as exc:  # noqa: BLE001 -- the contract is "never raises"
        return _unavailable(type(exc).__name__)


def base_ci_warnings(base_ci: Any) -> list[dict[str, Any]]:
    """Non-blocking warnings for a red base or a long-pending workflow run.

    Empty when ``base_ci`` is unavailable or every run is clean/young. Never
    raises.
    """
    warnings: list[dict[str, Any]] = []
    try:
        if not isinstance(base_ci, dict) or not base_ci.get("available"):
            return warnings
        for run in base_ci.get("runs") or []:
            if not isinstance(run, dict):
                continue
            conclusion = str(run.get("conclusion") or "")
            status = str(run.get("status") or "")
            workflow = str(run.get("workflow") or "")
            age = run.get("age_seconds")
            if conclusion in RED_CONCLUSIONS:
                warnings.append(
                    {
                        "kind": "base_ci_failure",
                        "workflow": workflow,
                        "conclusion": conclusion,
                        "message": (
                            f"Base commit CI workflow {workflow} concluded {conclusion}"
                        ),
                    }
                )
            elif (
                status in PENDING_STATUSES
                and isinstance(age, (int, float))
                and age > _IN_PROGRESS_AGE_WARN_SECONDS
            ):
                warnings.append(
                    {
                        "kind": "base_ci_stuck",
                        "workflow": workflow,
                        "status": status,
                        "message": (
                            f"Base commit CI workflow {workflow} has been "
                            f"{status} for over {int(age // 3600)}h"
                        ),
                    }
                )
    except Exception:  # noqa: BLE001 -- advisory data must never raise
        return warnings
    return warnings


def base_ci_section(repo_root: Any, base_ref: str = "HEAD") -> dict[str, Any]:
    """``fetch_base_ci_status`` plus its ``warnings``; advisory, never raises."""
    try:
        section = fetch_base_ci_status(
            Path(repo_root) if repo_root else repo_root, base_ref
        )
        return {**section, "warnings": base_ci_warnings(section)}
    except Exception as exc:  # noqa: BLE001 -- the contract is "never raises"
        return {**_unavailable(type(exc).__name__), "warnings": []}


def clear_cache() -> None:
    """Test seam: clear the per-sha cache."""
    _cache.clear()
