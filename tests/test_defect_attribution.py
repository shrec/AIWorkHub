import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from aiworkhub import defect_attribution, needfix_store, task_engine, task_store


def _git(repo_root, *args):
    result = subprocess.run(
        ["git", *args], cwd=str(repo_root), capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _init_repo(repo_root):
    repo_root.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "init", "-q")
    _git(repo_root, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(repo_root, "config", "user.email", "test@example.com")
    _git(repo_root, "config", "user.name", "Test")
    return repo_root


def _commit(repo_root, path, content, message):
    full = repo_root / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8", newline="")
    _git(repo_root, "add", path)
    _git(repo_root, "commit", "-q", "-m", message)
    return _git(repo_root, "rev-parse", "HEAD").strip()


def _blob_sha256(repo_root, commit, path):
    result = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=str(repo_root), capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    return hashlib.sha256(result.stdout).hexdigest()


def _base_content():
    return "def helper():\n    return 1\n\n\ndef other():\n    return 2\n"


def _build_receipt(*, task_id, request_id, claim_epoch, base_oid, promoted_paths, changed_path_hashes):
    payload = {
        "schema_id": needfix_store.ACCEPTED_OUTCOME_RECEIPT_SCHEMA_ID,
        "task_id": task_id,
        "request_id": request_id,
        "claim_epoch": claim_epoch,
        "base_oid": base_oid,
        "promoted_paths": sorted(promoted_paths),
        "changed_path_hashes": dict(changed_path_hashes),
        "attempt_artifact_manifest_id": hashlib.sha256(
            f"manifest:{task_id}:{request_id}".encode()
        ).hexdigest(),
    }
    payload["repository_revision"] = "sha256:" + hashlib.sha256(
        json.dumps(
            {"base_oid": base_oid, "changed_path_hashes": dict(changed_path_hashes)},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()
    ).hexdigest()
    payload["receipt_id"] = "sha256:" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload


def _identity(*, repository_id, task_id, request_id, receipt):
    return {
        "schema_id": needfix_store.CAUSED_BY_SCHEMA_ID,
        "repository_id": repository_id,
        "task_id": task_id,
        "request_id": request_id,
        "accepted_outcome_receipt": receipt,
    }


def _make_verifier(identities):
    def verify(identity):
        for known in identities:
            if known == identity:
                return {**known, "outcome": "accepted"}
        return None

    return verify


_NEEDFIX_SEQ: list[str] = []


def _new_converted_needfix(repo_root, task_id, *, status="task_created"):
    # A unique description per call: add_needfix dedupes identical content,
    # and callers that need several rows would otherwise get one id back.
    created = needfix_store.add_needfix(
        repo_root, title="bug", description=f"desc {task_id} {len(_NEEDFIX_SEQ)}"
    )
    _NEEDFIX_SEQ.append(created["id"])
    needfix_id = created["id"]
    db_path = repo_root.joinpath(*needfix_store.NEEDFIX_DB_REL)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE needfix SET status = ?, converted_task_id = ? WHERE id = ?",
            (status, task_id, needfix_id),
        )
        conn.commit()
    finally:
        conn.close()
    return needfix_id


def test_attribution_matches_the_introducing_accepted_card(tmp_path):
    repo_root = _init_repo(tmp_path / "repo")
    _commit(repo_root, "src/mod.py", _base_content(), "root")

    buggy_content = _base_content().replace("return 1", "return 1  # BUG")
    commit_a = _commit(repo_root, "src/mod.py", buggy_content, "card A introduces bug")
    hash_a = _blob_sha256(repo_root, commit_a, "src/mod.py")

    fixed_content = _base_content()
    commit_b = _commit(repo_root, "src/mod.py", fixed_content, "card B fixes bug")
    hash_b = _blob_sha256(repo_root, commit_b, "src/mod.py")

    receipt_a = _build_receipt(
        task_id="TASK-A1", request_id="req-a1", claim_epoch=1,
        base_oid=commit_a, promoted_paths=["src/mod.py"], changed_path_hashes={"src/mod.py": hash_a},
    )
    receipt_b = _build_receipt(
        task_id="TASK-B1", request_id="req-b1", claim_epoch=1,
        base_oid=commit_b, promoted_paths=["src/mod.py"], changed_path_hashes={"src/mod.py": hash_b},
    )
    identity_a = _identity(repository_id="repo-one", task_id="TASK-A1", request_id="req-a1", receipt=receipt_a)
    identity_b = _identity(repository_id="repo-one", task_id="TASK-B1", request_id="req-b1", receipt=receipt_b)

    needfix_id = _new_converted_needfix(repo_root, "TASK-B1")

    result = defect_attribution.attribute_needfix(
        repo_root, needfix_id,
        repository_id="repo-one",
        accepted_receipts=[identity_a, identity_b],
        verify_accepted_outcome=_make_verifier([identity_a, identity_b]),
    )

    assert result["outcome"] == "attributed"
    row = needfix_store.get_needfix(repo_root, needfix_id)
    assert row["caused_by"] == identity_a
    assert row["attribution_json"] is None


def test_attribution_writes_non_card_change_when_blame_matches_no_receipt(tmp_path):
    repo_root = _init_repo(tmp_path / "repo")
    _commit(repo_root, "src/mod.py", _base_content(), "root")

    buggy_content = _base_content().replace("return 2", "return 2  # BUG")
    intro_commit = _commit(repo_root, "src/mod.py", buggy_content, "unreviewed direct commit")

    fixed_content = _base_content()
    fix_commit = _commit(repo_root, "src/mod.py", fixed_content, "card B fixes bug")
    hash_fix = _blob_sha256(repo_root, fix_commit, "src/mod.py")

    receipt_b = _build_receipt(
        task_id="TASK-B2", request_id="req-b2", claim_epoch=1,
        base_oid=fix_commit, promoted_paths=["src/mod.py"], changed_path_hashes={"src/mod.py": hash_fix},
    )
    identity_b = _identity(repository_id="repo-one", task_id="TASK-B2", request_id="req-b2", receipt=receipt_b)

    needfix_id = _new_converted_needfix(repo_root, "TASK-B2")

    result = defect_attribution.attribute_needfix(
        repo_root, needfix_id,
        repository_id="repo-one",
        accepted_receipts=[identity_b],
        verify_accepted_outcome=_make_verifier([identity_b]),
    )

    assert result["outcome"] == "non_card"
    row = needfix_store.get_needfix(repo_root, needfix_id)
    assert row["caused_by"] is None
    assert row["attribution_json"]["state"] == "non_card_change"
    assert row["attribution_json"]["method"] == "szz_blame_v1"
    assert row["attribution_json"]["fix_commit"] == fix_commit
    assert intro_commit in row["attribution_json"]["introducing_commits"]


def test_attribution_leaves_row_unknown_on_tie(tmp_path):
    repo_root = _init_repo(tmp_path / "repo")
    base = "line_one\nline_two\nline_three\n"
    _commit(repo_root, "src/mod.py", base, "root")

    buggy_one = "line_one_BUG\nline_two\nline_three\n"
    commit_a1 = _commit(repo_root, "src/mod.py", buggy_one, "card A1")
    hash_a1 = _blob_sha256(repo_root, commit_a1, "src/mod.py")

    buggy_both = "line_one_BUG\nline_two\nline_three_BUG\n"
    commit_a2 = _commit(repo_root, "src/mod.py", buggy_both, "card A2")
    hash_a2 = _blob_sha256(repo_root, commit_a2, "src/mod.py")

    fix_commit = _commit(repo_root, "src/mod.py", base, "card B fixes both")
    hash_fix = _blob_sha256(repo_root, fix_commit, "src/mod.py")

    receipt_a1 = _build_receipt(
        task_id="TASK-A3a", request_id="req-a3a", claim_epoch=1,
        base_oid=commit_a1, promoted_paths=["src/mod.py"], changed_path_hashes={"src/mod.py": hash_a1},
    )
    receipt_a2 = _build_receipt(
        task_id="TASK-A3b", request_id="req-a3b", claim_epoch=1,
        base_oid=commit_a2, promoted_paths=["src/mod.py"], changed_path_hashes={"src/mod.py": hash_a2},
    )
    receipt_b = _build_receipt(
        task_id="TASK-B3", request_id="req-b3", claim_epoch=1,
        base_oid=fix_commit, promoted_paths=["src/mod.py"], changed_path_hashes={"src/mod.py": hash_fix},
    )
    identity_a1 = _identity(repository_id="repo-one", task_id="TASK-A3a", request_id="req-a3a", receipt=receipt_a1)
    identity_a2 = _identity(repository_id="repo-one", task_id="TASK-A3b", request_id="req-a3b", receipt=receipt_a2)
    identity_b = _identity(repository_id="repo-one", task_id="TASK-B3", request_id="req-b3", receipt=receipt_b)

    needfix_id = _new_converted_needfix(repo_root, "TASK-B3")
    receipts = [identity_a1, identity_a2, identity_b]

    result = defect_attribution.attribute_needfix(
        repo_root, needfix_id,
        repository_id="repo-one",
        accepted_receipts=receipts,
        verify_accepted_outcome=_make_verifier(receipts),
    )

    assert result["outcome"] == "unknown"
    assert result["reason"] == "tie"
    row = needfix_store.get_needfix(repo_root, needfix_id)
    assert row["caused_by"] is None
    assert row["attribution_json"] is None


def test_attribution_leaves_row_unknown_for_additive_only_fix(tmp_path):
    repo_root = _init_repo(tmp_path / "repo")
    _commit(repo_root, "src/mod.py", _base_content(), "root")

    fix_commit = _commit(repo_root, "src/extra.py", "print('added')\n", "card B adds a new file only")
    hash_extra = _blob_sha256(repo_root, fix_commit, "src/extra.py")

    receipt_b = _build_receipt(
        task_id="TASK-B4", request_id="req-b4", claim_epoch=1,
        base_oid=fix_commit, promoted_paths=["src/extra.py"], changed_path_hashes={"src/extra.py": hash_extra},
    )
    identity_b = _identity(repository_id="repo-one", task_id="TASK-B4", request_id="req-b4", receipt=receipt_b)

    needfix_id = _new_converted_needfix(repo_root, "TASK-B4")

    result = defect_attribution.attribute_needfix(
        repo_root, needfix_id,
        repository_id="repo-one",
        accepted_receipts=[identity_b],
        verify_accepted_outcome=_make_verifier([identity_b]),
    )

    assert result["outcome"] == "unknown"
    assert result["reason"] == "additive_only_fix"
    row = needfix_store.get_needfix(repo_root, needfix_id)
    assert row["caused_by"] is None
    assert row["attribution_json"] is None


def test_attribution_ignores_content_match_at_a_different_path(tmp_path):
    repo_root = _init_repo(tmp_path / "repo")
    _commit(repo_root, "README.md", "root\n", "init")

    (repo_root / "src").mkdir(parents=True, exist_ok=True)
    (repo_root / "src" / "mod.py").write_text(_base_content(), encoding="utf-8", newline="")
    (repo_root / "pkg" / "b").mkdir(parents=True, exist_ok=True)
    (repo_root / "pkg" / "b" / "__init__.py").write_text("STUB\n", encoding="utf-8", newline="")
    _git(repo_root, "add", "src/mod.py", "pkg/b/__init__.py")
    _git(repo_root, "commit", "-q", "-m", "introduce src and an unrelated stub")
    introduce_commit = _git(repo_root, "rev-parse", "HEAD").strip()
    stub_hash = _blob_sha256(repo_root, introduce_commit, "pkg/b/__init__.py")

    fix_content = _base_content().replace("return 1", "return 100")
    fix_commit = _commit(repo_root, "src/mod.py", fix_content, "card fixes the bug")
    hash_mod = _blob_sha256(repo_root, fix_commit, "src/mod.py")

    receipt_fix = _build_receipt(
        task_id="TASK-FIX", request_id="req-fix", claim_epoch=1,
        base_oid=fix_commit, promoted_paths=["src/mod.py"], changed_path_hashes={"src/mod.py": hash_mod},
    )
    identity_fix = _identity(repository_id="repo-one", task_id="TASK-FIX", request_id="req-fix", receipt=receipt_fix)

    receipt_other = _build_receipt(
        task_id="TASK-A", request_id="req-a", claim_epoch=1,
        base_oid=introduce_commit, promoted_paths=["pkg/a/__init__.py"],
        changed_path_hashes={"pkg/a/__init__.py": stub_hash},
    )
    identity_other = _identity(repository_id="repo-one", task_id="TASK-A", request_id="req-a", receipt=receipt_other)

    accepted_receipts = [identity_fix, identity_other]

    index = defect_attribution._receipt_hash_index(accepted_receipts, "repo-one")
    assert defect_attribution._match_commit_to_identity_key(repo_root, introduce_commit, index) is None

    needfix_id = _new_converted_needfix(repo_root, "TASK-FIX")
    result = defect_attribution.attribute_needfix(
        repo_root, needfix_id,
        repository_id="repo-one",
        accepted_receipts=accepted_receipts,
        verify_accepted_outcome=_make_verifier(accepted_receipts),
    )

    assert result["outcome"] == "non_card"
    row = needfix_store.get_needfix(repo_root, needfix_id)
    assert row["caused_by"] is None
    assert row["attribution_json"]["state"] == "non_card_change"


def test_attribution_leaves_row_unknown_when_fix_commit_is_missing(tmp_path):
    repo_root = _init_repo(tmp_path / "repo")
    _commit(repo_root, "src/mod.py", _base_content(), "root")

    receipt_ghost = _build_receipt(
        task_id="TASK-B5", request_id="req-b5", claim_epoch=1,
        base_oid="0" * 40, promoted_paths=["src/mod.py"], changed_path_hashes={"src/mod.py": "f" * 64},
    )
    identity_ghost = _identity(repository_id="repo-one", task_id="TASK-B5", request_id="req-b5", receipt=receipt_ghost)

    needfix_id = _new_converted_needfix(repo_root, "TASK-B5")

    result = defect_attribution.attribute_needfix(
        repo_root, needfix_id,
        repository_id="repo-one",
        accepted_receipts=[identity_ghost],
        verify_accepted_outcome=_make_verifier([identity_ghost]),
    )

    assert result["outcome"] == "unknown"
    assert result["reason"] == "fix_commit_not_found"
    row = needfix_store.get_needfix(repo_root, needfix_id)
    assert row["caused_by"] is None
    assert row["attribution_json"] is None


def test_attribution_is_write_once(tmp_path):
    repo_root = _init_repo(tmp_path / "repo")
    _commit(repo_root, "src/mod.py", _base_content(), "root")
    buggy_content = _base_content().replace("return 1", "return 1  # BUG")
    commit_a = _commit(repo_root, "src/mod.py", buggy_content, "card A introduces bug")
    hash_a = _blob_sha256(repo_root, commit_a, "src/mod.py")
    fixed_content = _base_content()
    commit_b = _commit(repo_root, "src/mod.py", fixed_content, "card B fixes bug")
    hash_b = _blob_sha256(repo_root, commit_b, "src/mod.py")

    receipt_a = _build_receipt(
        task_id="TASK-A6", request_id="req-a6", claim_epoch=1,
        base_oid=commit_a, promoted_paths=["src/mod.py"], changed_path_hashes={"src/mod.py": hash_a},
    )
    receipt_b = _build_receipt(
        task_id="TASK-B6", request_id="req-b6", claim_epoch=1,
        base_oid=commit_b, promoted_paths=["src/mod.py"], changed_path_hashes={"src/mod.py": hash_b},
    )
    identity_a = _identity(repository_id="repo-one", task_id="TASK-A6", request_id="req-a6", receipt=receipt_a)
    identity_b = _identity(repository_id="repo-one", task_id="TASK-B6", request_id="req-b6", receipt=receipt_b)

    needfix_id = _new_converted_needfix(repo_root, "TASK-B6")
    receipts = [identity_a, identity_b]
    verifier = _make_verifier(receipts)

    first = defect_attribution.attribute_needfix(
        repo_root, needfix_id, repository_id="repo-one",
        accepted_receipts=receipts, verify_accepted_outcome=verifier,
    )
    assert first["outcome"] == "attributed"

    second = defect_attribution.attribute_needfix(
        repo_root, needfix_id, repository_id="repo-one",
        accepted_receipts=receipts, verify_accepted_outcome=verifier,
    )
    assert second["outcome"] == "skipped"
    assert second["reason"] == "already_attributed"

    row = needfix_store.get_needfix(repo_root, needfix_id)
    assert row["caused_by"] == identity_a


def test_close_for_accepted_task_closes_even_when_attribution_raises(tmp_path, monkeypatch):
    repo_root = _init_repo(tmp_path / "repo")
    _commit(repo_root, "src/mod.py", _base_content(), "root")
    created = needfix_store.add_needfix(repo_root, title="bug", description="desc")
    needfix_id = created["id"]
    db_path = repo_root.joinpath(*needfix_store.NEEDFIX_DB_REL)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE needfix SET status = 'task_created', converted_task_id = ? WHERE id = ?",
            ("TASK-X7", needfix_id),
        )
        conn.commit()
    finally:
        conn.close()

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(defect_attribution, "attribute_needfix", _boom)

    result = needfix_store.close_for_accepted_task(
        repo_root, "TASK-X7",
        accepted_request_id="req-x7",
        repository_id="repo-one",
        accepted_receipts=(),
        verify_accepted_outcome=lambda identity: None,
    )

    assert result["state"] == "closed"


