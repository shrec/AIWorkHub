"""NF-WAVE-SG-TARGET-TRUTH: a symbol target must RESOLVE the symbol.

These regressions drive the MCP surface (``worker_ai_tools_mcp.source_graph_query``)
rather than ``source_graph`` directly, which is why they catch a defect every
existing Source Graph test missed: on canonical v0.9.92 the engine finds the
symbol but the wrapper passes the free-text ``query`` (not the selector) to the
engine and then re-applies a PATH-PREFIX filter using the qualname ``target``.
A file path never starts with a qualname, so the resolved match is always
dropped and a ``body``/``function`` call for an indexed symbol returns
``hit_count == 0`` with ``freshness == "fresh"`` -- an impossible pairing that
is the fingerprint of a filter applied AFTER retrieval.

Each test states the behaviour before/after so it can be read against the
acceptance contract:

* body/function with a qualname target now resolve the symbol WITH its source
  (before: empty result).
* the path-prefix filter is no longer applied to selector modes.
* a resolved symbol whose file is outside the allowlist is refused BY NAME,
  never returned as an empty "no such symbol" result.
* a ``body``/``function`` target that names no symbol is still a PATH SCOPE, so
  the pre-existing declared-target behaviour is unchanged.
* ``semantic_edit.path_is_allowed`` no longer raises on a ``None`` allowlist.
"""

from __future__ import annotations

import json

import pytest

from aiworkhub import semantic_edit
from aiworkhub import source_graph as sg
from aiworkhub import worker_ai_tools_mcp as w
from aiworkhub.repository_state import bootstrap_repository


def _new_repo(tmp_path, name="repo"):
    root = tmp_path / name
    root.mkdir()
    bootstrap_repository(root, repo_name=name)
    return root


def _write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ctx(repo, *, targets, task_id, request_id):
    return w.WorkerToolContext(
        task_id=task_id,
        runner="r",
        topic="topic",
        request_id=request_id,
        repo=repo,
        authority_repo=repo,
        source_graph_targets=targets,
        session_topic="topic",
        audit_ledger_path=None,
        audit_hmac_key_path=None,
    )


# ---------------------------------------------------------------------------
# The symbol is indexed, the engine finds it -- the MCP tool must too.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["body", "function"])
def test_qualname_target_resolves_symbol_with_source(tmp_path, mode):
    repo = _new_repo(tmp_path)
    _write(
        repo,
        "pkg/economy.py",
        "def receipt_conformance_report():\n"
        "    return 'conformance'\n",
    )
    sg.build_index(repo, incremental=True)

    # Baseline: the engine resolves the symbol directly.
    engine = sg.body_query(repo, "receipt_conformance_report", 16)
    assert engine["matches"], "the symbol must be indexed for this regression"

    qualname = "pkg/economy.py.receipt_conformance_report"
    # A manager query carries no restrictive target allowlist (unrestricted).
    ctx = _ctx(repo, targets=(), task_id=f"t-{mode}", request_id=f"req-{mode}")
    w._CACHE.clear()

    result = w.source_graph_query(
        ctx, mode=mode, query="receipt_conformance_report", target=qualname, budget=16,
    )

    # Before: hit_count == 0 with freshness "fresh" (filter applied post-retrieval).
    # After: the selector is passed to the engine and the result is not re-filtered.
    assert result["ok"] is True, result
    assert result["hit_count"] >= 1
    assert result["target"] == qualname
    payload = json.loads(result["content"])
    # The path-prefix filter is no longer applied to a selector mode.
    assert payload.get("scope") == "target_selector"
    match = payload["matches"][0]
    assert match["file_path"] == "pkg/economy.py"
    # The indexed symbol comes back WITH its source, not an empty shell.
    assert "receipt_conformance_report" in match["source"]
    assert "return 'conformance'" in match["source"]


