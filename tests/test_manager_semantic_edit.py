"""The manager gets the same verified editor a worker gets.

``semantic_edit_prepare``/``semantic_edit_apply`` exist only on the worker MCP
surface, so the manager -- whose charter is "small precise corrections" -- was
the one role without the instrument that makes a small correction verifiable,
and fell back to whole-string rewrites with no hash binding at all.

The applier layer is now one definition (``semantic_edit_applier``) with two
callers: the worker session and a manager CLI. These tests pin what both get --
the range and only the range changes, a file that moved underneath is refused
rather than overwritten, and the file's mode survives the atomic swap.

Run: python3 -m pytest -q tests/test_manager_semantic_edit.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from aiworkhub import semantic_edit, semantic_edit_applier  # noqa: E402

CLI = _ROOT / "scripts" / "manager_semantic_edit.py"


def _repo(tmp_path: Path, text: str = "one\ntwo\nthree\nfour\nfive\n") -> Path:
    (tmp_path / "x.py").write_text(text, encoding="utf-8")
    return tmp_path


def _prepare(root: Path, start: int, end: int):
    return semantic_edit.prepare_line_target(
        root, path="x.py", start_line=start, end_line=end, allowed_writes=["x.py"]
    )


def test_only_the_named_range_changes(tmp_path):
    root = _repo(tmp_path)
    target = _prepare(root, 2, 3)
    semantic_edit_applier.replace_prepared_range(
        root, target, "TWO\nTHREE\n", allowed_writes=["x.py"]
    )
    assert (root / "x.py").read_text(encoding="utf-8") == "one\nTWO\nTHREE\nfour\nfive\n"


def test_a_file_that_moved_underneath_the_edit_is_refused(tmp_path):
    """Prepared, then the file changes: the write must not land."""
    root = _repo(tmp_path)
    target = _prepare(root, 2, 3)
    (root / "x.py").write_text("one\ntwo\nthree\nfour\nfive\nsix\n", encoding="utf-8")

    with pytest.raises(semantic_edit.SemanticEditError, match="semantic_edit_stale"):
        semantic_edit_applier.replace_prepared_range(
            root, target, "TWO\n", allowed_writes=["x.py"]
        )
    assert (root / "x.py").read_text(encoding="utf-8").endswith("six\n"), (
        "a refused edit must leave the file exactly as it found it"
    )


def test_an_executable_file_keeps_its_mode(tmp_path):
    """mkstemp makes 0600 and os.replace carries it; the mode must survive."""
    root = _repo(tmp_path, "#!/bin/sh\necho one\necho two\n")
    os.chmod(root / "x.py", 0o755)
    target = _prepare(root, 2, 2)
    semantic_edit_applier.replace_prepared_range(
        root, target, "echo ONE\n", allowed_writes=["x.py"]
    )
    assert os.stat(root / "x.py").st_mode & 0o777 == 0o755


def test_a_path_outside_allowed_writes_is_refused(tmp_path):
    root = _repo(tmp_path)
    with pytest.raises(semantic_edit.SemanticEditError):
        semantic_edit.prepare_line_target(
            root, path="x.py", start_line=1, end_line=1, allowed_writes=["other.py"]
        )


def test_the_cli_emits_a_receipt_and_never_reemits_the_file(tmp_path):
    root = _repo(tmp_path, "\n".join(f"line{n}" for n in range(1, 200)) + "\n")
    result = subprocess.run(
        [sys.executable, str(CLI), "--repo", str(root), "--path", "x.py",
         "--start", "10", "--end", "11"],
        input="LINE10\nLINE11\n", capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["ok"] is True
    assert receipt["preimage_verified"] is True
    assert receipt["model_reemitted_old_bytes"] == 0
    assert receipt["whole_file_output_required"] is False
    # The point of the instrument: bytes emitted are the range, not the file.
    assert receipt["replacement_bytes"] < receipt["file_bytes"] / 10

    lines = (root / "x.py").read_text(encoding="utf-8").splitlines()
    assert lines[9] == "LINE10" and lines[10] == "LINE11"
    assert lines[8] == "line9" and lines[11] == "line12"


def test_the_cli_fails_closed_with_a_reason(tmp_path):
    root = _repo(tmp_path)
    result = subprocess.run(
        [sys.executable, str(CLI), "--repo", str(root), "--path", "x.py",
         "--start", "99", "--end", "99"],
        input="nope\n", capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert json.loads(result.stderr)["ok"] is False
    assert (root / "x.py").read_text(encoding="utf-8") == "one\ntwo\nthree\nfour\nfive\n"

# ---------------------------------------------------------------------------
# The MANAGER MCP surface itself: reachable, mandatory, and recorded.
#
# The CLI above proved the applier is shared.  It could not make the manager's
# edits MEASURABLE, because a CLI run leaves no ``semantic_edit_apply_receipt``
# in any authenticated ledger, and the coverage record that counts changed
# paths against apply receipts was therefore blind to every manager edit.
# These tests pin the MCP surface: the same guarantees, an actor bound to the
# verified route, a scope answer written for the canonical tree, and a fallback
# record that is evidence and never a gate.
# ---------------------------------------------------------------------------

from aiworkhub import core, manager_ai_tools  # noqa: E402
from aiworkhub import process_launcher  # noqa: E402
from aiworkhub import worker_ai_tools_mcp as worker_tools  # noqa: E402

_SESSION_ID = "019f5097-6dbe-7172-870a-945afc5f3bfa"


def _route(root: Path, *, provider: str = "claude", session_id: str = _SESSION_ID) -> dict:
    return {
        "ok": True,
        "role": "manager",
        "provider": provider,
        "repo": str(root),
        "manager_route": {
            "provider": provider,
            "session_id": session_id,
            "thread_id": session_id,
        },
    }


@pytest.fixture(autouse=True)
def _clean_manager_edit_sessions():
    manager_ai_tools._MANAGER_EDIT_SESSIONS.clear()
    yield
    manager_ai_tools._MANAGER_EDIT_SESSIONS.clear()


def _seat(monkeypatch, root: Path, *, provider: str = "claude", writes: bool = True):
    monkeypatch.setattr(core, "manager_bootstrap", lambda: _route(root, provider=provider))
    monkeypatch.setattr(core, "writes_allowed", lambda: writes)


def _ledger(root: Path) -> tuple[Path, Path]:
    runtime = root / ".aiworkhub" / "runtime" / "manager_semantic_edit"
    return runtime / "audit_ledger.jsonl", runtime / "audit_hmac.key"


def _verify(root: Path, *, provider: str = "claude") -> dict:
    ledger, key = _ledger(root)
    return worker_tools.verify_audit_ledger(
        ledger, key,
        task_id=f"manager:{_SESSION_ID}",
        runner=f"{provider}_manager",
        topic="semantic_edit",
        request_id=_SESSION_ID,
    )


def test_manager_prepare_and_apply_round_trip(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _seat(monkeypatch, root)

    prepared = manager_ai_tools.semantic_edit_prepare(
        file_path="x.py", start_line=2, end_line=3,
    )
    assert prepared["ok"] is True, prepared
    assert prepared["fragment"] == "two\nthree\n"
    assert prepared["token_savings_claimed"] is False

    applied = manager_ai_tools.semantic_edit_apply(
        target_id=prepared["target_id"], new="TWO\nTHREE\n",
        idempotency_key="manager-roundtrip-1",
    )
    assert applied["ok"] is True, applied
    assert (root / "x.py").read_text(encoding="utf-8") == "one\nTWO\nTHREE\nfour\nfive\n"

    # The same deterministic byte accounting a worker receipt carries.
    for field in (
        "file_bytes", "range_count", "old_region_bytes", "replacement_bytes",
        "model_reemitted_old_bytes",
    ):
        assert field in applied, field
    assert applied["range_count"] == 1
    assert applied["model_reemitted_old_bytes"] == 0
    assert applied["token_savings_claimed"] is False
    assert applied["preimage_verified"] is True
    assert applied["schema_id"] == "aiworkhub.semantic_edit_apply_receipt.v1"


def test_the_manager_apply_reaches_the_authenticated_ledger(tmp_path, monkeypatch):
    """The whole point: a manager edit is now joinable to a changed path."""
    root = _repo(tmp_path)
    _seat(monkeypatch, root)

    prepared = manager_ai_tools.semantic_edit_prepare(
        file_path="x.py", start_line=2, end_line=2,
    )
    manager_ai_tools.semantic_edit_apply(
        target_id=prepared["target_id"], new="TWO\n",
        idempotency_key="manager-ledger-1",
    )

    verification = _verify(root)
    receipts = verification["semantic_edit_apply_receipts"]
    assert len(receipts) == 1, verification
    assert receipts[0]["path_sha256"] == process_launcher.semantic_edit_path_identifier(
        "x.py"
    )
    assert receipts[0]["token_savings_claimed"] is False
    assert verification["entries_tampered"] == 0


def test_apply_is_refused_when_the_file_moved_between_prepare_and_apply(
    tmp_path, monkeypatch,
):
    root = _repo(tmp_path)
    _seat(monkeypatch, root)

    prepared = manager_ai_tools.semantic_edit_prepare(
        file_path="x.py", start_line=2, end_line=3,
    )
    (root / "x.py").write_text("one\ntwo\nthree\nfour\nfive\nsix\n", encoding="utf-8")

    applied = manager_ai_tools.semantic_edit_apply(
        target_id=prepared["target_id"], new="TWO\n",
        idempotency_key="manager-stale-1",
    )
    assert applied["ok"] is False
    assert "semantic_edit_stale" in applied["reason"]
    assert (root / "x.py").read_text(encoding="utf-8").endswith("six\n")


def test_a_path_outside_the_repository_is_refused(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    (tmp_path.parent / "outside.py").write_text("secret\n", encoding="utf-8")
    _seat(monkeypatch, root)

    for candidate in ("../outside.py", "/etc/passwd", ".git/config"):
        refused = manager_ai_tools.semantic_edit_prepare(
            file_path=candidate, start_line=1, end_line=1,
        )
        assert refused["ok"] is False, candidate
        assert "semantic_edit_path" in refused["reason"], candidate


def test_a_symlink_is_refused(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    try:
        (root / "link.py").symlink_to(root / "x.py")
    except (OSError, NotImplementedError):  # pragma: no cover - platform
        pytest.skip("symlinks unavailable on this host")
    _seat(monkeypatch, root)

    refused = manager_ai_tools.semantic_edit_prepare(
        file_path="link.py", start_line=1, end_line=1,
    )
    assert refused["ok"] is False
    assert "semantic_edit_symlink_forbidden" in refused["reason"]


def test_aiworkhub_state_is_outside_the_manager_scope(tmp_path, monkeypatch):
    """The seat writes source, never the evidence its own review reads."""
    root = _repo(tmp_path)
    rules = root / ".aiworkhub" / "config" / "development_rules.json"
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text("{\n  \"rules\": []\n}\n", encoding="utf-8")
    _seat(monkeypatch, root)

    refused = manager_ai_tools.semantic_edit_prepare(
        file_path=".aiworkhub/config/development_rules.json",
        start_line=1, end_line=1,
    )
    assert refused["ok"] is False
    assert refused["reason"].startswith("manager_semantic_edit_path_is_aiworkhub_state")
    assert refused["scope"] == manager_ai_tools.MANAGER_SEMANTIC_EDIT_SCOPE
    assert rules.read_text(encoding="utf-8") == "{\n  \"rules\": []\n}\n"


def test_the_actor_is_derived_from_the_route_and_unforgeable_from_arguments(
    tmp_path, monkeypatch,
):
    root = _repo(tmp_path)
    _seat(monkeypatch, root, provider="claude")

    prepared = manager_ai_tools.semantic_edit_prepare(
        file_path="x.py", start_line=1, end_line=1,
    )
    assert prepared["actor"] == {
        "role": "manager",
        "actor_id": _SESSION_ID,
        "provider": "claude",
        "session_id": _SESSION_ID,
        "repo": str(root),
    }

    # No argument can name an actor at all.
    with pytest.raises(TypeError):
        manager_ai_tools.semantic_edit_prepare(  # type: ignore[call-arg]
            file_path="x.py", start_line=1, end_line=1,
            actor={"role": "manager", "actor_id": "someone-else"},
        )

    # Change the verified route and the actor moves with it -- nothing else can.
    _seat(monkeypatch, root, provider="codex")
    again = manager_ai_tools.semantic_edit_prepare(
        file_path="x.py", start_line=1, end_line=1,
    )
    assert again["actor"]["provider"] == "codex"


def test_an_unverified_route_gets_no_editor(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(core, "manager_bootstrap", lambda: {"ok": True, "role": "worker"})
    refused = manager_ai_tools.semantic_edit_prepare(
        file_path="x.py", start_line=1, end_line=1,
    )
    assert refused["ok"] is False
    assert refused["error"] == "verified_manager_identity_required"


def test_the_write_gate_still_governs_the_apply(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _seat(monkeypatch, root, writes=False)
    prepared = manager_ai_tools.semantic_edit_prepare(
        file_path="x.py", start_line=2, end_line=2,
    )
    assert prepared["ok"] is True
    refused = manager_ai_tools.semantic_edit_apply(
        target_id=prepared["target_id"], new="TWO\n", idempotency_key="gated",
    )
    assert refused["ok"] is False and refused["error"] == "write_gate_closed"
    assert (root / "x.py").read_text(encoding="utf-8") == "one\ntwo\nthree\nfour\nfive\n"


# --- the recorded fallback ---------------------------------------------------


def test_the_fallback_record_derives_a_new_file(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _seat(monkeypatch, root)

    record = manager_ai_tools.semantic_edit_fallback_record(
        path="brand_new.py", reason="created the file in one write",
    )
    assert record["ok"] is True and record["recorded"] is True
    assert record["exception"] == "new_file"
    assert record["derived"][0]["source"] == "runtime_derivation"
    assert record["derived"][0]["basis"] == "path_absent_from_canonical_tree"
    assert record["declared"] == []


def test_the_fallback_record_derives_a_span_covering_most_of_a_file(
    tmp_path, monkeypatch,
):
    root = _repo(tmp_path)
    _seat(monkeypatch, root)

    record = manager_ai_tools.semantic_edit_fallback_record(
        path="x.py", reason="rewrote nearly the whole module",
        start_line=1, end_line=4,
    )
    assert record["ok"] is True and record["recorded"] is True
    assert record["exception"] == "spans_most_of_file"
    assert record["derived"][0]["basis"].startswith(
        "span_covers_a_strict_majority_of_file_lines:4/5"
    )

    # A minority span derives nothing: the caller's word is all there is.
    minority = manager_ai_tools.semantic_edit_fallback_record(
        path="x.py", reason="one line", start_line=1, end_line=2,
    )
    assert minority["derived"] == []


def test_a_declared_span_claim_is_recorded_and_marked_uncorroborated(
    tmp_path, monkeypatch,
):
    """The judgemental case: the caller's word, recorded, never dressed up."""
    root = _repo(tmp_path)
    _seat(monkeypatch, root)

    record = manager_ai_tools.semantic_edit_fallback_record(
        path="x.py", exception="spans_most_of_file",
        reason="the whole function body changed",
    )
    assert record["ok"] is True and record["recorded"] is True
    assert record["declared_exception_valid"] is True
    row = record["declared"][0]
    assert row["source"] == "manager_declaration"
    assert row["corroborated"] is False
    assert row["corroboration"] == "no_span_supplied_for_a_span_claim"

    corroborated = manager_ai_tools.semantic_edit_fallback_record(
        path="x.py", exception="spans_most_of_file",
        reason="the whole function body changed", start_line=1, end_line=4,
    )
    assert corroborated["declared"][0]["corroborated"] is True
    assert corroborated["declared"][0]["corroboration"] == "span_lines_vs_file_lines:4/5"


