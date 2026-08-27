# Source Graph

AIWorkHub's Source Graph is a repository-local structural index built under
`.aiworkhub/source_graph/`. It gives managers and workers bounded code evidence
without repeatedly scanning the repository tree. Initialization builds the
first index; the repository daemon refreshes changed files incrementally.

## Query modes

The manager and worker MCP surfaces expose exactly 37 bounded modes. All modes
read the one canonical repository database; analytics never create a parallel
index or decision store.

| Mode | Use |
| --- | --- |
| `focus` | Ranked symbols, hot paths, tests, ownership and churn around a target |
| `slice` | Minimal dependency and call slice needed to change a target |
| `context` | File or symbol structure with neighboring entities and edges |
| `file`, `function`, `class`, `body`, `bodygrep`, `deps` | Exact-target file, symbol, body-search and dependency evidence for bounded worker reads and semantic-edit preparation |
| `impact` | Bidirectional callers, dependencies and affected files |
| `trace` | Compact incoming/outgoing execution trace with supporting evidence |
| `bundle` | Task-shaped evidence bundle for bugfix, feature, refactor, audit, optimize or explore work |
| `tags`, `symbols`, `summarize`, `stats` | Deterministic symbol classification and repository inventory |
| `hotspots`, `complexity`, `bottlenecks` | Ranked branch/loop/span and call-centrality views |
| `calls`, `testmap`, `coverage`, `auditmap` | Call/test relationships with runtime coverage kept explicitly `not_available` unless real execution evidence is imported |
| `churn`, `ownership`, `reviewqueue` | Index-time 90-day history, ownership concentration and bounded review priorities |
| `todo`, `gaps` | TODOs, low-confidence relations, missing test mappings and evidence gaps |
| `leaks`, `nullrisks`, `rawptrs`, `casts`, `crashes`, `looprisks`, `deadmethods`, `duplicates` | Non-blocking lexical risk candidates; confirmed blocking defects belong to Quality Evidence's Known Bug Scanner |
| `pipeline` | Compact focus → impact → verification planning packet |

Every response is row- and byte-bounded. Evidence labels distinguish directly
extracted facts (`EXTRACTED`), deterministic unresolved relations
(`INFERRED`), ambiguous identities (`AMBIGUOUS`) and truthful file-only facts
(`FILE_EVIDENCE`). Empty, stale and unsupported results remain explicit.
Risk analytics are candidate generators, not proof of a bug. In particular,
structural test mapping is not reported as line or branch coverage.

## Language coverage

AIWorkHub registers 34 code, data and documentation families. Semantic adapters currently extract
modules, imports, declarations, functions or methods, inheritance and observed
calls for:

- Python through the standard-library AST;
- PHP through a conservative lexical adapter;
- C, C++, CUDA, OpenCL and Metal through a C-family lexical adapter;
- JavaScript and TypeScript (including JSX/TSX);
- Rust, Go, Java and C#.

The remaining registered families, including JSON, XML, YAML, TOML and
Markdown/MDX repository documentation, are
indexed with truthful path/language/size/hash evidence instead of fabricated
symbols. Generated trees, dependency caches and build outputs are excluded by
the repository ignore policy before indexing.

## Agent evidence

Ranked results combine deterministic structural signals with repository-local
history captured during indexing: callers and callees, related tests, TODO or
risk markers, churn and ownership. Git history is never invoked at query time.
Ambiguous cross-file targets are left unresolved rather than attached to an
arbitrary symbol.

For code tasks, Source Graph usage is continuous rather than a one-time prompt
injection. Each worker query accepts an explicit bounded `workflow_stage`
(`orientation`, `implementation`, `validation`, `review` or `rework`). The
authenticated tool-use ledger records that stage together with mode, hits,
bytes, latency, cache state and authority identity. Review can therefore
distinguish multi-stage continuous use from a live single-stage call,
injected-only context and missing/stale use without guessing old metadata.

Each authenticated response also records the current index revision and
successful-build timestamp plus unique structural entity, call-edge and file
evidence counts. The successful-build timestamp is part of the query-cache key:
an incremental refresh starts a new cache generation and an older cached answer
cannot be reused against the refreshed graph. Repository KPI v3 aggregates
bounded latency and inter-call-gap p50/p95 values, evidence-row populations and
index-generation call counts. A bounded 15-minute informational threshold also
surfaces unusually long observed inter-call gaps with an exact count, rate and
sample denominator. A gap may include legitimate testing or reasoning time, so
it is never treated as proof of model inactivity. These are observed local
facts; they do not claim token savings or causation.

## Focused semantic edits

`body` responses already carry an exact repository-relative file and
`line_start`/`line_end` for the selected symbol. Workers can bind that small
range through `aiworkhub_worker_semantic_edit_prepare`, receive only the old
fragment plus its hashes, and submit only the replacement through
`aiworkhub_worker_semantic_edit_apply`. The local applier then:

1. rechecks the isolated-worktree file and fragment hashes;
2. rejects stale, overlapping, out-of-scope or symlinked targets;
3. applies the replacement atomically; and
4. leaves formatter, static-check and test execution to the task's declared
   validation contract.

VS Code LM workers use the equivalent `semantic_edit_response.v3` envelope:
full-file hash, inclusive line range and `new` text only. The old exact-text
replacement protocol remains readable for compatibility, while complete-file
rewrites are not the default for existing files.

Semantic-edit receipts expose observed bytes (`file_bytes`,
`old_region_bytes`, `replacement_bytes`, and fragment bytes returned). They do
not claim provider token savings. Controlled A/B runs must compare provider
input/output tokens, acceptance, retries and validation outcomes before any
economy multiplier is published.

## Repository isolation

The database, generations and settings belong to the selected repository's
`.aiworkhub/` directory. No global graph database is shared across workspaces.
Changing repository binding changes the graph authority atomically with the
task, callback, session, memory and KB authorities.
