"""NF-2026-01691: a torn multi-byte UTF-8 ledger line must fail closed, not raise."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import worker_ai_tools_mcp as w  # noqa: E402


def _write_signed_entry(ledger: Path, key: bytes) -> None:
    entry = {
        "schema_id": w.AUDIT_ENTRY_SCHEMA_ID,
        "task_id": "T",
        "runner": "r",
        "topic": "t",
        "tool": "noop",
    }
    entry["hmac_sha256"] = w._hmac_entry(entry, key)
    ledger.write_bytes((json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))


def test_verify_audit_ledger_counts_torn_utf8_line_as_malformed(tmp_path: Path) -> None:
    key = b"k" * 32
    key_path = tmp_path / "key"
    key_path.write_bytes(key)
    ledger = tmp_path / "ledger.jsonl"
    _write_signed_entry(ledger, key)
    with open(ledger, "ab") as handle:
        handle.write(b'{"schema_id":"\xc3\n')

    result = w.verify_audit_ledger(ledger, key_path, task_id="T", runner="r", topic="t")

    assert result["ok"] is False
    assert result["entries_malformed"] == 1
    assert result["entries_total"] == 1
    assert result["reason"] == "audit_ledger_malformed_lines:1"
