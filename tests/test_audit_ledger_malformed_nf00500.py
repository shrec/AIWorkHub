"""Regression test for NF-00500: malformed ledger lines are counted and fail verification.

The audit ledger has exactly one writer which always stamps AUDIT_ENTRY_SCHEMA_ID,
so any non-blank line that does not parse as a well-formed audit entry is
corruption or tampering and must produce ``entries_malformed`` instead of being
silently skipped.
"""

import json
from pathlib import Path

from aiworkhub.worker_ai_tools_mcp import (
    AUDIT_ENTRY_SCHEMA_ID,
    _hmac_entry,
    verify_audit_ledger,
)


def _signed_entry(*, task_id: str, runner: str, topic: str, key: bytes) -> dict:
    """Build one authentic ledger entry using the module's own HMAC signer."""
    entry = {
        "schema_id": AUDIT_ENTRY_SCHEMA_ID,
        "task_id": task_id,
        "runner": runner,
        "topic": topic,
        "tool": "source_graph",
        "ok": True,
        "payload": {"mode": "focus", "query": "audit_ledger_malformed_nf00500"},
        "hit_count": 1,
        "bytes_returned": 0,
    }
    entry["hmac_sha256"] = _hmac_entry(entry, key)
    return entry


def _write_entries(ledger_path: Path, entries: list[dict]) -> None:
    ledger_path.write_text(
        "".join(f"{json.dumps(entry, sort_keys=True)}\n" for entry in entries),
        encoding="utf-8",
    )


def test_verify_audit_ledger_reports_malformed_lines(tmp_path: Path) -> None:
    ledger_path = tmp_path / "audit.jsonl"
    key_path = tmp_path / "key.bin"
    key = b"k" * 32
    key_path.write_bytes(key)

    _write_entries(
        ledger_path,
        [_signed_entry(task_id="nf-00500", runner="r", topic="t", key=key)],
    )

    result = verify_audit_ledger(
        ledger_path,
        key_path,
        task_id="nf-00500",
        runner="r",
        topic="t",
    )
    assert result["ok"] is True
    assert result["entries_malformed"] == 0
    assert result["entries_verified"] == 1

    ledger_path.write_text(
        ledger_path.read_text(encoding="utf-8") + '{"schema_id":' + "\n" + "not-json\n",
        encoding="utf-8",
    )

    result = verify_audit_ledger(
        ledger_path,
        key_path,
        task_id="nf-00500",
        runner="r",
        topic="t",
    )
    assert result["ok"] is False
    assert result["entries_malformed"] == 2
    assert result["reason"] == "audit_ledger_malformed_lines:2"
