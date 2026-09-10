"""Tests for quality_reviewer module: packet file transport, E2BIG avoidance,
manager alias rejection, and sealed receipt reconciliation."""

import hashlib
import importlib.machinery
import json
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from aiworkhub import quality_review, quality_review_ingest, quality_reviewer
from aiworkhub.quality_reviewer import ReviewerEvidenceError


def _canonical_digest(payload: dict) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _packet_with_findings(
    packet_sha256: str | None = None,
    candidate_path: str = "src/module.py",
) -> dict:
    body = {
        "candidate": {
            "scoped_audits": {
                "correctness": {
                    "known_unknowns": [],
                    "packet": {"changed_paths": [{"path": candidate_path}]},
                },
                "security": {
                    "known_unknowns": [],
                    "packet": {"changed_paths": [{"path": candidate_path}]},
                },
                "code_quality": {
                    "known_unknowns": [],
                    "packet": {
                        "changed_paths": [{"path": candidate_path}],
                        "target_symbols": [
                            {
                                "qualified_name": "module.parse_config",
                                "path": "src/existing.py",
                                "line_start": 8,
                            },
                            {"qualified_name": "pathlib.Path"},
                        ],
                    },
                },
            },
            "path": candidate_path,
            "findings": [
                {
                    "id": "F001",
                    "severity": "medium",
                    "summary": "Null check missing",
                    "evidence": f"{candidate_path}:42",
                }
            ],
        },
    }
    if packet_sha256 is None:
        packet_sha256 = _canonical_digest(body)
    return {"packet_sha256": packet_sha256, **body}


def _packet_with_changed_source(
    *,
    candidate_path: str = "src/module.py",
    start: int = 12,
    end: int = 14,
    candidate_start: int | None = None,
    candidate_end: int | None = None,
    mechanical_checks: list[dict] | None = None,
) -> dict:
    packet = _packet_with_findings(candidate_path=candidate_path)
    packet["candidate"]["changed_paths"] = [{"path": candidate_path}]
    packet["candidate"]["source_evidence"] = [
        {
            "path": candidate_path,
            "candidate_sha256": "a" * 64,
            "excerpt": "changed code",
            "excerpt_bytes": 12,
            "source_bytes": 12,
            "truncated": False,
            "segments": [
                {
                    "kind": "replace",
                    "candidate_start_line": candidate_start or start,
                    "candidate_end_line": candidate_end or end,
                    "changed_start_line": start,
                    "changed_end_line": end,
                    "baseline_start_line": start,
                    "baseline_end_line": end,
                    "excerpt_bytes": 12,
                    "truncated": False,
                }
            ],
        }
    ]
    if mechanical_checks is not None:
        packet["mechanical_checks"] = mechanical_checks
    return packet


