# Shannon capability review

Reviewed upstream: [KeygraphHQ/shannon](https://github.com/KeygraphHQ/shannon) at commit
`b9744f6cecc5429333873b86a6762f1d70c54fc2` (HEAD on 2026-11-01). This is a capability
review, not a code import. Shannon is licensed AGPL-3.0, so nothing in AIWorkHub copies its
code, prompts or configuration; only ideas are compared. Sources read: the upstream README,
its CLAUDE.md and its configuration documentation. Its source tree and license text were not
read.

Shannon is an autonomous penetration tester for web applications. It maps the attack surface
from source, runs parallel per-class vulnerability agents, tries to exploit each hypothesis
against the running application and reports only what it exploited ("no exploit, no report").
An unproven hypothesis never fails the run.

## Adopted ideas

| Idea | AIWorkHub implementation | Truth boundary |
| --- | --- | --- |
| A finding blocks only when it reproduces | Roadmap outcome "Reproduction-gated reviewer findings": a reviewer attaches a reproduction (an existing pytest node id or a shell-free probe), the coordinator runs it on a scratch copy of the sealed candidate and records a receipt bound to the candidate digest. A reproduction that fails on the candidate makes the finding `reproduced` and blocking; one that passes makes it advisory; one that cannot run leaves it at `static_evidence` for the manager. | Planned, not implemented. `EvidenceLevel.REPRODUCED` exists but no production path assigns it, and `normalize_packet_findings` caps reviewer findings at `static_evidence`. Security-critical findings keep blocking without a reproduction. The receipt reuses the Known Bug Scanner coordinator-HMAC pattern rather than a new store. |
| Vulnerability classes as an explicit checklist | The security review lens names its classes: path traversal and path normalization, subprocess argument injection, secrets in logs or receipts, route and tool authorization. | Planned. Motivated by a measured miss: a rooted-path check written as `raw.startswith('/')` accepted `.//x`. No recall gain is claimed until a fixed defect set shows one. |
| Per-model cost and recall on fixed targets | Feeds evidence-driven routing: run the same fixed review cards through each routed model and record accepted findings and cost. | Shannon's published single-application comparison (a low-cost model found 3 of 7 issues, a frontier model 6 of 7 at roughly 19 times the cost) is its own number, not ours. Routing changes only after an AIWorkHub measurement. |

## Already covered by canonical AIWorkHub authorities

| Shannon practice | AIWorkHub authority |
| --- | --- |
| Retry a transient provider error without relaunching the agent | Transient retry backoff in the task core, separate from relaunch and rework |
| Scope rules enforced by the harness, not by the prompt | Card `allowed_writes` carried into the worker tool environment and enforced by the write guard |
| One isolated workflow per target | Every task, route and receipt is bound to a `repo_id` |
| Credentials kept out of the target and the logs | Worker environment scrub and the AppContainer sandbox |
| Resume a long run after a crash | Crash-stream salvage and the sealed rework overlay |
| The report is rendered by code, not by a model | Review packet and SARIF 2.1.0 export |

## Deliberately not adopted

- Temporal orchestration: the AIWorkHub task queue and dispatcher are the single lifecycle
  authority; a second workflow engine would be a parallel lifecycle.
- A container per run: execution isolation is the sandbox seam today; container and remote
  backends are a separate roadmap outcome and must earn their place there.
- Typst or PDF reports: SARIF and the review packet are the machine contract.
- Browser-driven exploitation of a running application: AIWorkHub reviews a sealed code
  candidate; it does not attack live services.
- Any Shannon code, prompt or configuration text: AGPL-3.0, and this repository is public.

## Evidence still required before further adoption

- Reproduction gate: rework rounds per accepted card and the false-block rate, measured
  before and after, plus how often a reviewer can supply a runnable reproduction at all.
- Security checklist: findings on a fixed defect set compared with the current lens.
- Routing comparison: accepted-outcome cost per card for each routed model on the same cards.

The donor rule is unchanged: preserve useful invariants, avoid a parallel database or
lifecycle, and measure the result before advertising a benefit.
