# AIWorkHub — პროექტის შეფასება, გაუმჯობესების ფანჯრები და ბაგების სია

**თარიღი:** 2026-08-06
**HEAD:** `cbcf975` (`main`, origin/main-ზე 6 კომიტით წინ; tag `v0.9.0` = `85d4cd5`)
**გაფართოების ვერსია:** `vscode-extension` 0.9.4
**მეთოდი:** manager bootstrap + Source Graph orientation/review + არსებული აუდიტების/ledger-ების სინთეზი + ცნობილი სამიზნეების targeted verification
**შენიშვნა:** კოდი ამ სესიაში არ შეცვლილა. ეს არის შეფასება/ინვენტარი, არა performance claim.

---

## 1. Executive summary

AIWorkHub არის **production-grade, local-first multi-model AI coding control plane** VS Code/MCP-ისთვის. რეპოზიტორია-სკოპირებული task authority, isolated worker launch, Source Graph v5, evidence-gated review, Session/Memory/KB და callback lifecycle უკვე რეალურად მუშაობს — არა მხოლოდ დოკუმენტში.

ბოლო ტალღამ (`v0.8.85` → `v0.9.0` + 6 unpushed fix) დახურა მრავალი P0 control-plane დეფექტი (residual rework, recursive card inflation, Source Graph read-only, cost honesty, reviewer protocol). **კრიტიკული data-loss/crash კლასის ახალი ხვრელი ამ გადამოწმებით არ გამოჩნდა.**

მთავარი დარჩენილი რისკი აღარ არის „არ მუშაობს“, არამედ:

1. **სიმართლის/ცრუ-მწვანე ხარისხი** (behavioral false-green, სუსტი meaningful-output, reviewer semantic emptiness);
2. **VS Code LM / MCP isolation** (model timeout → control-plane circuit);
3. **მოდულური სიმძიმე** (`process_launcher.py` ~7.3k, `core.py` ~5.8k, `extension.js` ~7.3k);
4. **მიძინებული, უკვე დაწერილი capability-ების გააქტიურება** (quality adapters, context_cache, context_economics);
5. **benchmark-first ეკონომიკა** (routing/cost-per-accepted-outcome ჯერ advisory/measurement stage-ზეა).

| განზომილება | ქულა (1–5) | კომენტარი |
|---|---:|---|
| არქიტექტურული სიცხადე | 4.5 | repo-local authority, fail-closed gates, evidence boundary — ძლიერი |
| Control-plane სიმწიფე | 4.0 | task/review/callback/rework ძირითადად დახურულია; რამდენიმე lifecycle edge რჩება |
| Source Graph | 4.0 | v5 structural/slice ძლიერია; NL retrieval + lexical call precision ჯერ გასაზომია |
| ხარისხის გეითები | 3.5 | declared checks/evidence instruments არის; behavioral adequacy და dormant adapters — ღია |
| Token/cost honesty | 4.0 | unknown≠0, live budget opt-in, role/retry telemetry; matched economic routing ჯერ არა |
| VS Code extension სიმწიფე | 3.5 | მუშაობს, მაგრამ JS monolith + recovery/consent edge cases |
| ტესტის საფარი | 4.0 | ~177 test module; focused/full local suites ისტორიულად მწვანე; matrix release continuous |
| ოპერაციული მზადყოფნა | 4.0 | retention, quarantine, preflight, dashboard; cross-platform release discipline საჭიროა |
| **საერთო პროდუქტის სიმწიფე** | **4.0 / 5** | **v0.9.x — usable production control plane; ხარისხის/ეკონომიკის შემდეგი ნახტომი measurement + dormant wiring-ია** |

---

## 2. რა არის უკვე ძლიერი

- **Repository-native authority:** ყოველი repo-ს საკუთარი `.aiworkhub/`; არა shared cloud DB.
- **Evidence before accept:** diffs, validation, tool-use receipts, residual hashes, read-only reviewer path.
- **Source Graph v5:** focus/slice/body/calls/trace/impact/testmap + authenticated telemetry; incremental index.
- **Multi-model portfolio:** Codex / Claude / Copilot / DeepSeek / GLM routes; capability-aware routing surface.
- **Defensive persistence:** optimistic locking, atomic replace writes, bounded projections, retention/quarantine.
- **Honest economics direction:** uncapped-by-default, explicit opt-in live token cap, unknown cost not shown as `$0`.
- **ბოლო 6 local fix (unpushed):** reviewer packet protocol, launch context fast path, evidence gating, stall reconciliation, VS Code LM progress truth, uncapped capture/rework recovery.

