# AIWorkHub launch copy by platform

Use the canonical article in `AIWORKHUB_LAUNCH_ARTICLE.md` for long-form
publication. Keep the GitHub repository as the canonical URL so duplicated
articles do not compete with one another in search.

## Dev.to / Hashnode

### Title

**Introducing AIWorkHub: a repository-native control plane for AI coding agents**

### Description

Plan, delegate and review multi-model software tasks with repository-local
state, bounded code context and evidence-based acceptance.

### Tags

`ai`, `opensource`, `vscode`, `productivity`

### Recommended note

Publish the full canonical article. On Dev.to, set `canonical_url` to the
GitHub repository or to one chosen permanent article URL. Do not set different
canonical URLs across mirrors.

## Medium

### Title

**AI coding agents need a control plane, not another chat window**

### Subtitle

How AIWorkHub turns each Git repository into an isolated, evidence-driven
workspace for multiple coding models.

### Opening hook

The difficult part of multi-agent software development is no longer asking a
model to write code. It is deciding which task is ready, limiting what the
worker may change, preserving project context, and proving that the result is
safe to accept.

Use the body of the canonical article from “The problem is coordination”
onward and add the repository link at both the beginning and end.

## LinkedIn

I have released **AIWorkHub**, an open-source, local-first control plane for
multi-model software development in VS Code.

Instead of treating coding agents as isolated chat windows, AIWorkHub gives
each Git repository its own:

• dependency-aware task queue  
• automatically refreshed code graph  
• Session Manager, AI Memory and knowledge base  
• isolated workers with explicit write scopes  
• evidence bundles and manager review  
• durable, repository-routed callbacks

The design principle is simple: models can reason and implement, but software
should own identity, routing, lifecycle state and acceptance evidence.

AIWorkHub is local-first, uses stdio MCP rather than an HTTP service, and works
with model routes already available through VS Code or authenticated CLIs. It
does not make a vague “token savings” claim; it records requested, delivered
and acknowledged context so efficiency can be measured honestly.

The current public channel is a GitHub Release VSIX, with Marketplace/Open VSX
publication still ahead.

Repository and demo: https://github.com/shrec/AIWorkHub

#OpenSource #VSCode #AIEngineering #DeveloperTools #MultiAgent

## Hacker News

### Title

**Show HN: AIWorkHub – a local-first control plane for multi-model coding agents**

### Submission text

I built AIWorkHub to coordinate coding models around repository-owned state
instead of separate chat histories. Each initialized repository gets its own
task DAG, structural code index, session/memory/KB stores, isolated worker
workspaces, evidence bundles and manager review inbox.

The runtime is an MCP server over stdio; there is no AIWorkHub HTTP service.
Writes and process launches have separate default-off gates. Editor routes use
models already authorized in VS Code, while CLI routes reuse their own login.

One thing I am deliberately trying to measure is context use: the system keeps
requested/delivered/acknowledged receipts and degraded reasons rather than
claiming a synthetic token-savings percentage.

It is MIT licensed and currently distributed as a GitHub Release VSIX. I would
especially value feedback on the repository-isolation model, review boundary
and how much of the optional Codex callback compatibility layer should remain.

https://github.com/shrec/AIWorkHub

## Reddit

### Suggested communities

- `r/vscode`: emphasize the editor dashboard and extension workflow.
- `r/LocalLLaMA`: emphasize model-agnostic orchestration and local state; do
  not imply that every route is a local model.
- `r/programming`: emphasize deterministic lifecycle and evidence, not model
  marketing.
- `r/opensource`: emphasize MIT licensing and contributor feedback.

### Title

**I built an open-source, repo-local control plane for coordinating AI coding agents**

### Body

I have been building **AIWorkHub**, a VS Code extension and MCP runtime that
turns each Git repository into an isolated workspace for multi-model software
development.

The task system supports dependencies, bounded write scopes, isolated workers,
terminal-state review, diffs/tests/artifacts, and callbacks to the verified
manager. A continuously refreshed code graph is used to provide bounded source
context, while Session Manager, AI Memory, KB and an optional manager-only
conversation graph have separate authority.

Everything durable is repository-local under `.aiworkhub/`. The MCP transport
is stdio, write/launch gates are default-off, and credentials are not copied
from VS Code or CLIs.

It is an early MIT-licensed release, currently installed from a GitHub Release
VSIX. I am looking for concrete feedback on setup, multi-repo isolation,
callback reliability and the evidence/review workflow—not just stars.

Repo: https://github.com/shrec/AIWorkHub

## X / Twitter thread

