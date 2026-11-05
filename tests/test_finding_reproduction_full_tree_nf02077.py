"""NF-2026-02077: finding reproduction runs both sides on a complete tree.

The candidate side was a sparse review workspace, so a spec importing a package
under ``src/`` was refused and almost every reproduction was UNRUNNABLE. It is
now the combined validation tree (HEAD, sparse definition disabled, canonical
delta, candidate overlay) with an empty write scope; the base side disables its
sparse definition before running and is UNRUNNABLE, naming the cause, when that
fails.
"""

from __future__ import annotations

import importlib.util
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
SPARSE_DISABLE = ["git", "sparse-checkout", "disable"]


def _reproduce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, disable_returncode: int = 0
) -> tuple[dict[str, Any], dict[str, Any], rx._Trees]:
    trees = rx._Trees(tmp_path)
    source = rx.SimpleNamespace(
        path=trees.candidate, repo=trees.canonical, base_oid=rx.BASE_OID, home=tmp_path
    )
    seen: dict[str, Any] = {"events": []}
    events: list[str] = seen["events"]

    def create_combined_validation_workspace(src, candidate_card, paths):
        seen["combined"] = (src, candidate_card, list(paths))
        events.append("create_candidate")
        # A combined tree carries the union write scope; the reproducer drops it.
        return rx._Workspace(
            path=trees._scratch(trees.candidate), repo=trees.canonical, home=tmp_path,
            allowed_writes=("src/calc.py", "docs/notes.md"),
        ), {}

    def create_workspace(repo, request_id, card, adapter_id, *, pinned_base_oid=None):
        events.append("create_base")
        return rx._Workspace(
            path=trees._scratch(trees.canonical), repo=trees.canonical, home=tmp_path,
            allowed_writes=tuple(card.get("allowed_writes") or ()),
        )

    def worker_run(argv, **kwargs):
        events.append("sparse_disable")
        seen["disable"] = (list(argv), kwargs)
        return rx.SimpleNamespace(returncode=disable_returncode, stdout="", stderr="boom")

    run = rx._runner()

    def run_validations(workspace, commands, authority, **route):
        events.append(f"run:{workspace.path.name}")
        seen.setdefault("allowed_writes", []).append(workspace.allowed_writes)
        return run(workspace.path, list(commands))

    def cleanup_workspace(repo, path, home):
        events.append(f"cleanup:{path.name}")
        trees._cleanup(path)

    monkeypatch.setattr(
        process_launcher,
        "create_combined_validation_workspace",
        create_combined_validation_workspace,
    )
    monkeypatch.setattr(process_launcher, "create_workspace", create_workspace)
    monkeypatch.setattr(process_launcher, "cleanup_workspace", cleanup_workspace)
    monkeypatch.setattr(process_launcher._worker_workspace, "_run", worker_run)
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
        card={
            "task_id": "TARGET",
            "allowed_writes": ["src/calc.py"],
            "rework_predecessor": {"x": 1},
        },
    )
    receipt = reproduce(
        finding_id="reviewer:correctness:finding-1",
        spec_text=SPEC,
        candidate_sha256=rx.CANDIDATE,
        reviewer_receipt_sha256="r" * 64,
    )
    seen["source"] = source
    return receipt, seen, trees


def test_candidate_side_is_the_combined_tree_with_an_empty_write_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, seen, trees = _reproduce(tmp_path, monkeypatch)

    src, candidate_card, paths = seen["combined"]
    assert src is seen["source"]
    assert paths == ["src/calc.py"]
    assert candidate_card["task_id"] == "TARGET"
    assert "rework_predecessor" not in candidate_card
    # Neither side keeps the combined tree's union write scope.
    assert seen["allowed_writes"] == [(), ()]
    assert receipt["classification"] == fr.REPRODUCED
    assert trees.cleaned == trees.created


def test_failing_candidate_disables_the_base_sparse_definition_before_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, seen, _trees = _reproduce(tmp_path, monkeypatch)

    assert receipt["classification"] == fr.REPRODUCED
    argv, kwargs = seen["disable"]
    assert argv == SPARSE_DISABLE
    assert kwargs["cwd"].name == "ws1"  # the base worktree, the second one created
    assert kwargs["phase"] == "workspace_provision"
    assert kwargs["timeout"] == process_launcher._worker_workspace.WORKTREE_CREATE_TIMEOUT_SECONDS
    events = seen["events"]
    assert events.index("create_base") < events.index("sparse_disable")
    assert events.index("sparse_disable") < events.index("run:ws1")
    assert events.count("sparse_disable") == 1


def test_failed_sparse_disable_makes_the_base_unrunnable_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, seen, trees = _reproduce(tmp_path, monkeypatch, disable_returncode=128)

    base = receipt["base"]
    assert base["outcome"] == fr.UNRUNNABLE
    assert "finding_reproduction_base_sparse_disable_failed:boom" in base["detail"]
    assert receipt["classification"] != fr.REPRODUCED
    # The base spec never ran, and its workspace was disposed exactly once.
    assert "run:ws1" not in seen["events"]
    assert seen["events"].count("cleanup:ws1") == 1
    assert trees.cleaned == trees.created