# ---------------------------------------------------------------------------
# The security boundary: a resolved symbol out of scope is refused BY NAME.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["body", "function"])
def test_symbol_outside_allowlist_is_refused_by_name(tmp_path, mode):
    repo = _new_repo(tmp_path)
    _write(repo, "pkg/allowed.py", "def allowed_symbol():\n    return 1\n")
    _write(repo, "pkg/secret.py", "def hidden_symbol():\n    return 2\n")
    sg.build_index(repo, incremental=True)

    qualname = "pkg/secret.py.hidden_symbol"
    # The caller may NAME the selector (it passes the target-membership gate),
    # but the file allowlist covers only pkg/allowed.py.
    ctx = _ctx(
        repo,
        targets=("pkg/allowed.py", qualname),
        task_id=f"t-refuse-{mode}",
        request_id=f"req-refuse-{mode}",
    )
    w._CACHE.clear()

    result = w.source_graph_query(
        ctx, mode=mode, query="hidden_symbol", target=qualname, budget=16,
    )

    # Not an empty success that reads as "no such symbol" -- a named refusal.
    assert result["ok"] is False, result
    reason = str(result.get("reason") or "")
    assert reason.startswith("symbol_out_of_scope")
    assert "hidden_symbol" in reason  # the caller learns the symbol exists
    assert "pkg/secret.py" in reason  # ...and which out-of-scope file holds it


# ---------------------------------------------------------------------------
# Existing target-scoped (path) behaviour for body/function is unchanged: a
# target that names no symbol is a PATH SCOPE, not a selector.
# ---------------------------------------------------------------------------

def test_file_path_target_is_still_a_path_scope(tmp_path):
    repo = _new_repo(tmp_path)
    _write(repo, "pkg/status.py", "def DBAccountStatus():\n    return 'ok'\n")
    sg.build_index(repo, incremental=True)
    ctx = _ctx(
        repo, targets=("pkg/status.py",), task_id="t-path", request_id="req-path",
    )
    w._CACHE.clear()

    # target is a FILE (not a symbol) -> path scope; the symbol comes from query.
    result = w.source_graph_query(
        ctx, mode="body", query="DBAccountStatus", target="pkg/status.py", budget=16,
    )
    assert result["ok"] is True
    payload = json.loads(result["content"])
    # A path-scoped call is marked "target", never "target_selector".
    assert payload.get("scope") == "target"
    assert any(m["file_path"] == "pkg/status.py" for m in payload["matches"])


def test_file_path_target_recovers_symbol_from_a_second_declared_file(tmp_path):
    repo = _new_repo(tmp_path)
    _write(repo, "pkg/orientation.py", "def orientation_probe():\n    return 1\n")
    _write(repo, "pkg/status.py", "def DBAccountStatus():\n    return 'ok'\n")
    sg.build_index(repo, incremental=True)
    ctx = _ctx(
        repo,
        targets=("pkg/orientation.py", "pkg/status.py"),
        task_id="t-fallback",
        request_id="req-fallback",
    )
    w._CACHE.clear()

    # The declared-target rescue still recovers a symbol that resolves in a
    # second allowlisted file (unchanged from canonical behaviour).
    result = w.source_graph_query(
        ctx, mode="body", query="DBAccountStatus", target="pkg/orientation.py", budget=16,
    )
    assert result["ok"] is True
    payload = json.loads(result["content"])
    assert payload["scope"] == "declared_target_fallback"
    assert payload["requested_target"] == "pkg/orientation.py"
    assert any(m["file_path"] == "pkg/status.py" for m in payload["matches"])


# ---------------------------------------------------------------------------
# path_is_allowed no longer raises TypeError on a missing allowlist.
# ---------------------------------------------------------------------------

def test_path_is_allowed_tolerates_missing_allowlist():
    # Before: `for pattern in None` raised TypeError. After: no allowlist
    # authorizes nothing, returned as a plain False.
    assert semantic_edit.path_is_allowed("pkg/mod.py", None) is False
    assert semantic_edit.path_is_allowed("pkg/mod.py", ()) is False
    # A configured allowlist still matches exactly and by glob.
    assert semantic_edit.path_is_allowed("pkg/mod.py", ("pkg/mod.py",)) is True
    assert semantic_edit.path_is_allowed("pkg/mod.py", ("pkg/*.py",)) is True


# ---------------------------------------------------------------------------
# Rework: both selector-scope boundaries obey ONE rule and fail closed on a
# match that carries no attributable file_path.  Before this change the
# enforcement gate read ``if not file_path or ...`` (an unattributable match was
# IN scope -- fail-open) while the sibling declared-target fallback read
# ``if file_path and ...`` (fail-closed): the same boundary, opposite answers.
# ---------------------------------------------------------------------------

