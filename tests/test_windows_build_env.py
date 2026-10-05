"""NF-2026-01337: the Windows build environment's decisions, provable anywhere.

Two halves.  The first fakes the whole installation -- vswhere, the registry,
and real directories under ``tmp_path`` -- so every rule the module owns
(caller wins, append never prepend, case-insensitive dedupe, NMake only inside
a container, missing piece omitted) is pinned on any OS with no Visual Studio
and no AppContainer.

The second half is two live probes, skipped unless this is Windows and the VC
tools actually resolve: ``clang-cl`` compiling, linking and running a
hello-world, and VS's cmake configuring, building and ``ctest``-ing a one-test
project with ``-G "NMake Makefiles"``.  In the AppContainer validation lane
those two ARE the acceptance measurement for this card, so when one fails it
reports the probe's exact streams rather than skipping or working around it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import windows_build_env as wbe

_KIT_VERSION = "10.0.26100.0"
# Lexicographically LARGER than the version above and numerically smaller, so a
# string sort would pick this one. It must lose.
_OLDER_KIT_VERSION = "10.0.9999.0"
_MSVC_VERSION = "14.44.35207"
_OLDER_MSVC_VERSION = "14.30.30705"
_HELLO = '#include <cstdio>\nint main() { std::printf("nf1337-ok\\n"); return 0; }\n'


@pytest.fixture(autouse=True)
def forget_the_cached_toolchain():
    """Every case builds its own installation, so none may inherit a cache."""
    wbe.msvc_toolchain_env.cache_clear()
    yield
    wbe.msvc_toolchain_env.cache_clear()


class _FakeRegistryKey:
    """One opened key: the values beneath it, and nothing else."""

    def __init__(self, values: dict) -> None:
        self.values = values

    def __enter__(self) -> "_FakeRegistryKey":
        return self

    def __exit__(self, *_exc_info) -> bool:
        return False


def _fake_winreg(tree: dict):
    """A read-only ``winreg`` over ``{(hive, key): {name: (value, kind)}}``."""

    def _open_key(hive, key):
        if (hive, key) not in tree:
            raise FileNotFoundError(2, "no such key", str(key))
        return _FakeRegistryKey(tree[(hive, key)])

    def _query_value_ex(handle, name):
        if name not in handle.values:
            raise FileNotFoundError(2, "no such value", str(name))
        return handle.values[name]

    return SimpleNamespace(
        HKEY_LOCAL_MACHINE="HKLM",
        HKEY_CURRENT_USER="HKCU",
        REG_SZ=1,
        REG_EXPAND_SZ=2,
        OpenKey=_open_key,
        QueryValueEx=_query_value_ex,
    )


def _make_dirs(root: Path, *relative: str) -> list[Path]:
    made = []
    for item in relative:
        directory = root.joinpath(*item.split("/"))
        directory.mkdir(parents=True, exist_ok=True)
        made.append(directory)
    return made


def _install(
    tmp_path: Path,
    monkeypatch,
    *,
    vswhere_stdout: str | None = None,
    vswhere_returncode: int = 0,
    registry: dict | None = None,
):
    """A complete fake VS + Windows Kit installation, on whatever OS runs this.

    Returns the facts a case needs to assert against: the roots, the exact
    directories the module is expected to find, and the recorded vswhere argv.
    """
    monkeypatch.setattr(wbe, "is_windows", lambda *_args, **_kwargs: True)
    vs_root = tmp_path / "vs"
    kit_root = tmp_path / "kits"
    program_files_x86 = tmp_path / "pfx86"

    msvc = f"VC/Tools/MSVC/{_MSVC_VERSION}"
    _make_dirs(
        vs_root,
        f"{msvc}/include",
        f"{msvc}/lib/x64",
        f"{msvc}/bin/Hostx64/x64",
        f"VC/Tools/MSVC/{_OLDER_MSVC_VERSION}/include",
        "VC/Tools/Llvm/x64/bin",
        "Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin",
        "Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja",
    )
    _make_dirs(
        kit_root,
        *(f"Include/{_KIT_VERSION}/{leaf}" for leaf in wbe._KIT_INCLUDE_LEAVES),
        f"Include/{_OLDER_KIT_VERSION}/ucrt",
        f"Lib/{_KIT_VERSION}/ucrt/x64",
        f"Lib/{_KIT_VERSION}/um/x64",
        f"UnionMetadata/{_KIT_VERSION}",
        f"References/{_KIT_VERSION}",
        f"bin/{_KIT_VERSION}/x64",
    )

    vswhere = program_files_x86.joinpath(*wbe._VSWHERE_RELATIVE)
    vswhere.parent.mkdir(parents=True, exist_ok=True)
    vswhere.write_bytes(b"MZ")
    # Both roots, so a real Program Files on the host running this suite is
    # never one of the roots under test and cannot answer either mechanism.
    monkeypatch.setenv("ProgramFiles", str(program_files_x86))
    monkeypatch.setenv("ProgramFiles(x86)", str(program_files_x86))

    launches: list[dict] = []

    def _run(argv, **kwargs):
        launches.append({"argv": list(argv), **kwargs})
        stdout = str(vs_root) + "\n" if vswhere_stdout is None else vswhere_stdout
        return subprocess.CompletedProcess(argv, vswhere_returncode, stdout, "")

    monkeypatch.setattr(
        wbe,
        "subprocess",
        SimpleNamespace(
            run=_run,
            DEVNULL=subprocess.DEVNULL,
            SubprocessError=subprocess.SubprocessError,
        ),
    )
    tree = {
        ("HKLM", wbe._INSTALLED_ROOTS_KEY): {"KitsRoot10": (str(kit_root), 1)},
    }
    if registry is not None:
        tree.update(registry)
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg(tree))

    msvc_root = vs_root.joinpath(*msvc.split("/"))
    cmake = vs_root / "Common7" / "IDE" / "CommonExtensions" / "Microsoft" / "CMake"
    return SimpleNamespace(
        vs_root=vs_root,
        kit_root=kit_root,
        msvc_root=msvc_root,
        launches=launches,
        registry=tree,
        tool_dirs=[
            str(msvc_root / "bin" / "Hostx64" / "x64"),
            str(kit_root / "bin" / _KIT_VERSION / "x64"),
            str(vs_root / "VC" / "Tools" / "Llvm" / "x64" / "bin"),
            str(cmake / "CMake" / "bin"),
            str(cmake / "Ninja"),
        ],
    )


# --------------------------------------------------------------------------- #
# what the installation yields
# --------------------------------------------------------------------------- #


def test_the_toolchain_carries_the_x64_roots_and_the_tool_directories(
    tmp_path, monkeypatch
):
    install = _install(tmp_path, monkeypatch)

    toolchain = wbe.msvc_toolchain_env()

    kit, version = install.kit_root, _KIT_VERSION
    assert toolchain["INCLUDE"].split(os.pathsep) == [
        str(install.msvc_root / "include"),
        *(str(kit / "Include" / version / leaf) for leaf in wbe._KIT_INCLUDE_LEAVES),
    ]
    assert toolchain["LIB"].split(os.pathsep) == [
        str(install.msvc_root / "lib" / "x64"),
        str(kit / "Lib" / version / "ucrt" / "x64"),
        str(kit / "Lib" / version / "um" / "x64"),
    ]
    assert toolchain["LIBPATH"].split(os.pathsep) == [
        str(install.msvc_root / "lib" / "x64"),
        str(kit / "UnionMetadata" / version),
        str(kit / "References" / version),
    ]
    assert toolchain["PATH"].split(os.pathsep) == install.tool_dirs


def test_the_highest_version_is_numeric_not_lexicographic(tmp_path, monkeypatch):
    """``10.0.9999.0`` sorts above ``10.0.26100.0`` as text and must still lose."""
    install = _install(tmp_path, monkeypatch)

    toolchain = wbe.msvc_toolchain_env()

    assert _OLDER_KIT_VERSION not in toolchain["INCLUDE"]
    assert _OLDER_MSVC_VERSION not in toolchain["INCLUDE"]
    assert _KIT_VERSION in toolchain["INCLUDE"]
    assert str(install.msvc_root) in toolchain["INCLUDE"]


def test_vswhere_is_asked_as_an_argv_list_with_no_shell_and_a_timeout(
    tmp_path, monkeypatch
):
    install = _install(tmp_path, monkeypatch)

    wbe.msvc_toolchain_env()

    assert len(install.launches) == 1
    launch = install.launches[0]
    assert launch["argv"][0] == str(
        tmp_path / "pfx86" / Path(*wbe._VSWHERE_RELATIVE)
    )
    assert launch["argv"][1:] == list(wbe._VSWHERE_ARGV_TAIL)
    assert "Microsoft.VisualStudio.Component.VC.Tools.x86.x64" in launch["argv"]
    assert launch["shell"] is False
    assert launch["timeout"] == wbe._VSWHERE_TIMEOUT_SECONDS
    assert launch["stdin"] is subprocess.DEVNULL
    # No vcvars, no cmd.exe, no shell string: the argv is the whole derivation.
    assert not any("vcvars" in str(item).lower() for item in launch["argv"])
    assert not any("cmd.exe" in str(item).lower() for item in launch["argv"])


def test_the_toolchain_is_derived_once_per_process(tmp_path, monkeypatch):
    """A property of the installation, so one launch answers every command."""
    install = _install(tmp_path, monkeypatch)

    first = wbe.msvc_toolchain_env()
    second = wbe.msvc_toolchain_env()

    assert first == second
    assert len(install.launches) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"vswhere_stdout": ""},
        {"vswhere_returncode": 1},
    ],
    ids=["vswhere_names_nothing", "vswhere_fails"],
)
def test_no_visual_studio_yields_no_overlay_at_all(tmp_path, monkeypatch, kwargs):
    """Absent is absent: no INCLUDE, no LIB, no partial answer.

    Off the container that leaves the overlay empty.  Inside one the two
    container defaults stand on their own, because neither of them describes
    the installation -- they describe what the container cannot do.
    """
    _install(tmp_path, monkeypatch, registry={}, **kwargs)
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg({}))

    assert wbe.msvc_toolchain_env() == {}
    assert wbe.build_tool_env({}, appcontainer=False) == {}
    assert wbe.build_tool_env({}, appcontainer=True) == {
        "CMAKE_GENERATOR": "NMake Makefiles",
        "_CL_": "/Z7",
    }


def _blind_vswhere(monkeypatch, root: Path):
    """Both Program Files roots pinned at ``root``, and vswhere answering nothing.

    This is the measured container shape: vswhere exits 0 and prints nothing,
    because the installer state it reads under ProgramData is WinError 5 there.
    """
    monkeypatch.setattr(wbe, "is_windows", lambda *_a, **_k: True)
    monkeypatch.setenv("ProgramFiles", str(root))
    monkeypatch.setenv("ProgramFiles(x86)", str(root))
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg({}))
    monkeypatch.setattr(
        wbe,
        "subprocess",
        SimpleNamespace(
            run=lambda argv, **_k: subprocess.CompletedProcess(argv, 0, "", ""),
            DEVNULL=subprocess.DEVNULL,
            SubprocessError=subprocess.SubprocessError,
        ),
    )


def _probed_install(root: Path, *, msvc: str, clang: bool, cmake: bool) -> None:
    """A VS install tree as the filesystem shows one; ``cl.exe`` is required."""
    tools = root / "VC" / "Tools" / "MSVC" / msvc
    _make_dirs(tools, "include", "lib/x64", "bin/Hostx64/x64")
    (tools / "bin" / "Hostx64" / "x64" / "cl.exe").write_bytes(b"MZ")
    if clang:
        llvm = root / "VC" / "Tools" / "Llvm" / "x64" / "bin"
        llvm.mkdir(parents=True, exist_ok=True)
        (llvm / "clang-cl.exe").write_bytes(b"MZ")
    if cmake:
        cmake_bin = root.joinpath(*wbe._CMAKE_RELATIVE, "CMake", "bin")
        cmake_bin.mkdir(parents=True, exist_ok=True)
        (cmake_bin / "cmake.exe").write_bytes(b"MZ")


def test_a_program_files_root_with_no_visual_studio_is_not_an_error(
    tmp_path, monkeypatch
):
    """No installer and no install tree is an empty answer, never a raise."""
    empty = tmp_path / "empty-program-files"
    empty.mkdir()
    _blind_vswhere(monkeypatch, empty)

    assert wbe.msvc_toolchain_env() == {}


def test_the_install_tree_answers_when_vswhere_is_blind(tmp_path, monkeypatch):
    """Measured in this sandbox: listing %ProgramData%\\Microsoft\\VisualStudio
    \\Packages\\_Instances fails WinError 5, so vswhere exits 0 printing nothing
    even for ``-all -products *``. The install trees under Program Files are
    granted read+execute, so they are the evidence that remains.
    """
    root = tmp_path / "Program Files"
    lean = root / "Microsoft Visual Studio" / "18" / "Insiders"
    complete = root / "Microsoft Visual Studio" / "2022" / "Enterprise"
    _probed_install(lean, msvc="14.51.36231", clang=False, cmake=True)
    _probed_install(complete, msvc="14.44.35207", clang=True, cmake=True)
    _blind_vswhere(monkeypatch, root)

    toolchain = wbe.msvc_toolchain_env()

    # The lean install carries the NEWER MSVC; the complete one carries the
    # clang-cl a C++ validation compiles with, and that is what decides.
    assert toolchain["INCLUDE"] == str(
        complete / "VC" / "Tools" / "MSVC" / "14.44.35207" / "include"
    )
    assert str(lean) not in toolchain["PATH"]
    assert str(complete / "VC" / "Tools" / "Llvm" / "x64" / "bin") in toolchain["PATH"]


def test_a_tree_without_the_x64_host_compiler_is_not_a_candidate(
    tmp_path, monkeypatch
):
    """``cl.exe`` is the requirement vswhere is asked for; the probe keeps it."""
    root = tmp_path / "Program Files"
    hollow = root / "Microsoft Visual Studio" / "2022" / "Community"
    _make_dirs(hollow, "VC/Tools/MSVC/14.44.35207/include")
    _blind_vswhere(monkeypatch, root)

    assert wbe.msvc_toolchain_env() == {}


def test_vswhere_wins_over_the_probed_tree_when_it_can_answer(tmp_path, monkeypatch):
    """The installer's own answer is authoritative wherever it can be had."""
    install = _install(tmp_path, monkeypatch)
    probed = tmp_path / "pfx86" / "Microsoft Visual Studio" / "2022" / "Enterprise"
    _probed_install(probed, msvc="14.99.99999", clang=True, cmake=True)
    wbe.msvc_toolchain_env.cache_clear()

    toolchain = wbe.msvc_toolchain_env()

    assert str(install.msvc_root / "include") in toolchain["INCLUDE"]
    assert str(probed) not in toolchain["INCLUDE"]


