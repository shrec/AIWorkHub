"""Card ``read_roots`` on Linux Landlock and enforced card ``deny_read`` (NF-2026-02238).

Extends NF-2026-01499 / NF-2026-01857 (``card_read_roots``), which granted
read roots on Windows AppContainer only.  Every mechanism here is enforced by
the kernel or by the NTFS DACL; none of it is prompt prose.

``deny_read``
    Up to ``MAX_DENY_READ`` repo-style POSIX globs (``*``, ``?``, ``[...]``,
    ``**`` segments).  A glob without ``/`` matches a basename at any depth; a
    glob that matches a directory denies every file beneath it.  A glob that
    does not compile (``[z-a]``) is refused at create and at launch with
    ``deny_read_glob_invalid:<glob>``.  On Windows (``os.name == 'nt'``) globs
    match case-insensitively, as NTFS resolves names.  Matching regular files
    (and the regular-file target of a matching symlink) are collected from the
    worktree, each read root and, on Landlock, the canonical repository (reads
    there are otherwise unrestricted).  ``.git`` directories are not descended
    unless a glob names them.  A directory symlink (or junction) whose
    resolved target lies inside one of those roots (on AppContainer: inside
    the worktree) is followed, and its files are denied by realpath; there is
    no global visited set, so a glob matching only through a second link name
    still applies.  A link whose target is already an ancestor on the current
    descent chain (a cycle) is not descended; when a glob could match beneath
    it the launch is refused with ``deny_read_cycle_link:<relative link
    path>``.  Landlock and the DACL act on resolved targets, so a *link path*
    can never be denied.  A directory link resolving outside is therefore not
    followed: when the backend lets the worker read the target (Landlock:
    always, the read layer leaves every unsplit path readable; AppContainer:
    always, no classification) and a glob could match beneath the link, the
    launch is refused with ``deny_read_unenforceable_link:<relative link
    path>``; a ``granted`` scan instead lists a link outside every grant as
    ``outside_link_unreachable``.  The scan is bounded by ``MAX_SCAN_ENTRIES``
    entries (links reached twice are scanned twice and count twice) and
    ``MAX_DENIED_FILES`` hits; past either the launch is refused with
    ``deny_read_refused:<reason>:<limit>``.

    Declared limitation: ``deny_read`` hides only working-tree bytes; tracked
    content stays readable via ``.git`` objects (the receipt says so with
    ``git_object_store_readable: true``).

Linux Landlock
    The worker's existing ruleset (``worker_workspace._apply_landlock``)
    handles only mutation rights, so a read root is already readable and,
    having no write rule, not writable.  This module adds one more *stacked*
    Landlock layer that handles ``READ_FILE | READ_DIR`` and nothing else (never
    EXECUTE, never a write right); Landlock requires every layer to allow an
    access, so the inner write-only layer still applies unchanged.

    * ``read_roots`` declared (NF-2026-01004, the read *allow-list*): nothing
      outside :func:`read_allow_set` is readable -- the worktree, the card read
      roots, the request runtime roots the sandbox argv names (home, worker
      temp, exec scratch, short TMPDIR), the git read surface (the common
      dir's objects, refs, packed-refs, HEAD, config, info, commondir, shallow
      and worktrees plus this worktree's own ``worktrees/<name>``; never
      hooks, logs or the checkout root, so sibling checkouts and
      ``.aiworkhub/runtime`` / ``.aiworkhub/temp`` stay unreadable), the fixed
      system set ``SYSTEM_READ_ROOTS`` and the exact toolchain install roots
      (python prefixes, this package's import root, the adapter's node version
      root).  A toolchain or request root that is the filesystem root or an
      ancestor of (or equal to) the account home, the repository or the
      worktree is dropped, never widened.  The set is computed here in the
      supervisor and written into the plan; the child only applies the plan.
      Each allow root gets ``READ_FILE | READ_DIR`` (a file root ``READ_FILE``);
      one containing a denied file is split as below, starting at that root.
    * ``deny_read`` only, no denied file: the filesystem root gets
      ``READ_FILE | READ_DIR``.
    * ``deny_read`` only, denied files: the filesystem root gets ``READ_DIR``
      only, and every directory on the path from the root to a denied file is
      *split*: each of its children that is neither a denied file nor itself
      split gets a ``READ_FILE`` rule for its whole subtree, symlinks are
      skipped (Landlock checks the resolved target).  A split needing more than
      ``MAX_LAYER_RULES`` rules refuses the launch with
      ``deny_read_refused:rule_cap_exceeded``.

    The layer is applied by running this file as a direct script in front of
    the sandbox argv (``--plan <json> -- <argv>``), so the worker, its MCP
    server and any validation the worker starts inherit it.  The coordinator's
    own canonical validation after exit runs outside it.  Fail-closed limits:
    an entry created after launch directly inside a split directory is
    unreadable to the worker, and hard links to a denied file elsewhere are not
    discovered.  Landlock unavailable with a non-empty ``read_roots`` or
    ``deny_read`` refuses with ``read_roots_landlock_unavailable`` /
    ``deny_read_landlock_unavailable`` (:func:`check_backend`); at launch
    :func:`check_read_allowlist` turns every such refusal into
    ``read_allowlist_unenforceable:<cause>:owner_opt_out=<env>=1``, and only
    the owner's ``READ_ALLOWLIST_OPT_OUT_ENV=1`` launches it unenforced (the
    receipt then says ``owner_opt_out_unenforced``).

Windows AppContainer
    Read roots keep their ``read_execute`` grants.  Each denied file inside
    this launch's own isolated worktree receives one explicit deny ACE for the
    container SID (``icacls <file> /deny *<SID>:(R)``), written by the
    supervisor before the worker starts; an explicit deny precedes every allow
    ACE.  The worktree is per launch and deleted with it, so no ACE is ever
    removed and nothing is shared between launches.  Before an ACE is written
    the path is resolved (``realpath``) and must stay under the worktree; any
    path escaping it refuses the launch.  A deny glob matching a file under a
    *shared* read root (outside the worktree) refuses the launch with
    ``deny_read_shared_read_root_unsupported:<path relative to the root>``.
    Directory links are not classified by reachability here: every link
    resolving outside the worktree counts as readable, so one beneath which a
    glob could match refuses with ``deny_read_unenforceable_link:<rel>``.

Linux bubblewrap
    ``read_roots`` (NF-2026-01004): only the card's read roots (never the
    Landlock system, toolchain, git or package roots) not already presented by
    the bubblewrap argv are bound ``--ro-bind <root> <root>``, inserted right
    after the base mounts and before every original worktree, write or
    protective op, which keep their order and precedence; no blanket host
    bind.  Bubblewrap cannot hide a file
    inside a bound root, so ``deny_read`` there is refused (owner opt-out
    aside, recorded ``owner_opt_out_unenforced``).

Any other backend with ``read_roots`` or ``deny_read`` is refused.

This module has no package-level relative imports outside the guarded
``linux_kernel_abi`` import, so the supervisor and the read-layer entrypoint
can run it as a direct script.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

try:
    from . import platform_io
except ImportError:  # direct-script entrypoint (read layer)
    import platform_io  # type: ignore[no-redef]

try:
    from .linux_kernel_abi import (
        LANDLOCK_ACCESS_FS_READ_DIR,
        LANDLOCK_ACCESS_FS_READ_FILE,
        LANDLOCK_CREATE_RULESET_VERSION,
        LANDLOCK_RULE_PATH_BENEATH,
        PR_SET_NO_NEW_PRIVS,
        SYS_LANDLOCK_ADD_RULE,
        SYS_LANDLOCK_CREATE_RULESET,
        SYS_LANDLOCK_RESTRICT_SELF,
        _LandlockPathBeneathAttr,
        _LandlockRulesetAttr,
    )
except ImportError:  # direct-script entrypoint (read layer, worker_supervisor)
    from linux_kernel_abi import (  # type: ignore[no-redef]
        LANDLOCK_ACCESS_FS_READ_DIR,
        LANDLOCK_ACCESS_FS_READ_FILE,
        LANDLOCK_CREATE_RULESET_VERSION,
        LANDLOCK_RULE_PATH_BENEATH,
        PR_SET_NO_NEW_PRIVS,
        SYS_LANDLOCK_ADD_RULE,
        SYS_LANDLOCK_CREATE_RULESET,
        SYS_LANDLOCK_RESTRICT_SELF,
        _LandlockPathBeneathAttr,
        _LandlockRulesetAttr,
    )

MAX_DENY_READ = 32
MAX_DENY_GLOB_CHARS = 256
MAX_SCAN_ENTRIES = 200_000
MAX_DENIED_FILES = 1024
MAX_LAYER_RULES = 4096
MAX_PLAN_BYTES = 8 * 1024 * 1024
READ_LAYER_HANDLED = LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR
PLAN_SCHEMA_ID = "aiworkhub.linux_read_layer_plan.v1"
VISIBILITY_SCHEMA_ID = "aiworkhub.sandbox_visibility.v1"
_ECHO_CHARS = 240
_GLOB_CHARS = frozenset("*?[")
_APPCONTAINER_SID = re.compile(r"S-1-15-2(?:-\d{1,10}){1,15}")
_APPCONTAINER_DENY_TIMEOUT_SECONDS = 30.0
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # a Windows junction (stat module, nt only)
# NF-2026-01004: host system roots every Landlock worker, its MCP server and its
# validation read; a host data mount (/mnt, /srv, /data, /media, /home/...) is
# never listed.  Symlinked entries (/bin -> usr/bin) collapse onto /usr.
SYSTEM_READ_ROOTS: tuple[str, ...] = (
    "/usr",
    "/etc",
    "/proc",
    "/dev",
    "/sys",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/run/systemd/resolve",
    "/run/resolvconf",
    "/nix/store",
)
# The owner's explicit opt-out for a card read surface no backend can enforce.
READ_ALLOWLIST_OPT_OUT_ENV = "AIWORKHUB_UNENFORCED_READ_ALLOWLIST_OPT_OUT"
# worker_workspace.sandbox_argv ``--landlock-exec`` flags naming request roots,
# the parser's other value flags, and its switches (no value).  The argv is read
# positionally against these, so a value can never be taken for a flag.
_LANDLOCK_EXEC_ROOT_FLAGS = frozenset(
    {"--workspace", "--home", "--worker-temp", "--exec-scratch", "--short-tmp"}
)
_LANDLOCK_EXEC_VALUE_FLAGS = _LANDLOCK_EXEC_ROOT_FLAGS | frozenset(
    {"--allow", "--cwd", "--semlock-dev-shm", "--worker-semlock-dev-shm"}
)
_LANDLOCK_EXEC_SWITCHES = frozenset({"--outer-validation-authority", "--noop-chmod"})
# The git common-dir entries a linked worktree opens on every command; never
# hooks or logs (this worktree's own gitdir is added apart).  Landlock does not
# mediate access(2), so an unreadable ``config`` passes git's access_or_die and
# then fails fopen with EACCES: config, info, commondir, shallow and worktrees
# are read surface too.
_GIT_COMMON_READ_ENTRIES: tuple[str, ...] = (
    "objects", "refs", "packed-refs", "HEAD",
    "config", "info", "commondir", "shallow", "worktrees",
)
# bubblewrap options worker_workspace.sandbox_argv can emit, by argument count;
# any other option refuses (the argv is never guessed at).
_BWRAP_OPTION_ARITY: dict[str, int] = {
    **dict.fromkeys(
        (
            "--new-session", "--die-with-parent", "--unshare-all", "--unshare-pid",
            "--unshare-ipc", "--unshare-uts", "--unshare-net", "--unshare-user",
            "--unshare-cgroup", "--share-net", "--clearenv", "--as-pid-1",
        ),
        0,
    ),
    **dict.fromkeys(
        (
            "--chdir", "--dir", "--proc", "--dev", "--tmpfs", "--mqueue",
            "--remount-ro", "--unsetenv", "--hostname", "--perms", "--uid", "--gid",
            "--cap-add", "--cap-drop",
        ),
        1,
    ),
    **dict.fromkeys(
        (
            "--bind", "--bind-try", "--ro-bind", "--ro-bind-try", "--dev-bind",
            "--dev-bind-try", "--symlink", "--setenv", "--chmod", "--file",
            "--bind-data", "--ro-bind-data",
        ),
        2,
    ),
}
# Paths the bubblewrap argv already presents (host ro-binds of /usr and /etc,
# its own /proc and /dev, the /bin-style symlinks into /usr): an allow root
# beneath one is never bound again, so a host /proc or /dev never replaces them.
_BWRAP_BASE_MOUNTS: tuple[str, ...] = (
    "/usr", "/etc", "/proc", "/dev", "/bin", "/sbin", "/lib", "/lib64",
)


class SandboxVisibilityError(ValueError):
    """A typed refusal; ``str()`` is the stable ``<code>[:<detail>]`` text."""

    def __init__(self, code: str, detail: object = "") -> None:
        self.code = code
        text = str(detail)[:_ECHO_CHARS]
        super().__init__(f"{code}:{text}" if text else code)


def _invalid(reason: str, detail: object) -> SandboxVisibilityError:
    return SandboxVisibilityError(f"deny_read_invalid:{reason}", detail)


def _refused(reason: str, detail: object) -> SandboxVisibilityError:
    return SandboxVisibilityError(f"deny_read_refused:{reason}", detail)


# --------------------------------------------------------------------------
# deny_read globs
# --------------------------------------------------------------------------


def validate_deny_read(raw: Any) -> list[str]:
    """Deduplicated deny globs; ``None`` and ``[]`` are the no-deny case.

    Raises ``SandboxVisibilityError('deny_read_invalid:<reason>:<detail>')``.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _invalid("not_a_list", type(raw).__name__)
    if len(raw) > MAX_DENY_READ:
        raise _invalid("too_many", f"{len(raw)}>{MAX_DENY_READ}")
    globs: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise _invalid("not_a_string", type(item).__name__)
        glob = item.strip()
        if not glob:
            raise _invalid("empty", "")
        if len(glob) > MAX_DENY_GLOB_CHARS:
            raise _invalid("too_long", f"{len(glob)}>{MAX_DENY_GLOB_CHARS}")
        if "\x00" in glob:
            raise _invalid("nul_character", glob.replace("\x00", "\\x00"))
        if "\\" in glob:
            raise _invalid("backslash", glob)
        if glob.startswith("/") or re.match(r"[A-Za-z]:", glob):
            raise _invalid("not_relative", glob)
        if any(part in ("", ".", "..") for part in glob.split("/")):
            raise _invalid("bad_component", glob)
        try:
            glob_regex(glob)
        except re.error as exc:
            raise SandboxVisibilityError("deny_read_glob_invalid", glob) from exc
        if glob not in globs:
            globs.append(glob)
    return globs


