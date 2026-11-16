"""NF-2026-02305: a rework round never narrows a lens that did not judge the predecessor.

Field report (their NF-106): round 1 added 5 NEW files and ended
``validation_failed`` (no reviewer ran); the automatic rework round 2 edited one
of them, and both reviewers were told that the other four were "previously
reviewed context, not new review surface", leaving only the rework hunk.  The
reviewer surface for such a round is the whole candidate against the task base;
narrowing is earned per lens, by that lens's own judged report on the predecessor.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from aiworkhub import process_launcher, quality_evidence, quality_reviewer, worker_workspace

TASK_ID = "NF-2026-02305-REWORK-SURFACE"
PREDECESSOR_REQUEST_ID = "d" * 32
CANDIDATE_REQUEST_ID = "e" * 32
LENSES = tuple(quality_evidence.JUDGMENT_LENSES)
NEW_PATHS = tuple(f"src/new_{index}.py" for index in range(5))
EDITED_PATH = NEW_PATHS[0]
UNEDITED_PATHS = NEW_PATHS[1:]
ROUND_ONE = {
    path: f"value_{index} = 1\n# sentinel-round1-{index}\n".encode()
    for index, path in enumerate(NEW_PATHS)
}
ROUND_TWO = {**ROUND_ONE, EDITED_PATH: b"value_0 = 2\n"}
# The sentence ``build_review_prompt`` renders for a path the predecessor already holds.
UNCHANGED_SENTENCE = "as unchanged_since_reviewed: recognize"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scoped_audit(lens: str) -> dict[str, object]:
    payload = {
        "task_id": TASK_ID,
        "review_lens": {"lens_kind": lens},
        "changed_paths": [{"path": path} for path in NEW_PATHS],
    }
    import json

    fingerprint = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_id": "aiworkhub.scoped_audit.v1",
        "fingerprint": fingerprint,
        "packet": payload,
    }


def _predecessor_reviewed_by(monkeypatch: pytest.MonkeyPatch, *lenses: str) -> None:
    """Make the collector find a completed, clean report by each lens on round 1."""

    def receipts(_manager: object, _task_id: str, _exclude: str):
        return (
            [
                {
                    "lens": lens,
                    "reviewer_task_id": f"reviewer-{lens}",
                    "reviewer_request_id": f"rev-{lens}",
                    "reviewer_provider": "reviewer_adapter",
                    "packet_sha256": "",
                    "target_request_id": PREDECESSOR_REQUEST_ID,
                    "findings": [],
                }
                for lens in lenses
            ],
            {},
        )

    monkeypatch.setattr(process_launcher.ProcessManager, "_prior_reviewer_receipts", receipts)


def _round_two_packet(tmp_path: Path) -> dict[str, object]:
    """The packet production builds for round 2, from the real launcher helpers."""
    from test_process_launcher import _manager

    manager = _manager(
        tmp_path,
        show_task=lambda _task_id: {"returncode": 1, "stdout": "", "stderr": ""},
        argv=[sys.executable, "-c", "pass"],
    )
    path = tmp_path / "worktrees" / CANDIDATE_REQUEST_ID / "worktree"
    home = tmp_path / "worktrees" / CANDIDATE_REQUEST_ID / "home"
    (path / "src").mkdir(parents=True)
    home.mkdir(parents=True)
    for name, data in ROUND_TWO.items():
        (path / name).write_bytes(data)
    workspace = process_launcher.WorkerWorkspace(
        request_id=CANDIDATE_REQUEST_ID,
        repo=manager.repo,
        path=path,
        home=home,
        allowed_writes=NEW_PATHS,
        parent_baseline={},
        workspace_baseline={},
    )
    artifact = worker_workspace.seal_rework_delta_artifact(
        manager.repo,
        TASK_ID,
        PREDECESSOR_REQUEST_ID,
        1,
        ROUND_ONE.items(),
        tmp_path / "rework_deltas",
    )
    card = {
        "allowed_writes": list(NEW_PATHS),
        "rework_predecessor": {
            "request_id": PREDECESSOR_REQUEST_ID,
            "task_id": TASK_ID,
            "claim_epoch": 1,
            "changed_path_hashes": {p: _sha256(d) for p, d in ROUND_ONE.items()},
            "delta_artifact": artifact,
        },
    }
    hashes = {name: _sha256(data) for name, data in ROUND_TWO.items()}
    source_evidence = manager._quality_review_source_evidence(workspace, hashes)
    delta = manager._quality_review_candidate_delta(card, hashes, workspace)
    assert delta is not None and delta["rework_delta_complete"] is True
    prior = manager._quality_review_prior_findings(
        target_task_id=TASK_ID,
        target_request_id=CANDIDATE_REQUEST_ID,
        current_hashes=hashes,
        source_evidence=source_evidence,
        predecessor_request_id=PREDECESSOR_REQUEST_ID,
    )
    return quality_reviewer.build_review_packet(
        request_id=CANDIDATE_REQUEST_ID,
        task_id=TASK_ID,
        claim_epoch=1,
        worker_provider="worker_adapter",
        changed_path_hashes=hashes,
        objective="add five modules",
        acceptance=["all five modules exist"],
        required_outputs=list(NEW_PATHS),
        validation=["python -m pytest -q"],
        source_evidence=source_evidence,
        scoped_audits={lens: _scoped_audit(lens) for lens in LENSES},
        candidate_delta=delta,
        prior_findings=prior,
    )


def _prompt(packet: dict[str, object], lens: str) -> str:
    return quality_reviewer.build_review_prompt(
        quality_reviewer.build_lens_packet(packet, lens=lens), lens=lens
    )


def _delta_rows(lens_packet: dict[str, object]) -> dict[str, dict[str, object]]:
    return lens_packet["candidate"]["delta"]["paths"]  # type: ignore[index,return-value]


def _assert_full_surface(packet: dict[str, object], lens: str, prompt: str) -> None:
    """All five new files are on ``lens``'s surface, none is called previously reviewed."""
    lens_packet = quality_reviewer.build_lens_packet(packet, lens=lens)
    rows = {row["path"]: row for row in lens_packet["candidate"]["source_evidence"]}  # type: ignore[index]
    assert set(rows) == set(NEW_PATHS)
    for name in NEW_PATHS:
        # Against the task base every new file is entirely added, edited or not.
        assert rows[name]["excerpt"].count("+") >= 1, name
        assert rows[name]["diff_complete"] is True, name
    delta_rows = _delta_rows(lens_packet)
    assert set(delta_rows) == set(NEW_PATHS)
    for name in NEW_PATHS:
        # The sealed lens packet itself must not say "already reviewed".
        assert delta_rows[name]["unchanged_since_reviewed"] is not True, name
        assert "rework_hunks" not in delta_rows[name], name
    for name in UNEDITED_PATHS:
        # A line only round 1 wrote, in a file round 2 never touched, is on the surface.
        assert f"sentinel-round1-{NEW_PATHS.index(name)}" in rows[name]["excerpt"], name
    assert quality_reviewer.REWORK_DELTA_REVIEW_INSTRUCTION not in prompt
    assert UNCHANGED_SENTENCE not in prompt
    for name in UNEDITED_PATHS:
        assert name in prompt