def test_a_declared_adapter_claim_is_refuted_when_the_surface_works(
    tmp_path, monkeypatch,
):
    root = _repo(tmp_path)
    _seat(monkeypatch, root)
    record = manager_ai_tools.semantic_edit_fallback_record(
        path="x.py", exception="adapter_without_tools", reason="no editor here",
    )
    assert record["ok"] is True
    assert record["declared"][0]["corroborated"] is False
    assert record["declared"][0]["corroboration"] == (
        "manager_semantic_edit_surface_is_available"
    )


def test_the_fallback_record_refuses_nothing(tmp_path, monkeypatch):
    """A record that can be refused is a record that hides edits."""
    root = _repo(tmp_path)
    _seat(monkeypatch, root)

    unknown = manager_ai_tools.semantic_edit_fallback_record(
        path="x.py", exception="because_i_felt_like_it", reason="n/a",
    )
    assert unknown["ok"] is True
    assert unknown["declared_exception_valid"] is False
    assert unknown["declared"][0]["corroboration"] == "unknown_exception_code"
    assert unknown["allowed_exceptions"] == list(
        process_launcher.SEMANTIC_EDIT_POLICY_EXCEPTIONS
    )

    unusable = manager_ai_tools.semantic_edit_fallback_record(
        path="../escape.py", reason="n/a",
    )
    assert unusable["ok"] is True and unusable["recorded"] is False
    assert unusable["unrecorded_reason"].startswith("semantic_edit_path_escape")


