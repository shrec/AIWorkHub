"""Wall-clock session-limit reset and account-wide reviewer route exclusion (NF-2026-01684).

A Claude CLI refusal reports ``resets 2:40am (Asia/Tbilisi)``. The classifier
only knew retry-after seconds, ``resets in N s`` and ISO-8601, so the window
stayed unreported; and after the limit the reviewer orchestrator planned a
sibling model on the same exhausted claude_cli account.
"""

from __future__ import annotations

import calendar
import json
import sys
import time
import zoneinfo
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import (  # noqa: E402
    process_launcher_provider_failures,
    review_orchestrator,
    runtime_adapters,
    terminal_failure_classification,
)
import pytest  # noqa: E402


def _zone_available(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _needs_zone(name: str):
    return pytest.mark.skipif(
        not _zone_available(name),
        reason=f"tz database absent: no entry for {name}",
    )


needs_tbilisi = _needs_zone("Asia/Tbilisi")
needs_new_york = _needs_zone("America/New_York")


RESET_NOW = datetime(2026, 10, 23, 22, 32, 26, tzinfo=timezone.utc)
RESET_AT = "2026-10-23T22:40:00+00:00"
NEXT_DAY_RESET_AT = "2026-10-24T22:40:00+00:00"
MEASURED_LINE = (
    '"api_error_status":429,'
    '"result":"You\'ve hit your session limit · resets 2:40am (Asia/Tbilisi)"'
)
# No "session limit" prose, so _refusal_kind falls through to the 429 status map.
RATE_LIMITED_LINE = "http_status=429 resets 2:40am (Asia/Tbilisi)"
# Resolved against RESET_NOW on a +04:00 clock without DST: Asia/Tbilisi or the stubbed host.
TWELVE_HOUR_FORMS = [
    ("2:40am", "2026-10-23T22:40:00+00:00"),
    ("2:40 am", "2026-10-23T22:40:00+00:00"),
    ("02:40AM", "2026-10-23T22:40:00+00:00"),
    ("2am", "2026-10-24T22:00:00+00:00"),
    ("2:40pm", "2026-10-24T10:40:00+00:00"),
    ("11pm", "2026-10-24T19:00:00+00:00"),
    ("12am", "2026-10-24T20:00:00+00:00"),
    ("12:30am", "2026-10-24T20:30:00+00:00"),
    ("12pm", "2026-10-24T08:00:00+00:00"),
    ("12:30pm", "2026-10-24T08:30:00+00:00"),
]


def _classify(message: str, now: datetime | None = RESET_NOW) -> dict:
    return runtime_adapters.classify_provider_outcome(
        exit_code=1, message=message, now=now
    )


# -- (1) runtime_adapters: the wall-clock reset window ------------------------


@needs_tbilisi
def test_measured_claude_line_reports_the_wall_clock_reset() -> None:
    outcome = _classify(MEASURED_LINE)

    assert outcome["reset_at"] == RESET_AT
    assert outcome["reset_reported"] is True
    assert outcome["recoverable"] is True
    assert outcome["retry_after_seconds"] is None
    # The body names "session limit", which _refusal_kind resolves before the 429 status.
    assert outcome["refusal_kind"] == runtime_adapters.REFUSAL_SESSION_LIMIT
    assert outcome["reason"] == (
        "provider_refused_session_limit_recoverable_after_reported_window"
    )


@needs_tbilisi
def test_429_without_session_limit_prose_is_rate_limited_after_the_reported_window() -> None:
    outcome = _classify(RATE_LIMITED_LINE)

    assert outcome["refusal_kind"] == runtime_adapters.REFUSAL_RATE_LIMITED
    assert outcome["reset_at"] == RESET_AT
    assert outcome["reset_reported"] is True
    assert outcome["reason"] == (
        "provider_refused_rate_limited_recoverable_after_reported_window"
    )


@needs_tbilisi
def test_reset_window_resolves_the_measured_line_directly() -> None:
    assert runtime_adapters._reset_window(MEASURED_LINE, RESET_NOW) == (None, RESET_AT)


@needs_tbilisi
def test_a_reset_already_past_rolls_to_the_next_day() -> None:
    outcome = _classify(MEASURED_LINE, now=datetime(2026, 10, 23, 22, 45, tzinfo=timezone.utc))

    assert outcome["reset_at"] == NEXT_DAY_RESET_AT
    assert outcome["reset_reported"] is True


@needs_tbilisi
@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 23, 22, 39, 59, tzinfo=timezone.utc), RESET_AT),
        (datetime(2026, 10, 23, 22, 40, 0, tzinfo=timezone.utc), RESET_AT),
        (datetime(2026, 10, 23, 22, 40, 1, tzinfo=timezone.utc), NEXT_DAY_RESET_AT),
    ],
)
def test_the_reset_instant_itself_is_not_in_the_past(now: datetime, expected: str) -> None:
    assert _classify(MEASURED_LINE, now=now)["reset_at"] == expected


