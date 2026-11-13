"""Manager card tools that core.py has no line headroom for (NF-2026-02241).

Four gaps a downstream manager reported, owned here so ``core``/``server`` keep
only call sites and schemas:

1. ``amend_pending`` -- add-only ``allowed_writes``/``read_first`` and a
   replacement ``validation`` for a pending card no request was ever launched
   for, under the same rules task_create and reject_review already apply.
2. ``correction_allowed_writes_amendment`` and friends -- an accept-time
   ``manager_correction`` may widen ``allowed_writes`` by exact paths its own
   patch touches (e.g. a neighbour test the accept revalidation showed red).
3. ``evidence`` -- a bounded card field (``EVIDENCE_MAX_CHARS``) carried into
   the worker's task contract after the objective.
4. ``review_state_fields`` -- reject depth against its cap and the per-lens
   required-reviewer state, derived exactly as accept_review derives
   ``required_reviewer_missing``.

Every refusal names its cause; nothing here infers a value the caller did not
declare.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import threading
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from . import task_store

# The card ``evidence`` field: same strip-then-measure rule as the objective
# (``task_templates._bounded_text``), its own larger cap. The objective keeps
# its own cap untouched.
EVIDENCE_MAX_CHARS = 16000
# Whitespace a multi-line text legitimately carries; every other C0 control
# (NUL included) and DEL is refused rather than handed to a worker prompt.
_EVIDENCE_ALLOWED_CONTROLS = frozenset("\t\n\r")

AMEND_PENDING_SCHEMA_ID = "aiworkhub.task_amend_pending_receipt.v1"
AMEND_PENDING_GATE_ACTION = "amend-pending-card"


# ---------------------------------------------------------------- evidence


def _evidence_control_index(text: str) -> int:
    for index, char in enumerate(text):
        code = ord(char)
        if (code < 0x20 or code == 0x7F) and char not in _EVIDENCE_ALLOWED_CONTROLS:
            return index
    return -1


def normalize_evidence(value: Any) -> str | None:
    """The stripped evidence text, or ``None`` when absent or blank."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def evidence_violations(value: Any) -> list[dict[str, Any]]:
    """task_create-shaped violations for one ``evidence`` value (empty when valid).

    ``None`` and blank text mean "no evidence" and are not violations.
    """
    if value is None:
        return []
    if not isinstance(value, str):
        return [{
            "code": f"invalid_evidence:not_a_string:{type(value).__name__}",
            "field": "evidence",
        }]
    text = value.strip()
    if len(text) > EVIDENCE_MAX_CHARS:
        return [{
            "code": (
                f"invalid_evidence:too_long:{len(text)}_chars_exceeds_limit_"
                f"{EVIDENCE_MAX_CHARS}"
            ),
            "field": "evidence",
            "length": len(text),
            "limit": EVIDENCE_MAX_CHARS,
        }]
    index = _evidence_control_index(text)
    if index >= 0:
        return [{
            "code": f"invalid_evidence:control_character:U+{ord(text[index]):04X}",
            "field": "evidence",
            "index": index,
        }]
    return []


def evidence_card_fields(value: Any) -> dict[str, str]:
    """``{"evidence": text}`` for a declared value, else ``{}`` (absent stays absent)."""
    text = normalize_evidence(value)
    return {"evidence": text} if text else {}


# NeedFix conversion: the NeedFix plan schema (``needfix_store``) has no
# ``evidence`` field, so the server splits it off before the plan is signed and
# remembers it under the preview's ``plan_digest`` for a digest-only commit.
# Every preview REPLACES what its digest remembers: the text cache holds the
# evidence itself (bounded, LRU), and the marker map holds what that preview
# DECLARED -- evidence, or evidence it refused. Markers are one short string
# per digest and outlive both the text cache and core's previewed-plan cache
# (``PREVIEWED_PLAN_CACHE_MAX_ENTRIES``), so a digest whose text was evicted is
# still known to have carried evidence and its convert refuses by name instead
# of committing a card without it. After a restart both are empty, and a
# digest-only convert is already refused with ``previewed_plan_unavailable``.
_PREVIEW_EVIDENCE_LIMIT = 64
_PREVIEW_EVIDENCE_MARKER_LIMIT = 1024
_MARKER_EVIDENCE = "evidence"
_MARKER_INVALID = "invalid"
_PREVIEW_EVIDENCE: OrderedDict[str, str] = OrderedDict()
_PREVIEW_EVIDENCE_MARKERS: OrderedDict[str, str] = OrderedDict()
_PREVIEW_EVIDENCE_LOCK = threading.Lock()
PREVIEW_EVIDENCE_EXPIRED = "preview_evidence_expired:re-run needfix_preview_convert"
PREVIEW_EVIDENCE_INVALID = "preview_evidence_invalid:re-run needfix_preview_convert"


