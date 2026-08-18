"""End-to-end slice for the read-only AuditSystem.

Hermetic: imports only ``aiworkhub.audit_system`` (+ ``caas_enforcement``) and
stdlib. Constructs no ProcessManager and launches no process. Proves one narrow
scope -> one read-only pass -> structured findings in NeedFix with provenance,
and that the audit layer cannot mutate the repository.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from aiworkhub.audit_system import (
    AuditScope,
    AuditSystem,
    CallableFindingsSink,
    InMemoryFindingsSink,
    JsonlFindingsSink,
    RawFinding,
    caas_expansion_review,
    read_only_file_reader,
)


def _hash_tree(root: Path) -> dict[str, str]:
    """Content hash of every file under ``root`` — used to prove immutability."""

    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digests[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digests


def test_scope_rejects_whole_repository_sweep():
    with pytest.raises(ValueError):
        AuditScope(kind="file", target=".")
    with pytest.raises(ValueError):
        AuditScope(kind="file", target="")
    # A bounded scope is accepted.
    assert AuditScope(kind="file", target="README.md").label == "file:README.md"


def test_slice_runs_end_to_end_and_writes_findings_to_needfix(tmp_path):
    # A tiny "repository" with one file that misuses the CAAS expansion.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "clean.md").write_text(
        "CAAS is Continuous Audit as a Service.", encoding="utf-8"
    )
    (repo / "offender.md").write_text(
        "We run a Continuous Automated Assurance System here.", encoding="utf-8"
    )

    before = _hash_tree(repo)

    # Durable NeedFix intake artifact, separate from the repository.
    needfix = tmp_path / "needfix" / "findings.jsonl"
    sink = JsonlFindingsSink(needfix)
    audit = AuditSystem(sink=sink, source_reader=read_only_file_reader(repo))

    # ONE narrow scope, ONE read-only pass.
    scope = AuditScope(kind="file", target="offender.md")
    report = audit.run_pass(
        audit_pass_id="AUDIT-CAAS-0001",
        scope=scope,
        reviewer="caas-naming-audit",
        review=caas_expansion_review,
    )

    # Structured finding produced and delivered to NeedFix.
    assert report.finding_count == 1
    rows = sink.read_all()
    assert len(rows) == 1
    record = rows[0]
    assert record["severity"] == "high"
    assert "Continuous Automated Assurance System" in record["summary"]
    assert record["path"] == "offender.md"

    # Provenance identifies the audit pass.
    prov = record["provenance"]
    assert prov["audit_pass_id"] == "AUDIT-CAAS-0001"
    assert prov["scope"] == "file:offender.md"
    assert prov["reviewer"] == "caas-naming-audit"
    assert prov["layer"] == "AuditSystem"

    # READ-ONLY: the repository is byte-for-byte unchanged after the pass.
    assert _hash_tree(repo) == before


def test_audit_layer_has_no_repository_write_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.md").write_text("Continuous Audit as a Service", encoding="utf-8")
    before = _hash_tree(repo)

    sink = InMemoryFindingsSink()
    audit = AuditSystem(sink=sink, source_reader=read_only_file_reader(repo))

    # The AuditSystem exposes no write/mutate/apply/commit method on the repo.
    public = {name for name in dir(audit) if not name.startswith("_")}
    forbidden = {"write", "mutate", "apply", "edit", "commit", "save"}
    assert public & forbidden == set()

    # A pass that "wants" to flag everything still cannot change the repo.
    def flag_pass(scope, read):
        read(scope.target)  # read-only access only
        return [RawFinding(severity="low", summary="noted", evidence="seen")]

    audit.run_pass(
        audit_pass_id="AUDIT-RO-0001",
        scope=AuditScope(kind="file", target="a.md"),
        reviewer="ro-check",
        review=flag_pass,
    )
    assert _hash_tree(repo) == before
    assert len(sink.findings) == 1


def test_callable_sink_forwards_structured_record_to_needfix_intake():
    """The seam a manager uses to forward findings to the canonical store."""

    captured: list[dict] = []

    def fake_needfix_intake(record: dict) -> str:
        captured.append(record)
        return "NF-INTAKE-1"

    sink = CallableFindingsSink(fake_needfix_intake)
    audit = AuditSystem(sink=sink, source_reader=lambda target: "irrelevant")

    def one_finding(scope, read):
        return [RawFinding(severity="medium", summary="s", evidence="e")]

    report = audit.run_pass(
        audit_pass_id="AUDIT-ADAPTER-1",
        scope=AuditScope(kind="symbol", target="mod.func"),
        reviewer="adapter-check",
        review=one_finding,
    )
    assert report.finding_ids == ["NF-INTAKE-1"]
    assert captured[0]["provenance"]["audit_pass_id"] == "AUDIT-ADAPTER-1"


def test_missing_provenance_id_is_rejected():
    audit = AuditSystem(
        sink=InMemoryFindingsSink(), source_reader=lambda target: ""
    )
    with pytest.raises(ValueError):
        audit.run_pass(
            audit_pass_id="   ",
            scope=AuditScope(kind="file", target="x.md"),
            reviewer="r",
            review=lambda scope, read: [],
        )
