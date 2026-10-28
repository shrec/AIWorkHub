"""NF-2026-01797: an operational exemption follows a carried delta by content.

``_operational_disposition_exempts`` exempted a byte-identical rework from
``rework_no_delta`` only when ``rejection_disposition.request_id`` equalled
``rework_predecessor.request_id``. ``aiworkhub_task_retry_terminal`` carries the
rejected delta into a NEW predecessor request, so the ids differ and a correct
reproduction of an operationally failed candidate was refused.

What is locked here:

* ``reject_review`` records the rejected request's ``changed_path_hashes`` on
  ``rejection_disposition``;
* a disposition for request A whose non-empty ``changed_path_hashes`` equal the
  carried predecessor B's exactly, with an exempt category, mints no refusal;
* one differing digest, absent, empty, non-dict or non-str-digest hashes, a
  wrong schema and ``candidate_code`` keep the refusal: unknown is never an
  exemption.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, task_store  # noqa: E402
from aiworkhub import process_launcher_evidence as evidence  # noqa: E402

from _taskdb_compat import upsert_card  # noqa: E402
from test_learning_commit_store import (  # noqa: E402
    _coordinator_env,
    _setup_repo,
)
from test_rework_no_delta_operational_predecessor_nf01555 import (  # noqa: E402
    _disposition,
    _refuse,
    _reproduction,
)

_REJECTED_REQUEST_ID = "nf01797-rejected-request-a"


def _carried(tmp_path: Path, **overrides) -> tuple:
    """Predecessor B reproduced byte for byte; disposition pinned on request A."""
    workspace, metadata, changed = _reproduction(tmp_path, None)
    predecessor = metadata["rework_predecessor"]
    assert predecessor["request_id"] != _REJECTED_REQUEST_ID
    fields = {
        "request_id": _REJECTED_REQUEST_ID,
        "failure_category": "validation_environment",
        "changed_path_hashes": dict(predecessor["changed_path_hashes"]),
    }
    fields.update(overrides)
    metadata["rejection_disposition"] = _disposition(**fields)
    return workspace, metadata, changed


def _refused(workspace, metadata, changed) -> bool:
    return _refuse(workspace, metadata, changed).startswith(
        evidence.REWORK_NO_DELTA + ":"
    )


def test_a_carried_delta_with_identical_hashes_is_not_refused(tmp_path):
    workspace, metadata, changed = _carried(tmp_path)

    assert _refuse(workspace, metadata, changed) == ""

    # Contrast: the same bytes without the disposition are refused by name.
    del metadata["rejection_disposition"]
    assert _refused(workspace, metadata, changed)


@pytest.mark.parametrize(
    "category", sorted(evidence.REWORK_NO_DELTA_EXEMPT_FAILURE_CATEGORIES)
)
def test_every_exempt_category_follows_the_carried_content(tmp_path, category):
    workspace, metadata, changed = _carried(tmp_path, failure_category=category)

    assert _refuse(workspace, metadata, changed) == ""


def test_one_differing_digest_keeps_the_refusal(tmp_path):
    workspace, metadata, changed = _carried(tmp_path)
    hashes = metadata["rejection_disposition"]["changed_path_hashes"]
    hashes[sorted(hashes)[0]] = "0" * 64

    assert _refused(workspace, metadata, changed)


def test_a_different_path_set_keeps_the_refusal(tmp_path):
    workspace, metadata, changed = _carried(tmp_path)
    hashes = metadata["rejection_disposition"]["changed_path_hashes"]
    hashes["src/another_path.py"] = "1" * 64

    assert _refused(workspace, metadata, changed)


def test_absent_hashes_with_mismatched_ids_keep_the_refusal(tmp_path):
    workspace, metadata, changed = _carried(tmp_path)
    del metadata["rejection_disposition"]["changed_path_hashes"]

    assert _refused(workspace, metadata, changed)


@pytest.mark.parametrize("hashes", [
    {}, None, "", [], 7, "deadbeef", {"src/a.py": ""}, {"src/a.py": None},
])
def test_empty_or_malformed_hashes_keep_the_refusal(tmp_path, hashes):
    workspace, metadata, changed = _carried(tmp_path, changed_path_hashes=hashes)

    assert _refused(workspace, metadata, changed)


def test_a_non_str_digest_never_exempts_even_when_both_sides_agree(tmp_path):
    _workspace, metadata, _changed = _carried(tmp_path)
    predecessor = metadata["rework_predecessor"]
    first = sorted(predecessor["changed_path_hashes"])[0]
    predecessor["changed_path_hashes"][first] = 5
    metadata["rejection_disposition"]["changed_path_hashes"][first] = 5

    assert not evidence._operational_disposition_exempts(metadata, predecessor)


def test_candidate_code_with_matching_hashes_keeps_the_refusal(tmp_path):
    workspace, metadata, changed = _carried(
        tmp_path, failure_category="candidate_code"
    )

    assert _refused(workspace, metadata, changed)


def test_the_schema_check_stays_mandatory_for_the_content_branch(tmp_path):
    workspace, metadata, changed = _carried(
        tmp_path, schema_id="aiworkhub.rejection_disposition.v0"
    )

    assert _refused(workspace, metadata, changed)


def test_reject_review_records_the_rejected_hashes_on_the_disposition(
    tmp_path, monkeypatch,
):
    root = _setup_repo(tmp_path, monkeypatch)
    _coordinator_env(root, tmp_path, monkeypatch)
    task_id = "TASK-NF01797-DISPOSITION-HASHES"
    request_id = "request-nf01797-0001"
    hashes = {"src/a.py": "a" * 64, "tests/test_a.py": "b" * 64}
    con = sqlite3.connect(str(task_store.canonical_db_path(root)))
    con.row_factory = sqlite3.Row
    try:
        upsert_card(con, {
            "task_id": task_id,
            "runner": "claude_coding",
            "topic": "coding",
            "mode": "solo",
            "status": "review",
            "worker_status": "review",
            "terminal_review": {
                "substatus": "finalize_failed",
                "evidence": {
                    "request_identity": {"request_id": request_id},
                    "changed_path_hashes": dict(hashes),
                },
            },
        })
    finally:
        con.close()
    # The sealed-descriptor check is not under test: treat the absent delta as
    # valid so the rejected request's hashes reach the disposition.
    from aiworkhub import worker_workspace

    monkeypatch.setattr(
        worker_workspace, "validated_rework_delta", lambda *_a, **_k: (None, None)
    )

    rejected = core.reject_review(task_id, "environment failed", to="blocked")
    assert rejected["ok"] is True, rejected

    card = task_store.get_task(root, task_id)
    assert card is not None
    disposition = card["rejection_disposition"]
    assert disposition["request_id"] == request_id
    assert disposition["failure_category"] == "validation_environment"
    assert disposition["changed_path_hashes"] == hashes
