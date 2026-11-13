"""NF-2026-02238: Linux read_roots via Landlock, enforced deny_read, receipt.

Rule construction, glob semantics, validation, backend refusal and the
receipt are exercised on every OS; real Landlock enforcement runs only on a
Linux kernel with Landlock and is skipped elsewhere.
"""

from __future__ import annotations

import ctypes
import inspect
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import linux_read_roots as lrr
from aiworkhub import review_packet
from aiworkhub.linux_kernel_abi import (
    LANDLOCK_ACCESS_FS_READ_DIR,
    LANDLOCK_ACCESS_FS_READ_FILE,
    LANDLOCK_CREATE_RULESET_VERSION,
    SYS_LANDLOCK_CREATE_RULESET,
)

READ_FILE = LANDLOCK_ACCESS_FS_READ_FILE
READ_DIR = LANDLOCK_ACCESS_FS_READ_DIR
UNBOUNDED_RULES = 10**7
SID = "S-1-15-2-1-2-3-4-5-6-7"


def _landlock_abi() -> int:
    if not sys.platform.startswith("linux"):
        return 0
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(
        SYS_LANDLOCK_CREATE_RULESET,
        ctypes.c_void_p(),
        ctypes.c_size_t(0),
        ctypes.c_uint(LANDLOCK_CREATE_RULESET_VERSION),
    )
    return max(int(result), 0)


def _granted(path: Path | str, rules, right: int) -> bool:
    real = os.path.realpath(path)
    for rule_path, rights in rules:
        if not rights & right:
            continue
        prefix = rule_path.rstrip("\\/") + os.sep
        if real == rule_path or real.startswith(prefix):
            return True
    return False


def _tree(root: Path) -> Path:
    (root / "secrets").mkdir(parents=True)
    (root / "sub" / "deep").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "keep.txt").write_text("keep", encoding="utf-8")
    (root / "secrets" / "a.pem").write_text("A", encoding="utf-8")
    (root / "secrets" / "b.txt").write_text("B", encoding="utf-8")
    (root / "sub" / "deep" / "c.pem").write_text("C", encoding="utf-8")
    (root / ".git" / "x.pem").write_text("X", encoding="utf-8")
    return root


def _symlink(target: Path, link: Path, *, directory: bool = False) -> None:
    """A symlink; for a directory on Windows a junction when symlinks are denied."""
    try:
        os.symlink(target, link, target_is_directory=directory)
        return
    except (OSError, NotImplementedError):
        pass
    if directory and os.name == "nt":
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
            return
        except (ImportError, AttributeError, OSError):
            pass
    pytest.skip("neither a symlink nor a junction can be created here")


# --------------------------------------------------------------------------
# validation and glob semantics
# --------------------------------------------------------------------------


def test_validate_deny_read_accepts_and_deduplicates():
    assert lrr.validate_deny_read(None) == []
    assert lrr.validate_deny_read([]) == []
    assert lrr.validate_deny_read(["*.pem", " *.pem ", "secrets/**", ".env"]) == [
        "*.pem",
        "secrets/**",
        ".env",
    ]


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("*.pem", "not_a_list"),
        ([1], "not_a_string"),
        (["  "], "empty"),
        (["/etc/passwd"], "not_relative"),
        (["C:/x"], "not_relative"),
        (["a\\b"], "backslash"),
        (["a/../b"], "bad_component"),
        (["a//b"], "bad_component"),
        (["x\x00y"], "nul_character"),
        (["a" * (lrr.MAX_DENY_GLOB_CHARS + 1)], "too_long"),
        ([f"g{i}" for i in range(lrr.MAX_DENY_READ + 1)], "too_many"),
    ],
)
def test_validate_deny_read_refuses_with_a_typed_reason(raw, reason):
    with pytest.raises(lrr.SandboxVisibilityError) as info:
        lrr.validate_deny_read(raw)
    assert str(info.value).startswith(f"deny_read_invalid:{reason}")
    assert isinstance(info.value, ValueError)