დადებითი წყაროები: [ARCHITECTURE.md](ARCHITECTURE.md), [PRODUCT_ROADMAP.md](PRODUCT_ROADMAP.md), [AIWORKHUB_PRODUCTION_DEFECT_LEDGER_2026-08-02.md](AIWORKHUB_PRODUCTION_DEFECT_LEDGER_2026-08-02.md) (18/18 fixed disposition).

---

## 3. მეტრიკები (HEAD `cbcf975`)

| მეტრიკა | მნიშვნელობა |
|---|---|
| Python package modules (`src/aiworkhub/*.py` tree) | ~92 |
| Test modules (`tests/test_*.py`) | ~177 |
| Eval artifacts | ~234 |
| უდიდესი მოდულები | `process_launcher.py` 7318; `extension.js` 7282; `core.py` 5769; `worker_ai_tools_mcp.py` 3049 |
| Source Graph risk hotspot | `ProcessManager.accept_review` (~938 ხაზი, branch_heavy/large_symbol, priority 682) |
| `collect_project_context` | large/branch_heavy (priority 297) |
| Tree size (approx) | `src` 6.2M; `tests` 14M; `docs` 3.5M; `vscode-extension` 696M (node_modules/build artifacts სავარაუდოდ) |

---

## 4. დახურული vs ღია — მოკლე ისტორია

| პაკეტი | სტატუსი |
|---|---|
| 2026-08-02 production defect ledger (18 item) | **Fixed** (local + focused/full suite evidence; release matrix continuous) |
| Live token budget e2e wiring | **Fixed** (opt-in `max_live_tokens`, live vs posthoc honesty) |
| Residual rework / predecessor materialization | **Fixed** (ledger + later hardening commits) |
| Cost unknown-as-zero / KPI false 100% accept | **Fixed** |
| Worker Source Graph read-only WAL failure | **Fixed** (canonical graph DELETE journal) |
| Reviewer packet / stall / progress truth (2026-08-06 commits) | **Fixed locally, unpushed** |
| VS Code LM broker vs MCP circuit isolation | **Still open** (`mcp_recovery_circuit_open` still present) |
| Research meaningful-output anti-placeholder | **Partially open** (non-empty text only; ellipsis/placeholder reject არ ჩანს) |
| Task store WAL/busy_timeout | **Still open** (verified: no WAL/busy in `task_store._connect`) |
| AST `list(text)` masking cost | **Still open** (verified in `source_graph_ast.py`) |
| Dormant `context_cache` runtime wiring | **Still open** (0 runtime refs outside self) |
| `cost_per_accepted_outcome` advisory | **Still open** |
| Quality evidence adapters in completion gate | **Mostly dormant** (defs exist; runtime call fanout ~1) |

---

## 5. ბაგების სია (ღია / ნაწილობრივ ღია)

პრიორიტეტი: **P0** = correctness/control-plane; **P1** = reliability/perf/UX; **P2** = hygiene.

### 5.1 P0 — correctness და control-plane