def test_close_for_accepted_task_without_verifier_skips_attribution(tmp_path):
    repo_root = _init_repo(tmp_path / "repo")
    created = needfix_store.add_needfix(repo_root, title="bug", description="desc")
    needfix_id = created["id"]
    db_path = repo_root.joinpath(*needfix_store.NEEDFIX_DB_REL)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE needfix SET status = 'task_created', converted_task_id = ? WHERE id = ?",
            ("TASK-X8", needfix_id),
        )
        conn.commit()
    finally:
        conn.close()

    result = needfix_store.close_for_accepted_task(
        repo_root, "TASK-X8", accepted_request_id="req-x8",
    )

    assert result["state"] == "closed"


def test_attribute_many_parallel_matches_sequential(tmp_path):
    repo_root = _init_repo(tmp_path / "repo")
    _commit(repo_root, "src/mod.py", _base_content(), "root")
    buggy_content = _base_content().replace("return 1", "return 1  # BUG")
    commit_a = _commit(repo_root, "src/mod.py", buggy_content, "card A introduces bug")
    hash_a = _blob_sha256(repo_root, commit_a, "src/mod.py")
    fixed_content = _base_content()
    commit_b = _commit(repo_root, "src/mod.py", fixed_content, "card B fixes bug")
    hash_b = _blob_sha256(repo_root, commit_b, "src/mod.py")

    receipt_a = _build_receipt(
        task_id="TASK-A9", request_id="req-a9", claim_epoch=1,
        base_oid=commit_a, promoted_paths=["src/mod.py"], changed_path_hashes={"src/mod.py": hash_a},
    )
    receipt_b = _build_receipt(
        task_id="TASK-B9", request_id="req-b9", claim_epoch=1,
        base_oid=commit_b, promoted_paths=["src/mod.py"], changed_path_hashes={"src/mod.py": hash_b},
    )
    identity_a = _identity(repository_id="repo-one", task_id="TASK-A9", request_id="req-a9", receipt=receipt_a)
    identity_b = _identity(repository_id="repo-one", task_id="TASK-B9", request_id="req-b9", receipt=receipt_b)
    receipts = [identity_a, identity_b]
    verifier = _make_verifier(receipts)

    needfix_ids = [
        _new_converted_needfix(repo_root, "TASK-B9"),
        _new_converted_needfix(repo_root, "TASK-B9"),
        _new_converted_needfix(repo_root, "TASK-B9"),
    ]

    sequential = defect_attribution.attribute_many(
        repo_root, needfix_ids, repository_id="repo-one",
        accepted_receipts=receipts, verify_accepted_outcome=verifier,
        dry_run=True, max_workers=1,
    )
    parallel = defect_attribution.attribute_many(
        repo_root, needfix_ids, repository_id="repo-one",
        accepted_receipts=receipts, verify_accepted_outcome=verifier,
        dry_run=True, max_workers=8,
    )

    assert sequential == parallel
    assert all(r["outcome"] == "attributed" for r in sequential)