@pytest.mark.parametrize(
    ("glob", "path", "expected"),
    [
        ("*.pem", "a.pem", True),
        ("*.pem", "x/y/a.pem", True),
        ("*.pem", "a.pem.bak", False),
        ("secrets/*.key", "secrets/a.key", True),
        ("secrets/*.key", "secrets/x/a.key", False),
        ("secrets/*.key", "other/secrets/a.key", False),
        ("**/id_*", "id_rsa", True),
        ("**/id_*", "home/.ssh/id_rsa", True),
        ("conf/**", "conf/a/b.txt", True),
        ("file?.txt", "file1.txt", True),
        ("file?.txt", "file10.txt", False),
        ("[!a]b", "cb", True),
        ("[!a]b", "ab", False),
        ("[ab]c", "bc", True),
        (".env", "deep/dir/.env", True),
    ],
)
def test_glob_semantics(glob, path, expected, monkeypatch):
    monkeypatch.setattr(lrr.os, "name", "posix")
    assert bool(lrr.glob_regex(glob).fullmatch(path)) is expected


def test_glob_is_case_insensitive_only_on_windows(monkeypatch):
    monkeypatch.setattr(lrr.platform_io, "is_windows", lambda *_: True)
    nt_pattern = lrr.glob_regex(".env")
    monkeypatch.setattr(lrr.platform_io, "is_windows", lambda *_: False)
    posix_pattern = lrr.glob_regex(".env")
    monkeypatch.undo()
    assert nt_pattern.fullmatch(".ENV")
    assert nt_pattern.fullmatch("deep/.Env")
    assert not posix_pattern.fullmatch(".ENV")
    assert posix_pattern.fullmatch(".env")


def test_malformed_bracket_glob_is_refused_at_create_and_at_launch(tmp_path):
    # Create: core's one validation call for both card read fields.
    with pytest.raises(lrr.SandboxVisibilityError) as info:
        lrr.validate_read_surface(None, ["[z-a]"], tmp_path)
    assert str(info.value) == "deny_read_glob_invalid:[z-a]"
    assert isinstance(info.value, ValueError)
    # Launch: the launcher re-validates the stored card field ...
    with pytest.raises(lrr.SandboxVisibilityError, match=r"^deny_read_glob_invalid:\[z-a\]$"):
        lrr.validate_deny_read(["*.pem", "[z-a]"])
    # ... and a stored bad card reaching the scan is still a typed refusal.
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    with pytest.raises(lrr.SandboxVisibilityError, match=r"^deny_read_glob_invalid:"):
        lrr.prepare_launch_visibility(
            backend="windows_appcontainer",
            worktree=worktree,
            repo=tmp_path,
            read_roots=[],
            deny_read=["[z-a]"],
            plan_path=tmp_path / "plan.json",
            worker_argv=["worker"],
            write_plan=lambda *_: None,
        )


def test_read_surface_fields_keep_a_card_without_them_unchanged():
    assert lrr.read_surface_fields([], []) == {}
    assert lrr.read_surface_fields(["/d"], []) == {"read_roots": ["/d"]}
    assert lrr.read_surface_fields([], ["*.pem"]) == {"deny_read": ["*.pem"]}


# --------------------------------------------------------------------------
# denied file discovery
# --------------------------------------------------------------------------


def test_denied_files_match_by_glob_and_skip_git(tmp_path):
    root = _tree(tmp_path / "root")
    denied = lrr.denied_files([root], ["*.pem"])
    assert denied == sorted(
        os.path.realpath(p)
        for p in (root / "secrets" / "a.pem", root / "sub" / "deep" / "c.pem")
    )


def test_denied_files_directory_match_denies_everything_beneath(tmp_path):
    root = _tree(tmp_path / "root")
    denied = lrr.denied_files([root], ["secrets"])
    assert denied == sorted(
        os.path.realpath(p) for p in (root / "secrets" / "a.pem", root / "secrets" / "b.txt")
    )


def test_denied_files_symlink_denies_its_regular_file_target(tmp_path):
    root = _tree(tmp_path / "root")
    outside = tmp_path / "outside.txt"
    outside.write_text("O", encoding="utf-8")
    try:
        os.symlink(outside, root / "link.pem")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted here")
    assert os.path.realpath(outside) in lrr.denied_files([root], ["link.pem"])


def test_denied_files_scan_and_hit_caps_refuse(tmp_path):
    root = _tree(tmp_path / "root")
    with pytest.raises(lrr.SandboxVisibilityError, match="deny_read_refused:scan_cap_exceeded"):
        lrr.denied_files([root], ["**/*.pem"], max_entries=2)
    with pytest.raises(
        lrr.SandboxVisibilityError, match="deny_read_refused:too_many_denied_files"
    ):
        lrr.denied_files([root], ["*.pem"], max_files=1)


def test_denied_files_missing_root_refuses(tmp_path):
    with pytest.raises(lrr.SandboxVisibilityError, match="root_not_a_directory"):
        lrr.denied_files([tmp_path / "absent"], ["*.pem"])


