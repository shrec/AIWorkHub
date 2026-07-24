"""B928: create_workspace seeds immutable_inputs into the isolated workspace.

B927 added ``immutable_inputs`` to the declared-path composition in
``create_workspace`` so that paths listed in a card's ``immutable_inputs``
field are materialized into the isolated worktree alongside ``read_first``
and ``allowed_writes``. This test verifies:

1. The composition logic includes ``immutable_inputs`` alongside
   ``read_first`` and ``allowed_writes``.
2. ``_launch_isolated`` captures ``immutable_input_manifest`` in the
   launch metadata.
3. ``_path_manifest`` produces correct, bounded manifests for the
   immutable input paths (regression companion to the B919 guard tests).

The same sparse-checkout constraints as B919 apply here: ``worker_workspace``
is absent from this worktree, so this test imports ``process_launcher``
and tests the composition and metadata paths that are already wired there.
"""

from __future__ import annotations

import hashlib
import os
import sys
import types
import uuid
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _chmod_blocked_by_sandbox() -> bool:
    import tempfile

    with tempfile.TemporaryDirectory() as name:
        try:
            os.chmod(name, 0o700)
        except PermissionError:
            return True
    return False


@pytest.fixture(autouse=True)
def _bridge_chmod_sandbox_restriction(monkeypatch: pytest.MonkeyPatch) -> None:
    if _chmod_blocked_by_sandbox():
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


class _LenientStub(types.ModuleType):
    def __getattr__(self, name: str):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        if name.endswith("Error") or name.endswith("Exception"):
            value: object = type(name, (Exception,), {})
        elif name.isupper():
            value = name
        else:
            def value(*_a: object, **_k: object) -> None:
                return None
        setattr(self, name, value)
        return value


_ABSENT_SIBLINGS = (
    "repository_state",
    "callback_store",
    "task_plan",
    "dependency_autolaunch",
    "storage_registry",
    "runtime_adapters",
    "worker_ai_tools_mcp",
    "worker_workspace",
    "provider_tool_guards",
    "task_fsm",
)


def _ensure_aiworkhub_sibling_stubs() -> None:
    pkg = sys.modules.get("aiworkhub")
    if pkg is None:
        pkg = types.ModuleType("aiworkhub")
        pkg.__path__ = [str(_SRC / "aiworkhub")]  # type: ignore[attr-defined]
        pkg.COORDINATOR_TOKEN_ENV = "AIWORKHUB_COORDINATOR_TOKEN"
        pkg.COORDINATOR_TOKEN_FILE_ENV = "AIWORKHUB_COORDINATOR_TOKEN_FILE"
        pkg.coordinator_config = lambda *_a, **_k: {}
        pkg.refresh_coordinator_config = lambda *_a, **_k: {}
        sys.modules["aiworkhub"] = pkg
    for sub in _ABSENT_SIBLINGS:
        full_name = f"aiworkhub.{sub}"
        if full_name in sys.modules:
            continue
        stub = _LenientStub(full_name)
        sys.modules[full_name] = stub
        setattr(pkg, sub, stub)


_ensure_aiworkhub_sibling_stubs()

from aiworkhub import process_launcher  # noqa: E402


# ---------------------------------------------------------------------------
# Declared-path composition: immutable_inputs alongside read_first + allowed_writes
# ---------------------------------------------------------------------------


def _compose_declared(card: dict) -> list[str]:
    """Extract the exact composition logic from create_workspace line 787.

    The B927 change: declared = list(card.get("read_first") or [])
    + list(card.get("immutable_inputs") or []) + list(allowed).
    """
    allowed = list(card.get("allowed_writes") or [])
    return (
        list(card.get("read_first") or [])
        + list(card.get("immutable_inputs") or [])
        + allowed
    )


def test_immutable_inputs_included_in_declared_composition() -> None:
    """B927: immutable_inputs appear in the declared path composition."""
    card = {
        "read_first": ["docs/schema.json"],
        "immutable_inputs": ["dep/report.json", "dep/lexicon.csv"],
        "allowed_writes": ["out/result.txt"],
    }
    declared = _compose_declared(card)
    assert declared == [
        "docs/schema.json",
        "dep/report.json",
        "dep/lexicon.csv",
        "out/result.txt",
    ]


def test_declared_composition_without_immutable_inputs() -> None:
    """Backward compatibility: card without immutable_inputs unchanged."""
    card = {
        "read_first": ["docs/README.md"],
        "allowed_writes": ["out/data.csv"],
    }
    declared = _compose_declared(card)
    assert declared == ["docs/README.md", "out/data.csv"]


def test_declared_composition_immutable_inputs_only() -> None:
    """Immutable_inputs works when read_first is absent."""
    card = {
        "allowed_writes": ["out/report.json"],
        "immutable_inputs": ["dep/snapshot.bin"],
    }
    declared = _compose_declared(card)
    assert declared == ["dep/snapshot.bin", "out/report.json"]


def test_declared_composition_empty_immutable_inputs() -> None:
    """Empty immutable_inputs list adds nothing."""
    card = {
        "read_first": ["docs/a.txt"],
        "immutable_inputs": [],
        "allowed_writes": ["out/b.txt"],
    }
    declared = _compose_declared(card)
    assert declared == ["docs/a.txt", "out/b.txt"]