class PreviewEvidenceUnavailable(ValueError):
    """A digest-only convert whose preview declared evidence this server cannot supply."""


def split_plan_evidence(plan: Any) -> tuple[Any, Any]:
    """``(plan_without_evidence, evidence_or_None)``; the plan is never mutated."""
    if not isinstance(plan, Mapping) or "evidence" not in plan:
        return plan, None
    rest = {key: value for key, value in plan.items() if key != "evidence"}
    return rest, plan.get("evidence")


def _remember_preview_evidence(digest: str, marker: str | None, text: str | None) -> None:
    """Replace everything ``digest`` remembers; ``None`` forgets that part."""
    with _PREVIEW_EVIDENCE_LOCK:
        if marker is None:
            _PREVIEW_EVIDENCE_MARKERS.pop(digest, None)
        else:
            _PREVIEW_EVIDENCE_MARKERS[digest] = marker
            _PREVIEW_EVIDENCE_MARKERS.move_to_end(digest)
            while len(_PREVIEW_EVIDENCE_MARKERS) > _PREVIEW_EVIDENCE_MARKER_LIMIT:
                _PREVIEW_EVIDENCE_MARKERS.popitem(last=False)
        if text is None:
            _PREVIEW_EVIDENCE.pop(digest, None)
        else:
            _PREVIEW_EVIDENCE[digest] = text
            _PREVIEW_EVIDENCE.move_to_end(digest)
            while len(_PREVIEW_EVIDENCE) > _PREVIEW_EVIDENCE_LIMIT:
                _PREVIEW_EVIDENCE.popitem(last=False)


def bind_preview_evidence(preview: Any, evidence: Any) -> Any:
    """Validate plan evidence on a preview reply and remember it by digest.

    Every preview with a ``plan_digest`` replaces what that digest remembered,
    so evidence from an EARLIER preview of the same digest never reaches a
    digest-only convert. An invalid value refuses the preview by name
    (``refusal_reason``) and marks the digest refused; a valid one is
    remembered (bounded, least-recently-used eviction) and its length is
    reported as ``evidence_chars``; no or blank evidence forgets the digest.
    """
    if not isinstance(preview, dict):
        return preview
    digest = str(preview.get("plan_digest") or "")
    if evidence is None:
        if digest:
            _remember_preview_evidence(digest, None, None)
        return preview
    reply = dict(preview)
    violations = evidence_violations(evidence)
    if violations:
        if digest:
            _remember_preview_evidence(digest, _MARKER_INVALID, None)
        reply["refusal_reason"] = violations[0]["code"]
        reply["evidence_violations"] = violations
        return reply
    text = normalize_evidence(evidence)
    if digest:
        _remember_preview_evidence(digest, _MARKER_EVIDENCE if text else None, text)
    reply["evidence_chars"] = len(text or "")
    return reply