def test_denied_files_follow_a_symlinked_directory_inside_the_roots(tmp_path):
    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    (root / "real" / ".env").write_text("E", encoding="utf-8")
    _symlink(root / "real", root / "linked", directory=True)
    env = os.path.realpath(root / "real" / ".env")
    assert lrr.denied_files([root], [".env"]) == [env]
    # Only the path through the link matches these globs: the link is followed
    # both as a static scan base and as an entry met during the walk.
    assert lrr.denied_files([root], ["linked/.env"]) == [env]
    assert lrr.denied_files([root], ["**/linked/.env"]) == [env]
    assert lrr.denied_files([root], ["linked"]) == [env]


def test_denied_files_follow_a_symlinked_directory_into_a_read_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    data = tmp_path / "data"
    (data / "conf").mkdir(parents=True)
    (data / "conf" / ".env").write_text("E", encoding="utf-8")
    _symlink(data / "conf", root / "conf", directory=True)
    env = os.path.realpath(data / "conf" / ".env")
    assert lrr.denied_files([root, data], ["conf/.env"]) == [env]


def _outside_link(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".env").write_text("E", encoding="utf-8")
    _symlink(outside, root / "out", directory=True)
    return root


def test_outside_link_reachable_with_a_matching_glob_is_refused(tmp_path):
    root = _outside_link(tmp_path)
    # Default granted=None: every path counts as readable, so fail closed.
    with pytest.raises(lrr.SandboxVisibilityError) as info:
        lrr.denied_files([root], [".env"])
    assert str(info.value) == "deny_read_unenforceable_link:out"
    assert isinstance(info.value, ValueError)
    # Target beneath an explicitly granted root: the same typed refusal, also
    # when the link is a static scan base.
    with pytest.raises(lrr.SandboxVisibilityError, match=r"^deny_read_unenforceable_link:out$"):
        lrr.denied_files([root], ["out/*"], granted=[tmp_path])
    # No glob could match beneath the link: nothing to enforce, no refusal.
    assert lrr.denied_files([root], ["[!o]*/x.pem"], granted=[tmp_path]) == []
    # A Landlock launch refuses before any plan is written.
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(lrr.SandboxVisibilityError, match=r"^deny_read_unenforceable_link:out$"):
        lrr.prepare_launch_visibility(
            backend="landlock",
            worktree=root,
            repo=repo,
            read_roots=[],
            deny_read=[".env"],
            plan_path=tmp_path / "plan.json",
            worker_argv=["worker"],
            write_plan=lambda *_: pytest.fail("refused before planning"),
        )


def test_outside_link_unreachable_is_recorded_and_the_launch_proceeds(tmp_path):
    root = _outside_link(tmp_path)
    denied, unreachable = lrr.scan_deny_read([root], [".env"], granted=[])
    # Not followed and not put in the denied set: nothing is handed to the
    # read layer for it (the Landlock-side classification is unchanged).
    assert denied == [] and unreachable == ["out"]
    assert lrr._link_receipt(unreachable)["deny_read_links"] == [
        {"link": "out", "status": "outside_link_unreachable"}
    ]
    # Windows never classifies reachability: the same link is refused.
    with pytest.raises(lrr.SandboxVisibilityError, match=r"^deny_read_unenforceable_link:out$"):
        lrr.prepare_launch_visibility(
            backend="windows_appcontainer",
            worktree=root,
            repo=tmp_path,
            read_roots=[],
            deny_read=[".env"],
            plan_path=tmp_path / "plan.json",
            worker_argv=["worker"],
            write_plan=lambda *_: None,
        )


def test_a_glob_matching_only_through_a_second_link_still_denies(tmp_path):
    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    (root / "real" / "token.txt").write_text("T", encoding="utf-8")
    _symlink(root / "real", root / "first", directory=True)
    _symlink(root / "real", root / "second", directory=True)
    token = os.path.realpath(root / "real" / "token.txt")
    # No global visited set: whichever link the walk meets second is still
    # scanned under its own name.
    for name in ("first", "second"):
        assert lrr.denied_files([root], [f"**/{name}/token.txt"]) == [token], name
        assert lrr.denied_files([root], [f"{name}/*"]) == [token], name
    assert lrr.denied_files([root], ["**/token.txt"]) == [token]


