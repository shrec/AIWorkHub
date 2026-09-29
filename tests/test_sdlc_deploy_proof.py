"""RM-2026-00076 E: the release ledger reader and the deploy policy it is judged by.

The end-to-end Deploy/Maintain proofs over a real accepted candidate and a real
git release commit live in ``test_sdlc_stage_evidence.py``; these pin the
ledger parser the release script delegates to and the refusals that need no
canonical store at all.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiworkhub import repo_policy, sdlc_deploy_proof, task_store
from aiworkhub.repository_state import bootstrap_repository


@pytest.fixture(autouse=True)
def _fresh_git_facts():
    sdlc_deploy_proof._clear_git_facts()
    yield
    sdlc_deploy_proof._clear_git_facts()


def _write_ledger(root: Path, *lines) -> Path:
    ledger = root.joinpath(*sdlc_deploy_proof.LEDGER_REL)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        "".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines),
        encoding="utf-8",
    )
    return ledger


def _write_policy(root: Path, deploy=None) -> None:
    policy = copy.deepcopy(repo_policy.DEFAULT_POLICY)
    policy.pop("deploy")
    if deploy is not None:
        policy["deploy"] = deploy
    path = root / repo_policy.POLICY_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy), encoding="utf-8")


def _built(version: str, **extra) -> dict:
    return {"kind": "built", "version": version, "target": "vscode_local", **extra}


def _installed(version: str, server_version: str | None = None) -> dict:
    return {
        "kind": "installed", "version": version,
        "installed_at": "2026-09-29T00:05:00Z", "server_version": server_version or version,
    }


def test_a_missing_ledger_is_empty_and_confirms_nothing(tmp_path):
    assert sdlc_deploy_proof.load_release_ledger(tmp_path) == []
    assert sdlc_deploy_proof.latest_confirmed(tmp_path) is None


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("not-json", "is not valid JSON"),
        ("[1]", "is not a JSON object"),
        (json.dumps({"kind": "built", "version": 1}), "non-string version"),
        (json.dumps({"kind": "installed"}), "missing version"),
    ],
)
def test_a_malformed_ledger_line_is_refused_by_name(tmp_path, line, message):
    _write_ledger(tmp_path, line)
    with pytest.raises(sdlc_deploy_proof.ReleaseLedgerError, match=message):
        sdlc_deploy_proof.load_release_ledger(tmp_path)


def test_only_a_confirmation_reporting_the_built_version_confirms_it(tmp_path):
    _write_ledger(
        tmp_path,
        _built("1.0.0"), _installed("1.0.0"),
        _built("1.1.0"), _installed("1.1.0", server_version="1.0.0"),
        _built("1.2.0"),
    )
    entries = sdlc_deploy_proof.load_release_ledger(tmp_path)

    confirmed = sdlc_deploy_proof.confirmed_releases(entries)

    assert [built["version"] for built, _ in confirmed] == ["1.0.0"]
    assert sdlc_deploy_proof.latest_confirmed(tmp_path) == _built("1.0.0")


def test_the_newest_confirmed_version_is_ordered_numerically_not_by_ledger(tmp_path):
    _write_ledger(
        tmp_path,
        _built("1.10.0"), _installed("1.10.0"), _built("1.9.0"), _installed("1.9.0"),
    )
    assert sdlc_deploy_proof.latest_confirmed(tmp_path)["version"] == "1.10.0"
    confirmed = sdlc_deploy_proof.confirmed_releases(
        sdlc_deploy_proof.load_release_ledger(tmp_path)
    )
    # Earliest-first, as the deploy proof searches them.
    assert [built["version"] for built, _ in confirmed] == ["1.10.0", "1.9.0"]


def test_a_policy_without_a_deploy_section_defaults_to_vscode_local(tmp_path):
    _write_policy(tmp_path)
    assert sdlc_deploy_proof.deploy_policy(tmp_path) == {
        "targets": ["vscode_local"], "approver": "manager_seat",
    }


def test_an_unsupported_deploy_approver_makes_the_policy_refuse(tmp_path):
    _write_policy(tmp_path, {"targets": ["vscode_local"], "approver": "worker"})
    assert sdlc_deploy_proof.deploy_policy(tmp_path) == "deploy_policy_invalid"


@pytest.mark.parametrize("target", ["production", ""])
def test_a_target_outside_the_policy_is_refused_before_any_release_is_read(tmp_path, target):
    _write_policy(tmp_path, {"targets": ["vscode_local"]})
    _write_ledger(tmp_path, "not-json")
    assert sdlc_deploy_proof.deploy_proof(
        tmp_path, "repo", "T", {"promoted_paths": [], "changed_path_hashes": {}}, target
    ) == "deploy_target_unknown"


def test_no_confirmed_release_for_the_target_refuses_deploy(tmp_path):
    receipt = {"promoted_paths": [], "changed_path_hashes": {}}
    assert sdlc_deploy_proof.deploy_proof(
        tmp_path, "repo", "T", receipt, "vscode_local"
    ) == "release_not_confirmed"
    _write_ledger(tmp_path, _built("1.0.0", target="elsewhere"), _installed("1.0.0"))
    assert sdlc_deploy_proof.deploy_proof(
        tmp_path, "repo", "T", receipt, "vscode_local"
    ) == "release_not_confirmed"
    _write_ledger(tmp_path, "not-json")
    assert sdlc_deploy_proof.deploy_proof(
        tmp_path, "repo", "T", receipt, "vscode_local"
    ) == "release_ledger_invalid"


def test_a_release_whose_commit_is_not_in_the_repository_proves_nothing(tmp_path):
    _write_ledger(
        tmp_path,
        _built("1.0.0", release_commit="a" * 40, vsix_sha256="b" * 64),
        _installed("1.0.0"),
    )
    receipt = {"promoted_paths": ["src/x.py"], "changed_path_hashes": {"src/x.py": "c" * 64}}
    assert sdlc_deploy_proof.deploy_proof(
        tmp_path, "repo", "T", receipt, "vscode_local"
    ) == "release_commit_missing_promoted_hash"


# --------------------------------------------------------------------------- #
# supersession by a later acceptance, over a real task store and git commit
# --------------------------------------------------------------------------- #

PROMOTED = "src/feature.py"
MINE = b"accepted by this task\n"
RELEASED = b"released bytes\n"
OWN_ACCEPTED_AT = "2026-09-29T10:00:00Z"


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True, env=sdlc_deploy_proof.scrubbed_git_env(),
    )
    return completed.stdout.decode().strip()


@pytest.fixture
def released(tmp_path):
    """A bootstrapped repository whose confirmed release 1.0.0 holds ``RELEASED``."""
    root = tmp_path / "repo"
    root.mkdir()
    bootstrap_repository(root, repo_name="deploy-proof")
    readiness = task_store.storage_readiness(root)
    if not readiness.ready:
        task_store.initialize_repository(root)
        readiness = task_store.storage_readiness(root)
    assert readiness.ready
    target = root / PROMOTED
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(RELEASED)
    _git(root, "init", "-q")
    for key, value in (
        ("user.email", "fixture@example.com"), ("user.name", "Fixture"),
        ("commit.gpgsign", "false"), ("core.autocrlf", "false"),
    ):
        _git(root, "config", key, value)
    _git(root, "add", PROMOTED)
    _git(root, "commit", "-q", "-m", "release 1.0.0")
    _write_ledger(
        root,
        _built(
            "1.0.0", release_commit=_git(root, "rev-parse", "HEAD"), vsix_sha256="b" * 64,
            built_at="2026-09-29T12:00:00Z",
        ),
        _installed("1.0.0"),
    )
    return SimpleNamespace(root=root, repo_id=readiness.repo_id)


def _insert_card(root: Path, task_id: str, *, accepted_at: str, digest: str, accepted=True):
    """One other card carrying an accepted-outcome receipt for ``PROMOTED``."""
    card = {
        "task_id": task_id,
        "accepted_at": accepted_at,
        "accept_evidence": {"accepted_outcome_receipt": {
            "promoted_paths": [PROMOTED], "changed_path_hashes": {PROMOTED: digest},
        }},
    }
    status, worker_status = ("finished", "done") if accepted else ("review", "review")
    conn = sqlite3.connect(str(task_store.canonical_db_path(root)))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, status, worker_status, objective, "
            "card_json, created_at, updated_at, claimed_by, claimed_at, started_at) "
            "VALUES (?, 'codex_worker', 'deploy', ?, ?, 'objective', ?, ?, ?, ?, ?, ?)",
            (
                task_id, status, worker_status, json.dumps(card),
                accepted_at, accepted_at, "codex_worker", accepted_at, accepted_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _deploy(ns: SimpleNamespace):
    receipt = {"promoted_paths": [PROMOTED], "changed_path_hashes": {PROMOTED: _sha(MINE)}}
    return sdlc_deploy_proof.deploy_proof(
        ns.root, ns.repo_id, "T-SELF", receipt, "vscode_local", accepted_at=OWN_ACCEPTED_AT
    )


def test_release_bytes_matching_neither_this_nor_the_later_hash_are_refused(released):
    assert _deploy(released) == "release_commit_missing_promoted_hash"
    _insert_card(
        released.root, "T-LATER-OTHER", accepted_at="2026-09-29T11:00:00Z",
        digest=_sha(b"yet other bytes\n"),
    )
    assert _deploy(released) == "release_commit_missing_promoted_hash"

    _insert_card(
        released.root, "T-LATER-RELEASED", accepted_at="2026-09-29T11:00:00Z",
        digest=_sha(RELEASED),
    )
    proof = _deploy(released)
    assert proof["superseded_paths"] == [PROMOTED]
    assert proof["approval_policy"] == "manager_seat"
    assert "approver" not in proof


def test_a_later_card_that_is_not_accepted_does_not_supersede(released):
    _insert_card(
        released.root, "T-LATER-REVIEW", accepted_at="2026-09-29T11:00:00Z",
        digest=_sha(RELEASED), accepted=False,
    )
    assert _deploy(released) == "release_commit_missing_promoted_hash"


def test_mixed_z_and_offset_accepted_at_values_order_as_instants(released):
    # 11:00+02:00 is 09:00Z: TEXT-later than 10:00:00Z, but an earlier instant.
    _insert_card(
        released.root, "T-EARLIER", accepted_at="2026-09-29T11:00:00+02:00",
        digest=_sha(RELEASED),
    )
    assert _deploy(released) == "release_commit_missing_promoted_hash"

    # ``.500000+00:00`` sorts before ``Z`` as TEXT, yet is half a second later.
    _insert_card(
        released.root, "T-LATER", accepted_at="2026-09-29T10:00:00.500000+00:00",
        digest=_sha(RELEASED),
    )
    assert _deploy(released)["superseded_paths"] == [PROMOTED]


def test_git_object_and_config_redirection_is_scrubbed(released, monkeypatch, tmp_path):
    decoy = tmp_path / "alternates"
    decoy.mkdir()
    injected = {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(decoy),
        "GIT_OBJECT_DIRECTORY": str(decoy),
        "GIT_CONFIG_PARAMETERS": "'core.bare'='true'",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.bare",
        "GIT_CONFIG_VALUE_0": "true",
        "GIT_CONFIG_GLOBAL": str(decoy / "gitconfig"),
        "GIT_CONFIG_SYSTEM": str(decoy / "gitconfig"),
    }
    for key, value in injected.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AIWORKHUB_UNRELATED", "kept")

    scrubbed = sdlc_deploy_proof.scrubbed_git_env()

    assert not set(injected) & set(scrubbed)
    assert scrubbed["AIWORKHUB_UNRELATED"] == "kept"
    _insert_card(
        released.root, "T-LATER", accepted_at="2026-09-29T11:00:00Z", digest=_sha(RELEASED),
    )
    # git still reads the real repository's objects, not the decoy directory.
    assert _deploy(released)["superseded_paths"] == [PROMOTED]


def test_an_oversized_release_blob_is_refused_and_no_later_text_supersedes_it(
    released, monkeypatch
):
    monkeypatch.setattr(sdlc_deploy_proof, "MAX_RELEASE_BLOB_BYTES", 1)
    # Once a string sentinel, "oversized" could have matched this later "digest".
    _insert_card(
        released.root, "T-LATER-SENTINEL", accepted_at="2026-09-29T11:00:00Z",
        digest="oversized",
    )
    assert _deploy(released) == "release_blob_oversized"


def test_a_non_digest_hash_in_this_receipt_is_malformed(released):
    receipt = {"promoted_paths": [PROMOTED], "changed_path_hashes": {PROMOTED: "oversized"}}
    assert sdlc_deploy_proof.deploy_proof(
        released.root, released.repo_id, "T-SELF", receipt, "vscode_local",
        accepted_at=OWN_ACCEPTED_AT,
    ) == "acceptance_receipt_malformed"


@pytest.mark.parametrize("failing", ["commit", "blob"])
def test_an_unverifiable_earlier_release_is_not_skipped_for_a_later_match(
    released, monkeypatch, failing
):
    first = _git(released.root, "rev-parse", "HEAD")
    (released.root / PROMOTED).write_bytes(MINE)
    _git(released.root, "add", PROMOTED)
    _git(released.root, "commit", "-q", "-m", "release 1.1.0")
    ledger = released.root.joinpath(*sdlc_deploy_proof.LEDGER_REL)
    with ledger.open("a", encoding="utf-8") as handle:
        for line in (
            _built(
                "1.1.0", release_commit=_git(released.root, "rev-parse", "HEAD"),
                vsix_sha256="d" * 64, built_at="2026-09-29T13:00:00Z",
            ),
            _installed("1.1.0"),
        ):
            handle.write(json.dumps(line) + "\n")

    if failing == "commit":
        real_present = sdlc_deploy_proof._commit_present
        monkeypatch.setattr(
            sdlc_deploy_proof, "_commit_present",
            lambda root, commit: None if commit == first else real_present(root, commit),
        )
    else:
        real_blob = sdlc_deploy_proof._blob_sha256
        monkeypatch.setattr(
            sdlc_deploy_proof, "_blob_sha256",
            lambda root, commit, path: (
                sdlc_deploy_proof._UNVERIFIABLE if commit == first
                else real_blob(root, commit, path)
            ),
        )
    # Release 1.0.0 could not be checked: 1.1.0 must not be recorded as deployed.
    assert _deploy(released) == "release_blob_unverifiable"

    monkeypatch.undo()
    assert _deploy(released)["version"] == "1.1.0"


def test_a_deeply_nested_sibling_receipt_cannot_raise_out_of_deploy(released):
    card = {
        "task_id": "T-LATER-NESTED",
        "accepted_at": "2026-09-29T11:00:00Z",
        "accept_evidence": {"accepted_outcome_receipt": "[" * 100000},
    }
    conn = sqlite3.connect(str(task_store.canonical_db_path(released.root)))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, status, worker_status, objective, "
            "card_json, created_at, updated_at, claimed_by, claimed_at, started_at) "
            "VALUES (?, 'codex_worker', 'deploy', 'finished', 'done', 'objective', "
            "?, ?, ?, ?, ?, ?)",
            (
                card["task_id"], json.dumps(card), card["accepted_at"], card["accepted_at"],
                "codex_worker", card["accepted_at"], card["accepted_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # The crafted sibling is skipped; the release bytes are still not this task's.
    assert _deploy(released) == "release_commit_missing_promoted_hash"


def test_an_oversized_sibling_receipt_is_skipped(released, monkeypatch):
    monkeypatch.setattr(sdlc_deploy_proof, "MAX_LATER_RECEIPT_CHARS", 400)
    card = {
        "task_id": "T-LATER-OVERSIZED",
        "accepted_at": "2026-09-29T11:00:00Z",
        "accept_evidence": {"accepted_outcome_receipt": {
            "promoted_paths": [PROMOTED], "changed_path_hashes": {PROMOTED: _sha(RELEASED)},
            "padding": "x" * 1000,
        }},
    }
    conn = sqlite3.connect(str(task_store.canonical_db_path(released.root)))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, runner, topic, status, worker_status, objective, "
            "card_json, created_at, updated_at, claimed_by, claimed_at, started_at) "
            "VALUES (?, 'codex_worker', 'deploy', 'finished', 'done', 'objective', "
            "?, ?, ?, ?, ?, ?)",
            (
                card["task_id"], json.dumps(card), card["accepted_at"], card["accepted_at"],
                "codex_worker", card["accepted_at"], card["accepted_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # The oversized sibling would supersede, but it is never read.
    assert _deploy(released) == "release_commit_missing_promoted_hash"

    _insert_card(
        released.root, "T-LATER-SMALL", accepted_at="2026-09-29T11:00:00Z",
        digest=_sha(RELEASED),
    )
    assert _deploy(released)["superseded_paths"] == [PROMOTED]


def test_a_replace_ref_cannot_change_the_release_blob_digest(released, tmp_path):
    real_blob = _git(released.root, "rev-parse", f"HEAD:{PROMOTED}")
    forged = tmp_path / "forged"
    forged.write_bytes(MINE)
    forged_blob = _git(released.root, "hash-object", "-w", str(forged))
    _git(released.root, "replace", real_blob, forged_blob)
    # Without --no-replace-objects git would serve this task's accepted bytes.
    assert _git(released.root, "cat-file", "blob", f"HEAD:{PROMOTED}") == MINE.decode().strip()

    assert _deploy(released) == "release_commit_missing_promoted_hash"


def test_git_facts_are_remembered_across_calls_but_never_across_a_size_bound(
    released, monkeypatch
):
    calls: list[tuple[str, ...]] = []
    real_git = sdlc_deploy_proof._git

    def counting_git(root, *args):
        calls.append(args)
        return real_git(root, *args)

    monkeypatch.setattr(sdlc_deploy_proof, "_git", counting_git)
    assert _deploy(released) == "release_commit_missing_promoted_hash"
    first = len(calls)
    assert first > 0

    # A commit's bytes never change: the next proof, in or out of a pass, re-reads nothing.
    assert _deploy(released) == "release_commit_missing_promoted_hash"
    with sdlc_deploy_proof.band_report_per_pass():
        assert _deploy(released) == "release_commit_missing_promoted_hash"
    assert len(calls) == first

    # Oversized now: a digest remembered under the old bound must not answer.
    monkeypatch.setattr(sdlc_deploy_proof, "MAX_RELEASE_BLOB_BYTES", 1)
    assert _deploy(released) == "release_blob_oversized"
    assert len(calls) > first

    # And the other way round: a remembered oversize must not answer either.
    monkeypatch.setattr(sdlc_deploy_proof, "MAX_RELEASE_BLOB_BYTES", 16 * 1024 * 1024)
    assert _deploy(released) == "release_commit_missing_promoted_hash"


def test_the_git_fact_cache_never_exceeds_its_cap(released, monkeypatch):
    for name in ("a.txt", "b.txt"):
        (released.root / name).write_bytes(name.encode())
        _git(released.root, "add", name)
    _git(released.root, "commit", "-q", "-m", "more paths")
    head = _git(released.root, "rev-parse", "HEAD")
    monkeypatch.setattr(sdlc_deploy_proof, "MAX_GIT_FACTS", 2)

    for path in (PROMOTED, "a.txt", "b.txt"):
        assert isinstance(sdlc_deploy_proof._blob_sha256(released.root, head, path), str)

    assert len(sdlc_deploy_proof._GIT_FACTS) == 2