@needs_tbilisi
@pytest.mark.parametrize(
    ("clock", "expected"),
    TWELVE_HOUR_FORMS,
)
def test_twelve_hour_clock_forms_resolve_to_the_next_local_occurrence(
    clock: str, expected: str,
) -> None:
    outcome = _classify(f"http_status=429 resets {clock} (Asia/Tbilisi)")

    assert outcome["reset_at"] == expected
    assert outcome["reset_reported"] is True


@needs_tbilisi
@pytest.mark.parametrize("separator", [" · ", " - ", ", ", " ", ""])
def test_any_separator_before_resets_is_accepted(separator: str) -> None:
    outcome = _classify(
        f"http_status=429 usage reached{separator}resets 2:40am (Asia/Tbilisi)"
    )

    assert outcome["reset_at"] == RESET_AT


@needs_tbilisi
def test_wall_clock_match_is_case_insensitive() -> None:
    assert _classify("HTTP_STATUS=429 RESETS 2:40AM (Asia/Tbilisi)")["reset_at"] == RESET_AT


@needs_tbilisi
@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 10, 23, 22, 32, 26),
        datetime(2026, 10, 24, 2, 32, 26, tzinfo=timezone(timedelta(hours=4))),
    ],
)
def test_now_is_read_as_a_utc_instant(now: datetime) -> None:
    assert _classify(MEASURED_LINE, now=now)["reset_at"] == RESET_AT


@needs_tbilisi
def test_default_now_is_the_current_utc_time() -> None:
    before = datetime.now(timezone.utc)
    outcome = runtime_adapters.classify_provider_outcome(exit_code=1, message=MEASURED_LINE)
    after = datetime.now(timezone.utc)

    reset_at = datetime.fromisoformat(outcome["reset_at"])
    assert reset_at.utcoffset() == timedelta(0)
    assert before <= reset_at <= after + timedelta(days=1)
    assert reset_at.astimezone(ZoneInfo("Asia/Tbilisi")).strftime("%H:%M") == "02:40"


@needs_new_york
def test_the_next_occurrence_follows_the_local_wall_clock_across_a_dst_change() -> None:
    now = datetime(2026, 10, 31, 20, 0, tzinfo=timezone.utc)

    outcome = _classify("http_status=429 resets 9am (America/New_York)", now=now)

    assert outcome["reset_at"] == "2026-11-01T14:00:00+00:00"


@needs_new_york
def test_an_ambiguous_local_time_takes_its_first_occurrence() -> None:
    now = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)

    outcome = _classify("http_status=429 resets 1:30am (America/New_York)", now=now)

    assert outcome["reset_at"] == "2026-11-01T05:30:00+00:00"


@needs_new_york
@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 1, 5, 45, tzinfo=timezone.utc), "2026-11-01T06:30:00+00:00"),
        (datetime(2026, 11, 1, 6, 15, tzinfo=timezone.utc), "2026-11-01T06:30:00+00:00"),
        (datetime(2026, 11, 1, 6, 45, tzinfo=timezone.utc), "2026-11-02T06:30:00+00:00"),
    ],
)
def test_the_second_pass_of_a_repeated_local_hour_is_not_skipped(
    now: datetime, expected: str
) -> None:
    outcome = _classify("http_status=429 resets 1:30am (America/New_York)", now=now)

    assert outcome["reset_at"] == expected


def test_unknown_zone_leaves_the_window_unreported(monkeypatch: pytest.MonkeyPatch) -> None:
    # A host with a tz database can tell an unknown zone from a missing database.
    monkeypatch.setattr(zoneinfo, "available_timezones", lambda: {"Asia/Tbilisi"})
    outcome = _classify("http_status=429 resets 2:40am (Nowhere/Atlantis)")

    assert outcome["reset_at"] is None
    assert outcome["reset_reported"] is False
    assert outcome["recoverable"] is True
    assert outcome["reason"] == (
        "provider_refused_rate_limited_recoverable_but_reset_window_unreported"
    )