def validate_read_surface(
    read_roots: Any, deny_read: Any, repo_root: str | os.PathLike[str]
) -> tuple[list[str], list[str]]:
    """Create-time validation of both card read fields (core's one call site)."""
    from .card_read_roots import validate_read_roots

    return validate_read_roots(read_roots, repo_root=repo_root), validate_deny_read(
        deny_read
    )


def read_surface_fields(
    read_roots: Sequence[str], deny_read: Sequence[str]
) -> dict[str, list[str]]:
    """The non-empty card fields; a card declaring neither stays unchanged."""
    fields = (("read_roots", read_roots), ("deny_read", deny_read))
    return {key: list(value) for key, value in fields if value}


def _segment_regex(segment: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            close = segment.find("]", index + 2)
            if close < 0:
                out.append(re.escape(char))
            else:
                body = segment[index + 1 : close].replace("\\", "\\\\")
                if body.startswith("!"):
                    out.append("[^/" + body[1:] + "]")
                else:
                    out.append("[" + ("\\" + body if body.startswith("^") else body) + "]")
                index = close
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


def _glob_flags() -> int:
    """Case-insensitive on Windows, where ``.ENV`` and ``.env`` name one file."""
    return re.DOTALL | (re.IGNORECASE if platform_io.is_windows() else 0)


def _glob_parts(glob: str) -> list[str]:
    """``/`` segments; a glob without ``/`` matches a basename at any depth."""
    parts = glob.split("/")
    return ["**", parts[0]] if len(parts) == 1 else parts


def glob_regex(glob: str) -> re.Pattern[str]:
    """Compile one deny glob for ``fullmatch`` on a ``/``-separated relative path.

    Case-insensitive on Windows (:func:`_glob_flags`).
    Raises ``re.error`` for an uncompilable class; ``validate_deny_read``
    turns that into ``deny_read_glob_invalid``.
    """
    parts = _glob_parts(glob)
    regex = ""
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == "**":
            regex += ".*" if last else "(?:[^/]+/)*"
        else:
            regex += _segment_regex(part) + ("" if last else "/")
    return re.compile(regex, _glob_flags())


def _static_base(glob: str) -> str:
    parts = glob.split("/")
    base: list[str] = []
    for part in parts[:-1]:
        if part == "**" or any(char in _GLOB_CHARS for char in part):
            break
        base.append(part)
    return "/".join(base)


def _scan_bases(globs: Sequence[str]) -> list[str]:
    bases = sorted({_static_base(glob) for glob in globs})
    if "" in bases:
        return [""]
    kept: list[str] = []
    for base in bases:  # sorted: a parent sorts before its children
        if not any(base == other or base.startswith(other + "/") for other in kept):
            kept.append(base)
    return kept


def _entry_is_link(entry: os.DirEntry[str]) -> bool:
    """A symlink, or on Windows a junction (``is_symlink`` misses those).

    The junction test reads the reparse tag scandir already cached, so it
    costs no extra system call and needs no ``os.path.isjunction`` (3.12+).
    """
    if entry.is_symlink():
        return True
    if not platform_io.is_windows():
        return False
    tag = getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0)
    return bool(tag == _IO_REPARSE_TAG_MOUNT_POINT)