def resolve_convert_evidence(evidence: Any, plan_digest: Any) -> Any:
    """Explicit plan evidence wins; otherwise the evidence its preview remembered.

    A digest whose preview declared no evidence resolves to ``None``. One whose
    preview declared evidence that is no longer held (evicted) raises
    ``PreviewEvidenceUnavailable(PREVIEW_EVIDENCE_EXPIRED)``, and one whose
    preview refused its evidence raises it with ``PREVIEW_EVIDENCE_INVALID``:
    the card is never created silently without the evidence its preview showed.
    """
    if evidence is not None:
        return evidence
    digest = str(plan_digest or "")
    if not digest:
        return None
    with _PREVIEW_EVIDENCE_LOCK:
        marker = _PREVIEW_EVIDENCE_MARKERS.get(digest)
        if marker is None:
            return None
        _PREVIEW_EVIDENCE_MARKERS.move_to_end(digest)
        if marker == _MARKER_INVALID:
            raise PreviewEvidenceUnavailable(PREVIEW_EVIDENCE_INVALID)
        text = _PREVIEW_EVIDENCE.get(digest)
        if text is None:
            raise PreviewEvidenceUnavailable(PREVIEW_EVIDENCE_EXPIRED)
        _PREVIEW_EVIDENCE.move_to_end(digest)
        return text


def convert_evidence_or_refusal(
    evidence: Any, plan_digest: Any
) -> tuple[Any, dict[str, Any] | None]:
    """``(evidence, None)`` for needfix_convert, or ``(None, refusal)`` naming why not."""
    try:
        return resolve_convert_evidence(evidence, plan_digest), None
    except PreviewEvidenceUnavailable as exc:
        return None, _refusal(str(exc), plan_digest=str(plan_digest or ""))


# ------------------------------------------------------ amend unlaunched pending


def _refusal(code: str, returncode: int = 2, **extra: Any) -> dict[str, Any]:
    from . import core

    result = core._lifecycle_error(code, returncode)
    result["error"] = code
    result.update(extra)
    return result


def runner_route_remedy_hint(runner: str) -> str:
    """The create receipt's remedy for an unroutable runner (NF-2026-02252)."""
    return (
        f"runner {runner!r} has no workforce route, so a launch will refuse it; "
        "if this card is meant for a launchable worker, correct it before the "
        "first launch with aiworkhub_task_amend_pending runner=<routable runner> "
        "(e.g. claude_opus-5) instead of archiving and recreating the card"
    )


def amend_runner_refusal(runner: str) -> tuple[str, str]:
    """``(folded_runner, refusal)`` for a runner amendment; refusal ``""`` when allowed.

    Folds with ``core.resolve_create_time_runner`` (the create-time fold),
    refuses the reserved manager identity ``core.CODEX_RUNNER``, and -- strict
    where create stays advisory, because an amend is an explicit correction --
    refuses any runner ``core.create_time_runner_route_warnings`` names.
    """
    from . import core

    folded, named_refusal = core.resolve_create_time_runner(str(runner or "").strip())
    if named_refusal is not None:
        code = named_refusal.get("error") or named_refusal.get("reason")
        return folded, str(code or f"runner_refused:{folded}")
    if folded == core.CODEX_RUNNER:
        return folded, f"runner_reserved:{folded}"
    if core.create_time_runner_route_warnings(folded):
        return folded, f"runner_route_absent:{folded}"
    return folded, ""


