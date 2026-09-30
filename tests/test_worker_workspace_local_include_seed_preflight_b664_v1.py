"""B664 local-quoted-include dependency preflight tests.

Proves:
- B646 canary: S0/S4/S6 headers seeded transitively from the B646 feature packet.
- Unresolvable quoted includes fail closed before worker launch.
- Angle-bracket includes (NF-2026-01149) resolve transitively, ONLY against
  declared include roots -- never the including file's directory, never the
  tracked-file fallback; a target under no root is never seeded.
- Recursive resolution (A -> B -> C).
- Cycle deduplication.
- Symlink rejection is preserved.
- MAX_SEED_FILES bounds are applied to the combined set.
- B660 terminal-recursive-glob backward compatibility survives unchanged.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import worker_workspace  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )


# ---------------------------------------------------------------------------
# B646 canary: three transitive dependencies (S0, S4, S6) seeded from the
# declared feature-packet header.
# ---------------------------------------------------------------------------
def _seeded_count(ws_path: Path) -> int:
    """Count regular files beneath the workspace (excluding sentinel files)."""
    return sum(
        1
        for p in ws_path.rglob("*")
        if p.is_file() and not p.is_symlink() and p.name not in {".git", ".gitkeep"}
    )
def _b646_canary_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b664@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B664").returncode == 0

    include = repo / "bitnnv2" / "include"
    native_geom = repo / "bitnnv2" / "native" / "geometry"
    include.mkdir(parents=True)
    native_geom.mkdir(parents=True)

    # S0 sidecar -- no quoted includes, only angle-bracket system headers.
    (include / "parse_place_spatial_sidecar_v1.h").write_text(
        '#include <stddef.h>\n#include <stdint.h>\n#define S0_PRESENT 1\n',
        encoding="utf-8",
    )

    # S4 motion/time -- recursive: includes S0 via relative path.
    (native_geom / "parse_place_spatial_s4_motion_time_v1.h").write_text(
        '#include "../../include/parse_place_spatial_sidecar_v1.h"\n'
        '#define S4_PRESENT 1\n',
        encoding="utf-8",
    )

    # S6 shadow -- no quoted includes, only angle brackets.
    (native_geom / "parse_place_spatial_s6_shadow_v1.h").write_text(
        '#include <stddef.h>\n#define S6_PRESENT 1\n',
        encoding="utf-8",
    )

    # B646 feature packet -- quoted includes for S0, S4, S6 only (the three
    # transitive dependency headers that exist in the repo and were the B662
    # root cause).  Plus angle-bracket system headers that must be ignored.
    (include / "signal_atlas_production_feature_packet_b646_v1.h").write_text(
        '#include <stddef.h>\n'
        '#include <stdint.h>\n'
        '#include "parse_place_spatial_sidecar_v1.h"\n'
        '#include "../native/geometry/parse_place_spatial_s4_motion_time_v1.h"\n'
        '#include "../native/geometry/parse_place_spatial_s6_shadow_v1.h"\n'
        '#define B646_PRESENT 1\n',
        encoding="utf-8",
    )

    # Allowed-write placeholder.
    (repo / "out").mkdir()
    (repo / "out" / "result.txt").write_text("baseline\n", encoding="utf-8")

    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-qm", "b646-canary-fixture").returncode == 0
    return repo


def test_b646_canary_s0_s4_s6_headers_seeded_transitively(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The three declared transitive dependencies appear in the isolated
    workspace even though only the feature-packet header was in read_first."""
    repo = _b646_canary_repo(tmp_path)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "b646-canary",
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": [
                "bitnnv2/include/signal_atlas_production_feature_packet_b646_v1.h",
            ],
        },
        "validation",
    )
    try:
        ws = workspace.path
        assert (ws / "bitnnv2/include/signal_atlas_production_feature_packet_b646_v1.h").is_file()
        # S0, S4, S6 must be present (three transitive dependencies).
        assert (ws / "bitnnv2/include/parse_place_spatial_sidecar_v1.h").is_file()
        assert (ws / "bitnnv2/native/geometry/parse_place_spatial_s4_motion_time_v1.h").is_file()
        assert (ws / "bitnnv2/native/geometry/parse_place_spatial_s6_shadow_v1.h").is_file()
        # Declared (1) + resolved (3) + the existing allowed-write baseline (1).
        assert _seeded_count(ws) == 5
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# ---------------------------------------------------------------------------
# Unresolvable quoted include fails closed.
# ---------------------------------------------------------------------------
def _unresolvable_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b664@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B664").returncode == 0
    (repo / "read").mkdir()
    (repo / "out").mkdir()
    (repo / "read" / "broken.h").write_text(
        '#include "nonexistent_local_header_v99.h"\n#define BROKEN 1\n',
        encoding="utf-8",
    )
    (repo / "out" / "result.txt").write_text("baseline\n", encoding="utf-8")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-qm", "unresolvable-fixture").returncode == 0
    return repo


