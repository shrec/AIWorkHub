"""NF-2026-01869: a manager correction may name a confirmed reviewer observation.

``validate_manager_correction`` used to index only ``defect`` findings, so a
real defect a reviewer filed as an ``observation`` and the manager confirmed was
refused ``finding_not_found``. It now indexes ``defect`` and ``observation``
findings; every other guard (confirmation, severity, lens, ambiguity) is the
same, so an unconfirmed observation is refused ``finding_not_confirmed`` and any
other disposition or unknown id is still ``finding_not_found``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher_accept_review as accept_review  # noqa: E402
from aiworkhub import quality_evidence  # noqa: E402

TARGET = "src/widget.py"
BEFORE = "alpha\nbeta\n"
PATCH = (
    f"--- a/{TARGET}\n"
    f"+++ b/{TARGET}\n"
    "@@ -1,2 +1,2 @@\n"
    " alpha\n"
    "-beta\n"
    "+gamma\n"
)
REVIEWER_REQUEST_ID = "reviewer-request-1"
LENS = quality_evidence.LENS_CORRECTNESS
FINDING_LOCAL_ID = "correctness-0-6f1cf0617014234b"
FINDING_ID = f"reviewer:{LENS}:{FINDING_LOCAL_ID}"


class _Core:
    def manager_bootstrap(self) -> dict:
        return {
            "role": "manager",
            "repo": str(self.repo),
            "manager_route": {"provider": "codex", "thread_id": "thread-1"},
        }


def _validate(
    tmp_path: Path,
    *,
    disposition: str,
    ids: list[str] | None = None,
    confirmed: bool = True,
    severity: str = "low",
) -> tuple[object, dict, str]:
    repo = tmp_path.resolve()
    workspace = repo / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / TARGET).write_bytes(BEFORE.encode("utf-8"))
    core = _Core()
    core.repo = repo
    reviewers = [
        {
            "request_id": REVIEWER_REQUEST_ID,
            "lens": LENS,
            "usable": True,
            "receipt": {
                "report": {
                    "lens": LENS,
                    "findings": [
                        {
                            "id": FINDING_LOCAL_ID,
                            "disposition": disposition,
                            "severity": severity,
                        }
                    ],
                }
            },
        }
    ]
    dispositions = (
        [
            {
                "reviewer_request_id": REVIEWER_REQUEST_ID,
                "finding_id": FINDING_ID,
                "disposition": "confirmed",
            }
        ]
        if confirmed
        else []
    )
    return accept_review.validate_manager_correction(
        SimpleNamespace(repo=repo),
        {
            "patch": PATCH,
            "finding_ids": ids if ids is not None else [FINDING_ID],
            "reason": "fixed the observed defect",
        },
        card={"allowed_writes": [TARGET]},
        workspace=SimpleNamespace(path=workspace),
        changed=[TARGET],
        reviewers=reviewers,
        reviewer_request_ids=[REVIEWER_REQUEST_ID],
        finding_dispositions=dispositions,
        core=core,
        path_cls=Path,
    )


def test_confirmed_observation_is_accepted(tmp_path: Path) -> None:
    record, texts, error = _validate(tmp_path, disposition="observation")

    assert error == ""
    assert record is not None
    assert record["finding_ids"] == [FINDING_ID]
    assert texts[TARGET] == "alpha\ngamma\n"


def test_confirmed_defect_is_still_accepted(tmp_path: Path) -> None:
    record, _texts, error = _validate(tmp_path, disposition="defect")

    assert error == ""
    assert record is not None


def test_unconfirmed_observation_is_refused_as_not_confirmed(tmp_path: Path) -> None:
    record, texts, error = _validate(
        tmp_path, disposition="observation", confirmed=False
    )

    assert record is None
    assert texts == {}
    assert error == f"finding_not_confirmed:{FINDING_ID}"


def test_unknown_id_is_still_not_found(tmp_path: Path) -> None:
    unknown = f"reviewer:{LENS}:no-such-finding"

    record, _texts, error = _validate(
        tmp_path, disposition="observation", ids=[unknown]
    )

    assert record is None
    assert error == f"finding_not_found:{unknown}"


@pytest.mark.parametrize("disposition", ["note", "process_limit", "", "dismissed"])
def test_other_dispositions_are_still_not_found(
    tmp_path: Path, disposition: str
) -> None:
    record, _texts, error = _validate(tmp_path, disposition=disposition)

    assert record is None
    assert error == f"finding_not_found:{FINDING_ID}"


def test_blocking_severity_observation_is_still_refused(tmp_path: Path) -> None:
    severity = sorted(quality_evidence.BLOCKING_SEVERITIES)[0]

    record, _texts, error = _validate(
        tmp_path, disposition="observation", severity=severity
    )

    assert record is None
    assert error == f"finding_blocking_severity:{FINDING_ID}"
