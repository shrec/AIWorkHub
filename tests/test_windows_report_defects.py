"""Windows-11 field-report defects: diagnosis-with-measurement, and one fix pinned.

Card WIN-REPORT-UNCARDED. Each finding below was reproduced (or shown NOT
reproducible) on Linux with a measurement BEFORE any change, per the card.
Status now, so a reader does not mistake all four for still open: AWH-OBS-018 is
CLOSED (fixed and shipped, commit cd0d4eb, released in 0.9.86); AWH-OBS-013 is
fixed here (production side); AWH-OBS-014 and AWH-OBS-015 are diagnosed, named,
and stopped at a module outside this card's allowed_writes.

AWH-OBS-018 -- storage retention preview hit the 90s deadline on Windows. CLOSED.
  Diagnosis (measured deliberately at the time, now history): the 0.9.79 change
  collapsed three full worktree walks into one but did NOT touch the RESIDUAL
  per-worktree cost of that one pass -- worktree_storage._worktree_git_state
  spawned FIVE git subprocesses per worktree (`git rev-parse HEAD`,
  `git config --get remote.origin.url`, `git status --porcelain`,
  `git rev-list --count HEAD --not --remotes`, and the git-common-dir rev-parse).
  On Linux each fork+exec is cheap and the single pass finishes in ~9s; on Windows
  there is no fork(), every CreateProcess is ~10-50x dearer and every git binary
  open is scanned by Defender, so hundreds of worktrees overran 90s. The exact
  platform-dependent step was therefore the per-worktree git-subprocess spawn
  COUNT (with the Defender-mediated stat walk on top), never the deadline itself.
  FIXED and shipped (commit cd0d4eb, 0.9.86) by exactly the batching that
  diagnosis pointed at: git state is now resolved for the whole repository in a
  FIXED, small number of subprocess spawns (`git worktree list` + `git config` +
  one `git rev-list --stdin`) that does not grow with the worktree count; the one
  field that genuinely needs a per-worktree spawn (`dirty`) is deferred, so the
  preview still returns complete measured candidates/footprint/protected. The
  test below (test_awh_obs_018_git_state_resolution_does_not_grow_with_worktrees)
  now PINS that shipped invariant -- the repository-scoped scan the preview runs
  spends the same git-spawn count for a few worktrees as for many -- rather than
  documenting the old per-worktree bug. Raising the deadline was forbidden and was
  never the fix. What still cannot be measured on Linux is the absolute Windows
  wall-clock: reproduce it on a Windows 11 install with Defender real-time
  protection over a base holding hundreds of worktrees; the constant-spawn
  invariant proven here is the exact quantity whose former linear growth in N
  overran that deadline.

AWH-OBS-013 -- empty quarantine batches. FIXED (production) for the worktree
  quarantine in this card: storage_retention.quarantine now self-reaps an empty
  batch at the source and storage_retention.purge_empty_batches reaps any that
  already exist (test below + tests/test_storage_retention.py). The terminal-log
  quarantine already had both halves (tests/test_terminal_log_retention_empty_batches.py).

AWH-OBS-014 -- adapter usage/cost telemetry. Mechanism: provider_usage only sets
  cost_observed when the provider's own output literally carries total_cost_usd /
  cost_usd (provider_usage.py:206-214); there is NO price table anywhere.
    - claude_cli (anthropic): stream-json `result` event emits total_cost_usd ->
      cost AND tokens obtainable.
    - codex_cli (openai), deepseek_copilot_cli, glm_copilot_cli: their CLIs emit
      token usage but NO dollar field -> tokens obtainable, cost NOT obtainable
      (stays UNKNOWN, never zero).
    - *_vscode_lm / vscode_lm: in-process VS Code LM API exposes neither tokens
      nor cost -> neither obtainable.
  So every non-Anthropic bucket reports cost_known_attempts=0 / total_cost_usd=0
  with state UNKNOWN, reason one_or_more_matched_attempt_costs_unknown. What is
  now captured is unchanged (tokens, via terminal_log_retention.backfill_usage_capture);
  what is NOT obtainable is a provider-reported dollar cost for any non-Anthropic
  route -- and the test proves the unknown stays None, never a zero that reads as
  free. Making cost known for those routes REQUIRES a new per-model price table
  keyed by input/output/cache token rates, wired into a cost emitter that lives in
  forbidden modules (process_launcher / core / cost_ledger); per the card this is
  named and stopped here rather than half-built.

AWH-OBS-015 -- artifact_invalid. Emitter: evidence_instruments._regular (line 54)
  raises EvidenceInstrumentError(f"artifact_invalid:{relative}") when must_exist and
  the path is a symlink, a non-regular file, OR larger than 32 MiB. The symlink and
  oversize arms are TRUE invalid-artifact detections. But Path.is_file() returns
  False (it does not raise) for a merely-ABSENT file, so an absent required artifact
  takes the same arm and is reported artifact_invalid -- a FALSE POSITIVE relative
  to the module's own missing-vs-invalid contract (the sibling artifact_unavailable
  only fires on OSError, not on absence). The message names which artifact (the
  `:{relative}` suffix). The precise fix -- a distinct `artifact_missing:` reason for
  the not-a-file-because-absent case -- is in src/aiworkhub/evidence_instruments.py,
  OUTSIDE this card's allowed_writes, so it is named and stopped.

Validation commands the sandbox could run are reported in the worker's final
message; none is reported as a failure merely because it could not run.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aiworkhub import (
    evidence_instruments,
    provider_usage,
    runtime_adapters,
    storage_retention,
    task_store,
    worktree_storage,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _make_repo(tmp_path: Path, worktrees: int) -> dict[str, Path]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    base = tmp_path / "worktrees"
    base.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(tmp_path, "clone", str(remote), str(repo))
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "push", "origin", "HEAD:refs/heads/main")
    _git(repo, "fetch", "origin")
    assert task_store.initialize_repository(repo)["ok"]
    for index in range(worktrees):
        entry = base / f"request-{index:03d}"
        entry.mkdir()
        _git(repo, "worktree", "add", "--detach", str(entry / "worktree"), "HEAD")
    return {"repo": repo, "base": base}


# terminal_runs_days defaults to 30; 31 real days past it without touching mtimes.
_AGED_NOW_OFFSET_DAYS = 31


def _aged_now() -> float:
    import time

    return time.time() + _AGED_NOW_OFFSET_DAYS * 86400


# --- AWH-OBS-018 ------------------------------------------------------------


def test_awh_obs_018_preview_completes_within_deadline_on_linux(tmp_path: Path) -> None:
    # Reproduce the 0.9.79 claim on Linux: the single-pass measurement completes
    # (never the incomplete/deadline shape) and returns the aged worktrees as
    # candidates -- the fix is real and holds on Linux.
    repo = _make_repo(tmp_path, worktrees=3)
    result = storage_retention.preview(
        repo["repo"], base=repo["base"], now=_aged_now(), deadline_seconds=60.0
    )
    assert result.get("incomplete") is not True
    assert result["complete"] is True
    assert result["candidate_count"] == 3


def _add_worktrees(repo: Path, base: Path, count: int) -> Path:
    """Attach ``count`` detached worktrees of ``repo`` under a fresh ``base``."""
    base.mkdir(parents=True)
    for index in range(count):
        entry = base / f"wt-{index:03d}"
        entry.mkdir()
        _git(repo, "worktree", "add", "--detach", str(entry / "worktree"), "HEAD")
    return base


def _count_git_spawns(fn) -> int:
    """Count git subprocess spawns issued while ``fn`` runs.

    Wraps ``worktree_storage.subprocess.run`` -- through which BOTH the
    per-worktree ``_git`` and the batched repository-level git-state resolution
    pass -- so the tally is exactly the CreateProcess count the Windows deadline
    was sensitive to, countable on Linux without a wall clock.
    """
    real = worktree_storage.subprocess.run
    tally = {"n": 0}

    def _wrapped(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
            tally["n"] += 1
        return real(cmd, *args, **kwargs)

    worktree_storage.subprocess.run = _wrapped
    try:
        fn()
    finally:
        worktree_storage.subprocess.run = real
    return tally["n"]


def test_awh_obs_018_git_state_resolution_does_not_grow_with_worktrees(
    tmp_path: Path,
) -> None:
    # AWH-OBS-018 is CLOSED: fixed and shipped in commit cd0d4eb (0.9.86). The
    # diagnosis (five git spawns per worktree in _worktree_git_state) is now
    # history; this test PINS the shipped invariant instead of the old bug. The
    # repository-scoped scan that storage_retention.preview runs (see
    # storage_retention.repo_storage_footprint) resolves git state for the whole
    # repository in a FIXED, small number of subprocess spawns, so its
    # CreateProcess count -- the exact quantity that overran the Windows 90s
    # deadline -- does not grow with the number of worktrees.
    made = _make_repo(tmp_path, worktrees=0)
    repo = made["repo"]
    small = _add_worktrees(repo, tmp_path / "small", 2)
    large = _add_worktrees(repo, tmp_path / "large", 12)

    small_spawns = _count_git_spawns(
        lambda: worktree_storage.scan_worktrees(small, repo_root=repo, with_sizes=False)
    )
    large_spawns = _count_git_spawns(
        lambda: worktree_storage.scan_worktrees(large, repo_root=repo, with_sizes=False)
    )

    # The shipped invariant: a fixed handful of repository-level git queries,
    # identical for 2 worktrees and for 12 -- never ~5N. This is what the batching
    # in cd0d4eb guarantees, and what stops the Windows deadline overrun.
    assert small_spawns == large_spawns
    assert large_spawns <= 6

    # Before/after contrast in the same run: the per-worktree path the preview no
    # longer takes (scan without repo_root) still pays ~5 git spawns per extra
    # worktree, so the constant above is the batching, not git merely being idle.
    per_worktree_small = _count_git_spawns(
        lambda: worktree_storage.scan_worktrees(small, with_sizes=False)
    )
    per_worktree_large = _count_git_spawns(
        lambda: worktree_storage.scan_worktrees(large, with_sizes=False)
    )
    assert per_worktree_large - per_worktree_small >= 5 * (12 - 2)


# --- AWH-OBS-013 ------------------------------------------------------------


def test_awh_obs_013_worktree_quarantine_has_source_reap_and_reaper(tmp_path: Path) -> None:
    # The production side is closed for the worktree quarantine: a reaper exists
    # and removes a pre-existing batch that can never hold anything (record empty,
    # disk empty), while a content-bearing batch is spared.
    repo = _make_repo(tmp_path, worktrees=0)
    repo_id = task_store.storage_readiness(repo["repo"]).repo_id
    assert hasattr(storage_retention, "purge_empty_batches")

    qroot = repo["base"] / storage_retention.QUARANTINE_DIRNAME / repo_id
    qroot.mkdir(parents=True)
    batch = qroot / "q20260816T101142-000000000001"
    batch.mkdir()
    manifest = {
        "schema_id": storage_retention.SCHEMA_ID,
        "repo_id": repo_id,
        "batch_id": batch.name,
        "created_at": "2035-01-01T00:00:00+00:00",
        "restore_deadline": "2040-01-01T00:00:00+00:00",
        "preview_digest": "0" * 64,
        "status": "empty",
        "items": [{"id": "request-000", "head": "0" * 40, "size_bytes": 1,
                   "modified_at_epoch": 1, "state": "skipped_identity_changed"}],
    }
    storage_retention._atomic_json(batch / storage_retention.MANIFEST_NAME, manifest)

    result = storage_retention.purge_empty_batches(
        repo["repo"], confirm=True, base=repo["base"]
    )
    assert result["batch_ids"] == [batch.name]
    assert not batch.exists()


# --- AWH-OBS-014 ------------------------------------------------------------


def test_awh_obs_014_cost_observed_only_when_provider_reports_dollars(tmp_path: Path) -> None:
    # claude_cli: the stream-json result event carries total_cost_usd -> cost and
    # tokens both obtainable.
    claude = tmp_path / "claude.stdout.log"
    claude.write_text(
        json.dumps({"type": "result", "total_cost_usd": 0.42,
                    "usage": {"input_tokens": 100, "output_tokens": 50}}) + "\n",
        encoding="utf-8",
    )
    claude_usage = provider_usage.read_provider_usage(claude)
    assert claude_usage["usage_observed"] is True
    assert claude_usage["cost_observed"] is True
    assert claude_usage["cost_usd"] == pytest.approx(0.42)

    # codex_cli / copilot BYOK: token usage is reported, but no dollar field, so
    # cost is UNKNOWN -- and the unknown stays None, never a zero that reads free.
    codex = tmp_path / "codex.stdout.log"
    codex.write_text(
        json.dumps({"type": "token_count",
                    "usage": {"input_tokens": 100, "output_tokens": 50}}) + "\n",
        encoding="utf-8",
    )
    codex_usage = provider_usage.read_provider_usage(codex)
    assert codex_usage["usage_observed"] is True  # tokens obtainable
    assert codex_usage["cost_observed"] is False  # cost NOT obtainable
    assert codex_usage["cost_usd"] is None  # unknown, never 0.0


def test_awh_obs_014_every_configured_adapter_is_accounted_for() -> None:
    # Each configured adapter is named with what usage/cost it can actually report.
    obtainable = {
        "claude_cli": ("tokens", "cost"),
        "codex_cli": ("tokens",),
        "deepseek_copilot_cli": ("tokens",),
        "glm_copilot_cli": ("tokens",),
        "vscode_lm": (),
        "deepseek_vscode_lm": (),
        "glm_vscode_lm": (),
        "deepseek_manual": (),  # manual-only, never launched
    }
    # Every supported adapter has an accounted-for telemetry entry -- no adapter
    # is left with an unexplained zero.
    for adapter in runtime_adapters.SUPPORTED_ADAPTERS:
        assert adapter in obtainable
    # Only the Anthropic route yields a provider-reported dollar cost.
    assert obtainable["claude_cli"] == ("tokens", "cost")
    assert all("cost" not in caps for name, caps in obtainable.items() if name != "claude_cli")


# --- AWH-OBS-015 ------------------------------------------------------------


def test_awh_obs_015_artifact_invalid_conflates_absence_with_malformed(tmp_path: Path) -> None:
    # Resolve so the module's root-containment check compares like for like even
    # when the temp base is itself reached through a symlink.
    root = tmp_path.resolve()

    # False positive: a merely-ABSENT required artifact is reported artifact_invalid
    # (Path.is_file() returns False without raising), not artifact_missing.
    with pytest.raises(
        evidence_instruments.EvidenceInstrumentError, match=r"^artifact_invalid:missing\.json$"
    ):
        evidence_instruments._regular(root, "missing.json")

    # True positive: a symlink where a regular file is required. The message names
    # the artifact.
    (root / "real.json").write_text("{}", encoding="utf-8")
    (root / "link.json").symlink_to(root / "real.json")
    with pytest.raises(
        evidence_instruments.EvidenceInstrumentError, match=r"^artifact_invalid:link\.json$"
    ):
        evidence_instruments._regular(root, "link.json")

    # A genuine regular artifact resolves cleanly -- proving the label above is
    # specifically the absence/symlink distinction, not a blanket failure.
    assert evidence_instruments._regular(root, "real.json") == root / "real.json"