def _identity(path: str) -> tuple[int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino) if info.st_ino else None


def _under_root(path: str, roots: Iterable[str]) -> str | None:
    """``path`` (already resolved) spelled beneath the root holding it, or None.

    On Windows one directory can resolve to two spellings (a mapped or subst
    drive versus its volume path, e.g. through a junction), so a failed string
    test falls back to file identity: ``path`` is inside when one of its
    ancestors *is* a root, and it is re-spelled under that root so one file
    has one name.  Unknown identity is never inside.
    """
    key = os.path.normcase(path)
    root_list = list(roots)
    for root in root_list:
        base = os.path.normcase(root)
        if key == base or key.startswith(base.rstrip("\\/") + os.sep):
            return path
    if not platform_io.is_windows():
        return None
    identities: dict[tuple[int, int], str] = {}
    for root in root_list:
        ident = _identity(root)
        if ident is not None:
            identities.setdefault(ident, root)
    probe = path
    rest: list[str] = []
    while identities:
        ident = _identity(probe)
        if ident is not None and ident in identities:
            return os.path.join(identities[ident], *reversed(rest))
        parent, name = os.path.split(probe)
        if parent == probe or not name:
            break
        rest.append(name)
        probe = parent
    return None


def contained_paths(
    paths: Iterable[str], roots: Iterable[str | os.PathLike[str]]
) -> tuple[list[str], list[str]]:
    """``(resolved paths under a root, refused paths)``.

    Every path is resolved with ``realpath``; one whose result escapes every
    root (a symlink or junction leaf pointing elsewhere) is refused, never
    followed.  Only the first list may drive a DACL write.
    """
    real_roots = [os.path.realpath(os.fspath(root)) for root in roots]
    contained: dict[str, None] = {}
    refused: list[str] = []
    for path in paths:
        inside = _under_root(os.path.realpath(path), real_roots)
        if inside is not None:
            contained.setdefault(inside, None)
        else:
            refused.append(path)
    return list(contained), refused


def _could_match_beneath(globs: Sequence[str], rel: str) -> bool:
    """Whether some glob can match a path strictly beneath directory ``rel``.

    True as well when a glob already matches ``rel`` or one of its ancestors
    (everything beneath a matched directory is denied).  Memoised per glob, so
    ``**`` runs stay linear in segments times names.
    """
    names = rel.split("/")
    for glob in globs:
        segments = [
            None if part == "**" else re.compile(_segment_regex(part), _glob_flags())
            for part in _glob_parts(glob)
        ]
        memo: dict[tuple[int, int], bool] = {}

        def extend(seg: int, name: int) -> bool:
            if seg == len(segments) or name == len(names):
                return True
            key = (seg, name)
            if key not in memo:
                pattern = segments[seg]
                if pattern is None:
                    memo[key] = extend(seg + 1, name) or extend(seg, name + 1)
                else:
                    memo[key] = bool(pattern.fullmatch(names[name])) and extend(
                        seg + 1, name + 1
                    )
            return memo[key]

        if extend(0, 0):
            return True
    return False


