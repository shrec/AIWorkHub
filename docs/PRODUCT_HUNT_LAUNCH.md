# AIWorkHub Product Hunt Launch Pack

This is the launch-day source of truth. Product claims must remain consistent
with the repository, Marketplace package and checked-in benchmark evidence.

## Submission fields

**Name**

AIWorkHub

**Tagline**

The control plane for multi-model coding agents

**Description (under Product Hunt's 260-character limit)**

Coordinate Codex, Claude, Copilot, DeepSeek and GLM on real repositories with
task DAGs, durable context, Source Graph intelligence, isolated workers,
callbacks and evidence-based review—all local-first in VS Code.

**Primary URL**

https://shrec.github.io/AIWorkHub/

**Additional links**

- Marketplace: https://marketplace.visualstudio.com/items?itemName=IvaneChkheidze.aiworkhub
- GitHub: https://github.com/shrec/AIWorkHub
- Documentation: https://shrec.github.io/AIWorkHub/docs/
- Transparent benchmarks: https://shrec.github.io/AIWorkHub/benchmarks/

**Pricing**

Free (open source, MIT licensed)

**Suggested topics**

Developer Tools, Artificial Intelligence, Open Source

Keep the topic list narrow. Do not add unrelated categories for reach.

## Maker story draft

Product Hunt asks makers to open the conversation with a first comment. The
maker should rewrite this in their own voice before posting; it is a factual
outline, not a comment to paste blindly.

> I built AIWorkHub because using several coding models on long-running,
> real repositories created a coordination problem that better prompts did not
> solve. Decisions disappeared after context compaction, agents repeatedly
> scanned the same code, parallel changes collided, and a model saying “done”
> was not proof that the work was safe to accept.
>
> AIWorkHub gives each repository a local control plane: dependency-aware task
> planning, model routing, isolated workers, structural Source Graph context,
> durable sessions/memory/knowledge, callbacks and an evidence-first review
> boundary. Workers can propose changes, but the current manager verifies the
> diff, tests, logs and receipts before promotion.
>
> It is open source and runs as a VS Code workspace extension on Windows,
> macOS, Linux and Remote SSH. It uses models already authenticated in the
> editor or supported CLI rather than requiring an AIWorkHub cloud account.
>
> I am launching early because I want feedback from people who already feel
> the pain of managing coding agents beyond one-off prompts. I am especially
> interested in where setup is unclear, which provider routes are unreliable,
> and which workflow evidence you need before trusting an agent's result.

Do not add a universal token-savings multiplier. The current n=2 semantic-edit
pilot is public and machine checked, but explicitly fails the product-claim
gate because it is small, non-randomized and cache-confounded.

## Media

Product Hunt's current guidance recommends a square 240x240 thumbnail and
1270x760 gallery images; at least two gallery images are required for the
gallery to appear.

**Thumbnail**

`docs/assets/product-hunt/thumbnail.png` (240x240; generated from the canonical
AIWorkHub marketplace icon without stretching or substituting a letter icon).

**Gallery order**

1. `docs/assets/product-hunt/01-control-plane.png`
2. `docs/assets/product-hunt/02-engineering-loop.png`
3. Add `docs/assets/product-hunt/03-dashboard.png` only after regenerating it
   from a clean current-version dashboard capture.
4. Optional: a short product loop uploaded through a supported interactive
   demo provider or a public, non-private full YouTube URL.

All three prepared gallery images are exactly 1270x760, but images 1 and 2 are
the launch-safe minimum. Do not upload image 3 in its current form: its source
capture visibly shows an older version number. Replace that screenshot with a
clean current-version capture and regenerate image 3 first. Blur repository
identity, thread IDs, host paths, prompts, credentials, memory content and
proprietary task names.

## Launch-day checklist

- [ ] Use a personal Product Hunt account; company accounts cannot post.
- [ ] Complete account onboarding and verify the maker profile/name/avatar.
- [ ] Confirm the scheduled date and the 12:01 AM Pacific launch boundary.
- [ ] Make the scheduled preview public only if intentionally sharing it.
- [ ] Verify the landing page, Marketplace, GitHub and docs links in a private
      browser window.
- [ ] Upload the canonical logo thumbnail and at least two 1270x760 images.
- [ ] Replace the stale dashboard screenshot with a clean current build, or
      launch with gallery images 1 and 2 only.
- [ ] Confirm any YouTube video is public and uses its full URL.
- [ ] Add the maker by exact Product Hunt username.
- [ ] Select Free pricing and only the strongest relevant topics.
- [ ] Rewrite the maker-story outline in the maker's own voice.
- [ ] Prepare concise, factual answers for setup, privacy, supported models,
      platform support and benchmark methodology.
- [ ] Do not ask for or coordinate artificial upvotes; share the direct launch
      link and ask for genuine feedback.
- [ ] Monitor Marketplace install, GitHub issue and landing-page availability.

Official references checked on 2026-08-04:

- [How to post a product](https://help.producthunt.com/en/articles/479557-how-to-post-a-product)
- [How to schedule a post](https://help.producthunt.com/en/articles/2724119-how-to-schedule-a-post)
- [How to share a scheduled launch](https://help.producthunt.com/en/articles/15706445-how-to-share-a-scheduled-launch)

## Prepared answers

**Does AIWorkHub replace Codex, Claude or Copilot?**

No. It coordinates available coding models and keeps repository authority,
context and review evidence around their work.

**Does source code go to an AIWorkHub cloud service?**

No AIWorkHub cloud account or telemetry service is required. Repository state,
task data, context stores and the MCP runtime remain on the workspace host.
The selected model provider still receives whatever context its own adapter is
asked to process under that provider's terms.

**Why not just use several terminal tabs?**

Tabs do not provide a shared task DAG, write-scope collision checks, isolated
candidate workspaces, durable callbacks, context authorities or a manager-only
accept/reject boundary.

**Which platforms are supported?**

Linux, macOS, native Windows, WSL and Remote SSH are release-qualified. Exact
model availability depends on the user's installed/authenticated editor or CLI
providers and is shown by Preflight rather than assumed.

**How much does it save?**

AIWorkHub does not currently publish a universal savings multiplier. Operations
records structural bytes, file reads, provider tokens, cost availability and
outcomes separately. A preliminary n=2 focused-edit pilot observed lower total
tokens and elapsed time, but it is not randomized and its cache mix differs;
the raw ledger and contrary result are public.