def _scoped_audit(lens: str, *, path: str = "src/module.py") -> dict:
    packet = {
        "task_id": "task1",
        "review_lens": {"lens_kind": lens},
        "changed_paths": [{"path": path}],
        "known_unknowns": [f"{lens} graph boundary"],
    }
    return {
        "schema_id": "aiworkhub.scoped_audit.v1",
        "fingerprint": hashlib.sha256(
            json.dumps(
                packet,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        "known_unknowns": packet["known_unknowns"],
        "packet": packet,
    }


def _scoped_audits(*lenses: str, path: str = "src/module.py") -> dict[str, dict]:
    return {lens: _scoped_audit(lens, path=path) for lens in lenses}


class TestBuildReviewPrompt:
    def test_inline_embeds_prompt(self):
        packet = _packet_with_findings()
        prompt = quality_reviewer.build_review_prompt(
            packet, lens="correctness",
            submit_tool_name="aiworkhub_worker_quality_review_submit",
        )
        assert "aiworkhub_worker_quality_review_submit" not in prompt
        assert "exactly one JSON object" in prompt
        assert '"lens":"correctness","findings":[...]' in prompt

    def test_prompt_names_the_receipts_the_supervisor_already_produced(self):
        """Reviewers were re-deriving evidence the packet already carried.

        Measured on request fb150636: 38 model turns, 17 tool calls, only 4 of
        them reading the candidate. The rest were scaffolding -- sha256sum over
        paths whose digests were in the packet, and four attempts to re-run a
        pytest command whose exact argv, returncode and output the packet
        already carried, three of them lost hunting for an interpreter.
        """
        prompt = quality_reviewer.build_review_prompt(
            _packet_with_findings(), lens="correctness"
        )
        assert "sha256sum" in prompt, "digests must be named as already recorded"
        assert "terminal_validation" in prompt
        assert "mechanical_checks" in prompt
        assert "Do not re-run pytest" in prompt
        assert "do not go looking for an interpreter" in prompt

    def test_code_quality_prompt_names_evidence_bound_overbuild_categories(self):
        prompt = quality_reviewer.build_review_prompt(
            _packet_with_findings(), lens="code_quality"
        )

        assert "duplicate_existing_symbol" in prompt
        assert "handrolled_standard_or_platform_capability" in prompt
        assert "unnecessary_abstraction" in prompt
        assert "excess_scope" in prompt
        assert "replacement" in prompt
        assert "removable_surface" in prompt
        assert "Over-build reports must be disposition=defect/actionable" in prompt
        assert "check-only evidence and test-target evidence are rejected" in prompt
        assert "Raw line count, token count and aesthetic preference are not failures" in prompt
        assert "must not be downgraded for minimality" in prompt

    @pytest.mark.parametrize("lens", ["correctness", "security", "code_quality"])
    def test_prompt_delivers_the_lens_scope_once_inside_the_sealed_packet(self, lens):
        """The scope reaches the reviewer exactly once, inside the packet.

        Measured on 38 surviving packets (2026-09-08 audit): the prompt
        inlined the active scope as ACTIVE_SCOPED_AUDIT AND shipped the full
        three-lens packet, so a reviewer received its own scope twice and the
        other two lenses' identical scopes once, and every packet overflowed
        the inline cap.  ``build_lens_packet`` is what the launcher binds; the
        prompt carries that packet and nothing else about the scope.
        """
        full = quality_reviewer.build_review_packet(
            request_id="req1",
            task_id="task1",
            claim_epoch=1,
            worker_provider="adapter-a",
            changed_path_hashes={"src/module.py": "a" * 64},
            scoped_audits=_scoped_audits(
                "correctness",
                "security",
                "code_quality",
            ),
        )
        packet = quality_reviewer.build_lens_packet(full, lens=lens)

        prompt = quality_reviewer.build_review_prompt(packet, lens=lens)

        assert "ACTIVE_SCOPED_AUDIT:" not in prompt
        assert f"candidate.scoped_audits.{lens} is the graph-scoped audit" in prompt
        assert prompt.count(f'"lens_kind":"{lens}"') == 1
        # "Once" is measured on the scoped-audit OBJECT, not on one of its
        # strings: the wrapper promotes the payload's ``known_unknowns``
        # alongside the sealed payload -- a pre-existing shape pinned by
        # test_scoped_audit_known_unknowns_are_preserved_in_packet -- so that
        # one list legitimately reads twice inside the single delivered scope.
        scope_json = json.dumps(
            packet["candidate"]["scoped_audits"][lens],
            ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        )
        assert prompt.count(scope_json) == 1
        assert prompt.count(f'"{lens} graph boundary"') == 2
        for other in {"correctness", "security", "code_quality"} - {lens}:
            assert f'"lens_kind":"{other}"' not in prompt
            assert f'"{other} graph boundary"' not in prompt
        assert packet["packet_sha256"] in prompt

    def test_prompt_names_the_diff_caller_context_and_output_tails_as_settled(self):
        """The 2026-09-08 audit: 63.5% of reviewer read bytes were the
        candidate's own changed files and 38% of runs re-ran git diff, because
        the packet carried a 4 KB excerpt.  The prompt now names what the
        packet carries so the reviewer does not go and re-derive it."""
        prompt = quality_reviewer.build_review_prompt(
            _packet_with_findings(), lens="correctness"
        )
        assert "candidate.source_evidence carries the unified diff" in prompt
        assert "diff_complete" in prompt
        assert "do not run git diff" in prompt
        assert "candidate.caller_context" in prompt
        assert "stdout_tail" in prompt and "stderr_tail" in prompt
        assert "candidate.delta" in prompt
        assert "never a prior verdict" in prompt

    def test_structured_check_evidence_rejects_source_siblings(self):
        packet = _packet_with_changed_source(
            mechanical_checks=[{"check_id": "ruff", "status": "failed"}]
        )

        with pytest.raises(ReviewerEvidenceError) as excinfo:
            quality_reviewer.normalize_packet_findings(
                packet,
                lens="code_quality",
                findings=[
                    {
                        "severity": "medium",
                        "summary": "Ruff found a concrete issue",
                        "evidence": {"check_id": "ruff"},
                        "path": "src/module.py",
                        "line_start": 12,
                        "line_end": 12,
                    }
                ],
            )

        assert "structured_evidence_conflict:source" in str(excinfo.value)

    def test_file_transport_writes_canonical_packet_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        large_body = {
            "candidate": {
                "scoped_audits": {"code_quality": {"known_unknowns": [], "packet": {"changed_paths": [{"path": "src/big.py"}]}}},
                "path": "src/big.py",
                "findings": [
                    {"id": f"F{i:04d}", "severity": "low",
                     "summary": "x" * 1900, "evidence": "y" * 1900}
                    for i in range(100)
                ],
            },
        }
        packet_sha256 = _canonical_digest(large_body)
        large_body["packet_sha256"] = packet_sha256
        packet_file = tmp_path / "packet.json"
        packet_file.write_text("stale", encoding="utf-8")
        monkeypatch.setenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, str(tmp_path))

        prompt = quality_reviewer.build_review_prompt(
            large_body, lens="code_quality",
            submit_tool_name="aiworkhub_worker_quality_review_submit",
            packet_file=str(packet_file), max_inline_bytes=1,
        )

        expected = json.dumps(
            large_body,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        assert packet_file.read_text(encoding="utf-8") == expected
        assert "QUALITY_REVIEW_PACKET_FILE:" in prompt
        assert f"PACKET_SHA256: {packet_sha256}" in prompt
        assert "canonical serialized packet" in prompt
        assert "QUALITY_REVIEW_PACKET:" not in prompt

    def test_file_transport_creates_missing_packet_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        packet = _packet_with_findings()
        packet_file = tmp_path / "created.json"
        monkeypatch.setenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, str(tmp_path))

        quality_reviewer.build_review_prompt(
            packet, lens="correctness",
            packet_file=str(packet_file), max_inline_bytes=1,
        )

        assert json.loads(packet_file.read_text(encoding="utf-8")) == packet

    def test_no_root_direct_call_inlines_and_does_not_write_caller_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.delenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, raising=False)
        packet = _packet_with_findings()
        caller_path = tmp_path / "caller-selected.json"
        prompt = quality_review.assemble_reviewer_prompt(
            packet, lens="correctness", adapter_id="claude_cli",
            packet_path=str(caller_path),
        )
        assert quality_review.extract_inline_packet(prompt) == packet
        assert "QUALITY_REVIEW_PACKET_FILE:" not in prompt
        assert str(caller_path) not in prompt
        assert not caller_path.exists()

        oversized = dict(packet)
        oversized["candidate"] = dict(packet["candidate"])
        oversized["candidate"]["padding"] = "x" * (100 * 1024)
        oversized["packet_sha256"] = _canonical_digest(
            {k: v for k, v in oversized.items() if k != "packet_sha256"}
        )
        with pytest.raises(ReviewerEvidenceError, match="quality_review_packet_too_large"):
            quality_review.assemble_reviewer_prompt(
                oversized, lens="correctness", adapter_id="claude_cli",
                packet_path=str(caller_path),
            )
        assert not caller_path.exists()

        runtime_root = tmp_path / "runtime"
        runtime_root.mkdir()
        bound_path = runtime_root / "quality_review_packet.json"
        bound = quality_review.assemble_reviewer_prompt(
            packet, lens="correctness", adapter_id="claude_cli",
            packet_path=str(bound_path), packet_root=runtime_root,
        )
        assert bound.count("QUALITY_REVIEW_PACKET_FILE:") == 1
        assert f"PACKET_SHA256: {packet['packet_sha256']}" in bound
        assert "QUALITY_REVIEW_PACKET:" not in bound
        assert json.loads(bound_path.read_text(encoding="utf-8")) == packet

    @pytest.mark.parametrize("adapter_id", ["codex_cli", "deepseek_copilot_cli"])
    def test_native_oversized_packet_uses_explicit_coordinator_root_without_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter_id: str,
    ):
        monkeypatch.delenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, raising=False)
        packet = _packet_with_findings()
        packet["candidate"]["padding"] = "x" * (100 * 1024)
        packet["packet_sha256"] = _canonical_digest({k: v for k, v in packet.items() if k != "packet_sha256"})
        runtime_root = tmp_path / "task_mcp_worker_runtime"
        runtime_root.mkdir()
        packet_path = runtime_root / "quality_review_packet.json"

        prompt = quality_review.assemble_reviewer_prompt(
            packet, lens="correctness", adapter_id=adapter_id,
            packet_path=str(packet_path), packet_root=runtime_root,
        )

        assert "QUALITY_REVIEW_PACKET_FILE:" in prompt
        assert "QUALITY_REVIEW_PACKET:" not in prompt
        assert json.loads(packet_path.read_text(encoding="utf-8")) == packet
        assert quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV not in quality_reviewer.os.environ

    def test_explicit_packet_root_overrides_env_without_global_mutation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        env_root = tmp_path / "environment"
        explicit_root = tmp_path / "coordinator"
        env_root.mkdir()
        explicit_root.mkdir()
        monkeypatch.setenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, str(env_root))
        packet = _packet_with_findings()
        quality_reviewer.build_review_prompt(
            packet, lens="correctness", packet_file="packet.json",
            packet_root=explicit_root, max_inline_bytes=1,
        )
        assert json.loads((explicit_root / "packet.json").read_text(encoding="utf-8")) == packet
        assert not (env_root / "packet.json").exists()
        assert quality_reviewer.os.environ[quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV] == str(env_root)

    def test_concurrent_packet_roots_do_not_crosswrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        env_root = tmp_path / "environment"
        env_root.mkdir()
        monkeypatch.setenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, str(env_root))
        roots = [tmp_path / "manager-a", tmp_path / "manager-b"]
        packets = [_packet_with_findings(candidate_path=f"src/{name}.py") for name in ("a", "b")]
        for root in roots:
            root.mkdir()

        def render(index):
            return quality_reviewer.build_review_prompt(
                packets[index], lens="correctness", packet_file="packet.json",
                packet_root=roots[index], max_inline_bytes=1,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            prompts = list(pool.map(render, range(2)))
        for index, root in enumerate(roots):
            assert json.loads((root / "packet.json").read_text(encoding="utf-8")) == packets[index]
            assert packets[index]["packet_sha256"] in prompts[index]
        assert not (env_root / "packet.json").exists()
        assert quality_reviewer.os.environ[quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV] == str(env_root)

    @pytest.mark.parametrize("escape", ["../outside.json", "absolute"])
    def test_explicit_packet_root_keeps_path_confinement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, escape: str,
    ):
        root = tmp_path / "runtime"
        root.mkdir()
        monkeypatch.setenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, str(tmp_path))
        destination = str(tmp_path / "outside.json") if escape == "absolute" else escape
        with pytest.raises(ReviewerEvidenceError, match="review_packet_file_outside_root"):
            quality_reviewer.build_review_prompt(
                _packet_with_findings(), lens="correctness", packet_file=destination,
                packet_root=root, max_inline_bytes=1,
            )
        assert not (tmp_path / "outside.json").exists()

    def test_explicit_invalid_root_does_not_fall_back_to_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, str(tmp_path))
        with pytest.raises(ReviewerEvidenceError, match="review_packet_file_root_invalid"):
            quality_reviewer.build_review_prompt(
                _packet_with_findings(), lens="correctness", packet_file="packet.json",
                packet_root=tmp_path / "missing", max_inline_bytes=1,
            )
        assert not (tmp_path / "packet.json").exists()

    def test_blind_inline_transport_does_not_require_or_write_packet_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.delenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, raising=False)
        packet = _packet_with_findings()
        packet["candidate"]["padding"] = "x" * (100 * 1024)
        packet["packet_sha256"] = _canonical_digest({k: v for k, v in packet.items() if k != "packet_sha256"})
        path = tmp_path / "not-created" / "packet.json"
        prompt = quality_review.assemble_reviewer_prompt(
            packet, lens="correctness", adapter_id="vscode_lm",
            packet_path=str(path), packet_root=path.parent,
        )
        assert quality_review.extract_inline_packet(prompt) == packet
        assert not path.parent.exists()

    def test_process_manager_supplies_verified_workspace_packet_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        from test_process_launcher import _reviewer_launch_setup

        from aiworkhub import process_launcher, worker_ai_tools_mcp

        manager, binding = _reviewer_launch_setup(tmp_path, monkeypatch)
        monkeypatch.delenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, raising=False)
        packet = _packet_with_findings()
        # The launcher writes the packet ONE lens receives and refuses a packet
        # still carrying the other lenses' scopes
        # (``quality_review_packet_lens_scope_mismatch``), so bind this lens's
        # packet exactly as ``_launch_quality_review`` does.
        packet["candidate"]["scoped_audits"] = {
            binding["lens"]: packet["candidate"]["scoped_audits"][binding["lens"]]
        }
        packet["candidate"]["padding"] = "x" * (100 * 1024)
        packet["packet_sha256"] = _canonical_digest({k: v for k, v in packet.items() if k != "packet_sha256"})
        binding["packet"] = packet
        expected_root = Path(binding["source_workspace"]["home"]) / "task_mcp_worker_runtime"
        assert expected_root != manager.process_dir
        monkeypatch.setattr(worker_ai_tools_mcp, "verify_quality_review_prewarm_authority", lambda *_a, **_k: None)
        monkeypatch.setattr(worker_ai_tools_mcp, "prewarm_quality_review_source_graph", lambda *_a, **_k: None)
        monkeypatch.setattr(process_launcher, "_provision_worker_mcp_runtime_for_authority", lambda *_a, **_k: object())
        assemble = quality_review.assemble_reviewer_prompt
        observed = {}

        class PromptObserved(BaseException):
            """Stop before any provider or subprocess can be launched."""

        def assemble_then_stop(packet, **kwargs):
            observed.update(kwargs)
            observed["prompt"] = assemble(packet, **kwargs)
            raise PromptObserved

        monkeypatch.setattr(quality_review, "assemble_reviewer_prompt", assemble_then_stop)
        with pytest.raises(PromptObserved):
            manager._launch_isolated(
                task_id="TASK_REVIEW_1", runner="claude_worker_reviewer",
                topic="quality_review", adapter_id="claude_cli", model=None,
                owner_prompt="", timeout_seconds=30, quality_review_binding=binding,
            )

        assert observed["packet_root"] == expected_root
        assert Path(observed["packet_path"]) == expected_root / "quality_review_packet.json"
        assert "QUALITY_REVIEW_PACKET_FILE:" in observed["prompt"]
        assert json.loads(Path(observed["packet_path"]).read_text(encoding="utf-8")) == packet
        assert quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV not in quality_reviewer.os.environ

    def test_file_transport_rejects_target_symlink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        packet = _packet_with_findings()
        victim = tmp_path / "victim.json"
        victim.write_text("victim", encoding="utf-8")
        packet_file = tmp_path / "packet.json"
        packet_file.symlink_to(victim)
        monkeypatch.setenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, str(tmp_path))

        with pytest.raises(ReviewerEvidenceError, match="review_packet_file_symlink"):
            quality_reviewer.build_review_prompt(
                packet,
                lens="correctness",
                packet_file=str(packet_file),
                max_inline_bytes=1,
            )

        assert victim.read_text(encoding="utf-8") == "victim"

    def test_file_transport_ignores_precreated_predictable_temp_symlink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        packet = _packet_with_findings()
        victim = tmp_path / "victim.json"
        victim.write_text("victim", encoding="utf-8")
        packet_file = tmp_path / "packet.json"
        predictable_old_temp = tmp_path / ".packet.json.tmp"
        predictable_old_temp.symlink_to(victim)
        monkeypatch.setenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, str(tmp_path))

        quality_reviewer.build_review_prompt(
            packet,
            lens="correctness",
            packet_file=str(packet_file),
            max_inline_bytes=1,
        )

        assert victim.read_text(encoding="utf-8") == "victim"
        assert predictable_old_temp.is_symlink()
        assert json.loads(packet_file.read_text(encoding="utf-8")) == packet

    def test_file_transport_rejects_outside_runtime_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        packet = _packet_with_findings()
        root = tmp_path / "runtime"
        root.mkdir()
        outside = tmp_path / "outside.json"
        monkeypatch.setenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, str(root))

        with pytest.raises(ReviewerEvidenceError, match="review_packet_file_outside_root"):
            quality_reviewer.build_review_prompt(
                packet,
                lens="correctness",
                packet_file=str(outside),
                max_inline_bytes=1,
            )

        assert not outside.exists()

    def test_file_transport_rejects_parent_swap_during_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        packet = _packet_with_findings()
        root = tmp_path / "runtime"
        root.mkdir()
        packet_parent = root / "packets"
        packet_parent.mkdir()
        packet_file = packet_parent / "packet.json"
        real_replace = quality_reviewer.os.replace
        monkeypatch.setenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, str(root))

        def swap_parent_then_replace(src, dst):
            dst = Path(dst)
            moved = tmp_path / "packets-old"
            dst.parent.rename(moved)
            dst.parent.mkdir()
            real_replace(moved / Path(src).name, dst)

        monkeypatch.setattr(quality_reviewer.os, "replace", swap_parent_then_replace)

        with pytest.raises(
            ReviewerEvidenceError,
            match="review_packet_file_identity_changed",
        ):
            quality_reviewer.build_review_prompt(
                packet,
                lens="correctness",
                packet_file=str(packet_file),
                max_inline_bytes=1,
            )

    def test_invalid_lens_raises(self):
        with pytest.raises(ReviewerEvidenceError, match="invalid_reviewer_lens"):
            quality_reviewer.build_review_prompt(
                _packet_with_findings(), lens="unknown",
            )


