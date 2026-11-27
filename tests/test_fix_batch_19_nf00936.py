"""Fix batch 19 regressions: NF-2026-00936, -00944, -00952 (-00941 deferred)."""

from __future__ import annotations

import copy
import errno
import importlib.util
from pathlib import Path

import pytest

from aiworkhub import output_spill_store
from aiworkhub import quality_calibration as qc
from aiworkhub import quality_evidence as qe


# ── NF-2026-00936: sealed adjudicated defect/clean review corpus ───────────
def test_review_corpus_is_sealed_and_measures_per_lens_tp_fp_fn() -> None:
    report = qc.run_review_corpus_calibration()

    assert report["seal_intact"] is True
    assert report["corpus_sha256"] == qc.REVIEW_CORPUS_SHA256
    assert report["ok"] is True
    assert report["invalid_candidates"] == []
    assert report["defect_count"] > 0 and report["clean_count"] > 0
    lenses = report["lenses"]
    assert set(lenses) == set(qe.JUDGMENT_LENSES)
    assert all(metrics["state"] == qc.LENS_STATE_MEASURED for metrics in lenses.values())
    # The recorded reviewer misses one adjudicated correctness defect and
    # flags one adjudicated clean code-quality change: both are measured.
    assert lenses[qe.LENS_CORRECTNESS]["fn"] == 1
    assert lenses[qe.LENS_CORRECTNESS]["tp"] == 2
    assert lenses[qe.LENS_CODE_QUALITY]["fp"] == 1
    assert lenses[qe.LENS_SECURITY]["tp"] == 2
    assert lenses[qe.LENS_SECURITY]["fp"] == 0
    assert lenses[qe.LENS_CORRECTNESS]["recall"] == pytest.approx(2 / 3)
    # run_calibration carries the corpus result and depends on it.
    full = qc.run_calibration()
    assert full["review_corpus"]["ok"] is True
    assert full["ok"] is True


def test_review_corpus_tamper_and_undersampling_read_unknown_not_pass() -> None:
    tampered = copy.deepcopy(list(qc.REVIEW_CORPUS))
    tampered[0]["adjudication"] = qc.ADJUDICATION_CLEAN
    broken = qc.run_review_corpus_calibration(tampered)
    assert broken["seal_intact"] is False
    assert broken["ok"] is False
    assert sorted(broken["unknown_lenses"]) == sorted(qe.JUDGMENT_LENSES)

    only_security = [row for row in qc.REVIEW_CORPUS if row["lens"] == qe.LENS_SECURITY]
    sparse = qc.run_review_corpus_calibration(
        only_security, sealed_sha256=qc.corpus_digest(only_security)
    )
    assert sparse["seal_intact"] is True
    assert sparse["ok"] is False
    assert sparse["lenses"][qe.LENS_SECURITY]["state"] == qc.LENS_STATE_MEASURED
    for lens in (qe.LENS_CORRECTNESS, qe.LENS_CODE_QUALITY):
        assert sparse["lenses"][lens]["state"] == qc.UNKNOWN
        assert sparse["lenses"][lens]["recall"] == qc.UNKNOWN
        assert sparse["lenses"][lens]["precision"] == qc.UNKNOWN


# ── NF-2026-00944: spill persist failure keeps the OS cause ────────────────
def test_spill_persist_failure_names_exception_class_and_errno(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AIWORKHUB_WORKER_SPILL_ROOT", raising=False)
    secret_path = str(tmp_path / "secret-spill-path")

    def _denied(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", secret_path)

    monkeypatch.setattr(output_spill_store.tempfile, "mkstemp", _denied)

    with pytest.raises(output_spill_store.OutputSpillError) as caught:
        output_spill_store.spill_text("payload", repo=tmp_path)
    message = str(caught.value)
    assert message.startswith("output_spill_store_persist_failed:")
    assert message.endswith(":PermissionError:EACCES")
    assert secret_path not in message
    assert isinstance(caught.value.__cause__, PermissionError)

    outcome = output_spill_store.try_spill_text("payload", repo=tmp_path)
    assert isinstance(outcome, output_spill_store.SpillUnavailable)
    assert outcome.detail.endswith(":PermissionError:EACCES")


# ── NF-2026-00952: TS/JS enrichment test skips with a typed prerequisite ──
def _lsp_module():
    path = Path(__file__).with_name("test_source_graph_lsp_integration.py")
    spec = importlib.util.spec_from_file_location("_nf952_lsp_integration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ts_js_prerequisite_probe_names_the_missing_prerequisite(tmp_path, monkeypatch) -> None:
    lsp = _lsp_module()
    fixture = lsp._Lsp(tmp_path, monkeypatch, "nf952")
    fixture.write("util.ts", "export function helper(): number {\n  return 1;\n}\n")
    fixture.index("util.ts")

    monkeypatch.setattr(lsp.sg, "_lsp_configured_languages", lambda: ())
    assert (
        lsp.ts_js_enrichment_prerequisite_missing(fixture)
        == "server_group_not_configured:typescript"
    )

    monkeypatch.setattr(
        lsp.sg, "_lsp_configured_languages", lambda: ("typescript", "javascript")
    )
    # Server present: an unindexed TS/JS fixture is an extractor regression
    # and must FAIL with a named assertion, never skip.
    assert lsp.ts_js_enrichment_prerequisite_missing(fixture) == ""
    with pytest.raises(
        AssertionError, match="extractor_language_unavailable:app.ts:unindexed"
    ):
        lsp.assert_ts_js_extractor_output(fixture)
    assert lsp.TS_JS_PREREQUISITE_SKIP_PREFIX == "lsp_ts_js_prerequisite_absent:"
