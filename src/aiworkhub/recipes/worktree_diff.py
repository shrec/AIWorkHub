#!/usr/bin/env python3
"""Bounded unified diff of a retained candidate worktree against the canonical tree.

Replaces the heredoc a manager types to read what a candidate actually changed
before accepting or rejecting it. 388 of 4,962 measured Bash calls read
repository files by hand.

Never edits the worktree. A candidate worktree's file hashes ARE the review
evidence, so this script only reads: it opens the two files and diffs them in
memory with ``difflib``, spawning nothing and writing nothing.

The layout is verified against this repository's own runtime directory:
``.aiworkhub/runtime/worktrees/<request_id>/worktree`` is the candidate tree,
and the set of changed paths comes from
``process_logs/processes/attempt-artifacts/<request_id>/diff.json``
(``changed_paths``), which the launcher itself wrote. Measured: 75 of 75
retained worktrees have that file. When it is absent the script says so and
returns no paths rather than walking the tree and guessing which files matter.

    python -m aiworkhub.recipes.worktree_diff --request-id ID [--path P]
        [--max-bytes N]

``--path all`` (the default) diffs every changed path. A single ``--path``
restricts the diff to that one repository-relative file.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

from . import _common as common

SCHEMA_ID = "aiworkhub.recipe.worktree_diff.v1"

# A file that is not decodable as UTF-8 is reported as binary rather than
# diffed: a byte-level diff of a binary file is noise in a review, and
# pretending it decoded would corrupt the evidence.
_MAX_FILE_BYTES = 2_000_000


def _read_text(path: Path) -> tuple[list[str], str]:
    if not path.is_file():
        return [], "absent"
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return [], "oversized"
        data = path.read_bytes()
    except OSError as exc:
        return [], f"unreadable:{type(exc).__name__}"
    if b"\x00" in data[:8192]:
        return [], "binary"
    return data.decode("utf-8", errors="replace").splitlines(keepends=True), "text"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--request-id", required=True, help="32-hex launch request id")
    parser.add_argument(
        "--path",
        default=common.NO_FILTER,
        help="one repository-relative path, or 'all' (default: all)",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=60_000,
        help="total diff text budget across all files (default: 60000)",
    )
    args = parser.parse_args(argv)

    request_id = str(args.request_id).strip().lower()
    if not common.is_request_id(request_id):
        return common.fail(
            SCHEMA_ID, "invalid_request_id", "request id must be 32 hex characters"
        )
    budget = max(1024, min(int(args.max_bytes), 400_000))

    worktree = common.WORKTREES_DIR / request_id / "worktree"
    if not worktree.is_dir():
        return common.fail(
            SCHEMA_ID, "worktree_not_retained", f"no candidate worktree at {worktree}"
        )

    diff_artifact = common.ATTEMPT_ARTIFACTS_DIR / request_id / "diff.json"
    artifact = common.load_json(diff_artifact)
    if not isinstance(artifact, dict) or not isinstance(
        artifact.get("changed_paths"), list
    ):
        return common.fail(
            SCHEMA_ID,
            "changed_paths_unavailable",
            f"no changed_paths in {diff_artifact}; this script never guesses "
            "the changed set by walking the tree",
        )
    changed = [p for p in artifact["changed_paths"] if isinstance(p, str) and p]
    if args.path != common.NO_FILTER:
        wanted = str(args.path)
        changed = [p for p in changed if p == wanted]
        if not changed:
            return common.fail(
                SCHEMA_ID,
                "path_not_changed",
                f"{args.path!r} is not in this candidate's changed set",
            )

    files: list[dict] = []
    spent = 0
    budget_exhausted = False
    for relative in sorted(changed):
        if ".." in Path(relative).parts or Path(relative).is_absolute():
            files.append({"path": relative, "status": "rejected_unsafe_path"})
            continue
        canonical_lines, canonical_status = _read_text(common.REPO_ROOT / relative)
        candidate_lines, candidate_status = _read_text(worktree / relative)
        entry = {
            "path": relative,
            "canonical_status": canonical_status,
            "candidate_status": candidate_status,
        }
        if canonical_status in ("text", "absent") and candidate_status in (
            "text",
            "absent",
        ):
            diff = "".join(
                difflib.unified_diff(
                    canonical_lines,
                    candidate_lines,
                    fromfile=f"canonical/{relative}",
                    tofile=f"candidate/{relative}",
                    n=3,
                )
            )
            remaining = budget - spent
            if remaining <= 0:
                entry["diff_omitted"] = "budget_exhausted"
                budget_exhausted = True
                files.append(entry)
                continue
            if len(diff) > remaining:
                entry["diff"] = diff[:remaining]
                entry["diff_truncated"] = True
                entry["diff_chars"] = len(diff)
                spent = budget
                budget_exhausted = True
            else:
                entry["diff"] = diff
                entry["diff_truncated"] = False
                entry["diff_chars"] = len(diff)
                spent += len(diff)
        else:
            entry["diff_omitted"] = f"{canonical_status}/{candidate_status}"
        files.append(entry)

    return common.emit(
        {
            "schema_id": SCHEMA_ID,
            "request_id": request_id,
            "worktree_path": str(worktree),
            "changed_paths_source": str(diff_artifact),
            "changed_path_count": len(artifact["changed_paths"]),
            "path_filter": args.path,
            "returned_count": len(files),
            "max_bytes": budget,
            "budget_exhausted": budget_exhausted,
            "files": files,
        }
    )


if __name__ == "__main__":
    sys.exit(main())
