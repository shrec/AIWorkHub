"""Both production reviewer boundaries bind overbuild findings on canonical index proof."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import quality_review_ingest, quality_reviewer, worker_ai_tools_mcp

LENS = "overbuild"
CHANGED = "src/pkg/new.py"
REPLACEMENT = "src/pkg/old.py.helper"


def _packet() -> dict[str, Any]:
    return {
        "schema_id": quality_reviewer.PACKET_SCHEMA_ID,
        "packet_sha256": "a" * 64,
        "candidate": {
            "changed_paths": [{"path": CHANGED}],
            "source_evidence": [
                {"path": CHANGED, "segments": [{"changed_start_line": 1, "changed_end_line": 20}]}
            ],
            "scoped_audits": {LENS: {"packet": {"changed_paths": [{"path": CHANGED}]}}},
        },
    }


def _overbuild_finding() -> dict[str, Any]:
    return {
        "severity": "medium",
        "disposition": "defect",
        "category": "duplicate_existing_symbol",
        "summary": "new helper duplicates an existing one",
        "evidence": f"{CHANGED}:10 re-implements the existing helper",
        "path": CHANGED,
        "line_start": 10,
        "line_end": 10,
        "replacement": REPLACEMENT,
    }


@pytest.fixture
def fake_index(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Canonical index holding exactly one out-of-diff definition of REPLACEMENT."""
    roots: list[Path] = []

    def factory(repo_root: Path) -> quality_reviewer.SymbolResolver:
        roots.append(Path(repo_root))

        def resolve(qualname: str) -> list[dict[str, Any]]:
            if qualname != REPLACEMENT:
                return []
            return [{"file_path": "src/pkg/old.py", "line_start": 3, "line_end": 9}]

        return resolve

    monkeypatch.setattr(quality_reviewer, "canonical_index_symbol_resolver", factory)
    return roots


def _dropping_invalid(resolver: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    record: list[dict[str, Any]] = []
    malformed = {"severity": "bogus", "summary": "x", "evidence": "y"}
    kept = quality_review_ingest._packet_findings_dropping_invalid(
        _packet(), lens=LENS, findings=[malformed, _overbuild_finding()],
        positions=[0, 1], record=record, symbol_resolver=resolver,
    )
    return kept, record


def test_ingest_forwards_canonical_resolver_across_retries(
    tmp_path: Path, fake_index: list[Path]
) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    kept, record = _dropping_invalid(quality_review_ingest.canonical_symbol_resolver(canonical))
    assert fake_index == [canonical]
    assert [f["category"] for f in kept] == ["duplicate_existing_symbol"]
    # The malformed row forced a retry; the resolver survived it.
    assert [row["index"] for row in record] == [0]


@pytest.mark.parametrize("root", [None, "", "missing"])
def test_ingest_without_canonical_root_refuses_the_binding(
    tmp_path: Path, fake_index: list[Path], root: str | None
) -> None:
    if root == "missing":
        root = str(tmp_path / "missing")
    resolver = quality_review_ingest.canonical_symbol_resolver(root)
    assert resolver is None
    kept, record = _dropping_invalid(resolver)
    assert kept == []
    assert fake_index == []
    assert any("overbuild_replacement_unbound" in row["dropped"] for row in record)


class _Normalized(Exception):
    def __init__(self, outcome: Any) -> None:
        super().__init__(repr(outcome))
        self.outcome = outcome


def _submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, authority_repo: Path
) -> Any:
    real = quality_reviewer.normalize_packet_findings

    def spy(*args: Any, **kwargs: Any) -> Any:
        try:
            outcome: Any = real(*args, **kwargs)
        except quality_reviewer.ReviewerEvidenceError as exc:
            outcome = str(exc)
        raise _Normalized(outcome)

    monkeypatch.setattr(quality_reviewer, "normalize_packet_findings", spy)
    monkeypatch.setattr(worker_ai_tools_mcp, "_spent_submit_attempts", lambda ctx, tool: 0)
    packet = _packet()
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    ctx = worker_ai_tools_mcp.WorkerToolContext(
        task_id="task", runner="claude", topic="topic", request_id="req",
        repo=worktree, authority_repo=authority_repo, source_graph_targets=(),
        session_topic="topic", audit_ledger_path=None, audit_hmac_key_path=None,
        quality_review_packet_path=packet_path,
    )
    with pytest.raises(_Normalized) as caught:
        worker_ai_tools_mcp.quality_review_submit(
            ctx, packet_sha256=packet["packet_sha256"], lens=LENS,
            findings=[_overbuild_finding()],
        )
    return caught.value.outcome


def test_submit_binds_on_the_canonical_root_not_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_index: list[Path]
) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    outcome = _submit(tmp_path, monkeypatch, authority_repo=canonical)
    assert fake_index == [canonical]
    assert [f["category"] for f in outcome] == ["duplicate_existing_symbol"]


def test_submit_without_canonical_root_refuses_the_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_index: list[Path]
) -> None:
    outcome = _submit(tmp_path, monkeypatch, authority_repo=tmp_path / "missing")
    assert fake_index == []
    assert outcome == "review_finding_0_overbuild_replacement_unbound"
