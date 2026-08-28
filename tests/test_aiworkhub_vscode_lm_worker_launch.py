"""Three-lens tests: one packet preparation reused by every reviewer lens."""

from __future__ import annotations

import hashlib

import pytest

from aiworkhub import process_launcher as pl
from aiworkhub import quality_reviewer


class _Workspace:
    def __init__(self, path, repo, request_id):
        self.path = path
        self.repo = repo
        self.request_id = request_id
        self.home = path

    def as_metadata(self):
        return {"path": str(self.path)}


class _Manager(pl.ProcessManager):
    def __init__(self, repo, events):
        self.repo = repo
        self._events = events
        self.event_calls = 0
        self.card_calls = 0

    def _request_events(self, request_id):
        self.event_calls += 1
        return self._events

    def _show_task(self, task_id):
        self.card_calls += 1
        return "{}"


@pytest.fixture()
def manager(tmp_path, monkeypatch):
    canonical_repo = (tmp_path / "repo").resolve()
    canonical_repo.mkdir()
    workspace_dir = (tmp_path / "ws").resolve()
    (workspace_dir / "src").mkdir(parents=True)
    assert canonical_repo != workspace_dir
    assert canonical_repo == canonical_repo.resolve()
    assert workspace_dir == workspace_dir.resolve()
    candidate = workspace_dir / "src" / "mod.py"
    candidate.write_text("print('candidate marker')\n", encoding="utf-8")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    workspace = _Workspace(workspace_dir, canonical_repo, "req1")
    card = {
        "claim_epoch": 1,
        "objective": "objective",
        "acceptance": [],
        "required_outputs": [],
        "validation": [],
        "terminal_review": {
            "evidence": {
                "workspace": {},
                "changed_path_hashes": {"src/mod.py": digest},
                "validation": [],
                "quality_gate": {"checks": []},
            }
        },
    }
    monkeypatch.setattr(
        pl.WorkerWorkspace, "from_metadata", staticmethod(lambda meta: workspace)
    )
    monkeypatch.setattr(pl, "assert_gc_safe_workspace_shape", lambda *a, **k: None)
    monkeypatch.setattr(pl, "_parse_card", lambda *a, **k: card)
    monkeypatch.setattr(
        pl, "_changed_path_hashes", lambda ws, paths: {"src/mod.py": digest}
    )
    events = [
        {
            "task_id": "task1",
            "state": "review_ready",
            "adapter_id": "adapter-a",
            "runner": "vscode_lm",
        }
    ]
    return _Manager(canonical_repo, events), candidate


def test_three_lenses_reuse_one_preparation(manager):
    mgr, _candidate = manager
    packets = []
    for _lens in ("correctness", "security", "code_quality"):
        result = mgr._prepared_quality_review("req1", "task1")
        assert result["ok"] is True
        packets.append(result["prepared"]["packet"])
    assert mgr.event_calls == 1
    assert mgr.card_calls == 1
    assert {packet["packet_sha256"] for packet in packets} == {
        packets[0]["packet_sha256"]
    }


def test_prepared_packet_delivers_source_evidence_in_every_lens_prompt(manager):
    mgr, _candidate = manager
    packet = mgr._prepared_quality_review("req1", "task1")["prepared"]["packet"]
    scoped = packet["candidate"]["scoped_audits"]
    assert set(scoped) == {"correctness", "security", "code_quality"}
    for lens in ("correctness", "security", "code_quality"):
        prompt = quality_reviewer.build_review_prompt(packet, lens=lens)
        assert "candidate marker" in prompt
        assert "active graph-scoped audit entry" in prompt
        scope = scoped[lens]["packet"]
        assert scope["review_lens"]["lens_kind"] == lens
        assert scope["changed_paths"] == [
            {
                "path": "src/mod.py",
                "change_kind": "added",
                "line_start": 1,
                "line_end": 1,
            }
        ]
        assert scope["target_symbols"]
        assert scoped[lens]["fingerprint"]


def test_unreadable_candidate_fails_closed(manager):
    mgr, candidate = manager
    candidate.unlink()
    result = mgr._prepared_quality_review("req1", "task1")
    assert result["ok"] is False
    assert result["error"].startswith("quality_review_target_invalid:")


def test_identity_mismatch_fails_closed(manager):
    mgr, _candidate = manager
    result = mgr._prepared_quality_review("req1", "other-task")
    assert result == {"ok": False, "error": "quality_review_target_identity_mismatch"}


def test_quality_review_skips_generic_project_context_and_prefetch(
    tmp_path, monkeypatch
):
    card = {
        "project_context": {
            "source_graph": {"mode": "focus", "query": "slow duplicate prefetch"}
        }
    }

    def unexpected_collect(*_args, **_kwargs):
        raise AssertionError("generic project context must not run for reviewers")

    monkeypatch.setattr(pl.project_context, "collect_project_context", unexpected_collect)
    binding = {"packet": {"packet_sha256": "a" * 64}}

    assert pl._launch_project_context(tmp_path, card, binding) is None
    assert pl._launch_source_graph_request(card, binding) is None


def test_ordinary_worker_keeps_project_context_and_source_graph_prefetch(
    tmp_path, monkeypatch
):
    request = {"mode": "focus", "query": "worker orientation"}
    card = {"project_context": {"source_graph": request}}
    sentinel = object()
    monkeypatch.setattr(
        pl.project_context,
        "collect_project_context",
        lambda repo, observed_card: sentinel,
    )

    assert pl._launch_project_context(tmp_path, card, None) is sentinel
    assert pl._launch_source_graph_request(card, None) == request
