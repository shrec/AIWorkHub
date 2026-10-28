"""Commit exactly the paths an accepted review promoted.

``aiworkhub_agent_accept_review`` promotes a card's files into the canonical
tree; this module then commits those files -- and only those -- so the manager
never spends a turn composing ``git add``/``git commit`` by hand. Two calls,
both made under the server's lifecycle lock:

``snapshot_before_accept``  before ``accept_review`` runs: is the repo root a
                            git work tree on a branch, and which paths are
                            already dirty (``git status --porcelain=v1 -z
                            --untracked-files=all``)?
``commit_promoted``         after a successful accept: commit the receipt's
                            ``promoted_paths`` with ``git add -A --`` and
                            ``git commit --only -m <message> --``, or say why not.

The reply is the receipt's ``commit`` object: ``state`` (committed | skipped |
failed), ``paths``, ``message_subject``, plus ``sha`` for a commit and a stable
``reason`` otherwise. A promoted path that was dirty before the accept skips
the commit (``promoted_path_dirty_before_accept``, named in ``dirty_paths``) so
owner work in progress is never swept into a worker commit; every other path's
staged or unstaged state is left exactly as it was. A git failure is recorded
with a bounded ``stderr_tail`` and never undoes the promotion or the accept.

git always runs as an argv list without a shell, with the repository
redirection environment scrubbed, hooks enabled, and never with ``--no-verify``,
``--amend`` or ``push``. Reads carry a timeout; ``add`` and ``commit`` do not,
because they run the owner's filters and hooks and killing one would orphan
its children.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .sdlc_deploy_proof import scrubbed_git_env

STATE_COMMITTED = "committed"
STATE_SKIPPED = "skipped"
STATE_FAILED = "failed"

SKIP_AUTO_COMMIT_DISABLED = "auto_commit_disabled"
SKIP_GIT_UNAVAILABLE = "git_unavailable"
SKIP_NOT_A_GIT_WORK_TREE = "not_a_git_work_tree"
SKIP_REPO_ROOT_NOT_TOPLEVEL = "repo_root_not_git_toplevel"
SKIP_DETACHED_HEAD = "detached_head"
SKIP_NO_PROMOTED_PATHS = "no_promoted_paths"
SKIP_PROMOTED_PATH_INVALID = "promoted_path_invalid"
SKIP_PROMOTED_PATHS_TOO_LONG = "promoted_paths_exceed_argv_bound"
SKIP_COMMIT_MESSAGE_INVALID = "commit_message_invalid"
SKIP_DIRTY_BEFORE_ACCEPT = "promoted_path_dirty_before_accept"
SKIP_PROMOTED_PATHS_UNCHANGED = "promoted_paths_unchanged"

FAIL_GIT_INVOCATION = "git_invocation_failed"
FAIL_HEAD_CHECK = "git_head_check_failed"
FAIL_STATUS = "git_status_failed"
FAIL_TRAILER_CONFIG = "commit_trailer_config_failed"
FAIL_ADD = "git_add_failed"
FAIL_COMMIT = "git_commit_failed"
FAIL_INTERNAL = "auto_commit_internal_error"

STDERR_TAIL_CHARS = 2000
SUBJECT_MAX_CHARS = 100
COMMIT_MESSAGE_MAX_CHARS = 16384
# Windows caps a whole command line at 32767 characters; the paths share it
# with the message.
ARGV_PATH_CHARS_MAX = 12000
TRAILER_CONFIG_KEY = "aiworkhub.commitTrailer"
TRAILER_VALUES_MAX = 64
TRAILER_VALUE_MAX_CHARS = 2000
GIT_READ_TIMEOUT_SECONDS = 120

_NEEDFIX_ID = re.compile(r"NF-\d{4}-\d{5}")
_NEEDFIX_TASK_ID = re.compile(r"needfix-(NF-\d{4}-\d{5})(?:-|$)")


@dataclass(frozen=True)
class PreAcceptSnapshot:
    """What was true of the repo root before the accept promoted anything.

    ``outcome`` is set when the commit is already decided -- disabled, not a
    work tree, detached HEAD, or a git failure -- and ``commit_promoted``
    returns it without running git again.
    """

    root: Path
    dirty: frozenset[str] = frozenset()
    outcome: Mapping[str, Any] | None = None


def snapshot_before_accept(repo_root: str | Path, *, auto_commit: bool) -> PreAcceptSnapshot:
    """Probe the work tree and record its dirty paths; read-only."""

    root = Path(repo_root)
    if not auto_commit:
        return PreAcceptSnapshot(root, outcome=_decided(STATE_SKIPPED, SKIP_AUTO_COMMIT_DISABLED))
    try:
        refusal = _work_tree_refusal(root)
        if refusal is not None:
            return PreAcceptSnapshot(root, outcome=refusal)
        dirty = _dirty_paths(root)
    except _GitStepFailed as failed:
        return PreAcceptSnapshot(root, outcome=failed.outcome)
    except FileNotFoundError:
        return PreAcceptSnapshot(root, outcome=_decided(STATE_SKIPPED, SKIP_GIT_UNAVAILABLE))
    except (OSError, subprocess.SubprocessError) as exc:
        return PreAcceptSnapshot(
            root, outcome=_decided(STATE_FAILED, FAIL_GIT_INVOCATION, stderr=str(exc))
        )
    return PreAcceptSnapshot(root, dirty=dirty)


def commit_promoted(
    snapshot: PreAcceptSnapshot,
    receipt: Mapping[str, Any],
    card: Mapping[str, Any] | None,
    *,
    task_id: str,
    request_id: str,
    commit_message: str | None = None,
) -> dict[str, Any]:
    """Commit the receipt's promoted paths; never raises, never undoes the accept."""

    paths = _promoted_paths(receipt.get("promoted_paths"))
    listed = paths if paths is not None else []
    needfix_id = _needfix_id(receipt, task_id)
    subject = ""
    try:
        if snapshot.outcome is not None:
            return _reply(snapshot.outcome, listed, subject)
        if paths is None:
            return _reply(_decided(STATE_SKIPPED, SKIP_PROMOTED_PATH_INVALID), listed, subject)
        if not paths:
            return _reply(_decided(STATE_SKIPPED, SKIP_NO_PROMOTED_PATHS), listed, subject)
        if sum(len(path) + 1 for path in paths) > ARGV_PATH_CHARS_MAX:
            return _reply(_decided(STATE_SKIPPED, SKIP_PROMOTED_PATHS_TOO_LONG), listed, subject)
        head = _message_head(commit_message, card, task_id=task_id, needfix_id=needfix_id)
        if head is None:
            return _reply(_decided(STATE_SKIPPED, SKIP_COMMIT_MESSAGE_INVALID), listed, subject)
        subject = head.split("\n", 1)[0]
        dirty_before = sorted(set(paths) & snapshot.dirty)
        if dirty_before:
            outcome = _decided(STATE_SKIPPED, SKIP_DIRTY_BEFORE_ACCEPT)
            return {**_reply(outcome, listed, subject), "dirty_paths": dirty_before}
        root = snapshot.root
        if not set(paths) & _dirty_paths(root):
            return _reply(_decided(STATE_SKIPPED, SKIP_PROMOTED_PATHS_UNCHANGED), listed, subject)
        trailers = _trailers(root, head, task_id=task_id, request_id=request_id, needfix_id=needfix_id)
        message = head + "\n\n" + "\n".join(trailers) + "\n"
        _run_step(root, FAIL_ADD, "--literal-pathspecs", "add", "-A", "--", *paths, timeout=None)
        _run_step(
            root, FAIL_COMMIT,
            "--literal-pathspecs", "commit", "--only", "-m", message, "--", *paths,
            timeout=None,
        )
        sha = _git(root, "rev-parse", "--verify", "HEAD")
        committed: dict[str, Any] = {"state": STATE_COMMITTED}
        if sha.returncode == 0:
            committed["sha"] = sha.stdout.decode("ascii", "replace").strip()
        return _reply(committed, listed, subject)
    except _GitStepFailed as failed:
        return _reply(failed.outcome, listed, subject)
    except FileNotFoundError:
        return _reply(_decided(STATE_SKIPPED, SKIP_GIT_UNAVAILABLE), listed, subject)
    except (OSError, subprocess.SubprocessError) as exc:
        return _reply(_decided(STATE_FAILED, FAIL_GIT_INVOCATION, stderr=str(exc)), listed, subject)
    except Exception as exc:  # the accept already landed; report, never raise
        return _reply(
            _decided(STATE_FAILED, FAIL_INTERNAL, stderr=f"{type(exc).__name__}: {exc}"),
            listed,
            subject,
        )


