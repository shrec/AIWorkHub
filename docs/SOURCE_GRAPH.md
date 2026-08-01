# Source Graph

AIWorkHub's Source Graph is a repository-local structural index built under
`.aiworkhub/source_graph/`. It gives managers and workers bounded code evidence
without repeatedly scanning the repository tree. Initialization builds the
first index; the repository daemon refreshes changed files incrementally.

## Query modes

The manager and worker MCP surfaces expose the same bounded modes. All modes
read the one canonical repository database; analytics never create a parallel
index or decision store.

| Mode | Use |
| --- | --- |
| `focus` | Ranked symbols, hot paths, tests, ownership and churn around a target |
| `slice` | Minimal dependency and call slice needed to change a target |
| `context` | File or symbol structure with neighboring entities and edges |
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

## Repository isolation

The database, generations and settings belong to the selected repository's
`.aiworkhub/` directory. No global graph database is shared across workspaces.
Changing repository binding changes the graph authority atomically with the
task, callback, session, memory and KB authorities.
