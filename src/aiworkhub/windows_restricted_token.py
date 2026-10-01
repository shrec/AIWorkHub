"""Windows restricted-token sandbox primitive: the Landlock-analog rung.

Linux worker isolation is a ladder (``worker_workspace.select_sandbox_backend``):
bubblewrap, then Landlock -- unprivileged self-restriction that confines
mutations while leaving reads open -- then fail closed.  Windows has had exactly
one rung, AppContainer, and that rung needs ACL grants outside the repository,
which this repository's owner rejects: no admin/UAC, no drive letters, no ACL
write above ``<repo>/.aiworkhub/runtime/sandboxes``.

This module is the missing middle rung, built as a self-contained primitive and
deliberately wired into nothing.  It implements exactly three things:

1. TOKEN -- ``CreateRestrictedToken`` over this process's own primary token with
   ``DISABLE_MAX_PRIVILEGE``, ``BUILTIN\\Administrators`` deny-only, and a
   restricting-SID list of the world groups plus one slot SID.  The token user
   SID is deliberately *not* restricting (see :func:`_restricting_sids`).  Low
   integrity and a three-entry default DACL follow, so the child can reopen the
   kernel objects it creates.
2. LAUNCH -- ``CreateProcessAsUserW`` with that token, shell-free argv quoted by
   the same ``windows_appcontainer.build_command_line`` the AppContainer rung
   uses, only the three std handles inheritable, and membership of a
   kill-on-close Job carrying the full UI-restriction mask before the child
   executes a single instruction.
3. SLOT ROOT -- :func:`prepare_slot_root` writes a protected DACL and then a Low
   mandatory label on exactly one directory beneath
   ``<repo>/.aiworkhub/runtime/sandboxes/slots``, and refuses, before touching
   any security descriptor, anything that is not exactly that.

The effect a live host probe can measure: reads succeed only where the world
groups can read (system directories, Program Files, a repository on a data
volume), the user profile is unreadable, and writes succeed only where the
restricted check passes *and* the object is Low-labelled -- which is the slot's
own root and nothing else.  Another slot's root fails the restricted check
because its DACL names a different slot SID.

Import-safe on Linux: every ``ctypes.WinDLL`` binding lives inside
:class:`_CtypesWin32Api`, constructed only by :func:`_load_win32`, so no Windows
symbol is resolved at import time and the unit tests drive the whole primitive
through a fake seam injected via ``api=`` on any OS.  There is no backend
registry, config knob, cache or fallback here: a sandbox that silently degrades
is not one.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import struct
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Mapping, NamedTuple, Protocol, Sequence

try:
    from . import windows_appcontainer as _appcontainer
    from . import windows_job_structures as _job_structures
except ImportError:  # direct-script entrypoint
    import windows_appcontainer as _appcontainer  # type: ignore[no-redef]
    import windows_job_structures as _job_structures  # type: ignore[no-redef]

__all__ = [
    "RestrictedTokenProbe",
    "RestrictedTokenProcess",
    "RestrictedTokenRequest",
    "RestrictedTokenUnsupported",
    "SlotRootReceipt",
    "launch_restricted",
    "prepare_slot_root",
    "probe",
    "restricted_access",
    "slot_sid",
]


# --------------------------------------------------------------------------- #
# refusals
# --------------------------------------------------------------------------- #


class RestrictedTokenUnsupported(RuntimeError):
    """A restricted-token operation is refused, with a machine-stable reason.

    ``reason`` is the snake_case code callers branch on and the only part of
    the message whose spelling is a contract; ``str(exc)`` always *starts* with
    it, so a reason can never be lost by a log line that kept only the text.
    ``detail`` carries the failing Win32 call name and its ``GetLastError``
    when the refusal came from the boundary rather than from a contract check,
    which is what makes :func:`probe` diagnosable from one line.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}:{detail}" if detail else reason)


class _Win32CallFailed(Exception):
    """One failing Win32 call at the seam: its name and ``GetLastError``.

    Never escapes this module.  Each orchestration step translates it into a
    :class:`RestrictedTokenUnsupported` whose reason names the step, so a
    caller can tell "the integrity write failed" from "the job refused the
    child" without parsing a Win32 code.
    """

    def __init__(self, call: str, win_error: int) -> None:
        self.call = call
        self.win_error = win_error
        super().__init__(f"{call} win_error={win_error}")


def _boundary(reason: str, action: Callable[[], Any]) -> Any:
    """Run one seam call, translating its failure into a typed refusal."""

    try:
        return action()
    except _Win32CallFailed as failure:
        raise RestrictedTokenUnsupported(
            reason, f"{failure.call}:{failure.win_error}"
        ) from failure


def _quiet(action: Callable[..., Any], *args: Any) -> None:
    """Run an unwind action; a cleanup failure must not mask the real one."""

    try:
        action(*args)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# well-known SIDs and access masks (MODEL steps 1 and 3)
# --------------------------------------------------------------------------- #

_EVERYONE_SID = "S-1-1-0"
_AUTHENTICATED_USERS_SID = "S-1-5-11"
_RESTRICTED_SID = "S-1-5-12"
_LOCAL_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"
_USERS_SID = "S-1-5-32-545"
_LOW_INTEGRITY_SID = "S-1-16-4096"

# Identifier authority 0 is used by nothing Windows ships, so a slot SID names
# no account and grants nothing by itself: it is only ever meaningful inside a
# DACL this module wrote or a restricting-SID list it built.
_SLOT_SID_PREFIX = "S-1-0-"
_SLOT_SID_SUBAUTHORITIES = 4
_MAX_SUBAUTHORITY = 0xFFFFFFFF

# The one place beneath a repository where a slot root may live.
_SLOT_ROOT_RELATIVE = (".aiworkhub", "runtime", "sandboxes", "slots")

# ACE inheritance flags.
_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_INHERIT_ONLY_ACE = 0x08
_INHERIT_OI_CI = _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE
_INHERIT_OI_CI_IO = _INHERIT_OI_CI | _INHERIT_ONLY_ACE

# File and standard access rights.
_FILE_ADD_FILE = 0x00000002
_FILE_ADD_SUBDIRECTORY = 0x00000004
_FILE_WRITE_ATTRIBUTES = 0x00000100
_DELETE = 0x00010000
_WRITE_DAC = 0x00040000
_WRITE_OWNER = 0x00080000
_GENERIC_ALL = 0x10000000

_FILE_GENERIC_READ = 0x00120089
_FILE_GENERIC_WRITE = 0x00120116
_FILE_GENERIC_EXECUTE = 0x001200A0
_FILE_ALL_ACCESS = 0x001F01FF
_FILE_MODIFY = (
    _DELETE | _FILE_GENERIC_READ | _FILE_GENERIC_WRITE | _FILE_GENERIC_EXECUTE
)

# The three rights the slot never gets on its own root.  Masked off rather than
# merely omitted: a future right added to the union below cannot smuggle one of
# them back in, and "withheld" stops being a property of the comment.
_SLOT_ROOT_WITHHELD = _DELETE | _WRITE_DAC | _WRITE_OWNER

# The slot SID's ACE on the slot ROOT itself: enough to create and traverse,
# never enough to delete the root or rewrite its own security descriptor.
_SLOT_ROOT_ACCESS = (
    _FILE_GENERIC_READ
    | _FILE_GENERIC_EXECUTE
    | _FILE_ADD_FILE
    | _FILE_ADD_SUBDIRECTORY
    | _FILE_WRITE_ATTRIBUTES
) & ~_SLOT_ROOT_WITHHELD

# Mandatory label policy.
_SYSTEM_MANDATORY_LABEL_NO_WRITE_UP = 0x00000001

# CreateRestrictedToken flags.
_DISABLE_MAX_PRIVILEGE = 0x00000001

# Job object limits and the UI restriction mask of MODEL step 2.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_UILIMIT_HANDLES = 0x00000001
_JOB_OBJECT_UILIMIT_READCLIPBOARD = 0x00000002
_JOB_OBJECT_UILIMIT_WRITECLIPBOARD = 0x00000004
_JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS = 0x00000008
_JOB_OBJECT_UILIMIT_DISPLAYSETTINGS = 0x00000010
_JOB_OBJECT_UILIMIT_GLOBALATOMS = 0x00000020
_JOB_OBJECT_UILIMIT_DESKTOP = 0x00000040
_JOB_OBJECT_UILIMIT_EXITWINDOWS = 0x00000080
_JOB_UI_RESTRICTIONS = (
    _JOB_OBJECT_UILIMIT_READCLIPBOARD
    | _JOB_OBJECT_UILIMIT_WRITECLIPBOARD
    | _JOB_OBJECT_UILIMIT_EXITWINDOWS
    | _JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS
    | _JOB_OBJECT_UILIMIT_DISPLAYSETTINGS
    | _JOB_OBJECT_UILIMIT_GLOBALATOMS
    | _JOB_OBJECT_UILIMIT_DESKTOP
    | _JOB_OBJECT_UILIMIT_HANDLES
)

