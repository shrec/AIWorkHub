r"""NF-2026-01337 / EntryLink NF-44: the Windows build environment, derived.

Measured inside an AppContainer -- the validation lane and the worker sandbox
both -- no C or C++ build ran at all, for three independent reasons.

``clang-cl`` reported ``'cstdio' file not found`` and every link failed,
because the container inherits no ``INCLUDE``, ``LIB`` or ``LIBPATH``.

``ninja`` aborted with ``ninja: fatal: CreateNamedPipe: Access is denied``: it
creates the global ``\\.\pipe\ninja_pid<pid>`` and an AppContainer may only
create ``\\.\pipe\LOCAL\*``.  That one is structural and no ACL fixes it, so
:func:`ninja_pipe_denied` names it rather than letting it read as a candidate's
failing gate, and :func:`build_tool_env` defaults the generator away from it.

``cl.exe`` then failed its very first compiler probe with ``fatal error C1902:
Program database manager mismatch``: CMake's Debug default ``/Zi`` hands debug
information to ``mspdbsrv.exe`` over RPC, which an AppContainer cannot reach,
and ``-DCMAKE_BUILD_TYPE=Release`` never reaches a ``try_compile`` at all.  So
:func:`build_tool_env` appends ``/Z7`` through ``_CL_`` instead, which every
cl.exe and clang-cl reads after its own command line.

And workers inherited VS Code's stale ``PATH``, so anything installed into the
user profile after that window opened was invisible (``WinError 5``).

The Visual Studio tree, the Windows Kits and Program Files are all granted
``ALL APPLICATION PACKAGES`` read+execute, so VS's own cmake, ctest, nmake and
clang-cl do execute inside the container -- once they are on ``PATH`` and the
include and library roots are set.  Everything here is derived by reading the
filesystem and the registry: never ``vcvars*.bat``, never ``cmd.exe``, never
any shell.  A shell is denied the container, and spawning one to print an
environment is the exact dependency this module exists to remove.

Two rules hold throughout.  The caller always wins: ``INCLUDE``, ``LIB``,
``LIBPATH``, ``CMAKE_GENERATOR`` and ``_CL_`` are contributed only where the
caller set none.  And ``PATH`` is only ever appended to, never prepended to, so
nothing here can silently restage which compiler a declared command resolves to.

Off Windows every derivation answers ``{}`` or ``[]`` and none of them raises:
a toolchain this module could not derive is absent, never half applied.
"""

from __future__ import annotations

import ntpath
import os
import subprocess
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path

from .platform_io import is_windows

# vswhere.exe ships at this fixed location with every Visual Studio installer
# and is the documented way to locate an installation; the registry view of it
# is not.  ``-requires`` is what makes "latest" mean "latest that can actually
# compile x64", rather than a Build Tools install with no VC payload.
_VSWHERE_RELATIVE = ("Microsoft Visual Studio", "Installer", "vswhere.exe")
_VSWHERE_ARGV_TAIL = (
    "-latest",
    "-products",
    "*",
    "-requires",
    "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
    "-property",
    "installationPath",
)
_VSWHERE_TIMEOUT_SECONDS = 30

_INSTALLED_ROOTS_KEY = r"SOFTWARE\Microsoft\Windows Kits\Installed Roots"
_KITS_ROOT_VALUE = "KitsRoot10"
_USER_ENVIRONMENT_KEY = "Environment"
_MACHINE_ENVIRONMENT_KEY = (
    r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
)
_PATH_VALUE = "Path"

_CMAKE_RELATIVE = ("Common7", "IDE", "CommonExtensions", "Microsoft", "CMake")
_KIT_INCLUDE_LEAVES = ("ucrt", "shared", "um", "winrt", "cppwinrt")
_CONTRIBUTED_ROOT_VARIABLES = ("INCLUDE", "LIB", "LIBPATH")
_APPCONTAINER_CMAKE_GENERATOR = "NMake Makefiles"
# cl.exe and clang-cl both read ``_CL_`` as options APPENDED after the command
# line, so /Z7 there beats the /Zi CMake puts in front of it -- including inside
# a try_compile, which no -DCMAKE_BUILD_TYPE reaches.  /Zi hands debug info to
# mspdbsrv.exe over RPC and an AppContainer cannot reach it (measured: ``fatal
# error C1902: Program database manager mismatch`` on the first compiler probe);
# /Z7 writes the same information into the object file and needs no server.
_APPCONTAINER_CL_APPENDED_OPTIONS = "/Z7"

