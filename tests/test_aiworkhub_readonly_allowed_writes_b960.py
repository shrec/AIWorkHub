"""Readonly/canary cards may declare allowed_writes: [] on purpose.

The verification must distinguish a MISSING allowed_writes key (genuine
under-specification) from an intentionally-EMPTY readonly list (a canary /
no-output task). An empty list is accepted when the card declares no
required_outputs; it is rejected only when the card DOES declare outputs but
no write scope. No NO_WRITES sentinel value is required anywhere.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher as pl  # noqa: E402
from aiworkhub import review_summarizer  # noqa: E402


# --- launch-preflight scope validation -------------------------------------

def test_validate_scope_readonly_empty_allowed_writes_is_accepted(tmp_path):
    # allowed_writes: [] with no required_outputs -> readonly/canary, accepted.
    pl._validate_scope(tmp_path, {"allowed_writes": [], "required_outputs": None})
    pl._validate_scope(tmp_path, {"allowed_writes": []})  # required_outputs absent too


def test_validate_scope_empty_allowed_with_required_outputs_is_rejected(tmp_path):
    with pytest.raises(pl.LaunchRejected) as exc:
        pl._validate_scope(tmp_path, {"allowed_writes": [], "required_outputs": ["out/x.json"]})
    assert "allowed_writes_empty" in str(exc.value)


def test_validate_scope_missing_key_is_a_distinct_error(tmp_path):
    with pytest.raises(pl.LaunchRejected) as exc:
        pl._validate_scope(tmp_path, {})  # no allowed_writes key at all
    assert "allowed_writes_missing" in str(exc.value)


def test_validate_scope_non_list_is_invalid(tmp_path):
    with pytest.raises(pl.LaunchRejected) as exc:
        pl._validate_scope(tmp_path, {"allowed_writes": "out/x"})
    assert "allowed_writes_invalid" in str(exc.value)


def test_validate_scope_populated_allowed_writes_is_accepted(tmp_path):
    pl._validate_scope(tmp_path, {"allowed_writes": ["out/result.txt"]})


# --- review-summarizer risk derivation -------------------------------------

def _codes(card) -> set[str]:
    return {r["code"] for r in review_summarizer._derive_risks(card)}


def test_derive_risks_readonly_empty_not_flagged():
    card = {"allowed_writes": [], "validation": ["run"], "required_outputs": []}
    assert "missing_allowed_writes" not in _codes(card)


def test_derive_risks_absent_allowed_writes_is_flagged():
    assert "missing_allowed_writes" in _codes({"validation": ["run"]})


def test_derive_risks_empty_with_required_outputs_is_flagged():
    card = {"allowed_writes": [], "validation": ["run"], "required_outputs": ["out/x"]}
    assert "missing_allowed_writes" in _codes(card)