def test_attribution_leaves_row_unknown_for_ambiguous_blame(tmp_path):
    repo_root = _init_repo(tmp_path / "repo")
    _commit(repo_root, "src/mod.py", _base_content(), "root mod")
    _commit(repo_root, "src/other.py", "def helper2():\n    return 3\n", "root other")

    buggy_mod = _base_content().replace("return 1", "return 1  # BUG")
    buggy_other = "def helper2():\n    return 3  # BUG\n"
    (repo_root / "src" / "mod.py").write_text(buggy_mod, encoding="utf-8", newline="")
    (repo_root / "src" / "other.py").write_text(buggy_other, encoding="utf-8", newline="")
    _git(repo_root, "add", "src/mod.py", "src/other.py")
    _git(repo_root, "commit", "-q", "-m", "ambiguous: touches two files")
    ambiguous_commit = _git(repo_root, "rev-parse", "HEAD").strip()
    hash_mod_buggy = _blob_sha256(repo_root, ambiguous_commit, "src/mod.py")
    hash_other_buggy = _blob_sha256(repo_root, ambiguous_commit, "src/other.py")

    fixed_content = _base_content()
    fix_commit = _commit(repo_root, "src/mod.py", fixed_content, "card B fixes mod.py only")
    hash_fix = _blob_sha256(repo_root, fix_commit, "src/mod.py")

    receipt_p = _build_receipt(
        task_id="TASK-P1", request_id="req-p1", claim_epoch=1,
        base_oid=ambiguous_commit, promoted_paths=["src/mod.py"],
        changed_path_hashes={"src/mod.py": hash_mod_buggy},
    )
    receipt_q = _build_receipt(
        task_id="TASK-Q1", request_id="req-q1", claim_epoch=1,
        base_oid=ambiguous_commit, promoted_paths=["src/other.py"],
        changed_path_hashes={"src/other.py": hash_other_buggy},
    )
    receipt_b = _build_receipt(
        task_id="TASK-B12", request_id="req-b12", claim_epoch=1,
        base_oid=fix_commit, promoted_paths=["src/mod.py"],
        changed_path_hashes={"src/mod.py": hash_fix},
    )
    identity_p = _identity(repository_id="repo-one", task_id="TASK-P1", request_id="req-p1", receipt=receipt_p)
    identity_q = _identity(repository_id="repo-one", task_id="TASK-Q1", request_id="req-q1", receipt=receipt_q)
    identity_b = _identity(repository_id="repo-one", task_id="TASK-B12", request_id="req-b12", receipt=receipt_b)

    needfix_id = _new_converted_needfix(repo_root, "TASK-B12")
    receipts = [identity_p, identity_q, identity_b]

    result = defect_attribution.attribute_needfix(
        repo_root, needfix_id,
        repository_id="repo-one",
        accepted_receipts=receipts,
        verify_accepted_outcome=_make_verifier(receipts),
    )

    assert result["outcome"] == "unknown"
    assert result["reason"] == "ambiguous_blame"
    row = needfix_store.get_needfix(repo_root, needfix_id)
    assert row["caused_by"] is None
    assert row["attribution_json"] is None


