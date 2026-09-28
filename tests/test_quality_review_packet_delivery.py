from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from aiworkhub import process_launcher
from aiworkhub import quality_evidence as qe
from aiworkhub import quality_review as qr
from aiworkhub import quality_review_ingest
from aiworkhub import quality_reviewer, runtime_adapters
from aiworkhub import worker_ai_tools_mcp as worker_mcp

CANDIDATE_PATH = "src/aiworkhub/quality_review.py"
TASK_ID = "NF-2026-00259-PACKET-DELIVERY"

# The exact capability set given to a reviewer on the vscode_lm_in_process
# sandbox: a Source Graph query tool and a submit tool, and NO file-read tool.
BLIND_TOOLSET = frozenset(
    {
        "aiworkhub_worker_source_graph_query",
        "aiworkhub_worker_quality_review_submit",
    }
)
PACKET_READ_TOOLSET = BLIND_TOOLSET | {
    "aiworkhub_worker_quality_review_packet_read"
}


def _scoped_audit(lens: str, paths: list[str]) -> dict[str, object]:
    payload = {
        "task_id": TASK_ID,
        "review_lens": {"lens_kind": lens},
        "changed_paths": [{"path": path} for path in paths],
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_id": "aiworkhub.scoped_audit.v1",
        "fingerprint": fingerprint,
        "packet": payload,
    }


def _packet(lens: str = "correctness", **extra: object) -> dict[str, object]:
    digest = hashlib.sha256(b"candidate-bytes").hexdigest()
    return quality_reviewer.build_review_packet(
        request_id="req-1",
        task_id=TASK_ID,
        claim_epoch=1,
        worker_provider="claude",
        changed_path_hashes={CANDIDATE_PATH: digest},
        objective="deliver the packet as content",
        acceptance=["a blind reviewer can cite an exact path and line"],
        required_outputs=[CANDIDATE_PATH],
        validation=["python3 -m pytest -q"],
        scoped_audits={lens: _scoped_audit(lens, [CANDIDATE_PATH])},
        **extra,
    )


def _review_ctx(repo: Path, packet_path: Path | None) -> worker_mcp.WorkerToolContext:
    audit_ledger_path = repo / "review-audit.jsonl"
    audit_hmac_key_path = repo / "review-audit.key"
    audit_hmac_key_path.write_bytes(b"packet-read-test-key")
    return worker_mcp.WorkerToolContext(
        task_id="review-task",
        runner="codex",
        topic="quality-review",
        request_id="review-request",
        repo=repo,
        authority_repo=repo,
        source_graph_targets=(),
        session_topic="quality-review",
        audit_ledger_path=audit_ledger_path,
        audit_hmac_key_path=audit_hmac_key_path,
        quality_review_packet_path=packet_path,
    )


def _sealed_packet(tmp_path: Path) -> tuple[Path, dict[str, object], Path]:
    candidate = tmp_path / CANDIDATE_PATH
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"candidate-bytes")
    packet = _packet()
    packet_path = tmp_path / "sealed-review-packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    return packet_path, packet, candidate


