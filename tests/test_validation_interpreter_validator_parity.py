"""NF-2026-00452 / NF-2026-00582: a declared validator resolves through the SAME
trusted runtime root whether it is invoked bare (``mypy src``) or through
``<python> -m mypy``.

Measured 2026-09-02: the interpreter that starts the MCP server (an absolute
``/usr/bin/python3.12``) carries ruff but not mypy, while the repository ``.venv``
carries mypy. ``.aiworkhub/quality.json`` declares typed-kernel as ``{python} -m
mypy``. Before the fix a ``<python> -m mypy`` command whose head was that absolute
interpreter was passed through unchanged, so it inherited whatever modules that
interpreter happened to have and failed ``validation_environment_blocked:
missing_package`` -- six attempts, zero accepts -- even though the bare ``mypy``
form resolved safely. These tests pin the asymmetry closed: the ``-m`` form runs
the trusted root's OWN python with the module form (not the validator console
script, so the root's site-packages supply the validator and a root whose module
imports without a console script is still honoured), while NF-2026-00448 (a bare
python head is never resolved through a PATH search) stays intact.

Two properties the resolver must hold, each pinned below:

* It EXECUTES the venv's own ``bin/python`` -- the unresolved symlink that
  activates the venv -- while deciding SECURITY on the resolved target it points
  at. Resolving the symlink and running the base interpreter throws the venv away
  and makes the declared validator unimportable, the exact NF-2026-00582 defect.
* Every command it builds carries ``-P`` immediately after the interpreter so the
  ``-m`` form cannot import a candidate-authored ``mypy.py``/``ruff.py`` from the
  validation cwd (the candidate's own worktree). ``-P`` closes ONLY that cwd half:
  it does not ignore ``PYTHONPATH``, a supported validation env assignment whose
  components may be candidate-writable, so the run additionally prepends the trusted
  root's own ``site-packages`` ahead of every declared ``PYTHONPATH`` component --
  for the bare console script and the ``-m`` form alike -- so ``import <validator>``
  binds the trusted copy (NF-2026-00586 finding one). When NO trusted root supplies
  the validator the ``-m`` form falls back to ``sys.executable`` and there is no
  trusted ``site-packages`` to shadow with, so the candidate ``PYTHONPATH`` is
  SUPPRESSED instead -- a candidate ``mypy.py`` on ``.``/``src`` must never be
  importable by the fallback command (NF-2026-00586 finding one, rework HIGH).
* The module-presence probe proves an interpreter POSITIVELY: a ``bin/python`` that
  merely exits 0 and prints nothing is not a usable Python and never counts as
  supplying the validator, while a probe that cannot RUN still fails open toward
  keeping the root (NF-2026-00586 finding two).

The module-level skip is deliberately NARROW: only tests that build a POSIX venv
layout (``bin/python``, symlinks, mode bits, ``os.getuid``) are skipped on Windows.
The platform-agnostic assertions -- notably that the Windows ``python.exe`` spelling
is recognised as the ``-m`` module form -- run natively on every platform, so the
Windows-specific behaviour keeps native coverage (rework LOW).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import validation_runner, worker_workspace

# Narrow, per-test skip: applied ONLY to tests that construct a POSIX venv layout
# (``bin/python``, symlinks, mode bits, ``os.getuid``). Cross-platform assertions
# stay undecorated so they run on Windows too (rework LOW).
posix_layout = pytest.mark.skipif(
    os.name == "nt", reason="POSIX venv layout (bin/python, symlinks, mode bits)"
)


def _runtime_executable(root: Path, name: str) -> Path:
    path = root / "bin" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    return path


def _pin_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(
        worker_workspace,
        "_trusted_validation_runtime_roots",
        lambda repo=None: (root,),
    )


def _pin_roots(monkeypatch: pytest.MonkeyPatch, *roots: Path) -> None:
    monkeypatch.setattr(
        worker_workspace,
        "_trusted_validation_runtime_roots",
        lambda repo=None: tuple(roots),
    )


def _force_module_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the no-console-script module probe report the validator importable.

    The tests below that reach the probe pin OTHER properties -- symlink
    execution, ``-P``, owner/mode refusals -- not whether this host's stub or base
    interpreter actually carries ``mypy``. Forcing the probe positive keeps them
    deterministic on any host; the real subprocess probe and probe-driven root
    selection are exercised directly by their own tests below."""
    monkeypatch.setattr(
        worker_workspace,
        "_trusted_root_supplies_validator_module",
        lambda interpreter, module: True,
    )


def _real_root_owned_base_interpreter() -> Path | None:
    """A genuinely root-owned base interpreter on this host, or ``None``.

    The sandbox denies ``chown``, so a root-owned target cannot be fabricated;
    the venv/system base interpreter (``/usr/bin/python3.12`` here) is the one
    that exists and is root-owned, so the owner-boundary tests below symlink to
    the REAL file rather than a stub.
    """
    base = getattr(sys, "_base_executable", None) or sys.executable
    try:
        resolved = Path(base).resolve(strict=True)
        info = resolved.stat()
    except OSError:
        return None
    if (
        info.st_uid == 0
        and os.access(resolved, os.X_OK)
        and not (stat.S_IMODE(info.st_mode) & 0o002)
    ):
        return resolved
    return None


