"""emit() writes UTF-8 bytes so non-ASCII payloads survive a legacy code page."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from aiworkhub.recipes import _common

SRC = Path(__file__).resolve().parents[1] / "src"
PAYLOAD = {"schema_id": "test.v1", "text": "გამარჯობა 🙂 привет"}


def _run_child(encoding: str) -> subprocess.CompletedProcess[bytes]:
    code = (
        "import sys\n"
        "from aiworkhub.recipes._common import emit\n"
        "sys.exit(emit({'schema_id': 'test.v1', 'text': "
        "'\\u10d2\\u10d0\\u10db\\u10d0\\u10e0\\u10ef\\u10dd\\u10d1\\u10d0 "
        "\\U0001f642 \\u043f\\u0440\\u0438\\u0432\\u0435\\u0442'}))\n"
    )
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = encoding
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), env["PYTHONPATH"]] if env.get("PYTHONPATH") else [str(SRC)]
    )
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, env=env, timeout=60
    )


def test_emit_survives_cp1251_stdout_and_prints_utf8() -> None:
    for encoding in ("cp1251", "cp1252"):
        proc = _run_child(encoding)
        assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
        assert json.loads(proc.stdout.decode("utf-8")) == {"ok": True, **PAYLOAD}


def test_emit_keeps_shape_sort_order_and_indent(monkeypatch) -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1251", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    assert _common.emit({"schema_id": "s", "b": 1, "a": 2}) == 0
    expected = json.dumps(
        {"ok": True, "schema_id": "s", "b": 1, "a": 2},
        ensure_ascii=False,
        sort_keys=True,
        indent=1,
    )
    assert raw.getvalue() == (expected + "\n").encode("utf-8")


def test_emit_falls_back_to_text_write_without_buffer(monkeypatch) -> None:
    sink = io.StringIO()
    monkeypatch.setattr(sys, "stdout", sink)
    assert _common.emit(dict(PAYLOAD)) == 0
    assert json.loads(sink.getvalue()) == {"ok": True, **PAYLOAD}


def test_emit_output_bound_refusal_unchanged(monkeypatch) -> None:
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1251"))
    monkeypatch.setattr(_common, "MAX_OUTPUT_CHARS", 64)
    code = _common.emit({"schema_id": "s", "blob": "ж" * 500})
    assert code == 2
    body = json.loads(raw.getvalue().decode("utf-8"))
    assert body["ok"] is False
    assert body["error"] == "output_exceeded_bound"
    assert body["schema_id"] == "s"
    assert body["max_output_chars"] == 64
