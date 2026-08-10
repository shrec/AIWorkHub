from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/case-studies/ultrafast-secp256k1-evidence-v1.json"
DOCUMENT = ROOT / "docs/case-studies/ULTRAFAST_SECP256K1.md"


def test_ultrafast_case_study_uses_only_commit_pinned_sources() -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    commit = payload["commit"]
    assert re.fullmatch(r"[0-9a-f]{40}", commit)
    assert payload["measurement_boundary"] == (
        "pinned_public_repository_documentation_not_independent_runtime_reproduction"
    )
    assert len(payload["sources"]) >= 5
    for source in payload["sources"]:
        assert re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
        assert not str(source["path"]).startswith(("/", "../"))

    document = DOCUMENT.read_text(encoding="utf-8")
    assert commit in document
    assert "No adoption statement is independently verified here." in document
    assert "No performance multiplier from the donor is an AIWorkHub benchmark." in document
    assert "no external third-party audit has been completed" in " ".join(
        document.split()
    )


def test_ultrafast_case_study_manifest_records_claim_exclusions() -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    exclusions = "\n".join(payload["excluded_claims"])
    assert "independently reproduced" in exclusions
    assert "independently verified adoption" in exclusions
    assert "independent cryptographic security audit" in exclusions
