# External Difficult-Repository Qualification Protocol (RM-2026-00016)

Contract owner: `src/aiworkhub/external_qualification.py`
Tests: `tests/test_external_qualification.py`

## Phase boundary

**This phase is foundation only.** It defines, validates and canonically
serializes the corpus manifest and the qualification run artifact. It does
**not** clone, fetch, build, mutate or execute any external repository, and **no
performance, token or cost claim is established by it.** Any number produced
here describes an artifact, never a measured result on an external codebase.

The execution phase is a separate, later authority. Until it runs and produces
receipt-backed artifacts, the correct summary of AIWorkHub's behaviour on
difficult external repositories is `UNKNOWN`.

## The corpus manifest

A manifest is versioned by `corpus_version` and sealed with
`corpus_manifest_id`, the canonical digest of its normalized body.

It is invalid unless all four profiles are covered by at least one entry:

| Profile | Must demonstrate |
| --- | --- |
| `cpp_large_expensive` | Large, expensive C++ build; translation units that defeat naive whole-file context; toolchain configuration that must be discovered. |
| `ts_js_monorepo` | Many packages behind one workspace root; cross-package type resolution and build ordering; lint/typecheck gates distinct from tests. |
| `python_backend_or_data` | Runtime dependencies beyond the standard library; database, scheduler or pipeline state in the test path; a suite long enough that selection matters. |
| `submodules_or_lfs_strict_ci` | Submodules or Git LFS required before a build starts; CI failing closed on formatting, licensing or generated-file drift; checkout cost that makes clone-per-attempt untenable. |

### The fixed corpus

The corpus is checked in and pinned: **the shipped corpus state is `PINNED`**.
`fixed_corpus_manifest()` returns it, and it is validated against
`validate_corpus_manifest` at import, so an unpinned or malformed corpus is an
import error rather than a surprise partway through a qualification run.

| `corpus_id` | Profile | Repository | Pinned at |
| --- | --- | --- | --- |
| `cpp-llvm-project` | `cpp_large_expensive` | `llvm/llvm-project` | `refs/tags/llvmorg-19.1.0` |
| `ts-vscode` | `ts_js_monorepo` | `microsoft/vscode` | `refs/tags/1.95.0` |
| `py-airflow` | `python_backend_or_data` | `apache/airflow` | `refs/tags/2.10.0` |
| `sub-grpc` | `submodules_or_lfs_strict_ci` | `grpc/grpc` | `refs/tags/v1.66.0` |

Each revision is a full commit object id observed by reading that repository's
own remote ref advertisement for the release tag above. Read-only ref metadata
is not a clone, a build or an execution, so the corpus is materialized without
crossing the phase boundary.

### Pin evidence

A revision nobody observed is a fabricated pin, so every entry carries
`pin_evidence` recording the observation:

- `observation_method` — how the ref was read; currently `git_ls_remote` only.
- `observed_ref` — the immutable `refs/tags/…` ref. `HEAD`, `refs/heads/…` and
  bare branch names are rejected.
- `observed_ref_object_id` — what that ref advertised. For an annotated tag this
  is the tag object and `repository_revision` is the commit it peels to; for a
  lightweight tag the two coincide. Keeping both makes the observation
  reproducible by anyone reading the same ref. It is validated as an immutable
  object id in its own right: a branch name or a short sha is not a pin.
- `observed_on` — the ISO date the ref was read. The commit ids are immutable,
  so this records when, not a freshness requirement.
- `locator` — a public `https://` URL that resolves the pinned commit. It must
  point into the entry's own `repository_url` and end with the pinned revision,
  so a locator cannot silently reference a different commit or repository.
  Containment is decided on repository *identity*, not raw bytes: a trailing
  slash, a `.git` suffix and host case are cosmetic here exactly as they are for
  duplicate detection, so a `…/llvm-project.git` pin still owns its canonical
  `…/llvm-project/commit/<oid>` locator. The `/` path-segment boundary is still
  required, so a same-prefix sibling such as `…/llvm-project-mirror` is not
  contained.

#### Object id normalization

A git object id is case-insensitive, so every field that carries one follows the
same order: **normalize, validate, compare, seal**. The value is folded to
lowercase first, matched against the full-length object id rule second, compared
against the other folded operands third, and sealed in its canonical lowercase
spelling last. That covers `repository_revision`, `pin_evidence.observed_ref_object_id`,
the revision the `locator` ends with, and the run artifact's `repository_revision`
and `aiworkhub_revision`. Folding only some of them would reject an entry against
its own matching evidence, and would let one commit produce two different
`corpus_manifest_id` values.

