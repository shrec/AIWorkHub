"""Contract tests for the RM-2026-00016 external qualification foundation.

The corpus pins are real: the fixtures below are the checked-in
``external_qualification.FIXED_CORPUS_ENTRIES``, whose commit ids were read from
each repository's own remote ref advertisement. No test in this file contacts,
clones, builds or executes an external repository -- the pins are exercised as
data, never resolved over the network here. Every other revision and digest is
synthetic and locally derived.

The acceptance fixtures are deliberately *not* synthetic in shape: they are the
real ``aiworkhub.accepted_outcome_receipt.v1`` identity, built by the canonical
projection ``process_launcher_acceptance.accepted_outcome_receipt`` and checked
against the canonical authority ``task_engine._validate_accepted_outcome_receipt``,
so a look-alike receipt cannot be projected as an accepted outcome.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from aiworkhub import external_qualification as eq  # noqa: E402
from aiworkhub import process_launcher_acceptance, task_engine  # noqa: E402

_PROTOCOL_DOC = (
    Path(__file__).resolve().parents[1] / "docs" / "qualification" / "EXTERNAL_CORPUS_PROTOCOL.md"
)

REQUEST_ID = "req-0000000000000001"
TASK_ID = "AIWORKHUB_01130_RM16_EXTERNAL_QUALIFICATION_CORPUS_FOUNDATION_V1_OPUS5"
CLAIM_EPOCH = 7
BASE_OID = "9" * 40

# Synthetic promoted content: relative paths in a throwaway checkout, never an
# external repository and never the running one.
PROMOTED_CONTENT: dict[str, str] = {
    "docs/qualification/EXTERNAL_CORPUS_PROTOCOL.md": "synthetic promoted protocol\n",
    "src/aiworkhub/external_qualification.py": "synthetic promoted module\n",
}
PROMOTED_PATHS = sorted(PROMOTED_CONTENT)
CHANGED_PATH_HASHES = {
    path: hashlib.sha256(PROMOTED_CONTENT[path].encode("utf-8")).hexdigest()
    for path in PROMOTED_PATHS
}
ATTEMPT_MANIFEST: dict[str, Any] = {
    "schema_id": "aiworkhub.attempt_artifact_manifest.v1",
    "request_id": REQUEST_ID,
    "entries": [],
}


def _oid(seed: str) -> str:
    """Deterministic synthetic 40-hex commit id; no external repository is contacted."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:40]


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _evidence(seed: str, locator: str) -> dict[str, Any]:
    return {
        "locator": locator,
        "sha256": _digest(seed),
        "media_type": "application/json",
        "byte_count": 4096,
    }


def _entries() -> list[dict[str, Any]]:
    """The real checked-in corpus entries, deep-copied so a test may mutate one.

    The negative tests below mutate these rather than a parallel synthetic
    fixture, so every rule they prove is a rule the shipped corpus satisfies.
    """
    return [
        dict(entry, pin_evidence=dict(entry["pin_evidence"]))
        for entry in eq.FIXED_CORPUS_ENTRIES
    ]


def _manifest(**overrides: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_id": eq.CORPUS_MANIFEST_SCHEMA,
        "corpus_version": eq.CORPUS_VERSION,
        "entries": _entries(),
    }
    manifest.update(overrides)
    return manifest


def _promoted_repo(root: Path) -> Path:
    """Materialize the synthetic promoted checkout the receipt is built over."""
    for relative, content in PROMOTED_CONTENT.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # write_bytes, not write_text: CHANGED_PATH_HASHES below is computed
        # from these exact '\n'-only strings, and Path.write_text translates
        # '\n' to CRLF on Windows, inflating the real on-disk bytes past what
        # the fixture's pre-baked hash expects.
        path.write_bytes(content.encode("utf-8"))
    return root


def _sealed_card() -> dict[str, Any]:
    """The sealed terminal evidence the canonical authority binds a receipt to."""
    return {
        "claim_epoch": CLAIM_EPOCH,
        "terminal_review": {
            "evidence": {
                "changed_paths": list(PROMOTED_PATHS),
                "changed_path_hashes": dict(CHANGED_PATH_HASHES),
                "attempt_artifact_manifest": ATTEMPT_MANIFEST,
                "workspace": {"base_oid": BASE_OID},
            }
        },
    }


def _receipt(*, drop: tuple[str, ...] = (), **overrides: Any) -> dict[str, Any]:
    """Build the canonical receipt, then reseal it around whatever was changed.

    Resealing is what makes the negative tests precise: a receipt missing
    ``claim_epoch`` must fail on the canonical field set, not incidentally on a
    stale ``receipt_id``. ``receipt_id`` itself is only left wrong when a test
    overrides it on purpose.
    """
    body: dict[str, Any] = {
        "schema_id": task_engine.ACCEPTED_OUTCOME_RECEIPT_SCHEMA,
        "task_id": TASK_ID,
        "request_id": REQUEST_ID,
        "claim_epoch": CLAIM_EPOCH,
        "base_oid": BASE_OID,
        "promoted_paths": list(PROMOTED_PATHS),
        "changed_path_hashes": dict(CHANGED_PATH_HASHES),
        "attempt_artifact_manifest_id": task_engine._canonical_json_hash(ATTEMPT_MANIFEST),
    }
    body["repository_revision"] = "sha256:" + task_engine._canonical_json_hash(
        {"base_oid": body["base_oid"], "changed_path_hashes": body["changed_path_hashes"]}
    )
    body.update(overrides)
    for key in drop:
        body.pop(key, None)
    body.pop("receipt_id", None)
    if "receipt_id" not in drop:
        body["receipt_id"] = "sha256:" + task_engine._canonical_json_hash(body)
    if "receipt_id" in overrides:
        body["receipt_id"] = overrides["receipt_id"]
    return body


def _artifact(
    normalized: dict[str, Any], corpus_id: str = "cpp-llvm-project", **overrides: Any
) -> dict[str, Any]:
    entry = next(item for item in normalized["entries"] if item["corpus_id"] == corpus_id)
    artifact: dict[str, Any] = {
        "schema_id": eq.RUN_ARTIFACT_SCHEMA,
        "corpus_manifest_id": normalized["corpus_manifest_id"],
        "corpus_version": normalized["corpus_version"],
        "corpus_id": entry["corpus_id"],
        "profile": entry["profile"],
        "repository_url": entry["repository_url"],
        "repository_revision": entry["repository_revision"],
        "aiworkhub_revision": _oid("aiworkhub-candidate"),
        "route": "claude_opus-5",
        "model": "claude-opus-5",
        "task_id": TASK_ID,
        "request_id": REQUEST_ID,
        "attempt_id": "attempt-0000000000000001",
        "retries": 1,
        "validation": {
            "state": "pass",
            "evidence": [
                _evidence("validation-pytest", "ci://run/1/validation-pytest.json"),
                _evidence("validation-ruff", "ci://run/1/validation-ruff.json"),
            ],
        },
        "review": {
            "state": "pass",
            "evidence": [_evidence("review", "ci://run/1/review.json")],
        },
        "usage": {
            "input_tokens": 120_000,
            "output_tokens": 9_000,
            "total_tokens": 129_000,
            "cost_usd": 1.25,
        },
        "platform_claims": ["linux", "macos", "windows"],
        "platform_evidence": {
            "macos": _evidence("macos", "ci://run/1/macos/report.json"),
            "windows": _evidence("windows", "ci://run/1/windows/report.json"),
        },
        "outcome": eq.OUTCOME_ACCEPTED,
        "outcome_reason": "manager_accepted_after_independent_review",
        "accepted_outcome_receipt": _receipt(),
        "receipt_authentication": {
            "state": "pass",
            "evidence": [_evidence("acceptance", "ci://run/1/accepted-outcome-receipt.json")],
        },
    }
    artifact.update(overrides)
    # Authentication travels with the receipt: a row carrying no receipt has
    # nothing to authenticate, so the block leaves with it.
    if artifact["accepted_outcome_receipt"] is None and "receipt_authentication" not in overrides:
        artifact["receipt_authentication"] = None
    return artifact


@pytest.fixture()
def normalized_manifest() -> dict[str, Any]:
    return eq.validate_corpus_manifest(_manifest())


@pytest.fixture()
def authority(tmp_path: Path) -> Any:
    """The real canonical acceptance authority, bound to a synthetic checkout.

    Shared by every test that needs an *authenticated* acceptance, so the
    difference between an authenticated row and a merely-claimed one is one
    argument rather than a reimplementation.
    """
    return eq.canonical_acceptance_authority(
        _promoted_repo(tmp_path), _sealed_card(), task_id=TASK_ID, request_id=REQUEST_ID
    )


# --------------------------------------------------------------------------
# Corpus manifest
# --------------------------------------------------------------------------


def test_manifest_requires_all_four_heterogeneous_profiles(normalized_manifest):
    assert {entry["profile"] for entry in normalized_manifest["entries"]} == eq.REQUIRED_PROFILES
    assert normalized_manifest["required_profiles"] == sorted(eq.REQUIRED_PROFILES)

    for dropped in range(4):
        entries = _entries()
        removed = entries.pop(dropped)
        with pytest.raises(eq.InvalidCorpusError, match="required profiles not covered"):
            eq.validate_corpus_manifest(_manifest(entries=entries))
        assert removed["profile"] in eq.REQUIRED_PROFILES


