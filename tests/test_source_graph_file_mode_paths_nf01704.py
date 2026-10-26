"""NF-2026-01704: Source Graph file mode reads one path per call and names the next window."""

from types import SimpleNamespace

import pytest

from aiworkhub import source_graph as sg
from aiworkhub import worker_ai_tools_mcp as wtm
from aiworkhub.repository_state import bootstrap_repository
from source_graph_content_helpers import content_payload

ALPHA = "pkg/alpha.py"
BETA = "pkg/beta.py"
GAMMA = "pkg/gamma.py"
BIG = "pkg/big.py"
SPACED = "pkg/two words.py"
ROOT = "tool.py"
SMALL_SOURCES = {
    ALPHA: "def alpha():\n    return 1\n",
    BETA: "def beta():\n    return 2\n",
    GAMMA: "def gamma():\n    return 3\n",
    SPACED: "def spaced():\n    return 4\n",
    ROOT: "def tool():\n    return 5\n",
}
# Comment-only lines add no entities or edges, so the MCP reply stays under the 16 KiB file cap.
BIG_LINES = [f"# row {i:04d} " + "-" * 6 for i in range(1, 401)]
BIG_TEXT = "\n".join(BIG_LINES) + "\n"
ALL_PATHS = (ALPHA, BETA, GAMMA, BIG, SPACED, ROOT)
REFUSAL_REASON = "file_mode_one_path_per_call"
NEXT_WINDOW_KEY = "source_preview_next_window"
PREVIEW_KEYS = (
    "source_preview", "source_preview_bytes", "source_preview_truncated", NEXT_WINDOW_KEY,
)


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("file_mode_paths")
    bootstrap_repository(root, repo_name="file_mode_paths")
    (root / "pkg").mkdir(exist_ok=True)
    for relative, source in {**SMALL_SOURCES, BIG: BIG_TEXT}.items():
        (root / relative).write_bytes(source.encode("utf-8"))
    sg.build_index(root, incremental=False)
    return root


def _ctx(repo):
    return wtm.WorkerToolContext(
        task_id="nf01704-task",
        runner="test",
        topic="source_graph_file_mode_paths",
        request_id="nf01704-request",
        repo=repo,
        authority_repo=repo,
        source_graph_targets=ALL_PATHS,
        allowed_writes=ALL_PATHS,
        session_topic="source_graph_file_mode_paths",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        rework_overlay_packet=None,
    )


def _file_call(repo, query, target=None):
    return wtm.source_graph_query(
        _ctx(repo), mode="file", target=target, query=query,
        workflow_stage="implementation", compact_replay=False,
    )


def _served_context(reply):
    assert reply["ok"] is True
    return content_payload(reply)["contexts"][0]


def _preview_fields(context):
    return {key: context.get(key) for key in PREVIEW_KEYS}


def _next_start(preview):
    return preview.count("\n") + 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("pkg/alpha.py pkg/beta.py", [ALPHA, BETA]),
        ("pkg/alpha.py,pkg/beta.py", [ALPHA, BETA]),
        ("pkg/alpha.py, pkg/beta.py", [ALPHA, BETA]),
        ("pkg/alpha.py\npkg/beta.py\tpkg/gamma.py", [ALPHA, BETA, GAMMA]),
        ("  pkg/alpha.py   ,  pkg/beta.py  ", [ALPHA, BETA]),
        ("pkg/alpha.py pkg/beta.py pkg/alpha.py", [ALPHA, BETA]),
        ("`pkg/alpha.py` \"pkg/beta.py\" 'pkg/gamma.py'", [ALPHA, BETA, GAMMA]),
        ("(pkg/alpha.py), [pkg/beta.py]", [ALPHA, BETA]),
        ("pkg\\alpha.py pkg\\beta.py", ["pkg\\alpha.py", "pkg\\beta.py"]),
        ("pkg/alpha.py:12 pkg/beta.py.", [ALPHA, BETA]),
        ("pkg/alpha.py:12-30, (pkg/beta.py).", [ALPHA, BETA]),
        ("pkg/alpha.py 120-180", [ALPHA]),
        ("pkg/alpha.py", [ALPHA]),
        ("pkg/alpha.py pkg/alpha.py", [ALPHA]),
    ],
)
def test_file_query_paths_splits_on_whitespace_and_commas(text, expected):
    assert sg.file_query_paths(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "", "   ", ",,,", "pkg", "120-180", "L120-L180", "120:180", "1.5", "e.g.",
        "find the entry point", "os.path ctx.repo", "I/O and/or", "alpha.py beta.py",
        "pkg/two words.py",
    ],
)
def test_file_query_paths_ignores_tokens_that_are_not_paths(text):
    assert sg.file_query_paths(text) == []


