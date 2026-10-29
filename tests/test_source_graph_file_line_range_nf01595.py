"""NF-2026-01595: exact line-range reads for Source Graph file mode."""

import json
from types import SimpleNamespace

import pytest

from aiworkhub import source_graph as sg
from aiworkhub import worker_ai_tools_mcp as wtm
from aiworkhub.repository_state import bootstrap_repository
from source_graph_content_helpers import content_payload


def test_parse_file_line_range_valid_forms():
    assert sg.parse_file_line_range("120-180") == (120, 180)
    assert sg.parse_file_line_range("L120-L180") == (120, 180)
    assert sg.parse_file_line_range("120:180") == (120, 180)
    assert sg.parse_file_line_range("5-5") == (5, 5)


def test_parse_file_line_range_rejects_bad_input():
    assert sg.parse_file_line_range("180-120") is None
    assert sg.parse_file_line_range("0-5") is None
    assert sg.parse_file_line_range("junk") is None
    assert sg.parse_file_line_range("") is None
    assert sg.parse_file_line_range("120") is None
    assert sg.parse_file_line_range("120-180-200") is None
    assert sg.parse_file_line_range("L120:180") is None
    assert sg.parse_file_line_range(" 120-180 ") is None
    assert sg.parse_file_line_range(120) is None


def _write_lines(tmp_path, count):
    lines = [f"line {i:04d} " + "x" * 8 for i in range(1, count + 1)]
    target = tmp_path / "pkg" / "mod.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path, "pkg/mod.py", lines


def test_ranged_file_preview_returns_exact_window(tmp_path):
    root, relative, lines = _write_lines(tmp_path, 400)
    preview = sg._ranged_file_preview(root, relative, (120, 180))
    assert preview["source_preview_line_start"] == 120
    assert preview["source_preview_line_end"] == 180
    assert preview["source_preview_total_lines"] == 400
    assert preview["source_preview_truncated"] is False
    assert preview["source_preview"].splitlines() == lines[119:180]


def test_ranged_preview_clamps_past_eof(tmp_path):
    root, relative, lines = _write_lines(tmp_path, 400)
    preview = sg._ranged_file_preview(root, relative, (390, 500))
    assert preview["source_preview_line_start"] == 390
    assert preview["source_preview_line_end"] == 400
    assert preview["source_preview_total_lines"] == 400
    assert preview["source_preview"].splitlines() == lines[389:400]


def test_ranged_preview_clamps_fully_past_eof(tmp_path):
    root, relative, lines = _write_lines(tmp_path, 400)
    preview = sg._ranged_file_preview(root, relative, (450, 500))
    assert preview["source_preview_total_lines"] == 400
    assert preview["source_preview_line_start"] == 400
    assert preview["source_preview_line_end"] == 400
    assert preview["source_preview"].splitlines() == lines[399:]


def test_no_range_keeps_head_behavior(tmp_path):
    root, relative, _ = _write_lines(tmp_path, 400)
    head = sg._bounded_file_preview(root, relative, max_bytes=256)
    raw = (root / relative).read_bytes()
    assert head["source_preview_bytes"] == 256
    assert head["source_preview"] == raw[:256].decode("utf-8", errors="replace")
    assert head["source_preview_truncated"] is True
    assert "source_preview_line_start" not in head


def test_ranged_preview_byte_cap_cuts_at_line_boundary():
    lines = [f"line {i:05d} " + "y" * 40 for i in range(1, 3001)]
    text = "\n".join(lines) + "\n"
    preview = sg.ranged_text_preview(text, (1, 3000))
    assert preview["source_preview_truncated"] is True
    served = preview["source_preview"].splitlines()
    assert served == lines[: len(served)]
    assert preview["source_preview_line_end"] == (
        preview["source_preview_line_start"] + len(served) - 1
    )
    assert preview["source_preview_bytes"] <= sg.FILE_LINE_RANGE_PREVIEW_BYTES + 1
    assert preview["source_preview"].endswith("\n")


def _overlay_fixture():
    text = "\n".join(f"overlay {i:04d}" for i in range(1, 401)) + "\n"
    extraction = SimpleNamespace(
        language="python",
        status="ok",
        source_hash="a" * 64,
        entities=[],
    )
    view = SimpleNamespace(
        changed={"pkg/overlay.py": extraction},
        deleted=set(),
        digest_refs={},
        authorized_digests={},
        authorized_sources={"pkg/overlay.py": text},
        sealed_sources={},
        repair_evidence={},
        snapshot_sha256="b" * 64,
    )
    return SimpleNamespace(rework_overlay_packet={}), view, text


