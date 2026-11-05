"""NF-2026-02076: the base side runs only when the candidate side failed.

``classify`` reads the base outcome only for a FAILED candidate, so a passing
or unrunnable candidate never needs a base workspace. The skipped base is
recorded as UNRUNNABLE with a ``base_not_run:candidate_<outcome>`` detail and
a zero duration; classification is identical to the always-run behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import finding_reproduction as fr  # noqa: E402

KEY = b"k" * 32
BASE_OID = "a" * 40
SPEC = "pytest:tests/test_calc.py::test_add"
FINDING = "reviewer:correctness:finding-1"
CANDIDATE_SHA = "c" * 64
REVIEWER_SHA = "r" * 64


class _FakeTarget:
    """A ``ReproductionTarget`` stand-in: workspaces are labels, rows are scripted."""

    def __init__(self, candidate: Any, base: Any, base_oid: str = BASE_OID) -> None:
        self.base_oid = base_oid
        self._script = {"candidate": candidate, "base": base}
        self.created: list[str] = []
        self.ran: list[str] = []
        self.cleaned: list[str] = []
        self.candidate_paths: tuple[str, ...] = ()

    def create_candidate(self) -> str:
        self.created.append("candidate")
        return "candidate"

    def create_base(self) -> str:
        self.created.append("base")
        return "base"

    def cleanup(self, workspace: str) -> None:
        self.cleaned.append(workspace)

    def run(self, workspace: str, commands: list[str]) -> list[dict[str, Any]]:
        self.ran.append(workspace)
        scripted = self._script[workspace]
        if isinstance(scripted, Exception):
            raise scripted
        return [{"command": commands[0], "returncode": scripted, "timed_out": False}]

    def sealed_manifest(self, also: Any) -> dict[tuple[str, str], str | None]:
        return {}

    def canonical_materialized(self) -> list[str]:
        return []


def _reproduce(target: _FakeTarget) -> dict[str, Any]:
    spec = fr.parse_reproduction_spec(SPEC)
    assert spec is not None
    return fr.reproduce_finding(
        spec,
        finding_id=FINDING,
        candidate_sha256=CANDIDATE_SHA,
        reviewer_receipt_sha256=REVIEWER_SHA,
        target=target,  # type: ignore[arg-type]
        coordinator_key=fr.receipt_key(KEY),
    )


def test_passing_candidate_never_creates_base() -> None:
    target = _FakeTarget(candidate=0, base=1)

    receipt = _reproduce(target)

    assert target.created == ["candidate"]
    assert target.ran == ["candidate"]
    assert receipt["classification"] == fr.NOT_REPRODUCED
    assert receipt["candidate"]["outcome"] == fr.PASSED
    assert receipt["base"]["outcome"] == fr.UNRUNNABLE
    assert receipt["base"]["detail"] == "base_not_run:candidate_passed"
    assert receipt["base"]["duration_seconds"] == 0
    assert fr.verify_receipt(receipt, fr.receipt_key(KEY))


@pytest.mark.parametrize(
    "candidate",
    [RuntimeError("sandbox down"), 3],
    ids=["sandbox_refused", "exit_3"],
)
def test_unrunnable_candidate_never_creates_base(candidate: Any) -> None:
    target = _FakeTarget(candidate=candidate, base=1)

    receipt = _reproduce(target)

    assert target.created == ["candidate"]
    assert target.ran == ["candidate"]
    assert receipt["classification"] == fr.UNRUNNABLE
    assert receipt["candidate"]["outcome"] == fr.UNRUNNABLE
    assert receipt["base"]["outcome"] == fr.UNRUNNABLE
    assert receipt["base"]["detail"] == "base_not_run:candidate_unrunnable"
    assert receipt["base"]["duration_seconds"] == 0


def test_unrunnable_candidate_workspace_failure_never_creates_base() -> None:
    target = _FakeTarget(candidate=0, base=0)

    def boom() -> str:
        raise OSError("no disk")

    target.create_candidate = boom  # type: ignore[method-assign]

    receipt = _reproduce(target)

    assert target.created == []
    assert receipt["classification"] == fr.UNRUNNABLE
    assert receipt["base"]["detail"] == "base_not_run:candidate_unrunnable"


@pytest.mark.parametrize(
    ("base_returncode", "expected", "base_outcome"),
    [
        (0, fr.REPRODUCED, fr.PASSED),
        (1, fr.PRE_EXISTING, fr.FAILED),
        (3, fr.UNRUNNABLE, fr.UNRUNNABLE),
    ],
    ids=["base_passes", "base_fails", "base_unrunnable"],
)
def test_failing_candidate_runs_base(
    base_returncode: int, expected: str, base_outcome: str
) -> None:
    target = _FakeTarget(candidate=1, base=base_returncode)

    receipt = _reproduce(target)

    assert target.created == ["candidate", "base"]
    assert target.ran == ["candidate", "base"]
    assert target.cleaned == ["candidate", "base"]
    assert receipt["classification"] == expected
    assert receipt["candidate"]["outcome"] == fr.FAILED
    assert receipt["base"]["outcome"] == base_outcome
    assert not receipt["base"]["detail"].startswith("base_not_run")


def test_failing_candidate_without_base_oid_keeps_base_oid_missing() -> None:
    target = _FakeTarget(candidate=1, base=0, base_oid="")

    receipt = _reproduce(target)

    assert target.created == ["candidate"]
    assert receipt["classification"] == fr.UNRUNNABLE
    assert receipt["base"]["outcome"] == fr.UNRUNNABLE
    assert receipt["base"]["detail"] == "base_oid_missing"
