# SDLC Loop Closure (RM-2026-00076, release 0.12.5)

## Goal

AIWorkHub runs the playbook's six-stage loop (plan, design, build, test, deploy,
maintain) with no human in the per-card loop. Every gate the playbook gives a
person is held by the manager seat plus mechanical measurement. The owner sets
goals only.

This spec builds on `2026-09-20-model-neutral-sdlc-design.md`, which introduced the
case store and the proof gate. This spec only wires them in and adds the missing
producers.

## What already exists (do not rebuild)

- `sdlc_case_store` + `sdlc_stage_evidence`: cases, stage receipts, and a
  server-side proof for plan/design/build/test drawn from canonical receipts.
  Deploy and maintain are refused with named `MISSING_PRODUCERS`.
- `sdlc_outcome_metrics`: first-pass acceptance, review rounds per accepted
  task, and escaped-defect attribution over `needfix.caused_by`.
- `needfix_store.validate_caused_by`: verifies a cause against an accepted
  outcome receipt (`promoted_paths`, `changed_path_hashes`, `base_oid`).
- `scripts/build_accepted_task_eval.py`: a 20-50-row accepted-task eval corpus
  with an offline `--check`, and a historical-provenance link from a receipt to
  the first canonical commit after `base_oid` that holds every promoted-path hash.

The gap is that nothing calls these tools automatically. As of 2026-09-29, no
task has a case (`sdlc_case_for_task` returns null) and attribution is 0/50.

## Playbook to mechanism mapping

| Playbook lesson | Human gate in the playbook | AIWorkHub mechanism |
|---|---|---|
| 8. Feedback loop | The engineer sets up checks and reviews the PR with evidence attached | The card's `validation` commands are the quantified target. The worker loops until they pass. The manager review runs the mechanical gates first. The destructive-diff gate stops a worker from weakening tests. |
| 9. Continuous evals in CI | The team reviews a pass-rate drop before merge | The accepted-task corpus `--check` runs in the CI quality job (landed with this spec). Every attributed escaped defect's fix card carries a regression test, which stays in `tests/`. |
| 13. Closing the loop on metrics | The service owner triages 2σ/3σ findings | A deterministic band detector with no model. 1σ is logged only. 2σ files a NeedFix. 3σ files a high-severity NeedFix. The manager seat is the triager and accepts or rejects the fix card. |
| Six-stage loop | A person advances each stage | The reconciler sweep records every stage the server can prove. It never claims a stage it cannot prove. |

## Components

### A. Band detector — `src/aiworkhub/sdlc_control_bands.py` (card C2)

- **Config.** The config lives in `.aiworkhub/config/sdlc_bands.json` (tracked):
  `{"metrics": [{"id", "direction", "window_cards", "baseline_cards", "min_baseline"}]}`.
  - `direction` is `lower_is_bad` or `upper_is_bad`.
  - Defaults: window 20, baseline 100, min_baseline 30.
- **Metrics:**
  - `first_pass_acceptance` (lower_is_bad);
  - `review_rounds_per_accepted_task` (upper_is_bad);
  - `validation_failed_rate` (upper_is_bad): the share of worker terminal outcomes with substatus `validation_failed`, from canonical task events.
- **Windows.** The recent window is the newest `window_cards` decided cards. The baseline is the `baseline_cards` decided cards before the window. The two do not overlap.
- **Rates.** `z = (p - p0) / sqrt(p0(1-p0)/n)`. p0 is clamped to `[0.5/n, 1-0.5/n]`, so σ is never 0.
- **Means (review rounds).** `z = (m - m0) / (s0 / sqrt(n))`. s0 is the baseline sample σ, floored at 0.25.
- **Tiers.** Only the bad direction counts:
  - |z| ≥ 1: `log` (the finding is returned; nothing is written);
  - ≥ 2: `needfix` with severity `medium`;
  - ≥ 3: `needfix` with severity `high`.
- **Insufficient population.** If the baseline is smaller than `min_baseline`, the result is `insufficient_population` and nothing is filed.
- **Filing.** Filing goes through `needfix_store`. The dedupe key is `sdlc_band:<metric>:<direction>`, so there is one open NeedFix per metric. A re-breach updates the evidence and never opens a duplicate. The evidence carries p/m, p0/m0, n, z, the tier, the window task_ids and the config sha256.
- **Scope limits.** It never calls a model, never launches a card, and never reads prose.
- **API:**
  - `evaluate(repo_root, repository_id) -> BandReport` is pure and read-only.
  - `file_breaches(repo_root, repository_id, report) -> list[needfix_id]` is idempotent.

### B. Escaped-defect attribution — `src/aiworkhub/sdlc_attribution.py` (card C3)

