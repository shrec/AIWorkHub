"""NF-2026-01020: a non-inheritable ACE must be written to one object only.

``request_scoped_grants`` gives every directory above every request leaf --
the volume root, ``C:\\Users``, the profile, ``AppData``, ``AppData\\Local``,
``Temp`` -- a NON-inheritable traverse ACE, and revokes it again at close.
Writing those through ``SetNamedSecurityInfoW`` makes Windows re-propagate
inheritance into every existing child object, so each of those writes walks
the whole user profile, twice per launch.  Measured on installed 0.11.92:
606-868 s between supervisor start and ``CreateProcess``, and a further
10-20 minutes of one-threaded, ~21,000 "other" I/O operations per second in
teardown before the supervisor terminalized.

A traverse ACE has no descendant to propagate to, so it goes to that one
object instead.  An inheritable ACE -- the modify leaves, the persistent
provider-install grants -- keeps ``SetNamedSecurityInfoW``, because its
children must receive (and on revoke lose) the inherited copy.

These tests drive the real ctypes boundary against a recording advapi32 /
kernel32 double and assert which API wrote which path.
"""

from __future__ import annotations

import ctypes

import pytest

import aiworkhub.windows_appcontainer as wac
from aiworkhub.windows_appcontainer import (
    _Identity,
    _PathGrant,
    _Win32Failure,
    request_scoped_grants,
)


# A real, readable SID (S-1-15-2) so the boundary's string_at is safe.
_SID = b"\x01\x01\x00\x00\x00\x00\x00\x0f\x02\x00\x00\x00"
_SID_BUFFER = ctypes.create_string_buffer(_SID, len(_SID))
_GRANT, _REVOKE = 1, 4
_DACL_INFO = 0x4
_UNPROTECTED = _DACL_INFO | 0x20000000
_PROTECTED = _DACL_INFO | 0x80000000
_TRAVERSE_MASK = 0x000200A0
_ERROR_ACCESS_DENIED = 5


def _identity():
    return _Identity("n", "d", "S-1-15-2", ctypes.addressof(_SID_BUFFER), False)


class RecordingSecurityLib:
    """advapi32 + kernel32 for the grant/revoke path, recording every write.

    ``named_writes`` is the propagating API (``SetNamedSecurityInfoW``, which
    re-walks every existing descendant); ``object_writes`` is the one that
    touches this file or directory alone (``SetFileSecurityW``).
    """

    def __init__(self, *, dacl=222, protected=False, auto_inherited=False, status=0):
        self.dacl = dacl
        self.protected = protected
        self.auto_inherited = auto_inherited
        self.status = status
        self.entries = []
        self.named_writes = []
        self.object_writes = []
        self.object_controls = []
        self.freed = []

    def GetLengthSid(self, sid):
        return len(_SID)

    def GetNamedSecurityInfoW(self, path, obj, info, owner, group, dacl, sacl, sd):
        sd._obj.value = 111
        dacl._obj.value = self.dacl
        return 0

    def GetSecurityDescriptorControl(self, descriptor, control, revision):
        # What GetNamedSecurityInfoW really returns: a self-relative
        # descriptor with its DACL present (0x8004), plus the bits under test.
        control._obj.value = (
            0x8004
            | (0x1000 if self.protected else 0)
            | (0x0400 if self.auto_inherited else 0)
        )
        return 1

    def SetEntriesInAclW(self, count, entry, old_acl, new_acl):
        e = entry._obj
        trustee = ctypes.string_at(e.Trustee.ptstrName, len(_SID))
        self.entries.append(
            (count, e.grfAccessPermissions, e.grfAccessMode, e.grfInheritance,
             e.Trustee.TrusteeForm, trustee, old_acl.value)
        )
        new_acl._obj.value = 333
        return 0

    def SetNamedSecurityInfoW(self, path, obj, info, owner, group, dacl, sacl):
        self.named_writes.append((path, info, getattr(dacl, "value", dacl)))
        return self.status

    # -- the object-only write ---------------------------------------------

    def InitializeSecurityDescriptor(self, descriptor, revision):
        descriptor._obj.Revision = revision
        descriptor._obj.Control = 0
        descriptor._obj.Dacl = None
        return 1

    def SetSecurityDescriptorDacl(self, descriptor, present, dacl, defaulted):
        if not present or defaulted:
            return 0
        descriptor._obj.Dacl = getattr(dacl, "value", dacl)
        return 1

    def SetSecurityDescriptorControl(self, descriptor, interest, bits):
        # Only the auto-inherit and protected bits are settable; the real API
        # fails with ERROR_INVALID_PARAMETER on any other.
        if interest & ~0x3F00:
            return 0
        sd = descriptor._obj
        sd.Control = (sd.Control & ~interest) | (bits & interest)
        return 1

    def SetFileSecurityW(self, path, info, descriptor):
        sd = descriptor._obj
        self.object_writes.append((path, info, sd.Dacl, sd.Revision))
        self.object_controls.append(sd.Control)
        return 0 if self.status else 1

    def LocalFree(self, ptr):
        self.freed.append(getattr(ptr, "value", ptr))


