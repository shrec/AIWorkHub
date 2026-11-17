"""NF-2026-02304: a manager correction may carry a manager-measured failure.

Measured 2026-11-16 (NF-2026-02301): a neighbour JS test the manager measured
failing (``node --test`` fail 1) and fixed with a two-line patch had no reviewer
finding to name, so ``accept_review`` refused
``manager_correction_invalid:finding_ids_empty``. A correction may now state the
measurement inline as ``manager_measured = {"command", "failing_output"}``; it
lifts no reviewer blocker and its canonical-JSON digest is bound into the
validated record as ``manager_measured_sha256``.

These tests drive the real ``accept_review`` through the NF-2026-01436 harness
and the NF-2026-01845 helpers, imported, not copied.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_TESTS = Path(__file__).resolve().parent
_SRC = _TESTS.parent / "src"
for _path in (_SRC, _TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import test_accept_with_manager_correction_nf01436 as _nf01436  # noqa: E402
import test_manager_correction_validation_failed_nf01845 as _nf01845  # noqa: E402

from aiworkhub import process_launcher_accept_review  # noqa: E402

MEASURED = {
    "command": "node --test tests/js/neighbour.test.mjs",
    "failing_output": "not ok 1 - neighbour renders\n# fail 1\n",
}
MEASURED_DIGEST = hashlib.sha256(
    json.dumps(
        MEASURED, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
).hexdigest()
REASON = "the manager measured a neighbour JS test failing; two-line fix"


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The NF-2026-01436 review_ready harness with no reviewer finding at all."""
    return _nf01436._accept_env(tmp_path, monkeypatch, findings=[], dispositions=[])


def _correction(measured: Any = None, ids: Any = ()) -> dict:
    payload: dict[str, Any] = {"patch": _nf01436._patch(), "reason": REASON}
    if ids != ():
        payload["finding_ids"] = ids
    if measured is not None:
        payload["manager_measured"] = measured
    return payload


def _capture_validator(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Record what the real validator returned for each accept."""
    seen: list[tuple] = []
    real = process_launcher_accept_review.validate_manager_correction

    def _wrapped(*args: Any, **kwargs: Any):
        result = real(*args, **kwargs)
        seen.append(result)
        return result

    monkeypatch.setattr(
        process_launcher_accept_review, "validate_manager_correction", _wrapped
    )
    return seen


def test_digest_is_the_canonical_json_sha256() -> None:
    digest, reason = process_launcher_accept_review.manager_measured_digest(MEASURED)
    assert reason == ""
    assert digest == MEASURED_DIGEST
    reordered = {"failing_output": MEASURED["failing_output"], "command": MEASURED["command"]}
    assert process_launcher_accept_review.manager_measured_digest(reordered) == (
        MEASURED_DIGEST,
        "",
    )


@pytest.mark.parametrize("ids", [[], None, ()], ids=["empty", "null", "absent"])
def test_measured_correction_without_finding_ids_passes_the_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ids: Any
) -> None:
    manager = _env(tmp_path, monkeypatch)
    seen = _capture_validator(monkeypatch)

    result = _nf01845._accept(manager, manager_correction=_correction(MEASURED, ids))

    assert len(seen) == 1
    record, corrected_texts, reason = seen[0]
    assert reason == ""
    assert record is not None
    assert record["manager_measured_sha256"] == MEASURED_DIGEST
    assert record["finding_ids"] == []
    assert record["paths"] == [_nf01436.TARGET]
    assert corrected_texts == {_nf01436.TARGET: _nf01436.CORRECTED}
    # Whatever the rest of the accept decides, it is no longer this refusal.
    assert not str(result.get("error") or "").startswith(
        "manager_correction_invalid:"
    ), result.get("error")


def test_regression_empty_finding_ids_without_measurement_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _env(tmp_path, monkeypatch)

    result = _nf01845._accept(manager, manager_correction=_correction(None, []))

    assert result["ok"] is False
    assert result["error"] == "manager_correction_invalid:finding_ids_empty"
    _nf01845._assert_nothing_landed(manager)


def test_regression_absent_finding_ids_without_measurement_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _env(tmp_path, monkeypatch)

    result = _nf01845._accept(manager, manager_correction=_correction(None))

    assert result["ok"] is False
    assert result["error"] == "manager_correction_invalid:finding_ids_empty"
    _nf01845._assert_nothing_landed(manager)


@pytest.mark.parametrize(
    "measured",
    [
        "node --test fail 1",
        ["node --test", "fail 1"],
        {},
        {"command": "node --test"},
        {"failing_output": "fail 1"},
        {"command": "", "failing_output": "fail 1"},
        {"command": "node --test", "failing_output": "   "},
        {"command": 1, "failing_output": "fail 1"},
        {"command": "node --test", "failing_output": None},
        {"command": "x" * 1001, "failing_output": "fail 1"},
        {"command": "node --test", "failing_output": "y" * 4001},
        {"command": "node --test", "failing_output": "fail 1", "extra": "z"},
        {"command": "node --test \uD800", "failing_output": "fail 1"},
    ],
    ids=[
        "string",
        "list",
        "empty_object",
        "missing_output",
        "missing_command",
        "empty_command",
        "blank_output",
        "command_not_str",
        "output_null",
        "command_over_cap",
        "output_over_cap",
        "unknown_key",
        "lone_surrogate",
    ],
)
def test_regression_malformed_measurement_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, measured: Any
) -> None:
    manager = _env(tmp_path, monkeypatch)

    result = _nf01845._accept(manager, manager_correction=_correction(measured, []))

    assert result["ok"] is False
    assert result["error"] == "manager_correction_invalid:manager_measured_invalid"
    _nf01845._assert_nothing_landed(manager)


def test_caps_are_inclusive_at_the_limit() -> None:
    at_cap = {"command": "x" * 1000, "failing_output": "y" * 4000}
    digest, reason = process_launcher_accept_review.manager_measured_digest(at_cap)
    assert reason == ""
    assert len(digest) == 64


def test_regression_measurement_does_not_excuse_an_unknown_finding_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _env(tmp_path, monkeypatch)

    result = _nf01845._accept(
        manager,
        manager_correction=_correction(MEASURED, [_nf01436.FINDING_ID]),
    )

    assert result["ok"] is False
    assert result["error"] == (
        f"manager_correction_invalid:finding_not_found:{_nf01436.FINDING_ID}"
    )
    _nf01845._assert_nothing_landed(manager)
