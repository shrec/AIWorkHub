"""NF-2026-01004: the Linux Landlock read layer enforces a read allow-list.

On 0.13.16 a Landlock worker read blind DEV/TEST data under a host data mount
outside the card's ``read_roots``: the stacked read layer built by
``linux_read_roots.prepare_launch_visibility`` granted the filesystem root and
only *split* around denied files.  With ``read_roots`` declared it is now an
allow-list layer: the supervisor computes the allow set at plan time and
writes it into the plan, and the child only applies the plan.  A read surface
no backend can enforce is refused with ``read_allowlist_unenforceable`` unless
the owner opts out.

Plan and gate tests are pure and run on every OS; the real open() probe needs a
Linux kernel with Landlock and is skipped elsewhere with the reason.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import linux_read_roots as lrr
from aiworkhub.linux_kernel_abi import (
    LANDLOCK_ACCESS_FS_READ_DIR,
    LANDLOCK_ACCESS_FS_READ_FILE,
    LANDLOCK_CREATE_RULESET_VERSION,
    SYS_LANDLOCK_CREATE_RULESET,
)

READ_FILE = LANDLOCK_ACCESS_FS_READ_FILE
READ_DIR = LANDLOCK_ACCESS_FS_READ_DIR
FULL = READ_FILE | READ_DIR
OPT_OUT = lrr.READ_ALLOWLIST_OPT_OUT_ENV


def _granted(path: Path | str, rules, right: int = READ_FILE) -> bool:
    real = os.path.normcase(os.path.realpath(path))
    for rule_path, rights in rules:
        base = os.path.normcase(rule_path)
        if rights & right and (real == base or real.startswith(base.rstrip("\\/") + os.sep)):
            return True
    return False


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _layout(tmp_path: Path) -> SimpleNamespace:
    """A repo with a linked worktree, request roots, a card root and a host data mount."""
    repo = tmp_path / "repo"
    gitdir = repo / ".git" / "worktrees" / "wt"
    _write(gitdir / "commondir", "../..\n")
    _write(gitdir / "HEAD", "0123456789abcdef\n")
    worktree = tmp_path / "wt"
    _write(worktree / ".git", f"gitdir: {gitdir}\n")
    worker_temp = repo / ".aiworkhub" / "temp" / "worker" / "rid"
    request_home = tmp_path / "request" / "home"
    return SimpleNamespace(
        repo=repo,
        worktree=worktree,
        worker_temp=worker_temp,
        request_home=request_home,
        card=tmp_path / "card",
        account_home=tmp_path / "account",
        git_object=_write(repo / ".git" / "objects" / "ab" / "cdef", "blob"),
        worktree_file=_write(worktree / "mod.py", "print(1)\n"),
        card_file=_write(tmp_path / "card" / "ok.csv", "1"),
        card_secret=_write(tmp_path / "card" / "token.pem", "T"),
        temp_file=_write(worker_temp / "t.txt"),
        home_file=_write(request_home / ".claude" / "settings.json", "{}"),
        host_data=_write(tmp_path / "mnt" / "data" / "blind.csv", "blind"),
        checkout_file=_write(repo / "README.md", "checkout"),
        runtime_file=_write(repo / ".aiworkhub" / "runtime" / "secret.json", "{}"),
        other_temp=_write(repo / ".aiworkhub" / "temp" / "other" / "x.txt"),
        sibling_file=_write(tmp_path / "sibling-wt" / "mod.py"),
        account_file=_write(tmp_path / "account" / ".ssh" / "id_ed25519", "key"),
    )


def _landlock_exec_argv(lay: SimpleNamespace) -> list[str]:
    """The shape worker_workspace.sandbox_argv builds for the Landlock backend."""
    return [
        sys.executable,
        "/pkg/aiworkhub/worker_workspace.py",
        "--landlock-exec",
        "--workspace", str(lay.worktree),
        "--home", str(lay.request_home),
        "--allow", "src/**",
        "--worker-temp", str(lay.worker_temp),
        "--",
        os.path.abspath(os.path.join(os.sep, "nonexistent", "bin", "claude")),
        "-p",
    ]


def _launch(lay, tmp_path, *, deny_read=(), worker_argv=None, **extra):
    plan = tmp_path / "req.read-layer.json"

    def write_plan(path, document):
        Path(path).write_text(json.dumps(document), encoding="utf-8")

    argv, receipt, spec = lrr.prepare_launch_visibility(
        backend="landlock",
        worktree=lay.worktree,
        repo=lay.repo,
        read_roots=[str(lay.card)],
        deny_read=list(deny_read),
        plan_path=plan,
        worker_argv=worker_argv if worker_argv is not None else _landlock_exec_argv(lay),
        write_plan=write_plan,
        **extra,
    )
    return argv, receipt, spec, plan


@pytest.fixture
def account_home(tmp_path, monkeypatch):
    home = os.path.realpath(tmp_path / "account")
    monkeypatch.setattr(lrr, "_account_home", lambda: home)
    return home


# --------------------------------------------------------------------------
# the allow-list plan written through prepare_launch_visibility
# --------------------------------------------------------------------------


def test_landlock_plan_with_read_roots_is_an_allow_list(tmp_path, account_home):
    lay = _layout(tmp_path)
    argv, receipt, spec, plan = _launch(lay, tmp_path)
    rules = lrr.load_plan(plan)
    # The supervisor wrote the allow set; the child only applies the plan file.
    assert argv[1] == str(Path(lrr.__file__).resolve())
    assert argv[2:5] == ["--plan", os.fspath(plan), "--"]
    assert argv[5:] == _landlock_exec_argv(lay)
    assert spec == {}
    assert receipt["read_allowlist"] == "landlock_allow_list"
    assert receipt["read_roots_enforcement"] == "landlock_read_layer"
    assert receipt["read_layer_rule_count"] == len(rules)
    # Nothing at the filesystem root: the deny-only layer granted it whole.
    assert all(path != lrr._top(path) for path, _rights in rules)
    assert all(rights & ~lrr.READ_LAYER_HANDLED == 0 for _path, rights in rules)
    for inside in (
        lay.worktree_file,
        lay.card_file,
        lay.git_object,
        lay.repo / ".git" / "worktrees" / "wt" / "HEAD",
        lay.home_file,
        lay.temp_file,
    ):
        assert _granted(inside, rules), inside
    for outside in (
        lay.host_data,
        lay.checkout_file,
        lay.runtime_file,
        lay.other_temp,
        lay.sibling_file,
        lay.account_file,
    ):
        assert not _granted(outside, rules), outside
        assert not _granted(outside.parent, rules, READ_DIR), outside.parent


def test_deny_read_inside_an_allow_root_is_split_from_that_root(tmp_path, account_home):
    lay = _layout(tmp_path)
    _argv, receipt, _spec, plan = _launch(lay, tmp_path, deny_read=["*.pem"])
    rules = lrr.load_plan(plan)
    assert receipt["denied_file_count"] == 1
    assert receipt["deny_read_enforcement"] == "landlock_read_layer"
    assert not _granted(lay.card_secret, rules)
    assert _granted(lay.card_file, rules)
    assert _granted(lay.card, rules, READ_DIR)
    # The split starts at the allow root and never climbs above it.
    assert not _granted(lay.host_data, rules)
    assert not _granted(tmp_path, rules, READ_DIR)


def test_derived_roots_never_widen_to_the_home_repo_or_an_ancestor(tmp_path, monkeypatch):
    lay = _layout(tmp_path)
    tool = tmp_path / "toolchain" / "python3.12"
    _write(tool / "lib" / "os.py")
    monkeypatch.setattr(lrr, "_account_home", lambda: os.path.realpath(lay.account_home))
    monkeypatch.setattr(
        lrr,
        "_toolchain_roots",
        lambda _argv, **_kwargs: [
            str(lay.account_home),  # the account home itself
            str(tmp_path),  # an ancestor of the home, the repo and the worktree
            str(lay.repo),  # the checkout root
            os.path.abspath(os.sep),  # the filesystem root
            str(tool),
        ],
    )
    argv = _landlock_exec_argv(lay)
    argv[argv.index("--home") + 1] = str(lay.account_home)  # a request root equal to home
    allow, dropped = lrr.read_allow_set(
        worktree=lay.worktree, repo=lay.repo, read_roots=[str(lay.card)], worker_argv=argv
    )
    real = {os.path.normcase(path) for path in allow}
    for widened in (lay.account_home, tmp_path, lay.repo):
        assert os.path.normcase(os.path.realpath(widened)) not in real
        assert os.path.realpath(widened) in dropped
    assert os.path.normcase(os.path.realpath(tool)) in real
    assert os.path.normcase(os.path.realpath(lay.card)) in real
    assert os.path.normcase(os.path.realpath(lay.worktree)) in real
    # The git read surface is the common read entries (this worktree's gitdir
    # lies beneath worktrees/), never .git whole.
    assert os.path.normcase(os.path.realpath(lay.repo / ".git")) not in real
    assert os.path.normcase(os.path.realpath(lay.repo / ".git" / "objects")) in real
    assert os.path.normcase(os.path.realpath(lay.repo / ".git" / "worktrees")) in real


def test_toolchain_grants_are_exact_install_roots(tmp_path, monkeypatch):
    version = tmp_path / ".nvm" / "versions" / "node" / "v22.1.0"
    cli = _write(version / "lib" / "node_modules" / "@scope" / "cli" / "cli.js")
    roots = lrr._toolchain_roots([str(cli)])
    # The node version root, never versions/, node_modules or its parent.
    assert roots[-1] == os.fspath(version.resolve())
    assert os.fspath((version / "lib").resolve()) not in roots
    package = os.path.dirname(os.path.realpath(lrr.__file__))
    assert package in roots and os.path.dirname(package) in roots
    # A non-node absolute adapter executable is granted as that one file.
    native = _write(tmp_path / "bin" / "agent")
    assert lrr._toolchain_roots([str(native)])[-1] == os.path.realpath(native)


def test_without_read_roots_the_deny_only_layer_is_unchanged(tmp_path):
    worktree = tmp_path / "wt"
    _write(worktree / "keep.txt")
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = tmp_path / "plan.json"
    argv, receipt, _spec = lrr.prepare_launch_visibility(
        backend="landlock",
        worktree=worktree,
        repo=repo,
        read_roots=[],
        deny_read=["*.pem"],
        plan_path=plan,
        worker_argv=["worker"],
        write_plan=lambda path, doc: Path(path).write_text(json.dumps(doc), encoding="utf-8"),
    )
    assert "read_allowlist" not in receipt
    assert argv[-1] == "worker"
    assert lrr.load_plan(plan) == [(lrr._top(os.path.abspath(os.sep)), FULL)]


def test_the_child_applies_only_the_plan(tmp_path, monkeypatch):
    rules = [(os.path.realpath(tmp_path), FULL)]
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(lrr.plan_document(rules)), encoding="utf-8")
    applied: list[list[tuple[str, int]]] = []

    class _Exec(Exception):
        pass

    def no_derivation(*_args, **_kwargs):
        pytest.fail("the child derived its own allow set")

    def fake_exec(_file, args):
        raise _Exec(args)

    monkeypatch.setattr(lrr, "read_allow_set", no_derivation)
    monkeypatch.setattr(lrr, "_toolchain_roots", no_derivation)
    monkeypatch.setattr(lrr, "apply_read_layer", lambda r, **_k: applied.append(list(r)))
    monkeypatch.setattr(lrr.os, "execvp", fake_exec)
    with pytest.raises(_Exec):
        lrr.main(["--plan", str(plan), "--", "worker", "-x"])
    assert applied == [rules]


# --------------------------------------------------------------------------
# fail closed: read_allowlist_unenforceable and the owner opt-out
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("backend", "read_roots", "deny_read", "abi", "cause"),
    [
        ("bubblewrap", ["/x"], ["*.pem"], 3, "deny_read_unsupported_backend:bubblewrap"),
        ("bubblewrap", [], ["*.pem"], 3, "deny_read_unsupported_backend:bubblewrap"),
        ("landlock", ["/x"], [], 0, "read_roots_landlock_unavailable:abi=0"),
        ("landlock", [], ["*.pem"], 0, "deny_read_landlock_unavailable:abi=0"),
    ],
)
def test_unenforceable_read_surface_is_refused_naming_the_opt_out(
    backend, read_roots, deny_read, abi, cause
):
    def gate(environ):
        return lrr.check_read_allowlist(
            backend,
            read_roots=read_roots,
            deny_read=deny_read,
            landlock_abi=lambda: abi,
            environ=environ,
        )

    for environ in ({}, {OPT_OUT: "0"}, {OPT_OUT: "yes"}):
        with pytest.raises(lrr.SandboxVisibilityError) as info:
            gate(environ)
        assert info.value.code == "read_allowlist_unenforceable"
        assert str(info.value) == (
            f"read_allowlist_unenforceable:{cause}:owner_opt_out={OPT_OUT}=1"
        )
        assert isinstance(info.value, ValueError)
    assert gate({OPT_OUT: "1"}) == "owner_opt_out"


def test_enforcing_backends_and_undeclared_cards_pass_the_gate():
    gate = lrr.check_read_allowlist
    assert gate("landlock", read_roots=["/x"], deny_read=[], landlock_abi=lambda: 1) == "enforced"
    assert gate("windows_appcontainer", read_roots=["/x"], deny_read=[]) == "enforced"
    assert gate("bubblewrap", read_roots=["/x"], deny_read=[]) == "enforced"
    assert gate("bubblewrap", read_roots=[], deny_read=[]) == "not_declared"


def test_owner_opt_out_launches_unenforced_and_an_enforcing_backend_ignores_it(
    tmp_path, monkeypatch, account_home
):
    monkeypatch.setattr(lrr, "_default_landlock_abi", lambda: 0)
    # Bubblewrap enforces read_roots (see the bubblewrap tests); deny_read alone
    # is what it cannot enforce, so that is its opt-out case.
    for backend, read_roots, deny_read in (
        ("bubblewrap", [], ["*.pem"]),
        ("landlock", [str(tmp_path)], []),
    ):
        argv, receipt, spec = lrr.prepare_launch_visibility(
            backend=backend,
            worktree=tmp_path,
            repo=tmp_path,
            read_roots=read_roots,
            deny_read=deny_read,
            plan_path=tmp_path / "plan.json",
            worker_argv=["worker"],
            write_plan=lambda *_: pytest.fail("an unenforced launch writes no plan"),
            read_allowlist_opt_out=True,
        )
        assert argv == ["worker"] and spec == {}
        field = "read_roots_enforcement" if read_roots else "deny_read_enforcement"
        assert receipt[field] == "owner_opt_out_unenforced"
        assert receipt["read_allowlist"] == "owner_opt_out_unenforced"
    monkeypatch.setattr(lrr, "_default_landlock_abi", lambda: 3)
    lay = _layout(tmp_path)
    argv, receipt, _spec, plan = _launch(lay, tmp_path, read_allowlist_opt_out=True)
    assert receipt["read_allowlist"] == "landlock_allow_list"
    assert not _granted(lay.host_data, lrr.load_plan(plan))


def test_the_isolated_launch_uses_the_gate_and_forwards_the_opt_out():
    source = (
        Path(lrr.__file__).with_name("process_launcher_launch_isolated.py")
    ).read_text(encoding="utf-8")
    assert "linux_read_roots.check_read_allowlist(" in source
    assert 'read_allowlist_opt_out=read_allowlist == "owner_opt_out"' in source
    assert "linux_read_roots.check_backend(" not in source


# --------------------------------------------------------------------------
# real kernel enforcement
# --------------------------------------------------------------------------

_PROBE = """
import errno, sys
for path in sys.argv[1:]:
    try:
        with open(path, "rb") as handle:
            handle.read(1)
        print("read", flush=True)
    except OSError as exc:
        print("denied:" + errno.errorcode.get(exc.errno, str(exc.errno)), flush=True)
