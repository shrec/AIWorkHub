# Security Policy for AIWorkHub

## Supported Versions

Only the latest tagged release receives security patches.
Pre-release (`-alpha`, `-beta`, `-rc`) versions are not supported.

## Reporting a Vulnerability

If you find a security-sensitive bug, **do not open a public issue**.

Submit a private vulnerability report through
https://github.com/shrec/AIWorkHub/security/advisories/new with a clear
description and, if possible, a minimal reproduction. Do not include secrets
or credentials that are not required to reproduce the issue.

Response time: within 5 business days. We will confirm receipt, assess severity,
and coordinate a fix. A CVE will be filed when appropriate.

## Scope

The AIWorkHub MCP server runs over stdio transport on the local machine.
Security-sensitive areas:

- **Process launch**: worker processes are launched with environment
  sanitization (`sanitized_env`) to prevent credential leaks.
- **Write gate**: `AIWORKHUB_ALLOW_WRITES=1` must be explicitly set.
  Read-only by default.
- **File access**: workers operate in isolated workspaces with landlock
  rules when available.
- **No network listener**: the MCP server uses stdio only and the native VS
  Code dashboard communicates with its repository-scoped child process. It
  does not bind localhost, a LAN address or a public port.
- **Repository isolation**: task state, routes, context stores and Source Graph
  data resolve through the selected repository's canonical `.aiworkhub`
  registry. Cross-repository ambiguity fails closed.

Untrusted third-party model adapters are not supported. Use only adapters
provided or reviewed by the AIWorkHub maintainers.

## Disclosure Policy

Once a fix is released, we will publish an advisory with CVE number (if
applicable) and a summary of the issue.
