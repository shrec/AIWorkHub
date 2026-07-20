from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from geoai_task_mcp import worker_workspace  # noqa: E402


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"file:{path.stat().st_mode & 0o777:o}:{digest}"


class RequiredOutputRecursiveGlobB657V2Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.worktree = self.root / "worktree"
        self.home = self.root / "home"
        self.repo.mkdir()
        self.worktree.mkdir()
        self.home.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def workspace(
        self,
        *,
        allowed_writes: tuple[str, ...] = ("shards/**",),
        workspace_baseline: dict[str, str | None] | None = None,
        parent_baseline: dict[str, str | None] | None = None,
    ) -> worker_workspace.WorkerWorkspace:
        return worker_workspace.WorkerWorkspace(
            request_id="b657-v2",
            repo=self.repo,
            path=self.worktree,
            home=self.home,
            allowed_writes=allowed_writes,
            parent_baseline=parent_baseline or {},
            workspace_baseline=workspace_baseline or {},
        )

    def write(self, relative: str, payload: bytes) -> Path:
        path = self.worktree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def assert_workspace_error(self, match: str, workspace, required_outputs) -> None:
        with self.assertRaisesRegex(worker_workspace.WorkspaceError, match):
            worker_workspace.validate_required_outputs(workspace, required_outputs)

    def test_terminal_double_star_returns_nested_files_never_directory_records(self) -> None:
        self.write("shards/part-000.jsonl", b"root shard\n")
        self.write("shards/a/b/part-001.jsonl", b"nested shard\n")
        (self.worktree / "shards" / "a" / "empty-dir").mkdir(parents=True)

        records = worker_workspace.validate_required_outputs(
            self.workspace(),
            ["shards/**"],
        )

        self.assertEqual(
            [record["path"] for record in records],
            ["shards/a/b/part-001.jsonl", "shards/part-000.jsonl"],
        )
        self.assertEqual({record["pattern"] for record in records}, {"shards/**"})
        self.assertTrue(all(record["bytes"] > 0 for record in records))

    def test_empty_directory_only_and_no_match_recursive_patterns_fail_no_matches(self) -> None:
        self.assert_workspace_error(
            "required_output_no_matches:shards/\\*\\*",
            self.workspace(),
            ["shards/**"],
        )

        (self.worktree / "shards" / "nested").mkdir(parents=True)
        self.assert_workspace_error(
            "required_output_no_matches:shards/\\*\\*",
            self.workspace(),
            ["shards/**"],
        )

        self.assert_workspace_error(
            "required_output_no_matches:missing/\\*\\*",
            self.workspace(allowed_writes=("missing/**",)),
            ["missing/**"],
        )

    def test_non_terminal_globs_keep_pathlib_style_behavior(self) -> None:
        self.write("shards/nested/part-001.jsonl", b"nested shard\n")
        self.assert_workspace_error(
            "required_output_no_matches:shards/\\*",
            self.workspace(allowed_writes=("shards/*",)),
            ["shards/*"],
        )

    def test_recursive_matches_reject_symlink_and_traversal_or_scope_escape(self) -> None:
        target = self.write("real.bin", b"payload\n")
        link = self.worktree / "shards" / "link.bin"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)

        self.assert_workspace_error(
            "(required_output_symlink|symlink_path_component_forbidden)",
            self.workspace(),
            ["shards/**"],
        )
        self.assert_workspace_error(
            "unsafe_repo_path",
            self.workspace(),
            ["../escape/**"],
        )
        self.assert_workspace_error(
            "required_output_not_allowed:outside/\\*\\*",
            self.workspace(),
            ["outside/**"],
        )

    def test_zero_byte_unchanged_and_explicit_file_rules_remain_fail_closed(self) -> None:
        zero = self.write("shards/zero.bin", b"")
        self.assert_workspace_error(
            "required_output_zero_bytes:shards/zero.bin",
            self.workspace(),
            ["shards/**"],
        )

        zero.write_bytes(b"baseline\n")
        baseline = {"shards/zero.bin": _file_hash(zero)}
        self.assert_workspace_error(
            "required_output_unchanged:shards/zero.bin",
            self.workspace(workspace_baseline=baseline),
            ["shards/**"],
        )

        self.write("explicit/result.txt", b"ok\n")
        records = worker_workspace.validate_required_outputs(
            self.workspace(allowed_writes=("explicit/result.txt",)),
            ["explicit/result.txt"],
        )
        self.assertEqual([record["path"] for record in records], ["explicit/result.txt"])


if __name__ == "__main__":
    unittest.main()