class _Scan:
    """One bounded deny_read scan over a fixed set of resolved roots."""

    def __init__(
        self,
        patterns: Sequence[re.Pattern[str]],
        globs: Sequence[str],
        roots: Sequence[str],
        granted: Sequence[str] | None,
        max_entries: int,
        max_files: int,
    ) -> None:
        self.patterns = patterns
        self.globs = globs
        self.roots = roots
        self.granted = granted
        self.budget = max_entries
        self.max_entries = max_entries
        self.max_files = max_files
        self.found: dict[str, None] = {}
        self.unreachable: dict[str, None] = {}
        self.link_roots: Sequence[str] = roots

    def add(self, path: str) -> None:
        self.found.setdefault(path, None)
        if len(self.found) > self.max_files:
            raise _refused("too_many_denied_files", f">{self.max_files}")

    def resolved(self, path: str) -> str:
        """``realpath``, spelled under its root when inside one."""
        real = os.path.realpath(path)
        return _under_root(real, self.roots) or real

    def follow(
        self, root: str, link: str, hit: bool, ancestors: frozenset[str]
    ) -> str | None:
        """The key to descend a directory reached through ``link``, or None.

        Inside ``link_roots``: followed unless its target is already an
        ancestor on this descent chain (a cycle); a cycle under which a glob
        could match is refused with ``deny_read_cycle_link:<rel>``, never
        silently skipped.  There is no global visited set, so a second link to
        one directory is scanned under its own name.  Outside: when the worker
        can read the target (beneath ``granted``; ``None`` means everything)
        and a glob could match under the link, the launch is refused with
        ``deny_read_unenforceable_link:<rel>`` -- Landlock and the DACL act on
        the resolved target, so a link path cannot be denied.  Otherwise it is
        recorded as ``outside_link_unreachable``.
        """
        real = os.path.realpath(link)
        rel = os.path.relpath(link, root).replace(os.sep, "/")
        target = _under_root(real, self.link_roots)
        if target is None:
            if self.granted is not None and _under_root(real, self.granted) is None:
                self.unreachable.setdefault(rel, None)
            elif hit or _could_match_beneath(self.globs, rel):
                raise SandboxVisibilityError("deny_read_unenforceable_link", rel)
            return None
        key = os.path.normcase(target)
        if key not in ancestors:
            return key
        if hit or _could_match_beneath(self.globs, rel):
            raise SandboxVisibilityError("deny_read_cycle_link", rel)
        return None

    def walk(self, root: str, start: str) -> None:
        patterns = self.patterns

        def matches(path: str) -> bool:
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            return any(pattern.fullmatch(rel) for pattern in patterns)

        start_hit = start != root and matches(start)
        key: str | None = os.path.normcase(os.path.normpath(start))
        chain = frozenset({os.path.normcase(root)})
        # A static scan base reached through a link is checked like any link.
        if os.path.normcase(os.path.realpath(start)) != key:
            key = self.follow(root, start, start_hit, chain)
            if key is None:
                return
        stack: list[tuple[str, str, bool, frozenset[str]]] = [
            (start, key, start_hit, chain | {key})
        ]
        while stack:
            directory, real_key, inherited, ancestors = stack.pop()
            try:
                with os.scandir(directory) as iterator:
                    entries = list(iterator)
            except OSError as exc:
                raise _refused("unlistable_directory", directory) from exc
            for entry in entries:
                self.budget -= 1
                if self.budget < 0:
                    raise _refused("scan_cap_exceeded", f">{self.max_entries}")
                hit = inherited or matches(entry.path)
                try:
                    if _entry_is_link(entry):
                        if os.path.isdir(entry.path):
                            if hit or entry.name != ".git":
                                followed = self.follow(root, entry.path, hit, ancestors)
                                if followed is not None:
                                    stack.append(
                                        (entry.path, followed, hit, ancestors | {followed})
                                    )
                            continue
                        target = self.resolved(entry.path)
                        if not hit or not os.path.isfile(target):
                            continue
                    elif entry.is_dir(follow_symlinks=False):
                        if hit or entry.name != ".git":
                            child = os.path.join(real_key, os.path.normcase(entry.name))
                            stack.append((entry.path, child, hit, ancestors | {child}))
                        continue
                    elif hit and entry.is_file(follow_symlinks=False):
                        target = self.resolved(entry.path)
                    else:
                        continue
                except OSError as exc:
                    raise _refused("unreadable_entry", entry.path) from exc
                self.add(target)


def scan_deny_read(
    roots: Iterable[str | os.PathLike[str]],
    globs: Sequence[str],
    *,
    granted: Iterable[str | os.PathLike[str]] | None = None,
    link_roots: Iterable[str | os.PathLike[str]] | None = None,
    max_entries: int = MAX_SCAN_ENTRIES,
    max_files: int = MAX_DENIED_FILES,
) -> tuple[list[str], list[str]]:
    """``(denied real paths, outside_link_unreachable relative link paths)``.

    ``granted`` lists the trees the worker can read beyond ``roots``; the
    default ``None`` (fail closed) treats every path as readable, so a
    directory link leaving the roots with a glob that could match beneath it
    is refused instead of recorded.  ``link_roots`` (default ``roots``) are
    the trees a directory link may be followed into; AppContainer passes only
    the worktree, so every link resolving outside it is an outside link.
    """
    if not globs:
        return [], []
    # Re-validated: a stored card with a bad glob is a typed refusal here too.
    valid = validate_deny_read(list(globs))
    patterns = [glob_regex(glob) for glob in valid]
    real_roots = list(dict.fromkeys(os.path.realpath(os.fspath(r)) for r in roots))
    real_granted = (
        None if granted is None else [os.path.realpath(os.fspath(g)) for g in granted]
    )
    real_links = (
        real_roots
        if link_roots is None
        else [os.path.realpath(os.fspath(r)) for r in link_roots]
    )
    scan = _Scan(patterns, valid, real_roots, real_granted, max_entries, max_files)
    scan.link_roots = real_links
    for root in real_roots:
        if not os.path.isdir(root):
            raise _refused("root_not_a_directory", root)
        for base in _scan_bases(valid):
            start = os.path.join(root, *base.split("/")) if base else root
            if os.path.isdir(start):
                scan.walk(root, start)
    return sorted(scan.found), sorted(scan.unreachable)


def denied_files(
    roots: Iterable[str | os.PathLike[str]],
    globs: Sequence[str],
    *,
    granted: Iterable[str | os.PathLike[str]] | None = None,
    max_entries: int = MAX_SCAN_ENTRIES,
    max_files: int = MAX_DENIED_FILES,
) -> list[str]:
    """Real paths of every regular file a deny glob matches under ``roots``."""
    return scan_deny_read(
        roots, globs, granted=granted, max_entries=max_entries, max_files=max_files
    )[0]


# --------------------------------------------------------------------------
# Landlock read layer
# --------------------------------------------------------------------------


def _top(path: str) -> str:
    while True:
        parent = os.path.dirname(path)
        if parent == path:
            return path
        path = parent


def _list_dir(directory: str) -> list[os.DirEntry[str]]:
    """One directory's entries, sorted by name; ``OSError`` propagates."""
    with os.scandir(directory) as iterator:
        return sorted(iterator, key=lambda entry: entry.name)


def _split_dirs(denied: Sequence[str]) -> set[str]:
    """Every directory on the path from the filesystem root to a denied file."""
    split: set[str] = set()
    for path in denied:
        parent = os.path.dirname(path)
        while parent not in split:
            split.add(parent)
            above = os.path.dirname(parent)
            if above == parent:
                break
            parent = above
    return split


def _allow_list_rules(
    roots: Sequence[str],
    split: set[str],
    denied_set: set[str],
    add: Callable[[str, int], None],
) -> None:
    """NF-2026-01004: rules granting reads beneath ``roots`` only.

    A root holding no denied file gets one rule (``READ_FILE`` alone for a
    file root: Landlock refuses ``READ_DIR`` on a non-directory).  A root
    holding one is split from that root down: each split directory gets
    ``READ_DIR`` and each child that is neither split nor denied nor a symlink
    gets its whole subtree.  Nothing above a root is ever granted.
    """
    full = LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR
    granted: set[tuple[str, int]] = set()
    visited: set[str] = set()

    def grant(path: str, rights: int) -> None:
        if (path, rights) not in granted:
            granted.add((path, rights))
            add(path, rights)

    for root in sorted(dict.fromkeys(os.path.realpath(r) for r in roots)):
        if root in denied_set:
            continue
        if root not in split:
            grant(root, full if os.path.isdir(root) else LANDLOCK_ACCESS_FS_READ_FILE)
            continue
        prefix = root.rstrip("\\/") + os.sep
        for directory in sorted(d for d in split if d == root or d.startswith(prefix)):
            if directory in visited:
                continue
            visited.add(directory)
            grant(directory, LANDLOCK_ACCESS_FS_READ_DIR)
            try:
                children = _list_dir(directory)
            except OSError as exc:
                raise _refused("unlistable_directory", directory) from exc
            for child in children:
                if child.path in split or child.path in denied_set or child.is_symlink():
                    continue
                is_dir = child.is_dir(follow_symlinks=False)
                grant(child.path, full if is_dir else LANDLOCK_ACCESS_FS_READ_FILE)


