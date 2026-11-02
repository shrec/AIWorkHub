"""RM-2026-00083: coordinator-executed reproduction receipts for reviewer findings.

Every classification below comes from a real subprocess run on tiny fixture
trees: a base tree, and a candidate tree that breaks ``add``. The runner stands
in for the validation sandbox and executes the exact command the spec builds.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import finding_reproduction as fr  # noqa: E402
from aiworkhub import process_launcher  # noqa: E402
from aiworkhub import quality_evidence as qe  # noqa: E402
from aiworkhub import quality_reviewer  # noqa: E402
from aiworkhub import review_orchestrator  # noqa: E402

KEY = b"k" * 32
BASE_OID = "a" * 40
CHANGED = {"src/calc.py": "a" * 64}
CANDIDATE = review_orchestrator.candidate_digest(CHANGED)

_TEST_CALC = (
    "import pathlib\n"
    "import sys\n"
    "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))\n"
    "from calc import add\n"
    "\n"
    "def test_add():\n"
    "    assert add(2, 2) == 4\n"
    "\n"
    "def test_add_zero():\n"
    "    assert add(0, 0) == 0\n"
    "\n"
    "def test_preexisting():\n"
    "    assert add(1, 1) == 3\n"
)
BASE_FILES = {
    "pytest.ini": "[pytest]\n",
    "src/calc.py": "def add(a, b):\n    return a + b\n",
    "tests/test_calc.py": _TEST_CALC,
    "tests/test_broken.py": "def test_x(:\n    pass\n",
}
CANDIDATE_FILES = {**BASE_FILES, "src/calc.py": "def add(a, b):\n    return a - b\n"}


# --------------------------------------------------------------------------
# fixture trees and a real subprocess runner
# --------------------------------------------------------------------------
def _write_tree(root: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _copy_tree(source: Path, destination: Path) -> Path:
    # Bytes only: copystat's utime/chmod are not available in every sandbox.
    for path in sorted(source.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    return destination


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _runner(timeout: float = 120.0):
    def run(workspace: Path, commands: list[str]) -> list[dict[str, Any]]:
        rows = []
        for command in commands:
            argv = shlex.split(command)
            assert argv[0] == "python"
            argv[0] = sys.executable
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("PYTEST_")
            }
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            try:
                completed = subprocess.run(
                    argv, cwd=workspace, env=env, capture_output=True,
                    timeout=timeout, check=False,
                )
            except subprocess.TimeoutExpired:
                rows.append({"command": command, "returncode": None, "timed_out": True})
                continue
            rows.append(
                {"command": command, "returncode": completed.returncode, "timed_out": False}
            )
        return rows

    return run


def _delta(root: Path, snapshot: dict[str, bytes]) -> list[str]:
    """What git would list for ``root``: paths modified, added or removed."""
    current = _tree_bytes(root)
    return sorted(
        path for path in set(current) | set(snapshot) if current.get(path) != snapshot.get(path)
    )


@dataclass(frozen=True)
class _Workspace:
    path: Path
    repo: Path
    home: Path
    allowed_writes: tuple[str, ...]


class _Trees:
    """A sealed candidate tree, a canonical/base tree and scratch copies of each."""

    def __init__(
        self,
        root: Path,
        *,
        run=None,
        base_oid: str = BASE_OID,
        candidate_files: dict[str, str] | None = None,
    ) -> None:
        self.candidate = _write_tree(root / "sealed_candidate", candidate_files or CANDIDATE_FILES)
        self.canonical = _write_tree(root / "canonical", BASE_FILES)
        self._candidate_snapshot = _tree_bytes(self.candidate)
        self._canonical_snapshot = _tree_bytes(self.canonical)
        self.scratch_root = root / "scratch"
        self.created: list[Path] = []
        self.cleaned: list[Path] = []
        self.resolved = 0
        self.run = run or _runner()
        self.base_oid = base_oid

    def candidate_delta(self) -> list[str]:
        return _delta(self.candidate, self._candidate_snapshot)

    def canonical_delta(self) -> list[str]:
        return _delta(self.canonical, self._canonical_snapshot)

    def _scratch(self, source: Path) -> Path:
        path = self.scratch_root / f"ws{len(self.created)}"
        self.created.append(path)
        return _copy_tree(source, path)

    def _cleanup(self, path: Path) -> None:
        self.cleaned.append(path)
        shutil.rmtree(path)

    def target(self, spec: fr.ReproductionSpec | None = None) -> fr.ReproductionTarget:
        self.resolved += 1
        # The production scope: the candidate's changed paths plus each tree's
        # own current delta, re-listed on every manifest. No materialized set is
        # declared, so any canonical change still voids the run.
        return fr.ReproductionTarget(
            base_oid=self.base_oid,
            create_candidate=lambda: self._scratch(self.candidate),
            create_base=lambda: self._scratch(self.canonical),
            cleanup=self._cleanup,
            run=self.run,
            sealed_manifest=lambda also: fr.sealed_manifest(
                {fr.SEALED_ROOT: self.candidate, fr.CANONICAL_ROOT: self.canonical},
                sorted(CHANGED),
                deltas={
                    fr.SEALED_ROOT: (self.candidate_delta,),
                    fr.CANONICAL_ROOT: (self.canonical_delta,),
                },
                also=also,
            ),
            candidate_paths=tuple(sorted(CHANGED)),
        )


def _reproduce(trees: _Trees, spec_text: str) -> dict[str, Any]:
    spec = fr.parse_reproduction_spec(spec_text)
    assert spec is not None
    return fr.reproduce_finding(
        spec,
        finding_id="reviewer:correctness:finding-1",
        candidate_sha256=CANDIDATE,
        reviewer_receipt_sha256="r" * 64,
        target=trees.target(spec),
        coordinator_key=fr.receipt_key(KEY),
    )


# --------------------------------------------------------------------------
# classification, by real execution
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("spec_text", "classification", "candidate_outcome", "base_outcome"),
    [
        ("pytest:tests/test_calc.py::test_add", fr.REPRODUCED, fr.FAILED, fr.PASSED),
        ("pytest:tests/test_calc.py::test_preexisting", fr.PRE_EXISTING, fr.FAILED, fr.FAILED),
        ("pytest:tests/test_calc.py::test_add_zero", fr.NOT_REPRODUCED, fr.PASSED, fr.PASSED),
        ("pytest:tests/test_calc.py::test_missing", fr.UNRUNNABLE, fr.UNRUNNABLE, fr.UNRUNNABLE),
        ("pytest:tests/test_broken.py::test_x", fr.UNRUNNABLE, fr.UNRUNNABLE, fr.UNRUNNABLE),
        ("probe:from calc import add\nassert add(2, 3) == 5\n", fr.REPRODUCED, fr.FAILED, fr.PASSED),
    ],
)
def test_classification_by_real_execution(
    tmp_path: Path, spec_text, classification, candidate_outcome, base_outcome
) -> None:
    trees = _Trees(tmp_path)
    sealed_before = (_tree_bytes(trees.candidate), _tree_bytes(trees.canonical))

    receipt = _reproduce(trees, spec_text)

    assert receipt["schema_id"] == fr.SCHEMA_ID
    assert receipt["classification"] == classification
    assert receipt["candidate"]["outcome"] == candidate_outcome
    assert receipt["base"]["outcome"] == base_outcome
    assert receipt["spec_sha256"] == hashlib.sha256(spec_text.encode("utf-8")).hexdigest()
    assert receipt["base_oid"] == BASE_OID
    assert receipt["finding_id"] == "reviewer:correctness:finding-1"
    assert isinstance(receipt["candidate"]["duration_seconds"], float)
    assert fr.verify_receipt(receipt, fr.receipt_key(KEY))
    # Scratch workspaces only: the sealed candidate and canonical trees are
    # byte-identical, and every scratch workspace was cleaned up.
    assert (_tree_bytes(trees.candidate), _tree_bytes(trees.canonical)) == sealed_before
    assert trees.cleaned == trees.created and len(trees.created) == 2
    assert not any(path.exists() for path in trees.created)


def test_missing_node_and_collection_error_name_the_pytest_exit(tmp_path: Path) -> None:
    missing = _reproduce(_Trees(tmp_path / "a"), "pytest:tests/test_calc.py::test_missing")
    broken = _reproduce(_Trees(tmp_path / "b"), "pytest:tests/test_broken.py::test_x")
    assert missing["candidate"]["returncode"] == 4
    assert missing["candidate"]["detail"] == "exit_4"
    # A node in an uncollectable file is a collection error (2) or, on newer
    # pytest, "not found" (4): never a pass or a test failure.
    assert broken["candidate"]["returncode"] in {2, 4}
    assert broken["candidate"]["detail"] == f"exit_{broken['candidate']['returncode']}"


def test_timeout_is_unrunnable(tmp_path: Path) -> None:
    trees = _Trees(tmp_path, run=_runner(timeout=1.0))
    receipt = _reproduce(trees, "probe:import time\ntime.sleep(30)\n")
    assert receipt["classification"] == fr.UNRUNNABLE
    assert receipt["candidate"]["timed_out"] is True
    assert receipt["candidate"]["detail"] == "timeout"


def test_sandbox_refusal_is_unrunnable(tmp_path: Path) -> None:
    class ValidationRunError(RuntimeError):
        results: list[dict[str, Any]] = []

    def refuse(_workspace, _commands):
        raise ValidationRunError("validation_unsupported_in_sandbox:landlock")

    trees = _Trees(tmp_path, run=refuse)
    receipt = _reproduce(trees, "pytest:tests/test_calc.py::test_add")
    assert receipt["classification"] == fr.UNRUNNABLE
    assert receipt["candidate"]["detail"].startswith("sandbox_refused:")
    assert trees.cleaned == trees.created


def test_failing_run_rows_carried_on_the_exception_are_judged(tmp_path: Path) -> None:
    class ValidationRunError(RuntimeError):
        def __init__(self, results):
            super().__init__("validation_failed")
            self.results = results

    real = _runner()

    def raising(workspace, commands):
        rows = real(workspace, commands)
        if rows[0]["returncode"]:
            raise ValidationRunError(rows)
        return rows

    receipt = _reproduce(_Trees(tmp_path, run=raising), "pytest:tests/test_calc.py::test_add")
    assert receipt["classification"] == fr.REPRODUCED


def test_missing_base_oid_never_classifies_reproduced(tmp_path: Path) -> None:
    trees = _Trees(tmp_path, base_oid="")
    receipt = _reproduce(trees, "pytest:tests/test_calc.py::test_add")
    assert receipt["classification"] == fr.UNRUNNABLE
    assert receipt["base"]["detail"] == "base_oid_missing"


def test_a_spec_that_mutates_the_sealed_tree_is_never_signed(tmp_path: Path) -> None:
    trees = _Trees(tmp_path)
    real = trees.run

    def mutating(workspace, commands):
        (trees.candidate / "src" / "calc.py").write_text("tampered\n", encoding="utf-8")
        return real(workspace, commands)

    trees.run = mutating
    with pytest.raises(RuntimeError, match="sealed_tree_mutated"):
        _reproduce(trees, "pytest:tests/test_calc.py::test_add")


@pytest.mark.parametrize(
    ("relative", "text"),
    [
        ("tests/test_calc.py", "def test_add():\n    pass\n"),
        ("docs/new_notes.txt", "written by a spec\n"),
    ],
)
def test_a_write_to_a_non_candidate_canonical_file_is_never_signed(
    tmp_path: Path, relative: str, text: str
) -> None:
    trees = _Trees(tmp_path)
    assert relative not in CHANGED and trees.canonical_delta() == []
    real = trees.run

    def mutating(workspace, commands):
        path = trees.canonical / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return real(workspace, commands)

    trees.run = mutating
    with pytest.raises(RuntimeError, match="sealed_tree_mutated"):
        _reproduce(trees, "pytest:tests/test_calc.py::test_add")


# --------------------------------------------------------------------------
# a probe fails only on an assertion (or its own exit 1)
# --------------------------------------------------------------------------
def test_a_probe_importing_a_candidate_only_module_is_unrunnable(tmp_path: Path) -> None:
    trees = _Trees(
        tmp_path,
        candidate_files={**CANDIDATE_FILES, "src/newmod.py": "def f():\n    return 2\n"},
    )
    receipt = _reproduce(trees, "probe:from newmod import f\nassert f() == 1\n")
    assert receipt["candidate"]["outcome"] == fr.FAILED
    assert receipt["candidate"]["returncode"] == 1
    # The base lacks the module: an ImportError is no failed assertion.
    assert receipt["base"]["outcome"] == fr.UNRUNNABLE
    assert receipt["base"]["detail"] == "exit_3"
    assert receipt["classification"] == fr.UNRUNNABLE


def test_a_probe_raising_a_non_assertion_error_is_unrunnable(tmp_path: Path) -> None:
    receipt = _reproduce(
        _Trees(tmp_path),
        "probe:from calc import add\nif add(2, 3) != 5:\n    raise ValueError('wrong')\n",
    )
    assert receipt["candidate"]["outcome"] == fr.UNRUNNABLE
    assert receipt["candidate"]["detail"] == "exit_3"
    assert receipt["base"]["outcome"] == fr.PASSED
    assert receipt["classification"] == fr.UNRUNNABLE


def test_a_probe_explicit_exit_1_still_counts_as_failed(tmp_path: Path) -> None:
    receipt = _reproduce(
        _Trees(tmp_path),
        "probe:import sys\nfrom calc import add\nsys.exit(0 if add(2, 3) == 5 else 1)\n",
    )
    assert receipt["candidate"]["outcome"] == fr.FAILED
    assert receipt["base"]["outcome"] == fr.PASSED
    assert receipt["classification"] == fr.REPRODUCED


# --------------------------------------------------------------------------
# the spec parser
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("pytest:tests/test_calc.py::test_add", "pytest"),
        ("pytest:tests/unit/test_x.py::TestA::test_b[case-1]", "pytest"),
        ("pytest:tests/test_calc.py", "pytest"),
        ("probe:print(1)", "probe"),
        ("probe:" + "x = 1\n" * 600 + "x", "probe"),
    ],
)
def test_parser_accepts_exact_specs(text: str, kind: str) -> None:
    spec = fr.parse_reproduction_spec(text)
    assert spec is not None and spec.kind == kind and spec.text == text


@pytest.mark.parametrize(
    "text",
    [
        "run pytest tests/test_calc.py::test_add",
        "pytest tests/test_calc.py::test_add",
        "pytest:tests/test_calc.py::test_add; rm -rf /",
        "pytest:tests/test_calc.py::test_add && echo x",
        "pytest:tests/test_calc.py::test_add | cat",
        "pytest:tests/test_calc.py::test_add `id`",
        "pytest:tests/test_calc.py::test_add $(id)",
        "pytest:-p no:cacheprovider tests/test_calc.py",
        "pytest:-pmalicious",
        "pytest:--rootdir=/",
        "pytest:tests/test_calc.py --rootdir=/",
        "pytest:tests/test_calc.py::test_add -p evil",
        "pytest:src/aiworkhub/test_x.py::test_a",
        "pytest:/etc/tests/test_x.py::test_a",
        "pytest:tests/../src/test_x.py::test_a",
        "pytest:tests/test_calc.py\n::test_add",
        "pytest:",
        "probe:",
        "probe:   \n",
        "probe:" + "x" * (fr.MAX_PROBE_BYTES + 1),
        "probe:print('\x00')",
        # Source that does not compile would exit 1 before the prelude's hook
        # runs and read as a failed probe, so it is free text, never executed.
        "probe:def f(:\n    pass\n",
        "probe:    x = 1\n",
        "probe:from __future__ import annotations\n",
        None,
        42,
    ],
)
def test_parser_refuses_everything_else(text) -> None:
    assert fr.parse_reproduction_spec(text) is None


def test_probe_limit_is_counted_in_utf8_bytes() -> None:
    assert fr.parse_reproduction_spec("probe:" + "x" * fr.MAX_PROBE_BYTES) is not None
    assert fr.parse_reproduction_spec("probe:" + "é" * (fr.MAX_PROBE_BYTES // 2 + 1)) is None


def test_spec_commands_are_shell_free_and_option_free() -> None:
    pytest_spec = fr.parse_reproduction_spec("pytest:tests/test_calc.py::test_add")
    probe_spec = fr.parse_reproduction_spec("probe:print('a b; rm -rf /')")
    assert shlex.split(pytest_spec.command) == [
        "python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "tests/test_calc.py::test_add",
    ]
    argv = shlex.split(probe_spec.command)
    assert argv[:3] == ["python", "-I", "-c"] and len(argv) == 4
    import base64
    import re

    match = re.fullmatch(
        r"exec\(__import__\('base64'\)\.b64decode\('([A-Za-z0-9+/=]+)'\)\)",
        argv[3],
    )
    assert match is not None, argv[3]
    source = base64.b64decode(match.group(1)).decode("utf-8")
    assert source.endswith("print('a b; rm -rf /')")
    outside = argv[3].replace(match.group(1), "")
    assert "\n" not in outside and ";" not in outside


# --------------------------------------------------------------------------
# receipt HMAC
# --------------------------------------------------------------------------
def _signed(tmp_path: Path) -> dict[str, Any]:
    return _reproduce(_Trees(tmp_path), "probe:from calc import add\nassert add(2, 3) == 5\n")


def test_receipt_hmac_verifies_and_refuses_tampering(tmp_path: Path) -> None:
    receipt = _signed(tmp_path)
    key = fr.receipt_key(KEY)
    assert fr.verify_receipt(receipt, key)
    assert not fr.verify_receipt(receipt, fr.receipt_key(b"other-key"))
    assert not fr.verify_receipt(receipt, KEY)  # the raw key is not the receipt key
    for field, value in (
        ("classification", fr.NOT_REPRODUCED),
        ("candidate_sha256", "0" * 64),
        ("spec_sha256", "0" * 64),
        ("finding_id", "reviewer:correctness:other"),
        ("hmac_sha256", "0" * 64),
    ):
        assert not fr.verify_receipt({**receipt, field: value}, key), field
    unsigned = {k: v for k, v in receipt.items() if k != "hmac_sha256"}
    assert not fr.verify_receipt(unsigned, key)
    assert not fr.verify_receipt({**receipt, "schema_id": "other"}, key)
    assert fr.verify_receipt(fr.sign_receipt(receipt, key), key)
    with pytest.raises(ValueError):
        fr.receipt_key(b"")


# --------------------------------------------------------------------------
# the reviewer boundary
# --------------------------------------------------------------------------
def _packet() -> dict[str, Any]:
    return {
        "candidate": {
            "scoped_audits": {
                "correctness": {"packet": {"changed_paths": [{"path": "src/module.py"}]}}
            }
        }
    }


def test_no_reviewer_finding_reaches_reproduced_through_normalize() -> None:
    long_probe = "probe:" + "assert True\n" * 300
    assert len(long_probe) > quality_reviewer.MAX_TEXT_CHARS
    rows = quality_reviewer.normalize_packet_findings(
        _packet(),
        lens="correctness",
        findings=[
            {
                "severity": "medium",
                "summary": "add subtracts",
                "evidence": "src/module.py:3",
                "evidence_level": level,
                "reproduction": long_probe,
            }
            for level in ("reproduced", "fixed_and_verified", "tested")
        ],
    )
    assert {row["evidence_level"] for row in rows} == {"static_evidence"}
    # A parseable spec is kept byte-exact for the coordinator to run.
    assert all(row["reproduction"] == long_probe for row in rows)
    assert "reproduction" in quality_reviewer.QUALITY_REVIEW_FINDING_SCHEMA_DOC
    assert "probe:" in quality_reviewer.QUALITY_REVIEW_FINDING_SCHEMA_DOC


def test_truncation_never_manufactures_an_executable_spec() -> None:
    oversize = "probe:" + "y = 2\n" * 1000
    assert fr.parse_reproduction_spec(oversize) is None
    bounded = fr.bounded_reproduction_text(oversize, quality_reviewer.MAX_TEXT_CHARS)
    assert len(bounded) <= quality_reviewer.MAX_TEXT_CHARS
    assert bounded.startswith(fr.UNEXECUTED_PREFIX)
    assert fr.parse_reproduction_spec(bounded) is None
    assert fr.bounded_reproduction_text("free text", 4) == "free"
    assert fr.bounded_reproduction_text(None, 10) == ""


# --------------------------------------------------------------------------
# the dispose path
# --------------------------------------------------------------------------
def _finding(severity: str, reproduction: str | None) -> dict[str, Any]:
    row = {
        "id": "finding-1",
        "severity": severity,
        "summary": "add subtracts",
        "evidence": "fixture:claimed-defect",
        "disposition": "defect",
        "actionable": True,
    }
    if reproduction is not None:
        row["reproduction"] = reproduction
    return row


def _receipt(*findings: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_id": "aiworkhub.quality_review_receipt.v1",
        "packet_sha256": "b" * 64,
        "target": {"request_id": "target-request", "task_id": "TARGET", "claim_epoch": 3},
        "reviewer": {
            "request_id": "review-req-1",
            "task_id": "QUALITY_REVIEW_TARGET",
            "provider": "reviewer-b",
        },
        "report": {
            "lens": qe.LENS_CORRECTNESS,
            "provider": "reviewer-b",
            "read_only": True,
            "can_mutate_repo": False,
            "findings": list(findings),
        },
        "submission_id": "c" * 64,
    }


def _driver(tmp_path: Path) -> review_orchestrator.ReviewOrchestrator:
    return review_orchestrator.ReviewOrchestrator(
        SimpleNamespace(repo=tmp_path), db_path=tmp_path / "review.sqlite3"
    )


def _dispose(driver, receipt, reproduce=None, **overrides) -> dict[str, Any]:
    args = {
        "task_id": "TARGET",
        "request_id": "target-request",
        "reviewer_request_id": "review-req-1",
        "finding_id": "reviewer:correctness:finding-1",
        "disposition": "dismissed",
        "counter_evidence": "",
        "reason": "",
        "actor": "codex:thread-exact",
        "reviewer_receipt": receipt,
        "candidate_sha256": CANDIDATE,
    }
    args.update(overrides)
    if reproduce is not None:
        args["reproduce"] = reproduce
    return driver.dispose_review_finding(**args)


def _stored(driver) -> list[dict[str, Any]]:
    return driver.review_finding_dispositions(task_id="TARGET", request_id="target-request")


class _Counting:
    def __init__(self, trees: _Trees) -> None:
        self.trees = trees
        self.callback = fr.dispose_reproducer(
            coordinator_key=lambda: KEY, resolve_target=trees.target
        )

    @property
    def runs(self) -> int:
        return self.trees.resolved


REPRODUCES = "probe:from calc import add\nassert add(2, 3) == 5\n"
DOES_NOT_REPRODUCE = "probe:from calc import add\nassert add(0, 0) == 0\n"


def test_dismissing_a_reproduced_finding_is_refused_and_stores_nothing(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    reproducer = _Counting(_Trees(tmp_path / "trees"))
    result = _dispose(
        driver,
        _receipt(_finding(qe.SEVERITY_MEDIUM, REPRODUCES)),
        reproducer.callback,
        counter_evidence="measured: unreachable",
    )
    assert result["ok"] is False
    assert result["error"] == "review_finding_reproduced_not_dismissable"
    assert result["reproduction_receipt"]["classification"] == fr.REPRODUCED
    assert _stored(driver) == []


def test_a_reproduced_finding_dismissed_on_a_contract_citation_is_manager_signed(
    tmp_path: Path,
) -> None:
    driver = _driver(tmp_path)
    reproducer = _Counting(_Trees(tmp_path / "trees"))
    citation = "card: the objective mandates that add subtracts"
    result = _dispose(
        driver,
        _receipt(_finding(qe.SEVERITY_MEDIUM, REPRODUCES)),
        reproducer.callback,
        counter_evidence=citation,
    )
    assert result["ok"] is True, result
    stored = _stored(driver)
    assert len(stored) == 1
    assert stored[0]["manager_signed"] is True
    assert stored[0]["contract_citations"] == [citation]
    assert stored[0]["reproduction_receipt"]["classification"] == fr.REPRODUCED
    assert fr.verify_receipt(stored[0]["reproduction_receipt"], fr.receipt_key(KEY))
    assert "evidence_level" not in stored[0]


def test_a_spec_on_another_finding_never_defers_the_counter_evidence_check(
    tmp_path: Path,
) -> None:
    called: list[dict[str, Any]] = []

    def reproduce(**binding):
        called.append(binding)
        raise AssertionError("reproduce must not run for a finding without a spec")

    with_spec = {**_finding(qe.SEVERITY_LOW, REPRODUCES), "id": "finding-2"}
    # A HIGH finding would otherwise reach the later contract-citation gate.
    receipt = _receipt(_finding(qe.SEVERITY_HIGH, None), with_spec)
    assert fr.report_has_spec(receipt["report"])
    driver = _driver(tmp_path)
    result = _dispose(driver, receipt, reproduce)
    assert result == {
        "ok": False,
        "error": "review_finding_disposition_counter_evidence_required",
    }
    assert called == [] and _stored(driver) == []


def test_dismissed_not_reproduced_medium_records_with_receipt_as_counter_evidence(
    tmp_path: Path,
) -> None:
    driver = _driver(tmp_path)
    reproducer = _Counting(_Trees(tmp_path / "trees"))
    result = _dispose(
        driver, _receipt(_finding(qe.SEVERITY_MEDIUM, DOES_NOT_REPRODUCE)), reproducer.callback
    )
    assert result["ok"] is True, result
    stored = _stored(driver)
    assert len(stored) == 1
    receipt = stored[0]["reproduction_receipt"]
    assert receipt["classification"] == fr.NOT_REPRODUCED
    assert fr.verify_receipt(receipt, fr.receipt_key(KEY))
    assert stored[0]["counter_evidence"] == "coordinator reproduction receipt: not_reproduced"
    assert "evidence_level" not in stored[0]


def test_unrunnable_receipt_keeps_todays_counter_evidence_rule(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    reproducer = _Counting(_Trees(tmp_path / "trees"))
    receipt = _receipt(_finding(qe.SEVERITY_LOW, "pytest:tests/test_calc.py::test_missing"))
    refused = _dispose(driver, receipt, reproducer.callback)
    assert refused == {
        "ok": False,
        "error": "review_finding_disposition_counter_evidence_required",
    }
    recorded = _dispose(
        driver, receipt, reproducer.callback, counter_evidence="measured: unreachable"
    )
    assert recorded["ok"] is True
    assert recorded["reproduction_receipt"]["classification"] == fr.UNRUNNABLE
    assert recorded["counter_evidence"] == "measured: unreachable"


def test_high_without_contract_citation_is_refused_even_when_not_reproduced(
    tmp_path: Path,
) -> None:
    driver = _driver(tmp_path)
    reproducer = _Counting(_Trees(tmp_path / "trees"))
    receipt = _receipt(_finding(qe.SEVERITY_HIGH, DOES_NOT_REPRODUCE))
    result = _dispose(driver, receipt, reproducer.callback)
    assert result == {
        "ok": False,
        "error": "review_finding_disposition_contract_citation_required",
    }
    assert reproducer.runs == 0 and _stored(driver) == []
    cited = _dispose(
        driver, receipt, reproducer.callback,
        counter_evidence="card: acceptance excludes this path",
    )
    assert cited["ok"] is True and cited["manager_signed"] is True
    assert cited["reproduction_receipt"]["classification"] == fr.NOT_REPRODUCED


@pytest.mark.parametrize("disposition", ["dismissed", "confirmed"])
def test_critical_is_still_refused(tmp_path: Path, disposition: str) -> None:
    driver = _driver(tmp_path)
    reproducer = _Counting(_Trees(tmp_path / "trees"))
    result = _dispose(
        driver,
        _receipt(_finding(qe.SEVERITY_CRITICAL, DOES_NOT_REPRODUCE)),
        reproducer.callback,
        disposition=disposition,
        counter_evidence="card: excluded",
    )
    assert result == {"ok": False, "error": "review_finding_disposition_blocking_severity"}
    assert reproducer.runs == 0 and _stored(driver) == []


def test_confirmed_reproduced_records_evidence_level_reproduced(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    reproducer = _Counting(_Trees(tmp_path / "trees"))
    result = _dispose(
        driver,
        _receipt(_finding(qe.SEVERITY_MEDIUM, REPRODUCES)),
        reproducer.callback,
        disposition="confirmed",
    )
    assert result["ok"] is True
    assert result["evidence_level"] == "reproduced"
    assert result["reproduction_receipt"]["classification"] == fr.REPRODUCED


def test_finding_without_spec_behaves_exactly_as_before(tmp_path: Path) -> None:
    receipt = _receipt(_finding(qe.SEVERITY_LOW, "free text: run the suite"))
    reproducer = _Counting(_Trees(tmp_path / "trees"))
    plain = _dispose(
        _driver(tmp_path / "plain"), receipt,
        counter_evidence="measured", now=review_orchestrator.datetime(2026, 11, 1),
    )
    with_callback = _dispose(
        _driver(tmp_path / "callback"), receipt, reproducer.callback,
        counter_evidence="measured", now=review_orchestrator.datetime(2026, 11, 1),
    )
    assert plain == with_callback
    assert "reproduction_receipt" not in plain
    assert reproducer.runs == 0
    assert _dispose(_driver(tmp_path / "empty"), receipt) == {
        "ok": False,
        "error": "review_finding_disposition_counter_evidence_required",
    }
    assert not fr.report_has_spec(receipt["report"])
    assert fr.report_has_spec(_receipt(_finding("low", REPRODUCES))["report"])


def test_replayed_dispose_reuses_the_stored_receipt(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    reproducer = _Counting(_Trees(tmp_path / "trees"))
    receipt = _receipt(_finding(qe.SEVERITY_MEDIUM, DOES_NOT_REPRODUCE))
    first = _dispose(driver, receipt, reproducer.callback)
    assert first["ok"] is True and first["idempotent"] is False
    assert reproducer.runs == 1
    replay = _dispose(driver, receipt, reproducer.callback)
    assert replay["ok"] is True and replay["idempotent"] is True
    assert replay["reproduction_receipt"] == first["reproduction_receipt"]
    assert reproducer.runs == 1
    # A re-disposition of the same finding also reuses the verified receipt.
    redisposed = _dispose(
        driver, receipt, reproducer.callback, disposition="confirmed", reason="changed my mind"
    )
    assert redisposed["ok"] is True
    assert redisposed["reproduction_receipt"] == first["reproduction_receipt"]
    assert reproducer.runs == 1


def test_a_stored_receipt_that_fails_verification_is_never_reused(tmp_path: Path) -> None:
    trees = _Trees(tmp_path / "trees")
    callback = fr.dispose_reproducer(coordinator_key=lambda: KEY, resolve_target=trees.target)
    good = callback(
        finding_id="reviewer:correctness:finding-1",
        spec_text=DOES_NOT_REPRODUCE,
        candidate_sha256=CANDIDATE,
        reviewer_receipt_sha256="r" * 64,
    )
    assert trees.resolved == 1
    forged = {**good, "classification": fr.REPRODUCED}
    again = callback(
        finding_id="reviewer:correctness:finding-1",
        spec_text=DOES_NOT_REPRODUCE,
        candidate_sha256=CANDIDATE,
        reviewer_receipt_sha256="r" * 64,
        stored=forged,
    )
    assert trees.resolved == 2 and again["classification"] == fr.NOT_REPRODUCED
    other_candidate = callback(
        finding_id="reviewer:correctness:finding-1",
        spec_text=DOES_NOT_REPRODUCE,
        candidate_sha256="f" * 64,
        reviewer_receipt_sha256="r" * 64,
        stored=good,
    )
    assert trees.resolved == 3 and other_candidate["candidate_sha256"] == "f" * 64


def test_a_receipt_bound_elsewhere_is_refused_by_the_orchestrator(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    trees = _Trees(tmp_path / "trees")

    def misbound(**binding):
        spec = fr.parse_reproduction_spec(binding["spec_text"])
        return fr.reproduce_finding(
            spec,
            finding_id="reviewer:correctness:other",
            candidate_sha256=binding["candidate_sha256"],
            reviewer_receipt_sha256=binding["reviewer_receipt_sha256"],
            target=trees.target(spec),
            coordinator_key=fr.receipt_key(KEY),
        )

    result = _dispose(
        driver, _receipt(_finding(qe.SEVERITY_MEDIUM, DOES_NOT_REPRODUCE)), misbound
    )
    assert result == {"ok": False, "error": "review_finding_reproduction_receipt_invalid"}
    assert _stored(driver) == []


# --------------------------------------------------------------------------
# production wiring through the ProcessManager instance
# --------------------------------------------------------------------------
def test_manager_reproducer_uses_scratch_workspaces_of_the_sealed_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trees = _Trees(tmp_path)
    source = SimpleNamespace(
        path=trees.candidate, repo=trees.canonical, base_oid=BASE_OID, home=tmp_path
    )
    calls: dict[str, Any] = {}

    def create_quality_review_workspace(src, request_id, paths, adapter_id, read_only):
        calls["candidate"] = (src, request_id, list(paths), adapter_id, list(read_only))
        return _Workspace(
            path=trees._scratch(trees.candidate), repo=trees.canonical, home=tmp_path,
            allowed_writes=(),
        ), {}

    def create_workspace(repo, request_id, card, adapter_id, *, pinned_base_oid=None):
        calls["base"] = (repo, request_id, card, adapter_id, pinned_base_oid)
        return _Workspace(
            path=trees._scratch(trees.canonical), repo=trees.canonical, home=tmp_path,
            allowed_writes=tuple(card.get("allowed_writes") or ()),
        )

    def cleanup_workspace(repo, path, home):
        trees._cleanup(path)

    run = _runner()

    def run_validations(workspace, commands, authority, **route):
        calls.setdefault("route", []).append((authority, route))
        calls.setdefault("allowed_writes", []).append(workspace.allowed_writes)
        return run(workspace.path, list(commands))

    def changed_paths(workspace):
        calls.setdefault("changed_paths", []).append(workspace)
        return trees.candidate_delta()

    def canonical_delta(repo):
        calls.setdefault("canonical_delta", []).append(repo)
        return trees.canonical_delta()

    monkeypatch.setattr(
        process_launcher, "create_quality_review_workspace", create_quality_review_workspace
    )
    monkeypatch.setattr(process_launcher, "create_workspace", create_workspace)
    monkeypatch.setattr(process_launcher, "cleanup_workspace", cleanup_workspace)
    monkeypatch.setattr(
        process_launcher, "_run_validations_with_toolchain_receipt", run_validations
    )
    monkeypatch.setattr(
        process_launcher, "_validation_route_kwargs",
        lambda metadata: {"backend": "fixture", "adapter_id": metadata["adapter_id"]},
    )
    monkeypatch.setattr(process_launcher._worker_workspace, "changed_paths", changed_paths)
    monkeypatch.setattr(
        process_launcher._worker_workspace, "_canonical_worktree_delta_paths", canonical_delta
    )
    manager = SimpleNamespace(
        _terminal_authority_key=lambda: KEY,
        _prepared_quality_review=lambda request_id, task_id: {
            "ok": True,
            "prepared": {
                "workspace": source,
                "changed_hashes": dict(CHANGED),
                "worker_adapter_id": "codex_cli",
                "read_only_input_paths": [],
            },
        },
    )
    sealed_before = (_tree_bytes(trees.candidate), _tree_bytes(trees.canonical))
    reproduce = fr.manager_reproducer(
        manager,
        task_id="TARGET",
        request_id="target-request",
        card={
            "task_id": "TARGET",
            "allowed_writes": ["src/calc.py"],
            "rework_predecessor": {"x": 1},
        },
    )

    receipt = reproduce(
        finding_id="reviewer:correctness:finding-1",
        spec_text="pytest:tests/test_calc.py::test_add",
        candidate_sha256=CANDIDATE,
        reviewer_receipt_sha256="r" * 64,
    )

    assert receipt["classification"] == fr.REPRODUCED
    assert fr.verify_receipt(receipt, fr.receipt_key(KEY))
    assert calls["candidate"][0] is source and calls["candidate"][2] == ["src/calc.py"]
    assert calls["candidate"][4] == ["tests/test_calc.py"]
    assert calls["base"][4] == BASE_OID and "rework_predecessor" not in calls["base"][2]
    assert all(authority == {} for authority, _route in calls["route"])
    # Both sides run with the same (empty) write scope, whatever the card allows.
    assert calls["allowed_writes"] == [(), ()]
    # The sealed-tree guard re-lists both trees' current deltas before and after.
    assert calls["changed_paths"] == [source, source]
    assert calls["canonical_delta"] == [trees.canonical, trees.canonical]
    assert (_tree_bytes(trees.candidate), _tree_bytes(trees.canonical)) == sealed_before
    assert trees.cleaned == trees.created and len(trees.created) == 2


def test_manager_reproducer_names_an_unavailable_candidate(tmp_path: Path) -> None:
    manager = SimpleNamespace(
        _terminal_authority_key=lambda: KEY,
        _prepared_quality_review=lambda request_id, task_id: {
            "ok": False, "error": "quality_review_target_invalid"
        },
    )
    reproduce = fr.manager_reproducer(
        manager, task_id="TARGET", request_id="target-request", card={}
    )
    with pytest.raises(RuntimeError, match="candidate_unavailable"):
        reproduce(
            finding_id="reviewer:correctness:finding-1",
            spec_text=REPRODUCES,
            candidate_sha256=CANDIDATE,
            reviewer_receipt_sha256="r" * 64,
        )