**Input:** a NeedFix whose `converted_task_id` card was accepted, meaning the fix is known.

1. From the fix's accepted receipt, take the `base_oid` and the promoted non-test paths. Find the fix's canonical commit with the existing descendant-commit helper.
2. Diff `base_oid..fix_commit` per path. Blame the pre-image lines the fix deleted or modified at `base_oid` to get the blamed commits.
3. Map each blamed commit to an accepted receipt. A receipt matches when it has that path in `promoted_paths` and its first canonical holding commit is exactly the blamed commit.
4. **Cause.** The receipt that explains the most blamed lines is the cause. A tie is `unknown/tie`. A fix that only adds lines is `unknown/additive_only`. A blamed commit with no receipt, such as a manager commit, is `unknown/no_receipt`.
5. **Write.** Write `caused_by` through `needfix_store` with `validate_caused_by`. It is immutable once set.
   - If `needfix_update` does not accept `caused_by` yet, add it with the same validation.
   - An existing, different cause is refused.

- **Backfill.** Run the entry point `attribute_all(repo_root, repository_id) -> AttributionReport` once over existing rows. It reports attributed / unknown counts by reason.
- **Scope limits.** No model, no heuristics on prose.

### C. SDLC sweep — `src/aiworkhub/sdlc_sync.py` (card C1, after C2 and C3)

- **Trigger.** A single idempotent pass called from the task reconciler scan. The reconciler is the one call site that sees create, review_ready and accept.
- **Cursor.** It keeps a durable cursor of the last task event processed.
- **Per task with new events:**
  - ensure its case exists (`sdlc_case_create_for_task`, deterministic id, replay-idempotent);
  - try each stage in order: plan, design, build, test.
- **Stage payloads** are derived from the card only:
  - plan: `intent`/`problem` come from the objective; `expected_outcome` from the acceptance; `owner` from the manager route; `risk` from the risk tier.
  - design: `acceptance_criteria` come from the acceptance, `affected_contracts` from allowed_writes, and `constraints` from forbidden.
  - build/test: identity pointers come from the current claim.
- **Refusals.** A refusal leaves the stage `unknown` and records the typed reason. It is never forced.
- **After stages:** run `sdlc_attribution` for fix cards accepted since the cursor. Then run `sdlc_control_bands.evaluate` + `file_breaches` once per sweep, if any card was decided since the last band run.
- **Failure handling.** Each part is guarded, so a failure is recorded once as `sdlc_sync:<part>:failed` and never breaks the reconciler.

### D. Deploy producer — `scripts/release_receipt.py` (card C5)

- **Command.** `record --version X --vsix PATH` appends one JSON line to `.aiworkhub/releases.jsonl` (tracked). The line holds:
  - `version`, `release_commit` (HEAD);
  - `vsix_sha256`, `built_at` (ISO-8601 UTC);
  - `previous_vsix` (`{path, sha256}` for rollback);
  - `target` (`vscode_local`).
- **Installed confirmation.** `confirm --version X --server-version V` appends a confirmation line with `installed_at` and the observed `server_version`. It refuses when V != X.
- **Integration.** The release recipe calls it after `npm run package` and again after the reload.

### E. Deploy + maintain proofs (card C6, after C1 and C5)

- **Deploy is ready for a card** when a confirmed release receipt exists whose `release_commit` holds every promoted-path hash of the card's accepted receipt, or a later accepted receipt for that path.
- **Deploy config.** The target allowlist and deploy approval policy come from `.aiworkhub/config/policy.json` (`deploy.targets`, `deploy.approver: manager_seat`).
- **Maintain is ready** when deploy is ready, the band report is not breached for the release window, and no open NeedFix has `caused_by` pointing to this card.
- **Changes.** Remove the corresponding `MISSING_PRODUCERS` entries. `sdlc_sync` records these stages as well.

## Waves

- **Wave A (parallel, disjoint writes):** C2, C3, C5. The CI eval step is already landed by the manager.
- **Wave B:** C1.
- **Wave C:** C6.

## Acceptance for RM-2026-00076

1. A task created after C1 lands gets a case and reaches plan+design `ready` with no manual call. An accepted task reaches build+test `ready`.
2. A seeded 2σ breach in a fixture files exactly one NeedFix. A second sweep does not duplicate it. An insufficient population files nothing.
3. The attribution backfill on the real repository reports a measured count. Coverage (`evidence_covered / evidence_total`) is reported. A majority is the target, not a gate, because additive-only fixes are honestly unknown.
4. After the 0.12.5 release is recorded and confirmed, an accepted 0.12.5 card reaches deploy `ready`.

## Out of scope

- Model-run pass-rate evals in CI, because of CI cost.
- A nightly cron.
- Auto-launching fix cards on breach. The manager seat launches.
