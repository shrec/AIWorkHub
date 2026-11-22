"""NF-2026-02353: the prior_review section is bounded by bytes, not only by count."""

from __future__ import annotations

import hashlib

from aiworkhub import quality_reviewer


SOURCE = "print('candidate marker')\n"
DIGEST = hashlib.sha256(SOURCE.encode("utf-8")).hexdigest()
PATH = "src/mod.py"
LENSES = ("code_quality", "correctness", "security")
SEVERITY = sorted(quality_reviewer.FINDING_SEVERITIES)[0]
DISPOSITION = sorted(quality_reviewer.FINDING_DISPOSITIONS)[0]


def _packet(**kwargs):
    return quality_reviewer.build_review_packet(
        request_id="req1",
        task_id="task1",
        claim_epoch=1,
        worker_provider="adapter-a",
        changed_path_hashes={PATH: DIGEST},
        **kwargs,
    )


def _rank(*, unchanged: bool, actionable: bool) -> int:
    # Drop order: lines_changed before lines_unchanged, non-actionable first.
    return 2 * int(unchanged) + int(actionable)


def _finding(lens, index, *, unchanged, actionable, summary_chars):
    return {
        "reviewer_request_id": f"rev-{lens}-0",
        "finding_id": f"{lens}:{index}:{_rank(unchanged=unchanged, actionable=actionable)}",
        "severity": SEVERITY,
        "disposition": DISPOSITION,
        "actionable": actionable,
        "summary": "s" * summary_chars,
        "path": PATH,
        "line_start": 1,
        "line_end": 1,
        "status": "lines_unchanged" if unchanged else "lines_changed",
        "line_mapping": "identity" if unchanged else "unavailable",
        "path_in_candidate": True,
        "overlaps_current_hunk": None,
    }


def _reports(lens):
    return [
        {
            "reviewer_request_id": f"rev-{lens}-{number}",
            "reviewer_task_id": f"task-{lens}-{number}",
            "reviewer_provider": "provider-a",
            "target_request_id": f"req-{number}",
            "finding_count": 1,
            "packet_sha256": DIGEST,
        }
        for number in range(quality_reviewer.MAX_PRIOR_REPORTS_PER_LENS)
    ]


def _maximal(omitted=3):
    cap = quality_reviewer.MAX_PRIOR_FINDINGS_PER_LENS
    return {
        "predecessor_request_id": "r0",
        "omitted": omitted,
        "lenses": {
            lens: {
                "reports": _reports(lens),
                "findings": [
                    _finding(
                        lens,
                        index,
                        unchanged=index % 2 == 0,
                        actionable=index % 3 == 0,
                        summary_chars=quality_reviewer.MAX_TEXT_CHARS,
                    )
                    for index in range(cap)
                ],
            }
            for lens in LENSES
        },
    }


def _rows(value):
    return quality_reviewer._prior_findings_rows(value, changed_paths={PATH})


def _size(payload) -> int:
    return len(quality_reviewer._canonical_bytes(payload))


def _parse(finding_id):
    lens, index, rank = finding_id.split(":")
    return lens, int(index), int(rank)


def test_maximal_history_is_bounded_by_bytes_and_keeps_every_report():
    value = _maximal(omitted=3)
    section = _rows(value)

    assert _size(section) <= quality_reviewer.MAX_PRIOR_REVIEW_BYTES
    assert section["complete"] is False
    total = sum(len(value["lenses"][lens]["findings"]) for lens in LENSES)
    kept = sum(len(section["lenses"][lens]["findings"]) for lens in LENSES)
    assert 0 < kept < total
    assert section["omitted"] == 3 + (total - kept)
    for lens in LENSES:
        rows = section["lenses"][lens]
        assert [row["reviewer_request_id"] for row in rows["reports"]] == [
            report["reviewer_request_id"] for report in value["lenses"][lens]["reports"]
        ]
        assert rows["unchanged_line_findings"] == sum(
            1 for row in rows["findings"] if row["status"] == "lines_unchanged"
        )
        for row in rows["findings"]:
            assert row["source"] == "prior_reviewer_report"
            assert len(row["summary"]) == quality_reviewer.MAX_TEXT_CHARS
    assert section["notice"] == quality_reviewer.PRIOR_FINDINGS_NOTICE
    assert section["schema_id"] == quality_reviewer.PRIOR_FINDINGS_SCHEMA_ID


