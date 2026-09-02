#!/usr/bin/env python3
"""Manager-side semantic edit: replace one verified line range, nothing else.

The manager MCP surface has no semantic-edit tool -- ``semantic_edit_prepare``
and ``semantic_edit_apply`` exist only for workers -- so manager corrections
were being made by whole-string rewrites with no hash binding at all. The one
instrument that makes a small edit verifiable was locked to one caller, and the
role whose charter is "small precise corrections" was the role without it.

This is that instrument, over the same shared applier the worker session uses.
It cannot regenerate a file: it replaces exactly the line range named, refuses
if the file or the fragment moved since it was read, and prints a receipt of
what it exposed and what it emitted.

    python3 scripts/manager_semantic_edit.py --path src/x.py --start 10 --end 14 <<'NEW'
    replacement lines
    NEW

Read the range first (``sed -n '10,14p'``, or a Source Graph body slice) so the
replacement is written against bytes that were actually seen.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import semantic_edit, semantic_edit_applier  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="manager-semantic-edit",
        description="Replace one hash-verified line range; never rewrite a file.",
    )
    parser.add_argument("--repo", default=None, help="repository root (default: cwd)")
    parser.add_argument("--path", required=True, help="repo-relative file path")
    parser.add_argument("--start", type=int, required=True, help="first line, 1-based")
    parser.add_argument("--end", type=int, required=True, help="last line, inclusive")
    parser.add_argument(
        "--new-file",
        default=None,
        help="file holding the replacement text (default: read stdin)",
    )
    args = parser.parse_args(argv)

    root = Path(args.repo).resolve() if args.repo else Path.cwd().resolve()
    new = (
        Path(args.new_file).read_text(encoding="utf-8")
        if args.new_file
        else sys.stdin.read()
    )

    try:
        target = semantic_edit.prepare_line_target(
            root,
            path=args.path,
            start_line=args.start,
            end_line=args.end,
            allowed_writes=[args.path],
        )
        next_text, metrics = semantic_edit_applier.replace_prepared_range(
            root, target, new, allowed_writes=[args.path]
        )
    except (OSError, semantic_edit.SemanticEditError) as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}), file=sys.stderr)
        return 1

    print(json.dumps({
        "ok": True,
        "schema_id": "aiworkhub.manager_semantic_edit_receipt.v1",
        "path": target.path,
        "start_line": target.start_line,
        "end_line": target.end_line,
        "before_sha256": target.current_sha256,
        "old_region_bytes": len(target.fragment.encode("utf-8")),
        "replacement_bytes": len(new.encode("utf-8")),
        "file_bytes": len(next_text.encode("utf-8")),
        **{k: v for k, v in metrics.items() if isinstance(v, (int, float, bool, str))},
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
