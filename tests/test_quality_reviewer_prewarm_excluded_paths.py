"""Reviewer Source Graph prewarm tolerates deliberately-excluded paths.

A card that changes a file Source Graph *deliberately excludes* -- an ``eval/``
artifact, a generated fixture, a file whose extension has no language
representation -- used to be permanently unreviewable: the reviewer candidate
prewarm walked every changed path and RAISED when one was excluded, so the
reviewer process never launched and the work could never be accepted.

Prewarm is only an optimisation that makes the reviewer's Source Graph queries
fast; the sealed review packet already carries the candidate content, so a
reviewer never needs an excluded file indexed.  These tests pin the fix:

  * a prewarm whose changed paths include a Source-Graph-excluded file skips
    that path, records the skip and its reason, and the reviewer still launches;
  * a prewarm whose changed paths are all excluded still launches and records
    that it worked from the sealed packet alone;
  * a genuine indexing failure on a file that SHOULD be indexable (a hash
    mismatch against the sealed packet, an unreadable file, a clone/backup I/O
    error) still refuses loudly and is never swallowed.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from aiworkhub import process_launcher, quality_review, worker_ai_tools_mcp

# The real ``_launch_isolated`` reviewer-ordering scaffold lives next door; reuse
# it so this file exercises the actual launch path rather than a reimplementation.
from test_process_launcher import _reviewer_launch_setup


# ---------------------------------------------------------------------------
# Pure classification: which SourceGraphError conditions are tolerated.
# ---------------------------------------------------------------------------

# The wrapper ``prewarm_quality_review_source_graph`` builds around a
# ``SourceGraphError`` (see worker_ai_tools_mcp): prefix + type name + str(exc).
def _wrapped(condition_and_detail: str) -> str:
    return (
        "quality_review_candidate_source_graph_prewarm_error:"
        "SourceGraphError:" + condition_and_detail
    )


@pytest.mark.parametrize(
    "detail",
    [
        "source_graph_single_file_excluded_glob:eval/activation_b821_v1.json",
        "source_graph_single_file_excluded_dir:node_modules",
        "source_graph_single_file_unsupported_extension:.csv",
    ],
)
def test_deliberate_exclusion_conditions_are_tolerated(detail):
    verdict = quality_review.classify_prewarm_error(_wrapped(detail))
    assert verdict["tolerated"] is True
    assert verdict["condition"] == detail.split(":", 1)[0]
    # The recorded reason must show an auditor it was a deliberate skip and that
    # the reviewer proceeds on the sealed packet alone.
    reason = verdict["reason"]
    assert "reviewer_source_graph_prewarm_skipped_excluded_path" in reason
    assert "sealed" in reason and "packet alone" in reason
    assert verdict["condition"] in reason


@pytest.mark.parametrize(
    "detail",
    [
        # Integrity: the candidate on disk does not match the sealed packet.
        "source_graph_single_file_hash_mismatch:expected=abc actual=def",
        "source_graph_single_file_expected_hash_required",
        "source_graph_single_file_path_mismatch:expected=a returned=b",
        # A file that SHOULD be indexable but could not be read/extracted.
        "source_graph_single_file_unreadable:src/module.py",
        # Path-safety violations are genuine problems, not deliberate exclusions.
        "source_graph_single_file_symlink:src/module.py",
        "source_graph_single_file_traversal:../etc/passwd",
        "source_graph_single_file_outside_repository:/etc/passwd",
    ],
)
def test_genuine_source_graph_failures_are_not_tolerated(detail):
    verdict = quality_review.classify_prewarm_error(_wrapped(detail))
    assert verdict["tolerated"] is False
    assert verdict["condition"] is None
    assert "should be indexable" in verdict["reason"]


@pytest.mark.parametrize(
    "genuine_code",
    [
        "source_graph_single_file_unreadable",
        "source_graph_single_file_hash_mismatch",
        "source_graph_single_file_path_mismatch",
        "source_graph_single_file_traversal",
        "source_graph_single_file_outside_repository",
    ],
)
def test_genuine_failure_whose_path_carries_excluded_token_still_refuses(genuine_code):
    # Classify on the CODE, never on a substring of the message.  The wrapped
    # message embeds the worker-controlled candidate path after the code, so a
    # real indexing failure on an indexable file whose path literally CONTAINS
    # ``source_graph_single_file_excluded_glob`` must still refuse loudly.  The
    # leading code segment is the genuine-failure code; the excluded-glob token
    # buried in the path must not flip the verdict.
    detail = f"{genuine_code}:eval/source_graph_single_file_excluded_glob/module.py"
    verdict = quality_review.classify_prewarm_error(_wrapped(detail))
    assert verdict["tolerated"] is False
    assert verdict["condition"] is None
    assert "should be indexable" in verdict["reason"]


def test_tolerated_token_matched_only_as_whole_code_segment():
    # Anchoring is exact-segment, not prefix/substring: a code that merely STARTS
    # WITH a tolerated token but is a distinct (unknown) code must refuse.
    detail = "source_graph_single_file_excluded_glob_but_index_crashed:src/module.py"
    verdict = quality_review.classify_prewarm_error(_wrapped(detail))
    assert verdict["tolerated"] is False
    assert verdict["condition"] is None


def test_later_source_graph_marker_in_path_cannot_forge_tolerance():
    # Only the FIRST (genuine) type-name marker decides.  A candidate path that
    # itself embeds ``SourceGraphError:source_graph_single_file_excluded_glob``
    # after a genuine failure code must not be re-read into a tolerated verdict.
    detail = (
        "source_graph_single_file_unreadable:"
        "eval/SourceGraphError:source_graph_single_file_excluded_glob/x.py"
    )
    verdict = quality_review.classify_prewarm_error(_wrapped(detail))
    assert verdict["tolerated"] is False
    assert verdict["condition"] is None


def test_wrapped_infrastructure_error_stays_loud():
    # A sqlite3.Error/OSError folded into the same prefix carries the exception
    # TYPE name, not ``SourceGraphError`` -- it must never be tolerated.
    msg = (
        "quality_review_candidate_source_graph_prewarm_error:"
        "OperationalError:disk I/O error"
    )
    assert quality_review.classify_prewarm_error(msg)["tolerated"] is False


def test_excluded_token_without_source_graph_marker_fails_closed():
    # Fail-closed: an infrastructure error whose text merely MENTIONS an excluded
    # path (but is not a SourceGraphError) is not swallowed.
    msg = (
        "quality_review_candidate_source_graph_prewarm_error:OSError:"
        "leaked source_graph_single_file_excluded_glob eval/x.json"
    )
    assert quality_review.classify_prewarm_error(msg)["tolerated"] is False


# ---------------------------------------------------------------------------
# The rework finding: the verdict must be read from the type-name SLOT, never by
# searching the text.  ``str(exc)`` for a genuine non-SourceGraphError failure
# embeds the worker-controlled candidate path; a path literally named
# ``SourceGraphError:source_graph_single_file_excluded_glob`` lands in the
# ``<detail>`` slot.  A classifier that scanned for the marker anywhere would
# find it inside that path and read a tolerated code out of the detail, swallowing
# a real infrastructure failure.  Both directions are pinned below so the parse
# stays anchored: a wrapped ``<prefix>:<type_name>:<code>:<detail>`` is tolerated
# only when slot 0 == ``SourceGraphError`` AND slot 1 is an allowlisted code.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "non_source_graph_type",
    ["OSError", "OperationalError", "WorkerToolError", "RuntimeError"],
)
def test_non_source_graph_failure_with_marker_in_path_refuses(non_source_graph_type):
    # Direction 1 (the finding): a GENUINE non-SourceGraphError failure whose
    # candidate path literally contains ``SourceGraphError:``
    # ``source_graph_single_file_excluded_glob``.  Slot 0 is the real type name,
    # not ``SourceGraphError``; the tolerated token lives only in the path detail.
    # It must refuse loudly -- a search-anywhere classifier would have tolerated it.
    msg = (
        "quality_review_candidate_source_graph_prewarm_error:"
        f"{non_source_graph_type}:disk I/O error reading candidate "
        "eval/SourceGraphError:source_graph_single_file_excluded_glob/module.py"
    )
    verdict = quality_review.classify_prewarm_error(msg)
    assert verdict["tolerated"] is False
    assert verdict["condition"] is None
    assert "should be indexable" in verdict["reason"]


def test_genuine_source_graph_failure_with_marker_in_path_refuses():
    # Direction 2 (paired, from the prior round): a GENUINE SourceGraphError whose
    # ``<code>`` slot is a real-failure code but whose path detail re-embeds the
    # full ``SourceGraphError:source_graph_single_file_excluded_glob`` marker.  The
    # code slot decides; the re-embedded marker in the detail must not flip it.
    detail = (
        "source_graph_single_file_unreadable:eval/"
        "SourceGraphError:source_graph_single_file_excluded_glob/module.py"
    )
    verdict = quality_review.classify_prewarm_error(_wrapped(detail))
    assert verdict["tolerated"] is False
    assert verdict["condition"] is None
    assert "should be indexable" in verdict["reason"]


@pytest.mark.parametrize("msg", ["", None])
def test_empty_message_is_not_tolerated(msg):
    assert quality_review.classify_prewarm_error(msg)["tolerated"] is False


def test_non_source_graph_candidate_workertoolerror_is_not_tolerated():
    # Direct WorkerToolErrors raised by the prewarm walk (path escape, symlink,
    # empty candidate DB) are not SourceGraphError exclusions and must refuse.
    for msg in (
        "quality_review_candidate_path_escapes_workspace:eval/x.json",
        "quality_review_candidate_path_symlink:eval/x.json",
        "quality_review_candidate_source_graph_empty",
    ):
        assert quality_review.classify_prewarm_error(msg)["tolerated"] is False


# ---------------------------------------------------------------------------
# Launch behaviour: the reviewer must actually launch across an excluded path.
# ---------------------------------------------------------------------------

def _canonical_authority(_authority_repo):
    return SimpleNamespace(
        authority_source="canonical", authority_state="sole_authority"
    )


def _run_launch_with_prewarm(monkeypatch, tmp_path, *, prewarm, stop_registration):
    """Drive the real ``_launch_isolated`` reviewer path with a faked prewarm.

    Returns ``(order, events, result)`` where ``order`` records the authority /
    prewarm / registration sequence, ``events`` are the recorded reviewer
    progress phases, and ``result`` is the launch receipt (``None`` when the
    flow reached registration and stopped there).
    """

    manager, binding = _reviewer_launch_setup(tmp_path, monkeypatch)
    order: list[str] = []
    events: list[tuple[str, str | None]] = []

    def fake_authority(authority_repo):
        order.append("authority")
        return _canonical_authority(authority_repo)

    def wrapped_prewarm(*args, **kwargs):
        order.append("prewarm")
        return prewarm(*args, **kwargs)

    def fake_registration(*_args, **_kwargs):
        order.append("registration")
        if stop_registration:
            raise RuntimeError(
                "stop-after-registration: real subprocess spawn is out of scope"
            )
        return {}

    def progress(phase, detail=None):
        events.append((phase, detail))

    monkeypatch.setattr(
        worker_ai_tools_mcp, "verify_quality_review_prewarm_authority", fake_authority
    )
    monkeypatch.setattr(
        worker_ai_tools_mcp, "prewarm_quality_review_source_graph", wrapped_prewarm
    )
    monkeypatch.setattr(
        process_launcher, "_provision_worker_mcp_runtime_for_authority",
        fake_registration,
    )

    result = manager._launch_isolated(
        task_id="TASK_REVIEW_1",
        runner="claude_worker_reviewer",
        topic="quality_review",
        adapter_id="claude_cli",
        model=None,
        owner_prompt="",
        timeout_seconds=30,
        quality_review_binding=binding,
        prewarm_progress=progress,
    )
    return order, events, result


def test_excluded_glob_path_skips_and_reviewer_still_launches(monkeypatch, tmp_path):
    def prewarm(*_args, **_kwargs):
        raise worker_ai_tools_mcp.WorkerToolError(
            _wrapped(
                "source_graph_single_file_excluded_glob:"
                "eval/aiworkhub_agent_tool_instruction_activation_b821_v1.json"
            )
        )

    order, events, _result = _run_launch_with_prewarm(
        monkeypatch, tmp_path, prewarm=prewarm, stop_registration=True,
    )

    # The reviewer launch proceeded past prewarm all the way to registration:
    # prewarm did NOT block the review.
    assert order == ["authority", "prewarm", "registration"]
    phases = [phase for phase, _detail in events]
    assert "reviewer_source_graph_prewarm_started" in phases
    assert "reviewer_source_graph_prewarm_skipped_excluded" in phases
    # It was never (mis)recorded as a prewarm failure.
    assert "reviewer_source_graph_prewarm_failed" not in phases
    # The skip and its reason are recorded for an auditor.
    skip_detail = dict(events)["reviewer_source_graph_prewarm_skipped_excluded"]
    assert "source_graph_single_file_excluded_glob" in skip_detail


def test_all_excluded_paths_launch_from_sealed_packet_alone(monkeypatch, tmp_path):
    # When every changed path is excluded the prewarm cannot build any overlay;
    # this is still not a refusal -- the reviewer works from the packet alone.
    def prewarm(*_args, **_kwargs):
        raise worker_ai_tools_mcp.WorkerToolError(
            _wrapped("source_graph_single_file_excluded_glob:eval/generated_a.json")
        )

    order, events, _result = _run_launch_with_prewarm(
        monkeypatch, tmp_path, prewarm=prewarm, stop_registration=True,
    )

    assert order == ["authority", "prewarm", "registration"]
    skip_detail = dict(events)["reviewer_source_graph_prewarm_skipped_excluded"]
    # The recorded evidence states the reviewer proceeds on the sealed packet.
    assert "sealed" in skip_detail and "packet alone" in skip_detail
    assert "overlay was not built" in skip_detail


def test_genuine_indexable_failure_refuses_loudly(monkeypatch, tmp_path):
    # A hash mismatch is a real problem on a file that SHOULD be indexable: it
    # must refuse loudly and must never reach runtime/provider registration.
    def prewarm(*_args, **_kwargs):
        raise worker_ai_tools_mcp.WorkerToolError(
            _wrapped(
                "source_graph_single_file_hash_mismatch:"
                "expected=abcd0000 actual=ef120000"
            )
        )

    order, events, result = _run_launch_with_prewarm(
        monkeypatch, tmp_path, prewarm=prewarm, stop_registration=False,
    )

    assert order == ["authority", "prewarm"]
    assert result is not None and result.get("ok") is False
    reason = str(result.get("blocked_reason") or "")
    assert reason.startswith("quality_review_source_graph_prewarm_failed:")
    assert "source_graph_single_file_hash_mismatch" in reason
    phases = [phase for phase, _detail in events]
    assert "reviewer_source_graph_prewarm_failed" in phases
    assert "reviewer_source_graph_prewarm_skipped_excluded" not in phases


def test_genuine_failure_with_excluded_token_in_path_refuses_launch(monkeypatch, tmp_path):
    # End-to-end guard for the classifier finding: an unreadable INDEXABLE file
    # whose path literally contains ``source_graph_single_file_excluded_glob``
    # must refuse loudly and never reach runtime/provider registration -- a bare
    # substring match would have swallowed it and launched the reviewer.
    def prewarm(*_args, **_kwargs):
        raise worker_ai_tools_mcp.WorkerToolError(
            _wrapped(
                "source_graph_single_file_unreadable:"
                "eval/source_graph_single_file_excluded_glob/module.py"
            )
        )

    order, events, result = _run_launch_with_prewarm(
        monkeypatch, tmp_path, prewarm=prewarm, stop_registration=False,
    )

    assert order == ["authority", "prewarm"]
    assert "registration" not in order
    assert result is not None and result.get("ok") is False
    reason = str(result.get("blocked_reason") or "")
    assert reason.startswith("quality_review_source_graph_prewarm_failed:")
    phases = [phase for phase, _detail in events]
    assert "reviewer_source_graph_prewarm_failed" in phases
    assert "reviewer_source_graph_prewarm_skipped_excluded" not in phases