def test_profile_requirements_cover_every_required_profile():
    assert set(eq.PROFILE_REQUIREMENTS) == eq.REQUIRED_PROFILES
    pinned = {entry["profile"] for entry in eq.FIXED_CORPUS_ENTRIES}
    for profile, requirement in eq.PROFILE_REQUIREMENTS.items():
        assert requirement["summary"], profile
        assert requirement["must_demonstrate"], profile
        assert profile in pinned, profile


def test_manifest_requires_pin_evidence_for_every_entry():
    """A revision nobody observed is a fabricated pin, so the evidence is mandatory."""
    entries = _entries()
    entries[0] = {key: value for key, value in entries[0].items() if key != "pin_evidence"}
    with pytest.raises(eq.InvalidCorpusError, match="pin evidence mapping required"):
        eq.validate_corpus_manifest(_manifest(entries=entries))


def test_pin_locator_must_resolve_the_pinned_revision():
    entries = _entries()
    entries[0] = dict(
        entries[0],
        pin_evidence=dict(
            entries[0]["pin_evidence"],
            locator=f"{entries[0]['repository_url']}/commit/{_oid('some-other-commit')}",
        ),
    )
    with pytest.raises(eq.InvalidCorpusError, match="must resolve the pinned revision"):
        eq.validate_corpus_manifest(_manifest(entries=entries))


def test_pin_locator_must_point_into_the_pinned_repository():
    entries = _entries()
    entries[0] = dict(
        entries[0],
        pin_evidence=dict(
            entries[0]["pin_evidence"],
            locator=(
                "https://github.com/someone/elsewhere/commit/"
                f"{entries[0]['repository_revision']}"
            ),
        ),
    )
    with pytest.raises(eq.InvalidCorpusError, match="must point into"):
        eq.validate_corpus_manifest(_manifest(entries=entries))


def test_uppercase_revision_locator_and_ref_object_id_normalize_to_one_pin():
    """A git oid is case-insensitive, so one commit must not become two pins.

    Every field carrying an oid has to fold in the same order -- normalize, validate,
    compare, seal -- so the three are spelled in uppercase together here: the pinned
    revision, the locator tail that has to resolve it, and the ref object id the
    observation read. Folding only some of them rejects an entry against its own
    matching evidence, and the identical pin would otherwise seal two digests.
    """
    entries = _entries()
    entry = entries[0]
    revision = entry["repository_revision"]
    pin = entry["pin_evidence"]
    locator = pin["locator"]
    ref_object_id = pin["observed_ref_object_id"]
    assert locator.endswith("/" + revision)
    head = locator[: len(locator) - len(revision)]
    entries[0] = dict(
        entry,
        repository_revision=revision.upper(),
        pin_evidence=dict(
            pin,
            locator=head + revision.upper(),
            observed_ref_object_id=ref_object_id.upper(),
        ),
    )

    normalized = eq.validate_corpus_manifest(_manifest(entries=entries))
    pinned = next(
        item for item in normalized["entries"] if item["corpus_id"] == entry["corpus_id"]
    )
    assert pinned["repository_revision"] == revision
    assert pinned["pin_evidence"]["locator"] == locator
    assert pinned["pin_evidence"]["observed_ref_object_id"] == ref_object_id
    assert (
        normalized["corpus_manifest_id"]
        == eq.validate_corpus_manifest(_manifest())["corpus_manifest_id"]
    )


def _respell_host(url: str) -> str:
    """Uppercase only the authority: DNS is case-insensitive, a URL path is not."""
    host = url[len("https://") :].partition("/")[0]
    return url.replace(host, host.upper(), 1)


@pytest.mark.parametrize(
    "respell",
    [
        pytest.param(lambda url: f"{url}.git", id="dot_git_suffix"),
        pytest.param(lambda url: f"{url}/", id="trailing_slash"),
        pytest.param(_respell_host, id="host_case"),
        pytest.param(lambda url: f"{_respell_host(url)}.git/", id="host_case_dot_git_slash"),
    ],
)
def test_cosmetic_repository_spellings_still_contain_their_own_pin_locator(respell):
    """One repository spelled two ways still owns the locator it pinned.

    ``_repository_identity`` already rules that a trailing slash, a ``.git`` suffix
    and host case are cosmetic, so containment has to read the same rule. Comparing
    raw ``repository_url`` bytes rejected a ``…/llvm-project.git`` pin against its own
    canonical ``…/llvm-project/commit/<oid>`` locator.
    """
    entries = _entries()
    entry = entries[0]
    entries[0] = dict(entry, repository_url=respell(entry["repository_url"]))
    assert entries[0]["repository_url"] != entry["repository_url"]

    normalized = eq.validate_corpus_manifest(_manifest(entries=entries))
    pinned = next(
        item for item in normalized["entries"] if item["corpus_id"] == entry["corpus_id"]
    )
    assert pinned["pin_evidence"]["locator"] == entry["pin_evidence"]["locator"]


def test_locator_keeping_the_git_suffix_is_contained_by_the_bare_repository():
    """The suffix is cosmetic wherever the repository segment ends, not only at the tail.

    This is the mirror of the case above: here the pinned URL is bare and the locator
    carries ``.git`` in its own repository segment. Normalizing only one operand would
    still reject one of the two directions.
    """
    entries = _entries()
    entry = entries[0]
    url = entry["repository_url"]
    locator = entry["pin_evidence"]["locator"]
    assert locator.startswith(f"{url}/")
    respelled = f"{url}.git{locator[len(url):]}"
    entries[0] = dict(entry, pin_evidence=dict(entry["pin_evidence"], locator=respelled))

    normalized = eq.validate_corpus_manifest(_manifest(entries=entries))
    pinned = next(
        item for item in normalized["entries"] if item["corpus_id"] == entry["corpus_id"]
    )
    assert pinned["pin_evidence"]["locator"] == respelled


def test_sibling_repository_sharing_a_name_prefix_is_still_not_contained():
    """Collapsing cosmetic spellings must not soften the path-segment boundary.

    ``llvm-project-mirror`` merely starts with ``llvm-project``; it is a different
    repository, and a prefix comparison without the ``/`` boundary would admit it.
    """
    entries = _entries()
    entry = entries[0]
    entries[0] = dict(
        entry,
        pin_evidence=dict(
            entry["pin_evidence"],
            locator=f"{entry['repository_url']}-mirror/commit/{entry['repository_revision']}",
        ),
    )
    with pytest.raises(eq.InvalidCorpusError, match="must point into"):
        eq.validate_corpus_manifest(_manifest(entries=entries))


@pytest.mark.parametrize("ref_object_id", ["main", "refs/tags/v1", "abc1234", "z" * 40])
def test_pin_evidence_rejects_a_ref_object_id_that_is_not_an_immutable_oid(ref_object_id):
    """The tag object id is an oid like any other: a name or a short sha is not a pin."""
    entries = _entries()
    entries[0] = dict(
        entries[0],
        pin_evidence=dict(entries[0]["pin_evidence"], observed_ref_object_id=ref_object_id),
    )
    with pytest.raises(eq.InvalidCorpusError, match="observed_ref_object_id"):
        eq.validate_corpus_manifest(_manifest(entries=entries))


@pytest.mark.parametrize("observed_ref", ["refs/heads/main", "HEAD", "main", "refs/tags/"])
def test_pin_evidence_rejects_a_floating_observed_ref(observed_ref):
    entries = _entries()
    entries[0] = dict(
        entries[0],
        pin_evidence=dict(entries[0]["pin_evidence"], observed_ref=observed_ref),
    )
    with pytest.raises(eq.InvalidCorpusError, match="immutable refs/tags/"):
        eq.validate_corpus_manifest(_manifest(entries=entries))


def test_pin_evidence_rejects_an_unrecognized_observation_method():
    entries = _entries()
    entries[0] = dict(
        entries[0],
        pin_evidence=dict(entries[0]["pin_evidence"], observation_method="i_remembered_it"),
    )
    with pytest.raises(eq.InvalidCorpusError, match="observation_method"):
        eq.validate_corpus_manifest(_manifest(entries=entries))


def test_manifest_digest_is_independent_of_input_ordering(normalized_manifest):
    shuffled_entries = list(reversed(_entries()))
    shuffled_entries = [
        dict(reversed(list(entry.items()))) for entry in shuffled_entries
    ]
    reordered = dict(reversed(list(_manifest(entries=shuffled_entries).items())))
    assert (
        eq.validate_corpus_manifest(reordered)["corpus_manifest_id"]
        == normalized_manifest["corpus_manifest_id"]
    )


def test_manifest_round_trips_through_canonical_json(normalized_manifest):
    replayed = eq.validate_corpus_manifest(json.loads(eq.canonical_json(normalized_manifest)))
    assert replayed == normalized_manifest


def test_manifest_rejects_tampered_identity(normalized_manifest):
    tampered = dict(normalized_manifest, corpus_manifest_id="sha256:" + _digest("not-the-digest"))
    with pytest.raises(eq.InvalidCorpusError, match="does not match the canonical digest"):
        eq.validate_corpus_manifest(tampered)