def amend_pending(
    task_id: str,
    *,
    allowed_writes: list[str] | None = None,
    read_first: list[str] | None = None,
    validation: list[str] | None = None,
    validation_roles: list[str] | None = None,
    runner: str | None = None,
) -> dict[str, Any]:
    """Amend one UNLAUNCHED pending card: add-only paths, replacement validation.

    Gates exactly as task_create: a live manager identity, the canonical write
    gate and the coordinator capability. Paths and commands are validated with
    the helpers reject_review's amendments use (so a refusal carries the same
    code), validation replacement is dry-run through task_store's own
    ``apply_validation_amendment_mode`` (work_kind role gate included), and the
    store then re-decides eligibility inside its compare-and-swap transaction.
    A launched, claimed, archived or non-pending card refuses with
    ``task_amend_pending_refused:<card_already_launched|card_claimed|
    card_archived|card_not_pending:<state>>``.

    ``runner`` (NF-2026-02252) replaces the card's runner, checked by
    ``amend_runner_refusal``: ``task_amend_pending_refused:runner_reserved:<r>``
    for the manager identity, ``...:runner_route_absent:<r>`` for a runner with
    no workforce route. The receipt carries ``runner_before``/``runner_after``.
    """
    from . import core, quality_evidence, task_templates

    task_id = str(task_id or "").strip()
    if not task_id:
        return _refusal("invalid_task_id")
    if core._live_manager_identity() is None:
        return _refusal("manager_identity_required:task_amend_pending", 126)
    blocked = core._canonical_write_gate(AMEND_PENDING_GATE_ACTION)
    if blocked is not None:
        return blocked
    capability_ok, capability_reason = core._verify_coordinator_capability(core.CODEX_RUNNER)
    if not capability_ok:
        return _refusal(capability_reason, 126)
    writes = [str(value) for value in (allowed_writes or [])]
    reads = [str(value) for value in (read_first or [])]
    commands = [str(value) for value in (validation or [])]
    requested_runner = str(runner or "").strip()
    if validation_roles is not None and not commands:
        return _refusal("validation_roles_require_validation")
    if not (writes or reads or commands or requested_runner):
        return _refusal("task_amend_pending_empty")
    root = core.repo_root()
    card = task_store.get_task(root, task_id)
    if not isinstance(card, dict):
        return _refusal("task_amend_pending_refused:task_not_found", task_id=task_id)
    refusal = task_store.unlaunched_pending_refusal(card, card)
    if refusal:
        return _refusal(f"task_amend_pending_refused:{refusal}", task_id=task_id)
    folded_runner = ""
    if requested_runner:
        folded_runner, runner_refusal = amend_runner_refusal(requested_runner)
        if runner_refusal:
            return _refusal(f"task_amend_pending_refused:{runner_refusal}", task_id=task_id)
    if writes:
        if card.get("read_only") is True:
            return _refusal("read_only_allowed_writes_forbidden", task_id=task_id)
        path_error = core._validate_allowed_writes_amendment_paths(card, writes)
        if path_error is not None:
            return path_error
    validated_reads: list[str] = []
    if reads:
        validated_reads, read_error = core._validate_read_first_amendment_paths(reads)
        if read_error is not None:
            return read_error
    roles: list[str] | None = None
    if commands:
        command_error = core._validate_validation_amendment_commands(commands)
        if command_error is not None:
            return command_error
        if validation_roles is not None:
            roles = [str(value).strip().lower() for value in validation_roles]
        elif isinstance(card.get("validation_roles"), list):
            # The card carries roles, so a replacement must align them; an
            # undeclared list is the generic role per command, which the
            # work_kind gate below still refuses for a specialized card.
            roles = [quality_evidence.VALIDATION_ROLE_GENERIC] * len(commands)
        try:
            task_templates.validate_custom_validation_roles(commands, roles or [])
        except task_templates.TaskTemplateError as exc:
            return _refusal(str(exc), task_id=task_id)
        try:
            task_store.apply_validation_amendment_mode(
                dict(card), commands, mode="replace", roles=roles
            )
        except ValueError as exc:
            return _refusal(str(exc), task_id=task_id)
    actor = core._verified_manager_actor()
    ok, reason, detail = task_store.amend_pending_card(
        root,
        task_id,
        actor=actor,
        allowed_writes=writes,
        read_first=validated_reads,
        validation=commands or None,
        validation_roles=roles,
        runner=folded_runner or None,
    )
    if not ok:
        return _refusal(f"task_amend_pending_refused:{reason}", task_id=task_id)
    reply = {
        "ok": True,
        "returncode": 0,
        "schema_id": AMEND_PENDING_SCHEMA_ID,
        "task_id": task_id,
        "state": reason,
        "actor": actor,
        **detail,
    }
    reply["stdout"] = json.dumps(
        {key: reply[key] for key in ("task_id", "state", "fields")}, sort_keys=True
    )
    reply["stderr"] = ""
    return reply


# ------------------------------------- accept-time allowed_writes amendment


_GLOB_CHARS = "*?["
# The validated correction record's map of amended path -> sha256 of the
# CANONICAL bytes the corrected content was derived from (paths outside the
# sealed candidate only); ``widen_workspace_for_correction`` re-checks it.
CANONICAL_PREIMAGE_KEY = "paths_outside_candidate_canonical_sha256"