def test_a_kit_whose_headers_are_absent_contributes_nothing(tmp_path, monkeypatch):
    """A registry KitsRoot10 pointing at a tree with no Include is omitted."""
    install = _install(tmp_path, monkeypatch)
    shutil.rmtree(install.kit_root / "Include")
    wbe.msvc_toolchain_env.cache_clear()

    toolchain = wbe.msvc_toolchain_env()

    assert str(install.kit_root) not in toolchain["INCLUDE"]
    assert str(install.kit_root) not in toolchain["LIB"]
    assert toolchain["INCLUDE"] == str(install.msvc_root / "include")
    # The VS half is untouched by the kit's absence.
    assert str(install.msvc_root / "bin" / "Hostx64" / "x64") in toolchain["PATH"]


# --------------------------------------------------------------------------- #
# the registry PATH, read now rather than inherited
# --------------------------------------------------------------------------- #


def test_registry_path_entries_put_the_user_hive_first_and_expand_it(
    tmp_path, monkeypatch
):
    """REG_EXPAND_SZ is what the registry stores, so %VAR% must be resolved."""
    monkeypatch.setattr(wbe, "is_windows", lambda *_a, **_k: True)
    monkeypatch.setenv("NF1337_PROFILE", str(tmp_path / "profile"))
    user = f"%NF1337_PROFILE%{os.sep}bin"
    machine = str(tmp_path / "machine" / "bin")
    monkeypatch.setitem(
        sys.modules,
        "winreg",
        _fake_winreg(
            {
                ("HKCU", wbe._USER_ENVIRONMENT_KEY): {"Path": (user, 2)},
                ("HKLM", wbe._MACHINE_ENVIRONMENT_KEY): {
                    "Path": (os.pathsep.join([machine, ""]), 1)
                },
            }
        ),
    )

    assert wbe.registry_path_entries() == [
        str(tmp_path / "profile") + f"{os.sep}bin",
        machine,
    ]