def test_manifest_rejects_duplicate_corpus_id():
    entries = _entries()
    duplicate = dict(entries[0], repository_url="https://github.com/example/other-repo")
    with pytest.raises(eq.InvalidCorpusError, match="duplicate corpus identity"):
        eq.validate_corpus_manifest(_manifest(entries=[*entries, duplicate]))


@pytest.mark.parametrize(
    "spelling",
    [
        "https://github.com/llvm/llvm-project.git",
        # Case and suffix are both cosmetic, and they compose: an uppercase
        # ``.GIT`` is the same repository and must not survive the strip.
        "https://github.com/llvm/llvm-project.GIT",
        "https://github.com/LLVM/LLVM-Project.Git",
        "https://github.com/llvm/llvm-project/",
        "https://github.com/LLVM/LLVM-Project",
    ],
)
def test_manifest_rejects_duplicate_repository_identity(spelling):
    entries = _entries()
    duplicate = dict(entries[0], corpus_id="cpp-llvm-again", repository_url=spelling)
    with pytest.raises(eq.InvalidCorpusError, match="duplicate corpus identity"):
        eq.validate_corpus_manifest(_manifest(entries=[*entries, duplicate]))


@pytest.mark.parametrize(
    "revision",
    ["main", "HEAD", "master", "develop", "latest", "refs/heads/main", "origin/HEAD", "v1.2.3"],
)
def test_manifest_rejects_floating_revisions_and_default_branches(revision):
    entries = _entries()
    entries[0] = dict(entries[0], repository_revision=revision)
    with pytest.raises(eq.InvalidCorpusError, match="floating revision|immutable full-length"):
        eq.validate_corpus_manifest(_manifest(entries=entries))


def test_manifest_rejects_abbreviated_revision():
    entries = _entries()
    entries[0] = dict(entries[0], repository_revision=_oid("cpp-llvm-project")[:12])
    with pytest.raises(eq.InvalidCorpusError, match="immutable full-length"):
        eq.validate_corpus_manifest(_manifest(entries=entries))


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://github.com/llvm/llvm-project", "public https"),
        ("git@github.com:llvm/llvm-project.git", "public https"),
        ("ssh://git@github.com/llvm/llvm-project", "public https"),
        ("file:///srv/mirrors/llvm-project", "local file"),
        ("https://token@github.com/llvm/llvm-project", "embedded URL credentials"),
        ("https://github.com/llvm/llvm-project?token=abc", "query and fragment"),
    ],
)
def test_manifest_rejects_non_public_or_credentialed_urls(url, reason):
    entries = _entries()
    entries[0] = dict(entries[0], repository_url=url)
    with pytest.raises(eq.InvalidCorpusError, match=reason):
        eq.validate_corpus_manifest(_manifest(entries=entries))


def test_manifest_rejects_owner_local_path_in_rationale():
    entries = _entries()
    entries[0] = dict(entries[0], rationale="/home/shrek/checkouts/llvm-project is cheaper")
    with pytest.raises(eq.InvalidCorpusError, match="owner-local filesystem path"):
        eq.validate_corpus_manifest(_manifest(entries=entries))


# --------------------------------------------------------------------------
# Acceptance authority
# --------------------------------------------------------------------------


def test_fixture_receipt_is_what_the_canonical_projection_builds(tmp_path):
    """The fixtures below are the real receipt, not a look-alike of one."""
    produced = process_launcher_acceptance.accepted_outcome_receipt(
        _promoted_repo(tmp_path),
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        claim_epoch=CLAIM_EPOCH,
        base_oid=BASE_OID,
        promoted_paths=list(PROMOTED_PATHS),
        changed_path_hashes=dict(CHANGED_PATH_HASHES),
        attempt_artifact_manifest=ATTEMPT_MANIFEST,
    )
    assert produced == _receipt()
    assert set(produced) == eq.ACCEPTED_OUTCOME_RECEIPT_FIELDS


def test_accepted_outcome_requires_receipt_identity(normalized_manifest, authority):
    accepted = eq.validate_run_artifact(
        _artifact(normalized_manifest), acceptance_authority=authority
    )
    assert accepted["success"] is True
    assert accepted["accepted_outcome_receipt"] == _receipt()
    assert eq.is_accepted_outcome(
        _artifact(normalized_manifest), acceptance_authority=authority
    ) is True

    with pytest.raises(eq.InvalidRunArtifactError, match="manager acceptance identity required"):
        eq.validate_run_artifact(
            _artifact(normalized_manifest, accepted_outcome_receipt=None),
            acceptance_authority=authority,
        )


@pytest.mark.parametrize("signal", sorted(eq.NON_ACCEPTANCE_SIGNALS))
def test_progress_signals_cannot_be_projected_as_success(normalized_manifest, signal):
    with pytest.raises(eq.InvalidRunArtifactError, match="progress signal"):
        eq.validate_run_artifact(_artifact(normalized_manifest, outcome_reason=signal))


@pytest.mark.parametrize(
    "missing",
    [
        "schema_id",
        "receipt_id",
        "claim_epoch",
        "base_oid",
        "promoted_paths",
        "changed_path_hashes",
        "attempt_artifact_manifest_id",
        "repository_revision",
    ],
)
def test_receipt_missing_a_canonical_field_fails_closed(normalized_manifest, missing):
    """Each dropped receipt is resealed, so it fails on the field set itself."""
    artifact = _artifact(
        normalized_manifest, accepted_outcome_receipt=_receipt(drop=(missing,))
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="field set required"):
        eq.validate_run_artifact(artifact)


def test_receipt_rejects_an_invented_field(normalized_manifest):
    """``manager_decision`` is not part of the canonical receipt and never was."""
    artifact = _artifact(
        normalized_manifest, accepted_outcome_receipt=_receipt(manager_decision="accepted")
    )
    with pytest.raises(eq.InvalidRunArtifactError, match=r"unexpected=\['manager_decision'\]"):
        eq.validate_run_artifact(artifact)


def test_receipt_rejects_a_foreign_schema_id(normalized_manifest):
    artifact = _artifact(
        normalized_manifest,
        accepted_outcome_receipt=_receipt(schema_id="aiworkhub.some_other_receipt.v1"),
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="schema_id: must be"):
        eq.validate_run_artifact(artifact)


def test_receipt_digest_mismatch_fails_closed(normalized_manifest):
    artifact = _artifact(
        normalized_manifest,
        accepted_outcome_receipt=_receipt(receipt_id="sha256:" + _digest("not-the-receipt")),
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="recomputed canonical receipt digest"):
        eq.validate_run_artifact(artifact)


def test_receipt_revision_must_derive_from_its_own_promotion(normalized_manifest):
    artifact = _artifact(
        normalized_manifest, accepted_outcome_receipt=_receipt(base_oid="0" * 40)
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="does not match the base oid"):
        eq.validate_run_artifact(artifact)


def test_receipt_changed_hashes_must_cover_every_promoted_path(normalized_manifest):
    hashes = dict(CHANGED_PATH_HASHES)
    hashes.pop(PROMOTED_PATHS[0])
    artifact = _artifact(
        normalized_manifest, accepted_outcome_receipt=_receipt(changed_path_hashes=hashes)
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="one canonical hash per promoted path"):
        eq.validate_run_artifact(artifact)


def test_receipt_must_bind_this_attempt(normalized_manifest):
    artifact = _artifact(
        normalized_manifest, accepted_outcome_receipt=_receipt(request_id="req-0000000000000002")
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="does not bind this attempt"):
        eq.validate_run_artifact(artifact)


def test_receipt_must_bind_this_task(normalized_manifest):
    artifact = _artifact(
        normalized_manifest, accepted_outcome_receipt=_receipt(task_id="SOME_OTHER_TASK")
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="does not bind this task"):
        eq.validate_run_artifact(artifact)


def test_canonical_authority_authenticates_the_receipt(normalized_manifest, tmp_path):
    authority = eq.canonical_acceptance_authority(
        _promoted_repo(tmp_path), _sealed_card(), task_id=TASK_ID, request_id=REQUEST_ID
    )
    accepted = eq.validate_run_artifact(
        _artifact(normalized_manifest), acceptance_authority=authority
    )
    assert accepted["success"] is True
    assert eq.is_accepted_outcome(
        _artifact(normalized_manifest), acceptance_authority=authority
    ) is True


def test_zero_promotion_readonly_receipt_is_authenticated(normalized_manifest, tmp_path):
    """A readonly acceptance promotes nothing, and the canonical authority admits it.

    ``promoted_paths: []`` beside ``changed_path_hashes: {}`` is exactly what the
    canonical projection builds for an accepted outcome that changed no
    repository bytes -- readonly research, a quality review -- and what
    ``task_engine._validate_accepted_outcome_receipt`` authenticates for it.
    This module only rechecks that identity, so refusing a zero-promotion
    receipt here would be a second, stricter acceptance authority rejecting a
    receipt the only one granted.
    """
    repo = _promoted_repo(tmp_path)
    receipt = process_launcher_acceptance.accepted_outcome_receipt(
        repo,
        task_id=TASK_ID,
        request_id=REQUEST_ID,
        claim_epoch=CLAIM_EPOCH,
        base_oid=BASE_OID,
        promoted_paths=[],
        changed_path_hashes={},
        attempt_artifact_manifest=ATTEMPT_MANIFEST,
    )
    assert receipt["promoted_paths"] == []
    assert receipt["changed_path_hashes"] == {}

    card = _sealed_card()
    card["terminal_review"]["evidence"].update(changed_paths=[], changed_path_hashes={})
    authority = eq.canonical_acceptance_authority(
        repo, card, task_id=TASK_ID, request_id=REQUEST_ID
    )
    accepted = eq.validate_run_artifact(
        _artifact(normalized_manifest, accepted_outcome_receipt=receipt),
        acceptance_authority=authority,
    )
    assert accepted["accepted_outcome_receipt"] == receipt
    assert accepted["canonical_authority_state"] == "pass"
    assert accepted["success"] is True