# -- (1b) the same window on a host with no tz database (Windows without tzdata) --

HOST_OFFSET_SECONDS = 4 * 3600


@pytest.fixture
def host_zone_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """No tz database at all, and a host clock that reads +04:00 all year."""

    def no_such_zone(name: str):
        raise ZoneInfoNotFoundError(name)

    monkeypatch.setattr(runtime_adapters, "ZoneInfo", no_such_zone)
    monkeypatch.setattr(zoneinfo, "available_timezones", lambda: set())
    monkeypatch.setattr(
        time, "localtime", lambda stamp: time.gmtime(stamp + HOST_OFFSET_SECONDS)
    )
    monkeypatch.setattr(
        time,
        "mktime",
        lambda parts: float(calendar.timegm(tuple(parts)[:6]) - HOST_OFFSET_SECONDS),
    )


@pytest.mark.usefixtures("host_zone_only")
def test_without_a_tz_database_the_measured_line_resolves_in_the_host_zone() -> None:
    outcome = _classify(MEASURED_LINE)

    assert outcome["reset_at"] == RESET_AT
    assert outcome["reset_reported"] is True
    assert outcome["reason"] == (
        "provider_refused_session_limit_recoverable_after_reported_window"
    )


@pytest.mark.usefixtures("host_zone_only")
def test_without_a_tz_database_a_reset_already_past_rolls_to_the_next_day() -> None:
    now = datetime(2026, 10, 23, 22, 45, tzinfo=timezone.utc)

    assert _classify(MEASURED_LINE, now=now)["reset_at"] == NEXT_DAY_RESET_AT


@pytest.mark.usefixtures("host_zone_only")
@pytest.mark.parametrize(("clock", "expected"), TWELVE_HOUR_FORMS)
def test_without_a_tz_database_every_twelve_hour_form_resolves_in_the_host_zone(
    clock: str, expected: str
) -> None:
    outcome = _classify(f"http_status=429 resets {clock} (Asia/Tbilisi)")

    assert outcome["reset_at"] == expected
    assert outcome["reset_reported"] is True


@pytest.mark.parametrize("clock", ["13pm", "0am", "0:30pm", "99am", "12:60am"])
def test_an_hour_or_minute_off_the_twelve_hour_clock_is_unreported(clock: str) -> None:
    outcome = _classify(f"http_status=429 resets {clock} (Asia/Tbilisi)")

    assert outcome["reset_at"] is None
    assert outcome["reset_reported"] is False


@pytest.mark.parametrize(
    "text",
    [
        "http_status=429 resets 14:40 (Asia/Tbilisi)",
        "http_status=429 resets 2:40am Asia/Tbilisi",
        "http_status=429 resets 2:40am ()",
    ],
)
def test_text_that_is_not_the_wall_clock_form_is_unreported(text: str) -> None:
    outcome = _classify(text)

    assert outcome["reset_at"] is None
    assert outcome["reset_reported"] is False


def test_iso_window_wins_over_a_wall_clock_window() -> None:
    outcome = _classify(
        "429 session limit. Your limit resets at 2026-08-18T18:00:00Z; also resets 2:40am (Asia/Tbilisi)"
    )

    assert outcome["reset_at"] == "2026-08-18T18:00:00Z"
    assert outcome["reset_reported"] is True


@pytest.mark.parametrize(
    ("message", "retry_after", "reset_at"),
    [
        ("429 Too Many Requests. retry-after: 30", 30, None),
        ("429 rate limit. resets in 90 seconds", 90, None),
        (
            "429 You've hit your session limit. Your limit resets at 2026-08-18T18:00:00Z",
            None,
            "2026-08-18T18:00:00Z",
        ),
    ],
)
def test_existing_window_forms_are_unchanged(
    message: str, retry_after: int | None, reset_at: str | None,
) -> None:
    outcome = _classify(message)

    assert outcome["retry_after_seconds"] == retry_after
    assert outcome["reset_at"] == reset_at
    assert outcome["reset_reported"] is True


# -- (2) review_orchestrator: an account limit excludes the whole adapter -----


