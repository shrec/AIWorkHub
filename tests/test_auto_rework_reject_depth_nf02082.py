"""NF-2026-02082: an automatic validation-rework return must not count
toward the manager's reject-depth cap.

An auto-rework is the automatic return whose reject reason starts with
``validation_auto_rework signature=<sha> auto_return=N`` and that was made
with no manager decision. Only a real manager candidate-code rejection may
count toward ``REJECT_DEPTH_CAP``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import launch_replay_guard as guard


_CONTRACT = "contract-nf02082"


def _manager_reject(request_id: str) -> dict:
    return {
        "request_id": request_id,
        "to": "pending",
        "failure_category": "candidate_code",
        "failure_category_source": "manager",
        "contract_identity": _CONTRACT,
        "actor": "claude",
        "reason": "wrong approach, redo",
    }


def _auto_return(request_id: str, auto_return: int) -> dict:
    return {
        "request_id": request_id,
        "to": "pending",
        "failure_category": "candidate_code",
        "failure_category_source": "validation",
        "contract_identity": _CONTRACT,
        "actor": "validation_auto_rework",
        "reason": f"validation_auto_rework signature={'a' * 64} auto_return={auto_return}",
    }


def test_auto_returns_do_not_count_toward_the_reject_depth_cap() -> None:
    events = [
        _auto_return("r1", 1),
        _auto_return("r2", 2),
        _auto_return("r3", 3),
    ]
    counted = guard.counted_candidate_code_rejects(events, _CONTRACT)
    assert counted == []


def test_a_single_manager_reject_after_auto_returns_is_depth_one_not_capped() -> None:
    events = [
        _auto_return("r1", 1),
        _auto_return("r2", 2),
        _manager_reject("r3"),
    ]
    counted = guard.counted_candidate_code_rejects(events, _CONTRACT)
    assert counted == ["r3"]
    record = guard.rejection_depth_record(
        contract_identity=_CONTRACT,
        rejected_request_ids=counted,
        recorded_at="2026-11-05T00:00:00+00:00",
        actor="claude",
    )
    assert record is None


def test_the_cap_fires_only_on_manager_rejects_not_auto_returns() -> None:
    events = [
        _auto_return("r1", 1),
        _manager_reject("r2"),
        _auto_return("r3", 1),
        _manager_reject("r4"),
    ]
    counted = guard.counted_candidate_code_rejects(events, _CONTRACT)
    assert counted == ["r2", "r4"]
    record = guard.rejection_depth_record(
        contract_identity=_CONTRACT,
        rejected_request_ids=counted,
        recorded_at="2026-11-05T00:00:00+00:00",
        actor="claude",
    )
    assert record is not None
    assert record["depth"] == guard.REJECT_DEPTH_CAP
    assert record["next_action"] == "decompose_or_supersede_required"


def test_auto_rework_identity_matches_the_writer() -> None:
    from aiworkhub import validation_auto_rework as writer

    assert guard.AUTO_REWORK_ACTOR == writer.ACTOR
    header = writer._HEADER.format(signature="a" * 64, count=1)
    assert header.startswith(guard.AUTO_REWORK_REASON_PREFIX)