# All three are required: a named-pipe denial from some other tool is a
# different fact and must never be reported under ninja's name.
_NINJA_PIPE_DENIAL_TOKENS = ("ninja", "createnamedpipe", "access is denied")


def _path_key(entry: str) -> str:
    """One case- and separator-insensitive identity for a ``PATH`` entry.

    Windows compares paths case-insensitively, accepts either separator, and
    ignores a trailing one, so ``C:\\Tools\\`` and ``c:/tools`` name the same
    directory and must dedupe as one.  Spelled out here rather than taken from
    ``os.path.normcase``, which is the identity function off Windows and would
    make this module's answer depend on the host that happened to run it.
    """
    return entry.strip().strip('"').replace("/", "\\").rstrip("\\").casefold()


def _split_path(value: str) -> list[str]:
    """``value`` as ``PATH`` entries, blanks and surrounding quotes dropped."""
    return [
        entry
        for entry in (item.strip().strip('"') for item in value.split(os.pathsep))
        if entry
    ]


def _existing_directories(*candidates: Path) -> list[str]:
    """Those of ``candidates`` that are directories here, in the order given.

    "Missing piece -> omitted" is the contract: a kit whose headers were never
    installed contributes nothing, rather than an entry that would make the
    compiler fail later on a path it cannot read.
    """
    found: list[str] = []
    for candidate in candidates:
        try:
            if candidate.is_dir():
                found.append(str(candidate))
        except OSError:
            continue
    return found


def _child_directories(root: Path) -> list[Path]:
    """``root``'s subdirectories in a stable order, or none if unreadable.

    Unreadable is a real answer here rather than an error: the container is
    denied parts of this machine, and a directory it cannot list contributes
    nothing to the overlay.
    """
    try:
        return sorted(child for child in root.iterdir() if child.is_dir())
    except OSError:
        return []


def _highest_numeric_child(root: Path) -> Path | None:
    """The numerically greatest dotted-number subdirectory of ``root``.

    ``10.0.26100.0`` beats ``10.0.9999.0``, which string order gets backwards,
    and a name that is not a dotted number is not a version at all: ``MSVC``
    and ``Llvm`` sit beside the version directories in these very trees.
    """
    highest: Path | None = None
    highest_key: tuple[int, ...] = ()
    for child in _child_directories(root):
        fields = child.name.split(".")
        if not all(field.isdigit() for field in fields):
            continue
        key = tuple(int(field) for field in fields)
        if key > highest_key:
            highest, highest_key = child, key
    return highest