@posix_layout
def test_module_and_bare_mypy_resolve_to_one_trusted_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC1/AC2: the reproduction. A bare ``mypy`` and an absolute ``<python> -m
    mypy`` resolve to the SAME trusted runtime root; before the fix the ``-m``
    form was passed through unchanged (the asymmetry this proves is closed). The
    ``-m`` form runs that root's OWN python with the module form -- not the
    ``mypy`` console script -- so the root's site-packages supply the validator,
    with ``-P`` in front so the cwd cannot inject the module."""
    root = tmp_path / ".venv"
    mypy = _runtime_executable(root, "mypy")
    python = _runtime_executable(root, "python")
    _pin_root(monkeypatch, root)
    repo = tmp_path

    bare, bare_roots = (
        worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            ["mypy", "src"], repo
        )
    )
    assert bare == [str(mypy.resolve()), "src"]

    abs_python = "/usr/bin/python3.12"
    declared = [abs_python, "-m", "mypy", "src"]
    module, module_roots = (
        worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            list(declared), repo
        )
    )
    # Pre-fix this returned ``declared`` unchanged, running the untrusted MCP
    # interpreter; now it resolves to the bare form's own trusted root's python,
    # keeping the ``-m`` module form (behind ``-P``) so the import probe still
    # decides structurally.
    assert module != declared
    assert module == [str(python.resolve()), "-P", "-m", "mypy", "src"]
    assert module[0] != abs_python
    assert module_roots == bare_roots == (root.resolve(strict=False),)


@posix_layout
def test_module_and_bare_ruff_agree_for_bare_and_path_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC2: parity holds for every declared validator and every python spelling
    (bare ``python3`` and a repo-relative ``.venv/bin/python3`` alike) -- the head
    spelling never changes which runtime root's python supplies the ``-m`` form."""
    root = tmp_path / ".venv"
    _runtime_executable(root, "ruff")
    python = _runtime_executable(root, "python")
    _pin_root(monkeypatch, root)

    expected = (
        [str(python.resolve()), "-P", "-m", "ruff", "check", "src"],
        (root.resolve(strict=False),),
    )
    for head in ("python3", ".venv/bin/python3", "/opt/py/bin/python3.11"):
        assert (
            worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
                [head, "-m", "ruff", "check", "src"], tmp_path
            )
            == expected
        )


@posix_layout
def test_module_without_console_script_still_uses_root_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A trusted root whose validator MODULE is importable but whose console
    SCRIPT is absent (a missing entry point, a ``--no-scripts`` wheel) still
    resolves to that root's OWN python with the ``-m`` form -- never the
    ``sys.executable`` fallback that reproduced NF-2026-00452. With no console
    script the root is walked directly, so the returned interpreter is the
    unresolved ``bin/python`` under that root."""
    root = tmp_path / ".venv"
    python = _runtime_executable(root, "python")  # interpreter present...
    # ...but deliberately NO ``bin/mypy`` console script.
    _pin_root(monkeypatch, root)
    _force_module_present(monkeypatch)  # the sole root supplies the module

    tokens, roots, authority = worker_workspace._resolve_module_validator_argv(
        ["/usr/bin/python3.12", "-m", "mypy", "src"], tmp_path
    )
    assert tokens == [str(python), "-P", "-m", "mypy", "src"]
    assert tokens[0] != sys.executable
    assert roots == (root.resolve(strict=False),)
    assert authority is not None
    assert authority["source"] == "module_validator_trusted_runtime_root_module_present"
    assert authority["execution_path"] == str(python)


@posix_layout
def test_module_form_executes_venv_symlink_not_resolved_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FINDING 1 (corrected): the ``-m`` form must EXECUTE the venv's own
    ``bin/python`` -- the unresolved symlink that activates the venv and puts its
    site-packages on ``sys.path`` -- NOT the resolved base interpreter it points
    at. Running the base python would discard the venv and make the declared
    validator unimportable (NF-2026-00582). Security is still decided by the
    RESOLVED target's owner and mode (checked); only what is executed is the
    symlink, so target containment is deliberately not re-applied."""
    root = tmp_path / ".venv"
    (root / "bin").mkdir(parents=True)
    base_dir = tmp_path / "base" / "bin"
    base_dir.mkdir(parents=True)
    base_python = base_dir / "python3.12"  # a root-owned base python lives here IRL
    fd = os.open(base_python, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
    os.close(fd)
    venv_python = root / "bin" / "python"
    os.symlink(base_python, venv_python)  # the venv activates via THIS path
    _pin_root(monkeypatch, root)
    _force_module_present(monkeypatch)  # isolate: this test pins symlink execution

    tokens, roots, authority = worker_workspace._resolve_module_validator_argv(
        ["/usr/bin/python3.12", "-m", "mypy", "src"], tmp_path
    )
    assert tokens == [str(venv_python), "-P", "-m", "mypy", "src"]
    # The venv symlink is executed; its resolved base target is NOT.
    assert tokens[0] == str(venv_python)
    assert tokens[0] != str(venv_python.resolve())
    assert tokens[0] != str(base_python)
    assert authority is not None
    assert authority["execution_path"] == str(venv_python)
    assert roots == (root.resolve(strict=False),)


@posix_layout
def test_dash_p_blocks_cwd_validator_module_hijack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FINDING 2: ``-m`` puts the cwd at the front of ``sys.path`` and validation
    runs from the candidate's own worktree, so a candidate that writes ``mypy.py``
    beside its code could otherwise have it imported and executed by the validation
    command with host rights. The resolver inserts ``-P`` right after the
    interpreter, so the cwd module is NOT imported; stripping ``-P`` proves the
    same file WOULD be (the control), so the assertion cannot silently rot."""
    root = tmp_path / ".venv"
    (root / "bin").mkdir(parents=True)
    venv_python = root / "bin" / "python"
    # Use a contained, non-world-writable real interpreter. GitHub's Python 3.14
    # toolcache executable can itself be mode 0777, which the production trust
    # check correctly refuses; a symlink to it would make this -P regression test
    # accidentally test hosted-runner permissions instead.
    shutil.copyfile(sys.executable, venv_python)
    venv_python.chmod(0o755)
    _pin_root(monkeypatch, root)
    _force_module_present(monkeypatch)  # isolate: this test pins the -P behaviour

    workdir = tmp_path / "worktree"
    workdir.mkdir()
    (workdir / "mypy.py").write_text(
        "import pathlib\n"
        "pathlib.Path('HIJACKED').write_text('x', encoding='utf-8')\n",
        encoding="utf-8",
    )

    tokens, _roots, _authority = worker_workspace._resolve_module_validator_argv(
        ["/usr/bin/python3.12", "-m", "mypy", "src"], tmp_path
    )
    assert tokens[0] == str(venv_python)
    assert tokens[1] == "-P"  # immediately after the interpreter

    # With ``-P`` the cwd ``mypy.py`` is never imported.
    subprocess.run(
        tokens,
        cwd=workdir,
        timeout=60,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert not (workdir / "HIJACKED").exists()

    # Control: strip ``-P`` and the same cwd file IS imported and executed.
    hijack_argv = [tokens[0], *tokens[2:]]
    subprocess.run(
        hijack_argv,
        cwd=workdir,
        timeout=60,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert (workdir / "HIJACKED").exists()


@posix_layout
def test_bad_root_is_refused_identically_for_both_forms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC3: a world-writable runtime root is refused for the ``-m`` form exactly
    as for the bare form -- never silently accepted."""
    monkeypatch.setattr(
        worker_workspace, "posix_path_modes_supported", lambda _platform=None: True
    )
    root = tmp_path / ".venv"
    _runtime_executable(root, "mypy")
    _runtime_executable(root, "python")
    _pin_root(monkeypatch, root)
    monkeypatch.setattr(worker_workspace.stat, "S_IMODE", lambda mode: 0o002)

    for declared in (["mypy", "src"], ["/usr/bin/python3", "-m", "mypy", "src"]):
        with pytest.raises(
            worker_workspace.WorkspaceError, match="world_writable"
        ):
            worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
                list(declared), tmp_path
            )


