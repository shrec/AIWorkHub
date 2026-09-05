"""Focused tests for bounded hash-bound reviewer packet source evidence."""

from __future__ import annotations

import hashlib
import json

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


def _scoped_audit(lens: str) -> dict:
    packet = {
        "task_id": "task1",
        "review_lens": {"lens_kind": lens},
        "changed_paths": [{"path": "src/mod.py"}],
        "known_unknowns": [f"{lens} graph boundary"],
    }
    return {
        "schema_id": "aiworkhub.scoped_audit.v1",
        "fingerprint": hashlib.sha256(
            json.dumps(
                packet,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        "known_unknowns": packet["known_unknowns"],
        "packet": packet,
    }


def _scoped_audits(*lenses: str) -> dict[str, dict]:
    return {lens: _scoped_audit(lens) for lens in lenses}


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


def test_changed_hunk_segments_are_preserved_in_packet_and_prompt():
    segment = {
        "kind": "insert",
        "candidate_start_line": 405,
        "candidate_end_line": 412,
        "changed_start_line": 408,
        "changed_end_line": 409,
        "baseline_start_line": 407,
        "baseline_end_line": 407,
        "excerpt_bytes": len(SOURCE.encode("utf-8")),
        "truncated": False,
    }
    packet = _packet(source_evidence=_evidence(segments=[segment]))
    row = packet["candidate"]["source_evidence"][0]

    assert row["segments"] == [segment]
    assert "candidate_start_line" in quality_reviewer.build_review_prompt(
        packet, lens="correctness"
    )


def _deleted_packet():
    return quality_reviewer.build_review_packet(
        request_id="req1",
        task_id="task1",
        claim_epoch=1,
        worker_provider="adapter-a",
        changed_path_hashes={"src/deleted.py": None},
        source_evidence={
            "src/deleted.py": {
                "candidate_sha256": None,
                "excerpt": "",
                "excerpt_bytes": 0,
                "source_bytes": 0,
                "truncated": False,
                "segments": [],
                "omission_reason": "candidate_deleted_or_non_file",
            }
        },
    )


def _reseal(packet):
    body = {key: value for key, value in packet.items() if key != "packet_sha256"}
    packet["packet_sha256"] = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return packet


def test_deleted_candidate_path_uses_explicit_omission_reason():
    packet = _deleted_packet()

    row = packet["candidate"]["source_evidence"][0]
    assert row["candidate_sha256"] is None
    assert row["omission_reason"] == "candidate_deleted_or_non_file"


def test_verify_packet_accepts_authenticated_deleted_candidate(tmp_path):
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(_deleted_packet()), encoding="utf-8")

    verified = quality_reviewer.verify_review_packet_candidate(packet_path, tmp_path)

    assert verified["changed_paths"] == [{"path": "src/deleted.py", "sha256": None}]


def test_verify_packet_rejects_forged_deleted_candidate_omission(tmp_path):
    packet = _deleted_packet()
    packet["candidate"]["source_evidence"][0].pop("omission_reason")
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(_reseal(packet)), encoding="utf-8")

    with pytest.raises(quality_reviewer.ReviewerEvidenceError):
        quality_reviewer.verify_review_packet_candidate(packet_path, tmp_path)


def test_verify_packet_rejects_omission_when_candidate_file_exists(tmp_path):
    candidate = tmp_path / "src" / "deleted.py"
    candidate.parent.mkdir()
    candidate.write_text("still present\n", encoding="utf-8")
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(_deleted_packet()), encoding="utf-8")

    with pytest.raises(quality_reviewer.ReviewerEvidenceError):
        quality_reviewer.verify_review_packet_candidate(packet_path, tmp_path)


def test_scoped_audit_known_unknowns_are_preserved_in_packet():
    scoped = _scoped_audit("code_quality")
    scoped["known_unknowns"] = ["Source Graph omitted generated files"]
    scoped["packet"]["known_unknowns"] = ["Source Graph omitted generated files"]
    scoped["fingerprint"] = hashlib.sha256(
        json.dumps(
            scoped["packet"],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    packet = _packet(
        scoped_audits={"code_quality": scoped}
    )

    scope = packet["candidate"]["scoped_audits"]["code_quality"]
    assert scope["known_unknowns"] == ["Source Graph omitted generated files"]


@pytest.mark.parametrize("lens", ["correctness", "security", "code_quality"])
def test_packet_to_prompt_renders_matching_scoped_audit_for_each_lens(lens):
    packet = _packet(
        source_evidence=_evidence(),
        scoped_audits=_scoped_audits("correctness", "security", "code_quality"),
    )

    prompt = quality_reviewer.build_review_prompt(packet, lens=lens)

    assert f"Review lens: {lens}." in prompt
    assert f'"{lens} graph boundary"' in prompt
    assert f'"lens_kind":"{lens}"' in prompt
    for other_lens in {"correctness", "security", "code_quality"} - {lens}:
        assert f'"lens_kind":"{other_lens}"' in prompt


@pytest.mark.parametrize("lens", ["correctness", "security", "code_quality"])
def test_prompt_requires_matching_scoped_audit_for_each_lens(lens):
    packet = _packet(
        source_evidence=_evidence(),
        scoped_audits=_scoped_audits("code_quality"),
    )

    if lens == "code_quality":
        assert f"Review lens: {lens}." in quality_reviewer.build_review_prompt(
            packet, lens=lens
        )
    else:
        with pytest.raises(
            quality_reviewer.ReviewerEvidenceError,
            match="review_scope_lens_missing",
        ):
            quality_reviewer.build_review_prompt(packet, lens=lens)


def test_scoped_audit_known_unknowns_wrapper_tamper_fails_closed():
    scoped_payload = {
        "task_id": "task1",
        "review_lens": {"lens_kind": "code_quality"},
        "changed_paths": [{"path": "src/mod.py"}],
        "known_unknowns": ["payload limit"],
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            scoped_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    with pytest.raises(
        quality_reviewer.ReviewerEvidenceError,
        match="review_scope_known_unknowns_mismatch",
    ):
        _packet(
            scoped_audits={
                "code_quality": {
                    "schema_id": "aiworkhub.scoped_audit.v1",
                    "fingerprint": fingerprint,
                    "known_unknowns": ["outer tamper"],
                    "packet": scoped_payload,
                }
            }
        )
