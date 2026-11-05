"""NF-2026-01953: the sealed-tree guard names what changed and voids only what it must.

Field report: a manager dispose refused twice with a bare
``finding_reproduction_sealed_tree_mutated`` while the sealed worktree was
clean -- a canonical working-tree change made while the spec ran (the manager
editing, another card accepted) voided the run even on paths neither
reproduction side read, and the refusal named no path. The guard now compares a
per-(root, path) manifest: a sealed-worktree change, or a change at a candidate
path under either root, still raises and names root, path and both hashes; a
canonical change at a path no side materialized is signed into the receipt as
``concurrent_canonical_changes``; an unknown materialized set keeps the refusal.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_RECEIPTS_PATH = Path(__file__).resolve().with_name("test_finding_reproduction_receipts.py")
_RECEIPTS_MODULE = "_nf01953_finding_reproduction_receipts"


def _load_receipts() -> Any:
    """The existing ``_Trees`` fixture module, loaded once by file path."""
    module = sys.modules.get(_RECEIPTS_MODULE)
    if module is None:
        spec = importlib.util.spec_from_file_location(_RECEIPTS_MODULE, _RECEIPTS_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[_RECEIPTS_MODULE] = module
        spec.loader.exec_module(module)
    return module


rx = _load_receipts()
fr = rx.fr
process_launcher = rx.process_launcher

SPEC = "pytest:tests/test_calc.py::test_add"
FINDING = "reviewer:correctness:finding-1"
REVIEWER = "r" * 64
PREFIX = "finding_reproduction_sealed_tree_mutated:"
# What a review workspace of this fixture baselines from the canonical tree.
MATERIALIZED = ("pytest.ini", "src/calc.py", "tests/test_calc.py")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _during_candidate_run(trees: Any, write) -> None:
    """Run ``write`` once, while the first (candidate) side is executing."""
    real = trees.run
    ran: list[Any] = []

    def run(workspace, commands):
        if not ran:
            write()
        ran.append(workspace)
        return real(workspace, commands)

    trees.run = run


def _target(trees: Any, materialized=MATERIALIZED) -> Any:
    return dataclasses.replace(trees.target(), canonical_materialized=lambda: materialized)


def _reproduce(target: Any) -> dict[str, Any]:
    spec = fr.parse_reproduction_spec(SPEC)
    assert spec is not None
    return fr.reproduce_finding(
        spec,
        finding_id=FINDING,
        candidate_sha256=rx.CANDIDATE,
        reviewer_receipt_sha256=REVIEWER,
        target=target,
        coordinator_key=fr.receipt_key(rx.KEY),
    )


def _entries(excinfo: pytest.ExceptionInfo[RuntimeError]) -> list[dict[str, Any]]:
    message = str(excinfo.value)
    assert message.startswith(PREFIX)
    assert len(message) <= 2048
    entries = json.loads(message[len(PREFIX):])
    assert isinstance(entries, list) and 1 <= len(entries) <= 8
    return entries


# --------------------------------------------------------------------------
# the field false positive, reproduced with the existing fixture
# --------------------------------------------------------------------------
def test_concurrent_canonical_change_no_side_read_is_signed_and_listed(
    tmp_path: Path,
) -> None:
    trees = rx._Trees(tmp_path)
    _during_candidate_run(
        trees, lambda: _write(trees.canonical / "docs" / "manager_notes.md", "editing\n")
    )

    receipt = _reproduce(_target(trees))

    assert receipt["classification"] == fr.REPRODUCED
    assert receipt["concurrent_canonical_changes"] == ["docs/manager_notes.md"]
    assert receipt["concurrent_canonical_change_count"] == 1
    key = fr.receipt_key(rx.KEY)
    assert fr.verify_receipt(receipt, key)
    assert fr.receipt_binds(
        receipt,
        finding_id=FINDING,
        spec_text=SPEC,
        candidate_sha256=rx.CANDIDATE,
        reviewer_receipt_sha256=REVIEWER,
    )
    # A replayed dispose returns the stored receipt and never reruns the spec.
    replay = fr.dispose_reproducer(
        coordinator_key=lambda: rx.KEY,
        resolve_target=lambda spec: pytest.fail("a verified stored receipt was rerun"),
    )
    assert replay(
        finding_id=FINDING,
        spec_text=SPEC,
        candidate_sha256=rx.CANDIDATE,
        reviewer_receipt_sha256=REVIEWER,
        stored=receipt,
    ) == receipt
    # Any edit to the listed changes breaks the signature.
    forged = {**receipt, "concurrent_canonical_changes": []}
    assert not fr.verify_receipt(forged, key)


def test_a_clean_run_carries_no_concurrent_changes(tmp_path: Path) -> None:
    receipt = _reproduce(_target(rx._Trees(tmp_path)))
    assert "concurrent_canonical_changes" not in receipt
    assert fr.verify_receipt(receipt, fr.receipt_key(rx.KEY))


def test_an_unknown_materialized_set_keeps_the_refusal(tmp_path: Path) -> None:
    trees = rx._Trees(tmp_path)
    notes = trees.canonical / "docs" / "manager_notes.md"
    _during_candidate_run(trees, lambda: _write(notes, "editing\n"))

    with pytest.raises(RuntimeError) as excinfo:
        _reproduce(_target(trees, materialized=None))

    assert _entries(excinfo) == [
        {
            "root": "canonical",
            "path": "docs/manager_notes.md",
            "before_sha256": None,
            "after_sha256": _sha(notes),
        }
    ]


def test_the_default_target_declares_no_materialized_set(tmp_path: Path) -> None:
    trees = rx._Trees(tmp_path)
    _during_candidate_run(trees, lambda: _write(trees.canonical / "docs" / "x.md", "x\n"))
    with pytest.raises(RuntimeError, match="sealed_tree_mutated:"):
        _reproduce(trees.target())


# --------------------------------------------------------------------------
# what still raises, by root and path
# --------------------------------------------------------------------------
def test_a_sealed_candidate_path_change_names_root_path_and_both_hashes(
    tmp_path: Path,
) -> None:
    trees = rx._Trees(tmp_path)
    calc = trees.candidate / "src" / "calc.py"
    before = _sha(calc)
    _during_candidate_run(trees, lambda: _write(calc, "tampered\n"))

    with pytest.raises(RuntimeError) as excinfo:
        _reproduce(_target(trees))

    assert {
        "root": "sealed",
        "path": "src/calc.py",
        "before_sha256": before,
        "after_sha256": _sha(calc),
    } in _entries(excinfo)


def test_a_new_sealed_worktree_file_raises_with_a_null_before_hash(tmp_path: Path) -> None:
    trees = rx._Trees(tmp_path)
    extra = trees.candidate / "src" / "extra.py"
    _during_candidate_run(trees, lambda: _write(extra, "x = 1\n"))

    with pytest.raises(RuntimeError) as excinfo:
        _reproduce(_target(trees))

    assert _entries(excinfo) == [
        {
            "root": "sealed",
            "path": "src/extra.py",
            "before_sha256": None,
            "after_sha256": _sha(extra),
        }
    ]


def test_a_canonical_candidate_path_raises_even_when_no_side_materialized_it(
    tmp_path: Path,
) -> None:
    trees = rx._Trees(tmp_path)
    calc = trees.canonical / "src" / "calc.py"
    before = _sha(calc)
    _during_candidate_run(trees, lambda: _write(calc, "def add(a, b):\n    return 0\n"))

    with pytest.raises(RuntimeError) as excinfo:
        _reproduce(_target(trees, materialized=()))

    assert _entries(excinfo) == [
        {
            "root": "canonical",
            "path": "src/calc.py",
            "before_sha256": before,
            "after_sha256": _sha(calc),
        }
    ]


def test_a_canonical_path_a_side_materialized_still_raises(tmp_path: Path) -> None:
    trees = rx._Trees(tmp_path)
    test_file = trees.canonical / "tests" / "test_calc.py"
    _during_candidate_run(
        trees, lambda: _write(test_file, "def test_add():\n    pass\n")
    )

    with pytest.raises(RuntimeError) as excinfo:
        _reproduce(_target(trees))

    assert [(row["root"], row["path"]) for row in _entries(excinfo)] == [
        ("canonical", "tests/test_calc.py")
    ]


def test_a_guarded_change_alongside_a_concurrent_one_names_only_the_guarded(
    tmp_path: Path,
) -> None:
    trees = rx._Trees(tmp_path)

    def write() -> None:
        _write(trees.canonical / "docs" / "manager_notes.md", "editing\n")
        _write(trees.candidate / "src" / "calc.py", "tampered\n")

    _during_candidate_run(trees, write)
    with pytest.raises(RuntimeError) as excinfo:
        _reproduce(_target(trees))
    assert [(row["root"], row["path"]) for row in _entries(excinfo)] == [
        ("sealed", "src/calc.py")
    ]


# --------------------------------------------------------------------------
# bounds
# --------------------------------------------------------------------------
def test_the_refusal_is_bounded_to_eight_sorted_entries(tmp_path: Path) -> None:
    trees = rx._Trees(tmp_path)

    def write() -> None:
        for index in range(20):
            _write(trees.candidate / "gen" / f"file_{index:02d}.py", f"x = {index}\n")

    _during_candidate_run(trees, write)
    with pytest.raises(RuntimeError) as excinfo:
        _reproduce(_target(trees))
    entries = _entries(excinfo)
    paths = [row["path"] for row in entries]
    assert paths == sorted(paths) and paths[0] == "gen/file_00.py"
    assert all(row["root"] == "sealed" for row in entries)


def test_the_refusal_message_never_exceeds_two_kilobytes() -> None:
    keys = [("sealed", f"{index:02d}/" + "p" * 1000) for index in range(12)]
    after = {key: "a" * 64 for key in keys}
    message = fr._mutation_message(keys, {}, after)
    assert message.startswith(PREFIX) and len(message) <= 2048
    entries = json.loads(message[len(PREFIX):])
    assert 1 <= len(entries) <= 8
    assert all(len(row["path"]) <= 256 for row in entries)


def test_concurrent_changes_are_bounded_to_32_sorted_paths(tmp_path: Path) -> None:
    trees = rx._Trees(tmp_path)

    def write() -> None:
        for index in range(40):
            _write(trees.canonical / "notes" / f"n{index:02d}.md", f"{index}\n")

    _during_candidate_run(trees, write)
    receipt = _reproduce(_target(trees))
    assert receipt["concurrent_canonical_changes"] == [
        f"notes/n{index:02d}.md" for index in range(32)
    ]
    assert receipt["concurrent_canonical_change_count"] == 40
    assert fr.verify_receipt(receipt, fr.receipt_key(rx.KEY))


# --------------------------------------------------------------------------
# the manifest itself
# --------------------------------------------------------------------------
def test_manifest_rehashes_earlier_keys_and_refuses_unknown_roots(tmp_path: Path) -> None:
    root = _write(tmp_path / "tree" / "a.txt", "a\n").parent
    manifest = fr.sealed_manifest(
        {fr.CANONICAL_ROOT: root}, [], also=[(fr.CANONICAL_ROOT, "a.txt")]
    )
    assert manifest == {("canonical", "a.txt"): _sha(root / "a.txt")}
    assert fr.sealed_manifest({fr.CANONICAL_ROOT: root}, ["gone.txt"]) == {
        ("canonical", "gone.txt"): None
    }
    with pytest.raises(ValueError, match="sealed_manifest_root_unknown:other"):
        fr.sealed_manifest({fr.CANONICAL_ROOT: root}, [], also=[("other", "a.txt")])
    with pytest.raises(ValueError, match="sealed_manifest_root_unknown:other"):
        fr.sealed_manifest({fr.CANONICAL_ROOT: root}, [], deltas={"other": ()})


# --------------------------------------------------------------------------
# production wiring: the materialized set is the review workspace's baseline
# --------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class _ReviewWorkspace:
    path: Path
    repo: Path
    home: Path
    allowed_writes: tuple[str, ...]
    workspace_baseline: dict[str, str | None]


def _manager_reproduce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    concurrent: str,
    baseline: tuple[str, ...] | None,
) -> dict[str, Any]:
    trees = rx._Trees(tmp_path)
    source = rx.SimpleNamespace(
        path=trees.candidate, repo=trees.canonical, base_oid=rx.BASE_OID, home=tmp_path
    )

    def create_combined_validation_workspace(src, candidate_card, paths):
        path = trees._scratch(trees.candidate)
        if baseline is None:
            return rx._Workspace(
                path=path, repo=trees.canonical, home=tmp_path, allowed_writes=()
            ), {}
        return _ReviewWorkspace(
            path=path, repo=trees.canonical, home=tmp_path, allowed_writes=(),
            workspace_baseline={relative: None for relative in baseline},
        ), {}

    def create_workspace(repo, request_id, card, adapter_id, *, pinned_base_oid=None):
        return rx._Workspace(
            path=trees._scratch(trees.canonical), repo=trees.canonical, home=tmp_path,
            allowed_writes=tuple(card.get("allowed_writes") or ()),
        )

    run = rx._runner()
    ran: list[Any] = []

    def run_validations(workspace, commands, authority, **route):
        if not ran:
            _write(trees.canonical / concurrent, "concurrent edit\n")
        ran.append(workspace)
        return run(workspace.path, list(commands))

    monkeypatch.setattr(
        process_launcher,
        "create_combined_validation_workspace",
        create_combined_validation_workspace,
    )
    monkeypatch.setattr(process_launcher, "create_workspace", create_workspace)
    monkeypatch.setattr(
        process_launcher._worker_workspace,
        "_run",
        lambda argv, **kwargs: rx.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        process_launcher, "cleanup_workspace", lambda repo, path, home: trees._cleanup(path)
    )
    monkeypatch.setattr(
        process_launcher, "_run_validations_with_toolchain_receipt", run_validations
    )
    monkeypatch.setattr(
        process_launcher, "_validation_route_kwargs",
        lambda metadata: {"backend": "fixture", "adapter_id": metadata["adapter_id"]},
    )
    monkeypatch.setattr(
        process_launcher._worker_workspace, "changed_paths",
        lambda workspace: trees.candidate_delta(),
    )
    monkeypatch.setattr(
        process_launcher._worker_workspace, "_canonical_worktree_delta_paths",
        lambda repo: trees.canonical_delta(),
    )
    manager = rx.SimpleNamespace(
        _terminal_authority_key=lambda: rx.KEY,
        _prepared_quality_review=lambda request_id, task_id: {
            "ok": True,
            "prepared": {
                "workspace": source,
                "changed_hashes": dict(rx.CHANGED),
                "worker_adapter_id": "codex_cli",
                "read_only_input_paths": [],
            },
        },
    )
    reproduce = fr.manager_reproducer(
        manager,
        task_id="TARGET",
        request_id="target-request",
        card={"task_id": "TARGET", "allowed_writes": ["src/calc.py"]},
    )
    try:
        return reproduce(
            finding_id=FINDING,
            spec_text=SPEC,
            candidate_sha256=rx.CANDIDATE,
            reviewer_receipt_sha256=REVIEWER,
        )
    finally:
        assert trees.cleaned == trees.created


def test_manager_reproducer_signs_over_a_change_outside_the_review_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _manager_reproduce(
        tmp_path, monkeypatch, concurrent="docs/manager_notes.md", baseline=MATERIALIZED
    )
    assert receipt["classification"] == fr.REPRODUCED
    assert receipt["concurrent_canonical_changes"] == ["docs/manager_notes.md"]
    assert fr.verify_receipt(receipt, fr.receipt_key(rx.KEY))


@pytest.mark.parametrize(
    ("concurrent", "baseline", "path"),
    [
        # The review workspace copied this path from the canonical working tree.
        ("tests/test_calc.py", MATERIALIZED, "tests/test_calc.py"),
        # A candidate path stays guarded whatever the baseline holds.
        ("src/calc.py", (), "src/calc.py"),
        # A workspace with no baseline leaves the materialized set unknown.
        ("docs/manager_notes.md", None, "docs/manager_notes.md"),
    ],
)
def test_manager_reproducer_keeps_the_guard_where_it_must(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    concurrent: str,
    baseline: tuple[str, ...] | None,
    path: str,
) -> None:
    with pytest.raises(RuntimeError) as excinfo:
        _manager_reproduce(tmp_path, monkeypatch, concurrent=concurrent, baseline=baseline)
    assert [(row["root"], row["path"]) for row in _entries(excinfo)] == [
        ("canonical", path)
    ]
