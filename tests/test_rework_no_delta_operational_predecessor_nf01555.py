"""NF-2026-01555: a reproduction of an operationally failed predecessor is not refused.

``rework_no_delta_refusal`` exempted only a predecessor whose terminal substatus
was ``validation_failed``. A predecessor that failed for an operational reason
with correct bytes could therefore never be recovered: the rework reproduced
the bytes and was refused ``rework_no_delta``. The manager's attribution is
already recorded on the card as ``rejection_disposition`` and projected into the
provider-launch request packet.

What is locked here:

* a matching ``request_id`` and a ``failure_category`` of
  ``validation_environment``, ``provider_runtime``, ``dependency_or_route`` or
  ``cancellation_or_timeout`` mints no refusal;
* ``candidate_code``, a mismatched or empty request_id, and a missing,
  non-dict or wrong-schema disposition keep the refusal;
* an unknown, None, int or list category keeps the refusal and never raises;
* the provider-launch request packet carries the card's disposition (that
  assertion lives beside the launch fixture in
  ``test_process_launcher_launch_isolated``).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher_evidence as evidence  # noqa: E402
from aiworkhub.process_launcher_acceptance import (  # noqa: E402
    changed_path_hashes,
)

from test_rework_no_delta_nf1370 import (  # noqa: E402
    _PREDECESSOR,
    _metadata,
    _seed_rework_workspace,
)

_PREDECESSOR_REQUEST_ID = "nf1370-predecessor"


def _disposition(**overrides) -> dict:
    disposition = {
        "schema_id": "aiworkhub.rejection_disposition.v1",
        "failure_category": "provider_runtime",
        "failure_category_source": "manager",
        "request_id": _PREDECESSOR_REQUEST_ID,
        "to": "blocked",
    }
    disposition.update(overrides)
    return disposition


def _reproduction(tmp_path: Path, disposition) -> tuple:
    workspace = _seed_rework_workspace(tmp_path, _PREDECESSOR)
    changed = sorted(_PREDECESSOR)
    sealed = changed_path_hashes(workspace, changed)
    assert set(sealed) == set(changed)
    metadata = _metadata(workspace, sealed)
    assert metadata["rework_predecessor"]["request_id"] == _PREDECESSOR_REQUEST_ID
    if disposition is not None:
        metadata["rejection_disposition"] = disposition
    return workspace, metadata, changed


def _refuse(workspace, metadata, changed) -> str:
    return evidence.rework_no_delta_refusal(
        workspace, metadata, changed, validation_only_replay=False
    )


def test_the_exempt_categories_are_exactly_the_operational_ones():
    assert evidence.REWORK_NO_DELTA_EXEMPT_FAILURE_CATEGORIES == frozenset({
        "validation_environment",
        "provider_runtime",
        "dependency_or_route",
        "cancellation_or_timeout",
    })
    assert "candidate_code" not in evidence.REWORK_NO_DELTA_EXEMPT_FAILURE_CATEGORIES


@pytest.mark.parametrize("category", sorted(
    evidence.REWORK_NO_DELTA_EXEMPT_FAILURE_CATEGORIES
))
def test_an_operational_category_for_the_same_request_is_not_refused(
    tmp_path, category
):
    workspace, metadata, changed = _reproduction(
        tmp_path, _disposition(failure_category=category)
    )

    assert _refuse(workspace, metadata, changed) == ""

    # The contrast proves the exemption rather than a broken fixture: the SAME
    # bytes with no disposition are still refused by name.
    del metadata["rejection_disposition"]
    assert _refuse(workspace, metadata, changed).startswith(
        evidence.REWORK_NO_DELTA + ":"
    )


def test_candidate_code_keeps_the_refusal(tmp_path):
    workspace, metadata, changed = _reproduction(
        tmp_path, _disposition(failure_category="candidate_code")
    )

    assert _refuse(workspace, metadata, changed).startswith(
        evidence.REWORK_NO_DELTA + ":"
    )


@pytest.mark.parametrize("request_id", [
    "some-other-request", "", "   ", None, 7, ["nf1370-predecessor"],
])
def test_a_mismatched_or_empty_request_id_keeps_the_refusal(tmp_path, request_id):
    workspace, metadata, changed = _reproduction(
        tmp_path, _disposition(request_id=request_id)
    )

    assert _refuse(workspace, metadata, changed).startswith(
        evidence.REWORK_NO_DELTA + ":"
    )


def test_an_absent_disposition_request_id_keeps_the_refusal(tmp_path):
    disposition = _disposition()
    del disposition["request_id"]
    workspace, metadata, changed = _reproduction(tmp_path, disposition)

    assert _refuse(workspace, metadata, changed).startswith(
        evidence.REWORK_NO_DELTA + ":"
    )


def test_an_empty_predecessor_request_id_keeps_the_refusal(tmp_path):
    workspace, metadata, changed = _reproduction(
        tmp_path, _disposition(request_id="")
    )
    metadata["rework_predecessor"]["request_id"] = ""

    assert _refuse(workspace, metadata, changed).startswith(
        evidence.REWORK_NO_DELTA + ":"
    )


@pytest.mark.parametrize("disposition", [
    None,
    "provider_runtime",
    ["provider_runtime"],
    42,
    {},
    _disposition(schema_id="aiworkhub.rejection_disposition.v0"),
    _disposition(schema_id=None),
])
def test_a_missing_or_malformed_disposition_keeps_the_refusal(
    tmp_path, disposition
):
    workspace, metadata, changed = _reproduction(tmp_path, disposition)

    assert _refuse(workspace, metadata, changed).startswith(
        evidence.REWORK_NO_DELTA + ":"
    )


@pytest.mark.parametrize("category", [
    "unknown_category", "", "PROVIDER_RUNTIME", None, 5, 1.5, True,
    ["provider_runtime"], {"provider_runtime": 1}, ("provider_runtime",),
])
def test_an_unknown_or_non_string_category_keeps_the_refusal_and_never_raises(
    tmp_path, category
):
    workspace, metadata, changed = _reproduction(
        tmp_path, _disposition(failure_category=category)
    )

    assert _refuse(workspace, metadata, changed).startswith(
        evidence.REWORK_NO_DELTA + ":"
    )


def test_a_missing_category_keeps_the_refusal(tmp_path):
    disposition = _disposition()
    del disposition["failure_category"]
    workspace, metadata, changed = _reproduction(tmp_path, disposition)

    assert _refuse(workspace, metadata, changed).startswith(
        evidence.REWORK_NO_DELTA + ":"
    )


def test_an_exempt_disposition_does_not_excuse_a_real_delta_or_replay_semantics(
    tmp_path,
):
    workspace, metadata, changed = _reproduction(tmp_path, _disposition())
    # A genuine delta was never refused in the first place.
    first = workspace.path / changed[0]
    first.write_text(first.read_text(encoding="utf-8") + "# edit\n", encoding="utf-8")
    assert _refuse(workspace, metadata, changed) == ""
    # validation_only_replay keeps its current outcome regardless.
    assert evidence.rework_no_delta_refusal(
        workspace, metadata, changed, validation_only_replay=True
    ) == ""