**1/8**  
AI coding agents are capable. Coordinating them is still improvised. I built
AIWorkHub: an open-source, local-first control plane that turns each Git repo
into an isolated multi-model engineering workspace. https://github.com/shrec/AIWorkHub

**2/8**  
Each repo gets its own task DAG, code index, sessions, memory, KB, callback
outbox and audit trail under `.aiworkhub/`. No shared project DB and no
AIWorkHub HTTP service.

**3/8**  
Tasks declare dependencies, acceptance criteria, allowed write paths,
forbidden actions, required outputs and validation. Workers operate in bounded
workspaces; finishing a process is not the same as accepting a change.

**4/8**  
Review is evidence-based: diff, tests, logs, artifacts, validation history and
tool-use receipts travel with the task. The manager accepts or returns a
precise residual for rework.

**5/8**  
The Source Graph provides bounded structural code context so agents do not
need to repeatedly scan the tree. AIWorkHub reports requested, delivered and
acknowledged context—not an invented “tokens saved” percentage.

**6/8**  
Codex, Claude, DeepSeek, GLM and VS Code Language Model routes can be used when
available on the machine. Editor/CLI credentials stay with their original
provider; AIWorkHub does not copy them into the repo.

**7/8**  
Safety: default-off write + launch gates, scoped workspaces, shell-free task
launches, redacted logs, durable callbacks and fail-closed repo/manager/task
identity checks.

**8/8**  
AIWorkHub is MIT licensed. The current public install is a GitHub Release VSIX;
Marketplace/Open VSX are next. Try it on a non-critical repo and tell me where
the workflow breaks: https://github.com/shrec/AIWorkHub

## Product Hunt

### Name

AIWorkHub

### Tagline

The local-first control plane for multi-model software development

### Short description

Turn each Git repository into an isolated AI engineering workspace with a task
DAG, structural code context, durable memory, bounded workers, evidence bundles
and manager review—all inside VS Code.

### First comment

I built AIWorkHub because adding more capable coding models did not solve the
coordination problem. Tasks, project context, worker authority and acceptance
evidence were still scattered across chats and scripts.

AIWorkHub keeps those concerns repository-local. It routes tasks to available
models, isolates their writes, preserves evidence and returns every terminal
outcome to a verified manager for review. The system is open source under MIT
and currently available as a GitHub Release VSIX.

I would love feedback from teams already using more than one coding agent,
especially around multi-repository isolation, callback behavior and what
evidence you require before accepting generated changes.

## Georgian Facebook / LinkedIn

გამოვაქვეყნე **AIWorkHub** — ღია კოდის, local-first ორკესტრატორი VS Code-ში
მულტი-მოდელური პროგრამული დეველოპმენტისთვის.

AIWorkHub თითოეულ Git რეპოზიტორს აძლევს საკუთარ Task DAG-ს, ავტომატურად
განახლებად კოდის გრაფს, Session Manager-ს, AI Memory-ს, Knowledge Base-ს,
იზოლირებულ worker-ებს და მტკიცებულებებზე დაფუძნებულ review პროცესს.

მთავარი პრინციპია: მოდელმა იაზროვნოს და დაწეროს კოდი, მაგრამ repository
identity, routing, task lifecycle, ნებართვები და საბოლოო acceptance
დეტერმინისტულმა სისტემამ მართოს.

სისტემა არ ითხოვს AIWorkHub cloud account-ს, მუშაობს MCP stdio transport-ით
და პროექტის მონაცემებს ინახავს უშუალოდ რეპოზიტორის `.aiworkhub/` სივრცეში.
Codex, Claude, DeepSeek, GLM და VS Code-ში ხელმისაწვდომი სხვა მოდელები
ერთიან workflow-ში შეიძლება გადანაწილდეს.

პროექტი MIT ლიცენზიითაა გახსნილი. ამ ეტაპზე ინსტალაცია GitHub Release-ის
VSIX-ით ხდება; Marketplace/Open VSX შემდეგი ნაბიჯია.

რეპოზიტორი და დემო: https://github.com/shrec/AIWorkHub

## Reusable calls to action

Choose one per platform; do not stack all of them.

- **Try it:** Install the latest VSIX on a non-critical repository and report
  the first point of friction.
- **Review it:** Read the security and callback design and challenge the trust
  boundaries.
- **Contribute:** Pick one open issue that affects a platform or provider you
  use.
- **Discuss:** What evidence do you require before accepting an AI-generated
  change?

## SEO phrases

Use naturally; do not repeat them mechanically:

- multi-model AI coding agent orchestration
- VS Code AI agent task manager
- local-first MCP server for coding agents
- repository-scoped AI memory and code graph
- evidence-based AI code review
- open-source coding agent control plane