def test_attribution_leaves_row_unknown_for_mixed_blame(tmp_path):
    repo_root = _init_repo(tmp_path / "repo")
    paths = ["src/f1.py", "src/f2.py", "src/f3.py", "src/f4.py", "src/f5.py"]
    base_lines = {p: f"value = {i}\n" for i, p in enumerate(paths)}
    for p in paths:
        _commit(repo_root, p, base_lines[p], f"root {p}")

    card_a_hashes = {}
    for p in ("src/f1.py", "src/f2.py"):
        buggy = base_lines[p].replace("value", "value_BUG")
        commit = _commit(repo_root, p, buggy, f"card A touches {p}")
        card_a_hashes[p] = _blob_sha256(repo_root, commit, p)
    for p in ("src/f3.py", "src/f4.py", "src/f5.py"):
        buggy = base_lines[p].replace("value", "value_BUG")
        _commit(repo_root, p, buggy, f"unreviewed touches {p}")

    for p in paths:
        (repo_root / p).write_text(base_lines[p], encoding="utf-8", newline="")
    _git(repo_root, "add", *paths)
    _git(repo_root, "commit", "-q", "-m", "card B fixes all five")
    fix_commit = _git(repo_root, "rev-parse", "HEAD").strip()
    fix_hashes = {p: _blob_sha256(repo_root, fix_commit, p) for p in paths}

    receipt_a = _build_receipt(
        task_id="TASK-A13", request_id="req-a13", claim_epoch=1,
        base_oid=fix_commit, promoted_paths=["src/f1.py", "src/f2.py"],
        changed_path_hashes=card_a_hashes,
    )
    receipt_b = _build_receipt(
        task_id="TASK-B13", request_id="req-b13", claim_epoch=1,
        base_oid=fix_commit, promoted_paths=paths, changed_path_hashes=fix_hashes,
    )
    identity_a = _identity(repository_id="repo-one", task_id="TASK-A13", request_id="req-a13", receipt=receipt_a)
    identity_b = _identity(repository_id="repo-one", task_id="TASK-B13", request_id="req-b13", receipt=receipt_b)

    needfix_id = _new_converted_needfix(repo_root, "TASK-B13")
    receipts = [identity_a, identity_b]

    result = defect_attribution.attribute_needfix(
        repo_root, needfix_id,
        repository_id="repo-one",
        accepted_receipts=receipts,
        verify_accepted_outcome=_make_verifier(receipts),
    )

    assert result["outcome"] == "unknown"
    assert result["reason"] == "mixed_blame"
    row = needfix_store.get_needfix(repo_root, needfix_id)
    assert row["caused_by"] is None
    assert row["attribution_json"] is None