"""


def _landlock_abi() -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(
        SYS_LANDLOCK_CREATE_RULESET,
        ctypes.c_void_p(),
        ctypes.c_size_t(0),
        ctypes.c_uint(LANDLOCK_CREATE_RULESET_VERSION),
    )
    return max(int(result), 0)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Landlock is a Linux kernel feature; the read layer refuses off Linux",
)
def test_real_landlock_child_reads_only_inside_the_allow_list(tmp_path, account_home):
    if _landlock_abi() < 1:
        pytest.skip("this Linux kernel exposes no Landlock ABI")
    lay = _layout(tmp_path)
    targets = [
        lay.worktree_file,
        lay.card_file,
        lay.git_object,
        lay.host_data,
        lay.checkout_file,
        lay.account_file,
    ]
    worker_argv = [sys.executable, "-I", "-c", _PROBE, *map(str, targets)]
    argv, _receipt, _spec, _plan = _launch(lay, tmp_path, worker_argv=worker_argv)
    result = subprocess.run(
        argv,
        cwd=lay.worktree,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.split()
    assert lines[:3] == ["read", "read", "read"], result.stdout
    assert all(line in {"denied:EACCES", "denied:ENOENT"} for line in lines[3:]), lines
    assert len(lines) == len(targets)


# --------------------------------------------------------------------------
# bubblewrap: each allow root --ro-bind at its own path, nothing broader
# --------------------------------------------------------------------------


def _bwrap_argv(lay: SimpleNamespace, adapter: list[str]) -> list[str]:
    """The shape worker_workspace.sandbox_argv builds for the bubblewrap backend."""
    return [
        "/usr/bin/bwrap",
        "--new-session",
        "--die-with-parent",
        "--unshare-pid",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--ro-bind", "/etc", "/etc",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--dir", "/home",
        # Joined so the tracked-file host-path scan (NF-01061) sees no home path.
        "--bind", str(lay.request_home), "/".join(("", "home", "sandbox")),
        # A write bind beneath the card root a read bind must never shadow.
        "--bind", os.path.realpath(lay.card / "out"), os.path.realpath(lay.card / "out"),
        "--bind", str(lay.worktree), "/workspace",
        "--ro-bind", str(lay.worktree / ".git"), "/workspace/.git",
        "--chdir", "/workspace",
        "--",
        *adapter,
    ]


def _bwrap_launch(lay, tmp_path, *, deny_read=(), worker_argv=None, **extra):
    return lrr.prepare_launch_visibility(
        backend="bubblewrap",
        worktree=lay.worktree,
        repo=lay.repo,
        read_roots=[str(lay.card)],
        deny_read=list(deny_read),
        plan_path=tmp_path / "plan.json",
        worker_argv=worker_argv if worker_argv is not None else _bwrap_argv(lay, [_ADAPTER]),
        write_plan=lambda *_: pytest.fail("bubblewrap writes no plan"),
        **extra,
    )


_ADAPTER = os.path.abspath(os.path.join(os.sep, "nonexistent", "bin", "claude"))


def _triples(values: list[str]) -> list[tuple[str, str, str]]:
    assert len(values) % 3 == 0, values
    return [tuple(values[i : i + 3]) for i in range(0, len(values), 3)]


def test_bubblewrap_binds_exactly_the_allow_set_read_only(tmp_path, account_home):
    lay = _layout(tmp_path)
    (lay.card / "out").mkdir()
    (lay.repo / "src").mkdir()
    base = _bwrap_argv(lay, [_ADAPTER, "-p"])
    out, receipt, spec = _bwrap_launch(lay, tmp_path, worker_argv=base)
    allow, dropped = lrr.read_allow_set(
        worktree=lay.worktree,
        repo=lay.repo,
        read_roots=[str(lay.card)],
        worker_argv=base,
        backend="bubblewrap",
    )
    card = os.path.realpath(lay.card)
    # Only the card's read roots: no system, toolchain, git or package root.
    assert dropped == []
    assert lrr.bubblewrap_read_binds(allow, worktree=lay.worktree) == [card]
    base_ops = lrr._bubblewrap_parts(base)[1]
    out_ops = lrr._bubblewrap_parts(out)[1]
    extra = [op for op in out_ops if op not in base_ops]
    assert extra == [("--ro-bind", card, card)]
    for absent in (
        "/",
        "/sys",
        "/nix/store",
        "/run/systemd/resolve",
        os.path.realpath(lay.repo / "src"),
        os.path.realpath(lay.repo / ".git" / "objects"),
        os.path.dirname(os.path.realpath(lrr.__file__)),
        os.path.realpath(sys.prefix),
    ):
        assert ("--ro-bind", absent, absent) not in extra, absent
    for outside in (lay.host_data, lay.checkout_file, lay.account_file, lay.runtime_file):
        assert not lrr._within(os.path.realpath(outside), card)
    assert spec == {}
    assert receipt["read_roots_enforcement"] == "bubblewrap_ro_bind"
    assert receipt["read_allowlist"] == "bubblewrap_ro_bind"
    assert receipt["deny_read_enforcement"] == "none"


def test_bubblewrap_read_binds_follow_the_base_mounts_and_keep_every_original_op(
    tmp_path, account_home
):
    lay = _layout(tmp_path)
    (lay.card / "out").mkdir()
    base = _bwrap_argv(lay, [_ADAPTER, "-p"])
    out, _receipt, _spec = _bwrap_launch(lay, tmp_path, worker_argv=base)
    card = os.path.realpath(lay.card)
    # Inserted once, right after the base mounts (/usr, symlinks, /etc, /proc,
    # /dev, the fresh /tmp) and before the first original worktree/write op.
    first = base.index("--dir")
    assert out == [*base[:first], "--ro-bind", card, card, *base[first:]]
    out_ops = lrr._bubblewrap_parts(out)[1]
    base_ops = lrr._bubblewrap_parts(base)[1]
    # Every original op appears once, in its original relative order.
    assert [op for op in out_ops if op != ("--ro-bind", card, card)] == base_ops
    assert all(out_ops.count(op) == 1 for op in base_ops)
    # Precedence: the nested .git ro-bind still follows the worktree bind, and
    # the write bind beneath the card root still mounts over its read bind.
    worktree_bind = out_ops.index(("--bind", str(lay.worktree), "/workspace"))
    git_bind = out_ops.index(("--ro-bind", str(lay.worktree / ".git"), "/workspace/.git"))
    assert worktree_bind < git_bind
    out_dir = os.path.realpath(lay.card / "out")
    assert out_ops.index(("--ro-bind", card, card)) < out_ops.index(("--bind", out_dir, out_dir))
    assert out.index("/tmp") < out.index(card)


def test_bubblewrap_gate_enforces_read_roots_and_refuses_deny_read(tmp_path, account_home):
    gate = lrr.check_read_allowlist
    assert gate("bubblewrap", read_roots=["/x"], deny_read=[], environ={}) == "enforced"
    assert gate("bubblewrap", read_roots=["/x"], deny_read=[], environ={OPT_OUT: "1"}) == (
        "enforced"
    )
    lay = _layout(tmp_path)
    with pytest.raises(lrr.SandboxVisibilityError, match="^deny_read_unsupported_backend"):
        _bwrap_launch(lay, tmp_path, deny_read=["*.pem"])
    # Under the owner opt-out the read roots stay bound; deny_read is unenforced.
    out, receipt, _spec = _bwrap_launch(
        lay, tmp_path, deny_read=["*.pem"], read_allowlist_opt_out=True
    )
    assert ("--ro-bind", os.path.realpath(lay.card), os.path.realpath(lay.card)) in set(
        zip(out, out[1:], out[2:])
    )
    assert receipt["read_roots_enforcement"] == "bubblewrap_ro_bind"
    assert receipt["deny_read_enforcement"] == "owner_opt_out_unenforced"


def test_bubblewrap_argv_is_parsed_never_guessed(tmp_path, account_home):
    lay = _layout(tmp_path)
    for argv in (["worker"], ["/usr/bin/bwrap", "--unknown-flag", "--", _ADAPTER]):
        with pytest.raises(lrr.SandboxVisibilityError) as info:
            _bwrap_launch(lay, tmp_path, worker_argv=argv)
        assert info.value.code == "read_roots_unsupported_backend"
        assert "bubblewrap_argv_unrecognized" in str(info.value)


def _bwrap_usable(bwrap: str) -> bool:
    if not os.path.isfile(bwrap):
        return False
    probe = [
        bwrap, "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
        "--proc", "/proc", "--dev", "/dev", "--", "/usr/bin/true",
    ]
    try:
        return subprocess.run(probe, capture_output=True, timeout=30, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="bubblewrap is a Linux user-namespace sandbox",
)
def test_real_bubblewrap_child_reads_only_inside_the_allow_list(tmp_path, account_home):
    bwrap = os.environ.get("AIWORKHUB_BWRAP", "/usr/bin/bwrap")
    if not _bwrap_usable(bwrap):
        pytest.skip(f"no usable bubblewrap at {bwrap} on this host")
    lay = _layout(tmp_path)
    targets = [lay.card_file, lay.git_object, lay.host_data, lay.checkout_file, lay.account_file]
    # The python runtime is the argv's own bind (worker_workspace binds its
    # runtime itself); the read binds add only the card roots.
    runtime = sorted(
        {
            os.path.realpath(root)
            for root in (sys.prefix, sys.base_prefix, os.path.dirname(sys.executable))
        }
    )
    worker_argv = [
        bwrap, "--new-session", "--die-with-parent", "--unshare-pid",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--ro-bind", "/etc", "/etc",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        *(value for root in runtime for value in ("--ro-bind", root, root)),
        "--bind", str(lay.worktree), "/workspace",
        "--chdir", "/workspace",
        "--", sys.executable, "-I", "-c", _PROBE, *map(str, targets),
    ]
    argv, _receipt, _spec = _bwrap_launch(lay, tmp_path, worker_argv=worker_argv)
    result = subprocess.run(
        argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.split()
    # Only the card root is bound: the git object store is a Landlock-only grant.
    assert lines[:1] == ["read"], result.stdout
    assert all(line in {"denied:EACCES", "denied:ENOENT"} for line in lines[1:]), lines
    assert len(lines) == len(targets)


# --------------------------------------------------------------------------
# git: the object store, refs and this worktree's gitdir, never the common dir whole
# --------------------------------------------------------------------------


def test_git_grant_is_the_common_read_surface_never_hooks_or_logs(tmp_path, account_home):
    lay = _layout(tmp_path)
    common = lay.repo / ".git"
    config = _write(common / "config", "[core]\n")
    exclude = _write(common / "info" / "exclude", "*.tmp\n")
    hook = _write(common / "hooks" / "pre-commit", "#!/bin/sh\n")
    log = _write(common / "logs" / "HEAD", "log\n")
    ref = _write(common / "refs" / "heads" / "main", "0123\n")
    packed = _write(common / "packed-refs", "# pack-refs\n")
    head = _write(common / "HEAD", "ref: refs/heads/main\n")
    own = common / "worktrees" / "wt"
    roots = lrr._git_read_roots(os.path.realpath(lay.worktree))
    assert sorted(roots) == sorted(
        os.path.realpath(p)
        for p in (
            common / "objects", common / "refs", packed, head, config,
            common / "info", common / "worktrees", own,
        )
    )
    allow, _dropped = lrr.read_allow_set(
        worktree=lay.worktree,
        repo=lay.repo,
        read_roots=[str(lay.card)],
        worker_argv=_landlock_exec_argv(lay),
    )
    rules = [(root, FULL) for root in allow]
    for granted in (
        lay.git_object, ref, packed, head, config, exclude, own / "commondir", own / "HEAD"
    ):
        assert _granted(granted, rules), granted
    for refused in (hook, log, lay.checkout_file):
        assert not _granted(refused, rules), refused
    assert not _granted(common, rules, READ_DIR)


def test_linked_worktree_allow_set_contains_the_common_config(tmp_path, account_home):
    lay = _layout(tmp_path)
    common = lay.repo / ".git"
    for entry in ("config", "commondir", "shallow"):
        _write(common / entry, "x\n")
    (common / "info").mkdir()
    allow, _dropped = lrr.read_allow_set(
        worktree=lay.worktree,
        repo=lay.repo,
        read_roots=[str(lay.card)],
        worker_argv=_landlock_exec_argv(lay),
    )
    for entry in ("config", "info", "commondir", "shallow", "worktrees"):
        assert os.path.realpath(common / entry) in allow, entry
    assert os.path.realpath(common) not in allow


def _git(*args: str, env: dict[str, str]) -> None:
    subprocess.run(
        ["git", *args], env=env, stdin=subprocess.DEVNULL, capture_output=True,
        timeout=120, check=True,
    )


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Landlock is a Linux kernel feature; the read layer refuses off Linux",
)
def test_real_landlock_git_status_in_a_linked_worktree_succeeds(tmp_path, account_home):
    if _landlock_abi() < 1:
        pytest.skip("this Linux kernel exposes no Landlock ABI")
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed on this host")
    card = tmp_path / "card"
    card.mkdir()
    env = {
        **os.environ,
        "HOME": str(card),
        "XDG_CONFIG_HOME": str(card / ".config"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    repo = tmp_path / "repo"
    worktree = tmp_path / "wt"
    _git("init", "-q", str(repo), env=env)
    _write(repo / "mod.py", "print(1)\n")
    _git("-C", str(repo), "add", "mod.py", env=env)
    _git(
        "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
        "commit", "-q", "-m", "init", env=env,
    )
    _git("-C", str(repo), "worktree", "add", "-q", str(worktree), env=env)
    plan = tmp_path / "req.read-layer.json"
    argv, _receipt, _spec = lrr.prepare_launch_visibility(
        backend="landlock",
        worktree=worktree,
        repo=repo,
        read_roots=[str(card)],
        deny_read=[],
        plan_path=plan,
        worker_argv=[git, "-C", str(worktree), "status", "--porcelain"],
        write_plan=lambda path, doc: Path(path).write_text(json.dumps(doc), encoding="utf-8"),
    )
    rules = lrr.load_plan(plan)
    assert _granted(repo / ".git" / "config", rules)
    assert not _granted(repo / "mod.py", rules)
    result = subprocess.run(
        argv,
        cwd=worktree,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_git_gitdir_outside_the_common_worktrees_dir_is_refused(tmp_path):
    lay = _layout(tmp_path)
    stray = lay.repo / ".git" / "modules" / "x"
    _write(stray / "commondir", "../..\n")
    _write(lay.worktree / ".git", f"gitdir: {stray}\n")
    with pytest.raises(lrr.SandboxVisibilityError, match="^read_allowlist_refused:git_dir_unexpected"):
        lrr._git_read_roots(os.path.realpath(lay.worktree))


# --------------------------------------------------------------------------
# a PATH-relative adapter resolves; an unresolved one refuses
# --------------------------------------------------------------------------


def _fake_executable(directory: Path, name: str) -> Path:
    """An executable file created with its mode at open time (no chmod call)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (f"{name}.exe" if os.name == "nt" else name)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o755)
    try:
        os.write(fd, b"#!/bin/sh\n")
    finally:
        os.close(fd)
    return path


