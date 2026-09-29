"""Escaped-defect attribution over a real git history and a real canonical store.

Nothing here stubs git. Every fixture builds an actual repository, commits actual
bytes, and seals each card's ``accepted_outcome_receipt`` against the bytes that
commit really holds -- because the whole claim under test is that a blamed line
resolves to the *identity* of the commit an accepted receipt landed in. A fake
blame answer would prove only that the fake agrees with the assertion.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from aiworkhub import needfix_store, sdlc_attribution, task_store

REPOSITORY_ID = "repo_sdlc_attribution_fixture"

LEGACY = "src/pkg/legacy.py"
ALPHA = "src/pkg/alpha.py"
BETA = "src/pkg/beta.py"


# --------------------------------------------------------------------------
# fixture plumbing: a real repository, a real store, real receipts
# --------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    for key in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_PREFIX",
    ):
        env.pop(key, None)
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, env=env,
    )
    return result.stdout.decode().strip()


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _write(repo: Path, relative: str, lines: list[str]) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _commit(repo: Path, message: str, *relatives: str) -> str:
    _git(repo, "add", "--", *relatives)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    task_store.initialize_repository(repo)
    needfix_store.initialize_repository(repo)
    _git(repo, "init")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "core.autocrlf", "false")
    return repo


def _seal(repo: Path, task_id: str, request_id: str, base_oid: str, paths: list[str]) -> dict:
    """A receipt over the bytes ``paths`` hold on disk right now.

    Called immediately after the commit that promoted them, so the sealed hashes
    are the committed hashes.
    """
    promoted = sorted(set(paths))
    hashes = {
        relative: hashlib.sha256((repo / relative).read_bytes()).hexdigest()
        for relative in promoted
    }
    manifest = {"artifacts": ["metadata.json"]}
    receipt = {
        "schema_id": needfix_store.ACCEPTED_OUTCOME_RECEIPT_SCHEMA_ID,
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": 1,
        "base_oid": base_oid,
        "promoted_paths": promoted,
        "changed_path_hashes": hashes,
        "attempt_artifact_manifest_id": _digest(manifest),
        "repository_revision": "sha256:" + _digest(
            {"base_oid": base_oid, "changed_path_hashes": hashes}
        ),
    }
    receipt["receipt_id"] = "sha256:" + _digest(receipt)
    return receipt


def _insert_accepted_card(repo: Path, task_id: str, request_id: str, receipt: dict) -> None:
    card = {
        "runner": "fixture_runner",
        "topic": "coding",
        "risk_tier": "low",
        "status": "finished",
        "claim_epoch": receipt["claim_epoch"],
        "accepted_request_id": request_id,
        "accept_evidence": {"accepted_outcome_receipt": receipt},
    }
    now = "2026-09-01T00:00:00+00:00"
    _readiness, db_path = task_store._require_ready(repo)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, runner, topic, status, worker_status, priority, "
            "objective, card_json, created_at, updated_at, claimed_by, claimed_at, "
            "started_at, origin_thread_id) VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, "fixture_runner", "coding", "finished", "finished",
                json.dumps(card), now, now, "fixture_runner", now, now, f"thread-{task_id}",
            ),
        )
        conn.execute(
            "INSERT INTO task_events(task_id, event, runner, payload_json, created_at) "
            "VALUES (?, 'accept_review', 'fixture_runner', ?, ?)",
            (
                task_id,
                json.dumps({"request_id": request_id, "accepted_outcome_receipt": receipt}),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _accept(repo: Path, name: str, base_oid: str, paths: list[str], *, tamper: bool = False) -> str:
    """Seed one manager-accepted card over ``paths`` as they stand on disk."""
    task_id = f"FIXTURE_{name.upper()}"
    request_id = f"req-{name}"
    receipt = _seal(repo, task_id, request_id, base_oid, paths)
    if tamper:
        # A receipt whose id no longer signs its own content: exactly what a
        # hand-edited or half-written row looks like, and never accepted.
        receipt["receipt_id"] = "sha256:" + "0" * 64
    _insert_accepted_card(repo, task_id, request_id, receipt)
    return task_id


def _needfix_for(repo: Path, title: str, task_id: str) -> str:
    """A NeedFix taken through the real lifecycle and linked to ``task_id``."""
    record = needfix_store.capture_proposal(
        repo, title=title, description=f"{title} -- fixture", evidence={"origin": "fixture"},
    )
    needfix_id = record["id"]
    needfix_store.triage_needfix(repo, needfix_id)
    needfix_store.accept_needfix(repo, needfix_id)
    needfix_store.link_existing_task(
        repo,
        needfix_id,
        task_id,
        lambda wanted: task_store.get_task(repo, wanted),
        task_store.canonical_status,
    )
    return needfix_id


def _identity_of(repo: Path, task_id: str) -> dict:
    catalog = sdlc_attribution.load_accepted_catalog(repo, REPOSITORY_ID)
    card = catalog.card_for_task(task_id)
    assert card is not None, f"{task_id} is not in the accepted catalog"
    return dict(card.identity)


@pytest.fixture
def mixed_repo(tmp_path: Path) -> dict:
    """One history carrying an attributed fix and three distinct unknown shapes.

    c0 legacy.py (nobody's card)      c1 alpha.py -> card A
    c2 alpha.py line 2 rewritten -> card B, the fix that A caused
    c3 beta.py added                 -> card C, a purely additive fix
    c4 legacy.py line 1 rewritten    -> card D, blaming a commit no card owns
    c5 legacy.py line 2 rewritten    -> card E, whose receipt does not sign itself
    """
    repo = _new_repo(tmp_path)
    _write(repo, LEGACY, ["legacy one", "legacy two", "legacy three"])
    commit_0 = _commit(repo, "base", LEGACY)

    _write(repo, ALPHA, ["alpha one", "alpha two", "alpha three"])
    commit_a = _commit(repo, "card A", ALPHA)
    task_a = _accept(repo, "a", commit_0, [ALPHA])

    _write(repo, ALPHA, ["alpha one", "alpha two fixed", "alpha three"])
    commit_b = _commit(repo, "card B", ALPHA)
    task_b = _accept(repo, "b", commit_a, [ALPHA])

    _write(repo, BETA, ["beta one", "beta two"])
    commit_c = _commit(repo, "card C", BETA)
    task_c = _accept(repo, "c", commit_b, [BETA])

    _write(repo, LEGACY, ["legacy one rewritten", "legacy two", "legacy three"])
    commit_d = _commit(repo, "card D", LEGACY)
    task_d = _accept(repo, "d", commit_c, [LEGACY])

    _write(repo, LEGACY, ["legacy one rewritten", "legacy two rewritten", "legacy three"])
    _commit(repo, "card E", LEGACY)
    task_e = _accept(repo, "e", commit_d, [LEGACY], tamper=True)

    return {
        "repo": repo,
        "task_a": task_a,
        "commit_a": commit_a,
        "commit_b": commit_b,
        "attributed": _needfix_for(repo, "alpha two was wrong", task_b),
        "additive": _needfix_for(repo, "beta was missing", task_c),
        "no_receipt": _needfix_for(repo, "legacy one was wrong", task_d),
        "malformed": _needfix_for(repo, "legacy two was wrong", task_e),
    }


# --------------------------------------------------------------------------
# the attributed case
# --------------------------------------------------------------------------

def test_a_fix_that_rewrites_an_accepted_cards_line_names_that_card(mixed_repo: dict) -> None:
    repo = mixed_repo["repo"]
    result = sdlc_attribution.attribute_needfix(
        repo, REPOSITORY_ID, mixed_repo["attributed"],
    )

    expected = _identity_of(repo, mixed_repo["task_a"])
    assert result.attributed is True
    assert result.reason == sdlc_attribution.ATTRIBUTED
    assert result.caused_by == expected
    assert result.written is True
    assert result.blamed_lines == {f"{mixed_repo['task_a']}:req-a": 1}
    assert result.unexplained_lines == 0
    assert needfix_store.get_needfix(repo, mixed_repo["attributed"])["caused_by"] == expected


def test_the_written_cause_passes_needfix_stores_own_validation(mixed_repo: dict) -> None:
    """The row must survive the store's validator, not merely round-trip JSON."""
    repo = mixed_repo["repo"]
    sdlc_attribution.attribute_needfix(repo, REPOSITORY_ID, mixed_repo["attributed"])
    stored = needfix_store.get_needfix(repo, mixed_repo["attributed"])["caused_by"]

    catalog = sdlc_attribution.load_accepted_catalog(repo, REPOSITORY_ID)
    validated = needfix_store.validate_caused_by(
        stored,
        repository_id=REPOSITORY_ID,
        verify_accepted_outcome=catalog.verify_accepted_outcome,
    )

    assert validated == _identity_of(repo, mixed_repo["task_a"])


def test_the_cause_is_the_identity_of_a_commit_not_a_resemblance(mixed_repo: dict) -> None:
    """Card A is the cause because its receipt landed in exactly the blamed commit.

    Card A and card B promoted the same path; only the commit each one's sealed
    hashes actually appear in tells them apart, which is why a path match alone
    is never enough.
    """
    repo = mixed_repo["repo"]
    catalog = sdlc_attribution.load_accepted_catalog(repo, REPOSITORY_ID)
    card_a = catalog.card_for_task(mixed_repo["task_a"])
    card_b = catalog.card_for_task("FIXTURE_B")
    assert card_a is not None and card_b is not None

    assert catalog.holding_commit(card_a) == (mixed_repo["commit_a"], "")
    assert catalog.holding_commit(card_b) == (mixed_repo["commit_b"], "")
    assert card_a.promoted_paths == card_b.promoted_paths == (ALPHA,)


# --------------------------------------------------------------------------
# the unknown shapes -- none of which may write
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("additive", sdlc_attribution.UNKNOWN_ADDITIVE_ONLY),
        ("no_receipt", sdlc_attribution.UNKNOWN_NO_RECEIPT),
        ("malformed", sdlc_attribution.UNKNOWN_FIX_NOT_ACCEPTED),
    ],
)
def test_an_unknown_verdict_writes_nothing(mixed_repo: dict, key: str, reason: str) -> None:
    repo = mixed_repo["repo"]
    result = sdlc_attribution.attribute_needfix(repo, REPOSITORY_ID, mixed_repo[key])

    assert result.attributed is False
    assert result.reason == reason
    assert result.caused_by is None
    assert result.written is False
    assert needfix_store.get_needfix(repo, mixed_repo[key])["caused_by"] is None