The accepted-outcome receipt is the deliberate exception. Its `base_oid` is a
preimage of a digest the canonical acceptance authority already sealed, and its
`receipt_id`, `repository_revision` and `attempt_artifact_manifest_id` are
recomputed rather than re-spelled. Normalizing any of them here would produce an
identity the canonical builder never emitted, so those are compared byte-exact.

`corpus_specification()` is the criteria view of the same data: it reads every
URL, revision and pin back out of the manifest, so the two cannot drift apart.
It carries its own `schema_id` and is deliberately not accepted by
`validate_corpus_manifest`, which validates manifests only.

### Entry rules

Each entry declares `corpus_id`, `profile`, `repository_url`,
`repository_revision`, `revision_kind`, `rationale` and `pin_evidence`.

- `repository_url` must be a public, credential-free `https://` URL. `http://`,
  `file://`, `ssh://`, scp-style `git@host:path` and any URL carrying userinfo
  are rejected.
- `repository_revision` must be a full-length git commit object id (40 or 64 hex
  characters). Branches, default branches, tags, `refs/…`, `origin/…` and
  abbreviated shas are rejected as floating.
- `corpus_id` must be unique, and so must the repository identity: two spellings
  of one repository (trailing slash, `.git` suffix, case) collide and are
  rejected as a duplicate corpus identity. The two are normalized in that order
  — case first, then the suffix — so `.GIT` collides exactly as `.git` does.

## The run artifact

One schema records every outcome. A rejected or failed run is the *same* shape
as an accepted one, so a negative result is a first-class, replayable record
rather than a gap.

Recorded fields: corpus identity (`corpus_manifest_id`, `corpus_version`,
`corpus_id`, `profile`), `repository_url` and `repository_revision`,
`aiworkhub_revision`, `route` and `model`, `task_id`, `request_id` and
`attempt_id`, `retries`, `validation`, `review`, `usage`, `platform_claims` and
`platform_evidence`, `outcome` and `outcome_reason`,
`accepted_outcome_receipt`, `receipt_authentication`,
`canonical_authority_state`, `canonical_authority_reason`, and the derived
`success`. The artifact is sealed with `run_artifact_id`.

`task_id` and `request_id` are both required because the canonical acceptance
receipt binds that pair; recording only one would leave half the binding
uncheckable.

When a manifest is supplied, the artifact's profile, URL and revision must match
its corpus entry, so a run cannot silently drift off the pinned revision.

## Acceptance authority

There is exactly one acceptance authority in this repository, and it does not
live here: `task_engine._validate_accepted_outcome_receipt`. This module reuses
it rather than defining a second, weaker one — and it *requires* it.

An artifact's `accepted_outcome_receipt` must be the canonical
`aiworkhub.accepted_outcome_receipt.v1` identity — exactly the fields
`schema_id`, `receipt_id`, `task_id`, `request_id`, `claim_epoch`, `base_oid`,
`promoted_paths`, `changed_path_hashes`, `attempt_artifact_manifest_id` and
`repository_revision`, no more and no fewer. A missing field, an invented one, a
foreign `schema_id`, and a `repository_revision` or `receipt_id` that the
receipt's own contents do not reproduce are each rejected.

The admission rule is the canonical authority's, and nothing stricter. An
accepted outcome that promoted no repository bytes — readonly research, a
quality review — carries `promoted_paths: []` with `changed_path_hashes: {}`,
and that receipt is admitted here exactly as the canonical authority admits it.
A narrower rule would be a second acceptance authority refusing receipts the
only one granted.

That identity check is necessary and **not sufficient**. It proves a receipt is
well-formed, never that anyone authenticated it.

`canonical_acceptance_authority(repo, card, task_id=…, request_id=…)` binds the
canonical validator to a repository and card. Passing it to
`validate_run_artifact` re-authenticates the receipt against the sealed terminal
evidence and the canonical on-disk hashes.

Without that bound authority, `validate_run_artifact` **fails closed**. The row
is still validated and recorded in full — an acceptance the authority could not
be asked about is evidence, not a gap — but it records
`canonical_authority_state: UNKNOWN` and `success: false`. No
`receipt_authentication` block a caller writes can change that: a
hand-assembled `{"state": "pass", …}`, however well-formed its locator and
digest, is a *claim about* authentication and never authentication itself.
Declaring `success: true` or `canonical_authority_state: "pass"` without the
authority is rejected outright rather than believed.

`success` is true only when all of the following hold:

- `outcome == "accepted"`;
- the receipt is the canonical identity and binds this `task_id` and
  `request_id`;
- the bound canonical authority itself authenticated that receipt, so
  `canonical_authority_state` is `pass`;