def test_unresolvable_quoted_include_is_skipped_not_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The compiler resolves includes itself; a header found nowhere (generated,
    system, build-provided) is simply not seeded and never refuses a launch."""
    repo = _unresolvable_repo(tmp_path)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "unresolvable",
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": ["read/broken.h"],
        },
        "validation",
    )
    try:
        assert (workspace.path / "read/broken.h").is_file()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# ---------------------------------------------------------------------------
# Angle-bracket includes (NF-2026-01149) resolve only against declared
# include roots -- never the including file's directory, never the
# tracked-file fallback.
# ---------------------------------------------------------------------------
def _angle_bracket_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b664@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B664").returncode == 0

    include = repo / "include"
    include.mkdir()
    (include / "lib.h").write_text(
        '#include <stddef.h>\n#include "util.h"\n'
        '#include <no_such_system_header_b664.h>\n#define LIB 1\n',
        encoding="utf-8",
    )
    (include / "util.h").write_text("#define UTIL 1\n", encoding="utf-8")

    (repo / "out").mkdir()
    (repo / "out" / "result.txt").write_text("baseline\n", encoding="utf-8")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-qm", "angle-bracket-fixture").returncode == 0
    # NF-2026-01149: angle includes now resolve against declared include
    # roots (a project/SDK-style search), so a header reachable at
    # <root>/<target> is seeded even though it is untracked here -- the
    # include root on disk, not git tracking, is authoritative for angle
    # resolution.  This replaces the old (incorrect) expectation that
    # angle-bracket includes were never followed at all.
    (include / "stddef.h").write_text("#define FAKE_STDDEF 1\n", encoding="utf-8")
    return repo


def test_angle_bracket_include_resolves_under_declared_include_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``#include <stddef.h>`` resolves to include/stddef.h because it lives
    under a declared include root (NF-2026-01149).  A target that resolves
    under no root, like <no_such_system_header_b664.h>, is never seeded and
    never refuses the launch."""
    repo = _angle_bracket_repo(tmp_path)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "angle-bracket",
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": ["include/lib.h"],
        },
        "validation",
    )
    try:
        ws = workspace.path
        assert (ws / "include/lib.h").is_file()
        # util.h is a quoted include and is seeded.
        assert (ws / "include/util.h").is_file()
        # stddef.h is an angle-bracket include resolved under the "include"
        # root -- seeded under NF-2026-01149.
        assert (ws / "include/stddef.h").is_file()
        # A genuine SDK/system header matching no include root is skipped.
        assert not (ws / "include/no_such_system_header_b664.h").exists()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# ---------------------------------------------------------------------------
# Recursive resolution: A -> B -> C.
# ---------------------------------------------------------------------------
def _recursive_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b664@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B664").returncode == 0

    h = repo / "h"
    h.mkdir()
    (h / "a.h").write_text('#include "sub/b.h"\n#define A 1\n', encoding="utf-8")
    (h / "sub").mkdir()
    (h / "sub" / "b.h").write_text('#include "../c.h"\n#define B 1\n', encoding="utf-8")
    (h / "c.h").write_text("#define C 1\n", encoding="utf-8")

    (repo / "out").mkdir()
    (repo / "out" / "result.txt").write_text("baseline\n", encoding="utf-8")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-qm", "recursive-fixture").returncode == 0
    return repo


