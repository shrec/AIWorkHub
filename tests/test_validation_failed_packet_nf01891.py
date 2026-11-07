"""NF-2026-01891: compact aiworkhub.validation_failed_packet.v1 for red cards."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import core, server, validation_failed_packet  # noqa: E402

_MAX_BYTES = 4096
_REQUEST_ID = "req-red-0001"
_HASHES = {"src/a.py": "sha-a", "tests/test_a.py": "sha-b"}


def _red_card() -> dict:
    stdout = "".join(f"noise line {i} " + "y" * 40 + "\n" for i in range(1200))
    stdout += "FAILED tests/test_a.py::test_x - AssertionError\n"
    stdout += "FAILED tests/test_a.py::test_x - AssertionError\n"
    stdout += "ERROR tests/test_b.py::test_y - boom\n"
    assert len(stdout) > 60_000
    return {
        "task_id": "TASK_RED",
        "status": "finished",
        "terminal_substatus": "validation_failed",
        "validation_roles": ["reproduction", "regression", "lint"],
        "terminal_review": {
            "request_id": _REQUEST_ID,
            "evidence": {
                "changed_path_hashes": _HASHES,
                "validation": [
                    {"command": "pytest a", "returncode": 0, "stdout_tail": "ok"},
                    {
                        "declared_command": "python -m pytest tests/test_a.py -q",
                        "command": "/abs/python -m pytest tests/test_a.py -q",
                        "returncode": 1,
                        "duration_s": 3.5,
                        "timed_out": False,
                        "stdout_tail": stdout,
                        "stderr_tail": "stderr trailing words",
                    },
                    {"command": "ruff check", "returncode": 0, "stdout_tail": "clean"},
                ],
            },
        },
    }


def _canonical_len(packet: dict) -> int:
    return len(validation_failed_packet.canonical_json(packet).encode("utf-8"))


def test_build_packet_is_bounded_and_carries_the_failure_facts() -> None:
    card = _red_card()
    packet = validation_failed_packet.build_packet(card)

    assert packet is not None
    assert packet["schema_id"] == "aiworkhub.validation_failed_packet.v1"
    assert _canonical_len(packet) <= _MAX_BYTES
    assert packet["task_id"] == "TASK_RED"
    assert packet["request_id"] == _REQUEST_ID
    assert packet["candidate_sha256"] == core._evidence_identity(_HASHES)[1]
    assert packet["passing_count"] == 2
    assert len(packet["failing"]) == 1
    failing = packet["failing"][0]
    assert failing["command"] == "python -m pytest tests/test_a.py -q"
    assert failing["exit_code"] == 1
    assert failing["role"] == "regression"
    assert failing["duration_s"] == 3.5
    assert failing["timed_out"] is False
    assert failing["failing_nodeids"] == ["tests/test_a.py::test_x", "tests/test_b.py::test_y"]
    assert "ERROR tests/test_b.py::test_y" in failing["tail"]
    assert packet["log_ref"] == {
        "path": f".aiworkhub/runtime/process_logs/processes/attempt-artifacts/{_REQUEST_ID}/manifest.json",
        "sha256": None,
    }


def test_log_ref_hashes_the_manifest_when_repo_root_has_it(tmp_path: Path) -> None:
    manifest = tmp_path.joinpath(
        ".aiworkhub", "runtime", "process_logs", "processes", "attempt-artifacts",
        _REQUEST_ID, "manifest.json",
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b'{"k":1}')

    packet = validation_failed_packet.build_packet(_red_card(), repo_root=tmp_path)

    assert packet is not None
    assert packet["log_ref"]["sha256"] == hashlib.sha256(b'{"k":1}').hexdigest()


def test_bound_degrades_deterministically_and_marks_truncated() -> None:
    card = _red_card()
    rows = card["terminal_review"]["evidence"]["validation"]
    for index in range(6):
        rows.append(
            {
                "command": f"cmd {index}",
                "returncode": 2,
                "stdout_tail": "é" * 3000 + "".join(f"FAILED t{index}.py::t{n}\n" for n in range(12)),
            }
        )

    first = validation_failed_packet.build_packet(card)
    second = validation_failed_packet.build_packet(card)

    assert first == second
    assert first is not None
    assert first["truncated"] is True
    assert _canonical_len(first) <= _MAX_BYTES
    assert (
        first["passing_count"] + len(first["failing"]) + first.get("failing_omitted", 0) == len(rows)
    )


def test_long_parametrized_nodeids_are_capped_and_keep_the_bound() -> None:
    card = _red_card()
    long_param = "p" * 2000
    stdout = "".join(f"FAILED tests/t.py::test_x[{i}-{long_param}]\n" for i in range(10))
    card["terminal_review"]["evidence"]["validation"] = [
        {"command": "pytest t", "returncode": 1, "stdout_tail": stdout + "z" * 2048},
    ]

    packet = validation_failed_packet.build_packet(card)

    assert packet is not None
    assert _canonical_len(packet) <= _MAX_BYTES
    assert packet["truncated"] is True
    nodeids = packet["failing"][0]["failing_nodeids"]
    assert nodeids
    assert all(len(nodeid) <= 256 for nodeid in nodeids)


def test_non_ascii_command_and_nodeids_keep_the_byte_bound() -> None:
    card = _red_card()
    wide = chr(0x1F600) * 600
    stdout = "".join(f"FAILED tests/t.py::test_{i}{wide}\n" for i in range(3))
    card["terminal_review"]["evidence"]["validation"] = [
        {"command": wide, "returncode": 1, "stdout_tail": stdout},
    ]

    packet = validation_failed_packet.build_packet(card)

    assert packet is not None
    assert _canonical_len(packet) <= _MAX_BYTES
    failing = packet["failing"][0]
    assert len(failing["command"].encode("utf-8")) <= 512
    assert all(len(nodeid.encode("utf-8")) <= 256 for nodeid in failing["failing_nodeids"])


def test_non_validation_failed_card_has_no_packet() -> None:
    card = _red_card()
    card["terminal_substatus"] = "worker_failed"
    assert validation_failed_packet.build_packet(card) is None
    assert validation_failed_packet.build_packet({"task_id": "T", "status": "review_ready"}) is None


def test_terminal_review_substatus_also_selects_the_packet() -> None:
    card = _red_card()
    del card["terminal_substatus"]
    card["terminal_review"]["substatus"] = "validation_failed"
    assert validation_failed_packet.build_packet(card) is not None


def _envelope(card: dict) -> dict:
    return {
        "ok": True,
        "returncode": 0,
        "command": ["show", card["task_id"]],
        "stdout": json.dumps(card),
        "stderr": "",
    }


def test_task_show_summary_of_a_red_card_returns_the_packet(monkeypatch) -> None:
    card = _red_card()
    monkeypatch.setattr(server.core, "show_task", lambda task_id, full=False: _envelope(card))

    result = server.aiworkhub_task_show("TASK_RED")

    assert result["detail"] == "validation_failed_packet"
    assert len(result["stdout"].encode("utf-8")) <= _MAX_BYTES
    packet = json.loads(result["stdout"])
    assert packet["schema_id"] == "aiworkhub.validation_failed_packet.v1"
    assert packet["failing"][0]["exit_code"] == 1
    assert packet["request_id"] == _REQUEST_ID
    assert result["detail_request"]["evidence"]["detail"] == "evidence"
    assert result["detail_request"]["full"]["detail"] == "full"

    evidence = server.aiworkhub_task_show("TASK_RED", detail="evidence")
    assert evidence["detail"] == "evidence"
    full = server.aiworkhub_task_show("TASK_RED", detail="full")
    assert full["bounded"] is True
    full_card = json.loads(full["stdout"])
    assert full_card["task_id"] == "TASK_RED"
    assert full_card["card_bound"]["original_bytes"] > 60_000


def test_task_show_summary_of_a_review_ready_card_is_unchanged(monkeypatch) -> None:
    card = {
        "task_id": "TASK_OK",
        "status": "review_ready",
        "objective": "do the thing",
        "terminal_review": {
            "request_id": _REQUEST_ID,
            "evidence": {"validation": [{"command": "pytest", "returncode": 0}]},
        },
    }
    monkeypatch.setattr(server.core, "show_task", lambda task_id, full=False: _envelope(card))

    result = server.aiworkhub_task_show("TASK_OK", detail="summary")

    assert result["detail"] == "summary"
    rendered = json.loads(result["stdout"])
    assert rendered["objective"] == "do the thing"
    assert rendered["terminal_review"]["evidence"]["validation"]["summarized"] is True
    assert "schema_id" not in rendered
