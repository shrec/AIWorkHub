#!/usr/bin/env python3
"""Fail release CI on broken or leaked public-documentation contracts."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DOCS = tuple(
    ROOT / path
    for path in (
        "README.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "SUPPORT.md",
        "vscode-extension/README.md",
        "docs/ARCHITECTURE.md",
        "docs/CALLBACKS.md",
        "docs/GETTING_STARTED.md",
        "docs/PUBLISHING.md",
        "docs/PRODUCT_ROADMAP.md",
    )
)
LINK_RE = re.compile(r"!?\[[^]]*\]\(([^)]+)\)")
FORBIDDEN_README = {
    r"\bB\d{3,}\b": "internal task/bug identifier",
    r"(?:^|[\s`(])AITools/": "legacy host-only AITools path",
    r"tools/vscode-aiworkhub-task-operations": "removed extension documentation path",
    r"aiworkhub_task_completion_inbox": "non-canonical completion inbox tool name",
}


def _local_target(document: Path, raw: str) -> Path | None:
    target = raw.strip().strip("<>").split(maxsplit=1)[0]
    if not target or target.startswith(("#", "http://", "https://", "mailto:")):
        return None
    path_text = unquote(target.split("#", 1)[0])
    if not path_text:
        return None
    return (document.parent / path_text).resolve()


def check(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    documents = tuple(root / path.relative_to(ROOT) for path in PUBLIC_DOCS)
    for document in documents:
        if not document.is_file():
            errors.append(f"missing public document: {document.relative_to(root)}")
            continue
        text = document.read_text(encoding="utf-8")
        for raw in LINK_RE.findall(text):
            target = _local_target(document, raw)
            if target is not None and not target.exists():
                errors.append(f"{document.relative_to(root)}: broken local link {raw!r}")

    readme = root / "README.md"
    if readme.is_file():
        text = readme.read_text(encoding="utf-8")
        for pattern, label in FORBIDDEN_README.items():
            if re.search(pattern, text):
                errors.append(f"README.md: contains {label}")
    return errors


def main() -> int:
    errors = check()
    if errors:
        print("public documentation check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("public documentation check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
