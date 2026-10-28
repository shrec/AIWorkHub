"""accept_review commits exactly the promoted paths itself (``accept_commit``).

Every test drives the logic against its own ``git init`` repository under
``tmp_path``; git discovery is fenced at ``tmp_path`` with
``GIT_CEILING_DIRECTORIES`` and global/system configuration is replaced by an
empty home, so no test ever runs git against the real repository.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import accept_commit, core, process_launcher, server, task_store  # noqa: E402
from aiworkhub.sdlc_deploy_proof import scrubbed_git_env  # noqa: E402

TASK_ID = "accept-review-auto-commit"
REQUEST_ID = "request-auto-commit-0001"
CARD = {
    "task_id": TASK_ID,
    "title": "Accept commits the promoted paths",
    "topic": "accept_commit",
    "work_kind": "feature",
    "status": "finished",
    "worker_status": "done",
}
PROMOTED = ["src/feature.py", "tests/test_feature.py"]


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=scrubbed_git_env(),
        check=False,
    )
    assert completed.returncode == 0, (args, completed.stderr)
    return completed.stdout


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _status(repo: Path) -> str:
    return _git(repo, "status", "--porcelain=v1", "--untracked-files=all")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.resolve()))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    for key, value in (
        ("user.name", "AIWorkHub Test"),
        ("user.email", "test@aiworkhub.invalid"),
        ("commit.gpgsign", "false"),
        ("core.autocrlf", "false"),
    ):
        _git(root, "config", key, value)
    _write(root, "README.md", "readme\n")
    _write(root, "notes.txt", "notes\n")
    _write(root, "src/feature.py", "VALUE = 0\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _promote(repo: Path) -> None:
    """What the accept's promotion does: write the candidate's bytes."""

    _write(repo, "src/feature.py", "VALUE = 1\n")
    _write(repo, "tests/test_feature.py", "def test_value():\n    assert True\n")


def _accept(
    repo: Path,
    *,
    receipt: dict[str, Any] | None = None,
    card: dict[str, Any] | None = CARD,
    commit_message: str | None = None,
    auto_commit: bool = True,
) -> dict[str, Any]:
    snapshot = accept_commit.snapshot_before_accept(repo, auto_commit=auto_commit)
    _promote(repo)
    return accept_commit.commit_promoted(
        snapshot,
        receipt if receipt is not None else {"ok": True, "promoted_paths": list(PROMOTED)},
        card,
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        commit_message=commit_message,
    )


def _message(repo: Path) -> list[str]:
    return _git(repo, "log", "-1", "--format=%B").rstrip("\n").split("\n")


def _committed_paths(repo: Path) -> list[str]:
    return sorted(_git(repo, "show", "--name-only", "--format=", "HEAD").split())


def test_the_promoted_paths_are_committed_with_their_trailers(repo):
    _git(repo, "config", "--add", "aiworkhub.commitTrailer", "Co-Authored-By: Owner <owner@aiworkhub.invalid>")
    _git(repo, "config", "--add", "aiworkhub.commitTrailer", "Signed-off-by: Owner <owner@aiworkhub.invalid>")
    before = _head(repo)

    commit = _accept(repo)

    assert commit == {
        "state": "committed",
        "sha": _head(repo),
        "paths": PROMOTED,
        "message_subject": "feat(accept_commit): Accept commits the promoted paths",
    }
    assert _git(repo, "rev-parse", "HEAD~1").strip() == before, "a new commit, never an amend"
    assert _committed_paths(repo) == PROMOTED
    assert _message(repo) == [
        "feat(accept_commit): Accept commits the promoted paths",
        "",
        "Accept commits the promoted paths",
        "",
        f"AIWorkHub-Task: {TASK_ID}",
        f"AIWorkHub-Request: {REQUEST_ID}",
        "Co-Authored-By: Owner <owner@aiworkhub.invalid>",
        "Signed-off-by: Owner <owner@aiworkhub.invalid>",
    ]
    assert _status(repo) == ""


def test_an_unrelated_staged_and_unstaged_change_stays_exactly_as_it_was(repo):
    _write(repo, "staged.txt", "owner work in progress\n")
    _git(repo, "add", "staged.txt")
    _write(repo, "notes.txt", "notes edited but not staged\n")
    staged_before = _git(repo, "diff", "--cached", "--name-status")

    commit = _accept(repo)

    assert commit["state"] == "committed", commit
    assert _committed_paths(repo) == PROMOTED
    assert _git(repo, "diff", "--cached", "--name-status") == staged_before
    assert sorted(_status(repo).splitlines()) == [" M notes.txt", "A  staged.txt"]
    assert (repo / "notes.txt").read_text(encoding="utf-8") == "notes edited but not staged\n"


def test_a_promoted_path_dirty_before_the_accept_skips_the_commit(repo):
    _write(repo, "src/feature.py", "VALUE = 'owner edit'\n")
    before = _head(repo)

    commit = _accept(repo)

    assert commit == {
        "state": "skipped",
        "reason": "promoted_path_dirty_before_accept",
        "paths": PROMOTED,
        "message_subject": "feat(accept_commit): Accept commits the promoted paths",
        "dirty_paths": ["src/feature.py"],
    }
    assert _head(repo) == before
    assert _git(repo, "diff", "--cached", "--name-only") == "", "nothing was staged"
    assert (repo / "src/feature.py").read_text(encoding="utf-8") == "VALUE = 1\n", (
        "the promotion itself still landed"
    )


def test_a_needfix_backed_card_is_a_fix_that_resolves_its_needfix(repo):
    receipt = {
        "ok": True,
        "promoted_paths": list(PROMOTED),
        "needfix_closure": {"state": "closed", "needfix_id": "NF-2026-01234"},
    }

    commit = _accept(repo, receipt=receipt)

    assert commit["state"] == "committed", commit
    assert commit["message_subject"] == "fix(accept_commit): Accept commits the promoted paths"
    trailers = _message(repo)[-3:]
    assert trailers == [
        "Resolves: NF-2026-01234",
        f"AIWorkHub-Task: {TASK_ID}",
        f"AIWorkHub-Request: {REQUEST_ID}",
    ]


def test_a_long_title_is_clipped_on_a_word_boundary_and_kept_whole_in_the_body(repo):
    title = " ".join(["promoted"] * 20)
    commit = _accept(repo, card={**CARD, "title": title, "work_kind": "bugfix"})

    subject = commit["message_subject"]
    assert commit["state"] == "committed", commit
    assert subject.startswith("fix(accept_commit): promoted")
    assert len(subject) <= 100 and not subject.endswith(" ")
    assert f"fix(accept_commit): {title}".startswith(subject + " ")
    assert _message(repo)[2] == title


def test_a_manager_message_is_used_and_still_carries_the_trailers(repo):
    commit = _accept(repo, commit_message="chore: hand-written subject\n\nbody line")

    assert commit["message_subject"] == "chore: hand-written subject"
    assert _message(repo) == [
        "chore: hand-written subject",
        "",
        "body line",
        "",
        f"AIWorkHub-Task: {TASK_ID}",
        f"AIWorkHub-Request: {REQUEST_ID}",
    ]


def test_a_detached_head_skips_and_writes_nothing(repo):
    _git(repo, "checkout", "-q", "--detach")
    before = _head(repo)

    commit = _accept(repo)

    assert commit["state"] == "skipped"
    assert commit["reason"] == "detached_head"
    assert _head(repo) == before
    assert sorted(_status(repo).splitlines()) == [" M src/feature.py", "?? tests/test_feature.py"]


def test_a_failing_pre_commit_hook_is_recorded_and_the_promotion_stays(repo):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "#!/bin/sh\n"
        "i=0\n"
        "while [ $i -lt 100 ]; do\n"
        "  echo 'noise noise noise noise noise noise noise noise noise noise' >&2\n"
        "  i=$((i+1))\n"
        "done\n"
        "echo 'hook refused this commit' >&2\n"
        "exit 1\n"
    )
    # Created executable in one step: chmod is not available in every sandbox.
    fd = os.open(hook, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
    with os.fdopen(fd, "wb") as handle:
        handle.write(script.encode("utf-8"))
    before = _head(repo)

    commit = _accept(repo)

    assert commit["state"] == "failed", commit
    assert commit["reason"] == "git_commit_failed"
    # The hook's own stderr text is not asserted: in some sandboxes the hook
    # shell's stderr never reaches git's captured stderr (see the unit test below).
    assert commit["stderr_tail"]
    assert len(commit["stderr_tail"]) <= accept_commit.STDERR_TAIL_CHARS
    assert "sha" not in commit
    assert _head(repo) == before
    assert (repo / "src/feature.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def _recorded_tail(stdout: bytes, stderr: bytes) -> str:
    completed = subprocess.CompletedProcess(args=["git"], returncode=1, stdout=stdout, stderr=stderr)
    failure = accept_commit._failure_tail(completed)
    return accept_commit._decided(accept_commit.STATE_FAILED, "git_commit_failed", stderr=failure)["stderr_tail"]


def test_the_failure_tail_is_bounded_and_falls_back_to_stdout_then_exit_code():
    tail = _recorded_tail(b"", b"x" * 6000 + b"MARKER")
    assert len(tail) <= accept_commit.STDERR_TAIL_CHARS
    assert tail.endswith("MARKER")

    assert _recorded_tail(b"out-tail", b"") == "out-tail"
    assert _recorded_tail(b"", b"") == "git exited 1"


def test_each_skip_has_its_own_stable_reason_and_runs_no_git_write(repo, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    nested = repo / "nested"
    nested.mkdir()
    before = _head(repo)

    reasons = {
        "not_a_git_work_tree": _accept(plain),
        "repo_root_not_git_toplevel": _accept(nested),
        "no_promoted_paths": _accept(repo, receipt={"ok": True, "promoted_paths": []}),
        "auto_commit_disabled": _accept(repo, auto_commit=False),
    }

    for reason, commit in reasons.items():
        assert commit["state"] == "skipped", (reason, commit)
        assert commit["reason"] == reason
        assert "sha" not in commit
    assert not (plain / ".git").exists()
    assert _head(repo) == before
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_an_unsafe_promoted_path_is_never_handed_to_git(repo):
    before = _head(repo)

    commit = _accept(repo, receipt={"ok": True, "promoted_paths": ["../outside.py"]})

    assert (commit["state"], commit["reason"]) == ("skipped", "promoted_path_invalid")
    assert _head(repo) == before


def test_the_module_never_pushes_amends_skips_hooks_or_uses_a_shell():
    tree = ast.parse(Path(accept_commit.__file__).read_text(encoding="utf-8"))
    literals = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not literals & {"push", "--amend", "--no-verify", "-n", "--force"}
    keywords = {
        keyword.arg for node in ast.walk(tree) if isinstance(node, ast.Call)
        for keyword in node.keywords
    }
    assert "shell" not in keywords


class _Promoting:
    """``default_manager()`` whose accept promotes PROMOTED into ``repo``."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.process_dir = repo.parent / "processes"

    def accept_review(self, request_id: str, task_id: str, **_kwargs: Any) -> dict[str, Any]:
        _promote(self.repo)
        return {
            "ok": True,
            "request_id": request_id,
            "task_id": task_id,
            "promoted_paths": list(PROMOTED),
        }


