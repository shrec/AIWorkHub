from __future__ import annotations

import os
from pathlib import Path

import pytest

from aiworkhub import worker_workspace
from aiworkhub.worker_workspace import WorkspaceError


def _workspace(tmp_path: Path) -> worker_workspace.WorkerWorkspace:
    worktree = tmp_path / "worktree"
    home = tmp_path / "home"
    worktree.mkdir()
    home.mkdir()
    return worker_workspace.WorkerWorkspace(
        request_id="v6",
        repo=tmp_path,
        path=worktree,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )


@pytest.fixture
def mock_approved_site(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    monkeypatch.setattr(
        worker_workspace.site, "getusersitepackages", lambda: str(site_packages)
    )
    return site_packages


class TestForeignOwnerStatSimulation:
    def test_os_stat_result_index_4_is_st_uid(self, tmp_path: Path) -> None:
        stat = tmp_path.stat()
        # POSIX os.stat_result: index 0 == st_mode, index 1 == st_ino,
        # index 4 == st_uid. Build the foreign-owner clone by replacing ONLY
        # sequence index 4 and confirm every other entry -- including st_mode
        # at index 0 -- is preserved (real Path.stat signature intact).
        assert stat[0] == stat.st_mode
        assert stat[4] == stat.st_uid
        foreign_uid = 999997
        cloned = os.stat_result(
            tuple(stat[i] if i != 4 else foreign_uid for i in range(len(stat)))
        )
        assert cloned.st_uid == foreign_uid
        assert cloned.st_mode == stat.st_mode
        for index in range(len(stat)):
            if index != 4:
                assert cloned[index] == stat[index]

    def test_foreign_owner_stat_preserves_real_directory_status(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        foreign_uid = 999999
        real_stat = Path.stat

        def _mocked_stat(self, *args, **kwargs):
            result = real_stat(self, *args, **kwargs)
            new_seq = tuple(result[i] if i != 4 else foreign_uid for i in range(10))
            return os.stat_result(new_seq)

        monkeypatch.setattr(Path, "stat", _mocked_stat)
        assert tmp_path.stat().st_uid == foreign_uid
        assert tmp_path.is_dir() is True

    def test_foreign_owner_stat_does_not_disturb_pytest_internals(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        foreign_uid = 999998
        original_mode = tmp_path.stat().st_mode
        real_stat = Path.stat

        def _mocked_stat(self, *args, **kwargs):
            result = real_stat(self, *args, **kwargs)
            return os.stat_result(tuple(result[i] if i != 4 else foreign_uid for i in range(10)))

        monkeypatch.setattr(Path, "stat", _mocked_stat)
        mutated_mode = tmp_path.stat().st_mode
        assert original_mode == mutated_mode


class TestRelativePythonpathBeneathWorkspace:
    def test_dot_resolves_under_workspace_landlock(
        self, tmp_path: Path, mock_approved_site: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        resolved = worker_workspace.resolve_validation_pythonpath(
            workspace, "landlock", (".",)
        )
        assert resolved == str(workspace.path)

    def test_dot_resolves_under_workspace_bubblewrap(
        self, tmp_path: Path, mock_approved_site: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        resolved = worker_workspace.resolve_validation_pythonpath(
            workspace, "bubblewrap", (".",)
        )
        assert resolved == worker_workspace.SANDBOX_WORKSPACE

    def test_relative_resolves_under_workspace_landlock(
        self, tmp_path: Path, mock_approved_site: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        (workspace.path / "rel_dir").mkdir()
        resolved = worker_workspace.resolve_validation_pythonpath(
            workspace, "landlock", ("rel_dir",)
        )
        assert resolved == str(workspace.path / "rel_dir")

    def test_relative_resolves_under_workspace_bubblewrap(
        self, tmp_path: Path, mock_approved_site: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        (workspace.path / "rel_dir").mkdir()
        resolved = worker_workspace.resolve_validation_pythonpath(
            workspace, "bubblewrap", ("rel_dir",)
        )
        assert resolved == f"{worker_workspace.SANDBOX_WORKSPACE}/rel_dir"

    def test_relative_not_directory_fails(
        self, tmp_path: Path, mock_approved_site: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        (workspace.path / "rel_file").touch()
        with pytest.raises(WorkspaceError, match="validation_pythonpath_not_directory:rel_file"):
            worker_workspace.resolve_validation_pythonpath(
                workspace, "landlock", ("rel_file",)
            )

    def test_relative_symlink_component_fails(
        self, tmp_path: Path, mock_approved_site: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        target = tmp_path / "target_dir"
        target.mkdir()
        (workspace.path / "link_dir").symlink_to(target)
        with pytest.raises(WorkspaceError, match="symlink_path_component_forbidden"):
            worker_workspace.resolve_validation_pythonpath(
                workspace, "landlock", ("link_dir",)
            )

    def test_relative_escape_fails(
        self, tmp_path: Path, mock_approved_site: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        with pytest.raises(WorkspaceError, match="path_escapes_workspace"):
            worker_workspace.resolve_validation_pythonpath(
                workspace, "landlock", ("../escape",)
            )

    def test_relative_mixed_components_succeeds(
        self, tmp_path: Path, mock_approved_site: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        (workspace.path / "rel_dir1").mkdir()
        (workspace.path / "rel_dir2").mkdir()
        resolved = worker_workspace.resolve_validation_pythonpath(
            workspace, "landlock", (".", "rel_dir1", "rel_dir2")
        )
        assert resolved == os.pathsep.join(
            [str(workspace.path), str(workspace.path / "rel_dir1"), str(workspace.path / "rel_dir2")]
        )


class TestAbsolutePythonpathSymlinkRejection:
    def test_absolute_approved_site_succeeds_landlock(
        self, tmp_path: Path, mock_approved_site: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        resolved = worker_workspace.resolve_validation_pythonpath(
            workspace, "landlock", (str(mock_approved_site),)
        )
        assert resolved == str(mock_approved_site.resolve())

    def test_absolute_approved_site_succeeds_bubblewrap(
        self, tmp_path: Path, mock_approved_site: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        resolved = worker_workspace.resolve_validation_pythonpath(
            workspace, "bubblewrap", (str(mock_approved_site),)
        )
        assert resolved == "/validation-pythonpath/0"

    def test_absolute_symlink_fails_resolve(
        self, tmp_path: Path, mock_approved_site: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        target = tmp_path / "target_dir"
        target.mkdir()
        symlink_path = tmp_path / "symlinked_site"
        symlink_path.symlink_to(target)
        
        with pytest.raises(WorkspaceError, match="validation_pythonpath_absolute_component_forbidden"):
            worker_workspace.resolve_validation_pythonpath(
                workspace, "landlock", (str(symlink_path),)
            )

    def test_absolute_non_approved_fails_resolve(
        self, tmp_path: Path, mock_approved_site: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        fake_dir = tmp_path / "fake_dir"
        fake_dir.mkdir()
        with pytest.raises(WorkspaceError, match="validation_pythonpath_absolute_component_forbidden"):
            worker_workspace.resolve_validation_pythonpath(
                workspace, "landlock", (str(fake_dir),)
            )

    def test_absolute_lexical_symlink_fails_before_resolve(
        self, tmp_path: Path, mock_approved_site: Path
    ) -> None:
        target = tmp_path / "site_target"
        target.mkdir()
        mock_approved_site.rmdir()
        mock_approved_site.symlink_to(target)
        
        workspace = _workspace(tmp_path)
        with pytest.raises(WorkspaceError):
            worker_workspace.resolve_validation_pythonpath(
                workspace, "landlock", (str(mock_approved_site),)
            )

    def test_absolute_lexical_symlink_fails_before_readonly(
        self, tmp_path: Path, mock_approved_site: Path
    ) -> None:
        target = tmp_path / "readonly_target"
        target.mkdir()
        mock_approved_site.rmdir()
        mock_approved_site.symlink_to(target)
        
        with pytest.raises(WorkspaceError):
            worker_workspace._validation_pythonpath_readonly_dirs((str(mock_approved_site),))


class TestReadonlyDirs:
    def test_empty_components(self) -> None:
        assert worker_workspace._validation_pythonpath_readonly_dirs(()) == ()

    def test_skips_relative(self) -> None:
        assert worker_workspace._validation_pythonpath_readonly_dirs(("rel", "./rel")) == ()

    def test_extracts_absolute(
        self, tmp_path: Path, mock_approved_site: Path
    ) -> None:
        dirs = worker_workspace._validation_pythonpath_readonly_dirs((str(mock_approved_site),))
        assert dirs == (mock_approved_site.resolve(),)

    def test_non_approved_fails(
        self, tmp_path: Path, mock_approved_site: Path
    ) -> None:
        fake_dir = tmp_path / "fake_dir"
        fake_dir.mkdir()
        with pytest.raises(WorkspaceError, match="validation_pythonpath_absolute_component_forbidden"):
            worker_workspace._validation_pythonpath_readonly_dirs((str(fake_dir),))

    def test_not_directory_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        not_dir = tmp_path / "file.txt"
        not_dir.touch()
        monkeypatch.setattr(
            worker_workspace.site, "getusersitepackages", lambda: str(not_dir)
        )
        with pytest.raises(WorkspaceError, match="validation_pythonpath_absolute_component_forbidden"):
            worker_workspace._validation_pythonpath_readonly_dirs((str(not_dir),))

    def test_symlink_absolute_fails(
        self, tmp_path: Path, mock_approved_site: Path
    ) -> None:
        target = tmp_path / "target_dir"
        target.mkdir()
        symlink_path = tmp_path / "symlinked_site"
        symlink_path.symlink_to(target)
        
        with pytest.raises(WorkspaceError):
            worker_workspace._validation_pythonpath_readonly_dirs((str(symlink_path),))

    def test_lexical_intermediate_symlink_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        real_parent = tmp_path / "real_parent"
        real_parent.mkdir()
        target_dir = real_parent / "site"
        target_dir.mkdir()
        
        symlink_parent = tmp_path / "symlink_parent"
        symlink_parent.symlink_to(real_parent)
        
        target_path = symlink_parent / "site"
        
        monkeypatch.setattr(
            worker_workspace.site, "getusersitepackages", lambda: str(target_path)
        )
        
        with pytest.raises(WorkspaceError):
            worker_workspace._validation_pythonpath_readonly_dirs((str(target_path),))

    def test_approved_site_returns_resolved(
        self, tmp_path: Path, mock_approved_site: Path
    ) -> None:
        dirs = worker_workspace._validation_pythonpath_readonly_dirs((str(mock_approved_site),))
        assert len(dirs) == 1
        assert not dirs[0].is_symlink()
