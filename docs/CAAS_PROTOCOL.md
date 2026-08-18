# CAAS Protocol — Continuous Audit as a Service

**CAAS** stands for **Continuous Audit as a Service**. This is the owner-canonical
expansion. Any other expansion (notably "Continuous Automated Assurance System",
which appears in the separate **UltrafastSecp256k1** README) is wrong.

This document is the contract, not background reading. It enumerates the CAAS
properties AIWorkHub must uphold **by construction** — automatically, as a
service of the normal lifecycle, so a repository cannot drift out of compliance
silently and nobody has to remember to run a checker. Each property is marked
with how far it can be enforced automatically **from this repository**.

Enforcement lives in `aiworkhub.caas_enforcement`; the read-only audit layer
that produces findings lives in `aiworkhub.audit_system`.

## Enforceability legend

- **automatic** — checked automatically by `caas_enforcement` on every guarded
  lifecycle transition. A non-compliant state is rejected without anyone
  invoking a checker by hand.
- **partial** — the property has an automatically-enforced core and a named
  residual that a single card cannot close (it is owned elsewhere).
- **external** — the property cannot be enforced from this repository at all
  (it lives in another repository) and is reported by name, never silently
  treated as covered.

## Properties

| ID | Property | Enforceability |
| --- | --- | --- |
| CAAS-P1 | Canonical expansion. Repository-controlled documentation expands CAAS only as "Continuous Audit as a Service"; no forbidden expansion appears. | automatic |
| CAAS-P2 | Enforcement by construction. The compliance check runs as part of the lifecycle, not as a step someone remembers. | automatic |
| CAAS-P3 | Audit is read-only. The audit layer cannot mutate repository code; its only output path is the findings sink. | automatic |
| CAAS-P4 | Narrow scope. Audit passes operate on an explicitly bounded scope (a file, symbol, or range), never a whole-repository sweep. | automatic |
| CAAS-P5 | Structured findings with provenance. Every finding is structured and carries provenance identifying the audit pass that produced it. | automatic |
| CAAS-P6 | Durable in NeedFix. Findings are delivered through a findings sink so they land in the NeedFix queue. | partial |
| CAAS-P7 | Upstream expansion correction. The wrong expansion in the UltrafastSecp256k1 README is corrected. | external |

## Named residuals (what is *not* enforced from here)

These are stated by name so they do not read as covered:

- **CAAS-P6 residual.** `caas_enforcement` enforces that a findings sink is
  bound and that findings carry provenance, and `audit_system` provides durable
  (JSONL) and adapter sinks. Binding the audit layer to the **canonical
  MCP-bound NeedFix store** in `src/aiworkhub/task_store.py` is owned by another
  card; that store is not edited here. The adapter seam
  (`audit_system.CallableFindingsSink`) exists so a manager can forward findings
  to `needfix` intake without this layer importing the store.
- **CAAS-P7 (external).** The string "Continuous Automated Assurance System"
  lives in the **UltrafastSecp256k1** repository's README. This repository does
  not control that file and must not claim to have fixed it; it is named here
  and corrected only where this repository controls the wording
  (`docs/CAAS_PROTOCOL.md` and `README.md`).

## How enforcement is wired

`caas_enforcement.enforce_caas(state_getter)` is a decorator. A lifecycle
transition wrapped with it runs `CaasEnforcer.evaluate(state)` on every call and
raises `CaasComplianceError` before the transition body executes when the state
violates an automatically-enforced property. The caller never calls a checker;
the check is structurally coupled to the transition. External properties are
reported (`ComplianceReport.unenforceable()`) but never gate the transition,
because this repository cannot decide them.
