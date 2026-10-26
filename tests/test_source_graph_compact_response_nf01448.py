"""NF-2026-01448: single-page source_graph replies stop double-encoding.

Before this change every ``source_graph_query`` reply nested its payload as
``content``, a canonical JSON document serialized a SECOND time into a
string, so every quote was escaped on the wire; each match/symbol row also
repeated default values (``confidence`` 1.0, ``evidence_label`` EXTRACTED,
``qualname`` mechanically derivable from ``file_path``, ``line_end`` equal to
``line_start``, two redundant freshness hashes when fresh).

This file asserts: a single-page reply's ``content`` is a plain dict, not a
JSON-encoded string; ``canonical_bytes``/``content_sha256`` still cover the
full, unelided payload; per-row default fields are dropped while any
non-default value is always kept; and a paged (continuation) reply's
``content`` is untouched -- still a base64 string of the exact canonical
bytes.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json

import pytest

from aiworkhub import source_graph as source_graph_mod
from aiworkhub import worker_ai_tools_mcp as w
from aiworkhub.repository_state import bootstrap_repository


def _repo(tmp_path, name="repo"):
    root = tmp_path / name
    root.mkdir()
    bootstrap_repository(root, repo_name=name)
    return root


def _ctx(repo, *, task_id, request_id):
    return w.WorkerToolContext(
        task_id=task_id,
        runner="r",
        topic="topic",
        request_id=request_id,
        repo=repo,
        authority_repo=repo,
        source_graph_targets=(),
        session_topic="topic",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
    )


# ---------------------------------------------------------------------------
# Pure elision rules on ``_compact_source_graph_content``
# ---------------------------------------------------------------------------

def test_default_fields_are_elided_from_matches_and_symbols_without_mutating_input():
    payload = {
        "mode": "focus",
        "matches": [
            {
                "confidence": 1.0,
                "evidence_label": "EXTRACTED",
                "file_path": "pkg/mod.py",
                "kind": "function",
                "line_start": 10,
                "line_end": 10,
                "name": "widget",
                "qualname": "pkg/mod.py.widget",
            },
        ],
        "symbols": [
            {
                "confidence": 1.0,
                "evidence_label": "EXTRACTED",
                "file_path": "pkg/other.py",
                "line_start": 5,
                "line_end": 5,
                "name": "helper",
                "qualname": "pkg/other.py:5",
            },
        ],
    }
    compact = w._compact_source_graph_content(payload)

    for row in (compact["matches"][0], compact["symbols"][0]):
        assert "confidence" not in row
        assert "evidence_label" not in row
        assert "qualname" not in row
        assert "line_end" not in row

    # The elision helper never mutates its argument: a canonical_bytes /
    # content_sha256 computed from ``payload`` before or after this call
    # sees the exact same, undropped fields.
    assert payload["matches"][0]["confidence"] == 1.0
    assert payload["matches"][0]["qualname"] == "pkg/mod.py.widget"
    assert payload["symbols"][0]["line_end"] == 5


def test_non_default_field_values_are_always_kept_verbatim():
    payload = {
        "mode": "function",
        "matches": [
            {
                "confidence": 0.6,
                "evidence_label": "INFERRED",
                "file_path": "pkg/mod.py",
                "kind": "function",
                "line_start": 10,
                "line_end": 40,
                "name": "widget",
                # Not mechanically derivable from file_path+line or
                # file_path+name (it carries an extra nesting prefix).
                "qualname": "pkg/mod.py.register_tools.widget",
            },
        ],
    }
    row = w._compact_source_graph_content(payload)["matches"][0]
    assert row["confidence"] == 0.6
    assert row["evidence_label"] == "INFERRED"
    assert row["line_end"] == 40
    assert row["qualname"] == "pkg/mod.py.register_tools.widget"


def test_qualname_derivable_from_file_path_and_line_is_elided():
    payload = {
        "mode": "bodygrep",
        "matches": [
            {"file_path": "pkg/mod.py", "line_start": 7, "line_end": 7,
             "name": "x", "qualname": "pkg/mod.py:7"},
        ],
    }
    row = w._compact_source_graph_content(payload)["matches"][0]
    assert "qualname" not in row


def test_freshness_hashes_are_elided_only_when_state_is_fresh():
    payload = {
        "mode": "body",
        "matches": [
            {
                "file_path": "pkg/a.py", "line_start": 1, "line_end": 1,
                "name": "a", "qualname": "pkg/a.py:1", "source_hash": "a" * 64,
                "freshness": {
                    "disk_source_hash": "a" * 64,
                    "indexed_source_hash": "a" * 64,
                    "state": "fresh",
                },
            },
            {
                "file_path": "pkg/b.py", "line_start": 1, "line_end": 1,
                "name": "b", "qualname": "pkg/b.py:1",
                "freshness": {
                    "disk_source_hash": "b" * 64,
                    "indexed_source_hash": "c" * 64,
                    "state": "stale",
                },
            },
        ],
    }
    fresh_row, stale_row = w._compact_source_graph_content(payload)["matches"]
    # ``state`` is kept either way; the hashes are only redundant when fresh.
    assert fresh_row["freshness"] == {"state": "fresh"}
    assert stale_row["freshness"] == {
        "disk_source_hash": "b" * 64,
        "indexed_source_hash": "c" * 64,
        "state": "stale",
    }


def test_non_dict_and_non_list_payload_shapes_pass_through_unchanged():
    assert w._compact_source_graph_content("not-a-dict") == "not-a-dict"
    payload = {"mode": "summarize", "rows": [{"file": "a.py", "evidence": "kept"}]}
    compact = w._compact_source_graph_content(payload)
    assert compact["rows"] == payload["rows"]


# ---------------------------------------------------------------------------
# The 31-hit bodygrep fixture: >=40% smaller, non-default row still present
# ---------------------------------------------------------------------------

def _bodygrep_fixture(count: int = 31) -> dict:
    matches = []
    for index in range(count):
        file_path = f"src/pkg/module_{index:03d}.py"
        line_start = 10 + index
        matches.append({
            "confidence": 1.0,
            "evidence_label": "EXTRACTED",
            "file_path": file_path,
            "kind": "body_match",
            "line_start": line_start,
            "line_end": line_start,
            "name": "json.loads(",
            "qualname": f"{file_path}:{line_start}",
            "signature": f"payload = json.loads(response_{index}.text)",
        })
    # One deliberately non-default row: compaction must keep it verbatim.
    matches[-1]["confidence"] = 0.5
    matches[-1]["evidence_label"] = "INFERRED"
    return {
        "budget": 64,
        "bytes_scanned": 393977,
        "candidate_files": [m["file_path"] for m in matches],
        "cursor": None,
        "files_scanned": count,
        "matches": matches,
        "mode": "bodygrep",
        "next_cursor": None,
        "oversized_files": [],
        "oversized_files_skipped": 0,
        "query": "json.loads(",
        "query_tokens": ["json", "loads"],
        "query_tokens_source": "query",
        "scan_byte_cap": 16777216,
        "scan_file_cap": 2048,
        "scan_truncated": False,
    }


def test_31_hit_bodygrep_fixture_response_is_at_least_40_percent_smaller(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
):
    repo = _repo(tmp_path, "bodygrep31")
    source_graph_mod.build_index(repo, incremental=True)
    fixture = _bodygrep_fixture(31)

    def _bodygrep_query(repo_root, query, budget=64, *, target=None, cursor=None):
        return copy.deepcopy(fixture)

    monkeypatch.setattr(source_graph_mod, "bodygrep_query", _bodygrep_query)
    ctx = _ctx(repo, task_id="t-bodygrep31", request_id="req-bodygrep31")

    result = w.source_graph_query(ctx, mode="bodygrep", query="json.loads(", budget=64)

    assert result["ok"] is True
    assert isinstance(result["content"], dict)
    assert len(result["content"]["matches"]) == 31

    raw_text = json.dumps(fixture, ensure_ascii=False, sort_keys=True)
    canonical_bytes = w._canonical_json_bytes("source_graph", raw_text)
    old_content = canonical_bytes.decode("utf-8")

    before = len(json.dumps({"content": old_content}, ensure_ascii=False).encode("utf-8"))
    after = len(json.dumps({"content": result["content"]}, ensure_ascii=False).encode("utf-8"))
    assert after <= before * 0.6, (before, after)

    distinctive = next(
        row for row in result["content"]["matches"] if row.get("confidence") == 0.5
    )
    assert distinctive["evidence_label"] == "INFERRED"


# ---------------------------------------------------------------------------
# Live wiring: dict content, content_sha256 unchanged, paging untouched
# ---------------------------------------------------------------------------

def test_single_page_live_reply_is_a_dict_and_sha256_covers_the_full_payload(tmp_path):
    repo = _repo(tmp_path, "livefocus")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text(
        "def widget():\n    return 1\n", encoding="utf-8",
    )
    source_graph_mod.build_index(repo, incremental=True)
    ctx = _ctx(repo, task_id="t-compact", request_id="req-compact")

    result = w.source_graph_query(ctx, mode="body", query="widget", budget=8)

    assert result["ok"] is True
    assert result["outer_truncated"] is False
    assert isinstance(result["content"], dict)
    row = result["content"]["matches"][0]
    assert row["file_path"] == "pkg/mod.py"
    # Defaults an AST-extracted, single-line match always carries are gone.
    assert "confidence" not in row
    assert "evidence_label" not in row
    assert "qualname" not in row

    # content_sha256 is NOT simply the hash of the elided presentation view
    # -- it keeps covering the full canonical (unelided) bytes, so HMAC
    # ledger receipts and continuation reassembly stay correct.
    elided_bytes = json.dumps(
        result["content"], ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
    assert hashlib.sha256(elided_bytes).hexdigest() != result["content_sha256"]


def test_cache_hit_reply_content_is_also_a_dict_receipt_or_verbatim(tmp_path):
    repo = _repo(tmp_path, "cachehit")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text(
        "def cached_widget():\n    return 1\n", encoding="utf-8",
    )
    source_graph_mod.build_index(repo, incremental=True)
    ctx = _ctx(repo, task_id="t-cache", request_id="req-cache")

    first = w.source_graph_query(ctx, mode="body", query="cached_widget", budget=8)
    second = w.source_graph_query(ctx, mode="body", query="cached_widget", budget=8)

    assert first["ok"] is True
    assert second["cache_hit"] is True
    assert isinstance(second["content"], dict)
    if second["cache_receipt"]:
        assert second["content"]["reuse_previous_result"] is True
        assert second["content"]["content_sha256"] == first["content_sha256"]
    else:
        assert second["content"] == first["content"]


def test_paged_continuation_reply_keeps_content_as_a_base64_string(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
):
    repo = _repo(tmp_path, "paged")
    (repo / "pkg").mkdir()
    padding = "\n".join(f"    line_{i} = {i}  # {'x' * 40}" for i in range(1500))
    (repo / "pkg" / "big.py").write_text(
        f"def paged_target():\n{padding}\n    return 1\n", encoding="utf-8",
    )
    source_graph_mod.build_index(repo, incremental=True)
    monkeypatch.setattr(w, "_source_graph_output_cap", lambda mode: 4096)
    ctx = _ctx(repo, task_id="t-paged", request_id="req-paged")

    result = w.source_graph_query(ctx, mode="body", query="paged_target", budget=8)

    assert result["ok"] is True
    assert result["outer_truncated"] is True
    assert result["content_encoding"] == "base64"
    assert isinstance(result["content"], str)
    base64.b64decode(result["content"])  # still exactly a base64 chunk


# ---------------------------------------------------------------------------
# Invertibility: reapply(elide(row)) == row, and the edit ledger sees the
# same delivered ranges as the uncompacted canonical payload.
# ---------------------------------------------------------------------------

_H = "f" * 64

_ROUND_TRIP_ROWS = {
    "no_kind": {
        "file_path": "a.py", "line_start": 3, "line_end": 3, "name": "x",
        "qualname": "a.py:3", "confidence": 1.0,
    },
    "no_confidence_or_label": {
        "file_path": "a.py", "line_start": 3, "line_end": 9, "name": "x",
        "kind": "function",
    },
    "qualname_line_form_non_body_kind": {
        "file_path": "a.py", "line_start": 3, "line_end": 3, "name": "x",
        "kind": "function", "qualname": "a.py:3",
        "confidence": 1.0, "evidence_label": "EXTRACTED",
    },
    "qualname_name_form_no_kind": {
        "file_path": "a.py", "line_start": 3, "line_end": 5, "name": "x",
        "qualname": "a.py.x",
    },
    "fresh_with_source_hash": {
        "file_path": "a.py", "line_start": 1, "line_end": 1, "name": "x",
        "kind": "body_match", "source_hash": _H,
        "freshness": {
            "disk_source_hash": _H, "indexed_source_hash": _H, "state": "fresh",
        },
    },
    "fresh_without_source_hash": {
        "file_path": "a.py", "line_start": 1, "line_end": 1, "name": "x",
        "freshness": {
            "disk_source_hash": _H, "indexed_source_hash": _H, "state": "fresh",
        },
    },
    "fresh_with_differing_source_hash": {
        "file_path": "a.py", "line_start": 1, "line_end": 1, "name": "x",
        "source_hash": "e" * 64,
        "freshness": {
            "disk_source_hash": _H, "indexed_source_hash": _H, "state": "fresh",
        },
    },
    "nothing_to_elide": {"file_path": "a.py", "name": "x", "extra": 1},
}


@pytest.mark.parametrize("case", sorted(_ROUND_TRIP_ROWS))
def test_elide_then_reapply_is_the_identity_for_every_row_shape(case):
    row = _ROUND_TRIP_ROWS[case]
    original = copy.deepcopy(row)
    elided = w._elide_default_row_fields(row)
    assert row == original
    assert w._reapply_elided_row_defaults(elided) == row


def test_fresh_hashes_without_a_source_hash_are_kept_on_the_wire():
    row = _ROUND_TRIP_ROWS["fresh_without_source_hash"]
    assert w._elide_default_row_fields(row)["freshness"] == row["freshness"]


def test_live_body_reply_registers_the_same_delivered_ranges_as_the_canonical_payload(
    tmp_path,
):
    repo = _repo(tmp_path, "ledger")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text(
        "def widget():\n    return 1\n", encoding="utf-8",
    )
    source_graph_mod.build_index(repo, incremental=True)
    ctx = _ctx(repo, task_id="t-ledger", request_id="req-ledger")

    result = w.source_graph_query(ctx, mode="body", query="widget", budget=8)
    assert isinstance(result["content"], dict)

    canonical_payload = w._source_graph_payload(result)
    canonical_text = json.dumps(
        canonical_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    assert hashlib.sha256(canonical_text.encode("utf-8")).hexdigest() == (
        result["content_sha256"]
    )

    def _registered(reply):
        session = w.WorkerSemanticEditSession(ctx)
        count = session.note_source_graph_delivery(reply)
        return count, {
            (path, sha, start, end)
            for (path, sha), ranges in session._delivered.items()
            for start, end, _ in ranges
        }

    compact_count, compact_ranges = _registered(result)
    canonical_count, canonical_ranges = _registered(
        {"ok": True, "content": canonical_text, "content_sha256": result["content_sha256"]}
    )
    assert compact_count >= 1
    assert compact_ranges == canonical_ranges
    assert compact_count == canonical_count


def test_mixed_elision_codes_round_trip_through_the_payload_level_default():
    common = {
        "file_path": "a.py", "line_start": 3, "line_end": 3, "name": "x",
        "kind": "body_match", "qualname": "a.py:3",
        "confidence": 1.0, "evidence_label": "EXTRACTED",
    }
    payload = {
        "mode": "bodygrep",
        "matches": [
            *[{**common, "line_start": n, "line_end": n, "qualname": f"a.py:{n}"}
              for n in range(1, 6)],
            {**common, "confidence": 0.5, "evidence_label": "INFERRED"},
            {"file_path": "b.py", "name": "y", "extra": 1},
        ],
        "contexts": [{"entities": [dict(common)]}],
    }
    original = copy.deepcopy(payload)

    compact = w._compact_source_graph_content(payload)
    assert payload == original
    assert compact[w._ELIDED_DEFAULT_KEY] == "cekql"
    rows = compact["matches"]
    assert all(w._ELIDED_MARKER not in row for row in rows[:5])
    assert rows[5][w._ELIDED_MARKER] == "kql"
    assert rows[6][w._ELIDED_MARKER] == ""
    assert "confidence" not in rows[0]
    assert rows[5]["confidence"] == 0.5
    assert rows[5]["evidence_label"] == "INFERRED"

    assert w._reapply_elided_defaults_in_payload(compact) == payload
    result = {"ok": True, "content": compact}
    assert w._source_graph_payload(result) == payload