def test_denied_files_stop_at_a_directory_link_cycle(tmp_path):
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    (root / "a" / ".env").write_text("E", encoding="utf-8")
    _symlink(root, root / "a" / "loop", directory=True)
    env = os.path.realpath(root / "a" / ".env")
    # No glob could match beneath a/loop: the cycle is skipped silently.
    assert lrr.denied_files([root], ["a/.env"]) == [env]
    # A glob that matches only through the cycle link is refused, never skipped.
    with pytest.raises(lrr.SandboxVisibilityError) as info:
        lrr.denied_files([root], ["**/loop/a/.env"])
    assert str(info.value) == "deny_read_cycle_link:a/loop"
    assert isinstance(info.value, ValueError)
    # A basename glob could match anywhere beneath the cycle: refused too.
    with pytest.raises(lrr.SandboxVisibilityError, match=r"^deny_read_cycle_link:a/loop$"):
        lrr.denied_files([root], [".env"])


# --------------------------------------------------------------------------
# rule construction
# --------------------------------------------------------------------------


def test_plan_without_denials_grants_root_and_each_read_root(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    rules = lrr.plan_read_layer(read_roots=[str(root)], denied=[])
    full = READ_FILE | READ_DIR
    assert rules[0] == (lrr._top(os.path.abspath(os.sep)), full)
    assert (os.path.realpath(root), full) in rules
    assert all(rights & ~lrr.READ_LAYER_HANDLED == 0 for _, rights in rules)


@pytest.fixture
def confined(tmp_path, monkeypatch):
    """List only tmp_path's subtree.

    The split walks every ancestor of a denied file; inside a validation
    sandbox an ancestor (a volume root under AppContainer) may be unlistable,
    and a crowded host temp directory must not trip the production cap.
    Ancestors outside tmp_path are reported empty, so the rules asserted here
    are exactly the ones beneath it.
    """
    real = lrr._list_dir
    inside = os.path.realpath(tmp_path)

    def list_dir(directory):
        if directory == inside or directory.startswith(inside.rstrip("\\/") + os.sep):
            return real(directory)
        return []

    monkeypatch.setattr(lrr, "_list_dir", list_dir)
    monkeypatch.setattr(lrr, "MAX_LAYER_RULES", UNBOUNDED_RULES)


def test_plan_with_denials_never_grants_read_file_on_a_denied_file(tmp_path, confined):
    root = _tree(tmp_path / "root")
    (tmp_path / "sibling.txt").write_text("s", encoding="utf-8")
    denied = lrr.denied_files([root], ["*.pem"])
    rules = lrr.plan_read_layer(read_roots=[str(root)], denied=denied)
    for path in denied:
        assert not _granted(path, rules, READ_FILE), path
    for path in (
        root / "keep.txt",
        root / "secrets" / "b.txt",
        root / ".git" / "x.pem",
        tmp_path / "sibling.txt",  # a sibling of the split path stays readable
    ):
        assert _granted(path, rules, READ_FILE), path
    # The read root itself is split, never granted whole, and listing stays.
    assert (os.path.realpath(root), READ_FILE | READ_DIR) not in rules
    assert all(rights == READ_DIR for path, rights in rules if path == lrr._top(str(root)))
    assert _granted(root / "secrets", rules, READ_DIR)
    assert all(rights & ~lrr.READ_LAYER_HANDLED == 0 for _, rights in rules)


def test_plan_rule_cap_refuses(tmp_path, confined):
    root = _tree(tmp_path / "root")
    denied = lrr.denied_files([root], ["*.pem"])
    with pytest.raises(lrr.SandboxVisibilityError, match="deny_read_refused:rule_cap_exceeded"):
        lrr.plan_read_layer(read_roots=[str(root)], denied=denied, max_rules=2)


def test_plan_refuses_an_unlistable_split_directory(tmp_path, monkeypatch):
    root = _tree(tmp_path / "root")
    denied = lrr.denied_files([root], ["*.pem"])

    def unlistable(directory):
        raise PermissionError(directory)

    monkeypatch.setattr(lrr, "_list_dir", unlistable)
    with pytest.raises(
        lrr.SandboxVisibilityError, match="deny_read_refused:unlistable_directory"
    ):
        lrr.plan_read_layer(read_roots=[], denied=denied)


def test_plan_document_round_trips_through_load_plan(tmp_path):
    rules = [(str(tmp_path.resolve()), READ_FILE | READ_DIR)]
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(lrr.plan_document(rules)), encoding="utf-8")
    assert lrr.load_plan(plan) == rules


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_id="other"),
        lambda d: d.update(handled=READ_FILE),
        lambda d: d.update(rules=[]),
        lambda d: d.update(rules=[["relative", READ_FILE]]),
        lambda d: d.update(rules=[[os.path.abspath(os.sep), 1 << 1]]),
        lambda d: d.update(rules=[[os.path.abspath(os.sep), True]]),
    ],
)
def test_load_plan_rejects_a_bad_plan(tmp_path, mutate):
    document = lrr.plan_document([(os.path.abspath(os.sep), READ_DIR)])
    mutate(document)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(lrr.SandboxVisibilityError, match="read_layer_failed:plan"):
        lrr.load_plan(plan)