ACCOUNT_LIMITED = "launch_failed:provider_account_limit"
ACCOUNT_LIMIT_REASONS = [
    ACCOUNT_LIMITED,
    "worker_failed:provider_account_limit",
    f"{ACCOUNT_LIMITED}:provider_launched=false",
    f"{ACCOUNT_LIMITED}:hold=capacity_hold",
]
UNRELATED_REASONS = [
    "",
    "worker_failed:provider_timeout:exit_code=1",
    "worker_failed:mechanical_terminal",
    "launch_failed:model_unavailable",
    "worker_failed:vscode_lm_quality_review_submit_required:exit_code=1",
    "worker_failed:provider_quota",
    "launch_failed:provider_quota",
    "worker_failed:provider_quota:provider_launched=false",
    "worker_failed:provider_quota_typo",
    "launch_failed:provider_refusal",
    "launch_failed:provider_credit",
    "launch_failed:provider_auth",
    "worker_failed:provider_account_limit_typo",
    "launch_failed:xprovider_account_limit",
    "provider_refused_rate_limited_recoverable_after_reported_window",
]


@pytest.mark.parametrize("reason", ACCOUNT_LIMIT_REASONS)
def test_account_limited_predicate_recognises_the_persisted_account_limit_class(
    reason: str,
) -> None:
    assert review_orchestrator.reviewer_route_account_limited(reason) is True


@pytest.mark.parametrize("reason", UNRELATED_REASONS)
def test_account_limited_predicate_ignores_every_other_reason(reason: str) -> None:
    assert review_orchestrator.reviewer_route_account_limited(reason) is False


def test_account_limited_predicate_reads_none_as_not_limited() -> None:
    assert review_orchestrator.reviewer_route_account_limited(None) is False


@pytest.mark.parametrize(
    ("error", "failure_class"),
    [
        (
            "provider_refused_session_limit_recoverable_after_reported_window",
            "provider_account_limit",
        ),
        (
            "provider_refused_quota_exhausted_recoverable_but_reset_window_unreported",
            "provider_account_limit",
        ),
        (
            "provider_refused_rate_limited_recoverable_but_reset_window_unreported",
            "provider_account_limit",
        ),
        ("rate_limit_exceeded", "provider_quota"),
        ("provider_refused_balance_exhausted", "provider_refusal"),
    ],
)
def test_only_an_account_limit_refusal_mints_the_account_limit_class(
    error: str, failure_class: str
) -> None:
    status = {"ok": True, "state": "launch_failed", "latest_event": {"error": error}}
    reason = review_orchestrator.mechanical_reviewer_failure_reason(status)
    assert reason == f"launch_failed:{failure_class}"


CHAIN_NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)
_CLAUDE_OPUS = {"runner": "claude_opus-5", "adapter_id": "claude_cli", "model": "opus-5"}
_CLAUDE_SONNET = {"runner": "claude_sonnet-5", "adapter_id": "claude_cli", "model": "sonnet-5"}
_DEEPSEEK = {
    "runner": "deepseek_native-v4-pro",
    "adapter_id": "deepseek_copilot_cli",
    "model": "deepseek-v4-pro",
}
_VSCODE_OPUS = {
    "runner": "copilot_claude-opus-5",
    "adapter_id": "vscode_lm",
    "model": "claude-opus-5",
}
_VSCODE_SONNET = {
    "runner": "copilot_claude-sonnet-5",
    "adapter_id": "vscode_lm",
    "model": "claude-sonnet-5",
}
_CATALOG = {"schema_id": "aiworkhub.workforce_catalog.v1", "workers": [{"worker_id": "gpt-5.5"}]}


class _Manager:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def status(self, request_id: str) -> dict:
        if request_id == "target-request":
            return {
                "ok": True,
                "state": "review_ready",
                "task_card": {
                    "task_id": "TARGET",
                    "request_id": "target-request",
                    "claim_epoch": "1",
                    "packet_sha256": "a" * 64,
                    "candidate_sha256": "b" * 64,
                    "workspace_identity": "workspace-candidate-a",
                    "evidence": {"source_graph_partition_readiness": {"target": True}},
                },
            }
        return {"ok": True, "request_id": request_id, "state": "starting"}


@pytest.fixture(autouse=True)
def _fresh_routing_cache():
    review_orchestrator.reset_routing_catalog_cache()
    yield
    review_orchestrator.reset_routing_catalog_cache()


def _rank_task(routes):
    def rank_task(*_args, **_kwargs):
        return {
            "launch_contract": dict(routes[0]),
            "candidates": [
                {**route, "execution_runner": route["runner"], "excluded": False}
                for route in routes
            ],
        }

    return rank_task