def plan_read_layer(
    *,
    read_roots: Sequence[str],
    denied: Sequence[str],
    max_rules: int | None = None,
    allow_roots: Sequence[str] | None = None,
) -> list[tuple[str, int]]:
    """``(path, rights)`` path-beneath rules for the stacked read layer.

    No rule grants ``READ_FILE`` on a denied file or on any directory above
    one; see the module docstring for the split.  ``max_rules`` defaults to
    ``MAX_LAYER_RULES``; an unlistable split directory refuses (never skips).
    ``allow_roots`` (NF-2026-01004, from :func:`read_allow_set`) selects the
    allow-list layer: only those roots and ``read_roots`` are readable and the
    filesystem root gets no rule; ``None`` keeps the deny-only layer.
    """
    cap = MAX_LAYER_RULES if max_rules is None else max_rules
    full = LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR
    denied_set = set(denied)
    split = _split_dirs(denied)
    rules: list[tuple[str, int]] = []

    def add(path: str, rights: int) -> None:
        rules.append((path, rights))
        if len(rules) > cap:
            raise _refused("rule_cap_exceeded", f">{cap}")

    if allow_roots is not None:
        _allow_list_rules([*allow_roots, *read_roots], split, denied_set, add)
        if not rules:
            raise SandboxVisibilityError("read_allowlist_refused:empty_allow_set")
        return rules
    tops = sorted({_top(path) for path in denied}) or [_top(os.path.abspath(os.sep))]
    rules.extend((top, LANDLOCK_ACCESS_FS_READ_DIR if denied else full) for top in tops)
    for directory in sorted(split):
        try:
            children = _list_dir(directory)
        except OSError as exc:
            raise _refused("unlistable_directory", directory) from exc
        for child in children:
            if child.path in split or child.path in denied_set:
                continue
            if child.is_symlink():
                continue
            add(child.path, LANDLOCK_ACCESS_FS_READ_FILE)
    for root in read_roots:
        real = os.path.realpath(root)
        if real not in split:
            add(real, full)
    return rules


def plan_document(rules: Sequence[tuple[str, int]]) -> dict[str, Any]:
    return {
        "schema_id": PLAN_SCHEMA_ID,
        "handled": READ_LAYER_HANDLED,
        "rules": [[path, rights] for path, rights in rules],
    }


def load_plan(path: str | os.PathLike[str]) -> list[tuple[str, int]]:
    """Read and validate one plan written by :func:`plan_document`."""
    with open(path, "rb") as handle:
        raw = handle.read(MAX_PLAN_BYTES + 1)
    if len(raw) > MAX_PLAN_BYTES:
        raise SandboxVisibilityError("read_layer_failed:plan_too_large", f">{MAX_PLAN_BYTES}")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxVisibilityError("read_layer_failed:plan_not_json") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema_id") != PLAN_SCHEMA_ID
        or document.get("handled") != READ_LAYER_HANDLED
        or not isinstance(document.get("rules"), list)
        or not document["rules"]
        or len(document["rules"]) > MAX_LAYER_RULES + MAX_DENY_READ
    ):
        raise SandboxVisibilityError("read_layer_failed:plan_shape")
    rules: list[tuple[str, int]] = []
    for item in document["rules"]:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not os.path.isabs(item[0])
            or type(item[1]) is not int
            or item[1] <= 0
            or item[1] & ~READ_LAYER_HANDLED
        ):
            raise SandboxVisibilityError("read_layer_failed:plan_rule", item)
        rules.append((item[0], item[1]))
    return rules


def apply_read_layer(rules: Sequence[tuple[str, int]], *, libc: Any = None) -> None:
    """Restrict the calling process with one read-handling Landlock layer."""
    if not platform_io.is_linux():
        raise SandboxVisibilityError("read_layer_failed:not_linux", "non_linux_host")
    libc = libc if libc is not None else ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(
        SYS_LANDLOCK_CREATE_RULESET,
        ctypes.c_void_p(),
        ctypes.c_size_t(0),
        ctypes.c_uint(LANDLOCK_CREATE_RULESET_VERSION),
    )
    if abi < 1:
        raise SandboxVisibilityError("read_layer_failed:landlock_unavailable")
    attr = _LandlockRulesetAttr(READ_LAYER_HANDLED)
    ruleset_fd = libc.syscall(
        SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.sizeof(attr), 0
    )
    if ruleset_fd < 0:
        raise SandboxVisibilityError(
            "read_layer_failed:create_ruleset", os.strerror(ctypes.get_errno())
        )
    try:
        for path, rights in rules:
            try:
                path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
            except FileNotFoundError:
                continue  # gone since planning: granting nothing is narrower
            try:
                rule = _LandlockPathBeneathAttr(rights, path_fd)
                if (
                    libc.syscall(
                        SYS_LANDLOCK_ADD_RULE,
                        ruleset_fd,
                        LANDLOCK_RULE_PATH_BENEATH,
                        ctypes.byref(rule),
                        0,
                    )
                    < 0
                ):
                    raise SandboxVisibilityError(
                        "read_layer_failed:add_rule",
                        f"{path}:{os.strerror(ctypes.get_errno())}",
                    )
            finally:
                os.close(path_fd)
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise SandboxVisibilityError(
                "read_layer_failed:no_new_privs", os.strerror(ctypes.get_errno())
            )
        if libc.syscall(SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) != 0:
            raise SandboxVisibilityError(
                "read_layer_failed:restrict_self", os.strerror(ctypes.get_errno())
            )
    finally:
        os.close(ruleset_fd)


