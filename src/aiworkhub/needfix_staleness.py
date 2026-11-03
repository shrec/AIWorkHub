"""Read-only NeedFix staleness sweep (NF-2026-01990).

Proposes a disposition, with evidence, for every ``captured`` NeedFix row so a
manager can batch-triage the backlog through the existing transition tools.
This module never writes: no NeedFix transition, update or archive and no file
writes. Commit-trailer auto-resolve lives in
``needfix_ingest._reconcile_commit_trailers_on_read`` and is not duplicated
here; a mention found by this sweep is only ``possibly_resolved`` evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping, Sequence

from aiworkhub import needfix_store
from aiworkhub.task_engine import _parse_iso_timestamp as _parse_ts

SCHEMA_ID = "aiworkhub.needfix_staleness_sweep.v1"

DISPOSITIONS: tuple[str, ...] = (
    "archive_obsolete",
    "possibly_resolved",
    "needs_triage",
    "keep",
)

# Severities that must not wait in the captured inbox without a decision.
TRIAGE_SEVERITIES: frozenset[str] = frozenset(("critical", "high"))

MAX_MENTIONING_COMMITS = 5
GIT_LOG_TIMEOUT_SECONDS = 60

# Unanchored form of the store's own id pattern (single definition).
_NF_ID_IN_TEXT_RE = re.compile(
    r"\b" + needfix_store.NF_ID_RE.pattern.lstrip("^").rstrip("$") + r"\b"
)

_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"


def _captured_rows(repo_root: Path) -> list[dict[str, Any]]:
    """Every ``captured`` row, paged until exhausted (archived never match)."""
    rows: list[dict[str, Any]] = []
    limit = needfix_store.MAX_LIST_LIMIT
    offset = 0
    while True:
        page = needfix_store.list_needfix(
            repo_root,
            status="captured",
            limit=limit,
            offset=offset,
            order_by="created_at",
            order_dir="ASC",
        )
        rows.extend(page)
        if len(page) < limit:
            return rows
        offset += limit


def _git_commits_since(
    repo_root: Path, since: datetime
) -> tuple[list[tuple[str, datetime, frozenset[str]]], bool]:
    """Return ``(commits, git_unavailable)``; commits are newest first.

    Exactly one git process. Any failure (missing git, not a repository,
    timeout, non-zero exit) is reported as unavailable and never raised.
    """
    since_arg = (since.astimezone(timezone.utc) - timedelta(seconds=1)).strftime(
        "%Y-%m-%d %H:%M:%S +0000"
    )
    env = dict(os.environ)
    # The repo root itself must be the repository; never discover a parent one.
    env["GIT_CEILING_DIRECTORIES"] = str(repo_root.resolve().parent)
    try:
        result = subprocess.run(
            [
                "git", "-C", str(repo_root), "log",
                "--format=%H%x1f%cI%x1f%B%x1e", "HEAD", f"--since={since_arg}",
            ],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_LOG_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return [], True
    if result.returncode != 0:
        return [], True
    commits: list[tuple[str, datetime, frozenset[str]]] = []
    for record in result.stdout.split(_RECORD_SEP):
        record = record.lstrip("\r\n")
        if not record:
            continue
        sha, _, rest = record.partition(_FIELD_SEP)
        date_text, _, message = rest.partition(_FIELD_SEP)
        committed = _parse_ts(date_text)
        sha = sha.strip()
        if not sha or committed is None:
            continue
        ids = frozenset(_NF_ID_IN_TEXT_RE.findall(message))
        if ids:
            commits.append((sha, committed, ids))
    commits.sort(key=lambda item: item[1], reverse=True)
    return commits, False


def _is_outside_repo(path_text: str) -> bool:
    if not path_text:
        return True
    if PurePosixPath(path_text).is_absolute() or PureWindowsPath(path_text).anchor:
        return True
    parts = PureWindowsPath(path_text).parts
    return ".." in parts


def _missing_scope_files(repo_root: Path, scope_files: Sequence[Any]) -> list[str]:
    missing: list[str] = []
    for entry in scope_files:
        text = str(entry or "").strip()
        # Absolute or '..' paths are never stat'ed outside the repository.
        if _is_outside_repo(text) or not (repo_root / text).exists():
            missing.append(str(entry))
    return missing


def _disposition(
    scope_files: Sequence[Any],
    missing: Sequence[str],
    mentions: Sequence[str],
    severity: str,
) -> tuple[str, list[str]]:
    if scope_files and len(missing) == len(scope_files):
        return "archive_obsolete", [
            f"all {len(scope_files)} scope_files missing under repo root"
        ]
    if mentions:
        return "possibly_resolved", [
            f"named in {len(mentions)} commit(s) after capture: {mentions[0][:12]}"
        ]
    if severity in TRIAGE_SEVERITIES:
        return "needs_triage", [f"severity {severity} with no staleness evidence"]
    reasons = ["no staleness evidence"]
    if missing:
        reasons.append(f"{len(missing)} of {len(scope_files)} scope_files missing")
    return "keep", reasons


def _sort_key(row: Mapping[str, Any]) -> tuple[int, int, int, str]:
    severities = needfix_store.SEVERITIES
    severity = row["severity"]
    sev_rank = severities.index(severity) if severity in severities else len(severities)
    return (
        DISPOSITIONS.index(row["disposition"]),
        sev_rank,
        -int(row["age_days"]),
        str(row["id"]),
    )


def sweep(repo_root: str | Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Read-only disposition proposal for every captured NeedFix row."""
    # Sequential by design: one git process plus path stats on ~300 rows.
    root = Path(repo_root)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    captured = _captured_rows(root)
    created_by_id = {row["id"]: _parse_ts(row.get("created_at")) for row in captured}
    known_created = [ts for ts in created_by_id.values() if ts is not None]

    commits: list[tuple[str, datetime, frozenset[str]]] = []
    git_unavailable = False
    if known_created:
        commits, git_unavailable = _git_commits_since(root, min(known_created))

    out_rows: list[dict[str, Any]] = []
    for row in captured:
        created = created_by_id[row["id"]]
        mentions: list[str] = []
        if created is not None:
            for sha, committed, ids in commits:
                if len(mentions) >= MAX_MENTIONING_COMMITS:
                    break
                if row["id"] in ids and committed > created:
                    mentions.append(sha)
        scope_files = list(row.get("scope_files") or [])
        missing = _missing_scope_files(root, scope_files)
        severity = str(row.get("severity") or "")
        disposition, reasons = _disposition(scope_files, missing, mentions, severity)
        if created is None:
            reasons.append("created_at unparseable; age and commit evidence unknown")
        age_days = max(0, (current - created).days) if created is not None else 0
        out_rows.append(
            {
                "id": row["id"],
                "title": row.get("title"),
                "severity": severity,
                "created_at": row.get("created_at"),
                "age_days": int(age_days),
                "scope_files_missing": missing,
                "mentioning_commits": mentions,
                "disposition": disposition,
                "reasons": reasons,
            }
        )
    out_rows.sort(key=_sort_key)
    counts = {name: 0 for name in DISPOSITIONS}
    for item in out_rows:
        counts[item["disposition"]] += 1
    return {
        "schema_id": SCHEMA_ID,
        "generated_at": current.isoformat(),
        "captured_total": len(out_rows),
        "counts": counts,
        "git_unavailable": git_unavailable,
        "rows": out_rows,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aiworkhub.needfix_staleness",
        description="Read-only NeedFix staleness sweep (prints JSON).",
    )
    parser.add_argument("--repo", default=".", help="repository root (default: cwd)")
    parser.add_argument("--disposition", choices=DISPOSITIONS, default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be >= 0")
    report = sweep(args.repo)
    rows = report["rows"]
    if args.disposition is not None:
        rows = [row for row in rows if row["disposition"] == args.disposition]
    if args.limit is not None:
        rows = rows[: args.limit]
    report["rows"] = rows
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