def test_canonical_authority_rejects_promoted_bytes_that_changed(normalized_manifest, tmp_path):
    """Sealed hashes bind on-disk bytes, so a post-promotion edit breaks acceptance."""
    repo = _promoted_repo(tmp_path)
    authority = eq.canonical_acceptance_authority(
        repo, _sealed_card(), task_id=TASK_ID, request_id=REQUEST_ID
    )
    (repo / PROMOTED_PATHS[0]).write_bytes(b"edited after promotion\n")
    with pytest.raises(eq.InvalidRunArtifactError, match="canonical_hash_mismatch"):
        eq.validate_run_artifact(_artifact(normalized_manifest), acceptance_authority=authority)


def test_canonical_authority_rejects_a_foreign_claim_epoch(normalized_manifest, tmp_path):
    card = _sealed_card()
    card["claim_epoch"] = CLAIM_EPOCH + 1
    authority = eq.canonical_acceptance_authority(
        _promoted_repo(tmp_path), card, task_id=TASK_ID, request_id=REQUEST_ID
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="identity_mismatch"):
        eq.validate_run_artifact(_artifact(normalized_manifest), acceptance_authority=authority)


def test_canonical_authority_rejects_unsealed_terminal_evidence(normalized_manifest, tmp_path):
    authority = eq.canonical_acceptance_authority(
        _promoted_repo(tmp_path),
        {"claim_epoch": CLAIM_EPOCH},
        task_id=TASK_ID,
        request_id=REQUEST_ID,
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="sealed_evidence_missing"):
        eq.validate_run_artifact(_artifact(normalized_manifest), acceptance_authority=authority)


def test_unauthenticated_receipt_is_recorded_but_is_not_success(normalized_manifest):
    """A receipt no authority has checked is a recorded claim, not an acceptance."""
    artifact = _artifact(
        normalized_manifest, receipt_authentication={"state": eq.UNKNOWN, "evidence": []}
    )
    normalized = eq.validate_run_artifact(artifact)
    assert normalized["outcome"] == eq.OUTCOME_ACCEPTED
    assert normalized["receipt_authentication"]["state"] == eq.UNKNOWN
    assert normalized["success"] is False


def test_accepted_outcome_requires_recorded_authentication(normalized_manifest):
    artifact = _artifact(normalized_manifest, receipt_authentication=None)
    with pytest.raises(eq.InvalidRunArtifactError, match="receipt_authentication: mapping"):
        eq.validate_run_artifact(artifact)


def test_declared_authentication_cannot_outrun_the_canonical_authority(
    normalized_manifest, authority
):
    artifact = _artifact(
        normalized_manifest, receipt_authentication={"state": eq.UNKNOWN, "evidence": []}
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="contradicts the canonical"):
        eq.validate_run_artifact(artifact, acceptance_authority=authority)


def test_forged_authentication_block_cannot_project_success(normalized_manifest):
    """A well-shaped `pass` block is a claim about authentication, not authentication.

    ``_artifact`` already carries exactly what a forger would hand-assemble: a
    receipt whose canonical identity checks out, under a ``receipt_authentication``
    of ``state: pass`` with a well-formed, digest-bearing locator. With no bound
    authority to produce that verdict, none of it is authentication, so the row
    stays accepted-but-unsuccessful instead of projecting an acceptance.
    """
    artifact = _artifact(normalized_manifest)
    assert artifact["receipt_authentication"]["state"] == "pass"

    normalized = eq.validate_run_artifact(artifact)
    assert normalized["outcome"] == eq.OUTCOME_ACCEPTED
    assert normalized["receipt_authentication"]["state"] == "pass"
    assert normalized["canonical_authority_state"] == eq.UNKNOWN
    assert normalized["success"] is False
    assert eq.is_accepted_outcome(artifact) is False


def test_forged_authentication_cannot_declare_itself_successful(normalized_manifest):
    artifact = _artifact(normalized_manifest, success=True)
    with pytest.raises(eq.InvalidRunArtifactError, match="declared success contradicts"):
        eq.validate_run_artifact(artifact)


def test_forged_authority_state_cannot_be_self_declared(normalized_manifest):
    """Claiming the authority ran is rejected outright, not quietly believed."""
    artifact = _artifact(normalized_manifest, canonical_authority_state="pass")
    with pytest.raises(eq.InvalidRunArtifactError, match="contradicts the authority"):
        eq.validate_run_artifact(artifact)


def test_only_the_bound_authority_turns_an_acceptance_into_success(
    normalized_manifest, authority
):
    artifact = _artifact(normalized_manifest)
    assert eq.validate_run_artifact(artifact)["success"] is False

    authenticated = eq.validate_run_artifact(artifact, acceptance_authority=authority)
    assert authenticated["canonical_authority_state"] == "pass"
    assert authenticated["success"] is True


def test_non_accepted_outcome_may_not_carry_authentication(normalized_manifest):
    artifact = _artifact(
        normalized_manifest,
        outcome=eq.OUTCOME_REJECTED,
        outcome_reason="manager_rejected_insufficient_evidence",
        accepted_outcome_receipt=None,
        receipt_authentication={
            "state": "pass",
            "evidence": [_evidence("acceptance", "ci://run/2/acceptance.json")],
        },
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="may carry receipt authentication"):
        eq.validate_run_artifact(artifact)


def test_passing_validation_alone_is_not_acceptance(normalized_manifest):
    """A green validation block without a receipt stays a non-accepted outcome."""
    artifact = _artifact(
        normalized_manifest,
        outcome=eq.OUTCOME_REJECTED,
        outcome_reason="validation_passed",
        accepted_outcome_receipt=None,
    )
    normalized = eq.validate_run_artifact(artifact)
    assert normalized["validation"]["state"] == "pass"
    assert normalized["success"] is False
    assert eq.is_accepted_outcome(artifact) is False


def test_acceptance_requires_passing_validation_and_review(normalized_manifest):
    for block in ("validation", "review"):
        artifact = _artifact(
            normalized_manifest, **{block: {"state": eq.UNKNOWN, "evidence": []}}
        )
        expected = f"accepted outcome requires passing {block}"
        with pytest.raises(eq.InvalidRunArtifactError, match=expected):
            eq.validate_run_artifact(artifact)


def test_declared_success_cannot_contradict_the_evidence(normalized_manifest):
    artifact = _artifact(
        normalized_manifest,
        outcome=eq.OUTCOME_FAILED,
        outcome_reason="worker_process_crashed",
        accepted_outcome_receipt=None,
        success=True,
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="declared success contradicts"):
        eq.validate_run_artifact(artifact)


# --------------------------------------------------------------------------
# Negative outcomes share the schema
# --------------------------------------------------------------------------


def test_negative_outcomes_use_the_identical_schema(normalized_manifest):
    accepted = eq.validate_run_artifact(_artifact(normalized_manifest))
    rejected = eq.validate_run_artifact(
        _artifact(
            normalized_manifest,
            outcome=eq.OUTCOME_REJECTED,
            outcome_reason="manager_rejected_insufficient_evidence",
            accepted_outcome_receipt=None,
        )
    )
    failed = eq.validate_run_artifact(
        _artifact(
            normalized_manifest,
            outcome=eq.OUTCOME_FAILED,
            outcome_reason="worker_process_crashed",
            accepted_outcome_receipt=None,
            validation={"state": "fail", "evidence": [_evidence("v", "ci://run/2/v.json")]},
            review={"state": eq.UNKNOWN, "evidence": []},
        )
    )
    assert set(accepted) == set(rejected) == set(failed)
    assert (rejected["success"], failed["success"]) == (False, False)
    assert rejected["accepted_outcome_receipt"] is None
    assert failed["review"]["state"] == eq.UNKNOWN


def test_non_accepted_outcome_may_not_carry_a_receipt(normalized_manifest):
    artifact = _artifact(
        normalized_manifest,
        outcome=eq.OUTCOME_REJECTED,
        outcome_reason="manager_rejected_insufficient_evidence",
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="only an accepted outcome may carry"):
        eq.validate_run_artifact(artifact)


# --------------------------------------------------------------------------
# UNKNOWN discipline
# --------------------------------------------------------------------------


def test_missing_usage_is_unknown_and_never_zero(normalized_manifest):
    normalized = eq.validate_run_artifact(_artifact(normalized_manifest, usage={}))
    usage = normalized["usage"]
    assert usage["cost_usd"] == eq.UNKNOWN
    assert usage["total_tokens"] == eq.UNKNOWN
    assert usage["cost_known"] is False
    assert usage["tokens_known"] is False
    # Checked per metric key rather than over usage.values(): ``0 == False`` in
    # Python, so a values() membership test would pass on the boolean flags.
    for metric in ("input_tokens", "output_tokens", "total_tokens", "cost_usd"):
        assert usage[metric] == eq.UNKNOWN
        assert usage[metric] != 0