def _program_files_roots() -> list[Path]:
    """The Program Files roots, from the environment or the system anchor.

    ``ProgramFiles`` and ``ProgramFiles(x86)`` are BOTH absent from the
    sanitized worker and validation environment -- the same gap NF-2026-01366
    had to close for the dotnet CLI -- so deriving them from the anchor of
    ``SystemRoot`` is what keeps the installer and the install trees findable
    in the lane this module exists for.  ``SystemRoot`` is not a guess: every
    Windows process has it, because ``CreateProcess`` itself needs it.
    Deduped, and only roots that exist are returned.
    """
    system_root = os.environ.get("SystemRoot") or os.environ.get("windir") or ""
    anchor = Path(system_root).anchor if system_root else ""
    roots: list[Path] = []
    seen: set[str] = set()
    for name, leaf in (
        ("ProgramFiles", "Program Files"),
        ("ProgramFiles(x86)", "Program Files (x86)"),
    ):
        candidate = os.environ.get(name) or (anchor + leaf if anchor else "")
        key = _path_key(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        root = Path(candidate)
        if root.is_dir():
            roots.append(root)
    return roots


def _installation_rank(root: Path) -> tuple[int, int, tuple[int, ...]] | None:
    """How complete the x64 C++ toolchain under ``root`` is, or ``None``.

    The hard requirement is the one vswhere is asked for -- an x64 host
    ``cl.exe`` -- so a tree without it is not a candidate at all.  Beyond that
    the rank prefers an installation that demonstrably also carries
    ``clang-cl`` and VS's own cmake, and only then the highest MSVC version:
    this path has no installer metadata to ask, so what the tree provides is
    the only evidence there is, and a newer install missing clang-cl cannot
    satisfy a validation that compiles with it.
    """
    msvc = _highest_numeric_child(root / "VC" / "Tools" / "MSVC")
    if msvc is None or not (msvc / "bin" / "Hostx64" / "x64" / "cl.exe").is_file():
        return None
    return (
        int((root / "VC" / "Tools" / "Llvm" / "x64" / "bin" / "clang-cl.exe").is_file()),
        int(Path(root, *_CMAKE_RELATIVE, "CMake", "bin", "cmake.exe").is_file()),
        tuple(int(field) for field in msvc.name.split(".")),
    )


def _probed_installation_path(roots: Iterable[Path]) -> str:
    """The most complete VS install the filesystem itself shows, or ``""``.

    Needed because vswhere answers nothing at all inside an AppContainer.  It
    reads the installer state under
    ``%ProgramData%\\Microsoft\\VisualStudio\\Packages``, which is NOT granted
    to ALL APPLICATION PACKAGES: measured in this sandbox, listing
    ``...\\Packages\\_Instances`` fails ``WinError 5`` and vswhere then exits 0
    printing nothing, even for ``-all -products *``.  The install trees under
    Program Files ARE granted read+execute, so the documented
    ``<root>\\Microsoft Visual Studio\\<channel>\\<edition>`` layout is probed
    two levels deep rather than guessed at one hard-coded path.
    """
    best: tuple[tuple[int, int, tuple[int, ...]], str] | None = None
    for root in roots:
        for channel in _child_directories(root / "Microsoft Visual Studio"):
            for edition in _child_directories(channel):
                rank = _installation_rank(edition)
                if rank is not None and (best is None or rank > best[0]):
                    best = rank, str(edition)
    return best[1] if best is not None else ""


def _vswhere_installation_path(roots: Iterable[Path]) -> str:
    """The latest VS installation carrying the x64 VC tools, or ``""``.

    An argv list, no shell, a bounded timeout, and every failure -- no
    installer, a non-zero exit, a path that is no longer a directory -- answers
    the empty string rather than raising into a caller building an overlay.
    """
    for root in roots:
        vswhere = Path(root, *_VSWHERE_RELATIVE)
        if not vswhere.is_file():
            continue
        try:
            completed = subprocess.run(
                [str(vswhere), *_VSWHERE_ARGV_TAIL],
                capture_output=True,
                text=True,
                timeout=_VSWHERE_TIMEOUT_SECONDS,
                stdin=subprocess.DEVNULL,
                shell=False,
                check=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        if completed.returncode != 0:
            continue
        for line in (completed.stdout or "").splitlines():
            installation = line.strip()
            if installation and Path(installation).is_dir():
                return installation
    return ""


def _registry_text(hive_name: str, key: str, value_name: str) -> tuple[str, bool]:
    """One registry string and whether it is a ``REG_EXPAND_SZ``.

    Read-only by construction: ``OpenKey`` with no write access and one
    ``QueryValueEx``, nothing else.  ``winreg`` is imported here rather than at
    module scope so this module stays importable everywhere, and so a test can
    supply a fake one through ``sys.modules`` with no Windows host.
    """
    try:
        import winreg
    except ImportError:
        return "", False
    hive = getattr(winreg, hive_name, None)
    if hive is None:
        return "", False
    try:
        with winreg.OpenKey(hive, key) as handle:
            value, kind = winreg.QueryValueEx(handle, value_name)
    except OSError:
        return "", False
    if not isinstance(value, str):
        return "", False
    return value, kind == getattr(winreg, "REG_EXPAND_SZ", object())


def _kit_include_version() -> tuple[Path, Path] | None:
    """``(KitsRoot10, its highest numeric Include/<ver>)``, or nothing.

    The version is chosen by what ``Include`` actually holds, because a kit
    whose headers were not installed cannot contribute an include root
    whatever the neighbouring ``Lib`` and ``bin`` trees advertise.
    """
    raw, _expandable = _registry_text(
        "HKEY_LOCAL_MACHINE", _INSTALLED_ROOTS_KEY, _KITS_ROOT_VALUE
    )
    if not raw:
        return None
    root = Path(raw)
    version = _highest_numeric_child(root / "Include")
    if version is None:
        return None
    return root, version


@lru_cache(maxsize=1)
def msvc_toolchain_env() -> dict[str, str]:
    """The x64 MSVC build roots and tool directories this machine really has.

    ``INCLUDE``, ``LIB`` and ``LIBPATH`` carry the MSVC and Windows Kit roots.
    ``PATH`` carries only the tool directories -- the MSVC x64 host compilers,
    the kit's x64 binaries, LLVM's ``clang-cl``, and the cmake and ninja that
    Visual Studio ships -- so a caller can append them without this mapping
    deciding anything about the caller's own ``PATH``.  A variable whose roots
    all turned out to be missing is omitted rather than answered empty.

    vswhere is asked first, because the installer's own answer is authoritative
    where it can be had.  Inside an AppContainer it cannot: see
    :func:`_probed_installation_path`, which reads the install tree instead.

    Cached at one entry because the answer is a property of the installation:
    one vswhere launch and two registry reads per process rather than per
    command.  The cached mapping is shared, so callers read it and never
    mutate it; a test that changes the fake installation underneath calls
    ``msvc_toolchain_env.cache_clear()``.
    """
    if not is_windows():
        return {}
    roots = _program_files_roots()
    installation = _vswhere_installation_path(roots) or _probed_installation_path(roots)
    vs_root = Path(installation) if installation else None
    include: list[str] = []
    libraries: list[str] = []
    library_paths: list[str] = []
    tools: list[str] = []
    if vs_root is not None:
        msvc = _highest_numeric_child(vs_root / "VC" / "Tools" / "MSVC")
        if msvc is not None:
            include += _existing_directories(msvc / "include")
            libraries += _existing_directories(msvc / "lib" / "x64")
            library_paths += _existing_directories(msvc / "lib" / "x64")
            tools += _existing_directories(msvc / "bin" / "Hostx64" / "x64")
    kit = _kit_include_version()
    if kit is not None:
        kit_root, version = kit
        include += _existing_directories(
            *(version / leaf for leaf in _KIT_INCLUDE_LEAVES)
        )
        libraries += _existing_directories(
            kit_root / "Lib" / version.name / "ucrt" / "x64",
            kit_root / "Lib" / version.name / "um" / "x64",
        )
        library_paths += _existing_directories(
            kit_root / "UnionMetadata" / version.name,
            kit_root / "References" / version.name,
        )
        tools += _existing_directories(kit_root / "bin" / version.name / "x64")
    if vs_root is not None:
        tools += _existing_directories(
            vs_root / "VC" / "Tools" / "Llvm" / "x64" / "bin",
            Path(vs_root, *_CMAKE_RELATIVE, "CMake", "bin"),
            Path(vs_root, *_CMAKE_RELATIVE, "Ninja"),
        )
    derived = (
        ("INCLUDE", include),
        ("LIB", libraries),
        ("LIBPATH", library_paths),
        ("PATH", tools),
    )
    return {
        name: os.pathsep.join(entries) for name, entries in derived if entries
    }


def registry_path_entries() -> list[str]:
    """``Path`` as the registry holds it right now, user entries first.

    Deliberately uncached, and deliberately not ``os.environ["PATH"]``: VS
    Code captured its environment when the window opened, so every process it
    spawned inherits a ``PATH`` frozen at that moment and cannot see a tool
    installed since -- the measured symptom was ``WinError 5``.  Two read-only
    key reads per call are what make a fresh install usable without restarting
    the editor.  ``REG_EXPAND_SZ`` values are expanded through ``ntpath``,
    whose ``%VAR%`` syntax is the one the registry actually stores.
    """
    if not is_windows():
        return []
    entries: list[str] = []
    for hive_name, key in (
        ("HKEY_CURRENT_USER", _USER_ENVIRONMENT_KEY),
        ("HKEY_LOCAL_MACHINE", _MACHINE_ENVIRONMENT_KEY),
    ):
        raw, expandable = _registry_text(hive_name, key, _PATH_VALUE)
        entries += _split_path(ntpath.expandvars(raw) if expandable else raw)
    return entries


def _appended_path(current: str, additions: Iterable[str]) -> str | None:
    """``current`` plus every addition it does not already hold, or ``None``.

    ``None`` means nothing was missing, which is how :func:`build_tool_env`
    leaves a sufficient ``PATH`` byte-identical instead of rewriting it into
    this module's own normalised spelling.
    """
    kept = _split_path(current)
    declared = len(kept)
    seen = {_path_key(entry) for entry in kept}
    for candidate in additions:
        key = _path_key(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(candidate)
    return os.pathsep.join(kept) if len(kept) > declared else None


def build_tool_env(env: Mapping[str, str], *, appcontainer: bool) -> dict[str, str]:
    """The overlay that makes a C/C++ build runnable here, caller values intact.

    Only the keys that change are returned, so a caller applies it with
    ``env.update(...)`` and the recorded environment shows exactly what the
    lane added.  ``INCLUDE``, ``LIB`` and ``LIBPATH`` appear only where the
    caller set none.  ``PATH`` keeps the caller's own entries in the caller's
    own order with the missing tool directories and then the missing registry
    entries appended behind them, deduped case-insensitively, and is omitted
    entirely when nothing was missing.  Two defaults belong to the AppContainer
    alone, and both yield to a caller that declared one: ``CMAKE_GENERATOR`` is
    NMake because ninja cannot run there at all (see :func:`ninja_pipe_denied`),
    and ``_CL_`` carries ``/Z7`` because the PDB server ``/Zi`` needs cannot be
    reached there (see :data:`_APPCONTAINER_CL_APPENDED_OPTIONS`).
    """
    if not is_windows():
        return {}
    toolchain = msvc_toolchain_env()
    overlay = {
        name: toolchain[name]
        for name in _CONTRIBUTED_ROOT_VARIABLES
        if toolchain.get(name) and not (env.get(name) or "").strip()
    }
    appended = _appended_path(
        env.get("PATH") or "",
        (*_split_path(toolchain.get("PATH") or ""), *registry_path_entries()),
    )
    if appended is not None:
        overlay["PATH"] = appended
    if appcontainer and not (env.get("CMAKE_GENERATOR") or "").strip():
        overlay["CMAKE_GENERATOR"] = _APPCONTAINER_CMAKE_GENERATOR
    if appcontainer and not (env.get("_CL_") or "").strip():
        overlay["_CL_"] = _APPCONTAINER_CL_APPENDED_OPTIONS
    return overlay


def ninja_pipe_denied(stdout: str, stderr: str) -> bool:
    r"""True for ninja's AppContainer named-pipe refusal, and nothing else.

    ``ninja: fatal: CreateNamedPipe: Access is denied`` means ninja could not
    create its global ``\\.\pipe\ninja_pid<pid>``, which an AppContainer may
    never do.  No build step ran and no candidate code was judged, so the lane
    that sees this must report an environment restriction rather than a failed
    gate.  Not gated on Windows: this reads text, and the text is the evidence.
    """
    text = f"{stdout or ''}\n{stderr or ''}".casefold()
    return all(token in text for token in _NINJA_PIPE_DENIAL_TOKENS)
