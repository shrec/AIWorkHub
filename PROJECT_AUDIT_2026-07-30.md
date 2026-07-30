# AIWorkHub Project Audit — 2026-07-30

**Scope:** full repository audit of `/home/shrek/AIWorkHub` at `4e487a5` (branch `main`, version `0.8.0`).
**Method:** canonical AIWorkHub MCP read-only surfaces (task health, plan snapshot, source-graph health, storage/terminal retention previews, quality profile, environment preflight) plus direct test execution and bounded source inspection.

> Source Graph note: `aiworkhub_manager_source_graph_query` (bundle/audit) returned `hit_count: 0` for the retention subsystem query. Bounded exact-target inspection was used as fallback for that area only, per the documented unindexed-target exception.

---

## 1. Verdict

The project is **structurally healthy and functionally green**, but it carries a **systematic observability defect: several of its own governance surfaces report "clean" while the underlying reality is not measured.** The quality gate passes with zero checks, and the storage retention previews report ~370 MB while the true unmanaged footprint is ~7.1 GB.

No security vulnerabilities were found. No failing tests. The issues are in *self-verification truthfulness*, not in correctness of shipped behaviour.

| Area | Status |
| --- | --- |
| Test suite (Python) | Green — 1521 passed, 18 skipped, 0 failed (78 s) |
| Test suite (extension) | Green — all wired suites pass |
| Security posture | Green — no injection/exec/deserialization vectors found |
| CI / release pipeline | Green — 4-platform matrix, reproducible VSIX |
| Version consistency | Green — `0.8.0` across all projections |
| **Quality gate** | **Fail-open — passes with zero checks** |
| **Storage observability** | **Blind — under-reports footprint by ~19x** |
| **Static analysis** | **Absent — 43.5k LOC with no lint or typecheck** |
| Repository hygiene | Degraded — stale artifacts, leaked worktrees |

---

## 2. Baseline evidence (verified green)

- **Python tests:** `1521 passed, 18 skipped in 78.44s`, zero failures.
- **Extension tests:** `npm test` passes end to end; 13 pass / 1 skip in the launcher suite.
- **Version authority:** `scripts/release_metadata.py check` → `ok: true`, `mismatches: {}`; `0.8.0` in `_version.py`, `package.json`, `package-lock.json` (×2), `extension.js`.
- **Source Graph:** `ready`, 260 files, 478 entities, 17,742 edges, 0 errors, index age 176 s.
- **Task storage:** ready, canonical DB `repo_57de971f505d4a50a7729a99c32615de`.
- **Plan DAG:** valid, no cycles, 18 edges, 6 layers. Review queue empty.
- **Scale:** 217 Python files — `src` 43,568 LOC vs `tests` 46,699 LOC (**1.07:1 test-to-source ratio**), 150 test modules.
- **CI:** least-privilege `permissions: contents: read`, pinned major action versions, Linux/Windows/macOS/remote-SSH matrix, Python 3.10–3.12, byte-for-byte VSIX reproducibility check.

### Security review — clean

No matches for `shell=True`, `eval(`, `exec(`, `os.system(`, `pickle.loads`, or `yaml.load(` anywhere in `src/`. Positive controls observed:

- **SQL:** every f-string query interpolates internal constants (`_SPECS` table names, column allowlists, `PRAGMA table_info`); all user-supplied values are parameterized. No injection path found.
- **Path handling:** [context_importer.py](src/aiworkhub/context_importer.py#L55-L71) rejects absolute paths, `..` traversal, and per-component symlinks before resolving.
- **Read-only DB access:** SQLite opened via `file:…?mode=ro` URIs for all dashboard/import reads.
- **Secret redaction:** dedicated regexes in [dashboard.py](src/aiworkhub/dashboard.py#L57), [context_cache.py](src/aiworkhub/context_cache.py#L39), and [cli_adapter_dryrun.py](src/aiworkhub/cli_adapter_dryrun.py#L73); credential surfaces expose `api_key_present` booleans, never values.
- **Filesystem modes:** `0o700` directories and `0o600` credential files consistently applied in [worker_workspace.py](src/aiworkhub/worker_workspace.py#L519-L559).

---

## 3. Findings

### P1-1 — Quality gate is fail-open on missing configuration

`.aiworkhub/quality.json` does not exist in this repository. Calling the gate returns a green verdict:

```json
{"ok": true, "schema_id": "aiworkhub.quality_evidence.v1", "checks": []}
```

Root cause in [quality_evidence.py](src/aiworkhub/quality_evidence.py#L556-L561): when the config file is absent, `load_repo_config` silently returns `{"checks": []}`. The docstring promises "fail closed on malformed config" — but *missing* config is not malformed, so it passes.

Corroborated by `aiworkhub_environment_preflight`:

```json
"validation": {"required_check_ids": [], "declared_check_ids": [],
               "missing_check_ids": [], "config_error": ""}
```

**Impact:** a consumer reading `ok: true` cannot distinguish *"all checks passed"* from *"nothing was ever checked."* Every repository that has not authored a `quality.json` — including AIWorkHub itself — receives an unearned pass from the gate that governs task acceptance.

**Fix:** emit an explicit `status: "unverified"` (or `config_present: false`) and refuse to report `ok: true` when zero checks are declared.

---

### P1-2 — Retention previews do not measure the real disk footprint

The two governance surfaces both report "nothing to clean":

| Surface | Reported | Candidates |
| --- | --- | --- |
| `dashboard_storage_retention_preview` | 22,987,296 B (~23 MB) against a 5 GB cap | 0 |
| `dashboard_terminal_log_retention_preview` | 348,929,598 B (~349 MB) | 0, 130 protected |

Measured reality:

| Path | Size |
| --- | --- |
| `/tmp/aiworkhub-worktrees` | **3.2 GB** |
| `logs/` (repo root) | **3.6 GB** |
| `.aiworkhub/runtime` | 337 MB |
| **Total unmanaged** | **≈ 7.1 GB** |

`git worktree list` shows **12 of 13 registrations marked `prunable`**. Prune logic exists at [storage_retention.py](src/aiworkhub/storage_retention.py#L425), but the preview never surfaces these as candidates, so it is never invoked. The storage view reports 23 MB / 5 GB and concludes the repository is healthy while 3.2 GB of leaked worktrees sit outside its accounting.

**Fix:** measure actual on-disk bytes for every path the policy governs, including prunable worktree registrations and request files with no ledger row.

---

### P1-3 — Orphaned legacy log store: 3.6 GB unreachable by any tool

Retention governs `.aiworkhub/runtime/process_logs/` — see [terminal_log_retention.py](src/aiworkhub/terminal_log_retention.py#L28-L29). A second, older store still exists at repository root:

| Store | Files | Size | Newest write |
| --- | --- | --- | --- |
| `logs/processes` (legacy) | 4,344 | **3.6 GB** | 2026-07-21 |
| `.aiworkhub/runtime/process_logs/processes` (canonical) | 520 | 337 MB | 2026-07-30 |

The path migration left the legacy store behind with no migration step and no cleanup path. It is invisible to every retention, quarantine, and purge tool. Largest single artifact: `logs/processes/3c788c9b….stdout.log` at **327.8 MB**.

**Fix:** one-shot migration or purge, or extend retention to recognise the legacy path. Data is stale (9 days) and confirmed non-canonical.

---

### P2-4 — Event ledger has a hard cap but no rotation (fail-closed deadlock)

[terminal_log_retention.py](src/aiworkhub/terminal_log_retention.py#L35) sets `MAX_LEDGER_BYTES = 64 MiB`, and `_latest_rows` raises `terminal_log_ledger_too_large` above it. No rotation, compaction, or truncation logic exists anywhere in the module.

The canonical ledger is currently 2.25 MB — safe. But the **legacy ledger reached 51 MB within a single lifecycle**, proving the cap is reachable in normal operation.

Once crossed, `preview`, `quarantine`, and `purge` all raise. The only mechanism capable of reclaiming space is disabled by the size it was meant to control — an unrecoverable deadlock requiring manual intervention.

Secondary concern: `_latest_rows` calls `read_text()` on the entire ledger, holding up to 64 MB in memory plus the parsed dictionary.

**Fix:** rotate or compact before the cap is reached; stream the ledger instead of loading it whole.

---

### P2-5 — Per-run stdout is unbounded

Observed single-run capture sizes: **327.8 MB**, 184.1 MB, 103.1 MB, 86.9 MB, 73.7 MB. `process_launcher` reads *bounded byte ranges* for display but never bounds what is *written*. A single verbose worker can consume hundreds of megabytes before any retention window applies.

**Fix:** cap or roll per-run stdout at the writer.

---

### P2-6 — Stale verification verdict retained across claim epochs

`DEEPSEEK_V4PRO_AIWORKHUB_P0_ATOMIC_HANDOFF_GAP_20260730_V1` is persisted as:

- `status: blocked`, `worker_status: blocked`, `claim_epoch: 3`
- yet still carries `deterministic_verification: {pass: true, missing_required_output_count: 0, substatus: "review_ready"}`

Its declared required output `eval/aiworkhub_p0_atomic_handoff_gap_20260730.json` **does not exist** in the canonical tree.

The `deterministic_verification` block carries no epoch binding, so a passing verdict produced in an earlier claim epoch survives re-claim and re-block. Any consumer reading the card sees a passing verification attached to a blocked card with a missing artifact.

**Fix:** stamp verdicts with `claim_epoch` and invalidate them on re-claim or block transition.

---

### P2-7 — `blocked_count` contradicts `lifecycle` in the same payload

`aiworkhub_task_plan_snapshot` returns:

```json
"blockers": {}, "ready": [], "ready_capacity": 0,
"active_count": 2, "blocked_count": 0
```

while the `lifecycle` map in the *same response* marks two cards `blocked`, confirmed independently by `aiworkhub_task_list --status blocked`.

`blocked_count` counts only unmet DAG dependencies; cards blocked for lifecycle reasons are invisible to it. **Net effect: the queue is fully stalled — zero cards ready, zero capacity — while the summary counters report no blockage.** This is the exact condition an operator would rely on this surface to detect.

**Fix:** rename to `dependency_blocked_count`, or include lifecycle-blocked cards in the count.

---

### P2-8 — An extension test exists but never runs in CI

`vscode-extension/test/mux-launcher-packaging-b1038.test.js` is present and **passes when run standalone**, but is absent from the `npm test` chain — 23 of 24 test files are wired.

Its owning task `DEEPSEEK_V4FLASH_AIWORKHUB_LAUNCHER_PACKAGING_STRICT_B1038_V1` is recorded as `finished`. The task shipped a test that has never executed in CI.

Root cause: `npm test` is a hand-maintained literal `&&` chain of 23 filenames — structurally guaranteed to drift.

**Fix:** replace the chain with a glob runner (`node --test test/`).

---

### P3-9 — No static analysis for 43.5k LOC of Python

`pyproject.toml` ends after `[tool.pytest.ini_options]`. There is **no `[tool.ruff]`, no `[tool.mypy]`, no dev dependency group, and no lint or format configuration at all.** Neither CI workflow runs a linter or type checker.

`aiworkhub_quality_profile` confirms the gap:

```json
"declared_tools": {"pytest": true, "ruff": true, "mypy": true},
"installed_tools": {"ruff": false, "mypy": false, "gitleaks": false, "semgrep": true},
"runnable_tools": ["pytest"]
```

Ruff and mypy are *declared* but not installed, so neither can ever run. Additionally:

- `src/aiworkhub/py.typed` is shipped and declared in `package-data` — the package makes a **typed-package promise to downstream consumers with no typecheck gate anywhere** to back it.
- `semgrep` is installed but not declared; `gitleaks` is neither. The `secret_scan` lens mapped at [quality_evidence.py](src/aiworkhub/quality_evidence.py#L117) can therefore never produce evidence.

**Fix:** add ruff + mypy configuration, install them in CI, and declare them in `.aiworkhub/quality.json` so the gate in P1-1 has something to enforce.

---

### P3-10 — Repository hygiene

- **`src/geoai_task_mcp.egg-info/`** — stale artifact from the pre-rebrand package name, sitting inside the setuptools discovery root (`where = ["src"]`).
- **`scripts/`** retains ~12 one-shot historical scripts tracked in a published repository: `build_mcp_*_b119_v1.py`, `audit_*_b120_v1.py`, `train_mcp_*_b123_v1.py`, `survey_*_b116_v1.py`, plus a `__pycache__` directory.
- **4 untracked root reports** — `AUTONOMOUS_SESSION_SUMMARY.md`, `CODE_REVIEW_2026-07-23.md`, `NEXT_SESSION_HANDOFF.md`, `PROJECT_EVALUATION_2026-07-24.md`. Commit or delete; do not leave in limbo.
- **`eval/`** — 232 files / 8.4 MB of per-task artifacts distributed with the package; 4 more untracked.
- **CHANGELOG drift** — tag `v0.8.0` exists and `_version.py` reads `0.8.0`, but the changelog has **no `## [0.8.0]` section**; its 8 release bullets sit under `## [Unreleased]`. `release_metadata.py check` validates version projections but not changelog sectioning, so the release passed with unreleased-labelled notes.

---

## 4. Recommended sequence

1. **P1-1** — close the fail-open quality gate, then author `.aiworkhub/quality.json` for this repository.
2. **P1-2 / P1-3** — make retention previews measure real bytes; reclaim the ~6.8 GB in `logs/` and `/tmp/aiworkhub-worktrees`.
3. **P2-4 / P2-5** — add ledger rotation and a per-run stdout cap before the 64 MiB deadlock is reached in production.
4. **P2-6 / P2-7** — bind verification verdicts to claim epochs; correct the blocked-count semantics.
5. **P2-8 / P3-9** — glob the extension tests; add ruff and mypy to CI.
6. **P3-10** — clean stale artifacts and add the `## [0.8.0]` changelog section.

Items 1–3 are the substantive ones: the project's self-reported health is currently better than its measured health, and that gap is what this audit primarily documents.

---

## 5. Remediation status

The audit is retained as point-in-time evidence at `4e487a5`; findings are not
rewritten after remediation.

- **P1-1 closed:** missing/empty repository quality configuration now reports
  `unverified` and public execution returns `ok: false`; evidence distinguishes
  builtin/task-contract verification from configured repository policy.
  AIWorkHub carries portable declared quality checks using `{python}` resolved
  to the active interpreter without shell execution.
- **P2-8 closed:** `npm test` now discovers every `*.test.js` file
  deterministically and runs them sequentially. The previously omitted
  `mux-launcher-packaging-b1038.test.js` is included.
- **P1-2/P1-3, P2-4/P2-5, P2-6/P2-7 and P3-9/P3-10 remain open** and are
  tracked in `MVP_ROADMAP.md` in operational-risk order.