def receipt_with_amended_paths(
    receipt: Mapping[str, Any], record: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The accepted-outcome receipt naming the correction's amended paths.

    The validated correction record's ``paths_outside_candidate`` -- corrected
    paths the sealed candidate never changed, admitted by the amendment -- are
    written into the receipt's ``manager_correction`` as the sorted
    ``amended_paths`` list and ``receipt_id`` is re-derived over the result, so
    ``task_engine`` can admit exactly those unsealed paths. Without such a path
    the receipt is returned unchanged (byte-identical to an accept without an
    amendment).
    """
    outside = sorted(
        {str(path) for path in ((record or {}).get("paths_outside_candidate") or ())}
    )
    correction = receipt.get("manager_correction")
    if not outside or not isinstance(correction, Mapping):
        return dict(receipt)
    from . import task_engine

    unsigned = {key: value for key, value in receipt.items() if key != "receipt_id"}
    unsigned["manager_correction"] = {**correction, "amended_paths": outside}
    return {
        **unsigned,
        "receipt_id": "sha256:" + task_engine._canonical_json_hash(unsigned),
    }


class AmendedPathRefused(ValueError):
    """An amended path the accept tree must not seed, correct or promote."""


class AmendedPathCanonicalChanged(AmendedPathRefused):
    """An amended path's canonical bytes are not the ones its correction was derived from."""


class AmendedPathOutsideWorkspace(AmendedPathRefused):
    """An amended path that is not a plain relative path contained in its root."""


# Windows FILE_ATTRIBUTE_REPARSE_POINT: a junction (or any reparse point) on
# every Python version, where ``Path.is_junction`` only exists from 3.12.
_REPARSE_POINT_ATTRIBUTE = 0x400


def _is_link_or_junction(path: Path) -> bool:
    """True for a symlink, junction or other reparse point; False when absent."""
    if os.path.islink(path):
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & _REPARSE_POINT_ATTRIBUTE)


def amended_path_outside_workspace(relative: str, root: Path | None = None) -> str:
    """``amended_path_outside_workspace:<path>`` for an unsafe amended path, else ``""``.

    Refused lexically (no filesystem needed): an empty, absolute, drive-,
    root- or UNC-prefixed path, or one with a ``..`` part, under both POSIX and
    Windows rules. With ``root`` it is also refused when it resolves outside
    the resolved root, or when any existing component below the root (the
    path itself included) is a symlink or junction a write could follow.
    """
    refusal = f"amended_path_outside_workspace:{relative}"[:200]
    text = str(relative or "")
    posix = PurePosixPath(text.replace("\\", "/"))
    windows = PureWindowsPath(text)
    if (
        not text.strip()
        or text.startswith(("/", "\\"))
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or bool(windows.root)
        or ".." in posix.parts
        or ".." in windows.parts
    ):
        return refusal
    if root is None:
        return ""
    base = Path(root)
    try:
        resolved_root = base.resolve(strict=False)
        resolved = (base / text).resolve(strict=False)
    except (OSError, RuntimeError):
        return refusal
    if resolved == resolved_root or resolved_root not in resolved.parents:
        return refusal
    current = base
    for part in posix.parts:
        current = current / part
        if _is_link_or_junction(current):
            return refusal
        if not current.exists():
            break
    try:
        # A hard-linked leaf shares its inode with a file that may live outside the worktree.
        leaf = base / text
        if leaf.is_file() and os.lstat(leaf).st_nlink > 1:
            return refusal
    except OSError:
        return refusal
    return ""


def correction_allowed_writes_amendment(
    raw: Any, *, card: Mapping[str, Any], root: Path | None = None
) -> tuple[list[str], str]:
    """Validate ``manager_correction.allowed_writes_amendment``: ``(paths, "")`` or ``([], reason)``.

    Absent means ``([], "")`` and the correction behaves exactly as before.
    Each entry is one exact repo-relative file path (a patch touches files, so
    a glob could never be shown touched) contained in ``root`` (see
    ``amended_path_outside_workspace``) and obeys the allowed_writes path
    rules reject_review's amendment enforces (reserved roots, the card's
    forbidden list, task_create's path checks).
    """
    if raw is None:
        return [], ""
    if (
        not isinstance(raw, (list, tuple))
        or not raw
        or any(not isinstance(value, str) or not value.strip() for value in raw)
    ):
        return [], "allowed_writes_amendment_invalid"
    paths = sorted({value.strip() for value in raw})
    for path in paths:
        outside = amended_path_outside_workspace(path, root)
        if outside:
            return [], outside
        if any(char in path for char in _GLOB_CHARS) or "\\" in path:
            return [], f"allowed_writes_amendment_not_exact_path:{path}"[:200]
    from . import core

    refusal = core._validate_allowed_writes_amendment_paths(dict(card), paths)
    if refusal is not None:
        cause = str(refusal.get("stderr") or "allowed_writes_amendment_refused")
        return [], f"allowed_writes_amendment_refused:{cause}"[:200]
    return paths, ""


