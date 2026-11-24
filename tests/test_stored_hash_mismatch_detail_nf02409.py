"""NF-2026-02409: ``stored_hash_mismatch`` names the drifted paths and the cause.

``accept_review`` refused with a bare ``stored_hash_mismatch`` when the retained
worktree's changed-path hashes no longer matched the sealed candidate's. A
3-round consumer card hit it twice with unchanged candidate bytes. Two causes
are reproduced here: the declared validation (run INSIDE the workspace) rewrote
a changed path, and a path created in an early round and later deleted stayed
in one key set. The refusal now carries compact JSON naming the paths, says
when validation wrote into the tree, ignores a path absent from both trees, and
still refuses on any real byte change.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

from aiworkhub import process_launcher
from aiworkhub import process_launcher_accept_review as accept_review_module
from aiworkhub.process_launcher_acceptance import changed_path_hashes

refusal = accept_review_module._stored_hash_mismatch_refusal
PREFIX = "stored_hash_mismatch:"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _trees(tmp_path: Path) -> tuple[Path, Path]:
    candidate = tmp_path / "candidate"
    canonical = tmp_path / "canonical"
    candidate.mkdir()
    canonical.mkdir()
    return candidate, canonical


def _detail(message: str | None) -> dict:
    assert message is not None and message.startswith(PREFIX), message
    return json.loads(message[len(PREFIX):])


def _hashes(root: Path, changed: list[str]) -> dict[str, str | None]:
    return changed_path_hashes(SimpleNamespace(path=root), changed)


def test_refusal_names_only_stored_only_current_and_digest_changed(tmp_path):
    candidate, canonical = _trees(tmp_path)
    (candidate / "kept.py").write_bytes(b"new bytes\n")
    (candidate / "extra.py").write_bytes(b"extra\n")
    (candidate / "stored_only.py").write_bytes(b"still here\n")
    old = _sha(b"old bytes\n")
    accepted = {"kept.py": old, "stored_only.py": _sha(b"still here\n")}
    current = _hashes(candidate, ["kept.py", "extra.py"])

    message = refusal(
        accepted, current, candidate_root=candidate, canonical_root=canonical
    )

    detail = _detail(message)
    assert detail["only_stored"] == ["stored_only.py"]
    assert detail["only_current"] == ["extra.py"]
    assert detail["digest_changed"] == [
        {
            "path": "kept.py",
            "stored": old[:12],
            "current": _sha(b"new bytes\n")[:12],
        }
    ]
    # Compact, deterministic JSON; no validation baseline means no attribution.
    assert message == PREFIX + json.dumps(detail, sort_keys=True, separators=(",", ":"))
    assert "mutated_by_validation" not in detail
    assert "omitted" not in detail


def test_validation_that_rewrites_a_changed_file_is_named(tmp_path):
    candidate, canonical = _trees(tmp_path)
    fixture = candidate / "fixtures" / "golden.json"
    fixture.parent.mkdir()
    fixture.write_bytes(b'{"v": 1}\n')
    (candidate / "module.py").write_bytes(b"x = 1\n")
    changed = ["fixtures/golden.json", "module.py"]
    accepted = _hashes(candidate, changed)

    before_validation = _hashes(candidate, changed)
    # A real validation step that regenerates a fixture inside the tree.
    script = textwrap.dedent(
        """
        import sys, pathlib
        pathlib.Path(sys.argv[1]).write_text('{"v": 2}\\n', encoding="utf-8")
        """
    )
    subprocess.run(
        [sys.executable, "-c", script, str(fixture)], check=True, timeout=60
    )
    current = _hashes(candidate, changed)

    detail = _detail(
        refusal(
            accepted,
            current,
            candidate_root=candidate,
            canonical_root=canonical,
            before_validation=before_validation,
        )
    )
    assert detail["mutated_by_validation"] == ["fixtures/golden.json"]
    assert [row["path"] for row in detail["digest_changed"]] == [
        "fixtures/golden.json"
    ]
    assert detail["only_stored"] == [] and detail["only_current"] == []


def test_drift_before_validation_is_not_blamed_on_validation(tmp_path):
    candidate, canonical = _trees(tmp_path)
    (candidate / "a.py").write_bytes(b"edited after sealing\n")
    accepted = {"a.py": _sha(b"sealed\n")}
    before_validation = _hashes(candidate, ["a.py"])
    current = _hashes(candidate, ["a.py"])

    detail = _detail(
        refusal(
            accepted,
            current,
            candidate_root=candidate,
            canonical_root=canonical,
            before_validation=before_validation,
        )
    )
    assert [row["path"] for row in detail["digest_changed"]] == ["a.py"]
    assert "mutated_by_validation" not in detail


def test_path_deleted_after_an_earlier_round_and_absent_from_canonical_passes(
    tmp_path,
):
    candidate, canonical = _trees(tmp_path)
    (candidate / "a.py").write_bytes(b"a\n")
    # Round 1 created scratch.json, round 2 deleted it: the sealed record still
    # lists it (as absent) while the accept-time changed set no longer does.
    accepted = {"a.py": _sha(b"a\n"), "scratch.json": None}
    current = _hashes(candidate, ["a.py"])
    assert refusal(
        accepted, current, candidate_root=candidate, canonical_root=canonical
    ) is None
    # The mirror case: the accept-time set names it, the sealed record does not.
    assert refusal(
        {"a.py": _sha(b"a\n")},
        _hashes(candidate, ["a.py", "scratch.json"]),
        candidate_root=candidate,
        canonical_root=canonical,
    ) is None


def test_deleted_path_still_present_in_canonical_refuses(tmp_path):
    candidate, canonical = _trees(tmp_path)
    (candidate / "a.py").write_bytes(b"a\n")
    (canonical / "scratch.json").write_bytes(b"{}\n")
    accepted = {"a.py": _sha(b"a\n"), "scratch.json": None}
    detail = _detail(
        refusal(
            accepted,
            _hashes(candidate, ["a.py"]),
            candidate_root=candidate,
            canonical_root=canonical,
        )
    )
    assert detail["only_stored"] == ["scratch.json"]


def test_unconfined_absent_key_is_never_exempt(tmp_path):
    candidate, canonical = _trees(tmp_path)
    (candidate / "a.py").write_bytes(b"a\n")
    for key in ("../escape.json", "/rooted.json", "\\rooted.json", "C:/drive.json"):
        detail = _detail(
            refusal(
                {"a.py": _sha(b"a\n"), key: None},
                _hashes(candidate, ["a.py"]),
                candidate_root=candidate,
                canonical_root=canonical,
            )
        )
        assert detail["only_stored"] == [key], key


def test_recorded_bytes_for_a_now_missing_path_still_refuse(tmp_path):
    candidate, canonical = _trees(tmp_path)
    (candidate / "a.py").write_bytes(b"a\n")
    accepted = {"a.py": _sha(b"a\n"), "gone.py": _sha(b"was reviewed\n")}
    detail = _detail(
        refusal(
            accepted,
            _hashes(candidate, ["a.py"]),
            candidate_root=candidate,
            canonical_root=canonical,
        )
    )
    assert detail["only_stored"] == ["gone.py"]


def test_real_byte_change_still_refuses_and_identical_bytes_pass(tmp_path):
    candidate, canonical = _trees(tmp_path)
    target = candidate / "a.py"
    target.write_bytes(b"a\n")
    accepted = _hashes(candidate, ["a.py"])
    assert refusal(
        accepted,
        _hashes(candidate, ["a.py"]),
        candidate_root=candidate,
        canonical_root=canonical,
    ) is None

    target.write_bytes(b"a!\n")
    detail = _detail(
        refusal(
            accepted,
            _hashes(candidate, ["a.py"]),
            candidate_root=candidate,
            canonical_root=canonical,
        )
    )
    assert detail["digest_changed"][0]["path"] == "a.py"

    # A file deleted from the candidate tree is drift too, not a tolerated absence.
    target.unlink()
    detail = _detail(
        refusal(
            accepted,
            _hashes(candidate, ["a.py"]),
            candidate_root=candidate,
            canonical_root=canonical,
        )
    )
    assert detail["digest_changed"] == [
        {"path": "a.py", "stored": accepted["a.py"][:12], "current": None}
    ]


def test_lists_are_bounded_and_say_how_much_was_omitted(tmp_path):
    candidate, canonical = _trees(tmp_path)
    names = [f"p{index:02d}.py" for index in range(20)]
    accepted = {name: _sha(b"old") for name in names}
    current = {name: _sha(b"new") for name in names}
    detail = _detail(
        refusal(
            accepted,
            current,
            candidate_root=candidate,
            canonical_root=canonical,
            before_validation=dict(accepted),
        )
    )
    assert len(detail["digest_changed"]) == 12
    assert len(detail["mutated_by_validation"]) == 12
    assert detail["omitted"] == {"digest_changed": 8, "mutated_by_validation": 8}


def test_accept_review_hashes_before_validation_and_raises_the_detail():
    """The wiring: one helper owns the refusal; validation is bracketed."""
    assert "_stored_hash_mismatch_refusal" in accept_review_module.ACCEPT_REVIEW_LOCAL_NAMES
    assert not hasattr(process_launcher, "_stored_hash_mismatch_refusal")

    source = inspect.getsource(accept_review_module.accept_review)
    assert '"stored_hash_mismatch"' not in source
    tree = ast.parse(textwrap.dedent(source))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    refusal_calls = [c for c in calls if c.func.id == "_stored_hash_mismatch_refusal"]
    assert len(refusal_calls) == 1
    assert any(kw.arg == "before_validation" for kw in refusal_calls[0].keywords)

    hash_lines = [c.lineno for c in calls if c.func.id == "_changed_path_hashes"]
    validation_line = next(
        c.lineno for c in calls if c.func.id == "_run_full_snapshot_validations"
    )
    refusal_line = refusal_calls[0].lineno
    assert any(line < validation_line for line in hash_lines)
    assert any(validation_line < line < refusal_line for line in hash_lines)