def _api(lib, monkeypatch, *, satisfied=""):
    """The real ctypes boundary with every Win32 export bound to ``lib``."""
    api = wac._CtypesWin32Api.__new__(wac._CtypesWin32Api)
    api._advapi32 = lib
    api._kernel32 = lib
    api._security_base = lib
    api._security_base_library = "kernelbase"
    api._userenv = None
    monkeypatch.setattr(api, "_grant_already_satisfied", lambda *_a: satisfied)
    # SetFileSecurityW reports failure through GetLastError, not a status
    # return, so the double's ``status`` has to reach the caller that way.
    monkeypatch.setattr(wac, "_last_win_error", lambda: lib.status)
    return api


# -- 1. the traverse chain and its revokes ----------------------------------


def test_a_traverse_grant_never_calls_set_named_security_info(tmp_path, monkeypatch):
    lib = RecordingSecurityLib()
    api = _api(lib, monkeypatch)

    grant = api.grant_path_access(_identity(), str(tmp_path), "traverse")

    assert grant.inheritable is False
    assert lib.entries == [(1, _TRAVERSE_MASK, _GRANT, 0, 0, _SID, 222)]
    assert lib.named_writes == []
    assert lib.object_writes == [(str(tmp_path), _UNPROTECTED, 333, 1)]
    assert lib.freed == [333, 111]  # merged ACL, then the descriptor


def test_a_traverse_revoke_never_calls_set_named_security_info(tmp_path, monkeypatch):
    lib = RecordingSecurityLib()
    api = _api(lib, monkeypatch)
    grant = api.grant_path_access(_identity(), str(tmp_path), "traverse")

    api.revoke_path_access(grant)

    assert grant.revoke_error is None
    # The revoke still re-reads the current DACL and drops only this SID.
    assert lib.entries[-1] == (1, 0, _REVOKE, 0, 0, _SID, 222)
    assert lib.named_writes == []
    assert lib.object_writes == [
        (str(tmp_path), _UNPROTECTED, 333, 1),
        (str(tmp_path), _UNPROTECTED, 333, 1),
    ]
    # Idempotent: a second close writes nothing at all.
    api.revoke_path_access(grant)
    assert len(lib.object_writes) == 2


# -- 2. inheritable grants keep the propagating write -----------------------


def test_an_inheritable_modify_grant_still_uses_set_named_security_info(
    tmp_path, monkeypatch
):
    lib = RecordingSecurityLib()
    api = _api(lib, monkeypatch)

    grant = api.grant_path_access(_identity(), str(tmp_path), "modify")

    assert grant.inheritable is True
    # OBJECT_INHERIT | CONTAINER_INHERIT: the children must get this ACE.
    assert lib.entries[0][3] == 0x3
    assert lib.named_writes == [(str(tmp_path), _UNPROTECTED, 333)]
    assert lib.object_writes == []


