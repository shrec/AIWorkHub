from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher, required_output_glob, worker_workspace  # noqa: E402

_VALIDATORS = {
    "worker_workspace": worker_workspace.validate_required_outputs,
    "process_launcher": process_launcher._fallback_validate_required_outputs,
}


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"file:{path.stat().st_mode & 0o777:o}:{digest}"


class SplitGlobUnchangedTest(unittest.TestCase):
    def test_any_changed_match_allows_every_unchanged_sibling(self) -> None:
        self.assertEqual(
            required_output_glob.split_glob_unchanged(["a", "b"], ["c"]),
            ([], ["a", "b"]),
        )

    def test_no_changed_match_fails_every_unchanged_match(self) -> None:
        self.assertEqual(
            required_output_glob.split_glob_unchanged(["a", "b"], []),
            (["a", "b"], []),
        )


class RequiredOutputGlobAnyMatchNF01834Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.worktree = root / "worktree"
        self.home = root / "home"
        for directory in (self.repo, self.worktree, self.home):
            directory.mkdir()

    def write(self, relative: str, payload: bytes) -> Path:
        path = self.worktree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def workspace(
        self, allowed_writes: tuple[str, ...], baseline: dict[str, str]
    ) -> worker_workspace.WorkerWorkspace:
        return worker_workspace.WorkerWorkspace(
            request_id="nf01834",
            repo=self.repo,
            path=self.worktree,
            home=self.home,
            allowed_writes=allowed_writes,
            parent_baseline=dict(baseline),
            workspace_baseline=dict(baseline),
        )

    def three_files(self, changed: tuple[str, ...]) -> dict[str, str]:
        baseline: dict[str, str] = {}
        for name in ("a", "b", "c"):
            relative = f"out/{name}.txt"
            path = self.write(relative, f"{name} baseline\n".encode())
            baseline[relative] = _file_hash(path)
            if relative in changed:
                path.write_bytes(f"{name} edited\n".encode())
        return baseline

    def test_one_of_three_changed_satisfies_glob_through_both_validators(self) -> None:
        baseline = self.three_files(("out/b.txt",))
        for label, validate in _VALIDATORS.items():
            with self.subTest(validator=label):
                records = validate(self.workspace(("out/*.txt",), baseline), ["out/*.txt"])
                by_path = {record["path"]: record for record in records}
                self.assertEqual(sorted(by_path), ["out/a.txt", "out/b.txt", "out/c.txt"])
                self.assertFalse(by_path["out/b.txt"]["unchanged_allowed"])
                self.assertNotIn("reason", by_path["out/b.txt"])
                for path in ("out/a.txt", "out/c.txt"):
                    self.assertTrue(by_path[path]["unchanged_allowed"])
                    self.assertEqual(by_path[path]["reason"], "glob_sibling_unchanged")

    def test_zero_of_three_changed_still_fails_per_match(self) -> None:
        baseline = self.three_files(())
        for label, validate in _VALIDATORS.items():
            with self.subTest(validator=label):
                with self.assertRaises(worker_workspace.WorkspaceError) as caught:
                    validate(self.workspace(("out/*.txt",), baseline), ["out/*.txt"])
                message = str(caught.exception)
                for name in ("a", "b", "c"):
                    self.assertIn(f"required_output_unchanged:out/{name}.txt", message)

    def test_exact_path_unchanged_still_fails(self) -> None:
        baseline = self.three_files(("out/b.txt",))
        for label, validate in _VALIDATORS.items():
            with self.subTest(validator=label):
                with self.assertRaisesRegex(
                    worker_workspace.WorkspaceError, "required_output_unchanged:out/a.txt"
                ):
                    validate(self.workspace(("out/a.txt",), baseline), ["out/a.txt"])

    def test_inherited_unchanged_sibling_without_replay_still_fails(self) -> None:
        baseline = self.three_files(("out/b.txt",))
        for label, validate in _VALIDATORS.items():
            with self.subTest(validator=label):
                workspace = worker_workspace.WorkerWorkspace(
                    request_id="nf01834",
                    repo=self.repo,
                    path=self.worktree,
                    home=self.home,
                    allowed_writes=("out/*.txt",),
                    parent_baseline=dict(baseline),
                    workspace_baseline=dict(baseline),
                    inherited_rework_paths=("out/a.txt",),
                )
                with self.assertRaisesRegex(
                    worker_workspace.WorkspaceError, "required_output_unchanged:out/a.txt"
                ):
                    validate(workspace, ["out/*.txt"])

    def test_unchanged_sibling_with_parent_mismatch_still_fails(self) -> None:
        baseline = self.three_files(("out/b.txt",))
        parent = dict(baseline)
        parent["out/a.txt"] = "file:644:" + "0" * 64
        for label, validate in _VALIDATORS.items():
            with self.subTest(validator=label):
                workspace = worker_workspace.WorkerWorkspace(
                    request_id="nf01834",
                    repo=self.repo,
                    path=self.worktree,
                    home=self.home,
                    allowed_writes=("out/*.txt",),
                    parent_baseline=dict(parent),
                    workspace_baseline=dict(baseline),
                )
                with self.assertRaisesRegex(
                    worker_workspace.WorkspaceError, "required_output_unchanged:out/a.txt"
                ):
                    validate(workspace, ["out/*.txt"])


if __name__ == "__main__":
    unittest.main()