def test_merge_rework_overlay_file_range_uses_ranged_slice():
    ctx, view, _ = _overlay_fixture()
    merged, touched = wtm._merge_rework_overlay_payload(
        ctx, {"matches": [], "contexts": []}, view,
        mode="file", query="pkg/overlay.py", target="pkg/overlay.py",
        budget=64, file_line_range=(120, 180),
    )
    assert touched is True
    context = merged["contexts"][0]
    assert context["source_preview_line_start"] == 120
    assert context["source_preview_line_end"] == 180
    assert context["source_preview_total_lines"] == 400
    served = context["source_preview"].splitlines()
    assert served[0] == "overlay 0120"
    assert served[-1] == "overlay 0180"


def test_apply_rework_overlay_query_forwards_the_file_range():
    ctx, view, _ = _overlay_fixture()
    merged, touched = wtm._apply_rework_overlay_query(
        ctx, "file", "120-180", "pkg/overlay.py", 64,
        canonical_payload={"matches": [], "contexts": []}, view=view,
    )
    assert touched is True
    context = merged["contexts"][0]
    assert context["source_preview_line_start"] == 120
    assert context["source_preview_line_end"] == 180
    assert context["source_preview_total_lines"] == 400


def test_apply_rework_overlay_query_without_range_keeps_head_preview():
    ctx, view, text = _overlay_fixture()
    merged, touched = wtm._apply_rework_overlay_query(
        ctx, "file", "pkg/overlay.py", "pkg/overlay.py", 64,
        canonical_payload={"matches": [], "contexts": []}, view=view,
    )
    assert touched is True
    context = merged["contexts"][0]
    assert context["source_preview"] == text[:4096]
    assert "source_preview_line_start" not in context


RANGED_KEYS = (
    "source_preview",
    "source_preview_bytes",
    "source_preview_truncated",
    "source_preview_line_start",
    "source_preview_line_end",
    "source_preview_total_lines",
)


def _hundred_byte_lines(count):
    lines = [f"{i:05d}" + "x" * 94 for i in range(1, count + 1)]
    return lines, "\n".join(lines) + "\n"


def test_ranged_preview_serves_exactly_the_requested_lines_after_a_huge_line():
    text = "x" * 60000 + "\n" + "".join(f"l{i}\n" for i in range(2, 101))
    preview = sg.ranged_text_preview(text, (21, 25))
    assert preview["source_preview"].split("\n") == [f"l{n}" for n in range(21, 26)] + [""]
    assert (preview["source_preview_line_start"], preview["source_preview_line_end"]) == (21, 25)
    assert preview["source_preview_truncated"] is False
    assert preview["source_preview_bytes"] == len(
        preview["source_preview"].encode("utf-8")
    )


def test_ranged_preview_serves_a_large_window_exactly_within_the_cap():
    lines, text = _hundred_byte_lines(2000)
    preview = sg.ranged_text_preview(text, (1000, 1479))
    assert preview["source_preview_line_start"] == 1000
    assert preview["source_preview_line_end"] == 1479
    assert preview["source_preview_bytes"] == 48000
    assert preview["source_preview_truncated"] is False
    assert preview["source_preview"].splitlines() == lines[999:1479]


def test_ranged_preview_serves_an_overflowing_request_without_margin():
    lines, text = _hundred_byte_lines(2000)
    preview = sg.ranged_text_preview(text, (100, 3000))
    assert preview["source_preview_line_start"] == 100
    assert preview["source_preview_line_end"] == 590
    assert preview["source_preview_truncated"] is True
    assert preview["source_preview"].splitlines() == lines[99:590]


@pytest.mark.parametrize(
    "separator",
    ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", chr(0x2028), chr(0x2029), "\r"],
)
def test_ranged_preview_numbers_lines_by_newline_only(separator):
    text = f"a{separator}b\n" + "".join(f"l{i}\n" for i in range(2, 401))
    preview = sg.ranged_text_preview(text, (200, 200))
    assert preview["source_preview_total_lines"] == 400
    served = preview["source_preview"].split("\n")
    assert served[200 - preview["source_preview_line_start"]] == "l200"


