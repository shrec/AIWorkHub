"""Read-only "likely fixed" detector for open NeedFix rows.

Runs ONE ``git log --name-only`` pass since the oldest open row's
``created_at`` and scores each open row by whether commits after its own
``created_at`` touched its ``scope_files`` or named its id in the commit
message. This never writes the NeedFix store and never claims a row is
fixed: the output is a ranked candidate list for a human to verify and
resolve (``resolve_verified``).

Usage::

    python -m aiworkhub.needfix_likely_fixed --repo <root> [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aiworkhub import needfix_store

SIGNAL_ORDER = (
    "named_in_commit",
    "all_scope_files_touched",
    "some_scope_files_touched",
    "no_signal",
)

_PAGE_SIZE = 500
_COMMIT_MARK = "COMMIT"
_SEP = "\x1f"
_RECORD_SEP = "\x1e"
_GIT_LOG_FORMAT = f"{_COMMIT_MARK}{_SEP}%H{_SEP}%cI{_SEP}%B{_RECORD_SEP}"


def _parse_dt(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        from datetime import timezone

        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _id_named_in_text(row_id: str, text: str) -> bool:
    """Whole-token match: avoid NF-2026-0001 matching NF-2026-00010."""
    pattern = r"(?<![0-9A-Za-z-])" + re.escape(row_id) + r"(?![0-9A-Za-z-])"
    return re.search(pattern, text) is not None


def _open_rows(repo_root: Path) -> list[dict[str, Any]]:
    """Read every open (non-terminal) NeedFix row via the public read API."""
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = needfix_store.list_needfix(
            repo_root,
            limit=_PAGE_SIZE,
            offset=offset,
            order_by="created_at",
            order_dir="ASC",
        )
        if not page:
            break
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return [r for r in rows if r["status"] not in needfix_store.NEEDFIX_TERMINAL_STATUSES]


def _parse_git_log(output: str) -> list[dict[str, Any]]:
    commits: list[dict[str, Any]] = []
    prefix = _COMMIT_MARK + _SEP
    pieces = output.split(_RECORD_SEP)
    header: str | None = None
    for index, piece in enumerate(pieces):
        if index == 0:
            header = piece if piece.startswith(prefix) else None
            continue
        if prefix in piece:
            files_part, _, rest = piece.partition(prefix)
            next_header: str | None = prefix + rest
        else:
            files_part, next_header = piece, None
        if header is not None:
            _, sha, date, message = header.split(_SEP, 3)
            files = [line.strip() for line in files_part.splitlines() if line.strip()]
            subject = message.splitlines()[0] if message else ""
            commits.append(
                {"sha": sha, "date": date, "message": message, "subject": subject, "files": files}
            )
        header = next_header
    if header is not None:
        _, sha, date, message = header.split(_SEP, 3)
        subject = message.splitlines()[0] if message else ""
        commits.append({"sha": sha, "date": date, "message": message, "subject": subject, "files": []})
    return commits


def _run_git_log(repo_root: Path, since_iso: str) -> list[dict[str, Any]]:
    """Exactly one git invocation per run, regardless of open row count."""
    cmd = [
        "git",
        "-C",
        str(repo_root),
        "-c",
        "core.quotepath=false",
        "log",
        "--name-only",
        f"--since={since_iso}",
        f"--format={_GIT_LOG_FORMAT}",
    ]
    # Git emits UTF-8 regardless of the host locale; decode explicitly so
    # non-ASCII paths and messages are identical on every platform.
    proc = subprocess.run(
        cmd,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return _parse_git_log(proc.stdout)


def _signal_for_row(
    row: dict[str, Any], commits: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    scope_files = set(row.get("scope_files") or [])
    created = _parse_dt(row["created_at"])
    row_id = row["id"]
    matches: list[tuple[dict[str, Any], bool, set[str]]] = []
    for commit in commits:
        commit_date = _parse_dt(commit["date"])
        if commit_date <= created:
            continue
        touched = set(commit["files"])
        named = _id_named_in_text(row_id, commit["message"])
        file_match = bool(scope_files & touched)
        if named or file_match:
            matches.append((commit, named, touched))
    if not matches:
        return "no_signal", []
    if any(named for _, named, _ in matches):
        signal = "named_in_commit"
    elif scope_files and any(scope_files <= touched for _, _, touched in matches):
        signal = "all_scope_files_touched"
    elif scope_files and any(scope_files & touched for _, _, touched in matches):
        signal = "some_scope_files_touched"
    else:
        signal = "no_signal"
    matched_commits = [commit for commit, _, _ in matches]
    return signal, matched_commits


def build_candidates(repo_root: Path) -> dict[str, Any]:
    """Build the deterministic candidate list for open rows. Never writes."""
    rows = _open_rows(repo_root)
    summary = {signal: 0 for signal in SIGNAL_ORDER}
    if not rows:
        return {"candidates": [], "summary": summary}
    oldest = min(_parse_dt(row["created_at"]) for row in rows)
    since_iso = (oldest - timedelta(seconds=1)).isoformat()
    commits = _run_git_log(repo_root, since_iso)
    candidates = []
    for row in rows:
        signal, matched = _signal_for_row(row, commits)
        summary[signal] += 1
        candidates.append(
            {
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "signal": signal,
                "commits": [
                    {"sha": commit["sha"][:10], "subject": commit["subject"]}
                    for commit in matched
                ],
            }
        )
    rank = {signal: index for index, signal in enumerate(SIGNAL_ORDER)}
    candidates.sort(key=lambda c: (rank[c["signal"]], c["id"]))
    return {"candidates": candidates, "summary": summary}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aiworkhub.needfix_likely_fixed")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    result = build_candidates(Path(args.repo))
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        for candidate in result["candidates"]:
            print(f"{candidate['id']} [{candidate['signal']}] {candidate['title']}")
        print(json.dumps(result["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
