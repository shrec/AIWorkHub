"""NF-2026-01593: card-contract text reaches the reviewer whole, or is flagged."""

from __future__ import annotations

import hashlib

from aiworkhub import process_launcher, quality_reviewer, task_templates


DIGEST = hashlib.sha256(b"candidate").hexdigest()


def _packet(**kwargs):
    return quality_reviewer.build_review_packet(
        request_id="req1",
        task_id="task1",
        claim_epoch=1,
        worker_provider="adapter-a",
        changed_path_hashes={"src/mod.py": DIGEST},
        **kwargs,
    )


def _text(label: str, chars: int) -> str:
    rows = "".join(f"- {label}_{index:05d}\n" for index in range(chars // 8 + 1))
    return rows[:chars]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_objective_longer_than_the_evidence_bound_reaches_the_packet_whole():
    objective = _text("worker_text", 4600)

    packet = _packet(objective=objective)

    assert packet["contract"]["objective"] == objective
    assert "contract_truncation" not in packet


def test_long_acceptance_validation_and_required_outputs_reach_the_packet_whole():
    acceptance = [_text("acceptance", 3000), "short acceptance row"]
    validation = [_text("validation", 3000)]
    required_outputs = [_text("output", 3000)]

    packet = _packet(
        acceptance=acceptance,
        validation=validation,
        required_outputs=required_outputs,
    )

    assert packet["contract"]["acceptance"] == acceptance
    assert packet["contract"]["validation"] == validation
    assert packet["contract"]["required_outputs"] == required_outputs
    assert "contract_truncation" not in packet


def test_contract_text_up_to_the_cap_is_carried_without_a_record():
    cap = quality_reviewer.MAX_CONTRACT_TEXT_CHARS
    objective = _text("objective", cap)
    acceptance = [_text("acceptance", cap)]

    packet = _packet(objective=objective, acceptance=acceptance)

    assert packet["contract"]["objective"] == objective
    assert packet["contract"]["acceptance"] == acceptance
    assert "contract_truncation" not in packet


def test_contract_field_above_the_cap_is_cut_with_an_explicit_record():
    cap = quality_reviewer.MAX_CONTRACT_TEXT_CHARS
    objective = _text("objective", cap + 123)
    acceptance = ["short row", _text("acceptance", cap + 7)]

    packet = _packet(objective=objective, acceptance=acceptance)

    assert packet["contract"]["objective"] == objective[:cap]
    assert packet["contract"]["acceptance"] == ["short row", acceptance[1][:cap]]
    assert packet["contract_truncation"] == [
        {
            "field": "objective",
            "original_chars": cap + 123,
            "kept_chars": cap,
            "sha256": _sha256(objective),
        },
        {
            "field": "acceptance[1]",
            "original_chars": cap + 7,
            "kept_chars": cap,
            "sha256": _sha256(acceptance[1]),
        },
    ]


def test_contracts_that_differ_only_past_the_cap_seal_to_different_packets():
    head = _text("objective", quality_reviewer.MAX_CONTRACT_TEXT_CHARS)

    first = _packet(objective=head + "first tail")
    second = _packet(objective=head + "second tail")

    assert first["contract"] == second["contract"]
    assert first["packet_sha256"] != second["packet_sha256"]


def test_evidence_text_stays_bounded_at_the_finding_limit():
    limit = quality_reviewer.MAX_TEXT_CHARS
    assert limit == 2_000

    packet = _packet(
        mechanical_checks=[
            {
                "check_id": "check-1",
                "kind": "static",
                "status": "pass",
                "provenance": "p" * (limit + 500),
            }
        ],
        terminal_validation=[
            {
                "declared_command": "c" * (limit + 500),
                "executed_argv": ["a" * (limit + 500)],
                "returncode": 0,
            }
        ],
    )

    assert packet["mechanical_checks"][0]["provenance"] == "p" * limit
    assert packet["terminal_validation"][0]["declared_command"] == "c" * limit
    assert packet["terminal_validation"][0]["executed_argv"] == ["a" * limit]
    assert "contract_truncation" not in packet


def test_contract_cap_covers_every_card_contract_limit():
    cap = quality_reviewer.MAX_CONTRACT_TEXT_CHARS

    assert cap >= process_launcher.MAX_TASK_CONTRACT_BYTES
    assert cap >= process_launcher.MAX_REWORK_TASK_CONTRACT_BYTES
    assert cap >= task_templates.MAX_OBJECTIVE_LENGTH
    assert cap > quality_reviewer.MAX_TEXT_CHARS