@posix_layout
def test_module_without_console_script_still_refuses_bad_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC3 boundary: the interpreter route that survives an absent console script
    still applies the world-writable refusal -- a bad root is never downgraded to
    ``sys.executable`` just because the console script happened to be missing."""
    monkeypatch.setattr(
        worker_workspace, "posix_path_modes_supported", lambda _platform=None: True
    )
    root = tmp_path / ".venv"
    _runtime_executable(root, "python")  # interpreter present, no console script
    _pin_root(monkeypatch, root)
    monkeypatch.setattr(worker_workspace.stat, "S_IMODE", lambda mode: 0o002)

    with pytest.raises(worker_workspace.WorkspaceError, match="world_writable"):
        worker_workspace._resolve_module_validator_argv(
            ["/usr/bin/python3.12", "-m", "mypy", "src"], tmp_path
        )


def test_no_trusted_root_falls_back_to_sys_executable_with_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC4: no trusted root supplies the validator -> resolve to ``sys.executable``
    (never a PATH search), keeping the ``-m`` form behind ``-P``, and name the
    fallback and its reason in the interpreter authority receipt."""
    _pin_root(monkeypatch, tmp_path / "missing-venv")

    tokens, roots, authority = worker_workspace._resolve_module_validator_argv(
        ["python3", "-m", "mypy", "src"], tmp_path
    )
    assert tokens == [sys.executable, "-P", "-m", "mypy", "src"]
    assert roots == ()
    assert authority is not None
    assert authority["execution_path"] == sys.executable
    assert "no_trusted_root" in authority["source"]
    assert "validation_executable_unavailable:mypy" in authority["source"]


def test_no_trusted_root_fallback_suppresses_candidate_pythonpath_hijack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REWORK HIGH: the fallback carries NO ``validation_executable_roots``, so
    ``run_validations`` cannot shadow the candidate ``PYTHONPATH`` with a trusted
    root's site-packages. It must instead SUPPRESS the candidate PYTHONPATH --
    ``_module_validator_fallback_authority`` is the signal it keys on -- otherwise a
    candidate ``mypy.py``/``ruff.py`` on a declared ``.``/``src`` component would be
    imported by ``sys.executable -P -m mypy`` and false-green the gate (``-P`` drops
    only the cwd, not PYTHONPATH). The subprocess control proves the danger the
    suppression closes so the assertion cannot silently rot."""
    _pin_root(monkeypatch, tmp_path / "missing-venv")

    tokens, roots, authority = worker_workspace._resolve_module_validator_argv(
        ["python3", "-m", "mypy", "src"], tmp_path
    )
    assert tokens == [sys.executable, "-P", "-m", "mypy", "src"]
    assert roots == ()
    # The receipt is recognised as the no-trusted-root fallback -- the exact signal
    # ``run_validations`` uses to suppress candidate PYTHONPATH (never as a
    # trusted-root receipt, which would instead carry a shadow).
    assert worker_workspace._module_validator_fallback_authority(authority)
    assert not worker_workspace._module_validator_fallback_authority(
        {"source": "module_validator_trusted_runtime_root_module_present"}
    )
    assert not worker_workspace._module_validator_fallback_authority(None)

    workdir = tmp_path / "worktree"
    component = workdir / "src"
    component.mkdir(parents=True)
    (component / "mypy.py").write_text(
        "import pathlib\n"
        "pathlib.Path('HIJACKED').write_text('x', encoding='utf-8')\n",
        encoding="utf-8",
    )

    # Control: the candidate component ON ``PYTHONPATH`` (the pre-suppression run)
    # IS imported and executed by the fallback command -- ``-P`` does not stop it.
    control = {**os.environ, "PYTHONPATH": str(component)}
    subprocess.run(
        tokens,
        cwd=workdir,
        env=control,
        timeout=60,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert (workdir / "HIJACKED").exists()
    (workdir / "HIJACKED").unlink()

    # Suppressed: with the candidate PYTHONPATH dropped (what ``run_validations``
    # does for this fallback receipt), ``import mypy`` can bind ONLY the trusted
    # running interpreter's own site-packages, so the candidate file is unreachable.
    suppressed = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    subprocess.run(
        tokens,
        cwd=workdir,
        env=suppressed,
        timeout=60,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert not (workdir / "HIJACKED").exists()


@posix_layout
def test_bad_root_never_downgrades_to_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC3/AC4 boundary: an untrusted-owner root is refused, not turned into the
    ``sys.executable`` fallback (only a genuinely absent validator falls back)."""
    root = tmp_path / ".venv"
    _runtime_executable(root, "mypy")
    _runtime_executable(root, "python")
    _pin_root(monkeypatch, root)
    foreign_uid = os.getuid() + 12345
    monkeypatch.setattr(worker_workspace.os, "getuid", lambda: foreign_uid)

    with pytest.raises(
        worker_workspace.WorkspaceError, match="untrusted_owner"
    ):
        worker_workspace._resolve_module_validator_argv(
            ["python3", "-m", "mypy", "src"], tmp_path
        )