def test_path_relative_adapter_gets_its_install_root(tmp_path, account_home):
    version = tmp_path / ".nvm" / "versions" / "node" / "v22.1.0"
    _fake_executable(version / "bin", "agent")
    roots = lrr._toolchain_roots(["agent", "-p"], path=str(version / "bin"))
    assert roots[-1] == os.path.realpath(version)
    plain_bin = tmp_path / "plain-bin"
    plain = _fake_executable(plain_bin, "tool")
    assert lrr._toolchain_roots(["tool"], path=str(plain_bin))[-1] == os.path.realpath(plain)
    # Threaded from the launch env's PATH through read_allow_set.
    lay = _layout(tmp_path)
    argv = _landlock_exec_argv(lay)
    argv[argv.index("--") + 1] = "agent"
    allow, _dropped = lrr.read_allow_set(
        worktree=lay.worktree,
        repo=lay.repo,
        read_roots=[str(lay.card)],
        worker_argv=argv,
        launch_path=str(version / "bin"),
    )
    assert os.path.realpath(version) in allow


def test_unresolved_relative_adapter_is_refused(tmp_path):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    for argv in (["no-such-agent-nf01004"], [""]):
        with pytest.raises(lrr.SandboxVisibilityError) as info:
            lrr._toolchain_roots(argv, path=str(empty))
        assert info.value.code == "read_allowlist_refused:adapter_unresolved"