def test_main_usage_error_returns_2():
    assert lrr.main(["--plan"]) == 2


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="non-Linux refusal")
def test_apply_read_layer_refuses_off_linux():
    with pytest.raises(lrr.SandboxVisibilityError, match="read_layer_failed:not_linux"):
        lrr.apply_read_layer([(os.path.abspath(os.sep), READ_DIR)])


# --------------------------------------------------------------------------
# backend gate and the launch receipt
# --------------------------------------------------------------------------


def test_check_backend_contract():
    lrr.check_backend("bubblewrap", read_roots=[], deny_read=[])
    lrr.check_backend("windows_appcontainer", read_roots=["/x"], deny_read=["*.pem"])
    lrr.check_backend("landlock", read_roots=["/x"], deny_read=[], landlock_abi=lambda: 3)
    with pytest.raises(lrr.SandboxVisibilityError) as info:
        lrr.check_backend("landlock", read_roots=["/x"], deny_read=[], landlock_abi=lambda: 0)
    assert str(info.value) == "read_roots_landlock_unavailable:abi=0"
    with pytest.raises(lrr.SandboxVisibilityError) as info:
        lrr.check_backend("landlock", read_roots=[], deny_read=["a"], landlock_abi=lambda: 0)
    assert str(info.value).startswith("deny_read_landlock_unavailable")
    with pytest.raises(lrr.SandboxVisibilityError) as info:
        lrr.check_backend("bubblewrap", read_roots=["/x"], deny_read=[])
    assert str(info.value) == "read_roots_unsupported_backend"
    with pytest.raises(lrr.SandboxVisibilityError) as info:
        lrr.check_backend("bubblewrap", read_roots=[], deny_read=["a"])
    assert str(info.value) == "deny_read_unsupported_backend:bubblewrap"


def _dirs(tmp_path):
    worktree = _tree(tmp_path / "worktree")
    repo = tmp_path / "repo"
    repo.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    (data / "token.pem").write_text("T", encoding="utf-8")
    (data / "ok.csv").write_text("1", encoding="utf-8")
    return worktree, repo, data


def test_prepare_launch_visibility_without_fields_is_unchanged(tmp_path):
    worktree, repo, _data = _dirs(tmp_path)
    argv, receipt, deny = lrr.prepare_launch_visibility(
        backend="windows_appcontainer",
        worktree=worktree,
        repo=repo,
        read_roots=[],
        deny_read=[],
        plan_path=tmp_path / "plan.json",
        worker_argv=["worker", "--x"],
        write_plan=lambda *_: pytest.fail("no plan without fields"),
    )
    assert argv == ["worker", "--x"]
    assert deny == {}
    assert receipt["schema_id"] == lrr.VISIBILITY_SCHEMA_ID
    assert receipt["read_roots_enforcement"] == "none"
    assert receipt["deny_read_enforcement"] == "none"
    assert receipt["workspace_prepare"] == {"status": "not_applicable"}
    # Declared limitation, always on the receipt and in the review summary.
    assert receipt["git_object_store_readable"] is True
    summary = review_packet.summarize_sandbox_visibility(receipt)
    assert summary["git_object_store_readable"] is True


