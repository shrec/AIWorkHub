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

    * No denied file: the filesystem root gets ``READ_FILE | READ_DIR`` and each
      read root gets its own explicit ``READ_FILE | READ_DIR`` rule.
    * Denied files: the filesystem root gets ``READ_DIR`` only, and every
      directory on the path from the root to a denied file is *split*: each of
      its children that is neither a denied file nor itself split gets a
      ``READ_FILE`` rule for its whole subtree, symlinks are skipped (Landlock
      checks the resolved target).  A read root containing a denied file is
      therefore granted child-by-child instead of by one rule.  A split needing
      more than ``MAX_LAYER_RULES`` rules refuses the launch with
      ``deny_read_refused:rule_cap_exceeded``.

    The layer is applied by running this file as a direct script in front of
    the sandbox argv (``--plan <json> -- <argv>``), so the worker, its MCP
    server and any validation the worker starts inherit it.  The coordinator's
    own canonical validation after exit runs outside it.  Fail-closed limits:
    an entry created after launch directly inside a split directory is
    unreadable to the worker, and hard links to a denied file elsewhere are not
    discovered.  Landlock unavailable with a non-empty ``read_roots`` or
    ``deny_read`` refuses the launch with ``read_roots_landlock_unavailable`` /
    ``deny_read_landlock_unavailable``.

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


def plan_read_layer(
    *,
    read_roots: Sequence[str],
    denied: Sequence[str],
    max_rules: int | None = None,
) -> list[tuple[str, int]]:
    """``(path, rights)`` path-beneath rules for the stacked read layer.

    No rule grants ``READ_FILE`` on a denied file or on any directory above
    one; see the module docstring for the split.  ``max_rules`` defaults to
    ``MAX_LAYER_RULES``; an unlistable split directory refuses (never skips).
    """
    cap = MAX_LAYER_RULES if max_rules is None else max_rules
    full = LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR
    denied_set = set(denied)
    split: set[str] = set()
    for path in denied:
        parent = os.path.dirname(path)
        while parent not in split:
            split.add(parent)
            above = os.path.dirname(parent)
            if above == parent:
                break
            parent = above
    tops = sorted({_top(path) for path in denied}) or [_top(os.path.abspath(os.sep))]
    rules: list[tuple[str, int]] = [
        (top, LANDLOCK_ACCESS_FS_READ_DIR if denied else full) for top in tops
    ]

    def add(path: str, rights: int) -> None:
        rules.append((path, rights))
        if len(rules) > cap:
            raise _refused("rule_cap_exceeded", f">{cap}")

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
) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    """``(worker_argv, sandbox_visibility receipt, supervisor spec fields)``.

    The spec fields are empty except on AppContainer with denied files, where
    they carry ``deny_read_paths`` (resolved, each inside the worktree) and
    ``deny_read_roots`` (``[worktree]``, re-checked before an ACE is written).
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
    if backend == "landlock":
        # The read layer leaves every path outside a split readable, so no
        # outside link target is unreachable here (granted=None).
        denied, unreachable = scan_deny_read(
            [worktree, repo, *read_roots], deny_read, granted=None
        )
        rules = plan_read_layer(read_roots=read_roots, denied=denied)
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