class CorrectionOriginals(dict):
    """Pre-correction bytes per corrected path, plus every path seeding created.

    ``created`` lists the files and directories ``widen_workspace_for_correction``
    made in the accept worktree, in creation order; ``remove_seeded`` deletes
    them newest first. It is truthy while it holds either, so a cleanup guarded
    by ``if originals:`` still runs when seeding happened but no corrected
    bytes were saved yet.
    """

    def __init__(self) -> None:
        super().__init__()
        self.created: list[Path] = []

    def __bool__(self) -> bool:
        return bool(self.created) or len(self) > 0

    def is_seeded(self, path: Path) -> bool:
        """True when ``path`` is a file or directory seeding created."""
        return Path(path) in self.created

    def remove_seeded(self, root: Path) -> list[str]:
        """Remove every seeded path, newest first; return those that could not be.

        A seeded file that is gone afterwards also loses its saved bytes, so a
        later restore cannot write it back into the worktree.
        """
        base = Path(root)
        seeded_keys = [key for key in self if (base / key) in self.created]
        failed = remove_created_paths(self.created)
        for key in seeded_keys:
            if not (base / key).exists():
                self.pop(key, None)
        return failed


def remove_created_paths(created: list[Path]) -> list[str]:
    """Delete ``created`` newest first (files unlinked, directories rmdir'd).

    The list is emptied as it goes, so a second cleanup is a no-op. A path that
    cannot be removed (a directory something else wrote into) is named.
    """
    failed: list[str] = []
    while created:
        path = created.pop()
        try:
            if path.is_dir() and not _is_link_or_junction(path):
                path.rmdir()
            else:
                path.unlink(missing_ok=True)
        except OSError:
            failed.append(str(path))
    return failed


