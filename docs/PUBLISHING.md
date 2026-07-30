# AIWorkHub Publishing Guide

## Overview

The release workflow builds and attaches three artifacts to a draft GitHub
Release:

1. A Python wheel in `dist/`.
2. A Python source distribution in `dist/`.
3. A VS Code extension in `vscode-extension/dist/aiworkhub-*.vsix`.

The extension can also be published to the VS Code Marketplace and Open VSX
Registry when the corresponding repository secrets are configured. PyPI
publication is a separate manual operation.

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
5. Create a draft GitHub Release with all three artifacts attached.
   The published release also includes `SHA256SUMS` for the wheel, source
   distribution and VSIX; the canonical build verifies byte-identical repeated
   VSIX packaging before upload.
6. If `OVSX_TOKEN` is present, publish the VSIX to Open VSX Registry.
7. If `MARKETPLACE_TOKEN` is present, publish the VSIX to the VS Code
   Marketplace.

Both registries are optional. A missing secret skips only that registry. When
a secret is configured, a failed publication fails the workflow. Registry jobs
do not gate creation of the draft GitHub Release.

### Required Secrets (optional)

| Secret | Purpose |
|--------|---------|
| `OVSX_TOKEN` | Open VSX Registry publish |
| `MARKETPLACE_TOKEN` | VS Code Marketplace publish (VS Publisher) |

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

## Python Package (PyPI)

```bash
pip install build twine
python -m build --outdir dist
python -m twine check dist/*
python -m twine upload dist/*
```

## Versioning

AIWorkHub follows [Semantic Versioning](https://semver.org/). Before a release:

1. Update `__version__` in `src/aiworkhub/_version.py`.
2. Run `python scripts/release_metadata.py sync` and commit every projection.
3. Run `python scripts/release_metadata.py check --tag v<x.y.z>`.
4. Commit the version changes, then create the matching `v<x.y.z>` tag.
