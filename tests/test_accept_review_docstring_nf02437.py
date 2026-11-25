"""NF-2026-02437: the published accept_review description documents manager_measured."""

from __future__ import annotations

from aiworkhub import server


def test_accept_review_description_documents_manager_measured() -> None:
    description = server.aiworkhub_agent_accept_review.__doc__ or ""

    assert "manager_measured" in description
    assert "manager_measured_sha256" in description
    assert "validation:<index>" in description
    assert "manager:<repo-relative path>@<sha256>" in description
    assert "allowed_writes_amendment" in description