def test_an_unreadable_registry_is_no_entries_rather_than_a_raise(monkeypatch):
    monkeypatch.setattr(wbe, "is_windows", lambda *_a, **_k: True)
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg({}))

    assert wbe.registry_path_entries() == []


# --------------------------------------------------------------------------- #
# build_tool_env: the caller wins, and PATH only grows at the end
# --------------------------------------------------------------------------- #


def test_the_caller_keeps_every_value_it_declared(tmp_path, monkeypatch):
    """Caller wins is the rule the whole overlay is built around."""
    _install(tmp_path, monkeypatch)
    declared = {
        "INCLUDE": r"C:\declared\include",
        "LIB": r"C:\declared\lib",
        "LIBPATH": r"C:\declared\libpath",
        "CMAKE_GENERATOR": "Visual Studio 17 2022",
        "PATH": r"C:\declared\bin",
    }

    overlay = wbe.build_tool_env(declared, appcontainer=True)

    assert "INCLUDE" not in overlay
    assert "LIB" not in overlay
    assert "LIBPATH" not in overlay
    assert "CMAKE_GENERATOR" not in overlay
    # PATH is the one key that may change, and only by growing at the end.
    assert overlay["PATH"].split(os.pathsep)[0] == r"C:\declared\bin"


def test_the_overlay_appends_tool_directories_then_registry_entries(
    tmp_path, monkeypatch
):
    install = _install(
        tmp_path,
        monkeypatch,
        registry={
            ("HKCU", wbe._USER_ENVIRONMENT_KEY): {
                "Path": (str(tmp_path / "user-tool"), 1)
            },
        },
    )

    overlay = wbe.build_tool_env({"PATH": r"C:\caller\one"}, appcontainer=True)

    assert overlay["PATH"].split(os.pathsep) == [
        r"C:\caller\one",
        *install.tool_dirs,
        str(tmp_path / "user-tool"),
    ]
    assert overlay["INCLUDE"] == wbe.msvc_toolchain_env()["INCLUDE"]