| ID | არე | პრობლემა | მტკიცებულება / შენიშვნა | შემოთავაზებული მიმართულება |
|---|---|---|---|---|
| **B-P0-01** | VS Code LM ↔ MCP | Model/broker timeout-მა შეიძლება გახსნას **მთელი MCP recovery circuit** (`mcp_recovery_circuit_open`), რითაც manager tools (task/SG/memory) ერთად იკარგება | Live backlog P0.2; `extension.js` ჯერ კიდევ შეიცავს `mcp_recovery_circuit_open` | გამოყავი editor-model circuit MCP server circuit-ისგან; degrade მხოლოდ failed adapter/route |
| **B-P0-02** | Consent lifecycle | Editor model consent-ის circular persistence (approve → timeout → consent არ ინახება → თავიდან prompt) ისტორიულად live-ში დაფიქსირდა | Live backlog P0.1; extension-ში `consent` სტრიქონი ამ სპოტ-ჩეკში პირდაპირ არ გამოჩნდა — **გადაამოწმე სახელდების/სტორიჯის მიმდინარე API** სანამ fix-ს დაიწყებ | Persist explicit user approval response-success-ისგან დამოუკიდებლად; state machine: unknown/prompting/approved/denied/stale |
| **B-P0-03** | Preflight honesty | `model_visible` ≠ `consent_ready` ≠ `bridge_ready` ≠ `request_roundtrip_ready`; live-ში 34 visible model + ყველა reviewer pre-turn timeout | Live backlog P0.3 | განაცალკევე readiness კომპონენტები + bounded canary age |
| **B-P0-04** | Research meaningful gate | `_readonly_research_result_evidence` meaningful = `result_count > 0 and result_chars > 0` — **literal `"..."` ან placeholder შეიძლება გაიაროს** | `process_launcher.py` ~1738; evolution doc P0; ellipsis reject არ ჩანს | Reject ellipsis/placeholder; მოითხოვე ≥1 verifiable finding ან explicit evidence-backed `inconclusive` + raw hash |
| **B-P0-05** | Behavioral false-green | Declared validations შეიძლება 100% green იყოს უსარგებლო ქცევაზე (მაგ. 320/320 single-class collapse) | Live backlog P0/P2.1; B1458 V1 | Task-type opt-in behavioral gates: denominator, distribution, baseline, anti-collapse |
| **B-P0-06** | Reviewer semantic emptiness | Authenticated receipt `findings: []` მიუხედავად აშკარა packet evidence-ისა | Live backlog P1.1 (quality-critical) | Lens-specific required assertions; განასხვავე `no_findings_after_review` vs empty/default |
| **B-P0-07** | Callback/task txn split | `mark_terminal_review` → task write და callback enqueue **განცალკევებული connection/transaction** (TOCTOU) | 2026-08-06 audit H3 `task_engine.py` | ერთი ტრანზაქცია ან transactional outbox same-commit |
| **B-P0-08** | `enqueue_callback` error path | `IntegrityError`-ის გარდა სხვა sqlite error-ზე open transaction / rollback gap | 2026-08-06 audit H2 `callback_store.py` | `finally: rollback` non-success path-ზე |

### 5.2 P1 — reliability, extension, storage

| ID | არე | პრობლემა | შენიშვნა |
|---|---|---|---|
| **B-P1-01** | `repository_bootstrap.py` | Bare `except Exception` source graph daemon startup-ზე — `MemoryError`/`RecursionError` გადაყლაპვა | Audit H1 |
| **B-P1-02** | `task_store._atomic_write_json` | `os.fdopen` failure-ზე fd leak | Audit M1 — იაფი fix |
| **B-P1-03** | `vscode_lm_bridge._atomic_json` | `unlink` მხოლოდ `FileNotFoundError` — Windows `PermissionError` temp leak | Audit M2 |
| **B-P1-04** | `task_store.list_tasks` | შეიძლება ყველა row Python-ში ჩაიტვირთოს filter-მდე | Audit M3; SQL LIMIT/predicate გაძლიერება |
| **B-P1-05** | Extension recovery | `McpStdioClient._scheduleAutomaticRecovery` `.finally()` შეიძლება maxAttempts budget-ს გვერდი აუაროს | Audit VH1 |
| **B-P1-06** | Extension `ensureStarted` | readiness vs start TOCTOU; recovery circuit bypass | Audit VH2 |
| **B-P1-07** | `VscodeLmBridgeHost.stop` | double-stop after dispose | Audit VH3 |
| **B-P1-08** | `runtime-retention.js` | `lstatSync` unhandled TOCTOU | Audit VM1 |
| **B-P1-09** | `sanitizeWebviewPayload` | Windows absolute path pattern სუსტი | Audit VM2 |
| **B-P1-10** | Stream error guards | child stream errors მხოლოდ log — pending MCP requests არ fail-დება | Audit VM4 |
| **B-P1-11** | Source Graph standby preflight | ისტორიულად `ok=true` + standby მაინც `ready_for_code=false` | Live P1.3 — გადაამოწმე მიმდინარე preflight semantics |
| **B-P1-12** | SG result provenance | context/testmap row შეიძლება target-ს არ ემთხვეოდეს | Live P1.4 — fail-closed target consistency |
| **B-P1-13** | Forbidden glob overmatch | `**/*fragment*` unrelated artifacts-საც ურტყამს | Live P1.5 |
| **B-P1-14** | Validation evidence truncation | long failures 500-char clip / empty `validation: []` ისტორია | Live P0.7 — structured per-command head+tail |
| **B-P1-15** | Callback WAL retry | linear backoff without jitter | Audit L2 |