def test_an_inheritable_revoke_still_reaches_the_descendants(tmp_path, monkeypatch):
    """A revoke that stopped at the object would leave every child holding an
    inherited allow ACE for the container SID."""
    lib = RecordingSecurityLib()
    api = _api(lib, monkeypatch)
    grant = api.grant_path_access(_identity(), str(tmp_path), "modify")

    api.revoke_path_access(grant)

    assert grant.revoke_error is None
    assert lib.named_writes == [
        (str(tmp_path), _UNPROTECTED, 333),
        (str(tmp_path), _UNPROTECTED, 333),
    ]
    assert lib.object_writes == []


def test_a_grant_recorded_without_an_inheritance_answer_still_propagates(
    tmp_path, monkeypatch
):
    """``_PathGrant``'s default is the safe one: withdraw from the whole
    subtree unless the grant recorded that nothing inherited it."""
    lib = RecordingSecurityLib()
    api = _api(lib, monkeypatch)

    api.revoke_path_access(_PathGrant(str(tmp_path), "modify", _SID))

    assert lib.named_writes == [(str(tmp_path), _UNPROTECTED, 333)]
    assert lib.object_writes == []


# -- 3. what the new write is handed ----------------------------------------


@pytest.mark.parametrize("protected", [False, True])
def test_the_object_write_gets_the_dacl_and_control_state_of_the_named_write(
    tmp_path, monkeypatch, protected
):
    """Identical inputs, only the write API differs: the merged DACL and the
    DACL + (UN)PROTECTED control bits handed to the object-only write are the
    ones the SetNamedSecurityInfoW path passes."""
    propagating = RecordingSecurityLib(protected=protected)
    named_api = _api(propagating, monkeypatch)
    assert named_api._set_sid_entry(
        str(tmp_path), _SID, _GRANT, _TRAVERSE_MASK, 0, "grant_path_access",
        propagates=True,
    )

    single = RecordingSecurityLib(protected=protected)
    object_api = _api(single, monkeypatch)
    assert object_api._set_sid_entry(
        str(tmp_path), _SID, _GRANT, _TRAVERSE_MASK, 0, "grant_path_access",
        propagates=False,
    )

    assert propagating.entries == single.entries
    assert propagating.object_writes == [] and single.named_writes == []
    path, info, dacl = propagating.named_writes[0]
    assert info == (_PROTECTED if protected else _UNPROTECTED)
    assert single.object_writes == [(path, info, dacl, 1)]
    # Same allocations read and released, in the same order.
    assert single.freed == propagating.freed == [333, 111]


# -- 4. every existing rule is still in force -------------------------------


def test_a_null_dacl_is_refused_on_the_object_only_path_too(tmp_path, monkeypatch):
    """A NULL DACL already admits everyone; merging into it would replace it
    with a one-entry DACL that locks everyone else out."""
    lib = RecordingSecurityLib(dacl=None)
    api = _api(lib, monkeypatch)

    grant = api.grant_path_access(_identity(), str(tmp_path), "traverse")

    assert grant.restore is None
    assert lib.entries == []
    assert lib.named_writes == [] and lib.object_writes == []
    assert lib.freed == [111]


def test_a_boundary_omitted_ancestor_is_written_through_neither_api(monkeypatch):
    # C:\Users is omitted on every platform; the volume root is only refused
    # by _windows_volume_root, which is Windows-only.
    path = r"C:\Users"
    lib = RecordingSecurityLib()
    api = _api(lib, monkeypatch)

    grant = api.grant_path_access(_identity(), path, "traverse")

    assert grant.restore is None
    assert lib.entries == []
    assert lib.named_writes == [] and lib.object_writes == []
    api.revoke_path_access(grant)
    assert lib.named_writes == [] and lib.object_writes == []


def test_a_refused_object_write_raises_the_same_failure(tmp_path, monkeypatch):
    lib = RecordingSecurityLib(status=_ERROR_ACCESS_DENIED)
    api = _api(lib, monkeypatch)

    with pytest.raises(_Win32Failure) as excinfo:
        api.grant_path_access(_identity(), str(tmp_path), "traverse")

    assert lib.named_writes == []
    assert len(lib.object_writes) == 1
    assert excinfo.value.win_error == _ERROR_ACCESS_DENIED
    assert excinfo.value.operation == "grant_path_access"
    assert excinfo.value.detail == f"write DACL {tmp_path}"
    assert lib.freed == [333, 111]  # both allocations released on the way out


