# AIWorkHub article publishing checklist

## Before publishing

- Confirm the latest release link resolves and contains a VSIX.
- Use `https://github.com/shrec/AIWorkHub` as the product link.
- Say **GitHub Release VSIX only for now** until Marketplace/Open VSX/PyPI are
  actually live.
- Do not publish estimated token or cost savings as measured results.
- Do not claim passive Manager Context Graph capture for Claude or Copilot.
- Keep the Source Graph and Manager Context Graph as distinct features.
- Preserve the MIT license statement and the kimi-atlas acknowledgement.
- Use the current dashboard screenshot or verified demo GIF; do not include
  private task names, local paths, tokens, credentials or personal chat text.

## Recommended publication order

1. GitHub README/release page remains the source of truth.
2. Publish the long-form article on one canonical host (Dev.to, Hashnode or a
   personal site).
3. Import or cross-post to Medium with the canonical link.
4. Publish the LinkedIn post and X thread pointing to the canonical article.
5. Submit Show HN only when the repository can handle direct installation and
   issue traffic.
6. Tailor one Reddit post per community; do not paste the same promotional text
   everywhere at once.
7. Use Product Hunt after Marketplace/Open VSX is live or the VSIX installation
   path is demonstrably frictionless for new users.

## Visuals

- Hero: `docs/assets/aiworkhub-hero.svg`
- Product screenshot:
  `docs/assets/screenshots/aiworkhub-self-hosted-dashboard.png`
- Workflow demo: `docs/assets/demo/aiworkhub-task-review-loop.gif`

Suggested alt text:

> AIWorkHub VS Code dashboard showing repository-local context health, task
> lifecycle counts, callback delivery and a model worker in progress.

## After publishing

- Record the URL and publication date in the release notes or project log.
- Reply to setup failures with exact platform/runtime/version information.
- Convert repeated questions into Getting Started or Troubleshooting updates.
- Track meaningful signals: release downloads, successful first initialization,
  issue quality and returning contributors—not only views or reactions.