def test_an_unavailable_surface_records_itself_as_the_reason(tmp_path, monkeypatch):
    """Absence of a receipt reads as unmeasured with a named reason."""
    monkeypatch.setattr(core, "manager_bootstrap", lambda: {"ok": True, "role": "worker"})
    record = manager_ai_tools.semantic_edit_fallback_record(
        path="x.py", reason="no manager route in this process",
    )
    assert record["ok"] is True and record["recorded"] is False
    assert record["exception"] == "adapter_without_tools"
    assert record["unrecorded_reason"] == "verified_manager_identity_required"


def test_the_fallback_vocabulary_is_the_finalizers_own_list():
    assert manager_ai_tools.semantic_edit_policy_exceptions() == tuple(
        process_launcher.SEMANTIC_EDIT_POLICY_EXCEPTIONS
    )


def test_no_gate_consults_the_fallback_record():
    """It is EVIDENCE. Nothing that admits or refuses work may read it."""
    gates = [
        "process_launcher_accept_review.py",
        "process_launcher_acceptance.py",
        "process_launcher_validation.py",
        "quality_gates.py",
        "quality_review.py",
        "quality_review_ingest.py",
        "review_orchestrator.py",
        "task_fsm.py",
        "terminal_authority.py",
    ]
    package = _ROOT / "src" / "aiworkhub"
    names = ("semantic_edit_fallback_record", "manager_semantic_edit_fallback_record")
    for name in gates:
        module = package / name
        if not module.is_file():
            continue
        text = module.read_text(encoding="utf-8")
        for symbol in names:
            assert symbol not in text, f"{name} must not consult the fallback record"


def test_the_record_says_it_is_not_a_gate(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _seat(monkeypatch, root)
    record = manager_ai_tools.semantic_edit_fallback_record(
        path="x.py", reason="declared",
    )
    assert record["is_gate"] is False


def test_the_fallback_record_is_readable_from_the_authenticated_ledger(
    tmp_path, monkeypatch,
):
    """The record joins to the coverage evidence by the same path identifier."""
    root = _repo(tmp_path)
    _seat(monkeypatch, root)

    manager_ai_tools.semantic_edit_fallback_record(
        path="x.py", exception="spans_most_of_file",
        reason="the whole module changed", start_line=1, end_line=5,
    )

    verification = _verify(root)
    declarations = verification["semantic_edit_exception_declarations"]
    assert len(declarations) == 1, verification
    assert declarations[0]["path_sha256"] == (
        process_launcher.semantic_edit_path_identifier("x.py")
    )
    assert declarations[0]["exception"] == "spans_most_of_file"
    assert verification["entries_tampered"] == 0