def test_recursive_resolution_seeds_transitive_closure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A includes B includes C => all three appear in the workspace."""
    repo = _recursive_repo(tmp_path)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "recursive",
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": ["h/a.h"],
        },
        "validation",
    )
    try:
        ws = workspace.path
        assert (ws / "h/a.h").is_file()
        assert (ws / "h/sub/b.h").is_file()
        assert (ws / "h/c.h").is_file()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# ---------------------------------------------------------------------------
# Cycle deduplication.
# ---------------------------------------------------------------------------
def _cycle_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b664@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B664").returncode == 0

    inc = repo / "inc"
    inc.mkdir()
    (inc / "x.h").write_text('#include "y.h"\n#define X 1\n', encoding="utf-8")
    (inc / "y.h").write_text('#include "x.h"\n#define Y 1\n', encoding="utf-8")

    (repo / "out").mkdir()
    (repo / "out" / "result.txt").write_text("baseline\n", encoding="utf-8")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-qm", "cycle-fixture").returncode == 0
    return repo


def test_cycle_deduplication_does_not_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """x.h includes y.h includes x.h => both seeded, no infinite loop."""
    repo = _cycle_repo(tmp_path)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "cycle",
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": ["inc/x.h"],
        },
        "validation",
    )
    try:
        ws = workspace.path
        assert (ws / "inc/x.h").is_file()
        assert (ws / "inc/y.h").is_file()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# ---------------------------------------------------------------------------
# Symlink rejection is preserved through include resolution.
# ---------------------------------------------------------------------------
def _symlink_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b664@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B664").returncode == 0

    h = repo / "hdrs"
    h.mkdir()
    (h / "main.h").write_text('#include "target.h"\n#define MAIN 1\n', encoding="utf-8")
    real = h / "real.h"
    real.write_text("#define REAL 1\n", encoding="utf-8")
    link = h / "target.h"
    link.symlink_to(real)

    (repo / "out").mkdir()
    (repo / "out" / "result.txt").write_text("baseline\n", encoding="utf-8")
    assert _git(repo, "add", "hdrs/main.h", "hdrs/real.h", "out/result.txt").returncode == 0
    assert _git(repo, "commit", "-qm", "symlink-fixture").returncode == 0
    return repo


@pytest.mark.requires_symlink
def test_include_target_symlink_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A quoted include that resolves to a symlink is silently skipped (not
    seeded) -- only regular files are accepted."""
    repo = _symlink_repo(tmp_path)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "symlink",
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": ["hdrs/main.h"],
        },
        "validation",
    )
    try:
        ws = workspace.path
        assert (ws / "hdrs/main.h").is_file()
        # target.h is a symlink -- not seeded.
        assert not (ws / "hdrs/target.h").exists()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# ---------------------------------------------------------------------------
# MAX_SEED_FILES bound includes transitive closure.
# ---------------------------------------------------------------------------
def test_include_closure_respects_max_seed_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b664@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B664").returncode == 0

    h = repo / "h"
    h.mkdir()
    (h / "a.h").write_text('#include "b.h"\n#define A 1\n', encoding="utf-8")
    (h / "b.h").write_text('#include "c.h"\n#define B 1\n', encoding="utf-8")
    (h / "c.h").write_text('#include "d.h"\n#define C 1\n', encoding="utf-8")
    (h / "d.h").write_text("#define D 1\n", encoding="utf-8")

    (repo / "out").mkdir()
    (repo / "out" / "result.txt").write_text("baseline\n", encoding="utf-8")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-qm", "max-seed-fixture").returncode == 0

    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    monkeypatch.setattr(worker_workspace, "MAX_SEED_FILES", 3)
    # allowed output (1) + a.h/b.h/c.h/d.h (4) = 5 > 3 => must fail.
    with pytest.raises(
        worker_workspace.WorkspaceError,
        match=r"seed_file_limit_exceeded:5",
    ):
        worker_workspace.create_workspace(
            repo,
            "max-seed",
            {
                "allowed_writes": ["out/result.txt"],
                "read_first": ["h/a.h"],
            },
            "validation",
        )


