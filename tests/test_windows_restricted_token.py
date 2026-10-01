"""Fake-Win32 unit tests for the Windows restricted-token sandbox primitive.

Every test here runs on every OS and makes no real Win32 call: the whole
primitive is driven through a :class:`FakeWin32Api` injected on ``api=``, which
records each allocation, each release and each security-descriptor write, and
can fail at any single step.  That is what lets these tests assert the things a
live host probe cannot: that each refusal fires *before* anything is written,
that the DACL precedes the label, that every partial failure unwinds completely,
and that no error path ever reaches for a less-restricted launch.

Nothing in this file skips or xfails.  The only opt-in gate in this subject lives
in tests/test_windows_restricted_token_live.py, which measures a real host.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import importlib.util
import os
import struct
import subprocess
from pathlib import Path

import pytest

import aiworkhub.windows_appcontainer as wac
import aiworkhub.windows_restricted_token as wrt
from aiworkhub.windows_restricted_token import (
    RestrictedTokenProbe,
    RestrictedTokenRequest,
    RestrictedTokenUnsupported,
    SlotRootReceipt,
    _Ace,
    _Label,
    _ProcessCreation,
    _Security,
    _Win32CallFailed,
    launch_restricted,
    prepare_slot_root,
    probe,
    restricted_access,
    slot_sid,
)

# The Win32 call each seam method stands for, so an injected failure produces a
# reason a reader could act on rather than a placeholder.
WIN32_CALL = {
    "read_security": "GetNamedSecurityInfoW",
    "write_dacl": "SetNamedSecurityInfoW",
    "write_label": "SetNamedSecurityInfoW",
    "open_process_token": "OpenProcessToken",
    "current_user_sid": "GetTokenInformation",
    "current_logon_sid": "GetTokenInformation",
    "create_restricted_token": "CreateRestrictedToken",
    "set_token_integrity_level": "SetTokenInformation",
    "set_token_default_dacl": "SetTokenInformation",
    "duplicate_impersonation_token": "DuplicateTokenEx",
    "access_check": "AccessCheck",
    "create_job_object": "CreateJobObjectW",
    "set_job_limits": "SetInformationJobObject",
    "create_process_as_user": "CreateProcessAsUserW",
    "assign_process_to_job": "AssignProcessToJobObject",
    "resume_thread": "ResumeThread",
    "wait_process": "WaitForSingleObject",
    "process_exit_code": "GetExitCodeProcess",
    "terminate_job": "TerminateJobObject",
    "terminate_process": "TerminateProcess",
}

# Invented, not read from the host: no test here may depend on who is running it.
USER_SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"
LOGON_SID = "S-1-5-5-0-987654"
SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
LOW_SID = "S-1-16-4096"

BASE_TOKEN = 0x200
RESTRICTED_TOKEN = 0x201
IMPERSONATION_TOKEN = 0x202
JOB = 0x300
PROCESS = 0x400
THREAD = 0x401
CHILD_PID = 4242

# Masks the MODEL fixes.  Spelled out as literals so a silent change to the
# module's own constants cannot make these tests agree with it.
FILE_ALL_ACCESS = 0x001F01FF
FILE_MODIFY = 0x001301BF
SLOT_ROOT_ACCESS = 0x001201AF
GENERIC_READ = 0x00120089
GENERIC_READ_EXECUTE = 0x001200A9
GENERIC_WRITE = 0x00120116
GENERIC_ALL = 0x10000000
DELETE = 0x00010000
WRITE_DAC = 0x00040000
WRITE_OWNER = 0x00080000
INHERIT_OI_CI = 0x03
INHERIT_OI_CI_IO = 0x0B
NO_WRITE_UP = 0x01

SLOT_RELATIVE = (".aiworkhub", "runtime", "sandboxes", "slots")
SID_A = slot_sid("repo-a", "slot-a")
SID_B = slot_sid("repo-a", "slot-b")


class FakeWin32Api:
    """A recording, fail-injectable stand-in for the restricted-token boundary.

    ``fail_at`` names the one seam method that raises :class:`_Win32CallFailed`;
    the failure is raised *after* the call is recorded and *before* any handle is
    handed out, so a test can tell "never reached" from "reached and unwound".
    ``opened`` and ``closed`` are the ledger that makes a complete unwind
    assertable instead of merely plausible.

    There is deliberately no unrestricted process-creation method.  ``api``
    carries one anyway -- :meth:`create_process` -- whose only behaviour is to
    fail the test loudly if any code path ever reaches for it.
    """

    def __init__(
        self,
        fail_at: str | None = None,
        win_error: int = 5,
        reparse: tuple[str, ...] | list[str] = (),
        signaled: bool = False,
        exit_code: int = 0,
        allowed: bool = True,
        logon_sid: str | None = LOGON_SID,
    ) -> None:
        self.fail_at = fail_at
        self.win_error = win_error
        self.reparse = {os.path.normcase(path) for path in reparse}
        self.signaled = signaled
        self.exit_code = exit_code
        self.allowed = allowed
        self.logon_sid = logon_sid
        self.events: list[tuple[str, object]] = []
        self.writes: list[tuple[str, str, object]] = []
        self.protected_writes: list[tuple[str, bool]] = []
        self.opened: list[int] = []
        self.closed: list[int] = []
        self.access_requests: list[tuple[str, int]] = []
        self.token_calls: list[dict] = []
        self.integrity: list[tuple[int, str]] = []
        self.default_dacls: list[tuple[_Ace, ...]] = []
        self.job_limits: list[dict] = []
        self.terminated: list[int] = []
        self.spec = None
        self.create_token: int | None = None
        self._state: dict[str, _Security] = {}

    # --- test-side helpers ------------------------------------------------

    def names(self) -> list[str]:
        return [name for name, _detail in self.events]

    def dacl_for(self, path: str) -> tuple[_Ace, ...]:
        for kind, written, detail in self.writes:
            if kind == "dacl" and written == path:
                assert isinstance(detail, tuple)
                return detail
        raise AssertionError(f"no DACL was written for {path}")

    def label_for(self, path: str) -> _Label:
        for kind, written, detail in self.writes:
            if kind == "label" and written == path:
                assert isinstance(detail, _Label)
                return detail
        raise AssertionError(f"no label was written for {path}")

    def written_paths(self) -> set[str]:
        return {path for _kind, path, _detail in self.writes}

    def create_process(self, *_args, **_kwargs):
        raise AssertionError("an unrestricted CreateProcessW must never be attempted")

    def _record(self, name: str, detail: object = None) -> None:
        self.events.append((name, detail))
        if name == self.fail_at:
            raise _Win32CallFailed(WIN32_CALL[name], self.win_error)

    def _hand_out(self, handle: int) -> int:
        self.opened.append(handle)
        return handle

    # --- slot root security descriptor ------------------------------------

    def is_reparse_point(self, path: str) -> bool:
        self.events.append(("is_reparse_point", path))
        return os.path.normcase(path) in self.reparse

    def read_security(self, path: str) -> _Security:
        self._record("read_security", path)
        return self._state.get(path, _Security(False, (), None))

    def write_dacl(self, path, aces, *, protected) -> None:
        self._record("write_dacl", path)
        self.writes.append(("dacl", path, tuple(aces)))
        self.protected_writes.append((path, protected))
        previous = self._state.get(path, _Security(False, (), None))
        self._state[path] = _Security(protected, tuple(aces), previous.label)

    def write_label(self, path: str, label: _Label) -> None:
        self._record("write_label", path)
        self.writes.append(("label", path, label))
        previous = self._state.get(path, _Security(False, (), None))
        self._state[path] = _Security(previous.protected, previous.aces, label)

    # --- token ------------------------------------------------------------

    def open_process_token(self) -> int:
        self._record("open_process_token")
        return self._hand_out(BASE_TOKEN)

    def current_user_sid(self) -> str:
        self._record("current_user_sid")
        return USER_SID

    def current_logon_sid(self) -> str | None:
        self._record("current_logon_sid")
        return self.logon_sid

    def create_restricted_token(
        self, token, *, flags, disable_sids, restrict_sids
    ) -> int:
        self._record("create_restricted_token", token)
        self.token_calls.append(
            {
                "token": token,
                "flags": flags,
                "disable_sids": tuple(disable_sids),
                "restrict_sids": tuple(restrict_sids),
            }
        )
        return self._hand_out(RESTRICTED_TOKEN)

    def set_token_integrity_level(self, token: int, sid: str) -> None:
        self._record("set_token_integrity_level", token)
        self.integrity.append((token, sid))

    def set_token_default_dacl(self, token, aces) -> None:
        self._record("set_token_default_dacl", token)
        self.default_dacls.append(tuple(aces))

    def duplicate_impersonation_token(self, token: int) -> int:
        self._record("duplicate_impersonation_token", token)
        return self._hand_out(IMPERSONATION_TOKEN)

    def access_check(self, token: int, path: str, desired_access: int) -> bool:
        self._record("access_check", token)
        self.access_requests.append((path, desired_access))
        return self.allowed

    # --- job and process --------------------------------------------------

    def create_job_object(self) -> int:
        self._record("create_job_object")
        return self._hand_out(JOB)

    def set_job_limits(self, job, *, limit_flags, ui_restrictions) -> None:
        self._record("set_job_limits", job)
        self.job_limits.append(
            {
                "job": job,
                "limit_flags": limit_flags,
                "ui_restrictions": ui_restrictions,
            }
        )

    def create_process_as_user(self, token, spec) -> _ProcessCreation:
        self._record("create_process_as_user", token)
        self.create_token = token
        self.spec = spec
        self._hand_out(PROCESS)
        self._hand_out(THREAD)
        return _ProcessCreation(CHILD_PID, CHILD_PID + 1, PROCESS, THREAD)

    def assign_process_to_job(self, job, creation) -> None:
        self._record("assign_process_to_job", (job, creation.process_handle))

    def resume_thread(self, creation) -> None:
        self._record("resume_thread", creation.thread_handle)

    def terminate_process(self, creation) -> None:
        self._record("terminate_process", creation.process_handle)
        self.terminated.append(creation.process_handle)

    def wait_process(self, creation, timeout_ms) -> bool:
        self._record("wait_process", timeout_ms)
        return self.signaled

    def process_exit_code(self, creation) -> int:
        self._record("process_exit_code", creation.process_handle)
        return self.exit_code

    def terminate_job(self, job: int) -> None:
        self._record("terminate_job", job)
        self.terminated.append(job)

    def close_handle(self, handle: int) -> None:
        self.events.append(("close_handle", handle))
        self.closed.append(handle)


def _slot(tmp_path: Path, name: str = "slot-a") -> tuple[str, str, str]:
    """A real canonical slot root under a fake repo: (repo, slots dir, root)."""

    slots = tmp_path.joinpath(*SLOT_RELATIVE)
    slots.mkdir(parents=True, exist_ok=True)
    root = slots / name
    root.mkdir(exist_ok=True)
    return str(tmp_path), str(slots), str(root)


def _component_path(repo: str, component: str) -> str:
    index = SLOT_RELATIVE.index(component)
    return os.path.join(repo, *SLOT_RELATIVE[: index + 1])


def _request(**overrides) -> RestrictedTokenRequest:
    fields = {
        "argv": ["cmd.exe", "/c", "echo hello"],
        "slot_sid": SID_A,
        "working_directory": os.path.join("slot", "workspace"),
    }
    fields.update(overrides)
    return RestrictedTokenRequest(**fields)


# --------------------------------------------------------------------------- #
# surface and import safety
# --------------------------------------------------------------------------- #


def test_the_public_surface_is_exactly_the_declared_api():
    assert wrt.__all__ == [
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
    for name in wrt.__all__:
        assert getattr(wrt, name) is not None
    assert issubclass(RestrictedTokenUnsupported, RuntimeError)
    assert RestrictedTokenProbe._fields == ("available", "reason")


def test_every_windll_binding_is_lazy_behind_one_seam_constructor():
    """Import safety is structural, not a comment: prove where WinDLL is read.

    The module is already imported by the time this runs, which is the first
    half of the claim on a non-Windows host.  The second half is that no future
    edit can move a binding to module scope without this failing.
    """

    tree = ast.parse(Path(wrt.__file__).read_text(encoding="utf-8"))
    holders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        attributes = {
            inner.attr for inner in ast.walk(node) if isinstance(inner, ast.Attribute)
        }
        if {"WinDLL", "windll", "wintypes"} & attributes:
            holders.append(node.name)
    assert holders == ["__init__"]
    for statement in tree.body:
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            continue
        dumped = ast.dump(statement)
        if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Expr)):
            assert "WinDLL" not in dumped and "windll" not in dumped


def test_there_is_no_unrestricted_process_creation_path():
    source = Path(wrt.__file__).read_text(encoding="utf-8")
    assert "CreateProcessW" not in source
    # Exactly two: the seam protocol's declaration and the ctypes boundary's.
    assert source.count("def create_process_as_user") == 2
    api = FakeWin32Api()
    process = launch_restricted(_request(), api=api)
    assert api.create_token == RESTRICTED_TOKEN
    process.close()


def test_the_receipt_and_the_request_are_frozen():
    receipt = SlotRootReceipt("root", SID_A, True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        receipt.changed = False
    request = _request()
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.slot_sid = SID_B


# --------------------------------------------------------------------------- #
# slot SID
# --------------------------------------------------------------------------- #


def test_slot_sid_is_deterministic_correctly_shaped_and_validated():
    first = slot_sid("repo-a", "slot-1")
    assert first == slot_sid("repo-a", "slot-1")
    assert first != slot_sid("repo-a", "slot-2")
    assert first != slot_sid("repo-b", "slot-1")
    # The NUL separator is what makes the pair unambiguous.
    assert slot_sid("ab", "c") != slot_sid("a", "bc")

    digest = hashlib.sha256(b"repo-a\x00slot-1").digest()
    authorities = struct.unpack("<4I", digest[:16])
    assert first == "S-1-0-" + "-".join(str(value) for value in authorities)
    assert first.split("-")[:3] == ["S", "1", "0"]
    assert len(first.split("-")) == 7
    assert all(0 <= value <= 0xFFFFFFFF for value in authorities)

    with pytest.raises(ValueError):
        slot_sid("", "slot-1")
    with pytest.raises(ValueError):
        slot_sid("repo-a", "")


@pytest.mark.parametrize(
    "candidate",
    [
        ADMINISTRATORS_SID,
        LOW_SID,
        USER_SID,
        "S-1-0-1-2-3",
        "S-1-0-1-2-3-4-5",
        "S-1-0-1-2-3-0004",
        "S-1-0-1-2-3-4294967296",
        "S-1-0-a-b-c-d",
        "S-1-0--1-2-3",
        "",
        17,
    ],
)
def test_a_sid_that_is_not_a_slot_sid_is_refused_by_every_entry_point(
    candidate, tmp_path
):
    repo, _slots, root = _slot(tmp_path)
    target = tmp_path / "thing.txt"
    target.write_text("x", encoding="utf-8")

    for call in (
        lambda api: prepare_slot_root(root, candidate, repo_root=repo, api=api),
        lambda api: launch_restricted(
            RestrictedTokenRequest(argv=["x"], slot_sid=candidate), api=api
        ),
        lambda api: restricted_access(candidate, str(target), "read", api=api),
    ):
        api = FakeWin32Api()
        with pytest.raises(RestrictedTokenUnsupported) as refusal:
            call(api)
        assert refusal.value.reason == "slot_sid_invalid"
        assert str(refusal.value).startswith("slot_sid_invalid")
        assert api.writes == []
        assert api.events == []


# --------------------------------------------------------------------------- #
# prepare_slot_root refusals -- every one before any write
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "candidate",
    [
        r"\\?\C:\repo\.aiworkhub\runtime\sandboxes\slots\a",
        r"\\.\pipe\slots",
        r"\\server\share\slots\a",
        "//server/share/slots/a",
    ],
)
def test_a_unc_or_device_path_is_refused_before_it_is_resolved(tmp_path, candidate):
    repo, _slots, _root = _slot(tmp_path)
    api = FakeWin32Api()
    with pytest.raises(RestrictedTokenUnsupported) as refusal:
        prepare_slot_root(candidate, SID_A, repo_root=repo, api=api)
    assert refusal.value.reason == "slot_root_unc_or_device_path"
    assert api.writes == []
    assert api.events == []


def test_a_root_that_is_not_exactly_one_component_under_slots_is_refused(tmp_path):
    repo, slots, root = _slot(tmp_path)
    nested = os.path.join(root, "deeper")
    os.makedirs(nested, exist_ok=True)
    candidates = [
        str(tmp_path / "elsewhere"),
        slots,
        os.path.dirname(slots),
        nested,
        os.path.join(slots, "a", "b"),
    ]
    for candidate in candidates:
        api = FakeWin32Api()
        with pytest.raises(RestrictedTokenUnsupported) as refusal:
            prepare_slot_root(candidate, SID_A, repo_root=repo, api=api)
        assert refusal.value.reason == "slot_root_outside_sandbox_root"
        assert api.writes == []

    # An alternate spelling of the same canonical root is the same root, and is
    # accepted: containment is decided after realpath, not on the input text.
    api = FakeWin32Api()
    receipt = prepare_slot_root(
        os.path.join(root, "."), SID_A, repo_root=repo, api=api
    )
    assert receipt.slot_root == os.path.realpath(root)


def test_a_sibling_repository_root_cannot_reach_this_ones_slots(tmp_path):
    repo, _slots, root = _slot(tmp_path)
    other = tmp_path / "other-repo"
    other.mkdir()
    api = FakeWin32Api()
    with pytest.raises(RestrictedTokenUnsupported) as refusal:
        prepare_slot_root(root, SID_A, repo_root=str(other), api=api)
    assert refusal.value.reason == "slot_root_outside_sandbox_root"
    assert api.writes == []
    assert repo == str(tmp_path)


def test_a_case_shifted_spelling_never_fails_the_containment_check(tmp_path):
    """Case must never decide containment: Windows paths are case-insensitive.

    On a case-sensitive filesystem the shouted path simply is not there, so the
    refusal is "not a directory".  What must not happen on either OS is
    "outside the sandbox root", which is the comparison under test.
    """

    repo, _slots, _root = _slot(tmp_path)
    shouted = os.path.join(
        repo, ".AIWORKHUB", "RUNTIME", "SANDBOXES", "SLOTS", "slot-a"
    )
    api = FakeWin32Api()
    try:
        prepare_slot_root(shouted, SID_A, repo_root=repo, api=api)
    except RestrictedTokenUnsupported as refusal:
        assert refusal.reason == "slot_root_not_directory"


def test_a_missing_or_non_directory_slot_root_is_refused(tmp_path):
    repo, slots, _root = _slot(tmp_path)
    plain = os.path.join(slots, "a-file")
    Path(plain).write_text("x", encoding="utf-8")
    for candidate in (os.path.join(slots, "absent"), plain):
        api = FakeWin32Api()
        with pytest.raises(RestrictedTokenUnsupported) as refusal:
            prepare_slot_root(candidate, SID_A, repo_root=repo, api=api)
        assert refusal.value.reason == "slot_root_not_directory"
        assert api.writes == []


@pytest.mark.parametrize(
    "component", ["", ".aiworkhub", "runtime", "sandboxes", "slots"]
)
def test_a_reparse_point_anywhere_in_the_chain_is_refused(tmp_path, component):
    repo, _slots, root = _slot(tmp_path)
    canonical = os.path.realpath(root)
    marked = canonical if not component else _component_path(repo, component)
    api = FakeWin32Api(reparse=[marked])
    with pytest.raises(RestrictedTokenUnsupported) as refusal:
        prepare_slot_root(root, SID_A, repo_root=repo, api=api)
    assert refusal.value.reason == "slot_root_reparse_point"
    assert os.path.normcase(refusal.value.detail) == os.path.normcase(marked)
    assert api.writes == []


def test_the_reparse_check_covers_the_requested_spelling_and_the_canonical_one(
    tmp_path,
):
    repo, _slots, root = _slot(tmp_path)
    chain = wrt._ancestor_chain(
        os.path.realpath(repo), root, os.path.realpath(root)
    )
    assert os.path.realpath(root) in chain
    for component in SLOT_RELATIVE:
        assert os.path.realpath(_component_path(repo, component)) in chain
    assert os.path.realpath(repo) not in chain


# --------------------------------------------------------------------------- #
# prepare_slot_root writes
# --------------------------------------------------------------------------- #


def test_the_root_dacl_is_written_before_the_label_and_withholds_delete(tmp_path):
    repo, _slots, root = _slot(tmp_path)
    api = FakeWin32Api()
    receipt = prepare_slot_root(root, SID_A, repo_root=repo, api=api)
    canonical = receipt.slot_root
    assert receipt == SlotRootReceipt(os.path.realpath(root), SID_A, True)

    order = [(kind, path) for kind, path, _detail in api.writes]
    assert order[0] == ("dacl", canonical)
    assert order.index(("dacl", canonical)) < order.index(("label", canonical))
    assert api.protected_writes[0] == (canonical, True)
    assert all(protected for _path, protected in api.protected_writes)

    aces = api.dacl_for(canonical)
    assert aces[:3] == (
        _Ace(USER_SID, FILE_ALL_ACCESS, INHERIT_OI_CI),
        _Ace(SYSTEM_SID, FILE_ALL_ACCESS, INHERIT_OI_CI),
        _Ace(ADMINISTRATORS_SID, FILE_ALL_ACCESS, INHERIT_OI_CI),
    )
    inherit_only = next(ace for ace in aces if ace.sid == SID_A and ace.flags)
    assert inherit_only == _Ace(SID_A, FILE_MODIFY, INHERIT_OI_CI_IO)

    root_ace = next(ace for ace in aces if ace.sid == SID_A and ace.flags == 0)
    assert root_ace.access == SLOT_ROOT_ACCESS
    assert root_ace.access & DELETE == 0
    assert root_ace.access & WRITE_DAC == 0
    assert root_ace.access & WRITE_OWNER == 0
    assert root_ace.access & GENERIC_READ == GENERIC_READ
    assert root_ace.access & 0x0002 and root_ace.access & 0x0004
    assert root_ace.access & 0x0100

    assert api.label_for(canonical) == _Label(LOW_SID, NO_WRITE_UP, INHERIT_OI_CI)


def test_an_already_canonical_slot_root_is_a_no_op_with_no_walk(tmp_path):
    repo, _slots, root = _slot(tmp_path)
    Path(root, "existing.txt").write_text("x", encoding="utf-8")
    api = FakeWin32Api()
    first = prepare_slot_root(root, SID_A, repo_root=repo, api=api)
    assert first.changed is True
    assert len(api.written_paths()) == 2

    api.writes.clear()
    api.protected_writes.clear()
    api.events.clear()
    second = prepare_slot_root(root, SID_A, repo_root=repo, api=api)
    assert second == SlotRootReceipt(os.path.realpath(root), SID_A, False)
    assert api.writes == []
    assert "write_dacl" not in api.names()
    assert "write_label" not in api.names()
    # No walk: the only reparse questions asked are about the ancestor chain.
    walked = {
        detail
        for name, detail in api.events
        if name == "is_reparse_point" and isinstance(detail, str)
    }
    assert os.path.join(os.path.realpath(root), "existing.txt") not in walked


def test_the_first_preparation_walk_skips_a_reparse_point_and_stays_in_subtree(
    tmp_path,
):
    repo, _slots, root = _slot(tmp_path)
    Path(root, "keep.txt").write_text("x", encoding="utf-8")
    sub = Path(root, "sub")
    sub.mkdir()
    Path(sub, "inner.txt").write_text("x", encoding="utf-8")
    linked = Path(root, "linked")
    linked.mkdir()
    Path(linked, "hidden.txt").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")

    api = FakeWin32Api(reparse=[os.path.realpath(str(linked))])
    canonical = prepare_slot_root(root, SID_A, repo_root=repo, api=api).slot_root
    written = api.written_paths()

    assert canonical in written
    assert os.path.join(canonical, "keep.txt") in written
    assert os.path.join(canonical, "sub") in written
    assert os.path.join(canonical, "sub", "inner.txt") in written
    # Never written, and never descended into.
    assert os.path.join(canonical, "linked") not in written
    assert os.path.join(canonical, "linked", "hidden.txt") not in written
    assert os.path.realpath(str(outside)) not in written
    for path in written:
        assert path == canonical or path.startswith(canonical + os.sep)


def test_a_pre_existing_child_becomes_writable_by_the_slot_token(tmp_path):
    repo, _slots, root = _slot(tmp_path)
    Path(root, "old.txt").write_text("x", encoding="utf-8")
    Path(root, "olddir").mkdir()
    api = FakeWin32Api()
    canonical = prepare_slot_root(root, SID_A, repo_root=repo, api=api).slot_root

    child_file = os.path.join(canonical, "old.txt")
    child_dir = os.path.join(canonical, "olddir")
    assert _Ace(SID_A, FILE_MODIFY, 0) in api.dacl_for(child_file)
    assert _Ace(SID_A, FILE_MODIFY, INHERIT_OI_CI) in api.dacl_for(child_dir)
    assert api.label_for(child_file) == _Label(LOW_SID, NO_WRITE_UP, 0)
    assert api.label_for(child_dir) == _Label(LOW_SID, NO_WRITE_UP, INHERIT_OI_CI)


def test_nothing_this_module_writes_names_a_sid_broader_than_the_slot(tmp_path):
    repo, _slots, root = _slot(tmp_path)
    Path(root, "child.txt").write_text("x", encoding="utf-8")
    api = FakeWin32Api()
    prepare_slot_root(root, SID_A, repo_root=repo, api=api)
    process = launch_restricted(_request(), api=api)
    process.close()

    broad = {
        "S-1-1-0",  # Everyone
        "S-1-5-11",  # Authenticated Users
        "S-1-5-32-545",  # BUILTIN\\Users
        "S-1-15-2-1",  # ALL APPLICATION PACKAGES
        "S-1-15-2-2",  # ALL RESTRICTED APPLICATION PACKAGES
    }
    granted: set[str] = set()
    for kind, _path, detail in api.writes:
        if kind == "dacl":
            assert isinstance(detail, tuple)
            granted.update(ace.sid for ace in detail)
    for aces in api.default_dacls:
        granted.update(ace.sid for ace in aces)
    assert granted.isdisjoint(broad)
    assert granted == {USER_SID, SYSTEM_SID, ADMINISTRATORS_SID, SID_A}


def test_two_slots_get_disjoint_slot_aces(tmp_path):
    repo, _slots, root_a = _slot(tmp_path, "slot-a")
    _repo, _slots_b, root_b = _slot(tmp_path, "slot-b")
    api = FakeWin32Api()
    canonical_a = prepare_slot_root(root_a, SID_A, repo_root=repo, api=api).slot_root
    canonical_b = prepare_slot_root(root_b, SID_B, repo_root=repo, api=api).slot_root
    assert SID_A != SID_B
    assert SID_B not in {ace.sid for ace in api.dacl_for(canonical_a)}
    assert SID_A not in {ace.sid for ace in api.dacl_for(canonical_b)}


# --------------------------------------------------------------------------- #
# launch_restricted
# --------------------------------------------------------------------------- #


def test_a_successful_launch_restricts_exactly_the_model_sid_list():
    api = FakeWin32Api()
    process = launch_restricted(_request(), api=api)
    call = api.token_calls[0]
    assert call["token"] == BASE_TOKEN
    assert call["flags"] == 0x00000001  # DISABLE_MAX_PRIVILEGE
    assert call["disable_sids"] == (ADMINISTRATORS_SID,)
    assert call["restrict_sids"] == (
        "S-1-1-0",
        "S-1-5-32-545",
        "S-1-5-11",
        "S-1-5-12",
        LOGON_SID,
        SID_A,
    )
    # The token user SID is deliberately not restricting.
    assert USER_SID not in call["restrict_sids"]
    process.close()


def test_a_token_without_a_logon_sid_omits_it_rather_than_inventing_one():
    api = FakeWin32Api(logon_sid=None)
    process = launch_restricted(_request(), api=api)
    assert api.token_calls[0]["restrict_sids"] == (
        "S-1-1-0",
        "S-1-5-32-545",
        "S-1-5-11",
        "S-1-5-12",
        SID_A,
    )
    process.close()


def test_low_integrity_and_the_default_dacl_precede_process_creation():
    api = FakeWin32Api()
    process = launch_restricted(_request(), api=api)
    assert api.integrity == [(RESTRICTED_TOKEN, LOW_SID)]
    assert api.default_dacls == [
        (
            _Ace(USER_SID, GENERIC_ALL, 0),
            _Ace(SYSTEM_SID, GENERIC_ALL, 0),
            _Ace(SID_A, GENERIC_ALL, 0),
        )
    ]
    names = api.names()
    assert names.index("set_token_integrity_level") < names.index(
        "create_process_as_user"
    )
    assert names.index("set_token_default_dacl") < names.index(
        "create_process_as_user"
    )
    assert api.create_token == RESTRICTED_TOKEN
    process.close()


def test_the_child_joins_the_job_before_it_runs_with_the_model_ui_mask():
    api = FakeWin32Api()
    process = launch_restricted(_request(), api=api)
    names = api.names()
    assert names.index("create_job_object") < names.index("create_process_as_user")
    assert names.index("set_job_limits") < names.index("create_process_as_user")
    assert names.index("assign_process_to_job") < names.index("resume_thread")
    assert api.job_limits == [
        {
            "job": JOB,
            "limit_flags": 0x00002000,  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            "ui_restrictions": 0x000000FF,  # the model's eight UI limits
        }
    ]
    assert api.spec.creation_flags & 0x00000004  # CREATE_SUSPENDED
    assert api.spec.creation_flags & 0x00000400  # CREATE_UNICODE_ENVIRONMENT
    assert api.spec.creation_flags & 0x00080000  # EXTENDED_STARTUPINFO_PRESENT
    assert api.spec.creation_flags & 0x08000000  # CREATE_NO_WINDOW
    process.close()


def test_create_no_window_false_drops_only_that_flag():
    api = FakeWin32Api()
    process = launch_restricted(_request(create_no_window=False), api=api)
    assert api.spec.creation_flags & 0x08000000 == 0
    assert api.spec.creation_flags & 0x00000004
    process.close()


def test_only_the_std_handles_are_inheritable_and_duplicates_collapse():
    api = FakeWin32Api()
    process = launch_restricted(
        _request(stdin_handle=7, stdout_handle=8, stderr_handle=9), api=api
    )
    assert api.spec.handle_list == (7, 8, 9)
    process.close()

    api = FakeWin32Api()
    process = launch_restricted(
        _request(stdout_handle=8, stderr_handle=8), api=api
    )
    assert api.spec.handle_list == (8,)
    process.close()

    api = FakeWin32Api()
    process = launch_restricted(_request(), api=api)
    assert api.spec.handle_list == ()
    process.close()


def test_the_command_line_is_the_appcontainer_quoting_with_no_shell():
    api = FakeWin32Api()
    argv = ["C:\\tools\\x.exe", 'a "b"', "c\\", ""]
    process = launch_restricted(_request(argv=argv), api=api)
    assert api.spec.command_line == wac.build_command_line(argv)
    assert api.spec.executable == argv[0]
    process.close()

    api = FakeWin32Api()
    process = launch_restricted(
        _request(executable="C:\\other\\real.exe"), api=api
    )
    assert api.spec.executable == "C:\\other\\real.exe"
    process.close()


def test_environment_none_inherits_the_callers_block_like_appcontainer_does():
    api = FakeWin32Api()
    process = launch_restricted(_request(), api=api)
    assert api.spec.environment is None
    process.close()
    # The documented behaviour, pinned against the module it is copied from:
    # None means "pass NULL lpEnvironment", i.e. inherit, not "start empty".
    assert wrt._environment_block(None) is None
    assert wac._environment_block_text(None) is None

    api = FakeWin32Api()
    process = launch_restricted(_request(environment={"A": "b"}), api=api)
    assert api.spec.environment == {"A": "b"}
    assert wrt._environment_block({"A": "b"}) is not None
    assert wac._environment_block_text({"A": "b"}) == "A=b\x00\x00"
    process.close()


def test_an_embedded_nul_in_the_environment_is_refused_before_a_handle_opens():
    for environment in ({"A": "b\x00c"}, {"A\x00": "b"}):
        api = FakeWin32Api()
        with pytest.raises(ValueError):
            launch_restricted(_request(environment=environment), api=api)
        assert api.events == []


def test_an_empty_argv_is_a_value_error():
    with pytest.raises(ValueError):
        RestrictedTokenRequest(argv=(), slot_sid=SID_A)
    with pytest.raises(ValueError):
        RestrictedTokenRequest(argv=[], slot_sid=SID_A)


FAILURE_REASON = {
    "open_process_token": "open_token_failed",
    "current_user_sid": "token_identity_failed",
    "current_logon_sid": "token_identity_failed",
    "create_restricted_token": "create_restricted_token_failed",
    "set_token_integrity_level": "set_integrity_level_failed",
    "set_token_default_dacl": "set_default_dacl_failed",
    "create_job_object": "create_job_failed",
    "set_job_limits": "set_job_limits_failed",
    "create_process_as_user": "create_process_failed",
    "assign_process_to_job": "assign_job_failed",
    "resume_thread": "resume_thread_failed",
}


@pytest.mark.parametrize("failure", sorted(FAILURE_REASON))
def test_every_launch_failure_point_unwinds_completely(failure):
    api = FakeWin32Api(fail_at=failure, win_error=1314)
    with pytest.raises(RestrictedTokenUnsupported) as refusal:
        launch_restricted(_request(), api=api)

    assert refusal.value.reason == FAILURE_REASON[failure]
    assert str(refusal.value).startswith(FAILURE_REASON[failure])
    assert WIN32_CALL[failure] in str(refusal.value)
    assert "1314" in str(refusal.value)

    # Every handle handed out was closed, exactly once.
    assert sorted(api.closed) == sorted(api.opened)
    assert len(set(api.closed)) == len(api.closed)
    # No less-restricted retry: the restricted token is the only one ever used
    # for a creation, and nothing was created twice.
    assert api.create_token in (None, RESTRICTED_TOKEN)
    assert api.names().count("create_process_as_user") <= 1
    assert api.writes == []

    if failure in ("assign_process_to_job", "resume_thread"):
        # The child already exists, so it is terminated before any handle goes.
        assert api.terminated == [PROCESS]
        terminated_at = api.events.index(("terminate_process", PROCESS))
        closed_at = api.events.index(("close_handle", PROCESS))
        assert terminated_at < closed_at
    else:
        assert api.terminated == []


def test_a_failure_before_the_child_exists_never_terminates_anything():
    api = FakeWin32Api(fail_at="create_process_as_user")
    with pytest.raises(RestrictedTokenUnsupported):
        launch_restricted(_request(), api=api)
    assert api.terminated == []
    assert api.create_token is None
    assert sorted(api.closed) == sorted(api.opened) == [BASE_TOKEN, RESTRICTED_TOKEN, JOB]


def test_poll_wait_terminate_and_close_semantics():
    api = FakeWin32Api(signaled=False)
    process = launch_restricted(_request(), api=api)
    assert process.pid == CHILD_PID
    assert api.closed == [BASE_TOKEN]

    assert process.poll() is None
    with pytest.raises(subprocess.TimeoutExpired):
        process.wait(0.01)

    api.signaled = True
    api.exit_code = 7
    assert process.wait() == 7
    assert process.returncode == 7
    assert process.poll() == 7
    # A terminal outcome is remembered, never re-measured.
    assert api.names().count("process_exit_code") == 1

    process.terminate()
    assert api.terminated == [JOB]
    process.kill()
    assert api.terminated == [JOB, JOB]

    process.close()
    expected = [BASE_TOKEN, THREAD, PROCESS, RESTRICTED_TOKEN, JOB]
    assert api.closed == expected
    # Idempotent, and the job goes last so its close kills any survivor.
    process.close()
    assert api.closed == expected
    assert api.closed[-1] == JOB
    assert sorted(api.closed) == sorted(api.opened)


def test_terminate_after_close_is_a_no_op():
    api = FakeWin32Api()
    process = launch_restricted(_request(), api=api)
    process.close()
    process.terminate()
    process.kill()
    assert api.terminated == []


def test_a_wait_failure_is_a_typed_refusal_naming_the_call():
    api = FakeWin32Api(fail_at="wait_process", win_error=6)
    process = launch_restricted(_request(), api=api)
    with pytest.raises(RestrictedTokenUnsupported) as refusal:
        process.poll()
    assert refusal.value.reason == "wait_failed"
    assert "WaitForSingleObject" in str(refusal.value)
    process.close()


def test_a_terminate_failure_is_a_typed_refusal_naming_the_call():
    api = FakeWin32Api(fail_at="terminate_job", win_error=6)
    process = launch_restricted(_request(), api=api)
    with pytest.raises(RestrictedTokenUnsupported) as refusal:
        process.terminate()
    assert refusal.value.reason == "terminate_job_failed"
    assert "TerminateJobObject" in str(refusal.value)
    process.close()


# --------------------------------------------------------------------------- #
# restricted_access and probe
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "access,mask",
    [
        ("read", GENERIC_READ),
        ("read_execute", GENERIC_READ_EXECUTE),
        ("write", GENERIC_WRITE),
    ],
)
def test_restricted_access_maps_each_name_onto_one_file_right(tmp_path, access, mask):
    target = tmp_path / "thing.txt"
    target.write_text("x", encoding="utf-8")
    api = FakeWin32Api()
    assert restricted_access(SID_A, str(target), access, api=api) is True

    assert api.access_requests == [(str(target), mask)]
    assert api.token_calls[0]["restrict_sids"][-1] == SID_A
    assert api.integrity == [(RESTRICTED_TOKEN, LOW_SID)]
    assert api.names().count("duplicate_impersonation_token") == 1
    # Non-mutating: nothing launched, nothing written.
    assert "create_process_as_user" not in api.names()
    assert api.writes == []
    assert sorted(api.closed) == sorted(api.opened)


@pytest.mark.parametrize("access", ["execute", "", "READ", "read_write", None])
def test_restricted_access_refuses_an_unknown_access_name(tmp_path, access):
    api = FakeWin32Api()
    with pytest.raises(ValueError):
        restricted_access(SID_A, str(tmp_path), access, api=api)
    assert api.events == []


def test_restricted_access_answers_false_for_a_missing_path(tmp_path):
    api = FakeWin32Api()
    assert restricted_access(SID_A, str(tmp_path / "absent"), "read", api=api) is False
    assert api.events == []


def test_restricted_access_reports_a_denied_check_as_false(tmp_path):
    target = tmp_path / "thing.txt"
    target.write_text("x", encoding="utf-8")
    api = FakeWin32Api(allowed=False)
    assert restricted_access(SID_A, str(target), "read", api=api) is False
    assert sorted(api.closed) == sorted(api.opened)


def test_restricted_access_closes_every_handle_when_the_check_fails(tmp_path):
    target = tmp_path / "thing.txt"
    target.write_text("x", encoding="utf-8")
    api = FakeWin32Api(fail_at="access_check", win_error=5)
    with pytest.raises(RestrictedTokenUnsupported) as refusal:
        restricted_access(SID_A, str(target), "read", api=api)
    assert refusal.value.reason == "access_check_failed"
    assert sorted(api.closed) == sorted(api.opened)
    assert IMPERSONATION_TOKEN in api.closed


def test_probe_reports_not_windows_off_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    assert probe() == (False, "not_windows")
    assert probe() == RestrictedTokenProbe(False, "not_windows")


def test_probe_builds_and_closes_a_token_and_mutates_nothing():
    api = FakeWin32Api()
    assert probe(api=api) == (True, "available")
    assert api.integrity == [(RESTRICTED_TOKEN, LOW_SID)]
    assert sorted(api.closed) == sorted(api.opened) == [BASE_TOKEN, RESTRICTED_TOKEN]
    assert api.writes == []
    assert "create_process_as_user" not in api.names()


@pytest.mark.parametrize(
    "failure", ["open_process_token", "create_restricted_token", "set_token_integrity_level"]
)
def test_probe_names_the_failing_call_and_its_last_error(failure):
    api = FakeWin32Api(fail_at=failure, win_error=1314)
    result = probe(api=api)
    assert result.available is False
    assert result.reason.startswith(FAILURE_REASON[failure])
    assert WIN32_CALL[failure] in result.reason
    assert "1314" in result.reason
    assert sorted(api.closed) == sorted(api.opened)


def test_the_boundary_refuses_to_load_off_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "name", "posix")
    repo, _slots, root = _slot(tmp_path)
    target = tmp_path / "thing.txt"
    target.write_text("x", encoding="utf-8")
    calls = [
        lambda: wrt._load_win32(),
        lambda: prepare_slot_root(root, SID_A, repo_root=repo),
        lambda: launch_restricted(_request()),
        lambda: restricted_access(SID_A, str(target), "read"),
    ]
    for call in calls:
        with pytest.raises(RestrictedTokenUnsupported) as refusal:
            call()
        assert refusal.value.reason == "not_windows"


def test_scrub_matches_every_backslash_and_slash_spelling_of_the_home_prefix(monkeypatch):
    home = "C:\\Users\\probe-user"
    monkeypatch.setenv("USERPROFILE", home)
    monkeypatch.setattr(os.path, "expanduser", lambda _path: home)

    spec = importlib.util.spec_from_file_location(
        "test_windows_restricted_token_live",
        Path(__file__).with_name("test_windows_restricted_token_live.py"),
    )
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)

    doubled = home.replace("\\", "\\\\")
    quadrupled = home.replace("\\", "\\\\\\\\")
    forward = home.replace("\\", "/")

    for spelling in (home, doubled, quadrupled, forward):
        scrubbed = live._scrub(f"path={spelling}\\report.log")
        assert "<home>" in scrubbed
        assert "probe-user" not in scrubbed

    assert live._scrub("no home prefix here") == "no home prefix here"
