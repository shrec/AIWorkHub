"""AuditSystem — a read-only, narrow-scope review layer.

This is the layer that composes existing pieces (read-only review passes,
narrow scoping, and a structured findings queue) into repeatable audit passes
*without* going through the worker card lifecycle. It is, by contract:

- **separate** from the worker card lifecycle: this module imports no task
  engine, process manager, or worker workspace, and running a pass creates no
  card and launches no process;
- **read-only**: the only source access it holds is a ``source_reader``
  callable that returns text; its only output path is a :class:`FindingsSink`.
  It has no handle that can mutate repository code;
- **narrow-scoped**: every pass runs against a bounded :class:`AuditScope` (a
  file, symbol, or range), never a whole-repository sweep;
- **structured with provenance**: every finding it records is structured and
  carries provenance identifying the audit pass that produced it.

The durable sink writes findings toward the NeedFix queue. Binding to the
canonical MCP-bound NeedFix store in ``task_store.py`` is owned by another card;
:class:`CallableFindingsSink` is the seam a manager uses to forward findings to
that store without this layer importing it. See ``docs/CAAS_PROTOCOL.md``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from aiworkhub.caas_enforcement import (
    CAAS_EXPANSION,
    scan_text_for_forbidden_expansion,
)

VALID_SEVERITY = frozenset({"critical", "high", "medium", "low"})

# Scope targets that would make a pass a whole-repository sweep rather than a
# narrow, bounded audit.
_UNBOUNDED_TARGETS = frozenset({"", ".", "/", "*", "**", "./", "src", "src/"})


@dataclass(frozen=True)
class AuditScope:
    """A bounded target for one audit pass.

    ``kind`` is one of ``file``, ``symbol`` or ``range``; ``target`` names the
    exact thing under review. Construction rejects unbounded targets so a pass
    can never silently become a whole-repository sweep.
    """

    kind: str
    target: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"file", "symbol", "range"}:
            raise ValueError(f"unknown audit scope kind: {self.kind!r}")
        target = (self.target or "").strip()
        if not target:
            raise ValueError("audit scope target must be a bounded, non-empty target")
        if target in _UNBOUNDED_TARGETS:
            raise ValueError(
                f"audit scope {target!r} is a whole-repository sweep, not a narrow scope"
            )
        # Normalize once: the value validated is the value stored and used (label,
        # reads), so validation and use can never diverge on surrounding whitespace.
        object.__setattr__(self, "target", target)

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.target}"


@dataclass(frozen=True)
class AuditProvenance:
    """Provenance identifying the audit pass that produced a finding."""

    audit_pass_id: str
    scope: str
    reviewer: str
    layer: str = "AuditSystem"


@dataclass(frozen=True)
class RawFinding:
    """A finding as returned by a review pass, before provenance is stamped."""

    severity: str
    summary: str
    evidence: str
    path: str | None = None
    line: int | None = None
    symbol: str | None = None


@dataclass(frozen=True)
class AuditFinding:
    """A structured finding with provenance, ready to record to a sink."""

    severity: str
    summary: str
    evidence: str
    provenance: AuditProvenance
    path: str | None = None
    line: int | None = None
    symbol: str | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "summary": self.summary,
            "evidence": self.evidence,
            "path": self.path,
            "line": self.line,
            "symbol": self.symbol,
            "provenance": asdict(self.provenance),
        }


@runtime_checkable
class FindingsSink(Protocol):
    """Where structured findings are delivered. Records findings, not repo code.

    ``record`` returns a sink-assigned id that identifies the individual finding
    (used to populate :attr:`AuditReport.finding_ids`). A durable sink returns a
    per-finding-unique id; a forwarding adapter returns whatever id the store it
    forwards to assigns.
    """

    def record(self, finding: AuditFinding) -> str:
        ...


@dataclass
class InMemoryFindingsSink:
    """A sink that keeps findings in memory. Useful for tests and previews."""

    findings: list[AuditFinding] = field(default_factory=list)

    def record(self, finding: AuditFinding) -> str:
        self.findings.append(finding)
        return f"mem-{len(self.findings)}"


class JsonlFindingsSink:
    """Durable append-only sink writing structured findings as JSON lines.

    The file is the NeedFix intake artifact; it is a findings queue, separate
    from any repository source file. Recording a finding never touches
    repository code.

    ``record`` returns a durable id that *identifies* the individual finding, not
    merely the pass: ``"{audit_pass_id}#{seq}"`` where ``seq`` is the finding's
    1-based position in the queue file. Ids are unique per finding, so
    :attr:`AuditReport.finding_ids` can track a single finding, not just count
    them.
    """

    def __init__(self, path: Any):
        self._path = Path(path)
        self._seq: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def _next_seq(self) -> int:
        if self._seq is None:
            # Continue numbering after any findings already in the durable queue.
            self._seq = len(self.read_all())
        self._seq += 1
        return self._seq

    def record(self, finding: AuditFinding) -> str:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Number the finding by its position in the queue *before* appending, so
        # the first finding in a fresh file is #1, not #2.
        seq = self._next_seq()
        line = json.dumps(finding.to_record(), sort_keys=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return f"{finding.provenance.audit_pass_id}#{seq}"

    def read_all(self) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows


class CallableFindingsSink:
    """Adapter forwarding a finding record to any callable.

    A manager can pass the canonical NeedFix store's intake method here so
    findings land in the durable store, without the audit layer importing that
    store. The callable receives the structured record dict and returns an id.
    """

    def __init__(self, fn: Callable[[dict[str, Any]], str]):
        self._fn = fn

    def record(self, finding: AuditFinding) -> str:
        return self._fn(finding.to_record())


# A read-only review pass: given a bounded scope and a read-only source reader,
# it returns structured raw findings. It is handed no write capability.
ReviewPass = Callable[[AuditScope, Callable[[str], str]], Sequence[RawFinding]]


@dataclass
class AuditReport:
    """The result of one audit pass.

    ``finding_ids`` *identifies* the findings this pass recorded, one id per
    finding in ``findings`` order, as assigned by the sink. It is not a count:
    ``finding_count`` counts. A durable sink returns per-finding-unique ids, so
    an individual finding can be tracked; a forwarding adapter returns the ids
    the downstream store assigns.
    """

    audit_pass_id: str
    scope: str
    reviewer: str
    finding_ids: list[str] = field(default_factory=list)
    findings: list[AuditFinding] = field(default_factory=list)

    @property
    def finding_count(self) -> int:
        return len(self.findings)


class AuditSystem:
    """Runs read-only, narrow-scope review passes and records their findings.

    Read-only by contract: the only source access is ``source_reader`` (returns
    text) and the only output path is the ``sink``. The system exposes no method
    that writes repository files and holds no such handle.
    """

    def __init__(self, sink: FindingsSink, source_reader: Callable[[str], str]):
        self._sink = sink
        self._read = source_reader

    def run_pass(
        self,
        *,
        audit_pass_id: str,
        scope: AuditScope,
        reviewer: str,
        review: ReviewPass,
    ) -> AuditReport:
        if not audit_pass_id or not audit_pass_id.strip():
            raise ValueError("audit_pass_id is required for finding provenance")
        if not reviewer or not reviewer.strip():
            raise ValueError("reviewer is required for finding provenance")

        # The review pass receives only bounded scope and read-only access.
        raw_findings = review(scope, self._read)

        provenance = AuditProvenance(
            audit_pass_id=audit_pass_id,
            scope=scope.label,
            reviewer=reviewer,
        )
        report = AuditReport(
            audit_pass_id=audit_pass_id,
            scope=scope.label,
            reviewer=reviewer,
        )
        for raw in raw_findings:
            self._validate(raw)
            finding = AuditFinding(
                severity=raw.severity,
                summary=raw.summary,
                evidence=raw.evidence,
                provenance=provenance,
                path=raw.path,
                line=raw.line,
                symbol=raw.symbol,
            )
            finding_id = self._sink.record(finding)
            report.finding_ids.append(finding_id)
            report.findings.append(finding)
        return report

    @staticmethod
    def _validate(raw: RawFinding) -> None:
        if raw.severity not in VALID_SEVERITY:
            raise ValueError(
                f"finding severity {raw.severity!r} not in {sorted(VALID_SEVERITY)}"
            )
        if not raw.summary or not raw.summary.strip():
            raise ValueError("finding summary must be non-empty")
        if not raw.evidence or not raw.evidence.strip():
            raise ValueError("finding evidence must be non-empty")


def read_only_file_reader(root: Any) -> Callable[[str], str]:
    """Build a read-only reader over ``root`` that returns file text.

    The returned callable can only read text under ``root``; it offers no write
    path, which is what keeps an :class:`AuditSystem` built on it read-only.
    """

    base = Path(root).resolve()

    def _read(target: str) -> str:
        resolved = (base / target).resolve()
        if base != resolved and base not in resolved.parents:
            raise ValueError(f"target {target!r} escapes the audit root")
        return resolved.read_text(encoding="utf-8", errors="ignore")

    return _read


def caas_expansion_review(
    scope: AuditScope, read: Callable[[str], str]
) -> list[RawFinding]:
    """A read-only review pass: flag any forbidden CAAS expansion in ``scope``.

    This ties the audit layer to the CAAS naming contract: it detects the wrong
    expansions (e.g. "Continuous Automated Assurance System") so a manager can
    act on them. It only reads; it never edits.
    """

    text = read(scope.target)
    findings: list[RawFinding] = []
    for bad in scan_text_for_forbidden_expansion(text):
        findings.append(
            RawFinding(
                severity="high",
                summary=f"CAAS expanded incorrectly as {bad!r}",
                evidence=(
                    f"Found forbidden expansion {bad!r} in {scope.target}; "
                    f"the canonical expansion is {CAAS_EXPANSION!r}."
                ),
                path=scope.target,
            )
        )
    return findings
