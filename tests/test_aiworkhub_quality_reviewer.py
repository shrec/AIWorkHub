"""Focused tests for bounded hash-bound reviewer packet source evidence."""

from __future__ import annotations

import difflib
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
        packet, lens="correctness")

def _audit_with_targets(lens: str, targets: list[str]) -> dict:
    inner = {
        "task_id": "task1",
        "review_lens": {"lens_kind": lens},
        "changed_paths": [{"path": "src/mod.py"}],
        "targets": targets,
        "known_unknowns": [f"{lens} graph boundary"],
    }
    return {
        "schema_id": "aiworkhub.scoped_audit.v1",
        "fingerprint": hashlib.sha256(
            json.dumps(
                inner,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        "known_unknowns": inner["known_unknowns"],
        "packet": inner,
    }

def _segment(start: int, end: int) -> dict:
    return {
        "kind": "insert",
        "candidate_start_line": start,
        "candidate_end_line": end,
        "changed_start_line": start,
        "changed_end_line": end,
        "baseline_start_line": start,
        "baseline_end_line": start,
        "excerpt_bytes": len(SOURCE.encode("utf-8")),
        "truncated": False,
    }

def test_prompt_leads_with_changed_hunks_then_bounded_graph_impact():
    packet = _packet(scoped_audits=_scoped_audits("correctness"))
    prompt = quality_reviewer.build_review_prompt(packet, lens="correctness")
    hunks = prompt.index("authenticated changed hunks")
    impact = prompt.index("graph-connected affected callers and tests")
    unknowns = prompt.index("then its explicit known_unknowns")
    assert hunks < impact < unknowns
    assert "do not re-read whole files" in prompt
    assert "do not scan the whole repository" in prompt

def test_known_unknowns_escalate_instead_of_claiming_clean():
    packet = _packet(scoped_audits=_scoped_audits("correctness"))
    prompt = quality_reviewer.build_review_prompt(packet, lens="correctness")
    assert "escalated as a process_limit finding" in prompt
    assert "can never support a clean result" in prompt

def test_candidate_delta_marks_unchanged_paths_without_dropping_contract():
    delta = {
        "predecessor_request_id": "req0",
        "paths": {
            "src/mod.py": {
                "unchanged_since_reviewed": True,
                "predecessor_sha256": DIGEST,
            }
        },
    }
    packet = _packet(
        acceptance=["acceptance row must survive"],
        required_outputs=["src/mod.py"],
        scoped_audits=_scoped_audits("correctness"),
        candidate_delta=delta,
    )
    assert packet["contract"]["acceptance"] == ["acceptance row must survive"]
    prompt = quality_reviewer.build_review_prompt(packet, lens="correctness")
    assert (
        "candidate.delta marks src/mod.py as unchanged_since_reviewed" in prompt
    )
    assert "acceptance row must survive" in prompt

def test_identical_discontiguous_fixture_measured_for_targets_and_packet_bytes():
    audit = _audit_with_targets("correctness", ["near_handler", "far_handler"])
    packet = _packet(
        source_evidence=_evidence(segments=[_segment(12, 14), _segment(988, 990)]),
        scoped_audits={"correctness": audit},
    )
    lens_packet = quality_reviewer.build_lens_packet(packet, lens="correctness")
    measured_bytes = len(
        json.dumps(
            lens_packet, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    )
    body = {k: v for k, v in lens_packet.items() if k != "packet_sha256"}
    resealed = hashlib.sha256(
        json.dumps(
            body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()
    assert lens_packet["packet_sha256"] == resealed == packet["packet_sha256"]
    assert isinstance(measured_bytes, int)
    assert measured_bytes < 96 * 1024
    prompt = quality_reviewer.build_review_prompt(lens_packet, lens="correctness")
    assert prompt.count("near_handler") == 1
    assert prompt.count("far_handler") == 1
    assert "between_handler" not in prompt


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
    shared = _packet(
        source_evidence=_evidence(),
        scoped_audits=_scoped_audits("correctness", "security", "code_quality"),
    )
    packet = quality_reviewer.build_lens_packet(shared, lens=lens)

    prompt = quality_reviewer.build_review_prompt(packet, lens=lens)

    assert f"Review lens: {lens}." in prompt
    assert f'"{lens} graph boundary"' in prompt
    assert f'"lens_kind":"{lens}"' in prompt
    # The lens packet carries this lens's scope and no other: the shared
    # three-lens packet is the coordinator's, never the reviewer's.
    for other_lens in {"correctness", "security", "code_quality"} - {lens}:
        assert f'"lens_kind":"{other_lens}"' not in prompt
    assert packet["packet_sha256"] in prompt
    assert shared["packet_sha256"] not in prompt


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


def _validation_row(**overrides):
    row = {
        "declared_command": "python -m pytest -q",
        "executed_argv": ["python3", "-m", "pytest", "-q"],
        "returncode": 1,
        "duration_seconds": 12.5,
        "stdout_tail": "noise\n1 failed, 2 passed in 3.01s",
        "stdout_truncated": True,
        "stderr_tail": "Traceback tail",
        "stderr_truncated": False,
    }
    row.update(overrides)
    return row


def test_terminal_validation_carries_the_finalizers_output_tails_and_duration():
    """The prompt promises bounded stdout/stderr under terminal_validation.

    The finalizer already retains a 4 KiB ``stdout_tail``/``stderr_tail`` and a
    ``duration_seconds`` on every executed validation row (``worker_workspace``
    record); the packet carried only the truncation flags, so a reviewer that
    wanted the pytest summary line re-ran the command it was told not to run.
    """
    packet = _packet(terminal_validation=[_validation_row()])

    row = packet["terminal_validation"][0]
    assert row["returncode"] == 1
    assert row["duration_seconds"] == 12.5
    assert row["stdout_tail"].endswith("1 failed, 2 passed in 3.01s")
    assert row["stderr_tail"] == "Traceback tail"
    assert row["stdout_truncated"] is True
    assert row["stderr_truncated"] is False


def test_validation_output_tails_are_bounded_to_what_the_finalizer_retains():
    """Never more than the finalizer's own 4 KiB, and the LAST bytes at that."""
    long_tail = "x" * 9_000 + "\n1 failed, 2 passed in 3.01s"
    packet = _packet(terminal_validation=[_validation_row(stdout_tail=long_tail)])

    row = packet["terminal_validation"][0]
    assert len(row["stdout_tail"]) == quality_reviewer.MAX_VALIDATION_OUTPUT_TAIL_CHARS
    assert row["stdout_tail"] == long_tail[-4_096:]
    assert row["stdout_tail"].endswith("1 failed, 2 passed in 3.01s")


def test_validation_rows_without_tails_or_duration_stay_well_formed():
    """A row from a fixture or an older finalizer is bounded, never rejected."""
    packet = _packet(
        terminal_validation=[
            {"declared_command": "ruff check", "executed_argv": ["ruff"], "returncode": 0}
        ]
    )

    row = packet["terminal_validation"][0]
    assert row["duration_seconds"] is None
    assert row["stdout_tail"] == "" and row["stderr_tail"] == ""


def _caller_context(**overrides):
    value = {
        "rows": [
            {
                "identity": "callers:1",
                "path": "src/other.py",
                "line": 10,
                "line_start": 5,
                "line_end": 15,
                "source": "canonical",
                "text": "def caller():\n    return mod.changed()\n",
            }
        ],
        "complete": True,
        "omitted": 0,
    }
    value.update(overrides)
    return value


def test_caller_context_is_bound_as_canonical_source_around_graph_lines():
    packet = _packet(caller_context=_caller_context())

    context = packet["candidate"]["caller_context"]
    assert context["complete"] is True and context["omitted"] == 0
    assert context["rows"][0]["path"] == "src/other.py"
    assert context["rows"][0]["source"] == "canonical"
    assert "mod.changed()" in context["rows"][0]["text"]


@pytest.mark.parametrize(
    "broken",
    [
        {"rows": [{"identity": "c", "path": "/etc/passwd", "line": 1,
                   "line_start": 1, "line_end": 1, "source": "canonical", "text": ""}]},
        {"rows": [{"identity": "c", "path": "../outside.py", "line": 1,
                   "line_start": 1, "line_end": 1, "source": "canonical", "text": ""}]},
        {"rows": [{"identity": "c", "path": "src/other.py", "line": 20,
                   "line_start": 5, "line_end": 15, "source": "canonical", "text": ""}]},
        {"rows": [{"identity": "c", "path": "src/other.py", "line": 10,
                   "line_start": 5, "line_end": 15, "source": "reviewer", "text": ""}]},
        {"complete": True, "omitted": 3},
    ],
)
def test_caller_context_fails_closed_on_anything_it_cannot_vouch_for(broken):
    """Escaped paths, a line outside its own window, a non-canonical source and
    a 'complete' record that also counts omissions are each refused."""
    with pytest.raises(
        quality_reviewer.ReviewerEvidenceError, match="invalid_caller_context"
    ):
        _packet(caller_context=_caller_context(**broken))


def test_candidate_delta_records_byte_identity_and_nothing_else():
    """The delta is a fact about bytes: no prior report, verdict or line map."""
    packet = _packet(
        candidate_delta={
            "predecessor_request_id": "req0",
            "paths": {
                "src/mod.py": {"unchanged_since_reviewed": True, "predecessor_sha256": DIGEST}
            },
        }
    )

    delta = packet["candidate"]["delta"]
    assert delta["schema_id"] == quality_reviewer.CANDIDATE_DELTA_SCHEMA_ID
    assert delta["basis"] == "changed_path_hashes"
    assert delta["predecessor_request_id"] == "req0"
    assert delta["paths"] == {
        "src/mod.py": {"unchanged_since_reviewed": True, "predecessor_sha256": DIGEST}
    }


def test_candidate_delta_must_cover_exactly_the_changed_paths():
    with pytest.raises(
        quality_reviewer.ReviewerEvidenceError, match="invalid_candidate_delta"
    ):
        _packet(
            candidate_delta={
                "predecessor_request_id": "req0",
                "paths": {
                    "src/mod.py": {"unchanged_since_reviewed": True,
                                   "predecessor_sha256": DIGEST},
                    "src/not_changed.py": {"unchanged_since_reviewed": False,
                                           "predecessor_sha256": None},
                },
            }
        )
    with pytest.raises(
        quality_reviewer.ReviewerEvidenceError, match="invalid_candidate_delta"
    ):
        _packet(
            candidate_delta={
                "predecessor_request_id": "req0",
                "paths": {"src/mod.py": {"unchanged_since_reviewed": True,
                                         "predecessor_sha256": "not-a-digest"}},
            }
        )


# --- NF-2026-01093: a rework round's predecessor->candidate hunks -----------

REWORK_HUNKS = "--- \n+++ \n@@ -1 +1 @@\n-old\n+new\n"
_ABSENT = object()


def _rework_delta(*, complete=_ABSENT, hunks=_ABSENT):
    """A one-path delta whose rework keys are present only when supplied."""
    row = {"unchanged_since_reviewed": False, "predecessor_sha256": DIGEST}
    if hunks is not _ABSENT:
        row["rework_hunks"] = hunks
    delta = {"predecessor_request_id": "req0", "paths": {"src/mod.py": row}}
    if complete is not _ABSENT:
        delta["rework_delta_complete"] = complete
    return delta


def _predecessor_review(*lenses, target="req0"):
    """``prior_findings`` holding a completed, clean report by each lens on ``target``.

    ``req0`` is the predecessor ``_rework_delta`` names, so a lens listed here has
    reviewed exactly the candidate the rework hunks start from.
    """
    prior = _prior_findings(*lenses)
    for section in prior["lenses"].values():
        for report in section["reports"]:
            report["target_request_id"] = target
    return prior


def test_a_delta_without_rework_keys_is_carried_byte_identical():
    """No rework key in, none out: the record is what it was before."""
    supplied = {
        "predecessor_request_id": "req0",
        "paths": {
            "src/mod.py": {"unchanged_since_reviewed": True, "predecessor_sha256": DIGEST}
        },
    }

    delta = _packet(candidate_delta=supplied)["candidate"]["delta"]

    assert delta == {
        "schema_id": quality_reviewer.CANDIDATE_DELTA_SCHEMA_ID,
        "basis": "changed_path_hashes",
        "predecessor_request_id": "req0",
        "paths": {
            "src/mod.py": {"unchanged_since_reviewed": True, "predecessor_sha256": DIGEST}
        },
    }


def test_a_complete_rework_delta_carries_the_flag_and_every_paths_hunks():
    packet = _packet(candidate_delta=_rework_delta(complete=True, hunks=REWORK_HUNKS))

    delta = packet["candidate"]["delta"]
    assert delta["rework_delta_complete"] is True
    assert delta["paths"]["src/mod.py"] == {
        "unchanged_since_reviewed": False,
        "predecessor_sha256": DIGEST,
        "rework_hunks": REWORK_HUNKS,
    }


def test_an_incomplete_rework_delta_carries_the_false_flag_and_no_hunks():
    delta = _packet(candidate_delta=_rework_delta(complete=False))["candidate"]["delta"]

    assert delta["rework_delta_complete"] is False
    assert "rework_hunks" not in delta["paths"]["src/mod.py"]


@pytest.mark.parametrize(
    "malformed",
    [
        _rework_delta(complete="true", hunks=REWORK_HUNKS),
        _rework_delta(complete=1, hunks=REWORK_HUNKS),
        _rework_delta(complete=None, hunks=REWORK_HUNKS),
        _rework_delta(complete=True, hunks=b"@@ -1 +1 @@"),
        _rework_delta(complete=True, hunks=None),
        _rework_delta(complete=True, hunks=["@@ -1 +1 @@"]),
        _rework_delta(
            complete=True, hunks="x" * (quality_reviewer.MAX_REWORK_HUNKS_CHARS + 1)
        ),
        _rework_delta(complete=True),
        _rework_delta(complete=False, hunks=REWORK_HUNKS),
        _rework_delta(hunks=REWORK_HUNKS),
    ],
    ids=[
        "flag-string",
        "flag-int",
        "flag-null",
        "hunks-bytes",
        "hunks-null",
        "hunks-list",
        "hunks-over-cap",
        "complete-without-hunks",
        "hunks-on-an-incomplete-delta",
        "hunks-without-the-flag",
    ],
)
def test_a_malformed_rework_delta_is_refused(malformed):
    with pytest.raises(
        quality_reviewer.ReviewerEvidenceError, match="invalid_candidate_delta"
    ):
        _packet(candidate_delta=malformed)


def test_rework_hunks_are_capped_in_total_across_paths():
    cap = quality_reviewer.MAX_REWORK_HUNKS_CHARS
    assert cap == 65_536

    at_cap = _packet(candidate_delta=_rework_delta(complete=True, hunks="x" * cap))
    assert len(at_cap["candidate"]["delta"]["paths"]["src/mod.py"]["rework_hunks"]) == cap

    def row(size):
        return {
            "unchanged_since_reviewed": False,
            "predecessor_sha256": DIGEST,
            "rework_hunks": "x" * size,
        }

    # Each path alone fits under the cap; together they do not.
    with pytest.raises(
        quality_reviewer.ReviewerEvidenceError, match="invalid_candidate_delta"
    ):
        quality_reviewer._candidate_delta_rows(
            {
                "predecessor_request_id": "req0",
                "rework_delta_complete": True,
                "paths": {"src/a.py": row(cap // 2 + 1), "src/b.py": row(cap // 2 + 1)},
            },
            changed_paths={"src/a.py", "src/b.py"},
        )


def test_prompt_renders_the_rework_surface_after_the_hunk_surface_for_a_complete_delta():
    packet = _packet(
        candidate_delta=_rework_delta(complete=True, hunks=REWORK_HUNKS),
        scoped_audits=_scoped_audits("correctness"),
        # The narrowed surface is earned by this lens's own report on the predecessor.
        prior_findings=_predecessor_review("correctness"),
    )

    prompt = quality_reviewer.build_review_prompt(packet, lens="correctness")

    surface = quality_reviewer.HUNK_REVIEW_SURFACE_INSTRUCTION
    rework = quality_reviewer.REWORK_DELTA_REVIEW_INSTRUCTION
    assert prompt.count(surface) == 1 and prompt.count(rework) == 1
    # The narrowed surface follows the general one and precedes the scope boundary.
    assert prompt.index(surface) < prompt.index(rework) < prompt.index("Review boundary")
    for stated in (
        "candidate.delta.paths[*].rework_hunks",
        "plus whether each prior finding",
        "Everything else was reviewed in the predecessor round",
        # No contradiction with the omitted-hunk reading the prompt may also carry.
        "supersedes any omitted-hunk inspection below",
    ):
        assert stated in rework
    lowered = prompt.lower()
    for removed in ("adjacent failure modes", "whole changed scope", "context beyond it"):
        assert removed not in lowered


@pytest.mark.parametrize(
    "candidate_delta",
    [None, _rework_delta(), _rework_delta(complete=False)],
    ids=["no-delta", "rework-keys-absent", "incomplete"],
)
def test_without_a_complete_delta_the_prompt_keeps_the_hunk_surface(candidate_delta):
    extra = {} if candidate_delta is None else {"candidate_delta": candidate_delta}

    prompt = quality_reviewer.build_review_prompt(_packet(**extra), lens="correctness")

    assert quality_reviewer.HUNK_REVIEW_SURFACE_INSTRUCTION in prompt
    assert quality_reviewer.REWORK_DELTA_REVIEW_INSTRUCTION not in prompt
    assert "REWORK ROUND." not in prompt


def test_a_complete_claim_without_every_paths_hunks_renders_no_rework_surface():
    """The prompt trusts the flag only beside a hunk string for EVERY path."""
    packet = _packet(
        candidate_delta=_rework_delta(complete=False),
        # The lens's own report is present, so the missing hunks are the only gap.
        prior_findings=_predecessor_review("correctness"),
    )
    packet["candidate"]["delta"]["rework_delta_complete"] = True

    prompt = quality_reviewer.build_review_prompt(_reseal(packet), lens="correctness")

    assert quality_reviewer.HUNK_REVIEW_SURFACE_INSTRUCTION in prompt
    assert quality_reviewer.REWORK_DELTA_REVIEW_INSTRUCTION not in prompt


def _candidate_file(root, relative, data):
    """Write one candidate file and return its sha256, as the packet seals it."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _unified(old, new):
    return "".join(
        difflib.unified_diff(
            old.decode("utf-8").splitlines(keepends=True),
            new.decode("utf-8").splitlines(keepends=True),
            n=3,
        )
    )


# The marker line GNU diff writes after a last line that has no newline.
_NO_EOL = "\\ No newline at end of file\n"


def test_rework_delta_hunks_diff_each_changed_path_from_predecessor_to_candidate(tmp_path):
    old = b"one\ntwo\nthree\nfour\nfive\nsix\nseven\n"
    new = b"one\ntwo\nthree\nfour\nfive\nsix\nSEVEN\neight\n"

    hunks = quality_reviewer.rework_delta_hunks(
        {"src/mod.py": old, "src/same.py": b"same\n"},
        tmp_path,
        {
            "src/mod.py": _candidate_file(tmp_path, "src/mod.py", new),
            # Identical bytes need no read: the file is not even on disk.
            "src/same.py": hashlib.sha256(b"same\n").hexdigest(),
        },
    )

    assert hunks == {"src/mod.py": _unified(old, new), "src/same.py": ""}
    assert "-seven\n+SEVEN\n+eight\n" in hunks["src/mod.py"]
    # Three lines of context, as ``difflib.unified_diff(..., n=3)`` gives.
    assert " four\n" in hunks["src/mod.py"] and " three\n" not in hunks["src/mod.py"]


def test_rework_delta_hunks_cover_a_deleted_and_a_recreated_path(tmp_path):
    recreated = _candidate_file(tmp_path, "src/back.py", b"back\n")

    hunks = quality_reviewer.rework_delta_hunks(
        {"src/gone.py": b"gone\n", "src/back.py": None},
        tmp_path,
        {"src/gone.py": None, "src/back.py": recreated},
    )

    assert hunks["src/gone.py"].endswith("@@ -1 +0,0 @@\n-gone\n")
    assert hunks["src/back.py"].endswith("@@ -0,0 +1 @@\n+back\n")


def test_rework_delta_hunks_terminate_every_line_so_edits_never_glue_together(tmp_path):
    hunks = quality_reviewer.rework_delta_hunks(
        {"src/mod.py": b"keep\nold"},
        tmp_path,
        {"src/mod.py": _candidate_file(tmp_path, "src/mod.py", b"keep\nnew")},
    )

    # Each unterminated last line ends its own line and says so, as GNU diff does.
    assert hunks["src/mod.py"].endswith(" keep\n-old\n" + _NO_EOL + "+new\n" + _NO_EOL)


def test_rework_delta_hunks_only_read_the_candidate_root(tmp_path):
    digest = _candidate_file(tmp_path, "src/mod.py", b"new\n")
    before = sorted(
        (path.relative_to(tmp_path).as_posix(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
    )

    assert quality_reviewer.rework_delta_hunks(
        {"src/mod.py": b"old\n"}, tmp_path, {"src/mod.py": digest}
    )

    assert before == sorted(
        (path.relative_to(tmp_path).as_posix(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
    )


@pytest.mark.parametrize(
    "case",
    [
        "new-since-predecessor",
        "non-utf8-candidate",
        "non-utf8-predecessor",
        "drifted-candidate",
        "missing-candidate",
        "candidate-is-a-directory",
        "over-cap",
    ],
)
def test_rework_delta_hunks_refuse_the_whole_delta_when_one_path_cannot_be_proven(
    tmp_path, case
):
    """``None`` for the WHOLE delta: a provable path never rides out alone."""
    predecessor = {"src/a_ok.py": b"a\n"}
    hashes = {"src/a_ok.py": _candidate_file(tmp_path, "src/a_ok.py", b"b\n")}
    bad = "src/bad.py"
    if case == "new-since-predecessor":
        hashes[bad] = _candidate_file(tmp_path, bad, b"new\n")
    elif case == "non-utf8-candidate":
        predecessor[bad] = b"old\n"
        hashes[bad] = _candidate_file(tmp_path, bad, b"\xff\xfe\n")
    elif case == "non-utf8-predecessor":
        predecessor[bad] = b"\xff\xfe\n"
        hashes[bad] = _candidate_file(tmp_path, bad, b"new\n")
    elif case == "drifted-candidate":
        predecessor[bad] = b"old\n"
        _candidate_file(tmp_path, bad, b"drifted\n")
        hashes[bad] = hashlib.sha256(b"sealed\n").hexdigest()
    elif case == "missing-candidate":
        predecessor[bad] = b"old\n"
        hashes[bad] = hashlib.sha256(b"never written\n").hexdigest()
    elif case == "candidate-is-a-directory":
        predecessor[bad] = b"old\n"
        (tmp_path / bad).mkdir(parents=True)
        hashes[bad] = hashlib.sha256(b"a directory\n").hexdigest()
    else:
        lines = quality_reviewer.MAX_REWORK_HUNKS_CHARS // 2
        predecessor[bad] = b"x\n" * lines
        hashes[bad] = _candidate_file(tmp_path, bad, b"y\n" * lines)

    assert quality_reviewer.rework_delta_hunks(predecessor, tmp_path, hashes) is None


def test_rework_delta_hunks_never_read_outside_the_candidate_root(tmp_path):
    root = tmp_path / "candidate"
    root.mkdir()
    outside = _candidate_file(tmp_path, "outside.py", b"secret\n")

    assert (
        quality_reviewer.rework_delta_hunks(
            {"../outside.py": b"old\n"}, root, {"../outside.py": outside}
        )
        is None
    )


def _truncated_evidence():
    """A cut-short diff: one hunk inline, one omitted, candidate digest sealed."""

    return _evidence(
        excerpt="@@ replace @@\n+kept\n",
        excerpt_bytes=20,
        truncated=True,
        diff_complete=False,
        segments=[_segment(12, 14), {**_segment(30, 33), "truncated": True}],
        omission_reason="changed_hunks_omitted:1",
    )


def test_truncated_diff_with_a_sealed_digest_is_read_through_the_overlay_not_escalated():
    """NF-2026-00931: diff_complete=false plus a matching sealed digest is a read."""

    packet = _packet(
        source_evidence=_truncated_evidence(),
        scoped_audits=_scoped_audits("correctness"),
    )

    prompt = quality_reviewer.build_review_prompt(packet, lens="correctness")

    assert "OMITTED CHANGED HUNKS." in prompt
    assert f'- "src/mod.py": candidate_sha256 {DIGEST}, 1 omitted hunk\n' in prompt
    assert "or missing or stale changed-segment evidence for any changed path" not in prompt
    # The fixture's scope names a known unknown, so that escalation must survive
    # beside the overlay instruction.
    assert "escalated as a process_limit finding" in prompt
    assert "can never support a clean result" in prompt


@pytest.mark.parametrize("lens", ["correctness", "security", "code_quality"])
def test_every_lens_prompt_binds_the_overlay_instruction_to_its_own_sealed_packet(lens):
    shared = _packet(
        source_evidence=_truncated_evidence(),
        scoped_audits=_scoped_audits("correctness", "security", "code_quality"),
    )
    packet = quality_reviewer.build_lens_packet(shared, lens=lens)

    prompt = quality_reviewer.build_review_prompt(packet, lens=lens)

    assert f'- "src/mod.py": candidate_sha256 {DIGEST}, 1 omitted hunk\n' in prompt
    assert f"packet_sha256 {packet['packet_sha256']}" in prompt
    assert shared["packet_sha256"] not in prompt


def test_deleted_candidate_path_keeps_the_ordinary_fail_closed_prompt():
    """A deleted path has no sealed digest, so no overlay can resolve it."""

    inner = {
        "task_id": "task1",
        "review_lens": {"lens_kind": "correctness"},
        "changed_paths": [{"path": "src/deleted.py"}],
        "known_unknowns": [],
    }
    audit = {
        "schema_id": "aiworkhub.scoped_audit.v1",
        "fingerprint": hashlib.sha256(
            json.dumps(
                inner, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        ).hexdigest(),
        "known_unknowns": [],
        "packet": inner,
    }
    packet = quality_reviewer.build_review_packet(
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
        scoped_audits={"correctness": audit},
    )

    prompt = quality_reviewer.build_review_prompt(packet, lens="correctness")

    assert "OMITTED CHANGED HUNKS." not in prompt
    assert (
        "or missing or stale changed-segment evidence for any changed path, must be "
        "escalated as a process_limit finding"
    ) in prompt


# --- NF-2026-01086: the manager's rework amendment rides in the sealed packet ---

REWORK_FEEDBACK = {
    "schema_id": "aiworkhub.rework_feedback_delta.v1",
    "instruction": "keep the junction guard",
    "reason_identity": "reason-1",
    "predecessor_request_id": "r0",
    "predecessor_changed_paths": ["src/mod.py"],
    "residual_identities": [],
}


def _body(packet):
    return {key: value for key, value in packet.items() if key != "packet_sha256"}


def _prior_findings(*lenses: str) -> dict:
    return {
        "predecessor_request_id": "r0",
        "omitted": 0,
        "lenses": {
            lens: {
                "reports": [
                    {
                        "reviewer_request_id": f"rev-{lens}",
                        "finding_count": 0,
                        "packet_sha256": None,
                    }
                ],
                "findings": [],
            }
            for lens in lenses
        },
    }


def test_manager_amendment_is_a_top_level_section_sealed_into_the_digest():
    plain = _packet()
    packet = _packet(manager_amendment=REWORK_FEEDBACK)

    assert packet["manager_amendment"] == {
        "schema_id": "aiworkhub.quality_review_manager_amendment.v1",
        "notice": quality_reviewer.MANAGER_AMENDMENT_NOTICE,
        "instruction": "keep the junction guard",
        "predecessor_request_id": "r0",
    }
    # Beside ``contract``, never under ``candidate``: it is contract, not evidence.
    assert "manager_amendment" not in packet["candidate"]
    assert packet["packet_sha256"] != plain["packet_sha256"]
    assert packet["packet_sha256"] == quality_reviewer._canonical_digest(_body(packet))
    assert {
        key: value for key, value in _body(packet).items() if key != "manager_amendment"
    } == _body(plain)

    forged = {
        **packet,
        "manager_amendment": {**packet["manager_amendment"], "instruction": "drop it"},
    }
    with pytest.raises(
        quality_reviewer.ReviewerEvidenceError, match="review_packet_digest_invalid"
    ):
        quality_reviewer.build_review_prompt(forged, lens="correctness")


def test_manager_amendment_notice_states_its_authority_in_the_packets_own_words():
    notice = quality_reviewer.MANAGER_AMENDMENT_NOTICE.lower()

    for stated in (
        "task manager's amendment, issued when the previous candidate was rejected",
        "part of the task contract",
        "not worker prose",
        "not evidence",
        "conflicts with contract.acceptance, the amendment wins",
        "must not be reported as a defect, as scope creep, or as an unrequested change",
        "does not lower any other requirement",
    ):
        assert stated in notice


@pytest.mark.parametrize(
    "unusable",
    [
        None,
        {},
        {"schema_id": "aiworkhub.rework_feedback_delta.v1", "predecessor_request_id": "r0"},
        {"instruction": ""},
        {"instruction": " \n\t"},
        {"instruction": None},
        {"instruction": ["keep the junction guard"]},
        "keep the junction guard",
    ],
    ids=[
        "none",
        "empty-mapping",
        "no-instruction",
        "empty-instruction",
        "blank-instruction",
        "null-instruction",
        "non-string-instruction",
        "not-a-mapping",
    ],
)
def test_an_unusable_manager_amendment_leaves_the_packet_byte_identical(unusable):
    inputs = {
        "source_evidence": _evidence(),
        "scoped_audits": _scoped_audits("correctness"),
    }
    baseline = _packet(**inputs)

    packet = _packet(manager_amendment=unusable, **inputs)

    assert "manager_amendment" not in packet
    assert json.dumps(packet, sort_keys=True) == json.dumps(baseline, sort_keys=True)
    assert packet["packet_sha256"] == baseline["packet_sha256"]


def test_manager_amendment_instruction_is_bounded_to_8000_characters():
    assert quality_reviewer.MAX_MANAGER_AMENDMENT_CHARS == 8000

    long = _packet(manager_amendment={"instruction": "a" * 8000 + "b" * 500})
    exact = _packet(manager_amendment={"instruction": "c" * 8000})

    assert long["manager_amendment"]["instruction"] == "a" * 8000
    assert exact["manager_amendment"]["instruction"] == "c" * 8000
    # The amendment names the candidate it answered, or carries an empty string.
    assert exact["manager_amendment"]["predecessor_request_id"] == ""
    lengthy = _packet(
        manager_amendment={"instruction": "x", "predecessor_request_id": "r" * 300}
    )
    assert lengthy["manager_amendment"]["predecessor_request_id"] == "r" * 200


@pytest.mark.parametrize("lens", ["correctness", "security", "code_quality"])
def test_every_lens_packet_keeps_the_manager_amendment_unchanged(lens):
    shared = _packet(
        source_evidence=_evidence(),
        scoped_audits=_scoped_audits("correctness", "security", "code_quality"),
        prior_findings=_prior_findings("correctness", "security"),
        manager_amendment=REWORK_FEEDBACK,
    )

    packet = quality_reviewer.build_lens_packet(shared, lens=lens)

    assert packet["manager_amendment"] == shared["manager_amendment"]
    # The slice really happened, and its re-seal covers the amendment.
    assert set(packet["candidate"]["scoped_audits"]) == {lens}
    assert set(packet["prior_review"]["lenses"]) <= {lens}
    assert packet["packet_sha256"] != shared["packet_sha256"]
    assert packet["packet_sha256"] == quality_reviewer._canonical_digest(_body(packet))


def test_a_packet_with_nothing_to_slice_keeps_the_manager_amendment():
    shared = _packet(
        scoped_audits=_scoped_audits("correctness"),
        manager_amendment=REWORK_FEEDBACK,
    )

    assert quality_reviewer.build_lens_packet(shared, lens="correctness") == shared


def test_prompt_sends_the_reviewer_to_the_manager_amendment_only_when_the_packet_has_one():
    line = quality_reviewer.MANAGER_AMENDMENT_PROMPT_LINE
    assert line.startswith("Read manager_amendment before judging; ")
    assert "takes precedence over contract.acceptance" in line
    assert "is not a finding" in line
    assert line.count("\n") == 1

    amended = quality_reviewer.build_review_prompt(
        _packet(manager_amendment=REWORK_FEEDBACK), lens="correctness"
    )
    assert amended.count(line) == 1
    # Instruction text ahead of the inline packet it points at, not a copy in it.
    assert amended.index(line) < amended.index("QUALITY_REVIEW_PACKET:")

    for absent in (None, {}, {"instruction": ""}):
        prompt = quality_reviewer.build_review_prompt(
            _packet(manager_amendment=absent), lens="correctness"
        )
        assert line not in prompt
        assert "manager_amendment" not in prompt


# --- NF-2026-01093: the narrowed rework surface is earned per lens ------------

REWORK_LENSES = ("correctness", "security", "code_quality")


def _narrowed(prompt):
    return quality_reviewer.REWORK_DELTA_REVIEW_INSTRUCTION in prompt


@pytest.mark.parametrize(
    ("prior", "narrowed"),
    [
        (None, False),
        (_predecessor_review("security"), False),
        (_predecessor_review("correctness", target="req-older"), False),
        (_predecessor_review("correctness"), True),
        (_predecessor_review("correctness", "security"), True),
    ],
    ids=[
        "predecessor-never-reviewed",
        "only-another-lens-reviewed-it",
        "own-report-is-on-an-earlier-round",
        "own-report-on-the-predecessor",
        "own-report-among-others",
    ],
)
def test_a_lens_narrows_only_on_its_own_completed_report_on_the_predecessor(prior, narrowed):
    packet = _packet(
        candidate_delta=_rework_delta(complete=True, hunks=REWORK_HUNKS),
        scoped_audits=_scoped_audits("correctness"),
        prior_findings=prior,
    )

    prompt = quality_reviewer.build_review_prompt(packet, lens="correctness")

    assert _narrowed(prompt) is narrowed
    # Narrowed or not, the surface every round states is there and the hunks ride along.
    assert quality_reviewer.HUNK_REVIEW_SURFACE_INSTRUCTION in prompt
    assert json.dumps(REWORK_HUNKS, ensure_ascii=False) in prompt


@pytest.mark.parametrize("omitted_hunk", [False, True], ids=["inline-diff", "omitted-hunk"])
def test_a_narrowed_prompt_fails_closed_on_known_unknowns_alone(omitted_hunk):
    """The narrowed surface says an omitted hunk is neither read nor escalated.

    A reviewer that obeyed a fail-closed sentence asking for the opposite would
    file a process_limit, and the next round would not count that lens as having
    judged the predecessor, so the narrowing would be lost.
    """
    extra = {"source_evidence": _truncated_evidence()} if omitted_hunk else {}

    def sentence(prior):
        packet = _packet(
            candidate_delta=_rework_delta(complete=True, hunks=REWORK_HUNKS),
            scoped_audits=_scoped_audits("correctness"),
            prior_findings=prior,
            **extra,
        )
        prompt = quality_reviewer.build_review_prompt(packet, lens="correctness")
        start = prompt.index("Fail closed on unknowns")
        end = prompt.index("\n", start)
        return _narrowed(prompt), prompt[start:end]

    narrowed, fail_closed = sentence(_predecessor_review("correctness"))
    assert narrowed
    assert "known_unknowns" in fail_closed
    assert "changed-segment evidence" not in fail_closed
    assert "can never support a clean result" in fail_closed

    # The same packet without an earned narrowing keeps the whole sentence.
    narrowed, fail_closed = sentence(None)
    assert not narrowed
    assert "changed-segment evidence" in fail_closed
    assert "can never support a clean result" in fail_closed


def test_a_predecessor_reviewed_by_correctness_alone_narrows_only_that_lens():
    shared = _packet(
        candidate_delta=_rework_delta(complete=True, hunks=REWORK_HUNKS),
        scoped_audits=_scoped_audits(*REWORK_LENSES),
        prior_findings=_predecessor_review("correctness"),
    )

    for lens in REWORK_LENSES:
        # The packet the reviewer receives, and the shared one it was sliced from.
        for packet in (quality_reviewer.build_lens_packet(shared, lens=lens), shared):
            prompt = quality_reviewer.build_review_prompt(packet, lens=lens)

            assert _narrowed(prompt) is (lens == "correctness")
            assert quality_reviewer.HUNK_REVIEW_SURFACE_INSTRUCTION in prompt


def _hand_built_packet(*hunks, prior=None):
    """A mapping shaped just far enough for ``_rework_delta_instruction`` to judge."""
    packet = {
        "candidate": {
            "delta": {
                "predecessor_request_id": "req0",
                "rework_delta_complete": True,
                "paths": {
                    f"src/f{index}.py": {"rework_hunks": hunk}
                    for index, hunk in enumerate(hunks)
                },
            }
        }
    }
    if prior is not None:
        packet["prior_review"] = prior
    return packet


@pytest.mark.parametrize(
    ("hunks", "narrowed"),
    [
        (("",), False),
        (("", ""), False),
        ((REWORK_HUNKS,), True),
        (("", REWORK_HUNKS), True),
    ],
    ids=[
        "one-empty-path",
        "every-path-empty",
        "one-changed-path",
        "a-changed-path-among-unchanged",
    ],
)
def test_narrowing_needs_at_least_one_non_empty_rework_hunk(hunks, narrowed):
    packet = _hand_built_packet(*hunks, prior=_predecessor_review("correctness"))

    instruction = quality_reviewer._rework_delta_instruction(packet, lens="correctness")

    assert instruction == (
        quality_reviewer.REWORK_DELTA_REVIEW_INSTRUCTION if narrowed else ""
    )


def test_an_all_empty_rework_delta_keeps_the_hunk_surface_even_with_the_lens_report():
    packet = _packet(
        candidate_delta=_rework_delta(complete=True, hunks=""),
        prior_findings=_predecessor_review("correctness"),
    )

    prompt = quality_reviewer.build_review_prompt(packet, lens="correctness")

    assert not _narrowed(prompt)
    assert quality_reviewer.HUNK_REVIEW_SURFACE_INSTRUCTION in prompt


@pytest.mark.parametrize(
    "prior",
    [
        [],
        {"lenses": []},
        {"lenses": {"correctness": None}},
        {"lenses": {"correctness": {"reports": "req0"}}},
        {"lenses": {"correctness": {"reports": ["req0"]}}},
        {"lenses": {"correctness": {"reports": [{"target_request_id": None}]}}},
        {"lenses": {"correctness": {"reports": [{"target_request_id": ["req0"]}]}}},
    ],
    ids=[
        "not-a-mapping",
        "lenses-not-a-mapping",
        "section-null",
        "reports-a-string",
        "report-a-string",
        "target-null",
        "target-a-list",
    ],
)
def test_a_malformed_prior_review_never_narrows_and_never_raises(prior):
    packet = _hand_built_packet(REWORK_HUNKS, prior=prior)

    assert quality_reviewer._rework_delta_instruction(packet, lens="correctness") == ""


def test_a_blank_predecessor_identity_never_matches_a_blank_report_target():
    packet = _hand_built_packet(
        REWORK_HUNKS, prior=_predecessor_review("correctness", target="")
    )
    packet["candidate"]["delta"]["predecessor_request_id"] = ""

    assert quality_reviewer._rework_delta_instruction(packet, lens="correctness") == ""


# --- NF-2026-01093: a report counts only when it JUDGED the predecessor ------------


def _finding_row(reviewer, disposition, index=0):
    """One prior finding as ``build_review_packet`` validates it: no path, no cited line."""
    return {
        "reviewer_request_id": reviewer,
        "finding_id": f"{reviewer}-{index}",
        "severity": "low",
        "disposition": disposition,
        "actionable": disposition == "defect",
        "summary": f"a {disposition}",
        "path": None,
        "line_start": None,
        "line_end": None,
        "status": "lines_changed",
        "line_mapping": "unavailable",
        "path_in_candidate": False,
        "overlaps_current_hunk": None,
    }


def _report(**fields):
    return {
        "target_request_id": "req0",
        "reviewer_request_id": "rev",
        "finding_count": 0,
        "packet_sha256": None,
        **fields,
    }


def _judged_review(*dispositions, count=None):
    """A correctness report on the predecessor that filed one finding per disposition.

    ``count`` replaces the report's own ``finding_count``: what it filed when the
    per-lens cap listed fewer of its findings than it wrote.
    """
    prior = _predecessor_review("correctness")
    section = prior["lenses"]["correctness"]
    section["findings"] = [
        _finding_row("rev-correctness", disposition, index)
        for index, disposition in enumerate(dispositions)
    ]
    section["reports"][0]["finding_count"] = (
        len(dispositions) if count is None else count
    )
    return prior


def _judged_instruction(prior):
    packet = _packet(
        candidate_delta=_rework_delta(complete=True, hunks=REWORK_HUNKS),
        prior_findings=prior,
    )
    return quality_reviewer._rework_delta_instruction(packet, lens="correctness")


_PRIOR_CAP = quality_reviewer.MAX_PRIOR_FINDINGS_PER_LENS


@pytest.mark.parametrize(
    ("prior", "narrowed"),
    [
        (_judged_review(), True),
        (_judged_review("defect"), True),
        (_judged_review("defect", "observation"), True),
        (_judged_review("process_limit"), False),
        (_judged_review("defect", "process_limit"), False),
        (_judged_review("defect", "observation", count=3), False),
        (_judged_review("defect", "observation", count=1), False),
        (_judged_review(*(["defect"] * _PRIOR_CAP), count=_PRIOR_CAP + 1), False),
    ],
    ids=[
        "clean-report",
        "one-defect",
        "defect-and-observation",
        "one-process-limit",
        "process-limit-among-defects",
        "a-finding-the-cap-dropped",
        "more-findings-listed-than-counted",
        "one-finding-past-the-cap",
    ],
)
def test_a_lens_narrows_only_on_a_report_that_judged_the_predecessor(prior, narrowed):
    assert _judged_instruction(prior) == (
        quality_reviewer.REWORK_DELTA_REVIEW_INSTRUCTION if narrowed else ""
    )


def test_a_report_that_could_not_read_the_packet_leaves_the_hunk_surface():
    packet = _packet(
        candidate_delta=_rework_delta(complete=True, hunks=REWORK_HUNKS),
        scoped_audits=_scoped_audits("correctness"),
        prior_findings=_judged_review("process_limit"),
    )

    prompt = quality_reviewer.build_review_prompt(packet, lens="correctness")

    assert not _narrowed(prompt)
    assert quality_reviewer.HUNK_REVIEW_SURFACE_INSTRUCTION in prompt
    assert json.dumps(REWORK_HUNKS, ensure_ascii=False) in prompt


def test_another_reports_process_limit_does_not_unjudge_a_clean_report_on_the_predecessor():
    """A blind first read and a blind earlier round sit beside the clean re-read."""
    prior = _judged_review()
    section = prior["lenses"]["correctness"]
    section["reports"] += [
        _report(reviewer_request_id="rev-blind", finding_count=1),
        _report(reviewer_request_id="rev-older", finding_count=1, target_request_id="req-older"),
    ]
    section["findings"] = [
        _finding_row("rev-blind", "process_limit"),
        _finding_row("rev-older", "process_limit"),
    ]

    assert _judged_instruction(prior) == quality_reviewer.REWORK_DELTA_REVIEW_INSTRUCTION


def test_a_clean_report_on_an_earlier_round_does_not_stand_in_for_a_blind_one_on_the_predecessor():
    prior = _judged_review("process_limit")
    prior["lenses"]["correctness"]["reports"].append(
        _report(reviewer_request_id="rev-older", target_request_id="req-older")
    )

    assert _judged_instruction(prior) == ""


@pytest.mark.parametrize(
    ("section", "narrowed"),
    [
        ({"reports": [_report()], "findings": []}, True),
        ({"reports": [_report(finding_count=False)], "findings": []}, False),
        ({"reports": [_report(finding_count="0")], "findings": []}, False),
        ({"reports": [_report(finding_count=None)], "findings": []}, False),
        ({"reports": [_report(reviewer_request_id="")], "findings": []}, False),
        ({"reports": [_report(reviewer_request_id=None)], "findings": []}, False),
        ({"reports": [_report(reviewer_request_id=["rev"])], "findings": []}, False),
        ({"reports": [_report()], "findings": None}, False),
        ({"reports": [_report()], "findings": {"rev": []}}, False),
        ({"reports": [_report()]}, False),
        ({"reports": [_report(finding_count=1)], "findings": ["process_limit"]}, False),
        ({"reports": [_report(finding_count=1)], "findings": [None]}, False),
    ],
    ids=[
        "readable-report",
        "count-a-bool",
        "count-a-string",
        "count-null",
        "reviewer-blank",
        "reviewer-null",
        "reviewer-a-list",
        "findings-null",
        "findings-a-mapping",
        "findings-missing",
        "listed-finding-a-string",
        "listed-finding-null",
    ],
)
def test_a_report_the_gate_cannot_read_never_narrows_and_never_raises(section, narrowed):
    packet = _hand_built_packet(REWORK_HUNKS, prior={"lenses": {"correctness": section}})

    assert quality_reviewer._rework_delta_instruction(packet, lens="correctness") == (
        quality_reviewer.REWORK_DELTA_REVIEW_INSTRUCTION if narrowed else ""
    )


# --- NF-2026-01093: an incomplete delta may say why -------------------------------


def _incomplete_with_reason(reason):
    delta = _rework_delta(complete=False)
    delta["rework_delta_fallback_reason"] = reason
    return delta


def test_an_incomplete_delta_carries_its_fallback_reason_and_no_hunks():
    delta = _packet(candidate_delta=_incomplete_with_reason("no_delta_artifact"))[
        "candidate"
    ]["delta"]

    assert delta["rework_delta_complete"] is False
    assert delta["rework_delta_fallback_reason"] == "no_delta_artifact"
    assert "rework_hunks" not in delta["paths"]["src/mod.py"]


@pytest.mark.parametrize("supplied", [_rework_delta(complete=False), _incomplete_with_reason(None)])
def test_a_delta_without_a_fallback_reason_carries_none(supplied):
    delta = _packet(candidate_delta=supplied)["candidate"]["delta"]

    assert "rework_delta_fallback_reason" not in delta


def test_an_over_long_fallback_reason_is_cut_rather_than_refused():
    cap = quality_reviewer.MAX_REWORK_FALLBACK_REASON_CHARS
    assert cap == 200

    delta = _packet(candidate_delta=_incomplete_with_reason("r" * (cap + 50)))[
        "candidate"
    ]["delta"]

    assert delta["rework_delta_fallback_reason"] == "r" * cap


@pytest.mark.parametrize(
    "delta",
    [
        _incomplete_with_reason(""),
        _incomplete_with_reason(7),
        _incomplete_with_reason(b"no_delta_artifact"),
        _incomplete_with_reason(["no_delta_artifact"]),
        {**_rework_delta(), "rework_delta_fallback_reason": "no_delta_artifact"},
    ],
    ids=["empty", "int", "bytes", "list", "reason-without-the-flag"],
)
def test_a_malformed_fallback_reason_is_refused(delta):
    with pytest.raises(
        quality_reviewer.ReviewerEvidenceError, match="invalid_candidate_delta"
    ):
        _packet(candidate_delta=delta)


def test_a_complete_delta_cannot_carry_a_fallback_reason():
    delta = _rework_delta(complete=True, hunks=REWORK_HUNKS)
    delta["rework_delta_fallback_reason"] = "no_delta_artifact"

    with pytest.raises(
        quality_reviewer.ReviewerEvidenceError, match="invalid_candidate_delta"
    ):
        _packet(candidate_delta=delta)


# --- NF-2026-01093: hunk fidelity and a cap enforced as the diff is produced -----


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (
            b"keep\nold",
            b"keep\nnew",
            "--- \n+++ \n@@ -1,2 +1,2 @@\n keep\n-old\n" + _NO_EOL + "+new\n" + _NO_EOL,
        ),
        (
            b"keep\nold",
            b"keep\nnew\n",
            "--- \n+++ \n@@ -1,2 +1,2 @@\n keep\n-old\n" + _NO_EOL + "+new\n",
        ),
        (
            b"keep\nold\n",
            b"keep\nnew",
            "--- \n+++ \n@@ -1,2 +1,2 @@\n keep\n-old\n+new\n" + _NO_EOL,
        ),
        (
            b"a\nkeep",
            b"b\nkeep",
            "--- \n+++ \n@@ -1,2 +1,2 @@\n-a\n+b\n keep\n" + _NO_EOL,
        ),
        (
            b"same",
            b"same\n",
            "--- \n+++ \n@@ -1 +1 @@\n-same\n" + _NO_EOL + "+same\n",
        ),
    ],
    ids=[
        "neither-side-ends-in-a-newline",
        "only-the-old-side",
        "only-the-new-side",
        "an-unchanged-last-line-is-marked-as-context",
        "only-the-final-newline-differs",
    ],
)
def test_rework_delta_hunks_mark_a_last_line_that_has_no_newline(
    tmp_path, old, new, expected
):
    """As GNU diff writes it, so the hunk never claims a newline the file lacks."""
    hunks = quality_reviewer.rework_delta_hunks(
        {"src/mod.py": old},
        tmp_path,
        {"src/mod.py": _candidate_file(tmp_path, "src/mod.py", new)},
    )

    assert hunks == {"src/mod.py": expected}


def test_rework_delta_hunks_mark_no_newline_only_where_the_file_ends_without_one(tmp_path):
    # A form feed ends neither a line nor the file, whatever ``splitlines`` says.
    old, new = b"a\x0cb\nkeep\n", b"a\x0cc\nkeep\n"

    hunks = quality_reviewer.rework_delta_hunks(
        {"src/mod.py": old},
        tmp_path,
        {"src/mod.py": _candidate_file(tmp_path, "src/mod.py", new)},
    )

    assert "No newline" not in hunks["src/mod.py"]
    assert hunks["src/mod.py"].endswith("-a\x0cb\n+a\x0cc\n keep\n")


@pytest.mark.parametrize(
    "separator",
    ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", chr(0x2028), chr(0x2029), "\r"],
    ids=["vt", "ff", "fs", "gs", "rs", "nel", "ls", "ps", "cr"],
)
def test_rework_delta_hunks_number_lines_by_newline_alone(tmp_path, separator):
    """``str.splitlines`` breaks at these too, so every ``@@`` number below one would drift."""
    old = f"a{separator}b\nk1\nk2\nk3\nold\ntail\n".encode()
    new = old.replace(b"old", b"new")

    hunks = quality_reviewer.rework_delta_hunks(
        {"src/mod.py": old},
        tmp_path,
        {"src/mod.py": _candidate_file(tmp_path, "src/mod.py", new)},
    )

    # ``old`` is line 5 of the file: three lines of context above it, one below.
    assert hunks["src/mod.py"] == (
        "--- \n+++ \n@@ -2,5 +2,5 @@\n k1\n k2\n k3\n-old\n+new\n tail\n"
    )


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (
            b"one\r\ntwo\r\nthree\r\n",
            b"one\r\nTWO\r\nthree\r\n",
            "--- \n+++ \n@@ -1,3 +1,3 @@\n one\r\n-two\r\n+TWO\r\n three\r\n",
        ),
        (
            b"a\r\nb\r",
            b"a\r\nc\r",
            "--- \n+++ \n@@ -1,2 +1,2 @@\n a\r\n-b\r\n" + _NO_EOL + "+c\r\n" + _NO_EOL,
        ),
    ],
    ids=["crlf-lines", "a-carriage-return-ends-the-file-without-a-newline"],
)
def test_rework_delta_hunks_keep_a_carriage_return_inside_its_line(
    tmp_path, old, new, expected
):
    """The ``\\r`` stays where the file has it: a CRLF file is diffed byte for byte."""
    hunks = quality_reviewer.rework_delta_hunks(
        {"src/mod.py": old},
        tmp_path,
        {"src/mod.py": _candidate_file(tmp_path, "src/mod.py", new)},
    )

    assert hunks == {"src/mod.py": expected}


def test_rework_delta_hunks_stop_at_the_cap_instead_of_building_the_whole_diff(
    tmp_path, monkeypatch
):
    """The cap is enforced as diff lines arrive: an endless diff is refused, not joined."""
    line = "+" + "x" * 99 + "\n"
    pulled = 0

    def endless(*_args, **_kwargs):
        nonlocal pulled
        while True:
            pulled += 1
            yield line

    monkeypatch.setattr(quality_reviewer.difflib, "unified_diff", endless)
    digest = _candidate_file(tmp_path, "src/mod.py", b"new\n")

    assert (
        quality_reviewer.rework_delta_hunks(
            {"src/mod.py": b"old\n"}, tmp_path, {"src/mod.py": digest}
        )
        is None
    )
    assert pulled == quality_reviewer.MAX_REWORK_HUNKS_CHARS // len(line) + 1


def test_rework_delta_hunks_cap_the_running_total_across_paths(tmp_path):
    """Each path fits under the cap alone; together they do not."""
    lines = quality_reviewer.MAX_REWORK_HUNKS_CHARS // 10
    old, new = b"x\n" * lines, b"y\n" * lines
    predecessor = {"src/a.py": old, "src/b.py": old}
    hashes = {path: _candidate_file(tmp_path, path, new) for path in predecessor}

    for path in predecessor:
        alone = quality_reviewer.rework_delta_hunks(
            {path: old}, tmp_path, {path: hashes[path]}
        )
        assert alone is not None
        assert len(alone[path]) <= quality_reviewer.MAX_REWORK_HUNKS_CHARS
    assert quality_reviewer.rework_delta_hunks(predecessor, tmp_path, hashes) is None


def test_rework_delta_hunks_give_a_small_hunk_for_a_one_line_change_in_a_large_file(
    tmp_path,
):
    """Only the size of the DIFF is capped: a big file with a small change still narrows."""
    old = "".join(f"line {number}\n" for number in range(60_000)).encode("utf-8")
    new = old.replace(b"line 30000\n", b"line 30000 changed\n")
    assert len(new) > 8 * quality_reviewer.MAX_REWORK_HUNKS_CHARS

    hunks = quality_reviewer.rework_delta_hunks(
        {"src/big.py": old},
        tmp_path,
        {"src/big.py": _candidate_file(tmp_path, "src/big.py", new)},
    )

    assert hunks == {"src/big.py": _unified(old, new)}
    assert len(hunks["src/big.py"]) < 400
    assert "-line 30000\n+line 30000 changed\n" in hunks["src/big.py"]
