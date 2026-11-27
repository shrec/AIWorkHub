"""Regression tests for fix batch 04 (NF-2026-00328, NF-2026-00644)."""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aiworkhub import process_launcher, quality_review
from aiworkhub.worker_workspace import WorkerWorkspace


def _owner_class() -> type:
    for value in vars(process_launcher).values():
        if (
            inspect.isclass(value)
            and value.__module__ == process_launcher.__name__
            and "_retained_review_workspace_integrity" in vars(value)
        ):
            return value
    raise AssertionError("no owner of _retained_review_workspace_integrity")


# --- NF-2026-00328 ---------------------------------------------------------


def test_changed_symbols_distinguish_added_modified_and_untouched(tmp_path: Path) -> None:
    base = tmp_path / "base"
    cand = tmp_path / "cand"
    base.mkdir()
    cand.mkdir()
    (base / "mod.py").write_text(
        "def keep():\n    return 1\n\n\ndef edit():\n    return 1\n", encoding="utf-8"
    )
    (cand / "mod.py").write_text(
        "def keep():\n    return 1\n\n\ndef edit():\n    return 2\n\n\n"
        "def orphan():\n    return 3\n",
        encoding="utf-8",
    )
    (cand / "new.py").write_text("def fresh():\n    pass\n", encoding="utf-8")

    rows = process_launcher._candidate_changed_symbols(
        cand, ["mod.py", "new.py"], base_root=base
    )
    by_name = {row["symbol"]: row["change"] for row in rows}
    assert by_name == {
        "edit": quality_review.CHANGE_MODIFIED,
        "orphan": quality_review.CHANGE_ADDED,
        "fresh": quality_review.CHANGE_ADDED,
    }


