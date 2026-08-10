from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import core, manager_ai_tools, needfix_ingest, needfix_store, task_store


SESSION_ID = "019f5097-6dbe-7172-870a-945afc5f3bfa"


def _manager_route(root: Path) -> dict:
    return {
        "ok": True,
        "role": "manager",
        "provider": "codex",
        "repo": str(root),
        "manager_route": {
            "provider": "codex",
            "session_id": SESSION_ID,
            "thread_id": SESSION_ID,
        },
    }


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert task_store.initialize_repository(root)["ok"]
    reviews = root / "docs" / "reviews"
    reviews.mkdir(parents=True)
    (reviews / "README.md").write_text(
        "# Reviews\n\n[Audit](AUDIT.md)\n\nThis prose is not an intake item.\n",
        encoding="utf-8",
    )
    (reviews / "AUDIT.md").write_text(
        "# Audit\n\n"
        "## Findings\n\n"
        "### HIGH\n\n"
        "- Callback retry can duplicate a stale notification.\n"
        "  Preserve the exact event identity across retries.\n\n"
        "Unstructured prose must remain ignored.\n\n"
        "## Recommendations\n\n"
        "1. Add an idempotency regression for callback publication.\n\n"
        "## Background\n\n"
        "- This ordinary bullet is not a candidate.\n\n"
        "## Positive Findings\n\n"
        "- Atomic writes are already implemented correctly.\n",
        encoding="utf-8",
    )
    (root / "docs" / "PRODUCT_ROADMAP.md").write_text(
        "# Roadmap\n\n"
        "## Current work\n\n"
        "- [x] Already finished item.\n"
        "- [ ] Measure accepted-outcome routing cost.\n",
        encoding="utf-8",
    )
    return root


def _preview(root: Path) -> dict:
    return needfix_ingest.preview(
        root,
        source_paths=["docs/reviews/README.md", "docs/PRODUCT_ROADMAP.md"],
        follow_links=True,
    )


def test_preview_follows_bounded_links_and_writes_nothing(repo: Path):
    receipt = _preview(repo)

    assert receipt["ok"] is True
    assert receipt["source_count"] == 3
    assert receipt["candidate_count"] == 3
    assert receipt["new_count"] == 3
    assert {row["kind"] for row in receipt["candidates"]} == {
        "investigation", "improvement", "roadmap_candidate",
    }
    assert all(row["dedupe_match"] is None for row in receipt["candidates"])
    assert needfix_store.list_needfix(repo, include_archived=True, limit=500) == []


def test_commit_requires_exact_preview_and_is_captured_only(repo: Path):
    preview = _preview(repo)
    with pytest.raises(needfix_ingest.NeedFixIngestError, match="preview_identity_mismatch"):
        needfix_ingest.commit(
            repo,
            source_paths=["docs/reviews/README.md", "docs/PRODUCT_ROADMAP.md"],
            preview_id="wrong",
            follow_links=True,
        )

    first = needfix_ingest.commit(
        repo,
        source_paths=["docs/reviews/README.md", "docs/PRODUCT_ROADMAP.md"],
        preview_id=preview["preview_id"],
        follow_links=True,
    )
    second_preview = _preview(repo)
    second = needfix_ingest.commit(
        repo,
        source_paths=["docs/reviews/README.md", "docs/PRODUCT_ROADMAP.md"],
        preview_id=second_preview["preview_id"],
        follow_links=True,
    )

    assert first["counts"] == {"created_captured": 3}
    assert second["counts"] == {"updated_captured": 3}
    rows = needfix_store.list_needfix(repo, include_archived=True, limit=500)
    assert len(rows) == 3
    assert {row["status"] for row in rows} == {"captured"}
    assert all(row["provenance"]["verified"] is False for row in rows)
    assert all(row["evidence"]["source_fingerprint"] for row in rows)


def test_commit_never_mutates_a_promoted_dedupe_match(repo: Path):
    preview = _preview(repo)
    committed = needfix_ingest.commit(
        repo,
        source_paths=["docs/reviews/README.md", "docs/PRODUCT_ROADMAP.md"],
        preview_id=preview["preview_id"],
        follow_links=True,
    )
    promoted_id = committed["results"][0]["needfix_id"]
    needfix_store.triage_needfix(repo, promoted_id, readiness_score=25)

    replay = _preview(repo)
    result = needfix_ingest.commit(
        repo,
        source_paths=["docs/reviews/README.md", "docs/PRODUCT_ROADMAP.md"],
        preview_id=replay["preview_id"],
        follow_links=True,
    )

    skipped = [row for row in result["results"] if row["needfix_id"] == promoted_id]
    assert skipped == [{
        "source_fingerprint": skipped[0]["source_fingerprint"],
        "needfix_id": promoted_id,
        "action": "skipped_non_captured",
        "status": "triaged",
    }]
    assert needfix_store.get_needfix(repo, promoted_id)["status"] == "triaged"


def test_source_paths_fail_closed_on_traversal_and_symlink(repo: Path):
    with pytest.raises(needfix_ingest.NeedFixIngestError, match="unsafe_source_path"):
        needfix_ingest.preview(repo, source_paths=["../outside.md"])
    link = repo / "docs" / "linked.md"
    link.symlink_to(repo / "docs" / "PRODUCT_ROADMAP.md")
    with pytest.raises(needfix_ingest.NeedFixIngestError, match="source_not_regular"):
        needfix_ingest.preview(repo, source_paths=["docs/linked.md"])


def test_manager_surface_requires_identity_and_write_gate(repo: Path, monkeypatch):
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _manager_route(repo))
    monkeypatch.setattr(core, "writes_allowed", lambda: False)
    preview = manager_ai_tools.needfix_markdown_preview(
        source_paths=["docs/PRODUCT_ROADMAP.md"], follow_links=False,
    )
    denied = manager_ai_tools.needfix_markdown_commit(
        source_paths=["docs/PRODUCT_ROADMAP.md"],
        preview_id=preview["preview_id"],
        follow_links=False,
    )
    assert preview["ok"] is True
    assert denied["error"] == "write_gate_closed"

    monkeypatch.setattr(
        core,
        "manager_bootstrap",
        lambda: {"ok": True, "role": "worker_or_unverified_client", "manager_route": {}},
    )
    rejected = manager_ai_tools.needfix_markdown_preview(
        source_paths=["docs/PRODUCT_ROADMAP.md"], follow_links=False,
    )
    assert rejected == {"ok": False, "error": "verified_manager_identity_required"}