def test_selector_scope_rule_is_stated_once_and_fails_closed():
    # The single per-match rule both boundaries read.  An unattributable match
    # is OUT of scope regardless of the allowlist -- never fail-open.
    assert w._selector_match_in_scope("", ("pkg/x.py",)) is False
    assert w._selector_match_in_scope("", ()) is False
    # An attributable file is in scope only when the allowlist covers it; the
    # per-match rule itself grants nothing on an empty allowlist (the
    # unrestricted grant lives in the caller, mirroring the target gate).
    assert w._selector_match_in_scope("pkg/x.py", ("pkg/x.py",)) is True
    assert w._selector_match_in_scope("pkg/x.py", ("pkg/y.py",)) is False
    assert w._selector_match_in_scope("pkg/x.py", ()) is False


def test_enforce_selector_allowlist_refuses_unattributable_match_by_name(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    ctx = _ctx(repo, targets=("pkg/allowed.py",), task_id="t-ghost", request_id="req-ghost")

    # A resolved payload whose only match cannot be attributed to a file.  The
    # old fail-open branch returned it as an ordinary in-scope hit; now it is
    # refused BY NAME like any other out-of-scope symbol.
    payload = {
        "matches": [{"name": "ghost", "qualname": "pkg/ghost.py.ghost", "file_path": ""}],
        "candidate_files": [],
    }
    violation, returned = w._enforce_selector_allowlist(ctx, "source_graph", payload, ("pkg/allowed.py",))
    assert violation is not None, "an unattributable match must not be returned in scope"
    assert violation["ok"] is False
    reason = str(violation.get("reason") or "")
    assert reason.startswith("symbol_out_of_scope")
    assert "ghost" in reason  # refused by name, never an empty "no such symbol"

    # The unrestricted grant is applied ONCE in the caller: with no allowlist
    # there is nothing to enforce and the payload passes through untouched.
    grant, grant_payload = w._enforce_selector_allowlist(ctx, "source_graph", payload, ())
    assert grant is None
    assert grant_payload is payload


def test_declared_target_fallback_stays_fail_closed_on_unattributable_match(tmp_path, monkeypatch):
    # Drive the MCP surface through the sibling boundary (the declared-target
    # rescue) and prove it also refuses an unattributable match -- so the two
    # boundaries agree.  The engine schema never emits an empty file_path, so a
    # stubbed engine is the only way to reach the empty-path case at all.
    repo = _new_repo(tmp_path)
    _write(repo, "pkg/real.py", "def anchor():\n    return 1\n")
    sg.build_index(repo, incremental=True)

    def fake_body_query(_repo, query, _budget):
        if query == "ghosthunt":
            # A match the wrapper cannot attribute to any file.
            return {
                "matches": [{"name": "ghost", "qualname": "ghost", "file_path": "", "source": "x"}],
                "candidate_files": [],
            }
        return {"matches": [], "candidate_files": []}

    monkeypatch.setattr(sg, "body_query", fake_body_query)
    ctx = _ctx(repo, targets=("pkg/real.py",), task_id="t-fc", request_id="req-fc")
    w._CACHE.clear()

    # target is a FILE (path scope, resolves no symbol) -> the free-text query is
    # searched and its zero-scope result triggers the declared-target fallback.
    result = w.source_graph_query(
        ctx, mode="body", query="ghosthunt", target="pkg/real.py", budget=16,
    )
    assert result["ok"] is True, result
    # The unattributable match is dropped, never broadened in -- same answer the
    # enforcement gate now gives.  Probe the STRUCTURE, not a substring of the
    # whole serialized payload: the payload echoes the query token "ghosthunt"
    # back as retrieval provenance, and "ghost" is a prefix of it, so a substring
    # test would match the query we sent -- not a leaked row.  Decode and assert
    # no match survived.
    assert result["hit_count"] == 0
    payload = json.loads(result["content"])
    assert payload["matches"] == []
    assert all(m.get("qualname") != "ghost" for m in payload["matches"])
