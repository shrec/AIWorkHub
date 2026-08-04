#!/usr/bin/env python3
"""Fail release CI on broken or leaked public-documentation contracts."""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
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
SITE_BASE = "https://shrec.github.io/AIWorkHub/"
PUBLIC_SITE_PAGES = {
    "site/index.html": SITE_BASE,
    "site/ai-coding-agent-orchestration/index.html": (
        SITE_BASE + "ai-coding-agent-orchestration/"
    ),
    "site/multi-agent-coding/index.html": SITE_BASE + "multi-agent-coding/",
    "site/coding-agent-context-memory/index.html": (
        SITE_BASE + "coding-agent-context-memory/"
    ),
    "site/source-graph/index.html": SITE_BASE + "source-graph/",
    "site/evidence-based-code-review/index.html": (
        SITE_BASE + "evidence-based-code-review/"
    ),
    "site/vscode-extension/index.html": SITE_BASE + "vscode-extension/",
    "site/docs/index.html": SITE_BASE + "docs/",
}
LINK_RE = re.compile(r"!?\[[^]]*\]\(([^)]+)\)")
HTML_IMAGE_RE = re.compile(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
SITE_LINK_RE = re.compile(r"(?:href|src)=[\"'](/AIWorkHub/[^\"'#?]*)[\"']", re.IGNORECASE)
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

        if document.relative_to(root) == Path("vscode-extension/README.md"):
            for raw in HTML_IMAGE_RE.findall(text):
                if not raw.startswith("https://"):
                    errors.append(
                        "vscode-extension/README.md: Marketplace image must use "
                        f"a public HTTPS URL, got {raw!r}"
                    )

    readme = root / "README.md"
    if readme.is_file():
        text = readme.read_text(encoding="utf-8")
        for pattern, label in FORBIDDEN_README.items():
            if re.search(pattern, text):
                errors.append(f"README.md: contains {label}")

    canonical_urls: set[str] = set()
    for relative, canonical in PUBLIC_SITE_PAGES.items():
        page = root / relative
        if not page.is_file():
            errors.append(f"missing public site page: {relative}")
            continue
        text = page.read_text(encoding="utf-8")
        required_fragments = {
            "HTML title": "<title>",
            "meta description": '<meta name="description"',
            "canonical URL": f'<link rel="canonical" href="{canonical}">',
            "Open Graph title": '<meta property="og:title"',
            "Open Graph URL": f'<meta property="og:url" content="{canonical}">',
            "Open Graph image": '<meta property="og:image"',
            "Twitter card": '<meta name="twitter:card"',
            "index directive": '<meta name="robots" content="index,follow',
        }
        for label, fragment in required_fragments.items():
            if fragment not in text:
                errors.append(f"{relative}: missing {label}")
        for raw in SITE_LINK_RE.findall(text):
            deployed_path = raw.removeprefix("/AIWorkHub/")
            source_target = root / "site" / deployed_path
            if raw.endswith("/"):
                source_target = source_target / "index.html"
            copied_asset = root / "docs" / deployed_path
            extension_asset = root / "vscode-extension" / "media" / Path(deployed_path).name
            if (
                not source_target.exists()
                and not copied_asset.exists()
                and not extension_asset.exists()
            ):
                errors.append(f"{relative}: broken deployed site link {raw!r}")
        canonical_urls.add(canonical)

    home = root / "site/index.html"
    if home.is_file():
        text = home.read_text(encoding="utf-8")
        for schema_type in ('"SoftwareApplication"', '"SoftwareSourceCode"'):
            if schema_type not in text:
                errors.append(f"site/index.html: missing JSON-LD {schema_type}")

    sitemap = root / "site/sitemap.xml"
    if not sitemap.is_file():
        errors.append("missing public site sitemap: site/sitemap.xml")
    else:
        try:
            tree = ET.parse(sitemap)
            namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            sitemap_urls = {
                str(node.text or "").strip()
                for node in tree.findall("sm:url/sm:loc", namespace)
            }
            if sitemap_urls != canonical_urls:
                missing = sorted(canonical_urls - sitemap_urls)
                extra = sorted(sitemap_urls - canonical_urls)
                errors.append(
                    "site/sitemap.xml: canonical URL mismatch "
                    f"missing={missing} extra={extra}"
                )
        except (ET.ParseError, OSError) as exc:
            errors.append(f"site/sitemap.xml: invalid XML: {exc}")

    robots = root / "site/robots.txt"
    if not robots.is_file():
        errors.append("missing public site robots.txt")
    elif f"Sitemap: {SITE_BASE}sitemap.xml" not in robots.read_text(encoding="utf-8"):
        errors.append("site/robots.txt: missing canonical sitemap URL")
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