def test_file_query_paths_counts_what_the_resolver_finds():
    found = {"Makefile", "setup.py"}.__contains__
    assert sg.file_query_paths("pkg/alpha.py Makefile setup.py:3 os.path", found) == [
        ALPHA, "Makefile", "setup.py",
    ]


@pytest.mark.parametrize(
    "query",
    [
        f"{ALPHA} {BETA}",
        f"{ALPHA},{BETA}",
        f"{ALPHA}, {BETA}",
        f"{ALPHA}\n{BETA}",
    ],
)
def test_two_paths_are_refused_by_name(repo, query):
    reply = _file_call(repo, query)
    assert reply["ok"] is False
    assert reply["tool"] == "source_graph"
    assert reply["reason"] == REFUSAL_REASON
    assert reply["requested_paths"] == [ALPHA, BETA]
    assert "one path per call" in reply["valid_next_call"]
    assert "content" not in reply


def test_three_paths_are_all_named(repo):
    reply = _file_call(repo, f"{ALPHA} {BETA}, {GAMMA}")
    assert reply["ok"] is False
    assert reply["reason"] == REFUSAL_REASON
    assert reply["requested_paths"] == [ALPHA, BETA, GAMMA]


def test_unindexed_paths_are_refused_too(repo):
    reply = _file_call(repo, "pkg/missing.py pkg/other.py")
    assert reply["ok"] is False
    assert reply["reason"] == REFUSAL_REASON
    assert reply["requested_paths"] == ["pkg/missing.py", "pkg/other.py"]


def test_refusal_applies_with_a_declared_target(repo):
    reply = _file_call(repo, f"{ALPHA} {BETA}", target=ALPHA)
    assert reply["ok"] is False
    assert reply["reason"] == REFUSAL_REASON
    assert reply["requested_paths"] == [ALPHA, BETA]


def test_paths_with_line_ranges_are_still_refused(repo):
    reply = _file_call(repo, f"{ALPHA} 1-10, {BETA} 20-30")
    assert reply["ok"] is False
    assert reply["reason"] == REFUSAL_REASON
    assert reply["requested_paths"] == [ALPHA, BETA]


def test_each_refused_path_reads_cleanly_on_its_own(repo):
    refused = _file_call(repo, f"{ALPHA} {BETA}")
    assert refused["ok"] is False
    for path in refused["requested_paths"]:
        context = _served_context(_file_call(repo, path))
        assert context["file"]["file_path"] == path
        assert context["source_preview"] == SMALL_SOURCES[path]


@pytest.mark.parametrize("path", [ALPHA, BETA, BIG])
def test_single_path_query_is_unchanged(repo, path):
    expected = _preview_fields(sg.file_query(repo, path, 64)["contexts"][0])
    by_query = _served_context(_file_call(repo, path))
    by_target = _served_context(_file_call(repo, path, target=path))
    assert by_query["file"]["file_path"] == by_target["file"]["file_path"] == path
    assert _preview_fields(by_query) == expected
    assert _preview_fields(by_target) == expected


@pytest.mark.parametrize("query", ["pkg/missing.py", f"  `{ALPHA}`, "])
def test_a_single_path_is_never_refused(repo, query):
    reply = _file_call(repo, query)
    assert reply.get("reason") != REFUSAL_REASON
    assert "requested_paths" not in reply


def test_a_path_that_contains_a_space_is_read_whole(repo):
    assert sg.file_query(repo, SPACED, 64)["contexts"], "fixture file must be indexed"
    assert sg.file_query_paths(SPACED) == []
    context = _served_context(_file_call(repo, SPACED))
    assert context["file"]["file_path"] == SPACED
    assert context["source_preview"] == SMALL_SOURCES[SPACED]