def test_bare_python_non_validator_module_still_uses_sys_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC5: NF-2026-00448 is intact -- a bare python head with a non-validator
    ``-m`` target still resolves to ``sys.executable`` (no PATH search, and this
    non-validator path is untouched -- no ``-P`` rewriting), and an absolute
    non-``-m-validator`` interpreter declaration is untouched."""
    _pin_root(monkeypatch, tmp_path / ".venv")

    assert (
        worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            ["python", "-m", "compileall", "src"], tmp_path
        )
        == ([sys.executable, "-m", "compileall", "src"], ())
    )
    assert (
        worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            ["/usr/bin/python3", "-c", "pass"], tmp_path
        )
        == (["/usr/bin/python3", "-c", "pass"], ())
    )


@posix_layout
def test_module_symlink_to_world_writable_target_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FINDING 1 boundary: the ``-m`` interpreter route resolves the SYMLINK
    TARGET and gates its mode. A trusted root whose ``bin/python`` symlinks to a
    world-writable python OUTSIDE the root -- which the pre-fix code returned
    unchecked because it examined only the symlink's parent -- is refused, never
    executed. Target containment stays skipped (a venv legitimately points at a
    base python outside the root); only the target's owner and mode are re-checked."""
    monkeypatch.setattr(
        worker_workspace, "posix_path_modes_supported", lambda _platform=None: True
    )
    # World-writable applies to the resolved FILE only, so the root directory
    # still passes and the refusal can only come from the resolved target.
    real_s_imode = stat.S_IMODE
    monkeypatch.setattr(
        worker_workspace.stat,
        "S_IMODE",
        lambda mode: 0o002 if stat.S_ISREG(mode) else real_s_imode(mode),
    )
    root = tmp_path / ".venv"
    (root / "bin").mkdir(parents=True)
    outside = tmp_path / "base-python"  # a root-owned base python lives here IRL
    fd = os.open(outside, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
    os.close(fd)
    os.symlink(outside, root / "bin" / "python")  # ...but no console script
    _pin_root(monkeypatch, root)

    with pytest.raises(worker_workspace.WorkspaceError, match="world_writable"):
        worker_workspace._resolve_module_validator_argv(
            ["/usr/bin/python3.12", "-m", "mypy", "src"], tmp_path
        )


def test_windows_python_exe_is_recognised_as_module_form() -> None:
    """REWORK LOW: the Windows ``python.exe`` spelling is recognised as the ``-m``
    module form (the ``.exe`` suffix is accepted case-insensitively) exactly as the
    POSIX ``python`` spelling is. This assertion is platform-agnostic, so it keeps
    native coverage on Windows where the layout tests below cannot run -- the
    Windows-specific recognition is precisely the behaviour NF-2026-00582 is about,
    and a module-wide skip would have silently dropped it."""
    assert worker_workspace._is_module_validator_invocation(
        ["python.exe", "-m", "mypy", "src"]
    )
    assert worker_workspace._is_module_validator_invocation(
        ["python.EXE", "-m", "ruff", "check", "src"]
    )
    assert worker_workspace._is_module_validator_invocation(
        ["python", "-m", "mypy", "src"]
    )
    # A stray ``.exe`` on a non-python stem, and a non-``-m`` head, still never match.
    assert not worker_workspace._is_module_validator_invocation(
        ["notpython.exe", "-m", "mypy", "src"]
    )
    assert not worker_workspace._is_module_validator_invocation(
        ["python.exe", "-c", "pass"]
    )


@posix_layout
def test_windows_python_exe_module_form_resolves_through_trusted_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FINDING 2 (predecessor): the Windows spelling ``python.exe -m mypy``
    resolves through the SAME trusted runtime root as the POSIX ``python -m mypy``
    -- before the fix it fell through to the untrusted head, which is exactly the
    platform NF-2026-00582 is about. (The pure ``.exe`` RECOGNITION is proven
    cross-platform in ``test_windows_python_exe_is_recognised_as_module_form``;
    this test pins the resolution and so needs the POSIX ``bin/`` layout.)"""
    root = tmp_path / ".venv"
    _runtime_executable(root, "mypy")  # the root definitively carries the validator
    python = _runtime_executable(root, "python")
    _pin_root(monkeypatch, root)

    expected = (
        [str(python), "-P", "-m", "mypy", "src"],
        (root.resolve(strict=False),),
    )
    windows = worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
        ["python.exe", "-m", "mypy", "src"], tmp_path
    )
    posix = worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
        ["python", "-m", "mypy", "src"], tmp_path
    )
    assert windows == expected == posix


@posix_layout
def test_root_owned_target_outside_system_prefix_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC3 (residual finding): ``st_uid == 0`` alone no longer accepts an arbitrary
    root-owned executable. A trusted root whose ``bin/python`` symlinks to a
    root-owned target that lies OUTSIDE every system prefix is refused for the
    ``-m`` form, exactly as the bare form refuses any target it does not own inside
    the trusted root -- the two owner refusals now behave identically."""
    base = _real_root_owned_base_interpreter()
    if base is None:
        pytest.skip("no root-owned base interpreter available on this host")
    root = tmp_path / ".venv"
    (root / "bin").mkdir(parents=True)
    os.symlink(base, root / "bin" / "python")  # root-owned resolved target...
    _pin_root(monkeypatch, root)
    # ...but no system prefix contains it: no ``pyvenv.cfg`` records a base, and
    # ``sys.base_prefix`` is redirected to an unrelated tree.
    monkeypatch.setattr(
        worker_workspace.sys, "base_prefix", str(tmp_path / "fake-base")
    )

    with pytest.raises(worker_workspace.WorkspaceError, match="untrusted_owner"):
        worker_workspace._resolve_module_validator_argv(
            ["/usr/bin/python3.12", "-m", "mypy", "src"], tmp_path
        )


@posix_layout
def test_root_owned_base_interpreter_in_declared_prefix_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC3 companion: the SAME root-owned base interpreter STILL resolves when a
    system prefix contains it -- here the base the root's own ``pyvenv.cfg`` records
    -- so a legitimate venv layout is honoured while the arbitrary case above is
    refused. ``sys.base_prefix`` is redirected away so the ``pyvenv.cfg`` base is
    demonstrably what admits the target, and the executed argv is still the venv's
    unresolved ``bin/python`` symlink."""
    base = _real_root_owned_base_interpreter()
    if base is None:
        pytest.skip("no root-owned base interpreter available on this host")
    root = tmp_path / ".venv"
    (root / "bin").mkdir(parents=True)
    (root / "pyvenv.cfg").write_text(f"home = {base.parent}\n", encoding="utf-8")
    venv_python = root / "bin" / "python"
    os.symlink(base, venv_python)
    _pin_root(monkeypatch, root)
    _force_module_present(monkeypatch)  # isolate: this test pins the prefix boundary
    monkeypatch.setattr(
        worker_workspace.sys, "base_prefix", str(tmp_path / "fake-base")
    )

    tokens, roots, authority = worker_workspace._resolve_module_validator_argv(
        ["/usr/bin/python3.12", "-m", "mypy", "src"], tmp_path
    )
    assert tokens == [str(venv_python), "-P", "-m", "mypy", "src"]
    assert roots == (root.resolve(strict=False),)
    assert authority is not None
    assert authority["execution_path"] == str(venv_python)


@posix_layout
def test_module_selection_prefers_root_that_supplies_the_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC4 (residual finding): when no trusted root carries the console script, a
    root with an interpreter but WITHOUT the module must lose to a later root that
    has both. Two trusted roots are walked in order; the first supplies a python
    but not ``mypy``, the second supplies both. Resolution must choose the SECOND
    -- having a python is not the same as having the module -- and record which
    root supplied it in the receipt."""
    first = tmp_path / "first" / ".venv"
    second = tmp_path / "second" / ".venv"
    first_python = _runtime_executable(first, "python")  # python, but no module
    second_python = _runtime_executable(second, "python")  # python AND the module
    # Neither root carries the ``mypy`` console script, so the console-script
    # lookup finds nothing and the module-probe selection decides.
    _pin_roots(monkeypatch, first, second)
    monkeypatch.setattr(
        worker_workspace,
        "_trusted_root_supplies_validator_module",
        lambda interpreter, module: str(interpreter) == str(second_python),
    )

    tokens, roots, authority = worker_workspace._resolve_module_validator_argv(
        ["/usr/bin/python3.12", "-m", "mypy", "src"], tmp_path
    )
    assert tokens == [str(second_python), "-P", "-m", "mypy", "src"]
    assert tokens[0] != str(first_python)
    assert tokens[0] != sys.executable
    assert roots == (second.resolve(strict=False),)
    assert authority is not None
    assert authority["source"] == "module_validator_trusted_runtime_root_module_present"
    assert authority["execution_path"] == str(second_python)
    assert authority["resolved"] == str(second.resolve(strict=False))


@posix_layout
def test_module_selection_falls_back_when_no_root_supplies_the_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC4 companion: when trusted roots supply an interpreter but NONE supplies
    the module, the command still falls back to ``sys.executable`` (never a PATH
    search), keeping the ``-m`` form behind ``-P``, and the receipt records that
    the module was importable in none of the trusted roots rather than leaving the
    fallback implicit."""
    first = tmp_path / "first" / ".venv"
    second = tmp_path / "second" / ".venv"
    _runtime_executable(first, "python")
    _runtime_executable(second, "python")
    _pin_roots(monkeypatch, first, second)
    monkeypatch.setattr(
        worker_workspace,
        "_trusted_root_supplies_validator_module",
        lambda interpreter, module: False,
    )

    tokens, roots, authority = worker_workspace._resolve_module_validator_argv(
        ["/usr/bin/python3.12", "-m", "mypy", "src"], tmp_path
    )
    assert tokens == [sys.executable, "-P", "-m", "mypy", "src"]
    assert roots == ()
    assert authority is not None
    assert authority["execution_path"] == sys.executable
    assert "no_trusted_root" in authority["source"]
    assert "module_absent_in_all_trusted_roots:mypy" in authority["source"]


def test_module_probe_detects_present_and_absent_modules(tmp_path: Path) -> None:
    """The real subprocess probe (not a monkeypatch) is what drives selection:
    against a genuine interpreter it reports a present module importable and an
    absent one missing, so the selection above rests on a real ``find_spec``
    result -- while a probe that cannot run fails OPEN and never mis-reports a
    present validator as missing."""
    real_python = Path(sys.executable)
    assert worker_workspace._trusted_root_supplies_validator_module(
        real_python, "json"
    )
    assert not worker_workspace._trusted_root_supplies_validator_module(
        real_python, "aiworkhub_nonexistent_validator_module_zzz"
    )
    # A probe that cannot even launch never downgrades the root (fail-open).
    assert worker_workspace._trusted_root_supplies_validator_module(
        tmp_path / "does-not-exist" / "python", "aiworkhub_nonexistent_validator_module_zzz"
    )


def test_probe_and_row_restriction_invariants_intact() -> None:
    """AC6: the import probe still targets the interpreter that ran the command,
    and ``row_restriction`` still keys ``missing_package`` on the structural
    field alone -- never on candidate-controlled output text. ``-P`` sits before
    ``-m`` so the probe still finds the interpreter head and the module."""
    # The fallback keeps the ``-m`` form so the probe imports in the exact
    # interpreter the command runs (``sys.executable`` here); ``-P`` before ``-m``
    # does not change which interpreter or which module the probe reads.
    assert (
        worker_workspace._validator_probe_interpreter(
            [sys.executable, "-P", "-m", "mypy"]
        )
        == sys.executable
    )
    assert validation_runner.dash_m_validator_modules(
        [sys.executable, "-P", "-m", "mypy"]
    ) == ("mypy",)
    # Candidate output text never establishes a restriction...
    assert (
        validation_runner.row_restriction(
            {"stderr_tail": "No module named mypy", "returncode": 1}
        )
        is None
    )
    # ...only the runner-authored structural signals do.
    assert (
        validation_runner.row_restriction({"launch_error": "FileNotFoundError"})
        == validation_runner.RESTRICTION_ABSENT_INTERPRETER
    )


@posix_layout
def test_trusted_site_packages_shadows_pythonpath_validator_hijack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FINDING ONE (corrected): ``-P`` drops only the implicit cwd entry, NOT
    ``PYTHONPATH`` -- a supported validation env assignment whose components can be
    candidate-writable (``.``/``src``/a declared relative dir). A candidate that
    writes ``mypy.py`` onto such a component would otherwise have it imported by
    ``python -P -m mypy``, because ``PYTHONPATH`` precedes the interpreter's own
    site-packages on ``sys.path``. Prepending the trusted root's own
    ``site-packages`` (``_trusted_validator_pythonpath_prefix``) makes the trusted
    copy win the import; stripping the prefix proves the same file WOULD be executed
    (the control), so the assertion cannot silently rot."""
    root = tmp_path / ".venv"
    (root / "bin").mkdir(parents=True)
    venv_python = root / "bin" / "python"
    shutil.copyfile(sys.executable, venv_python)  # a REAL interpreter to import with
    venv_python.chmod(0o755)
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site_packages = root / "lib" / version / "site-packages"
    site_packages.mkdir(parents=True)
    # The trusted validator ``import mypy`` must bind: benign, ignores argv.
    (site_packages / "mypy.py").write_text("print('trusted-mypy')\n", encoding="utf-8")
    _pin_root(monkeypatch, root)
    _force_module_present(monkeypatch)  # sole root supplies the module, no console script

    tokens, roots, _authority = worker_workspace._resolve_module_validator_argv(
        ["/usr/bin/python3.12", "-m", "mypy", "src"], tmp_path
    )
    assert tokens[0] == str(venv_python)
    assert tokens[1:] == ["-P", "-m", "mypy", "src"]

    # The landlock/in-process spelling is the real host path; it points inside the
    # trusted root that already passed owner/mode/containment checks (no new trust).
    prefix = worker_workspace._trusted_validator_pythonpath_prefix(roots[0], "landlock")
    assert len(prefix) == 1
    assert prefix[0].endswith(f"{version}/site-packages")
    assert Path(prefix[0]).is_dir()
    # The bubblewrap spelling addresses the same dir through the executable-root bind.
    bwrap_prefix = worker_workspace._trusted_validator_pythonpath_prefix(
        roots[0], "bubblewrap"
    )
    assert bwrap_prefix == (
        f"{worker_workspace.SANDBOX_VALIDATION_EXECUTABLE_ROOT}/0/lib/{version}/site-packages",
    )

    workdir = tmp_path / "worktree"
    (workdir / "candidate").mkdir(parents=True)
    (workdir / "candidate" / "mypy.py").write_text(
        "import pathlib\n"
        "pathlib.Path('HIJACKED').write_text('x', encoding='utf-8')\n",
        encoding="utf-8",
    )
    candidate_component = str(workdir / "candidate")

    # Trusted site-packages prepended ahead of the candidate component: the trusted
    # ``mypy`` wins and the candidate file is never imported.
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((*prefix, candidate_component))}
    subprocess.run(
        tokens,
        cwd=workdir,
        env=env,
        timeout=60,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert not (workdir / "HIJACKED").exists()

    # Control: the candidate component alone (the pre-fix PYTHONPATH) imports and
    # executes the candidate ``mypy.py`` -- ``-P`` does not stop it.
    control = {**os.environ, "PYTHONPATH": candidate_component}
    subprocess.run(
        tokens,
        cwd=workdir,
        env=control,
        timeout=60,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert (workdir / "HIJACKED").exists()


@posix_layout
def test_trusted_runtime_site_packages_discovers_venv_layout(tmp_path: Path) -> None:
    """``_trusted_runtime_site_packages`` returns the venv's own site-packages and
    is empty for a root that exposes none, so the shadow prefix is derived from a
    real directory rather than a guessed path."""
    root = tmp_path / ".venv"
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site_packages = root / "lib" / version / "site-packages"
    site_packages.mkdir(parents=True)
    assert worker_workspace._trusted_runtime_site_packages(root) == (site_packages,)
    assert worker_workspace._trusted_runtime_site_packages(tmp_path / "empty") == ()


@posix_layout
def test_trusted_runtime_site_packages_refuses_escaping_symlink(tmp_path: Path) -> None:
    """REWORK MEDIUM: ``_trusted_runtime_site_packages`` is PREPENDED ahead of every
    candidate ``PYTHONPATH`` component, so a ``site-packages`` that is a symlink
    ESCAPING the trusted root would hand a target outside the owner/mode/containment
    checked root top import precedence -- re-opening the very hijack the shadow is
    meant to close. Such an entry must be refused (excluded), while a genuinely
    in-root directory (including an in-root ``lib64`` -> ``lib`` symlink) survives."""
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"

    # An escaping ``site-packages`` symlink is excluded, so the prefix is empty and
    # nothing candidate-writable is ever prepended.
    escaping = tmp_path / "escaping" / ".venv"
    (escaping / "lib" / version).mkdir(parents=True)
    outside = tmp_path / "outside-site-packages"
    outside.mkdir()
    os.symlink(outside, escaping / "lib" / version / "site-packages")
    assert worker_workspace._trusted_runtime_site_packages(escaping) == ()
    assert worker_workspace._trusted_validator_pythonpath_prefix(escaping, "landlock") == ()

    # A real in-root site-packages IS still returned (no over-refusal).
    contained = tmp_path / "contained" / ".venv"
    sp = contained / "lib" / version / "site-packages"
    sp.mkdir(parents=True)
    assert worker_workspace._trusted_runtime_site_packages(contained) == (sp,)

    # An in-root ``lib64`` -> ``lib`` symlink resolves INSIDE the root and survives
    # (deduplicated against the ``lib`` entry it resolves to).
    os.symlink(contained / "lib", contained / "lib64")
    assert worker_workspace._trusted_runtime_site_packages(contained) == (sp,)


@posix_layout
def test_module_probe_rejects_non_python_interpreter_stub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FINDING TWO: an executable at ``bin/python`` that merely exits 0 and prints
    nothing is not a usable Python. The probe proves presence POSITIVELY via an
    interpreter marker, so the stub is reported as NOT supplying the module (never
    as "present"), and resolution falls back to ``sys.executable`` rather than
    trusting the stub -- while a probe that cannot RUN still fails open."""
    root = tmp_path / ".venv"
    stub = _runtime_executable(root, "python")  # #!/bin/sh\nexit 0\n -- runs, prints nothing
    # Ran but emitted no marker: not usable -> does not supply the module.
    assert not worker_workspace._trusted_root_supplies_validator_module(stub, "mypy")
    # A probe that cannot even launch still fails open (the root is kept).
    assert worker_workspace._trusted_root_supplies_validator_module(
        tmp_path / "absent" / "python", "mypy"
    )
    # End to end: the stub is the sole trusted root and carries no console script,
    # so resolution falls back to sys.executable with an auditable reason -- the
    # non-python stub is never allowed to back the gate.
    _pin_root(monkeypatch, root)
    tokens, roots, authority = worker_workspace._resolve_module_validator_argv(
        ["/usr/bin/python3.12", "-m", "mypy", "src"], tmp_path
    )
    assert tokens == [sys.executable, "-P", "-m", "mypy", "src"]
    assert roots == ()
    assert authority is not None
    assert authority["execution_path"] == sys.executable
    assert "module_absent_in_all_trusted_roots:mypy" in authority["source"]


def test_validator_run_pythonpath_keeps_only_trusted_absolute_components() -> None:
    """REWORK finding one: the trusted ruff/mypy validator run keeps ONLY the
    trusted host-absolute PYTHONPATH components (the explicit safe channel) and
    drops every candidate-writable (workspace-relative) one -- ``.``/``src``/any
    ``sub/dir`` -- exactly as ``_host_probe_pythonpath`` does for the host probe.
    The site-packages shadow only wins names present in it, so a relative
    component would otherwise stay on the validator's ``sys.path`` and could shadow
    a startup/dependency import."""
    assert worker_workspace._validator_run_pythonpath_components(
        (".", "src", "sub/dir")
    ) == ()
    kept = "/opt/trusted-site"
    assert worker_workspace._validator_run_pythonpath_components(
        (".", "src", kept)
    ) == (kept,)
    # Order among trusted absolutes is preserved; only candidate-writable ones drop.
    assert worker_workspace._validator_run_pythonpath_components(
        ("/a", "src", "/b", ".")
    ) == ("/a", "/b")


@posix_layout
def test_candidate_relative_pythonpath_component_cannot_hijack_validator_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REWORK finding one (executable): ``-P`` drops only the implicit cwd entry
    and the trusted ``site-packages`` shadow only wins names that EXIST in it, so a
    candidate-writable (workspace-relative) PYTHONPATH component can still shadow
    the validator's STARTUP imports -- most sharply a ``sitecustomize.py``, which
    CPython imports during site initialisation from ANY ``sys.path`` entry
    regardless of order. The trusted ruff/mypy run therefore keeps only the trusted
    host-absolute components and drops every candidate-writable one, so such a file
    never lands on the validator's ``sys.path``. The subprocess control proves the
    file WOULD execute when the component is present, so the assertion cannot
    silently rot."""
    root = tmp_path / ".venv"
    (root / "bin").mkdir(parents=True)
    venv_python = root / "bin" / "python"
    os.symlink(sys.executable, venv_python)  # a REAL interpreter to import with
    _pin_root(monkeypatch, root)
    _force_module_present(monkeypatch)

    tokens, _roots, _authority = worker_workspace._resolve_module_validator_argv(
        ["/usr/bin/python3.12", "-m", "mypy", "src"], tmp_path
    )
    assert tokens[0] == str(venv_python)
    assert tokens[1] == "-P"

    workdir = tmp_path / "worktree"
    component = workdir / "src"
    component.mkdir(parents=True)
    # ``sitecustomize`` runs at interpreter startup, before ``-m mypy`` -- the
    # startup-import hijack the drop closes and ``-P`` does not.
    (component / "sitecustomize.py").write_text(
        "import pathlib\n"
        "pathlib.Path('HIJACKED').write_text('x', encoding='utf-8')\n",
        encoding="utf-8",
    )

    # The relative worktree component is candidate-writable and is dropped from the
    # trusted validator run's PYTHONPATH -- the exact filter run_validations applies.
    assert worker_workspace._validator_run_pythonpath_components(("src",)) == ()

    # Control: with the candidate component ON PYTHONPATH (the pre-drop run) the
    # ``sitecustomize`` executes at startup -- ``-P`` does not stop it.
    control = {**os.environ, "PYTHONPATH": str(component)}
    subprocess.run(
        tokens,
        cwd=workdir,
        env=control,
        timeout=60,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert (workdir / "HIJACKED").exists()
    (workdir / "HIJACKED").unlink()

    # Dropped: with the candidate component removed (what the trusted run does),
    # startup imports nothing candidate-authored.
    dropped = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    subprocess.run(
        tokens,
        cwd=workdir,
        env=dropped,
        timeout=60,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert not (workdir / "HIJACKED").exists()


@posix_layout
def test_console_script_root_with_python3_but_no_python_keeps_parity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REWORK finding two: a trusted root that carries the validator CONSOLE SCRIPT
    and a ``bin/python3`` but NO ``bin/python`` must still resolve the ``-m`` form
    through that SAME trusted root -- via its own ``bin/python3`` -- never silently
    downgrade to ``sys.executable``. Downgrading would re-open the NF-2026-00582
    asymmetry the bare form (which resolves the console script) does not have."""
    root = tmp_path / ".venv"
    mypy = _runtime_executable(root, "mypy")  # console script -> bare form trusted
    python3 = _runtime_executable(root, "python3")  # ONLY python3, no bin/python
    _pin_root(monkeypatch, root)
    assert not (root / "bin" / "python").exists()

    bare, bare_roots = (
        worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            ["mypy", "src"], tmp_path
        )
    )
    assert bare == [str(mypy.resolve()), "src"]

    module, module_roots = (
        worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            ["/usr/bin/python3.12", "-m", "mypy", "src"], tmp_path
        )
    )
    # Resolved through the root's own ``bin/python3`` (the executed activation
    # spelling), not the untrusted MCP interpreter or ``sys.executable``.
    assert module == [str(python3), "-P", "-m", "mypy", "src"]
    assert module[0] != sys.executable
    assert module_roots == bare_roots == (root.resolve(strict=False),)


@posix_layout
def test_console_script_root_prefers_bin_python_when_both_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REWORK finding two boundary: a root that carries BOTH ``bin/python`` and
    ``bin/python3`` still executes ``bin/python`` -- the canonical venv activation
    symlink -- so the python3 fallback only ever fills the gap, never changes the
    preferred spelling."""
    root = tmp_path / ".venv"
    _runtime_executable(root, "mypy")
    python = _runtime_executable(root, "python")
    _runtime_executable(root, "python3")
    _pin_root(monkeypatch, root)

    module, _roots = (
        worker_workspace._normalize_trusted_validation_executable_argv_with_roots(
            ["/usr/bin/python3.12", "-m", "mypy", "src"], tmp_path
        )
    )
    assert module == [str(python), "-P", "-m", "mypy", "src"]


def test_run_validations_records_final_module_interpreter_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The receipt describes the interpreter in final executed argv.

    Interpreter normalization runs twice: workspace-relative normalization may
    first author a receipt for the declared ``.venv/bin/python``, then the
    module-validator resolver may choose a different trusted runtime or the
    coordinator fallback.  The final resolver's authority must win while the
    receipt still preserves the originally declared command head.
    """
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    home = tmp_path / "home"
    scratch = tmp_path / "scratch"
    for path in (repo, worktree, home, scratch):
        path.mkdir()
    workspace = worker_workspace.WorkerWorkspace(
        request_id="nf582-final-authority",
        repo=repo,
        path=worktree,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    old_python = tmp_path / "old-runtime" / "python"
    final_python = tmp_path / "final-runtime" / "python"
    old_receipt = {
        "schema_id": "aiworkhub.validation_interpreter_authority.v1",
        "declared": ".venv/bin/python",
        "source": "workspace_venv",
        "execution_path": str(old_python),
        "endpoint": str(old_python.parent),
    }
    final_receipt = {
        "schema_id": "aiworkhub.validation_interpreter_authority.v1",
        "declared": str(old_python),
        "source": "module_validator_no_trusted_root:test",
        "execution_path": str(final_python),
        "endpoint": str(final_python.parent),
    }
    monkeypatch.setattr(
        worker_workspace,
        "_normalize_validation_interpreter_argv",
        lambda _workspace, argv: (
            [str(old_python), "-m", "mypy", *argv[3:]],
            old_receipt,
        ),
    )
    monkeypatch.setattr(
        worker_workspace,
        "_normalize_trusted_validation_executable_argv_with_authority",
        lambda argv, _repo: (
            [str(final_python), "-P", "-m", "mypy", *argv[3:]],
            (),
            final_receipt,
        ),
    )
    monkeypatch.setattr(
        worker_workspace, "provision_validation_exec_scratch", lambda _workspace: scratch
    )
    monkeypatch.setattr(
        worker_workspace, "cleanup_validation_exec_scratch", lambda _scratch: None
    )
    monkeypatch.setattr(worker_workspace, "sanitized_env", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        worker_workspace,
        "python_candidate_authority",
        lambda _workspace: {"digest": ""},
    )
    monkeypatch.setattr(
        worker_workspace.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
    )

    [result] = worker_workspace.run_validations(
        workspace,
        [".venv/bin/python -m mypy src"],
        backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
        adapter_id="vscode_lm",
    )

    assert result["executed_argv"][0] == str(final_python)
    assert result["interpreter_authority"]["execution_path"] == str(final_python)
    assert result["interpreter_authority"]["declared"] == ".venv/bin/python"
