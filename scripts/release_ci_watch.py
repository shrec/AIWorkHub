#!/usr/bin/env python3
"""Watch a release tag's exact-commit CI and Release runs; draft a NeedFix per failure.

Release v0.12.70 and v0.12.71 were never published: the push ``CI`` run for
their commit was red and nobody looked until the next cut. Run this right after
pushing a release tag. It resolves the tag's commit (``git rev-parse
<tag>^{commit}`` unless ``--sha`` is given), polls ``gh run list --commit <sha>``
until every push ``CI`` run and tag ``Release`` run of that commit has completed
or ``--timeout-seconds`` elapses, reads the failing jobs and steps of each run
that did not succeed, and prints one JSON object (also written to ``--output``)
with one NeedFix draft per failed run.

Every ``gh``/``git`` call is an argv list run through one injectable runner;
nothing here uses a shell.

Exit 0 when every watched run succeeded; 1 when any watched run failed (its
draft is in the output, even if another run was still pending at the timeout);
2 on a timeout, a ``gh``/``git`` error (a run already seen failing still gets
its draft), an unwritable ``--output`` or a bad argument.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

CI_WORKFLOW = "CI"
RELEASE_WORKFLOW = "Release"
WATCHED_WORKFLOWS = (CI_WORKFLOW, RELEASE_WORKFLOW)
# The gating CI run comes from a branch push and the Release run from a tag push.
WATCHED_EVENT = "push"
RUN_FIELDS = "databaseId,workflowName,status,conclusion,event,url"
RUN_LIST_LIMIT = 200
DEFAULT_TIMEOUT_SECONDS = 3600.0
DEFAULT_POLL_SECONDS = 30.0
COMMAND_TIMEOUT_SECONDS = 120.0
DETAIL_CHARS = 2000
# A watched run is green only on success: a skipped or neutral run proves nothing.
GREEN_CONCLUSION = "success"
# Inside a failed run, skipped and neutral jobs or steps are collateral, not causes.
_NOT_FAILING = frozenset({"success", "skipped", "neutral"})
_FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+-]*")
_REPO = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9.-]*/)?[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+")

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class WatchError(RuntimeError):
    """The watch cannot reach a verdict; ``reason`` is a stable name (exit 2)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail[-DETAIL_CHARS:]


def run_argv(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one argv list (never a shell) and capture its output as UTF-8 text."""
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )


def _call(runner: Runner, argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return runner(argv)
    except subprocess.TimeoutExpired as exc:
        raise WatchError(f"{argv[0]}_timeout", " ".join(argv)) from exc
    except OSError as exc:
        raise WatchError(f"{argv[0]}_unavailable", f"{' '.join(argv)}: {exc}") from exc


def gh_json(runner: Runner, repo: str | None, args: list[str]) -> Any:
    """Run ``gh <args> [--repo <repo>]`` through ``runner`` and parse its JSON stdout."""
    argv = ["gh", *args, *(["--repo", repo] if repo else [])]
    done = _call(runner, argv)
    if done.returncode != 0:
        raise WatchError("gh_failed", f"{' '.join(argv)}: {done.stderr or done.stdout}")
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise WatchError("gh_bad_output", f"{' '.join(argv)}: {exc}") from exc


def resolve_sha(runner: Runner, tag: str, sha: str | None) -> str:
    """``sha`` itself, or the commit ``git rev-parse --verify <tag>^{commit}`` names."""
    if sha is None:
        done = _call(runner, ["git", "rev-parse", "--verify", f"{tag}^{{commit}}"])
        if done.returncode != 0:
            raise WatchError("tag_not_found", f"{tag}: {done.stderr.strip()}")
        sha = done.stdout.strip()
    if not _FULL_SHA.fullmatch(sha):
        raise WatchError("invalid_sha", f"not a full lowercase commit SHA: {sha[:80]!r}")
    return sha


def _run_record(run: dict[str, Any]) -> dict[str, Any]:
    run_id, status, conclusion = run.get("databaseId"), run.get("status"), run.get("conclusion")
    if type(run_id) is not int or not isinstance(status, str):
        raise WatchError("gh_bad_output", f"malformed run: {json.dumps(run)[:400]}")
    return {
        "workflow": run["workflowName"],
        "id": run_id,
        "url": str(run.get("url") or ""),
        "status": status,
        "conclusion": conclusion if isinstance(conclusion, str) and conclusion else None,
        "failed_jobs": [],
    }


def list_runs(runner: Runner, repo: str | None, sha: str) -> list[dict[str, Any]]:
    """The push ``CI`` and ``Release`` runs of ``sha``, in workflow then run-id order."""
    payload = gh_json(
        runner,
        repo,
        ["run", "list", "--commit", sha, "--limit", str(RUN_LIST_LIMIT), "--json", RUN_FIELDS],
    )
    if not isinstance(payload, list) or not all(isinstance(run, dict) for run in payload):
        raise WatchError("gh_bad_output", "gh run list did not return a list of runs")
    if len(payload) >= RUN_LIST_LIMIT:
        raise WatchError("gh_run_list_truncated", f"{len(payload)} runs reached the limit")
    runs = [
        _run_record(run)
        for run in payload
        if run.get("workflowName") in WATCHED_WORKFLOWS and run.get("event") == WATCHED_EVENT
    ]
    return sorted(runs, key=lambda run: (WATCHED_WORKFLOWS.index(run["workflow"]), run["id"]))


def pending_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A watched workflow with no run yet, and every watched run not yet completed."""
    pending: list[dict[str, Any]] = []
    for workflow in WATCHED_WORKFLOWS:
        mine = [run for run in runs if run["workflow"] == workflow]
        if not mine:
            pending.append({"workflow": workflow, "id": None, "status": "not_registered"})
        pending.extend(
            {"workflow": workflow, "id": run["id"], "status": run["status"]}
            for run in mine
            if run["status"] != "completed"
        )
    return pending


def _did_not_succeed(run: dict[str, Any]) -> bool:
    return run["status"] == "completed" and run["conclusion"] != GREEN_CONCLUSION


def _failing(item: dict[str, Any]) -> bool:
    """A job or step whose conclusion names an outcome other than a pass or a skip."""
    conclusion = item.get("conclusion")
    return isinstance(conclusion, str) and bool(conclusion) and conclusion not in _NOT_FAILING


def failed_jobs(runner: Runner, repo: str | None, run_id: int) -> list[dict[str, Any]]:
    """Each failing job of ``run_id`` with the names of its failing steps."""
    payload = gh_json(runner, repo, ["run", "view", str(run_id), "--json", "jobs"])
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list) or not all(isinstance(job, dict) for job in jobs):
        raise WatchError("gh_bad_output", f"gh run view {run_id} did not return a list of jobs")
    return [
        {
            "name": str(job.get("name") or ""),
            "failed_steps": [
                str(step.get("name") or "")
                for step in job.get("steps") or []
                if isinstance(step, dict) and _failing(step)
            ],
        }
        for job in jobs
        if _failing(job)
    ]