def test_path_additions_dedupe_case_and_separator_insensitively(
    tmp_path, monkeypatch
):
    """``C:\\Tools\\`` and ``c:/tools`` are one directory on Windows."""
    install = _install(tmp_path, monkeypatch)
    already = install.tool_dirs[0]
    caller = already.upper().replace(os.sep, "/") + "/"

    overlay = wbe.build_tool_env({"PATH": caller}, appcontainer=True)

    entries = overlay["PATH"].split(os.pathsep)
    assert entries[0] == caller
    assert already not in entries
    assert len(entries) == len(install.tool_dirs)


def test_a_path_that_already_holds_everything_is_left_untouched(
    tmp_path, monkeypatch
):
    """Nothing missing means no PATH key at all, not a renormalised copy."""
    install = _install(tmp_path, monkeypatch)
    declared = os.pathsep.join(install.tool_dirs)

    overlay = wbe.build_tool_env({"PATH": declared}, appcontainer=True)

    assert "PATH" not in overlay


def test_the_nmake_default_is_the_container_s_alone(tmp_path, monkeypatch):
    """Off the container ninja is fine, so the lane must not pick a generator."""
    _install(tmp_path, monkeypatch)

    assert wbe.build_tool_env({}, appcontainer=True)["CMAKE_GENERATOR"] == (
        "NMake Makefiles"
    )
    assert "CMAKE_GENERATOR" not in wbe.build_tool_env({}, appcontainer=False)