def test_declared_cost_known_cannot_contradict_observed_cost(normalized_manifest):
    artifact = _artifact(normalized_manifest, usage={"cost_known": True})
    with pytest.raises(eq.InvalidRunArtifactError, match="contradicts the observed evidence"):
        eq.validate_run_artifact(artifact)


def test_summary_keeps_thin_populations_unknown(normalized_manifest, authority):
    artifacts = [
        _artifact(
            normalized_manifest,
            attempt_id=f"attempt-{index:016d}",
            usage={
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "cost_usd": 0.5,
            },
        )
        for index in range(3)
    ]
    summary = eq.qualification_summary(
        artifacts,
        corpus_manifest=normalized_manifest,
        minimum_samples_per_profile=3,
        acceptance_authority=authority,
    )
    measured = summary["profiles"][eq.PROFILE_CPP_LARGE]
    assert measured["state"] == "MEASURED"
    assert measured["acceptance_rate"] == 1.0
    assert measured["observed_cost_usd"] == 1.5
    assert measured["observed_total_tokens"] == 45

    for profile in eq.REQUIRED_PROFILES - {eq.PROFILE_CPP_LARGE}:
        empty = summary["profiles"][profile]
        assert empty["state"] == eq.UNKNOWN
        assert empty["reason"] == "no_observed_run"
        assert empty["acceptance_rate"] == eq.UNKNOWN
        assert empty["observed_cost_usd"] == eq.UNKNOWN
        assert empty["observed_total_tokens"] == eq.UNKNOWN


def test_summary_without_an_authority_is_unknown_not_a_measured_zero(normalized_manifest):
    """Never asking the authority is UNKNOWN, not a measured 0% acceptance rate."""
    artifacts = [
        _artifact(normalized_manifest, attempt_id=f"attempt-{index:016d}")
        for index in range(3)
    ]
    summary = eq.qualification_summary(
        artifacts, corpus_manifest=normalized_manifest, minimum_samples_per_profile=3
    )
    row = summary["profiles"][eq.PROFILE_CPP_LARGE]
    assert row["observed_runs"] == 3
    # The rows recorded three acceptances; none of them was authenticated,
    # because nothing was ever in a position to authenticate them.
    assert row["recorded_accepted_outcomes"] == 3
    assert row["authenticated_accepted_outcomes"] == 0
    assert row["state"] == eq.UNKNOWN
    assert row["reason"] == "acceptance_authority_not_consulted"
    assert row["acceptance_rate"] == eq.UNKNOWN
    assert row["acceptance_rate"] != 0.0


def test_summary_counters_partition_the_observed_population(normalized_manifest, authority):
    """Recorded outcomes partition observed_runs; authenticated acceptance does not.

    Counting an authenticated acceptance alongside recorded rejections and
    failures mixes two different facts, and the three counters then no longer
    add up to the population they describe.
    """
    artifacts = [
        _artifact(normalized_manifest, attempt_id="attempt-0000000000000001"),
        _artifact(
            normalized_manifest,
            attempt_id="attempt-0000000000000002",
            outcome=eq.OUTCOME_REJECTED,
            outcome_reason="manager_rejected_insufficient_evidence",
            accepted_outcome_receipt=None,
        ),
        _artifact(
            normalized_manifest,
            attempt_id="attempt-0000000000000003",
            outcome=eq.OUTCOME_FAILED,
            outcome_reason="worker_process_crashed",
            accepted_outcome_receipt=None,
        ),
    ]
    summary = eq.qualification_summary(
        artifacts,
        corpus_manifest=normalized_manifest,
        minimum_samples_per_profile=3,
        acceptance_authority=authority,
    )
    row = summary["profiles"][eq.PROFILE_CPP_LARGE]
    partition = (
        row["recorded_accepted_outcomes"]
        + row["recorded_rejected_outcomes"]
        + row["recorded_failed_outcomes"]
    )
    assert partition == row["observed_runs"] == 3
    assert (row["recorded_rejected_outcomes"], row["recorded_failed_outcomes"]) == (1, 1)
    assert row["authenticated_accepted_outcomes"] == 1
    assert row["state"] == "MEASURED"
    assert row["acceptance_rate"] == round(1 / 3, 6)

    for profile in eq.REQUIRED_PROFILES - {eq.PROFILE_CPP_LARGE}:
        empty = summary["profiles"][profile]
        assert empty["observed_runs"] == 0
        assert empty["recorded_accepted_outcomes"] == 0
        assert empty["authenticated_accepted_outcomes"] == 0


def test_summary_below_minimum_samples_is_unknown_not_an_average(
    normalized_manifest, authority
):
    artifacts = [_artifact(normalized_manifest)]
    summary = eq.qualification_summary(
        artifacts,
        corpus_manifest=normalized_manifest,
        minimum_samples_per_profile=3,
        acceptance_authority=authority,
    )
    row = summary["profiles"][eq.PROFILE_CPP_LARGE]
    assert row["state"] == eq.UNKNOWN
    assert row["reason"] == "insufficient_samples"
    assert row["acceptance_rate"] == eq.UNKNOWN
    assert row["observed_runs"] == 1
    assert row["recorded_accepted_outcomes"] == 1
    assert row["authenticated_accepted_outcomes"] == 1


def test_summary_one_unknown_cost_makes_the_population_unknown(
    normalized_manifest, authority
):
    artifacts = [
        _artifact(normalized_manifest, attempt_id="attempt-0000000000000001"),
        _artifact(normalized_manifest, attempt_id="attempt-0000000000000002", usage={}),
    ]
    summary = eq.qualification_summary(
        artifacts,
        corpus_manifest=normalized_manifest,
        minimum_samples_per_profile=1,
        acceptance_authority=authority,
    )
    row = summary["profiles"][eq.PROFILE_CPP_LARGE]
    assert row["state"] == "MEASURED"
    assert row["observed_cost_usd"] == eq.UNKNOWN
    assert row["observed_cost_reason"] == "one_or_more_attempt_costs_unknown"
    assert row["observed_total_tokens"] == eq.UNKNOWN


def test_summary_rejects_a_meaningless_minimum(normalized_manifest):
    with pytest.raises(ValueError, match="at least 1"):
        eq.qualification_summary(
            [], corpus_manifest=normalized_manifest, minimum_samples_per_profile=0
        )


def test_summary_spanning_two_attempts_records_the_row_it_cannot_authenticate(
    normalized_manifest, authority
):
    """One attempt's authority must not abort a summary covering two attempts.

    ``qualification_summary`` threads a single acceptance authority down every
    row, but a bound authority speaks for exactly one ``(task_id, request_id)``.
    Asking it about the neighbouring attempt returns its identity refusal, which
    answers the question rather than judging the row -- and raising on it would
    delete the first attempt's evidence along with the second's.
    """
    other_request = "req-0000000000000002"
    artifacts = [
        _artifact(normalized_manifest, attempt_id="attempt-0000000000000001", retries=2),
        _artifact(
            normalized_manifest,
            attempt_id="attempt-0000000000000002",
            request_id=other_request,
            accepted_outcome_receipt=_receipt(request_id=other_request),
            retries=3,
        ),
    ]
    summary = eq.qualification_summary(
        artifacts,
        corpus_manifest=normalized_manifest,
        minimum_samples_per_profile=1,
        acceptance_authority=authority,
    )
    row = summary["profiles"][eq.PROFILE_CPP_LARGE]
    assert row["observed_runs"] == 2
    assert len(summary["run_artifact_ids"]) == 2
    # Both acceptances are recorded; only the in-scope one is authenticated.
    assert row["recorded_accepted_outcomes"] == 2
    assert row["authenticated_accepted_outcomes"] == 1
    assert row["unauthenticated_accepted_outcomes"] == 1
    assert row["total_retries"] == 5
    # A rate over a population the authority could only half speak for would
    # publish the unasked row as a refusal it never issued.
    assert row["state"] == eq.UNKNOWN
    assert row["reason"] == "acceptance_authority_did_not_cover_every_accepted_row"
    assert row["acceptance_rate"] == eq.UNKNOWN


def test_a_row_outside_the_authority_scope_is_recorded_not_refused(
    normalized_manifest, authority
):
    other_request = "req-0000000000000002"
    artifact = _artifact(
        normalized_manifest,
        request_id=other_request,
        accepted_outcome_receipt=_receipt(request_id=other_request),
    )
    normalized = eq.validate_run_artifact(artifact, acceptance_authority=authority)
    assert normalized["outcome"] == eq.OUTCOME_ACCEPTED
    assert normalized["canonical_authority_state"] == eq.UNKNOWN
    assert (
        normalized["canonical_authority_reason"]
        == "acceptance_authority_bound_to_another_attempt"
    )
    assert normalized["success"] is False
    assert eq.is_accepted_outcome(artifact, acceptance_authority=authority) is False


def test_the_canonical_authority_declares_the_attempt_it_speaks_for(authority):
    assert eq.acceptance_authority_binding(authority) == (TASK_ID, REQUEST_ID)
    assert eq.acceptance_authority_binding(None) is None


