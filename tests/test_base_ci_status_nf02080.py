"""Tests for base commit CI status surfacing (NF-2026-02080). gh is always mocked."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiworkhub import ci_status  # noqa: E402
from aiworkhub import completion_inbox  # noqa: E402
from aiworkhub import process_launcher  # noqa: E402
from aiworkhub import process_launcher_accept_review as par  # noqa: E402

SHA = "a" * 40
SHORT = "abc1234"


def setup_function(_fn):
    ci_status.clear_cache()


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _row(workflow="CI", status="completed", conclusion="success",
         created="2026-11-05T10:00:00Z", url="https://example/1"):
    return {
        "workflowName": workflow,
        "name": workflow,
        "status": status,
        "conclusion": conclusion,
        "url": url,
        "createdAt": created,
        "headSha": SHA,
    }


def _stub_gh(monkeypatch, rows, calls=None):
    def fake(repo_root, sha):
        if calls is not None:
            calls.append(sha)
        return 0, json.dumps(rows), ""

    monkeypatch.setattr(ci_status, "_run_gh", fake)
    monkeypatch.setattr(ci_status, "_resolve_sha", lambda repo_root, ref: SHA)


def _gh_missing(monkeypatch):
    monkeypatch.setattr(ci_status, "_resolve_sha", lambda repo_root, ref: SHA)
    monkeypatch.setattr(ci_status, "_run_gh", lambda repo_root, sha: (-1, "", "gh_not_found"))


# --------------------------------------------------------------- unavailable


def test_gh_missing_is_unavailable_with_reason(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = ci_status.fetch_base_ci_status(None, SHORT)
    assert result["available"] is False
    assert result["state"] == "unavailable"
    assert result["reason"] == "gh_not_found"
    assert result["runs"] == []


def test_gh_timeout_is_unavailable(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=10)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ci_status.fetch_base_ci_status(None, SHORT)["reason"] == "gh_timeout"


def test_gh_unauthenticated_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompleted(1, stderr="not logged in, auth required"),
    )
    assert ci_status.fetch_base_ci_status(None, SHORT)["reason"] == "gh_unauthenticated"


def test_no_github_remote_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompleted(1, stderr="could not determine repository"),
    )
    assert ci_status.fetch_base_ci_status(None, SHORT)["reason"] == "no_github_remote"


def test_bad_json_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _FakeCompleted(0, stdout="not json")
    )
    assert ci_status.fetch_base_ci_status(None, SHORT)["reason"] == "gh_bad_json"


def test_non_utf8_gh_output_does_not_raise(monkeypatch):
    seen = {}

    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return _FakeCompleted(0, stdout="�� not json �")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = ci_status.fetch_base_ci_status(Path("."), SHORT)
    assert result["state"] == "unavailable"
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace"
    assert seen["cwd"] == "."


def test_unexpected_exception_becomes_unavailable(monkeypatch):
    def boom(repo_root, sha):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ci_status, "_resolve_sha", lambda r, ref: SHA)
    monkeypatch.setattr(ci_status, "_run_gh", boom)
    result = ci_status.fetch_base_ci_status(None)
    assert result["state"] == "unavailable"
    assert result["reason"] == "RuntimeError"


def test_empty_ref_and_unresolvable_ref_are_unavailable(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(1))
    assert ci_status.fetch_base_ci_status(None, "-bad")["reason"] == "no_commit_sha"
    assert ci_status.fetch_base_ci_status(None, "HEAD")["reason"] == "no_commit_sha"


# ------------------------------------------------------------------ happy path


def test_successful_fetch_returns_runs(monkeypatch):
    _stub_gh(monkeypatch, [_row()])
    result = ci_status.fetch_base_ci_status(None)
    assert result["available"] is True and result["state"] == "ok"
    assert result["sha"] == SHA
    assert result["runs"][0]["workflow"] == "CI"
    assert result["runs"][0]["conclusion"] == "success"


def test_dedup_keeps_newest_run_per_workflow(monkeypatch):
    rows = [
        _row("CI", "completed", "failure", "2026-11-05T08:00:00Z", "https://example/old"),
        _row("CI", "completed", "success", "2026-11-05T09:00:00Z", "https://example/new"),
        _row("Lint", "completed", "success", "2026-11-05T07:00:00Z"),
    ]
    _stub_gh(monkeypatch, rows)
    runs = ci_status.fetch_base_ci_status(None)["runs"]
    by_workflow = {run["workflow"]: run for run in runs}
    assert set(by_workflow) == {"CI", "Lint"}
    assert by_workflow["CI"]["url"] == "https://example/new"
    assert by_workflow["CI"]["conclusion"] == "success"


def test_age_seconds_on_fixed_utc_timestamp(monkeypatch):
    # 2026-11-05T12:00:00Z == 1793880000 regardless of the local zone / DST.
    monkeypatch.setattr(time, "time", lambda: 1793880000 + 3725)
    assert ci_status._age_seconds("2026-11-05T12:00:00Z") == pytest.approx(3725)
    # A timestamp in the future clamps to zero; junk is None.
    monkeypatch.setattr(time, "time", lambda: 1793880000 - 10)
    assert ci_status._age_seconds("2026-11-05T12:00:00Z") == 0.0
    assert ci_status._age_seconds("") is None
    assert ci_status._age_seconds("yesterday") is None


# ----------------------------------------------------------------------- cache


def test_completed_results_are_cached_and_force_bypasses(monkeypatch):
    calls: list[str] = []
    _stub_gh(monkeypatch, [_row()], calls)
    ci_status.fetch_base_ci_status(None)
    ci_status.fetch_base_ci_status(None)
    assert len(calls) == 1
    ci_status.fetch_base_ci_status(None, force=True)
    assert len(calls) == 2


@pytest.mark.parametrize("status", ["in_progress", "queued", "waiting"])
def test_pending_results_are_refetched(monkeypatch, status):
    calls: list[str] = []
    _stub_gh(monkeypatch, [_row(status=status, conclusion="")], calls)
    ci_status.fetch_base_ci_status(None)
    ci_status.fetch_base_ci_status(None)
    assert len(calls) == 2


def test_unavailable_and_empty_results_are_not_cached(monkeypatch):
    calls: list[str] = []
    _stub_gh(monkeypatch, [], calls)
    ci_status.fetch_base_ci_status(None)
    ci_status.fetch_base_ci_status(None)
    assert len(calls) == 2
    _gh_missing(monkeypatch)
    assert ci_status.fetch_base_ci_status(None)["reason"] == "gh_not_found"
    _stub_gh(monkeypatch, [_row()], calls)
    assert ci_status.fetch_base_ci_status(None)["available"] is True


def test_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(ci_status, "_CACHE_MAX_ENTRIES", 3)
    monkeypatch.setattr(ci_status, "_run_gh", lambda r, s: (0, json.dumps([_row()]), ""))
    for index in range(6):
        ci_status.fetch_base_ci_status(None, f"{index:07x}")
    assert len(ci_status._cache) == 3


def test_result_mutation_does_not_poison_cache(monkeypatch):
    _stub_gh(monkeypatch, [_row()])
    first = ci_status.fetch_base_ci_status(None)
    first["runs"][0]["conclusion"] = "failure"
    assert ci_status.fetch_base_ci_status(None)["runs"][0]["conclusion"] == "success"


# -------------------------------------------------------------------- warnings


def _ci(*runs):
    return {"available": True, "reason": "", "runs": list(runs)}


def _run(status="completed", conclusion="", age=60, workflow="CI"):
    return {"workflow": workflow, "status": status, "conclusion": conclusion,
            "url": "https://example/1", "age_seconds": age}


def test_warnings_empty_when_unavailable():
    assert ci_status.base_ci_warnings({"available": False, "runs": []}) == []
    assert ci_status.base_ci_warnings(None) == []


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "timed_out", "startup_failure", "action_required", "cancelled"],
)
def test_each_red_conclusion_warns(conclusion):
    warnings = ci_status.base_ci_warnings(_ci(_run(conclusion=conclusion)))
    assert [w["kind"] for w in warnings] == ["base_ci_failure"]
    assert warnings[0]["conclusion"] == conclusion


def test_green_and_skipped_conclusions_do_not_warn():
    assert ci_status.base_ci_warnings(
        _ci(_run(conclusion="success"), _run(conclusion="skipped"), _run(conclusion="neutral"))
    ) == []


def test_stuck_in_progress_warns():
    warnings = ci_status.base_ci_warnings(_ci(_run("in_progress", age=3 * 3600)))
    assert [w["kind"] for w in warnings] == ["base_ci_stuck"]


@pytest.mark.parametrize("status", ["queued", "waiting", "pending", "requested"])
def test_old_queued_run_is_stuck(status):
    warnings = ci_status.base_ci_warnings(_ci(_run(status, age=5 * 3600)))
    assert [w["kind"] for w in warnings] == ["base_ci_stuck"]
    assert warnings[0]["status"] == status


def test_young_pending_run_does_not_warn():
    assert ci_status.base_ci_warnings(_ci(_run("in_progress", age=300))) == []
    assert ci_status.base_ci_warnings(_ci(_run("queued", age=None))) == []


# ---------------------------------------------------------------------- wiring


def _review_ready_card():
    return {
        "task_id": "T-1",
        "runner": "claude_coding",
        "topic": "coding",
        "status": "review",
        "terminal_review": {"substatus": "review_ready", "evidence": {}},
    }


def test_review_packet_carries_base_ci_from_stubbed_gh(monkeypatch):
    _stub_gh(monkeypatch, [_row(conclusion="failure")])
    monkeypatch.setattr(completion_inbox.core, "repo_root", lambda: Path("."))
    packet = completion_inbox.review_packet("R-1", card=_review_ready_card(), task_id="T-1")
    base_ci = packet["base_ci"]
    assert base_ci["state"] == "ok"
    assert base_ci["runs"][0]["conclusion"] == "failure"
    assert [w["kind"] for w in base_ci["warnings"]] == ["base_ci_failure"]
    assert packet["mutation"]["agent_or_process_launched"] is False


def test_review_packet_reports_unavailable_when_gh_missing(monkeypatch):
    _gh_missing(monkeypatch)
    monkeypatch.setattr(completion_inbox.core, "repo_root", lambda: Path("."))
    packet = completion_inbox.review_packet("R-1", card=_review_ready_card(), task_id="T-1")
    assert packet["ok"] is True
    assert packet["base_ci"]["state"] == "unavailable"
    assert packet["base_ci"]["reason"] == "gh_not_found"


def test_review_packet_survives_repo_root_failure(monkeypatch):
    def boom():
        raise RuntimeError("no repo")

    monkeypatch.setattr(completion_inbox.core, "repo_root", boom)
    packet = completion_inbox.review_packet("R-1", card=_review_ready_card(), task_id="T-1")
    assert packet["ok"] is True
    assert packet["base_ci"]["state"] == "unavailable"


def test_review_packet_prefers_base_ci_already_on_the_preview(monkeypatch):
    def not_called(*a, **k):
        raise AssertionError("must reuse the preview's base_ci")

    monkeypatch.setattr(ci_status, "base_ci_section", not_called)
    preview = {"evaluated": True, "blockers": [],
               "base_ci": {"state": "ok", "runs": [], "warnings": []}}
    packet = completion_inbox.review_packet(
        "R-1", card=_review_ready_card(), task_id="T-1", accept_preview=preview
    )
    assert packet["base_ci"]["state"] == "ok"


class _PreviewManager:
    def __init__(self, repo):
        self.repo = repo

    def _show_task(self, task_id):
        return {}

    def _context_write_intent_snapshot(self, request_id):
        return {"ok": False}


@pytest.fixture
def stubbed_preview(monkeypatch):
    card = _review_ready_card()
    monkeypatch.setattr(process_launcher, "_parse_card", lambda raw, task_id: card)
    monkeypatch.setattr(process_launcher, "_canonical_task_status", lambda c: "review")

    class _NoWorkspace:
        @staticmethod
        def from_metadata(_meta):
            raise ValueError("no workspace")

    monkeypatch.setattr(process_launcher, "WorkerWorkspace", _NoWorkspace)
    profile = {
        "requested_tier": "low", "effective_tier": "low", "signals": [],
        "required_reviewer_lenses": [], "combined_tree_required": False,
        "explicit_human_approval_required": False,
    }
    qe = par.quality_evidence
    monkeypatch.setattr(qe, "derive_risk_signals", lambda c, changed: [])
    monkeypatch.setattr(qe, "resolve_risk_profile", lambda req, signals=None: dict(profile))
    monkeypatch.setattr(qe, "repository_requires_explicit_human_approval", lambda repo: False)
    monkeypatch.setattr(par, "accept_in_flight", lambda s, t, r: None)
    monkeypatch.setattr(par, "effective_requested_risk_tier", lambda c, r: "low")
    monkeypatch.setattr(par, "server_derived_risk_tier", lambda c: "low")
    monkeypatch.setattr(par, "reviewer_evidence", lambda *a, **k: [])
    monkeypatch.setattr(par, "bound_finding_dispositions", lambda *a, **k: [])
    monkeypatch.setattr(par, "_target_candidate_sha256", lambda c: "")
    monkeypatch.setattr(par, "_sealed_receipt_sha256", lambda rows: {})
    monkeypatch.setattr(par, "fold_accept_blockers", lambda **k: {"blockers": []})
    monkeypatch.setattr(par, "accept_gate_blockers", lambda *a, **k: [])


def test_accept_preview_carries_base_ci_and_red_base_is_not_a_blocker(
    monkeypatch, tmp_path, stubbed_preview
):
    _stub_gh(monkeypatch, [_row(conclusion="failure")])
    preview = par.accept_preview(_PreviewManager(tmp_path), "R-1", "T-1")
    assert preview["evaluated"] is True
    assert preview["base_ci"]["state"] == "ok"
    assert [w["kind"] for w in preview["base_ci"]["warnings"]] == ["base_ci_failure"]
    assert preview["blocked"] is False
    assert preview["blockers"] == []


def test_accept_preview_base_ci_unavailable_when_gh_missing(
    monkeypatch, tmp_path, stubbed_preview
):
    _gh_missing(monkeypatch)
    preview = par.accept_preview(_PreviewManager(tmp_path), "R-1", "T-1")
    assert preview["blocked"] is False
    assert preview["base_ci"]["state"] == "unavailable"
    assert preview["base_ci"]["reason"] == "gh_not_found"


def test_base_ci_advisory_never_raises(monkeypatch):
    def boom(repo):
        raise RuntimeError("x")

    monkeypatch.setattr(ci_status, "base_ci_section", boom)
    section = par._base_ci_advisory(Path("."))
    assert section["state"] == "unavailable" and section["warnings"] == []
