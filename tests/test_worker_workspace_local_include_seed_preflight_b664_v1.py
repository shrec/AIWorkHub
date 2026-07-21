"""B664 local-quoted-include dependency preflight tests.

Proves:
- B646 canary: S0/S4/S6 headers seeded transitively from the B646 feature packet.
- Unresolvable quoted includes fail closed before worker launch.
- Angle-bracket includes are never followed.
- Recursive resolution (A -> B -> C).
- Cycle deduplication.
- Symlink rejection is preserved.
- MAX_SEED_FILES bounds are applied to the combined set.
- B660 terminal-recursive-glob backward compatibility survives unchanged.
"""

from __future__ import annotations

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


def test_unresolvable_quoted_include_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    repo = _unresolvable_repo(tmp_path)
    monkeypatch.setenv(
        worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees")
    )
    with pytest.raises(
        worker_workspace.WorkspaceError,
        match=r"local_quoted_include_unresolved:.*nonexistent_local_header_v99",
    ):
        worker_workspace.create_workspace(
            repo,
            "unresolvable",
            {
                "allowed_writes": ["out/result.txt"],
                "read_first": ["read/broken.h"],
            },
            "validation",
        )


# ---------------------------------------------------------------------------
# Angle-bracket includes are never followed.
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
        '#include <stddef.h>\n#include "util.h"\n#define LIB 1\n',
        encoding="utf-8",
    )
    (include / "util.h").write_text("#define UTIL 1\n", encoding="utf-8")

    (repo / "out").mkdir()
    (repo / "out" / "result.txt").write_text("baseline\n", encoding="utf-8")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-qm", "angle-bracket-fixture").returncode == 0
    # Keep this file untracked: if it appears in the detached workspace it was
    # incorrectly pulled in by angle-bracket dependency hydration.
    (include / "stddef.h").write_text("#define FAKE_STDDEF 1\n", encoding="utf-8")
    return repo


def test_angle_bracket_includes_never_followed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``#include <stddef.h>`` is never resolved even when a file with that
    name sits inside a configured include root."""
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
        # util.h is a quoted include and should be seeded.
        assert (ws / "include/util.h").is_file()
        # stddef.h was an angle-bracket include -- NOT seeded even though it
        # exists in the repo include root.
        assert not (ws / "include/stddef.h").exists()
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