def _canonical_packet_text(packet: dict[str, object]) -> str:
    """The exact bytes ``packet_sha256`` is the digest of, as text."""
    return json.dumps(
        {key: value for key, value in packet.items() if key != "packet_sha256"},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _read_every_part(ctx: worker_mcp.WorkerToolContext) -> list[dict]:
    """Part 0 -- the bare call a reviewer makes first -- then the rest, in order."""
    results = [worker_mcp.quality_review_packet_read(ctx)]
    for index in range(1, int(results[0]["part_count"])):
        results.append(worker_mcp.quality_review_packet_read(ctx, part=index))
    return results


def _paging_packet(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    """A sealed packet whose evidence holds a >200 KB multi-line string.

    This is the NF-2026-01029 shape: the newlines are escaped on the way into
    JSON, so the whole packet serializes as ONE line and a host that spills an
    oversized result leaves the reviewer a file no paged read can walk.
    """
    candidate = tmp_path / CANDIDATE_PATH
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"candidate-bytes")
    body = {key: value for key, value in _packet().items() if key != "packet_sha256"}
    section = dict(body["candidate"])  # type: ignore[arg-type]
    transcript = "".join(
        f"{index:06d} | evidence line with enough width to be worth paging\n"
        for index in range(3500)
    )
    assert len(transcript) > 200 * 1024
    section["source_evidence"] = [
        {"path": CANDIDATE_PATH, "excerpt": transcript, "truncated": False}
    ]
    body["candidate"] = section
    encoded = json.dumps(
        body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    packet = {
        **body,
        "packet_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }
    packet_path = tmp_path / "paged-review-packet.json"
    packet_path.write_text(
        json.dumps(packet, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    assert packet_path.stat().st_size <= worker_mcp.MAX_QUALITY_REVIEW_PACKET_BYTES
    return packet_path, packet


def test_reviewer_packet_read_returns_exact_bound_packet(tmp_path: Path) -> None:
    packet_path, packet, _candidate = _sealed_packet(tmp_path)
    ctx = _review_ctx(tmp_path, packet_path)
    result = worker_mcp.quality_review_packet_read(ctx)
    assert result == {
        "ok": True,
        "tool": "quality_review_packet_read",
        "packet_sha256": packet["packet_sha256"],
        "part": 0,
        "part_count": 1,
        "text": _canonical_packet_text(packet),
    }
    assert ctx.audit_ledger_path is not None
    assert ctx.audit_hmac_key_path is not None
    verification = worker_mcp.verify_audit_ledger(
        ctx.audit_ledger_path,
        ctx.audit_hmac_key_path,
        task_id=ctx.task_id,
        runner=ctx.runner,
        topic=ctx.topic,
        request_id=ctx.request_id,
    )
    assert verification["ok"] is True
    assert verification["entries_tampered"] == 0
    assert verification["call_count_by_tool"]["quality_review_packet_read"] == 1
    assert (
        verification["successful_call_count_by_tool"]["quality_review_packet_read"]
        == 1
    )
    entry = json.loads(ctx.audit_ledger_path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["authority_source"] == "candidate_packet"
    assert entry["authority_state"] == "quality_review_readonly"
    target = packet["target"]
    assert isinstance(target, dict)
    assert entry["payload"] == {
        "packet_sha256": packet["packet_sha256"],
        "target_request_id": target["request_id"],
        "target_task_id": target["task_id"],
        "part": 0,
        "part_count": 1,
    }


def test_a_small_packet_is_one_part_of_unchanged_packet_text(tmp_path: Path) -> None:
    """Paging must not chop, re-order or re-encode a packet that already fits."""
    packet_path, packet, _candidate = _sealed_packet(tmp_path)
    result = worker_mcp.quality_review_packet_read(_review_ctx(tmp_path, packet_path))
    assert result["part_count"] == 1
    assert result["part"] == 0
    assert result["text"] == _canonical_packet_text(packet)
    assert (
        hashlib.sha256(str(result["text"]).encode("utf-8")).hexdigest()
        == packet["packet_sha256"]
    )


def test_a_packet_the_host_would_spill_is_paged_under_the_part_cap(
    tmp_path: Path,
) -> None:
    """NF-2026-01029. 8 minutes were lost to an unpageable 26.6k-token line.

    Every part has to be small enough that the host never spills it, and the
    parts have to rejoin to exactly the bytes the digest authenticates -- a
    bound that only holds if each character is charged its ESCAPED width, which
    is what >200 KB of escaped newlines is here to prove.
    """
    packet_path, packet = _paging_packet(tmp_path)
    ctx = _review_ctx(tmp_path, packet_path)
    results = _read_every_part(ctx)

    assert len(results) > 1
    assert [row["part"] for row in results] == list(range(len(results)))
    assert {row["part_count"] for row in results} == {len(results)}
    assert {row["packet_sha256"] for row in results} == {packet["packet_sha256"]}
    for row in results:
        serialized = len(json.dumps(row).encode("utf-8"))
        assert serialized <= worker_mcp.QUALITY_REVIEW_PACKET_PART_BYTES

    joined = "".join(str(row["text"]) for row in results)
    assert hashlib.sha256(joined.encode("utf-8")).hexdigest() == packet["packet_sha256"]
    assert json.loads(joined) == {
        key: value for key, value in packet.items() if key != "packet_sha256"
    }

    assert ctx.audit_ledger_path is not None
    assert ctx.audit_hmac_key_path is not None
    verification = worker_mcp.verify_audit_ledger(
        ctx.audit_ledger_path,
        ctx.audit_hmac_key_path,
        task_id=ctx.task_id,
        runner=ctx.runner,
        topic=ctx.topic,
        request_id=ctx.request_id,
    )
    assert verification["ok"] is True
    assert verification["entries_tampered"] == 0
    assert (
        verification["successful_call_count_by_tool"]["quality_review_packet_read"]
        == len(results)
    )
    payloads = [
        json.loads(line)["payload"]
        for line in ctx.audit_ledger_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["part"] for row in payloads] == list(range(len(results)))
    assert {row["part_count"] for row in payloads} == {len(results)}


# ``part`` is the ONLY argument, so the typed refusal is the whole guard: a
# small packet has exactly one part, which makes 1 the first out-of-range index.
@pytest.mark.parametrize("part", [-1, 1, "0", 1.5, True])
def test_an_invalid_part_is_a_typed_violation(tmp_path: Path, part: object) -> None:
    packet_path, _packet_body, _candidate = _sealed_packet(tmp_path)
    result = worker_mcp.quality_review_packet_read(
        _review_ctx(tmp_path, packet_path), part=part  # type: ignore[arg-type]
    )
    assert result["ok"] is False
    assert result["reason"] == "quality_review_packet_part_invalid"


def test_reviewer_packet_read_is_inert_without_bound_packet(tmp_path: Path) -> None:
    result = worker_mcp.quality_review_packet_read(_review_ctx(tmp_path, None))
    assert result["ok"] is False
    assert result["reason"] == "quality_review_packet_not_bound"


@pytest.mark.parametrize("failure", ["malformed", "oversized", "digest", "changed_path"])
def test_reviewer_packet_read_fails_closed(
    tmp_path: Path, failure: str
) -> None:
    packet_path, packet, candidate = _sealed_packet(tmp_path)
    if failure == "malformed":
        packet_path.write_text("{", encoding="utf-8")
    elif failure == "oversized":
        packet_path.write_bytes(b"x" * (worker_mcp.MAX_QUALITY_REVIEW_PACKET_BYTES + 1))
    elif failure == "digest":
        packet["packet_sha256"] = "0" * 64
        packet_path.write_text(json.dumps(packet), encoding="utf-8")
    else:
        candidate.write_bytes(b"changed")
    result = worker_mcp.quality_review_packet_read(
        _review_ctx(tmp_path, packet_path)
    )
    assert result["ok"] is False


def test_reviewer_packet_read_rejects_symlink(tmp_path: Path) -> None:
    packet_path, _packet_body, _candidate = _sealed_packet(tmp_path)
    link = tmp_path / "packet-link.json"
    try:
        os.symlink(packet_path, link)
    except OSError:
        pytest.skip("symlinks unavailable")
    result = worker_mcp.quality_review_packet_read(_review_ctx(tmp_path, link))
    assert result["ok"] is False
    assert result["reason"] == "quality_review_packet_invalid"


def test_file_transport_prompt_names_the_paged_packet_tool(tmp_path: Path) -> None:
    """NF-2026-01029: the prompt has to name the paging, or nobody pages."""
    packet = _packet()
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    prompt = quality_reviewer.build_review_prompt(
        packet,
        lens="correctness",
        packet_file=str(packet_path),
        packet_root=tmp_path,
        max_inline_bytes=0,
    )
    assert "aiworkhub_worker_quality_review_packet_read" in prompt
    assert "part=0" in prompt
    assert "part_count-1" in prompt
    # Paging adds an index argument and nothing else: the tool still refuses to
    # be pointed at a file or an identity, and the prompt still says so.
    assert "Do not supply a path or identity" in prompt


def test_sighted_bound_packet_stays_file_ref_just_below_inline_cap(
    tmp_path: Path,
) -> None:
    cap = 96 * 1024
    body = {k: v for k, v in _packet().items() if k != "packet_sha256"}
    candidate = dict(body["candidate"])
    body = {**body, "candidate": candidate}

    def sealed() -> dict[str, object]:
        digest = hashlib.sha256(
            json.dumps(
                body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        ).hexdigest()
        return {**body, "packet_sha256": digest}

    def encoded_len(payload: dict[str, object]) -> int:
        return len(
            json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        )

    candidate["padding"] = "x" * (90 * 1024)
    packet = sealed()
    extra = cap - 1 - encoded_len(packet)
    assert extra > 0
    candidate["padding"] = str(candidate["padding"]) + ("x" * extra)
    packet = sealed()
    assert encoded_len(packet) == cap - 1

    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    packet_path = runtime_root / "quality_review_packet.json"
    candidate = tmp_path / CANDIDATE_PATH
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"candidate-bytes")

    blind = qr.assemble_reviewer_prompt(
        packet,
        lens="correctness",
        adapter_id="vscode_lm",
        packet_path=str(packet_path),
        packet_root=runtime_root,
    )
    assert qr.extract_inline_packet(blind) == packet
    assert "QUALITY_REVIEW_PACKET_FILE:" not in blind
    assert not packet_path.exists()

    sighted = qr.assemble_reviewer_prompt(
        packet,
        lens="correctness",
        adapter_id="claude_cli",
        packet_path=str(packet_path),
        packet_root=runtime_root,
    )
    assert "QUALITY_REVIEW_PACKET_FILE:" in sighted
    assert f"PACKET_SHA256: {packet['packet_sha256']}" in sighted
    assert "QUALITY_REVIEW_PACKET:" not in sighted
    assert qr.extract_inline_packet(sighted) is None
    assert len(sighted.encode("utf-8")) < len(blind.encode("utf-8"))
    assert json.loads(packet_path.read_text(encoding="utf-8")) == packet

    ctx = _review_ctx(tmp_path, packet_path)
    results = _read_every_part(ctx)
    canonical = _canonical_packet_text(packet)
    assert results[0] == {
        "ok": True,
        "tool": "quality_review_packet_read",
        "packet_sha256": packet["packet_sha256"],
        "part": 0,
        "part_count": len(results),
        "text": canonical[: len(str(results[0]["text"]))],
    }
    assert "".join(str(row["text"]) for row in results) == canonical
    assert ctx.audit_ledger_path is not None
    assert ctx.audit_hmac_key_path is not None
    verification = worker_mcp.verify_audit_ledger(
        ctx.audit_ledger_path,
        ctx.audit_hmac_key_path,
        task_id=ctx.task_id,
        runner=ctx.runner,
        topic=ctx.topic,
        request_id=ctx.request_id,
    )
    assert verification["ok"] is True
    assert verification["entries_tampered"] == 0
    # One authenticated read per part, and every one of them succeeded: paging
    # splits the delivery, never the verification or the audit behind it.
    reads = len(results)
    assert verification["call_count_by_tool"]["quality_review_packet_read"] == reads
    assert (
        verification["successful_call_count_by_tool"]["quality_review_packet_read"]
        == reads
    )


def test_legacy_blind_toolset_remains_unchanged() -> None:
    assert "aiworkhub_worker_quality_review_packet_read" not in BLIND_TOOLSET
    assert "aiworkhub_worker_quality_review_packet_read" in PACKET_READ_TOOLSET


# --- Acceptance 1: content delivered, packet_sha256 unchanged ----------------


def test_deliver_packet_content_preserves_sha256_and_needs_no_file_read() -> None:
    packet = _packet()
    delivery = qr.deliver_packet_content(
        packet, packet_path="/runtime/quality_review_packet.json"
    )
    assert delivery["requires_file_read"] is False
    # packet_sha256 is the same contract it is today.
    assert delivery["packet_sha256"] == packet["packet_sha256"]
    # The delivered content round-trips to the identical packet, digest and all.
    reconstructed = json.loads(delivery["packet_content"])
    assert reconstructed == packet
    assert reconstructed["packet_sha256"] == packet["packet_sha256"]
    # The path survives only as a convenience.
    assert delivery["packet_path"] == "/runtime/quality_review_packet.json"


def test_deliver_packet_content_rejects_tampered_digest() -> None:
    packet = _packet()
    packet["packet_sha256"] = "0" * 64
    with pytest.raises(quality_reviewer.ReviewerEvidenceError):
        qr.deliver_packet_content(packet)


def test_prompt_embeds_content_and_never_requires_a_file_read() -> None:
    packet = _packet()
    prompt = qr.build_reviewer_prompt_with_content(
        packet, lens="correctness", reviewer_tool_names=BLIND_TOOLSET
    )
    assert qr._INLINE_PACKET_PREFIX in prompt
    assert packet["packet_sha256"] in prompt
    # No forced path read for a blind reviewer.
    assert "Read the packet file first" not in prompt
    assert qr._CONVENIENCE_PREFIX not in prompt


def test_prompt_keeps_path_convenience_only_for_sighted_reviewers() -> None:
    packet = _packet()
    sighted = qr.build_reviewer_prompt_with_content(
        packet,
        lens="correctness",
        reviewer_tool_names={"Read", "aiworkhub_worker_quality_review_submit"},
        packet_path="/runtime/quality_review_packet.json",
    )
    assert qr._CONVENIENCE_PREFIX in sighted
    assert "authoritative and requires no file read" in sighted
    blind = qr.build_reviewer_prompt_with_content(
        packet,
        lens="correctness",
        reviewer_tool_names=BLIND_TOOLSET,
        packet_path="/runtime/quality_review_packet.json",
    )
    assert qr._CONVENIENCE_PREFIX not in blind


# --- Acceptance 2: a blind reviewer cites an exact packet-permitted path/line -


def test_blind_reviewer_can_cite_exact_path_and_line_from_content() -> None:
    packet = _packet()
    # This is the exact capability set of a vscode_lm_in_process reviewer.
    assert qr.capability_set_has_file_read(BLIND_TOOLSET) is False

    prompt = qr.build_reviewer_prompt_with_content(
        packet, lens="correctness", reviewer_tool_names=BLIND_TOOLSET
    )
    # The reviewer reaches the packet with NO file-read tool: it reads the
    # content straight out of its own prompt.
    inline = qr.extract_inline_packet(prompt)
    assert inline is not None
    permitted = [
        row["path"] for row in inline["candidate"]["changed_paths"]
    ]
    assert CANDIDATE_PATH in permitted

    # It then produces a defect finding citing that exact packet-permitted path
    # and line, and the canonical normalizer accepts it.
    finding = {
        "severity": "medium",
        "disposition": "defect",
        "summary": "capability set omits a file-read tool",
        "evidence": f"{CANDIDATE_PATH}:42 delivers the packet as content",
        "path": CANDIDATE_PATH,
        "line_start": 42,
        "line_end": 42,
    }
    normalized = quality_reviewer.normalize_packet_findings(
        packet, lens="correctness", findings=[finding]
    )
    assert normalized[0]["evidence_reference"] == {
        "kind": "source",
        "path": CANDIDATE_PATH,
        "line_start": 42,
        "line_end": 42,
    }
    assert normalized[0]["actionable"] is True


def test_rm44_canonical_second_ingress_reaches_durable_submit_boundary() -> None:
    packet = _packet()
    raw_finding = {
        "severity": "medium",
        "disposition": "defect",
        "summary": "capability set omits a file-read tool",
        "evidence": f"{CANDIDATE_PATH}:42 delivers the packet as content",
        "path": CANDIDATE_PATH,
        "line_start": 42,
        "line_end": 42,
    }
    submitted: list[dict[str, object]] = []

    def normalize(report: dict[str, object]) -> dict[str, object]:
        return {
            "lens": "correctness",
            "findings": quality_reviewer.normalize_packet_findings(
                packet,
                lens="correctness",
                findings=report.get("findings") or [],
            ),
        }

    def durable_submit(report: dict[str, object]) -> None:
        submitted.append(normalize(report))

    provider_report = {"lens": "correctness", "findings": [raw_finding]}
    event = json.dumps({"type": "result", "result": json.dumps(provider_report)})
    result = quality_review_ingest.ingest_structured_final(
        [event],
        expected_lens="correctness",
        normalize=normalize,
        submit=durable_submit,
    )

    assert result.status == "submitted"
    assert result.submitted is True
    assert submitted == [result.report]
    assert submitted[0]["findings"][0]["actionable"] is True
    assert submitted[0]["findings"][0]["evidence_reference"] == {
        "kind": "source",
        "path": CANDIDATE_PATH,
        "line_start": 42,
        "line_end": 42,
    }


def test_capability_set_and_adapter_file_read_classification() -> None:
    assert qr.capability_set_has_file_read({"Read"}) is True
    assert qr.capability_set_has_file_read({"aiworkhub_worker_file_read"}) is True
    assert qr.capability_set_has_file_read(BLIND_TOOLSET) is False

    assert runtime_adapters.adapter_provides_file_read("claude_cli") is True
    assert runtime_adapters.adapter_provides_file_read("glm_vscode_lm") is False
    # A Copilot CLI that fell back to the in-process bridge is blind too.
    assert (
        runtime_adapters.adapter_provides_file_read(
            "glm_copilot_cli", adapter_fallback_used=True
        )
        is False
    )
    assert runtime_adapters.adapter_provides_file_read("glm_copilot_cli") is True


# --- Acceptance 3: not forced to submit after a single zero-hit query ---------


def test_single_zero_hit_query_does_not_force_submission() -> None:
    # One zero-hit orientation query: the reviewer may keep inspecting.
    assert qr.reviewer_submit_forced(1, prior_hit_total=0) is False
    assert qr.reviewer_may_query_source_graph_again(1, prior_hit_total=0) is True
    # The inspection phase still terminates.
    assert (
        qr.reviewer_submit_forced(
            qr.REVIEWER_MAX_INSPECTION_QUERIES, prior_hit_total=0
        )
        is True
    )
    # A reviewer that has already found evidence past the minimum is not stuck.
    assert (
        qr.reviewer_may_query_source_graph_again(
            qr.REVIEWER_MIN_INSPECTION_QUERIES, prior_hit_total=3
        )
        is False
    )


# --- Acceptance 4: reviewer worktree indexing / parent-repo scoping ----------


def test_zero_row_worktree_with_baseline_scopes_to_parent_repository() -> None:
    baseline = [{"path": f"src/pkg/mod_{i}.py", "sha256": "x"} for i in range(6)]
    scope = qr.choose_reviewer_source_graph_scope(
        worktree_evidence={"entity_rows": 0, "edge_rows": 0, "file_rows": 0},
        workspace_baseline=baseline,
        parent_repo="/repo",
    )
    assert scope["scope"] == "parent_repository"
    assert scope["parent_repo"] == "/repo"
    assert scope["why"]


def test_indexed_worktree_uses_its_own_scope() -> None:
    scope = qr.choose_reviewer_source_graph_scope(
        worktree_evidence={"entity_rows": 5, "edge_rows": 4, "file_rows": 1},
        workspace_baseline=[{"path": "src/pkg/mod.py", "sha256": "x"}],
        parent_repo="/repo",
    )
    assert scope["scope"] == "reviewer_worktree"
    assert scope["why"]


# --- Acceptance 5: refuse when no independent sighted reviewer exists ---------


def _fleet() -> list[dict[str, object]]:
    return [
        {"provider": "glm", "adapter_id": "glm_vscode_lm", "availability": "available"},
        {
            "provider": "gpt",
            "adapter_id": "glm_copilot_cli",
            "availability": "available",
            "adapter_fallback_used": True,
        },
        {
            "provider": "deepseek",
            "adapter_id": "deepseek_copilot_cli",
            "availability": "provider_refused",
        },
    ]


def test_all_blind_or_refused_reviewers_refuse_with_per_provider_reasons() -> None:
    result = qr.assess_reviewer_availability(
        worker_provider="claude",
        candidates=_fleet(),
        packet_delivered_as_content=False,
    )
    assert result["can_launch"] is False
    assert result["refusal_reason"] == qr.REFUSAL_NO_INDEPENDENT_SIGHTED
    assert result["reasons_by_provider"]["glm"] == qr.REASON_BLIND_NO_FILE_READ
    assert result["reasons_by_provider"]["gpt"] == qr.REASON_BLIND_NO_FILE_READ
    assert result["reasons_by_provider"]["deepseek"] == qr.REASON_PROVIDER_REFUSED


def test_content_delivery_makes_blind_adapters_viable_reviewers() -> None:
    result = qr.assess_reviewer_availability(
        worker_provider="claude",
        candidates=_fleet(),
        packet_delivered_as_content=True,
    )
    assert result["can_launch"] is True
    assert "glm" in result["viable_reviewers"]
    assert "gpt" in result["viable_reviewers"]
    # A paid-out provider stays unusable even with content delivery.
    assert result["reasons_by_provider"]["deepseek"] == qr.REASON_PROVIDER_REFUSED


def test_single_provider_review_is_accepted_via_the_ladder() -> None:
    # RENAMED from test_same_provider_review_is_never_accepted.
    #
    # BEFORE: this asserted a reviewer sharing the worker's provider was never
    # usable -- can_launch False with reasons_by_provider["glm"] ==
    # REASON_SAME_PROVIDER.  That vendor check contradicted the independence
    # ladder (which never refuses on provider identity) and left a
    # single-provider installation -- a user with only Claude, or only Codex --
    # unable to complete any review at all.
    #
    # WHY THE VENDOR CHECK WAS REMOVED: what makes a review independent is the
    # anti-anchored packet, the sealed candidate, the separate read-only process
    # and the authenticated packet_sha256 submission -- all of which hold on
    # every rung.  Vendor identity was never one of them.  A same-provider
    # reviewer is recorded at the same_model_fresh_context rung and completes.
    #
    # WHICH TESTS NOW CARRY THE PROTECTION THIS ONE USED TO PROVIDE:
    #   * test_review_independence_ladder::test_anti_anchoring_holds_on_every_rung
    #     and ::test_packet_sha256_is_unchanged_and_tamper_is_rejected keep the
    #     reviewer from ever receiving the worker's rationale/verdict/answer and
    #     bind the packet_sha256 contract on every rung;
    #   * test_process_limit_only_report_still_fails_its_lens (below) keeps an
    #     uninspected (process_limit-only) review from being accepted;
    #   * test_review_independence_ladder::
    #     test_single_provider_installation_reaches_acceptance records the rung a
    #     single-provider review runs at.

    # Exactly one provider is offered, and it shares the worker's provider.
    # Content delivery makes it sighted, so it is a viable reviewer and the
    # launch proceeds instead of refusing.
    only_provider = qr.assess_reviewer_availability(
        worker_provider="glm",
        candidates=[
            {"provider": "glm", "adapter_id": "glm_vscode_lm", "availability": "available"},
        ],
        packet_delivered_as_content=True,
    )
    assert only_provider["can_launch"] is True
    assert only_provider["viable_reviewers"] == ["glm"]
    assert only_provider["refusal_reason"] is None
    assert only_provider["reasons_by_provider"]["glm"] == qr.REASON_AVAILABLE

    # A same-provider reviewer alongside an unusable one still launches on the
    # same-provider reviewer; the unusable provider keeps its true reason.
    result = qr.assess_reviewer_availability(
        worker_provider="glm",
        candidates=[
            {"provider": "glm", "adapter_id": "glm_vscode_lm", "availability": "available"},
            {
                "provider": "deepseek",
                "adapter_id": "deepseek_copilot_cli",
                "availability": "quota_unobserved",
            },
        ],
        packet_delivered_as_content=True,
    )
    assert result["can_launch"] is True
    assert "glm" in result["viable_reviewers"]
    assert result["reasons_by_provider"]["glm"] == qr.REASON_AVAILABLE
    assert result["reasons_by_provider"]["deepseek"] == qr.REASON_QUOTA_UNOBSERVED


# --- Forbidden guard: the 0.9.74 blind-reviewer gate is not weakened ---------


def test_process_limit_only_report_still_fails_its_lens() -> None:
    # Even after content delivery un-blinds adapters, a report that inspected
    # nothing (all findings process_limit) must still fail its lens.
    report = {
        "lens": qe.LENS_CORRECTNESS,
        "provider": "glm",
        "read_only": True,
        "can_mutate_repo": False,
        "findings": [
            {
                "id": "pl-1",
                "severity": qe.SEVERITY_LOW,
                "disposition": qe.FINDING_DISPOSITION_PROCESS_LIMIT,
                "summary": "reviewer could not inspect the packet",
                "evidence": "no file-read tool was available for the packet path",
            }
        ],
    }
    verdict = qe.fold_quality_verdict(
        [],
        risk_profile=qe.resolve_risk_profile(qe.RISK_MEDIUM),
        reviewer_reports=[report],
        combined_tree_checks=[
            qe.EvidenceCheck(
                check_id="union-tests", kind="test", status=qe.STATUS_PASSED, summary=""
            )
        ],
        worker_provider="claude",
    )
    assert verdict["passed"] is False
    assert "reviewer_could_not_inspect:correctness" in verdict["blocking_evidence"]


# --- NF-2026-00600: category survives BOTH normalization boundaries ----------


def test_packet_and_reviewer_normalization_preserve_category_end_to_end() -> None:
    # The reviewer boundary stamps the canonical category on a defect finding...
    packet = _packet()
    packet_finding = quality_reviewer.normalize_packet_findings(
        packet,
        lens="correctness",
        findings=[
            {
                "severity": "medium",
                "disposition": "defect",
                "summary": "capability set omits a file-read tool",
                "evidence": f"{CANDIDATE_PATH}:42 delivers the packet as content",
                "path": CANDIDATE_PATH,
                "line_start": 42,
                "line_end": 42,
            }
        ],
    )[0]
    assert packet_finding["category"] == "general"

    # ...and the evidence-normalization boundary the launcher trusts must carry
    # that category through rather than dropping it (the NF-2026-00600 defect).
    report = {
        "lens": qe.LENS_CORRECTNESS,
        "provider": "glm",
        "read_only": True,
        "can_mutate_repo": False,
        "findings": [packet_finding],
    }
    normalized, errors = qe.normalize_reviewer_reports([report])

    assert errors == []
    reviewed = normalized[0]["findings"][0]
    assert reviewed["category"] == "general"
    assert reviewed["actionable"] is True
    assert (
        quality_reviewer.QUALITY_REVIEW_FINDING_REQUIRED_KEYS
        <= set(reviewed)
        <= quality_reviewer.QUALITY_REVIEW_FINDING_KEYS
    )


# --- NF-2026-01086: the manager's rework amendment reaches both transports ----

REWORK_FEEDBACK = {
    "schema_id": "aiworkhub.rework_feedback_delta.v1",
    "instruction": "keep the junction guard",
    "reason_identity": "reason-1",
    "predecessor_request_id": "r0",
    "predecessor_changed_paths": [CANDIDATE_PATH],
    "residual_identities": [],
}
PRECEDENCE_LINE = quality_reviewer.MANAGER_AMENDMENT_PROMPT_LINE
LENSES = ("correctness", "security", "code_quality")


def _assemble(adapter_id: str, packet: dict[str, object], runtime_root: Path) -> str:
    return qr.assemble_reviewer_prompt(
        packet,
        lens="correctness",
        adapter_id=adapter_id,
        packet_path=str(runtime_root / "quality_review_packet.json"),
        packet_root=runtime_root,
    )


def test_sighted_prompt_differs_by_exactly_the_manager_amendment_line(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    plain = _packet()
    amended = _packet(manager_amendment=REWORK_FEEDBACK)

    plain_prompt = _assemble("claude_cli", plain, runtime_root)
    amended_prompt = _assemble("claude_cli", amended, runtime_root)

    assert PRECEDENCE_LINE not in plain_prompt
    assert "manager_amendment" not in plain_prompt
    assert amended_prompt.count(PRECEDENCE_LINE) == 1
    # File transport: the prompt only names the sealed file, so the section adds
    # one instruction line and re-points the digest, and nothing else.
    assert amended_prompt.replace(PRECEDENCE_LINE, "", 1).replace(
        str(amended["packet_sha256"]), str(plain["packet_sha256"])
    ) == plain_prompt
    sealed = json.loads(
        (runtime_root / "quality_review_packet.json").read_text(encoding="utf-8")
    )
    assert sealed == amended
    assert sealed["manager_amendment"]["instruction"] == "keep the junction guard"


def test_blind_prompt_delivers_the_amendment_inline_after_the_precedence_line(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    amended = _packet(manager_amendment=REWORK_FEEDBACK)

    prompt = _assemble("vscode_lm", amended, runtime_root)
    plain_prompt = _assemble("vscode_lm", _packet(), runtime_root)

    assert prompt.count(PRECEDENCE_LINE) == 1
    # Instruction text ahead of the inline packet, not a copy inside it.
    assert prompt.index(PRECEDENCE_LINE) < prompt.index("QUALITY_REVIEW_PACKET:")
    inline = qr.extract_inline_packet(prompt)
    assert inline == amended
    assert (
        inline["manager_amendment"]["notice"]
        == quality_reviewer.MANAGER_AMENDMENT_NOTICE
    )
    assert PRECEDENCE_LINE not in plain_prompt
    assert "manager_amendment" not in plain_prompt


@pytest.mark.parametrize(
    "tools", [BLIND_TOOLSET, {"Read", "aiworkhub_worker_quality_review_submit"}]
)
def test_content_prompt_builder_adds_the_precedence_line_for_any_toolset(tools) -> None:
    def prompt_for(packet: dict[str, object]) -> str:
        return qr.build_reviewer_prompt_with_content(
            packet,
            lens="correctness",
            reviewer_tool_names=tools,
            packet_path="/runtime/quality_review_packet.json",
        )

    assert prompt_for(_packet(manager_amendment=REWORK_FEEDBACK)).count(
        PRECEDENCE_LINE
    ) == 1
    assert PRECEDENCE_LINE not in prompt_for(_packet())


def _launched_review_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **card_extra: object
) -> dict[str, object]:
    """Seal the packet ``ProcessManager._build_quality_review_packet`` builds.

    The seam of ``test_process_launcher``'s
    ``test_quality_review_packet_binding_carries_explicit_target_inputs`` -- a
    review_ready target card, its retained workspace and its review_ready
    event -- but with the real packet builder in place of a kwargs spy.
    """
    from test_process_launcher import _card, _manager, _show

    monkeypatch.setattr(
        process_launcher.storage_retention,
        "schedule_repository_cleanup",
        lambda *_args, **_kwargs: None,
    )
    request_id = "e" * 32
    manager = _manager(
        tmp_path,
        show_task=lambda _task_id: {"returncode": 1, "stdout": "", "stderr": ""},
        argv=[sys.executable, "-c", "pass"],
    )
    workspace_path = tmp_path / "worktrees" / request_id / "worktree"
    home = tmp_path / "worktrees" / request_id / "home"
    (workspace_path / "src").mkdir(parents=True)
    home.mkdir(parents=True)
    source = "value = 2\n"
    (workspace_path / "src" / "changed.py").write_bytes(source.encode("utf-8"))
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    workspace = process_launcher.WorkerWorkspace(
        request_id=request_id,
        repo=manager.repo,
        path=workspace_path,
        home=home,
        allowed_writes=("src/changed.py",),
        parent_baseline={},
        workspace_baseline={},
    )
    card = _card(task_id=TASK_ID, state="review")
    card.update(
        {
            "claim_epoch": 1,
            "terminal_substatus": "review_ready",
            "allowed_writes": ["src/changed.py"],
            "objective": "keep the guard",
            "acceptance": ["the original acceptance"],
            "terminal_review": {
                "substatus": "review_ready",
                "evidence": {
                    "workspace": workspace.as_metadata(),
                    "changed_path_hashes": {"src/changed.py": digest},
                    "quality_gate": {"checks": []},
                    "validation": [],
                },
            },
            **card_extra,
        }
    )
    manager._show_task = _show(lambda: card)
    manager._append_event(
        {
            "request_id": request_id,
            "task_id": TASK_ID,
            "runner": "worker",
            "topic": "code",
            "adapter_id": "worker_adapter",
            "state": "review_ready",
        }
    )
    monkeypatch.setenv("AIWORKHUB_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.setattr(
        manager,
        "_quality_review_source_evidence",
        lambda *_args, **_kwargs: {
            "src/changed.py": {
                "candidate_sha256": digest,
                "excerpt": source,
                "excerpt_bytes": len(source),
                "source_bytes": len(source),
                "truncated": False,
            }
        },
    )
    monkeypatch.setattr(
        process_launcher.quality_review_scope,
        "build_scoped_audits",
        lambda **_kwargs: {
            lens: _scoped_audit(lens, ["src/changed.py"]) for lens in LENSES
        },
    )

    result = manager._build_quality_review_packet(request_id, TASK_ID)

    assert result["ok"] is True, result
    return result["prepared"]["packet"]


def test_the_launch_path_seals_the_target_cards_review_feedback_into_the_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = _launched_review_packet(
        tmp_path, monkeypatch, review_feedback=dict(REWORK_FEEDBACK)
    )

    amendment = packet["manager_amendment"]
    assert amendment["instruction"] == "keep the junction guard"
    assert amendment["predecessor_request_id"] == "r0"
    assert amendment["notice"] == quality_reviewer.MANAGER_AMENDMENT_NOTICE
    # The launcher re-seals after binding immutable inputs: the seal must still
    # cover the section, and every lens packet must carry it on unchanged.
    body = {key: value for key, value in packet.items() if key != "packet_sha256"}
    assert packet["packet_sha256"] == quality_reviewer._canonical_digest(body)
    for lens in LENSES:
        lens_packet = quality_reviewer.build_lens_packet(packet, lens=lens)
        assert lens_packet["manager_amendment"] == amendment


def test_a_first_attempt_card_yields_a_packet_with_no_manager_amendment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = _launched_review_packet(tmp_path, monkeypatch)

    assert "manager_amendment" not in packet