def _correctness_driver(monkeypatch, tmp_path: Path, *routes: dict):
    """The real route selector over a ranking of exactly these routes, best first."""
    monkeypatch.setattr(
        review_orchestrator.task_store,
        "storage_readiness",
        lambda _repo: SimpleNamespace(
            ready=True, reason="ready", repo_id="repo-test", canonical_db="queue.sqlite"
        ),
    )
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog, "rank_task", _rank_task(routes)
    )
    monkeypatch.setattr(
        review_orchestrator.workforce_catalog,
        "build_routing_catalog",
        lambda _repo: dict(_CATALOG),
    )
    driver = review_orchestrator.ReviewOrchestrator(
        _Manager(tmp_path), db_path=tmp_path / "review.sqlite"
    )
    chain = driver.ensure_chain(
        target_task_id="TARGET", target_request_id="target-request", claim_epoch=1,
        packet_sha256="a" * 64, candidate_sha256="b" * 64, now=CHAIN_NOW,
    )
    action = next(
        candidate for candidate in chain.actions
        if candidate.lens == "correctness" and candidate.action_type == "launch"
    )
    return driver, action


def _retire_and_replan(driver, action, reason: str):
    first = driver._plan_route_attempt(action)
    assert driver._retire_route_attempt(action, first, reason) is True
    return first, driver._plan_route_attempt(action)


@pytest.mark.parametrize("reason", ACCOUNT_LIMIT_REASONS)
def test_an_account_limit_keeps_the_successor_off_the_whole_adapter(
    monkeypatch, tmp_path: Path, reason: str,
) -> None:
    driver, action = _correctness_driver(
        monkeypatch, tmp_path, _CLAUDE_OPUS, _CLAUDE_SONNET, _DEEPSEEK
    )

    first, second = _retire_and_replan(driver, action, reason)

    assert first["adapter_id"] == "claude_cli"
    assert second["attempt_index"] == 2
    assert second["adapter_id"] != "claude_cli"
    assert second["runner"] == _DEEPSEEK["runner"]


MEASURED_EVENT = json.loads("{" + MEASURED_LINE + "}")


def _launcher_recorded_status(tmp_path: Path, provider_fields: dict) -> tuple[dict, dict]:
    """The launch failure and the reviewer status the launcher records for a provider refusal."""
    stdout = tmp_path / "worker.stdout.jsonl"
    event = {"type": "result", "is_error": True, "terminal_reason": "api_error", **provider_fields}
    stdout.write_text(json.dumps(event) + "\n", encoding="utf-8")
    failure = process_launcher_provider_failures._provider_auth_failure_from_output(stdout)
    assert failure is not None
    terminal = terminal_failure_classification.terminal_event_authority(
        state="launch_failed",
        exit_code=1,
        error=failure["reason"],
        reason=terminal_failure_classification.recognised_reason(failure["reason"]),
        stdout_path=stdout,
    )
    status = {
        "ok": True,
        "request_id": "review-request",
        "task_id": "QUALITY_REVIEW_TASK",
        "state": "launch_failed",
        "latest_event": terminal,
        "task_card": {"terminal_substatus": "launch_failed", "worker_status": "launch_failed"},
    }
    return failure, status


@pytest.mark.parametrize(
    "provider_fields",
    [
        pytest.param(MEASURED_EVENT, id="measured_429"),
        pytest.param(
            {"api_error_status": 429, "error": "session_limit_reached"}, id="session_limit"
        ),
        pytest.param({"api_error_status": 429, "error": "quota_exceeded"}, id="quota_exceeded"),
    ],
)
def test_a_launcher_recorded_account_limit_keeps_the_successor_off_claude_cli(
    monkeypatch, tmp_path: Path, provider_fields: dict,
) -> None:
    failure, status = _launcher_recorded_status(tmp_path, provider_fields)
    reason = review_orchestrator.mechanical_reviewer_failure_reason(status)
    driver, action = _correctness_driver(
        monkeypatch, tmp_path, _CLAUDE_OPUS, _CLAUDE_SONNET, _DEEPSEEK
    )

    first, second = _retire_and_replan(driver, action, reason)

    assert failure["recoverable"] is True
    assert reason == "launch_failed:provider_account_limit"
    assert first["adapter_id"] == "claude_cli"
    assert second["adapter_id"] != "claude_cli"
    assert second["runner"] == _DEEPSEEK["runner"]