- `receipt_authentication.state` is `pass`, carrying a retrievable reference;
- `validation` and `review` both pass.

Because `canonical_authority_state` is part of the sealed body, an authenticated
artifact's `run_artifact_id` is reproducible only under the same authority.
Replaying one without it is rejected, never silently downgraded to a pass.
`canonical_authority_reason` is sealed beside it and records *why* the authority
stayed silent, so a replay cannot keep the `UNKNOWN` while relabelling its cause.

A receipt nobody has authenticated is a recorded claim, not an accepted outcome:
the row keeps `outcome: accepted` and reports `success: false`.

### One authority, one attempt

A bound authority speaks for exactly the `(task_id, request_id)` it was bound
to. Given a row from another attempt it is **not consulted at all**: the row
fails closed exactly as it would with no authority, with reason
`acceptance_authority_bound_to_another_attempt`. Its refusal there would be an
answer about the question rather than a verdict on the row, and treating it as
an error would let one foreign row abort every batch containing it — a summary
spanning several attempts can only hold one attempt's authority. Within its own
scope the authority's refusal *is* about the row, and that is still raised.

`acceptance_authority_binding(authority)` reports the declared scope, and
`bind_acceptance_authority` declares one. An authority that declares no scope is
consulted about every row: an undeclared binding must never excuse a row from
authentication.

These are progress signals and are **never** projected as success:

- a process launched, or a launch that succeeded
- a worker that completed, or exited zero
- a validation command that passed, or tests that were invoked
- a review that became ready or was submitted

Using any of them as `outcome_reason` on an accepted outcome is rejected. A
non-accepted outcome may carry neither a receipt nor receipt authentication.

## UNKNOWN discipline

Missing evidence stays `UNKNOWN`. It is never a zero and never an inferred
average.

- Token counts and `cost_usd` may each be `UNKNOWN`; `cost_known` and
  `tokens_known` are derived from the evidence and a declared flag that
  contradicts them is rejected.
- In `qualification_summary`, a profile with fewer than
  `minimum_samples_per_profile` observed runs reports `state: UNKNOWN` with
  reason `insufficient_samples` (or `no_observed_run`) and an `UNKNOWN`
  acceptance rate.
- A profile aggregated **without** a bound acceptance authority also stays
  `state: UNKNOWN`, with reason `acceptance_authority_not_consulted` and an
  `UNKNOWN` acceptance rate. Every row fails closed to `success: false` there,
  so a rate would report a question nobody asked as a measured `0.0`.
- One attempt with unknown cost makes that profile's observed cost `UNKNOWN`;
  the same holds for tokens. A partial population never reports a total.
- A profile with no observed run reports `total_retries: UNKNOWN`, not `0`. An
  empty sum is zero only in arithmetic; published beside an `UNKNOWN` cost and
  `UNKNOWN` tokens, a `0` reads as a profile that ran and happened never to
  retry. `total_retries_reason` records which of the two it is.
- A profile holding an accepted row the bound authority could not speak for
  also stays `state: UNKNOWN`, with reason
  `acceptance_authority_did_not_cover_every_accepted_row` and an `UNKNOWN`
  acceptance rate — a rate over a half-asked population would report the
  unasked rows as refusals nobody issued.

Each profile reports three different facts about acceptance, and never mixes
them. `recorded_accepted_outcomes`, `recorded_rejected_outcomes` and
`recorded_failed_outcomes` are what the rows recorded: `outcome` is a closed
three-value set, so those three partition `observed_runs` exactly, and a
violation is raised rather than published. `authenticated_accepted_outcomes` is
the strictly narrower count the canonical authority granted, and it is the only
input to `acceptance_rate`. `unauthenticated_accepted_outcomes` is the
remainder: acceptances recorded but never authenticated, whether because no
authority was consulted or because the one that was belongs to another attempt.

## Cross-platform evidence

A Linux development host cannot witness a Windows or macOS result. Claiming
either platform therefore requires its own evidence reference under
`platform_evidence`.

Every evidence reference is a retrievable locator plus its own content digest:

- `locator` uses `https://`, `ci://` or `artifact://`, names an authority holding
  at least one alphanumeric character, and carries no userinfo.
- `sha256` is 64 lowercase hex characters.
- `media_type` is required; `byte_count` may be `UNKNOWN`.

