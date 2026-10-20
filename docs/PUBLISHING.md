# AIWorkHub Publishing Guide

## Overview

The release workflow builds and attaches three artifacts to a GitHub Release:

1. A Python wheel in `dist/`.
2. A Python source distribution in `dist/`.
3. A VS Code extension in `vscode-extension/dist/aiworkhub-*.vsix`.

The extension can also be published to the VS Code Marketplace and Open VSX
Registry when the corresponding repository secrets are configured. PyPI uses
GitHub OIDC Trusted Publishing and requires no long-lived upload token.

## Current public channels

GitHub Releases and the VS Code Marketplace are public channels. The MCP
Registry listing (`io.github.shrec/aiworkhub`) goes live only after a tag
release runs with both `PYPI_PUBLISH_ENABLED` and
`MCP_REGISTRY_PUBLISH_ENABLED` enabled; confirm it with the listing check
below. Each GitHub release provides a VSIX, Python wheel, source distribution
and checksums. The tag workflow publishes to a registry only when that
registry's explicit repository variable is enabled and its credential is
present. A skipped registry job is not evidence that a package was published
there; verify the registry's public version after every release.

## Release Preflight

Run from the standalone repository root:

```bash
python -m pytest tests/ -v --tb=short -x
python -m build --outdir dist
npm --prefix vscode-extension install
npm --prefix vscode-extension test
npm --prefix vscode-extension run package
```

The canonical version lives only in `src/aiworkhub/_version.py`. After changing
that literal, synchronize the generated extension projections and verify them:

```bash
python scripts/release_metadata.py sync
python scripts/release_metadata.py check --tag v<x.y.z>
```

Python package metadata reads the canonical value dynamically. Extension
`package.json`, `package-lock.json`, and the packaged-runtime compatibility
constant are generated projections; CI fails on any drift.

Public-release presentation must also pass this checklist:

- canonical name, tagline and logo follow [the Brand Guide](BRAND.md);
- the repository and Marketplace READMEs describe the current shipped product;
- `CHANGELOG.md` has a dated entry for the release;
- `SECURITY.md`, `SUPPORT.md` and community templates contain no legacy paths;
- screenshots come from a clean demonstration repository and expose no source,
  prompts, credentials, host paths or memory content;
- GitHub description and topics match the current product position;
- generated wheel, sdist and VSIX checksums are attached to the release.

## Tag-Driven Release

Push a version tag to trigger the release workflow:

```bash
VERSION=1.2.3  # replace with the synchronized package version
git tag "v${VERSION}"
git push origin "v${VERSION}"
```

The `release.yml` workflow will:

1. Check out the selected tag and verify both package versions.
2. Run `python -m pytest tests/` and build the wheel and source distribution.
3. Run `npm test` and `npm run package` in `vscode-extension/`.
4. Upload the Python distributions and
   `vscode-extension/dist/aiworkhub-*.vsix` as workflow artifacts.
5. Create a GitHub Release with all three artifacts attached.
   The published release also includes `SHA256SUMS` for the wheel, source
   distribution and VSIX; the canonical build verifies byte-identical repeated
   VSIX packaging before upload.
6. If `OVSX_PUBLISH_ENABLED=true`, require `OVSX_TOKEN` and publish the VSIX to
   Open VSX Registry; fail clearly when the enabled credential is missing.
7. If `MARKETPLACE_PUBLISH_ENABLED=true`, require `MARKETPLACE_TOKEN` and
   publish the VSIX to the VS Code Marketplace; fail clearly when the enabled
   credential is missing.
8. If repository variable `PYPI_PUBLISH_ENABLED=true`, publish the wheel and
   source distribution through the `pypi` GitHub environment and PyPI Trusted
   Publisher identity.
9. If repository variable `MCP_REGISTRY_PUBLISH_ENABLED=true`, publish
   `server.json` to the MCP Registry after the PyPI job succeeds, using the
   job's GitHub OIDC identity; see the MCP Registry section below.

All registries are optional. A disabled registry job is visibly skipped. An
enabled registry with a missing credential or failed upload fails instead of
reporting a false-green no-op. PyPI publication is skipped unless its
repository variable is enabled. Registry publication does not gate GitHub
Release creation.

### Required Secrets (optional)

| Secret | Purpose |
|--------|---------|
| `OVSX_TOKEN` | Open VSX Registry publish |
| `MARKETPLACE_TOKEN` | VS Code Marketplace publish (VS Publisher) |