def test_a_refused_object_write_keeps_the_access_denied_hint(tmp_path, monkeypatch):
    """The persistent provider-install grant on a FILE is non-inheritable, so
    it takes the new write -- and must still name the one-time icacls
    command when this user has no WRITE_DAC there."""
    target = tmp_path / "claude.cmd"
    target.write_text("@echo off\n", encoding="utf-8")
    lib = RecordingSecurityLib(status=_ERROR_ACCESS_DENIED)
    api = _api(lib, monkeypatch)

    with pytest.raises(_Win32Failure) as excinfo:
        api.grant_path_access(
            _identity(), str(target), "read_execute", persistent=True
        )

    assert lib.named_writes == []
    assert excinfo.value.detail == wac._all_packages_grant_hint(str(target))
    # Past 400 characters the hint keeps only the command, never nothing.
    assert "icacls" in excinfo.value.detail


def test_a_satisfied_persistent_grant_still_writes_through_neither_api(
    tmp_path, monkeypatch
):
    lib = RecordingSecurityLib()
    api = _api(lib, monkeypatch, satisfied="all_application_packages")

    grant = api.grant_path_access(
        _identity(), str(tmp_path), "read_execute", persistent=True
    )

    assert grant.satisfied_by == "all_application_packages"
    assert grant.restore is None
    assert lib.entries == []
    assert lib.named_writes == [] and lib.object_writes == []
    assert lib.freed == []


def test_a_protected_ancestor_keeps_its_protection_on_the_object_write(
    tmp_path, monkeypatch
):
    """A protected DACL stays protected and an unprotected one stays
    unprotected: the write names the state it just read."""
    lib = RecordingSecurityLib(protected=True)
    api = _api(lib, monkeypatch)

    api.grant_path_access(_identity(), str(tmp_path), "traverse")

    assert lib.object_writes == [(str(tmp_path), _PROTECTED, 333, 1)]
    assert lib.object_controls == [0x1000]


@pytest.mark.parametrize(
    ("protected", "auto_inherited", "written"),
    [(False, False, 0), (True, False, 0x1000), (False, True, 0x0500), (True, True, 0x1500)],
)
def test_the_object_write_carries_the_control_bits_it_read(
    tmp_path, monkeypatch, protected, auto_inherited, written
):
    """A fresh descriptor has no control bits, and written bare it loses
    SE_DACL_AUTO_INHERITED (0x0400) and a protected DACL's SE_DACL_PROTECTED
    (0x1000).  Both are copied from the descriptor just read -- auto-inherited
    together with the SE_DACL_AUTO_INHERIT_REQ (0x0100) it needs to survive
    the write -- and nothing else is: SE_SELF_RELATIVE | SE_DACL_PRESENT, also
    read, are not settable and the strict double refuses them."""
    lib = RecordingSecurityLib(protected=protected, auto_inherited=auto_inherited)
    api = _api(lib, monkeypatch)

    grant = api.grant_path_access(_identity(), str(tmp_path), "traverse")
    api.revoke_path_access(grant)

    assert grant.revoke_error is None
    assert lib.object_controls == [written, written]


# -- 5. the plan this was measured on ---------------------------------------


def test_no_traverse_in_a_request_plan_reaches_set_named_security_info(
    tmp_path, monkeypatch
):
    """Applying and revoking a whole request plan: the ancestor chain -- the
    part that walked the user profile -- touches the propagating API zero
    times, while the request leaf still does."""
    leaf = tmp_path / "request"
    leaf.mkdir()
    plan = request_scoped_grants({}, str(leaf))
    traverse = [grant.path for grant in plan if grant.access == "traverse"]
    assert traverse, "the ancestor chain is what NF-2026-01020 is about"

    lib = RecordingSecurityLib()
    api = _api(lib, monkeypatch)
    applied = [
        api.grant_path_access(_identity(), grant.path, grant.access)
        for grant in plan
    ]
    for grant in applied:
        api.revoke_path_access(grant)

    propagated = {path for path, _info, _dacl in lib.named_writes}
    assert propagated == {str(leaf)}
    assert propagated.isdisjoint(traverse)