def test_changed_symbols_without_base_stay_conservative(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("def a():\n    pass\n", encoding="utf-8")
    rows = process_launcher._candidate_changed_symbols(tmp_path, ["mod.py"])
    assert rows == [
        {"symbol": "a", "file": "mod.py", "change": quality_review.CHANGE_MODIFIED}
    ]


# --- NF-2026-00644 ---------------------------------------------------------


def _workspace_payload(repo: Path, root: Path, request_id: str) -> dict[str, Any]:
    path = root / request_id / "worktree"
    home = root / request_id / "home"
    path.mkdir(parents=True)
    home.mkdir(parents=True)
    return {
        "request_id": request_id,
        "repo": str(repo),
        "path": str(path),
        "home": str(home),
        "allowed_writes": ["src/x.py"],
        "parent_baseline": {},
    }


def _card(payload: dict[str, Any], request_id: str, substatus: str) -> dict[str, Any]:
    return {
        "terminal_review": {
            "substatus": substatus,
            "evidence": {
                "request_identity": {"request_id": request_id},
                "workspace": payload,
                "changed_paths": ["src/x.py"],
            },
        }
    }


def test_writable_finalize_failed_without_hashes_is_preserved(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    request_id = "req-nf00644"
    payload = _workspace_payload(repo, tmp_path / "ws", request_id)
    meta_ws = WorkerWorkspace.from_metadata(payload)
    fake_self = SimpleNamespace(repo=repo.resolve())
    method = _owner_class()._retained_review_workspace_integrity

    ok, reason = method(fake_self, _card(payload, request_id, "finalize_failed"),
                        request_id, meta_ws)
    assert (ok, reason) == (True, "review_workspace_finalize_failed_unhashed")

    # Any other writable substatus without hashes stays fail-closed.
    ok, reason = method(fake_self, _card(payload, request_id, "review_ready"),
                        request_id, meta_ws)
    assert (ok, reason) == (False, "review_workspace_hashes_missing")


def test_non_writable_finalize_failed_without_hashes_still_quarantines(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    request_id = "req-nf00644-ro"
    payload = _workspace_payload(repo, tmp_path / "ws", request_id)
    payload["allowed_writes"] = []
    meta_ws = WorkerWorkspace.from_metadata(payload)
    fake_self = SimpleNamespace(repo=repo.resolve())
    method = _owner_class()._retained_review_workspace_integrity

    ok, reason = method(fake_self, _card(payload, request_id, "finalize_failed"),
                        request_id, meta_ws)
    assert (ok, reason) == (False, "review_workspace_hashes_missing")


# --- NF-2026-00328 rework --------------------------------------------------


def test_later_redefinition_is_reported_modified(tmp_path: Path) -> None:
    base = tmp_path / "base"
    cand = tmp_path / "cand"
    base.mkdir()
    cand.mkdir()
    (base / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (cand / "mod.py").write_text(
        "def f():\n    return 1\n\n\ndef f():\n    return 2\n", encoding="utf-8"
    )
    rows = process_launcher._candidate_changed_symbols(cand, ["mod.py"], base_root=base)
    assert rows == [
        {"symbol": "f", "file": "mod.py", "change": quality_review.CHANGE_MODIFIED}
    ]


def test_methods_are_keyed_by_qualified_name(tmp_path: Path) -> None:
    base = tmp_path / "base"
    cand = tmp_path / "cand"
    base.mkdir()
    cand.mkdir()
    (base / "mod.py").write_text(
        "def run():\n    return 1\n\n\nclass A:\n    def run(self):\n        return 1\n",
        encoding="utf-8",
    )
    (cand / "mod.py").write_text(
        "def run():\n    return 1\n\n\nclass A:\n    def run(self):\n        return 2\n",
        encoding="utf-8",
    )
    rows = process_launcher._candidate_changed_symbols(cand, ["mod.py"], base_root=base)
    assert {row["symbol"] for row in rows} == {"A", "A.run"}


def test_import_only_change_yields_module_symbol(tmp_path: Path) -> None:
    base = tmp_path / "base"
    cand = tmp_path / "cand"
    base.mkdir()
    cand.mkdir()
    (base / "mod.py").write_text("import os\n\n\ndef a():\n    pass\n", encoding="utf-8")
    (cand / "mod.py").write_text("import sys\n\n\ndef a():\n    pass\n", encoding="utf-8")
    rows = process_launcher._candidate_changed_symbols(cand, ["mod.py"], base_root=base)
    assert rows == [
        {"symbol": "<module>", "file": "mod.py", "change": quality_review.CHANGE_MODIFIED}
    ]


def test_traversing_relative_path_never_reads_outside_base(tmp_path: Path) -> None:
    base = tmp_path / "root" / "base"
    cand = tmp_path / "root" / "cand"
    base.mkdir(parents=True)
    cand.mkdir(parents=True)
    # ``../x.py`` resolves to the same file from both roots; accepting it would
    # make the symbol look untouched.  Rejected, it keeps the no-base label.
    (tmp_path / "root" / "x.py").write_text("def leak():\n    pass\n", encoding="utf-8")
    rows = process_launcher._candidate_changed_symbols(cand, ["../x.py"], base_root=base)
    assert rows == [
        {"symbol": "leak", "file": "../x.py", "change": quality_review.CHANGE_MODIFIED}
    ]


def test_reachability_inputs_wire_base_root_and_drop_self_base(
    tmp_path: Path, monkeypatch: Any
) -> None:
    base = tmp_path / "base"
    cand = tmp_path / "cand"
    base.mkdir()
    cand.mkdir()
    captured: list[dict[str, Any]] = []

    def _capture(workspace_root: Path, changed_py: Any, **kwargs: Any) -> list[Any]:
        captured.append(kwargs)
        return []

    monkeypatch.setattr(process_launcher, "_candidate_changed_symbols", _capture)
    method = process_launcher.ProcessManager._candidate_reachability_inputs
    fake_self = SimpleNamespace()

    assert method(fake_self, SimpleNamespace(path=cand, repo=base, base_oid=None), ["m.py"]) is None
    assert captured[-1]["base_root"] == base
    assert captured[-1]["base_reader"] is None

    # Self-base (same directory spelled differently) must fall back to None.
    assert method(
        fake_self, SimpleNamespace(path=cand, repo=cand / "sub" / "..", base_oid=None), ["m.py"]
    ) is None
    assert captured[-1]["base_root"] is None

    # A recorded pinned baseline commit is read from the authority repo only.
    method(fake_self, SimpleNamespace(path=cand, repo=base, base_oid="a" * 40), ["m.py"])
    assert captured[-1]["base_reader"] is not None
    # Self-base: never read the pin through the candidate's own .git.
    method(fake_self, SimpleNamespace(path=cand, repo=cand, base_oid="a" * 40), ["m.py"])
    assert captured[-1]["base_reader"] is None


def test_git_base_reader_reads_pinned_blob(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    git += ["-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]
    subprocess.run([*git, "init", "-q"], check=True)
    (repo / "m.py").write_bytes(b"def f():\n    return 1\n")
    subprocess.run([*git, "add", "m.py"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "base"], check=True)
    oid = subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "m.py").write_bytes(b"def f():\n    return 2\n")
    read = process_launcher._git_base_reader(repo, oid)
    assert read("m.py") == "def f():\n    return 1\n"
    assert read("absent.py") is None
    with pytest.raises(ValueError):
        read("../m.py")
    with pytest.raises(OSError):
        process_launcher._git_base_reader(repo, "f" * 40)("m.py")