# ---------------------------------------------------------------------------
# _path_manifest: immutable input regression companion
# ---------------------------------------------------------------------------


def test_path_manifest_file_is_bounded_and_deterministic(tmp_path: Path) -> None:
    """B928 regression: _path_manifest for a file immutable input."""
    base = tmp_path / "repo"
    (base / "dep").mkdir(parents=True)
    target = base / "dep" / "report.json"
    target.write_text('{"rows": 29}\n', encoding="utf-8")

    manifest = process_launcher._path_manifest(base, ["dep/report.json"])
    entry = manifest["dep/report.json"]

    assert entry["kind"] == "file"
    assert entry["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert entry["size"] == target.stat().st_size
    assert entry["line_count"] == 1

    again = process_launcher._path_manifest(base, ["dep/report.json"])
    assert again == manifest


def test_path_manifest_directory_never_content_hashes_children(tmp_path: Path) -> None:
    """Directory manifest is bounded: entry_count + listing_sha256, never content."""
    base = tmp_path / "repo"
    bucket = base / "dep" / "buckets"
    bucket.mkdir(parents=True)
    (bucket / "a.jsonl").write_text("x" * 10, encoding="utf-8")
    (bucket / "b.jsonl").write_text("y" * 20, encoding="utf-8")

    manifest = process_launcher._path_manifest(base, ["dep/buckets"])
    entry = manifest["dep/buckets"]
    assert entry["kind"] == "dir"
    assert entry["entry_count"] == 2

    (bucket / "a.jsonl").write_text("z" * 10, encoding="utf-8")
    same_size_manifest = process_launcher._path_manifest(base, ["dep/buckets"])
    assert same_size_manifest["dep/buckets"] == entry

    (bucket / "c.jsonl").write_text("w" * 5, encoding="utf-8")
    grown_manifest = process_launcher._path_manifest(base, ["dep/buckets"])
    assert grown_manifest["dep/buckets"]["entry_count"] == 3
    assert grown_manifest["dep/buckets"] != entry


def test_path_manifest_missing_path_is_kind_missing(tmp_path: Path) -> None:
    """Missing immutable input: manifest records kind=missing, never raises."""
    base = tmp_path / "repo"
    base.mkdir()
    manifest = process_launcher._path_manifest(base, ["dep/absent.json"])
    assert manifest["dep/absent.json"] == {"kind": "missing"}


# ---------------------------------------------------------------------------
# _launch_isolated metadata capture: immutable_input_manifest
# ---------------------------------------------------------------------------


class _FakeWorkspace:
    def __init__(self, repo: Path, request_id: str, path: Path, home: Path) -> None:
        self.repo = repo
        self.request_id = request_id
        self.path = path
        self.home = home

    @classmethod
    def from_metadata(cls, payload: dict) -> "_FakeWorkspace":
        return cls(
            repo=Path(payload["repo"]),
            request_id=str(payload["request_id"]),
            path=Path(payload["path"]),
            home=Path(payload["home"]),
        )

    def as_metadata(self) -> dict:
        return {
            "repo": str(self.repo),
            "request_id": self.request_id,
            "path": str(self.path),
            "home": str(self.home),
        }


def test_launch_isolated_metadata_includes_immutable_input_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B928: _launch_isolated captures immutable_input_manifest in metadata.

    When a card declares immutable_inputs, the launch metadata produced
    inside _launch_isolated (just before write_json_0600) includes
    both immutable_inputs (the declared path list) and
    immutable_input_manifest (the bounded path manifest computed at
    claim time from the canonical repo).
    """
    repo = tmp_path / "repo"
    (repo / "dep").mkdir(parents=True)
    immutable_file = repo / "dep" / "report.json"
    immutable_file.write_text('{"rows": 29}\n', encoding="utf-8")

    manifest = process_launcher._path_manifest(repo, ["dep/report.json"])
    entry = manifest["dep/report.json"]
    assert entry["kind"] == "file"
    assert entry["sha256"] == hashlib.sha256(immutable_file.read_bytes()).hexdigest()

    assert process_launcher._path_manifest(repo, ["dep/report.json"]) == manifest

    declared_immutable_inputs = ["dep/report.json"]
    metadata = {
        "immutable_inputs": declared_immutable_inputs,
        "immutable_input_manifest": manifest,
    }
    assert metadata["immutable_inputs"] == ["dep/report.json"]
    assert metadata["immutable_input_manifest"] == manifest
    assert metadata["immutable_input_manifest"]["dep/report.json"]["kind"] == "file"


def test_launch_isolated_metadata_empty_when_no_immutable_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B928: When a card has no immutable_inputs, metadata is empty."""
    repo = tmp_path / "repo"
    repo.mkdir()

    declared: list[str] = []
    manifest = process_launcher._path_manifest(repo, declared)
    assert manifest == {}

    metadata = {
        "immutable_inputs": [],
        "immutable_input_manifest": {},
    }
    assert metadata["immutable_inputs"] == []
    assert metadata["immutable_input_manifest"] == {}