def widen_workspace_for_correction(
    workspace: Any,
    record: Mapping[str, Any],
    repo: Path,
    *,
    seeded: list[Path] | None = None,
) -> Any:
    """The accept workspace with the amended, non-candidate paths in scope.

    A path outside the sealed candidate was patched against its CANONICAL
    bytes, so its promotion parent baseline is the canonical file as it stands
    now, and a sparse worktree that never materialized it is seeded with those
    same bytes before the corrected bytes are written. Returns ``workspace``
    unchanged when the record names no such path.

    Every path is first checked for containment in both the workspace and the
    canonical repo: an absolute, drive/UNC-prefixed or ``..`` path, one that
    resolves outside its root, or one under a symlink/junction raises
    ``AmendedPathOutsideWorkspace`` (``amended_path_outside_workspace:<path>``)
    before any directory or file is created.

    The corrected bytes were derived from the canonical bytes whose sha256 the
    record names (``CANONICAL_PREIMAGE_KEY``). Every path is re-read and
    compared with that digest BEFORE anything is seeded or widened: a canonical
    file that changed in between raises ``AmendedPathCanonicalChanged``
    (``amended_path_canonical_changed:<path>``) instead of overwriting it.

    Each file and directory seeding creates is appended, in creation order, to
    ``seeded`` (e.g. ``CorrectionOriginals.created``) so a non-promoting exit
    removes it; a failure while seeding removes what it created itself.
    """
    extra = [str(path) for path in (record.get("paths_outside_candidate") or ())]
    if not extra:
        return workspace
    for relative in extra:
        for root in (Path(workspace.path), Path(repo)):
            outside = amended_path_outside_workspace(relative, root)
            if outside:
                raise AmendedPathOutsideWorkspace(outside)
    from . import worker_workspace

    recorded = record.get(CANONICAL_PREIMAGE_KEY)
    recorded = recorded if isinstance(recorded, Mapping) else {}
    baseline = dict(getattr(workspace, "parent_baseline", {}) or {})
    seeds: dict[str, bytes] = {}
    for relative in extra:
        expected = str(recorded.get(relative) or "")
        if len(expected) != 64:
            raise AmendedPathCanonicalChanged(
                f"amended_path_canonical_hash_missing:{relative}"[:200]
            )
        canonical = Path(repo) / relative
        try:
            raw = (
                canonical.read_bytes()
                if canonical.is_file() and not canonical.is_symlink()
                else None
            )
        except OSError:
            raw = None
        hashed = worker_workspace._hash_path(canonical) if raw is not None else None
        if (
            raw is None
            or hashlib.sha256(raw).hexdigest() != expected
            or not str(hashed or "").endswith(f":{expected}")
        ):
            raise AmendedPathCanonicalChanged(
                f"amended_path_canonical_changed:{relative}"[:200]
            )
        baseline[relative] = hashed
        seeds[relative] = raw
    created: list[Path] = []
    try:
        for relative, raw in seeds.items():
            target = Path(workspace.path) / relative
            if target.exists():
                continue
            missing: list[Path] = []
            parent = target.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir()
                created.append(directory)
            # Exclusive create: never follows a link planted after the check.
            with target.open("xb") as handle:
                created.append(target)
                handle.write(raw)
    except BaseException:
        remove_created_paths(created)
        raise
    if seeded is not None:
        seeded.extend(created)
    writes = tuple(dict.fromkeys([*getattr(workspace, "allowed_writes", ()), *extra]))
    if dataclasses.is_dataclass(workspace) and not isinstance(workspace, type):
        return dataclasses.replace(workspace, allowed_writes=writes, parent_baseline=baseline)
    widened = copy.copy(workspace)
    widened.allowed_writes = writes
    widened.parent_baseline = baseline
    return widened