### 5.3 P2 — low severity / accepted risk

| ID | პრობლემა |
|---|---|
| **B-P2-01** | `source_graph` index lease unlock path edge (`ValueError` on closed fd) |
| **B-P2-02** | `os.replace` without dir fsync (POSIX accepted) |
| **B-P2-03** | macOS `/proc` absence silent degrade |
| **B-P2-04** | Webview `innerHTML` as entity decoder (CSP-bound, fragile) |
| **B-P2-05** | `renewWindowRouteLease` silent swallow every 4 min |
| **B-P2-06** | process_launcher log read TOCTOU (observability-only) |

---

## 6. გაუმჯობესების ფანჯრები

### 6.1 P0 — ახლა (მაღალი ROI, correctness/perf)

| ID | ფანჯარა | რატომ | Effort | Expected effect |
|---|---|---|---|---|
| **I-P0-01** | **MCP ↔ VS Code LM circuit isolation** | ერთი provider timeout არ უნდა მოკლას მთელი control plane | M | Manager tools რჩება ცოცხალი route degrade-ისას |
| **I-P0-02** | **Meaningful research/output gate გაძლიერება** | ცრუ review_ready content-free შედეგზე | S | Correctness; evolution roadmap #1 |
| **I-P0-03** | **Task store SQLite WAL + busy_timeout + synchronous=NORMAL** | `task_store._connect`-ში pragma არ არის (callback_store/source_graph-ს აქვს) | S | Concurrent throughput / `SQLITE_BUSY` ↓ (გაზომე A/B-ით) |
| **I-P0-04** | **AST non-code masking: `list(text)` → bytearray/streaming** | `_mask_php_non_code` / `_mask_c_family_non_code` / `_mask_comments_preserve_strings` | M | Index CPU/RAM ↓ დიდ ფაილებზე (benchmark-first) |
| **I-P0-05** | **Callback enqueue atomicity (B-P0-07/08)** | Terminal review სიმართლე | M | Lost/dup callback ↓ |
| **I-P0-06** | **Quality adapters wiring** (`adapt_coverage_summary`, SARIF, JUnit, benchmark, AI findings) → completion gate | კოდი უკვე წერია და ტესტირებულია, runtime fanout თითქმის ნულია | S–M | Change-sensitive / external evidence without rewrite |

### 6.2 P1 — შემდეგი ციკლი (პროდუქტი + ეკონომიკა)

| ID | ფანჯარა | შენიშვნა |
|---|---|---|
| **I-P1-01** | Advisory **cost_per_accepted_outcome** matched denominators | `cost_ledger` + `workforce_router`; unknown never ranks free; no auto-routing until canary |
| **I-P1-02** | Diagnostic **delta-rework** proposals from failed validation receipts | Opt-in per failure class; stop conditions |
| **I-P1-03** | Manager-assisted **task decomposition** from SG impact/deps | Advisory only; manager approves child DAG |
| **I-P1-04** | Source Graph **retrieval eval corpus** (precision@k, MRR, bytes, latency, accepted outcomes) | ვექტორები მხოლოდ A/B-ის შემდეგ |
| **I-P1-05** | Lexical **C/C++/CUDA/PHP call-edge** labeled benchmark | impact/trace quality |
| **I-P1-06** | Wire **`context_economics`** into KPI/usage surfaces | measurement truth, არა გამოგონილი savings |
| **I-P1-07** | Decide **`context_cache`**: wire safely (repo+rev+task+input hash) **ან** მონიშნე ops-only | Cross-session cache უსაფრთხოების გარეშე აკრძალულია |
| **I-P1-08** | **Context Graph** default-on ან manager-handoff auto-enable | ახლა default `False` |
| **I-P1-09** | Durable **quality metrics store** | escape_rate, rework_yield, disagreement, false-green/red |
| **I-P1-10** | Extension recovery circuit hardening (VH1/VH2/VH3) | episode counter + disposed guards |
| **I-P1-11** | `accept_review` decomposition | 938-line branch_heavy hotspot — characterization tests first |
| **I-P1-12** | Behavioral task templates (ML/data anti-collapse) | opt-in by task type |