@pytest.fixture
def through_server(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(process_launcher, "default_manager", lambda: _Promoting(repo))
    monkeypatch.setattr(core, "repo_root", lambda: repo)
    monkeypatch.setattr(task_store, "get_task", lambda _root, task_id: dict(CARD))
    return repo


def test_the_accept_tool_reports_the_commit_in_its_receipt(through_server):
    receipt = server.aiworkhub_agent_accept_review(request_id=REQUEST_ID, task_id=TASK_ID)

    assert receipt["ok"] is True
    assert receipt["promoted_paths"] == PROMOTED
    assert receipt["commit"]["state"] == "committed", receipt["commit"]
    assert receipt["commit"]["sha"] == _head(through_server)
    assert _committed_paths(through_server) == PROMOTED


def test_a_skipped_commit_never_flips_the_accept(through_server):
    _write(through_server, "src/feature.py", "VALUE = 'owner edit'\n")

    receipt = server.aiworkhub_agent_accept_review(request_id=REQUEST_ID, task_id=TASK_ID)

    assert receipt["ok"] is True
    assert receipt["commit"]["reason"] == "promoted_path_dirty_before_accept"


def test_auto_commit_false_leaves_the_tree_for_the_manager(through_server):
    before = _head(through_server)

    receipt = server.aiworkhub_agent_accept_review(
        request_id=REQUEST_ID, task_id=TASK_ID, auto_commit=False
    )

    assert receipt["ok"] is True
    assert (receipt["commit"]["state"], receipt["commit"]["reason"]) == (
        "skipped",
        "auto_commit_disabled",
    )
    assert _head(through_server) == before