def test_ranged_preview_round_trips_crlf_text():
    text = "".join(f"l{i}\r\n" for i in range(1, 51))
    preview = sg.ranged_text_preview(text, (1, 50))
    assert preview["source_preview"] == text
    assert preview["source_preview_bytes"] == len(text.encode("utf-8"))
    assert preview["source_preview_total_lines"] == 50


@pytest.mark.parametrize(
    ("text", "total"),
    [("", 0), ("a", 1), ("a\n", 1), ("a\n\n", 2), ("\n", 1), ("a\r\nb", 2)],
)
def test_ranged_preview_counts_newline_terminated_lines(text, total):
    preview = sg.ranged_text_preview(text, (1, 1))
    assert preview["source_preview_total_lines"] == total


def test_ranged_preview_refuses_an_oversized_requested_line():
    for text, request, line in (
        ("z" * 60000 + "\n", (1, 1), 1),
        ("head\n" + "z" * 60000 + "\ntail\n", (2, 3), 2),
    ):
        preview = sg.ranged_text_preview(text, request)
        assert preview["source_preview"] == ""
        assert preview["source_preview_bytes"] == 0
        assert preview["source_preview_truncated"] is True
        assert preview["source_preview_oversized_line"] == line


def test_ranged_preview_drops_a_later_oversized_line_at_the_line_boundary():
    text = "ok\n" + "z" * 60000 + "\nafter\n"
    preview = sg.ranged_text_preview(text, (1, 3))
    assert preview["source_preview"] == "ok\n"
    assert preview["source_preview_line_start"] == 1
    assert preview["source_preview_line_end"] == 1
    assert preview["source_preview_truncated"] is True
    assert "source_preview_oversized_line" not in preview


def _indexed_repo(tmp_path, lines):
    root = tmp_path / "ranged"
    root.mkdir()
    bootstrap_repository(root, repo_name="ranged")
    target = root / "pkg" / "mod.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    sg.build_index(root, incremental=False)
    return root, "pkg/mod.py"


def test_file_query_and_worker_serve_the_same_ranged_window(tmp_path):
    lines = [f"VALUE_{i:04d} = {i}" for i in range(1, 401)]
    root, relative = _indexed_repo(tmp_path, lines)

    context = sg.file_query(root, relative, 64, line_range=(120, 180))["contexts"][0]
    assert context["source_preview_line_start"] == 120
    assert context["source_preview_line_end"] == 180
    assert context["source_preview_total_lines"] == 400
    assert context["source_preview_truncated"] is False
    assert context["source_preview"].splitlines() == lines[119:180]
    assert "entities" not in context and "edges" not in context
    assert "source_preview_line_start" not in sg.file_query(root, relative, 64)["contexts"][0]

    ctx = wtm.WorkerToolContext(
        task_id="nf01595-task",
        runner="test",
        topic="source_graph_file_range",
        request_id="nf01595-request",
        repo=root,
        authority_repo=root,
        source_graph_targets=(relative,),
        allowed_writes=(relative,),
        session_topic="source_graph_file_range",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        rework_overlay_packet=None,
    )
    reply = wtm.source_graph_query(
        ctx, mode="file", target=relative, query="120-180",
        workflow_stage="implementation", compact_replay=False,
    )
    assert reply["ok"] is True
    served = content_payload(reply)["contexts"][0]
    assert {key: served[key] for key in RANGED_KEYS} == {
        key: context[key] for key in RANGED_KEYS
    }


def test_file_query_keeps_an_escape_heavy_window_whole(tmp_path):
    lines = ["# " + '"' * 78 for _ in range(1000)]
    root, relative = _indexed_repo(tmp_path, lines)
    expected = sg.ranged_text_preview("\n".join(lines) + "\n", (1, 1000))
    unmeasured_cap = (
        max(4096, 8 * 768) + sg.FILE_LINE_RANGE_PREVIEW_BYTES + 16384
    )
    assert expected["source_preview_truncated"] is True
    assert len(json.dumps(expected, ensure_ascii=False).encode("utf-8")) > unmeasured_cap

    context = sg.file_query(root, relative, 8, line_range=(1, 1000))["contexts"][0]
    assert {key: context[key] for key in RANGED_KEYS} == expected
