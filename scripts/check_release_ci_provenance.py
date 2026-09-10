#!/usr/bin/env python3
"""Fail closed unless GitHub CI succeeded for the exact release commit."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://api.github.com"
MAX_PAGES = 10
PER_PAGE = 100


class ProvenanceError(RuntimeError):
    """The required exact-commit CI provenance could not be established."""


def _request_json(url: str, token: str) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "aiworkhub-release-provenance",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            value = json.load(response)
    except (OSError, urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"GitHub Actions API request failed: {exc}") from exc
    if not isinstance(value, dict):
        raise ProvenanceError("GitHub Actions API returned a non-object response")
    return value


def require_successful_push_ci(
    *, repository: str, sha: str, workflow: str, token: str
) -> None:
    """Require a completed, successful push run of workflow at exactly *sha*."""
    if not repository or not sha or not workflow or not token:
        raise ProvenanceError("repository, SHA, workflow, and GITHUB_TOKEN are required")
    encoded_workflow = urllib.parse.quote(workflow, safe="")
    base = f"{API_ROOT}/repos/{repository}/actions/workflows/{encoded_workflow}/runs"
    saw_full_page = False
    for page in range(1, MAX_PAGES + 1):
        query = urllib.parse.urlencode(
            {"event": "push", "head_sha": sha, "per_page": PER_PAGE, "page": page}
        )
        payload = _request_json(f"{base}?{query}", token)
        runs = payload.get("workflow_runs")
        if not isinstance(runs, list):
            raise ProvenanceError("GitHub Actions API response omitted workflow_runs")
        for run in runs:
            if not isinstance(run, dict):
                raise ProvenanceError("GitHub Actions API returned a malformed workflow run")
            run_path = run.get("path")
            if (
                run.get("head_sha") == sha
                and run.get("event") == "push"
                and run.get("status") == "completed"
                and run.get("conclusion") == "success"
                and isinstance(run_path, str)
                and (run_path == workflow or run_path.startswith(f"{workflow}@"))
            ):
                return
        if len(runs) < PER_PAGE:
            saw_full_page = False
            break
        saw_full_page = True
    if saw_full_page:
        raise ProvenanceError(f"CI run search exhausted {MAX_PAGES} bounded pages")
    raise ProvenanceError("no completed successful push CI run matched workflow and exact SHA")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--sha", required=True)
    parser.add_argument("--workflow", default=".github/workflows/ci.yml")
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN", "")
    try:
        require_successful_push_ci(
            repository=args.repository or "",
            sha=args.sha,
            workflow=args.workflow,
            token=token,
        )
    except ProvenanceError as exc:
        print(f"release CI provenance rejected: {exc}", file=sys.stderr)
        return 1
    print(f"release CI provenance accepted for {args.sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