def test_attribution_leaves_row_unknown_for_split_vote_without_majority(tmp_path):
    repo_root = _init_repo(tmp_path / "repo")
    paths = ["src/g1.py", "src/g2.py", "src/g3.py", "src/g4.py"]
    base_lines = {p: f"value = {i}\n" for i, p in enumerate(paths)}
    for p in paths:
        _commit(repo_root, p, base_lines[p], f"root {p}")

    card_a_hashes = {}
    for p in ("src/g1.py", "src/g2.py"):
        buggy = base_lines[p].replace("value", "value_BUG")
        commit = _commit(repo_root, p, buggy, f"card A touches {p}")
        card_a_hashes[p] = _blob_sha256(repo_root, commit, p)

    buggy_g3 = base_lines["src/g3.py"].replace("value", "value_BUG")
    commit_b = _commit(repo_root, "src/g3.py", buggy_g3, "card B touches g3")
    hash_b = _blob_sha256(repo_root, commit_b, "src/g3.py")

    buggy_g4 = base_lines["src/g4.py"].replace("value", "value_BUG")
    commit_c = _commit(repo_root, "src/g4.py", buggy_g4, "card C touches g4")
    hash_c = _blob_sha256(repo_root, commit_c, "src/g4.py")

    for p in paths:
        (repo_root / p).write_text(base_lines[p], encoding="utf-8", newline="")
    _git(repo_root, "add", *paths)
    _git(repo_root, "commit", "-q", "-m", "card D fixes all four")
    fix_commit = _git(repo_root, "rev-parse", "HEAD").strip()
    fix_hashes = {p: _blob_sha256(repo_root, fix_commit, p) for p in paths}

    receipt_a = _build_receipt(
        task_id="TASK-A14", request_id="req-a14", claim_epoch=1,
        base_oid=fix_commit, promoted_paths=["src/g1.py", "src/g2.py"],
        changed_path_hashes=card_a_hashes,
    )
    receipt_b = _build_receipt(
        task_id="TASK-B14", request_id="req-b14", claim_epoch=1,
        base_oid=fix_commit, promoted_paths=["src/g3.py"], changed_path_hashes={"src/g3.py": hash_b},
    )
    receipt_c = _build_receipt(
        task_id="TASK-C14", request_id="req-c14", claim_epoch=1,
        base_oid=fix_commit, promoted_paths=["src/g4.py"], changed_path_hashes={"src/g4.py": hash_c},
    )
    receipt_d = _build_receipt(
        task_id="TASK-D14", request_id="req-d14", claim_epoch=1,
        base_oid=fix_commit, promoted_paths=paths, changed_path_hashes=fix_hashes,
    )
    identity_a = _identity(repository_id="repo-one", task_id="TASK-A14", request_id="req-a14", receipt=receipt_a)
    identity_b = _identity(repository_id="repo-one", task_id="TASK-B14", request_id="req-b14", receipt=receipt_b)
    identity_c = _identity(repository_id="repo-one", task_id="TASK-C14", request_id="req-c14", receipt=receipt_c)
    identity_d = _identity(repository_id="repo-one", task_id="TASK-D14", request_id="req-d14", receipt=receipt_d)

    needfix_id = _new_converted_needfix(repo_root, "TASK-D14")
    receipts = [identity_a, identity_b, identity_c, identity_d]

    result = defect_attribution.attribute_needfix(
        repo_root, needfix_id,
        repository_id="repo-one",
        accepted_receipts=receipts,
        verify_accepted_outcome=_make_verifier(receipts),
    )

    assert result["outcome"] == "unknown"
    row = needfix_store.get_needfix(repo_root, needfix_id)
    assert row["caused_by"] is None
    assert row["attribution_json"] is None