# Process creation flags.
_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_NO_WINDOW = 0x08000000

# WaitForSingleObject's largest bounded timeout; INFINITE itself is avoided so
# a wait can never outlive the process that asked for it by accident.
_INFINITE_WAIT_MS = 0xFFFFFFFE


# --------------------------------------------------------------------------- #
# value types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SlotRootReceipt:
    """What :func:`prepare_slot_root` did to one slot root.

    ``slot_root`` is the canonical path actually written, not the caller's
    spelling of it, and ``changed`` is False exactly when the root was already
    canonical and nothing was written at all.
    """

    slot_root: str
    sid: str
    changed: bool


@dataclass(frozen=True)
class RestrictedTokenRequest:
    """A fully specified, shell-free restricted-token launch request.

    ``environment=None`` is handled exactly as
    ``windows_appcontainer.AppContainerRequest.environment=None`` is: no block
    is built and ``CreateProcessAsUserW`` receives NULL for ``lpEnvironment``,
    so the child inherits the caller's environment verbatim.  A mapping --
    including an empty one -- becomes an explicit Unicode block that replaces
    the caller's environment entirely.  The distinction matters: "inherit" and
    "start from nothing" are different requests, and collapsing them is how a
    child ends up without the variables process creation itself needs.
    """

    argv: Sequence[str]
    slot_sid: str
    executable: str | None = None
    working_directory: str | None = None
    environment: Mapping[str, str] | None = None
    stdin_handle: int | None = None
    stdout_handle: int | None = None
    stderr_handle: int | None = None
    create_no_window: bool = True

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("argv must contain at least the executable")


class RestrictedTokenProbe(NamedTuple):
    """Whether this host can build the token, and why not when it cannot."""

    available: bool
    reason: str


@dataclass(frozen=True)
class _Ace:
    """One allow ACE: trustee SID, access mask, inheritance flags."""

    sid: str
    access: int
    flags: int


@dataclass(frozen=True)
class _Label:
    """A ``SYSTEM_MANDATORY_LABEL`` ACE: integrity SID, policy, inheritance."""

    sid: str
    policy: int
    flags: int


@dataclass(frozen=True)
class _Security:
    """The descriptor state :func:`prepare_slot_root` compares and writes."""

    protected: bool
    aces: tuple[_Ace, ...]
    label: _Label | None


@dataclass(frozen=True)
class _ProcessSpec:
    """Everything ``CreateProcessAsUserW`` needs, resolved before the call."""

    executable: str
    command_line: str
    working_directory: str | None
    environment: Mapping[str, str] | None
    handle_list: tuple[int, ...]
    creation_flags: int
    std_input: int | None
    std_output: int | None
    std_error: int | None


@dataclass(frozen=True)
class _ProcessCreation:
    """The four values ``CreateProcessAsUserW`` hands back."""

    process_id: int
    thread_id: int
    process_handle: int
    thread_handle: int


class _Win32Api(Protocol):
    """The bounded Windows surface this primitive uses.

    Every mutating call raises :class:`_Win32CallFailed` carrying the failing
    call name and ``GetLastError``.  ``close_handle`` and ``terminate_*`` are
    invoked during unwind and must tolerate being called on a handle whose
    owner already failed.
    """

    # --- slot root security descriptor ------------------------------------
    def is_reparse_point(self, path: str) -> bool: ...

    def read_security(self, path: str) -> _Security: ...

    def write_dacl(
        self, path: str, aces: Sequence[_Ace], *, protected: bool
    ) -> None: ...

    def write_label(self, path: str, label: _Label) -> None: ...

    # --- token ------------------------------------------------------------
    def open_process_token(self) -> int: ...

    def current_user_sid(self) -> str: ...

    def current_logon_sid(self) -> str | None: ...

    def create_restricted_token(
        self,
        token: int,
        *,
        flags: int,
        disable_sids: Sequence[str],
        restrict_sids: Sequence[str],
    ) -> int: ...

    def set_token_integrity_level(self, token: int, sid: str) -> None: ...

    def set_token_default_dacl(self, token: int, aces: Sequence[_Ace]) -> None: ...

    def duplicate_impersonation_token(self, token: int) -> int: ...

    def access_check(self, token: int, path: str, desired_access: int) -> bool: ...

    # --- job and process --------------------------------------------------
    def create_job_object(self) -> int: ...

    def set_job_limits(
        self, job: int, *, limit_flags: int, ui_restrictions: int
    ) -> None: ...

    def create_process_as_user(
        self, token: int, spec: _ProcessSpec
    ) -> _ProcessCreation: ...

    def assign_process_to_job(self, job: int, creation: _ProcessCreation) -> None: ...

    def resume_thread(self, creation: _ProcessCreation) -> None: ...

    def terminate_process(self, creation: _ProcessCreation) -> None: ...

    def wait_process(self, creation: _ProcessCreation, timeout_ms: int) -> bool: ...

    def process_exit_code(self, creation: _ProcessCreation) -> int: ...

    def terminate_job(self, job: int) -> None: ...

    def close_handle(self, handle: int) -> None: ...


# --------------------------------------------------------------------------- #
# slot SID
# --------------------------------------------------------------------------- #


def slot_sid(repo_id: str, slot_id: str) -> str:
    """Return the deterministic sandbox SID for ``repo_id`` and ``slot_id``.

    The shape is ``S-1-0-a-b-c-d``: revision 1, the NULL identifier authority,
    and four 32-bit sub-authorities read little-endian out of the first 16
    bytes of ``sha256(repo_id + NUL + slot_id)``.  The NUL separator is what
    makes the pair unambiguous, so ``("ab", "c")`` and ``("a", "bc")`` cannot
    produce the same SID.  Same input, same SID -- which is the whole point: a
    slot's DACL and the restricting-SID list of the token allowed to write it
    are derived independently and must agree without any stored state.
    """

    if not repo_id:
        raise ValueError("repo_id must be a non-empty string")
    if not slot_id:
        raise ValueError("slot_id must be a non-empty string")
    digest = hashlib.sha256(
        repo_id.encode("utf-8") + b"\x00" + slot_id.encode("utf-8")
    ).digest()
    authorities = struct.unpack("<4I", digest[:16])
    return _SLOT_SID_PREFIX + "-".join(str(value) for value in authorities)


def _is_slot_sid(value: object) -> bool:
    """True only for the exact ``S-1-0-a-b-c-d`` shape :func:`slot_sid` emits.

    Anything else -- a real account SID, an integrity SID, a hand-edited string
    with a leading zero -- is refused rather than written into a DACL, because
    a DACL ACE naming a trustee this module did not derive is a grant nobody
    audited.
    """

    if not isinstance(value, str) or not value.startswith(_SLOT_SID_PREFIX):
        return False
    authorities = value[len(_SLOT_SID_PREFIX) :].split("-")
    if len(authorities) != _SLOT_SID_SUBAUTHORITIES:
        return False
    for text in authorities:
        if not (text.isascii() and text.isdigit()):
            return False
        if text != str(int(text)) or int(text) > _MAX_SUBAUTHORITY:
            return False
    return True


# --------------------------------------------------------------------------- #
# slot root preparation (MODEL step 3)
# --------------------------------------------------------------------------- #