def test_an_authority_declaring_no_scope_is_still_consulted(normalized_manifest, authority):
    """Out-of-scope silence is opt-in: an undeclared binding excuses nothing."""

    def _undeclared(receipt: dict[str, Any]) -> Any:
        return authority(receipt)

    assert eq.acceptance_authority_binding(_undeclared) is None
    other_request = "req-0000000000000002"
    artifact = _artifact(
        normalized_manifest,
        request_id=other_request,
        accepted_outcome_receipt=_receipt(request_id=other_request),
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="rejected this receipt"):
        eq.validate_run_artifact(artifact, acceptance_authority=_undeclared)


def test_declared_authority_reason_cannot_be_self_declared(normalized_manifest):
    """The reason is sealed beside the state, so a replay cannot relabel it."""
    artifact = _artifact(
        normalized_manifest,
        canonical_authority_reason="canonical_authority_authenticated_receipt",
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="contradicts the authority"):
        eq.validate_run_artifact(artifact)


def test_unobserved_profile_reports_unknown_retries_not_a_measured_zero(
    normalized_manifest, authority
):
    """A profile nobody ran did not retry zero times; nothing was observed at all."""
    summary = eq.qualification_summary(
        [_artifact(normalized_manifest)],
        corpus_manifest=normalized_manifest,
        minimum_samples_per_profile=1,
        acceptance_authority=authority,
    )
    for profile in eq.REQUIRED_PROFILES - {eq.PROFILE_CPP_LARGE}:
        empty = summary["profiles"][profile]
        assert empty["observed_runs"] == 0
        assert empty["total_retries"] == eq.UNKNOWN
        assert empty["total_retries"] != 0
        assert empty["total_retries_reason"] == "no_observed_run"

    observed = summary["profiles"][eq.PROFILE_CPP_LARGE]
    assert observed["total_retries"] == 1
    assert observed["total_retries_reason"] == "complete_observed_retry_population"


# --------------------------------------------------------------------------
# Cross-platform evidence
# --------------------------------------------------------------------------


@pytest.mark.parametrize("platform", sorted(eq.EVIDENCE_REQUIRED_PLATFORMS))
def test_windows_and_macos_claims_require_evidence(normalized_manifest, platform):
    evidence = {
        key: _evidence(key, f"ci://run/1/{key}/report.json")
        for key in eq.EVIDENCE_REQUIRED_PLATFORMS
        if key != platform
    }
    artifact = _artifact(normalized_manifest, platform_evidence=evidence)
    with pytest.raises(eq.InvalidRunArtifactError, match="without a retrievable evidence"):
        eq.validate_run_artifact(artifact)


def test_linux_only_claim_needs_no_cross_platform_evidence(normalized_manifest):
    normalized = eq.validate_run_artifact(
        _artifact(normalized_manifest, platform_claims=["linux"], platform_evidence={})
    )
    assert normalized["platform_claims"] == ["linux"]


def test_evidence_for_an_unclaimed_platform_is_rejected(normalized_manifest):
    artifact = _artifact(normalized_manifest, platform_claims=["linux", "macos", "windows"])
    artifact["platform_evidence"] = dict(artifact["platform_evidence"])
    artifact["platform_claims"] = ["linux", "macos"]
    with pytest.raises(eq.InvalidRunArtifactError, match="does not claim"):
        eq.validate_run_artifact(artifact)


def _locator_artifact(normalized: dict[str, Any], locator: str) -> dict[str, Any]:
    """A run artifact whose Windows evidence reference carries exactly ``locator``."""
    windows = dict(_evidence("windows", "ci://run/1/windows/report.json"), locator=locator)
    return _artifact(
        normalized,
        platform_evidence={
            "macos": _evidence("macos", "ci://run/1/macos/report.json"),
            "windows": windows,
        },
    )


@pytest.mark.parametrize(
    "locator",
    [
        "/home/shrek/ci/windows-report.json",
        "~/ci/windows-report.json",
        "C:/builds/windows-report.json",
        "file:///var/log/windows-report.json",
        "\\\\buildhost\\share\\windows-report.json",
        "/Volumes/backup/windows-report.json",
        "/Library/Logs/aiworkhub/windows-report.json",
    ],
)
def test_owner_local_paths_are_not_retrievable_locators(normalized_manifest, locator):
    with pytest.raises(
        eq.InvalidRunArtifactError, match="owner-local filesystem path|local file://|retrievable"
    ):
        eq.validate_run_artifact(_locator_artifact(normalized_manifest, locator))


@pytest.mark.parametrize(
    "locator",
    [
        # Windows spellings wearing a retrievable scheme.
        "ci://C:/builds/windows-report.json",
        "ci://C:\\builds\\windows-report.json",
        "artifact://d:/builds/windows-report.json",
        "ci://\\\\buildhost\\share\\windows-report.json",
        # POSIX spellings wearing a retrievable scheme. The empty authority in
        # ``scheme:///…`` is exactly what makes the remainder an absolute path.
        "artifact:///home/shrek/ci/windows-report.json",
        "artifact:///Users/shrek/ci/windows-report.json",
        "ci:///var/log/windows-report.json",
        "https:///tmp/windows-report.json",
        "ci://~/ci/windows-report.json",
        "artifact://~shrek/ci/windows-report.json",
        # Case variants of the same POSIX roots. Case-insensitive filesystems
        # resolve these to the identical owner-local directories.
        "artifact:///users/shrek/ci/windows-report.json",
        "artifact:///Home/shrek/ci/windows-report.json",
        "ci:///VAR/log/windows-report.json",
        "https:///Tmp/windows-report.json",
        # The macOS roots behind a retrievable scheme, which is where an
        # owner-local path most plausibly hides in a cross-platform locator.
        "artifact:///Volumes/backup/windows-report.json",
        "artifact:///volumes/backup/windows-report.json",
        "ci:///Library/Logs/windows-report.json",
        "ci:///LIBRARY/Logs/windows-report.json",
        # Punctuation standing in for the empty authority. ``.``, ``-`` and ``_`` are
        # nonempty, so the authority check passes, and each one sits inside the
        # whole-string scan's negative lookbehind, so that scan stays silent too --
        # one character laundering exactly the paths rejected above.
        "artifact://./Users/shrek/ci/windows-report.json",
        "ci://-/home/shrek/ci/windows-report.json",
        "artifact://_/var/log/windows-report.json",
    ],
)
def test_scheme_prefixed_owner_local_paths_are_rejected(normalized_manifest, locator):
    """A retrievable scheme in front of a local path does not make it retrievable.

    The whole-string local-path scan cannot see any of these: the path begins
    immediately after ``://`` rather than at a text boundary, so each one used to
    pass as a well-formed ``ci://``/``artifact://`` reference.
    """
    with pytest.raises(
        eq.InvalidRunArtifactError,
        match="owner-local filesystem path|authority-less locator",
    ):
        eq.validate_run_artifact(_locator_artifact(normalized_manifest, locator))


def test_hostless_retrievable_authority_is_still_accepted(normalized_manifest):
    """The rejection above is targeted: `ci://run/…` is a CI run, not a local path."""
    normalized = eq.validate_run_artifact(
        _locator_artifact(normalized_manifest, "ci://run/1/windows/report-2.json")
    )
    assert normalized["platform_claims"] == ["linux", "macos", "windows"]


def test_punctuation_authority_names_no_retrievable_host(normalized_manifest):
    """Punctuation is rejected as an authority even when the path is not owner-local.

    ``artifact://./artifacts/…`` is nobody's host, so it is not retrievable either --
    and leaving it accepted would keep the one-character shim that hides an owner-local
    path alive for the next spelling of a local root.
    """
    with pytest.raises(eq.InvalidRunArtifactError, match="names no retrievable host"):
        eq.validate_run_artifact(
            _locator_artifact(normalized_manifest, "artifact://./artifacts/windows-report.json")
        )


@pytest.mark.parametrize(
    "reason",
    [
        # Colon-adjacent: the path follows a label rather than a text boundary.
        "macos evidence:/Users/shrek/report.json",
        "windows evidence:C:/builds/windows-report.json",
        "windows evidence:\\\\buildhost\\share\\windows-report.json",
        "checkout:~/ci/windows-report.json",
        # Comma-adjacent: the path follows a separator inside a list.
        "rejected,/home/shrek/ci/windows-report.json,retry",
        "rejected,C:\\builds\\windows-report.json",
        "rejected,~/ci/windows-report.json",
        # Case variants, still adjacent to punctuation. A case-insensitive
        # filesystem resolves ``/users`` and ``/Home`` to the same owner-local
        # directories as ``/Users`` and ``/home``, so a case-sensitive matcher
        # would seal the very same path into the artifact under another casing.
        "macos evidence:/users/shrek/report.json",
        "macos evidence:/USERS/shrek/report.json",
        "rejected,/Home/shrek/ci/windows-report.json,retry",
        "rejected,/HOME/shrek/ci/windows-report.json",
        "windows evidence:/Tmp/windows-report.json",
        "logs:/VAR/log/windows-report.json",
        # The macOS roots. ``/Volumes/<disk>`` is a mounted external or network
        # disk and ``/Library`` is per-machine application state; both are as
        # owner-local as ``/home``, and a macOS evidence reference is exactly
        # the field that would carry one, punctuation-adjacent and in any case.
        "macos evidence:/Volumes/backup/report.json",
        "macos evidence:/volumes/backup/report.json",
        "rejected,/VOLUMES/backup/report.json,retry",
        "macos evidence:/Library/Logs/aiworkhub/report.json",
        "rejected,/library/Logs/aiworkhub/report.json,retry",
        "logs:/LIBRARY/Logs/aiworkhub/report.json",
        # A named home directory. ``~shrek/report.json`` resolves to exactly the
        # owner-local home ``~/report.json`` names, so rejecting only the bare
        # ``~/`` spelling would let the same path through under the owner's name.
        "~shrek/report.json",
        "macos evidence:~shrek/report.json",
        "rejected,~shrek/ci/windows-report.json,retry",
        "windows evidence:~shrek\\ci\\windows-report.json",
    ],
)
def test_owner_local_paths_embedded_in_free_text_are_rejected(normalized_manifest, reason):
    """A path is owner-local wherever it sits, not only where a field begins.

    ``outcome_reason`` is free text, so a local path lands there after a colon or
    a comma far more often than at the start of the value. Requiring a leading
    boundary would seal an owner-local path into a replayable artifact.
    """
    with pytest.raises(eq.InvalidRunArtifactError, match="owner-local filesystem path"):
        eq.validate_run_artifact(_artifact(normalized_manifest, outcome_reason=reason))