def test_unchanged_and_actionable_rows_are_kept_in_preference():
    cap = quality_reviewer.MAX_PRIOR_FINDINGS_PER_LENS
    classes = [(False, False), (False, True), (True, False), (True, True)]
    value = _maximal(omitted=0)
    for lens in LENSES:
        value["lenses"][lens]["findings"] = [
            _finding(
                lens,
                index,
                unchanged=classes[index % 4][0],
                actionable=classes[index % 4][1],
                # The most relevant class is small enough to fit whole.
                summary_chars=40 if classes[index % 4] == (True, True) else 2_000,
            )
            for index in range(cap)
        ]
    section = _rows(value)

    assert _size(section) <= quality_reviewer.MAX_PRIOR_REVIEW_BYTES
    kept = {
        _parse(row["finding_id"])
        for lens in LENSES
        for row in section["lenses"][lens]["findings"]
    }
    every = {
        _parse(row["finding_id"])
        for lens in LENSES
        for row in value["lenses"][lens]["findings"]
    }
    dropped = every - kept
    assert dropped and kept
    assert section["omitted"] == len(dropped)
    assert section["complete"] is False
    # Every lines_unchanged + actionable row survives; every
    # lines_changed + non-actionable row is dropped first.
    assert {item for item in every if item[2] == 3} <= kept
    assert {item for item in every if item[2] == 0} <= dropped
    # Strict class order: no kept row ranks below a dropped one.
    assert max(item[2] for item in dropped) <= min(item[2] for item in kept)
    # Within the boundary class the OLDEST (end of list) go first, round-robin.
    boundary = max(item[2] for item in dropped)
    for lens in LENSES:
        indexes = sorted(i for (name, i, rank) in every if name == lens and rank == boundary)
        kept_here = sorted(i for (name, i, rank) in kept if name == lens and rank == boundary)
        assert kept_here == indexes[: len(kept_here)]
    per_lens = [
        sum(1 for (name, _i, rank) in kept if name == lens and rank == boundary)
        for lens in LENSES
    ]
    assert max(per_lens) - min(per_lens) <= 1
    # Kept rows keep their original relative order.
    for lens in LENSES:
        order = [_parse(row["finding_id"])[1] for row in section["lenses"][lens]["findings"]]
        assert order == sorted(order)


def test_within_budget_section_is_byte_identical(monkeypatch):
    value = _maximal(omitted=2)
    for lens in LENSES:
        value["lenses"][lens]["findings"] = value["lenses"][lens]["findings"][:3]
        for row in value["lenses"][lens]["findings"]:
            row["summary"] = "short"
    bounded = _rows(value)
    monkeypatch.setattr(quality_reviewer, "MAX_PRIOR_REVIEW_BYTES", 1 << 40)
    unbounded = _rows(value)

    assert quality_reviewer._canonical_bytes(bounded) == quality_reviewer._canonical_bytes(
        unbounded
    )
    assert bounded["omitted"] == 2
    assert bounded["complete"] is False
    for lens in LENSES:
        assert len(bounded["lenses"][lens]["findings"]) == 3
    section = {**bounded}
    assert quality_reviewer._bound_prior_review_bytes(section) is section

    clean = {**value, "omitted": 0}
    assert _rows(clean)["complete"] is True
    assert _rows(clean)["omitted"] == 0


def test_packet_size_does_not_scale_with_history():
    plain = _packet()
    heavy = _packet(prior_findings=_maximal(omitted=0))

    assert _size(heavy) <= _size(plain) + quality_reviewer.MAX_PRIOR_REVIEW_BYTES + 1024
    assert heavy["packet_sha256"] == quality_reviewer._canonical_digest(
        {key: value for key, value in heavy.items() if key != "packet_sha256"}
    )
    for lens in LENSES:
        sliced = quality_reviewer.build_lens_packet(heavy, lens=lens)
        assert set(sliced["prior_review"]["lenses"]) == {lens}
        assert sliced["prior_review"]["lenses"][lens]["reports"] == (
            heavy["prior_review"]["lenses"][lens]["reports"]
        )