Owner-local absolute paths are not locators. `/home/…`, `/Users/…`, `~/…`,
`~user/…`, `C:\…`, UNC `\\host\…` and `file://` are rejected wherever they
appear, in any
field and at any offset — not only where a field begins. A path embedded in
free text after punctuation is the same path: `outcome_reason` values such as
`macos evidence:/Users/owner/report.json` or `rejected,C:\builds\report.json`
are rejected exactly like the standalone spelling. Root segment names are
matched case-insensitively, because Windows and macOS filesystems are
case-insensitive by default: `/users/…`, `/Home/…` and `/TMP/…` name the same
owner-local directories as their canonical spellings and are rejected the same
way, including behind a retrievable scheme. Punctuation in front of a path is
not an exemption either: `rejected-/home/owner/report.json`,
`rejected./Users/owner/report.json` and `logs+/var/log/report.json` are rejected
exactly like the comma-adjacent spelling.

The rule stays targeted at filesystem shapes, so an ordinary URL path segment
(`https://ci.example.com/var/log/run-1`) is not one, and a per-user directory
inside a URL path (`https://ci.example.com/~runner/windows-report.json`) names a
CI user directory that stays a retrievable locator. That exemption is decided by
the URL itself rather than by the character in front of the path: a scheme, a
non-empty authority holding an alphanumeric, and the path that follows are
matched as one span, and only a filesystem shape found outside such a span is a
violation. The character to the left cannot decide it, because a URL path
segment and free-text prose end in the same characters — only the `://` further
left tells them apart. So `~shrek/report.json`, `checkout:~/ci/report.json` and
the authority-less `artifact:///home/…` carry no URL authority in front of them
and remain rejected as owner-local paths.

That span ends where the URL ends, not where the line does. Whitespace is not
the only thing that ends a URL, so the span stops at every character that cannot
carry a path forward: the ones a URI may not contain at all (whitespace, `\`,
`"`, `<`, `>`, `^`, a backquote, `{`, `|`, `}`), the `?` and `#` that open a
query or fragment, and the prose separators `,`, `;`, `'` and the bracket pairs.
Otherwise a single whitespace-free token such as
`https://ci.example.com/run/1,/home/owner/report.json` would ride out entirely
inside the exemption. Stopping early never exempts more: the remainder is read
back as ordinary free text, so the owner-local rules above apply to it in full,
while the punctuation that genuinely continues a path (`.`, `-`, `_`, `~`, `%`,
`+`) keeps `https://ci.example.com/run-1-/var/log/report.json` a single URL.

The macOS roots are named too: `/Volumes/<disk>/…` is a mounted external or
network disk and `/Library/…` is per-machine application state. Neither is
reachable from another host, and a macOS evidence reference is precisely the
field that would otherwise carry one, so both are rejected on the same terms —
in free text, behind a retrievable scheme, and in any casing.

A retrievable scheme in front of a local path does not make the path
retrievable, so the locator's own authority and path are validated as well:
`ci://C:/Users/…`, `ci://C:\…`, `artifact://~/…` and the authority-less
`artifact:///home/…` form are each rejected. Punctuation is not an authority
either. `.`, `-` and `_` are non-empty, so `artifact://./Users/…`,
`ci://-/home/…` and `artifact://_/var/…` would otherwise satisfy the non-empty
check while moving the path off the start of the scheme-relative remainder — one
character laundering the very paths above. The owner-local check is therefore
re-anchored on the path following any authority that holds no alphanumeric
character, and such an authority is rejected as a host even when its path is
harmless. A hostless CI authority such as `ci://run/1/windows/report.json` is
unaffected — the rule targets filesystem path shapes and punctuation standing in
for an absent authority, not the absence of a DNS name.

## Determinism

`canonical_json` serializes with sorted keys, `":"`/`","` separators, no
NaN/Infinity, and no ASCII escaping. `canonical_digest` is the `sha256:` digest
of those bytes.

Validation normalizes before sealing: entries sort by `corpus_id`, evidence
sorts by locator then digest, platform claims sort. Input mapping order and
entry order therefore cannot change a digest, and a declared
`corpus_manifest_id`, `run_artifact_id` or `success` that contradicts the
canonical value is rejected rather than trusted.

Artifacts round-trip: `canonical_json` → `json.loads` → validate yields an
identical digest.

## Rejected content

Beyond floating revisions and local paths, validation rejects credential-shaped
material anywhere in an artifact — GitHub, Slack, AWS and OpenAI-style token
prefixes, PEM private key headers, `Authorization:`/`password=`-style
assignments — and any mapping key named for a credential. Control characters
and non-finite numbers are rejected because they are not canonically
serializable.

## What the next phase adds

Real clone, build and execution on the corpus pinned here; receipt-backed
accepted outcomes authenticated by a bound acceptance authority; Windows and
macOS evidence produced by CI runners. Only then does a performance claim become
possible, and only for the population actually measured.