def test_round_two_after_an_unreviewed_round_one_keeps_every_new_file_on_the_surface(
    tmp_path: Path,
) -> None:
    packet = _round_two_packet(tmp_path)

    delta = packet["candidate"]["delta"]  # type: ignore[index]
    assert [p for p, row in delta["paths"].items() if row["unchanged_since_reviewed"]] == sorted(
        UNEDITED_PATHS
    )
    assert "prior_review" not in packet
    for lens in LENSES:
        _assert_full_surface(packet, lens, _prompt(packet, lens))


def test_only_the_lens_that_judged_round_one_narrows_the_others_keep_the_full_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _predecessor_reviewed_by(monkeypatch, "correctness")
    packet = _round_two_packet(tmp_path)

    assert set(packet["prior_review"]["lenses"]) == {"correctness"}  # type: ignore[index]
    prompts = {lens: _prompt(packet, lens) for lens in LENSES}
    # Unchanged behavior for the lens that reviewed the predecessor.
    assert quality_reviewer.REWORK_DELTA_REVIEW_INSTRUCTION in prompts["correctness"]
    assert UNCHANGED_SENTENCE in prompts["correctness"]
    narrowed = _delta_rows(quality_reviewer.build_lens_packet(packet, lens="correctness"))
    for name in UNEDITED_PATHS:
        assert narrowed[name]["unchanged_since_reviewed"] is True, name
        assert "rework_hunks" in narrowed[name], name
    for lens in LENSES:
        if lens != "correctness":
            _assert_full_surface(packet, lens, prompts[lens])


def test_widening_keeps_a_launcher_sealed_fallback_reason() -> None:
    lens_body = {
        "candidate": {
            "delta": {
                "rework_delta_complete": False,
                "rework_delta_fallback_reason": "launcher_reason",
                "paths": {
                    EDITED_PATH: {"unchanged_since_reviewed": True, "rework_hunks": []},
                },
            }
        }
    }

    assert quality_reviewer._widen_delta_to_task_base(lens_body) is True
    delta = lens_body["candidate"]["delta"]
    assert delta["rework_delta_fallback_reason"] == "launcher_reason"
    assert delta["rework_delta_complete"] is False
    assert delta["paths"][EDITED_PATH]["unchanged_since_reviewed"] is False
    assert "rework_hunks" not in delta["paths"][EDITED_PATH]