class TestBuildLensPacket:
    def _full_packet(self) -> dict:
        return quality_reviewer.build_review_packet(
            request_id="req1",
            task_id="task1",
            claim_epoch=1,
            worker_provider="adapter-a",
            changed_path_hashes={"src/module.py": "a" * 64},
            objective="keep every section",
            scoped_audits=_scoped_audits("correctness", "security", "code_quality"),
        )

    @pytest.mark.parametrize("lens", ["correctness", "security", "code_quality"])
    def test_lens_packet_keeps_only_that_lens_and_reseals_the_digest(self, lens):
        full = self._full_packet()

        packet = quality_reviewer.build_lens_packet(full, lens=lens)

        assert set(packet["candidate"]["scoped_audits"]) == {lens}
        assert packet["candidate"]["scoped_audits"][lens] == (
            full["candidate"]["scoped_audits"][lens]
        )
        for key in ("schema_id", "target", "contract", "terminal_validation",
                    "mechanical_checks", "combined_tree_checks"):
            assert packet[key] == full[key]
        assert packet["candidate"]["changed_paths"] == full["candidate"]["changed_paths"]
        body = {k: v for k, v in packet.items() if k != "packet_sha256"}
        assert packet["packet_sha256"] == _canonical_digest(body)
        assert packet["packet_sha256"] != full["packet_sha256"]
        # The shared packet is untouched: the other lenses still read it.
        assert set(full["candidate"]["scoped_audits"]) == {
            "correctness", "security", "code_quality",
        }

    def test_lens_packet_is_smaller_than_the_shared_packet(self):
        full = self._full_packet()

        def encoded(value):
            return len(json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
            ).encode("utf-8"))

        packet = quality_reviewer.build_lens_packet(full, lens="security")

        assert encoded(packet) < encoded(full)

    def test_lens_digest_is_what_receipt_and_findings_bind_to(self):
        packet = quality_reviewer.build_lens_packet(self._full_packet(), lens="security")

        findings = quality_reviewer.normalize_packet_findings(
            packet, lens="security",
            findings=[{"severity": "medium", "summary": "s", "evidence": "src/module.py:3"}],
        )
        assert findings[0]["evidence_reference"]["path"] == "src/module.py"
        receipt = {
            "schema_id": quality_reviewer.RECEIPT_SCHEMA_ID,
            "packet_sha256": packet["packet_sha256"],
            "target": dict(packet["target"]),
            "reviewer": {"request_id": "rev-1", "task_id": "rev-task-1", "provider": "adapter-b"},
            "report": {"lens": "security", "read_only": True, "can_mutate_repo": False, "findings": findings},
        }
        verified = quality_reviewer.verify_reviewer_receipt(
            receipt, packet=packet,
            expected_reviewer_request_id="rev-1",
            expected_reviewer_task_id="rev-task-1",
            observed_provider="adapter-b",
            observed_terminal_state="review_ready",
            audit_verified=True,
        )
        assert verified["packet_sha256"] == packet["packet_sha256"]

    def test_lens_packet_missing_lens_fails_closed(self):
        full = quality_reviewer.build_review_packet(
            request_id="req1", task_id="task1", claim_epoch=1,
            worker_provider="adapter-a",
            changed_path_hashes={"src/module.py": "a" * 64},
            scoped_audits=_scoped_audits("correctness", "security"),
        )
        with pytest.raises(ReviewerEvidenceError, match="review_scope_lens_missing"):
            quality_reviewer.build_lens_packet(full, lens="code_quality")
        with pytest.raises(ReviewerEvidenceError, match="invalid_reviewer_lens"):
            quality_reviewer.build_lens_packet(full, lens="vibes")

    def test_lens_packet_refuses_a_tampered_shared_packet(self):
        full = self._full_packet()
        full["contract"]["objective"] = "tampered after sealing"
        with pytest.raises(ReviewerEvidenceError, match="review_packet_digest_invalid"):
            quality_reviewer.build_lens_packet(full, lens="correctness")

    def test_packet_without_scoped_audits_or_already_single_lens_passes_through(self):
        bare = {"packet_sha256": "a" * 64, "target": {"claim_epoch": 1}}
        assert quality_reviewer.build_lens_packet(bare, lens="correctness") == bare
        single = quality_reviewer.build_lens_packet(self._full_packet(), lens="security")
        assert quality_reviewer.build_lens_packet(single, lens="security") == single

    def test_bound_packet_file_uses_file_transport_even_when_lens_fits_inline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Sighted bound-path transport is FILE+SHA regardless of size.

        Measured over 38 surviving packets: 130 KB mean, 83.6% of it
        ``candidate.scoped_audits``. Lens slicing still shrinks the on-disk
        packet; a bound packet_file no longer switches the prompt to inline
        just because the slice is under 96 KiB.
        """
        scoped = _scoped_audits("correctness", "security", "code_quality")
        for lens, audit in scoped.items():
            audit["packet"]["known_unknowns"] = [f"{lens} graph boundary " + "x" * 35_000]
            audit["known_unknowns"] = audit["packet"]["known_unknowns"]
            audit["fingerprint"] = _canonical_digest(audit["packet"])
        full = quality_reviewer.build_review_packet(
            request_id="req1", task_id="task1", claim_epoch=1,
            worker_provider="adapter-a",
            changed_path_hashes={"src/module.py": "a" * 64},
            scoped_audits=scoped,
        )
        packet = quality_reviewer.build_lens_packet(full, lens="security")
        monkeypatch.setenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, str(tmp_path))

        shared_prompt = quality_reviewer.build_review_prompt(
            full, lens="security", packet_file=str(tmp_path / "shared.json"),
        )
        lens_prompt = quality_reviewer.build_review_prompt(
            packet, lens="security", packet_file=str(tmp_path / "lens.json"),
        )

        assert "QUALITY_REVIEW_PACKET_FILE:" in shared_prompt
        assert "QUALITY_REVIEW_PACKET:" not in shared_prompt
        assert "QUALITY_REVIEW_PACKET_FILE:" in lens_prompt
        assert "QUALITY_REVIEW_PACKET:" not in lens_prompt
        assert json.loads((tmp_path / "lens.json").read_text(encoding="utf-8")) == packet
        assert quality_review.extract_inline_packet(lens_prompt) is None
        unbound = quality_reviewer.build_review_prompt(packet, lens="security")
        assert quality_review.extract_inline_packet(unbound) == packet


class TestNormalizePacketFindings:
    def test_valid_findings_bound(self):
        packet = _packet_with_findings()
        findings = [{
            "id": "F002", "severity": "high",
            "summary": "Unsafe input", "evidence": "src/module.py:12",
        }]
        result = quality_reviewer.normalize_packet_findings(
            packet, lens="correctness", findings=findings,
        )
        assert isinstance(result, list)
        assert result[0]["id"] == "F002"

    def test_structured_source_evidence_is_canonicalized_and_bound(self):
        packet = _packet_with_findings()
        findings = [{
            "severity": "high",
            "summary": "Unsafe input",
            "evidence": {
                "path": "src/module.py",
                "line_start": 12,
                "line_end": 14,
            },
        }]

        result = quality_reviewer.normalize_packet_findings(
            packet, lens="correctness", findings=findings,
        )

        assert result[0]["evidence"] == "src/module.py:12-14"
        assert result[0]["evidence_reference"] == {
            "kind": "source",
            "path": "src/module.py",
            "line_start": 12,
            "line_end": 14,
        }

    @pytest.mark.parametrize(
        "evidence",
        [
            {
                "path": "src/module.py",
                "line_start": 12,
                "column": 3,
            },
            {
                "check_id": "lint:ruff",
                "line_start": 12,
            },
            {
                "path": "src/module.py",
                "line_start": 12,
                "line_end": 12,
                "check_id": "lint:ruff",
            },
        ],
    )
    def test_structured_evidence_requires_exact_source_or_check_shape(self, evidence):
        with pytest.raises(
            ReviewerEvidenceError,
            match="structured_evidence_invalid",
        ):
            quality_reviewer.normalize_packet_findings(
                _packet_with_findings(),
                lens="correctness",
                findings=[{
                    "severity": "high",
                    "summary": "Unsafe input",
                    "evidence": evidence,
                }],
            )

    def test_structured_evidence_conflict_fails_closed(self):
        packet = _packet_with_findings()
        with pytest.raises(
            ReviewerEvidenceError,
            match="structured_evidence_conflict:path",
        ):
            quality_reviewer.normalize_packet_findings(
                packet,
                lens="correctness",
                findings=[{
                    "severity": "high",
                    "summary": "Unsafe input",
                    "path": "src/other.py",
                    "evidence": {
                        "path": "src/module.py",
                        "line_start": 12,
                        "line_end": 12,
                    },
                }],
            )

    def test_canonical_finding_reingress_is_idempotent_and_rederives_authority(self):
        packet = _packet_with_findings()
        canonical = quality_reviewer.normalize_packet_findings(
            packet,
            lens="correctness",
            findings=[{
                "severity": "high",
                "summary": "Unsafe input",
                "evidence": "src/module.py:12-14",
                "path": "src/module.py",
                "line_start": 12,
                "line_end": 14,
            }],
        )[0]

        contradictory = {**canonical, "actionable": False}
        renormalized = quality_reviewer.normalize_packet_findings(
            packet, lens="correctness", findings=[contradictory]
        )[0]

        assert renormalized == canonical
        assert json.dumps(renormalized, sort_keys=True) == json.dumps(
            canonical, sort_keys=True
        )
        assert renormalized["actionable"] is True

    @pytest.mark.parametrize(
        "reference, error",
        [
            (
                {"kind": "source", "path": "src/other.py", "line_start": 1, "line_end": 1},
                "path_out_of_scope",
            ),
            (
                {"kind": "source", "path": "src/module.py", "line_start": 0, "line_end": 1},
                "line_invalid",
            ),
            ({"kind": "check", "check_id": "invented"}, "check_out_of_scope"),
            (
                {"kind": "test_target", "path": "tests/invented.py"},
                "path_out_of_scope",
            ),
            ({"kind": "source", "path": "src/module.py"}, "evidence_reference_invalid"),
        ],
    )
    def test_canonical_evidence_reference_is_revalidated(self, reference, error):
        finding = {
            "severity": "medium",
            "summary": "Unsafe input",
            "evidence": "src/module.py:12",
            "evidence_reference": reference,
            "actionable": True,
        }
        with pytest.raises(ReviewerEvidenceError, match=error):
            quality_reviewer.normalize_packet_findings(
                _packet_with_findings(), lens="correctness", findings=[finding]
            )

    @pytest.mark.parametrize(
        "reference",
        [
            {
                "kind": "source",
                "path": ["src/module.py"],
                "line_start": 12,
                "line_end": 12,
            },
            {
                "kind": "test_target",
                "path": {"value": "src/module.py"},
            },
            {"kind": "check", "check_id": ["lint:ruff"]},
        ],
    )
    def test_canonical_evidence_reference_rejects_unhashable_identity_values(
        self, reference
    ):
        packet = _packet_with_changed_source(
            mechanical_checks=[
                {
                    "check_id": "lint:ruff",
                    "kind": "lint",
                    "status": "failed",
                    "provenance": "precomputed",
                }
            ]
        )
        finding = {
            "severity": "medium",
            "summary": "Unsafe input",
            "evidence": "src/module.py:12",
            "evidence_reference": reference,
        }

        with pytest.raises(
            ReviewerEvidenceError,
            match="evidence_reference_invalid",
        ):
            quality_reviewer.normalize_packet_findings(
                packet, lens="correctness", findings=[finding]
            )

    @pytest.mark.parametrize(
        "line_fields",
        [
            {"line_start": 99},
            {"line_end": 99},
            {"line_start": 12, "line_end": 99},
        ],
    )
    def test_canonical_evidence_reference_rejects_sibling_line_bounds(
        self, line_fields
    ):
        finding = {
            "severity": "medium",
            "summary": "Unsafe input",
            "evidence": "src/module.py:12",
            "evidence_reference": {
                "kind": "source",
                "path": "src/module.py",
                "line_start": 12,
                "line_end": 12,
            },
            **line_fields,
        }

        with pytest.raises(
            ReviewerEvidenceError,
            match="evidence_reference_conflict",
        ):
            quality_reviewer.normalize_packet_findings(
                _packet_with_findings(), lens="correctness", findings=[finding]
            )

    @pytest.mark.parametrize(
        "evidence",
        [
            "Stack trace points at src/other.py:12",
            "Stack trace points at src/module.py:13",
        ],
    )
    def test_canonical_evidence_reference_rejects_conflicting_source_text(
        self, evidence
    ):
        finding = {
            "severity": "medium",
            "summary": "Unsafe input",
            "evidence": evidence,
            "evidence_reference": {
                "kind": "source",
                "path": "src/module.py",
                "line_start": 12,
                "line_end": 12,
            },
        }

        with pytest.raises(
            ReviewerEvidenceError,
            match="evidence_reference_conflict",
        ):
            quality_reviewer.normalize_packet_findings(
                _packet_with_findings(), lens="correctness", findings=[finding]
            )

    def test_canonical_evidence_reference_rejects_conflicting_check_text(self):
        packet = _packet_with_changed_source(
            mechanical_checks=[
                {
                    "check_id": "lint:ruff",
                    "kind": "lint",
                    "status": "failed",
                    "provenance": "precomputed",
                }
            ]
        )

        with pytest.raises(
            ReviewerEvidenceError,
            match="evidence_reference_conflict",
        ):
            quality_reviewer.normalize_packet_findings(
                packet,
                lens="correctness",
                findings=[{
                    "severity": "medium",
                    "summary": "Unsafe input",
                    "evidence": "lint:ruff",
                    "evidence_reference": {
                        "kind": "source",
                        "path": "src/module.py",
                        "line_start": 12,
                        "line_end": 12,
                    },
                }],
            )

    def test_sibling_source_reference_rejects_conflicting_evidence_text(self):
        with pytest.raises(
            ReviewerEvidenceError,
            match="evidence_reference_conflict",
        ):
            quality_reviewer.normalize_packet_findings(
                _packet_with_findings(candidate_path="src/module.py"),
                lens="correctness",
                findings=[{
                    "severity": "medium",
                    "summary": "Sibling path citation must agree with text.",
                    "evidence": "Trace points at src/other.py:12",
                    "path": "src/module.py",
                    "line_start": 12,
                    "line_end": 12,
                }],
            )

    def test_sibling_check_reference_rejects_conflicting_evidence_text(self):
        packet = _packet_with_changed_source(
            mechanical_checks=[
                {
                    "check_id": "lint:ruff",
                    "kind": "lint",
                    "status": "failed",
                    "provenance": "precomputed",
                }
            ]
        )

        with pytest.raises(
            ReviewerEvidenceError,
            match="evidence_reference_conflict",
        ):
            quality_reviewer.normalize_packet_findings(
                packet,
                lens="correctness",
                findings=[{
                    "severity": "medium",
                    "summary": "Sibling check citation must agree with text.",
                    "evidence": "Trace points at src/module.py:12",
                    "check_id": "lint:ruff",
                }],
            )

    def test_sibling_derived_references_accept_matching_evidence_text(self):
        packet = _packet_with_changed_source(
            mechanical_checks=[
                {
                    "check_id": "lint:ruff",
                    "kind": "lint",
                    "status": "failed",
                    "provenance": "precomputed",
                }
            ]
        )

        result = quality_reviewer.normalize_packet_findings(
            packet,
            lens="correctness",
            findings=[
                {
                    "severity": "medium",
                    "summary": "Sibling source citation agrees with text.",
                    "evidence": "Trace points at src/module.py:12",
                    "path": "src/module.py",
                    "line_start": 12,
                    "line_end": 12,
                },
                {
                    "severity": "medium",
                    "summary": "Sibling check citation agrees with text.",
                    "evidence": "lint:ruff",
                    "check_id": "lint:ruff",
                },
            ],
        )

        assert result[0]["evidence_reference"] == {
            "kind": "source",
            "path": "src/module.py",
            "line_start": 12,
            "line_end": 12,
        }
        assert result[1]["evidence_reference"] == {
            "kind": "check",
            "check_id": "lint:ruff",
        }

    def test_canonical_evidence_reference_preserves_free_form_evidence_text(self):
        finding = {
            "severity": "medium",
            "summary": "Unsafe input",
            "evidence": "The canonical reference below identifies the changed branch.",
            "evidence_reference": {
                "kind": "source",
                "path": "src/module.py",
                "line_start": 12,
                "line_end": 12,
            },
        }

        result = quality_reviewer.normalize_packet_findings(
            _packet_with_findings(), lens="correctness", findings=[finding]
        )

        assert result[0]["evidence"] == finding["evidence"]
        assert result[0]["evidence_reference"] == finding["evidence_reference"]

    def test_arbitrary_unknown_key_remains_rejected(self):
        with pytest.raises(ReviewerEvidenceError, match="unknown_key:authority"):
            quality_reviewer.normalize_packet_findings(
                _packet_with_findings(),
                lens="correctness",
                findings=[{
                    "severity": "medium",
                    "summary": "Unsafe input",
                    "evidence": "src/module.py:12",
                    "authority": True,
                }],
            )

    def test_legacy_finding_defaults_to_general_category(self):
        result = quality_reviewer.normalize_packet_findings(
            _packet_with_findings(),
            lens="correctness",
            findings=[{
                "severity": "medium",
                "summary": "Unsafe input",
                "evidence": "src/module.py:12",
            }],
        )

        assert result[0]["category"] == "general"

    @pytest.mark.parametrize(
        ("category", "detail_key", "detail_value"),
        [
            ("duplicate_existing_symbol", "replacement", "module.parse_config"),
            (
                "handrolled_standard_or_platform_capability",
                "replacement",
                "pathlib.Path",
            ),
            ("unnecessary_abstraction", "removable_surface", "src/module.py:12-14"),
            ("excess_scope", "removable_surface", "src/module.py:12-14"),
        ],
    )
    def test_overbuild_categories_are_preserved_with_actionable_evidence(
        self, category, detail_key, detail_value
    ):
        result = quality_reviewer.normalize_packet_findings(
            _packet_with_changed_source(),
            lens="code_quality",
            findings=[{
                "severity": "low",
                "category": category,
                "summary": "Over-built code has a direct replacement",
                "evidence": "Source Graph and diff show src/module.py:12",
                detail_key: detail_value,
            }],
        )

        assert result[0]["category"] == category
        assert result[0][detail_key] == detail_value
        assert result[0]["actionable"] is True
        assert result[0]["evidence_reference"]["path"] == "src/module.py"
        assert quality_reviewer.QUALITY_REVIEW_FINDING_REQUIRED_KEYS <= set(result[0])
        assert set(result[0]) <= quality_reviewer.QUALITY_REVIEW_FINDING_KEYS
        assert {"replacement", "removable_surface"} <= (
            quality_reviewer.QUALITY_REVIEW_FINDING_KEYS
            - quality_reviewer.QUALITY_REVIEW_FINDING_REQUIRED_KEYS
        )

    @pytest.mark.parametrize(
        "replacement",
        [
            "pathlib.Path.read_text",
            "str.removeprefix",
            "pathlib.Path",
            "uuid.UUID",
            "os.PathLike",
            "math.isfinite",
            "math.exp",
            "zlib.compress",
            "_hashlib.openssl_sha256",
            "os.path.join",
            "urllib.parse.urlparse",
        ],
    )
    def test_handrolled_platform_replacement_accepts_real_stdlib_symbol(
        self, replacement
    ):
        packet = _packet_with_changed_source()
        scope = packet["candidate"]["scoped_audits"]["code_quality"]["packet"]
        scope["target_symbols"] = [{"qualified_name": "module.parse_config"}]

        result = quality_reviewer.normalize_packet_findings(
            packet,
            lens="code_quality",
            findings=[{
                "severity": "low",
                "category": "handrolled_standard_or_platform_capability",
                "summary": "Hand-rolled path wrapper duplicates platform code.",
                "evidence": "Source Graph and diff show src/module.py:12",
                "replacement": replacement,
            }],
        )

        assert result[0]["replacement"] == replacement

    def test_stdlib_symbol_follows_relative_star_reexport_without_importing(
        self, monkeypatch
    ):
        trees = {
            "sample": quality_reviewer.ast.parse("from ._local import *\n"),
            "sample._local": quality_reviewer.ast.parse(
                "class Capability:\n    pass\n"
            ),
        }
        monkeypatch.setattr(
            quality_reviewer,
            "_stdlib_python_tree",
            lambda module: trees.get(module),
        )
        monkeypatch.setattr(
            quality_reviewer,
            "_platform_module_spec",
            lambda module: importlib.machinery.ModuleSpec(
                module,
                loader=None,
                is_package=module == "sample",
            ),
        )

        assert quality_reviewer._stdlib_python_module_defines(
            "sample", "Capability"
        )
        assert quality_reviewer._stdlib_alias_target(
            "sample", "Capability"
        ) == "sample._local.Capability"

    @pytest.mark.parametrize("replacement", ["math.isfinite", "str.removeprefix"])
    def test_handrolled_platform_replacement_accepts_without_typeshed(
        self, monkeypatch, replacement
    ):
        monkeypatch.setattr(quality_reviewer, "_typeshed_stdlib_roots", lambda: ())

        result = quality_reviewer.normalize_packet_findings(
            _packet_with_changed_source(),
            lens="code_quality",
            findings=[{
                "severity": "low",
                "category": "handrolled_standard_or_platform_capability",
                "summary": "Hand-rolled helper duplicates a platform capability.",
                "evidence": "Source Graph and diff show src/module.py:12",
                "replacement": replacement,
            }],
        )

        assert result[0]["replacement"] == replacement

    def test_handrolled_platform_replacement_rejects_fake_builtin_member_without_typeshed(
        self, monkeypatch
    ):
        monkeypatch.setattr(quality_reviewer, "_typeshed_stdlib_roots", lambda: ())

        with pytest.raises(ReviewerEvidenceError, match="overbuild_replacement_unbound"):
            quality_reviewer.normalize_packet_findings(
                _packet_with_changed_source(),
                lens="code_quality",
                findings=[{
                    "severity": "low",
                    "category": "handrolled_standard_or_platform_capability",
                    "summary": "Invented builtin members are not platform authority.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "str.not_a_real_method",
                }],
            )

    @pytest.mark.parametrize(
        "replacement",
        [
            "pathlib.Path.not_a_real_method",
            "pathlib.NotARealPathThing.read_text",
            "str.not_a_real_method",
            "not_stdlib.Path.read_text",
        ],
    )
    def test_handrolled_platform_replacement_rejects_unbound_qualified_lookalike(
        self, replacement
    ):
        with pytest.raises(ReviewerEvidenceError, match="overbuild_replacement_unbound"):
            quality_reviewer.normalize_packet_findings(
                _packet_with_changed_source(),
                lens="code_quality",
                findings=[{
                    "severity": "low",
                    "category": "handrolled_standard_or_platform_capability",
                    "summary": "Lookalike platform replacements are not authoritative.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": replacement,
                }],
            )

    def test_duplicate_replacement_rejects_candidate_introduced_symbol(self):
        packet = _packet_with_changed_source()
        scope = packet["candidate"]["scoped_audits"]["code_quality"]["packet"]
        scope["target_symbols"] = [
            {
                "qualified_name": "module.new_helper",
                "path": "src/module.py",
                "line_start": 12,
            }
        ]

        with pytest.raises(ReviewerEvidenceError, match="overbuild_replacement_unbound"):
            quality_reviewer.normalize_packet_findings(
                packet,
                lens="code_quality",
                findings=[{
                    "severity": "low",
                    "category": "duplicate_existing_symbol",
                    "summary": "A helper added in this diff cannot replace itself.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "module.new_helper",
                }],
            )

    def test_duplicate_replacement_rejects_locationless_target_symbol(self):
        packet = _packet_with_changed_source()
        scope = packet["candidate"]["scoped_audits"]["code_quality"]["packet"]
        scope["target_symbols"] = [{"qualified_name": "module.new_helper"}]

        with pytest.raises(ReviewerEvidenceError, match="overbuild_replacement_unbound"):
            quality_reviewer.normalize_packet_findings(
                packet,
                lens="code_quality",
                findings=[{
                    "severity": "low",
                    "category": "duplicate_existing_symbol",
                    "summary": "Locationless rows do not prove pre-existing code.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "module.new_helper",
                }],
            )

    def test_handrolled_replacement_rejects_locationless_impact_evidence(self):
        packet = _packet_with_changed_source()
        scope = packet["candidate"]["scoped_audits"]["code_quality"]["packet"]
        scope["target_symbols"] = []
        scope["impact_evidence"] = [{"replacement": "module.new_helper"}]

        with pytest.raises(ReviewerEvidenceError, match="overbuild_replacement_unbound"):
            quality_reviewer.normalize_packet_findings(
                packet,
                lens="code_quality",
                findings=[{
                    "severity": "low",
                    "category": "handrolled_standard_or_platform_capability",
                    "summary": "Locationless impact rows do not prove a base-tree helper.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "module.new_helper",
                }],
            )

    def test_handrolled_platform_replacement_does_not_import_reviewer_named_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        marker = tmp_path / "imported.txt"
        malicious = tmp_path / "malicious_replacement.py"
        malicious.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n"
            "payload = object()\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(tmp_path))

        with pytest.raises(ReviewerEvidenceError, match="overbuild_replacement_unbound"):
            quality_reviewer.normalize_packet_findings(
                _packet_with_changed_source(),
                lens="code_quality",
                findings=[{
                    "severity": "low",
                    "category": "handrolled_standard_or_platform_capability",
                    "summary": "Reviewer supplied import-time code as a replacement.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "malicious_replacement.payload",
                }],
            )

        assert not marker.exists()

    def test_handrolled_platform_replacement_ignores_stdlib_shadow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        marker = tmp_path / "shadow_imported.txt"
        shadow = tmp_path / "pathlib.py"
        shadow.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(tmp_path))

        result = quality_reviewer.normalize_packet_findings(
            _packet_with_changed_source(),
            lens="code_quality",
            findings=[{
                "severity": "low",
                "category": "handrolled_standard_or_platform_capability",
                "summary": "Hand-rolled path wrapper duplicates platform code.",
                "evidence": "Source Graph and diff show src/module.py:12",
                "replacement": "pathlib.Path",
            }],
        )

        assert result[0]["replacement"] == "pathlib.Path"
        assert not marker.exists()

    def test_handrolled_platform_replacement_rejects_ambient_third_party_symbol(self):
        with pytest.raises(ReviewerEvidenceError, match="overbuild_replacement_unbound"):
            quality_reviewer.normalize_packet_findings(
                _packet_with_changed_source(),
                lens="code_quality",
                findings=[{
                    "severity": "low",
                    "category": "handrolled_standard_or_platform_capability",
                    "summary": "Ambient packages are not platform authority.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "pytest.mark",
                }],
            )

    def test_handrolled_platform_replacement_rejects_antigravity_without_importing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        opened: list[str] = []
        sys.modules.pop("antigravity", None)
        monkeypatch.setattr(webbrowser, "open", opened.append)

        with pytest.raises(ReviewerEvidenceError, match="overbuild_replacement_unbound"):
            quality_reviewer.normalize_packet_findings(
                _packet_with_changed_source(),
                lens="code_quality",
                findings=[{
                    "severity": "low",
                    "category": "handrolled_standard_or_platform_capability",
                    "summary": "Invalid stdlib names must not execute top-level code.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "antigravity.not_real",
                }],
            )

        assert opened == []
        assert "antigravity" not in sys.modules

    def test_replacement_categories_do_not_cross_bind_authority(self):
        with pytest.raises(ReviewerEvidenceError, match="overbuild_replacement_unbound"):
            quality_reviewer.normalize_packet_findings(
                _packet_with_changed_source(),
                lens="code_quality",
                findings=[{
                    "severity": "low",
                    "category": "duplicate_existing_symbol",
                    "summary": "Duplicate findings require repository replacement evidence.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "pathlib.Path",
                }],
            )

        with pytest.raises(ReviewerEvidenceError, match="overbuild_replacement_unbound"):
            quality_reviewer.normalize_packet_findings(
                _packet_with_changed_source(),
                lens="code_quality",
                findings=[{
                    "severity": "low",
                    "category": "handrolled_standard_or_platform_capability",
                    "summary": "Platform findings require platform authority.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "module.parse_config",
                }],
            )

    def test_handrolled_platform_replacement_ignores_ambient_forged_typeshed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        forged = tmp_path / "mypy" / "typeshed" / "stdlib"
        forged.mkdir(parents=True)
        (forged / "pathlib.pyi").write_text("class Fake: ...\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.setenv("PYTHONPATH", str(tmp_path))

        with pytest.raises(ReviewerEvidenceError, match="overbuild_replacement_unbound"):
            quality_reviewer.normalize_packet_findings(
                _packet_with_changed_source(),
                lens="code_quality",
                findings=[{
                    "severity": "low",
                    "category": "handrolled_standard_or_platform_capability",
                    "summary": "Forged ambient stubs are not platform authority.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "pathlib.Fake",
                }],
            )

        for replacement in ("pathlib.Path", "dict", "math.isfinite"):
            result = quality_reviewer.normalize_packet_findings(
                _packet_with_changed_source(),
                lens="code_quality",
                findings=[{
                    "severity": "low",
                    "category": "handrolled_standard_or_platform_capability",
                    "summary": "Real interpreter-owned platform capability remains valid.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": replacement,
                }],
            )
            assert result[0]["replacement"] == replacement

    def test_extension_backed_platform_replacement_uses_non_executing_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        def fail_exec_module(self, module):
            raise AssertionError("extension loader must not execute during validation")

        monkeypatch.setattr(
            importlib.machinery.ExtensionFileLoader,
            "exec_module",
            fail_exec_module,
        )

        result = quality_reviewer.normalize_packet_findings(
            _packet_with_changed_source(),
            lens="code_quality",
            findings=[{
                "severity": "low",
                "category": "handrolled_standard_or_platform_capability",
                "summary": "Hand-rolled finite check duplicates platform code.",
                "evidence": "Source Graph and diff show src/module.py:12",
                "replacement": "math.isfinite",
            }],
        )

        assert result[0]["replacement"] == "math.isfinite"

    @pytest.mark.parametrize("replacement", ["dict", "set", "open", "builtins.dict"])
    def test_handrolled_platform_replacement_accepts_builtins(self, replacement):
        result = quality_reviewer.normalize_packet_findings(
            _packet_with_changed_source(),
            lens="code_quality",
            findings=[{
                "severity": "low",
                "category": "handrolled_standard_or_platform_capability",
                "summary": "Hand-rolled container/open wrapper duplicates a builtin.",
                "evidence": "Source Graph and diff show src/module.py:12",
                "replacement": replacement,
            }],
        )

        assert result[0]["replacement"] == replacement

    @pytest.mark.parametrize(
        "replacement",
        [
            "pathlib...Path",
            "pathlib./tmp/escape.Path",
            "pathlib.\\.Path",
            "pathlib.\x00.Path",
            "../pathlib.Path",
            "pathlib.Path.__mro__",
        ],
    )
    def test_handrolled_platform_replacement_rejects_non_identifier_traversal_components(
        self, replacement
    ):
        with pytest.raises(ReviewerEvidenceError, match="overbuild_replacement_unbound"):
            quality_reviewer.normalize_packet_findings(
                _packet_with_changed_source(),
                lens="code_quality",
                findings=[{
                    "severity": "low",
                    "category": "handrolled_standard_or_platform_capability",
                    "summary": "Reviewer-controlled replacement must not shape stdlib paths.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": replacement,
                }],
            )

    def test_overbuild_category_requires_exact_evidence_and_remedy(self):
        with pytest.raises(ReviewerEvidenceError, match="exact_evidence_required"):
            quality_reviewer.normalize_packet_findings(
                _packet_with_changed_source(),
                lens="code_quality",
                findings=[{
                    "severity": "low",
                    "category": "duplicate_existing_symbol",
                    "summary": "Duplicates existing code",
                    "evidence": "duplicate helper exists",
                    "replacement": "module.helper",
                }],
            )
        with pytest.raises(
            ReviewerEvidenceError,
            match="overbuild_removable_surface_required",
        ):
            quality_reviewer.normalize_packet_findings(
                _packet_with_changed_source(),
                lens="code_quality",
                findings=[{
                    "severity": "low",
                    "category": "unnecessary_abstraction",
                    "summary": "Wrapper adds no behavior",
                    "evidence": "src/module.py:12",
                }],
            )

    @pytest.mark.parametrize(
        ("finding", "error"),
        [
            (
                {
                    "severity": "low",
                    "category": "duplicate_existing_symbol",
                    "summary": "Duplicate helper needs an existing replacement.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "removable_surface": "src/module.py:12",
                },
                "overbuild_replacement_required",
            ),
            (
                {
                    "severity": "low",
                    "category": "handrolled_standard_or_platform_capability",
                    "summary": "Hand-rolled capability needs an existing replacement.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "removable_surface": "src/module.py:12",
                },
                "overbuild_replacement_required",
            ),
            (
                {
                    "severity": "low",
                    "category": "unnecessary_abstraction",
                    "summary": "Abstraction finding must name changed removable surface.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "module.parse_config",
                },
                "overbuild_removable_surface_required",
            ),
            (
                {
                    "severity": "low",
                    "category": "excess_scope",
                    "summary": "Scope finding must name changed removable surface.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "module.parse_config",
                },
                "overbuild_removable_surface_required",
            ),
            (
                {
                    "severity": "low",
                    "category": "duplicate_existing_symbol",
                    "summary": "Duplicate finding cannot mix remedy types.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "module.parse_config",
                    "removable_surface": "src/module.py:12",
                },
                "overbuild_replacement_required",
            ),
            (
                {
                    "severity": "low",
                    "category": "excess_scope",
                    "summary": "Scope finding cannot mix remedy types.",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "module.parse_config",
                    "removable_surface": "src/module.py:12",
                },
                "overbuild_removable_surface_required",
            ),
        ],
    )
    def test_overbuild_category_rejects_swapped_or_mixed_remedy_types(
        self, finding, error
    ):
        with pytest.raises(ReviewerEvidenceError, match=error):
            quality_reviewer.normalize_packet_findings(
                _packet_with_changed_source(), lens="code_quality", findings=[finding]
            )

    @pytest.mark.parametrize("disposition", ["observation", "process_limit"])
    def test_overbuild_category_rejects_non_actionable_dispositions(self, disposition):
        with pytest.raises(ReviewerEvidenceError, match="overbuild_must_be_defect"):
            quality_reviewer.normalize_packet_findings(
                _packet_with_changed_source(),
                lens="code_quality",
                findings=[{
                    "severity": "low",
                    "disposition": disposition,
                    "category": "duplicate_existing_symbol",
                    "summary": "Helper duplicates an existing symbol",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "module.helper",
                }],
            )

    @pytest.mark.parametrize(
        ("finding", "error"),
        [
            (
                {
                    "severity": "low",
                    "category": "duplicate_existing_symbol",
                    "summary": "Check output suggests a duplicate helper",
                    "evidence": "check:quality",
                    "check_id": "check:quality",
                    "replacement": "module.helper",
                },
                "overbuild_source_evidence_required",
            ),
            (
                {
                    "severity": "low",
                    "category": "excess_scope",
                    "summary": "Test target is not changed source evidence",
                    "evidence": "src/module.py::test_added_behavior",
                    "removable_surface": "src/module.py:12",
                },
                "exact_evidence_required",
            ),
            (
                {
                    "severity": "low",
                    "category": "excess_scope",
                    "summary": "Unchanged line is outside the diff",
                    "evidence": "Source Graph and diff show src/module.py:40",
                    "removable_surface": "src/module.py:40",
                },
                "overbuild_changed_source_required",
            ),
            (
                {
                    "severity": "low",
                    "category": "duplicate_existing_symbol",
                    "summary": "Remedy is too vague to act on",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "x",
                },
                "overbuild_replacement_unbound",
            ),
            (
                {
                    "severity": "low",
                    "category": "handrolled_standard_or_platform_capability",
                    "summary": "Invented extension-module symbol is not authoritative",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "math.definitely_not_a_real_symbol",
                },
                "overbuild_replacement_unbound",
            ),
            (
                {
                    "severity": "low",
                    "category": "duplicate_existing_symbol",
                    "summary": "Concrete-looking symbol was not in scoped-audit evidence",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "module.invented_helper",
                },
                "overbuild_replacement_unbound",
            ),
            (
                {
                    "severity": "low",
                    "category": "handrolled_standard_or_platform_capability",
                    "summary": "Invented platform symbol is not authoritative",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "replacement": "pathlib.NotARealPathThing",
                },
                "overbuild_replacement_unbound",
            ),
            (
                {
                    "severity": "low",
                    "category": "unnecessary_abstraction",
                    "summary": "Line-like surface is outside changed source evidence",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "removable_surface": "src/module.py:99-120",
                },
                "overbuild_removable_surface_unbound",
            ),
            (
                {
                    "severity": "low",
                    "category": "excess_scope",
                    "summary": "Named surface does not identify changed lines",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "removable_surface": "ConfigWrapper",
                },
                "overbuild_removable_surface_unbound",
            ),
        ],
    )
    def test_overbuild_category_rejects_unbound_or_vague_findings(self, finding, error):
        packet = _packet_with_changed_source(
            mechanical_checks=[
                {
                    "check_id": "check:quality",
                    "kind": "lint",
                    "status": "failed",
                    "provenance": "precomputed",
                }
            ]
        )
        with pytest.raises(ReviewerEvidenceError, match=error):
            quality_reviewer.normalize_packet_findings(
                packet, lens="code_quality", findings=[finding]
            )

    @pytest.mark.parametrize(
        "evidence",
        ["Source Graph and diff show src/module.py:12junk", "src/module.py:12-extra"],
    )
    def test_source_evidence_requires_line_token_boundary(self, evidence):
        with pytest.raises(ReviewerEvidenceError, match="exact_evidence_required"):
            quality_reviewer.normalize_packet_findings(
                _packet_with_findings(),
                lens="correctness",
                findings=[{
                    "severity": "medium",
                    "summary": "Malformed suffix must not bind as source evidence.",
                    "evidence": evidence,
                }],
            )

    def test_check_evidence_requires_exact_token_boundary(self):
        packet = _packet_with_changed_source(
            mechanical_checks=[
                {
                    "check_id": "lint:ruff",
                    "kind": "lint",
                    "status": "failed",
                    "provenance": "precomputed",
                }
            ]
        )

        with pytest.raises(ReviewerEvidenceError, match="exact_evidence_required"):
            quality_reviewer.normalize_packet_findings(
                packet,
                lens="correctness",
                findings=[{
                    "severity": "medium",
                    "summary": "Check id superstring must not bind as check evidence.",
                    "evidence": "lint:ruff-extra",
                }],
            )

    def test_test_target_reference_is_rejected_for_actionable_defects(self):
        packet = _packet_with_findings()

        with pytest.raises(ReviewerEvidenceError, match="exact_evidence_required"):
            quality_reviewer.normalize_packet_findings(
                packet,
                lens="correctness",
                findings=[{
                    "severity": "medium",
                    "summary": "Legacy report names only a test target for the defect.",
                    "evidence": "src/module.py::test_behavior",
                }],
            )
        with pytest.raises(ReviewerEvidenceError, match="exact_evidence_required"):
            quality_reviewer.normalize_packet_findings(
                packet,
                lens="security",
                findings=[{
                    "severity": "medium",
                    "summary": "Canonical report names only a test target for the defect.",
                    "evidence": "src/module.py::test_behavior",
                    "evidence_reference": {
                        "kind": "test_target",
                        "path": "src/module.py",
                    },
                }],
            )

    def test_test_target_reference_remains_available_for_observations(self):
        result = quality_reviewer.normalize_packet_findings(
            _packet_with_findings(),
            lens="correctness",
            findings=[{
                "severity": "low",
                "disposition": "observation",
                "summary": "Related test target for follow-up",
                "evidence": "src/module.py::test_behavior",
            }],
        )

        assert result[0]["actionable"] is False
        assert result[0]["evidence_reference"] == {
            "kind": "test_target",
            "path": "src/module.py",
        }

    @pytest.mark.parametrize(
        "evidence",
        [
            "Source Graph and diff show src/module.py:10",
            "Source Graph and diff show src/module.py:1-100",
            "Source Graph and diff show src/module.py:12-15",
        ],
    )
    def test_overbuild_category_requires_cited_range_inside_changed_span(self, evidence):
        packet = _packet_with_changed_source(candidate_start=10, candidate_end=20)

        with pytest.raises(
            ReviewerEvidenceError,
            match="overbuild_changed_source_required",
        ):
            quality_reviewer.normalize_packet_findings(
                packet,
                lens="code_quality",
                findings=[{
                    "severity": "low",
                    "category": "excess_scope",
                    "summary": "Citation includes unchanged context around the hunk",
                    "evidence": evidence,
                    "removable_surface": "src/module.py:12-14",
                }],
            )

    def test_ingest_preserves_new_categories_and_legacy_reports(self):
        packet = _packet_with_changed_source()

        def normalize(report):
            return {
                "lens": "code_quality",
                "findings": quality_reviewer.normalize_packet_findings(
                    packet,
                    lens="code_quality",
                    findings=list(report.get("findings") or []),
                ),
            }

        report = {
            "lens": "code_quality",
            "findings": [
                {
                    "severity": "low",
                    "category": "excess_scope",
                    "summary": "Extra exported helper is outside the card",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "removable_surface": "src/module.py:12",
                },
                {
                    "severity": "medium",
                    "summary": "Legacy report shape still works",
                    "evidence": "src/module.py:20",
                },
            ],
        }
        event = json.dumps({"type": "result", "result": json.dumps(report)})

        result = quality_review_ingest.ingest_structured_final(
            [event], expected_lens="code_quality", normalize=normalize
        )

        assert result.report is not None
        findings = result.report["findings"]
        assert findings[0]["category"] == "excess_scope"
        assert findings[0]["removable_surface"] == "src/module.py:12"
        assert findings[1]["category"] == "general"
        assert quality_reviewer.QUALITY_REVIEW_FINDING_REQUIRED_KEYS <= set(
            findings[1]
        )
        assert set(findings[1]) <= quality_reviewer.QUALITY_REVIEW_FINDING_KEYS
        assert "replacement" not in findings[1]
        assert "removable_surface" not in findings[1]

    @pytest.mark.parametrize(
        "evidence_reference",
        [
            {"check_id": "lint:ruff", "line_start": 12},
            {
                "path": "src/module.py",
                "line_start": 12,
                "line_end": 12,
                "check_id": "lint:ruff",
            },
        ],
    )
    def test_ingest_rejects_malformed_evidence_reference_aliases(
        self, evidence_reference
    ):
        report = {
            "lens": "code_quality",
            "findings": [
                {
                    "severity": "medium",
                    "summary": "Alias carries contradictory evidence shape",
                    "evidence": "src/module.py:12",
                    "evidence_reference": evidence_reference,
                }
            ],
        }
        event = json.dumps({"type": "result", "result": json.dumps(report)})

        with pytest.raises(
            quality_review_ingest.ReviewProtocolError,
            match="evidence_reference_invalid",
        ):
            quality_review_ingest.ingest_structured_final(
                [event],
                expected_lens="code_quality",
                normalize=quality_review_ingest._normalize_review_finding_aliases,
            )

    @pytest.mark.parametrize(
        "evidence_reference",
        [
            {
                "kind": "source",
                "path": ["src/module.py"],
                "line_start": 12,
                "line_end": 12,
            },
            {
                "kind": "test_target",
                "path": {"value": "src/module.py"},
            },
            {"kind": "check", "check_id": ["lint:ruff"]},
        ],
    )
    def test_ingest_rejects_canonical_evidence_reference_non_string_identities(
        self, evidence_reference
    ):
        report = {
            "lens": "code_quality",
            "findings": [
                {
                    "severity": "medium",
                    "summary": "Canonical alias has a malformed identity.",
                    "evidence": "src/module.py:12",
                    "evidence_reference": evidence_reference,
                }
            ],
        }
        event = json.dumps({"type": "result", "result": json.dumps(report)})

        with pytest.raises(
            quality_review_ingest.ReviewProtocolError,
            match="evidence_reference_invalid",
        ):
            quality_review_ingest.ingest_structured_final(
                [event],
                expected_lens="code_quality",
                normalize=quality_review_ingest._normalize_review_finding_aliases,
            )

    def test_ingest_accepts_canonical_evidence_reference_aliases(self):
        report = {
            "lens": "code_quality",
            "findings": [
                {
                    "severity": "low",
                    "category": "excess_scope",
                    "summary": "Extra exported helper is outside the card",
                    "evidence": "Source Graph and diff show src/module.py:12",
                    "evidence_reference": {
                        "kind": "source",
                        "path": "src/module.py",
                        "line_start": 12,
                        "line_end": 12,
                    },
                    "removable_surface": "src/module.py:12",
                },
                {
                    "severity": "low",
                    "disposition": "observation",
                    "summary": "Check evidence remains accepted",
                    "evidence": "lint:ruff",
                    "evidence_reference": {
                        "kind": "check",
                        "check_id": "lint:ruff",
                    },
                },
            ],
        }
        event = json.dumps({"type": "result", "result": json.dumps(report)})
        packet = _packet_with_changed_source(
            mechanical_checks=[
                {
                    "check_id": "lint:ruff",
                    "kind": "lint",
                    "status": "failed",
                    "provenance": "precomputed",
                }
            ]
        )

        def normalize(raw_report):
            aliases = quality_review_ingest._normalize_review_finding_aliases(
                raw_report
            )
            return {
                "lens": "code_quality",
                "findings": quality_reviewer.normalize_packet_findings(
                    packet,
                    lens="code_quality",
                    findings=list(aliases.get("findings") or []),
                ),
            }

        result = quality_review_ingest.ingest_structured_final(
            [event], expected_lens="code_quality", normalize=normalize
        )

        assert result.report is not None
        assert result.report["findings"][0]["evidence_reference"] == {
            "kind": "source",
            "path": "src/module.py",
            "line_start": 12,
            "line_end": 12,
        }
        assert result.report["findings"][1]["evidence_reference"] == {
            "kind": "check",
            "check_id": "lint:ruff",
        }

    def test_ingest_revalidates_canonical_reference_conflicts(self):
        report = {
            "lens": "code_quality",
            "findings": [
                {
                    "severity": "medium",
                    "summary": "Conflicting canonical alias",
                    "evidence": "src/module.py:12",
                    "path": "src/other.py",
                    "evidence_reference": {
                        "kind": "source",
                        "path": "src/module.py",
                        "line_start": 12,
                        "line_end": 12,
                    },
                }
            ],
        }
        event = json.dumps({"type": "result", "result": json.dumps(report)})
        packet = _packet_with_changed_source()

        def normalize(raw_report):
            aliases = quality_review_ingest._normalize_review_finding_aliases(
                raw_report
            )
            return {
                "lens": "code_quality",
                "findings": quality_reviewer.normalize_packet_findings(
                    packet,
                    lens="code_quality",
                    findings=list(aliases.get("findings") or []),
                ),
            }

        with pytest.raises(ReviewerEvidenceError, match="evidence_reference_conflict"):
            quality_review_ingest.ingest_structured_final(
                [event], expected_lens="code_quality", normalize=normalize
            )


class TestVerifyReviewerReceipt:
    def test_valid_receipt_passes(self) -> None:
        """A properly constructed receipt with matching process-observed
        facts must pass verification and return the canonical sealed receipt."""
        packet = quality_reviewer.build_review_packet(
            request_id="req-vrfy-001",
            task_id="task-vrfy-001",
            claim_epoch=1,
            worker_provider="deepseek_vscode_lm",
            changed_path_hashes={"src/module.py": "a" * 64},
        )
        receipt = {
            "schema_id": quality_reviewer.RECEIPT_SCHEMA_ID,
            "packet_sha256": packet["packet_sha256"],
            "target": {
                "request_id": "req-vrfy-001",
                "task_id": "task-vrfy-001",
                "claim_epoch": 1,
            },
            "reviewer": {
                "request_id": "rev-vrfy-001",
                "task_id": "rev-task-vrfy-001",
                "provider": "deepseek_vscode_lm",
            },
            "report": {
                "lens": "correctness",
                "read_only": True,
                "can_mutate_repo": False,
                "findings": [],
            },
            "authority": {
                "process_identity_verified": True,
                "audit_verified": True,
                "terminal_state": "review_ready",
            },
        }
        result = quality_reviewer.verify_reviewer_receipt(
            receipt,
            packet=packet,
            expected_reviewer_request_id="rev-vrfy-001",
            expected_reviewer_task_id="rev-task-vrfy-001",
            observed_provider="deepseek_vscode_lm",
            observed_terminal_state="review_ready",
            audit_verified=True,
        )
        assert result["schema_id"] == quality_reviewer.RECEIPT_SCHEMA_ID
        assert result["packet_sha256"] == packet["packet_sha256"]

    def test_missing_schema_rejected(self) -> None:
        """A receipt without the canonical schema_id must be rejected —
        the protocol fails closed rather than accepting untagged JSON."""
        packet = quality_reviewer.build_review_packet(
            request_id="req-vrfy-002",
            task_id="task-vrfy-002",
            claim_epoch=1,
            worker_provider="deepseek_vscode_lm",
            changed_path_hashes={"src/module.py": "a" * 64},
        )
        receipt = {"report": {"lens": "correctness", "findings": []}}
        with pytest.raises(ReviewerEvidenceError, match="reviewer_receipt_schema_mismatch"):
            quality_reviewer.verify_reviewer_receipt(
                receipt,
                packet=packet,
                expected_reviewer_request_id="rev-vrfy-002",
                expected_reviewer_task_id="rev-task-vrfy-002",
                observed_provider="deepseek_vscode_lm",
                observed_terminal_state="review_ready",
                audit_verified=True,
            )

    def test_wrong_schema_rejected(self) -> None:
        """A receipt with a fabricated schema_id must be rejected."""
        packet = quality_reviewer.build_review_packet(
            request_id="req-vrfy-003",
            task_id="task-vrfy-003",
            claim_epoch=1,
            worker_provider="deepseek_vscode_lm",
            changed_path_hashes={"src/module.py": "a" * 64},
        )
        receipt = {"schema_id": "aiworkhub.wrong_schema.v1", "report": {}}
        with pytest.raises(ReviewerEvidenceError, match="reviewer_receipt_schema_mismatch"):
            quality_reviewer.verify_reviewer_receipt(
                receipt,
                packet=packet,
                expected_reviewer_request_id="rev-vrfy-003",
                expected_reviewer_task_id="rev-task-vrfy-003",
                observed_provider="deepseek_vscode_lm",
                observed_terminal_state="review_ready",
                audit_verified=True,
            )


class TestSchemaRepairTurn:
    """NF-2026-00667: the one bounded correction turn, and where it can reach.

    ``assemble_reviewer_prompt`` is the single prompt-assembly seam the reviewer
    launch path uses, and it is the ONLY thing every reviewer transport shares:
    the reviewer process is gone by ingest time, only ``prompt_sha256`` is
    retained, and no route can continue the previous conversation. So the ask
    rides the prompt of the next turn, which means the sighted CLI adapters and
    the blind in-process ``vscode_lm`` bridge get exactly the same repair.
    """

    # Cut verbatim from card_json.blocker_reason on
    # CLAUDESONNET5_QR_PONYTAIL_MINIMALITY_V1_E38_SECURITY_V7 in
    # .aiworkhub/tasking/task_queue.sqlite, read 2026-09-07.
    STORED_REJECTION = (
        "review_protocol:structured_report_invalid:"
        "review_finding_0_unknown_key:category,file,line"
    )

    def _blind_packet(self) -> dict:
        packet = _packet_with_findings()
        packet["candidate"]["padding"] = "x" * (100 * 1024)
        packet["packet_sha256"] = _canonical_digest(
            {k: v for k, v in packet.items() if k != "packet_sha256"}
        )
        return packet

    @pytest.mark.parametrize(
        "adapter_id,sighted", [("codex_cli", True), ("vscode_lm", False)]
    )
    def test_both_transports_carry_the_repair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        adapter_id: str, sighted: bool,
    ):
        monkeypatch.delenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, raising=False)
        packet = self._blind_packet()
        path = tmp_path / "packet.json"
        prompt = quality_review.assemble_reviewer_prompt(
            packet, lens="correctness", adapter_id=adapter_id,
            packet_path=str(path), packet_root=tmp_path,
            prior_rejection=self.STORED_REJECTION,
        )

        assert prompt.startswith("SCHEMA REPAIR TURN.")
        assert "review_finding_0_unknown_key:category,file,line" in prompt
        assert "must be dropped, not decorated" in prompt
        # The sighted route still uses the file transport, the blind route still
        # gets the packet inline: the repair changes neither.
        assert (quality_review.extract_inline_packet(prompt) == packet) is not sighted

    def test_the_repair_is_first_because_the_packet_is_long(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.delenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, raising=False)
        prompt = quality_review.assemble_reviewer_prompt(
            self._blind_packet(), lens="correctness", adapter_id="vscode_lm",
            packet_path=str(tmp_path / "packet.json"), packet_root=tmp_path,
            prior_rejection=self.STORED_REJECTION,
        )

        assert prompt.index("SCHEMA REPAIR TURN.") < prompt.index(
            "QUALITY_REVIEW_PACKET:"
        )

    @pytest.mark.parametrize(
        "prior_rejection",
        [
            "",
            "worker_failed:exit=1",
            # DEEPSEEKV4P_QR_NF625_M2_SECURITY_V1: the provider stream failed,
            # not the reviewer. Asking again spends a turn on a fault that will
            # not clear.
            "review_protocol:provider_events_oversized",
        ],
    )
    def test_an_ordinary_launch_is_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior_rejection: str,
    ):
        """A repair that fires on everything is not a repair, it is a preamble."""

        monkeypatch.delenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, raising=False)
        packet = self._blind_packet()
        baseline = quality_review.assemble_reviewer_prompt(
            packet, lens="correctness", adapter_id="vscode_lm",
            packet_path=str(tmp_path / "packet.json"), packet_root=tmp_path,
        )
        prompt = quality_review.assemble_reviewer_prompt(
            packet, lens="correctness", adapter_id="vscode_lm",
            packet_path=str(tmp_path / "packet.json"), packet_root=tmp_path,
            prior_rejection=prior_rejection,
        )

        assert prompt == baseline
        assert "SCHEMA REPAIR TURN." not in prompt

    def test_the_reviewers_own_prose_is_never_echoed_into_the_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """CLAUDESONNET5_QR_NF658_V6_SECURITY_V1's reason is 500 chars of prose."""

        monkeypatch.delenv(quality_reviewer.REVIEW_PACKET_FILE_ROOT_ENV, raising=False)
        prompt = quality_review.assemble_reviewer_prompt(
            self._blind_packet(), lens="correctness", adapter_id="vscode_lm",
            packet_path=str(tmp_path / "packet.json"), packet_root=tmp_path,
            prior_rejection=(
                "review_protocol:no_report_in_final:Reported one critical "
                "finding: the per-request audit-ledger HMAC key lives inside "
                "the sandboxed worker's own $HOME"
            ),
        )

        assert prompt.startswith("SCHEMA REPAIR TURN.")
        assert "Rejected as: no_report_in_final\n" in prompt
        assert "audit-ledger" not in prompt
        assert "$HOME" not in prompt

    def test_the_repair_never_tells_the_reviewer_what_to_conclude(self):
        for request in (
            quality_review_ingest.schema_repair_request(self.STORED_REJECTION),
            quality_review_ingest.schema_repair_request(
                "review_protocol:no_report_in_final:prose"
            ),
            quality_review_ingest.schema_repair_request(
                "review_protocol:multiple_structured_finals"
            ),
        ):
            assert request is not None
            instruction = str(request["instruction"]).lower()
            for forbidden in ("severity", "disposition", "defect", "approve", "accept"):
                assert forbidden not in instruction, request