def test_deleted_modified_src_lines_handles_leading_dashdash_content(tmp_path):
    repo_root = _init_repo(tmp_path / "repo")
    base = "line1\n-- marker\nline3\nline4\nline5\nline6_OLD\n"
    _commit(repo_root, "src/mod.py", base, "root")
    changed = "line1\nline3\nline4\nline5\nline6_NEW\n"
    fix_commit = _commit(repo_root, "src/mod.py", changed, "delete a '-- ' line and change another")
    parent_commit = _git(repo_root, "rev-parse", f"{fix_commit}^").strip()

    result = defect_attribution._deleted_modified_src_lines(repo_root, fix_commit, parent_commit)

    assert list(result.keys()) == ["src/mod.py"]
    assert len(result["src/mod.py"]) >= 2


def test_attribution_leaves_row_unknown_when_fix_receipt_is_malformed(tmp_path):
    repo_root = _init_repo(tmp_path / "repo")
    _commit(repo_root, "src/mod.py", _base_content(), "root")

    needfix_id = _new_converted_needfix(repo_root, "TASK-B15")
    malformed_identity = {
        "schema_id": needfix_store.CAUSED_BY_SCHEMA_ID,
        "repository_id": "repo-one",
        "task_id": "TASK-B15",
        "request_id": "req-b15",
        "accepted_outcome_receipt": "not-a-mapping",
    }

    result = defect_attribution.attribute_needfix(
        repo_root, needfix_id,
        repository_id="repo-one",
        accepted_receipts=[malformed_identity],
        verify_accepted_outcome=lambda identity: None,
    )

    assert result["outcome"] == "unknown"
    assert result["reason"] == "fix_receipt_unavailable"
    row = needfix_store.get_needfix(repo_root, needfix_id)
    assert row["caused_by"] is None
    assert row["attribution_json"] is None