def test_prepare_launch_visibility_landlock_wraps_the_argv(tmp_path, confined):
    worktree, repo, data = _dirs(tmp_path)
    written: dict[str, object] = {}

    def write_plan(path, document):
        written["path"] = path
        Path(path).write_text(json.dumps(document), encoding="utf-8")

    plan = tmp_path / "req.read-layer.json"
    argv, receipt, deny = lrr.prepare_launch_visibility(
        backend="landlock",
        worktree=worktree,
        repo=repo,
        read_roots=[str(data)],
        deny_read=["*.pem"],
        plan_path=plan,
        worker_argv=["/usr/bin/worker", "--flag"],
        write_plan=write_plan,
        workspace_prepare={"status": "succeeded", "returncode": 0},
    )
    assert deny == {}
    assert argv[1].endswith("linux_read_roots.py")
    assert argv[2:5] == ["--plan", os.fspath(plan), "--"]
    assert argv[5:] == ["/usr/bin/worker", "--flag"]
    assert written["path"] == plan
    rules = lrr.load_plan(plan)
    assert receipt["read_layer_rule_count"] == len(rules)
    assert receipt["denied_file_count"] == 3  # a.pem, c.pem, token.pem
    assert receipt["read_roots_enforcement"] == "landlock_read_layer"
    assert receipt["deny_read_enforcement"] == "landlock_read_layer"
    assert receipt["workspace_prepare"]["status"] == "succeeded"
    assert not _granted(data / "token.pem", rules, READ_FILE)
    assert _granted(data / "ok.csv", rules, READ_FILE)


def _windows_launch(tmp_path, worktree, read_roots, deny_read):
    return lrr.prepare_launch_visibility(
        backend="windows_appcontainer",
        worktree=worktree,
        repo=tmp_path / "repo",
        read_roots=read_roots,
        deny_read=deny_read,
        plan_path=tmp_path / "plan.json",
        worker_argv=["worker"],
        write_plan=lambda *_: pytest.fail("AppContainer writes no plan"),
    )


def test_appcontainer_deny_glob_matching_a_shared_read_root_is_refused(tmp_path):
    worktree, _repo, data = _dirs(tmp_path)
    # token.pem lives in a shared read root, outside this launch's worktree:
    # no per-launch ACE can cover it, so the launch fails closed.
    with pytest.raises(lrr.SandboxVisibilityError) as info:
        _windows_launch(tmp_path, worktree, [str(data)], ["*.pem"])
    assert str(info.value) == "deny_read_shared_read_root_unsupported:token.pem"
    assert isinstance(info.value, ValueError)