def read_layer_argv(
    plan_path: str | os.PathLike[str],
    worker_argv: Sequence[str],
    *,
    python: str | None = None,
) -> list[str]:
    return [
        python or sys.executable,
        str(Path(__file__).resolve()),
        "--plan",
        os.fspath(plan_path),
        "--",
        *worker_argv,
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """``linux_read_roots.py --plan <json> -- <argv...>``: restrict, then exec."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 4 or args[0] != "--plan" or args[2] != "--":
        print("read_layer_failed:usage", file=sys.stderr)
        return 2
    try:
        apply_read_layer(load_plan(args[1]))
    except (OSError, ValueError) as exc:
        print(str(exc)[:1024], file=sys.stderr)
        return 126
    command = args[3:]
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        print(f"read_layer_failed:exec:{exc.strerror or exc}"[:1024], file=sys.stderr)
        return 127
    return 127  # pragma: no cover - execvp does not return


# --------------------------------------------------------------------------
# Windows AppContainer deny ACE
# --------------------------------------------------------------------------


def _icacls(system_root: str | None) -> str:
    root = system_root or os.environ.get("SystemRoot") or r"C:\Windows"
    return os.path.join(root, "System32", "icacls.exe")


def _checked(path: str, sid: str) -> None:
    if not _APPCONTAINER_SID.fullmatch(sid):
        raise SandboxVisibilityError("deny_read_ace_failed:invalid_sid", sid)
    if not os.path.isabs(path):
        raise SandboxVisibilityError("deny_read_ace_failed:not_absolute", path)


def appcontainer_deny_argv(
    path: str, sid: str, *, system_root: str | None = None
) -> list[str]:
    _checked(path, sid)
    return [_icacls(system_root), path, "/deny", f"*{sid}:(R)", "/Q"]


def _run_icacls(argv: list[str], run: Callable[..., Any], timeout: float) -> Any:
    return run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
        shell=False,
    )


def _write_deny_ace(path: str, sid: str, run: Callable[..., Any], timeout: float) -> None:
    argv = appcontainer_deny_argv(path, sid)
    try:
        result = _run_icacls(argv, run, timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxVisibilityError(
            "deny_read_ace_failed:spawn", f"{type(exc).__name__}:{path}"
        ) from exc
    if result.returncode != 0:
        raise SandboxVisibilityError(f"deny_read_ace_failed:exit={result.returncode}", path)


def apply_appcontainer_deny(
    paths: Sequence[str],
    sid: str,
    *,
    roots: Sequence[str | os.PathLike[str]],
    run: Callable[..., Any] = subprocess.run,
    timeout: float = _APPCONTAINER_DENY_TIMEOUT_SECONDS,
) -> list[str]:
    """Write one explicit deny-read ACE for ``sid`` on every contained path.

    ``roots`` is this launch's own isolated worktree: the ACEs live on its
    per-launch files and disappear with it, so nothing is ever removed.  Each
    path is resolved first and must stay under ``roots``; a path whose
    realpath escapes is never handed to icacls and is returned as refused
    (the supervisor then refuses the launch).
    """
    if len(paths) > MAX_DENIED_FILES:
        raise _refused("too_many_denied_files", f">{MAX_DENIED_FILES}")
    contained, refused = contained_paths(paths, roots)
    for path in contained:
        _write_deny_ace(path, sid, run, timeout)
    return refused


# --------------------------------------------------------------------------
# Coordinator-side launch wiring and the sandbox_visibility receipt
# --------------------------------------------------------------------------


def _default_landlock_abi() -> int:
    from .worker_workspace import landlock_abi_version

    return landlock_abi_version()


def _within(path: str, root: str) -> bool:
    """``path`` is ``root`` or lies beneath it (both already resolved)."""
    key, base = os.path.normcase(path), os.path.normcase(root)
    return key == base or key.startswith(base.rstrip("\\/") + os.sep)


def _account_home() -> str | None:
    home = os.path.expanduser("~")
    return os.path.realpath(home) if os.path.isabs(home) else None


def _landlock_exec_parts(worker_argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """``(request root values, adapter argv)`` of a ``--landlock-exec`` argv.

    The flags are read positionally from index 3 to the ``--`` terminator: a
    value flag consumes the next element, so a value is never read as a flag
    (``--allow --home`` names no root).  Every root value must be non-empty and
    absolute; an unknown flag or a missing value refuses.  Any other argv names
    no request root and is its own adapter argv.
    """
    argv = list(worker_argv)
    if len(argv) < 3 or argv[2] != "--landlock-exec":
        return [], argv
    values: list[str] = []
    index = 3
    while index < len(argv) and argv[index] != "--":
        flag = argv[index]
        if flag in _LANDLOCK_EXEC_SWITCHES:
            index += 1
            continue
        if flag not in _LANDLOCK_EXEC_VALUE_FLAGS:
            raise SandboxVisibilityError("read_allowlist_refused:landlock_argv_unrecognized", flag)
        if index + 1 >= len(argv):
            break
        value = argv[index + 1]
        if flag in _LANDLOCK_EXEC_ROOT_FLAGS:
            if not value or not os.path.isabs(value):
                raise SandboxVisibilityError(
                    "read_allowlist_refused:request_root_invalid", f"{flag}={value!r}"
                )
            values.append(value)
        index += 2
    if index >= len(argv) or argv[index] != "--":
        raise SandboxVisibilityError("read_allowlist_refused:landlock_argv_unterminated")
    return values, argv[index + 1 :]


def _bubblewrap_parts(
    worker_argv: Sequence[str],
) -> tuple[int, list[tuple[str, str, str]], list[str]]:
    """``(terminator index, two-argument ops, adapter argv)`` of a bubblewrap argv.

    Options are read positionally by :data:`_BWRAP_OPTION_ARITY`; an unknown
    option or a missing ``--`` refuses, never guesses.
    """
    argv = list(worker_argv)
    ops: list[tuple[str, str, str]] = []
    index = 1
    while index < len(argv) and argv[index] != "--":
        arity = _BWRAP_OPTION_ARITY.get(argv[index])
        if arity is None or index + arity >= len(argv):
            raise SandboxVisibilityError(
                "read_roots_unsupported_backend", f"bubblewrap_argv_unrecognized:{argv[index]}"
            )
        if arity == 2:
            ops.append((argv[index], argv[index + 1], argv[index + 2]))
        index += 1 + arity
    if len(argv) < 2 or index >= len(argv):
        raise SandboxVisibilityError(
            "read_roots_unsupported_backend", "bubblewrap_argv_unrecognized:unterminated"
        )
    return index, ops, argv[index + 1 :]


def _git_read_roots(worktree: str) -> list[str]:
    """A linked worktree's git read surface, never the common dir whole.

    The common dir's :data:`_GIT_COMMON_READ_ENTRIES` that exist (objects,
    refs, packed-refs, HEAD, config, info, commondir, shallow, worktrees), plus
    ``worktrees/<name>`` -- the gitdir this worktree's ``.git`` file points to
    (it holds ``commondir``, so git still resolves).  Never ``hooks``, ``logs``
    or the checkout.  A ``.git`` directory lies inside the worktree (already
    granted).
    """
    marker = os.path.join(worktree, ".git")
    if not os.path.lexists(marker) or (os.path.isdir(marker) and not os.path.islink(marker)):
        return []
    from .worker_workspace import WorkspaceError, _common_git_dir, _gitdir_pointer

    try:
        common = os.path.realpath(_common_git_dir(Path(worktree)))
        gitdir = os.path.realpath(_gitdir_pointer(Path(marker), label="repository_git_marker"))
    except (WorkspaceError, OSError) as exc:
        raise SandboxVisibilityError("read_allowlist_refused:git_dir_unavailable", exc) from exc
    roots = [
        os.path.join(common, entry)
        for entry in _GIT_COMMON_READ_ENTRIES
        if os.path.lexists(os.path.join(common, entry))
    ]
    if os.path.normcase(gitdir) == os.path.normcase(common):
        return roots
    if os.path.normcase(os.path.dirname(gitdir)) != os.path.normcase(
        os.path.join(common, "worktrees")
    ):
        raise SandboxVisibilityError("read_allowlist_refused:git_dir_unexpected", gitdir)
    return [*roots, gitdir]


def _toolchain_roots(adapter_argv: Sequence[str], *, path: str | None = None) -> list[str]:
    """Exact install roots: python prefixes, this package, the adapter's node root.

    The package directory follows its import root so a dropped import root
    (a flat layout whose parent is the repository) still leaves it readable.
    A PATH-relative adapter resolves with :func:`shutil.which` against ``path``
    (the launch env's ``PATH``), else ``os.environ``'s; unresolved it refuses
    with ``read_allowlist_refused:adapter_unresolved``, never granting nothing.
    """
    package = os.path.dirname(os.path.realpath(__file__))
    roots = [
        sys.prefix,
        sys.base_prefix,
        sys.exec_prefix,
        getattr(sys, "base_exec_prefix", sys.base_prefix),
        os.path.dirname(os.path.realpath(sys.executable)),
        os.path.dirname(package),
        package,
    ]
    if not adapter_argv:
        return roots
    executable = adapter_argv[0]
    if not os.path.isabs(executable):
        search = path if path is not None else os.environ.get("PATH", os.defpath)
        found = shutil.which(executable, path=search) if executable else None
        if found is None:
            raise SandboxVisibilityError(
                "read_allowlist_refused:adapter_unresolved", executable or "<empty>"
            )
        executable = os.path.abspath(found)
    from .worker_workspace import _node_install_root

    node_root = _node_install_root(executable)
    roots.append(
        os.fspath(node_root) if node_root is not None else os.path.realpath(executable)
    )
    return roots


def _guarded(roots: Iterable[str], anchors: Sequence[str]) -> tuple[list[str], list[str]]:
    """``(kept, dropped)``: a root that is the filesystem root or an ancestor of
    (or equal to) an anchor is dropped, never widened; a missing one is skipped."""
    kept: list[str] = []
    dropped: list[str] = []
    for raw in roots:
        real = os.path.realpath(raw)
        if not os.path.exists(real):
            continue
        if os.path.dirname(real) == real or any(_within(anchor, real) for anchor in anchors):
            dropped.append(real)
        else:
            kept.append(real)
    return kept, dropped


def _outermost(paths: Iterable[str]) -> list[str]:
    ordered = sorted(dict.fromkeys(paths), key=lambda path: (len(path), path))
    kept: list[str] = []
    for path in ordered:
        if not any(os.path.isdir(root) and _within(path, root) for root in kept):
            kept.append(path)
    return sorted(kept)


def read_allow_set(
    *,
    worktree: str | os.PathLike[str],
    repo: str | os.PathLike[str],
    read_roots: Sequence[str],
    worker_argv: Sequence[str],
    backend: str = "landlock",
    launch_path: str | None = None,
) -> tuple[list[str], list[str]]:
    """``(allow roots, dropped roots)`` of the NF-2026-01004 read allow-list.

    Computed in the supervisor at plan time: Landlock writes it into the plan
    (the child never derives it), bubblewrap turns it into ``--ro-bind``s.  The
    worktree and the card read roots are kept as declared; every derived root
    (system, toolchain, the request roots the ``--landlock-exec`` argv names,
    the git read surface) passes the anchor drop against the account home, the
    repository and the worktree.  ``launch_path`` is the launch env's ``PATH``
    a PATH-relative adapter resolves against.
    """
    real_worktree = os.path.realpath(os.fspath(worktree))
    anchors = [
        anchor
        for anchor in (_account_home(), os.path.realpath(os.fspath(repo)), real_worktree)
        if anchor
    ]
    explicit = [real_worktree, *(os.path.realpath(root) for root in read_roots)]
    if backend == "bubblewrap":
        # Only the card's read roots: the system, toolchain, git and package
        # roots are Landlock-only needs; the base bwrap argv already presents
        # /usr, /etc, /proc and /dev, and its own binds carry the rest.
        _bubblewrap_parts(worker_argv)
        return _outermost(explicit), []
    request_values, adapter_argv = _landlock_exec_parts(worker_argv)
    request = [v for v in request_values if os.path.realpath(v) != real_worktree]
    kept, dropped = _guarded(
        [
            *SYSTEM_READ_ROOTS,
            *_toolchain_roots(adapter_argv, path=launch_path),
            *request,
            *_git_read_roots(real_worktree),
        ],
        anchors,
    )
    return _outermost([*explicit, *kept]), sorted(set(dropped))


def bubblewrap_read_binds(allow: Sequence[str], *, worktree: str | os.PathLike[str]) -> list[str]:
    """The card read roots bubblewrap binds read-only at their own path.

    ``allow`` is the bubblewrap :func:`read_allow_set` (the worktree plus the
    card ``read_roots``, never a system, toolchain or git root); every root
    except the worktree (already bound at the sandbox workspace) and roots
    beneath :data:`_BWRAP_BASE_MOUNTS` (the argv already presents those).
    Never the filesystem root.
    """
    real_worktree = os.path.realpath(os.fspath(worktree))
    return [
        root
        for root in allow
        if os.path.dirname(root) != root
        and os.path.normcase(root) != os.path.normcase(real_worktree)
        and not any(_within(root, base) for base in _BWRAP_BASE_MOUNTS)
    ]


def _bwrap_base_op(option: str, values: Sequence[str]) -> bool:
    """Whether a bubblewrap op is one of the base mounts the read binds follow:
    the ``/usr``-style ro-binds and symlinks, ``--proc``, ``--dev`` and the
    fresh ``--tmpfs /tmp`` (a card root under ``/tmp`` must sit above it)."""
    if option in {"--proc", "--dev"}:
        return True
    if option == "--tmpfs":
        return values[0] == "/tmp"
    if option in {"--ro-bind", "--ro-bind-try", "--symlink"}:
        return values[-1] in _BWRAP_BASE_MOUNTS
    return False


def bubblewrap_read_argv(
    worker_argv: Sequence[str], allow: Sequence[str], *, worktree: str | os.PathLike[str]
) -> list[str]:
    """``worker_argv`` with ``--ro-bind <root> <root>`` per card read root.

    Nothing broader is bound.  The binds are inserted right after the base
    mounts (:func:`_bwrap_base_op`) and before the first original worktree,
    write or protective op, so every original op keeps its relative order and
    its precedence: a write bind beneath a read root, or the nested
    ``--ro-bind <worktree>/.git``, still mounts over the read bind.
    """
    argv = list(worker_argv)
    end, _ops, _adapter = _bubblewrap_parts(argv)
    binds = bubblewrap_read_binds(allow, worktree=worktree)
    insert_at = end
    index = 1
    while index < end:
        option = argv[index]
        arity = _BWRAP_OPTION_ARITY[option]
        if arity and not _bwrap_base_op(option, argv[index + 1 : index + 1 + arity]):
            insert_at = index
            break
        index += 1 + arity
    added = [value for root in binds for value in ("--ro-bind", root, root)]
    return [*argv[:insert_at], *added, *argv[insert_at:]]


def check_read_allowlist(
    backend: str,
    *,
    read_roots: Sequence[str],
    deny_read: Sequence[str],
    landlock_abi: Callable[[], int] = _default_landlock_abi,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Launch gate: ``not_declared`` | ``enforced`` | ``owner_opt_out``.

    Bubblewrap enforces ``read_roots`` (each allow root ``--ro-bind``, nothing
    broader) but can never hide a file inside a bound root, so it also
    declaring ``deny_read`` is refused.  Every other :func:`check_backend`
    refusal becomes ``read_allowlist_unenforceable:<cause>:owner_opt_out=<env>=1``
    unless the owner set ``READ_ALLOWLIST_OPT_OUT_ENV=1``.
    """
    if not read_roots and not deny_read:
        return "not_declared"
    try:
        if backend == "bubblewrap" and read_roots:
            if not deny_read:
                return "enforced"
            raise SandboxVisibilityError("deny_read_unsupported_backend", backend)
        check_backend(
            backend, read_roots=read_roots, deny_read=deny_read, landlock_abi=landlock_abi
        )
    except SandboxVisibilityError as exc:
        env = os.environ if environ is None else environ
        if str(env.get(READ_ALLOWLIST_OPT_OUT_ENV, "")).strip() == "1":
            return "owner_opt_out"
        raise SandboxVisibilityError(
            "read_allowlist_unenforceable",
            f"{exc}:owner_opt_out={READ_ALLOWLIST_OPT_OUT_ENV}=1",
        ) from exc
    return "enforced"


def _link_receipt(unreachable: Sequence[str]) -> dict[str, Any]:
    """Receipt fields for directory links whose target no backend grant reaches."""
    return {
        "deny_read_link_count": len(unreachable),
        "deny_read_links": [
            {"link": link[:_ECHO_CHARS], "status": "outside_link_unreachable"}
            for link in unreachable[:16]
        ],
    }


def check_backend(
    backend: str,
    *,
    read_roots: Sequence[str],
    deny_read: Sequence[str],
    landlock_abi: Callable[[], int] = _default_landlock_abi,
) -> None:
    """Refuse a backend that cannot enforce the declared read surface."""
    if not read_roots and not deny_read:
        return
    if backend == "windows_appcontainer":
        return
    field = "read_roots" if read_roots else "deny_read"
    if backend == "landlock":
        abi = landlock_abi()
        if abi < 1:
            raise SandboxVisibilityError(f"{field}_landlock_unavailable", f"abi={abi}")
        return
    if read_roots:
        raise SandboxVisibilityError("read_roots_unsupported_backend")
    raise SandboxVisibilityError("deny_read_unsupported_backend", backend)


def prepare_launch_visibility(
    *,
    backend: str,
    worktree: str | os.PathLike[str],
    repo: str | os.PathLike[str],
    read_roots: Sequence[str],
    deny_read: Sequence[str],
    plan_path: str | os.PathLike[str],
    worker_argv: Sequence[str],
    write_plan: Callable[[Any, Mapping[str, Any]], Any],
    workspace_prepare: Mapping[str, Any] | None = None,
    read_allowlist_opt_out: bool = False,
    launch_path: str | None = None,
) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    """``(worker_argv, sandbox_visibility receipt, supervisor spec fields)``.

    The spec fields are empty except on AppContainer with denied files, where
    they carry ``deny_read_paths`` (resolved, each inside the worktree) and
    ``deny_read_roots`` (``[worktree]``, re-checked before an ACE is written).
    ``read_allowlist_opt_out`` is honoured only while :func:`check_backend`
    still refuses this backend (the owner opt-out of
    :func:`check_read_allowlist`); an enforcing backend ignores it.  Bubblewrap
    with ``read_roots`` binds only the card read roots read-only
    (:func:`bubblewrap_read_argv`); its ``deny_read`` launches only under the
    opt-out, recorded unenforced.  ``launch_path`` is the launch env's ``PATH``.
    """
    receipt: dict[str, Any] = {
        "schema_id": VISIBILITY_SCHEMA_ID,
        "backend": backend,
        "read_roots": list(read_roots),
        "read_roots_enforcement": "none",
        "deny_read": list(deny_read),
        "deny_read_enforcement": "none",
        "denied_file_count": 0,
        "deny_read_refused_count": 0,
        "read_layer_rule_count": 0,
        # Declared limitation: deny_read hides working-tree bytes only.
        "git_object_store_readable": True,
        "workspace_prepare": dict(workspace_prepare or {"status": "not_applicable"}),
    }
    if not read_roots and not deny_read:
        return list(worker_argv), receipt, {}
    if backend == "bubblewrap" and read_roots:
        # NF-2026-01004: each allow root read-only at its own path, nothing broader.
        if deny_read and not read_allowlist_opt_out:
            raise SandboxVisibilityError("deny_read_unsupported_backend", backend)
        allow, dropped = read_allow_set(
            worktree=worktree,
            repo=repo,
            read_roots=read_roots,
            worker_argv=worker_argv,
            backend=backend,
            launch_path=launch_path,
        )
        argv = bubblewrap_read_argv(worker_argv, allow, worktree=worktree)
        receipt.update(
            read_roots_enforcement="bubblewrap_ro_bind",
            deny_read_enforcement="owner_opt_out_unenforced" if deny_read else "none",
            read_allowlist="bubblewrap_ro_bind",
            read_allowlist_root_count=len(allow),
            read_allowlist_dropped=[path[:_ECHO_CHARS] for path in dropped[:16]],
        )
        return argv, receipt, {}
    if read_allowlist_opt_out:
        try:
            check_backend(
                backend,
                read_roots=read_roots,
                deny_read=deny_read,
                landlock_abi=_default_landlock_abi,
            )
        except SandboxVisibilityError:
            unenforced = "owner_opt_out_unenforced"
            receipt.update(
                read_roots_enforcement=unenforced if read_roots else "none",
                deny_read_enforcement=unenforced if deny_read else "none",
                read_allowlist=unenforced,
            )
            return list(worker_argv), receipt, {}
    if backend == "landlock":
        allow: list[str] | None = None
        if read_roots:
            # NF-2026-01004: an allow-list layer; only the plan's roots are readable.
            allow, dropped = read_allow_set(
                worktree=worktree,
                repo=repo,
                read_roots=read_roots,
                worker_argv=worker_argv,
                launch_path=launch_path,
            )
            denied, unreachable = scan_deny_read(
                [worktree, *read_roots], deny_read, granted=allow
            )
            receipt.update(
                read_allowlist="landlock_allow_list",
                read_allowlist_root_count=len(allow),
                read_allowlist_dropped=[path[:_ECHO_CHARS] for path in dropped[:16]],
            )
        else:
            # deny_read only: the layer leaves every path outside a split
            # readable, so no outside link target is unreachable (granted=None).
            denied, unreachable = scan_deny_read(
                [worktree, repo, *read_roots], deny_read, granted=None
            )
        rules = plan_read_layer(read_roots=read_roots, denied=denied, allow_roots=allow)
        write_plan(Path(plan_path), plan_document(rules))
        receipt.update(
            read_roots_enforcement="landlock_read_layer" if read_roots else "none",
            deny_read_enforcement="landlock_read_layer" if deny_read else "none",
            denied_file_count=len(denied),
            read_layer_rule_count=len(rules),
            read_layer_plan=os.fspath(plan_path),
            **_link_receipt(unreachable),
        )
        return read_layer_argv(plan_path, worker_argv), receipt, {}
    if backend == "windows_appcontainer":
        own = os.path.realpath(os.fspath(worktree))
        shared = [os.path.realpath(os.fspath(root)) for root in read_roots]
        # No reachability classification here: every directory link resolving
        # outside this launch's worktree counts as readable (granted=None).
        denied, _ = scan_deny_read(
            [own, *shared], deny_read, granted=None, link_roots=[own]
        )
        contained, refused = contained_paths(denied, [own])
        for path in refused:
            for root in shared:
                inside = _under_root(path, [root])
                if inside is not None:
                    raise SandboxVisibilityError(
                        "deny_read_shared_read_root_unsupported",
                        os.path.relpath(inside, root).replace(os.sep, "/"),
                    )
            # A leaf resolving outside the worktree and every shared root cannot carry an ACE.
            raise SandboxVisibilityError(
                "deny_read_unenforceable_link", "file_link_outside_worktree"
            )
        receipt.update(
            read_roots_enforcement=(
                "appcontainer_read_execute_grant" if read_roots else "none"
            ),
            deny_read_enforcement="appcontainer_deny_ace" if deny_read else "none",
            denied_file_count=len(contained),
            deny_read_refused_count=len(refused),
            deny_read_refused=[path[:_ECHO_CHARS] for path in refused[:16]],
        )
        spec = {"deny_read_paths": contained, "deny_read_roots": [own]}
        return list(worker_argv), receipt, spec if contained else {}
    check_backend(backend, read_roots=read_roots, deny_read=deny_read)
    raise SandboxVisibilityError("deny_read_unsupported_backend", backend)


if __name__ == "__main__":
    sys.exit(main())
