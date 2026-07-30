# AIWorkHub Brand Guide

AIWorkHub is the repository-native control plane for multi-model software
development.

This guide keeps the product recognizable across GitHub, VS Code, release
notes, articles, screenshots and community material.

## Name and short name

- Product name: **AIWorkHub**
- Short name: **AWH**
- Repository and package name: `AIWorkHub` / `aiworkhub`
- Do not use: AI Working Hub, AIWorkingHub, AI Work Hub or GeoAI Task MCP

Use the full product name on first mention. Use AWH only after AIWorkHub has
already been introduced.

## Positioning

**Category:** repository-native AI engineering control plane

**Primary tagline:**

> Plan. Delegate. Verify. Remember.

**One-sentence description:**

> AIWorkHub gives every repository its own plan, source intelligence, durable
> context, model workforce and evidence-first review loop.

**Long description:**

> AIWorkHub is a local-first orchestration layer for AI-assisted software
> development in VS Code. It binds task planning, a Source Graph, sessions,
> memory, knowledge, isolated model workers and review evidence to the exact
> repository that owns them.

## Product promises

Every public description should reinforce these four promises:

1. **Repository-native** — authority and durable state belong to the repo.
2. **Evidence-first** — work is accepted from proofs, not optimistic status.
3. **Multi-model** — the system coordinates available models instead of
   locking the user into a single provider.
4. **Local-first** — AIWorkHub itself requires no hosted service or network
   listener.

Avoid claiming fully autonomous development, guaranteed token savings, perfect
model quality or support that has not passed the release qualification matrix.

## Visual identity

| Token | Value | Use |
| --- | --- | --- |
| Midnight | `#07111F` | Primary dark background |
| Deep navy | `#0B1D31` | Panels and gradients |
| Sky | `#38BDF8` | Source intelligence and active state |
| Teal | `#2DD4BF` | Verified state and durable context |
| Ice | `#A5F3FC` | Supporting text and highlights |
| Slate | `#CBD5E1` | Body text on dark surfaces |

The graph-node mark is the canonical logo. Preserve its aspect ratio and clear
space. Do not place it inside a second unrelated badge, recolor individual
nodes randomly or use the old hexagonal `G` mark.

Canonical assets:

- Activity icon: `vscode-extension/media/aiworkhub-activity.svg`
- Marketplace icon: `vscode-extension/media/aiworkhub-icon.png`
- Repository hero: `docs/assets/aiworkhub-hero.svg`
- Marketplace hero: `vscode-extension/media/aiworkhub-hero.svg`

## Voice

AIWorkHub speaks plainly and operationally:

- lead with the user outcome;
- state authority and safety boundaries precisely;
- distinguish shipped, qualified, experimental and planned behavior;
- prefer concrete evidence over superlatives;
- describe models as a workforce, not as magical autonomous employees.

## Public content checklist

Before publishing an article, screenshot, release or Marketplace update:

- use the canonical name, tagline, logo and colors;
- show the current version or avoid embedding a version in durable prose;
- link to `README.md`, `SECURITY.md` and `docs/GETTING_STARTED.md`;
- redact repository paths, prompts, source, credentials and private memories;
- label experimental provider/authentication paths truthfully;
- use product screenshots from a clean demonstration repository;
- verify the statement against the current release, not a development branch.