# --------------------------------------------------------------------------
# --landlock-exec request roots are parsed positionally and validated
# --------------------------------------------------------------------------


def _exec_argv(*flags: str) -> list[str]:
    return [sys.executable, "/pkg/aiworkhub/worker_workspace.py", "--landlock-exec", *flags]


def test_request_root_values_are_never_read_as_flags(tmp_path):
    workspace = os.fspath(tmp_path / "wt")
    home = os.fspath(tmp_path / "home")
    values, adapter = lrr._landlock_exec_parts(
        _exec_argv(
            "--workspace", workspace,
            "--allow", "--home",
            "--allow", "--worker-temp",
            "--noop-chmod",
            "--home", home,
            "--allow", "--",
            "--", "agent", "--home", "x",
        )
    )
    assert values == [workspace, home]
    assert adapter == ["agent", "--home", "x"]


@pytest.mark.parametrize(
    ("flags", "code"),
    [
        (("--home", "rel/home", "--"), "read_allowlist_refused:request_root_invalid"),
        (("--worker-temp", "", "--"), "read_allowlist_refused:request_root_invalid"),
        (("--bogus", "x", "--"), "read_allowlist_refused:landlock_argv_unrecognized"),
        (("--allow", "--"), "read_allowlist_refused:landlock_argv_unterminated"),
        (("--home",), "read_allowlist_refused:landlock_argv_unterminated"),
    ],
)
def test_invalid_request_roots_are_refused(flags, code):
    with pytest.raises(lrr.SandboxVisibilityError) as info:
        lrr._landlock_exec_parts(_exec_argv(*flags))
    assert info.value.code == code
