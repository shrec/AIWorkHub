"""Fix batch 10 regressions (NF-2026-00036, NF-2026-00603, NF-2026-00943)."""

from __future__ import annotations

import errno
import hashlib
import os

import pytest

from aiworkhub import platform_io, task_store


def _hard_link_or_skip(source, link):
    try:
        os.link(source, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hard links unavailable: {exc}")


# NF-2026-00036 -------------------------------------------------------------


def test_pinned_paths_refuses_multi_linked_leaf(tmp_path):
    held = tmp_path / "held.py"
    held.write_bytes(b"x = 1\n")
    with platform_io.pinned_paths([tmp_path, held]):
        pass

    _hard_link_or_skip(held, tmp_path / "outside_alias.py")
    with pytest.raises(platform_io.HardLinkRefused) as raised:
        with platform_io.pinned_paths([tmp_path, held]):
            pytest.fail("a multi-linked leaf must not be held")
    assert raised.value.errno == errno.EMLINK
    # Callers that already refuse links keep refusing this one.
    assert isinstance(raised.value, platform_io.ReparsePointRefused)


# NF-2026-00603 -------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="native Windows publication branch")
def test_windows_identity_bound_replace_publishes_held_source(tmp_path):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "canonical"
    source.write_bytes(b"verified")
    destination.write_bytes(b"prior")

    platform_io.identity_bound_durable_atomic_replace(source, destination)

    assert destination.read_bytes() == b"verified"
    assert not source.exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows publication branch")
def test_windows_identity_bound_replace_refuses_concurrently_written_source(tmp_path):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "canonical"
    source.write_bytes(b"verified")
    destination.write_bytes(b"prior")

    with open(source, "r+b") as writer:
        with pytest.raises(OSError):
            platform_io.identity_bound_durable_atomic_replace(source, destination)
        writer.write(b"x")

    assert destination.read_bytes() == b"prior"
    assert source.exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows publication branch")
def test_windows_identity_bound_replace_refuses_multi_linked_source(tmp_path):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "canonical"
    source.write_bytes(b"verified")
    destination.write_bytes(b"prior")
    _hard_link_or_skip(source, tmp_path / "outside_alias.tmp")

    with pytest.raises(platform_io.IdentityBoundPublicationError) as raised:
        platform_io.identity_bound_durable_atomic_replace(source, destination)

    assert raised.value.errno == errno.EMLINK
    assert "publication_source_has_multiple_links" in str(raised.value)
    assert destination.read_bytes() == b"prior"
    assert source.read_bytes() == b"verified"


@pytest.mark.skipif(os.name != "nt", reason="native Windows publication branch")
def test_windows_rename_is_anchored_on_held_parent_handle(tmp_path, monkeypatch):
    held_root = 0x1234
    buffer, size = platform_io._windows_relative_rename_buffer(
        held_root, str(tmp_path / "nested" / "canonical")
    )
    record = platform_io._WindowsFileRenameInfo.from_buffer(buffer)
    offset = platform_io._WindowsFileRenameInfo.FileName.offset
    name = bytes(buffer.raw[offset : offset + record.FileNameLength]).decode("utf-16-le")
    assert record.RootDirectory == held_root
    assert name == "canonical"
    assert record.Flags & 0x1
    assert size >= offset + record.FileNameLength

    # End to end: the commit is issued with the held parent as anchor and
    # lands in exactly that directory.
    seen: list[str] = []
    real = platform_io._windows_relative_rename_buffer

    def _spy(directory_handle, destination_abs):
        seen.append(destination_abs)
        assert directory_handle
        return real(directory_handle, destination_abs)

    monkeypatch.setattr(platform_io, "_windows_relative_rename_buffer", _spy)
    parent = tmp_path / "nested"
    parent.mkdir()
    source = tmp_path / "source.tmp"
    destination = parent / "canonical"
    source.write_bytes(b"verified")
    platform_io.identity_bound_durable_atomic_replace(source, destination)

    assert seen == [os.path.abspath(destination)]
    assert destination.read_bytes() == b"verified"
    assert not source.exists()


def test_windows_rename_buffer_refuses_missing_leaf():
    with pytest.raises(platform_io.IdentityBoundPublicationError) as raised:
        platform_io._windows_relative_rename_buffer(1, "")
    assert raised.value.errno == errno.EINVAL


# NF-2026-00943 -------------------------------------------------------------


def test_replay_candidate_hash_is_single_open_bounded_and_single_linked(tmp_path):
    candidate = tmp_path / "candidate.py"
    payload = b"a" * 4096
    candidate.write_bytes(payload)

    assert task_store._replay_candidate_sha256(candidate) == (
        hashlib.sha256(payload).hexdigest(),
        "",
    )
    assert task_store._replay_candidate_sha256(candidate, max_bytes=1024) == (
        None,
        "bytes_invalid",
    )
    assert task_store._replay_candidate_sha256(tmp_path / "missing.py") == (
        None,
        "read_failed",
    )

    _hard_link_or_skip(candidate, tmp_path / "alias.py")
    assert task_store._replay_candidate_sha256(candidate) == (
        None,
        "not_regular_file",
    )


def test_replay_candidate_hash_refuses_symlink(tmp_path):
    target = tmp_path / "target.py"
    target.write_bytes(b"secret")
    link = tmp_path / "link.py"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    assert task_store._replay_candidate_sha256(link) == (None, "not_regular_file")
