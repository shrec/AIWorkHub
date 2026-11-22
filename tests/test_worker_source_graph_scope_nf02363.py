"""Worker Source Graph read scope and live worktree overlay (NF-2026-02363).

Field 0.13.9: workers fell back to raw reads because Source Graph (a) never saw
a file the worker itself had just created (a first-launch worker had no overlay
over the canonical index), and (b) refused every existing repository file that
was not one of the <=12 declared targets.  These tests pin the replacement:

* a first-launch worker's changed/untracked files beneath ``allowed_writes``
  are composed over the canonical index, keyed by content hash, so an edit is
  visible on the very next query, and overflow is named, never dropped;
* with a rework packet the live delta is layered on top of the packet;
* READ authority is every repository path except paths outside the repo,
  private repository state (``.git``/``.aiworkhub``/dot-paths) and the card's
  deny_read globs -- each refusal names its reason; writes are untouched.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aiworkhub import repository_state
from aiworkhub import semantic_edit
from aiworkhub import source_graph
from aiworkhub import worker_ai_tools_mcp as w
from aiworkhub.worker_workspace import materialize_rework_overlay

_EXISTING = "src/existing.py"
_OTHER = "src/other_module.py"
_SECRET = "src/secret/keys.py"
_NEW = "src/new_live_module.py"
_CANONICAL = {
    _EXISTING: b"def existing_symbol_nf02363():\n    return 'canonical'\n",
    _OTHER: b"def other_symbol_nf02363():\n    return 'undeclared'\n",
    _SECRET: b"def secret_symbol_nf02363():\n    return 'denied'\n",
}
_NEW_BODY = (
    b"def fresh_live_symbol_nf02363():\n"
    b"    return 'live_marker_nf02363'\n"
)


@pytest.fixture(autouse=True)
def _isolated_caches():
    w._rework_overlay_view_cache_clear()
    w._source_graph_cache_clear()
    yield
    w._rework_overlay_view_cache_clear()
    w._source_graph_cache_clear()


def _repos(tmp_path: Path) -> tuple[Path, Path]:
    authority = tmp_path / "authority"
    workspace = tmp_path / "workspace"
    for root in (authority, workspace):
        root.mkdir()
        for relative, data in _CANONICAL.items():
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_bytes(data)
    repository_state.bootstrap_repository(authority)
    repository_state.bootstrap_repository(workspace)
    source_graph.build_index(authority, incremental=False)
    return authority, workspace


def _ctx(
    tmp_path: Path,
    *,
    allowed: tuple[str, ...] = (_EXISTING, _NEW),
    targets: tuple[str, ...] | None = None,
    contract: object = None,
    packet: dict | None = None,
    repos: tuple[Path, Path] | None = None,
) -> w.WorkerToolContext:
    authority, workspace = repos or _repos(tmp_path)
    contract_path = None
    if contract is not None:
        contract_path = tmp_path / "home" / "worker_contract.json"
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text(
            contract if isinstance(contract, str) else json.dumps(contract),
            encoding="utf-8",
        )
    packet_path = None
    if packet is not None:
        packet_path = tmp_path / "runtime" / "rework_overlay.json"
        packet_path.parent.mkdir(parents=True, exist_ok=True)
        packet_path.write_text(json.dumps(packet), encoding="utf-8")
    return w.WorkerToolContext(
        task_id="nf02363-task",
        runner="runner",
        topic="task_mcp",
        request_id="nf02363-request",
        repo=workspace,
        authority_repo=authority,
        source_graph_targets=tuple(targets if targets is not None else allowed),
        allowed_writes=allowed,
        session_topic="task_mcp",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
        contract_packet_path=contract_path,
        rework_overlay_packet=packet,
        rework_overlay_packet_path=packet_path,
    )


def _query(ctx: w.WorkerToolContext, mode: str, query: str, **kwargs) -> dict:
    reply = w.source_graph_query(ctx, mode=mode, query=query, budget=16, **kwargs)
    assert reply["ok"] is True, reply
    content = reply["content"]
    return content if isinstance(content, dict) else json.loads(content)


def _paths(content: dict) -> set[str]:
    return {
        str(row.get("file_path") or "")
        for row in content.get("matches") or []
        if isinstance(row, dict)
    }


# ---------------------------------------------------------------------------
# (a) A first-launch worker sees the files it just created.
# ---------------------------------------------------------------------------

def test_new_untracked_file_is_served_by_every_read_mode(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (ctx.repo / _NEW).write_bytes(_NEW_BODY)

    as_file = _query(ctx, "file", _NEW, target=_NEW)
    assert "fresh_live_symbol_nf02363" in json.dumps(as_file)
    assert as_file["overlay"]["authority_source"] == "worker_live_overlay"
    assert as_file["overlay"]["live_worktree_paths"] == [_NEW]

    body = _query(ctx, "body", "fresh_live_symbol_nf02363")
    assert _NEW in _paths(body)
    assert "live_marker_nf02363" in json.dumps(body)

    grep = _query(ctx, "bodygrep", "live_marker_nf02363")
    assert _NEW in _paths(grep)

    focus = _query(ctx, "focus", "fresh_live_symbol_nf02363")
    assert _NEW in _paths(focus)

    sliced = _query(ctx, "slice", "fresh_live_symbol_nf02363")
    assert _NEW in _paths(sliced)


def test_editing_the_new_file_again_returns_the_new_bytes(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (ctx.repo / _NEW).write_bytes(_NEW_BODY)
    assert _NEW in _paths(_query(ctx, "body", "fresh_live_symbol_nf02363"))

    (ctx.repo / _NEW).write_bytes(
        b"def renamed_live_symbol_nf02363():\n    return 'second_edit_nf02363'\n",
    )
    renamed = _query(ctx, "body", "renamed_live_symbol_nf02363")
    assert _NEW in _paths(renamed)
    assert "second_edit_nf02363" in json.dumps(renamed)
    stale = _query(ctx, "bodygrep", "live_marker_nf02363")
    assert _NEW not in _paths(stale)
    as_file = _query(ctx, "file", _NEW, target=_NEW)
    assert "renamed_live_symbol_nf02363" in json.dumps(as_file)
    assert "fresh_live_symbol_nf02363" not in json.dumps(as_file)


def test_a_modified_tracked_allowed_write_shadows_its_canonical_row(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    (ctx.repo / _EXISTING).write_bytes(
        b"def existing_symbol_nf02363():\n    return 'edited_in_worktree'\n",
    )
    body = _query(ctx, "body", "existing_symbol_nf02363")
    rendered = json.dumps(body)
    assert "edited_in_worktree" in rendered
    assert "'canonical'" not in rendered


def test_an_unchanged_worktree_composes_no_overlay(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    db = w._canonical_source_graph_binding(ctx).db_path
    assert w._prepare_rework_overlay_view(
        ctx, db, build_revision=source_graph.BUILD_REVISION,
    ) is None
    body = _query(ctx, "body", "existing_symbol_nf02363")
    assert "overlay" not in body


def test_overlay_limit_overflow_is_named_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx(tmp_path, allowed=("src/gen_*.py",))
    for index in range(3):
        (ctx.repo / f"src/gen_{index}.py").write_bytes(
            f"def generated_{index}_nf02363():\n    return {index}\n".encode(),
        )
    monkeypatch.setattr(w, "MAX_REWORK_OVERLAY_FILES", 1)

    focus = _query(ctx, "focus", "generated_0_nf02363")
    overflow = focus["overlay"]["live_overflow"]
    assert overflow["count"] == 2
    assert overflow["paths"] == ["src/gen_1.py", "src/gen_2.py"]
    assert overflow["max_files"] == 1
    assert focus["overlay"]["live_worktree_paths"] == ["src/gen_0.py"]


def test_rework_packet_still_gets_the_live_delta_layered_on_top(tmp_path: Path) -> None:
    authority, workspace = _repos(tmp_path)
    sealed = b"def packet_symbol_nf02363():\n    return 'from_packet'\n"
    (workspace / _EXISTING).write_bytes(sealed)
    packet = json.loads(materialize_rework_overlay(
        "nf02363-request", "nf02363-task", "predecessor-request", "nf02363-task",
        authority, [(_EXISTING, hashlib.sha256(sealed).hexdigest(), sealed)],
    ))
    ctx = _ctx(tmp_path, packet=packet, repos=(authority, workspace))
    (workspace / _NEW).write_bytes(_NEW_BODY)

    assert _EXISTING in _paths(_query(ctx, "body", "packet_symbol_nf02363"))
    live = _query(ctx, "body", "fresh_live_symbol_nf02363")
    assert _NEW in _paths(live)
    assert live["overlay"]["authority_source"] == "rework_overlay"
    assert live["overlay"]["predecessor_request_id"] == "predecessor-request"
    assert live["overlay"]["live_worktree_paths"] == [_NEW]


# ---------------------------------------------------------------------------
# (b) Every repository file is readable; only excluded paths are refused.
# ---------------------------------------------------------------------------

def test_undeclared_tracked_file_is_readable_in_every_mode(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, targets=(_EXISTING,))
    assert _OTHER not in ctx.source_graph_targets
    assert _OTHER not in ctx.allowed_writes

    as_file = _query(ctx, "file", _OTHER, target=_OTHER)
    assert "other_symbol_nf02363" in json.dumps(as_file)
    body = _query(ctx, "body", "other_symbol_nf02363", target=_OTHER)
    assert _OTHER in _paths(body)
    selector = _query(
        ctx, "body", "other_symbol_nf02363", target=f"{_OTHER}.other_symbol_nf02363",
    )
    assert _OTHER in _paths(selector)
    grep = _query(ctx, "bodygrep", "undeclared", target=_OTHER)
    assert _OTHER in _paths(grep)


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        (".git/config", "private_repository_path"),
        (".aiworkhub/project.json", "private_repository_path"),
        ("src/.env", "private_repository_path"),
        ("../outside.py", "path_outside_repo"),
        ("C:/Windows/win.ini", "path_outside_repo"),
    ],
)
def test_excluded_targets_are_refused_with_a_named_reason(
    tmp_path: Path, target: str, reason: str,
) -> None:
    ctx = _ctx(tmp_path)
    refused = w.source_graph_query(ctx, mode="file", query=target, target=target)
    assert refused["ok"] is False
    assert refused["reason"] == "target_not_allowed"
    assert refused["excluded_reason"] == reason
    assert refused["requested_target"] == target


def test_card_deny_read_is_refused_for_target_and_resolved_symbol(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, contract={"deny_read": ["src/secret/**"]})

    refused = w.source_graph_query(ctx, mode="file", query=_SECRET, target=_SECRET)
    assert refused["reason"] == "target_not_allowed"
    assert refused["excluded_reason"] == "card_deny_read:src/secret/**"

    violation, _payload = w._enforce_selector_allowlist(
        ctx, "source_graph",
        {"matches": [{"qualname": "secret_symbol_nf02363", "file_path": _SECRET}]},
    )
    assert violation is not None
    assert violation["reason"].startswith("symbol_out_of_scope:secret_symbol_nf02363@")
    assert violation["excluded_reason"] == "card_deny_read:src/secret/**"

    # Everything else stays readable under the same contract.
    assert _OTHER in _paths(_query(ctx, "body", "other_symbol_nf02363", target=_OTHER))


def test_forbidden_paths_deny_but_forbidden_prose_does_not(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, contract={
        "card": {"forbidden": ["src/secret", "do not touch the release notes"]},
    })
    refused = w.source_graph_query(ctx, mode="file", query=_SECRET, target=_SECRET)
    assert refused["excluded_reason"] == "card_deny_read:src/secret"
    assert _OTHER in _paths(_query(ctx, "file", _OTHER, target=_OTHER))


def test_unreadable_contract_fails_closed_to_the_declared_targets(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, targets=(_EXISTING,), contract="{not json")
    refused = w.source_graph_query(ctx, mode="file", query=_OTHER, target=_OTHER)
    assert refused["ok"] is False
    assert refused["excluded_reason"] == "card_read_scope_unreadable"
    assert _EXISTING in _paths(_query(ctx, "file", _EXISTING, target=_EXISTING))


def test_writes_stay_bounded_by_allowed_writes(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, allowed=(_EXISTING,))
    # Reading an undeclared file never widens what the worker may write.
    assert _OTHER in _paths(_query(ctx, "file", _OTHER, target=_OTHER))
    assert ctx.allowed_writes == (_EXISTING,)
    assert semantic_edit.path_is_allowed(_EXISTING, ctx.allowed_writes) is True
    assert semantic_edit.path_is_allowed(_OTHER, ctx.allowed_writes) is False


# ---------------------------------------------------------------------------
# Review fixes: absolute paths, Windows case aliases, unscoped deny_read rows,
# and the live-overlay file cap applied to changed files only.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/etc/passwd", "//srv/share/a.py", "C:/x.py"])
def test_absolute_and_unc_paths_are_outside_the_repo(tmp_path: Path, path: str) -> None:
    ctx = _ctx(tmp_path)
    assert w._source_graph_read_refusal(ctx, path) == "path_outside_repo"
    refused = w.source_graph_query(ctx, mode="file", query=path, target=path)
    assert refused["ok"] is False
    assert refused["excluded_reason"] == "path_outside_repo"


def test_windows_case_aliases_cannot_bypass_deny_read_or_private_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiworkhub import platform_io

    ctx = _ctx(tmp_path, contract={"deny_read": ["secret/**"]})
    monkeypatch.setattr(platform_io, "is_windows", lambda *_a, **_k: True)
    assert w._source_graph_read_refusal(ctx, "SECRET/a.py") == "card_deny_read:secret/**"
    assert w._source_graph_read_refusal(ctx, "secret/a.py") == "card_deny_read:secret/**"
    assert w._source_graph_read_refusal(ctx, ".GIT/config") == "private_repository_path"
    assert w._source_graph_read_refusal(ctx, _OTHER) is None


def test_unscoped_bodygrep_drops_rows_from_a_deny_read_file(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, contract={"deny_read": ["src/secret/**"]})
    grep = _query(ctx, "bodygrep", "denied")
    assert _SECRET not in _paths(grep)
    assert _SECRET not in json.dumps(grep)
    assert grep["read_gate_filtered"] >= 1


def test_unscoped_body_returns_no_source_from_a_deny_read_file(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, contract={"deny_read": ["src/secret/**"]})
    body = _query(ctx, "body", "secret_symbol_nf02363")
    rendered = json.dumps(body)
    assert _SECRET not in _paths(body)
    assert "return 'denied'" not in rendered
    assert _SECRET not in body.get("candidate_files", [])
    # The same unscoped query without the denial still finds the symbol.
    w._source_graph_cache_clear()
    (tmp_path / "open").mkdir()
    open_ctx = _ctx(tmp_path / "open")
    assert _SECRET in _paths(_query(open_ctx, "body", "secret_symbol_nf02363"))


def _bulk_ctx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unchanged: int,
) -> w.WorkerToolContext:
    monkeypatch.setattr(w, "MAX_REWORK_OVERLAY_FILES", 3)
    authority = tmp_path / "authority"
    workspace = tmp_path / "workspace"
    for root in (authority, workspace):
        (root / "src" / "bulk").mkdir(parents=True)
        for index in range(unchanged):
            (root / f"src/bulk/kept_{index:02d}.py").write_bytes(
                f"def kept_{index}_nf02363():\n    return {index}\n".encode(),
            )
    repository_state.bootstrap_repository(authority)
    repository_state.bootstrap_repository(workspace)
    source_graph.build_index(authority, incremental=False)
    (workspace / "src/bulk/zz_new.py").write_bytes(_NEW_BODY)
    return _ctx(tmp_path, allowed=("src/bulk/",), repos=(authority, workspace))


def test_live_overlay_cap_counts_changed_files_not_unchanged_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _bulk_ctx(tmp_path, monkeypatch, unchanged=3 + 5)
    db = w._canonical_source_graph_binding(ctx).db_path
    observations, _hashes, named, unserved = w._observe_live_worktree_files(
        ctx, db, frozenset(),
    )
    assert [obs[0] for obs in observations] == ["src/bulk/zz_new.py"]
    assert unserved == 0
    assert named == ()


def test_live_overlay_scan_limit_is_reported_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _bulk_ctx(tmp_path, monkeypatch, unchanged=4)
    monkeypatch.setattr(w, "_LIVE_OVERLAY_SCAN_LIMIT", 2)
    db = w._canonical_source_graph_binding(ctx).db_path
    _observations, _hashes, named, unserved = w._observe_live_worktree_files(
        ctx, db, frozenset(),
    )
    assert unserved == 1
    assert named == ("scan_limit:2:src/bulk",)


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("secret./a.py", "card_deny_read:secret/**"),
        ("secret /a.py", "card_deny_read:secret/**"),
        (".git./config", "private_repository_path"),
        ("SECRE~1/a.py", "path_short_name_alias"),
    ],
)
def test_windows_trailing_dot_and_short_name_aliases_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, reason: str,
) -> None:
    from aiworkhub import platform_io

    ctx = _ctx(tmp_path, contract={"deny_read": ["secret/**"]})
    monkeypatch.setattr(platform_io, "is_windows", lambda *_a, **_k: True)
    assert w._source_graph_read_refusal(ctx, path) == reason


def test_a_row_is_dropped_when_any_of_its_file_keys_is_excluded(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, contract={"deny_read": ["src/secret/**"]})
    payload = {"edges": [
        {"caller_file": _OTHER, "callee_file": _SECRET},
        {"caller_file": _OTHER, "callee_file": _OTHER},
    ]}
    filtered, dropped = w._apply_source_graph_read_gate(ctx, payload)
    assert dropped == 1
    assert filtered["edges"] == [{"caller_file": _OTHER, "callee_file": _OTHER}]
