"""Focused tests for bounded hash-bound reviewer packet source evidence."""

from __future__ import annotations

import hashlib

import pytest

from aiworkhub import quality_reviewer


SOURCE = "print('candidate marker')\n"
DIGEST = hashlib.sha256(SOURCE.encode("utf-8")).hexdigest()


def _evidence(**overrides):
    row = {
        "candidate_sha256": DIGEST,
        "excerpt": SOURCE,
        "excerpt_bytes": len(SOURCE.encode("utf-8")),
        "source_bytes": len(SOURCE.encode("utf-8")),
        "truncated": False,
    }
    row.update(overrides)
    return {"src/mod.py": row}


def _packet(**kwargs):
    return quality_reviewer.build_review_packet(
        request_id="req1",
        task_id="task1",
        claim_epoch=1,
        worker_provider="adapter-a",
        changed_path_hashes={"src/mod.py": DIGEST},
        **kwargs,
    )


def test_packet_binds_source_evidence_and_prompt_delivers_it():
    packet = _packet(source_evidence=_evidence())
    rows = packet["candidate"]["source_evidence"]
    assert [row["path"] for row in rows] == ["src/mod.py"]
    assert rows[0]["candidate_sha256"] == DIGEST
    prompt = quality_reviewer.build_review_prompt(packet, lens="correctness")
    assert "candidate marker" in prompt
    assert packet["packet_sha256"] in prompt


def test_packet_sha256_changes_with_source_evidence():
    assert _packet()["packet_sha256"] != _packet(source_evidence=_evidence())["packet_sha256"]


def test_hash_drift_fails_closed():
    with pytest.raises(quality_reviewer.ReviewerEvidenceError):
        _packet(source_evidence=_evidence(candidate_sha256="0" * 64))


def test_path_mismatch_fails_closed():
    with pytest.raises(quality_reviewer.ReviewerEvidenceError):
        _packet(source_evidence={"src/other.py": _evidence()["src/mod.py"]})


def test_missing_or_unreadable_evidence_fails_closed():
    with pytest.raises(quality_reviewer.ReviewerEvidenceError):
        _packet(source_evidence={})
    with pytest.raises(quality_reviewer.ReviewerEvidenceError):
        _packet(source_evidence=_evidence(excerpt=None))


def test_excerpt_overflow_fails_closed():
    oversized = "x" * (quality_reviewer.MAX_SOURCE_EVIDENCE_CHARS + 1)
    with pytest.raises(quality_reviewer.ReviewerEvidenceError):
        _packet(source_evidence=_evidence(excerpt=oversized))


def test_truncation_metadata_is_preserved():
    packet = _packet(source_evidence=_evidence(excerpt="pr", excerpt_bytes=2, truncated=True))
    row = packet["candidate"]["source_evidence"][0]
    assert row["truncated"] is True
    assert row["excerpt_bytes"] == 2
    assert row["source_bytes"] == len(SOURCE.encode("utf-8"))