def test_a_blamed_commit_without_a_receipt_is_counted_not_guessed(mixed_repo: dict) -> None:
    """``no_receipt`` must be reached with the line actually measured."""
    result = sdlc_attribution.attribute_needfix(
        mixed_repo["repo"], REPOSITORY_ID, mixed_repo["no_receipt"],
    )

    assert result.reason == sdlc_attribution.UNKNOWN_NO_RECEIPT
    assert result.unexplained_lines == 1
    assert result.blamed_lines == {}


def test_a_needfix_with_no_converted_task_is_not_converted(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    record = needfix_store.capture_proposal(
        repo, title="never converted", description="fixture", evidence={},
    )

    result = sdlc_attribution.attribute_needfix(repo, REPOSITORY_ID, record["id"])

    assert result.reason == sdlc_attribution.UNKNOWN_NOT_CONVERTED
    assert result.caused_by is None


def test_two_receipts_explaining_equal_line_counts_is_a_tie(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    _write(repo, LEGACY, ["keep"])
    base = _commit(repo, "base", LEGACY)

    _write(repo, ALPHA, ["alpha one", "alpha two"])
    commit_a = _commit(repo, "card A", ALPHA)
    task_a = _accept(repo, "a", base, [ALPHA])

    _write(repo, BETA, ["beta one", "beta two"])
    commit_c = _commit(repo, "card C", BETA)
    task_c = _accept(repo, "c", commit_a, [BETA])

    _write(repo, ALPHA, ["alpha one rewritten", "alpha two"])
    _write(repo, BETA, ["beta one rewritten", "beta two"])
    _commit(repo, "card F", ALPHA, BETA)
    task_f = _accept(repo, "f", commit_c, [ALPHA, BETA])
    needfix_id = _needfix_for(repo, "both were wrong", task_f)

    result = sdlc_attribution.attribute_needfix(repo, REPOSITORY_ID, needfix_id)

    assert result.reason == sdlc_attribution.UNKNOWN_TIE
    assert result.blamed_lines == {f"{task_a}:req-a": 1, f"{task_c}:req-c": 1}
    assert result.caused_by is None
    assert needfix_store.get_needfix(repo, needfix_id)["caused_by"] is None


# --------------------------------------------------------------------------
# idempotence and the write-once refusal
# --------------------------------------------------------------------------

def test_re_running_attribution_is_a_no_op(mixed_repo: dict) -> None:
    repo = mixed_repo["repo"]
    needfix_id = mixed_repo["attributed"]
    first = sdlc_attribution.attribute_needfix(repo, REPOSITORY_ID, needfix_id)
    stored_after_first = needfix_store.get_needfix(repo, needfix_id)["caused_by"]

    second = sdlc_attribution.attribute_needfix(repo, REPOSITORY_ID, needfix_id)

    assert first.written is True
    assert second.written is False, "the second run must not rewrite an identical cause"
    assert second.attributed is True
    assert second.caused_by == first.caused_by
    assert needfix_store.get_needfix(repo, needfix_id)["caused_by"] == stored_after_first


def test_a_conflicting_cause_is_refused_and_the_recorded_one_survives(mixed_repo: dict) -> None:
    repo = mixed_repo["repo"]
    needfix_id = mixed_repo["attributed"]
    sdlc_attribution.attribute_needfix(repo, REPOSITORY_ID, needfix_id)
    recorded = needfix_store.get_needfix(repo, needfix_id)["caused_by"]

    catalog = sdlc_attribution.load_accepted_catalog(repo, REPOSITORY_ID)
    other = catalog.card_for_task("FIXTURE_C")
    assert other is not None

    with pytest.raises(needfix_store.NeedFixConflictError):
        needfix_store.update_needfix(
            repo,
            needfix_id,
            caused_by=other.identity,
            repository_id=REPOSITORY_ID,
            verify_accepted_outcome=catalog.verify_accepted_outcome,
        )

    assert needfix_store.get_needfix(repo, needfix_id)["caused_by"] == recorded


# --------------------------------------------------------------------------
# attribute_all
# --------------------------------------------------------------------------

def test_attribute_all_reports_exact_counts_per_reason(mixed_repo: dict) -> None:
    report = sdlc_attribution.attribute_all(mixed_repo["repo"], REPOSITORY_ID)

    assert report.schema_id == sdlc_attribution.REPORT_SCHEMA_ID
    assert report.considered == 4
    assert report.attributed == 1
    assert report.unknown == 3
    assert report.written == 1
    assert report.unknown_by_reason == {
        sdlc_attribution.UNKNOWN_ADDITIVE_ONLY: 1,
        sdlc_attribution.UNKNOWN_NO_RECEIPT: 1,
        sdlc_attribution.UNKNOWN_FIX_NOT_ACCEPTED: 1,
    }
    assert {row.needfix_id for row in report.attributions} == {
        mixed_repo["attributed"], mixed_repo["additive"],
        mixed_repo["no_receipt"], mixed_repo["malformed"],
    }


def test_attribute_all_records_an_unreadable_row_rather_than_raising(
    mixed_repo: dict, monkeypatch
) -> None:
    """A report that dies on one row says nothing about the rows after it."""
    exploding = mixed_repo["no_receipt"]
    real_get_needfix = needfix_store.get_needfix

    def _get_needfix(repo_root, needfix_id, *args, **kwargs):
        if needfix_id == exploding:
            raise RuntimeError("row is unreadable")
        return real_get_needfix(repo_root, needfix_id, *args, **kwargs)

    monkeypatch.setattr(needfix_store, "get_needfix", _get_needfix)

    report = sdlc_attribution.attribute_all(mixed_repo["repo"], REPOSITORY_ID)

    assert report.considered == 4
    assert report.attributed == 1
    assert report.unknown_by_reason[sdlc_attribution.UNKNOWN_ROW_UNREADABLE] == 1
    unreadable = [row for row in report.attributions if row.needfix_id == exploding]
    assert len(unreadable) == 1
    assert unreadable[0].detail == "RuntimeError"


# --------------------------------------------------------------------------
# path shape, and the boundaries git itself decides
# (tests/test_build_accepted_task_eval.py owns the "one shared helper,
# no shell, src never imports scripts" assertions, next to the script.)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("relative", "is_test"),
    [
        ("src/pkg/alpha.py", False),
        ("tests/test_alpha.py", True),
        ("src/pkg/tests/helper.py", True),
        ("src/pkg/alpha_test.py", True),
        ("src/pkg/latest.py", False),
    ],
)
def test_test_paths_are_decided_by_path_shape(relative: str, is_test: bool) -> None:
    assert sdlc_attribution.is_test_path(relative) is is_test


def test_a_fix_promoting_only_tests_has_no_source_paths_to_blame(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    _write(repo, LEGACY, ["keep"])
    base = _commit(repo, "base", LEGACY)

    _write(repo, "tests/test_thing.py", ["assert True"])
    _commit(repo, "tests only", "tests/test_thing.py")
    task_id = _accept(repo, "testsonly", base, ["tests/test_thing.py"])
    needfix_id = _needfix_for(repo, "tests were missing", task_id)

    result = sdlc_attribution.attribute_needfix(repo, REPOSITORY_ID, needfix_id)

    assert result.reason == sdlc_attribution.UNKNOWN_NO_SOURCE_PATHS
    assert result.caused_by is None


def test_a_history_that_holds_other_bytes_is_a_hash_mismatch(tmp_path: Path) -> None:
    """Read and found nothing is not the same answer as could not be read.

    The unreadable-history half of this distinction is pinned next to the
    provenance check that consumes it, in tests/test_build_accepted_task_eval.py.
    """
    repo = _new_repo(tmp_path)
    _write(repo, LEGACY, ["keep"])
    base = _commit(repo, "base", LEGACY)
    _write(repo, ALPHA, ["alpha"])
    _commit(repo, "later", ALPHA)

    commit, failure = sdlc_attribution.first_holding_commit(
        repo, base, [ALPHA], {ALPHA: "0" * 64},
    )

    assert commit is None
    assert failure == sdlc_attribution.HASH_MISMATCH
