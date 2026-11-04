"""NF-2026-02061: bounded cross-file lexical edge resolution.

``_resolve_cpp_cross_file_edges`` used to re-normalize every import target and
every candidate path through pathlib for every (edge, candidate, import)
triple, so an incremental build of a mid-size repository ran for hours. These
tests pin three things on a synthetic 300-file repository (a common name
defined in 100 files, 40 includes per file):

* the resolved ``dst_qualname`` of every edge equals the former per-edge
  pathlib implementation (kept below verbatim as the oracle);
* resolution completes under a small wall-clock bound;
* progress is published as a heartbeat and a stall or wall-clock overrun
  aborts with a recorded reason rather than after a fixed step count.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from aiworkhub import source_graph as sg
from aiworkhub import source_graph_ast as sgast
from aiworkhub.repository_state import bootstrap_repository

FILE_COUNT = 300
COMMON_DEFINERS = 100  # files ``lib/mod_k.cpp`` with ``k % 3 == 0`` define ``get``
IMPORTS_PER_FILE = 40
RESOLUTION_BOUND_SECONDS = 15.0

_EXTRACTORS = (
    sgast.CPP_LEXICAL_EXTRACTOR_ID,
    sgast.POLYGLOT_LEXICAL_EXTRACTOR_ID,
    sgast.TREE_SITTER_JS_TS_EXTRACTOR_ID,
)


def _legacy_import_target_matches_file(target: str, file_path: str) -> bool:
    """The pre-NF-2026-02061 matcher, verbatim, as the equivalence oracle."""

    normalized_target = str(target).strip().replace("\\", "/")
    normalized_target = normalized_target.removeprefix("./")
    while normalized_target.startswith("../"):
        normalized_target = normalized_target[3:]
    target_path = Path(normalized_target)
    if target_path.suffix.casefold() in sg._INDEXED_EXTENSION_SET:
        normalized_target = target_path.with_suffix("").as_posix()
    normalized_target = normalized_target.replace("::", "/").replace(".", "/")
    normalized_file = str(file_path).strip().replace("\\", "/")
    target_path = Path(normalized_target)
    file_path_obj = Path(normalized_file)
    if target_path.name in {"", ".", ".."} or file_path_obj.name in {"", ".", ".."}:
        return False
    target_stem = target_path.stem.casefold()
    file_stem = file_path_obj.stem.casefold()
    if "/" not in normalized_target and target_stem and target_stem == file_stem:
        return True
    target_no_suffix = target_path.with_suffix("").as_posix().casefold().strip("/")
    file_no_suffix = file_path_obj.with_suffix("").as_posix().casefold().strip("/")
    return bool(
        target_no_suffix
        and (
            file_no_suffix.endswith(target_no_suffix)
            or file_no_suffix.endswith(f"{target_no_suffix}/index")
            or file_no_suffix.endswith(f"{target_no_suffix}/mod")
        )
    )


def _legacy_resolve(conn) -> int:
    """The pre-NF-2026-02061 full-scope resolver (both passes), as the oracle.

    The matcher is memoized here only to keep the oracle affordable; it is a
    pure function of its two strings, so the verdicts are unchanged.
    """

    import functools

    matches = functools.lru_cache(maxsize=None)(_legacy_import_target_matches_file)
    placeholders = ",".join("?" for _ in _EXTRACTORS)
    js_calls = (
        "AND NOT (kind='calls' AND file_path IN ("
        "SELECT file_path FROM files WHERE language IN ('javascript', 'typescript'))) "
    )
    conn.execute(
        f"UPDATE edges SET dst_qualname=NULL WHERE id IN (SELECT e.id FROM edges e "
        f"WHERE e.extractor IN ({placeholders}) "
        f"AND e.kind IN ('calls','inherits') ) {js_calls}",
        _EXTRACTORS,
    )
    select_unresolved = (
        f"SELECT e.id, e.file_path, e.dst_name, f.language FROM edges e "
        "JOIN files f ON f.file_path=e.file_path "
        f"WHERE e.extractor IN ({placeholders}) "
        "AND e.kind IN ('calls','inherits') AND e.dst_qualname IS NULL "
        "AND NOT (e.kind='calls' AND f.language IN ('javascript', 'typescript')) "
        "ORDER BY e.id"
    )

    def candidates_for(language: str, name: str) -> list:
        rows = conn.execute(
            "SELECT e.file_path, e.qualname, f.language FROM entities e "
            "JOIN files f ON f.file_path=e.file_path WHERE e.name=? AND "
            f"e.kind IN ({sg._LEXICAL_TARGET_KINDS_SQL}) "
            "ORDER BY e.file_path, e.qualname",
            (name,),
        ).fetchall()
        return [
            row for row in rows
            if sg._resolution_languages_compatible(language, str(row["language"]))
        ]

    resolved = 0
    for edge in conn.execute(select_unresolved, _EXTRACTORS).fetchall():
        candidates = candidates_for(str(edge["language"]), str(edge["dst_name"]))
        if len(candidates) != 1:
            continue
        cur = conn.execute(
            "UPDATE edges SET dst_qualname=? WHERE id=? AND dst_qualname IS NULL",
            (candidates[0]["qualname"], edge["id"]),
        )
        resolved += int(cur.rowcount or 0)
    for edge in conn.execute(select_unresolved, _EXTRACTORS).fetchall():
        imports = [
            str(row["dst_name"])
            for row in conn.execute(
                "SELECT dst_name FROM edges WHERE file_path=? AND kind='imports' "
                "ORDER BY id",
                (str(edge["file_path"]),),
            )
        ]
        if not imports:
            continue
        candidates = [
            row
            for row in candidates_for(str(edge["language"]), str(edge["dst_name"]))
            if any(matches(target, str(row["file_path"])) for target in imports)
        ]
        if len(candidates) != 1:
            continue
        cur = conn.execute(
            "UPDATE edges SET dst_qualname=? WHERE id=? AND dst_qualname IS NULL",
            (candidates[0]["qualname"], edge["id"]),
        )
        resolved += int(cur.rowcount or 0)
    return resolved


def _snapshot(conn) -> dict[int, str | None]:
    return {
        int(row["id"]): row["dst_qualname"]
        for row in conn.execute("SELECT id, dst_qualname FROM edges ORDER BY id")
    }


def _source(index: int) -> str:
    includes = [f'#include "util/u{(index * 7 + j) % 50}.hpp"' for j in range(37)]
    # Exactly one include names a ``get``-defining module by directory path ...
    includes.append(f'#include "lib/mod_{(index * 3) % FILE_COUNT}.hpp"')
    # ... one bare include matches by stem only (a second ``get`` definer when
    # ``index + 1`` is a multiple of three), and every third file names a
    # second definer by path so that edge stays ambiguous.
    includes.append(f'#include "mod_{(index + 1) % FILE_COUNT}.hpp"')
    if index % 3 == 0:
        includes.append(f'#include "lib/mod_{(index * 3 + 3) % FILE_COUNT}.hpp"')
    else:
        includes.append(f'#include "lib/other_{index}.hpp"')
    assert len(includes) == IMPORTS_PER_FILE
    body = []
    if index % 3 == 0:
        body.append("int get(int x) { return x; }")
    body.append("int run(int x) { return x * 2; }")
    body.append(f"int unique_{index}(int x) {{ return x; }}")
    body.append(
        f"int caller_{index}(int x) {{ return get(x) + run(x) + "
        f"unique_{(index + 1) % FILE_COUNT}(x); }}"
    )
    return "\n".join(includes + body) + "\n"


@pytest.fixture(scope="module")
def synthetic_repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("nf02061") / "repo"
    root.mkdir()
    bootstrap_repository(root, repo_name="nf02061")
    lib = root / "lib"
    lib.mkdir()
    for index in range(FILE_COUNT):
        (lib / f"mod_{index}.cpp").write_text(_source(index), encoding="utf-8")
    sg.build_index(root, incremental=False)
    return root


def test_synthetic_repo_shape(synthetic_repo):
    conn = sg.connect(sg.resolve_db_path(synthetic_repo), read_only=True)
    try:
        definers = conn.execute(
            "SELECT COUNT(DISTINCT file_path) FROM entities WHERE name='get'"
        ).fetchone()[0]
        assert definers == COMMON_DEFINERS
        per_file = conn.execute(
            "SELECT MIN(n), MAX(n), COUNT(*) FROM (SELECT file_path, COUNT(*) AS n "
            "FROM edges WHERE kind='imports' GROUP BY file_path)"
        ).fetchone()
        assert tuple(per_file) == (IMPORTS_PER_FILE, IMPORTS_PER_FILE, FILE_COUNT)
    finally:
        conn.close()


def test_resolution_is_bounded_and_equals_former_implementation(synthetic_repo):
    conn = sg.connect(sg.resolve_db_path(synthetic_repo))
    try:
        started = time.monotonic()
        resolved = sg._resolve_cpp_cross_file_edges(conn)
        elapsed = time.monotonic() - started
        current = _snapshot(conn)
        heartbeat = sg.cross_file_resolution_heartbeat()

        legacy_resolved = _legacy_resolve(conn)
        legacy = _snapshot(conn)
        conn.rollback()
    finally:
        conn.close()

    assert elapsed < RESOLUTION_BOUND_SECONDS, elapsed
    assert resolved == legacy_resolved
    assert current == legacy
    get_targets = {
        qualname for qualname in current.values()
        if qualname is not None and qualname.endswith("::get")
    }
    # Import evidence actually disambiguated the common name for many files,
    # so the equality above covers the import pass, not only unique names.
    assert len(get_targets) > COMMON_DEFINERS // 2
    assert heartbeat["schema_id"] == sg.CROSS_FILE_RESOLUTION_HEARTBEAT_SCHEMA_ID
    assert heartbeat["state"] == "completed"
    assert heartbeat["scope"] == "full"
    assert heartbeat["phase"] == "import_disambiguation"
    assert heartbeat["edges_resolved"] == resolved
    assert heartbeat["edges_processed"] == heartbeat["edges_total"] > 0
    assert heartbeat["files_processed"] == FILE_COUNT
    assert heartbeat["abort_reason"] is None


def test_incremental_scope_reads_only_changed_file_edges(synthetic_repo):
    changed = "lib/mod_6.cpp"
    conn = sg.connect(sg.resolve_db_path(synthetic_repo))
    try:
        # Start from a fully resolved generation whatever ran before.
        sg._resolve_cpp_cross_file_edges(conn)
        owned = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE file_path=? AND kind IN ('calls','inherits')",
            (changed,),
        ).fetchone()[0]
        before = _snapshot(conn)
        sg._resolve_cpp_cross_file_edges(
            conn, changed_files={changed}, affected_names=set()
        )
        after = _snapshot(conn)
        heartbeat = sg.cross_file_resolution_heartbeat()
        conn.rollback()
    finally:
        conn.close()

    assert after == before
    assert heartbeat["scope"] == "incremental"
    assert heartbeat["state"] == "completed"
    assert heartbeat["files_processed"] <= 1
    # Each phase reads at most the changed file's own edges.
    assert heartbeat["edges_total"] <= 2 * owned


def test_stall_aborts_with_recorded_reason(synthetic_repo):
    ticks = iter(float(n) * 1000.0 for n in range(10_000))
    conn = sg.connect(sg.resolve_db_path(synthetic_repo))
    try:
        with pytest.raises(sg.SourceGraphResolutionAbortedError) as excinfo:
            sg._resolve_cpp_cross_file_edges(
                conn, clock=lambda: next(ticks),
                stall_seconds=300.0, wall_clock_cap_seconds=1e12,
            )
        conn.rollback()
    finally:
        conn.close()
    assert excinfo.value.reason.startswith("stalled:")
    heartbeat = sg.cross_file_resolution_heartbeat()
    assert heartbeat["state"] == "aborted"
    assert heartbeat["abort_reason"] == excinfo.value.reason
    assert excinfo.value.heartbeat["abort_reason"] == excinfo.value.reason


def test_wall_clock_cap_aborts_with_recorded_reason(synthetic_repo):
    ticks = iter(float(n) for n in range(1_000_000))
    conn = sg.connect(sg.resolve_db_path(synthetic_repo))
    try:
        with pytest.raises(sg.SourceGraphResolutionAbortedError) as excinfo:
            sg._resolve_cpp_cross_file_edges(
                conn, clock=lambda: next(ticks),
                stall_seconds=1e12, wall_clock_cap_seconds=50.0,
            )
        conn.rollback()
    finally:
        conn.close()
    assert excinfo.value.reason.startswith("wall_clock_cap_exceeded:")
    heartbeat = excinfo.value.heartbeat
    assert heartbeat["state"] == "aborted"
    # Progress was made before the cap; the abort is time-based, not a step cap.
    assert heartbeat["edges_processed"] > 0


@pytest.mark.parametrize(
    "target",
    [
        "", ".", "/", "..", "./", "../", "math.hpp", "../b/math", "a/math.ts",
        "pkg.helper", "pkg::helper", "pkg/helper.py", "./pkg/helper", "helper",
        "lib/mod_3.hpp", "mod_3", "pkg", "pkg/index", "x.y.z", "  spaced.h  ",
        "dir\\win.hpp", "pkg/", "...", "a..b",
    ],
)
@pytest.mark.parametrize(
    "file_path",
    [
        "pkg/helper.py", "pkg/index.ts", "pkg/mod.rs", "lib/mod_3.cpp",
        "b/math.ts", "a/math.ts", "math.cpp", "x/y/z.py", "spaced.h",
        "dir/win.hpp", "", ".", "a..b.py",
    ],
)
def test_cached_matcher_equals_former_matcher(target, file_path):
    assert sg._import_target_matches_file(target, file_path) is (
        _legacy_import_target_matches_file(target, file_path)
    )
