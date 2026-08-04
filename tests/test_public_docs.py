from pathlib import Path

from scripts import check_public_docs


ROOT = Path(__file__).resolve().parents[1]


def _fixture(tmp_path: Path, readme: str) -> None:
    for document in check_public_docs.PUBLIC_DOCS:
        target = tmp_path / document.relative_to(check_public_docs.ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Document\n")
    (tmp_path / "README.md").write_text(readme)
    for relative, canonical in check_public_docs.PUBLIC_SITE_PAGES.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        schemas = ""
        if relative == "site/index.html":
            schemas = '"SoftwareApplication" "SoftwareSourceCode"'
        target.write_text(
            "<title>AIWorkHub</title>"
            '<meta name="description" content="Description">'
            f'<link rel="canonical" href="{canonical}">'
            '<meta property="og:title" content="AIWorkHub">'
            f'<meta property="og:url" content="{canonical}">'
            '<meta property="og:image" content="https://example.com/image.png">'
            '<meta name="twitter:card" content="summary_large_image">'
            '<meta name="robots" content="index,follow">'
            + schemas,
            encoding="utf-8",
        )
    sitemap_urls = "".join(
        f"<url><loc>{canonical}</loc></url>"
        for canonical in check_public_docs.PUBLIC_SITE_PAGES.values()
    )
    (tmp_path / "site/sitemap.xml").write_text(
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + sitemap_urls
        + "</urlset>",
        encoding="utf-8",
    )
    (tmp_path / "site/robots.txt").write_text(
        f"Sitemap: {check_public_docs.SITE_BASE}sitemap.xml\n",
        encoding="utf-8",
    )


def test_live_public_docs_have_valid_local_links_and_no_internal_leaks() -> None:
    assert check_public_docs.check(ROOT) == []


def test_broken_local_link_is_rejected(tmp_path: Path) -> None:
    _fixture(tmp_path, "# Product\n\n[Missing](docs/nope.md)\n")
    assert check_public_docs.check(tmp_path) == [
        "README.md: broken local link 'docs/nope.md'"
    ]


def test_marketplace_readme_rejects_relative_html_image(tmp_path: Path) -> None:
    _fixture(tmp_path, "# Product\n")
    marketplace_readme = tmp_path / "vscode-extension/README.md"
    marketplace_readme.write_text(
        '<img src="media/aiworkhub-hero.png" alt="AIWorkHub">\n',
        encoding="utf-8",
    )
    assert check_public_docs.check(tmp_path) == [
        "vscode-extension/README.md: Marketplace image must use a public HTTPS "
        "URL, got 'media/aiworkhub-hero.png'"
    ]


def test_legacy_paths_task_ids_and_wrong_tool_name_are_rejected(tmp_path: Path) -> None:
    _fixture(
        tmp_path,
        "# Product\n\nB416 `AITools/taskctl.py` aiworkhub_task_completion_inbox\n",
    )
    errors = check_public_docs.check(tmp_path)
    assert len(errors) == 3
    assert all(error.startswith("README.md: contains ") for error in errors)


def test_site_page_missing_canonical_metadata_is_rejected(tmp_path: Path) -> None:
    _fixture(tmp_path, "# Product\n")
    target = tmp_path / "site/source-graph/index.html"
    target.write_text("<title>Source Graph</title>", encoding="utf-8")
    errors = check_public_docs.check(tmp_path)
    assert any(
        error == "site/source-graph/index.html: missing canonical URL"
        for error in errors
    )


def test_sitemap_must_match_all_canonical_pages(tmp_path: Path) -> None:
    _fixture(tmp_path, "# Product\n")
    (tmp_path / "site/sitemap.xml").write_text(
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" />',
        encoding="utf-8",
    )
    errors = check_public_docs.check(tmp_path)
    assert any(error.startswith("site/sitemap.xml: canonical URL mismatch") for error in errors)


def test_broken_deployed_site_link_is_rejected(tmp_path: Path) -> None:
    _fixture(tmp_path, "# Product\n")
    home = tmp_path / "site/index.html"
    home.write_text(
        home.read_text(encoding="utf-8")
        + '<a href="/AIWorkHub/missing-page/">Missing</a>',
        encoding="utf-8",
    )
    errors = check_public_docs.check(tmp_path)
    assert "site/index.html: broken deployed site link '/AIWorkHub/missing-page/'" in errors