class _GitStepFailed(Exception):
    def __init__(self, outcome: Mapping[str, Any]) -> None:
        super().__init__(outcome.get("reason"))
        self.outcome = outcome


def _decided(state: str, reason: str, *, stderr: str | None = None) -> dict[str, Any]:
    outcome: dict[str, Any] = {"state": state, "reason": reason}
    if stderr is not None:
        outcome["stderr_tail"] = stderr[-STDERR_TAIL_CHARS:]
    return outcome


def _reply(outcome: Mapping[str, Any], paths: Sequence[str], subject: str) -> dict[str, Any]:
    reply: dict[str, Any] = {"state": outcome["state"]}
    for key in ("sha", "reason", "stderr_tail"):
        if key in outcome:
            reply[key] = outcome[key]
    reply["paths"] = list(paths)
    reply["message_subject"] = subject
    return reply


def _git(
    root: Path, *args: str, timeout: float | None = GIT_READ_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[bytes]:
    env = scrubbed_git_env()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", "-C", str(root), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=env,
        timeout=timeout,
        check=False,
    )


def _failure_tail(completed: subprocess.CompletedProcess[bytes]) -> str:
    text = completed.stderr.decode("utf-8", "replace").strip()
    if not text:
        text = completed.stdout.decode("utf-8", "replace").strip()
    return text or f"git exited {completed.returncode}"


