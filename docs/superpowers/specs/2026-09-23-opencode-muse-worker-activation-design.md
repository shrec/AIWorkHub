# OpenCode Muse 1.3 as a Task MCP Worker

## Decision and scope

Muse 1.3 runs through `opencode_cli` as an AIWorkHub implementation worker. It
does not act as the Manager Chat model. The exact first acceptance identity is
`opencode/muse-spark-1.3-contributor-free`; the distinct
`opencode-go/muse-spark-1.3-contributor` identity is not a fallback and must
not be invoked without its own explicit cost/access decision. The manager
assigns English task cards, launches them into `processing`, and reviews the
result. The worker never accepts its own change into the canonical tree.

This design covers Windows worker startup, model round trip, AIWorkHub worker
tools, task lifecycle, and a bounded implementation canary. Manager Chat's
automatic session and display behavior is a separate design. No code is
copied from `E:\claude-code-main`.

## Measured starting point

- The canonical workforce catalog already reports the free Muse identity as
  `opencode_cli`, `implementation_worker=true`, `manager=false`,
  `policy_enabled=true`, `launch_eligible=true`, and `available=true`.
- OpenCode preflight sees the executable and both Muse identities, but says
  `installed_unverified_access` and has no observed successful round trip.
  `opencode --version` returns `1.18.32` in a non-elevated Windows shell. These
  observations do not prove that Muse can finish a worker task.
- A prior read-only OpenCode worker canary (`opencode_lane_probe_v1`) used
  `opencode/big-pickle` and failed before worker-context acknowledgement with
  `EPERM: operation not permitted, lstat 'D:\'`.
- The exact free-Muse read-only canary
  `OPENCODE_MUSE_FREE_WINDOWS_READONLY_CANARY_20260923_V1` entered
  `processing` on request `961fb302928d4badaee23548aff991c1`, then
  exited 1 with zero stdout and the same stderr. Its context receipt and
  Source Graph call were absent; no file changed and the workspace was
  retained. This confirms a live worker-startup blocker for Muse, not its
  filesystem/ACL root cause or a need for administrator privileges. The
  measured obstacle is tracked as `NF-2026-00973`.
- Task creation warned `workforce_route_absent` and
  `workforce_model_unpinnable` for that exact dynamic runner even though the
  workforce catalog lists it; an explicit exact-model launch still succeeded.
  Diagnose this routing inconsistency separately from the child startup error.
- The source constructs read/execute grants for the provider executable and
  request-scoped modify grants for the worker directory, HOME, and temp
  paths. OpenCode worker MCP config uses the short `awh` alias with exact,
  default-deny tool permissions. Existing unit tests cover pieces of these
  contracts, not the live failing Windows path.
- The connected MCP server reports 0.11.66 while source tree release is
  0.11.68. A source fix cannot count as live verification until the matching
  package is installed and the loaded version is checked.

## Worker contract

The manager uses the exact catalog identity and preflight result when making
a task card. Discovery, policy eligibility, executable reachability, model
access, and successful round trip remain separate states. Do not advertise
the route as operational until a real free-Muse task has completed.

The Task MCP receipt verifies repository identity before the worker accesses
files. A launched card must become `processing`; merely creating `pending`
work is not execution. Its allowed writes include every production call site
and contract test needed for its task, with no overlap between parallel cards.
The worker follows the role-specific Source Graph, session, AI Memory, and KB
rules, acknowledges the injected bundle, and uses semantic-edit prepare/apply
for existing files. It receives only worker MCP tools; it cannot invoke
manager tools, Context Graph, a cross-repository route, or an unapproved
write. Acceptance remains a manager decision after mechanical gates and
source review.

OpenCode runs without Windows elevation. The per-request AppContainer boundary
must remain intact: no unrestricted process fallback, blanket drive grant,
machine-wide ACL modification, or weakening of the default-deny MCP tool
policy to make the canary pass. A required path grant must be narrow,
revocable where possible, and justified by a measured access trace. A route
that cannot meet this boundary fails closed with an actionable phase/error.

## Diagnosis and recovery sequence

The exact-model canary has reproduced the startup failure on the installed
version. Next capture its argv/executable, cwd,
environment path names (not secret values), AppContainer identity, filesystem
grant plan, failing operation/path, stdout/stderr, and cleanup state. Compare
the granted paths against the `D:\` lookup and verify whether that lookup
comes from OpenCode/Bun startup, configuration discovery, path normalization,
or AIWorkHub launch. A different outcome from the earlier `big-pickle` probe
must update the hypothesis before any fix is chosen.

Then add a regression that fails for the measured cause, make the smallest
worker-launch or grant change, and retest. Do not equate `EPERM lstat D:\`
with a request to run the manager or worker as administrator. Keep all
filesystem and subprocess failures tagged by phase and path without logging
credentials. Clean up per-request grants and process handles on every terminal
path; retain the task evidence needed for review.

After the startup path passes, run the exact free-Muse identity through a
read-only Task MCP canary: verified repository receipt; worker Source Graph and
context acknowledgement; model answer; zero writes; exit 0; genuine
`review_ready`. This checks both provider access and AIWorkHub worker tool
operation, not just process launch. Next run one isolated, low-risk code card
that includes its test scope and semantic-edit contract. The manager applies
mechanical gates, reviews the diff and evidence independently, and accepts or
returns that card in the same turn.

## Acceptance and non-goals

Acceptance requires a matching installed release and live evidence for both
canaries: exact model identity, `processing` transition, valid worker-tool
receipts, no cross-role access, no unexpected writes, terminal status visible
in the inbox/review queue, and no leaked process, lock, workspace, or grant.
Distinct launch, access, auth, quota, timeout, permission, and worker-protocol
failures must remain distinguishable. Unit tests and catalog presence alone
are not a success claim.

This work does not make OpenCode a Manager Chat backend, enable the paid Muse
identity, copy Claude Code source, or alter another repository's task store.