def test_the_pdb_free_debug_flag_is_the_container_s_alone(tmp_path, monkeypatch):
    """Measured: cl's very first probe died with C1902, because /Zi wants a PDB
    server over RPC that the container cannot reach.  /Z7 carries the same
    information in the object file; off the container /Zi works, so the lane
    declares nothing there.
    """
    _install(tmp_path, monkeypatch)

    assert wbe.build_tool_env({}, appcontainer=True)["_CL_"] == "/Z7"
    assert "_CL_" not in wbe.build_tool_env({}, appcontainer=False)


def test_caller_declared_compiler_options_are_never_touched(tmp_path, monkeypatch):
    """``_CL_`` is an option list, so replacing it -- or quietly appending to
    it -- would change what the caller's own command compiles."""
    _install(tmp_path, monkeypatch)

    overlay = wbe.build_tool_env({"_CL_": "/Zi /W4"}, appcontainer=True)

    assert "_CL_" not in overlay


def test_a_blank_caller_value_is_no_value(tmp_path, monkeypatch):
    """An inherited empty INCLUDE is what "no INCLUDE" looks like in practice."""
    _install(tmp_path, monkeypatch)

    overlay = wbe.build_tool_env(
        {"INCLUDE": "   ", "CMAKE_GENERATOR": "", "_CL_": " "}, appcontainer=True
    )

    assert overlay["INCLUDE"] == wbe.msvc_toolchain_env()["INCLUDE"]
    assert overlay["CMAKE_GENERATOR"] == "NMake Makefiles"
    assert overlay["_CL_"] == "/Z7"