def needfix_draft(tag: str, sha: str, run: dict[str, Any]) -> dict[str, Any]:
    """One NeedFix draft for one watched run that did not succeed."""
    conclusion = run["conclusion"] or "unknown"
    jobs = run["failed_jobs"]
    lines = [
        f"- {job['name']}: {', '.join(job['failed_steps']) or 'no failing step reported'}"
        for job in jobs
    ]
    description = "\n".join(
        [
            f"The {run['workflow']} run {run['id']} for release tag {tag} "
            f"(commit {sha}) concluded {conclusion}.",
            f"Run: {run['url']}",
            "Failing jobs and steps:",
            *(lines or ["- no failing job reported"]),
        ]
    )
    return {
        "title": f"Release {tag}: {run['workflow']} run {run['id']} concluded {conclusion}",
        "description": description,
        "kind": "bug",
        "severity": "high",
        "tags": ["release", "ci", tag],
        "evidence": {
            "tag": tag,
            "sha": sha,
            "workflow": run["workflow"],
            "run_id": run["id"],
            "run_url": run["url"],
            "conclusion": conclusion,
            "failed_jobs": jobs,
        },
    }


def watch(
    tag: str,
    sha: str | None = None,
    *,
    repo: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    runner: Runner = run_argv,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[int, dict[str, Any]]:
    """``(exit code, result)`` once the watched runs complete or the timeout hits."""
    result: dict[str, Any] = {
        "tag": tag,
        "sha": sha,
        "state": "error",
        "runs": [],
        "pending": [],
        "needfix_drafts": [],
    }
    try:
        commit = result["sha"] = resolve_sha(runner, tag, sha)
        deadline = clock() + timeout_seconds
        while True:
            runs = result["runs"] = list_runs(runner, repo, commit)
            pending = result["pending"] = pending_runs(runs)
            remaining = deadline - clock()
            if not pending or remaining <= 0:
                break
            pause = min(poll_seconds, remaining)
            waiting = ", ".join(f"{item['workflow']} {item['status']}" for item in pending)
            print(
                f"release CI watch: waiting on {waiting}; next check in {pause:g}s",
                file=sys.stderr,
            )
            sleep(pause)
        failed = [run for run in runs if _did_not_succeed(run)]
        for run in failed:
            run["failed_jobs"] = failed_jobs(runner, repo, run["id"])
    except WatchError as exc:
        result.update(reason=exc.reason, detail=exc.detail)
        # A run already seen failing keeps its draft, even if its jobs were never read.
        result["needfix_drafts"] = [
            needfix_draft(tag, result["sha"], run)
            for run in result["runs"]
            if _did_not_succeed(run)
        ]
        return 2, result
    result["needfix_drafts"] = [needfix_draft(tag, commit, run) for run in failed]
    if failed:
        result["state"] = "failed"
        return 1, result
    if pending:
        result["state"] = "timeout"
        return 2, result
    result["state"] = "green"
    return 0, result


def main(
    argv: list[str] | None = None,
    *,
    runner: Runner = run_argv,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").partition("\n")[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--sha")
    parser.add_argument("--repo")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not _TAG.fullmatch(args.tag):
        parser.error(f"--tag must be a plain tag name, got {args.tag!r}")
    if args.repo is not None and not _REPO.fullmatch(args.repo):
        parser.error(f"--repo must be [HOST/]OWNER/NAME, got {args.repo!r}")
    if not (math.isfinite(args.timeout_seconds) and args.timeout_seconds >= 0):
        parser.error(f"--timeout-seconds must be finite and >= 0, got {args.timeout_seconds}")
    if not (math.isfinite(args.poll_seconds) and args.poll_seconds > 0):
        parser.error(f"--poll-seconds must be finite and > 0, got {args.poll_seconds}")
    code, result = watch(
        args.tag,
        args.sha,
        repo=args.repo,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
        runner=runner,
        sleep=sleep,
        clock=clock,
    )
    text = json.dumps(result, indent=2)
    print(text)
    if args.output is not None:
        try:
            args.output.write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"release CI watch: cannot write {args.output}: {exc}", file=sys.stderr)
            return 2
    return code


if __name__ == "__main__":
    raise SystemExit(main())
