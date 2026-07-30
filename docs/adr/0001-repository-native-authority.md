# ADR 0001: Repository-native authority and local stdio transport

- Status: Accepted
- Date: 2026-07-30

## Context

AI coding tools commonly keep task state, memory and worker routing in a global
process or hosted service. That makes multi-repository use prone to ambiguous
identity, stale callbacks and accidental cross-project context reuse. It also
makes a repository difficult to move between local, Windows, WSL and
Remote-SSH environments without re-creating hidden state.

## Decision

AIWorkHub binds canonical operational authority to a verified repository
identity. Each initialized repository owns a `.aiworkhub/` state directory
containing its task store, Source Graph, Session Manager, AI Memory, KB,
routing configuration, audit evidence and bounded runtime data.

The VS Code extension launches a repository-scoped MCP child over stdio on the
workspace host. AIWorkHub does not use a browser dashboard, HTTP server, static
port or shared central database. Multi-repository handoff selects a verified
repository ID, never an arbitrary model-supplied path, and changes the context
authorities as one coordinated operation.

## Consequences

- Repositories remain portable and independently inspectable.
- Local, native Windows and Remote-SSH deployments use the same authority
  model even when their process and IPC implementations differ.
- A model cannot silently combine tasks, source indexes, memories or callbacks
  from different repositories.
- Explicit initialization and capability gates add ceremony, but make durable
  state creation and write authority observable.
- Global installation/runtime caches may optimize startup, but can never
  become the canonical project-data authority.

## Validation

Release qualification must prove clean initialization, disjoint databases and
context stores, repository switching, callback isolation, reload recovery and
packaged VSIX operation across the supported platform matrix. An ambiguous or
foreign repository identity fails closed and surfaces a bounded reason.