def test_off_windows_nothing_is_derived_and_nothing_raises(monkeypatch):
    monkeypatch.setattr(wbe, "is_windows", lambda *_a, **_k: False)

    assert wbe.msvc_toolchain_env() == {}
    assert wbe.registry_path_entries() == []
    assert wbe.build_tool_env({"PATH": "x"}, appcontainer=True) == {}
    assert wbe.build_tool_env({}, appcontainer=False) == {}


# --------------------------------------------------------------------------- #
# the ninja denial, named rather than mistaken for a failing gate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "stdout, stderr, denied",
    [
        ("", "ninja: fatal: CreateNamedPipe: Access is denied\n", True),
        ("ninja: fatal: CreateNamedPipe: Access is denied", "", True),
        ("", "NINJA: FATAL: CREATENAMEDPIPE: ACCESS IS DENIED", True),
        ("", "ninja: build stopped: subcommand failed.\n", False),
        ("", "cl: fatal error C1083: 'cstdio' file not found", False),
        # A named-pipe denial from some other tool is a different fact.
        ("", "MSBUILD : error MSB0001: CreateNamedPipe: Access is denied", False),
        ("", "", False),
    ],
)
def test_the_ninja_pipe_denial_is_recognised_by_all_three_tokens(
    stdout, stderr, denied
):
    assert wbe.ninja_pipe_denied(stdout, stderr) is denied


# --------------------------------------------------------------------------- #
# the live probes: in the AppContainer lane these ARE the measurement
# --------------------------------------------------------------------------- #


def _live_build_env() -> dict[str, str] | None:
    """``os.environ`` merged with the real overlay, or ``None`` off a VS host."""
    if sys.platform != "win32":
        return None
    env = dict(os.environ)
    env.update(wbe.build_tool_env(env, appcontainer=True))
    if not env.get("INCLUDE") or not env.get("LIB"):
        return None
    return env


def _streams(step) -> str:
    """The probe's exact streams: a container failure must read as itself."""
    return (
        f"argv={step.args}\nreturncode={step.returncode}\n"
        f"stdout={step.stdout}\nstderr={step.stderr}"
    )


def _live_probe(tool: str):
    """``(env, resolved tool)``, or a skip naming exactly what was unavailable."""
    env = _live_build_env()
    if env is None:
        pytest.skip(f"not win32 with resolvable VC tools; cannot run {tool}")
    resolved = shutil.which(tool, path=env.get("PATH", ""))
    if resolved is None:
        pytest.skip(f"{tool} is not in the derived tool directories")
    return env, resolved


_LIVE_PROBE_TIMEOUT_SECONDS = 300
_LIVE_LOG_TAIL_CHARS = 4096