def test_attribute_many_dry_run_false_parallel_matches_sequential(tmp_path):
    repo_root = _init_repo(tmp_path / "repo")
    _commit(repo_root, "src/mod.py", _base_content(), "root")
    buggy_content = _base_content().replace("return 1", "return 1  # BUG")
    commit_a = _commit(repo_root, "src/mod.py", buggy_content, "card A introduces bug")
    hash_a = _blob_sha256(repo_root, commit_a, "src/mod.py")
    fixed_content = _base_content()
    commit_b = _commit(repo_root, "src/mod.py", fixed_content, "card B fixes bug")
    hash_b = _blob_sha256(repo_root, commit_b, "src/mod.py")

    receipt_a = _build_receipt(
        task_id="TASK-A11", request_id="req-a11", claim_epoch=1,
        base_oid=commit_a, promoted_paths=["src/mod.py"], changed_path_hashes={"src/mod.py": hash_a},
    )
    receipt_b = _build_receipt(
        task_id="TASK-B11", request_id="req-b11", claim_epoch=1,
        base_oid=commit_b, promoted_paths=["src/mod.py"], changed_path_hashes={"src/mod.py": hash_b},
    )
    identity_a = _identity(repository_id="repo-one", task_id="TASK-A11", request_id="req-a11", receipt=receipt_a)
    identity_b = _identity(repository_id="repo-one", task_id="TASK-B11", request_id="req-b11", receipt=receipt_b)
    receipts = [identity_a, identity_b]
    verifier = _make_verifier(receipts)

    sequential_ids = [_new_converted_needfix(repo_root, "TASK-B11") for _ in range(3)]
    parallel_ids = [_new_converted_needfix(repo_root, "TASK-B11") for _ in range(3)]

    sequential = defect_attribution.attribute_many(
        repo_root, sequential_ids, repository_id="repo-one",
        accepted_receipts=receipts, verify_accepted_outcome=verifier,
        dry_run=False, max_workers=1,
    )
    parallel = defect_attribution.attribute_many(
        repo_root, parallel_ids, repository_id="repo-one",
        accepted_receipts=receipts, verify_accepted_outcome=verifier,
        dry_run=False, max_workers=8,
    )

    def _normalized(results):
        return [
            {k: v for k, v in r.items() if k not in ("needfix_id", "update_receipt")}
            for r in results
        ]

    assert _normalized(sequential) == _normalized(parallel)
    assert all(r["outcome"] == "attributed" for r in sequential + parallel)
    for needfix_id in sequential_ids + parallel_ids:
        row = needfix_store.get_needfix(repo_root, needfix_id)
        assert row["caused_by"] == identity_a


