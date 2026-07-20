"""Tests for geoai_task_mcp.route_identity -- B811 composite route identity.

Runs standalone (python3 this_file.py) or via pytest.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

_HAS_PYTEST = False
try:
    import pytest as _pytest  # noqa: F401
    _HAS_PYTEST = True
except ImportError:
    pass

from geoai_task_mcp.route_identity import (
    RepoRouteKey,
    _validate_identifier_field,
    fail_on_legacy_thread_only,
)


def _run_standalone() -> int:
    results = {"pass": 0, "fail": 0, "details": []}

    def check(name, ok, detail=""):
        if ok:
            results["pass"] += 1
            results["details"].append({"name": name, "status": "PASS"})
        else:
            results["fail"] += 1
            results["details"].append({"name": name, "status": "FAIL", "detail": detail})
            print(f"  FAIL: {name} -- {detail}")

    def raises_value_error(fn, match=None):
        try:
            fn()
            return False
        except ValueError as e:
            if match and match not in str(e):
                return False
            return True
        except Exception:
            return False

    print("=== B811 Route Identity Tests ===\n")

    k = RepoRouteKey(repo_id="geoai", thread_id="th1", task_id="task1")
    check("valid_minimal", k.repo_id == "geoai" and k.thread_id == "th1" and k.task_id == "task1" and k.event_id == "")
    k2 = RepoRouteKey(repo_id="r", thread_id="t", task_id="k", event_id="e")
    check("valid_with_event", k2.event_id == "e")
    k3 = RepoRouteKey(repo_id="  r  ", thread_id=" t ", task_id=" k ", event_id=" e ")
    check("strips_whitespace", k3.repo_id == "r" and k3.thread_id == "t" and k3.task_id == "k" and k3.event_id == "e")
    check("rejects_empty_repo", raises_value_error(lambda: RepoRouteKey(repo_id="", thread_id="t", task_id="k"), "repo_id must not be empty"))
    check("rejects_empty_thread", raises_value_error(lambda: RepoRouteKey(repo_id="r", thread_id="", task_id="k")))
    check("rejects_empty_task", raises_value_error(lambda: RepoRouteKey(repo_id="r", thread_id="t", task_id="")))
    check("allows_empty_event", RepoRouteKey(repo_id="r", thread_id="t", task_id="k", event_id="").event_id == "")
    check("rejects_path_separator", raises_value_error(lambda: RepoRouteKey(repo_id="r/a", thread_id="t", task_id="k")))
    check("rejects_backslash", raises_value_error(lambda: RepoRouteKey(repo_id="r", thread_id="t\\1", task_id="k")))
    check("rejects_control_char", raises_value_error(lambda: RepoRouteKey(repo_id="r\x00", thread_id="t", task_id="k")))
    check("rejects_non_ascii", raises_value_error(lambda: RepoRouteKey(repo_id="r\xe4", thread_id="t", task_id="k")))
    check("rejects_space_in_id", raises_value_error(lambda: RepoRouteKey(repo_id="r r", thread_id="t", task_id="k")))
    check("rejects_excessive_length", raises_value_error(lambda: RepoRouteKey(repo_id="a" * 129, thread_id="t", task_id="k")))
    check("rejects_zero_width", raises_value_error(lambda: RepoRouteKey(repo_id="r\u200bt", thread_id="t", task_id="k")))
    check("rejects_bidi_override", raises_value_error(lambda: RepoRouteKey(repo_id="r\u202et", thread_id="t", task_id="k")))

    kf = RepoRouteKey(repo_id="r", thread_id="t", task_id="k")
    try:
        kf.repo_id = "x"
        check("frozen_immutable", False)
    except Exception:
        check("frozen_immutable", True)

    check("canonical_with_event", RepoRouteKey(repo_id="r", thread_id="t", task_id="k", event_id="e").canonical() == "r:t:k:e")
    check("canonical_without_event", RepoRouteKey(repo_id="r", thread_id="t", task_id="k").canonical() == "r:t:k:")
    o = RepoRouteKey(repo_id="repo-a", thread_id="th-x", task_id="tk-y", event_id="ev-z")
    check("round_trip", RepoRouteKey.parse(o.canonical()) == o)
    check("round_trip_no_event", RepoRouteKey.parse(RepoRouteKey(repo_id="a", thread_id="b", task_id="c").canonical()) == RepoRouteKey(repo_id="a", thread_id="b", task_id="c"))
    check("parse_rejects_3_fields", raises_value_error(lambda: RepoRouteKey.parse("a:b:c")))
    check("parse_rejects_empty_repo", raises_value_error(lambda: RepoRouteKey.parse(":t:k:e")))

    a = RepoRouteKey(repo_id="r", thread_id="t", task_id="k", event_id="e")
    b = RepoRouteKey(repo_id="r", thread_id="t", task_id="k", event_id="e")
    check("deterministic_canonical", a.canonical() == b.canonical())
    check("deterministic_hash", hash(a) == hash(b))

    check("digest_len_32", len(RepoRouteKey(repo_id="r", thread_id="t", task_id="k").digest()) == 32)
    check("digest_full_len_64", len(RepoRouteKey(repo_id="r", thread_id="t", task_id="k").digest_full()) == 64)
    check("digest_deterministic", RepoRouteKey(repo_id="r", thread_id="t", task_id="k").digest() == RepoRouteKey(repo_id="r", thread_id="t", task_id="k").digest())
    check("digest_differs_per_repo",
          RepoRouteKey(repo_id="repo-a", thread_id="t", task_id="k", event_id="e").digest()
          != RepoRouteKey(repo_id="repo-b", thread_id="t", task_id="k", event_id="e").digest())
    check("digest_matches_manual",
          RepoRouteKey(repo_id="r", thread_id="t", task_id="k", event_id="e").digest()
          == hashlib.sha256(b"r:t:k:e").hexdigest()[:32])
    check("short_id_len_12", len(RepoRouteKey(repo_id="r", thread_id="t", task_id="k").short_id()) == 12)

    check("cross_repo_disjoint_eq",
          RepoRouteKey(repo_id="repo-a", thread_id="th-1", task_id="tk-1", event_id="ev-1")
          != RepoRouteKey(repo_id="repo-b", thread_id="th-1", task_id="tk-1", event_id="ev-1"))
    check("cross_repo_disjoint_hash",
          hash(RepoRouteKey(repo_id="repo-a", thread_id="th-1", task_id="tk-1", event_id="ev-1"))
          != hash(RepoRouteKey(repo_id="repo-b", thread_id="th-1", task_id="tk-1", event_id="ev-1")))
    check("cross_repo_disjoint_digest",
          RepoRouteKey(repo_id="repo-a", thread_id="th-1", task_id="tk-1").digest()
          != RepoRouteKey(repo_id="repo-b", thread_id="th-1", task_id="tk-1").digest())
    check("cross_repo_disjoint_canonical",
          RepoRouteKey(repo_id="repo-a", thread_id="th-1", task_id="tk-1", event_id="ev-1").canonical()
          != RepoRouteKey(repo_id="repo-b", thread_id="th-1", task_id="tk-1", event_id="ev-1").canonical())
    check("same_repo_diff_thread_disjoint",
          RepoRouteKey(repo_id="r", thread_id="th-1", task_id="tk-1", event_id="ev-1").digest()
          != RepoRouteKey(repo_id="r", thread_id="th-2", task_id="tk-1", event_id="ev-1").digest())
    check("same_repo_diff_task_disjoint",
          RepoRouteKey(repo_id="r", thread_id="th-1", task_id="tk-1", event_id="ev-1").digest()
          != RepoRouteKey(repo_id="r", thread_id="th-1", task_id="tk-2", event_id="ev-1").digest())
    check("same_repo_diff_event_disjoint",
          RepoRouteKey(repo_id="r", thread_id="th-1", task_id="tk-1", event_id="ev-1").digest()
          != RepoRouteKey(repo_id="r", thread_id="th-1", task_id="tk-1", event_id="ev-2").digest())

    ke1 = RepoRouteKey(repo_id="r", thread_id="t", task_id="k", event_id="e")
    ke2 = RepoRouteKey(repo_id="r", thread_id="t", task_id="k", event_id="e")
    check("idempotent_equality", ke1 == ke2)
    check("idempotent_hash", hash(ke1) == hash(ke2))
    check("idempotent_set", len({ke1, ke2}) == 1)
    d = {}
    d[ke1] = 1
    d[ke2] = 2
    check("idempotent_dict_key", len(d) == 1 and d[ke1] == 2)

    check("legacy_missing_repo", raises_value_error(lambda: fail_on_legacy_thread_only(thread_id="t", task_id="k"), "repo_id is required"))
    check("legacy_missing_task", raises_value_error(lambda: fail_on_legacy_thread_only(thread_id="t", repo_id="r"), "task_id is required"))
    check("legacy_both_missing", raises_value_error(lambda: fail_on_legacy_thread_only(thread_id="t"), "repo_id is required"))
    kl = fail_on_legacy_thread_only(thread_id="t", repo_id="r", task_id="k")
    check("legacy_succeeds", kl.repo_id == "r" and kl.thread_id == "t" and kl.task_id == "k" and kl.event_id == "")
    kle = fail_on_legacy_thread_only(thread_id="t", repo_id="r", task_id="k", event_id="e")
    check("legacy_with_event", kle.event_id == "e")

    ko = RepoRouteKey(repo_id="r", thread_id="t", task_id="k", event_id="e")
    check("dict_round_trip", RepoRouteKey.from_dict(ko.as_dict()) == ko)
    jstr = json.dumps(ko.as_dict())
    jd = json.loads(jstr)
    check("json_round_trip", RepoRouteKey.from_dict(jd) == ko)

    check("dedup_key_format", ko.to_legacy_dedup_key("review_ready", "ep1") == "k:review_ready:t:ep1")
    cid_a = RepoRouteKey(repo_id="repo-a", thread_id="t", task_id="k", event_id="e").to_client_user_message_id("review_ready", "ep1")
    cid_b = RepoRouteKey(repo_id="repo-b", thread_id="t", task_id="k", event_id="e").to_client_user_message_id("review_ready", "ep1")
    check("client_msg_id_repo_scoped", cid_a != cid_b)
    check("client_msg_id_prefix", cid_a.startswith("cbmsg_") and len(cid_a) == 38)

    with tempfile.TemporaryDirectory() as tmp:
        repo_base = Path(tmp) / "repo"
        repo_base.mkdir()
        (repo_base / "subdir").mkdir()
        (repo_base / "subdir" / "file.txt").write_text("hello")
        key = RepoRouteKey(repo_id="r", thread_id="t", task_id="k")

        p = key.repo_scoped_path(repo_base, "subdir/file.txt")
        check("path_within_repo", p.is_absolute() and p.exists())
        check("path_relative", key.repo_scoped_relative(repo_base, "subdir") == "subdir")
        safe = key.repo_scoped_safe_repr(repo_base, "subdir/file.txt")
        check("safe_repr_no_abs_path", tmp not in safe and str(repo_base) not in safe)
        check("safe_repr_format", safe == "<repo_root>/subdir/file.txt")

        outside = Path(tmp) / "outside"
        outside.mkdir()
        (repo_base / "link").symlink_to(outside)
        check("symlink_escape", raises_value_error(lambda: key.repo_scoped_path(repo_base, "link")))

        check("path_rejects_parent", raises_value_error(lambda: key.repo_scoped_path(repo_base, "../escape")))
        check("path_rejects_absolute", raises_value_error(lambda: key.repo_scoped_path(repo_base, "/etc/passwd")))
        check("path_rejects_drive", raises_value_error(lambda: key.repo_scoped_path(repo_base, "C:evil")))
        check("path_rejects_null", raises_value_error(lambda: key.repo_scoped_path(repo_base, "sub\x00evil")))
        check("path_rejects_dot", raises_value_error(lambda: key.repo_scoped_path(repo_base, "./hidden")))
        check("path_rejects_empty", raises_value_error(lambda: key.repo_scoped_path(repo_base, "")))

    check("rejects_non_string", raises_value_error(lambda: RepoRouteKey(repo_id=42, thread_id="t", task_id="k")))

    total = results["pass"] + results["fail"]
    print(f"\n=== Results: {results['pass']}/{total} PASS, {results['fail']} FAIL ===")
    return 0 if results["fail"] == 0 else 1


if __name__ == "__main__":
    if _HAS_PYTEST:
        print("pytest available -- use: python3 -m pytest ...")
    sys.exit(_run_standalone())