def test_url_path_segments_are_not_owner_local_paths(normalized_manifest):
    """The rule targets filesystem shapes, so a URL path segment stays accepted."""
    normalized = eq.validate_run_artifact(
        _artifact(
            normalized_manifest,
            outcome_reason=(
                "manager_accepted; evidence at https://ci.example.com/var/tmp/home/run-1 "
                "and https://github.com/llvm/llvm-project/tree/main/opt/README.md"
            ),
        )
    )
    assert normalized["outcome"] == eq.OUTCOME_ACCEPTED


# Every character a left-boundary class would plausibly read as "URL-ish". A
# lookbehind holding these suppressed a rooted POSIX path standing right behind
# one of them, which is where free text most often puts it.
_PATH_ADJACENT_PUNCTUATION = [".", "_", "~", "%", "+", "-"]

# One root per family the ban names: the Linux home, the macOS home, and a
# system root that carries a CI report just as plausibly as a home does.
_OWNER_LOCAL_ROOTS = ["/home/shrek", "/Users/shrek", "/var/log"]


@pytest.mark.parametrize("root", _OWNER_LOCAL_ROOTS)
@pytest.mark.parametrize("punctuation", _PATH_ADJACENT_PUNCTUATION)
def test_punctuation_before_a_rooted_path_does_not_launder_it(
    normalized_manifest, punctuation, root
):
    """Prose punctuation is not a URL, so it cannot exempt the path behind it.

    ``rejected-/home/shrek/ci/windows-report.json`` seals the same owner-local
    path as ``rejected,/home/shrek/…``. Any left boundary wide enough to keep a
    URL path segment accepted accepted these too, because the character in front
    of a URL path segment and the character in front of free text are the same
    character -- only the ``://`` further left tells the two apart.
    """
    reason = f"rejected{punctuation}{root}/ci/windows-report.json"
    with pytest.raises(eq.InvalidRunArtifactError, match="owner-local filesystem path"):
        eq.validate_run_artifact(_artifact(normalized_manifest, outcome_reason=reason))


@pytest.mark.parametrize("root", _OWNER_LOCAL_ROOTS)
@pytest.mark.parametrize("punctuation", _PATH_ADJACENT_PUNCTUATION)
def test_url_path_segments_stay_accepted_after_the_same_punctuation(
    normalized_manifest, punctuation, root
):
    """The paired control: identical bytes inside a URL path stay retrievable.

    Narrowing the boundary must not turn a published CI path such as
    ``https://ci.example.com/run-1-/var/log/windows-report.json`` into a false
    rejection, which is the failure mode the previous widening was fixing.
    """
    locator = f"https://ci.example.com/run-1{punctuation}{root}/windows-report.json"
    normalized = eq.validate_run_artifact(_locator_artifact(normalized_manifest, locator))
    assert normalized["platform_claims"] == ["linux", "macos", "windows"]


# Every character a URL token has to stop at. None of them can continue a URL
# path, so a rooted owner-local path standing immediately behind one is a path,
# not a URL segment -- and no whitespace is needed anywhere. They are swept
# together because a grammar repaired for the comma alone stays broken for the
# next separator someone types.
_URL_TERMINATING_PUNCTUATION = [
    ",", ";", "'", '"', "`", "^", "|", "?", "#",
    "(", ")", "[", "]", "{", "}", "<", ">",
]


@pytest.mark.parametrize("root", _OWNER_LOCAL_ROOTS)
@pytest.mark.parametrize("punctuation", _URL_TERMINATING_PUNCTUATION)
def test_a_url_token_does_not_absorb_a_path_adjacent_to_it(
    normalized_manifest, punctuation, root
):
    """A real URL in front of a local path does not exempt the path behind it.

    ``https://ci.example.com/run/1,/home/shrek/report.json`` is one whitespace-free
    token: the scheme is real, the authority names a host, and the owner-local
    tail begins one separator later. A URL path matched as "everything up to
    whitespace" absorbs that tail into the URL span, where the owner-local
    alternatives never see it -- so the exemption has to end where the URL does.
    """
    reason = (
        f"manager_accepted; evidence at https://ci.example.com/run/1{punctuation}"
        f"{root}/ci/windows-report.json"
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="owner-local filesystem path"):
        eq.validate_run_artifact(_artifact(normalized_manifest, outcome_reason=reason))


def test_comma_adjacent_local_path_in_a_locator_is_rejected(normalized_manifest):
    """The same adjacency in an evidence locator, which is where it matters most.

    A locator is how a Windows or macOS claim is meant to be retrieved by someone
    else. One that carries an owner-local report path behind a comma is neither
    retrievable nor owner-independent, however well-formed its authority is.
    """
    locator = "https://ci.example.com/run/1,/home/shrek/report.json"
    with pytest.raises(eq.InvalidRunArtifactError, match="owner-local filesystem path"):
        eq.validate_run_artifact(_locator_artifact(normalized_manifest, locator))


def test_url_user_directory_segment_is_a_retrievable_locator(normalized_manifest):
    """``/~runner/`` inside a URL path is a CI user directory, not an owner home.

    The tilde alternative needs the same URL-safe left boundary the rooted
    alternative has. Without it, a published per-user CI path is misread as
    ``~user/…`` and a legitimate retrievable locator is rejected.
    """
    normalized = eq.validate_run_artifact(
        _locator_artifact(
            normalized_manifest, "https://ci.example.com/~runner/windows-report.json"
        )
    )
    assert normalized["platform_claims"] == ["linux", "macos", "windows"]


@pytest.mark.parametrize(
    "reason",
    [
        # No ``/`` in front at all: the home reference opens the value.
        "~shrek/report.json",
        # A ``/`` appears later, but the ``~`` follows a colon, so the URL
        # exception never reaches it.
        "checkout:~/ci/report.json",
    ],
)
def test_tilde_home_references_are_rejected_outside_url_paths(normalized_manifest, reason):
    """The URL exception is the narrow case, not the rule for every tilde."""
    with pytest.raises(eq.InvalidRunArtifactError, match="owner-local filesystem path"):
        eq.validate_run_artifact(_artifact(normalized_manifest, outcome_reason=reason))


def test_tilde_approximations_in_prose_are_not_owner_local_paths(normalized_manifest):
    """Widening ``~`` to ``~user`` must not swallow an approximation in prose.

    ``outcome_reason`` is free text that routinely carries rates like ``~3/attempt``.
    A home reference names a user, so the optional name must start with a letter or
    underscore rather than matching any run of characters before the separator.
    """
    normalized = eq.validate_run_artifact(
        _artifact(
            normalized_manifest,
            outcome_reason="manager_accepted after ~3/attempt retries at ~50/sec",
        )
    )
    assert normalized["outcome"] == eq.OUTCOME_ACCEPTED


def test_evidence_reference_requires_a_content_digest(normalized_manifest):
    evidence = _evidence("windows", "ci://run/1/windows/report.json")
    evidence.pop("sha256")
    artifact = _artifact(
        normalized_manifest,
        platform_evidence={
            "macos": _evidence("macos", "ci://run/1/macos/report.json"),
            "windows": evidence,
        },
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="sha256: non-empty string required"):
        eq.validate_run_artifact(artifact)


def test_decided_evidence_block_requires_at_least_one_reference(normalized_manifest):
    artifact = _artifact(
        normalized_manifest,
        outcome=eq.OUTCOME_REJECTED,
        outcome_reason="manager_rejected_insufficient_evidence",
        accepted_outcome_receipt=None,
        review={"state": "fail", "evidence": []},
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="decided state requires"):
        eq.validate_run_artifact(artifact)


# --------------------------------------------------------------------------
# Determinism, round-trip and forbidden content
# --------------------------------------------------------------------------