def record_accept_amendment(
    repo: Path, *, task_id: str, request_id: str, record: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Record a landed correction's amendment on the card, add-only; ``None`` when none.

    Runs only after the accept landed, so a refused accept amends nothing. A
    failure here never undoes the accept: it is reported by name.
    """
    paths = [str(path) for path in ((record or {}).get("allowed_writes_amendment") or ())]
    if not paths:
        return None
    actor = str((record or {}).get("actor") or "manager")
    try:
        ok, reason, applied = task_store.record_allowed_writes_amendment(
            repo, task_id, paths, actor=actor, request_id=request_id
        )
    except Exception as exc:  # noqa: BLE001 -- the accept already landed
        ok, reason, applied = False, f"amendment_record_failed:{type(exc).__name__}", []
    result: dict[str, Any] = {
        "paths": paths,
        "applied": list(applied),
        "state": reason if ok else "failed",
        "paths_outside_candidate": list(
            (record or {}).get("paths_outside_candidate") or ()
        ),
    }
    if not ok:
        result["error"] = reason
    return result


# ------------------------------------------------ task_show review state


def _review_request_id(card: Mapping[str, Any]) -> str:
    """The request the card's review names (terminal record first), else its launch."""
    review = card.get("terminal_review")
    evidence = review.get("evidence") if isinstance(review, Mapping) else None
    identity = evidence.get("request_identity") if isinstance(evidence, Mapping) else None
    if isinstance(identity, Mapping) and str(identity.get("request_id") or "").strip():
        return str(identity["request_id"]).strip()
    return str(card.get("launch_request_id") or "").strip()


def required_reviewer_lens_states(
    card: Mapping[str, Any], *, manager: Any
) -> list[dict[str, Any]]:
    """Per required lens ``{lens, state, reviewer_request_id, has_verified_report}``.

    The required lenses come from the profile accept_review resolves (server
    derived tier floored by the declared one, signals from the sealed changed
    paths); the reviewer rows from ``reviewer_evidence`` -- the rows
    ``fold_accept_blockers`` reads. ``state`` is ``verified`` (a usable
    review_ready/accepted attempt), ``state_unknown`` (the ledger was
    unreadable), ``running``, ``not_review_ready`` (an attempt exists but is
    not usable) or ``missing``.
    """
    from . import process_launcher_accept_review as accept_review
    from . import quality_evidence

    review = card.get("terminal_review")
    review = review if isinstance(review, Mapping) else {}
    evidence = review.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    changed = [str(value) for value in (evidence.get("changed_paths") or ())]
    requested = accept_review.effective_requested_risk_tier(card, None)
    signals = quality_evidence.derive_risk_signals(dict(card), changed)
    profile = quality_evidence.resolve_risk_profile(requested, signals=signals)
    required = [str(lens) for lens in (profile.get("required_reviewer_lenses") or ())]
    task_id = str(card.get("task_id") or "")
    request_id = _review_request_id(card)
    reviewers: list[dict[str, Any]] = []
    if task_id and request_id and required:
        # ``manager`` may be a zero-argument factory: the ledger is opened only
        # when a review request exists whose lenses need resolving.
        resolved = manager() if callable(manager) and not hasattr(manager, "repo") else manager
        if resolved is not None:
            reviewers = accept_review.reviewer_evidence(resolved, task_id, request_id)
    state_unknown = any(row.get("state_unknown") for row in reviewers)
    states: list[dict[str, Any]] = []
    for lens in required:
        rows = [row for row in reviewers if row.get("lens") == lens]
        usable = next((row for row in rows if row.get("usable")), None)
        running = next((row for row in rows if row.get("running")), None)
        if usable is not None:
            state, row = "verified", usable
        elif state_unknown:
            state, row = "state_unknown", None
        elif running is not None:
            state, row = "running", running
        elif rows:
            state, row = "not_review_ready", rows[0]
        else:
            state, row = "missing", None
        reviewer_request_id = ""
        if row is not None:
            reviewer_request_id = str(
                (row.get("running_request_id") if state == "running" else "")
                or row.get("request_id")
                or ""
            )
        states.append({
            "lens": lens,
            "state": state,
            "reviewer_request_id": reviewer_request_id,
            "has_verified_report": bool(
                usable is not None and isinstance(usable.get("receipt"), Mapping)
            ),
        })
    return states


def review_state_fields(
    card: Mapping[str, Any], *, repo: Any, manager: Any
) -> dict[str, Any]:
    """``reject_count``/``reject_cap``/``required_reviewer_lenses`` for a show reply.

    ``reject_count`` counts exactly the rejections ``reject_depth_refusal``
    counts against ``REJECT_DEPTH_CAP`` for this contract. A derivation that
    cannot run reports ``None`` plus a ``*_unavailable`` cause -- never a
    count or a lens list that merely looks empty.
    """
    from . import launch_replay_guard

    fields: dict[str, Any] = {"reject_cap": launch_replay_guard.REJECT_DEPTH_CAP}
    task_id = str(card.get("task_id") or "")
    try:
        if not repo or not task_id:
            raise LookupError("repository_or_task_unresolved")
        from . import sqlite_readonly

        # The cap's own reader and counting rule, but an unreadable store is
        # reported rather than read as "no rejections" (fail closed).
        conn = sqlite_readonly.connect_readonly(task_store.canonical_db_path(repo))
        try:
            events = launch_replay_guard.reject_event_payloads(conn, task_id)
        finally:
            conn.close()
        counted = launch_replay_guard.counted_candidate_code_rejects(
            events, launch_replay_guard.task_contract_identity(card)
        )
        fields["reject_count"] = len(counted)
    except Exception as exc:  # noqa: BLE001 -- unknown is reported, never zero
        fields["reject_count"] = None
        cause = str(exc) if isinstance(exc, LookupError) else type(exc).__name__
        fields["reject_count_unavailable"] = cause[:80]
    try:
        fields["required_reviewer_lenses"] = required_reviewer_lens_states(
            card, manager=manager
        )
    except Exception as exc:  # noqa: BLE001 -- unknown is reported, never "none required"
        fields["required_reviewer_lenses"] = None
        fields["required_reviewer_lenses_unavailable"] = type(exc).__name__[:80]
    return fields