def test_a_target_equal_to_the_query_is_never_refused(repo):
    context = _served_context(_file_call(repo, SPACED, target=SPACED))
    assert context["file"]["file_path"] == SPACED


def test_a_line_range_with_a_target_is_unchanged(repo):
    context = _served_context(_file_call(repo, "120-180", target=BIG))
    assert context["source_preview_line_start"] == 100
    assert context["source_preview_line_end"] == 200
    assert context["source_preview"].splitlines() == BIG_LINES[99:200]
    assert NEXT_WINDOW_KEY not in context


def test_a_dotted_identifier_beside_the_target_is_served(repo):
    context = _served_context(_file_call(repo, f"{ALPHA} os.path", target=ALPHA))
    assert context["file"]["file_path"] == ALPHA


def test_a_query_that_mentions_identifiers_is_not_refused(repo):
    reply = _file_call(repo, "where does alpha.py read ctx.repo")
    assert reply.get("reason") != REFUSAL_REASON
    assert "requested_paths" not in reply


def test_a_spaced_target_with_a_line_range_is_served(repo):
    context = _served_context(_file_call(repo, f"{SPACED} 1-10", target=SPACED))
    assert context["file"]["file_path"] == SPACED


def test_a_spaced_target_and_another_path_are_refused_whole(repo):
    reply = _file_call(repo, f"{SPACED} {BETA}", target=SPACED)
    assert reply["reason"] == REFUSAL_REASON
    assert reply["requested_paths"] == [SPACED, BETA]


def test_an_indexed_bare_name_with_a_line_suffix_counts(repo):
    reply = _file_call(repo, f"{ROOT}:2 {BETA}.")
    assert reply["reason"] == REFUSAL_REASON
    assert reply["requested_paths"] == [ROOT, BETA]


def test_truncated_unranged_preview_names_the_next_window(repo):
    payload = sg.file_query(repo, BIG, 64)
    context = payload["contexts"][0]
    assert context["source_preview_truncated"] is True
    start = _next_start(context["source_preview"])
    assert context[NEXT_WINDOW_KEY] == {
        "mode": "file", "target": BIG, "query": f"{start}-{start + 199}",
    }
    assert BIG_LINES[start - 1].startswith(context["source_preview"].split("\n")[-1])
    assert payload["matches"][0]["kind"] == "file"
    assert NEXT_WINDOW_KEY not in payload["matches"][0]


def test_the_named_window_continues_where_the_preview_stopped(repo):
    head = sg.file_query(repo, BIG, 64)["contexts"][0]
    hint = head[NEXT_WINDOW_KEY]
    assert hint["mode"] == "file"
    start = int(hint["query"].split("-")[0])
    assert start > 1
    assert sg.parse_file_line_range(hint["query"]) == (start, start + 199)

    window = _served_context(_file_call(repo, hint["query"], target=hint["target"]))
    assert window["source_preview_line_start"] <= start <= window["source_preview_line_end"]
    assert window["source_preview_line_end"] == 400
    served = window["source_preview"].splitlines()
    offset = start - window["source_preview_line_start"]
    assert served[offset:] == BIG_LINES[start - 1:]
    assert NEXT_WINDOW_KEY not in window

    complete_head_lines = head["source_preview"].split("\n")[:-1]
    assert complete_head_lines + served[offset:] == BIG_LINES


def test_the_mcp_reply_carries_the_same_hint(repo):
    expected = sg.file_query(repo, BIG, 64)["contexts"][0][NEXT_WINDOW_KEY]
    assert _served_context(_file_call(repo, BIG))[NEXT_WINDOW_KEY] == expected
    assert _served_context(_file_call(repo, BIG, target=BIG))[NEXT_WINDOW_KEY] == expected


def test_untruncated_preview_has_no_next_window(repo):
    context = sg.file_query(repo, ALPHA, 64)["contexts"][0]
    assert context["source_preview_truncated"] is False
    assert NEXT_WINDOW_KEY not in context
    assert NEXT_WINDOW_KEY not in _served_context(_file_call(repo, ALPHA))