def _run_step(
    root: Path, reason: str, *args: str, timeout: float | None = GIT_READ_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[bytes]:
    completed = _git(root, *args, timeout=timeout)
    if completed.returncode != 0:
        raise _GitStepFailed(_decided(STATE_FAILED, reason, stderr=_failure_tail(completed)))
    return completed


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def _work_tree_refusal(root: Path) -> dict[str, Any] | None:
    toplevel = _git(root, "rev-parse", "--show-toplevel")
    if toplevel.returncode != 0:
        return _decided(STATE_SKIPPED, SKIP_NOT_A_GIT_WORK_TREE)
    # Never let discovery walk up into an enclosing repository.
    if not _same_path(toplevel.stdout.decode("utf-8", "surrogateescape").strip(), root):
        return _decided(STATE_SKIPPED, SKIP_REPO_ROOT_NOT_TOPLEVEL)
    head = _git(root, "symbolic-ref", "-q", "HEAD")
    if head.returncode == 1:
        return _decided(STATE_SKIPPED, SKIP_DETACHED_HEAD)
    if head.returncode != 0:
        return _decided(STATE_FAILED, FAIL_HEAD_CHECK, stderr=_failure_tail(head))
    return None


def _dirty_paths(root: Path) -> frozenset[str]:
    status = _run_step(root, FAIL_STATUS, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    fields = status.stdout.split(b"\0")
    dirty: set[str] = set()
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        dirty.add(entry[3:].decode("utf-8", "surrogateescape"))
        # A rename or copy is followed by its origin path as its own field.
        if b"R" in entry[:2] or b"C" in entry[:2]:
            if index < len(fields):
                dirty.add(fields[index].decode("utf-8", "surrogateescape"))
            index += 1
    return frozenset(dirty)


def _promoted_paths(raw: Any) -> list[str] | None:
    """Repo-relative ``/`` paths in receipt order, or None when any is unsafe."""

    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        return None
    paths: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item or "\0" in item:
            return None
        path = item.replace("\\", "/")
        parts = path.split("/")
        if (
            PurePosixPath(path).is_absolute()
            or PureWindowsPath(item).drive
            or any(part in ("", ".", "..") for part in parts)
        ):
            return None
        if path not in paths:
            paths.append(path)
    return paths


def _needfix_id(receipt: Mapping[str, Any], task_id: str) -> str | None:
    closure = receipt.get("needfix_closure")
    if isinstance(closure, Mapping):
        linked = closure.get("needfix_id")
        if isinstance(linked, str) and _NEEDFIX_ID.fullmatch(linked):
            return linked
    match = _NEEDFIX_TASK_ID.match(task_id or "")
    return match.group(1) if match else None


def _one_line(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _clip_subject(text: str, *, keep: int) -> str:
    """Clip to ``SUBJECT_MAX_CHARS`` on a word boundary past the first ``keep`` chars."""

    if len(text) <= SUBJECT_MAX_CHARS:
        return text
    cut = text.rfind(" ", keep + 1, SUBJECT_MAX_CHARS + 1)
    return text[:cut].rstrip() if cut > keep else text[:SUBJECT_MAX_CHARS]


def _message_head(
    commit_message: str | None,
    card: Mapping[str, Any] | None,
    *,
    task_id: str,
    needfix_id: str | None,
) -> str | None:
    """Subject and body before the trailers; None for an unusable manager message."""

    if commit_message is not None:
        if (
            not isinstance(commit_message, str)
            or not commit_message.strip()
            or len(commit_message) > COMMIT_MESSAGE_MAX_CHARS
            or "\0" in commit_message
        ):
            return None
        return commit_message.strip()
    card = card if isinstance(card, Mapping) else {}
    title = _one_line(card.get("title")) or task_id
    topic = _one_line(card.get("topic"))
    kind = "fix" if needfix_id or card.get("work_kind") == "bugfix" else "feat"
    prefix = f"{kind}({topic}): " if topic else f"{kind}: "
    subject = _clip_subject(prefix + title, keep=len(prefix))
    return f"{subject}\n\n{title}"


def _trailers(
    root: Path, head: str, *, task_id: str, request_id: str, needfix_id: str | None
) -> list[str]:
    configured = _git(root, "config", "-z", "--get-all", TRAILER_CONFIG_KEY)
    if configured.returncode not in (0, 1):  # 1: the key is not set
        raise _GitStepFailed(
            _decided(STATE_FAILED, FAIL_TRAILER_CONFIG, stderr=_failure_tail(configured))
        )
    values: list[str] = []
    if configured.returncode == 0:
        values = [value.decode("utf-8", "replace") for value in configured.stdout.split(b"\0") if value]
    if len(values) > TRAILER_VALUES_MAX or any(len(v) > TRAILER_VALUE_MAX_CHARS for v in values):
        raise _GitStepFailed(
            _decided(
                STATE_FAILED,
                FAIL_TRAILER_CONFIG,
                stderr=f"{TRAILER_CONFIG_KEY} exceeds {TRAILER_VALUES_MAX} values "
                f"of {TRAILER_VALUE_MAX_CHARS} characters",
            )
        )
    trailers = [f"Resolves: {needfix_id}"] if needfix_id else []
    trailers += [f"AIWorkHub-Task: {task_id}", f"AIWorkHub-Request: {request_id}", *values]
    # A manager message that already carries a trailer line keeps one copy.
    present = set(head.splitlines())
    return [line for line in trailers if line not in present]
