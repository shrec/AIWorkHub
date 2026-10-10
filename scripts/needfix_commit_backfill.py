#!/usr/bin/env python
"""Backfill-resolve NeedFix ids a commit already closed on HEAD.

Walks the full HEAD history once, oldest-first, for commits that either
carry a ``Resolves: NF-YYYY-NNNNN`` git trailer or whose subject starts with
a conventional-commit kind (fix/feat/refactor/perf/chore) and ends with
``(NF-YYYY-NNNNN)``. A subject mentioning "part" is reported but never
resolved: a partial fix must not close its NeedFix.

``_reconcile_commit_trailers_on_read`` (see ``needfix_ingest.py``) only reads
the ``Resolves:`` trailer, and only within a bounded newest-commits window on
its first read -- this script is the one-shot, full-history, operator-run
complement for ids it can never reach.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from aiworkhub import needfix_store
from aiworkhub.needfix_ingest import (
    _COMMIT_TRAILER_NF_ID_RE,
    _git_resolves_trailer_log,
    _git_rev_list_oldest_first,
)

_SUBJECT_KIND_RE = re.compile(r"^(?:fix|feat|refactor|perf|chore)\b", re.IGNORECASE)
_SUBJECT_TRAILING_PAREN_RE = re.compile(r"\(([^()]*)\)$")
_GIT_TIMEOUT_SECONDS = 60


def _subjects_of(repo: Path, shas: list[str]) -> dict[str, str]:
    """One git-log call returning each commit's subject line, keyed by sha."""
    if not shas:
        return {}
    result = subprocess.run(
        [
            "git", "-C", str(repo), "log", "--no-walk=unsorted", "--stdin",
            "--format=%x1e%H%x1f%s",
        ],
        input="".join(f"{sha}\n" for sha in shas),
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        return {}
    subjects: dict[str, str] = {}
    for record in result.stdout.split("\x1e"):
        if not record:
            continue
        sha, _, subject = record.partition("\x1f")
        sha = sha.strip()
        if sha:
            subjects[sha] = subject
    return subjects


def collect_candidates(
    repo: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Return ``(eligible, skipped)`` rows from the full HEAD history, or
    ``None`` when the git scan itself failed (as opposed to an empty but
    successful history).

    Each row is ``{"needfix_id", "sha", "subject", "source"}`` where
    ``source`` is ``"trailer"`` or ``"subject"``. A commit whose subject
    contains "part" (case-insensitive) is reported in ``skipped`` only, never
    in ``eligible``.
    """
    try:
        commits = _git_rev_list_oldest_first(repo, ["HEAD"])
        if commits is None:
            return None
        if not commits:
            return [], []
        shas = [sha for sha, _parents in commits]
        subjects = _subjects_of(repo, shas)
        trailer_by_sha = dict(_git_resolves_trailer_log(repo, shas) or [])
    except subprocess.TimeoutExpired:
        return None

    eligible: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for sha in shas:
        subject = subjects.get(sha, "")
        is_part = "part" in subject.lower()
        found: list[tuple[str, str]] = []

        for nfid in _COMMIT_TRAILER_NF_ID_RE.findall(trailer_by_sha.get(sha, "")):
            found.append((nfid, "trailer"))

        if _SUBJECT_KIND_RE.match(subject):
            paren = _SUBJECT_TRAILING_PAREN_RE.search(subject.rstrip())
            if paren:
                year = None
                for part in paren.group(1).split("/"):
                    part = part.strip()
                    if _COMMIT_TRAILER_NF_ID_RE.fullmatch(part):
                        year = part.split("-")[1]
                        found.append((part, "subject"))
                    elif year and re.fullmatch(r"\d{5}", part):
                        found.append((f"NF-{year}-{part}", "subject"))

        for nfid, source in found:
            key = (sha, nfid)
            if key in seen:
                continue
            seen.add(key)
            row = {"needfix_id": nfid, "sha": sha, "subject": subject, "source": source}
            (skipped if is_part else eligible).append(row)

    return eligible, skipped


def confirm_resolve(repo: Path, eligible: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve each eligible id through ``needfix_store``, never raw SQL.

    Skips an id already ``resolved``/``archived`` rather than attempting it.
    """
    results: list[dict[str, Any]] = []
    for row in eligible:
        nfid = row["needfix_id"]
        try:
            record = needfix_store.get_needfix(repo, nfid)
        except needfix_store.NeedFixNotFoundError:
            results.append({**row, "result": "not_found"})
            continue
        if record["status"] in ("resolved", "archived"):
            results.append({**row, "result": f"already_{record['status']}"})
            continue
        updated = needfix_store.resolve_from_commit_trailer(repo, nfid, row["sha"])
        results.append({**row, "result": "resolved" if updated is not None else "no_op"})
    return results


def _print_rows(label: str, rows: list[dict[str, Any]]) -> None:
    print(f"{label} ({len(rows)}):")
    for row in rows:
        print(f"  {row['needfix_id']} {row['sha']} {row['subject']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="Repository root (default: cwd)")
    parser.add_argument(
        "--confirm", action="store_true",
        help="Resolve the listed eligible ids (default: preview only)",
    )
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()

    candidates = collect_candidates(repo)
    if candidates is None:
        print("git_scan_failed", file=sys.stderr)
        return 2
    eligible, skipped = candidates
    _print_rows("Eligible", eligible)
    _print_rows("Skipped (part)", skipped)

    if args.confirm:
        results = confirm_resolve(repo, eligible)
        resolved = [r for r in results if r["result"] == "resolved"]
        print(f"Resolved ({len(resolved)}):")
        for row in resolved:
            print(f"  {row['needfix_id']} {row['sha']}")
        other = [r for r in results if r["result"] != "resolved"]
        if other:
            print(f"Not resolved ({len(other)}):")
            for row in other:
                print(f"  {row['needfix_id']} {row['sha']} [{row['result']}]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