### Required repository variables (optional registry switches)

| Variable | Purpose |
|---|---|
| `OVSX_PUBLISH_ENABLED=true` | Enable the Open VSX job and require `OVSX_TOKEN` |
| `MARKETPLACE_PUBLISH_ENABLED=true` | Enable the Marketplace job and require `MARKETPLACE_TOKEN` |
| `MCP_REGISTRY_PUBLISH_ENABLED=true` | Enable the MCP Registry job; it also needs `PYPI_PUBLISH_ENABLED=true` |

### PyPI trusted publisher setup

In the PyPI `aiworkhub` project, add a GitHub Trusted Publisher for repository
`shrec/AIWorkHub`, workflow `release.yml`, environment `pypi`. In GitHub, create
the protected `pypi` environment and set repository variable
`PYPI_PUBLISH_ENABLED=true`. The release job then receives a short-lived OIDC
identity; no PyPI token is stored in GitHub.

## MCP Registry

The root `server.json` is the MCP Registry manifest for the `aiworkhub` PyPI
package, listed as `io.github.shrec/aiworkhub`. It declares a `uvx` stdio
package and the environment variables the server reads: the required
`AIWORKHUB_REPO_ROOT`, and the `AIWORKHUB_ALLOW_WRITES` and
`AIWORKHUB_ALLOW_LAUNCH` gates, which both default to `0` (closed). Its
top-level `version` and `packages[0].version` are generated projections of the
canonical version: `python scripts/release_metadata.py sync` rewrites both, and
`python scripts/release_metadata.py check` fails on any drift, including in the
`build` job before anything is published.

The registry verifies PyPI package ownership by looking for a marker in the
package's PyPI description. The root `README.md` is that description, so it
must keep this line:

```text
<!-- mcp-name: io.github.shrec/aiworkhub -->
```

The `publish-mcp-registry` job in `release.yml` publishes the manifest from the
release tag:

- It runs only when repository variable `MCP_REGISTRY_PUBLISH_ENABLED=true`.
- It needs the `publish-pypi` job, so `PYPI_PUBLISH_ENABLED=true` and a
  published PyPI release of the same version are prerequisites. When PyPI
  publication is skipped or fails, this job fails with an explicit error
  instead of being skipped.
- It authenticates with the job's GitHub OIDC identity (`id-token: write`)
  through `mcp-publisher login github-oidc`. The `io.github.shrec/` name prefix
  is tied to the repository owner, and no registry token is stored in GitHub or
  in the repository.
- It downloads `mcp-publisher` v1.8.1 from its GitHub release and checks the
  archive against a SHA-256 digest pinned in the workflow, failing on any
  mismatch. The pinned version, the download URL and the digest change
  together; take a new digest from that release's checksums file.
- It retries `mcp-publisher publish` up to 10 times, 30 seconds apart, because
  a new PyPI version can take minutes to become visible to the registry.

Verify the listing after every release:

```bash
curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.shrec/aiworkhub"
```

The response must list `io.github.shrec/aiworkhub` at the released version. A
skipped job is not evidence that the entry was published.

## Local VSIX Installation

Build and install locally:

```bash
npm --prefix vscode-extension install
npm --prefix vscode-extension test
npm --prefix vscode-extension run package
code --install-extension vscode-extension/dist/aiworkhub-*.vsix
```

### Remote-SSH Installation

When using VS Code Remote - SSH, install the extension into the remote host
workspace:

1. Download or build the VSIX on the remote host.
2. Run
   `code --install-extension vscode-extension/dist/aiworkhub-*.vsix` inside
   the remote SSH terminal (or use **Extensions: Install from VSIX...**).
3. The extension kind is `workspace`, so it runs on the remote host — no
   browser, iframe, or port-forwarding required.

## Python Package (manual build verification)

```bash
pip install build twine
python -m build --outdir dist
python -m twine check dist/*
```

Production uploads are owned by the tag-driven Trusted Publishing job. Do not
store a long-lived PyPI credential in the repository or workflow secrets.

## Versioning

AIWorkHub follows [Semantic Versioning](https://semver.org/). Before a release:

1. Update `__version__` in `src/aiworkhub/_version.py`.
2. Run `python scripts/release_metadata.py sync` and commit every projection.
3. Run `python scripts/release_metadata.py check --tag v<x.y.z>`.
4. Commit the version changes, then create the matching `v<x.y.z>` tag.