def prepare_slot_root(
    slot_root: str, sid: str, *, repo_root: str, api: _Win32Api | None = None
) -> SlotRootReceipt:
    """Make ``slot_root`` the one directory ``sid`` may write, and nothing else.

    Writes a protected DACL and then a Low mandatory label on the slot root
    and, on a first preparation only, on its existing descendants.  DACL first
    and deliberately: the creator-owner has implicit ``WRITE_DAC``, but the
    label write needs ``WRITE_OWNER``, and the Full Control ACE the DACL just
    wrote is what grants it.  An already-canonical root is a no-op --
    ``changed=False``, no walk, no write.

    Every refusal is raised BEFORE any security descriptor is touched, because
    a half-prepared root is worse than an unprepared one:

    ``slot_root_unc_or_device_path``
        a UNC share or a ``\\\\?\\`` / ``\\\\.\\`` prefixed input, whose
        canonicalisation rules are not the ones the containment check assumes.
    ``slot_sid_invalid``
        not the :func:`slot_sid` shape, so nothing proves it is sandbox-only.
    ``slot_root_outside_sandbox_root``
        not exactly ``realpath(repo_root)`` plus
        ``.aiworkhub/runtime/sandboxes/slots`` plus one path component,
        compared case-insensitively after ``realpath``.  This is the refusal
        that keeps an ancestor's DACL and label permanently out of reach, and
        it also catches a reparse point that redirects the path out of the
        sandbox, since ``realpath`` resolves it before the comparison.
    ``slot_root_not_directory``
        no directory is there to secure.
    ``slot_root_reparse_point``
        the slot root, or any component between ``repo_root`` and it, is a
        reparse point -- which would aim the write at wherever it points.
    """

    _refuse_device_path(repo_root)
    _refuse_device_path(slot_root)
    if not _is_slot_sid(sid):
        raise RestrictedTokenUnsupported("slot_sid_invalid", str(sid))
    repo = os.path.realpath(repo_root)
    root = os.path.realpath(slot_root)
    sandbox = os.path.join(repo, *_SLOT_ROOT_RELATIVE)
    parent, leaf = os.path.split(root)
    if parent.casefold() != sandbox.casefold() or not _is_one_component(leaf):
        raise RestrictedTokenUnsupported("slot_root_outside_sandbox_root", root)
    if not os.path.isdir(root):
        raise RestrictedTokenUnsupported("slot_root_not_directory", root)

    if api is None:
        api = _load_win32()
    for path in _ancestor_chain(repo, slot_root, root):
        if api.is_reparse_point(path):
            raise RestrictedTokenUnsupported("slot_root_reparse_point", path)

    user_sid = _boundary("token_identity_failed", api.current_user_sid)
    desired = _root_security(sid, user_sid)
    current = _boundary("slot_root_security_unreadable", lambda: api.read_security(root))
    if current == desired:
        return SlotRootReceipt(root, sid, False)
    _write_security(api, root, desired)
    _prepare_descendants(api, root, sid, user_sid)
    return SlotRootReceipt(root, sid, True)


def _refuse_device_path(value: str) -> None:
    """Refuse a UNC share or a device-namespace path before it is resolved.

    ``\\\\server\\share``, ``\\\\?\\C:\\x`` and ``\\\\.\\pipe\\x`` all begin
    with two separators, and all three canonicalise under rules the containment
    check below does not model.  One test covers all three.
    """

    if value.replace("/", "\\").startswith("\\\\"):
        raise RestrictedTokenUnsupported("slot_root_unc_or_device_path", value)


def _is_one_component(leaf: str) -> bool:
    """True for exactly one ordinary path component, never ``.`` or ``..``."""

    return bool(leaf) and leaf not in (".", "..")


def _ancestor_chain(repo: str, requested: str, root: str) -> tuple[str, ...]:
    """Every path from ``repo``'s first sandbox component down to the slot root.

    Both spellings are walked: the literal path the caller asked for and the
    canonical one that will actually be written.  A reparse point sitting in
    either is a refusal, so a junction cannot be slipped in between the
    containment check and the write.
    """

    chain: list[str] = []
    for base in (os.path.abspath(requested), root):
        current = os.path.dirname(base)
        components: list[str] = [base]
        for _ in _SLOT_ROOT_RELATIVE:
            components.append(current)
            current = os.path.dirname(current)
        chain.extend(components)
    seen: list[str] = []
    for path in chain:
        if path.casefold() == repo.casefold():
            continue
        if path not in seen:
            seen.append(path)
    return tuple(seen)


def _full_control_aces(user_sid: str, flags: int) -> tuple[_Ace, ...]:
    """The three trustees that keep Full Control: the user, SYSTEM, admins.

    Nothing broader ever appears: no ``ALL APPLICATION PACKAGES``, no
    ``Users``, no ``Everyone``, no ``Authenticated Users``.  Those groups are
    restricting SIDs on the token, so granting them here would hand every
    restricted child on the machine a write path into this slot.
    """

    return (
        _Ace(user_sid, _FILE_ALL_ACCESS, flags),
        _Ace(_LOCAL_SYSTEM_SID, _FILE_ALL_ACCESS, flags),
        _Ace(_ADMINISTRATORS_SID, _FILE_ALL_ACCESS, flags),
    )


def _low_label(flags: int) -> _Label:
    """The Low mandatory label a restricted child is allowed to write through."""

    return _Label(_LOW_INTEGRITY_SID, _SYSTEM_MANDATORY_LABEL_NO_WRITE_UP, flags)


def _root_security(sid: str, user_sid: str) -> _Security:
    """MODEL step 3's descriptor for the slot ROOT itself."""

    aces = _full_control_aces(user_sid, _INHERIT_OI_CI) + (
        # Children inherit Modify; the ACE is inherit-only so it never applies
        # to the root object it sits on.
        _Ace(sid, _FILE_MODIFY, _INHERIT_OI_CI_IO),
        # The root itself is create-and-traverse only.  Without DELETE the slot
        # cannot remove its own root, and without WRITE_DAC / WRITE_OWNER it
        # cannot re-ACL or re-label itself out of this module's control -- which
        # is the difference between a sandbox and a suggestion.
        _Ace(sid, _SLOT_ROOT_ACCESS, 0),
    )
    return _Security(True, aces, _low_label(_INHERIT_OI_CI))


def _descendant_security(sid: str, user_sid: str, is_directory: bool) -> _Security:
    """The descriptor a pre-existing child gets on a first preparation.

    A slot directory that already held files keeps DACLs and labels the
    restricted token cannot use, and inheritance is not retroactive, so this is
    what makes those children writable by the slot token at all.
    """

    flags = _INHERIT_OI_CI if is_directory else 0
    aces = _full_control_aces(user_sid, flags) + (_Ace(sid, _FILE_MODIFY, flags),)
    return _Security(True, aces, _low_label(flags))


def _write_security(api: _Win32Api, path: str, security: _Security) -> None:
    """Write the DACL, then the label.  Never the other way round."""

    try:
        api.write_dacl(path, security.aces, protected=security.protected)
        if security.label is not None:
            api.write_label(path, security.label)
    except _Win32CallFailed as failure:
        raise RestrictedTokenUnsupported(
            "slot_root_security_write_failed", f"{failure.call}:{failure.win_error}"
        ) from failure