def test_close_for_accepted_task_populates_caused_by_via_nested_write(tmp_path):
    repo_root = _init_repo(tmp_path / "repo")
    _commit(repo_root, "src/mod.py", _base_content(), "root")
    buggy_content = _base_content().replace("return 1", "return 1  # BUG")
    commit_a = _commit(repo_root, "src/mod.py", buggy_content, "card A introduces bug")
    hash_a = _blob_sha256(repo_root, commit_a, "src/mod.py")
    fixed_content = _base_content()
    commit_b = _commit(repo_root, "src/mod.py", fixed_content, "card B fixes bug")
    hash_b = _blob_sha256(repo_root, commit_b, "src/mod.py")

    receipt_a = _build_receipt(
        task_id="TASK-A16", request_id="req-a16", claim_epoch=1,
        base_oid=commit_a, promoted_paths=["src/mod.py"], changed_path_hashes={"src/mod.py": hash_a},
    )
    receipt_b = _build_receipt(
        task_id="TASK-B16", request_id="req-b16", claim_epoch=1,
        base_oid=commit_b, promoted_paths=["src/mod.py"], changed_path_hashes={"src/mod.py": hash_b},
    )
    identity_a = _identity(repository_id="repo-one", task_id="TASK-A16", request_id="req-a16", receipt=receipt_a)
    identity_b = _identity(repository_id="repo-one", task_id="TASK-B16", request_id="req-b16", receipt=receipt_b)

    created = needfix_store.add_needfix(repo_root, title="bug", description="desc")
    needfix_id = created["id"]
    db_path = repo_root.joinpath(*needfix_store.NEEDFIX_DB_REL)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE needfix SET status = 'task_created', converted_task_id = ? WHERE id = ?",
            ("TASK-B16", needfix_id),
        )
        conn.commit()
    finally:
        conn.close()

    receipts = [identity_a, identity_b]
    result = needfix_store.close_for_accepted_task(
        repo_root, "TASK-B16",
        accepted_request_id="req-b16",
        repository_id="repo-one",
        accepted_receipts=receipts,
        verify_accepted_outcome=_make_verifier(receipts),
    )

    assert result["state"] == "closed"
    row = needfix_store.get_needfix(repo_root, needfix_id)
    assert row["caused_by"] == identity_a


def test_backfill_script_dry_run_then_apply(tmp_path):
    repo_root = _init_repo(tmp_path / "repo")
    _commit(repo_root, "src/mod.py", _base_content(), "root")
    buggy_content = _base_content().replace("return 1", "return 1  # BUG")
    commit_a = _commit(repo_root, "src/mod.py", buggy_content, "card A introduces bug")
    hash_a = _blob_sha256(repo_root, commit_a, "src/mod.py")
    fixed_content = _base_content()
    commit_b = _commit(repo_root, "src/mod.py", fixed_content, "card B fixes bug")
    hash_b = _blob_sha256(repo_root, commit_b, "src/mod.py")

    receipt_a = _build_receipt(
        task_id="TASK-A10", request_id="req-a10", claim_epoch=1,
        base_oid=commit_a, promoted_paths=["src/mod.py"], changed_path_hashes={"src/mod.py": hash_a},
    )
    receipt_b = _build_receipt(
        task_id="TASK-B10", request_id="req-b10", claim_epoch=1,
        base_oid=commit_b, promoted_paths=["src/mod.py"], changed_path_hashes={"src/mod.py": hash_b},
    )

    needfix_id = _new_converted_needfix(repo_root, "TASK-B10")

    task_store.initialize_repository(repo_root)
    readiness = task_store.storage_readiness(repo_root)
    assert readiness.ready, readiness.reason

    # Seed the accept_review events the backfill reads. The live accept_review
    # gate needs sealed attempt evidence and canonical hashes of the current
    # tree, which a replayed history (A's buggy blob is gone) cannot supply.
    conn = sqlite3.connect(readiness.canonical_db)
    try:
        conn.executemany(
            "INSERT INTO task_events(task_id, event, payload_json, created_at) VALUES (?, ?, ?, ?)",
            [
                (
                    task_id, "accept_review",
                    json.dumps({"request_id": request_id, "accepted_outcome_receipt": receipt}),
                    "2026-01-01T00:00:00Z",
                )
                for task_id, request_id, receipt in (
                    ("TASK-A10", "req-a10", receipt_a),
                    ("TASK-B10", "req-b10", receipt_b),
                )
            ],
        )
        conn.commit()
    finally:
        conn.close()

    script = Path(__file__).resolve().parent.parent / "scripts" / "needfix_attribution_backfill.py"
    common = [sys.executable, str(script), str(repo_root)]

    dry = subprocess.run(common, capture_output=True, text=True, timeout=60)
    assert dry.returncode == 0, dry.stderr
    summary = json.loads(dry.stdout)
    assert summary["dry_run"] is True
    assert summary["attributed"] == 1
    assert needfix_store.get_needfix(repo_root, needfix_id)["caused_by"] is None

    applied = subprocess.run(common + ["--apply"], capture_output=True, text=True, timeout=60)
    assert applied.returncode == 0, applied.stderr
    summary2 = json.loads(applied.stdout)
    assert summary2["dry_run"] is False
    assert summary2["attributed"] == 1
    row = needfix_store.get_needfix(repo_root, needfix_id)
    assert (row["caused_by"] or {}).get("task_id") == "TASK-A10"