@pytest.mark.parametrize("line_range", [(1, 50), (120, 180), (300, 400), (390, 500)])
def test_ranged_preview_has_no_next_window(repo, line_range):
    context = sg.file_query(repo, BIG, 64, line_range=line_range)["contexts"][0]
    assert "source_preview_line_start" in context
    assert NEXT_WINDOW_KEY not in context


def test_next_window_follows_the_delivered_preview_length(repo):
    short = sg.file_query(repo, BIG, 16)["contexts"][0]
    long = sg.file_query(repo, BIG, 64)["contexts"][0]
    assert len(short["source_preview"]) < len(long["source_preview"])
    for context in (short, long):
        start = _next_start(context["source_preview"])
        assert context[NEXT_WINDOW_KEY]["query"] == f"{start}-{start + 199}"
    assert short[NEXT_WINDOW_KEY] != long[NEXT_WINDOW_KEY]


@pytest.mark.parametrize(
    ("preview", "expected_start"),
    [("a\nb\nc", 3), ("a\nb\nc\n", 4), ("single line without a break", 1)],
)
def test_next_window_starts_on_the_last_line_the_preview_reached(preview, expected_start):
    context = {"source_preview": preview, "source_preview_truncated": True}
    assert sg.next_file_window_fields("pkg/x.py", context) == {
        NEXT_WINDOW_KEY: {
            "mode": "file", "target": "pkg/x.py",
            "query": f"{expected_start}-{expected_start + sg.FILE_NEXT_WINDOW_LINES - 1}",
        },
    }


@pytest.mark.parametrize(
    "context",
    [
        {},
        {"source_preview": "a\nb\nc"},
        {"source_preview": "a\nb\nc", "source_preview_truncated": False},
        {"source_preview": "a\nb\nc", "source_preview_truncated": "yes"},
        {"source_preview": "a\nb\nc", "source_preview_truncated": 1},
        {"source_preview_truncated": True},
        {"source_preview": None, "source_preview_truncated": True},
        {"source_preview": b"a\nb", "source_preview_truncated": True},
        {
            "source_preview": "a\nb\nc",
            "source_preview_truncated": True,
            "source_preview_line_start": 1,
            "source_preview_line_end": 3,
        },
    ],
)
def test_next_window_is_absent_unless_an_unranged_preview_was_cut(context):
    assert sg.next_file_window_fields("pkg/x.py", context) == {}


OVERLAY_TEXT = "\n".join(f"overlay {i:04d}" for i in range(1, 401)) + "\n"


def _overlay_context(text, file_line_range=None):
    extraction = SimpleNamespace(
        language="python", status="ok", source_hash="a" * 64, entities=[],
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
    merged, touched = wtm._merge_rework_overlay_payload(
        SimpleNamespace(rework_overlay_packet={}), {"matches": [], "contexts": []}, view,
        mode="file", query="pkg/overlay.py", target="pkg/overlay.py",
        budget=64, file_line_range=file_line_range,
    )
    assert touched is True
    return merged["contexts"][0]


def test_overlay_preview_names_the_next_window_like_the_canonical_one():
    context = _overlay_context(OVERLAY_TEXT)
    assert context["source_preview"] == OVERLAY_TEXT[:4096]
    assert context["source_preview_truncated"] is True
    start = _next_start(context["source_preview"])
    assert context[NEXT_WINDOW_KEY] == {
        "mode": "file", "target": "pkg/overlay.py", "query": f"{start}-{start + 199}",
    }


def test_overlay_untruncated_and_ranged_previews_are_unchanged():
    short = _overlay_context("overlay 0001\noverlay 0002\n")
    assert short["source_preview_truncated"] is False
    assert NEXT_WINDOW_KEY not in short

    ranged = _overlay_context(OVERLAY_TEXT, file_line_range=(120, 180))
    assert ranged["source_preview_line_start"] == 100
    assert NEXT_WINDOW_KEY not in ranged


def test_a_whole_multibyte_overlay_preview_is_not_truncated():
    text = "é\n" * 2000
    context = _overlay_context(text)
    assert context["source_preview"] == text
    assert context["source_preview_truncated"] is False
    assert NEXT_WINDOW_KEY not in context