def _prepare_descendants(
    api: _Win32Api, root: str, sid: str, user_sid: str
) -> None:
    """Re-secure the slot root's pre-existing children, reparse points excluded.

    ``os.scandir`` is walked without following links.  A reparse point is never
    descended into and never written: the write would land wherever it points,
    which by definition is not this subtree.  The containment guard is belt and
    braces on top of that -- the one invariant this walk must never break is
    that no security descriptor outside the slot root subtree is touched.
    """

    pending = [root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                children = sorted(entries, key=lambda entry: entry.path)
        except OSError:
            continue
        for entry in children:
            if not _within(root, entry.path) or api.is_reparse_point(entry.path):
                continue
            is_directory = entry.is_dir(follow_symlinks=False)
            _write_security(
                api, entry.path, _descendant_security(sid, user_sid, is_directory)
            )
            if is_directory:
                pending.append(entry.path)


def _within(root: str, path: str) -> bool:
    """True only for a path strictly beneath ``root``, compared case-blind."""

    prefix = root.rstrip("\\/") + os.sep
    return path.casefold().startswith(prefix.casefold())


# --------------------------------------------------------------------------- #
# restricted token (MODEL step 1)
# --------------------------------------------------------------------------- #


def _restricting_sids(slot: str, api: _Win32Api) -> tuple[str, ...]:
    """MODEL step 1's restricting-SID list, in order.

    The token USER SID is deliberately absent.  Adding it would restrict every
    object whose only grant is to the user -- which is nearly everything a
    worker legitimately needs -- while the world groups plus the slot SID give
    exactly the intended shape: an object is reachable only when its DACL
    grants one of the world groups (system directories, Program Files, a
    repository on a data volume) or names this slot.  That asymmetry is the
    whole mechanism; it is not an oversight to be "fixed" by adding the user.

    The logon SID is included when the token has one so the child keeps its
    window station and desktop, and omitted when it has none rather than
    invented.
    """

    sids = [_EVERYONE_SID, _USERS_SID, _AUTHENTICATED_USERS_SID, _RESTRICTED_SID]
    logon = api.current_logon_sid()
    if logon:
        sids.append(logon)
    sids.append(slot)
    return tuple(sids)


def _default_dacl(slot: str, user_sid: str) -> tuple[_Ace, ...]:
    """MODEL step 1's ``TokenDefaultDacl``: the child must reopen what it makes.

    Without this the restricted child creates kernel objects -- its own
    threads, events, pipes -- whose default DACL names only the token user, and
    reopening them then fails the restricting-SID check.  ``GENERIC_ALL`` for
    the slot SID is what makes that check pass on the child's own objects
    without widening its reach to anything else on the machine.
    """

    return (
        _Ace(user_sid, _GENERIC_ALL, 0),
        _Ace(_LOCAL_SYSTEM_SID, _GENERIC_ALL, 0),
        _Ace(slot, _GENERIC_ALL, 0),
    )


def _build_restricted_token(api: _Win32Api, slot: str, opened: list[int]) -> int:
    """Build the MODEL step 1 token: restricted, Low integrity, default DACL.

    Every handle is appended to ``opened`` the moment it exists, so one
    caller-side unwind closes exactly what succeeded -- including the case
    where the integrity write or the default DACL is what failed.  The exact
    same token is used by :func:`launch_restricted`, :func:`restricted_access`
    and :func:`probe`, so the check the probe reports and the confinement the
    child actually gets can never drift apart.
    """

    base_token = _boundary("open_token_failed", api.open_process_token)
    opened.append(base_token)
    user_sid = _boundary("token_identity_failed", api.current_user_sid)
    restricting = _boundary("token_identity_failed", lambda: _restricting_sids(slot, api))
    token = _boundary(
        "create_restricted_token_failed",
        lambda: api.create_restricted_token(
            base_token,
            flags=_DISABLE_MAX_PRIVILEGE,
            disable_sids=(_ADMINISTRATORS_SID,),
            restrict_sids=restricting,
        ),
    )
    opened.append(token)
    _boundary(
        "set_integrity_level_failed",
        lambda: api.set_token_integrity_level(token, _LOW_INTEGRITY_SID),
    )
    _boundary(
        "set_default_dacl_failed",
        lambda: api.set_token_default_dacl(token, _default_dacl(slot, user_sid)),
    )
    return token


# --------------------------------------------------------------------------- #
# launch (MODEL step 2)
# --------------------------------------------------------------------------- #


class RestrictedTokenProcess:
    """A Popen-shaped owner for one restricted, Job-owned child.

    :meth:`terminate` kills the whole tree through ``TerminateJobObject`` and
    never a single pid: a restricted child that spawned helpers must not
    outlive the call that ended it, and the Job is the only owner that needs no
    pid lookup and no helper executable.  :meth:`close` is idempotent and
    releases the process, thread, token and job handles -- and because the Job
    carries ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``, closing it kills any
    survivor, so even a dropped reference cannot leak a running restricted
    child.
    """

    def __init__(
        self,
        api: _Win32Api,
        creation: _ProcessCreation,
        token: int,
        job: int,
        command_line: str,
    ) -> None:
        self._api = api
        self._creation = creation
        self._token = token
        self._job = job
        self._command_line = command_line
        self._closed = False
        self.pid = creation.process_id
        self.returncode: int | None = None

    def poll(self) -> int | None:
        """The exit code if the child has already exited, else ``None``."""

        return self._observe(0)

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the child, raising ``TimeoutExpired`` when ``timeout`` runs out."""

        if self.returncode is not None:
            return self.returncode
        bounded = _INFINITE_WAIT_MS if timeout is None else max(0, int(timeout * 1000))
        observed = self._observe(bounded)
        if observed is None:
            if timeout is None:
                raise RestrictedTokenUnsupported("unbounded_wait_returned_timeout")
            raise subprocess.TimeoutExpired(self._command_line, float(timeout))
        return observed

    def terminate(self) -> None:
        """Kill the whole Job-owned tree, not just the direct child."""

        if self._closed:
            return
        _boundary("terminate_job_failed", lambda: self._api.terminate_job(self._job))

    def kill(self) -> None:
        """Alias for :meth:`terminate`: Windows offers no gentler signal here."""

        self.terminate()

    def close(self) -> None:
        """Release every owned handle.  Idempotent; closing the Job kills survivors."""

        if self._closed:
            return
        self._closed = True
        for handle in (
            self._creation.thread_handle,
            self._creation.process_handle,
            self._token,
            self._job,
        ):
            _quiet(self._api.close_handle, handle)

    def _observe(self, timeout_ms: int) -> int | None:
        if self.returncode is not None or self._closed:
            return self.returncode
        signaled = _boundary(
            "wait_failed", lambda: self._api.wait_process(self._creation, timeout_ms)
        )
        if not signaled:
            return None
        self.returncode = _boundary(
            "exit_code_failed", lambda: self._api.process_exit_code(self._creation)
        )
        return self.returncode


def launch_restricted(
    request: RestrictedTokenRequest, *, api: _Win32Api | None = None
) -> RestrictedTokenProcess:
    """Launch ``request.argv`` under a restricted, Low-integrity token.

    MODEL steps 1 and 2 in order: restrict this process's own primary token (a
    restricted child of the caller's own token needs no privilege), label it
    Low, give it a default DACL it can reopen its own kernel objects through,
    create the kill-on-close Job with the full UI-restriction mask, then
    ``CreateProcessAsUserW`` suspended, assign the child to the Job, and only
    then resume -- so the child belongs to the Job before it executes a single
    instruction.

    Only the three std handles are made inheritable, through
    ``PROC_THREAD_ATTRIBUTE_HANDLE_LIST``.  Anything else in that list would be
    a hole straight through the restriction.

    There is no fallback.  Any failure closes every handle this call opened,
    terminates the child first if one already exists, and raises
    :class:`RestrictedTokenUnsupported`; an unrestricted or less-restricted
    launch is never attempted, because a sandbox that silently degrades is
    indistinguishable from none.
    """

    if not _is_slot_sid(request.slot_sid):
        raise RestrictedTokenUnsupported("slot_sid_invalid", str(request.slot_sid))
    _check_environment(request.environment)
    command_line = _appcontainer.build_command_line(request.argv)
    spec = _ProcessSpec(
        executable=request.executable or str(request.argv[0]),
        command_line=command_line,
        working_directory=request.working_directory,
        environment=request.environment,
        handle_list=_std_handles(request),
        creation_flags=_creation_flags(request),
        std_input=request.stdin_handle,
        std_output=request.stdout_handle,
        std_error=request.stderr_handle,
    )
    if api is None:
        api = _load_win32()

    opened: list[int] = []
    creation: _ProcessCreation | None = None
    try:
        token = _build_restricted_token(api, request.slot_sid, opened)
        job = _boundary("create_job_failed", api.create_job_object)
        opened.append(job)
        _boundary(
            "set_job_limits_failed",
            lambda: api.set_job_limits(
                job,
                limit_flags=_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
                ui_restrictions=_JOB_UI_RESTRICTIONS,
            ),
        )
        creation = _boundary(
            "create_process_failed", lambda: api.create_process_as_user(token, spec)
        )
        launched = creation
        _boundary(
            "assign_job_failed", lambda: api.assign_process_to_job(job, launched)
        )
        _boundary("resume_thread_failed", lambda: api.resume_thread(launched))
    except BaseException:
        _unwind(api, opened, creation)
        raise

    # The unrestricted primary token opened to derive this one has no further
    # use and is not retained for the child's lifetime.
    base_token = opened[0]
    _quiet(api.close_handle, base_token)
    return RestrictedTokenProcess(api, creation, token, job, command_line)


def _unwind(
    api: _Win32Api, opened: Sequence[int], creation: _ProcessCreation | None
) -> None:
    """Close everything a failed launch opened, child first.

    The child is terminated before any handle is released.  A failure between
    ``CreateProcessAsUserW`` and ``ResumeThread`` leaves a suspended process
    nothing else will ever reap, and a failure after ``ResumeThread`` leaves one
    running; closing the Job would kill it too, but only once it is a member,
    which is exactly what an assign failure means it is not.
    """

    if creation is not None:
        _quiet(api.terminate_process, creation)
        _quiet(api.close_handle, creation.thread_handle)
        _quiet(api.close_handle, creation.process_handle)
    for handle in reversed(tuple(opened)):
        _quiet(api.close_handle, handle)


def _check_environment(environment: Mapping[str, str] | None) -> None:
    """Refuse an embedded NUL before a single handle is opened.

    A NUL in a key or value would either truncate the block
    ``CreateProcessAsUserW`` receives or splice a caller-chosen ``NAME=VALUE``
    pair into it.  ``None`` is not an environment and carries nothing to check.
    """

    if environment is None:
        return
    for key, value in environment.items():
        if "\x00" in key or "\x00" in value:
            raise ValueError("environment keys and values must not contain embedded NUL")


def _std_handles(request: RestrictedTokenRequest) -> tuple[int, ...]:
    """The inheritable handle list: the three std handles, deduplicated."""

    handles: list[int] = []
    for handle in (
        request.stdin_handle,
        request.stdout_handle,
        request.stderr_handle,
    ):
        if handle is not None and handle not in handles:
            handles.append(handle)
    return tuple(handles)


def _creation_flags(request: RestrictedTokenRequest) -> int:
    """Suspended, Unicode environment, extended startup info, optional no-window.

    ``CREATE_SUSPENDED`` is not a convenience: it is what lets the child be
    assigned to the Job before it runs.
    """

    flags = (
        _CREATE_SUSPENDED | _CREATE_UNICODE_ENVIRONMENT | _EXTENDED_STARTUPINFO_PRESENT
    )
    if request.create_no_window:
        flags |= _CREATE_NO_WINDOW
    return flags


# --------------------------------------------------------------------------- #
# non-mutating queries
# --------------------------------------------------------------------------- #


def _desired_access(access: str) -> int:
    """Map an access name onto the file right :func:`restricted_access` checks."""

    if access == "read":
        return _FILE_GENERIC_READ
    if access == "read_execute":
        return _FILE_GENERIC_READ | _FILE_GENERIC_EXECUTE
    if access == "write":
        return _FILE_GENERIC_WRITE
    raise ValueError(f"access must be read, read_execute or write, not {access!r}")


def restricted_access(
    slot_sid: str, path: str, access: str, *, api: _Win32Api | None = None
) -> bool:
    """Answer whether the restricted token could reach ``path`` for ``access``.

    A read-only ``AccessCheck`` of the path's own owner, group, DACL and
    mandatory label against an impersonation duplicate of exactly the token
    MODEL step 1 builds.  Nothing is opened for write, nothing is launched and
    no security descriptor is modified, which is what lets a probe ask whether
    ``%USERPROFILE%`` or a provider executable is reachable *before* deciding
    that launching there is worth attempting.

    ``access`` is ``'read'``, ``'read_execute'`` or ``'write'``; anything else
    is a :class:`ValueError`.  A path that does not exist answers ``False``
    rather than raising -- "not reachable" is the honest answer about a path
    with no security descriptor at all.
    """

    desired = _desired_access(access)
    if not _is_slot_sid(slot_sid):
        raise RestrictedTokenUnsupported("slot_sid_invalid", str(slot_sid))
    if not os.path.exists(path):
        return False
    if api is None:
        api = _load_win32()

    opened: list[int] = []
    try:
        token = _build_restricted_token(api, slot_sid, opened)
        impersonation = _boundary(
            "duplicate_token_failed", lambda: api.duplicate_impersonation_token(token)
        )
        opened.append(impersonation)
        return bool(
            _boundary(
                "access_check_failed",
                lambda: api.access_check(impersonation, path, desired),
            )
        )
    finally:
        for handle in reversed(opened):
            _quiet(api.close_handle, handle)


def probe(*, api: _Win32Api | None = None) -> RestrictedTokenProbe:
    """Report, without mutating anything, whether the primitive can run here.

    Off Windows the reason is exactly ``not_windows``.  On Windows the probe
    builds the MODEL step 1 token and closes it again -- the only way to know
    that ``CreateRestrictedToken`` and the Low integrity write actually succeed
    on this host rather than merely being spelled correctly -- and on failure
    the reason names the failing Win32 call and its ``GetLastError``.
    """

    if api is None:
        if os.name != "nt":
            return RestrictedTokenProbe(False, "not_windows")
        api = _load_win32()

    opened: list[int] = []
    try:
        _build_restricted_token(
            api, slot_sid("aiworkhub-restricted-token-probe", "probe"), opened
        )
    except RestrictedTokenUnsupported as refusal:
        return RestrictedTokenProbe(False, str(refusal))
    finally:
        for handle in reversed(opened):
            _quiet(api.close_handle, handle)
    return RestrictedTokenProbe(True, "available")


# --------------------------------------------------------------------------- #
# the real ctypes boundary -- Windows only, loaded on demand
# --------------------------------------------------------------------------- #

_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_GROUP_SECURITY_INFORMATION = 0x00000002
_DACL_SECURITY_INFORMATION = 0x00000004
_LABEL_SECURITY_INFORMATION = 0x00000010
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_DACL_PROTECTED = 0x1000

_ACL_REVISION = 2
_ACCESS_ALLOWED_ACE_TYPE = 0x00
_SYSTEM_MANDATORY_LABEL_ACE_TYPE = 0x11

_TOKEN_ASSIGN_PRIMARY = 0x0001
_TOKEN_DUPLICATE = 0x0002
_TOKEN_IMPERSONATE = 0x0004
_TOKEN_QUERY = 0x0008
_TOKEN_ADJUST_DEFAULT = 0x0080
_TOKEN_OPEN_ACCESS = (
    _TOKEN_ASSIGN_PRIMARY | _TOKEN_DUPLICATE | _TOKEN_QUERY | _TOKEN_ADJUST_DEFAULT
)

_TOKEN_USER_CLASS = 1
_TOKEN_GROUPS_CLASS = 2
_TOKEN_DEFAULT_DACL_CLASS = 6
_TOKEN_INTEGRITY_LEVEL_CLASS = 25

_SE_GROUP_INTEGRITY = 0x00000020
_SE_GROUP_LOGON_ID = 0xC0000000

_SECURITY_IMPERSONATION_LEVEL = 2
_TOKEN_IMPERSONATION_TYPE = 2

_JOB_OBJECT_BASIC_UI_RESTRICTIONS_CLASS = 4
_JOB_OBJECT_EXTENDED_LIMIT_CLASS = 9

_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_STARTF_USESTDHANDLES = 0x00000100
_HANDLE_FLAG_INHERIT = 0x00000001
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_RESUME_THREAD_FAILED = 0xFFFFFFFF


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


class _TOKEN_GROUPS(ctypes.Structure):
    _fields_ = [
        ("GroupCount", ctypes.c_uint32),
        ("Groups", _SID_AND_ATTRIBUTES * 1),
    ]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


class _TOKEN_DEFAULT_DACL(ctypes.Structure):
    _fields_ = [("DefaultDacl", ctypes.c_void_p)]


class _ACL_HEADER(ctypes.Structure):
    _fields_ = [
        ("AclRevision", ctypes.c_uint8),
        ("Sbz1", ctypes.c_uint8),
        ("AclSize", ctypes.c_uint16),
        ("AceCount", ctypes.c_uint16),
        ("Sbz2", ctypes.c_uint16),
    ]


class _ACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_uint8),
        ("AceFlags", ctypes.c_uint8),
        ("AceSize", ctypes.c_uint16),
    ]


class _KNOWN_ACE(ctypes.Structure):
    """The prefix ACCESS_ALLOWED_ACE and SYSTEM_MANDATORY_LABEL_ACE share."""

    _fields_ = [
        ("Header", _ACE_HEADER),
        ("Mask", ctypes.c_uint32),
        ("SidStart", ctypes.c_uint32),
    ]


class _JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
    """Declared here because windows_job_structures owns the LIMIT layouts only."""

    _fields_ = [("UIRestrictionsClass", ctypes.c_uint32)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_uint32),
        ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32),
        ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16),
        ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class _GENERIC_MAPPING(ctypes.Structure):
    _fields_ = [
        ("GenericRead", ctypes.c_uint32),
        ("GenericWrite", ctypes.c_uint32),
        ("GenericExecute", ctypes.c_uint32),
        ("GenericAll", ctypes.c_uint32),
    ]


def _load_win32() -> _Win32Api:
    """Build the real ctypes boundary.  Windows only, and only on demand.

    Resolving the DLLs here instead of at import time is what keeps this module
    importable on Linux, where the unit tests exercise every orchestration path
    through a fake seam.
    """

    if os.name != "nt":
        raise RestrictedTokenUnsupported("not_windows", os.name)
    return _CtypesWin32Api()


class _CtypesWin32Api:
    """The real Win32 boundary for this primitive.

    Constructed only on Windows by :func:`_load_win32`.  ``argtypes`` are set
    for every handle-taking call deliberately: without them ctypes marshals a
    Python int as a C ``int`` and silently truncates a 64-bit HANDLE.
    """

    def __init__(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        void, u32, size_t = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t

        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = void
        kernel32.CloseHandle.argtypes = [void]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [void]
        kernel32.LocalFree.restype = void
        kernel32.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetFileAttributesW.restype = u32
        kernel32.CreateJobObjectW.argtypes = [void, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = void
        kernel32.SetInformationJobObject.argtypes = [void, ctypes.c_int, void, u32]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [void, void]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.TerminateJobObject.argtypes = [void, u32]
        kernel32.TerminateJobObject.restype = ctypes.c_int
        kernel32.TerminateProcess.argtypes = [void, u32]
        kernel32.TerminateProcess.restype = ctypes.c_int
        kernel32.ResumeThread.argtypes = [void]
        kernel32.ResumeThread.restype = u32
        kernel32.WaitForSingleObject.argtypes = [void, u32]
        kernel32.WaitForSingleObject.restype = u32
        kernel32.GetExitCodeProcess.argtypes = [void, void]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.SetHandleInformation.argtypes = [void, u32, u32]
        kernel32.SetHandleInformation.restype = ctypes.c_int
        kernel32.InitializeProcThreadAttributeList.argtypes = [void, u32, u32, void]
        kernel32.InitializeProcThreadAttributeList.restype = ctypes.c_int
        kernel32.UpdateProcThreadAttribute.argtypes = [
            void, u32, size_t, void, size_t, void, void,
        ]
        kernel32.UpdateProcThreadAttribute.restype = ctypes.c_int
        kernel32.DeleteProcThreadAttributeList.argtypes = [void]
        kernel32.DeleteProcThreadAttributeList.restype = None

        advapi32.OpenProcessToken.argtypes = [void, u32, void]
        advapi32.OpenProcessToken.restype = ctypes.c_int
        advapi32.GetTokenInformation.argtypes = [void, ctypes.c_int, void, u32, void]
        advapi32.GetTokenInformation.restype = ctypes.c_int
        advapi32.SetTokenInformation.argtypes = [void, ctypes.c_int, void, u32]
        advapi32.SetTokenInformation.restype = ctypes.c_int
        advapi32.CreateRestrictedToken.argtypes = [
            void, u32, u32, void, u32, void, u32, void, void,
        ]
        advapi32.CreateRestrictedToken.restype = ctypes.c_int
        advapi32.DuplicateTokenEx.argtypes = [
            void, u32, void, ctypes.c_int, ctypes.c_int, void,
        ]
        advapi32.DuplicateTokenEx.restype = ctypes.c_int
        advapi32.ConvertStringSidToSidW.argtypes = [ctypes.c_wchar_p, void]
        advapi32.ConvertStringSidToSidW.restype = ctypes.c_int
        advapi32.ConvertSidToStringSidW.argtypes = [void, void]
        advapi32.ConvertSidToStringSidW.restype = ctypes.c_int
        advapi32.GetLengthSid.argtypes = [void]
        advapi32.GetLengthSid.restype = u32
        advapi32.InitializeAcl.argtypes = [void, u32, u32]
        advapi32.InitializeAcl.restype = ctypes.c_int
        advapi32.AddAccessAllowedAceEx.argtypes = [void, u32, u32, u32, void]
        advapi32.AddAccessAllowedAceEx.restype = ctypes.c_int
        advapi32.AddMandatoryAce.argtypes = [void, u32, u32, u32, void]
        advapi32.AddMandatoryAce.restype = ctypes.c_int
        advapi32.GetAce.argtypes = [void, u32, void]
        advapi32.GetAce.restype = ctypes.c_int
        advapi32.GetSecurityDescriptorControl.argtypes = [void, void, void]
        advapi32.GetSecurityDescriptorControl.restype = ctypes.c_int
        advapi32.GetNamedSecurityInfoW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_int, u32, void, void, void, void, void,
        ]
        advapi32.GetNamedSecurityInfoW.restype = u32
        advapi32.SetNamedSecurityInfoW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_int, u32, void, void, void, void,
        ]
        advapi32.SetNamedSecurityInfoW.restype = u32
        advapi32.MapGenericMask.argtypes = [void, void]
        advapi32.MapGenericMask.restype = None
        advapi32.AccessCheck.argtypes = [
            void, void, u32, void, void, void, void, void,
        ]
        advapi32.AccessCheck.restype = ctypes.c_int
        advapi32.CreateProcessAsUserW.argtypes = [
            void, ctypes.c_wchar_p, void, void, void, ctypes.c_int, u32, void,
            ctypes.c_wchar_p, void, void,
        ]
        advapi32.CreateProcessAsUserW.restype = ctypes.c_int

        self._kernel32 = kernel32
        self._advapi32 = advapi32

    # --- filesystem security descriptors ----------------------------------

    def is_reparse_point(self, path: str) -> bool:
        attributes = self._kernel32.GetFileAttributesW(path)
        if attributes == _INVALID_FILE_ATTRIBUTES:
            return False
        return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)

    def read_security(self, path: str) -> _Security:
        descriptor = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        sacl = ctypes.c_void_p()
        status = self._advapi32.GetNamedSecurityInfoW(
            path,
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _LABEL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(dacl),
            ctypes.byref(sacl),
            ctypes.byref(descriptor),
        )
        if status != 0:
            raise _Win32CallFailed("GetNamedSecurityInfoW", int(status))
        try:
            control = ctypes.c_uint16(0)
            revision = ctypes.c_uint32(0)
            if not self._advapi32.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)
            ):
                raise _Win32CallFailed(
                    "GetSecurityDescriptorControl", ctypes.get_last_error()
                )
            allowed = tuple(
                _Ace(sid, mask, flags)
                for kind, sid, mask, flags in self._read_aces(dacl)
                if kind == _ACCESS_ALLOWED_ACE_TYPE
            )
            label: _Label | None = None
            for kind, sid, mask, flags in self._read_aces(sacl):
                if kind == _SYSTEM_MANDATORY_LABEL_ACE_TYPE:
                    label = _Label(sid, mask, flags)
                    break
            protected = bool(control.value & _SE_DACL_PROTECTED)
            return _Security(protected, allowed, label)
        finally:
            self._kernel32.LocalFree(descriptor)

    def write_dacl(
        self, path: str, aces: Sequence[_Ace], *, protected: bool
    ) -> None:
        acl = self._build_acl(aces, mandatory=False)
        information = _DACL_SECURITY_INFORMATION
        if protected:
            information |= _PROTECTED_DACL_SECURITY_INFORMATION
        status = self._advapi32.SetNamedSecurityInfoW(
            path, _SE_FILE_OBJECT, information, None, None, acl, None
        )
        if status != 0:
            raise _Win32CallFailed("SetNamedSecurityInfoW", int(status))

    def write_label(self, path: str, label: _Label) -> None:
        acl = self._build_acl(
            (_Ace(label.sid, label.policy, label.flags),), mandatory=True
        )
        status = self._advapi32.SetNamedSecurityInfoW(
            path, _SE_FILE_OBJECT, _LABEL_SECURITY_INFORMATION, None, None, None, acl
        )
        if status != 0:
            raise _Win32CallFailed("SetNamedSecurityInfoW", int(status))

    # --- token ------------------------------------------------------------

    def open_process_token(self) -> int:
        token = ctypes.c_void_p()
        if not self._advapi32.OpenProcessToken(
            self._kernel32.GetCurrentProcess(),
            _TOKEN_OPEN_ACCESS,
            ctypes.byref(token),
        ):
            raise _Win32CallFailed("OpenProcessToken", ctypes.get_last_error())
        return int(token.value or 0)

    def current_user_sid(self) -> str:
        token = self.open_process_token()
        try:
            buffer = self._token_information(token, _TOKEN_USER_CLASS)
            user = _TOKEN_USER.from_buffer(buffer)
            return self._sid_string(int(user.User.Sid or 0))
        finally:
            self.close_handle(token)

    def current_logon_sid(self) -> str | None:
        """The caller's logon SID, or ``None`` when the token carries none.

        A service token, or one built without a logon session, has no
        ``SE_GROUP_LOGON_ID`` entry at all; MODEL step 1 omits it rather than
        inventing one.
        """

        token = self.open_process_token()
        try:
            buffer = self._token_information(token, _TOKEN_GROUPS_CLASS)
            header = _TOKEN_GROUPS.from_buffer(buffer)
            groups = (_SID_AND_ATTRIBUTES * header.GroupCount).from_buffer(
                buffer, _TOKEN_GROUPS.Groups.offset
            )
            for group in groups:
                if group.Attributes & _SE_GROUP_LOGON_ID == _SE_GROUP_LOGON_ID:
                    return self._sid_string(int(group.Sid or 0))
            return None
        finally:
            self.close_handle(token)

    def create_restricted_token(
        self,
        token: int,
        *,
        flags: int,
        disable_sids: Sequence[str],
        restrict_sids: Sequence[str],
    ) -> int:
        disable, disable_storage = self._sid_array(disable_sids)
        restrict, restrict_storage = self._sid_array(restrict_sids)
        restricted = ctypes.c_void_p()
        created = self._advapi32.CreateRestrictedToken(
            ctypes.c_void_p(token),
            flags,
            len(disable_sids),
            disable,
            0,
            None,
            len(restrict_sids),
            restrict,
            ctypes.byref(restricted),
        )
        # disable_storage / restrict_storage hold the SID buffers the arrays
        # above point into; they must stay referenced until the call has copied
        # them into the new token.
        del disable_storage, restrict_storage
        if not created:
            raise _Win32CallFailed("CreateRestrictedToken", ctypes.get_last_error())
        return int(restricted.value or 0)

    def set_token_integrity_level(self, token: int, sid: str) -> None:
        buffer = self._sid_buffer(sid)
        label = _TOKEN_MANDATORY_LABEL()
        label.Label.Sid = ctypes.cast(buffer, ctypes.c_void_p)
        label.Label.Attributes = _SE_GROUP_INTEGRITY
        if not self._advapi32.SetTokenInformation(
            ctypes.c_void_p(token),
            _TOKEN_INTEGRITY_LEVEL_CLASS,
            ctypes.byref(label),
            ctypes.sizeof(label),
        ):
            raise _Win32CallFailed("SetTokenInformation", ctypes.get_last_error())

    def set_token_default_dacl(self, token: int, aces: Sequence[_Ace]) -> None:
        acl = self._build_acl(aces, mandatory=False)
        default = _TOKEN_DEFAULT_DACL()
        default.DefaultDacl = ctypes.cast(acl, ctypes.c_void_p)
        if not self._advapi32.SetTokenInformation(
            ctypes.c_void_p(token),
            _TOKEN_DEFAULT_DACL_CLASS,
            ctypes.byref(default),
            ctypes.sizeof(default),
        ):
            raise _Win32CallFailed("SetTokenInformation", ctypes.get_last_error())

    def duplicate_impersonation_token(self, token: int) -> int:
        duplicate = ctypes.c_void_p()
        if not self._advapi32.DuplicateTokenEx(
            ctypes.c_void_p(token),
            _TOKEN_QUERY | _TOKEN_IMPERSONATE,
            None,
            _SECURITY_IMPERSONATION_LEVEL,
            _TOKEN_IMPERSONATION_TYPE,
            ctypes.byref(duplicate),
        ):
            raise _Win32CallFailed("DuplicateTokenEx", ctypes.get_last_error())
        return int(duplicate.value or 0)

    def access_check(self, token: int, path: str, desired_access: int) -> bool:
        descriptor = ctypes.c_void_p()
        status = self._advapi32.GetNamedSecurityInfoW(
            path,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION
            | _GROUP_SECURITY_INFORMATION
            | _DACL_SECURITY_INFORMATION
            | _LABEL_SECURITY_INFORMATION,
            None,
            None,
            None,
            None,
            ctypes.byref(descriptor),
        )
        if status != 0:
            raise _Win32CallFailed("GetNamedSecurityInfoW", int(status))
        try:
            mapping = _GENERIC_MAPPING(
                _FILE_GENERIC_READ,
                _FILE_GENERIC_WRITE,
                _FILE_GENERIC_EXECUTE,
                _FILE_ALL_ACCESS,
            )
            desired = ctypes.c_uint32(desired_access)
            self._advapi32.MapGenericMask(
                ctypes.byref(desired), ctypes.byref(mapping)
            )
            privileges = (ctypes.c_char * 256)()
            privilege_bytes = ctypes.c_uint32(ctypes.sizeof(privileges))
            granted = ctypes.c_uint32(0)
            allowed = ctypes.c_int(0)
            if not self._advapi32.AccessCheck(
                descriptor,
                ctypes.c_void_p(token),
                desired.value,
                ctypes.byref(mapping),
                privileges,
                ctypes.byref(privilege_bytes),
                ctypes.byref(granted),
                ctypes.byref(allowed),
            ):
                raise _Win32CallFailed("AccessCheck", ctypes.get_last_error())
            return bool(allowed.value)
        finally:
            self._kernel32.LocalFree(descriptor)

    # --- job and process --------------------------------------------------

    def create_job_object(self) -> int:
        job = self._kernel32.CreateJobObjectW(None, None)
        if not job:
            raise _Win32CallFailed("CreateJobObjectW", ctypes.get_last_error())
        return int(job)

    def set_job_limits(
        self, job: int, *, limit_flags: int, ui_restrictions: int
    ) -> None:
        limits = _job_structures.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = limit_flags
        if not self._kernel32.SetInformationJobObject(
            ctypes.c_void_p(job),
            _JOB_OBJECT_EXTENDED_LIMIT_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise _Win32CallFailed("SetInformationJobObject", ctypes.get_last_error())
        restrictions = _JOBOBJECT_BASIC_UI_RESTRICTIONS(ui_restrictions)
        if not self._kernel32.SetInformationJobObject(
            ctypes.c_void_p(job),
            _JOB_OBJECT_BASIC_UI_RESTRICTIONS_CLASS,
            ctypes.byref(restrictions),
            ctypes.sizeof(restrictions),
        ):
            raise _Win32CallFailed("SetInformationJobObject", ctypes.get_last_error())

    def create_process_as_user(
        self, token: int, spec: _ProcessSpec
    ) -> _ProcessCreation:
        attributes, handle_storage = self._attribute_list(spec.handle_list)
        try:
            startup = _STARTUPINFOEXW()
            startup.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
            if attributes is not None:
                startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
                startup.StartupInfo.dwFlags |= _STARTF_USESTDHANDLES
                startup.StartupInfo.hStdInput = spec.std_input or 0
                startup.StartupInfo.hStdOutput = spec.std_output or 0
                startup.StartupInfo.hStdError = spec.std_error or 0
                for handle in spec.handle_list:
                    self._kernel32.SetHandleInformation(
                        ctypes.c_void_p(handle),
                        _HANDLE_FLAG_INHERIT,
                        _HANDLE_FLAG_INHERIT,
                    )
            command = ctypes.create_unicode_buffer(spec.command_line)
            environment = _environment_block(spec.environment)
            info = _PROCESS_INFORMATION()
            created = self._advapi32.CreateProcessAsUserW(
                ctypes.c_void_p(token),
                spec.executable,
                command,
                None,
                None,
                1 if spec.handle_list else 0,
                spec.creation_flags,
                environment,
                spec.working_directory,
                ctypes.byref(startup),
                ctypes.byref(info),
            )
            if not created:
                raise _Win32CallFailed(
                    "CreateProcessAsUserW", ctypes.get_last_error()
                )
            return _ProcessCreation(
                int(info.dwProcessId),
                int(info.dwThreadId),
                int(info.hProcess or 0),
                int(info.hThread or 0),
            )
        finally:
            if attributes is not None:
                self._kernel32.DeleteProcThreadAttributeList(
                    ctypes.cast(attributes, ctypes.c_void_p)
                )
            # handle_storage kept the HANDLE array the attribute list pointed
            # into alive across the call above.
            del handle_storage

    def assign_process_to_job(self, job: int, creation: _ProcessCreation) -> None:
        if not self._kernel32.AssignProcessToJobObject(
            ctypes.c_void_p(job), ctypes.c_void_p(creation.process_handle)
        ):
            raise _Win32CallFailed(
                "AssignProcessToJobObject", ctypes.get_last_error()
            )

    def resume_thread(self, creation: _ProcessCreation) -> None:
        resumed = self._kernel32.ResumeThread(
            ctypes.c_void_p(creation.thread_handle)
        )
        if resumed == _RESUME_THREAD_FAILED:
            raise _Win32CallFailed("ResumeThread", ctypes.get_last_error())

    def terminate_process(self, creation: _ProcessCreation) -> None:
        if not self._kernel32.TerminateProcess(
            ctypes.c_void_p(creation.process_handle), 1
        ):
            raise _Win32CallFailed("TerminateProcess", ctypes.get_last_error())

    def wait_process(self, creation: _ProcessCreation, timeout_ms: int) -> bool:
        result = self._kernel32.WaitForSingleObject(
            ctypes.c_void_p(creation.process_handle), timeout_ms
        )
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
        raise _Win32CallFailed("WaitForSingleObject", ctypes.get_last_error())

    def process_exit_code(self, creation: _ProcessCreation) -> int:
        code = ctypes.c_uint32(0)
        if not self._kernel32.GetExitCodeProcess(
            ctypes.c_void_p(creation.process_handle), ctypes.byref(code)
        ):
            raise _Win32CallFailed("GetExitCodeProcess", ctypes.get_last_error())
        return int(code.value)

    def terminate_job(self, job: int) -> None:
        if not self._kernel32.TerminateJobObject(ctypes.c_void_p(job), 1):
            raise _Win32CallFailed("TerminateJobObject", ctypes.get_last_error())

    def close_handle(self, handle: int) -> None:
        if handle:
            self._kernel32.CloseHandle(ctypes.c_void_p(handle))

    # --- internals --------------------------------------------------------

    def _token_information(self, token: int, information_class: int) -> Any:
        size = ctypes.c_uint32(0)
        self._advapi32.GetTokenInformation(
            ctypes.c_void_p(token), information_class, None, 0, ctypes.byref(size)
        )
        buffer = (ctypes.c_char * max(int(size.value), 16))()
        if not self._advapi32.GetTokenInformation(
            ctypes.c_void_p(token),
            information_class,
            buffer,
            len(buffer),
            ctypes.byref(size),
        ):
            raise _Win32CallFailed("GetTokenInformation", ctypes.get_last_error())
        return buffer

    def _sid_string(self, address: int) -> str:
        text = ctypes.c_wchar_p()
        if not self._advapi32.ConvertSidToStringSidW(
            ctypes.c_void_p(address), ctypes.byref(text)
        ):
            raise _Win32CallFailed(
                "ConvertSidToStringSidW", ctypes.get_last_error()
            )
        try:
            return text.value or ""
        finally:
            self._kernel32.LocalFree(text)

    def _sid_buffer(self, sid: str) -> Any:
        """A private binary copy of ``sid``; the OS allocation is freed here."""

        pointer = ctypes.c_void_p()
        if not self._advapi32.ConvertStringSidToSidW(sid, ctypes.byref(pointer)):
            raise _Win32CallFailed(
                "ConvertStringSidToSidW", ctypes.get_last_error()
            )
        try:
            length = int(self._advapi32.GetLengthSid(pointer))
            buffer = (ctypes.c_char * length)()
            ctypes.memmove(buffer, pointer, length)
            return buffer
        finally:
            self._kernel32.LocalFree(pointer)

    def _sid_array(self, sids: Sequence[str]) -> tuple[Any, list[Any]]:
        """A SID_AND_ATTRIBUTES array plus the SID buffers it points into."""

        if not sids:
            return None, []
        storage = [self._sid_buffer(sid) for sid in sids]
        array = (_SID_AND_ATTRIBUTES * len(sids))()
        for index, buffer in enumerate(storage):
            array[index].Sid = ctypes.cast(buffer, ctypes.c_void_p)
            array[index].Attributes = 0
        return array, storage

    def _build_acl(self, aces: Sequence[_Ace], *, mandatory: bool) -> Any:
        """Build one ACL buffer holding ``aces`` in the order given.

        ``Add*Ace`` copies each SID into the ACL itself, so the temporary SID
        buffers never outlive this call.
        """

        storage = [self._sid_buffer(ace.sid) for ace in aces]
        ace_overhead = ctypes.sizeof(_KNOWN_ACE) - ctypes.sizeof(ctypes.c_uint32)
        size = ctypes.sizeof(_ACL_HEADER) + sum(
            ace_overhead + len(buffer) for buffer in storage
        )
        acl = (ctypes.c_char * ((size + 3) & ~3))()
        if not self._advapi32.InitializeAcl(acl, len(acl), _ACL_REVISION):
            raise _Win32CallFailed("InitializeAcl", ctypes.get_last_error())
        for ace, buffer in zip(aces, storage):
            if mandatory:
                added = self._advapi32.AddMandatoryAce(
                    acl, _ACL_REVISION, ace.flags, ace.access, buffer
                )
                call = "AddMandatoryAce"
            else:
                added = self._advapi32.AddAccessAllowedAceEx(
                    acl, _ACL_REVISION, ace.flags, ace.access, buffer
                )
                call = "AddAccessAllowedAceEx"
            if not added:
                raise _Win32CallFailed(call, ctypes.get_last_error())
        return acl

    def _read_aces(self, acl: Any) -> list[tuple[int, str, int, int]]:
        """Copy every ACE of ``acl`` out as plain values; a NULL ACL gives []."""

        if not acl:
            return []
        header = _ACL_HEADER.from_address(int(acl.value))
        copied: list[tuple[int, str, int, int]] = []
        for index in range(header.AceCount):
            pointer = ctypes.c_void_p()
            if not self._advapi32.GetAce(acl, index, ctypes.byref(pointer)):
                raise _Win32CallFailed("GetAce", ctypes.get_last_error())
            address = int(pointer.value or 0)
            ace = _KNOWN_ACE.from_address(address)
            sid = self._sid_string(address + _KNOWN_ACE.SidStart.offset)
            copied.append(
                (ace.Header.AceType, sid, int(ace.Mask), int(ace.Header.AceFlags))
            )
        return copied

    def _attribute_list(self, handles: Sequence[int]) -> tuple[Any, Any]:
        """A PROC_THREAD_ATTRIBUTE_LIST carrying only the std-handle list.

        Nothing else goes in it.  An inheritable handle the child was not meant
        to have is a hole straight through every restriction above.
        """

        if not handles:
            return None, None
        size = ctypes.c_size_t(0)
        self._kernel32.InitializeProcThreadAttributeList(
            None, 1, 0, ctypes.byref(size)
        )
        buffer = (ctypes.c_char * int(size.value))()
        if not self._kernel32.InitializeProcThreadAttributeList(
            buffer, 1, 0, ctypes.byref(size)
        ):
            raise _Win32CallFailed(
                "InitializeProcThreadAttributeList", ctypes.get_last_error()
            )
        array = (ctypes.c_void_p * len(handles))(
            *[ctypes.c_void_p(handle) for handle in handles]
        )
        if not self._kernel32.UpdateProcThreadAttribute(
            buffer,
            0,
            _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            array,
            ctypes.sizeof(array),
            None,
            None,
        ):
            error = ctypes.get_last_error()
            self._kernel32.DeleteProcThreadAttributeList(
                ctypes.cast(buffer, ctypes.c_void_p)
            )
            raise _Win32CallFailed("UpdateProcThreadAttribute", error)
        return buffer, array


def _environment_block(environment: Mapping[str, str] | None) -> Any:
    """The ``CREATE_UNICODE_ENVIRONMENT`` block, or ``None`` to inherit.

    ``None`` is passed through as a NULL ``lpEnvironment``, which is exactly how
    ``windows_appcontainer`` treats ``environment=None``: the child inherits the
    caller's environment rather than being handed an empty one.
    """

    if environment is None:
        return None
    pairs = [f"{key}={value}" for key, value in environment.items()]
    return ctypes.create_unicode_buffer("\x00".join(pairs) + "\x00\x00")