def test_windows_outside_link_into_a_granted_read_root_is_refused(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    data = tmp_path / "data"
    (data / "conf").mkdir(parents=True)
    (data / "conf" / "x.txt").write_text("X", encoding="utf-8")
    _symlink(data / "conf", worktree / "cfg", directory=True)
    # The target is readable through the read root grant, but Windows never
    # classifies reachability: a link out of the worktree is refused.
    with pytest.raises(lrr.SandboxVisibilityError) as info:
        _windows_launch(tmp_path, worktree, [str(data)], ["cfg/*.txt"])
    assert str(info.value) == "deny_read_unenforceable_link:cfg"
    # Landlock keeps following a link into a scanned root and denies the file.
    denied, _ = lrr.scan_deny_read([worktree, data], ["cfg/*.txt"])
    assert denied == [os.path.realpath(data / "conf" / "x.txt")]


def test_prepare_launch_visibility_appcontainer_returns_deny_paths(tmp_path):
    worktree, _repo, data = _dirs(tmp_path)
    argv, receipt, spec = _windows_launch(tmp_path, worktree, [str(data)], ["secrets/*"])
    assert argv == ["worker"]
    assert spec["deny_read_paths"] == sorted(
        os.path.realpath(worktree / "secrets" / name) for name in ("a.pem", "b.txt")
    )
    # ACEs only ever land in this launch's own worktree.
    assert spec["deny_read_roots"] == [os.path.realpath(worktree)]
    assert set(spec) == {"deny_read_paths", "deny_read_roots"}
    assert receipt["deny_read_enforcement"] == "appcontainer_deny_ace"
    assert receipt["read_roots_enforcement"] == "appcontainer_read_execute_grant"
    assert receipt["deny_read_refused_count"] == 0


def test_prepare_launch_visibility_refuses_other_backends(tmp_path):
    worktree, repo, data = _dirs(tmp_path)
    with pytest.raises(lrr.SandboxVisibilityError, match="read_roots_unsupported_backend"):
        lrr.prepare_launch_visibility(
            backend="bubblewrap",
            worktree=worktree,
            repo=repo,
            read_roots=[str(data)],
            deny_read=[],
            plan_path=tmp_path / "plan.json",
            worker_argv=["worker"],
            write_plan=lambda *_: None,
        )


# --------------------------------------------------------------------------
# AppContainer deny ACE
# --------------------------------------------------------------------------


def test_appcontainer_deny_writes_one_explicit_deny_ace_per_file(tmp_path):
    target = os.path.realpath(tmp_path / "a.pem")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    refused = lrr.apply_appcontainer_deny([target], SID, roots=[tmp_path], run=run)
    assert refused == []
    argv, kwargs = calls[0]
    assert argv[0].lower().endswith("icacls.exe")
    assert argv[1:] == [target, "/deny", f"*{SID}:(R)", "/Q"]
    assert kwargs["shell"] is False and kwargs["timeout"] > 0


def test_appcontainer_deny_failure_and_bad_sid_are_typed(tmp_path):
    target = os.path.realpath(tmp_path / "a.pem")
    with pytest.raises(lrr.SandboxVisibilityError, match="deny_read_ace_failed:exit=5"):
        lrr.apply_appcontainer_deny(
            [target], SID, roots=[tmp_path], run=lambda *a, **k: SimpleNamespace(returncode=5)
        )
    with pytest.raises(lrr.SandboxVisibilityError, match="deny_read_ace_failed:invalid_sid"):
        lrr.apply_appcontainer_deny(
            [target], "S-1-5-32-544", roots=[tmp_path], run=lambda *a, **k: None
        )

    def timeout(*_a, **_k):
        raise subprocess.TimeoutExpired("icacls", 1)

    with pytest.raises(lrr.SandboxVisibilityError, match="deny_read_ace_failed:spawn"):
        lrr.apply_appcontainer_deny([target], SID, roots=[tmp_path], run=timeout)


def _recording_run(calls):
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    return run


def test_appcontainer_deny_never_writes_an_ace_outside_the_roots(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    inside = os.path.realpath(worktree / "a.pem")
    outside = str(tmp_path / "outside.txt")
    calls: list[list[str]] = []
    refused = lrr.apply_appcontainer_deny(
        [outside, inside], SID, roots=[worktree], run=_recording_run(calls)
    )
    assert refused == [outside]
    assert [argv[1] for argv in calls] == [inside]


def test_appcontainer_deny_never_follows_a_link_whose_realpath_escapes(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".env").write_text("O", encoding="utf-8")
    _symlink(outside, worktree / "linked", directory=True)
    calls: list[list[str]] = []
    through = str(worktree / "linked" / ".env")
    refused = lrr.apply_appcontainer_deny(
        [through], SID, roots=[worktree], run=_recording_run(calls)
    )
    assert calls == []
    assert refused == [through]
    # The launch side never classifies reachability on Windows: a link out of
    # the worktree with a glob that could match beneath it is refused.
    with pytest.raises(lrr.SandboxVisibilityError, match=r"^deny_read_unenforceable_link:linked$"):
        lrr.prepare_launch_visibility(
            backend="windows_appcontainer",
            worktree=worktree,
            repo=tmp_path,
            read_roots=[],
            deny_read=[".env"],
            plan_path=tmp_path / "plan.json",
            worker_argv=["worker"],
            write_plan=lambda *_: pytest.fail("AppContainer writes no plan"),
        )


def test_appcontainer_deny_never_follows_a_file_symlink_leaf_outside(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("O", encoding="utf-8")
    _symlink(outside, worktree / ".env")
    calls: list[list[str]] = []
    leaf = str(worktree / ".env")
    refused = lrr.apply_appcontainer_deny(
        [leaf], SID, roots=[worktree], run=_recording_run(calls)
    )
    assert calls == [] and refused == [leaf]
    with pytest.raises(lrr.SandboxVisibilityError, match=r"^deny_read_unenforceable_link:file_link_outside_worktree$"):
        lrr.prepare_launch_visibility(
            backend="windows_appcontainer",
            worktree=worktree,
            repo=tmp_path,
            read_roots=[],
            deny_read=[".env"],
            plan_path=tmp_path / "plan.json",
            worker_argv=["worker"],
            write_plan=lambda *_: None,
        )


def test_worktree_deny_ace_is_written_and_never_removed_at_teardown(tmp_path):
    from aiworkhub import worker_supervisor

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    target = os.path.realpath(worktree / "a.pem")
    calls: list[list[str]] = []
    run = _recording_run(calls)
    assert lrr.apply_appcontainer_deny([target], SID, roots=[worktree], run=run) == []
    assert [argv[1:] for argv in calls] == [[target, "/deny", f"*{SID}:(R)", "/Q"]]
    # The ACE lives on this launch's own worktree file and goes with it: no
    # removal helper, no shared reference map, no supervisor teardown hook.
    for name in (
        "remove_appcontainer_deny",
        "release_appcontainer_deny",
        "appcontainer_deny_cleanup",
        "appcontainer_remove_deny_argv",
        "deny_ace_refs_path",
    ):
        assert not hasattr(lrr, name), name
    parameters = inspect.signature(worker_supervisor._AppContainerProcess).parameters
    assert "deny_cleanup" not in parameters
    launch = SimpleNamespace(pid=4242, close=lambda: None)
    process = worker_supervisor._AppContainerProcess(launch, io.BytesIO(), io.BytesIO())
    process.close()
    assert len(calls) == 1  # closing ran no icacls
    assert "deny_read_ace_cleanup" not in process.bridge_diagnostics


# --------------------------------------------------------------------------
# review packet view
# --------------------------------------------------------------------------


def test_review_packet_distinguishes_could_not_run_from_did_not_run(tmp_path):
    base = {"backend": "landlock", "read_roots": ["/data"], "deny_read": ["*.pem"]}
    for status, verdict in (
        ("succeeded", "ran"),
        ("timed_out", "could_not_run"),
        ("spawn_failed", "could_not_run"),
        ("failed", "could_not_run"),
        ("not_configured", "did_not_run"),
        ("not_applicable", "did_not_run"),
    ):
        summary = review_packet.summarize_sandbox_visibility(
            {**base, "workspace_prepare": {"status": status, "output_tail": "x" * 5000}}
        )
        assert summary["workspace_prepare"]["verdict"] == verdict
        assert len(summary["workspace_prepare"]["output_tail"]) <= review_packet.MAX_TAIL_CHARS
        assert summary["backend"] == "landlock"
        assert summary["deny_read"] == ["*.pem"]


def test_review_packet_reads_the_launch_record(tmp_path):
    assert review_packet.launch_sandbox_visibility(tmp_path, "abc123") is None
    record = tmp_path / "abc123.request.json"
    record.write_text(json.dumps({"sandbox_backend": "landlock"}), encoding="utf-8")
    assert review_packet.launch_sandbox_visibility(tmp_path, "abc123") is None
    record.write_text(
        json.dumps({
            "sandbox_visibility": {
                "backend": "landlock",
                "read_roots": ["/data"],
                "read_roots_enforcement": "landlock_read_layer",
                "workspace_prepare": {"status": "failed", "returncode": 3},
            }
        }),
        encoding="utf-8",
    )
    summary = review_packet.launch_sandbox_visibility(tmp_path, "abc123")
    assert summary["read_roots_enforcement"] == "landlock_read_layer"
    assert summary["workspace_prepare"]["returncode"] == 3
    record.write_text("{not json", encoding="utf-8")
    assert review_packet.launch_sandbox_visibility(tmp_path, "abc123")["status"] == (
        "launch_record_unreadable"
    )
    assert review_packet.launch_sandbox_visibility(tmp_path, "../x")["status"] == (
        "launch_record_unreadable"
    )


# --------------------------------------------------------------------------
# real Landlock enforcement (Linux only)
# --------------------------------------------------------------------------


@pytest.mark.skipif(_landlock_abi() < 1, reason="needs a Linux kernel with Landlock")
def test_real_landlock_read_layer_denies_only_the_denied_files(tmp_path):
    root = _tree(tmp_path / "root")
    data = tmp_path / "data"
    data.mkdir()
    (data / "rows.csv").write_text("rows", encoding="utf-8")
    denied = lrr.denied_files([root, data], ["*.pem"])
    rules = lrr.plan_read_layer(
        read_roots=[str(data)], denied=denied, max_rules=UNBOUNDED_RULES
    )
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(lrr.plan_document(rules)), encoding="utf-8")
    probe = (
        "import json, sys\n"
        "out = {}\n"
        "for p in sys.argv[1:]:\n"
        "    try:\n"
        "        with open(p) as h:\n"
        "            out[p] = h.read()\n"
        "    except PermissionError:\n"
        "        out[p] = 'EACCES'\n"
        "print(json.dumps(out))\n"
    )
    targets = [
        str(root / "secrets" / "a.pem"),
        str(root / "sub" / "deep" / "c.pem"),
        str(root / "secrets" / "b.txt"),
        str(root / "keep.txt"),
        str(data / "rows.csv"),
    ]
    result = subprocess.run(
        [
            sys.executable,
            str(Path(lrr.__file__).resolve()),
            "--plan",
            str(plan),
            "--",
            sys.executable,
            "-c",
            probe,
            *targets,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    assert seen[targets[0]] == "EACCES"
    assert seen[targets[1]] == "EACCES"
    assert seen[targets[2]] == "B"
    assert seen[targets[3]] == "keep"
    assert seen[targets[4]] == "rows"
