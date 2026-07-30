# ADR 0002: Canonical version and release artifact integrity

- Status: Accepted
- Date: 2026-07-30

## Context

AIWorkHub ships one Python package and one VS Code extension containing an
embedded copy of that Python runtime. A manually repeated version in Python
metadata, the extension manifest, its lockfile and runtime compatibility code
can drift, producing a VSIX that installs successfully but rejects its own MCP
child or a release tag that does not match its artifacts.

## Decision

`src/aiworkhub/_version.py` is the sole canonical release-version authority.
Python package metadata resolves it through the build backend. The extension
manifest, lockfile and runtime compatibility constant are deterministic
projections maintained by `scripts/release_metadata.py`.

CI checks every projection and the release tag against the canonical value.
The canonical release build packages the VSIX twice and requires byte identity.
GitHub Releases publish one sorted `SHA256SUMS` file covering the wheel, source
distribution and VSIX.

## Consequences

- Version changes require one intentional source edit and one explicit sync.
- Derived metadata remains readable by standard Python, npm and VS Code tools.
- Projection drift fails before packaging or publication.
- Checksums prove downloaded artifact identity; they do not replace signing or
  registry provenance and must not be described as either.

## Validation

Release qualification runs the metadata check on every supported platform,
builds the Python distribution from dynamic metadata, verifies repeated VSIX
byte identity and attaches checksums computed from the final downloaded release
artifacts.