### 6.3 P2 — არქიტექტურა / DX / scale

| ID | ფანჯარა |
|---|---|
| **I-P2-01** | `process_launcher.py` / `core.py` / `extension.js` characterization-first extraction |
| **I-P2-02** | Extension incremental TypeScript migration + reload E2E |
| **I-P2-03** | Event-driven dashboard transport (replace hot polling paths only; keep snapshot reconcile) |
| **I-P2-04** | Optional OCI/rootless worker sandbox behind capability flags |
| **I-P2-05** | Federated cross-repo dependency **receipts** (never shared SQLite) |
| **I-P2-06** | OPTIONAL_GATES (semgrep/osv/gitleaks/…): real run **ან** ამოღება ცრუ დაპირების თავიდან ასაცილებლად |
| **I-P2-07** | Honor `repo_policy.tools.session_memory_kb_required_for_nontrivial` (ახლა parsed, underused) |
| **I-P2-08** | Source Graph connect migration guard via `PRAGMA user_version` |
| **I-P2-09** | Combined aggregation for SG quality scorecard |
| **I-P2-10** | `storage_observability` / retention tree size via `scandir` |
| **I-P2-11** | Push unpushed 6 fixes + full Linux/Win/macOS/Remote-SSH release matrix before next tag |

### 6.4 რა **არ** უნდა გაკეთდეს ბრმად

adversarial token-economy აუდიტის მიხედვით ([AUDIT_SOURCE_GRAPH_AND_TOKEN_ECONOMY_2026-08-04.md](AUDIT_SOURCE_GRAPH_AND_TOKEN_ECONOMY_2026-08-04.md)):

| იდეა | ვერდიქტი |
|---|---|
| Local tokenizer როგორც runtime P0 | **უარყოფილი** — authority = provider usage receipt |
| Default token/USD caps | **უარყოფილი** — მხოლოდ explicit owner opt-in |
| Cross-session response cache როგორც token saving | **უარყოფილი** სანამ input ისევ მოდელს ეგზავნება / stale risk |
| Bundle cap-ის უბრალოდ გაზრდა | **benchmark-first only** |
| Structural byte ratio → token multiplier claim | **აკრძალული** paired provider receipt-ის გარეშე |
| Auto economic routing | **advisory → shadow → canary** მხოლოდ matched denominators-ით |

---

## 7. რეკომენდებული სამუშაო რიგი (შემდეგი 2–3 კვირა)

```text
1) B-P0-01 / I-P0-01  VS Code LM circuit ≠ MCP circuit
2) B-P0-04 / I-P0-02  meaningful-output anti-placeholder + research gate tests
3) I-P0-03            task_store WAL/busy_timeout (A/B concurrent lock bench)
4) B-P0-07/08         terminal review + callback atomicity
5) I-P0-06            wire quality adapters into completion gate
6) I-P0-04            AST masking rewrite + index bench
7) I-P1-10            extension recovery/dispose hardening
8) I-P1-01            cost_per_accepted_outcome advisory view (no auto route)
9) I-P2-11            push + platform matrix for 0.9.x hardening train
10) I-P1-04/05        SG retrieval + call-edge corpora before any vector talk
```

---

## 8. მიძინებული / ნახევრად-გააქტიურებული capability inventory