def test_run_artifact_digest_is_independent_of_input_ordering(normalized_manifest):
    baseline = eq.validate_run_artifact(_artifact(normalized_manifest))

    shuffled = _artifact(normalized_manifest)
    shuffled["platform_claims"] = list(reversed(shuffled["platform_claims"]))
    shuffled["validation"] = {
        "evidence": list(reversed(shuffled["validation"]["evidence"])),
        "state": shuffled["validation"]["state"],
    }
    shuffled = dict(reversed(list(shuffled.items())))

    assert eq.validate_run_artifact(shuffled)["run_artifact_id"] == baseline["run_artifact_id"]


def test_run_artifact_round_trips_through_canonical_json(normalized_manifest):
    baseline = eq.validate_run_artifact(_artifact(normalized_manifest))
    replayed = eq.validate_run_artifact(json.loads(eq.canonical_json(baseline)))
    assert replayed == baseline
    assert eq.canonical_digest(replayed) == eq.canonical_digest(baseline)


def test_authenticated_artifact_round_trips_under_the_same_authority(
    normalized_manifest, authority
):
    baseline = eq.validate_run_artifact(
        _artifact(normalized_manifest), acceptance_authority=authority
    )
    assert baseline["success"] is True
    replayed = eq.validate_run_artifact(
        json.loads(eq.canonical_json(baseline)), acceptance_authority=authority
    )
    assert replayed == baseline
    assert eq.canonical_digest(replayed) == eq.canonical_digest(baseline)


def test_authenticated_artifact_replayed_without_the_authority_fails_closed(
    normalized_manifest, authority
):
    """A sealed success is only reproducible under the authority that granted it."""
    baseline = eq.validate_run_artifact(
        _artifact(normalized_manifest), acceptance_authority=authority
    )
    with pytest.raises(eq.InvalidRunArtifactError, match="contradicts the authority"):
        eq.validate_run_artifact(json.loads(eq.canonical_json(baseline)))


def test_run_artifact_must_match_its_corpus_entry(normalized_manifest):
    artifact = _artifact(normalized_manifest, repository_revision=_oid("some-other-commit"))
    with pytest.raises(eq.InvalidRunArtifactError, match="does not match corpus entry"):
        eq.validate_run_artifact(artifact, corpus_manifest=normalized_manifest)

    # Built from a real entry first: _artifact resolves the fixture by corpus_id,
    # so the unknown identity has to be stamped on afterwards.
    unknown = _artifact(normalized_manifest)
    unknown["corpus_id"] = "not-in-the-corpus"
    with pytest.raises(eq.InvalidRunArtifactError, match="is not in the corpus manifest"):
        eq.validate_run_artifact(unknown, corpus_manifest=normalized_manifest)


def test_run_artifact_rejects_tampered_identity(normalized_manifest):
    baseline = eq.validate_run_artifact(_artifact(normalized_manifest))
    tampered = dict(baseline, run_artifact_id="sha256:" + _digest("not-the-digest"))
    with pytest.raises(eq.InvalidRunArtifactError, match="does not match the canonical digest"):
        eq.validate_run_artifact(tampered)


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "a" * 32,
        "github_pat_" + "b" * 30,
        "AKIA" + "C" * 16,
        "xoxb-0123456789-abcdefghij",
        "-----BEGIN RSA PRIVATE KEY-----",
        "password=hunter2",
        "Authorization: Bearer abc",
    ],
)
def test_credential_shaped_content_is_rejected(normalized_manifest, secret):
    artifact = _artifact(normalized_manifest, outcome_reason=f"accepted after {secret}")
    with pytest.raises(eq.InvalidRunArtifactError, match="credential-shaped content"):
        eq.validate_run_artifact(artifact)


def test_credential_bearing_field_names_are_rejected(normalized_manifest):
    artifact = _artifact(normalized_manifest)
    artifact["api_key"] = "redacted"
    with pytest.raises(eq.InvalidRunArtifactError, match="credential-bearing field"):
        eq.validate_run_artifact(artifact)


def test_non_serializable_values_are_rejected(normalized_manifest):
    artifact = _artifact(normalized_manifest, usage={"cost_usd": float("inf")})
    with pytest.raises(eq.InvalidRunArtifactError, match="not canonically serializable"):
        eq.validate_run_artifact(artifact)

    with pytest.raises(ValueError):
        eq.canonical_json({"cost_usd": float("nan")})


def test_canonical_json_is_compact_and_sorted():
    assert eq.canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert eq.canonical_digest({"a": 2, "b": 1}) == eq.canonical_digest({"b": 1, "a": 2})
    assert eq.canonical_digest({}).startswith("sha256:")


# --------------------------------------------------------------------------
# Phase boundary
# --------------------------------------------------------------------------


def test_phase_boundary_is_documented_in_the_protocol():
    # Collapsed to single-spaced text so a markdown rewrap cannot silently
    # delete the phase-boundary sentence by moving a line break into it.
    flat = " ".join(_PROTOCOL_DOC.read_text(encoding="utf-8").split())
    assert "this phase is foundation only" in flat.lower()
    assert "does **not** clone, fetch, build, mutate or execute any external repository" in flat
    assert "no performance, token or cost claim is established by it" in flat


def test_phase_boundary_travels_on_every_artifact(normalized_manifest):
    artifact = eq.validate_run_artifact(_artifact(normalized_manifest))
    summary = eq.qualification_summary(
        [_artifact(normalized_manifest)], corpus_manifest=normalized_manifest
    )
    assert normalized_manifest["phase_boundary"] == eq.PHASE_BOUNDARY
    assert artifact["phase_boundary"] == eq.PHASE_BOUNDARY
    assert summary["phase_boundary"] == eq.PHASE_BOUNDARY
    assert eq.corpus_specification()["phase_boundary"] == eq.PHASE_BOUNDARY
    assert "foundation_only" in eq.PHASE_BOUNDARY


# --------------------------------------------------------------------------
# The shipped corpus is fixed and pinned
# --------------------------------------------------------------------------


def test_checked_in_corpus_is_fixed_and_pinned():
    """The deliverable is a manifest with observed pins, not a candidate list."""
    manifest = eq.fixed_corpus_manifest()
    assert eq.CORPUS_MANIFEST_STATE == "PINNED"
    assert len(manifest["entries"]) == 4
    assert {entry["profile"] for entry in manifest["entries"]} == eq.REQUIRED_PROFILES

    for entry in manifest["entries"]:
        assert entry["repository_url"].startswith("https://")
        assert re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", entry["repository_revision"])
        assert entry["revision_kind"] == eq.REVISION_KIND
        pin = entry["pin_evidence"]
        assert pin["observation_method"] == "git_ls_remote"
        assert pin["observed_ref"].startswith("refs/tags/")
        assert re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", pin["observed_ref_object_id"])
        assert pin["locator"].startswith(entry["repository_url"] + "/")
        assert pin["locator"].endswith("/" + entry["repository_revision"])


def test_checked_in_corpus_is_validated_at_import():
    """An unpinned or malformed corpus cannot reach a qualification run."""
    assert eq.CORPUS_MANIFEST == eq.fixed_corpus_manifest()
    assert eq.CORPUS_MANIFEST_ID == eq.CORPUS_MANIFEST["corpus_manifest_id"]
    assert eq.CORPUS_MANIFEST_ID.startswith("sha256:")
    assert eq.CORPUS_MANIFEST["corpus_version"] == eq.CORPUS_VERSION


def test_checked_in_corpus_pins_four_distinct_repositories():
    manifest = eq.fixed_corpus_manifest()
    assert len({entry["repository_url"] for entry in manifest["entries"]}) == 4
    assert len({entry["repository_revision"] for entry in manifest["entries"]}) == 4
    assert len({entry["corpus_id"] for entry in manifest["entries"]}) == 4


def test_corpus_specification_reads_back_the_pinned_manifest():
    specification = eq.corpus_specification()
    manifest = eq.fixed_corpus_manifest()
    by_profile = {entry["profile"]: entry for entry in manifest["entries"]}
    assert specification["state"] == "PINNED"
    assert "materialized" in specification["reason"]
    assert specification["corpus_manifest_id"] == manifest["corpus_manifest_id"]
    assert set(specification["profiles"]) == eq.REQUIRED_PROFILES
    for profile, row in specification["profiles"].items():
        assert row["must_demonstrate"]
        assert row["repository_revision"] == by_profile[profile]["repository_revision"]
        assert row["pin_evidence"] == by_profile[profile]["pin_evidence"]

    # A criteria view is not a manifest, so the manifest contract refuses it.
    with pytest.raises(eq.InvalidCorpusError):
        eq.validate_corpus_manifest(specification)


def test_corpus_specification_is_deterministic():
    first = eq.corpus_specification()
    assert eq.corpus_specification() == first
    unsealed = {key: value for key, value in first.items() if key != "corpus_specification_id"}
    assert first["corpus_specification_id"] == eq.canonical_digest(unsealed)


def test_protocol_documents_the_pinned_corpus_state():
    flat = " ".join(_PROTOCOL_DOC.read_text(encoding="utf-8").split())
    assert "the shipped corpus state is `PINNED`" in flat
    assert "read-only ref metadata is not a clone, a build or an execution" in flat.lower()


def test_protocol_names_the_canonical_acceptance_authority():
    flat = " ".join(_PROTOCOL_DOC.read_text(encoding="utf-8").split())
    assert "task_engine._validate_accepted_outcome_receipt" in flat
    assert "fails closed" in flat.lower()