def _run_live(label: str, argv: list[str], *, cwd: Path, env: dict[str, str], logs: Path):
    """One bounded probe step, its streams written to files rather than pipes.

    Files, not ``capture_output``: a build tool hands its pipes to its own
    children -- clang-cl spawns the linker, nmake spawns cl -- and any one of
    them outliving the tool keeps the parent blocked in ``communicate()``
    draining a pipe that never reaches EOF.  Measured on this host: trivial
    ``--version`` probes answer in under a second, while a captured compile
    step did not return at all and left an orphaned ``conhost.exe`` behind.
    Writing to files removes the pipe, so the only way this step can fail to
    answer is its own bounded deadline -- and expiring that fails the test by
    name with whatever had already been written, never a silent skip and never
    an opaque hang for the whole validation command to wedge on.
    """
    logs.mkdir(parents=True, exist_ok=True)
    out_path, err_path = logs / f"{label}.stdout", logs / f"{label}.stderr"

    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")[
                -_LIVE_LOG_TAIL_CHARS:
            ]
        except OSError:
            return ""

    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd),
                env=env,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                timeout=_LIVE_PROBE_TIMEOUT_SECONDS,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            pytest.fail(
                "validation_unsupported_in_sandbox:live_build_probe_timed_out:"
                f"{label}\nargv={argv}\n"
                f"timeout_seconds={_LIVE_PROBE_TIMEOUT_SECONDS}\n"
                f"partial_stdout={_read(out_path)}\n"
                f"partial_stderr={_read(err_path)}"
            )
    return SimpleNamespace(
        args=argv,
        returncode=completed.returncode,
        stdout=_read(out_path),
        stderr=_read(err_path),
    )


def test_clang_cl_compiles_links_and_runs_a_hello_world(tmp_path):
    """NF-2026-01337's first measured failure: ``'cstdio' file not found``."""
    env, clang_cl = _live_probe("clang-cl")
    logs = tmp_path / "logs"
    source = tmp_path / "hello.cpp"
    source.write_text(_HELLO, encoding="utf-8")
    binary = tmp_path / "hello.exe"

    compiled = _run_live(
        "clang_cl_compile",
        [
            clang_cl,
            "/nologo",
            "/EHsc",
            str(source),
            f"/Fe:{binary}",
            # An explicit object FILE, never a directory: a trailing separator
            # here ends the argument with a backslash, which Windows argv
            # quoting then reads as escaping the closing quote.
            f"/Fo:{tmp_path / 'hello.obj'}",
        ],
        cwd=tmp_path,
        env=env,
        logs=logs,
    )
    assert compiled.returncode == 0, _streams(compiled)
    assert binary.is_file(), _streams(compiled)

    ran = _run_live("hello", [str(binary)], cwd=tmp_path, env=env, logs=logs)
    assert ran.returncode == 0, _streams(ran)
    assert "nf1337-ok" in ran.stdout


def test_cmake_nmake_configures_builds_and_tests_a_project(tmp_path):
    """The generator the container can actually run, end to end through ctest."""
    env, cmake = _live_probe("cmake")
    ctest = shutil.which("ctest", path=env.get("PATH", ""))
    if ctest is None:
        pytest.skip("ctest is not in the derived tool directories")
    logs = tmp_path / "logs"
    project, build = tmp_path / "project", tmp_path / "build"
    project.mkdir()
    build.mkdir()
    (project / "main.cpp").write_text(_HELLO, encoding="utf-8")
    (project / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\n"
        "project(nf1337 CXX)\n"
        "add_executable(nf1337 main.cpp)\n"
        "enable_testing()\n"
        "add_test(NAME nf1337_runs COMMAND nf1337)\n",
        encoding="utf-8",
    )

    configured = _run_live(
        "cmake_configure",
        [
            cmake,
            "-S",
            str(project),
            "-B",
            str(build),
            "-G",
            "NMake Makefiles",
            "-DCMAKE_BUILD_TYPE=Release",
        ],
        cwd=tmp_path,
        env=env,
        logs=logs,
    )
    assert configured.returncode == 0, _streams(configured)

    built = _run_live(
        "cmake_build", [cmake, "--build", str(build)], cwd=tmp_path, env=env, logs=logs
    )
    assert built.returncode == 0, _streams(built)

    tested = _run_live(
        "ctest", [ctest, "--output-on-failure"], cwd=build, env=env, logs=logs
    )
    assert tested.returncode == 0, _streams(tested)
    assert "nf1337_runs" in tested.stdout
