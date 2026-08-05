"""Optional pytest plugin used only by ``aiworkhub_suite_profile``."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any


_STATE: dict[str, dict[str, Any]] = {}
_ROWS: dict[str, dict[str, Any]] = {}


def _rss_bytes() -> int | None:
    try:
        import resource

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value if os.name == "nt" else value * 1024
    except (ImportError, OSError, ValueError):
        return None


def pytest_runtest_setup(item: Any) -> None:
    root = Path(str(item.config.rootpath))
    disk = shutil.disk_usage(root)
    times = os.times()
    _STATE[item.nodeid] = {
        "started": time.perf_counter(),
        "cpu": float(times.user + times.system),
        "rss": _rss_bytes(),
        "disk_free": int(disk.free),
    }


def pytest_runtest_logreport(report: Any) -> None:
    row = _ROWS.setdefault(report.nodeid, {
        "nodeid": report.nodeid,
        "outcome": "passed",
        "duration_seconds": 0.0,
        "cpu_seconds": None,
        "rss_delta_bytes": None,
        "disk_free_delta_bytes": None,
    })
    row["duration_seconds"] = round(
        float(row["duration_seconds"]) + float(report.duration or 0.0), 6
    )
    if report.failed:
        row["outcome"] = "failed"
    elif report.skipped and row["outcome"] != "failed":
        row["outcome"] = "skipped"
    if report.when != "teardown":
        return
    started = _STATE.pop(report.nodeid, None)
    if not started:
        return
    times = os.times()
    row["cpu_seconds"] = round(
        max(0.0, float(times.user + times.system) - float(started["cpu"])), 6
    )
    rss = _rss_bytes()
    if rss is not None and started["rss"] is not None:
        row["rss_delta_bytes"] = max(0, rss - int(started["rss"]))
    try:
        disk_free = shutil.disk_usage(Path.cwd()).free
        row["disk_free_delta_bytes"] = int(disk_free) - int(started["disk_free"])
    except OSError:
        pass


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    output = os.environ.get("AIWORKHUB_SUITE_PROFILE_OUTPUT", "")
    if not output:
        return
    payload = {
        "schema_id": "aiworkhub.suite_profile.pytest_run.v1",
        "exitstatus": int(exitstatus),
        "tests": sorted(_ROWS.values(), key=lambda row: row["nodeid"]),
    }
    try:
        Path(output).write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