# ---------------------------------------------------------------------------
# B660 backward-compatibility: terminal-recursive-glob behavior survives.
# (Re-run the core B660 test fixture directly.)
# ---------------------------------------------------------------------------
def _b660_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b660@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B660").returncode == 0
    (repo / "read").mkdir()
    (repo / "out").mkdir()
    (repo / "read" / "manifest.json").write_text("{}\n", encoding="utf-8")
    (repo / "out" / "result.txt").write_text("baseline\n", encoding="utf-8")
    (repo / ".gitignore").write_text("*.safp6461\n", encoding="utf-8")
    assert _git(repo, "add", ".gitignore", "read/manifest.json", "out/result.txt").returncode == 0
    assert _git(repo, "commit", "-qm", "b660-fixture").returncode == 0
    return repo


def test_b660_terminal_recursive_glob_still_hydrates_untracked_ignored_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The B660 recursive-glob behavior is not broken by the B664 preflight
    (which adds zero extra files for this non-C fixture)."""
    repo = _b660_repo(tmp_path)
    shards = repo / "shards"
    (shards / "shard_0000").mkdir(parents=True)
    (shards / "shard_0001").mkdir()
    payloads = {
        "shards/shard_0000/CURRENT": b"generation-0\n",
        "shards/shard_0000/packet.safp6461": b"\x00\x01\x02\x03",
        "shards/shard_0000/packet.safp6461.sha256": b"hash-0\n",
        "shards/shard_0001/packet.safp6461": b"\x04\x05\x06\x07",
    }
    for relative, payload in payloads.items():
        (repo / relative).write_bytes(payload)
    assert "shards/shard_0000/packet.safp6461" not in _git(
        repo, "ls-files"
    ).stdout.splitlines()

    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "b660-live-seed",
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": ["read/manifest.json", "shards/**"],
        },
        "validation",
    )
    try:
        for relative, payload in payloads.items():
            assert (workspace.path / relative).read_bytes() == payload
        (workspace.path / "shards/shard_0000/packet.safp6461").write_bytes(b"worker")
        assert (
            repo / "shards/shard_0000/packet.safp6461"
        ).read_bytes() == b"\x00\x01\x02\x03"
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


# ---------------------------------------------------------------------------
# Include-roots validation.
# ---------------------------------------------------------------------------
def test_invalid_include_root_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b664@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B664").returncode == 0
    (repo / "read").mkdir()
    (repo / "read" / "ok.h").write_text("#define OK 1\n", encoding="utf-8")
    (repo / "out").mkdir()
    (repo / "out" / "result.txt").write_text("baseline\n", encoding="utf-8")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-qm", "invalid-root-fixture").returncode == 0

    with pytest.raises(
        worker_workspace.WorkspaceError,
        match=r"include_root_not_directory",
    ):
        worker_workspace._resolve_local_quoted_includes(
            repo, ["read/ok.h"], include_roots=(".", "nonexistent_dir")
        )


def _declared_include_root_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b664@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B664").returncode == 0

    include = repo / "deps" / "include" / "ufsecp"
    src = repo / "kernels"
    include.mkdir(parents=True)
    src.mkdir()
    (include / "ufsecp.h").write_text("#define UFSECP_CPU 1\n", encoding="utf-8")
    (include / "ufsecp_gpu.h").write_text(
        "#define UFSECP_GPU 1\n", encoding="utf-8"
    )
    (src / "cpu.c").write_text(
        '#include "ufsecp/ufsecp.h"\nint cpu(void) { return UFSECP_CPU; }\n',
        encoding="utf-8",
    )
    (src / "cpupp.cpp").write_text(
        '#include "ufsecp/ufsecp.h"\nint cpupp(void) { return UFSECP_CPU; }\n',
        encoding="utf-8",
    )
    (src / "gpu.cu").write_text(
        '#include "ufsecp/ufsecp_gpu.h"\n'
        "int gpu(void) { return UFSECP_GPU; }\n",
        encoding="utf-8",
    )
    (repo / "out").mkdir()
    (repo / "out" / "result.txt").write_text("baseline\n", encoding="utf-8")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-qm", "declared-include-root-fixture").returncode == 0
    return repo


def test_c_cpp_cuda_sources_resolve_tracked_headers_under_normalized_include_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    repo = _declared_include_root_repo(tmp_path)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "declared-root",
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": ["kernels/cpu.c", "kernels/cpupp.cpp", "kernels/gpu.cu"],
            "include_roots": ["deps/include/../include"],
        },
        "validation",
    )
    try:
        assert (workspace.path / "deps/include/ufsecp/ufsecp.h").is_file()
        assert (workspace.path / "deps/include/ufsecp/ufsecp_gpu.h").is_file()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


@pytest.mark.parametrize(
    ("include_line", "needle"),
    [
        ('#include "/tmp/absolute_escape_b664.h"\n', "/tmp/absolute_escape_b664.h"),
        (
            '#include "../../../../traversal_escape_b664.h"\n',
            "../../../../traversal_escape_b664.h",
        ),
    ],
)
def test_absolute_and_traversal_escape_includes_fail_closed_with_evidence(
    include_line: str,
    needle: str,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b664@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B664").returncode == 0
    (repo / "src" / "nested").mkdir(parents=True)
    (repo / "src" / "nested" / "escape.c").write_text(
        include_line, encoding="utf-8"
    )
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-qm", "escape-fixture").returncode == 0

    assert needle
    assert worker_workspace._resolve_local_quoted_includes(
        repo, ["src/nested/escape.c"], include_roots=(".",)
    ) == ["src/nested/escape.c"]


@pytest.mark.requires_symlink
def test_symlink_escape_include_under_declared_root_fails_closed_with_evidence(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b664@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B664").returncode == 0
    outside = tmp_path / "outside.h"
    outside.write_text("#define OUTSIDE 1\n", encoding="utf-8")
    (repo / "include" / "ufsecp").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "include" / "ufsecp" / "escape.h").symlink_to(outside)
    (repo / "src" / "main.c").write_text(
        '#include "ufsecp/escape.h"\n', encoding="utf-8"
    )
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-qm", "symlink-escape-fixture").returncode == 0

    assert worker_workspace._resolve_local_quoted_includes(
        repo, ["src/main.c"], include_roots=("include",)
    ) == ["src/main.c"]


# ---------------------------------------------------------------------------
# Conventional repository include roots (include/ and src/).
# ---------------------------------------------------------------------------
def _cmake_layout_repo(tmp_path: Path, *extra: tuple[str, str]) -> Path:
    """A CMake project: public headers under include/, sources under src/."""
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b664@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B664").returncode == 0
    files = {
        "include/pkg/core/types.hpp": "#pragma once\n",
        "include/pkg/core/audit.hpp": '#include "pkg/core/types.hpp"\n',
        "src/detail/impl.hpp": "#pragma once\n",
        "src/audit.cpp": '#include "pkg/core/audit.hpp"\n',
        "tests/test_audit.cpp": (
            '#include "pkg/core/audit.hpp"\n#include "detail/impl.hpp"\n'
        ),
        "out/result.txt": "baseline\n",
        **dict(extra),
    }
    for relative, text in files.items():
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / relative).write_text(text, encoding="utf-8")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-qm", "cmake-layout-fixture").returncode == 0
    return repo


def test_conventional_include_roots_seed_transitive_closure_from_src(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """src/ and tests/ files resolve "pkg/..." under include/ and "detail/..."
    under src/, and the header-to-header closure follows the same roots."""
    repo = _cmake_layout_repo(tmp_path)
    assert worker_workspace._repository_include_roots(repo) == ("include", "src")
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "cmake-layout",
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": ["src/audit.cpp", "tests/test_audit.cpp"],
        },
        "validation",
    )
    try:
        ws = workspace.path
        assert (ws / "include/pkg/core/audit.hpp").is_file()
        # Reached only through audit.hpp -> "pkg/core/types.hpp".
        assert (ws / "include/pkg/core/types.hpp").is_file()
        assert (ws / "src/detail/impl.hpp").is_file()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_conventional_include_roots_skip_an_absent_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    repo = _cmake_layout_repo(
        tmp_path, ("src/broken.cpp", '#include "pkg/core/absent.hpp"\n')
    )
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    workspace = worker_workspace.create_workspace(
        repo,
        "cmake-layout-absent",
        {
            "allowed_writes": ["out/result.txt"],
            "read_first": ["src/audit.cpp", "src/broken.cpp"],
        },
        "validation",
    )
    try:
        assert (workspace.path / "src/broken.cpp").is_file()
        assert not (workspace.path / "include/pkg/core/absent.hpp").exists()
    finally:
        worker_workspace.cleanup_workspace(repo, workspace.path, workspace.home)


def test_conventional_include_root_closure_respects_max_seed_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    repo = _cmake_layout_repo(tmp_path)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    monkeypatch.setattr(worker_workspace, "MAX_SEED_FILES", 3)
    # out/result.txt + src/audit.cpp + audit.hpp + types.hpp = 4 > 3.
    with pytest.raises(
        worker_workspace.WorkspaceError, match=r"seed_file_limit_exceeded:4"
    ):
        worker_workspace.create_workspace(
            repo,
            "cmake-layout-limit",
            {"allowed_writes": ["out/result.txt"], "read_first": ["src/audit.cpp"]},
            "validation",
        )


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_symlinked_or_reparse_include_root_is_never_searched(
    kind: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An include/ that is a link is never searched: the header resolves only
    to its real tracked path, never through the link."""
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b664@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B664").returncode == 0
    real = repo / "real_headers"
    (real / "pkg").mkdir(parents=True)
    (real / "pkg" / "x.hpp").write_text("#pragma once\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "main.cpp").write_text('#include "pkg/x.hpp"\n', encoding="utf-8")
    (repo / "out").mkdir()
    (repo / "out" / "result.txt").write_text("baseline\n", encoding="utf-8")
    try:
        if kind == "symlink":
            (repo / "include").symlink_to(real, target_is_directory=True)
        else:
            if sys.platform != "win32":
                pytest.skip("directory junctions are a Windows reparse point")
            import _winapi

            _winapi.CreateJunction(str(real), str(repo / "include"))
    except OSError as exc:
        pytest.skip(f"cannot create {kind} on this host: {exc}")
    assert _git(
        repo, "add", "real_headers/pkg/x.hpp", "src/main.cpp", "out/result.txt"
    ).returncode == 0
    assert _git(repo, "commit", "-qm", f"{kind}-include-root").returncode == 0

    assert worker_workspace._repository_include_roots(repo) == ("src",)
    assert worker_workspace._resolve_local_quoted_includes(
        repo, ["src/main.cpp"], include_roots=(".", "src")
    ) == ["real_headers/pkg/x.hpp", "src/main.cpp"]


# ---------------------------------------------------------------------------
# Nested include roots (e.g. src/cpu/include) resolve through tracked files.
# ---------------------------------------------------------------------------
def _nested_root_repo(tmp_path: Path, *, track_header: bool) -> Path:
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b664@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B664").returncode == 0
    header = repo / "src/cpu/include/secp256k1/detail/batch_pool.hpp"
    header.parent.mkdir(parents=True)
    header.write_text("#pragma once\n", encoding="utf-8")
    source = repo / "src/cpu/src/pool.cpp"
    source.parent.mkdir(parents=True)
    source.write_text('#include "secp256k1/detail/batch_pool.hpp"\n', encoding="utf-8")
    assert _git(repo, "add", "src/cpu/src/pool.cpp").returncode == 0
    if track_header:
        assert _git(repo, "add", header.relative_to(repo).as_posix()).returncode == 0
    assert _git(repo, "commit", "-qm", "nested-root").returncode == 0
    return repo


def test_a_nested_include_root_resolves_through_tracked_files(tmp_path: Path) -> None:
    repo = _nested_root_repo(tmp_path, track_header=True)

    assert worker_workspace._resolve_local_quoted_includes(
        repo, ["src/cpu/src/pool.cpp"], include_roots=(".", "src")
    ) == [
        "src/cpu/include/secp256k1/detail/batch_pool.hpp",
        "src/cpu/src/pool.cpp",
    ]


def test_an_untracked_header_is_never_seeded_by_the_fallback(tmp_path: Path) -> None:
    repo = _nested_root_repo(tmp_path, track_header=False)

    assert worker_workspace._resolve_local_quoted_includes(
        repo, ["src/cpu/src/pool.cpp"], include_roots=(".", "src")
    ) == ["src/cpu/src/pool.cpp"]


# ---------------------------------------------------------------------------
# NF-2026-01149: angle-bracket includes seed transitively against include
# roots only -- never the including file's directory, never the tracked-file
# fallback that quoted includes use.
# ---------------------------------------------------------------------------
def _angle_root_repro_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b664@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B664").returncode == 0

    proj = repo / "include" / "proj"
    proj.mkdir(parents=True)
    (proj / "a.hpp").write_text('#include <proj/b.hpp>\n#pragma once\n', encoding="utf-8")
    (proj / "b.hpp").write_text("#pragma once\n", encoding="utf-8")
    (proj / "q.hpp").write_text("#pragma once\n", encoding="utf-8")

    src = repo / "src"
    src.mkdir()
    (src / "main.cpp").write_text(
        '#include <proj/a.hpp>\n#include "proj/q.hpp"\n#include <vector>\n',
        encoding="utf-8",
    )

    (repo / "out").mkdir()
    (repo / "out" / "result.txt").write_text("baseline\n", encoding="utf-8")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-qm", "angle-root-repro-fixture").returncode == 0
    return repo


def test_angle_bracket_includes_seed_transitively_under_include_roots(
    tmp_path: Path,
) -> None:
    """NF-2026-01149 repro: ``<proj/a.hpp>`` and its transitive
    ``<proj/b.hpp>`` resolve under include/, the quoted "proj/q.hpp" include
    is unaffected, and ``<vector>`` (no include root contains it) is never
    seeded."""
    repo = _angle_root_repro_repo(tmp_path)
    assert worker_workspace._resolve_local_quoted_includes(
        repo, ["src/main.cpp"], include_roots=("include", "src")
    ) == [
        "include/proj/a.hpp",
        "include/proj/b.hpp",
        "include/proj/q.hpp",
        "src/main.cpp",
    ]


def test_angle_bracket_include_matching_only_a_tracked_file_outside_roots_is_not_seeded(
    tmp_path: Path,
) -> None:
    """A target that only exists as a tracked file outside every include root
    is not seeded -- angle includes never use the tracked-file fallback that
    quoted includes use."""
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b664@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B664").returncode == 0

    (repo / "elsewhere" / "proj").mkdir(parents=True)
    (repo / "elsewhere" / "proj" / "outside.hpp").write_text(
        "#pragma once\n", encoding="utf-8"
    )
    (repo / "src").mkdir()
    (repo / "src" / "main.cpp").write_text(
        '#include <proj/outside.hpp>\n', encoding="utf-8"
    )
    assert _git(repo, "add", ".").returncode == 0
    assert _git(
        repo, "commit", "-qm", "angle-tracked-outside-root-fixture"
    ).returncode == 0

    assert worker_workspace._resolve_local_quoted_includes(
        repo, ["src/main.cpp"], include_roots=("src",)
    ) == ["src/main.cpp"]


@pytest.mark.requires_symlink
def test_angle_bracket_include_target_symlink_rejected(tmp_path: Path) -> None:
    """An angle include that resolves to a symlink under an include root is
    never seeded -- only regular files are accepted."""
    repo = tmp_path / "parent"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "b664@example.invalid").returncode == 0
    assert _git(repo, "config", "user.name", "B664").returncode == 0

    include = repo / "include"
    include.mkdir()
    real = include / "real.hpp"
    real.write_text("#pragma once\n", encoding="utf-8")
    link = include / "target.hpp"
    link.symlink_to(real)
    (repo / "src").mkdir()
    (repo / "src" / "main.cpp").write_text(
        '#include <target.hpp>\n', encoding="utf-8"
    )
    assert _git(repo, "add", "include/real.hpp", "src/main.cpp").returncode == 0
    assert _git(repo, "commit", "-qm", "angle-symlink-fixture").returncode == 0

    assert worker_workspace._resolve_local_quoted_includes(
        repo, ["src/main.cpp"], include_roots=("include", "src")
    ) == ["src/main.cpp"]


def test_include_seed_module_importable_standalone() -> None:
    """``worker_workspace_include_seed`` must not top-level-import back from
    ``worker_workspace``: a fresh interpreter importing it directly (never
    having imported ``worker_workspace`` first) must not hit a partially
    initialized module ImportError."""
    result = subprocess.run(
        [sys.executable, "-c", "import aiworkhub.worker_workspace_include_seed"],
        env={**os.environ, "PYTHONPATH": str(Path(worker_workspace.__file__).resolve().parents[1])},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