| კომპონენტი | მდგომარეობა | მოქმედება |
|---|---|---|
| `context_cache.py` | არსებობს; **0 runtime import** | wire უსაფრთხო key-ით ან მონიშნე non-runtime |
| `context_economics.py` | არსებობს; ~1 runtime ref | KPI/usage-ში სრული integ. |
| Quality adapters (SARIF/JUnit/coverage/benchmark/AI findings) | defined+tested; completion path-ში თითქმის unused | wire or document-as-library |
| `context_graph` feature flag | default **False** | product decision: on vs opt-in |
| OPTIONAL_GATES | metadata/binary presence only | run or remove from inventory claims |
| `session_memory_kb_required_for_nontrivial` policy knob | validated/stored; under-enforced | honor in launcher gate |
| Migration CLIs (`fresh_task_store`, `*_migration`) | test/ops oriented | keep labeled as ops tools |

*(2026-08-03 აუდიტში Claude `ai_memory_get/related` allow-list gap იყო; HEAD-ზე runtime_adapters-ში ორივე tool უკვე ჩანს allowedTools სიაში — **განიხილე დახურულად**, regress test შეინახე.)*

---

## 9. რისკები release-მდე

1. **6 local commit unpushed** (`4b237bd`…`cbcf975`) — origin/main ჯერ `v0.9.0`-ზეა; CI matrix ამ ფიქსებზე ჯერ არ გაშვებულა remote-ზე.
2. **Monolith blast radius** — `accept_review` და extension recovery ნებისმიერი ცვლილება მოითხოვს characterization tests-ს.
3. **False-green product risk** — platform შეიძლება „მწვანე“ იყოს უსარგებლო მოდელის შედეგზე, თუ behavioral gates არ ჩაირთო task type-ზე.
4. **Claim hygiene** — ნუ გამოაქვეყნებ token/cost multiplier-ს A/B artifact-ის გარეშე.

---

## 10. დასკვნა

AIWorkHub **v0.9.x** უკვე არის სერიოზული, გამოყენებადი control plane: იზოლირებული multi-model orchestration, Source Graph, evidence review და repo-local memory/KB არ არის MVP თეატრი.

შემდეგი ნახტომი არის:

- **სიმართლის გაძლიერება** (circuit isolation, meaningful output, behavioral anti-collapse, reviewer substance);
- **იაფი infra wins** (task_store WAL, AST mask, callback atomicity);
- **უკვე დაწერილი capability-ების გაღვიძება** (quality adapters, economics surfaces);
- **გაზომვა → შემდეგ ოპტიმიზაცია** (routing, SG retrieval, decomposition, rework) — ბრმა cap/tokenizer/cache P0-ების გარეშე.

---

## წყაროები

- HEAD audit context: git `cbcf975`, Source Graph index `semantic.v5` @ 2026-08-06
- [AUDIT_BUGS_AND_OPTIMIZATION_2026-08-06.md](AUDIT_BUGS_AND_OPTIMIZATION_2026-08-06.md)
- [LIVE_BUG_BACKLOG_2026-08-03.md](LIVE_BUG_BACKLOG_2026-08-03.md)
- [AIWORKHUB_PRODUCTION_DEFECT_LEDGER_2026-08-02.md](AIWORKHUB_PRODUCTION_DEFECT_LEDGER_2026-08-02.md)
- [AUDIT_2026-08-03.md](AUDIT_2026-08-03.md)
- [AUDIT_SOURCE_GRAPH_AND_TOKEN_ECONOMY_2026-08-04.md](AUDIT_SOURCE_GRAPH_AND_TOKEN_ECONOMY_2026-08-04.md)
- [SYSTEM_EVOLUTION_RECOMMENDATIONS_2026-08-05.md](SYSTEM_EVOLUTION_RECOMMENDATIONS_2026-08-05.md)
- [PRODUCT_ROADMAP.md](PRODUCT_ROADMAP.md)
- [ARCHITECTURE.md](ARCHITECTURE.md)

*Targeted verification this session: `task_store` WAL absent; AST `list(text)` present; research meaningful = non-empty chars only; `mcp_recovery_circuit_open` present; `context_cache` unwired; `cost_per_accepted` absent; Claude ai_memory tools present in allow-list; `accept_review` hotspot confirmed via Source Graph.*