@pytest.mark.parametrize(
    "provider_fields",
    [
        pytest.param({"api_error_status": 402}, id="balance_402"),
        pytest.param({"api_error_status": 401, "error": "invalid_api_key"}, id="credential_401"),
        pytest.param({"api_error_status": 401}, id="undistinguished_401"),
    ],
)
def test_a_launcher_recorded_refusal_that_is_no_account_limit_is_not_classed_as_one(
    tmp_path: Path, provider_fields: dict,
) -> None:
    _failure, status = _launcher_recorded_status(tmp_path, provider_fields)

    reason = review_orchestrator.mechanical_reviewer_failure_reason(status)

    assert reason.startswith("launch_failed:")
    assert review_orchestrator.reviewer_route_account_limited(reason) is False


@pytest.mark.parametrize(
    "reason",
    [
        "worker_failed:provider_timeout",
        "worker_failed:mechanical_terminal",
        "launch_failed:model_unavailable",
    ],
)
def test_a_non_quota_failure_keeps_the_exact_route_exclusion(
    monkeypatch, tmp_path: Path, reason: str,
) -> None:
    driver, action = _correctness_driver(
        monkeypatch, tmp_path, _CLAUDE_OPUS, _CLAUDE_SONNET, _DEEPSEEK
    )

    first, second = _retire_and_replan(driver, action, reason)

    assert first["runner"] == _CLAUDE_OPUS["runner"]
    assert second["adapter_id"] == "claude_cli"
    assert second["runner"] == _CLAUDE_SONNET["runner"]


def test_the_route_scoped_provider_quota_class_keeps_the_exact_route_exclusion(
    monkeypatch, tmp_path: Path,
) -> None:
    # provider_quota is the route-scoped class: one model's quota does not bar its siblings.
    driver, action = _correctness_driver(
        monkeypatch, tmp_path, _CLAUDE_OPUS, _CLAUDE_SONNET, _DEEPSEEK
    )

    first, second = _retire_and_replan(driver, action, "launch_failed:provider_quota")

    assert first["runner"] == _CLAUDE_OPUS["runner"]
    assert second["runner"] == _CLAUDE_SONNET["runner"]


def test_the_vscode_lm_behaviour_code_still_excludes_the_whole_adapter(
    monkeypatch, tmp_path: Path,
) -> None:
    driver, action = _correctness_driver(
        monkeypatch, tmp_path, _VSCODE_OPUS, _VSCODE_SONNET, _DEEPSEEK
    )

    first, second = _retire_and_replan(
        driver, action, "worker_failed:vscode_lm_quality_review_submit_required:exit_code=1"
    )

    assert first["adapter_id"] == "vscode_lm"
    assert second["adapter_id"] == "deepseek_copilot_cli"


def test_the_account_exclusion_outlives_the_next_attempt(
    monkeypatch, tmp_path: Path,
) -> None:
    # The ceiling is 2; raise it only so a third attempt can show the exclusions accumulate.
    monkeypatch.setattr(review_orchestrator, "_MAX_ROUTE_ATTEMPTS", 3)
    driver, action = _correctness_driver(
        monkeypatch, tmp_path, _CLAUDE_OPUS, _CLAUDE_SONNET, _DEEPSEEK, _VSCODE_OPUS
    )
    first = driver._plan_route_attempt(action)
    assert driver._retire_route_attempt(action, first, ACCOUNT_LIMITED) is True
    second = driver._plan_route_attempt(action)
    assert driver._retire_route_attempt(action, second, "worker_failed:provider_timeout") is True

    third = driver._plan_route_attempt(action)

    assert [row["adapter_id"] for row in (first, second, third)] == [
        "claude_cli", "deepseek_copilot_cli", "vscode_lm",
    ]


def test_no_route_left_after_an_account_limit_defers_with_the_named_reason(
    monkeypatch, tmp_path: Path,
) -> None:
    driver, action = _correctness_driver(
        monkeypatch, tmp_path, _CLAUDE_OPUS, _CLAUDE_SONNET
    )
    first = driver._plan_route_attempt(action)
    assert driver._retire_route_attempt(action, first, ACCOUNT_LIMITED) is True

    with pytest.raises(RuntimeError, match=r"^review_route_unavailable:correctness$"):
        driver._plan_route_attempt(action)
    with pytest.raises(review_orchestrator._DeferredLaunch) as deferred:
        driver._plan_deferrable_route_attempt(action)

    assert deferred.value.reason == "review_route_unavailable:correctness"
